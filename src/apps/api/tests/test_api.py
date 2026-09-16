from __future__ import annotations

import json
from threading import Event

import app.main as main
from app.main import CHALLENGES, CHALLENGE_TURNS
from app.phase3 import ProviderFailure

from .helpers import CLIENT, assert_contract, confirm_standard_flow, create_challenge, create_session, create_worldlines, idem


def test_health_and_strict_error_envelope() -> None:
    response = CLIENT.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "provider": "deterministic_mock",
        "search_provider": "client-evidence-bundle",
        "project_search_available": False,
        "schema_version": "v1",
        "clock": "2026-08-10T00:00:00Z",
    }
    assert_contract(response.json(), "HealthResponse")

    invalid = CLIENT.post("/v1/sessions", json={"unknown": True}, headers=idem("invalid-session-key"))
    assert invalid.status_code == 422
    assert invalid.json()["error_code"] == "validation_error"
    assert_contract(invalid.json(), "ErrorResponse")


def test_idempotency_replays_exact_result_and_rejects_conflicting_body() -> None:
    first = CLIENT.post("/v1/sessions", json={}, headers=idem("same-session-key"))
    second = CLIENT.post("/v1/sessions", json={}, headers=idem("same-session-key"))
    assert first.json() == second.json()
    assert first.headers["Idempotency-Replayed"] == "false"
    assert second.headers["Idempotency-Replayed"] == "true"
    conflict = CLIENT.post("/v1/sessions", json={"client_locale": "zh-CN"}, headers=idem("same-session-key"))
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "idempotency_conflict"

    challenge = create_challenge(key="replay-state-challenge")
    challenge_id = challenge["challenge"]["challenge_id"]
    body = {"message": "A retry must not advance the durable state twice."}
    first_message = CLIENT.post(f"/v1/challenges/{challenge_id}/messages", json=body, headers=idem("state-message-key"))
    replayed_message = CLIENT.post(f"/v1/challenges/{challenge_id}/messages", json=body, headers=idem("state-message-key"))
    assert first_message.status_code == replayed_message.status_code == 200
    assert first_message.json() == replayed_message.json()
    assert replayed_message.headers["Idempotency-Replayed"] == "true"


def _fail_first_terminal_after_admission(monkeypatch):
    original_finish = main.operations.store.finish
    failed = Event()

    def fail_first(*args, **kwargs):
        if not failed.is_set():
            failed.set()
            admission = kwargs.get("admission")
            if admission is not None:
                admission()
            raise RuntimeError("injected terminal persistence failure")
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(main.operations.store, "finish", fail_first)
    return failed


def test_confirmation_terminal_failure_restores_challenge_state(monkeypatch) -> None:
    created = create_challenge(key="atomic-confirm-challenge")
    challenge_id = created["challenge"]["challenge_id"]
    before = CHALLENGES[challenge_id]
    before_turn = CHALLENGE_TURNS[challenge_id]
    failed = _fail_first_terminal_after_admission(monkeypatch)

    response = CLIENT.post(
        f"/v1/challenges/{challenge_id}/confirm",
        json={"command": "CONFIRM_WITH_RESERVATIONS", "unresolved_issues_acknowledged": True, "reservation_note": "Retain unresolved evidence gaps."},
        headers=idem("atomic-confirm-command"),
    )

    assert response.status_code == 500
    assert failed.is_set()
    assert CHALLENGES[challenge_id] == before
    assert CHALLENGE_TURNS[challenge_id] == before_turn


def test_worldline_terminal_failure_removes_staged_worldlines(monkeypatch) -> None:
    _, confirmed = confirm_standard_flow()
    challenge_id = confirmed["challenge"]["challenge_id"]
    failed = _fail_first_terminal_after_admission(monkeypatch)

    response = CLIENT.post(
        "/v1/worldlines",
        json={"challenge_id": challenge_id, "horizon_end": "2027-08-10"},
        headers=idem("atomic-worldline-command"),
    )

    assert response.status_code == 500
    assert failed.is_set()
    assert list(main.WORLDLINES) == []
    assert list(main.WORLDLINE_CHALLENGES) == []


