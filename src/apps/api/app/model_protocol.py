"""Provider-neutral research turn and opaque continuation contract."""
from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
import re
from typing import Any

_JSON_WORD_RE = re.compile(r'\bjson\b', re.IGNORECASE)
_JSON_OBJECT_INPUT_INSTRUCTION = 'Respond with a valid JSON object matching the schema in instructions.'
_UNRELATED_REJECTION_PARAMETERS = frozenset({
    'model', 'reasoning.effort', 'reasoning', 'max_output_tokens',
    'max_tokens', 'max_completion_tokens', 'temperature', 'top_p', 'seed', 'stream',
})


def _content_has_json_word(value: Any) -> bool:
    """Return True when text or structured message content contains the standalone word 'json'."""
    if isinstance(value, str):
        return bool(_JSON_WORD_RE.search(value))
    if isinstance(value, Mapping):
        if isinstance(value.get('text'), str) and _JSON_WORD_RE.search(value['text']):
            return True
        content = value.get('content')
        if content is not None:
            return _content_has_json_word(content)
        return False
    if isinstance(value, (list, tuple)):
        return any(_content_has_json_word(item) for item in value)
    return False


def ensure_json_word_in_messages(
    items: list[dict[str, Any]],
    *,
    roles: tuple[str, ...] = ('user',),
    instruction: str = _JSON_OBJECT_INPUT_INSTRUCTION,
    text_part_type: str = 'text',
) -> list[dict[str, Any]]:
    """Ensure message payloads satisfy API json_object 'json' word requirements without touching system prompts."""
    for item in items:
        if not isinstance(item, Mapping):
            continue
        role = item.get('role')
        if role not in roles:
            continue
        if _content_has_json_word(item):
            return items
    updated = list(items)
    if updated and isinstance(updated[-1], Mapping) and updated[-1].get('role') == 'user':
        last = dict(updated[-1])
        content = last.get('content')
        if isinstance(content, str):
            last['content'] = f'{content}\n\n{instruction}' if content else instruction
            updated[-1] = last
            return updated
        if isinstance(content, list):
            parts = list(content)
            parts.append({'type': text_part_type, 'text': instruction})
            last['content'] = parts
            updated[-1] = last
            return updated
    updated.append({'role': 'user', 'content': instruction})
    return updated


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
    user_followup: str | None = None


@dataclass(frozen=True)
class ModelTurnInput:
    system: str
    user: str
    tools: tuple[ModelToolDefinition, ...]
    history: tuple[ModelTurnExchange, ...]
    tail_note: str | None = None
    user_instructions: tuple[dict[str, Any], ...] = ()
    response_schema: dict[str, Any] | None = None
    response_schema_name: str | None = None
    max_output_tokens: int | None = None
    timeout_seconds: float | None = None
    cancel_event: Any = None
    cache_user_prefix: bool = True


def extract_assistant_text(message: Mapping[str, Any] | None) -> str | None:
    """Extract joined assistant text from string content, content block lists, or Responses output."""
    if not isinstance(message, Mapping):
        return None
    content = message.get('content')
    if isinstance(content, str):
        return content if content else None
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                if block:
                    parts.append(block)
                continue
            if not isinstance(block, Mapping):
                continue
            block_type = block.get('type')
            if block_type in (None, 'text', 'output_text'):
                text = block.get('text')
                if isinstance(text, str) and text:
                    parts.append(text)
                elif isinstance(text, Mapping):
                    nested_val = text.get('value')
                    if isinstance(nested_val, str) and nested_val:
                        parts.append(nested_val)
        return '\n'.join(parts) if parts else None
    output = message.get('output')
    if isinstance(output, (list, tuple)):
        parts = []
        for item in output:
            if isinstance(item, Mapping) and item.get('type') == 'message' and item.get('role') in (None, 'assistant'):
                nested = extract_assistant_text(item)
                if nested:
                    parts.append(nested)
        return '\n'.join(parts) if parts else None
    return None


def format_response_schema_instruction(
    schema: Mapping[str, Any] | None,
    schema_name: str | None = None,
    *,
    has_tools: bool = False,
) -> str | None:
    """Render a deterministic JSON contract instruction when native strict schemas are unavailable."""
    name = schema_name.strip() if isinstance(schema_name, str) and schema_name.strip() else None
    if schema is None and name is None:
        return None
    label = name or 'response'
    prefix = (
        f'When returning your final response (without tool calls), return a single valid JSON object matching contract "{label}"'
        if has_tools
        else f'Return a single valid JSON object matching contract "{label}". Output raw JSON only with no surrounding commentary'
    )
    if schema is None:
        return f'{prefix}, and include no unrequested keys.'
    serialized = canonical_json_dumps(canonicalize_json_value(schema))
    return f'{prefix} and no unrequested keys.\nJSON Schema: {serialized}'


class _CompositeCancelEvent:
    def __init__(self, primary: Any, secondary: Any) -> None:
        self._primary = primary
        self._secondary = secondary

    def is_set(self) -> bool:
        return bool(
            (self._primary is not None and self._primary.is_set())
            or (self._secondary is not None and self._secondary.is_set())
        )


def merge_cancel_events(primary: Any, secondary: Any) -> Any:
    """Combine caller-supplied and turn-level cancel events without duplicating checks."""
    if primary is None:
        return secondary
    if secondary is None or primary is secondary:
        return primary
    return _CompositeCancelEvent(primary, secondary)


def merge_optional_timeout(primary: float | None, secondary: float | None) -> float | None:
    """Select the tighter finite timeout while preserving invalid values for provider validation."""
    candidates = [val for val in (primary, secondary) if val is not None]
    if not candidates:
        return None
    for val in candidates:
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or val <= 0:
            return val
    return min(float(val) for val in candidates)


def merge_optional_max_tokens(primary: int | None, secondary: int | None) -> int | None:
    """Select the tighter max_output_tokens while preserving invalid values for provider validation."""
    candidates = [val for val in (primary, secondary) if val is not None]
    if not candidates:
        return None
    for val in candidates:
        if type(val) is not int or val < 1:
            return val
    return min(candidates)


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
