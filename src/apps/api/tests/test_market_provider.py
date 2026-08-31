from __future__ import annotations

from datetime import date, timedelta
import json

from fastapi.testclient import TestClient

from app import market_provider
from app.main import app
from app.market_provider import (
    ANCHORS,
    AlphaVantageEvaluationAdapter,
    FinnhubEvaluationAdapter,
    FrankfurterFxAdapter,
    MarketDataEvaluationAdapter,
    MarketSnapshotRequest,
    StooqEvaluationAdapter,
    TwelveDataEvaluationAdapter,
    YahooEvaluationAdapter,
    build_snapshot_response,
)
from app.market_cache import RedisMarketCache


REQUEST = {
    "assetIds": ["GRID", "CASH"],
    "anchors": {
        "year_start": "2025-01-02",
        "latest_month_start": "2026-07-01",
        "latest_completed_session": "2026-08-28",
    },
    "cutoff": "2026-08-30",
    "timezone": "UTC",
    "requestedAt": "2026-08-30T00:00:00Z",
}


def test_request_requires_exactly_the_three_snapshot_anchors() -> None:
    value = dict(REQUEST)
    value["anchors"] = {"latest_month_start": "2026-07-01"}
    try:
        MarketSnapshotRequest.model_validate(value)
    except ValueError as error:
        assert "snapshot anchors" in str(error)
    else:
        raise AssertionError("incomplete anchor requests must be rejected")


def test_default_provider_is_explicitly_limited(monkeypatch) -> None:
    monkeypatch.delenv("FORESIGHTNINE_MARKET_PROVIDER_MODE", raising=False)
    response = build_snapshot_response(MarketSnapshotRequest.model_validate(REQUEST))
    assert len(response["snapshots"]) == 6
    assert all(row["status"] == "missing" for row in response["snapshots"])
    assert any("No approved provider" in warning for warning in response["warnings"])


def test_evaluation_mode_requires_the_admission_manifest(monkeypatch, tmp_path) -> None:
    # The checked-in manifest now admits the owner-licensed Yahoo source.
    assert market_provider._price_manifest_allows("yahoo", "stooq") is True
    monkeypatch.setattr(market_provider, "PROVIDER_MANIFEST_PATH", tmp_path / "missing.json")
    monkeypatch.setenv("FORESIGHTNINE_MARKET_PROVIDER_MODE", "public_evaluation")
    monkeypatch.setenv("FORESIGHTNINE_ENV", "development")

    response = build_snapshot_response(MarketSnapshotRequest.model_validate(REQUEST))

    assert all(row["status"] == "missing" for row in response["snapshots"])
    assert any("No approved provider" in warning for warning in response["warnings"])
    assert "fx" not in response


def test_evaluation_mode_requires_asset_level_mapping(monkeypatch, tmp_path) -> None:
    manifest = json.loads(market_provider.PROVIDER_MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest["asset_mappings"][0]["symbol"] = "WRONG"
    manifest_path = tmp_path / "provider-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(market_provider, "PROVIDER_MANIFEST_PATH", manifest_path)

    assert market_provider._price_manifest_allows("yahoo", "stooq") is False


def test_price_gate_tolerates_only_credential_pending_fallbacks(monkeypatch, tmp_path) -> None:
    manifest = json.loads(market_provider.PROVIDER_MANIFEST_PATH.read_text(encoding="utf-8"))
    stooq = next(provider for provider in manifest["providers"] if provider["id"] == "stooq")
    stooq["enabled_modes"] = []
    stooq["credentials"] = "required_server_secret"
    stooq["authorization_status"] = "licensed_pending_credentials"
    manifest_path = tmp_path / "pending-fallback-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(market_provider, "PROVIDER_MANIFEST_PATH", manifest_path)

    assert market_provider._price_manifest_allows("yahoo", "stooq") is True

    stooq["authorization_status"] = "not_approved"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert market_provider._price_manifest_allows("yahoo", "stooq") is False


def test_stooq_csv_parser_accepts_only_valid_positive_close_rows() -> None:
    payload = b"Date,Open,High,Low,Close,Volume\n2026-08-27,1,2,0.5,123.45,10\n2026-08-28,1,2,0.5,124.50,11\n"
    rows = market_provider._safe_stooq_rows(payload)
    assert rows == {date(2026, 8, 27): 123.45, date(2026, 8, 28): 124.5}


def test_stooq_challenge_payload_is_rejected() -> None:
    try:
        market_provider._safe_stooq_rows(b"<html><title>JavaScript verification</title></html>")
    except ValueError as error:
        assert "CSV shape" in str(error)
    else:
        raise AssertionError("challenge HTML must not be treated as market data")


def _chart_payload(timestamps: list[int], closes: list[float | None]) -> bytes:
    import json as jsonlib

    return jsonlib.dumps({"chart": {"result": [{"timestamp": timestamps, "indicators": {"quote": [{"close": closes}]}}]}}).encode("utf-8")


def test_yahoo_parser_accepts_only_bounded_close_series() -> None:
    rows = market_provider._safe_yahoo_rows(_chart_payload([1759276800, 1759363200], [100.5, None]))
    assert rows == {date(2025, 10, 1): 100.5}

    for payload in (_chart_payload([1759276800, 1759276800], [1.0, 2.0]), _chart_payload([1], [0.0]), b"<html>blocked</html>"):
        try:
            market_provider._safe_yahoo_rows(payload)
        except ValueError:
            continue
        raise AssertionError("malformed vendor payloads must be rejected")


def test_server_provider_parsers_accept_vendor_shapes() -> None:
    alpha = json.dumps({"Time Series (Daily)": {"2026-08-28": {"4. close": "123.45"}}}).encode()
    assert AlphaVantageEvaluationAdapter
    assert market_provider._safe_alpha_vantage_rows(alpha) == {date(2026, 8, 28): 123.45}

    # The parser only needs a valid Unix-second value.
    candle = json.dumps({"s": "ok", "t": [1787961600], "c": [123.45]}).encode()
    assert market_provider._safe_candle_rows(candle) == {date(2026, 8, 29): 123.45}

    twelve = json.dumps({"values": [{"datetime": "2026-08-28", "close": "123.45"}]}).encode()
    assert market_provider._safe_twelve_data_rows(twelve) == {date(2026, 8, 28): 123.45}
    assert MarketDataEvaluationAdapter and FinnhubEvaluationAdapter and TwelveDataEvaluationAdapter


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.values[key] = value
        self.ttls[key] = ttl

    def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)


