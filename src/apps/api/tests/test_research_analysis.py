from datetime import datetime, timezone
import json

import pytest

from app.research_analysis import AnalysisInterrupted, InvalidAnalysis, analyze, _compact_pdf_with_offsets, _comparison_windows, _financial_table_windows, _policy_vote_windows, _select_window, _select_windows, _trim_partial_ascii_words, _quoted_numbers
from decimal import Decimal
from app.research_sources import ResearchSource, SourcePermissions, locate_user_quotation


def source(source_id, *, text="Revenue was 100 million.", url=None, model_input=True,
           display=True, published_at=None):
    return ResearchSource(source_id=source_id, owner_id="owner", title="Report", text=text,
        url=url, kind="user_supplied", obtained_at=datetime.now(timezone.utc),
        published_at=published_at, content_sha256="test",
        permissions=SourcePermissions(store=True, model_input=model_input, display=display))


def test_selected_source_offsets_survive_generation_failure():
    report = source("s", text="Revenue was 100 million.")
    prepared = []

    def fail(prompt):
        assert "range" not in json.loads(prompt)["sources"][0]
        raise RuntimeError("provider failed")

    with pytest.raises(RuntimeError, match="provider failed"):
        analyze("Revenue?", [report], None, fail, on_prepare=prepared.extend)
    assert len(prepared) == 1
    segment = prepared[0]
    assert segment["source_id"] == "s"
    assert report.text[segment["character_start"]:segment["character_end"]] == report.text


def test_fractional_rate_notation_is_converted_exactly():
    values = _quoted_numbers("lower by 1/2 percentage point to 4-3/4 to 5 percent")
    assert Decimal("0.5") in values and Decimal("4.75") in values and Decimal("5") in values
    assert Decimal("3") not in values and Decimal("4") not in values
    assert _quoted_numbers("-1/2") == {Decimal("-0.5")}
    assert _quoted_numbers("- 1/2") == {Decimal("-0.5")}
    assert _quoted_numbers("区间为5-1/4%至5-1/2%。") == {Decimal("5.25"), Decimal("5.5")}
    assert _quoted_numbers("目標為5-1/4至5-1/2百分點") == {Decimal("5.25"), Decimal("5.5")}
    assert Decimal("0.3333333333333333333333333333") not in _quoted_numbers("1/3")
    assert Decimal("2.6") in _quoted_numbers("同比2.6%，环比0.3%")


def test_partial_number_cannot_be_promoted_to_a_source_fact_or_calculation():
    report = source("s", text="The target rate is 4-1/4 percent. Revenue is 1000 dollars. Loss was (85).")
    def item(id, value, quote):
        return {"fact_id": id, "label": "Metric", "value": value, "unit": "number", "period": "2024",
                "citation": {"source_id": "s", "quotation": quote}}
    raw = {"claims": [{"text": "Target rate is one quarter.", "citations": [{"source_id": "s", "quotation": "1/4"}]}],
           "facts": [item("rate", "0.25", "1/4"), item("negative_rate", "-0.25", "-1/4"),
                     item("revenue", "100", "100"), item("loss", "85", "85")],
           "calculations": [{"formula": "difference", "first_fact_id": "rate", "second_fact_id": "revenue"}]}
    result = analyze("What happened?", [report], None, lambda _: (raw, 10))
    assert not result.facts and not result.calculations
    assert result.claims[0]["support_status"] == "incomplete"
    assert result.claims[0]["citations"][0]["partial_token"] is True
    assert any("cuts through a word or number" in item for item in result.limitations)
    assert locate_user_quotation(report, "85)")["partial_token"]
    assert locate_user_quotation(report, "(85")["partial_token"]
    assert "partial_token" not in locate_user_quotation(report, "(85)")


def test_partial_word_is_marked_but_a_complete_word_and_chinese_text_are_not():
    report = source("s", text="Additional adjustments remain possible. 同比增长百分之二。")
    def run(quote):
        return analyze("Question?", [report], None, lambda _: ({"claims": [{"text": "Claim", "citations": [
            {"source_id": "s", "quotation": quote}]}]}, 1))
    assert run("Additional adjustm").claims[0]["support_status"] == "incomplete"
    assert run("Additional adjustments").claims[0]["support_status"] == "not_independently_verified"
    assert run("同比增长").claims[0]["support_status"] == "not_independently_verified"
    assert locate_user_quotation(report, "Additional adjustm")["partial_token"] is True
    assert "partial_token" not in locate_user_quotation(report, "Additional adjustments")


def test_ambiguous_quotation_is_retained_for_review_but_not_used_for_calculation():
    report = source("s", text="Revenue 100. Revenue 100. Revenue 80.")
    def item(id, value, period):
        return {"fact_id": id, "label": "Revenue", "value": value, "unit": "USD", "currency": "USD", "period": period,
                "citation": {"source_id": "s", "quotation": "Revenue " + value}}
    raw = {"facts": [item("current", "100", "2024"), item("prior", "80", "2023")],
           "calculations": [{"formula": "growth_percent", "first_fact_id": "current", "second_fact_id": "prior"}]}
    result = analyze("Revenue?", [report], None, lambda _: (raw, 1))
    assert len(result.facts) == 2 and result.facts[0]["location"]["status"] == "ambiguous"
    assert not result.calculations
    assert any("ambiguous quotation location" in item and "excluded" in item for item in result.limitations)


def test_invalid_numeric_fact_is_omitted_without_discarding_grounded_claims():
    report = source("s", text="The target was lowered by 1/2 percentage point.")
    citation = {"source_id": "s", "quotation": "lowered by 1/2 percentage point"}
    raw = {"claims": [{"text": "The target was lowered", "citations": [citation]}],
           "facts": [{"fact_id": "bad", "label": "cut", "value": "one half",
                      "unit": "percentage point", "period": "2024", "citation": citation}],
           "calculations": [{"formula": "difference", "first_fact_id": "bad", "second_fact_id": "missing"}]}
    result = analyze("target?", [report], None, lambda _: (raw, 10))
    assert len(result.claims) == 1 and not result.facts and not result.calculations
    assert any("1 model-generated numeric fact(s) were omitted" in item for item in result.limitations)
    assert any("calculation was omitted" in item for item in result.limitations)


def test_fraction_fact_with_matching_unit_is_normalized_and_checked_against_source():
    report = source("s", text="The target was lowered by 1/2 percentage point.")
    citation = {"source_id": "s", "quotation": "lowered by 1/2 percentage point"}
    raw = {"facts": [{"fact_id": "cut", "label": "cut", "value": "1/2 percentage point",
                      "unit": "percentage point", "period": "2024", "citation": citation}]}
    result = analyze("target?", [report], None, lambda _: (raw, 10))
    assert result.facts[0]["value"] == "0.5"
    assert result.facts[0]["numeric_normalization"] == "exact_fraction"


def test_explicit_rate_range_is_split_into_source_checked_bounds():
    report = source("s", text="The target range was 4-1/4 to 4-1/2 percent.")
    citation = {"source_id": "s", "quotation": "4-1/4 to 4-1/2 percent"}
    raw = {"facts": [{"fact_id": "range", "label": "Target range", "value": "4-1/4 to 4-1/2 percent",
                       "unit": "percent", "currency": None, "period": "2024", "citation": citation}]}
    result = analyze("Target?", [report], None, lambda _: (raw, 10))
    assert [(fact["fact_id"], fact["value"]) for fact in result.facts] == [("range:lower", "4.25"), ("range:upper", "4.5")]
    assert all(fact["numeric_normalization"] == "exact_range_bound" and fact["range_parent_id"] == "range" for fact in result.facts)
    reversed_range = {"facts": [{**raw["facts"][0], "value": "4-1/2 to 4-1/4 percent"}]}
    assert analyze("Target?", [report], None, lambda _: (reversed_range, 10)).facts == []
    bad_quote = {"facts": [{**raw["facts"][0], "citation": {"source_id": "s", "quotation": "4-1/4"}}]}
    assert [fact["value"] for fact in analyze("Target?", [report], None, lambda _: (bad_quote, 10)).facts] == ["4.25"]


def test_rate_range_currency_context_is_retained_but_cannot_feed_calculations():
    report = source("s", text="The target range was 4-1/4 to 4-1/2 percent.")
    citation = {"source_id": "s", "quotation": "4-1/4 to 4-1/2 percent"}
    raw = {"facts": [{"fact_id": "range", "label": "Target range", "value": "4-1/4 to 4-1/2",
                      "unit": "percent", "currency": "USD", "period": "2025", "citation": citation}],
           "calculations": [{"formula": "difference", "first_fact_id": "range:upper", "second_fact_id": "range:lower"}]}
    result = analyze("Target?", [report], None, lambda _: (raw, 10))
    assert [(item["value"], item["currency"]) for item in result.facts] == [("4.25", "USD"), ("4.5", "USD")]
    assert all(item["currency_context_unverified"] for item in result.facts)
    assert not result.calculations
    assert any("currency; it was excluded" in item for item in result.limitations)
    assert any("input facts were unavailable" in item for item in result.limitations)
    raw["facts"][0]["citation"]["quotation"] = "4-1/4"
    assert [item["value"] for item in analyze("Target?", [report], None, lambda _: (raw, 10)).facts] == ["4.25"]


def test_equivalent_range_and_scalar_forms_are_checked_against_original():
    report = source("s", text="The target was lowered by 1/4 percentage point to 3-1/2 to 3-3/4 percent.")
    range_fact = {"fact_id": "range", "label": "Target range", "value": "3.5% 至 3.75%（3-1/2 to 3-3/4 percent）",
                  "unit": "percent", "currency": None, "period": "2025", "citation": {"source_id": "s", "quotation": "3-1/2 to 3-3/4 percent"}}
    cut_fact = {"fact_id": "cut", "label": "Cut", "value": "0.25（1/4）", "unit": "percentage point",
                "currency": None, "period": "2025", "citation": {"source_id": "s", "quotation": "by 1/4 percentage point"}}
    def run(facts):
        return analyze("Target?", [report], None, lambda _: ({"facts": facts}, 1))
    result = run([range_fact, cut_fact])
    assert [(f["fact_id"], f["value"]) for f in result.facts] == [("range:lower", "3.5"), ("range:upper", "3.75"), ("cut", "0.25")]
    assert result.facts[-1]["numeric_normalization"] == "equivalent_decimal_fraction"
    assert all(report.text[f["location"]["character_start"]:f["location"]["character_end"]] == f["quotation"] for f in result.facts)
    assert not run([{**range_fact, "value": "3.5% 至 3.75%（3-1/2 to 3-1/2 percent）"}]).facts
    assert not run([{**cut_fact, "value": "0.5（1/4）"}]).facts
    assert not run([{**cut_fact, "value": "0.25（1/3）"}]).facts
    assert not run([{**cut_fact, "value": "0.25（1/4）", "unit": "USD"}]).facts
    assert not run([{**cut_fact, "citation": {"source_id": "s", "quotation": "3-3/4 percent"}}]).facts
    assert not run([{**cut_fact, "value": "9" * 5000 + "/2"}]).facts


def test_only_authorized_sources_are_sent_and_quotes_must_be_real():
    good = source("good")
    private = source("no-model", model_input=False)
    fred = source("fred", url="https://fred.stlouisfed.org/series/GDP")
    def model(prompt):
        assert "no-model" not in prompt
        assert "fred" not in prompt
        return {"claims": [{"text": "Revenue was reported", "citations": [
            {"source_id": "good", "quotation": "100 million"}]}],
            "counterarguments": [], "limitations": []}, 42
    result = analyze("What happened?", [good, private, fred], None, model)
    assert result.source_ids == ["good"]
    assert result.claims[0]["citations"][0]["quotation"] == "100 million"
    assert result.claims[0]["support_status"] == "not_independently_verified"
    assert len(result.limitations) == 3
    assert result.total_tokens == 42


