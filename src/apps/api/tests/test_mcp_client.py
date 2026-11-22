from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import socket
import threading
from time import monotonic

import pytest

from app.mcp_client import (
    McpCallResult,
    McpClientError,
    McpServerConfig,
    McpToolSpec,
    StreamableHttpMcpClient,
)
from app.openai_compatible import HttpResponse, _TransportTimeout


_TOKEN = "unit-test-only-generic-mcp-token"
_CUSTOM_SECRET = "unit-test-only-custom-api-key"
_ENDPOINT = "https://mcp.example.com/v1/mcp"


def _json_response(
    payload: object,
    *,
    session: str | None = None,
    status: int = 200,
    content_type: str = "application/json",
) -> HttpResponse:
    headers: dict[str, str] = {"Content-Type": content_type}
    if session is not None:
        headers["Mcp-Session-Id"] = session
    return HttpResponse(status, json.dumps(payload), headers)


def test_generic_mcp_client_lifecycle_bearer_and_custom_header_auth() -> None:
    calls: list[tuple[str, str, dict[str, str], dict[str, object], float]] = []

    def transport(method: str, url: str, headers: dict[str, str], body: bytes, timeout: float) -> HttpResponse:
        payload = json.loads(body)
        calls.append((method, url, headers, payload, timeout))
        rpc_method = payload.get("method")
        rpc_id = payload.get("id")
        if rpc_method == "initialize":
            return _json_response(
                {"jsonrpc": "2.0", "id": rpc_id, "result": {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}},
                session="generic-sess-1",
            )
        if rpc_method == "notifications/initialized":
            return HttpResponse(202, "", {})
        if rpc_method == "tools/list":
            return _json_response(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "tools": [
                            {
                                "name": "lookup_filing",
                                "description": "Look up public filing metadata.",
                                "inputSchema": {
                                    "type": "object",
                                    "required": ["ticker"],
                                    "properties": {
                                        "year": {"type": "integer", "description": "Fiscal year"},
                                        "ticker": {"type": "string", "minLength": 1},
                                    },
                                },
                            }
                        ]
                    },
                }
            )
        if rpc_method == "tools/call":
            return _json_response(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "content": [{"type": "text", "text": '{"ok": true}'}],
                        "structuredContent": {"ticker": payload["params"]["arguments"]["ticker"]},
                    },
                }
            )
        raise AssertionError(f"Unexpected method {rpc_method}")

    config = McpServerConfig(
        endpoint=_ENDPOINT,
        auth_token=_TOKEN,
        headers={"X-Tenant-Key": _CUSTOM_SECRET},
    )
    client = StreamableHttpMcpClient(config, transport=transport, sleeper=lambda _d: None)

    assert _TOKEN not in repr(config)
    assert _CUSTOM_SECRET not in repr(config)
    assert _TOKEN not in repr(client)
    assert _CUSTOM_SECRET not in repr(client)

    tools = client.list_tools()
    assert len(tools) == 1
    assert isinstance(tools[0], McpToolSpec)
    assert tools[0].name == "lookup_filing"
    assert list(tools[0].input_schema.keys()) == ["properties", "required", "type"]
    assert list(tools[0].input_schema["properties"].keys()) == ["ticker", "year"]
    assert list(tools[0].input_schema["properties"]["year"].keys()) == ["description", "type"]

    result = client.call_tool("lookup_filing", {"year": 2026, "ticker": "NVDA"})
    assert isinstance(result, McpCallResult)
    assert result.untrusted_data is True
    assert result.is_error is False
    assert result.error_metadata == {}
    assert result.structured_content == {"ticker": "NVDA"}
    assert result.content == ({"type": "text", "text": '{"ok": true}'},)

    assert [c[3]["method"] for c in calls] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    assert [c[3].get("id") for c in calls] == [1, None, 2, 3]
    assert all(c[2]["Authorization"] == f"Bearer {_TOKEN}" for c in calls)
    assert all(c[2]["X-Tenant-Key"] == _CUSTOM_SECRET for c in calls)
    assert all(c[2].get("Mcp-Session-Id") == "generic-sess-1" for c in calls[1:])