def test_server_market_cache_prevents_repeat_vendor_fetch(monkeypatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(market_provider, "_admitted_price_providers", lambda: ("yahoo",))
    monkeypatch.setattr(market_provider, "_fx_manifest_allows", lambda _provider: False)
    monkeypatch.setattr(market_provider, "build_market_cache", lambda: RedisMarketCache(fake))
    monkeypatch.setenv("FORESIGHTNINE_MARKET_PROVIDER_MODE", "public_evaluation")
    monkeypatch.setenv("FORESIGHTNINE_ENV", "development")
    calls = 0

    def fetch(_self, _symbol, _anchors, _cutoff):
        nonlocal calls
        calls += 1
        return {date(2026, 8, 28): 123.45}

    monkeypatch.setattr(YahooEvaluationAdapter, "fetch", fetch)
    request = MarketSnapshotRequest.model_validate({**REQUEST, "assetIds": ["GRID"]})
    first = build_snapshot_response(request)
    second = build_snapshot_response(request)
    assert first["snapshots"][-1]["price"] == second["snapshots"][-1]["price"] == 123.45
    assert calls == 1
    assert any(key.startswith("foresightnine:market:v2:") for key in fake.values)


def _enable_isolated_yahoo(monkeypatch, tmp_path) -> None:
    manifest = json.loads(market_provider.PROVIDER_MANIFEST_PATH.read_text(encoding="utf-8"))
    yahoo = next(provider for provider in manifest["providers"] if provider["id"] == "yahoo")
    yahoo["enabled_modes"] = ["staging_evaluation"]
    yahoo["authorization_status"] = "owner_confirmed"
    yahoo["public_redistribution"] = "approved"
    # The credential-pending Stooq fallback stays declared but disabled; the
    # runtime gate must tolerate it and simply never reach it.
    manifest_path = tmp_path / "isolated-authorized-provider-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(market_provider, "PROVIDER_MANIFEST_PATH", manifest_path)


def test_future_vendor_sessions_are_rejected() -> None:
    try:
        market_provider._reject_future_rows({date(2026, 8, 31): 123.45}, date(2026, 8, 30))
    except ValueError as error:
        assert "after the request cutoff" in str(error)
    else:
        raise AssertionError("future vendor sessions must be rejected")


def test_provider_consensus_uses_priority_then_majority() -> None:
    priority = {"frankfurter": 0, "secondary": 1, "tertiary": 1}
    assert market_provider.resolve_provider_consensus(
        [("frankfurter", 7), ("secondary", 8), ("tertiary", 8)], priority,
    ) == 7
    assert market_provider.resolve_provider_consensus(
        [("secondary", 8), ("tertiary", 8)], priority,
    ) == 8
    assert market_provider.resolve_provider_consensus(
        [("secondary", 8), ("tertiary", 9)], priority,
    ) is None


def test_evaluation_adapter_emits_price_rows_without_derivable_features(monkeypatch, tmp_path) -> None:
    _enable_isolated_yahoo(monkeypatch, tmp_path)
    monkeypatch.setenv("FORESIGHTNINE_MARKET_PROVIDER_MODE", "public_evaluation")
    monkeypatch.setenv("FORESIGHTNINE_ENV", "development")

    def fake_fetch(self, symbol: str, anchors: tuple[str, ...], cutoff: date) -> dict[date, float]:
        assert symbol == "PAVE"
        assert cutoff == date(2026, 8, 30)
        assert anchors == tuple(REQUEST["anchors"][anchor] for anchor in ANCHORS)
        return {date(2026, 8, 28): 123.45}

    monkeypatch.setattr(YahooEvaluationAdapter, "fetch", fake_fetch)
    response = build_snapshot_response(MarketSnapshotRequest.model_validate(REQUEST))
    grid_rows = [row for row in response["snapshots"] if row["assetId"] == "GRID"]
    cash_rows = [row for row in response["snapshots"] if row["assetId"] == "CASH"]
    assert grid_rows[-1]["status"] == "ok"
    assert grid_rows[-1]["price"] == 123.45
    # A single fetched session cannot derive any point-in-time feature, so the
    # row stays honest and feature-free.
    assert all("features" not in row for row in grid_rows)
    assert all(row["status"] == "missing" for row in cash_rows)
    assert any("No licensed provider mapping" in warning for warning in response["warnings"])


def test_evaluation_adapter_derives_point_in_time_features(monkeypatch, tmp_path) -> None:
    _enable_isolated_yahoo(monkeypatch, tmp_path)
    monkeypatch.setenv("FORESIGHTNINE_MARKET_PROVIDER_MODE", "public_evaluation")
    monkeypatch.setenv("FORESIGHTNINE_ENV", "development")

    # A rising weekday series with more than a year of history gives every
    # feature real context, and each feature only uses sessions at or before
    # its own anchor.
    series: dict[date, float] = {}
    current, index = date(2025, 8, 1), 0
    while current <= date(2026, 8, 28):
        if current.weekday() < 5:
            series[current] = 100.0 + index * 0.5
            index += 1
        current += timedelta(days=1)
    monkeypatch.setattr(YahooEvaluationAdapter, "fetch", lambda *_args: series)
    request = MarketSnapshotRequest.model_validate({**REQUEST, "assetIds": ["GRID"]})

    rows = build_snapshot_response(request)["snapshots"]

    latest = rows[-1]
    assert latest["status"] == "ok"
    features = latest["features"]
    assert features["momentum"] is not None and features["momentum"] > 0.0
    assert features["momentum_month"] is not None and features["momentum_month"] > 0.0
    assert features["rsi14"] == 100.0  # monotonically rising closes
    assert features["sma_ratio"] is not None
    # Independently recompute momentum from the same series: the anchor value
    # must equal close/close_365d_ago - 1 exactly, proving no future session
    # leaked into the derived feature.
    latest_session = date(2026, 8, 28)
    prior = max(observed for observed in series if observed <= latest_session - timedelta(days=365))
    assert features["momentum"] == series[latest_session] / series[prior] - 1.0


def test_price_fallback_uses_the_next_admitted_provider(monkeypatch, tmp_path) -> None:
    manifest = json.loads(market_provider.PROVIDER_MANIFEST_PATH.read_text(encoding="utf-8"))
    stooq = next(provider for provider in manifest["providers"] if provider["id"] == "stooq")
    stooq["enabled_modes"] = ["staging_evaluation"]
    stooq["authorization_status"] = "owner_confirmed"
    stooq["public_redistribution"] = "approved"
    stooq["credentials"] = "optional_server_secret"
    manifest_path = tmp_path / "fallback-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(market_provider, "PROVIDER_MANIFEST_PATH", manifest_path)
    monkeypatch.setenv("FORESIGHTNINE_MARKET_PROVIDER_MODE", "public_evaluation")
    monkeypatch.setenv("FORESIGHTNINE_ENV", "development")
    monkeypatch.setattr(YahooEvaluationAdapter, "fetch", lambda *_args: (_ for _ in ()).throw(ValueError("primary unavailable")))
    monkeypatch.setattr(StooqEvaluationAdapter, "fetch", lambda *_args: {date(2026, 8, 28): 90.0})

    rows = build_snapshot_response(MarketSnapshotRequest.model_validate({**REQUEST, "assetIds": ["GRID"]}))["snapshots"]

    assert rows[-1]["status"] == "ok"
    assert rows[-1]["price"] == 90.0


def test_evaluation_adapter_marks_rows_before_the_requested_anchor_stale(monkeypatch, tmp_path) -> None:
    _enable_isolated_yahoo(monkeypatch, tmp_path)
    monkeypatch.setenv("FORESIGHTNINE_MARKET_PROVIDER_MODE", "public_evaluation")
    monkeypatch.setenv("FORESIGHTNINE_ENV", "development")
    monkeypatch.setattr(YahooEvaluationAdapter, "fetch", lambda *_args: {
        date(2026, 8, 2): 490.0,
        date(2026, 8, 27): 500.0,
    })
    request = MarketSnapshotRequest.model_validate({
        **REQUEST,
        "assetIds": ["ETF:SPY"],
        "anchors": {
            "year_start": "2026-01-01",
            "latest_month_start": "2026-08-03",
            "latest_completed_session": "2026-08-28",
        },
    })

    rows = build_snapshot_response(request)["snapshots"]

    assert rows[0]["status"] == "missing"
    assert rows[1]["status"] == "ok"  # 1-day non-trading gap on a month anchor is calendar reality
    assert rows[2]["status"] == "stale"  # the latest-session anchor stays strict
    assert any("non-trading date" in warning for warning in rows[1]["warnings"])
    assert any("precedes requested anchor" in warning for warning in rows[2]["warnings"])


def test_canonical_registry_etf_ids_are_allowlisted(monkeypatch, tmp_path) -> None:
    _enable_isolated_yahoo(monkeypatch, tmp_path)
    monkeypatch.setenv("FORESIGHTNINE_MARKET_PROVIDER_MODE", "public_evaluation")
    monkeypatch.setenv("FORESIGHTNINE_ENV", "development")
    monkeypatch.setattr(YahooEvaluationAdapter, "fetch", lambda *_args: {date(2026, 8, 28): 500.0})
    request = MarketSnapshotRequest.model_validate({**REQUEST, "assetIds": ["ETF:SPY"]})
    rows = build_snapshot_response(request)["snapshots"]
    assert len(rows) == 3
    assert all(row["assetId"] == "ETF:SPY" for row in rows)
    assert all(row["currency"] == "USD" for row in rows)


def test_frankfurter_payload_is_parsed_into_cny_per_unit() -> None:
    import json as jsonlib

    payload = jsonlib.dumps({"amount": 1.0, "base": "CNY", "date": "2026-08-28", "rates": {"EUR": 0.12779, "USD": 0.14879}}).encode("utf-8")
    fx = market_provider._safe_frankfurter_document(payload, date(2026, 8, 30))
    assert fx["asOf"] == "2026-08-28"
    assert fx["provider"] == "frankfurter"
    assert fx["cnyPerUnit"]["CNY"] == 1.0
    assert abs(fx["cnyPerUnit"]["USD"] - 1.0 / 0.14879) < 1e-9
    assert abs(fx["cnyPerUnit"]["EUR"] - 1.0 / 0.12779) < 1e-9


def test_frankfurter_requires_the_full_currency_coverage() -> None:
    import json as jsonlib

    partial = jsonlib.dumps({"base": "CNY", "date": "2026-08-28", "rates": {"USD": 0.14879}}).encode("utf-8")
    for payload in (partial, b"<html>blocked</html>"):
        try:
            market_provider._safe_frankfurter_document(payload, date(2026, 8, 30))
        except ValueError:
            continue
        raise AssertionError("incomplete or non-JSON FX payloads must be rejected")
    future = jsonlib.dumps({"base": "CNY", "date": "2026-08-31", "rates": {"EUR": 0.1, "USD": 0.1}}).encode("utf-8")
    try:
        market_provider._safe_frankfurter_document(future, date(2026, 8, 30))
    except ValueError as error:
        assert "after the request cutoff" in str(error)
    else:
        raise AssertionError("future FX dates must be rejected")
    same_day = jsonlib.dumps({"base": "CNY", "date": "2026-08-28", "rates": {"EUR": 0.1, "USD": 0.1}}).encode("utf-8")
    try:
        market_provider._safe_frankfurter_document(same_day, date(2026, 8, 27))
    except ValueError:
        pass
    else:
        raise AssertionError("same-day FX cannot pass an intraday cutoff boundary")


def test_vendor_redirects_are_rejected() -> None:
    handler = market_provider._RejectVendorRedirect()
    try:
        handler.redirect_request(None, None, 302, "Found", {}, "https://untrusted.example/")
    except ValueError as error:
        assert "redirects are not allowed" in str(error)
    else:
        raise AssertionError("vendor redirects must not be followed")


def test_snapshot_route_is_client_compatible(monkeypatch) -> None:
    monkeypatch.delenv("FORESIGHTNINE_MARKET_PROVIDER_MODE", raising=False)
    response = TestClient(app).post("/snapshots", json=REQUEST)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["snapshots"]) == 6
    assert all(set(row) >= {"assetId", "anchor", "status", "price", "close"} for row in payload["snapshots"])
