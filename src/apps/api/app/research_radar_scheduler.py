"""Single SEC Filing Dispatcher, P1-B Staggered Priority Queue, and Unified Change Inbox (Direction B).

Implements:
- ``FilingWatchSubscription`` with per-channel idempotent ``seen_accessions`` and
  ``failed_accessions`` (a failed channel is NEVER written to ``seen_accessions``).
- P1-B two-phase staggered sweep:
  1. Fast 0-LLM Layer 1 scan across all enabled subscriptions (1-3s/company).
  2. Priority-ordered Layer 2 queue (P0 Statutory Critical > P1 Earnings Delta >
     P2 Governance/M&A/13D > P3 Routine) executed through the platform concurrency gate.
- Unified ``ChangeInboxItem`` routing for both V1 ``CompanySignalEnvelope`` alerts and
  Direction B ``ThesisScorecardDiff`` updates, with ``event_thread_key`` amendment
  folding and explicit user decision audit trail (never silently mutates confirmed ledgers).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Literal, Mapping, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .research_signals import CompanySignalEnvelope, RawEventCandidate, SecEventAnalysisResult, SecEventScanResult
from .research_thesis_ledger import (
    ThesisClaim,
    ThesisLedger,
    ThesisPillar,
    WatchTrigger,
    build_v1_thesis_context,
    confirm_thesis_ledger_version,
    evaluate_metric_trigger,
)

WatchChannel = Literal["v1_events", "earnings_delta", "peer_relative", "governance_audit"]
WatchHealthStatus = Literal["healthy", "degraded", "error"]
InboxSourceChannel = Literal["v1_events", "earnings_delta", "peer_relative", "governance_audit"]
InboxImpactLevel = Literal["critical", "high", "medium", "low"]
InboxDirection = Literal["positive", "negative", "mixed", "neutral"]
InboxReviewState = Literal["unread", "read", "accepted", "dismissed_with_note"]
InboxDecisionAction = Literal["accept", "edit_and_accept", "keep_original", "mark_read"]

_P0_STATUTORY_ITEMS = frozenset({"4.02", "1.03", "2.04", "4.01", "1.05"})
_P2_GOVERNANCE_MNA_ITEMS = frozenset({"5.02", "1.01", "1.02", "2.05"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class _FrozenRadarModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PeerWatchConfig(_FrozenRadarModel):
    """Optional V2 relative-valuation watch configuration stored on a subscription."""

    symbols: list[str] = Field(min_length=2, max_length=6)
    triggers: list[WatchTrigger] = Field(default_factory=list)


class FilingWatchSubscription(_FrozenRadarModel):
    """Per-company SEC radar subscription shared across V1 events, earnings delta, and peer relative channels."""

    subscription_id: str = Field(default_factory=lambda: _new_id("sub"), min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=120)
    topic_id: str | None = None
    cik: str = Field(min_length=1, max_length=20)
    symbol: str | None = None
    company_name: str | None = None
    enabled: bool = True
    watched_channels: list[WatchChannel] = Field(
        default_factory=lambda: ["v1_events", "earnings_delta", "governance_audit"]
    )
    peer_watch_config: PeerWatchConfig | None = None
    # Per-channel idempotency & failure isolation: accession -> {"v1"?: status, "earnings"?: status}
    seen_accessions: dict[str, dict[str, str]] = Field(default_factory=dict)
    failed_accessions: dict[str, dict[str, str]] = Field(default_factory=dict)
    lookback_days: int = Field(default=30, ge=1, le=365)
    check_interval_minutes: int = Field(default=60, ge=5, le=1440)
    last_checked_at: datetime | None = None
    next_check_at: datetime | None = None
    last_signal_at: datetime | None = None
    health_status: WatchHealthStatus = "healthy"
    last_error: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ChangeInboxItem(_FrozenRadarModel):
    """Unified 30-second decision card in the user's Change Inbox."""

    inbox_item_id: str = Field(default_factory=lambda: _new_id("inb"), min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=120)
    subscription_id: str | None = None
    topic_id: str | None = None
    cik: str = Field(min_length=1, max_length=20)
    symbol: str | None = None
    company_name: str | None = None
    source_channel: InboxSourceChannel
    event_thread_key: str = Field(min_length=1, max_length=240)
    accession: str | None = None
    superseded_by_accession: str | None = None
    headline: str = Field(min_length=1, max_length=400)
    summary: str = Field(min_length=1, max_length=4000)
    impact_level: InboxImpactLevel = "medium"
    direction: InboxDirection = "neutral"
    affected_pillars: list[str] = Field(default_factory=list)
    linked_claim_ids: list[str] = Field(default_factory=list)
    skeptic_dissent: str | None = None
    signal_envelope: CompanySignalEnvelope | None = None
    scorecard_diff: dict[str, Any] | None = None
    review_state: InboxReviewState = "unread"
    user_decision_note: str | None = None
    resulting_ledger_version: int | None = None
    disclosed_at: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class SubscriptionHealthView(_FrozenRadarModel):
    subscription_id: str
    topic_id: str | None = None
    cik: str
    symbol: str | None = None
    company_name: str | None = None
    enabled: bool
    health_status: WatchHealthStatus
    last_checked_at: datetime | None = None
    last_signal_at: datetime | None = None
    last_error: str | None = None
    failed_accession_count: int = 0


