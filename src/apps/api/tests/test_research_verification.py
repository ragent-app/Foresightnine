from datetime import datetime, timezone
import json

import pytest

from app.research_sources import ResearchSource, SourcePermissions
from app.research_verification import InvalidReview, REVIEW_METHOD, REVIEW_SYSTEM_PROMPT, apply_support_review, prepare_support_review


def source(source_id="s", *, model=True, display=True):
    return ResearchSource(source_id=source_id, owner_id="owner", title="Report", text="Before. Revenue 100. After.",
        kind="user_supplied", obtained_at=datetime.now(timezone.utc), content_sha256="hash",
        permissions=SourcePermissions(store=True, model_input=model, display=display))


def claim(source_id="s", quotation="Revenue 100", location=None):
    return {"text": "Revenue was 100", "citations": [{"source_id": source_id, "quotation": quotation,
        "location": location or {"character_start": 8, "character_end": 19}}], "support_status": "not_independently_verified"}


def test_review_uses_authorized_exact_excerpts_and_keeps_verdict_distinct_from_verification():
    batch = prepare_support_review([claim()], [source()], 3000)
    assert batch.indexes == [0] and not batch.omitted
    payload = json.loads(batch.prompt)
    excerpt = payload["claims"][0]["excerpts"][0]
    assert excerpt["preceding_context"] + excerpt["quotation"] + excerpt["following_context"] == "Before. Revenue 100. After."
    assert excerpt["quotation"] == "Revenue 100"
    assert excerpt["quotation"] == "Revenue 100"
    assert "not as a substitute for an unquoted fact" in REVIEW_SYSTEM_PROMPT
    result = apply_support_review([claim()], batch, {"reviews": [{"claim_index": 0, "verdict": "supports"}]})
    assert result[0]["support_status"] == "not_independently_verified"
    assert result[0]["support_review"]["independently_verified"] is False
    assert result[0]["support_review"]["method"] == REVIEW_METHOD == "bounded_quotation_context_model_review_v15"
    assert len(batch.prompt_sha256) == 64


@pytest.mark.parametrize("item,src", [
    (claim(), source(model=False)), (claim(), source(display=False)),
    (claim(quotation="invented"), source()),
    ({**claim(), "citations": [{**claim()["citations"][0], "partial_token": True}]}, source()),
    (claim(location={"status": "ambiguous", "matches": []}), source()),
    ({**claim(), "support_status": "incomplete"}, source()),
    ({**claim(), "uncited_amounts": ["100万美元"]}, source()),
    ({**claim(), "uncited_fact_values": ["100 million"]}, source()),
    ({**claim(), "numeric_discrepancy_for_review": True}, source()),
    ({"text": "No citation", "citations": []}, source()),
])
def test_review_omits_unauthorized_unlocated_or_unsupported_claims(item, src):
    batch = prepare_support_review([item], [src], 3000)
    assert batch.indexes == [] and batch.omitted == [0]
    assert batch.omissions == [{"claim_index": 0, "reason": "not_reviewable"}]
    assert "Revenue 100" not in batch.prompt


def test_review_enforces_budget_and_exact_result_coverage():
    batch = prepare_support_review([claim(), claim()], [source()], 3000)
    for bad in ({"reviews": [{"claim_index": 0, "verdict": "supports"}]},
                {"reviews": [{"claim_index": 0, "verdict": "supports"}, {"claim_index": 0, "verdict": "supports"}]},
                {"reviews": [{"claim_index": 0, "verdict": "supports"}, {"claim_index": 1, "verdict": "invented"}]},
                {"reviews": [{"claim_index": True, "verdict": "supports"}, {"claim_index": 1, "verdict": "supports"}]}):
        with pytest.raises(InvalidReview):
            apply_support_review([claim(), claim()], batch, bad)
    limited = prepare_support_review([claim()], [source()], 100)
    assert not limited.indexes and limited.omitted == [0]
    assert limited.omissions == [{"claim_index": 0, "reason": "input_budget", "required_subject_context": False}]


