"""Contracts for the bounded first-research operation."""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .research_topics import ResearchContent


from .research_quality import CLAIM_EVIDENCE_WARNINGS


def has_usable_counterpoint(claims: list[dict], excluded_indexes: set[int] | None = None) -> bool:
    """Identify a cited candidate with no known evidence warnings, not proof of relevance."""
    excluded = excluded_indexes or set()
    return any(index not in excluded and isinstance(item, dict) and item.get('role') == 'counterargument' and item.get('citations') and
               item.get('support_status') != 'incomplete' and
               not any(item.get(warning) for warning in CLAIM_EVIDENCE_WARNINGS) for index, item in enumerate(claims))


class FocusCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_index: int = Field(ge=0)
    citation_index: int = Field(ge=0)


class ResearchRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question_override: str | None = Field(default=None, min_length=1, max_length=10000)
    parent_version_id: str = Field(min_length=1, max_length=100)
    source_ids: list[str] = Field(default_factory=list, max_length=100)
    # None preserves the historical behavior; [] explicitly selects no source.
    selected_source_ids: list[str] | None = Field(default=None, max_length=100)
    max_model_calls: int = Field(default=1, ge=0, le=10)
    max_input_tokens_per_call: int | None = Field(default=None, ge=1, le=12000, strict=True)
    support_review: bool = False
    support_review_mode: Literal["batch", "isolated"] = "batch"
    counterpoint_followup: bool = Field(default=False, strict=True)
    gap_followup: bool = Field(default=False, strict=True)
    plan_research: bool = Field(default=False, strict=True)
    require_counterarguments: bool = True
    include_scope_in_search: bool = False
    auto_fetch_official: bool = Field(default=False, strict=True)
    focus_citation: FocusCitation | None = None
    max_search_calls: int = Field(default=1, ge=0, le=1)
    max_elapsed_seconds: int = Field(default=60, ge=1, le=180)
    max_total_tokens: int = Field(default=10000, ge=0, le=20000)
    max_cost_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("selected_source_ids")
    @classmethod
    def selected_sources_unique_and_nonblank(cls, values: list[str] | None) -> list[str] | None:
        if values is not None and (any(not value for value in values) or len(values) != len(set(values))):
            raise ValueError("Selected source IDs must be nonblank and unique.")
        return values


def discovery_query(question: str, scope: str, include_scope: bool) -> str:
    """Bound the user-authorized text sent to source discovery."""
    question = question.strip()
    scope = scope.strip()
    if not include_scope or not scope:
        return question[:70]
    question = question[:45]
    return question + " " + scope[:69 - len(question)]


class ResearchRunResult(BaseModel):
    operation_id: str
    topic_id: str
    parent_version_id: str
    status: Literal["completed", "partial"]
    content: ResearchContent
