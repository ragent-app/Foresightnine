from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest

from app.glm_mcp_search import GlmCodingMcpSearchEvidenceProvider
from app.openai_compatible import HttpResponse
from app.phase3 import ProviderFailure, build_search_evidence_provider


_SECRET = "unit-test-only-mcp-secret"


def response(message: dict[str, object], *, session: str | None = None, status: int = 200) -> HttpResponse:
    headers = {"mcp-session-id": session} if session else {}
    return HttpResponse(status, json.dumps(message), headers)


def test_glm_mcp_search_uses_fixed_endpoint_pkce_free_session_protocol_and_returns_unverified_candidates() -> None:
    calls: list[tuple[str, str, dict[str, str], dict[str, object], float]] = []
    replies = [
        response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}, session="safe-session-1"),
        HttpResponse(202, ""),
        response({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "webSearchPrime", "inputSchema": {"type": "object", "properties": {"search_query": {"type": "string"}, "count": {"type": "integer"}}}}]}}),
        response({"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": json.dumps({"search_result": [
            {"title": "Official filing", "link": "https://example.com/filing", "content": "A dated source excerpt.", "media": "Example", "publish_date": "2026-08-01"},
            {"title": "No date", "link": "https://example.com/context", "content": "Context only.", "media": "Example"},
        ]})}]}}),
    ]

    def transport(method: str, url: str, headers: dict[str, str], body: bytes, timeout: float) -> HttpResponse:
        calls.append((method, url, headers, json.loads(body), timeout))
        return replies.pop(0)

    provider = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=transport, sleeper=lambda _delay: None)
    candidates = provider.search("dated public macro release", limit=2)

    assert [candidate.title for candidate in candidates] == ["Official filing", "No date"]
    assert candidates[0].independently_reviewable is True
    assert candidates[1].independently_reviewable is False
    assert [call[1] for call in calls] == ["https://open.bigmodel.cn/api/mcp/web_search_prime/mcp"] * 4
    assert [call[3].get("method") for call in calls] == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert calls[0][2]["Authorization"] == f"Bearer {_SECRET}"
    assert all(call[2].get("Mcp-Session-Id") == "safe-session-1" for call in calls[1:])
    assert calls[-1][3]["params"] == {"name": "webSearchPrime", "arguments": {"search_query": "dated public macro release", "count": 2}}
    assert _SECRET not in repr(provider)
    assert _SECRET not in json.dumps(calls[-1][3])


def test_glm_mcp_search_accepts_official_snake_case_tool_and_nested_json_text() -> None:
    replies = [
        response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}, session="safe-session-official"),
        HttpResponse(202, ""),
        response({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "web_search_prime", "inputSchema": {"type": "object", "properties": {"search_query": {"type": "string"}, "count": {"type": "integer"}}}}]}}),
        response({"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": json.dumps(json.dumps([{"title": "Official result", "link": "https://example.com/official", "content": "A dated official result.", "publish_date": "2026-08-30"}]))}]}}),
    ]
    calls: list[dict[str, object]] = []

    def transport(_method: str, _url: str, _headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        calls.append(json.loads(body))
        return replies.pop(0)

    candidates = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=transport, sleeper=lambda _delay: None).search("official policy announcement", limit=1)

    assert candidates[0].url == "https://example.com/official"
    assert candidates[0].independently_reviewable is True
    assert calls[-1]["params"] == {"name": "web_search_prime", "arguments": {"search_query": "official policy announcement", "count": 1}}


def test_glm_mcp_search_missing_secret_makes_no_request() -> None:
    calls = 0

    def transport(*_args: object) -> HttpResponse:
        nonlocal calls
        calls += 1
        raise AssertionError("network must not be called without a server secret")

    provider = GlmCodingMcpSearchEvidenceProvider(None, transport=transport)
    assert provider.supports_project_search is False
    with pytest.raises(ProviderFailure, match="not configured"):
        provider.search("public query")
    assert calls == 0