class RadarHealthSummary(_FrozenRadarModel):
    total_subscriptions: int = 0
    enabled_subscriptions: int = 0
    healthy_count: int = 0
    degraded_count: int = 0
    error_count: int = 0
    unread_inbox_count: int = 0
    last_checked_at: datetime | None = None
    subscriptions: list[SubscriptionHealthView] = Field(default_factory=list)


class SubscriptionCheckOutcome(_FrozenRadarModel):
    subscription_id: str
    cik: str
    symbol: str | None = None
    priority_tier: int | None = None
    l1_candidates_found: int = 0
    earnings_candidates_found: int = 0
    l2_executed: bool = False
    inbox_items_created: int = 0
    inbox_items_updated: int = 0
    health_status: WatchHealthStatus = "healthy"
    error: str | None = None


class RadarSweepSummary(_FrozenRadarModel):
    subscriptions_checked: int = 0
    l1_zero_change_count: int = 0
    l2_queued_count: int = 0
    inbox_items_created: int = 0
    inbox_items_updated: int = 0
    degraded_count: int = 0
    error_count: int = 0
    outcomes: list[SubscriptionCheckOutcome] = Field(default_factory=list)


# --------------------------------------------------------------------------- Priority & Routing Helpers

def seen_accessions_for_channel(sub: FilingWatchSubscription, channel: Literal["v1", "earnings"]) -> list[str]:
    """Return accessions already completed for ``channel`` (excluding any still in ``failed_accessions``)."""
    failed_for_ch = {
        acc for acc, ch_map in sub.failed_accessions.items() if channel in ch_map
    }
    return sorted(
        acc
        for acc, ch_map in sub.seen_accessions.items()
        if channel in ch_map and acc not in failed_for_ch
    )


def compute_candidate_priority_tier(
    v1_candidates: Sequence[RawEventCandidate],
    has_new_earnings: bool = False,
) -> int:
    """Compute P1-B queue priority tier (lower number = higher priority):

    - ``0`` (P0): Statutory critical 8-K (4.02 / 1.03 / 2.04 / 4.01 / 1.05)
    - ``1`` (P1): New earnings filing (10-Q / 10-K / 20-F / 2.02 / earnings 6-K)
    - ``2`` (P2): Governance / M&A / Activist 13D (5.02 / 1.01 / 1.02 / 2.05 / 13D)
    - ``3`` (P3): Routine 8-K / Form 4 / 13G / non-earnings 6-K
    """
    for cand in v1_candidates:
        if cand.statutory_critical_items or (set(cand.items) & _P0_STATUTORY_ITEMS):
            return 0
    if has_new_earnings:
        return 1
    for cand in v1_candidates:
        form_u = cand.form.upper()
        if (set(cand.items) & _P2_GOVERNANCE_MNA_ITEMS) or ("13D" in form_u):
            return 2
    return 3


def map_pillars_to_claim_ids(
    affected_pillars: Sequence[str],
    ledger: ThesisLedger | None,
) -> list[str]:
    """Match V1/Delta ``affected_pillars`` to active claim IDs on a confirmed ``ThesisLedger``."""
    if ledger is None or not affected_pillars:
        return []
    wanted = {p.strip().lower() for p in affected_pillars if isinstance(p, str) and p.strip()}
    if not wanted:
        return []
    return [
        c.claim_id
        for c in ledger.claims
        if c.status != "superseded" and c.pillar.lower() in wanted
    ]


def build_inbox_item_from_v1_signal(
    *,
    account_id: str,
    subscription: FilingWatchSubscription,
    signal: CompanySignalEnvelope,
    ledger: ThesisLedger | None = None,
) -> ChangeInboxItem:
    """Convert an actionable V1 ``CompanySignalEnvelope`` into a ``ChangeInboxItem``."""
    linked_ids = map_pillars_to_claim_ids(signal.affected_thesis_pillars, ledger)
    subtype_label = signal.event_subtype.replace("_", " ").title()
    sym_prefix = f"{subscription.symbol or signal.symbol or subscription.cik} · "
    headline = f"{sym_prefix}{signal.provenance.form} {subtype_label}"[:400]
    thread_key = signal.event_thread_key or f"{subscription.cik}:{signal.provenance.form}:{signal.provenance.accession}"
    return ChangeInboxItem(
        account_id=account_id,
        subscription_id=subscription.subscription_id,
        topic_id=subscription.topic_id,
        cik=subscription.cik,
        symbol=subscription.symbol or signal.symbol,
        company_name=subscription.company_name or signal.company_name,
        source_channel="v1_events",
        event_thread_key=thread_key,
        accession=signal.provenance.accession,
        superseded_by_accession=signal.superseded_by_accession,
        headline=headline,
        summary=signal.summary[:4000],
        impact_level=signal.severity,
        direction=signal.direction,
        affected_pillars=list(signal.affected_thesis_pillars),
        linked_claim_ids=linked_ids,
        skeptic_dissent=signal.skeptic_dissent,
        signal_envelope=signal,
        review_state="unread",
        disclosed_at=signal.disclosed_at,
    )


