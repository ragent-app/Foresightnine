"""Scheme 2: Bounded Buy-Side US Earnings Research Squad Orchestrator (Phase A2).

Implements the 6-stage bounded coordinator-worker research squad with strict
Pydantic v2 contracts, 4 logical model slots, read-only scoped workers,
subtask-level checkpoint save/resume, Lead-directed mid-run steering,
server-side evidence/citation/anchor gating, immutable EarningsDossier locking
with on-demand Deep Report caching, and Organization-Graph event emission.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import importlib
import inspect
import json
import os
from pathlib import Path
import re
from threading import Event, RLock
from time import monotonic
from typing import Any, Callable, Iterable, Literal, Mapping, Protocol, Sequence, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .earnings_external_data import (
    CONSENSUS_FALLBACK_SUGGESTIONS,
    EarningsConsensus,
    EarningsTranscript,
    ExternalEarningsService,
    TRANSCRIPT_FALLBACK_SUGGESTIONS,
    redact_secrets,
)
from .fin_data.adapters.sec_deep import (
    DualPeriodEarningsRelease,
    FilingRedlineReport,
    FilingSectionsBundle,
    FilingTableCatalog,
    SecDeepAdapter,
    cik_scoped_efts_search,
    sec_earnings_release,
    sec_filing_sections,
    sec_filing_tables,
    sec_redline_diff,
)
from .fin_data.adapters.sec_edgar import SecEdgarAdapter, cik10, derive_period_metrics
from .fin_data.models import Filing
from .model_protocol import ModelToolCall, ModelToolDefinition, ModelUsage
from .phase3 import ProviderFailure
from .research_budget import ResearchExecutionBudget
from .research_coordinator import ResearchExecutionContext
from .research_earnings_diagnostics import (
    DiagnosticFlag,
    EarningsQualityDiagnosticsResult,
    EarningsQualityRequest,
    GuidanceAndNonGaapRequest,
    GuidanceAndNonGaapResult,
    GuidanceMetricInput,
    NonGaapBridgeInput,
    PeriodFinancials,
    SectorApplicability,
    ValuationAndExpectationsResult,
    ValuationRequest,
    earnings_quality_diagnostics,
    evaluate_sec_sic_gate,
    guidance_and_nongaap_reconciler,
    period_financials_from_statement_lines,
    valuation_and_implied_expectations,
)
from .research_tools_registry import TOOL_REGISTRY

ValuationAndImpliedExpectationsResult = ValuationAndExpectationsResult

ModelSlot = Literal["fast_extractor", "deep_reasoner", "bear_reviewer", "writer"]
MODEL_SLOTS: tuple[ModelSlot, ...] = ("fast_extractor", "deep_reasoner", "bear_reviewer", "writer")

WorkerRole = Literal["worker_segments", "worker_forensics", "worker_guidance"]
WORKER_ROLES: tuple[WorkerRole, ...] = ("worker_segments", "worker_forensics", "worker_guidance")

BearAnchorKind = Literal["flag", "redline", "guidance_gap", "valuation_gap"]
OrgActionKind = Literal[
    "READING",
    "SEARCHING",
    "INSPECTING",
    "DELEGATING",
    "WAITING_SUBORDINATES",
    "RESPONDING",
    "NEEDS_USER",
    "BLOCKED",
    "FAILED",
]
OrgRuntimeState = Literal[
    "idle",
    "running",
    "waiting",
    "completed",
    "failed",
    "blocked",
    "cancelled",
    "skipped",
]

# Strict per-worker read-only tool subsets. Write, submission, and unscoped web tools
# are permanently excluded from all worker roles.
WORKER_ALLOWED_TOOLS: dict[WorkerRole, frozenset[str]] = {
    "worker_segments": frozenset({
        "sec_filing_tables",
        "sec_earnings_release",
        "sec_filing_sections",
        "guidance_and_nongaap_reconciler",
        "read_sources",
        "search_source",
        "outline_source",
    }),
    "worker_forensics": frozenset({
        "sec_filing_tables",
        "sec_filing_sections",
        "sec_redline_diff",
        "sec_fulltext_search",
        "earnings_quality_diagnostics",
        "read_sources",
        "search_source",
        "outline_source",
    }),
    "worker_guidance": frozenset({
        "sec_earnings_release",
        "earnings_consensus",
        "earnings_transcript",
        "guidance_and_nongaap_reconciler",
        "valuation_and_implied_expectations",
        "read_sources",
        "search_source",
        "outline_source",
    }),
}

DISALLOWED_WORKER_TOOLS: frozenset[str] = frozenset({
    "submit_research",
    "acquire_sources",
    "news_search",
    "activate_tool_group",
    "activate_method_pack",
    "request_user_clarification",
    "checkpoint_question_part",
    "checkpoint_report_draft",
    "plan_research_requirements",
    "record_facts",
    "calculate_financials",
    "create_chart",
    "create_table",
})

TOOL_TO_ORG_ACTION: dict[str, OrgActionKind] = {
    "sec_fulltext_search": "SEARCHING",
    "search_source": "SEARCHING",
    "search_library": "SEARCHING",
    "sec_earnings_release": "READING",
    "sec_filing_tables": "READING",
    "sec_filing_sections": "READING",
    "earnings_consensus": "READING",
    "earnings_transcript": "READING",
    "read_sources": "READING",
    "outline_source": "READING",
    "read_pages": "READING",
    "sec_redline_diff": "INSPECTING",
    "earnings_quality_diagnostics": "INSPECTING",
    "guidance_and_nongaap_reconciler": "INSPECTING",
    "valuation_and_implied_expectations": "INSPECTING",
}

_NODE_META: dict[str, tuple[str, str, str | None]] = {
    "prefetch_engine": ("prefetch", "Stage 0 Prefetch", None),
    "lead_coordinator": ("lead_coordinator", "Lead Coordinator", None),
    "worker_segments": ("worker_segments", "Segments & KPIs", "lead_coordinator"),
    "worker_forensics": ("worker_forensics", "Quality Forensics", "lead_coordinator"),
    "worker_guidance": ("worker_guidance", "Guidance & Transcript", "lead_coordinator"),
    "bear_reviewer": ("bear_reviewer", "Bear Reviewer", "lead_coordinator"),
    "dossier_writer": ("writer", "Dossier Writer", "lead_coordinator"),
}

_GENERIC_BEAR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bmacro(?:economic)?\s+(?:headwinds|uncertainty|slowdown|environment|risks?)\b", re.I),
    re.compile(r"\bcompetition\s+(?:may|could|might)\s+(?:intensify|increase|pressure)\b", re.I),
    re.compile(r"\bmarket\s+(?:volatility|conditions|sentiment)\s+(?:may|could|might)\b", re.I),
    re.compile(r"\bvaluation\s+(?:is|looks|appears)\s+(?:expensive|high|stretched)\b", re.I),
    re.compile(r"\bexecution\s+(?:risk|challenges)\b", re.I),
)
_CONCRETE_SPECIFICITY_RE = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:%|bps|pp|x|\$|billion|million|days|q[1-4]|fy\d{2,4})|"
    r"\b(?:dso|dio|dpo|ccc|sbc|fcf|ocf|rpo|crpo|nrr|arr|arpu|tac|sssg|loe|nim|cet1|ffo|affo|"
    r"item\s+1a|item\s+[27]|ex-?99\.1|redline|flag_|guidance|consensus|reverse-dcf|wacc)\b)",
    re.I,
)

# Deterministic fallback SIC -> L2 pack map used ONLY if app.research_methods cannot be imported.
# Kept aligned with app.research_methods.SIC_INDUSTRY_RANGES.
_FALLBACK_SIC_RANGES: tuple[tuple[int, int, str], ...] = (
    (7371, 7374, "kpi_saas_cloud"),
    (7376, 7379, "kpi_saas_cloud"),
    (3559, 3559, "kpi_semis_hardware"),
    (3570, 3579, "kpi_semis_hardware"),
    (3661, 3669, "kpi_semis_hardware"),
    (3670, 3679, "kpi_semis_hardware"),
    (3690, 3699, "kpi_semis_hardware"),
    (3825, 3829, "kpi_semis_hardware"),
    (4832, 4833, "kpi_internet_ads"),
    (4841, 4841, "kpi_internet_ads"),
    (4899, 4899, "kpi_internet_ads"),
    (7310, 7319, "kpi_internet_ads"),
    (7370, 7370, "kpi_internet_ads"),
    (7375, 7375, "kpi_internet_ads"),
    (7812, 7812, "kpi_internet_ads"),
    (7841, 7841, "kpi_internet_ads"),
    (2000, 2199, "kpi_consumer_retail"),
    (2300, 2399, "kpi_consumer_retail"),
    (2840, 2844, "kpi_consumer_retail"),
    (3021, 3021, "kpi_consumer_retail"),
    (3140, 3149, "kpi_consumer_retail"),
    (5200, 5999, "kpi_consumer_retail"),
    (2830, 2836, "kpi_pharma_biotech"),
    (3841, 3845, "kpi_pharma_biotech"),
    (3851, 3851, "kpi_pharma_biotech"),
    (8071, 8071, "kpi_pharma_biotech"),
    (8731, 8731, "kpi_pharma_biotech"),
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def _canonical_sha256(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(raw.encode("utf-8")).hexdigest()


def _clean_nonblank(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be blank.")
    return cleaned


def _norm_quote_text(text: str) -> str:
    folded = text.replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", folded).strip().casefold()


# --------------------------------------------------------------------------- Errors
class SquadExecutionError(RuntimeError):
    """Error raised when a squad stage encounters an unrecoverable failure."""

    def __init__(self, message: str, *, code: str = "squad_error", stage: str = "unknown", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.retryable = retryable


class SquadCancelledError(SquadExecutionError):
    """Raised when the run or worker was cancelled via cancellation signal."""

    def __init__(self, message: str = "Research squad execution was cancelled.", *, stage: str = "unknown") -> None:
        super().__init__(message, code="cancelled", stage=stage, retryable=False)


class SquadConfigurationError(SquadExecutionError):
    """Raised when model slots or capabilities fail validation."""

    def __init__(self, message: str, *, code: str = "invalid_configuration", stage: str = "init") -> None:
        super().__init__(message, code=code, stage=stage, retryable=False)


class SquadToolViolationError(SquadExecutionError):
    """Raised when a model attempts an unauthorized or disallowed tool call."""

    def __init__(self, message: str, *, stage: str = "unknown") -> None:
        super().__init__(message, code="tool_violation", stage=stage, retryable=False)


# --------------------------------------------------------------------------- Strict Contracts
class _FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class SquadGap(_FrozenContract):
    """Explicit unresolved gap or degradation record."""

    gap_id: str = Field(default_factory=lambda: _new_id("gap"), min_length=1, max_length=120)
    stage: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=1200)
    worker_role: WorkerRole | None = None
    source: str | None = Field(default=None, max_length=160)
    fallback: str | None = Field(default=None, max_length=500)
    is_blocking: bool = False


class PrefetchedSourceRef(_FrozenContract):
    """Metadata reference for an authoritative source persisted during Stage 0."""

    source_id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=500)
    kind: Literal["user_supplied", "official", "market_data"] = "official"
    retrieval_method: str = Field(min_length=1, max_length=64)
    content_sha256: str = Field(min_length=1, max_length=64)
    url: str | None = Field(default=None, max_length=1000)
    permissions: dict[str, bool] = Field(default_factory=lambda: {
        "store": True, "display": True, "model_input": True, "export": True,
    })
    total_characters: int = Field(default=0, ge=0)


class ConsensusSnapshotSummary(_FrozenContract):
    """Optional FMP consensus index captured at Stage 0."""

    status: Literal["available", "unavailable", "skipped"] = "unavailable"
    records: list[EarningsConsensus] = Field(default_factory=list)
    source_id: str | None = None
    provider: str | None = None
    permissions: dict[str, bool] = Field(default_factory=dict)
    unavailable_reason: str | None = None
    fallback_suggestions: list[str] = Field(default_factory=list)


class TranscriptIndexEntry(_FrozenContract):
    """Lightweight Stage 0 index of an earnings call transcript without bloating prompts."""

    status: Literal["available", "unavailable", "skipped"] = "unavailable"
    symbol: str | None = None
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None
    call_date: datetime | date | None = None
    sections: list[str] = Field(default_factory=list)
    speakers: list[str] = Field(default_factory=list)
    content_sha256: str | None = None
    source_id: str | None = None
    provider: str | None = None
    permissions: dict[str, bool] = Field(default_factory=dict)
    unavailable_reason: str | None = None
    fallback_suggestions: list[str] = Field(default_factory=list)


class EarningsPrefetchSnapshot(_FrozenContract):
    """Stage 0 immutable deterministic prefetch snapshot (0 model calls)."""

    snapshot_id: str = Field(default_factory=lambda: _new_id("snap"), min_length=1, max_length=120)
    entity_id: str = Field(min_length=1, max_length=120)
    symbol: str | None = Field(default=None, max_length=32)
    cik: str | None = Field(default=None, max_length=16)
    company_name: str | None = Field(default=None, max_length=300)
    sic_code: str | None = Field(default=None, max_length=8)
    sic_description: str | None = Field(default=None, max_length=200)
    period_label: str | None = Field(default=None, max_length=100)
    as_of: date | datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)

    sector_applicability: SectorApplicability
    statements: list[PeriodFinancials] = Field(default_factory=list)
    derived_metrics: dict[str, Any] = Field(default_factory=dict)
    diagnostics: EarningsQualityDiagnosticsResult | None = None
    flags: list[DiagnosticFlag] = Field(default_factory=list)
    earnings_release: DualPeriodEarningsRelease | None = None
    filing_accession: str | None = None
    prior_filing_accession: str | None = None
    filing_table_catalog: FilingTableCatalog | None = None
    redline_summary: FilingRedlineReport | None = None
    guidance_nongaap: GuidanceAndNonGaapResult | None = None
    valuation: ValuationAndExpectationsResult | None = None
    consensus_summary: ConsensusSnapshotSummary = Field(default_factory=ConsensusSnapshotSummary)
    transcript_index: TranscriptIndexEntry = Field(default_factory=TranscriptIndexEntry)
    prefetched_sources: list[PrefetchedSourceRef] = Field(default_factory=list)
    open_gaps: list[SquadGap] = Field(default_factory=list)
    snapshot_sha256: str = ""

    @model_validator(mode="after")
    def _populate_flags_and_hash(self) -> "EarningsPrefetchSnapshot":
        if not self.flags and self.diagnostics is not None and self.diagnostics.flags:
            object.__setattr__(self, "flags", list(self.diagnostics.flags))
        if not self.snapshot_sha256:
            payload = self.model_dump(mode="json", exclude={"snapshot_sha256", "created_at"})
            object.__setattr__(self, "snapshot_sha256", _canonical_sha256(payload))
        return self


class WorkerTask(_FrozenContract):
    """Bounded, read-only Stage 2/2.5 task dispatched by Lead Coordinator."""

    task_id: str = Field(default_factory=lambda: _new_id("task"), min_length=1, max_length=120)
    worker_role: WorkerRole
    status: Literal["planned", "skipped"] = "planned"
    skip_reason: str | None = Field(default=None, max_length=500)
    objective: str = Field(min_length=1, max_length=2000)
    allowed_tools: list[str] = Field(default_factory=list)
    soft_turn_limit: int = Field(default=4, ge=1, le=8)
    focus_items: list[str] = Field(default_factory=list)
    target_r_files: list[str] = Field(default_factory=list)
    target_flag_ids: list[str] = Field(default_factory=list)
    target_section_keys: list[str] = Field(default_factory=list)
    round_number: int = Field(default=1, ge=1, le=2)
    steering_instruction_sequence: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_scope_and_skip(self) -> "WorkerTask":
        allowed_pool = WORKER_ALLOWED_TOOLS[self.worker_role]
        if self.status == "skipped":
            if not self.skip_reason or not self.skip_reason.strip():
                raise ValueError("Skipped WorkerTask requires a non-blank skip_reason.")
        elif not self.allowed_tools:
            object.__setattr__(self, "allowed_tools", sorted(allowed_pool))
        bad_tools = [t for t in self.allowed_tools if t not in allowed_pool or t in DISALLOWED_WORKER_TOOLS]
        if bad_tools:
            raise ValueError(
                f"Worker {self.worker_role!r} cannot be granted tools {bad_tools}; "
                f"permitted subset is {sorted(allowed_pool)}."
            )
        deduped = list(dict.fromkeys(self.allowed_tools))
        if deduped != self.allowed_tools:
            object.__setattr__(self, "allowed_tools", deduped)
        return self


class ResearchDispatchPlan(_FrozenContract):
    """Stage 1 Lead Coordinator output specifying L2 pack and 1-3 worker tasks."""

    plan_id: str = Field(default_factory=lambda: _new_id("plan"), min_length=1, max_length=120)
    l1_pack_id: str = Field(default="earnings_review", min_length=1, max_length=100)
    selected_l2_pack_id: str | None = Field(default=None, max_length=100)
    selected_l2_pack_reason: str = Field(default="", max_length=600)
    tasks: list[WorkerTask] = Field(min_length=1, max_length=3)
    cross_worker_hypotheses: list[str] = Field(default_factory=list)
    planning_notes: str = Field(default="", max_length=1500)
    instruction_sequence: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _unique_roles(self) -> "ResearchDispatchPlan":
        roles = [t.worker_role for t in self.tasks]
        if len(set(roles)) != len(roles):
            raise ValueError("ResearchDispatchPlan tasks must target distinct worker_role values.")
        return self


class EvidenceCitation(_FrozenContract):
    """Source-anchored citation attached to an EvidenceCard."""

    source_id: str = Field(min_length=1, max_length=160)
    quotation: str = Field(min_length=1, max_length=2500)
    location: str | None = Field(default=None, max_length=240)
    character_start: int | None = Field(default=None, ge=0)
    character_end: int | None = Field(default=None, ge=0)
    content_sha256: str | None = Field(default=None, max_length=64)
    fact_id: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _valid_span(self) -> "EvidenceCitation":
        if (
            self.character_start is not None
            and self.character_end is not None
            and self.character_end < self.character_start
        ):
            raise ValueError("character_end must be >= character_start.")
        return self


class EvidenceCard(_FrozenContract):
    """Structured evidence output emitted by read-only Stage 2/2.5 workers."""

    card_id: str = Field(min_length=1, max_length=160)
    worker_role: WorkerRole
    topic_tag: str = Field(min_length=1, max_length=120)
    finding: str = Field(min_length=1, max_length=4000)
    supporting_fact_ids: list[str] = Field(default_factory=list)
    citations: list[EvidenceCitation] = Field(default_factory=list)
    caveat: str | None = Field(default=None, max_length=1500)
    unresolved_gaps: list[str] = Field(default_factory=list)
    numeric_claims: list[dict[str, Any]] = Field(default_factory=list)
    round_number: int = Field(default=1, ge=1, le=2)
    instruction_sequence: int = Field(default=0, ge=0)


class EvidenceCardBatch(_FrozenContract):
    """Per-worker batch of EvidenceCards and execution telemetry."""

    batch_id: str = Field(default_factory=lambda: _new_id("batch"), min_length=1, max_length=120)
    task_id: str = Field(min_length=1, max_length=120)
    worker_role: WorkerRole
    round_number: int = Field(default=1, ge=1, le=2)
    status: Literal["completed", "partial", "failed", "skipped", "cancelled"] = "completed"
    cards: list[EvidenceCard] = Field(default_factory=list)
    unresolved_gaps: list[str] = Field(default_factory=list)
    tools_invoked: list[str] = Field(default_factory=list)
    blocked_tool_attempts: list[str] = Field(default_factory=list)
    tool_calls_made: int = Field(default=0, ge=0)
    turns_used: int = Field(default=0, ge=0)
    error: str | None = None
    instruction_sequence: int = Field(default=0, ge=0)


class Stage25ReviewDecision(_FrozenContract):
    """Stage 2.5 Lead cross-worker review decision (hard-capped at 1 follow-up round)."""

    decision_id: str = Field(default_factory=lambda: _new_id("s25"), min_length=1, max_length=120)
    has_cross_worker_leads: bool = False
    rationale: str = Field(default="", max_length=1500)
    followup_tasks: list[WorkerTask] = Field(default_factory=list, max_length=3)
    resolved_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_round_two(self) -> "Stage25ReviewDecision":
        normalized: list[WorkerTask] = []
        seen_roles: set[WorkerRole] = set()
        for task in self.followup_tasks:
            if task.worker_role in seen_roles:
                raise ValueError("Stage 2.5 followup_tasks must target distinct worker_role values.")
            seen_roles.add(task.worker_role)
            if task.round_number != 2:
                normalized.append(task.model_copy(update={"round_number": 2}))
            else:
                normalized.append(task)
        if normalized != self.followup_tasks:
            object.__setattr__(self, "followup_tasks", normalized)
        return self


class BearCaseItem(_FrozenContract):
    """Evidence-anchored adversarial bear challenge with next-quarter invalidation trigger."""

    bear_id: str = Field(default_factory=lambda: _new_id("bear"), min_length=1, max_length=160)
    thesis_challenge: str = Field(min_length=1, max_length=3000)
    anchored_evidence_ids: list[str] = Field(min_length=1)
    anchor_kind: BearAnchorKind
    next_quarter_invalidation_trigger: str = Field(min_length=1, max_length=1500)
    supporting_numbers: list[str] = Field(default_factory=list)
    severity: Literal["high", "medium", "low"] = "medium"

    @field_validator("anchored_evidence_ids")
    @classmethod
    def _nonblank_anchors(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if not cleaned:
            raise ValueError("anchored_evidence_ids must contain at least one non-blank evidence anchor ID.")
        return list(dict.fromkeys(cleaned))


class RejectedBearCase(_FrozenContract):
    """Generic or unanchored bear case rejected by the server-side Stage 3A/3B anchor gate."""

    bear_id: str = ""
    thesis_challenge: str = ""
    anchored_evidence_ids: list[str] = Field(default_factory=list)
    anchor_kind: str | None = None
    next_quarter_invalidation_trigger: str | None = None
    rejection_reason: str


class BearCaseBatch(_FrozenContract):
    """Stage 3A Adversarial Bear Reviewer output."""

    batch_id: str = Field(default_factory=lambda: _new_id("bearbatch"), min_length=1, max_length=120)
    items: list[BearCaseItem] = Field(default_factory=list)
    rejected_items: list[RejectedBearCase] = Field(default_factory=list)
    reviewer_notes: str = Field(default="", max_length=2000)


class DossierGateReport(_FrozenContract):
    """Audit record from Stage 3B server-side citation, anchor, and export-permission gate."""

    passed: bool
    verified_card_ids: list[str] = Field(default_factory=list)
    dropped_card_ids: list[str] = Field(default_factory=list)
    unverified_citations: list[dict[str, str]] = Field(default_factory=list)
    verified_bear_ids: list[str] = Field(default_factory=list)
    rejected_bear_ids: list[str] = Field(default_factory=list)
    exportable: bool = True
    non_exportable_sources: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class SlotUsageRecord(_FrozenContract):
    """Per-slot call and token usage accounting (no secret credentials)."""

    slot: ModelSlot
    model: str | None = None
    protocol: str | None = None
    fallback_used: bool = True
    calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    failed_calls: int = Field(default=0, ge=0)
    elapsed_ms: int = Field(default=0, ge=0)


class SlotUsageSummary(_FrozenContract):
    """Aggregated slot usage summary attached to EarningsDossier."""

    slots: dict[ModelSlot, SlotUsageRecord]
    total_calls: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    stage_0_model_calls: int = Field(default=0, ge=0)
    deep_report_tool_calls: int = Field(default=0, ge=0)


class EarningsDossier(_FrozenContract):
    """Locked, immutable server-gated earnings research dossier (Stage 3B)."""

    dossier_id: str = Field(default_factory=lambda: _new_id("dossier"), min_length=1, max_length=120)
    run_id: str = Field(min_length=1, max_length=120)
    entity_id: str = Field(min_length=1, max_length=120)
    symbol: str | None = None
    cik: str | None = None
    company_name: str | None = None
    period_label: str | None = None
    selected_l2_pack_id: str | None = None
    snapshot: EarningsPrefetchSnapshot
    dispatch_plan: ResearchDispatchPlan | None = None
    evidence_cards: list[EvidenceCard] = Field(default_factory=list)
    bear_cases: list[BearCaseItem] = Field(default_factory=list)
    rejected_bear_cases: list[RejectedBearCase] = Field(default_factory=list)
    open_gaps: list[SquadGap] = Field(default_factory=list)
    sector_applicability: SectorApplicability
    gate_report: DossierGateReport
    memo_markdown: str
    deep_report_markdown: str | None = None
    slot_usage_summary: SlotUsageSummary
    locked: bool = True
    dossier_sha256: str = ""
    created_at: datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def _seal_dossier_digest(self) -> "EarningsDossier":
        if not self.dossier_sha256:
            canonical_core = {
                "dossier_id": self.dossier_id,
                "entity_id": self.entity_id,
                "snapshot_sha256": self.snapshot.snapshot_sha256,
                "evidence_cards": [c.model_dump(mode="json") for c in self.evidence_cards],
                "bear_cases": [b.model_dump(mode="json") for b in self.bear_cases],
                "open_gaps": [g.model_dump(mode="json") for g in self.open_gaps],
                "sector_applicability": self.sector_applicability.model_dump(mode="json"),
            }
            object.__setattr__(self, "dossier_sha256", _canonical_sha256(canonical_core))
        return self


# --------------------------------------------------------------------------- Steering & Org Graph Contracts
class UserSteeringInstruction(_FrozenContract):
    """Mid-run user instruction routed to Lead Coordinator."""

    instruction_id: str = Field(default_factory=lambda: _new_id("instr"), min_length=1, max_length=120)
    sequence: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=4000)
    created_at: datetime = Field(default_factory=_utcnow)


class SteeringImpactDecision(_FrozenContract):
    """Lead Coordinator incremental impact analysis for mid-run steering."""

    decision_id: str = Field(default_factory=lambda: _new_id("steer"), min_length=1, max_length=120)
    instruction_sequences: list[int] = Field(default_factory=list)
    affected_workers: list[WorkerRole] = Field(default_factory=list)
    cancel_running_workers: list[WorkerRole] = Field(default_factory=list)
    updated_tasks: list[WorkerTask] = Field(default_factory=list, max_length=3)
    invalidate_stage_2_5: bool = True
    invalidate_stage_3a: bool = True
    rationale: str = Field(default="", max_length=1500)

    @model_validator(mode="after")
    def _validate_affected_only(self) -> "SteeringImpactDecision":
        affected_set = set(self.affected_workers)
        for role in self.cancel_running_workers:
            if role not in affected_set:
                affected_set.add(role)
        for task in self.updated_tasks:
            if task.worker_role not in affected_set:
                raise ValueError(
                    f"Steering updated_tasks includes {task.worker_role!r} which is not in affected_workers."
                )
        dedup_affected = list(dict.fromkeys([*self.affected_workers, *self.cancel_running_workers]))
        if dedup_affected != self.affected_workers:
            object.__setattr__(self, "affected_workers", dedup_affected)
        return self


class OrgEdgeEvent(_FrozenContract):
    """Directed organization graph edge event (delegation vs review)."""

    from_node_id: str = Field(min_length=1, max_length=120)
    to_node_id: str = Field(min_length=1, max_length=120)
    is_review: bool = False
    edge_kind: Literal["dispatch", "report", "review", "steering"] = "dispatch"
    label: str | None = Field(default=None, max_length=240)


class OrgGraphEvent(_FrozenContract):
    """Sequenced Organization-Graph event aligned with OrganizationUiState.kt."""

    sequence: int = Field(ge=1)
    timestamp: datetime = Field(default_factory=_utcnow)
    stage: str = Field(min_length=1, max_length=64)
    node_id: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    runtime_state: OrgRuntimeState
    action_kind: OrgActionKind
    current_activity: str = Field(min_length=1, max_length=400)
    blocker_text: str | None = Field(default=None, max_length=500)
    edge_event: OrgEdgeEvent | None = None
    slot: ModelSlot | None = None
    parent_node_id: str | None = None


class SquadCheckpoint(_FrozenContract):
    """Subtask-level persistent checkpoint enabling lossless skip-and-resume."""

    checkpoint_version: str = "a2_squad_v1"
    run_id: str = Field(min_length=1, max_length=120)
    question: str = ""
    entity_id: str | None = None
    symbol: str | None = None
    cik: str | None = None
    completed_stages: list[str] = Field(default_factory=list)
    stage_status: dict[str, str] = Field(default_factory=dict)
    snapshot: EarningsPrefetchSnapshot | None = None
    dispatch_plan: ResearchDispatchPlan | None = None
    worker_batches: dict[str, EvidenceCardBatch] = Field(default_factory=dict)
    stage25_decision: Stage25ReviewDecision | None = None
    stage25_executed: bool = False
    bear_batch: BearCaseBatch | None = None
    dossier: EarningsDossier | None = None
    open_gaps: list[SquadGap] = Field(default_factory=list)
    source_texts: dict[str, str] = Field(default_factory=dict)
    source_permissions: dict[str, dict[str, bool]] = Field(default_factory=dict)
    user_instructions: list[UserSteeringInstruction] = Field(default_factory=list)
    applied_instruction_sequences: list[int] = Field(default_factory=list)
    slot_usage: dict[ModelSlot, SlotUsageRecord] = Field(default_factory=dict)
    org_events: list[OrgGraphEvent] = Field(default_factory=list)
    last_sequence: int = 0
    updated_at: datetime = Field(default_factory=_utcnow)


# --------------------------------------------------------------------------- Checkpoint Store
@runtime_checkable
class SquadCheckpointStore(Protocol):
    def save(self, checkpoint: SquadCheckpoint) -> None: ...
    def load(self, run_id: str) -> SquadCheckpoint | None: ...


class InMemorySquadCheckpointStore:
    """Thread-safe in-memory checkpoint repository."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._by_run: dict[str, str] = {}
        self.save_history: list[SquadCheckpoint] = []

    def save(self, checkpoint: SquadCheckpoint) -> None:
        raw = checkpoint.model_dump_json()
        with self._lock:
            self._by_run[checkpoint.run_id] = raw
            self.save_history.append(checkpoint)

    def load(self, run_id: str) -> SquadCheckpoint | None:
        with self._lock:
            raw = self._by_run.get(run_id)
            if raw is None:
                return None
            return SquadCheckpoint.model_validate_json(raw)


