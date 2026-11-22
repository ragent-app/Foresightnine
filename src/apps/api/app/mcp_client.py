"""Generic protocol-first Streamable HTTP MCP client (JSON-RPC 2.0).

Implements vendor-agnostic MCP 2025-03-26 session lifecycle, Streamable HTTP
``application/json`` and ``text/event-stream`` parsing, strict public HTTPS
SSRF validation, thread-safe session/tool-catalog caching with canonicalized
schemas, generation-checked single reconnect, and untrusted result wrapping.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
import math
import re
import threading
from time import monotonic, sleep
from types import MappingProxyType
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from .model_protocol import canonical_json_dumps, canonicalize_json_value
from .openai_compatible import (
    HttpResponse,
    OpenAICompatibleTransport,
    _SseLineParser,
    _TransportFailure,
    _TransportTimeout,
    _default_transport,
)
from .phase3 import ProviderFailure
from .research_fetch import validate_public_https_url


_MCP_PROTOCOL_VERSION = "2025-03-26"
_DEFAULT_CLIENT_NAME = "foresightnine"
_DEFAULT_CLIENT_VERSION = "1.0"
_DEFAULT_TIMEOUT_SECONDS = 15.0
_MAX_TIMEOUT_SECONDS = 120.0
_DEFAULT_MAX_ATTEMPTS = 2
_MAX_ATTEMPTS_LIMIT = 5
_DEFAULT_MAX_RESPONSE_BYTES = 256 * 1024
_MAX_RESPONSE_BYTES_LIMIT = 5_000_000
_DEFAULT_MAX_CONTENT_BLOCKS = 64
_MAX_TOOLS = 512
_MAX_TOOL_PAGES = 16
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_SESSION_REINIT_STATUS_CODES = frozenset({401, 404, 410})
_SESSION_VALUE = re.compile(r"^[\x21-\x7e]{1,256}$")
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,128}$")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_MCP_ERROR_PREFIX = re.compile(r"^\s*MCP error\s*(-?\d+)?\s*:?\s*(.*)$", re.I | re.S)
_SESSION_EXPIRED_WORDS = re.compile(
    r"session.{0,24}(?:expired|invalid|unknown|not found|closed|missing)"
    r"|(?:expired|invalid|unknown|missing).{0,24}session|mcp-session-id",
    re.I,
)
_RATE_LIMIT_WORDS = re.compile(r"too many requests|rate limit|频率|速率|请求过多|请求过于频繁", re.I)
_ALLOWED_METADATA_KEYS = frozenset({
    "code",
    "kind",
    "retryable",
    "http_status",
    "rpc_code",
    "provider_code",
    "session_expired",
    "protocol_error",
    "provider_response_received",
    "is_error",
    "error_kind",
    "content_block_count",
})


def _sanitize_error_metadata(
    raw: Mapping[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Return strictly bounded, content-free error metadata without raw provider text."""
    merged: dict[str, Any] = {}
    if isinstance(raw, Mapping):
        merged.update(raw)
    merged.update({k: v for k, v in overrides.items() if v is not None})
    safe: dict[str, Any] = {}
    for key in _ALLOWED_METADATA_KEYS:
        if key not in merged:
            continue
        value = merged[key]
        if key in ("retryable", "session_expired", "protocol_error", "provider_response_received", "is_error"):
            if isinstance(value, bool):
                safe[key] = value
        elif key in ("http_status", "rpc_code", "content_block_count"):
            if type(value) is int and -1_000_000 <= value <= 1_000_000:
                safe[key] = value
        elif key in ("code", "kind", "error_kind", "provider_code"):
            if isinstance(value, str) and _SAFE_CODE.fullmatch(value):
                safe[key] = value
    return safe


