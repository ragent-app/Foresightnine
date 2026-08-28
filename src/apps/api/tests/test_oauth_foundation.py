from __future__ import annotations

from hashlib import sha256
import base64

import pytest

from app.oauth_foundation import (
    ENV_CALLBACK_BASE_URL,
    ENV_CLIENT_ID,
    ENV_CLIENT_SECRET,
    ENV_RETURN_URI_ALLOWLIST,
    OAuthConfigurationError,
    OAuthFoundation,
    OAuthTransactionError,
)


TEST_SECRET = "oauth-unit-test-secret-only"


def environment() -> dict[str, str]:
    return {
        ENV_CALLBACK_BASE_URL: "https://api.example.test/oauth/callback",
        ENV_RETURN_URI_ALLOWLIST: "https://app.example.test/oauth/complete,foresightnine://oauth/complete",
        ENV_CLIENT_ID["google"]: "google-test-client-id",
        ENV_CLIENT_SECRET["google"]: TEST_SECRET,
        ENV_CLIENT_ID["github"]: "github-test-client-id",
        ENV_CLIENT_SECRET["github"]: "github-test-secret-only",
    }


def foundation(*, now: list[float] | None = None, ttl: int = 300) -> OAuthFoundation:
    clock = (lambda: now[0]) if now is not None else (lambda: 1000.0)
    return OAuthFoundation.from_environment(
        environ=environment(),
        transaction_ttl_seconds=ttl,
        clock=clock,
    )


def test_no_configuration_fails_closed_without_leaking_environment_values() -> None:
    with pytest.raises(OAuthConfigurationError, match="configuration|missing|provider") as error:
        OAuthFoundation.from_environment(environ={})
    assert TEST_SECRET not in str(error.value)


def test_google_and_github_are_the_only_configured_providers() -> None:
    value = foundation()
    assert value.supported_providers == ("google", "github")
    assert value.configuration.providers["google"].client_id == "google-test-client-id"


@pytest.mark.parametrize("provider", ["google", "github"])
@pytest.mark.parametrize("return_uri", [
    "https://app.example.test/oauth/complete",
    "foresightnine://oauth/complete",
])
def test_exactly_registered_https_and_deep_link_return_uris_are_allowed(
    provider: str,
    return_uri: str,
) -> None:
    request = foundation().create_transaction(provider, return_uri)
    assert request.provider == provider
    assert request.return_uri == return_uri
    assert request.redirect_uri == "https://api.example.test/oauth/callback"


@pytest.mark.parametrize("return_uri", [
    "http://app.example.test/oauth/complete",
    "https://evil.example.test/oauth/complete",
    "https://app.example.test/oauth/complete?next=https://evil.example.test",
    "//evil.example.test/oauth/complete",
])
def test_unregistered_or_open_redirect_return_uris_are_rejected(return_uri: str) -> None:
    with pytest.raises(OAuthTransactionError, match="registered"):
        foundation().create_transaction("google", return_uri)


@pytest.mark.parametrize("provider", ["microsoft", "GOOGLE", ""])
def test_unknown_provider_is_rejected(provider: str) -> None:
    with pytest.raises(OAuthTransactionError, match="supported"):
        foundation().create_transaction(provider, "https://app.example.test/oauth/complete")


@pytest.mark.parametrize("callback", [
    "http://api.example.test/oauth/callback",
    "https://api.example.test/oauth/callback?unsafe=1",
    "https://user:password@api.example.test/oauth/callback",
])
def test_invalid_callback_configuration_fails_closed(callback: str) -> None:
    values = environment()
    values[ENV_CALLBACK_BASE_URL] = callback
    with pytest.raises(OAuthConfigurationError):
        OAuthFoundation.from_environment(environ=values)


def test_pkce_s256_challenge_matches_the_server_stored_verifier() -> None:
    value = foundation()
    request = value.create_transaction("google", "https://app.example.test/oauth/complete")
    transaction = value.validate_and_consume(state=request.state, provider="google")
    expected = base64.urlsafe_b64encode(sha256(transaction.code_verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    assert request.code_challenge_method == "S256"
    assert request.code_challenge == expected
    assert "code_verifier" not in request.__dict__
    assert "nonce" not in request.__dict__


def test_state_is_one_use_and_provider_mismatch_is_rejected() -> None:
    value = foundation()
    request = value.create_transaction("google", "https://app.example.test/oauth/complete")

    with pytest.raises(OAuthTransactionError, match="mismatch"):
        value.validate_and_consume(state=request.state, provider="github")

    consumed = value.validate_and_consume(state=request.state, provider="google")
    assert consumed.provider == "google"
    with pytest.raises(OAuthTransactionError, match="invalid or expired"):
        value.validate_and_consume(state=request.state, provider="google")


def test_expired_state_is_rejected_and_removed() -> None:
    now = [1000.0]
    value = foundation(now=now, ttl=60)
    request = value.create_transaction("github", "foresightnine://oauth/complete")
    now[0] = 1060.0

    with pytest.raises(OAuthTransactionError, match="expired"):
        value.validate_and_consume(state=request.state, provider="github")
    with pytest.raises(OAuthTransactionError, match="invalid or expired"):
        value.validate_and_consume(state=request.state, provider="github")


def test_secrets_are_redacted_from_repr_and_errors() -> None:
    value = foundation()
    request = value.create_transaction("google", "https://app.example.test/oauth/complete")
    transaction = value.validate_and_consume(state=request.state, provider="google")
    rendered = repr(value.configuration) + repr(transaction)
    assert TEST_SECRET not in rendered
    assert "github-test-secret-only" not in rendered

    with pytest.raises(OAuthConfigurationError) as error:
        OAuthFoundation.from_environment(environ={
            ENV_CALLBACK_BASE_URL: "https://api.example.test/oauth/callback",
            ENV_RETURN_URI_ALLOWLIST: "https://app.example.test/oauth/complete",
            ENV_CLIENT_ID["google"]: "google-test-client-id",
            ENV_CLIENT_SECRET["google"]: TEST_SECRET,
            ENV_CLIENT_SECRET["github"]: "github-incomplete-secret-only",
        })
    assert TEST_SECRET not in str(error.value)
