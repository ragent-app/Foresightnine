"""Tool-boundary persistence validation and sanitized error summaries for research items."""
from __future__ import annotations

import re
from typing import Any, Literal, Mapping, Sequence, overload

from pydantic import BaseModel, ValidationError

from .research_charts import ResearchChart
from .research_topics import ResearchCalculation, ResearchClaim, ResearchFact

PLACEHOLDER_TURN_ID = 'turn-validation'
ItemKind = Literal['calculation', 'fact', 'chart', 'claim']

_MODEL_BY_KIND: dict[ItemKind, type[BaseModel]] = {
    'calculation': ResearchCalculation,
    'fact': ResearchFact,
    'chart': ResearchChart,
    'claim': ResearchClaim,
}

_CONTROL_CHARS_RE = re.compile(r'[\x00-\x1f\x7f]+')


def sanitize_validation_errors(
    error: ValidationError,
    *,
    max_items: int = 5,
) -> list[dict[str, str]]:
    """Return bounded loc + type entries only; never expose input values, ctx, or raw msg."""
    if max_items <= 0:
        return []
    entries: list[dict[str, str]] = []
    for item in error.errors(include_url=False, include_context=False, include_input=False)[:max_items]:
        loc_parts = [
            _CONTROL_CHARS_RE.sub(' ', str(part)).strip()
            for part in item.get('loc', ())
        ]
        loc = '.'.join(loc_parts)[:120]
        err_type = _CONTROL_CHARS_RE.sub(' ', str(item.get('type') or 'validation_error')).strip()[:64]
        entries.append({'loc': loc, 'type': err_type})
    return entries


def format_validation_summary(errors: Sequence[Mapping[str, str]]) -> str:
    """Format sanitized loc/type entries into a bounded string for logs or tool feedback."""
    parts = [
        f"{item.get('loc') or '<root>'}:{item.get('type') or 'validation_error'}"
        for item in errors
    ]
    return ', '.join(parts) if parts else 'validation_error'


class ItemPersistenceValidationError(ValueError):
    """Raised at a tool boundary when an output dict fails its persistence schema."""

    def __init__(
        self,
        item_kind: ItemKind,
        validation_errors: list[dict[str, str]],
        *,
        item_index: int | None = None,
    ) -> None:
        self.item_kind = item_kind
        self.validation_errors = validation_errors
        self.item_index = item_index
        summary = format_validation_summary(validation_errors)
        prefix = f'Generated {item_kind} item' + (f'[{item_index}]' if item_index is not None else '')
        self.sanitized_message = f'{prefix} failed persistence validation ({summary}).'
        super().__init__(self.sanitized_message)


def _prepare_candidate(item: Any, *, turn_id: str | None) -> Any:
    if isinstance(item, BaseModel):
        candidate: dict[str, Any] = item.model_dump(mode='python')
    elif isinstance(item, Mapping):
        candidate = dict(item)
    else:
        return item
    if turn_id is not None:
        candidate['turn_id'] = turn_id
    elif 'turn_id' not in candidate:
        candidate['turn_id'] = PLACEHOLDER_TURN_ID
    return candidate


@overload
def validate_research_item(
    item: Any,
    *,
    item_kind: Literal['calculation'],
    turn_id: str | None = None,
    item_index: int | None = None,
) -> ResearchCalculation: ...


@overload
def validate_research_item(
    item: Any,
    *,
    item_kind: Literal['fact'],
    turn_id: str | None = None,
    item_index: int | None = None,
) -> ResearchFact: ...


@overload
def validate_research_item(
    item: Any,
    *,
    item_kind: Literal['chart'],
    turn_id: str | None = None,
    item_index: int | None = None,
) -> ResearchChart: ...


@overload
def validate_research_item(
    item: Any,
    *,
    item_kind: Literal['claim'],
    turn_id: str | None = None,
    item_index: int | None = None,
) -> ResearchClaim: ...


def validate_research_item(
    item: Any,
    *,
    item_kind: ItemKind,
    turn_id: str | None = None,
    item_index: int | None = None,
) -> BaseModel:
    """Validate one tool-produced dict against its persistence model without mutating ``item``."""
    model = _MODEL_BY_KIND.get(item_kind)
    if model is None:
        raise ValueError(f'Unsupported research item kind: {item_kind!r}')
    candidate = _prepare_candidate(item, turn_id=turn_id)
    try:
        return model.model_validate(candidate)
    except ValidationError as error:
        raise ItemPersistenceValidationError(
            item_kind,
            sanitize_validation_errors(error),
            item_index=item_index,
        ) from error


def validate_calculation(
    item: Any,
    *,
    turn_id: str | None = None,
    item_index: int | None = None,
) -> ResearchCalculation:
    """Validate a calculation dict against ``ResearchCalculation`` without mutating input."""
    return validate_research_item(item, item_kind='calculation', turn_id=turn_id, item_index=item_index)


def validate_fact(
    item: Any,
    *,
    turn_id: str | None = None,
    item_index: int | None = None,
) -> ResearchFact:
    """Validate a fact dict against ``ResearchFact`` without mutating input."""
    return validate_research_item(item, item_kind='fact', turn_id=turn_id, item_index=item_index)


def validate_chart(
    item: Any,
    *,
    turn_id: str | None = None,
    item_index: int | None = None,
) -> ResearchChart:
    """Validate a chart dict against ``ResearchChart`` without mutating input."""
    return validate_research_item(item, item_kind='chart', turn_id=turn_id, item_index=item_index)


def validate_claim(
    item: Any,
    *,
    turn_id: str | None = None,
    item_index: int | None = None,
) -> ResearchClaim:
    """Validate a claim dict against ``ResearchClaim`` without mutating input."""
    return validate_research_item(item, item_kind='claim', turn_id=turn_id, item_index=item_index)
