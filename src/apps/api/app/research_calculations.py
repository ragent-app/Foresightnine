"""Auditable deterministic financial calculations for research tools."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class NumericFact(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    fact_id: str = Field(min_length=1, max_length=200)
    value: Decimal
    unit: str = Field(min_length=1, max_length=100)
    currency: str | None = Field(default=None, min_length=1, max_length=16)
    period: str = Field(min_length=1, max_length=100)

    @field_validator("value")
    @classmethod
    def finite_reasonable_value(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or len(value.as_tuple().digits) > 30 or abs(value.adjusted()) > 30:
            raise ValueError("Value must be finite and within supported precision.")
        return value


class CalculationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    formula: Literal["growth_percent", "margin_percent", "difference"]
    first: NumericFact
    second: NumericFact
    decimal_places: int = Field(default=2, ge=0, le=8)


class RecalculateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent_version_id: str = Field(min_length=1, max_length=100)
    calculation_index: int = Field(ge=0, le=1999, strict=True)


class CalculationError(ValueError):
    """The available inputs do not support the requested calculation."""


_TEXT_NUMBER = re.compile(r"(?<![\w.])[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_TEXT_PERCENT = re.compile(r"(?<![\w.])([-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(?:%|％|percent\b)", re.IGNORECASE)
_RATIO_SCALE_PREFIX = re.compile(r"(?:/|÷)\s*[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*\)*\s*[×*]\s*$")


def _is_ratio_scale_factor(text: str, match: re.Match[str]) -> bool:
    """Recognize only the explicit conversion factor in a numeric ratio.

    A bare 100% or other percentage still needs review. This does not parse
    arbitrary expressions or establish whether a model's formula is correct.
    """
    try:
        return Decimal(match.group(1).replace(",", "").replace("−", "-")) == 100 and bool(
            _RATIO_SCALE_PREFIX.search(text[:match.start()]))
    except InvalidOperation:
        return False


def calculation_input_status(calculation: dict, facts: list[dict]) -> str:
    """Check a retained calculation against this version's fact values.

    This verifies consistency, not whether the facts or the result are true.
    Old or user-supplied records with missing structure fail closed.
    """
    inputs = calculation.get("inputs")
    if not isinstance(inputs, list) or len(inputs) != 2 or any(not isinstance(item, dict) for item in inputs):
        return "unverifiable"
    fields = ("fact_id", "value", "unit", "currency", "period")
    try:
        expected = [NumericFact.model_validate({key: item[key] for key in fields if key in item}) for item in inputs]
        if expected[0].fact_id == expected[1].fact_id:
            return "unverifiable"
        by_id: dict[str, list[dict]] = {}
        for item in facts:
            fact_id = item.get("fact_id")
            if isinstance(fact_id, str):
                by_id.setdefault(fact_id, []).append(item)
        for snapshot in expected:
            candidates = by_id.get(snapshot.fact_id, [])
            if len(candidates) != 1:
                return "missing_or_ambiguous"
            actual = NumericFact.model_validate({key: candidates[0][key] for key in fields if key in candidates[0]})
            if actual != snapshot:
                return "changed"
    except (ValidationError, TypeError, ValueError):
        return "unverifiable"
    return "matched"


def calculate(request: CalculationRequest) -> dict:
    """Calculate first versus second; first is current/numerator.

    Preserve original inputs and reject ambiguous or non-comparable quantities.
    The second input is the baseline for growth and denominator for margin.
    """
    a, b = request.first, request.second
    if a.fact_id == b.fact_id:
        raise CalculationError("Inputs must identify distinct facts.")
    if a.unit != b.unit or a.currency != b.currency:
        raise CalculationError("Input units and currencies must match.")
    if request.formula == "margin_percent" and a.period != b.period:
        raise CalculationError("Margin inputs must have the same period.")
    if request.formula == "growth_percent" and a.period == b.period:
        raise CalculationError("Growth inputs must refer to different periods.")
    if request.formula == "growth_percent" and b.value <= 0:
        raise CalculationError("Growth requires a positive baseline.")
    if request.formula == "margin_percent" and b.value <= 0:
        raise CalculationError("Margin requires a positive denominator.")
    formula_version = "1"
    try:
        with localcontext() as context:
            context.prec = 100
            if request.formula == "growth_percent":
                result = (a.value / b.value - 1) * 100
                expression = "(first / second - 1) * 100"
                unit = "percent"
            elif request.formula == "margin_percent":
                result = a.value / b.value * 100
                expression = "first / second * 100"
                unit = "percent"
            else:
                result = a.value - b.value
                expression = "first - second"
                if a.unit.strip().casefold() in {"percent", "percentage", "%"}:
                    if a.currency is not None:
                        raise CalculationError("Percentage inputs cannot have a currency.")
                    unit = "percentage points"
                    formula_version = "2"
                else:
                    unit = a.unit
            rounded = result.quantize(Decimal(1).scaleb(-request.decimal_places), rounding=ROUND_HALF_UP)
    except (InvalidOperation, OverflowError) as error:
        raise CalculationError("Calculation exceeds supported precision.") from error
    return {"formula_id": request.formula, "formula_version": formula_version, "expression": expression,
            "input_verification": "not_verified_by_calculator",
            "inputs": [a.model_dump(mode="json"), b.model_dump(mode="json")],
            "value": str(rounded), "unit": unit,
            "currency": None if unit in {"percent", "percentage points"} else a.currency,
            "rounding": "half_up", "decimal_places": request.decimal_places}


def claim_calculation_differences(claims: list[dict], calculations: list[dict], facts: list[dict]) -> list[dict]:
    """Flag narrow numerical mismatches for review, without asserting entailment.

    Compare claims whose percentages denote the same number and have a uniquely
    matching denominator; inspect each stated precision separately.
    Recompute the saved calculation from current facts at both its own and the
    claim's stated precision; ignore user-edited, stale or ambiguous calculations.
    """
    by_denominator: dict[Decimal, list[tuple[int, CalculationRequest]]] = {}
    for index, item in enumerate(calculations):
        if (item.get("authorship") == "user" or item.get("formula_version") != "1" or
                item.get("formula_id") not in {"growth_percent", "margin_percent"} or
                item.get("unit") != "percent" or calculation_input_status(item, facts) != "matched"):
            continue
        try:
            inputs = item["inputs"]
            request = CalculationRequest.model_validate({"formula": item["formula_id"], "first": inputs[0],
                "second": inputs[1], "decimal_places": item.get("decimal_places", 2)})
            recomputed = calculate(request)
            if Decimal(str(item.get("value"))) != Decimal(recomputed["value"]):
                continue
        except (ValidationError, CalculationError, InvalidOperation, TypeError, ValueError, KeyError, IndexError):
            continue
        by_denominator.setdefault(request.second.value, []).append((index, request))
    differences: list[dict] = []
    for claim_index, claim in enumerate(claims):
        text = claim.get("text")
        if not isinstance(text, str) or len(text) > 3000:
            continue
        percentages = [match for match in _TEXT_PERCENT.finditer(text) if not _is_ratio_scale_factor(text, match)]
        if not percentages or len(percentages) > 20:
            continue
        raw_values = [item.group(1).replace(",", "").replace("−", "-") for item in percentages]
        if any(len(raw) > 40 or ("." in raw and len(raw.rsplit(".", 1)[1]) > 8) for raw in raw_values):
            continue
        try:
            stated_values = {Decimal(raw) for raw in raw_values}
            mentioned = {Decimal(m.group().replace(",", "").replace("−", "-")) for m in _TEXT_NUMBER.finditer(text)}
        except InvalidOperation:
            continue
        if len(stated_values) != 1:
            continue
        candidates = [entry for value in mentioned for entry in by_denominator.get(value, [])]
        if len(candidates) != 1:
            continue
        calculation_index, request = candidates[0]
        seen: set[tuple[str, str]] = set()
        for raw in raw_values:
            precision = len(raw.rsplit(".", 1)[1]) if "." in raw else 0
            try:
                expected = calculate(request.model_copy(update={"decimal_places": precision}))["value"]
                if Decimal(raw) == Decimal(expected) or (raw, expected) in seen:
                    continue
            except (CalculationError, InvalidOperation):
                continue
            seen.add((raw, expected))
            differences.append({"claim_index": claim_index, "calculation_index": calculation_index,
                                "claimed_value": raw, "calculated_value": expected,
                                "unit": "percent", "status": "requires_review"})
            if len(differences) == 50:
                return differences
    return differences
