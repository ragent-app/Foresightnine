"""Provider-neutral research turn and opaque continuation contract."""
from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import Any


def _reject_json_constant(token: str) -> Any:
    raise ValueError(f'Invalid JSON constant: {token}')


def canonicalize_json_value(value: Any) -> Any:
    """Recursively sort mapping keys so JSON serialization is deterministic."""
    if isinstance(value, Mapping):
        return {k: canonicalize_json_value(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, list):
        return [canonicalize_json_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(canonicalize_json_value(item) for item in value)
    return value


def canonical_json_dumps(value: Any) -> str:
    """Serialize a JSON-compatible value with sorted keys and compact separators."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def canonicalize_json_string(value: str) -> str:
    """Normalize a JSON object/array string for deterministic prefix caching, leaving plain text intact."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not (
        (stripped.startswith('{') and stripped.endswith('}'))
        or (stripped.startswith('[') and stripped.endswith(']'))
    ):
        return value
    try:
        parsed = json.loads(stripped, parse_constant=_reject_json_constant)
    except (ValueError, TypeError):
        return value
    if not isinstance(parsed, (dict, list)):
        return value
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True)
class ModelToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ModelToolCall:
    id: str
    name: str
    arguments: dict[str, Any] | None
    raw_arguments: str | None = None


@dataclass(frozen=True)
class ModelToolResult:
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class ModelAssistantContinuation:
    envelope: dict[str, Any]


@dataclass(frozen=True)
class ModelTurnExchange:
    assistant: ModelAssistantContinuation
    results: tuple[ModelToolResult, ...]


@dataclass(frozen=True)
class ModelTurnInput:
    system: str
    user: str
    tools: tuple[ModelToolDefinition, ...]
    history: tuple[ModelTurnExchange, ...]
    tail_note: str | None = None
    user_instructions: tuple[dict[str, Any], ...] = ()


def render_user_instructions(turn: ModelTurnInput) -> list[str]:
    """Preserve user provenance and order independently from recovery notes."""
    return [canonical_json_dumps({'instruction_id': item.get('instruction_id'),
                                  'sequence': item.get('sequence'), 'text': item['text']})
            for item in turn.user_instructions
            if isinstance(item, Mapping) and isinstance(item.get('text'), str)]


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None

    def _total_input_tokens(self) -> int | None:
        inp = self.input_tokens if type(self.input_tokens) is int and self.input_tokens >= 0 else None
        out = self.output_tokens if type(self.output_tokens) is int and self.output_tokens >= 0 else None
        tot = self.total_tokens if type(self.total_tokens) is int and self.total_tokens >= 0 else None
        creation = (
            self.cache_creation_input_tokens
            if type(self.cache_creation_input_tokens) is int and self.cache_creation_input_tokens >= 0
            else 0
        )
        read = (
            self.cache_read_input_tokens
            if type(self.cache_read_input_tokens) is int and self.cache_read_input_tokens >= 0
            else 0
        )
        if tot is not None and out is not None and tot >= out:
            implied_prompt = tot - out
            if implied_prompt == (inp or 0) + creation + read:
                return implied_prompt
            if inp is not None and implied_prompt == inp and inp >= read:
                return inp
            if implied_prompt >= read and (inp is None or implied_prompt >= inp):
                return implied_prompt
        if self.cache_creation_input_tokens is not None or (inp is not None and read > inp):
            return (inp or 0) + creation + read
        if inp is not None:
            return inp
        if read or creation:
            return creation + read
        return None

    @property
    def cache_hit_ratio(self) -> float | None:
        """Fraction of input tokens served from prompt cache in [0.0, 1.0], or None if unavailable."""
        has_read = type(self.cache_read_input_tokens) is int and self.cache_read_input_tokens >= 0
        has_create = type(self.cache_creation_input_tokens) is int and self.cache_creation_input_tokens >= 0
        if not has_read and not has_create:
            return None
        total_input = self._total_input_tokens()
        if total_input is None or total_input <= 0:
            return None
        read = self.cache_read_input_tokens if has_read else 0
        return min(1.0, read / total_input)

    def usage_cache_summary(self) -> dict[str, int | float | None]:
        """Protocol-neutral cache summary without altering dataclass serialization."""
        return {
            'total_input_tokens': self._total_input_tokens(),
            'cache_read_input_tokens': (
                self.cache_read_input_tokens
                if type(self.cache_read_input_tokens) is int and self.cache_read_input_tokens >= 0
                else None
            ),
            'cache_creation_input_tokens': (
                self.cache_creation_input_tokens
                if type(self.cache_creation_input_tokens) is int and self.cache_creation_input_tokens >= 0
                else None
            ),
            'cache_hit_ratio': self.cache_hit_ratio,
        }


@dataclass(frozen=True)
class ModelResponse:
    message: dict[str, Any]
    tool_calls: tuple[ModelToolCall, ...]
    usage: ModelUsage | None
    finish_reason: str | None
    stop_class: str = 'other'
