"""Versioned Thesis Ledger, Watch Triggers, and Dossier-to-Thesis Draft Extraction (Direction B).

Design invariants:
- Independent of chat context and persisted in PostgreSQL ``research_state_documents``
  (collection ``thesis_ledgers``).
- Background radar or delta-review jobs NEVER silently overwrite a user-confirmed
  ``ThesisLedger`` (``review_status == "confirmed"``). Updates are staged via
  ``ChangeInboxItem`` and only advance ``version`` (``v(N) -> v(N+1)``) upon
  explicit user confirmation.
- All nullable fields default to ``None`` for ``_drop_legacy_null_fields`` safety.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Mapping, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

ThesisPillar = Literal[
    "growth",
    "margin",
    "cash_quality",
    "capital_allocation",
    "valuation",
    "risk",
]
ThesisStance = Literal["bull", "cautious", "bear", "neutral"]
ClaimStatus = Literal["active", "strengthened", "weakened", "invalidated", "superseded"]
LedgerReviewStatus = Literal["draft", "confirmed"]
TriggerKind = Literal["metric_rule", "disclosure_rule"]
TriggerOperator = Literal[">=", "<=", ">", "<", "delta_bps", "delta_pp"]
TriggerEvalStatus = Literal["triggered", "cleared", "unchanged", "awaiting_10q", "not_evaluated"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class _FrozenLedgerModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class WatchTrigger(_FrozenLedgerModel):
    """Observable metric or disclosure condition attached to a ``ThesisClaim``."""

    trigger_id: str = Field(default_factory=lambda: _new_id("trg"), min_length=1, max_length=120)
    kind: TriggerKind = "disclosure_rule"
    description: str = Field(min_length=1, max_length=600)
    # Deterministic Decimal metric rule fields (used when kind == "metric_rule")
    metric_key: str | None = None
    operator: TriggerOperator | None = None
    threshold_decimal: str | None = None
    horizon: str | None = None
    # Disclosure rule fields (used when kind == "disclosure_rule")
    target_sections: list[str] = Field(default_factory=list)
    last_eval_status: TriggerEvalStatus = "not_evaluated"
    last_observed_value: str | None = None
    last_evaluated_at: datetime | None = None


class ThesisClaim(_FrozenLedgerModel):
    """Single investment thesis pillar claim with explicit invalidation trigger."""

    claim_id: str = Field(default_factory=lambda: _new_id("clm"), min_length=1, max_length=120)
    pillar: ThesisPillar
    statement: str = Field(min_length=1, max_length=2000)
    stance: ThesisStance = "neutral"
    watch_triggers: list[WatchTrigger] = Field(default_factory=list)
    invalidation_condition: str = Field(min_length=1, max_length=1500)
    status: ClaimStatus = "active"
    evidence_refs: list[str] = Field(default_factory=list)
    user_note: str | None = None


class ThesisLedger(_FrozenLedgerModel):
    """Versioned, account-isolated investment thesis ledger anchored to a company/topic."""

    ledger_id: str = Field(default_factory=lambda: _new_id("ldg"), min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=120)
    topic_id: str = Field(min_length=1, max_length=120)
    entity_id: str = Field(min_length=1, max_length=120)
    symbol: str | None = None
    cik: str | None = None
    company_name: str | None = None
    version: int = Field(default=1, ge=1)
    parent_version: int | None = None
    review_status: LedgerReviewStatus = "draft"
    base_dossier_id: str | None = None
    base_period_label: str | None = None
    claims: list[ThesisClaim] = Field(default_factory=list)
    active_red_yellow_flags: list[str] = Field(default_factory=list)
    source_inbox_item_id: str | None = None
    decision_summary: str | None = None
    confirmed_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class ClaimDiffEntry(_FrozenLedgerModel):
    """Field-level difference for a single claim between two ledger versions."""

    claim_id: str
    pillar: ThesisPillar
    change_kind: Literal["added", "removed", "modified", "unchanged"]
    old_statement: str | None = None
    new_statement: str | None = None
    old_status: ClaimStatus | None = None
    new_status: ClaimStatus | None = None
    old_stance: ThesisStance | None = None
    new_stance: ThesisStance | None = None
    user_note: str | None = None


class ThesisLedgerDiff(_FrozenLedgerModel):
    """Auditable diff between ``from_version`` and ``to_version`` of a ``ThesisLedger``."""

    topic_id: str
    from_version: int
    to_version: int
    entries: list[ClaimDiffEntry] = Field(default_factory=list)
    decision_summary: str | None = None
    source_inbox_item_id: str | None = None


class ThesisLedgerHistoryView(_FrozenLedgerModel):
    """API response envelope exposing latest confirmed ledger, draft, and version history."""

    topic_id: str
    latest_confirmed: ThesisLedger | None = None
    pending_draft: ThesisLedger | None = None
    versions: list[ThesisLedger] = Field(default_factory=list)
    latest_diff: ThesisLedgerDiff | None = None


# --------------------------------------------------------------------------- Helpers

_WORKER_TO_PILLAR: dict[str, ThesisPillar] = {
    "growth_demand": "growth",
    "revenue_growth": "growth",
    "margin_cost": "margin",
    "profitability": "margin",
    "cash_quality": "cash_quality",
    "working_capital": "cash_quality",
    "balance_sheet": "cash_quality",
    "capital_allocation": "capital_allocation",
    "valuation": "valuation",
    "valuation_peer": "valuation",
    "risk": "risk",
    "governance_risk": "risk",
}


def _to_mapping(obj: Any) -> Mapping[str, Any]:
    if isinstance(obj, Mapping):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return {}


def _infer_pillar(text: str, role_or_tag: str = "") -> ThesisPillar:
    low_role = (role_or_tag or "").strip().lower()
    if low_role in _WORKER_TO_PILLAR:
        return _WORKER_TO_PILLAR[low_role]
    low = f"{low_role} {text}".lower()
    if any(k in low for k in ("cash", "ocf", "fcf", "receivable", "inventory", "dso", "working capital", "现金流", "应收")):
        return "cash_quality"
    if any(k in low for k in ("margin", "gross", "operating", "cost", "opex", "毛利", "利润率")):
        return "margin"
    if any(k in low for k in ("valuation", "dcf", "cagr", "multiple", "pe ", "ev/", "估值")):
        return "valuation"
    if any(k in low for k in ("buyback", "dividend", "capex", "debt", "m&a", "acqui", "资本配置", "回购", "发债")):
        return "capital_allocation"
    if any(k in low for k in ("risk", "litigation", "lawsuit", "cyber", "regulator", "auditor", "governance", "诉讼", "风险", "监管")):
        return "risk"
    return "growth"


def _extract_red_yellow_flags(snapshot: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    raw_flags = snapshot.get("flags") or []
    if isinstance(raw_flags, Sequence) and not isinstance(raw_flags, (str, bytes)):
        for f in raw_flags:
            fm = _to_mapping(f)
            sev = str(fm.get("severity") or fm.get("level") or fm.get("status") or "").strip().lower()
            if sev in ("red", "yellow", "high", "warning", "critical"):
                code = str(fm.get("code") or fm.get("flag_id") or fm.get("name") or "").strip()
                msg = str(fm.get("label") or fm.get("summary") or fm.get("message") or "").strip()
                label = f"[{sev.upper()}] {code}: {msg}" if code and msg else (msg or code)
                if label:
                    out.append(label[:200])
    return list(dict.fromkeys(out))[:12]


def _parse_decimal_safe(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).strip().replace(",", "").replace("%", ""))
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def evaluate_metric_trigger(
    trigger: WatchTrigger,
    observed_metrics: Mapping[str, Any],
    *,
    is_10q_available: bool = True,
) -> WatchTrigger:
    """Deterministically evaluate a ``metric_rule`` WatchTrigger using Decimal arithmetic."""
    if trigger.kind != "metric_rule" or not trigger.metric_key or not trigger.operator or trigger.threshold_decimal is None:
        return trigger
    now = _utcnow()
    raw_obs = observed_metrics.get(trigger.metric_key)
    if raw_obs is None:
        status: TriggerEvalStatus = "unchanged" if is_10q_available else "awaiting_10q"
        return trigger.model_copy(
            update={"last_eval_status": status, "last_observed_value": None, "last_evaluated_at": now}
        )
    obs = _parse_decimal_safe(raw_obs)
    thr = _parse_decimal_safe(trigger.threshold_decimal)
    if obs is None or thr is None:
        return trigger.model_copy(
            update={"last_eval_status": "not_evaluated", "last_observed_value": str(raw_obs), "last_evaluated_at": now}
        )

    op = trigger.operator
    breached = False
    if op == ">=":
        # Trigger represents healthy floor (e.g. gross_margin >= 74.0): breached if obs < thr
        breached = obs < thr
    elif op == ">":
        breached = obs <= thr
    elif op == "<=":
        # Trigger represents ceiling (e.g. ar_minus_rev_pp <= 5.0): breached if obs > thr
        breached = obs > thr
    elif op == "<":
        breached = obs >= thr
    elif op in ("delta_bps", "delta_pp"):
        # Positive threshold means drop/shift magnitude exceeds threshold
        breached = abs(obs) >= abs(thr)

    return trigger.model_copy(
        update={
            "last_eval_status": "triggered" if breached else "cleared",
            "last_observed_value": format(obs, "f"),
            "last_evaluated_at": now,
        }
    )


def build_thesis_draft_from_dossier(
    *,
    account_id: str,
    topic_id: str,
    dossier: Any,
    existing_version: int = 0,
) -> ThesisLedger:
    """Deterministically extract a structured 3-5 claim ``ThesisLedger`` draft from a locked ``EarningsDossier``.

    Runs with 0 LLM tokens so users get an instant, evidence-grounded draft anchored directly
    to ``bear_cases[].next_quarter_invalidation_trigger``, ``evidence_cards``, ``flags``, and
    ``reverse_dcf``.
    """
    dm = _to_mapping(dossier)
    snapshot = _to_mapping(dm.get("snapshot"))
    entity_id = str(dm.get("entity_id") or snapshot.get("entity_id") or f"topic:{topic_id}")
    symbol = str(dm.get("symbol") or snapshot.get("symbol") or "").strip().upper() or None
    cik = str(dm.get("cik") or snapshot.get("cik") or "").strip() or None
    company_name = str(dm.get("company_name") or snapshot.get("company_name") or "").strip() or None
    dossier_id = str(dm.get("dossier_id") or "").strip() or None
    period_label = str(dm.get("period_label") or snapshot.get("period_label") or "").strip() or None
    red_yellow_flags = _extract_red_yellow_flags(snapshot)

    claims_by_pillar: dict[ThesisPillar, ThesisClaim] = {}

    # 1. Seed high-signal claims from bear_cases (each already carries an explicit invalidation trigger)
    bear_cases = dm.get("bear_cases") or []
    for idx, raw_bear in enumerate(bear_cases):
        bm = _to_mapping(raw_bear)
        challenge = str(bm.get("thesis_challenge") or "").strip()
        invalidation = str(bm.get("next_quarter_invalidation_trigger") or "").strip()
        if not challenge or not invalidation:
            continue
        pillar = _infer_pillar(f"{challenge} {invalidation}", str(bm.get("anchor_kind") or ""))
        if pillar in claims_by_pillar:
            continue
        anchors = [str(a) for a in (bm.get("anchored_evidence_ids") or []) if str(a).strip()]
        if dossier_id:
            anchors.append(dossier_id)
        sev = str(bm.get("severity") or "medium").lower()
        stance: ThesisStance = "cautious" if sev in ("high", "medium") else "neutral"
        claims_by_pillar[pillar] = ThesisClaim(
            claim_id=f"clm_{pillar}_{idx + 1}",
            pillar=pillar,
            statement=challenge[:1800],
            stance=stance,
            watch_triggers=[
                WatchTrigger(
                    trigger_id=f"trg_{pillar}_inv_{idx + 1}",
                    kind="disclosure_rule",
                    description=invalidation[:550],
                    target_sections=["8-K EX-99.1", "10-Q MD&A", "8-K Material Events"],
                )
            ],
            invalidation_condition=invalidation[:1400],
            status="active",
            evidence_refs=list(dict.fromkeys(anchors))[:8],
        )
        if len(claims_by_pillar) >= 4:
            break

    # 2. Supplement from evidence_cards for uncovered core pillars (growth / margin / cash_quality)
    evidence_cards = dm.get("evidence_cards") or []
    for idx, raw_card in enumerate(evidence_cards):
        if len(claims_by_pillar) >= 5:
            break
        cm = _to_mapping(raw_card)
        finding = str(cm.get("finding") or "").strip()
        if not finding:
            continue
        role = str(cm.get("worker_role") or cm.get("topic_tag") or "")
        pillar = _infer_pillar(finding, role)
        if pillar in claims_by_pillar:
            continue
        caveat = str(cm.get("caveat") or "").strip()
        inv = caveat or f"Next-quarter filing contradicts core {pillar} finding or triggers negative diagnostic flag."
        refs = [str(cm.get("card_id"))] if cm.get("card_id") else []
        if dossier_id:
            refs.append(dossier_id)
        claims_by_pillar[pillar] = ThesisClaim(
            claim_id=f"clm_{pillar}_ev_{idx + 1}",
            pillar=pillar,
            statement=finding[:1800],
            stance="bull",
            watch_triggers=[
                WatchTrigger(
                    trigger_id=f"trg_{pillar}_ev_{idx + 1}",
                    kind="disclosure_rule",
                    description=inv[:550],
                    target_sections=["8-K EX-99.1", "10-Q", "10-K"],
                )
            ],
            invalidation_condition=inv[:1400],
            status="active",
            evidence_refs=list(dict.fromkeys(refs))[:8],
        )

    # 3. Add deterministic Reverse-DCF OR V4a Bank Gordon P/TBV valuation pillar if uncovered
    valuation = _to_mapping(snapshot.get("valuation"))
    rdcf = _to_mapping(valuation.get("reverse_dcf"))
    implied_cagr = _parse_decimal_safe(rdcf.get("implied_5y_fcf_cagr_pct"))
    bank_val = _to_mapping(snapshot.get("bank_valuation"))
    bank_diag = _to_mapping(snapshot.get("bank_diagnostics"))
    if bank_val and "valuation" not in claims_by_pillar and len(claims_by_pillar) < 5:
        p_tbv = str(bank_val.get("p_to_tbv_primary") or "n/a")
        imp_rotce = _parse_decimal_safe(bank_val.get("implied_rotce_pct"))
        cur_rotce = _parse_decimal_safe(bank_val.get("current_rotce_pct"))
        spread_pp = _parse_decimal_safe(bank_val.get("rotce_spread_vs_implied_pp"))
        cet1_buf = _parse_decimal_safe(bank_diag.get("cet1_buffer_bps")) if bank_diag else None
        triggers_b: list[WatchTrigger] = [
            WatchTrigger(
                trigger_id="trg_bank_val_rotce_spread",
                kind="metric_rule",
                description=(
                    f"Bank ROTCE vs Gordon Implied ROTCE spread shifts >= 1.50pp "
                    f"(baseline spread={spread_pp if spread_pp is not None else 'n/a'}pp, Ke={bank_val.get('coe_pct', '10.00')}%, g={bank_val.get('terminal_g_pct', '3.00')}%)"
                ),
                metric_key="peer_rotce_spread_pp",
                operator="<=",
                threshold_decimal="-1.50",
                horizon="next_quarter",
            ),
        ]
        if cet1_buf is not None:
            triggers_b.append(
                WatchTrigger(
                    trigger_id="trg_bank_cet1_buffer",
                    kind="metric_rule",
                    description="CET1 standardized buffer compresses below 100 bps regulatory cushion",
                    metric_key="cet1_buffer_bps",
                    operator="<",
                    threshold_decimal="100",
                    horizon="next_quarter",
                )
            )
        claims_by_pillar["valuation"] = ThesisClaim(
            claim_id="clm_valuation_bank_gordon",
            pillar="valuation",
            statement=(
                f"Bank trades at {p_tbv}x P/TBV (Reported Market Convention), implying {imp_rotce if imp_rotce is not None else 'n/a'}% "
                f"sustainable ROTCE vs {cur_rotce if cur_rotce is not None else 'n/a'}% current ROTCE "
                f"(CET1 buffer +{cet1_buf if cet1_buf is not None else 'n/a'} bps)."
            ),
            stance="bull" if (spread_pp is not None and spread_pp >= Decimal("1.50")) else ("cautious" if (spread_pp is not None and spread_pp <= Decimal("-1.50")) else "neutral"),
            watch_triggers=triggers_b,
            invalidation_condition=(
                "CET1 standardized buffer drops below 100 bps, HTM overhang escalates to red, or sustainable ROTCE falls below Gordon implied ROTCE."
            ),
            status="active",
            evidence_refs=[dossier_id] if dossier_id else [],
        )
    elif implied_cagr is not None and "valuation" not in claims_by_pillar and len(claims_by_pillar) < 5:
        cagr_str = f"{implied_cagr:.2f}"
        claims_by_pillar["valuation"] = ThesisClaim(
            claim_id="clm_valuation_rdcf",
            pillar="valuation",
            statement=(
                f"Current valuation implies ~{cagr_str}% 5-year FCF CAGR (Reverse-DCF anchor from {period_label or 'latest dossier'}). "
                "Monitor whether forward guidance and FCF conversion sustain this implied growth bar."
            ),
            stance="neutral",
            watch_triggers=[
                WatchTrigger(
                    trigger_id="trg_valuation_rdcf",
                    kind="metric_rule",
                    description=f"Reverse-DCF implied 5Y FCF CAGR drift exceeds 500 bps vs baseline {cagr_str}%",
                    metric_key="implied_5y_fcf_cagr_delta_bps",
                    operator="delta_bps",
                    threshold_decimal="500",
                    horizon="next_quarter",
                )
            ],
            invalidation_condition=(
                f"Implied 5-year FCF CAGR widens by >= 500 bps without matching upward guidance or TTM FCF expansion."
            ),
            status="active",
            evidence_refs=[dossier_id] if dossier_id else [],
        )

    # Guarantee at least 1 claim even on sparse dossiers
    if not claims_by_pillar:
        claims_by_pillar["growth"] = ThesisClaim(
            claim_id="clm_growth_default",
            pillar="growth",
            statement=f"Track {symbol or entity_id} revenue growth, margin trajectory, and material SEC disclosures.",
            stance="neutral",
            watch_triggers=[
                WatchTrigger(
                    trigger_id="trg_growth_default",
                    kind="disclosure_rule",
                    description="Any material 8-K, guidance cut, or 10-Q/10-K deterioration.",
                    target_sections=["8-K", "10-Q", "10-K"],
                )
            ],
            invalidation_condition="Material downward guidance revision or statutory adverse SEC disclosure (4.01/4.02/1.05).",
            status="active",
            evidence_refs=[dossier_id] if dossier_id else [],
        )

    next_ver = max(1, existing_version + 1)
    return ThesisLedger(
        account_id=account_id,
        topic_id=topic_id,
        entity_id=entity_id,
        symbol=symbol,
        cik=cik,
        company_name=company_name,
        version=next_ver,
        parent_version=existing_version if existing_version > 0 else None,
        review_status="draft",
        base_dossier_id=dossier_id,
        base_period_label=period_label,
        claims=list(claims_by_pillar.values()),
        active_red_yellow_flags=red_yellow_flags,
    )


def confirm_thesis_ledger_version(
    *,
    account_id: str,
    topic_id: str,
    claims: Sequence[ThesisClaim],
    previous_confirmed: ThesisLedger | None = None,
    base_draft: ThesisLedger | None = None,
    entity_id: str | None = None,
    symbol: str | None = None,
    cik: str | None = None,
    company_name: str | None = None,
    source_inbox_item_id: str | None = None,
    decision_summary: str | None = None,
) -> ThesisLedger:
    """Create a new immutable user-confirmed ``ThesisLedger`` version ``v(N+1)``."""
    ref = previous_confirmed or base_draft
    prev_ver = previous_confirmed.version if previous_confirmed is not None else 0
    return ThesisLedger(
        account_id=account_id,
        topic_id=topic_id,
        entity_id=entity_id or (ref.entity_id if ref else f"topic:{topic_id}"),
        symbol=(symbol or (ref.symbol if ref else None)),
        cik=(cik or (ref.cik if ref else None)),
        company_name=(company_name or (ref.company_name if ref else None)),
        version=prev_ver + 1,
        parent_version=prev_ver if prev_ver > 0 else None,
        review_status="confirmed",
        base_dossier_id=ref.base_dossier_id if ref else None,
        base_period_label=ref.base_period_label if ref else None,
        claims=list(claims),
        active_red_yellow_flags=list(ref.active_red_yellow_flags) if ref else [],
        source_inbox_item_id=source_inbox_item_id,
        decision_summary=decision_summary,
        confirmed_at=_utcnow(),
    )


def diff_thesis_ledgers(older: ThesisLedger, newer: ThesisLedger) -> ThesisLedgerDiff:
    """Compute a deterministic claim-by-claim diff between two confirmed ledger versions."""
    old_by_id = {c.claim_id: c for c in older.claims}
    new_by_id = {c.claim_id: c for c in newer.claims}
    entries: list[ClaimDiffEntry] = []

    for cid, new_c in new_by_id.items():
        old_c = old_by_id.get(cid)
        if old_c is None:
            entries.append(
                ClaimDiffEntry(
                    claim_id=cid,
                    pillar=new_c.pillar,
                    change_kind="added",
                    new_statement=new_c.statement,
                    new_status=new_c.status,
                    new_stance=new_c.stance,
                    user_note=new_c.user_note,
                )
            )
        elif (
            old_c.statement != new_c.statement
            or old_c.status != new_c.status
            or old_c.stance != new_c.stance
            or old_c.invalidation_condition != new_c.invalidation_condition
            or old_c.user_note != new_c.user_note
        ):
            entries.append(
                ClaimDiffEntry(
                    claim_id=cid,
                    pillar=new_c.pillar,
                    change_kind="modified",
                    old_statement=old_c.statement,
                    new_statement=new_c.statement,
                    old_status=old_c.status,
                    new_status=new_c.status,
                    old_stance=old_c.stance,
                    new_stance=new_c.stance,
                    user_note=new_c.user_note,
                )
            )
        else:
            entries.append(
                ClaimDiffEntry(
                    claim_id=cid,
                    pillar=new_c.pillar,
                    change_kind="unchanged",
                    old_statement=old_c.statement,
                    new_statement=new_c.statement,
                    old_status=old_c.status,
                    new_status=new_c.status,
                    old_stance=old_c.stance,
                    new_stance=new_c.stance,
                )
            )

    for cid, old_c in old_by_id.items():
        if cid not in new_by_id:
            entries.append(
                ClaimDiffEntry(
                    claim_id=cid,
                    pillar=old_c.pillar,
                    change_kind="removed",
                    old_statement=old_c.statement,
                    old_status=old_c.status,
                    old_stance=old_c.stance,
                )
            )

    return ThesisLedgerDiff(
        topic_id=newer.topic_id,
        from_version=older.version,
        to_version=newer.version,
        entries=entries,
        decision_summary=newer.decision_summary,
        source_inbox_item_id=newer.source_inbox_item_id,
    )


def build_v1_thesis_context(ledger: ThesisLedger | None) -> tuple[list[str] | None, str | None]:
    """Project a confirmed ``ThesisLedger`` into ``(active_red_yellow_flags, thesis_context)`` for V1 Layer 2."""
    if ledger is None:
        return None, None
    flags = list(ledger.active_red_yellow_flags) or None
    lines: list[str] = [
        f"Active Thesis Ledger v{ledger.version} ({ledger.symbol or ledger.entity_id}, status={ledger.review_status}):"
    ]
    for claim in ledger.claims:
        if claim.status == "superseded":
            continue
        lines.append(
            f"- [{claim.pillar}/{claim.stance}/{claim.status}] {claim.statement[:260]} "
            f"| Invalidation: {claim.invalidation_condition[:200]}"
        )
    return flags, "\n".join(lines) if len(lines) > 1 else None
