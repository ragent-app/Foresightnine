from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from threading import Event, RLock, Thread
from time import sleep
from typing import Any, Callable

from pydantic import BaseModel

from .models import (
    ErrorDetail,
    ErrorResponse,
    HeartbeatEvent,
    Operation,
    OperationCompleteEvent,
    OperationErrorEvent,
    OperationProgressEvent,
    OperationResponse,
    OperationStartedEvent,
    SourceRef,
)
from .phase3 import ProviderFailure
from .providers import FixedClock
from .services import canonical_json, content_hash, deterministic_id, validate_payload


class ApiProblem(RuntimeError):
    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        *,
        retryable: bool = False,
        details: list[ErrorDetail] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.retryable = retryable
        self.details = details or []


@dataclass(frozen=True)
class IdempotencyEntry:
    request_hash: str
    response: BaseModel | ApiProblem


@dataclass(frozen=True)
class PendingProviderExecution:
    endpoint: str
    idempotency_key: str
    request_hash: str


@dataclass(frozen=True)
class SingleFlight:
    request_hash: str
    operation_id: str
    completed: Event


class OperationManager:
    def __init__(self, clock: FixedClock | None = None) -> None:
        self.clock = clock or FixedClock()
        self._idempotency: dict[tuple[str, str], IdempotencyEntry] = {}
        self._single_flight: dict[tuple[str, str], SingleFlight] = {}
        self._pending_provider_executions: dict[str, PendingProviderExecution] = {}
        self._cancel_events: dict[str, Event] = {}
        self.operations: dict[str, Operation] = {}
        self.events: dict[str, list[BaseModel]] = {}
        self._lock = RLock()

    def reset(self) -> None:
        with self._lock:
            self._idempotency.clear()
            self._single_flight.clear()
            self._pending_provider_executions.clear()
            self._cancel_events.clear()
            self.operations.clear()
            self.events.clear()

    def _operation(
        self,
        *,
        operation_id: str,
        endpoint: str,
        idempotency_key: str,
        status: str,
        last_sequence: int,
    ) -> Operation:
        refs = (SourceRef(ref_id="mock:operation", source_type="model_inference", label="Deterministic operation controller"),)
        payload = {
            "operation_id": operation_id,
            "endpoint": endpoint,
            "status": status,
            "updated_at": self.clock.now(),
            "idempotency_key_hash": sha256(idempotency_key.encode("utf-8")).hexdigest(),
            "cancel_token": sha256(f"cancel\0{operation_id}".encode("utf-8")).hexdigest(),
            "resume_token": sha256(f"resume\0{operation_id}".encode("utf-8")).hexdigest(),
            "last_event_id": f"{operation_id}:{last_sequence}",
        }
        metadata_input = {
            "schema_version": "v1",
            "source_refs": [ref.model_dump(mode="json", exclude_none=True) for ref in refs],
            **payload,
        }
        operation = Operation(
            schema_version="v1",
            created_at=self.clock.now(),
            source_refs=refs,
            content_hash=content_hash(metadata_input),
            **payload,
        )
        validate_payload(operation, "Operation")
        return operation

    def _base_event(self, operation_id: str, sequence: int) -> dict[str, Any]:
        return {"schema_version": "v1", "operation_id": operation_id, "sequence": sequence, "emitted_at": self.clock.now()}

    def cancel_event(self, operation_id: str) -> Event:
        """Return the event registered before provider work begins."""
        with self._lock:
            try:
                return self._cancel_events[operation_id]
            except KeyError as error:
                raise RuntimeError("Provider attempted work before its operation was registered.") from error

    def execute(
        self,
        *,
        endpoint: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
        response_model: type[BaseModel],
        result_field: str,
        build_result: Callable[[str], tuple[BaseModel, str]],
    ) -> tuple[BaseModel, bool]:
        request_hash = content_hash(request_payload)
        key = (endpoint, idempotency_key)
        with self._lock:
            existing = self._idempotency.get(key)
            if existing:
                if existing.request_hash != request_hash:
                    raise ApiProblem(409, "idempotency_conflict", "Idempotency-Key was already used with a different request body.")
                if isinstance(existing.response, ApiProblem):
                    raise existing.response
                return existing.response, True
            inflight = self._single_flight.get(key)
            if inflight and inflight.request_hash != request_hash:
                raise ApiProblem(409, "idempotency_conflict", "Idempotency-Key was already used with a different request body.")
            if inflight is None:
                operation_id = deterministic_id("operation", f"{endpoint}\0{idempotency_key}")
                inflight = SingleFlight(request_hash=request_hash, operation_id=operation_id, completed=Event())
                self._single_flight[key] = inflight
                owner = True
            else:
                owner = False
        if not owner:
            # The first submission owns provider work. Waiting callers replay its
            # terminal response (including its typed failure), never submit again.
            inflight.completed.wait()
            with self._lock:
                existing = self._idempotency.get(key)
                if existing is None:
                    raise ApiProblem(500, "internal_error", "Operation did not reach an idempotent terminal checkpoint.", retryable=True)
                if isinstance(existing.response, ApiProblem):
                    raise existing.response
                return existing.response, True

        # Register the real running operation and its cancellation primitive
        # before build_result can submit provider work.
        event_log: list[BaseModel] = [
            OperationStartedEvent(**self._base_event(operation_id, 1), event="operation.started", message="Operation accepted."),
            OperationProgressEvent(**self._base_event(operation_id, 2), event="operation.progress", progress=0.0, message="Operation is awaiting provider output."),
            HeartbeatEvent(**self._base_event(operation_id, 3), event="heartbeat"),
        ]
        with self._lock:
            self.events[operation_id] = event_log
            self.operations[operation_id] = self._operation(
                operation_id=operation_id, endpoint=endpoint, idempotency_key=idempotency_key, status="running", last_sequence=3,
            )
            self._cancel_events[operation_id] = Event()
            self._pending_provider_executions[operation_id] = PendingProviderExecution(
                endpoint=endpoint, idempotency_key=idempotency_key, request_hash=request_hash,
            )
        try:
            result, result_ref = build_result(operation_id)
        except ProviderFailure as error:
            failure = ApiProblem(500, "internal_error", "Provider request could not produce an accepted result.", retryable=error.retryable)
            with self._lock:
                if self.operations[operation_id].status != "cancelled":
                    self.events[operation_id].append(OperationErrorEvent(
                        **self._base_event(operation_id, len(self.events[operation_id]) + 1), event="operation.error",
                        error=ErrorResponse(schema_version="v1", error_code="internal_error", message="Provider result was not accepted.", request_id=deterministic_id("request", operation_id), retryable=error.retryable, details=[]),
                        retryable=error.retryable,
                    ))
                    self.operations[operation_id] = self._operation(
                        operation_id=operation_id, endpoint=endpoint, idempotency_key=idempotency_key, status="failed", last_sequence=len(self.events[operation_id]),
                    )
                self._idempotency[key] = IdempotencyEntry(request_hash=request_hash, response=failure)
                self._pending_provider_executions.pop(operation_id, None)
                self._single_flight.pop(key, None)
                inflight.completed.set()
            raise failure from error
        except ApiProblem as error:
            with self._lock:
                if self.operations[operation_id].status != "cancelled":
                    sequence = len(self.events[operation_id]) + 1
                    self.events[operation_id].append(OperationErrorEvent(
                        **self._base_event(operation_id, sequence), event="operation.error",
                        error=ErrorResponse(
                            schema_version="v1", error_code=error.error_code, message=error.message,
                            request_id=deterministic_id("request", operation_id), retryable=error.retryable, details=error.details,
                        ),
                        retryable=error.retryable,
                    ))
                    self.operations[operation_id] = self._operation(
                        operation_id=operation_id, endpoint=endpoint, idempotency_key=idempotency_key, status="failed", last_sequence=sequence,
                    )
                self._idempotency[key] = IdempotencyEntry(request_hash=request_hash, response=error)
                self._pending_provider_executions.pop(operation_id, None)
                self._single_flight.pop(key, None)
                inflight.completed.set()
            raise
        with self._lock:
            cancelled = self._cancel_events[operation_id].is_set() or self.operations[operation_id].status == "cancelled"
        if cancelled:
            failure = ApiProblem(500, "internal_error", "Provider request was cancelled before an accepted result.", retryable=True)
            with self._lock:
                self._idempotency[key] = IdempotencyEntry(request_hash=request_hash, response=failure)
                self._pending_provider_executions.pop(operation_id, None)
                self._single_flight.pop(key, None)
                inflight.completed.set()
            raise failure
        result_hash = getattr(result, "content_hash", content_hash(result))
        event_log.append(OperationCompleteEvent(**self._base_event(operation_id, 4), event="operation.complete", result_ref=result_ref, result_hash=result_hash))
        validate_payload(event_log[-1], "OperationCompleteEvent")
        operation = self._operation(
            operation_id=operation_id,
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            status="completed",
            last_sequence=4,
        )
        response = response_model(operation=operation, **{result_field: result})
        with self._lock:
            self.operations[operation_id] = operation
            self._idempotency[key] = IdempotencyEntry(request_hash=request_hash, response=response)
            self._pending_provider_executions.pop(operation_id, None)
            self._single_flight.pop(key, None)
            inflight.completed.set()
        return response, False

    def start(self, *, endpoint: str, idempotency_key: str) -> Operation:
        """Register a durable running operation for asynchronous adapters and cancellation tests."""
        operation_id = deterministic_id("operation", f"{endpoint}\0{idempotency_key}")
        event_log: list[BaseModel] = [
            OperationStartedEvent(**self._base_event(operation_id, 1), event="operation.started", message="Operation accepted."),
            OperationProgressEvent(**self._base_event(operation_id, 2), event="operation.progress", progress=0.0, message="Operation is awaiting provider output."),
            HeartbeatEvent(**self._base_event(operation_id, 3), event="heartbeat"),
        ]
        operation = self._operation(
            operation_id=operation_id,
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            status="running",
            last_sequence=3,
        )
        self.operations[operation_id] = operation
        self.events[operation_id] = event_log
        self._cancel_events[operation_id] = Event()
        return operation

    def submit_async(
        self,
        *,
        endpoint: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
        result_field: str,
        build_result: Callable[[str], tuple[BaseModel, str]],
    ) -> tuple[OperationResponse, bool]:
        """Accept provider work now and run it after returning a typed operation.

        This is intentionally a separate opt-in path: existing synchronous POST
        contracts keep their response shape and timing.  The operation, replay
        log and cancellation Event exist before the worker thread is started.
        """
        request_hash = content_hash(request_payload)
        key = (endpoint, idempotency_key)
        with self._lock:
            existing = self._idempotency.get(key)
            if existing:
                if existing.request_hash != request_hash:
                    raise ApiProblem(409, "idempotency_conflict", "Idempotency-Key was already used with a different request body.")
                if isinstance(existing.response, ApiProblem):
                    raise existing.response
                if not isinstance(existing.response, OperationResponse):
                    raise ApiProblem(409, "idempotency_conflict", "Idempotency-Key belongs to a different operation response.")
                return existing.response, True
            operation_id = deterministic_id("operation", f"{endpoint}\0{idempotency_key}")
            event_log: list[BaseModel] = [
                OperationStartedEvent(**self._base_event(operation_id, 1), event="operation.started", message="Operation accepted."),
                OperationProgressEvent(**self._base_event(operation_id, 2), event="operation.progress", progress=0.0, message="Operation is awaiting provider output."),
                HeartbeatEvent(**self._base_event(operation_id, 3), event="heartbeat"),
            ]
            operation = self._operation(
                operation_id=operation_id, endpoint=endpoint, idempotency_key=idempotency_key, status="running", last_sequence=3,
            )
            response = OperationResponse(operation=operation)
            self.events[operation_id] = event_log
            self.operations[operation_id] = operation
            self._cancel_events[operation_id] = Event()
            self._pending_provider_executions[operation_id] = PendingProviderExecution(endpoint, idempotency_key, request_hash)
            # The first response deliberately remains a running receipt. Clients
            # consume the durable event stream for live status rather than racing
            # a second provider submission for a newer body.
            self._idempotency[key] = IdempotencyEntry(request_hash, response)

        def worker() -> None:
            try:
                result, result_ref = build_result(operation_id)
                with self._lock:
                    if self._cancel_events[operation_id].is_set() or self.operations[operation_id].status == "cancelled":
                        return
                    sequence = len(self.events[operation_id]) + 1
                    result_hash = getattr(result, "content_hash", content_hash(result))
                    self.events[operation_id].append(OperationCompleteEvent(
                        **self._base_event(operation_id, sequence), event="operation.complete", result_ref=result_ref, result_hash=result_hash,
                    ))
                    self.operations[operation_id] = self._operation(
                        operation_id=operation_id, endpoint=endpoint, idempotency_key=idempotency_key, status="completed", last_sequence=sequence,
                    )
                    self._idempotency[key] = IdempotencyEntry(request_hash, OperationResponse(operation=self.operations[operation_id]))
            except Exception:
                # Async workers have no caller awaiting their exception.  Every
                # unexpected failure must therefore become the same safe terminal
                # record as a provider failure, without exposing exception text.
                with self._lock:
                    if self.operations[operation_id].status != "cancelled":
                        sequence = len(self.events[operation_id]) + 1
                        event = OperationErrorEvent(
                            **self._base_event(operation_id, sequence), event="operation.error",
                            error=ErrorResponse(schema_version="v1", error_code="internal_error", message="Provider result was not accepted.", request_id=deterministic_id("request", operation_id), retryable=True, details=[]),
                            retryable=True,
                        )
                        validate_payload(event, "OperationErrorEvent")
                        self.events[operation_id].append(event)
                        self.operations[operation_id] = self._operation(
                            operation_id=operation_id, endpoint=endpoint, idempotency_key=idempotency_key, status="failed", last_sequence=sequence,
                        )
                        self._idempotency[key] = IdempotencyEntry(request_hash, OperationResponse(operation=self.operations[operation_id]))
            finally:
                with self._lock:
                    self._pending_provider_executions.pop(operation_id, None)

        Thread(target=worker, name=f"provider-{operation_id}", daemon=True).start()
        return response, False

    def _control_replay(self, endpoint: str, idempotency_key: str, payload: dict[str, Any]) -> BaseModel | None:
        with self._lock:
            request_hash = content_hash(payload)
            existing = self._idempotency.get((endpoint, idempotency_key))
            if not existing:
                return None
            if existing.request_hash != request_hash:
                raise ApiProblem(409, "idempotency_conflict", "Idempotency-Key was already used with a different control request.")
            return existing.response

    def _store_control(self, endpoint: str, idempotency_key: str, payload: dict[str, Any], response: BaseModel) -> None:
        with self._lock:
            self._idempotency[(endpoint, idempotency_key)] = IdempotencyEntry(content_hash(payload), response)

    def cancel(self, operation_id: str, cancel_token: str, idempotency_key: str) -> tuple[OperationResponse, bool]:
        endpoint = f"/v1/operations/{operation_id}/cancel"
        payload = {"cancel_token": cancel_token}
        replay = self._control_replay(endpoint, idempotency_key, payload)
        if replay:
            return replay, True
        with self._lock:
            operation = self.operations.get(operation_id)
            if not operation:
                raise ApiProblem(404, "not_found", "Operation not found.")
            if operation.cancel_token != cancel_token:
                raise ApiProblem(403, "invalid_operation_token", "Invalid cancel token.")
            if operation.status in ("pending", "running"):
                self._cancel_events[operation_id].set()
                pending = self._pending_provider_executions.get(operation_id)
                sequence = len(self.events[operation_id]) + 1
                error = ErrorResponse(
                    schema_version="v1", error_code="bad_request", message="Operation cancelled by client command.",
                    request_id=deterministic_id("request", operation_id), retryable=True, details=[],
                )
                event = OperationErrorEvent(**self._base_event(operation_id, sequence), event="operation.error", error=error, retryable=True)
                validate_payload(event, "OperationErrorEvent")
                self.events[operation_id].append(event)
                operation = self._operation(
                    operation_id=operation_id, endpoint=operation.endpoint,
                    idempotency_key=pending.idempotency_key if pending else idempotency_key, status="cancelled", last_sequence=sequence,
                )
                self.operations[operation_id] = operation
                if pending:
                    self._idempotency[(pending.endpoint, pending.idempotency_key)] = IdempotencyEntry(
                        pending.request_hash, OperationResponse(operation=operation),
                    )
        response = OperationResponse(operation=operation)
        self._store_control(endpoint, idempotency_key, payload, response)
        return response, False

    def resume(self, operation_id: str, resume_token: str, idempotency_key: str) -> tuple[OperationResponse, bool]:
        endpoint = f"/v1/operations/{operation_id}/resume"
        payload = {"resume_token": resume_token}
        replay = self._control_replay(endpoint, idempotency_key, payload)
        if replay:
            return replay, True
        operation = self.operations.get(operation_id)
        if not operation:
            raise ApiProblem(404, "not_found", "Operation not found.")
        if operation.resume_token != resume_token:
            raise ApiProblem(403, "invalid_operation_token", "Invalid resume token.")
        if operation.status in {"failed", "cancelled"}:
            # There is no provider-side charge/idempotency receipt exposed by the
            # OpenCode CLI. Resuming must therefore preserve the terminal
            # checkpoint, never invoke build_result again, and never create a
            # second billable submission. A user may explicitly start a new
            # command with a new idempotency key after reviewing this record.
            response = OperationResponse(operation=operation)
            self._store_control(endpoint, idempotency_key, payload, response)
            return response, False
        if operation.status == "cancelled":
            sequence = len(self.events[operation_id]) + 1
            resumed_events: list[BaseModel] = [
                OperationProgressEvent(**self._base_event(operation_id, sequence), event="operation.progress", progress=0.0, message="Operation resumed from its retained checkpoint."),
                HeartbeatEvent(**self._base_event(operation_id, sequence + 1), event="heartbeat"),
                OperationCompleteEvent(
                    **self._base_event(operation_id, sequence + 2),
                    event="operation.complete",
                    result_ref=operation_id,
                    result_hash=operation.content_hash,
                ),
            ]
            self.events[operation_id].extend(resumed_events)
            operation = self._operation(
                operation_id=operation_id,
                endpoint=operation.endpoint,
                idempotency_key=idempotency_key,
                status="completed",
                last_sequence=sequence + 2,
            )
            self.operations[operation_id] = operation
        response = OperationResponse(operation=operation)
        self._store_control(endpoint, idempotency_key, payload, response)
        return response, False

    def events_after(self, operation_id: str, last_event_id: str | None) -> list[BaseModel]:
        with self._lock:
            events = self.events.get(operation_id)
            if events is None:
                raise ApiProblem(404, "not_found", "Operation not found.")
            if last_event_id is None:
                return list(events)
            prefix = f"{operation_id}:"
            if not last_event_id.startswith(prefix):
                raise ApiProblem(400, "invalid_last_event_id", "Last-Event-ID belongs to another operation.")
            raw_sequence = last_event_id.removeprefix(prefix)
            if not raw_sequence.isdigit():
                raise ApiProblem(400, "invalid_last_event_id", "Last-Event-ID sequence is invalid.")
            sequence = int(raw_sequence)
            if sequence < 0 or sequence > len(events):
                raise ApiProblem(400, "invalid_last_event_id", "Last-Event-ID is outside the retained replay window.")
            return [event for event in events if event.sequence > sequence]

    def sse_events(self, operation_id: str, last_event_id: str | None, *, heartbeat_seconds: float = 0.1):
        """Yield replay then live SSE frames until a terminal event is observed."""
        cursor = last_event_id
        while True:
            batch = self.events_after(operation_id, cursor)
            if batch:
                for event in batch:
                    cursor = f"{event.operation_id}:{event.sequence}"
                    yield format_sse([event])
                    if event.event in {"operation.complete", "operation.error"}:
                        return
                continue
            with self._lock:
                operation = self.operations[operation_id]
                terminal = operation.status in {"completed", "failed", "cancelled"}
            if terminal:
                return
            # A standards-compliant SSE comment keeps intermediaries and clients
            # alive without creating an unbounded audited-event log per viewer.
            yield ": heartbeat\n\n"
            sleep(heartbeat_seconds)


def format_sse(events: list[BaseModel]) -> str:
    chunks: list[str] = []
    for event in events:
        payload = event.model_dump(mode="json", exclude_none=True)
        chunks.extend((
            f"id: {event.operation_id}:{event.sequence}",
            f"event: {event.event}",
            f"data: {canonical_json(payload)}",
            "",
        ))
    return "\n".join(chunks) + ("\n" if chunks else "")
