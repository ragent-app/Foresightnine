"""Cheap network reachability probe for providers that are only reachable from some networks.

Tushare and BaoStock are reachable from mainland China networks (or through a proxy) only. From other hosts a
call would sit in a TCP connect timeout (BaoStock: ~40 s per call), so every call and the capability flags first
consult this probe: one TCP connect with a short timeout, cached per process, at most one probe in flight per
provider (concurrent callers wait for it and share its answer).

Env: FORESIGHTNINE_CN_PROBE_TIMEOUT_SECONDS (default 5) and FORESIGHTNINE_CN_PROBE_CACHE_SECONDS (default 600).
The probe checks the TCP path only (no TLS handshake, no request, no credential), which is all that fails here.
"""
from __future__ import annotations

import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable

PROBE_TIMEOUT_ENV = "FORESIGHTNINE_CN_PROBE_TIMEOUT_SECONDS"
PROBE_TTL_ENV = "FORESIGHTNINE_CN_PROBE_CACHE_SECONDS"
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_TTL_SECONDS = 600.0
# provider -> (display name, host, port)
TARGETS: dict[str, tuple[str, str, int]] = {
    "tushare": ("Tushare", "api.tushare.pro", 443),
    "baostock": ("BaoStock", "public-api.baostock.com", 10030),
}
UNREACHABLE_REASON = "cn_provider_unreachable"


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def tcp_connect(host: str, port: int, timeout: float) -> None:
    """Raise OSError when a TCP connection cannot be established within ``timeout`` seconds."""
    with socket.create_connection((host, port), timeout=timeout):
        pass


@dataclass(frozen=True)
class ProbeResult:
    provider: str
    reachable: bool
    checked_at: float
    error: str | None = None


class ReachabilityProbe:
    def __init__(self, *, connect: Callable[[str, int, float], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._connect, self._clock = connect, clock
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._cache: dict[str, ProbeResult] = {}

    def _fresh(self, provider: str) -> ProbeResult | None:
        hit = self._cache.get(provider)
        if hit is not None and self._clock() - hit.checked_at < _env_float(PROBE_TTL_ENV, DEFAULT_TTL_SECONDS):
            return hit
        return None

    def check(self, provider: str) -> ProbeResult:
        target = TARGETS.get(provider)
        if target is None:  # not a probed provider
            return ProbeResult(provider, True, self._clock())
        with self._guard:
            hit = self._fresh(provider)
            lock = self._locks.setdefault(provider, threading.Lock())
        if hit is not None:
            return hit
        with lock:  # one probe in flight; waiters re-read the cache once it finishes
            with self._guard:
                hit = self._fresh(provider)
            if hit is not None:
                return hit
            _name, host, port = target
            connect = self._connect or tcp_connect
            try:
                connect(host, port, _env_float(PROBE_TIMEOUT_ENV, DEFAULT_TIMEOUT_SECONDS))
                result = ProbeResult(provider, True, self._clock())
            except OSError as error:  # includes socket.timeout and gaierror
                result = ProbeResult(provider, False, self._clock(), type(error).__name__)
            with self._guard:
                self._cache[provider] = result
            return result

    def reachable(self, provider: str) -> bool:
        return self.check(provider).reachable

    def clear(self) -> None:
        with self._guard:
            self._cache.clear()


_default: ReachabilityProbe | None = None
_default_lock = threading.Lock()


def default_probe() -> ReachabilityProbe:
    global _default
    with _default_lock:
        if _default is None:
            _default = ReachabilityProbe()
        return _default


def provider_label(provider: str) -> str:
    return TARGETS.get(provider, (provider.title(),))[0]


def unreachable_message(providers: list[str]) -> str:
    names = " and ".join(provider_label(p) for p in providers) or "The A-share data provider"
    verb = "are" if len(providers) > 1 else "is"
    return (f"{names} {verb} not reachable from this server (the connection timed out), so no request was made. "
            "They are reachable only from mainland China networks or through a proxy. Use another source.")
