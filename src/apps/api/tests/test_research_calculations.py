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
    ("Fiscal year ended July 29, 2023", "Fiscal year ended July 27, 2024"),
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
    fiscal = calculate(CalculationRequest(formula="growth_percent",
        first=fact("current", "53803", "Fiscal year ended July 27, 2024"),
        second=fact("prior", "56998", "Fiscal year ended July 29, 2023")))
    assert fiscal["value"] == "-5.61" and fiscal["period_order_check"] == "recognized_formats_only"


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


def test_table_currency_cue_does_not_treat_bare_dollar_sign_as_usd():
    from app.research_calculations import table_currency_quote_missing
    assert table_currency_quote_missing("USD", "| Revenue | 100 | 90 |")
    assert table_currency_quote_missing("USD", "| Revenue | $100 | $90 |")
    assert not table_currency_quote_missing("USD", "| Revenue USD | 100 | 90 |")
    assert not table_currency_quote_missing("USD", "| Revenue US$ | 100 | 90 |")
    assert not table_currency_quote_missing("EUR", "| Revenue | €100 | €90 |")
    assert not table_currency_quote_missing(None, "| Revenue | 100 | 90 |")
    assert not table_currency_quote_missing("USD", "Revenue was 100 million.")


def test_ratio_difference_is_not_inferred_from_an_unrelated_yoy_percentage():
    total = fact('total', '317', '2024年11月')
    online = fact('online', '29', '2024年11月')
    ratio = calculate(CalculationRequest(formula='margin_percent', first=online, second=total))
    unrelated = {'text': '總銷貨價值為317億元，較上年同月下跌7.3%。'}
    assert claim_calculation_differences([unrelated], [ratio], [online, total]) == []
    intended = {'text': '網上銷售佔總銷貨317億元的9.3%。'}
    differences = claim_calculation_differences([intended], [ratio], [online, total])
    assert len(differences) == 1 and differences[0]['status'] == 'requires_review'
    both_inputs = {'text': '網上銷售29億元、總銷貨317億元，結果為9.3%。'}
    assert len(claim_calculation_differences([both_inputs], [ratio], [online, total])) == 1


def test_ratio_result_does_not_claim_precision_of_its_recorded_inputs():
    total = fact('total', '317', '2024-11')
    online = fact('online', '29', '2024-11')
    result = calculate(CalculationRequest(formula='margin_percent', first=online, second=total))
    assert result['value'] == '9.15'
    assert result['rounding'] == 'half_up' and result['decimal_places'] == 2
    assert result['input_precision_status'] == 'not_established'
    assert result['input_verification'] == 'not_verified_by_calculator'


def test_currency_conflict_requires_explicit_cue_on_same_value():
    from app.research_calculations import currency_value_conflict
    assert currency_value_conflict('USD', '100', 'Revenue was EUR 100 million.')
    assert currency_value_conflict('USD', '100', 'Revenue was €100 million.')
    assert currency_value_conflict('EUR', '100', 'Revenue was 100 US$ million.')
    assert not currency_value_conflict('EUR', '100', 'Revenue was EUR 100 million.')
    assert not currency_value_conflict('USD', '100', 'Revenue was $100 million.')
    assert not currency_value_conflict('USD', '100', 'Revenue was EUR 200 million and total was 100.')
    assert not currency_value_conflict('USD', '100', 'Revenue was EUR 100 and USD 100.')
    assert not currency_value_conflict(None, '100', 'Revenue was EUR 100.')


@pytest.mark.parametrize('current,baseline', [
    ('FY2024', '2023-Q4'), ('2024-Q1', '2023'),
    ('2024-02', '2023-Q4'), ('2024-Q1', '2023-12'),
    ('Year ended January 30, 2024', '2023-Q1'),
])
def test_growth_flags_different_period_grains_without_assuming_stock_or_flow(current, baseline):
    result = calculate(CalculationRequest(formula='growth_percent', first=fact('a', '100', current),
                                          second=fact('b', '80', baseline)))
    assert result['value'] == '25.00'
    assert result['period_granularity_check'] == 'different_labels_verify_stock_or_flow'
    assert result['period_order_check'] == 'not_checked'
    assert calculation_input_status(result, result['inputs']) == 'matched'


def test_growth_keeps_unknown_period_grain_and_does_not_guess_fiscal_calendar():
    for current, baseline in [('FY2024', '2023-12-31'), ('FY2024', 'prior quarter as described'),
                              ('FY2024', '2023'), ('Year ended June 30, 2024', '2023')]:
        result = calculate(CalculationRequest(formula='growth_percent', first=fact('a', '100', current),
                                              second=fact('b', '80', baseline)))
        assert result['value'] == '25.00' and result['period_order_check'] == 'not_checked'


def test_different_period_labels_do_not_become_claim_correction_or_suggestion():
    from app.research_calculations import suggest_percentage_calculations
    current = {**fact('current', '100', 'FY2024'), 'verification':'model_extraction_not_independently_verified',
               'source_id':'s', 'quotation':'Current 100', 'location':{'character_start':0,'character_end':11}}
    prior = {**fact('prior', '80', '2023-Q4'), 'verification':'model_extraction_not_independently_verified',
             'source_id':'s', 'quotation':'Prior 80', 'location':{'character_start':20,'character_end':28}}
    calculation = calculate(CalculationRequest(formula='growth_percent', first=fact('current','100','FY2024'),
                                                second=fact('prior','80','2023-Q4')))
    claim = {'text':'Using 80 as baseline, growth was 26%.', 'citations':[
        {'source_id':'s','location':current['location']}, {'source_id':'s','location':prior['location']}]}
    assert not claim_calculation_differences([claim], [calculation], [current, prior])
    claim['text'] = 'Growth was 25%.'
    assert not suggest_percentage_calculations([claim], [calculation], [current, prior])[0].get('suggested_calculations')


