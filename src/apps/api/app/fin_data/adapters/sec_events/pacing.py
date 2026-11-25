"""SEC shared waitable pacing limiter (4.5 req/s), unified GET helper, and raw submissions reader.

Design invariants:
1. Never calls or falls back to ``base.py``'s non-waiting ``InProcessRateLimiter`` burst error.
2. Supports ``cancel_event``, monotonic ``deadline``, HTTP ``429`` ``Retry-After`` backoff,
   optional Redis distributed slot coordination (via ``FORESIGHTNINE_REDIS_URL`` / ``REDIS_URL``),
   and short-transaction PostgreSQL advisory lock fallback (never reusing ambient long business tx).
3. Reads raw SEC ``submissions`` JSON preserving the ``items`` array and supports on-demand
   ``filings.files`` pagination for historical lookbacks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import os
import re
import tempfile
from threading import Event, Lock
from time import monotonic, sleep
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from ....openai_compatible import _TransportFailure, _TransportTimeout
from ....research_sec import SecFailure, validate_user_agent
from ....storage import DOCUMENT_TABLE, StorageRuntimeError, _default_connection_factory
from ...policy import Denied, FinDataPolicy, load_policy
from ..base import StreamTransport, Transport, default_stream_transport, default_transport, header as http_header, retry_after
from ..sec_deep import SEC_ALLOWED_HOSTS, _decode_bytes, _normalize_items, _parse_date, _parse_dt, normalize_accession
from ..sec_edgar import PROVIDER, _CappedSink, cik10

DEFAULT_SEC_PACING_RATE_PER_SEC = 4.5
DEFAULT_SEC_PACING_BURST = 4.5
DEFAULT_MAX_SUBMISSIONS_BYTES = 25_000_000
DEFAULT_MAX_429_RETRIES = 3
DEFAULT_429_RETRY_AFTER_SECONDS = 5.0
MAX_429_RETRY_AFTER_SECONDS = 30.0

_REDIS_PACING_KEY = "foresightnine:sec_events:pacing_next_us"
_REDIS_PACING_SCRIPT = """
local clock = redis.call('TIME')
local now_us = tonumber(clock[1]) * 1000000 + tonumber(clock[2])
local interval_us = tonumber(ARGV[1])
local burst_us = tonumber(ARGV[2])
local cooldown_us = tonumber(ARGV[3])
local min_next = now_us - burst_us
local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
if cur < min_next then cur = min_next end
if cooldown_us > cur then cur = cooldown_us end
local wait_us = cur - now_us
if wait_us > 0 then
  return {0, wait_us}