class McpClientError(ProviderFailure):
    """Sanitized, content-free failure raised by :class:`StreamableHttpMcpClient`."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        http_status: int | None = None,
        rpc_code: int | None = None,
        session_expired: bool = False,
        protocol_error: bool = False,
        provider_response_received: bool = False,
        error_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(code, message, retryable=retryable)
        self.http_status = http_status
        self.rpc_code = rpc_code
        self.session_expired = session_expired
        self.protocol_error = protocol_error
        self.provider_response_received = provider_response_received
        self.error_metadata: Mapping[str, Any] = MappingProxyType(
            _sanitize_error_metadata(
                error_metadata,
                code=code,
                retryable=retryable,
                http_status=http_status,
                rpc_code=rpc_code,
                session_expired=session_expired,
                protocol_error=protocol_error,
                provider_response_received=provider_response_received,
            )
        )


def _validate_endpoint_url(endpoint: str) -> str:
    if not isinstance(endpoint, str):
        raise McpClientError(
            "mcp_url_rejected",
            "MCP server endpoint must be a public HTTPS URL.",
            retryable=False,
            error_metadata={"kind": "url_rejected"},
        )
    normalized = endpoint.strip()
    if not normalized or _CONTROL_CHAR_RE.search(normalized):
        raise McpClientError(
            "mcp_url_rejected",
            "MCP server endpoint must be a public HTTPS URL.",
            retryable=False,
            error_metadata={"kind": "url_rejected"},
        )
    try:
        validate_public_https_url(normalized, allow_fragment=False, check_sec_path=False)
    except ValueError as error:
        raise McpClientError(
            "mcp_url_rejected",
            "MCP server endpoint must be a valid public HTTPS URL.",
            retryable=False,
            error_metadata={"kind": "url_rejected"},
        ) from error
    return normalized


def _validate_headers(headers: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(headers, Mapping):
        raise McpClientError(
            "mcp_configuration_invalid",
            "MCP custom headers must be a mapping.",
            retryable=False,
            error_metadata={"kind": "configuration_invalid"},
        )
    cleaned: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise McpClientError(
                "mcp_configuration_invalid",
                "MCP header names and values must be strings.",
                retryable=False,
                error_metadata={"kind": "configuration_invalid"},
            )
        name = key.strip()
        if not _HEADER_NAME_RE.fullmatch(name) or _CONTROL_CHAR_RE.search(value):
            raise McpClientError(
                "mcp_configuration_invalid",
                "MCP custom header contains invalid characters.",
                retryable=False,
                error_metadata={"kind": "configuration_invalid"},
            )
        cleaned[name] = value
    return MappingProxyType(cleaned)


@dataclass(frozen=True)
class McpServerConfig:
    """Validated configuration for a remote Streamable HTTP MCP server."""

    endpoint: str
    auth_token: str | None = field(default=None, repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    protocol_version: str = _MCP_PROTOCOL_VERSION
    client_name: str = _DEFAULT_CLIENT_NAME
    client_version: str = _DEFAULT_CLIENT_VERSION
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES
    max_content_blocks: int = _DEFAULT_MAX_CONTENT_BLOCKS
    fixed_operation_rpc_ids: bool = False

    def __post_init__(self) -> None:
        normalized_endpoint = _validate_endpoint_url(self.endpoint)
        object.__setattr__(self, "endpoint", normalized_endpoint)

        if self.auth_token is not None:
            if not isinstance(self.auth_token, str):
                raise McpClientError(
                    "mcp_configuration_invalid",
                    "MCP auth token must be a string.",
                    retryable=False,
                    error_metadata={"kind": "configuration_invalid"},
                )
            token = self.auth_token.strip()
            if token and _CONTROL_CHAR_RE.search(token):
                raise McpClientError(
                    "mcp_configuration_invalid",
                    "MCP auth token contains invalid characters.",
                    retryable=False,
                    error_metadata={"kind": "configuration_invalid"},
                )
            object.__setattr__(self, "auth_token", token or None)

        object.__setattr__(self, "headers", _validate_headers(self.headers))

        for label, value, max_len in (
            ("protocol_version", self.protocol_version, 64),
            ("client_name", self.client_name, 128),
            ("client_version", self.client_version, 64),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > max_len or _CONTROL_CHAR_RE.search(value):
                raise McpClientError(
                    "mcp_configuration_invalid",
                    f"MCP {label} configuration is invalid.",
                    retryable=False,
                    error_metadata={"kind": "configuration_invalid"},
                )

        if (
            type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < float(self.timeout_seconds) <= _MAX_TIMEOUT_SECONDS
        ):
            raise McpClientError(
                "mcp_configuration_invalid",
                "MCP timeout must be between 0 and 120 seconds.",
                retryable=False,
                error_metadata={"kind": "configuration_invalid"},
            )
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= _MAX_ATTEMPTS_LIMIT:
            raise McpClientError(
                "mcp_configuration_invalid",
                "MCP max_attempts must be between 1 and 5.",
                retryable=False,
                error_metadata={"kind": "configuration_invalid"},
            )
        if type(self.max_response_bytes) is not int or not 1 <= self.max_response_bytes <= _MAX_RESPONSE_BYTES_LIMIT:
            raise McpClientError(
                "mcp_configuration_invalid",
                "MCP max_response_bytes is outside the accepted range.",
                retryable=False,
                error_metadata={"kind": "configuration_invalid"},
            )
        if type(self.max_content_blocks) is not int or not 1 <= self.max_content_blocks <= 256:
            raise McpClientError(
                "mcp_configuration_invalid",
                "MCP max_content_blocks is outside the accepted range.",
                retryable=False,
                error_metadata={"kind": "configuration_invalid"},
            )


@dataclass(frozen=True)
class McpToolSpec:
    """Cached MCP tool catalog entry with canonically ordered ``input_schema``."""

    name: str
    description: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    annotations: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class McpCallResult:
    """Untrusted MCP ``tools/call`` output paired with content-free error metadata."""

    tool_name: str
    content: tuple[Mapping[str, Any], ...]
    structured_content: Any | None = None
    is_error: bool = False
    untrusted_data: bool = True
    error_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.untrusted_data is not True:
            raise ValueError("MCP tool results must always be marked untrusted_data=True.")


def _response_header(response: HttpResponse, name: str) -> str | None:
    target = name.lower()
    for key, value in response.headers.items():
        if key.lower() == target:
            return value
    return None


def _is_session_expired_text(text: str | None) -> bool:
    return bool(text and _SESSION_EXPIRED_WORDS.search(text))


def _tool_error_metadata(content: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Derive bounded, content-free error metadata from MCP tool error blocks."""
    rpc_code: int | None = None
    provider_code: str | None = None
    session_expired = False
    rate_limited = False
    for block in content:
        if block.get("type") != "text" or not isinstance(block.get("text"), str):
            continue
        text = block["text"].strip()
        if _is_session_expired_text(text):
            session_expired = True
        if _RATE_LIMIT_WORDS.search(text):
            rate_limited = True
        if rpc_code is not None or provider_code is not None:
            continue
        body = text
        prefixed = _MCP_ERROR_PREFIX.match(text)
        if prefixed is not None:
            if prefixed.group(1):
                try:
                    rpc_code = int(prefixed.group(1))
                except ValueError:
                    rpc_code = None
            body = prefixed.group(2).strip()
        start = body.find("{")
        if start != -1:
            try:
                parsed = json.loads(body[start:])
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                raw_code = parsed["error"].get("code")
                if isinstance(raw_code, int) and not isinstance(raw_code, bool):
                    provider_code = str(raw_code)
                elif isinstance(raw_code, str) and _SAFE_CODE.fullmatch(raw_code.strip()):
                    provider_code = raw_code.strip()
    if rpc_code in (429, -429):
        rate_limited = True
    error_kind = "session_expired" if session_expired else ("rate_limited" if rate_limited else "tool_error")
    return _sanitize_error_metadata(
        is_error=True,
        error_kind=error_kind,
        session_expired=session_expired,
        rpc_code=rpc_code,
        provider_code=provider_code,
        content_block_count=len(content),
    )