def test_table_period_conflict_only_for_narrow_unambiguous_rectangles():
    from decimal import Decimal
    from app.research_calculations import table_period_conflict
    text = '| Year Ended | 2024 | 2023 |\n| Revenue | USD 100 | USD 90 |\n'
    quote = 'USD 100'
    start = text.index(quote)
    check = lambda txt, pos, q, value, period: table_period_conflict(txt, pos, q, Decimal(value), period)
    assert check(text, start, quote, '100', 'FY2023') == {'declared_year': '2023', 'column_year': '2024'}
    assert check(text, start, quote, '100', 'FY2024') is None
    assert check(text, start, quote, '100', '2023 Q1') is None
    assert check(text, start + 1, quote, '100', '2023') is None
    duplicate = text.replace('USD 90', 'USD 100')
    assert check(duplicate, duplicate.index(quote), quote, '100', '2023') is None
    for changed in (text.replace('2023 |', '2024 |', 1),
                    text.replace('Year Ended', 'Revenue'),
                    text.replace('USD 100', '[colspan=2] USD 100'),
                    text.replace('| Revenue |', 'Narrative\n| Revenue |'),
                    text.replace('| Revenue | USD 100 | USD 90 |', '| Revenue | USD 100 | USD 90 | 80 |')):
        assert check(changed, changed.index(quote), quote, '100', '2023') is None


def test_spanned_table_period_conflict_needs_complete_unique_consistent_columns():
    from decimal import Decimal
    from app.research_calculations import table_period_conflict
    title = '| [colspan=3] | [colspan=9] Year Ended |\n'
    dates = '| [colspan=3] | [colspan=3] January 29, 2023 | [colspan=3] | [colspan=3] January 30, 2022 |\n'
    unit = '| [colspan=3] | [colspan=9] (In millions) |\n'
    row = '| [colspan=3] Operating cash | $ | 5,641 | | [colspan=3] | $ | 9,108 | |\n'
    text = title + dates + unit + row
    quote = 'Operating cash | $ | 5,641'
    def check(t, q=quote, value='5641', period='Year Ended January 30, 2022'):
        return table_period_conflict(t, t.index(q), q, Decimal(value), period)
    assert check(text) == {'declared_year': '2022', 'column_year': '2023'}
    assert check(text, period='Year Ended January 29, 2023') is None
    assert check(text, q='9,108', value='9108', period='FY2023') == {'declared_year': '2023', 'column_year': '2022'}
    assert check(text, q='Operating cash', value='5641') is None
    assert check(text.replace('9,108','5,641')) is None
    for changed in [text.replace('[colspan=9] Year Ended', '[rowspan=2] Year Ended'),
                    text.replace('[colspan=3] January 29, 2023', '[colspan=2] January 29, 2023'),
                    text.replace('January 29, 2023', 'February 30, 2023'),
                    text.replace(unit, 'Intervening narrative\n'),
                    text.replace('Year Ended', 'Year Beginning', 1)]:
        assert check(changed) is None


def test_decrease_wording_matches_negative_growth_only_with_local_unnegated_cue():
    current, prior = fact('current','5641','2023'), fact('prior','9108','2022')
    calculation = calculate(CalculationRequest(formula='growth_percent', first=current, second=prior))
    def compare(text):
        return claim_calculation_differences([{'text': text}], [calculation], [current, prior])
    for text in ('经营现金流从9108降至5641，同比减少约38.1%。',
                 'On 9108, cash flow declined by approximately 38.1%.'):
        assert compare(text) == []
    for text in ('经营现金流从9108降至5641，同比增加约38.1%。',
                 '经营现金流从9108降至5641，同比没有减少约38.1%。',
                 'On 9108, cash flow did not decrease by 38.1%.',
                 '经营现金流从9108降至5641，同比减少38.2%。',
                 'On 9108, cash flow declined by +38.1%.'):
        assert compare(text), text


def test_decrease_calculation_suggestion_still_requires_evidence_and_unique_wording():
    from app.research_calculations import suggest_percentage_calculations
    current = {**fact('current','5641','2023'), 'verification':'model_extraction_not_independently_verified',
               'source_id':'s','quotation':'current 5641','location':{'character_start':0,'character_end':12}}
    prior = {**fact('prior','9108','2022'), 'verification':'model_extraction_not_independently_verified',
             'source_id':'s','quotation':'prior 9108','location':{'character_start':20,'character_end':30}}
    calculation = calculate(CalculationRequest(formula='growth_percent', first=fact('current','5641','2023'), second=fact('prior','9108','2022')))
    claim = {'text':'现金流同比减少约38.1%。','uncited_percentages':['38.1%'], 'citations':[
        {'source_id':'s','location':{'character_start':0,'character_end':12}},
        {'source_id':'s','location':{'character_start':20,'character_end':30}}]}
    result = suggest_percentage_calculations([claim],[calculation],[current,prior])[0]
    assert result['suggested_calculations'][0]['status'] == 'requires_semantic_review'
    assert result['uncited_percentages'] == ['38.1%']
    assert not suggest_percentage_calculations([{**claim,'text':'现金流并未减少约38.1%。'}],[calculation],[current,prior])[0].get('suggested_calculations')
    assert not suggest_percentage_calculations([{**claim,'text':'现金流同比减少约38.1%，另有38.1%。'}],[calculation],[current,prior])[0].get('suggested_calculations')
    assert not suggest_percentage_calculations([{**claim,'citations':claim['citations'][:1]}],[calculation],[current,prior])[0].get('suggested_calculations')
