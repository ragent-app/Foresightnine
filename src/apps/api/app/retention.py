"""Bounded in-memory retention for anonymous research sessions and operations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, MutableMapping

from .telemetry import record_cleanup_failure


@dataclass(frozen=True)
class RetentionPolicy:
    session_ttl: timedelta = timedelta(hours=24)
    terminal_ttl: timedelta = timedelta(hours=24)
    max_terminal_operations: int = 1000
    max_idempotency_entries: int = 5000
    max_terminal_sse_events: int = 1000


@dataclass(frozen=True)
class CleanupReport:
    expired_sessions: int = 0
    removed_theses: int = 0
    removed_challenges: int = 0
    removed_evidence: int = 0
    removed_worldlines: int = 0
    removed_market_manifests: int = 0
    removed_terminal_operations: int = 0
    removed_idempotency_entries: int = 0
    trimmed_terminal_sse_events: int = 0
    failures: int = 0


def cleanup_research_state(
    *,
    sessions: MutableMapping[str, Any],
    theses: MutableMapping[str, Any],
    challenges: MutableMapping[str, Any],
    challenge_turns: MutableMapping[str, Any],
    evidence: MutableMapping[str, Any],
    worldlines: MutableMapping[str, Any],
    market_manifests: MutableMapping[str, Any] | None = None,
    worldline_challenges: MutableMapping[str, str] | None = None,
    active_session_ids: set[str] | None = None,
    operations: Any | None = None,
    now: datetime | None = None,
    policy: RetentionPolicy | None = None,
) -> CleanupReport:
    """Expire sessions and their descendants, then prune terminal operation state.

    All maps are mutated under the caller's process-local ownership. Active
    sessions are retained when the caller has associated running work with
    them; the operation manager independently protects pending/running state.
    """
    current = now or datetime.now(timezone.utc)
    selected = policy or RetentionPolicy()
    active = active_session_ids or set()
    counts = {
        "expired_sessions": 0, "removed_theses": 0, "removed_challenges": 0,
        "removed_evidence": 0, "removed_worldlines": 0, "removed_market_manifests": 0, "failures": 0,
    }
    try:
        expired = {
            session_id for session_id, session in sessions.items()
            if session_id not in active and getattr(session, "expires_at", current + selected.session_ttl) <= current
        }
        thesis_ids = {thesis_id for thesis_id, thesis in theses.items() if getattr(thesis, "session_id", None) in expired}
        challenge_ids = {challenge_id for challenge_id, challenge in challenges.items() if getattr(challenge, "thesis_id", None) in thesis_ids}
        evidence_ids = {evidence_id for evidence_id, item in evidence.items() if getattr(item, "challenge_id", None) in challenge_ids}
        worldline_ids = {
            worldline_id for worldline_id, challenge_id in (worldline_challenges or {}).items()
            if challenge_id in challenge_ids
        }
        for key in expired:
            sessions.pop(key, None)
        for key in thesis_ids:
            theses.pop(key, None)
        for key in challenge_ids:
            challenges.pop(key, None)
            challenge_turns.pop(key, None)
        for key in evidence_ids:
            evidence.pop(key, None)
        for key in worldline_ids:
            worldlines.pop(key, None)
            if worldline_challenges is not None:
                worldline_challenges.pop(key, None)
        manifest_ids = {
            manifest_id for manifest_id, manifest in (market_manifests or {}).items()
            if getattr(manifest, "created_at", current + selected.terminal_ttl) + selected.terminal_ttl <= current
        }
        for key in manifest_ids:
            if market_manifests is not None:
                market_manifests.pop(key, None)
        counts.update({
            "expired_sessions": len(expired), "removed_theses": len(thesis_ids),
            "removed_challenges": len(challenge_ids), "removed_evidence": len(evidence_ids),
            "removed_worldlines": len(worldline_ids), "removed_market_manifests": len(manifest_ids),
        })
    except Exception:
        counts["failures"] += 1
        record_cleanup_failure(kind="research_state")

    operation_counts = {"removed_terminal_operations": 0, "removed_idempotency_entries": 0, "trimmed_terminal_sse_events": 0}
    if operations is not None:
        try:
            operation_counts = operations.prune(
                now=current,
                max_terminal_operations=selected.max_terminal_operations,
                max_idempotency_entries=selected.max_idempotency_entries,
                max_terminal_sse_events=selected.max_terminal_sse_events,
                terminal_ttl_seconds=selected.terminal_ttl.total_seconds(),
            )
        except Exception:
            counts["failures"] += 1
            record_cleanup_failure(kind="operation_state")
    return CleanupReport(**counts, **operation_counts)
