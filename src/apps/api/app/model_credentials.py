"""Account-scoped encrypted OpenAI-compatible model credentials.

This boundary accepts only public HTTPS OpenAI-compatible endpoints.  Ciphertext
is bound to the account record, and the runtime builds an isolated provider per
request rather than changing the process-wide platform provider.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .openai_compatible import OpenAICompatibleSettings
from .phase3 import ProviderFailure


class ModelCredentialError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class ModelConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["platform", "personal"]
    protocol: Literal["openai_chat_completions", "anthropic_messages"] | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    api_key: str | None = Field(default=None, min_length=8, max_length=512)
    clear_api_key: bool = False

    @model_validator(mode="after")
    def validate_key_action(self) -> "ModelConfigUpdate":
        if self.api_key is not None and self.clear_api_key:
            raise ValueError("api_key and clear_api_key are mutually exclusive")
        if self.mode == "platform" and any((self.protocol is not None, self.base_url is not None, self.model is not None, self.api_key is not None, self.clear_api_key)):
            raise ValueError("platform mode does not accept personal model fields")
        for name in ("base_url", "model", "api_key"):
            value = getattr(self, name)
            if value is not None and value != value.strip():
                raise ValueError(f"{name} must not contain surrounding whitespace")
        return self


class ModelConfigResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v1"] = "v1"
    mode: Literal["platform", "personal"]
    configured: bool
    protocol: Literal["openai_chat_completions", "anthropic_messages"] | None = None
    base_url: str | None = None
    model: str | None = None
    key_hint: str | None = Field(default=None, pattern=r"^••••[A-Za-z0-9]{4}$")
    updated_at: datetime | None = None


class ModelConfigTestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "unavailable"]


class StoredModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v1"] = "v1"
    account_id: str = Field(min_length=8, max_length=80)
    mode: Literal["platform", "personal"]
    protocol: Literal["openai_chat_completions", "anthropic_messages"] = "openai_chat_completions"
    base_url: str | None = Field(default=None, max_length=2048)
    model: str | None = Field(default=None, max_length=128)
    encrypted_api_key: str | None = Field(default=None, min_length=32, max_length=4096)
    key_hint: str | None = Field(default=None, pattern=r"^••••[A-Za-z0-9]{4}$")
    updated_at: datetime

    @model_validator(mode="after")
    def validate_personal_config(self) -> "StoredModelConfig":
        if self.mode == "personal" and not all((self.base_url, self.model, self.encrypted_api_key, self.key_hint)):
            raise ValueError("personal model configuration must be complete")
        return self


class ModelCredentialCipher:
    def __init__(self, key: bytes) -> None:
        if len(key) not in {16, 24, 32}:
            raise ValueError("model credential encryption key must be 16, 24, or 32 bytes")
        self._key = key

    @classmethod
    def from_environment(cls) -> "ModelCredentialCipher | None":
        path = os.getenv("FORESIGHTNINE_MODEL_CREDENTIAL_ENCRYPTION_KEY_FILE", "").strip()
        if not path:
            return None
        try:
            raw = Path(path).read_bytes().strip()
            if len(raw) not in {16, 24, 32}:
                raw = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
            return cls(raw)
        except (OSError, ValueError) as error:
            raise ModelCredentialError(503, "model_credential_storage_unavailable", "Model credential storage is unavailable.") from error

    def encrypt(self, account_id: str, api_key: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        encrypted = AESGCM(self._key).encrypt(nonce, api_key.encode("utf-8"), f"{account_id}:model:v1".encode())
        return base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")

    def decrypt(self, account_id: str, encrypted: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        try:
            payload = base64.urlsafe_b64decode(encrypted.encode("ascii"))
            return AESGCM(self._key).decrypt(payload[:12], payload[12:], f"{account_id}:model:v1".encode()).decode("utf-8")
        except Exception as error:
            raise ModelCredentialError(503, "model_credential_storage_unavailable", "Model credential storage is unavailable.") from error


def _hint(api_key: str) -> str:
    suffix = api_key[-4:]
    return f"••••{suffix if suffix.isascii() and suffix.isalnum() else sha256(api_key.encode('utf-8')).hexdigest()[-4:]}"


def _validate_personal_values(base_url: str, model: str, api_key: str) -> OpenAICompatibleSettings:
    try:
        return OpenAICompatibleSettings.from_account_values(base_url=base_url, model=model, api_key=api_key)
    except ProviderFailure as error:
        raise ModelCredentialError(422, "validation_error", "Personal model configuration is invalid.") from error


class AccountModelSettings:
    def __init__(self, repository: Any, cipher: ModelCredentialCipher | None) -> None:
        self.repository = repository
        self.cipher = cipher
        self._lock = RLock()

    def _stored(self, account_id: str) -> StoredModelConfig | None:
        value = self.repository.get(account_id)
        return value if value is None or isinstance(value, StoredModelConfig) else StoredModelConfig.model_validate(value)

    def snapshot(self, account_id: str) -> ModelConfigResponse:
        stored = self._stored(account_id)
        if stored is None or stored.mode == "platform":
            return ModelConfigResponse(mode="platform", configured=True, updated_at=stored.updated_at if stored else None)
        return ModelConfigResponse(
            mode="personal",
            configured=True,
            protocol=stored.protocol,
            base_url=stored.base_url,
            model=stored.model,
            key_hint=stored.key_hint,
            updated_at=stored.updated_at,
        )

    def update(self, account_id: str, request: ModelConfigUpdate, *, now: datetime | None = None) -> ModelConfigResponse:
        current_time = now or datetime.now(timezone.utc)

        def operation() -> ModelConfigResponse:
            with self._lock:
                existing = self._stored(account_id)
                if request.mode == "platform":
                    self.repository.put(account_id, StoredModelConfig(
                        account_id=account_id,
                        mode="platform",
                        updated_at=current_time,
                    ))
                    return self.snapshot(account_id)

                base_url = request.base_url if request.base_url is not None else (existing.base_url if existing else None)
                model = request.model if request.model is not None else (existing.model if existing else None)
                protocol = request.protocol or (existing.protocol if existing else "openai_chat_completions")
                if existing and existing.base_url and base_url and request.api_key is None:
                    try:
                        old, new = urlsplit(existing.base_url), urlsplit(base_url)
                        changed = (old.scheme.lower(), old.hostname, old.port or 443) != (new.scheme.lower(), new.hostname, new.port or 443)
                    except ValueError:
                        raise ModelCredentialError(422, "validation_error", "Personal model configuration is invalid.") from None
                    if changed or protocol != existing.protocol:
                        raise ModelCredentialError(422, "validation_error", "Changing model origin or protocol requires supplying the API key again.")
                encrypted = existing.encrypted_api_key if existing else None
                hint = existing.key_hint if existing else None
                if request.clear_api_key:
                    encrypted = None
                    hint = None
                elif request.api_key is not None:
                    if self.cipher is None:
                        raise ModelCredentialError(503, "model_credential_storage_unavailable", "Model credential storage is unavailable.")
                    encrypted = self.cipher.encrypt(account_id, request.api_key)
                    hint = _hint(request.api_key)
                if not all((base_url, model, encrypted, hint)):
                    raise ModelCredentialError(422, "validation_error", "Personal model configuration is incomplete.")
                if self.cipher is None:
                    raise ModelCredentialError(503, "model_credential_storage_unavailable", "Model credential storage is unavailable.")
                api_key = self.cipher.decrypt(account_id, encrypted)
                _validate_personal_values(base_url, model, api_key)
                self.repository.put(account_id, StoredModelConfig(
                    account_id=account_id,
                    mode="personal",
                    protocol=protocol,
                    base_url=base_url,
                    model=model,
                    encrypted_api_key=encrypted,
                    key_hint=hint,
                    updated_at=current_time,
                ))
                return self.snapshot(account_id)

        locker = getattr(self.repository, "with_advisory_lock", None)
        return locker(f"model-config:{account_id}", operation) if callable(locker) else operation()

    def runtime_configuration(self, account_id: str) -> tuple[OpenAICompatibleSettings | None, StoredModelConfig | None]:
        stored = self._stored(account_id)
        if stored is None or stored.mode == "platform":
            return None, stored
        if self.cipher is None:
            raise ModelCredentialError(503, "model_credential_storage_unavailable", "Model credential storage is unavailable.")
        if not all((stored.base_url, stored.model, stored.encrypted_api_key)):
            raise ModelCredentialError(503, "model_credential_storage_unavailable", "Model credential storage is unavailable.")
        api_key = self.cipher.decrypt(account_id, stored.encrypted_api_key)
        try:
            return OpenAICompatibleSettings.from_account_values(
                base_url=stored.base_url,
                model=stored.model,
                api_key=api_key,
            ), stored
        except ProviderFailure as error:
            raise ModelCredentialError(503, "model_credential_storage_unavailable", "Model credential storage is unavailable.") from error

    def runtime_settings(self, account_id: str) -> OpenAICompatibleSettings | None:
        return self.runtime_configuration(account_id)[0]

    def delete(self, account_id: str) -> None:
        self.repository.delete(account_id)


__all__ = [
    "AccountModelSettings", "ModelConfigResponse", "ModelConfigTestResponse", "ModelConfigUpdate",
    "ModelCredentialCipher", "ModelCredentialError", "StoredModelConfig",
]
