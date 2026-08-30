from __future__ import annotations

from datetime import date
import json

from fastapi.testclient import TestClient

from app import market_provider
from app.main import app
from app.market_provider import ANCHORS, MarketSnapshotRequest, StooqEvaluationAdapter, YahooEvaluationAdapter, build_snapshot_response


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
    assert market_provider._evaluation_manifest_allows("stooq", "yahoo_chart_evaluation") is True
    monkeypatch.setattr(market_provider, "PROVIDER_MANIFEST_PATH", tmp_path / "missing.json")
    monkeypatch.setenv("FORESIGHTNINE_MARKET_PROVIDER_MODE", "yahoo_evaluation")
    monkeypatch.setenv("FORESIGHTNINE_ENV", "development")

    response = build_snapshot_response(MarketSnapshotRequest.model_validate(REQUEST))

    assert all(row["status"] == "missing" for row in response["snapshots"])
    assert any("No approved provider" in warning for warning in response["warnings"])


def test_evaluation_mode_requires_asset_level_mapping(monkeypatch, tmp_path) -> None:
    manifest = json.loads(market_provider.PROVIDER_MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest["asset_mappings"][0]["symbol"] = "WRONG"
    manifest_path = tmp_path / "provider-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(market_provider, "PROVIDER_MANIFEST_PATH", manifest_path)

    assert market_provider._evaluation_manifest_allows("stooq", "yahoo_chart_evaluation") is False


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


def test_future_vendor_sessions_are_rejected() -> None:
    try:
        market_provider._reject_future_rows({date(2026, 8, 31): 123.45}, date(2026, 8, 30))
    except ValueError as error:
        assert "after the request cutoff" in str(error)
    else:
        raise AssertionError("future vendor sessions must be rejected")


def test_evaluation_adapter_returns_prices_without_features_or_fx(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_MARKET_PROVIDER_MODE", "yahoo_evaluation")
    monkeypatch.setenv("FORESIGHTNINE_ENV", "development")

    def fake_fetch(self, symbol: str, anchors: tuple[str, ...], cutoff: date) -> dict[date, float]:
        assert symbol == "PAVE"
        assert cutoff == date(2026, 8, 30)
        assert anchors == tuple(REQUEST["anchors"][anchor] for anchor in ANCHORS)
        return {date(2026, 8, 28): 123.45}

    monkeypatch.setattr(StooqEvaluationAdapter, "fetch", lambda *_args: {})
    monkeypatch.setattr(YahooEvaluationAdapter, "fetch", fake_fetch)
    response = build_snapshot_response(MarketSnapshotRequest.model_validate(REQUEST))
    grid_rows = [row for row in response["snapshots"] if row["assetId"] == "GRID"]
    cash_rows = [row for row in response["snapshots"] if row["assetId"] == "CASH"]
    assert grid_rows[-1]["status"] == "ok"
    assert grid_rows[-1]["price"] == 123.45
    assert all("features" not in row and "fx" not in row for row in grid_rows)
    assert all(row["status"] == "missing" for row in cash_rows)
    assert any("No licensed provider mapping" in warning for warning in response["warnings"])


def test_evaluation_adapter_marks_rows_before_the_requested_anchor_stale(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_MARKET_PROVIDER_MODE", "yahoo_evaluation")
    monkeypatch.setenv("FORESIGHTNINE_ENV", "development")
    monkeypatch.setattr(StooqEvaluationAdapter, "fetch", lambda *_args: {})
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
    assert rows[1]["status"] == "stale"
    assert rows[2]["status"] == "stale"
    assert all(any("precedes requested anchor" in warning for warning in row["warnings"]) for row in rows[1:])


def test_canonical_registry_etf_ids_are_allowlisted(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_MARKET_PROVIDER_MODE", "yahoo_evaluation")
    monkeypatch.setenv("FORESIGHTNINE_ENV", "development")
    monkeypatch.setattr(StooqEvaluationAdapter, "fetch", lambda *_args: {})
    monkeypatch.setattr(YahooEvaluationAdapter, "fetch", lambda *_args: {date(2026, 8, 28): 500.0})
    request = MarketSnapshotRequest.model_validate({**REQUEST, "assetIds": ["ETF:SPY"]})
    rows = build_snapshot_response(request)["snapshots"]
    assert len(rows) == 3
    assert all(row["assetId"] == "ETF:SPY" for row in rows)
    assert all(row["currency"] == "USD" for row in rows)


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
