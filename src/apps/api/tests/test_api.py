from __future__ import annotations

import json

from .helpers import CLIENT, assert_contract, confirm_standard_flow, create_challenge, create_session, create_worldlines, idem


def test_health_and_strict_error_envelope() -> None:
    response = CLIENT.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "provider": "deterministic_mock", "schema_version": "v1", "clock": "2026-08-10T00:00:00Z"}
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


def test_normal_state_path_worldlines_probability_and_get() -> None:
    _, confirmed = confirm_standard_flow()
    assert confirmed["challenge"]["status"] == "CONFIRMED"
    assert confirmed["challenge"]["user_confirmation"] == "confirmed"
    assert_contract(confirmed, "ChallengeOperationResponse")

    challenge_id = confirmed["challenge"]["challenge_id"]
    worldline_response = CLIENT.post(
        "/v1/worldlines",
        json={"challenge_id": challenge_id, "premise": "Observable grid investment accelerates.", "horizon_end": "2027-08-10"},
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
        json={"worldline_id": worldline["worldline_id"], "premise": worldline["premise"], "conditional_path": "main path given premise"},
        headers=idem("probability-key-0001"),
    )
    assert probability.status_code == 200, probability.text
    assessment = probability.json()["probability"]
    assert_contract(probability.json(), "ProbabilityOperationResponse")
    assert assessment["joint_probability"] == assessment["premise_probability"] * assessment["conditional_probability"]
    assert assessment["joint_probability_range"] == {"minimum": 0.06, "maximum": 0.15}
    assert assessment["calibration_status"] == "model_estimate_unvalidated"


def test_explicit_reservation_command_and_illegal_state_paths() -> None:
    created = create_challenge(private=True, key="private-challenge-key")
    challenge_id = created["challenge"]["challenge_id"]
    illegal_evidence = CLIENT.post(
        f"/v1/challenges/{challenge_id}/evidence",
        json={"title": "Private note", "retrieved_at": "2026-08-10T00:00:00Z", "excerpt": "Unverifiable", "user_note": "", "content_hash": "b" * 64},
        headers=idem("illegal-evidence-key"),
    )
    assert illegal_evidence.status_code == 409
    CLIENT.post(
        f"/v1/challenges/{challenge_id}/messages",
        json={"message": "Private claim without public corroboration."},
        headers=idem("private-message-key"),
    )
    missing_note = CLIENT.post(
        f"/v1/challenges/{challenge_id}/confirm",
        json={"command": "CONFIRM_WITH_RESERVATIONS", "unresolved_issues_acknowledged": True},
        headers=idem("missing-reservation-key"),
    )
    assert missing_note.status_code == 422
    confirmed = CLIENT.post(
        f"/v1/challenges/{challenge_id}/confirm",
        json={
            "command": "CONFIRM_WITH_RESERVATIONS",
            "unresolved_issues_acknowledged": True,
            "reservation_note": "The industry claim is not independently reviewable.",
        },
        headers=idem("reservation-key-0001"),
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["challenge"]["user_confirmation"] == "confirmed_with_reservations"
    assert confirmed.json()["challenge"]["reservation_note"]


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
