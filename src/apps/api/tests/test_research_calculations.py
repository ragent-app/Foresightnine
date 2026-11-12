from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.research_calculations import CalculationError, CalculationRequest, calculate, calculation_input_status, claim_calculation_differences


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


def test_difference_of_percentages_is_percentage_points_not_percent_change():
    current = fact("current", "2.8", "2024", unit="percent", currency=None)
    prior = fact("prior", "2.9", "2023", unit="percent", currency=None)
    result = calculate(CalculationRequest(formula="difference", first=current, second=prior))
    assert result["value"] == "-0.10" and result["unit"] == "percentage points"
    assert result["currency"] is None and result["formula_version"] == "2"
    money = calculate(CalculationRequest(formula="difference", first=fact("a", "100", "2024"), second=fact("b", "80", "2023")))
    assert money["value"] == "20.00" and money["unit"] == "USD millions" and money["formula_version"] == "1"
    with pytest.raises(CalculationError):
        calculate(CalculationRequest(formula="difference", first={**current, "currency": "USD"}, second={**prior, "currency": "USD"}))


def test_flags_anchored_claim_percentage_but_handles_rounding_and_stale_results():
    numerator = fact("income", "27012", "FY2024")
    denominator = fact("sales", "642637", "FY2024")
    margin = calculate(CalculationRequest(formula="margin_percent", first=numerator, second=denominator))
    claims = [{"text": "Using net sales of 642,637, the margin is about 4.21%."}]
    differences = claim_calculation_differences(claims, [margin], [numerator, denominator])
    assert differences == [{"claim_index": 0, "calculation_index": 0, "claimed_value": "4.21",
                            "calculated_value": "4.20", "unit": "percent", "status": "requires_review"}]
    assert claim_calculation_differences([{"text": "Using 642,637, the margin is 4.2%."}], [margin], [numerator, denominator]) == []
    assert claim_calculation_differences([{"text": "The margin is 4.21%."}], [margin], [numerator, denominator]) == []
    assert claim_calculation_differences(claims, [margin], [numerator, {**denominator, "value": "642638"}]) == []
    assert claim_calculation_differences(claims, [{**margin, "value": "4.21"}], [numerator, denominator]) == []
    assert claim_calculation_differences(claims, [{**margin, "authorship": "user"}], [numerator, denominator]) == []


def test_avoids_guessing_when_a_denominator_or_percentage_is_ambiguous():
    revenue = fact("revenue", "100", "FY2024")
    profit = fact("profit", "10", "FY2024")
    earnings = fact("earnings", "20", "FY2024")
    calculations = [calculate(CalculationRequest(formula="margin_percent", first=item, second=revenue))
                    for item in (profit, earnings)]
    assert claim_calculation_differences([{"text": "On 100 revenue the margin is 11%."}], calculations,
                                         [revenue, profit, earnings]) == []
    assert claim_calculation_differences([{"text": "On 100 revenue margins are 11% and 20%."}], calculations[:1],
                                         [revenue, profit, earnings]) == []


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
