"""Per-provider token bucket plus daily quota; in-process and thread-safe.

``RateLimitBackend`` is the seam for a shared (Redis) implementation later.
Defaults for Twelve Data are placeholders to be verified against the actual plan.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Protocol


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    wait_seconds: float = 0.0
    reason: str | None = None  # "rate_limit" | "daily_quota"
    daily_remaining: int | None = None


@dataclass(frozen=True)
class ProviderLimit:
    rate_per_sec: float
    burst: float
    daily_quota: int | None = None


class RateLimitBackend(Protocol):
    def acquire(self, provider: str, cost: int = 1) -> RateDecision: ...


def _env_int(env, name: str, default: int) -> int:
    try:
        value = int(env.get(name, ""))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def default_limits(env=None) -> dict[str, ProviderLimit]:
    env = os.environ if env is None else env
    # DEFAULTS TO BE VERIFIED against the actual Twelve Data plan.
    per_min = _env_int(env, "FORESIGHTNINE_TWELVEDATA_RATE_PER_MIN", 8)
    daily = _env_int(env, "FORESIGHTNINE_TWELVEDATA_DAILY_QUOTA", 800)
    # Tushare: per-account bucket, points-dependent real limits are enforced by the provider (error 40203).
    tushare_min = _env_int(env, "FORESIGHTNINE_TUSHARE_RATE_PER_MIN", 50)
    return {"sec_edgar": ProviderLimit(rate_per_sec=10.0, burst=10.0),
            "tushare": ProviderLimit(rate_per_sec=tushare_min / 60.0, burst=float(min(tushare_min, 10))),
            "baostock": ProviderLimit(rate_per_sec=1.0, burst=3.0),
            "twelve_data": ProviderLimit(rate_per_sec=per_min / 60.0, burst=float(per_min), daily_quota=daily)}


class InProcessRateLimiter:
    def __init__(self, limits: dict[str, ProviderLimit] | None = None, *,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._limits = dict(limits if limits is not None else default_limits())
        self._clock, self._now = clock, now
        self._lock = threading.Lock()
        self._tokens: dict[str, tuple[float, float]] = {}
        self._daily: dict[str, tuple[date, int]] = {}

    def acquire(self, provider: str, cost: int = 1) -> RateDecision:
        limit = self._limits.get(provider) or self._limits.get(provider.split(':', 1)[0])
        if limit is None:
            return RateDecision(True)
        with self._lock:
            tokens, last = self._tokens.get(provider, (limit.burst, self._clock()))
            current = self._clock()
            tokens = min(limit.burst, tokens + (current - last) * limit.rate_per_sec)
            now = self._now()
            day, used = self._daily.get(provider, (now.date(), 0))
            if day != now.date():
                day, used = now.date(), 0
            remaining = None if limit.daily_quota is None else limit.daily_quota - used
            if limit.daily_quota is not None and used + cost > limit.daily_quota:
                midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
                self._tokens[provider] = (tokens, current)
                return RateDecision(False, (midnight - now).total_seconds(), "daily_quota", 0)
            if tokens < cost:
                self._tokens[provider] = (tokens, current)
                return RateDecision(False, (cost - tokens) / limit.rate_per_sec, "rate_limit", remaining)
            self._tokens[provider] = (tokens - cost, current)
            self._daily[provider] = (day, used + cost)
            return RateDecision(True, 0.0, None, None if remaining is None else remaining - cost)


_default: InProcessRateLimiter | None = None
_default_lock = threading.Lock()


def default_limiter() -> InProcessRateLimiter:
    global _default
    with _default_lock:
        if _default is None:
            _default = InProcessRateLimiter()
        return _default
