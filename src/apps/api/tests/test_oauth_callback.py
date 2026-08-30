from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import app.main as main_module
from app.oauth_foundation import OAuthFoundation
from app.oauth_provider import OAuthProviderClient

from .helpers import CLIENT


def oauth_environment() -> dict[str, str]:
    return {
        "FORESIGHTNINE_OAUTH_CALLBACK_BASE_URL": "https://api.example.test/oauth/callback",
        "FORESIGHTNINE_OAUTH_RETURN_URI_ALLOWLIST": "https://app.example.test/oauth/complete,foresightnine://oauth/complete",
        "FORESIGHTNINE_OAUTH_GOOGLE_CLIENT_ID": "google-route-client-id",
        "FORESIGHTNINE_OAUTH_GOOGLE_CLIENT_SECRET": "server-only-google-secret",
        "FORESIGHTNINE_OAUTH_GITHUB_CLIENT_ID": "github-route-client-id",
        "FORESIGHTNINE_OAUTH_GITHUB_CLIENT_SECRET": "server-only-github-secret",
    }


def configured_foundation() -> OAuthFoundation:
    return OAuthFoundation.from_environment(environ=oauth_environment(), clock=lambda: 1000.0)


def test_browser_start_builds_fixed_google_authorization_redirect(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "oauth_foundation", configured_foundation())
    response = CLIENT.get(
        "/oauth/google/start",
        params={"return_uri": "https://app.example.test/oauth/complete"},
        follow_redirects=False,
    )

    assert response.status_code == 307
    location = response.headers["location"]
    parsed = urlsplit(location)
    query = parse_qs(parsed.query)
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert query["client_id"] == ["google-route-client-id"]
    assert query["redirect_uri"] == ["https://api.example.test/oauth/callback"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["openid email profile"]
    assert "server-only-google-secret" not in location


def test_provider_client_requires_verified_github_primary_email() -> None:
    calls: list[tuple[str, str, bytes | None]] = []

    def fake_request(method: str, url: str, *, body: bytes | None = None, headers: dict[str, str] | None = None):
        calls.append((method, url, body))
        if method == "POST":
            assert body is not None and b"client_secret=server-only-github-secret" in body
            return {"access_token": "provider-access-token"}
        if url.endswith("/user"):
            return {"id": 12345, "email": None}
        return [
            {"email": "unverified@example.test", "verified": False, "primary": True},
            {"email": "verified@example.test", "verified": True, "primary": True},
        ]

    client = OAuthProviderClient(
        request_json=fake_request,
        client_secret_for=lambda provider: "server-only-github-secret" if provider == "github" else "unused",
    )
    foundation = configured_foundation()
    authorization = foundation.create_transaction("github", "foresightnine://oauth/complete")
    transaction = foundation.consume_state(authorization.state)

    identity = client.identity(transaction.provider, client.exchange_code(transaction, "authorization-code"))

    assert identity.provider == "github"
    assert identity.subject == "12345"
    assert identity.email == "verified@example.test"
    assert [url for _method, url, _body in calls] == [
        "https://github.com/login/oauth/access_token",
        "https://api.github.com/user",
        "https://api.github.com/user/emails",
    ]


def test_oauth_callback_creates_persistent_identity_session_and_consumes_state(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    foundation = configured_foundation()
    monkeypatch.setattr(main_module, "oauth_foundation", foundation)

    def fake_request(method: str, url: str, **_kwargs):
        if method == "POST" and url == "https://oauth2.googleapis.com/token":
            return {"access_token": "provider-token-never-returned"}
        if method == "GET" and url == "https://openidconnect.googleapis.com/v1/userinfo":
            return {"sub": "google-subject-1", "email": "oauth@example.test", "email_verified": True}
        raise AssertionError(f"unexpected provider request: {method} {url}")

    monkeypatch.setattr(
        main_module,
        "oauth_provider_client",
        OAuthProviderClient(
            request_json=fake_request,
            client_secret_for=lambda _provider: "server-only-google-secret",
        ),
    )
    started = CLIENT.post("/v1/auth/google/start", json={"return_uri": "foresightnine://oauth/complete"})
    assert started.status_code == 200, started.text
    state = started.json()["state"]

    completed = CLIENT.get("/oauth/callback", params={"code": "authorization-code", "state": state})
    assert completed.status_code == 200, completed.text
    payload = completed.json()
    assert payload["auth_mode"] == "oauth"
    assert payload["provider"] == "google"
    assert payload["account"]["email"] == "oauth@example.test"
    assert "provider-token-never-returned" not in completed.text
    assert len(main_module.OAUTH_IDENTITIES) == 1

    replay = CLIENT.get("/oauth/callback", params={"code": "authorization-code", "state": state})
    assert replay.status_code == 400
    assert "provider-token-never-returned" not in replay.text

    deleted = CLIENT.delete(
        "/v1/auth/account",
        headers={"Authorization": f"Bearer {payload['access_token']}"},
    )
    assert deleted.status_code == 200, deleted.text
    assert len(main_module.OAUTH_IDENTITIES) == 0


def test_unconfigured_oauth_callback_fails_closed_without_provider_request(monkeypatch) -> None:
    calls = 0

    class UnexpectedClient:
        def exchange_code(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("provider must not be called")

    monkeypatch.setattr(main_module, "oauth_foundation", None)
    monkeypatch.setattr(main_module, "oauth_provider_client", UnexpectedClient())
    response = CLIENT.get("/oauth/callback", params={"code": "code", "state": "state"})

    assert response.status_code == 400
    assert response.json()["message"] == "OAuth callback is not available."
    assert calls == 0
