from __future__ import annotations

from app.operations import OperationManager
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
