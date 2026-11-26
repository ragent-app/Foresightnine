"""V3a Layer 1 (SEC 10-Q Collision Ledger + Stateless XBRL Library) & Layer 3 (7-Track Decimal Collision Engine).

Zero-LLM (<10ms hot Layer 3 re-evaluation):
- Stateless shared XBRL actuals query library (``xbrl_actuals_lookup``) covering 12 canonical metrics
  with initial-filing basis reconstruction (``filed <= initial_filing_cutoff`` BEFORE ``_derive_ytd_quarters``
  to prevent post-restatement / post-split false alarms), ``restated_later`` notice (>1%), strict
  GAAP/Non-GAAP basis lock, universal ISO-4217 currency lock, and ``as_of`` anti-lookahead.
- Dual-anchored fiscal period calibration ``P`` via Layer 0 ``call_date`` + SEC ``submissions``
  0-65 day ``reportDate`` window + ``P+1`` four-timestamp hindsight grading.
- 64KB streaming 10-Q/10-K redline helper (``HEAVY_MEM_GATE`` + ``sec_get_to_tempfile`` +
  ``stream_strip_sec_html_file``) and lightweight ``FilingSummary.xml`` + <=3 ``R*.htm`` (<=1.5MB)
  footnote fallback.
- Layer 3 zero-drift citation grounding (``find_quotation_span_with_raw`` local-window first +
  ``page_for_offset`` + 2-tuple ``check_negation_context``) and 7-track Decimal collision engine:
  Track 0 (Internal Contradiction & CEO/CFO Divergence), Track 1 (Demand & Channel Health),
  Track 2 (Earnings & Cash Quality + Non-GAAP gap), Track 3 (CapEx & ASC 440 Supply Commitments),
  Track 4 (V2.5 Capital Allocation read-only cross-check), Track 5 (10-Q Item 1A dual-factor risk-bucket
  + stemmed-entity omission screening), Track 6 (12-metric x 7-operator verbal commitments, Guidance
  Range Drift, Silent Drop tiering, Speaker-Baseline Shift), Tab 2 Q&A Evasion Audit (D3 dual-condition
  confidentiality whitelist + selective multi-part omission), Tab 3 4-tier expectation isolation, and
  export-permission-aware Markdown audit brief renderer.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
from pathlib import Path
import re
from typing import Any, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .fin_data.adapters.sec_edgar import STANDARD_LINES, Period, select_line
from .research_documents import page_for_offset
from .research_earnings_diagnostics import evaluate_sec_sic_gate
from .research_llm_primitives import (
    DEFAULT_NEGATION_EXCEPTION_MARKERS,
    check_negation_context,
    find_quotation_span_with_raw,
)
from .research_memory_gate import heavy_memory_section
from .research_peer_compare import PeerCompanySnapshot
from .research_retrieval import tokenize_query
from .research_transcript_parser import (
    CallDefaultBasis,
    CompiledV3aRules,
    ExecutiveBaselineStats,
    ParsedCallDocument,
    SpeakerRole,
    load_v3a_call_rules,
)

# --------------------------------------------------------------------------- Shared Enums & Helpers

CollisionSeverity = Literal["red", "yellow", "green", "info", "not_applicable"]
BasisType = Literal["gaap", "non_gaap", "operating_kpi", "unknown", "inherited_call_default"]
ClaimScope = Literal["consolidated", "segment", "product_line", "geography"]
ConfidentialityPolicyKind = Literal["none", "strict_nda_legal", "segment_disclosure_policy"]
PeriodScope = Literal["single_quarter", "ytd", "full_year", "ttm", "multi_year", "point_in_time", "unspecified"]
EvidenceSufficiency = Literal["explicit", "implied", "ambiguous"]
CommitmentOperator = Literal[
    "ge",
    "le",
    "between",
    "approx",
    "growth_yoy_ge",
    "growth_yoy_le",
    "growth_yoy_approx",
    "growth_yoy_between",
    "direction_up_down",
]
VerificationTier = Literal[
    "xbrl_auto",
    "8k_nongaap_manual",
    "next_call_check",
    "operating_kpi_next_call",
    "qualitative_milestone",
]
CommitmentStatus = Literal[
    "pending_10q",
    "pending_next_call",
    "met",
    "missed",
    "manual_nongaap_review",
    "silent_drop_high_risk",
    "likely_concluded_or_unasked",
    "muted",
]
EvasionLevel = Literal["direct", "intermediate", "fully_evasive"]
EvasionSubtype = Literal[
    "none",
    "omission",
    "selective_part_omission",
    "vagueness_obfuscation",
    "topic_pivot",
    "restatement",
]
LedgerMatchStatus = Literal[
    "same_period_10q_matched",
    "awaiting_current_10q_using_8k_and_prior_10q",
    "fpi_20f_6k_limited",
    "financial_sic_gated",
    "non_sec_entity",
]

TWELVE_XBRL_METRICS: tuple[str, ...] = (
    "revenue",
    "gross_margin",
    "operating_margin",
    "operating_income",
    "net_income",
    "eps_diluted",
    "operating_cash_flow",
    "free_cash_flow",
    "capex",
    "rd_expense",
    "sga_or_opex",
    "rpo_or_deferred_revenue",
)

_MARGIN_METRICS = frozenset({"gross_margin", "operating_margin"})
_REVENUE_METRICS = frozenset({"revenue", "rpo_or_deferred_revenue"})
# GAAP-invariant cash-flow & top-line lines: exempt from IMPLICIT opening-statement Non-GAAP lock,
# while still honoring EXPLICIT basis="non_gaap" declarations (D2).
_GAAP_INVARIANT_METRICS = frozenset({"revenue", "rpo_or_deferred_revenue", "operating_cash_flow", "capex"})
_PER_SHARE_METRICS = frozenset({"eps_diluted"})

_SCALE_MULTIPLIERS: dict[str, Decimal] = {
    "ones": Decimal("1"),
    "units": Decimal("1"),
    "exact": Decimal("1"),
    "thousands": Decimal("1000"),
    "thousand": Decimal("1000"),
    "k": Decimal("1000"),
    "millions": Decimal("1000000"),
    "million": Decimal("1000000"),
    "m": Decimal("1000000"),
    "billions": Decimal("1000000000"),
    "billion": Decimal("1000000000"),
    "b": Decimal("1000000000"),
    "trillions": Decimal("1000000000000"),
    "percent": Decimal("1"),
    "pct": Decimal("1"),
    "%": Decimal("1"),
    "bps": Decimal("0.01"),
    "per_share": Decimal("1"),
}

_METRIC_TO_PILLAR: dict[str, str] = {
    "revenue": "growth",
    "rpo_or_deferred_revenue": "growth",
    "gross_margin": "margin",
    "operating_margin": "margin",
    "operating_income": "margin",
    "net_income": "margin",
    "eps_diluted": "margin",
    "rd_expense": "margin",
    "sga_or_opex": "margin",
    "operating_cash_flow": "cash_quality",
    "free_cash_flow": "cash_quality",
    "capex": "capital_allocation",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dec(val: Any) -> Decimal | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        cleaned = str(val).strip().replace(",", "").replace("%", "").replace("$", "")
        if not cleaned:
            return None
        d = Decimal(cleaned)
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _fmt_dec(val: Decimal | None, places: int = 2) -> str | None:
    if val is None or not val.is_finite():
        return None
    q = Decimal(10) ** (-places)
    return format(val.quantize(q, rounding=ROUND_HALF_UP), "f")


def _parse_iso_date(val: Any) -> date | None:
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    if not val:
        return None
    try:
        return date.fromisoformat(str(val)[:10])
    except ValueError:
        return None


def _fiscal_key(fy: int | None, fq: int | None) -> str | None:
    if fy is None:
        return None
    if fq in (1, 2, 3, 4):
        return f"FY{fy}-Q{fq}"
    return f"FY{fy}-FY"


def _parse_fiscal_label(label: str | None) -> tuple[int, int | None] | None:
    if not label:
        return None
    s = label.strip().upper()
    m = re.match(r"^(?:FY)?(\d{4})[- ]?Q([1-4])$", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m2 = re.match(r"^Q([1-4])[- ]?(?:FY)?(\d{4})$", s)
    if m2:
        return int(m2.group(2)), int(m2.group(1))
    m3 = re.match(r"^(?:FY)?(\d{4})(?:[- ]?FY)?$", s)
    if m3:
        return int(m3.group(1)), None
    return None


def _scale_claim_value(val: Decimal | None, unit: str | None, scale: str | None, metric_canonical: str) -> Decimal | None:
    if val is None:
        return None
    if metric_canonical in _MARGIN_METRICS:
        u_low = (unit or scale or "").strip().lower()
        if u_low == "bps":
            return val * Decimal("0.01")
        return val
    if metric_canonical in _PER_SHARE_METRICS:
        return val
    sc = (scale or "").strip().lower()
    if sc in _SCALE_MULTIPLIERS:
        return val * _SCALE_MULTIPLIERS[sc]
    u = (unit or "").strip().lower()
    if u in _SCALE_MULTIPLIERS:
        return val * _SCALE_MULTIPLIERS[u]
    return val


def _stem_token(tok: str) -> str:
    """Lightweight English/CJK stemmer for Item 1A omission entity coverage."""
    t = tok.casefold().strip()
    if len(t) <= 4 or any("\u4e00" <= ch <= "\u9fff" for ch in t):
        return t
    for suf in ("ations", "ation", "ments", "ment", "ities", "ity", "ings", "ing", "ies", "es", "ed", "s"):
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            if suf == "ies":
                return t[:-3] + "y"
            return t[: -len(suf)]
    return t


# --------------------------------------------------------------------------- Pydantic Models (Layer 1, Layer 2, Layer 3)

class _StrictV3aModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class XbrlActualLookupResult(_StrictV3aModel):
    """Result of stateless ``xbrl_actuals_lookup`` for one canonical metric and period."""

    metric_canonical: str
    status: Literal[
        "matched",
        "nongaap_locked",
        "currency_mismatch",
        "period_scope_mismatch",
        "unavailable",
    ]
    fiscal_period_key: str | None = None
    period_end: str | None = None
    filed_at: str | None = None
    accession: str | None = None
    currency: str | None = None
    unit: str | None = None
    value: str | None = None
    prior_year_value: str | None = None
    yoy_pct: str | None = None
    derived: bool = False
    tag: str | None = None
    initial_basis_value: str | None = None
    latest_value: str | None = None
    restated_later: bool = False
    restatement_delta_pct: str | None = None
    note: str | None = None


class LedgerLinePoint(_StrictV3aModel):
    line_name: str
    value: str | None = None
    prior_year_value: str | None = None
    yoy_pct: str | None = None
    unit: str | None = None
    currency: str | None = None
    tag: str | None = None
    period_end: str | None = None
    filed_at: str | None = None
    accession: str | None = None
    derived: bool = False
    initial_basis_value: str | None = None
    latest_value: str | None = None
    restated_later: bool = False
    restatement_delta_pct: str | None = None
    source_note: str | None = None


class RedlineRiskSnippet(_StrictV3aModel):
    section_key: str = "item_1a_risk_factors"
    paragraph_text: str
    risk_buckets: list[str] = Field(default_factory=list)
    entity_tokens: list[str] = Field(default_factory=list)
    call_coverage_ratio: float = 0.0
    bucket_covered_in_call: bool = False
    flagged_omission: bool = False
    filing_accession: str | None = None


class Sec10QCollisionLedger(_StrictV3aModel):
    """Layer 1 deterministic 10-Q / 8-K ledger aligned to fiscal period ``P`` initial basis."""

    schema_version: Literal["1"] = "1"
    status: LedgerMatchStatus
    entity_key: str
    cik: str | None = None
    symbol: str | None = None
    company_name: str | None = None
    sic: str | None = None
    sector_category: str = "non_financial"
    is_financial_gated: bool = False
    accounting_standard: str = "us-gaap"
    currency: str = "USD"
    # Four-timestamp availability contract (R2-D2 / ICAIF '26)
    target_fiscal_year: int | None = None
    target_fiscal_quarter: int | None = None
    fiscal_period_key: str | None = None
    period_end_date: str | None = None
    call_date: str | None = None
    initial_10q_filed_at: str | None = None
    initial_10q_accession: str | None = None
    latest_10q_accession: str | None = None
    p1_fiscal_year: int | None = None
    p1_fiscal_quarter: int | None = None
    p1_10q_filed_at: str | None = None
    p1_10q_accession: str | None = None
    as_of_date: str | None = None
    # Track 1: Demand & channel health
    revenue_yoy_pct: str | None = None
    ar_yoy_pct: str | None = None
    ar_minus_rev_pp: str | None = None
    inv_yoy_pct: str | None = None
    cogs_yoy_pct: str | None = None
    inv_minus_cogs_pp: str | None = None
    dso_days: str | None = None
    dso_yoy_days: str | None = None
    dio_days: str | None = None
    dio_yoy_days: str | None = None
    rpo_or_defrev_yoy_pct: str | None = None
    rpo_minus_rev_pp: str | None = None
    calculated_billings: str | None = None
    calculated_billings_yoy_pct: str | None = None
    # Track 2: Earnings & cash quality
    gross_margin_pct: str | None = None
    gross_margin_yoy_bps: str | None = None
    operating_margin_pct: str | None = None
    operating_margin_yoy_bps: str | None = None
    ocf_to_ni_ratio: str | None = None
    fcf_to_ni_ratio: str | None = None
    stock_based_compensation: str | None = None
    depreciation_amortization: str | None = None
    restructuring_charges: str | None = None
    impairment_charges: str | None = None
    consecutive_restructuring_quarters: int = 0
    reg_g_reconciliation_excerpts: list[str] = Field(default_factory=list)
    # Track 3: CapEx & ASC 440 supply commitments + finance-lease CapEx bridge
    capex_yoy_pct: str | None = None
    ppe_net_yoy_pct: str | None = None
    purchase_commitments_yoy_pct: str | None = None
    finance_lease_principal_payments: str | None = None
    finance_lease_rou_additions: str | None = None
    # Track 4: V2.5 Capital allocation cross-reference (read-only)
    capital_allocation_summary: dict[str, Any] | None = None
    # Track 5: 10-Q / 10-K redline added risks
    redline_added_risks: list[RedlineRiskSnippet] = Field(default_factory=list)
    redline_current_accession: str | None = None
    redline_prior_accession: str | None = None
    # Line-by-line initial-basis ledger
    lines: dict[str, LedgerLinePoint] = Field(default_factory=dict)
    footnote_fallbacks: list[str] = Field(default_factory=list)
    open_gaps: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- Layer 2 Atomic Fact Models

class ManagementFactualClaim(_StrictV3aModel):
    claim_id: str | None = None
    metric_canonical: str = "other"
    kpi_key: str | None = None
    scope: ClaimScope = "consolidated"
    basis: BasisType = "unknown"
    period_scope: PeriodScope = "single_quarter"
    target_fy: int | None = None
    target_fq: int | None = None
    operator: CommitmentOperator = "approx"
    value_low: str | None = None
    value_high: str | None = None
    unit: str | None = None
    scale: str | None = None
    currency: str | None = None
    evidence_sufficiency: EvidenceSufficiency = "explicit"
    qualitative_stance: Literal["bullish_healthy", "cautious_headwind", "neutral", "unspecified"] = "unspecified"
    track_hint: Literal["demand_channel", "earnings_cash", "capex_supply", "capital_allocation", "other"] = "other"
    speaker_name: str | None = None
    speaker_role: SpeakerRole = "executive"
    source_id: str
    turn_index: int | None = None
    slide_index: int | None = None
    verbatim_quote: str
    claim_summary: str


class ExtractedVerbalCommitment(_StrictV3aModel):
    commitment_key: str | None = None
    metric_canonical: str = "other"
    kpi_key: str | None = None
    scope: ClaimScope = "consolidated"
    basis: BasisType = "unknown"
    period_scope: PeriodScope = "single_quarter"
    target_fy: int | None = None
    target_fq: int | None = None
    operator: CommitmentOperator = "approx"
    value_low: str | None = None
    value_high: str | None = None
    unit: str | None = None
    scale: str | None = None
    currency: str | None = None
    evidence_sufficiency: EvidenceSufficiency = "explicit"
    is_high_profile_theme: bool = False
    theme_key: str | None = None
    horizon_label: str | None = None
    qualitative_milestone: str | None = None
    speaker_name: str | None = None
    speaker_role: SpeakerRole = "executive"
    source_id: str
    turn_index: int | None = None
    slide_index: int | None = None
    verbatim_quote: str
    commitment_text: str


class ExtractedQaEvasion(_StrictV3aModel):
    exchange_index: int
    analyst_name: str
    analyst_firm: str | None = None
    executive_name: str
    executive_role: SpeakerRole = "executive"
    sub_questions: list[str] = Field(default_factory=list)
    unanswered_sub_questions: list[str] = Field(default_factory=list)
    topic_tags: list[str] = Field(default_factory=list)
    question_asks_sensitive_detail: bool = False
    exec_cites_confidentiality_policy: bool = False
    confidentiality_policy_kind: ConfidentialityPolicyKind = "none"
    evasion_level: EvasionLevel = "direct"
    behavior_subtype: EvasionSubtype = "none"
    followup_count: int = 0
    source_id: str
    question_quote: str
    answer_quote: str
    rationale: str


class ExtractedInternalContradiction(_StrictV3aModel):
    topic: str
    speaker_a_name: str | None = None
    speaker_a_role: SpeakerRole = "executive"
    source_a_id: str
    quote_a: str
    turn_a_index: int | None = None
    slide_a_index: int | None = None
    speaker_b_name: str | None = None
    speaker_b_role: SpeakerRole = "executive"
    source_b_id: str
    quote_b: str
    turn_b_index: int | None = None
    slide_b_index: int | None = None
    evidence_sufficiency: EvidenceSufficiency = "explicit"
    contradiction_summary: str


class ExtractedExpectationSignal(_StrictV3aModel):
    kind: Literal["analyst_cluster", "sellside_expectation"] = "analyst_cluster"
    topic: str
    metric_canonical: str | None = None
    value_low: str | None = None
    value_high: str | None = None
    unit: str | None = None
    scale: str | None = None
    analyst_count: int = 1
    analyst_names: list[str] = Field(default_factory=list)
    sellside_firm: str | None = None
    sellside_timing: Literal["pre_earnings_preview", "post_earnings_review", "unspecified"] | None = None
    source_id: str
    verbatim_quote: str
    summary: str


class ExtractedSkepticNote(_StrictV3aModel):
    target_ref: str
    note: str


class Layer2ManagementExtraction(_StrictV3aModel):
    """Ledger-independent management extraction cached in ``v3a_call_l2_extractions``."""

    schema_version: Literal["1"] = "1"
    prompt_version: str = "v1"
    context_budget_version: str = "v1"
    owner_id: str | None = None
    entity_key: str
    fiscal_period_key: str | None = None
    mgmt_bundle_sha256: str
    factual_claims: list[ManagementFactualClaim] = Field(default_factory=list)
    verbal_commitments: list[ExtractedVerbalCommitment] = Field(default_factory=list)
    qa_evasions: list[ExtractedQaEvasion] = Field(default_factory=list)
    internal_contradictions: list[ExtractedInternalContradiction] = Field(default_factory=list)
    expectation_signals: list[ExtractedExpectationSignal] = Field(default_factory=list)
    skeptic_notes: list[ExtractedSkepticNote] = Field(default_factory=list)
    model_id: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    call_count: int = 0
    latency_ms: int = 0
    truncated_retried: bool = False
    partial_lane_status: dict[str, str] = Field(default_factory=dict)
    lane_errors: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class Layer2SellsideExtraction(_StrictV3aModel):
    """Independent per-note sell-side extraction cached in ``v3a_sellside_extractions`` (R2-D6)."""

    schema_version: Literal["1"] = "1"
    prompt_version: str = "v1"
    owner_id: str | None = None
    entity_key: str
    source_id: str
    content_sha256: str
    sellside_firm: str | None = None
    sellside_timing: Literal["pre_earnings_preview", "post_earnings_review", "unspecified"] = "unspecified"
    expectation_signals: list[ExtractedExpectationSignal] = Field(default_factory=list)
    key_challenges: list[str] = Field(default_factory=list)
    model_id: str | None = None
    total_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    call_count: int = 1
    latency_ms: int = 0
    created_at: datetime = Field(default_factory=_utcnow)


# --------------------------------------------------------------------------- Layer 3 Output & Account Commitment Ledger

class GroundedQuoteRef(_StrictV3aModel):
    source_id: str
    quotation: str
    verified: bool = False
    raw_char_start: int | None = None
    raw_char_end: int | None = None
    page_number: int | None = None
    slide_number: int | None = None
    timestamp_start: str | None = None
    match_space: Literal["raw", "norm_text", "norm_table"] | None = None
    quote_context_warning: str | None = None
    surrounding_context: str | None = None


class CollisionFinding(_StrictV3aModel):
    finding_id: str
    track: Literal[
        "track_0_internal",
        "track_1_demand_channel",
        "track_2_earnings_cash",
        "track_3_capex_supply",
        "track_4_capital_allocation",
        "track_5_10q_omission",
        "track_6_commitments",
    ]
    collision_kind: Literal[
        "direct_contradiction",
        "ceo_cfo_divergence",
        "internal_walkback",
        "basis_divergence_nongaap_gap",
        "period_scope_mismatch",
        "currency_mismatch",
        "filing_10q_omission",
        "awaiting_10q",
        "corroborated",
        "observation_only_insufficient_evidence",
        "not_applicable",
    ]
    severity: CollisionSeverity
    title: str
    management_statement: str
    ledger_evidence: str
    formula_note: str | None = None
    basis_badge: str | None = None
    period_badge: str | None = None
    restated_later: bool = False
    restatement_note: str | None = None
    linked_pillar: str | None = None
    skeptic_note: str | None = None
    quotes: list[GroundedQuoteRef] = Field(default_factory=list)
    priority_rank: int = 50


class QaEvasionFinding(_StrictV3aModel):
    finding_id: str
    exchange_index: int
    analyst_name: str
    analyst_firm: str | None = None
    executive_name: str
    executive_role: SpeakerRole = "executive"
    evasion_level: EvasionLevel
    behavior_subtype: EvasionSubtype = "none"
    display_severity: CollisionSeverity
    legitimate_confidentiality: bool = False
    disclosure_policy_gap: bool = False
    collapsed_by_default: bool = False
    sub_questions: list[str] = Field(default_factory=list)
    unanswered_sub_questions: list[str] = Field(default_factory=list)
    topic_tags: list[str] = Field(default_factory=list)
    followup_count: int = 0
    rationale: str
    question_quote: GroundedQuoteRef
    answer_quote: GroundedQuoteRef


class GuidanceRangeSnapshot(_StrictV3aModel):
    fiscal_period_key: str
    call_date: str | None = None
    value_low: str | None = None
    value_high: str | None = None
    unit: str | None = None
    scale: str | None = None


class VerbalCommitmentRecord(_StrictV3aModel):
    commitment_id: str
    owner_id: str | None = None
    entity_key: str
    source_id: str
    source_fiscal_period: str | None = None
    call_date: str | None = None
    metric_canonical: str
    kpi_key: str | None = None
    scope: ClaimScope = "consolidated"
    basis: BasisType = "unknown"
    effective_basis: Literal["gaap", "non_gaap", "operating_kpi", "unknown"] = "unknown"
    period_scope: PeriodScope = "single_quarter"
    target_fy: int | None = None
    target_fq: int | None = None
    target_period_key: str | None = None
    operator: CommitmentOperator = "approx"
    value_low: str | None = None
    value_high: str | None = None
    unit: str | None = None
    scale: str | None = None
    currency: str | None = None
    verification_tier: VerificationTier = "xbrl_auto"
    status: CommitmentStatus = "pending_10q"
    actual_value: str | None = None
    actual_period_end: str | None = None
    verified_against_accession: str | None = None
    verified_at_filed_date: str | None = None
    delta_note: str | None = None
    is_high_profile_theme: bool = False
    theme_key: str | None = None
    qualitative_milestone: str | None = None
    commitment_text: str
    linked_pillar: str | None = None
    muted: bool = False
    range_drift_note: str | None = None
    range_history: list[GuidanceRangeSnapshot] = Field(default_factory=list)
    skeptic_note: str | None = None
    quote: GroundedQuoteRef | None = None


class ExecutiveQuarterBaseline(_StrictV3aModel):
    fiscal_period_key: str
    call_date: str | None = None
    executive_name: str
    speaker_role: SpeakerRole = "executive"
    answer_count: int = 0
    avg_hedge_density: float = 0.0
    avg_verbosity_ratio: float = 1.0
    evasion_rate_pct: float = 0.0


class SpeakerBaselineShiftFinding(_StrictV3aModel):
    executive_name: str
    speaker_role: SpeakerRole = "executive"
    quarters_observed: int
    current_hedge_density: float
    historical_avg_hedge_density: float
    hedge_density_delta: float
    current_evasion_rate_pct: float
    historical_avg_evasion_rate_pct: float
    evasion_rate_delta_pp: float
    severity: CollisionSeverity
    summary: str


class AccountVerbalCommitmentLedger(_StrictV3aModel):
    """Account-isolated cross-quarter commitment & speaker baseline ledger (``v3a_verbal_commitment_ledger``)."""

    schema_version: Literal["1"] = "1"
    owner_id: str
    entity_key: str
    symbol: str | None = None
    company_name: str | None = None
    commitments: list[VerbalCommitmentRecord] = Field(default_factory=list)
    executive_baselines: list[ExecutiveQuarterBaseline] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=_utcnow)


class ExpectationComparisonItem(_StrictV3aModel):
    tier: Literal[
        "v25_historical_credibility",
        "analyst_cluster_hotspot",
        "sellside_third_party",
        "v3b_byok_consensus",
    ]
    authority_tier: Literal["sec_public_prior", "call_qa_signal", "third_party_opinion", "byok_consensus"]
    title: str
    summary: str
    sellside_firm: str | None = None
    sellside_timing: Literal["pre_earnings_preview", "post_earnings_review", "unspecified"] | None = None
    analyst_count: int | None = None
    exportable: bool = True
    quote: GroundedQuoteRef | None = None


class CallVerificationReport(_StrictV3aModel):
    """Final Layer 3 merged audit report delivered to frontend, Topic R2-D5 digest, and Radar Phase 2c."""

    schema_version: Literal["1"] = "1"
    owner_id: str | None = None
    entity_key: str
    cik: str | None = None
    symbol: str | None = None
    company_name: str | None = None
    fiscal_period_key: str | None = None
    call_date: str | None = None
    call_default_basis: CallDefaultBasis = "unspecified"
    ledger_status: LedgerMatchStatus = "non_sec_entity"
    awaiting_current_10q: bool = False
    overall_light: CollisionSeverity = "green"
    headline: str
    red_count: int = 0
    yellow_count: int = 0
    green_count: int = 0
    evasion_red_yellow_count: int = 0
    evasion_total_exchanges: int = 0
    evasion_distribution: dict[str, int] = Field(default_factory=dict)
    confidentiality_whitelisted_count: int = 0
    disclosure_policy_gap_count: int = 0
    collision_findings: list[CollisionFinding] = Field(default_factory=list)
    qa_evasions: list[QaEvasionFinding] = Field(default_factory=list)
    commitments: list[VerbalCommitmentRecord] = Field(default_factory=list)
    speaker_baseline_shifts: list[SpeakerBaselineShiftFinding] = Field(default_factory=list)
    expectation_benchmarks: list[ExpectationComparisonItem] = Field(default_factory=list)
    omission_phase2_candidates: list[RedlineRiskSnippet] = Field(default_factory=list)
    skeptic_calibration: list[str] = Field(default_factory=list)
    open_gaps: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    source_hashes: dict[str, str] = Field(default_factory=dict)
    verified_quote_count: int = 0
    total_quote_count: int = 0
    rules_sha256: str
    evaluation_rules_version: str
    context_budget_version: str
    text_only_evasion_disclaimer: str = (
        "仅基于电话会文本语义与账本数据进行响应核验，不构成声学测谎或情绪断言。"
    )
    model_id: str | None = None
    total_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    llm_call_count: int = 0
    partial_lane_status: dict[str, str] = Field(default_factory=dict)
    lane_errors: dict[str, str] = Field(default_factory=dict)
    footnote_verification_notes: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=_utcnow)


# --------------------------------------------------------------------------- Expectation Benchmark Protocol (Tab 3 / V3b Seam)

class ExpectationBenchmarkProvider(Protocol):
    def provide(
        self,
        *,
        entity_key: str,
        fiscal_period_key: str | None,
        as_of: date | None,
    ) -> list[ExpectationComparisonItem]: ...


# --------------------------------------------------------------------------- Stateless Shared XBRL Actuals Query Library

def _rebuild_snapshot_facts_dict(
    snap: PeerCompanySnapshot,
    *,
    filed_cutoff: date | None = None,
) -> dict[str, Any]:
    """Convert ``PeerCompanySnapshot.facts_by_line`` into ``companyfacts`` taxonomy dict.

    When ``filed_cutoff`` is set (e.g. ``period_P_initial_10q_filed_at``), rows filed AFTER
    that date are stripped BEFORE calling ``select_line`` / ``_derive_ytd_quarters``, guaranteeing
    exact initial-filing-basis reconstruction free from post-restatement / post-split false alarms.
    """
    tax_bucket: dict[str, Any] = {}
    for _line_name, tag_map in (snap.facts_by_line or {}).items():
        if not isinstance(tag_map, Mapping):
            continue
        for tag, unit_map in tag_map.items():
            if not isinstance(unit_map, Mapping):
                continue
            filtered_units: dict[str, list[dict[str, Any]]] = {}
            for unit_key, rows in unit_map.items():
                if not isinstance(rows, list):
                    continue
                kept: list[dict[str, Any]] = []
                for r in rows:
                    if not isinstance(r, Mapping):
                        continue
                    if filed_cutoff is not None:
                        f_d = _parse_iso_date(r.get("filed"))
                        if f_d is not None and f_d > filed_cutoff:
                            continue
                    kept.append(dict(r))
                if kept:
                    filtered_units[unit_key] = kept
            if filtered_units:
                existing = tax_bucket.get(tag)
                if existing is None:
                    tax_bucket[tag] = {"units": filtered_units}
                else:
                    existing["units"].update(filtered_units)
    return {snap.taxonomy: tax_bucket}


def _select_line_from_facts(
    facts_dict: Mapping[str, Any],
    line_name: str,
    kind: Literal["quarter", "year"],
    n: int,
    *,
    taxonomy: str,
    as_of: date | None,
) -> list[Period]:
    spec = STANDARD_LINES.get(line_name)
    if spec is None:
        return []
    return select_line(facts_dict, spec, kind, n, derive_q4=True, taxonomy=taxonomy, as_of=as_of)


def _match_period_for_target(
    periods: Sequence[Period],
    *,
    target_fy: int | None,
    target_fq: int | None,
    target_end: date | None = None,
) -> Period | None:
    if not periods:
        return None
    if target_end is not None:
        by_end = min(periods, key=lambda p: abs((p.end - target_end).days))
        if abs((by_end.end - target_end).days) <= 10:
            return by_end
    if target_fy is not None:
        want = (target_fy, target_fq)
        for p in periods:
            pk = _parse_fiscal_label(p.label)
            if pk == want:
                return p
            if target_fq is None and p.kind == "year" and p.end.year == target_fy:
                return p
    return None


def _find_prior_year_period(periods: Sequence[Period], cur: Period) -> Period | None:
    for p in periods:
        if p.end == cur.end:
            continue
        delta = (cur.end - p.end).days
        if 345 <= delta <= 385:
            return p
    return None


def _compute_opex_for_end(
    end_d: date,
    gp_map: Mapping[date, Period],
    oi_map: Mapping[date, Period],
    rd_map: Mapping[date, Period],
    sga_map: Mapping[date, Period],
    sell_map: Mapping[date, Period],
    admin_map: Mapping[date, Period],
    tot_opex_map: Mapping[date, Period],
) -> tuple[Decimal | None, bool, str | None, str | None, date | None, str | None]:
    """Compute total operating expenses handling split OpEx lines (META/NVDA/MSFT/GOOGL)."""
    if end_d in tot_opex_map:
        p = tot_opex_map[end_d]
        return p.value, p.derived, p.tag, p.unit, p.filed, p.accession
    if end_d in gp_map and end_d in oi_map:
        gp_p, oi_p = gp_map[end_d], oi_map[end_d]
        return gp_p.value - oi_p.value, True, f"{gp_p.tag}-{oi_p.tag}", gp_p.unit, gp_p.filed or oi_p.filed, gp_p.accession or oi_p.accession
    parts = [m[end_d] for m in (rd_map, sga_map, sell_map, admin_map) if end_d in m]
    if parts:
        total = sum((p.value for p in parts), Decimal(0))
        return total, True, "+".join(p.tag for p in parts), parts[0].unit, parts[0].filed, parts[0].accession
    return None, False, None, None, None, None


def _lookup_metric_at_cutoff(
    snap: PeerCompanySnapshot,
    metric_canonical: str,
    *,
    kind: Literal["quarter", "year"],
    target_fy: int | None,
    target_fq: int | None,
    target_end: date | None,
    filed_cutoff: date | None,
    as_of: date | None,
) -> tuple[Decimal | None, Decimal | None, Period | None, bool, str | None]:
    """Return ``(cur_val, prior_year_val, anchor_period, derived_flag, tag_str)`` for a metric."""
    effective_cutoff = filed_cutoff
    if as_of is not None and (effective_cutoff is None or as_of < effective_cutoff):
        effective_cutoff = as_of
    facts_dict = _rebuild_snapshot_facts_dict(snap, filed_cutoff=effective_cutoff)
    tax = snap.taxonomy

    def _get(line_name: str) -> list[Period]:
        return _select_line_from_facts(facts_dict, line_name, kind, 16, taxonomy=tax, as_of=effective_cutoff)

    if metric_canonical in ("revenue", "operating_income", "net_income", "eps_diluted", "operating_cash_flow", "rd_expense"):
        ps = _get(metric_canonical)
        cur = _match_period_for_target(ps, target_fy=target_fy, target_fq=target_fq, target_end=target_end)
        if cur is None:
            return None, None, None, False, None
        py = _find_prior_year_period(ps, cur)
        return cur.value, (py.value if py else None), cur, cur.derived, cur.tag

    if metric_canonical == "capex":
        ps = _get("capex")
        cur = _match_period_for_target(ps, target_fy=target_fy, target_fq=target_fq, target_end=target_end)
        if cur is None:
            return None, None, None, False, None
        py = _find_prior_year_period(ps, cur)
        return abs(cur.value), (abs(py.value) if py else None), cur, cur.derived, cur.tag

    if metric_canonical == "free_cash_flow":
        ocf_ps = _get("operating_cash_flow")
        cap_ps = _get("capex")
        cur_o = _match_period_for_target(ocf_ps, target_fy=target_fy, target_fq=target_fq, target_end=target_end)
        if cur_o is None:
            return None, None, None, False, None
        cap_by_end = {p.end: abs(p.value) for p in cap_ps}
        if cur_o.end not in cap_by_end:
            return None, None, None, False, None
        cur_fcf = cur_o.value - cap_by_end[cur_o.end]
        py_o = _find_prior_year_period(ocf_ps, cur_o)
        py_fcf = (py_o.value - cap_by_end[py_o.end]) if (py_o is not None and py_o.end in cap_by_end) else None
        return cur_fcf, py_fcf, cur_o, True, f"{cur_o.tag}-capex"

    if metric_canonical in ("gross_margin", "operating_margin"):
        rev_ps = _get("revenue")
        cur_r = _match_period_for_target(rev_ps, target_fy=target_fy, target_fq=target_fq, target_end=target_end)
        if cur_r is None or cur_r.value <= 0:
            return None, None, None, False, None
        py_r = _find_prior_year_period(rev_ps, cur_r)
        if metric_canonical == "gross_margin":
            gp_map = {p.end: p.value for p in _get("gross_profit")}
            cogs_map = {p.end: p.value for p in _get("cost_of_revenue")}
            gp_cur = gp_map.get(cur_r.end)
            if gp_cur is None and cur_r.end in cogs_map:
                gp_cur = cur_r.value - cogs_map[cur_r.end]
            if gp_cur is None:
                return None, None, None, False, None
            cur_gm = (gp_cur / cur_r.value) * Decimal(100)
            py_gm: Decimal | None = None
            if py_r is not None and py_r.value > 0:
                gp_py = gp_map.get(py_r.end)
                if gp_py is None and py_r.end in cogs_map:
                    gp_py = py_r.value - cogs_map[py_r.end]
                if gp_py is not None:
                    py_gm = (gp_py / py_r.value) * Decimal(100)
            return cur_gm, py_gm, cur_r, True, "gross_profit/revenue"
        oi_map = {p.end: p.value for p in _get("operating_income")}
        oi_cur = oi_map.get(cur_r.end)
        if oi_cur is None:
            return None, None, None, False, None
        cur_om = (oi_cur / cur_r.value) * Decimal(100)
        py_om = (
            (oi_map[py_r.end] / py_r.value) * Decimal(100)
            if (py_r is not None and py_r.value > 0 and py_r.end in oi_map)
            else None
        )
        return cur_om, py_om, cur_r, True, "operating_income/revenue"

    if metric_canonical == "sga_or_opex":
        rev_ps = _get("revenue")
        cur_r = _match_period_for_target(rev_ps, target_fy=target_fy, target_fq=target_fq, target_end=target_end)
        if cur_r is None:
            return None, None, None, False, None
        gp_m = {p.end: p for p in _get("gross_profit")}
        oi_m = {p.end: p for p in _get("operating_income")}
        rd_m = {p.end: p for p in _get("rd_expense")}
        sga_m = {p.end: p for p in _get("sga_expense")}
        sell_m = {p.end: p for p in _get("selling_expense")}
        adm_m = {p.end: p for p in _get("admin_expense")}
        tot_m = {p.end: p for p in _get("total_operating_costs")}
        val_cur, drv, tag, _u, _f, _a = _compute_opex_for_end(cur_r.end, gp_m, oi_m, rd_m, sga_m, sell_m, adm_m, tot_m)
        if val_cur is None:
            return None, None, None, False, None
        py_r = _find_prior_year_period(rev_ps, cur_r)
        val_py: Decimal | None = None
        if py_r is not None:
            val_py, *_ = _compute_opex_for_end(py_r.end, gp_m, oi_m, rd_m, sga_m, sell_m, adm_m, tot_m)
        return val_cur, val_py, cur_r, drv, tag

    if metric_canonical == "rpo_or_deferred_revenue":
        rpo_ps = _get("rpo")
        cur = _match_period_for_target(rpo_ps, target_fy=target_fy, target_fq=target_fq, target_end=target_end)
        if cur is not None:
            py = _find_prior_year_period(rpo_ps, cur)
            return cur.value, (py.value if py else None), cur, cur.derived, cur.tag
        dc_ps = _get("deferred_revenue_current")
        dnc_ps = _get("deferred_revenue_noncurrent")
        cur_dc = _match_period_for_target(dc_ps, target_fy=target_fy, target_fq=target_fq, target_end=target_end)
        cur_dnc = _match_period_for_target(dnc_ps, target_fy=target_fy, target_fq=target_fq, target_end=target_end)
        anchor = cur_dc or cur_dnc
        if anchor is None:
            return None, None, None, False, None
        dc_map = {p.end: p.value for p in dc_ps}
        dnc_map = {p.end: p.value for p in dnc_ps}
        cur_val = dc_map.get(anchor.end, Decimal(0)) + dnc_map.get(anchor.end, Decimal(0))
        py_anchor = _find_prior_year_period(dc_ps or dnc_ps, anchor)
        py_val = (
            dc_map.get(py_anchor.end, Decimal(0)) + dnc_map.get(py_anchor.end, Decimal(0))
            if py_anchor is not None
            else None
        )
        return cur_val, py_val, anchor, True, "deferred_revenue_total"

    return None, None, None, False, None


def xbrl_actuals_lookup(
    snap: PeerCompanySnapshot | None,
    metric_canonical: str,
    *,
    target_fy: int | None,
    target_fq: int | None,
    target_end: date | None = None,
    basis_lock: BasisType = "gaap",
    call_default_basis: CallDefaultBasis = "unspecified",
    period_scope: PeriodScope = "single_quarter",
    claim_currency: str | None = None,
    initial_filing_cutoff: date | None = None,
    as_of: date | None = None,
    restatement_notice_pct: float = 1.0,
) -> XbrlActualLookupResult:
    """Stateless shared XBRL actuals query library (reusable by V2.5 & V3a; zero shared storage writes)."""
    fkey = _fiscal_key(target_fy, target_fq)
    if snap is None or metric_canonical not in TWELVE_XBRL_METRICS:
        return XbrlActualLookupResult(
            metric_canonical=metric_canonical,
            status="unavailable",
            fiscal_period_key=fkey,
            note="Snapshot unavailable or metric outside 12-metric XBRL universe.",
        )

    # 1. Resolve effective basis (D2: opening-statement implicit Non-GAAP lock)
    # OCF and CapEx are GAAP-invariant under implicit call default (still locked if explicitly declared non_gaap).
    eff_basis: str = basis_lock
    if basis_lock in ("unknown", "inherited_call_default") and call_default_basis == "non_gaap":
        if metric_canonical not in _GAAP_INVARIANT_METRICS:
            eff_basis = "non_gaap"
        else:
            eff_basis = "gaap"
    elif basis_lock in ("unknown", "inherited_call_default") and call_default_basis == "gaap":
        eff_basis = "gaap"

    # 2. Period scope lock (prevent single_quarter vs full_year/ytd/ttm mis-comparison)
    kind: Literal["quarter", "year"]
    if period_scope == "full_year" or (target_fq is None and target_fy is not None):
        kind = "year"
    elif period_scope in ("single_quarter", "point_in_time", "unspecified"):
        kind = "quarter"
    else:
        return XbrlActualLookupResult(
            metric_canonical=metric_canonical,
            status="period_scope_mismatch",
            fiscal_period_key=fkey,
            currency=snap.currency,
            note=f"Claim period_scope={period_scope!r} does not match standalone 10-Q/10-K basis.",
        )

    # 3. Universal ISO-4217 currency lock
    if claim_currency and metric_canonical not in _MARGIN_METRICS:
        cc = claim_currency.strip().upper()
        sc = (snap.currency or "USD").strip().upper()
        if len(cc) == 3 and cc != sc:
            return XbrlActualLookupResult(
                metric_canonical=metric_canonical,
                status="currency_mismatch",
                fiscal_period_key=fkey,
                currency=sc,
                note=f"Claim currency {cc} differs from XBRL reporting currency {sc}.",
            )

    # 4. Initial-filing basis lookup (reconstructed BEFORE _derive_ytd_quarters)
    init_val, init_py, init_p, init_drv, init_tag = _lookup_metric_at_cutoff(
        snap,
        metric_canonical,
        kind=kind,
        target_fy=target_fy,
        target_fq=target_fq,
        target_end=target_end,
        filed_cutoff=initial_filing_cutoff,
        as_of=as_of,
    )
    if init_val is None or init_p is None:
        return XbrlActualLookupResult(
            metric_canonical=metric_canonical,
            status="unavailable",
            fiscal_period_key=fkey,
            currency=snap.currency,
            note="No matching XBRL fact available within as_of/initial cutoff.",
        )

    # 5. Restatement detection (compare initial basis vs latest <= as_of)
    restated_later = False
    restatement_delta_pct: str | None = None
    latest_val_str: str | None = _fmt_dec(init_val, 4 if metric_canonical in _PER_SHARE_METRICS else 2)
    if initial_filing_cutoff is not None and (as_of is None or as_of > initial_filing_cutoff):
        lat_val, *_ = _lookup_metric_at_cutoff(
            snap,
            metric_canonical,
            kind=kind,
            target_fy=target_fy,
            target_fq=target_fq,
            target_end=init_p.end,
            filed_cutoff=as_of,
            as_of=as_of,
        )
        if lat_val is not None:
            latest_val_str = _fmt_dec(lat_val, 4 if metric_canonical in _PER_SHARE_METRICS else 2)
            if init_val != 0:
                rel_diff_pct = abs((lat_val - init_val) / init_val) * Decimal(100)
                if rel_diff_pct > Decimal(str(restatement_notice_pct)):
                    restated_later = True
                    restatement_delta_pct = _fmt_dec(((lat_val - init_val) / abs(init_val)) * Decimal(100), 2)

    yoy_pct: Decimal | None = None
    if init_py is not None and init_py != 0:
        if metric_canonical in _MARGIN_METRICS:
            yoy_pct = init_val - init_py  # percentage points
        else:
            yoy_pct = ((init_val - init_py) / abs(init_py)) * Decimal(100)

    places = 4 if metric_canonical in _PER_SHARE_METRICS else 2
    val_str = _fmt_dec(init_val, places)
    py_str = _fmt_dec(init_py, places)

    # Strict Non-GAAP lock (still returns GAAP benchmark so Layer 3 can report reconciliation gap bps!)
    if eff_basis == "non_gaap" and metric_canonical not in _REVENUE_METRICS:
        return XbrlActualLookupResult(
            metric_canonical=metric_canonical,
            status="nongaap_locked",
            fiscal_period_key=fkey or init_p.label,
            period_end=init_p.end.isoformat(),
            filed_at=init_p.filed.isoformat() if init_p.filed else None,
            accession=init_p.accession,
            currency=snap.currency,
            unit="%" if metric_canonical in _MARGIN_METRICS else (init_p.unit or snap.currency),
            value=val_str,
            prior_year_value=py_str,
            yoy_pct=_fmt_dec(yoy_pct, 2),
            derived=init_drv,
            tag=init_tag,
            initial_basis_value=val_str,
            latest_value=latest_val_str,
            restated_later=restated_later,
            restatement_delta_pct=restatement_delta_pct,
            note="Non-GAAP claim locked against GAAP XBRL hard contradiction; GAAP value shown for reconciliation gap only.",
        )

    return XbrlActualLookupResult(
        metric_canonical=metric_canonical,
        status="matched",
        fiscal_period_key=fkey or init_p.label,
        period_end=init_p.end.isoformat(),
        filed_at=init_p.filed.isoformat() if init_p.filed else None,
        accession=init_p.accession,
        currency=snap.currency,
        unit="%" if metric_canonical in _MARGIN_METRICS else (init_p.unit or snap.currency),
        value=val_str,
        prior_year_value=py_str,
        yoy_pct=_fmt_dec(yoy_pct, 2),
        derived=init_drv,
        tag=init_tag,
        initial_basis_value=val_str,
        latest_value=latest_val_str,
        restated_later=restated_later,
        restatement_delta_pct=restatement_delta_pct,
    )


# --------------------------------------------------------------------------- Layer 1: Dual-Anchored Period P & Ledger Builder

def _extract_recent_periodic_filings(submissions: Mapping[str, Any] | None, *, as_of: date | None) -> list[dict[str, Any]]:
    if not isinstance(submissions, Mapping):
        return []
    recent = (submissions.get("filings") or {}).get("recent") if isinstance(submissions.get("filings"), Mapping) else None
    if not isinstance(recent, Mapping):
        return []
    forms = recent.get("form") or []
    accns = recent.get("accessionNumber") or []
    f_dates = recent.get("filingDate") or []
    r_dates = recent.get("reportDate") or []
    p_docs = recent.get("primaryDocument") or []
    n = min(len(forms), len(accns), len(f_dates))
    out: list[dict[str, Any]] = []
    for i in range(n):
        form = str(forms[i] or "").strip().upper()
        if form not in ("10-Q", "10-Q/A", "10-K", "10-K/A", "20-F", "20-F/A", "6-K", "8-K"):
            continue
        fd = _parse_iso_date(f_dates[i])
        if fd is None:
            continue
        if as_of is not None and fd > as_of:
            continue
        rd = _parse_iso_date(r_dates[i]) if i < len(r_dates) else None
        out.append(
            {
                "form": form,
                "accession": str(accns[i] or "").strip(),
                "filing_date": fd,
                "report_date": rd,
                "primary_document": str(p_docs[i] or "").strip() if i < len(p_docs) else "",
            }
        )
    out.sort(key=lambda x: (x["filing_date"], x["accession"]))
    return out


def anchor_fiscal_period_with_submissions(
    *,
    call_fy: int | None,
    call_fq: int | None,
    call_date_iso: str | None,
    snap: PeerCompanySnapshot | None,
    submissions: Mapping[str, Any] | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Dual-anchor fiscal period ``P`` using ``call_date`` + SEC submissions 0-65d ``reportDate`` + XBRL ``(fy, fp)``."""
    call_d = _parse_iso_date(call_date_iso)
    filings = _extract_recent_periodic_filings(submissions, as_of=as_of)
    periodic_10 = [f for f in filings if f["form"] in ("10-Q", "10-K", "20-F")]

    matched_p_filing: dict[str, Any] | None = None
    matched_end: date | None = None
    resolved_fy = call_fy
    resolved_fq = call_fq

    # Anchor 1: Match call_date against periodic reportDate within [0..65] days prior to call_date
    if call_d is not None:
        cands = [
            f
            for f in periodic_10
            if f["report_date"] is not None and 0 <= (call_d - f["report_date"]).days <= 65
        ]
        if cands:
            # Earliest original filing for that report_date = initial 10-Q/10-K filing!
            best_rd = max(c["report_date"] for c in cands)
            same_rd = [c for c in cands if c["report_date"] == best_rd]
            matched_p_filing = min(same_rd, key=lambda c: c["filing_date"])
            matched_end = best_rd

    # Anchor 2: Cross-check against XBRL revenue periods (handles non-calendar FY like NVDA/MSFT/AAPL)
    if snap is not None:
        facts_dict = _rebuild_snapshot_facts_dict(snap, filed_cutoff=as_of)
        rev_q = _select_line_from_facts(facts_dict, "revenue", "quarter", 20, taxonomy=snap.taxonomy, as_of=as_of)
        if matched_end is not None:
            p_hit = _match_period_for_target(rev_q, target_fy=None, target_fq=None, target_end=matched_end)
            if p_hit is not None:
                pk = _parse_fiscal_label(p_hit.label)
                if pk is not None:
                    resolved_fy, resolved_fq = pk
        elif resolved_fy is not None and resolved_fq is not None:
            p_hit = _match_period_for_target(rev_q, target_fy=resolved_fy, target_fq=resolved_fq)
            if p_hit is not None:
                # Guard against call_date pointing to a newer period whose 10-Q has not been filed yet
                if call_d is None or (0 <= (call_d - p_hit.end).days <= 75):
                    matched_end = p_hit.end
                    if matched_p_filing is None and p_hit.filed is not None:
                        matched_p_filing = {
                            "form": p_hit.form or "10-Q",
                            "accession": p_hit.accession,
                            "filing_date": p_hit.filed,
                            "report_date": p_hit.end,
                        }
        elif call_d is not None:
            # Find latest XBRL period whose end is 0..65d before call_date
            cands_q = [p for p in rev_q if 0 <= (call_d - p.end).days <= 65]
            if cands_q:
                p_hit = cands_q[0]
                matched_end = p_hit.end
                pk = _parse_fiscal_label(p_hit.label)
                if pk is not None:
                    resolved_fy, resolved_fq = pk
                if matched_p_filing is None and p_hit.filed is not None:
                    matched_p_filing = {
                        "form": p_hit.form or "10-Q",
                        "accession": p_hit.accession,
                        "filing_date": p_hit.filed,
                        "report_date": p_hit.end,
                    }

    # Check if submissions show an 8-K around call_date but NO 10-Q/10-K for that period yet
    awaiting_10q = False
    if matched_p_filing is None and call_d is not None:
        # Either call_date is very recent or newer than latest available periodic filing
        latest_periodic_rd = max((f["report_date"] for f in periodic_10 if f["report_date"] is not None), default=None)
        if latest_periodic_rd is None or (call_d - latest_periodic_rd).days > 65:
            awaiting_10q = True

    # Find P+1 filing strictly respecting four-timestamp availability contract:
    # p1_10q.filed_at <= as_of AND p1_10q.filed_at > call_date AND p1_report_date in [end+70d, end+115d]
    p1_fy: int | None = None
    p1_fq: int | None = None
    p1_filed: date | None = None
    p1_accn: str | None = None
    if resolved_fy is not None and resolved_fq in (1, 2, 3, 4):
        p1_fy = resolved_fy + 1 if resolved_fq == 4 else resolved_fy
        p1_fq = 1 if resolved_fq == 4 else resolved_fq + 1
    if matched_end is not None:
        p1_cands = [
            f
            for f in periodic_10
            if f["report_date"] is not None
            and 70 <= (f["report_date"] - matched_end).days <= 115
            and (call_d is None or f["filing_date"] > call_d)
        ]
        if p1_cands:
            first_p1 = min(p1_cands, key=lambda c: c["filing_date"])
            p1_filed = first_p1["filing_date"]
            p1_accn = first_p1["accession"]
        elif snap is not None and p1_fy is not None and p1_fq is not None:
            facts_dict = _rebuild_snapshot_facts_dict(snap, filed_cutoff=as_of)
            rev_q = _select_line_from_facts(facts_dict, "revenue", "quarter", 20, taxonomy=snap.taxonomy, as_of=as_of)
            p1_hit = _match_period_for_target(rev_q, target_fy=p1_fy, target_fq=p1_fq)
            if p1_hit is not None and p1_hit.filed is not None and (call_d is None or p1_hit.filed > call_d):
                p1_filed = p1_hit.filed
                p1_accn = p1_hit.accession

    return {
        "target_fy": resolved_fy,
        "target_fq": resolved_fq,
        "fiscal_period_key": _fiscal_key(resolved_fy, resolved_fq),
        "period_end": matched_end,
        "call_date": call_d,
        "initial_10q_filed_at": matched_p_filing["filing_date"] if matched_p_filing else None,
        "initial_10q_accession": matched_p_filing["accession"] if matched_p_filing else None,
        "awaiting_10q": awaiting_10q,
        "p1_fy": p1_fy,
        "p1_fq": p1_fq,
        "p1_10q_filed_at": p1_filed,
        "p1_10q_accession": p1_accn,
    }


