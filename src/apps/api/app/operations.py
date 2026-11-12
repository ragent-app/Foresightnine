from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from threading import Event, RLock, Thread
from time import sleep
from typing import Any, Callable
from uuid import uuid4

from pydantic import BaseModel

from .models import (
    ErrorDetail, ErrorResponse, HeartbeatEvent, Operation, OperationCompleteEvent,
    OperationErrorEvent, OperationProgressEvent, OperationResponse, OperationStartedEvent, SourceRef,
)
from .operation_store import InMemoryOperationStore, OperationStore, OperationStoreConflict, OperationStorePassThrough, StoredClaim
from .phase3 import ProviderFailure
from .providers import FixedClock
from .services import canonical_json, content_hash, deterministic_id, validate_payload
from .telemetry import record_provider_failure


class ApiProblem(OperationStorePassThrough):
    def __init__(self, status_code: int, error_code: str, message: str, *, retryable: bool = False,
                 details: list[ErrorDetail] | None = None, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code; self.error_code = error_code; self.message = message
        self.retryable = retryable; self.details = details or []; self.retry_after = retry_after


@dataclass(frozen=True)
class IdempotencyEntry:
    request_hash: str
    response: BaseModel | ApiProblem
    stored_at: datetime | None = None


@dataclass(frozen=True)
class PendingProviderExecution:
    endpoint: str
    idempotency_key_hash: str
    request_hash: str


@dataclass(frozen=True)
class SingleFlight:
    request_hash: str
    operation_id: str
    completed: Event


class _CancellationSignal(Event):
    """An Event that also observes cancellation committed by another manager."""

    def __init__(self, store: OperationStore, operation_id: str) -> None:
        super().__init__(); self._store = store; self._operation_id = operation_id

    def is_set(self) -> bool:
        return super().is_set() or self._store.is_cancel_requested(self._operation_id)

    def wait(self, timeout: float | None = None) -> bool:
        if super().is_set(): return True
        end = None if timeout is None else datetime.now().timestamp() + timeout
        while True:
            if self.is_set(): return True
            if end is not None and datetime.now().timestamp() >= end: return False
            sleep(0.02)


def _dump(value: BaseModel | dict[str, Any]) -> dict[str, Any]:
    return value.model_dump(mode="json", exclude_none=True) if isinstance(value, BaseModel) else dict(value)


def _operation_for_model(document: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in document.items()
        if key not in {"lease_owner", "lease_expires_at", "owner_subject"}
    }


def _error_record(error: ApiProblem) -> dict[str, Any]:
    return {"status_code": error.status_code, "error_code": error.error_code, "message": error.message,
            "retryable": error.retryable, "retry_after": error.retry_after,
            "details": [detail.model_dump(mode="json", exclude_none=True) for detail in error.details]}


def _problem(record: dict[str, Any]) -> ApiProblem:
    return ApiProblem(int(record["status_code"]), str(record["error_code"]), str(record["message"]),
                      retryable=bool(record.get("retryable", False)),
                      details=[ErrorDetail.model_validate(item) for item in record.get("details", [])],
                      retry_after=record.get("retry_after"))


_EVENT_MODELS: dict[str, type[BaseModel]] = {
    "operation.started": OperationStartedEvent, "operation.progress": OperationProgressEvent,
    "heartbeat": HeartbeatEvent, "operation.error": OperationErrorEvent, "operation.complete": OperationCompleteEvent,
}


class OperationManager:
    def __init__(self, clock: FixedClock | None = None, *, store: OperationStore | None = None, lease_seconds: int = 300) -> None:
        self.clock = clock or FixedClock(); self.store = store or InMemoryOperationStore()
        self.lease_seconds = max(1, lease_seconds); self.lease_owner = f"manager:{uuid4().hex}"
        # Compatibility mirrors; the store is authoritative and is shared across managers.
        self._idempotency: dict[tuple[str, str], IdempotencyEntry] = {}
        self._single_flight: dict[tuple[str, str], SingleFlight] = {}
        self._pending_provider_executions: dict[str, PendingProviderExecution] = {}
        self._operation_keys: dict[str, tuple[str, str]] = {}
        self._cancel_events: dict[str, _CancellationSignal] = {}
        self._transition_locks: dict[str, RLock] = {}
        self._terminal_observer: Callable[[str, str], None] | None = None
        self.operations: dict[str, Operation] = {}; self.events: dict[str, list[BaseModel]] = {}; self._lock = RLock()

    def reset(self) -> None:
        with self._lock:
            self.store.reset(); self._idempotency.clear(); self._single_flight.clear(); self._pending_provider_executions.clear()
            self._operation_keys.clear(); self._cancel_events.clear(); self.operations.clear(); self.events.clear()
            self._transition_locks.clear()

    def set_terminal_observer(self, observer: Callable[[str, str], None] | None) -> None:
        self._terminal_observer = observer

    def _transition_lock(self, operation_id: str) -> RLock:
        with self._lock:
            return self._transition_locks.setdefault(operation_id, RLock())

    def _notify_terminal(self, operation_id: str, status: str) -> None:
        observer = self._terminal_observer
        if observer is not None:
            observer(operation_id, status)

    def _operation(self, *, operation_id: str, endpoint: str, idempotency_key_hash: str, status: str,
                   last_sequence: int, created_at: datetime | None = None) -> Operation:
        refs = (SourceRef(ref_id="mock:operation", source_type="model_inference", label="Deterministic operation controller"),)
        payload = {"operation_id": operation_id, "endpoint": endpoint, "status": status, "updated_at": self.clock.now(),
                   "idempotency_key_hash": idempotency_key_hash,
                   "cancel_token": sha256(f"cancel\0{operation_id}".encode()).hexdigest(),
                   "resume_token": sha256(f"resume\0{operation_id}".encode()).hexdigest(),
                   "last_event_id": f"{operation_id}:{last_sequence}"}
        operation = Operation(schema_version="v1", created_at=created_at or self.clock.now(), source_refs=refs,
                              content_hash=content_hash({"schema_version": "v1", "source_refs": [_dump(ref) for ref in refs], **payload}), **payload)
        validate_payload(operation, "Operation"); return operation

    def _base_event(self, operation_id: str, sequence: int) -> dict[str, Any]:
        return {"schema_version": "v1", "operation_id": operation_id, "sequence": sequence, "emitted_at": self.clock.now()}

    def _initial(self, operation_id: str) -> list[BaseModel]:
        events = [
            OperationStartedEvent(**self._base_event(operation_id, 1), event="operation.started", message="Operation accepted."),
            OperationProgressEvent(**self._base_event(operation_id, 2), event="operation.progress", progress=0.0, message="Operation is awaiting provider output."),
            HeartbeatEvent(**self._base_event(operation_id, 3), event="heartbeat"),
        ]
        for event in events: validate_payload(event, event.__class__.__name__)
        return events

    @staticmethod
    def _event_model(document: dict[str, Any]) -> BaseModel:
        model = _EVENT_MODELS.get(str(document.get("event")))
        if model is None: raise RuntimeError("Stored operation event has an unknown type.")
        return model.model_validate(document)

    def _cache_operation(self, operation_id: str, operation: dict[str, Any] | None = None,
                         events: list[dict[str, Any]] | None = None) -> Operation:
        document = operation if operation is not None else self.store.get_operation(operation_id)
        if document is None: raise ApiProblem(404, "not_found", "Operation not found.")
        parsed = Operation.model_validate(_operation_for_model(document)); event_docs = events if events is not None else self.store.get_events(operation_id)
        with self._lock:
            self.operations[operation_id] = parsed; self.events[operation_id] = [self._event_model(item) for item in event_docs]
            self._operation_keys[operation_id] = (parsed.endpoint, parsed.idempotency_key_hash)
            self._cancel_events.setdefault(operation_id, _CancellationSignal(self.store, operation_id))
        return parsed

    def cancel_event(self, operation_id: str) -> Event:
        with self._lock:
            signal = self._cancel_events.get(operation_id)
            if signal is None:
                if self.store.get_operation(operation_id) is None: raise RuntimeError("Provider attempted work before its operation was registered.")
                signal = _CancellationSignal(self.store, operation_id); self._cancel_events[operation_id] = signal
            return signal

    @staticmethod
    def _key_hash(idempotency_key: str) -> str: return sha256(idempotency_key.encode("utf-8")).hexdigest()

    @staticmethod
    def _operation_id(endpoint: str, key_hash: str) -> str: return deterministic_id("operation", f"{endpoint}\0{key_hash}")

    @classmethod
    def _scoped_key_hash(cls, owner_subject: str, idempotency_key: str) -> str:
        """Namespace every idempotency claim without persisting bearer material."""
        if owner_subject == "anonymous":
            # Preserve the established anonymous-capability namespace and its
            # deterministic receipts; authenticated subjects are isolated.
            return cls._key_hash(idempotency_key)
        return sha256(f"{owner_subject}\0{cls._key_hash(idempotency_key)}".encode("utf-8")).hexdigest()

    @staticmethod
    def _stored_operation(operation: Operation, owner_subject: str) -> dict[str, Any]:
        document = _dump(operation)
        document["owner_subject"] = owner_subject
        return document

    def owner_subject(self, operation_id: str) -> str:
        document = self.store.get_operation(operation_id)
        if document is None:
            raise ApiProblem(404, "not_found", "Operation not found.")
        owner = document.get("owner_subject")
        if not isinstance(owner, str) or not owner:
            # Existing unowned receipts are not made cross-account readable by
            # an upgrade. New work always carries an explicit subject.
            raise ApiProblem(404, "not_found", "Operation not found.")
        return owner

    def require_owner(self, operation_id: str, owner_subject: str) -> None:
        if self.owner_subject(operation_id) != owner_subject:
            raise ApiProblem(404, "not_found", "Operation not found.")

    @staticmethod
    def _claim_result(claim: StoredClaim, response_model: type[BaseModel]) -> BaseModel:
        if claim.error is not None: raise _problem(claim.error)
        if claim.response is None: raise ApiProblem(500, "internal_error", "Operation did not reach an idempotent terminal checkpoint.", retryable=True)
        return response_model.model_validate(claim.response)

    def _remember(self, endpoint: str, key_hash: str, request_hash: str, response: BaseModel | ApiProblem) -> None:
        """Keep an already-typed local result for same-process waiters."""
        self._idempotency[(endpoint, key_hash)] = IdempotencyEntry(request_hash, response, self.clock.now())

    def _begin(self, *, endpoint: str, idempotency_key: str, owner_subject: str, request_hash: str, mode: str,
               response: BaseModel | None, response_type: str | None, session_id: str | None,
               admission: Callable[[], None] | None = None) -> tuple[str, bool, StoredClaim]:
        key_hash = self._scoped_key_hash(owner_subject, idempotency_key); operation_id = self._operation_id(endpoint, key_hash); events = self._initial(operation_id)
        operation = self._operation(operation_id=operation_id, endpoint=endpoint, idempotency_key_hash=key_hash, status="running", last_sequence=3)
        try:
            result = self.store.begin(endpoint=endpoint, idempotency_key_hash=key_hash, request_hash=request_hash, operation_id=operation_id,
                                      operation=self._stored_operation(operation, owner_subject), events=[_dump(event) for event in events], mode=mode,
                                      response=_dump(response) if response is not None else None, response_type=response_type, session_id=session_id,
                                      lease_owner=self.lease_owner, lease_expires_at=self.clock.now() + timedelta(seconds=self.lease_seconds), admission=admission)
        except OperationStoreConflict as error:
            raise ApiProblem(409, "idempotency_conflict", str(error)) from error
        if result.owner: self._cache_operation(operation_id, self._stored_operation(operation, owner_subject), [_dump(event) for event in events])
        return operation_id, result.owner, result.claim

    def _finish_failure(self, *, operation_id: str, endpoint: str, key_hash: str, error: ApiProblem, async_mode: bool) -> None:
        current = self._cache_operation(operation_id); sequence = len(self.store.get_events(operation_id)) + 1
        event = OperationErrorEvent(**self._base_event(operation_id, sequence), event="operation.error",
                                    error=ErrorResponse(schema_version="v1", error_code=error.error_code, message=error.message,
                                                        request_id=deterministic_id("request", operation_id), retryable=error.retryable, details=error.details),
                                    retryable=error.retryable)
        updated = self._operation(operation_id=operation_id, endpoint=current.endpoint, idempotency_key_hash=current.idempotency_key_hash,
                                  status="failed", last_sequence=sequence, created_at=current.created_at)
        receipt = OperationResponse(operation=updated) if async_mode else None
        won, actual = self.store.finish(operation_id=operation_id, operation=self._stored_operation(updated, self.owner_subject(operation_id)), event=_dump(event),
                                        response=_dump(receipt) if receipt is not None else None, error=None if async_mode else _error_record(error))
        self._cache_operation(operation_id, actual)
        if won:
            self._notify_terminal(operation_id, "failed")
            self._pending_provider_executions.pop(operation_id, None)

    def execute(self, *, endpoint: str, idempotency_key: str, owner_subject: str = "anonymous", request_payload: dict[str, Any], response_model: type[BaseModel],
                result_field: str, build_result: Callable[[str], tuple[BaseModel, str]], session_id: str | None = None,
                commit_result: Callable[[str, BaseModel], None] | None = None,
                rollback_result: Callable[[str, BaseModel], None] | None = None) -> tuple[BaseModel, bool]:
        request_hash = content_hash(request_payload); key_hash = self._scoped_key_hash(owner_subject, idempotency_key)
        operation_id, owner, claim = self._begin(endpoint=endpoint, idempotency_key=idempotency_key, owner_subject=owner_subject, request_hash=request_hash, mode="sync",
                                                 response=None, response_type=response_model.__name__, session_id=session_id)
        if not owner:
            if not claim.terminal: claim = self.store.wait_for_terminal(endpoint, key_hash, request_hash)
            cached = self._idempotency.get((endpoint, key_hash))
            if cached is not None:
                if isinstance(cached.response, ApiProblem): raise cached.response
                return cached.response, True
            return self._claim_result(claim, response_model), True
        flight = SingleFlight(request_hash, operation_id, Event()); self._single_flight[(endpoint, key_hash)] = flight
        self._pending_provider_executions[operation_id] = PendingProviderExecution(endpoint, key_hash, request_hash)
        try:
            try: result, result_ref = build_result(operation_id)
            except ProviderFailure as failure:
                status_code = 503 if failure.retryable else 500
                error_code = "internal_error"
                record_provider_failure(endpoint=endpoint); error = ApiProblem(
                    status_code,
                    error_code,
                    "Provider request could not produce an accepted result.",
                    retryable=failure.retryable,
                    retry_after=15 if failure.retryable else None,
                )
                self._remember(endpoint, key_hash, request_hash, error)
                self._finish_failure(operation_id=operation_id, endpoint=endpoint, key_hash=key_hash, error=error, async_mode=False); raise error from failure
            except ApiProblem as error:
                self._remember(endpoint, key_hash, request_hash, error)
                self._finish_failure(operation_id=operation_id, endpoint=endpoint, key_hash=key_hash, error=error, async_mode=False); raise
            except Exception as failure:
                record_provider_failure(endpoint=endpoint); error = ApiProblem(500, "internal_error", "Provider request could not produce an accepted result.", retryable=True)
                self._remember(endpoint, key_hash, request_hash, error)
                self._finish_failure(operation_id=operation_id, endpoint=endpoint, key_hash=key_hash, error=error, async_mode=False); raise error from failure
            try:
                committed = False
                with self._transition_lock(operation_id):
                    current = self._cache_operation(operation_id)
                    if self.cancel_event(operation_id).is_set() or current.status == "cancelled":
                        raise ApiProblem(500, "internal_error", "Provider request was cancelled before an accepted result.", retryable=True)
                    sequence = len(self.store.get_events(operation_id)) + 1; result_hash = getattr(result, "content_hash", content_hash(result))
                    event = OperationCompleteEvent(**self._base_event(operation_id, sequence), event="operation.complete", result_ref=result_ref, result_hash=result_hash); validate_payload(event, "OperationCompleteEvent")
                    operation = self._operation(operation_id=operation_id, endpoint=current.endpoint, idempotency_key_hash=current.idempotency_key_hash,
                                                status="completed", last_sequence=sequence, created_at=current.created_at)
                    response = response_model(operation=operation, **{result_field: result})
                    try:
                        def commit_admission() -> None:
                            nonlocal committed
                            committed = True
                            if commit_result is not None:
                                commit_result(operation_id, result)
                        won, actual = self.store.finish(
                            operation_id=operation_id, operation=self._stored_operation(operation, owner_subject),
                            event=_dump(event), response=_dump(response), error=None,
                            admission=commit_admission if commit_result is not None else None,
                        )
                    except BaseException:
                        if committed and rollback_result is not None: rollback_result(operation_id, result)
                        raise
                    self._cache_operation(operation_id, actual)
                    if not won and committed and rollback_result is not None:
                        rollback_result(operation_id, result)
            except ApiProblem as error:
                self._remember(endpoint, key_hash, request_hash, error)
                if self._cache_operation(operation_id).status not in {"cancelled", "completed", "failed"}:
                    self._finish_failure(operation_id=operation_id, endpoint=endpoint, key_hash=key_hash, error=error, async_mode=False)
                raise
            except Exception as failure:
                record_provider_failure(endpoint=endpoint)
                error = ApiProblem(500, "internal_error", "Provider request could not produce an accepted result.", retryable=True)
                self._remember(endpoint, key_hash, request_hash, error)
                if self._cache_operation(operation_id).status not in {"cancelled", "completed", "failed"}:
                    self._finish_failure(operation_id=operation_id, endpoint=endpoint, key_hash=key_hash, error=error, async_mode=False)
                raise error from failure
            if not won:
                claim = self.store.get_claim(endpoint, key_hash, request_hash)
                if claim is None: raise ApiProblem(500, "internal_error", "Operation completion was lost.", retryable=True)
                return self._claim_result(claim, response_model), True
            self._remember(endpoint, key_hash, request_hash, response)
            return response, False
        finally:
            self._pending_provider_executions.pop(operation_id, None); self._single_flight.pop((endpoint, key_hash), None); flight.completed.set()

    def start(self, *, endpoint: str, idempotency_key: str, owner_subject: str = "anonymous", session_id: str | None = None) -> Operation:
        key_hash = self._scoped_key_hash(owner_subject, idempotency_key); operation_id = self._operation_id(endpoint, key_hash); events = self._initial(operation_id)
        operation = self._operation(operation_id=operation_id, endpoint=endpoint, idempotency_key_hash=key_hash, status="running", last_sequence=3)
        self.store.start(operation=self._stored_operation(operation, owner_subject), events=[_dump(event) for event in events], session_id=session_id,
                         lease_owner=self.lease_owner, lease_expires_at=self.clock.now() + timedelta(seconds=self.lease_seconds))
        return self._cache_operation(operation_id, self._stored_operation(operation, owner_subject), [_dump(event) for event in events])

    def submit_async(self, *, endpoint: str, idempotency_key: str, owner_subject: str = "anonymous", request_payload: dict[str, Any], result_field: str,
                     build_result: Callable[[str], tuple[BaseModel, str]], before_accept: Callable[[str], None] | None = None,
                     session_id: str | None = None, commit_result: Callable[[str, BaseModel], None] | None = None,
                     rollback_result: Callable[[str, BaseModel], None] | None = None) -> tuple[OperationResponse, bool]:
        request_hash = content_hash(request_payload); key_hash = self._scoped_key_hash(owner_subject, idempotency_key); operation_id = self._operation_id(endpoint, key_hash)
        operation = self._operation(operation_id=operation_id, endpoint=endpoint, idempotency_key_hash=key_hash, status="running", last_sequence=3)
        receipt = OperationResponse(operation=operation)
        _operation_id, owner, claim = self._begin(endpoint=endpoint, idempotency_key=idempotency_key, owner_subject=owner_subject, request_hash=request_hash, mode="async",
                                                   response=receipt, response_type="OperationResponse", session_id=session_id,
                                                   admission=(lambda: before_accept(operation_id)) if before_accept is not None else None)
        if not owner:
            if claim.error is not None: raise _problem(claim.error)
            if claim.response is None: claim = self.store.wait_for_terminal(endpoint, key_hash, request_hash)
            response_document = dict(claim.response or {})
            if isinstance(response_document.get("operation"), dict):
                response_document["operation"] = _operation_for_model(response_document["operation"])
            return OperationResponse.model_validate(response_document), True
        self._pending_provider_executions[operation_id] = PendingProviderExecution(endpoint, key_hash, request_hash); self._cache_operation(operation_id)

        def worker() -> None:
            try:
                try:
                    result, result_ref = build_result(operation_id)
                    committed = False
                    with self._transition_lock(operation_id):
                        current = self._cache_operation(operation_id)
                        if self.cancel_event(operation_id).is_set() or current.status == "cancelled": return
                        sequence = len(self.store.get_events(operation_id)) + 1
                        event = OperationCompleteEvent(**self._base_event(operation_id, sequence), event="operation.complete", result_ref=result_ref, result_hash=getattr(result, "content_hash", content_hash(result)))
                        updated = self._operation(operation_id=operation_id, endpoint=current.endpoint, idempotency_key_hash=current.idempotency_key_hash,
                                                  status="completed", last_sequence=sequence, created_at=current.created_at)
                        try:
                            def commit_admission() -> None:
                                nonlocal committed
                                committed = True
                                if commit_result is not None:
                                    commit_result(operation_id, result)
                            won, actual = self.store.finish(
                                operation_id=operation_id, operation=self._stored_operation(updated, owner_subject),
                                event=_dump(event), response=_dump(OperationResponse(operation=updated)), error=None,
                                admission=commit_admission if commit_result is not None else None,
                            )
                        except BaseException:
                            if committed and rollback_result is not None: rollback_result(operation_id, result)
                            raise
                        self._cache_operation(operation_id, actual)
                        if not won and committed and rollback_result is not None:
                            rollback_result(operation_id, result)
                except Exception as failure:
                    current = self._cache_operation(operation_id)
                    if current.status in {"cancelled", "completed", "failed"}: return
                    if isinstance(failure, ProviderFailure):
                        error = ApiProblem(
                            503 if failure.retryable else 500,
                            "internal_error",
                            "Provider request could not produce an accepted result.",
                            retryable=failure.retryable,
                            retry_after=15 if failure.retryable else None,
                        )
                    elif isinstance(failure, ApiProblem):
                        # Preserve typed lifecycle/idempotency semantics from
                        # terminal admission callbacks. In particular, a
                        # stale concurrent challenge mutation must surface as
                        # lifecycle_conflict instead of a synthetic 500.
                        error = failure
                    else:
                        error = ApiProblem(500, "internal_error", "Provider request could not produce an accepted result.", retryable=True)
                    record_provider_failure(endpoint=endpoint)
                    self._finish_failure(operation_id=operation_id, endpoint=endpoint, key_hash=key_hash, error=error, async_mode=True)
            finally: self._pending_provider_executions.pop(operation_id, None)
        Thread(target=worker, name=f"provider-{operation_id}", daemon=True).start(); return receipt, False

    def _control_replay(self, endpoint: str, idempotency_key: str, owner_subject: str, payload: dict[str, Any]) -> BaseModel | None:
        try:
            claim = self.store.get_claim(endpoint, self._scoped_key_hash(owner_subject, idempotency_key), content_hash(payload))
        except OperationStoreConflict as error:
            raise ApiProblem(409, "idempotency_conflict", str(error)) from error
        if claim is None: return None
        if claim.error is not None: raise _problem(claim.error)
        return OperationResponse.model_validate(claim.response) if claim.response is not None else None

    def replay_async(self, *, endpoint: str, idempotency_key: str, owner_subject: str,
                     request_payload: dict[str, Any]) -> OperationResponse | None:
        """Read an existing asynchronous receipt without revalidating provider configuration."""
        try:
            claim = self.store.get_claim(endpoint, self._scoped_key_hash(owner_subject, idempotency_key), content_hash(request_payload))
        except OperationStoreConflict as error:
            raise ApiProblem(409, "idempotency_conflict", str(error)) from error
        if claim is None:
            return None
        if claim.error is not None:
            raise _problem(claim.error)
        if claim.mode != "async" or claim.response is None:
            raise ApiProblem(500, "internal_error", "The existing operation receipt is unavailable.", retryable=True)
        response_document = dict(claim.response)
        if isinstance(response_document.get("operation"), dict):
            response_document["operation"] = _operation_for_model(response_document["operation"])
        return OperationResponse.model_validate(response_document)

    def _store_control(self, endpoint: str, idempotency_key: str, owner_subject: str, payload: dict[str, Any], response: OperationResponse) -> tuple[OperationResponse, bool]:
        key_hash = self._scoped_key_hash(owner_subject, idempotency_key); request_hash = content_hash(payload)
        try:
            existing = self.store.get_claim(endpoint, key_hash, request_hash)
        except OperationStoreConflict as error:
            raise ApiProblem(409, "idempotency_conflict", str(error)) from error
        if existing is not None:
            if existing.error is not None: raise _problem(existing.error)
            return OperationResponse.model_validate(existing.response), True
        try:
            claim = self.store.put_control(endpoint=endpoint, idempotency_key_hash=key_hash, request_hash=request_hash, response=_dump(response), response_type="OperationResponse", now=self.clock.now())
        except OperationStoreConflict as error:
            raise ApiProblem(409, "idempotency_conflict", str(error)) from error
        actual = OperationResponse.model_validate(claim.response)
        return actual, actual.model_dump(mode="json", exclude_none=True) != _dump(response)

    def cancel(self, operation_id: str, cancel_token: str, idempotency_key: str, *, owner_subject: str = "anonymous") -> tuple[OperationResponse, bool]:
        self.require_owner(operation_id, owner_subject)
        endpoint = f"/v1/operations/{operation_id}/cancel"; payload = {"cancel_token": cancel_token}; replay = self._control_replay(endpoint, idempotency_key, owner_subject, payload)
        if replay is not None: return replay, True
        with self._transition_lock(operation_id):
            operation = self._cache_operation(operation_id)
            if operation.cancel_token != cancel_token: raise ApiProblem(403, "invalid_operation_token", "Invalid cancel token.")
            if operation.status in {"pending", "running"}:
                sequence = len(self.store.get_events(operation_id)) + 1
                event = OperationErrorEvent(**self._base_event(operation_id, sequence), event="operation.error", error=ErrorResponse(schema_version="v1", error_code="bad_request", message="Operation cancelled by client command.", request_id=deterministic_id("request", operation_id), retryable=True, details=[]), retryable=True)
                updated = self._operation(operation_id=operation_id, endpoint=operation.endpoint, idempotency_key_hash=operation.idempotency_key_hash, status="cancelled", last_sequence=sequence, created_at=operation.created_at)
                cancellation = ApiProblem(500, "internal_error", "Provider request was cancelled before an accepted result.", retryable=True)
                won, actual = self.store.cancel(operation_id=operation_id, operation=self._stored_operation(updated, owner_subject), event=_dump(event), response=_dump(OperationResponse(operation=updated)), error=_error_record(cancellation)); self._cache_operation(operation_id, actual)
                # Keep the execution registered until the worker's finally block runs.
                if won:
                    self.cancel_event(operation_id).set()
                    self._notify_terminal(operation_id, "cancelled")
                operation = Operation.model_validate(_operation_for_model(actual))
        response, replayed = self._store_control(endpoint, idempotency_key, owner_subject, payload, OperationResponse(operation=operation)); return response, replayed

    def resume(self, operation_id: str, resume_token: str, idempotency_key: str, *, owner_subject: str = "anonymous") -> tuple[OperationResponse, bool]:
        self.require_owner(operation_id, owner_subject)
        endpoint = f"/v1/operations/{operation_id}/resume"; payload = {"resume_token": resume_token}; replay = self._control_replay(endpoint, idempotency_key, owner_subject, payload)
        if replay is not None: return replay, True
        operation = self._cache_operation(operation_id)
        if operation.resume_token != resume_token: raise ApiProblem(403, "invalid_operation_token", "Invalid resume token.")
        # There is no provider receipt. Failed/cancelled resume is a terminal no-op.
        return self._store_control(endpoint, idempotency_key, owner_subject, payload, OperationResponse(operation=operation))

    def events_after(self, operation_id: str, last_event_id: str | None) -> list[BaseModel]:
        operation = self._cache_operation(operation_id); events = [self._event_model(item) for item in self.store.get_events(operation_id)]
        if last_event_id is None: return events
        prefix = f"{operation_id}:"
        if not last_event_id.startswith(prefix): raise ApiProblem(400, "invalid_last_event_id", "Last-Event-ID belongs to another operation.")
        raw = last_event_id.removeprefix(prefix)
        if not raw.isdigit(): raise ApiProblem(400, "invalid_last_event_id", "Last-Event-ID sequence is invalid.")
        sequence = int(raw); max_sequence = max((event.sequence for event in events), default=int(operation.last_event_id.rsplit(":", 1)[1])); min_sequence = min((event.sequence for event in events), default=max_sequence)
        if sequence < min_sequence - 1 or sequence > max_sequence: raise ApiProblem(400, "invalid_last_event_id", "Last-Event-ID is outside the retained replay window.")
        return [event for event in events if event.sequence > sequence]

    def sse_events(self, operation_id: str, last_event_id: str | None, *, heartbeat_seconds: float = 0.1):
        cursor = last_event_id
        while True:
            batch = self.events_after(operation_id, cursor)
            if batch:
                for event in batch:
                    cursor = f"{event.operation_id}:{event.sequence}"; yield format_sse([event])
                    if event.event in {"operation.complete", "operation.error"}: return
                continue
            if self._cache_operation(operation_id).status in {"completed", "failed", "cancelled"}: return
            yield ": heartbeat\n\n"; sleep(heartbeat_seconds)

    def active_operation_ids(self) -> set[str]:
        ids = self.store.active_operation_ids()
        for operation_id in ids: self._cache_operation(operation_id)
        return ids

    def session_ids(self) -> dict[str, str]: return self.store.session_ids()

    def associate_session(self, operation_id: str, session_id: str) -> None:
        self.store.associate_session(operation_id, session_id)

    def owner_for_session(self, session_id: str) -> str:
        operation_ids = [operation_id for operation_id, retained in self.store.session_ids().items() if retained == session_id]
        if not operation_ids:
            raise ApiProblem(404, "not_found", "Session not found.")
        owners = {self.owner_subject(operation_id) for operation_id in operation_ids}
        if len(owners) != 1:
            raise ApiProblem(409, "lifecycle_conflict", "Session ownership is inconsistent.")
        return owners.pop()

    def recover_expired_leases(self, *, now: datetime | None = None) -> int:
        current = now or self.clock.now()
        def event_builder(operation: dict[str, Any], sequence: int) -> dict[str, Any]:
            return _dump(OperationErrorEvent(**self._base_event(operation["operation_id"], sequence), event="operation.error", error=ErrorResponse(schema_version="v1", error_code="internal_error", message="Worker lease expired before an accepted result.", request_id=deterministic_id("request", operation["operation_id"]), retryable=True, details=[]), retryable=True))
        def operation_builder(operation: dict[str, Any], sequence: int) -> dict[str, Any]:
            current_operation = Operation.model_validate(_operation_for_model(operation))
            updated = self._operation(operation_id=current_operation.operation_id, endpoint=current_operation.endpoint, idempotency_key_hash=current_operation.idempotency_key_hash, status="failed", last_sequence=sequence, created_at=current_operation.created_at)
            return self._stored_operation(updated, str(operation.get("owner_subject", "legacy")))
        error = _error_record(ApiProblem(500, "internal_error", "Worker lease expired before an accepted result.", retryable=True))
        recovered = self.store.recover_expired_leases(now=current, operation_builder=operation_builder, event_builder=event_builder, error=error)
        for operation in recovered:
            self._cache_operation(operation["operation_id"])
            self._pending_provider_executions.pop(operation["operation_id"], None)
            self._notify_terminal(operation["operation_id"], "failed")
        return len(recovered)

    def prune(self, *, now: datetime | None = None, max_terminal_operations: int = 1000, max_idempotency_entries: int = 5000,
              max_terminal_sse_events: int = 1000, terminal_ttl_seconds: float | None = 86400.0) -> dict[str, int]:
        self.recover_expired_leases(now=now); report = self.store.prune(now=now or self.clock.now(), max_terminal_operations=max_terminal_operations, max_idempotency_entries=max_idempotency_entries, max_terminal_sse_events=max_terminal_sse_events, terminal_ttl_seconds=terminal_ttl_seconds)
        for operation_id in tuple(self.operations):
            if self.store.get_operation(operation_id) is None: self.operations.pop(operation_id, None); self.events.pop(operation_id, None); self._cancel_events.pop(operation_id, None)
        return report


def format_sse(events: list[BaseModel]) -> str:
    chunks: list[str] = []
    for event in events:
        payload = event.model_dump(mode="json", exclude_none=True)
        chunks.extend((f"id: {event.operation_id}:{event.sequence}", f"event: {event.event}", f"data: {canonical_json(payload)}", ""))
    return "\n".join(chunks) + ("\n" if chunks else "")
