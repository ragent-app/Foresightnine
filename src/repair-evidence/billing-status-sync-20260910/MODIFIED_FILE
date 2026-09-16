"""Server-authoritative subscription entitlements and token accounting.

The module keeps provider purchase tokens out of logs and API responses.  The
document repositories make the development adapter deterministic; production
uses the same repository boundary backed by PostgreSQL and optimistic versions.
"""
from __future__ import annotations

from calendar import monthrange
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import base64
import binascii
import json
import os
from pathlib import Path
from threading import RLock
from typing import Any, Literal, Protocol
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field


PlanName = Literal["free", "pro", "max"]
EntitlementStatus = Literal["active", "pending", "grace_period", "on_hold", "canceled", "expired"]

FREE_GRANT_TOKENS = 200_000
PRO_MONTHLY_TOKENS = 30_000_000
MAX_MONTHLY_TOKENS = 500_000_000
PRO_PRODUCT_ID = "foresightnine_pro"
MAX_PRODUCT_ID = "foresightnine_max"
MONTHLY_BASE_PLAN_ID = "monthly"
DEFAULT_PROVIDER_MODEL = "glm-5.3-flash"


def configured_provider_model(environ: Mapping[str, str] | None = None) -> str:
    """Return the server-selected model shown on an account entitlement.

    The GLM model remains the safe default for deployments without an
    override.  The staging deployment explicitly selects the host-local Cline
    OpenAI-compatible model, so the account surface must not claim that GLM
    handled a request when the pool handled it instead.
    """
    value = (environ or os.environ).get("FORESIGHTNINE_LLM_MODEL", "").strip()
    return value or DEFAULT_PROVIDER_MODEL


class BillingError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after


