"""V1 Layer 1 deterministic SEC event engine (0 LLM).

Provides:
- ``scan_sec_material_events(...) -> tuple[SecEventScanResult, dict[str, str]]``
- ``fetch_lazy_attachment(...) -> LazyAttachmentContent``
- ``rescreen_from_l1_cache(...)`` for 0-SEC-request rule re-evaluation
- ``SecPacingLimiter`` (4.5 req/s waitable SEC pacing), ``build_scale_anchor``, and all form parsers.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Any, Callable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from ....research_signals import (
    EventRunMetrics,
    FailedAccession,
    RawEventCandidate,
    ScaleAnchor,
    SecEventScanResult,
    SuppressedEventRecord,
)
from ....storage import JsonDocumentRepository, _drop_legacy_null_fields
from ...policy import FinDataPolicy, load_policy
from ..base import StreamTransport, Transport, utcnow
from ..sec_deep import validate_single_cik
from ..sec_edgar import cik10, cik_entity_id
from .form4 import RawForm4Parsed, parse_form4_raw, process_form4_batch
from .form6k import Raw6kParsed, apply_6k_rules, parse_6k_raw
from .form8k import (
    DEFAULT_LAZY_MAX_BYTES,
    HARD_LAZY_MAX_BYTES,
    LazyAttachmentContent,
    Raw8kParsed,
    apply_8k_rules,
    classify_8k_items,
    fetch_lazy_attachment,
    parse_8k_raw,
)
from .form13dg import (
    Raw13dgParsed,
    backfill_13dg_via_cik_efts,
    is_13dg_form,
    parse_13dg_raw,
    process_13dg_batch,
)
from .pacing import (
    DEFAULT_SEC_PACING_BURST,
    DEFAULT_SEC_PACING_RATE_PER_SEC,
    PacingTelemetry,
    RawSubmissionRow,
    RawSubmissionsResult,
    SecPacingError,
    SecPacingLimiter,
    fetch_company_submissions,
)
from .rules import (
    SecEventsRules,
    get_rules_taxonomy_subset_hash,
    load_sec_events_rules,
    normalize_event_subtype,
    resolve_affected_pillars,
)
from .scale_anchor import MaterialityCheck, build_scale_anchor, evaluate_materiality_against_anchor, evaluate_share_pct_against_anchor

EventCategory = Literal["material_8k", "insider_trade", "ownership_13dg", "foreign_6k"]
ALL_EVENT_CATEGORIES: tuple[EventCategory, ...] = (
    "material_8k",
    "insider_trade",
    "ownership_13dg",
    "foreign_6k",
)
L1_RAW_COLLECTION = "sec_events_l1_raw"
SEC_EVENTS_CHANNEL = "sec_events"
_EARNINGS_TIMING_FORMS = frozenset({"10-Q", "10-Q/A", "10-K", "10-K/A", "20-F", "20-F/A"})
_MAX_BASELINE_FORM4_FETCHES = 30
_MAX_BASELINE_13DG_FETCHES = 20


class L1RawRecord(BaseModel):
    """Unfiltered deterministic Layer 1 raw record persisted in ``sec_events_l1_raw``."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    cik: str
    accession: str
    parser_version: str = "v1"
    category: EventCategory
    form: str
    filed_at: str
    raw_8k: Raw8kParsed | None = None
    raw_form4: RawForm4Parsed | None = None
    raw_13dg: Raw13dgParsed | None = None
    raw_6k: Raw6kParsed | None = None


def l1_raw_cache_key(cik: str | int, accession: str, parser_version: str = "v1") -> str:
    return f"l1raw:{cik10(cik)}:{accession}:{parser_version}"


def _coerce_l1_raw_record(value: Any) -> L1RawRecord | None:
    if isinstance(value, L1RawRecord):
        return value
    if isinstance(value, Mapping):
        try:
            normalized = _drop_legacy_null_fields(dict(value))
            return L1RawRecord.model_validate(normalized)
        except Exception:
            return None
    return None


