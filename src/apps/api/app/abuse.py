"""Provider-neutral in-memory abuse controls for the anonymous MVP."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Callable, Iterable

from .telemetry import record_quota_denial


@dataclass(frozen=True)
class AbuseDecision:
    allowed: bool
    retry_after: int = 0
    reason: str = "allowed"


class SlidingWindowLimiter:
    def __init__(self, *, limit: int, window_seconds: float = 60.0, now: Callable[[], float] = monotonic) -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValueError("rate limit and window must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._now = now
        self._events: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = RLock()

    def check(self, key: str) -> AbuseDecision:
        current = self._now()
        with self._lock:
            bucket = self._events[key]
            cutoff = current - self.window_seconds
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                retry = max(1, int(bucket[0] + self.window_seconds - current + 0.999))
                return AbuseDecision(False, retry, "rate_limit")
            bucket.append(current)
            return AbuseDecision(True)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


class ModelCallQuota:
    """At-most-once reservations for one logical idempotent provider call."""

    def __init__(self, *, limit: int = 20, window_seconds: float = 86400.0, now: Callable[[], float] = monotonic) -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValueError("quota and window must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._now = now
        self._events: defaultdict[str, deque[float]] = defaultdict(deque)
        self._reservations: dict[str, tuple[str, ...]] = {}
        self._lock = RLock()

    def reserve(self, reservation_id: str, subjects: Iterable[str]) -> AbuseDecision:
        normalized = tuple(dict.fromkeys(subject for subject in subjects if subject))
        if not normalized:
            raise ValueError("at least one quota subject is required")
        current = self._now()
        with self._lock:
            if reservation_id in self._reservations:
                return AbuseDecision(True)
            buckets: list[deque[float]] = []
            retry_after = 0
            for subject in normalized:
                bucket = self._events[subject]
                cutoff = current - self.window_seconds
                while bucket and bucket[0] <= cutoff:
                    bucket.popleft()
                buckets.append(bucket)
                if len(bucket) >= self.limit:
                    retry_after = max(retry_after, int(bucket[0] + self.window_seconds - current + 0.999))
            if retry_after:
                record_quota_denial()
                return AbuseDecision(False, max(1, retry_after), "model_quota")
            for bucket in buckets:
                bucket.append(current)
            self._reservations[reservation_id] = normalized
            return AbuseDecision(True)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._reservations.clear()


class AbuseControl:
    def __init__(self, *, ip_limit: int = 120, session_limit: int = 60, model_quota: int = 20, now: Callable[[], float] = monotonic) -> None:
        self.ip = SlidingWindowLimiter(limit=ip_limit, now=now)
        self.session = SlidingWindowLimiter(limit=session_limit, now=now)
        self.model = ModelCallQuota(limit=model_quota, now=now)

    def check_request(self, *, client_ip: str, session_id: str | None = None) -> AbuseDecision:
        decision = self.ip.check(f"ip:{client_ip}")
        if not decision.allowed:
            return decision
        if session_id:
            return self.session.check(f"session:{session_id}")
        return decision

    def reset(self) -> None:
        self.ip.reset()
        self.session.reset()
        self.model.reset()

