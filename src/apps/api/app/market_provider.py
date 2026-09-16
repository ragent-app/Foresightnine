"""Bounded server-side market snapshot adapter for the Linux staging edge.

The adapter serves the licensed staging evaluation only.  It has a fixed
symbol allowlist, does not accept a provider URL or credential from a
request, does not cache raw vendor responses, and never invents vendor
factor scores or FX quotes.  Factor features emitted here are deterministic
derivations of the licensed price series (momentum, momentum_month, rsi14,
sma_ratio) computed strictly from sessions at or before each anchor, so no
future information leaks into a factor value.  Without the owner-approved
evaluation mode and admission manifest it returns limited rows so the
client fails closed for current Public Default amounts.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import io
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .market_cache import (
    MarketCache,
    MarketCacheUnavailable,
    build_market_cache,
    cache_ttl_seconds,
    fx_key,
    provider_rows_key,
    read_rows,
    write_rows,
)


ANCHORS = ("year_start", "latest_month_start", "latest_completed_session")
ASSET_SYMBOLS = {
    # Legacy client IDs remain supported during the staged client migration.
    "GRID": "PAVE",
    "CHIP": "SOXX",
    "UTIL": "XLU",
    "GOLD": "GLD",
    "WORLD": "VT",
    # Canonical registry IDs from public-provider-adaptation-plan.md.
    "ETF:SPY": "SPY",
    "ETF:SOXX": "SOXX",
    "ETF:GLD": "GLD",
    "ETF:QQQ": "QQQ",
    "ETF:IEF": "IEF",
}
# Trailing calendar days fetched before the earliest anchor so every anchor
# has honest point-in-time context for the derived features.
FEATURE_HISTORY_DAYS = 420
MAX_BODY_BYTES = 64 * 1024
MAX_VENDOR_BYTES = 512 * 1024
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9._-]{0,15}$")
PROVIDER_MANIFEST_PATH = Path(__file__).resolve().parents[3] / "infra" / "deployment" / "provider-manifest.v1.json"
FEATURE_NAMES = ("momentum", "momentum_month", "rsi14", "sma_ratio")
FX_CURRENCIES = ("CNY", "USD", "EUR")
PRICE_PROVIDER_ORDER = ("market_data", "alpha_vantage", "twelve_data", "finnhub", "yahoo", "stooq")
PROVIDER_KEY_ENV = {
    "market_data": "FORESIGHTNINE_MARKETDATA_API_KEY",
    "alpha_vantage": "FORESIGHTNINE_ALPHAVANTAGE_API_KEY",
    "twelve_data": "FORESIGHTNINE_TWELVEDATA_API_KEY",
    "finnhub": "FORESIGHTNINE_FINNHUB_API_KEY",
    "stooq": "FORESIGHTNINE_STOOQ_API_KEY",
}


class _RejectVendorRedirect(HTTPRedirectHandler):
    """Keep a fixed-host adapter from following a vendor redirect elsewhere."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        raise ValueError("vendor redirects are not allowed")


