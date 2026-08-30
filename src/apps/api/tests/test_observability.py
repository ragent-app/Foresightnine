from __future__ import annotations

import pytest

from app.telemetry import telemetry
from .helpers import CLIENT


def test_metrics_is_fixed_redacted_and_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_METRICS_ENABLED", "true")
    telemetry.record("quota_denial", kind="model_call")

    response = CLIENT.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert 'foresightnine_events_total{event="quota_denial"} 1' in response.text
    assert "model_call" not in response.text
    assert "Authorization" not in response.text

    monkeypatch.setenv("FORESIGHTNINE_METRICS_ENABLED", "false")
    assert CLIENT.get("/metrics").status_code == 404
