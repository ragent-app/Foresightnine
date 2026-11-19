from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest
from fastapi.testclient import TestClient

import app.main as main
import app.openai_compatible as compatible
from app.model_credentials import (
    AccountModelSettings,
    ModelConfigUpdate,
    ModelCredentialCipher,
    ModelCredentialError,
)
from app.phase3 import DeterministicMockLlmProvider, ProviderFailure
from app.storage import InMemoryDocumentRepository


SYNTHETIC_MODEL_KEY = "personal-" + "test-value-1234"


def _personal_request(*, api_key: str | None = SYNTHETIC_MODEL_KEY, **overrides: object) -> ModelConfigUpdate:
    body: dict[str, object] = {
        "mode": "personal",
        "base_url": "https://models.example.test/v1",
        "model": "test-model",
    }
    if api_key is not None:
        body["api_key"] = api_key
    body.update(overrides)
    return ModelConfigUpdate.model_validate(body)


def test_model_credentials_encrypt_mask_and_bind_to_account(monkeypatch) -> None:
    monkeypatch.setattr(compatible, "_require_public_dns", lambda *_args, **_kwargs: None)
    repository = InMemoryDocumentRepository()
    service = AccountModelSettings(repository, ModelCredentialCipher(b"m" * 32))

    response = service.update("account_123456", _personal_request(), now=datetime(2026, 9, 13, tzinfo=timezone.utc))

    assert response.mode == "personal"
    assert response.configured is True
    assert response.key_hint == "••••1234"
    stored = repository["account_123456"].model_dump_json()
    assert SYNTHETIC_MODEL_KEY not in stored
    assert service.runtime_settings("account_123456").api_key == SYNTHETIC_MODEL_KEY
    ciphertext = repository["account_123456"].encrypted_api_key
    assert ciphertext is not None
    with pytest.raises(ModelCredentialError):
        service.cipher.decrypt("account_other", ciphertext)


def test_switching_to_platform_keeps_the_saved_personal_configuration(monkeypatch) -> None:
    monkeypatch.setattr(compatible, "_require_public_dns", lambda *_args, **_kwargs: None)
    repository = InMemoryDocumentRepository()
    service = AccountModelSettings(repository, ModelCredentialCipher(b"k" * 32))
    service.update("account_123456", _personal_request(protocol="anthropic_messages"), now=datetime(2026, 11, 19, tzinfo=timezone.utc))

    platform = service.update("account_123456", ModelConfigUpdate(mode="platform"), now=datetime(2026, 11, 19, 1, tzinfo=timezone.utc))

    # Platform mode is active: no personal credentials reach the runtime.
    assert platform.mode == "platform"
    assert service.runtime_settings("account_123456") is None
    # The inactive personal configuration is reported (masked) and kept encrypted.
    assert platform.model_dump(exclude_none=True) == {
        "schema_version": "v1", "mode": "platform", "configured": True, "protocol": "anthropic_messages",
        "base_url": "https://models.example.test/v1", "model": "test-model", "key_hint": "••••1234",
        "updated_at": datetime(2026, 11, 19, 1, tzinfo=timezone.utc),
    }
    assert SYNTHETIC_MODEL_KEY not in repository["account_123456"].model_dump_json()

    # Switching back needs no fields: the saved origin, protocol and key are reused.
    personal = service.update("account_123456", ModelConfigUpdate(mode="personal"), now=datetime(2026, 11, 19, 2, tzinfo=timezone.utc))
    assert personal.mode == "personal"
    assert personal.protocol == "anthropic_messages"
    assert service.runtime_settings("account_123456").api_key == SYNTHETIC_MODEL_KEY

    # Delete still removes everything.
    service.delete("account_123456")
    assert service.snapshot("account_123456").model_dump(exclude_none=True) == {"schema_version": "v1", "mode": "platform", "configured": True}


def test_platform_switch_without_saved_configuration_stays_minimal() -> None:
    service = AccountModelSettings(InMemoryDocumentRepository(), ModelCredentialCipher(b"q" * 32))
    snapshot = service.update("account_123456", ModelConfigUpdate(mode="platform"), now=datetime(2026, 11, 19, tzinfo=timezone.utc))
    assert snapshot.model_dump(exclude_none=True) == {
        "schema_version": "v1", "mode": "platform", "configured": True, "updated_at": datetime(2026, 11, 19, tzinfo=timezone.utc),
    }
    with pytest.raises(ModelCredentialError):
        service.update("account_123456", ModelConfigUpdate(mode="personal"))