def build_sec_10q_collision_ledger(
    *,
    entity_key: str,
    snap: PeerCompanySnapshot | None,
    submissions: Mapping[str, Any] | None = None,
    call_fy: int | None = None,
    call_fq: int | None = None,
    call_date_iso: str | None = None,
    as_of: date | None = None,
    capital_allocation_summary: Mapping[str, Any] | None = None,
    redline_added_paragraphs: Sequence[Mapping[str, Any]] = (),
    redline_current_accession: str | None = None,
    redline_prior_accession: str | None = None,
    cached_initial_10q_accession: str | None = None,
    cached_initial_10q_filed_at: str | None = None,
    cached_period_end_date: str | None = None,
    rules: CompiledV3aRules | None = None,
) -> Sec10QCollisionLedger:
    """Build deterministic Layer 1 ``Sec10QCollisionLedger`` aligned to Period ``P`` initial filing basis."""
    compiled = rules or load_v3a_call_rules()
    restatement_pct = compiled.rules.ledger_collision_thresholds.restatement_notice_pct

    if snap is None:
        return Sec10QCollisionLedger(
            status="non_sec_entity",
            entity_key=entity_key,
            target_fiscal_year=call_fy,
            target_fiscal_quarter=call_fq,
            fiscal_period_key=_fiscal_key(call_fy, call_fq),
            call_date=call_date_iso,
            as_of_date=as_of.isoformat() if as_of else None,
            open_gaps=["non_sec_entity:sec_10q_ledger_not_applicable"],
        )

    sic_gate = evaluate_sec_sic_gate(snap.sic, snap.sic_description)
    sector_cat = str(getattr(sic_gate, "sector_category", "non_financial") or "non_financial")
    is_fin = bool(getattr(sic_gate, "is_gated", False) or sector_cat in ("bank", "insurance", "reit", "financial_other"))

    anchor = anchor_fiscal_period_with_submissions(
        call_fy=call_fy,
        call_fq=call_fq,
        call_date_iso=call_date_iso,
        snap=snap,
        submissions=submissions,
        as_of=as_of,
    )
    target_fy = anchor["target_fy"]
    target_fq = anchor["target_fq"]
    period_end: date | None = anchor["period_end"]
    init_filed: date | None = anchor["initial_10q_filed_at"]
    init_accn: str | None = anchor["initial_10q_accession"]

    # Preserve exact initial-filing anchor under cache_only=True (submissions=None) so GET /report &
    # R2-D5 Agent Digest never drift to a later comparative 10-K or mis-downgrade financial_sic_gated!
    if submissions is None and cached_initial_10q_filed_at:
        c_filed = _parse_iso_date(cached_initial_10q_filed_at)
        c_end = _parse_iso_date(cached_period_end_date)
        if c_filed is not None and (as_of is None or c_filed <= as_of):
            init_filed = c_filed
            if cached_initial_10q_accession:
                init_accn = cached_initial_10q_accession
            if c_end is not None:
                period_end = c_end
            anchor["awaiting_10q"] = False

    open_gaps: list[str] = []
    if anchor["awaiting_10q"]:
        status: LedgerMatchStatus = "awaiting_current_10q_using_8k_and_prior_10q"
        open_gaps.append("awaiting_current_10q:using_prior_10q_baseline_until_filed")
    elif snap.taxonomy == "ifrs-full" and period_end is None:
        status = "fpi_20f_6k_limited"
        open_gaps.append("fpi_20f_6k_limited:quarterly_xbrl_unavailable")
    elif is_fin:
        status = "financial_sic_gated"
    elif period_end is not None or init_filed is not None:
        status = "same_period_10q_matched"
    else:
        status = "awaiting_current_10q_using_8k_and_prior_10q"
        open_gaps.append("period_p_10q_not_matched_in_snapshot")

    # Reconstruct initial-basis facts (filtered <= init_filed when period P matched, else <= as_of)
    basis_cutoff = init_filed if status in ("same_period_10q_matched", "financial_sic_gated") else as_of
    facts_init = _rebuild_snapshot_facts_dict(snap, filed_cutoff=basis_cutoff)
    facts_latest = _rebuild_snapshot_facts_dict(snap, filed_cutoff=as_of)

    def _pick_line(line_name: str, kind: Literal["quarter", "year"] = "quarter") -> tuple[Period | None, Period | None, LedgerLinePoint | None]:
        ps = _select_line_from_facts(facts_init, line_name, kind, 16, taxonomy=snap.taxonomy, as_of=basis_cutoff)
        if not ps:
            return None, None, None
        cur = _match_period_for_target(ps, target_fy=target_fy, target_fq=target_fq, target_end=period_end) if (period_end or target_fy) else ps[0]
        if cur is None and status == "awaiting_current_10q_using_8k_and_prior_10q":
            cur = ps[0]
        if cur is None:
            return None, None, None
        py = _find_prior_year_period(ps, cur)
        yoy: Decimal | None = None
        if py is not None and py.value != 0:
            yoy = ((cur.value - py.value) / abs(py.value)) * Decimal(100)

        restated = False
        rest_delta: str | None = None
        lat_val_str = _fmt_dec(cur.value, 2)
        if basis_cutoff is not None and (as_of is None or as_of > basis_cutoff):
            ps_lat = _select_line_from_facts(facts_latest, line_name, kind, 16, taxonomy=snap.taxonomy, as_of=as_of)
            lat_hit = _match_period_for_target(ps_lat, target_fy=None, target_fq=None, target_end=cur.end)
            if lat_hit is not None:
                lat_val_str = _fmt_dec(lat_hit.value, 2)
                if cur.value != 0:
                    diff_pct = abs((lat_hit.value - cur.value) / cur.value) * Decimal(100)
                    if diff_pct > Decimal(str(restatement_pct)):
                        restated = True
                        rest_delta = _fmt_dec(((lat_hit.value - cur.value) / abs(cur.value)) * Decimal(100), 2)

        pt = LedgerLinePoint(
            line_name=line_name,
            value=_fmt_dec(cur.value, 4 if "eps" in line_name else 2),
            prior_year_value=_fmt_dec(py.value, 4 if "eps" in line_name else 2) if py else None,
            yoy_pct=_fmt_dec(yoy, 2),
            unit=cur.unit,
            currency=snap.currency,
            tag=cur.tag,
            period_end=cur.end.isoformat(),
            filed_at=cur.filed.isoformat() if cur.filed else None,
            accession=cur.accession,
            derived=cur.derived,
            initial_basis_value=_fmt_dec(cur.value, 4 if "eps" in line_name else 2),
            latest_value=lat_val_str,
            restated_later=restated,
            restatement_delta_pct=rest_delta,
        )
        return cur, py, pt

    lines: dict[str, LedgerLinePoint] = {}
    core_line_names = (
        "revenue",
        "cost_of_revenue",
        "gross_profit",
        "operating_income",
        "net_income",
        "eps_diluted",
        "accounts_receivable",
        "inventory",
        "accounts_payable",
        "operating_cash_flow",
        "capex",
        "ppe_net",
        "rd_expense",
        "sga_expense",
        "selling_expense",
        "admin_expense",
        "total_operating_costs",
        "rpo",
        "deferred_revenue_current",
        "deferred_revenue_noncurrent",
        "stock_based_compensation",
        "depreciation_amortization",
        "restructuring_charges",
        "impairment_charges",
        "purchase_commitments",
        "finance_lease_principal_payments",
        "finance_lease_rou_additions",
    )
    picked: dict[str, tuple[Period | None, Period | None]] = {}
    for ln in core_line_names:
        cur_p, py_p, pt = _pick_line(ln, "quarter")
        picked[ln] = (cur_p, py_p)
        if pt is not None:
            lines[ln] = pt

    # Guard against SEC submissions-vs-companyfacts indexing lag:
    # Require revenue OR >=2 core financial lines actually populated for period_end before declaring same_period_10q_matched!
    if status == "same_period_10q_matched" and period_end is not None:
        matched_target_lines = [
            ln
            for ln, (cp, _pp) in picked.items()
            if cp is not None and abs((cp.end - period_end).days) <= 20
        ]
        if ("revenue" not in matched_target_lines and len(matched_target_lines) < 2) or (
            snap.xbrl_indexing_pending_accession is not None and init_accn == snap.xbrl_indexing_pending_accession
        ):
            status = "awaiting_current_10q_using_8k_and_prior_10q"
            open_gaps.append("xbrl_indexing_lag:submissions_ahead_of_companyfacts")

    rev_cur, rev_py = picked["revenue"]
    cogs_cur, cogs_py = picked["cost_of_revenue"]
    gp_cur, gp_py = picked["gross_profit"]
    oi_cur, oi_py = picked["operating_income"]
    ni_cur, _ni_py = picked["net_income"]
    ar_cur, ar_py = picked["accounts_receivable"]
    inv_cur, inv_py = picked["inventory"]
    ocf_cur, _ocf_py = picked["operating_cash_flow"]
    cap_cur, cap_py = picked["capex"]
    ppe_cur, ppe_py = picked["ppe_net"]
    rpo_cur, rpo_py = picked["rpo"]
    dc_cur, dc_py = picked["deferred_revenue_current"]
    dnc_cur, dnc_py = picked["deferred_revenue_noncurrent"]
    sbc_cur, _sbc_py = picked["stock_based_compensation"]
    da_cur, _da_py = picked["depreciation_amortization"]
    restruct_cur, _ = picked["restructuring_charges"]
    impair_cur, _ = picked["impairment_charges"]
    pcomm_cur, pcomm_py = picked["purchase_commitments"]
    fl_princ_cur, _ = picked["finance_lease_principal_payments"]
    fl_rou_cur, _ = picked["finance_lease_rou_additions"]

    def _yoy(c: Period | None, p: Period | None, *, take_abs: bool = False) -> Decimal | None:
        if c is None or p is None:
            return None
        cv = abs(c.value) if take_abs else c.value
        pv = abs(p.value) if take_abs else p.value
        if pv == 0:
            return None
        return ((cv - pv) / abs(pv)) * Decimal(100)

    rev_yoy = _yoy(rev_cur, rev_py)
    cogs_yoy = _yoy(cogs_cur, cogs_py)
    ar_yoy = _yoy(ar_cur, ar_py) if not is_fin else None
    inv_yoy = _yoy(inv_cur, inv_py) if not is_fin else None
    ar_minus_rev = (ar_yoy - rev_yoy) if (ar_yoy is not None and rev_yoy is not None) else None
    inv_minus_cogs = (inv_yoy - cogs_yoy) if (inv_yoy is not None and cogs_yoy is not None) else None

    # DSO / DIO (non-financial only)
    dso_cur = (ar_cur.value / rev_cur.value * Decimal("91.25")) if (not is_fin and ar_cur and rev_cur and rev_cur.value > 0) else None
    dso_py = (ar_py.value / rev_py.value * Decimal("91.25")) if (not is_fin and ar_py and rev_py and rev_py.value > 0) else None
    dso_yoy = (dso_cur - dso_py) if (dso_cur is not None and dso_py is not None) else None
    dio_cur = (inv_cur.value / cogs_cur.value * Decimal("91.25")) if (not is_fin and inv_cur and cogs_cur and cogs_cur.value > 0) else None
    dio_py = (inv_py.value / cogs_py.value * Decimal("91.25")) if (not is_fin and inv_py and cogs_py and cogs_py.value > 0) else None
    dio_yoy = (dio_cur - dio_py) if (dio_cur is not None and dio_py is not None) else None

    # RPO or Deferred Revenue YoY + Strict QoQ Calculated Billings (Rev_t + TotalDR_t - TotalDR_{t-1})
    rpo_yoy = _yoy(rpo_cur, rpo_py)
    if rpo_yoy is None and (dc_cur is not None or dnc_cur is not None):
        cur_dr = (dc_cur.value if dc_cur else Decimal(0)) + (dnc_cur.value if dnc_cur else Decimal(0))
        py_dr = (dc_py.value if dc_py else Decimal(0)) + (dnc_py.value if dnc_py else Decimal(0))
        if py_dr > 0:
            rpo_yoy = ((cur_dr - py_dr) / py_dr) * Decimal(100)
    rpo_minus_rev = (rpo_yoy - rev_yoy) if (rpo_yoy is not None and rev_yoy is not None) else None
    if rpo_cur is None and dc_cur is None and dnc_cur is None:
        open_gaps.append("rpo:footnote_unstructured_manual")

    calc_billings_cur: Decimal | None = None
    calc_billings_yoy: Decimal | None = None
    if not is_fin and rev_cur is not None:
        dc_series = _select_line_from_facts(facts_init, "deferred_revenue_current", "quarter", 16, taxonomy=snap.taxonomy, as_of=basis_cutoff)
        dnc_series = _select_line_from_facts(facts_init, "deferred_revenue_noncurrent", "quarter", 16, taxonomy=snap.taxonomy, as_of=basis_cutoff)
        dr_by_end: dict[date, Decimal] = {}
        for p_dc in dc_series:
            dr_by_end[p_dc.end] = dr_by_end.get(p_dc.end, Decimal(0)) + p_dc.value
        for p_dnc in dnc_series:
            dr_by_end[p_dnc.end] = dr_by_end.get(p_dnc.end, Decimal(0)) + p_dnc.value

        def _billings_for(rev_p: Period | None) -> Decimal | None:
            if rev_p is None or rev_p.end not in dr_by_end:
                return None
            # Strict contiguous prior quarter Q_{t-1}: 65..115 days before rev_p.end
            prev_ends = [d for d in dr_by_end if 65 <= (rev_p.end - d).days <= 115]
            if not prev_ends:
                return None
            prev_end = max(prev_ends)
            return rev_p.value + (dr_by_end[rev_p.end] - dr_by_end[prev_end])

        calc_billings_cur = _billings_for(rev_cur)
        calc_billings_py = _billings_for(rev_py)
        if calc_billings_cur is not None and calc_billings_py is not None and calc_billings_py > 0:
            calc_billings_yoy = ((calc_billings_cur - calc_billings_py) / abs(calc_billings_py)) * Decimal(100)

    # Track 2 margins & cash quality
    gp_val = gp_cur.value if gp_cur else ((rev_cur.value - cogs_cur.value) if (rev_cur and cogs_cur) else None)
    gp_py_val = gp_py.value if gp_py else ((rev_py.value - cogs_py.value) if (rev_py and cogs_py) else None)
    gm_cur = (gp_val / rev_cur.value * Decimal(100)) if (gp_val is not None and rev_cur and rev_cur.value > 0) else None
    gm_py = (gp_py_val / rev_py.value * Decimal(100)) if (gp_py_val is not None and rev_py and rev_py.value > 0) else None
    gm_yoy_bps = ((gm_cur - gm_py) * Decimal(100)) if (gm_cur is not None and gm_py is not None) else None

    om_cur = (oi_cur.value / rev_cur.value * Decimal(100)) if (oi_cur and rev_cur and rev_cur.value > 0) else None
    om_py = (oi_py.value / rev_py.value * Decimal(100)) if (oi_py and rev_py and rev_py.value > 0) else None
    om_yoy_bps = ((om_cur - om_py) * Decimal(100)) if (om_cur is not None and om_py is not None) else None

    ocf_to_ni = (ocf_cur.value / ni_cur.value) if (ocf_cur and ni_cur and ni_cur.value > 0) else None
    fcf_cur = (ocf_cur.value - abs(cap_cur.value)) if (ocf_cur and cap_cur) else None
    fcf_to_ni = (fcf_cur / ni_cur.value) if (fcf_cur is not None and ni_cur and ni_cur.value > 0) else None

    # Consecutive restructuring / impairment quarters
    consec_restruct = 0
    restruct_ps = _select_line_from_facts(facts_init, "restructuring_charges", "quarter", 8, taxonomy=snap.taxonomy, as_of=basis_cutoff)
    for rp in restruct_ps:
        if abs(rp.value) > 0:
            consec_restruct += 1
        else:
            break

    capex_yoy = _yoy(cap_cur, cap_py, take_abs=True)
    ppe_yoy = _yoy(ppe_cur, ppe_py)
    pcomm_yoy = _yoy(pcomm_cur, pcomm_py)
    if pcomm_cur is None:
        open_gaps.append("purchase_commitments:footnote_unstructured_manual")

    # Convert pre-supplied redline added paragraphs into RedlineRiskSnippet entries (scored in Layer 3)
    redline_snips: list[RedlineRiskSnippet] = []
    for item in redline_added_paragraphs:
        txt = str(item.get("current_text") or item.get("paragraph_text") or "").strip()
        if len(txt) < 40:
            continue
        redline_snips.append(
            RedlineRiskSnippet(
                section_key=str(item.get("section_key") or "item_1a_risk_factors"),
                paragraph_text=txt[:2000],
                filing_accession=redline_current_accession,
            )
        )

    eff_end = period_end or (rev_cur.end if rev_cur else None)
    return Sec10QCollisionLedger(
        status=status,
        entity_key=entity_key,
        cik=snap.cik,
        symbol=snap.symbol,
        company_name=snap.company_name,
        sic=snap.sic,
        sector_category=sector_cat,
        is_financial_gated=is_fin,
        accounting_standard=snap.taxonomy,
        currency=snap.currency,
        target_fiscal_year=target_fy,
        target_fiscal_quarter=target_fq,
        fiscal_period_key=anchor["fiscal_period_key"] or (rev_cur.label if rev_cur else None),
        period_end_date=eff_end.isoformat() if eff_end else None,
        call_date=call_date_iso,
        initial_10q_filed_at=init_filed.isoformat() if init_filed else None,
        initial_10q_accession=init_accn,
        latest_10q_accession=snap.latest_periodic_accession,
        p1_fiscal_year=anchor["p1_fy"],
        p1_fiscal_quarter=anchor["p1_fq"],
        p1_10q_filed_at=anchor["p1_10q_filed_at"].isoformat() if anchor["p1_10q_filed_at"] else None,
        p1_10q_accession=anchor["p1_10q_accession"],
        as_of_date=as_of.isoformat() if as_of else None,
        revenue_yoy_pct=_fmt_dec(rev_yoy, 2),
        ar_yoy_pct=_fmt_dec(ar_yoy, 2),
        ar_minus_rev_pp=_fmt_dec(ar_minus_rev, 2),
        inv_yoy_pct=_fmt_dec(inv_yoy, 2),
        cogs_yoy_pct=_fmt_dec(cogs_yoy, 2),
        inv_minus_cogs_pp=_fmt_dec(inv_minus_cogs, 2),
        dso_days=_fmt_dec(dso_cur, 1),
        dso_yoy_days=_fmt_dec(dso_yoy, 1),
        dio_days=_fmt_dec(dio_cur, 1),
        dio_yoy_days=_fmt_dec(dio_yoy, 1),
        rpo_or_defrev_yoy_pct=_fmt_dec(rpo_yoy, 2),
        rpo_minus_rev_pp=_fmt_dec(rpo_minus_rev, 2),
        calculated_billings=_fmt_dec(calc_billings_cur, 2),
        calculated_billings_yoy_pct=_fmt_dec(calc_billings_yoy, 2),
        gross_margin_pct=_fmt_dec(gm_cur, 2),
        gross_margin_yoy_bps=_fmt_dec(gm_yoy_bps, 0),
        operating_margin_pct=_fmt_dec(om_cur, 2),
        operating_margin_yoy_bps=_fmt_dec(om_yoy_bps, 0),
        ocf_to_ni_ratio=_fmt_dec(ocf_to_ni, 2),
        fcf_to_ni_ratio=_fmt_dec(fcf_to_ni, 2),
        stock_based_compensation=_fmt_dec(sbc_cur.value, 2) if sbc_cur else None,
        depreciation_amortization=_fmt_dec(da_cur.value, 2) if da_cur else None,
        restructuring_charges=_fmt_dec(restruct_cur.value, 2) if restruct_cur else None,
        impairment_charges=_fmt_dec(impair_cur.value, 2) if impair_cur else None,
        consecutive_restructuring_quarters=consec_restruct,
        capex_yoy_pct=_fmt_dec(capex_yoy, 2),
        ppe_net_yoy_pct=_fmt_dec(ppe_yoy, 2),
        purchase_commitments_yoy_pct=_fmt_dec(pcomm_yoy, 2),
        finance_lease_principal_payments=_fmt_dec(abs(fl_princ_cur.value), 2) if fl_princ_cur else None,
        finance_lease_rou_additions=_fmt_dec(abs(fl_rou_cur.value), 2) if fl_rou_cur else None,
        capital_allocation_summary=dict(capital_allocation_summary) if capital_allocation_summary else None,
        redline_added_risks=redline_snips,
        redline_current_accession=redline_current_accession,
        redline_prior_accession=redline_prior_accession,
        lines=lines,
        open_gaps=open_gaps,
    )