def test_generic_mcp_client_supports_custom_authorization_header_without_bearer_token() -> None:
    seen_headers: list[dict[str, str]] = []

    def transport(_method: str, _url: str, headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        seen_headers.append(headers)
        payload = json.loads(body)
        rpc_method = payload.get("method")
        rpc_id = payload.get("id")
        if rpc_method == "initialize":
            return _json_response({"jsonrpc": "2.0", "id": rpc_id, "result": {"protocolVersion": "2025-03-26"}})
        if rpc_method == "notifications/initialized":
            return HttpResponse(202, "")
        if rpc_method == "tools/list":
            return _json_response({"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": [{"name": "ping", "inputSchema": {"type": "object"}}]}})
        return _json_response({"jsonrpc": "2.0", "id": rpc_id, "result": {"content": [{"type": "text", "text": "pong"}]}})

    client = StreamableHttpMcpClient(
        McpServerConfig(endpoint=_ENDPOINT, headers={"Authorization": f"ApiKey {_CUSTOM_SECRET}"}),
        transport=transport,
    )
    res = client.call_tool("ping")
    assert res.content == ({"type": "text", "text": "pong"},)
    assert seen_headers[0]["Authorization"] == f"ApiKey {_CUSTOM_SECRET}"


def test_generic_mcp_client_parses_streamable_sse_responses_and_rejects_bad_content() -> None:
    def transport(_method: str, _url: str, _headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        payload = json.loads(body)
        rpc_method = payload.get("method")
        rpc_id = payload.get("id")
        if rpc_method == "initialize":
            pretty_lines = json.dumps(
                {"jsonrpc": "2.0", "id": rpc_id, "result": {"protocolVersion": "2025-03-26"}},
                indent=2,
            ).splitlines()
            data_block = "\r\n".join(f"data: {line}" for line in pretty_lines)
            sse_body = (
                ": stream opened\r\n\r\n"
                "event: ping\r\n"
                "data: keepalive\r\n\r\n"
                "event: message\r\n"
                f"{data_block}\r\n\r\n"
            )
            return HttpResponse(
                200,
                sse_body,
                {"Content-Type": "text/event-stream; charset=utf-8", "Mcp-Session-Id": "sse-sess-1"},
            )
        if rpc_method == "notifications/initialized":
            return HttpResponse(202, "", {})
        if rpc_method == "tools/list":
            notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {}})
            tools_msg = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}})
            sse_body = f"data: {notif}\n\nevent: message\ndata: {tools_msg}\n\n"
            return HttpResponse(200, sse_body, {"Content-Type": "text/event-stream"})
        if rpc_method == "tools/call":
            call_msg = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": {"content": [{"type": "text", "text": "hello sse"}]}})
            return HttpResponse(200, f"data: {call_msg}\n\n", {"Content-Type": "text/event-stream"})
        raise AssertionError(f"Unexpected method {rpc_method}")

    client = StreamableHttpMcpClient(McpServerConfig(endpoint=_ENDPOINT, auth_token=_TOKEN), transport=transport)
    result = client.call_tool("echo", {"msg": "hi"})
    assert result.untrusted_data is True
    assert result.content == ({"type": "text", "text": "hello sse"},)
    assert client.session_id == "sse-sess-1"

    bad_sse_client = StreamableHttpMcpClient(
        McpServerConfig(endpoint=_ENDPOINT, max_attempts=1),
        transport=lambda *_a: HttpResponse(200, "event: message\ndata: {not-json}\n\n", {"Content-Type": "text/event-stream"}),
    )
    with pytest.raises(McpClientError) as err:
        bad_sse_client.list_tools()
    assert err.value.code == "mcp_protocol_error" and err.value.protocol_error is True

    bad_ct_client = StreamableHttpMcpClient(
        McpServerConfig(endpoint=_ENDPOINT, max_attempts=1),
        transport=lambda *_a: HttpResponse(200, "<html>nope</html>", {"Content-Type": "text/html"}),
    )
    with pytest.raises(McpClientError) as ct_err:
        bad_ct_client.list_tools()
    assert ct_err.value.code == "mcp_protocol_error" and ct_err.value.protocol_error is True