def test_personal_configuration_rejects_ssrf_and_missing_encryption(monkeypatch) -> None:
    monkeypatch.setattr(compatible, "_require_public_dns", lambda *_args, **_kwargs: None)
    service = AccountModelSettings(InMemoryDocumentRepository(), ModelCredentialCipher(b"n" * 32))
    for base_url in (
        "http://models.example.test/v1",
        "https://user@models.example.test/v1",
        "https://models.example.test:8443/v1",
        "https://localhost/v1",
        "https://169.254.169.254/v1",
        "https://metadata.google.internal/v1",
        "https://10.0.0.1/v1",
    ):
        with pytest.raises(ModelCredentialError) as caught:
            service.update("account_123456", _personal_request(base_url=base_url))
        assert caught.value.status_code == 422

    with pytest.raises(ModelCredentialError) as caught:
        AccountModelSettings(InMemoryDocumentRepository(), None).update("account_123456", _personal_request())
    assert caught.value.status_code == 503

    with pytest.raises(ValueError):
        ModelConfigUpdate.model_validate({"mode": "platform", "api_key": "must-not-" + "be-accepted"})


def test_personal_configuration_converts_dns_transport_failure_to_validation_error(monkeypatch) -> None:
    monkeypatch.setattr(compatible, "_require_public_dns", lambda *_args, **_kwargs: (_ for _ in ()).throw(compatible._TransportFailure()))
    service = AccountModelSettings(InMemoryDocumentRepository(), ModelCredentialCipher(b"d" * 32))

    with pytest.raises(ModelCredentialError) as error:
        service.update("account_123456", _personal_request())

    assert error.value.status_code == 422
    assert error.value.code == "validation_error"


def test_non_alphanumeric_secret_suffix_is_never_reflected_in_hint(monkeypatch) -> None:
    monkeypatch.setattr(compatible, "_require_public_dns", lambda *_args, **_kwargs: None)
    service = AccountModelSettings(InMemoryDocumentRepository(), ModelCredentialCipher(b"h" * 32))
    punctuated_key = "personal-value-" + "!@#$"
    response = service.update("account_123456", _personal_request(api_key=punctuated_key))
    assert response.key_hint is not None
    assert response.key_hint.startswith("••••")
    assert response.key_hint != "••••!@#$"
    assert response.key_hint[4:].isalnum()


