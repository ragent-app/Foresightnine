from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import create_model

from app.operation_store import InMemoryOperationStore, OperationStoreUnavailable, PostgresOperationStore
from app.operations import ApiProblem, OperationManager
from app.providers import FixedClock
from app.services import content_hash


class RecordingCursor:
    def __init__(self, connection: "RecordingConnection") -> None:
        self.connection = connection
        self.rowcount = 0

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
        self.connection.calls.append((statement, parameters))


class RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __enter__(self) -> "RecordingConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self)


class BeginCursor(RecordingCursor):
    def fetchone(self) -> tuple[str]:
        return ("owned",)


class BeginConnection(RecordingConnection):
    def cursor(self) -> BeginCursor:
        return BeginCursor(self)


def _put_control(store: InMemoryOperationStore, key_hash: str, now: datetime) -> None:
    store.put_control(
        endpoint="/same-endpoint",
        idempotency_key_hash=key_hash,
        request_hash=f"request-{key_hash}",
        response={"operation": {"operation_id": key_hash}},
        response_type="OperationResponse",
        now=now,
    )


def test_postgres_begin_propagates_typed_admission_error_unchanged() -> None:
    connection = BeginConnection()
    store = PostgresOperationStore(
        "postgresql://db.example.test/app",
        connection_factory=lambda _dsn: connection,
    )
    admission_error = ApiProblem(429, "quota_exceeded", "Model-call quota exceeded; retry later.", retryable=True, retry_after=17)

    def admission() -> None:
        raise admission_error

    with pytest.raises(ApiProblem) as raised:
        store.begin(
            endpoint="/v1/challenges",
            idempotency_key_hash="a" * 64,
            request_hash="b" * 64,
            operation_id="operation:one",
            operation={"status": "running"},
            events=[{"event": "operation.started"}],
            mode="async",
            response=None,
            response_type="OperationResponse",
            session_id=None,
            lease_owner="manager:one",
            lease_expires_at=datetime(2026, 8, 29, 13, tzinfo=timezone.utc),
            admission=admission,
        )

    assert raised.value is admission_error
    assert len(connection.calls) == 1


def test_postgres_connection_errors_remain_operation_store_unavailable() -> None:
    def failing_connection(_dsn: str) -> Any:
        raise OSError("database unavailable")

    store = PostgresOperationStore(
        "postgresql://db.example.test/app",
        connection_factory=failing_connection,
    )

    with pytest.raises(OperationStoreUnavailable):
        store.check_ready()


def test_in_memory_prune_retains_newer_same_endpoint_claim_when_one_excess_is_allowed() -> None:
    store = InMemoryOperationStore()
    now = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)
    older_hash = "a" * 64
    newer_hash = "b" * 64
    _put_control(store, older_hash, now - timedelta(minutes=1))
    _put_control(store, newer_hash, now)

    result = store.prune(
        now=now,
        max_terminal_operations=10,
        max_idempotency_entries=1,
        max_terminal_sse_events=10,
        terminal_ttl_seconds=None,
    )

    assert result["removed_idempotency_entries"] == 1
    assert store.get_claim("/same-endpoint", older_hash, f"request-{older_hash}") is None
    assert store.get_claim("/same-endpoint", newer_hash, f"request-{newer_hash}") is not None


def test_terminal_operation_ttl_uses_operation_updated_at_for_claim_pruning() -> None:
    manager = OperationManager(FixedClock())
    payload_type = create_model("RetentionPayload", content_hash=(str, ...))
    response_type = create_model("RetentionResponse", operation=(object, ...), value=(payload_type, ...))
    request_payload = {"message": "retention"}
    idempotency_key = "retention-operation-key"

    response, replayed = manager.execute(
        endpoint="/v1/challenges",
        idempotency_key=idempotency_key,
        request_payload=request_payload,
        response_model=response_type,
        result_field="value",
        build_result=lambda operation_id: (payload_type(content_hash="d" * 64), "result:retention"),
    )
    operation = response.operation
    claim = manager.store.get_claim(
        "/v1/challenges",
        manager._key_hash(idempotency_key),
        content_hash(request_payload),
    )

    assert replayed is False
    assert claim is not None
    assert claim.updated_at == operation.updated_at

    report = manager.prune(
        now=operation.updated_at + timedelta(seconds=1),
        max_terminal_operations=10,
        max_idempotency_entries=10,
        max_terminal_sse_events=10,
        terminal_ttl_seconds=1,
    )

    assert report["removed_terminal_operations"] == 1
    assert report["removed_idempotency_entries"] == 1
    assert manager.store.get_operation(operation.operation_id) is None
    assert manager.store.get_claim("/v1/challenges", manager._key_hash(idempotency_key), content_hash(request_payload)) is None


def test_in_memory_prune_excludes_active_async_receipt_from_terminal_overflow() -> None:
    store = InMemoryOperationStore()
    now = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)
    active_operation = {
        "operation_id": "operation:active",
        "status": "running",
        "updated_at": now.isoformat(),
    }
    active_key_hash = "d" * 64
    active_request_hash = "e" * 64
    store.begin(
        endpoint="/same-endpoint",
        idempotency_key_hash=active_key_hash,
        request_hash=active_request_hash,
        operation_id=active_operation["operation_id"],
        operation=active_operation,
        events=[{"event": "operation.started", "sequence": 1}],
        mode="async",
        response={"operation": active_operation},
        response_type="OperationResponse",
        session_id=None,
        lease_owner="manager:active",
        lease_expires_at=now + timedelta(minutes=5),
    )
    older_hash = "a" * 64
    newer_hash = "b" * 64
    _put_control(store, older_hash, now - timedelta(minutes=1))
    _put_control(store, newer_hash, now)

    result = store.prune(
        now=now + timedelta(days=30),
        max_terminal_operations=10,
        max_idempotency_entries=1,
        max_terminal_sse_events=10,
        terminal_ttl_seconds=None,
    )

    assert result["removed_idempotency_entries"] == 1
    assert store.get_claim("/same-endpoint", active_key_hash, active_request_hash) is not None
    assert store.get_claim("/same-endpoint", older_hash, f"request-{older_hash}") is None
    assert store.get_claim("/same-endpoint", newer_hash, f"request-{newer_hash}") is not None


