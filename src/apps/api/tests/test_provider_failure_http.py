from fastapi.testclient import TestClient

from app import main as main_module
from app.main import app, reset_state


def test_retryable_provider_failure_is_a_retryable_service_response(monkeypatch) -> None:
    class FailingProvider:
        name = "failing-provider"
        supports_native_search = False

        def generate_challenge(self, _request):
            raise main_module.ProviderFailure("provider_timeout", "provider detail", retryable=True)

    reset_state()
    previous_provider = main_module.challenge_provider
    main_module.challenge_provider = FailingProvider()
    try:
        client = TestClient(app)
        session = client.post(
            "/v1/sessions",
            headers={"Idempotency-Key": "provider-http-session"},
            json={"client_locale": "en-US"},
        )
        response = client.post(
            "/v1/challenges",
            headers={"Idempotency-Key": "provider-http-challenge"},
            json={
                "session_id": session.json()["session"]["session_id"],
                "thesis": "A retryable provider failure should invite a bounded retry.",
                "capital": 100000,
                "base_currency": "USD",
                "user_private_evidence_claims": False,
            },
        )
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "15"
        assert response.json()["error_code"] == "internal_error"
        assert response.json()["retryable"] is True
        assert "provider detail" not in response.text
    finally:
        main_module.challenge_provider = previous_provider
        reset_state()
