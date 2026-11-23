"""Deterministic, provenance-bound calculations for explicitly hypothetical answers."""
from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .research_calculations import CalculationRequest, NumericFact, calculate


class ScenarioOperand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    label: str = Field(min_length=1, max_length=100)
    value: Decimal
    unit: str = Field(min_length=1, max_length=100)
    currency: str | None = Field(default=None, min_length=1, max_length=16)
    period: str = Field(min_length=1, max_length=100)
    kind: Literal['user', 'assumption']
    sequence: int | None = Field(default=None, ge=0)
    quotation: str = Field(min_length=1, max_length=1000)


class ScenarioCalculation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    formula: Literal['difference', 'sum', 'subtract', 'ratio', 'margin_percent', 'growth_percent']
    operands: list[ScenarioOperand] = Field(min_length=2, max_length=10)
    decimal_places: int = Field(default=2, ge=0, le=8, strict=True)


class ScenarioInputReceipt(BaseModel):
    model_config = ConfigDict(extra='forbid')
    label: str
    value: str
    unit: str
    currency: str | None
    period: str
    kind: Literal['user', 'assumption']
    sequence: int | None
    quotation: str
    content_sha256: str
    character_start: int = Field(ge=0)
    character_end: int = Field(gt=0)


class ScenarioCalculationReceipt(BaseModel):
    model_config = ConfigDict(extra='forbid')
    formula: Literal['difference', 'sum', 'subtract', 'ratio', 'margin_percent', 'growth_percent']
    operands: list[ScenarioInputReceipt] = Field(min_length=2, max_length=10)
    decimal_places: int = Field(ge=0, le=8)
    expression: str
    value: str
    unit: str
    currency: str | None
    display: str
    formula_version: str
    rounding: Literal['half_up']


def _contains_value(quote: str, value: Decimal) -> bool:
    # Values must appear literally, not as a substring of a different amount.
    return any(Decimal(m.group()) == value for m in re.finditer(r'(?<![\d.,+\-])-?\d+(?:\.\d+)?(?![\d.,])', quote))


def scenario_receipt(spec: ScenarioCalculation, text: str, messages: dict[int, str]) -> ScenarioCalculationReceipt:
    if not re.search(r'假设|假定|模拟|示例|例子|assum(?:e|ption)|hypothetical|illustrat(?:ion|ive)|example', text, re.I):
        raise ValueError('Scenario calculation must be explicitly hypothetical.')
    operands = []
    facts = []
    for index, item in enumerate(spec.operands):
        if item.kind == 'user':
            if item.sequence is None or item.sequence not in messages:
                raise ValueError('User operand must identify an existing user message.')
            original = messages[item.sequence]
        else:
            if item.sequence is not None:
                raise ValueError('Hypothetical operand cannot identify a user message.')
            original = text
            if not re.search(r'假设|假定|模拟|示例|例子|assum(?:e|ption)|hypothetical|illustrat(?:ion|ive)|example', item.quotation, re.I):
                raise ValueError('Each hypothetical operand quotation must explicitly label its assumption.')
        if item.quotation not in original:
            raise ValueError('Operand quotation must be an exact excerpt.')
        if not _contains_value(item.quotation, item.value) or any(part not in item.quotation for part in (item.unit, item.period, *([item.currency] if item.currency else []))):
            raise ValueError('Operand quotation must include its exact value, unit, period and currency.')
        start = original.index(item.quotation)
        operands.append(ScenarioInputReceipt(**item.model_dump(mode='json'), content_sha256=sha256(original.encode()).hexdigest(), character_start=start, character_end=start + len(item.quotation)))
        facts.append(NumericFact(fact_id=f'scenario-{index}', value=item.value, unit=item.unit, currency=item.currency, period=item.period))
    # Personal cash-flow inputs must refer to the same stated period.
    if spec.formula in ('difference', 'sum', 'subtract', 'ratio') and len({f.period for f in facts}) != 1:
        raise ValueError('Scenario inputs must have matching periods.')
    result = calculate(CalculationRequest(formula=spec.formula, facts=facts, decimal_places=spec.decimal_places))
    display = ' '.join(str(x) for x in (result['value'], result['unit'], result['currency']) if x)
    if not re.search(r'(?<![\d.,+\-])' + re.escape(display) + r'(?![\w.]|\s+[A-Za-z]{3}\b)', text):
        raise ValueError(f'Answer must include the calculated result exactly: {display}')
    return ScenarioCalculationReceipt(formula=spec.formula, operands=operands, decimal_places=spec.decimal_places,
        expression=result['expression'], value=result['value'], unit=result['unit'], currency=result['currency'],
        display=display, formula_version=result['formula_version'], rounding=result['rounding'])


def scenario_receipt_intact(receipt: ScenarioCalculationReceipt, text: str, messages: dict[int, str]) -> bool:
    try:
        spec = ScenarioCalculation(formula=receipt.formula, operands=[ScenarioOperand.model_validate(x.model_dump(include=set(ScenarioOperand.model_fields))) for x in receipt.operands], decimal_places=receipt.decimal_places)
        return scenario_receipt(spec, text, messages) == receipt
    except (ValueError, ArithmeticError):
        return False