class BillingRepository(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...
    def put(self, key: str, value: Any, *, expected_version: int | None = None) -> int: ...
    def delete(self, key: str) -> bool: ...


class PurchaseTokenCipher:
    """AES-GCM envelope for the rollback/audit copy of a purchase token."""

    def __init__(self, key: bytes) -> None:
        if len(key) not in {16, 24, 32}:
            raise ValueError("purchase token encryption key must be 16, 24, or 32 bytes")
        self._key = key

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "PurchaseTokenCipher | None":
        env = environ or os.environ
        raw = env.get("FORESIGHTNINE_PURCHASE_TOKEN_ENCRYPTION_KEY", "").strip()
        if not raw:
            key_file = env.get("FORESIGHTNINE_PURCHASE_TOKEN_ENCRYPTION_KEY_FILE", "").strip()
            if key_file:
                try:
                    raw = Path(key_file).read_text(encoding="ascii").strip()
                except (OSError, UnicodeError):
                    raise BillingError(503, "billing_storage_unavailable", "Billing storage encryption is not configured.", retry_after=30) from None
        if not raw:
            return None
        try:
            key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except (ValueError, binascii.Error) as error:
            raise BillingError(503, "billing_storage_unavailable", "Billing storage encryption is not configured.", retry_after=30) from error
        try:
            return cls(key)
        except ValueError as error:
            raise BillingError(503, "billing_storage_unavailable", "Billing storage encryption is not configured.", retry_after=30) from error

    def encrypt(self, token: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._key).encrypt(nonce, token.encode("utf-8"), None)
        return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


class BillingState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v1"] = "v1"
    account_id: str = Field(min_length=8, max_length=80)
    plan: PlanName
    entitlement_status: EntitlementStatus
    product_id: str | None = None
    base_plan_id: str | None = None
    period_start: datetime
    period_end: datetime | None = None
    quota_limit: int = Field(ge=0)
    consumed_tokens: int = Field(ge=0)
    reserved_tokens: int = Field(ge=0)
    provider_model: str = Field(min_length=1, max_length=128)
    free_grant_claimed: bool = False
    updated_at: datetime


class PurchaseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v1"] = "v1"
    purchase_token_hash: str = Field(min_length=64, max_length=64)
    encrypted_purchase_token: str = Field(min_length=32, max_length=4096)
    account_id: str = Field(min_length=8, max_length=80)
    product_id: str = Field(min_length=1, max_length=40)
    base_plan_id: str = Field(min_length=1, max_length=63)
    subscription_state: str = Field(min_length=1, max_length=64)
    period_start: datetime
    period_end: datetime
    created_at: datetime
    updated_at: datetime


class UsageLedgerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v1"] = "v1"
    reservation_id: str = Field(min_length=8, max_length=160)
    account_id: str = Field(min_length=8, max_length=80)
    provider_model: str = Field(min_length=1, max_length=128)
    estimated_tokens: int = Field(ge=1)
    actual_tokens: int | None = Field(default=None, ge=0)
    status: Literal["reserved", "settled", "released"]
    created_at: datetime
    settled_at: datetime | None = None


class VerifiedGooglePurchase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str
    base_plan_id: str
    subscription_state: Literal["active", "pending", "grace_period", "on_hold", "canceled", "expired"]
    period_start: datetime
    period_end: datetime
    raw_state: str


class GooglePlayVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    purchase_token: str = Field(min_length=20, max_length=4096)
    product_id: str = Field(min_length=1, max_length=40)
    base_plan_id: str = Field(min_length=1, max_length=63)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _add_month(value: datetime) -> datetime:
    value = _utc(value)
    year = value.year + (1 if value.month == 12 else 0)
    month = 1 if value.month == 12 else value.month + 1
    return value.replace(year=year, month=month, day=min(value.day, monthrange(year, month)[1]))


def purchase_token_hash(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


def plan_for_product(product_id: str) -> tuple[PlanName, int] | None:
    return {
        PRO_PRODUCT_ID: ("pro", PRO_MONTHLY_TOKENS),
        MAX_PRODUCT_ID: ("max", MAX_MONTHLY_TOKENS),
    }.get(product_id)


def _subscription_has_quota(verified: VerifiedGooglePurchase, now: datetime) -> bool:
    """Keep access for active/grace subscriptions and unexpired cancellations."""
    return verified.subscription_state in {"active", "grace_period"} or (
        verified.subscription_state == "canceled" and _utc(now) < _utc(verified.period_end)
    )


def _parse_rfc3339(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_google_subscription(payload: Mapping[str, Any], *, expected_product_id: str, expected_base_plan_id: str) -> VerifiedGooglePurchase:
    """Validate only the fields needed to bind a Play purchase to an account."""
    line_items = payload.get("lineItems")
    if not isinstance(line_items, list) or len(line_items) != 1 or not isinstance(line_items[0], dict):
        raise BillingError(502, "billing_provider_invalid", "Google Play returned an invalid subscription response.")
    item = line_items[0]
    product_id = item.get("productId")
    offer_details = item.get("offerDetails")
    base_plan_id = offer_details.get("basePlanId") if isinstance(offer_details, dict) else None
    if product_id != expected_product_id or base_plan_id != expected_base_plan_id:
        raise BillingError(409, "billing_product_mismatch", "The purchase does not match the selected subscription.")
    raw_state = payload.get("subscriptionState")
    state_map = {
        "SUBSCRIPTION_STATE_ACTIVE": "active",
        "SUBSCRIPTION_STATE_IN_GRACE_PERIOD": "grace_period",
        "SUBSCRIPTION_STATE_ON_HOLD": "on_hold",
        "SUBSCRIPTION_STATE_PENDING": "pending",
        "SUBSCRIPTION_STATE_CANCELED": "canceled",
        "SUBSCRIPTION_STATE_EXPIRED": "expired",
    }
    state = state_map.get(raw_state)
    expiry = item.get("expiryTime")
    if not isinstance(raw_state, str) or state is None or not isinstance(expiry, str):
        raise BillingError(502, "billing_provider_invalid", "Google Play returned an invalid subscription state.")
    period_end = _parse_rfc3339(expiry)
    period_start = _utc(datetime.now(timezone.utc))
    return VerifiedGooglePurchase(
        product_id=product_id,
        base_plan_id=base_plan_id,
        subscription_state=state,
        period_start=period_start,
        period_end=period_end,
        raw_state=raw_state,
    )


class GooglePlaySubscriptionVerifier:
    """Small server-side verifier; tokens are never returned or logged."""

    def __init__(self, credentials_path: str, *, request: Any | None = None) -> None:
        self.credentials_path = credentials_path
        self._request = request

    def _access_token(self) -> str:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        credentials = service_account.Credentials.from_service_account_file(
            self.credentials_path,
            scopes=["https://www.googleapis.com/auth/androidpublisher"],
        )
        credentials.refresh(Request())
        if not credentials.token:
            raise BillingError(503, "billing_provider_unavailable", "Google Play verification is unavailable.", retry_after=30)
        return credentials.token

    def fetch(self, purchase_token: str, *, expected_product_id: str, expected_base_plan_id: str) -> VerifiedGooglePurchase:
        import requests
        token = self._access_token()
        url = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/com.foresightnine.mobile/purchases/subscriptionsv2/tokens/" + quote(purchase_token, safe="")
        response = requests.get(url, headers={"Authorization": "Bearer " + token}, timeout=20)
        if response.status_code in {401, 403, 404}:
            raise BillingError(409 if response.status_code == 404 else 502, "billing_purchase_unavailable", "Google Play could not verify this purchase.")
        if response.status_code != 200:
            raise BillingError(503, "billing_provider_unavailable", "Google Play verification is temporarily unavailable.", retry_after=30)
        try:
            payload = response.json()
        except ValueError as error:
            raise BillingError(502, "billing_provider_invalid", "Google Play returned an invalid subscription response.") from error
        if payload.get("packageName") != "com.foresightnine.mobile":
            raise BillingError(409, "billing_package_mismatch", "The purchase belongs to a different application.")
        return parse_google_subscription(payload, expected_product_id=expected_product_id, expected_base_plan_id=expected_base_plan_id)

    def acknowledge(self, purchase_token: str, product_id: str) -> None:
        import requests
        token = self._access_token()
        url = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/com.foresightnine.mobile/purchases/subscriptions/" + quote(product_id, safe="") + ":acknowledge"
        response = requests.post(url, params={"token": purchase_token}, headers={"Authorization": "Bearer " + token}, json={}, timeout=20)
        if response.status_code not in {200, 204}:
            raise BillingError(503, "billing_acknowledge_failed", "Google Play purchase acknowledgement is temporarily unavailable.", retry_after=30)


class BillingLedger:
    def __init__(self, states: BillingRepository, purchases: BillingRepository, usage: BillingRepository, *, cipher: PurchaseTokenCipher | None = None) -> None:
        self.states = states
        self.purchases = purchases
        self.usage = usage
        self.cipher = cipher
        self._lock = RLock()

    def _with_lock(self, key: str, operation: Callable[[], Any]) -> Any:
        locker = getattr(self.states, "with_advisory_lock", None)
        if callable(locker):
            return locker(key, operation)
        return operation()

    def _free_state(self, account_id: str, now: datetime) -> BillingState:
        return BillingState(account_id=account_id, plan="free", entitlement_status="active", period_start=now, quota_limit=FREE_GRANT_TOKENS, consumed_tokens=0, reserved_tokens=0, provider_model=configured_provider_model(), free_grant_claimed=True, updated_at=now)

    def ensure_state(self, account_id: str, *, now: datetime | None = None) -> BillingState:
        current = _utc(now or datetime.now(timezone.utc))
        with self._lock:
            state = self.states.get(account_id)
            if state is None:
                state = self._free_state(account_id, current)
                self.states.put(account_id, state)
            elif not isinstance(state, BillingState):
                state = BillingState.model_validate(state)
            configured_model = configured_provider_model()
            if state.provider_model != configured_model:
                state = state.model_copy(update={"provider_model": configured_model, "updated_at": current})
                self.states.put(account_id, state)
            if (
                state.plan != "free"
                and state.period_end is not None
                and current >= _utc(state.period_end)
                and state.entitlement_status != "expired"
            ):
                state = state.model_copy(
                    update={
                        "entitlement_status": "expired",
                        "quota_limit": 0,
                        "reserved_tokens": 0,
                        "updated_at": current,
                    }
                )
                self.states.put(account_id, state)
            return state

    def snapshot(self, account_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        state = self.ensure_state(account_id, now=now)
        remaining = max(0, state.quota_limit - state.consumed_tokens - state.reserved_tokens)
        return {"plan": state.plan, "entitlement_status": state.entitlement_status, "product_id": state.product_id, "base_plan_id": state.base_plan_id, "quota_limit": state.quota_limit, "quota_consumed": state.consumed_tokens, "quota_reserved": state.reserved_tokens, "quota_remaining": remaining, "quota_resets_at": state.period_end, "provider_model": state.provider_model}

    def reserve(self, account_id: str, reservation_id: str, estimated_tokens: int, *, now: datetime | None = None) -> None:
        if estimated_tokens < 1:
            raise ValueError("estimated_tokens must be positive")
        current = _utc(now or datetime.now(timezone.utc))
        def operation() -> None:
          with self._lock:
            entry = self.usage.get(reservation_id)
            if entry is not None:
                return
            state = self.ensure_state(account_id, now=current)
            remaining = state.quota_limit - state.consumed_tokens - state.reserved_tokens
            if remaining < estimated_tokens:
                raise BillingError(429, "quota_exceeded", "Token quota exceeded.", retry_after=60)
            updated = state.model_copy(update={"reserved_tokens": state.reserved_tokens + estimated_tokens, "updated_at": current})
            self.states.put(account_id, updated)
            self.usage.put(reservation_id, UsageLedgerEntry(reservation_id=reservation_id, account_id=account_id, provider_model=state.provider_model, estimated_tokens=estimated_tokens, status="reserved", created_at=current))
        self._with_lock(f"billing-account:{account_id}", operation)

    def settle(self, reservation_id: str, actual_tokens: int, *, now: datetime | None = None) -> None:
        if actual_tokens < 0:
            raise ValueError("actual_tokens must be non-negative")
        current = _utc(now or datetime.now(timezone.utc))
        entry = self.usage.get(reservation_id)
        if entry is None:
            raise BillingError(409, "usage_reservation_missing", "Usage reservation was not found.")
        if not isinstance(entry, UsageLedgerEntry):
            entry = UsageLedgerEntry.model_validate(entry)
        def operation() -> None:
            with self._lock:
                current_entry = self.usage.get(reservation_id)
                if current_entry is None:
                    raise BillingError(409, "usage_reservation_missing", "Usage reservation was not found.")
                if not isinstance(current_entry, UsageLedgerEntry):
                    current_entry = UsageLedgerEntry.model_validate(current_entry)
                if current_entry.status == "settled":
                    return
                entry = current_entry
                if entry.status == "settled":
                    return
                state = self.ensure_state(entry.account_id, now=current)
                if actual_tokens > state.quota_limit - state.consumed_tokens:
                    raise BillingError(429, "quota_exceeded", "Token quota exceeded.", retry_after=60)
                reserved = max(0, state.reserved_tokens - entry.estimated_tokens)
                updated = state.model_copy(update={"reserved_tokens": reserved, "consumed_tokens": state.consumed_tokens + actual_tokens, "updated_at": current})
                self.states.put(entry.account_id, updated)
                self.usage.put(reservation_id, entry.model_copy(update={"actual_tokens": actual_tokens, "status": "settled", "settled_at": current}))
        self._with_lock(f"billing-account:{entry.account_id}", operation)

    def release(self, reservation_id: str, *, now: datetime | None = None) -> None:
        current = _utc(now or datetime.now(timezone.utc))
        entry = self.usage.get(reservation_id)
        if entry is None:
            return
        if not isinstance(entry, UsageLedgerEntry):
            entry = UsageLedgerEntry.model_validate(entry)
        def operation() -> None:
            with self._lock:
                current_entry = self.usage.get(reservation_id)
                if current_entry is None:
                    return
                if not isinstance(current_entry, UsageLedgerEntry):
                    current_entry = UsageLedgerEntry.model_validate(current_entry)
                entry = current_entry
                if entry.status != "reserved":
                    return
                state = self.ensure_state(entry.account_id, now=current)
                self.states.put(entry.account_id, state.model_copy(update={"reserved_tokens": max(0, state.reserved_tokens - entry.estimated_tokens), "updated_at": current}))
                self.usage.put(reservation_id, entry.model_copy(update={"status": "released", "settled_at": current}))
        self._with_lock(f"billing-account:{entry.account_id}", operation)

    def apply_google_purchase(self, account_id: str, purchase_token: str, verified: VerifiedGooglePurchase, *, now: datetime | None = None) -> dict[str, Any]:
        current = _utc(now or datetime.now(timezone.utc))
        selected = plan_for_product(verified.product_id)
        if selected is None or verified.base_plan_id != MONTHLY_BASE_PLAN_ID:
            raise BillingError(409, "billing_product_mismatch", "The purchase does not match a supported subscription.")
        if self.cipher is None:
            raise BillingError(503, "billing_storage_unavailable", "Billing storage encryption is not configured.", retry_after=30)
        token_hash = purchase_token_hash(purchase_token)
        return self._with_lock(f"billing-token:{token_hash}", lambda: self._apply_google_purchase_locked(account_id, purchase_token, token_hash, verified, current))

    def _apply_google_purchase_locked(self, account_id: str, purchase_token: str, token_hash: str, verified: VerifiedGooglePurchase, current: datetime) -> dict[str, Any]:
        with self._lock:
          return self._apply_google_purchase_account_locked(account_id, purchase_token, token_hash, verified, current)

    def _apply_google_purchase_account_locked(self, account_id: str, purchase_token: str, token_hash: str, verified: VerifiedGooglePurchase, current: datetime) -> dict[str, Any]:
        return self._with_lock(f"billing-account:{account_id}", lambda: self._apply_google_purchase_body(account_id, purchase_token, token_hash, verified, current))

    def _apply_google_purchase_body(self, account_id: str, purchase_token: str, token_hash: str, verified: VerifiedGooglePurchase, current: datetime) -> dict[str, Any]:
        existing = self.purchases.get(token_hash)
        if existing is not None:
            if not isinstance(existing, PurchaseRecord):
                existing = PurchaseRecord.model_validate(existing)
            if existing.account_id != account_id or existing.product_id != verified.product_id or existing.base_plan_id != verified.base_plan_id:
                raise BillingError(409, "billing_purchase_binding_conflict", "The purchase is already bound to another account or product.")
            current_state = self.ensure_state(account_id, now=current)
        else:
            current_state = self.ensure_state(account_id, now=current)
        selected = plan_for_product(verified.product_id)
        if selected is None:
            raise BillingError(409, "billing_product_mismatch", "The purchase does not match a supported subscription.")
        plan, quota = selected
        same_period = (
            current_state.product_id == verified.product_id
            and current_state.base_plan_id == verified.base_plan_id
            and current_state.period_end is not None
            and _utc(current_state.period_end) >= _utc(verified.period_end)
        )
        has_quota = _subscription_has_quota(verified, current)
        if existing is not None and same_period:
            # A restore/status refresh for the same provider period must not
            # reset already-consumed tokens. It may only change entitlement
            # state and reopen/close the quota gate.
            record = existing.model_copy(update={"subscription_state": verified.raw_state, "period_end": verified.period_end, "updated_at": current})
            self.purchases.put(token_hash, record)
            state = current_state.model_copy(
                update={
                    "plan": plan,
                    "entitlement_status": verified.subscription_state,
                    "period_end": max(_utc(current_state.period_end), _utc(verified.period_end)),
                    "quota_limit": quota if has_quota else 0,
                    "updated_at": current,
                }
            )
            self.states.put(account_id, state)
            return self.snapshot(account_id, now=current)
        record = PurchaseRecord(
            purchase_token_hash=token_hash,
            encrypted_purchase_token=self.cipher.encrypt(purchase_token),
            account_id=account_id,
            product_id=verified.product_id,
            base_plan_id=verified.base_plan_id,
            subscription_state=verified.raw_state,
            period_start=verified.period_start,
            period_end=verified.period_end,
            created_at=current,
            updated_at=current,
        )
        self.purchases.put(token_hash, record)
        state = BillingState(account_id=account_id, plan=plan, entitlement_status=verified.subscription_state, product_id=verified.product_id, base_plan_id=verified.base_plan_id, period_start=verified.period_start, period_end=verified.period_end, quota_limit=quota if has_quota else 0, consumed_tokens=0, reserved_tokens=0, provider_model=configured_provider_model(), free_grant_claimed=True, updated_at=current)
        self.states.put(account_id, state)
        return self.snapshot(account_id, now=current)


__all__ = [
    "BillingError", "BillingLedger", "BillingState", "DEFAULT_PROVIDER_MODEL", "FREE_GRANT_TOKENS", "configured_provider_model",
    "GooglePlaySubscriptionVerifier", "MAX_MONTHLY_TOKENS", "MONTHLY_BASE_PLAN_ID", "MAX_PRODUCT_ID",
    "PRO_MONTHLY_TOKENS", "PRO_PRODUCT_ID", "PurchaseRecord", "PurchaseTokenCipher", "UsageLedgerEntry",
    "VerifiedGooglePurchase", "GooglePlayVerifyRequest", "parse_google_subscription", "plan_for_product",
    "purchase_token_hash",
]