class JsonFileSquadCheckpointStore:
    """Thread-safe atomic JSON file checkpoint store keyed by run_id."""

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def _path_for(self, run_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", run_id)
        return self._dir / f"{safe}.json"

    def save(self, checkpoint: SquadCheckpoint) -> None:
        target = self._path_for(checkpoint.run_id)
        tmp = target.with_suffix(".tmp")
        payload = checkpoint.model_dump_json(indent=2)
        with self._lock:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, target)

    def load(self, run_id: str) -> SquadCheckpoint | None:
        target = self._path_for(run_id)
        with self._lock:
            if not target.exists():
                return None
            return SquadCheckpoint.model_validate_json(target.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- Model Slot Resolution & Method Packs
class SlotBinding(_FrozenContract):
    """Protocol-first resolved binding for one logical model slot (zero raw secrets)."""

    slot: ModelSlot
    model: str | None = None
    endpoint: str | None = None
    protocol: str | None = None
    native_tools: bool = True
    fallback_used: bool = True
    credential_ref: str | None = None


def _is_full_slot_binding_input(obj: Any) -> bool:
    if obj is None:
        return False
    if isinstance(obj, Mapping):
        return any(k in obj for k in ("protocol", "base_url", "deployment_ref"))
    return True


def _coerce_slot_overrides_map(slot_overrides: Any) -> dict[str, Any]:
    if slot_overrides is None:
        return {}
    if isinstance(slot_overrides, Mapping):
        return {str(k): v for k, v in slot_overrides.items() if v is not None}
    if callable(getattr(slot_overrides, "as_dict", None)):
        return {str(k): v for k, v in slot_overrides.as_dict().items() if v is not None}
    out: dict[str, Any] = {}
    for slot in MODEL_SLOTS:
        val = getattr(slot_overrides, slot, None)
        if val is not None:
            out[slot] = val
    return out


def resolve_squad_slots(
    *,
    primary_profile: Mapping[str, Any] | Any | None = None,
    slot_overrides: Mapping[str, Any] | Any | None = None,
    resolved_slots: Any = None,
) -> dict[ModelSlot, SlotBinding]:
    """Resolve the 4 logical model slots with zero-config fallback and capability check.

    Integrates natively with ``research_model_slots.resolve_model_slots`` /
    ``ResolvedModelSlots`` when full wire bindings are provided, while also supporting
    lightweight dict profiles and Pydantic ``ModelSlotConfig`` objects.
    """
    primary_model = _read_attr_or_key(primary_profile, "model") or "primary_default"
    primary_endpoint = _read_attr_or_key(primary_profile, "endpoint") or _read_attr_or_key(primary_profile, "base_url")
    primary_protocol = _read_attr_or_key(primary_profile, "protocol")
    primary_native = _read_attr_or_key(primary_profile, "native_tools")
    if primary_native is None:
        caps_obj = _read_attr_or_key(primary_profile, "capabilities")
        if caps_obj is not None:
            primary_native = _read_attr_or_key(caps_obj, "native_tools")
    if primary_native is None:
        primary_native = True
    primary_cred_ref = _read_attr_or_key(primary_profile, "credential_ref")

    overrides_map = _coerce_slot_overrides_map(slot_overrides)
    full_primary = _is_full_slot_binding_input(primary_profile)
    has_full_override = any(_is_full_slot_binding_input(v) for v in overrides_map.values())

    # Use research_model_slots when full wire bindings are provided on primary or slot overrides.
    if resolved_slots is None and (full_primary or has_full_override):
        try:
            rms = importlib.import_module("app.research_model_slots")
            resolver = getattr(rms, "resolve_model_slots", None)
            err_cls = getattr(rms, "ModelSlotError", Exception)
            if callable(resolver):
                if full_primary:
                    try:
                        resolved_slots = resolver(primary_profile, slot_overrides)
                    except err_cls as exc:
                        raise SquadConfigurationError(
                            str(exc),
                            code=getattr(exc, "code", "model_slot_capability_invalid"),
                        ) from exc
                else:
                    # Lightweight primary (e.g. dev/test or platform fallback) paired with full
                    # slot overrides: resolve & validate every explicitly configured slot strictly
                    # while keeping unset slots ready to inherit the lightweight primary profile.
                    try:
                        partial_resolved = resolver(None, slot_overrides, strict=False)
                    except err_cls as exc:
                        raise SquadConfigurationError(
                            str(exc),
                            code=getattr(exc, "code", "model_slot_capability_invalid"),
                        ) from exc
                    override_errors: list[str] = []
                    override_raw: dict[ModelSlot, Any] = {}
                    for slot_name in MODEL_SLOTS:
                        if slot_name not in overrides_map:
                            continue
                        slot_res = partial_resolved.slot_for(slot_name)
                        if not getattr(slot_res, "capability_valid", True):
                            override_errors.extend(getattr(slot_res, "validation_errors", ()) or ())
                        override_raw[slot_name] = slot_res
                    if override_errors:
                        raise SquadConfigurationError(
                            override_errors[0],
                            code="model_slot_capability_invalid",
                        )
                    resolved_slots = override_raw
        except ImportError:
            pass

    if resolved_slots is not None and getattr(resolved_slots, "is_valid", True) is False:
        errs = getattr(resolved_slots, "errors", ()) or ("Model slot capability validation failed.",)
        raise SquadConfigurationError(str(errs[0]), code="model_slot_capability_invalid")

    bindings: dict[ModelSlot, SlotBinding] = {}
    raw_slots = None
    if resolved_slots is not None:
        if callable(getattr(resolved_slots, "slots_dict", None)):
            raw_slots = resolved_slots.slots_dict()
        else:
            raw_slots = getattr(resolved_slots, "slots", None)
            if raw_slots is None and isinstance(resolved_slots, Mapping):
                raw_slots = resolved_slots

    for slot in MODEL_SLOTS:
        candidate = None
        if isinstance(raw_slots, Mapping) and slot in raw_slots:
            candidate = raw_slots[slot]
        elif resolved_slots is not None and callable(getattr(resolved_slots, "slot_for", None)):
            candidate = resolved_slots.slot_for(slot)
        elif resolved_slots is not None and callable(getattr(resolved_slots, "for_slot", None)):
            candidate = resolved_slots.for_slot(slot)
        elif slot in overrides_map:
            candidate = overrides_map[slot]

        if candidate is None:
            bindings[slot] = SlotBinding(
                slot=slot,
                model=primary_model,
                endpoint=primary_endpoint,
                protocol=primary_protocol,
                native_tools=bool(primary_native),
                fallback_used=True,
                credential_ref=primary_cred_ref,
            )
            continue

        native_val = _read_attr_or_key(candidate, "native_tools_supported")
        if native_val is None:
            native_val = _read_attr_or_key(candidate, "native_tools")
        caps = _read_attr_or_key(candidate, "capabilities")
        if native_val is None and caps is not None:
            native_val = _read_attr_or_key(caps, "native_tools")
        if native_val is None:
            native_val = bool(primary_native)
        fb = _read_attr_or_key(candidate, "fallback_used")
        if fb is None:
            fb = False
        bindings[slot] = SlotBinding(
            slot=slot,
            model=_read_attr_or_key(candidate, "model") or primary_model,
            endpoint=(
                _read_attr_or_key(candidate, "endpoint")
                or _read_attr_or_key(candidate, "base_url")
                or primary_endpoint
            ),
            protocol=_read_attr_or_key(candidate, "protocol") or primary_protocol,
            native_tools=bool(native_val),
            fallback_used=bool(fb),
            credential_ref=_read_attr_or_key(candidate, "credential_ref") or primary_cred_ref,
        )

    if not bindings["fast_extractor"].native_tools:
        raise SquadConfigurationError(
            "Model slot 'fast_extractor' must support native tool calling for Stage 2 workers.",
            code="fast_extractor_native_tools_required",
        )
    return bindings


def _read_attr_or_key(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def resolve_method_pack_context(
    sic: str | int | None,
    *,
    preferred_pack_id: str | None = None,
) -> dict[str, Any]:
    """Resolve L1 earnings backbone + L2 non-financial industry KPI pack deterministically."""
    sector = evaluate_sec_sic_gate(sic)
    if sector.is_gated:
        return {
            "l1_pack_id": "earnings_review",
            "l2_pack_id": None,
            "reason": f"Sector gated ({sector.sector_category}): {sector.explanation}",
            "recommended_alternatives": list(sector.recommended_alternatives),
            "composed_pack": None,
        }

    matched_l2: str | None = preferred_pack_id
    composed: Any = None
    rm_loaded = False
    try:
        rm = importlib.import_module("app.research_methods")
        rm_loaded = True
        if matched_l2 is None:
            matcher = getattr(rm, "match_industry_pack_by_sic", None)
            if callable(matcher):
                matched_l2 = matcher(sic)
        composer = getattr(rm, "compose_earnings_method_pack", None)
        if callable(composer):
            composed = composer(sic=sic, industry_pack_id=matched_l2)
    except Exception:
        composed = None

    if not rm_loaded and matched_l2 is None and sector.sic_code is not None:
        sic_num = int(sector.sic_code)
        for lo, hi, pack_id in _FALLBACK_SIC_RANGES:
            if lo <= sic_num <= hi:
                matched_l2 = pack_id
                break

    reason = (
        f"Matched L2 industry pack {matched_l2} for SIC {sector.sic_code}."
        if matched_l2
        else f"No specific L2 KPI pack matched SIC {sector.sic_code or 'unknown'}; using L1 earnings_review backbone."
    )
    return {
        "l1_pack_id": "earnings_review",
        "l2_pack_id": matched_l2,
        "reason": reason,
        "recommended_alternatives": [],
        "composed_pack": composed,
    }


# --------------------------------------------------------------------------- Org Graph Emitter
class OrgGraphEmitter:
    """Thread-safe strictly-sequenced Organization-Graph event emitter."""

    def __init__(
        self,
        *,
        sinks: Sequence[Callable[[OrgGraphEvent], None]] = (),
        initial_events: Sequence[OrgGraphEvent] = (),
        start_sequence: int = 0,
    ) -> None:
        self._lock = RLock()
        self._sinks: list[Callable[[OrgGraphEvent], None]] = list(sinks)
        self._events: list[OrgGraphEvent] = list(initial_events)
        max_existing = max((e.sequence for e in self._events), default=0)
        self._sequence = max(start_sequence, max_existing)

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    @property
    def events(self) -> list[OrgGraphEvent]:
        with self._lock:
            return list(self._events)

    def add_sink(self, sink: Callable[[OrgGraphEvent], None]) -> None:
        with self._lock:
            self._sinks.append(sink)

    def emit(
        self,
        *,
        stage: str,
        node_id: str,
        action_kind: OrgActionKind,
        runtime_state: OrgRuntimeState,
        current_activity: str,
        blocker_text: str | None = None,
        edge_event: OrgEdgeEvent | None = None,
        slot: ModelSlot | None = None,
        role: str | None = None,
        display_name: str | None = None,
        parent_node_id: str | None = None,
    ) -> OrgGraphEvent:
        meta_role, meta_display, meta_parent = _NODE_META.get(node_id, (node_id, node_id, None))
        with self._lock:
            self._sequence += 1
            ev = OrgGraphEvent(
                sequence=self._sequence,
                timestamp=_utcnow(),
                stage=stage,
                node_id=node_id,
                role=role or meta_role,
                display_name=display_name or meta_display,
                runtime_state=runtime_state,
                action_kind=action_kind,
                current_activity=current_activity[:400],
                blocker_text=blocker_text[:500] if blocker_text else None,
                edge_event=edge_event,
                slot=slot,
                parent_node_id=parent_node_id if parent_node_id is not None else meta_parent,
            )
            self._events.append(ev)
            sinks_copy = list(self._sinks)
        for sink in sinks_copy:
            sink(ev)
        return ev


# --------------------------------------------------------------------------- Thread-safe Shared Ledger
class SquadExecutionLedger:
    """Thread-safe shared budget, slot usage, tool-call accounting, and cancellation controller."""

    def __init__(
        self,
        *,
        slot_bindings: Mapping[ModelSlot, SlotBinding],
        context: ResearchExecutionContext | None = None,
        budget: ResearchExecutionBudget | None = None,
        cancel_event: Event | None = None,
        initial_usage: Mapping[ModelSlot, SlotUsageRecord] | None = None,
    ) -> None:
        self._lock = RLock()
        self._bindings = dict(slot_bindings)
        self._context = context
        self._budget = budget or (context.budget if context is not None else ResearchExecutionBudget(None, None, None, float("inf")))
        self._cancel_event = cancel_event or Event()
        self._usage: dict[ModelSlot, dict[str, Any]] = {}
        for slot in MODEL_SLOTS:
            binding = self._bindings[slot]
            prior = (initial_usage or {}).get(slot)
            self._usage[slot] = {
                "slot": slot,
                "model": binding.model,
                "protocol": binding.protocol,
                "fallback_used": binding.fallback_used,
                "calls": prior.calls if prior else 0,
                "input_tokens": prior.input_tokens if prior else 0,
                "output_tokens": prior.output_tokens if prior else 0,
                "total_tokens": prior.total_tokens if prior else 0,
                "failed_calls": prior.failed_calls if prior else 0,
                "elapsed_ms": prior.elapsed_ms if prior else 0,
            }
        self._stage_0_model_calls = 0
        self._deep_report_tool_calls = 0
        self._tool_calls_total = 0

    @property
    def cancel_event(self) -> Event:
        return self._cancel_event

    def cancel(self) -> None:
        self._cancel_event.set()

    def ensure_running(self, *, stage: str = "unknown") -> None:
        if self._cancel_event.is_set():
            raise SquadCancelledError(stage=stage)
        if self._context is not None:
            ctx_cancel = self._context.cancel_event()
            if ctx_cancel is not None and hasattr(ctx_cancel, "is_set") and ctx_cancel.is_set():
                self._cancel_event.set()
                raise SquadCancelledError(stage=stage)
            try:
                self._context.ensure_running()
            except Exception as exc:
                raise SquadCancelledError(str(exc), stage=stage) from exc
        if self._now() >= self._budget.deadline:
            raise SquadExecutionError("Research time budget exhausted.", code="time_budget", stage=stage)

    def _now(self) -> float:
        if self._context is not None:
            return self._context.now()
        return monotonic()

    def admit_model_call(self, slot: ModelSlot, *, stage: str, estimated_tokens: int | None = None) -> tuple[str, tuple[int, int | None, int | None] | None]:
        with self._lock:
            self.ensure_running(stage=stage)
            if stage == "stage_0_prefetch":
                raise SquadExecutionError(
                    "Stage 0 deterministic prefetch must make zero model calls.",
                    code="stage_0_model_call_forbidden",
                    stage=stage,
                )
            call_id = _new_id(f"call_{slot}")
            if self._context is not None:
                before = self._context.start_model(call_id, estimated_tokens)
                return call_id, before
            total_calls = sum(item["calls"] for item in self._usage.values())
            total_tokens = sum(item["total_tokens"] for item in self._usage.values())
            reason = self._budget.model_exhaustion(
                calls=total_calls,
                usage=total_tokens,
                per_call=estimated_tokens,
                now=self._now(),
            )
            if reason is not None:
                raise SquadExecutionError(
                    f"Model budget exhausted ({reason}).",
                    code="provider_budget_exhausted",
                    stage=stage,
                )
            return call_id, None

    def settle_model_call(
        self,
        slot: ModelSlot,
        call_id: str,
        before: tuple[int, int | None, int | None] | None,
        *,
        usage: ModelUsage | None,
        elapsed_ms: int,
        failed: bool = False,
    ) -> None:
        with self._lock:
            rec = self._usage[slot]
            rec["calls"] += 1
            rec["elapsed_ms"] += max(0, elapsed_ms)
            if failed:
                rec["failed_calls"] += 1
            inp = usage.input_tokens if usage and type(usage.input_tokens) is int and usage.input_tokens >= 0 else 0
            out = usage.output_tokens if usage and type(usage.output_tokens) is int and usage.output_tokens >= 0 else 0
            tot = usage.total_tokens if usage and type(usage.total_tokens) is int and usage.total_tokens >= 0 else (inp + out)
            rec["input_tokens"] += inp
            rec["output_tokens"] += out
            rec["total_tokens"] += tot
            if self._context is not None and before is not None:
                receipt = self._context.model_call_receipts.get(call_id)
                if receipt is not None and not failed:
                    receipt["provider"] = "returned"
                self._context.finish_model(call_id, tot if usage is not None else None, before)

    def record_tool_call(self, *, stage: str) -> None:
        with self._lock:
            if stage == "deep_report":
                self._deep_report_tool_calls += 1
                raise SquadToolViolationError(
                    "render_deep_report must execute zero retrieval tool calls.",
                    stage=stage,
                )
            self._tool_calls_total += 1

    def slot_usage_snapshot(self) -> dict[ModelSlot, SlotUsageRecord]:
        with self._lock:
            return {slot: SlotUsageRecord(**self._usage[slot]) for slot in MODEL_SLOTS}

    def summary(self) -> SlotUsageSummary:
        with self._lock:
            records = self.slot_usage_snapshot()
            return SlotUsageSummary(
                slots=records,
                total_calls=sum(r.calls for r in records.values()),
                total_tokens=sum(r.total_tokens for r in records.values()),
                stage_0_model_calls=self._stage_0_model_calls,
                deep_report_tool_calls=self._deep_report_tool_calls,
            )


# --------------------------------------------------------------------------- Injectable Model & Tool Runners
class SlotModelRequest(_FrozenContract):
    """Structured prompt & tool envelope sent to a logical model slot runner."""

    slot: ModelSlot
    stage: str
    role: str
    system_prompt: str
    user_prompt: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    response_schema_name: str | None = None
    response_schema: dict[str, Any] | None = None
    binding: SlotBinding
    turn_index: int = 0


@dataclass(frozen=True)
class SlotModelResponse:
    """Normalized response returned by a SquadModelRunner."""

    parsed: Any = None
    content: str | None = None
    tool_calls: tuple[ModelToolCall, ...] = ()
    usage: ModelUsage | None = None
    finish_reason: str = "stop"


@runtime_checkable
class SquadModelRunner(Protocol):
    """Injectable protocol executing a single logical slot call."""

    def invoke(self, request: SlotModelRequest) -> SlotModelResponse: ...


class CallableSlotModelRunner:
    """Wraps a callable ``fn(SlotModelRequest) -> SlotModelResponse | dict | BaseModel | str``."""

    def __init__(self, fn: Callable[[SlotModelRequest], Any]) -> None:
        self._fn = fn

    def invoke(self, request: SlotModelRequest) -> SlotModelResponse:
        raw = self._fn(request)
        return _coerce_slot_response(raw)


class DictSlotModelRunner:
    """Routes each slot (or stage) to a dedicated callable for clean multi-slot testing."""

    def __init__(
        self,
        handlers: Mapping[str, Callable[[SlotModelRequest], Any]],
        *,
        default: Callable[[SlotModelRequest], Any] | None = None,
    ) -> None:
        self._handlers = dict(handlers)
        self._default = default

    def invoke(self, request: SlotModelRequest) -> SlotModelResponse:
        fn = self._handlers.get(request.role) or self._handlers.get(request.slot) or self._default
        if fn is None:
            raise SquadConfigurationError(
                f"No model handler configured for role={request.role!r} / slot={request.slot!r}.",
                stage=request.stage,
            )
        return _coerce_slot_response(fn(request))


def _coerce_slot_response(raw: Any) -> SlotModelResponse:
    if isinstance(raw, SlotModelResponse):
        return raw
    if isinstance(raw, BaseModel):
        return SlotModelResponse(parsed=raw, content=raw.model_dump_json())
    if isinstance(raw, Mapping):
        if "tool_calls" in raw or "parsed" in raw or "content" in raw:
            tcs = tuple(
                tc if isinstance(tc, ModelToolCall) else ModelToolCall(
                    id=str(tc.get("id") or _new_id("tc")),
                    name=str(tc["name"]),
                    arguments=tc.get("arguments") if isinstance(tc.get("arguments"), dict) else None,
                    raw_arguments=tc.get("raw_arguments") or (
                        tc.get("arguments") if isinstance(tc.get("arguments"), str) else None
                    ),
                )
                for tc in (raw.get("tool_calls") or ())
            )
            usage = raw.get("usage")
            if isinstance(usage, Mapping):
                usage = ModelUsage(
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    total_tokens=usage.get("total_tokens"),
                )
            return SlotModelResponse(
                parsed=raw.get("parsed"),
                content=raw.get("content"),
                tool_calls=tcs,
                usage=usage,
                finish_reason=str(raw.get("finish_reason") or ("tool_calls" if tcs else "stop")),
            )
        return SlotModelResponse(parsed=dict(raw), content=json.dumps(dict(raw), ensure_ascii=False, default=str))
    if isinstance(raw, str):
        return SlotModelResponse(content=raw)
    # Support standard ModelResponse objects from app.model_protocol
    if hasattr(raw, "tool_calls") and hasattr(raw, "message"):
        msg = getattr(raw, "message", {}) or {}
        return SlotModelResponse(
            content=msg.get("content") if isinstance(msg, Mapping) else None,
            tool_calls=tuple(getattr(raw, "tool_calls", ()) or ()),
            usage=getattr(raw, "usage", None),
            finish_reason=str(getattr(raw, "finish_reason", "stop") or "stop"),
        )
    raise SquadExecutionError(f"Unsupported model runner output type: {type(raw).__name__}")


@runtime_checkable
class SquadToolRunner(Protocol):
    """Injectable read-only tool executor used during Stage 0 and Stage 2/2.5."""

    def call_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        worker_role: WorkerRole | None = None,
    ) -> dict[str, Any]: ...


class FinToolboxToolRunner:
    """Adapter bridging ``FinDataToolbox`` + in-memory sources into ``SquadToolRunner``."""

    def __init__(
        self,
        toolbox: Any,
        *,
        entities: dict[str, dict[str, Any]] | None = None,
        source_by_id: dict[str, Any] | None = None,
    ) -> None:
        self._toolbox = toolbox
        self._entities = entities if entities is not None else {}
        self._source_by_id = source_by_id if source_by_id is not None else {}
        self._lock = RLock()

    @property
    def source_by_id(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._source_by_id)

    def call_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        worker_role: WorkerRole | None = None,
    ) -> dict[str, Any]:
        if tool_name in DISALLOWED_WORKER_TOOLS:
            raise SquadToolViolationError(f"Tool {tool_name!r} is disallowed in the research squad.")
        if worker_role is not None and tool_name not in WORKER_ALLOWED_TOOLS[worker_role]:
            raise SquadToolViolationError(
                f"Worker {worker_role!r} is not permitted to invoke {tool_name!r}."
            )
        if tool_name == "read_sources":
            return self._local_read_sources(arguments)
        if tool_name == "search_source":
            return self._local_search_source(arguments)
        if tool_name == "outline_source":
            return self._local_outline_source(arguments)

        spec = TOOL_REGISTRY.get(tool_name)
        if spec is None:
            return {"status": "failed", "reason": "unknown_tool", "message": f"Unknown tool {tool_name!r}."}
        req_payload = dict(arguments)
        req_payload.setdefault("rationale", f"Squad {worker_role or 'prefetch'} read-only lookup.")
        req = spec.input_model.model_validate(req_payload)

        with self._lock:
            entities_snapshot = dict(self._entities)
            sources_snapshot = dict(self._source_by_id)

        method = getattr(self._toolbox, tool_name, None)
        if not callable(method):
            return {"status": "unavailable", "reason": "tool_not_wired", "message": f"Toolbox missing {tool_name!r}."}
        sig = inspect.signature(method)
        kwargs: dict[str, Any] = {}
        if "source_by_id" in sig.parameters:
            kwargs["source_by_id"] = sources_snapshot
        if "entities" in sig.parameters:
            outcome = method(req, entities_snapshot, **kwargs)
        else:
            outcome = method(req, **kwargs)

        with self._lock:
            if getattr(outcome, "entities", None):
                self._entities.update(outcome.entities)
            if getattr(outcome, "source", None) is not None:
                src = outcome.source
                self._source_by_id[src.source_id] = src
        return dict(outcome.result)

    def _local_read_sources(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        reads = arguments.get("reads") or []
        out: list[dict[str, Any]] = []
        with self._lock:
            sources = dict(self._source_by_id)
        for item in reads:
            if not isinstance(item, Mapping):
                continue
            sid = str(item.get("source_id") or "")
            src = sources.get(sid)
            if src is None:
                out.append({"source_id": sid, "status": "unavailable", "reason": "source_not_found"})
                continue
            start = max(0, int(item.get("start") or 0))
            end = min(len(src.text), int(item.get("end") or min(len(src.text), start + 8000)))
            out.append({
                "source_id": sid,
                "status": "ok",
                "title": src.title,
                "content_sha256": src.content_sha256,
                "start": start,
                "end": end,
                "text": src.text[start:end],
            })
        return {"status": "ok", "reads": out}

    def _local_search_source(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        sid = str(arguments.get("source_id") or "")
        query = str(arguments.get("query") or "").strip()
        with self._lock:
            src = self._source_by_id.get(sid)
        if src is None:
            return {"status": "failed", "reason": "source_not_found"}
        if not query:
            return {"status": "failed", "reason": "invalid_request", "message": "query required"}
        hits: list[dict[str, Any]] = []
        lower_text = src.text.casefold()
        lower_q = query.casefold()
        pos = 0
        while len(hits) < 10:
            idx = lower_text.find(lower_q, pos)
            if idx < 0:
                break
            lo = max(0, idx - 120)
            hi = min(len(src.text), idx + len(query) + 180)
            hits.append({"start": lo, "end": hi, "snippet": src.text[lo:hi]})
            pos = idx + max(1, len(query))
        return {"status": "ok", "source_id": sid, "hits": hits}

    def _local_outline_source(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        sid = str(arguments.get("source_id") or "")
        with self._lock:
            src = self._source_by_id.get(sid)
        if src is None:
            return {"status": "failed", "reason": "source_not_found"}
        lines = [line for line in src.text.splitlines() if line.strip()][:25]
        return {"status": "ok", "source_id": sid, "title": src.title, "preview_lines": lines}


# --------------------------------------------------------------------------- Prefetch Inputs & Stage 0
class SquadPrefetchRequest(_FrozenContract):
    """Input configuration for Stage 0 deterministic prefetch + full squad run."""

    question: str = Field(min_length=1, max_length=2000)
    entity_id: str | None = None
    cik: str | None = None
    symbol: str | None = None
    company_name: str | None = None
    sic_code: str | None = None
    sic_description: str | None = None
    period_label: str | None = None
    as_of: date | datetime | None = None
    account_id: str | None = None
    fmp_byok_api_key: str | None = Field(default=None, repr=False)
    close_price: Decimal | float | int | str | None = None
    filing_accession: str | None = None
    prior_filing_accession: str | None = None
    current_filing_url: str | None = None
    prior_filing_url: str | None = None
    filing_form: str | None = "10-Q"
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None
    period_financials: list[PeriodFinancials] = Field(default_factory=list)
    guidance_inputs: list[GuidanceMetricInput] = Field(default_factory=list)
    nongaap_bridges: list[NonGaapBridgeInput] = Field(default_factory=list)
    max_prefetch_workers: int = Field(default=6, ge=1, le=8)


@dataclass
class PrefetchDependencies:
    """Injectable adapters/functions used by Stage 0 deterministic prefetch."""

    sec_adapter: SecEdgarAdapter | None = None
    sec_deep: SecDeepAdapter | None = None
    external_earnings: ExternalEarningsService | None = None
    tool_runner: SquadToolRunner | None = None
    market_price_fn: Callable[[str | None, str | None], Decimal | float | str | None] | None = None
    precomputed_snapshot: EarningsPrefetchSnapshot | None = None
    initial_source_texts: dict[str, str] = field(default_factory=dict)
    initial_source_permissions: dict[str, dict[str, bool]] = field(default_factory=dict)


def _as_of_date(as_of: date | datetime | None) -> date | None:
    if as_of is None:
        return None
    if isinstance(as_of, datetime):
        return as_of.date()
    return as_of


def _fetch_sec_submissions_meta(
    sec: SecEdgarAdapter,
    cik: str,
) -> tuple[dict[str, Any], Mapping[str, Any] | None, list[Filing]]:
    """Fetch submissions metadata (SIC/name/tickers) and recent filings in one request."""
    c = cik10(cik)
    eid = f"cik:{c}"
    doc, _prov, _cost, _perms = sec._get(f"https://data.sec.gov/submissions/CIK{c}.json", "filings", "cik_not_found", large=True)
    if not isinstance(doc, Mapping):
        return {"cik": c, "name": None, "sic": None, "sic_description": None, "tickers": []}, None, []
    meta = {
        "cik": c,
        "name": doc.get("name"),
        "sic": doc.get("sic"),
        "sic_description": doc.get("sicDescription"),
        "tickers": list(doc.get("tickers") or []),
    }
    recent = (doc.get("filings") or {}).get("recent")
    periodic_forms = frozenset({"10-Q", "10-K", "20-F", "40-F"})
    earnings_forms = frozenset({"8-K", "6-K"})
    wanted_forms = periodic_forms | earnings_forms
    periodic_rows: list[Filing] = []
    other_rows: list[Filing] = []
    if isinstance(recent, Mapping) and isinstance(recent.get("accessionNumber"), list):
        accs = recent["accessionNumber"]
        forms = recent.get("form") or []
        fdates = recent.get("filingDate") or []
        rdates = recent.get("reportDate") or []
        adts = recent.get("acceptanceDateTime") or []
        pdocs = recent.get("primaryDocument") or []
        pdescs = recent.get("primaryDocDescription") or []
        for idx, acc in enumerate(accs):
            form = forms[idx] if idx < len(forms) else None
            filed_raw = fdates[idx] if idx < len(fdates) else None
            if not isinstance(acc, str) or not isinstance(form, str) or not isinstance(filed_raw, str):
                continue
            base_form = form.strip().upper().removesuffix("/A")
            if base_form not in wanted_forms:
                continue
            try:
                filed = date.fromisoformat(filed_raw[:10])
            except ValueError:
                continue
            rdate_raw = rdates[idx] if idx < len(rdates) else None
            period_of_report: date | None = None
            if isinstance(rdate_raw, str) and rdate_raw.strip():
                try:
                    period_of_report = date.fromisoformat(rdate_raw[:10])
                except ValueError:
                    pass
            adt_raw = adts[idx] if idx < len(adts) else None
            accepted_at: datetime | None = None
            if isinstance(adt_raw, str) and adt_raw.strip():
                try:
                    accepted_at = datetime.fromisoformat(adt_raw.replace("Z", "+00:00"))
                    if accepted_at.tzinfo is None:
                        accepted_at = accepted_at.replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
            nodash = acc.replace("-", "")
            pdoc = pdocs[idx] if idx < len(pdocs) else None
            base_url = f"https://www.sec.gov/Archives/edgar/data/{int(c)}/{nodash}"
            filing_row = Filing(
                entity_id=eid,
                form=form,
                filed_at=filed,
                accepted_at=accepted_at,
                period_of_report=period_of_report,
                accession=acc,
                primary_document_url=f"{base_url}/{pdoc}" if isinstance(pdoc, str) and pdoc else None,
                index_url=f"{base_url}/{acc}-index.htm",
                description=(pdescs[idx] if idx < len(pdescs) and pdescs[idx] else None),
            )
            if base_form in periodic_forms:
                periodic_rows.append(filing_row)
            else:
                other_rows.append(filing_row)
    periodic_rows.sort(key=lambda f: (f.filed_at, f.accession), reverse=True)
    other_rows.sort(key=lambda f: (f.filed_at, f.accession), reverse=True)
    merged = periodic_rows[:15] + other_rows[:15]
    merged.sort(key=lambda f: (f.filed_at, f.accession), reverse=True)
    return meta, doc, merged


def _pick_current_prior_periodic_filings(
    filings: Sequence[Filing],
    *,
    as_of: date | None,
    preferred_form: str | None = None,
) -> tuple[Filing | None, Filing | None]:
    eligible = [
        f for f in filings
        if f.form.upper().removesuffix("/A") in ("10-Q", "10-K")
        and (as_of is None or f.filed_at <= as_of)
    ]
    eligible.sort(key=lambda f: f.filed_at, reverse=True)
    if not eligible:
        return None, None
    if preferred_form:
        pref_base = preferred_form.upper().removesuffix("/A")
        typed = [f for f in eligible if f.form.upper().removesuffix("/A") == pref_base]
        if len(typed) >= 2:
            return typed[0], typed[1]
    return eligible[0], (eligible[1] if len(eligible) > 1 else None)


def _build_periods_from_sec(
    sec: SecEdgarAdapter,
    cik: str,
    *,
    entity_id: str,
    as_of: date | None,
) -> tuple[list[PeriodFinancials], dict[str, Any]]:
    q_res = sec.statement_lines(cik, period="quarter", n=8, derive_q4=True, entity_id=entity_id)
    if q_res.status == "failed" and q_res.reason == "source_too_large":
        q_res = sec.statement_lines(
            cik,
            lines=[
                "revenue", "cost_of_revenue", "gross_profit", "rd_expense", "sga_expense",
                "operating_income", "interest_expense", "net_income", "ebitda",
                "depreciation_amortization", "shares_diluted", "operating_cash_flow",
                "capex", "share_repurchases", "dividends_paid", "cash",
                "short_term_investments", "accounts_receivable", "inventory",
                "current_assets", "accounts_payable", "short_term_debt",
                "long_term_debt", "current_liabilities",
            ],
            period="quarter",
            n=8,
            derive_q4=True,
            entity_id=entity_id,
        )
    lines = list(q_res.data) if q_res.status in ("ok", "partial") else []
    if as_of is not None:
        lines = [ln for ln in lines if ln.filed_at is None or ln.filed_at <= as_of]
    derived_rows = derive_period_metrics(lines) if lines else []
    derived: dict[str, Any] = {
        "statement_lines_count": len(derived_rows),
        "metrics_by_period": {
            f"{row.period_end.isoformat()}:{row.line}": {
                "value": str(row.value),
                "unit": row.unit,
                "currency": row.currency,
                "derivation": row.derivation,
            }
            for row in derived_rows
        },
    }
    by_end: dict[date, list[Any]] = {}
    for ln in lines:
        by_end.setdefault(ln.period_end, []).append(ln)
    ordered = sorted(by_end.items(), key=lambda kv: kv[0], reverse=True)
    periods: list[PeriodFinancials] = []
    for pend, grp in ordered[:6]:
        q_labels = [g.fiscal_label for g in grp if g.fiscal_label and "-Q" in g.fiscal_label]
        any_labels = [g.fiscal_label for g in grp if g.fiscal_label]
        label = q_labels[0] if q_labels else (any_labels[0] if any_labels else pend.isoformat())
        try:
            periods.append(
                period_financials_from_statement_lines(
                    grp, pend, period_label=label, period_type="quarter"
                )
            )
        except Exception:
            continue
        if len(periods) >= 5:
            break
    return periods, derived


def _compute_ttm_period(quarters: Sequence[PeriodFinancials]) -> PeriodFinancials | None:
    if len(quarters) < 4:
        return None
    q4 = list(quarters[:4])
    flow_fields = (
        "revenue", "cost_of_revenue", "gross_profit", "rd_expense", "sga_expense",
        "operating_income", "ebit", "interest_expense", "net_income", "ebitda",
        "depreciation_amortization", "sbc", "operating_cash_flow", "capex",
        "free_cash_flow", "share_repurchases", "dividends_paid",
    )
    latest = q4[0]
    payload: dict[str, Any] = {
        "period_label": f"TTM_{latest.period_label}",
        "period_type": "ttm",
        "period_end": latest.period_end,
        "currency": latest.currency,
        "monetary_unit": latest.monetary_unit,
    }
    for fname in flow_fields:
        vals = [getattr(q, fname) for q in q4]
        if all(v is not None for v in vals):
            total = sum((v.value for v in vals if v is not None), Decimal(0))
            first = vals[0]
            assert first is not None
            payload[fname] = first.model_copy(update={"value": total, "period": payload["period_label"]})
    for bs_field in (
        "cash", "short_term_investments", "accounts_receivable", "inventory", "current_assets",
        "accounts_payable", "short_term_debt", "long_term_debt", "total_debt",
        "current_liabilities", "minority_interest", "shares_diluted",
    ):
        val = getattr(latest, bs_field)
        if val is not None:
            payload[bs_field] = val
    return PeriodFinancials.model_validate(payload)


def _valuation_request_from_snapshot(
    req: SquadPrefetchRequest,
    *,
    entity_id: str,
    symbol: str | None,
    sic: str | None,
    sic_description: str | None,
    periods: Sequence[PeriodFinancials],
    close_price: Decimal | None,
    consensus_records: Sequence[EarningsConsensus],
) -> ValuationRequest:
    latest = periods[0] if periods else None
    ttm = next((p for p in periods if p.period_type == "ttm"), None) or _compute_ttm_period(
        [p for p in periods if p.period_type == "quarter"]
    )
    base = ttm or latest
    ntm_rev = None
    ntm_eps = None
    if consensus_records:
        future = [c for c in consensus_records if c.revenue_actual is None and c.eps_actual is None]
        if future and future[0].revenue_estimate_avg is not None:
            ntm_rev = future[0].revenue_estimate_avg
        if future and future[0].eps_estimate_avg is not None:
            ntm_eps = future[0].eps_estimate_avg
    fcf_val = base.free_cash_flow if base is not None else None
    if fcf_val is None and base is not None and base.operating_cash_flow is not None and base.capex is not None:
        cap = abs(base.capex.value)
        fcf_val = base.operating_cash_flow.model_copy(update={"value": base.operating_cash_flow.value - cap})
    return ValuationRequest(
        entity_id=entity_id,
        symbol=symbol,
        sic=sic,
        sic_description=sic_description,
        currency=base.currency if base else None,
        monetary_unit=base.monetary_unit if base else None,
        close_price=close_price,
        diluted_shares=latest.shares_diluted if latest else None,
        cash=latest.cash if latest else None,
        short_term_investments=latest.short_term_investments if latest else None,
        total_debt=latest.total_debt if latest else None,
        short_term_debt=latest.short_term_debt if latest else None,
        long_term_debt=latest.long_term_debt if latest else None,
        minority_interest=latest.minority_interest if latest else None,
        ttm_revenue=base.revenue if base else None,
        ttm_ebitda=base.ebitda if base else None,
        ttm_ebit=base.ebit if base else None,
        ttm_depreciation_amortization=base.depreciation_amortization if base else None,
        ttm_net_income=base.net_income if base else None,
        ttm_ocf=base.operating_cash_flow if base else None,
        ttm_capex=base.capex if base else None,
        ttm_fcf=fcf_val,
        ttm_sbc=base.sbc if base else None,
        ntm_revenue=ntm_rev,
        ntm_eps=ntm_eps,
    )


# --------------------------------------------------------------------------- Server-side Verification & Gating
def collect_valid_evidence_anchors(
    snapshot: EarningsPrefetchSnapshot,
    cards: Sequence[EvidenceCard],
    open_gaps: Sequence[SquadGap] = (),
) -> set[str]:
    """Collect the authoritative anchor IDs eligible for BearCaseItem grounding."""
    anchors: set[str] = set()
    for flag in snapshot.flags:
        anchors.add(flag.flag_id)
    if snapshot.diagnostics is not None:
        for flag in snapshot.diagnostics.flags:
            anchors.add(flag.flag_id)
    if snapshot.guidance_nongaap is not None:
        for warn in snapshot.guidance_nongaap.warnings:
            anchors.add(warn.flag_id)
        for ev in snapshot.guidance_nongaap.guidance_evaluations:
            anchors.add(ev.metric_name)
            anchors.add(f"guidance:{ev.metric_name}")
        for br in snapshot.guidance_nongaap.nongaap_verifications:
            anchors.add(br.metric_name)
            anchors.add(f"nongaap:{br.metric_name}")
    if snapshot.redline_summary is not None:
        anchors.add("redline:summary")
        for sec_key in snapshot.redline_summary.section_diffs:
            anchors.add(sec_key)
            anchors.add(f"redline:{sec_key}")
    if snapshot.valuation is not None:
        anchors.add("reverse_dcf")
        anchors.add("valuation:reverse_dcf")
        for metric_field in (
            "market_cap", "enterprise_value", "pe_ttm", "pe_forward",
            "ev_to_sales", "ev_to_ntm_sales", "ev_to_ebitda", "ev_to_ntm_ebitda",
            "ev_to_ebit", "p_to_fcf", "fcf_yield_pct", "fcf_ex_sbc",
            "ev_to_fcf_ex_sbc", "fcf_ex_sbc_yield_pct",
        ):
            metric_obj = getattr(snapshot.valuation, metric_field, None)
            if metric_obj is not None and metric_obj.status == "computed":
                anchors.add(metric_field)
                anchors.add(f"valuation:{metric_field}")
                for inp in metric_obj.input_ids:
                    anchors.add(inp)
    if snapshot.earnings_release is not None:
        if snapshot.earnings_release.current is not None:
            anchors.add(f"8k:{snapshot.earnings_release.current.accession}")
            if snapshot.earnings_release.current.guidance_passages:
                anchors.add("8k:guidance")
            if snapshot.earnings_release.current.nongaap_reconciliations:
                anchors.add("8k:nongaap")
        if snapshot.earnings_release.prior is not None:
            anchors.add(f"8k:{snapshot.earnings_release.prior.accession}")
    if snapshot.consensus_summary.status == "available":
        anchors.add("consensus:gap")
    if snapshot.transcript_index.status == "available":
        anchors.add("transcript:qa")
        if snapshot.transcript_index.source_id:
            anchors.add(snapshot.transcript_index.source_id)
    for src in snapshot.prefetched_sources:
        anchors.add(src.source_id)
    for card in cards:
        anchors.add(card.card_id)
        for fid in card.supporting_fact_ids:
            if fid:
                anchors.add(fid)
        for cit in card.citations:
            if cit.fact_id:
                anchors.add(cit.fact_id)
            anchors.add(cit.source_id)
    for gap_item in open_gaps:
        anchors.add(gap_item.gap_id)
        anchors.add(gap_item.code)
    return anchors


def _is_generic_unanchored_bear(item: BearCaseItem, valid_anchors: set[str]) -> str | None:
    resolved = [a for a in item.anchored_evidence_ids if a in valid_anchors]
    if not resolved:
        return "anchored_evidence_ids do not match any verified snapshot flag, redline, metric, or EvidenceCard."
    has_concrete = bool(_CONCRETE_SPECIFICITY_RE.search(item.thesis_challenge)) or bool(item.supporting_numbers)
    if not has_concrete and any(p.search(item.thesis_challenge) for p in _GENERIC_BEAR_PATTERNS):
        return "generic macro/competitive platitude lacking concrete company metrics or quantified evidence."
    return None


def gate_bear_cases(
    raw_items: Sequence[Any],
    snapshot: EarningsPrefetchSnapshot,
    cards: Sequence[EvidenceCard],
    open_gaps: Sequence[SquadGap] = (),
) -> tuple[list[BearCaseItem], list[RejectedBearCase], list[SquadGap]]:
    """Server-side anchor gate rejecting/flagging generic or unanchored bear cases."""
    valid_anchors = collect_valid_evidence_anchors(snapshot, cards, open_gaps)
    accepted: list[BearCaseItem] = []
    rejected: list[RejectedBearCase] = []
    gaps: list[SquadGap] = []

    for idx, raw in enumerate(raw_items):
        try:
            item = raw if isinstance(raw, BearCaseItem) else BearCaseItem.model_validate(raw)
        except Exception as exc:
            raw_dict = dict(raw) if isinstance(raw, Mapping) else {}
            rej = RejectedBearCase(
                bear_id=str(raw_dict.get("bear_id") or f"bear_rej_{idx}"),
                thesis_challenge=str(raw_dict.get("thesis_challenge") or ""),
                anchored_evidence_ids=list(raw_dict.get("anchored_evidence_ids") or []),
                anchor_kind=raw_dict.get("anchor_kind"),
                next_quarter_invalidation_trigger=raw_dict.get("next_quarter_invalidation_trigger"),
                rejection_reason=f"schema_or_missing_anchor: {exc}",
            )
            rejected.append(rej)
            gaps.append(SquadGap(
                stage="stage_3a_bear",
                code="bear_case_rejected",
                message=f"Rejected generic/invalid bear case {rej.bear_id}: {rej.rejection_reason}",
            ))
            continue

        reason = _is_generic_unanchored_bear(item, valid_anchors)
        if reason is not None:
            rej = RejectedBearCase(
                bear_id=item.bear_id,
                thesis_challenge=item.thesis_challenge,
                anchored_evidence_ids=list(item.anchored_evidence_ids),
                anchor_kind=item.anchor_kind,
                next_quarter_invalidation_trigger=item.next_quarter_invalidation_trigger,
                rejection_reason=reason,
            )
            rejected.append(rej)
            gaps.append(SquadGap(
                stage="stage_3a_bear",
                code="bear_case_unanchored",
                message=f"Rejected unanchored bear case {item.bear_id}: {reason}",
            ))
            continue
        accepted.append(item)
    return accepted, rejected, gaps


def gate_evidence_cards(
    batches: Sequence[EvidenceCardBatch],
    snapshot: EarningsPrefetchSnapshot,
    *,
    source_texts: Mapping[str, str] | None = None,
    source_permissions: Mapping[str, Mapping[str, bool]] | None = None,
) -> tuple[list[EvidenceCard], DossierGateReport, list[SquadGap]]:
    """Verify EvidenceCard citations/provenance and compute export permissions."""
    texts = source_texts or {}
    perms_by_src: dict[str, dict[str, bool]] = {
        ref.source_id: dict(ref.permissions) for ref in snapshot.prefetched_sources
    }
    if source_permissions:
        for sid, p in source_permissions.items():
            perms_by_src[sid] = dict(p)

    sha_by_src = {ref.source_id: ref.content_sha256 for ref in snapshot.prefetched_sources}
    verified_cards: list[EvidenceCard] = []
    verified_ids: list[str] = []
    dropped_ids: list[str] = []
    unverified_cits: list[dict[str, str]] = []
    gaps: list[SquadGap] = []
    non_exportable: set[str] = set()

    for ref in snapshot.prefetched_sources:
        if ref.permissions.get("export") is False:
            non_exportable.add(ref.source_id)
    if snapshot.consensus_summary.permissions.get("export") is False:
        non_exportable.add(snapshot.consensus_summary.source_id or "fmp_consensus")
    if snapshot.transcript_index.permissions.get("export") is False:
        non_exportable.add(snapshot.transcript_index.source_id or "fmp_transcript")

    for batch in batches:
        for card in batch.cards:
            kept_citations: list[EvidenceCitation] = []
            for cit in card.citations:
                src_perms = perms_by_src.get(cit.source_id)
                if src_perms is not None and src_perms.get("export") is False:
                    non_exportable.add(cit.source_id)
                expected_sha = sha_by_src.get(cit.source_id)
                if cit.content_sha256 and expected_sha and cit.content_sha256 != expected_sha:
                    unverified_cits.append({
                        "card_id": card.card_id,
                        "source_id": cit.source_id,
                        "reason": "content_sha256_mismatch",
                    })
                    continue
                src_text = texts.get(cit.source_id)
                if src_text is not None:
                    if cit.quotation not in src_text and _norm_quote_text(cit.quotation) not in _norm_quote_text(src_text):
                        unverified_cits.append({
                            "card_id": card.card_id,
                            "source_id": cit.source_id,
                            "reason": "quotation_not_found_in_source",
                        })
                        continue
                kept_citations.append(cit)

            if not kept_citations and not card.supporting_fact_ids:
                dropped_ids.append(card.card_id)
                gaps.append(SquadGap(
                    stage="stage_3b_gate",
                    worker_role=card.worker_role,
                    code="card_unverified_provenance",
                    message=f"Dropped EvidenceCard {card.card_id} lacking verified citations or supporting_fact_ids.",
                ))
                continue

            if len(kept_citations) != len(card.citations):
                card = card.model_copy(update={"citations": kept_citations})
            verified_cards.append(card)
            verified_ids.append(card.card_id)

    notes: list[str] = []
    if dropped_ids:
        notes.append(f"Dropped {len(dropped_ids)} ungrounded EvidenceCard(s).")
    if non_exportable:
        notes.append(f"Export restricted by third-party/BYOK permissions on: {', '.join(sorted(non_exportable))}.")

    report = DossierGateReport(
        passed=(len(dropped_ids) == 0 and len(unverified_cits) == 0),
        verified_card_ids=verified_ids,
        dropped_card_ids=dropped_ids,
        unverified_citations=unverified_cits,
        exportable=(len(non_exportable) == 0),
        non_exportable_sources=sorted(non_exportable),
        notes=notes,
    )
    return verified_cards, report, gaps


# --------------------------------------------------------------------------- Prompt Builders (Isolated Bear Prompt)
def build_isolated_bear_prompt(
    snapshot: EarningsPrefetchSnapshot,
    cards: Sequence[EvidenceCard],
    open_gaps: Sequence[SquadGap],
) -> tuple[str, str]:
    """Build a physically isolated adversarial Bear Reviewer prompt with ZERO bull narrative."""
    system_prompt = (
        "You are an independent buy-side Adversarial Bear Reviewer (bear_reviewer). "
        "You receive ONLY deterministic financial diagnostics, SEC disclosures/redlines, "
        "valuation expectations, and factual EvidenceCards. You must NOT rely on any bull thesis "
        "or optimistic narrative. Every BearCaseItem MUST anchor to concrete evidence IDs "
        "(flag_id, redline section key, guidance/nongaap metric, valuation/Reverse-DCF metric, "
        "or EvidenceCard card_id/fact_id), specify anchor_kind in {flag, redline, guidance_gap, valuation_gap}, "
        "and define a concrete next_quarter_invalidation_trigger. Generic macro or competitive "
        "platitudes without hard evidence anchors are rejected by the server gate."
    )
    payload = {
        "entity": {
            "entity_id": snapshot.entity_id,
            "symbol": snapshot.symbol,
            "cik": snapshot.cik,
            "company_name": snapshot.company_name,
            "period_label": snapshot.period_label,
        },
        "sector_applicability": snapshot.sector_applicability.model_dump(mode="json"),
        "diagnostic_flags": [f.model_dump(mode="json") for f in snapshot.flags],
        "redline_summary": snapshot.redline_summary.model_dump(mode="json") if snapshot.redline_summary else None,
        "guidance_nongaap": snapshot.guidance_nongaap.model_dump(mode="json") if snapshot.guidance_nongaap else None,
        "valuation": snapshot.valuation.model_dump(mode="json") if snapshot.valuation else None,
        "evidence_cards": [
            {
                "card_id": c.card_id,
                "worker_role": c.worker_role,
                "topic_tag": c.topic_tag,
                "finding": c.finding,
                "supporting_fact_ids": c.supporting_fact_ids,
                "caveat": c.caveat,
                "unresolved_gaps": c.unresolved_gaps,
                "citations": [cit.model_dump(mode="json") for cit in c.citations],
            }
            for c in cards
        ],
        "open_gaps": [g.model_dump(mode="json") for g in open_gaps],
    }
    return system_prompt, json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _build_writer_payload(dossier_core: Mapping[str, Any], *, mode: Literal["memo", "deep_report"]) -> str:
    return json.dumps(
        {
            "render_mode": mode,
            "locked_dossier": dossier_core,
            "rules": [
                "Use ONLY facts, numbers, citations, bear cases, and open gaps present in locked_dossier.",
                "Respect sector_applicability: never fabricate or report disabled_by_sector metrics.",
                "Do not invoke any retrieval tools; report open_gaps explicitly.",
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _deterministic_markdown_fallback(dossier_core: Mapping[str, Any], *, mode: Literal["memo", "deep_report"]) -> str:
    entity = dossier_core.get("symbol") or dossier_core.get("company_name") or dossier_core.get("entity_id")
    period = dossier_core.get("period_label") or "Latest Period"
    title = "Buy-Side Earnings Memo" if mode == "memo" else "Deep Earnings Research Report"
    lines = [f"# {title}: {entity} ({period})", ""]
    cards = dossier_core.get("evidence_cards") or []
    bears = dossier_core.get("bear_cases") or []
    gaps = dossier_core.get("open_gaps") or []
    lines.append("## Key Evidence Findings")
    if cards:
        for c in cards:
            lines.append(f"- **[{c.get('topic_tag')}]** ({c.get('card_id')}): {c.get('finding')}")
    else:
        lines.append("- No verified evidence cards available.")
    lines.extend(["", "## Adversarial Bear Cases"])
    if bears:
        for b in bears:
            lines.append(
                f"- **[{b.get('anchor_kind')}]** ({', '.join(b.get('anchored_evidence_ids') or [])}): "
                f"{b.get('thesis_challenge')} | Invalidation: {b.get('next_quarter_invalidation_trigger')}"
            )
    else:
        lines.append("- No anchored bear cases recorded.")
    lines.extend(["", "## Open Gaps & Boundaries"])
    sector = dossier_core.get("sector_applicability") or {}
    if sector.get("is_gated"):
        lines.append(f"- Sector gate active ({sector.get('sector_category')}): {sector.get('explanation')}")
    if gaps:
        for g in gaps:
            lines.append(f"- [{g.get('stage')}] {g.get('code')}: {g.get('message')}")
    elif not sector.get("is_gated"):
        lines.append("- None.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- On-Demand Deep Report
def render_deep_report(
    dossier: EarningsDossier,
    *,
    model_runner: SquadModelRunner,
    slot_bindings: Mapping[ModelSlot, SlotBinding] | None = None,
    ledger: SquadExecutionLedger | None = None,
    org_emitter: OrgGraphEmitter | None = None,
    cache: dict[tuple[str, str], str] | None = None,
    force: bool = False,
) -> EarningsDossier:
    """Render and cache the full Deep Report from a locked ``EarningsDossier``.

    Strictly executes ZERO retrieval tool calls and preserves identical facts/numbers.
    """
    cache_key = (dossier.dossier_id, dossier.dossier_sha256)
    if not force:
        if dossier.deep_report_markdown is not None:
            if org_emitter is not None:
                org_emitter.emit(
                    stage="deep_report",
                    node_id="dossier_writer",
                    action_kind="RESPONDING",
                    runtime_state="completed",
                    current_activity="Returned cached Deep Report from locked EarningsDossier.",
                    slot="writer",
                )
            return dossier
        if cache is not None and cache_key in cache:
            cached_md = cache[cache_key]
            if org_emitter is not None:
                org_emitter.emit(
                    stage="deep_report",
                    node_id="dossier_writer",
                    action_kind="RESPONDING",
                    runtime_state="completed",
                    current_activity="Returned cached Deep Report from dossier cache.",
                    slot="writer",
                )
            return dossier.model_copy(update={"deep_report_markdown": cached_md})

    bindings = dict(slot_bindings) if slot_bindings is not None else resolve_squad_slots()
    active_ledger = ledger or SquadExecutionLedger(slot_bindings=bindings)
    active_ledger.ensure_running(stage="deep_report")

    if org_emitter is not None:
        org_emitter.emit(
            stage="deep_report",
            node_id="dossier_writer",
            action_kind="RESPONDING",
            runtime_state="running",
            current_activity="Synthesizing full Deep Report from locked EarningsDossier (zero retrieval tools).",
            slot="writer",
        )

    dossier_core = dossier.model_dump(
        mode="json",
        exclude={"deep_report_markdown", "slot_usage_summary"},
    )
    sys_prompt = (
        "You are an institutional buy-side equity research writer (writer slot). "
        "Produce the full Deep Report in Markdown strictly from the locked EarningsDossier. "
        "You have ZERO retrieval tools and MUST NOT alter any number, period, unit, or fact."
    )
    user_prompt = _build_writer_payload(dossier_core, mode="deep_report")
    call_id, before = active_ledger.admit_model_call("writer", stage="deep_report")
    t0 = monotonic()
    failed = False
    usage: ModelUsage | None = None
    try:
        req = SlotModelRequest(
            slot="writer",
            stage="deep_report",
            role="writer",
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            tools=[],  # Zero tools permitted
            binding=bindings["writer"],
        )
        resp = model_runner.invoke(req)
        usage = resp.usage
        if resp.tool_calls:
            active_ledger.record_tool_call(stage="deep_report")
        md = _extract_markdown_from_response(resp, dossier_core, mode="deep_report")
    except SquadToolViolationError:
        failed = True
        raise
    except Exception as exc:
        failed = True
        if org_emitter is not None:
            org_emitter.emit(
                stage="deep_report",
                node_id="dossier_writer",
                action_kind="FAILED",
                runtime_state="failed",
                current_activity="Deep Report synthesis failed.",
                blocker_text=str(exc),
                slot="writer",
            )
        raise
    finally:
        elapsed_ms = int((monotonic() - t0) * 1000)
        active_ledger.settle_model_call("writer", call_id, before, usage=usage, elapsed_ms=elapsed_ms, failed=failed)

    if cache is not None:
        cache[cache_key] = md
    if org_emitter is not None:
        org_emitter.emit(
            stage="deep_report",
            node_id="dossier_writer",
            action_kind="RESPONDING",
            runtime_state="completed",
            current_activity="Deep Report synthesized and cached on locked EarningsDossier.",
            slot="writer",
        )
    return dossier.model_copy(update={"deep_report_markdown": md})


def _extract_markdown_from_response(
    resp: SlotModelResponse,
    dossier_core: Mapping[str, Any],
    *,
    mode: Literal["memo", "deep_report"],
) -> str:
    if isinstance(resp.parsed, Mapping):
        for key in ("deep_report_markdown", "memo_markdown", "markdown", "report"):
            val = resp.parsed.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    if isinstance(resp.content, str) and resp.content.strip():
        txt = resp.content.strip()
        if txt.startswith("{") and txt.endswith("}"):
            try:
                obj = json.loads(txt)
                if isinstance(obj, Mapping):
                    for key in ("deep_report_markdown", "memo_markdown", "markdown", "report"):
                        val = obj.get(key)
                        if isinstance(val, str) and val.strip():
                            return val.strip()
            except ValueError:
                pass
        return txt
    return _deterministic_markdown_fallback(dossier_core, mode=mode)


# --------------------------------------------------------------------------- Orchestrator
class EarningsSquadOrchestrator:
    """6-stage bounded buy-side earnings research squad orchestrator (Scheme 2)."""

    def __init__(
        self,
        *,
        model_runner: SquadModelRunner,
        tool_runner: SquadToolRunner | None = None,
        prefetch_deps: PrefetchDependencies | None = None,
        slot_bindings: Mapping[ModelSlot, SlotBinding] | None = None,
        resolved_slots: Any = None,
        primary_profile: Mapping[str, Any] | Any | None = None,
        slot_overrides: Mapping[str, Any] | None = None,
        checkpoint_store: SquadCheckpointStore | None = None,
        org_sinks: Sequence[Callable[[OrgGraphEvent], None]] = (),
        context: ResearchExecutionContext | None = None,
        budget: ResearchExecutionBudget | None = None,
        cancel_event: Event | None = None,
        max_worker_concurrency: int = 3,
    ) -> None:
        self._model_runner = model_runner
        self._prefetch_deps = prefetch_deps or PrefetchDependencies()
        self._tool_runner = tool_runner or self._prefetch_deps.tool_runner
        if slot_bindings is not None:
            self._slot_bindings = dict(slot_bindings)
            if not self._slot_bindings["fast_extractor"].native_tools:
                raise SquadConfigurationError(
                    "Model slot 'fast_extractor' must support native tool calling for Stage 2 workers.",
                    code="fast_extractor_native_tools_required",
                )
        else:
            self._slot_bindings = resolve_squad_slots(
                primary_profile=primary_profile,
                slot_overrides=slot_overrides,
                resolved_slots=resolved_slots,
            )
        self._checkpoint_store = checkpoint_store or InMemorySquadCheckpointStore()
        self._org_sinks = list(org_sinks)
        self._context = context
        self._budget = budget
        self._cancel_event = cancel_event or Event()
        self._max_worker_concurrency = max(1, min(3, max_worker_concurrency))

        self._steering_lock = RLock()
        self._deep_report_lock = RLock()
        self._steering_wakeup = Event()
        self._steering_queue: list[UserSteeringInstruction] = []
        self._next_instruction_seq = 0
        self._deep_report_cache: dict[tuple[str, str], str] = {}
        self._active_ledger: SquadExecutionLedger | None = None
        self._active_emitter: OrgGraphEmitter | None = None

    @property
    def checkpoint_store(self) -> SquadCheckpointStore:
        return self._checkpoint_store

    @property
    def slot_bindings(self) -> dict[ModelSlot, SlotBinding]:
        return dict(self._slot_bindings)

    def cancel(self) -> None:
        self._cancel_event.set()
        if self._active_ledger is not None:
            self._active_ledger.cancel()
        self._steering_wakeup.set()

    def submit_steering_instruction(
        self,
        text: str,
        *,
        sequence: int | None = None,
    ) -> UserSteeringInstruction:
        """Queue a mid-run user instruction to be incrementally evaluated by Lead Coordinator."""
        cleaned = _clean_nonblank(text, "steering instruction")
        with self._steering_lock:
            seq = sequence if sequence is not None else (self._next_instruction_seq + 1)
            if seq <= self._next_instruction_seq:
                seq = self._next_instruction_seq + 1
            self._next_instruction_seq = seq
            instr = UserSteeringInstruction(sequence=seq, text=cleaned)
            self._steering_queue.append(instr)
            self._steering_wakeup.set()
            return instr

    def _sync_instruction_counter(self, instructions: Sequence[UserSteeringInstruction]) -> None:
        with self._steering_lock:
            max_seq = max((i.sequence for i in instructions), default=0)
            if max_seq > self._next_instruction_seq:
                self._next_instruction_seq = max_seq

    def _collect_pending_instructions(
        self,
        applied_sequences: set[int],
        instruction_provider: Callable[[], Sequence[UserSteeringInstruction | Mapping[str, Any]]] | None = None,
    ) -> list[UserSteeringInstruction]:
        if instruction_provider is not None:
            provided = instruction_provider() or ()
            for raw in provided:
                item = raw if isinstance(raw, UserSteeringInstruction) else UserSteeringInstruction.model_validate(raw)
                with self._steering_lock:
                    if not any(q.sequence == item.sequence for q in self._steering_queue):
                        self._steering_queue.append(item)
                    if item.sequence > self._next_instruction_seq:
                        self._next_instruction_seq = item.sequence
        with self._steering_lock:
            return sorted(
                [i for i in self._steering_queue if i.sequence not in applied_sequences],
                key=lambda i: i.sequence,
            )

    def render_deep_report(self, dossier: EarningsDossier, *, force: bool = False) -> EarningsDossier:
        """Generate or return cached Deep Report from a locked ``EarningsDossier`` (0 tool calls)."""
        with self._deep_report_lock:
            return render_deep_report(
                dossier,
                model_runner=self._model_runner,
                slot_bindings=self._slot_bindings,
                ledger=self._active_ledger,
                org_emitter=self._active_emitter,
                cache=self._deep_report_cache,
                force=force,
            )

    async def render_deep_report_async(self, dossier: EarningsDossier, *, force: bool = False) -> EarningsDossier:
        return await asyncio.to_thread(self.render_deep_report, dossier, force=force)

    async def run_async(self, *args: Any, **kwargs: Any) -> EarningsDossier:
        return await asyncio.to_thread(self.run, *args, **kwargs)

    # --------------------------------------------------------------------------- Main Run Loop
    def run(
        self,
        request: SquadPrefetchRequest,
        *,
        run_id: str | None = None,
        resume_from: SquadCheckpoint | str | None = None,
        user_instructions: Sequence[UserSteeringInstruction | Mapping[str, Any]] = (),
        instruction_provider: Callable[[], Sequence[UserSteeringInstruction | Mapping[str, Any]]] | None = None,
        on_stage_complete: Callable[[str, SquadCheckpoint], None] | None = None,
        on_worker_complete: Callable[[WorkerRole, EvidenceCardBatch, SquadCheckpoint], None] | None = None,
    ) -> EarningsDossier:
        """Execute or resume the 6-stage bounded buy-side earnings research squad."""
        checkpoint = self._load_resume_checkpoint(run_id=run_id, resume_from=resume_from)
        active_run_id = checkpoint.run_id if checkpoint else (run_id or _new_id("squad"))

        with self._steering_lock:
            self._steering_queue.clear()
            self._next_instruction_seq = 0
            if checkpoint:
                self._steering_queue.extend(checkpoint.user_instructions)
                self._sync_instruction_counter(checkpoint.user_instructions)
            for raw_instr in user_instructions:
                item = (
                    raw_instr
                    if isinstance(raw_instr, UserSteeringInstruction)
                    else UserSteeringInstruction.model_validate(raw_instr)
                )
                if not any(q.sequence == item.sequence for q in self._steering_queue):
                    self._steering_queue.append(item)
                self._sync_instruction_counter([item])

        ledger = SquadExecutionLedger(
            slot_bindings=self._slot_bindings,
            context=self._context,
            budget=self._budget,
            cancel_event=self._cancel_event,
            initial_usage=checkpoint.slot_usage if checkpoint else None,
        )
        emitter = OrgGraphEmitter(
            sinks=self._org_sinks,
            initial_events=checkpoint.org_events if checkpoint else (),
            start_sequence=checkpoint.last_sequence if checkpoint else 0,
        )
        self._active_ledger = ledger
        self._active_emitter = emitter

        cp_lock = RLock()
        state: dict[str, Any] = {
            "run_id": active_run_id,
            "question": request.question,
            "entity_id": checkpoint.entity_id if checkpoint else request.entity_id,
            "symbol": checkpoint.symbol if checkpoint else request.symbol,
            "cik": checkpoint.cik if checkpoint else request.cik,
            "completed_stages": list(checkpoint.completed_stages) if checkpoint else [],
            "stage_status": dict(checkpoint.stage_status) if checkpoint else {},
            "snapshot": checkpoint.snapshot if checkpoint else None,
            "dispatch_plan": checkpoint.dispatch_plan if checkpoint else None,
            "worker_batches": dict(checkpoint.worker_batches) if checkpoint else {},
            "stage25_decision": checkpoint.stage25_decision if checkpoint else None,
            "stage25_executed": checkpoint.stage25_executed if checkpoint else False,
            "bear_batch": checkpoint.bear_batch if checkpoint else None,
            "dossier": checkpoint.dossier if checkpoint else None,
            "open_gaps": list(checkpoint.open_gaps) if checkpoint else [],
            "source_texts": dict(checkpoint.source_texts) if checkpoint else dict(self._prefetch_deps.initial_source_texts),
            "source_permissions": (
                dict(checkpoint.source_permissions)
                if checkpoint
                else dict(self._prefetch_deps.initial_source_permissions)
            ),
            "applied_instruction_sequences": set(checkpoint.applied_instruction_sequences) if checkpoint else set(),
        }

        def save_checkpoint(*, stage_completed: str | None = None) -> SquadCheckpoint:
            with cp_lock:
                if stage_completed and stage_completed not in state["completed_stages"]:
                    state["completed_stages"].append(stage_completed)
                    state["stage_status"][stage_completed] = "completed"
                with self._steering_lock:
                    all_instr = sorted(self._steering_queue, key=lambda x: x.sequence)
                cp_obj = SquadCheckpoint(
                    run_id=active_run_id,
                    question=request.question,
                    entity_id=state["entity_id"],
                    symbol=state["symbol"],
                    cik=state["cik"],
                    completed_stages=list(state["completed_stages"]),
                    stage_status=dict(state["stage_status"]),
                    snapshot=state["snapshot"],
                    dispatch_plan=state["dispatch_plan"],
                    worker_batches=dict(state["worker_batches"]),
                    stage25_decision=state["stage25_decision"],
                    stage25_executed=bool(state["stage25_executed"]),
                    bear_batch=state["bear_batch"],
                    dossier=state["dossier"],
                    open_gaps=list(state["open_gaps"]),
                    source_texts=dict(state["source_texts"]),
                    source_permissions=dict(state["source_permissions"]),
                    user_instructions=all_instr,
                    applied_instruction_sequences=sorted(state["applied_instruction_sequences"]),
                    slot_usage=ledger.slot_usage_snapshot(),
                    org_events=emitter.events,
                    last_sequence=emitter.sequence,
                    updated_at=_utcnow(),
                )
                self._checkpoint_store.save(cp_obj)
                return cp_obj

        def mark_applied_existing_instructions() -> None:
            pending = self._collect_pending_instructions(state["applied_instruction_sequences"], instruction_provider)
            for item in pending:
                state["applied_instruction_sequences"].add(item.sequence)

        # ---- Stage 0: Deterministic Parallel Prefetch (0 model calls)
        if "stage_0" in state["completed_stages"] and state["snapshot"] is not None:
            snapshot: EarningsPrefetchSnapshot = state["snapshot"]
            emitter.emit(
                stage="stage_0_prefetch",
                node_id="prefetch_engine",
                action_kind="RESPONDING",
                runtime_state="completed",
                current_activity="Reused Stage 0 EarningsPrefetchSnapshot from checkpoint.",
            )
        else:
            snapshot = self._run_stage_0_prefetch(
                request,
                ledger=ledger,
                emitter=emitter,
                source_texts=state["source_texts"],
                source_permissions=state["source_permissions"],
            )
            state["snapshot"] = snapshot
            state["entity_id"] = snapshot.entity_id
            state["symbol"] = snapshot.symbol
            state["cik"] = snapshot.cik
            state["open_gaps"] = _merge_gaps(state["open_gaps"], snapshot.open_gaps)
            cp = save_checkpoint(stage_completed="stage_0")
            if on_stage_complete is not None:
                on_stage_complete("stage_0", cp)

        # ---- Stage 1: Lead Coordinator Dispatch Plan
        if "stage_1" in state["completed_stages"] and state["dispatch_plan"] is not None:
            dispatch_plan: ResearchDispatchPlan = state["dispatch_plan"]
            emitter.emit(
                stage="stage_1_plan",
                node_id="lead_coordinator",
                action_kind="DELEGATING",
                runtime_state="completed",
                current_activity="Reused Stage 1 ResearchDispatchPlan from checkpoint.",
                slot="deep_reasoner",
            )
        else:
            mark_applied_existing_instructions()
            dispatch_plan = self._run_stage_1_plan(
                request=request,
                snapshot=snapshot,
                ledger=ledger,
                emitter=emitter,
                instruction_seq=max(state["applied_instruction_sequences"], default=0),
            )
            state["dispatch_plan"] = dispatch_plan
            for task in dispatch_plan.tasks:
                if task.status == "skipped":
                    gap_item = SquadGap(
                        stage="stage_1_plan",
                        worker_role=task.worker_role,
                        code="worker_skipped",
                        message=f"{task.worker_role} skipped: {task.skip_reason}",
                    )
                    state["open_gaps"] = _merge_gaps(state["open_gaps"], [gap_item])
                    state["worker_batches"][f"round1:{task.worker_role}"] = EvidenceCardBatch(
                        task_id=task.task_id,
                        worker_role=task.worker_role,
                        round_number=1,
                        status="skipped",
                        unresolved_gaps=[task.skip_reason or "skipped"],
                    )
            cp = save_checkpoint(stage_completed="stage_1")
            if on_stage_complete is not None:
                on_stage_complete("stage_1", cp)

        # ---- Stage 2: Parallel Read-Only Workers (with Partial Survival, Checkpoints & Mid-Run Steering)
        if "stage_2" not in state["completed_stages"]:
            self._run_stage_2_with_steering(
                request=request,
                snapshot=snapshot,
                dispatch_plan=dispatch_plan,
                state=state,
                cp_lock=cp_lock,
                save_checkpoint=save_checkpoint,
                ledger=ledger,
                emitter=emitter,
                instruction_provider=instruction_provider,
                on_worker_complete=on_worker_complete,
            )
            cp = save_checkpoint(stage_completed="stage_2")
            if on_stage_complete is not None:
                on_stage_complete("stage_2", cp)
        else:
            # Check if new steering arrived on resume after Stage 2 completed
            self._handle_post_stage2_steering(
                request=request,
                snapshot=snapshot,
                dispatch_plan=dispatch_plan,
                state=state,
                cp_lock=cp_lock,
                save_checkpoint=save_checkpoint,
                ledger=ledger,
                emitter=emitter,
                instruction_provider=instruction_provider,
                on_worker_complete=on_worker_complete,
            )

        # ---- Stage 2.5: Lead Cross-Worker Review (Hard Cap 1 Round)
        self._handle_post_stage2_steering(
            request=request,
            snapshot=snapshot,
            dispatch_plan=dispatch_plan,
            state=state,
            cp_lock=cp_lock,
            save_checkpoint=save_checkpoint,
            ledger=ledger,
            emitter=emitter,
            instruction_provider=instruction_provider,
            on_worker_complete=on_worker_complete,
        )
        if "stage_2_5" in state["completed_stages"] and state["stage25_decision"] is not None:
            emitter.emit(
                stage="stage_2_5_review",
                node_id="lead_coordinator",
                action_kind="INSPECTING",
                runtime_state="completed",
                current_activity="Reused Stage 2.5 cross-worker review decision from checkpoint.",
                slot="deep_reasoner",
            )
        else:
            self._run_stage_2_5(
                request=request,
                snapshot=snapshot,
                dispatch_plan=dispatch_plan,
                state=state,
                cp_lock=cp_lock,
                save_checkpoint=save_checkpoint,
                ledger=ledger,
                emitter=emitter,
                on_worker_complete=on_worker_complete,
            )
            cp = save_checkpoint(stage_completed="stage_2_5")
            if on_stage_complete is not None:
                on_stage_complete("stage_2_5", cp)

        # Check steering once more before Stage 3A
        self._handle_post_stage2_steering(
            request=request,
            snapshot=snapshot,
            dispatch_plan=dispatch_plan,
            state=state,
            cp_lock=cp_lock,
            save_checkpoint=save_checkpoint,
            ledger=ledger,
            emitter=emitter,
            instruction_provider=instruction_provider,
            on_worker_complete=on_worker_complete,
        )
        if "stage_2_5" not in state["completed_stages"]:
            self._run_stage_2_5(
                request=request,
                snapshot=snapshot,
                dispatch_plan=dispatch_plan,
                state=state,
                cp_lock=cp_lock,
                save_checkpoint=save_checkpoint,
                ledger=ledger,
                emitter=emitter,
                on_worker_complete=on_worker_complete,
            )
            cp = save_checkpoint(stage_completed="stage_2_5")
            if on_stage_complete is not None:
                on_stage_complete("stage_2_5", cp)

        # ---- Stage 3A: Independent Adversarial Bear Reviewer
        all_cards = _collect_cards_from_batches(state["worker_batches"])
        if "stage_3a" in state["completed_stages"] and state["bear_batch"] is not None:
            bear_batch: BearCaseBatch = state["bear_batch"]
            emitter.emit(
                stage="stage_3a_bear",
                node_id="bear_reviewer",
                action_kind="RESPONDING",
                runtime_state="completed",
                current_activity="Reused Stage 3A BearCaseBatch from checkpoint.",
                slot="bear_reviewer",
            )
        else:
            bear_batch, bear_gaps = self._run_stage_3a_bear(
                snapshot=snapshot,
                cards=all_cards,
                open_gaps=state["open_gaps"],
                ledger=ledger,
                emitter=emitter,
            )
            state["bear_batch"] = bear_batch
            state["open_gaps"] = _merge_gaps(state["open_gaps"], bear_gaps)
            cp = save_checkpoint(stage_completed="stage_3a")
            if on_stage_complete is not None:
                on_stage_complete("stage_3a", cp)

        # ---- Stage 3B: Server Gate -> Lock EarningsDossier -> Render Memo
        if "stage_3b" in state["completed_stages"] and state["dossier"] is not None:
            dossier: EarningsDossier = state["dossier"]
            emitter.emit(
                stage="stage_3b_gate_memo",
                node_id="dossier_writer",
                action_kind="RESPONDING",
                runtime_state="completed",
                current_activity="Reused locked EarningsDossier from checkpoint.",
                slot="writer",
            )
            return dossier

        dossier = self._run_stage_3b_gate_and_memo(
            run_id=active_run_id,
            snapshot=snapshot,
            dispatch_plan=dispatch_plan,
            batches=list(state["worker_batches"].values()),
            bear_batch=bear_batch,
            open_gaps=state["open_gaps"],
            source_texts=state["source_texts"],
            source_permissions=state["source_permissions"],
            ledger=ledger,
            emitter=emitter,
        )
        state["dossier"] = dossier
        cp = save_checkpoint(stage_completed="stage_3b")
        if on_stage_complete is not None:
            on_stage_complete("stage_3b", cp)
        return dossier

    def _load_resume_checkpoint(
        self,
        *,
        run_id: str | None,
        resume_from: SquadCheckpoint | str | None,
    ) -> SquadCheckpoint | None:
        if isinstance(resume_from, SquadCheckpoint):
            return resume_from
        if isinstance(resume_from, str) and resume_from.strip():
            loaded = self._checkpoint_store.load(resume_from.strip())
            if loaded is None:
                raise SquadExecutionError(
                    f"Checkpoint for run_id={resume_from!r} was not found.",
                    code="checkpoint_not_found",
                    stage="resume",
                )
            return loaded
        if run_id:
            return self._checkpoint_store.load(run_id)
        return None

    # --------------------------------------------------------------------------- Stage 0 Implementation
    def _run_stage_0_prefetch(
        self,
        request: SquadPrefetchRequest,
        *,
        ledger: SquadExecutionLedger,
        emitter: OrgGraphEmitter,
        source_texts: dict[str, str],
        source_permissions: dict[str, dict[str, bool]],
    ) -> EarningsPrefetchSnapshot:
        ledger.ensure_running(stage="stage_0_prefetch")
        if self._prefetch_deps.precomputed_snapshot is not None:
            snap = self._prefetch_deps.precomputed_snapshot
            emitter.emit(
                stage="stage_0_prefetch",
                node_id="prefetch_engine",
                action_kind="RESPONDING",
                runtime_state="completed",
                current_activity=f"Loaded Stage 0 prefetch snapshot for {snap.symbol or snap.entity_id}.",
            )
            return snap

        emitter.emit(
            stage="stage_0_prefetch",
            node_id="prefetch_engine",
            action_kind="READING",
            runtime_state="running",
            current_activity=f"Running deterministic parallel Stage 0 prefetch for {request.symbol or request.cik or request.entity_id}.",
        )

        sec = self._prefetch_deps.sec_adapter or (
            self._prefetch_deps.sec_deep.sec_adapter if self._prefetch_deps.sec_deep else None
        )
        ext = self._prefetch_deps.external_earnings
        as_of_d = _as_of_date(request.as_of)

        cik = cik10(request.cik) if request.cik else None
        symbol = request.symbol.strip().upper() if request.symbol else None
        company_name = request.company_name
        sic_code = str(request.sic_code).strip() if request.sic_code is not None else None
        sic_description = request.sic_description
        filings_list: list[Filing] = []
        raw_sub_doc: Mapping[str, Any] | None = None
        gaps: list[SquadGap] = []

        if sec is not None:
            if not cik and symbol:
                try:
                    id_res = sec.company_identity(symbol)
                    if id_res.status in ("ok", "partial") and id_res.data:
                        ref = id_res.data[0]
                        cik = cik10(ref.identifiers["cik"])
                        company_name = company_name or ref.company_name
                except Exception as exc:
                    gaps.append(SquadGap(
                        stage="stage_0_prefetch",
                        code="sec_identity_lookup_failed",
                        message=f"SEC identity resolution failed: {exc}",
                        source="sec_edgar",
                    ))
            if cik:
                try:
                    meta, raw_sub_doc, filings_list = _fetch_sec_submissions_meta(sec, cik)
                    company_name = company_name or meta.get("name")
                    sic_code = sic_code or (str(meta["sic"]) if meta.get("sic") is not None else None)
                    sic_description = sic_description or meta.get("sic_description")
                    if not symbol and meta.get("tickers"):
                        symbol = str(meta["tickers"][0]).upper()
                except Exception as exc:
                    gaps.append(SquadGap(
                        stage="stage_0_prefetch",
                        code="sec_submissions_failed",
                        message=f"SEC submissions metadata fetch failed: {exc}",
                        source="sec_edgar",
                    ))

        entity_id = request.entity_id or (f"cik:{cik}" if cik else (f"ticker:{symbol}" if symbol else "us:unknown"))
        sector_app = evaluate_sec_sic_gate(sic_code, sic_description)

        cur_filing, pri_filing = _pick_current_prior_periodic_filings(
            filings_list, as_of=as_of_d, preferred_form=request.filing_form
        )
        filing_acc = request.filing_accession or (cur_filing.accession if cur_filing else None)
        prior_acc = request.prior_filing_accession or (pri_filing.accession if pri_filing else None)
        cur_url = request.current_filing_url or (cur_filing.primary_document_url if cur_filing else None)
        pri_url = request.prior_filing_url or (pri_filing.primary_document_url if pri_filing else None)

        periods: list[PeriodFinancials] = list(request.period_financials)
        derived_metrics: dict[str, Any] = {}
        diagnostics_res: EarningsQualityDiagnosticsResult | None = None
        earnings_release: DualPeriodEarningsRelease | None = None
        table_catalog: FilingTableCatalog | None = None
        redline_report: FilingRedlineReport | None = None
        consensus_summary = ConsensusSnapshotSummary(
            status="unavailable",
            unavailable_reason="external_earnings_unconfigured",
            fallback_suggestions=list(CONSENSUS_FALLBACK_SUGGESTIONS),
        )
        transcript_index = TranscriptIndexEntry(
            status="unavailable",
            symbol=symbol,
            unavailable_reason="external_earnings_unconfigured",
            fallback_suggestions=list(TRANSCRIPT_FALLBACK_SUGGESTIONS),
        )
        close_price_dec: Decimal | None = None
        if request.close_price is not None:
            close_price_dec = Decimal(str(request.close_price))

        prefetched_sources: list[PrefetchedSourceRef] = []

        def task_statements_and_diagnostics() -> tuple[list[PeriodFinancials], dict[str, Any], EarningsQualityDiagnosticsResult | None, list[SquadGap]]:
            local_gaps: list[SquadGap] = []
            local_periods = list(periods)
            local_derived: dict[str, Any] = {}
            if not local_periods and sec is not None and cik:
                local_periods, local_derived = _build_periods_from_sec(sec, cik, entity_id=entity_id, as_of=as_of_d)
            elif not local_periods and self._tool_runner is not None and (cik or symbol or entity_id):
                res = self._tool_runner.call_tool("company_financials", {
                    "entity_id": entity_id,
                    **({"cik": cik} if cik else {}),
                    **({"ticker": symbol} if symbol else {}),
                    "periods": "last_8_quarters",
                    "derive_q4": True,
                })
                if res.get("status") not in ("ok", "partial"):
                    local_gaps.append(SquadGap(
                        stage="stage_0_prefetch",
                        code="financial_statements_unavailable",
                        message=str(res.get("message") or res.get("reason") or "SEC XBRL statements unavailable."),
                        source="sec_edgar",
                    ))
            diag: EarningsQualityDiagnosticsResult | None = None
            if local_periods:
                cur_p = local_periods[0]
                qoq_p = local_periods[1] if len(local_periods) > 1 else None
                yoy_p = local_periods[4] if len(local_periods) > 4 else None
                ttm_p = _compute_ttm_period([p for p in local_periods if p.period_type == "quarter"])
                diag = earnings_quality_diagnostics(EarningsQualityRequest(
                    entity_id=entity_id,
                    symbol=symbol,
                    sic=sic_code,
                    sic_description=sic_description,
                    current=cur_p,
                    prior_qoq=qoq_p,
                    prior_yoy=yoy_p,
                    ttm=ttm_p,
                ))
            return local_periods, local_derived, diag, local_gaps

        def task_earnings_release() -> tuple[DualPeriodEarningsRelease | None, list[SquadGap]]:
            if sec is None or not cik:
                return None, []
            sub_arg: Any = raw_sub_doc if raw_sub_doc is not None else (filings_list or None)
            res = sec_earnings_release(sec, cik, submissions=sub_arg, as_of=as_of_d)
            if res.status in ("ok", "partial") and res.data:
                er: DualPeriodEarningsRelease = res.data[0]
                er_gaps = [
                    SquadGap(stage="stage_0_prefetch", code="8k_release_partial", message=g, source="sec_earnings_release")
                    for g in er.gaps
                ]
                return er, er_gaps
            return None, [SquadGap(
                stage="stage_0_prefetch",
                code="8k_release_unavailable",
                message=res.message or f"8-K Item 2.02 release unavailable ({res.reason}).",
                source="sec_earnings_release",
            )]

        def task_filing_catalog() -> tuple[FilingTableCatalog | None, list[SquadGap]]:
            if sec is None or not cik or not filing_acc:
                return None, []
            res = sec_filing_tables(sec, cik, filing_acc)
            if res.status in ("ok", "partial") and res.data:
                return res.data[0], []
            return None, [SquadGap(
                stage="stage_0_prefetch",
                code="filing_summary_unavailable",
                message=res.message or f"FilingSummary.xml unavailable ({res.reason}).",
                source="sec_filing_tables",
            )]

        def task_redline() -> tuple[FilingRedlineReport | None, FilingSectionsBundle | None, list[SquadGap]]:
            if sec is None or not cur_url or not pri_url:
                return None, None, []
            c_res = sec_filing_sections(sec, url=cur_url, form=cur_filing.form if cur_filing else request.filing_form)
            p_res = sec_filing_sections(sec, url=pri_url, form=pri_filing.form if pri_filing else request.filing_form)
            if c_res.status in ("ok", "partial") and c_res.data and p_res.status in ("ok", "partial") and p_res.data:
                c_bundle: FilingSectionsBundle = c_res.data[0]
                p_bundle: FilingSectionsBundle = p_res.data[0]
                report = sec_redline_diff(c_bundle, p_bundle)
                assert isinstance(report, FilingRedlineReport)
                return report, c_bundle, []
            return None, None, [SquadGap(
                stage="stage_0_prefetch",
                code="redline_unavailable",
                message="Could not compute cross-period redline diff from prior filing sections.",
                source="sec_redline_diff",
            )]

        def task_external_consensus() -> tuple[ConsensusSnapshotSummary, list[SquadGap]]:
            if ext is None or not symbol:
                return consensus_summary, [SquadGap(
                    stage="stage_0_prefetch",
                    code="consensus_unavailable",
                    message="FMP consensus service not configured; falling back to 8-K 99.1 guidance.",
                    source="earnings_consensus",
                    fallback=CONSENSUS_FALLBACK_SUGGESTIONS[0],
                )]
            res = ext.get_consensus(symbol, byok_api_key=request.fmp_byok_api_key, account_id=request.account_id)
            if res.status in ("ok", "partial") and res.consensus:
                return ConsensusSnapshotSummary(
                    status="available",
                    records=list(res.consensus),
                    provider=res.provider,
                    permissions=res.permissions.model_dump(mode="json"),
                ), []
            safe_msg = redact_secrets(res.message or res.reason, (request.fmp_byok_api_key,) if request.fmp_byok_api_key else ())
            return ConsensusSnapshotSummary(
                status="unavailable",
                provider=res.provider,
                unavailable_reason=res.reason,
                fallback_suggestions=list(res.fallback_suggestions),
            ), [SquadGap(
                stage="stage_0_prefetch",
                code=f"consensus_{res.reason}",
                message=safe_msg,
                source="earnings_consensus",
                fallback=res.fallback_suggestions[0] if res.fallback_suggestions else CONSENSUS_FALLBACK_SUGGESTIONS[0],
            )]

        def task_external_transcript() -> tuple[TranscriptIndexEntry, str | None, list[SquadGap]]:
            if ext is None or not symbol:
                return transcript_index, None, [SquadGap(
                    stage="stage_0_prefetch",
                    code="transcript_unavailable",
                    message="FMP transcript service not configured; prompt user upload or rely on 8-K/MD&A.",
                    source="earnings_transcript",
                    fallback=TRANSCRIPT_FALLBACK_SUGGESTIONS[0],
                )]
            res = ext.get_transcript(
                symbol,
                fiscal_year=request.fiscal_year,
                fiscal_quarter=request.fiscal_quarter,
                byok_api_key=request.fmp_byok_api_key,
                account_id=request.account_id,
            )
            if res.status in ("ok", "partial") and res.transcript is not None:
                tr: EarningsTranscript = res.transcript
                sid = f"src_transcript_{tr.symbol}_{tr.fiscal_year}Q{tr.fiscal_quarter}"
                perms_dict = tr.permissions.model_dump(mode="json")
                return TranscriptIndexEntry(
                    status="available",
                    symbol=tr.symbol,
                    fiscal_year=tr.fiscal_year,
                    fiscal_quarter=tr.fiscal_quarter,
                    call_date=tr.call_date,
                    sections=[s.name for s in tr.sections],
                    speakers=list(tr.speakers),
                    content_sha256=tr.content_sha256,
                    source_id=sid,
                    provider=res.provider,
                    permissions=perms_dict,
                ), tr.full_text, []
            safe_msg = redact_secrets(res.message or res.reason, (request.fmp_byok_api_key,) if request.fmp_byok_api_key else ())
            return TranscriptIndexEntry(
                status="unavailable",
                symbol=symbol,
                provider=res.provider,
                unavailable_reason=res.reason,
                fallback_suggestions=list(res.fallback_suggestions),
            ), None, [SquadGap(
                stage="stage_0_prefetch",
                code=f"transcript_{res.reason}",
                message=safe_msg,
                source="earnings_transcript",
                fallback=res.fallback_suggestions[0] if res.fallback_suggestions else TRANSCRIPT_FALLBACK_SUGGESTIONS[0],
            )]

        def task_market_price() -> Decimal | None:
            if close_price_dec is not None:
                return close_price_dec
            if self._prefetch_deps.market_price_fn is not None:
                val = self._prefetch_deps.market_price_fn(entity_id, symbol)
                if val is not None:
                    return Decimal(str(val))
            return None

        # Execute independent Stage 0 fetch tasks concurrently in bounded ThreadPoolExecutor.
        with ThreadPoolExecutor(max_workers=request.max_prefetch_workers, thread_name_prefix="squad_s0") as pool:
            f_diag = pool.submit(task_statements_and_diagnostics)
            f_er = pool.submit(task_earnings_release)
            f_cat = pool.submit(task_filing_catalog)
            f_red = pool.submit(task_redline)
            f_cons = pool.submit(task_external_consensus)
            f_tr = pool.submit(task_external_transcript)
            f_px = pool.submit(task_market_price)

            ledger.ensure_running(stage="stage_0_prefetch")
            periods, derived_metrics, diagnostics_res, g_diag = f_diag.result()
            gaps.extend(g_diag)

            earnings_release, g_er = f_er.result()
            gaps.extend(g_er)
            if earnings_release is not None:
                er_chunks: list[str] = []
                for p in (earnings_release.current, earnings_release.prior):
                    if p is None:
                        continue
                    if p.management_summary:
                        er_chunks.append(p.management_summary)
                    for gp in p.guidance_passages:
                        er_chunks.append(gp.text)
                    if p.ex99_1 is not None:
                        er_chunks.append(p.ex99_1.text)
                if er_chunks:
                    er_text = "\n\n".join(er_chunks)
                    er_sid = f"src_8k_release_{cik or entity_id}"
                    er_perms = {"store": True, "display": True, "model_input": True, "export": True}
                    source_texts[er_sid] = er_text
                    source_permissions[er_sid] = er_perms
                    prefetched_sources.append(PrefetchedSourceRef(
                        source_id=er_sid,
                        title=f"{symbol or entity_id} SEC 8-K Item 2.02 Earnings Release",
                        kind="official",
                        retrieval_method="sec_earnings_release",
                        content_sha256=sha256(er_text.encode("utf-8")).hexdigest(),
                        permissions=er_perms,
                        total_characters=len(er_text),
                    ))

            table_catalog, g_cat = f_cat.result()
            gaps.extend(g_cat)

            redline_report, cur_sections_bundle, g_red = f_red.result()
            gaps.extend(g_red)
            if cur_sections_bundle is not None and cur_sections_bundle.clean_text:
                sec_sid = f"src_sec_sections_{filing_acc or cik or entity_id}"
                sec_perms = {"store": True, "display": True, "model_input": True, "export": True}
                source_texts[sec_sid] = cur_sections_bundle.clean_text
                source_permissions[sec_sid] = sec_perms
                prefetched_sources.append(PrefetchedSourceRef(
                    source_id=sec_sid,
                    title=f"{symbol or entity_id} SEC {cur_sections_bundle.form or 'Filing'} Sections",
                    kind="official",
                    retrieval_method="sec_filing_sections",
                    content_sha256=cur_sections_bundle.content_sha256,
                    permissions=sec_perms,
                    total_characters=len(cur_sections_bundle.clean_text),
                ))

            consensus_summary, g_cons = f_cons.result()
            gaps.extend(g_cons)

            transcript_index, tr_text, g_tr = f_tr.result()
            gaps.extend(g_tr)
            if transcript_index.status == "available" and transcript_index.source_id and tr_text:
                source_texts[transcript_index.source_id] = tr_text
                source_permissions[transcript_index.source_id] = dict(transcript_index.permissions)
                prefetched_sources.append(PrefetchedSourceRef(
                    source_id=transcript_index.source_id,
                    title=f"{symbol} Earnings Call Transcript (FY{transcript_index.fiscal_year} Q{transcript_index.fiscal_quarter})",
                    kind="market_data",
                    retrieval_method="fmp_transcript",
                    content_sha256=transcript_index.content_sha256 or sha256(tr_text.encode("utf-8")).hexdigest(),
                    permissions=dict(transcript_index.permissions),
                    total_characters=len(tr_text),
                ))

            try:
                close_price_dec = f_px.result()
            except Exception as exc:
                gaps.append(SquadGap(
                    stage="stage_0_prefetch",
                    code="market_price_failed",
                    message=f"Market price fetch failed: {exc}",
                    source="market_data",
                ))

        # Capture any sources already persisted in FinToolboxToolRunner
        if isinstance(self._tool_runner, FinToolboxToolRunner):
            for sid, src in self._tool_runner.source_by_id.items():
                source_texts[sid] = src.text
                perms = src.permissions.model_dump(mode="json")
                source_permissions[sid] = perms
                prefetched_sources.append(PrefetchedSourceRef(
                    source_id=sid,
                    title=src.title,
                    kind=src.kind,
                    retrieval_method=src.retrieval_method,
                    content_sha256=src.content_sha256,
                    url=str(src.url) if src.url else None,
                    permissions=perms,
                    total_characters=len(src.text),
                ))

        # Deterministic guidance & Non-GAAP reconciliation (if structured inputs supplied)
        guidance_res: GuidanceAndNonGaapResult | None = None
        if request.guidance_inputs or request.nongaap_bridges:
            guidance_res = guidance_and_nongaap_reconciler(GuidanceAndNonGaapRequest(
                entity_id=entity_id,
                symbol=symbol,
                period_label=request.period_label or (periods[0].period_label if periods else "current"),
                guidance_metrics=list(request.guidance_inputs),
                nongaap_bridges=list(request.nongaap_bridges),
            ))

        # Deterministic valuation & Reverse-DCF
        val_req = _valuation_request_from_snapshot(
            request,
            entity_id=entity_id,
            symbol=symbol,
            sic=sic_code,
            sic_description=sic_description,
            periods=periods,
            close_price=close_price_dec,
            consensus_records=consensus_summary.records,
        )
        valuation_res = valuation_and_implied_expectations(val_req)
        if valuation_res.reverse_dcf.status == "unavailable" and valuation_res.reverse_dcf.reason:
            gaps.append(SquadGap(
                stage="stage_0_prefetch",
                code="reverse_dcf_unavailable",
                message=valuation_res.reverse_dcf.reason,
                source="valuation_and_implied_expectations",
            ))

        resolved_period = request.period_label or (periods[0].period_label if periods else None)
        snapshot = EarningsPrefetchSnapshot(
            entity_id=entity_id,
            symbol=symbol,
            cik=cik,
            company_name=company_name,
            sic_code=sector_app.sic_code,
            sic_description=sic_description,
            period_label=resolved_period,
            as_of=request.as_of,
            sector_applicability=sector_app,
            statements=periods,
            derived_metrics=derived_metrics,
            diagnostics=diagnostics_res,
            flags=list(diagnostics_res.flags) if diagnostics_res else [],
            earnings_release=earnings_release,
            filing_accession=filing_acc,
            prior_filing_accession=prior_acc,
            filing_table_catalog=table_catalog,
            redline_summary=redline_report,
            guidance_nongaap=guidance_res,
            valuation=valuation_res,
            consensus_summary=consensus_summary,
            transcript_index=transcript_index,
            prefetched_sources=prefetched_sources,
            open_gaps=gaps,
        )

        emitter.emit(
            stage="stage_0_prefetch",
            node_id="prefetch_engine",
            action_kind="RESPONDING",
            runtime_state="completed",
            current_activity=(
                f"Stage 0 complete: {len(snapshot.flags)} R/Y/G flag(s), "
                f"{len(snapshot.open_gaps)} open gap(s), 0 model calls."
            ),
        )
        return snapshot

    # --------------------------------------------------------------------------- Stage 1 Implementation
    def _run_stage_1_plan(
        self,
        *,
        request: SquadPrefetchRequest,
        snapshot: EarningsPrefetchSnapshot,
        ledger: SquadExecutionLedger,
        emitter: OrgGraphEmitter,
        instruction_seq: int,
    ) -> ResearchDispatchPlan:
        ledger.ensure_running(stage="stage_1_plan")
        pack_ctx = resolve_method_pack_context(snapshot.sic_code)

        emitter.emit(
            stage="stage_1_plan",
            node_id="lead_coordinator",
            action_kind="INSPECTING",
            runtime_state="running",
            current_activity="Lead Coordinator inspecting Stage 0 snapshot and selecting L2 KPI pack.",
            slot="deep_reasoner",
        )

        sys_prompt = (
            "You are the Lead Coordinator (deep_reasoner) of a bounded US Buy-Side Earnings Research Squad. "
            "Inspect the Stage 0 EarningsPrefetchSnapshot, SIC sector gate, and L1/L2 method pack hint. "
            "Produce a ResearchDispatchPlan JSON with selected_l2_pack_id and 1-3 read-only WorkerTask items "
            "for worker_segments, worker_forensics, and worker_guidance. Explicitly mark a worker task as "
            "status='skipped' with skip_reason when its required primary source is unavailable."
        )
        user_payload = {
            "question": request.question,
            "instruction_sequence": instruction_seq,
            "method_pack_hint": {
                "l1_pack_id": pack_ctx["l1_pack_id"],
                "suggested_l2_pack_id": pack_ctx["l2_pack_id"],
                "reason": pack_ctx["reason"],
                "recommended_alternatives": pack_ctx["recommended_alternatives"],
            },
            "snapshot_summary": {
                "entity_id": snapshot.entity_id,
                "symbol": snapshot.symbol,
                "cik": snapshot.cik,
                "sic_code": snapshot.sic_code,
                "sector_applicability": snapshot.sector_applicability.model_dump(mode="json"),
                "flags": [f.model_dump(mode="json") for f in snapshot.flags],
                "has_filing_catalog": snapshot.filing_table_catalog is not None,
                "has_earnings_release": snapshot.earnings_release is not None,
                "has_redline": snapshot.redline_summary is not None,
                "consensus_status": snapshot.consensus_summary.status,
                "transcript_status": snapshot.transcript_index.status,
                "open_gaps": [g.model_dump(mode="json") for g in snapshot.open_gaps],
            },
        }
        user_prompt = json.dumps(user_payload, ensure_ascii=False, sort_keys=True, default=str)

        call_id, before = ledger.admit_model_call("deep_reasoner", stage="stage_1_plan")
        t0 = monotonic()
        failed = False
        usage: ModelUsage | None = None
        try:
            resp = self._model_runner.invoke(SlotModelRequest(
                slot="deep_reasoner",
                stage="stage_1_plan",
                role="lead_coordinator",
                system_prompt=sys_prompt,
                user_prompt=user_prompt,
                messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
                response_schema_name="ResearchDispatchPlan",
                response_schema=ResearchDispatchPlan.model_json_schema(),
                binding=self._slot_bindings["deep_reasoner"],
            ))
            usage = resp.usage
            plan = _parse_model_output(resp, ResearchDispatchPlan)
            if plan.instruction_sequence != instruction_seq:
                plan = plan.model_copy(update={"instruction_sequence": instruction_seq})
        except Exception as exc:
            failed = True
            emitter.emit(
                stage="stage_1_plan",
                node_id="lead_coordinator",
                action_kind="FAILED",
                runtime_state="failed",
                current_activity="Lead Coordinator failed to produce ResearchDispatchPlan.",
                blocker_text=str(exc),
                slot="deep_reasoner",
            )
            raise
        finally:
            ledger.settle_model_call("deep_reasoner", call_id, before, usage=usage, elapsed_ms=int((monotonic() - t0) * 1000), failed=failed)

        for task in plan.tasks:
            if task.status == "skipped":
                emitter.emit(
                    stage="stage_1_plan",
                    node_id=task.worker_role,
                    action_kind="BLOCKED",
                    runtime_state="skipped",
                    current_activity=f"Skipped {task.worker_role}.",
                    blocker_text=task.skip_reason,
                    edge_event=OrgEdgeEvent(
                        from_node_id="lead_coordinator",
                        to_node_id=task.worker_role,
                        is_review=False,
                        edge_kind="dispatch",
                        label=f"Skipped: {task.skip_reason}",
                    ),
                )
            else:
                emitter.emit(
                    stage="stage_1_plan",
                    node_id="lead_coordinator",
                    action_kind="DELEGATING",
                    runtime_state="running",
                    current_activity=f"Dispatched {task.worker_role}: {task.objective[:120]}",
                    edge_event=OrgEdgeEvent(
                        from_node_id="lead_coordinator",
                        to_node_id=task.worker_role,
                        is_review=False,
                        edge_kind="dispatch",
                        label=task.objective[:120],
                    ),
                    slot="deep_reasoner",
                )
        return plan

    # --------------------------------------------------------------------------- Stage 2 Workers & Mid-Run Steering
    def _run_stage_2_with_steering(
        self,
        *,
        request: SquadPrefetchRequest,
        snapshot: EarningsPrefetchSnapshot,
        dispatch_plan: ResearchDispatchPlan,
        state: dict[str, Any],
        cp_lock: RLock,
        save_checkpoint: Callable[..., SquadCheckpoint],
        ledger: SquadExecutionLedger,
        emitter: OrgGraphEmitter,
        instruction_provider: Callable[[], Sequence[UserSteeringInstruction | Mapping[str, Any]]] | None,
        on_worker_complete: Callable[[WorkerRole, EvidenceCardBatch, SquadCheckpoint], None] | None,
    ) -> None:
        task_by_role: dict[WorkerRole, WorkerTask] = {t.worker_role: t for t in dispatch_plan.tasks}
        tasks_to_run: dict[WorkerRole, WorkerTask] = {}

        for role, task in task_by_role.items():
            key = f"round1:{role}"
            existing: EvidenceCardBatch | None = state["worker_batches"].get(key)
            if existing is not None and existing.status in ("completed", "skipped"):
                emitter.emit(
                    stage="stage_2_workers",
                    node_id=role,
                    action_kind="RESPONDING",
                    runtime_state="completed" if existing.status == "completed" else "skipped",
                    current_activity=f"Reused {role} EvidenceCardBatch from checkpoint (0 calls).",
                    slot="fast_extractor",
                )
                continue
            if task.status == "skipped":
                state["worker_batches"][key] = EvidenceCardBatch(
                    task_id=task.task_id,
                    worker_role=role,
                    round_number=1,
                    status="skipped",
                    unresolved_gaps=[task.skip_reason or "skipped"],
                )
                continue
            tasks_to_run[role] = task

        if not tasks_to_run:
            return

        emitter.emit(
            stage="stage_2_workers",
            node_id="lead_coordinator",
            action_kind="WAITING_SUBORDINATES",
            runtime_state="waiting",
            current_activity=f"Waiting for {len(tasks_to_run)} parallel read-only worker(s).",
            slot="deep_reasoner",
        )

        with ThreadPoolExecutor(max_workers=self._max_worker_concurrency, thread_name_prefix="squad_s2") as pool:
            active_futures: dict[Future[EvidenceCardBatch], tuple[WorkerRole, WorkerTask, Event]] = {}
            for role, task in tasks_to_run.items():
                w_cancel = Event()
                fut = pool.submit(
                    self._execute_worker_task,
                    request=request,
                    snapshot=snapshot,
                    task=task,
                    l2_pack_id=dispatch_plan.selected_l2_pack_id,
                    ledger=ledger,
                    emitter=emitter,
                    worker_cancel=w_cancel,
                    source_texts=state["source_texts"],
                    source_permissions=state["source_permissions"],
                )
                fut.add_done_callback(lambda _f: self._steering_wakeup.set())
                active_futures[fut] = (role, task, w_cancel)

            while active_futures:
                ledger.ensure_running(stage="stage_2_workers")
                done_futs = [f for f in active_futures if f.done()]
                for fut in done_futs:
                    role, task, w_cancel = active_futures.pop(fut)
                    batch = self._collect_worker_future(fut, role=role, task=task, emitter=emitter)
                    if batch.status == "cancelled" and any(
                        r == role for _, (r, _, _) in active_futures.items()
                    ):
                        # Already replaced by steering re-dispatch
                        continue
                    with cp_lock:
                        state["worker_batches"][f"round1:{role}"] = batch
                        if batch.status == "failed":
                            state["open_gaps"] = _merge_gaps(state["open_gaps"], [SquadGap(
                                stage="stage_2_workers",
                                worker_role=role,
                                code="worker_failed",
                                message=batch.error or f"{role} failed during Stage 2.",
                            )])
                        elif batch.status == "partial" or batch.unresolved_gaps:
                            for gtxt in batch.unresolved_gaps:
                                state["open_gaps"] = _merge_gaps(state["open_gaps"], [SquadGap(
                                    stage="stage_2_workers",
                                    worker_role=role,
                                    code="worker_partial_gap",
                                    message=gtxt,
                                )])
                    cp = save_checkpoint()
                    if on_worker_complete is not None:
                        on_worker_complete(role, batch, cp)

                # Evaluate mid-run steering instructions incrementally
                pending_instr = self._collect_pending_instructions(
                    state["applied_instruction_sequences"], instruction_provider
                )
                if pending_instr:
                    running_roles = {r for _, (r, _, _) in active_futures.items()}
                    decision = self._evaluate_steering_impact(
                        request=request,
                        snapshot=snapshot,
                        dispatch_plan=dispatch_plan,
                        completed_batches=state["worker_batches"],
                        running_roles=running_roles,
                        instructions=pending_instr,
                        ledger=ledger,
                        emitter=emitter,
                    )
                    for instr in pending_instr:
                        state["applied_instruction_sequences"].add(instr.sequence)

                    # Cancel only running affected workers
                    for fut, (r, _t, w_cancel) in list(active_futures.items()):
                        if r in decision.cancel_running_workers or r in decision.affected_workers:
                            w_cancel.set()
                            emitter.emit(
                                stage="steering",
                                node_id=r,
                                action_kind="BLOCKED",
                                runtime_state="cancelled",
                                current_activity=f"Cancelled in-flight {r} due to mid-run steering.",
                                blocker_text=decision.rationale or "Mid-run steering override",
                                slot="fast_extractor",
                            )
                            active_futures.pop(fut, None)

                    # Re-dispatch ONLY affected workers
                    updated_map = {t.worker_role: t for t in decision.updated_tasks}
                    latest_seq = max(i.sequence for i in pending_instr)
                    for role in decision.affected_workers:
                        base_task = updated_map.get(role) or task_by_role.get(role)
                        if base_task is None:
                            continue
                        steered_task = base_task.model_copy(update={
                            "task_id": _new_id("task"),
                            "status": "planned",
                            "steering_instruction_sequence": latest_seq,
                        })
                        task_by_role[role] = steered_task
                        w_cancel = Event()
                        emitter.emit(
                            stage="steering",
                            node_id="lead_coordinator",
                            action_kind="DELEGATING",
                            runtime_state="running",
                            current_activity=f"Re-dispatching affected worker {role} for instruction #{latest_seq}.",
                            edge_event=OrgEdgeEvent(
                                from_node_id="lead_coordinator",
                                to_node_id=role,
                                is_review=False,
                                edge_kind="steering",
                                label=f"Steering #{latest_seq}",
                            ),
                            slot="deep_reasoner",
                        )
                        fut = pool.submit(
                            self._execute_worker_task,
                            request=request,
                            snapshot=snapshot,
                            task=steered_task,
                            l2_pack_id=dispatch_plan.selected_l2_pack_id,
                            ledger=ledger,
                            emitter=emitter,
                            worker_cancel=w_cancel,
                            source_texts=state["source_texts"],
                            source_permissions=state["source_permissions"],
                        )
                        fut.add_done_callback(lambda _f: self._steering_wakeup.set())
                        active_futures[fut] = (role, steered_task, w_cancel)

                if active_futures:
                    self._steering_wakeup.wait(timeout=0.02)
                    self._steering_wakeup.clear()

    def _handle_post_stage2_steering(
        self,
        *,
        request: SquadPrefetchRequest,
        snapshot: EarningsPrefetchSnapshot,
        dispatch_plan: ResearchDispatchPlan,
        state: dict[str, Any],
        cp_lock: RLock,
        save_checkpoint: Callable[..., SquadCheckpoint],
        ledger: SquadExecutionLedger,
        emitter: OrgGraphEmitter,
        instruction_provider: Callable[[], Sequence[UserSteeringInstruction | Mapping[str, Any]]] | None,
        on_worker_complete: Callable[[WorkerRole, EvidenceCardBatch, SquadCheckpoint], None] | None,
    ) -> None:
        pending_instr = self._collect_pending_instructions(
            state["applied_instruction_sequences"], instruction_provider
        )
        if not pending_instr:
            return
        decision = self._evaluate_steering_impact(
            request=request,
            snapshot=snapshot,
            dispatch_plan=dispatch_plan,
            completed_batches=state["worker_batches"],
            running_roles=set(),
            instructions=pending_instr,
            ledger=ledger,
            emitter=emitter,
        )
        for instr in pending_instr:
            state["applied_instruction_sequences"].add(instr.sequence)

        if not decision.affected_workers:
            save_checkpoint()
            return

        if decision.invalidate_stage_2_5:
            state["completed_stages"] = [s for s in state["completed_stages"] if s not in ("stage_2_5", "stage_3a", "stage_3b")]
            state["stage25_executed"] = False
            state["stage25_decision"] = None
        if decision.invalidate_stage_3a:
            state["completed_stages"] = [s for s in state["completed_stages"] if s not in ("stage_3a", "stage_3b")]
            state["bear_batch"] = None
            state["dossier"] = None

        task_by_role = {t.worker_role: t for t in dispatch_plan.tasks}
        updated_map = {t.worker_role: t for t in decision.updated_tasks}
        latest_seq = max(i.sequence for i in pending_instr)
        affected_tasks: list[WorkerTask] = []
        for role in decision.affected_workers:
            base_task = updated_map.get(role) or task_by_role.get(role)
            if base_task is None:
                continue
            affected_tasks.append(base_task.model_copy(update={
                "task_id": _new_id("task"),
                "status": "planned",
                "steering_instruction_sequence": latest_seq,
            }))

        with ThreadPoolExecutor(max_workers=min(3, max(1, len(affected_tasks))), thread_name_prefix="squad_steer") as pool:
            futs: dict[Future[EvidenceCardBatch], WorkerTask] = {}
            for steered_task in affected_tasks:
                emitter.emit(
                    stage="steering",
                    node_id="lead_coordinator",
                    action_kind="DELEGATING",
                    runtime_state="running",
                    current_activity=f"Re-dispatching {steered_task.worker_role} for instruction #{latest_seq}.",
                    edge_event=OrgEdgeEvent(
                        from_node_id="lead_coordinator",
                        to_node_id=steered_task.worker_role,
                        is_review=False,
                        edge_kind="steering",
                        label=f"Steering #{latest_seq}",
                    ),
                    slot="deep_reasoner",
                )
                fut = pool.submit(
                    self._execute_worker_task,
                    request=request,
                    snapshot=snapshot,
                    task=steered_task,
                    l2_pack_id=dispatch_plan.selected_l2_pack_id,
                    ledger=ledger,
                    emitter=emitter,
                    worker_cancel=Event(),
                    source_texts=state["source_texts"],
                    source_permissions=state["source_permissions"],
                )
                futs[fut] = steered_task
            for fut, steered_task in futs.items():
                batch = self._collect_worker_future(fut, role=steered_task.worker_role, task=steered_task, emitter=emitter)
                with cp_lock:
                    state["worker_batches"][f"round1:{steered_task.worker_role}"] = batch
                cp = save_checkpoint()
                if on_worker_complete is not None:
                    on_worker_complete(steered_task.worker_role, batch, cp)

    def _evaluate_steering_impact(
        self,
        *,
        request: SquadPrefetchRequest,
        snapshot: EarningsPrefetchSnapshot,
        dispatch_plan: ResearchDispatchPlan,
        completed_batches: Mapping[str, EvidenceCardBatch],
        running_roles: set[WorkerRole],
        instructions: Sequence[UserSteeringInstruction],
        ledger: SquadExecutionLedger,
        emitter: OrgGraphEmitter,
    ) -> SteeringImpactDecision:
        emitter.emit(
            stage="steering",
            node_id="lead_coordinator",
            action_kind="INSPECTING",
            runtime_state="running",
            current_activity=f"Evaluating mid-run user steering ({len(instructions)} new instruction(s)).",
            slot="deep_reasoner",
        )
        sys_prompt = (
            "You are the Lead Coordinator handling mid-run user steering instructions. "
            "Incrementally evaluate impact and return SteeringImpactDecision JSON identifying ONLY "
            "affected_workers (and cancel_running_workers / updated_tasks). Do NOT re-dispatch "
            "unrelated completed workers."
        )
        user_prompt = json.dumps({
            "question": request.question,
            "new_instructions": [i.model_dump(mode="json") for i in instructions],
            "current_plan": dispatch_plan.model_dump(mode="json"),
            "running_workers": sorted(running_roles),
            "completed_workers": {
                k: {"status": v.status, "card_ids": [c.card_id for c in v.cards]}
                for k, v in completed_batches.items()
            },
            "snapshot_symbol": snapshot.symbol,
        }, ensure_ascii=False, sort_keys=True, default=str)
        call_id, before = ledger.admit_model_call("deep_reasoner", stage="steering")
        t0 = monotonic()
        failed = False
        usage: ModelUsage | None = None
        try:
            resp = self._model_runner.invoke(SlotModelRequest(
                slot="deep_reasoner",
                stage="steering",
                role="lead_coordinator",
                system_prompt=sys_prompt,
                user_prompt=user_prompt,
                messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
                response_schema_name="SteeringImpactDecision",
                response_schema=SteeringImpactDecision.model_json_schema(),
                binding=self._slot_bindings["deep_reasoner"],
            ))
            usage = resp.usage
            decision = _parse_model_output(resp, SteeringImpactDecision)
            if not decision.instruction_sequences:
                decision = decision.model_copy(update={
                    "instruction_sequences": [i.sequence for i in instructions]
                })
            return decision
        except Exception:
            failed = True
            raise
        finally:
            ledger.settle_model_call("deep_reasoner", call_id, before, usage=usage, elapsed_ms=int((monotonic() - t0) * 1000), failed=failed)

    def _collect_worker_future(
        self,
        fut: Future[EvidenceCardBatch],
        *,
        role: WorkerRole,
        task: WorkerTask,
        emitter: OrgGraphEmitter,
    ) -> EvidenceCardBatch:
        try:
            return fut.result()
        except SquadCancelledError as exc:
            return EvidenceCardBatch(
                task_id=task.task_id,
                worker_role=role,
                round_number=task.round_number,
                status="cancelled",
                error=str(exc),
                instruction_sequence=task.steering_instruction_sequence,
            )
        except Exception as exc:
            err_msg = str(exc) or type(exc).__name__
            emitter.emit(
                stage="stage_2_workers" if task.round_number == 1 else "stage_2_5_review",
                node_id=role,
                action_kind="FAILED",
                runtime_state="failed",
                current_activity=f"{role} failed; partial survival preserving sibling workers.",
                blocker_text=err_msg,
                slot="fast_extractor",
            )
            return EvidenceCardBatch(
                task_id=task.task_id,
                worker_role=role,
                round_number=task.round_number,
                status="failed",
                unresolved_gaps=[f"{role} failed: {err_msg}"],
                error=err_msg,
                instruction_sequence=task.steering_instruction_sequence,
            )

    def _execute_worker_task(
        self,
        *,
        request: SquadPrefetchRequest,
        snapshot: EarningsPrefetchSnapshot,
        task: WorkerTask,
        l2_pack_id: str | None,
        ledger: SquadExecutionLedger,
        emitter: OrgGraphEmitter,
        worker_cancel: Event,
        source_texts: dict[str, str],
        source_permissions: dict[str, dict[str, bool]],
    ) -> EvidenceCardBatch:
        stage_name = "stage_2_workers" if task.round_number == 1 else "stage_2_5_review"
        role = task.worker_role
        allowed_set = set(task.allowed_tools) & WORKER_ALLOWED_TOOLS[role]

        def check_active() -> None:
            ledger.ensure_running(stage=stage_name)
            if worker_cancel.is_set():
                raise SquadCancelledError(f"Worker {role} cancelled.", stage=stage_name)

        check_active()
        emitter.emit(
            stage=stage_name,
            node_id=role,
            action_kind="INSPECTING",
            runtime_state="running",
            current_activity=f"Starting {role} (round {task.round_number}, limit {task.soft_turn_limit} turns).",
            slot="fast_extractor",
        )

        allowed_defs = [
            _tool_def_dict(name)
            for name in sorted(allowed_set)
            if name in TOOL_REGISTRY
        ]
        sys_prompt = (
            f"You are {role} (slot fast_extractor), a strictly read-only financial analyst worker. "
            f"Allowed tools: {sorted(allowed_set)}. You must NEVER write reports or call disallowed tools. "
            f"Return EvidenceCardBatch JSON with verified citations and supporting_fact_ids."
        )
        init_payload = {
            "question": request.question,
            "worker_role": role,
            "round_number": task.round_number,
            "l2_pack_id": l2_pack_id,
            "task": task.model_dump(mode="json"),
            "snapshot": {
                "entity_id": snapshot.entity_id,
                "symbol": snapshot.symbol,
                "cik": snapshot.cik,
                "period_label": snapshot.period_label,
                "sector_applicability": snapshot.sector_applicability.model_dump(mode="json"),
                "flags": [f.model_dump(mode="json") for f in snapshot.flags],
                "filing_accession": snapshot.filing_accession,
                "prior_filing_accession": snapshot.prior_filing_accession,
                "filing_table_catalog": (
                    snapshot.filing_table_catalog.model_dump(mode="json")
                    if snapshot.filing_table_catalog
                    else None
                ),
                "prefetched_sources": [s.model_dump(mode="json") for s in snapshot.prefetched_sources],
                "consensus_status": snapshot.consensus_summary.status,
                "transcript_index": snapshot.transcript_index.model_dump(mode="json"),
            },
        }
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": json.dumps(init_payload, ensure_ascii=False, sort_keys=True, default=str)},
        ]

        tools_invoked: list[str] = []
        blocked_attempts: list[str] = []
        tool_calls_made = 0
        turns_used = 0

        for turn_idx in range(task.soft_turn_limit):
            check_active()
            turns_used += 1
            call_id, before = ledger.admit_model_call("fast_extractor", stage=stage_name)
            t0 = monotonic()
            failed = False
            usage: ModelUsage | None = None
            try:
                resp = self._model_runner.invoke(SlotModelRequest(
                    slot="fast_extractor",
                    stage=stage_name,
                    role=role,
                    system_prompt=sys_prompt,
                    user_prompt=messages[-1]["content"],
                    messages=list(messages),
                    tools=allowed_defs,
                    response_schema_name="EvidenceCardBatch",
                    response_schema=EvidenceCardBatch.model_json_schema(),
                    binding=self._slot_bindings["fast_extractor"],
                    turn_index=turn_idx,
                ))
                usage = resp.usage
            except Exception:
                failed = True
                raise
            finally:
                ledger.settle_model_call(
                    "fast_extractor", call_id, before, usage=usage,
                    elapsed_ms=int((monotonic() - t0) * 1000), failed=failed,
                )

            if not resp.tool_calls:
                batch = _parse_worker_batch(
                    resp,
                    task=task,
                    role=role,
                    tools_invoked=tools_invoked,
                    blocked_attempts=blocked_attempts,
                    tool_calls_made=tool_calls_made,
                    turns_used=turns_used,
                )
                emitter.emit(
                    stage=stage_name,
                    node_id=role,
                    action_kind="RESPONDING",
                    runtime_state="completed" if batch.status == "completed" else "blocked",
                    current_activity=f"{role} emitted {len(batch.cards)} EvidenceCard(s) in {turns_used} turn(s).",
                    edge_event=OrgEdgeEvent(
                        from_node_id=role,
                        to_node_id="lead_coordinator",
                        is_review=True,
                        edge_kind="report",
                        label=f"{len(batch.cards)} cards",
                    ),
                    slot="fast_extractor",
                )
                return batch

            # Execute tool calls inside the strict read-only subset boundary
            if turn_idx >= task.soft_turn_limit - 1:
                # Soft turn limit reached while still requesting tool calls; do not execute unbounded turns
                break

            assistant_calls_msg: list[dict[str, Any]] = []
            tool_result_msgs: list[dict[str, Any]] = []
            for tc in resp.tool_calls:
                check_active()
                tc_args = tc.arguments
                if tc_args is None and tc.raw_arguments:
                    try:
                        tc_args = json.loads(tc.raw_arguments)
                    except ValueError:
                        tc_args = {}
                tc_args = tc_args or {}
                assistant_calls_msg.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc_args)},
                })

                if tc.name not in allowed_set or tc.name in DISALLOWED_WORKER_TOOLS:
                    blocked_attempts.append(tc.name)
                    emitter.emit(
                        stage=stage_name,
                        node_id=role,
                        action_kind="BLOCKED",
                        runtime_state="blocked",
                        current_activity=f"Blocked unauthorized tool call {tc.name} for {role}.",
                        blocker_text=f"Tool {tc.name} is outside {role} allowed subset.",
                        slot="fast_extractor",
                    )
                    tool_result_msgs.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps({
                            "status": "denied",
                            "reason": "tool_not_allowed_for_worker",
                            "tool": tc.name,
                            "allowed_tools": sorted(allowed_set),
                        }),
                    })
                    continue

                if self._tool_runner is None:
                    tool_result_msgs.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps({"status": "unavailable", "reason": "no_tool_runner"}),
                    })
                    continue

                ledger.record_tool_call(stage=stage_name)
                tools_invoked.append(tc.name)
                tool_calls_made += 1
                emitter.emit(
                    stage=stage_name,
                    node_id=role,
                    action_kind=TOOL_TO_ORG_ACTION.get(tc.name, "READING"),
                    runtime_state="running",
                    current_activity=f"{role} executing {tc.name}.",
                    slot="fast_extractor",
                )
                out = self._tool_runner.call_tool(tc.name, tc_args, worker_role=role)
                if isinstance(out, Mapping) and out.get("reason") in ("byok_key_required", "market_data_key_required", "api_key_not_configured"):
                    emitter.emit(
                        stage=stage_name,
                        node_id=role,
                        action_kind="NEEDS_USER",
                        runtime_state="blocked",
                        current_activity=f"{role} requires user credential for {tc.name}.",
                        blocker_text=str(out.get("message") or out.get("reason")),
                        slot="fast_extractor",
                    )
                if isinstance(self._tool_runner, FinToolboxToolRunner):
                    for sid, src in self._tool_runner.source_by_id.items():
                        source_texts[sid] = src.text
                        source_permissions[sid] = src.permissions.model_dump(mode="json")
                elif isinstance(out, Mapping) and isinstance(out.get("source_id"), str) and isinstance(out.get("text"), str):
                    source_texts[out["source_id"]] = out["text"]
                tool_result_msgs.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(out, ensure_ascii=False, default=str),
                })

            messages.append({"role": "assistant", "content": None, "tool_calls": assistant_calls_msg})
            messages.extend(tool_result_msgs)

        gap_msg = f"{role} hit soft_turn_limit={task.soft_turn_limit} before finalizing EvidenceCardBatch."
        emitter.emit(
            stage=stage_name,
            node_id=role,
            action_kind="RESPONDING",
            runtime_state="completed",
            current_activity=gap_msg,
            blocker_text=gap_msg,
            edge_event=OrgEdgeEvent(
                from_node_id=role,
                to_node_id="lead_coordinator",
                is_review=True,
                edge_kind="report",
                label="Partial (turn limit)",
            ),
            slot="fast_extractor",
        )
        return EvidenceCardBatch(
            task_id=task.task_id,
            worker_role=role,
            round_number=task.round_number,
            status="partial",
            cards=[],
            unresolved_gaps=[gap_msg],
            tools_invoked=list(dict.fromkeys(tools_invoked)),
            blocked_tool_attempts=list(dict.fromkeys(blocked_attempts)),
            tool_calls_made=tool_calls_made,
            turns_used=turns_used,
            instruction_sequence=task.steering_instruction_sequence,
        )

    # --------------------------------------------------------------------------- Stage 2.5 Cross-Worker Review (Hard Cap 1 Round)
    def _run_stage_2_5(
        self,
        *,
        request: SquadPrefetchRequest,
        snapshot: EarningsPrefetchSnapshot,
        dispatch_plan: ResearchDispatchPlan,
        state: dict[str, Any],
        cp_lock: RLock,
        save_checkpoint: Callable[..., SquadCheckpoint],
        ledger: SquadExecutionLedger,
        emitter: OrgGraphEmitter,
        on_worker_complete: Callable[[WorkerRole, EvidenceCardBatch, SquadCheckpoint], None] | None,
    ) -> None:
        ledger.ensure_running(stage="stage_2_5_review")
        if state.get("stage25_executed"):
            return

        if state["stage25_decision"] is None:
            emitter.emit(
                stage="stage_2_5_review",
                node_id="lead_coordinator",
                action_kind="INSPECTING",
                runtime_state="running",
                current_activity="Lead Coordinator reviewing Stage 2 EvidenceCardBatches for cross-worker leads.",
                slot="deep_reasoner",
            )
            sys_prompt = (
                "You are the Lead Coordinator performing Stage 2.5 cross-worker review. "
                "You have a HARD CAP of 1 targeted follow-up round. If no cross-worker follow-up is needed, "
                "set has_cross_worker_leads=false and followup_tasks=[]."
            )
            user_prompt = json.dumps({
                "question": request.question,
                "snapshot_flags": [f.model_dump(mode="json") for f in snapshot.flags],
                "worker_batches": {k: v.model_dump(mode="json") for k, v in state["worker_batches"].items()},
                "open_gaps": [g.model_dump(mode="json") for g in state["open_gaps"]],
            }, ensure_ascii=False, sort_keys=True, default=str)

            call_id, before = ledger.admit_model_call("deep_reasoner", stage="stage_2_5_review")
            t0 = monotonic()
            failed = False
            usage: ModelUsage | None = None
            try:
                resp = self._model_runner.invoke(SlotModelRequest(
                    slot="deep_reasoner",
                    stage="stage_2_5_review",
                    role="lead_coordinator",
                    system_prompt=sys_prompt,
                    user_prompt=user_prompt,
                    messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
                    response_schema_name="Stage25ReviewDecision",
                    response_schema=Stage25ReviewDecision.model_json_schema(),
                    binding=self._slot_bindings["deep_reasoner"],
                ))
                usage = resp.usage
                decision = _parse_model_output(resp, Stage25ReviewDecision)
            except Exception:
                failed = True
                raise
            finally:
                ledger.settle_model_call(
                    "deep_reasoner", call_id, before, usage=usage,
                    elapsed_ms=int((monotonic() - t0) * 1000), failed=failed,
                )
            state["stage25_decision"] = decision
            save_checkpoint()
        else:
            decision = state["stage25_decision"]

        active_followups = [
            t for t in decision.followup_tasks
            if t.status != "skipped" and f"round2:{t.worker_role}" not in state["worker_batches"]
        ]
        if not decision.has_cross_worker_leads or not active_followups:
            state["stage25_executed"] = True
            emitter.emit(
                stage="stage_2_5_review",
                node_id="lead_coordinator",
                action_kind="RESPONDING",
                runtime_state="completed",
                current_activity="Stage 2.5 cleanly skipped (no additional cross-worker follow-up needed).",
                slot="deep_reasoner",
            )
            return

        emitter.emit(
            stage="stage_2_5_review",
            node_id="lead_coordinator",
            action_kind="WAITING_SUBORDINATES",
            runtime_state="waiting",
            current_activity=f"Stage 2.5 follow-up round (hard cap 1): dispatching {len(active_followups)} task(s).",
            slot="deep_reasoner",
        )
        for task in active_followups:
            emitter.emit(
                stage="stage_2_5_review",
                node_id="lead_coordinator",
                action_kind="DELEGATING",
                runtime_state="running",
                current_activity=f"Stage 2.5 targeted follow-up to {task.worker_role}.",
                edge_event=OrgEdgeEvent(
                    from_node_id="lead_coordinator",
                    to_node_id=task.worker_role,
                    is_review=True,
                    edge_kind="review",
                    label=f"Stage 2.5 follow-up: {task.objective[:100]}",
                ),
                slot="deep_reasoner",
            )

        with ThreadPoolExecutor(max_workers=min(3, len(active_followups)), thread_name_prefix="squad_s25") as pool:
            futs: dict[Future[EvidenceCardBatch], WorkerTask] = {
                pool.submit(
                    self._execute_worker_task,
                    request=request,
                    snapshot=snapshot,
                    task=task,
                    l2_pack_id=dispatch_plan.selected_l2_pack_id,
                    ledger=ledger,
                    emitter=emitter,
                    worker_cancel=Event(),
                    source_texts=state["source_texts"],
                    source_permissions=state["source_permissions"],
                ): task
                for task in active_followups
            }
            for fut, task in futs.items():
                batch = self._collect_worker_future(fut, role=task.worker_role, task=task, emitter=emitter)
                with cp_lock:
                    state["worker_batches"][f"round2:{task.worker_role}"] = batch
                    if batch.status == "failed":
                        state["open_gaps"] = _merge_gaps(state["open_gaps"], [SquadGap(
                            stage="stage_2_5_review",
                            worker_role=task.worker_role,
                            code="followup_worker_failed",
                            message=batch.error or f"Stage 2.5 {task.worker_role} failed.",
                        )])
                cp = save_checkpoint()
                if on_worker_complete is not None:
                    on_worker_complete(task.worker_role, batch, cp)

        state["stage25_executed"] = True

    # --------------------------------------------------------------------------- Stage 3A Bear Reviewer
    def _run_stage_3a_bear(
        self,
        *,
        snapshot: EarningsPrefetchSnapshot,
        cards: Sequence[EvidenceCard],
        open_gaps: Sequence[SquadGap],
        ledger: SquadExecutionLedger,
        emitter: OrgGraphEmitter,
    ) -> tuple[BearCaseBatch, list[SquadGap]]:
        ledger.ensure_running(stage="stage_3a_bear")
        emitter.emit(
            stage="stage_3a_bear",
            node_id="bear_reviewer",
            action_kind="INSPECTING",
            runtime_state="running",
            current_activity="Independent Bear Reviewer evaluating isolated diagnostics and EvidenceCards.",
            edge_event=OrgEdgeEvent(
                from_node_id="lead_coordinator",
                to_node_id="bear_reviewer",
                is_review=True,
                edge_kind="review",
                label="Isolated Bear Review",
            ),
            slot="bear_reviewer",
        )

        sys_prompt, user_prompt = build_isolated_bear_prompt(snapshot, cards, open_gaps)
        call_id, before = ledger.admit_model_call("bear_reviewer", stage="stage_3a_bear")
        t0 = monotonic()
        failed = False
        usage: ModelUsage | None = None
        try:
            resp = self._model_runner.invoke(SlotModelRequest(
                slot="bear_reviewer",
                stage="stage_3a_bear",
                role="bear_reviewer",
                system_prompt=sys_prompt,
                user_prompt=user_prompt,
                messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
                tools=[],
                response_schema_name="BearCaseBatch",
                response_schema=BearCaseBatch.model_json_schema(),
                binding=self._slot_bindings["bear_reviewer"],
            ))
            usage = resp.usage
            if resp.tool_calls:
                raise SquadToolViolationError("bear_reviewer is not permitted to invoke tools.", stage="stage_3a_bear")
            raw_items, notes, batch_id = _extract_raw_bear_items(resp)
            accepted, rejected, bear_gaps = gate_bear_cases(raw_items, snapshot, cards, open_gaps)
            bear_batch = BearCaseBatch(
                batch_id=batch_id,
                items=accepted,
                rejected_items=rejected,
                reviewer_notes=notes,
            )
        except Exception as exc:
            failed = True
            emitter.emit(
                stage="stage_3a_bear",
                node_id="bear_reviewer",
                action_kind="FAILED",
                runtime_state="failed",
                current_activity="Adversarial Bear Reviewer failed.",
                blocker_text=str(exc),
                slot="bear_reviewer",
            )
            raise
        finally:
            ledger.settle_model_call(
                "bear_reviewer", call_id, before, usage=usage,
                elapsed_ms=int((monotonic() - t0) * 1000), failed=failed,
            )

        emitter.emit(
            stage="stage_3a_bear",
            node_id="bear_reviewer",
            action_kind="RESPONDING",
            runtime_state="completed",
            current_activity=(
                f"Bear Reviewer produced {len(bear_batch.items)} anchored bear case(s) "
                f"({len(bear_batch.rejected_items)} rejected)."
            ),
            edge_event=OrgEdgeEvent(
                from_node_id="bear_reviewer",
                to_node_id="dossier_writer",
                is_review=True,
                edge_kind="review",
                label=f"{len(bear_batch.items)} anchored challenges",
            ),
            slot="bear_reviewer",
        )
        return bear_batch, bear_gaps

    # --------------------------------------------------------------------------- Stage 3B Server Gate + Lock + Memo
    def _run_stage_3b_gate_and_memo(
        self,
        *,
        run_id: str,
        snapshot: EarningsPrefetchSnapshot,
        dispatch_plan: ResearchDispatchPlan,
        batches: Sequence[EvidenceCardBatch],
        bear_batch: BearCaseBatch,
        open_gaps: Sequence[SquadGap],
        source_texts: Mapping[str, str],
        source_permissions: Mapping[str, Mapping[str, bool]],
        ledger: SquadExecutionLedger,
        emitter: OrgGraphEmitter,
    ) -> EarningsDossier:
        ledger.ensure_running(stage="stage_3b_gate_memo")
        emitter.emit(
            stage="stage_3b_gate_memo",
            node_id="dossier_writer",
            action_kind="INSPECTING",
            runtime_state="running",
            current_activity="Running server-side citation/anchor gate before locking EarningsDossier.",
            slot="writer",
        )

        verified_cards, card_gate, gate_gaps = gate_evidence_cards(
            batches, snapshot, source_texts=source_texts, source_permissions=source_permissions
        )
        all_gaps = _merge_gaps(open_gaps, gate_gaps)

        # Re-verify bear cases against final gated cards so dropped-card-only anchors never slip through
        accepted_bears, extra_rejected, extra_bear_gaps = gate_bear_cases(
            bear_batch.items, snapshot, verified_cards, all_gaps
        )
        all_rejected = [*bear_batch.rejected_items, *extra_rejected]
        all_gaps = _merge_gaps(all_gaps, extra_bear_gaps)

        gate_report = card_gate.model_copy(update={
            "passed": card_gate.passed and len(all_rejected) == 0,
            "verified_bear_ids": [b.bear_id for b in accepted_bears],
            "rejected_bear_ids": [r.bear_id for r in all_rejected],
        })

        dossier_id = _new_id("dossier")
        dossier_core = {
            "dossier_id": dossier_id,
            "run_id": run_id,
            "entity_id": snapshot.entity_id,
            "symbol": snapshot.symbol,
            "cik": snapshot.cik,
            "company_name": snapshot.company_name,
            "period_label": snapshot.period_label,
            "selected_l2_pack_id": dispatch_plan.selected_l2_pack_id,
            "snapshot": snapshot.model_dump(mode="json"),
            "evidence_cards": [c.model_dump(mode="json") for c in verified_cards],
            "bear_cases": [b.model_dump(mode="json") for b in accepted_bears],
            "open_gaps": [g.model_dump(mode="json") for g in all_gaps],
            "sector_applicability": snapshot.sector_applicability.model_dump(mode="json"),
            "gate_report": gate_report.model_dump(mode="json"),
        }

        sys_prompt = (
            "You are the Dossier Writer (writer slot). Render a concise, institutional Buy-Side Earnings Memo "
            "in Markdown strictly from the server-gated EarningsDossier payload. "
            "Do not invent numbers or ignore open_gaps or sector_applicability boundaries."
        )
        user_prompt = _build_writer_payload(dossier_core, mode="memo")
        call_id, before = ledger.admit_model_call("writer", stage="stage_3b_gate_memo")
        t0 = monotonic()
        failed = False
        usage: ModelUsage | None = None
        try:
            resp = self._model_runner.invoke(SlotModelRequest(
                slot="writer",
                stage="stage_3b_gate_memo",
                role="writer",
                system_prompt=sys_prompt,
                user_prompt=user_prompt,
                messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
                tools=[],
                binding=self._slot_bindings["writer"],
            ))
            usage = resp.usage
            if resp.tool_calls:
                raise SquadToolViolationError("Stage 3B writer is not permitted to invoke retrieval tools.", stage="stage_3b_gate_memo")
            memo_md = _extract_markdown_from_response(resp, dossier_core, mode="memo")
        except Exception as exc:
            failed = True
            emitter.emit(
                stage="stage_3b_gate_memo",
                node_id="dossier_writer",
                action_kind="FAILED",
                runtime_state="failed",
                current_activity="Memo rendering failed at Stage 3B.",
                blocker_text=str(exc),
                slot="writer",
            )
            raise
        finally:
            ledger.settle_model_call(
                "writer", call_id, before, usage=usage,
                elapsed_ms=int((monotonic() - t0) * 1000), failed=failed,
            )

        usage_summary = ledger.summary()
        dossier = EarningsDossier(
            dossier_id=dossier_id,
            run_id=run_id,
            entity_id=snapshot.entity_id,
            symbol=snapshot.symbol,
            cik=snapshot.cik,
            company_name=snapshot.company_name,
            period_label=snapshot.period_label,
            selected_l2_pack_id=dispatch_plan.selected_l2_pack_id,
            snapshot=snapshot,
            dispatch_plan=dispatch_plan,
            evidence_cards=verified_cards,
            bear_cases=accepted_bears,
            rejected_bear_cases=all_rejected,
            open_gaps=all_gaps,
            sector_applicability=snapshot.sector_applicability,
            gate_report=gate_report,
            memo_markdown=memo_md,
            deep_report_markdown=None,
            slot_usage_summary=usage_summary,
            locked=True,
        )

        emitter.emit(
            stage="stage_3b_gate_memo",
            node_id="dossier_writer",
            action_kind="RESPONDING",
            runtime_state="completed",
            current_activity=f"Locked EarningsDossier {dossier.dossier_id} and rendered Memo.",
            slot="writer",
        )
        return dossier