def test_generic_mcp_client_session_cache_hits_misses_pagination_and_invalidation() -> None:
    rpc_calls: list[tuple[str | None, str, int | None]] = []

    def transport(_method: str, _url: str, headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        payload = json.loads(body)
        rpc_method = str(payload.get("method"))
        rpc_id = payload.get("id")
        sid = headers.get("Mcp-Session-Id")
        rpc_calls.append((sid, rpc_method, rpc_id))
        if rpc_method == "initialize":
            return _json_response({"jsonrpc": "2.0", "id": rpc_id, "result": {"protocolVersion": "2025-03-26"}}, session=f"s-{len(rpc_calls)}")
        if rpc_method == "notifications/initialized":
            return HttpResponse(202, "")
        if rpc_method == "tools/list":
            cursor = (payload.get("params") or {}).get("cursor")
            if cursor is None:
                return _json_response({
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "tools": [{"name": "tool_a", "inputSchema": {"z": 1, "a": 2}}],
                        "nextCursor": "page-2",
                    },
                })
            return _json_response({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {"tools": [{"name": "tool_b", "inputSchema": {"type": "object"}}]},
            })
        if rpc_method == "tools/call":
            return _json_response(
                {"jsonrpc": "2.0", "id": rpc_id, "result": {"content": [{"type": "text", "text": "ok"}]}},
                session="s-rotated" if rpc_id == 5 else None,
            )
        raise AssertionError(f"Unexpected method {rpc_method}")

    client = StreamableHttpMcpClient(McpServerConfig(endpoint=_ENDPOINT), transport=transport)
    assert client.session_cache_hits == 0 and client.session_cache_misses == 0

    tools = client.list_tools()
    assert [t.name for t in tools] == ["tool_a", "tool_b"]
    assert list(tools[0].input_schema.keys()) == ["a", "z"]
    assert client.session_cache_misses == 1 and client.session_cache_hits == 0

    # Warm list_tools and call_tool reuse cached session without network handshake.
    assert client.list_tools() == tools
    assert client.session_cache_misses == 1 and client.session_cache_hits == 1

    client.call_tool("tool_a")
    client.call_tool("tool_b")
    assert client.session_cache_misses == 1 and client.session_cache_hits == 3
    assert client.session_id == "s-rotated"

    # Unknown tool fails locally from cached catalog with zero extra HTTP calls.
    before = len(rpc_calls)
    with pytest.raises(McpClientError) as missing_err:
        client.call_tool("nonexistent_tool")
    assert missing_err.value.code == "mcp_tool_not_found"
    assert len(rpc_calls) == before

    # Explicit invalidation clears cache and forces a miss on next call.
    client.invalidate_session()
    assert client.session_id is None and client.cached_tools is None
    client.call_tool("tool_a")
    assert client.session_cache_misses == 2


@pytest.mark.parametrize("mode", ["401", "404", "410", "body_session_expired", "jsonrpc_error", "tool_is_error"])
def test_generic_mcp_client_single_reconnect_on_expired_session(mode: str) -> None:
    calls: list[tuple[str | None, str]] = []
    session_counter = 0

    def transport(_method: str, _url: str, headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        nonlocal session_counter
        payload = json.loads(body)
        rpc_method = str(payload.get("method"))
        rpc_id = payload.get("id")
        sid = headers.get("Mcp-Session-Id")
        calls.append((sid, rpc_method))
        if rpc_method == "initialize":
            session_counter += 1
            return _json_response(
                {"jsonrpc": "2.0", "id": rpc_id, "result": {"protocolVersion": "2025-03-26"}},
                session=f"sess-{session_counter}",
            )
        if rpc_method == "notifications/initialized":
            return HttpResponse(202, "")
        if rpc_method == "tools/list":
            return _json_response({"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": [{"name": "ping", "inputSchema": {"type": "object"}}]}})
        if rpc_method == "tools/call":
            if sid == "sess-1" and sum(1 for _, m in calls if m == "tools/call") >= 2:
                if mode in ("401", "404", "410"):
                    return HttpResponse(int(mode), "expired", {})
                if mode == "body_session_expired":
                    return HttpResponse(400, "Mcp-Session-Id expired or unknown", {})
                if mode == "jsonrpc_error":
                    return _json_response({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32001, "message": "Session expired"}})
                return _json_response({
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {"isError": True, "content": [{"type": "text", "text": "MCP error -32001: session expired"}]},
                })
            return _json_response({"jsonrpc": "2.0", "id": rpc_id, "result": {"content": [{"type": "text", "text": f"ok:{sid}"}]}})
        raise AssertionError(f"Unexpected method {rpc_method}")

    client = StreamableHttpMcpClient(McpServerConfig(endpoint=_ENDPOINT, max_attempts=1), transport=transport)
    assert client.call_tool("ping").content[0]["text"] == "ok:sess-1"
    recovered = client.call_tool("ping")
    assert recovered.content[0]["text"] == "ok:sess-2"
    assert client.call_tool("ping").content[0]["text"] == "ok:sess-2"
    assert [m for _, m in calls] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call",
        "tools/call",
        "initialize", "notifications/initialized", "tools/list", "tools/call",
        "tools/call",
    ]


