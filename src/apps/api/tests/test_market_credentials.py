from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.market_credentials import (
    AccountMarketDataSettings,
    MARKET_PROVIDER_ORDER,
    MarketCredentialCipher,
    MarketCredentialError,
    MarketDataConfigUpdate,
)
from app.storage import InMemoryDocumentRepository
from app.main import MARKET_DATA_CONFIGS, app, market_data_settings, reset_state


def _request(*, key: str | None = "personal-secret-1234", clear: bool = False) -> MarketDataConfigUpdate:
    providers = []
    for provider_id in MARKET_PROVIDER_ORDER:
        item = {"provider_id": provider_id, "enabled": provider_id != "finnhub"}
        if provider_id == "alpha_vantage" and key is not None:
            item["api_key"] = key
        if provider_id == "alpha_vantage" and clear:
            item["clear_api_key"] = True
        providers.append(item)
    return MarketDataConfigUpdate.model_validate({"fallback_order": list(reversed(MARKET_PROVIDER_ORDER)), "providers": providers})


def test_account_market_credentials_are_encrypted_masked_and_ordered() -> None:
    repository = InMemoryDocumentRepository()
    service = AccountMarketDataSettings(repository, MarketCredentialCipher(b"x" * 32))
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)

    response = service.update("account_123456", _request(), now=now)

    assert response.fallback_order == list(reversed(MARKET_PROVIDER_ORDER))
    alpha = next(item for item in response.providers if item.provider_id == "alpha_vantage")
    assert alpha.personal_key_configured is True
    assert alpha.key_hint == "••••1234"
    stored = repository["account_123456"].model_dump_json()
    assert "personal-secret-1234" not in stored
    order, credentials = service.runtime("account_123456")
    assert "finnhub" not in order
    assert credentials == {"alpha_vantage": "personal-secret-1234"}


def test_market_key_hint_is_fixed_safe_shape_and_short_keys_are_rejected() -> None:
    service = AccountMarketDataSettings(InMemoryDocumentRepository(), MarketCredentialCipher(b"h" * 32))
    response = service.update("account_123456", _request(key="personal-value-!?尾部"))
    hint = next(item for item in response.providers if item.provider_id == "alpha_vantage").key_hint
    assert hint is not None and hint.startswith("••••") and len(hint) == 8 and hint[4:].isascii() and hint[4:].isalnum()
    assert "!?尾部" not in hint
    with pytest.raises(ValueError):
        _request(key="short")


def test_existing_key_is_preserved_then_can_be_removed() -> None:
    repository = InMemoryDocumentRepository()
    service = AccountMarketDataSettings(repository, MarketCredentialCipher(b"y" * 32))
    service.update("account_123456", _request())
    service.update("account_123456", _request(key=None))
    assert service.runtime("account_123456")[1]["alpha_vantage"] == "personal-secret-1234"

    service.update("account_123456", _request(key=None, clear=True))
    assert "alpha_vantage" not in service.runtime("account_123456")[1]


def test_cipher_binds_ciphertext_to_account_and_provider() -> None:
    cipher = MarketCredentialCipher(b"z" * 32)
    encrypted = cipher.encrypt("account_123456", "alpha_vantage", "secret")
    assert cipher.decrypt("account_123456", "alpha_vantage", encrypted) == "secret"
    with pytest.raises(MarketCredentialError):
        cipher.decrypt("account_other", "alpha_vantage", encrypted)


def test_writing_a_key_fails_closed_without_encryption_configuration() -> None:
    service = AccountMarketDataSettings(InMemoryDocumentRepository(), None)
    with pytest.raises(MarketCredentialError) as caught:
        service.update("account_123456", _request())
    assert caught.value.status_code == 503


def test_authenticated_config_route_never_returns_key_and_delete_cleans_it(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.setattr(market_data_settings, "cipher", MarketCredentialCipher(b"r" * 32))
    reset_state()
    client = TestClient(app)
    registered = client.post("/v1/auth/local/register", json={"email": "market@example.test", "password": "correct horse battery staple!"})
    assert registered.status_code == 201
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

    initial = client.get("/v1/account/market-data-config", headers=headers)
    assert initial.status_code == 200
    body = _request().model_dump(mode="json", exclude_none=True)
    saved = client.put("/v1/account/market-data-config", json=body, headers=headers)
    assert saved.status_code == 200, saved.text
    assert "personal-secret-1234" not in saved.text
    assert next(item for item in saved.json()["providers"] if item["provider_id"] == "alpha_vantage")["key_hint"] == "••••1234"
    account_id = registered.json()["account"]["account_id"]
    assert "personal-secret-1234" not in MARKET_DATA_CONFIGS[account_id].model_dump_json()

    deleted = client.delete("/v1/auth/account", headers=headers)
    assert deleted.status_code == 200
    assert MARKET_DATA_CONFIGS.get(account_id) is None