def compute_streaming_10q_redline(
    *,
    cik: str,
    current_accession: str,
    current_primary_doc: str,
    prior_accession: str,
    prior_primary_doc: str,
    current_form: str = "10-Q",
    prior_form: str = "10-Q",
    user_agent: str | None = None,
    limiter: Any = None,
    policy: Any = None,
    transport: Any = None,
    cache_repo: Any = None,
) -> list[dict[str, Any]]:
    """Stream-download and diff Item 1A / MD&A between two 10-Q/10-K filings (<5MB RAM via 64KB HTMLParser).

    Caches the compact substantive-added paragraph summary in ``sec_10q_redline_cache`` keyed by
    ``redline:{cik}:{cur_accn}:{prior_accn}``.
    """
    from .fin_data.adapters.sec_deep import diff_filing_sections, split_filing_sections, stream_strip_sec_html_file
    from .fin_data.adapters.sec_events.pacing import sec_get_to_tempfile

    cik_num = str(int(cik)) if cik.isdigit() else cik
    cache_key = f"redline:{cik.zfill(10)}:{current_accession}:{prior_accession}"
    if cache_repo is not None:
        cached = cache_repo.get(cache_key)
        if isinstance(cached, Mapping) and isinstance(cached.get("added_paragraphs"), list):
            return list(cached["added_paragraphs"])

    def _fetch_sections(accn: str, doc_name: str, form: str) -> Any:
        accn_nodash = accn.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{accn_nodash}/{doc_name}"
        tmp_path: Path | None = None
        try:
            # Download streaming to disk OUTSIDE heavy_memory_section (~64KB buffer) so concurrent fetches don't block!
            tmp_path, _sha, _sz = sec_get_to_tempfile(
                url,
                category="filings",
                user_agent=user_agent,
                limiter=limiter,
                policy=policy,
                transport=transport,
                accept="text/html, text/plain",
                max_bytes=80_000_000,
                suffix=".htm",
            )
            with heavy_memory_section():
                plain = stream_strip_sec_html_file(tmp_path)
            return split_filing_sections(plain, form=form, is_html=False)
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    cur_bundle = _fetch_sections(current_accession, current_primary_doc, current_form)
    pri_bundle = _fetch_sections(prior_accession, prior_primary_doc, prior_form)
    report = diff_filing_sections(
        cur_bundle,
        pri_bundle,
        section_keys=("item_1a_risk_factors", "mda"),
        filter_rolling_noise=True,
    )
    added_out: list[dict[str, Any]] = []
    for sec_key in ("item_1a_risk_factors", "mda"):
        sdiff = report.section_diffs.get(sec_key)
        if sdiff is None:
            continue
        for para in sdiff.added[:20]:
            if para.current_text and len(para.current_text) >= 60:
                added_out.append({"section_key": sec_key, "current_text": para.current_text[:2000]})

    if cache_repo is not None:
        cache_repo.put(
            cache_key,
            {
                "cik": cik.zfill(10),
                "current_accession": current_accession,
                "prior_accession": prior_accession,
                "added_paragraphs": added_out,
                "cached_at": _utcnow().isoformat(),
            },
        )
    return added_out


