"""Redis-backed distributed abuse controls.

All admission decisions are made by Redis Lua scripts.  The scripts use the
Redis server clock, prune before reading, and mutate every subject only after
all subjects have passed their limit check.  Subjects and reservation markers
are hashed before they become key material.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from typing import Any, Iterable, Mapping
from uuid import uuid4

from .abuse import AbuseControl, AbuseDecision
from .telemetry import record_quota_denial


ABUSE_KEY_PREFIX = "foresightnine:abuse:{abuse}"
UNAVAILABLE_REASON = "abuse_backend_unavailable"
DEFAULT_WINDOW_SECONDS = 60
DEFAULT_SESSION_LIMIT = 60
DEFAULT_IP_LIMIT = 120
DEFAULT_RESERVATION_TTL_SECONDS = 86400
MAX_TIMEOUT_SECONDS = 30.0


WINDOW_SCRIPT = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000000 + tonumber(clock[2])
local window = tonumber(ARGV[1])
local marker = ARGV[2 + #KEYS]
local cutoff = now - window
local retry = 0

for index, key in ipairs(KEYS) do
  local limit = tonumber(ARGV[1 + index])
  redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
  local count = redis.call('ZCARD', key)
  if count >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    if oldest[2] then
      local wait = math.ceil((tonumber(oldest[2]) + window - now) / 1000000)
      if wait > retry then retry = wait end
    end
  end
end

if retry > 0 then
  for _, key in ipairs(KEYS) do
    redis.call('PEXPIRE', key, math.max(1, math.floor(window / 1000)))
  end
  return {0, retry}
end

for index, key in ipairs(KEYS) do
  redis.call('ZADD', key, now, marker .. ':' .. index)
  redis.call('PEXPIRE', key, math.max(1, math.floor(window / 1000)))
end
return {1, 0}
"""


QUOTA_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return {1, 0}
end

local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000000 + tonumber(clock[2])
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local marker = ARGV[3]
local retention = tonumber(ARGV[4])
local cutoff = now - window
local retry = 0

for index = 2, #KEYS do
  local key = KEYS[index]
  redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
  local count = redis.call('ZCARD', key)
  if count >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    if oldest[2] then
      local wait = math.ceil((tonumber(oldest[2]) + window - now) / 1000000)
      if wait > retry then retry = wait end
    end
  end
end

if retry > 0 then
  for index = 2, #KEYS do
    redis.call('PEXPIRE', KEYS[index], math.max(1, math.floor(window / 1000)))
  end
  return {0, retry}
end

for index = 2, #KEYS do
  redis.call('ZADD', KEYS[index], now, marker)
  redis.call('PEXPIRE', KEYS[index], math.max(1, math.floor(window / 1000)))
