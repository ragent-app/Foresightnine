"""Secret-free OAuth configuration and PKCE transaction primitives.

This module deliberately does not make HTTP requests or expose OAuth endpoints.  It
only validates server configuration and keeps short-lived authorization transactions
in server memory until a future callback handler consumes them.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Callable, Literal, Mapping
from urllib.parse import urlsplit


OAuthProvider = Literal["google", "github"]
SUPPORTED_PROVIDERS: tuple[OAuthProvider, ...] = ("google", "github")

ENV_CALLBACK_BASE_URL = "FORESIGHTNINE_OAUTH_CALLBACK_BASE_URL"
ENV_RETURN_URI_ALLOWLIST = "FORESIGHTNINE_OAUTH_RETURN_URI_ALLOWLIST"
ENV_CLIENT_ID = {
    "google": "FORESIGHTNINE_OAUTH_GOOGLE_CLIENT_ID",
    "github": "FORESIGHTNINE_OAUTH_GITHUB_CLIENT_ID",
}
ENV_CLIENT_SECRET = {
    "google": "FORESIGHTNINE_OAUTH_GOOGLE_CLIENT_SECRET",
    "github": "FORESIGHTNINE_OAUTH_GITHUB_CLIENT_SECRET",
}

DEFAULT_TRANSACTION_TTL_SECONDS = 300
MAX_TRANSACTION_TTL_SECONDS = 600
PKCE_METHOD = "S256"


class OAuthConfigurationError(ValueError):
    """Raised when OAuth is not configured safely enough to be enabled."""


class OAuthTransactionError(ValueError):
    """Raised when a PKCE transaction is invalid, expired, or already consumed."""


def pkce_s256_challenge(code_verifier: str) -> str:
    """Return the RFC 7636 S256 challenge for a code verifier."""

    if not isinstance(code_verifier, str) or not 43 <= len(code_verifier) <= 128:
        raise ValueError("invalid PKCE verifier")
    try:
        code_verifier.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("invalid PKCE verifier") from exc
    digest = sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _require_nonempty(value: str | None, *, error: str) -> str:
    if not isinstance(value, str) or not value:
        raise OAuthConfigurationError(error)
    return value


def _has_control_character(value: str) -> bool:
    return any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or character == "\\"
        or character.isspace()
        for character in value
    )


def _validate_url_parts(value: str, *, deep_link: bool) -> None:
    if _has_control_character(value):
        raise OAuthConfigurationError("OAuth URI contains invalid characters")
    try:
        parts = urlsplit(value)
        # Accessing port forces validation of malformed numeric ports.
        _ = parts.port
    except ValueError as exc:
        raise OAuthConfigurationError("OAuth URI is invalid") from exc

    if parts.username is not None or parts.password is not None:
        raise OAuthConfigurationError("OAuth URI userinfo is not allowed")
    if parts.fragment:
        raise OAuthConfigurationError("OAuth URI fragments are not allowed")

    if deep_link:
        if parts.scheme != "foresightnine" or not parts.netloc:
            raise OAuthConfigurationError("OAuth return URI must be an allowed deep link")
        return

    if parts.scheme != "https" or not parts.hostname:
        raise OAuthConfigurationError("OAuth URI must use HTTPS")


def _validate_callback_base_url(value: str) -> str:
    value = _require_nonempty(value, error="OAuth callback configuration is missing")
    _validate_url_parts(value, deep_link=False)
    try:
        parts = urlsplit(value)
    except ValueError as exc:  # Defensive: the URL was already parsed above.
        raise OAuthConfigurationError("OAuth callback configuration is invalid") from exc
    if parts.query:
        raise OAuthConfigurationError("OAuth callback must not contain a query")
    return value


def _validate_return_uri(value: str) -> str:
    value = _require_nonempty(value, error="OAuth return URI is missing")
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise OAuthConfigurationError("OAuth return URI is invalid") from exc
    _validate_url_parts(value, deep_link=parts.scheme == "foresightnine")
    return value


def _parse_return_uri_allowlist(raw: str | None) -> frozenset[str]:
    raw = _require_nonempty(raw, error="OAuth return URI allowlist is missing")
    values: object
    if raw.lstrip().startswith("["):
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OAuthConfigurationError("OAuth return URI allowlist is invalid") from exc
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise OAuthConfigurationError("OAuth return URI allowlist is invalid")
        candidates = [value.strip() for value in values]
    else:
        candidates = [value.strip() for value in raw.split(",")]

    if not candidates or any(not value for value in candidates):
        raise OAuthConfigurationError("OAuth return URI allowlist is invalid")
    validated = tuple(_validate_return_uri(value) for value in candidates)
    if len(set(validated)) != len(validated):
        raise OAuthConfigurationError("OAuth return URI allowlist contains duplicates")
    return frozenset(validated)


@dataclass(frozen=True, repr=False)
class OAuthProviderConfig:
    """One provider's server-only credentials and public client identifier."""

    provider: OAuthProvider
    client_id: str
    client_secret: str = ""

    def __post_init__(self) -> None:
        if self.provider not in SUPPORTED_PROVIDERS:
            raise OAuthConfigurationError("OAuth provider is not supported")
        if not isinstance(self.client_id, str) or not self.client_id.strip():
            raise OAuthConfigurationError("OAuth client ID is missing")
        if not isinstance(self.client_secret, str) or not self.client_secret:
            raise OAuthConfigurationError("OAuth client secret is missing")

    def __repr__(self) -> str:
        return f"OAuthProviderConfig(provider={self.provider!r}, client_id=<configured>, client_secret=<redacted>)"


