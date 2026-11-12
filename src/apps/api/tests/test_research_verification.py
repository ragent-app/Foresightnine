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
    assert excerpt["text"] == "Before. Revenue 100. After."
    assert excerpt["quotation"] == "Revenue 100"
    assert excerpt["text"][excerpt["quotation_start"]:excerpt["quotation_end"]] == excerpt["quotation"]
    assert "not as a substitute for an unquoted fact" in REVIEW_SYSTEM_PROMPT
    result = apply_support_review([claim()], batch, {"reviews": [{"claim_index": 0, "verdict": "supports"}]})
    assert result[0]["support_status"] == "not_independently_verified"
    assert result[0]["support_review"]["independently_verified"] is False
    assert result[0]["support_review"]["method"] == REVIEW_METHOD == "bounded_quotation_context_model_review_v5"
    assert len(batch.prompt_sha256) == 64


@pytest.mark.parametrize("item,src", [
    (claim(), source(model=False)), (claim(), source(display=False)),
    (claim(quotation="invented"), source()),
    ({**claim(), "citations": [{**claim()["citations"][0], "partial_token": True}]}, source()),
    (claim(location={"status": "ambiguous", "matches": []}), source()),
    ({**claim(), "support_status": "incomplete"}, source()),
    ({**claim(), "uncited_amounts": ["100万美元"]}, source()),
    ({"text": "No citation", "citations": []}, source()),
])
def test_review_omits_unauthorized_unlocated_or_unsupported_claims(item, src):
    batch = prepare_support_review([item], [src], 3000)
    assert batch.indexes == [] and batch.omitted == [0]
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


def test_review_reduces_context_to_fit_without_truncating_the_quote():
    report = source().model_copy(update={"text": "x" * 200 + "Revenue 100" + "y" * 200})
    item = claim(location={"character_start": 200, "character_end": 211})
    batch = prepare_support_review([item], [report], 350)
    assert batch.indexes == [0] and len(batch.prompt.encode()) <= 350
    assert batch.segments[0]["context_radius"] < 180
    excerpt = json.loads(batch.prompt)["claims"][0]["excerpts"][0]
    assert excerpt["text"][excerpt["quotation_start"]:excerpt["quotation_end"]] == "Revenue 100"


def test_review_reserves_budget_for_multiple_claims_before_extra_context():
    report = source().model_copy(update={"text": "x" * 200 + "Revenue 100" + "y" * 200})
    item = claim(location={"character_start": 200, "character_end": 211})
    batch = prepare_support_review([item, item, item], [report], 850)
    assert batch.indexes == [0, 1, 2] and not batch.omitted
    assert len(batch.prompt.encode("utf-8")) <= 850
    assert any(segment["context_radius"] < 180 for segment in batch.segments)
    for entry in json.loads(batch.prompt)["claims"]:
        excerpt = entry["excerpts"][0]
        assert excerpt["text"][excerpt["quotation_start"]:excerpt["quotation_end"]] == "Revenue 100"


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
    assert "Personal saving rate" in excerpt["text"]
    assert excerpt["text"][excerpt["quotation_start"]:excerpt["quotation_end"]] == quotation
    assert batch.segments[0]["required_context_start"] == 0
    assert report.text[batch.segments[0]["character_start"]:batch.segments[0]["character_end"]] == excerpt["text"]
    neutral = report.model_copy(update={"text": text.replace("Personal saving rate", "x" * len("Personal saving rate"))})
    # Exact quote alone fits, but the identified subject context does not.
    for budget in range(200, 700):
        if prepare_support_review([item], [neutral], budget).indexes and not prepare_support_review([item], [report], budget).indexes:
            break
    else:
        pytest.fail("expected a budget that admits only the context-free quote")
    limited = prepare_support_review([item], [report], budget)
    assert limited.indexes == [] and limited.omitted == [0]
    assert "Personal saving rate" not in limited.prompt


def test_review_ignores_caller_supplied_context_warning_and_excludes_simulation():
    item = {**claim(), "citation_context_warnings": [{"citation_index": 0, "context_character_start": 0}]}
    batch = prepare_support_review([item], [source()], 3000)
    assert batch.indexes == [0] and "required_context_start" not in batch.segments[0]
    simulated = source().model_copy(update={"is_simulated": True})
    blocked = prepare_support_review([item], [simulated], 3000)
    assert not blocked.indexes and blocked.omitted == [0]
