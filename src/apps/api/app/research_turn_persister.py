"""Turn finalization and optimistic-lock persistence for research runs."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from .operations import ApiProblem
from .research_lifecycle import ResearchVersionChain
from .research_runs import ResearchRunResult
from .research_source_policy import source_eligibility, summarize_sources
from .research_sources import ResearchSource
from .research_topics import (
    ReadRange,
    ResearchAttempt,
    ResearchContent,
    ResearchTurn,
    ResearchVersion,
    SavedMarketAnalysis,
    TopicNotFound,
    TopicVersionConflict,
    TurnUsage,
)
from .research_turns import gap_rows, merge_turn


def _default_runtime_gap(code: str, message: str | None = None) -> dict[str, Any]:
    from .research_runtime import gap as runtime_gap

    return runtime_gap(code, message)


def extract_source_candidates(runtime_state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract sanitized unverified HTTP/HTTPS lead candidates from runtime state."""
    candidates: list[dict[str, Any]] = []
    for lead in runtime_state.get("leads") or ():
        if not isinstance(lead, Mapping) or not isinstance(lead.get("url"), str):
            continue
        try:
            parsed = urlsplit(lead["url"])
        except ValueError:
            continue
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            continue
        candidates.append(
            {
                "title": str(lead.get("title") or "")[:500],
                "url": lead["url"],
                "status": "unverified",
                "published_at": lead.get("published_at"),
            }
        )
    return candidates


