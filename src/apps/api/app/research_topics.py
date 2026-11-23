"""Versioned, account-owned research documents for the new workspace.

This store is independent of the legacy thesis/challenge documents.  One
aggregate contains its complete version history so an optimistic write is
atomic even when PostgreSQL and multiple API processes are used.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
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
    # Set when this turn regenerated (or edited and resent) an earlier answer to the same position;
    # the replaced turn remains in the version that preceded this one.
    replaces_turn_id: str | None = None
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


class VersionRestoreInput(BaseModel):
    """Make an earlier version current again by appending a server-made copy of it."""
    model_config = ConfigDict(extra="forbid")
    parent_version_id: str = Field(min_length=1, max_length=100)


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


class TopicOrigin(BaseModel):
    """Where a forked research came from, so it can be traced back."""
    model_config = ConfigDict(extra="forbid")
    topic_id: str
    version_id: str
    turn_id: str | None = None
    turn_index: int | None = None


class ResearchTopic(BaseModel):
    schema_version: Literal["1"] = "1"
    topic_id: str
    owner_id: str
    request: ResearchInput
    created_at: datetime
    updated_at: datetime
    versions: list[ResearchVersion]
    # Presentation metadata, merged from TopicMeta when a topic is read.  It is
    # never written into the topic document: that document's storage revision
    # tracks its version count for optimistic appends.
    title: str | None = None
    pinned_at: datetime | None = None
    forked_from: TopicOrigin | None = None


class TopicMeta(BaseModel):
    """User-controlled list metadata kept beside the versioned topic document."""
    model_config = ConfigDict(extra="forbid")
    topic_id: str
    owner_id: str
    title: str | None = None
    pinned_at: datetime | None = None
    deleted_at: datetime | None = None
    forked_from: TopicOrigin | None = None


class TopicUpdate(BaseModel):
    """Rename (a blank or null title restores the question as the name) and pin."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str | None = Field(default=None, max_length=200)
    pinned: bool | None = None

    @model_validator(mode="after")
    def validate_update(self) -> TopicUpdate:
        if not self.model_fields_set & {"title", "pinned"}:
            raise ValueError("Provide a title or a pinned state.")
        return self


