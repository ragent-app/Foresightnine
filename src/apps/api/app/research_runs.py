"""Contracts for the bounded first-research operation."""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .research_topics import ResearchContent


from .research_quality import CLAIM_EVIDENCE_WARNINGS

DOCUMENT_OVERVIEW_QUESTION = (
    "Summarize the supplied material's main verifiable findings, relevant figures, "
    "limitations and unresolved questions. Cite the material and do not assume an investment decision."
)


def has_usable_counterpoint(claims: list[dict], excluded_indexes: set[int] | None = None) -> bool:
    """Identify a cited candidate with no known evidence warnings, not proof of relevance."""
    excluded = excluded_indexes or set()
    return any(index not in excluded and isinstance(item, dict) and item.get('role') == 'counterargument' and item.get('citations') and
               item.get('support_status') != 'incomplete' and
               not any(item.get(warning) for warning in CLAIM_EVIDENCE_WARNINGS) for index, item in enumerate(claims))


class FocusCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str = Field(min_length=1, max_length=100)
    citation_index: int = Field(ge=0)


class ResearchRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question_override: str | None = Field(default=None, min_length=1, max_length=10000)
    parent_version_id: str = Field(min_length=1, max_length=100)
    source_ids: list[str] = Field(default_factory=list)
    # None selects every available source; [] explicitly selects none.
    selected_source_ids: list[str] | None = Field(default=None)
    max_model_calls: int | None = Field(default=None, ge=0, strict=True)
    max_input_tokens_per_call: int | None = Field(default=None, ge=1, strict=True)
    discover_sources: bool | None = Field(default=None, strict=True)
    auto_fetch_official: bool = Field(default=False, strict=True)
    focus_citation: FocusCitation | None = None
    max_search_calls: int | None = Field(default=None, ge=0, strict=True)
    max_elapsed_seconds: int | None = Field(default=None, ge=0, strict=True)
    max_total_tokens: int | None = Field(default=None, ge=0, strict=True)
    max_cost_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    # Regenerate or edit-and-resend the latest turn: the run starts from the version before that
    # turn and its result is appended as the newest version, so the replaced answer stays in history.
    replace_turn_id: str | None = Field(default=None, min_length=1, max_length=100)
    # Required when review edits made after the replaced turn would not carry over.
    discard_review_edits: bool = Field(default=False, strict=True)
    # Optional override for default-entry US earnings squad auto-routing (None = automatic).
    allow_auto_squad: bool | None = Field(default=None, strict=True)

    @field_validator("selected_source_ids")
    @classmethod
    def selected_sources_unique_and_nonblank(cls, values: list[str] | None) -> list[str] | None:
        if values is not None and (any(not value for value in values) or len(values) != len(set(values))):
            raise ValueError("Selected source IDs must be nonblank and unique.")
        return values


def resolve_automatic_research_request(request: ResearchRunRequest, *, search_available: bool,
                                       historical_prices_available: bool = False) -> ResearchRunRequest:
    """Resolve omitted controls while retaining every explicit caller choice.

    Discovery and retrieval use only the existing configured project-search
    provider and official-source allowlist. Explicit empty selection never
    silently enables additional sources. No credentials or providers are made.
    """
    supplied = request.model_fields_set
    updates: dict[str, object] = {}
    def default(name: str, value: object) -> None:
        if name not in supplied:
            updates[name] = value

    explicit_empty_search = 'max_search_calls' in supplied and request.max_search_calls is not None and request.max_search_calls > 0
    searches = request.max_search_calls
    default('discover_sources', searches != 0 and (request.selected_source_ids != [] or explicit_empty_search))
    discovery_enabled = bool(updates.get('discover_sources', request.discover_sources))
    allow_discovery = search_available and discovery_enabled and searches != 0
    allow_historical_prices = historical_prices_available and discovery_enabled and searches != 0
    default('auto_fetch_official', (allow_discovery or allow_historical_prices) and request.selected_source_ids != [])
    return ResearchRunRequest.model_validate({**request.model_dump(), **updates})


def discovery_query(question: str, scope: str, include_scope: bool) -> str:
    """Bound the user-authorized text sent to source discovery."""
    question = question.strip()
    scope = scope.strip()
    return f'{question} {scope}' if include_scope and scope else question


class ResearchRunResult(BaseModel):
    operation_id: str
    topic_id: str
    parent_version_id: str
    status: Literal["completed", "partial"]
    content: ResearchContent
