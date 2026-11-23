import json

from fastapi.testclient import TestClient

import app.main as main
import app.model_discovery as discovery
import app.openai_compatible as chat
from app.model_credentials import ModelCredentialCipher


def test_catalog_uses_draft_or_bound_key_and_only_returns_model_metadata(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.setattr(chat, "_require_public_dns", lambda *_: None)
    monkeypatch.setattr(main.model_settings, "cipher", ModelCredentialCipher(b"d" * 32))
    client = TestClient(main.app)
    auth = client.post("/v1/auth/local/register", json={
        "email": "catalog@example.test", "password": "correct horse battery staple!"
    }).json()
    headers = {"Authorization": "Bearer " + auth["access_token"]}
    path = "/v1/account/model-config"
    draft = {"protocol": "openai_chat_completions", "base_url": "https://example.test/v1/chat/completions"}
    key = "fixture-catalog-key"
    seen = []

    def transport(method, url, auth, body, timeout, **kwargs):
        assert method == "GET" and not body
        assert auth["Authorization"] == "Bearer " + key
        seen.append(url)
        return chat.HttpResponse(200, json.dumps({"data": [
            {"id": "with-effort", "supported_reasoning_efforts": ["low", "high", key], "private": key},
            {"id": "unspecified"}, {"id": key}]}))

    monkeypatch.setattr(discovery, "_default_transport", transport)
    result = client.post(path + "/models", headers=headers, json={**draft, "api_key": key})
    assert result.headers["cache-control"] == "no-store"
    assert result.json() == {"models": [
        {"id": "with-effort", "efforts": ["low", "high"], "effort_source": "model_api"}, {"id": "unspecified"},
    ]}
    assert key not in result.text
    assert "key_hint" not in client.get(path, headers=headers).json()
    client.put(path, headers=headers, json={**draft, "mode": "personal", "api_key": key, "model": "unspecified"})
    assert client.post(path + "/models", headers=headers, json=draft).json() == result.json()
    for override in ({"base_url": "https://other.test/v1"}, {"base_url": "https://example.test/elsewhere"},
                     {"protocol": "anthropic_messages", "version": "version", "auth_scheme": "bearer"},
                     {"base_url": "https://127.0.0.1/v1", "api_key": key}):
        assert client.post(path + "/models", headers=headers, json={**draft, **override}).status_code == 422
    assert seen == ["https://example.test/v1/models"] * 2


def test_official_effort_fallback_reads_exact_public_card_without_credentials(monkeypatch):
    monkeypatch.setattr(chat, "_require_public_dns", lambda *_: None)
    calls = []

    def transport(method, url, headers, body, timeout, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return chat.HttpResponse(200, '{"id":"sample","owned_by":"system"}')
        assert "Authorization" not in headers
        return chat.HttpResponse(200, "Model ID: `sample`\nReasoning.effort supports: none, low (default), and ultra.")

    monkeypatch.setattr(discovery, "_default_transport", transport)
    req = discovery.ModelDiscoveryRequest(protocol="openai_chat_completions", base_url="https://api.openai.com/v1",
                                          api_key="fixture-key", model="sample")
    model, = discovery.discover_models("account:test", req, main.model_settings).models
    assert model.efforts == ["none", "low", "ultra"] and model.effort_source == "official_docs"
    assert calls == ["https://api.openai.com/v1/models/sample", "https://developers.openai.com/api/docs/models/sample.md"]


def test_messages_catalog_pagination_auth_and_explicit_capabilities(monkeypatch):
    monkeypatch.delenv("FORESIGHTNINE_MESSAGES_PERSONAL_BASE_URL", raising=False)
    monkeypatch.setattr(chat, "_require_public_dns", lambda *_: None)

    def transport(method, url, headers, body, timeout, **kwargs):
        assert headers["x-api-key"] == "fixture-key" and headers["anthropic-version"] == "version"
        if url.endswith("/v1/models"):
            return chat.HttpResponse(200, json.dumps({"data": [{"id": "first"}], "has_more": True, "last_id": "first"}))
        assert url.endswith("/v1/models?after_id=first")
        return chat.HttpResponse(200, json.dumps({"data": [{"id": "second", "capabilities": {"effort": {
            "supported": True, "high": {"supported": True}, "low": {"supported": False},
        }}}]}))

    monkeypatch.setattr(discovery, "_default_transport", transport)
    req = discovery.ModelDiscoveryRequest(protocol="anthropic_messages", base_url="https://example.test", api_key="fixture-key",
                                          version="version", auth_scheme="x-api-key")
    models = discovery.discover_models("account:test", req, main.model_settings).models
    assert [(m.id, m.efforts) for m in models] == [("first", None), ("second", ["high"])]