# --------------------------------------------------------------------------- Parsing & Normalization Helpers
def _tool_def_dict(tool_name: str) -> dict[str, Any]:
    spec = TOOL_REGISTRY[tool_name]
    tdef: ModelToolDefinition = spec.to_definition()
    return {"name": tdef.name, "description": tdef.description, "input_schema": tdef.input_schema}


def _parse_json_payload(resp: SlotModelResponse) -> Any:
    if resp.parsed is not None:
        return resp.parsed
    if isinstance(resp.content, str) and resp.content.strip():
        raw = resp.content.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
        return json.loads(raw)
    raise ValueError("Model response contained neither parsed object nor JSON text.")


def _parse_model_output(resp: SlotModelResponse, model_cls: type[BaseModel]) -> Any:
    if isinstance(resp.parsed, model_cls):
        return resp.parsed
    data = _parse_json_payload(resp)
    return model_cls.model_validate(data)


def _parse_worker_batch(
    resp: SlotModelResponse,
    *,
    task: WorkerTask,
    role: WorkerRole,
    tools_invoked: list[str],
    blocked_attempts: list[str],
    tool_calls_made: int,
    turns_used: int,
) -> EvidenceCardBatch:
    if isinstance(resp.parsed, EvidenceCardBatch):
        batch = resp.parsed
    else:
        data = _parse_json_payload(resp)
        if isinstance(data, Mapping):
            copied = dict(data)
            copied.setdefault("task_id", task.task_id)
            copied.setdefault("worker_role", role)
            copied.setdefault("round_number", task.round_number)
            for idx, card in enumerate(copied.get("cards") or []):
                if isinstance(card, dict):
                    card.setdefault("card_id", f"{role}_r{task.round_number}_c{idx + 1}")
                    card.setdefault("worker_role", role)
                    card.setdefault("round_number", task.round_number)
            batch = EvidenceCardBatch.model_validate(copied)
        else:
            raise ValueError("Worker response must be an EvidenceCardBatch object.")

    return batch.model_copy(update={
        "task_id": task.task_id,
        "worker_role": role,
        "round_number": task.round_number,
        "tools_invoked": list(dict.fromkeys([*batch.tools_invoked, *tools_invoked])),
        "blocked_tool_attempts": list(dict.fromkeys([*batch.blocked_tool_attempts, *blocked_attempts])),
        "tool_calls_made": max(batch.tool_calls_made, tool_calls_made),
        "turns_used": max(batch.turns_used, turns_used),
        "instruction_sequence": task.steering_instruction_sequence,
    })