class _PacingToRateLimiterShim:
    """Adapt ``SecPacingLimiter`` (wait-based) to ``SecEdgarAdapter``'s ``RateLimiter.acquire`` contract."""

    def __init__(self, pacing_limiter: Any) -> None:
        self._pacing = pacing_limiter

    def acquire(self, provider: str = "sec_edgar", cost: float = 1.0) -> Any:
        from types import SimpleNamespace

        if self._pacing is not None and hasattr(self._pacing, "wait"):
            self._pacing.wait()
        return SimpleNamespace(allowed=True, retry_after_seconds=0.0, provider=provider, cost=cost)


def _format_r_table_rows_markdown(td: Any, *, max_rows: int = 24) -> tuple[str, int]:
    """Extract compact Markdown rows from ``FilingTableData`` (handling ``values_by_column``, ``cells``, and ``subtables``)."""
    md_rows: list[str] = []
    header_parts: list[str] = []
    if getattr(td, "unit_header_note", None):
        header_parts.append(str(td.unit_header_note).strip())
    elif getattr(td, "currency", None) or getattr(td, "unit_scale", None):
        header_parts.append(f"{getattr(td, 'currency', '') or ''} {getattr(td, 'unit_scale', '') or ''}".strip())
    periods = getattr(td, "periods", None) or []
    if periods:
        header_parts.append("Periods: " + " | ".join(str(p).strip() for p in list(periods)[:6] if str(p).strip()))
    if header_parts:
        md_rows.append(" · ".join(p for p in header_parts if p)[:180])

    def _emit_rows(rows_seq: Sequence[Any]) -> int:
        cnt = 0
        for row in list(rows_seq)[:max_rows]:
            if len(md_rows) >= max_rows + 2:
                break
            line = ""
            if isinstance(row, Mapping):
                lbl = str(row.get("label") or row.get("name") or "").strip()
                vals = row.get("values_by_column") or row.get("values") or row.get("cells") or []
                if isinstance(vals, Mapping):
                    v_str = " | ".join(f"{k}: {v}" for k, v in list(vals.items())[:5] if str(v).strip())
                elif isinstance(vals, Sequence) and not isinstance(vals, str):
                    v_str = " | ".join(str(v).strip() for v in list(vals)[:5] if str(v).strip())
                else:
                    v_str = str(vals).strip()
                line = f"{lbl} | {v_str}".strip(" |")
            elif isinstance(row, Sequence) and not isinstance(row, str):
                line = " | ".join(str(c).strip() for c in list(row)[:6] if str(c).strip())
            else:
                lbl = str(getattr(row, "label", "") or "").strip()
                vbc = getattr(row, "values_by_column", None)
                cells = getattr(row, "cells", None)
                vals = getattr(row, "values", None)
                if isinstance(vbc, Mapping) and vbc:
                    v_str = " | ".join(f"{k}: {v}" for k, v in list(vbc.items())[:5] if str(v).strip())
                elif isinstance(cells, Sequence) and not isinstance(cells, str) and cells:
                    v_str = " | ".join(
                        str(getattr(c, "raw_text", c) or "").strip()
                        for c in list(cells)[:5]
                        if str(getattr(c, "raw_text", c) or "").strip()
                    )
                elif isinstance(vals, Mapping):
                    v_str = " | ".join(f"{k}: {v}" for k, v in list(vals.items())[:5] if str(v).strip())
                elif isinstance(vals, Sequence) and not isinstance(vals, str):
                    v_str = " | ".join(str(v).strip() for v in list(vals)[:5] if str(v).strip())
                else:
                    v_str = str(vals or "").strip()
                line = f"{lbl} | {v_str}".strip(" |")
            if line:
                md_rows.append(line[:200])
                cnt += 1
        return cnt

    total_rows = _emit_rows(getattr(td, "rows", None) or [])
    if total_rows == 0:
        for sub in (getattr(td, "subtables", None) or [])[:3]:
            sub_title = str(getattr(sub, "title", "") or "").strip()
            if sub_title:
                md_rows.append(f"## {sub_title[:120]}")
            total_rows += _emit_rows(getattr(sub, "rows", None) or [])
    return "\n".join(md_rows)[:2600], total_rows


def fetch_footnote_fallback_tables(
    *,
    adapter: Any = None,
    cik: str,
    accession: str,
    keywords: Sequence[str] = (
        "remaining performance",
        "commitment",
        "purchase obligation",
        "deferred revenue",
        "contract",
        "segment",
        "disaggregated revenue",
        "backlog",
    ),
    max_tables: int = 2,
    max_bytes: int = 1_500_000,
    user_agent: str | None = None,
    limiter: Any = None,
    policy: Any = None,
    transport: Any = None,
    cache_repo: Any = None,
) -> list[dict[str, Any]]:
    """Lightweight ``FilingSummary.xml`` + <=max_tables ``R*.htm`` (<=1.5MB) footnote loader.

    Supports both direct ``sec_get_text`` (shared ``SecPacingLimiter``) and ``adapter=SecEdgarAdapter``
    paths, scans ``catalog.all_reports`` (capturing ``Details`` in ``catalog.other`` as well as
    ``Tables``/``Notes``), and extracts ``FilingTableRow.values_by_column`` / ``cells`` / ``subtables``.
    """
    from .fin_data.adapters.sec_deep import (
        fetch_filing_table,
        normalize_accession,
        parse_filing_summary_xml,
        parse_r_html_table,
        sec_filing_tables,
    )
    from .fin_data.adapters.sec_edgar import cik10
    from .fin_data.adapters.sec_events.pacing import sec_get_text

    target_cik = cik10(cik)
    dashed, nodash = normalize_accession(accession)
    base_url = f"https://www.sec.gov/Archives/edgar/data/{int(target_cik)}/{nodash}"

    catalog: Any = None
    if adapter is not None:
        # Adapt SecPacingLimiter if passed directly on adapter
        if getattr(adapter, "limiter", None) is not None and not hasattr(adapter.limiter, "acquire"):
            adapter.limiter = _PacingToRateLimiterShim(adapter.limiter)
        res = sec_filing_tables(adapter, cik=target_cik, accession=dashed)
        if res.status != "ok" or not res.data:
            return []
        catalog = res.data[0]
    else:
        summary_url = f"{base_url}/FilingSummary.xml"
        sum_cache_key = f"fn_sum:{target_cik}:{dashed}"
        xml_text: str | None = None
        if cache_repo is not None:
            cached_sum = cache_repo.get(sum_cache_key)
            if isinstance(cached_sum, Mapping) and isinstance(cached_sum.get("xml"), str):
                xml_text = cached_sum["xml"]
        if xml_text is None:
            xml_text, _ct_s, _sha_s = sec_get_text(
                summary_url,
                category="filings",
                user_agent=user_agent,
                limiter=limiter,
                policy=policy,
                transport=transport,
                max_bytes=2_000_000,
            )
            if cache_repo is not None and xml_text:
                cache_repo.put(sum_cache_key, {"xml": xml_text[:1_500_000], "cached_at": _utcnow().isoformat()})
        if not xml_text:
            return []
        catalog = parse_filing_summary_xml(xml_text, cik=target_cik, accession=dashed, filing_summary_url=summary_url)

    entries = list(getattr(catalog, "all_reports", None) or getattr(catalog, "tables", None) or [])
    kws = [k.strip().casefold() for k in keywords if isinstance(k, str) and len(k.strip()) >= 3]
    _exclude_re = re.compile(r"\b(?:cover\s+page|document\s+and\s+entity|summary\s+of\s+significant\s+accounting\s+policies)\b", re.I)

    scored: list[tuple[int, int, Any]] = []
    for idx_e, entry in enumerate(entries):
        name_blob = f"{getattr(entry, 'short_name', '')} {getattr(entry, 'long_name', '')}".strip()
        low_blob = name_blob.casefold()
        if not low_blob or _exclude_re.search(low_blob):
            continue
        menu_cat = str(getattr(entry, "menu_category", "") or "").casefold()
        norm_cat = str(getattr(entry, "normalized_category", "") or "").casefold()
        if norm_cat == "cover" or "cover" in menu_cat:
            continue
        kw_hits = sum(1 for k in kws if k in low_blob)
        if kw_hits <= 0:
            continue
        if "detail" in low_blob or "detail" in menu_cat:
            cat_weight = 3
        elif norm_cat in ("tables", "notes") or "table" in menu_cat or "note" in menu_cat:
            cat_weight = 2
        else:
            cat_weight = 0
        # Prefer Details/Tables/Notes over bare primary Statements
        if norm_cat == "statements" and kw_hits < 2:
            continue
        scored.append((kw_hits * 4 + cat_weight, -idx_e, entry))

    scored.sort(reverse=True)
    matched = [item[2] for item in scored[:max_tables]]

    out: list[dict[str, Any]] = []
    for entry in matched:
        r_file = getattr(entry, "r_file", None) or getattr(entry, "html_file", None)
        if not r_file:
            continue
        r_cache_key = f"fn_tbl:{target_cik}:{dashed}:{r_file}"
        if cache_repo is not None:
            cached_tbl = cache_repo.get(r_cache_key)
            if isinstance(cached_tbl, Mapping) and cached_tbl.get("r_file") == r_file:
                out.append(dict(cached_tbl))
                continue

        td: Any = None
        if adapter is not None:
            tbl_res = fetch_filing_table(
                adapter,
                cik=target_cik,
                accession=dashed,
                r_file=r_file,
                max_bytes=max_bytes,
            )
            if tbl_res.status == "ok" and tbl_res.data:
                td = tbl_res.data[0]
        else:
            r_url = f"{base_url}/{r_file}"
            r_html, _ct_r, _sha_r = sec_get_text(
                r_url,
                category="filings",
                user_agent=user_agent,
                limiter=limiter,
                policy=policy,
                transport=transport,
                max_bytes=max_bytes,
            )
            if r_html:
                td = parse_r_html_table(
                    r_html,
                    r_file=r_file,
                    url=r_url,
                    accession=dashed,
                )

        if td is not None:
            rows_preview, total_rows = _format_r_table_rows_markdown(td)
            item_dict = {
                "r_file": td.r_file,
                "title": td.title or getattr(entry, "short_name", r_file),
                "category": getattr(entry, "menu_category", None),
                "currency": td.currency,
                "unit_scale": td.unit_scale,
                "narrative_excerpt": (td.narrative_text or "")[:1200],
                "rows_preview": rows_preview,
                "row_count": total_rows or len(getattr(td, "rows", None) or []),
            }
            if cache_repo is not None:
                cache_repo.put(r_cache_key, item_dict)
            out.append(item_dict)
    return out


# --------------------------------------------------------------------------- Layer 3: Zero-Drift Quote Grounder

def _resolve_local_window(
    *,
    parsed_doc: ParsedCallDocument | None,
    raw_text: str,
    turn_index: int | None = None,
    slide_index: int | None = None,
    exchange_index: int | None = None,
    part: Literal["question", "answer", "both"] = "both",
) -> tuple[tuple[int, int] | None, str | None, int | None]:
    """Return ``(local_window, timestamp_hint, slide_hint)`` so short quotes match in the exact turn/slide first."""
    if parsed_doc is not None:
        if exchange_index is not None:
            for ex in parsed_doc.qa_exchanges:
                if ex.exchange_index == exchange_index:
                    if part == "question":
                        return (ex.question_raw_start, ex.question_raw_end), ex.timestamp_start, (ex.slide_references[0] if ex.slide_references else None)
                    if part == "answer":
                        return (ex.answer_raw_start, ex.answer_raw_end), ex.timestamp_start, (ex.slide_references[0] if ex.slide_references else None)
                    return (ex.raw_char_start, ex.raw_char_end), ex.timestamp_start, (ex.slide_references[0] if ex.slide_references else None)
        if turn_index is not None:
            for t in parsed_doc.turns:
                if t.turn_index == turn_index:
                    return (t.raw_char_start, t.raw_char_end), t.timestamp_start, (t.slide_references[0] if t.slide_references else None)
    if slide_index is not None and raw_text:
        m = re.search(rf"\[Slide\s+{slide_index}\b", raw_text)
        if m:
            nxt = re.search(r"\f\[Slide\s+\d+\b", raw_text[m.start() + 1 :])
            end = (m.start() + 1 + nxt.start()) if nxt else len(raw_text)
            return (m.start(), end), None, slide_index
    return None, None, slide_index


def ground_v3a_quote(
    *,
    quotation: str,
    source_id: str,
    source_texts: Mapping[str, str],
    document_pages_by_source: Mapping[str, Mapping[str, Any]] | None = None,
    parsed_docs_by_source: Mapping[str, ParsedCallDocument] | None = None,
    rules: CompiledV3aRules | None = None,
    turn_index: int | None = None,
    slide_index: int | None = None,
    exchange_index: int | None = None,
    part: Literal["question", "answer", "both"] = "both",
) -> GroundedQuoteRef:
    """Ground a quote to exact raw offsets (local window first), page/slide/timestamp, and 2-tuple negation scan."""
    compiled = rules or load_v3a_call_rules()
    raw_text = source_texts.get(source_id) or ""
    if not quotation or not raw_text:
        return GroundedQuoteRef(source_id=source_id, quotation=quotation or "", verified=False)

    parsed_doc = (parsed_docs_by_source or {}).get(source_id)
    local_win, ts_hint, slide_hint = _resolve_local_window(
        parsed_doc=parsed_doc,
        raw_text=raw_text,
        turn_index=turn_index,
        slide_index=slide_index,
        exchange_index=exchange_index,
        part=part,
    )
    match, raw_start, raw_end = find_quotation_span_with_raw(
        quotation,
        raw_text,
        local_window=local_win,
    )
    if match is None or raw_start is None or raw_end is None:
        return GroundedQuoteRef(source_id=source_id, quotation=quotation, verified=False)

    markers = tuple(dict.fromkeys([*DEFAULT_NEGATION_EXCEPTION_MARKERS, *compiled.rules.negation_context_markers]))
    neg_warn, neg_surr = check_negation_context(match, markers=markers)

    doc_pages = (document_pages_by_source or {}).get(source_id)
    page_num = page_for_offset(dict(doc_pages), raw_start) if doc_pages else None
    resolved_slide = slide_hint
    if resolved_slide is None and doc_pages and doc_pages.get("method") == "pptx_slides_v1":
        resolved_slide = page_num

    # Look up timestamp from parsed turns if subtitle source
    resolved_ts = ts_hint
    if resolved_ts is None and parsed_doc is not None and parsed_doc.turns:
        for t in parsed_doc.turns:
            if t.raw_char_start <= raw_start <= t.raw_char_end and t.timestamp_start:
                resolved_ts = t.timestamp_start
                break

    return GroundedQuoteRef(
        source_id=source_id,
        quotation=quotation,
        verified=True,
        raw_char_start=raw_start,
        raw_char_end=raw_end,
        page_number=page_num,
        slide_number=resolved_slide,
        timestamp_start=resolved_ts,
        match_space=match.match_space,
        quote_context_warning=neg_warn,
        surrounding_context=neg_surr,
    )


# --------------------------------------------------------------------------- Layer 3: Item 1A Dual-Factor Screening (Track 5)

def screen_item1a_omissions(
    *,
    redline_snippets: Sequence[RedlineRiskSnippet],
    call_corpus_text: str,
    rules: CompiledV3aRules | None = None,
) -> list[RedlineRiskSnippet]:
    """Dual-factor 0-LLM Item 1A omission screen: Risk-Bucket Coverage + Stemmed-Entity Coverage.

    Eliminates false positives like ``"tariff"`` vs ``"customs duties"`` (both map to ``export_control``)
    and flags only substantive newly added risk paragraphs genuinely absent from management's call.
    """
    compiled = rules or load_v3a_call_rules()
    buckets = compiled.rules.item1a_risk_buckets
    cov_floor = compiled.rules.ledger_collision_thresholds.omission_entity_coverage_floor
    call_low = (call_corpus_text or "").casefold()
    call_stems = {_stem_token(t) for t in tokenize_query(call_low) if len(t) >= 4}

    # Precompute which risk buckets were discussed anywhere in the call
    call_buckets_hit: set[str] = set()
    for b_name, terms in buckets.items():
        if any(term.casefold() in call_low for term in terms):
            call_buckets_hit.add(b_name)

    _stop = {
        "company", "business", "results", "operations", "financial", "condition", "material",
        "adverse", "effect", "could", "would", "should", "might", "including", "certain",
        "factors", "risks", "quarter", "fiscal", "period", "ended", "management", "future",
    }

    scored: list[RedlineRiskSnippet] = []
    _HIGH_RISK_MDA_BUCKETS = frozenset({"litigation_regulatory", "liquidity_going_concern", "export_control", "cyber_incident", "customer_concentration"})
    for snip in redline_snippets:
        p_low = snip.paragraph_text.casefold()
        hit_buckets = [
            b_name
            for b_name, terms in buckets.items()
            if any(term.casefold() in p_low for term in terms)
        ]
        # Routine MD&A narrative fluctuates every quarter; restrict MD&A alerts to high-risk buckets only
        sec_low = (snip.section_key or "").casefold()
        if "mda" in sec_low and "item_1a" not in sec_low:
            hit_buckets = [b for b in hit_buckets if b in _HIGH_RISK_MDA_BUCKETS]
        raw_toks = [t for t in tokenize_query(p_low) if len(t) >= 4 and t not in _stop]
        stems = list(dict.fromkeys(_stem_token(t) for t in raw_toks))[:24]
        overlap_ratio = (
            sum(1 for st in stems if st in call_stems) / float(len(stems))
            if stems
            else 1.0
        )
        bucket_covered = bool(hit_buckets) and all(b in call_buckets_hit for b in hit_buckets)
        # Dual-factor trigger (eliminates synonym false positives such as "tariff" vs "customs duties"):
        # Flag only when a concrete risk bucket is hit AND that risk bucket was NOT discussed in the call
        # AND the paragraph's stemmed entities are not already extensively covered in the call.
        entity_low_bound = max(0.45, cov_floor)
        flagged = bool(hit_buckets) and (not bucket_covered) and (overlap_ratio < entity_low_bound)
        scored.append(
            snip.model_copy(
                update={
                    "risk_buckets": hit_buckets,
                    "entity_tokens": stems[:12],
                    "call_coverage_ratio": round(overlap_ratio, 3),
                    "bucket_covered_in_call": bucket_covered,
                    "flagged_omission": flagged,
                }
            )
        )
    return scored


# --------------------------------------------------------------------------- Layer 3: Segment Scope Guard, Rounding Tolerance & Cross-Lane Dedup

_SEGMENT_SCOPE_RE = re.compile(
    r"(?:\b(?:intelligent\s+cloud|microsoft\s+cloud|productivity\s+and\s+business|more\s+personal\s+computing|"
    r"azure|aws|google\s+cloud|cloud\s+revenue|data\s+center|gaming|automotive|professional\s+visualization|"
    r"services\s+revenue|iphone|ipad|mac\s+revenue|wearables|family\s+of\s+apps|reality\s+labs|advertising\s+revenue|"
    r"segment|sub-?segment|division|product\s+line|business\s+unit|standalone\s+product|copilot|blackwell\s+revenue|"
    r"north\s+america|international|greater\s+china|china\s+revenue|emea|apac)\b|"
    r"(?:分部|子业务|云业务|数据中心|游戏业务|智能云|产品线|大中华区|北美区))",
    re.IGNORECASE,
)
_CONSOLIDATED_EXPLICIT_RE = re.compile(
    r"(?:\b(?:total\s+(?:company\s+)?(?:revenue|revenues|sales|operating\s+income|gross\s+margin|capex|operating\s+cash\s+flow)|"
    r"consolidated\s+(?:revenue|revenues|operating|gross\s+margin|cash\s+flow)|company-?wide)\b|"
    r"(?:总营收|合并营收|公司整体|总收入|合并口径))",
    re.IGNORECASE,
)


