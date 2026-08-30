"""Bounded server-side market snapshot adapter for the Linux staging edge.

The adapter is intentionally evaluation-only.  It has a fixed symbol allowlist,
does not accept a provider URL or credential from a request, does not cache raw
vendor responses, and never derives factors or FX values.  Without an explicit
evaluation mode it returns limited rows so the client fails closed for current
Public Default amounts.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
import io
import json
import math
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
MAX_BODY_BYTES = 64 * 1024
MAX_VENDOR_BYTES = 512 * 1024
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9._-]{0,15}$")
PROVIDER_MANIFEST_PATH = Path(__file__).resolve().parents[3] / "infra" / "deployment" / "provider-manifest.v1.json"


class _RejectVendorRedirect(HTTPRedirectHandler):
    """Keep a fixed-host adapter from following a vendor redirect elsewhere."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        raise ValueError("vendor redirects are not allowed")


def _evaluation_manifest_allows(provider_id: str, fallback_provider_id: str | None = None) -> bool:
    """Require the checked-in admission manifest to agree with runtime mode."""
    try:
        manifest = json.loads(PROVIDER_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "provider-admission-manifest/v1":
        return False
    if manifest.get("runtime_status") != "staging_only" or manifest.get("production_admission") != "blocked_pending_written_terms":
        return False
    providers = manifest.get("providers")
    if not isinstance(providers, list):
        return False
    expected_provider_ids = {provider_id}
    if fallback_provider_id is not None:
        expected_provider_ids.add(fallback_provider_id)
    admitted_provider_ids = {
        provider.get("id")
        for provider in providers
        if isinstance(provider, dict)
        and provider.get("id") in expected_provider_ids
        and provider.get("enabled_modes") == ["staging_evaluation"]
        and provider.get("credentials") == "none"
        and provider.get("raw_cache") == "disabled"
        and provider.get("features") == "not_authorized"
        and provider.get("fx") == "not_authorized"
        and provider.get("public_redistribution") == "not_approved"
    }
    if admitted_provider_ids != expected_provider_ids:
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
        and mapping.get("provider") == provider_id
        and (fallback_provider_id is None or mapping.get("fallback_provider") == fallback_provider_id)
        and mapping.get("symbol") == symbol
        and mapping.get("currency") == "USD"
        for asset_id, symbol in ASSET_SYMBOLS.items()
    )


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
    cutoff_end = datetime.combine(_cutoff_date(request.cutoff), datetime.max.time(), tzinfo=timezone.utc)
    if request.requestedAt is not None:
        try:
            candidate = datetime.fromisoformat(request.requestedAt.replace("Z", "+00:00"))
            if candidate.tzinfo is not None:
                return min(candidate.astimezone(timezone.utc), cutoff_end).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    return cutoff_end.isoformat().replace("+00:00", "Z")


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


def _safe_vendor_rows(payload: bytes) -> dict[date, float]:
    if len(payload) > MAX_VENDOR_BYTES:
        raise ValueError("vendor response exceeded the bounded payload")
    document = json.loads(payload.decode("utf-8"))
    result = document.get("chart", {}).get("result") if isinstance(document, dict) else None
    if not isinstance(result, list) or not result or not isinstance(result[0], dict):
        raise ValueError("vendor response had no chart result")
    chart = result[0]
    timestamps = chart.get("timestamp")
    quote = chart.get("indicators", {}).get("quote") if isinstance(chart.get("indicators"), dict) else None
    closes = quote[0].get("close") if isinstance(quote, list) and quote and isinstance(quote[0], dict) else None
    if not isinstance(timestamps, list) or not isinstance(closes, list):
        raise ValueError("vendor response had no close series")
    rows: dict[date, float] = {}
    for timestamp, close in zip(timestamps, closes, strict=False):
        if not isinstance(timestamp, int) or isinstance(timestamp, bool) or not isinstance(close, (int, float)) or isinstance(close, bool):
            continue
        if not math.isfinite(float(close)) or close <= 0:
            continue
        try:
            observed = datetime.fromtimestamp(timestamp, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError) as error:
            raise ValueError("vendor response had an invalid timestamp") from error
        if observed in rows:
            raise ValueError("vendor response had duplicate session dates")
        rows[observed] = float(close)
    return rows


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


