from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main


def _client(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    main.reset_state()
    client = TestClient(main.app)
    token = client.post("/v1/auth/local/register", json={"email": "caps@example.test", "password": "correct horse battery staple!"}).json()["access_token"]
    return client, {"Authorization": f"Bearer {token}"}


def test_capabilities_report_sec_and_research_model_without_configuration_values(monkeypatch) -> None:
    monkeypatch.delenv("FORESIGHTNINE_SEC_USER_AGENT", raising=False)
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setenv("FORESIGHTNINE_RESEARCH_MODEL_PROTOCOL", "anthropic_messages")
    monkeypatch.setenv("FORESIGHTNINE_RESEARCH_LLM_MODEL", "research-model")
    client, headers = _client(monkeypatch)
    assert client.get("/v2/research/capabilities").status_code == 401
    body = client.get("/v2/research/capabilities", headers=headers).json()
    assert body["sec_available"] is False
    sec = {item["name"]: item for item in body["sources"]}
    assert sec["sec_filing"]["configured"] is False and sec["sec_filing"]["hosts"] == ["www.sec.gov"]
    assert sec["official_html_pdf"]["configured"] is True
    assert client.get("/v1/account", headers=headers).json()["research_model"] == "research-model"

    monkeypatch.setenv("FORESIGHTNINE_SEC_USER_AGENT", "Example Research contact@example.test")
    body = client.get("/v2/research/capabilities", headers=headers).json()
    assert body["sec_available"] is True
    assert "contact@example.test" not in str(body)