def _parse_sse_messages(body: str) -> list[Any]:
    parser = _SseLineParser()
    frames = parser.feed_text(body)
    frames.extend(parser.finish())
    messages: list[Any] = []
    for kind, event_name, data in frames:
        if kind != "frame":
            continue
        if event_name not in ("", "message"):
            continue
        payload = data.strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as error:
            raise McpClientError(
                "mcp_protocol_error",
                "MCP server returned invalid structured SSE data.",
                retryable=False,
                protocol_error=True,
                provider_response_received=True,
                error_metadata={"kind": "invalid_sse_json"},
            ) from error
        if isinstance(parsed, list):
            messages.extend(parsed)
        else:
            messages.append(parsed)
    return messages


def _looks_like_sse(body: str) -> bool:
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return stripped.startswith(("data:", "event:", "id:", "retry:", ":"))
    return False


class StreamableHttpMcpClient:
    """Thread-safe Streamable HTTP MCP client with session and schema caching."""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        transport: OpenAICompatibleTransport | None = None,
        sleeper: Callable[[float], None] | None = None,
        error_classifier: Callable[[str, int | None], ProviderFailure | None] | None = None,
    ) -> None:
        if not isinstance(config, McpServerConfig):
            raise McpClientError(
                "mcp_configuration_invalid",
                "MCP server configuration must be an McpServerConfig instance.",
                retryable=False,
                error_metadata={"kind": "configuration_invalid"},
            )
        # Re-validate endpoint defensively even if config attributes were bypassed.
        _validate_endpoint_url(config.endpoint)
        self._config = config
        self._uses_default_transport = transport is None
        self._transport: OpenAICompatibleTransport = transport or _default_transport
        self._sleeper = sleeper or sleep
        self._error_classifier = error_classifier

        self._lock = threading.Lock()
        self._rpc_id_counter = 0
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        self._tools_cache: tuple[McpToolSpec, ...] | None = None
        self._tools_by_name: dict[str, McpToolSpec] | None = None
        self._session_generation = 0
        self._session_cache_hits = 0
        self._session_cache_misses = 0

    def __repr__(self) -> str:
        parts = urlsplit(self._config.endpoint)
        safe_endpoint = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        has_auth = bool(self._config.auth_token or self._config.headers)
        return f"{type(self).__name__}(endpoint={safe_endpoint!r}, configured_auth={has_auth!r})"

    @property
    def config(self) -> McpServerConfig:
        return self._config

    @property
    def session_id(self) -> str | None:
        with self._lock:
            return self._session_id

    @property
    def session_generation(self) -> int:
        with self._lock:
            return self._session_generation

    @property
    def session_cache_hits(self) -> int:
        with self._lock:
            return self._session_cache_hits

    @property
    def session_cache_misses(self) -> int:
        with self._lock:
            return self._session_cache_misses

    @property
    def cached_tools(self) -> tuple[McpToolSpec, ...] | None:
        with self._lock:
            return self._tools_cache

    def _next_rpc_id(self, fixed_id: int) -> int:
        if self._config.fixed_operation_rpc_ids:
            return fixed_id
        self._rpc_id_counter += 1
        return self._rpc_id_counter

    def _build_headers(self, session_id: str | None) -> dict[str, str]:
        headers: dict[str, str] = {}
        reserved = {"content-type", "accept", "mcp-session-id"}
        if self._config.auth_token is not None:
            reserved.add("authorization")
        for key, value in self._config.headers.items():
            if key.lower() not in reserved:
                headers[key] = value
        if self._config.auth_token is not None:
            headers["Authorization"] = f"Bearer {self._config.auth_token}"
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json, text/event-stream"
        if session_id is not None:
            headers["Mcp-Session-Id"] = session_id
        return headers

    def _parse_jsonrpc_response(self, response: HttpResponse, expected_id: int | None) -> dict[str, Any]:
        if not isinstance(response, HttpResponse) or not isinstance(response.body, str):
            raise McpClientError(
                "mcp_protocol_error",
                "MCP transport returned an invalid HTTP response.",
                retryable=False,
                protocol_error=True,
                provider_response_received=True,
                error_metadata={"kind": "invalid_http_response"},
            )
        if len(response.body.encode("utf-8")) > self._config.max_response_bytes:
            raise McpClientError(
                "mcp_protocol_error",
                "MCP server returned an oversized response.",
                retryable=False,
                protocol_error=True,
                provider_response_received=True,
                error_metadata={"kind": "response_oversized"},
            )
        content_type = (_response_header(response, "Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type and content_type not in ("application/json", "text/event-stream"):
            raise McpClientError(
                "mcp_protocol_error",
                "MCP server returned an unsupported content type.",
                retryable=False,
                protocol_error=True,
                provider_response_received=True,
                error_metadata={"kind": "unsupported_content_type"},
            )

        messages: list[Any] = []
        body = response.body.strip()
        if body:
            if content_type == "text/event-stream":
                messages = _parse_sse_messages(response.body)
            else:
                try:
                    parsed = json.loads(body)
                    messages = parsed if isinstance(parsed, list) else [parsed]
                except json.JSONDecodeError as error:
                    if content_type == "application/json" or not _looks_like_sse(response.body):
                        raise McpClientError(
                            "mcp_protocol_error",
                            "MCP server returned invalid JSON-RPC data.",
                            retryable=False,
                            protocol_error=True,
                            provider_response_received=True,
                            error_metadata={"kind": "invalid_json"},
                        ) from error
                    messages = _parse_sse_messages(response.body)

        if expected_id is None:
            return {}

        for message in messages:
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or message.get("id") != expected_id:
                continue
            if "error" in message:
                error_object = message["error"] if isinstance(message["error"], dict) else {}
                if self._error_classifier is not None and error_object:
                    payload = json.dumps(
                        {"error": {"code": error_object.get("code"), "message": error_object.get("message")}},
                        ensure_ascii=False,
                    )
                    classified = self._error_classifier(payload, None)
                    if classified is not None:
                        raise classified
                raw_code = error_object.get("code")
                rpc_code = raw_code if type(raw_code) is int else None
                raw_msg = error_object.get("message")
                session_expired = isinstance(raw_msg, str) and _is_session_expired_text(raw_msg)
                raise McpClientError(
                    "mcp_protocol_error",
                    "MCP server rejected its own protocol request.",
                    retryable=False,
                    rpc_code=rpc_code,
                    session_expired=session_expired,
                    protocol_error=True,
                    provider_response_received=True,
                    error_metadata={"kind": "jsonrpc_error"},
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise McpClientError(
                    "mcp_protocol_error",
                    "MCP server returned an invalid JSON-RPC result object.",
                    retryable=False,
                    protocol_error=True,
                    provider_response_received=True,
                    error_metadata={"kind": "invalid_result_object"},
                )
            return result

        raise McpClientError(
            "mcp_protocol_error",
            "MCP server returned no matching protocol response.",
            retryable=False,
            protocol_error=True,
            provider_response_received=True,
            error_metadata={"kind": "missing_rpc_response"},
        )

    def _post(
        self,
        *,
        message: Mapping[str, Any],
        session_id: str | None,
        expected_id: int | None,
        deadline: float | None = None,
        cancel_event: Any = None,
    ) -> tuple[dict[str, Any], str | None]:
        _validate_endpoint_url(self._config.endpoint)
        headers = self._build_headers(session_id)
        body = canonical_json_dumps(message).encode("utf-8")
        last_failure: McpClientError | None = None

        for attempt in range(self._config.max_attempts):
            if cancel_event is not None and cancel_event.is_set():
                raise McpClientError(
                    "mcp_cancelled",
                    "MCP request was cancelled.",
                    retryable=True,
                    error_metadata={"kind": "cancelled"},
                )
            remaining = deadline - monotonic() if deadline is not None else float(self._config.timeout_seconds)
            if remaining <= 0:
                raise McpClientError(
                    "mcp_timeout",
                    "MCP request exceeded its time budget.",
                    retryable=True,
                    error_metadata={"kind": "budget_exhausted"},
                )
            call_timeout = min(float(self._config.timeout_seconds), remaining)
            try:
                if self._uses_default_transport:
                    response = _default_transport(
                        "POST",
                        self._config.endpoint,
                        headers,
                        body,
                        call_timeout,
                        allow_private_network=False,
                        max_response_bytes=self._config.max_response_bytes,
                        cancel_event=cancel_event,
                    )
                else:
                    response = self._transport(
                        "POST",
                        self._config.endpoint,
                        headers,
                        body,
                        call_timeout,
                    )
            except _TransportTimeout:
                last_failure = McpClientError(
                    "mcp_timeout",
                    "MCP server timed out without a usable result.",
                    retryable=True,
                    error_metadata={"kind": "transport_timeout"},
                )
            except (_TransportFailure, TimeoutError, OSError):
                last_failure = McpClientError(
                    "mcp_unavailable",
                    "MCP server is temporarily unavailable.",
                    retryable=True,
                    error_metadata={"kind": "transport_unavailable"},
                )
            else:
                if not isinstance(response, HttpResponse) or type(getattr(response, "status_code", None)) is not int:
                    raise McpClientError(
                        "mcp_protocol_error",
                        "MCP transport returned an invalid response object.",
                        retryable=False,
                        protocol_error=True,
                        error_metadata={"kind": "invalid_http_response"},
                    )
                if 200 <= response.status_code < 300:
                    result = self._parse_jsonrpc_response(response, expected_id)
                    received_session = _response_header(response, "Mcp-Session-Id")
                    if received_session is not None and not _SESSION_VALUE.fullmatch(received_session):
                        raise McpClientError(
                            "mcp_protocol_error",
                            "MCP server returned an invalid session identifier.",
                            retryable=False,
                            protocol_error=True,
                            provider_response_received=True,
                            error_metadata={"kind": "invalid_session_id"},
                        )
                    return result, received_session or session_id

                if response.status_code == 429 and self._error_classifier is not None and response.body:
                    classified = self._error_classifier(response.body, 429)
                    if classified is not None:
                        raise classified

                if response.status_code not in _RETRYABLE_STATUS_CODES:
                    session_expired = response.status_code in _SESSION_REINIT_STATUS_CODES or _is_session_expired_text(
                        response.body
                    )
                    raise McpClientError(
                        "mcp_rejected",
                        "MCP server rejected the request.",
                        retryable=False,
                        http_status=response.status_code,
                        session_expired=session_expired,
                        provider_response_received=True,
                        error_metadata={"kind": "http_rejected"},
                    )

                is_429 = response.status_code == 429
                last_failure = McpClientError(
                    "mcp_rate_limited" if is_429 else "mcp_unavailable",
                    "MCP server rate limit reached." if is_429 else "MCP server is temporarily unavailable.",
                    retryable=True,
                    http_status=response.status_code,
                    provider_response_received=True,
                    error_metadata={"kind": "http_rate_limited" if is_429 else "http_unavailable"},
                )

            if attempt + 1 < self._config.max_attempts:
                delay = 0.05 * (2**attempt)
                if deadline is not None and monotonic() + delay >= deadline:
                    raise McpClientError(
                        "mcp_timeout",
                        "MCP request exceeded its time budget.",
                        retryable=True,
                        error_metadata={"kind": "budget_exhausted"},
                    )
                self._sleeper(delay)

        assert last_failure is not None
        raise last_failure

    def _parse_tools_catalog(self, tools_result: Mapping[str, Any]) -> tuple[McpToolSpec, ...]:
        tools_raw = tools_result.get("tools")
        if not isinstance(tools_raw, list):
            raise McpClientError(
                "mcp_protocol_error",
                "MCP server returned no tool catalog.",
                retryable=False,
                protocol_error=True,
                provider_response_received=True,
                error_metadata={"kind": "invalid_tool_catalog"},
            )
        if len(tools_raw) > _MAX_TOOLS:
            raise McpClientError(
                "mcp_protocol_error",
                "MCP server returned an oversized tool catalog.",
                retryable=False,
                protocol_error=True,
                provider_response_received=True,
                error_metadata={"kind": "oversized_tool_catalog"},
            )
        specs: list[McpToolSpec] = []
        seen_names: set[str] = set()
        for entry in tools_raw:
            if not isinstance(entry, Mapping):
                raise McpClientError(
                    "mcp_protocol_error",
                    "MCP server returned an invalid tool specification.",
                    retryable=False,
                    protocol_error=True,
                    provider_response_received=True,
                    error_metadata={"kind": "invalid_tool_spec"},
                )
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                raise McpClientError(
                    "mcp_protocol_error",
                    "MCP tool specification is missing a valid name.",
                    retryable=False,
                    protocol_error=True,
                    provider_response_received=True,
                    error_metadata={"kind": "invalid_tool_name"},
                )
            raw_desc = entry.get("description")
            description = raw_desc if isinstance(raw_desc, str) else ""
            raw_schema = entry.get("inputSchema")
            if raw_schema is None:
                canonical_schema: dict[str, Any] = {}
            elif isinstance(raw_schema, Mapping):
                canonical_schema = canonicalize_json_value(dict(raw_schema))
            else:
                raise McpClientError(
                    "mcp_protocol_error",
                    "MCP tool specification has an invalid inputSchema.",
                    retryable=False,
                    protocol_error=True,
                    provider_response_received=True,
                    error_metadata={"kind": "invalid_tool_schema"},
                )
            raw_annotations = entry.get("annotations")
            canonical_annotations: dict[str, Any] = (
                canonicalize_json_value(dict(raw_annotations)) if isinstance(raw_annotations, Mapping) else {}
            )
            if name not in seen_names:
                seen_names.add(name)
                specs.append(
                    McpToolSpec(
                        name=name,
                        description=description,
                        input_schema=canonical_schema,
                        annotations=canonical_annotations,
                    )
                )
        return tuple(specs)

    def _fetch_all_tools(
        self,
        *,
        session_id: str | None,
        deadline: float | None,
        cancel_event: Any,
    ) -> tuple[tuple[McpToolSpec, ...], str | None]:
        all_specs: list[McpToolSpec] = []
        seen_names: set[str] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None
        current_session = session_id

        for _ in range(_MAX_TOOL_PAGES):
            params: dict[str, Any] = {"cursor": cursor} if cursor is not None else {}
            req_id = self._next_rpc_id(2)
            tools_result, current_session = self._post(
                message={"jsonrpc": "2.0", "id": req_id, "method": "tools/list", "params": params},
                session_id=current_session,
                expected_id=req_id,
                deadline=deadline,
                cancel_event=cancel_event,
            )
            page_specs = self._parse_tools_catalog(tools_result)
            for spec in page_specs:
                if spec.name not in seen_names:
                    seen_names.add(spec.name)
                    all_specs.append(spec)
                    if len(all_specs) > _MAX_TOOLS:
                        raise McpClientError(
                            "mcp_protocol_error",
                            "MCP server returned an oversized tool catalog.",
                            retryable=False,
                            protocol_error=True,
                            provider_response_received=True,
                            error_metadata={"kind": "oversized_tool_catalog"},
                        )
            next_cursor = tools_result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return tuple(all_specs), current_session
            if next_cursor in seen_cursors:
                raise McpClientError(
                    "mcp_protocol_error",
                    "MCP server returned a cyclic tools/list cursor.",
                    retryable=False,
                    protocol_error=True,
                    provider_response_received=True,
                    error_metadata={"kind": "cyclic_cursor"},
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        raise McpClientError(
            "mcp_protocol_error",
            "MCP server tools/list pagination exceeded safety limit.",
            retryable=False,
            protocol_error=True,
            provider_response_received=True,
            error_metadata={"kind": "too_many_tool_pages"},
        )

    def invalidate_session(self, *, expected_generation: int | None = None) -> None:
        """Clear cached MCP session and tool catalog unless superseded by a newer generation."""
        with self._lock:
            if expected_generation is not None and self._session_generation != expected_generation:
                return
            if self._session_id is not None or self._tools_cache is not None or self._protocol_version is not None:
                self._session_id = None
                self._protocol_version = None
                self._tools_cache = None
                self._tools_by_name = None
                self._session_generation += 1

    def _ensure_session(
        self,
        *,
        deadline: float | None = None,
        cancel_event: Any = None,
        force_refresh: bool = False,
        stale_generation: int | None = None,
    ) -> tuple[str | None, dict[str, McpToolSpec], int, bool]:
        """Return ``(session_id, tools_by_name, generation, reused_cached)``."""
        with self._lock:
            if not force_refresh and self._tools_cache is not None and self._tools_by_name is not None:
                self._session_cache_hits += 1
                return self._session_id, self._tools_by_name, self._session_generation, True
            if (
                force_refresh
                and self._tools_cache is not None
                and self._tools_by_name is not None
                and stale_generation is not None
                and self._session_generation != stale_generation
            ):
                self._session_cache_hits += 1
                return self._session_id, self._tools_by_name, self._session_generation, True

            if cancel_event is not None and cancel_event.is_set():
                raise McpClientError(
                    "mcp_cancelled",
                    "MCP request was cancelled.",
                    retryable=True,
                    error_metadata={"kind": "cancelled"},
                )
            if deadline is not None and deadline - monotonic() <= 0:
                raise McpClientError(
                    "mcp_timeout",
                    "MCP request exceeded its time budget.",
                    retryable=True,
                    error_metadata={"kind": "budget_exhausted"},
                )

            self._session_cache_misses += 1
            self._session_id = None
            self._protocol_version = None
            self._tools_cache = None
            self._tools_by_name = None
            self._session_generation += 1

            init_id = self._next_rpc_id(1)
            initialize, session_id = self._post(
                message={
                    "jsonrpc": "2.0",
                    "id": init_id,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": self._config.protocol_version,
                        "capabilities": {},
                        "clientInfo": {
                            "name": self._config.client_name,
                            "version": self._config.client_version,
                        },
                    },
                },
                session_id=None,
                expected_id=init_id,
                deadline=deadline,
                cancel_event=cancel_event,
            )
            protocol_version = initialize.get("protocolVersion")
            if not isinstance(protocol_version, str) or not protocol_version.strip():
                raise McpClientError(
                    "mcp_protocol_error",
                    "MCP server returned no protocol version.",
                    retryable=False,
                    protocol_error=True,
                    provider_response_received=True,
                    error_metadata={"kind": "missing_protocol_version"},
                )

            _, session_id = self._post(
                message={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                session_id=session_id,
                expected_id=None,
                deadline=deadline,
                cancel_event=cancel_event,
            )

            tools_tuple, session_id = self._fetch_all_tools(
                session_id=session_id,
                deadline=deadline,
                cancel_event=cancel_event,
            )
            tools_by_name = {spec.name: spec for spec in tools_tuple}
            self._session_id = session_id
            self._protocol_version = protocol_version
            self._tools_cache = tools_tuple
            self._tools_by_name = tools_by_name
            self._session_generation += 1
            return session_id, tools_by_name, self._session_generation, False

    def list_tools(
        self,
        *,
        force_refresh: bool = False,
        deadline: float | None = None,
        cancel_event: Any = None,
    ) -> tuple[McpToolSpec, ...]:
        """Return cached or freshly discovered MCP tool specifications."""
        self._ensure_session(
            deadline=deadline,
            cancel_event=cancel_event,
            force_refresh=force_refresh,
            stale_generation=None,
        )
        with self._lock:
            return self._tools_cache or ()

    def _parse_call_result(self, tool_name: str, result: Mapping[str, Any]) -> McpCallResult:
        raw_content = result.get("content")
        if not isinstance(raw_content, list) or len(raw_content) > self._config.max_content_blocks:
            raise McpClientError(
                "mcp_protocol_error",
                "MCP server returned invalid tool call content.",
                retryable=False,
                protocol_error=True,
                provider_response_received=True,
                error_metadata={"kind": "invalid_call_content"},
            )
        blocks: list[Mapping[str, Any]] = []
        for item in raw_content:
            if not isinstance(item, Mapping):
                raise McpClientError(
                    "mcp_protocol_error",
                    "MCP server returned a non-object content block.",
                    retryable=False,
                    protocol_error=True,
                    provider_response_received=True,
                    error_metadata={"kind": "invalid_content_block"},
                )
            blocks.append(dict(item))
        is_error = result.get("isError") is True
        structured = result.get("structuredContent")
        metadata = MappingProxyType(_tool_error_metadata(blocks)) if is_error else MappingProxyType({})
        return McpCallResult(
            tool_name=tool_name,
            content=tuple(blocks),
            structured_content=structured,
            is_error=is_error,
            untrusted_data=True,
            error_metadata=metadata,
        )

    def _run_tool_call(
        self,
        *,
        name: str,
        arguments: Mapping[str, Any],
        session_id: str | None,
        tools_by_name: Mapping[str, McpToolSpec],
        generation: int,
        deadline: float | None,
        cancel_event: Any,
    ) -> McpCallResult:
        if name not in tools_by_name:
            raise McpClientError(
                "mcp_tool_not_found",
                "Requested MCP tool is not exposed by the server.",
                retryable=False,
                error_metadata={"kind": "tool_not_found"},
            )
        req_id = self._next_rpc_id(3)
        canonical_args = canonicalize_json_value(dict(arguments))
        result, returned_session = self._post(
            message={
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": canonical_args},
            },
            session_id=session_id,
            expected_id=req_id,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        if returned_session and returned_session != session_id:
            with self._lock:
                if self._session_generation == generation:
                    self._session_id = returned_session
        return self._parse_call_result(name, result)

    @staticmethod
    def _should_reinitialize(error: McpClientError) -> bool:
        return bool(error.session_expired or error.protocol_error)

    def _retry_tool_call_once(
        self,
        *,
        name: str,
        arguments: Mapping[str, Any],
        stale_generation: int,
        deadline: float | None,
        cancel_event: Any,
    ) -> McpCallResult:
        session_id, tools_by_name, generation, _ = self._ensure_session(
            deadline=deadline,
            cancel_event=cancel_event,
            force_refresh=True,
            stale_generation=stale_generation,
        )
        try:
            call_result = self._run_tool_call(
                name=name,
                arguments=arguments,
                session_id=session_id,
                tools_by_name=tools_by_name,
                generation=generation,
                deadline=deadline,
                cancel_event=cancel_event,
            )
        except McpClientError as retry_error:
            if self._should_reinitialize(retry_error):
                self.invalidate_session(expected_generation=generation)
            raise
        if call_result.is_error and call_result.error_metadata.get("session_expired"):
            self.invalidate_session(expected_generation=generation)
        return call_result

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        deadline: float | None = None,
        cancel_event: Any = None,
    ) -> McpCallResult:
        """Invoke a discovered MCP tool and return an untrusted :class:`McpCallResult`."""
        if not isinstance(name, str) or not name.strip():
            raise McpClientError(
                "mcp_configuration_invalid",
                "MCP tool name must be a non-empty string.",
                retryable=False,
                error_metadata={"kind": "invalid_tool_name"},
            )
        args = {} if arguments is None else arguments
        if not isinstance(args, Mapping):
            raise McpClientError(
                "mcp_configuration_invalid",
                "MCP tool arguments must be a mapping.",
                retryable=False,
                error_metadata={"kind": "invalid_tool_arguments"},
            )

        session_id, tools_by_name, generation, reused = self._ensure_session(
            deadline=deadline,
            cancel_event=cancel_event,
        )
        try:
            call_result = self._run_tool_call(
                name=name,
                arguments=args,
                session_id=session_id,
                tools_by_name=tools_by_name,
                generation=generation,
                deadline=deadline,
                cancel_event=cancel_event,
            )
        except McpClientError as error:
            if not self._should_reinitialize(error):
                raise
            if not reused:
                self.invalidate_session(expected_generation=generation)
                raise
            return self._retry_tool_call_once(
                name=name,
                arguments=args,
                stale_generation=generation,
                deadline=deadline,
                cancel_event=cancel_event,
            )

        if call_result.is_error and call_result.error_metadata.get("session_expired"):
            if not reused:
                self.invalidate_session(expected_generation=generation)
                return call_result
            return self._retry_tool_call_once(
                name=name,
                arguments=args,
                stale_generation=generation,
                deadline=deadline,
                cancel_event=cancel_event,
            )

        return call_result
