"""Durable operation/idempotency state with an explicit development adapter.

The operation store is deliberately separate from the entity document store.  An
operation claim is a hash-only record; neither the raw idempotency key nor the raw
request document crosses this boundary.  PostgreSQL transitions lock the
idempotency or operation row in the same transaction that appends its event.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from threading import Condition, RLock
from time import monotonic, sleep
from typing import Any, Protocol

from pydantic import BaseModel

from .storage import StorageConfig, StorageConfigurationError, StorageMode, StorageRuntimeError, current_postgres_unit_of_work, postgres_unit_of_work


OPERATION_STATE_TABLE = "operation_state"
OPERATION_IDEMPOTENCY_TABLE = "operation_idempotency"
OPERATION_EVENTS_TABLE = "operation_events"
OPERATION_SESSIONS_TABLE = "operation_session_associations"


class OperationStoreConflict(StorageRuntimeError):
    """The same endpoint/key hash was submitted with a different body hash."""


class OperationStoreNotFound(StorageRuntimeError):
    """A requested operation or claim does not exist."""


class OperationStoreUnavailable(StorageRuntimeError):
    """The selected durable store cannot currently serve the request."""


class OperationStorePassThrough(RuntimeError):
    """Typed application errors that must cross the store boundary unchanged."""


@dataclass(frozen=True, slots=True)
class StoredClaim:
    endpoint: str
    idempotency_key_hash: str
    request_hash: str
    operation_id: str | None
    mode: str
    response: dict[str, Any] | None
    response_type: str | None
    error: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime

    @property
    def terminal(self) -> bool:
        return self.response is not None or self.error is not None


@dataclass(frozen=True, slots=True)
class BeginResult:
    owner: bool
    claim: StoredClaim


class OperationStore(Protocol):
    def begin(
        self,
        *,
        endpoint: str,
        idempotency_key_hash: str,
        request_hash: str,
        operation_id: str,
        operation: dict[str, Any],
        events: list[dict[str, Any]],
        mode: str,
        response: dict[str, Any] | None,
        response_type: str | None,
        session_id: str | None,
        lease_owner: str,
        lease_expires_at: datetime,
        admission: Callable[[], None] | None = None,
    ) -> BeginResult: ...

    def get_claim(self, endpoint: str, idempotency_key_hash: str, request_hash: str) -> StoredClaim | None: ...

    def wait_for_terminal(self, endpoint: str, idempotency_key_hash: str, request_hash: str) -> StoredClaim: ...

    def put_control(
        self,
        *,
        endpoint: str,
        idempotency_key_hash: str,
        request_hash: str,
        response: dict[str, Any],
        response_type: str,
        now: datetime,
    ) -> StoredClaim: ...

    def get_operation(self, operation_id: str) -> dict[str, Any] | None: ...

    def get_events(self, operation_id: str) -> list[dict[str, Any]]: ...

    def is_cancel_requested(self, operation_id: str) -> bool: ...

    def finish(
        self,
        *,
        operation_id: str,
        operation: dict[str, Any],
        event: dict[str, Any],
        response: dict[str, Any] | None,
        error: dict[str, Any] | None,
        admission: Callable[[], None] | None = None,
    ) -> tuple[bool, dict[str, Any]]: ...

    def cancel(
        self,
        *,
        operation_id: str,
        operation: dict[str, Any],
        event: dict[str, Any],
        response: dict[str, Any],
        error: dict[str, Any] | None,
    ) -> tuple[bool, dict[str, Any]]: ...

    def start(
        self,
        *,
        operation: dict[str, Any],
        events: list[dict[str, Any]],
        session_id: str | None,
        lease_owner: str,
        lease_expires_at: datetime,
    ) -> None: ...

    def recover_expired_leases(
        self,
        *,
        now: datetime,
        operation_builder: Callable[[dict[str, Any], int], dict[str, Any]],
        event_builder: Callable[[dict[str, Any], int], dict[str, Any]],
        error: dict[str, Any],
    ) -> list[dict[str, Any]]: ...

    def active_operation_ids(self) -> set[str]: ...

    def session_ids(self) -> dict[str, str]: ...

    def associate_session(self, operation_id: str, session_id: str) -> None: ...

    def prune(
        self,
        *,
        now: datetime,
        max_terminal_operations: int,
        max_idempotency_entries: int,
        max_terminal_sse_events: int,
        terminal_ttl_seconds: float | None,
    ) -> dict[str, int]: ...

    def reset(self) -> None: ...


def _json_document(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    try:
        document = json.loads(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError) as error:
        raise StorageRuntimeError("Operation state is not JSON serializable.") from error
    if not isinstance(document, dict):
        raise StorageRuntimeError("Operation state documents must be JSON objects.")
    return document


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _key(endpoint: str, key_hash: str) -> tuple[str, str]:
    return endpoint, key_hash


def _claim_copy(claim: StoredClaim) -> StoredClaim:
    return StoredClaim(
        endpoint=claim.endpoint,
        idempotency_key_hash=claim.idempotency_key_hash,
        request_hash=claim.request_hash,
        operation_id=claim.operation_id,
        mode=claim.mode,
        response=deepcopy(claim.response),
        response_type=claim.response_type,
        error=deepcopy(claim.error),
        created_at=claim.created_at,
        updated_at=claim.updated_at,
    )


class InMemoryOperationStore:
    """Thread-safe explicit adapter for development and deterministic tests."""

    def __init__(self) -> None:
        self._claims: dict[tuple[str, str], StoredClaim] = {}
        self._operations: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._sessions: dict[str, str] = {}
        self._cancelled: set[str] = set()
        self._lock = RLock()
        self._condition = Condition(self._lock)

    def begin(self, *, endpoint: str, idempotency_key_hash: str, request_hash: str, operation_id: str,
              operation: dict[str, Any], events: list[dict[str, Any]], mode: str,
              response: dict[str, Any] | None, response_type: str | None, session_id: str | None,
              lease_owner: str, lease_expires_at: datetime, admission: Callable[[], None] | None = None) -> BeginResult:
        now = _utc(datetime.now(timezone.utc))
        with self._condition:
            existing = self._claims.get(_key(endpoint, idempotency_key_hash))
            if existing is not None:
                self._check_hash(existing, request_hash)
                return BeginResult(False, _claim_copy(existing))
            claim = StoredClaim(endpoint, idempotency_key_hash, request_hash, operation_id, mode, None, response_type, None, now, now)
            self._claims[_key(endpoint, idempotency_key_hash)] = claim
            try:
                if admission is not None:
                    admission()
                stored_operation = deepcopy(operation)
                stored_operation["lease_expires_at"] = _utc(lease_expires_at).isoformat()
                self._operations[operation_id] = stored_operation
                self._events[operation_id] = deepcopy(events)
                if session_id is not None:
                    self._sessions[operation_id] = session_id
                if response is not None:
                    claim = StoredClaim(
                        claim.endpoint, claim.idempotency_key_hash, claim.request_hash, claim.operation_id,
                        claim.mode, deepcopy(response), claim.response_type, claim.error, claim.created_at, now,
                    )
                    self._claims[_key(endpoint, idempotency_key_hash)] = claim
                self._condition.notify_all()
                return BeginResult(True, _claim_copy(claim))
            except Exception:
                self._claims.pop(_key(endpoint, idempotency_key_hash), None)
                self._condition.notify_all()
                raise

    @staticmethod
    def _check_hash(claim: StoredClaim, request_hash: str) -> None:
        if claim.request_hash != request_hash:
            raise OperationStoreConflict("Idempotency-Key was already used with a different request body.")

    def get_claim(self, endpoint: str, idempotency_key_hash: str, request_hash: str) -> StoredClaim | None:
        with self._lock:
            claim = self._claims.get(_key(endpoint, idempotency_key_hash))
            if claim is None:
                return None
            self._check_hash(claim, request_hash)
            return _claim_copy(claim)

    def wait_for_terminal(self, endpoint: str, idempotency_key_hash: str, request_hash: str) -> StoredClaim:
        with self._condition:
            while True:
                claim = self._claims.get(_key(endpoint, idempotency_key_hash))
                if claim is None:
                    raise OperationStoreNotFound("Idempotency claim disappeared before its terminal checkpoint.")
                self._check_hash(claim, request_hash)
                if claim.terminal:
                    return _claim_copy(claim)
                self._condition.wait(timeout=0.25)

    def put_control(self, *, endpoint: str, idempotency_key_hash: str, request_hash: str, response: dict[str, Any],
                    response_type: str, now: datetime) -> StoredClaim:
        with self._condition:
            existing = self._claims.get(_key(endpoint, idempotency_key_hash))
            if existing is not None:
                self._check_hash(existing, request_hash)
                return _claim_copy(existing)
            claim = StoredClaim(endpoint, idempotency_key_hash, request_hash, None, "control", deepcopy(response), response_type, None, _utc(now), _utc(now))
            self._claims[_key(endpoint, idempotency_key_hash)] = claim
            self._condition.notify_all()
            return _claim_copy(claim)

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._operations.get(operation_id)
            return deepcopy(value) if value is not None else None

    def get_events(self, operation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._events.get(operation_id, []))

    def is_cancel_requested(self, operation_id: str) -> bool:
        with self._lock:
            return operation_id in self._cancelled

    def finish(self, *, operation_id: str, operation: dict[str, Any], event: dict[str, Any], response: dict[str, Any] | None,
               error: dict[str, Any] | None, admission: Callable[[], None] | None = None) -> tuple[bool, dict[str, Any]]:
        with self._condition:
            current = self._operations.get(operation_id)
            if current is None:
                raise OperationStoreNotFound("Operation not found.")
            if current.get("status") not in {"pending", "running"}:
                return False, deepcopy(current)
            if admission is not None:
                admission()
            self._events[operation_id].append(deepcopy(event))
            self._operations[operation_id] = deepcopy(operation)
            self._update_claim_for_operation(
                operation_id,
                response=response,
                error=error,
                operation_updated_at=operation.get("updated_at"),
            )
            self._condition.notify_all()
            return True, deepcopy(operation)

    def cancel(self, *, operation_id: str, operation: dict[str, Any], event: dict[str, Any], response: dict[str, Any], error: dict[str, Any] | None) -> tuple[bool, dict[str, Any]]:
        with self._condition:
            current = self._operations.get(operation_id)
            if current is None:
                raise OperationStoreNotFound("Operation not found.")
            if current.get("status") not in {"pending", "running"}:
                return False, deepcopy(current)
            self._cancelled.add(operation_id)
            self._events[operation_id].append(deepcopy(event))
            self._operations[operation_id] = deepcopy(operation)
            claim = self._claim_for_operation(operation_id)
            if claim is not None:
                self._update_claim_for_operation(
                    operation_id,
                    response=response if claim.mode == "async" else None,
                    error=error if claim.mode != "async" else None,
                    operation_updated_at=operation.get("updated_at"),
                )
            self._condition.notify_all()
            return True, deepcopy(operation)

    def start(self, *, operation: dict[str, Any], events: list[dict[str, Any]], session_id: str | None,
              lease_owner: str, lease_expires_at: datetime) -> None:
        with self._condition:
            operation_id = operation["operation_id"]
            if operation_id in self._operations:
                return
            stored_operation = deepcopy(operation)
            stored_operation["lease_expires_at"] = _utc(lease_expires_at).isoformat()
            self._operations[operation_id] = stored_operation
            self._events[operation_id] = deepcopy(events)
            if session_id is not None:
                self._sessions[operation_id] = session_id
            self._condition.notify_all()

    def _claim_for_operation(self, operation_id: str) -> StoredClaim | None:
        return next((claim for claim in self._claims.values() if claim.operation_id == operation_id), None)

    def _update_claim_for_operation(
        self,
        operation_id: str,
        *,
        response: dict[str, Any] | None,
        error: dict[str, Any] | None,
        operation_updated_at: Any | None,
    ) -> None:
        claim = self._claim_for_operation(operation_id)
        if claim is None:
            return
        updated_at = _parse_time(operation_updated_at) if operation_updated_at is not None else claim.updated_at
        self._claims[_key(claim.endpoint, claim.idempotency_key_hash)] = StoredClaim(
            claim.endpoint, claim.idempotency_key_hash, claim.request_hash, claim.operation_id, claim.mode,
            deepcopy(response) if response is not None else claim.response, claim.response_type,
            deepcopy(error) if error is not None else claim.error, claim.created_at, updated_at,
        )

    def recover_expired_leases(self, *, now: datetime, operation_builder: Callable[[dict[str, Any], int], dict[str, Any]],
                               event_builder: Callable[[dict[str, Any], int], dict[str, Any]], error: dict[str, Any]) -> list[dict[str, Any]]:
        recovered: list[dict[str, Any]] = []
        with self._condition:
            for operation_id, current in list(self._operations.items()):
                if current.get("status") not in {"pending", "running"}:
                    continue
                expires = current.get("lease_expires_at")
                if expires is None or _utc(datetime.fromisoformat(expires) if isinstance(expires, str) else expires) > _utc(now):
                    continue
                sequence = (self._events[operation_id][-1].get("sequence", 0) if self._events.get(operation_id) else 0) + 1
                event = event_builder(current, sequence)
                updated = operation_builder(current, sequence)
                self._events[operation_id].append(deepcopy(event))
                self._operations[operation_id] = deepcopy(updated)
                claim = self._claim_for_operation(operation_id)
                if claim is not None and claim.mode == "async":
                    self._update_claim_for_operation(
                        operation_id,
                        response={"operation": deepcopy(updated)},
                        error=None,
                        operation_updated_at=updated.get("updated_at"),
                    )
                else:
                    self._update_claim_for_operation(
                        operation_id,
                        response=None,
                        error=error,
                        operation_updated_at=updated.get("updated_at"),
                    )
                recovered.append(deepcopy(updated))
            self._condition.notify_all()
        return recovered

    def active_operation_ids(self) -> set[str]:
        with self._lock:
            return {key for key, value in self._operations.items() if value.get("status") in {"pending", "running"}}

    def session_ids(self) -> dict[str, str]:
        with self._lock:
            return dict(self._sessions)

    def associate_session(self, operation_id: str, session_id: str) -> None:
        with self._condition:
            if operation_id not in self._operations:
                raise OperationStoreNotFound("Operation not found.")
            existing = self._sessions.get(operation_id)
            if existing is not None and existing != session_id:
                raise OperationStoreConflict("Operation is already associated with another session.")
            self._sessions[operation_id] = session_id
            self._condition.notify_all()

    def prune(self, *, now: datetime, max_terminal_operations: int, max_idempotency_entries: int,
              max_terminal_sse_events: int, terminal_ttl_seconds: float | None) -> dict[str, int]:
        with self._condition:
            terminal = [value for value in self._operations.values() if value.get("status") in {"completed", "failed", "cancelled"}]
            terminal.sort(key=lambda value: value.get("updated_at", ""))
            cutoff = _utc(now) - timedelta(seconds=terminal_ttl_seconds) if terminal_ttl_seconds is not None else None
            remove_ids = {value["operation_id"] for value in terminal if cutoff is not None and _parse_time(value.get("updated_at")) <= cutoff}
            remove_ids.update(value["operation_id"] for value in terminal[:max(0, len(terminal) - max_terminal_operations)])
            for operation_id in remove_ids:
                self._operations.pop(operation_id, None)
                self._events.pop(operation_id, None)
                self._sessions.pop(operation_id, None)
                self._cancelled.discard(operation_id)
            trimmed = 0
            for operation_id, event_log in list(self._events.items()):
                operation = self._operations.get(operation_id)
                if operation is None or operation.get("status") not in {"completed", "failed", "cancelled"} or len(event_log) <= max_terminal_sse_events:
                    continue
                kept = event_log[-max_terminal_sse_events:] if max_terminal_sse_events else []
                trimmed += len(event_log) - len(kept)
                self._events[operation_id] = kept
            active_ids = {
                operation_id for operation_id, operation in self._operations.items()
                if operation.get("status") in {"pending", "running"}
            }
            terminal_claims = [
                claim for claim in self._claims.values()
                if claim.operation_id is None
                or claim.operation_id in remove_ids
                or (claim.terminal and claim.operation_id not in active_ids)
            ]
            terminal_claims.sort(key=lambda claim: claim.updated_at)
            remove_claims = {(_claim.endpoint, _claim.idempotency_key_hash) for _claim in terminal_claims if cutoff is not None and _claim.updated_at <= cutoff}
            remaining = [claim for claim in terminal_claims if (claim.endpoint, claim.idempotency_key_hash) in self._claims and (claim.endpoint, claim.idempotency_key_hash) not in remove_claims]
            excess = max(0, len(remaining) - max_idempotency_entries)
            for claim in remaining:
                if excess <= 0:
                    break
                remove_claims.add((claim.endpoint, claim.idempotency_key_hash))
                excess -= 1
            removed_idempotency = 0
            for key in remove_claims:
                claim = self._claims.get(key)
                if claim is not None and claim.operation_id not in active_ids:
                    self._claims.pop(key, None)
                    removed_idempotency += 1
                elif claim is not None:
                    continue
            self._condition.notify_all()
            return {"removed_terminal_operations": len(remove_ids), "removed_idempotency_entries": removed_idempotency, "trimmed_terminal_sse_events": trimmed}

    def reset(self) -> None:
        with self._condition:
            self._claims.clear(); self._operations.clear(); self._events.clear(); self._sessions.clear(); self._cancelled.clear()
            self._condition.notify_all()


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, str):
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    return datetime.min.replace(tzinfo=timezone.utc)


def _default_connection_factory(dsn: str) -> Any:
    try:
        import psycopg
    except ImportError as error:
        raise OperationStoreUnavailable("The PostgreSQL driver is unavailable.") from error
    try:
        return psycopg.connect(dsn)
    except Exception as error:
        raise OperationStoreUnavailable("PostgreSQL operation storage is unavailable.") from error


def _row_value(row: Any, key: str, index: int) -> Any:
    return row[key] if isinstance(row, Mapping) else row[index]


class PostgresOperationStore:
    """PostgreSQL implementation with row locks around all terminal transitions."""

    def __init__(self, dsn: str, *, connection_factory: Callable[[str], Any] | None = None) -> None:
        if not dsn or not dsn.strip():
            raise StorageConfigurationError("PostgreSQL operation storage requires a nonblank server-side DSN.")
        self._dsn = dsn.strip()
        self._connection_factory = connection_factory or _default_connection_factory
        self._uses_injected_connection = connection_factory is not None

    def _connection(self) -> Any:
        try:
            return self._connection_factory(self._dsn)
        except (OperationStoreUnavailable, StorageRuntimeError):
            raise
        except Exception as error:
            raise OperationStoreUnavailable("PostgreSQL operation storage is unavailable.") from error

    def _jsonb(self, document: dict[str, Any]) -> Any:
        if self._uses_injected_connection:
            return document
        try:
            from psycopg.types.json import Jsonb
        except ImportError as error:
            raise OperationStoreUnavailable("The PostgreSQL driver is unavailable.") from error
        return Jsonb(document)

    @staticmethod
    def _claim_from_row(row: Any) -> StoredClaim:
        return StoredClaim(
            endpoint=_row_value(row, "endpoint", 0), idempotency_key_hash=_row_value(row, "idempotency_key_hash", 1),
            request_hash=_row_value(row, "request_hash", 2), operation_id=_row_value(row, "operation_id", 3),
            mode=_row_value(row, "mode", 4), response=_as_dict(_row_value(row, "response", 5)),
            response_type=_row_value(row, "response_type", 6), error=_as_dict(_row_value(row, "error", 7)),
            created_at=_utc(_row_value(row, "created_at", 8)), updated_at=_utc(_row_value(row, "updated_at", 9)),
        )

    def _run(self, operation: Callable[[Any], Any]) -> Any:
        try:
            ambient = current_postgres_unit_of_work(self._dsn)
            if ambient is not None:
                return operation(ambient)
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    with postgres_unit_of_work(self._dsn, connection, cursor):
                        return operation((connection, cursor))
        except OperationStorePassThrough:
            raise
        except (OperationStoreUnavailable, StorageRuntimeError):
            raise
        except Exception as error:
            raise OperationStoreUnavailable("PostgreSQL operation storage failed.") from error

    def begin(self, *, endpoint: str, idempotency_key_hash: str, request_hash: str, operation_id: str,
              operation: dict[str, Any], events: list[dict[str, Any]], mode: str, response: dict[str, Any] | None,
              response_type: str | None, session_id: str | None, lease_owner: str, lease_expires_at: datetime,
              admission: Callable[[], None] | None = None) -> BeginResult:
        operation_doc = _json_document(operation); event_docs = [_json_document(event) for event in events]

        def action(ctx: tuple[Any, Any]) -> BeginResult:
            _connection, cursor = ctx
            cursor.execute(
                f"""INSERT INTO {OPERATION_IDEMPOTENCY_TABLE}
                    (endpoint, idempotency_key_hash, request_hash, mode, response_type)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (endpoint, idempotency_key_hash) DO NOTHING
                    RETURNING endpoint""",
                (endpoint, idempotency_key_hash, request_hash, mode, response_type),
            )
            inserted = cursor.fetchone() is not None
            if not inserted:
                cursor.execute(
                    f"""SELECT endpoint, idempotency_key_hash, request_hash, operation_id, mode, response, response_type, error, created_at, updated_at
                    FROM {OPERATION_IDEMPOTENCY_TABLE} WHERE endpoint = %s AND idempotency_key_hash = %s FOR UPDATE""",
                    (endpoint, idempotency_key_hash),
                )
                row = cursor.fetchone()
                if row is None:
                    raise OperationStoreUnavailable("Idempotency claim could not be read after a conflict.")
                claim = self._claim_from_row(row)
                if claim.request_hash != request_hash:
                    raise OperationStoreConflict("Idempotency-Key was already used with a different request body.")
                return BeginResult(False, claim)
            if admission is not None:
                admission()
            cursor.execute(
                f"""INSERT INTO {OPERATION_STATE_TABLE}
                    (operation_id, endpoint, idempotency_key_hash, status, operation, lease_owner, lease_expires_at)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)""",
                (operation_id, endpoint, idempotency_key_hash, operation_doc["status"], self._jsonb(operation_doc), lease_owner, lease_expires_at),
            )
            cursor.execute(
                f"UPDATE {OPERATION_IDEMPOTENCY_TABLE} SET operation_id = %s WHERE endpoint = %s AND idempotency_key_hash = %s",
                (operation_id, endpoint, idempotency_key_hash),
            )
            cursor.executemany(
                f"INSERT INTO {OPERATION_EVENTS_TABLE} (operation_id, sequence, event, emitted_at) VALUES (%s, %s, %s::jsonb, %s)",
                [(operation_id, event["sequence"], self._jsonb(event), event["emitted_at"]) for event in event_docs],
            )
            if session_id is not None:
                cursor.execute(
                    f"INSERT INTO {OPERATION_SESSIONS_TABLE} (operation_id, session_id) VALUES (%s, %s)",
                    (operation_id, session_id),
                )
            if response is not None:
                cursor.execute(
                    f"UPDATE {OPERATION_IDEMPOTENCY_TABLE} SET response = %s::jsonb, updated_at = %s WHERE endpoint = %s AND idempotency_key_hash = %s",
                    (self._jsonb(_json_document(response)), operation_doc["updated_at"], endpoint, idempotency_key_hash),
                )
            cursor.execute(
                f"""SELECT endpoint, idempotency_key_hash, request_hash, operation_id, mode, response, response_type, error, created_at, updated_at
                FROM {OPERATION_IDEMPOTENCY_TABLE} WHERE endpoint = %s AND idempotency_key_hash = %s FOR UPDATE""",
                (endpoint, idempotency_key_hash),
            )
            return BeginResult(True, self._claim_from_row(cursor.fetchone()))

        return self._run(action)

    def get_claim(self, endpoint: str, idempotency_key_hash: str, request_hash: str) -> StoredClaim | None:
        def action(ctx: tuple[Any, Any]) -> StoredClaim | None:
            _connection, cursor = ctx
            cursor.execute(
                f"SELECT endpoint, idempotency_key_hash, request_hash, operation_id, mode, response, response_type, error, created_at, updated_at FROM {OPERATION_IDEMPOTENCY_TABLE} WHERE endpoint = %s AND idempotency_key_hash = %s",
                (endpoint, idempotency_key_hash),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            claim = self._claim_from_row(row)
            if claim.request_hash != request_hash:
                raise OperationStoreConflict("Idempotency-Key was already used with a different request body.")
            return claim
        return self._run(action)

    def wait_for_terminal(self, endpoint: str, idempotency_key_hash: str, request_hash: str) -> StoredClaim:
        deadline = monotonic() + 300.0
        while monotonic() < deadline:
            claim = self.get_claim(endpoint, idempotency_key_hash, request_hash)
            if claim is None:
                raise OperationStoreNotFound("Idempotency claim disappeared before its terminal checkpoint.")
            if claim.terminal:
                return claim
            sleep(0.02)
        raise OperationStoreUnavailable("Timed out waiting for the idempotent terminal checkpoint.")

    def put_control(self, *, endpoint: str, idempotency_key_hash: str, request_hash: str, response: dict[str, Any], response_type: str, now: datetime) -> StoredClaim:
        response_doc = _json_document(response)
        def action(ctx: tuple[Any, Any]) -> StoredClaim:
            _connection, cursor = ctx
            cursor.execute(
                f"""INSERT INTO {OPERATION_IDEMPOTENCY_TABLE}
                    (endpoint, idempotency_key_hash, request_hash, mode, response, response_type, created_at, updated_at)
                    VALUES (%s, %s, %s, 'control', %s::jsonb, %s, %s, %s)
                    ON CONFLICT (endpoint, idempotency_key_hash) DO NOTHING""",
                (endpoint, idempotency_key_hash, request_hash, self._jsonb(response_doc), response_type, now, now),
            )
            cursor.execute(
                f"SELECT endpoint, idempotency_key_hash, request_hash, operation_id, mode, response, response_type, error, created_at, updated_at FROM {OPERATION_IDEMPOTENCY_TABLE} WHERE endpoint = %s AND idempotency_key_hash = %s FOR UPDATE",
                (endpoint, idempotency_key_hash),
            )
            claim = self._claim_from_row(cursor.fetchone())
            if claim.request_hash != request_hash:
                raise OperationStoreConflict("Idempotency-Key was already used with a different control request.")
            return claim
        return self._run(action)

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        def action(ctx: tuple[Any, Any]) -> dict[str, Any] | None:
            _connection, cursor = ctx
            cursor.execute(f"SELECT operation FROM {OPERATION_STATE_TABLE} WHERE operation_id = %s", (operation_id,))
            row = cursor.fetchone()
            return _as_dict(row[0] if row is not None else None)
        return self._run(action)

    def get_events(self, operation_id: str) -> list[dict[str, Any]]:
        def action(ctx: tuple[Any, Any]) -> list[dict[str, Any]]:
            _connection, cursor = ctx
            cursor.execute(f"SELECT event FROM {OPERATION_EVENTS_TABLE} WHERE operation_id = %s ORDER BY sequence", (operation_id,))
            return [_as_dict(row[0]) for row in cursor.fetchall()]
        return self._run(action)

    def is_cancel_requested(self, operation_id: str) -> bool:
        def action(ctx: tuple[Any, Any]) -> bool:
            _connection, cursor = ctx
            cursor.execute(f"SELECT status = 'cancelled' FROM {OPERATION_STATE_TABLE} WHERE operation_id = %s", (operation_id,))
            row = cursor.fetchone()
            return bool(row and row[0])
        return self._run(action)

    def finish(self, *, operation_id: str, operation: dict[str, Any], event: dict[str, Any], response: dict[str, Any] | None, error: dict[str, Any] | None,
               admission: Callable[[], None] | None = None) -> tuple[bool, dict[str, Any]]:
        return self._transition(operation_id=operation_id, operation=operation, event=event, response=response, error=error, cancel=False, admission=admission)

    def cancel(self, *, operation_id: str, operation: dict[str, Any], event: dict[str, Any], response: dict[str, Any], error: dict[str, Any] | None) -> tuple[bool, dict[str, Any]]:
        return self._transition(operation_id=operation_id, operation=operation, event=event, response=response, error=error, cancel=True, admission=None)

    def start(self, *, operation: dict[str, Any], events: list[dict[str, Any]], session_id: str | None,
              lease_owner: str, lease_expires_at: datetime) -> None:
        operation_doc = _json_document(operation); event_docs = [_json_document(event) for event in events]
        def action(ctx: tuple[Any, Any]) -> None:
            _connection, cursor = ctx; operation_id = operation_doc["operation_id"]
            cursor.execute(f"INSERT INTO {OPERATION_STATE_TABLE} (operation_id, endpoint, idempotency_key_hash, status, operation, lease_owner, lease_expires_at) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s) ON CONFLICT (operation_id) DO NOTHING", (operation_id, operation_doc["endpoint"], operation_doc["idempotency_key_hash"], operation_doc["status"], self._jsonb(operation_doc), lease_owner, lease_expires_at))
            cursor.executemany(f"INSERT INTO {OPERATION_EVENTS_TABLE} (operation_id, sequence, event, emitted_at) VALUES (%s, %s, %s::jsonb, %s) ON CONFLICT (operation_id, sequence) DO NOTHING", [(operation_id, event["sequence"], self._jsonb(event), event["emitted_at"]) for event in event_docs])
            if session_id is not None:
                cursor.execute(f"INSERT INTO {OPERATION_SESSIONS_TABLE} (operation_id, session_id) VALUES (%s, %s) ON CONFLICT (operation_id) DO NOTHING", (operation_id, session_id))
        self._run(action)

    def _transition(self, *, operation_id: str, operation: dict[str, Any], event: dict[str, Any], response: dict[str, Any] | None, error: dict[str, Any] | None, cancel: bool,
                    admission: Callable[[], None] | None) -> tuple[bool, dict[str, Any]]:
        operation_doc = _json_document(operation); event_doc = _json_document(event)
        def action(ctx: tuple[Any, Any]) -> tuple[bool, dict[str, Any]]:
            _connection, cursor = ctx
            cursor.execute(f"SELECT operation, status FROM {OPERATION_STATE_TABLE} WHERE operation_id = %s FOR UPDATE", (operation_id,))
            row = cursor.fetchone()
            if row is None:
                raise OperationStoreNotFound("Operation not found.")
            current = _as_dict(row[0]) or {}
            if _row_value(row, "status", 1) not in {"pending", "running"}:
                return False, current
            if admission is not None:
                admission()
            cursor.execute(f"SELECT COALESCE(MAX(sequence), 0) FROM {OPERATION_EVENTS_TABLE} WHERE operation_id = %s", (operation_id,))
            sequence = int(cursor.fetchone()[0]) + 1
            event_doc["sequence"] = sequence
            operation_doc["last_event_id"] = f"{operation_id}:{sequence}"
            cursor.execute(f"INSERT INTO {OPERATION_EVENTS_TABLE} (operation_id, sequence, event, emitted_at) VALUES (%s, %s, %s::jsonb, %s)", (operation_id, sequence, self._jsonb(event_doc), event_doc["emitted_at"]))
            cursor.execute(f"UPDATE {OPERATION_STATE_TABLE} SET status = %s, operation = %s::jsonb, lease_owner = NULL, lease_expires_at = NULL, updated_at = %s WHERE operation_id = %s", (operation_doc["status"], self._jsonb(operation_doc), operation_doc["updated_at"], operation_id))
            cursor.execute(f"SELECT endpoint, idempotency_key_hash, mode FROM {OPERATION_IDEMPOTENCY_TABLE} WHERE operation_id = %s FOR UPDATE", (operation_id,))
            claim_row = cursor.fetchone()
            if claim_row is not None:
                mode = _row_value(claim_row, "mode", 2)
                if response is not None and (mode == "async" or not cancel):
                    cursor.execute(f"UPDATE {OPERATION_IDEMPOTENCY_TABLE} SET response = %s::jsonb, error = NULL, updated_at = %s WHERE operation_id = %s", (self._jsonb(_json_document(response)), operation_doc["updated_at"], operation_id))
                elif error is not None:
                    cursor.execute(f"UPDATE {OPERATION_IDEMPOTENCY_TABLE} SET error = %s::jsonb, response = NULL, updated_at = %s WHERE operation_id = %s", (self._jsonb(_json_document(error)), operation_doc["updated_at"], operation_id))
            return True, operation_doc
        return self._run(action)

    def recover_expired_leases(self, *, now: datetime, operation_builder: Callable[[dict[str, Any], int], dict[str, Any]], event_builder: Callable[[dict[str, Any], int], dict[str, Any]], error: dict[str, Any]) -> list[dict[str, Any]]:
        def action(ctx: tuple[Any, Any]) -> list[dict[str, Any]]:
            _connection, cursor = ctx
            cursor.execute(f"SELECT operation_id, operation FROM {OPERATION_STATE_TABLE} WHERE status IN ('pending', 'running') AND lease_expires_at IS NOT NULL AND lease_expires_at <= %s FOR UPDATE", (now,))
            rows = cursor.fetchall(); recovered: list[dict[str, Any]] = []
            for row in rows:
                current = _as_dict(row[1]) or {}; operation_id = row[0]
                cursor.execute(f"SELECT COALESCE(MAX(sequence), 0) FROM {OPERATION_EVENTS_TABLE} WHERE operation_id = %s", (operation_id,))
                sequence = int(cursor.fetchone()[0]) + 1
                event = _json_document(event_builder(current, sequence)); operation = _json_document(operation_builder(current, sequence));
                cursor.execute(f"INSERT INTO {OPERATION_EVENTS_TABLE} (operation_id, sequence, event, emitted_at) VALUES (%s, %s, %s::jsonb, %s)", (operation_id, sequence, self._jsonb(event), event["emitted_at"]))
                cursor.execute(f"UPDATE {OPERATION_STATE_TABLE} SET status = 'failed', operation = %s::jsonb, lease_owner = NULL, lease_expires_at = NULL, updated_at = %s WHERE operation_id = %s", (self._jsonb(operation), operation["updated_at"], operation_id))
                cursor.execute(f"SELECT mode FROM {OPERATION_IDEMPOTENCY_TABLE} WHERE operation_id = %s FOR UPDATE", (operation_id,))
                claim = cursor.fetchone()
                if claim is not None and _row_value(claim, "mode", 0) == "async":
                    cursor.execute(f"UPDATE {OPERATION_IDEMPOTENCY_TABLE} SET response = %s::jsonb, error = NULL, updated_at = %s WHERE operation_id = %s", (self._jsonb({"operation": operation}), operation["updated_at"], operation_id))
                else:
                    cursor.execute(f"UPDATE {OPERATION_IDEMPOTENCY_TABLE} SET error = %s::jsonb, response = NULL, updated_at = %s WHERE operation_id = %s", (self._jsonb(error), operation["updated_at"], operation_id))
                recovered.append(operation)
            return recovered
        return self._run(action)

    def active_operation_ids(self) -> set[str]:
        def action(ctx: tuple[Any, Any]) -> set[str]:
            _connection, cursor = ctx; cursor.execute(f"SELECT operation_id FROM {OPERATION_STATE_TABLE} WHERE status IN ('pending', 'running')"); return {row[0] for row in cursor.fetchall()}
        return self._run(action)

    def session_ids(self) -> dict[str, str]:
        def action(ctx: tuple[Any, Any]) -> dict[str, str]:
            _connection, cursor = ctx; cursor.execute(f"SELECT operation_id, session_id FROM {OPERATION_SESSIONS_TABLE}"); return {row[0]: row[1] for row in cursor.fetchall()}
        return self._run(action)

    def associate_session(self, operation_id: str, session_id: str) -> None:
        def action(ctx: tuple[Any, Any]) -> None:
            _connection, cursor = ctx
            cursor.execute(
                f"INSERT INTO {OPERATION_SESSIONS_TABLE} (operation_id, session_id) VALUES (%s, %s) ON CONFLICT (operation_id) DO UPDATE SET session_id = EXCLUDED.session_id WHERE {OPERATION_SESSIONS_TABLE}.session_id = EXCLUDED.session_id RETURNING operation_id",
                (operation_id, session_id),
            )
            if cursor.fetchone() is None:
                raise OperationStoreConflict("Operation is already associated with another session.")
        self._run(action)

    def prune(self, *, now: datetime, max_terminal_operations: int, max_idempotency_entries: int, max_terminal_sse_events: int, terminal_ttl_seconds: float | None) -> dict[str, int]:
        def action(ctx: tuple[Any, Any]) -> dict[str, int]:
            _connection, cursor = ctx; removed_operations = 0; removed_idempotency = 0; trimmed = 0
            if terminal_ttl_seconds is not None:
                cursor.execute(f"DELETE FROM {OPERATION_STATE_TABLE} WHERE status IN ('completed', 'failed', 'cancelled') AND updated_at <= %s", (now - timedelta(seconds=terminal_ttl_seconds),)); removed_operations += cursor.rowcount
            cursor.execute(f"""DELETE FROM {OPERATION_STATE_TABLE} WHERE operation_id IN (
                SELECT operation_id FROM {OPERATION_STATE_TABLE} WHERE status IN ('completed', 'failed', 'cancelled') ORDER BY updated_at LIMIT GREATEST((SELECT COUNT(*) FROM {OPERATION_STATE_TABLE} WHERE status IN ('completed', 'failed', 'cancelled')) - %s, 0)
            )""", (max_terminal_operations,)); removed_operations += cursor.rowcount
            cursor.execute(f"""WITH ranked AS (
                SELECT operation_id, sequence, ROW_NUMBER() OVER (PARTITION BY operation_id ORDER BY sequence DESC) AS rank
                FROM {OPERATION_EVENTS_TABLE} e JOIN {OPERATION_STATE_TABLE} o USING (operation_id)
                WHERE o.status IN ('completed', 'failed', 'cancelled')
            ) DELETE FROM {OPERATION_EVENTS_TABLE} e USING ranked r WHERE e.operation_id = r.operation_id AND e.sequence = r.sequence AND r.rank > %s""", (max_terminal_sse_events,)); trimmed += cursor.rowcount
            if max_idempotency_entries >= 0:
                eligible_claim = f"(operation_id IS NULL OR operation_id NOT IN (SELECT operation_id FROM {OPERATION_STATE_TABLE} WHERE status IN ('pending', 'running')))"
                if terminal_ttl_seconds is not None:
                    cursor.execute(
                        f"DELETE FROM {OPERATION_IDEMPOTENCY_TABLE} WHERE {eligible_claim} AND updated_at <= %s",
                        (now - timedelta(seconds=terminal_ttl_seconds),),
                    )
                    removed_idempotency += cursor.rowcount
                cursor.execute(
                    f"""DELETE FROM {OPERATION_IDEMPOTENCY_TABLE}
                    WHERE {eligible_claim}
                      AND (endpoint, idempotency_key_hash) IN (
                        SELECT endpoint, idempotency_key_hash
                        FROM {OPERATION_IDEMPOTENCY_TABLE}
                        WHERE {eligible_claim}
                        ORDER BY updated_at ASC, endpoint ASC, idempotency_key_hash ASC
                        LIMIT GREATEST((SELECT COUNT(*) FROM {OPERATION_IDEMPOTENCY_TABLE} WHERE {eligible_claim}) - %s, 0)
                    )""",
                    (max_idempotency_entries,),
                )
                removed_idempotency += cursor.rowcount
            return {"removed_terminal_operations": removed_operations, "removed_idempotency_entries": removed_idempotency, "trimmed_terminal_sse_events": trimmed}
        return self._run(action)

    def reset(self) -> None:
        def action(ctx: tuple[Any, Any]) -> None:
            _connection, cursor = ctx
            cursor.execute(f"TRUNCATE TABLE {OPERATION_EVENTS_TABLE}, {OPERATION_SESSIONS_TABLE}, {OPERATION_IDEMPOTENCY_TABLE}, {OPERATION_STATE_TABLE}")
        self._run(action)

    def check_ready(self) -> None:
        self._run(lambda ctx: ctx[1].execute("SELECT 1"))


def _as_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else None


def build_operation_store(config: StorageConfig) -> OperationStore:
    if config.mode is StorageMode.MEMORY:
        if config.requires_postgres:
            raise StorageConfigurationError("The memory operation store is not permitted in the selected runtime.")
        return InMemoryOperationStore()
    if not config.dsn or not config.dsn.strip():
        raise StorageConfigurationError("PostgreSQL operation storage requires a nonblank server-side DSN.")
    return PostgresOperationStore(config.dsn)


__all__ = [
    "BeginResult", "InMemoryOperationStore", "OPERATION_EVENTS_TABLE", "OPERATION_IDEMPOTENCY_TABLE",
    "OPERATION_SESSIONS_TABLE", "OPERATION_STATE_TABLE", "OperationStore", "OperationStoreConflict",
    "OperationStoreNotFound", "OperationStoreUnavailable", "PostgresOperationStore", "StoredClaim",
    "OperationStorePassThrough",
    "build_operation_store",
]
