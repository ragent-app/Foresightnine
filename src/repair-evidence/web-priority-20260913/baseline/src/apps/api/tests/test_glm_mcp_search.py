from __future__ import annotations

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
        provider.search("x" * 71)

    replies = [
        response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}, session="safe-session-3"),
        HttpResponse(202, ""),
        response({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}),
    ]
    provider = GlmCodingMcpSearchEvidenceProvider(_SECRET, transport=lambda *_args: replies.pop(0), sleeper=lambda _delay: None)
    with pytest.raises(ProviderFailure, match="expected search tool"):
        provider.search("public query")


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