def test_normal_state_path_worldlines_probability_and_get() -> None:
    _, confirmed = confirm_standard_flow()
    assert confirmed["challenge"]["status"] == "CONFIRMED"
    assert confirmed["challenge"]["user_confirmation"] == "confirmed"
    assert_contract(confirmed, "ChallengeOperationResponse")

    challenge_id = confirmed["challenge"]["challenge_id"]
    worldline_response = CLIENT.post(
        "/v1/worldlines",
        json={"challenge_id": challenge_id, "horizon_end": "2027-08-10"},
        headers=idem("worldline-path-key"),
    )
    assert worldline_response.status_code == 201, worldline_response.text
    worldline_payload = worldline_response.json()
    assert_contract(worldline_payload, "WorldlineBundleOperationResponse")
    assert {item["branch_kind"] for item in worldline_payload["bundle"]["worldlines"]} == {"weak", "main", "strong"}

    worldline = worldline_payload["bundle"]["worldlines"][1]
    fetched = CLIENT.get(f"/v1/worldlines/{worldline['worldline_id']}")
    assert fetched.status_code == 200
    assert fetched.json() == worldline

    probability = CLIENT.post(
        "/v1/probabilities/assess",
        json={"worldline_id": worldline["worldline_id"]},
        headers=idem("probability-key-0001"),
    )
    assert probability.status_code == 200, probability.text
    assessment = probability.json()["probability"]
    assert_contract(probability.json(), "ProbabilityOperationResponse")
    assert assessment["joint_probability"] == assessment["premise_probability"] * assessment["conditional_probability"]
    assert assessment["joint_probability_range"] == {"minimum": 0.06, "maximum": 0.15}
    assert assessment["calibration_status"] == "model_estimate_unvalidated"


def test_direct_ordinary_confirmation_rejects_low_rubric_without_mutation() -> None:
    created = create_challenge(private=True, key="direct-ordinary-challenge-key")
    challenge_id = created["challenge"]["challenge_id"]
    before = CHALLENGES[challenge_id].model_dump(mode="json", exclude_none=True)
    before_turn = CHALLENGE_TURNS[challenge_id]

    rejected = CLIENT.post(
        f"/v1/challenges/{challenge_id}/confirm",
        json={"command": "CONFIRM", "unresolved_issues_acknowledged": False},
        headers=idem("direct-ordinary-confirm-key"),
    )
    assert rejected.status_code == 409
    assert rejected.json()["error_code"] == "confirmation_requirements_not_met"
    assert_contract(rejected.json(), "ErrorResponse")
    locations = {item["location"] for item in rejected.json()["details"]}
    assert {
        "challenge/rubric_scores/total",
        "challenge/rubric_scores/premise_clarity",
        "challenge/rubric_scores/time_boundary",
        "challenge/rubric_scores/causal_chain",
        "challenge/rubric_scores/asset_transmission",
        "challenge/rubric_scores/falsifiability",
        "challenge/missing_evidence",
        "challenge/unresolved_issues",
    } <= locations
    assert CHALLENGES[challenge_id].model_dump(mode="json", exclude_none=True) == before
    assert CHALLENGE_TURNS[challenge_id] == before_turn


def test_ordinary_confirmation_requires_independently_reviewable_evidence_even_with_passing_rubric() -> None:
    created = create_challenge(key="passing-rubric-without-evidence")
    challenge_id = created["challenge"]["challenge_id"]
    current = CHALLENGES[challenge_id]
    passing_scores = tuple(score.model_copy(update={"score": 3}) for score in current.rubric_scores)
    CHALLENGES[challenge_id] = current.model_copy(update={"rubric_scores": passing_scores, "missing_evidence": (), "unresolved_issues": ()})
    rejected = CLIENT.post(
        f"/v1/challenges/{challenge_id}/confirm",
        json={"command": "CONFIRM", "unresolved_issues_acknowledged": False},
        headers=idem("passing-rubric-no-evidence-confirm"),
    )
    assert rejected.status_code == 409
    assert {item["location"] for item in rejected.json()["details"]} == {"challenge/evidence"}