def upsert_inbox_item_with_folding(
    new_item: ChangeInboxItem,
    existing_items: Sequence[ChangeInboxItem],
) -> tuple[ChangeInboxItem, bool]:
    """Fold amendments sharing ``(account_id, cik, event_thread_key)`` instead of spamming duplicates.

    Returns ``(item_to_persist, is_new)``.
    """
    for old in existing_items:
        if (
            old.account_id == new_item.account_id
            and old.cik == new_item.cik
            and old.source_channel == new_item.source_channel
            and old.event_thread_key == new_item.event_thread_key
        ):
            if old.accession == new_item.accession:
                # Exact same accession already in inbox -> idempotent no-op refresh
                return old, False
            # Amendment / newer filing on same event thread -> fold in place and re-mark unread
            updated = old.model_copy(
                update={
                    "accession": new_item.accession,
                    "superseded_by_accession": new_item.accession,
                    "headline": new_item.headline,
                    "summary": new_item.summary,
                    "impact_level": new_item.impact_level,
                    "direction": new_item.direction,
                    "affected_pillars": new_item.affected_pillars,
                    "linked_claim_ids": new_item.linked_claim_ids,
                    "skeptic_dissent": new_item.skeptic_dissent,
                    "signal_envelope": new_item.signal_envelope,
                    "scorecard_diff": new_item.scorecard_diff,
                    "review_state": "unread",
                    "disclosed_at": new_item.disclosed_at,
                    "updated_at": _utcnow(),
                }
            )
            return updated, False
    return new_item, True


def apply_inbox_decision(
    *,
    item: ChangeInboxItem,
    action: InboxDecisionAction,
    user_note: str | None = None,
    confirmed_ledger: ThesisLedger | None = None,
    edited_claims: Sequence[ThesisClaim] | None = None,
) -> tuple[ChangeInboxItem, ThesisLedger | None]:
    """Apply an explicit user review action to a ``ChangeInboxItem``.

    - ``mark_read``: marks ``review_state="read"``, never modifies ledger.
    - ``keep_original``: marks ``review_state="dismissed_with_note"``, preserves confirmed ledger untouched.
    - ``accept`` / ``edit_and_accept``: if a confirmed ledger exists (or ``edited_claims`` supplied),
      creates immutable ``ThesisLedger v(N+1)`` and records ``resulting_ledger_version``.
    """
    now = _utcnow()
    if action == "mark_read":
        return item.model_copy(update={"review_state": "read", "updated_at": now}), None

    if action == "keep_original":
        return (
            item.model_copy(
                update={
                    "review_state": "dismissed_with_note",
                    "user_decision_note": (user_note or "").strip() or "User retained prior confirmed thesis.",
                    "updated_at": now,
                }
            ),
            None,
        )

    # accept / edit_and_accept
    new_ledger: ThesisLedger | None = None
    if confirmed_ledger is not None or edited_claims:
        if edited_claims is not None:
            next_claims = list(edited_claims)
        else:
            assert confirmed_ledger is not None
            linked_set = set(item.linked_claim_ids)
            next_claims = []
            for claim in confirmed_ledger.claims:
                if claim.claim_id in linked_set:
                    next_status = claim.status
                    if item.direction == "negative" and item.impact_level in ("critical", "high"):
                        next_status = "weakened"
                    elif item.direction == "positive" and item.impact_level in ("critical", "high"):
                        next_status = "strengthened"
                    refs = list(dict.fromkeys([*claim.evidence_refs, *([item.accession] if item.accession else [])]))
                    note_parts = [p for p in (claim.user_note, user_note) if p and p.strip()]
                    next_claims.append(
                        claim.model_copy(
                            update={
                                "status": next_status,
                                "evidence_refs": refs[:12],
                                "user_note": " | ".join(note_parts) if note_parts else None,
                            }
                        )
                    )
                else:
                    next_claims.append(claim)

        new_ledger = confirm_thesis_ledger_version(
            account_id=item.account_id,
            topic_id=item.topic_id or (confirmed_ledger.topic_id if confirmed_ledger else f"sub:{item.subscription_id}"),
            claims=next_claims,
            previous_confirmed=confirmed_ledger,
            entity_id=(confirmed_ledger.entity_id if confirmed_ledger else f"cik:{item.cik}"),
            symbol=item.symbol or (confirmed_ledger.symbol if confirmed_ledger else None),
            cik=item.cik,
            company_name=item.company_name or (confirmed_ledger.company_name if confirmed_ledger else None),
            source_inbox_item_id=item.inbox_item_id,
            decision_summary=(user_note or f"Accepted radar update: {item.headline}")[:500],
        )

    updated_item = item.model_copy(
        update={
            "review_state": "accepted",
            "user_decision_note": (user_note or "").strip() or None,
            "resulting_ledger_version": new_ledger.version if new_ledger is not None else None,
            "updated_at": now,
        }
    )
    return updated_item, new_ledger