def test_in_memory_expired_lease_recovers_to_retryable_terminal_failure_without_resubmission() -> None:
    store = InMemoryOperationStore()
    manager = OperationManager(FixedClock(), store=store)
    endpoint = "/v1/challenges"
    idempotency_key = "expired-async-key"
    request_payload = {"message": "expired"}
    operation = manager._operation(
        operation_id="operation:expired",
        endpoint=endpoint,
        idempotency_key_hash=manager._key_hash(idempotency_key),
        status="running",
        last_sequence=3,
    )
    operation_document = operation.model_dump(mode="json", exclude_none=True)
    event_documents = [event.model_dump(mode="json", exclude_none=True) for event in manager._initial(operation.operation_id)]
    request_hash = content_hash(request_payload)
    lease_expires_at = operation.updated_at - timedelta(seconds=1)
    store.begin(
        endpoint=operation.endpoint,
        idempotency_key_hash=operation.idempotency_key_hash,
        request_hash=request_hash,
        operation_id=operation.operation_id,
        operation=operation_document,
        events=event_documents,
        mode="async",
        response={"operation": operation_document},
        response_type="OperationResponse",
        session_id=None,
        lease_owner="manager:expired",
        lease_expires_at=lease_expires_at,
    )
    assert store.get_operation(operation.operation_id)["lease_expires_at"] == lease_expires_at.isoformat()
    manager._cache_operation(operation.operation_id, operation_document, event_documents)

    assert manager.recover_expired_leases(now=operation.updated_at) == 1
    recovered = store.get_operation(operation.operation_id)
    assert recovered is not None
    assert recovered["status"] == "failed"
    assert "lease_expires_at" not in recovered
    assert store.get_events(operation.operation_id)[-1]["error"]["retryable"] is True
    claim = store.get_claim(operation.endpoint, operation.idempotency_key_hash, request_hash)
    assert claim is not None and claim.response is not None
    assert claim.response["operation"]["status"] == "failed"
    assert claim.updated_at == operation.updated_at

    replayed_response, replayed = manager.submit_async(
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_payload=request_payload,
        result_field="challenge",
        build_result=lambda operation_id: (_ for _ in ()).throw(AssertionError("recovered operation must not resubmit provider work")),
    )
    assert replayed is True and replayed_response.operation.status == "failed"
    assert store.get_operation(operation.operation_id)["status"] == "failed"


def test_in_memory_start_retains_lease_expiry_in_stored_operation_document() -> None:
    store = InMemoryOperationStore()
    manager = OperationManager(FixedClock(), store=store, lease_seconds=300)
    lease_expires_at = manager.clock.now() + timedelta(minutes=5)
    manager.start(endpoint="/v1/challenges", idempotency_key="start-lease-key")
    started_operation_id = manager._operation_id("/v1/challenges", manager._key_hash("start-lease-key"))
    stored = store.get_operation(started_operation_id)

    assert stored is not None
    assert stored["lease_expires_at"] == lease_expires_at.isoformat()


def test_postgres_prune_overflow_deletes_exact_composite_claim_keys() -> None:
    connection = RecordingConnection()
    store = PostgresOperationStore(
        "postgresql://db.example.test/app",
        connection_factory=lambda _dsn: connection,
    )

    store.prune(
        now=datetime(2026, 8, 29, 12, tzinfo=timezone.utc),
        max_terminal_operations=10,
        max_idempotency_entries=1,
        max_terminal_sse_events=10,
        terminal_ttl_seconds=None,
    )

    idempotency_statements = [statement for statement, _parameters in connection.calls if "operation_idempotency" in statement]
    assert len(idempotency_statements) == 1
    statement = idempotency_statements[0]
    assert "(endpoint, idempotency_key_hash) IN" in statement
    assert "ORDER BY updated_at ASC, endpoint ASC, idempotency_key_hash ASC" in statement
    assert "endpoint IN" not in statement


def test_postgres_prune_none_ttl_has_no_time_based_idempotency_delete() -> None:
    connection = RecordingConnection()
    store = PostgresOperationStore(
        "postgresql://db.example.test/app",
        connection_factory=lambda _dsn: connection,
    )

    store.prune(
        now=datetime(2026, 8, 29, 12, tzinfo=timezone.utc),
        max_terminal_operations=10,
        max_idempotency_entries=10,
        max_terminal_sse_events=10,
        terminal_ttl_seconds=None,
    )

    idempotency_statements = [statement.lower() for statement, _parameters in connection.calls if "operation_idempotency" in statement]
    assert len(idempotency_statements) == 1
    assert "created_at <=" not in idempotency_statements[0]
    assert "updated_at <=" not in idempotency_statements[0]

    memory_store = InMemoryOperationStore()
    recent_hash = "c" * 64
    now = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)
    _put_control(memory_store, recent_hash, now)
    memory_store.prune(
        now=now + timedelta(days=30),
        max_terminal_operations=10,
        max_idempotency_entries=10,
        max_terminal_sse_events=10,
        terminal_ttl_seconds=None,
    )
    assert memory_store.get_claim("/same-endpoint", recent_hash, f"request-{recent_hash}") is not None