def test_glm_mcp_search_drops_private_or_malformed_result_urls() -> None:
    replies = [
        response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}, session="safe-session-2"),
        HttpResponse(202, ""),
        response({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "webSearchPrime", "inputSchema": {"type": "object", "properties": {"search_query": {"type": "string"}}}}]}}),
        response({"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": json.dumps({"search_result": [
            {"title": "Private", "link": "http://127.0.0.1/private", "content": "Must not enter the product."},
        ]})}]}}),
    ]
    provider = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=lambda *_args: replies.pop(0), sleeper=lambda _delay: None)

    with pytest.raises(ProviderFailure, match="no safely usable"):
        provider.search("public query")


def test_glm_mcp_search_rejects_unexpected_protocol_tool_and_bounds_query() -> None:
    provider = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=lambda *_args: (_ for _ in ()).throw(AssertionError("no call expected")))
    with pytest.raises(ProviderFailure, match="length"):
        provider.search("x" * 10001)

    replies = [
        response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}, session="safe-session-3"),
        HttpResponse(202, ""),
        response({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}),
    ]
    provider = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=lambda *_args: replies.pop(0), sleeper=lambda _delay: None)
    with pytest.raises(ProviderFailure, match="expected search tool"):
        provider.search("public query")


def test_glm_declared_query_limit_is_reported_before_search_call() -> None:
    replies = [
        response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}, session="limit-session"),
        HttpResponse(202, ""),
        response({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "webSearchPrime", "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "maxLength": 80}}}}]}}),
    ]
    provider = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=lambda *_args: replies.pop(0), sleeper=lambda _delay: None)
    with pytest.raises(ProviderFailure, match="provider-declared limit"):
        provider.search("x" * 81)
    assert not replies


def test_glm_mcp_search_accepts_advertised_query_alias_and_structured_content() -> None:
    replies = [
        response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}, session="safe-session-4"),
        HttpResponse(202, ""),
        response({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "webSearchPrime", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}}]}}),
        response({"jsonrpc": "2.0", "id": 3, "result": {"structuredContent": {"results": [{"title": "Structured", "url": "https://example.com/structured", "snippet": "A structured result."}]}, "content": []}}),
    ]
    calls: list[dict[str, object]] = []

    def transport(_method: str, _url: str, _headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        calls.append(json.loads(body))
        return replies.pop(0)

    candidates = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=transport, sleeper=lambda _delay: None).search("public query")

    assert candidates[0].title == "Structured"
    assert calls[-1]["params"] == {"name": "webSearchPrime", "arguments": {"query": "public query"}}


def test_glm_mcp_search_fails_closed_when_result_nesting_exceeds_budget() -> None:
    nested: object = []
    for index in range(12):
        nested = {"nested": nested} if index % 2 else [nested]

    provider = GlmCodingMcpSearchEvidenceProvider(_SECRET)

    with pytest.raises(ProviderFailure) as error:
        provider._result_rows(nested)

    assert error.value.code == "search_response_invalid"
    assert error.value.retryable is False


def test_glm_mcp_search_fails_closed_when_result_node_budget_is_exceeded() -> None:
    value = {"nested": [{} for _ in range(2050)]}
    provider = GlmCodingMcpSearchEvidenceProvider(_SECRET)

    with pytest.raises(ProviderFailure) as error:
        provider._result_rows(value)

    assert error.value.code == "search_response_invalid"
    assert error.value.retryable is False


def test_glm_mcp_search_security_document_names_official_tool_and_fixture_alias() -> None:
    document = Path(__file__).resolve().parents[3] / "docs" / "security" / "glm-coding-mcp-search.md"
    text = document.read_text(encoding="utf-8")

    assert "`web_search_prime`" in text
    assert "`webSearchPrime`" in text
    assert "旧测试夹具" in text


