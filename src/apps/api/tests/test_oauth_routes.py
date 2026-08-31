from __future__ import annotations

from typing import Any

import app.main as main_module
from app.oauth_foundation import OAuthFoundation

from .helpers import CLIENT, assert_contract


TEST_SECRET = "oauth-route-test-secret-only"


def oauth_environment() -> dict[str, str]:
    return {
        "FORESIGHTNINE_OAUTH_CALLBACK_BASE_URL": "https://api.example.test/oauth/callback",
        "FORESIGHTNINE_OAUTH_RETURN_URI_ALLOWLIST": "https://app.example.test/oauth/complete,foresightnine://oauth/complete",
        "FORESIGHTNINE_OAUTH_GOOGLE_CLIENT_ID": "google-route-client-id",
        "FORESIGHTNINE_OAUTH_GOOGLE_CLIENT_SECRET": TEST_SECRET,
        "FORESIGHTNINE_OAUTH_GITHUB_CLIENT_ID": "github-route-client-id",
        "FORESIGHTNINE_OAUTH_GITHUB_CLIENT_SECRET": "github-route-secret-only",
    }


def configured_foundation() -> OAuthFoundation:
    return OAuthFoundation.from_environment(environ=oauth_environment(), clock=lambda: 1000.0)


def test_providers_returns_exactly_two_redacted_readiness_records(monkeypatch: Any) -> None:
    monkeypatch.setattr(main_module, "oauth_foundation", configured_foundation())

    response = CLIENT.get("/v1/auth/providers")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "providers": [
            {"provider": "google", "enabled": True},
            {"provider": "github", "enabled": True},
        ]
    }
    assert TEST_SECRET not in response.text


def test_unconfigured_providers_fail_closed_without_revealing_configuration(monkeypatch: Any) -> None:
    monkeypatch.setattr(main_module, "oauth_foundation", None)

    readiness = CLIENT.get("/v1/auth/providers")
    start = CLIENT.post("/v1/auth/google/start", json={"return_uri": "https://app.example.test/oauth/complete"})

    assert readiness.status_code == 200
    assert readiness.json() == {
        "providers": [
            {"provider": "google", "enabled": False},
            {"provider": "github", "enabled": False},
        ]
    }
    assert start.status_code == 400
    assert_contract(start.json(), "ErrorResponse")
    assert start.json()["message"] == "OAuth authorization is not available."
    assert TEST_SECRET not in start.text


def test_start_returns_only_safe_pkce_authorization_fields(monkeypatch: Any) -> None:
    foundation = configured_foundation()
    monkeypatch.setattr(main_module, "oauth_foundation", foundation)

    response = CLIENT.post(
        "/v1/auth/google/start",
        json={"return_uri": "foresightnine://oauth/complete"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {
        "provider", "client_id", "redirect_uri", "return_uri", "state",
        "code_challenge", "code_challenge_method",
    }
    assert payload["provider"] == "google"
    assert payload["client_id"] == "google-route-client-id"
    assert payload["redirect_uri"] == "https://api.example.test/oauth/callback"
    assert payload["return_uri"] == "foresightnine://oauth/complete"
    assert payload["code_challenge_method"] == "S256"
    assert "nonce" not in payload
    assert "code_verifier" not in payload
    assert "client_secret" not in payload
    assert TEST_SECRET not in response.text

    consumed = foundation.validate_and_consume(state=payload["state"], provider="google")
    assert consumed.code_challenge == payload["code_challenge"]


def test_start_rejects_unknown_or_unregistered_return_values_with_typed_redacted_errors(monkeypatch: Any) -> None:
    monkeypatch.setattr(main_module, "oauth_foundation", configured_foundation())

    for path, body in (
        ("/v1/auth/microsoft/start", {"return_uri": "https://app.example.test/oauth/complete"}),
        ("/v1/auth/google/start", {"return_uri": "https://evil.example.test/oauth/complete"}),
        ("/v1/auth/google/start", {"return_uri": "http://app.example.test/oauth/complete"}),
        ("/v1/auth/google/start", {"return_uri": "foresightnine://oauth/other"}),
    ):
        response = CLIENT.post(path, json=body)
        assert response.status_code == 400, response.text
        assert_contract(response.json(), "ErrorResponse")
        assert response.json()["message"] == "OAuth authorization request is unavailable."
        assert TEST_SECRET not in response.text


def test_start_request_is_strict_and_does_not_accept_client_credentials(monkeypatch: Any) -> None:
    monkeypatch.setattr(main_module, "oauth_foundation", configured_foundation())

    response = CLIENT.post(
        "/v1/auth/github/start",
        json={
            "return_uri": "https://app.example.test/oauth/complete",
            "client_secret": TEST_SECRET,
        },
    )

    assert response.status_code == 422
    assert_contract(response.json(), "ErrorResponse")
    assert TEST_SECRET not in response.text


def test_per_provider_callback_urls_override_the_shared_callback_base(monkeypatch: Any) -> None:
    environment = oauth_environment()
    environment["FORESIGHTNINE_OAUTH_GOOGLE_CALLBACK_URL"] = "https://app.example.test/api/auth/callback/google"
    environment["FORESIGHTNINE_OAUTH_GITHUB_CALLBACK_URL"] = "https://app.example.test/api/auth/callback/github"
    monkeypatch.setattr(main_module, "oauth_foundation", OAuthFoundation.from_environment(environ=environment, clock=lambda: 1000.0))

    google = CLIENT.post("/v1/auth/google/start", json={"return_uri": "https://app.example.test/oauth/complete"})
    github = CLIENT.post("/v1/auth/github/start", json={"return_uri": "https://app.example.test/oauth/complete"})

    assert google.status_code == 200, google.text
    assert github.status_code == 200, github.text
    assert google.json()["redirect_uri"] == "https://app.example.test/api/auth/callback/google"
    assert github.json()["redirect_uri"] == "https://app.example.test/api/auth/callback/github"
    assert TEST_SECRET not in google.text and TEST_SECRET not in github.text


def test_per_provider_callback_alias_routes_serve_the_same_callback_contract(monkeypatch: Any) -> None:
    environment = oauth_environment()
    environment["FORESIGHTNINE_OAUTH_GOOGLE_CALLBACK_URL"] = "https://app.example.test/api/auth/callback/google"
    monkeypatch.setattr(main_module, "oauth_foundation", OAuthFoundation.from_environment(environ=environment, clock=lambda: 1000.0))

    for path in ("/oauth/callback", "/api/auth/callback/google", "/api/auth/callback/github"):
        response = CLIENT.get(f"{path}?state=unknown-state&code=x")
        assert response.status_code == 400, response.text
        assert_contract(response.json(), "ErrorResponse")
        assert response.json()["message"] == "OAuth callback is unavailable."
        assert TEST_SECRET not in response.text
