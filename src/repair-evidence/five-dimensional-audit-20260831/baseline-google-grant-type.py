"""Fixed-endpoint OAuth provider clients for the optional production callback.

The client has no discovery or user-controlled URL path.  Every endpoint,
request shape, response size and accepted identity field is fixed per provider;
provider access tokens are used only in memory for the current callback.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .oauth_foundation import OAuthAuthorizationRequest, OAuthProvider, OAuthTransaction


MAX_RESPONSE_BYTES = 64 * 1024
MAX_ACCESS_TOKEN_LENGTH = 4096
OAuthHttpResponse = dict[str, Any] | list[Any]


class OAuthProviderError(RuntimeError):
    """Raised when a provider response cannot be accepted safely."""


@dataclass(frozen=True)
class OAuthIdentityResult:
    provider: OAuthProvider
    subject: str
    email: str


class _RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        raise OAuthProviderError("OAuth provider redirect rejected")


def _default_json_request(
    method: Literal["GET", "POST"],
    url: str,
    *,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
) -> OAuthHttpResponse:
    request = Request(url, data=body, headers=dict(headers or {}), method=method)
    try:
        with build_opener(_RejectRedirect).open(request, timeout=8) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except Exception as error:
        if isinstance(error, OAuthProviderError):
            raise
        raise OAuthProviderError("OAuth provider request failed") from error
    if len(payload) > MAX_RESPONSE_BYTES:
        raise OAuthProviderError("OAuth provider response exceeded the bounded size")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OAuthProviderError("OAuth provider response was not valid JSON") from error
    if not isinstance(decoded, (dict, list)):
        raise OAuthProviderError("OAuth provider response had an invalid shape")
    return decoded


def _nonempty_string(value: object, *, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise OAuthProviderError("OAuth provider identity was incomplete")
    return value


def _verified_email(value: object) -> str:
    email = _nonempty_string(value, max_length=254).strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise OAuthProviderError("OAuth provider identity email was invalid")
    return email


class OAuthProviderClient:
    """Exchange authorization codes and read a verified provider identity."""

    def __init__(
        self,
        request_json: Callable[..., OAuthHttpResponse] | None = None,
        client_secret_for: Callable[[OAuthProvider], str] | None = None,
    ) -> None:
        self._request_json = request_json or _default_json_request
        self._client_secret_for = client_secret_for

    @staticmethod
    def authorization_url(request: OAuthAuthorizationRequest | OAuthTransaction) -> str:
        if request.provider == "google":
            endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
            scope = "openid email profile"
        else:
            endpoint = "https://github.com/login/oauth/authorize"
            scope = "read:user user:email"
        query = {
            "client_id": request.client_id,
            "redirect_uri": request.redirect_uri,
            "response_type": "code",
            "scope": scope,
            "state": request.state,
            "code_challenge": request.code_challenge,
            "code_challenge_method": "S256",
        }
        if request.provider == "google":
            query["nonce"] = request.nonce
            query["access_type"] = "online"
            query["prompt"] = "select_account"
        return f"{endpoint}?{urlencode(query)}"

    def exchange_code(self, transaction: OAuthTransaction, code: str) -> str:
        code = _nonempty_string(code, max_length=4096)
        if transaction.provider == "google":
            endpoint = "https://oauth2.googleapis.com/token"
        else:
            endpoint = "https://github.com/login/oauth/access_token"
        body = urlencode({
            "client_id": transaction.client_id,
            "client_secret": self._client_secret(transaction.provider),
            "code": code,
            "redirect_uri": transaction.redirect_uri,
            "code_verifier": transaction.code_verifier,
        }).encode("ascii")
        response = self._request_json(
            "POST",
            endpoint,
            body=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "ForesightNine-oauth/1",
            },
        )
        if not isinstance(response, dict):
            raise OAuthProviderError("OAuth token response had an invalid shape")
        return _nonempty_string(response.get("access_token"), max_length=MAX_ACCESS_TOKEN_LENGTH)

    def identity(self, provider: OAuthProvider, access_token: str) -> OAuthIdentityResult:
        access_token = _nonempty_string(access_token, max_length=MAX_ACCESS_TOKEN_LENGTH)
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "ForesightNine-oauth/1",
        }
        if provider == "google":
            response = self._request_json("GET", "https://openidconnect.googleapis.com/v1/userinfo", headers=headers)
            if not isinstance(response, dict) or response.get("email_verified") is not True:
                raise OAuthProviderError("OAuth provider email was not verified")
            return OAuthIdentityResult(
                provider="google",
                subject=_nonempty_string(response.get("sub"), max_length=255),
                email=_verified_email(response.get("email")),
            )

        github_headers = {
            **headers,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        user = self._request_json("GET", "https://api.github.com/user", headers=github_headers)
        if not isinstance(user, dict):
            raise OAuthProviderError("OAuth provider identity had an invalid shape")
        raw_id = user.get("id")
        if not isinstance(raw_id, int) or isinstance(raw_id, bool) or raw_id <= 0:
            raise OAuthProviderError("OAuth provider subject was invalid")
        emails = self._request_json("GET", "https://api.github.com/user/emails", headers=github_headers)
        if not isinstance(emails, list):
            raise OAuthProviderError("OAuth provider email list was invalid")
        candidates = [
            item.get("email")
            for item in emails
            if isinstance(item, dict) and item.get("verified") is True and item.get("primary") is True
        ]
        email = candidates[0] if candidates else None
        return OAuthIdentityResult(provider="github", subject=str(raw_id), email=_verified_email(email))

    def _client_secret(self, provider: OAuthProvider) -> str:
        if self._client_secret_for is None:
            raise OAuthProviderError("OAuth client secret is unavailable")
        try:
            secret = self._client_secret_for(provider)
        except Exception as error:
            raise OAuthProviderError("OAuth client secret is unavailable") from error
        return _nonempty_string(secret, max_length=4096)


__all__ = ["OAuthIdentityResult", "OAuthProviderClient", "OAuthProviderError"]