end
redis.call('SET', KEYS[1], '1', 'PX', retention, 'NX')
return {1, 0}
"""


def _hash_subject(subject: str) -> str:
    return sha256(subject.encode("utf-8")).hexdigest()


def _key(kind: str, subject_hash: str) -> str:
    return f"{ABUSE_KEY_PREFIX}:{kind}:{subject_hash}"


def _marker_hash(reservation_id: str) -> str:
    return _hash_subject(f"reservation:{reservation_id}")


def _env_int(environ: Mapping[str, str], name: str, default: int, *, minimum: int = 1, maximum: int = 7 * 24 * 3600) -> int:
    try:
        value = int(environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


def _env_float(environ: Mapping[str, str], name: str, default: float) -> float:
    try:
        value = float(environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(max(value, 0.05), MAX_TIMEOUT_SECONDS)


def _is_hardened(environ: Mapping[str, str]) -> bool:
    return environ.get("FORESIGHTNINE_ENV", "development").strip().lower() == "production" or environ.get("FORESIGHTNINE_PUBLIC_HARDENING", "").strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class RedisAbuseSettings:
    ip_limit: int = DEFAULT_IP_LIMIT
    session_limit: int = DEFAULT_SESSION_LIMIT
    model_quota: int = 20
    request_window_seconds: int = DEFAULT_WINDOW_SECONDS
    model_window_seconds: int = 86400
    reservation_ttl_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS
    socket_connect_timeout: float = 1.0
    socket_timeout: float = 1.0


class RedisSlidingWindowLimiter:
    def __init__(self, client: Any, *, limit: int, window_seconds: int, key_kind: str = "window") -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValueError("rate limit and window must be positive")
        self.client = client
        self.limit = limit
        self.window_seconds = window_seconds
        self.key_kind = key_kind

    def _keys(self, subjects: Iterable[str]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(subject for subject in subjects if subject))
        if not normalized:
            raise ValueError("at least one abuse subject is required")
        return tuple(_key(self.key_kind, _hash_subject(subject)) for subject in normalized)

    def check_many(self, subjects: Iterable[str], *, limits: Iterable[int] | None = None) -> AbuseDecision:
        try:
            normalized = tuple(dict.fromkeys(subject for subject in subjects if subject))
            keys = self._keys(normalized)
            effective_limits = tuple(self.limit for _ in keys) if limits is None else tuple(int(value) for value in limits)
            if len(effective_limits) != len(keys) or any(value < 1 for value in effective_limits):
                raise ValueError("one positive limit is required per abuse subject")
            result = self.client.eval(
                WINDOW_SCRIPT,
                len(keys),
                *keys,
                str(self.window_seconds * 1_000_000),
                *(str(value) for value in effective_limits),
                _hash_subject(f"event:{uuid4().hex}"),
            )
            allowed, retry_after = _parse_script_result(result)
            if allowed:
                return AbuseDecision(True)
            return AbuseDecision(False, retry_after, "rate_limit")
        except ValueError:
            raise
        except Exception:
            return AbuseDecision.unavailable()

    def check(self, key: str) -> AbuseDecision:
        return self.check_many((key,))


class RedisModelCallQuota:
    def __init__(self, client: Any, *, limit: int = 20, window_seconds: int = 86400, reservation_ttl_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS) -> None:
        if limit < 1 or window_seconds <= 0 or reservation_ttl_seconds <= 0:
            raise ValueError("quota, window and reservation retention must be positive")
        self.client = client
        self.limit = limit
        self.window_seconds = window_seconds
        self.reservation_ttl_seconds = reservation_ttl_seconds

    def reserve(self, reservation_id: str, subjects: Iterable[str]) -> AbuseDecision:
        normalized = tuple(dict.fromkeys(subject for subject in subjects if subject))
        if not normalized:
            raise ValueError("at least one quota subject is required")
        reservation_hash = _marker_hash(reservation_id)
        marker_key = _key("reservation", reservation_hash)
        keys = (marker_key,) + tuple(_key("quota", _hash_subject(subject)) for subject in normalized)
        try:
            result = self.client.eval(
                QUOTA_SCRIPT,
                len(keys),
                *keys,
                str(self.window_seconds * 1_000_000),
                str(self.limit),
                reservation_hash,
                str(self.reservation_ttl_seconds * 1000),
            )
            allowed, retry_after = _parse_script_result(result)
            if allowed:
                return AbuseDecision(True)
            record_quota_denial()
            return AbuseDecision(False, retry_after, "model_quota")
        except ValueError:
            raise
        except Exception:
            return AbuseDecision.unavailable()


class RedisAbuseControl:
    """Distributed abuse control with the same shape as ``AbuseControl``."""

    backend_name = "redis"

    def __init__(self, client: Any, *, settings: RedisAbuseSettings | None = None) -> None:
        settings = settings or RedisAbuseSettings()
        self.ip = RedisSlidingWindowLimiter(client, limit=settings.ip_limit, window_seconds=settings.request_window_seconds)
        self.session = RedisSlidingWindowLimiter(client, limit=settings.session_limit, window_seconds=settings.request_window_seconds)
        self.model = RedisModelCallQuota(
            client,
            limit=settings.model_quota,
            window_seconds=settings.model_window_seconds,
            reservation_ttl_seconds=settings.reservation_ttl_seconds,
        )

    def check_request(self, *, client_ip: str, session_id: str | None = None) -> AbuseDecision:
        subjects = [f"ip:{client_ip}"]
        limits = [self.ip.limit]
        if session_id:
            subjects.append(f"session:{session_id}")
            limits.append(self.session.limit)
        # One script call is required: an IP success must never survive a
        # session denial (and vice versa).
        return self.ip.check_many(subjects, limits=limits)

    def reset(self) -> None:
        # Redis reset is deliberately non-destructive. TTLs and normal pruning
        # reclaim this namespace; never FLUSHDB or scan/delete a broad prefix.
        return None


def _parse_script_result(result: Any) -> tuple[bool, int]:
    if not isinstance(result, (list, tuple)) or len(result) != 2:
        raise RuntimeError("invalid abuse script response")
    try:
        status = int(result[0])
        retry_after = int(result[1])
    except (TypeError, ValueError, OverflowError) as error:
        raise RuntimeError("invalid abuse script response") from error
    if status not in {0, 1} or retry_after < 0:
        raise RuntimeError("invalid abuse script response")
    return status == 1, retry_after


def _settings(environ: Mapping[str, str], model_quota: int) -> RedisAbuseSettings:
    return RedisAbuseSettings(
        ip_limit=_env_int(environ, "FORESIGHTNINE_IP_RATE_LIMIT", DEFAULT_IP_LIMIT),
        session_limit=_env_int(environ, "FORESIGHTNINE_SESSION_RATE_LIMIT", DEFAULT_SESSION_LIMIT),
        model_quota=max(1, model_quota),
        request_window_seconds=_env_int(environ, "FORESIGHTNINE_ABUSE_WINDOW_SECONDS", DEFAULT_WINDOW_SECONDS),
        model_window_seconds=_env_int(environ, "FORESIGHTNINE_MODEL_QUOTA_WINDOW_SECONDS", 86400),
        reservation_ttl_seconds=_env_int(environ, "FORESIGHTNINE_ABUSE_RESERVATION_TTL_SECONDS", DEFAULT_RESERVATION_TTL_SECONDS),
        socket_connect_timeout=_env_float(environ, "FORESIGHTNINE_REDIS_CONNECT_TIMEOUT_SECONDS", 1.0),
        socket_timeout=_env_float(environ, "FORESIGHTNINE_REDIS_SOCKET_TIMEOUT_SECONDS", 1.0),
    )


def build_abuse_control(*, model_quota: int = 20, environ: Mapping[str, str] | None = None, redis_client: Any | None = None) -> AbuseControl | RedisAbuseControl:
    environ = os.environ if environ is None else environ
    mode = environ.get("FORESIGHTNINE_ABUSE_MODE", "").strip().lower()
    hardened = _is_hardened(environ)
    if not mode:
        if hardened:
            raise RuntimeError("Hardened environments require explicit Redis abuse mode.")
        mode = "memory"
    if mode == "memory":
        if hardened:
            raise RuntimeError("Redis abuse controls are required in hardened environments.")
        return AbuseControl(model_quota=max(1, model_quota))
    if mode != "redis":
        raise ValueError("FORESIGHTNINE_ABUSE_MODE must be memory or redis.")
    redis_url = environ.get("FORESIGHTNINE_REDIS_URL", "").strip() or environ.get("REDIS_URL", "").strip()
    if not redis_url:
        raise RuntimeError("Redis abuse controls require a Redis URL.")
    settings = _settings(environ, model_quota)
    client = redis_client
    if client is None:
        try:
            import redis
            client = redis.Redis.from_url(
                redis_url,
                socket_connect_timeout=settings.socket_connect_timeout,
                socket_timeout=settings.socket_timeout,
                health_check_interval=30,
            )
        except Exception:
            # Do not chain or stringify the provider exception: URL-bearing
            # parser/connection messages must never reach logs or responses.
            raise RuntimeError("Redis abuse controls could not be configured.") from None
    return RedisAbuseControl(client, settings=settings)
