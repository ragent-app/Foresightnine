from __future__ import annotations

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
