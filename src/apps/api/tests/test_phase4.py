from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
import pytest

import app.main as main
from app.services import ContractViolation, validate_branch_probability_consistency, validate_worldline_bundle_semantics

from .helpers import CLIENT, confirm_standard_flow, create_worldlines, idem


def test_phase4_worldline_is_challenge_derived_and_interval_first() -> None:
    _, confirmed = confirm_standard_flow()
    challenge_id = confirmed["challenge"]["challenge_id"]
    rejected = CLIENT.post("/v1/worldlines", json={"challenge_id": challenge_id, "premise": "caller supplied", "horizon_end": "2027-08-10"}, headers=idem("phase4-extra-premise"))
    assert rejected.status_code == 422
    generated = CLIENT.post("/v1/worldlines", json={"challenge_id": challenge_id, "horizon_end": "2027-08-10"}, headers=idem("phase4-generated-path"))
    assert generated.status_code == 201, generated.text
    bundle = generated.json()["bundle"]
    assert {line["branch_kind"] for line in bundle["worldlines"]} == {"weak", "main", "strong"}
    assert len(bundle["probability_assessments"]) == 3
    assert sum(item["conditional_probability"] for item in bundle["probability_assessments"]) == 1
    for line in bundle["worldlines"]:
        assert line["premise"] != "caller supplied"
        assert line["asset_impacts"]
        assert all(node["data_as_of"] <= line["baseline_date"] <= node["event_date"] for node in line["nodes"])
    for item in bundle["probability_assessments"]:
        assert item["joint_probability"] == item["premise_probability"] * item["conditional_probability"]
        assert item["calibration_status"] == "model_estimate_unvalidated"


def test_phase4_semantic_validators_reject_non_partition_and_future_data() -> None:
    _, confirmed = confirm_standard_flow()
    response = CLIENT.post("/v1/worldlines", json={"challenge_id": confirmed["challenge"]["challenge_id"], "horizon_end": "2027-08-10"}, headers=idem("phase4-validator-path"))
    bundle = main.WorldlineBundle(**response.json()["bundle"])
    invalid_assessments = list(bundle.probability_assessments)
    invalid_assessments[0] = invalid_assessments[0].model_copy(update={"conditional_probability": 0.21, "joint_probability": 0.042})
    with pytest.raises(ContractViolation, match="sum to one"):
        validate_branch_probability_consistency(tuple(invalid_assessments), bundle.worldlines)
    invalid_line = bundle.worldlines[0].model_copy(update={"nodes": [bundle.worldlines[0].nodes[0].model_copy(update={"data_as_of": date(2026, 8, 11)}), *bundle.worldlines[0].nodes[1:]]})
    invalid_bundle = bundle.model_copy(update={"worldlines": [invalid_line, *bundle.worldlines[1:]]})
    with pytest.raises(ContractViolation, match="future data"):
        validate_worldline_bundle_semantics(invalid_bundle)