def _extract_raw_bear_items(resp: SlotModelResponse) -> tuple[list[Any], str, str]:
    if isinstance(resp.parsed, BearCaseBatch):
        return list(resp.parsed.items), resp.parsed.reviewer_notes, resp.parsed.batch_id
    data = _parse_json_payload(resp)
    if isinstance(data, Mapping):
        raw_items = list(data.get("items") or [])
        notes = str(data.get("reviewer_notes") or "")
        batch_id = str(data.get("batch_id") or _new_id("bearbatch"))
        return raw_items, notes, batch_id
    if isinstance(data, list):
        return list(data), "", _new_id("bearbatch")
    raise ValueError("bear_reviewer output must be a BearCaseBatch or list of BearCaseItem.")


def _collect_cards_from_batches(batches: Mapping[str, EvidenceCardBatch]) -> list[EvidenceCard]:
    ordered: list[EvidenceCard] = []
    for key in sorted(batches):
        ordered.extend(batches[key].cards)
    return ordered


def _merge_gaps(existing: Sequence[SquadGap], incoming: Sequence[SquadGap]) -> list[SquadGap]:
    seen = {(g.stage, g.code, g.worker_role, g.message) for g in existing}
    out = list(existing)
    for g in incoming:
        k = (g.stage, g.code, g.worker_role, g.message)
        if k not in seen:
            seen.add(k)
            out.append(g)
    return out


