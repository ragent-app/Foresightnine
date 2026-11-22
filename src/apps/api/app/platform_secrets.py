"""Platform-wide secrets (search provider keys, shared data keys) editable from the admin page.

A secret is encrypted at rest with the existing credential key file; its AAD binds it to ``platform-secret:<name>`` so a
ciphertext cannot be swapped between names or into an account credential record.  ``platform_secret(name)`` resolves the
database value first, then the environment, with a short process cache for database reads (invalidated on update).
Values never appear in listings, audit records, logs or exceptions.
"""
from __future__ import annotations

import base64
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .market_credentials import MarketCredentialCipher, MarketCredentialError

AAD_ACCOUNT = "platform-secret"   # MarketCredentialCipher AAD = "platform-secret:<name>:v1"
CACHE_TTL_ENV = "FORESIGHTNINE_PLATFORM_SECRET_CACHE_SECONDS"
DEFAULT_CACHE_TTL = 60.0

SEARCH_PROVIDER_IDS = ("brave", "tavily", "exa", "parallel", "qianfan", "bocha")
TWELVEDATA_ENV = "FORESIGHTNINE_TWELVEDATA_API_KEY"
TUSHARE_ENV = "FORESIGHTNINE_TUSHARE_TOKEN"
GLM_ENV = "FORESIGHTNINE_GLM_CODING_API_KEY"


def search_secret_name(provider_id: str) -> str:
    return f"FORESIGHTNINE_{provider_id.upper()}_SEARCH_API_KEY"


@dataclass(frozen=True)
class ManagedSecret:
    name: str
    label: str
    category: str            # search | data | other
    provider_id: str | None = None
    signup_url: str | None = None
    docs_url: str | None = None
    notes: str | None = None
    region: str | None = None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return value.get("zh") or value.get("en") or next(iter(value.values()), None)
    if isinstance(value, (tuple, list)) and value:
        return str(value[-1])
    return str(value)


def provider_registry() -> Mapping[str, Any]:
    """The search provider registry owned by ``app.news.providers`` (empty until it exists)."""
    try:
        from .news.providers import PROVIDERS
        return PROVIDERS
    except Exception:
        return {}


def managed_secrets() -> list[ManagedSecret]:
    registry = provider_registry()
    ids = [i for i in SEARCH_PROVIDER_IDS if i in registry] + [i for i in registry if i not in SEARCH_PROVIDER_IDS]
    if not registry:
        ids = list(SEARCH_PROVIDER_IDS)
    items: list[ManagedSecret] = []
    for pid in ids:
        spec = registry.get(pid)
        items.append(ManagedSecret(
            name=search_secret_name(pid), label=_text(getattr(spec, "label", None)) or pid.title(), category="search", provider_id=pid,
            signup_url=getattr(spec, "signup_url", None), docs_url=getattr(spec, "docs_url", None),
            notes=_text(getattr(spec, "notes", None)), region=getattr(spec, "region", None)))
    items += [
        ManagedSecret(GLM_ENV, "智谱 GLM 搜索", "search", docs_url="https://docs.bigmodel.cn/"),
        ManagedSecret(TWELVEDATA_ENV, "Twelve Data", "data", signup_url="https://twelvedata.com/pricing", docs_url="https://twelvedata.com/docs"),
        ManagedSecret(TUSHARE_ENV, "Tushare Pro", "data", signup_url="https://tushare.pro/register", docs_url="https://tushare.pro/document/1"),
    ]
    return items


def managed_secret(name: str) -> ManagedSecret | None:
    return next((item for item in managed_secrets() if item.name == name), None)


class LastTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    reason: str = Field(max_length=120)
    at: datetime


class StoredPlatformSecret(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=3, max_length=120)
    encrypted_value: str | None = Field(default=None, max_length=8192)
    key_hint: str | None = Field(default=None, max_length=12)
    updated_at: datetime
    updated_by: str | None = Field(default=None, max_length=80)
    last_test: LastTest | None = None


def _hint(value: str) -> str:
    suffix = value[-4:] if len(value) > 4 else ""
    return "••••" + (suffix if len(suffix) == 4 and suffix.isascii() and suffix.isalnum() else sha256(value.encode()).hexdigest()[-4:])


def default_cipher() -> MarketCredentialCipher | None:
    """The market credential key file, falling back to the model credential key file."""
    cipher = MarketCredentialCipher.from_environment()
    if cipher is not None:
        return cipher
    path = os.getenv("FORESIGHTNINE_MODEL_CREDENTIAL_ENCRYPTION_KEY_FILE", "").strip()
    if not path:
        return None
    try:
        raw = Path(path).read_bytes().strip()
        if len(raw) not in {16, 24, 32}:
            raw = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
        return MarketCredentialCipher(raw)
    except (OSError, ValueError) as error:
        raise MarketCredentialError(503, "platform_secret_storage_unavailable", "Platform secret storage is unavailable.") from error


