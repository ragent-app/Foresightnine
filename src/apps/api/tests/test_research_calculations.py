from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.research_calculations import CalculationError, CalculationRequest, calculate, calculation_input_status


def fact(fact_id, value, period, *, unit="USD millions", currency="USD"):
    return {"fact_id": fact_id, "value": value, "period": period,
            "unit": unit, "currency": currency}


def test_growth_and_margin_keep_reproducible_inputs_and_formula():
    growth = calculate(CalculationRequest(formula="growth_percent",
        first=fact("current", "211915", "FY2023"), second=fact("prior", "198270", "FY2022")))
    assert growth["value"] == "6.88"
    assert growth["expression"] == "(first / second - 1) * 100"
    assert [item["fact_id"] for item in growth["inputs"]] == ["current", "prior"]
    assert growth["input_verification"] == "not_verified_by_calculator"
    margin = calculate(CalculationRequest(formula="margin_percent",
        first=fact("income", "88523", "FY2023"), second=fact("revenue", "211915", "FY2023")))
    assert margin["value"] == "41.77"


@pytest.mark.parametrize("first,second,formula", [
    (fact("a", "1", "2023"), fact("b", "0", "2022"), "growth_percent"),
    (fact("a", "1", "2023"), fact("b", "-1", "2022"), "growth_percent"),
    (fact("a", "1", "2023"), fact("b", "2", "2022", currency="EUR"), "growth_percent"),
    (fact("a", "1", "2023"), fact("b", "2", "2022"), "margin_percent"),
    (fact("a", "1", "2023"), fact("a", "2", "2022"), "growth_percent"),
])
def test_invalid_inputs_do_not_produce_numbers(first, second, formula):
    with pytest.raises(CalculationError):
        calculate(CalculationRequest(formula=formula, first=first, second=second))


def test_nonfinite_values_rejected():
    with pytest.raises(ValidationError):
        CalculationRequest(formula="difference", first=fact("a", Decimal("NaN"), "2023"),
                           second=fact("b", "2", "2022"))


def test_retained_calculation_detects_changed_missing_and_ambiguous_facts():
    current, prior = fact("current", "100", "2023"), fact("prior", "80", "2022")
    calculation = calculate(CalculationRequest(formula="growth_percent", first=current, second=prior))
    assert calculation_input_status(calculation, [current, prior]) == "matched"
    assert calculation_input_status(calculation, [dict(current, value="100.0"), prior]) == "matched"
    assert calculation_input_status(calculation, [dict(current, value="101"), prior]) == "changed"
    assert calculation_input_status(calculation, [dict(current, currency="EUR"), prior]) == "changed"
    assert calculation_input_status(calculation, [current]) == "missing_or_ambiguous"
    assert calculation_input_status(calculation, [current, current, prior]) == "missing_or_ambiguous"
    assert calculation_input_status({"inputs": [current]}, [current, prior]) == "unverifiable"