def test_authenticated_routes_isolate_keys_clear_and_route_personal_without_billing(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.setattr(compatible, "_require_public_dns", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main.model_settings, "cipher", ModelCredentialCipher(b"p" * 32))
    main.reset_state()
    client = TestClient(main.app)

    test_password = "correct horse " + "battery staple!"
    first = client.post("/v1/auth/local/register", json={"email": "model-one@example.test", "password": test_password})
    second = client.post("/v1/auth/local/register", json={"email": "model-two@example.test", "password": test_password})
    assert first.status_code == second.status_code == 201
    first_headers = {"Authorization": f"Bearer {first.json()['access_token']}"}
    second_headers = {"Authorization": f"Bearer {second.json()['access_token']}"}

    saved = client.put("/v1/account/model-config", headers=first_headers, json=_personal_request().model_dump(exclude_none=True))
    assert saved.status_code == 200, saved.text
    assert SYNTHETIC_MODEL_KEY not in saved.text
    assert saved.json()["key_hint"] == "••••1234"
    assert client.get("/v1/account/model-config", headers=second_headers).json() == {
        "schema_version": "v1", "mode": "platform", "configured": True,
    }
    assert client.post("/v1/account/model-config/test", headers=second_headers).json() == {"status": "unavailable"}
    assert SYNTHETIC_MODEL_KEY not in main.MODEL_CONFIGS[first.json()["account"]["account_id"]].model_dump_json()

    captured: list[object] = []

    class PersonalProvider:
        name = "openai-compatible"

        def __init__(self, settings: object) -> None:
            captured.append(settings)
            self._inner = DeterministicMockLlmProvider()

        @property
        def supports_native_search(self) -> bool:
            return False

        def generate_challenge(self, request, **_kwargs):
            return self._inner.generate_challenge(request)

        def connection_ready(self) -> bool:
            return True

    monkeypatch.setattr(main, "OpenAICompatibleLlmProvider", PersonalProvider)
    session = client.post("/v1/sessions", json={}, headers={**first_headers, "Idempotency-Key": "personal-model-session"})
    assert session.status_code == 201
    challenge = client.post(
        "/v1/challenges",
        headers={**first_headers, "Idempotency-Key": "personal-model-challenge"},
        json={
            "session_id": session.json()["session"]["session_id"],
            "thesis": "A personal-model request must not consume platform tokens.",
            "capital": 100_000,
            "base_currency": "USD",
            "user_private_evidence_claims": False,
        },
    )
    assert challenge.status_code == 201, challenge.text
    assert len(captured) == 1
    assert captured[0].api_key == SYNTHETIC_MODEL_KEY
    assert not any(item.account_id == first.json()["account"]["account_id"] for item in main.USAGE_LEDGER.values())
    assert challenge.json()["challenge"]["source_refs"][0]["ref_id"] == "provider:account-openai-compatible"

    tested = client.post("/v1/account/model-config/test", headers=first_headers)
    assert tested.status_code == 200
    assert tested.json() == {"status": "ready"}
    deleted = client.delete("/v1/account/model-config", headers=first_headers)
    assert deleted.status_code == 200
    assert deleted.json() == {"schema_version": "v1", "mode": "platform", "configured": True}
    assert main.MODEL_CONFIGS.get(first.json()["account"]["account_id"]) is None

    resaved = client.put("/v1/account/model-config", headers=first_headers, json=_personal_request().model_dump(exclude_none=True))
    assert resaved.status_code == 200
    account_deleted = client.delete("/v1/auth/account", headers=first_headers)
    assert account_deleted.status_code == 200
    assert main.MODEL_CONFIGS.get(first.json()["account"]["account_id"]) is None


def test_invalid_auth_and_personal_failure_never_fall_back_or_bill(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.setattr(compatible, "_require_public_dns", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main.model_settings, "cipher", ModelCredentialCipher(b"q" * 32))
    main.reset_state()
    client = TestClient(main.app)
    registered = client.post("/v1/auth/local/register", json={"email": "no-fallback@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    saved = client.put("/v1/account/model-config", headers=headers, json=_personal_request().model_dump(exclude_none=True))
    assert saved.status_code == 200
    session = client.post("/v1/sessions", json={}, headers={**headers, "Idempotency-Key": "no-fallback-session"}).json()["session"]

    platform_calls = 0

    class PlatformProvider:
        def generate_challenge(self, _request):
            nonlocal platform_calls
            platform_calls += 1
            raise AssertionError("platform provider must not be called")

    class FailingPersonalProvider:
        name = "openai-compatible"

        def __init__(self, _settings: object) -> None:
            pass

        @property
        def supports_native_search(self) -> bool:
            return False

        def generate_challenge(self, _request, **_kwargs):
            raise ProviderFailure("provider_unavailable", "Personal provider unavailable.", retryable=True)

    monkeypatch.setattr(main, "challenge_provider", PlatformProvider())
    monkeypatch.setattr(main, "OpenAICompatibleLlmProvider", FailingPersonalProvider)
    request_body = {
        "session_id": session["session_id"],
        "thesis": "A failing personal model must not reach the platform model.",
        "capital": 100_000,
        "base_currency": "USD",
        "user_private_evidence_claims": False,
    }
    failed = client.post("/v1/challenges", headers={**headers, "Idempotency-Key": "personal-failure"}, json=request_body)
    assert failed.status_code == 503
    assert platform_calls == 0
    account_id = registered.json()["account"]["account_id"]
    assert not any(item.account_id == account_id for item in main.USAGE_LEDGER.values())

    invalid = client.post(
        "/v1/challenges",
        headers={"Authorization": "Bearer " + "expired-test-token", "Idempotency-Key": "invalid-auth-no-fallback"},
        json=request_body,
    )
    assert invalid.status_code == 401
    assert platform_calls == 0
    assert not any(item.account_id == account_id for item in main.USAGE_LEDGER.values())


def test_operation_receipts_and_idempotency_are_account_scoped(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    main.reset_state()
    client = TestClient(main.app)
    synthetic_passphrase = "correct horse " + "battery staple!"
    account_a = client.post("/v1/auth/local/register", json={"email": "operation-a@example.test", "password": synthetic_passphrase})
    account_b = client.post("/v1/auth/local/register", json={"email": "operation-b@example.test", "password": synthetic_passphrase})
    assert account_a.status_code == account_b.status_code == 201
    headers_a = {"Authorization": f"Bearer {account_a.json()['access_token']}"}
    headers_b = {"Authorization": f"Bearer {account_b.json()['access_token']}"}

    session = client.post(
        "/v1/sessions",
        headers={**headers_a, "Idempotency-Key": "account-scoped-session"},
        json={},
    ).json()["session"]
    body = {
        "session_id": session["session_id"],
        "thesis": "Operation receipts must stay bound to the account that created them.",
        "capital": 100_000,
        "base_currency": "USD",
        "user_private_evidence_claims": False,
    }
    idempotency_key = "same-key-across-two-accounts"
    accepted_a = client.post(
        "/v1/challenges/async",
        headers={**headers_a, "Idempotency-Key": idempotency_key},
        json=body,
    )
    assert accepted_a.status_code == 202, accepted_a.text
    operation_a = accepted_a.json()["operation"]

    replay_a = client.post(
        "/v1/challenges/async",
        headers={**headers_a, "Idempotency-Key": idempotency_key},
        json=body,
    )
    assert replay_a.status_code == 202
    assert replay_a.headers["Idempotency-Replayed"] == "true"
    assert replay_a.json()["operation"]["operation_id"] == operation_a["operation_id"]

    accepted_b = client.post(
        "/v1/challenges/async",
        headers={**headers_b, "Idempotency-Key": idempotency_key},
        json=body,
    )
    assert accepted_b.status_code == 404
    assert client.post(
        "/v1/challenges/async",
        headers={"Idempotency-Key": idempotency_key},
        json=body,
    ).status_code == 404

    market_body = {
        "asset_ids": ["ETF:SPY"],
        "field_set": ["close"],
        "from": "2026-08-01T00:00:00Z",
        "to": "2026-08-10T00:00:00Z",
    }
    market_a = client.post(
        "/v1/market-data/manifests",
        headers={**headers_a, "Idempotency-Key": "same-stateless-key"},
        json=market_body,
    )
    market_b = client.post(
        "/v1/market-data/manifests",
        headers={**headers_b, "Idempotency-Key": "same-stateless-key"},
        json=market_body,
    )
    assert market_a.status_code == market_b.status_code == 201
    assert market_a.json()["operation"]["operation_id"] != market_b.json()["operation"]["operation_id"]

    operation_url = f"/v1/operations/{operation_a['operation_id']}"
    assert client.get(f"{operation_url}/events", headers=headers_b).status_code == 404
    assert client.get(f"{operation_url}/events").status_code == 404
    assert client.post(
        f"{operation_url}/cancel",
        headers={**headers_b, "Idempotency-Key": "cross-account-cancel"},
        json={"cancel_token": operation_a["cancel_token"]},
    ).status_code == 404

    stream_a = client.get(
        f"{operation_url}/events",
        headers={**headers_a, "Last-Event-ID": operation_a["last_event_id"]},
    )
    assert stream_a.status_code == 200
    events = [json.loads(line.removeprefix("data: ")) for line in stream_a.text.splitlines() if line.startswith("data: ")]
    completed = next(item for item in events if item["event"] == "operation.complete")
    challenge_id = completed["result_ref"]
    assert client.get(
        f"/v1/challenges/{challenge_id}",
        params={"operation_id": operation_a["operation_id"]},
        headers=headers_b,
    ).status_code == 404
    assert client.get(
        f"/v1/challenges/{challenge_id}",
        params={"operation_id": operation_a["operation_id"]},
        headers=headers_a,
    ).status_code == 200

@pytest.mark.parametrize("api_key", ["short", "四五六七"])
def test_model_key_minimum_length_prevents_full_hint_disclosure(api_key: str) -> None:
    with pytest.raises(ValueError):
        _personal_request(api_key=api_key)


def test_api_messages_protocol_and_credential_origin_rebinding(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setattr(compatible, '_require_public_dns', lambda *_a, **_k: None)
    monkeypatch.setattr(main.model_settings, 'cipher', ModelCredentialCipher(b'm'*32))
    client = TestClient(main.app)
    auth = client.post('/v1/auth/local/register', json={'email':'origin@example.test','password':'correct horse battery staple!'}).json()
    headers = {'Authorization':'Bearer '+auth['access_token']}
    body = _personal_request().model_dump(exclude_none=True)
    assert client.put('/v1/account/model-config',headers=headers,json=body).status_code == 200
    assert client.put('/v1/account/model-config',headers=headers,json={'mode':'personal','base_url':'https://elsewhere.example.test/v1'}).status_code == 422
    assert client.put('/v1/account/model-config',headers=headers,json={'mode':'personal','protocol':'anthropic_messages'}).status_code == 422
    assert client.put('/v1/account/model-config',headers=headers,json={'mode':'personal','base_url':'https://bad.example.test:bad/v1'}).status_code == 422
    changed = client.put('/v1/account/model-config',headers=headers,json={'mode':'personal','protocol':'anthropic_messages','api_key':SYNTHETIC_MODEL_KEY})
    assert changed.status_code == 200 and changed.json()['protocol'] == 'anthropic_messages'
    assert client.post('/v1/account/model-config/test',headers=headers).json() == {'status':'unavailable'}


def test_runtime_configuration_resolves_protocol_and_key_from_one_read(monkeypatch):
    monkeypatch.setattr(compatible, '_require_public_dns', lambda *_a, **_k: None)
    repository = InMemoryDocumentRepository()
    service = AccountModelSettings(repository, ModelCredentialCipher(b'r'*32))
    account_id = 'account:atomic'
    service.update(account_id, _personal_request(protocol='anthropic_messages'))
    original = repository.get
    reads = []
    def counted(key):
        reads.append(key)
        return original(key)
    monkeypatch.setattr(repository, 'get', counted)
    settings, revision = service.runtime_configuration(account_id)
    assert len(reads) == 1 and revision.protocol == 'anthropic_messages'
    assert settings.api_key == SYNTHETIC_MODEL_KEY and settings.base_url == revision.base_url