def test_review_reduces_context_to_fit_without_truncating_the_quote():
    report = source().model_copy(update={"text": "x" * 200 + "Revenue 100" + "y" * 200})
    item = claim(location={"character_start": 200, "character_end": 211})
    batch = prepare_support_review([item], [report], 350)
    assert batch.indexes == [0] and len(batch.prompt.encode()) <= 350
    assert batch.segments[0]["context_radius"] < 180
    excerpt = json.loads(batch.prompt)["claims"][0]["excerpts"][0]
    assert excerpt["quotation"] == "Revenue 100"


def test_review_reserves_budget_for_multiple_claims_before_extra_context():
    report = source().model_copy(update={"text": "x" * 200 + "Revenue 100" + "y" * 200})
    item = claim(location={"character_start": 200, "character_end": 211})
    batch = prepare_support_review([item, item, item], [report], 850)
    assert batch.indexes == [0, 1, 2] and not batch.omitted
    assert len(batch.prompt.encode("utf-8")) <= 850
    assert any(segment["context_radius"] < 180 for segment in batch.segments)
    for entry in json.loads(batch.prompt)["claims"]:
        excerpt = entry["excerpts"][0]
        assert excerpt["quotation"] == "Revenue 100"


def test_review_preserves_required_subject_context_or_omits_claim():
    text = "Personal saving rate was 2.7 percent in the third quarter, a downward revision of 0.1 percentage point."
    quotation = "quarter, a downward revision of 0.1 percentage point."
    start = text.index(quotation)
    item = {"text": "GDP was revised downward", "citations": [{"source_id": "s", "quotation": quotation,
        "location": {"character_start": start, "character_end": start + len(quotation)}}]}
    report = source().model_copy(update={"text": text})
    batch = prepare_support_review([item], [report], 2000)
    assert batch.indexes == [0] and not batch.omitted
    excerpt = json.loads(batch.prompt)["claims"][0]["excerpts"][0]
    assert "Personal saving rate" in excerpt["preceding_context"]
    assert excerpt["quotation"] == quotation
    assert batch.segments[0]["required_context_start"] == 0
    assert report.text[batch.segments[0]["character_start"]:batch.segments[0]["character_end"]] == excerpt["preceding_context"] + excerpt["quotation"] + excerpt["following_context"]
    neutral = report.model_copy(update={"text": text.replace("Personal saving rate", "x" * len("Personal saving rate"))})
    # Exact quote alone fits, but the identified subject context does not.
    for budget in range(200, 700):
        if prepare_support_review([item], [neutral], budget).indexes and not prepare_support_review([item], [report], budget).indexes:
            break
    else:
        pytest.fail("expected a budget that admits only the context-free quote")
    limited = prepare_support_review([item], [report], budget)
    assert limited.indexes == [] and limited.omitted == [0]
    assert limited.omissions == [{"claim_index": 0, "reason": "input_budget", "required_subject_context": True}]
    assert "Personal saving rate" not in limited.prompt


def test_review_ignores_caller_supplied_context_warning_and_excludes_simulation():
    item = {**claim(), "citation_context_warnings": [{"citation_index": 0, "context_character_start": 0}]}
    batch = prepare_support_review([item], [source()], 3000)
    assert batch.indexes == [0] and "required_context_start" not in batch.segments[0]
    simulated = source().model_copy(update={"is_simulated": True})
    blocked = prepare_support_review([item], [simulated], 3000)
    assert not blocked.indexes and blocked.omitted == [0]


def test_review_keeps_required_table_header_or_omits_the_claim():
    text = ("ACME Corp\nStatements of Operations\n(in millions)\n"
            "| Years Ended | 2024 | 2023 |\n" + "| Item | 10 | 20 |\n" * 10 + "| Revenue | 100 | 90 |")
    quote = "| Revenue | 100 | 90 |"
    start = text.index(quote)
    report = source().model_copy(update={"text": text})
    item = claim(quotation=quote, location={"character_start": start, "character_end": start + len(quote)})
    batch = prepare_support_review([item], [report], 2000)
    assert batch.indexes == [0]
    excerpt = json.loads(batch.prompt)["claims"][0]["excerpts"][0]
    assert "Years Ended" in excerpt["preceding_context"] and "millions" in excerpt["preceding_context"]
    assert excerpt["quotation"] == quote
    neutral = report.model_copy(update={"text": text.replace("Years Ended", "xxxxxxxxxxx")})
    for budget in range(180, 1200):
        if prepare_support_review([item], [neutral], budget).indexes and not prepare_support_review([item], [report], budget).indexes:
            break
    else:
        pytest.fail("expected the required table context to exceed some budget")
    limited = prepare_support_review([item], [report], budget)
    assert limited.omissions == [{"claim_index": 0, "reason": "input_budget", "required_subject_context": True}]


