from __future__ import annotations

from datetime import datetime, timezone
import base64

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.billing import (
    BillingError,
    BillingLedger,
    BillingState,
    GooglePlayVerifyRequest,
    PurchaseTokenCipher,
    UsageLedgerEntry,
    parse_google_subscription,
    VerifiedGooglePurchase,
    configured_provider_model,
)
from app.local_auth import LocalAccount, LocalSession
from app.storage import InMemoryDocumentRepository
from app.main import ACCOUNTS, AUTH_SESSIONS, app, billing_ledger, reset_state


def test_free_account_snapshot_and_atomic_reservation() -> None:
    ledger = BillingLedger(
        InMemoryDocumentRepository(),
        InMemoryDocumentRepository(),
        InMemoryDocumentRepository(),
    )
    snapshot = ledger.snapshot("account_test_123", now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert snapshot["plan"] == "free"
    assert snapshot["quota_limit"] == 200_000
    ledger.reserve("account_test_123", "reservation_test_123", 100_000, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    ledger.reserve("account_test_123", "reservation_test_123", 100_000, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    ledger.settle("reservation_test_123", 80_000, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    ledger.settle("reservation_test_123", 80_000, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    result = ledger.snapshot("account_test_123", now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert result["quota_consumed"] == 80_000
    assert result["quota_reserved"] == 0
    assert result["quota_remaining"] == 120_000


def test_zero_provider_usage_settles_to_zero_instead_of_conservative_estimate() -> None:
    ledger = BillingLedger(
        InMemoryDocumentRepository(),
        InMemoryDocumentRepository(),
        InMemoryDocumentRepository(),
    )
    ledger.reserve("account_test_123", "reservation_zero_usage", 4_500)
    ledger.settle("reservation_zero_usage", 0)
    result = ledger.snapshot("account_test_123")
    assert result["quota_consumed"] == 0
    assert result["quota_reserved"] == 0


def test_entitlement_model_uses_server_override_and_glm_is_fallback(monkeypatch) -> None:
    monkeypatch.delenv("FORESIGHTNINE_LLM_MODEL", raising=False)
    assert configured_provider_model({}) == "glm-5.3-flash"
    monkeypatch.setenv("FORESIGHTNINE_LLM_MODEL", "deepseek/deepseek-v4-flash")
    assert configured_provider_model() == "deepseek/deepseek-v4-flash"


def test_quota_rejects_before_reservation_crosses_limit() -> None:
    ledger = BillingLedger(InMemoryDocumentRepository(), InMemoryDocumentRepository(), InMemoryDocumentRepository())
    with pytest.raises(BillingError) as error:
        ledger.reserve("account_test_123", "reservation_test_123", 200_001)
    assert error.value.status_code == 429
    assert error.value.code == "quota_exceeded"


def test_google_subscription_payload_binds_product_and_base_plan() -> None:
    payload = {
        "packageName": "com.foresightnine.mobile",
        "subscriptionState": "SUBSCRIPTION_STATE_ACTIVE",
        "lineItems": [{
            "productId": "foresightnine_pro",
            "expiryTime": "2026-10-01T00:00:00Z",
            "offerDetails": {"basePlanId": "monthly"},
        }],
    }
    verified = parse_google_subscription(payload, expected_product_id="foresightnine_pro", expected_base_plan_id="monthly")
    assert verified.subscription_state == "active"
    assert verified.product_id == "foresightnine_pro"
    with pytest.raises(BillingError) as error:
        parse_google_subscription(payload, expected_product_id="foresightnine_max", expected_base_plan_id="monthly")
    assert error.value.code == "billing_product_mismatch"


def test_account_endpoint_exposes_server_authoritative_free_grant(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    reset_state()
    client = TestClient(app)
    registered = client.post("/v1/auth/local/register", json={"email": "billing@example.test", "password": "correct horse battery staple!"})
    assert registered.status_code == 201
    token = registered.json()["access_token"]
    response = client.get("/v1/account", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["plan"] == "free"
    assert body["quota_limit"] == 200_000
    assert body["provider_model"] == "glm-5.3-flash"
    reset_state()


def test_purchase_cipher_does_not_return_plaintext() -> None:
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
    cipher = PurchaseTokenCipher.from_environment({"FORESIGHTNINE_PURCHASE_TOKEN_ENCRYPTION_KEY": key})
    assert cipher is not None
    encrypted = cipher.encrypt("purchase-token-test-value")
    assert "purchase-token-test-value" not in encrypted


def test_google_purchase_grant_is_idempotent_and_preserves_usage() -> None:
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
    cipher = PurchaseTokenCipher.from_environment({"FORESIGHTNINE_PURCHASE_TOKEN_ENCRYPTION_KEY": key})
    assert cipher is not None
    ledger = BillingLedger(InMemoryDocumentRepository(), InMemoryDocumentRepository(), InMemoryDocumentRepository(), cipher=cipher)
    verified = VerifiedGooglePurchase(
        product_id="foresightnine_pro",
        base_plan_id="monthly",
        subscription_state="active",
        period_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        period_end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        raw_state="SUBSCRIPTION_STATE_ACTIVE",
    )
    first = ledger.apply_google_purchase("account_test_123", "purchase-token-test-value", verified, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert first["plan"] == "pro"
    ledger.reserve("account_test_123", "reservation_test_123", 4_500, now=datetime(2026, 1, 2, tzinfo=timezone.utc))
    ledger.settle("reservation_test_123", 3_000, now=datetime(2026, 1, 2, tzinfo=timezone.utc))
    second = ledger.apply_google_purchase("account_test_123", "purchase-token-test-value", verified, now=datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert second["plan"] == "pro"
    assert second["quota_consumed"] == 3_000


def test_expired_google_period_fails_closed_without_regranting_free_quota() -> None:
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
    cipher = PurchaseTokenCipher.from_environment({"FORESIGHTNINE_PURCHASE_TOKEN_ENCRYPTION_KEY": key})
    assert cipher is not None
    ledger = BillingLedger(InMemoryDocumentRepository(), InMemoryDocumentRepository(), InMemoryDocumentRepository(), cipher=cipher)
    verified = VerifiedGooglePurchase(
        product_id="foresightnine_pro",
        base_plan_id="monthly",
        subscription_state="active",
        period_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        period_end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        raw_state="SUBSCRIPTION_STATE_ACTIVE",
    )
    ledger.apply_google_purchase("account_test_123", "purchase-token-expiry-test", verified, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    expired = ledger.snapshot("account_test_123", now=datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert expired["entitlement_status"] == "expired"
    assert expired["quota_limit"] == 0
    assert expired["quota_remaining"] == 0


def test_subscription_status_refresh_preserves_consumption_and_reopens_same_period() -> None:
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
    cipher = PurchaseTokenCipher.from_environment({"FORESIGHTNINE_PURCHASE_TOKEN_ENCRYPTION_KEY": key})
    assert cipher is not None
    ledger = BillingLedger(InMemoryDocumentRepository(), InMemoryDocumentRepository(), InMemoryDocumentRepository(), cipher=cipher)
    active = VerifiedGooglePurchase(product_id="foresightnine_pro", base_plan_id="monthly", subscription_state="active", period_start=datetime(2026, 1, 1, tzinfo=timezone.utc), period_end=datetime(2026, 2, 1, tzinfo=timezone.utc), raw_state="SUBSCRIPTION_STATE_ACTIVE")
    on_hold = active.model_copy(update={"subscription_state": "on_hold", "raw_state": "SUBSCRIPTION_STATE_ON_HOLD"})
    restored = active.model_copy(update={"period_start": datetime(2026, 1, 2, tzinfo=timezone.utc)})
    ledger.apply_google_purchase("account_test_123", "purchase-token-status-test", active, now=datetime(2026, 1, 2, tzinfo=timezone.utc))
    ledger.reserve("account_test_123", "reservation_status_test", 4_500, now=datetime(2026, 1, 3, tzinfo=timezone.utc))
    ledger.settle("reservation_status_test", 3_000, now=datetime(2026, 1, 3, tzinfo=timezone.utc))
    held = ledger.apply_google_purchase("account_test_123", "purchase-token-status-test", on_hold, now=datetime(2026, 1, 4, tzinfo=timezone.utc))
    assert held["entitlement_status"] == "on_hold"
    assert held["quota_limit"] == 0
    restored_snapshot = ledger.apply_google_purchase("account_test_123", "purchase-token-status-test", restored, now=datetime(2026, 1, 5, tzinfo=timezone.utc))
    assert restored_snapshot["entitlement_status"] == "active"
    assert restored_snapshot["quota_consumed"] == 3_000
    assert restored_snapshot["quota_limit"] == 30_000_000


def test_failed_provider_call_releases_account_reservation(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    reset_state()

    class FailingProvider:
        name = "failing-provider"
        supports_native_search = False

        def generate_challenge(self, _request):
            raise main_module.ProviderFailure("provider_timeout", "provider failed", retryable=True)

    previous_provider = main_module.challenge_provider
    main_module.challenge_provider = FailingProvider()
    try:
        client = TestClient(app)
        registered = client.post(
            "/v1/auth/local/register",
            json={"email": "billing-release@example.test", "password": "correct horse battery staple!"},
        )
        assert registered.status_code == 201
        token = registered.json()["access_token"]
        session = client.post(
            "/v1/sessions",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "billing-release-session"},
            json={"client_locale": "en-US"},
        )
        assert session.status_code == 201
        response = client.post(
            "/v1/challenges",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "billing-release-challenge"},
            json={
                "session_id": session.json()["session"]["session_id"],
                "thesis": "A failed provider call must release its token reservation.",
                "capital": 100000,
                "base_currency": "USD",
                "user_private_evidence_claims": False,
            },
        )
        assert response.status_code == 500
        summary = client.get("/v1/account", headers={"Authorization": f"Bearer {token}"})
        assert summary.status_code == 200
        assert summary.json()["quota_reserved"] == 0
        assert summary.json()["quota_remaining"] == 200_000
    finally:
        main_module.challenge_provider = previous_provider
        reset_state()