def _infer_effective_scope(
    declared_scope: ClaimScope,
    metric_canonical: str,
    kpi_key: str | None,
    summary_text: str,
    quote_text: str,
) -> ClaimScope:
    """Guard against segment/product figures colliding into consolidated XBRL lines (R3-D6)."""
    if declared_scope != "consolidated":
        return declared_scope
    if metric_canonical not in TWELVE_XBRL_METRICS:
        return "consolidated"
    blob = f"{kpi_key or ''} {summary_text} {quote_text}"
    if _CONSOLIDATED_EXPLICIT_RE.search(blob):
        return "consolidated"
    if _SEGMENT_SCOPE_RE.search(blob):
        return "segment"
    return "consolidated"


def _rounding_half_step_tolerance(
    raw_value: str | None,
    unit: str | None,
    scale: str | None,
    metric_canonical: str,
) -> Decimal:
    """Compute half-ULP presentation rounding tolerance (e.g. '$35.1B' -> ±$0.05B) so rounding never fires red."""
    if not raw_value:
        return Decimal("0")
    cleaned = re.sub(r"[,$€£¥%\s]", "", str(raw_value).strip())
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", cleaned):
        return Decimal("0")
    if "." in cleaned:
        dec_places = len(cleaned.split(".", 1)[1])
        half_step = Decimal("0.5") * (Decimal("10") ** (-dec_places))
    else:
        half_step = Decimal("0.5")
    scaled = _scale_claim_value(half_step, unit, scale, metric_canonical)
    return abs(scaled) if scaled is not None else Decimal("0")


def _dedup_factual_claims(
    claims: Sequence[ManagementFactualClaim],
) -> list[ManagementFactualClaim]:
    """Deterministically merge duplicate claims targeting the exact same (metric, scope, period, operator, value)."""
    best: dict[tuple[Any, ...], ManagementFactualClaim] = {}
    order: list[tuple[Any, ...]] = []
    for cl in claims:
        eff_scope = _infer_effective_scope(cl.scope, cl.metric_canonical, cl.kpi_key, cl.claim_summary, cl.verbatim_quote)
        key = (
            cl.metric_canonical,
            (cl.kpi_key or "").casefold() if eff_scope != "consolidated" or cl.metric_canonical == "other" else "",
            eff_scope,
            cl.target_fy,
            cl.target_fq,
            cl.period_scope,
            cl.operator,
            (cl.value_low or "").strip(),
            (cl.value_high or "").strip(),
        )
        if key not in best:
            best[key] = cl.model_copy(update={"scope": eff_scope})
            order.append(key)
        else:
            prev = best[key]
            # Prefer explicit sufficiency, CFO speaker, or longer verbatim quote
            prev_score = (
                (2 if prev.evidence_sufficiency == "explicit" else 0)
                + (1 if prev.speaker_role == "cfo" else 0)
            )
            cur_score = (
                (2 if cl.evidence_sufficiency == "explicit" else 0)
                + (1 if cl.speaker_role == "cfo" else 0)
            )
            if cur_score > prev_score:
                best[key] = cl.model_copy(update={"scope": eff_scope})
    return [best[k] for k in order]


# --------------------------------------------------------------------------- Layer 3: Commitment Grading & Silent Drop

_PCT_UNIT_TOKENS = frozenset({"%", "percent", "pct", "percentage", "bps", "basis_points", "pp"})
_MONEY_SCALE_TOKENS = frozenset({"thousands", "thousand", "k", "millions", "million", "m", "billions", "billion", "b", "trillions", "trillion", "t"})
_QOQ_ONLY_RE = re.compile(r"(?:\b(?:qoq|sequential(?:ly)?|quarter[- ]over[- ]quarter|from\s+(?:the\s+)?prior\s+quarter)\b|环比)", re.I)
_YOY_HINT_RE = re.compile(r"(?:\b(?:yoy|year[- ]over[- ]year|year[- ]on[- ]year|annual(?:ized)?\s+growth|vs\.?\s+prior\s+year)\b|同比)", re.I)


def _is_pct_or_bps_unit(unit: str | None, scale: str | None) -> bool:
    u = (unit or "").strip().casefold()
    s = (scale or "").strip().casefold()
    return u in _PCT_UNIT_TOKENS or s in _PCT_UNIT_TOKENS or "%" in u


def _is_bps_unit(unit: str | None, scale: str | None) -> bool:
    u = (unit or "").strip().casefold()
    s = (scale or "").strip().casefold()
    return u in ("bps", "basis_points") or s in ("bps", "basis_points")


def _check_dimension_compatibility(
    *,
    metric_canonical: str,
    operator: CommitmentOperator,
    unit: str | None,
    scale: str | None,
    text_blob: str,
) -> tuple[Literal["ok", "reroute_yoy", "mismatch"], CommitmentOperator, str | None]:
    """Deterministic unit/dimension guard preventing %/bps growth rates from colliding with absolute $ totals (and vice versa)."""
    is_margin = metric_canonical in _MARGIN_METRICS
    is_pct = _is_pct_or_bps_unit(unit, scale)
    s_low = (scale or "").strip().casefold()

    if is_margin and s_low in _MONEY_SCALE_TOKENS:
        return "mismatch", operator, f"利润率指标 {metric_canonical} 声明量纲为金额规模 ({scale})，跳过绝对值硬对撞。"

    if not is_margin and is_pct:
        if operator in ("growth_yoy_ge", "growth_yoy_le", "growth_yoy_approx", "growth_yoy_between"):
            return "ok", operator, None
        has_qoq = bool(_QOQ_ONLY_RE.search(text_blob or ""))
        has_yoy = bool(_YOY_HINT_RE.search(text_blob or ""))
        if has_qoq and not has_yoy:
            return "mismatch", operator, "该表述为环比 (QoQ) 增速百分比，不与 10-Q 单季绝对金额硬对撞。"
        if operator == "approx":
            return "reroute_yoy", "growth_yoy_approx", None
        if operator == "between":
            return "reroute_yoy", "growth_yoy_between", None
        if operator == "ge":
            return "reroute_yoy", "growth_yoy_ge", None
        if operator == "le":
            return "reroute_yoy", "growth_yoy_le", None
        return "mismatch", operator, "百分比/基点量纲不与 10-Q 绝对金额硬对撞。"

    return "ok", operator, None


def _evaluate_operator(
    *,
    operator: CommitmentOperator,
    actual: Decimal,
    prior_year: Decimal | None,
    low: Decimal | None,
    high: Decimal | None,
    rel_tol_pct: Decimal,
    abs_tol: Decimal,
    is_margin_metric: bool = False,
) -> tuple[bool, str]:
    """Evaluate 9 commitment operators against Decimal actual with configured tolerance."""
    if operator == "ge":
        if low is None:
            return True, "no_threshold"
        margin = max(abs(low) * rel_tol_pct / Decimal(100), abs_tol)
        passed = actual >= (low - margin)
        return passed, f"actual={_fmt_dec(actual, 2)} vs >= {_fmt_dec(low, 2)}"
    if operator == "le":
        bound = high if high is not None else low
        if bound is None:
            return True, "no_threshold"
        margin = max(abs(bound) * rel_tol_pct / Decimal(100), abs_tol)
        passed = actual <= (bound + margin)
        return passed, f"actual={_fmt_dec(actual, 2)} vs <= {_fmt_dec(bound, 2)}"
    if operator == "between":
        lo = low if low is not None else high
        hi = high if high is not None else low
        if lo is None or hi is None:
            return True, "incomplete_range"
        lo_m = max(abs(lo) * rel_tol_pct / Decimal(100), abs_tol)
        hi_m = max(abs(hi) * rel_tol_pct / Decimal(100), abs_tol)
        passed = (lo - lo_m) <= actual <= (hi + hi_m)
        return passed, f"actual={_fmt_dec(actual, 2)} vs [{_fmt_dec(lo, 2)}, {_fmt_dec(hi, 2)}]"
    if operator == "approx":
        target = low if low is not None else high
        if target is None:
            return True, "no_target"
        margin = max(abs(target) * rel_tol_pct / Decimal(100), abs_tol)
        passed = abs(actual - target) <= margin
        return passed, f"actual={_fmt_dec(actual, 2)} vs ~{_fmt_dec(target, 2)}"
    if operator in ("growth_yoy_ge", "growth_yoy_le", "growth_yoy_approx", "growth_yoy_between"):
        if prior_year is None or (not is_margin_metric and prior_year == 0):
            return True, "prior_year_unavailable"
        if is_margin_metric:
            # Align with xbrl_actuals_lookup: margin YoY is percentage-point delta (actual - prior_year)
            yoy = actual - prior_year
            unit_lbl = "pp"
        else:
            yoy = ((actual - prior_year) / abs(prior_year)) * Decimal(100)
            unit_lbl = "%"
        tol = max(rel_tol_pct, abs_tol, Decimal("1.0"))
        if operator == "growth_yoy_ge":
            if low is None:
                return True, "no_threshold"
            passed = yoy >= (low - tol)
            return passed, f"YoY={_fmt_dec(yoy, 2)}{unit_lbl} vs >= {_fmt_dec(low, 2)}{unit_lbl}"
        if operator == "growth_yoy_le":
            bound = high if high is not None else low
            if bound is None:
                return True, "no_threshold"
            passed = yoy <= (bound + tol)
            return passed, f"YoY={_fmt_dec(yoy, 2)}{unit_lbl} vs <= {_fmt_dec(bound, 2)}{unit_lbl}"
        if operator == "growth_yoy_approx":
            target = low if low is not None else high
            if target is None:
                return True, "no_target"
            passed = abs(yoy - target) <= tol
            return passed, f"YoY={_fmt_dec(yoy, 2)}{unit_lbl} vs ~{_fmt_dec(target, 2)}{unit_lbl}"
        lo = low if low is not None else high
        hi = high if high is not None else low
        if lo is None or hi is None:
            return True, "incomplete_range"
        passed = (lo - tol) <= yoy <= (hi + tol)
        return passed, f"YoY={_fmt_dec(yoy, 2)}{unit_lbl} vs [{_fmt_dec(lo, 2)}, {_fmt_dec(hi, 2)}]{unit_lbl}"
    if operator == "direction_up_down":
        if prior_year is None:
            return True, "prior_year_unavailable"
        want_down = (low is not None and low < 0)
        passed = (actual <= prior_year) if want_down else (actual >= prior_year)
        return passed, f"actual={_fmt_dec(actual, 2)} vs prior_year={_fmt_dec(prior_year, 2)}"
    return True, "unsupported_operator"


def _resolve_effective_basis(raw_basis: BasisType, call_default_basis: CallDefaultBasis, metric_canonical: str) -> Literal["gaap", "non_gaap", "operating_kpi", "unknown"]:
    if raw_basis == "operating_kpi":
        return "operating_kpi"
    if raw_basis == "gaap":
        return "gaap"
    if raw_basis == "non_gaap":
        return "non_gaap"
    if metric_canonical in _GAAP_INVARIANT_METRICS:
        return "gaap"
    if call_default_basis == "non_gaap":
        return "non_gaap"
    if call_default_basis == "gaap":
        return "gaap"
    return "unknown"


def _map_linked_pillar(metric_canonical: str, text_hint: str, thesis_pillars: Sequence[Mapping[str, Any]] = ()) -> str | None:
    if metric_canonical in _METRIC_TO_PILLAR:
        return _METRIC_TO_PILLAR[metric_canonical]
    low = (text_hint or "").casefold()
    for p in thesis_pillars:
        pid = str(p.get("pillar") or p.get("pillar_key") or p.get("title") or "").strip()
        kws = [str(k).casefold() for k in (p.get("keywords") or []) if str(k).strip()]
        if pid and kws and any(kw in low for kw in kws):
            return pid
    if any(w in low for w in ("margin", "gross", "opex", "cost", "pricing", "毛利", "利润率")):
        return "margin"
    if any(w in low for w in ("cash", "fcf", "working capital", "receivable", "inventory", "现金流", "应收", "库存")):
        return "cash_quality"
    if any(w in low for w in ("capex", "buyback", "repurchase", "dividend", "capital", "资本开支", "回购")):
        return "capital_allocation"
    if any(w in low for w in ("revenue", "demand", "growth", "rpo", "backlog", "arr", "营收", "需求", "订单")):
        return "growth"
    if any(w in low for w in ("risk", "tariff", "export", "litigation", "regulatory", "风险", "关税", "管制")):
        return "risk"
    return None


def _commitment_stable_id(entity_key: str, metric: str, kpi_key: str | None, target_key: str | None, text: str) -> str:
    seed = f"{entity_key}|{metric}|{kpi_key or ''}|{target_key or ''}|{text[:100].casefold()}"
    return f"cmt_{sha256(seed.encode('utf-8')).hexdigest()[:16]}"


# --------------------------------------------------------------------------- Layer 3: Master 7-Track Collision Engine

