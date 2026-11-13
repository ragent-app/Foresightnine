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

from .storage import JsonDocumentRepository, StorageConflictError
from .research_provenance import derive_evidence_links, derive_question_links, normalize_claim_links, normalize_research_questions, normalize_user_fact_links
from .research_quality import research_gaps
from .research_stage_summary import summarize_research_stages
from .research_calculations import calculation_result_status


class ResearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(default="", max_length=10000)
    source_ids: list[str] = Field(default_factory=list, max_length=100)
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


class ResearchContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(default="", max_length=100000)
    # The structured entries preserve separate categories; further processing
    # and source authorization are implemented at the tool boundary.
    sources: list[dict] = Field(default_factory=list, max_length=200)
    source_candidates: list[dict] = Field(default_factory=list, max_length=10)
    facts: list[dict] = Field(default_factory=list, max_length=2000)
    evidence_differences: list[dict] = Field(default_factory=list, max_length=50)
    calculations: list[dict] = Field(default_factory=list, max_length=2000)
    calculation_differences: list[dict] = Field(default_factory=list, max_length=50)
    amount_differences: list[dict] = Field(default_factory=list, max_length=50)
    claims: list[dict] = Field(default_factory=list, max_length=2000)
    limitations: list[str] = Field(default_factory=list, max_length=500)
    run: dict = Field(default_factory=dict)


class VersionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent_version_id: str
    content: ResearchContent
    review: Literal["draft", "confirmed", "reservation"] = "draft"
    note: str = Field(default="", max_length=10000)


class AttachSourcesInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    parent_version_id: str = Field(min_length=1, max_length=100)
    source_ids: list[str] = Field(min_length=1, max_length=100)

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
    limitation_count: int = Field(ge=0)
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
        return [topic.model_copy(deep=True) for _, topic in self.repository.items()
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
        if len(combined_source_ids) > 100:
            raise ValueError("A research version can include at most 100 sources.")
        content = deepcopy(request.content)
        # Never trust a client-supplied result status. Recompute against the
        # saved formula and input snapshot for every newly created version.
        for calculation in content.calculations:
            if isinstance(calculation, dict):
                calculation['result_status'] = calculation_result_status(calculation)
        normalize_claim_links(content.claims, topic.versions[-1].content.claims, model_generated=created_by == 'agent')
        normalize_user_fact_links(content.claims, content.facts, topic.versions[-1].content.claims,
                                  topic.versions[-1].content.facts, model_generated=created_by == 'agent')
        content.run['research_questions'] = normalize_research_questions(
            content.run.get('research_questions'), content.claims,
            topic.versions[-1].content.run.get('research_questions'), topic.versions[-1].content.claims,
            model_generated=created_by == 'agent')
        # Compute from the server-accepted snapshot on every version, including
        # user edits and checkpoints; do not trust a client-supplied link map.
        content.run['evidence_links'] = derive_evidence_links(content.claims, content.facts, content.calculations)
        content.run['question_links'] = derive_question_links(content.run['research_questions'], content.claims)
        content.run['research_gaps'] = research_gaps(
            claims=content.claims, calculations=content.calculations, sources=content.sources,
            calculation_differences=content.calculation_differences,
            amount_differences=content.amount_differences, run=content.run)
        content.run['stage_summary'] = summarize_research_stages(content.run)
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