def summarize_radar_health(
    subscriptions: Sequence[FilingWatchSubscription],
    inbox_items: Sequence[ChangeInboxItem],
) -> RadarHealthSummary:
    """Build the Monitoring Health Bar summary (explicitly separates healthy vs degraded/error)."""
    views: list[SubscriptionHealthView] = []
    healthy = degraded = error = enabled = 0
    latest_check: datetime | None = None
    for sub in subscriptions:
        if sub.enabled:
            enabled += 1
        if sub.health_status == "healthy":
            healthy += 1
        elif sub.health_status == "degraded":
            degraded += 1
        else:
            error += 1
        if sub.last_checked_at is not None and (latest_check is None or sub.last_checked_at > latest_check):
            latest_check = sub.last_checked_at
        views.append(
            SubscriptionHealthView(
                subscription_id=sub.subscription_id,
                topic_id=sub.topic_id,
                cik=sub.cik,
                symbol=sub.symbol,
                company_name=sub.company_name,
                enabled=sub.enabled,
                health_status=sub.health_status,
                last_checked_at=sub.last_checked_at,
                last_signal_at=sub.last_signal_at,
                last_error=sub.last_error,
                failed_accession_count=len(sub.failed_accessions),
            )
        )
    unread = sum(1 for it in inbox_items if it.review_state == "unread")
    return RadarHealthSummary(
        total_subscriptions=len(subscriptions),
        enabled_subscriptions=enabled,
        healthy_count=healthy,
        degraded_count=degraded,
        error_count=error,
        unread_inbox_count=unread,
        last_checked_at=latest_check,
        subscriptions=views,
    )


def _peer_trigger_worsened_enough(
    metric_key: str,
    old_obs_str: str | None,
    new_obs_str: str | None,
    operator: str | None,
) -> bool:
    """Hysteresis gate: after a user reviews a peer alert in the same period, only re-alert on material worsening."""
    from decimal import Decimal, InvalidOperation
    if not old_obs_str or not new_obs_str:
        return True
    try:
        old_v = Decimal(str(old_obs_str).strip())
        new_v = Decimal(str(new_obs_str).strip())
    except (InvalidOperation, ValueError):
        return False
    delta = new_v - old_v
    if metric_key.endswith("_bps"):
        # <= ceiling breached when obs > thr -> worsening means delta >= +250 bps; >= floor means delta <= -250 bps
        return (delta >= Decimal("250")) if operator in ("<=", "<") else (delta <= Decimal("-250"))
    if metric_key.endswith("_rank"):
        return (delta >= Decimal("1")) if operator in ("<=", "<") else (delta <= Decimal("-1"))
    return abs(delta) >= Decimal("2.0")


def evaluate_peer_relative_watch(
    *,
    sub: FilingWatchSubscription,
    ledger: ThesisLedger | None,
    existing_inbox: Sequence[ChangeInboxItem],
    peer_compare_fn: Callable[[FilingWatchSubscription, list[str]], Any],
) -> list[ChangeInboxItem]:
    """Pure 0-LLM evaluation of ``peer_*`` WatchTriggers with period-bucket folding & hysteresis."""
    triggers: list[WatchTrigger] = []
    seen_sig: set[tuple[str, str, str]] = set()

    def _collect(trgs: Sequence[WatchTrigger]) -> None:
        for t in trgs:
            if t.kind != "metric_rule" or not t.metric_key or not t.metric_key.startswith("peer_"):
                continue
            sig = (t.metric_key, str(t.operator or ""), str(t.threshold_decimal or ""))
            if sig not in seen_sig:
                seen_sig.add(sig)
                triggers.append(t)

    if ledger is not None:
        for c in ledger.claims:
            _collect(c.watch_triggers)
    if sub.peer_watch_config is not None:
        _collect(sub.peer_watch_config.triggers)
    if not triggers:
        return []

    sym = (sub.symbol or "").strip().upper()
    peer_syms = list(sub.peer_watch_config.symbols) if sub.peer_watch_config else ([sym] if sym else [])
    if not peer_syms:
        return []

    matrix = peer_compare_fn(sub, peer_syms)
    if matrix is None or not getattr(matrix, "companies", None):
        return []

    observed = getattr(matrix, "primary_watch_trigger_metrics", None) or {}
    period_fp = "|".join(f"{c.symbol}:{c.period_end_date or 'na'}" for c in matrix.companies)
    prim = getattr(matrix, "primary_symbol", sym)
    peers_only = [c.symbol for c in matrix.companies if c.symbol != prim]
    compare_url = f"/compare?primary={prim}&peers={','.join(peers_only)}"
    by_thread = {it.event_thread_key: it for it in existing_inbox}

    out: list[ChangeInboxItem] = []
    for trg in triggers:
        ev = evaluate_metric_trigger(trg, observed)
        if ev.last_eval_status != "triggered" or ev.last_observed_value is None:
            continue
        thread_key = f"peer_rel:{sub.cik}:{trg.metric_key}:{period_fp}"
        prev = by_thread.get(thread_key)
        if prev is not None and prev.review_state in ("accepted", "dismissed_with_note"):
            prev_obs = (prev.scorecard_diff or {}).get("observed") if isinstance(prev.scorecard_diff, Mapping) else None
            if not _peer_trigger_worsened_enough(str(trg.metric_key), prev_obs, ev.last_observed_value, trg.operator):
                continue

        headline = f"{prim} 同业相对坐标越界：{trg.metric_key} = {ev.last_observed_value}（健康边界 {trg.operator} {trg.threshold_decimal}）"
        summary = (
            f"{trg.description} 当前观测值 {ev.last_observed_value} 已突破设定边界（{trg.operator} {trg.threshold_decimal}）。"
            f" 同业坐标：{getattr(matrix, 'primary_coordinate_summary', '')}"
        )
        out.append(
            ChangeInboxItem(
                account_id=sub.account_id,
                subscription_id=sub.subscription_id,
                topic_id=sub.topic_id,
                cik=sub.cik,
                symbol=prim,
                company_name=sub.company_name,
                source_channel="peer_relative",
                event_thread_key=thread_key,
                accession=None,
                headline=headline[:400],
                summary=summary[:4000],
                impact_level="high",
                direction="mixed",
                affected_pillars=["valuation"],
                scorecard_diff={
                    "metric_key": trg.metric_key,
                    "operator": trg.operator,
                    "threshold": trg.threshold_decimal,
                    "observed": ev.last_observed_value,
                    "primary_coordinate_summary": getattr(matrix, "primary_coordinate_summary", ""),
                    "compare_url": compare_url,
                    "period_fingerprint": period_fp,
                },
                disclosed_at=_utcnow().date().isoformat(),
            )
        )
    return out


