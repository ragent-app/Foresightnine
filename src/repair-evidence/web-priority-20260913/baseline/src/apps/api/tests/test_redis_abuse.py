from __future__ import annotations

from math import ceil
import os
from typing import Any

import pytest

from app.redis_abuse import (
    QUOTA_SCRIPT,
    WINDOW_SCRIPT,
    RedisAbuseControl,
    RedisAbuseSettings,
    build_abuse_control,
)


class RecordingRedis:
    """Deterministic Redis protocol double for the Lua admission boundary."""

    def __init__(self) -> None:
        self.now_us = 1_000_000_000_000
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.sorted_sets: dict[str, list[tuple[int, str]]] = {}
        self.expires: dict[str, int] = {}
        self.markers: dict[str, int] = {}
        self.destructive_calls: list[str] = []
        self.fail = False

    def _prune(self, key: str, cutoff: int) -> None:
        if key not in self.sorted_sets:
            return
        values = self.sorted_sets[key]
        self.sorted_sets[key] = [(score, member) for score, member in values if score > cutoff]

    def eval(self, script: str, numkeys: int, *args: Any) -> list[int]:
        if self.fail:
            raise TimeoutError("test backend timeout")
        keys = tuple(str(item) for item in args[:numkeys])
        values = args[numkeys:]
        self.calls.append((script, args))
        if script == QUOTA_SCRIPT:
            marker_key = keys[0]
            if self.markers.get(marker_key, 0) > self.now_us // 1000:
                return [1, 0]
            window_us, limit, marker, retention_ms = int(values[0]), int(values[1]), str(values[2]), int(values[3])
            cutoff = self.now_us - window_us
            retry = 0
            for key in keys[1:]:
                self._prune(key, cutoff)
                if len(self.sorted_sets.get(key, [])) >= limit:
                    oldest = self.sorted_sets[key][0][0]
                    retry = max(retry, ceil((oldest + window_us - self.now_us) / 1_000_000))
            if retry:
                return [0, retry]
            for key in keys[1:]:
                self.sorted_sets.setdefault(key, []).append((self.now_us, str(marker)))
                self.expires[key] = self.now_us // 1000 + window_us // 1000
            self.markers[marker_key] = self.now_us // 1000 + int(retention_ms)
            return [1, 0]

        window_us = int(values[0])
        limits = tuple(int(value) for value in values[1:1 + len(keys)])
        marker = str(values[1 + len(keys)])
        cutoff = self.now_us - window_us
        retry = 0
        for index, key in enumerate(keys):
            self._prune(key, cutoff)
            if len(self.sorted_sets.get(key, [])) >= limits[index]:
                oldest = self.sorted_sets[key][0][0]
                retry = max(retry, ceil((oldest + window_us - self.now_us) / 1_000_000))
        if retry:
            return [0, retry]
        for index, key in enumerate(keys, start=1):
            self.sorted_sets.setdefault(key, []).append((self.now_us, f"{marker}:{index}"))
            self.expires[key] = self.now_us // 1000 + window_us // 1000
        return [1, 0]

    def delete(self, *keys: str) -> None:
        self.destructive_calls.append(f"delete:{len(keys)}")

    def flushdb(self) -> None:
        self.destructive_calls.append("flushdb")


def _redis_control(fake: RecordingRedis, *, limit: int = 2, window: int = 10, retention: int = 60) -> RedisAbuseControl:
    return RedisAbuseControl(
        fake,
        settings=RedisAbuseSettings(
            ip_limit=limit,
            session_limit=limit,
            model_quota=limit,
            request_window_seconds=window,
            model_window_seconds=window,
            reservation_ttl_seconds=retention,
        ),
    )


def test_redis_window_is_one_lua_call_with_hashed_cluster_colocated_keys() -> None:
    fake = RecordingRedis()
    control = _redis_control(fake, limit=1)
    ip = "ip:203.0.113.8"
    session = "session:private-session-id"

    assert control.check_request(client_ip=ip, session_id=session).allowed
    script, args = fake.calls[-1]
    keys = tuple(str(item) for item in args[:2])
    assert script == WINDOW_SCRIPT
    assert "TIME" in script and "ZREMRANGEBYSCORE" in script and "ZCARD" in script
    assert all("{abuse}" in key for key in keys)
    assert all(ip not in key and session not in key for key in keys)
    assert len(fake.calls) == 1


