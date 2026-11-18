"""Server-only GLM Coding Plan MCP web-search boundary.

The official Coding Plan search service is a remote Streamable HTTP MCP server.
This adapter keeps its fixed endpoint, credential, JSON-RPC session and raw search
results on the API service.  It intentionally returns *candidates*, never facts:
callers must show the source to a user and submit a canonical EvidenceBundle before
any result becomes challenge evidence or is supplied to an LLM prompt.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import os
import re
from time import monotonic, sleep
from typing import Any, Callable, Mapping

from .openai_compatible import HttpResponse, OpenAICompatibleTransport, _TransportFailure, _TransportTimeout, _default_transport
from .phase3 import ClientSearchEvidenceProvider, ProviderFailure


_GLM_CODING_API_KEY_ENV = "FORESIGHTNINE_GLM_CODING_API_KEY"
_MCP_ENDPOINT = "https://open.bigmodel.cn/api/mcp/web_search_prime/mcp"
_MCP_HOST = "open.bigmodel.cn"
_MCP_PROTOCOL_VERSION = "2025-03-26"
_MCP_CLIENT_NAME = "foresightnine"
_MCP_CLIENT_VERSION = "1.0"
# The Coding Plan currently advertises the snake_case name.  Keep the older
# camelCase spelling as a strict compatibility alias for recorded fixtures,
# but always call the exact name returned by tools/list.
_MCP_TOOL_NAMES = frozenset({"web_search_prime", "webSearchPrime"})
_MAX_QUERY_LENGTH = 70
_MAX_RESULTS = 10
_MAX_RESPONSE_CHARS = 256 * 1024
_MAX_CONTENT_BLOCKS = 32
_MAX_CANDIDATES = 50
_MAX_RESULT_DEPTH = 8
_MAX_RESULT_NODES = 2048
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_SESSION_VALUE = re.compile(r"^[\x21-\x7e]{1,256}$")


@dataclass(frozen=True)
class SearchCandidate:
    """An unverified provider search result, safe to display for user review."""

    title: str
    url: str
    excerpt: str
    publisher: str | None
    published_at: date | None
    independently_reviewable: bool


class GlmCodingMcpSearchEvidenceProvider(ClientSearchEvidenceProvider):
    """Fixed-host, session-aware adapter for GLM Coding Plan web-search MCP."""

    name = "glm-coding-mcp-search"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        transport: OpenAICompatibleTransport | None = None,
        timeout_seconds: float = 15.0,
        max_attempts: int = 2,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._api_key = (api_key or "").strip() or None
        self._transport = transport or _default_transport
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._sleeper = sleeper or sleep
        if not 0 < timeout_seconds <= 30:
            raise ValueError("MCP search timeout must be between 0 and 30 seconds.")
        if not 1 <= max_attempts <= 3:
            raise ValueError("MCP search attempts must be between 1 and 3.")

    @classmethod
    def from_environment(cls) -> "GlmCodingMcpSearchEvidenceProvider":
        return cls(os.environ.get(_GLM_CODING_API_KEY_ENV))

    def __repr__(self) -> str:
        return f"{type(self).__name__}(configured={bool(self._api_key)!r}, endpoint=<fixed>)"

    @property
    def supports_project_search(self) -> bool:
        # Health and discovery capability must agree: a configured adapter is
        # not usable until its server-only credential is present.  Returning
        # False here lets every client choose the explicit client-bundle
        # fallback without first attempting an external request.
        return self._api_key is not None

    @staticmethod
    def _validate_query(query: str, limit: int) -> None:
        if not isinstance(query, str) or not query.strip() or len(query) > _MAX_QUERY_LENGTH:
            raise ProviderFailure("search_query_invalid", "Search query is outside the accepted length limit.", retryable=False)
        if not isinstance(limit, int) or not 1 <= limit <= _MAX_RESULTS:
            raise ProviderFailure("search_query_invalid", "Search result limit is outside the accepted range.", retryable=False)

    @staticmethod
    def _response_header(response: HttpResponse, name: str) -> str | None:
        for key, value in response.headers.items():
            if key.lower() == name.lower():
                return value
        return None

    @staticmethod
    def _jsonrpc_message(response: HttpResponse, expected_id: int | None) -> dict[str, Any]:
        if len(response.body) > _MAX_RESPONSE_CHARS:
            raise ProviderFailure("search_response_invalid", "Search provider returned an oversized result.", retryable=False)
        messages: list[Any] = []
        body = response.body.strip()
        if body:
            try:
                parsed = json.loads(body)
                messages = parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                for line in response.body.splitlines():
                    if line.startswith("data:"):
                        try:
                            messages.append(json.loads(line.removeprefix("data:").strip()))
                        except json.JSONDecodeError as error:
                            raise ProviderFailure("search_response_invalid", "Search provider returned invalid structured data.", retryable=False) from error
        if expected_id is None:
            return {}
        for message in messages:
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or message.get("id") != expected_id:
                continue
            if "error" in message or not isinstance(message.get("result"), dict):
                raise ProviderFailure("search_response_invalid", "Search provider rejected its own protocol request.", retryable=False)
            return message["result"]
        raise ProviderFailure("search_response_invalid", "Search provider returned no matching protocol response.", retryable=False)

    def _post(self, *, message: Mapping[str, Any], session_id: str | None, expected_id: int | None,
              deadline: float | None = None, cancel_event: Any = None) -> tuple[dict[str, Any], str | None]:
        if self._api_key is None:
            raise ProviderFailure("search_provider_unconfigured", "Project search is not configured on this server.", retryable=False)
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session_id is not None:
            headers["Mcp-Session-Id"] = session_id
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        last_failure: ProviderFailure | None = None
        for attempt in range(self._max_attempts):
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderFailure("search_cancelled", "Search was cancelled.", retryable=True)
            remaining = deadline - monotonic() if deadline is not None else self._timeout_seconds
            if remaining <= 0:
                raise ProviderFailure("search_timeout", "Search exceeded its time budget.", retryable=True)
            try:
                response = self._transport("POST", _MCP_ENDPOINT, headers, body, min(self._timeout_seconds, remaining))
            except _TransportTimeout as error:
                last_failure = ProviderFailure("search_provider_timeout", "Search provider timed out without a usable result.", retryable=True)
            except (_TransportFailure, TimeoutError, OSError):
                last_failure = ProviderFailure("search_provider_unavailable", "Search provider is temporarily unavailable.", retryable=True)
            else:
                if 200 <= response.status_code < 300:
                    result = self._jsonrpc_message(response, expected_id)
                    received_session = self._response_header(response, "Mcp-Session-Id")
                    if received_session is not None and not _SESSION_VALUE.fullmatch(received_session):
                        raise ProviderFailure("search_response_invalid", "Search provider returned an invalid session identifier.", retryable=False)
                    return result, received_session or session_id
                if response.status_code not in _RETRYABLE_STATUS_CODES:
                    raise ProviderFailure("search_provider_rejected", "Search provider rejected the request.", retryable=False)
                last_failure = ProviderFailure(
                    "search_provider_rate_limited" if response.status_code == 429 else "search_provider_unavailable",
                    "Search provider is temporarily unavailable.",
                    retryable=True,
                )
            if attempt + 1 < self._max_attempts:
                delay = 0.05 * (2**attempt)
                if deadline is not None and monotonic() + delay >= deadline:
                    raise ProviderFailure("search_timeout", "Search exceeded its time budget.", retryable=True)
                self._sleeper(delay)
        assert last_failure is not None
        raise last_failure

    @staticmethod
    def _tool_schema(tools_result: Mapping[str, Any]) -> tuple[Mapping[str, Any], str, str]:
        tools = tools_result.get("tools")
        if not isinstance(tools, list):
            raise ProviderFailure("search_response_invalid", "Search provider returned no tool catalog.", retryable=False)
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("name") not in _MCP_TOOL_NAMES:
                continue
            schema = tool.get("inputSchema")
            if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
                break
            properties = schema["properties"]
            query_field = "search_query" if "search_query" in properties else "query" if "query" in properties else None
            if query_field is None:
                break
            return schema, query_field, tool["name"]
        raise ProviderFailure("search_response_invalid", "Search provider did not expose the expected search tool.", retryable=False)

    @staticmethod
    def _result_rows(value: Any) -> list[Mapping[str, Any]]:
        nodes = 0

        def visit(current: Any, depth: int) -> list[Mapping[str, Any]]:
            nonlocal nodes
            if not isinstance(current, (dict, list)):
                return []
            if depth > _MAX_RESULT_DEPTH:
                raise ProviderFailure(
                    "search_response_invalid",
                    "Search provider returned overly nested result data.",
                    retryable=False,
                )
            nodes += 1
            if nodes > _MAX_RESULT_NODES:
                raise ProviderFailure(
                    "search_response_invalid",
                    "Search provider returned excessive result data.",
                    retryable=False,
                )
            if isinstance(current, dict):
                for key in ("search_result", "search_results", "results"):
                    rows = current.get(key)
                    if isinstance(rows, list):
                        return [row for row in rows if isinstance(row, dict)]
                for nested in current.values():
                    rows = visit(nested, depth + 1)
                    if rows:
                        return rows
            else:
                direct_rows = [row for row in current if isinstance(row, dict)]
                if any("title" in row and ("link" in row or "url" in row) for row in direct_rows):
                    return direct_rows
                for nested in current:
                    rows = visit(nested, depth + 1)
                    if rows:
                        return rows
            return []

        return visit(value, 0)

    def _candidates(self, result: Mapping[str, Any], limit: int) -> tuple[SearchCandidate, ...]:
        content = result.get("content")
        if not isinstance(content, list) or len(content) > _MAX_CONTENT_BLOCKS:
            raise ProviderFailure("search_response_invalid", "Search provider returned invalid result content.", retryable=False)
        rows: list[Mapping[str, Any]] = []
        structured_content = result.get("structuredContent")
        if structured_content is not None:
            rows.extend(self._result_rows(structured_content))
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
                continue
            text = block["text"].strip()
            if text.startswith("```json") and text.endswith("```"):
                text = text[7:-3].strip()
            try:
                parsed = json.loads(text)
                # The Coding Plan currently wraps the JSON search payload in a
                # JSON-encoded string inside the MCP text block.  Decode only
                # this bounded second layer; arbitrary recursive decoding is
                # intentionally not allowed.
                if isinstance(parsed, str):
                    parsed = json.loads(parsed)
            except json.JSONDecodeError:
                continue
            rows.extend(self._result_rows(parsed))
        candidates: list[SearchCandidate] = []
        seen_urls: set[str] = set()
        for row in rows[:_MAX_CANDIDATES]:
            title = row.get("title")
            url = row.get("link", row.get("url"))
            excerpt = row.get("content", row.get("snippet", row.get("summary")))
            publisher = row.get("media", row.get("publisher"))
            raw_date = row.get("publish_date", row.get("published_at"))
            if not all(isinstance(value, str) and value.strip() for value in (title, url, excerpt)):
                continue
            if len(title) > 500 or len(url) > self.max_url_length or len(excerpt) > 10_000:
                continue
            try:
                self.validate_url(url)
            except ProviderFailure:
                continue
            published_at: date | None = None
            if isinstance(raw_date, str) and raw_date:
                try:
                    published_at = date.fromisoformat(raw_date[:10])
                except ValueError:
                    pass
            normalized_url = url.strip()
            if normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)
            candidates.append(SearchCandidate(
                title=title.strip(),
                url=normalized_url,
                excerpt=excerpt.strip(),
                publisher=publisher.strip() if isinstance(publisher, str) and publisher.strip() else None,
                published_at=published_at,
                independently_reviewable=published_at is not None,
            ))
            if len(candidates) == limit:
                break
        if not candidates:
            raise ProviderFailure("search_response_invalid", "Search provider returned no safely usable source candidates.", retryable=False)
        return tuple(candidates)

    def search(self, query: str, *, limit: int = 5, deadline: float | None = None, cancel_event: Any = None) -> tuple[SearchCandidate, ...]:
        """Discover bounded source candidates without supplying them to a model."""

        self._validate_query(query, limit)
        # Omit new keywords for legacy callers and their injected transports.
        budget = {"deadline": deadline, "cancel_event": cancel_event} if deadline is not None or cancel_event is not None else {}
        initialize, session_id = self._post(
            message={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": _MCP_CLIENT_NAME, "version": _MCP_CLIENT_VERSION},
                },
            },
            session_id=None,
            expected_id=1,
            **budget,
        )
        protocol_version = initialize.get("protocolVersion")
        if not isinstance(protocol_version, str) or not protocol_version:
            raise ProviderFailure("search_response_invalid", "Search provider returned no protocol version.", retryable=False)
        _, session_id = self._post(
            message={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            session_id=session_id,
            expected_id=None,
            **budget,
        )
        tools, session_id = self._post(
            message={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            session_id=session_id,
            expected_id=2,
            **budget,
        )
        schema, query_field, tool_name = self._tool_schema(tools)
        arguments: dict[str, Any] = {query_field: query.strip()}
        properties = schema["properties"]
        if "count" in properties:
            arguments["count"] = limit
        result, _ = self._post(
            message={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
            session_id=session_id,
            expected_id=3,
            **budget,
        )
        return self._candidates(result, limit)