def run_layer3_call_collision(
    *,
    entity_key: str,
    parsed_docs: Sequence[ParsedCallDocument],
    ledger: Sec10QCollisionLedger,
    snap: PeerCompanySnapshot | None,
    mgmt_extraction: Layer2ManagementExtraction | None,
    sellside_extractions: Sequence[Layer2SellsideExtraction] = (),
    source_texts: Mapping[str, str],
    document_pages_by_source: Mapping[str, Mapping[str, Any]] | None = None,
    source_permissions: Mapping[str, Mapping[str, Any]] | None = None,
    existing_commitment_ledger: AccountVerbalCommitmentLedger | None = None,
    v25_credibility_records: Sequence[Mapping[str, Any]] = (),
    thesis_pillars: Sequence[Mapping[str, Any]] = (),
    extra_benchmark_providers: Sequence[ExpectationBenchmarkProvider] = (),
    as_of: date | None = None,
    rules: CompiledV3aRules | None = None,
    owner_id: str | None = None,
) -> tuple[CallVerificationReport, AccountVerbalCommitmentLedger]:
    """Execute 0-LLM (<10ms) Layer 3 7-track collision, evasion audit, commitment grading & Silent Drop."""
    compiled = rules or load_v3a_call_rules()
    th = compiled.rules.ledger_collision_thresholds
    ev_cfg = compiled.rules.evasion_scoring

    parsed_by_src = {d.source_id: d for d in parsed_docs}
    mgmt_docs = [d for d in parsed_docs if d.classification.authority_tier == "management_first_party"]
    primary_doc = next((d for d in mgmt_docs if d.classification.archetype == "transcript"), mgmt_docs[0] if mgmt_docs else (parsed_docs[0] if parsed_docs else None))

    call_default_basis: CallDefaultBasis = "unspecified"
    for d in mgmt_docs:
        if d.call_default_basis != "unspecified":
            call_default_basis = d.call_default_basis
            break

    fiscal_period_key = ledger.fiscal_period_key or (
        _fiscal_key(primary_doc.call_fiscal_year, primary_doc.call_fiscal_quarter) if primary_doc else None
    )
    call_date_iso = ledger.call_date or (primary_doc.call_date_iso if primary_doc else None)
    call_d = _parse_iso_date(call_date_iso)
    init_10q_filed = _parse_iso_date(ledger.initial_10q_filed_at)
    p1_10q_filed = _parse_iso_date(ledger.p1_10q_filed_at)

    skeptic_map: dict[str, str] = {}
    if mgmt_extraction is not None:
        for sk in mgmt_extraction.skeptic_notes:
            skeptic_map[sk.target_ref.strip().casefold()] = sk.note

    findings: list[CollisionFinding] = []
    verified_quotes = 0
    total_quotes = 0

    def _track_quote(q: GroundedQuoteRef) -> GroundedQuoteRef:
        nonlocal verified_quotes, total_quotes
        total_quotes += 1
        if q.verified:
            verified_quotes += 1
        return q

    def _fid(prefix: str, seed: str) -> str:
        return f"{prefix}_{sha256(seed.encode('utf-8')).hexdigest()[:12]}"

    # ------------------------------------------------------------------ Track 0: Internal Contradictions & CEO/CFO Divergence
    if mgmt_extraction is not None:
        for idx, ic in enumerate(mgmt_extraction.internal_contradictions):
            qa = _track_quote(
                ground_v3a_quote(
                    quotation=ic.quote_a,
                    source_id=ic.source_a_id,
                    source_texts=source_texts,
                    document_pages_by_source=document_pages_by_source,
                    parsed_docs_by_source=parsed_by_src,
                    rules=compiled,
                    turn_index=ic.turn_a_index,
                    slide_index=ic.slide_a_index,
                )
            )
            qb = _track_quote(
                ground_v3a_quote(
                    quotation=ic.quote_b,
                    source_id=ic.source_b_id,
                    source_texts=source_texts,
                    document_pages_by_source=document_pages_by_source,
                    parsed_docs_by_source=parsed_by_src,
                    rules=compiled,
                    turn_index=ic.turn_b_index,
                    slide_index=ic.slide_b_index,
                )
            )
            both_verified = qa.verified and qb.verified
            is_ceo_cfo = {ic.speaker_a_role, ic.speaker_b_role} == {"ceo", "cfo"}
            if ic.evidence_sufficiency != "explicit" or not both_verified:
                sev: CollisionSeverity = "info"
                ckind: Any = "observation_only_insufficient_evidence"
                rank = 80
            elif is_ceo_cfo:
                sev = "red"
                ckind = "ceo_cfo_divergence"
                rank = 5  # Pinned above standard internal walkbacks
            else:
                sev = "yellow"
                ckind = "internal_walkback"
                rank = 18

            findings.append(
                CollisionFinding(
                    finding_id=_fid("t0", f"{idx}:{ic.topic}:{ic.quote_a[:40]}"),
                    track="track_0_internal",
                    collision_kind=ckind,
                    severity=sev,
                    title=(f"CEO vs CFO 跨角色口径分歧 · {ic.topic}" if is_ceo_cfo else f"同场口径退缩 · {ic.topic}"),
                    management_statement=f"{ic.speaker_a_name or ic.speaker_a_role}: {ic.quote_a[:200]}",
                    ledger_evidence=f"{ic.speaker_b_name or ic.speaker_b_role}: {ic.quote_b[:200]}",
                    formula_note=ic.contradiction_summary,
                    basis_badge="同场双原话对照",
                    period_badge=fiscal_period_key,
                    linked_pillar=_map_linked_pillar("other", ic.topic, thesis_pillars),
                    skeptic_note=skeptic_map.get(ic.topic.casefold()),
                    quotes=[qa, qb],
                    priority_rank=rank,
                )
            )

    # ------------------------------------------------------------------ Tracks 1 / 2 / 3: Claims vs 10-Q Initial-Basis Ledger
    rel_tol = Decimal(str(th.numeric_tolerance_rel_pct))
    margin_tol_pct = Decimal(str(th.margin_tolerance_bps)) / Decimal(100)

    if ledger.is_financial_gated:
        findings.append(
            CollisionFinding(
                finding_id=_fid("t1", f"sic_gate:{entity_key}"),
                track="track_1_demand_channel",
                collision_kind="not_applicable",
                severity="not_applicable",
                title="金融行业 SIC 豁免 · AR/库存/DSO/DIO 对撞不适用",
                management_statement=f"SIC {ledger.sic or 'N/A'} ({ledger.sector_category})",
                ledger_evidence="银行/保险/REITs 无传统库存与应收渠道周转口径，轨道 1 自动豁免。",
                priority_rank=95,
            )
        )

    if mgmt_extraction is not None:
        deduped_claims = _dedup_factual_claims(mgmt_extraction.factual_claims)

        # Deterministic Cross-Lane / Same-Call Numeric Contradiction Check (Prepared Remarks vs Q&A / CEO vs CFO)
        _seen_numeric_by_slot: dict[tuple[Any, ...], tuple[ManagementFactualClaim, GroundedQuoteRef, Decimal]] = {}
        for idx_c, cl_c in enumerate(deduped_claims):
            if cl_c.evidence_sufficiency != "explicit" or cl_c.value_low is None or cl_c.operator not in ("approx", "ge", "le", "between"):
                continue
            q_c = ground_v3a_quote(
                quotation=cl_c.verbatim_quote,
                source_id=cl_c.source_id,
                source_texts=source_texts,
                document_pages_by_source=document_pages_by_source,
                parsed_docs_by_source=parsed_by_src,
                rules=compiled,
                turn_index=cl_c.turn_index,
                slide_index=cl_c.slide_index,
            )
            if not q_c.verified:
                continue
            scaled_v = _scale_claim_value(_dec(cl_c.value_low), cl_c.unit, cl_c.scale, cl_c.metric_canonical)
            if scaled_v is None or scaled_v == 0:
                continue
            eff_b_c = _resolve_effective_basis(cl_c.basis, call_default_basis, cl_c.metric_canonical)
            is_pct_dim = _is_pct_or_bps_unit(cl_c.unit, cl_c.scale) and cl_c.metric_canonical not in _MARGIN_METRICS
            slot_k = (cl_c.metric_canonical, cl_c.scope, cl_c.target_fy, cl_c.target_fq, cl_c.period_scope, eff_b_c, is_pct_dim)
            prev_entry = _seen_numeric_by_slot.get(slot_k)
            if prev_entry is None:
                _seen_numeric_by_slot[slot_k] = (cl_c, q_c, scaled_v)
            else:
                p_cl, p_q, p_val = prev_entry
                if p_cl.verbatim_quote != cl_c.verbatim_quote and (p_cl.turn_index != cl_c.turn_index or p_cl.speaker_role != cl_c.speaker_role):
                    if cl_c.metric_canonical in _MARGIN_METRICS:
                        diverged = abs(scaled_v - p_val) >= Decimal("1.5")
                    else:
                        diverged = (abs(scaled_v - p_val) / max(abs(p_val), abs(scaled_v))) >= Decimal("0.05")
                    if diverged:
                        roles_pair = {p_cl.speaker_role, cl_c.speaker_role}
                        is_ceo_cfo_x = ("ceo" in roles_pair and "cfo" in roles_pair)
                        findings.append(
                            CollisionFinding(
                                finding_id=_fid("t0x", f"{idx_c}:{cl_c.metric_canonical}:{cl_c.scope}"),
                                track="track_0_internal",
                                collision_kind="ceo_cfo_divergence" if is_ceo_cfo_x else "internal_walkback",
                                severity="red" if is_ceo_cfo_x else "yellow",
                                title=(
                                    f"跨段/跨角色数值分歧 · {cl_c.metric_canonical} ({cl_c.scope})"
                                    if is_ceo_cfo_x
                                    else f"同场前后数值回撤 · {cl_c.metric_canonical} ({cl_c.scope})"
                                ),
                                management_statement=f"{p_cl.speaker_name or p_cl.speaker_role}: {p_cl.claim_summary}",
                                ledger_evidence=f"{cl_c.speaker_name or cl_c.speaker_role}: {cl_c.claim_summary}",
                                formula_note=f"前值 {_fmt_dec(p_val, 2)} vs 后值 {_fmt_dec(scaled_v, 2)}（同口径 {eff_b_c} / {cl_c.scope}）",
                                basis_badge="同场跨段确定性核对",
                                period_badge=_fiscal_key(cl_c.target_fy, cl_c.target_fq) or fiscal_period_key,
                                linked_pillar=_map_linked_pillar(cl_c.metric_canonical, cl_c.claim_summary, thesis_pillars),
                                quotes=[p_q, q_c],
                                priority_rank=6 if is_ceo_cfo_x else 17,
                            )
                        )

        for idx, claim in enumerate(deduped_claims):
            qref = _track_quote(
                ground_v3a_quote(
                    quotation=claim.verbatim_quote,
                    source_id=claim.source_id,
                    source_texts=source_texts,
                    document_pages_by_source=document_pages_by_source,
                    parsed_docs_by_source=parsed_by_src,
                    rules=compiled,
                    turn_index=claim.turn_index,
                    slide_index=claim.slide_index,
                )
            )
            eff_basis = _resolve_effective_basis(claim.basis, call_default_basis, claim.metric_canonical)
            pillar = _map_linked_pillar(claim.metric_canonical, claim.claim_summary, thesis_pillars)
            track_name: Any = {
                "demand_channel": "track_1_demand_channel",
                "earnings_cash": "track_2_earnings_cash",
                "capex_supply": "track_3_capex_supply",
                "capital_allocation": "track_4_capital_allocation",
            }.get(claim.track_hint, "track_2_earnings_cash")

            # Conscious Incompetence gate: non-explicit or unverified quote never triggers red/yellow
            if claim.evidence_sufficiency != "explicit" or not qref.verified:
                findings.append(
                    CollisionFinding(
                        finding_id=_fid("ci", f"{idx}:{claim.metric_canonical}:{claim.verbatim_quote[:30]}"),
                        track=track_name,
                        collision_kind="observation_only_insufficient_evidence",
                        severity="info",
                        title=f"观察线索（证据不足/未显式量化）· {claim.claim_summary[:60]}",
                        management_statement=claim.claim_summary,
                        ledger_evidence=(
                            "管理层表述偏模糊或定性（Conscious Incompetence 门控降级，不触发硬打假）。"
                            if qref.verified
                            else "引文未能通过服务端原文验真，降级为观察线索。"
                        ),
                        basis_badge=eff_basis,
                        period_badge=_fiscal_key(claim.target_fy, claim.target_fq) or fiscal_period_key,
                        linked_pillar=pillar,
                        quotes=[qref],
                        priority_rank=85,
                    )
                )
                continue

            # Non-SEC or awaiting current 10-Q
            if ledger.status == "non_sec_entity":
                continue
            if ledger.status == "awaiting_current_10q_using_8k_and_prior_10q":
                findings.append(
                    CollisionFinding(
                        finding_id=_fid("aw", f"{idx}:{claim.metric_canonical}:{claim.verbatim_quote[:30]}"),
                        track=track_name,
                        collision_kind="awaiting_10q",
                        severity="info",
                        title=f"待当季 10-Q 自动对撞 · {claim.claim_summary[:60]}",
                        management_statement=claim.claim_summary,
                        ledger_evidence="当季 10-Q 尚未归档，已自动挂载雷达 Phase 2c 监控，10-Q 落地后自动重算。",
                        basis_badge=eff_basis,
                        period_badge=fiscal_period_key,
                        linked_pillar=pillar,
                        quotes=[qref],
                        priority_rank=70,
                    )
                )
                continue

            # Track 1 qualitative demand/channel claim vs 10-Q AR/Inv/RPO divergences
            if claim.track_hint == "demand_channel" and claim.qualitative_stance == "bullish_healthy" and not ledger.is_financial_gated:
                ar_gap = _dec(ledger.ar_minus_rev_pp)
                inv_gap = _dec(ledger.inv_minus_cogs_pp)
                rpo_gap = _dec(ledger.rpo_minus_rev_pp)
                if inv_gap is not None and inv_gap >= Decimal(str(th.inv_minus_cogs_pp_yellow)):
                    sev_t1: CollisionSeverity = "red" if inv_gap >= Decimal(str(th.inv_minus_cogs_pp_red)) else "yellow"
                    findings.append(
                        CollisionFinding(
                            finding_id=_fid("t1inv", f"{idx}:{claim.verbatim_quote[:30]}"),
                            track="track_1_demand_channel",
                            collision_kind="direct_contradiction",
                            severity=sev_t1,
                            title=f"库存增速超 COGS +{ledger.inv_minus_cogs_pp}pp，与管理层渠道健康表述背离",
                            management_statement=claim.claim_summary,
                            ledger_evidence=f"10-Q 库存 YoY {ledger.inv_yoy_pct}% vs COGS YoY {ledger.cogs_yoy_pct}%（DIO {ledger.dio_days or 'N/A'} 天，YoY {ledger.dio_yoy_days or 'N/A'} 天）",
                            formula_note="Inv YoY - COGS YoY >= threshold",
                            basis_badge="GAAP 10-Q 首发口径",
                            period_badge=fiscal_period_key,
                            linked_pillar="cash_quality",
                            skeptic_note=skeptic_map.get("inventory") or "可能系主动战略备货迎接新品爬坡，需结合下季去化速度验证。",
                            quotes=[qref],
                            priority_rank=10 if sev_t1 == "red" else 28,
                        )
                    )
                    continue
                if ar_gap is not None and ar_gap >= Decimal(str(th.ar_minus_rev_pp_yellow)):
                    sev_t1 = "red" if ar_gap >= Decimal(str(th.ar_minus_rev_pp_red)) else "yellow"
                    findings.append(
                        CollisionFinding(
                            finding_id=_fid("t1ar", f"{idx}:{claim.verbatim_quote[:30]}"),
                            track="track_1_demand_channel",
                            collision_kind="direct_contradiction",
                            severity=sev_t1,
                            title=f"应收账款增速超营收 +{ledger.ar_minus_rev_pp}pp，与强劲需求表述存在背离",
                            management_statement=claim.claim_summary,
                            ledger_evidence=f"10-Q AR YoY {ledger.ar_yoy_pct}% vs Revenue YoY {ledger.revenue_yoy_pct}%（DSO {ledger.dso_days or 'N/A'} 天）",
                            formula_note="AR YoY - Revenue YoY >= threshold",
                            basis_badge="GAAP 10-Q 首发口径",
                            period_badge=fiscal_period_key,
                            linked_pillar="cash_quality",
                            skeptic_note=skeptic_map.get("receivables") or "可能系季末集中出货或大客户账期结构变化所致。",
                            quotes=[qref],
                            priority_rank=12 if sev_t1 == "red" else 30,
                        )
                    )
                    continue
                if rpo_gap is not None and rpo_gap <= Decimal(str(th.rpo_vs_rev_pp_yellow)):
                    sev_t1 = "red" if rpo_gap <= Decimal(str(th.rpo_vs_rev_pp_red)) else "yellow"
                    findings.append(
                        CollisionFinding(
                            finding_id=_fid("t1rpo", f"{idx}:{claim.verbatim_quote[:30]}"),
                            track="track_1_demand_channel",
                            collision_kind="direct_contradiction",
                            severity=sev_t1,
                            title=f"RPO/递延收入增速落后营收 {ledger.rpo_minus_rev_pp}pp",
                            management_statement=claim.claim_summary,
                            ledger_evidence=f"10-Q RPO/递延收入 YoY {ledger.rpo_or_defrev_yoy_pct}% vs Revenue YoY {ledger.revenue_yoy_pct}%",
                            basis_badge="GAAP 10-Q 首发口径",
                            period_badge=fiscal_period_key,
                            linked_pillar="growth",
                            quotes=[qref],
                            priority_rank=15 if sev_t1 == "red" else 32,
                        )
                    )
                    continue

            # R3-D6: Segment/Product/Geography figures MUST NOT collide against consolidated XBRL total lines!
            if claim.scope != "consolidated" and claim.metric_canonical in TWELVE_XBRL_METRICS:
                val_desc = ""
                if claim.value_low is not None:
                    val_desc = f"{claim.value_low}{f'-{claim.value_high}' if claim.value_high else ''} {claim.scale or ''}{claim.unit or ''}".strip()
                findings.append(
                    CollisionFinding(
                        finding_id=_fid("seg", f"{idx}:{claim.metric_canonical}:{claim.kpi_key or claim.scope}"),
                        track=track_name,
                        collision_kind="observation_only_insufficient_evidence",
                        severity="info",
                        title=f"分部/产品线口径登记（不误撞合并报表）· {claim.kpi_key or claim.metric_canonical}" + (f" ({val_desc})" if val_desc else ""),
                        management_statement=claim.claim_summary,
                        ledger_evidence=(
                            f"已识别为 {claim.scope} 口径（非合并总口径），按 R3-D6 隔离防误撞合并 XBRL；"
                            + (
                                f"已拉取 10-Q 附注表供对照（{'; '.join(ledger.footnote_fallbacks[:2])}）。"
                                if ledger.footnote_fallbacks
                                else "可对照 10-Q 分部附注或跨季电话会 KPI 追踪。"
                            )
                        ),
                        basis_badge=f"{claim.scope} 口径隔离",
                        period_badge=_fiscal_key(claim.target_fy, claim.target_fq) or fiscal_period_key,
                        linked_pillar=pillar,
                        quotes=[qref],
                        priority_rank=80,
                    )
                )
                continue

            # Quantitative claim collision via stateless xbrl_actuals_lookup (12 metrics, consolidated only)
            if claim.metric_canonical in TWELVE_XBRL_METRICS and (claim.value_low is not None or claim.value_high is not None):
                t_fy = claim.target_fy or ledger.target_fiscal_year
                t_fq = claim.target_fq if claim.period_scope != "full_year" else None
                if t_fq is None and claim.period_scope in ("single_quarter", "unspecified"):
                    t_fq = ledger.target_fiscal_quarter
                t_end = _parse_iso_date(ledger.period_end_date) if (t_fy == ledger.target_fiscal_year and t_fq == ledger.target_fiscal_quarter) else None

                xres = xbrl_actuals_lookup(
                    snap,
                    claim.metric_canonical,
                    target_fy=t_fy,
                    target_fq=t_fq,
                    target_end=t_end,
                    basis_lock=claim.basis,
                    call_default_basis=call_default_basis,
                    period_scope=claim.period_scope,
                    claim_currency=claim.currency,
                    initial_filing_cutoff=init_10q_filed,
                    as_of=as_of,
                    restatement_notice_pct=th.restatement_notice_pct,
                )
                rest_note = (
                    f"首发 10-Q 口径为 {xres.initial_basis_value}；后续归档重述/拆股调整为 {xres.latest_value}（变动 {xres.restatement_delta_pct}%），本对撞严格使用首发口径。"
                    if xres.restated_later
                    else None
                )

                if xres.status == "period_scope_mismatch":
                    findings.append(
                        CollisionFinding(
                            finding_id=_fid("ps", f"{idx}:{claim.metric_canonical}"),
                            track=track_name,
                            collision_kind="period_scope_mismatch",
                            severity="info",
                            title=f"期间口径差异披露 · {claim.metric_canonical} ({claim.period_scope})",
                            management_statement=claim.claim_summary,
                            ledger_evidence=xres.note or "期间口径与单季 10-Q 不一致，不作硬对撞。",
                            basis_badge=eff_basis,
                            period_badge=claim.period_scope,
                            linked_pillar=pillar,
                            quotes=[qref],
                            priority_rank=72,
                        )
                    )
                    continue

                if xres.status == "currency_mismatch":
                    findings.append(
                        CollisionFinding(
                            finding_id=_fid("cur", f"{idx}:{claim.metric_canonical}"),
                            track=track_name,
                            collision_kind="currency_mismatch",
                            severity="info",
                            title=f"币种口径差异 · {claim.metric_canonical}",
                            management_statement=claim.claim_summary,
                            ledger_evidence=xres.note or "声明币种与 10-Q 报告币种不同，跳过跨币种算术对撞。",
                            basis_badge=eff_basis,
                            period_badge=xres.fiscal_period_key,
                            linked_pillar=pillar,
                            quotes=[qref],
                            priority_rank=74,
                        )
                    )
                    continue

                if xres.status == "nongaap_locked":
                    # D2: NEVER hard-collide Non-GAAP against GAAP; provide deterministic FCF baseline or SBC/D&A bridge!
                    gaap_val = _dec(xres.value)
                    claim_lo = _scale_claim_value(_dec(claim.value_low), claim.unit, claim.scale, claim.metric_canonical)
                    claim_hi = _scale_claim_value(_dec(claim.value_high), claim.unit, claim.scale, claim.metric_canonical)

                    # Special FCF baseline (OCF - CapEx): never red contradiction; green match or yellow definition-gap note
                    if claim.metric_canonical == "free_cash_flow" and gaap_val is not None and claim_lo is not None:
                        passed_fcf, fcf_detail = _evaluate_operator(
                            operator=claim.operator,
                            actual=gaap_val,
                            prior_year=_dec(xres.prior_year_value),
                            low=claim_lo,
                            high=claim_hi,
                            rel_tol_pct=rel_tol,
                            abs_tol=Decimal("0"),
                        )
                        findings.append(
                            CollisionFinding(
                                finding_id=_fid("fcf", f"{idx}:{claim.metric_canonical}"),
                                track="track_2_earnings_cash",
                                collision_kind="corroborated" if passed_fcf else "basis_divergence_nongaap_gap",
                                severity="green" if passed_fcf else "yellow",
                                title=(
                                    "FCF 标准口径印证 (10-Q OCF - CapEx)"
                                    if passed_fcf
                                    else "FCF 定义差异提示 (管理层口径 vs 10-Q OCF - CapEx)"
                                ),
                                management_statement=claim.claim_summary,
                                ledger_evidence=f"10-Q 标准基线 (OCF - CapEx) = {xres.value} {xres.unit or ''}（{xres.fiscal_period_key or ''}）",
                                formula_note=f"标准 FCF = OCF - CapEx ({fcf_detail})",
                                basis_badge="FCF 标准基线对照",
                                period_badge=xres.fiscal_period_key,
                                restated_later=xres.restated_later,
                                restatement_note=rest_note,
                                linked_pillar="cash_quality",
                                skeptic_note="自由现金流属非 GAAP 指标，管理层口径可能额外调整资本化软件、租赁或并购款项；仅作定义差异提示，不作硬冲突。",
                                quotes=[qref],
                                priority_rank=76 if passed_fcf else 35,
                            )
                        )
                        continue

                    gap_sev: CollisionSeverity = "info"
                    gap_title = f"Non-GAAP 口径锁（不误撞 GAAP）· {claim.metric_canonical}"
                    formula_str = f"管理层 Non-GAAP={claim.value_low}{claim.unit or ''} vs 10-Q GAAP={xres.value}{xres.unit or ''}"
                    if claim.metric_canonical in _MARGIN_METRICS and gaap_val is not None and claim_lo is not None:
                        gap_bps = abs(claim_lo - gaap_val) * Decimal(100)
                        if gap_bps >= Decimal(str(th.nongaap_margin_gap_bps_red)):
                            gap_sev = "yellow"  # Never red contradiction — GAAP/Non-GAAP gap is a warning
                            gap_title = f"Non-GAAP vs GAAP 调节差偏大 ({_fmt_dec(gap_bps, 0)} bps) · {claim.metric_canonical}"
                        elif gap_bps >= Decimal(str(th.nongaap_margin_gap_bps_yellow)):
                            gap_sev = "yellow"
                            gap_title = f"Non-GAAP vs GAAP 调节差提示 ({_fmt_dec(gap_bps, 0)} bps) · {claim.metric_canonical}"
                        formula_str = f"Non-GAAP {_fmt_dec(claim_lo, 2)}% vs GAAP {_fmt_dec(gaap_val, 2)}% (差值 {_fmt_dec(gap_bps, 0)} bps)"

                    # Deterministic GAAP -> Non-GAAP Bridge (GAAP OI + SBC + D&A + Restructuring + Impairment)
                    rev_pt = ledger.lines.get("revenue")
                    oi_pt = ledger.lines.get("operating_income")
                    rev_d = _dec(rev_pt.value) if rev_pt else None
                    oi_d = _dec(oi_pt.value) if oi_pt else None
                    sbc_d = abs(_dec(ledger.stock_based_compensation) or Decimal(0))
                    da_d = abs(_dec(ledger.depreciation_amortization) or Decimal(0))
                    rest_d = abs(_dec(ledger.restructuring_charges) or Decimal(0))
                    imp_d = abs(_dec(ledger.impairment_charges) or Decimal(0))
                    has_bridge_items = any(
                        v is not None
                        for v in (
                            ledger.stock_based_compensation,
                            ledger.depreciation_amortization,
                            ledger.restructuring_charges,
                            ledger.impairment_charges,
                        )
                    )
                    if (
                        claim.metric_canonical in ("operating_income", "operating_margin")
                        and has_bridge_items
                        and rev_d is not None
                        and rev_d > 0
                        and oi_d is not None
                        and claim_lo is not None
                    ):
                        known_addbacks = sbc_d + da_d + rest_d + imp_d
                        bridge_oi = oi_d + known_addbacks
                        bridge_om = (bridge_oi / rev_d) * Decimal(100)
                        if claim.metric_canonical == "operating_income":
                            unexpl = claim_lo - bridge_oi
                            unexpl_pct_rev = abs(unexpl) / rev_d * Decimal(100)
                            formula_str = (
                                f"确定性调节桥: GAAP OI ({_fmt_dec(oi_d, 0)}) + SBC ({_fmt_dec(sbc_d, 0)}) + D&A ({_fmt_dec(da_d, 0)})"
                                + (f" + 重组/减值 ({_fmt_dec(rest_d + imp_d, 0)})" if (rest_d + imp_d) > 0 else "")
                                + f" = {_fmt_dec(bridge_oi, 0)} vs 管理层 {_fmt_dec(claim_lo, 0)}（未解释调节差占营收 {_fmt_dec(unexpl_pct_rev, 2)}%）"
                            )
                            if unexpl_pct_rev > Decimal("3.0"):
                                gap_sev = "yellow"
                                gap_title = f"Non-GAAP 调节桥未解释项偏高 ({_fmt_dec(unexpl_pct_rev, 1)}% 营收) · operating_income"
                        else:
                            unexpl_bps = abs(claim_lo - bridge_om) * Decimal(100)
                            formula_str = (
                                f"确定性调节桥: GAAP OM {_fmt_dec(gaap_val, 2)}% + SBC/D&A/重组加回 → 桥接 OM {_fmt_dec(bridge_om, 2)}% "
                                f"vs 管理层 {_fmt_dec(claim_lo, 2)}%（未解释差 {_fmt_dec(unexpl_bps, 0)} bps）"
                            )
                            if unexpl_bps > Decimal("200"):
                                gap_sev = "yellow"
                                gap_title = f"Non-GAAP 利润率调节桥未解释项偏高 ({_fmt_dec(unexpl_bps, 0)} bps) · operating_margin"

                    if ledger.reg_g_reconciliation_excerpts:
                        formula_str = f"{formula_str} | 参考 8-K Ex-99.1 Reg-G 表已提取（不作自动红灯）"

                    findings.append(
                        CollisionFinding(
                            finding_id=_fid("ng", f"{idx}:{claim.metric_canonical}"),
                            track="track_2_earnings_cash",
                            collision_kind="basis_divergence_nongaap_gap",
                            severity=gap_sev,
                            title=gap_title,
                            management_statement=claim.claim_summary,
                            ledger_evidence=f"10-Q 首发 GAAP {claim.metric_canonical} = {xres.value} {xres.unit or ''}（{xres.fiscal_period_key or ''}）",
                            formula_note=formula_str,
                            basis_badge="Non-GAAP 口径锁" + ("（继承开场白）" if claim.basis in ("unknown", "inherited_call_default") else ""),
                            period_badge=xres.fiscal_period_key,
                            restated_later=xres.restated_later,
                            restatement_note=rest_note,
                            linked_pillar=pillar,
                            skeptic_note="Non-GAAP 通常剔除 SBC、无形资产摊销或一次性重组，调节差本身不等于财务造假；请核对 8-K Ex-99.1 Reg-G 调节表。",
                            quotes=[qref],
                            priority_rank=36 if gap_sev == "yellow" else 65,
                        )
                    )
                    continue

                if xres.status == "matched" and xres.value is not None:
                    actual_d = _dec(xres.value)
                    py_d = _dec(xres.prior_year_value)
                    dim_status, eff_op, dim_note = _check_dimension_compatibility(
                        metric_canonical=claim.metric_canonical,
                        operator=claim.operator,
                        unit=claim.unit,
                        scale=claim.scale,
                        text_blob=f"{claim.claim_summary} {claim.verbatim_quote}",
                    )
                    if dim_status == "mismatch":
                        findings.append(
                            CollisionFinding(
                                finding_id=_fid("dim", f"{idx}:{claim.metric_canonical}"),
                                track=track_name,
                                collision_kind="observation_only_insufficient_evidence",
                                severity="info",
                                title=f"量纲/口径不适用硬对撞 · {claim.metric_canonical}",
                                management_statement=claim.claim_summary,
                                ledger_evidence=dim_note or "管理层量纲与 10-Q 指标量纲不一致，跳过算术硬对撞。",
                                basis_badge="量纲守卫隔离",
                                period_badge=xres.fiscal_period_key,
                                linked_pillar=pillar,
                                quotes=[qref],
                                priority_rank=79,
                            )
                        )
                        continue
                    is_yoy_eval = eff_op in ("growth_yoy_ge", "growth_yoy_le", "growth_yoy_approx", "growth_yoy_between")
                    if is_yoy_eval:
                        lo_d = _dec(claim.value_low)
                        hi_d = _dec(claim.value_high)
                        if _is_bps_unit(claim.unit, claim.scale):
                            lo_d = (lo_d / Decimal("100")) if lo_d is not None else None
                            hi_d = (hi_d / Decimal("100")) if hi_d is not None else None
                    else:
                        lo_d = _scale_claim_value(_dec(claim.value_low), claim.unit, claim.scale, claim.metric_canonical)
                        hi_d = _scale_claim_value(_dec(claim.value_high), claim.unit, claim.scale, claim.metric_canonical)
                    if actual_d is not None:
                        round_tol = (
                            _rounding_half_step_tolerance(claim.value_low, "%", None, "gross_margin")
                            if is_yoy_eval
                            else _rounding_half_step_tolerance(claim.value_low, claim.unit, claim.scale, claim.metric_canonical)
                        )
                        abs_tol = max(margin_tol_pct if claim.metric_canonical in _MARGIN_METRICS else Decimal("0"), round_tol)
                        passed, formula_detail = _evaluate_operator(
                            operator=eff_op,
                            actual=actual_d,
                            prior_year=py_d,
                            low=lo_d,
                            high=hi_d,
                            rel_tol_pct=rel_tol,
                            abs_tol=abs_tol,
                            is_margin_metric=(claim.metric_canonical in _MARGIN_METRICS),
                        )
                        if passed:
                            findings.append(
                                CollisionFinding(
                                    finding_id=_fid("ok", f"{idx}:{claim.metric_canonical}"),
                                    track=track_name,
                                    collision_kind="corroborated",
                                    severity="green",
                                    title=f"10-Q 首发口径印证 · {claim.metric_canonical}",
                                    management_statement=claim.claim_summary,
                                    ledger_evidence=f"10-Q 首发实数 {xres.value} {xres.unit or ''}（{xres.fiscal_period_key or ''}）",
                                    formula_note=formula_detail,
                                    basis_badge="GAAP 10-Q 首发口径" + ("（同比增速核验）" if is_yoy_eval else ""),
                                    period_badge=xres.fiscal_period_key,
                                    restated_later=xres.restated_later,
                                    restatement_note=rest_note,
                                    linked_pillar=pillar,
                                    quotes=[qref],
                                    priority_rank=78,
                                )
                            )
                        else:
                            # Deterministic Broad CapEx (Finance Lease) Bridge: cash PP&E + finance lease principal / ROU additions
                            capex_bridge_note: str | None = None
                            if (
                                claim.metric_canonical == "capex"
                                and not is_yoy_eval
                                and lo_d is not None
                                and lo_d > actual_d
                            ):
                                fl_princ = _dec(ledger.finance_lease_principal_payments)
                                fl_rou = _dec(ledger.finance_lease_rou_additions)
                                for fl_lbl, fl_val in (
                                    ("融资租赁本金偿付", fl_princ),
                                    ("融资租赁使用权资产新增", fl_rou),
                                ):
                                    if fl_val is not None and fl_val > 0:
                                        broad_actual = actual_d + fl_val
                                        b_ok, b_detail = _evaluate_operator(
                                            operator=eff_op,
                                            actual=broad_actual,
                                            prior_year=None,
                                            low=lo_d,
                                            high=hi_d,
                                            rel_tol_pct=rel_tol,
                                            abs_tol=abs_tol,
                                        )
                                        if b_ok:
                                            capex_bridge_note = (
                                                f"管理层广义 CapEx 口径吻合：10-Q 现金购建 PP&E ({_fmt_dec(actual_d, 0)}) "
                                                f"+ {fl_lbl} ({_fmt_dec(fl_val, 0)}) = {_fmt_dec(broad_actual, 0)} {xres.unit or ''}（{b_detail}）"
                                            )
                                            break
                            if capex_bridge_note:
                                findings.append(
                                    CollisionFinding(
                                        finding_id=_fid("capfl", f"{idx}:capex"),
                                        track="track_3_capex_supply",
                                        collision_kind="basis_divergence_nongaap_gap",
                                        severity="info",
                                        title="广义 CapEx 口径调节吻合（含融资租赁） · capex",
                                        management_statement=claim.claim_summary,
                                        ledger_evidence=capex_bridge_note,
                                        formula_note="广义 CapEx = 现金购建固定资产 + 融资租赁本金偿付/新增 ROU",
                                        basis_badge="CapEx 融资租赁调节桥",
                                        period_badge=xres.fiscal_period_key,
                                        restated_later=xres.restated_later,
                                        restatement_note=rest_note,
                                        linked_pillar=pillar,
                                        quotes=[qref],
                                        priority_rank=64,
                                    )
                                )
                                continue
                            # If surrounding context had a qualifying/negation clause, soften red -> yellow
                            sev_m: CollisionSeverity = "yellow" if qref.quote_context_warning else "red"
                            findings.append(
                                CollisionFinding(
                                    finding_id=_fid("mis", f"{idx}:{claim.metric_canonical}"),
                                    track=track_name,
                                    collision_kind="direct_contradiction",
                                    severity=sev_m,
                                    title=f"口头数据与 10-Q 首发口径矛盾 · {claim.metric_canonical}",
                                    management_statement=claim.claim_summary,
                                    ledger_evidence=f"10-Q 首发实数 {xres.value} {xres.unit or ''}（{xres.fiscal_period_key or ''}，accn {xres.accession or 'N/A'}）",
                                    formula_note=formula_detail,
                                    basis_badge="GAAP 10-Q 首发口径",
                                    period_badge=xres.fiscal_period_key,
                                    restated_later=xres.restated_later,
                                    restatement_note=rest_note,
                                    linked_pillar=pillar,
                                    skeptic_note=qref.quote_context_warning or skeptic_map.get(claim.metric_canonical.casefold()),
                                    quotes=[qref],
                                    priority_rank=8 if sev_m == "red" else 26,
                                )
                            )
                continue

            # Verified Operating KPI / ARR / NRR claims (metric_canonical == "other"):
            # 1) Cross-check ARR/NRR/Billings against strict QoQ Calculated Billings YoY if available
            # 2) Surface verified Operating KPIs explicitly in Tab 1 rather than silently dropping them
            kpi_text_blob = f"{claim.kpi_key or ''} {claim.claim_summary}".casefold()
            is_arr_nrr = bool(re.search(r"\b(?:arr|nrr|net\s+retention|net\s+dollar\s+retention|billings)\b", kpi_text_blob))
            bill_yoy_d = _dec(ledger.calculated_billings_yoy_pct)
            if is_arr_nrr and bill_yoy_d is not None:
                claim_growth = _dec(claim.value_low) if (claim.unit == "%" or claim.operator in ("growth_yoy_ge", "growth_yoy_le")) else None
                divergent = claim_growth is not None and (claim_growth - bill_yoy_d) >= Decimal("15")
                findings.append(
                    CollisionFinding(
                        finding_id=_fid("bill", f"{idx}:{claim.kpi_key or 'arr'}"),
                        track="track_1_demand_channel",
                        collision_kind="basis_divergence_nongaap_gap" if divergent else "observation_only_insufficient_evidence",
                        severity="yellow" if divergent else "info",
                        title=(
                            f"ARR/NRR 增速与 Calculated Billings YoY 背离 ({_fmt_dec(claim_growth, 1)}% vs {ledger.calculated_billings_yoy_pct}%)"
                            if divergent
                            else f"SaaS Calculated Billings 交叉核验参考 · {claim.kpi_key or 'ARR/Billings'}"
                        ),
                        management_statement=claim.claim_summary,
                        ledger_evidence=(
                            f"10-Q 严格环比 Calculated Billings = {ledger.calculated_billings} {ledger.currency}，"
                            f"YoY {ledger.calculated_billings_yoy_pct}%（公式：Revenue_t + [TotalDR_t - TotalDR_{{t-1}}]）"
                        ),
                        formula_note="Calculated Billings = Revenue(Q_t) + [DeferredRev(Q_t) - DeferredRev(Q_{t-1})]",
                        basis_badge="SaaS 账本交叉校验",
                        period_badge=fiscal_period_key,
                        linked_pillar="growth",
                        skeptic_note="Calculated Billings 受多年度合同预付周期、并购递延收入重估及汇率波动影响，与 ARR/NRR 的差值需结合合同期限解读。",
                        quotes=[qref],
                        priority_rank=33 if divergent else 79,
                    )
                )
            elif claim.kpi_key:
                val_disp = (
                    f"{claim.value_low}{('–' + claim.value_high) if claim.value_high else ''}"
                    f"{' ' + claim.scale if claim.scale else ''}{claim.unit or ''}"
                    if claim.value_low is not None
                    else "定性/里程碑"
                )
                findings.append(
                    CollisionFinding(
                        finding_id=_fid("kpi", f"{idx}:{claim.kpi_key}"),
                        track=track_name,
                        collision_kind="observation_only_insufficient_evidence",
                        severity="info",
                        title=f"运营 KPI 登记与跟踪 · {claim.kpi_key} ({val_disp})",
                        management_statement=claim.claim_summary,
                        ledger_evidence="属管理层自报运营 KPI（无对应 XBRL 强制披露项），已入账用于跨季一致性与 Silent Drop 追踪。",
                        basis_badge="operating_kpi",
                        period_badge=_fiscal_key(claim.target_fy, claim.target_fq) or fiscal_period_key,
                        linked_pillar=pillar,
                        quotes=[qref],
                        priority_rank=82,
                    )
                )

    # Standalone Layer 1 deterministic red/yellow diagnostics (even when Layer 2 is absent or didn't pick a claim)
    if ledger.status in ("same_period_10q_matched",):
        ocf_ni = _dec(ledger.ocf_to_ni_ratio)
        if ocf_ni is not None and ocf_ni < Decimal(str(th.ocf_to_ni_yellow)):
            sev_c: CollisionSeverity = "red" if ocf_ni < Decimal(str(th.ocf_to_ni_red)) else "yellow"
            findings.append(
                CollisionFinding(
                    finding_id=_fid("t2ocf", f"{entity_key}:{fiscal_period_key}"),
                    track="track_2_earnings_cash",
                    collision_kind="direct_contradiction",
                    severity=sev_c,
                    title=f"现金转化偏弱 · OCF/NI = {ledger.ocf_to_ni_ratio}x",
                    management_statement="10-Q 盈利质量基线自动核验",
                    ledger_evidence=f"OCF/NI = {ledger.ocf_to_ni_ratio}x，FCF/NI = {ledger.fcf_to_ni_ratio or 'N/A'}x",
                    formula_note=f"OCF/NI < {th.ocf_to_ni_yellow}x",
                    basis_badge="GAAP 10-Q 首发口径",
                    period_badge=fiscal_period_key,
                    linked_pillar="cash_quality",
                    priority_rank=24 if sev_c == "red" else 42,
                )
            )
        pcomm_yoy = _dec(ledger.purchase_commitments_yoy_pct)
        if pcomm_yoy is not None and pcomm_yoy >= Decimal(str(th.purchase_commitment_yoy_pct_yellow)):
            sev_pc: CollisionSeverity = "red" if pcomm_yoy >= Decimal(str(th.purchase_commitment_yoy_pct_red)) else "yellow"
            findings.append(
                CollisionFinding(
                    finding_id=_fid("t3pc", f"{entity_key}:{fiscal_period_key}"),
                    track="track_3_capex_supply",
                    collision_kind="direct_contradiction",
                    severity=sev_pc,
                    title=f"ASC 440 无条件采购承诺大幅扩张 (+{ledger.purchase_commitments_yoy_pct}% YoY)",
                    management_statement="10-Q 供应承诺表外杠杆基线核验",
                    ledger_evidence=f"无条件采购承诺 YoY +{ledger.purchase_commitments_yoy_pct}%，CapEx YoY {ledger.capex_yoy_pct or 'N/A'}%",
                    basis_badge="GAAP 10-Q ASC 440",
                    period_badge=fiscal_period_key,
                    linked_pillar="capital_allocation",
                    priority_rank=34,
                )
            )

    # ------------------------------------------------------------------ Track 4: V2.5 Capital Allocation Cross-Check (Read-Only)
    if ledger.capital_allocation_summary:
        ca = ledger.capital_allocation_summary
        ca_light = str(ca.get("overall_light") or ca.get("light") or "info").lower()
        ca_headline = str(ca.get("headline") or ca.get("summary") or "V2.5 资本配置审计已关联").strip()
        if ca_light in ("red", "yellow"):
            findings.append(
                CollisionFinding(
                    finding_id=_fid("t4ca", f"{entity_key}:{fiscal_period_key}"),
                    track="track_4_capital_allocation",
                    collision_kind="direct_contradiction",
                    severity="yellow" if ca_light == "yellow" else "red",
                    title="真实回购/SBC 稀释审计预警（直连 V2.5）",
                    management_statement="电话会资本回报与回购表述对照",
                    ledger_evidence=ca_headline,
                    basis_badge="V2.5 确定性台账（零额外拉取）",
                    period_badge=fiscal_period_key,
                    linked_pillar="capital_allocation",
                    priority_rank=38,
                )
            )

    # ------------------------------------------------------------------ Track 5: 10-Q Item 1A Dual-Factor Omission Screening
    mgmt_corpus = "\n".join(source_texts.get(d.source_id, "") for d in mgmt_docs)
    screened_redlines = screen_item1a_omissions(
        redline_snippets=ledger.redline_added_risks,
        call_corpus_text=mgmt_corpus,
        rules=compiled,
    )
    omission_candidates = [sn for sn in screened_redlines if sn.flagged_omission]
    for idx, sn in enumerate(omission_candidates[:6]):
        findings.append(
            CollisionFinding(
                finding_id=_fid("t5om", f"{idx}:{sn.paragraph_text[:60]}"),
                track="track_5_10q_omission",
                collision_kind="filing_10q_omission",
                severity="yellow",
                title=f"10-Q 新增风险未在电话会正面提及 · {', '.join(sn.risk_buckets)}",
                management_statement=f"电话会风险桶覆盖={'已提' if sn.bucket_covered_in_call else '未提'}，实体词干覆盖率={sn.call_coverage_ratio:.0%}",
                ledger_evidence=sn.paragraph_text[:360],
                formula_note="风险类别桶 + 词干实体覆盖双因子 0-LLM 初筛命中",
                basis_badge=f"10-Q Redline ({sn.section_key})",
                period_badge=fiscal_period_key,
                linked_pillar="risk",
                skeptic_note="10-Q 律师措辞可能系例行合规预防性披露，不等同于已发生重大经营冲击。",
                priority_rank=44,
            )
        )

    # ------------------------------------------------------------------ Tab 2: Q&A Evasion Audit + R3-D7 3-Tier Refusal Triage
    qa_findings: list[QaEvasionFinding] = []
    evasion_dist = {
        "direct": 0,
        "intermediate": 0,
        "fully_evasive": 0,
        "confidentiality_whitelisted": 0,
        "disclosure_policy_gap": 0,
    }
    conf_whitelist_count = 0
    disclosure_gap_count = 0
    evasion_ry_count = 0

    if mgmt_extraction is not None:
        for idx, ev in enumerate(mgmt_extraction.qa_evasions):
            qq = _track_quote(
                ground_v3a_quote(
                    quotation=ev.question_quote,
                    source_id=ev.source_id,
                    source_texts=source_texts,
                    document_pages_by_source=document_pages_by_source,
                    parsed_docs_by_source=parsed_by_src,
                    rules=compiled,
                    exchange_index=ev.exchange_index,
                    part="question",
                )
            )
            aq = _track_quote(
                ground_v3a_quote(
                    quotation=ev.answer_quote,
                    source_id=ev.source_id,
                    source_texts=source_texts,
                    document_pages_by_source=document_pages_by_source,
                    parsed_docs_by_source=parsed_by_src,
                    rules=compiled,
                    exchange_index=ev.exchange_index,
                    part="answer",
                )
            )
            # R3-D7 3-Tier Refusal Triage (anti-abuse: topic_pivot is NEVER whitelisted or downgraded!)
            qa_text_blob = f"{ev.question_quote} {ev.answer_quote} {ev.rationale}"
            seg_policy_regex_hit = any(rx.search(qa_text_blob) for rx in compiled.segment_policy_re)
            strict_nda_regex_hit = any(rx.search(qa_text_blob) for rx in compiled.conf_whitelist_re)
            legit_conf = bool(
                ev.behavior_subtype != "topic_pivot"
                and (
                    ev.confidentiality_policy_kind == "strict_nda_legal"
                    or (
                        ev.confidentiality_policy_kind != "segment_disclosure_policy"
                        and ev.question_asks_sensitive_detail
                        and (ev.exec_cites_confidentiality_policy or strict_nda_regex_hit)
                    )
                )
            )
            disc_policy_gap = bool(
                not legit_conf
                and ev.behavior_subtype != "topic_pivot"
                and (
                    ev.confidentiality_policy_kind == "segment_disclosure_policy"
                    or seg_policy_regex_hit
                )
            )
            if legit_conf:
                disp_sev: CollisionSeverity = "info"
                collapsed = True
                conf_whitelist_count += 1
                evasion_dist["confidentiality_whitelisted"] += 1
            elif disc_policy_gap:
                disp_sev = "info"
                collapsed = False
                disclosure_gap_count += 1
                evasion_dist["disclosure_policy_gap"] += 1
            elif ev.evasion_level == "direct":
                disp_sev = "green"
                collapsed = True
                evasion_dist["direct"] += 1
            elif not (qq.verified and aq.verified):
                disp_sev = "info"
                collapsed = True
                evasion_dist[ev.evasion_level] += 1
            elif ev.evasion_level == "fully_evasive":
                disp_sev = "red"
                collapsed = False
                evasion_ry_count += 1
                evasion_dist["fully_evasive"] += 1
            else:
                disp_sev = "yellow"
                collapsed = False
                evasion_ry_count += 1
                evasion_dist["intermediate"] += 1

            qa_findings.append(
                QaEvasionFinding(
                    finding_id=_fid("qa", f"{idx}:{ev.exchange_index}:{ev.analyst_name}"),
                    exchange_index=ev.exchange_index,
                    analyst_name=ev.analyst_name,
                    analyst_firm=ev.analyst_firm,
                    executive_name=ev.executive_name,
                    executive_role=ev.executive_role,
                    evasion_level=ev.evasion_level,
                    behavior_subtype=ev.behavior_subtype,
                    display_severity=disp_sev,
                    legitimate_confidentiality=legit_conf,
                    disclosure_policy_gap=disc_policy_gap,
                    collapsed_by_default=collapsed,
                    sub_questions=list(ev.sub_questions),
                    unanswered_sub_questions=list(ev.unanswered_sub_questions),
                    topic_tags=list(ev.topic_tags),
                    followup_count=ev.followup_count,
                    rationale=ev.rationale,
                    question_quote=qq,
                    answer_quote=aq,
                )
            )

    # Sort Tab 2: red -> yellow -> info -> green
    _ev_order = {"red": 0, "yellow": 1, "info": 2, "green": 3, "not_applicable": 4}
    qa_findings.sort(key=lambda x: (_ev_order.get(x.display_severity, 9), x.exchange_index))

    # ------------------------------------------------------------------ Track 6: Commitments, Guidance Range Drift, Silent Drop & Speaker Baseline
    existing_commitments = list(existing_commitment_ledger.commitments) if existing_commitment_ledger else []
    by_id: dict[str, VerbalCommitmentRecord] = {c.commitment_id: c for c in existing_commitments}

    # Add newly extracted commitments from management extraction
    if mgmt_extraction is not None:
        for vc in mgmt_extraction.verbal_commitments:
            if vc.evidence_sufficiency != "explicit":
                continue
            qref = _track_quote(
                ground_v3a_quote(
                    quotation=vc.verbatim_quote,
                    source_id=vc.source_id,
                    source_texts=source_texts,
                    document_pages_by_source=document_pages_by_source,
                    parsed_docs_by_source=parsed_by_src,
                    rules=compiled,
                    turn_index=vc.turn_index,
                    slide_index=vc.slide_index,
                )
            )
            if not qref.verified:
                continue
            eff_scope = _infer_effective_scope(vc.scope, vc.metric_canonical, vc.kpi_key, vc.commitment_text, vc.verbatim_quote)
            eff_basis = _resolve_effective_basis(vc.basis, call_default_basis, vc.metric_canonical)
            target_key = _fiscal_key(vc.target_fy, vc.target_fq) or vc.horizon_label
            if eff_scope != "consolidated":
                tier = "operating_kpi_next_call"
                init_status = "pending_next_call"
            elif vc.metric_canonical in TWELVE_XBRL_METRICS:
                if eff_basis == "non_gaap" and vc.metric_canonical not in _REVENUE_METRICS:
                    tier = "8k_nongaap_manual"
                    init_status = "manual_nongaap_review"
                elif ledger.status == "non_sec_entity":
                    tier = "next_call_check"
                    init_status = "pending_next_call"
                else:
                    tier = "xbrl_auto"
                    init_status = "pending_10q"
            elif vc.kpi_key:
                tier = "operating_kpi_next_call"
                init_status = "pending_next_call"
            else:
                tier = "qualitative_milestone"
                init_status = "pending_next_call"

            cid = _commitment_stable_id(entity_key, vc.metric_canonical, vc.kpi_key, target_key, vc.commitment_text)
            prev = by_id.get(cid)
            if prev is not None and prev.muted:
                continue

            # Guidance Range Drift check against any existing commitment targeting the same (metric, target_period_key)
            drift_note: str | None = None
            range_hist: list[GuidanceRangeSnapshot] = []
            if target_key and (vc.value_low is not None or vc.value_high is not None):
                prior_same_target = [
                    c
                    for c in by_id.values()
                    if c.commitment_id != cid
                    and c.metric_canonical == vc.metric_canonical
                    and c.target_period_key == target_key
                    and c.source_fiscal_period != fiscal_period_key
                    and (c.value_low is not None or c.value_high is not None)
                ]
                if prior_same_target:
                    prev_g = prior_same_target[-1]
                    range_hist = list(prev_g.range_history)
                    if not range_hist:
                        range_hist.append(
                            GuidanceRangeSnapshot(
                                fiscal_period_key=prev_g.source_fiscal_period or "prior",
                                call_date=prev_g.call_date,
                                value_low=prev_g.value_low,
                                value_high=prev_g.value_high,
                                unit=prev_g.unit,
                                scale=prev_g.scale,
                            )
                        )
                    range_hist.append(
                        GuidanceRangeSnapshot(
                            fiscal_period_key=fiscal_period_key or "current",
                            call_date=call_date_iso,
                            value_low=vc.value_low,
                            value_high=vc.value_high,
                            unit=vc.unit,
                            scale=vc.scale,
                        )
                    )
                    p_lo = _scale_claim_value(_dec(prev_g.value_low), prev_g.unit, prev_g.scale, vc.metric_canonical)
                    p_hi = _scale_claim_value(_dec(prev_g.value_high or prev_g.value_low), prev_g.unit, prev_g.scale, vc.metric_canonical)
                    c_lo = _scale_claim_value(_dec(vc.value_low), vc.unit, vc.scale, vc.metric_canonical)
                    c_hi = _scale_claim_value(_dec(vc.value_high or vc.value_low), vc.unit, vc.scale, vc.metric_canonical)
                    if p_lo is not None and p_hi is not None and c_lo is not None and c_hi is not None:
                        p_mid = (p_lo + p_hi) / Decimal(2)
                        c_mid = (c_lo + c_hi) / Decimal(2)
                        p_w = abs(p_hi - p_lo)
                        c_w = abs(c_hi - c_lo)
                        notes_d: list[str] = []
                        if p_mid != 0 and ((c_mid - p_mid) / abs(p_mid)) < Decimal("-0.02"):
                            notes_d.append(f"中枢下移（{_fmt_dec(p_mid, 2)} → {_fmt_dec(c_mid, 2)}）")
                        elif p_mid != 0 and ((c_mid - p_mid) / abs(p_mid)) > Decimal("0.02"):
                            notes_d.append(f"中枢上调（{_fmt_dec(p_mid, 2)} → {_fmt_dec(c_mid, 2)}）")
                        if p_w > 0 and c_w > p_w * Decimal("1.25"):
                            notes_d.append("指引区间拓宽 >25%（可见度下降）")
                        if notes_d:
                            drift_note = f"{target_key} 指引漂移：{'；'.join(notes_d)}"

            by_id[cid] = VerbalCommitmentRecord(
                commitment_id=cid,
                owner_id=owner_id,
                entity_key=entity_key,
                source_id=vc.source_id,
                source_fiscal_period=fiscal_period_key,
                call_date=call_date_iso,
                metric_canonical=vc.metric_canonical,
                kpi_key=vc.kpi_key or (f"{eff_scope}:{vc.metric_canonical}" if eff_scope != "consolidated" else None),
                scope=eff_scope,
                basis=vc.basis,
                effective_basis=eff_basis,
                period_scope=vc.period_scope,
                target_fy=vc.target_fy,
                target_fq=vc.target_fq,
                target_period_key=target_key,
                operator=vc.operator,
                value_low=vc.value_low,
                value_high=vc.value_high,
                unit=vc.unit,
                scale=vc.scale,
                currency=vc.currency,
                verification_tier=tier,
                status=init_status,
                is_high_profile_theme=vc.is_high_profile_theme,
                theme_key=vc.theme_key,
                qualitative_milestone=vc.qualitative_milestone,
                commitment_text=vc.commitment_text,
                linked_pillar=_map_linked_pillar(vc.metric_canonical, vc.commitment_text, thesis_pillars),
                muted=False,
                range_drift_note=drift_note,
                range_history=range_hist,
                skeptic_note=skeptic_map.get((vc.theme_key or vc.metric_canonical).casefold()),
                quote=qref,
            )

    # Index current-call verified explicit Operating KPI claims for cross-quarter Track 6 arithmetic grading
    def _norm_kpi(raw: str | None) -> str:
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (raw or "").casefold())

    cur_verified_kpi_claims: dict[str, list[ManagementFactualClaim]] = {}
    if mgmt_extraction is not None:
        for cl in mgmt_extraction.factual_claims:
            if cl.kpi_key and cl.evidence_sufficiency == "explicit" and cl.value_low is not None:
                nk = _norm_kpi(cl.kpi_key)
                if nk:
                    cur_verified_kpi_claims.setdefault(nk, []).append(cl)

    # Evaluate all active (non-muted) commitments against Period P AND P+1 (four-timestamp hindsight grading) + Operating KPI + Silent Drop
    mgmt_corpus_low = mgmt_corpus.casefold()
    updated_commitments: list[VerbalCommitmentRecord] = []
    for cmt in by_id.values():
        if cmt.muted:
            updated_commitments.append(cmt)
            continue

        # 1b) Cross-quarter Operating KPI arithmetic grading against current call's verified self-reported KPI actual
        if (
            cmt.verification_tier == "operating_kpi_next_call"
            and cmt.status == "pending_next_call"
            and cmt.kpi_key
            and cmt.source_fiscal_period
            and fiscal_period_key
            and cmt.source_fiscal_period != fiscal_period_key
            and (cmt.value_low is not None or cmt.value_high is not None)
        ):
            nk = _norm_kpi(cmt.kpi_key)
            matching_kpis = cur_verified_kpi_claims.get(nk) or []
            for mcl in matching_kpis:
                # Require compatible unit family (% vs non-%) and matching target period if both specify one
                same_pct = (cmt.unit == "%") == (mcl.unit == "%")
                period_ok = (
                    cmt.target_period_key is None
                    or cmt.target_period_key == fiscal_period_key
                    or (mcl.target_fy == cmt.target_fy and mcl.target_fq == cmt.target_fq)
                )
                if not same_pct or not period_ok:
                    continue
                act_kpi = _scale_claim_value(_dec(mcl.value_low), mcl.unit, mcl.scale, "other")
                lo_kpi = _scale_claim_value(_dec(cmt.value_low), cmt.unit, cmt.scale, "other")
                hi_kpi = _scale_claim_value(_dec(cmt.value_high), cmt.unit, cmt.scale, "other")
                if act_kpi is not None and (lo_kpi is not None or hi_kpi is not None):
                    passed_kpi, detail_kpi = _evaluate_operator(
                        operator=cmt.operator,
                        actual=act_kpi,
                        prior_year=None,
                        low=lo_kpi,
                        high=hi_kpi,
                        rel_tol_pct=rel_tol,
                        abs_tol=Decimal("0"),
                    )
                    cmt = cmt.model_copy(
                        update={
                            "status": "met" if passed_kpi else "missed",
                            "actual_value": f"{mcl.value_low}{mcl.unit or ''}",
                            "verified_against_accession": f"call_kpi:{mcl.source_id}",
                            "verified_at_filed_date": call_date_iso,
                            "delta_note": f"跨季管理层自报运营 KPI 算术比对 ({detail_kpi})",
                        }
                    )
                    break

        # 1) XBRL Auto Grading (including immediate P+1 hindsight when p1_10q_filed <= as_of and > call_date)
        if cmt.verification_tier == "xbrl_auto" and snap is not None and cmt.target_fy is not None:
            cmt_call_d = _parse_iso_date(cmt.call_date)
            # Determine initial_filing_cutoff: if targeting P use init_10q_filed; if P+1 use p1_10q_filed
            cutoff_for_target: date | None = None
            if cmt.target_fy == ledger.target_fiscal_year and cmt.target_fq == ledger.target_fiscal_quarter:
                cutoff_for_target = init_10q_filed
            elif cmt.target_fy == ledger.p1_fiscal_year and cmt.target_fq == ledger.p1_fiscal_quarter:
                cutoff_for_target = p1_10q_filed

            xres = xbrl_actuals_lookup(
                snap,
                cmt.metric_canonical,
                target_fy=cmt.target_fy,
                target_fq=cmt.target_fq,
                basis_lock=cmt.basis,
                call_default_basis="non_gaap" if cmt.effective_basis == "non_gaap" else "gaap",
                period_scope=cmt.period_scope,
                claim_currency=cmt.currency,
                initial_filing_cutoff=cutoff_for_target,
                as_of=as_of,
                restatement_notice_pct=th.restatement_notice_pct,
            )
            filed_d = _parse_iso_date(xres.filed_at)
            # Strict four-timestamp availability check: actual must be filed <= as_of AND > commitment call_date
            if (
                xres.status == "matched"
                and xres.value is not None
                and (as_of is None or filed_d is None or filed_d <= as_of)
                and (cmt_call_d is None or filed_d is None or filed_d > cmt_call_d)
            ):
                act_d = _dec(xres.value)
                py_d = _dec(xres.prior_year_value)
                c_dim, c_op, _c_dim_note = _check_dimension_compatibility(
                    metric_canonical=cmt.metric_canonical,
                    operator=cmt.operator,
                    unit=cmt.unit,
                    scale=cmt.scale,
                    text_blob=f"{cmt.commitment_text} {cmt.quote.quotation if cmt.quote else ''}",
                )
                if c_dim == "mismatch":
                    continue
                c_is_yoy = c_op in ("growth_yoy_ge", "growth_yoy_le", "growth_yoy_approx", "growth_yoy_between")
                if c_is_yoy:
                    lo_d = _dec(cmt.value_low)
                    hi_d = _dec(cmt.value_high)
                    if _is_bps_unit(cmt.unit, cmt.scale):
                        lo_d = (lo_d / Decimal("100")) if lo_d is not None else None
                        hi_d = (hi_d / Decimal("100")) if hi_d is not None else None
                else:
                    lo_d = _scale_claim_value(_dec(cmt.value_low), cmt.unit, cmt.scale, cmt.metric_canonical)
                    hi_d = _scale_claim_value(_dec(cmt.value_high), cmt.unit, cmt.scale, cmt.metric_canonical)
                if act_d is not None:
                    c_round_tol = (
                        _rounding_half_step_tolerance(cmt.value_low, "%", None, "gross_margin")
                        if c_is_yoy
                        else _rounding_half_step_tolerance(cmt.value_low, cmt.unit, cmt.scale, cmt.metric_canonical)
                    )
                    abs_tol = max(margin_tol_pct if cmt.metric_canonical in _MARGIN_METRICS else Decimal("0"), c_round_tol)
                    passed, detail = _evaluate_operator(
                        operator=c_op,
                        actual=act_d,
                        prior_year=py_d,
                        low=lo_d,
                        high=hi_d,
                        rel_tol_pct=rel_tol,
                        abs_tol=abs_tol,
                        is_margin_metric=(cmt.metric_canonical in _MARGIN_METRICS),
                    )
                    cmt = cmt.model_copy(
                        update={
                            "status": "met" if passed else "missed",
                            "actual_value": xres.value,
                            "actual_period_end": xres.period_end,
                            "verified_against_accession": xres.accession,
                            "verified_at_filed_date": xres.filed_at,
                            "delta_note": detail,
                        }
                    )

        # 2) Chronological Silent Drop Tiering (prior-quarter open/high-profile commitment absent in current call)
        if (
            mgmt_corpus_low
            and cmt.source_fiscal_period
            and fiscal_period_key
            and cmt.source_fiscal_period != fiscal_period_key
            and cmt.status in ("pending_10q", "pending_next_call", "manual_nongaap_review")
        ):
            # Ensure chronological order (only detect silent drop when current call is later than commitment call)
            cmt_d = _parse_iso_date(cmt.call_date)
            is_older = (call_d is not None and cmt_d is not None and call_d > cmt_d) or (
                cmt.source_fiscal_period < fiscal_period_key
            )
            if is_older:
                probe_terms = [
                    t
                    for t in (cmt.theme_key, cmt.kpi_key)
                    if t and len(t.strip()) >= 3
                ]
                if not probe_terms:
                    probe_terms = [tok for tok in tokenize_query(cmt.commitment_text) if len(tok) >= 5][:4]
                mentioned = any(term.casefold() in mgmt_corpus_low for term in probe_terms) if probe_terms else True
                if not mentioned:
                    if cmt.is_high_profile_theme or cmt.verification_tier in ("xbrl_auto", "operating_kpi_next_call"):
                        cmt = cmt.model_copy(
                            update={
                                "status": "silent_drop_high_risk",
                                "delta_note": f"上期（{cmt.source_fiscal_period}）高调提出/未到期承诺在本期（{fiscal_period_key}）纪要与 PPT 中零提及且无收官说明。",
                                "skeptic_note": cmt.skeptic_note or "可能系分析师本季未追问或项目已正常完结但未主动通报。",
                            }
                        )
                    else:
                        cmt = cmt.model_copy(
                            update={
                                "status": "likely_concluded_or_unasked",
                                "delta_note": f"上期（{cmt.source_fiscal_period}）定性事项本期未提及。",
                                "skeptic_note": cmt.skeptic_note or "可能系已收官或非本季焦点，风险权重较低。",
                            }
                        )

        updated_commitments.append(cmt)

    # Surface missed commitments & silent_drop_high_risk into collision_findings as well for unified L1/Tab1 awareness
    for cmt in updated_commitments:
        if cmt.muted:
            continue
        if cmt.status == "missed":
            findings.append(
                CollisionFinding(
                    finding_id=_fid("t6miss", cmt.commitment_id),
                    track="track_6_commitments",
                    collision_kind="direct_contradiction",
                    severity="red",
                    title=f"历史口头承诺违背 · {cmt.metric_canonical} ({cmt.target_period_key or ''})",
                    management_statement=cmt.commitment_text,
                    ledger_evidence=f"10-Q 实绩 {cmt.actual_value}（{cmt.delta_note or ''}，accn {cmt.verified_against_accession or 'N/A'}）",
                    basis_badge="跨季承诺验真",
                    period_badge=cmt.target_period_key,
                    linked_pillar=cmt.linked_pillar,
                    quotes=[cmt.quote] if cmt.quote else [],
                    priority_rank=6,
                )
            )
        elif cmt.status == "silent_drop_high_risk":
            findings.append(
                CollisionFinding(
                    finding_id=_fid("t6sd", cmt.commitment_id),
                    track="track_6_commitments",
                    collision_kind="direct_contradiction",
                    severity="yellow",
                    title=f"高危 Silent Drop · 上季承诺本季消失 ({cmt.theme_key or cmt.kpi_key or cmt.metric_canonical})",
                    management_statement=cmt.commitment_text,
                    ledger_evidence=cmt.delta_note or "本期管理层材料零提及。",
                    basis_badge=f"源自 {cmt.source_fiscal_period or '上季'}",
                    period_badge=fiscal_period_key,
                    linked_pillar=cmt.linked_pillar,
                    skeptic_note=cmt.skeptic_note,
                    quotes=[cmt.quote] if cmt.quote else [],
                    priority_rank=22,
                )
            )

    # Speaker-Baseline Shift (>= 2 quarters of same executive)
    prior_baselines = list(existing_commitment_ledger.executive_baselines) if existing_commitment_ledger else []
    cur_exec_evasions: dict[str, list[QaEvasionFinding]] = {}
    for qf in qa_findings:
        cur_exec_evasions.setdefault(qf.executive_name.casefold(), []).append(qf)

    cur_baselines: list[ExecutiveQuarterBaseline] = []
    for doc in mgmt_docs:
        for eb in doc.executive_baselines:
            evs = cur_exec_evasions.get(eb.executive_name.casefold(), [])
            ev_rate = (
                sum(1 for e in evs if e.display_severity in ("red", "yellow")) / float(len(evs)) * 100.0
                if evs
                else 0.0
            )
            cur_baselines.append(
                ExecutiveQuarterBaseline(
                    fiscal_period_key=fiscal_period_key or "current",
                    call_date=call_date_iso,
                    executive_name=eb.executive_name,
                    speaker_role=eb.speaker_role,
                    answer_count=eb.answer_count,
                    avg_hedge_density=eb.avg_hedge_density,
                    avg_verbosity_ratio=eb.avg_verbosity_ratio,
                    evasion_rate_pct=round(ev_rate, 1),
                )
            )

    baseline_shifts: list[SpeakerBaselineShiftFinding] = []
    min_q = th.speaker_baseline_shift_min_quarters
    for cb in cur_baselines:
        hist = [
            pb
            for pb in prior_baselines
            if pb.executive_name.casefold() == cb.executive_name.casefold()
            and pb.fiscal_period_key != cb.fiscal_period_key
        ]
        if len(hist) + 1 >= min_q and hist:
            h_hedge = sum(p.avg_hedge_density for p in hist) / float(len(hist))
            h_ev = sum(p.evasion_rate_pct for p in hist) / float(len(hist))
            d_hedge = round(cb.avg_hedge_density - h_hedge, 4)
            d_ev = round(cb.evasion_rate_pct - h_ev, 1)
            shifted = (d_hedge >= th.speaker_hedge_shift_yellow) or (d_ev >= th.speaker_evasion_rate_shift_yellow)
            baseline_shifts.append(
                SpeakerBaselineShiftFinding(
                    executive_name=cb.executive_name,
                    speaker_role=cb.speaker_role,
                    quarters_observed=len(hist) + 1,
                    current_hedge_density=cb.avg_hedge_density,
                    historical_avg_hedge_density=round(h_hedge, 4),
                    hedge_density_delta=d_hedge,
                    current_evasion_rate_pct=cb.evasion_rate_pct,
                    historical_avg_evasion_rate_pct=round(h_ev, 1),
                    evasion_rate_delta_pp=d_ev,
                    severity="yellow" if shifted else "info",
                    summary=(
                        f"{cb.executive_name}（观察 {len(hist) + 1} 季）模糊词密度变化 {d_hedge:+.4f}，回避率变化 {d_ev:+.1f}pp。"
                    ),
                )
            )

    # Merge baseline history (dedup by executive + fiscal_period_key)
    merged_baselines_map: dict[tuple[str, str], ExecutiveQuarterBaseline] = {
        (b.executive_name.casefold(), b.fiscal_period_key): b for b in prior_baselines
    }
    for cb in cur_baselines:
        merged_baselines_map[(cb.executive_name.casefold(), cb.fiscal_period_key)] = cb
    merged_baselines = list(merged_baselines_map.values())[-40:]

    updated_ledger = AccountVerbalCommitmentLedger(
        owner_id=owner_id or (existing_commitment_ledger.owner_id if existing_commitment_ledger else "unknown"),
        entity_key=entity_key,
        symbol=ledger.symbol or (existing_commitment_ledger.symbol if existing_commitment_ledger else None),
        company_name=ledger.company_name or (existing_commitment_ledger.company_name if existing_commitment_ledger else None),
        commitments=updated_commitments,
        executive_baselines=merged_baselines,
    )

    # ------------------------------------------------------------------ Tab 3: 4-Tier Expectation Benchmark Isolation
    benchmarks: list[ExpectationComparisonItem] = []

    # Tier 1: Read-only V2.5 public GUIDANCE_CREDIBILITY_LEDGER prior
    if v25_credibility_records:
        total_v25 = len(v25_credibility_records)
        beats = sum(1 for r in v25_credibility_records if str(r.get("outcome") or "").lower() == "beat")
        misses = sum(1 for r in v25_credibility_records if str(r.get("outcome") or "").lower() == "miss")
        benchmarks.append(
            ExpectationComparisonItem(
                tier="v25_historical_credibility",
                authority_tier="sec_public_prior",
                title="V2.5 公共指引历史信用先验（只读）",
                summary=f"历史公开指引记录 {total_v25} 条（Beat {beats} / Miss {misses}），仅供先验参考，V3a 私有上传绝不回写公共台账。",
                exportable=True,
            )
        )

    # Tier 2: Analyst Q&A cluster hotspots (>=2 analysts pressing same topic)
    if mgmt_extraction is not None:
        for sig in mgmt_extraction.expectation_signals:
            if sig.kind == "analyst_cluster" and sig.analyst_count >= 2:
                qref = _track_quote(
                    ground_v3a_quote(
                        quotation=sig.verbatim_quote,
                        source_id=sig.source_id,
                        source_texts=source_texts,
                        document_pages_by_source=document_pages_by_source,
                        parsed_docs_by_source=parsed_by_src,
                        rules=compiled,
                    )
                )
                benchmarks.append(
                    ExpectationComparisonItem(
                        tier="analyst_cluster_hotspot",
                        authority_tier="call_qa_signal",
                        title=f"分析师集群追问热区（{sig.analyst_count} 位分析师）· {sig.topic}",
                        summary=sig.summary,
                        analyst_count=sig.analyst_count,
                        exportable=bool((source_permissions or {}).get(sig.source_id, {}).get("export", True)),
                        quote=qref if qref.verified else None,
                    )
                )

    # Tier 3: Sell-side notes (strictly isolated third_party_opinion; cached independently of management transcript!)
    for ss in sellside_extractions:
        ss_export = bool((source_permissions or {}).get(ss.source_id, {}).get("export", True))
        for sig in ss.expectation_signals:
            qref = _track_quote(
                ground_v3a_quote(
                    quotation=sig.verbatim_quote,
                    source_id=ss.source_id,
                    source_texts=source_texts,
                    document_pages_by_source=document_pages_by_source,
                    parsed_docs_by_source=parsed_by_src,
                    rules=compiled,
                )
            )
            benchmarks.append(
                ExpectationComparisonItem(
                    tier="sellside_third_party",
                    authority_tier="third_party_opinion",
                    title=f"卖方预期（{ss.sellside_firm or 'Third-Party'} · {'前瞻' if ss.sellside_timing == 'pre_earnings_preview' else ('点评' if ss.sellside_timing == 'post_earnings_review' else '研报')}）· {sig.topic}",
                    summary=sig.summary,
                    sellside_firm=ss.sellside_firm,
                    sellside_timing=ss.sellside_timing,
                    exportable=ss_export,
                    quote=qref if qref.verified else None,
                )
            )

    # Tier 4: Optional V3b BYOK Consensus extension seam
    for provider in extra_benchmark_providers:
        try:
            benchmarks.extend(
                provider.provide(entity_key=entity_key, fiscal_period_key=fiscal_period_key, as_of=as_of)
            )
        except Exception:
            continue

    # Sort Tab 1 findings by priority_rank
    findings.sort(key=lambda f: (f.priority_rank, f.finding_id))

    red_count = sum(1 for f in findings if f.severity == "red")
    yellow_count = sum(1 for f in findings if f.severity == "yellow")
    green_count = sum(1 for f in findings if f.severity == "green")

    if red_count > 0 or evasion_ry_count >= ev_cfg.red_evasion_count_threshold:
        overall_light: CollisionSeverity = "red"
    elif yellow_count > 0 or evasion_ry_count >= ev_cfg.yellow_evasion_count_threshold:
        overall_light = "yellow"
    else:
        overall_light = "green"

    # Build concise L1 30-second headline
    light_emoji = {"red": "🔴", "yellow": "🟡", "green": "🟢"}.get(overall_light, "⚪")
    period_tag = fiscal_period_key or (ledger.symbol or entity_key)
    top_issue = next((f.title for f in findings if f.severity in ("red", "yellow")), None)
    if top_issue:
        ev_tail = f" · Q&A 回避 {evasion_ry_count}/{len(qa_findings)} 轮" if qa_findings else ""
        headline = f"{light_emoji} {period_tag} · {top_issue}{ev_tail}"
    elif ledger.status == "awaiting_current_10q_using_8k_and_prior_10q":
        headline = f"{light_emoji} {period_tag} · 同场与 Q&A 初审完成，待当季 10-Q 归档后自动对撞"
    else:
        headline = f"{light_emoji} {period_tag} · 未检出重大口头与账本矛盾（核验 {len(findings)} 项）"

    skeptic_cal = list(dict.fromkeys([*(f.skeptic_note for f in findings if f.skeptic_note), *(sk.note for sk in (mgmt_extraction.skeptic_notes if mgmt_extraction else []))]))[:8]
    lane_gap_tags = [
        f"lane_failed:{lname}"
        for lname, lst in ((mgmt_extraction.partial_lane_status if mgmt_extraction else {}) or {}).items()
        if lst == "failed"
    ]
    open_gaps = list(dict.fromkeys([*ledger.open_gaps, *(g for d in parsed_docs for g in d.open_gaps), *lane_gap_tags]))
    src_ids = list(dict.fromkeys(d.source_id for d in parsed_docs))
    src_hashes = {d.source_id: d.content_sha256 for d in parsed_docs}

    report = CallVerificationReport(
        owner_id=owner_id,
        entity_key=entity_key,
        cik=ledger.cik,
        symbol=ledger.symbol,
        company_name=ledger.company_name,
        fiscal_period_key=fiscal_period_key,
        call_date=call_date_iso,
        call_default_basis=call_default_basis,
        ledger_status=ledger.status,
        awaiting_current_10q=(ledger.status == "awaiting_current_10q_using_8k_and_prior_10q"),
        overall_light=overall_light,
        headline=headline,
        red_count=red_count,
        yellow_count=yellow_count,
        green_count=green_count,
        evasion_red_yellow_count=evasion_ry_count,
        evasion_total_exchanges=len(qa_findings),
        evasion_distribution=evasion_dist,
        confidentiality_whitelisted_count=conf_whitelist_count,
        disclosure_policy_gap_count=disclosure_gap_count,
        collision_findings=findings,
        qa_evasions=qa_findings,
        commitments=updated_commitments,
        speaker_baseline_shifts=baseline_shifts,
        expectation_benchmarks=benchmarks,
        omission_phase2_candidates=omission_candidates,
        skeptic_calibration=skeptic_cal,
        open_gaps=open_gaps,
        source_ids=src_ids,
        source_hashes=src_hashes,
        verified_quote_count=verified_quotes,
        total_quote_count=total_quotes,
        rules_sha256=compiled.rules_sha256,
        evaluation_rules_version=compiled.evaluation_rules_version,
        context_budget_version=compiled.context_budget_version,
        model_id=(
            (mgmt_extraction.model_id if mgmt_extraction else None)
            or next((s.model_id for s in sellside_extractions if s.model_id), None)
        ),
        total_tokens=(mgmt_extraction.total_tokens if mgmt_extraction else 0) + sum(s.total_tokens for s in sellside_extractions),
        cache_read_input_tokens=(mgmt_extraction.cache_read_input_tokens if mgmt_extraction else 0)
        + sum(s.cache_read_input_tokens for s in sellside_extractions),
        cache_creation_input_tokens=(mgmt_extraction.cache_creation_input_tokens if mgmt_extraction else 0)
        + sum(s.cache_creation_input_tokens for s in sellside_extractions),
        llm_call_count=(mgmt_extraction.call_count if mgmt_extraction else 0)
        + sum(max(1, int(s.call_count)) for s in sellside_extractions),
        partial_lane_status=dict(mgmt_extraction.partial_lane_status) if mgmt_extraction else {},
        lane_errors=dict(mgmt_extraction.lane_errors) if mgmt_extraction else {},
    )
    return report, updated_ledger


