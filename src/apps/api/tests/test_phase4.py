from __future__ import annotations

from datetime import date
import pytest

import app.main as main
from app.services import ContractViolation, validate_branch_probability_consistency, validate_worldline_bundle_semantics

from .helpers import CLIENT, confirm_standard_flow, idem


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