def test_reservation_confirmation_requires_true_acknowledgment_and_note() -> None:
    created = create_challenge(private=True, key="false-ack-challenge-key")
    challenge_id = created["challenge"]["challenge_id"]
    before = CHALLENGES[challenge_id].model_dump(mode="json", exclude_none=True)

    false_ack = CLIENT.post(
        f"/v1/challenges/{challenge_id}/confirm",
        json={
            "command": "CONFIRM_WITH_RESERVATIONS",
            "unresolved_issues_acknowledged": False,
            "reservation_note": "The private claim remains unresolved.",
        },
        headers=idem("false-ack-confirm-key"),
    )
    assert false_ack.status_code == 422
    assert false_ack.json()["error_code"] == "validation_error"

    missing_note = CLIENT.post(
        f"/v1/challenges/{challenge_id}/confirm",
        json={"command": "CONFIRM_WITH_RESERVATIONS", "unresolved_issues_acknowledged": True},
        headers=idem("missing-reservation-key"),
    )
    assert missing_note.status_code == 422
    whitespace_note = CLIENT.post(
        f"/v1/challenges/{challenge_id}/confirm",
        json={
            "command": "CONFIRM_WITH_RESERVATIONS",
            "unresolved_issues_acknowledged": True,
            "reservation_note": "   ",
        },
        headers=idem("whitespace-reservation-key"),
    )
    assert whitespace_note.status_code == 422
    assert CHALLENGES[challenge_id].model_dump(mode="json", exclude_none=True) == before


def test_direct_reservation_confirmation_preserves_review_and_private_evidence() -> None:
    created = create_challenge(private=True, key="direct-reservation-challenge-key")
    challenge_id = created["challenge"]["challenge_id"]
    before = created["challenge"]
    confirmed = CLIENT.post(
        f"/v1/challenges/{challenge_id}/confirm",
        json={
            "command": "CONFIRM_WITH_RESERVATIONS",
            "unresolved_issues_acknowledged": True,
            "reservation_note": "The industry claim is not independently reviewable.",
        },
        headers=idem("direct-reservation-confirm-key"),
    )
    assert confirmed.status_code == 200, confirmed.text
    after = confirmed.json()["challenge"]
    assert after["status"] == "CONFIRMED"
    assert after["user_confirmation"] == "confirmed_with_reservations"
    assert after["reservation_note"] == "The industry claim is not independently reviewable."
    for field in (
        "claims", "objections", "missing_evidence", "questions", "rubric_scores",
        "unresolved_issues", "private_evidence_declared",
    ):
        assert after[field] == before[field]


def test_market_factor_registry_and_client_attestation_contracts() -> None:
    session = create_session()
    asset_search = CLIENT.get("/v1/assets/search", params={"query": "bitcoin"})
    assert asset_search.status_code == 200
    assert {item["asset_id"] for item in asset_search.json()["items"]} == {"CRYPTO:BTC", "ETF:IBIT"}
    assert CLIENT.get("/v1/assets/YIELD:US10Y").json()["proxy_instruments"] == ["ETF:IEF"]
    assert CLIENT.get("/v1/assets/UNKNOWN").status_code == 404

    manifest_response = CLIENT.post(
        "/v1/market-data/manifests",
        json={
            "asset_ids": ["ETF:SPY", "CRYPTO:BTC"],
            "from": "2026-08-01T00:00:00Z",
            "to": "2026-08-10T00:00:00Z",
            "field_set": ["open", "high", "low", "close", "volume", "amount", "currency", "adjusted", "provider", "as_of"],
        },
        headers=idem("market-manifest-key"),
    )
    assert manifest_response.status_code == 201, manifest_response.text
    assert_contract(manifest_response.json(), "MarketDataManifestOperationResponse")
    manifest = manifest_response.json()["manifest"]
    assert manifest["freshness"] == "historical_only"
    shard_response = CLIENT.post(
        "/v1/market-data/shards",
        json={"manifest_id": manifest["manifest_id"], "shard_id": manifest["shards"][0]["shard_id"]},
        headers=idem("market-shard-key-01"),
    )
    assert shard_response.status_code == 200, shard_response.text
    assert_contract(shard_response.json(), "MarketDataShardOperationResponse")
    assert len(shard_response.json()["shard"]["rows"]) == 2

    factor_manifest = CLIENT.get("/v1/factor-libraries/v1.0.0/manifest")
    assert factor_manifest.status_code == 200, factor_manifest.text
    assert_contract(factor_manifest.json(), "FactorLibraryManifest")
    factor_shard = CLIENT.get(
        "/v1/factor-libraries/v1.0.0/shards",
        params={"shard_id": factor_manifest.json()["shards"][0]["shard_id"]},
    )
    assert factor_shard.status_code == 200, factor_shard.text
    assert_contract(factor_shard.json(), "FactorLibraryShard")
    assert factor_shard.json()["factors"][0]["expression_tokens"][0]["kind"] == "field"

    unversioned_factor_manifest = CLIENT.get("/v1/factor-libraries/manifest")
    assert unversioned_factor_manifest.status_code == 404

    operation_id = session["operation"]["operation_id"]
    attestation = CLIENT.post(
        "/v1/client-computations/computation:test/attest",
        json={
            "operation_id": operation_id,
            "engine_version": "v1.0.0",
            "worldline_hash": "1" * 64,
            "market_manifest_hash": manifest["content_hash"],
            "factor_manifest_hash": factor_manifest.json()["content_hash"],
            "input_hash": "2" * 64,
            "output_hash": "3" * 64,
            "completed_at": "2026-08-10T00:00:00Z",
        },
        headers=idem("attestation-key-01"),
    )
    assert attestation.status_code == 200, attestation.text
    assert_contract(attestation.json(), "ClientAttestationOperationResponse")
    assert attestation.json()["attestation"]["accepted"] is True


