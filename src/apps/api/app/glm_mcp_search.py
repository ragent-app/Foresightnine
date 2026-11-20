"""Server-only GLM Coding Plan MCP web-search boundary.

The official Coding Plan search service is a remote Streamable HTTP MCP server.
This adapter keeps its fixed endpoint, credential, JSON-RPC session and raw search
results on the API service.  It intentionally returns *candidates*, never facts:
callers must show the source to a user and submit a canonical EvidenceBundle before
any result becomes challenge evidence or is supplied to an LLM prompt.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import os
import re
from time import monotonic, sleep
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .openai_compatible import HttpResponse, OpenAICompatibleTransport, _TransportFailure, _TransportTimeout, _default_transport
from .phase3 import ClientSearchEvidenceProvider, ProviderFailure
from .research_discovery import RedirectLead, unwrap_redirect_url


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
_MAX_QUERY_LENGTH = 10000
_MAX_RESULTS = 10
_MAX_RESPONSE_CHARS = 256 * 1024
_MAX_CONTENT_BLOCKS = 32
_MAX_CANDIDATES = 50
_MAX_RESULT_DEPTH = 8
_MAX_RESULT_NODES = 2048
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_SESSION_VALUE = re.compile(r"^[\x21-\x7e]{1,256}$")
_URL_FIELDS = ("link", "url", "target_url", "real_url", "origin_url")
_DATE_FIELDS = ("publish_date", "published_at", "date")


# --- MCP tool-error detection -------------------------------------------------------------------------------
# The provider reports usage caps and rate limits as an MCP tool result whose text is, for example,
# ``MCP error -429: {"error":{"code":"1310","message":"..."}}``.  Those are not malformed responses.
_MCP_ERROR_PREFIX = re.compile(r"^\s*MCP error\s*(-?\d+)?\s*:?\s*(.*)$", re.I | re.S)
_QUOTA_PROVIDER_CODES = frozenset({"1308", "1309", "1310"})
_RATE_PROVIDER_CODES = frozenset({"1302", "1303", "1305"})
_QUOTA_WORDS = re.compile(r"使用上限|用量上限|额度|限额|已用尽|已耗尽|每周|每月|usage limit|quota|exhaust|weekly|monthly|balance", re.I)
_RATE_WORDS = re.compile(r"too many requests|rate limit|频率|速率|请求过多|请求过于频繁|稍后", re.I)
_RESET_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?")
# bigmodel.cn reports local (China Standard Time) timestamps without an offset.
_PROVIDER_TZ = timezone(timedelta(hours=8))
_SECRET_LIKE = re.compile(r"(Bearer\s+\S+|sk-[A-Za-z0-9_\-]{8,}|[A-Za-z0-9_\-.]{32,})")
_MAX_ERROR_MESSAGE = 300


def _safe_provider_text(text: str) -> str:
    return _SECRET_LIKE.sub("[redacted]", " ".join(text.split()))[:_MAX_ERROR_MESSAGE]


def _parse_reset_at(text: str) -> datetime | None:
    match = _RESET_TIME.search(text)
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(part) if part else 0 for part in match.groups())
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=_PROVIDER_TZ)
    except ValueError:
        return None


def mcp_error_failure(text: str, *, http_status: int | None = None) -> ProviderFailure | None:
    """Classify an MCP/provider error payload; ``None`` when the text is not an error payload.

    Quota (a usage cap with an optional reset time) is ``search_quota_exhausted`` and not retryable; transient
    429s are ``search_rate_limited``; any other error is ``search_provider_error``.  The provider's message is kept
    (credential-like tokens redacted).  The failure is marked as a received provider response.
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    rpc_code: int | None = None
    body = stripped
    prefixed = _MCP_ERROR_PREFIX.match(stripped)
    if prefixed is not None:
        rpc_code = int(prefixed.group(1)) if prefixed.group(1) else None
        body = prefixed.group(2).strip()
    provider_code: str | None = None
    message = body
    parsed: Any = None
    start = body.find("{")
    if start != -1:
        try:
            parsed = json.loads(body[start:])
        except json.JSONDecodeError:
            parsed = None
    if isinstance(parsed, dict):
        error = parsed.get("error") if isinstance(parsed.get("error"), dict) else None
        if error is None and prefixed is None:
            return None   # an ordinary JSON payload, not an error
        if isinstance(error, dict):
            code = error.get("code")
            provider_code = str(code).strip() if isinstance(code, (str, int)) and not isinstance(code, bool) else None
            if isinstance(error.get("message"), str) and error["message"].strip():
                message = error["message"].strip()
    elif prefixed is None:
        return None
    if provider_code is not None and provider_code.lstrip("-").isdigit() and rpc_code is None and provider_code in ("429", "-429"):
        rpc_code = -429
    safe = _safe_provider_text(message)
    is_429 = http_status == 429 or rpc_code in (429, -429) or (provider_code or "") in ("429", "-429")
    if provider_code in _QUOTA_PROVIDER_CODES or ((is_429 or provider_code in _RATE_PROVIDER_CODES) and _QUOTA_WORDS.search(message)):
        failure = ProviderFailure("search_quota_exhausted", safe or "Search usage limit reached.", retryable=False)
        reset_at = _parse_reset_at(message)
        failure.reset_at = reset_at.isoformat() if reset_at is not None else None
    elif is_429 or provider_code in _RATE_PROVIDER_CODES or _RATE_WORDS.search(message):
        failure = ProviderFailure("search_rate_limited", safe or "Search provider rate limit reached.", retryable=True)
        failure.reset_at = None
    else:
        failure = ProviderFailure("search_provider_error", safe or "Search provider reported an error.", retryable=False)
        failure.reset_at = None
    failure.provider_response_received = True
    return failure


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

    max_query_length = _MAX_QUERY_LENGTH

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
            if "error" in message and isinstance(message["error"], dict):
                error_object = message["error"]
                classified = mcp_error_failure(json.dumps({"error": {"code": error_object.get("code"), "message": error_object.get("message")}}, ensure_ascii=False))
                if classified is not None and classified.code in ("search_quota_exhausted", "search_rate_limited"):
                    raise classified
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
                if response.status_code == 429:
                    classified = mcp_error_failure(response.body, http_status=429) if response.body else None
                    if classified is not None and classified.code == "search_quota_exhausted":
                        raise classified   # a usage cap does not clear by retrying
                if response.status_code not in _RETRYABLE_STATUS_CODES:
                    raise ProviderFailure("search_provider_rejected", "Search provider rejected the request.", retryable=False)
                last_failure = ProviderFailure(
                    "search_provider_rate_limited" if response.status_code == 429 else "search_provider_unavailable",
                    "Search provider is temporarily unavailable.",
                    retryable=True,
                )
                if response.status_code == 429:
                    last_failure.provider_response_received = True
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
                if any("title" in row and any(field in row for field in _URL_FIELDS) for row in direct_rows):
                    return direct_rows
                for nested in current:
                    rows = visit(nested, depth + 1)
                    if rows:
                        return rows
            return []

        return visit(value, 0)

    @staticmethod
    def _parse_publish_date(raw_date: Any) -> date | None:
        """Parse YYYY-MM-DD strings or ISO-8601 timestamps into a UTC calendar date."""
        if isinstance(raw_date, datetime):
            return raw_date.astimezone(timezone.utc).date() if raw_date.tzinfo is not None else raw_date.date()
        if isinstance(raw_date, date):
            return raw_date
        if not isinstance(raw_date, str):
            return None
        text = raw_date.strip()
        if not text:
            return None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            try:
                return date.fromisoformat(text)
            except ValueError:
                return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed.astimezone(timezone.utc).date() if parsed.tzinfo is not None else parsed.date()

    @classmethod
    def _extract_publish_date(cls, row: Mapping[str, Any]) -> date | None:
        for field in _DATE_FIELDS:
            parsed = cls._parse_publish_date(row.get(field))
            if parsed is not None:
                return parsed
        return None

    def _resolve_row_url(self, row: Mapping[str, Any]) -> tuple[str, bool] | None:
        """Select a safe direct/unwrapped URL, or retain a safe opaque redirector for runtime classification."""
        fallback_redirect: str | None = None
        for field in _URL_FIELDS:
            raw = row.get(field)
            if not isinstance(raw, str):
                continue
            candidate = raw.strip()
            if not candidate or len(candidate) > self.max_url_length:
                continue
            try:
                self.validate_url(candidate)
            except ProviderFailure:
                if urlsplit(candidate).scheme in {"http", "https"}:
                    return None
                continue
            try:
                unwrapped = unwrap_redirect_url(candidate)
                self.validate_url(unwrapped)
                return unwrapped, False
            except RedirectLead:
                if fallback_redirect is None:
                    fallback_redirect = candidate
            except ProviderFailure:
                return None
        if fallback_redirect is None:
            return None
        return fallback_redirect, True

    def _candidates(self, result: Mapping[str, Any], limit: int) -> tuple[SearchCandidate, ...]:
        content = result.get("content")
        if not isinstance(content, list) or len(content) > _MAX_CONTENT_BLOCKS:
            raise ProviderFailure("search_response_invalid", "Search provider returned invalid result content.", retryable=False)
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                error_failure = mcp_error_failure(block["text"])
                if error_failure is not None:
                    raise error_failure
        if result.get("isError") is True:
            first_text = next((b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)), "")
            error_failure = ProviderFailure("search_provider_error", _safe_provider_text(first_text) or "Search provider reported an error.", retryable=False)
            error_failure.provider_response_received = True
            error_failure.reset_at = None
            raise error_failure
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
        usable_candidates: list[SearchCandidate] = []
        redirect_candidates: list[SearchCandidate] = []
        seen_urls: set[str] = set()
        for row in rows[:_MAX_CANDIDATES]:
            title = row.get("title")
            excerpt = next((v for k in ("content", "snippet", "summary") if isinstance((v := row.get(k)), str) and v.strip()), None)
            publisher = next((v.strip() for k in ("media", "publisher") if isinstance((v := row.get(k)), str) and v.strip()), None)
            if not isinstance(title, str) or not title.strip() or excerpt is None:
                continue
            if len(title) > 500 or len(excerpt) > 10_000:
                continue
            resolved = self._resolve_row_url(row)
            if resolved is None:
                continue
            normalized_url, is_opaque_redirect = resolved
            if normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)
            published_at = self._extract_publish_date(row)
            candidate = SearchCandidate(
                title=title.strip(),
                url=normalized_url,
                excerpt=excerpt.strip(),
                publisher=publisher,
                published_at=published_at,
                independently_reviewable=published_at is not None,
            )
            if is_opaque_redirect:
                redirect_candidates.append(candidate)
            else:
                usable_candidates.append(candidate)
                if len(usable_candidates) == limit:
                    break
        candidates = (usable_candidates + redirect_candidates)[:limit]
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
        properties = schema["properties"]
        query_schema = properties.get(query_field, {})
        provider_limit = query_schema.get("maxLength") if isinstance(query_schema, dict) else None
        if isinstance(provider_limit, int) and len(query.strip()) > provider_limit:
            raise ProviderFailure("search_query_provider_limit", "Query exceeds the provider-declared limit.", retryable=False)
        arguments: dict[str, Any] = {query_field: query.strip()}
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
