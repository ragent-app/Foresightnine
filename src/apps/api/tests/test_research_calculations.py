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
    assert growth["period_order_check"] == "recognized_formats_only"
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
    assert claim_calculation_differences([{"text": "分母为642,637，利润率为4.21%。"}], [margin], [numerator, denominator])[0]["calculated_value"] == "4.20"
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


def test_same_percentage_at_two_precisions_checks_each_rounding():
    current = fact("current", "25785", "FY2024")
    prior = fact("prior", "22680", "FY2023")
    growth = calculate(CalculationRequest(formula="growth_percent", first=current, second=prior))
    claim = {"text": "Using 22680 as baseline, growth is about 13.7% (13.70%)."}
    result = claim_calculation_differences([claim], [growth], [current, prior])
    assert len(result) == 1 and result[0]["claimed_value"] == "13.70" and result[0]["calculated_value"] == "13.69"


def test_formula_scaling_factor_is_not_treated_as_a_claimed_percentage():
    current, prior = fact("current", "254453", "FY2024"), fact("prior", "242290", "FY2023")
    growth = calculate(CalculationRequest(formula="growth_percent", first=current, second=prior))
    for formula in ("(254,453 − 242,290) ÷ 242,290 × 100%", "(254453 / 242290) * 100%"):
        assert claim_calculation_differences([{"text": formula}], [growth], [current, prior]) == []
    claim = {"text": "(254453 / 242290) * 100% = 5.03%"}
    assert claim_calculation_differences([claim], [growth], [current, prior])[0]["claimed_value"] == "5.03"
    assert claim_calculation_differences([{"text": "On 242290 revenue growth was 100%."}], [growth], [current, prior])[0]["claimed_value"] == "100"


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


@pytest.mark.parametrize("current,baseline", [
    ("2023", "2024"), ("FY2023", "fy2024"), ("2023-Q4", "2024-Q1"),
    ("2024-01", "2024-02"), ("2024-12-30", "2024-12-31"), ("FY2024", "fy2024"),
    ("Year Ended January 30, 2022", "Year Ended January 29, 2023"),
    ("Year ended January 29 2023", "YEAR ENDED JANUARY 29, 2023"),
])
def test_growth_rejects_reversed_or_equivalent_recognized_periods(current, baseline):
    with pytest.raises(CalculationError, match="later than its baseline"):
        calculate(CalculationRequest(formula="growth_percent", first=fact("a", "100", current),
                                     second=fact("b", "80", baseline)))


def test_growth_does_not_guess_order_across_fiscal_and_calendar_or_free_text():
    for current, baseline in [("FY2023", "2024"), ("earlier estimate", "later estimate"),
                              ("2024-02-31", "2024-03-01"),
                              ("Year Ended February 30, 2024", "Year Ended March 1, 2024"),
                              ("Year Ended January 29, 2023", "2022-01-30")]:
        result = calculate(CalculationRequest(formula="growth_percent", first=fact("a", "100", current),
                                              second=fact("b", "80", baseline)))
        assert result["value"] == "25.00" and result["input_verification"] == "not_verified_by_calculator"
        assert result["period_order_check"] == "not_checked"


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


def test_suggested_calculation_requires_matching_result_and_both_source_passages():
    from app.research_calculations import suggest_percentage_calculations
    current = {**fact("current", "100", "2024"), "verification": "model_extraction_not_independently_verified",
               "source_id": "s", "quotation": "Current 100", "location": {"character_start": 0, "character_end": 11}}
    prior = {**fact("prior", "80", "2023"), "verification": "model_extraction_not_independently_verified",
             "source_id": "s", "quotation": "Prior 80", "location": {"character_start": 20, "character_end": 28}}
    calculation = calculate(CalculationRequest(formula="growth_percent", first=fact("current", "100", "2024"),
                                                second=fact("prior", "80", "2023")))
    claim = {"text": "Revenue growth was 25%", "uncited_percentages": ["25%"], "citations": [
        {"source_id": "s", "location": {"character_start": 0, "character_end": 11}},
        {"source_id": "s", "location": {"character_start": 20, "character_end": 28}}]}
    result = suggest_percentage_calculations([claim], [calculation], [current, prior])[0]
    assert result["uncited_percentages"] == ["25%"]
    assert result["suggested_calculations"] == [{"calculation_index": 0, "claimed_percentage": "25%",
        "formula_id": "growth_percent", "input_fact_ids": ["current", "prior"], "status": "requires_semantic_review"}]
    assert "suggested_calculations" not in claim
    assert not suggest_percentage_calculations([{**claim, "citations": claim["citations"][:1]}], [calculation], [current, prior])[0].get("suggested_calculations")
    assert not suggest_percentage_calculations([claim], [{**calculation, "value": "26"}], [current, prior])[0].get("suggested_calculations")
    assert not suggest_percentage_calculations([claim], [calculation], [current, {**prior, "value": "81"}])[0].get("suggested_calculations")
    assert not suggest_percentage_calculations([claim], [calculation], [current, {**prior, "authorship": "user"}])[0].get("suggested_calculations")
    assert not suggest_percentage_calculations([{**claim, "citations": [{**claim["citations"][0], "partial_token": True}, claim["citations"][1]]}], [calculation], [current, prior])[0].get("suggested_calculations")
    assert not suggest_percentage_calculations([{**claim, "uncited_percentages": ["26%"], "suggested_calculations": [{"fabricated": True}]}], [calculation], [current, prior])[0].get("suggested_calculations")


def test_year_ended_dates_check_order_without_changing_fiscal_identity():
    result = calculate(CalculationRequest(formula="growth_percent",
        first=fact("current", "26974", "Year Ended January 29, 2023"),
        second=fact("prior", "26914", "Year Ended January 30, 2022")))
    assert result["value"] == "0.22"
    assert result["period_order_check"] == "recognized_formats_only"
    assert result["input_verification"] == "not_verified_by_calculator"


def test_direction_context_blocks_treating_a_saved_calculation_as_verified():
    current, prior = fact("current", "3.2", "2023", unit="percent", currency=None), fact("prior", "-0.6", "2022", unit="percent", currency=None)
    calculation = calculate(CalculationRequest(formula="difference", first=current, second=prior))
    assert calculation_input_status(calculation, [current, prior]) == "matched"
    assert calculation_input_status(calculation, [current, {**prior, "direction_context_unverified": True}]) == "unverifiable"


@pytest.mark.parametrize("unit", ["percent (annual rate)", "percent, annualized", "percentage (quarterly rate)", "百分比"])
def test_qualified_percentage_difference_is_percentage_points(unit):
    current = fact("current", "3.2", "2023", unit=unit, currency=None)
    prior = fact("prior", "2.9", "2023", unit=unit, currency=None)
    result = calculate(CalculationRequest(formula="difference", first=current, second=prior))
    assert result["value"] == "0.30" and result["unit"] == "percentage points" and result["formula_version"] == "2"
    with pytest.raises(CalculationError, match="cannot have a currency"):
        calculate(CalculationRequest(formula="difference", first={**current, "currency": "USD"}, second={**prior, "currency": "USD"}))


def test_percentage_point_inputs_remain_points_without_becoming_percentage_levels():
    current = fact("current", "2", "2023", unit="percentage points", currency=None)
    prior = fact("prior", "1", "2022", unit="percentage points", currency=None)
    result = calculate(CalculationRequest(formula="difference", first=current, second=prior))
    assert result["unit"] == "percentage points" and result["formula_version"] == "1"
