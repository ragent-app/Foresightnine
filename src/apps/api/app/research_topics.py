"""Versioned, account-owned research documents for the new workspace.

This store is independent of the legacy thesis/challenge documents.  One
aggregate contains its complete version history so an optimistic write is
atomic even when PostgreSQL and multiple API processes are used.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .storage import JsonDocumentRepository, StorageConflictError, owner_items
from .research_calculations import BINARY_FORMULAS, FormulaId, MULTI_FACT_FORMULAS, calculation_result_status
from .research_charts import ResearchChart
from .research_tables import ResearchTable
from .research_market_analytics import AnalyzeMarketSeriesRequest


class ResearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(default="", max_length=10000)
    source_ids: list[str] = Field(default_factory=list)
    scope: str = Field(default="", max_length=10000)
    as_of: datetime | None = None

    @model_validator(mode="after")
    def validate_input(self) -> ResearchInput:
        if not self.question and not any(s.strip() for s in self.source_ids):
            raise ValueError("A question or source is required.")
        if any(not s.strip() for s in self.source_ids):
            raise ValueError("Source IDs must not be blank.")
        if self.as_of is not None and self.as_of.utcoffset() is None:
            raise ValueError("as_of must include a timezone.")
        return self


class SourceLocation(BaseModel):
    """Half-open character range inside one source's stored text."""
    model_config = ConfigDict(extra="forbid")
    character_start: int = Field(ge=0)
    character_end: int

    @model_validator(mode="after")
    def validate_range(self) -> SourceLocation:
        if self.character_end <= self.character_start:
            raise ValueError("character_end must be greater than character_start.")
        return self


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=100)
    quotation: str = Field(max_length=1000)
    location: SourceLocation


def _new_claim_id() -> str:
    return "claim-" + uuid4().hex


class ResearchClaim(BaseModel):
    """One finding. `turn_id` is None for a user-authored claim."""
    model_config = ConfigDict(extra="forbid")
    claim_id: str = Field(default_factory=_new_claim_id, min_length=1, max_length=100)
    turn_id: str | None = None
    role: Literal["support", "counterargument"]
    text: str = Field(min_length=1, max_length=3000)
    citations: list[Citation] = Field(default_factory=list, max_length=10)
    challenges_claim_ids: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)
    calculation_ids: list[str] = Field(default_factory=list)
    grounding_status: Literal["mechanically_checked", "needs_review"] | None = None
    reasoning: str | None = None
    qualification: str | None = None
    edited_by_user: bool = False


class ResearchFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact_id: str = Field(min_length=1, max_length=200)
    turn_id: str | None = None
    label: str = Field(min_length=1)
    value: str = Field(min_length=1)  # Decimal string exactly as recorded.
    unit: str = Field(min_length=1)
    currency: str | None = None
    period: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    quotation: str
    character_start: int = Field(ge=0)
    character_end: int = Field(gt=0)
    location: SourceLocation
    verification: str
    currency_quote_missing: bool | None = None
    scale_quote_missing: bool | None = None
    currency_unspecified: bool | None = None
    # Set when the quoted text lies on a page recognised by OCR (see research_runtime record_facts).
    extraction_method: Literal["ocr"] | None = None
    ocr_engine: str | None = None
    ocr_mean_confidence: float | None = None


class CalculationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact_id: str
    value: str
    unit: str
    currency: str | None = None
    period: str


class CalculationScaleAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact_id: str
    from_unit: str
    to_unit: str
    from_scale: str
    to_scale: str
    multiplier: str
    original_value: str
    aligned_value: str


class ReturnContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_type: Literal["issue_price", "first_close", "open", "close", "unknown"]
    start_date: str
    end_date: str
    start_price: str
    end_price: str
    instrument: str | None = None
    price_basis: str
    identity_verified: bool
    start_price_type: Literal["open", "close", "issue_price"]
    end_price_type: Literal["open", "close", "issue_price"]