# --------------------------------------------------------------------------- Export-Aware Markdown Audit Brief Renderer (R2-D5)

def render_call_verification_markdown(
    report: CallVerificationReport,
    *,
    source_permissions: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """Render a compact 1-click Markdown audit brief strictly honoring ``permissions.export``."""
    perms = source_permissions or {}
    has_perms_table = bool(perms)

    def _can_export(src_id: str | None) -> bool:
        if not src_id:
            return True
        p = perms.get(src_id)
        if not isinstance(p, Mapping):
            # Fail-closed when caller provided a permissions map but source_id is missing (e.g. unmapped OCR version)
            return not has_perms_table
        return p.get("export") is not False

    def _quote_md(q: GroundedQuoteRef | None) -> str:
        if q is None:
            return ""
        if not _can_export(q.source_id):
            return "[受版权/导出权限限制，原话已脱敏]"
        loc_parts: list[str] = []
        if q.slide_number:
            loc_parts.append(f"Slide {q.slide_number}")
        elif q.page_number:
            loc_parts.append(f"p.{q.page_number}")
        if q.timestamp_start:
            loc_parts.append(q.timestamp_start)
        loc = f" ({', '.join(loc_parts)})" if loc_parts else ""
        return f'"{q.quotation[:220]}"{loc}'

    lines: list[str] = [
        f"# 电话会核验与预期差审计简报 — {report.symbol or report.entity_key} ({report.fiscal_period_key or 'N/A'})",
        "",
        f"**结论**: {report.headline}",
        f"- **账本状态**: `{report.ledger_status}` | **默认口径**: `{report.call_default_basis}` | **引文验真**: `{report.verified_quote_count}/{report.total_quote_count}`",
        f"- **规则版本**: `{report.evaluation_rules_version}` (`{report.rules_sha256}`)",
        "",
        "## 1. Talk vs. Walk（口头 vs 10-Q 账本与同场分歧）",
    ]
    if not report.collision_findings:
        lines.append("- 暂无显著冲突条目。")
    for f in report.collision_findings[:12]:
        badge = {"red": "🔴", "yellow": "🟡", "green": "🟢", "info": "ℹ️", "not_applicable": "⚪"}.get(f.severity, "•")
        q_str = " | ".join(_quote_md(q) for q in f.quotes if q.quotation)
        lines.append(f"- {badge} **{f.title}**")
        lines.append(f"  - 管理层: {f.management_statement}")
        lines.append(f"  - 账本/对照: {f.ledger_evidence}")
        if q_str:
            lines.append(f"  - 原文锚点: {q_str}")
        if f.restatement_note:
            lines.append(f"  - 重述提示: {f.restatement_note}")

    lines.extend(["", "## 2. Q&A 回避审计"])
    non_direct = [q for q in report.qa_evasions if q.display_severity in ("red", "yellow")]
    if not non_direct:
        lines.append(f"- 未检出高危回避（共审查 {report.evasion_total_exchanges} 轮；合理商业保密降噪 {report.confidentiality_whitelisted_count} 轮）。")
    for q in non_direct[:10]:
        badge = "🔴" if q.display_severity == "red" else "🟡"
        unans = f" | 漏答子问: {'; '.join(q.unanswered_sub_questions)}" if q.unanswered_sub_questions else ""
        lines.append(f"- {badge} **Q{q.exchange_index} ({q.analyst_name} → {q.executive_name})**: `{q.evasion_level}` / `{q.behavior_subtype}`{unans}")
        lines.append(f"  - 问: {_quote_md(q.question_quote)}")
        lines.append(f"  - 答: {_quote_md(q.answer_quote)}")

    lines.extend(["", "## 3. 跨季口头承诺与预期差"])
    active_cmts = [c for c in report.commitments if not c.muted]
    if not active_cmts:
        lines.append("- 暂无活跃口头承诺记录。")
    for c in active_cmts[:12]:
        lines.append(f"- **[{c.status}]** `{c.metric_canonical}` ({c.target_period_key or 'N/A'}): {c.commitment_text}")
        if c.delta_note:
            lines.append(f"  - 核验明细: {c.delta_note}")
        if c.range_drift_note:
            lines.append(f"  - 指引漂移: {c.range_drift_note}")

    if report.expectation_benchmarks:
        lines.extend(["", "### 预期差参照（权威分层隔离）"])
        for bm in report.expectation_benchmarks[:8]:
            if not bm.exportable:
                lines.append(f"- **[{bm.authority_tier}]** {bm.title}: [受导出权限限制已脱敏]")
            else:
                lines.append(f"- **[{bm.authority_tier}]** {bm.title}: {bm.summary}")

    if report.skeptic_calibration:
        lines.extend(["", "## 4. Skeptic 反方校正", *[f"- {s}" for s in report.skeptic_calibration[:6]]])

    lines.extend(["", f"> {report.text_only_evasion_disclaimer}"])
    return "\n".join(lines)