def test_search_provider_selector_requires_explicit_glm_mcp_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_SEARCH_PROVIDER", "glm_coding_mcp")
    monkeypatch.delenv("FORESIGHTNINE_GLM_CODING_API_KEY", raising=False)
    built = build_search_evidence_provider()

    assert isinstance(built, GlmCodingMcpSearchEvidenceProvider)
    assert built.supports_project_search is False
    with pytest.raises(ProviderFailure, match="not configured"):
        built.search("public query")


def _mcp_replies(search_results: list[dict[str, object]]) -> list[HttpResponse]:
    return [
        response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}, session="safe-session-extra"),
        HttpResponse(202, ""),
        response({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "web_search_prime", "inputSchema": {"type": "object", "properties": {"search_query": {"type": "string"}, "count": {"type": "integer"}}}}]}}),
        response({"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": json.dumps({"search_result": search_results})}]}}),
    ]


def test_glm_mcp_search_prefers_fallback_and_unwrapped_urls_ahead_of_opaque_goto() -> None:
    import base64

    bing_target = "https://www.sec.gov/Archives/edgar/data/1045810/nvda-10k.htm"
    bing_token = "a1" + base64.urlsafe_b64encode(bing_target.encode("utf-8")).decode("ascii").rstrip("=")
    replies = _mcp_replies([
        {"title": "Opaque first", "link": "https://www.google.com/goto?url=CAESdwHEIkXX&gsessionid=x", "content": "Opaque only."},
        {"title": "Fallback url", "link": "https://www.google.com/goto?url=CAESopaque1", "url": "https://investor.nvidia.com/news", "content": "Investor release."},
        {"title": "Fallback target_url", "link": "https://www.google.com/goto?url=CAESopaque2", "target_url": "https://www.bea.gov/news/release", "content": "BEA release."},
        {"title": "Fallback real_url", "link": "https://www.google.com/goto?url=CAESopaque3", "real_url": "https://example.com/real", "content": "Real url."},
        {"title": "Fallback origin_url", "link": "https://www.google.com/goto?url=CAESopaque4", "origin_url": "https://example.com/origin", "content": "Origin url."},
        {"title": "Bing ck/a", "link": f"https://www.bing.com/ck/a?!&&p=1&u={bing_token}&ntb=1", "content": "SEC filing."},
        {"title": "Yahoo RU", "link": "https://r.search.yahoo.com/_ylt=Awr9/RV=2/RO=10/RU=https%3a%2f%2ffinance.yahoo.com%2fquote%2fNVDA/RK=2/RS=xyz", "content": "Quote page."},
    ])
    calls: list[dict[str, object]] = []

    def transport(_method: str, _url: str, _headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        calls.append(json.loads(body))
        return replies.pop(0)

    candidates = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=transport, sleeper=lambda _delay: None).search("nvidia filings", limit=6)

    assert [c.url for c in candidates] == [
        "https://investor.nvidia.com/news",
        "https://www.bea.gov/news/release",
        "https://example.com/real",
        "https://example.com/origin",
        bing_target,
        "https://finance.yahoo.com/quote/NVDA",
    ]
    assert calls[-1]["params"] == {"name": "web_search_prime", "arguments": {"search_query": "nvidia filings", "count": 6}}