def _batch_get_l1(repo: JsonDocumentRepository[Any] | None, keys: Sequence[str]) -> dict[str, L1RawRecord]:
    if repo is None or not keys:
        return {}
    get_many = getattr(repo, "get_many", None)
    if callable(get_many):
        raw_map = get_many(list(keys))
    else:
        raw_map = {k: repo.get(k) for k in keys if repo.get(k) is not None}
    out: dict[str, L1RawRecord] = {}
    for k, v in raw_map.items():
        rec = _coerce_l1_raw_record(v)
        if rec is not None:
            out[k] = rec
    return out


def _batch_put_l1(repo: JsonDocumentRepository[Any] | None, items: Mapping[str, L1RawRecord]) -> None:
    if repo is None or not items:
        return
    put_many = getattr(repo, "put_many", None)
    if callable(put_many):
        put_many(dict(items))
        return
    for k, v in items.items():
        repo.put(k, v)


_PERIODIC_FORMS = frozenset({"10-Q", "10-Q/A", "10-K", "10-K/A", "20-F", "20-F/A"})
_LATE_NOTICE_FORMS = frozenset({"NT 10-Q", "NT 10-K", "NT 20-F", "NT 10-Q/A", "NT 10-K/A"})


def _is_seen_for_channel(
    seen_accessions: Mapping[str, Any] | Sequence[str] | set[str] | frozenset[str] | None,
    accession: str,
    channel: str = SEC_EVENTS_CHANNEL,
) -> bool:
    if not seen_accessions:
        return False
    if isinstance(seen_accessions, Mapping):
        entry = seen_accessions.get(accession)
        if entry is None:
            return False
        if isinstance(entry, Mapping):
            return bool(entry.get(channel))
        if isinstance(entry, str):
            return bool(entry.strip())
        return bool(entry)
    if isinstance(seen_accessions, (list, tuple, set, frozenset)):
        return accession in seen_accessions
    return False


def _classify_row_category(row: RawSubmissionRow, rules: SecEventsRules) -> EventCategory | None:
    form = row.form.upper()
    if form in ("8-K", "8-K/A"):
        return "material_8k"
    if form in ("4", "4/A"):
        return "insider_trade"
    if is_13dg_form(form, rules):
        return "ownership_13dg"
    if form in ("6-K", "6-K/A"):
        return "foreign_6k"
    return None


def _build_timing_and_precedent(
    rows: Sequence[RawSubmissionRow],
    *,
    as_of: date,
    baseline_start: date,
) -> tuple[dict[str, Any], dict[str, Any]]:
    latest_earnings: RawSubmissionRow | None = None
    item_counts: Counter[str] = Counter()
    form_counts: Counter[str] = Counter()

    for r in rows:
        if r.filed_at > as_of:
            continue
        is_earnings_8k = r.form in ("8-K", "8-K/A") and r.items is not None and "2.02" in r.items
        if r.form in _EARNINGS_TIMING_FORMS or is_earnings_8k:
            if latest_earnings is None or r.filed_at > latest_earnings.filed_at:
                latest_earnings = r
        if r.filed_at >= baseline_start:
            form_counts[r.form] += 1
            if r.form in ("8-K", "8-K/A") and r.items:
                for it in r.items:
                    item_counts[it] += 1

    timing_context: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "latest_earnings_filing_date": latest_earnings.filed_at.isoformat() if latest_earnings else None,
        "latest_earnings_form": latest_earnings.form if latest_earnings else None,
        "latest_earnings_accession": latest_earnings.accession if latest_earnings else None,
        "days_since_last_earnings_filing": (as_of - latest_earnings.filed_at).days if latest_earnings else None,
    }
    historical_precedent: dict[str, Any] = {
        "lookback_days": (as_of - baseline_start).days,
        "form_counts_12m": dict(sorted(form_counts.items())),
        "8k_item_counts_12m": dict(sorted(item_counts.items())),
    }
    return timing_context, historical_precedent


