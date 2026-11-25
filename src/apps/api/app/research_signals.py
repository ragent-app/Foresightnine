"""Canonical cross-engine signal envelope and V1 SEC event models.

All nullable fields explicitly declare ``= None`` defaults so round-tripping through
``storage.py`` (which strips ``None`` keys via ``exclude_none=True`` and
``_drop_legacy_null_fields``) is 100% lossless and safe.
"""
from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _FrozenSignalModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


EngineId = Literal[
    "sec_events",
    "earnings_squad",
    "peer_matrix",
    "call_qa",
    "sector_plugin",
    "supply_chain",
]
SignalDisposition = Literal["push_alert", "log_only", "suppress"]
SignalSeverity = Literal["critical", "high", "medium", "low"]
SignalDirection = Literal["positive", "negative", "neutral", "mixed"]
ImpactScope = Literal["company_only", "sector_readthrough", "supply_chain_readthrough"]
EvidenceCompleteness = Literal["complete", "preliminary_pending_amendment", "insufficient_in_excerpt"]
GroundingStatus = Literal["verified", "needs_review", "fail_safe_deterministic"]
ThesisPillar = Literal[
    "growth",
    "margin",
    "cash_quality",
    "capital_allocation",
    "valuation",
    "risk",
]


def deterministic_signal_id(*parts: str | None) -> str:
    """Build a stable, idempotent signal identifier from canonical components."""
    joined = "|".join((p or "").strip() for p in parts)
    return f"sig_{sha256(joined.encode('utf-8')).hexdigest()[:20]}"


class SignalQuote(_FrozenSignalModel):
    source_id: str
    accession: str | None = None
    location: str | None = None
    quotation: str
    verified: bool = False
    match_space: Literal["raw", "norm_text", "norm_table"] | None = None
    quote_context_warning: str | None = None
    surrounding_context: str | None = None


class SignalProvenance(_FrozenSignalModel):
    provider: str = "sec_edgar"
    cik: str | None = None
    accession: str | None = None
    form: str | None = None
    filename: str | None = None
    sec_url: str | None = None
    source_id: str | None = None
    content_sha256: str | None = None
    owner_scoped: bool = False
    exportable: bool = True


class ScaleAnchor(_FrozenSignalModel):
    """SEC-native company scale metrics (strictly ``owner_scoped=False``)."""

    cik: str
    as_of_filed_date: date | None = None
    taxonomy_used: str | None = None
    currency: str | None = None
    latest_ttm_revenue: str | None = None
    latest_net_income: str | None = None
    latest_total_assets: str | None = None
    public_float_usd: str | None = None
    public_float_as_of_date: date | None = None
    shares_outstanding: str | None = None
    shares_outstanding_as_of_date: date | None = None
    share_unit_basis: Literal["common_shares", "ordinary_vs_adr_unverified"] = "common_shares"
    scale_anchor_gaps: list[str] = Field(default_factory=list)


class QuantifiedFinancialImpact(_FrozenSignalModel):
    metric: str
    amount_or_range: str
    currency: str | None = None
    cash_or_non_cash: Literal["cash", "non_cash", "mixed", "unspecified"] = "unspecified"
    affected_period: str | None = None
    materiality_ratio_note: str | None = None


class UpcomingMilestone(_FrozenSignalModel):
    description: str
    expected_date_or_window: str | None = None
    trigger_type: Literal["amendment_expected", "closing_date", "effective_date", "earnings_call", "regulatory", "other"] = "other"


class CompanySignalEnvelope(_FrozenSignalModel):
    """Canonical signal emitted by any background vertical engine for downstream Agents & Direction B."""

    signal_id: str
    engine_id: EngineId = "sec_events"
    entity_id: str
    cik: str | None = None
    symbol: str | None = None
    company_name: str | None = None
    event_date: date | None = None
    disclosed_at: str
    reporting_lag_days: int | None = None
    event_thread_key: str | None = None
    amends_accession: str | None = None
    superseded_by_accession: str | None = None
    disposition: SignalDisposition
    severity: SignalSeverity
    direction: SignalDirection
    event_subtype: str
    affected_thesis_pillars: list[ThesisPillar] = Field(default_factory=list)
    impact_scope: ImpactScope = "company_only"
    mentioned_counterparties: list[str] = Field(default_factory=list)
    market_pricing_context: str = "price_reaction_unobserved"
    evidence_completeness: EvidenceCompleteness = "complete"
    open_questions: list[str] = Field(default_factory=list)
    summary: str
    skeptic_counter_check: str = ""
    skeptic_dissent: str | None = None
    thesis_impact: str = ""
    cross_filing_links: list[str] = Field(default_factory=list)
    quantified_financial_impact: list[QuantifiedFinancialImpact] = Field(default_factory=list)
    upcoming_milestones: list[UpcomingMilestone] = Field(default_factory=list)
    key_facts: dict[str, Any] = Field(default_factory=dict)
    verbatim_quotes: list[SignalQuote] = Field(default_factory=list)
    grounding_status: GroundingStatus = "verified"
    fail_safe_triggered: bool = False
    suppression_reason: str | None = None
    provenance: SignalProvenance
    payload: dict[str, Any] = Field(default_factory=dict)