@dataclass(frozen=True, repr=False)
class OAuthConfiguration:
    """Validated OAuth configuration; secrets remain server-side and redacted."""

    callback_base_url: str
    return_uri_allowlist: frozenset[str]
    providers: Mapping[OAuthProvider, OAuthProviderConfig]

    def __post_init__(self) -> None:
        _validate_callback_base_url(self.callback_base_url)
        allowlist = frozenset(self.return_uri_allowlist)
        if not allowlist:
            raise OAuthConfigurationError("OAuth return URI allowlist is missing")
        for return_uri in allowlist:
            _validate_return_uri(return_uri)
        providers = dict(self.providers)
        if not providers:
            raise OAuthConfigurationError("No OAuth provider is configured")
        if any(
            provider not in SUPPORTED_PROVIDERS
            or not isinstance(provider_config, OAuthProviderConfig)
            or provider_config.provider != provider
            for provider, provider_config in providers.items()
        ):
            raise OAuthConfigurationError("OAuth provider is not supported")
        object.__setattr__(self, "return_uri_allowlist", allowlist)
        object.__setattr__(self, "providers", MappingProxyType(providers))

    def provider_config(self, provider: str) -> OAuthProviderConfig:
        if provider not in SUPPORTED_PROVIDERS:
            raise OAuthTransactionError("OAuth provider is not supported")
        configured = self.providers.get(provider)
        if configured is None:
            raise OAuthConfigurationError("OAuth provider is not configured")
        return configured

    def __repr__(self) -> str:
        providers = tuple(sorted(self.providers))
        return (
            "OAuthConfiguration("
            f"callback_base_url={self.callback_base_url!r}, "
            f"return_uri_count={len(self.return_uri_allowlist)}, "
            f"providers={providers!r})"
        )


@dataclass(frozen=True)
class OAuthAuthorizationRequest:
    """Safe-to-use fields for a future provider authorization redirect."""

    provider: OAuthProvider
    client_id: str
    redirect_uri: str
    return_uri: str
    state: str
    code_challenge: str
    code_challenge_method: str = PKCE_METHOD


@dataclass(frozen=True, repr=False)
class OAuthTransaction:
    """A consumed transaction, including values needed by a future token exchange."""

    provider: OAuthProvider
    client_id: str
    redirect_uri: str
    return_uri: str
    state: str
    nonce: str
    code_verifier: str
    code_challenge: str
    created_at: float
    expires_at: float

    def __repr__(self) -> str:
        return (
            "OAuthTransaction("
            f"provider={self.provider!r}, client_id=<configured>, "
            f"redirect_uri={self.redirect_uri!r}, return_uri={self.return_uri!r}, "
            "state=<redacted>, nonce=<redacted>, code_verifier=<redacted>, "
            f"code_challenge={self.code_challenge!r}, created_at={self.created_at!r}, "
            f"expires_at={self.expires_at!r})"
        )


@dataclass(frozen=True, repr=False)
class _StoredTransaction:
    provider: OAuthProvider
    client_id: str
    redirect_uri: str
    return_uri: str
    state: str
    nonce: str
    code_verifier: str
    code_challenge: str
    created_at: float
    expires_at: float

    def consumed(self) -> OAuthTransaction:
        return OAuthTransaction(
            provider=self.provider,
            client_id=self.client_id,
            redirect_uri=self.redirect_uri,
            return_uri=self.return_uri,
            state=self.state,
            nonce=self.nonce,
            code_verifier=self.code_verifier,
            code_challenge=self.code_challenge,
            created_at=self.created_at,
            expires_at=self.expires_at,
        )

    def __repr__(self) -> str:
        return "_StoredTransaction(provider=<configured>, state=<redacted>, secrets=<redacted>)"