def _enforce_source_texts_budget(
    source_texts: dict[str, str],
    kept_candidates: Sequence[RawEventCandidate],
    max_total_chars: int,
) -> dict[str, str]:
    """Keep only sources referenced by kept candidates + exhibits, bounded by ``max_total_chars``."""
    allowed_ids: set[str] = set()
    for cand in kept_candidates:
        allowed_ids.add(cand.source_id)
        ex_ids = cand.facts.get("exhibit_source_ids")
        if isinstance(ex_ids, Mapping):
            for sid in ex_ids.values():
                if isinstance(sid, str):
                    allowed_ids.add(sid)
        if cand.facts.get("has_ex16_auditor_letter"):
            nodash = cand.accession.replace("-", "")
            allowed_ids.add(f"sec_event:{cand.cik}:{nodash}:ex16")

    out: dict[str, str] = {}
    used = 0
    for sid in sorted(allowed_ids):
        txt = source_texts.get(sid)
        if not txt:
            continue
        remaining = max_total_chars - used
        if remaining <= 0:
            break
        if len(txt) > remaining and not sid.endswith(":ex16"):
            out[sid] = txt[: max(0, remaining - 32)].rstrip() + "\n...[source budget cap reached]"
            used = max_total_chars
        else:
            out[sid] = txt
            used += len(txt)
    return out


def _priority_sort_key(cand: RawEventCandidate) -> tuple[int, int, str]:
    is_crit = 0 if cand.statutory_critical_items else 1
    return (is_crit, -cand.filed_at.toordinal(), cand.accession)