__all__ = [
    "BearAnchorKind",
    "BearCaseBatch",
    "BearCaseItem",
    "CallableSlotModelRunner",
    "ConsensusSnapshotSummary",
    "DISALLOWED_WORKER_TOOLS",
    "DictSlotModelRunner",
    "DossierGateReport",
    "EarningsDossier",
    "EarningsPrefetchSnapshot",
    "EarningsSquadOrchestrator",
    "EvidenceCard",
    "EvidenceCardBatch",
    "EvidenceCitation",
    "FinToolboxToolRunner",
    "InMemorySquadCheckpointStore",
    "JsonFileSquadCheckpointStore",
    "MODEL_SLOTS",
    "ModelSlot",
    "OrgActionKind",
    "OrgEdgeEvent",
    "OrgGraphEmitter",
    "OrgGraphEvent",
    "OrgRuntimeState",
    "PrefetchDependencies",
    "PrefetchedSourceRef",
    "RejectedBearCase",
    "ResearchDispatchPlan",
    "SlotBinding",
    "SlotModelRequest",
    "SlotModelResponse",
    "SlotUsageRecord",
    "SlotUsageSummary",
    "SquadCancelledError",
    "SquadCheckpoint",
    "SquadCheckpointStore",
    "SquadConfigurationError",
    "SquadExecutionError",
    "SquadExecutionLedger",
    "SquadGap",
    "SquadModelRunner",
    "SquadPrefetchRequest",
    "SquadToolRunner",
    "SquadToolViolationError",
    "Stage25ReviewDecision",
    "SteeringImpactDecision",
    "TOOL_TO_ORG_ACTION",
    "TranscriptIndexEntry",
    "UserSteeringInstruction",
    "ValuationAndImpliedExpectationsResult",
    "WORKER_ALLOWED_TOOLS",
    "WORKER_ROLES",
    "WorkerRole",
    "WorkerTask",
    "build_isolated_bear_prompt",
    "collect_valid_evidence_anchors",
    "gate_bear_cases",
    "gate_evidence_cards",
    "render_deep_report",
    "resolve_method_pack_context",
    "resolve_squad_slots",
]
