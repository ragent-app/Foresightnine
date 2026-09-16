from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import app.main as main_module
from app.oauth_foundation import OAuthFoundation
from app.oauth_provider import OAuthProviderClient, OAuthProviderError

from .helpers import CLIENT


def oauth_environment() -> dict[str, str]:
    return {
        "FORESIGHTNINE_OAUTH_CALLBACK_BASE_URL": "https://api.example.test/oauth/callback",
        "FORESIGHTNINE_OAUTH_RETURN_URI_ALLOWLIST": "https://app.example.test/oauth/complete?client=web,foresightnine://oauth/complete",
        "FORESIGHTNINE_OAUTH_GOOGLE_CLIENT_ID": "google-test-client-id",
        "FORESIGHTNINE_OAUTH_GOOGLE_CLIENT_SECRET": "test-only-google-secret",
    }


def configured_foundation(clock: list[float] | None = None) -> OAuthFoundation:
    return OAuthFoundation.from_environment(
        environ=oauth_environment(),
        clock=(lambda: clock[0]) if clock is not None else (lambda: 1000.0),
    )


def google_client() -> OAuthProviderClient:
    def fake_request(method: str, url: str, **_kwargs):
        if method == "POST" and url == "https://oauth2.googleapis.com/token":
            return {"access_token": "provider-token-never-returned"}
        if method == "GET" and url == "https://openidconnect.googleapis.com/v1/userinfo":
            return {"sub": "ticket-subject", "email": "ticket@example.test", "email_verified": True}
        raise AssertionError(f"unexpected provider request: {method} {url}")

    return OAuthProviderClient(request_json=fake_request, client_secret_for=lambda _provider: "test-only-google-secret")


def start_callback(*, return_uri: str = "foresightnine://oauth/complete") -> str:
    started = CLIENT.post("/v1/auth/google/start", json={"return_uri": return_uri})
    assert started.status_code == 200, started.text
    return started.json()["state"]


def test_callback_redirects_only_with_ticket_and_exchange_issues_session(monkeypatch) -> None:
    foundation = configured_foundation()
    monkeypatch.setattr(main_module, "oauth_foundation", foundation)
    monkeypatch.setattr(main_module, "oauth_provider_client", google_client())

    state = start_callback()
    callback = CLIENT.get("/oauth/callback", params={"state": state, "code": "authorization-code"}, follow_redirects=False)

    assert callback.status_code == 303
    location = callback.headers["location"]
    query = parse_qs(urlsplit(location).query)
    assert urlsplit(location).scheme == "foresightnine"
    assert set(query) == {"ticket"}
    ticket = query["ticket"][0]
    assert len(main_module.AUTH_SESSIONS) == 0
    assert "access_token" not in location
    assert "provider-token-never-returned" not in location

    exchanged = CLIENT.post("/v1/auth/oauth/exchange", json={"ticket": ticket})
    assert exchanged.status_code == 200, exchanged.text
    assert exchanged.headers["cache-control"] == "no-store"
    assert exchanged.headers["pragma"] == "no-cache"
    assert exchanged.json()["account"]["email"] == "ticket@example.test"
    assert exchanged.json()["auth_mode"] == "oauth"
    assert len(main_module.AUTH_SESSIONS) == 1

    replay = CLIENT.post("/v1/auth/oauth/exchange", json={"ticket": ticket})
    assert replay.status_code == 400
    assert replay.json()["message"] == "OAuth exchange is unavailable."


def test_callback_cancel_and_provider_failure_redirect_without_sensitive_query_values(monkeypatch) -> None:
    foundation = configured_foundation()
    monkeypatch.setattr(main_module, "oauth_foundation", foundation)
    state = start_callback()
    cancelled = CLIENT.get(
        "/oauth/callback",
        params={"state": state, "error": "access_denied", "error_description": "provider detail"},
        follow_redirects=False,
    )
    assert cancelled.status_code == 303
    cancelled_query = parse_qs(urlsplit(cancelled.headers["location"]).query)
    assert cancelled_query["error"] == ["oauth_callback_failed"]
    assert "access_denied" not in cancelled.text
    assert "provider detail" not in cancelled.text

    failing_state = start_callback()

    class FailingClient:
        def exchange_code(self, *_args, **_kwargs):
            raise OAuthProviderError("provider diagnostic must stay server-side")

    monkeypatch.setattr(main_module, "oauth_provider_client", FailingClient())
    failed = CLIENT.get("/oauth/callback", params={"state": failing_state, "code": "authorization-code"}, follow_redirects=False)
    assert failed.status_code == 303
    failed_query = parse_qs(urlsplit(failed.headers["location"]).query)
    assert failed_query["error"] == ["oauth_callback_failed"]
    assert "provider diagnostic" not in failed.text
    assert len(main_module.AUTH_SESSIONS) == 0


