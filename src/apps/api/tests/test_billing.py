from __future__ import annotations

from datetime import datetime, timedelta, timezone
import base64
from hashlib import sha256
from threading import Event, Thread
from time import sleep

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.billing import (
    BillingError,
    BillingLedger,
    BillingState,
    GooglePlaySubscriptionVerifier,
    GooglePlayVerifyRequest,
    PurchaseTokenCipher,
    UsageLedgerEntry,
    parse_google_subscription,
    VerifiedGooglePurchase,
    configured_provider_model,
    configured_research_model,
)
from app.local_auth import LocalAccount, LocalSession
from app.operations import OperationManager
from app.phase3 import DeterministicMockLlmProvider
from app.providers import FixedClock
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


def test_released_reservation_cannot_be_settled_by_late_provider_completion() -> None:
    ledger = BillingLedger(InMemoryDocumentRepository(), InMemoryDocumentRepository(), InMemoryDocumentRepository())
    ledger.reserve("account_test_123", "reservation_cancelled", 4_500)
    ledger.release("reservation_cancelled")
    ledger.settle("reservation_cancelled", 3_000)
    result = ledger.snapshot("account_test_123")
    assert result["quota_consumed"] == 0
    assert result["quota_reserved"] == 0


def test_terminal_compensation_reverses_a_settled_reservation_once() -> None:
    ledger = BillingLedger(InMemoryDocumentRepository(), InMemoryDocumentRepository(), InMemoryDocumentRepository())
    ledger.reserve("account_test_123", "reservation_settled_then_cancelled", 4_500)
    ledger.settle("reservation_settled_then_cancelled", 3_200)
    ledger.release("reservation_settled_then_cancelled", reverse_settled=True)
    ledger.release("reservation_settled_then_cancelled", reverse_settled=True)
    result = ledger.snapshot("account_test_123")
    usage = ledger.usage.get("reservation_settled_then_cancelled")
    assert result["quota_consumed"] == 0
    assert result["quota_reserved"] == 0
    assert usage is not None and usage.status == "released"


def test_expired_operation_lease_releases_reserved_billing_entry() -> None:
    clock = FixedClock()
    ledger = BillingLedger(InMemoryDocumentRepository(), InMemoryDocumentRepository(), InMemoryDocumentRepository())
    ledger.ensure_state("account_test_123", now=clock.now())
    manager = OperationManager(clock, lease_seconds=1)
    manager.set_terminal_observer(
        lambda operation_id, status: ledger.release(
            f"model:{operation_id}", now=clock.now(), reverse_settled=status in {"failed", "cancelled"},
        )
    )
    operation = manager.start(endpoint="/v1/challenges/async", idempotency_key="lease-billing-release")
    ledger.reserve("account_test_123", f"model:{operation.operation_id}", 4_500, now=clock.now())

    assert manager.recover_expired_leases(now=clock.now() + timedelta(seconds=2)) == 1
    snapshot = ledger.snapshot("account_test_123", now=clock.now())
    usage = ledger.usage.get(f"model:{operation.operation_id}")
    assert snapshot["quota_reserved"] == 0
    assert usage is not None and usage.status == "released"


def test_cleanup_reconciles_terminal_reservation_left_by_prior_process() -> None:
    reset_state()
    main_module.operations.set_terminal_observer(None)
    try:
        operation = main_module.operations.start(endpoint="/v1/challenges/async", idempotency_key="prior-process-terminal-reservation")
        billing_ledger.ensure_state("account_test_123", now=main_module.lifecycle_clock.now())
        billing_ledger.reserve("account_test_123", f"model:{operation.operation_id}", 4_500, now=main_module.lifecycle_clock.now())
        cancelled, _ = main_module.operations.cancel(operation.operation_id, operation.cancel_token, "prior-process-cancel")
        assert cancelled.operation.status == "cancelled"
        assert billing_ledger.snapshot("account_test_123")["quota_reserved"] == 4_500

        main_module.cleanup_expired_state()
        usage = billing_ledger.usage.get(f"model:{operation.operation_id}")
        assert billing_ledger.snapshot("account_test_123")["quota_reserved"] == 0
        assert usage is not None and usage.status == "released"
    finally:
        main_module.operations.set_terminal_observer(main_module._release_terminal_reservation)
        reset_state()