def evaluate_governance_audit_watch(
    *,
    sub: FilingWatchSubscription,
    existing_inbox: Sequence[ChangeInboxItem],
    governance_audit_fn: Callable[[FilingWatchSubscription], Any],
) -> list[ChangeInboxItem]:
    """Pure 0-LLM V2.5 governance radar channel: fires only on high-value governance/capital-allocation alerts."""
    from decimal import Decimal, InvalidOperation
    from .research_governance_audit import load_v25_governance_rules

    report = governance_audit_fn(sub)
    if report is None:
        return []
    rules, _sha, _comp = load_v25_governance_rules()
    cap = report.capital_allocation
    inc = report.incentive_governance
    prim = report.symbol
    fy = (inc.latest_filed_at or _utcnow().date().isoformat())[:4]
    by_thread = {it.event_thread_key: it for it in existing_inbox}

    alerts: list[tuple[str, str, str, InboxImpactLevel]] = []
    if inc.anchor_downgraded_yoy:
        alerts.append((
            "anchor_downgrade",
            f"{prim} 高管激励考核锚降级（DEF 14A {inc.latest_filed_at or ''}）",
            f"最新 DEF 14A 移除或降级了原有资本回报/现金流考核锚，当前最高考核档位为 {inc.highest_anchor_label}。",
            "critical",
        ))
    if inc.mnpi_red_flag:
        alerts.append((
            "mnpi_flag",
            f"{prim} 披露高管股权激励与 MNPI 时点重叠红灯",
            "最新 DEF 14A 勾选了 MnpiDiscTimedForCompValFlag=true，提示存在重大未公开信息窗口期授予股权激励风险。",
            "high",
        ))
    if cap.fake_buyback_flag:
        alerts.append((
            "fake_buyback",
            f"{prim} 触发「假回购真稀释」红灯（3Y净股本 {cap.net_diluted_share_change_3y_pct}%）",
            f"近财年回购支出 ${cap.buybacks_ttm_usd}，但 3 年复权稀释股本仍扩张 {cap.net_diluted_share_change_3y_pct}%，SBC/FCF 为 {cap.sbc_pct_of_fcf}%。",
            "high",
        ))
    if cap.net_diluted_share_change_1y_pct and not cap.possible_ma_equity_issuance:
        try:
            sh1y = Decimal(cap.net_diluted_share_change_1y_pct)
            if sh1y >= Decimal(str(rules.radar_alerts.net_dilution_yoy_worsen_pp)):
                alerts.append((
                    "net_dilution_worsen",
                    f"{prim} 年度净稀释恶化 +{cap.net_diluted_share_change_1y_pct}%",
                    f"单年复权稀释股本净增长 {cap.net_diluted_share_change_1y_pct}%（超过阈值 {rules.radar_alerts.net_dilution_yoy_worsen_pp}pp），SBC/FCF={cap.sbc_pct_of_fcf}%。",
                    "medium",
                ))
        except (InvalidOperation, ValueError):
            pass

    out: list[ChangeInboxItem] = []
    for kind, headline, summary, impact in alerts:
        tkey = f"v25:{sub.cik}:{fy}:{kind}"
        prev = by_thread.get(tkey)
        if prev is not None and prev.review_state in ("accepted", "dismissed_with_note"):
            continue
        out.append(
            ChangeInboxItem(
                account_id=sub.account_id,
                subscription_id=sub.subscription_id,
                topic_id=sub.topic_id,
                cik=sub.cik,
                symbol=prim,
                company_name=sub.company_name or report.company_name,
                source_channel="governance_audit",
                event_thread_key=tkey,
                accession=inc.latest_accession,
                headline=headline[:400],
                summary=summary[:4000],
                impact_level=impact,
                direction="negative",
                affected_pillars=["capital_allocation", "risk"],
                scorecard_diff={
                    "kind": kind,
                    "headline_anchor_summary": report.headline_anchor_summary,
                    "rules_version": report.rules_version,
                    "def14a_accession": inc.latest_accession,
                },
                disclosed_at=inc.latest_filed_at or _utcnow().date().isoformat(),
            )
        )
    return out


