"""Small, explicitly non-production local account lifecycle.

This module exists so Linux staging can exercise durable registration, session,
logout and deletion behavior without pretending that an OAuth provider was
configured.  Production identity must use an approved provider and secret
manager configuration instead of this mock mode.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import pbkdf2_hmac, sha256
import base64
import hmac
import re
import secrets
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PASSWORD_ITERATIONS = 310_000
SESSION_TTL = timedelta(days=30)


class LocalAuthError(RuntimeError):
    def __init__(self, status_code: int, error_code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message


class LocalRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=256)


class LocalAccount(BaseModel):
    # Persisted JSON dates are parsed by Pydantic on reload. Request strictness
    # belongs to LocalRegisterRequest, not to the storage representation.
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v1"] = "v1"
    account_id: str = Field(min_length=8, max_length=80)
    email: str = Field(min_length=3, max_length=254)
    password_digest: str = Field(min_length=32, max_length=512)
    created_at: datetime
    deleted_at: datetime | None = None


class LocalSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v1"] = "v1"
    session_id: str = Field(min_length=8, max_length=80)
    account_id: str = Field(min_length=8, max_length=80)
    token_digest: str = Field(min_length=64, max_length=64)
    created_at: datetime
    expires_at: datetime


def normalize_email(value: str) -> str:
    normalized = value.strip().lower()
    if not EMAIL_PATTERN.fullmatch(normalized):
        raise LocalAuthError(422, "validation_error", "A valid email address is required.")
    return normalized


def _password_digest(password: str, salt: bytes, iterations: int = PASSWORD_ITERATIONS) -> str:
    derived = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${iterations}${salt}${digest}".format(
        iterations=iterations,
        salt=base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        digest=base64.urlsafe_b64encode(derived).decode("ascii").rstrip("="),
    )


def hash_password(password: str) -> str:
    return _password_digest(password, secrets.token_bytes(16))


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, raw_iterations, raw_salt, raw_digest = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(raw_iterations)
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        salt = base64.urlsafe_b64decode(raw_salt + "=" * (-len(raw_salt) % 4))
        expected = base64.urlsafe_b64decode(raw_digest + "=" * (-len(raw_digest) % 4))
        actual = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _token_digest(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


def _now_utc(now: datetime) -> datetime:
    return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)


def _public_account(account: LocalAccount) -> dict[str, Any]:
    return {"account_id": account.account_id, "email": account.email, "created_at": account.created_at.isoformat()}


def _new_session(sessions: Any, account: LocalAccount, now: datetime) -> dict[str, Any]:
    token = secrets.token_urlsafe(32)
    session = LocalSession(
        session_id=f"session_{uuid4().hex}",
        account_id=account.account_id,
        token_digest=_token_digest(token),
        created_at=now,
        expires_at=now + SESSION_TTL,
    )
    sessions[session.session_id] = session
    return {
        "account": _public_account(account),
        "session_id": session.session_id,
        "access_token": token,
        "expires_at": session.expires_at.isoformat(),
        "auth_mode": "local_mock",
    }


def register(
    accounts: Any,
    sessions: Any,
    *,
    email: str,
    password: str,
    now: datetime,
) -> dict[str, Any]:
    normalized = normalize_email(email)
    if len(password) < 12:
        raise LocalAuthError(422, "validation_error", "Password does not meet the staging minimum.")
    if any(account.email == normalized and account.deleted_at is None for _, account in accounts.items()):
        raise LocalAuthError(409, "lifecycle_conflict", "An account with this email already exists.")
    current = _now_utc(now)
    account = LocalAccount(
        account_id=f"account_{uuid4().hex}",
        email=normalized,
        password_digest=hash_password(password),
        created_at=current,
    )
    accounts[account.account_id] = account
    try:
        return _new_session(sessions, account, current)
    except Exception:
        accounts.delete(account.account_id)
        raise


def login(
    accounts: Any,
    sessions: Any,
    *,
    email: str,
    password: str,
    now: datetime,
) -> dict[str, Any]:
    normalized = normalize_email(email)
    for _, account in accounts.items():
        if account.email == normalized and account.deleted_at is None and verify_password(password, account.password_digest):
            return _new_session(sessions, account, _now_utc(now))
    raise LocalAuthError(401, "bad_request", "Email or password is incorrect.")


def authenticate(accounts: Any, sessions: Any, authorization: str | None, *, now: datetime) -> tuple[LocalAccount, LocalSession]:
    if not authorization or not authorization.startswith("Bearer "):
        raise LocalAuthError(401, "bad_request", "Authentication is required.")
    token = authorization[7:].strip()
    if len(token) < 32 or len(token) > 256:
        raise LocalAuthError(401, "bad_request", "Authentication is required.")
    digest = _token_digest(token)
    current = _now_utc(now)
    for _, session in sessions.items():
        if hmac.compare_digest(session.token_digest, digest):
            if session.expires_at <= current:
                sessions.delete(session.session_id)
                raise LocalAuthError(401, "bad_request", "Authentication is required.")
            account = accounts.get(session.account_id)
            if account is None or account.deleted_at is not None:
                raise LocalAuthError(401, "bad_request", "Authentication is required.")
            return account, session
    raise LocalAuthError(401, "bad_request", "Authentication is required.")


def logout(sessions: Any, session: LocalSession) -> None:
    sessions.delete(session.session_id)


def delete_account(accounts: Any, sessions: Any, account: LocalAccount, *, now: datetime) -> None:
    # Remove every session before deleting the account record so a token cannot
    # remain usable after a successful deletion request.
    for key, session in tuple(sessions.items()):
        if session.account_id == account.account_id:
            sessions.delete(key)
    # Account credentials and contact data are hard-deleted. Research records
    # are separate and continue under their existing retention policy.
    accounts.delete(account.account_id)


def purge_expired_sessions(sessions: Any, *, now: datetime) -> int:
    current = _now_utc(now)
    removed = 0
    for key, session in tuple(sessions.items()):
        if session.expires_at <= current and sessions.delete(key):
            removed += 1
    return removed


__all__ = [
    "LocalAccount", "LocalAuthError", "LocalRegisterRequest", "LocalSession",
    "authenticate", "delete_account", "hash_password", "login", "logout",
    "purge_expired_sessions", "register", "verify_password",
]
