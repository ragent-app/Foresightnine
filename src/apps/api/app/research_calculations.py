"""Auditable deterministic financial calculations for research tools."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
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
                unit = a.unit
            rounded = result.quantize(Decimal(1).scaleb(-request.decimal_places), rounding=ROUND_HALF_UP)
    except (InvalidOperation, OverflowError) as error:
        raise CalculationError("Calculation exceeds supported precision.") from error
    return {"formula_id": request.formula, "formula_version": "1", "expression": expression,
            "input_verification": "not_verified_by_calculator",
            "inputs": [a.model_dump(mode="json"), b.model_dump(mode="json")],
            "value": str(rounded), "unit": unit,
            "currency": a.currency if unit != "percent" else None,
            "rounding": "half_up", "decimal_places": request.decimal_places}
