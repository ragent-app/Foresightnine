"""Auditable deterministic financial calculations for research tools."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from datetime import date
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


def is_percent_unit(unit: str) -> bool:
    """Recognize percentage levels, excluding percentage-point differences."""
    normalized = " ".join(unit.strip().casefold().split())
    if re.search(r"\bpoints?\b|百分点", normalized):
        return False
    return bool(re.fullmatch(r"(?:percent|percentage|%|百分比)(?:\s*\([^()]{1,80}\)|,\s*(?:annualized|annual rate))?", normalized))


def table_currency_quote_missing(currency: str | None, quotation: str | None) -> bool:
    """Flag a table-row quotation without an explicit currency identifier.

    A code or symbol is only a cue, not verification of the financial fact.
    A bare dollar sign is ambiguous across several currencies.
    """
    if not currency or not isinstance(quotation, str) or not quotation.lstrip().startswith("|"):
        return False
    code = currency.strip().upper()
    if re.search(r"(?<![A-Za-z])" + re.escape(code) + r"(?![A-Za-z])", quotation, re.IGNORECASE):
        return False
    patterns = {"USD": r"US\s*\$|U\.?S\.?\s+dollars?", "EUR": r"€|\beuros?\b",
                "GBP": r"£|\bpounds?\s+sterling\b", "JPY": r"\bJapanese\s+yen\b",
                "KRW": r"₩|\bKorean\s+won\b"}
    return not bool(re.search(patterns.get(code, r"(?!)"), quotation, re.IGNORECASE))


def _comparable_period(period: str) -> tuple[str, tuple[int, ...]] | None:
    """Parse only unambiguous matching period formats, without guessing fiscal calendars."""
    value = period.strip()
    if match := re.fullmatch(r"(FY)?((?:19|20)\d{2})", value, re.IGNORECASE):
        return ("fiscal_year" if match.group(1) else "calendar_year", (int(match.group(2)),))
    if match := re.fullmatch(r"((?:19|20)\d{2})[- ]?Q([1-4])", value, re.IGNORECASE):
        return ("calendar_quarter", (int(match.group(1)), int(match.group(2))))
    if match := re.fullmatch(r"((?:19|20)\d{2})-(0[1-9]|1[0-2])", value):
        return ("calendar_month", (int(match.group(1)), int(match.group(2))))
    # Financial filings commonly spell out the end date of each annual period.
    # Keep this distinct from bare dates and FY labels: matching dates alone do
    # not establish that two companies use comparable fiscal calendars.
    match = re.fullmatch(r"(?:fiscal\s+)?year\s+ended\s+([A-Za-z]+)\s+(\d{1,2}),?\s+((?:19|20)\d{2})", value, re.IGNORECASE)
    if match:
        months = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
        try:
            day = date(int(match.group(3)), months.index(match.group(1).casefold()) + 1, int(match.group(2)))
        except ValueError:
            return None
        return ("year_ended_date", (day.year, day.month, day.day))
    if re.fullmatch(r"(?:19|20)\d{2}-\d{2}-\d{2}", value):
        try:
            day = date.fromisoformat(value)
        except ValueError:
            return None
        return ("date", (day.year, day.month, day.day))
    return None


_TEXT_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_TEXT_PERCENT = re.compile(r"(?<![A-Za-z0-9_.])([-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(?:%|％|percent\b)", re.IGNORECASE)
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
            if candidates[0].get("direction_context_unverified"):
                return "unverifiable"
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
    period_order_check = "not_checked"
    if a.fact_id == b.fact_id:
        raise CalculationError("Inputs must identify distinct facts.")
    if a.unit != b.unit or a.currency != b.currency:
        raise CalculationError("Input units and currencies must match.")
    if request.formula == "margin_percent" and a.period != b.period:
        raise CalculationError("Margin inputs must have the same period.")
    if request.formula == "growth_percent" and a.period == b.period:
        raise CalculationError("Growth inputs must refer to different periods.")
    if request.formula == "growth_percent":
        current, baseline = _comparable_period(a.period), _comparable_period(b.period)
        if current is not None and baseline is not None and current[0] == baseline[0]:
            if current[1] <= baseline[1]:
                raise CalculationError("Growth current period must be later than its baseline.")
            period_order_check = "recognized_formats_only"
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
                if is_percent_unit(a.unit):
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
            **({"period_order_check": period_order_check} if request.formula == "growth_percent" else {}),
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


def suggest_percentage_calculations(claims: list[dict], calculations: list[dict], facts: list[dict]) -> list[dict]:
    """Suggest source-covered calculations; matching is not semantic verification.

    Only reproducible machine calculations with uniquely identified, unedited
    facts whose original passages are covered by the claim's citations qualify.
    Percentages remain uncited and still require review.
    """
    from copy import deepcopy

    facts_by_id: dict[str, list[dict]] = {}
    for fact in facts:
        if isinstance(fact.get("fact_id"), str):
            facts_by_id.setdefault(fact["fact_id"], []).append(fact)
    valid: list[tuple[int, CalculationRequest, list[dict]]] = []
    for index, item in enumerate(calculations):
        if (item.get("authorship") == "user" or item.get("formula_version") != "1" or
                item.get("formula_id") not in {"growth_percent", "margin_percent"} or
                item.get("unit") != "percent" or calculation_input_status(item, facts) != "matched"):
            continue
        try:
            inputs = item["inputs"]
            request = CalculationRequest.model_validate({"formula": item["formula_id"], "first": inputs[0],
                "second": inputs[1], "decimal_places": item.get("decimal_places", 2)})
            if Decimal(str(item.get("value"))) != Decimal(calculate(request)["value"]):
                continue
            source_facts = [facts_by_id[fact.fact_id][0] for fact in (request.first, request.second)]
        except (ValidationError, CalculationError, InvalidOperation, TypeError, ValueError, KeyError, IndexError):
            continue
        if any(fact.get("verification") != "model_extraction_not_independently_verified" or
               fact.get("authorship") == "user" or not isinstance(fact.get("source_id"), str) or
               not isinstance(fact.get("quotation"), str) or not fact.get("quotation") or
               not isinstance(fact.get("location"), dict) or fact["location"].get("status") == "ambiguous" or
               type(fact["location"].get("character_start")) is not int or
               type(fact["location"].get("character_end")) is not int or
               fact["location"]["character_end"] - fact["location"]["character_start"] != len(fact["quotation"])
               for fact in source_facts):
            continue
        valid.append((index, request, source_facts))
    # Index rounded values once so a large saved version does not repeatedly
    # recompute every calculation for every claim.
    by_result: dict[tuple[int, Decimal], list[tuple[int, CalculationRequest, list[dict]]]] = {}
    for index, request, source_facts in valid:
        for precision in range(9):
            try:
                value = Decimal(calculate(request.model_copy(update={"decimal_places": precision}))["value"])
            except (CalculationError, InvalidOperation):
                continue
            by_result.setdefault((precision, value), []).append((index, request, source_facts))
    output = []
    for claim in claims:
        entry = deepcopy(claim)
        entry.pop("suggested_calculations", None)
        citations = entry.get("citations")
        percentages = entry.get("uncited_percentages")
        if not isinstance(citations, list) or not isinstance(percentages, list):
            output.append(entry)
            continue
        candidates = []
        for raw in percentages[:5]:
            match = _TEXT_PERCENT.fullmatch(raw) if isinstance(raw, str) else None
            if match is None:
                continue
            value = match.group(1).replace(",", "").replace("−", "-")
            precision = len(value.rsplit(".", 1)[1]) if "." in value else 0
            if len(value) > 40 or precision > 8:
                continue
            try:
                matching = by_result.get((precision, Decimal(value)), [])
            except InvalidOperation:
                continue
            for index, request, source_facts in matching:
                if not all(any(isinstance(citation, dict) and not citation.get("partial_token") and
                    citation.get("source_id") == fact["source_id"] and isinstance(citation.get("location"), dict) and
                    citation["location"].get("status") != "ambiguous" and
                    type(citation["location"].get("character_start")) is int and
                    type(citation["location"].get("character_end")) is int and
                    citation["location"]["character_start"] <= fact["location"]["character_start"] and
                    citation["location"]["character_end"] >= fact["location"]["character_end"]
                    for citation in citations) for fact in source_facts):
                    continue
                candidates.append({"calculation_index": index, "claimed_percentage": raw,
                    "formula_id": request.formula, "input_fact_ids": [request.first.fact_id, request.second.fact_id],
                    "status": "requires_semantic_review"})
                if len(candidates) >= 5:
                    break
            if len(candidates) >= 5:
                break
        if candidates:
            entry["suggested_calculations"] = candidates
        output.append(entry)
    return output