# --------------------------------------------------------------------------- P1-B Staggered Two-Phase Sweep

def run_p1b_radar_sweep(
    *,
    subscriptions: Sequence[FilingWatchSubscription],
    scan_fn: Callable[[FilingWatchSubscription, list[str]], tuple[SecEventScanResult, dict[str, str]]],
    analyze_v1_fn: Callable[
        [FilingWatchSubscription, SecEventScanResult, dict[str, str], list[str] | None, str | None],
        SecEventAnalysisResult,
    ],
    ledger_loader: Callable[[FilingWatchSubscription], ThesisLedger | None],
    existing_inbox_loader: Callable[[str], Sequence[ChangeInboxItem]],
    save_subscription: Callable[[FilingWatchSubscription], None],
    save_inbox_item: Callable[[ChangeInboxItem], None],
    earnings_delta_fn: Callable[[FilingWatchSubscription, list[str], ThesisLedger | None], list[ChangeInboxItem]] | None = None,
    peer_compare_fn: Callable[[FilingWatchSubscription, list[str]], Any] | None = None,
    governance_audit_fn: Callable[[FilingWatchSubscription], Any] | None = None,
    as_of: date | None = None,
) -> RadarSweepSummary:
    """Execute P1-B staggered radar sweep:

    1. Phase 1: Fast 0-LLM L1 scan across all enabled subscriptions. Zero-change companies
       immediately update ``last_checked_at`` and exit with 0 LLM tokens.
    2. Phase 2: Priority-sort queued subscriptions (P0 statutory > P1 earnings > P2 governance/M&A/13D > P3 routine)
       and run Layer 2 analysis & unified inbox routing in priority order.
    """
    now = _utcnow()
    outcomes_by_id: dict[str, SubscriptionCheckOutcome] = {}
    l2_queue: list[
        tuple[
            int,
            datetime,
            FilingWatchSubscription,
            SecEventScanResult,
            dict[str, str],
            list[str],
        ]
    ] = []

    zero_change_count = 0
    degraded_count = 0
    error_count = 0
    total_created = 0
    total_updated = 0

    # --- Phase 1: Fast 0-LLM L1 scan (+ 0-LLM peer_relative channel) ---
    for sub in subscriptions:
        if not sub.enabled:
            continue
        seen_v1 = seen_accessions_for_channel(sub, "v1")
        try:
            scan_res, source_texts = scan_fn(sub, seen_v1)
        except Exception as exc:
            error_count += 1
            failed_sub = sub.model_copy(
                update={
                    "health_status": "error",
                    "last_error": f"L1 scan failed: {type(exc).__name__}: {exc}"[:500],
                    "last_checked_at": now,
                    "next_check_at": now + timedelta(minutes=sub.check_interval_minutes),
                    "updated_at": now,
                }
            )
            save_subscription(failed_sub)
            outcomes_by_id[sub.subscription_id] = SubscriptionCheckOutcome(
                subscription_id=sub.subscription_id,
                cik=sub.cik,
                symbol=sub.symbol,
                health_status="error",
                error=failed_sub.last_error,
            )
            continue

        # Detect new earnings accessions routed away from V1
        seen_earn = set(seen_accessions_for_channel(sub, "earnings"))
        new_earnings_accs: list[str] = []
        if "earnings_delta" in sub.watched_channels:
            for sup in scan_res.suppressed_events:
                if sup.suppression_reason == "routed_to_earnings_engine" and sup.accession not in seen_earn:
                    new_earnings_accs.append(sup.accession)

        v1_cands = list(scan_res.target_candidates) if "v1_events" in sub.watched_channels else []

        # Record L1-suppressed accessions as seen for v1 so we don't re-scan them needlessly,
        # and record L1 failed_accessions into failed_accessions (NEVER into seen_accessions).
        new_seen = {k: dict(v) for k, v in sub.seen_accessions.items()}
        new_failed = {k: dict(v) for k, v in sub.failed_accessions.items()}
        for sup in scan_res.suppressed_events:
            if sup.suppression_reason != "routed_to_earnings_engine":
                new_seen.setdefault(sup.accession, {})["v1"] = f"l1_suppressed:{sup.suppression_reason}"
                if sup.accession in new_failed:
                    new_failed[sup.accession].pop("v1", None)
                    if not new_failed[sup.accession]:
                        new_failed.pop(sup.accession, None)
        for fail in scan_res.failed_accessions:
            new_failed.setdefault(fail.accession, {})["v1"] = fail.error_reason

        l1_health: WatchHealthStatus = "degraded" if scan_res.failed_accessions else "healthy"
        l1_err = (
            f"{len(scan_res.failed_accessions)} SEC accession(s) failed in L1"
            if scan_res.failed_accessions
            else None
        )

        # 1b. Optional 0-LLM peer_relative channel (runs even when V1/earnings have 0 new SEC filings)
        peer_created = 0
        peer_updated = 0
        peer_last_sig = sub.last_signal_at
        if peer_compare_fn is not None and "peer_relative" in sub.watched_channels:
            try:
                p_ledger = ledger_loader(sub)
                p_existing = list(existing_inbox_loader(sub.account_id))
                peer_items = evaluate_peer_relative_watch(
                    sub=sub,
                    ledger=p_ledger,
                    existing_inbox=p_existing,
                    peer_compare_fn=peer_compare_fn,
                )
                for p_it in peer_items:
                    persisted_p, is_new_p = upsert_inbox_item_with_folding(p_it, p_existing)
                    if is_new_p:
                        p_existing.append(persisted_p)
                        save_inbox_item(persisted_p)
                        peer_created += 1
                        peer_last_sig = _utcnow()
                    else:
                        save_inbox_item(persisted_p)
                        peer_updated += 1
                        peer_last_sig = _utcnow()
            except Exception as exc:
                l1_health = "degraded" if l1_health == "healthy" else l1_health
                l1_err = f"Peer relative watch failed: {type(exc).__name__}: {exc}"[:500]
        total_created += peer_created
        total_updated += peer_updated

        # 1c. Default-on 0-LLM governance_audit channel (fires only on high-value DEF 14A / 10-K governance alerts)
        gov_created = 0
        gov_updated = 0
        if governance_audit_fn is not None and "governance_audit" in sub.watched_channels:
            try:
                g_existing = list(existing_inbox_loader(sub.account_id))
                gov_items = evaluate_governance_audit_watch(
                    sub=sub,
                    existing_inbox=g_existing,
                    governance_audit_fn=governance_audit_fn,
                )
                for g_it in gov_items:
                    persisted_g, is_new_g = upsert_inbox_item_with_folding(g_it, g_existing)
                    if is_new_g:
                        g_existing.append(persisted_g)
                        save_inbox_item(persisted_g)
                        gov_created += 1
                        peer_last_sig = _utcnow()
                    else:
                        save_inbox_item(persisted_g)
                        gov_updated += 1
                        peer_last_sig = _utcnow()
            except Exception as exc:
                l1_health = "degraded" if l1_health == "healthy" else l1_health
                l1_err = f"Governance audit watch failed: {type(exc).__name__}: {exc}"[:500]
        total_created += gov_created
        total_updated += gov_updated

        if not v1_cands and not new_earnings_accs:
            if peer_created == 0 and peer_updated == 0 and gov_created == 0 and gov_updated == 0:
                zero_change_count += 1
            if l1_health == "degraded":
                degraded_count += 1
            updated_sub = sub.model_copy(
                update={
                    "seen_accessions": new_seen,
                    "failed_accessions": new_failed,
                    "health_status": l1_health,
                    "last_error": l1_err,
                    "last_checked_at": now,
                    "next_check_at": now + timedelta(minutes=sub.check_interval_minutes),
                    "last_signal_at": peer_last_sig,
                    "updated_at": now,
                }
            )
            save_subscription(updated_sub)
            outcomes_by_id[sub.subscription_id] = SubscriptionCheckOutcome(
                subscription_id=sub.subscription_id,
                cik=sub.cik,
                symbol=sub.symbol,
                l1_candidates_found=0,
                earnings_candidates_found=0,
                l2_executed=False,
                inbox_items_created=peer_created + gov_created,
                inbox_items_updated=peer_updated + gov_updated,
                health_status=l1_health,
                error=l1_err,
            )
            continue

        prio = compute_candidate_priority_tier(v1_cands, has_new_earnings=bool(new_earnings_accs))
        staged_sub = sub.model_copy(
            update={
                "seen_accessions": new_seen,
                "failed_accessions": new_failed,
                "last_signal_at": peer_last_sig,
            }
        )
        l2_queue.append((prio, sub.last_checked_at or datetime.min.replace(tzinfo=timezone.utc), staged_sub, scan_res, source_texts, new_earnings_accs))

    # --- Phase 2: Priority-ordered L2 execution ---
    l2_queue.sort(key=lambda entry: (entry[0], entry[1]))

    for prio, _last_chk, sub, scan_res, source_texts, new_earnings_accs in l2_queue:
        ledger = ledger_loader(sub)
        active_flags, thesis_ctx = build_v1_thesis_context(ledger)
        existing_inbox = list(existing_inbox_loader(sub.account_id))
        new_seen = {k: dict(v) for k, v in sub.seen_accessions.items()}
        new_failed = {k: dict(v) for k, v in sub.failed_accessions.items()}

        created_here = 0
        updated_here = 0
        sub_health: WatchHealthStatus = "degraded" if scan_res.failed_accessions else "healthy"
        sub_err: str | None = (
            f"{len(scan_res.failed_accessions)} SEC accession(s) failed in L1"
            if scan_res.failed_accessions
            else None
        )
        last_sig_at = sub.last_signal_at

        # 2a. V1 Events Channel
        if "v1_events" in sub.watched_channels and scan_res.target_candidates:
            try:
                analysis = analyze_v1_fn(sub, scan_res, source_texts, active_flags, thesis_ctx)
                covered_accs: dict[str, str] = {}
                for sig in analysis.signals:
                    acc = sig.provenance.accession
                    covered_accs[acc] = sig.disposition
                    if sig.disposition == "push_alert" or sig.fail_safe_triggered:
                        candidate_item = build_inbox_item_from_v1_signal(
                            account_id=sub.account_id,
                            subscription=sub,
                            signal=sig,
                            ledger=ledger,
                        )
                        persisted_item, is_new = upsert_inbox_item_with_folding(candidate_item, existing_inbox)
                        if is_new:
                            existing_inbox.append(persisted_item)
                            save_inbox_item(persisted_item)
                            created_here += 1
                            last_sig_at = _utcnow()
                        elif persisted_item.accession != candidate_item.accession or persisted_item.updated_at != candidate_item.updated_at:
                            save_inbox_item(persisted_item)
                            updated_here += 1
                            last_sig_at = _utcnow()

                # Mark covered accessions seen; any candidate NOT covered stays failed for retry
                for cand in scan_res.target_candidates:
                    if cand.accession in covered_accs:
                        new_seen.setdefault(cand.accession, {})["v1"] = covered_accs[cand.accession]
                        if cand.accession in new_failed:
                            new_failed[cand.accession].pop("v1", None)
                            if not new_failed[cand.accession]:
                                new_failed.pop(cand.accession, None)
                    else:
                        new_failed.setdefault(cand.accession, {})["v1"] = "l2_uncovered"
                        sub_health = "degraded"
                        sub_err = (
                            "; ".join(analysis.open_gaps[:3])
                            if analysis.open_gaps
                            else f"Uncovered candidate {cand.accession}"
                        )[:500]
            except Exception as exc:
                sub_health = "error"
                sub_err = f"V1 L2 analysis failed: {type(exc).__name__}: {exc}"[:500]
                for cand in scan_res.target_candidates:
                    new_failed.setdefault(cand.accession, {})["v1"] = type(exc).__name__

        # 2b. Earnings Delta Channel (wired in Step B2 when earnings_delta_fn is provided)
        if earnings_delta_fn is not None and new_earnings_accs and "earnings_delta" in sub.watched_channels:
            try:
                delta_items = earnings_delta_fn(sub, new_earnings_accs, ledger)
                for d_item in delta_items:
                    persisted_item, is_new = upsert_inbox_item_with_folding(d_item, existing_inbox)
                    if is_new:
                        existing_inbox.append(persisted_item)
                        save_inbox_item(persisted_item)
                        created_here += 1
                        last_sig_at = _utcnow()
                    else:
                        save_inbox_item(persisted_item)
                        updated_here += 1
                        last_sig_at = _utcnow()
                for acc in new_earnings_accs:
                    new_seen.setdefault(acc, {})["earnings"] = "delta_reviewed"
                    if acc in new_failed:
                        new_failed[acc].pop("earnings", None)
                        if not new_failed[acc]:
                            new_failed.pop(acc, None)
            except Exception as exc:
                sub_health = "degraded" if sub_health == "healthy" else sub_health
                sub_err = f"Earnings delta failed: {type(exc).__name__}: {exc}"[:500]
                for acc in new_earnings_accs:
                    new_failed.setdefault(acc, {})["earnings"] = type(exc).__name__

        if sub_health == "degraded":
            degraded_count += 1
        elif sub_health == "error":
            error_count += 1

        total_created += created_here
        total_updated += updated_here

        done_now = _utcnow()
        final_sub = sub.model_copy(
            update={
                "seen_accessions": new_seen,
                "failed_accessions": new_failed,
                "health_status": sub_health,
                "last_error": sub_err,
                "last_checked_at": done_now,
                "next_check_at": done_now + timedelta(minutes=sub.check_interval_minutes),
                "last_signal_at": last_sig_at,
                "updated_at": done_now,
            }
        )
        save_subscription(final_sub)
        outcomes_by_id[sub.subscription_id] = SubscriptionCheckOutcome(
            subscription_id=sub.subscription_id,
            cik=sub.cik,
            symbol=sub.symbol,
            priority_tier=prio,
            l1_candidates_found=len(scan_res.target_candidates),
            earnings_candidates_found=len(new_earnings_accs),
            l2_executed=True,
            inbox_items_created=created_here,
            inbox_items_updated=updated_here,
            health_status=sub_health,
            error=sub_err,
        )

    ordered_outcomes = [outcomes_by_id[s.subscription_id] for s in subscriptions if s.subscription_id in outcomes_by_id]
    return RadarSweepSummary(
        subscriptions_checked=len(ordered_outcomes),
        l1_zero_change_count=zero_change_count,
        l2_queued_count=len(l2_queue),
        inbox_items_created=total_created,
        inbox_items_updated=total_updated,
        degraded_count=degraded_count,
        error_count=error_count,
        outcomes=ordered_outcomes,
    )