def test_generic_mcp_client_persistent_expiry_reconnects_once_and_concurrent_threads_share_reinit() -> None:
    lock = threading.Lock()
    init_count = 0
    expired_flag = False

    def transport(_method: str, _url: str, headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        nonlocal init_count
        payload = json.loads(body)
        rpc_method = str(payload.get("method"))
        rpc_id = payload.get("id")
        sid = headers.get("Mcp-Session-Id")
        if rpc_method == "initialize":
            with lock:
                init_count += 1
                current = init_count
            return _json_response({"jsonrpc": "2.0", "id": rpc_id, "result": {"protocolVersion": "2025-03-26"}}, session=f"conc-{current}")
        if rpc_method == "notifications/initialized":
            return HttpResponse(202, "")
        if rpc_method == "tools/list":
            return _json_response({"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}})
        if rpc_method == "tools/call":
            if expired_flag and sid == "conc-1":
                return HttpResponse(410, "Session gone", {})
            val = payload["params"]["arguments"]["v"]
            return _json_response({"jsonrpc": "2.0", "id": rpc_id, "result": {"content": [{"type": "text", "text": f"{sid}:{val}"}]}})
        raise AssertionError(f"Unexpected method {rpc_method}")

    client = StreamableHttpMcpClient(McpServerConfig(endpoint=_ENDPOINT, max_attempts=1), transport=transport)
    with ThreadPoolExecutor(max_workers=6) as pool:
        first_batch = list(pool.map(lambda i: client.call_tool("echo", {"v": i}).content[0]["text"], range(6)))
    assert first_batch == [f"conc-1:{i}" for i in range(6)]
    assert init_count == 1

    expired_flag = True
    barrier = threading.Barrier(4, timeout=2.0)

    def after_expiry(i: int) -> str:
        barrier.wait()
        return client.call_tool("echo", {"v": i}).content[0]["text"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        recovered = list(pool.map(after_expiry, range(4)))
    assert recovered == [f"conc-2:{i}" for i in range(4)]
    assert init_count == 2

    # Persistent failure clears cache and fails after at most one reconnect.
    always_404 = StreamableHttpMcpClient(
        McpServerConfig(endpoint=_ENDPOINT, max_attempts=1),
        transport=lambda _m, _u, _h, body, _t: (
            _json_response({"jsonrpc": "2.0", "id": json.loads(body).get("id"), "result": {"protocolVersion": "2025-03-26"}}, session="s1")
            if json.loads(body).get("method") == "initialize"
            else HttpResponse(202, "")
            if json.loads(body).get("method") == "notifications/initialized"
            else _json_response({"jsonrpc": "2.0", "id": json.loads(body).get("id"), "result": {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}})
            if json.loads(body).get("method") == "tools/list"
            else HttpResponse(404, "missing", {})
        ),
    )
    with pytest.raises(McpClientError) as err:
        always_404.call_tool("echo")
    assert err.value.code == "mcp_rejected"
    assert always_404.session_id is None and always_404.cached_tools is None


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://mcp.example.com/mcp",
        "ftp://mcp.example.com/mcp",
        "https://localhost/mcp",
        "https://127.0.0.1/mcp",
        "https://10.0.0.1/mcp",
        "https://169.254.169.254/latest/meta-data",
        "https://metadata.google.internal/mcp",
        "https://service.internal/mcp",
        "https://[::1]/mcp",
        "https://[64:ff9b::127.0.0.1]/mcp",
        "https://0x7f.1/mcp",
        "https://mcp.example.com:8443/mcp",
        "https://user:pass@mcp.example.com/mcp",
        "https://mcp.example.com/mcp#frag",
        "https://fred.stlouisfed.org/mcp",
        "   ",
    ],
)
def test_generic_mcp_client_rejects_ssrf_and_non_https_urls(bad_url: str) -> None:
    with pytest.raises(McpClientError) as err:
        McpServerConfig(endpoint=bad_url)
    assert err.value.code == "mcp_url_rejected"
    assert err.value.retryable is False


def test_default_transport_rejects_private_dns_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, port: int, *_args: object, **_kwargs: object):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    client = StreamableHttpMcpClient(McpServerConfig(endpoint=_ENDPOINT, max_attempts=1))
    with pytest.raises(McpClientError) as err:
        client.list_tools()
    assert err.value.code == "mcp_unavailable"


def test_generic_mcp_client_bounds_response_size_deadlines_retries_and_sanitizes_errors() -> None:
    # Oversized response is rejected.
    oversized = StreamableHttpMcpClient(
        McpServerConfig(endpoint=_ENDPOINT, max_response_bytes=64, max_attempts=1),
        transport=lambda *_a: HttpResponse(200, "x" * 128, {"Content-Type": "application/json"}),
    )
    with pytest.raises(McpClientError) as size_err:
        oversized.list_tools()
    assert size_err.value.code == "mcp_protocol_error"
    assert size_err.value.error_metadata["kind"] == "response_oversized"

    # Expired deadline and cancel_event never call transport.
    called = 0

    def never_called(*_a: object) -> HttpResponse:
        nonlocal called
        called += 1
        raise AssertionError("transport must not run")

    budget_client = StreamableHttpMcpClient(McpServerConfig(endpoint=_ENDPOINT), transport=never_called)
    with pytest.raises(McpClientError) as deadline_err:
        budget_client.list_tools(deadline=monotonic() - 1)
    assert deadline_err.value.code == "mcp_timeout"
    cancel_ev = threading.Event()
    cancel_ev.set()
    with pytest.raises(McpClientError) as cancel_err:
        budget_client.list_tools(cancel_event=cancel_ev)
    assert cancel_err.value.code == "mcp_cancelled"
    assert called == 0

    # Transient timeout retries up to max_attempts then reports mcp_timeout.
    timeout_attempts = 0

    def timeout_transport(*_a: object) -> HttpResponse:
        nonlocal timeout_attempts
        timeout_attempts += 1
        raise _TransportTimeout("slow")

    retry_client = StreamableHttpMcpClient(
        McpServerConfig(endpoint=_ENDPOINT, max_attempts=2),
        transport=timeout_transport,
        sleeper=lambda _d: None,
    )
    with pytest.raises(McpClientError) as timeout_err:
        retry_client.list_tools()
    assert timeout_err.value.code == "mcp_timeout"
    assert timeout_attempts == 2

    # Sanitized content-free error metadata never leaks upstream bodies or secrets.
    secret_marker = "sk-secret-value-must-never-appear"
    reject_client = StreamableHttpMcpClient(
        McpServerConfig(endpoint=_ENDPOINT, auth_token=secret_marker, max_attempts=1),
        transport=lambda *_a: HttpResponse(403, f"upstream secret body {secret_marker}", {}),
    )
    with pytest.raises(McpClientError) as rej_err:
        reject_client.list_tools()
    assert rej_err.value.code == "mcp_rejected"
    assert secret_marker not in str(rej_err.value)
    assert secret_marker not in json.dumps(dict(rej_err.value.error_metadata))
    assert rej_err.value.error_metadata["http_status"] == 403
    assert rej_err.value.error_metadata["kind"] == "http_rejected"

    # Tool isError=True produces untrusted_data=True and content-free error_metadata.
    tool_err_client = StreamableHttpMcpClient(
        McpServerConfig(endpoint=_ENDPOINT, auth_token=secret_marker),
        transport=lambda _m, _u, _h, body, _t: (
            _json_response({"jsonrpc": "2.0", "id": json.loads(body).get("id"), "result": {"protocolVersion": "2025-03-26"}})
            if json.loads(body).get("method") == "initialize"
            else HttpResponse(202, "")
            if json.loads(body).get("method") == "notifications/initialized"
            else _json_response({"jsonrpc": "2.0", "id": json.loads(body).get("id"), "result": {"tools": [{"name": "search", "inputSchema": {"type": "object"}}]}})
            if json.loads(body).get("method") == "tools/list"
            else _json_response({
                "jsonrpc": "2.0",
                "id": json.loads(body).get("id"),
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": f'MCP error -429: {{"error":{{"code":"1302","message":"rate limit {secret_marker}"}}}}'}],
                },
            })
        ),
    )
    tool_res = tool_err_client.call_tool("search")
    assert tool_res.untrusted_data is True
    assert tool_res.is_error is True
    assert tool_res.error_metadata["is_error"] is True
    assert tool_res.error_metadata["error_kind"] == "rate_limited"
    assert tool_res.error_metadata["rpc_code"] == -429
    assert tool_res.error_metadata["provider_code"] == "1302"
    assert secret_marker not in json.dumps(dict(tool_res.error_metadata))

    with pytest.raises(ValueError, match="untrusted_data=True"):
        McpCallResult(tool_name="search", content=(), untrusted_data=False)