def _environment_configuration(environ: Mapping[str, str]) -> OAuthConfiguration:
    callback_base_url = _validate_callback_base_url(environ.get(ENV_CALLBACK_BASE_URL))
    allowlist = _parse_return_uri_allowlist(environ.get(ENV_RETURN_URI_ALLOWLIST))
    providers: dict[OAuthProvider, OAuthProviderConfig] = {}

    for provider in SUPPORTED_PROVIDERS:
        client_id = environ.get(ENV_CLIENT_ID[provider])
        client_secret = environ.get(ENV_CLIENT_SECRET[provider])
        if client_id is None and client_secret is None:
            continue
        if client_id is None or client_secret is None:
            raise OAuthConfigurationError("OAuth provider configuration is incomplete")
        providers[provider] = OAuthProviderConfig(
            provider=provider,
            client_id=client_id,
            client_secret=client_secret,
        )

    return OAuthConfiguration(
        callback_base_url=callback_base_url,
        return_uri_allowlist=allowlist,
        providers=providers,
    )


class OAuthFoundation:
    """Pure server-side OAuth setup and one-use PKCE transaction storage."""

    def __init__(
        self,
        configuration: OAuthConfiguration,
        *,
        transaction_ttl_seconds: int = DEFAULT_TRANSACTION_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(configuration, OAuthConfiguration):
            raise TypeError("configuration must be OAuthConfiguration")
        if not 1 <= transaction_ttl_seconds <= MAX_TRANSACTION_TTL_SECONDS:
            raise ValueError("OAuth transaction TTL is outside the allowed range")
        self._configuration = configuration
        self._transaction_ttl_seconds = transaction_ttl_seconds
        self._clock = clock
        self._transactions: dict[str, _StoredTransaction] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_environment(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        transaction_ttl_seconds: int = DEFAULT_TRANSACTION_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> OAuthFoundation:
        """Build a foundation from environment-only configuration.

        No provider is contacted.  Missing or partial configuration raises a
        generic error without including any environment value.
        """

        source = os.environ if environ is None else environ
        configuration = _environment_configuration(source)
        return cls(
            configuration,
            transaction_ttl_seconds=transaction_ttl_seconds,
            clock=clock,
        )

    @property
    def configuration(self) -> OAuthConfiguration:
        """Return validated configuration whose repr redacts provider secrets."""

        return self._configuration

    @property
    def supported_providers(self) -> tuple[OAuthProvider, ...]:
        return tuple(provider for provider in SUPPORTED_PROVIDERS if provider in self._configuration.providers)

    def create_transaction(
        self,
        provider: str,
        return_uri: str,
    ) -> OAuthAuthorizationRequest:
        """Create a short-lived PKCE transaction without making a network call."""

        provider_config = self._configuration.provider_config(provider)
        if return_uri not in self._configuration.return_uri_allowlist:
            raise OAuthTransactionError("OAuth return URI is not registered")
        # Revalidate at the use boundary in case a configuration object was
        # constructed manually with a mutable allowlist implementation.
        _validate_return_uri(return_uri)

        created_at = float(self._clock())
        expires_at = created_at + self._transaction_ttl_seconds
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = pkce_s256_challenge(code_verifier)
        transaction = _StoredTransaction(
            provider=provider_config.provider,
            client_id=provider_config.client_id,
            redirect_uri=self._configuration.callback_base_url,
            return_uri=return_uri,
            state=state,
            nonce=nonce,
            code_verifier=code_verifier,
            code_challenge=code_challenge,
            created_at=created_at,
            expires_at=expires_at,
        )

        with self._lock:
            self._remove_expired(created_at)
            self._transactions[state] = transaction

        return OAuthAuthorizationRequest(
            provider=provider_config.provider,
            client_id=provider_config.client_id,
            redirect_uri=self._configuration.callback_base_url,
            return_uri=return_uri,
            state=state,
            code_challenge=code_challenge,
        )

    def validate_and_consume(
        self,
        *,
        state: str,
        provider: str,
    ) -> OAuthTransaction:
        """Validate and atomically consume a transaction exactly once.

        A provider mismatch does not consume a still-valid transaction, allowing
        the legitimate callback to complete.  Expired and successfully consumed
        transactions are removed permanently.
        """

        if not isinstance(state, str) or not state:
            raise OAuthTransactionError("OAuth state is invalid or expired")
        if provider not in SUPPORTED_PROVIDERS:
            raise OAuthTransactionError("OAuth provider is not supported")

        now = float(self._clock())
        with self._lock:
            transaction = self._transactions.get(state)
            if transaction is None:
                raise OAuthTransactionError("OAuth state is invalid or expired")
            if now >= transaction.expires_at:
                del self._transactions[state]
                raise OAuthTransactionError("OAuth state is expired")
            if transaction.provider != provider:
                raise OAuthTransactionError("OAuth state provider mismatch")
            del self._transactions[state]
            return transaction.consumed()

    def _remove_expired(self, now: float) -> None:
        expired_states = [
            state
            for state, transaction in self._transactions.items()
            if now >= transaction.expires_at
        ]
        for state in expired_states:
            del self._transactions[state]


# Short alias for callers that prefer the concise configuration name.
OAuthConfig = OAuthConfiguration