def test_review_preserves_following_context_for_unfinished_quoted_phrase():
    quote = "Consumer spending and nonresidential"
    text = quote + " fixed investment increased. Other context."
    report = source().model_copy(update={"text": text})
    item = {"text": "Consumer spending and investment increased", "citations": [{"source_id": "s", "quotation": quote,
        "location": {"character_start": 0, "character_end": len(quote)}}]}
    batch = prepare_support_review([item], [report], 2000)
    excerpt = json.loads(batch.prompt)["claims"][0]["excerpts"][0]
    assert batch.indexes == [0] and excerpt["quotation"] == quote
    assert "fixed investment increased" in excerpt["following_context"]
    assert batch.segments[0]["required_context_end"] > len(quote)
    neutral = report.model_copy(update={"text": quote + ". Other context."})
    for budget in range(180, 700):
        if prepare_support_review([item], [neutral], budget).indexes and not prepare_support_review([item], [report], budget).indexes:
            break
    else:
        pytest.fail("expected the required continuation to exceed some budget")
    limited = prepare_support_review([item], [report], budget)
    assert limited.omissions == [{"claim_index": 0, "reason": "input_budget", "required_subject_context": True}]


def test_review_keeps_metric_prefix_and_qualifier_when_quotation_splits_subject():
    text = 'Other prices increased. Excluding food and energy, the PCE price index increased 0.3 percent.'
    quotation = 'index increased 0.3 percent.'
    start = text.index(quotation)
    report = source().model_copy(update={'text': text})
    item = claim(quotation=quotation, location={'character_start': start, 'character_end': start + len(quotation)})
    batch = prepare_support_review([item], [report], 3000)
    assert batch.indexes == [0]
    excerpt = json.loads(batch.prompt)['claims'][0]['excerpts'][0]
    assert 'Excluding food and energy, the PCE price ' in excerpt['preceding_context']
    assert excerpt['quotation'] == quotation
    required = batch.segments[0]
    assert text[required['required_context_start']:start].strip() == 'Excluding food and energy, the PCE price'
    complete = 'the PCE price index increased 0.3 percent.'
    full_start = text.index(complete)
    complete_item = claim(quotation=complete, location={'character_start': full_start, 'character_end': full_start + len(complete)})
    full_batch = prepare_support_review([complete_item], [report], 3000)
    assert 'required_context_start' not in full_batch.segments[0]


def test_review_does_not_accept_unquoted_basis_point_figures():
    item = {**claim(), 'text': 'Rates increased by 25 basis points'}
    batch = prepare_support_review([item], [source()], 3000)
    assert batch.indexes == [] and batch.omissions[0]['reason'] == 'not_reviewable'


def test_review_keeps_relative_clause_antecedent_with_verbatim_quote():
    text = 'Other update. Imports, which are a subtraction in GDP, were revised down.'
    quotation = 'which are a subtraction in GDP, were revised down.'
    start = text.index(quotation)
    report = source().model_copy(update={'text':text})
    item = {'text':'Inventory was subtracted from GDP.', 'citations':[{'source_id':'s', 'quotation':quotation,
        'location':{'character_start':start,'character_end':start+len(quotation)}}]}
    batch = prepare_support_review([item], [report], 2000)
    assert batch.indexes == [0]
    excerpt = json.loads(batch.prompt)['claims'][0]['excerpts'][0]
    assert 'Imports,' in excerpt['preceding_context'] and excerpt['quotation'] == quotation
    assert report.text[batch.segments[0]['required_context_start']:start].strip() == 'Imports,'


def test_review_rechecks_usd_amount_without_fact_or_trusting_saved_warning():
    item = {**claim(), "text": "Revenue was 100万美元", "uncited_claim_amounts": []}
    batch = prepare_support_review([item], [source()], 3000)
    assert batch.indexes == [] and batch.omitted == [0]