def test_glm_mcp_search_keeps_all_opaque_goto_rows_for_runtime_without_poisoning_and_rejects_private_targets() -> None:
    import base64

    opaque_replies = _mcp_replies([
        {"title": "Opaque 1", "link": "https://www.google.com/goto?url=CAESdwHEIkXX&gsessionid=x", "content": "Snippet 1."},
        {"title": "Opaque 2", "link": "https://www.google.com/goto?url=CAESother123&gsessionid=y", "content": "Snippet 2."},
    ])
    provider = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=lambda *_args: opaque_replies.pop(0), sleeper=lambda _delay: None)
    candidates = provider.search("public query", limit=5)

    assert [c.url for c in candidates] == [
        "https://www.google.com/goto?url=CAESdwHEIkXX&gsessionid=x",
        "https://www.google.com/goto?url=CAESother123&gsessionid=y",
    ]

    private_bing = "a1" + base64.urlsafe_b64encode(b"http://127.0.0.1/private").decode("ascii").rstrip("=")
    unsafe_replies = _mcp_replies([
        {"title": "Wrapped private", "link": f"https://www.bing.com/ck/a?u={private_bing}", "content": "Must be dropped."},
        {"title": "Opaque with private fallback", "link": "https://www.google.com/goto?url=CAESopaque", "url": "http://127.0.0.1/secret", "content": "Must be dropped."},
    ])
    unsafe_provider = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=lambda *_args: unsafe_replies.pop(0), sleeper=lambda _delay: None)
    with pytest.raises(ProviderFailure, match="no safely usable"):
        unsafe_provider.search("public query")


def test_glm_mcp_search_parses_iso_timestamps_and_date_fallback_fields() -> None:
    replies = _mcp_replies([
        {"title": "YMD", "link": "https://example.com/1", "content": "One.", "publish_date": "2026-08-01"},
        {"title": "ISO Z", "link": "https://example.com/2", "content": "Two.", "published_at": "2026-08-02T14:30:00Z"},
        {"title": "Date field space TZ", "link": "https://example.com/3", "content": "Three.", "publish_date": "", "date": "2026-08-03 09:15:00+00:00"},
        {"title": "TZ rollover", "link": "https://example.com/4", "content": "Four.", "date": "2026-08-03T23:30:00-05:00"},
        {"title": "Bad trailing text", "link": "https://example.com/5", "content": "Five.", "publish_date": "2026-08-05garbage"},
    ])
    candidates = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=lambda *_args: replies.pop(0), sleeper=lambda _delay: None).search("dates", limit=5)

    assert [(c.published_at, c.independently_reviewable) for c in candidates] == [
        (date(2026, 8, 1), True),
        (date(2026, 8, 2), True),
        (date(2026, 8, 3), True),
        (date(2026, 8, 4), True),
        (None, False),
    ]


_QUOTA_TEXT = 'MCP error -429: {"error":{"code":"1310","message":"您已达到每周/每月使用上限，您的限额将在 2026-11-23 21:28:02 重置。"}}'


def _call_result_transport(call_result: dict[str, object], *, calls: list[bytes] | None = None):
    replies = [
        response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}, session="safe-session-err"),
        HttpResponse(202, ""),
        response({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "web_search_prime", "inputSchema": {"type": "object", "properties": {"search_query": {"type": "string"}}}}]}}),
        response({"jsonrpc": "2.0", "id": 3, "result": call_result}),
    ]

    def transport(_method: str, _url: str, _headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        if calls is not None:
            calls.append(body)
        return replies.pop(0)

    return transport


def _search_failure(call_result: dict[str, object]) -> ProviderFailure:
    provider = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=_call_result_transport(call_result), sleeper=lambda _d: None)
    with pytest.raises(ProviderFailure) as raised:
        provider.search("微软 营收", limit=3)
    return raised.value


def test_mcp_quota_error_text_is_search_quota_exhausted_with_reset_time() -> None:
    failure = _search_failure({"isError": True, "content": [{"type": "text", "text": _QUOTA_TEXT}]})
    assert failure.code == "search_quota_exhausted" and failure.retryable is False
    assert failure.provider_response_received is True
    assert "每周/每月使用上限" in str(failure) and "2026-11-23 21:28:02" in str(failure)
    assert failure.reset_at == "2026-11-23T21:28:02+08:00"
    assert _SECRET not in str(failure)


def test_mcp_quota_error_without_is_error_flag_and_json_error_object() -> None:
    assert _search_failure({"content": [{"type": "text", "text": _QUOTA_TEXT}]}).code == "search_quota_exhausted"
    as_json = json.dumps({"error": {"code": "1310", "message": "weekly usage limit reached, resets 2026-11-23 21:28:02"}})
    failure = _search_failure({"content": [{"type": "text", "text": as_json}]})
    assert failure.code == "search_quota_exhausted" and failure.reset_at == "2026-11-23T21:28:02+08:00"


