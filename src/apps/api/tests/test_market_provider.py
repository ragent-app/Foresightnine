from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from app import market_provider
from app.main import app
from app.market_provider import ANCHORS, MarketSnapshotRequest, YahooEvaluationAdapter, build_snapshot_response


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
    assert market_provider._evaluation_manifest_allows("yahoo_chart_evaluation") is True
    monkeypatch.setattr(market_provider, "PROVIDER_MANIFEST_PATH", tmp_path / "missing.json")
    monkeypatch.setenv("FORESIGHTNINE_MARKET_PROVIDER_MODE", "yahoo_evaluation")
    monkeypatch.setenv("FORESIGHTNINE_ENV", "development")

    response = build_snapshot_response(MarketSnapshotRequest.model_validate(REQUEST))

    assert all(row["status"] == "missing" for row in response["snapshots"])
    assert any("No approved provider" in warning for warning in response["warnings"])


def test_evaluation_adapter_returns_prices_without_features_or_fx(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_MARKET_PROVIDER_MODE", "yahoo_evaluation")
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
    assert all("features" not in row and "fx" not in row for row in grid_rows)
    assert all(row["status"] == "missing" for row in cash_rows)
    assert any("No licensed provider mapping" in warning for warning in response["warnings"])


def test_canonical_registry_etf_ids_are_allowlisted(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_MARKET_PROVIDER_MODE", "yahoo_evaluation")
    monkeypatch.setenv("FORESIGHTNINE_ENV", "development")
    monkeypatch.setattr(YahooEvaluationAdapter, "fetch", lambda *_args: {date(2026, 8, 28): 500.0})
    request = MarketSnapshotRequest.model_validate({**REQUEST, "assetIds": ["ETF:SPY"]})
    rows = build_snapshot_response(request)["snapshots"]
    assert len(rows) == 3
    assert all(row["assetId"] == "ETF:SPY" for row in rows)
    assert all(row["currency"] == "USD" for row in rows)


def test_snapshot_route_is_client_compatible(monkeypatch) -> None:
    monkeypatch.delenv("FORESIGHTNINE_MARKET_PROVIDER_MODE", raising=False)
    response = TestClient(app).post("/snapshots", json=REQUEST)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["snapshots"]) == 6
    assert all(set(row) >= {"assetId", "anchor", "status", "price", "close"} for row in payload["snapshots"])