def test_entitlement_model_uses_server_override_and_glm_is_fallback(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LLM_PROVIDER", "openai_compatible")
    monkeypatch.delenv("FORESIGHTNINE_LLM_MODEL", raising=False)
    assert configured_provider_model({}) == "unknown"
    monkeypatch.setenv("FORESIGHTNINE_LLM_MODEL", "deepseek/deepseek-v4-flash")
    assert configured_provider_model() == "deepseek/deepseek-v4-flash"


def test_quota_rejects_before_reservation_crosses_limit() -> None:
    ledger = BillingLedger(InMemoryDocumentRepository(), InMemoryDocumentRepository(), InMemoryDocumentRepository())
    with pytest.raises(BillingError) as error:
        ledger.reserve("account_test_123", "reservation_test_123", 200_001)
    assert error.value.status_code == 429
    assert error.value.code == "quota_exceeded"


def test_google_subscription_payload_binds_product_and_base_plan() -> None:
    account_binding = sha256(b"account_google_play_123").hexdigest()
    payload = {
        "packageName": "com.foresightnine.mobile",
        "startTime": "2026-09-01T00:00:00Z",
        "subscriptionState": "SUBSCRIPTION_STATE_ACTIVE",
        "acknowledgementState": "ACKNOWLEDGEMENT_STATE_PENDING",
        "externalAccountIdentifiers": {"obfuscatedExternalAccountId": account_binding},
        "lineItems": [{
            "productId": "foresightnine_pro",
            "expiryTime": "2026-10-01T00:00:00Z",
            "offerDetails": {"basePlanId": "monthly"},
        }],
    }
    verified = parse_google_subscription(
        payload,
        expected_product_id="foresightnine_pro",
        expected_base_plan_id="monthly",
        expected_obfuscated_account_id=account_binding,
    )
    assert verified.subscription_state == "active"
    assert verified.product_id == "foresightnine_pro"
    assert verified.period_start == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert verified.acknowledgement_state == "pending"
    assert verified.obfuscated_account_id == account_binding
    with pytest.raises(BillingError) as error:
        parse_google_subscription(payload, expected_product_id="foresightnine_max", expected_base_plan_id="monthly")
    assert error.value.code == "billing_product_mismatch"
    with pytest.raises(BillingError) as account_error:
        parse_google_subscription(
            payload,
            expected_product_id="foresightnine_pro",
            expected_base_plan_id="monthly",
            expected_obfuscated_account_id=sha256(b"different_account").hexdigest(),
        )
    assert account_error.value.code == "billing_account_mismatch"


def test_google_play_acknowledgement_uses_the_documented_token_path(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        status_code = 204

    def post(url: str, **kwargs: object) -> Response:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return Response()

    verifier = GooglePlaySubscriptionVerifier("unused-for-test")
    monkeypatch.setattr(verifier, "_access_token", lambda: "test-only-access-token")
    monkeypatch.setattr("requests.post", post)
    verifier.acknowledge("purchase/token+value", "foresightnine_pro")

    assert captured["url"] == (
        "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
        "com.foresightnine.mobile/purchases/subscriptions/foresightnine_pro/"
        "tokens/purchase%2Ftoken%2Bvalue:acknowledge"
    )
    assert "params" not in captured["kwargs"]


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
    assert body["provider_model"] == "unknown"
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
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "15"
        assert response.json()["error_code"] == "internal_error"
        summary = client.get("/v1/account", headers={"Authorization": f"Bearer {token}"})
        assert summary.status_code == 200
        assert summary.json()["quota_reserved"] == 0
        assert summary.json()["quota_remaining"] == 200_000
    finally:
        main_module.challenge_provider = previous_provider
        reset_state()


def test_async_provider_failure_releases_account_reservation(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    reset_state()

    class AsyncFailingProvider:
        name = "async-failing-provider"
        supports_native_search = False

        def generate_challenge(self, _request):
            raise main_module.ProviderFailure("provider_timeout", "provider failed", retryable=True)

    previous_provider = main_module.challenge_provider
    main_module.challenge_provider = AsyncFailingProvider()
    try:
        client = TestClient(app)
        registered = client.post(
            "/v1/auth/local/register",
            json={"email": "billing-async-failure@example.test", "password": "correct horse battery staple!"},
        )
        token = registered.json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        session = client.post(
            "/v1/sessions",
            headers={**auth, "Idempotency-Key": "billing-async-failure-session"},
            json={},
        ).json()["session"]
        accepted = client.post(
            "/v1/challenges/async",
            headers={**auth, "Idempotency-Key": "billing-async-failure-challenge"},
            json={
                "session_id": session["session_id"],
                "thesis": "Async provider failure must release its token reservation.",
                "capital": 100000,
                "base_currency": "USD",
                "user_private_evidence_claims": False,
            },
        )
        assert accepted.status_code == 202
        operation_id = accepted.json()["operation"]["operation_id"]
        for _ in range(100):
            if main_module.operations._cache_operation(operation_id).status == "failed":
                break
            sleep(0.01)
        assert main_module.operations._cache_operation(operation_id).status == "failed"
        summary = client.get("/v1/account", headers=auth).json()
        assert summary["quota_reserved"] == 0
        assert summary["quota_consumed"] == 0
        usage = billing_ledger.usage.get(f"model:{operation_id}")
        assert usage is not None and usage.status == "released"
        for _ in range(100):
            if operation_id not in main_module.operations._pending_provider_executions:
                break
            sleep(0.01)
        assert operation_id not in main_module.operations._pending_provider_executions
    finally:
        main_module.challenge_provider = previous_provider
        reset_state()


def test_async_failure_and_cancel_release_account_reservations(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    reset_state()
    provider_started = Event()

    class CancellableFailingProvider:
        name = "failing-provider"
        supports_native_search = False

        def generate_challenge(self, request):
            provider_started.set()
            assert request.cancel_event is not None and request.cancel_event.wait(1)
            raise main_module.ProviderFailure("provider_cancelled", "provider failed", retryable=True)

    previous_provider = main_module.challenge_provider
    main_module.challenge_provider = CancellableFailingProvider()
    try:
        client = TestClient(app)
        registered = client.post(
            "/v1/auth/local/register",
            json={"email": "billing-async-release@example.test", "password": "correct horse battery staple!"},
        )
        token = registered.json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        session = client.post(
            "/v1/sessions",
            headers={**auth, "Idempotency-Key": "billing-async-session"},
            json={},
        ).json()["session"]
        accepted = client.post(
            "/v1/challenges/async",
            headers={**auth, "Idempotency-Key": "billing-async-challenge"},
            json={
                "session_id": session["session_id"],
                "thesis": "Cancellation must release an async token reservation.",
                "capital": 100000,
                "base_currency": "USD",
                "user_private_evidence_claims": False,
            },
        )
        assert accepted.status_code == 202
        operation = accepted.json()["operation"]
        assert provider_started.wait(1)
        before = client.get("/v1/account", headers=auth).json()
        assert before["quota_reserved"] == 4_500
        cancelled = client.post(
            f"/v1/operations/{operation['operation_id']}/cancel",
            headers={**auth, "Idempotency-Key": "billing-async-cancel"},
            json={"cancel_token": operation["cancel_token"]},
        )
        assert cancelled.status_code == 200
        after = client.get("/v1/account", headers=auth).json()
        assert after["quota_reserved"] == 0
        assert after["quota_consumed"] == 0
        usage = billing_ledger.usage.get(f"model:{operation['operation_id']}")
        assert usage is not None and usage.status == "released"
        for _ in range(100):
            if operation["operation_id"] not in main_module.operations._pending_provider_executions:
                break
            sleep(0.01)
        assert operation["operation_id"] not in main_module.operations._pending_provider_executions
    finally:
        main_module.challenge_provider = previous_provider
        reset_state()


def test_cancel_during_terminal_commit_returns_completed_without_mixed_billing_state(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    reset_state()
    previous_provider = main_module.challenge_provider
    main_module.challenge_provider = DeterministicMockLlmProvider()
    settle_entered = Event()
    release_settle = Event()
    original_settle = billing_ledger.settle

    def blocking_settle(*args, **kwargs):
        settle_entered.set()
        assert release_settle.wait(1)
        return original_settle(*args, **kwargs)

    monkeypatch.setattr(billing_ledger, "settle", blocking_settle)
    try:
        client = TestClient(app)
        registered = client.post(
            "/v1/auth/local/register",
            json={"email": "billing-terminal-race@example.test", "password": "correct horse battery staple!"},
        )
        token = registered.json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        session = client.post(
            "/v1/sessions", headers={**auth, "Idempotency-Key": "billing-terminal-race-session"}, json={},
        ).json()["session"]
        accepted = client.post(
            "/v1/challenges/async",
            headers={**auth, "Idempotency-Key": "billing-terminal-race-challenge"},
            json={"session_id": session["session_id"], "thesis": "Commit and cancel must have one terminal winner.", "capital": 100000, "base_currency": "USD", "user_private_evidence_claims": False},
        )
        operation = accepted.json()["operation"]
        assert accepted.status_code == 202 and settle_entered.wait(1)
        cancellation_result: list[object] = []
        cancellation = Thread(target=lambda: cancellation_result.append(client.post(
            f"/v1/operations/{operation['operation_id']}/cancel",
            headers={**auth, "Idempotency-Key": "billing-terminal-race-cancel"},
            json={"cancel_token": operation["cancel_token"]},
        )))
        cancellation.start()
        sleep(0.05)
        assert cancellation.is_alive()
        release_settle.set()
        cancellation.join(1)
        assert not cancellation.is_alive()
        cancelled = cancellation_result[0]
        assert getattr(cancelled, "status_code") == 200
        assert cancelled.json()["operation"]["status"] == "completed"
        summary = client.get("/v1/account", headers=auth).json()
        assert summary["quota_reserved"] == 0
        assert summary["quota_consumed"] > 0
        assert len(main_module.CHALLENGES) == 1
        assert main_module.operations._cache_operation(operation["operation_id"]).status == "completed"
    finally:
        release_settle.set()
        main_module.challenge_provider = previous_provider
        reset_state()


def test_post_commit_operation_store_failure_compensates_business_and_billing(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    reset_state()
    previous_provider = main_module.challenge_provider
    main_module.challenge_provider = DeterministicMockLlmProvider()
    original_finish = main_module.operations.store.finish
    completion_attempted = Event()

    def fail_first_finish(*args, **kwargs):
        if not completion_attempted.is_set():
            completion_attempted.set()
            admission = kwargs.get("admission")
            if admission is not None:
                admission()
            raise RuntimeError("injected completion persistence failure")
        return original_finish(*args, **kwargs)

    try:
        client = TestClient(app)
        registered = client.post(
            "/v1/auth/local/register",
            json={"email": "billing-post-commit-failure@example.test", "password": "correct horse battery staple!"},
        )
        token = registered.json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        session = client.post(
            "/v1/sessions", headers={**auth, "Idempotency-Key": "billing-post-commit-session"}, json={},
        ).json()["session"]
        monkeypatch.setattr(main_module.operations.store, "finish", fail_first_finish)
        accepted = client.post(
            "/v1/challenges/async",
            headers={**auth, "Idempotency-Key": "billing-post-commit-challenge"},
            json={"session_id": session["session_id"], "thesis": "A failed terminal write must compensate all staged state.", "capital": 100000, "base_currency": "USD", "user_private_evidence_claims": False},
        )
        operation_id = accepted.json()["operation"]["operation_id"]
        for _ in range(100):
            if main_module.operations._cache_operation(operation_id).status == "failed":
                break
            sleep(0.01)
        assert completion_attempted.is_set()
        assert main_module.operations._cache_operation(operation_id).status == "failed"
        summary = client.get("/v1/account", headers=auth).json()
        assert summary["quota_reserved"] == 0
        assert summary["quota_consumed"] == 0
        assert len(main_module.CHALLENGES) == 0
        assert len(main_module.THESES) == 0
        usage = billing_ledger.usage.get(f"model:{operation_id}")
        assert usage is not None and usage.status == "released"
    finally:
        main_module.challenge_provider = previous_provider
        reset_state()


def test_google_play_verify_route_grants_only_verified_active_purchase(monkeypatch) -> None:
    current = datetime.now(timezone.utc)
    class PurchaseTestClock(FixedClock):
        def now(self):
            return current

    # Keep lifecycle time aligned with the repository's real-time expiry clock.
    monkeypatch.setattr(main_module, "lifecycle_clock", PurchaseTestClock())
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    reset_state()
    encryption_key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
    cipher = PurchaseTokenCipher.from_environment({"FORESIGHTNINE_PURCHASE_TOKEN_ENCRYPTION_KEY": encryption_key})
    assert cipher is not None
    ledger = BillingLedger(InMemoryDocumentRepository(), InMemoryDocumentRepository(), InMemoryDocumentRepository(), cipher=cipher)
    active = VerifiedGooglePurchase(
        product_id="foresightnine_pro",
        base_plan_id="monthly",
        subscription_state="active",
        period_start=current - timedelta(days=14),
        period_end=current + timedelta(days=16),
        raw_state="SUBSCRIPTION_STATE_ACTIVE",
        acknowledgement_state="pending",
    )
    pending = active.model_copy(update={
        "product_id": "foresightnine_max",
        "subscription_state": "pending",
        "raw_state": "SUBSCRIPTION_STATE_PENDING",
    })

    class FakeVerifier:
        verified = active
        acknowledgements: list[tuple[str, str]] = []

        def __init__(self, _credentials_path: str) -> None:
            pass

        def fetch(self, _purchase_token: str, *, expected_product_id: str, expected_base_plan_id: str, expected_obfuscated_account_id: str):
            assert self.verified.product_id == expected_product_id
            assert self.verified.base_plan_id == expected_base_plan_id
            assert len(expected_obfuscated_account_id) == 64
            return self.verified

        def acknowledge(self, purchase_token: str, product_id: str) -> None:
            states = list(ledger.states.values())
            assert states and states[0].plan == "pro"
            self.acknowledgements.append((purchase_token, product_id))

    previous_ledger = main_module.billing_ledger
    previous_verifier = main_module.GooglePlaySubscriptionVerifier
    main_module.billing_ledger = ledger
    main_module.GooglePlaySubscriptionVerifier = FakeVerifier
    try:
        client = TestClient(app)
        registered = client.post(
            "/v1/auth/local/register",
            json={"email": "billing-route@example.test", "password": "correct horse battery staple!"},
        )
        assert registered.status_code == 201
        token = registered.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        active_response = client.post(
            "/v1/billing/google-play/verify",
            headers=headers,
            json={"purchase_token": "active-purchase-token-1234567890", "product_id": "foresightnine_pro", "base_plan_id": "monthly"},
        )
        assert active_response.status_code == 200
        assert active_response.json()["plan"] == "pro"
        assert active_response.json()["quota_limit"] == 30_000_000
        assert FakeVerifier.acknowledgements == [("active-purchase-token-1234567890", "foresightnine_pro")]

        FakeVerifier.verified = active.model_copy(update={"acknowledgement_state": "acknowledged"})
        acknowledged_response = client.post(
            "/v1/billing/google-play/restore",
            headers=headers,
            json={"purchase_token": "active-purchase-token-1234567890", "product_id": "foresightnine_pro", "base_plan_id": "monthly"},
        )
        assert acknowledged_response.status_code == 200
        assert FakeVerifier.acknowledgements == [("active-purchase-token-1234567890", "foresightnine_pro")]

        FakeVerifier.verified = pending
        pending_response = client.post(
            "/v1/billing/google-play/verify",
            headers=headers,
            json={"purchase_token": "pending-purchase-token-1234567890", "product_id": "foresightnine_max", "base_plan_id": "monthly"},
        )
        assert pending_response.status_code == 200
        assert pending_response.json()["purchase_state"] == "pending"
        assert pending_response.json()["entitlement_status"] == "pending"
        assert pending_response.json()["quota_limit"] == 0
        assert FakeVerifier.acknowledgements == [("active-purchase-token-1234567890", "foresightnine_pro")]
    finally:
        main_module.billing_ledger = previous_ledger
        main_module.GooglePlaySubscriptionVerifier = previous_verifier
        reset_state()


def test_account_model_uses_explicit_deployment_model() -> None:
    assert configured_provider_model({
        "FORESIGHTNINE_LLM_PROVIDER": "openai_chat_completions",
        "FORESIGHTNINE_LLM_MODEL": "deployment/model",
    }) == "deployment/model"


def test_explicit_empty_provider_environment_does_not_inherit_process_model(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LLM_MODEL", "deployment/model")
    assert configured_provider_model({}) == "unknown"


def test_reservation_is_labelled_with_the_model_actually_called(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LLM_MODEL", "general-model")
    ledger = BillingLedger(InMemoryDocumentRepository(), InMemoryDocumentRepository(), InMemoryDocumentRepository())
    ledger.reserve("account_test_123", "reservation_research", 1_000, provider_model="research-model")
    ledger.reserve("account_test_123", "reservation_general", 1_000)
    assert ledger.usage.get("reservation_research").provider_model == "research-model"
    assert ledger.usage.get("reservation_general").provider_model == "general-model"


def test_configured_research_model_follows_the_research_protocol() -> None:
    assert configured_research_model({"FORESIGHTNINE_LLM_MODEL": "general", "FORESIGHTNINE_RESEARCH_MODEL_PROTOCOL": "anthropic_messages",
                                      "FORESIGHTNINE_RESEARCH_LLM_MODEL": "research"}) == "research"
    assert configured_research_model({"FORESIGHTNINE_LLM_MODEL": "general"}) == "general"
    assert configured_research_model({"FORESIGHTNINE_RESEARCH_MODEL_PROTOCOL": "anthropic_messages"}) == "unknown"
