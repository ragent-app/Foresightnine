"""Contracts for the bounded first-research operation."""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .research_topics import ResearchContent


class FocusCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_index: int = Field(ge=0)
    citation_index: int = Field(ge=0)


class ResearchRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent_version_id: str = Field(min_length=1, max_length=100)
    source_ids: list[str] = Field(default_factory=list, max_length=100)
    max_model_calls: int = Field(default=1, ge=0, le=2)
    support_review: bool = False
    require_counterarguments: bool = True
    focus_citation: FocusCitation | None = None
    max_search_calls: int = Field(default=1, ge=0, le=1)
    max_elapsed_seconds: int = Field(default=60, ge=1, le=180)
    max_total_tokens: int = Field(default=10000, ge=0, le=20000)
    max_cost_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)


class ResearchRunResult(BaseModel):
    operation_id: str
    topic_id: str
    parent_version_id: str
    status: Literal["completed", "partial"]
    content: ResearchContent
