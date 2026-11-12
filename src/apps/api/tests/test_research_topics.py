from copy import deepcopy
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


def test_legacy_percent_difference_recalculates_as_percentage_points(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)
    registration = client.post("/v1/auth/local/register", json={"email": "percentage-recalculate@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registration.json()["access_token"]}
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Growth differences?"}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    first = {"fact_id": "a", "value": "2.8", "period": "2024", "unit": "percent", "currency": None}
    second = {"fact_id": "b", "value": "2.9", "period": "2023", "unit": "percent", "currency": None}
    legacy = {**calculate(CalculationRequest(formula="difference", first=first, second=second)),
              "formula_version": "1", "unit": "percent"}
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": topic["versions"][0]["version_id"],
        "content": {"facts": [first, second], "calculations": [legacy]}}).json()["versions"][-1]
    response = client.post(path + "/calculations/recalculate", headers=headers,
        json={"parent_version_id": saved["version_id"], "calculation_index": 0})
    assert response.status_code == 201
    latest = response.json()["versions"][-1]
    assert latest["content"]["calculations"][0]["unit"] == "percentage points"
    assert latest["content"]["calculations"][0]["formula_version"] == "2"
    assert response.json()["versions"][-2]["content"]["calculations"][0]["unit"] == "percent"
    repeated = client.post(path + "/calculations/recalculate", headers=headers,
        json={"parent_version_id": latest["version_id"], "calculation_index": 0})
    assert repeated.status_code == 201 and repeated.json()["versions"][-1]["content"]["calculations"][0]["formula_version"] == "2"


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


def test_partial_user_quotation_stays_incomplete_until_replaced(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)
    registration = client.post("/v1/auth/local/register", json={"email": "partial-word@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registration.json()["access_token"]}
    source = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Revenue was 1000.",
        "permissions": {"store": True, "display": True}}).json()
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source["source_id"]]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    content = {"claims": [{"text": "Revenue", "citations": [{"source_id": source["source_id"], "quotation": "100"}]}]}
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": topic["versions"][-1]["version_id"], "content": content})
    assert saved.status_code == 201, saved.text
    current = saved.json()["versions"][-1]
    assert current["content"]["claims"][0]["support_status"] == "incomplete"
    assert current["content"]["claims"][0]["citations"][0]["partial_token"]
    changed = deepcopy(current["content"])
    changed["claims"][0]["text"] = "Revenue changed"
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": changed})
    assert saved.status_code == 201, saved.text
    current = saved.json()["versions"][-1]
    assert current["content"]["claims"][0]["support_status"] == "incomplete"
    changed = deepcopy(current["content"])
    changed["claims"][0]["citations"] = [{"source_id": source["source_id"], "quotation": "1000"}]
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": changed})
    assert saved.status_code == 201, saved.text
    assert saved.json()["versions"][-1]["content"]["claims"][0]["support_status"] == "not_independently_verified"


def test_recalculation_rejects_an_explicitly_ambiguous_fact_location(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(app)
    registration = client.post("/v1/auth/local/register", json={"email": "ambiguous-calc@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registration.json()["access_token"]}
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Growth?"}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    current = {"fact_id": "current", "label": "Revenue", "value": "100", "unit": "USD", "currency": "USD", "period": "2024",
               "location": {"status": "ambiguous", "matches": []}}
    previous = {"fact_id": "prior", "label": "Revenue", "value": "80", "unit": "USD", "currency": "USD", "period": "2023"}
    inputs = [{key: fact[key] for key in ("fact_id", "value", "unit", "currency", "period")} for fact in (current, previous)]
    content = {"facts": [current, previous], "calculations": [{"formula_id": "growth_percent", "formula_version": "1",
                "inputs": inputs, "value": "25", "unit": "percent"}]}
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": topic["versions"][-1]["version_id"], "content": content})
    assert saved.status_code == 201, saved.text
    version = saved.json()["versions"][-1]
    response = client.post(path + "/calculations/recalculate", headers=headers,
                           json={"parent_version_id": version["version_id"], "calculation_index": 0})
    assert response.status_code == 422


def test_basis_point_warnings_recompute_after_edit_and_preserve_history(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    client = TestClient(app)
    auth = client.post('/v1/auth/local/register', json={'email': f'basis-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source = client.post('/v2/research/sources', headers=headers, json={'title': 'Statement', 'text': 'The rate increased by 0.25 percentage point. Further increases are anticipated.', 'permissions': {'store': True, 'display': True, 'model_input': True}}).json()
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'What happened?', 'source_ids': [source['source_id']]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    content = {'claims': [{'text': 'Rates increased by 25 basis points.', 'citations': [{'source_id': source['source_id'], 'quotation': 'Further increases are anticipated.'}]}]}
    saved = client.post(path + '/versions', headers=headers, json={'parent_version_id': topic['versions'][-1]['version_id'], 'content': content})
    assert saved.status_code == 201, saved.text
    current = saved.json()['versions'][-1]
    assert current['content']['claims'][0]['uncited_basis_points'] == ['25 basis points']
    edited = deepcopy(current['content'])
    edited['claims'][0]['citations'] = [{'source_id': source['source_id'], 'quotation': 'increased by 0.25 percentage point'}]
    saved = client.post(path + '/versions', headers=headers, json={'parent_version_id': current['version_id'], 'content': edited})
    assert saved.status_code == 201, saved.text
    assert not saved.json()['versions'][-1]['content']['claims'][0].get('uncited_basis_points')
    assert saved.json()['versions'][-2]['content']['claims'][0]['uncited_basis_points'] == ['25 basis points']
