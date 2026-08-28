"""Small, redacted observability hooks for the public MVP boundary.

Telemetry is deliberately allow-listed.  Callers may pass rich internal
context, but only stable low-cardinality labels are retained; request content,
credentials, tokens, URLs and network identifiers never enter this store.
"""
from __future__ import annotations

from collections import Counter, deque
from datetime import datetime, timezone
from threading import RLock
from typing import Any


_SAFE_FIELDS = {"provider", "endpoint", "status", "reason", "kind"}
_SAFE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./:-")
_KNOWN_EVENTS = frozenset({
    "provider_failure", "quota_denial", "captcha_denial", "stale_data_denial", "cleanup_failure",
})


def _safe_label(value: object, *, maximum: int = 80) -> str | None:
    if not isinstance(value, str) or not value or len(value) > maximum or "://" in value:
        return None
    if any(character not in _SAFE_CHARS for character in value):
        return None
    return value


class Telemetry:
    """Thread-safe counters and a bounded redacted event ring."""

    def __init__(self, *, max_events: int = 256) -> None:
        self.max_events = max_events
        self._counters: Counter[str] = Counter()
        self._events: deque[dict[str, str]] = deque(maxlen=max_events)
        self._lock = RLock()

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._events.clear()

    def record(self, event: str, **fields: object) -> None:
        event_label = event if event in _KNOWN_EVENTS else "unknown"
        safe_fields = {
            key: label
            for key, value in fields.items()
            if key in _SAFE_FIELDS
            for label in [_safe_label(value)]
            if label is not None
        }
        with self._lock:
            self._counters[event_label] += 1
            entry = {
                "event": event_label,
                "at": datetime.now(timezone.utc).isoformat(),
                **safe_fields,
            }
            self._events.append(entry)

    def increment(self, event: str) -> None:
        self.record(event)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"counters": dict(self._counters), "events": list(self._events)}


telemetry = Telemetry()


def record_provider_failure(*, provider: str | None = None, endpoint: str | None = None) -> None:
    telemetry.record("provider_failure", provider=provider, endpoint=endpoint)


def record_quota_denial(*, kind: str = "model_call") -> None:
    telemetry.record("quota_denial", kind=kind)


def record_captcha_denial(*, reason: str = "verification") -> None:
    telemetry.record("captcha_denial", reason=reason)


def record_stale_data_denial(*, endpoint: str | None = None) -> None:
    telemetry.record("stale_data_denial", endpoint=endpoint)


def record_cleanup_failure(*, kind: str | None = None) -> None:
    telemetry.record("cleanup_failure", kind=kind)
