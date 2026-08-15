from __future__ import annotations

from threading import Event, Thread
import pytest
from pydantic import BaseModel, create_model

from app.operations import ApiProblem
from app.operations import OperationManager
from app.phase3 import ProviderFailure
from app.providers import FixedClock


def test_running_operation_cancel_resume_and_replay_sequence() -> None:
    manager = OperationManager(FixedClock())
    running = manager.start(endpoint="/v1/worldlines", idempotency_key="async-running-key")
    assert running.status == "running"
    cancelled, replayed = manager.cancel(running.operation_id, running.cancel_token, "cancel-running-key")
    assert replayed is False
    assert cancelled.operation.status == "cancelled"
    assert manager.events[running.operation_id][-1].event == "operation.error"
    resumed, replayed = manager.resume(running.operation_id, running.resume_token, "resume-running-key")
    assert replayed is False
    assert resumed.operation.status == "completed"
    assert manager.events[running.operation_id][-2].event == "heartbeat"
    assert manager.events[running.operation_id][-1].event == "operation.complete"
    assert [event.sequence for event in manager.events_after(running.operation_id, f"{running.operation_id}:4")] == [5, 6, 7]


def test_provider_failure_is_replayed_without_resubmission_then_explicit_resume_reuses_operation() -> None:
    manager = OperationManager(FixedClock())
    payload_type = create_model("ProviderPayload", content_hash=(str, ...))
    response_type = create_model("ProviderResponse", operation=(object, ...), value=(object, ...))
    attempts = 0

    def build(operation_id: str) -> tuple[BaseModel, str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProviderFailure("provider_timeout", "Provider timed out", retryable=True)
        return payload_type(content_hash="a" * 64), "result:resumed"

    with pytest.raises(ApiProblem) as failed:
        manager.execute(
            endpoint="/v1/challenges", idempotency_key="provider-recovery-key",
            request_payload={"message": "same command"}, response_model=response_type,
            result_field="value", build_result=build,
        )
    assert failed.value.retryable is True and attempts == 1
    operation = next(iter(manager.operations.values()))
    assert operation.status == "failed"
    assert [event.event for event in manager.events[operation.operation_id]] == ["operation.started", "operation.progress", "heartbeat", "operation.error"]

    with pytest.raises(ApiProblem):
        manager.execute(
            endpoint="/v1/challenges", idempotency_key="provider-recovery-key",
            request_payload={"message": "same command"}, response_model=response_type,
            result_field="value", build_result=build,
        )
    assert attempts == 1

    resumed, replayed = manager.resume(operation.operation_id, operation.resume_token, "provider-resume-key")
    assert replayed is False and resumed.operation.status == "completed" and attempts == 2
    assert [event.sequence for event in manager.events_after(operation.operation_id, f"{operation.operation_id}:4")] == [5, 6, 7]
    response, replayed = manager.execute(
        endpoint="/v1/challenges", idempotency_key="provider-recovery-key",
        request_payload={"message": "same command"}, response_model=response_type,
        result_field="value", build_result=build,
    )
    assert replayed is True and response.value.content_hash == "a" * 64 and attempts == 2


def test_execute_registers_live_cancel_event_before_provider_and_resume_retries_once() -> None:
    manager = OperationManager(FixedClock())
    payload_type = create_model("CancelledPayload", content_hash=(str, ...))
    response_type = create_model("CancelledResponse", operation=(object, ...), value=(object, ...))
    provider_started = Event()
    provider_saw_cancel = Event()
    attempts = 0
    errors: list[ApiProblem] = []

    def build(operation_id: str) -> tuple[BaseModel, str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            provider_started.set()
            assert manager.cancel_event(operation_id).wait(1)
            provider_saw_cancel.set()
            raise ProviderFailure("provider_cancelled", "cancelled by operation", retryable=True)
        return payload_type(content_hash="c" * 64), "result:after-cancel"

    def execute() -> None:
        try:
            manager.execute(
                endpoint="/v1/challenges", idempotency_key="live-cancel-provider-key",
                request_payload={"message": "cancel me"}, response_model=response_type,
                result_field="value", build_result=build,
            )
        except ApiProblem as error:
            errors.append(error)

    worker = Thread(target=execute)
    worker.start()
    assert provider_started.wait(1)
    operation = next(iter(manager.operations.values()))
    cancelled, replayed = manager.cancel(operation.operation_id, operation.cancel_token, "live-cancel-command-key")
    worker.join(1)
    assert not worker.is_alive() and replayed is False
    assert cancelled.operation.status == "cancelled" and provider_saw_cancel.is_set() and attempts == 1
    assert errors and errors[0].retryable is True
    assert [event.event for event in manager.events[operation.operation_id]] == ["operation.started", "operation.progress", "heartbeat", "operation.error"]

    with pytest.raises(ApiProblem):
        manager.execute(
            endpoint="/v1/challenges", idempotency_key="live-cancel-provider-key",
            request_payload={"message": "cancel me"}, response_model=response_type,
            result_field="value", build_result=build,
        )
    assert attempts == 1
    resumed, _ = manager.resume(operation.operation_id, operation.resume_token, "live-cancel-resume-key")
    assert resumed.operation.status == "completed" and attempts == 2
