from __future__ import annotations

from threading import Barrier, Event, Thread
from time import sleep
import pytest
from pydantic import BaseModel, create_model

from app.operations import ApiProblem
from app.operations import OperationManager
from app.phase3 import ProviderFailure
from app.providers import FixedClock


def test_running_operation_cancel_resume_preserves_terminal_checkpoint_without_synthetic_complete() -> None:
    manager = OperationManager(FixedClock())
    running = manager.start(endpoint="/v1/worldlines", idempotency_key="async-running-key")
    assert running.status == "running"
    cancelled, replayed = manager.cancel(running.operation_id, running.cancel_token, "cancel-running-key")
    assert replayed is False
    assert cancelled.operation.status == "cancelled"
    assert manager.events[running.operation_id][-1].event == "operation.error"
    resumed, replayed = manager.resume(running.operation_id, running.resume_token, "resume-running-key")
    assert replayed is False
    assert resumed.operation.status == "cancelled"
    assert manager.events[running.operation_id][-1].event == "operation.error"
    assert manager.events_after(running.operation_id, f"{running.operation_id}:4") == []


def test_provider_failure_and_resume_never_resubmit_without_provider_charge_receipt() -> None:
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
    assert replayed is False and resumed.operation.status == "failed" and attempts == 1
    assert [event.sequence for event in manager.events_after(operation.operation_id, f"{operation.operation_id}:4")] == []


def test_execute_registers_live_cancel_event_and_resume_never_retries_submission() -> None:
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
    assert resumed.operation.status == "cancelled" and attempts == 1


def test_build_api_problem_transitions_running_operation_to_failed_and_replays_error() -> None:
    manager = OperationManager(FixedClock())
    response_type = create_model("ApiProblemResponse", operation=(object, ...), value=(object, ...))

    def build(operation_id: str) -> tuple[BaseModel, str]:
        raise ApiProblem(404, "not_found", "Challenge disappeared.")

    with pytest.raises(ApiProblem) as raised:
        manager.execute(
            endpoint="/v1/challenges", idempotency_key="api-problem-key",
            request_payload={"message": "missing"}, response_model=response_type,
            result_field="value", build_result=build,
        )
    operation = next(iter(manager.operations.values()))
    assert raised.value.status_code == 404 and operation.status == "failed"
    assert manager.events[operation.operation_id][-1].event == "operation.error"
    with pytest.raises(ApiProblem) as replayed:
        manager.execute(
            endpoint="/v1/challenges", idempotency_key="api-problem-key",
            request_payload={"message": "missing"}, response_model=response_type,
            result_field="value", build_result=build,
        )
    assert replayed.value.status_code == 404


def test_execute_is_atomic_single_flight_under_same_idempotency_key() -> None:
    manager = OperationManager(FixedClock())
    payload_type = create_model("SingleFlightPayload", content_hash=(str, ...))
    response_type = create_model("SingleFlightResponse", operation=(object, ...), value=(object, ...))
    callers = Barrier(3)
    provider_started = Event()
    release_provider = Event()
    submissions = 0
    results: list[object] = []

    def build(operation_id: str) -> tuple[BaseModel, str]:
        nonlocal submissions
        submissions += 1
        provider_started.set()
        assert release_provider.wait(1)
        return payload_type(content_hash="d" * 64), "result:single-flight"

    def invoke() -> None:
        callers.wait()
        results.append(manager.execute(
            endpoint="/v1/challenges", idempotency_key="single-flight-key",
            request_payload={"message": "same"}, response_model=response_type,
            result_field="value", build_result=build,
        ))

    workers = [Thread(target=invoke) for _ in range(2)]
    for worker in workers:
        worker.start()
    callers.wait()
    assert provider_started.wait(1)
    release_provider.set()
    for worker in workers:
        worker.join(1)
        assert not worker.is_alive()
    assert submissions == 1
    assert len(results) == 2
    assert len({result[0].operation.operation_id for result in results}) == 1
    assert sorted(result[1] for result in results) == [False, True]


def test_async_sse_emits_before_provider_completion_and_cancel_reaches_runner() -> None:
    manager = OperationManager(FixedClock())
    runner_started = Event()
    runner_cancelled = Event()

    def build(operation_id: str) -> tuple[BaseModel, str]:
        runner_started.set()
        assert manager.cancel_event(operation_id).wait(1)
        runner_cancelled.set()
        raise ProviderFailure("provider_cancelled", "cancelled", retryable=True)

    receipt, replayed = manager.submit_async(
        endpoint="/v1/challenges/challenge:stream/messages/async", idempotency_key="streaming-provider-key",
        request_payload={"message": "block"}, result_field="challenge", build_result=build,
    )
    assert replayed is False and runner_started.wait(1)
    stream = manager.sse_events(receipt.operation.operation_id, None, heartbeat_seconds=0.001)
    assert "event: operation.started" in next(stream)
    assert "event: operation.progress" in next(stream)
    assert "event: heartbeat" in next(stream)
    assert runner_cancelled.is_set() is False
    cancelled, _ = manager.cancel(receipt.operation.operation_id, receipt.operation.cancel_token, "cancel-streaming-provider-key")
    assert cancelled.operation.status == "cancelled" and runner_cancelled.wait(1)
    assert "event: operation.error" in next(stream)


@pytest.mark.parametrize("failure", [False, True])
def test_async_submit_replay_updates_from_running_to_terminal_operation(failure: bool) -> None:
    manager = OperationManager(FixedClock())
    payload_type = create_model("AsyncReplayPayload", content_hash=(str, ...))

    def build(operation_id: str) -> tuple[BaseModel, str]:
        if failure:
            raise ProviderFailure("provider_timeout", "timed out", retryable=True)
        return payload_type(content_hash="e" * 64), "result:async-replay"

    receipt, replayed = manager.submit_async(
        endpoint="/v1/challenges/challenge:async/messages/async", idempotency_key=f"async-replay-{failure}",
        request_payload={"message": "same"}, result_field="challenge", build_result=build,
    )
    assert replayed is False and receipt.operation.status == "running"
    for _ in range(100):
        if manager.operations[receipt.operation.operation_id].status != "running":
            break
        sleep(0.01)
    expected_status = "failed" if failure else "completed"
    assert manager.operations[receipt.operation.operation_id].status == expected_status
    replay, replayed = manager.submit_async(
        endpoint="/v1/challenges/challenge:async/messages/async", idempotency_key=f"async-replay-{failure}",
        request_payload={"message": "same"}, result_field="challenge",
        build_result=lambda operation_id: (_ for _ in ()).throw(AssertionError("replay must not submit provider work")),
    )
    assert replayed is True and replay.operation.status == expected_status