def _load_manifest() -> dict[str, Any] | None:
    try:
        manifest = json.loads(PROVIDER_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "provider-admission-manifest/v1":
        return None
    if manifest.get("runtime_status") != "staging_only" or manifest.get("production_admission") != "blocked_pending_external_release_approvals":
        return None
    return manifest


def _manifest_providers(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    providers = manifest.get("providers")
    if not isinstance(providers, list):
        return {}
    return {provider.get("id"): provider for provider in providers if isinstance(provider, dict) and isinstance(provider.get("id"), str)}


def _price_manifest_allows(provider_id: str, fallback_provider_id: str | None = None) -> bool:
    """Require the checked-in admission manifest to agree with runtime mode."""
    manifest = _load_manifest()
    if manifest is None:
        return False
    providers = _manifest_providers(manifest)
    def _admitted(entry: Mapping[str, Any]) -> bool:
        return (
            entry.get("enabled_modes") == ["staging_evaluation"]
            and entry.get("credentials") in {"none", "optional_server_secret", "required_server_secret"}
            and entry.get("raw_cache") == "disabled"
            and entry.get("features") in {"not_authorized", "price_derived_only"}
            and entry.get("fx") == "not_authorized"
            and entry.get("authorization_status") == "owner_confirmed"
            and entry.get("public_redistribution") == "approved"
        )

    # The primary provider must be admitted. A declared fallback must either
    # be admitted as well or be explicitly disabled with credentials pending,
    # in which case the runtime simply never reaches it.
    primary = providers.get(provider_id)
    if not isinstance(primary, dict) or not _admitted(primary):
        return False
    if fallback_provider_id is not None:
        fallback = providers.get(fallback_provider_id)
        if not isinstance(fallback, dict):
            return False
        if not _admitted(fallback) and not (
            fallback.get("enabled_modes") == []
            and fallback.get("credentials") == "required_server_secret"
            and fallback.get("authorization_status") == "licensed_pending_credentials"
        ):
            return False

    mappings = manifest.get("asset_mappings")
    if not isinstance(mappings, list):
        return False
    mapping_by_asset: dict[str, dict[str, Any]] = {}
    for mapping in mappings:
        if not isinstance(mapping, dict) or not isinstance(mapping.get("asset_id"), str):
            return False
        asset_id = mapping["asset_id"]
        if asset_id in mapping_by_asset:
            return False
        mapping_by_asset[asset_id] = mapping
    return all(
        (mapping := mapping_by_asset.get(asset_id)) is not None
        and (
            mapping.get("provider") == provider_id
            or provider_id in mapping.get("fallback_providers", [])
            or mapping.get("fallback_provider") == provider_id
        )
        and (
            fallback_provider_id is None
            or mapping.get("provider") == fallback_provider_id
            or fallback_provider_id in mapping.get("fallback_providers", [])
            or mapping.get("fallback_provider") == fallback_provider_id
        )
        and mapping.get("symbol") == symbol
        and mapping.get("currency") == "USD"
        for asset_id, symbol in ASSET_SYMBOLS.items()
    )


def _admitted_price_providers() -> tuple[str, ...]:
    """Return the server-owned provider order from the admission manifest."""
    manifest = _load_manifest()
    if manifest is None:
        return ()
    providers = _manifest_providers(manifest)
    source_selection = manifest.get("source_selection")
    selection = source_selection.get("fallback_order", {}).get("market_price") if isinstance(source_selection, dict) else None
    if not isinstance(selection, list):
        return ()
    mappings = manifest.get("asset_mappings")
    if not isinstance(mappings, list):
        return ()
    mapping_by_asset = {item.get("asset_id"): item for item in mappings if isinstance(item, dict)}
    admitted: list[str] = []
    for provider_id in selection:
        entry = providers.get(provider_id)
        if not isinstance(entry, dict):
            continue
        if not (
            entry.get("enabled_modes") == ["staging_evaluation"]
            and entry.get("credentials") in {"none", "optional_server_secret", "required_server_secret"}
            and entry.get("raw_cache") == "disabled"
            and entry.get("features") == "price_derived_only"
            and entry.get("fx") == "not_authorized"
            and entry.get("authorization_status") == "owner_confirmed"
            and entry.get("public_redistribution") == "approved"
        ):
            continue
        if not all(
            isinstance(mapping_by_asset.get(asset_id), dict)
            and (
                mapping_by_asset[asset_id].get("provider") == provider_id
                or provider_id in mapping_by_asset[asset_id].get("fallback_providers", [])
                or mapping_by_asset[asset_id].get("fallback_provider") == provider_id
            )
            for asset_id in ASSET_SYMBOLS
        ):
            continue
        admitted.append(provider_id)
    return tuple(admitted)


def _fx_manifest_allows(provider_id: str) -> bool:
    """Admit exactly one owner-approved non-trading FX reference source."""
    manifest = _load_manifest()
    if manifest is None:
        return False
    provider = _manifest_providers(manifest).get(provider_id)
    if not isinstance(provider, dict):
        return False
    if not (
        provider.get("enabled_modes") == ["staging_evaluation"]
        and provider.get("credentials") == "none"
        and provider.get("raw_cache") == "disabled"
        and provider.get("features") == "not_authorized"
        and provider.get("fx") == "ecb_reference_approved"
        and provider.get("authorization_status") == "owner_confirmed"
        and provider.get("public_redistribution") == "approved"
    ):
        return False
    fx_mapping = manifest.get("fx_mapping")
    return isinstance(fx_mapping, dict) and fx_mapping.get("primary") == provider_id


class MarketSnapshotRequest(BaseModel):
    """The small JSON request used by the shared Web/Android runtime."""

    model_config = ConfigDict(extra="forbid", strict=True)

    assetIds: list[str] = Field(min_length=1, max_length=64)
    anchors: dict[str, str]
    cutoff: str = Field(min_length=10, max_length=40)
    timezone: str = Field(min_length=1, max_length=128)
    requestedAt: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_shape(self) -> "MarketSnapshotRequest":
        if len(set(self.assetIds)) != len(self.assetIds):
            raise ValueError("assetIds must be unique")
        if any(not isinstance(asset, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", asset) for asset in self.assetIds):
            raise ValueError("assetIds contain an invalid identifier")
        if set(self.anchors) != set(ANCHORS) or len(self.anchors) != len(ANCHORS):
            raise ValueError("anchors must contain exactly the three snapshot anchors")
        cutoff = _cutoff_date(self.cutoff)
        for anchor in ANCHORS:
            anchor_date = _date_only(self.anchors[anchor])
            if anchor_date is None or anchor_date > cutoff:
                raise ValueError("anchors must be valid dates no later than cutoff")
        return self


def _date_only(value: str) -> date | None:
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return None
        return date.fromisoformat(value)
    except ValueError:
        return None


def _cutoff_date(value: str) -> date:
    parsed = _date_only(value)
    if parsed is not None:
        return parsed
    try:
        parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("cutoff must be an ISO date or offset datetime") from error
    if parsed_datetime.tzinfo is None:
        raise ValueError("cutoff must include a timezone")
    return parsed_datetime.date()


def _retrieved_at(request: MarketSnapshotRequest) -> str:
    """Timestamp ceiling that can never exceed the client's cutoff boundary.

    A date-only cutoff denotes the end of that calendar day; an explicit
    offset datetime is an intraday boundary and caps at that exact instant.
    """
    cutoff_instant = _cutoff_instant(request.cutoff)
    if request.requestedAt is not None:
        try:
            candidate = datetime.fromisoformat(request.requestedAt.replace("Z", "+00:00"))
            if candidate.tzinfo is not None:
                return min(candidate.astimezone(timezone.utc), cutoff_instant).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    return cutoff_instant.isoformat().replace("+00:00", "Z")


def _cutoff_boundary_date(value: str) -> date:
    """Mirror the client boundary: an intraday datetime cannot include its own calendar day."""
    try:
        parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed_datetime = None
    base = parsed_datetime.astimezone(timezone.utc).date() if parsed_datetime is not None and parsed_datetime.tzinfo is not None else _cutoff_date(value)
    return base - timedelta(days=1) if parsed_datetime is not None else base


def _cutoff_instant(value: str) -> datetime:
    try:
        parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed_datetime = None
    if parsed_datetime is not None and parsed_datetime.tzinfo is not None:
        return parsed_datetime.astimezone(timezone.utc)
    return datetime.combine(_cutoff_date(value), datetime.max.time(), tzinfo=timezone.utc)


def _missing_row(asset_id: str, anchor: str, request: MarketSnapshotRequest, warning: str) -> dict[str, Any]:
    return {
        "assetId": asset_id,
        "anchor": anchor,
        "asOf": None,
        "retrievedAt": _retrieved_at(request),
        "timezone": request.timezone,
        "price": None,
        "close": None,
        "currency": "USD",
        "status": "missing",
        "warnings": [warning],
    }


def _reject_future_rows(rows: dict[date, float], cutoff: date) -> dict[date, float]:
    if any(observed > cutoff for observed in rows):
        raise ValueError("vendor response contained a session after the request cutoff")
    return rows


def resolve_provider_consensus(
    observations: Sequence[tuple[str, Any]],
    priority: Mapping[str, int],
) -> Any | None:
    """Resolve observations for one canonical field without cross-category fallback.

    The highest-priority available provider wins. When several providers share
    that priority, a strict majority of equal values is required; ties and
    missing priorities fail closed as ``None``.
    """
    ranked = [
        (priority[provider_id], value)
        for provider_id, value in observations
        if provider_id in priority and value is not None
    ]
    if not ranked:
        return None
    best_rank = min(rank for rank, _value in ranked)
    candidates = [value for rank, value in ranked if rank == best_rank]
    for candidate in candidates:
        votes = sum(value == candidate for value in candidates)
        if votes * 2 > len(candidates):
            return candidate
    return None


def _safe_stooq_rows(payload: bytes) -> dict[date, float]:
    """Parse only the bounded Date/Close subset of the Stooq CSV response."""
    if len(payload) > MAX_VENDOR_BYTES:
        raise ValueError("vendor response exceeded the bounded payload")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("vendor response was not valid UTF-8") from error
    reader = csv.DictReader(io.StringIO(text))
    fields = set((field or "").strip() for field in (reader.fieldnames or []))
    if not {"Date", "Close"} <= fields:
        raise ValueError("vendor response was not the expected CSV shape")
    rows: dict[date, float] = {}
    for record in reader:
        observed = _date_only((record.get("Date") or "").strip())
        raw_close = (record.get("Close") or "").strip()
        if observed is None or not raw_close:
            continue
        try:
            close = float(raw_close)
        except ValueError:
            continue
        if not math.isfinite(close) or close <= 0:
            continue
        if observed in rows:
            raise ValueError("vendor response had duplicate session dates")
        rows[observed] = close
    if not rows:
        raise ValueError("vendor response had no usable close series")
    return rows


def _safe_yahoo_rows(payload: bytes) -> dict[date, float]:
    """Parse only the bounded timestamp/close subset of the Yahoo chart JSON."""
    if len(payload) > MAX_VENDOR_BYTES:
        raise ValueError("vendor response exceeded the bounded payload")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("vendor response was not valid JSON") from error
    chart = document.get("chart") if isinstance(document, dict) else None
    results = chart.get("result") if isinstance(chart, dict) else None
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise ValueError("vendor response was not the expected chart shape")
    result = results[0]
    timestamps = result.get("timestamp")
    indicators = result.get("indicators")
    quotes = indicators.get("quote") if isinstance(indicators, dict) else None
    closes = quotes[0].get("close") if isinstance(quotes, list) and quotes and isinstance(quotes[0], dict) else None
    if not isinstance(timestamps, list) or not isinstance(closes, list) or len(timestamps) != len(closes):
        raise ValueError("vendor response was not the expected chart shape")
    rows: dict[date, float] = {}
    for timestamp, close in zip(timestamps, closes):
        if not isinstance(timestamp, int) or isinstance(timestamp, bool) or close is None:
            continue
        try:
            observed = datetime.fromtimestamp(timestamp, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            continue
        if not isinstance(close, (int, float)) or isinstance(close, bool):
            continue
        value = float(close)
        if not math.isfinite(value) or value <= 0:
            continue
        if observed in rows:
            raise ValueError("vendor response had duplicate session dates")
        rows[observed] = value
    if not rows:
        raise ValueError("vendor response had no usable close series")
    return rows


def _bounded_get(url: str, accept: str, timeout: float, *, headers: Mapping[str, str] | None = None) -> bytes:
    request_headers = {"Accept": accept, "User-Agent": "ForesightNine-staging-provider-evaluation/1"}
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    with build_opener(_RejectVendorRedirect).open(request, timeout=timeout) as response:
        return response.read(MAX_VENDOR_BYTES + 1)


def _fetch_start(anchors: Sequence[str], cutoff: date) -> date:
    valid_dates = [_date_only(value) for value in anchors]
    earliest = min((value for value in valid_dates if value is not None), default=cutoff)
    return earliest - timedelta(days=FEATURE_HISTORY_DAYS)


class StooqEvaluationAdapter:
    """Fixed-host CSV adapter with an optional server-only API key."""

    endpoint = "https://stooq.com/q/d/l/"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    def fetch(self, symbol: str, anchors: tuple[str, ...], cutoff: date) -> dict[date, float]:
        if symbol not in ASSET_SYMBOLS.values() or not SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError("symbol is outside the fixed provider allowlist")
        start = _fetch_start(anchors, cutoff)
        query_params: dict[str, str] = {
            "s": f"{symbol.lower()}.us",
            "i": "d",
            "d1": start.strftime("%Y%m%d"),
            "d2": cutoff.strftime("%Y%m%d"),
        }
        # Stooq permits deployments with no key as well as deployments issued
        # a server-side key.  Never accept this value from a client request or
        # expose it in a bundle/log; it is injected only by the process env.
        api_key = self._api_key if self._api_key is not None else os.getenv("FORESIGHTNINE_STOOQ_API_KEY", "").strip()
        if api_key:
            query_params["apikey"] = api_key
        query = urlencode(query_params)
        return _reject_future_rows(_safe_stooq_rows(_bounded_get(f"{self.endpoint}?{query}", "text/csv", 8)), cutoff)


class YahooEvaluationAdapter:
    """Fixed-host chart adapter for the licensed fallback price source."""

    endpoint = "https://query1.finance.yahoo.com/v8/finance/chart/"

    def fetch(self, symbol: str, anchors: tuple[str, ...], cutoff: date) -> dict[date, float]:
        if symbol not in ASSET_SYMBOLS.values() or not SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError("symbol is outside the fixed provider allowlist")
        period1 = int(datetime.combine(_fetch_start(anchors, cutoff), datetime.min.time(), tzinfo=timezone.utc).timestamp())
        period2 = int(datetime.combine(cutoff + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp())
        query = urlencode({"period1": period1, "period2": period2, "interval": "1d", "events": "history"})
        return _reject_future_rows(
            _safe_yahoo_rows(_bounded_get(f"{self.endpoint}{symbol}?{query}", "application/json", 8)), cutoff
        )


def _required_provider_key(env_name: str) -> str:
    value = os.getenv(env_name, "").strip()
    if not value or len(value) > 512:
        raise ValueError(f"{env_name} is not configured")
    return value


def _provider_keys(env_name: str) -> tuple[str, ...]:
    """Read one or more server-only keys without ever returning them to a client."""
    values = [item.strip() for item in os.getenv(f"{env_name}S", "").split(",") if item.strip()]
    single = os.getenv(env_name, "").strip()
    if single and single not in values:
        values.insert(0, single)
    if not values or any(len(value) > 512 for value in values):
        raise ValueError(f"{env_name} is not configured")
    return tuple(values)


def _safe_alpha_vantage_rows(payload: bytes) -> dict[date, float]:
    if len(payload) > MAX_VENDOR_BYTES:
        raise ValueError("vendor response exceeded the bounded payload")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("vendor response was not valid JSON") from error
    series = document.get("Time Series (Daily)") if isinstance(document, dict) else None
    if not isinstance(series, dict):
        raise ValueError("vendor response was not the expected daily time-series shape")
    rows: dict[date, float] = {}
    for raw_date, record in series.items():
        observed = _date_only(raw_date) if isinstance(raw_date, str) else None
        raw_close = record.get("4. close") if isinstance(record, dict) else None
        if observed is None or not isinstance(raw_close, (str, int, float)) or isinstance(raw_close, bool):
            continue
        try:
            close = float(raw_close)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(close) or close <= 0 or observed in rows:
            if observed in rows:
                raise ValueError("vendor response had duplicate session dates")
            continue
        rows[observed] = close
    if not rows:
        raise ValueError("vendor response had no usable close series")
    return rows


def _safe_candle_rows(payload: bytes) -> dict[date, float]:
    """Parse the common timestamp/close candle shape used by Market Data/Finnhub."""
    if len(payload) > MAX_VENDOR_BYTES:
        raise ValueError("vendor response exceeded the bounded payload")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("vendor response was not valid JSON") from error
    if not isinstance(document, dict) or document.get("s") not in {"ok", "success"}:
        raise ValueError("vendor response did not report usable candles")
    timestamps = document.get("t")
    closes = document.get("c")
    if not isinstance(timestamps, list) or not isinstance(closes, list) or len(timestamps) != len(closes):
        raise ValueError("vendor response was not the expected candle shape")
    rows: dict[date, float] = {}
    for timestamp, raw_close in zip(timestamps, closes):
        if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool) or not isinstance(raw_close, (int, float)) or isinstance(raw_close, bool):
            continue
        try:
            observed = datetime.fromtimestamp(float(timestamp), tz=timezone.utc).date()
            close = float(raw_close)
        except (OverflowError, OSError, ValueError):
            continue
        if not math.isfinite(close) or close <= 0:
            continue
        if observed in rows:
            raise ValueError("vendor response had duplicate session dates")
        rows[observed] = close
    if not rows:
        raise ValueError("vendor response had no usable close series")
    return rows


def _safe_twelve_data_rows(payload: bytes) -> dict[date, float]:
    if len(payload) > MAX_VENDOR_BYTES:
        raise ValueError("vendor response exceeded the bounded payload")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("vendor response was not valid JSON") from error
    values = document.get("values") if isinstance(document, dict) else None
    if not isinstance(values, list):
        raise ValueError("vendor response was not the expected time-series shape")
    rows: dict[date, float] = {}
    for record in values:
        if not isinstance(record, dict):
            continue
        raw_date = record.get("datetime")
        raw_close = record.get("close")
        observed = _date_only(raw_date[:10]) if isinstance(raw_date, str) and len(raw_date) >= 10 else None
        if observed is None or not isinstance(raw_close, (str, int, float)) or isinstance(raw_close, bool):
            continue
        try:
            close = float(raw_close)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(close) or close <= 0:
            continue
        if observed in rows:
            raise ValueError("vendor response had duplicate session dates")
        rows[observed] = close
    if not rows:
        raise ValueError("vendor response had no usable close series")
    return rows


class AlphaVantageEvaluationAdapter:
    """Server-only Alpha Vantage daily-price adapter."""

    endpoint = "https://www.alphavantage.co/query"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    def fetch(self, symbol: str, anchors: tuple[str, ...], cutoff: date) -> dict[date, float]:
        if symbol not in ASSET_SYMBOLS.values() or not SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError("symbol is outside the fixed provider allowlist")
        keys = (self._api_key,) if self._api_key is not None else _provider_keys("FORESIGHTNINE_ALPHAVANTAGE_API_KEY")
        key = keys[int(sha256(symbol.encode("utf-8")).hexdigest(), 16) % len(keys)]
        query = urlencode({"function": "TIME_SERIES_DAILY", "symbol": symbol, "outputsize": "full", "apikey": key})
        return _reject_future_rows(_safe_alpha_vantage_rows(_bounded_get(f"{self.endpoint}?{query}", "application/json", 8)), cutoff)


class MarketDataEvaluationAdapter:
    """Server-only Market Data historical candle adapter."""

    endpoint = "https://api.marketdata.app/v1/stocks/candles/D/"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    def fetch(self, symbol: str, anchors: tuple[str, ...], cutoff: date) -> dict[date, float]:
        if symbol not in ASSET_SYMBOLS.values() or not SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError("symbol is outside the fixed provider allowlist")
        key = self._api_key if self._api_key is not None else _required_provider_key("FORESIGHTNINE_MARKETDATA_API_KEY")
        start = _fetch_start(anchors, cutoff)
        query = urlencode({"from": start.isoformat(), "to": cutoff.isoformat(), "adjustdividends": "false"})
        payload = _bounded_get(
            f"{self.endpoint}{symbol}/?{query}",
            "application/json",
            8,
            headers={"Authorization": f"Bearer {key}"},
        )
        return _reject_future_rows(_safe_candle_rows(payload), cutoff)


class TwelveDataEvaluationAdapter:
    """Server-only Twelve Data daily time-series adapter."""

    endpoint = "https://api.twelvedata.com/time_series"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    def fetch(self, symbol: str, anchors: tuple[str, ...], cutoff: date) -> dict[date, float]:
        if symbol not in ASSET_SYMBOLS.values() or not SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError("symbol is outside the fixed provider allowlist")
        key = self._api_key if self._api_key is not None else _required_provider_key("FORESIGHTNINE_TWELVEDATA_API_KEY")
        start = _fetch_start(anchors, cutoff)
        query = urlencode({"symbol": symbol, "interval": "1day", "start_date": start.isoformat(), "end_date": cutoff.isoformat(), "order": "ASC", "timezone": "UTC"})
        payload = _bounded_get(
            f"{self.endpoint}?{query}",
            "application/json",
            8,
            headers={"Authorization": f"apikey {key}"},
        )
        return _reject_future_rows(_safe_twelve_data_rows(payload), cutoff)


class FinnhubEvaluationAdapter:
    """Server-only Finnhub daily candle adapter."""

    endpoint = "https://finnhub.io/api/v1/stock/candle"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    def fetch(self, symbol: str, anchors: tuple[str, ...], cutoff: date) -> dict[date, float]:
        if symbol not in ASSET_SYMBOLS.values() or not SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError("symbol is outside the fixed provider allowlist")
        key = self._api_key if self._api_key is not None else _required_provider_key("FORESIGHTNINE_FINNHUB_API_KEY")
        start = _fetch_start(anchors, cutoff)
        start_ts = int(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp())
        end_ts = int(datetime.combine(cutoff + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp())
        query = urlencode({"symbol": symbol, "resolution": "D", "from": start_ts, "to": end_ts, "token": key})
        return _reject_future_rows(_safe_candle_rows(_bounded_get(f"{self.endpoint}?{query}", "application/json", 8)), cutoff)


class FrankfurterFxAdapter:
    """Fixed-host ECB reference FX adapter; never an executable quote feed."""

    endpoint = "https://api.frankfurter.dev/v1/latest"

    def fetch(self, cutoff: date, *, boundary: date | None = None) -> dict[str, Any]:
        query = urlencode({"base": "CNY", "symbols": ",".join(currency for currency in FX_CURRENCIES if currency != "CNY")})
        payload = _bounded_get(f"{self.endpoint}?{query}", "application/json", 6)
        return _safe_frankfurter_document(payload, boundary if boundary is not None else cutoff)


def _safe_frankfurter_document(payload: bytes, cutoff: date) -> dict[str, Any]:
    """Parse the bounded ECB reference response into CNY-per-unit rates."""
    if len(payload) > MAX_VENDOR_BYTES:
        raise ValueError("vendor response exceeded the bounded payload")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("vendor response was not valid JSON") from error
    if not isinstance(document, dict) or document.get("base") != "CNY" or not isinstance(document.get("rates"), dict):
        raise ValueError("vendor response was not the expected FX shape")
    as_of = _date_only(str(document.get("date") or ""))
    if as_of is None or as_of > cutoff:
        raise ValueError("vendor FX date is missing or after the request cutoff")
    cny_per_unit: dict[str, float] = {"CNY": 1.0}
    for currency, rate in document["rates"].items():
        if currency not in FX_CURRENCIES or not isinstance(rate, (int, float)) or isinstance(rate, bool):
            continue
        value = float(rate)
        if not math.isfinite(value) or value <= 0:
            continue
        # The ECB base is CNY, so each rate is foreign units per CNY and
        # the client contract needs the inverse CNY-per-unit rate.
        cny_per_unit[currency] = 1.0 / value
    if any(currency not in cny_per_unit for currency in FX_CURRENCIES):
        raise ValueError("vendor FX response did not cover the required currencies")
    return {"asOf": as_of.isoformat(), "provider": "frankfurter", "cnyPerUnit": cny_per_unit}


def _session_on_or_before(rows: Mapping[date, float], target: date) -> date | None:
    candidates = [observed for observed in rows if observed <= target]
    return max(candidates) if candidates else None


def _derived_features(rows: Mapping[date, float], session: date) -> dict[str, float | None]:
    """Point-in-time factor features from sessions at or before ``session``.

    Every feature uses only closes dated at or before the anchor session, so
    no future anchor price can leak into an earlier anchor's factor value.
    """
    close = rows[session]
    features: dict[str, float | None] = {name: None for name in FEATURE_NAMES}

    def _lag_return(days: int) -> float | None:
        prior = _session_on_or_before(rows, session - timedelta(days=days))
        if prior is None or prior == session:
            return None
        lagged = rows[prior]
        change = close / lagged - 1.0
        return change if math.isfinite(change) else None

    features["momentum"] = _lag_return(365)
    features["momentum_month"] = _lag_return(28)

    sma_window = [value for observed, value in sorted(rows.items()) if session - timedelta(days=90) < observed <= session]
    if len(sma_window) >= 5:
        average = math.fsum(sma_window) / len(sma_window)
        ratio = close / average - 1.0
        features["sma_ratio"] = ratio if math.isfinite(ratio) and average > 0 else None

    rsi_window = [value for observed, value in sorted(rows.items()) if session - timedelta(days=40) < observed <= session][-15:]
    if len(rsi_window) >= 15:
        changes = [rsi_window[index] - rsi_window[index - 1] for index in range(1, len(rsi_window))][-14:]
        gains = [change for change in changes if change > 0]
        losses = [-change for change in changes if change < 0]
        average_gain = math.fsum(gains) / 14.0
        average_loss = math.fsum(losses) / 14.0
        if average_gain == 0.0 and average_loss == 0.0:
            features["rsi14"] = None
        elif average_loss == 0.0:
            features["rsi14"] = 100.0
        else:
            features["rsi14"] = 100.0 - 100.0 / (1.0 + average_gain / average_loss)
    return features


def _adapter_for(provider_id: str, api_key: str | None = None) -> Any:
    adapters: dict[str, Any] = {
        "market_data": MarketDataEvaluationAdapter(api_key),
        "alpha_vantage": AlphaVantageEvaluationAdapter(api_key),
        "twelve_data": TwelveDataEvaluationAdapter(api_key),
        "finnhub": FinnhubEvaluationAdapter(api_key),
        "yahoo": YahooEvaluationAdapter(),
        "stooq": StooqEvaluationAdapter(api_key),
    }
    try:
        return adapters[provider_id]
    except KeyError as error:
        raise ValueError("provider is not implemented") from error


def _cached_provider_rows(
    provider_id: str,
    adapter: Any,
    symbol: str,
    anchors: tuple[str, ...],
    cutoff: date,
    cache: MarketCache,
    *,
    warnings: list[str],
) -> dict[date, float]:
    start = _fetch_start(anchors, cutoff)
    key = provider_rows_key(provider_id, symbol, start, cutoff)
    try:
        cached = read_rows(cache, key)
    except MarketCacheUnavailable:
        if os.getenv("FORESIGHTNINE_ENV", "development").strip().lower() == "production":
            raise
        cached = None
        if "Server market cache unavailable; fetched data was not cached." not in warnings:
            warnings.append("Server market cache unavailable; fetched data was not cached.")
    if cached is not None:
        return _reject_future_rows(cached, cutoff)
    try:
        rows = adapter.fetch(symbol, anchors, cutoff)
    except Exception:
        # Cache only the typed unavailable marker, never the provider error or
        # response body.  This keeps a rate-limited source from being retried
        # for every browser request while the healthy priority source serves.
        try:
            cache.set_json(
                key,
                {"__status__": "unavailable"},
                ttl_seconds=min(60, cache_ttl_seconds()),
            )
        except MarketCacheUnavailable:
            if os.getenv("FORESIGHTNINE_ENV", "development").strip().lower() == "production":
                raise
        raise
    try:
        write_rows(cache, key, rows, ttl_seconds=cache_ttl_seconds())
    except MarketCacheUnavailable:
        if os.getenv("FORESIGHTNINE_ENV", "development").strip().lower() == "production":
            raise
        if "Server market cache unavailable; fetched data was not cached." not in warnings:
            warnings.append("Server market cache unavailable; fetched data was not cached.")
    return rows


def _merge_provider_rows(
    provider_rows: Mapping[str, Mapping[date, float]],
    priority: Mapping[str, int],
) -> dict[date, float]:
    """Merge normalized provider series using priority and equal-rank majority."""
    dates = set().union(*(rows.keys() for rows in provider_rows.values())) if provider_rows else set()
    merged: dict[date, float] = {}
    for observed in dates:
        value = resolve_provider_consensus(
            [(provider_id, rows.get(observed)) for provider_id, rows in provider_rows.items()],
            priority,
        )
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and float(value) > 0:
            merged[observed] = float(value)
    return merged


def build_snapshot_response(
    request: MarketSnapshotRequest,
    *,
    provider_order: Sequence[str] | None = None,
    provider_credentials: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a client-compatible bundle fragment with explicit limitations."""

    mode = os.getenv("FORESIGHTNINE_MARKET_PROVIDER_MODE", "unavailable").strip().lower()
    evaluation_enabled = (
        mode in {"public_evaluation", "stooq_evaluation"}
        and os.getenv("FORESIGHTNINE_ENV", "development").strip().lower() != "production"
        and bool(_admitted_price_providers())
    )
    fx_enabled = (
        mode in {"public_evaluation", "stooq_evaluation"}
        and os.getenv("FORESIGHTNINE_ENV", "development").strip().lower() != "production"
        and _fx_manifest_allows("frankfurter")
    )
    admitted_provider_ids = _admitted_price_providers() if evaluation_enabled else ()
    provider_ids = admitted_provider_ids if provider_order is None else tuple(
        provider_id for provider_id in provider_order if provider_id in admitted_provider_ids
    )
    credential_overrides = provider_credentials or {}
    adapters = tuple(
        (
            provider_id,
            _adapter_for(provider_id, credential_overrides.get(provider_id)),
            provider_id if provider_id not in credential_overrides else f"{provider_id}:account:{sha256(credential_overrides[provider_id].encode()).hexdigest()[:16]}",
        )
        for provider_id in provider_ids
    )
    provider_priority = {provider_id: index for index, provider_id in enumerate(provider_ids)}
    consensus_mode = os.getenv("FORESIGHTNINE_MARKET_CONSENSUS_MODE", "priority").strip().lower()
    if consensus_mode not in {"priority", "all"}:
        consensus_mode = "priority"
    cache = build_market_cache()
    cache_ttl = cache_ttl_seconds()
    warnings = [
        "Licensed staging evaluation only; public production admission remains blocked pending release approvals.",
        f"Market data is fetched server-side and normalized into a Redis-backed cache (TTL {cache_ttl}s); provider credentials never reach clients.",
        f"Provider conflicts use {consensus_mode} mode: priority order first, with strict majority for equal-rank observations when all candidates are evaluated.",
        "Factor features are deterministic price-derived values (momentum, momentum_month, rsi14, sma_ratio) computed from the licensed close series; they are not vendor factor scores.",
        "Only three point-in-time anchors are returned; this is not a backtest window.",
    ]
    snapshots: list[dict[str, Any]] = []
    cutoff = _cutoff_date(request.cutoff)
    for asset_id in request.assetIds:
        symbol = ASSET_SYMBOLS.get(asset_id)
        rows: dict[date, float] = {}
        if adapters and symbol is not None:
            failed_providers: list[str] = []
            candidate_rows: dict[str, dict[date, float]] = {}
            for provider_id, adapter, cache_provider_id in adapters:
                try:
                    provider_result = _cached_provider_rows(
                        cache_provider_id,
                        adapter,
                        symbol,
                        tuple(request.anchors[anchor] for anchor in ANCHORS),
                        cutoff,
                        cache,
                        warnings=warnings,
                    )
                    if not provider_result:
                        raise ValueError("provider returned no usable rows")
                    candidate_rows[provider_id] = provider_result
                    if consensus_mode == "priority":
                        break
                except Exception:
                    failed_providers.append(provider_id)
            rows = _merge_provider_rows(candidate_rows, provider_priority)
            if not rows and failed_providers:
                warnings.append(
                    f"All configured price providers unavailable for {asset_id}; returning explicit missing rows."
                )
        elif symbol is None:
            warnings.append(f"No licensed provider mapping is configured for {asset_id}.")
        else:
            warnings.append("No approved provider is enabled on this server.")
        for anchor in ANCHORS:
            anchor_date = _date_only(request.anchors[anchor])
            selected = max((observed for observed in rows if anchor_date is not None and observed <= anchor_date), default=None)
            if selected is None:
                snapshots.append(_missing_row(asset_id, anchor, request, "Price or authorized feature data is unavailable for this anchor."))
                continue
            price = rows[selected]
            calendar_gap = (anchor_date - selected).days
            # A calendar anchor can fall on a non-trading day (holiday/weekend).
            # Year/month anchors tolerate a bounded non-trading gap and stay
            # honest via an explicit warning; the latest-session anchor stays
            # strict so genuine vendor lag always fails closed.
            tolerant_anchor = anchor != "latest_completed_session"
            is_stale = selected < anchor_date and not (tolerant_anchor and calendar_gap <= 10)
            row_warnings = ["Price row with deterministic price-derived factor features; FX conversion is not embedded in the row."]
            if selected < anchor_date:
                if is_stale:
                    row_warnings.append(
                        f"Provider session {selected.isoformat()} precedes requested anchor {anchor_date.isoformat()}."
                    )
                else:
                    row_warnings.append(
                        f"Anchor {anchor_date.isoformat()} is a non-trading date; the last completed session {selected.isoformat()} is used."
                    )
            features = _derived_features(rows, selected)
            row: dict[str, Any] = {
                "assetId": asset_id,
                "anchor": anchor,
                "asOf": selected.isoformat(),
                "retrievedAt": _retrieved_at(request),
                "timezone": request.timezone,
                "price": price,
                "close": price,
                "currency": "USD",
                "status": "stale" if is_stale else "ok",
                "warnings": row_warnings,
            }
            if any(value is not None for value in features.values()):
                # Emitting the whole feature map, including nulls, lets the
                # client fail closed on any underivable feature.
                row["features"] = features
            snapshots.append(row)
    response: dict[str, Any] = {"snapshots": snapshots, "warnings": sorted(set(warnings))}
    if fx_enabled:
        try:
            fx_cache_key = fx_key("frankfurter", _cutoff_boundary_date(request.cutoff))
            cached_fx = cache.get_json(fx_cache_key)
            if isinstance(cached_fx, dict) and isinstance(cached_fx.get("asOf"), str) and isinstance(cached_fx.get("provider"), str) and isinstance(cached_fx.get("cnyPerUnit"), dict):
                fx = dict(cached_fx)
            else:
                fx = FrankfurterFxAdapter().fetch(cutoff, boundary=_cutoff_boundary_date(request.cutoff))
                cache.set_json(fx_cache_key, fx, ttl_seconds=cache_ttl)
            fx["retrievedAt"] = _retrieved_at(request)
            response["fx"] = fx
        except MarketCacheUnavailable:
            if os.getenv("FORESIGHTNINE_ENV", "development").strip().lower() == "production":
                raise
            warnings.append("Server market cache unavailable; FX was not cached.")
            try:
                fx = FrankfurterFxAdapter().fetch(cutoff, boundary=_cutoff_boundary_date(request.cutoff))
                fx["retrievedAt"] = _retrieved_at(request)
                response["fx"] = fx
            except Exception:
                response["warnings"] = sorted(set([*response["warnings"], "Owner-approved FX reference source unavailable; FX conversion is missing and Public Default must stay disabled."]))
        except Exception:
            response["warnings"] = sorted(
                set(
                    [
                        *response["warnings"],
                        "Owner-approved FX reference source unavailable; FX conversion is missing and Public Default must stay disabled.",
                    ]
                )
            )
    return response


__all__ = [
    "ASSET_SYMBOLS",
    "ANCHORS",
    "FEATURE_NAMES",
    "MarketSnapshotRequest",
    "resolve_provider_consensus",
    "StooqEvaluationAdapter",
    "YahooEvaluationAdapter",
    "AlphaVantageEvaluationAdapter",
    "MarketDataEvaluationAdapter",
    "TwelveDataEvaluationAdapter",
    "FinnhubEvaluationAdapter",
    "FrankfurterFxAdapter",
    "build_snapshot_response",
]
