"""Account-scoped encrypted OpenAI-compatible model credentials.

This boundary accepts only public HTTPS OpenAI-compatible endpoints.  Ciphertext
is bound to the account record, and the runtime builds an isolated provider per
request rather than changing the process-wide platform provider.
"""
from __future__ import annotations

import base64
from dataclasses import replace
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
from .provider_capabilities import ChatGenerationParameters


ModelProtocol = Literal["openai_chat_completions", "openai_responses", "anthropic_messages"]


class ModelCredentialError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class MessagesOptions(BaseModel):
    """Wire values supplied for this Messages endpoint."""
    model_config = ConfigDict(extra="forbid", strict=True)

    version: str = Field(min_length=1, max_length=64, pattern=r"^[!-~]+$")
    auth_scheme: Literal["x-api-key", "bearer"]
    max_tokens: int = Field(gt=0)
    effort: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")


class ModelConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["platform", "personal"]
    protocol: ModelProtocol | None = None
    messages_options: MessagesOptions | None = None
    generation_parameters: ChatGenerationParameters | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    api_key: str | None = Field(default=None, min_length=8, max_length=512)
    clear_api_key: bool = False

    @model_validator(mode="after")
    def validate_key_action(self) -> "ModelConfigUpdate":
        if self.api_key is not None and self.clear_api_key:
            raise ValueError("api_key and clear_api_key are mutually exclusive")
        if self.mode == "platform" and any((self.protocol is not None, self.base_url is not None, self.model is not None, self.api_key is not None, self.clear_api_key, self.messages_options is not None, self.generation_parameters is not None)):
            raise ValueError("platform mode does not accept personal model fields")
        for name in ("base_url", "model", "api_key"):
            value = getattr(self, name)
            if value is not None and value != value.strip():
                raise ValueError(f"{name} must not contain surrounding whitespace")
        return self


class ModelConfigResponse(BaseModel):
    """Account model settings.

    In platform mode the personal fields, when present, describe a saved but
    inactive personal configuration that switching back to personal mode reuses.
    """
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v1"] = "v1"
    mode: Literal["platform", "personal"]
    configured: bool
    protocol: ModelProtocol | None = None
    messages_options: MessagesOptions | None = None
    generation_parameters: ChatGenerationParameters | None = None
    base_url: str | None = None
    model: str | None = None
    key_hint: str | None = Field(default=None, pattern=r"^••••[A-Za-z0-9]{4}$")
    research_tools_verified_at: datetime | None = None
    updated_at: datetime | None = None


class ModelConfigTestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "unavailable"]
    upstream_status: int | None = Field(default=None, ge=400, le=599)
    parameter: str | None = None


class StoredModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v1"] = "v1"
    account_id: str = Field(min_length=8, max_length=80)
    mode: Literal["platform", "personal"]
    protocol: ModelProtocol = "openai_chat_completions"
    messages_options: MessagesOptions | None = None
    generation_parameters: ChatGenerationParameters | None = None
    base_url: str | None = Field(default=None, max_length=2048)
    model: str | None = Field(default=None, max_length=128)
    encrypted_api_key: str | None = Field(default=None, min_length=32, max_length=4096)
    key_hint: str | None = Field(default=None, pattern=r"^••••[A-Za-z0-9]{4}$")
    research_tools_verified_at: datetime | None = None
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
        if stored is None:
            return ModelConfigResponse(mode="platform", configured=True)
        if not stored.key_hint:
            return ModelConfigResponse(mode=stored.mode, configured=True, updated_at=stored.updated_at)
        return ModelConfigResponse(
            mode=stored.mode, configured=True, protocol=stored.protocol,
            generation_parameters=stored.generation_parameters,
            messages_options=stored.messages_options, research_tools_verified_at=stored.research_tools_verified_at,
            base_url=stored.base_url, model=stored.model, key_hint=stored.key_hint, updated_at=stored.updated_at,
        )

    def _mutate(self, account_id, operation):
        locker = getattr(self.repository, "with_advisory_lock", None)
        if callable(locker):
            return locker(f"model-config:{account_id}", operation)
        with self._lock:
            return operation()

    def update(self, account_id: str, request: ModelConfigUpdate, *, now: datetime | None = None) -> ModelConfigResponse:
        current_time = now or datetime.now(timezone.utc)

        def operation() -> ModelConfigResponse:
            existing = self._stored(account_id)
            if request.mode == "platform":
                # Keep a saved personal configuration (key still encrypted) so the
                # account can switch back without re-entering it; DELETE removes it.
                self.repository.put(account_id, StoredModelConfig(
                    account_id=account_id,
                    mode="platform",
                    generation_parameters=existing.generation_parameters if existing else None,
                    messages_options=existing.messages_options if existing else None,
                    research_tools_verified_at=existing.research_tools_verified_at if existing else None,
                    protocol=existing.protocol if existing else "openai_chat_completions",
                    base_url=existing.base_url if existing else None,
                    model=existing.model if existing else None,
                    encrypted_api_key=existing.encrypted_api_key if existing else None,
                    key_hint=existing.key_hint if existing else None,
                    updated_at=current_time,
                ))
                return self.snapshot(account_id)

            base_url = request.base_url if request.base_url is not None else (existing.base_url if existing else None)
            model = request.model if request.model is not None else (existing.model if existing else None)
            protocol = request.protocol or (existing.protocol if existing else "openai_chat_completions")
            if request.messages_options and protocol != "anthropic_messages":
                raise ModelCredentialError(422, "validation_error", "Messages options require the Messages protocol.")
            parameters = request.generation_parameters
            if parameters is not None and protocol == "anthropic_messages":
                raise ModelCredentialError(422, "validation_error", "Generation parameters require a Chat Completions or Responses connection.")
            if protocol == "openai_responses" and parameters and parameters.seed is not None:
                raise ModelCredentialError(422, "validation_error", "Responses does not accept seed.")
            options = request.messages_options
            if existing and (protocol, base_url, model) == (existing.protocol, existing.base_url, existing.model):
                if options is None:
                    options = existing.messages_options
                if "generation_parameters" not in request.model_fields_set:
                    parameters = existing.generation_parameters
            if parameters is not None and not parameters.model_dump(exclude_none=True):
                parameters = None
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
            same_binding = existing and (protocol, base_url, model, encrypted, options, parameters) == (
                existing.protocol, existing.base_url, existing.model, existing.encrypted_api_key, existing.messages_options, existing.generation_parameters)
            self.repository.put(account_id, StoredModelConfig(
                account_id=account_id,
                mode="personal",
                messages_options=options,
                generation_parameters=parameters,
                research_tools_verified_at=existing.research_tools_verified_at if same_binding else None,
                protocol=protocol,
                base_url=base_url,
                model=model,
                encrypted_api_key=encrypted,
                key_hint=hint,
                updated_at=current_time,
            ))
            return self.snapshot(account_id)

        return self._mutate(account_id, operation)

    def saved_discovery_key(self, account_id: str, protocol: str, base_url: str) -> str:
        stored = self._stored(account_id)
        if (not stored or not stored.base_url or stored.base_url.rstrip("/") != base_url.rstrip("/")
                or stored.protocol != protocol or not stored.encrypted_api_key):
            raise ModelCredentialError(422, "validation_error", "Supply the API key for this endpoint.")
        if self.cipher is None:
            raise ModelCredentialError(503, "model_credential_storage_unavailable", "Model credential storage is unavailable.")
        return self.cipher.decrypt(account_id, stored.encrypted_api_key)

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
            settings = OpenAICompatibleSettings.from_account_values(
                base_url=stored.base_url,
                model=stored.model,
                api_key=api_key,
            )
            return replace(settings, tools_verified=stored.research_tools_verified_at is not None,
                           generation_parameters=stored.generation_parameters), stored
        except ProviderFailure as error:
            raise ModelCredentialError(503, "model_credential_storage_unavailable", "Model credential storage is unavailable.") from error

    def runtime_settings(self, account_id: str) -> OpenAICompatibleSettings | None:
        return self.runtime_configuration(account_id)[0]

    def record_tools_result(self, tested: StoredModelConfig, verified: bool) -> bool:
        """Commit only against the revision actually probed, including its previous verification."""
        def record() -> bool:
            current = self._stored(tested.account_id)
            if current != tested:
                return False
            verified_at = datetime.now(timezone.utc) if verified else None
            if verified_at != current.research_tools_verified_at:
                self.repository.put(tested.account_id, current.model_copy(update={"research_tools_verified_at": verified_at}))
            return True
        return self._mutate(tested.account_id, record)

    def delete(self, account_id: str) -> None:
        self._mutate(account_id, lambda: self.repository.delete(account_id))


__all__ = [
    "AccountModelSettings", "ModelConfigResponse", "ModelConfigTestResponse", "ModelConfigUpdate",
    "ModelCredentialCipher", "ModelCredentialError", "StoredModelConfig",
]