def test_research_prompt_requests_counterevidence_and_preserves_gaps():
    def model(prompt):
        payload = json.loads(prompt)
        example = payload["output_schema"]["counterarguments"][0]
        assert example["citations"][0]["quotation"] == "quote"
        assert "missing evidence in limitations" in payload["evidence_guidance"]
        assert "one continuous source passage" in payload["evidence_guidance"]
        assert "never join passages or quote metadata" in payload["evidence_guidance"]
        assert "Cite table headers, units and rows separately" in payload["evidence_guidance"]
        return {"claims": [], "counterarguments": [], "limitations": ["No opposing evidence supplied."]}, 10

    result = analyze("What would contradict this?", [source("s")], None, model)
    assert result.counterarguments == []
    assert "Model-reported limitation (not independently verified): No opposing evidence supplied." in result.limitations


def test_prompt_distinguishes_full_source_from_partial_excerpts():
    def inspect(expected):
        def generate(prompt):
            payload = json.loads(prompt)
            assert all(entry["source_coverage"] == expected for entry in payload["sources"])
            assert "not the whole source" in payload["evidence_guidance"]
            return {"claims": []}, 10
        return generate
    analyze("Revenue?", [source("short")], None, inspect("complete"))
    analyze("Revenue?", [source("long", text="Revenue. " * 1000)], None, inspect("partial"), max_prompt_chars=2500)


def test_prompt_exposes_declared_source_times_separately_from_reporting_period():
    published = datetime(2023, 10, 27, 12, 30, tzinfo=timezone.utc)
    revised = datetime(2023, 11, 1, tzinfo=timezone.utc)
    event = datetime(2023, 9, 30, tzinfo=timezone.utc)
    report = source("dated", published_at=published).model_copy(update={"revised_at": revised, "event_at": event})

    def model(prompt):
        payload = json.loads(prompt)
        item = payload["sources"][0]
        assert item["declared_published_at"] == published.isoformat()
        assert item["declared_revised_at"] == revised.isoformat()
        assert item["declared_event_at"] == event.isoformat()
        assert "Dates are declared, not verified" in payload["evidence_guidance"]
        return {}, 1

    analyze("What happened?", [report], None, model)
    def undated(prompt):
        item = json.loads(prompt)["sources"][0]
        assert not any(key.startswith("declared_") for key in item)
        return {}, 1
    analyze("What happened?", [source("undated")], None, undated)


def test_rejects_fabricated_citations_and_respects_display_permission():
    hidden = source("hidden", display=False)
    visible = source("hidden", display=True)
    def model(quote):
        return lambda _: ({"claims": [{"text": "Claim", "citations": [
            {"source_id": "hidden", "quotation": quote}]}],
            "counterarguments": [], "limitations": []}, None)
    rejected = analyze("Question", [visible], None, model("invented"))
    assert rejected.claims == []
    assert any("quotation" in item and "omitted" in item for item in rejected.limitations)
    assert "invented" not in str(rejected)
    result = analyze("Question", [hidden], None, model("100 million"))
    assert result.claims == []
    assert any("display permission" in item for item in result.limitations)


def test_keeps_verified_portions_but_marks_partially_invalid_citations():
    report = source("s", text="Revenue 100. Profit 20.")
    def model(_):
        return {"claims": [{"text": "Revenue", "citations": [{"source_id": "s", "quotation": "Revenue 100"}]},
                           {"text": "Profit", "citations": [{"source_id": "s", "quotation": "Profit 20"},
                                                                {"source_id": "s", "quotation": "invented"}]}],
                "facts": [{"fact_id": "r", "label": "Revenue", "value": "100", "unit": "USD", "period": "2023",
                           "citation": {"source_id": "s", "quotation": "Revenue 100"}}]}, 10
    result = analyze("Revenue and profit?", [report], None, model)
    assert len(result.claims) == 2 and len(result.facts) == 1
    assert result.claims[0]["support_status"] == "not_independently_verified"
    assert result.claims[1]["support_status"] == "incomplete" and len(result.claims[1]["citations"]) == 1
    assert "invented" not in str(result)
    assert any("quotation" in item and "omitted" in item for item in result.limitations)


def test_joined_model_quote_recovers_only_independently_located_passages():
    report = source("s", text='{"unit":"USD millions","revenue":{"2023":100,"2024":120}}')
    def run(quote):
        return analyze("Revenue?", [report], None, lambda _: ({"claims": [{"text": "Revenue grew", "citations": [
            {"source_id": "s", "quotation": quote}]}]}, 10))
    result = run('"revenue":{"2023":100,"2024":120} and "unit":"USD millions"')
    claim = result.claims[0]
    assert claim["support_status"] == "incomplete" and len(claim["citations"]) == 2
    assert all(item["quotation_match"] == "split_conjunction" for item in claim["citations"])
    assert all(report.text[item["location"]["character_start"]:item["location"]["character_end"]] == item["quotation"]
               for item in claim["citations"])
    assert any("split into separately located" in item for item in result.limitations)
    assert not run('"revenue":{"2023":100,"2024":120} and "unit":"EUR"').claims
    assert not run('"unit":"USD millions" and "unit":"USD millions"').claims
    assert not run('"unit":"USD millions" and revenue ... 120').claims


def test_joined_quote_recovery_rejects_ambiguous_fragments():
    report = source("s", text="Unit USD. Revenue 100. Unit USD. Revenue 200.")
    output = {"claims": [{"text": "Revenue", "citations": [{"source_id": "s", "quotation": "Unit USD and Revenue 100"}]}]}
    result = analyze("Revenue?", [report], None, lambda _: (output, 1))
    assert not result.claims


def test_duplicate_model_fact_ids_cannot_feed_a_calculation():
    report = source("s", text="Revenue 100. Profit 20.")
    def model(_):
        def item(value, quote):
            return {"fact_id": "same", "label": "Value", "value": value, "unit": "USD", "period": "2023",
                    "citation": {"source_id": "s", "quotation": quote}}
        return {"facts": [item("100", "Revenue 100"), item("20", "Profit 20")],
                "calculations": [{"formula": "difference", "first_fact_id": "same", "second_fact_id": "same"}]}, 10
    result = analyze("Report?", [report], None, model)
    assert result.facts == [] and result.calculations == []
    assert any("ambiguous identifier" in item for item in result.limitations)


def test_reversed_growth_periods_omit_calculation_but_retain_source_facts():
    report = source("s", text="Revenue in 2023 was 80. Revenue in 2024 was 100.")
    def item(fact_id, value, period, quote):
        return {"fact_id": fact_id, "label": "Revenue", "value": value, "unit": "USD",
                "currency": "USD", "period": period, "citation": {"source_id": "s", "quotation": quote}}
    output = {"facts": [item("old", "80", "2023", "Revenue in 2023 was 80"),
                        item("new", "100", "2024", "Revenue in 2024 was 100")],
              "calculations": [{"formula": "growth_percent", "first_fact_id": "old", "second_fact_id": "new"}]}
    result = analyze("Revenue growth?", [report], None, lambda _: (output, 10))
    assert len(result.facts) == 2 and not result.calculations
    assert any("current period must be later" in text for text in result.limitations)


def test_pdf_line_wrap_matches_only_whitespace_and_preserves_original():
    report = source("s", text="GDP increased 3.3 percent in the fourth quarter of\n2023.")
    def generate(quotation):
        return lambda _: ({"claims": [{"text": "GDP increased", "citations": [
            {"source_id": "s", "quotation": quotation}]}]}, 10)
    result = analyze("GDP 2023", [report], None, generate("GDP increased 3.3 percent in the fourth quarter of 2023"))
    citation = result.claims[0]["citations"][0]
    assert citation["quotation"] == "GDP increased 3.3 percent in the fourth quarter of\n2023"
    assert citation["quotation_match"] == "whitespace_normalized"
    assert report.text[citation["location"]["character_start"]:citation["location"]["character_end"]] == citation["quotation"]
    rejected = analyze("GDP 2023", [report], None, generate("GDP increased 3.4 percent in the fourth quarter of 2023"))
    assert rejected.claims == []
    assert any("quotation" in item and "omitted" in item for item in rejected.limitations)


def test_expands_ellipsis_only_to_a_unique_continuous_original_span():
    report = source("s", text="Unit USD millions; intervening context; Revenue 91471 in 2023.")
    quote = "Unit USD millions ... Revenue 91471"
    def model(_):
        citation = {"source_id": "s", "quotation": quote}
        return {"claims": [{"text": "Revenue", "citations": [citation]}],
                "facts": [{"fact_id": "r", "label": "Revenue", "value": "91471", "unit": "USD millions",
                           "period": "2023", "citation": citation}]}, 10
    result = analyze("Revenue?", [report], None, model)
    for item in (result.claims[0]["citations"][0], result.facts[0]):
        loc = item["location"]
        assert item["quotation_match"] == "expanded_ellipsis"
        assert item["quotation"] == "Unit USD millions; intervening context; Revenue 91471"
        assert report.text[loc["character_start"]:loc["character_end"]] == item["quotation"]


def test_expands_unspaced_ellipsis_to_continuous_json_source():
    report = source("s", text='{"issuer":"Example","unit":"USD millions","period":"2024","revenue":100}')
    quote = '{"issuer":"Example","unit":"USD millions",..."revenue":100}'
    result = analyze("Revenue?", [report], None, lambda _: ({"claims": [{"text": "Revenue",
        "citations": [{"source_id": "s", "quotation": quote}]}]}, 10))
    citation = result.claims[0]["citations"][0]
    assert citation["quotation"] == report.text and citation["quotation_match"] == "expanded_ellipsis"


def test_month_and_year_comparison_select_distinct_prose_passages():
    paragraph_a = "From the preceding month, the PCE price index increased 0.1 percent."
    paragraph_b = "From the same month one year ago, the PCE price index increased 2.1 percent."
    report = ("PCE price index 2025 table. " * 350) + paragraph_a + (" neutral filler " * 80) + paragraph_b + ("PCE price index 2025. " * 350)
    windows = _select_windows(report, "2025 年 PCE 价格指数较上月和较上年同期分别变化多少？", 300, 2)
    assert any("0.1 percent" in part and "preceding month" in part for _, part in windows)
    assert any("2.1 percent" in part and "same month one year" in part for _, part in windows)


def test_period_anchor_retains_a_number_after_wide_pdf_chart_spacing():
    monthly = "From the preceding month" + " " * 210 + "0.1 percent."
    annual = "From the same month one year ago, PCE increased 2.1 percent."
    report = ("PCE price index 2025 table. " * 240) + monthly + (" filler " * 80) + annual + ("PCE price index 2025. " * 240)
    windows = _select_windows(report, "2025 年 PCE 价格指数较上月和较上年同期分别变化多少？", 250, 2)
    assert any("preceding month" in part and "0.1 percent" in part for _, part in windows)
    assert any("same month one year" in part and "2.1 percent" in part for _, part in windows)


def test_expanded_ellipsis_does_not_hide_ambiguity_or_cross_the_length_limit():
    def model(_):
        return {"claims": [{"text": "Revenue", "citations": [
            {"source_id": "s", "quotation": "Unit USD ... Revenue 100"}]}]}, 10
    ambiguous = analyze("Revenue?", [source("s", text="Unit USD; Revenue 100. Unit USD; Revenue 100.")], None, model)
    assert ambiguous.claims[0]["citations"][0]["location"]["status"] == "ambiguous"
    distant = analyze("Revenue?", [source("s", text="Unit USD" + "x" * 1200 + "Revenue 100")], None, model)
    assert distant.claims == [] and any("quotation" in item and "omitted" in item for item in distant.limitations)


def test_literal_ellipsis_is_not_expanded_when_it_matches_exactly():
    report = source("s", text="Unit USD ... Revenue 100")
    result = analyze("Revenue?", [report], None, lambda _: ({"claims": [{"text": "Revenue", "citations": [
        {"source_id": "s", "quotation": "Unit USD ... Revenue 100"}]}]}, 10))
    assert result.claims[0]["citations"][0]["quotation_match"] == "exact"