def test_mcp_transient_429_and_other_errors_are_distinguished() -> None:
    limited = _search_failure({"isError": True, "content": [{"type": "text", "text": 'MCP error -429: {"error":{"code":"1302","message":"Too many requests, slow down"}}'}]})
    assert limited.code == "search_rate_limited" and limited.retryable is True and limited.provider_response_received is True
    other = _search_failure({"isError": True, "content": [{"type": "text", "text": "MCP error -32602: invalid params Bearer abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"}]})
    assert other.code == "search_provider_error" and other.provider_response_received is True
    assert "abcdefghijklmnop" not in str(other) and "invalid params" in str(other)
    flagged = _search_failure({"isError": True, "content": [{"type": "text", "text": "something went wrong"}]})
    assert flagged.code == "search_provider_error"


def test_http_429_with_quota_body_does_not_retry_and_plain_429_is_marked_received() -> None:
    attempts: list[int] = []

    def quota_transport(*_args: object) -> HttpResponse:
        attempts.append(1)
        return HttpResponse(429, json.dumps({"error": {"code": "1310", "message": "usage limit reached, resets 2026-11-23 21:28:02"}}), {})

    provider = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=quota_transport, sleeper=lambda _d: None)
    with pytest.raises(ProviderFailure) as raised:
        provider.search("query")
    assert raised.value.code == "search_quota_exhausted" and len(attempts) == 1

    plain = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=lambda *_a: HttpResponse(429, "", {}), sleeper=lambda _d: None)
    with pytest.raises(ProviderFailure) as raised:
        plain.search("query")
    assert raised.value.code == "search_provider_rate_limited" and raised.value.provider_response_received is True


def test_ordinary_search_payload_is_not_mistaken_for_an_error() -> None:
    payload = json.dumps({"search_result": [{"title": "T", "link": "https://example.com/a", "content": "c", "publish_date": "2026-08-01"}]})
    provider = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=_call_result_transport({"content": [{"type": "text", "text": payload}]}), sleeper=lambda _d: None)
    assert provider.search("q", limit=1)[0].url == "https://example.com/a"


def test_glm_mcp_reuses_session_and_tool_schema_across_searches() -> None:
    calls: list[tuple[str | None, str, dict[str, object]]] = []

    def transport(_method: str, _url: str, headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        payload = json.loads(body)
        rpc_method = payload.get("method")
        calls.append((headers.get("Mcp-Session-Id"), str(rpc_method), payload))
        if rpc_method == "initialize":
            return response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}, session="reuse-sess-1")
        if rpc_method == "notifications/initialized":
            return HttpResponse(202, "")
        if rpc_method == "tools/list":
            return response({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{
                "name": "web_search_prime",
                "inputSchema": {"type": "object", "properties": {"search_query": {"type": "string", "maxLength": 50}, "count": {"type": "integer"}}},
            }]}})
        if rpc_method == "tools/call":
            q = payload["params"]["arguments"]["search_query"]
            return response({"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": json.dumps({
                "search_result": [{"title": f"Hit for {q}", "link": f"https://example.com/{len(calls)}", "content": "Snippet."}]
            })}]}})
        raise AssertionError(f"Unexpected method {rpc_method}")

    provider = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=transport, sleeper=lambda _d: None)
    first = provider.search("alpha", limit=2)
    second = provider.search("beta", limit=3)
    third = provider.search("gamma", limit=1)

    assert [c.title for c in first] == ["Hit for alpha"]
    assert [c.title for c in second] == ["Hit for beta"]
    assert [c.title for c in third] == ["Hit for gamma"]
    assert [c[1] for c in calls] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
        "tools/call",
        "tools/call",
    ]
    assert all(c[0] == "reuse-sess-1" for c in calls[3:])

    # Cached schema maxLength enforcement fires with zero additional HTTP calls.
    before = len(calls)
    with pytest.raises(ProviderFailure, match="provider-declared limit"):
        provider.search("x" * 51, limit=1)
    assert len(calls) == before


