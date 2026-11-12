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


def test_full_export_discloses_input_precision_for_existing_versions(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    client = TestClient(app)
    auth = client.post('/v1/auth/local/register', json={'email': f'precision-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Compare figures', 'scope': '2024', 'as_of': '2025-01-01T00:00:00Z'}).json()
    path = '/v2/research/topics/' + topic['topic_id'] + '/versions/' + topic['versions'][0]['version_id']
    result = client.get(path + '/export', headers=headers)
    assert result.status_code == 200
    exported = result.json()
    assert exported['request']['scope'] == '2024'
    assert exported['calculation_disclosure'] == 'Calculations use the recorded inputs; their source precision and prior rounding are not established.'
    assert 'calculation_disclosure' not in client.get(path + '/export/metadata', headers=headers).json()


def test_edited_source_fact_reports_stale_value_and_untrusted_location(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    client = TestClient(app)
    auth = client.post('/v1/auth/local/register', json={'email': f'fact-source-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source = client.post('/v2/research/sources', headers=headers, json={'title': 'Report', 'text': 'Revenue 100. Revenue 200.', 'permissions': {'store': True, 'display': True, 'model_input': True}}).json()
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?', 'source_ids': [source['source_id']]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    fact = {'fact_id': 'revenue', 'label': 'Revenue', 'value': '100', 'unit': 'USD', 'currency': 'USD', 'period': '2024', 'source_id': source['source_id'], 'quotation': 'Revenue 100', 'location': {'character_start': 0, 'character_end': 11}}
    first = client.post(path + '/versions', headers=headers, json={'parent_version_id': topic['versions'][-1]['version_id'], 'content': {'facts': [fact]}})
    assert first.status_code == 201, first.text
    old = first.json()['versions'][-1]
    assert not old['content']['facts'][0].get('source_value_warnings')
    changed = deepcopy(old['content']);changed['facts'][0]['value'] = '200';changed['facts'][0]['source_value_warnings'] = []
    second = client.post(path + '/versions', headers=headers, json={'parent_version_id': old['version_id'], 'content': changed})
    assert second.status_code == 201, second.text
    current = second.json()['versions'][-1]
    assert current['content']['facts'][0]['source_value_warnings'] == ['value_missing']
    assert current['content']['facts'][0]['verification'] == 'user_edited_not_independently_verified'
    assert not second.json()['versions'][-2]['content']['facts'][0].get('source_value_warnings')
    corrected = deepcopy(current['content']);corrected['facts'][0]['quotation'] = 'Revenue 200';corrected['facts'][0]['location'] = {'character_start': 13, 'character_end': 24}
    third = client.post(path + '/versions', headers=headers, json={'parent_version_id': current['version_id'], 'content': corrected})
    assert third.status_code == 201, third.text
    assert not third.json()['versions'][-1]['content']['facts'][0].get('source_value_warnings')
    invalid = deepcopy(third.json()['versions'][-1]['content']);invalid['facts'][0]['location'] = {'character_start': True, 'character_end': 24}
    fourth = client.post(path + '/versions', headers=headers, json={'parent_version_id': third.json()['versions'][-1]['version_id'], 'content': invalid})
    assert fourth.status_code == 201, fourth.text
    assert fourth.json()['versions'][-1]['content']['facts'][0]['source_value_warnings'] == ['location_mismatch']
    detached = deepcopy(fourth.json()['versions'][-1]['content']);detached['facts'][0]['source_id'] = None
    fifth = client.post(path + '/versions', headers=headers, json={'parent_version_id': fourth.json()['versions'][-1]['version_id'], 'content': detached})
    assert fifth.status_code == 201, fifth.text
    assert fifth.json()['versions'][-1]['content']['facts'][0]['source_value_warnings'] == ['source_missing']


def test_edited_fact_source_checks_bound_failed_lookups(monkeypatch):
    from uuid import uuid4
    import app.main as api
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    calls = []
    original = api.locate_user_quotation
    def bounded(source, quotation):
        calls.append(quotation)
        return original(source, quotation)
    monkeypatch.setattr(api, 'locate_user_quotation', bounded)
    client = TestClient(app)
    auth = client.post('/v1/auth/local/register', json={'email': f'fact-budget-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source = client.post('/v2/research/sources', headers=headers, json={'title': 'Report', 'text': 'Revenue 100.', 'permissions': {'store': True, 'display': True}}).json()
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?', 'source_ids': [source['source_id']]}).json()
    facts = [{'fact_id': f'f{i}', 'label': 'Revenue', 'value': '100', 'unit': 'USD', 'period': '2024', 'source_id': source['source_id'], 'quotation': f'not found {i}'} for i in range(201)]
    path = '/v2/research/topics/' + topic['topic_id'] + '/versions'
    result = client.post(path, headers=headers, json={'parent_version_id': topic['versions'][-1]['version_id'], 'content': {'facts': facts}})
    assert result.status_code == 201, result.text
    saved = result.json()['versions'][-1]['content']['facts']
    assert len(calls) == 200
    assert saved[199]['source_value_warnings'] == ['quotation_unlocated']
    assert saved[200]['source_value_warnings'] == ['not_checked']


def test_edited_table_fragment_preserves_currency_warning(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    client = TestClient(app)
    auth = client.post('/v1/auth/local/register', json={'email': f'currency-fragment-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    text = '| Revenue | $ | 100 |\n| Revenue | USD 200 |'
    source = client.post('/v2/research/sources', headers=headers, json={'title': 'Report', 'text': text, 'permissions': {'store': True, 'display': True, 'model_input': True}}).json()
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?', 'source_ids': [source['source_id']]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    quote = 'Revenue | $ | 100'
    start = text.index(quote)
    fact = {'fact_id': 'revenue', 'label': 'Revenue', 'value': '100', 'unit': 'million', 'currency': 'USD', 'period': '2024', 'source_id': source['source_id'], 'quotation': quote, 'location': {'character_start': start, 'character_end': start + len(quote)}}
    first = client.post(path + '/versions', headers=headers, json={'parent_version_id': topic['versions'][-1]['version_id'], 'content': {'facts': [fact]}})
    assert first.status_code == 201, first.text
    original = first.json()['versions'][-1]
    assert original['content']['facts'][0]['currency_quote_missing'] is True
    assert original['content']['facts'][0]['table_alignment_unverified'] is True
    edited = deepcopy(original['content'])
    edited['facts'][0]['period'] = '2023'
    edited['facts'][0]['currency_quote_missing'] = False
    edited['facts'][0]['table_alignment_unverified'] = False
    second = client.post(path + '/versions', headers=headers, json={'parent_version_id': original['version_id'], 'content': edited})
    assert second.status_code == 201, second.text
    current = second.json()['versions'][-1]
    assert current['content']['facts'][0]['currency_quote_missing'] is True
    assert current['content']['facts'][0]['table_alignment_unverified'] is True
    assert client.get(path, headers=headers).json()['versions'][-1]['content']['facts'][0]['currency_quote_missing'] is True
    corrected = deepcopy(current['content'])
    quote = 'Revenue | USD 200'
    start = text.index(quote)
    corrected['facts'][0].update(value='200', quotation=quote, location={'character_start': start, 'character_end': start + len(quote)})
    third = client.post(path + '/versions', headers=headers, json={'parent_version_id': current['version_id'], 'content': corrected})
    assert third.status_code == 201, third.text
    assert not third.json()['versions'][-1]['content']['facts'][0].get('currency_quote_missing')
    assert third.json()['versions'][-1]['content']['facts'][0]['table_alignment_unverified'] is True


def test_create_calculation_from_saved_facts_validates_inputs_and_preserves_history(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE","mock")
    client=TestClient(app)
    account=client.post("/v1/auth/local/register",json={"email":f"create-calc-{uuid4().hex}@example.test",
        "password":"correct horse battery staple!"}).json()
    headers={"Authorization":"Bearer "+account["access_token"]}
    topic=client.post("/v2/research/topics",headers=headers,json={"question":"Compare revenue"}).json()
    path="/v2/research/topics/"+topic["topic_id"]
    facts=[{"fact_id":"old","label":"Old revenue","value":"100","unit":"USD million","currency":"USD","period":"2023"},
           {"fact_id":"new","label":"New revenue","value":"125","unit":"USD million","currency":"USD","period":"2024"},
           {"fact_id":"zero","label":"Zero","value":"0","unit":"USD million","currency":"USD","period":"2022"},
           {"fact_id":"other","label":"Other","value":"5","unit":"EUR million","currency":"EUR","period":"2024"}]
    saved=client.post(path+"/versions",headers=headers,json={"parent_version_id":topic["versions"][0]["version_id"],"content":{"facts":facts}})
    assert saved.status_code==201
    parent=saved.json()["versions"][-1]["version_id"]
    spec={"parent_version_id":parent,"formula":"growth_percent","first_fact_id":"new","second_fact_id":"old"}
    for invalid in ({**spec,"first_fact_id":"missing"},{**spec,"second_fact_id":"new"},{**spec,"second_fact_id":"zero"},
                    {**spec,"second_fact_id":"other"},{**spec,"formula":"unknown"}):
        assert client.post(path+"/calculations",headers=headers,json=invalid).status_code==422
    assert client.get(path,headers=headers).json()["versions"][-1]["version_id"]==parent
    result=client.post(path+"/calculations",headers=headers,json=spec)
    assert result.status_code==201, result.text
    previous,current=result.json()["versions"][-2:]
    assert not previous["content"]["calculations"]
    calc=current["content"]["calculations"][0]
    assert calc["value"]=="25.00" and calc["unit"]=="percent" and calc["input_status"]=="matched"
    assert [x["fact_id"] for x in calc["inputs"]]==["new","old"]
    assert calc["authorship"]=="calculation_engine" and current["review"]=="draft"
    assert client.post(path+"/calculations",headers=headers,json=spec).status_code==409