class ResearchCalculation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calculation_id: str = Field(min_length=1, max_length=200)
    return_context: ReturnContext | None = None
    turn_id: str | None = None
    formula_id: FormulaId
    formula_version: str
    expression: str
    input_verification: str
    input_precision_status: str
    period_order_check: str | None = None
    period_granularity_check: str | None = None
    inputs: list[CalculationInput] = Field(min_length=2, max_length=50)
    value: str
    unit: str
    currency: str | None = None
    rounding: str
    decimal_places: int = Field(ge=0, le=8)
    periods: str | None = None
    weights: list[str] | None = None
    scale_adjustments: list[CalculationScaleAdjustment] | None = None
    result_status: str | None = None
    input_status: str | None = None
    legacy_unit_warning: str | None = None
    authorship: str | None = None
    verification: str | None = None

    @model_validator(mode="after")
    def validate_inputs_for_formula(self) -> ResearchCalculation:
        if self.formula_id in BINARY_FORMULAS and len(self.inputs) != 2:
            raise ValueError(f"Formula {self.formula_id!r} requires exactly 2 inputs.")
        if self.formula_id in MULTI_FACT_FORMULAS and not (2 <= len(self.inputs) <= 50):
            raise ValueError(f"Formula {self.formula_id!r} requires between 2 and 50 inputs.")
        return self


class ResearchGap(BaseModel):
    """An unresolved gap. The UI localizes by `code`; `message` is an English fallback."""
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    message: str


class TurnUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_calls: int = Field(ge=0)
    search_calls: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    reported_tokens: int | None = Field(default=None, ge=0)
    unknown_usage_calls: int | None = Field(default=None, ge=0)
    reserved_estimate_tokens: int | None = Field(default=None, ge=0)
    # partial_unknown: some model call reported no usage. total_tokens is then the ledger total including
    # the reserved estimate for those calls, not a lower bound.
    usage_status: Literal['complete', 'partial_unknown'] | None = Field(default=None, description=(
        "'partial_unknown': some call's usage is unknown; total_tokens is the ledger total including "
        "reservations for those calls, not a lower bound."))


class TurnFocusCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str
    citation_index: int = Field(ge=0)


class ReadRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    character_start: int = Field(ge=0)
    character_end: int = Field(gt=0)


class MethodPackRef(BaseModel):
    """A research method pack that was loaded into the run's first message (id, version and content fingerprint)."""
    model_config = ConfigDict(extra="forbid")
    pack_id: str
    version: str
    fingerprint: str


class TurnAttribution(BaseModel):
    """Attribution that must travel with a result, for example the GDELT Project citation."""
    model_config = ConfigDict(extra="forbid")
    id: str
    text: str
    url: str | None = None


class QuestionPartReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact_id: str | None = None
    calculation_id: str | None = None
    analysis_id: str | None = None
    chart_id: str | None = None
    claim_id: str | None = None
    source_id: str | None = None
    character_start: int | None = Field(default=None, ge=0)
    character_end: int | None = Field(default=None, gt=0)
    origin: Literal["prior", "current"] = "current"

    @model_validator(mode="after")
    def validate_target(self) -> QuestionPartReference:
        if not any((self.fact_id, self.calculation_id, self.analysis_id, self.chart_id, self.claim_id, self.source_id)):
            raise ValueError("An evidence reference requires a target.")
        if self.character_start is not None or self.character_end is not None:
            if not self.source_id or self.character_start is None or self.character_end is None or self.character_end <= self.character_start:
                raise ValueError("A cited range requires a source and ordered offsets.")
        return self


class SavedMarketAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_id: str
    request: AnalyzeMarketSeriesRequest
    result: dict

    @model_validator(mode="after")
    def verify_sources(self) -> SavedMarketAnalysis:
        expected = {self.request.source_id}
        if self.request.benchmark_source_id:
            expected.add(self.request.benchmark_source_id)
        if self.result.get("source_id") != self.request.source_id or set(self.result.get("source_ids", ())) != expected:
            raise ValueError("Market analysis sources must match the analyzed series.")
        return self


class ResearchEvidenceRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    question: str
    status: Literal["satisfied", "partial", "missing", "unavailable", "conflict", "not_applicable"]
    reason: str
    references: list[QuestionPartReference] = Field(default_factory=list)
    core: bool = True


class ResearchQuestionPart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    core: bool = True
    question: str
    status: Literal["answered", "partial", "unresolved"]
    answer: str = ""
    reason: str
    references: list[QuestionPartReference] = Field(default_factory=list)
    requirements: list[ResearchEvidenceRequirement] = Field(default_factory=list)