def test_phase4_unknown_provider_output_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    class UnknownGenerator:
        def generate(self, _request: object) -> object:
            return object()
    _, confirmed = confirm_standard_flow()
    monkeypatch.setattr(main, "worldline_generator", UnknownGenerator())
    response = CLIENT.post("/v1/worldlines", json={"challenge_id": confirmed["challenge"]["challenge_id"], "horizon_end": "2027-08-10"}, headers=idem("phase4-unknown-output"))
    assert response.status_code == 500
    assert response.json()["error_code"] == "internal_error"


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("challenge", lambda bundle: bundle.model_copy(update={"challenge_id": "challenge:other"})),
        (
            "bundle-id",
            lambda bundle: bundle.model_copy(update={
                "worldlines": [bundle.worldlines[0].model_copy(update={"bundle_id": "bundle:other"}), *bundle.worldlines[1:]],
            }),
        ),
        (
            "asset-impact-schema",
            lambda bundle: bundle.model_copy(update={
                "worldlines": [bundle.worldlines[0].model_copy(update={
                    "asset_impacts": [bundle.worldlines[0].asset_impacts[0].model_copy(update={"already_priced_score": 5})],
                }), *bundle.worldlines[1:]],
            }),
        ),
        (
            "reveal-horizon",
            lambda bundle: bundle.model_copy(update={
                "worldlines": [bundle.worldlines[0].model_copy(update={
                    "event_reveal_schedule": [bundle.worldlines[0].event_reveal_schedule[0].model_copy(update={
                        "reveal_date": bundle.worldlines[0].horizon_end + timedelta(days=1),
                    })],
                }), *bundle.worldlines[1:]],
            }),
        ),
        (
            "conditional-partition",
            lambda bundle: bundle.model_copy(update={
                "probability_assessments": [bundle.probability_assessments[0].model_copy(update={
                    "conditional_probability": 0.21,
                    "joint_probability": 0.042,
                }), *bundle.probability_assessments[1:]],
            }),
        ),
    ],
)
def test_phase4_typed_invalid_worldline_bundle_fails_terminal_without_persistence(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    mutate: Callable[[main.WorldlineBundle], main.WorldlineBundle],
) -> None:
    class TypedInvalidGenerator:
        def generate(self, request: main.WorldlineGenerationInput) -> main.WorldlineBundle:
            valid = main.DeterministicMockWorldlineGenerator(main.probability_assessor, main.clock).generate(request)
            return mutate(valid)

    _, confirmed = confirm_standard_flow()
    monkeypatch.setattr(main, "worldline_generator", TypedInvalidGenerator())
    response = CLIENT.post(
        "/v1/worldlines",
        json={"challenge_id": confirmed["challenge"]["challenge_id"], "horizon_end": "2027-08-10"},
        headers=idem(f"phase4-typed-invalid-{case}"),
    )

    assert response.status_code == 500
    assert response.json()["error_code"] == "internal_error"
    assert "outside the accepted boundary" not in response.text
    assert main.WORLDLINES == {}
    operation = [item for item in main.operations.operations.values() if item.endpoint == "/v1/worldlines"][-1]
    assert operation.status == "failed"
    assert main.operations.events[operation.operation_id][-1].event == "operation.error"


def test_phase4_typed_invalid_probability_assessment_fails_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = main.WorldlineBundle(**create_worldlines()["bundle"])
    worldline = payload.worldlines[0]

    class TypedInvalidAssessor:
        def assess(self, request: main.ProbabilityAssessmentInput) -> main.ProbabilityAssessment:
            valid = main.DeterministicMockProbabilityAssessor(main.clock).assess(request)
            return valid.model_copy(update={"joint_probability": valid.joint_probability + 0.001})

    monkeypatch.setattr(main, "probability_assessor", TypedInvalidAssessor())
    stored_worldlines = dict(main.WORLDLINES)
    response = CLIENT.post(
        "/v1/probabilities/assess",
        json={"worldline_id": worldline.worldline_id},
        headers=idem("phase4-typed-invalid-probability"),
    )

    assert response.status_code == 500
    assert response.json()["error_code"] == "internal_error"
    assert "outside the accepted boundary" not in response.text
    assert main.WORLDLINES == stored_worldlines
    operation = [item for item in main.operations.operations.values() if item.endpoint == "/v1/probabilities/assess"][-1]
    assert operation.status == "failed"
    assert main.operations.events[operation.operation_id][-1].event == "operation.error"


def test_phase4_typed_wrong_worldline_probability_assessment_fails_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = main.WorldlineBundle(**create_worldlines()["bundle"])
    requested_worldline, other_worldline = payload.worldlines[:2]

    class TypedWrongWorldlineAssessor:
        def assess(self, request: main.ProbabilityAssessmentInput) -> main.ProbabilityAssessment:
            valid = main.DeterministicMockProbabilityAssessor(main.clock).assess(request)
            return valid.model_copy(update={"worldline_id": other_worldline.worldline_id})

    monkeypatch.setattr(main, "probability_assessor", TypedWrongWorldlineAssessor())
    stored_worldlines = dict(main.WORLDLINES)
    response = CLIENT.post(
        "/v1/probabilities/assess",
        json={"worldline_id": requested_worldline.worldline_id},
        headers=idem("phase4-typed-wrong-worldline-probability"),
    )

    assert response.status_code == 500
    assert response.json()["error_code"] == "internal_error"
    assert "outside the accepted boundary" not in response.text
    assert main.WORLDLINES == stored_worldlines
    operation = [item for item in main.operations.operations.values() if item.endpoint == "/v1/probabilities/assess"][-1]
    assert operation.status == "failed"
    assert main.operations.events[operation.operation_id][-1].event == "operation.error"
