"""Provider-neutral research turn and opaque continuation contract."""
from dataclasses import dataclass
from typing import Any


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


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None


@dataclass(frozen=True)
class ModelResponse:
    message: dict[str, Any]
    tool_calls: tuple[ModelToolCall, ...]
    usage: ModelUsage | None
    finish_reason: str | None
    stop_class: str = 'other'
