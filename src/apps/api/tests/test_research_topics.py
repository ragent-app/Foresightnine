from fastapi.testclient import TestClient

from app.main import app
from app.research_topics import ResearchVersion
from app.research_calculations import CalculationRequest, calculate
import pytest


def test_legacy_versions_keep_unknown_creator():
    version = ResearchVersion.model_validate({"version_id": "old", "created_at": "2023-01-01T00:00:00Z",
                                               "content": {}, "review": "draft"})
    assert version.created_by == "unknown"


@pytest.mark.parametrize("claim", [
    {"text": "", "role": "support"},
    {"text": "A", "role": ["support"]},
    {"text": "A", "citations": [{"source_id": ""}]},
    {"text": "A", "citations": "not a list"},
])
def test_user_findings_reject_invalid_shapes(monkeypatch, claim):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)
    from uuid import uuid4
    registration = client.post("/v1/auth/local/register", json={"email": f"claims-{uuid4().hex}@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registration.json()["access_token"]}
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Review?"}).json()
    path = "/v2/research/topics/" + topic["topic_id"] + "/versions"
    response = client.post(path, headers=headers, json={"parent_version_id": topic["versions"][0]["version_id"],
        "content": {"claims": [claim]}})
    assert response.status_code == 422


def test_recalculate_current_facts_creates_auditable_draft(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)
    registration = client.post("/v1/auth/local/register", json={"email": "recalculate@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registration.json()["access_token"]}
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Growth?"}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    first = {"fact_id": "a", "value": "100", "period": "2023", "unit": "USD", "currency": "USD"}
    second = {"fact_id": "b", "value": "80", "period": "2022", "unit": "USD", "currency": "USD"}
    old = calculate(CalculationRequest(formula="growth_percent", first=first, second=second))
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": topic["versions"][0]["version_id"],
        "review": "confirmed", "note": "Review me", "content": {"summary": "My view",
        "facts": [dict(first, value="200"), second], "calculations": [old]}}).json()
    original = saved["versions"][-1]
    assert original["content"]["calculations"][0]["input_status"] == "changed"
    payload = {"parent_version_id": original["version_id"], "calculation_index": 0}
    response = client.post(path + "/calculations/recalculate", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    latest = response.json()["versions"][-1]
    assert latest["content"]["calculations"][0]["value"] == "150.00"
    assert latest["content"]["calculations"][0]["authorship"] == "calculation_engine"
    assert latest["content"]["calculations"][0]["input_status"] == "matched"
    assert latest["review"] == "draft" and latest["note"] == "Review me"
    assert latest["content"]["summary"] == "My view"
    assert response.json()["versions"][-2]["content"]["calculations"][0]["value"] == "25.00"
    assert client.post(path + "/calculations/recalculate", headers=headers, json=payload).status_code == 409
    content = latest["content"]
    content["facts"][1]["value"] = "0"
    broken = client.post(path + "/versions", headers=headers, json={"parent_version_id": latest["version_id"], "content": content}).json()
    parent = broken["versions"][-1]["version_id"]
    failed = client.post(path + "/calculations/recalculate", headers=headers, json={"parent_version_id": parent, "calculation_index": 0})
    assert failed.status_code == 422
    assert client.get(path, headers=headers).json()["versions"][-1]["version_id"] == parent


def test_edited_facts_reject_invalid_values_and_duplicate_identifiers(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)
    registration = client.post("/v1/auth/local/register", json={"email": "fact-validation@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registration.json()["access_token"]}
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Review facts?"}).json()
    path = "/v2/research/topics/" + topic["topic_id"] + "/versions"
    parent = topic["versions"][0]["version_id"]
    base = {"fact_id": "a", "value": "100", "period": "2023", "unit": "USD", "currency": "USD", "label": "Revenue"}
    for invalid in (dict(base, value="NaN"), dict(base, value="1e31"), dict(base, unit=" "), dict(base, source_id=[])):
        response = client.post(path, headers=headers, json={"parent_version_id": parent, "content": {"facts": [invalid]}})
        assert response.status_code == 422
    duplicate = client.post(path, headers=headers, json={"parent_version_id": parent,
        "content": {"facts": [base, dict(base, fact_id=" a ")]}})
    assert duplicate.status_code == 422
    valid = client.post(path, headers=headers, json={"parent_version_id": parent,
        "content": {"facts": [dict(base, source_id=None, quotation=None)]}})
    assert valid.status_code == 201
    saved = valid.json()["versions"][-1]["content"]["facts"][0]
    assert saved["value"] == "100" and saved["verification"] == "user_edited_not_independently_verified"
    assert saved["source_id"] is None and saved["quotation"] is None


def test_account_scoped_research_version_history_and_conflicts(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)

    def account(email):
        response = client.post("/v1/auth/local/register", json={
            "email": email, "password": "correct horse battery staple!"})
        assert response.status_code == 201, response.text
        return {"Authorization": "Bearer " + response.json()["access_token"]}

    first = account("first@example.test")
    second = account("second@example.test")
    assert client.post("/v2/research/topics", json={"question": "Research?"}).status_code == 401
    assert client.post("/v2/research/topics", headers=first, json={}).status_code == 422
    created = client.post("/v2/research/topics", headers=first,
                          json={"question": "Compare revenue"})
    assert created.status_code == 201, created.text
    topic = created.json()
    path = "/v2/research/topics/" + topic["topic_id"]
    assert client.get(path, headers=second).status_code == 404
    assert client.get("/v2/research/topics", headers=second).json() == []
    parent = topic["versions"][0]["version_id"]
    payload = {"parent_version_id": parent, "content": {"summary": "Reviewed"},
               "review": "confirmed"}
    assert client.post(path + "/versions", headers=second, json=payload).status_code == 404
    saved = client.post(path + "/versions", headers=first, json=payload)
    assert saved.status_code == 201, saved.text
    assert len(saved.json()["versions"]) == 2
    assert saved.json()["versions"][0]["content"]["summary"] == ""
    assert client.post(path + "/versions", headers=first, json=payload).status_code == 409
    assert client.get(path, headers=first).json()["versions"][-1]["content"]["summary"] == "Reviewed"
    assert client.delete("/v1/auth/local/account", headers=first).status_code == 200