def test_sse_union_heartbeat_complete_and_last_event_id_replay() -> None:
    payload = create_worldlines()
    operation = payload["operation"]
    stream = CLIENT.get(f"/v1/operations/{operation['operation_id']}/events")
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line.removeprefix("data: ")) for line in stream.text.splitlines() if line.startswith("data: ")]
    assert [event["event"] for event in events] == ["operation.started", "operation.progress", "heartbeat", "operation.complete"]
    for event in events:
        assert_contract(event, "SseEvent")
    replay = CLIENT.get(
        f"/v1/operations/{operation['operation_id']}/events",
        headers={"Last-Event-ID": f"{operation['operation_id']}:3"},
    )
    assert replay.text.count("data: ") == 1
    assert "operation.complete" in replay.text
    wrong_cursor = CLIENT.get(
        f"/v1/operations/{operation['operation_id']}/events",
        headers={"Last-Event-ID": "operation:other:3"},
    )
    assert wrong_cursor.status_code == 400
    assert wrong_cursor.json()["error_code"] == "invalid_last_event_id"


def test_completed_operation_cancel_and_resume_are_safe_noops() -> None:
    payload = create_session()
    operation = payload["operation"]
    bad = CLIENT.post(
        f"/v1/operations/{operation['operation_id']}/cancel",
        json={"cancel_token": "x" * 16},
        headers=idem("bad-cancel-key-01"),
    )
    assert bad.status_code == 403
    cancelled = CLIENT.post(
        f"/v1/operations/{operation['operation_id']}/cancel",
        json={"cancel_token": operation["cancel_token"]},
        headers=idem("cancel-key-00001"),
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["operation"]["status"] == "completed"
    resumed = CLIENT.post(
        f"/v1/operations/{operation['operation_id']}/resume",
        json={"resume_token": operation["resume_token"]},
        headers=idem("resume-key-00001"),
    )
    assert resumed.status_code == 200
    assert resumed.json()["operation"]["status"] == "completed"


def test_async_initial_challenge_fetches_authoritative_record_and_replays_events() -> None:
    session = create_session("async-initial-session")
    accepted = CLIENT.post(
        "/v1/challenges/async",
        json={
            "session_id": session["session"]["session_id"],
            "thesis": "An async challenge must retain the authoritative challenger result.",
            "capital": 100_000,
            "base_currency": "USD",
            "user_private_evidence_claims": False,
        },
        headers=idem("async-initial-challenge"),
    )
    assert accepted.status_code == 202, accepted.text
    receipt = accepted.json()
    assert_contract(receipt, "OperationResponse")
    operation = receipt["operation"]
    stream = CLIENT.get(f"/v1/operations/{operation['operation_id']}/events", headers={"Last-Event-ID": operation["last_event_id"]})
    events = [json.loads(line.removeprefix("data: ")) for line in stream.text.splitlines() if line.startswith("data: ")]
    complete = next(event for event in events if event["event"] == "operation.complete")
    challenge_id = complete["result_ref"]
    fetched = CLIENT.get(f"/v1/challenges/{challenge_id}")
    assert fetched.status_code == 200, fetched.text
    assert_contract(fetched.json(), "ChallengeOperationResponse")
    assert fetched.json()["challenge"]["challenge_id"] == challenge_id
    assert fetched.json()["operation"]["operation_id"] == operation["operation_id"]


def test_async_challenge_cancel_then_resume_replays_from_cursor_without_second_provider_call(monkeypatch) -> None:
    session = create_session("async-cancel-session")
    provider_started = Event()
    provider_cancelled = Event()
    calls = 0

    def blocked_generate(request):
        nonlocal calls
        calls += 1
        provider_started.set()
        assert request.cancel_event is not None and request.cancel_event.wait(1)
        provider_cancelled.set()
        raise ProviderFailure("provider_cancelled", "cancelled", retryable=True)

    monkeypatch.setattr(main.challenge_provider, "generate_challenge", blocked_generate)
    accepted = CLIENT.post(
        "/v1/challenges/async",
        json={
            "session_id": session["session"]["session_id"],
            "thesis": "A cancellation must stop the accepted challenge operation.",
            "capital": 100_000,
            "base_currency": "USD",
            "user_private_evidence_claims": False,
        },
        headers=idem("async-cancel-challenge"),
    )
    assert accepted.status_code == 202, accepted.text
    operation = accepted.json()["operation"]
    assert provider_started.wait(1)
    cancelled = CLIENT.post(
        f"/v1/operations/{operation['operation_id']}/cancel",
        json={"cancel_token": operation["cancel_token"]},
        headers=idem("async-cancel-command"),
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["operation"]["status"] == "cancelled"
    assert provider_cancelled.wait(1)
    resumed = CLIENT.post(
        f"/v1/operations/{operation['operation_id']}/resume",
        json={"resume_token": operation["resume_token"]},
        headers=idem("async-cancel-resume"),
    )
    assert resumed.status_code == 200
    assert resumed.json()["operation"]["status"] == "cancelled"
    replay = CLIENT.get(
        f"/v1/operations/{operation['operation_id']}/events",
        headers={"Last-Event-ID": operation["last_event_id"]},
    )
    assert replay.status_code == 200
    assert "operation.error" in replay.text
    assert calls == 1


def test_challenge_authoritative_fetch_uses_durable_operation_receipt_after_restart_simulation() -> None:
    created = create_challenge(key="durable-challenge-receipt")
    challenge_id = created["challenge"]["challenge_id"]
    operation_id = created["operation"]["operation_id"]
    original_hash = created["challenge"]["content_hash"]

    newer = CLIENT.post(
        f"/v1/challenges/{challenge_id}/messages",
        json={"message": "A later turn must not replace the earlier operation-bound result."},
        headers=idem("durable-challenge-later-turn"),
    )
    assert newer.status_code == 200, newer.text
    assert newer.json()["challenge"]["content_hash"] != original_hash

    main.CHALLENGE_OPERATION_IDS.clear()
    main.operations.operations.clear()
    main.operations.events.clear()

    durable = CLIENT.get(f"/v1/challenges/{challenge_id}", params={"operation_id": operation_id})
    assert durable.status_code == 200, durable.text
    assert durable.json()["operation"]["operation_id"] == operation_id
    assert durable.json()["challenge"]["challenge_id"] == challenge_id
    assert durable.json()["challenge"]["content_hash"] == original_hash
    legacy = CLIENT.get(f"/v1/challenges/{challenge_id}")
    assert legacy.status_code == 409
    unrelated = create_session("durable-receipt-unrelated")
    wrong_completed = CLIENT.get(
        f"/v1/challenges/{challenge_id}",
        params={"operation_id": unrelated["operation"]["operation_id"]},
    )
    assert wrong_completed.status_code == 409
    mismatched = CLIENT.get(f"/v1/challenges/{challenge_id}", params={"operation_id": "operation_not_the_matching_receipt"})
    assert mismatched.status_code == 404