class PlatformSecretError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


class PlatformSecretStore:
    def __init__(self, repository: Any, audit: Any, cipher: MarketCredentialCipher | None, *, now: Any = None) -> None:
        self.repository, self.audit, self.cipher = repository, audit, cipher
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[str | None, float]] = {}

    # -- storage ---------------------------------------------------------------------------------
    def record(self, name: str) -> StoredPlatformSecret | None:
        value = self.repository.get(name)
        return value if value is None or isinstance(value, StoredPlatformSecret) else StoredPlatformSecret.model_validate(value)

    def _require_cipher(self) -> MarketCredentialCipher:
        if self.cipher is None:
            raise PlatformSecretError(503, "platform_secret_storage_unavailable", "Platform secret storage is unavailable.")
        return self.cipher

    def set(self, name: str, value: str, *, actor: str) -> StoredPlatformSecret:
        cipher = self._require_cipher()
        with self._lock:
            previous = self.record(name)
            stored = StoredPlatformSecret(
                name=name, encrypted_value=cipher.encrypt(AAD_ACCOUNT, name, value), key_hint=_hint(value),
                updated_at=self._now(), updated_by=actor, last_test=None)
            self.repository.put(name, stored)
            self.invalidate(name)
        self.log(actor, "set" if previous is None or not previous.encrypted_value else "replace", name, "ok")
        return stored

    def clear(self, name: str, *, actor: str) -> None:
        with self._lock:
            self.repository.delete(name)
            self.invalidate(name)
        self.log(actor, "clear", name, "ok")

    def record_test(self, name: str, ok: bool, reason: str, *, actor: str, expected_ciphertext: str | None = None) -> None:
        with self._lock:
            current = self.record(name)
            if current is not None and current.encrypted_value == expected_ciphertext:
                self.repository.put(name, current.model_copy(update={"last_test": LastTest(ok=ok, reason=reason[:120], at=self._now())}))
        self.log(actor, "test", name, "ok" if ok else reason[:120])

    def log(self, actor: str | None, action: str, name: str | None, result: str) -> None:
        """Append-only audit record (who, when, action, name, result); never a value."""
        at = self._now()
        entry_id = f"{at.strftime('%Y%m%dT%H%M%S%f')}-{os.urandom(4).hex()}"
        self.audit[entry_id] = {"at": at.isoformat(), "actor": actor, "action": action, "name": name, "result": result[:120]}

    # -- resolution ------------------------------------------------------------------------------
    def _db_value(self, name: str) -> str | None:
        ttl = _ttl()
        cached = self._cache.get(name)
        now = time.monotonic()
        if cached is not None and cached[1] > now:
            return cached[0]
        value: str | None = None
        try:
            record = self.record(name)
            if record is not None and record.encrypted_value:
                value = self._require_cipher().decrypt(AAD_ACCOUNT, name, record.encrypted_value).strip() or None
                if value is None:
                    raise ValueError("empty secret")
        except Exception as error:
            raise PlatformSecretError(503, "platform_secret_storage_unavailable", "Platform secret storage is unavailable.") from error
        self._cache[name] = (value, now + ttl)
        return value

    def invalidate(self, name: str | None = None) -> None:
        if name is None:
            self._cache.clear()
        else:
            self._cache.pop(name, None)

    def resolve(self, name: str) -> tuple[str | None, str]:
        """(value, source) with source in db | env | none."""
        db = self._db_value(name)
        if db:
            return db, "db"
        env = os.environ.get(name, "").strip()
        return (env, "env") if env else (None, "none")


def _ttl() -> float:
    try:
        return max(0.0, float(os.environ.get(CACHE_TTL_ENV, DEFAULT_CACHE_TTL)))
    except ValueError:
        return DEFAULT_CACHE_TTL


_store: PlatformSecretStore | None = None


def configure(store: PlatformSecretStore | None) -> None:
    global _store
    _store = store


def platform_secret(name: str) -> str | None:
    """Database value if set, else the environment variable; None when neither is set."""
    if _store is None:
        return os.environ.get(name, "").strip() or None
    return _store.resolve(name)[0]


def search_provider_key(provider_id: str) -> str | None:
    return platform_secret(search_secret_name(provider_id))


__all__ = [
    "ManagedSecret", "PlatformSecretError", "PlatformSecretStore", "StoredPlatformSecret", "configure", "default_cipher",
    "managed_secret", "managed_secrets", "platform_secret", "provider_registry", "search_provider_key", "search_secret_name",
]