class StooqEvaluationAdapter:
    """No-key, no-cache, fixed-host CSV evaluation adapter."""

    endpoint = "https://stooq.com/q/d/l/"

    def fetch(self, symbol: str, anchors: tuple[str, ...], cutoff: date) -> dict[date, float]:
        if symbol not in ASSET_SYMBOLS.values() or not SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError("symbol is outside the fixed provider allowlist")
        anchor_dates = [_date_only(value) for value in anchors]
        valid_dates = [value for value in anchor_dates if value is not None]
        start = min(valid_dates, default=cutoff) - timedelta(days=5)
        query = urlencode({
            "s": f"{symbol.lower()}.us",
            "i": "d",
            "d1": start.strftime("%Y%m%d"),
            "d2": cutoff.strftime("%Y%m%d"),
        })
        request = Request(
            f"{self.endpoint}?{query}",
            headers={"Accept": "text/csv", "User-Agent": "ForesightNine-staging-provider-evaluation/1"},
        )
        with build_opener(_RejectVendorRedirect).open(request, timeout=8) as response:
            payload = response.read(MAX_VENDOR_BYTES + 1)
        return _safe_stooq_rows(payload)


class YahooEvaluationAdapter:
    """No-key, no-cache, fixed-host evaluation adapter for the staging edge."""

    endpoint = "https://query1.finance.yahoo.com/v8/finance/chart/"

    def fetch(self, symbol: str, anchors: tuple[str, ...], cutoff: date) -> dict[date, float]:
        if symbol not in ASSET_SYMBOLS.values() or not SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError("symbol is outside the fixed provider allowlist")
        anchor_dates = [_date_only(value) for value in anchors]
        valid_dates = [value for value in anchor_dates if value is not None]
        start = min(valid_dates, default=cutoff) - timedelta(days=5)
        query = urlencode({
            "period1": int(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp()),
            "period2": int(datetime.combine(cutoff + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp()),
            "interval": "1d",
            "events": "history",
            "includeAdjustedClose": "true",
        })
        request = Request(
            f"{self.endpoint}{symbol}?{query}",
            headers={"Accept": "application/json", "User-Agent": "ForesightNine-staging-provider-evaluation/1"},
        )
        with build_opener(_RejectVendorRedirect).open(request, timeout=8) as response:
            payload = response.read(MAX_VENDOR_BYTES + 1)
        return _safe_vendor_rows(payload)


def build_snapshot_response(request: MarketSnapshotRequest) -> dict[str, Any]:
    """Return a client-compatible bundle fragment with explicit limitations."""

    mode = os.getenv("FORESIGHTNINE_MARKET_PROVIDER_MODE", "unavailable").strip().lower()
    evaluation_enabled = (
        mode in {"public_evaluation", "yahoo_evaluation"}
        and os.getenv("FORESIGHTNINE_ENV", "development").strip().lower() != "production"
        and _evaluation_manifest_allows("stooq", "yahoo_chart_evaluation")
    )
    adapters = (
        ("stooq", StooqEvaluationAdapter()),
        ("yahoo_chart_evaluation", YahooEvaluationAdapter()),
    ) if evaluation_enabled else ()
    warnings = [
        "Linux provider adapter is staging/evaluation-only; no production data license is asserted.",
        "No factor features or FX conversion are supplied; client Public Default must remain disabled.",
        "Only three point-in-time anchors are returned; this is not a backtest window.",
    ]
    snapshots: list[dict[str, Any]] = []
    cutoff = _cutoff_date(request.cutoff)
    for asset_id in request.assetIds:
        symbol = ASSET_SYMBOLS.get(asset_id)
        rows: dict[date, float] = {}
        if adapters and symbol is not None:
            for provider_id, adapter in adapters:
                try:
                    candidate_rows = adapter.fetch(symbol, tuple(request.anchors[anchor] for anchor in ANCHORS), cutoff)
                    if not candidate_rows:
                        raise ValueError("provider returned no usable rows")
                    rows = candidate_rows
                    if provider_id != "stooq":
                        warnings.append("Primary public price provider unavailable; staging fallback was used.")
                    break
                except Exception:
                    warnings.append(f"{provider_id} unavailable for {asset_id}; trying the next staging provider.")
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
            snapshots.append({
                "assetId": asset_id,
                "anchor": anchor,
                "asOf": selected.isoformat(),
                "retrievedAt": _retrieved_at(request),
                "timezone": request.timezone,
                "price": price,
                "close": price,
                "currency": "USD",
                "status": "ok",
                "warnings": ["Price-only evaluation row; no licensed factor or FX fields."],
            })
    return {"snapshots": snapshots, "warnings": sorted(set(warnings))}


__all__ = [
    "ASSET_SYMBOLS",
    "ANCHORS",
    "MarketSnapshotRequest",
    "StooqEvaluationAdapter",
    "YahooEvaluationAdapter",
    "build_snapshot_response",
]
