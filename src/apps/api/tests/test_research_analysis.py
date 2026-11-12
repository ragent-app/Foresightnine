from datetime import datetime, timezone
import json

import pytest

from app.research_analysis import AnalysisInterrupted, InvalidAnalysis, analyze, _select_window, _quoted_numbers
from decimal import Decimal
from app.research_sources import ResearchSource, SourcePermissions


def source(source_id, *, text="Revenue was 100 million.", url=None, model_input=True,
           display=True, published_at=None):
    return ResearchSource(source_id=source_id, owner_id="owner", title="Report", text=text,
        url=url, kind="user_supplied", obtained_at=datetime.now(timezone.utc),
        published_at=published_at, content_sha256="test",
        permissions=SourcePermissions(store=True, model_input=model_input, display=display))


def test_fractional_rate_notation_is_converted_exactly():
    values = _quoted_numbers("lower by 1/2 percentage point to 4-3/4 to 5 percent")
    assert Decimal("0.5") in values and Decimal("4.75") in values and Decimal("5") in values
    assert Decimal("3") not in values and Decimal("4") not in values
    assert _quoted_numbers("-1/2") == {Decimal("-0.5")}
    assert _quoted_numbers("- 1/2") == {Decimal("-0.5")}
    assert Decimal("0.3333333333333333333333333333") not in _quoted_numbers("1/3")


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


def test_rejects_fabricated_citations_and_respects_display_permission():
    hidden = source("hidden", display=False)
    visible = source("hidden", display=True)
    def model(quote):
        return lambda _: ({"claims": [{"text": "Claim", "citations": [
            {"source_id": "hidden", "quotation": quote}]}],
            "counterarguments": [], "limitations": []}, None)
    with pytest.raises(InvalidAnalysis):
        analyze("Question", [visible], None, model("invented"))
    result = analyze("Question", [hidden], None, model("100 million"))
    assert result.claims == []
    assert any("display permission" in item for item in result.limitations)


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
    with pytest.raises(InvalidAnalysis):
        analyze("GDP 2023", [report], None, generate("GDP increased 3.4 percent in the fourth quarter of 2023"))


def test_future_or_unknown_publication_is_not_sent():
    cutoff = datetime(2023, 1, 1, tzinfo=timezone.utc)
    later = source("later", published_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
    unknown = source("unknown")
    def must_not_call(_):
        raise AssertionError("No eligible source should reach the model")
    result = analyze("Question", [later, unknown], cutoff, must_not_call)
    assert result.source_ids == []
    assert len(result.limitations) == 3


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
    with pytest.raises(InvalidAnalysis, match="absent"):
        analyze("Revenue growth?", [report], None, fabricated)


def test_negative_parenthetical_value_is_not_treated_as_positive():
    report = source("s", text="Loss was (1,000) million.")
    def model(_prompt):
        return {"facts": [{"fact_id": "loss", "label": "Loss", "value": "1000", "unit": "USD million",
                 "currency": "USD", "period": "FY2023", "citation": {"source_id": "s", "quotation": "Loss was (1,000) million."}}]}, None
    with pytest.raises(InvalidAnalysis):
        analyze("Loss?", [report], None, model)


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
    with pytest.raises(InvalidAnalysis):
        analyze("2023 revenue and profit", [report], None, model, max_prompt_chars=5000)


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


@pytest.mark.parametrize("url", ["https://fred.stlouisfed.org./series/GDP",
    "https://alfred.stlouisfed.org/series/GDP", "https://api.stlouisfed.org/fred/series/observations"])
def test_fred_endpoint_variants_are_not_sent_to_models(url):
    def must_not_call(_):
        raise AssertionError("FRED is not an authorized model input")
    result = analyze("GDP?", [source("fred", url=url)], None, must_not_call)
    assert result.source_ids == []
