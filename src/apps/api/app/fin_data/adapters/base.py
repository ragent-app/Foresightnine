"""Shared HTTP plumbing for adapters: policy/limiter gates, error mapping, provenance."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Any, BinaryIO, Callable, Mapping, Protocol

from ...openai_compatible import _TransportFailure, _TransportTimeout, _default_binary_transport
from ..models import Cost, Provenance, SuggestedAction, ToolResult


class HttpReply(Protocol):
    status_code: int
    body: bytes
    headers: Mapping[str, str]


Transport = Callable[[str, str, Mapping[str, str], bytes, float], HttpReply]
StreamTransport = Callable[[str, str, Mapping[str, str], bytes, float, BinaryIO], HttpReply]
MAX_BYTES = 5_000_000


def default_stream_transport(max_response_bytes: int | None = None, *, cancel_event=None) -> StreamTransport:
    """Streaming GET into a caller supplied sink, bounded by the large-document cap (not the 5 MB in-memory cap)."""
    from ...research_fetch import large_document_max_bytes
    cap = max_response_bytes or large_document_max_bytes()

    def send(method, url, headers, body, timeout, sink):
        return _default_binary_transport(method, url, headers, body, timeout, max_response_bytes=cap,
                                         sink=sink, cancel_event=cancel_event)
    return send


def default_transport(max_response_bytes: int = MAX_BYTES) -> Transport:
    def send(method, url, headers, body, timeout):
        return _default_binary_transport(method, url, headers, body, timeout, max_response_bytes=max_response_bytes)
    return send


class StopWith(Exception):
    def __init__(self, result: ToolResult) -> None:
        super().__init__(result.reason)
        self.result = result


def header(headers: Mapping[str, str], name: str) -> str:
    for key, value in (headers or {}).items():
        if key.lower() == name.lower():
            return value
    return ""


def retry_after(headers: Mapping[str, str]) -> float | None:
    try:
        return max(0.0, float(header(headers, "retry-after")))
    except ValueError:
        return None


def scrub(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text[:300]


def rate_limited_result(decision, provider: str) -> ToolResult:
    wait = round(decision.wait_seconds, 1)
    reason = "daily_quota_exhausted" if decision.reason == "daily_quota" else "rate_limited"
    return ToolResult.failure("rate_limited", reason,
                              f"{provider} {reason.replace('_', ' ')}; retry after about {wait} seconds.",
                              retryable=True,
                              suggested_actions=[SuggestedAction(action="wait", hint="retry later",
                                                                 retry_after_seconds=wait)])


def request_json(*, provider: str, url: str, headers: Mapping[str, str], transport: Transport, timeout: float,
                 limiter, now_fn: Callable[[], datetime], credits: int | None = None,
                 unknown_on_transport_error: bool = False, secrets: tuple[str, ...] = (),
                 not_found_reason: str = "not_found",
                 limiter_key: str | None = None,
                 stream_transport: StreamTransport | None = None,
                 stream_parser: Callable[[Any], Any] | None = None) -> tuple[Any, Provenance, Cost]:
    """Rate-limit, GET and parse JSON; raise ``StopWith`` carrying the failure envelope."""
    decision = limiter.acquire(limiter_key or provider, 1)
    if not decision.allowed:
        raise StopWith(rate_limited_result(decision, provider))
    cost = Cost(provider_calls=1, credits=credits)
    sink = tempfile.TemporaryFile() if stream_transport is not None else None
    try:
        if sink is not None:
            response = stream_transport("GET", url, {**headers, "Accept": "application/json"}, b"", timeout, sink)
        else:
            response = transport("GET", url, {**headers, "Accept": "application/json"}, b"", timeout)
    except _TransportTimeout:
        if sink is not None:
            sink.close()
        status, retry = ("unknown", False) if unknown_on_transport_error else ("failed", True)
        raise StopWith(ToolResult.failure(status, "source_timeout", f"{provider} request timed out.",
                                          retryable=retry, cost=cost)) from None
    except (_TransportFailure, OSError) as error:
        if sink is not None:
            sink.close()
        if str(error) == "response too large":
            raise StopWith(ToolResult.failure("failed", "source_too_large",
                                              f"{provider} response exceeded the size limit.", cost=cost)) from None
        status, retry = ("unknown", False) if unknown_on_transport_error else ("failed", True)
        raise StopWith(ToolResult.failure(status, "source_unavailable", f"{provider} request failed.",
                                          retryable=retry, cost=cost)) from None
    try:
        return _finish_json(response, sink, provider=provider, url=url, now_fn=now_fn, cost=cost,
                            not_found_reason=not_found_reason, stream_parser=stream_parser)
    finally:
        if sink is not None:
            sink.close()


def _finish_json(response, sink, *, provider, url, now_fn, cost, not_found_reason, stream_parser=None):
    code = response.status_code
    if code == 429:
        wait = retry_after(response.headers) or 60.0
        raise StopWith(ToolResult.failure("rate_limited", "provider_rate_limited",
                                          f"{provider} returned HTTP 429.", retryable=True, cost=cost,
                                          suggested_actions=[SuggestedAction(action="wait", retry_after_seconds=wait)]))
    if code == 404:
        raise StopWith(ToolResult.failure("empty", not_found_reason, f"{provider} has no data at this address.", cost=cost))
    if code in (401, 403):
        raise StopWith(ToolResult.failure("failed", "forbidden", f"{provider} refused the request (HTTP {code}).", cost=cost))
    if not 200 <= code < 300:
        raise StopWith(ToolResult.failure("failed", "source_unavailable", f"{provider} returned HTTP {code}.",
                                          retryable=code >= 500, cost=cost))
    if sink is not None:
        sink.flush()
        sink.seek(0)
        digest = sha256()
        for chunk in iter(lambda: sink.read(1 << 20), b""):
            digest.update(chunk)
        sink.seek(0)
        prov = Provenance(provider=provider, endpoint=url, retrieved_at=now_fn(), response_sha256=digest.hexdigest())
        loader = (lambda: stream_parser(sink)) if stream_parser is not None else (lambda: json.load(sink, parse_float=Decimal))
    else:
        body = response.body
        prov = Provenance(provider=provider, endpoint=url, retrieved_at=now_fn(), response_sha256=sha256(body).hexdigest())
        loader = lambda: json.loads(body, parse_float=Decimal)
    try:
        doc = loader()
    except (ValueError, UnicodeDecodeError):
        raise StopWith(ToolResult.failure("failed", "invalid_response", f"{provider} returned invalid JSON.",
                                          provenance=prov, cost=cost)) from None
    return doc, prov, cost


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