def extract_read_ranges(runtime_state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Convert runtime read_ranges entries (`start`/`end`) into ReadRange payloads."""
    return [
        {
            "source_id": item["source_id"],
            "character_start": item["start"],
            "character_end": item["end"],
        }
        for item in (runtime_state.get("read_ranges") or ())
    ]


def resolve_search_status(events: Sequence[Mapping[str, Any]], *, default_status: str) -> str:
    """Resolve turn search_status from the final news_search event, or return default."""
    search_events = [
        event for event in events if isinstance(event, Mapping) and event.get("name") == "news_search"
    ]
    if not search_events:
        return default_status
    last = search_events[-1]
    return str(last["reason"] if last.get("reason") else last["status"])


def is_source_fully_read(spans: Sequence[tuple[int, int]], text_length: int) -> bool:
    """Return True when [start, end) spans cover [0, text_length) without gaps."""
    position = 0
    for start, end in sorted(spans):
        if start > position:
            break
        position = max(position, end)
    return position >= text_length


def has_partial_read_coverage(
    *,
    sources: list[ResearchSource],
    as_of: datetime | None,
    current_ranges: Sequence[Mapping[str, Any]],
    prior_turns: Sequence[ResearchTurn],
) -> bool:
    """Return True if any eligible source remains incompletely read across turns."""
    # Eligible material that is not fully read (counting earlier turns) limits what absence can mean.
    covered: dict[str, list[tuple[int, int]]] = {}
    for item in current_ranges:
        covered.setdefault(item["source_id"], []).append(
            (item["character_start"], item["character_end"])
        )
    for turn in prior_turns:
        for read_range in turn.read_ranges:
            covered.setdefault(read_range.source_id, []).append(
                (read_range.character_start, read_range.character_end)
            )
    return any(
        not is_source_fully_read(covered.get(item.source_id, ()), len(item.text))
        for item, code in source_eligibility(sources, as_of)
        if code == "eligible"
    )


def compute_turn_gaps(
    *,
    outcome_gaps: Sequence[Mapping[str, Any]],
    sources: list[ResearchSource],
    as_of: datetime | None,
    current_ranges: Sequence[Mapping[str, Any]],
    prior_turns: Sequence[ResearchTurn],
    gap_fn: Callable[[str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Combine outcome gaps with source_selection_partial when eligible text is unread."""
    gaps = [dict(item) for item in outcome_gaps]
    if has_partial_read_coverage(
        sources=sources,
        as_of=as_of,
        current_ranges=current_ranges,
        prior_turns=prior_turns,
    ):
        build_gap = gap_fn or _default_runtime_gap
        gaps.append(build_gap("source_selection_partial"))
    return gaps


def build_turn_usage(
    *,
    model_calls: int,
    search_calls: int,
    usage: int | None,
    accounted_tokens: int | None,
) -> TurnUsage:
    """Assemble TurnUsage with complete vs partial_unknown accounting status."""
    # Admission accounting can include reserved estimates for interrupted calls.
    known_total = max(usage or 0, accounted_tokens or 0)
    usage_status = "partial_unknown" if usage is None and model_calls > 0 else "complete"
    return TurnUsage(
        model_calls=model_calls,
        search_calls=search_calls,
        total_tokens=known_total,
        usage_status=usage_status,
    )


def receipt_breakdown(receipts, events, accounting):
    """Count each admitted call once; keep reservations separate from provider receipts."""
    reported = {}
    receipt_items = receipts.items() if isinstance(receipts, Mapping) else ()
    admitted = {key: r for key, r in receipt_items if isinstance(r, Mapping)
                and r.get('provider') != 'not_invoked' and r.get('settlement') != 'released'}
    for e in events if isinstance(events, (list, tuple)) else ():
        if not isinstance(e, Mapping):
            continue
        cid, tokens = e.get('call_id'), e.get('usage')
        if isinstance(cid, str) and cid in admitted and type(tokens) is int and tokens >= 0 and e.get('kind') in ('model', 'model_failure'):
            reported[cid] = tokens
    for cid, receipt in admitted.items():
        tokens = receipt.get('reported_tokens')
        if type(tokens) is int and tokens >= 0:
            reported[cid] = tokens
    unknown = [r for cid, r in admitted.items() if cid not in reported]
    estimates = [r.get('reserved_estimate_tokens') for r in unknown]
    known_total = sum(reported.values())
    reservation = sum(estimates) if all(type(n) is int for n in estimates) else None
    # Earlier checkpoints retained the charged aggregate and per-call events.
    if reservation is None and type(accounting) is int and all(r.get('settlement') == 'unknown' for r in unknown):
        reservation = max(0, accounting - known_total)
    return dict(reported_tokens=known_total, unknown_usage_calls=len(unknown), reserved_estimate_tokens=reservation)


def build_research_turn(
    *,
    turn_meta: Mapping[str, Any],
    operation_id: str,
    finished_at: datetime,
    outcome: Any,
    gaps: Sequence[Mapping[str, Any]],
    usage: TurnUsage,
    search_status: str,
    official_fetch_status: str,
    read_ranges: Sequence[Mapping[str, Any]],
) -> ResearchTurn:
    """Construct a validated ResearchTurn from run metadata and agent outcome."""
    raw_started_at = turn_meta["started_at"]
    started_at = (
        raw_started_at
        if isinstance(raw_started_at, datetime)
        else datetime.fromisoformat(raw_started_at)
    )
    return ResearchTurn(
        turn_id=turn_meta["turn_id"],
        index=turn_meta["index"],
        question=turn_meta["question"],
        question_origin=turn_meta["question_origin"],
        focus_citation=turn_meta.get("focus_citation"),
        replaces_turn_id=turn_meta.get("replaces_turn_id"),
        operation_ids=[*turn_meta["operation_ids"], operation_id],
        parent_version_id=turn_meta["parent_version_id"],
        started_at=started_at,
        finished_at=finished_at,
        status=outcome.status,
        reason=outcome.reason,
        report=outcome.report,
        partial_notes=outcome.partial_notes,
        question_parts=outcome.question_parts,
        stopping_note=outcome.stopping_note,
        claim_ids=[item["claim_id"] for item in outcome.claims],
        fact_ids=[item["fact_id"] for item in outcome.facts],
        calculation_ids=[item["calculation_id"] for item in outcome.calculations],
        chart_ids=[item["chart_id"] for item in outcome.charts],
        table_ids=[item["table_id"] for item in outcome.tables],
        gaps=gap_rows([dict(item) for item in gaps]),
        usage=usage,
        search_status=search_status,
        official_fetch_status=official_fetch_status,
        read_ranges=[ReadRange.model_validate(item) for item in read_ranges],
        events=outcome.events,
        reconciliation=outcome.reconciliation,
        counter_evidence=getattr(outcome, 'counter_evidence', None),
        coverage_map=outcome.coverage_map,
        method_packs=outcome.method_packs,
        attributions=outcome.attributions,
    )


def record_failed_checkpoint_version(
    run_inputs: Any,
    operation_id: str,
    runtime_state: Mapping[str, Any],
) -> None:
    """Record the checkpoint version that failed locally so recovery requires explicit migration."""
    if run_inputs is None:
        return
    try:
        record = run_inputs.get(operation_id)
        if not isinstance(record, dict):
            return
        updated = deepcopy(record)
        cp = updated.get("runtime_checkpoint")
        version = (
            cp.get("checkpoint_schema_version")
            if isinstance(cp, Mapping)
            else runtime_state.get("checkpoint_schema_version")
        )
        updated["failed_checkpoint_schema_version"] = version if version is not None else 0
        updated.pop("checkpoint_migrated", None)
        updated.pop("checkpoint_repaired", None)
        if isinstance(cp, dict):
            cp.pop("checkpoint_migrated", None)
            cp.pop("checkpoint_repaired", None)
        run_inputs.put(operation_id, updated)
    except Exception:
        pass


def commit_research_version(
    version_chain: ResearchVersionChain,
    content: ResearchContent,
) -> ResearchVersion:
    """Save content to version_chain with optimistic-lock conflict translation."""
    # Refuse to save a stale result over a user's intervening edit.
    try:
        parent = version_chain.current()
        return version_chain.save(parent, content)
    except (TopicNotFound, TopicVersionConflict) as error:
        raise ApiProblem(
            409,
            "lifecycle_conflict",
            "Research changed before the result was saved.",
        ) from error


@dataclass
class ResearchTurnPersister:
    """Coordinates turn finalization, merge_turn isolation handoff, and optimistic save."""

    topic_id: str
    parent_version_id: str
    as_of: datetime | None
    sources: list[ResearchSource]
    version_chain: ResearchVersionChain
    turn_meta: Mapping[str, Any]
    run_inputs: Any = None
    merge_turn_fn: Callable[..., ResearchContent] = merge_turn
    gap_fn: Callable[[str], dict[str, Any]] | None = None
    source_summaries_fn: Callable[[list[str], object], list[dict[str, Any]]] | None = None
    record_failure_fn: Callable[[str, Mapping[str, Any]], None] | None = None

    def record_failed_checkpoint(
        self,
        operation_id: str,
        runtime_state: Mapping[str, Any],
    ) -> None:
        if self.record_failure_fn is not None:
            self.record_failure_fn(operation_id, runtime_state)
            return
        record_failed_checkpoint_version(self.run_inputs, operation_id, runtime_state)

    def finalize_turn(
        self,
        *,
        operation_id: str,
        parent_version: ResearchVersion,
        outcome: Any,
        runtime_state: Mapping[str, Any],
        context: Any,
        finished_at: datetime,
        default_search_status: str,
        official_fetch_status: str,
        operation_started_at: datetime | str | None = None,
    ) -> ResearchRunResult:
        """Assemble ResearchTurn, delegate to merge_turn, and return ResearchRunResult."""
        self.version_chain.source_ids = list(
            dict.fromkeys([*self.version_chain.source_ids, *outcome.source_ids])
        )
        candidates = extract_source_candidates(runtime_state)
        ranges = extract_read_ranges(runtime_state)
        search_status = resolve_search_status(outcome.events, default_status=default_search_status)
        gaps = compute_turn_gaps(
            outcome_gaps=outcome.gaps,
            sources=self.sources,
            as_of=self.as_of,
            current_ranges=ranges,
            prior_turns=parent_version.content.turns,
            gap_fn=self.gap_fn,
        )
        usage = build_turn_usage(
            model_calls=context.calls,
            search_calls=context.search_calls,
            usage=context.usage,
            accounted_tokens=context.accounted_tokens,
        )
        receipts = getattr(context, 'model_call_receipts', {})
        if receipts or context.calls == 0:
            usage = usage.model_copy(update=receipt_breakdown(receipts, outcome.events, context.accounted_tokens))
        turn = build_research_turn(
            turn_meta=self.turn_meta,
            operation_id=operation_id,
            finished_at=finished_at,
            outcome=outcome,
            gaps=gaps,
            usage=usage,
            search_status=search_status,
            official_fetch_status=official_fetch_status,
            read_ranges=ranges,
        )
        turn.market_analyses = [SavedMarketAnalysis.model_validate(a) for a in runtime_state.get("market_analyses", ())
                                if a.get("analysis_id")]
        summaries_fn = self.source_summaries_fn or (
            lambda analyzed_ids, segments: summarize_sources(self.sources, analyzed_ids, segments)
        )
        content = self.merge_turn_fn(
            parent_version.content,
            turn,
            claims=outcome.claims,
            facts=outcome.facts,
            calculations=outcome.calculations,
            charts=outcome.charts,
            tables=outcome.tables,
            sources=summaries_fn(outcome.source_ids, ranges),
            source_candidates=candidates,
        )
        saved = content.turns[-1]
        previous = next((t for t in parent_version.content.turns if t.turn_id == saved.turn_id), None)
        start = operation_started_at
        if isinstance(start, str):
            start = datetime.fromisoformat(start)
        old_answered = {p.question for p in previous.question_parts if p.status == 'answered'} if previous else set()
        operation_receipts = {cid: receipt for cid, receipt in receipts.items()
                              if cid.startswith(operation_id + ':')}
        breakdown = receipt_breakdown(operation_receipts, outcome.events, None) if operation_receipts else {}
        attempt = ResearchAttempt(
            **breakdown,
            operation_id=operation_id, reason=saved.reason,
            elapsed_seconds=max(0.0, (finished_at - start).total_seconds()) if start is not None else None,
            model_calls=max(0, usage.model_calls - (previous.usage.model_calls if previous else 0)),
            search_calls=max(0, usage.search_calls - (previous.usage.search_calls if previous else 0)),
            new_facts=len(set(saved.fact_ids) - set(previous.fact_ids if previous else [])),
            new_calculations=len(set(saved.calculation_ids) - set(previous.calculation_ids if previous else [])),
            new_claims=len(set(saved.claim_ids) - set(previous.claim_ids if previous else [])),
            answered_parts=len({p.question for p in saved.question_parts if p.status == 'answered'} - old_answered),
        )
        saved.attempts = [a for a in (previous.attempts if previous else []) if a.operation_id != operation_id] + [attempt]
        return ResearchRunResult(
            operation_id=operation_id,
            topic_id=self.topic_id,
            parent_version_id=self.parent_version_id,
            status=content.turns[-1].status,
            content=content,
        )

    def commit_result(
        self,
        *,
        operation_id: str,
        result: ResearchRunResult,
        runtime_state: Mapping[str, Any],
    ) -> ResearchVersion:
        """Persist finalized result content with optimistic-lock conflict and failure handling."""
        try:
            return commit_research_version(self.version_chain, result.content)
        except ApiProblem:
            raise
        except Exception:
            self.record_failed_checkpoint(operation_id, runtime_state)
            raise