def rescreen_from_l1_cache(
    *,
    cik: str | int,
    rows: Sequence[RawSubmissionRow],
    l1_records: Mapping[str, L1RawRecord],
    rules: SecEventsRules,
    as_of: date,
    lookback_days: int | None = None,
    categories: Sequence[EventCategory] | None = None,
    seen_accessions: Mapping[str, Any] | None = None,
    is_fpi: bool = False,
    symbol: str | None = None,
    company_name: str | None = None,
    scale_anchor: ScaleAnchor | None = None,
    max_target_candidates: int | None = None,
    max_recent_context_candidates: int | None = None,
) -> tuple[SecEventScanResult, dict[str, str]]:
    """Re-apply externalized rules locally over already-parsed ``L1RawRecord`` objects (0 SEC requests)."""
    target_cik = cik10(cik)
    eid = cik_entity_id(target_cik)
    lookback = lookback_days if lookback_days is not None else rules.limits.default_lookback_days
    target_start = as_of - timedelta(days=lookback)
    context_start = target_start - timedelta(days=rules.limits.recent_context_days)
    baseline_start = as_of - timedelta(days=rules.limits.baseline_lookback_days)
    active_cats = set(categories) if categories else set(ALL_EVENT_CATEGORIES)

    filtered_rows = [r for r in rows if r.filed_at <= as_of]
    timing_ctx, precedent = _build_timing_and_precedent(
        filtered_rows,
        as_of=as_of,
        baseline_start=baseline_start,
    )

    all_targets: list[RawEventCandidate] = []
    all_contexts: list[RawEventCandidate] = []
    suppressed: list[SuppressedEventRecord] = []
    all_source_texts: dict[str, str] = {}
    processed: dict[str, str] = {}

    raw_f4_list: list[RawForm4Parsed] = []
    raw_13dg_list: list[Raw13dgParsed] = []

    for row in filtered_rows:
        cat = _classify_row_category(row, rules)
        if cat is None or cat not in active_cats:
            continue
        if _is_seen_for_channel(seen_accessions, row.accession) and row.filed_at >= target_start:
            continue
        rec = l1_records.get(row.accession)
        if rec is None:
            continue
        if cat == "material_8k" and rec.raw_8k is not None:
            if row.filed_at < context_start:
                continue
            is_ctx = row.filed_at < target_start
            cand, supp, stexts = apply_8k_rules(
                rec.raw_8k,
                rules,
                symbol=symbol,
                company_name=company_name,
                is_context_only=is_ctx,
            )
            if cand is not None:
                (all_contexts if is_ctx else all_targets).append(cand)
                all_source_texts.update(stexts)
            elif supp is not None and not is_ctx:
                suppressed.append(supp)
                processed[row.accession] = f"suppressed:{supp.suppression_reason}"
        elif cat == "foreign_6k" and rec.raw_6k is not None:
            if row.filed_at < context_start:
                continue
            is_ctx = row.filed_at < target_start
            cand, supp, stexts = apply_6k_rules(
                rec.raw_6k,
                rules,
                symbol=symbol,
                company_name=company_name,
                is_context_only=is_ctx,
            )
            if cand is not None:
                (all_contexts if is_ctx else all_targets).append(cand)
                all_source_texts.update(stexts)
            elif supp is not None and not is_ctx:
                suppressed.append(supp)
                processed[row.accession] = f"suppressed:{supp.suppression_reason}"
        elif cat == "insider_trade" and rec.raw_form4 is not None and row.filed_at >= baseline_start:
            raw_f4_list.append(rec.raw_form4)
        elif cat == "ownership_13dg" and rec.raw_13dg is not None and row.filed_at >= baseline_start:
            raw_13dg_list.append(rec.raw_13dg)

    if raw_f4_list and "insider_trade" in active_cats:
        f4_cands, f4_supp, f4_texts, f4_base_summary = process_form4_batch(
            raw_f4_list,
            rules,
            target_start=target_start,
            context_start=context_start,
            as_of=as_of,
            symbol=symbol,
            company_name=company_name,
        )
        precedent["form4_12m_summary"] = f4_base_summary
        for c in f4_cands:
            (all_contexts if c.is_context_only else all_targets).append(c)
        all_source_texts.update(f4_texts)
        for s in f4_supp:
            suppressed.append(s)
            processed[s.accession] = f"suppressed:{s.suppression_reason}"

    if raw_13dg_list and "ownership_13dg" in active_cats:
        d_cands, d_supp, d_texts = process_13dg_batch(
            raw_13dg_list,
            rules,
            target_start=target_start,
            context_start=context_start,
            as_of=as_of,
            is_fpi=is_fpi,
            symbol=symbol,
            company_name=company_name,
        )
        for c in d_cands:
            (all_contexts if c.is_context_only else all_targets).append(c)
        all_source_texts.update(d_texts)
        for s in d_supp:
            suppressed.append(s)
            processed[s.accession] = f"suppressed:{s.suppression_reason}"

    target_cap = max_target_candidates if max_target_candidates is not None else rules.limits.max_target_candidates
    ctx_cap = max_recent_context_candidates if max_recent_context_candidates is not None else rules.limits.max_recent_context_candidates

    all_targets.sort(key=_priority_sort_key)
    kept_targets = all_targets[:target_cap]
    overflow = all_targets[target_cap:]
    open_gaps = [
        f"overflow_deferred:{c.accession}:{c.form}:{c.filed_at.isoformat()}"
        for c in overflow
    ]

    all_contexts.sort(key=lambda c: (c.filed_at, c.accession), reverse=True)
    kept_contexts = all_contexts[:ctx_cap]

    for c in kept_targets:
        processed[c.accession] = "candidate"
    for c in kept_contexts:
        processed.setdefault(c.accession, "context_only")

    kept_source_texts = _enforce_source_texts_budget(
        all_source_texts,
        [*kept_targets, *kept_contexts],
        rules.limits.source_texts_total_max_chars,
    )
    supp_counts = dict(Counter(s.suppression_reason for s in suppressed))
    metrics = EventRunMetrics(
        rules_version=rules.rules_version,
        parser_version=rules.parser_version,
        total_filings_inspected=len(l1_records),
        l1_cache_hits=len(l1_records),
        l1_cache_misses=0,
        l1_suppressed_counts=supp_counts,
        l1_failed_count=0,
        l1_candidates_emitted=len(kept_targets),
        l1_overflow_deferred=len(overflow),
    )

    result = SecEventScanResult(
        cik=target_cik,
        entity_id=eid,
        symbol=symbol,
        company_name=company_name,
        is_fpi=is_fpi,
        fpi_exempt_from_form4=is_fpi,
        as_of=as_of,
        lookback_days=lookback,
        rules_version=rules.rules_version,
        parser_version=rules.parser_version,
        scale_anchor=scale_anchor,
        timing_context=timing_ctx,
        historical_precedent=precedent,
        target_candidates=kept_targets,
        recent_context_candidates=kept_contexts,
        suppressed_events=suppressed,
        failed_accessions=[],
        processed_accessions=processed,
        open_gaps=open_gaps,
        metrics=metrics,
    )
    return result, kept_source_texts


