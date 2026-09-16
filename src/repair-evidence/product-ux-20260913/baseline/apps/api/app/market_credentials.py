"""Account-scoped encrypted credentials for the admitted market-data chain."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import os
from pathlib import Path
from threading import RLock
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


MarketProviderId = Literal["market_data", "alpha_vantage", "twelve_data", "finnhub", "yahoo"]
MARKET_PROVIDER_ORDER: tuple[MarketProviderId, ...] = (
    "market_data", "alpha_vantage", "twelve_data", "finnhub", "yahoo",
)
KEYED_MARKET_PROVIDERS = frozenset({"market_data", "alpha_vantage", "twelve_data", "finnhub"})
PROVIDER_LABELS: Mapping[str, str] = {
    "market_data": "Market Data",
    "alpha_vantage": "Alpha Vantage",
    "twelve_data": "Twelve Data",
    "finnhub": "Finnhub",
    "yahoo": "Yahoo Finance",
}
PROVIDER_ENV: Mapping[str, str] = {
    "market_data": "FORESIGHTNINE_MARKETDATA_API_KEY",
    "alpha_vantage": "FORESIGHTNINE_ALPHAVANTAGE_API_KEY",
    "twelve_data": "FORESIGHTNINE_TWELVEDATA_API_KEY",
    "finnhub": "FORESIGHTNINE_FINNHUB_API_KEY",
}


class MarketCredentialError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class StoredMarketProvider(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: MarketProviderId
    enabled: bool = True
    encrypted_api_key: str | None = Field(default=None, min_length=32, max_length=4096)
    key_hint: str | None = Field(default=None, min_length=4, max_length=12)


class StoredMarketDataConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["v1"] = "v1"
    account_id: str = Field(min_length=8, max_length=80)
    fallback_order: list[MarketProviderId]
    providers: list[StoredMarketProvider]
    updated_at: datetime

    @model_validator(mode="after")
    def validate_provider_set(self) -> "StoredMarketDataConfig":
        expected = set(MARKET_PROVIDER_ORDER)
        if len(self.fallback_order) != len(expected) or set(self.fallback_order) != expected:
            raise ValueError("fallback_order must contain every supported provider exactly once")
        ids = [item.provider_id for item in self.providers]
        if len(ids) != len(expected) or set(ids) != expected:
            raise ValueError("providers must contain every supported provider exactly once")
        return self


class MarketProviderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provider_id: MarketProviderId
    enabled: bool
    api_key: str | None = Field(default=None, min_length=1, max_length=512)
    clear_api_key: bool = False

    @model_validator(mode="after")
    def validate_key_action(self) -> "MarketProviderUpdate":
        if self.api_key is not None and self.clear_api_key:
            raise ValueError("api_key and clear_api_key are mutually exclusive")
        if self.provider_id not in KEYED_MARKET_PROVIDERS and (self.api_key is not None or self.clear_api_key):
            raise ValueError("this provider does not accept an API key")
        if self.api_key is not None and self.api_key != self.api_key.strip():
            raise ValueError("api_key must not contain surrounding whitespace")
        return self


class MarketDataConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    fallback_order: list[MarketProviderId]
    providers: list[MarketProviderUpdate]

    @model_validator(mode="after")
    def validate_provider_set(self) -> "MarketDataConfigUpdate":
        expected = set(MARKET_PROVIDER_ORDER)
        if len(self.fallback_order) != len(expected) or set(self.fallback_order) != expected:
            raise ValueError("fallback_order must contain every supported provider exactly once")
        ids = [item.provider_id for item in self.providers]
        if len(ids) != len(expected) or set(ids) != expected:
            raise ValueError("providers must contain every supported provider exactly once")
        return self


class MarketProviderSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: MarketProviderId
    display_name: str
    enabled: bool
    requires_key: bool
    personal_key_configured: bool
    platform_available: bool
    key_hint: str | None = None


class MarketDataConfigResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["v1"] = "v1"
    fallback_order: list[MarketProviderId]
    providers: list[MarketProviderSummary]
    updated_at: datetime | None = None


class MarketProviderTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provider_id: MarketProviderId


class MarketProviderTestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: MarketProviderId
    status: Literal["ready", "unavailable"]


class MarketCredentialCipher:
    def __init__(self, key: bytes) -> None:
        if len(key) not in {16, 24, 32}:
            raise ValueError("market credential encryption key must be 16, 24, or 32 bytes")
        self._key = key

    @classmethod
    def from_environment(cls) -> "MarketCredentialCipher | None":
        path = os.getenv("FORESIGHTNINE_MARKET_CREDENTIAL_ENCRYPTION_KEY_FILE", "").strip()
        if not path:
            return None
        try:
            raw = Path(path).read_bytes().strip()
            if len(raw) not in {16, 24, 32}:
                raw = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
            return cls(raw)
        except (OSError, ValueError) as error:
            raise MarketCredentialError(503, "market_credential_storage_unavailable", "Market credential storage is unavailable.") from error

    def encrypt(self, account_id: str, provider_id: str, api_key: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        aad = f"{account_id}:{provider_id}:v1".encode()
        encrypted = AESGCM(self._key).encrypt(nonce, api_key.encode(), aad)
        return base64.urlsafe_b64encode(nonce + encrypted).decode()

    def decrypt(self, account_id: str, provider_id: str, encrypted: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        try:
            payload = base64.urlsafe_b64decode(encrypted.encode())
            aad = f"{account_id}:{provider_id}:v1".encode()
            return AESGCM(self._key).decrypt(payload[:12], payload[12:], aad).decode()
        except Exception as error:
            raise MarketCredentialError(503, "market_credential_storage_unavailable", "Market credential storage is unavailable.") from error


def _hint(api_key: str) -> str:
    return f"••••{api_key[-4:]}"


class AccountMarketDataSettings:
    def __init__(self, repository: Any, cipher: MarketCredentialCipher | None) -> None:
        self.repository = repository
        self.cipher = cipher
        self._lock = RLock()

    def _stored(self, account_id: str) -> StoredMarketDataConfig | None:
        value = self.repository.get(account_id)
        return value if value is None or isinstance(value, StoredMarketDataConfig) else StoredMarketDataConfig.model_validate(value)

    def snapshot(self, account_id: str) -> MarketDataConfigResponse:
        stored = self._stored(account_id)
        records = {item.provider_id: item for item in stored.providers} if stored else {}
        providers = []
        for provider_id in MARKET_PROVIDER_ORDER:
            record = records.get(provider_id)
            platform_available = provider_id == "yahoo" or bool(os.getenv(PROVIDER_ENV.get(provider_id, ""), "").strip())
            providers.append(MarketProviderSummary(
                provider_id=provider_id,
                display_name=PROVIDER_LABELS[provider_id],
                enabled=record.enabled if record else True,
                requires_key=provider_id in KEYED_MARKET_PROVIDERS,
                personal_key_configured=bool(record and record.encrypted_api_key),
                platform_available=platform_available,
                key_hint=record.key_hint if record else None,
            ))
        return MarketDataConfigResponse(
            fallback_order=list(stored.fallback_order) if stored else list(MARKET_PROVIDER_ORDER),
            providers=providers,
            updated_at=stored.updated_at if stored else None,
        )

    def update(self, account_id: str, request: MarketDataConfigUpdate, *, now: datetime | None = None) -> MarketDataConfigResponse:
        current_time = now or datetime.now(timezone.utc)
        def operation() -> MarketDataConfigResponse:
            with self._lock:
                existing = self._stored(account_id)
                existing_by_id = {item.provider_id: item for item in existing.providers} if existing else {}
                records = []
                for item in request.providers:
                    previous = existing_by_id.get(item.provider_id)
                    encrypted = previous.encrypted_api_key if previous else None
                    hint = previous.key_hint if previous else None
                    if item.clear_api_key:
                        encrypted = None
                        hint = None
                    elif item.api_key is not None:
                        if self.cipher is None:
                            raise MarketCredentialError(503, "market_credential_storage_unavailable", "Market credential storage is unavailable.")
                        encrypted = self.cipher.encrypt(account_id, item.provider_id, item.api_key)
                        hint = _hint(item.api_key)
                    records.append(StoredMarketProvider(provider_id=item.provider_id, enabled=item.enabled, encrypted_api_key=encrypted, key_hint=hint))
                stored = StoredMarketDataConfig(account_id=account_id, fallback_order=request.fallback_order, providers=records, updated_at=current_time)
                self.repository.put(account_id, stored)
                return self.snapshot(account_id)
        locker = getattr(self.repository, "with_advisory_lock", None)
        return locker(f"market-config:{account_id}", operation) if callable(locker) else operation()

    def runtime(self, account_id: str) -> tuple[tuple[str, ...] | None, dict[str, str]]:
        stored = self._stored(account_id)
        if stored is None:
            return None, {}
        records = {item.provider_id: item for item in stored.providers}
        order = tuple(provider_id for provider_id in stored.fallback_order if records[provider_id].enabled)
        credentials: dict[str, str] = {}
        for provider_id in order:
            encrypted = records[provider_id].encrypted_api_key
            if encrypted:
                if self.cipher is None:
                    raise MarketCredentialError(503, "market_credential_storage_unavailable", "Market credential storage is unavailable.")
                credentials[provider_id] = self.cipher.decrypt(account_id, provider_id, encrypted)
        return order, credentials

    def delete(self, account_id: str) -> None:
        self.repository.delete(account_id)


__all__ = [
    "AccountMarketDataSettings", "MarketCredentialCipher", "MarketCredentialError",
    "MarketDataConfigResponse", "MarketDataConfigUpdate", "MarketProviderTestRequest",
    "MarketProviderTestResponse", "StoredMarketDataConfig",
]
