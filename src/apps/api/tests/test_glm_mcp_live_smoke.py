from __future__ import annotations

import json
from typing import Any

import pytest

from app.openai_compatible import HttpResponse
from scripts.glm_mcp_live_smoke import main


TEST_SECRET = "unit-test-only-mcp-live-smoke-secret"


def response(message: dict[str, Any], *, session: str | None = None, status: int = 200) -> HttpResponse:
    headers = {"mcp-session-id": session} if session else {}
    return HttpResponse(status, json.dumps(message), headers)


def configure_secret(monkeypatch: pytest.MonkeyPatch, value: str | None = TEST_SECRET) -> None:
    if value is None:
        monkeypatch.delenv("FORESIGHTNINE_GLM_CODING_API_KEY", raising=False)
    else:
        monkeypatch.setenv("FORESIGHTNINE_GLM_CODING_API_KEY", value)


def test_missing_secret_blocks_without_network(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    configure_secret(monkeypatch, None)

    def no_transport(*_args: object) -> HttpResponse:
        raise AssertionError("network must not be called without the server secret")

    assert main((), transport=no_transport) != 0  # type: ignore[arg-type]
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "no_network_attempted": True,
        "protocol": "MCP Streamable HTTP",
        "status": "blocked",
        "validation_summary": {"result": "secret_missing"},
    }


def test_success_reports_only_redacted_protocol_metadata(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    configure_secret(monkeypatch)
    private_marker = "MUST_NOT_APPEAR_IN_SMOKE_OUTPUT"
    replies = [
        response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}, session="safe-smoke-session"),
        HttpResponse(202, ""),
        response({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "web_search_prime", "inputSchema": {"type": "object", "properties": {"search_query": {"type": "string"}, "count": {"type": "integer"}}}}]}}),
        response({"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": json.dumps(json.dumps([
            {"title": private_marker, "link": "https://example.com/source", "content": "private excerpt", "publish_date": "2026-08-30"},
            {"title": "Context", "link": "https://example.com/context", "content": "context"},
        ]))}]}}),
    ]

    def transport(*_args: object) -> HttpResponse:
        return replies.pop(0)

    assert main((), transport=transport) == 0  # type: ignore[arg-type]
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "all_https": True,
        "candidate_count": 2,
        "dated_count": 1,
        "protocol": "MCP Streamable HTTP",
        "status": "passed",
    }
    assert private_marker not in output
    assert TEST_SECRET not in output


def test_provider_failure_is_classified_without_response_details(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    configure_secret(monkeypatch)
    marker = "UPSTREAM_RESPONSE_MUST_NOT_PRINT"

    def transport(*_args: object) -> HttpResponse:
        return HttpResponse(503, marker)

    assert main((), transport=transport) != 0  # type: ignore[arg-type]
    output = capsys.readouterr().out
    assert json.loads(output)["validation_summary"] == {
        "failure_code": "search_provider_unavailable",
        "result": "provider_failure",
    }
    assert marker not in output
