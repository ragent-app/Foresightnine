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