end
local next_ts = cur + interval_us
redis.call('SET', KEYS[1], tostring(next_ts), 'PX', 60000)
return {1, 0}
"""


class SecPacingError(RuntimeError):
    """Structured error raised by V1 SEC HTTP / pacing layer."""

    def __init__(
        self,
        reason: str,
        message: str = "",
        *,
        status_code: int | None = None,
        retryable: bool = True,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message or reason)
        self.reason = reason
        self.message = message or reason
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


@dataclass
class PacingTelemetry:
    requests_sent: int = 0
    pacing_waits: int = 0
    pacing_wait_ms: int = 0
    http_429_count: int = 0
    http_429_wait_ms: int = 0


def _wait_with_cancel(delay: float, cancel_event: Event | None, sleep_fn: Callable[[float], None]) -> bool:
    """Sleep for ``delay`` seconds; return False if cancelled during wait."""
    if delay <= 0:
        return not (cancel_event is not None and cancel_event.is_set())
    if cancel_event is not None:
        if cancel_event.is_set():
            return False
        return not cancel_event.wait(delay)
    sleep_fn(delay)
    return True


def _validate_sec_https_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or (parts.hostname or "").lower() not in SEC_ALLOWED_HOSTS:
        raise SecPacingError("invalid_sec_url", f"Only official SEC HTTPS hosts are allowed: {url}", retryable=False)
    return url


def _parse_retry_after_seconds(headers: Mapping[str, str], default: float = DEFAULT_429_RETRY_AFTER_SECONDS) -> float:
    parsed = retry_after(headers)
    if parsed is None:
        return default
    return min(MAX_429_RETRY_AFTER_SECONDS, max(0.25, parsed))


class SecPacingLimiter:
    """Waitable 4.5 req/s SEC pacing limiter (in-process + optional Redis or short-tx Postgres)."""

    def __init__(
        self,
        *,
        rate_per_sec: float = DEFAULT_SEC_PACING_RATE_PER_SEC,
        burst: float = DEFAULT_SEC_PACING_BURST,
        clock: Callable[[], float] = monotonic,
        sleep_fn: Callable[[float], None] = sleep,
        redis_client: Any | None = None,
        postgres_dsn: str | None = None,
        postgres_connection_factory: Callable[[str], Any] | None = None,
    ) -> None:
        if rate_per_sec <= 0 or rate_per_sec > 9.5:
            raise ValueError("SEC pacing rate_per_sec must be in (0, 9.5] to stay safely below 10 req/s.")
        if burst < 1.0:
            raise ValueError("SEC pacing burst must be >= 1.0.")
        self.rate_per_sec = float(rate_per_sec)
        self.burst = float(burst)
        self._clock = clock
        self._sleep_fn = sleep_fn
        self._lock = Lock()
        self._tokens = float(burst)
        self._last_ts = self._clock()
        self._cooldown_until = 0.0
        self._redis_client = redis_client
        self._postgres_dsn = postgres_dsn.strip() if isinstance(postgres_dsn, str) and postgres_dsn.strip() else None
        self._pg_conn_factory = postgres_connection_factory or _default_connection_factory
        self._pg_next_allowed_epoch: float = 0.0

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        rate_per_sec: float = DEFAULT_SEC_PACING_RATE_PER_SEC,
        burst: float = DEFAULT_SEC_PACING_BURST,
        redis_client: Any | None = None,
        postgres_connection_factory: Callable[[str], Any] | None = None,
    ) -> "SecPacingLimiter":
        env = os.environ if environ is None else environ
        client = redis_client
        if client is None:
            redis_url = (env.get("FORESIGHTNINE_REDIS_URL") or env.get("REDIS_URL") or "").strip()
            if redis_url:
                try:
                    import redis  # type: ignore[import-untyped]

                    client = redis.Redis.from_url(
                        redis_url,
                        socket_connect_timeout=1.0,
                        socket_timeout=1.0,
                        health_check_interval=30,
                    )
                except Exception:
                    client = None
        dsn = (env.get("FORESIGHTNINE_POSTGRES_DSN") or "").strip() or None
        mode = (env.get("FORESIGHTNINE_STORAGE_MODE") or "").strip().lower()
        pg_dsn = dsn if (mode == "postgres" or postgres_connection_factory is not None) and dsn else None
        return cls(
            rate_per_sec=rate_per_sec,
            burst=burst,
            redis_client=client,
            postgres_dsn=pg_dsn,
            postgres_connection_factory=postgres_connection_factory,
        )

    def notify_rate_limited(self, retry_after_seconds: float) -> None:
        """Apply a shared cooldown window when SEC responds with HTTP 429."""
        delay = min(MAX_429_RETRY_AFTER_SECONDS, max(0.1, float(retry_after_seconds)))
        with self._lock:
            target = self._clock() + delay
            if target > self._cooldown_until:
                self._cooldown_until = target
            self._tokens = 0.0
            self._last_ts = self._clock()

    def _try_acquire_in_process(self) -> float:
        with self._lock:
            now = self._clock()
            if now < self._cooldown_until:
                return max(0.01, self._cooldown_until - now)
            elapsed = max(0.0, now - self._last_ts)
            self._tokens = min(self.burst, self._tokens + elapsed * self.rate_per_sec)
            self._last_ts = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            needed = (1.0 - self._tokens) / self.rate_per_sec
            return max(0.01, needed)

    def _try_acquire_redis(self) -> float | None:
        if self._redis_client is None:
            return None
        interval_us = int(round(1_000_000 / self.rate_per_sec))
        burst_us = int(round(((self.burst - 1.0) / self.rate_per_sec) * 1_000_000))
        with self._lock:
            cooldown_rem = max(0.0, self._cooldown_until - self._clock())
        if cooldown_rem > 0:
            return cooldown_rem
        try:
            res = self._redis_client.eval(
                _REDIS_PACING_SCRIPT,
                1,
                _REDIS_PACING_KEY,
                str(interval_us),
                str(max(0, burst_us)),
                "0",
            )
            if isinstance(res, (list, tuple)) and len(res) == 2:
                allowed = int(res[0]) == 1
                wait_us = max(0, int(res[1]))
                return 0.0 if allowed else max(0.01, wait_us / 1_000_000.0)
        except Exception:
            # Never expose Redis URL or block local pacing if optional Redis is transiently unavailable
            return None
        return None

    def _try_acquire_postgres_short_tx(self) -> float | None:
        """Reserve a global slot using an independent short-lived Postgres advisory lock tx.

        Explicitly opens its own connection outside any ambient ``postgres_unit_of_work`` so
        long SEC/LLM work NEVER holds an open transaction or advisory lock.
        """
        if not self._postgres_dsn:
            return None
        interval = 1.0 / self.rate_per_sec
        try:
            with self._pg_conn_factory(self._postgres_dsn) as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("sec_events:global_pacing",))
                    cursor.execute(
                        f"SELECT document->>'next_allowed_at' FROM {DOCUMENT_TABLE} "
                        "WHERE collection = %s AND document_key = %s",
                        ("sec_events_pacing", "global"),
                    )
                    row = cursor.fetchone()
                    cursor.execute("SELECT EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)")
                    now_row = cursor.fetchone()
                    now_epoch = float(now_row[0]) if now_row else datetime.now(timezone.utc).timestamp()
                    next_epoch = now_epoch
                    if row and row[0]:
                        try:
                            next_epoch = max(now_epoch - ((self.burst - 1.0) * interval), float(row[0]))
                        except (TypeError, ValueError):
                            next_epoch = now_epoch
                    if next_epoch > now_epoch:
                        return max(0.01, next_epoch - now_epoch)
                    updated_next = max(now_epoch, next_epoch) + interval
                    payload = json.dumps({"next_allowed_at": str(updated_next)})
                    cursor.execute(
                        f"INSERT INTO {DOCUMENT_TABLE} "
                        "(collection, document_key, document, expires_at, created_at, updated_at, version) "
                        "VALUES (%s, %s, %s::jsonb, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1) "
                        "ON CONFLICT (collection, document_key) DO UPDATE "
                        f"SET document = EXCLUDED.document, updated_at = CURRENT_TIMESTAMP, version = {DOCUMENT_TABLE}.version + 1",
                        ("sec_events_pacing", "global", payload),
                    )
                    return 0.0
        except (StorageRuntimeError, Exception):
            return None

    def _peek_in_process_cooldown(self) -> float:
        with self._lock:
            now = self._clock()
            if now < self._cooldown_until:
                return max(0.01, self._cooldown_until - now)
            return 0.0

    def acquire(
        self,
        *,
        cancel_event: Event | None = None,
        deadline: float | None = None,
        telemetry: PacingTelemetry | None = None,
    ) -> None:
        """Wait until one SEC request slot is available, or raise ``SecPacingError`` on cancel/deadline."""
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise SecPacingError("cancelled", "SEC request cancelled before pacing.", retryable=False)

            wait_s = self._peek_in_process_cooldown()
            if wait_s == 0.0:
                dist_wait: float | None = None
                if self._redis_client is not None:
                    dist_wait = self._try_acquire_redis()
                if dist_wait is None and self._postgres_dsn is not None:
                    dist_wait = self._try_acquire_postgres_short_tx()
                if dist_wait is not None:
                    wait_s = dist_wait
                else:
                    wait_s = self._try_acquire_in_process()

            if wait_s <= 0.0:
                return

            now = self._clock()
            if deadline is not None and (now + wait_s) > deadline:
                raise SecPacingError(
                    "deadline_exceeded",
                    f"SEC pacing wait ({wait_s:.2f}s) would exceed run deadline.",
                    retryable=True,
                    retry_after_seconds=wait_s,
                )
            if telemetry is not None:
                telemetry.pacing_waits += 1
                telemetry.pacing_wait_ms += int(round(wait_s * 1000))

            if not _wait_with_cancel(wait_s, cancel_event, self._sleep_fn):
                raise SecPacingError("cancelled", "SEC request cancelled while waiting for pacing slot.", retryable=False)


def sec_get_bytes(
    url: str,
    *,
    category: str = "filings",
    user_agent: str | None,
    limiter: SecPacingLimiter | None = None,
    policy: FinDataPolicy | None = None,
    transport: Transport | None = None,
    stream_transport: StreamTransport | None = None,
    accept: str = "application/json, text/html, application/xml, text/plain",
    timeout: float = 15.0,
    max_bytes: int = 10_000_000,
    large: bool = False,
    cancel_event: Event | None = None,
    deadline: float | None = None,
    max_429_retries: int = DEFAULT_MAX_429_RETRIES,
    telemetry: PacingTelemetry | None = None,
    clock: Callable[[], float] = monotonic,
    sleep_fn: Callable[[float], None] = sleep,
) -> tuple[bytes, str, str]:
    """Unified V1 SEC GET helper with waitable pacing and HTTP 429 Retry-After backoff.

    Returns ``(raw_bytes, content_type, response_sha256)``.
    Never delegates to non-waiting ``fin_data/adapters/base.py`` burst errors.
    """
    _validate_sec_https_url(url)
    active_policy = policy or load_policy()
    decision = active_policy.decide(PROVIDER, category)
    if isinstance(decision, Denied):
        raise SecPacingError(decision.reason, decision.message or f"Denied by policy for {category}", retryable=False)
    try:
        agent = validate_user_agent(user_agent)
    except SecFailure as err:
        raise SecPacingError(err.code, "SEC requires a valid declared User-Agent with contact email.", retryable=False) from None

    active_limiter = limiter or SecPacingLimiter()
    send_plain = transport or default_transport(max_response_bytes=min(max_bytes, 5_000_000))
    send_stream = stream_transport if stream_transport is not None else (
        default_stream_transport(max_response_bytes=max_bytes, cancel_event=cancel_event) if (large and transport is None) else None
    )
    headers = {"User-Agent": agent, "Accept": accept}

    attempts = max(0, int(max_429_retries)) + 1
    for attempt in range(attempts):
        if cancel_event is not None and cancel_event.is_set():
            raise SecPacingError("cancelled", "SEC request cancelled.", retryable=False)
        if deadline is not None:
            rem = deadline - clock()
            if rem <= 0:
                raise SecPacingError("deadline_exceeded", "SEC request exceeded deadline.", retryable=True)
            req_timeout = min(timeout, max(0.1, rem))
        else:
            req_timeout = timeout

        active_limiter.acquire(cancel_event=cancel_event, deadline=deadline, telemetry=telemetry)
        if telemetry is not None:
            telemetry.requests_sent += 1

        try:
            if send_stream is not None:
                with tempfile.TemporaryFile() as sink:
                    capped = _CappedSink(sink, max_bytes)
                    reply = send_stream("GET", url, headers, b"", req_timeout, capped)
                    sink.flush()
                    sink.seek(0)
                    raw_body = sink.read()
            else:
                reply = send_plain("GET", url, headers, b"", req_timeout)
                raw_raw = reply.body
                raw_body = raw_raw.encode("utf-8") if isinstance(raw_raw, str) else bytes(raw_raw or b"")
                if len(raw_body) > max_bytes:
                    raise _TransportFailure("response too large")
        except _TransportTimeout:
            raise SecPacingError("source_timeout", f"SEC request timed out for {url}", retryable=True) from None
        except (_TransportFailure, OSError) as err:
            if str(err) == "response too large":
                raise SecPacingError(
                    "source_too_large",
                    f"SEC response exceeded {max_bytes} byte cap for {url}",
                    retryable=False,
                ) from None
            raise SecPacingError("source_unavailable", f"SEC transport failed for {url}", retryable=True) from None

        code = int(reply.status_code)
        if code == 429:
            wait_s = _parse_retry_after_seconds(reply.headers)
            active_limiter.notify_rate_limited(wait_s)
            if telemetry is not None:
                telemetry.http_429_count += 1
            if attempt >= attempts - 1:
                raise SecPacingError(
                    "provider_rate_limited",
                    f"SEC returned HTTP 429 after {attempts} attempts.",
                    status_code=429,
                    retryable=True,
                    retry_after_seconds=wait_s,
                )
            if deadline is not None and (clock() + wait_s) > deadline:
                raise SecPacingError(
                    "deadline_exceeded",
                    f"SEC 429 Retry-After ({wait_s:.2f}s) exceeds deadline.",
                    status_code=429,
                    retryable=True,
                    retry_after_seconds=wait_s,
                )
            if telemetry is not None:
                telemetry.http_429_wait_ms += int(round(wait_s * 1000))
            if not _wait_with_cancel(wait_s, cancel_event, sleep_fn):
                raise SecPacingError("cancelled", "SEC request cancelled during 429 Retry-After backoff.", retryable=False)
            continue

        if code == 404:
            raise SecPacingError("not_found", f"SEC returned HTTP 404 for {url}", status_code=404, retryable=False)
        if code in (401, 403):
            raise SecPacingError("forbidden", f"SEC refused request (HTTP {code}) for {url}", status_code=code, retryable=False)
        if not 200 <= code < 300:
            raise SecPacingError(
                "source_unavailable",
                f"SEC returned HTTP {code} for {url}",
                status_code=code,
                retryable=code >= 500,
            )

        content_type = http_header(reply.headers, "content-type")
        digest = sha256(raw_body).hexdigest()
        return raw_body, content_type, digest

    raise SecPacingError("provider_rate_limited", "SEC HTTP 429 retry budget exhausted.", status_code=429, retryable=True)


def sec_get_to_tempfile(
    url: str,
    *,
    accept: str = "application/json",
    category: str = "filings",
    user_agent: str,
    limiter: SecPacingLimiter | None = None,
    policy: Any = None,
    transport: Any = None,
    stream_transport: Any = None,
    max_bytes: int = 60_000_000,
    timeout: float = 60.0,
    cancel_event: Any = None,
    deadline: float | None = None,
    telemetry: PacingTelemetry | None = None,
    max_429_retries: int = 3,
    clock: Callable[[], float] = monotonic,
    sleep_fn: Callable[[float], None] = sleep,
    suffix: str = ".dat",
) -> tuple[Path, str, str]:
    """Stream an SEC response directly to a temporary file on disk using waitable pacing.

    Avoids holding 60MB ``bytes`` + decoded ``str`` simultaneously in RAM on 512M containers.
    Caller is responsible for unlinking the returned ``Path``.
    Returns ``(temp_path, content_type, sha256_hex)``.
    """
    from pathlib import Path

    active_policy = policy or load_policy()
    decision = active_policy.decide(PROVIDER, category)
    if isinstance(decision, Denied):
        raise SecPacingError(decision.reason, decision.message or f"Denied by policy for {category}", retryable=False)
    try:
        agent = validate_user_agent(user_agent)
    except SecFailure as err:
        raise SecPacingError(err.code, "SEC requires a valid declared User-Agent with contact email.", retryable=False) from None

    active_limiter = limiter or SecPacingLimiter()
    send_plain = transport or default_transport(max_response_bytes=min(max_bytes, 5_000_000))
    send_stream = stream_transport if stream_transport is not None else (
        default_stream_transport(max_response_bytes=max_bytes, cancel_event=cancel_event) if transport is None else None
    )
    headers = {"User-Agent": agent, "Accept": accept}
    attempts = max(0, int(max_429_retries)) + 1

    for attempt in range(attempts):
        if cancel_event is not None and cancel_event.is_set():
            raise SecPacingError("cancelled", "SEC request cancelled.", retryable=False)
        if deadline is not None:
            rem = deadline - clock()
            if rem <= 0:
                raise SecPacingError("deadline_exceeded", "SEC request exceeded deadline.", retryable=True)
            req_timeout = min(timeout, max(0.1, rem))
        else:
            req_timeout = timeout

        active_limiter.acquire(cancel_event=cancel_event, deadline=deadline, telemetry=telemetry)
        if telemetry is not None:
            telemetry.requests_sent += 1

        fd, tmp_name = tempfile.mkstemp(prefix="foresight-sec-", suffix=suffix)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as sink:
                if send_stream is not None:
                    capped = _CappedSink(sink, max_bytes)
                    reply = send_stream("GET", url, headers, b"", req_timeout, capped)
                else:
                    reply = send_plain("GET", url, headers, b"", req_timeout)
                    raw_raw = reply.body
                    raw_body = raw_raw.encode("utf-8") if isinstance(raw_raw, str) else bytes(raw_raw or b"")
                    if len(raw_body) > max_bytes:
                        raise _TransportFailure("response too large")
                    sink.write(raw_body)
                sink.flush()
        except _TransportTimeout:
            tmp_path.unlink(missing_ok=True)
            raise SecPacingError("source_timeout", f"SEC request timed out for {url}", retryable=True) from None
        except (_TransportFailure, OSError) as err:
            tmp_path.unlink(missing_ok=True)
            if str(err) == "response too large":
                raise SecPacingError(
                    "source_too_large",
                    f"SEC response exceeded {max_bytes} byte cap for {url}",
                    retryable=False,
                ) from None
            raise SecPacingError("source_unavailable", f"SEC transport failed for {url}", retryable=True) from None

        code = int(reply.status_code)
        if code == 429:
            tmp_path.unlink(missing_ok=True)
            wait_s = _parse_retry_after_seconds(reply.headers)
            active_limiter.notify_rate_limited(wait_s)
            if telemetry is not None:
                telemetry.http_429_count += 1
            if attempt >= attempts - 1:
                raise SecPacingError(
                    "provider_rate_limited",
                    f"SEC returned HTTP 429 after {attempts} attempts.",
                    status_code=429,
                    retryable=True,
                    retry_after_seconds=wait_s,
                )
            if deadline is not None and (clock() + wait_s) > deadline:
                raise SecPacingError(
                    "deadline_exceeded",
                    f"SEC 429 Retry-After ({wait_s:.2f}s) exceeds deadline.",
                    status_code=429,
                    retryable=True,
                    retry_after_seconds=wait_s,
                )
            if telemetry is not None:
                telemetry.http_429_wait_ms += int(round(wait_s * 1000))
            if not _wait_with_cancel(wait_s, cancel_event, sleep_fn):
                raise SecPacingError("cancelled", "SEC request cancelled during 429 Retry-After backoff.", retryable=False)
            continue

        if code == 404:
            tmp_path.unlink(missing_ok=True)
            raise SecPacingError("not_found", f"SEC returned HTTP 404 for {url}", status_code=404, retryable=False)
        if code in (401, 403):
            tmp_path.unlink(missing_ok=True)
            raise SecPacingError("forbidden", f"SEC refused request (HTTP {code}) for {url}", status_code=code, retryable=False)
        if not 200 <= code < 300:
            tmp_path.unlink(missing_ok=True)
            raise SecPacingError("source_unavailable", f"SEC returned HTTP {code} for {url}", status_code=code, retryable=code >= 500)

        content_type = http_header(reply.headers, "content-type")
        hasher = sha256()
        with tmp_path.open("rb") as fh:
            while True:
                blk = fh.read(65_536)
                if not blk:
                    break
                hasher.update(blk)
        return tmp_path, content_type, hasher.hexdigest()

    raise SecPacingError("provider_rate_limited", "SEC HTTP 429 retry budget exhausted.", status_code=429, retryable=True)


def sec_get_json(
    url: str,
    *,
    parse_float: Callable[[str], Any] | None = None,
    **kwargs: Any,
) -> tuple[Any, str]:
    """Fetch SEC JSON endpoint using waitable pacing and return ``(parsed_json, sha256)``."""
    raw_body, _ct, digest = sec_get_bytes(url, accept="application/json", **kwargs)
    try:
        text = raw_body.decode("utf-8")
        doc = json.loads(text, parse_float=parse_float) if parse_float is not None else json.loads(text)
    except (UnicodeDecodeError, ValueError) as err:
        raise SecPacingError("invalid_json", f"SEC returned invalid JSON for {url}", retryable=False) from err
    return doc, digest


def sec_get_text(
    url: str,
    *,
    accept: str = "text/html, application/xml, text/plain",
    **kwargs: Any,
) -> tuple[str, str, str]:
    """Fetch SEC HTML/XML/text endpoint and decode cleanly; returns ``(text, content_type, sha256)``."""
    raw_body, content_type, digest = sec_get_bytes(url, accept=accept, **kwargs)
    return _decode_bytes(raw_body, content_type).replace("\x00", ""), content_type, digest


@dataclass(frozen=True)
class RawSubmissionRow:
    """Single row from raw SEC ``submissions`` JSON (``recent`` or ``filings.files`` page)."""

    cik: str
    accession: str
    nodash: str
    form: str
    filed_at: date
    accepted_at: datetime | None = None
    period_of_report: date | None = None
    primary_document: str | None = None
    primary_document_url: str | None = None
    index_url: str | None = None
    description: str | None = None
    items: list[str] | None = None


@dataclass(frozen=True)
class RawSubmissionsResult:
    cik: str
    company_name: str | None
    tickers: list[str]
    exchanges: list[str]
    sic: str | None
    is_fpi: bool
    rows: list[RawSubmissionRow]
    pages_fetched: int
    earliest_loaded_date: date | None
    gaps: list[str] = field(default_factory=list)


def _extract_rows_from_columnar(cik: str, columnar: Mapping[str, Any]) -> tuple[list[RawSubmissionRow], date | None]:
    accs = columnar.get("accessionNumber")
    if not isinstance(accs, list):
        return [], None
    has_items = isinstance(columnar.get("items"), list)

    def col(name: str, idx: int) -> Any:
        arr = columnar.get(name)
        return arr[idx] if isinstance(arr, list) and idx < len(arr) else None

    rows: list[RawSubmissionRow] = []
    earliest: date | None = None
    for idx, raw_acc in enumerate(accs):
        form_raw = col("form", idx)
        filed = _parse_date(col("filingDate", idx))
        if not isinstance(raw_acc, str) or not isinstance(form_raw, str) or filed is None:
            continue
        earliest = filed if earliest is None or filed < earliest else earliest
        try:
            dashed, nodash = normalize_accession(raw_acc)
        except ValueError:
            continue
        primary = col("primaryDocument", idx)
        primary_clean = primary.strip() if isinstance(primary, str) and primary.strip() else None
        base_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}"
        items_val = _normalize_items(col("items", idx)) if has_items else None
        desc = col("primaryDocDescription", idx)
        rows.append(
            RawSubmissionRow(
                cik=cik,
                accession=dashed,
                nodash=nodash,
                form=form_raw.strip().upper(),
                filed_at=filed,
                accepted_at=_parse_dt(col("acceptanceDateTime", idx)),
                period_of_report=_parse_date(col("reportDate", idx)),
                primary_document=primary_clean,
                primary_document_url=f"{base_url}/{primary_clean}" if primary_clean else None,
                index_url=f"{base_url}/{dashed}-index.htm",
                description=desc.strip() if isinstance(desc, str) and desc.strip() else None,
                items=items_val,
            )
        )
    return rows, earliest


def _page_overlaps_range(file_meta: Mapping[str, Any], min_date: date, max_date: date | None) -> bool:
    f_from = _parse_date(file_meta.get("filingFrom"))
    f_to = _parse_date(file_meta.get("filingTo"))
    if f_to is not None and f_to < min_date:
        return False
    if max_date is not None and f_from is not None and f_from > max_date:
        return False
    return True


_SAFE_SUBMISSIONS_FILE_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}\.json$")


_SUBMISSIONS_TTL_LOCK = Lock()
_SUBMISSIONS_TTL_CACHE: dict[str, tuple[float, RawSubmissionsResult]] = {}
_SUBMISSIONS_TTL_SECONDS = 1200.0  # 20 minutes for lightweight fingerprint lookups


def fetch_company_submissions(
    cik: str | int,
    *,
    user_agent: str | None,
    fetch_since_date: date | None = None,
    as_of: date | None = None,
    max_pages: int = 5,
    fpi_indicator_forms: Sequence[str] = ("20-F", "20-F/A", "6-K", "6-K/A"),
    limiter: SecPacingLimiter | None = None,
    policy: FinDataPolicy | None = None,
    transport: Transport | None = None,
    stream_transport: StreamTransport | None = None,
    cancel_event: Event | None = None,
    deadline: float | None = None,
    telemetry: PacingTelemetry | None = None,
    use_ttl_cache: bool = False,
) -> RawSubmissionsResult:
    """Fetch raw SEC ``submissions`` JSON preserving ``items`` and paginating ``filings.files`` on demand."""
    target_cik = cik10(cik)
    if (
        use_ttl_cache
        and max_pages == 0
        and fetch_since_date is None
        and as_of is None
        and transport is None
        and stream_transport is None
    ):
        now_m = monotonic()
        with _SUBMISSIONS_TTL_LOCK:
            cached = _SUBMISSIONS_TTL_CACHE.get(target_cik)
            if cached is not None and (now_m - cached[0]) < _SUBMISSIONS_TTL_SECONDS:
                return cached[1]
    root_url = f"https://data.sec.gov/submissions/CIK{target_cik}.json"
    doc, _sha = sec_get_json(
        root_url,
        category="filings",
        user_agent=user_agent,
        limiter=limiter,
        policy=policy,
        transport=transport,
        stream_transport=stream_transport,
        max_bytes=DEFAULT_MAX_SUBMISSIONS_BYTES,
        large=True,
        cancel_event=cancel_event,
        deadline=deadline,
        telemetry=telemetry,
    )
    if not isinstance(doc, Mapping):
        raise SecPacingError("invalid_submissions", "SEC submissions response must be a JSON object.", retryable=False)
    filings_node = doc.get("filings")
    if not isinstance(filings_node, Mapping) or not isinstance(filings_node.get("recent"), Mapping):
        raise SecPacingError("invalid_submissions", "SEC submissions JSON missing filings.recent.", retryable=False)

    rows, earliest = _extract_rows_from_columnar(target_cik, filings_node["recent"])
    seen_acc: set[str] = {r.accession for r in rows}
    gaps: list[str] = []
    pages_fetched = 0

    files_meta = filings_node.get("files")
    if (
        isinstance(files_meta, list)
        and files_meta
        and fetch_since_date is not None
        and (earliest is None or fetch_since_date < earliest)
    ):
        for entry in files_meta:
            if not isinstance(entry, Mapping):
                continue
            fname = entry.get("name")
            if not isinstance(fname, str) or not _SAFE_SUBMISSIONS_FILE_RE.fullmatch(fname.strip()):
                continue
            if not _page_overlaps_range(entry, fetch_since_date, as_of):
                continue
            if pages_fetched >= max(0, int(max_pages)):
                gaps.append(
                    f"submissions_pagination_cap_reached:stopped_at_{earliest.isoformat() if earliest else 'unknown'}"
                )
                break
            page_url = f"https://data.sec.gov/submissions/{fname.strip()}"
            try:
                page_doc, _page_sha = sec_get_json(
                    page_url,
                    category="filings",
                    user_agent=user_agent,
                    limiter=limiter,
                    policy=policy,
                    transport=transport,
                    stream_transport=stream_transport,
                    max_bytes=DEFAULT_MAX_SUBMISSIONS_BYTES,
                    large=True,
                    cancel_event=cancel_event,
                    deadline=deadline,
                    telemetry=telemetry,
                )
                pages_fetched += 1
            except SecPacingError as err:
                gaps.append(f"submissions_page_failed:{fname}:{err.reason}")
                if err.reason in ("cancelled", "deadline_exceeded"):
                    break
                continue
            if not isinstance(page_doc, Mapping):
                gaps.append(f"submissions_page_invalid:{fname}")
                continue
            page_rows, page_earliest = _extract_rows_from_columnar(target_cik, page_doc)
            for row in page_rows:
                if row.accession not in seen_acc:
                    seen_acc.add(row.accession)
                    rows.append(row)
            if page_earliest is not None and (earliest is None or page_earliest < earliest):
                earliest = page_earliest
            if earliest is not None and earliest <= fetch_since_date:
                break

    if as_of is not None:
        rows = [r for r in rows if r.filed_at <= as_of]
    rows.sort(
        key=lambda r: (r.filed_at, r.accepted_at or datetime.min.replace(tzinfo=timezone.utc), r.accession),
        reverse=True,
    )

    fpi_forms = {f.strip().upper() for f in fpi_indicator_forms}
    is_fpi = any(r.form in fpi_forms for r in rows)
    tickers = [str(t).strip() for t in (doc.get("tickers") or []) if str(t).strip()]
    exchanges = [str(e).strip() for e in (doc.get("exchanges") or []) if str(e).strip()]
    raw_name = doc.get("name")
    company_name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
    raw_sic = doc.get("sic")
    sic = str(raw_sic).strip() if raw_sic is not None and str(raw_sic).strip() else None

    result = RawSubmissionsResult(
        cik=target_cik,
        company_name=company_name,
        tickers=tickers,
        exchanges=exchanges,
        sic=sic,
        is_fpi=is_fpi,
        rows=rows,
        pages_fetched=pages_fetched,
        earliest_loaded_date=earliest,
        gaps=gaps,
    )
    if (
        use_ttl_cache
        and max_pages == 0
        and fetch_since_date is None
        and as_of is None
        and transport is None
        and stream_transport is None
    ):
        with _SUBMISSIONS_TTL_LOCK:
            if len(_SUBMISSIONS_TTL_CACHE) > 512:
                _SUBMISSIONS_TTL_CACHE.clear()
            _SUBMISSIONS_TTL_CACHE[target_cik] = (monotonic(), result)
    return result


__all__ = [
    "DEFAULT_MAX_429_RETRIES",
    "DEFAULT_SEC_PACING_BURST",
    "DEFAULT_SEC_PACING_RATE_PER_SEC",
    "PacingTelemetry",
    "RawSubmissionRow",
    "RawSubmissionsResult",
    "SecPacingError",
    "SecPacingLimiter",
    "fetch_company_submissions",
    "sec_get_bytes",
    "sec_get_json",
    "sec_get_text",
]