def test_review_does_not_clear_percentage_missing_from_quotations():
    item = {**claim(), "text": "Revenue increased 12 percent", "uncited_percentages": []}
    batch = prepare_support_review([item], [source()], 3000)
    assert batch.indexes == [] and batch.omitted == [0]
    present = {**claim(), "text": "Revenue increased 100 percent", "uncited_percentages": ["stale"]}
    assert prepare_support_review([present], [source()], 3000).indexes == [0]


def test_review_omits_uncited_percentage_point_amount():
    item = {**claim(), "text": "Revenue increased 0.2 percentage points"}
    batch = prepare_support_review([item], [source()], 3000)
    assert batch.indexes == [] and batch.omitted == [0]



def test_review_omits_claim_when_related_sentence_is_unquoted():
    text = "Officials may adjust policy. They monitor labor market conditions and inflation pressures."
    quotation = "Officials may adjust policy."
    item = {"text":"Officials may adjust policy and monitor labor market conditions.",
            "citations":[{"source_id":"s","quotation":quotation,"location":{"character_start":0,"character_end":len(quotation)}}]}
    report = source().model_copy(update={"text":text})
    batch = prepare_support_review([item],[report],2000)
    assert not batch.indexes and batch.omitted == [0]
    assert batch.omissions[0]["reason"] == "not_reviewable"
    assert "labor market conditions" not in batch.prompt


def test_subject_and_related_context_remain_visible_together():
    from app.research_evidence import flag_citation_subject_context
    text = "Personal saving rate declined. Officials monitor labor market conditions."
    quotation = "declined."
    start = text.index(quotation)
    item = {"text":"GDP fell and labor market conditions are monitored.", "citations":[
        {"source_id":"s", "quotation":quotation, "location":{"character_start":start,"character_end":start+len(quotation)}}]}
    report = source().model_copy(update={"text":text})
    warnings = flag_citation_subject_context([item],[report])[0]["citation_context_warnings"]
    assert {w["context_metric"] for w in warnings} == {"personal saving rate", "related unquoted sentence"}
    assert prepare_support_review([item],[report],2000).omitted == [0]


def test_review_retains_pronoun_antecedent_as_context_not_evidence():
    text = "Last December, the Committee indicated that it would continue to buy securities."
    quote = "it would continue to buy securities."
    start = text.index(quote)
    item = {"text":"The Committee would continue to buy securities.","citations":[
        {"source_id":"s","quotation":quote,"location":{"character_start":start,"character_end":start+len(quote)}}]}
    report = source().model_copy(update={"text":text})
    batch = prepare_support_review([item],[report],2000)
    assert batch.indexes == [0]
    excerpt = json.loads(batch.prompt)["claims"][0]["excerpts"][0]
    assert "Last December, the Committee indicated that" in excerpt["preceding_context"]
    assert excerpt["quotation"] == quote
    assert "not as a substitute for an unquoted fact" in REVIEW_SYSTEM_PROMPT


def test_review_preserves_amount_subject_as_context_not_quotation():
    text = "The total deficit was $68.9 billion in February, up $1.3 billion from $67.6 billion in January, revised."
    quotation = "$67.6 billion in January, revised."
    start = text.index(quotation)
    item = {"text":"The January deficit was $67.6 billion.", "citations":[
        {"source_id":"s", "quotation":quotation, "location":{"character_start":start,"character_end":start+len(quotation)}}]}
    report = source().model_copy(update={"text":text})
    batch = prepare_support_review([item],[report],2000)
    assert batch.indexes == [0]
    excerpt = json.loads(batch.prompt)["claims"][0]["excerpts"][0]
    assert "total deficit" in excerpt["preceding_context"]
    assert excerpt["quotation"] == quotation
    assert "not as a substitute for an unquoted fact" in REVIEW_SYSTEM_PROMPT


def test_review_omits_dollar_amount_missing_from_exact_quote():
    item={**claim(),"text":"Revenue rose $7.1 billion", "uncited_dollar_amounts":[]}
    batch=prepare_support_review([item],[source()],3000)
    assert not batch.indexes and batch.omitted==[0]
