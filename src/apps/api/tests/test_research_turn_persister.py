"""Unit tests for research_turn_persister: turn assembly, coverage gaps, isolation handoff, usage, and optimistic locking."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.operations import ApiProblem
from app.research_calculations import CalculationRequest, calculate, calculation_input_status
from app.research_tables import CreateTableRequest, build_research_table
from app.research_turns import merge_turn
from app.research_lifecycle import ResearchVersionChain
from app.research_runs import ResearchRunResult
from app.research_sources import ResearchSource, SourcePermissions
from app.research_topics import (
    ReadRange,
    ResearchContent,
    ResearchInput,
    ResearchTopics,
    ResearchTurn,
    ResearchVersion,
    TopicNotFound,
    TopicVersionConflict,
    TurnUsage,
)
from app.research_turn_persister import (
    ResearchTurnPersister,
    build_research_turn,
    build_turn_usage,
    commit_research_version,
    compute_turn_gaps,
    extract_read_ranges,
    extract_source_candidates,
    has_partial_read_coverage,
    is_source_fully_read,
    record_failed_checkpoint_version,
    resolve_search_status,
)
from app.storage import InMemoryDocumentRepository


def _make_source(
    source_id: str,
    text: str,
    *,
    is_simulated: bool = False,
    model_input: bool = True,
    display: bool = True,
    published_at: datetime | None = None,
) -> ResearchSource:
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    return ResearchSource(
        source_id=source_id,
        owner_id="acct-1",
        title=f"Title {source_id}",
        text=text,
        kind="user_supplied",
        is_simulated=is_simulated,
        published_at=published_at,
        obtained_at=now,
        content_sha256=sha256(text.encode("utf-8")).hexdigest(),
        permissions=SourcePermissions(
            store=True,
            model_input=model_input,
            display=display,
            export=True,
        ),
    )


def _make_outcome(**overrides) -> SimpleNamespace:
    defaults = {
        "status": "completed",
        "reason": "submitted",
        "report": "Revenue was 100 USD.",
        "partial_notes": None,
        "question_parts": [],
        "stopping_note": "answered",
        "claims": [],
        "facts": [],
        "calculations": [],
        "charts": [],
        "tables": [],
        "gaps": [],
        "source_ids": [],
        "events": [],
        "reconciliation": None,
        "coverage_map": None,
        "method_packs": [],
        "attributions": [],
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_turn_meta(**overrides) -> dict:
    started_at = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    defaults = {
        "turn_id": "turn-1",
        "index": 1,
        "question": "What was revenue?",
        "question_origin": "user",
        "focus_citation": None,
        "operation_ids": [],
        "parent_version_id": "v0",
        "started_at": started_at.isoformat(),
    }
    defaults.update(overrides)
    return defaults


def _sample_fact(fact_id: str, value: str, period: str, quote: str, start: int, source_id: str = "src-1") -> dict:
    return {
        "fact_id": fact_id,
        "label": "revenue",
        "value": value,
        "unit": "USD",
        "currency": "USD",
        "period": period,
        "source_id": source_id,
        "quotation": quote,
        "character_start": start,
        "character_end": start + len(quote),
        "location": {"character_start": start, "character_end": start + len(quote)},
        "verification": "exact_quotation_match",
    }


def test_build_turn_usage_complete_vs_partial_unknown() -> None:
    zero_calls = build_turn_usage(model_calls=0, search_calls=0, usage=None, accounted_tokens=0)
    assert zero_calls == TurnUsage(model_calls=0, search_calls=0, total_tokens=0, usage_status="complete")

    known = build_turn_usage(model_calls=2, search_calls=1, usage=350, accounted_tokens=400)
    assert known == TurnUsage(model_calls=2, search_calls=1, total_tokens=400, usage_status="complete")

    known_higher_usage = build_turn_usage(model_calls=2, search_calls=0, usage=500, accounted_tokens=300)
    assert known_higher_usage == TurnUsage(model_calls=2, search_calls=0, total_tokens=500, usage_status="complete")

    partial_unknown = build_turn_usage(model_calls=3, search_calls=2, usage=None, accounted_tokens=1200)
    assert partial_unknown == TurnUsage(
        model_calls=3,
        search_calls=2,
        total_tokens=1200,
        usage_status="partial_unknown",
    )

    partial_none_accounted = build_turn_usage(model_calls=1, search_calls=0, usage=None, accounted_tokens=None)
    assert partial_none_accounted == TurnUsage(
        model_calls=1,
        search_calls=0,
        total_tokens=0,
        usage_status="partial_unknown",
    )


def test_read_range_coverage_gap_detection_across_turns_and_eligibility() -> None:
    # Span sweep-line cases
    assert is_source_fully_read([], 0) is True
    assert is_source_fully_read([], 10) is False
    assert is_source_fully_read([(1, 10)], 10) is False
    assert is_source_fully_read([(0, 4), (5, 10)], 10) is False
    assert is_source_fully_read([(5, 10), (0, 5)], 10) is True
    assert is_source_fully_read([(0, 7), (4, 10)], 10) is True

    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    s_eligible = _make_source("src-1", "A" * 100, published_at=now - timedelta(days=1))
    s_simulated = _make_source("src-sim", "Simulated text", is_simulated=True, published_at=now - timedelta(days=1))
    s_no_model = _make_source("src-restricted", "Restricted text", model_input=False, published_at=now - timedelta(days=1))
    s_future = _make_source("src-future", "Future text", published_at=now + timedelta(days=5))
    s_undated = _make_source("src-undated", "Undated text", published_at=None)

    # Prior turn read [0, 60); current turn reads [60, 100) -> full coverage; ineligible sources ignored.
    prior_turn = ResearchTurn(
        turn_id="turn-0",
        index=1,
        question="Earlier?",
        question_origin="user",
        operation_ids=["op-0"],
        parent_version_id="v0",
        started_at=now,
        finished_at=now,
        status="completed",
        reason="submitted",
        report="Earlier report.",
        usage=TurnUsage(model_calls=1, search_calls=0, total_tokens=10, usage_status="complete"),
        search_status="not_requested",
        official_fetch_status="not_requested",
        read_ranges=[ReadRange(source_id="src-1", character_start=0, character_end=60)],
    )
    sources = [s_eligible, s_simulated, s_no_model, s_future, s_undated]

    # Partial current range [60, 90) leaves [90, 100) unread -> source_selection_partial emitted.
    partial_gaps = compute_turn_gaps(
        outcome_gaps=[{"code": "model_reported", "message": "Limited data."}],
        sources=sources,
        as_of=now,
        current_ranges=[{"source_id": "src-1", "character_start": 60, "character_end": 90}],
        prior_turns=[prior_turn],
    )
    assert [g["code"] for g in partial_gaps] == ["model_reported", "source_selection_partial"]

    # Completing [60, 100) across turns clears source_selection_partial (undated is availability_unestablished at as_of).
    assert (
        has_partial_read_coverage(
            sources=sources,
            as_of=now,
            current_ranges=[{"source_id": "src-1", "character_start": 60, "character_end": 100}],
            prior_turns=[prior_turn],
        )
        is False
    )
    # Without as_of, undated source becomes eligible and unread -> partial coverage is True.
    assert (
        has_partial_read_coverage(
            sources=[s_eligible, s_undated],
            as_of=None,
            current_ranges=[{"source_id": "src-1", "character_start": 60, "character_end": 100}],
            prior_turns=[prior_turn],
        )
        is True
    )
    complete_gaps = compute_turn_gaps(
        outcome_gaps=[{"code": "model_reported", "message": "Limited data."}],
        sources=sources,
        as_of=now,
        current_ranges=[{"source_id": "src-1", "character_start": 60, "character_end": 100}],
        prior_turns=[prior_turn],
    )
    assert complete_gaps == [{"code": "model_reported", "message": "Limited data."}]


def test_extract_candidates_ranges_and_search_status() -> None:
    runtime_state = {
        "leads": [
            {"title": "Valid Lead " + ("x" * 600), "url": "https://example.com/filing", "published_at": "2026-11-01"},
            {"title": "Userinfo Rejected", "url": "https://user:pass@example.com/secret"},
            {"title": "Scheme Rejected", "url": "javascript:alert(1)"},
            {"title": "Malformed Rejected", "url": "https://[::1"},
            {"title": "Missing URL"},
            "not-a-dict",
        ],
        "read_ranges": [{"source_id": "src-1", "start": 0, "end": 42}],
    }
    candidates = extract_source_candidates(runtime_state)
    assert len(candidates) == 1
    assert candidates[0]["url"] == "https://example.com/filing"
    assert candidates[0]["status"] == "unverified"
    assert candidates[0]["published_at"] == "2026-11-01"
    assert len(candidates[0]["title"]) == 500

    assert extract_read_ranges(runtime_state) == [
        {"source_id": "src-1", "character_start": 0, "character_end": 42}
    ]
    assert extract_read_ranges({}) == []

    assert resolve_search_status([], default_status="not_requested") == "not_requested"
    assert (
        resolve_search_status(
            [
                {"name": "news_search", "status": "completed", "reason": None},
                {"name": "other_tool", "status": "completed"},
                {"name": "news_search", "status": "unavailable", "reason": "search_budget_exhausted"},
            ],
            default_status="not_requested",
        )
        == "search_budget_exhausted"
    )
    assert (
        resolve_search_status(
            [{"name": "news_search", "status": "completed", "reason": ""}],
            default_status="unavailable",
        )
        == "completed"
    )


def test_build_research_turn_and_persister_finalize_with_merge_turn_isolation() -> None:
    text = "Revenue was 100 USD. Revenue was 80 USD."
    src = _make_source("src-1", text)
    f1 = _sample_fact("fact-2025", "100", "2025", "Revenue was 100 USD.", 0)
    f2 = _sample_fact("fact-2024", "80", "2024", "Revenue was 80 USD.", 21)
    valid_calc = {
        "calculation_id": "calc-valid",
        **calculate(
            CalculationRequest(
                formula="growth_percent",
                first={k: f1[k] for k in ("fact_id", "value", "unit", "currency", "period")},
                second={k: f2[k] for k in ("fact_id", "value", "unit", "currency", "period")},
            )
        ),
    }
    valid_calc["input_status"] = calculation_input_status(valid_calc, [f1, f2])
    invalid_calc = {**valid_calc, "calculation_id": "calc-bad", "formula_id": "DRIFTED_UNKNOWN_FORMULA"}
    claim = {
        "claim_id": "claim-1",
        "role": "support",
        "text": "Revenue rose 25%.",
        "citations": [
            {
                "source_id": "src-1",
                "quotation": "Revenue was 100 USD.",
                "location": {"character_start": 0, "character_end": 20},
            }
        ],
        "challenges_claim_ids": [],
    }

    table = build_research_table(CreateTableRequest.model_validate({
        "title": "Revenue", "columns": [{"key": "amount", "label": "Revenue"}],
        "rows": [{"label": "FY2025", "cells": {"amount": {"kind": "fact", "fact_id": "fact-2025"}}}],
        "rationale": "Compare revenue",
    }), facts=[f1, f2], calculations=[], sources={"src-1": src}).model_dump(mode="json")
    outcome = _make_outcome(
        report=f"Revenue.\n\n[[table:{table['table_id']}]]",
        tables=[table],
        method_packs=[{"pack_id": "market_trend", "version": "1.2.0", "fingerprint": "abc", "activated_mid_run": True}],
        claims=[claim],
        facts=[f1, f2],
        calculations=[valid_calc, invalid_calc],
        charts=[],
        source_ids=["src-1"],
        events=[{"kind": "tool", "name": "news_search", "status": "completed"}],
        stopping_note="answered",
    )
    finished_at = datetime(2026, 11, 20, 12, 1, tzinfo=timezone.utc)
    turn_meta = _make_turn_meta(operation_ids=["op-prev"], parent_version_id="v0")

    # Direct build_research_turn verification
    built_turn = build_research_turn(
        turn_meta=turn_meta,
        operation_id="op-2",
        finished_at=finished_at,
        outcome=outcome,
        gaps=[{"code": "source_selection_partial", "message": "Partial coverage."}],
        usage=TurnUsage(model_calls=2, search_calls=1, total_tokens=200, usage_status="complete"),
        search_status="completed",
        official_fetch_status="not_requested",
        read_ranges=[{"source_id": "src-1", "character_start": 0, "character_end": 20}],
    )
    assert built_turn.operation_ids == ["op-prev", "op-2"]
    assert built_turn.claim_ids == ["claim-1"]
    assert built_turn.fact_ids == ["fact-2025", "fact-2024"]
    assert built_turn.calculation_ids == ["calc-valid", "calc-bad"]
    assert built_turn.method_packs[0].activated_mid_run is True
    assert built_turn.finished_at == finished_at

    # Persister finalize_turn hands off to real merge_turn -> isolates invalid_calc while keeping valid items.
    topics = ResearchTopics(InMemoryDocumentRepository())
    topic = topics.create("acct-1", ResearchInput(question="What was revenue?", source_ids=["src-1"]))
    v0 = topic.versions[-1]
    version_chain = ResearchVersionChain(topics, "acct-1", topic.topic_id, v0.version_id, ["src-1"])
    mutable_sources = [src]
    persister = ResearchTurnPersister(
        topic_id=topic.topic_id,
        parent_version_id=v0.version_id,
        as_of=None,
        sources=mutable_sources,
        version_chain=version_chain,
        turn_meta={**turn_meta, "parent_version_id": v0.version_id},
    )
    # Simulate a source discovered/added during run_research_agent and a partial read range on src-1
    runtime_state = {
        "leads": [{"title": "Filing", "url": "https://ir.example.com/10k"}],
        "read_ranges": [{"source_id": "src-1", "start": 0, "end": 20}],
    }
    ctx = SimpleNamespace(calls=2, search_calls=1, usage=None, accounted_tokens=450)
    run_result = persister.finalize_turn(
        operation_id="op-2",
        parent_version=v0,
        outcome=outcome,
        runtime_state=runtime_state,
        context=ctx,
        finished_at=finished_at,
        default_search_status="not_requested",
        official_fetch_status="not_requested",
    )

    assert isinstance(run_result, ResearchRunResult)
    assert run_result.operation_id == "op-2"
    assert run_result.topic_id == topic.topic_id
    assert run_result.parent_version_id == v0.version_id
    assert run_result.status == "completed"
    assert version_chain.source_ids == ["src-1"]

    saved_turn = run_result.content.turns[-1]
    assert saved_turn.usage == TurnUsage(
        model_calls=2,
        search_calls=1,
        total_tokens=450,
        usage_status="partial_unknown",
    )
    assert saved_turn.search_status == "completed"
    assert saved_turn.table_ids == [table["table_id"]]
    reopened = ResearchContent.model_validate_json(run_result.content.model_dump_json())
    assert reopened.tables[0].rows[0].cells["amount"].provenance.fact_id == "fact-2025"
    assert reopened.turns[-1].report.endswith(f"[[table:{table['table_id']}]]")
    resumed = merge_turn(reopened, saved_turn, claims=[], facts=[], calculations=[], charts=[], tables=[table], sources=[], source_candidates=[])
    assert len(resumed.tables) == 1
    assert resumed.turns[-1].table_ids == [table["table_id"]]
    assert saved_turn.calculation_ids == ["calc-valid"]
    assert [c.calculation_id for c in run_result.content.calculations] == ["calc-valid"]
    assert [f.fact_id for f in run_result.content.facts] == ["fact-2025", "fact-2024"]
    assert [c.claim_id for c in run_result.content.claims] == ["claim-1"]
    assert [c["url"] for c in run_result.content.source_candidates] == ["https://ir.example.com/10k"]
    gap_codes = [g.code for g in saved_turn.gaps]
    assert "source_selection_partial" in gap_codes
    assert "calculation_persistence_dropped" in gap_codes

    # A reported answer that specifically relied on the rejected calculation must be removed.
    outcome.question_parts = [
        {"id": "part-value", "question": "What was revenue?", "status": "answered", "reason": "",
         "answer": "100 USD", "references": [{"fact_id": "fact-2025"}]},
        {"id": "part-growth", "question": "How did it change?", "status": "answered", "reason": "",
         "answer": "Reported growth", "references": [{"calculation_id": "calc-bad"}]},
    ]
    revised = persister.finalize_turn(
        operation_id="op-3", parent_version=v0, outcome=outcome, runtime_state=runtime_state,
        context=ctx, finished_at=finished_at, default_search_status="not_requested",
        official_fetch_status="not_requested",
    )
    assert revised.status == revised.content.turns[-1].status == "partial"
    valid_answer, missing_answer = revised.content.turns[-1].question_parts
    assert valid_answer.answer == "100 USD"
    assert missing_answer.status == "unresolved" and not missing_answer.answer and not missing_answer.references
    assert not revised.content.turns[-1].report



def test_persister_commit_handles_optimistic_lock_conflict_and_unexpected_error() -> None:
    topics = ResearchTopics(InMemoryDocumentRepository())
    topic = topics.create("acct-1", ResearchInput(question="What was revenue?"))
    v0 = topic.versions[-1]

    # 1. Successful commit advances version_chain
    chain_ok = ResearchVersionChain(topics, "acct-1", topic.topic_id, v0.version_id, [])
    run_inputs = InMemoryDocumentRepository()
    run_inputs.put("op-ok", {"runtime_checkpoint": {"checkpoint_schema_version": 2}})
    persister_ok = ResearchTurnPersister(
        topic_id=topic.topic_id,
        parent_version_id=v0.version_id,
        as_of=None,
        sources=[],
        version_chain=chain_ok,
        turn_meta=_make_turn_meta(parent_version_id=v0.version_id),
        run_inputs=run_inputs,
    )
    result_ok = ResearchRunResult(
        operation_id="op-ok",
        topic_id=topic.topic_id,
        parent_version_id=v0.version_id,
        status="completed",
        content=ResearchContent(summary="Saved"),
    )
    saved_v1 = persister_ok.commit_result(operation_id="op-ok", result=result_ok, runtime_state={})
    assert isinstance(saved_v1, ResearchVersion)
    assert chain_ok.parent_version_id == saved_v1.version_id
    assert "failed_checkpoint_schema_version" not in run_inputs.get("op-ok")

    # 2. Stale parent_version_id triggers TopicVersionConflict -> ApiProblem(409, "lifecycle_conflict", ...)
    stale_chain = ResearchVersionChain(topics, "acct-1", topic.topic_id, v0.version_id, [])
    run_inputs.put(
        "op-conflict",
        {"runtime_checkpoint": {"checkpoint_schema_version": 2}},
    )
    persister_conflict = ResearchTurnPersister(
        topic_id=topic.topic_id,
        parent_version_id=v0.version_id,
        as_of=None,
        sources=[],
        version_chain=stale_chain,
        turn_meta=_make_turn_meta(parent_version_id=v0.version_id),
        run_inputs=run_inputs,
    )
    with pytest.raises(ApiProblem) as conflict_exc:
        persister_conflict.commit_result(
            operation_id="op-conflict",
            result=result_ok.model_copy(update={"operation_id": "op-conflict"}),
            runtime_state={"checkpoint_schema_version": 2},
        )
    assert conflict_exc.value.status_code == 409
    assert conflict_exc.value.error_code == "lifecycle_conflict"
    assert "Research changed before the result was saved." in conflict_exc.value.message
    # Optimistic lock conflicts must not mark checkpoint as failed local schema version
    assert "failed_checkpoint_schema_version" not in run_inputs.get("op-conflict")

    # 3. Direct TopicNotFound also maps to ApiProblem(409, "lifecycle_conflict", ...)
    missing_chain = MagicMock(spec=ResearchVersionChain)
    missing_chain.current.side_effect = TopicNotFound()
    with pytest.raises(ApiProblem) as not_found_exc:
        commit_research_version(missing_chain, ResearchContent())
    assert not_found_exc.value.status_code == 409
    assert not_found_exc.value.error_code == "lifecycle_conflict"

    # 4. Unexpected non-ApiProblem error during save records failed_checkpoint_schema_version and re-raises
    broken_chain = MagicMock(spec=ResearchVersionChain)
    broken_chain.current.return_value = saved_v1
    broken_chain.save.side_effect = RuntimeError("disk write failed")
    run_inputs.put(
        "op-err",
        {
            "checkpoint_migrated": True,
            "checkpoint_repaired": True,
            "runtime_checkpoint": {
                "checkpoint_schema_version": 3,
                "checkpoint_migrated": True,
                "checkpoint_repaired": True,
            },
        },
    )
    persister_err = ResearchTurnPersister(
        topic_id=topic.topic_id,
        parent_version_id=saved_v1.version_id,
        as_of=None,
        sources=[],
        version_chain=broken_chain,
        turn_meta=_make_turn_meta(parent_version_id=saved_v1.version_id),
        run_inputs=run_inputs,
    )
    with pytest.raises(RuntimeError, match="disk write failed"):
        persister_err.commit_result(
            operation_id="op-err",
            result=result_ok.model_copy(update={"operation_id": "op-err"}),
            runtime_state={"checkpoint_schema_version": 3},
        )
    updated = run_inputs.get("op-err")
    assert updated["failed_checkpoint_schema_version"] == 3
    assert "checkpoint_migrated" not in updated
    assert "checkpoint_repaired" not in updated
    assert "checkpoint_migrated" not in updated["runtime_checkpoint"]
    assert "checkpoint_repaired" not in updated["runtime_checkpoint"]


def test_record_failed_checkpoint_version_handles_missing_and_fallback_state() -> None:
    repo = InMemoryDocumentRepository()
    record_failed_checkpoint_version(repo, "missing-op", {"checkpoint_schema_version": 5})
    assert repo.get("missing-op") is None

    repo.put("op-no-cp", {"topic_id": "t1"})
    record_failed_checkpoint_version(repo, "op-no-cp", {"checkpoint_schema_version": 4})
    assert repo.get("op-no-cp")["failed_checkpoint_schema_version"] == 4

    repo.put("op-none-ver", {"topic_id": "t1"})
    record_failed_checkpoint_version(repo, "op-none-ver", {})
    assert repo.get("op-none-ver")["failed_checkpoint_schema_version"] == 0


def test_persister_tracks_in_place_acquired_sources_and_continuation_turn_merge() -> None:
    src_initial = _make_source("src-1", "Initial text.")
    src_acquired = _make_source("src-2", "Acquired filing text.")
    topics = ResearchTopics(InMemoryDocumentRepository())
    topic = topics.create("acct-1", ResearchInput(question="Revenue?", source_ids=["src-1"]))
    v0 = topic.versions[-1]

    # First partial attempt saved as v1
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    mutable_sources = [src_initial]
    chain1 = ResearchVersionChain(topics, "acct-1", topic.topic_id, v0.version_id, ["src-1"])
    turn_meta = _make_turn_meta(turn_id="turn-1", parent_version_id=v0.version_id)
    p1 = ResearchTurnPersister(
        topic_id=topic.topic_id,
        parent_version_id=v0.version_id,
        as_of=None,
        sources=mutable_sources,
        version_chain=chain1,
        turn_meta=turn_meta,
    )
    res1 = p1.finalize_turn(
        operation_id="op-1",
        parent_version=v0,
        outcome=_make_outcome(status="partial", reason="provider_timeout", report="", source_ids=["src-1"]),
        runtime_state={"read_ranges": [{"source_id": "src-1", "start": 0, "end": len(src_initial.text)}]},
        context=SimpleNamespace(calls=1, search_calls=0, usage=100, accounted_tokens=100),
        finished_at=now,
        default_search_status="not_requested",
        official_fetch_status="not_requested",
    )
    v1 = p1.commit_result(operation_id="op-1", result=res1, runtime_state={})

    # Continuation starts from v1 while keeping original turn_meta (parent_version_id=v0, turn_id="turn-1")
    # and acquires src_acquired into the shared mutable_sources list during execution.
    chain2 = ResearchVersionChain(topics, "acct-1", topic.topic_id, v1.version_id, list(v1.source_ids))
    p2 = ResearchTurnPersister(
        topic_id=topic.topic_id,
        parent_version_id=v1.version_id,
        as_of=None,
        sources=mutable_sources,
        version_chain=chain2,
        turn_meta={**turn_meta, "operation_ids": ["op-1"]},
    )
    mutable_sources.append(src_acquired)
    res2 = p2.finalize_turn(
        operation_id="op-2",
        parent_version=v1,
        outcome=_make_outcome(status="completed", reason="submitted", report="Done.", source_ids=["src-1", "src-2"]),
        runtime_state={
            "read_ranges": [{"source_id": "src-2", "start": 0, "end": len(src_acquired.text)}],
        },
        context=SimpleNamespace(calls=2, search_calls=1, usage=250, accounted_tokens=250),
        finished_at=now + timedelta(minutes=1),
        default_search_status="not_requested",
        official_fetch_status="completed",
    )
    assert res2.parent_version_id == v1.version_id
    assert chain2.source_ids == ["src-1", "src-2"]
    assert len(res2.content.turns) == 1
    merged_turn = res2.content.turns[0]
    assert merged_turn.parent_version_id == v0.version_id
    assert merged_turn.operation_ids == ["op-1", "op-2"]
    assert merged_turn.status == "completed"
    assert merged_turn.usage == TurnUsage(model_calls=2, search_calls=1, total_tokens=250, usage_status="complete")
    assert [row["source_id"] for row in res2.content.sources] == ["src-1", "src-2"]
    assert "source_selection_partial" not in {g.code for g in merged_turn.gaps}



def test_partial_question_components_are_persisted_and_reopened_with_turn():
    components = [
        {'question': 'Revenue', 'status': 'answered', 'answer': 'Reported revenue was 100 USD.', 'reason': '',
         'references': [{'fact_id': 'f-prior', 'origin': 'prior'}]},
        {'question': 'entry timing', 'status': 'unresolved', 'answer': '',
         'reason': 'Current price and entry criteria are missing.', 'references': []},
    ]
    turn = build_research_turn(
        turn_meta=_make_turn_meta(question='Revenue and entry timing?'), operation_id='op-1',
        finished_at=datetime(2026, 11, 20, 12, 1, tzinfo=timezone.utc),
        outcome=_make_outcome(status='partial', reason='provider_timeout', report='', question_parts=components),
        gaps=[], usage=TurnUsage(model_calls=1, search_calls=0, total_tokens=1),
        search_status='not_requested', official_fetch_status='not_requested', read_ranges=[],
    )
    reopened = ResearchContent.model_validate_json(ResearchContent(turns=[turn]).model_dump_json())
    restored = reopened.turns[0].question_parts
    assert restored[0].answer == components[0]["answer"]
    assert restored[0].references[0].fact_id == "f-prior"
    assert restored[0].references[0].origin == "prior"
    assert restored[1].reason == components[1]["reason"]
    assert reopened.turns[0].question == 'Revenue and entry timing?'