def scan_sec_material_events(
    cik: str | int,
    *,
    user_agent: str | None,
    as_of: date | None = None,
    lookback_days: int | None = None,
    categories: Sequence[EventCategory] | None = None,
    seen_accessions: Mapping[str, Any] | None = None,
    l1_cache: JsonDocumentRepository[Any] | None = None,
    rules: SecEventsRules | None = None,
    rules_path: str | Path | None = None,
    transport: Transport | None = None,
    stream_transport: StreamTransport | None = None,
    limiter: SecPacingLimiter | None = None,
    policy: FinDataPolicy | None = None,
    now_fn: Callable[[], datetime] = utcnow,
    deadline: float | None = None,
    cancel_event: Event | None = None,
    max_target_candidates: int | None = None,
    max_recent_context_candidates: int | None = None,
    enable_13dg_efts_backfill: bool = True,
    symbol: str | None = None,
    entity_id: str | None = None,
    include_scale_anchor: bool = True,
    submissions_override: RawSubmissionsResult | None = None,
    facts_override: Mapping[str, Any] | None = None,
) -> tuple[SecEventScanResult, dict[str, str]]:
    """Orchestrate V1 Layer 1 deterministic SEC event scan (0 LLM).

    Transaction discipline:
    - Never holds any Postgres transaction across SEC network I/O.
    - Performs one short-lived ``get_many`` before network I/O and one short-lived ``put_many`` after.
    - Failed accessions are returned in ``failed_accessions`` and NEVER written to ``processed_accessions``.
    """
    started_at = monotonic()
    target_cik = validate_single_cik(cik)
    eid = entity_id or cik_entity_id(target_cik)
    active_rules = rules or load_sec_events_rules(rules_path)
    active_policy = policy or load_policy()
    active_limiter = limiter or SecPacingLimiter()
    telemetry = PacingTelemetry()

    effective_as_of = as_of or now_fn().date()
    lookback = lookback_days if lookback_days is not None else active_rules.limits.default_lookback_days
    if lookback < 1:
        raise ValueError("lookback_days must be >= 1.")

    target_start = effective_as_of - timedelta(days=lookback)
    context_start = target_start - timedelta(days=active_rules.limits.recent_context_days)
    baseline_start = effective_as_of - timedelta(days=active_rules.limits.baseline_lookback_days)
    fetch_since = min(context_start, baseline_start)

    active_cats = set(categories) if categories else set(ALL_EVENT_CATEGORIES)
    unknown_cats = active_cats - set(ALL_EVENT_CATEGORIES)
    if unknown_cats:
        raise ValueError(f"Unsupported event categories: {', '.join(sorted(unknown_cats))}")

    open_gaps: list[str] = []

    # 1. Fetch raw submissions (preserving items + on-demand filings.files pagination) — NO DB tx held!
    if submissions_override is not None:
        sub_res = submissions_override
    else:
        try:
            sub_res = fetch_company_submissions(
                target_cik,
                user_agent=user_agent,
                fetch_since_date=fetch_since,
                as_of=effective_as_of,
                max_pages=active_rules.limits.max_submissions_pages,
                fpi_indicator_forms=active_rules.form6k.fpi_indicator_forms,
                limiter=active_limiter,
                policy=active_policy,
                transport=transport,
                stream_transport=stream_transport,
                cancel_event=cancel_event,
                deadline=deadline,
                telemetry=telemetry,
            )
        except SecPacingError as err:
            if err.reason in ("sec_contact_unconfigured", "forbidden", "invalid_sec_url") or "denied" in err.reason or "policy" in err.reason:
                raise
            open_gaps.append(f"submissions_unavailable:{err.reason}")
            return (
                SecEventScanResult(
                    cik=target_cik,
                    entity_id=eid,
                    symbol=symbol,
                    as_of=effective_as_of,
                    lookback_days=lookback,
                    rules_version=active_rules.rules_version,
                    parser_version=active_rules.parser_version,
                    open_gaps=open_gaps,
                    metrics=EventRunMetrics(
                        rules_version=active_rules.rules_version,
                        parser_version=active_rules.parser_version,
                        elapsed_ms=int((monotonic() - started_at) * 1000),
                    ),
                ),
                {},
            )

    open_gaps.extend(sub_res.gaps)
    resolved_symbol = symbol or (sub_res.tickers[0] if sub_res.tickers else None)
    resolved_name = sub_res.company_name
    is_fpi = sub_res.is_fpi
    fpi_exempt = is_fpi

    # Strict as_of truncation
    sub_rows = [r for r in sub_res.rows if r.filed_at <= effective_as_of]

    # Optional single-CIK EFTS backfill for 13D/G
    if enable_13dg_efts_backfill and "ownership_13dg" in active_cats and submissions_override is None:
        try:
            extra_13dg = backfill_13dg_via_cik_efts(
                target_cik,
                start_date=context_start,
                as_of=effective_as_of,
                existing_accessions={r.accession for r in sub_rows},
                rules=active_rules,
                user_agent=user_agent,
                limiter=active_limiter,
                policy=active_policy,
                transport=transport,
                stream_transport=stream_transport,
                cancel_event=cancel_event,
                deadline=deadline,
                telemetry=telemetry,
            )
            if extra_13dg:
                sub_rows.extend(extra_13dg)
                sub_rows.sort(
                    key=lambda r: (r.filed_at, r.accepted_at or datetime.min.replace(tzinfo=timezone.utc), r.accession),
                    reverse=True,
                )
        except SecPacingError as err:
            open_gaps.append(f"efts_13dg_backfill_stopped:{err.reason}")

    timing_ctx, precedent = _build_timing_and_precedent(
        sub_rows,
        as_of=effective_as_of,
        baseline_start=baseline_start,
    )

    # 2. Select candidate submission rows to inspect
    to_inspect: list[tuple[RawSubmissionRow, EventCategory]] = []
    hist_f4_count = 0
    hist_13dg_count = 0
    hist_f4_truncated = False

    for row in sub_rows:
        cat = _classify_row_category(row, active_rules)
        if cat is None or cat not in active_cats:
            continue
        if _is_seen_for_channel(seen_accessions, row.accession) and row.filed_at >= target_start:
            continue

        if cat in ("material_8k", "foreign_6k"):
            if row.filed_at < context_start:
                continue
            to_inspect.append((row, cat))
        elif cat == "insider_trade":
            if row.filed_at < baseline_start:
                continue
            if row.filed_at < context_start:
                if hist_f4_count >= _MAX_BASELINE_FORM4_FETCHES:
                    hist_f4_truncated = True
                    continue
                hist_f4_count += 1
            to_inspect.append((row, cat))
        elif cat == "ownership_13dg":
            if row.filed_at < baseline_start:
                continue
            if row.filed_at < context_start:
                if hist_13dg_count >= _MAX_BASELINE_13DG_FETCHES:
                    continue
                hist_13dg_count += 1
            to_inspect.append((row, cat))

    if hist_f4_truncated:
        precedent["form4_baseline_truncated"] = True

    # 3. Short-transaction batch get_many BEFORE any filing SEC I/O
    parser_ver = active_rules.parser_version
    key_by_acc = {row.accession: l1_raw_cache_key(target_cik, row.accession, parser_ver) for row, _ in to_inspect}
    cached_by_key = _batch_get_l1(l1_cache, list(key_by_acc.values()))

    l1_hits = 0
    l1_misses = 0
    parsed_records: dict[str, L1RawRecord] = {}
    new_to_cache: dict[str, L1RawRecord] = {}
    failed_accessions: list[FailedAccession] = []
    aborted_reason: str | None = None

    # 4. Fetch & parse cache misses outside DB transaction
    for row, cat in to_inspect:
        ckey = key_by_acc[row.accession]
        cached_rec = cached_by_key.get(ckey)

        # If cached 8-K only stored pre-screen metadata (fetched_full_content=False) and rules now want it, fetch full!
        needs_upgrade = False
        if cached_rec is not None and cat == "material_8k" and cached_rec.raw_8k is not None:
            if not cached_rec.raw_8k.fetched_full_content:
                decision, _k, _m = classify_8k_items(cached_rec.raw_8k.raw_items, active_rules)
                if decision == "keep":
                    needs_upgrade = True

        if cached_rec is not None and not needs_upgrade:
            l1_hits += 1
            parsed_records[row.accession] = cached_rec
            continue

        if aborted_reason is not None:
            open_gaps.append(f"unfetched_after_{aborted_reason}:{row.accession}:{row.form}")
            continue

        l1_misses += 1
        try:
            if cat == "material_8k":
                r8 = parse_8k_raw(
                    row,
                    active_rules,
                    user_agent=user_agent,
                    limiter=active_limiter,
                    policy=active_policy,
                    transport=transport,
                    stream_transport=stream_transport,
                    cancel_event=cancel_event,
                    deadline=deadline,
                    telemetry=telemetry,
                )
                rec = L1RawRecord(
                    cik=target_cik,
                    accession=row.accession,
                    parser_version=parser_ver,
                    category=cat,
                    form=row.form,
                    filed_at=row.filed_at.isoformat(),
                    raw_8k=r8,
                )
            elif cat == "insider_trade":
                rf4 = parse_form4_raw(
                    row,
                    active_rules,
                    user_agent=user_agent,
                    limiter=active_limiter,
                    policy=active_policy,
                    transport=transport,
                    stream_transport=stream_transport,
                    cancel_event=cancel_event,
                    deadline=deadline,
                    telemetry=telemetry,
                )
                rec = L1RawRecord(
                    cik=target_cik,
                    accession=row.accession,
                    parser_version=parser_ver,
                    category=cat,
                    form=row.form,
                    filed_at=row.filed_at.isoformat(),
                    raw_form4=rf4,
                )
            elif cat == "ownership_13dg":
                r13 = parse_13dg_raw(
                    row,
                    user_agent=user_agent,
                    limiter=active_limiter,
                    policy=active_policy,
                    transport=transport,
                    stream_transport=stream_transport,
                    cancel_event=cancel_event,
                    deadline=deadline,
                    telemetry=telemetry,
                )
                rec = L1RawRecord(
                    cik=target_cik,
                    accession=row.accession,
                    parser_version=parser_ver,
                    category=cat,
                    form=row.form,
                    filed_at=row.filed_at.isoformat(),
                    raw_13dg=r13,
                )
            else:  # foreign_6k
                r6 = parse_6k_raw(
                    row,
                    active_rules,
                    user_agent=user_agent,
                    limiter=active_limiter,
                    policy=active_policy,
                    transport=transport,
                    stream_transport=stream_transport,
                    cancel_event=cancel_event,
                    deadline=deadline,
                    telemetry=telemetry,
                )
                rec = L1RawRecord(
                    cik=target_cik,
                    accession=row.accession,
                    parser_version=parser_ver,
                    category=cat,
                    form=row.form,
                    filed_at=row.filed_at.isoformat(),
                    raw_6k=r6,
                )
        except SecPacingError as err:
            failed_accessions.append(
                FailedAccession(
                    accession=row.accession,
                    form=row.form,
                    filed_at=row.filed_at,
                    error_reason=err.reason,
                    retryable=err.retryable,
                )
            )
            if err.reason in ("cancelled", "deadline_exceeded"):
                aborted_reason = err.reason
            continue
        except Exception as err:
            failed_accessions.append(
                FailedAccession(
                    accession=row.accession,
                    form=row.form,
                    filed_at=row.filed_at,
                    error_reason=f"parse_error:{type(err).__name__}",
                    retryable=False,
                )
            )
            continue

        parsed_records[row.accession] = rec
        new_to_cache[ckey] = rec

    # 5. Short-transaction batch put_many AFTER filing SEC I/O
    if new_to_cache:
        _batch_put_l1(l1_cache, new_to_cache)

    # 6. Build compliant SEC-native ScaleAnchor (strictly filed_at <= effective_as_of, owner_scoped=False)
    scale_anchor: ScaleAnchor | None = None
    if include_scale_anchor and aborted_reason is None:
        scale_anchor = build_scale_anchor(
            target_cik,
            as_of_filed_date=effective_as_of,
            is_fpi=is_fpi,
            user_agent=user_agent,
            limiter=active_limiter,
            policy=active_policy,
            transport=transport,
            stream_transport=stream_transport,
            cancel_event=cancel_event,
            deadline=deadline,
            telemetry=telemetry,
            facts_override=facts_override,
        )

    # 7. Apply externalized rules deterministically over unfiltered parsed records
    inspected_rows = [row for row, _cat in to_inspect if row.accession in parsed_records]
    rescreened, kept_source_texts = rescreen_from_l1_cache(
        cik=target_cik,
        rows=sub_rows,
        l1_records=parsed_records,
        rules=active_rules,
        as_of=effective_as_of,
        lookback_days=lookback,
        categories=list(active_cats),
        seen_accessions=seen_accessions,
        is_fpi=is_fpi,
        symbol=resolved_symbol,
        company_name=resolved_name,
        scale_anchor=scale_anchor,
        max_target_candidates=max_target_candidates,
        max_recent_context_candidates=max_recent_context_candidates,
    )

    # Ensure failed_accessions are NEVER in processed_accessions
    processed = dict(rescreened.processed_accessions)
    for failed in failed_accessions:
        processed.pop(failed.accession, None)

    combined_gaps = [*open_gaps, *rescreened.open_gaps]
    elapsed_ms = int((monotonic() - started_at) * 1000)
    final_metrics = rescreened.metrics.model_copy(
        update={
            "total_filings_inspected": len(inspected_rows) + len(failed_accessions),
            "l1_cache_hits": l1_hits,
            "l1_cache_misses": l1_misses,
            "l1_failed_count": len(failed_accessions),
            "elapsed_ms": elapsed_ms,
        }
    )

    # Collect periodic filings (10-Q/10-K/20-F + NT late-filing notices) at zero extra SEC cost
    from ....research_signals import PeriodicFilingRef

    periodic_filings: list[PeriodicFilingRef] = []
    for row in sub_rows:
        form_u = row.form.upper()
        if form_u in _PERIODIC_FORMS or form_u in _LATE_NOTICE_FORMS:
            if row.filed_at >= target_start:
                periodic_filings.append(
                    PeriodicFilingRef(
                        cik=target_cik,
                        accession=row.accession,
                        form=form_u,
                        filed_at=row.filed_at,
                        period_of_report=row.period_of_report,
                        primary_document_url=row.primary_document_url,
                        is_late_notice=form_u in _LATE_NOTICE_FORMS,
                    )
                )

    final_result = rescreened.model_copy(
        update={
            "entity_id": eid,
            "symbol": resolved_symbol,
            "company_name": resolved_name,
            "is_fpi": is_fpi,
            "fpi_exempt_from_form4": fpi_exempt,
            "timing_context": timing_ctx,
            "historical_precedent": {**precedent, **{k: v for k, v in rescreened.historical_precedent.items() if k not in precedent}},
            "periodic_filings": periodic_filings,
            "failed_accessions": failed_accessions,
            "processed_accessions": processed,
            "open_gaps": combined_gaps,
            "metrics": final_metrics,
        }
    )
    return final_result, kept_source_texts


__all__ = [
    "ALL_EVENT_CATEGORIES",
    "DEFAULT_LAZY_MAX_BYTES",
    "DEFAULT_SEC_PACING_BURST",
    "DEFAULT_SEC_PACING_RATE_PER_SEC",
    "EventCategory",
    "HARD_LAZY_MAX_BYTES",
    "L1RawRecord",
    "L1_RAW_COLLECTION",
    "LazyAttachmentContent",
    "MaterialityCheck",
    "PacingTelemetry",
    "RawSubmissionRow",
    "RawSubmissionsResult",
    "SEC_EVENTS_CHANNEL",
    "SecEventsRules",
    "SecPacingError",
    "SecPacingLimiter",
    "build_scale_anchor",
    "evaluate_materiality_against_anchor",
    "evaluate_share_pct_against_anchor",
    "fetch_company_submissions",
    "fetch_lazy_attachment",
    "get_rules_taxonomy_subset_hash",
    "l1_raw_cache_key",
    "load_sec_events_rules",
    "normalize_event_subtype",
    "rescreen_from_l1_cache",
    "resolve_affected_pillars",
    "scan_sec_material_events",
]