@pytest.mark.parametrize("failure_mode", ["http_404", "http_401", "jsonrpc_error", "tool_session_expired"])
def test_glm_mcp_invalidates_and_reinitializes_once_on_expired_session_or_protocol_error(failure_mode: str) -> None:
    calls: list[tuple[str | None, str]] = []
    session_counter = 0

    def transport(_method: str, _url: str, headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        nonlocal session_counter
        payload = json.loads(body)
        rpc_method = str(payload.get("method"))
        sid = headers.get("Mcp-Session-Id")
        calls.append((sid, rpc_method))
        if rpc_method == "initialize":
            session_counter += 1
            return response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}, session=f"sess-{session_counter}")
        if rpc_method == "notifications/initialized":
            return HttpResponse(202, "")
        if rpc_method == "tools/list":
            return response({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{
                "name": "web_search_prime",
                "inputSchema": {"type": "object", "properties": {"search_query": {"type": "string"}, "count": {"type": "integer"}}},
            }]}})
        if rpc_method == "tools/call":
            if sid == "sess-1" and len([c for c in calls if c[1] == "tools/call"]) >= 2:
                if failure_mode == "http_404":
                    return HttpResponse(404, "Session not found", {})
                if failure_mode == "http_401":
                    return HttpResponse(401, "Session unauthorized", {})
                if failure_mode == "jsonrpc_error":
                    return response({"jsonrpc": "2.0", "id": 3, "error": {"code": -32001, "message": "Session expired"}})
                return response({"jsonrpc": "2.0", "id": 3, "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": "MCP error -32001: session expired"}],
                }})
            return response({"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": json.dumps({
                "search_result": [{"title": f"Result on {sid}", "link": "https://example.com/ok", "content": "Content."}]
            })}]}})
        raise AssertionError(f"Unexpected method {rpc_method}")

    provider = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=transport, sleeper=lambda _d: None)
    assert provider.search("first", limit=1)[0].title == "Result on sess-1"
    recovered = provider.search("second", limit=1)
    assert recovered[0].title == "Result on sess-2"
    # Third search reuses sess-2 without another handshake.
    assert provider.search("third", limit=1)[0].title == "Result on sess-2"
    assert [c[1] for c in calls] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call",
        "tools/call",
        "initialize", "notifications/initialized", "tools/list", "tools/call",
        "tools/call",
    ]


def test_glm_mcp_persistent_session_rejection_reinitializes_at_most_once_and_clears_cache() -> None:
    calls: list[tuple[str | None, str]] = []
    session_counter = 0

    def transport(_method: str, _url: str, headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        nonlocal session_counter
        payload = json.loads(body)
        rpc_method = str(payload.get("method"))
        sid = headers.get("Mcp-Session-Id")
        calls.append((sid, rpc_method))
        if rpc_method == "initialize":
            session_counter += 1
            return response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}, session=f"sess-{session_counter}")
        if rpc_method == "notifications/initialized":
            return HttpResponse(202, "")
        if rpc_method == "tools/list":
            return response({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{
                "name": "web_search_prime",
                "inputSchema": {"type": "object", "properties": {"search_query": {"type": "string"}}},
            }]}})
        if rpc_method == "tools/call":
            if len([c for c in calls if c[1] == "tools/call"]) == 1:
                return response({"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": json.dumps({
                    "search_result": [{"title": "Initial", "link": "https://example.com/1", "content": "Ok."}]
                })}]}})
            return HttpResponse(404, "Session expired", {})
        raise AssertionError(f"Unexpected method {rpc_method}")

    provider = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=transport, sleeper=lambda _d: None)
    assert provider.search("ok", limit=1)[0].title == "Initial"
    with pytest.raises(ProviderFailure) as raised:
        provider.search("fails", limit=1)
    assert raised.value.code == "search_provider_rejected"
    assert [c[1] for c in calls] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call",
        "tools/call",
        "initialize", "notifications/initialized", "tools/list", "tools/call",
    ]
    assert provider._session_id is None and provider._tool_cache is None


