"""Server-side normalized market-data cache.

Only validated, non-secret provider data is cached.  The cache is Redis-backed
when the deployment supplies ``FORESIGHTNINE_REDIS_URL`` (the staging Compose
stack enables AOF persistence); tests and offline development use a no-op
backend rather than inventing a second source of truth.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import hashlib
import json
import math
import os
import re
from typing import Any, Protocol


class MarketCacheUnavailable(RuntimeError):
    """Raised when the configured durable cache cannot be reached."""


class MarketCache(Protocol):
    def get_json(self, key: str) -> object | None: ...

    def set_json(self, key: str, value: object, *, ttl_seconds: int) -> None: ...


class NoopMarketCache:
    """Explicit offline cache used only when no Redis URL is configured."""

    def get_json(self, key: str) -> object | None:
        return None

    def set_json(self, key: str, value: object, *, ttl_seconds: int) -> None:
        return None


class RedisMarketCache:
    """Small Redis JSON boundary with bounded values and no raw vendor cache."""

    def __init__(self, client: Any, *, prefix: str = "foresightnine:market:v2:", max_bytes: int = 2 * 1024 * 1024) -> None:
        self._client = client
        self._prefix = prefix
        self._max_bytes = max_bytes

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def get_json(self, key: str) -> object | None:
        try:
            raw = self._client.get(self._key(key))
        except Exception as error:
            raise MarketCacheUnavailable("Market cache is unavailable.") from error
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="strict")
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > self._max_bytes:
            try:
                self._client.delete(self._key(key))
            except Exception:
                pass
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            try:
                self._client.delete(self._key(key))
            except Exception:
                pass
            return None

    def set_json(self, key: str, value: object, *, ttl_seconds: int) -> None:
        try:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise MarketCacheUnavailable("Market cache value is not JSON serializable.") from error
        if len(encoded.encode("utf-8")) > self._max_bytes:
            raise MarketCacheUnavailable("Market cache value exceeded the bounded size.")
        try:
            self._client.setex(self._key(key), max(1, int(ttl_seconds)), encoded)
        except Exception as error:
            raise MarketCacheUnavailable("Market cache is unavailable.") from error


_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def cache_material_key(*parts: str) -> str:
    """Hash stable non-secret cache dimensions; never hash or store a key."""
    material = "\x00".join(parts).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def provider_rows_key(provider_id: str, symbol: str, start: date, cutoff: date) -> str:
    return cache_material_key("rows", "v2", provider_id, symbol, start.isoformat(), cutoff.isoformat())


def fx_key(provider_id: str, cutoff: date) -> str:
    return cache_material_key("fx", "v1", provider_id, cutoff.isoformat())


def read_rows(cache: MarketCache, key: str) -> dict[date, float] | None:
    value = cache.get_json(key)
    if value is None:
        return None
    # Short-lived negative entries prevent repeatedly hammering a provider that
    # is currently rate-limited or unavailable.  An empty real price series is
    # never written by the adapter, so the sentinel is unambiguous.
    if isinstance(value, Mapping) and value.get("__status__") == "unavailable":
        return {}
    if not isinstance(value, Mapping) or len(value) > 10000:
        return None
    rows: dict[date, float] = {}
    for raw_date, raw_value in value.items():
        if not isinstance(raw_date, str) or not _DATE_PATTERN.fullmatch(raw_date):
            return None
        try:
            observed = date.fromisoformat(raw_date)
        except ValueError:
            return None
        if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool):
            return None
        close = float(raw_value)
        if not math.isfinite(close) or close <= 0:
            return None
        rows[observed] = close
    return rows or None


def write_rows(cache: MarketCache, key: str, rows: Mapping[date, float], *, ttl_seconds: int) -> None:
    cache.set_json(key, {observed.isoformat(): float(close) for observed, close in rows.items()}, ttl_seconds=ttl_seconds)


def build_market_cache(*, environ: Mapping[str, str] | None = None, redis_client: Any | None = None) -> MarketCache:
    env = environ if environ is not None else os.environ
    url = (env.get("FORESIGHTNINE_REDIS_URL") or env.get("REDIS_URL") or "").strip()
    if redis_client is not None:
        return RedisMarketCache(redis_client)
    if not url:
        return NoopMarketCache()
    try:
        import redis

        client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
    except Exception as error:
        raise MarketCacheUnavailable("Market cache could not be configured.") from error
    return RedisMarketCache(client)


def cache_ttl_seconds(environ: Mapping[str, str] | None = None) -> int:
    env = environ if environ is not None else os.environ
    try:
        value = int(env.get("FORESIGHTNINE_MARKET_CACHE_TTL_SECONDS", "900"))
    except ValueError:
        value = 900
    return max(60, min(value, 86_400))


__all__ = [
    "MarketCache",
    "MarketCacheUnavailable",
    "NoopMarketCache",
    "RedisMarketCache",
    "build_market_cache",
    "cache_ttl_seconds",
    "fx_key",
    "provider_rows_key",
    "read_rows",
    "write_rows",
]
