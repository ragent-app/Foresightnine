"""Typed persistence record for an external OAuth identity.

The provider access token is deliberately never part of this record.  Only the
provider subject needed for account lookup and the verified display email are
retained, and the account repository remains the source of account lifecycle
truth.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


OAuthIdentityProvider = Literal["google", "github"]


class OAuthIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["v1"] = "v1"
    identity_id: str = Field(min_length=8, max_length=160)
    provider: OAuthIdentityProvider
    subject: str = Field(min_length=1, max_length=255)
    account_id: str = Field(min_length=8, max_length=80)
    email: str = Field(min_length=3, max_length=254)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _hydrate_serialized_datetime(cls, value: object) -> object:
        # PostgreSQL JSONB returns persisted ISO datetimes as strings. Convert
        # only that storage representation before strict model validation.
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value


class OAuthSessionResponse(BaseModel):
    """Strict exchange response; the provider access token is never included."""

    model_config = ConfigDict(extra="forbid", strict=True)

    account: dict[str, str]
    session_id: str = Field(min_length=8, max_length=80)
    access_token: str = Field(min_length=32, max_length=256)
    expires_at: str = Field(min_length=20, max_length=80)
    auth_mode: Literal["oauth"]
    provider: OAuthIdentityProvider


__all__ = ["OAuthIdentity", "OAuthIdentityProvider", "OAuthSessionResponse"]