def test_redis_window_denial_is_atomic_exact_and_expires_at_boundary() -> None:
    fake = RecordingRedis()
    control = _redis_control(fake, limit=1, window=10)
    assert control.check_request(client_ip="198.51.100.1", session_id="s1").allowed

    fake.now_us += 1_000_000
    denied = control.check_request(client_ip="198.51.100.1", session_id="s2")
    assert denied.allowed is False
    assert denied.reason == "rate_limit"
    assert denied.retry_after == 9
    # The new session subject was never incremented after the IP denial.
    assert all(len(values) == 1 for values in fake.sorted_sets.values())

    fake.now_us += 9_000_000
    assert control.check_request(client_ip="198.51.100.1", session_id="s2").allowed
    assert all(len(values) <= 1 for values in fake.sorted_sets.values())
    assert all(expiry <= fake.now_us // 1000 + 10_000 for expiry in fake.expires.values())


def test_redis_request_uses_distinct_ip_and_session_limits_atomically() -> None:
    fake = RecordingRedis()
    control = RedisAbuseControl(
        fake,
        settings=RedisAbuseSettings(
            ip_limit=2,
            session_limit=1,
            model_quota=2,
            request_window_seconds=60,
            model_window_seconds=60,
        ),
    )

    assert control.check_request(client_ip="ip-one", session_id="session-one").allowed
    first_script, first_args = fake.calls[-1]
    assert first_script == WINDOW_SCRIPT
    assert tuple(first_args[3:5]) == ("2", "1")
    assert not control.check_request(client_ip="ip-two", session_id="session-one").allowed
    assert sum(len(values) for values in fake.sorted_sets.values()) == 2
    assert control.check_request(client_ip="ip-one", session_id="session-two").allowed
    assert sum(len(values) for values in fake.sorted_sets.values()) == 4
    # The IP limit now denies, and the new session subject is not partially
    # incremented by the failed multi-subject decision.
    assert not control.check_request(client_ip="ip-one", session_id="session-three").allowed
    assert sum(len(values) for values in fake.sorted_sets.values()) == 4


def test_redis_quota_is_atomic_and_reservation_marker_is_idempotent() -> None:
    fake = RecordingRedis()
    control = _redis_control(fake, limit=1, window=60, retention=120)
    subjects = ("global:daily", "ip:198.51.100.2", "session:opaque")
    first = control.model.reserve("operation-private-id", subjects)
    assert first.allowed
    assert QUOTA_SCRIPT in fake.calls[-1][0]
    keys_after_first = set(fake.sorted_sets)
    assert all("operation-private-id" not in key for key in keys_after_first)
    assert all(len(values) == 1 for values in fake.sorted_sets.values())

    denied = control.model.reserve("different-operation", subjects)
    assert denied.allowed is False
    assert denied.reason == "model_quota"
    assert set(fake.sorted_sets) == keys_after_first
    assert all(len(values) == 1 for values in fake.sorted_sets.values())

    replay = control.model.reserve("operation-private-id", subjects)
    assert replay.allowed
    assert all(len(values) == 1 for values in fake.sorted_sets.values())
    assert all(expiry <= fake.now_us // 1000 + 120_000 for expiry in fake.markers.values())


def test_redis_reset_is_non_destructive() -> None:
    fake = RecordingRedis()
    control = _redis_control(fake)
    assert control.check_request(client_ip="198.51.100.3").allowed
    before = dict(fake.sorted_sets)
    control.reset()
    assert fake.sorted_sets == before
    assert fake.destructive_calls == []


def test_redis_backend_failures_are_typed_unavailable_without_fallback() -> None:
    fake = RecordingRedis()
    control = _redis_control(fake)
    fake.fail = True
    request_decision = control.check_request(client_ip="198.51.100.4")
    quota_decision = control.model.reserve("operation-id", ("global:daily",))
    assert request_decision.allowed is False and request_decision.available is False
    assert request_decision.reason == "abuse_backend_unavailable"
    assert quota_decision.allowed is False and quota_decision.available is False
    assert quota_decision.reason == "abuse_backend_unavailable"


def test_abuse_backend_configuration_is_memory_by_default_and_hardened_fail_closed() -> None:
    assert build_abuse_control(environ={}).__class__.__name__ == "AbuseControl"
    with pytest.raises(RuntimeError, match="explicit Redis abuse mode"):
        build_abuse_control(environ={"FORESIGHTNINE_ENV": "production"})
    with pytest.raises(RuntimeError, match="required in hardened"):
        build_abuse_control(environ={"FORESIGHTNINE_ABUSE_MODE": "memory", "FORESIGHTNINE_PUBLIC_HARDENING": "true"})
    with pytest.raises(ValueError, match="memory or redis"):
        build_abuse_control(environ={"FORESIGHTNINE_ABUSE_MODE": "unknown"})
    with pytest.raises(RuntimeError, match="require a Redis URL"):
        build_abuse_control(environ={"FORESIGHTNINE_ABUSE_MODE": "redis"})


def test_redis_factory_uses_either_supported_url_name_without_exposing_it() -> None:
    fake = RecordingRedis()
    secret_url = "rediss://:never-log-this@example.invalid:6380/4"
    backend = build_abuse_control(
        model_quota=3,
        environ={"FORESIGHTNINE_ABUSE_MODE": "redis", "REDIS_URL": secret_url},
        redis_client=fake,
    )
    assert isinstance(backend, RedisAbuseControl)
    assert secret_url not in repr(backend)
    assert secret_url not in repr(backend.model)


def test_optional_live_redis_is_explicitly_gated() -> None:
    url = os.getenv("FORESIGHTNINE_REDIS_URL") or os.getenv("REDIS_URL")
    if os.getenv("FORESIGHTNINE_REDIS_INTEGRATION") != "1" or not url:
        pytest.skip("set FORESIGHTNINE_REDIS_INTEGRATION=1 and a Redis URL to run")
    redis = pytest.importorskip("redis")
    client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
    assert client.ping() is True