def test_glm_mcp_concurrent_calls_share_single_handshake_and_single_reinit() -> None:
    from concurrent.futures import ThreadPoolExecutor
    import threading

    lock = threading.Lock()
    init_count = 0
    call_sessions: list[str | None] = []
    expired_flag = False

    def transport(_method: str, _url: str, headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        nonlocal init_count
        payload = json.loads(body)
        rpc_method = str(payload.get("method"))
        sid = headers.get("Mcp-Session-Id")
        if rpc_method == "initialize":
            with lock:
                init_count += 1
                current = init_count
            return response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}, session=f"conc-{current}")
        if rpc_method == "notifications/initialized":
            return HttpResponse(202, "")
        if rpc_method == "tools/list":
            return response({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{
                "name": "web_search_prime",
                "inputSchema": {"type": "object", "properties": {"search_query": {"type": "string"}}},
            }]}})
        if rpc_method == "tools/call":
            with lock:
                call_sessions.append(sid)
            if expired_flag and sid == "conc-1":
                return HttpResponse(404, "expired", {})
            q = payload["params"]["arguments"]["search_query"]
            return response({"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": json.dumps({
                "search_result": [{"title": q, "link": f"https://example.com/{q}", "content": "Body."}]
            })}]}})
        raise AssertionError(f"Unexpected method {rpc_method}")

    provider = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=transport, sleeper=lambda _d: None)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda i: provider.search(f"q{i}", limit=1)[0].title, range(6)))

    assert results == [f"q{i}" for i in range(6)]
    assert init_count == 1
    assert call_sessions == ["conc-1"] * 6

    expired_flag = True
    barrier = threading.Barrier(4, timeout=2.0)

    def run_after_expiry(i: int) -> str:
        barrier.wait()
        return provider.search(f"r{i}", limit=1)[0].title

    with ThreadPoolExecutor(max_workers=4) as pool:
        recovered = list(pool.map(run_after_expiry, range(4)))

    assert recovered == [f"r{i}" for i in range(4)]
    assert init_count == 2


def test_glm_mcp_delegates_to_mcp_client_with_sse_and_cache_counters() -> None:
    def transport(_method: str, _url: str, _headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        payload = json.loads(body)
        rpc_method = payload.get("method")
        if rpc_method == "initialize":
            msg = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}})
            return HttpResponse(200, f"event: message\ndata: {msg}\n\n", {"Content-Type": "text/event-stream", "Mcp-Session-Id": "sse-glm-1"})
        if rpc_method == "notifications/initialized":
            return HttpResponse(202, "")
        if rpc_method == "tools/list":
            msg = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "web_search_prime", "inputSchema": {"type": "object", "properties": {"search_query": {"type": "string"}}}}]}})
            return HttpResponse(200, f"data: {msg}\n\n", {"Content-Type": "text/event-stream"})
        if rpc_method == "tools/call":
            msg = json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": json.dumps({"search_result": [{"title": "SSE Hit", "link": "https://example.com/sse", "content": "Body."}]})}]}})
            return HttpResponse(200, f"data: {msg}\n\n", {"Content-Type": "text/event-stream"})
        raise AssertionError(f"Unexpected method {rpc_method}")

    provider = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=transport, sleeper=lambda _d: None)
    candidates = provider.search("sse test", limit=1)
    assert [c.title for c in candidates] == ["SSE Hit"]
    assert provider.mcp_client.session_cache_misses == 1
    assert provider.mcp_client.session_cache_hits >= 1
    provider.search("sse test 2", limit=1)
    assert provider.mcp_client.session_cache_misses == 1

