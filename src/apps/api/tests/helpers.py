from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker

from app.main import app
from app.phase3 import SearchEvidenceBundle

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
SCHEMA = json.loads((WORKSPACE_ROOT / "packages" / "contracts" / "schemas" / "v1" / "contracts.schema.json").read_text(encoding="utf-8"))
CLIENT = TestClient(app, raise_server_exceptions=False)


def idem(value: str) -> dict[str, str]:
    return {"Idempotency-Key": value}


def evidence_hash(*, url: str | None, title: str, published_at: str | None, retrieved_at: str, excerpt: str, user_note: str) -> str:
    return SearchEvidenceBundle(
        url=url, title=title,
        published_at=datetime.fromisoformat(published_at.replace("Z", "+00:00")) if published_at else None,
        retrieved_at=datetime.fromisoformat(retrieved_at.replace("Z", "+00:00")),
        excerpt=excerpt, user_note=user_note, supplied_content_hash="",
    ).expected_content_hash()


def assert_contract(payload: dict[str, Any], name: str) -> None:
    validator = Draft202012Validator({"$ref": f"#/$defs/{name}", "$defs": SCHEMA["$defs"]}, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.absolute_path))
    assert not errors, [(error.json_path, error.message) for error in errors]


def create_session(key: str = "session-key-0001") -> dict[str, Any]:
    response = CLIENT.post("/v1/sessions", json={}, headers=idem(key))
    assert response.status_code == 201, response.text
    assert_contract(response.json(), "SessionOperationResponse")
    return response.json()


def create_challenge(*, private: bool = False, key: str = "challenge-key-0001") -> dict[str, Any]:
    session = create_session(f"{key}-session")
    response = CLIENT.post(
        "/v1/challenges",
        json={
            "session_id": session["session"]["session_id"],
            "thesis": "A dated infrastructure demand thesis with observable falsification criteria.",
            "capital": 100_000,
            "base_currency": "USD",
            "user_private_evidence_claims": private,
        },
        headers=idem(key),
    )
    assert response.status_code == 201, response.text
    assert_contract(response.json(), "ChallengeOperationResponse")
    return response.json()


def confirm_standard_flow() -> tuple[dict[str, Any], dict[str, Any]]:
    created = create_challenge()
    challenge_id = created["challenge"]["challenge_id"]
    message = CLIENT.post(
        f"/v1/challenges/{challenge_id}/messages",
        json={"message": "The trigger must be visible by the stated horizon."},
        headers=idem("message-key-0001"),
    )
    assert message.status_code == 200, message.text
    assert message.json()["challenge"]["status"] == "EVIDENCE_REQUIRED"
    evidence = CLIENT.post(
        f"/v1/challenges/{challenge_id}/evidence",
        json={
            "url": "https://example.com/public-evidence",
            "title": "Dated public evidence",
            "publisher": "Example publisher",
            "published_at": "2026-08-01T00:00:00Z",
            "retrieved_at": "2026-08-10T00:00:00Z",
            "excerpt": "A reproducible public excerpt used only by the deterministic fixture.",
            "user_note": "Client supplied fallback.",
            "content_hash": evidence_hash(
                url="https://example.com/public-evidence", title="Dated public evidence",
                published_at="2026-08-01T00:00:00Z", retrieved_at="2026-08-10T00:00:00Z",
                excerpt="A reproducible public excerpt used only by the deterministic fixture.", user_note="Client supplied fallback.",
            ),
        },
        headers=idem("evidence-key-0001"),
    )
    assert evidence.status_code == 200, evidence.text
    passing_challenge = evidence.json()["challenge"]
    assert passing_challenge["status"] == "CHALLENGING"
    assert sum(item["score"] for item in passing_challenge["rubric_scores"]) >= 30
    critical = {"premise_clarity", "time_boundary", "causal_chain", "asset_transmission", "falsifiability"}
    assert all(item["score"] >= 3 for item in passing_challenge["rubric_scores"] if item["criterion"] in critical)
    assert passing_challenge["missing_evidence"] == []
    assert passing_challenge["unresolved_issues"] == []
    confirmed = CLIENT.post(
        f"/v1/challenges/{challenge_id}/confirm",
        json={"command": "CONFIRM", "unresolved_issues_acknowledged": False},
        headers=idem("confirm-key-0001"),
    )
    assert confirmed.status_code == 200, confirmed.text
    confirmed_challenge = confirmed.json()["challenge"]
    for field in (
        "claims", "objections", "missing_evidence", "questions", "rubric_scores",
        "unresolved_issues", "private_evidence_declared",
    ):
        assert confirmed_challenge[field] == passing_challenge[field]
    return created, confirmed.json()


def create_worldlines() -> dict[str, Any]:
    _, confirmed = confirm_standard_flow()
    challenge_id = confirmed["challenge"]["challenge_id"]
    response = CLIENT.post(
        "/v1/worldlines",
        json={"challenge_id": challenge_id, "premise": "Observable grid investment accelerates.", "horizon_end": "2027-08-10"},
        headers=idem("worldline-key-0001"),
    )
    assert response.status_code == 201, response.text
    assert_contract(response.json(), "WorldlineBundleOperationResponse")
    return response.json()