def test_ticket_exchange_is_strict_and_expired_or_unknown_tickets_fail_closed(monkeypatch) -> None:
    now = [1000.0]
    foundation = configured_foundation(now)
    monkeypatch.setattr(main_module, "oauth_foundation", foundation)
    monkeypatch.setattr(main_module, "oauth_provider_client", google_client())

    state = start_callback()
    callback = CLIENT.get("/oauth/callback", params={"state": state, "code": "authorization-code"}, follow_redirects=False)
    ticket = parse_qs(urlsplit(callback.headers["location"]).query)["ticket"][0]

    strict = CLIENT.post("/v1/auth/oauth/exchange", json={"ticket": ticket, "access_token": "forbidden"})
    assert strict.status_code == 422
    assert len(main_module.AUTH_SESSIONS) == 0

    now[0] = 1090.0
    expired = CLIENT.post("/v1/auth/oauth/exchange", json={"ticket": ticket})
    assert expired.status_code == 400
    assert expired.json()["message"] == "OAuth exchange is unavailable."
    assert len(main_module.AUTH_SESSIONS) == 0

    unknown = CLIENT.post("/v1/auth/oauth/exchange", json={"ticket": "a" * 43})
    assert unknown.status_code == 400
    assert unknown.json()["message"] == "OAuth exchange is unavailable."


def test_callback_state_replay_is_not_redirected_and_account_lifecycle_remains_supported(monkeypatch) -> None:
    foundation = configured_foundation()
    monkeypatch.setattr(main_module, "oauth_foundation", foundation)
    monkeypatch.setattr(main_module, "oauth_provider_client", google_client())

    state = start_callback(return_uri="https://app.example.test/oauth/complete?client=web")
    first = CLIENT.get("/oauth/callback", params={"state": state, "code": "authorization-code"}, follow_redirects=False)
    assert first.status_code == 303
    location = urlsplit(first.headers["location"])
    query = parse_qs(location.query)
    assert query["client"] == ["web"]
    ticket = query["ticket"][0]
    exchanged = CLIENT.post("/v1/auth/oauth/exchange", json={"ticket": ticket})
    token = exchanged.json()["access_token"]

    replay = CLIENT.get("/oauth/callback", params={"state": state, "code": "authorization-code"}, follow_redirects=False)
    assert replay.status_code == 400
    assert replay.json()["message"] == "OAuth callback is unavailable."

    logged_out = CLIENT.post("/v1/auth/logout", json={}, headers={"Authorization": f"Bearer {token}"})
    assert logged_out.status_code == 200
    assert CLIENT.delete("/v1/auth/account", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_verified_oauth_identity_joins_existing_password_account(monkeypatch) -> None:
    """Provider-verified email links into the existing password account."""
    from app.local_auth import LocalAccount

    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.setenv("FORESIGHTNINE_ENV", "development")
    monkeypatch.delenv("FORESIGHTNINE_STORAGE_MODE", raising=False)
    registered = CLIENT.post(
        "/v1/auth/local/register",
        json={"email": "ticket@example.test", "password": "local-password-123"},
    )
    assert registered.status_code == 201, registered.text
    password_account_id = registered.json()["account"]["account_id"]

    foundation = configured_foundation()
    monkeypatch.setattr(main_module, "oauth_foundation", foundation)
    monkeypatch.setattr(main_module, "oauth_provider_client", google_client())

    state = start_callback()
    callback = CLIENT.get("/oauth/callback", params={"state": state, "code": "authorization-code"}, follow_redirects=False)
    ticket = parse_qs(urlsplit(callback.headers["location"]).query)["ticket"][0]
    exchanged = CLIENT.post("/v1/auth/oauth/exchange", json={"ticket": ticket})

    assert exchanged.status_code == 200, exchanged.text
    assert exchanged.json()["account"]["account_id"] == password_account_id
    assert exchanged.json()["auth_mode"] == "oauth"
    from app.main import _oauth_identity_key

    identity_key = _oauth_identity_key("google", "ticket-subject")
    linked = main_module.OAUTH_IDENTITIES.get(identity_key)
    assert linked is not None and linked.account_id == password_account_id