class ResearchAttempt(BaseModel):
    operation_id: str
    reported_tokens: int | None = Field(default=None, ge=0)
    unknown_usage_calls: int | None = Field(default=None, ge=0)
    reserved_estimate_tokens: int | None = Field(default=None, ge=0)
    elapsed_seconds: float | None = Field(default=None, ge=0)
    model_calls: int = Field(ge=0)
    search_calls: int = Field(ge=0)
    new_facts: int = Field(ge=0)
    new_calculations: int = Field(ge=0)
    new_claims: int = Field(ge=0)
    answered_parts: int = Field(ge=0)
    reason: str


class ResearchTurn(BaseModel):
    """One question and its run. Immutable once saved, except by continuing the same turn."""
    model_config = ConfigDict(extra="forbid")
    turn_id: str
    index: int = Field(ge=1)
    question: str
    question_origin: Literal["user", "document_overview_default"]
    focus_citation: TurnFocusCitation | None = None
    operation_ids: list[str] = Field(min_length=1)
    parent_version_id: str
    started_at: datetime
    finished_at: datetime
    status: Literal["completed", "partial"]
    reason: str
    report: str = ""
    partial_notes: str | None = None
    question_parts: list[ResearchQuestionPart] = Field(default_factory=list)
    market_analyses: list[SavedMarketAnalysis] = Field(default_factory=list)
    stopping_note: str | None = None
    claim_ids: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)
    calculation_ids: list[str] = Field(default_factory=list)
    chart_ids: list[str] = Field(default_factory=list)
    table_ids: list[str] = Field(default_factory=list)
    gaps: list[ResearchGap] = Field(default_factory=list)
    usage: TurnUsage
    attempts: list[ResearchAttempt] = Field(default_factory=list)
    search_status: str
    official_fetch_status: str
    read_ranges: list[ReadRange] = Field(default_factory=list)
    # Sanitized run events keep their own per-tool shape; extra keys are allowed here only.
    events: list[dict] = Field(default_factory=list)
    # Deterministic run views (versioned UI structures, see research_reconciliation.ui_view / research_coverage_map.ui_view).
    reconciliation: dict | None = None
    counter_evidence: dict | None = None
    coverage_map: dict | None = None
    method_packs: list[MethodPackRef] = Field(default_factory=list)
    attributions: list[TurnAttribution] = Field(default_factory=list)


class ResearchContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # User-owned research summary; the agent never writes it. Each turn's answer is `turns[i].report`.
    summary: str = Field(default="", max_length=100000)
    sources: list[dict] = Field(default_factory=list)
    # Unverified leads from the latest turn only.
    source_candidates: list[dict] = Field(default_factory=list, max_length=10)
    facts: list[ResearchFact] = Field(default_factory=list)
    calculations: list[ResearchCalculation] = Field(default_factory=list)
    claims: list[ResearchClaim] = Field(default_factory=list)
    charts: list[ResearchChart] = Field(default_factory=list)
    tables: list[ResearchTable] = Field(default_factory=list)
    # Open gaps: the latest turn's gaps.
    gaps: list[ResearchGap] = Field(default_factory=list)
    turns: list[ResearchTurn] = Field(default_factory=list)


class VersionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent_version_id: str
    content: ResearchContent
    review: Literal["draft", "confirmed", "reservation"] = "draft"
    note: str = Field(default="", max_length=10000)


class AttachSourcesInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    parent_version_id: str = Field(min_length=1, max_length=100)
    source_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_sources(self) -> AttachSourcesInput:
        if any(not source_id for source_id in self.source_ids):
            raise ValueError("Source IDs must not be blank.")
        return self


class ResearchVersion(BaseModel):
    version_id: str
    parent_version_id: str | None = None
    created_at: datetime
    content: ResearchContent
    review: Literal["draft", "confirmed", "reservation"]
    note: str = ""
    source_ids: list[str] = Field(default_factory=list)
    created_by: Literal["user", "agent", "unknown"] = "unknown"


class ResearchTopic(BaseModel):
    schema_version: Literal["1"] = "1"
    topic_id: str
    owner_id: str
    request: ResearchInput
    created_at: datetime
    updated_at: datetime
    versions: list[ResearchVersion]


