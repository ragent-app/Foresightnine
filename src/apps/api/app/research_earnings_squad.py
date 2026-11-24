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
from contextlib import nullcontext
import copy
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import lru_cache
from hashlib import sha256
import importlib
import inspect
import json
import os
from pathlib import Path
import re
from threading import Event, RLock
from time import monotonic
from typing import Any, Callable, Iterable, Literal, Mapping, Protocol, Sequence, TypeVar, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

_TModel = TypeVar("_TModel", bound=BaseModel)

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
from .research_source_policy import is_fred_source, source_eligibility
from .research_sources import ResearchSource, SourcePermissions
from .research_tools_registry import (
    MAX_READ_SOURCES_BATCH_CHARS,
    MAX_READ_SOURCE_SPAN_CHARS,
    OutlineSourceRequest,
    ReadRequest,
    SearchSourceRequest,
    TOOL_REGISTRY,
)

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


from .research_llm_primitives import (
    norm_quote_table_tolerant as _norm_quote_table_tolerant,
    norm_quote_text as _norm_quote_text,
    quotation_matches_source as _quotation_matches_source,
)


def _truncate_str(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n...[truncated {len(value) - limit} chars]"


def _compact_tool_output_for_worker(tool_name: str, out: Any) -> Any:
    """Cap oversized raw tool payloads before appending to worker LLM context.

    Full source texts remain untouched in ``source_texts`` for server-side citation gating.
    """
    if not isinstance(out, Mapping):
        return out
    data = dict(out)

    if tool_name == "sec_filing_tables":
        tables = data.get("tables")
        if isinstance(tables, list):
            capped_tables = []
            for tbl in tables[:6]:
                if not isinstance(tbl, Mapping):
                    capped_tables.append(tbl)
                    continue
                tcopy = dict(tbl)
                rows = tcopy.get("rows")
                if isinstance(rows, list) and len(rows) > 80:
                    tcopy["rows"] = [*rows[:60], *rows[-20:]]
                    tcopy["rows_truncated"] = True
                    tcopy["total_rows"] = len(rows)
                for k in ("markdown", "text", "html_text"):
                    if isinstance(tcopy.get(k), str) and len(tcopy[k]) > 10000:
                        tcopy[k] = _truncate_str(tcopy[k], 10000)
                capped_tables.append(tcopy)
            data["tables"] = capped_tables
        for k in ("markdown", "text", "content"):
            if isinstance(data.get(k), str) and len(data[k]) > 12000:
                data[k] = _truncate_str(data[k], 12000)

    elif tool_name == "sec_redline_diff":
        for sec_key in ("added", "removed", "revised"):
            items = data.get(sec_key)
            if isinstance(items, list):
                trimmed = []
                for it in items[:12]:
                    if isinstance(it, Mapping):
                        ic = dict(it)
                        for tk in ("current_text", "prior_text", "redline_markup", "text"):
                            if isinstance(ic.get(tk), str) and len(ic[tk]) > 600:
                                ic[tk] = _truncate_str(ic[tk], 600)
                        trimmed.append(ic)
                    elif isinstance(it, str):
                        trimmed.append(_truncate_str(it, 600))
                    else:
                        trimmed.append(it)
                data[sec_key] = trimmed
        sections = data.get("section_diffs")
        if isinstance(sections, Mapping):
            new_secs: dict[str, Any] = {}
            for sk, sv in sections.items():
                if not isinstance(sv, Mapping):
                    new_secs[sk] = sv
                    continue
                svc = dict(sv)
                for sec_key in ("added", "removed", "revised"):
                    items = svc.get(sec_key)
                    if isinstance(items, list):
                        svc[sec_key] = [
                            {
                                k: (_truncate_str(v, 500) if isinstance(v, str) and len(v) > 500 else v)
                                for k, v in it.items()
                            }
                            if isinstance(it, Mapping)
                            else it
                            for it in items[:8]
                        ]
                new_secs[sk] = svc
            data["section_diffs"] = new_secs

    elif tool_name in ("read_sources", "sec_filing_sections", "sec_earnings_release"):
        if isinstance(data.get("reads"), list):
            capped_reads = []
            for r in data["reads"]:
                if isinstance(r, Mapping):
                    rc = dict(r)
                    if isinstance(rc.get("text"), str) and len(rc["text"]) > 12000:
                        rc["text"] = _truncate_str(rc["text"], 12000)
                        rc["truncated_for_context"] = True
                    capped_reads.append(rc)
                else:
                    capped_reads.append(r)
            data["reads"] = capped_reads
        for k in ("text", "content", "markdown", "raw_text"):
            if isinstance(data.get(k), str) and len(data[k]) > 12000:
                data[k] = _truncate_str(data[k], 12000)
                data["truncated_for_context"] = True

    serialized = json.dumps(data, ensure_ascii=False, default=str)
    if len(serialized) > 18000:
        for k in ("text", "content", "markdown", "raw_text", "excerpt"):
            if isinstance(data.get(k), str) and len(data[k]) > 6000:
                data[k] = _truncate_str(data[k], 6000)
                data["truncated_for_context"] = True
    return data


def _compact_stale_worker_tool_messages(messages: list[dict[str, Any]], *, keep_recent_tool_turns: int = 1) -> None:
    """Compact tool-result messages from turns older than the most recent tool turn.

    Prevents O(T^2) token growth across multi-turn worker loops while keeping the latest turn
    verbatim and retaining full source texts on the server for citation gating.
    """
    assistant_tool_indices = [
        idx for idx, m in enumerate(messages)
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    if len(assistant_tool_indices) <= keep_recent_tool_turns:
        return
    cutoff_idx = assistant_tool_indices[-keep_recent_tool_turns]
    for idx in range(cutoff_idx):
        msg = messages[idx]
        if msg.get("role") != "tool":
            continue
        raw = msg.get("content")
        if not isinstance(raw, str) or len(raw) <= 1200:
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            msg["content"] = _truncate_str(raw, 800)
            continue
        if isinstance(parsed, Mapping) and parsed.get("compacted_for_context"):
            continue
        if isinstance(parsed, Mapping):
            summary: dict[str, Any] = {
                "compacted_for_context": True,
                "status": parsed.get("status"),
                "reason": parsed.get("reason"),
                "source_id": parsed.get("source_id"),
                "title": parsed.get("title"),
                "note": "Already inspected in an earlier turn; full text remains indexed for citation verification.",
            }
            if isinstance(parsed.get("reads"), list):
                summary["read_source_ids"] = [
                    r.get("source_id") for r in parsed["reads"]
                    if isinstance(r, Mapping) and r.get("source_id")
                ]
            preview_src = parsed.get("text") or parsed.get("markdown") or parsed.get("content") or raw
            if isinstance(preview_src, str):
                summary["preview"] = preview_src[:500]
            msg["content"] = json.dumps(
                {k: v for k, v in summary.items() if v is not None},
                ensure_ascii=False,
                default=str,
            )


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
    published_at: datetime | None = None
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
    current_filing_url: str | None = None
    prior_filing_url: str | None = None
    prior_sections_source_id: str | None = None
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


@lru_cache(maxsize=32)
def _cached_model_json_schema(model_cls: type[BaseModel]) -> dict[str, Any]:
    return model_cls.model_json_schema()


@lru_cache(maxsize=128)
def _resolve_method_pack_context_cached(
    sic_key: str | None,
    preferred_pack_id: str | None,
) -> dict[str, Any]:
    """Resolve and cache L1 earnings backbone + L2 non-financial industry KPI pack."""
    sector = evaluate_sec_sic_gate(sic_key)
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
                matched_l2 = matcher(sic_key)
        composer = getattr(rm, "compose_earnings_method_pack", None)
        if callable(composer):
            composed = composer(sic=sic_key, industry_pack_id=matched_l2)
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


def resolve_method_pack_context(
    sic: str | int | None,
    *,
    preferred_pack_id: str | None = None,
) -> dict[str, Any]:
    """Resolve L1 earnings backbone + L2 non-financial industry KPI pack deterministically."""
    sic_key = str(sic).strip() if sic is not None else None
    return dict(_resolve_method_pack_context_cached(sic_key, preferred_pack_id))


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
    conversation_id: str | None = None


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


_DIAGNOSTIC_TOOL_RUNNERS: dict[str, tuple[type[BaseModel], Callable[[Any], BaseModel]]] = {
    "earnings_quality_diagnostics": (EarningsQualityRequest, earnings_quality_diagnostics),
    "guidance_and_nongaap_reconciler": (GuidanceAndNonGaapRequest, guidance_and_nongaap_reconciler),
    "valuation_and_implied_expectations": (ValuationRequest, valuation_and_implied_expectations),
}

_TOOL_ARGUMENT_HINTS: dict[str, str] = {
    "sec_earnings_release": "Provide 'cik', 'ticker', or 'entity_id' (plus 'rationale'). Example: {'cik': '0000320193', 'rationale': 'Read 8-K release'}",
    "sec_filing_tables": "Provide 'cik'/'ticker'/'entity_id', 18-24 char 'accession', optional 'r_file' (e.g. 'R2.htm'), and 'rationale'.",
    "sec_filing_sections": "Provide either 'source_id' (e.g. from prefetched_sources) or 'url' (www.sec.gov), plus optional 'form' and 'section_keys'.",
    "sec_redline_diff": "Provide 'current_source_id' or 'current_url' AND 'prior_source_id' or 'prior_url', plus optional 'section_keys'.",
    "sec_fulltext_search": "Provide 'cik'/'ticker'/'entity_id', non-blank 'query', and 'rationale'.",
    "earnings_consensus": "Provide 'symbol' or 'entity_id' and 'rationale'.",
    "earnings_transcript": "Provide 'symbol' or 'entity_id' (optional 'fiscal_year', 'fiscal_quarter') and 'rationale'.",
    "read_sources": "Provide {'reads': [{'source_id': '...', 'start': 0, 'end': 8000}], 'rationale': '...'}.",
    "search_source": "Provide {'source_id': '...', 'query': '...', 'max_hits': 10, 'rationale': '...'}.",
    "outline_source": "Provide {'source_id': '...', 'max_entries': 60}.",
    "earnings_quality_diagnostics": "Provide 'current' PeriodFinancials (with 'period_label', 'revenue', etc.), optional 'prior_qoq'/'prior_yoy'/'ttm', 'sic', and 'rationale'.",
    "guidance_and_nongaap_reconciler": "Provide 'period_label', 'guidance_metrics' and/or 'nongaap_bridges', and 'rationale'.",
    "valuation_and_implied_expectations": "Provide 'close_price', 'diluted_shares', TTM metrics ('ttm_revenue', 'ttm_fcf', etc.), 'sic', and 'rationale'.",
}


def _normalize_as_of_datetime(as_of: date | datetime | None) -> datetime | None:
    if as_of is None:
        return None
    if isinstance(as_of, datetime):
        return as_of if as_of.utcoffset() is not None else as_of.replace(tzinfo=timezone.utc)
    return datetime(as_of.year, as_of.month, as_of.day, 23, 59, 59, tzinfo=timezone.utc)


def _norm_runner_entity_id(value: str) -> str:
    text = value.strip()
    if text.lower().startswith("cik:") and text[4:].isdigit():
        return "cik:" + text[4:].zfill(10)
    return text.upper()


def _source_eligibility_status(
    src: Any,
    *,
    as_of_dt: datetime | None,
    in_flight_ids: set[str] | frozenset[str] = frozenset(),
) -> tuple[bool, str]:
    if src is None:
        return False, "source_not_found"
    if getattr(src, "is_simulated", False):
        return False, "simulated"
    url = getattr(src, "url", None)
    if url is not None and is_fred_source(str(url)):
        return False, "source_not_authorized"
    perms = getattr(src, "permissions", None)
    if isinstance(perms, Mapping):
        if not perms.get("model_input", False):
            return False, "model_permission_missing"
        if not perms.get("display", False):
            return False, "display_permission_missing"
    elif perms is not None:
        if not getattr(perms, "model_input", False):
            return False, "model_permission_missing"
        if not getattr(perms, "display", False):
            return False, "display_permission_missing"

    if as_of_dt is not None:
        if isinstance(src, ResearchSource):
            if (
                src.source_id in in_flight_ids
                and src.published_at is None
                and src.retrieval_method
                in ("sec_filing_table", "sec_filing_sections", "sec_earnings_release", "sec_companyfacts", "fmp_consensus")
            ):
                if src.revised_at is not None and src.revised_at > as_of_dt:
                    return False, "availability_unestablished"
                return True, "eligible"
            code = source_eligibility([src], as_of_dt)[0][1]
            return code == "eligible", code
        pub_at = getattr(src, "published_at", None)
        rev_at = getattr(src, "revised_at", None)
        if pub_at is None:
            return False, "availability_unestablished"
        pub_dt = _normalize_as_of_datetime(pub_at)
        rev_dt = _normalize_as_of_datetime(rev_at) if rev_at is not None else None
        if (pub_dt is not None and pub_dt > as_of_dt) or (rev_dt is not None and rev_dt > as_of_dt):
            return False, "availability_unestablished"
    return True, "eligible"


def _normalize_tool_arguments(
    tool_name: str,
    input_model: type[BaseModel],
    arguments: Mapping[str, Any],
    *,
    worker_role: WorkerRole | None = None,
) -> dict[str, Any]:
    payload = dict(arguments) if isinstance(arguments, Mapping) else {}
    fields = input_model.model_fields

    if "ticker" in fields and "symbol" not in fields and "symbol" in payload and "ticker" not in payload:
        sym_val = payload.pop("symbol")
        if sym_val is not None:
            payload["ticker"] = sym_val
    elif "symbol" in fields and "ticker" not in fields and "ticker" in payload and "symbol" not in payload:
        tkr_val = payload.pop("ticker")
        if tkr_val is not None:
            payload["symbol"] = tkr_val

    if "accession" in fields and "accession_number" in payload and "accession" not in payload:
        acc_val = payload.pop("accession_number")
        if acc_val is not None:
            payload["accession"] = acc_val

    if tool_name == "guidance_and_nongaap_reconciler" and "period" in payload and "period_label" not in payload:
        per_val = payload.pop("period")
        if per_val is not None:
            payload["period_label"] = per_val

    if tool_name == "read_sources" and "reads" not in payload and "source_id" in payload:
        read_item: dict[str, Any] = {"source_id": payload.pop("source_id")}
        if "start" in payload:
            read_item["start"] = payload.pop("start")
        if "end" in payload:
            read_item["end"] = payload.pop("end")
        payload["reads"] = [read_item]

    if "rationale" in fields:
        raw_rat = payload.get("rationale")
        if not isinstance(raw_rat, str) or not raw_rat.strip():
            payload["rationale"] = f"Squad {worker_role or 'prefetch'} read-only lookup."
    return payload


def _tool_validation_error_payload(
    tool_name: str,
    input_model: type[BaseModel],
    exc: ValidationError | ValueError,
) -> dict[str, Any]:
    errors_list: list[dict[str, str]] = []
    if isinstance(exc, ValidationError):
        for err in exc.errors()[:10]:
            loc = ".".join(str(part) for part in err.get("loc", ())) or "<root>"
            errors_list.append({
                "loc": loc,
                "type": str(err.get("type") or "validation_error"),
                "msg": str(err.get("msg") or "Invalid value"),
            })
    else:
        errors_list.append({"loc": "<root>", "type": "value_error", "msg": str(exc)})
    return {
        "status": "failed",
        "reason": "invalid_tool_arguments",
        "tool": tool_name,
        "message": f"Invalid arguments for tool {tool_name!r}: {errors_list[0]['loc']} - {errors_list[0]['msg']}",
        "validation_errors": errors_list,
        "allowed_keys": sorted(input_model.model_fields.keys()),
        "hint": _TOOL_ARGUMENT_HINTS.get(tool_name, "Match the tool JSON schema keys and required fields."),
    }


class FinToolboxToolRunner:
    """Adapter bridging ``FinDataToolbox`` + in-memory sources into ``SquadToolRunner``."""

    def __init__(
        self,
        toolbox: Any,
        *,
        entities: dict[str, dict[str, Any]] | None = None,
        source_by_id: dict[str, Any] | None = None,
        as_of: date | datetime | None = None,
    ) -> None:
        self._toolbox = toolbox
        self._entities = entities if entities is not None else {}
        self._source_by_id = source_by_id if source_by_id is not None else {}
        self._as_of_dt = _normalize_as_of_datetime(as_of)
        self._in_flight_source_ids: set[str] = set()
        self._lock = RLock()

    def set_as_of(self, as_of: date | datetime | None) -> None:
        with self._lock:
            self._as_of_dt = _normalize_as_of_datetime(as_of)

    def register_entity(self, entity_id: str, info: Mapping[str, Any]) -> None:
        if not entity_id or not isinstance(info, Mapping):
            return
        entry = dict(info)
        with self._lock:
            self._entities[entity_id] = entry
            self._entities[_norm_runner_entity_id(entity_id)] = entry
            ids = entry.get("identifiers") if isinstance(entry.get("identifiers"), Mapping) else {}
            cik_val = ids.get("cik") or entry.get("cik")
            if isinstance(cik_val, str) and cik_val.strip().isdigit():
                self._entities[f"cik:{cik10(cik_val.strip())}"] = entry
            sym_val = entry.get("symbol")
            if isinstance(sym_val, str) and sym_val.strip():
                sym_up = sym_val.strip().upper()
                self._entities[f"TICKER:{sym_up}"] = entry
                self._entities[f"ticker:{sym_up}"] = entry

    def register_source(self, source: Any, *, in_flight: bool = False) -> None:
        sid = getattr(source, "source_id", None)
        if not isinstance(sid, str) or not sid.strip():
            return
        with self._lock:
            self._source_by_id[sid] = source
            if in_flight:
                self._in_flight_source_ids.add(sid)

    def register_sources(self, sources: Iterable[Any], *, in_flight: bool = False) -> None:
        for src in sources:
            self.register_source(src, in_flight=in_flight)

    @property
    def entities(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._entities)

    @property
    def source_by_id(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._source_by_id)

    def _eligible_sources_snapshot(self) -> dict[str, Any]:
        with self._lock:
            as_of_dt = self._as_of_dt
            in_flight = set(self._in_flight_source_ids)
            raw = dict(self._source_by_id)
        out: dict[str, Any] = {}
        for sid, src in raw.items():
            ok, _ = _source_eligibility_status(src, as_of_dt=as_of_dt, in_flight_ids=in_flight)
            if ok:
                out[sid] = src
        return out

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
        if not isinstance(arguments, Mapping):
            return {
                "status": "failed",
                "reason": "invalid_tool_arguments",
                "tool": tool_name,
                "message": f"Tool {tool_name!r} arguments must be a JSON object.",
                "hint": _TOOL_ARGUMENT_HINTS.get(tool_name, "Pass a JSON object matching the tool schema."),
            }
        if tool_name == "read_sources":
            return self._local_read_sources(arguments, worker_role=worker_role)
        if tool_name == "search_source":
            return self._local_search_source(arguments, worker_role=worker_role)
        if tool_name == "outline_source":
            return self._local_outline_source(arguments, worker_role=worker_role)

        spec = TOOL_REGISTRY.get(tool_name)
        if spec is None:
            return {"status": "failed", "reason": "unknown_tool", "message": f"Unknown tool {tool_name!r}."}

        req_payload = _normalize_tool_arguments(tool_name, spec.input_model, arguments, worker_role=worker_role)
        try:
            req = spec.input_model.model_validate(req_payload)
        except (ValidationError, ValueError) as exc:
            return _tool_validation_error_payload(tool_name, spec.input_model, exc)

        method = getattr(self._toolbox, tool_name, None)
        if not callable(method) and tool_name in _DIAGNOSTIC_TOOL_RUNNERS:
            return self._run_local_diagnostic_tool(tool_name, req)
        if not callable(method):
            return {"status": "unavailable", "reason": "tool_not_wired", "message": f"Toolbox missing {tool_name!r}."}

        with self._lock:
            entities_snapshot = dict(self._entities)
        sources_snapshot = self._eligible_sources_snapshot()

        sig = inspect.signature(method)
        kwargs: dict[str, Any] = {}
        if "source_by_id" in sig.parameters:
            kwargs["source_by_id"] = sources_snapshot
        try:
            if "entities" in sig.parameters:
                outcome = method(req, entities_snapshot, **kwargs)
            else:
                outcome = method(req, **kwargs)
        except SquadExecutionError:
            raise
        except (ValidationError, ValueError) as exc:
            return _tool_validation_error_payload(tool_name, spec.input_model, exc)
        except Exception as exc:
            return {
                "status": "failed",
                "reason": "tool_execution_error",
                "tool": tool_name,
                "message": f"{tool_name} failed: {exc}",
                "hint": _TOOL_ARGUMENT_HINTS.get(tool_name, "Verify input identifiers and try an alternative source."),
            }

        with self._lock:
            if getattr(outcome, "entities", None):
                for eid, einfo in outcome.entities.items():
                    self.register_entity(str(eid), einfo)
            if getattr(outcome, "source", None) is not None:
                self.register_source(outcome.source, in_flight=True)
        return dict(outcome.result)

    def _run_local_diagnostic_tool(self, tool_name: str, req: BaseModel) -> dict[str, Any]:
        request_cls, runner = _DIAGNOSTIC_TOOL_RUNNERS[tool_name]
        try:
            payload_args = req.model_dump(mode="python", exclude={"rationale"})
            inner_req = request_cls.model_validate(payload_args)
            diagnostic = runner(inner_req)
        except (ValidationError, ValueError) as exc:
            return _tool_validation_error_payload(tool_name, request_cls, exc)
        except Exception as exc:
            return {
                "status": "failed",
                "reason": "tool_execution_error",
                "tool": tool_name,
                "message": f"{tool_name} execution failed: {exc}",
                "hint": _TOOL_ARGUMENT_HINTS.get(tool_name, ""),
            }
        rendered = diagnostic.model_dump(mode="json")
        req_dumped = req.model_dump(mode="json")
        digest = sha256(
            json.dumps(
                {"tool": tool_name, "request": req_dumped, "result": rendered},
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:32]
        return {
            "status": "ok",
            "analysis_id": f"earnings-diag-{digest}",
            **rendered,
        }

    def _get_checked_local_source(self, source_id: str) -> tuple[Any | None, str]:
        with self._lock:
            src = self._source_by_id.get(source_id)
            as_of_dt = self._as_of_dt
            in_flight = set(self._in_flight_source_ids)
        if src is None:
            return None, "source_not_found"
        ok, code = _source_eligibility_status(src, as_of_dt=as_of_dt, in_flight_ids=in_flight)
        if not ok:
            return None, code
        return src, "eligible"

    def _local_read_sources(
        self,
        arguments: Mapping[str, Any],
        *,
        worker_role: WorkerRole | None = None,
    ) -> dict[str, Any]:
        req_payload = _normalize_tool_arguments("read_sources", ReadRequest, arguments, worker_role=worker_role)
        try:
            req = ReadRequest.model_validate(req_payload)
        except (ValidationError, ValueError) as exc:
            return _tool_validation_error_payload("read_sources", ReadRequest, exc)

        total_requested = 0
        planned: list[tuple[Any, int, int, str, str]] = []
        for item in req.reads:
            src, elig_code = self._get_checked_local_source(item.source_id)
            if src is None:
                planned.append((None, 0, 0, item.source_id, elig_code))
                continue
            total_chars = len(src.text)
            start = item.start
            raw_end = item.end if item.end is not None else min(total_chars, start + MAX_READ_SOURCE_SPAN_CHARS)
            if start >= total_chars or raw_end <= start:
                return {
                    "status": "failed",
                    "reason": "invalid_source_range",
                    "source_id": item.source_id,
                    "total_characters": total_chars,
                    "message": f"Invalid source range [{start}, {raw_end}) for {item.source_id} (total_characters={total_chars}).",
                    "hint": f"Use 0 <= start < end <= {total_chars}.",
                }
            end = min(raw_end, total_chars)
            span_chars = end - start
            if span_chars > MAX_READ_SOURCE_SPAN_CHARS:
                return {
                    "status": "invalid",
                    "reason": "read_span_too_large",
                    "source_id": item.source_id,
                    "total_characters": total_chars,
                    "requested_characters": span_chars,
                    "max_span_characters": MAX_READ_SOURCE_SPAN_CHARS,
                    "message": (
                        f"Requested span ({span_chars} chars) exceeds max {MAX_READ_SOURCE_SPAN_CHARS}. "
                        "Use outline_source or search_source first, then read smaller slices."
                    ),
                }
            total_requested += span_chars
            planned.append((src, start, end, item.source_id, "eligible"))

        if total_requested > MAX_READ_SOURCES_BATCH_CHARS:
            return {
                "status": "invalid",
                "reason": "read_batch_too_large",
                "requested_characters": total_requested,
                "max_batch_characters": MAX_READ_SOURCES_BATCH_CHARS,
                "message": (
                    f"Total requested batch ({total_requested} chars) exceeds max {MAX_READ_SOURCES_BATCH_CHARS}. "
                    "Split into smaller read_sources calls."
                ),
            }

        out: list[dict[str, Any]] = []
        for src, start, end, sid, code in planned:
            if src is None:
                out.append({
                    "source_id": sid,
                    "status": "unavailable" if code == "source_not_found" else "denied_or_unavailable",
                    "reason": code,
                })
                continue
            total_chars = len(src.text)
            out.append({
                "source_id": sid,
                "status": "ok",
                "title": src.title,
                "content_sha256": src.content_sha256,
                "start": start,
                "end": end,
                "total_characters": total_chars,
                "truncated": start > 0 or end < total_chars,
                "text": src.text[start:end],
            })
        return {"status": "ok", "reads": out}

    def _local_search_source(
        self,
        arguments: Mapping[str, Any],
        *,
        worker_role: WorkerRole | None = None,
    ) -> dict[str, Any]:
        req_payload = _normalize_tool_arguments("search_source", SearchSourceRequest, arguments, worker_role=worker_role)
        try:
            req = SearchSourceRequest.model_validate(req_payload)
        except (ValidationError, ValueError) as exc:
            return _tool_validation_error_payload("search_source", SearchSourceRequest, exc)

        src, elig_code = self._get_checked_local_source(req.source_id)
        if src is None:
            return {
                "status": "failed" if elig_code == "source_not_found" else "denied_or_unavailable",
                "reason": elig_code,
                "source_id": req.source_id,
            }
        from .research_tool_executor import _search_source_text

        result = _search_source_text(
            src,
            query=req.query,
            max_hits=req.max_hits,
            case_sensitive=req.case_sensitive,
        )
        result["status"] = "ok"
        return result

    def _local_outline_source(
        self,
        arguments: Mapping[str, Any],
        *,
        worker_role: WorkerRole | None = None,
    ) -> dict[str, Any]:
        req_payload = _normalize_tool_arguments("outline_source", OutlineSourceRequest, arguments, worker_role=worker_role)
        try:
            req = OutlineSourceRequest.model_validate(req_payload)
        except (ValidationError, ValueError) as exc:
            return _tool_validation_error_payload("outline_source", OutlineSourceRequest, exc)

        src, elig_code = self._get_checked_local_source(req.source_id)
        if src is None:
            return {
                "status": "failed" if elig_code == "source_not_found" else "denied_or_unavailable",
                "reason": elig_code,
                "source_id": req.source_id,
            }
        from .research_tool_executor import _extract_source_outline

        outline = _extract_source_outline(src, max_entries=req.max_entries)
        outline["status"] = "ok"
        if not outline.get("entries"):
            outline["preview_lines"] = [line for line in src.text.splitlines() if line.strip()][:25]
        return outline


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
    hard_excluded_codes = {
        "source_excluded_simulated",
        "source_excluded_source_not_authorized",
        "source_excluded_model_permission_missing",
        "source_excluded_display_permission_missing",
        "source_excluded_after_as_of_cutoff",
    }
    excluded_sources = {
        g.source
        for g in snapshot.open_gaps
        if g.code in hard_excluded_codes and isinstance(g.source, str) and g.source
    }
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
                if cit.source_id in excluded_sources:
                    unverified_cits.append({
                        "card_id": card.card_id,
                        "source_id": cit.source_id,
                        "reason": "source_excluded_by_policy",
                    })
                    continue
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
                    if not _quotation_matches_source(cit.quotation, src_text):
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


# --------------------------------------------------------------------------- Prompt Builders, Pack Injection & Writer Slimming
_DEFAULT_WRITER_SECTIONS: tuple[str, ...] = (
    "Summary & Expectations Gap (Guidance/Non-GAAP)",
    "Earnings Quality & Cash Forensics (R/Y/G)",
    "Multi-Period Trends, Segments & KPIs",
    "Cross-Period Redline & Policy Changes",
    "Valuation Multiples & Reverse-DCF",
    "Anchored Bear Case (w/ Next-Q Trigger) & Gaps",
)
_ZH_WRITER_SECTIONS: tuple[str, ...] = (
    "核心结论与预期差（指引与 Non-GAAP 口径）",
    "盈利质量与现金流排雷（R/Y/G 信号）",
    "多期趋势、业务分部与核心 KPI",
    "跨期披露红线与会计政策变动",
    "估值倍数与 Reverse-DCF 隐含预期",
    "证据锚定空头反证（含下季证伪指标）与研究边界",
)
_DEFAULT_WRITER_MUST_STATE: tuple[str, ...] = (
    "as-of dates, clock & SIC gate",
    "coverage gaps (consensus/transcript, FCF<=0 DCF)",
    "calculated vs quoted; bear anchor + trigger",
)


def _contains_cjk(text: str | None) -> bool:
    return bool(text and re.search(r"[\u4e00-\u9fff]", text))


def _l1_writer_guidance(sic: str | None) -> tuple[list[str], list[str]]:
    ctx = resolve_method_pack_context(sic)
    composed = ctx.get("composed_pack")
    base = getattr(composed, "base_pack", None)
    if base is not None and getattr(base, "report_structure", None) is not None:
        return list(base.report_structure.sections), list(base.report_structure.must_state)
    try:
        rm = importlib.import_module("app.research_methods")
        pack = getattr(rm, "get_pack", lambda _k: None)("earnings_review")
        if pack is not None and getattr(pack, "report_structure", None) is not None:
            return list(pack.report_structure.sections), list(pack.report_structure.must_state)
    except Exception:
        pass
    return list(_DEFAULT_WRITER_SECTIONS), list(_DEFAULT_WRITER_MUST_STATE)


def _compact_statements_summary(statements: Sequence[PeriodFinancials]) -> list[dict[str, Any]]:
    key_fields = (
        "revenue", "cost_of_revenue", "gross_profit", "operating_income",
        "net_income", "ebitda", "sbc", "operating_cash_flow", "capex",
        "free_cash_flow", "cash", "accounts_receivable", "inventory",
        "total_debt", "shares_diluted",
    )
    out: list[dict[str, Any]] = []
    for p in list(statements)[:6]:
        metrics: dict[str, Any] = {}
        for fname in key_fields:
            mv = getattr(p, fname, None)
            if mv is not None:
                metrics[fname] = {
                    "value": str(mv.value),
                    "unit": mv.unit,
                    "currency": mv.currency,
                    "fact_id": mv.fact_id,
                }
        out.append({
            "period_label": p.period_label,
            "period_type": p.period_type,
            "period_end": p.period_end.isoformat() if p.period_end else None,
            "currency": p.currency,
            "monetary_unit": p.monetary_unit,
            "metrics": metrics,
        })
    return out


def _compact_snapshot_for_writer(snapshot: EarningsPrefetchSnapshot | Mapping[str, Any]) -> dict[str, Any]:
    """Build a compact structured snapshot for Stage 3B Writer without giant raw exhibit blobs."""
    if isinstance(snapshot, EarningsPrefetchSnapshot):
        statements_summary = _compact_statements_summary(snapshot.statements)
        flags = [f.model_dump(mode="json") for f in snapshot.flags]
        diag = snapshot.diagnostics
        diag_summary = (
            {
                "margin_bridge": [m.model_dump(mode="json") for m in diag.margin_bridge],
                "earnings_quality": diag.earnings_quality.model_dump(mode="json") if diag.earnings_quality else None,
                "working_capital": diag.working_capital.model_dump(mode="json") if diag.working_capital else None,
                "capital_allocation": diag.capital_allocation.model_dump(mode="json") if diag.capital_allocation else None,
            }
            if diag is not None
            else None
        )
        er = snapshot.earnings_release
        er_summary = None
        if er is not None:
            def _er_part(p: Any) -> dict[str, Any] | None:
                if p is None:
                    return None
                return {
                    "accession": p.accession,
                    "filed_at": p.filed_at.isoformat() if p.filed_at else None,
                    "period_of_report": p.period_of_report.isoformat() if p.period_of_report else None,
                    "management_summary": (p.management_summary or "")[:1200] or None,
                    "management_highlights": list(p.management_highlights[:8]),
                    "guidance_passages": [
                        {"heading": gp.heading, "horizon": gp.horizon, "text": gp.text[:500]}
                        for gp in p.guidance_passages[:6]
                    ],
                    "nongaap_titles": [ng.title for ng in p.nongaap_reconciliations[:8] if ng.title],
                    "gaps": list(p.gaps),
                }
            er_summary = {
                "pairing_mode": er.pairing_mode,
                "days_between_filings": er.days_between_filings,
                "current": _er_part(er.current),
                "prior": _er_part(er.prior),
                "gaps": list(er.gaps),
            }
        redline = snapshot.redline_summary
        redline_summary = None
        if redline is not None:
            redline_summary = {
                "total_added": redline.total_added,
                "total_removed": redline.total_removed,
                "total_revised": redline.total_revised,
                "total_filtered_noise": redline.total_filtered_noise,
                "sections": {
                    k: {
                        "has_substantive_changes": sec.has_substantive_changes,
                        "added_count": len(sec.added),
                        "removed_count": len(sec.removed),
                        "revised_count": len(sec.revised),
                        "added_previews": [(p.current_text or "")[:300] for p in sec.added[:3]],
                        "revised_previews": [(p.redline_markup or p.current_text or "")[:300] for p in sec.revised[:3]],
                    }
                    for k, sec in redline.section_diffs.items()
                },
            }
        cat = snapshot.filing_table_catalog
        cat_summary = (
            {
                "accession": cat.accession,
                "report_count": cat.report_count,
                "key_tables": [
                    {"r_file": r.r_file, "short_name": r.short_name, "category": r.normalized_category}
                    for r in [*cat.statements[:8], *cat.tables[:8], *cat.notes[:6]]
                ],
            }
            if cat is not None
            else None
        )
        return {
            "snapshot_id": snapshot.snapshot_id,
            "entity_id": snapshot.entity_id,
            "symbol": snapshot.symbol,
            "cik": snapshot.cik,
            "company_name": snapshot.company_name,
            "sic_code": snapshot.sic_code,
            "sic_description": snapshot.sic_description,
            "period_label": snapshot.period_label,
            "as_of": snapshot.as_of.isoformat() if snapshot.as_of else None,
            "filing_accession": snapshot.filing_accession,
            "prior_filing_accession": snapshot.prior_filing_accession,
            "sector_applicability": snapshot.sector_applicability.model_dump(mode="json"),
            "statements_summary": statements_summary,
            "flags": flags,
            "diagnostics_summary": diag_summary,
            "earnings_release_summary": er_summary,
            "filing_table_catalog_summary": cat_summary,
            "redline_summary": redline_summary,
            "guidance_nongaap": snapshot.guidance_nongaap.model_dump(mode="json") if snapshot.guidance_nongaap else None,
            "valuation": snapshot.valuation.model_dump(mode="json") if snapshot.valuation else None,
            "consensus_summary": {
                "status": snapshot.consensus_summary.status,
                "provider": snapshot.consensus_summary.provider,
                "records": [r.model_dump(mode="json") for r in snapshot.consensus_summary.records[:6]],
            },
            "transcript_index": snapshot.transcript_index.model_dump(mode="json"),
            "prefetched_sources": [s.model_dump(mode="json") for s in snapshot.prefetched_sources],
        }

    # Mapping fallback (e.g. if dossier.model_dump() was passed to _build_writer_payload)
    snap_dict = dict(snapshot)
    if "statements" in snap_dict and "statements_summary" not in snap_dict:
        raw_stmts = snap_dict.pop("statements") or []
        snap_dict["statements_summary"] = [
            {
                "period_label": s.get("period_label"),
                "period_type": s.get("period_type"),
                "currency": s.get("currency"),
                "metrics": {
                    k: {"value": v.get("value"), "unit": v.get("unit"), "fact_id": v.get("fact_id")}
                    for k, v in s.items()
                    if isinstance(v, Mapping) and "value" in v
                },
            }
            for s in raw_stmts[:6]
            if isinstance(s, Mapping)
        ]
    snap_dict.pop("derived_metrics", None)
    er_raw = snap_dict.pop("earnings_release", None)
    if isinstance(er_raw, Mapping) and "earnings_release_summary" not in snap_dict:
        def _slim_er_part(part: Any) -> dict[str, Any] | None:
            if not isinstance(part, Mapping):
                return None
            return {
                "accession": part.get("accession"),
                "filed_at": part.get("filed_at"),
                "management_summary": str(part.get("management_summary") or "")[:1200] or None,
                "management_highlights": list((part.get("management_highlights") or [])[:8]),
                "guidance_passages": [
                    {"heading": g.get("heading"), "horizon": g.get("horizon"), "text": str(g.get("text") or "")[:500]}
                    for g in (part.get("guidance_passages") or [])[:6]
                    if isinstance(g, Mapping)
                ],
                "gaps": list(part.get("gaps") or []),
            }
        snap_dict["earnings_release_summary"] = {
            "pairing_mode": er_raw.get("pairing_mode"),
            "current": _slim_er_part(er_raw.get("current")),
            "prior": _slim_er_part(er_raw.get("prior")),
        }
    cat_raw = snap_dict.pop("filing_table_catalog", None)
    if isinstance(cat_raw, Mapping) and "filing_table_catalog_summary" not in snap_dict:
        items = [
            *(cat_raw.get("statements") or [])[:8],
            *(cat_raw.get("tables") or [])[:8],
            *(cat_raw.get("notes") or [])[:6],
        ]
        snap_dict["filing_table_catalog_summary"] = {
            "accession": cat_raw.get("accession"),
            "report_count": cat_raw.get("report_count"),
            "key_tables": [
                {"r_file": r.get("r_file"), "short_name": r.get("short_name"), "category": r.get("normalized_category")}
                for r in items
                if isinstance(r, Mapping)
            ],
        }
    red_raw = snap_dict.get("redline_summary")
    if isinstance(red_raw, Mapping) and "section_diffs" in red_raw:
        sdiffs = red_raw.get("section_diffs") or {}
        snap_dict["redline_summary"] = {
            "total_added": red_raw.get("total_added"),
            "total_removed": red_raw.get("total_removed"),
            "total_revised": red_raw.get("total_revised"),
            "total_filtered_noise": red_raw.get("total_filtered_noise"),
            "sections": {
                k: {
                    "has_substantive_changes": sec.get("has_substantive_changes"),
                    "added_count": len(sec.get("added") or []),
                    "removed_count": len(sec.get("removed") or []),
                    "revised_count": len(sec.get("revised") or []),
                    "added_previews": [str(p.get("current_text") or "")[:300] for p in (sec.get("added") or [])[:3] if isinstance(p, Mapping)],
                    "revised_previews": [str(p.get("redline_markup") or p.get("current_text") or "")[:300] for p in (sec.get("revised") or [])[:3] if isinstance(p, Mapping)],
                }
                for k, sec in sdiffs.items()
                if isinstance(sec, Mapping)
            },
        }
    return snap_dict


@lru_cache(maxsize=128)
def _render_l1_l2_pack_cached(
    sic_key: str | None,
    effective_preferred: str | None,
    is_sector_gated: bool,
) -> dict[str, Any]:
    ctx = resolve_method_pack_context(sic_key, preferred_pack_id=effective_preferred)
    composed = ctx.get("composed_pack")
    base_pack = getattr(composed, "base_pack", None)
    industry_pack = getattr(composed, "industry_pack", None)
    rendered_text = getattr(composed, "rendered_prompt", None)
    if not rendered_text and base_pack is not None:
        try:
            rm = importlib.import_module("app.research_methods")
            render_fn = getattr(rm, "render_for_prompt", None)
            if callable(render_fn):
                rendered_text = render_fn(composed)
        except Exception:
            rendered_text = None

    l1_checks: list[str] = []
    l1_reqs: list[dict[str, Any]] = []
    l1_reconcile: list[dict[str, Any]] = []
    if base_pack is not None:
        l1_checks = list(base_pack.unit_and_period_checks)
        l1_reqs = [
            {
                "id": req.id,
                "description": req.description,
                "required": req.required,
                "acceptance": req.acceptance,
                "fallback": req.fallback,
            }
            for req in base_pack.evidence_requirements
        ]
        l1_reconcile = [
            {"metric": r.metric, "source_classes": list(r.source_classes), "note": r.note}
            for r in base_pack.reconciliation
        ]

    l2_kpis: list[dict[str, Any]] = []
    l2_filing_focus: list[str] = []
    l2_bear_angles: list[dict[str, Any]] = []
    l2_checks: list[str] = []
    if industry_pack is not None:
        l2_kpis = [
            {
                "id": k.id,
                "name_en": k.name_en,
                "name_zh": k.name_zh,
                "definition_and_formula": k.definition_and_formula,
                "preferred_tools": list(k.preferred_tools),
                "where_to_look": k.where_to_look,
                "red_flags": list(k.red_flags),
                "required": k.required,
            }
            for k in industry_pack.kpis
        ]
        l2_filing_focus = list(industry_pack.filing_focus)
        l2_bear_angles = [
            {"angle": a.angle, "evidence_anchor": a.evidence_anchor, "next_quarter_trigger": a.next_quarter_trigger}
            for a in industry_pack.counter_evidence_angles
        ]
        l2_checks = list(industry_pack.unit_and_period_checks)

    return {
        "l1_pack_id": ctx["l1_pack_id"],
        "l2_pack_id": ctx["l2_pack_id"],
        "selection_reason": ctx["reason"],
        "is_sector_gated": is_sector_gated,
        "rendered_prompt": rendered_text or "",
        "l1_evidence_requirements": l1_reqs,
        "l1_unit_and_period_checks": l1_checks,
        "l1_reconciliation": l1_reconcile,
        "l2_kpis": l2_kpis,
        "l2_filing_focus": l2_filing_focus,
        "l2_counter_evidence_angles": l2_bear_angles,
        "l2_unit_and_period_checks": l2_checks,
    }


def _render_l1_l2_pack_for_worker(
    snapshot: EarningsPrefetchSnapshot,
    *,
    l2_pack_id: str | None,
    worker_role: WorkerRole,
) -> dict[str, Any]:
    """Resolve and render L1 (earnings_review) + L2 (kpi_*) checklists & KPIs for Stage 2 workers."""
    effective_preferred = None if snapshot.sector_applicability.is_gated else l2_pack_id
    sic_key = str(snapshot.sic_code).strip() if snapshot.sic_code is not None else None
    cached = _render_l1_l2_pack_cached(sic_key, effective_preferred, snapshot.sector_applicability.is_gated)
    return {**cached, "worker_role": worker_role}


def _worker_tool_sop(
    role: WorkerRole,
    allowed_set: set[str],
    snapshot: EarningsPrefetchSnapshot,
) -> str:
    cik_hint = snapshot.cik or "<cik>"
    sym_hint = snapshot.symbol or "<symbol>"
    acc_hint = snapshot.filing_accession or "<accession_18_to_24_chars>"
    common_read = (
        "Local prefetched sources in `snapshot.prefetched_sources` are directly readable via "
        "`read_sources` (`{\"reads\": [{\"source_id\": \"...\", \"start\": 0, \"end\": 8000}], \"rationale\": \"...\"}`), "
        "`search_source` (`{\"source_id\": \"...\", \"query\": \"...\", \"rationale\": \"...\"}`), and "
        "`outline_source` (`{\"source_id\": \"...\"}`)."
    )
    if role == "worker_segments":
        order = (
            f"Recommended tool order for worker_segments:\n"
            f"1) Check `snapshot.filing_table_catalog` (`r_file`s) and call `sec_filing_tables` with "
            f"`cik=\"{cik_hint}\"`, `accession=\"{acc_hint}\"`, `r_file=\"<R*.htm>\"`, `rationale=\"...\"` (omit `r_file` first if catalog absent).\n"
            f"2) Inspect 8-K/10-Q/user-uploaded sources using `search_source`/`read_sources` (or `sec_earnings_release(cik=\"{cik_hint}\")` / `sec_filing_sections(source_id=\"...\")`).\n"
            f"3) Optionally call `guidance_and_nongaap_reconciler(symbol=\"{sym_hint}\", period_label=..., guidance_metrics=[...], nongaap_bridges=[...], rationale=\"...\")` if reconciliations need deterministic check.\n"
        )
    elif role == "worker_forensics":
        order = (
            f"Recommended tool order for worker_forensics:\n"
            f"1) Inspect Stage 0 `flags`, `redline_summary`, and `sec_sections` sources (`read_sources` / `search_source`).\n"
            f"2) Call `sec_redline_diff` (using `current_source_id`/`prior_source_id` or `current_url`/`prior_url`), "
            f"`sec_filing_tables(cik=\"{cik_hint}\", accession=\"{acc_hint}\", r_file=\"...\")`, or "
            f"`sec_fulltext_search(cik=\"{cik_hint}\", query=\"...\", rationale=\"...\")`.\n"
            f"3) Optionally call `earnings_quality_diagnostics` when recalculating PeriodFinancials.\n"
        )
    else:
        order = (
            f"Recommended tool order for worker_guidance:\n"
            f"1) Inspect `snapshot.earnings_release_summary`, `snapshot.consensus_summary`, `snapshot.transcript_index`, and any user-uploaded sources via `read_sources`/`search_source`.\n"
            f"2) Call `earnings_consensus(symbol=\"{sym_hint}\", rationale=\"...\")` and `earnings_transcript(symbol=\"{sym_hint}\", rationale=\"...\")` when FMP is configured, or `sec_earnings_release(cik=\"{cik_hint}\", rationale=\"...\")`.\n"
            f"3) Call `guidance_and_nongaap_reconciler` / `valuation_and_implied_expectations` if structured guidance/Non-GAAP or valuation checks are needed.\n"
        )
    return (
        f"{order}"
        f"Tool argument conventions: SEC tools (`sec_earnings_release`, `sec_filing_tables`, `sec_filing_sections`, `sec_fulltext_search`) "
        f"accept `cik` or `ticker` (plus `accession` >= 18 chars and `r_file` like `R10.htm` for tables); "
        f"FMP & diagnostic tools accept `symbol` or `entity_id`.\n"
        f"{common_read}\n"
        f"Error & gap handling: if any tool returns `status=\"unavailable\"` / `\"failed\"` (e.g. `byok_key_required`), "
        f"do NOT retry the exact same broken call; record the gap in `unresolved_gaps` and pivot to available 8-K/10-Q/user sources.\n"
        f"Termination rule: stop calling tools once you have gathered verified quotes/facts (or exhausted available sources), "
        f"and emit ONLY the final `EvidenceCardBatch` JSON object (strictly allowed tools only: {sorted(allowed_set)})."
    )


def _compact_table_catalog(cat: Any) -> dict[str, Any] | None:
    if cat is None:
        return None
    seen: set[str] = set()
    key_tables: list[dict[str, Any]] = []
    for rep in [*cat.statements[:10], *cat.tables[:12], *cat.notes[:8]]:
        if rep.r_file in seen:
            continue
        seen.add(rep.r_file)
        key_tables.append({
            "r_file": rep.r_file,
            "short_name": rep.short_name,
            "category": rep.normalized_category,
        })
    return {
        "accession": cat.accession,
        "report_count": cat.report_count,
        "key_tables": key_tables,
    }


def _compact_snapshot_for_worker(
    snapshot: EarningsPrefetchSnapshot,
    *,
    worker_role: WorkerRole | None = None,
) -> dict[str, Any]:
    """Return role-scoped Stage 0 snapshot metadata without duplicating heavy unused blobs."""
    include_er = worker_role in (None, "worker_guidance", "worker_segments")
    include_redline = worker_role in (None, "worker_forensics")
    include_catalog = worker_role in (None, "worker_segments", "worker_forensics")
    include_guidance_diag = worker_role in (None, "worker_guidance")
    include_consensus = worker_role in (None, "worker_guidance")
    include_flags = worker_role in (None, "worker_forensics", "worker_segments")
    stmt_limit = 2 if worker_role == "worker_guidance" else 4

    er_summary = None
    if include_er and snapshot.earnings_release is not None:
        cur = snapshot.earnings_release.current
        pri = snapshot.earnings_release.prior
        er_summary = {
            "pairing_mode": snapshot.earnings_release.pairing_mode,
            "current": {
                "accession": cur.accession,
                "filed_at": cur.filed_at.isoformat() if cur.filed_at else None,
                "management_summary": (cur.management_summary or "")[:800] or None,
                "management_highlights": list(cur.management_highlights[:6]),
                "guidance_passages": [
                    {"heading": g.heading, "horizon": g.horizon, "text": g.text[:350]}
                    for g in cur.guidance_passages[:5]
                ],
                "nongaap_titles": [n.title for n in cur.nongaap_reconciliations[:6] if n.title],
            } if cur else None,
            "prior_accession": pri.accession if pri else None,
        }

    redline_compact = None
    if include_redline and snapshot.redline_summary is not None:
        redline_compact = {
            "total_added": snapshot.redline_summary.total_added,
            "total_removed": snapshot.redline_summary.total_removed,
            "total_revised": snapshot.redline_summary.total_revised,
            "section_keys": list(snapshot.redline_summary.section_diffs.keys()),
            "sections": {
                k: {
                    "has_substantive_changes": sec.has_substantive_changes,
                    "added_count": len(sec.added),
                    "revised_count": len(sec.revised),
                    "added_previews": [(p.current_text or "")[:220] for p in sec.added[:2]],
                    "revised_previews": [(p.redline_markup or p.current_text or "")[:220] for p in sec.revised[:2]],
                }
                for k, sec in snapshot.redline_summary.section_diffs.items()
            },
        }

    payload: dict[str, Any] = {
        "entity_id": snapshot.entity_id,
        "symbol": snapshot.symbol,
        "cik": snapshot.cik,
        "company_name": snapshot.company_name,
        "sic_code": snapshot.sic_code,
        "period_label": snapshot.period_label,
        "sector_applicability": snapshot.sector_applicability.model_dump(mode="json"),
        "statements_compact": _compact_statements_summary(snapshot.statements)[:stmt_limit],
        "filing_accession": snapshot.filing_accession,
        "prior_filing_accession": snapshot.prior_filing_accession,
        "current_filing_url": snapshot.current_filing_url,
        "prior_filing_url": snapshot.prior_filing_url,
        "prior_sections_source_id": snapshot.prior_sections_source_id,
        "prefetched_sources": [
            {"source_id": s.source_id, "kind": s.kind, "title": s.title, "retrieval_method": s.retrieval_method}
            for s in snapshot.prefetched_sources
        ],
        "user_uploaded_sources": [
            {"source_id": s.source_id, "title": s.title}
            for s in snapshot.prefetched_sources
            if s.kind == "user_supplied"
        ],
    }
    if include_flags:
        payload["flags"] = [f.model_dump(mode="json") for f in snapshot.flags]
    if include_catalog:
        payload["filing_table_catalog"] = _compact_table_catalog(snapshot.filing_table_catalog)
    if include_er:
        payload["earnings_release_summary"] = er_summary
    if include_redline:
        payload["redline_summary"] = redline_compact
    if include_guidance_diag and snapshot.guidance_nongaap is not None:
        payload["guidance_nongaap"] = snapshot.guidance_nongaap.model_dump(mode="json")
    if include_consensus:
        payload["consensus_status"] = snapshot.consensus_summary.status
        payload["consensus_records"] = [r.model_dump(mode="json") for r in snapshot.consensus_summary.records[:6]]
        payload["transcript_index"] = snapshot.transcript_index.model_dump(mode="json")
    return payload


def _compact_redline_for_prompt(redline: Any, *, max_items: int = 6, max_chars: int = 350) -> dict[str, Any] | None:
    if redline is None:
        return None
    return {
        "total_added": redline.total_added,
        "total_removed": redline.total_removed,
        "total_revised": redline.total_revised,
        "total_filtered_noise": redline.total_filtered_noise,
        "sections": {
            k: {
                "has_substantive_changes": sec.has_substantive_changes,
                "added_count": len(sec.added),
                "removed_count": len(sec.removed),
                "revised_count": len(sec.revised),
                "added_previews": [(p.current_text or "")[:max_chars] for p in sec.added[:max_items]],
                "removed_previews": [(p.prior_text or "")[:max_chars] for p in sec.removed[:max_items]],
                "revised_previews": [(p.redline_markup or p.current_text or "")[:max_chars] for p in sec.revised[:max_items]],
            }
            for k, sec in redline.section_diffs.items()
        },
    }


def _build_source_footnotes(dossier_core: Mapping[str, Any]) -> list[dict[str, str]]:
    """Deterministically map internal source_ids to clean institutional footnote labels [1], [2], ..."""
    snap = dossier_core.get("snapshot") if isinstance(dossier_core.get("snapshot"), Mapping) else {}
    sym = str(dossier_core.get("symbol") or dossier_core.get("company_name") or "").strip()
    seen: dict[str, dict[str, str]] = {}

    def _register(sid: str, label: str, kind: str = "filing") -> None:
        sid = sid.strip()
        if not sid or sid in seen:
            return
        idx = len(seen) + 1
        seen[sid] = {
            "ref": f"[{idx}]",
            "source_id": sid,
            "label": label.strip() or sid,
            "kind": kind,
        }

    for ref in (snap.get("prefetched_sources") or []):
        if not isinstance(ref, Mapping):
            continue
        sid = str(ref.get("source_id") or "").strip()
        if not sid:
            continue
        title = str(ref.get("title") or "").strip()
        kind = str(ref.get("kind") or "filing").strip()
        filed = str(ref.get("filed_at") or "").strip()[:10]
        if title and filed and filed not in title:
            label = f"{title} ({filed})"
        elif title:
            label = title
        else:
            label = f"{sym} {kind}" + (f" ({filed})" if filed else "")
        _register(sid, label, kind)

    for c in (dossier_core.get("evidence_cards") or []):
        if not isinstance(c, Mapping):
            continue
        for cit in (c.get("citations") or []):
            if not isinstance(cit, Mapping):
                continue
            sid = str(cit.get("source_id") or "").strip()
            loc = str(cit.get("location") or "").strip()
            if sid and sid not in seen:
                label = f"{sym} {loc}".strip() if loc else f"{sym} SEC Filing".strip()
                _register(sid, label, "filing")

    return list(seen.values())


def build_isolated_bear_prompt(
    snapshot: EarningsPrefetchSnapshot,
    cards: Sequence[EvidenceCard],
    open_gaps: Sequence[SquadGap],
    *,
    question: str | None = None,
) -> tuple[str, str]:
    """Build a physically isolated adversarial Bear Reviewer prompt with ZERO bull narrative."""
    valid_anchors = sorted(collect_valid_evidence_anchors(snapshot, cards, open_gaps))
    example_anchor = valid_anchors[0] if valid_anchors else "flag_example"
    use_zh = _contains_cjk(question) or any(_contains_cjk(c.finding) for c in cards)
    if use_zh:
        lang_rule = (
            "LANGUAGE REQUIREMENT: Write `thesis_challenge`, `next_quarter_invalidation_trigger`, and `reviewer_notes` "
            "in concise, institutional Simplified Chinese (简体中文), keeping exact English `anchored_evidence_ids` "
            "and standard financial abbreviations (GAAP, Non-GAAP, DSO, DIO, CCC, FCF, Reverse-DCF, RPO)."
        )
        example_json = json.dumps({
            "batch_id": "bearbatch_example",
            "items": [
                {
                    "bear_id": "bear_1",
                    "thesis_challenge": "应收账款同比增长 +50%，显著快于营收 +20% 增速，导致 OCF/净利润降至 0.70x 以下。",
                    "anchored_evidence_ids": [example_anchor],
                    "anchor_kind": "flag",
                    "next_quarter_invalidation_trigger": "下季度应收账款同比增速与营收同比增速剪刀差收窄至 <= +5.0 pp。",
                    "supporting_numbers": ["AR +50% YoY", "Revenue +20% YoY", "OCF/NI 0.68x"],
                    "severity": "high",
                }
            ],
            "rejected_items": [],
            "reviewer_notes": "全部反证均严格锚定至已核验诊断旗标与证据卡片。",
        }, ensure_ascii=False)
    else:
        lang_rule = (
            "LANGUAGE REQUIREMENT: Match the language of the user's question (English if English, Simplified Chinese if Chinese)."
        )
        example_json = json.dumps({
            "batch_id": "bearbatch_example",
            "items": [
                {
                    "bear_id": "bear_1",
                    "thesis_challenge": "Accounts receivable grew +50% YoY vs +20% revenue growth, compressing OCF/NI below 0.70x.",
                    "anchored_evidence_ids": [example_anchor],
                    "anchor_kind": "flag",
                    "next_quarter_invalidation_trigger": "AR YoY growth minus revenue YoY growth narrows to <= +5.0 pp next quarter.",
                    "supporting_numbers": ["AR +50% YoY", "Revenue +20% YoY"],
                    "severity": "high",
                }
            ],
            "rejected_items": [],
            "reviewer_notes": "All challenges anchored strictly to verified diagnostic flags and EvidenceCards.",
        }, ensure_ascii=False)

    system_prompt = (
        "You are an independent buy-side Adversarial Bear Reviewer (bear_reviewer).\n"
        "You receive ONLY deterministic financial diagnostics, SEC disclosures/redlines, "
        "valuation expectations, open gaps, and factual EvidenceCards. You must NOT rely on any bull thesis "
        "or optimistic narrative.\n"
        f"{lang_rule}\n\n"
        "STRICT JSON CONTRACT — BearCaseBatch (extra='forbid' on outer batch AND every BearCaseItem; "
        "NO extra keys like 'summary', 'bear_cases', or 'confidence'; output ONLY raw JSON):\n"
        "- Outer BearCaseBatch keys: batch_id (str), items (list[BearCaseItem]), rejected_items (list, usually []), reviewer_notes (str, max 2000).\n"
        "- BearCaseItem keys (extra='forbid'):\n"
        "  * bear_id: non-blank str (e.g. 'bear_1')\n"
        "  * thesis_challenge: non-blank str (max 3000) with concrete company metrics/numbers\n"
        "  * anchored_evidence_ids: non-empty list[str] — MUST choose exact IDs from payload['valid_anchors']\n"
        "  * anchor_kind: MUST be one of {'flag', 'redline', 'guidance_gap', 'valuation_gap'}\n"
        "  * next_quarter_invalidation_trigger: non-blank str (max 1500) specifying measurable next-quarter falsification threshold\n"
        "  * supporting_numbers: list[str] (strings only, e.g. ['+50% YoY', '0.68x'])\n"
        "  * severity: MUST be one of {'high', 'medium', 'low'}\n"
        "Generic macro or competitive platitudes without concrete numeric evidence and valid anchors are rejected by the server gate.\n\n"
        f"Valid JSON example:\n{example_json}"
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
        "valid_anchors": valid_anchors,
        "diagnostic_flags": [f.model_dump(mode="json") for f in snapshot.flags],
        "redline_summary": _compact_redline_for_prompt(snapshot.redline_summary),
        "guidance_nongaap": snapshot.guidance_nongaap.model_dump(mode="json") if snapshot.guidance_nongaap else None,
        "valuation": snapshot.valuation.model_dump(mode="json") if snapshot.valuation else None,
        "evidence_cards": [
            {
                "card_id": c.card_id,
                "worker_role": c.worker_role,
                "topic_tag": c.topic_tag,
                "finding": (c.finding or "")[:900],
                "supporting_fact_ids": c.supporting_fact_ids[:10],
                "caveat": (c.caveat or "")[:300] or None,
                "unresolved_gaps": list(c.unresolved_gaps[:5]),
                "citations": [
                    {
                        "source_id": cit.source_id,
                        "location": cit.location,
                        "quotation": (cit.quotation or "")[:260],
                    }
                    for cit in c.citations[:4]
                ],
            }
            for c in cards
        ],
        "open_gaps": [g.model_dump(mode="json") for g in open_gaps[:20]],
    }
    return system_prompt, json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _build_writer_payload(dossier_core: Mapping[str, Any], *, mode: Literal["memo", "deep_report"]) -> str:
    slimmed = dict(dossier_core)
    if "snapshot" in slimmed and slimmed["snapshot"] is not None:
        slimmed["snapshot"] = _compact_snapshot_for_writer(slimmed["snapshot"])
    footnotes = _build_source_footnotes(slimmed)
    return json.dumps(
        {
            "render_mode": mode,
            "source_footnotes": footnotes,
            "locked_dossier": slimmed,
            "rules": [
                "Use ONLY facts, numbers, citations, bear cases, and open gaps present in locked_dossier.",
                "Cite sources cleanly using numbered footnotes [1], [2], etc. from source_footnotes, and include a concise Sources list at the end.",
                "NEVER output raw internal IDs or debug strings such as 'Gate 状态: exportable: true, passed: false', 'source_...', '[card_...]', or raw JSON field names.",
                "Respect sector_applicability: never fabricate or report disabled_by_sector metrics.",
                "Do not invoke any retrieval tools; report open_gaps and bear invalidation triggers clearly in natural institutional prose.",
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _deterministic_markdown_fallback(dossier_core: Mapping[str, Any], *, mode: Literal["memo", "deep_report"]) -> str:
    entity = dossier_core.get("symbol") or dossier_core.get("company_name") or dossier_core.get("entity_id")
    period = dossier_core.get("period_label") or "Latest Period"
    use_zh = _contains_cjk(str(dossier_core.get("question") or "")) or any(
        _contains_cjk(str(c.get("finding") or "")) for c in (dossier_core.get("evidence_cards") or [])
    )
    if use_zh:
        title = "买方财报研究简报 (Memo)" if mode == "memo" else "深度财报研究报告 (Deep Report)"
    else:
        title = "Buy-Side Earnings Memo" if mode == "memo" else "Deep Earnings Research Report"
    lines = [f"# {title}: {entity} ({period})", ""]

    snap = dossier_core.get("snapshot") if isinstance(dossier_core.get("snapshot"), Mapping) else {}
    stmts = snap.get("statements_summary") or []
    if stmts:
        lines.append("## 核心财务指标摘要" if use_zh else "## Key Financial Summary")
        latest = stmts[0]
        metrics = latest.get("metrics") or {}
        if metrics:
            lines.append(f"- **Period ({latest.get('period_label')})**: " + ", ".join(
                f"{k}={v.get('value')} {v.get('unit') or ''} [{v.get('fact_id') or '-'}]"
                for k, v in list(metrics.items())[:8]
                if isinstance(v, Mapping)
            ))
        lines.append("")

    val = snap.get("valuation") if isinstance(snap.get("valuation"), Mapping) else {}
    rdcf = val.get("reverse_dcf") if isinstance(val.get("reverse_dcf"), Mapping) else {}
    if rdcf:
        lines.append("## 估值与隐含预期" if use_zh else "## Valuation & Implied Expectations")
        lines.append(
            f"- Reverse-DCF status: `{rdcf.get('status')}`"
            + (f" | implied 5Y FCF CAGR: {rdcf.get('implied_5y_fcf_cagr_pct')}%" if rdcf.get("implied_5y_fcf_cagr_pct") is not None else "")
            + (f" ({rdcf.get('reason')})" if rdcf.get("reason") else "")
        )
        lines.append("")

    cards = dossier_core.get("evidence_cards") or []
    bears = dossier_core.get("bear_cases") or []
    gaps = dossier_core.get("open_gaps") or []
    lines.append("## 已核验证据发现" if use_zh else "## Key Evidence Findings")
    if cards:
        for c in cards:
            cits = c.get("citations") or []
            cit_str = f" [sources: {', '.join(str(x.get('source_id')) for x in cits if isinstance(x, Mapping) and x.get('source_id'))}]" if cits else ""
            lines.append(f"- **[{c.get('topic_tag')}]** (`{c.get('card_id')}`){cit_str}: {c.get('finding')}")
    else:
        lines.append("- 暂无已核验证据卡片。" if use_zh else "- No verified evidence cards available.")

    lines.extend(["", "## 证据锚定空头反证" if use_zh else "## Adversarial Bear Cases"])
    if bears:
        for b in bears:
            lines.append(
                f"- **[{b.get('anchor_kind')}]** ({', '.join(b.get('anchored_evidence_ids') or [])}): "
                f"{b.get('thesis_challenge')} | Invalidation: {b.get('next_quarter_invalidation_trigger')}"
            )
    else:
        lines.append("- 暂无锚定空头反证。" if use_zh else "- No anchored bear cases recorded.")

    lines.extend(["", "## 覆盖缺口与研究边界" if use_zh else "## Open Gaps & Boundaries"])
    sector = dossier_core.get("sector_applicability") or {}
    if sector.get("is_gated"):
        lines.append(f"- Sector gate active ({sector.get('sector_category')}): {sector.get('explanation')}")
    if gaps:
        for g in gaps:
            lines.append(f"- [{g.get('stage')}] {g.get('code')}: {g.get('message')}")
    elif not sector.get("is_gated"):
        lines.append("- 无。" if use_zh else "- None.")
    return "\n".join(lines)


def _build_writer_system_prompt(
    *,
    mode: Literal["memo", "deep_report"],
    sic_code: str | None,
    question: str | None = None,
) -> str:
    sections, must_state = _l1_writer_guidance(sic_code)
    use_zh = _contains_cjk(question)
    effective_sections = list(_ZH_WRITER_SECTIONS) if use_zh else sections
    lang_rule = (
        "Write the entire report (including all `##` section headings, bear-case arguments, and boundaries) "
        "in natural, institutional Simplified Chinese (简体中文), keeping standard English tickers, SEC form names "
        "(8-K, 10-Q, 10-K), and financial abbreviations (GAAP, Non-GAAP, DSO, DIO, CCC, FCF, Reverse-DCF, RPO) intact."
        if use_zh
        else "Match the language of the user's question (Chinese if Chinese, English if English)."
    )
    mode_desc = (
        "concise, institutional Buy-Side Earnings Memo"
        if mode == "memo"
        else "comprehensive institutional Buy-Side Deep Research Report"
    )
    return (
        f"You are an institutional buy-side equity research writer (writer slot). "
        f"Render a {mode_desc} in clean, publication-grade Markdown strictly from the server-gated `locked_dossier` JSON.\n"
        f"Rules:\n"
        f"1. You have ZERO retrieval tools. Do NOT emit tool calls and do NOT alter or invent any number, period, unit, or fact. "
        f"Always use precomputed deltas/percentages from `locked_dossier` verbatim and verify any stated subtraction or delta exactly.\n"
        f"2. {lang_rule}\n"
        f"3. Required report structure `##` section headings: {' | '.join(effective_sections)}.\n"
        f"4. Must explicitly state: {'; '.join(must_state)}.\n"
        f"5. CITATION STYLE (INSTITUTIONAL): Use clean numbered footnotes `[1]`, `[2]` matching `source_footnotes` in the payload, "
        f"and end with a concise `## 参考来源` (or `## Sources`) table mapping each `[n]` to its human-readable filing/data label. "
        f"NEVER expose internal engineering identifiers or raw debug fields in the prose — specifically forbid strings like "
        f"`Gate 状态: exportable: true, passed: false`, `source_...`, `[card_...]`, `fact_id` hashes, or raw JSON keys.\n"
        f"6. Include every accepted bear case with its concrete numeric basis and next-quarter invalidation trigger.\n"
        f"7. Respect `sector_applicability` boundaries (never compute or report `disabled_by_sector` metrics) and summarize "
        f"any `open_gaps` or gate-excluded items in plain institutional prose under `## 研究边界与资料缺口` (or `## Research Boundaries & Data Gaps`)."
    )


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
        exclude={"deep_report_markdown", "slot_usage_summary", "snapshot"},
    )
    dossier_core["snapshot"] = _compact_snapshot_for_writer(dossier.snapshot)
    if dossier.dispatch_plan is not None and dossier.dispatch_plan.planning_notes:
        dossier_core["planning_context"] = dossier.dispatch_plan.planning_notes[:400]
    sys_prompt = _build_writer_system_prompt(
        mode="deep_report",
        sic_code=dossier.snapshot.sic_code,
        question=dossier.memo_markdown[:400],
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
            conversation_id=f"{dossier.run_id}:deep_report",
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


def _extract_markdown_or_fallback(
    resp: SlotModelResponse,
    dossier_core: Mapping[str, Any],
    *,
    mode: Literal["memo", "deep_report"],
) -> tuple[str, bool]:
    if isinstance(resp.parsed, Mapping):
        for key in ("deep_report_markdown", "memo_markdown", "markdown", "report"):
            val = resp.parsed.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip(), False
    if isinstance(resp.content, str) and resp.content.strip():
        txt = resp.content.strip()
        if txt.startswith("{") and txt.endswith("}"):
            try:
                obj = json.loads(txt)
                if isinstance(obj, Mapping):
                    for key in ("deep_report_markdown", "memo_markdown", "markdown", "report"):
                        val = obj.get(key)
                        if isinstance(val, str) and val.strip():
                            return val.strip(), False
            except ValueError:
                pass
        return txt, False
    return _deterministic_markdown_fallback(dossier_core, mode=mode), True


def _extract_markdown_from_response(
    resp: SlotModelResponse,
    dossier_core: Mapping[str, Any],
    *,
    mode: Literal["memo", "deep_report"],
) -> str:
    md, _ = _extract_markdown_or_fallback(resp, dossier_core, mode=mode)
    return md


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
        max_repair_attempts: int = 3,
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
        self._max_repair_attempts = max(0, int(max_repair_attempts))

        self._steering_lock = RLock()
        self._deep_report_lock = RLock()
        self._diag_lock = RLock()
        self._steering_wakeup = Event()
        self._steering_queue: list[UserSteeringInstruction] = []
        self._next_instruction_seq = 0
        self._deep_report_cache: dict[tuple[str, str], str] = {}
        self._active_ledger: SquadExecutionLedger | None = None
        self._active_emitter: OrgGraphEmitter | None = None
        self._repair_diagnostics: list[dict[str, Any]] = []

    @property
    def checkpoint_store(self) -> SquadCheckpointStore:
        return self._checkpoint_store

    @property
    def slot_bindings(self) -> dict[ModelSlot, SlotBinding]:
        return dict(self._slot_bindings)

    @property
    def repair_diagnostics(self) -> list[dict[str, Any]]:
        with self._diag_lock:
            return [dict(item) for item in self._repair_diagnostics]

    def _record_repair_diagnostic(self, entry: Mapping[str, Any]) -> None:
        with self._diag_lock:
            self._repair_diagnostics.append(dict(entry))

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

    def _sync_stage0_into_tool_runner(
        self,
        *,
        request: SquadPrefetchRequest,
        snapshot: EarningsPrefetchSnapshot,
        source_texts: Mapping[str, str],
        source_permissions: Mapping[str, Mapping[str, bool]],
    ) -> None:
        if not isinstance(self._tool_runner, FinToolboxToolRunner):
            return
        self._tool_runner.set_as_of(request.as_of)
        self._tool_runner.register_entity(
            snapshot.entity_id,
            {
                "company": snapshot.company_name,
                "symbol": snapshot.symbol,
                "cik": snapshot.cik,
                "identifiers": {**({"cik": snapshot.cik} if snapshot.cik else {})},
            },
        )
        existing = self._tool_runner.source_by_id
        for ref in snapshot.prefetched_sources:
            if ref.source_id in existing:
                continue
            txt = source_texts.get(ref.source_id)
            if not isinstance(txt, str) or not txt.strip():
                continue
            perms_raw = source_permissions.get(ref.source_id) or ref.permissions or {}
            perms = SourcePermissions(
                store=bool(perms_raw.get("store", True)),
                model_input=bool(perms_raw.get("model_input", True)),
                display=bool(perms_raw.get("display", True)),
                export=bool(perms_raw.get("export", True)),
            )
            pub_at: datetime | None = ref.published_at
            if pub_at is None:
                if ref.source_id.startswith("src_8k_release_") and snapshot.earnings_release and snapshot.earnings_release.current:
                    cur_er = snapshot.earnings_release.current
                    pub_at = cur_er.accepted_at or datetime(cur_er.filed_at.year, cur_er.filed_at.month, cur_er.filed_at.day, tzinfo=timezone.utc)
                elif ref.retrieval_method == "fmp_transcript" and snapshot.transcript_index.call_date is not None:
                    pub_at = _normalize_as_of_datetime(snapshot.transcript_index.call_date)
                elif ref.retrieval_method == "fmp_consensus" and snapshot.consensus_summary.records:
                    pub_at = _normalize_as_of_datetime(snapshot.consensus_summary.records[0].period_end)
            valid_url: Any = None
            if ref.url and ref.url.startswith(("http://", "https://")):
                valid_url = ref.url
            rm_val = ref.retrieval_method if ref.retrieval_method in (
                "user_supplied", "official_html", "sec_companyconcept", "user_pdf", "user_html",
                "sec_filing_html", "sec_earnings_release", "sec_filing_table", "sec_filing_sections",
                "historical_prices", "market_data", "sec_companyfacts", "structured_financials",
                "fmp_consensus", "fmp_transcript", "web_html", "web_pdf",
            ) else "official_html"
            try:
                src = ResearchSource(
                    source_id=ref.source_id,
                    owner_id=request.account_id or "squad_stage0",
                    title=ref.title,
                    text=txt,
                    url=valid_url,
                    kind=ref.kind,
                    is_simulated=False,
                    published_at=pub_at,
                    obtained_at=_utcnow(),
                    content_sha256=ref.content_sha256 or sha256(txt.encode("utf-8")).hexdigest(),
                    retrieval_method=rm_val,
                    permissions=perms,
                )
                self._tool_runner.register_source(src)
            except Exception:
                continue

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
        with self._diag_lock:
            self._repair_diagnostics = []
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
            self._sync_stage0_into_tool_runner(
                request=request,
                snapshot=snapshot,
                source_texts=state["source_texts"],
                source_permissions=state["source_permissions"],
            )
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
            self._sync_stage0_into_tool_runner(
                request=request,
                snapshot=snapshot,
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
            stage1_gaps: list[SquadGap] = []
            dispatch_plan = self._run_stage_1_plan(
                request=request,
                snapshot=snapshot,
                ledger=ledger,
                emitter=emitter,
                instruction_seq=max(state["applied_instruction_sequences"], default=0),
                gaps_out=stage1_gaps,
            )
            if stage1_gaps:
                state["open_gaps"] = _merge_gaps(state["open_gaps"], stage1_gaps)
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
                question=request.question,
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
            question=request.question,
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
        as_of_dt = _normalize_as_of_datetime(request.as_of)

        if isinstance(self._tool_runner, FinToolboxToolRunner):
            self._tool_runner.set_as_of(request.as_of)
            self._tool_runner.register_entity(
                entity_id,
                {
                    "company": company_name,
                    "symbol": symbol,
                    "cik": cik,
                    "identifiers": {**({"cik": cik} if cik else {})},
                },
            )

        cur_filing, pri_filing = _pick_current_prior_periodic_filings(
            filings_list, as_of=as_of_d, preferred_form=request.filing_form
        )
        filing_acc = request.filing_accession or (cur_filing.accession if cur_filing else None)
        prior_acc = request.prior_filing_accession or (pri_filing.accession if pri_filing else None)
        cur_url = request.current_filing_url or (cur_filing.primary_document_url if cur_filing else None)
        pri_url = request.prior_filing_url or (pri_filing.primary_document_url if pri_filing else None)
        pri_sec_sid: str | None = None

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

        def task_redline() -> tuple[FilingRedlineReport | None, FilingSectionsBundle | None, FilingSectionsBundle | None, list[SquadGap]]:
            if sec is None or not cur_url or not pri_url:
                return None, None, None, []
            c_res = sec_filing_sections(sec, url=cur_url, form=cur_filing.form if cur_filing else request.filing_form)
            p_res = sec_filing_sections(sec, url=pri_url, form=pri_filing.form if pri_filing else request.filing_form)
            if c_res.status in ("ok", "partial") and c_res.data and p_res.status in ("ok", "partial") and p_res.data:
                c_bundle: FilingSectionsBundle = c_res.data[0]
                p_bundle: FilingSectionsBundle = p_res.data[0]
                report = sec_redline_diff(c_bundle, p_bundle)
                assert isinstance(report, FilingRedlineReport)
                return report, c_bundle, p_bundle, []
            return None, None, None, [SquadGap(
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
                    er_sha = sha256(er_text.encode("utf-8")).hexdigest()
                    er_perms = {"store": True, "display": True, "model_input": True, "export": True}
                    cur_er = earnings_release.current or earnings_release.prior
                    er_url = (
                        cur_er.ex99_1.url if cur_er and cur_er.ex99_1
                        else (cur_er.index_json_url if cur_er else None)
                    )
                    er_pub = (
                        (cur_er.accepted_at or datetime(cur_er.filed_at.year, cur_er.filed_at.month, cur_er.filed_at.day, tzinfo=timezone.utc))
                        if cur_er is not None
                        else None
                    )
                    source_texts[er_sid] = er_text
                    source_permissions[er_sid] = er_perms
                    prefetched_sources.append(PrefetchedSourceRef(
                        source_id=er_sid,
                        title=f"{symbol or entity_id} SEC 8-K Item 2.02 Earnings Release",
                        kind="official",
                        retrieval_method="sec_earnings_release",
                        content_sha256=er_sha,
                        url=er_url,
                        permissions=er_perms,
                        published_at=er_pub,
                        total_characters=len(er_text),
                    ))
                    if isinstance(self._tool_runner, FinToolboxToolRunner):
                        self._tool_runner.register_source(ResearchSource(
                            source_id=er_sid,
                            owner_id=request.account_id or "squad_stage0",
                            title=f"{symbol or entity_id} SEC 8-K Item 2.02 Earnings Release",
                            text=er_text,
                            url=er_url,
                            kind="official",
                            is_simulated=False,
                            published_at=er_pub,
                            obtained_at=_utcnow(),
                            content_sha256=er_sha,
                            retrieval_method="sec_earnings_release",
                            permissions=SourcePermissions(**er_perms),
                        ))

            table_catalog, g_cat = f_cat.result()
            gaps.extend(g_cat)

            redline_report, cur_sections_bundle, pri_sections_bundle, g_red = f_red.result()
            gaps.extend(g_red)
            if cur_sections_bundle is not None and cur_sections_bundle.clean_text:
                sec_sid = f"src_sec_sections_{filing_acc or cik or entity_id}"
                sec_perms = {"store": True, "display": True, "model_input": True, "export": True}
                cur_sec_pub = (
                    (cur_filing.accepted_at or datetime(cur_filing.filed_at.year, cur_filing.filed_at.month, cur_filing.filed_at.day, tzinfo=timezone.utc))
                    if cur_filing is not None
                    else None
                )
                source_texts[sec_sid] = cur_sections_bundle.clean_text
                source_permissions[sec_sid] = sec_perms
                prefetched_sources.append(PrefetchedSourceRef(
                    source_id=sec_sid,
                    title=f"{symbol or entity_id} SEC {cur_sections_bundle.form or 'Filing'} Sections",
                    kind="official",
                    retrieval_method="sec_filing_sections",
                    content_sha256=cur_sections_bundle.content_sha256,
                    url=cur_url,
                    permissions=sec_perms,
                    published_at=cur_sec_pub,
                    total_characters=len(cur_sections_bundle.clean_text),
                ))
                if isinstance(self._tool_runner, FinToolboxToolRunner):
                    self._tool_runner.register_source(ResearchSource(
                        source_id=sec_sid,
                        owner_id=request.account_id or "squad_stage0",
                        title=f"{symbol or entity_id} SEC {cur_sections_bundle.form or 'Filing'} Sections",
                        text=cur_sections_bundle.clean_text,
                        url=cur_url,
                        kind="official",
                        is_simulated=False,
                        published_at=cur_sec_pub,
                        obtained_at=_utcnow(),
                        content_sha256=cur_sections_bundle.content_sha256,
                        retrieval_method="sec_filing_sections",
                        permissions=SourcePermissions(**sec_perms),
                    ))

            if pri_sections_bundle is not None and pri_sections_bundle.clean_text:
                pri_sec_sid = f"src_sec_sections_prior_{prior_acc or cik or entity_id}"
                pri_sec_perms = {"store": True, "display": True, "model_input": True, "export": True}
                pri_sec_pub = (
                    (pri_filing.accepted_at or datetime(pri_filing.filed_at.year, pri_filing.filed_at.month, pri_filing.filed_at.day, tzinfo=timezone.utc))
                    if pri_filing is not None
                    else None
                )
                source_texts[pri_sec_sid] = pri_sections_bundle.clean_text
                source_permissions[pri_sec_sid] = pri_sec_perms
                prefetched_sources.append(PrefetchedSourceRef(
                    source_id=pri_sec_sid,
                    title=f"{symbol or entity_id} Prior SEC {pri_sections_bundle.form or 'Filing'} Sections",
                    kind="official",
                    retrieval_method="sec_filing_sections",
                    content_sha256=pri_sections_bundle.content_sha256,
                    url=pri_url,
                    permissions=pri_sec_perms,
                    published_at=pri_sec_pub,
                    total_characters=len(pri_sections_bundle.clean_text),
                ))
                if isinstance(self._tool_runner, FinToolboxToolRunner):
                    self._tool_runner.register_source(ResearchSource(
                        source_id=pri_sec_sid,
                        owner_id=request.account_id or "squad_stage0",
                        title=f"{symbol or entity_id} Prior SEC {pri_sections_bundle.form or 'Filing'} Sections",
                        text=pri_sections_bundle.clean_text,
                        url=pri_url,
                        kind="official",
                        is_simulated=False,
                        published_at=pri_sec_pub,
                        obtained_at=_utcnow(),
                        content_sha256=pri_sections_bundle.content_sha256,
                        retrieval_method="sec_filing_sections",
                        permissions=SourcePermissions(**pri_sec_perms),
                    ))

            consensus_summary, g_cons = f_cons.result()
            gaps.extend(g_cons)
            if consensus_summary.status == "available" and consensus_summary.records:
                con_sid = consensus_summary.source_id or f"src_consensus_{symbol or entity_id}"
                con_lines = [
                    f"{r.symbol} | period {r.fiscal_period or '-'} | period_end {r.period_end.isoformat() if r.period_end else '-'} | "
                    f"rev_est_avg {r.revenue_estimate_avg if r.revenue_estimate_avg is not None else '-'} | "
                    f"eps_est_avg {r.eps_estimate_avg if r.eps_estimate_avg is not None else '-'} | "
                    f"rev_actual {r.revenue_actual if r.revenue_actual is not None else '-'} | "
                    f"eps_actual {r.eps_actual if r.eps_actual is not None else '-'}"
                    for r in consensus_summary.records
                ]
                con_text = "\n".join(con_lines)
                con_sha = sha256(con_text.encode("utf-8")).hexdigest()
                con_perms = dict(consensus_summary.permissions or {"store": True, "display": True, "model_input": True, "export": False})
                source_texts[con_sid] = con_text
                source_permissions[con_sid] = con_perms
                consensus_summary = consensus_summary.model_copy(update={"source_id": con_sid})
                prefetched_sources.append(PrefetchedSourceRef(
                    source_id=con_sid,
                    title=f"{symbol or entity_id} Street Earnings Consensus",
                    kind="market_data",
                    retrieval_method="fmp_consensus",
                    content_sha256=con_sha,
                    permissions=con_perms,
                    published_at=as_of_dt,
                    total_characters=len(con_text),
                ))
                if isinstance(self._tool_runner, FinToolboxToolRunner):
                    self._tool_runner.register_source(ResearchSource(
                        source_id=con_sid,
                        owner_id=request.account_id or "squad_stage0",
                        title=f"{symbol or entity_id} Street Earnings Consensus",
                        text=con_text,
                        kind="market_data",
                        is_simulated=False,
                        published_at=as_of_dt,
                        obtained_at=_utcnow(),
                        content_sha256=con_sha,
                        retrieval_method="fmp_consensus",
                        permissions=SourcePermissions(
                            store=bool(con_perms.get("store", True)),
                            model_input=bool(con_perms.get("model_input", True)),
                            display=bool(con_perms.get("display", True)),
                            export=bool(con_perms.get("export", False)),
                        ),
                    ))

            transcript_index, tr_text, g_tr = f_tr.result()
            gaps.extend(g_tr)
            if transcript_index.status == "available" and transcript_index.source_id and tr_text:
                tr_sha = transcript_index.content_sha256 or sha256(tr_text.encode("utf-8")).hexdigest()
                tr_perms = dict(transcript_index.permissions)
                source_texts[transcript_index.source_id] = tr_text
                source_permissions[transcript_index.source_id] = tr_perms
                tr_pub = _normalize_as_of_datetime(transcript_index.call_date)
                prefetched_sources.append(PrefetchedSourceRef(
                    source_id=transcript_index.source_id,
                    title=f"{symbol} Earnings Call Transcript (FY{transcript_index.fiscal_year} Q{transcript_index.fiscal_quarter})",
                    kind="market_data",
                    retrieval_method="fmp_transcript",
                    content_sha256=tr_sha,
                    permissions=tr_perms,
                    published_at=tr_pub,
                    total_characters=len(tr_text),
                ))
                if isinstance(self._tool_runner, FinToolboxToolRunner):
                    self._tool_runner.register_source(ResearchSource(
                        source_id=transcript_index.source_id,
                        owner_id=request.account_id or "squad_stage0",
                        title=f"{symbol} Earnings Call Transcript (FY{transcript_index.fiscal_year} Q{transcript_index.fiscal_quarter})",
                        text=tr_text,
                        kind="market_data",
                        is_simulated=False,
                        published_at=_normalize_as_of_datetime(transcript_index.call_date),
                        obtained_at=_utcnow(),
                        content_sha256=tr_sha,
                        retrieval_method="fmp_transcript",
                        permissions=SourcePermissions(
                            store=bool(tr_perms.get("store", True)),
                            model_input=bool(tr_perms.get("model_input", True)),
                            display=bool(tr_perms.get("display", True)),
                            export=bool(tr_perms.get("export", False)),
                        ),
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

        # Capture any eligible topic-attached or pre-existing sources in FinToolboxToolRunner
        existing_ref_ids = {ref.source_id for ref in prefetched_sources}
        if isinstance(self._tool_runner, FinToolboxToolRunner):
            for sid, src in self._tool_runner.source_by_id.items():
                if sid in existing_ref_ids:
                    continue
                ok, elig_code = _source_eligibility_status(src, as_of_dt=as_of_dt)
                if not ok:
                    source_texts.pop(sid, None)
                    source_permissions.pop(sid, None)
                    pub_dt = _normalize_as_of_datetime(getattr(src, "published_at", None))
                    rev_dt = _normalize_as_of_datetime(getattr(src, "revised_at", None))
                    gap_code = (
                        "source_excluded_after_as_of_cutoff"
                        if elig_code == "availability_unestablished"
                        and as_of_dt is not None
                        and ((pub_dt is not None and pub_dt > as_of_dt) or (rev_dt is not None and rev_dt > as_of_dt))
                        else f"source_excluded_{elig_code}"
                    )
                    gaps.append(SquadGap(
                        stage="stage_0_prefetch",
                        code=gap_code,
                        message=f"Excluded ineligible source {sid!r} ({elig_code}).",
                        source=sid,
                    ))
                    continue
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
            current_filing_url=cur_url,
            prior_filing_url=pri_url,
            prior_sections_source_id=pri_sec_sid,
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

    # --------------------------------------------------------------------------- Shared Schema Repair & Stage 1
    def _invoke_and_parse_with_repair(
        self,
        *,
        slot: ModelSlot,
        stage: str,
        role: str,
        node_id: str,
        system_prompt: str,
        user_prompt: str,
        model_cls: type[_TModel],
        ledger: SquadExecutionLedger,
        emitter: OrgGraphEmitter,
        norm_kwargs: Mapping[str, Any] | None = None,
        fallback_fn: Callable[[Exception], _TModel] | None = None,
        conversation_id: str | None = None,
        forbidden_tool_error: str | None = None,
    ) -> tuple[_TModel, bool, Exception | None]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        max_repairs = max(0, int(self._max_repair_attempts))
        total_attempts = 1 + max_repairs
        last_exc: Exception | None = None
        norm_args = dict(norm_kwargs or {})

        for attempt_idx in range(total_attempts):
            ledger.ensure_running(stage=stage)
            if attempt_idx > 0:
                emitter.emit(
                    stage=stage,
                    node_id=node_id,
                    action_kind="INSPECTING",
                    runtime_state="running",
                    current_activity=(
                        f"{role} repairing {model_cls.__name__} JSON output "
                        f"(repair {attempt_idx}/{max_repairs})."
                    ),
                    slot=slot,
                )
            call_id, before = ledger.admit_model_call(slot, stage=stage)
            t0 = monotonic()
            failed = False
            usage: ModelUsage | None = None
            try:
                resp = self._model_runner.invoke(SlotModelRequest(
                    slot=slot,
                    stage=stage,
                    role=role,
                    system_prompt=system_prompt,
                    user_prompt=messages[-1]["content"],
                    messages=list(messages),
                    tools=[],
                    response_schema_name=model_cls.__name__,
                    response_schema=_cached_model_json_schema(model_cls),
                    binding=self._slot_bindings[slot],
                    turn_index=attempt_idx,
                    conversation_id=conversation_id,
                ))
                usage = resp.usage
                if resp.tool_calls:
                    msg = forbidden_tool_error or f"{role} is not permitted to invoke tools during {stage}."
                    raise SquadToolViolationError(msg, stage=stage)
            except Exception:
                failed = True
                raise
            finally:
                ledger.settle_model_call(
                    slot,
                    call_id,
                    before,
                    usage=usage,
                    elapsed_ms=int((monotonic() - t0) * 1000),
                    failed=failed,
                )

            try:
                parsed = _parse_model_output(resp, model_cls, **norm_args)
                self._record_repair_diagnostic({
                    "stage": stage,
                    "role": role,
                    "schema": model_cls.__name__,
                    "repair_attempts": attempt_idx,
                    "succeeded": True,
                    "used_fallback": False,
                    "last_error": str(last_exc)[:400] if last_exc else None,
                })
                return parsed, False, None
            except (ValidationError, ValueError, TypeError) as exc:
                last_exc = exc
                if attempt_idx >= max_repairs:
                    break
                prior_raw = resp.content or ""
                if not prior_raw and isinstance(resp.parsed, Mapping):
                    prior_raw = json.dumps(resp.parsed, ensure_ascii=False, default=str)
                messages.append({"role": "assistant", "content": prior_raw[:3000] or "<invalid output>"})
                messages.append({
                    "role": "user",
                    "content": _format_pydantic_repair_prompt(
                        model_cls,
                        exc,
                        attempt=attempt_idx + 1,
                        max_attempts=max_repairs,
                    ),
                })

        assert last_exc is not None
        if fallback_fn is not None:
            fallback_obj = fallback_fn(last_exc)
            self._record_repair_diagnostic({
                "stage": stage,
                "role": role,
                "schema": model_cls.__name__,
                "repair_attempts": max_repairs,
                "succeeded": False,
                "used_fallback": True,
                "last_error": str(last_exc)[:400],
            })
            return fallback_obj, True, last_exc

        self._record_repair_diagnostic({
            "stage": stage,
            "role": role,
            "schema": model_cls.__name__,
            "repair_attempts": max_repairs,
            "succeeded": False,
            "used_fallback": False,
            "last_error": str(last_exc)[:400],
        })
        raise last_exc

    @staticmethod
    def _fallback_steering_decision(
        dispatch_plan: ResearchDispatchPlan,
        running_roles: set[WorkerRole],
        instructions: Sequence[UserSteeringInstruction],
    ) -> SteeringImpactDecision:
        seqs = [i.sequence for i in instructions]
        instr_text = "; ".join(i.text for i in instructions if i.text)[:800]
        planned_roles = [t.worker_role for t in dispatch_plan.tasks if t.status != "skipped"]
        affected = list(dict.fromkeys([*running_roles, *planned_roles]))
        if not affected:
            affected = ["worker_segments"]
        task_map = {t.worker_role: t for t in dispatch_plan.tasks}
        updated_tasks: list[WorkerTask] = []
        for r in affected:
            base = task_map.get(r)
            if base is not None:
                updated_tasks.append(base.model_copy(update={
                    "task_id": _new_id("task"),
                    "status": "planned",
                    "objective": f"{base.objective} | User steering: {instr_text}".strip(" |")[:2000],
                    "steering_instruction_sequence": max(seqs) if seqs else base.steering_instruction_sequence,
                }))
        return SteeringImpactDecision(
            instruction_sequences=seqs,
            affected_workers=affected,
            cancel_running_workers=sorted(running_roles),
            invalidate_stage_2_5=True,
            invalidate_stage_3a=True,
            rationale="Deterministic steering fallback: re-dispatching active/running workers with latest user instructions.",
            updated_tasks=updated_tasks,
        )

    def _run_stage_1_plan(
        self,
        *,
        request: SquadPrefetchRequest,
        snapshot: EarningsPrefetchSnapshot,
        ledger: SquadExecutionLedger,
        emitter: OrgGraphEmitter,
        instruction_seq: int,
        gaps_out: list[SquadGap] | None = None,
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

        allowed_tools_table = {r: sorted(v) for r, v in WORKER_ALLOWED_TOOLS.items()}
        sys_prompt = (
            "You are the Lead Coordinator (deep_reasoner) of a bounded US Buy-Side Earnings Research Squad.\n"
            "Inspect the Stage 0 snapshot, SIC sector applicability gate, and L1/L2 method pack hint, and return a SINGLE raw JSON object matching `ResearchDispatchPlan`.\n\n"
            "STRICT CONTRACT RULES (extra='forbid' on BOTH ResearchDispatchPlan and WorkerTask — unknown keys like `worker_tasks` or `dispatch_rationale` cause validation errors):\n"
            "- Top-level keys ONLY: "
            '["l1_pack_id", "selected_l2_pack_id", "selected_l2_pack_reason", "tasks", "cross_worker_hypotheses", "planning_notes", "instruction_sequence"]\n'
            "- Field types:\n"
            "  * l1_pack_id: string (\"earnings_review\")\n"
            "  * selected_l2_pack_id: string or null (MUST be null when sector_applicability.is_gated is true)\n"
            "  * selected_l2_pack_reason: string explaining sector KPI pack choice or SIC gate\n"
            "  * tasks: array of 1 to 3 unique WorkerTask objects for worker_segments, worker_forensics, worker_guidance\n"
            "  * cross_worker_hypotheses: array of concise strings\n"
            "  * planning_notes: plain string summary\n"
            "  * instruction_sequence: integer matching input instruction_sequence\n"
            "- Each WorkerTask keys ONLY: "
            '["worker_role", "status", "skip_reason", "objective", "target_r_files", "target_flag_ids", "target_section_keys", "allowed_tools", "soft_turn_limit", "round_number"]\n'
            "  * worker_role enum: [\"worker_segments\", \"worker_forensics\", \"worker_guidance\"]\n"
            "  * status enum: [\"planned\", \"skipped\"]\n"
            "  * skip_reason: REQUIRED non-empty string if status='skipped'; omit or null if status='planned'\n"
            "  * allowed_tools: either omit or pass ONLY a subset of that role's permitted tools: "
            f"{json.dumps(allowed_tools_table)}\n"
            "  * round_number: 1, soft_turn_limit: integer 1..8 (recommend 4)\n\n"
            "Valid JSON example:\n"
            "{\n"
            '  "l1_pack_id": "earnings_review",\n'
            '  "selected_l2_pack_id": "kpi_saas_cloud",\n'
            '  "selected_l2_pack_reason": "SIC 7372 maps to SaaS & Cloud KPI pack.",\n'
            '  "tasks": [\n'
            '    {"worker_role": "worker_segments", "status": "planned", "objective": "Verify segment revenue and NRR.", "target_r_files": ["R10.htm"], "target_flag_ids": [], "target_section_keys": [], "soft_turn_limit": 4, "round_number": 1},\n'
            '    {"worker_role": "worker_forensics", "status": "planned", "objective": "Investigate DSO and redline changes.", "target_r_files": [], "target_flag_ids": ["ar_surge_vs_rev"], "target_section_keys": ["mda"], "soft_turn_limit": 4, "round_number": 1},\n'
            '    {"worker_role": "worker_guidance", "status": "skipped", "skip_reason": "Consensus and transcript unavailable and no 8-K outlook provided.", "objective": "Skip guidance.", "target_r_files": [], "target_flag_ids": [], "target_section_keys": [], "soft_turn_limit": 4, "round_number": 1}\n'
            "  ],\n"
            '  "cross_worker_hypotheses": ["Check whether segment backlog expansion explains DSO divergence."],\n'
            '  "planning_notes": "Targeted 2-worker dispatch with consensus skipped.",\n'
            f'  "instruction_sequence": {instruction_seq}\n'
            "}\n"
            "Reply ONLY with raw JSON (no markdown commentary)."
        )

        composed_pack = pack_ctx.get("composed_pack")
        ind_pack = getattr(composed_pack, "industry_pack", None)
        l2_kpi_preview = [f"{k.id}: {k.name_en}" for k in ind_pack.kpis[:8]] if ind_pack else []
        cat = snapshot.filing_table_catalog
        cat_summary = (
            {
                "accession": cat.accession,
                "statements": [{"r_file": t.r_file, "short_name": t.short_name} for t in cat.statements[:12]],
                "tables": [{"r_file": t.r_file, "short_name": t.short_name} for t in cat.tables[:15]],
            }
            if cat is not None
            else None
        )
        user_payload = {
            "question": request.question,
            "instruction_sequence": instruction_seq,
            "method_pack_hint": {
                "l1_pack_id": pack_ctx["l1_pack_id"],
                "suggested_l2_pack_id": pack_ctx["l2_pack_id"],
                "reason": pack_ctx["reason"],
                "recommended_alternatives": pack_ctx["recommended_alternatives"],
                "suggested_l2_kpis": l2_kpi_preview,
            },
            "snapshot_summary": {
                "entity_id": snapshot.entity_id,
                "symbol": snapshot.symbol,
                "cik": snapshot.cik,
                "company_name": snapshot.company_name,
                "period_label": snapshot.period_label,
                "sic_code": snapshot.sic_code,
                "filing_accession": snapshot.filing_accession,
                "prior_filing_accession": snapshot.prior_filing_accession,
                "current_filing_url": snapshot.current_filing_url,
                "prior_filing_url": snapshot.prior_filing_url,
                "sector_applicability": snapshot.sector_applicability.model_dump(mode="json"),
                "flags": [f.model_dump(mode="json") for f in snapshot.flags],
                "has_filing_catalog": snapshot.filing_table_catalog is not None,
                "filing_table_catalog_summary": cat_summary,
                "has_earnings_release": snapshot.earnings_release is not None,
                "has_redline": snapshot.redline_summary is not None,
                "redline_section_keys": (
                    list(snapshot.redline_summary.section_diffs.keys())
                    if snapshot.redline_summary
                    else []
                ),
                "guidance_nongaap_summary": (
                    {
                        "warning_count": len(snapshot.guidance_nongaap.warnings),
                        "warning_flag_ids": [w.flag_id for w in snapshot.guidance_nongaap.warnings],
                    }
                    if snapshot.guidance_nongaap is not None
                    else None
                ),
                "valuation_summary": (
                    {
                        "pe_ttm": str(snapshot.valuation.pe_ttm.value) if snapshot.valuation.pe_ttm.value is not None else None,
                        "ev_to_ebitda": str(snapshot.valuation.ev_to_ebitda.value) if snapshot.valuation.ev_to_ebitda.value is not None else None,
                        "fcf_yield_pct": str(snapshot.valuation.fcf_yield_pct.value) if snapshot.valuation.fcf_yield_pct.value is not None else None,
                    }
                    if snapshot.valuation is not None
                    else None
                ),
                "prefetched_sources": [
                    {
                        "source_id": s.source_id,
                        "title": s.title,
                        "kind": s.kind,
                        "retrieval_method": s.retrieval_method,
                        "total_characters": s.total_characters,
                        "permissions": dict(s.permissions),
                    }
                    for s in snapshot.prefetched_sources[:20]
                ],
                "consensus_status": snapshot.consensus_summary.status,
                "transcript_status": snapshot.transcript_index.status,
                "open_gaps": [g.model_dump(mode="json") for g in snapshot.open_gaps],
            },
        }
        user_prompt = json.dumps(user_payload, ensure_ascii=False, sort_keys=True, default=str)

        try:
            plan, used_fallback, last_err = self._invoke_and_parse_with_repair(
                slot="deep_reasoner",
                stage="stage_1_plan",
                role="lead_coordinator",
                node_id="lead_coordinator",
                system_prompt=sys_prompt,
                user_prompt=user_prompt,
                model_cls=ResearchDispatchPlan,
                ledger=ledger,
                emitter=emitter,
                norm_kwargs={"instruction_seq": instruction_seq},
                fallback_fn=lambda _exc: _fallback_dispatch_plan(snapshot, pack_ctx, instruction_seq=instruction_seq),
                conversation_id=f"stage1:seq{instruction_seq}",
                forbidden_tool_error="Lead Coordinator must not invoke retrieval tools during stage_1_plan.",
            )
            if used_fallback and gaps_out is not None:
                gaps_out.append(SquadGap(
                    stage="stage_1_plan",
                    worker_role=None,
                    code="stage1_plan_fallback_used",
                    message=(
                        f"Stage 1 Lead output failed ResearchDispatchPlan validation after "
                        f"{self._max_repair_attempts} repair(s); deterministic 3-worker dispatch plan used "
                        f"({last_err})."
                    )[:1000],
                ))
            if plan.instruction_sequence != instruction_seq:
                plan = plan.model_copy(update={"instruction_sequence": instruction_seq})
        except Exception as exc:
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
                    state_lock=cp_lock,
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
                    steer_gaps: list[SquadGap] = []
                    decision = self._evaluate_steering_impact(
                        request=request,
                        snapshot=snapshot,
                        dispatch_plan=dispatch_plan,
                        completed_batches=state["worker_batches"],
                        running_roles=running_roles,
                        instructions=pending_instr,
                        ledger=ledger,
                        emitter=emitter,
                        gaps_out=steer_gaps,
                    )
                    if steer_gaps:
                        with cp_lock:
                            state["open_gaps"] = _merge_gaps(state["open_gaps"], steer_gaps)
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
        steer_gaps: list[SquadGap] = []
        decision = self._evaluate_steering_impact(
            request=request,
            snapshot=snapshot,
            dispatch_plan=dispatch_plan,
            completed_batches=state["worker_batches"],
            running_roles=set(),
            instructions=pending_instr,
            ledger=ledger,
            emitter=emitter,
            gaps_out=steer_gaps,
        )
        if steer_gaps:
            with cp_lock:
                state["open_gaps"] = _merge_gaps(state["open_gaps"], steer_gaps)
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
        gaps_out: list[SquadGap] | None = None,
    ) -> SteeringImpactDecision:
        emitter.emit(
            stage="steering",
            node_id="lead_coordinator",
            action_kind="INSPECTING",
            runtime_state="running",
            current_activity=f"Evaluating mid-run user steering ({len(instructions)} new instruction(s)).",
            slot="deep_reasoner",
        )
        allowed_tools_table = {r: sorted(v) for r, v in WORKER_ALLOWED_TOOLS.items()}
        sys_prompt = (
            "You are the Lead Coordinator handling mid-run user steering instructions.\n"
            "Incrementally evaluate impact and return ONLY a single raw JSON object matching `SteeringImpactDecision`.\n"
            "Do NOT re-dispatch unrelated completed workers.\n\n"
            "STRICT CONTRACT RULES (extra='forbid' on SteeringImpactDecision and WorkerTask):\n"
            '- Top-level keys ONLY: ["instruction_sequences", "affected_workers", "cancel_running_workers", "invalidate_stage_2_5", "invalidate_stage_3a", "rationale", "updated_tasks"]\n'
            '- worker_role enum: ["worker_segments", "worker_forensics", "worker_guidance"]\n'
            "- Every `worker_role` in `updated_tasks` MUST also appear in `affected_workers`.\n"
            f"- Any `allowed_tools` inside `updated_tasks` MUST remain a subset of: {json.dumps(allowed_tools_table)}\n\n"
            "Valid JSON example:\n"
            "{\n"
            f'  "instruction_sequences": {[i.sequence for i in instructions]},\n'
            '  "affected_workers": ["worker_guidance"],\n'
            '  "cancel_running_workers": [],\n'
            '  "invalidate_stage_2_5": true,\n'
            '  "invalidate_stage_3a": true,\n'
            '  "rationale": "Only worker_guidance covers management outlook requested by the user.",\n'
            '  "updated_tasks": [\n'
            '    {"worker_role": "worker_guidance", "status": "planned", "objective": "Focus on FY26 capex outlook.", "target_r_files": [], "target_flag_ids": [], "target_section_keys": [], "soft_turn_limit": 4, "round_number": 1}\n'
            "  ]\n"
            "}\n"
            "Reply ONLY with raw JSON (no markdown commentary)."
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
        decision, used_fallback, last_err = self._invoke_and_parse_with_repair(
            slot="deep_reasoner",
            stage="steering",
            role="lead_coordinator",
            node_id="lead_coordinator",
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
            model_cls=SteeringImpactDecision,
            ledger=ledger,
            emitter=emitter,
            norm_kwargs={"instructions": instructions},
            fallback_fn=lambda _exc: self._fallback_steering_decision(dispatch_plan, running_roles, instructions),
            conversation_id=f"steering:seq{instructions[-1].sequence if instructions else 0}",
            forbidden_tool_error="Lead Coordinator must not invoke tools during steering impact evaluation.",
        )
        if used_fallback and gaps_out is not None:
            gaps_out.append(SquadGap(
                stage="steering",
                worker_role=None,
                code="steering_fallback_used",
                message=(
                    f"Steering impact evaluation fell back after {self._max_repair_attempts} repair(s): "
                    f"{last_err}"
                )[:1000],
            ))
        if not decision.instruction_sequences:
            decision = decision.model_copy(update={
                "instruction_sequences": [i.sequence for i in instructions]
            })
        return decision

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
        state_lock: RLock | None = None,
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
        l1_l2_pack = _render_l1_l2_pack_for_worker(
            snapshot,
            l2_pack_id=l2_pack_id,
            worker_role=role,
        )
        tool_sop = _worker_tool_sop(role, allowed_set, snapshot)
        sys_prompt = (
            f"You are {role} (slot fast_extractor), a strictly read-only institutional buy-side financial analyst worker.\n"
            f"Allowed tools ONLY: {sorted(allowed_set)}. NEVER call disallowed or write tools.\n"
            f"Follow the L1/L2 method pack checklists, unit/period checks, and KPI formulas provided in `method_pack`.\n\n"
            f"{tool_sop}\n\n"
            "FINAL OUTPUT CONTRACT (extra='forbid' on EvidenceCardBatch, EvidenceCard, and EvidenceCitation):\n"
            '- Top-level EvidenceCardBatch keys ONLY: ["batch_id", "task_id", "worker_role", "round_number", "status", "cards", "unresolved_gaps", "tools_invoked", "blocked_tool_attempts", "tool_calls_made", "turns_used", "error", "instruction_sequence"]\n'
            '  * status enum: ["completed", "partial", "failed", "skipped", "cancelled"] (use "completed" or "partial")\n'
            '- Each EvidenceCard keys ONLY: ["card_id", "worker_role", "topic_tag", "finding", "supporting_fact_ids", "citations", "caveat", "unresolved_gaps", "numeric_claims", "round_number", "instruction_sequence"]\n'
            '  * Provenance rule: every EvidenceCard MUST have non-empty `citations` OR non-empty `supporting_fact_ids`.\n'
            '- Each EvidenceCitation keys ONLY: ["source_id", "quotation", "location", "character_start", "character_end", "content_sha256", "fact_id"]\n'
            '  * `quotation` MUST be an exact verbatim excerpt from the source text returned by `read_sources`/`search_source`/Stage 0 (do NOT invent placeholders like "...").\n\n'
            "Valid EvidenceCardBatch JSON example:\n"
            "{\n"
            f'  "task_id": "{task.task_id}",\n'
            f'  "worker_role": "{role}",\n'
            f'  "round_number": {task.round_number},\n'
            '  "status": "completed",\n'
            '  "cards": [\n'
            "    {\n"
            '      "card_id": "card_rev_1",\n'
            f'      "worker_role": "{role}",\n'
            '      "topic_tag": "segment_growth",\n'
            '      "finding": "Cloud revenue reached $720M (+28% YoY) per EX-99.1 and R10.htm.",\n'
            '      "citations": [{"source_id": "src_8k_release_0001", "quotation": "Cloud revenue was $720 million", "location": "EX-99.1"}],\n'
            '      "supporting_fact_ids": [],\n'
            '      "numeric_claims": [{"metric_name": "Cloud Revenue", "current_value": "$720M", "prior_value": "$562M", "unit": "USD millions", "period_label": "Q4 2025"}]\n'
            "    }\n"
            "  ],\n"
            '  "unresolved_gaps": []\n'
            "}\n"
            "When ready to finish (or when tools are disabled), reply ONLY with a single raw JSON object."
        )
        init_payload = {
            "question": request.question,
            "worker_role": role,
            "round_number": task.round_number,
            "l2_pack_id": l2_pack_id,
            "method_pack": l1_l2_pack,
            "task": task.model_dump(mode="json"),
            "snapshot": _compact_snapshot_for_worker(snapshot, worker_role=role),
        }
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": json.dumps(init_payload, ensure_ascii=False, sort_keys=True, default=str)},
        ]

        worker_conv_id = f"{task.task_id}:r{task.round_number}"
        tools_invoked: list[str] = []
        blocked_attempts: list[str] = []
        tool_calls_made = 0
        turns_used = 0
        loop_limit = 2 if task.soft_turn_limit == 1 else task.soft_turn_limit

        for turn_idx in range(loop_limit):
            check_active()
            if turn_idx >= 2:
                _compact_stale_worker_tool_messages(messages, keep_recent_tool_turns=1)
            is_wrapup_turn = (turn_idx == loop_limit - 1) and (turn_idx >= 1)
            turn_tools = [] if is_wrapup_turn else allowed_defs
            if is_wrapup_turn:
                messages.append({
                    "role": "user",
                    "content": (
                        "FINAL WRAP-UP TURN (tools disabled): You have reached the turn limit. "
                        "Do NOT call any tools. Reply ONLY with a single raw JSON object matching `EvidenceCardBatch` "
                        "(extra='forbid') synthesizing all verified findings gathered above, and list any remaining "
                        "missing sources or unverified items in `unresolved_gaps`."
                    ),
                })

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
                    tools=turn_tools,
                    response_schema_name="EvidenceCardBatch",
                    response_schema=_cached_model_json_schema(EvidenceCardBatch),
                    binding=self._slot_bindings["fast_extractor"],
                    turn_index=turn_idx,
                    conversation_id=worker_conv_id,
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
                max_repairs = max(0, int(self._max_repair_attempts))
                cur_resp = resp
                repair_forced_err: Exception | None = None
                last_parse_err: Exception | None = None
                for repair_idx in range(max_repairs + 1):
                    try:
                        if repair_forced_err is not None:
                            raise repair_forced_err
                        batch = _parse_worker_batch(
                            cur_resp,
                            task=task,
                            role=role,
                            tools_invoked=tools_invoked,
                            blocked_attempts=blocked_attempts,
                            tool_calls_made=tool_calls_made,
                            turns_used=turns_used,
                        )
                        self._record_repair_diagnostic({
                            "stage": stage_name,
                            "role": role,
                            "schema": "EvidenceCardBatch",
                            "repair_attempts": repair_idx,
                            "succeeded": True,
                            "used_fallback": False,
                            "last_error": str(last_parse_err)[:400] if last_parse_err else None,
                        })
                        has_deliverable = bool(batch.cards)
                        is_effectively_done = batch.status == "completed" or (
                            batch.status == "partial" and has_deliverable
                        )
                        gap_note = f" ({len(batch.unresolved_gaps)} gap(s))" if batch.unresolved_gaps else ""
                        emitter.emit(
                            stage=stage_name,
                            node_id=role,
                            action_kind="RESPONDING" if is_effectively_done else "BLOCKED",
                            runtime_state=(
                                "completed"
                                if is_effectively_done
                                else ("failed" if batch.status == "failed" else "blocked")
                            ),
                            current_activity=(
                                f"{role} emitted {len(batch.cards)} EvidenceCard(s) "
                                f"in {turns_used} turn(s){gap_note}."
                            ),
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
                    except (ValidationError, ValueError, TypeError) as exc:
                        last_parse_err = exc
                        repair_forced_err = None
                        if repair_idx >= max_repairs:
                            break
                        check_active()
                        prior_raw = cur_resp.content or ""
                        if not prior_raw and isinstance(cur_resp.parsed, Mapping):
                            prior_raw = json.dumps(cur_resp.parsed, ensure_ascii=False, default=str)
                        messages.append({"role": "assistant", "content": prior_raw[:3000] or "<invalid output>"})
                        messages.append({
                            "role": "user",
                            "content": _format_pydantic_repair_prompt(
                                EvidenceCardBatch,
                                exc,
                                attempt=repair_idx + 1,
                                max_attempts=max_repairs,
                            ),
                        })
                        emitter.emit(
                            stage=stage_name,
                            node_id=role,
                            action_kind="INSPECTING",
                            runtime_state="running",
                            current_activity=(
                                f"{role} repairing EvidenceCardBatch JSON output "
                                f"(repair {repair_idx + 1}/{max_repairs})."
                            ),
                            slot="fast_extractor",
                        )
                        turns_used += 1
                        rep_call_id, rep_before = ledger.admit_model_call("fast_extractor", stage=stage_name)
                        rep_t0 = monotonic()
                        rep_failed = False
                        rep_usage: ModelUsage | None = None
                        try:
                            cur_resp = self._model_runner.invoke(SlotModelRequest(
                                slot="fast_extractor",
                                stage=stage_name,
                                role=role,
                                system_prompt=sys_prompt,
                                user_prompt=messages[-1]["content"],
                                messages=list(messages),
                                tools=[],
                                response_schema_name="EvidenceCardBatch",
                                response_schema=EvidenceCardBatch.model_json_schema(),
                                binding=self._slot_bindings["fast_extractor"],
                                turn_index=turn_idx + repair_idx + 1,
                                conversation_id=worker_conv_id,
                            ))
                            rep_usage = cur_resp.usage
                        except Exception:
                            rep_failed = True
                            raise
                        finally:
                            ledger.settle_model_call(
                                "fast_extractor",
                                rep_call_id,
                                rep_before,
                                usage=rep_usage,
                                elapsed_ms=int((monotonic() - rep_t0) * 1000),
                                failed=rep_failed,
                            )
                        if cur_resp.tool_calls:
                            repair_forced_err = ValueError(
                                "Tools are disabled during schema repair; emit ONLY valid EvidenceCardBatch JSON."
                            )

                # Partial Survival when worker exhausts all repair attempts
                self._record_repair_diagnostic({
                    "stage": stage_name,
                    "role": role,
                    "schema": "EvidenceCardBatch",
                    "repair_attempts": max_repairs,
                    "succeeded": False,
                    "used_fallback": True,
                    "last_error": str(last_parse_err)[:400] if last_parse_err else None,
                })
                repair_gap_msg = (
                    f"{role} EvidenceCardBatch failed schema validation after {max_repairs} repair(s): "
                    f"{last_parse_err}"
                )[:1000]
                emitter.emit(
                    stage=stage_name,
                    node_id=role,
                    action_kind="RESPONDING",
                    runtime_state="completed",
                    current_activity=repair_gap_msg,
                    blocker_text=repair_gap_msg,
                    edge_event=OrgEdgeEvent(
                        from_node_id=role,
                        to_node_id="lead_coordinator",
                        is_review=True,
                        edge_kind="report",
                        label="Partial (repair exhausted)",
                    ),
                    slot="fast_extractor",
                )
                return EvidenceCardBatch(
                    task_id=task.task_id,
                    worker_role=role,
                    round_number=task.round_number,
                    status="partial",
                    cards=[],
                    unresolved_gaps=[repair_gap_msg],
                    tools_invoked=list(dict.fromkeys(tools_invoked)),
                    blocked_tool_attempts=list(dict.fromkeys(blocked_attempts)),
                    tool_calls_made=tool_calls_made,
                    turns_used=turns_used,
                    instruction_sequence=task.steering_instruction_sequence,
                )

            # Execute tool calls inside the strict read-only subset boundary
            if is_wrapup_turn or turn_idx >= loop_limit - 1:
                # Wrap-up turn exhausted while still requesting tool calls; do not execute unbounded turns
                break

            assistant_calls_msg: list[dict[str, Any]] = []
            tool_result_msgs: list[dict[str, Any]] = []
            for tc in resp.tool_calls:
                check_active()
                invalid_raw_json_err: str | None = None
                tc_args = tc.arguments
                raw_args_for_msg = ""
                if tc_args is None and tc.raw_arguments is not None:
                    stripped_raw = tc.raw_arguments.strip()
                    if not stripped_raw:
                        tc_args = {}
                    else:
                        try:
                            parsed_raw = json.loads(stripped_raw)
                        except ValueError as exc:
                            invalid_raw_json_err = str(exc)
                            parsed_raw = None
                        if isinstance(parsed_raw, Mapping):
                            tc_args = dict(parsed_raw)
                        elif invalid_raw_json_err is None:
                            invalid_raw_json_err = f"Expected JSON object, got {type(parsed_raw).__name__}."
                if tc_args is None:
                    tc_args = {}
                raw_args_for_msg = (
                    tc.raw_arguments
                    if invalid_raw_json_err is not None and tc.raw_arguments is not None
                    else json.dumps(tc_args, ensure_ascii=False, default=str)
                )
                assistant_calls_msg.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": raw_args_for_msg},
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

                if invalid_raw_json_err is not None:
                    ledger.record_tool_call(stage=stage_name)
                    tool_calls_made += 1
                    tool_result_msgs.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps({
                            "status": "failed",
                            "reason": "tool_arguments_invalid_json",
                            "tool": tc.name,
                            "message": f"Malformed JSON in tool arguments for {tc.name}: {invalid_raw_json_err}",
                            "hint": _TOOL_ARGUMENT_HINTS.get(
                                tc.name,
                                "Pass a valid JSON object matching the tool parameter schema.",
                            ),
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
                with state_lock if state_lock is not None else nullcontext():
                    if isinstance(self._tool_runner, FinToolboxToolRunner):
                        for sid, src in self._tool_runner._eligible_sources_snapshot().items():
                            source_texts[sid] = src.text
                            source_permissions[sid] = src.permissions.model_dump(mode="json")
                    elif isinstance(out, Mapping) and isinstance(out.get("source_id"), str) and isinstance(out.get("text"), str):
                        source_texts[out["source_id"]] = out["text"]
                compacted_out = _compact_tool_output_for_worker(tc.name, out)
                tool_result_msgs.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(compacted_out, ensure_ascii=False, default=str),
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
            allowed_tools_table = {r: sorted(v) for r, v in WORKER_ALLOWED_TOOLS.items()}
            sys_prompt = (
                "You are the Lead Coordinator performing Stage 2.5 cross-worker review.\n"
                "You have a HARD CAP of 1 targeted follow-up round. If no cross-worker follow-up is needed, "
                "set has_cross_worker_leads=false and followup_tasks=[].\n\n"
                "STRICT CONTRACT RULES (extra='forbid' on Stage25ReviewDecision and WorkerTask):\n"
                '- Top-level keys ONLY: ["has_cross_worker_leads", "followup_tasks", "rationale"]\n'
                '- Nested WorkerTask keys ONLY: ["worker_role", "status", "skip_reason", "objective", "target_r_files", "target_flag_ids", "target_section_keys", "allowed_tools", "soft_turn_limit", "round_number"]\n'
                '- worker_role enum: ["worker_segments", "worker_forensics", "worker_guidance"]\n'
                "- In followup_tasks, every task MUST have a unique worker_role and round_number=2.\n"
                f"- Any allowed_tools must be a subset of: {json.dumps(allowed_tools_table)}\n\n"
                "Valid JSON example (no follow-up):\n"
                '{"has_cross_worker_leads": false, "followup_tasks": [], "rationale": "Workers corroborate segments and cash conversion without unresolved leads."}\n'
                "Reply ONLY with raw JSON (no markdown commentary)."
            )
            cat = snapshot.filing_table_catalog
            user_prompt = json.dumps({
                "question": request.question,
                "l2_pack_id": dispatch_plan.selected_l2_pack_id,
                "snapshot_flags": [f.model_dump(mode="json") for f in snapshot.flags],
                "prefetched_source_ids": [s.source_id for s in snapshot.prefetched_sources[:20]],
                "filing_table_r_files": (
                    [r.r_file for r in [*cat.statements[:10], *cat.tables[:10]]]
                    if cat is not None
                    else []
                ),
                "worker_batches": {k: v.model_dump(mode="json") for k, v in state["worker_batches"].items()},
                "open_gaps": [g.model_dump(mode="json") for g in state["open_gaps"]],
            }, ensure_ascii=False, sort_keys=True, default=str)

            decision, used_fallback, last_err = self._invoke_and_parse_with_repair(
                slot="deep_reasoner",
                stage="stage_2_5_review",
                role="lead_coordinator",
                node_id="lead_coordinator",
                system_prompt=sys_prompt,
                user_prompt=user_prompt,
                model_cls=Stage25ReviewDecision,
                ledger=ledger,
                emitter=emitter,
                fallback_fn=lambda exc: Stage25ReviewDecision(
                    has_cross_worker_leads=False,
                    followup_tasks=[],
                    rationale=f"Deterministic Stage 2.5 fallback: skipped follow-up after schema validation failure ({exc})."[:1000],
                ),
                conversation_id="stage2_5_review",
                forbidden_tool_error="Lead Coordinator must not invoke retrieval tools during stage_2_5_review.",
            )
            if used_fallback:
                with cp_lock:
                    state["open_gaps"] = _merge_gaps(state["open_gaps"], [SquadGap(
                        stage="stage_2_5_review",
                        worker_role=None,
                        code="stage25_fallback_skipped",
                        message=(
                            f"Stage 2.5 cross-worker review skipped follow-up via fallback after "
                            f"{self._max_repair_attempts} repair(s): {last_err}"
                        )[:1000],
                    )])
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
                    state_lock=cp_lock,
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

        emitter.emit(
            stage="stage_2_5_review",
            node_id="lead_coordinator",
            action_kind="RESPONDING",
            runtime_state="completed",
            current_activity=(
                f"Stage 2.5 follow-up complete ({len(active_followups)} targeted task(s)); "
                "handing off to Bear Reviewer."
            ),
            slot="deep_reasoner",
        )
        state["stage25_executed"] = True

    # --------------------------------------------------------------------------- Stage 3A Bear Reviewer
    def _run_stage_3a_bear(
        self,
        *,
        snapshot: EarningsPrefetchSnapshot,
        cards: Sequence[EvidenceCard],
        open_gaps: Sequence[SquadGap],
        question: str | None = None,
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

        sys_prompt, user_prompt = build_isolated_bear_prompt(snapshot, cards, open_gaps, question=question)
        try:
            parsed_batch, used_fallback, last_err = self._invoke_and_parse_with_repair(
                slot="bear_reviewer",
                stage="stage_3a_bear",
                role="bear_reviewer",
                node_id="bear_reviewer",
                system_prompt=sys_prompt,
                user_prompt=user_prompt,
                model_cls=BearCaseBatch,
                ledger=ledger,
                emitter=emitter,
                fallback_fn=lambda _exc: _fallback_bear_batch(snapshot, cards, open_gaps),
                conversation_id="stage3a_bear",
                forbidden_tool_error="bear_reviewer is not permitted to invoke tools.",
            )
            accepted, rejected, bear_gaps = gate_bear_cases(
                parsed_batch.items, snapshot, cards, open_gaps
            )
            rejected_all = [*parsed_batch.rejected_items, *rejected]
            if used_fallback:
                bear_gaps = _merge_gaps(bear_gaps, [SquadGap(
                    stage="stage_3a_bear",
                    worker_role=None,
                    code="stage3a_deterministic_fallback",
                    message=(
                        f"Bear Reviewer failed BearCaseBatch validation after "
                        f"{self._max_repair_attempts} repair(s); deterministic flag/redline bear cases used ({last_err})."
                    )[:1000],
                )])
            bear_batch = BearCaseBatch(
                batch_id=parsed_batch.batch_id,
                items=accepted,
                rejected_items=rejected_all,
                reviewer_notes=parsed_batch.reviewer_notes,
            )
        except Exception as exc:
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
        question: str = "",
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
            "question": question,
            "entity_id": snapshot.entity_id,
            "symbol": snapshot.symbol,
            "cik": snapshot.cik,
            "company_name": snapshot.company_name,
            "period_label": snapshot.period_label,
            "selected_l2_pack_id": dispatch_plan.selected_l2_pack_id,
            "snapshot": _compact_snapshot_for_writer(snapshot),
            "evidence_cards": [c.model_dump(mode="json") for c in verified_cards],
            "bear_cases": [b.model_dump(mode="json") for b in accepted_bears],
            "open_gaps": [g.model_dump(mode="json") for g in all_gaps],
            "sector_applicability": snapshot.sector_applicability.model_dump(mode="json"),
            "gate_report": gate_report.model_dump(mode="json"),
        }

        sys_prompt = _build_writer_system_prompt(
            mode="memo",
            sic_code=snapshot.sic_code,
            question=question,
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
                conversation_id=f"{run_id}:stage3b_memo",
            ))
            usage = resp.usage
            if resp.tool_calls:
                raise SquadToolViolationError("Stage 3B writer is not permitted to invoke retrieval tools.", stage="stage_3b_gate_memo")
            memo_md, writer_fallback_used = _extract_markdown_or_fallback(resp, dossier_core, mode="memo")
            if writer_fallback_used:
                all_gaps = _merge_gaps(all_gaps, [SquadGap(
                    stage="stage_3b_gate_memo",
                    worker_role=None,
                    code="stage3b_writer_fallback_used",
                    message="Stage 3B Writer returned empty content; deterministic Markdown memo fallback used.",
                )])
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


# --------------------------------------------------------------------------- Parsing, Normalization, Repair & Fallbacks
def _sanitize_tool_json_schema(node: Any) -> Any:
    """Strip lookaround regex patterns (e.g. Pydantic Decimal lookaheads) rejected by strict schema engines."""
    if isinstance(node, Mapping):
        cleaned: dict[str, Any] = {}
        for k, v in node.items():
            if k == "pattern" and isinstance(v, str) and "(?" in v:
                continue
            cleaned[k] = _sanitize_tool_json_schema(v)
        return cleaned
    if isinstance(node, list):
        return [_sanitize_tool_json_schema(item) for item in node]
    return node


@lru_cache(maxsize=64)
def _tool_def_dict(tool_name: str) -> dict[str, Any]:
    spec = TOOL_REGISTRY[tool_name]
    tdef: ModelToolDefinition = spec.to_definition()
    return {
        "name": tdef.name,
        "description": tdef.description,
        "input_schema": _sanitize_tool_json_schema(copy.deepcopy(tdef.input_schema)),
    }


_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)


def _extract_balanced_json_candidates(text: str) -> list[str]:
    """Scan text for balanced `{...}` or `[...]` JSON substrings, respecting JSON string escapes."""
    candidates: list[str] = []
    n = len(text)
    idx = 0
    while idx < n:
        ch = text[idx]
        if ch not in "{[":
            idx += 1
            continue
        stack = [ch]
        in_str = False
        escaped = False
        pos = idx + 1
        while pos < n and stack:
            cur = text[pos]
            if in_str:
                if escaped:
                    escaped = False
                elif cur == "\\":
                    escaped = True
                elif cur == '"':
                    in_str = False
            else:
                if cur == '"':
                    in_str = True
                elif cur in "{[":
                    stack.append(cur)
                elif cur == "}" and stack[-1] == "{":
                    stack.pop()
                elif cur == "]" and stack[-1] == "[":
                    stack.pop()
                elif cur in "}]":
                    break
            pos += 1
        if not stack:
            candidates.append(text[idx:pos])
            idx = pos
        else:
            idx += 1
    return candidates


def _extract_json_value(raw_text: str) -> Any:
    """Robustly extract a JSON object or array from raw LLM text (prose, fences, or raw JSON)."""
    stripped = raw_text.strip()
    if not stripped:
        raise ValueError("Empty model response text.")
    try:
        return json.loads(stripped)
    except ValueError:
        pass

    for m in _FENCED_JSON_RE.finditer(stripped):
        inner = m.group(1).strip()
        if not inner:
            continue
        try:
            return json.loads(inner)
        except ValueError:
            for cand in _extract_balanced_json_candidates(inner):
                try:
                    return json.loads(cand)
                except ValueError:
                    continue

    # Try outer balanced blocks (longest first so nested arrays don't preempt outer object)
    candidates = sorted(_extract_balanced_json_candidates(stripped), key=len, reverse=True)
    for cand in candidates:
        try:
            return json.loads(cand)
        except ValueError:
            continue
    raise ValueError("Could not extract valid JSON object or array from model output.")


def _parse_json_payload(resp: SlotModelResponse) -> Any:
    if resp.parsed is not None:
        return resp.parsed
    if isinstance(resp.content, str) and resp.content.strip():
        return _extract_json_value(resp.content)
    raise ValueError("Model response contained neither parsed object nor JSON text.")


def _coerce_to_str(val: Any, *, max_len: int | None = None) -> str:
    if val is None:
        out = ""
    elif isinstance(val, str):
        out = val.strip()
    elif isinstance(val, (list, tuple)):
        parts = [_coerce_to_str(item) for item in val]
        out = "; ".join(p for p in parts if p)
    elif isinstance(val, Mapping):
        parts = [f"{k}: {_coerce_to_str(v)}" for k, v in val.items() if _coerce_to_str(v)]
        out = "; ".join(parts) if parts else json.dumps(dict(val), ensure_ascii=False, default=str)
    else:
        out = str(val).strip()
    if max_len is not None and len(out) > max_len:
        out = out[:max_len].rstrip()
    return out


def _coerce_to_str_list(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        s = val.strip()
        return [s] if s else []
    if isinstance(val, (list, tuple)):
        out: list[str] = []
        for item in val:
            s = _coerce_to_str(item)
            if s:
                out.append(s)
        return out
    if isinstance(val, Mapping):
        return [f"{k}: {_coerce_to_str(v)}" for k, v in val.items() if _coerce_to_str(v)]
    s = str(val).strip()
    return [s] if s else []


def _normalize_worker_role_val(val: Any) -> Any:
    if not isinstance(val, str):
        return val
    key = val.strip().lower().replace("-", "_").replace(" ", "_")
    mapping = {
        "worker_segments": "worker_segments",
        "segments": "worker_segments",
        "segment": "worker_segments",
        "segments_kpis": "worker_segments",
        "segments_and_kpis": "worker_segments",
        "worker_a": "worker_segments",
        "a": "worker_segments",
        "worker_forensics": "worker_forensics",
        "forensics": "worker_forensics",
        "quality_forensics": "worker_forensics",
        "earnings_quality": "worker_forensics",
        "worker_b": "worker_forensics",
        "b": "worker_forensics",
        "worker_guidance": "worker_guidance",
        "guidance": "worker_guidance",
        "guidance_transcript": "worker_guidance",
        "guidance_and_transcript": "worker_guidance",
        "worker_c": "worker_guidance",
        "c": "worker_guidance",
    }
    return mapping.get(key, val.strip())


def _normalize_task_status_val(val: Any) -> Any:
    if not isinstance(val, str):
        return val
    key = val.strip().lower()
    if key in ("skip", "skipped", "omit", "disabled"):
        return "skipped"
    if key in ("planned", "plan", "active", "run", "running", "pending", "ready", "enabled"):
        return "planned"
    return key


def _normalize_severity_val(val: Any) -> Any:
    if not isinstance(val, str):
        return "medium"
    key = val.strip().lower()
    if key in ("high", "critical", "severe", "major"):
        return "high"
    if key in ("medium", "med", "moderate"):
        return "medium"
    if key in ("low", "minor"):
        return "low"
    return key


def _normalize_anchor_kind_val(val: Any) -> Any:
    if not isinstance(val, str):
        return val
    key = val.strip().lower().replace("-", "_").replace(" ", "_")
    mapping = {
        "flag": "flag",
        "quality_flag": "flag",
        "diagnostic_flag": "flag",
        "red_flag": "flag",
        "yellow_flag": "flag",
        "earnings_quality": "flag",
        "redline": "redline",
        "redline_diff": "redline",
        "disclosure_diff": "redline",
        "risk_factor": "redline",
        "filing_redline": "redline",
        "guidance_gap": "guidance_gap",
        "guidance": "guidance_gap",
        "nongaap": "guidance_gap",
        "non_gaap": "guidance_gap",
        "consensus_gap": "guidance_gap",
        "transcript_gap": "guidance_gap",
        "valuation_gap": "valuation_gap",
        "valuation": "valuation_gap",
        "reverse_dcf": "valuation_gap",
        "dcf": "valuation_gap",
        "multiple_gap": "valuation_gap",
    }
    return mapping.get(key, key)


def _strip_extra_keys(d: dict[str, Any], allowed_fields: Iterable[str], notes: list[str], *, prefix: str) -> dict[str, Any]:
    allowed = set(allowed_fields)
    dropped = [k for k in list(d.keys()) if k not in allowed]
    for k in dropped:
        d.pop(k, None)
    if dropped:
        notes.append(f"stripped {prefix} extra keys: {sorted(dropped)}")
    return d


def _normalize_worker_task_dict(raw: Any, *, default_round: int = 1, notes: list[str] | None = None) -> Any:
    if isinstance(raw, WorkerTask):
        return raw.model_copy(update={"round_number": 2}) if default_round == 2 and raw.round_number != 2 else raw
    if not isinstance(raw, Mapping):
        return raw
    log = notes if notes is not None else []
    d = dict(raw)
    for alias, target in (
        ("role", "worker_role"),
        ("worker", "worker_role"),
        ("agent", "worker_role"),
        ("tools", "allowed_tools"),
        ("tool_names", "allowed_tools"),
        ("goal", "objective"),
        ("description", "objective"),
        ("instruction", "objective"),
        ("task", "objective"),
        ("reason", "skip_reason"),
        ("skipped_reason", "skip_reason"),
        ("r_files", "target_r_files"),
        ("flag_ids", "target_flag_ids"),
        ("section_keys", "target_section_keys"),
        ("turn_limit", "soft_turn_limit"),
        ("max_turns", "soft_turn_limit"),
    ):
        if alias in d and target not in d:
            d[target] = d.pop(alias)

    if "worker_role" in d:
        d["worker_role"] = _normalize_worker_role_val(d["worker_role"])
    if "status" in d:
        d["status"] = _normalize_task_status_val(d["status"])
    if "objective" in d:
        d["objective"] = _coerce_to_str(d["objective"], max_len=2000)
    if "skip_reason" in d and d["skip_reason"] is not None:
        d["skip_reason"] = _coerce_to_str(d["skip_reason"], max_len=500) or None
    if d.get("status") == "skipped" and (not d.get("skip_reason") or not str(d["skip_reason"]).strip()):
        d["skip_reason"] = "Required primary source unavailable."
        log.append("defaulted blank skip_reason")
    if d.get("status") == "skipped" and (not d.get("objective") or not str(d["objective"]).strip()):
        d["objective"] = f"Skipped: {d['skip_reason']}"
        log.append("defaulted objective for skipped task")
    for list_key in ("focus_items", "target_r_files", "target_flag_ids", "target_section_keys"):
        if list_key in d:
            d[list_key] = _coerce_to_str_list(d[list_key])
    if "allowed_tools" in d:
        tools_list = _coerce_to_str_list(d["allowed_tools"])
        role_val = d.get("worker_role")
        if role_val in WORKER_ALLOWED_TOOLS:
            pool = WORKER_ALLOWED_TOOLS[role_val]
            kept = [t for t in tools_list if t in pool and t not in DISALLOWED_WORKER_TOOLS]
            dropped = [t for t in tools_list if t not in kept]
            if dropped:
                log.append(f"dropped unauthorized tools {dropped} on {role_val}")
            d["allowed_tools"] = list(dict.fromkeys(kept))
        else:
            d["allowed_tools"] = tools_list
    if default_round == 2:
        d["round_number"] = 2
    elif "round_number" in d:
        try:
            rn = int(d["round_number"])
        except (TypeError, ValueError):
            rn = default_round
        if rn not in (1, 2):
            clamped_rn = 1 if rn < 1 else 2
            log.append(f"clamped round_number {rn} -> {clamped_rn}")
            rn = clamped_rn
        d["round_number"] = rn
    if "soft_turn_limit" in d and d["soft_turn_limit"] is not None:
        try:
            tl = int(d["soft_turn_limit"])
        except (TypeError, ValueError):
            tl = 4
            log.append("defaulted non-int soft_turn_limit -> 4")
        if tl < 1 or tl > 8:
            clamped_tl = max(1, min(8, tl))
            log.append(f"clamped soft_turn_limit {tl} -> {clamped_tl}")
            tl = clamped_tl
        d["soft_turn_limit"] = tl
    return _strip_extra_keys(d, WorkerTask.model_fields.keys(), log, prefix="WorkerTask")


def _normalize_dispatch_plan(data: Any, *, instruction_seq: int = 0, notes: list[str] | None = None) -> Any:
    if isinstance(data, ResearchDispatchPlan):
        return data
    log = notes if notes is not None else []
    if isinstance(data, list):
        data = {"tasks": data}
    if not isinstance(data, Mapping):
        return data
    d = dict(data)
    for wrapper in ("dispatch_plan", "research_dispatch_plan", "result", "output"):
        if wrapper in d and isinstance(d[wrapper], Mapping) and "tasks" not in d and "worker_tasks" not in d:
            d = dict(d[wrapper])
            break
    if "plan" in d and isinstance(d["plan"], Mapping) and "tasks" not in d and "worker_tasks" not in d:
        d = dict(d["plan"])

    for alias in ("worker_tasks", "subtasks", "assignments", "workers", "plan"):
        if alias in d and isinstance(d[alias], list) and "tasks" not in d:
            d["tasks"] = d.pop(alias)
    for alias, target in (
        ("l2_pack_id", "selected_l2_pack_id"),
        ("l2_pack", "selected_l2_pack_id"),
        ("selected_pack_id", "selected_l2_pack_id"),
        ("reason", "selected_l2_pack_reason"),
        ("l2_pack_reason", "selected_l2_pack_reason"),
        ("pack_reason", "selected_l2_pack_reason"),
        ("hypotheses", "cross_worker_hypotheses"),
    ):
        if alias in d and target not in d:
            d[target] = d.pop(alias)

    # Coerce dispatch_rationale / rationale / notes / summary -> planning_notes
    for rat_alias in ("dispatch_rationale", "rationale", "notes", "summary", "reasoning"):
        if rat_alias in d:
            val = d.pop(rat_alias)
            if "planning_notes" not in d or not _coerce_to_str(d.get("planning_notes")):
                d["planning_notes"] = _coerce_to_str(val, max_len=1500)
                log.append(f"mapped {rat_alias} -> planning_notes")

    if "selected_l2_pack_reason" in d:
        d["selected_l2_pack_reason"] = _coerce_to_str(d["selected_l2_pack_reason"], max_len=600)
    if "planning_notes" in d:
        d["planning_notes"] = _coerce_to_str(d["planning_notes"], max_len=1500)
    if "cross_worker_hypotheses" in d:
        d["cross_worker_hypotheses"] = _coerce_to_str_list(d["cross_worker_hypotheses"])
    if "instruction_sequence" not in d:
        d["instruction_sequence"] = instruction_seq

    raw_tasks = d.get("tasks")
    if isinstance(raw_tasks, list):
        norm_tasks: list[Any] = []
        seen_roles: set[str] = set()
        for t in raw_tasks:
            nt = _normalize_worker_task_dict(t, default_round=1, notes=log)
            if isinstance(nt, Mapping):
                r = nt.get("worker_role")
                if isinstance(r, str) and r in seen_roles:
                    log.append(f"deduplicated duplicate worker_role {r}")
                    continue
                if isinstance(r, str):
                    seen_roles.add(r)
            norm_tasks.append(nt)
        d["tasks"] = norm_tasks[:3]
    return _strip_extra_keys(d, ResearchDispatchPlan.model_fields.keys(), log, prefix="ResearchDispatchPlan")


def _normalize_evidence_citation_dict(raw: Any, *, notes: list[str] | None = None) -> Any:
    if isinstance(raw, EvidenceCitation):
        return raw
    if isinstance(raw, str):
        return {"source_id": "unknown", "quotation": raw[:2500]}
    if not isinstance(raw, Mapping):
        return raw
    log = notes if notes is not None else []
    d = dict(raw)
    for alias, target in (
        ("source", "source_id"),
        ("quote", "quotation"),
        ("excerpt", "quotation"),
        ("text", "quotation"),
        ("verbatim", "quotation"),
        ("fact", "fact_id"),
        ("start", "character_start"),
        ("char_start", "character_start"),
        ("end", "character_end"),
        ("char_end", "character_end"),
        ("loc", "location"),
        ("section", "location"),
        ("page_or_section", "location"),
    ):
        if alias in d and target not in d:
            d[target] = d.pop(alias)
    if "quotation" in d:
        d["quotation"] = _coerce_to_str(d["quotation"], max_len=2500)
    if "location" in d and d["location"] is not None:
        d["location"] = _coerce_to_str(d["location"], max_len=240) or None
    cs = d.get("character_start")
    ce = d.get("character_end")
    if isinstance(cs, int) and cs < 0:
        d["character_start"] = None
    if isinstance(ce, int) and ce < 0:
        d["character_end"] = None
    if isinstance(d.get("character_start"), int) and isinstance(d.get("character_end"), int):
        if d["character_end"] < d["character_start"]:
            d["character_start"] = None
            d["character_end"] = None
            log.append("cleared inverted citation span")
    return _strip_extra_keys(d, EvidenceCitation.model_fields.keys(), log, prefix="EvidenceCitation")


def _normalize_evidence_card_dict(
    raw: Any,
    *,
    role: WorkerRole,
    round_number: int,
    idx: int,
    notes: list[str] | None = None,
) -> Any:
    if isinstance(raw, EvidenceCard):
        return raw
    if not isinstance(raw, Mapping):
        return raw
    log = notes if notes is not None else []
    d = dict(raw)
    for alias, target in (
        ("id", "card_id"),
        ("role", "worker_role"),
        ("title", "topic_tag"),
        ("topic", "topic_tag"),
        ("category", "topic_tag"),
        ("tag", "topic_tag"),
        ("summary", "finding"),
        ("claim", "finding"),
        ("findings", "finding"),
        ("analysis", "finding"),
        ("content", "finding"),
        ("fact_ids", "supporting_fact_ids"),
        ("facts", "supporting_fact_ids"),
        ("gaps", "unresolved_gaps"),
        ("metrics", "numeric_claims"),
        ("claims", "numeric_claims"),
    ):
        if alias in d and target not in d:
            d[target] = d.pop(alias)
    if not isinstance(d.get("card_id"), str) or not d["card_id"].strip():
        d["card_id"] = f"{role}_r{round_number}_c{idx + 1}"
    d["worker_role"] = role
    d["round_number"] = round_number
    if "topic_tag" in d:
        d["topic_tag"] = _coerce_to_str(d["topic_tag"], max_len=120) or "earnings_evidence"
    if "finding" in d:
        d["finding"] = _coerce_to_str(d["finding"], max_len=4000)
    if "caveat" in d and d["caveat"] is not None:
        d["caveat"] = _coerce_to_str(d["caveat"], max_len=1500) or None
    if "supporting_fact_ids" in d:
        d["supporting_fact_ids"] = _coerce_to_str_list(d["supporting_fact_ids"])
    if "unresolved_gaps" in d:
        d["unresolved_gaps"] = _coerce_to_str_list(d["unresolved_gaps"])
    if "citations" in d and isinstance(d["citations"], list):
        d["citations"] = [_normalize_evidence_citation_dict(c, notes=log) for c in d["citations"]]
    if "numeric_claims" in d:
        nc = d["numeric_claims"]
        if isinstance(nc, Mapping):
            d["numeric_claims"] = [dict(nc)]
        elif isinstance(nc, list):
            d["numeric_claims"] = [
                dict(item) if isinstance(item, Mapping) else {"value": _coerce_to_str(item)}
                for item in nc
            ]
        elif nc is None:
            d["numeric_claims"] = []
    return _strip_extra_keys(d, EvidenceCard.model_fields.keys(), log, prefix="EvidenceCard")


def _normalize_worker_batch_payload(
    data: Any,
    *,
    task: WorkerTask,
    role: WorkerRole,
    notes: list[str] | None = None,
) -> Any:
    if isinstance(data, EvidenceCardBatch):
        return data
    log = notes if notes is not None else []
    if isinstance(data, list):
        data = {"cards": data}
    if not isinstance(data, Mapping):
        return data
    d = dict(data)
    for wrapper in ("evidence_card_batch", "batch", "result", "output"):
        if wrapper in d and isinstance(d[wrapper], Mapping) and "cards" not in d and "evidence_cards" not in d:
            d = dict(d[wrapper])
            break
    for alias, target in (
        ("evidence_cards", "cards"),
        ("items", "cards"),
        ("results", "cards"),
        ("gaps", "unresolved_gaps"),
    ):
        if alias in d and target not in d:
            d[target] = d.pop(alias)

    if not isinstance(d.get("task_id"), str) or not d["task_id"].strip():
        d["task_id"] = task.task_id
    d["worker_role"] = role
    d["round_number"] = task.round_number
    d.setdefault("instruction_sequence", task.steering_instruction_sequence)
    if isinstance(d.get("status"), str):
        st = d["status"].strip().lower()
        d["status"] = {
            "ok": "completed",
            "success": "completed",
            "done": "completed",
            "complete": "completed",
        }.get(st, st)
    if "unresolved_gaps" in d:
        d["unresolved_gaps"] = _coerce_to_str_list(d["unresolved_gaps"])
    if isinstance(d.get("cards"), list):
        d["cards"] = [
            _normalize_evidence_card_dict(c, role=role, round_number=task.round_number, idx=i, notes=log)
            for i, c in enumerate(d["cards"])
        ]
    return _strip_extra_keys(d, EvidenceCardBatch.model_fields.keys(), log, prefix="EvidenceCardBatch")


def _normalize_stage25_decision(data: Any, *, notes: list[str] | None = None) -> Any:
    if isinstance(data, Stage25ReviewDecision):
        return data
    if not isinstance(data, Mapping):
        return data
    log = notes if notes is not None else []
    d = dict(data)
    for wrapper in ("stage25_decision", "review_decision", "decision", "result"):
        if wrapper in d and isinstance(d[wrapper], Mapping) and "has_cross_worker_leads" not in d:
            d = dict(d[wrapper])
            break
    for alias, target in (
        ("needs_followup", "has_cross_worker_leads"),
        ("followup_required", "has_cross_worker_leads"),
        ("followup_needed", "has_cross_worker_leads"),
        ("has_followups", "has_cross_worker_leads"),
        ("followups", "followup_tasks"),
        ("follow_up_tasks", "followup_tasks"),
        ("tasks", "followup_tasks"),
        ("resolved", "resolved_notes"),
    ):
        if alias in d and target not in d:
            d[target] = d.pop(alias)
    for rat_alias in ("notes", "summary", "reasoning", "explanation"):
        if rat_alias in d:
            val = d.pop(rat_alias)
            if "rationale" not in d or not _coerce_to_str(d.get("rationale")):
                d["rationale"] = _coerce_to_str(val, max_len=1500)
    if "rationale" in d:
        d["rationale"] = _coerce_to_str(d["rationale"], max_len=1500)
    if "resolved_notes" in d:
        d["resolved_notes"] = _coerce_to_str_list(d["resolved_notes"])
    if isinstance(d.get("followup_tasks"), list):
        norm_tasks: list[Any] = []
        seen_roles: set[str] = set()
        for t in d["followup_tasks"]:
            nt = _normalize_worker_task_dict(t, default_round=2, notes=log)
            if isinstance(nt, Mapping):
                r = nt.get("worker_role")
                if isinstance(r, str) and r in seen_roles:
                    continue
                if isinstance(r, str):
                    seen_roles.add(r)
            norm_tasks.append(nt)
        d["followup_tasks"] = norm_tasks[:3]
    return _strip_extra_keys(d, Stage25ReviewDecision.model_fields.keys(), log, prefix="Stage25ReviewDecision")


def _normalize_bear_item_dict(raw: Any, *, idx: int, notes: list[str] | None = None) -> Any:
    if isinstance(raw, BearCaseItem):
        return raw
    if not isinstance(raw, Mapping):
        return raw
    log = notes if notes is not None else []
    d = dict(raw)
    for alias, target in (
        ("id", "bear_id"),
        ("challenge", "thesis_challenge"),
        ("thesis", "thesis_challenge"),
        ("risk", "thesis_challenge"),
        ("description", "thesis_challenge"),
        ("bear_case", "thesis_challenge"),
        ("anchors", "anchored_evidence_ids"),
        ("evidence_ids", "anchored_evidence_ids"),
        ("anchor_ids", "anchored_evidence_ids"),
        ("evidence_anchors", "anchored_evidence_ids"),
        ("kind", "anchor_kind"),
        ("anchor_type", "anchor_kind"),
        ("invalidation_trigger", "next_quarter_invalidation_trigger"),
        ("trigger", "next_quarter_invalidation_trigger"),
        ("next_quarter_trigger", "next_quarter_invalidation_trigger"),
        ("invalidation", "next_quarter_invalidation_trigger"),
        ("numbers", "supporting_numbers"),
        ("metrics", "supporting_numbers"),
        ("evidence_numbers", "supporting_numbers"),
    ):
        if alias in d and target not in d:
            d[target] = d.pop(alias)
    if not d.get("bear_id") or not str(d.get("bear_id")).strip():
        d["bear_id"] = f"bear_{idx + 1}"
    if "thesis_challenge" in d:
        d["thesis_challenge"] = _coerce_to_str(d["thesis_challenge"], max_len=3000)
    if "next_quarter_invalidation_trigger" in d:
        d["next_quarter_invalidation_trigger"] = _coerce_to_str(d["next_quarter_invalidation_trigger"], max_len=1500)
    if "anchored_evidence_ids" in d:
        d["anchored_evidence_ids"] = _coerce_to_str_list(d["anchored_evidence_ids"])
    if "anchor_kind" in d:
        d["anchor_kind"] = _normalize_anchor_kind_val(d["anchor_kind"])
    if "severity" in d:
        d["severity"] = _normalize_severity_val(d["severity"])
    if "supporting_numbers" in d:
        d["supporting_numbers"] = _coerce_to_str_list(d["supporting_numbers"])
    return _strip_extra_keys(d, BearCaseItem.model_fields.keys(), log, prefix="BearCaseItem")


def _normalize_bear_batch(data: Any, *, notes: list[str] | None = None) -> Any:
    if isinstance(data, BearCaseBatch):
        return data
    log = notes if notes is not None else []
    if isinstance(data, list):
        data = {"items": data}
    if not isinstance(data, Mapping):
        return data
    d = dict(data)
    for wrapper in ("bear_batch", "bear_case_batch", "result", "output"):
        if wrapper in d and isinstance(d[wrapper], Mapping) and "items" not in d and "bear_cases" not in d:
            d = dict(d[wrapper])
            break
    for alias in ("bear_cases", "cases", "challenges", "risks"):
        if alias in d and "items" not in d:
            d["items"] = d.pop(alias)
            log.append(f"mapped {alias} -> items")
    for note_alias in ("notes", "summary", "rationale"):
        if note_alias in d:
            val = d.pop(note_alias)
            if "reviewer_notes" not in d or not _coerce_to_str(d.get("reviewer_notes")):
                d["reviewer_notes"] = _coerce_to_str(val, max_len=2000)
    if "reviewer_notes" in d:
        d["reviewer_notes"] = _coerce_to_str(d["reviewer_notes"], max_len=2000)
    if isinstance(d.get("items"), list):
        d["items"] = [_normalize_bear_item_dict(item, idx=i, notes=log) for i, item in enumerate(d["items"])]
    if isinstance(d.get("rejected_items"), list):
        norm_rej: list[Any] = []
        for r_item in d["rejected_items"]:
            if not isinstance(r_item, Mapping):
                norm_rej.append(r_item)
                continue
            rd = dict(r_item)
            for alias in ("reason", "rejected_reason", "rationale"):
                if alias in rd and "rejection_reason" not in rd:
                    rd["rejection_reason"] = rd.pop(alias)
            rd["rejection_reason"] = _coerce_to_str(rd.get("rejection_reason"), max_len=1000) or "Unanchored bear case."
            if "anchored_evidence_ids" in rd:
                rd["anchored_evidence_ids"] = _coerce_to_str_list(rd["anchored_evidence_ids"])
            norm_rej.append(_strip_extra_keys(rd, RejectedBearCase.model_fields.keys(), log, prefix="RejectedBearCase"))
        d["rejected_items"] = norm_rej
    return _strip_extra_keys(d, BearCaseBatch.model_fields.keys(), log, prefix="BearCaseBatch")


def _normalize_steering_decision(
    data: Any,
    *,
    instructions: Sequence[UserSteeringInstruction] = (),
    notes: list[str] | None = None,
) -> Any:
    if isinstance(data, SteeringImpactDecision):
        return data
    if not isinstance(data, Mapping):
        return data
    log = notes if notes is not None else []
    d = dict(data)
    for alias, target in (
        ("workers", "affected_workers"),
        ("target_workers", "affected_workers"),
        ("cancel_workers", "cancel_running_workers"),
        ("tasks", "updated_tasks"),
        ("revised_tasks", "updated_tasks"),
    ):
        if alias in d and target not in d:
            d[target] = d.pop(alias)
    for rat_alias in ("reasoning", "summary", "notes"):
        if rat_alias in d:
            val = d.pop(rat_alias)
            if "rationale" not in d or not _coerce_to_str(d.get("rationale")):
                d["rationale"] = _coerce_to_str(val, max_len=1500)
    if "rationale" in d:
        d["rationale"] = _coerce_to_str(d["rationale"], max_len=1500)
    if "affected_workers" in d and isinstance(d["affected_workers"], list):
        d["affected_workers"] = [_normalize_worker_role_val(r) for r in d["affected_workers"]]
    if "cancel_running_workers" in d and isinstance(d["cancel_running_workers"], list):
        d["cancel_running_workers"] = [_normalize_worker_role_val(r) for r in d["cancel_running_workers"]]
    if isinstance(d.get("updated_tasks"), list):
        d["updated_tasks"] = [_normalize_worker_task_dict(t, default_round=1, notes=log) for t in d["updated_tasks"]]
        affected = list(d.get("affected_workers") or [])
        for t in d["updated_tasks"]:
            if isinstance(t, Mapping) and t.get("worker_role") and t["worker_role"] not in affected:
                affected.append(t["worker_role"])
                log.append(f"auto-added {t['worker_role']} to affected_workers")
        d["affected_workers"] = affected
    if "instruction_sequences" not in d and instructions:
        d["instruction_sequences"] = [i.sequence for i in instructions]
    return _strip_extra_keys(d, SteeringImpactDecision.model_fields.keys(), log, prefix="SteeringImpactDecision")


def _normalize_for_model_cls(
    data: Any,
    model_cls: type[BaseModel],
    *,
    notes: list[str] | None = None,
    **kwargs: Any,
) -> Any:
    if model_cls is ResearchDispatchPlan:
        return _normalize_dispatch_plan(data, instruction_seq=int(kwargs.get("instruction_seq", 0)), notes=notes)
    if model_cls is Stage25ReviewDecision:
        return _normalize_stage25_decision(data, notes=notes)
    if model_cls is BearCaseBatch:
        return _normalize_bear_batch(data, notes=notes)
    if model_cls is SteeringImpactDecision:
        return _normalize_steering_decision(data, instructions=kwargs.get("instructions", ()), notes=notes)
    if model_cls is EvidenceCardBatch and "task" in kwargs and "role" in kwargs:
        return _normalize_worker_batch_payload(data, task=kwargs["task"], role=kwargs["role"], notes=notes)
    return data


def _format_pydantic_repair_prompt(
    model_cls: type[BaseModel],
    exc: Exception,
    *,
    attempt: int,
    max_attempts: int,
) -> str:
    err_lines: list[str] = []
    if isinstance(exc, ValidationError):
        for err in exc.errors()[:12]:
            loc = ".".join(str(x) for x in err.get("loc", ())) or "<root>"
            err_lines.append(f"- path `{loc}` ({err.get('type')}): {err.get('msg')}")
    else:
        err_lines.append(f"- parse error: {exc}")

    top_keys = sorted(model_cls.model_fields.keys())
    nested_hint = ""
    if model_cls is ResearchDispatchPlan:
        nested_hint = (
            f"Nested WorkerTask keys: {sorted(WorkerTask.model_fields.keys())}. "
            f"worker_role enum: {list(WORKER_ROLES)}. status enum: ['planned', 'skipped']. "
            f"If status='skipped', skip_reason must be non-blank. "
            f"Allowed tools per role: { {r: sorted(v) for r, v in WORKER_ALLOWED_TOOLS.items()} }."
        )
    elif model_cls is EvidenceCardBatch:
        nested_hint = (
            f"Nested EvidenceCard keys: {sorted(EvidenceCard.model_fields.keys())}. "
            f"Nested EvidenceCitation keys: {sorted(EvidenceCitation.model_fields.keys())}. "
            "Each EvidenceCard must have non-empty citations or supporting_fact_ids."
        )
    elif model_cls is Stage25ReviewDecision:
        nested_hint = (
            f"Nested WorkerTask keys: {sorted(WorkerTask.model_fields.keys())}. "
            "In Stage 2.5, followup_tasks must have distinct worker_role values and round_number=2."
        )
    elif model_cls is BearCaseBatch:
        nested_hint = (
            f"Nested BearCaseItem keys: {sorted(BearCaseItem.model_fields.keys())}. "
            "anchor_kind enum: ['flag', 'redline', 'guidance_gap', 'valuation_gap']. "
            "severity enum: ['high', 'medium', 'low']. anchored_evidence_ids must be non-empty list of valid_anchors."
        )
    elif model_cls is SteeringImpactDecision:
        nested_hint = (
            f"Nested WorkerTask keys: {sorted(WorkerTask.model_fields.keys())}. "
            "Every worker_role in updated_tasks must also appear in affected_workers."
        )

    return (
        f"SCHEMA REPAIR REQUIRED (attempt {attempt}/{max_attempts}) for `{model_cls.__name__}` (extra='forbid').\n"
        f"Your previous output failed validation with:\n" + "\n".join(err_lines) + "\n\n"
        f"Allowed top-level keys ONLY: {top_keys}.\n"
        f"{nested_hint}\n"
        "Tools are disabled. Reply ONLY with a single valid raw JSON object matching the schema (no markdown commentary)."
    )


def _fallback_dispatch_plan(
    snapshot: EarningsPrefetchSnapshot,
    pack_ctx: Mapping[str, Any],
    *,
    instruction_seq: int,
) -> ResearchDispatchPlan:
    cat = snapshot.filing_table_catalog
    r_files = (
        [r.r_file for r in [*cat.statements[:4], *cat.tables[:4]]]
        if cat is not None
        else []
    )
    flag_ids = [f.flag_id for f in snapshot.flags[:6]]
    sec_keys = list(snapshot.redline_summary.section_diffs.keys())[:6] if snapshot.redline_summary else []

    has_seg_sources = bool(r_files or snapshot.earnings_release or snapshot.prefetched_sources or snapshot.statements)
    seg_task = WorkerTask(
        worker_role="worker_segments",
        status="planned" if has_seg_sources else "skipped",
        skip_reason=None if has_seg_sources else "No filing table catalog, 8-K release, or prefetched sources available.",
        objective="Extract segment revenue, operational KPIs, and Non-GAAP metrics from SEC tables and releases.",
        target_r_files=r_files,
        soft_turn_limit=4,
    )

    has_for_sources = bool(
        snapshot.statements or snapshot.flags or snapshot.redline_summary or snapshot.filing_accession or snapshot.prefetched_sources
    )
    for_task = WorkerTask(
        worker_role="worker_forensics",
        status="planned" if has_for_sources else "skipped",
        skip_reason=None if has_for_sources else "No financial statements, flags, or SEC filings available.",
        objective="Investigate diagnostic R/Y flags, cash flow quality, and cross-period filing redlines.",
        target_flag_ids=flag_ids,
        target_section_keys=sec_keys,
        soft_turn_limit=4,
    )

    has_gui_sources = bool(
        snapshot.consensus_summary.status == "available"
        or snapshot.transcript_index.status == "available"
        or snapshot.earnings_release is not None
        or any(s.kind in ("user_supplied", "market_data") for s in snapshot.prefetched_sources)
    )
    gui_task = WorkerTask(
        worker_role="worker_guidance",
        status="planned" if has_gui_sources else "skipped",
        skip_reason=None if has_gui_sources else "Consensus, transcript, 8-K guidance, and user sources unavailable.",
        objective="Compare reported results against guidance/consensus and inspect call or 8-K outlook commentary.",
        soft_turn_limit=4,
    )

    return ResearchDispatchPlan(
        l1_pack_id=str(pack_ctx.get("l1_pack_id") or "earnings_review"),
        selected_l2_pack_id=pack_ctx.get("l2_pack_id"),
        selected_l2_pack_reason=str(pack_ctx.get("reason") or "Deterministic fallback dispatch plan.")[:600],
        tasks=[seg_task, for_task, gui_task],
        cross_worker_hypotheses=["Cross-check segment momentum against working-capital diagnostics and outlook commentary."],
        planning_notes="Deterministic Stage 1 fallback plan derived from Stage 0 snapshot.",
        instruction_sequence=instruction_seq,
    )


def _fallback_bear_batch(
    snapshot: EarningsPrefetchSnapshot,
    cards: Sequence[EvidenceCard],
    open_gaps: Sequence[SquadGap],
) -> BearCaseBatch:
    raw_candidates: list[dict[str, Any]] = []
    prioritized_flags = sorted(
        snapshot.flags,
        key=lambda f: {"red": 0, "yellow": 1, "green": 2}.get(f.severity, 3),
    )
    for f in prioritized_flags:
        if f.severity not in ("red", "yellow"):
            continue
        raw_candidates.append({
            "bear_id": f"bear_fallback_{f.flag_id}",
            "thesis_challenge": f"{f.metric_name}: {f.summary} (flag={f.flag_id}).",
            "anchored_evidence_ids": [f.flag_id],
            "anchor_kind": "flag",
            "next_quarter_invalidation_trigger": (
                f.investigation_prompt or f"Diagnostic {f.flag_id} resolves to green next quarter."
            )[:1500],
            "supporting_numbers": [f"{f.flag_id} ({f.severity}, observed={f.observed_value} {f.unit})"],
            "severity": "high" if f.severity == "red" else "medium",
        })
        if len(raw_candidates) >= 3:
            break

    if snapshot.redline_summary is not None and len(raw_candidates) < 4:
        for sec_key, sec in snapshot.redline_summary.section_diffs.items():
            if not (sec.has_substantive_changes or sec.added or sec.revised):
                continue
            snippet = ""
            if sec.added and sec.added[0].current_text:
                snippet = sec.added[0].current_text[:200]
            elif sec.revised and sec.revised[0].current_text:
                snippet = sec.revised[0].current_text[:200]
            raw_candidates.append({
                "bear_id": f"bear_fallback_redline_{sec_key}",
                "thesis_challenge": (
                    f"Redline disclosure change in {sec_key}: +{len(sec.added)} added / "
                    f"{len(sec.revised)} revised paragraphs. {snippet}".strip()
                )[:3000],
                "anchored_evidence_ids": [f"redline:{sec_key}"],
                "anchor_kind": "redline",
                "next_quarter_invalidation_trigger": f"Subsequent filing removes or mitigates new risk language in {sec_key}.",
                "supporting_numbers": [f"added={len(sec.added)}", f"revised={len(sec.revised)}"],
                "severity": "medium",
            })
            if len(raw_candidates) >= 4:
                break

    if snapshot.guidance_nongaap is not None and not raw_candidates:
        for w in snapshot.guidance_nongaap.warnings[:2]:
            raw_candidates.append({
                "bear_id": f"bear_fallback_{w.flag_id}",
                "thesis_challenge": f"{w.metric_name}: {w.summary}",
                "anchored_evidence_ids": [w.flag_id],
                "anchor_kind": "guidance_gap",
                "next_quarter_invalidation_trigger": f"Guidance/Non-GAAP warning {w.flag_id} resolves next quarter.",
                "supporting_numbers": [w.flag_id],
                "severity": "high" if w.severity == "red" else "medium",
            })

    if not raw_candidates and cards:
        c0 = cards[0]
        raw_candidates.append({
            "bear_id": f"bear_fallback_{c0.card_id}",
            "thesis_challenge": f"Adversarial check on {c0.card_id} ({c0.topic_tag}): verify durability of {c0.finding[:300]}",
            "anchored_evidence_ids": [c0.card_id],
            "anchor_kind": "flag",
            "next_quarter_invalidation_trigger": f"Next-quarter disclosure confirms KPI durability for {c0.topic_tag}.",
            "supporting_numbers": list(c0.supporting_fact_ids[:3]),
            "severity": "medium",
        })

    accepted, rejected, _ = gate_bear_cases(raw_candidates, snapshot, cards, open_gaps)
    return BearCaseBatch(
        items=accepted,
        rejected_items=rejected,
        reviewer_notes="Deterministic Stage 3A fallback bear batch anchored to Stage 0 flags/redlines.",
    )


def _parse_model_output(resp: SlotModelResponse, model_cls: type[BaseModel], **norm_kwargs: Any) -> Any:
    if isinstance(resp.parsed, model_cls):
        return resp.parsed
    data = _parse_json_payload(resp)
    normalized = _normalize_for_model_cls(data, model_cls, **norm_kwargs)
    return model_cls.model_validate(normalized)


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
        if isinstance(data, (Mapping, list)):
            normalized = _normalize_worker_batch_payload(data, task=task, role=role)
            batch = EvidenceCardBatch.model_validate(normalized)
        else:
            raise ValueError("Worker response must be an EvidenceCardBatch object or list of cards.")

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
    normalized = _normalize_bear_batch(data)
    if isinstance(normalized, Mapping):
        raw_items = list(normalized.get("items") or [])
        notes = str(normalized.get("reviewer_notes") or "")
        batch_id = str(normalized.get("batch_id") or _new_id("bearbatch"))
        return raw_items, notes, batch_id
    if isinstance(normalized, list):
        return list(normalized), "", _new_id("bearbatch")
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