class LazyAttachmentRef(_FrozenSignalModel):
    """Deferred handle for large SEC attachments (e.g. EX-10.1 / EX-4.*) to prevent 512MB OOM."""

    source_id: str
    filename: str
    exhibit_type: str | None = None
    attachment_role: Literal[
        "ex16_auditor_letter",
        "ex99_press_release",
        "ex10_material_contract",
        "ex4_debt_indenture",
        "other_exhibit",
    ] = "other_exhibit"
    sec_url: str
    description: str | None = None


class RawEventCandidate(_FrozenSignalModel):
    """Layer 1 objective candidate passed to Layer 2 (strictly de-anchored: no subjective severity)."""

    candidate_id: str
    cik: str
    symbol: str | None = None
    company_name: str | None = None
    category: Literal["material_8k", "insider_trade", "ownership_13dg", "foreign_6k"]
    form: str
    accession: str
    amends_accession: str | None = None
    event_thread_key: str
    filed_at: date
    event_date: date | None = None
    reporting_lag_days: int | None = None
    items: list[str] = Field(default_factory=list)
    statutory_critical_items: list[str] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
    excerpt: str = ""
    source_id: str
    sec_url: str
    filename: str | None = None
    content_sha256: str | None = None
    lazy_attachments: list[LazyAttachmentRef] = Field(default_factory=list)
    is_context_only: bool = False


class SuppressedEventRecord(_FrozenSignalModel):
    accession: str
    form: str
    filed_at: date
    category: Literal["material_8k", "insider_trade", "ownership_13dg", "foreign_6k"]
    suppression_reason: str
    items: list[str] = Field(default_factory=list)
    summary_hint: str | None = None


class FailedAccession(_FrozenSignalModel):
    accession: str
    form: str
    filed_at: date | None = None
    error_reason: str
    retryable: bool = True


class EventRunMetrics(_FrozenSignalModel):
    rules_version: str = "v1"
    parser_version: str = "v1"
    total_filings_inspected: int = 0
    l1_cache_hits: int = 0
    l1_cache_misses: int = 0
    l1_suppressed_counts: dict[str, int] = Field(default_factory=dict)
    l1_failed_count: int = 0
    l1_candidates_emitted: int = 0
    l1_overflow_deferred: int = 0
    l2_batches_run: int = 0
    l2_cache_hits: int = 0
    l2_drilldown_turns: int = 0
    l2_skeptic_calls: int = 0
    l2_skeptic_downgrades: int = 0
    l2_invariant_corrections: int = 0
    l2_negation_warnings: int = 0
    l2_omission_repairs: int = 0
    l2_fail_safes: int = 0
    disposition_counts: dict[str, int] = Field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    elapsed_ms: int = 0
    model_used: str | None = None
    protocol_used: str | None = None


class PeriodicFilingRef(_FrozenSignalModel):
    """Lightweight metadata for a periodic filing (10-Q/10-K/20-F or NT late-filing notice)."""

    cik: str
    accession: str
    form: str
    filed_at: date
    period_of_report: date | None = None
    primary_document_url: str | None = None
    is_late_notice: bool = False


class SecEventScanResult(_FrozenSignalModel):
    """Output of Layer 1 deterministic scan (0 LLM)."""

    cik: str
    entity_id: str
    symbol: str | None = None
    company_name: str | None = None
    is_fpi: bool = False
    fpi_exempt_from_form4: bool = False
    as_of: date | None = None
    lookback_days: int = 90
    rules_version: str = "v1"
    parser_version: str = "v1"
    scale_anchor: ScaleAnchor | None = None
    timing_context: dict[str, Any] = Field(default_factory=dict)
    historical_precedent: dict[str, Any] = Field(default_factory=dict)
    target_candidates: list[RawEventCandidate] = Field(default_factory=list)
    recent_context_candidates: list[RawEventCandidate] = Field(default_factory=list)
    suppressed_events: list[SuppressedEventRecord] = Field(default_factory=list)
    periodic_filings: list[PeriodicFilingRef] = Field(default_factory=list)
    failed_accessions: list[FailedAccession] = Field(default_factory=list)
    processed_accessions: dict[str, str] = Field(default_factory=dict)
    open_gaps: list[str] = Field(default_factory=list)
    metrics: EventRunMetrics = Field(default_factory=EventRunMetrics)


class SecEventAnalysisResult(_FrozenSignalModel):
    """Output of Layer 1 + Layer 2 Bounded Event Agent."""

    cik: str
    entity_id: str
    symbol: str | None = None
    company_name: str | None = None
    as_of: date | None = None
    is_fpi: bool = False
    fpi_exempt_from_form4: bool = False
    scale_anchor: ScaleAnchor | None = None
    cluster_reasoning_chains: list[str] = Field(default_factory=list)
    signals: list[CompanySignalEnvelope] = Field(default_factory=list)
    suppressed_events: list[SuppressedEventRecord] = Field(default_factory=list)
    failed_accessions: list[FailedAccession] = Field(default_factory=list)
    processed_accessions: dict[str, str] = Field(default_factory=dict)
    open_gaps: list[str] = Field(default_factory=list)
    metrics: EventRunMetrics = Field(default_factory=EventRunMetrics)
