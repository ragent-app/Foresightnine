"""Authorization for the platform admin API.

Eligibility (all from the environment, nothing hard-coded):
  1. the request's account id is in FORESIGHTNINE_ADMIN_ACCOUNT_IDS, and
  2. that account has a linked OAuth identity (provider in FORESIGHTNINE_ADMIN_OAUTH_PROVIDERS, default google) whose
     email (verified by the provider before the identity is stored) equals one of FORESIGHTNINE_ADMIN_EMAILS.
The API is disabled unless both lists are set.  If FORESIGHTNINE_ADMIN_PASSPHRASE_HASH is set, a passphrase is also
required and yields a 15-minute server-side elevation bound to the login session (sent as ``X-Admin-Elevation``).
Bearer-header authentication carries no ambient credential, so there is no cookie CSRF surface; CORS and the global
security middleware apply unchanged.  Failed passphrase attempts are limited per account and per IP.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

ELEVATION_TTL = timedelta(minutes=15)
MAX_FAILURES = 5
FAILURE_WINDOW = timedelta(minutes=15)
ELEVATION_HEADER = "X-Admin-Elevation"
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1


class AdminAuthError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status_code, self.code, self.message, self.retry_after = status_code, code, message, retry_after


def _scrypt(passphrase: str, salt: bytes, n: int, r: int, p: int, length: int = 32) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=length, maxmem=256 * 1024 * 1024)


def hash_passphrase(passphrase: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else os.urandom(16)
    digest = _scrypt(passphrase, salt, n, r, p)
    return "scrypt${}${}${}${}${}".format(n, r, p, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_passphrase(passphrase: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        if (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            return False
        expected_bytes = base64.b64decode(expected, validate=True)
        salt_bytes = base64.b64decode(salt, validate=True)
        if len(expected_bytes) != 32 or len(salt_bytes) != 16:
            return False
        actual = _scrypt(passphrase, salt_bytes, int(n), int(r), int(p), len(expected_bytes))
    except Exception:
        return False
    return hmac.compare_digest(actual, expected_bytes)


def _csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class AdminGate:
    def __init__(self, identities: Any, elevations: Any, attempts: Any, now: Callable[[], datetime],
                 environ: Mapping[str, str] | None = None) -> None:
        self.identities, self.elevations, self.attempts, self._now = identities, elevations, attempts, now
        self._environ = environ
        self._lock = threading.RLock()

    # -- configuration -----------------------------------------------------------------------
    def _env(self, name: str) -> str:
        return (self._environ if self._environ is not None else os.environ).get(name, "")

    @property
    def account_ids(self) -> list[str]:
        return _csv(self._env("FORESIGHTNINE_ADMIN_ACCOUNT_IDS"))

    @property
    def emails(self) -> list[str]:
        return [e.lower() for e in _csv(self._env("FORESIGHTNINE_ADMIN_EMAILS"))]

    @property
    def providers(self) -> list[str]:
        return [p.lower() for p in _csv(self._env("FORESIGHTNINE_ADMIN_OAUTH_PROVIDERS"))] or ["google"]

    @property
    def passphrase_hash(self) -> str:
        return self._env("FORESIGHTNINE_ADMIN_PASSPHRASE_HASH").strip()

    def enabled(self) -> bool:
        return bool(self.account_ids and self.emails)

    def eligible(self, account: Any, session: Any) -> bool:
        if not self.enabled() or account.account_id not in self.account_ids or getattr(session, "auth_provider", None) != "google":
            return False
        emails, providers = self.emails, self.providers
        return any(identity.account_id == account.account_id and str(identity.provider).lower() == "google" and "google" in providers
                   and identity.email.strip().lower() in emails for _, identity in self.identities.items())

    # -- state ---------------------------------------------------------------------------------
    def _active_elevation(self, account: Any, session: Any, token: str | None) -> dict | None:
        if not token or len(token) > 256:
            return None
        digest, now = _digest(token), self._now()
        for _, record in self.elevations.items():
            if (hmac.compare_digest(record["token_digest"], digest) and record["account_id"] == account.account_id
                    and record["session_id"] == session.session_id and not record.get("revoked_at") and _iso(record["expires_at"]) > now):
                return record
        return None

    def status(self, account: Any, session: Any, token: str | None) -> dict[str, Any]:
        enabled = self.enabled()
        eligible = enabled and self.eligible(account, session)
        needs = bool(self.passphrase_hash)
        record = self._active_elevation(account, session, token) if eligible and needs else None
        elevated = eligible and (not needs or record is not None)
        return {"enabled": enabled, "eligible": eligible, "passphrase_required": needs, "elevated": elevated,
                "expires_at": record["expires_at"] if record else None}

    def _authorize(self, account: Any, session: Any) -> None:
        if not self.enabled():
            raise AdminAuthError(403, "admin_disabled", "The admin API is not enabled.")
        if not self.eligible(account, session):
            raise AdminAuthError(403, "admin_forbidden", "This account is not permitted to use the admin API.")

    def require(self, account: Any, session: Any, token: str | None) -> None:
        self._authorize(account, session)
        if self.passphrase_hash and self._active_elevation(account, session, token) is None:
            raise AdminAuthError(401, "admin_elevation_required", "Admin elevation is required or has expired.")

    # -- elevation ----------------------------------------------------------------------------
    def _failures(self, key: str) -> list[datetime]:
        record = self.attempts.get(key) or {}
        cutoff = self._now() - FAILURE_WINDOW
        return [t for t in (_iso(x) for x in record.get("failures", [])) if t > cutoff]

    def _locked(self, keys: list[str]) -> int | None:
        now = self._now()
        worst: int | None = None
        for key in keys:
            failures = self._failures(key)
            if len(failures) >= MAX_FAILURES:
                wait = int((min(failures) + FAILURE_WINDOW - now).total_seconds()) + 1
                worst = max(worst or 0, max(wait, 1))
        return worst

    def open(self, account: Any, session: Any, passphrase: str | None, client_ip: str) -> dict[str, Any]:
        self._authorize(account, session)
        keys = [f"acct:{account.account_id}", f"ip:{client_ip}"]
        with self._lock:
            wait = self._locked(keys)
            if wait is not None:
                raise AdminAuthError(429, "admin_locked", "Too many failed attempts; try again later.", retry_after=wait)
            stored = self.passphrase_hash
            if stored and not (isinstance(passphrase, str) and verify_passphrase(passphrase, stored)):
                now = self._now()
                for key in keys:
                    self.attempts[key] = {"failures": [t.isoformat() for t in [*self._failures(key), now]]}
                raise AdminAuthError(403, "admin_passphrase_invalid", "The passphrase is not correct.")
            self.attempts.delete(keys[0])
            now = self._now()
            for key, record in tuple(self.elevations.items()):
                if _iso(record["expires_at"]) <= now or record.get("revoked_at"):
                    self.elevations.delete(key)
            token = secrets.token_urlsafe(32)
            expires = now + ELEVATION_TTL
            elevation_id = f"elev_{secrets.token_hex(8)}"
            self.elevations[elevation_id] = {
                "account_id": account.account_id, "session_id": session.session_id, "token_digest": _digest(token),
                "created_at": now.isoformat(), "expires_at": expires.isoformat(), "revoked_at": None}
        return {"elevation_token": token, "expires_at": expires.isoformat()}

    def revoke(self, account: Any, session: Any, token: str | None) -> None:
        record = self._active_elevation(account, session, token)
        if record is None:
            return
        for key, item in tuple(self.elevations.items()):
            if item["token_digest"] == record["token_digest"]:
                self.elevations[key] = {**item, "revoked_at": self._now().isoformat()}

    def revoke_account(self, account_id: str) -> None:
        for key, item in tuple(self.elevations.items()):
            if item["account_id"] == account_id:
                self.elevations.delete(key)


__all__ = ["AdminAuthError", "AdminGate", "ELEVATION_HEADER", "hash_passphrase", "verify_passphrase"]