def test_future_or_unknown_publication_is_not_sent():
    cutoff = datetime(2023, 1, 1, tzinfo=timezone.utc)
    later = source("later", published_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
    unknown = source("unknown")
    def must_not_call(_):
        raise AssertionError("No eligible source should reach the model")
    result = analyze("Question", [later, unknown], cutoff, must_not_call)
    assert result.source_ids == []
    assert len(result.limitations) == 3


def test_source_revised_after_cutoff_is_not_sent_even_if_published_before():
    cutoff = datetime(2023, 6, 1, tzinfo=timezone.utc)
    report = source("revised", published_at=datetime(2023, 1, 1, tzinfo=timezone.utc))
    report.revised_at = datetime(2023, 7, 1, tzinfo=timezone.utc)
    def must_not_call(_):
        raise AssertionError("A later revision should not reach the model")
    result = analyze("Question", [report], cutoff, must_not_call)
    assert result.source_ids == []
    assert any("availability at the requested time is not established" in item for item in result.limitations)


def test_model_facts_are_quoted_and_calculations_are_deterministic():
    report = source("s", text="FY2022 revenue was 198,270 million. FY2023 revenue was 211,915 million.")
    def model(_prompt):
        return {"claims": [], "counterarguments": [], "limitations": [], "facts": [
            {"fact_id": "prior", "label": "Revenue", "value": "198270", "unit": "USD million", "currency": "USD", "period": "FY2022",
             "citation": {"source_id": "s", "quotation": "FY2022 revenue was 198,270 million."}},
            {"fact_id": "current", "label": "Revenue", "value": "211915", "unit": "USD million", "currency": "USD", "period": "FY2023",
             "citation": {"source_id": "s", "quotation": "FY2023 revenue was 211,915 million."}}],
            "calculations": [{"formula": "growth_percent", "first_fact_id": "current", "second_fact_id": "prior"}]}, 30
    result = analyze("Revenue growth?", [report], None, model)
    assert result.calculations[0]["value"] == "6.88"
    assert result.facts[0]["verification"] == "model_extraction_not_independently_verified"
    assert result.calculations[0]["inputs"][0]["fact_id"] == "current"

    def fabricated(prompt):
        payload, count = model(prompt)
        payload["facts"][0]["value"] = "123"
        return payload, count
    rejected = analyze("Revenue growth?", [report], None, fabricated)
    assert [item["fact_id"] for item in rejected.facts] == ["current"]
    assert rejected.calculations == []
    assert any("absent from its quotation" in item for item in rejected.limitations)


def test_negative_parenthetical_value_is_not_treated_as_positive():
    report = source("s", text="Loss was (1,000) million.")
    def model(_prompt):
        return {"facts": [{"fact_id": "loss", "label": "Loss", "value": "1000", "unit": "USD million",
                 "currency": "USD", "period": "FY2023", "citation": {"source_id": "s", "quotation": "Loss was (1,000) million."}}]}, None
    rejected = analyze("Loss?", [report], None, model)
    assert rejected.facts == []
    assert any("absent from its quotation" in item for item in rejected.limitations)


def test_long_source_uses_relevant_window_with_exact_original_offsets_and_budget():
    prefix = "Unrelated material. " * 1400
    quote = "Revenue for 2023 was 211,915 million."
    report = source("long", text=prefix + quote + " Other details. " * 300)
    def model(prompt):
        assert len(prompt) <= 5000
        assert quote in prompt
        return {"claims": [{"text": "Revenue was reported", "citations": [
            {"source_id": "long", "quotation": quote}]}]}, 20
    result = analyze("2023 revenue?", [report], None, model, max_prompt_chars=5000)
    citation = result.claims[0]["citations"][0]
    assert citation["location"]["character_start"] == len(prefix)
    assert report.text[citation["location"]["character_start"]:citation["location"]["character_end"]] == quote
    assert result.segments[0]["character_start"] <= len(prefix) < result.segments[0]["character_end"]
    assert result.prompt_characters <= 5000
    assert len(result.prompt_sha256 or "") == 64
    assert any("remaining text was not examined" in item for item in result.limitations)


def test_repeated_quotation_returns_candidates_instead_of_false_unique_location():
    report = source("s", text="First: Revenue 100. Second: Revenue 100.")
    def model(_prompt):
        citation = {"source_id": "s", "quotation": "Revenue 100"}
        return {"claims": [{"text": "Reported", "citations": [citation]}],
                "facts": [{"fact_id": "a", "label": "Revenue", "value": "100", "unit": "USD",
                           "currency": "USD", "period": "2023", "citation": citation}]}, 10
    result = analyze("Revenue?", [report], None, model)
    for location in (result.facts[0]["location"], result.claims[0]["citations"][0]["location"]):
        assert location["status"] == "ambiguous" and "character_start" not in location
        assert len(location["matches"]) == 2 and not location["truncated"]
        assert all(report.text[item["character_start"]:item["character_end"]] == "Revenue 100" for item in location["matches"])
    assert sum("location is ambiguous" in item for item in result.limitations) == 1


def test_repeated_quotation_candidate_count_is_bounded():
    report = source("s", text="Revenue 100. " * 8)
    result = analyze("Revenue?", [report], None, lambda _: ({"claims": [
        {"text": "Reported", "citations": [{"source_id": "s", "quotation": "Revenue 100"}]}]}, None))
    location = result.claims[0]["citations"][0]["location"]
    assert len(location["matches"]) == 5 and location["truncated"] is True


def test_insufficient_prompt_budget_avoids_external_call():
    def must_not_call(_):
        raise AssertionError("No source can fit")
    result = analyze("question", [source("s")], None, must_not_call, max_prompt_chars=100)
    assert result.source_ids == []
    assert any("input budget" in item for item in result.limitations)


def test_multilingual_source_respects_byte_budget():
    report = source("chinese", text="营业收入增长。" * 1000)
    def model(prompt):
        assert len(prompt.encode("utf-8")) <= 2000
        return {"claims": [], "counterarguments": [], "limitations": []}, 10
    result = analyze("营业收入增长情况", [report], None, model, max_prompt_chars=2000)
    assert result.source_ids == ["chinese"]
    assert result.prompt_bytes <= 2000


@pytest.mark.parametrize("reverse", [False, True])
def test_multilingual_comparison_reserves_budget_for_each_source(reverse):
    positive = source("positive", text="营业收入增长证据。" * 1000)
    negative = source("negative", text="相反证据收入下降。" * 1000)
    reports = [negative, positive] if reverse else [positive, negative]

    def model(prompt):
        assert len(prompt.encode("utf-8")) <= 3000
        parts = json.loads(prompt)["sources"]
        assert {part["source_id"] for part in parts} == {"positive", "negative"}
        assert all(part["source_coverage"] == "partial" and part["text"] for part in parts)
        return {}, 10

    result = analyze("收入变化", reports, None, model, max_prompt_chars=3000)
    assert set(result.source_ids) == {"positive", "negative"}
    assert all(any(source_id in gap and "remaining text" in gap for gap in result.limitations)
               for source_id in ("positive", "negative"))


def test_pdf_horizontal_spacing_is_compacted_only_in_prompt_and_offsets_stay_original():
    quote = "Personal income:\nCurrent dollars" + " " * 150 + "74.4  88.2"
    report = source("pdf", text=quote).model_copy(update={"retrieval_method": "user_pdf"})

    def model(prompt):
        item = json.loads(prompt)["sources"][0]
        assert item["text"] == "Personal income:\nCurrent dollars\t74.4\t88.2"
        return {"claims": [{"text": "Personal income was revised.", "citations": [
            {"source_id": "pdf", "quotation": item["text"]}]}]}, 10

    result = analyze("收入?", [report], None, model)
    citation = result.claims[0]["citations"][0]
    assert citation["quotation"] == quote and citation["quotation_match"] == "whitespace_normalized"
    assert citation["location"]["character_end"] == len(quote)
    assert result.segments[0]["character_end"] == len(quote)


def test_compacted_pdf_offsets_map_all_boundaries_back_to_original():
    import re
    original = "前  \t  📈  100\nQ\t\t3   end"
    compacted, offset = _compact_pdf_with_offsets(original)
    assert compacted == "前\t📈\t100\nQ\t3\tend"
    for start in range(len(compacted) + 1):
        for end in range(start, len(compacted) + 1):
            assert re.sub(r"[ \t]{2,}", "\t", original[offset(start):offset(end)]) == compacted[start:end]
    with pytest.raises(ValueError):
        offset(len(compacted) + 1)


def test_two_pdf_tables_keep_labels_and_values_under_shared_budget():
    def report(sid, earlier, revised):
        table = ("Change from preceding month\n" + " " * 40 + "August" + " " * 30 + "September\n" +
                 "Previous  Revised  Previous  Revised\n(Billions of dollars)  (Percent)\nPersonal income:\n" +
                 "Current dollars" + " " * 45 + f"{earlier}     {revised}\n")
        text = "Unrelated introduction. " * 500 + table + "Unrelated appendix. " * 500
        return source(sid, text=text).model_copy(update={"retrieval_method": "user_pdf"})
    reports = [report("first", "71.6", "74.4"), report("second", "74.4", "88.2")]

    def model(prompt):
        parts = json.loads(prompt)["sources"]
        for sid, value in (("first", "74.4"), ("second", "88.2")):
            assert any(part["source_id"] == sid and value in part["text"] and "income" in part["text"].lower()
                       and "september" in part["text"].lower() for part in parts)
        assert len(prompt.encode()) <= 2055
        return {}, 10

    result = analyze("比较2024年9月个人收入较前月的当期美元变化。", reports, None, model, max_prompt_chars=2055)
    assert result.source_ids == ["first", "second"]
    assert all(any(s["source_id"] == item.source_id for s in result.segments) for item in reports)


def test_comparison_windows_keep_unequal_extracted_passages_within_budget():
    month = "From the preceding month, the" + " " * 90 + "\nPCE price index for December" + " " * 75 + "\nincreased 0.3 percent."
    year = "From the same month one year" + " " * 30 + "\nago, the PCE price index increased 2.6 percent."
    text = "Unrelated heading. " * 40 + month + "\nOther context. " * 10 + year + "\nAppendix. " * 40
    result = _comparison_windows(text, "价格指数较上月和较上年同期分别多少？", 440)
    assert result is not None and len(result) == 2
    assert all(text[start:start + len(part)] == part for start, part in result)
    assert "preceding month" in result[0][1] and "0.3 percent" in result[0][1]
    assert "same month one year" in result[1][1] and "2.6 percent" in result[1][1]
    assert sum(len(part) for _, part in result) <= 440
    assert _comparison_windows(text, "价格指数较上月和较上年同期分别多少？", 100) is None


def test_comparison_windows_prefer_requested_indicator_over_earlier_pair():
    unrelated = ("From the preceding month, unemployment rose 1.0 percent. "
                 "From the same month one year ago unemployment rose 2.0 percent.")
    relevant = ("From the preceding month, the PCE price index increased 0.3 percent. "
                "From the same month one year ago the PCE price index increased 2.6 percent.")
    text = unrelated + ("\nUnrelated appendix. " * 60) + relevant + ("\nOther text. " * 60)
    result = _comparison_windows(text, "PCE价格指数较上月和较上年同期的变化？", 300)
    assert result is not None
    assert "0.3 percent" in result[0][1] and "2.6 percent" in result[1][1]
    assert all(text[start:start + len(part)] == part for start, part in result)


def test_comparison_windows_do_not_substitute_shorter_unrelated_pair():
    short = ("From the preceding month, price increased 1.0 percent. "
             "From the same month one year ago price increased 2.0 percent.")
    long = ("From the preceding month, PCE price index " + " " * 150 + "increased 0.3 percent. "
            "From the same month one year ago PCE price index " + " " * 150 + "increased 2.6 percent.")
    assert _comparison_windows(short + "\n" * 100 + long, "PCE价格指数较上月和较上年同期？", 180) is None


def test_chinese_year_and_relevance_outrank_unrelated_numeric_density():
    target = "Revenue increased in 2023 to 200 million."
    text = ("Generic discussion. " * 500 + target + " Context. " * 200 +
            "Revenue data: " + "999999 888888 777777 " * 300)
    start, passage = _select_window(text, "2023年营收增长多少？", 1000)
    assert target in passage
    assert text[start:start + len(passage)] == passage


def test_separate_passages_cannot_be_stitched_into_a_fake_quotation():
    filler_a = " ".join(f"rowA{i:05d}" for i in range(1000))
    filler_b = " ".join(f"rowB{i:05d}" for i in range(1000))
    report = source("s", text="2023 Revenue 100. " + filler_a + " 2023 Profit 200. " + filler_b)
    def model(prompt):
        parts = json.loads(prompt)["sources"]
        assert len(parts) == 2
        quote = parts[0]["text"][-30:] + parts[1]["text"][:30]
        assert quote not in report.text
        return {"claims": [{"text": "Invented join", "citations": [{"source_id": "s", "quotation": quote}]}]}, 10
    rejected = analyze("2023 revenue and profit", [report], None, model, max_prompt_chars=5000)
    assert rejected.claims == []
    assert any("quotation" in item and "omitted" in item for item in rejected.limitations)


def test_ellipsis_does_not_stitch_two_selected_passages():
    report = source("s", text="2023 Revenue 100. " + "background " * 1200 +
                    "2023 Profit 200. " + "irrelevant " * 1200)
    def model(prompt):
        assert len(json.loads(prompt)["sources"]) == 2
        return {"claims": [{"text": "Both", "citations": [{"source_id": "s",
                "quotation": "2023 Revenue 100. ... 2023 Profit 200."}]}]}, 10
    result = analyze("2023 revenue and profit", [report], None, model, max_prompt_chars=5000)
    assert result.claims == [] and any("quotation" in item and "omitted" in item for item in result.limitations)


def test_long_document_uses_two_windows_under_a_thousand_character_allowance():
    report = source("s", text="2023 Revenue 100. " + "background " * 400 +
                    "2023 Profit 200. " + "irrelevant " * 400)
    def model(prompt):
        parts = json.loads(prompt)["sources"]
        assert len(parts) == 2
        assert sum(len(part["text"]) for part in parts) < 1000
        assert any("2023 Revenue 100." in part["text"] for part in parts)
        assert any("2023 Profit 200." in part["text"] for part in parts)
        return {"claims": [], "counterarguments": [], "limitations": []}, 10
    result = analyze("2023 revenue and profit", [report], None, model, max_prompt_chars=1750)
    assert len(result.segments) == 2 and result.prompt_bytes <= 1750


def test_short_document_remains_whole_when_it_fits():
    report = source("s", text="Annual report. " * 150)
    result = analyze("report", [report], None, lambda _: ({}, 10), max_prompt_chars=5000)
    assert len(result.segments) == 1
    assert result.segments[0]["character_start"] == 0
    assert result.segments[0]["character_end"] == len(report.text)


def test_casefold_expansion_preserves_original_character_offsets():
    prefix = "ß " * 1000
    quote = "2023 Revenue was 100 million."
    report = source("s", text=prefix + quote + " unrelated " * 1000)
    def model(prompt):
        assert any(quote in part["text"] for part in json.loads(prompt)["sources"])
        return {"claims": [{"text": "Revenue", "citations": [{"source_id": "s", "quotation": quote}]}]}, 10
    result = analyze("2023 revenue", [report], None, model, max_prompt_chars=3000)
    location = result.claims[0]["citations"][0]["location"]
    assert location["character_start"] == len(prefix)
    assert report.text[location["character_start"]:location["character_end"]] == quote


def test_time_budget_interrupts_source_selection_before_model_call():
    report = source("s", text="2023 revenue material. " * 10000)
    checks = 0
    def stop():
        nonlocal checks
        checks += 1
        return checks >= 4
    def must_not_call(_prompt):
        raise AssertionError("Model must not be called after time budget expires")
    with pytest.raises(AnalysisInterrupted):
        analyze("2023 revenue", [report], None, must_not_call, max_prompt_chars=3000, should_stop=stop)
    assert checks == 4


def test_excerpt_word_trimming_preserves_original_coordinates_and_non_ascii_text():
    text = "Before the important revenue figure ends after the sentence."
    start, end = text.index("portant"), text.index("sentenc") + len("sentenc")
    offset, passage = _trim_partial_ascii_words(text, start, text[start:end])
    # Tiny fragments are intentionally preserved to avoid discarding context.
    assert offset == start and passage == text[start:end]
    text = "prefix fragmented " + ("relevant content " * 6) + "unfinished suffix"
    start, end = text.index("mented"), text.index("unfinish") + len("unfinish")
    offset, passage = _trim_partial_ascii_words(text, start, text[start:end])
    assert passage == text[offset:offset + len(passage)]
    assert not passage.startswith("mented") and not passage.endswith("unfinish")
    chinese = "前文数据" * 20
    offset, passage = _trim_partial_ascii_words(chinese, 3, chinese[3:70])
    assert offset == 3 and passage == chinese[3:70]


@pytest.mark.parametrize("url", ["https://fred.stlouisfed.org./series/GDP",
    "https://alfred.stlouisfed.org/series/GDP", "https://api.stlouisfed.org/fred/series/observations"])
def test_fred_endpoint_variants_are_not_sent_to_models(url):
    def must_not_call(_):
        raise AssertionError("FRED is not an authorized model input")
    result = analyze("GDP?", [source("fred", url=url)], None, must_not_call)
    assert result.source_ids == []


def test_fiscal_revenue_table_keeps_both_years_and_row_within_budget():
    table = ("Year Ended\n| January 29, 2023 | January 30, 2022 |\n"
             "| (USD in millions) |\n| Revenue | 26,974 | 26,914 |\n")
    text = "Unrelated notes. " * 70 + table + " Other text. " * 70
    selected = _financial_table_windows(text, "2023 与 2022 财年收入多少？", 200)
    assert selected is not None
    start, passage = selected[0]
    assert len(passage) <= 200 and text[start:start + len(passage)] == passage
    assert all(term in passage for term in ("2023", "2022", "millions", "26,974", "26,914"))
    assert _financial_table_windows(text, "2023 年现金是多少？", 200) is None
    assert _financial_table_windows(text, "2023 与 2021 财年收入多少？", 200) is None
    assert _financial_table_windows(text, "2023 与 2022 财年收入多少？", 30) is None


def test_vote_and_second_estimate_anchors_find_requested_passages():
    report = ("unrelated business commentary " * 50 +
              "In the second estimate, real GDP increased 2.9 percent. " +
              "unrelated commentary " * 50 +
              "Voting against this action: None. " + "unrelated commentary " * 50)
    estimates = _select_windows(report, "2022 GDP 第二次估计是多少？", 180, 2)
    assert any("second estimate" in part and "2.9 percent" in part for _, part in estimates)
    votes = _select_windows(report, "该行动有委员投反对票吗？", 180, 2)
    assert any("Voting against this action: None" in part for _, part in votes)


def test_grouped_number_normalization_requires_valid_grouping_and_matching_quote():
    report = source("s", text="Revenue was 26,974 in 2023 and 26,914 in 2022.")
    def item(fact_id, value, period, quotation):
        return {"fact_id": fact_id, "label": "Revenue", "value": value, "unit": "USD million",
                "currency": "USD", "period": period, "citation": {"source_id": "s", "quotation": quotation}}
    raw = {"facts": [item("current", "26,974", "2023", "Revenue was 26,974 in 2023"),
                     item("prior", "26,914", "2022", "26,914 in 2022"),
                     item("invalid", "26,91", "2022", "26,914 in 2022"),
                     item("wrong", "26,973", "2023", "Revenue was 26,974 in 2023")],
           "calculations": [{"formula": "growth_percent", "first_fact_id": "current", "second_fact_id": "prior"}]}
    result = analyze("Revenue?", [report], None, lambda _: (raw, 1))
    assert [(fact["fact_id"], fact["value"], fact["numeric_normalization"]) for fact in result.facts] == [
        ("current", "26974", "thousands_separator"), ("prior", "26914", "thousands_separator")]
    assert result.calculations[0]["value"] == "0.22"
    assert any("format was invalid" in note for note in result.limitations)
    assert any("absent from its quotation" in note for note in result.limitations)


def test_policy_vote_retrieval_keeps_action_and_vote_as_separate_source_passages():
    action = "The members agreed to raise the target range for the federal funds rate to 5 percent."
    vote = "Voting against this action: None."
    text = "intro " * 30 + action + " unrelated material " * 100 + vote + " end " * 30
    result = _policy_vote_windows(text, "美联储货币政策行动有反对票吗？", 250)
    assert result is not None and len(result) == 2
    assert sum(len(part) for _, part in result) <= 250
    assert action in result[0][1] and vote in result[1][1]
    assert all(text[start:start + len(part)] == part for start, part in result)
    assert _policy_vote_windows(text, "其他会议的反对票？", 250) is None
    assert _policy_vote_windows(text, "美联储货币政策行动有反对票吗？", 30) is None
    assert _policy_vote_windows(vote + " unrelated ", "美联储货币政策行动有反对票吗？", 250) is None


def test_bounded_non_schema_annotations_are_discarded_without_using_their_contents():
    report = source("s", text="Revenue was 100 million in 2023.")
    citation = {"source_id": "s", "quotation": "Revenue was 100 million in 2023"}
    raw = {"claims": [{"text": "Revenue was 100 million", "citations": [citation], "status": "evidence"}],
           "calculations": [{"formula": "difference", "first_fact_id": "missing", "second_fact_id": "missing",
                             "note": "untrusted instructions"}]}
    result = analyze("Revenue?", [report], None, lambda _: (raw, 1))
    assert result.claims[0]["text"] == "Revenue was 100 million" and "status" not in result.claims[0]
    assert "untrusted instructions" not in str(result)
    assert any("annotations were omitted" in note for note in result.limitations)
    assert any("calculation was omitted" in note for note in result.limitations)
    assert raw["claims"][0]["status"] == "evidence"
    for extra in ({"another_field": "unsupported"}, {"status": {"nested": True}}, {"status": "x" * 101}):
        bad = {"claims": [{"text": "Revenue", "citations": [citation], **extra}]}
        with pytest.raises(InvalidAnalysis):
            analyze("Revenue?", [report], None, lambda _: (bad, 1))


def test_explicit_decrease_can_be_retained_as_unverified_signed_observation():
    report = source("s", text="Real GDP decreased 0.6 percent. It increased 3.2 percent later.")
    def raw(quotation, value="-0.6", unit="percent"):
        return {"facts": [{"fact_id": "old", "label": "GDP change", "value": value, "unit": unit,
                           "period": "2022", "citation": {"source_id": "s", "quotation": quotation}},
                          {"fact_id": "new", "label": "GDP change", "value": "3.2", "unit": "percent",
                           "period": "2023", "citation": {"source_id": "s", "quotation": "increased 3.2 percent"}}],
                "calculations": [{"formula": "difference", "first_fact_id": "new", "second_fact_id": "old"}]}
    output = analyze("GDP?", [report], None, lambda _: (raw("decreased 0.6 percent"), 1))
    old = next(item for item in output.facts if item["fact_id"] == "old")
    assert old["value"] == "-0.6" and old["direction_context_unverified"] is True
    assert not output.calculations
    assert any("excluded from automatic calculations" in note for note in output.limitations)
    assert not any(item["fact_id"] == "old" for item in analyze("GDP?", [report], None,
        lambda _: (raw("decreased 0.6 percent", value="-0.7"), 1)).facts)
    assert not any(item["fact_id"] == "old" for item in analyze("GDP?", [report], None,
        lambda _: (raw("decreased 0.6 percent", unit="USD"), 1)).facts)
    negated = source("s", text="Real GDP did not decrease 0.6 percent.")
    negative_raw = {"facts": [{"fact_id": "n", "label": "GDP", "value": "-0.6", "unit": "percent", "period": "2022",
                               "citation": {"source_id": "s", "quotation": "did not decrease 0.6 percent"}}]}
    assert not analyze("GDP?", [negated], None, lambda _: (negative_raw, 1)).facts


def test_qualified_percent_currency_is_not_used_for_automatic_calculation():
    report = source("s", text="The annualized rate was 3.2 percent, previously 2.9 percent.")
    def item(fact_id, value):
        return {"fact_id": fact_id, "label": "rate", "value": value, "unit": "percent (annual rate)",
                "currency": "USD", "period": "2022", "citation": {"source_id": "s", "quotation": value + " percent"}}
    raw = {"facts": [item("current", "3.2"), item("previous", "2.9")],
           "calculations": [{"formula": "difference", "first_fact_id": "current", "second_fact_id": "previous"}]}
    result = analyze("Rate?", [report], None, lambda _: (raw, 1))
    assert len(result.facts) == 2 and all(item["currency_context_unverified"] for item in result.facts)
    assert not result.calculations


def test_financial_table_keeps_original_header_and_total_when_rows_are_long():
    header = ("ACME CORP\nConsolidated Statements of Operations\n(in millions)\n"
              "| Years Ended | July 31, 2024 | July 31, 2023 |\n")
    middle = "| Product | 150 | 140 |\n" + "| Other item | 5 | 4 |\n" * 10
    total = "| Total revenue | 200 | 190 |\n"
    unrelated = ("pro forma information\n(in millions)\n| Years Ended | 2024 | 2023 |\n"
                 "| Revenue | 500 | 400 |\n")
    text = "unrelated " * 80 + unrelated + "other material " * 30 + header + middle + total
    parts = _financial_table_windows(text, "2024 与 2023 财年营收多少？", 240)
    assert parts and len(parts) == 2
    assert sum(len(part) for _, part in parts) <= 240
    assert all(text[start:start + len(part)] == part for start, part in parts)
    assert "ACME CORP" in parts[0][1] and "millions" in parts[0][1]
    assert "2024" in parts[0][1] and "2023" in parts[0][1]
    assert "200" in parts[1][1] and "190" in parts[1][1]
    assert "500" not in ''.join(part for _, part in parts)


def test_vote_retrieval_keeps_full_names_and_policy_preference_with_initials():
    action = "The Committee decided to lower the target range for the federal funds rate to 2.0 percent."
    vote = ("Voting against the action were Jane A. Smith and Robert B. Jones, "
            "who preferred to maintain the target range at 2.25 percent.")
    text = "intro " * 50 + action + " other " * 120 + vote + " more " * 30
    parts = _policy_vote_windows(text, "美联储的反对票与利率偏好是什么？", 360)
    assert parts and len(parts) == 2
    assert action in parts[0][1] and vote in parts[1][1]
    assert all(text[start:start + len(part)] == part for start, part in parts)
    assert sum(len(part) for _, part in parts) <= 360


@pytest.mark.parametrize("verb,names", [("was", "Jane Smith"), ("were", "Jane Smith and Robert Jones")])
def test_vote_retrieval_includes_singular_and_plural_dissent(verb, names):
    action = "The Committee decided to raise the target range for the federal funds rate to 1/4 to 1/2 percent."
    vote = f"Voting against this action {verb} {names}, who preferred to raise the rate by 0.5 percentage point."
    text = "intro " * 50 + action + " background " * 130 + vote + " end " * 30
    parts = _policy_vote_windows(text, "美联储的目标利率和反对票是什么？", 300)
    assert parts and len(parts) == 2
    assert action in parts[0][1] and vote in parts[1][1]
    assert all(text[start:start + len(part)] == part for start, part in parts)
    assert sum(len(part) for _, part in parts) <= 300


def test_third_estimate_selection_keeps_current_and_prior_values():
    text = ("unrelated " * 70 +
            'Real GDP increased at an annual rate of 2.0 percent in the first quarter, according to the "third" estimate. ' +
            "background " * 15 + "In the second estimate, real GDP increased 1.3 percent. " +
            "unrelated " * 100)
    parts = _select_windows(text, "2023 GDP 第三次估计和第二次估计是多少？", 220, 2)
    assert any("2.0 percent" in part and 'third" estimate' in part for _, part in parts)
    assert any("second estimate" in part and "1.3 percent" in part for _, part in parts)
    assert all(text[start:start + len(part)] == part for start, part in parts)


def test_analyze_keeps_short_structured_total_row_with_its_longer_header():
    header = ("ACME CORP\nConsolidated Statements of Operations\n(in millions)\n"
              "| Years Ended | July 31, 2024 | July 31, 2023 |\n")
    middle = "| Product | 150 | 140 |\n" + "| Other item | 5 | 4 |\n" * 28
    text = "other " * 600 + header + middle + "| Total revenue | 200 | 190 |\n" + "other " * 600
    report = source("source_0123456789abcdef0123456789abcdef", text=text)
    received = []
    def generate(prompt):
        received.extend(json.loads(prompt)["sources"])
        return {}, 1
    result = analyze("2024 与 2023 财年营收多少？", [report], None, generate, max_prompt_chars=1800)
    excerpts = " ".join(item["text"] for item in received)
    assert len(result.segments) == 2
    assert all(value in excerpts for value in ("ACME CORP", "millions", "2024", "2023", "200", "190"))
    assert all(text[item["character_start"]:item["character_end"]] == excerpt["text"]
               for item, excerpt in zip(result.segments, received))


def test_model_table_fact_flags_currency_missing_from_the_quotation():
    text = "| Total revenue | 100 | 90 |"
    report = source("s", text=text)
    raw = {"facts": [{"fact_id": "f", "label": "Revenue", "value": "100", "unit": "millions",
                       "currency": "USD", "period": "2024", "citation": {"source_id": "s", "quotation": text}}]}
    result = analyze("Revenue?", [report], None, lambda _: (raw, 1))
    assert result.facts[0]["currency_quote_missing"] is True
    assert any("currency identifier is absent" in note for note in result.limitations)
    assert result.facts[0]["verification"] == "model_extraction_not_independently_verified"


def test_monthly_table_retrieval_prefers_complete_row_with_header():
    table = ("Change from Preceding Period (Months)\n[Billions of dollars]\nSeasonally adjusted at annual rates\n"
             "2024\nMarch April May June July Aug. Sept. Oct.\n"
             "1 Personal income 144.0 36.2 73.2 42.5 68.4 45.1 74.4 147.4 1\n")
    text = "personal income dollar data " * 60 + table + "personal income dollar data " * 60
    selected = _select_window(text, "2024年9月个人收入较前月变化的美元金额是多少？", 250)
    assert "Change from Preceding Period (Months)" in selected[1]
    assert "Sept." in selected[1] and "74.4" in selected[1] and "147.4" in selected[1]
    assert text[selected[0]:selected[0] + len(selected[1])] == selected[1]


def test_focused_passage_is_included_and_budget_failure_never_calls_model():
    quote = 'Special finding: revenue was 123 dollars.'
    report = source('focus', text='unrelated text. ' * 500 + quote + ' context. ' * 100)
    start = report.text.index(quote)
    prompts = []
    def generate(prompt):
        prompts.append(json.loads(prompt))
        return {'claims': [{'text': 'Revenue was 123', 'citations': [{'source_id': 'focus', 'quotation': quote}]}]}, 10
    result = analyze('Revenue?', [report], None, generate, focus=('focus', start, start + len(quote)))
    assert quote in prompts[0]['sources'][0]['text']
    assert prompts[0]['sources'][0]['user_selected_passage'] == quote
    assert 'do not assume it supports any claim' in prompts[0]['evidence_guidance']
    assert result.claims[0]['citations'][0]['location']['character_start'] == start
    assert any(segment['character_start'] <= start and segment['character_end'] >= start + len(quote) for segment in result.segments)
    prompts.clear()
    result = analyze('Revenue?', [report], None, generate, focus=('focus', start, start + len(quote)), max_prompt_chars=50)
    assert not prompts and any('no model call was made' in item for item in result.limitations)
    denied = source('focus', text=report.text, model_input=False)
    result = analyze('Revenue?', [denied], None, generate, focus=('focus', start, start + len(quote)))
    assert not prompts and any('no model call was made' in item for item in result.limitations)


def test_focused_pdf_passage_is_labeled_and_uses_same_whitespace_as_source_text():
    quote = '收入    123 million'
    report = source('pdf', text='其他段落。' * 300 + quote + '  additional context.')
    report.retrieval_method = 'user_pdf'
    start = report.text.index(quote)
    prompts = []
    def generate(prompt):
        prompts.append(json.loads(prompt))
        return {'claims': []}, 1
    analyze('What is the revenue?', [report], None, generate, focus=('pdf', start, start + len(quote)))
    passage = prompts[0]['sources'][0]
    assert passage['user_selected_passage'] == '收入\t123 million'
    assert passage['user_selected_passage'] in passage['text']
    prompts.clear()
    analyze('What is the revenue?', [report], None, generate)
    assert all('user_selected_passage' not in part for part in prompts[0]['sources'])
    assert 'A user selected a passage' not in prompts[0]['evidence_guidance']


def test_model_added_json_closing_brace_recovers_only_unique_original_numeric_prefix():
    text = '{"issuer":"Example","FY2023":{"revenue":100},"FY2024":{"revenue":120,"income":8}}'
    report = source('s', text=text)
    wrong = '"FY2024":{"revenue":120}'
    raw = {'claims': [{'text': 'Revenue was 120', 'citations': [{'source_id': 's', 'quotation': wrong}]}],
           'facts': [
               {'fact_id': 'current', 'label': 'revenue', 'value': '120', 'unit': 'USD millions', 'currency': 'USD', 'period': 'FY2024', 'citation': {'source_id': 's', 'quotation': wrong}},
               {'fact_id': 'prior', 'label': 'revenue', 'value': '100', 'unit': 'USD millions', 'currency': 'USD', 'period': 'FY2023', 'citation': {'source_id': 's', 'quotation': '"FY2023":{"revenue":100}'}},
           ], 'calculations': [{'formula': 'growth_percent', 'first_fact_id': 'current', 'second_fact_id': 'prior'}]}
    result = analyze('Revenue growth?', [report], None, lambda _: (raw, 1))
    fact = result.facts[0]
    assert fact['quotation'] == '"FY2024":{"revenue":120'
    assert fact['quotation_match'] == 'trimmed_json_closure'
    assert text[fact['location']['character_start']:fact['location']['character_end']] == fact['quotation']
    assert result.claims[0]['citations'][0]['quotation'] == fact['quotation']
    assert result.calculations[0]['value'] == '20.00'
    assert any('model-added JSON closing brace' in note for note in result.limitations)

    # A prefix appearing twice is ambiguous when each occurrence is
    # followed by another property. Invalid JSON and a longer number cannot be repaired.
    for bad_text in [
        '{"a":{"FY2024":{"revenue":120,"income":8}},"b":{"FY2024":{"revenue":120,"income":9}}}',
        '"FY2024":{"revenue":120,"income":8}',
        '{"FY2024":{"revenue":1200,"income":8}}',
    ]:
        rejected = analyze('Revenue?', [source('s', text=bad_text)], None, lambda _: (raw, 1))
        assert not rejected.facts and not rejected.calculations and not rejected.claims


def test_scope_is_in_untrusted_question_and_influences_source_selection():
    target = 'Geographic segment: Oceania. Revenue was 4321 dollars.'
    text = 'Unrelated operations and commentary. ' * 180 + target + ' Other financial notes. ' * 200
    report = source('s', text=text)
    prompts = []
    def capture(prompt):
        prompts.append(json.loads(prompt))
        return {}, 1
    analyze('What was revenue?', [report], None, capture, scope='Oceania segment')
    assert 'Oceania segment' in prompts[0]['question']
    assert any(target in item['text'] for item in prompts[0]['sources'])
    assert 'untrusted data' in __import__('app.research_analysis', fromlist=['SYSTEM_PROMPT']).SYSTEM_PROMPT
    prompts.clear()
    analyze('What was revenue?', [report], None, capture)
    assert prompts[0]['question'] == 'What was revenue?'


def test_prompt_includes_cutoff_only_for_historical_research():
    cutoff = datetime(2023, 7, 1, 12, 30, 0, 123456, tzinfo=timezone.utc)
    old = source('old', published_at=datetime(2023, 1, 1, tzinfo=timezone.utc))
    later = source('later', text='Later revision', published_at=datetime(2023, 7, 2, tzinfo=timezone.utc))
    captured = []
    def generate(prompt):
        captured.append(json.loads(prompt))
        return {}, 1
    analyze('What was known?', [old, later], cutoff, generate)
    assert captured[0]['research_cutoff'] == cutoff.isoformat()
    assert 'Do not use events after research_cutoff' in captured[0]['evidence_guidance']
    assert [item['source_id'] for item in captured[0]['sources']] == ['old']
    captured.clear()
    analyze('What happened?', [old], None, generate)
    assert 'research_cutoff' not in captured[0]
    assert 'Do not use events after research_cutoff' not in captured[0]['evidence_guidance']


def test_chinese_question_retrieves_matching_chinese_source_with_original_offsets():
    from app.research_analysis import _select_windows
    target = '云计算业务2024年营业收入达到4321万元，较上年增长百分之十。'
    text = '无关地区的库存、税务和采购讨论。' * 350 + target + '其他业务说明。' * 350
    parts = _select_windows(text, '比较云计算业务2024年营业收入变化', 160, 2)
    assert any(target in passage for _, passage in parts)
    assert all(text[start:start + len(passage)] == passage for start, passage in parts)
    assert sum(len(passage) for _, passage in parts) <= 320
    calls = []
    def generate(prompt):
        payload = json.loads(prompt)
        calls.append(payload)
        return {'claims': [{'text': '营业收入达到4321万元', 'citations': [{'source_id': 's', 'quotation': target}]}]}, 5
    result = analyze('比较云计算业务2024年营业收入变化', [source('s', text=text)], None, generate, max_prompt_chars=2500)
    assert any(target in item['text'] for item in calls[0]['sources'])
    assert result.claims[0]['citations'][0]['location']['character_start'] == text.index(target)
    assert result.prompt_bytes <= 2500


def test_chinese_multitopic_question_balances_phrases_and_utf8_windows():
    text = ('資料來源：星島研究院，星島研究院發布的財務報告。\n' +
            '其他事項說明沒有相關收入與支出的數字。' * 60 +
            '2024年倉儲物流收入為123億元，按年上升5%。' +
            '其他事項說明沒有相關收入與支出的數字。' * 220 +
            '2024年雲端支出為47億元，按年下降3%。' + '其他事項說明。' * 300)
    report = source('s', text=text).model_copy(update={'title': '財務報告'})
    captured = []
    def generate(prompt):
        captured.append(json.loads(prompt))
        return {}, 1
    result = analyze('根據星島研究院2024年報告，倉儲物流收入及雲端支出分別是多少？',
                     [report], None, generate, max_prompt_chars=2055)
    passages = [item['text'] for item in captured[0]['sources']]
    assert len(passages) == 2
    assert any('倉儲物流收入為123億元' in part for part in passages)
    assert any('雲端支出為47億元' in part for part in passages)
    assert result.prompt_bytes <= 2055
    assert all(text[s['character_start']:s['character_end']] == passage
               for s, passage in zip(result.segments, passages))


def test_chinese_explicit_decline_is_retained_as_unverified_direction():
    from app.research_analysis import _directional_negative_percent
    for text in ('較上年同期下跌7.3%', '收入下降7.3％', '銷售減少了7.3%'):
        assert _directional_negative_percent(Decimal('-7.3'), '%', text)
    for text in ('收入未下跌7.3%', '收入沒有下降7.3%', '收入不一定下跌7.3%', '收入上升7.3%', '下跌7.30個百分點'):
        assert not _directional_negative_percent(Decimal('-7.3'), '%', text)
    text = '零售額較上年同月下跌7.3%。'
    quote = '較上年同月下跌7.3%'
    raw = {'facts': [{'fact_id': 'rate', 'label': 'retail change', 'value': '-7.3', 'unit': '%', 'period': '2024年11月', 'citation': {'source_id': 's', 'quotation': quote}}]}
    result = analyze('變化多少？', [source('s', text=text)], None, lambda _: (raw, 1))
    assert result.facts[0]['value'] == '-7.3' and result.facts[0]['direction_context_unverified'] is True
    assert result.facts[0]['quotation'] == quote
    assert any('excluded from automatic calculations' in note for note in result.limitations)


def test_traditional_chinese_policy_query_selects_decision_not_other_rate():
    decision = 'The Committee decided to raise the target range for the federal funds rate to 3-3/4 to 4 percent. The Committee anticipates that ongoing increases will be appropriate.'
    other = 'The Board voted to raise the interest rate paid on reserve balances to 3.9 percent.'
    text = 'Unrelated text. ' * 80 + decision + ' Other material. ' * 100 + (other + ' Other information. ') * 20
    report = source('s', text=text)
    prompts = []
    result = analyze('美聯儲決議的聯邦基金目標利率區間和未來政策為何？', [report], None,
                     lambda q: (prompts.append(json.loads(q)) or {}, 1), max_prompt_chars=2055)
    assert any('target range for the federal funds rate to 3-3/4 to 4 percent' in item['text'] for item in prompts[0]['sources'])
    assert result.prompt_bytes <= 2055
    assert all(text[seg['character_start']:seg['character_end']] == part['text'] for seg, part in zip(result.segments, prompts[0]['sources']))


def test_structured_annual_observations_keep_period_value_and_issuer_within_budget():
    from app.research_analysis import _structured_observation_windows
    data = {'issuer': 'Example Corp', 'concept': 'Revenue', 'unit': 'USD', 'notes': 'unrelated ' * 180,
            'records': [{'start': '2021-07-01', 'end': '2022-06-30', 'val': 100, 'fy': 2023},
                        {'start': '2022-07-01', 'end': '2023-06-30', 'val': 120, 'fy': 2023}]}
    text = json.dumps(data, indent=2)
    question = 'Compare revenue for 2022 and 2023.'
    windows = _structured_observation_windows(text, question, 1000)
    assert windows and len(windows) == 3
    assert 'Example Corp' in windows[0][1] and 'USD' in windows[0][1]
    assert '2022-06-30' in windows[1][1] and '100' in windows[1][1] and '"fy": 2023' in windows[1][1]
    assert '2023-06-30' in windows[2][1] and '120' in windows[2][1]
    assert all(text[start:start + len(part)] == part for start, part in windows)
    assert _structured_observation_windows(text, question, 20) is None
    duplicate = {**data, 'records': [*data['records'], data['records'][0]]}
    assert _structured_observation_windows(json.dumps(duplicate), question, 1000) is None
    mismatch = {**data, 'records': [data['records'][0], {**data['records'][1], 'unit': 'EUR'}]}
    assert _structured_observation_windows(json.dumps(mismatch), question, 1000) is None
    quarter = {**data, 'records': [data['records'][0], {**data['records'][1], 'start': '2023-04-01'}]}
    assert _structured_observation_windows(json.dumps(quarter), question, 1000) is None
    # Root metadata must not be attributed to nested records whose parent
    # can override it, even when the individual leaf repeats no unit.
    grouped = {k: v for k, v in data.items() if k != 'records'} | {
        'groups': [{'unit': 'EUR', 'records': data['records']}]}
    assert _structured_observation_windows(json.dumps(grouped), question, 1000) is None
    mixed = {**data, 'nested': {'unit': 'EUR', 'records': data['records']}}
    assert _structured_observation_windows(json.dumps(mixed), question, 1000) is None
    root_records_with_nested = {**data, 'records': [{**data['records'][0], 'dimensions': {'unit': 'EUR'}}, data['records'][1]]}
    assert _structured_observation_windows(json.dumps(root_records_with_nested), question, 1000) is None
    duplicate_key = text.replace('"issuer": "Example Corp"', '"issuer": "Example Corp", "issuer": "Other"')
    assert _structured_observation_windows(duplicate_key, question, 1000) is None
    assert _structured_observation_windows(text[:-2], question, 1000) is None
    assert _structured_observation_windows(text, 'Compare 2022, 2023 and 2024.', 1000) is None
    with pytest.raises(AnalysisInterrupted):
        _structured_observation_windows(text, question, 1000, lambda: True)
    captured = []
    def generate(prompt):
        payload = json.loads(prompt);captured.append(payload)
        parts = payload['sources']
        return {'claims': [], 'facts': [
            {'fact_id': 'prior', 'label': 'Revenue', 'value': '100', 'unit': 'USD', 'currency': 'USD', 'period': 'FY2022', 'citation': {'source_id': 's', 'quotation': parts[1]['text']}},
            {'fact_id': 'current', 'label': 'Revenue', 'value': '120', 'unit': 'USD', 'currency': 'USD', 'period': 'FY2023', 'citation': {'source_id': 's', 'quotation': parts[2]['text']}}],
            'calculations': [{'formula': 'growth_percent', 'first_fact_id': 'current', 'second_fact_id': 'prior'}]}, 5
    result = analyze(question, [source('s', text=text)], None, generate, max_prompt_chars=2055)
    assert len(captured[0]['sources']) == 3 and result.prompt_bytes <= 2055
    assert result.calculations[0]['value'] == '20.00'
    assert all(text[f['location']['character_start']:f['location']['character_end']] == f['quotation'] for f in result.facts)


def test_explicit_currency_conflict_does_not_enter_calculation():
    text = 'Revenue in 2023 was EUR 100. Revenue in 2022 was EUR 80.'
    quote1, quote2 = 'EUR 100', 'EUR 80'
    raw = {'facts': [
        {'fact_id': 'new', 'label': 'Revenue', 'value': '100', 'unit': 'million', 'currency': 'USD', 'period': '2023', 'citation': {'source_id': 's', 'quotation': quote1}},
        {'fact_id': 'old', 'label': 'Revenue', 'value': '80', 'unit': 'million', 'currency': 'USD', 'period': '2022', 'citation': {'source_id': 's', 'quotation': quote2}}],
        'calculations': [{'formula': 'growth_percent', 'first_fact_id': 'new', 'second_fact_id': 'old'}]}
    result = analyze('Compare revenue.', [source('s', text=text)], None, lambda _: (raw, 1))
    assert all(f['currency_conflict_unverified'] for f in result.facts)
    assert result.calculations == []
    assert any('currency conflicts' in note for note in result.limitations)


def test_comparison_prefers_each_labeled_release_without_trusting_title_as_fact():
    q = '比较第二次估计与第三次估计的实际GDP及修订方向。'
    second = ('Other information. ' * 20 + 'Real GDP increased at an annual rate of 2.8 percent in the third quarter, according to the "second" estimate. ' +
              'Other material. ' * 30 + 'In the third estimate, the prior 2.9 percent was revised. ' + 'Other material. ' * 200)
    third = ('Other information. ' * 20 + 'Real GDP increased at an annual rate of 3.1 percent in the third quarter, according to the "third" estimate. ' +
             'Other material. ' * 30 + 'In the second estimate, the increase in real GDP was 2.8 percent. ' * 5 + 'Other material. ' * 200)
    sources = [source('s1', text=second).model_copy(update={'title': 'GDP second estimate'}),
               source('s2', text=third).model_copy(update={'title': 'GDP third estimate'})]
    captured = []
    result = analyze(q, sources, None, lambda prompt: (captured.append(json.loads(prompt)) or {}, 1), max_prompt_chars=2055)
    excerpts = captured[0]['sources']
    assert any(part['source_id'] == 's1' and '2.8 percent' in part['text'] for part in excerpts)
    assert any(part['source_id'] == 's2' and '3.1 percent' in part['text'] for part in excerpts)
    assert result.prompt_bytes <= 2055
    assert all(next(s for s in sources if s.source_id == part['source_id']).text[seg['character_start']:seg['character_end']] == part['text']
               for seg, part in zip(result.segments, excerpts))


def test_policy_factor_selection_covers_decision_and_considerations():
    text = ('Other policy material. ' * 60 +
            'The Committee decided to maintain the target range for the federal funds rate at 5-1/4 to 5-1/2 percent. ' +
            'Other policy details. ' * 15 +
            'The Committee will take into account the cumulative tightening of monetary policy, the lags with which policy affects the economy, and economic and financial developments. ' +
            'Other text. ' * 140 +
            'The Desk will maintain the federal funds rate in a target range. ' * 10)
    prompt = []
    result = analyze('决议目标利率区间及后续政策考虑因素是什么？', [source('s', text=text)], None,
                     lambda q: (prompt.append(json.loads(q)) or {}, 1), max_prompt_chars=2055)
    combined = ' '.join(part['text'] for part in prompt[0]['sources'])
    assert 'decided to maintain the target range' in combined
    assert 'cumulative tightening' in combined and 'lags' in combined and 'economic and financial developments' in combined
    assert result.prompt_bytes <= 2055


def test_revision_comparison_preserves_both_levels_and_opposing_drivers_within_budget():
    from datetime import datetime, timezone
    q = '比较第二次估计与第三次估计的GDP，说明修订原因和抵消因素。'
    first_text = ('Other context. ' * 20 + 'Real gross domestic product (GDP) increased at an annual rate of 2.8 percent in the third quarter of 2024, according to the "second" estimate released by the U.S. Bureau of Economic Analysis. ' + 'Other information. ' * 200)
    third_text = ('Other context. ' * 20 + 'Real gross domestic product (GDP) increased at an annual rate of 3.1 percent in the third quarter of 2024, according to the "third" estimate released by the U.S. Bureau of Economic Analysis. ' +
        'Other preliminary information. ' * 15 + 'The update primarily reflected upward revisions to exports and consumer spending that were partly offset by a downward revision to private inventory investment. ' + 'Other information. ' * 200)
    sources = [source('second', text=first_text).model_copy(update={'title':'GDP second estimate', 'published_at':datetime(2024, 11, 27, tzinfo=timezone.utc)}),
               source('third', text=third_text).model_copy(update={'title':'GDP third estimate', 'published_at':datetime(2024, 12, 19, tzinfo=timezone.utc)})]
    captured = []
    result = analyze(q, sources, None, lambda prompt: (captured.append(json.loads(prompt)) or {}, 1), max_prompt_chars=2055)
    parts = captured[0]['sources']
    assert any(p['source_id'] == 'second' and '2.8 percent' in p['text'] and 'GDP' in p['text'] for p in parts)
    assert any(p['source_id'] == 'third' and '3.1 percent' in p['text'] and 'third' in p['text'] for p in parts)
    assert any(p['source_id'] == 'third' and 'upward revisions' in p['text'] and 'downward revision' in p['text'] for p in parts)
    assert result.prompt_bytes <= 2055
    for segment, part in zip(result.segments, parts):
        original = next(s.text for s in sources if s.source_id == segment['source_id'])
        assert original[segment['character_start']:segment['character_end']] == part['text']
    assert any('remaining text was not examined' in note for note in result.limitations)


def test_revision_excerpts_require_opposing_drivers_and_retain_context():
    from app.research_analysis import _revision_summary_windows
    q = 'Explain the GDP revision and offsetting factors.'
    text = ('Real gross domestic product (GDP) increased at an annual rate of 3.1 percent, according to the "third" estimate released by the U.S. Bureau of Economic Analysis. '
            'The update primarily reflected upward revisions to exports that were partly offset by a downward revision to inventories. Further remarks.')
    parts = _revision_summary_windows(text, q, 1000)
    assert parts and len(parts) == 2 and '3.1 percent' in parts[0][1] and 'third" estimate' in parts[0][1]
    assert parts[1][1].endswith('inventories.')
    assert all(text[start:start + len(part)] == part for start, part in parts)
    assert _revision_summary_windows(text, q, 30) is None
    assert _revision_summary_windows(text.replace('partly offset', 'unrelated'), q, 1000) is None
    assert _revision_summary_windows(text.replace('downward revision', 'upward revision'), q, 1000) is None
    assert _revision_summary_windows(text.replace('that were partly offset', '. Other information was partly offset'), q, 1000) is None
    assert _revision_summary_windows(text, 'Explain annual GDP.', 1000) is None


def test_percent_marked_hyphenated_range_is_source_checked_and_currency_guarded():
    from app.research_analysis import _rate_bounds
    assert _rate_bounds('5.25%-5.50%') == (Decimal('5.25'), Decimal('5.50'))
    assert _rate_bounds('-1%--0.5%') == (Decimal('-1'), Decimal('-0.5'))
    assert _rate_bounds('5-1/4% - 5-1/2%') == (Decimal('5.25'), Decimal('5.5'))
    assert _rate_bounds('5.25-5.50') is None
    assert _rate_bounds('5.50%-5.25%') is None
    assert _rate_bounds('5.25%-5.50') is None
    text = 'The Committee decided to maintain the target range at 5-1/4 to 5-1/2 percent.'
    report = source('s', text=text)
    fact = {'fact_id': 'target', 'label': 'target', 'value': '5.25%-5.50%', 'unit': 'percent',
            'currency': 'USD', 'period': '2023', 'citation': {'source_id':'s', 'quotation':'5-1/4 to 5-1/2 percent'}}
    raw = {'facts':[fact], 'calculations':[{'formula':'difference', 'first_fact_id':'target:upper', 'second_fact_id':'target:lower'}]}
    result = analyze('What is the target?', [report], None, lambda _: (raw, 1))
    assert [f['value'] for f in result.facts] == ['5.25', '5.50']
    assert all(f['currency_context_unverified'] for f in result.facts) and not result.calculations
    raw['facts'][0]['citation']['quotation'] = '5-1/4'
    assert [f['value'] for f in analyze('What is the target?', [report], None, lambda _: (raw, 1)).facts] == ['5.25']


def test_pure_absence_of_counterargument_is_a_limitation_not_counterevidence():
    text = 'Official estimate: GDP grew 3.1 percent.'
    report = source('s', text=text)
    citation = {'source_id': 's', 'quotation': 'Official estimate'}
    raw = {'claims': [{'text':'GDP grew 3.1 percent.', 'citations':[{'source_id':'s', 'quotation':'GDP grew 3.1 percent'}]}],
           'counterarguments': [{'text':'无来源提供反方观点；文本为官方估计，不含对修订本身的质疑。', 'citations':[citation]},
                                {'text':'No counterarguments were provided in these excerpts.', 'citations':[citation]}]}
    result = analyze('Compare GDP.', [report], None, lambda _: (raw, 1))
    assert len(result.claims) == 1 and not result.counterarguments
    assert any('not counterevidence' in note for note in result.limitations)
    assert any('无来源提供反方观点' in note and 'not independently verified' in note for note in result.limitations)
    assert any('No counterarguments were provided in these excerpts' in note for note in result.limitations)


def test_mixed_counterargument_absence_does_not_discard_opposing_evidence():
    from app.research_analysis import _counterargument_absence_only
    assert _counterargument_absence_only('未发现反证。')
    assert not _counterargument_absence_only('未发现反证，但库存投资下降。')
    assert not _counterargument_absence_only('No counterarguments, but inventory fell.')
    assert not _counterargument_absence_only('没有反方观点，但是支出下降了 3%。')
    assert not _counterargument_absence_only('反方指出库存投资下修。')


def test_model_growth_with_different_period_labels_is_explicitly_flagged():
    text = 'Annual revenue was 100. Quarterly revenue was 80.'
    raw = {'facts': [
        {'fact_id':'annual', 'label':'Annual revenue', 'value':'100', 'unit':'USD', 'currency':'USD', 'period':'FY2024', 'citation':{'source_id':'s', 'quotation':'Annual revenue was 100'}},
        {'fact_id':'quarter', 'label':'Quarter revenue', 'value':'80', 'unit':'USD', 'currency':'USD', 'period':'2023-Q4', 'citation':{'source_id':'s', 'quotation':'Quarterly revenue was 80'}}],
        'calculations':[{'formula':'growth_percent', 'first_fact_id':'annual', 'second_fact_id':'quarter'}]}
    result = analyze('Compare revenue.', [source('s', text=text)], None, lambda _:(raw,1))
    assert len(result.facts) == 2 and len(result.calculations) == 1
    assert result.calculations[0]['period_granularity_check'] == 'different_labels_verify_stock_or_flow'
    assert any('Period comparability is not established' in note for note in result.limitations)


def test_financial_metric_table_selects_matching_single_metric_and_nearby_units():
    # The earlier unit belongs to a different table and must not displace the
    # unit explicitly shown below this table's year columns.
    older = '| Previous balance sheet |\n| (in billions) |\n| Other | 700 | 600 |\n' + '| other | 1 | 2 |\n' * 10
    header = '| Year Ended |\n| January 29, 2024 | January 30, 2023 |\n| (in millions) |\n'
    rows = ('| Net cash provided by operating activities | 120 | 100 |\n'
            '| Operating income (loss) | 50 | 40 |\n'
            '| Net income | 30 | 25 |\n'
            '| Gross profit | 70 | 60 |\n'
            '| Revenue | 200 | 190 |\n')
    text = 'Other information. ' * 50 + older + header + rows + 'Other information. ' * 50
    for question, expected in [
        ('对比2024年和2023年经营活动现金流', 'Net cash provided by operating activities'),
        ('比较2024年和2023年营业利润', 'Operating income (loss)'),
        ('Compare 2024 and 2023 net income', 'Net income'),
        ('比较2024和2023年毛利润', 'Gross profit'),
    ]:
        parts = _financial_table_windows(text, question, 500)
        assert parts
        selected = ''.join(part for _, part in parts)
        assert expected in selected and '2024' in selected and '2023' in selected and 'millions' in selected
        assert all(text[start:start+len(part)] == part for start, part in parts)
    cash = _financial_table_windows(text, '对比2024年和2023年经营活动现金流', 500)
    assert cash and 'billions' not in ''.join(part for _, part in cash)
    assert _financial_table_windows(text, '比较2024和2023年的营收及净利润', 500) is None
    assert _financial_table_windows(text, '比较2024和2023年的净利润率', 500) is None
    assert _financial_table_windows(text, 'Compare 2024 Q1 and 2023 Q1 net income', 500) is None
    assert _financial_table_windows(text, '比较2024年和2023年第一季度的经营现金流', 500) is None
    separated = '| Year Ended |\n| 2024 | 2023 |\nUnrelated discussion.\n| (in millions) |\n| Net income | 30 | 25 |\n'
    assert _financial_table_windows(separated, 'Compare 2024 and 2023 net income', 500) is None
    distant_unit = '| (in millions) |\n' + '| unrelated row | 1 | 2 |\n' * 20 + '| Year Ended | 2024 | 2023 |\n| Net income | 30 | 25 |\n'
    assert _financial_table_windows(distant_unit, 'Compare 2024 and 2023 net income', 1000) is None


def test_generated_table_markup_expands_only_to_a_complete_original_row():
    from app.research_analysis import _table_markup_rows
    row = '| [colspan=3] Net cash provided by operating activities | $ | 5,641 | | [colspan=3] | $ | 9,108 |'
    text = 'Lead text.\n' + row + '\nEnd.'
    quotation = 'Net cash provided by operating activities | $ | 5,641 | | $ | 9,108'
    start = text.index(row)
    assert _table_markup_rows(text, [(0, text)], quotation) == [(start, start + len(row))]
    assert _table_markup_rows(text, [(0, text)], quotation.replace('5,641', '5,642')) == []
    assert _table_markup_rows(text, [(0, text)], quotation.replace(' | $ | 5,641', '')) == []
    assert _table_markup_rows(text, [(start + 1, text[start+1:])], quotation) == []
    assert _table_markup_rows(text, [(0, text[:start + len(row) - 2])], quotation) == []
    assert _table_markup_rows(text.replace('[colspan=3]', ''), [(0, text.replace('[colspan=3]', ''))], quotation) == []
    with pytest.raises(AnalysisInterrupted):
        _table_markup_rows(text, [(0, text)], quotation, lambda: True)
    result = analyze('Review cash flow.', [source('s', text=text)], None, lambda _: ({
        'claims':[{'text':'Cash flow figures were reported.', 'citations':[{'source_id':'s','quotation':quotation}]}],
        'facts':[{'fact_id':'cash','label':'cash','value':'5641','unit':'USD million','currency':'USD','period':'2023',
                  'citation':{'source_id':'s','quotation':quotation}}]}, 1))
    assert len(result.claims) == 1 and result.claims[0]['support_status'] == 'incomplete'
    citation = result.claims[0]['citations'][0]
    assert citation['quotation_match'] == 'table_markup_expanded' and citation['quotation'] == row
    assert text[citation['location']['character_start']:citation['location']['character_end']] == row
    assert not result.facts
    assert any('column alignment' in note for note in result.limitations)


def test_recovered_table_rows_with_multiple_matches_remain_ambiguous():
    row = '| [colspan=3] Revenue | $ | 100 | | [colspan=3] | $ | 90 |'
    text = row + '\n' + row + '\n'
    quote = 'Revenue | $ | 100 | | $ | 90'
    result = analyze('Review revenue.', [source('s', text=text)], None,
        lambda _: ({'claims':[{'text':'Revenue was reported.', 'citations':[{'source_id':'s','quotation':quote}]}]}, 1))
    assert result.claims[0]['support_status'] == 'incomplete'
    loc = result.claims[0]['citations'][0]['location']
    assert loc['status'] == 'ambiguous' and len(loc['matches']) == 2


def test_currency_warning_for_quotation_inside_table_row():
    text = '| [colspan=3] Revenue | $ | 100 | 90 |\nRevenue was $100.'
    raw = {'facts': [
        {'fact_id':'table', 'label':'Revenue', 'value':'100', 'unit':'million', 'currency':'USD', 'period':'2024', 'citation':{'source_id':'s','quotation':'Revenue | $ | 100'}},
        {'fact_id':'prose', 'label':'Revenue', 'value':'100', 'unit':'million', 'currency':'USD', 'period':'2024', 'citation':{'source_id':'s','quotation':'Revenue was $100'}}]}
    result = analyze('Review revenue.', [source('s', text=text)], None, lambda _: (raw, 1))
    assert result.facts[0]['currency_quote_missing'] is True
    assert 'currency_quote_missing' not in result.facts[1]


def test_table_fact_and_calculation_require_column_alignment_review():
    text = '| Year Ended | 2024 | 2023 |\n| Revenue | USD 100 | USD 90 |\nCash was USD 80.'
    raw = {'facts': [
        {'fact_id':'a', 'label':'Revenue', 'value':'100', 'unit':'million', 'currency':'USD', 'period':'2024', 'citation':{'source_id':'s','quotation':'Revenue | USD 100'}},
        {'fact_id':'b', 'label':'Revenue', 'value':'90', 'unit':'million', 'currency':'USD', 'period':'2023', 'citation':{'source_id':'s','quotation':'USD 90'}},
        {'fact_id':'c', 'label':'Cash', 'value':'80', 'unit':'million', 'currency':'USD', 'period':'2024', 'citation':{'source_id':'s','quotation':'Cash was USD 80'}}],
        'calculations': [{'formula':'growth_percent', 'first_fact_id':'a', 'second_fact_id':'b'}]}
    result = analyze('Compare revenue.', [source('s', text=text)], None, lambda _: (raw, 1))
    assert [item.get('table_alignment_unverified', False) for item in result.facts] == [True, True, False]
    assert result.calculations[0]['value'] == '11.11'
    assert any('Table column alignment is not established' in note for note in result.limitations)


def test_spanned_table_year_conflict_does_not_feed_automatic_calculation():
    text = ('| [colspan=3] | [colspan=9] Year Ended |\n'
            '| [colspan=3] | [colspan=3] January 29, 2023 | [colspan=3] | [colspan=3] January 30, 2022 |\n'
            '| [colspan=3] | [colspan=9] (In millions) |\n'
            '| [colspan=3] Operating cash | $ | 5,641 | | [colspan=3] | $ | 9,108 | |\n')
    raw = {'facts': [
        {'fact_id':'a', 'label':'Operating cash', 'value':'5641', 'unit':'million', 'currency':'USD', 'period':'Year Ended January 30, 2022', 'citation':{'source_id':'s','quotation':'Operating cash | $ | 5,641'}},
        {'fact_id':'b', 'label':'Operating cash', 'value':'9108', 'unit':'million', 'currency':'USD', 'period':'Year Ended January 29, 2023', 'citation':{'source_id':'s','quotation':'9,108'}}],
        'calculations':[{'formula':'growth_percent', 'first_fact_id':'a', 'second_fact_id':'b'}]}
    result = analyze('Compare operating cash.', [source('s', text=text)], None, lambda _: (raw, 1))
    assert [f['table_period_conflict']['column_year'] for f in result.facts] == ['2023','2022']
    assert not result.calculations
    assert any('excluded from automatic calculations' in note for note in result.limitations)


def test_date_and_unit_observations_are_source_located_claims_not_calculation_inputs():
    text = '| January 29, 2023 | January 30, 2022 |\n| (In millions) |\nRevenue was 100.'
    def observation(fid, label, value, unit, quotation):
        return {'fact_id':fid, 'label':label, 'value':value, 'unit':unit, 'currency':None, 'period':'Year Ended', 'citation':{'source_id':'s','quotation':quotation}}
    raw = {'facts': [
        observation('date','Period end','January 29, 2023','date','January 29, 2023'),
        observation('unit','Units','In millions','unit declaration','(In millions)'),
        observation('bad-date','Bad date','February 30, 2023','date','February 30, 2023'),
        observation('uncited','Other date','January 30, 2022','date','January 29, 2023'),
        observation('extended','Misleading year','January 29, 2023','date','January 29, 20230'),
        observation('invented','Other date','January 29, 2023','date','Invented January 29, 2023'),
        {'fact_id':'revenue','label':'Revenue','value':'100','unit':'million','currency':'USD','period':'2023','citation':{'source_id':'s','quotation':'Revenue was 100'}}],
        'calculations':[{'formula':'difference','first_fact_id':'date','second_fact_id':'revenue'}]}
    result = analyze('Review reporting details.', [source('s', text=text)], None, lambda _: (raw,1))
    assert [claim['text'] for claim in result.claims] == ['Period end: January 29, 2023','Units: In millions']
    assert all(c['kind'] == 'model_interpretation' and c['support_status'] == 'not_independently_verified' for c in result.claims)
    assert len(result.facts) == 1 and result.facts[0]['fact_id'] == 'revenue'
    assert not result.calculations
    assert any('retained as unverified claims' in note for note in result.limitations)
    assert any('fact(s) were omitted' in note for note in result.limitations)
    assert any('unlocatable' in note or 'did not match' in note for note in result.limitations)


def test_single_release_revision_comparison_keeps_both_estimates_and_drivers():
    from app.research_analysis import _revision_summary_windows
    text = ('Intro. Real gross domestic product (GDP) increased at an annual rate of 3.4 percent in the fourth quarter of 2023, according to the "third" estimate. '
            'The previous data are older. In the second estimate, the increase in real GDP was 3.2 percent. '
            'The update primarily reflected upward revisions to consumer spending and nonresidential fixed investment that were partly offset by a downward revision to private inventory investment. Tail.')
    question = '比较第三次和第二次估计的GDP年化增长率，说明上修来源及抵消因素。'
    parts = _revision_summary_windows(text, question, 1000)
    assert parts and len(parts) == 3
    selected = ' '.join(segment for _, segment in parts)
    assert all(term in selected for term in ('3.4 percent','3.2 percent','consumer spending','nonresidential fixed investment','private inventory investment'))
    assert all(text[start:start + len(segment)] == segment for start,segment in parts)
    assert _revision_summary_windows(text, '比较2022年第三次和第二次估计的GDP年化增长率，说明上修来源及抵消因素。', 1000) is None
    assert _revision_summary_windows(text, '比较2023年第三季度第三次和第二次估计的GDP，说明上修及抵消因素。', 1000) is None
    assert _revision_summary_windows(text, question, sum(len(part) for _,part in parts)-1) is None
    same_direction = text.replace('downward revision', 'upward revision')
    assert _revision_summary_windows(same_direction, question, 1000) is None
    no_compare = _revision_summary_windows(text, '说明GDP修订原因和抵消因素。', 1000)
    assert no_compare and len(no_compare) == 2


def test_policy_rate_purchase_risk_windows_preserve_condition_and_bounded_offsets():
    from app.research_analysis import _policy_rate_purchase_risk_windows
    text = ('Unrelated. ' * 100 + 'Risks to the economic outlook remain. ' + 'Unrelated. ' * 40 +
            'The Committee decided to keep the target range for the federal funds rate at 0 to 1/4 percent and expects more. ' +
            'Unrelated. ' * 20 +
            'The Committee indicated that it would continue to increase its holdings of Treasury securities by at least $80 billion per month and of agency mortgage-backed securities by at least $40 billion per month until substantial further progress has been made toward its maximum employment and price stability goals. ' + 'End. ' * 100)
    question = '利率目标区间、国债与抵押贷款证券购买金额，以及经济风险是什么？'
    parts = _policy_rate_purchase_risk_windows(text, question, 1000)
    assert parts and len(parts) == 3
    combined = ' '.join(part for _,part in parts)
    assert '0 to 1/4 percent' in combined and '$80 billion' in combined and '$40 billion' in combined
    assert 'Risks to the economic outlook remain' in combined and 'until substantial further progress' in combined
    assert all(text[start:start + len(part)] == part for start,part in parts)
    assert _policy_rate_purchase_risk_windows(text, question, sum(len(part) for _,part in parts)-1) is None
    specific = text.replace('Risks to the economic outlook remain.', 'The path of the economy continues to depend on the course of the virus. Risks to the economic outlook remain.')
    specific_parts = _policy_rate_purchase_risk_windows(specific, question, 1000)
    assert specific_parts and any('course of the virus' in part for _,part in specific_parts)
    assert _policy_rate_purchase_risk_windows(text.replace('by at least $40','by $40'), question, 1000) is None
    assert _policy_rate_purchase_risk_windows(text, '利率目标区间是什么？', 1000) is None
    with pytest.raises(AnalysisInterrupted):
        _policy_rate_purchase_risk_windows(text, question, 1000, lambda: True)


def test_pure_evidence_gap_is_a_limitation_not_a_substantive_counterargument():
    from app.research_analysis import _counterargument_evidence_gap_only
    for text in ("所提供的材料未包含未来风险的详细信息。", "The provided excerpts do not include future guidance.", "Available sources lack risk details."):
        assert _counterargument_evidence_gap_only(text)
    for text in ("材料未提供风险，但库存下降。", "Sources do not include forecasts, but costs increased.", "公司没有提供预测。", "库存下降。", "材料未包含财务数据而销售下降。", "Sources do not include forecasts and revenue fell."):
        assert not _counterargument_evidence_gap_only(text)
    report = source("s", text="Revenue increased.")
    citation = {"source_id": "s", "quotation": "Revenue increased."}
    raw = {"claims": [{"text": "Revenue increased.", "citations": [citation]}],
           "counterarguments": [{"text": "The provided excerpts do not include future guidance.", "citations": [citation]}]}
    result = analyze("Assess revenue.", [report], None, lambda _: (raw, 1))
    assert not result.counterarguments
    assert any("future guidance" in note for note in result.limitations)


def test_larger_research_prompt_budget_can_include_complete_source_without_changing_text():
    text = "Revenue context. " * 240
    report = source("large", text=text)
    small_prompts, large_prompts = [], []
    analyze("Revenue?", [report], None, lambda prompt: (small_prompts.append(json.loads(prompt)) or {}, 0), max_prompt_chars=2000)
    analyze("Revenue?", [report], None, lambda prompt: (large_prompts.append(json.loads(prompt)) or {}, 0), max_prompt_chars=6000)
    assert all(part["source_coverage"] == "partial" for part in small_prompts[0]["sources"])
    assert len(large_prompts[0]["sources"]) == 1
    assert large_prompts[0]["sources"][0]["source_coverage"] == "complete"
    assert large_prompts[0]["sources"][0]["text"] == text
