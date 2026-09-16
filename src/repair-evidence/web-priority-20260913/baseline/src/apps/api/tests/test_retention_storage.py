from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.retention import cleanup_research_state
from app.storage import InMemoryDocumentRepository


class RetentionTrackingRepository(InMemoryDocumentRepository[Any]):
    def __init__(self) -> None:
        super().__init__()
        self.retention_calls = 0

    def items(self) -> Any:
        raise AssertionError("retention must not use ordinary mapping enumeration")

    def items_for_retention(self) -> Any:
        self.retention_calls += 1
        return super().items_for_retention()


def test_cleanup_uses_retention_enumeration_and_cascades_expired_storage_documents() -> None:
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    expired_session = "session:expired"
    active_session = "session:active"

    sessions = RetentionTrackingRepository()
    sessions[expired_session] = SimpleNamespace(expires_at=now - timedelta(seconds=1))
    sessions[active_session] = SimpleNamespace(expires_at=now - timedelta(seconds=1))

    theses = RetentionTrackingRepository()
    theses["thesis:expired"] = SimpleNamespace(session_id=expired_session)
    theses["thesis:active"] = SimpleNamespace(session_id=active_session)

    challenges = RetentionTrackingRepository()
    challenges["challenge:expired"] = SimpleNamespace(thesis_id="thesis:expired")
    challenges["challenge:active"] = SimpleNamespace(thesis_id="thesis:active")

    evidence = RetentionTrackingRepository()
    evidence["evidence:expired"] = SimpleNamespace(challenge_id="challenge:expired")

    worldlines = RetentionTrackingRepository()
    worldlines["worldline:expired"] = object()

    market_manifests = RetentionTrackingRepository()
    market_manifests["manifest:expired"] = SimpleNamespace(created_at=now - timedelta(days=2))

    worldline_challenges = RetentionTrackingRepository()
    worldline_challenges["worldline:expired"] = "challenge:expired"

    challenge_turns = {"challenge:expired": 2, "challenge:active": 2}
    report = cleanup_research_state(
        sessions=sessions,
        theses=theses,
        challenges=challenges,
        challenge_turns=challenge_turns,
        evidence=evidence,
        worldlines=worldlines,
        market_manifests=market_manifests,
        worldline_challenges=worldline_challenges,
        active_session_ids={active_session},
        now=now,
    )

    assert report.expired_sessions == 1
    assert report.removed_theses == 1
    assert report.removed_challenges == 1
    assert report.removed_evidence == 1
    assert report.removed_worldlines == 1
    assert report.removed_market_manifests == 1
    assert all(repository.retention_calls == 1 for repository in (
        sessions, theses, challenges, evidence, market_manifests, worldline_challenges,
    ))
    assert worldlines.retention_calls == 0
    assert expired_session not in sessions
    assert "thesis:expired" not in theses
    assert "challenge:expired" not in challenges
    assert "challenge:expired" not in challenge_turns
    assert "evidence:expired" not in evidence
    assert "worldline:expired" not in worldlines
    assert "worldline:expired" not in worldline_challenges
    assert "manifest:expired" not in market_manifests
    assert active_session in sessions._values
    assert "thesis:active" in theses
    assert "challenge:active" in challenges
    assert "challenge:active" in challenge_turns