class TopicForkInput(BaseModel):
    """Start a new research from one version, up to and including one of its turns."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    version_id: str = Field(min_length=1, max_length=100)
    turn_id: str | None = Field(default=None, min_length=1, max_length=100)
    title: str | None = Field(default=None, max_length=200)


def truncate_content(content: ResearchContent, upto_turn_id: str | None) -> ResearchContent:
    """Content as it stood after ``upto_turn_id``: that turn and earlier ones with their records.

    Up to the last turn this is a full copy. Up to an earlier turn, user-authored records and the
    latest-turn-only lists (leads, open gaps, the user's summary) are left out, since they may
    concern later turns; the open gaps become that turn's own.
    """
    turn_ids = [turn.turn_id for turn in content.turns]
    if upto_turn_id is None or (turn_ids and upto_turn_id == turn_ids[-1]):
        return content.model_copy(deep=True)
    if upto_turn_id not in turn_ids:
        raise ValueError("Turn not found in this version.")
    kept_turns = content.turns[:turn_ids.index(upto_turn_id) + 1]
    kept = {turn.turn_id for turn in kept_turns}
    belongs = lambda item: getattr(item, "turn_id", None) in kept  # noqa: E731
    return ResearchContent(
        sources=deepcopy(content.sources),
        facts=[item.model_copy(deep=True) for item in content.facts if belongs(item)],
        calculations=[item.model_copy(deep=True) for item in content.calculations if belongs(item)],
        claims=[item.model_copy(deep=True) for item in content.claims if belongs(item)],
        charts=[item.model_copy(deep=True) for item in content.charts if belongs(item)],
        tables=[item.model_copy(deep=True) for item in content.tables if belongs(item)],
        gaps=[gap.model_copy(deep=True) for gap in kept_turns[-1].gaps],
        turns=[turn.model_copy(deep=True) for turn in kept_turns],
    )


class TopicDeletion(BaseModel):
    topic_id: str
    deleted_at: datetime
    purge_after: datetime


DELETED_TOPIC_RETENTION = timedelta(days=30)


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
    def __init__(self, repository: JsonDocumentRepository[ResearchTopic],
                 meta_repository: JsonDocumentRepository[TopicMeta] | None = None):
        self.repository = repository
        self.meta_repository = meta_repository

    def _meta(self, owner: str, topic_id: str) -> TopicMeta | None:
        if self.meta_repository is None:
            return None
        meta = self.meta_repository.get(topic_id)
        return meta if meta is not None and meta.owner_id == owner else None

    def _with_meta(self, topic: ResearchTopic, meta: TopicMeta | None) -> ResearchTopic:
        if meta is None:
            return topic
        return topic.model_copy(update={"title": meta.title, "pinned_at": meta.pinned_at, "forked_from": meta.forked_from})

    def _put_meta(self, meta: TopicMeta) -> None:
        if self.meta_repository is None:
            raise RuntimeError("Topic metadata storage is not configured.")
        self.meta_repository.put(meta.topic_id, meta)

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

    def get(self, owner: str, topic_id: str, *, include_deleted: bool = False) -> ResearchTopic:
        """A deleted topic reads as missing everywhere (runs, versions, sources) until restored."""
        topic = self.repository.get(topic_id)
        if topic is None or topic.owner_id != owner:
            raise TopicNotFound()
        meta = self._meta(owner, topic_id)
        if meta is not None and meta.deleted_at is not None and not include_deleted:
            raise TopicNotFound()
        return self._with_meta(topic.model_copy(deep=True), meta)

    def list(self, owner: str, *, now: datetime | None = None) -> list[ResearchTopic]:
        """Visible topics; deleted ones past their retention are purged on the way."""
        current = now or datetime.now(timezone.utc)
        result: list[ResearchTopic] = []
        for _, topic in list(owner_items(self.repository, owner)):
            if topic.owner_id != owner:
                continue
            meta = self._meta(owner, topic.topic_id)
            if meta is not None and meta.deleted_at is not None:
                if current - meta.deleted_at >= DELETED_TOPIC_RETENTION:
                    self.purge(owner, topic.topic_id)
                continue
            result.append(self._with_meta(topic.model_copy(deep=True), meta))
        return result

    def update(self, owner: str, topic_id: str, request: TopicUpdate, *, now: datetime | None = None) -> ResearchTopic:
        topic = self.get(owner, topic_id)
        meta = self._meta(owner, topic_id) or TopicMeta(topic_id=topic_id, owner_id=owner)
        changes: dict[str, object] = {}
        if "title" in request.model_fields_set:
            changes["title"] = request.title or None
        if "pinned" in request.model_fields_set:
            changes["pinned_at"] = (meta.pinned_at or now or datetime.now(timezone.utc)) if request.pinned else None
        meta = meta.model_copy(update=changes)
        self._put_meta(meta)
        return self._with_meta(topic, meta)

    def soft_delete(self, owner: str, topic_id: str, *, now: datetime | None = None) -> TopicDeletion:
        self.get(owner, topic_id)
        deleted_at = now or datetime.now(timezone.utc)
        meta = self._meta(owner, topic_id) or TopicMeta(topic_id=topic_id, owner_id=owner)
        self._put_meta(meta.model_copy(update={"deleted_at": deleted_at}))
        return TopicDeletion(topic_id=topic_id, deleted_at=deleted_at, purge_after=deleted_at + DELETED_TOPIC_RETENTION)

    def restore(self, owner: str, topic_id: str) -> ResearchTopic:
        self.get(owner, topic_id, include_deleted=True)
        meta = self._meta(owner, topic_id)
        if meta is not None and meta.deleted_at is not None:
            self._put_meta(meta.model_copy(update={"deleted_at": None}))
        return self.get(owner, topic_id)

    def purge(self, owner: str, topic_id: str) -> None:
        topic = self.repository.get(topic_id)
        if topic is None or topic.owner_id != owner:
            return
        self.repository.delete(topic_id)
        if self.meta_repository is not None:
            self.meta_repository.delete(topic_id)

    def fork(self, owner: str, topic_id: str, request: TopicForkInput, *, now: datetime | None = None) -> ResearchTopic:
        """A new topic holding the chosen content, one version per kept turn.

        Each copied turn's ``parent_version_id`` points at the new topic's version before it, so the
        fork's latest answer can itself be regenerated. Records are copied by the server, so their
        verification data survives; sources are shared (same account), not duplicated.
        """
        source_topic = self.get(owner, topic_id)
        version = next((item for item in source_topic.versions if item.version_id == request.version_id), None)
        if version is None:
            raise TopicNotFound()
        content = truncate_content(version.content, request.turn_id)
        created = now or datetime.now(timezone.utc)
        new_id = f"topic_{uuid4().hex}"
        source_ids = list(dict.fromkeys([*source_topic.request.source_ids, *version.source_ids]))
        # ids[k] holds turns[0..k-1]; turn j was asked on the version holding the turns before it, ids[j].
        ids = [f"version_{uuid4().hex}" for _ in range(len(content.turns) + 1)]
        versions = [ResearchVersion(version_id=ids[0], created_at=created, content=ResearchContent(), review="draft",
                                    created_by="user", source_ids=list(dict.fromkeys(source_topic.request.source_ids)))]
        name = source_topic.title or source_topic.request.question or "研究"
        for k, turn in enumerate(content.turns, start=1):
            step = content if k == len(content.turns) else truncate_content(content, turn.turn_id)
            step = step.model_copy(deep=True)
            for j, copied in enumerate(step.turns):
                copied.parent_version_id = ids[j]
            versions.append(ResearchVersion(
                version_id=ids[k], parent_version_id=ids[k - 1], created_at=created, content=step, review="draft",
                created_by="agent", source_ids=source_ids,
                note=f"分叉自《{name[:80]}》第 {turn.index} 轮" if k == len(content.turns) else ""))
        topic = ResearchTopic(topic_id=new_id, owner_id=owner, request=source_topic.request.model_copy(deep=True),
                              created_at=created, updated_at=created, versions=versions[:1])
        # One write per version: the document's storage revision must equal its version count,
        # which later optimistic appends (add_version) rely on.
        self.repository.put(new_id, topic)
        for count in range(2, len(versions) + 1):
            topic = topic.model_copy(update={"versions": versions[:count]})
            self.repository.put(new_id, topic, expected_version=count - 1)
        origin = TopicOrigin(topic_id=topic_id, version_id=version.version_id,
                             turn_id=content.turns[-1].turn_id if content.turns else None,
                             turn_index=content.turns[-1].index if content.turns else None)
        title = request.title or f"{name[:180]}（分支）"
        self._put_meta(TopicMeta(topic_id=new_id, owner_id=owner, title=title, forked_from=origin))
        return self.get(owner, new_id)

    def delete_owner(self, owner: str) -> None:
        for _, topic in list(owner_items(self.repository, owner)):
            if topic.owner_id == owner:
                self.purge(owner, topic.topic_id)

    def add_version(self, owner: str, topic_id: str, request: VersionInput, *, source_ids: list[str] | None = None,
                    created_by: Literal["user", "agent"] = "user") -> ResearchTopic:
        self.get(owner, topic_id)  # ownership and deletion check
        stored = self.repository.get(topic_id)
        assert stored is not None
        # Append to the stored document itself: merged presentation metadata must not be persisted.
        topic = stored.model_copy(deep=True, update={"title": None, "pinned_at": None, "forked_from": None})
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
        return self._with_meta(updated.model_copy(deep=True), self._meta(owner, topic_id))