class ResearchFullExport(BaseModel):
    """A permission-checked snapshot of one immutable research version."""
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1"] = "1"
    format: Literal["research_json"] = "research_json"
    topic_id: str
    generated_at: datetime
    request: ResearchInput
    version: ResearchVersion
    source_provenance: list[dict]
    disclosure: Literal["Model interpretations and declared source permissions are not independently verified."] = \
        "Model interpretations and declared source permissions are not independently verified."
    calculation_disclosure: Literal["Calculations use the recorded inputs; their source precision and prior rounding are not established."] = \
        "Calculations use the recorded inputs; their source precision and prior rounding are not established."


class ResearchMetadataExport(BaseModel):
    """Content-free export when the full research cannot be shared."""
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1"] = "1"
    format: Literal["metadata_only"] = "metadata_only"
    topic_id: str
    version_id: str
    parent_version_id: str | None
    version_created_at: datetime
    generated_at: datetime
    as_of: datetime | None
    review: Literal["draft", "confirmed", "reservation"]
    created_by: Literal["user", "agent", "unknown"]
    tracked_source_count: int = Field(ge=0)
    fact_count: int = Field(ge=0)
    claim_count: int = Field(ge=0)
    calculation_count: int = Field(ge=0)
    gap_count: int = Field(ge=0)
    turn_count: int = Field(ge=0)
    disclosure: Literal["No research text or source content is included. Full export requires source permissions."] = \
        "No research text or source content is included. Full export requires source permissions."


class TopicNotFound(Exception):
    pass


class TopicVersionConflict(Exception):
    pass


class ResearchTopics:
    def __init__(self, repository: JsonDocumentRepository[ResearchTopic]):
        self.repository = repository

    def create(self, owner: str, request: ResearchInput) -> ResearchTopic:
        now = datetime.now(timezone.utc)
        topic = ResearchTopic(topic_id=f"topic_{uuid4().hex}", owner_id=owner,
            request=request, created_at=now, updated_at=now,
            versions=[ResearchVersion(version_id=f"version_{uuid4().hex}",
                created_at=now, content=ResearchContent(), review="draft",
                created_by="user",
                source_ids=list(dict.fromkeys(request.source_ids)))])
        self.repository.put(topic.topic_id, topic)
        return topic.model_copy(deep=True)

    def get(self, owner: str, topic_id: str) -> ResearchTopic:
        topic = self.repository.get(topic_id)
        if topic is None or topic.owner_id != owner:
            raise TopicNotFound()
        return topic.model_copy(deep=True)

    def list(self, owner: str) -> list[ResearchTopic]:
        return [topic.model_copy(deep=True) for _, topic in owner_items(self.repository, owner)
                if topic.owner_id == owner]

    def delete_owner(self, owner: str) -> None:
        for topic in self.list(owner):
            self.repository.delete(topic.topic_id)

    def add_version(self, owner: str, topic_id: str, request: VersionInput, *, source_ids: list[str] | None = None,
                    created_by: Literal["user", "agent"] = "user") -> ResearchTopic:
        topic = self.get(owner, topic_id)
        if topic.versions[-1].version_id != request.parent_version_id:
            raise TopicVersionConflict()
        now = datetime.now(timezone.utc)
        updated = topic.model_copy(deep=True)
        combined_source_ids = list(dict.fromkeys([*topic.versions[-1].source_ids, *(source_ids or [])]))
        content = deepcopy(request.content)
        # Never trust a client-supplied result status. Recompute against the
        # saved formula and input snapshot for every newly created version.
        for calculation in content.calculations:
            calculation.result_status = calculation_result_status(calculation.model_dump(mode="json"))
        updated.versions.append(ResearchVersion(version_id=f"version_{uuid4().hex}",
            parent_version_id=request.parent_version_id, created_at=now,
            content=content, review=request.review, note=request.note,
            source_ids=combined_source_ids, created_by=created_by))
        updated.updated_at = now
        try:
            self.repository.put(topic_id, updated, expected_version=len(topic.versions))
        except StorageConflictError as error:
            raise TopicVersionConflict() from error
        return updated.model_copy(deep=True)
