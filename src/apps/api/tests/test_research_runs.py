import time
import threading
from copy import deepcopy
from threading import Event
from datetime import date, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

import app.main as api
from app.glm_mcp_search import SearchCandidate
from app.research_topics import ResearchContent
from app.research_analysis import AnalysisInterrupted
from app.research_verification import REVIEW_SYSTEM_PROMPT


@pytest.mark.parametrize('mode', ['batch', 'isolated'])
def test_review_of_flagged_claim_is_diagnostic_only_even_if_model_says_supports(monkeypatch, mode):
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    source_id = ''
    class Provider:
        name = 'guarded-review'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            if system == REVIEW_SYSTEM_PROMPT:
                return {'reviews': [{'claim_index': 0, 'verdict': 'supports'}]}, 5
            return {'claims': [{'text': 'Revenue was 100 in Q1 2025 due to a pandemic.',
                'citations': [{'source_id': source_id, 'quotation': 'Revenue was 100.'}]}]}, 10
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    reg = client.post('/v1/auth/local/register', json={'email': f'guarded-{uuid4().hex}@example.test',
        'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + reg['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title': 'Report',
        'text': 'Revenue was 100.', 'permissions': {'store': True, 'display': True, 'model_input': True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?', 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    run = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'guarded-review'}, json={
        'parent_version_id': topic['versions'][0]['version_id'], 'require_counterarguments': False,
        'support_review': True, 'support_review_mode': mode, 'max_model_calls': 2})
    assert run.status_code == 202
    op = run.json()['operation']['operation_id']
    for _ in range(100):
        status = client.get(path + '/runs/' + op, headers=headers).json()['operation']['status']
        if status in {'completed', 'failed'}: break
        time.sleep(.02)
    assert status == 'completed'
    content = client.get(path, headers=headers).json()['versions'][-1]['content']
    assert content['run']['status'] == 'partial' and content['run']['model_calls'] == 2
    claim = content['claims'][0]
    assert claim['uncited_quarters'] and claim['causal_attribution_unverified']
    assert claim['support_review']['verdict'] == 'uncertain'
    assert claim['support_review']['reported_verdict'] == 'supports'
    assert claim['support_review']['diagnostic_only'] is True
    assert set(claim['support_review']['guard_reasons']) == {'uncited_quarters', 'causal_attribution_unverified'}


def test_legacy_annual_rate_difference_is_warned_without_rewriting_history(monkeypatch):
    from uuid import uuid4
    from app.research_topics import VersionInput
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    client = TestClient(api.app)
    reg = client.post('/v1/auth/local/register', json={'email': f'legacy-rate-{uuid4().hex}@example.test',
        'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + reg['access_token']}
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Rates?'}).json()
    facts = [{'fact_id': 'a', 'value': '1.3', 'unit': 'annual percent', 'currency': None, 'period': '2024 Q1'},
             {'fact_id': 'b', 'value': '3.4', 'unit': 'annual percent', 'currency': None, 'period': '2023 Q4'}]
    calculation = {'formula_id': 'difference', 'formula_version': '1', 'inputs': facts, 'value': '-2.10',
                   'unit': 'annual percent', 'decimal_places': 2, 'input_status': 'matched'}
    stored = api.research_topics.add_version(topic['owner_id'], topic['topic_id'], VersionInput(
        parent_version_id=topic['versions'][0]['version_id'], content=ResearchContent(facts=facts, calculations=[calculation])))
    version = stored.versions[-1]
    path = '/v2/research/topics/' + topic['topic_id']
    for response in (client.get(path, headers=headers).json()['versions'][-1],
                     client.get(path + '/versions/' + version.version_id, headers=headers).json(),
                     client.get(path + '/versions/' + version.version_id + '/export', headers=headers).json()['version']):
        assert response['content']['calculations'][0]['legacy_unit_warning'] == 'percentage_difference_requires_recalculation'
        assert response['content']['calculations'][0]['unit'] == 'annual percent'
    assert 'legacy_unit_warning' not in api.research_topics.get(topic['owner_id'], topic['topic_id']).versions[-1].content.calculations[0]
    recalc = client.post(path + '/calculations/recalculate', headers=headers,
        json={'parent_version_id': version.version_id, 'calculation_index': 0})
    assert recalc.status_code == 201, recalc.text
    current = recalc.json()['versions'][-1]['content']['calculations'][0]
    assert current['value'] == '-2.10' and current['unit'] == 'percentage points' and current['formula_version'] == '2'
    assert 'legacy_unit_warning' not in current


def test_provisional_html_calculation_remains_partial_through_export_and_edit(monkeypatch):
    import base64
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    source_id = ''
    class Provider:
        name = 'table-provisional-lifecycle'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def fact(key, value, period):
                return {'fact_id': key, 'label': 'Revenue', 'value': value, 'unit': 'USD million', 'currency': 'USD',
                        'period': period, 'citation': {'source_id': source_id, 'quotation': 'Revenue | 100 | 80'}}
            return {'claims': [{'text': 'Revenue is reported.', 'citations': [
                        {'source_id': source_id, 'quotation': 'Revenue is reported.'}]}],
                    'facts': [fact('current', '100', 'Fiscal year ended July 27, 2024'),
                              fact('prior', '80', 'Fiscal year ended July 29, 2023')],
                    'calculations': [{'formula': 'growth_percent', 'first_fact_id': 'current', 'second_fact_id': 'prior'}]}, 20
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    original_analyze = api.analyze
    def without_diagnostic_wording(*args, **kwargs):
        from dataclasses import replace
        return replace(original_analyze(*args, **kwargs), limitations=[])
    # Completion must depend on input status, not matching a warning string.
    monkeypatch.setattr(api, 'analyze', without_diagnostic_wording)
    client = TestClient(api.app)
    reg = client.post('/v1/auth/local/register', json={'email': f'provisional-{uuid4().hex}@example.test',
        'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + reg['access_token']}
    html = ('<p>Revenue is reported.</p><table><tr><th colspan="2">Years Ended</th>'
            '<th colspan="2">July 27, 2024</th><th colspan="2">July 29, 2023</th></tr>'
            '<tr><td colspan="2">Revenue</td><td colspan="2">100</td><td colspan="2">80</td></tr></table>')
    source_id = client.post('/v2/research/sources/html', headers=headers, json={'title': 'Report',
        'html_base64': base64.b64encode(html.encode()).decode(),
        'permissions': {'store': True, 'model_input': True, 'display': True, 'export': True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?', 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    run = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'provisional-html'},
        json={'parent_version_id': topic['versions'][0]['version_id'], 'require_counterarguments': False})
    assert run.status_code == 202
    op = run.json()['operation']['operation_id']
    for _ in range(100):
        status = client.get(path + '/runs/' + op, headers=headers).json()['operation']['status']
        if status in {'completed', 'failed'}: break
        time.sleep(.02)
    assert status == 'completed'
    original = client.get(path, headers=headers).json()['versions'][-1]
    assert original['content']['run']['status'] == 'partial'
    calculation = original['content']['calculations'][0]
    assert calculation['value'] == '25.00' and calculation['input_status'] == 'unverifiable'
    assert any('provisional' in item for item in original['content']['limitations'])
    export = client.get(path + '/versions/' + original['version_id'] + '/export', headers=headers)
    assert export.status_code == 200
    assert export.json()['version']['content']['calculations'][0]['input_status'] == 'unverifiable'
    edited = deepcopy(original['content'])
    edited['facts'][0]['period'] = 'Fiscal year ended July 28, 2024'
    edited['facts'][0]['table_structure_alignment'] = {'status': 'matched'}
    saved = client.post(path + '/versions', headers=headers, json={'parent_version_id': original['version_id'], 'content': edited})
    assert saved.status_code == 201, saved.text
    latest = saved.json()['versions'][-1]
    assert 'table_structure_alignment' not in latest['content']['facts'][0]
    assert latest['content']['calculations'][0]['input_status'] == 'unverifiable'
    assert saved.json()['versions'][-2]['content']['facts'][0]['table_structure_alignment']['status'] == 'matched'
    assert client.post(path + '/facts/confirm-table', headers=headers,
        json={'parent_version_id': latest['version_id'], 'fact_id': 'current'}).status_code == 422
    assert client.post(path + '/calculations/recalculate', headers=headers,
        json={'parent_version_id': latest['version_id'], 'calculation_index': 0}).status_code == 422


@pytest.mark.parametrize("required,bad_citation,expected", [
    (True, False, "partial"),
    (False, False, "completed"),
    (False, True, "partial"),
])
def test_counterargument_requirement_can_be_disabled_for_fact_checks(monkeypatch, required, bad_citation, expected):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""

    class Provider:
        name = "fact-check-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            quotation = "invented" if bad_citation else "Revenue was 100"
            return {"claims": [{"text": "Revenue was 100", "citations": [
                {"source_id": source_id, "quotation": quotation}]}], "counterarguments": []}, 10

    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    account = client.post("/v1/auth/local/register", json={"email": f"facts-{required}-{bad_citation}@example.test",
        "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + account["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report",
        "text": "Revenue was 100.", "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    payload = {"parent_version_id": topic["versions"][0]["version_id"], "require_counterarguments": required}
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "fact-check-requirement"}, json=payload)
    assert receipt.status_code == 202
    op = receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        state = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if state in {"completed", "failed"}: break
        time.sleep(0.02)
    assert state == "completed"
    content = client.get(path, headers=headers).json()["versions"][-1]["content"]
    assert content["run"]["status"] == expected
    assert content["run"]["require_counterarguments"] is required
    assert any("No counterarguments were provided" in item for item in content["limitations"])
    snapshot = client.get(path + "/runs/" + op + "/inputs", headers=headers).json()
    assert snapshot["budget"].get("require_counterarguments", True) is required
    # Old requests and explicit default true have identical idempotency payloads.
    if required:
        replay = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "fact-check-requirement"},
                             json={"parent_version_id": payload["parent_version_id"]})
        assert replay.status_code == 202 and replay.headers["Idempotency-Replayed"] == "true"


def test_research_run_commits_evidence_once_and_replays(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    calls = []

    class Provider:
        name = "test-provider"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)

        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            calls.append(prompt)
            assert "untrusted data" in system
            assert 0 < timeout_seconds <= 5
            return {"claims": [{"text": "Revenue is reported", "citations": [
                {"source_id": source_id, "quotation": "Revenue was 100"}]}],
                "counterarguments": [{"text": "Revenue alone does not establish profit growth", "citations": [
                    {"source_id": source_id, "quotation": "Revenue was 100"}]}],
                "limitations": [], "facts": [
                    {"fact_id": "current", "label": "Revenue", "value": "100", "unit": "USD million", "currency": "USD", "period": "2023", "citation": {"source_id": source_id, "quotation": "Revenue was 100 million in 2023."}},
                    {"fact_id": "prior", "label": "Revenue", "value": "80", "unit": "USD million", "currency": "USD", "period": "2022", "citation": {"source_id": source_id, "quotation": "Revenue was 80 million in 2022."}}],
                "calculations": [{"formula": "growth_percent", "first_fact_id": "current", "second_fact_id": "prior"}]}, 55

    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    registered = client.post("/v1/auth/local/register", json={"email": "a@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registered.json()["access_token"]}
    source = client.post("/v2/research/sources", headers=headers, json={"title": "Report",
        "text": "Revenue was 100 million in 2023. Revenue was 80 million in 2022.",
        "event_at": "2023-12-31T00:00:00Z", "published_at": "2024-01-01T00:00:00Z",
        "revised_at": "2024-02-01T00:00:00Z", "permissions": {"store": True,
        "model_input": True, "display": True}}).json()
    source_id = source["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={
        "question": "What is revenue?"}).json()
    topic_path = "/v2/research/topics/" + topic["topic_id"]
    attached = client.post(topic_path + "/sources", headers=headers, json={
        "parent_version_id": topic["versions"][-1]["version_id"], "source_ids": [source_id]})
    assert attached.status_code == 201
    topic = attached.json()
    run_path = topic_path + "/runs"
    payload = {"parent_version_id": topic["versions"][-1]["version_id"]}
    request_headers = {**headers, "Idempotency-Key": "research-request-0001"}
    started = client.post(run_path, headers=request_headers, json=payload)
    assert started.status_code == 202, started.text
    operation_id = started.json()["operation"]["operation_id"]
    inputs_path = run_path + "/" + operation_id + "/inputs"
    inputs = client.get(inputs_path, headers=headers)
    assert inputs.status_code == 200
    snapshot = inputs.json()
    assert snapshot["parent_version_id"] == payload["parent_version_id"]
    assert snapshot["sources"][0]["source_id"] == source_id
    assert snapshot["sources"][0]["permissions"]["model_input"] is True
    assert snapshot["sources"][0]["event_at"].startswith("2023-12-31")
    assert snapshot["sources"][0]["published_at"].startswith("2024-01-01")
    assert snapshot["sources"][0]["revised_at"].startswith("2024-02-01")
    assert snapshot["sources"][0]["obtained_at"]
    assert snapshot["budget"]["max_model_calls"] == 1
    assert snapshot["selection_method"] == "ranked_nonoverlapping_windows_v49"
    assert "Revenue was 100" not in inputs.text and "owner_id" not in inputs.json()
    other = client.post("/v1/auth/local/register", json={"email": "other@example.test",
        "password": "correct horse battery staple!"}).json()["access_token"]
    assert client.get(inputs_path, headers={"Authorization": "Bearer " + other}).status_code == 404
    for _ in range(100):
        state = client.get(run_path + "/" + operation_id, headers=headers)
        if state.json()["operation"]["status"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert state.json()["operation"]["status"] == "completed", state.text
    updated = client.get(topic_path, headers=headers).json()
    assert len(updated["versions"]) == 3
    assert [v["created_by"] for v in updated["versions"]] == ["user", "user", "agent"]
    result = updated["versions"][-1]["content"]
    assert result["run"]["status"] == "completed"
    assert result["run"]["cost_status"] == "unknown"
    assert result["run"]["selection_method"] == snapshot["selection_method"]
    assert result["sources"][0]["event_at"] == snapshot["sources"][0]["event_at"]
    assert result["sources"][0]["revised_at"] == snapshot["sources"][0]["revised_at"]
    assert result["claims"][0]["citations"][0]["quotation"] == "Revenue was 100"
    assert result["calculations"][0]["value"] == "25.00"
    assert result["facts"][0]["verification"] == "model_extraction_not_independently_verified"
    assert updated["versions"][-1]["source_ids"] == [source_id]
    replay = client.post(run_path, headers=request_headers, json=payload)
    assert replay.status_code == 202, replay.text
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert len(calls) == 1
    assert client.get(inputs_path, headers=headers).json() == snapshot
    runs = client.get(run_path, headers=headers)
    assert runs.status_code == 200
    assert runs.json()[0]["operation"]["operation_id"] == operation_id

    # A later user edit cannot erase source permissions from version lineage.
    edited = deepcopy(result)
    edited["summary"] = "Edited"
    edited["sources"] = []
    edited["run"] = {"status": "fabricated"}
    edited["claims"][0]["text"] = "User changed the finding"
    edited["claims"][0]["support_status"] = "verified"
    edited["facts"][0]["value"] = "999"
    edited["calculations"][0]["value"] = "999"
    scrubbed = client.post(topic_path + "/versions", headers=headers, json={
        "parent_version_id": updated["versions"][-1]["version_id"], "content": edited,
        "review": "confirmed", "note": "My retained reservation"})
    assert scrubbed.status_code == 201
    version = scrubbed.json()["versions"][-1]
    assert version["created_by"] == "user"
    assert version["content"]["run"] == result["run"]
    assert version["content"]["sources"] == result["sources"]
    assert version["content"]["claims"][0]["authorship"] == "user"
    assert version["content"]["claims"][0]["support_status"] == "not_independently_verified"
    assert version["content"]["facts"][0]["verification"] == "user_edited_not_independently_verified"
    assert version["content"]["calculations"][0]["authorship"] == "user"
    assert version["content"]["calculations"][0]["input_status"] == "changed"
    assert "authorship" not in version["content"]["claims"][1]
    assert version["source_ids"] == [source_id]
    export_path = topic_path + "/versions/" + version["version_id"] + "/export"
    assert client.get(export_path, headers=headers).status_code == 403

    # When pricing is unknown, an explicit currency cap prevents the call.
    new_parent = version["version_id"]
    capped = client.post(run_path, headers={**headers, "Idempotency-Key": "research-request-0002"},
                         json={"parent_version_id": new_parent, "max_cost_usd": "0.10"})
    assert capped.status_code == 202
    second_id = capped.json()["operation"]["operation_id"]
    for _ in range(100):
        second = client.get(run_path + "/" + second_id, headers=headers)
        if second.json()["operation"]["status"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert second.json()["operation"]["status"] == "completed"
    capped_result = client.get(topic_path, headers=headers).json()["versions"][-1]["content"]
    capped_version = client.get(topic_path, headers=headers).json()["versions"][-1]
    assert capped_version["review"] == "draft"
    assert capped_result["summary"] == "Edited"
    assert capped_version["note"] == "My retained reservation"
    assert capped_version["parent_version_id"] == version["version_id"]
    assert capped_result["run"]["status"] == "partial"
    assert capped_result["run"]["error"] == "provider_budget_unknown"
    assert capped_result["run"]["model_calls"] == 0
    assert len(calls) == 1

    latest = client.get(topic_path, headers=headers).json()["versions"][-1]["version_id"]
    token_limited = client.post(run_path, headers={**headers, "Idempotency-Key": "research-token-budget"},
        json={"parent_version_id": latest, "max_total_tokens": 100})
    token_id = token_limited.json()["operation"]["operation_id"]
    for _ in range(100):
        token_status = client.get(run_path + "/" + token_id, headers=headers).json()["operation"]["status"]
        if token_status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert token_status == "completed"
    limited_result = client.get(topic_path, headers=headers).json()["versions"][-1]["content"]
    assert limited_result["run"]["error"] == "provider_budget_exhausted"
    assert limited_result["run"]["model_calls"] == 0 and len(calls) == 1
    assert client.delete("/v1/auth/local/account", headers=headers).status_code == 200
    assert api.RESEARCH_RUN_INPUTS.get(operation_id) is None


def test_cancelled_run_does_not_save_late_model_output(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    entered, release = Event(), Event()

    class SlowProvider:
        name = "slow-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            entered.set()
            assert release.wait(5)
            return {"claims": [], "counterarguments": [], "limitations": []}, 10

    monkeypatch.setattr(api, "challenge_provider", SlowProvider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    registered = client.post("/v1/auth/local/register", json={"email": "cancel@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registered.json()["access_token"]}
    source = client.post("/v2/research/sources", headers=headers, json={"title": "Report",
        "text": "Revenue was 100.", "permissions": {"store": True, "model_input": True, "display": True}}).json()
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?",
        "source_ids": [source["source_id"]]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    started = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "research-cancel-1"},
                          json={"parent_version_id": topic["versions"][0]["version_id"]}).json()["operation"]
    assert entered.wait(5)
    restored = client.get(path + "/runs", headers=headers).json()
    assert restored[0]["operation"]["operation_id"] == started["operation_id"]
    stopped = client.post("/v1/operations/" + started["operation_id"] + "/cancel",
        headers={**headers, "Idempotency-Key": "cancel-research-1"}, json={"cancel_token": started["cancel_token"]})
    assert stopped.status_code == 200
    assert stopped.json()["operation"]["status"] == "cancelled"
    release.set()
    time.sleep(0.1)
    assert len(client.get(path, headers=headers).json()["versions"]) == 1


def test_cancelling_support_review_keeps_completed_generation_as_draft(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    entered, release = Event(), Event()
    source_id = ""
    class Provider:
        name = "review-cancellation-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            if system == REVIEW_SYSTEM_PROMPT:
                entered.set()
                assert release.wait(5)
                return {"reviews": [{"claim_index": 0, "verdict": "supports"}]}, 10
            citation = {"source_id": source_id, "quotation": "Revenue was 100"}
            return {"claims": [{"text": "Revenue was 100", "citations": [citation]}],
                    "counterarguments": [], "facts": [], "calculations": [], "limitations": []}, 55
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    registered = client.post("/v1/auth/local/register", json={"email": "review-cancel@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registered.json()["access_token"]}
    source = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Revenue was 100.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()
    source_id = source["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    started = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "review-cancel-run"}, json={
        "parent_version_id": topic["versions"][0]["version_id"], "support_review": True,
        "max_model_calls": 2, "max_total_tokens": 9000}).json()["operation"]
    assert entered.wait(5)
    versions = client.get(path, headers=headers).json()["versions"]
    assert len(versions) == 2 and versions[-1]["review"] == "draft"
    checkpoint = versions[-1]["content"]
    assert checkpoint["run"]["checkpoint"] == "generation" and checkpoint["run"]["status"] == "partial"
    assert checkpoint["run"]["cost_status"] == "unknown" and checkpoint["run"]["generation_tokens"] == 55
    assert checkpoint["run"]["max_model_calls"] == 2 and checkpoint["run"]["source_hashes"][source_id] == source["content_sha256"]
    assert checkpoint["claims"][0]["citations"][0]["quotation"] == "Revenue was 100"
    stopped = client.post("/v1/operations/" + started["operation_id"] + "/cancel",
        headers={**headers, "Idempotency-Key": "cancel-review-run"}, json={"cancel_token": started["cancel_token"]})
    assert stopped.status_code == 200 and stopped.json()["operation"]["status"] == "cancelled"
    release.set()
    time.sleep(0.1)
    assert client.get(path, headers=headers).json()["versions"] == versions
    assert client.delete("/v1/auth/local/account", headers=headers).status_code == 200


def test_selection_timeout_produces_partial_result_without_calling_model(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.setattr(api, "analyze", lambda *args, **kwargs: (_ for _ in ()).throw(AnalysisInterrupted()))
    client = TestClient(api.app)
    registration = client.post("/v1/auth/local/register", json={"email": "selection-timeout@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registration.json()["access_token"]}
    source = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Data",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Data?", "source_ids": [source["source_id"]]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    started = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "selection-timeout-1"},
        json={"parent_version_id": topic["versions"][0]["version_id"], "max_elapsed_seconds": 1}).json()
    operation_id = started["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + operation_id, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert status == "completed"
    content = client.get(path, headers=headers).json()["versions"][-1]["content"]
    assert content["run"]["status"] == "partial" and content["run"]["model_calls"] == 0
    assert content["run"]["error"] == "provider_budget_exhausted"
    assert content["claims"] == []


@pytest.mark.parametrize("budget,second_verdict,first_usage,output_limit,expected_calls,expected_review", [
    (9000, "contradicts", 55, 1500, 2, "complete"),
    (9000, "supports", 55, 1500, 2, "complete"),
    (4500, "supports", 55, 1500, 1, "budget_exhausted"),
    (5000, "supports", 55, 1500, 2, "complete"),
    (5000, "supports", 1000, 1500, 1, "budget_exhausted"),
    (5000, "supports", None, 1500, 1, "budget_exhausted"),
    (5000, "supports", -1, 1500, 1, "budget_exhausted"),
    (9000, "supports", None, 1500, 2, "complete"),
    (9000, "invalid", 55, 1500, 2, "failed"),
    (9000, "provider_failure", 55, 1500, 2, "failed"),
    (10000, "supports", 3000, 4000, 2, "complete"),
    (10000, "supports", 3001, 4000, 1, "budget_exhausted"),
])
def test_optional_support_review_is_bounded_and_preserves_initial_research(monkeypatch, budget, second_verdict, first_usage, output_limit, expected_calls, expected_review):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    calls = []
    source_id = ""
    class Provider:
        name = "review-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=output_limit)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            calls.append(system)
            if system == REVIEW_SYSTEM_PROMPT:
                assert "Revenue was 100" in prompt
                if second_verdict == "provider_failure":
                    from app.phase3 import ProviderFailure
                    raise ProviderFailure("invalid_provider_output", "Invalid output.", retryable=False, reported_tokens=12)
                return {"reviews": [{"claim_index": 0, "verdict": "supports"},
                                    {"claim_index": 1, "verdict": second_verdict}]}, 12
            citation = {"source_id": source_id, "quotation": "Revenue was 100"}
            return {"claims": [{"text": "Revenue was 100", "citations": [citation]}],
                    "counterarguments": [{"text": "Revenue does not establish profit", "citations": [citation]}],
                    "limitations": [], "facts": [], "calculations": []}, first_usage
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    registration = client.post("/v1/auth/local/register", json={"email": f"review-{budget}-{second_verdict}-{first_usage}-{output_limit}@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registration.json()["access_token"]}
    source = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Revenue was 100.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()
    source_id = source["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    response = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "support-review-test-1"},
        json={"parent_version_id": topic["versions"][0]["version_id"], "max_model_calls": 2,
              "max_total_tokens": budget, "support_review": True})
    assert response.status_code == 202
    op = response.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert status == "completed"
    versions = client.get(path, headers=headers).json()["versions"]
    current = versions[-1]
    content, run = current["content"], current["content"]["run"]
    assert len(calls) == expected_calls == run["model_calls"]
    assert run["support_review_status"] == expected_review
    assert len(content["claims"]) == 2
    if expected_review == "budget_exhausted":
        assert run["support_review_omissions"] == [{"claim_index": 0, "reason": "token_budget"}, {"claim_index": 1, "reason": "token_budget"}]
    else:
        assert run["support_review_omissions"] == []
    assert run["support_review_failed_indexes"] == ([0, 1] if expected_review == "failed" else [])
    first_entry = api.billing_ledger.usage.get("model:" + op)
    accounted_first = first_usage if type(first_usage) is int and first_usage >= 0 else None
    assert first_entry.status == "settled" and first_entry.actual_tokens == (accounted_first if accounted_first is not None else 3000 + output_limit)
    second_entry = api.billing_ledger.usage.get("model:" + op + ":support-review")
    if expected_calls == 2:
        assert len(versions) == 3 and versions[-2]["content"]["run"]["checkpoint"] == "generation"
        assert current["parent_version_id"] == versions[-2]["version_id"]
        assert second_entry.status == "settled" and second_entry.actual_tokens == 12
        assert run["total_tokens"] == (accounted_first + 12 if accounted_first is not None else None)
        assert len(run["support_review_prompt_sha256"]) == 64
    else:
        assert len(versions) == 2
        assert second_entry is None and run["total_tokens"] == accounted_first
    if expected_review == "complete":
        assert content["claims"][1]["support_review"]["verdict"] == second_verdict
        assert content["claims"][1]["support_review"]["independently_verified"] is False
        edited = deepcopy(content)
        edited["claims"][0]["text"] = "Modified conclusion"
        saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": edited})
        assert saved.status_code == 201
        assert "support_review" not in saved.json()["versions"][-1]["content"]["claims"][0]
    else:
        assert all("support_review" not in claim for claim in content["claims"])


def test_invalid_model_output_retains_prompt_provenance_without_output(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    class Provider:
        name = "invalid-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            return {"claims": "sensitive model output"}, 25
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    registered = client.post("/v1/auth/local/register", json={"email": "invalid-provenance@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registered.json()["access_token"]}
    source = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Revenue was 100.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()
    excluded = client.post("/v2/research/sources", headers=headers, json={"title": "Not authorized", "text": "Private source.",
        "permissions": {"store": True, "model_input": False, "display": True}}).json()
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source["source_id"], excluded["source_id"]]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    response = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "invalid-provenance-test"},
                           json={"parent_version_id": topic["versions"][0]["version_id"]})
    operation = response.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + operation, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert status == "completed"
    content = client.get(path, headers=headers).json()["versions"][-1]["content"]
    run = content["run"]
    assert run["error"] == "invalid_model_output"
    assert run["analysis_error_detail"] == "Model output did not match the research schema."
    assert len(run["prompt_sha256"]) == 64 and run["selected_segments"][0]["source_id"] == source["source_id"]
    assert run["status"] == "partial" and run["model_calls"] == 1
    assert [entry["source_id"] for entry in content["sources"]] == [source["source_id"]]
    assert content["sources"][0]["content_sha256"] == source["content_sha256"]
    assert any("processing permission is not granted" in note for note in content["limitations"])
    assert any("selected for analysis" in note for note in content["limitations"])
    assert "sensitive model output" not in str(content)


def test_unlocatable_quote_preserves_valid_work_as_partial(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""
    class Provider:
        name = "partial-citation-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def cite(quote): return {"source_id": source_id, "quotation": quote}
            return {"claims": [{"text": "Revenue is 100", "citations": [cite("Revenue 100")]},
                               {"text": "Unsupported assertion", "citations": [cite("invented quotation")]}],
                    "counterarguments": [{"text": "Revenue is not profit", "citations": [cite("Revenue 100")]}],
                    "facts": [{"fact_id": "r", "label": "Revenue", "value": "100", "unit": "USD", "period": "2023",
                               "citation": cite("Revenue 100")}], "calculations": [], "limitations": []}, 20
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    registered = client.post("/v1/auth/local/register", json={"email": "partial-quotation@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registered.json()["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Revenue 100",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "partial-citation-001"},
        json={"parent_version_id": topic["versions"][0]["version_id"]}).json()
    operation_id = receipt["operation"]["operation_id"]
    for _ in range(100):
        state = client.get(path + "/runs/" + operation_id, headers=headers).json()["operation"]["status"]
        if state in {"completed", "failed"}: break
        time.sleep(0.02)
    assert state == "completed"
    content = client.get(path, headers=headers).json()["versions"][-1]["content"]
    assert content["run"]["status"] == "partial" and len(content["facts"]) == 1
    assert len(content["claims"]) == 2 and "Unsupported assertion" not in str(content)
    assert "invented quotation" not in str(content)
    assert any("quotation" in item and "omitted" in item for item in content["limitations"])


def test_research_marks_cross_source_differences_for_review_and_recomputes_them(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    ids = []
    class Provider:
        name = "conflict-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def cite(index, quote): return {"source_id": ids[index], "quotation": quote}
            return {"claims": [{"text": "Revenue was revised from 100 to 120", "citations": [cite(1, "Revenue 120")]}],
                    "counterarguments": [{"text": "Sources may measure different things", "citations": [cite(1, "Revenue 120")]}],
                    "facts": [{"fact_id": "a", "label": "Revenue", "value": "100", "unit": "USD million",
                               "currency": "USD", "period": "2023", "citation": cite(0, "Revenue 100")},
                              {"fact_id": "b", "label": "Revenue", "value": "120", "unit": "USD million",
                               "currency": "USD", "period": "2023", "citation": cite(1, "Revenue 120")}],
                    "calculations": [], "limitations": []}, 20
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    registration = client.post("/v1/auth/local/register", json={"email": "differences@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registration.json()["access_token"]}
    for text in ("Revenue 100", "Revenue 120"):
        response = client.post("/v2/research/sources", headers=headers, json={"title": text, "text": text,
            "permissions": {"store": True, "model_input": True, "display": True}})
        ids.append(response.json()["source_id"])
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": ids}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "difference-run-001"},
        json={"parent_version_id": topic["versions"][0]["version_id"]}).json()
    operation_id = receipt["operation"]["operation_id"]
    for _ in range(100):
        state = client.get(path + "/runs/" + operation_id, headers=headers).json()["operation"]["status"]
        if state in {"completed", "failed"}: break
        time.sleep(0.02)
    assert state == "completed"
    current = client.get(path, headers=headers).json()["versions"][-1]
    assert current["content"]["run"]["status"] == "partial"
    assert len(current["content"]["evidence_differences"]) == 1
    comparison = current["content"]["claims"][0]
    assert [citation["source_id"] for citation in comparison["citations"]] == [ids[1]]
    assert [item["source_id"] for item in comparison["suggested_fact_evidence"]] == [ids[0]]
    assert comparison["uncited_fact_values"] == ["100 USD million"]
    edited = deepcopy(current["content"])
    edited["facts"][1]["value"] = "100"
    edited["evidence_differences"] = [{"fabricated": True}]
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": edited})
    assert saved.status_code == 201, saved.text
    assert saved.json()["versions"][-1]["content"]["evidence_differences"] == []
    assert not saved.json()["versions"][-1]["content"]["claims"][0].get("suggested_fact_evidence")
    assert len(saved.json()["versions"][-2]["content"]["evidence_differences"]) == 1


def test_claim_calculation_difference_is_saved_and_rechecked_after_edit(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""
    class Provider:
        name = "calculation-conflict-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def cite(quote): return {"source_id": source_id, "quotation": quote}
            return {"claims": [{"text": "On sales of 642,637, the margin is 4.21%.", "citations": [cite("sales 642637")]}],
                    "counterarguments": [{"text": "Revenue alone is not enough", "citations": [cite("income 27012")]}],
                    "facts": [{"fact_id": "profit", "label": "Profit", "value": "27012", "unit": "USD million", "currency": "USD", "period": "2024", "citation": cite("income 27012")},
                              {"fact_id": "sales", "label": "Sales", "value": "642637", "unit": "USD million", "currency": "USD", "period": "2024", "citation": cite("sales 642637")}],
                    "calculations": [{"formula": "margin_percent", "first_fact_id": "profit", "second_fact_id": "sales"}], "limitations": []}, 20
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    registered = client.post("/v1/auth/local/register", json={"email": "claim-calculation-conflict@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registered.json()["access_token"]}
    source = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "income 27012; sales 642637",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()
    source_id = source["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Margin?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "calculation-conflict-001"},
        json={"parent_version_id": topic["versions"][0]["version_id"]}).json()
    operation_id = receipt["operation"]["operation_id"]
    for _ in range(100):
        state = client.get(path + "/runs/" + operation_id, headers=headers).json()["operation"]["status"]
        if state in {"completed", "failed"}: break
        time.sleep(0.02)
    assert state == "completed"
    current = client.get(path, headers=headers).json()["versions"][-1]
    assert current["content"]["run"]["status"] == "partial"
    assert current["content"]["calculation_differences"][0]["calculated_value"] == "4.20"
    edited = deepcopy(current["content"])
    edited["claims"][0]["text"] = "On sales of 642,637, the margin is 4.20%."
    edited["calculation_differences"] = [{"fabricated": True}]
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": edited})
    assert saved.status_code == 201, saved.text
    assert saved.json()["versions"][-1]["content"]["calculation_differences"] == []
    assert len(saved.json()["versions"][-2]["content"]["calculation_differences"]) == 1


def test_candidate_numeric_evidence_is_separate_and_recomputed_after_edit(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""
    class Provider:
        name = "candidate-evidence-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def cite(quote): return {"source_id": source_id, "quotation": quote}
            return {"claims": [{"text": "Annual growth was 2.5 percent and quarter growth was 3.2 percent.",
                                "citations": [cite("Quarter growth 3.2 percent")]}],
                    "counterarguments": [{"text": "Periods differ", "citations": [cite("Annual growth 2.5 percent")]}],
                    "facts": [{"fact_id": "annual", "label": "Annual", "value": "2.5", "unit": "percent", "period": "2023",
                               "citation": cite("Annual growth 2.5 percent")},
                              {"fact_id": "quarter", "label": "Quarter", "value": "3.2", "unit": "percent", "period": "Q4 2023",
                               "citation": cite("Quarter growth 3.2 percent")}], "calculations": [], "limitations": []}, 20
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    registered = client.post("/v1/auth/local/register", json={"email": "candidate-fact-evidence@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registered.json()["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Annual growth 2.5 percent. Quarter growth 3.2 percent.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Growth?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "candidate-evidence-001"},
        json={"parent_version_id": topic["versions"][0]["version_id"]}).json()
    op = receipt["operation"]["operation_id"]
    for _ in range(100):
        state = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if state in {"completed", "failed"}: break
        time.sleep(0.02)
    assert state == "completed"
    current = client.get(path, headers=headers).json()["versions"][-1]
    claim = current["content"]["claims"][0]
    assert current["content"]["run"]["status"] == "partial"
    assert claim["uncited_percentages"] == ["2.5 percent"]
    assert any("claim percentage is absent" in note for note in current["content"]["limitations"])
    assert len(claim["citations"]) == 1 and claim["suggested_fact_evidence"][0]["fact_id"] == "annual"
    assert claim["suggested_fact_evidence"][0]["status"] == "requires_semantic_review"
    edited = deepcopy(current["content"])
    edited["facts"][0]["value"] = "2.6"
    edited["claims"][0]["suggested_fact_evidence"] = [{"fabricated": True}]
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": edited})
    assert saved.status_code == 201, saved.text
    assert not saved.json()["versions"][-1]["content"]["claims"][0].get("suggested_fact_evidence")
    assert current["content"]["claims"][0]["suggested_fact_evidence"][0]["fact_id"] == "annual"


def test_research_lease_recovery_keeps_ambiguous_charge_and_does_not_replay():
    api.reset_state()
    entered, release, finished = Event(), Event(), Event()
    committed = []
    calls = []
    endpoint = "/v1/research/topics/topic_recovery/runs"
    payload = {"parent_version_id": "version_one"}
    def build(operation_id):
        calls.append(operation_id)
        entered.set()
        try:
            assert release.wait(5)
            return ResearchContent(summary="late result"), "topic_recovery"
        finally:
            finished.set()
    try:
        receipt, replayed = api.operations.submit_async(endpoint=endpoint, idempotency_key="research-lease-recovery",
            owner_subject="account:account_test_123", request_payload=payload, result_field="research",
            build_result=build, session_id="topic_recovery", commit_result=lambda *_: committed.append(True))
        assert not replayed and entered.wait(5)
        operation = receipt.operation
        worker = next(thread for thread in threading.enumerate() if thread.name == f"provider-{operation.operation_id}")
        reservation = f"model:{operation.operation_id}"
        now = api.lifecycle_clock.now()
        api.billing_ledger.ensure_state("account_test_123", now=now)
        api.billing_ledger.reserve("account_test_123", reservation, 4500, now=now)
        assert api.operations.recover_expired_leases(now=now + timedelta(seconds=301)) == 1
        assert api.operations._cache_operation(operation.operation_id).status == "failed"
        api.cleanup_expired_state()
        assert api.billing_ledger.usage.get(reservation).status == "reserved"
        replay, replayed = api.operations.submit_async(endpoint=endpoint, idempotency_key="research-lease-recovery",
            owner_subject="account:account_test_123", request_payload=payload, result_field="research",
            build_result=build, session_id="topic_recovery")
        assert replayed and replay.operation.status == "failed" and len(calls) == 1
        release.set()
        assert finished.wait(5)
        # The terminal transition must not admit a result from an expired worker.
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert committed == []
    finally:
        release.set()
        api.reset_state()


def test_question_only_discovers_unverified_leads_without_creating_evidence(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    calls = []
    class Search:
        name = "test-search"
        supports_project_search = True
        def search(self, query, *, limit, deadline, cancel_event):
            assert deadline > time.monotonic()
            calls.append((query, limit))
            return (SearchCandidate(title="Official report", url="https://www.bea.gov/news/report?tracking=secret",
                excerpt="Search-only text", publisher="BEA", published_at=date(2023, 1, 1), independently_reviewable=True),
                SearchCandidate(title="Future report", url="https://www.bea.gov/news/future",
                excerpt="Future", publisher="BEA", published_at=date(2024, 1, 1), independently_reviewable=True))
    monkeypatch.setattr(api, "evidence_provider", Search())
    monkeypatch.setattr(api, "_reserve_search_call", lambda request: None)
    client = TestClient(api.app)
    registration = client.post("/v1/auth/local/register", json={"email": "search@example.test",
        "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + registration.json()["access_token"]}
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Find economic evidence",
        "as_of": "2023-02-01T00:00:00Z"}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    payload = {"parent_version_id": topic["versions"][0]["version_id"], "max_search_calls": 1}
    req_headers = {**headers, "Idempotency-Key": "discovery-request-1"}
    receipt = client.post(path + "/runs", headers=req_headers, json=payload)
    assert receipt.status_code == 202
    operation_id = receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + operation_id, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert status == "completed"
    result = client.get(path, headers=headers).json()["versions"][-1]["content"]
    assert result["run"]["status"] == "partial"
    assert result["run"]["search_calls"] == 1
    assert len(result["source_candidates"]) == 1
    assert result["source_candidates"][0]["status"] == "unverified"
    assert result["source_candidates"][0]["url"] == "https://www.bea.gov/news/report"
    assert "Search-only text" not in str(result)
    assert result["facts"] == [] and result["claims"] == []
    assert client.post(path + "/runs", headers=req_headers, json=payload).headers["Idempotency-Replayed"] == "true"
    assert len(calls) == 1
    latest = client.get(path, headers=headers).json()["versions"][-1]["version_id"]
    disabled = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "discovery-disabled-1"},
                           json={"parent_version_id": latest, "max_search_calls": 0})
    assert disabled.status_code == 202
    disabled_id = disabled.json()["operation"]["operation_id"]
    for _ in range(100):
        state = client.get(path + "/runs/" + disabled_id, headers=headers).json()["operation"]["status"]
        if state in {"completed", "failed"}: break
        time.sleep(0.02)
    assert state == "completed"
    disabled_content = client.get(path, headers=headers).json()["versions"][-1]["content"]
    assert disabled_content["run"]["search_status"] == "not_requested"
    assert len(calls) == 1


def test_review_completion_does_not_overwrite_edits_to_generation_checkpoint(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    entered, release = Event(), Event()
    source_id = ""
    class Provider:
        name = "checkpoint-edit-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            if system == REVIEW_SYSTEM_PROMPT:
                entered.set()
                assert release.wait(5)
                return {"reviews": [{"claim_index": 0, "verdict": "supports"}]}, 10
            citation = {"source_id": source_id, "quotation": "Revenue was 100"}
            return {"claims": [{"text": "Revenue was 100", "citations": [citation]}]}, 55
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    reg = client.post("/v1/auth/local/register", json={"email": "checkpoint-edit@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + reg.json()["access_token"]}
    source = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Revenue was 100.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()
    source_id = source["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "checkpoint-edit-run"}, json={
        "parent_version_id": topic["versions"][0]["version_id"], "support_review": True,
        "max_model_calls": 2, "max_total_tokens": 9000}).json()["operation"]
    assert entered.wait(5)
    checkpoint = client.get(path, headers=headers).json()["versions"][-1]
    edited = deepcopy(checkpoint["content"]); edited["summary"] = "User revision"
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": checkpoint["version_id"], "content": edited})
    assert saved.status_code == 201
    release.set()
    for _ in range(100):
        status = client.get(path + "/runs/" + receipt["operation_id"], headers=headers).json()["operation"]["status"]
        if status in {"failed", "completed"}: break
        time.sleep(.02)
    assert status == "failed"
    versions = client.get(path, headers=headers).json()["versions"]
    assert len(versions) == 3 and versions[-1]["content"]["summary"] == "User revision"
    assert client.delete("/v1/auth/local/account", headers=headers).status_code == 200


def test_discovery_keeps_safe_leads_when_another_candidate_is_malformed(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    calls = []
    class Search:
        name = "mixed-search"
        supports_project_search = True
        valid = True
        def search(self, query, *, limit, deadline, cancel_event):
            calls.append(query)
            invalid = SimpleNamespace(title="SECRET_INVALID", url="not-a-url", excerpt="invalid", publisher=None,
                                      published_at=None, independently_reviewable=True)
            if not self.valid:
                return (invalid,)
            return (invalid, SearchCandidate(title="BEA report", url="https://www.bea.gov/news/report?private=secret",
                        excerpt="Unverified result", publisher="BEA", published_at=date(2023, 1, 1), independently_reviewable=True),
                    SearchCandidate(title="Unsafe local", url="http://127.0.0.1/private", excerpt="invalid",
                                    publisher=None, published_at=None, independently_reviewable=True))
    search = Search()
    monkeypatch.setattr(api, "evidence_provider", search)
    monkeypatch.setattr(api, "_reserve_search_call", lambda request: None)
    client = TestClient(api.app)
    reg = client.post("/v1/auth/local/register", json={"email": "mixed-search@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + reg.json()["access_token"]}
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Find a report"}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    payload = {"parent_version_id": topic["versions"][0]["version_id"], "max_search_calls": 1}
    req_headers = {**headers, "Idempotency-Key": "mixed-search-test"}
    receipt = client.post(path + "/runs", headers=req_headers, json=payload)
    assert receipt.status_code == 202
    op = receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(.02)
    assert status == "completed"
    content = client.get(path, headers=headers).json()["versions"][-1]["content"]
    assert content["run"]["status"] == "partial" and content["run"]["search_status"] == "partial"
    assert len(content["source_candidates"]) == 1 and content["source_candidates"][0]["url"] == "https://www.bea.gov/news/report"
    assert "Some search candidates could not be validated and were omitted." in content["limitations"]
    assert "SECRET_INVALID" not in str(content) and "private=secret" not in str(content)
    replay = client.post(path + "/runs", headers=req_headers, json=payload)
    assert replay.headers["Idempotency-Replayed"] == "true" and len(calls) == 1
    search.valid = False
    other = client.post("/v2/research/topics", headers=headers, json={"question": "Another report"}).json()
    other_path = "/v2/research/topics/" + other["topic_id"]
    second = client.post(other_path + "/runs", headers={**headers, "Idempotency-Key": "invalid-search-test"},
        json={"parent_version_id": other["versions"][0]["version_id"], "max_search_calls": 1}).json()["operation"]["operation_id"]
    for _ in range(100):
        state = client.get(other_path + "/runs/" + second, headers=headers).json()["operation"]["status"]
        if state in {"completed", "failed"}: break
        time.sleep(.02)
    assert state == "completed"
    no_safe = client.get(other_path, headers=headers).json()["versions"][-1]["content"]
    assert no_safe["run"]["search_status"] == "failed" and no_safe["source_candidates"] == []
    assert "No eligible search candidates remained after validation." in no_safe["limitations"]
    assert "SECRET_INVALID" not in str(no_safe) and len(calls) == 2
    assert client.delete("/v1/auth/local/account", headers=headers).status_code == 200


def test_simulated_source_is_not_sent_to_real_research_or_saved_as_evidence(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    seen = []
    class Provider:
        name = "simulation-boundary-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            seen.append(prompt)
            import json
            data = json.loads(prompt)
            assert len(data["sources"]) == 1 and "SIMULATED_VALUE" not in prompt
            citation = {"source_id": data["sources"][0]["source_id"], "quotation": "Revenue was 100"}
            return {"claims": [{"text": "Revenue was 100", "citations": [citation]}],
                    "counterarguments": [{"text": "Revenue is not profit", "citations": [citation]}]}, 55
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    reg = client.post("/v1/auth/local/register", json={"email": "simulation-boundary@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + reg.json()["access_token"]}
    def create(title, text, simulated):
        response = client.post("/v2/research/sources", headers=headers, json={"title": title, "text": text,
            "is_simulated": simulated, "permissions": {"store": True, "display": True, "model_input": True, "export": True}})
        assert response.status_code == 201
        return response.json()
    real = create("Report", "Revenue was 100.", False)
    simulated = create("Scenario", "SIMULATED_VALUE 900", True)
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?",
        "source_ids": [real["source_id"], simulated["source_id"]]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    # Marked simulated material cannot be promoted by manual citations or facts either.
    for content in ({"claims": [{"text": "A finding", "citations": [{"source_id": simulated["source_id"], "quotation": "SIMULATED_VALUE 900"}]}]},
                    {"facts": [{"fact_id": "sim", "label": "Revenue", "value": "900", "unit": "USD", "period": "2024", "source_id": simulated["source_id"]}]}):
        assert client.post(path + "/versions", headers=headers, json={"parent_version_id": topic["versions"][0]["version_id"], "content": content}).status_code == 422
    started = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "simulation-boundary-run"},
        json={"parent_version_id": topic["versions"][0]["version_id"]}).json()["operation"]["operation_id"]
    for _ in range(100):
        state = client.get(path + "/runs/" + started, headers=headers).json()["operation"]["status"]
        if state in {"completed", "failed"}: break
        time.sleep(.02)
    assert state == "completed" and len(seen) == 1
    version = client.get(path, headers=headers).json()["versions"][-1]
    content = version["content"]
    assert content["run"]["status"] == "partial"
    assert [item["source_id"] for item in content["sources"]] == [real["source_id"]]
    assert any("simulated material is excluded" in item for item in content["limitations"])
    assert "SIMULATED_VALUE" not in str(content)
    snapshot = client.get(path + "/runs/" + started + "/inputs", headers=headers).json()
    assert next(item for item in snapshot["sources"] if item["source_id"] == simulated["source_id"])["is_simulated"] is True
    metadata = client.get(path + "/versions/" + version["version_id"] + "/sources", headers=headers).json()
    assert next(item for item in metadata if item["source_id"] == simulated["source_id"])["is_simulated"] is True
    export = client.get(path + "/versions/" + version["version_id"] + "/export", headers=headers).json()
    assert next(item for item in export["source_provenance"] if item["source_id"] == simulated["source_id"])["is_simulated"] is True
    assert "SIMULATED_VALUE" not in str(export)
    only_sim = client.post("/v2/research/topics", headers=headers, json={"question": "Scenario?",
        "source_ids": [simulated["source_id"]]}).json()
    sim_path = "/v2/research/topics/" + only_sim["topic_id"]
    sim_op = client.post(sim_path + "/runs", headers={**headers, "Idempotency-Key": "only-simulation-run"},
        json={"parent_version_id": only_sim["versions"][0]["version_id"]}).json()["operation"]["operation_id"]
    for _ in range(100):
        sim_status = client.get(sim_path + "/runs/" + sim_op, headers=headers).json()["operation"]["status"]
        if sim_status in {"completed", "failed"}: break
        time.sleep(.02)
    assert sim_status == "completed" and len(seen) == 1
    sim_content = client.get(sim_path, headers=headers).json()["versions"][-1]["content"]
    assert sim_content["run"]["model_calls"] == 0 and sim_content["run"]["status"] == "partial"
    assert any("simulated material is excluded" in item for item in sim_content["limitations"])
    assert client.delete("/v1/auth/local/account", headers=headers).status_code == 200


def test_mismatched_currency_conversion_is_reviewable_and_recomputed_on_edit(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    class Provider:
        name = "amount-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            import json
            sid = json.loads(prompt)["sources"][0]["source_id"]
            quote = "2023 revenue 53717, 2024 revenue 57063 million USD"
            cite = {"source_id": sid, "quotation": quote}
            facts = [{"fact_id": id, "label": "Revenue", "value": value, "unit": "USD millions", "currency": "USD",
                      "period": year, "citation": cite} for id, value, year in (("prior", "53717", "2023"), ("current", "57063", "2024"))]
            return {"claims": [{"text": "2023为53,717百万美元，2024为57,063百万美元，即57,070.63亿美元。", "citations": [cite]}],
                    "counterarguments": [{"text": "Revenue alone does not establish profit.", "citations": [cite]}], "facts": facts}, 55
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    reg = client.post("/v1/auth/local/register", json={"email": "amount-warning@example.test", "password": "correct horse battery staple!"})
    headers = {"Authorization": "Bearer " + reg.json()["access_token"]}
    source = client.post("/v2/research/sources", headers=headers, json={"title": "Report",
        "text": "2023 revenue 53717, 2024 revenue 57063 million USD", "permissions": {"store": True, "display": True, "model_input": True}}).json()
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source["source_id"]]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    op = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "amount-warning-run"},
        json={"parent_version_id": topic["versions"][0]["version_id"]}).json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(.02)
    assert status == "completed"
    original = client.get(path, headers=headers).json()["versions"][-1]
    assert original["content"]["run"]["status"] == "partial"
    assert original["content"]["amount_differences"][0]["claimed_amount"] == "57,070.63亿美元"
    edited = deepcopy(original["content"])
    edited["claims"][0]["text"] = "2023为53,717百万美元，2024为57,063百万美元，即570.63亿美元。"
    edited["amount_differences"] = [{"fabricated": True}]
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": original["version_id"], "content": edited})
    assert saved.status_code == 201
    assert saved.json()["versions"][-1]["content"]["amount_differences"] == []
    assert saved.json()["versions"][-2]["content"]["amount_differences"] == original["content"]["amount_differences"]
    assert client.delete("/v1/auth/local/account", headers=headers).status_code == 200


def test_publication_date_candidate_persists_and_is_recomputed_after_edit(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    class Provider:
        name = "publication-date-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            citation = {"source_id": source_id, "quotation": "Revenue was 100"}
            return {"claims": [{"text": "2023年9月发布的公告报告收入。", "citations": [citation]}],
                    "counterarguments": [{"text": "收入不能证明利润。", "citations": [citation]}]}, 30
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    auth = client.post("/v1/auth/local/register", json={"email": "date-warning@example.test", "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + auth["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Revenue was 100",
        "published_at": "2023-10-27T12:30:00Z", "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Report?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "date-warning-run"},
                          json={"parent_version_id": topic["versions"][0]["version_id"]})
    assert receipt.status_code == 202
    op = receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(.02)
    assert status == "completed"
    current = client.get(path, headers=headers).json()["versions"][-1]
    assert current["content"]["run"]["status"] == "partial"
    assert current["content"]["claims"][0]["publication_date_warnings"][0]["claimed_text"] == "2023年9月发布"
    changed = deepcopy(current["content"])
    changed["claims"][0]["text"] = "2023年10月发布的公告报告收入。"
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": changed})
    assert saved.status_code == 201, saved.text
    versions = saved.json()["versions"]
    assert not versions[-1]["content"]["claims"][0].get("publication_date_warnings")
    assert versions[-2]["content"]["claims"][0]["publication_date_warnings"]


def test_rate_unit_candidate_makes_run_partial_and_recomputes_on_edit(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    class Provider:
        name = "rate-unit-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            citation = {"source_id": source_id, "quotation": "target range at 4-1/4 to 4-1/2 percent"}
            return {"claims": [{"text": "目标区间维持在4-1/4至4-1/2个百分点。", "citations": [citation]}],
                    "counterarguments": [{"text": "未来政策存在不确定性。", "citations": [citation]}]}, 30
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    auth = client.post("/v1/auth/local/register", json={"email": "rate-unit@example.test", "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + auth["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Statement", "text": "target range at 4-1/4 to 4-1/2 percent",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Rate?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "rate-unit-run"},
                          json={"parent_version_id": topic["versions"][0]["version_id"]})
    assert receipt.status_code == 202
    op = receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(.02)
    assert status == "completed"
    current = client.get(path, headers=headers).json()["versions"][-1]
    assert current["content"]["run"]["status"] == "partial"
    assert current["content"]["claims"][0]["rate_unit_warnings"]
    changed = deepcopy(current["content"])
    changed["claims"][0]["text"] = "目标区间维持在4.25%至4.50%。"
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": changed})
    assert saved.status_code == 201, saved.text
    versions = saved.json()["versions"]
    assert not versions[-1]["content"]["claims"][0].get("rate_unit_warnings")
    assert versions[-2]["content"]["claims"][0]["rate_unit_warnings"]


def test_unverified_rate_currency_is_visible_and_rechecked_on_edit(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    class Provider:
        name = "rate-currency-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            citation = {"source_id": source_id, "quotation": "4-1/4 to 4-1/2 percent"}
            return {"claims": [{"text": "The rate target is 4.25% to 4.5%.", "citations": [citation]}],
                    "facts": [{"fact_id": "range", "label": "Target", "value": "4-1/4 to 4-1/2",
                               "unit": "percent", "currency": "USD", "period": "2025", "citation": citation}]}, 30
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    auth = client.post("/v1/auth/local/register", json={"email": "rate-currency@example.test", "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + auth["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Statement", "text": "4-1/4 to 4-1/2 percent",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Rate?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "rate-currency-run"},
                          json={"parent_version_id": topic["versions"][0]["version_id"], "require_counterarguments": False})
    assert receipt.status_code == 202
    op = receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(.02)
    assert status == "completed"
    current = client.get(path, headers=headers).json()["versions"][-1]
    assert current["content"]["run"]["status"] == "partial"
    assert len(current["content"]["facts"]) == 2
    assert all(item["currency_context_unverified"] for item in current["content"]["facts"])
    with_calculation = deepcopy(current["content"])
    inputs = [{key: fact[key] for key in ("fact_id", "value", "unit", "currency", "period")}
              for fact in with_calculation["facts"]]
    with_calculation["calculations"] = [{"formula_id": "difference", "formula_version": "1", "inputs": inputs,
                                          "expression": "first - second", "value": "0", "unit": "percent"}]
    saved_calc = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": with_calculation})
    assert saved_calc.status_code == 201, saved_calc.text
    current = saved_calc.json()["versions"][-1]
    retry = client.post(path + "/calculations/recalculate", headers=headers,
                        json={"parent_version_id": current["version_id"], "calculation_index": 0})
    assert retry.status_code == 422
    changed = deepcopy(current["content"])
    for item in changed["facts"]:
        item["currency"] = None
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": changed})
    assert saved.status_code == 201, saved.text
    versions = saved.json()["versions"]
    assert all(not item.get("currency_context_unverified") for item in versions[-1]["content"]["facts"])
    assert all(item["currency_context_unverified"] for item in versions[-2]["content"]["facts"])


def test_uncited_amounts_are_candidates_and_rechecked_after_adding_quotes(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    class Provider:
        name = "amount-candidate-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def item(id, value, quote):
                return {"fact_id": id, "label": id, "value": value, "unit": "millions", "currency": "USD", "period": "2025",
                        "citation": {"source_id": source_id, "quotation": quote}}
            return {"claims": [{"text": "收入200,966百万美元，营业利润83,276百万美元。", "citations": [
                        {"source_id": source_id, "quotation": "unit USD millions"}]}],
                    "facts": [item("revenue", "200966", "revenue 200966"), item("income", "83276", "income 83276")]}, 30
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    auth = client.post("/v1/auth/local/register", json={"email": "amount-candidate@example.test", "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + auth["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report",
        "text": "unit USD millions; revenue 200966; income 83276.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue and income?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "amount-candidate-run"},
                          json={"parent_version_id": topic["versions"][0]["version_id"], "require_counterarguments": False})
    assert receipt.status_code == 202
    op = receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(.02)
    assert status == "completed"
    current = client.get(path, headers=headers).json()["versions"][-1]
    claim = current["content"]["claims"][0]
    assert current["content"]["run"]["status"] == "partial"
    assert len(claim["uncited_amounts"]) == 2 and len(claim["suggested_fact_evidence"]) == 2
    assert len(claim["uncited_fact_values"]) == 2
    changed = deepcopy(current["content"])
    changed["claims"][0]["citations"].extend({"source_id": item["source_id"], "quotation": item["quotation"]}
                                                for item in claim["suggested_fact_evidence"])
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": changed})
    assert saved.status_code == 201, saved.text
    versions = saved.json()["versions"]
    assert not versions[-1]["content"]["claims"][0].get("uncited_amounts")
    assert not versions[-1]["content"]["claims"][0].get("uncited_fact_values")
    assert len(versions[-1]["content"]["claims"][0]["citations"]) == 3
    assert versions[-2]["content"]["claims"][0]["uncited_amounts"]
    assert versions[-1]["content"]["claims"][0]["support_status"] == "not_independently_verified"


def test_budget_exhaustion_keeps_selected_source_without_claiming_a_call(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    class Provider:
        name = "no-call-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            raise AssertionError("budget disallows this call")
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    registered = client.post("/v1/auth/local/register", json={"email": "no-call-source@example.test",
        "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + registered["access_token"]}
    src = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Revenue was 100.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [src["source_id"]]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    response = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "no-call-source-test"},
                           json={"parent_version_id": topic["versions"][0]["version_id"], "max_model_calls": 0})
    operation = response.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + operation, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert status == "completed"
    content = client.get(path, headers=headers).json()["versions"][-1]["content"]
    assert content["run"]["status"] == "partial" and content["run"]["model_calls"] == 0
    assert content["run"]["error"] == "provider_budget_exhausted"
    assert content["run"]["prompt_sha256"] is None
    assert [item["source_id"] for item in content["sources"]] == [src["source_id"]]
    assert any("selected for analysis" in note for note in content["limitations"])


def test_discarded_model_annotations_keep_research_partial(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""
    class Provider:
        name = "extra-metadata-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            return {"claims": [{"text": "Revenue was 100", "status": "evidence", "citations": [
                {"source_id": source_id, "quotation": "Revenue was 100"}]}], "counterarguments": []}, 10
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    account = client.post("/v1/auth/local/register", json={"email": "extra-model-fields@example.test",
        "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + account["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Revenue was 100.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "extra-metadata-001"},
        json={"parent_version_id": topic["versions"][0]["version_id"], "require_counterarguments": False}).json()
    op = receipt["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert status == "completed"
    content = client.get(path, headers=headers).json()["versions"][-1]["content"]
    assert content["run"]["status"] == "partial" and content["run"]["error"] is None
    assert content["claims"][0]["text"] == "Revenue was 100"
    assert any("annotations were omitted" in note for note in content["limitations"])


def test_omitted_citation_subject_keeps_run_partial_and_recomputes_on_edit(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""
    text = "Personal saving rate was 2.7 percent in the third quarter, a downward revision of 0.1 percentage point."
    quote = "quarter, a downward revision of 0.1 percentage point."
    class Provider:
        name = "subject-context-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            return {"claims": [{"text": "GDP 向下修订 0.1 个百分点。", "citations": [
                {"source_id": source_id, "quotation": quote}]}], "counterarguments": []}, 10
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    account = client.post("/v1/auth/local/register", json={"email": "context-subject@example.test", "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + account["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": text,
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "GDP?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "subject-context-001"},
        json={"parent_version_id": topic["versions"][0]["version_id"], "require_counterarguments": False}).json()
    op = receipt["operation"]["operation_id"]
    for _ in range(100):
        state = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if state in {"completed", "failed"}: break
        time.sleep(0.02)
    assert state == "completed"
    current = client.get(path, headers=headers).json()["versions"][-1]
    content = current["content"]
    assert content["run"]["status"] == "partial"
    assert content["claims"][0]["citation_context_warnings"][0]["context_metric"] == "personal saving rate"
    revised = deepcopy(content)
    revised["claims"][0]["citations"] = [{"source_id": source_id, "quotation": text}]
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": revised})
    assert saved.status_code == 201
    assert not saved.json()["versions"][-1]["content"]["claims"][0].get("citation_context_warnings")
    assert current["content"]["claims"][0]["citation_context_warnings"]


def test_inferred_negative_percentage_cannot_be_silently_recalculated(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""
    class Provider:
        name = "direction-context-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def item(key, value, period, quotation):
                return {"fact_id": key, "label": "GDP change", "value": value, "unit": "percent", "period": period,
                        "citation": {"source_id": source_id, "quotation": quotation}}
            return {"facts": [item("old", "-0.6", "2022", "decreased 0.6 percent"),
                               item("new", "3.2", "2023", "increased 3.2 percent")],
                    "calculations": [{"formula": "difference", "first_fact_id": "new", "second_fact_id": "old"}]}, 10
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    account = client.post("/v1/auth/local/register", json={"email": "direction-context@example.test", "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + account["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report",
        "text": "Real GDP decreased 0.6 percent. It increased 3.2 percent later.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "GDP?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "direction-context-001"},
        json={"parent_version_id": topic["versions"][0]["version_id"]}).json()
    op = receipt["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert status == "completed"
    current = client.get(path, headers=headers).json()["versions"][-1]
    content = deepcopy(current["content"])
    assert content["run"]["status"] == "partial" and not content["calculations"]
    negative = next(item for item in content["facts"] if item["fact_id"] == "old")
    assert negative.pop("direction_context_unverified") is True
    content["calculations"] = [{"formula_id": "difference", "formula_version": "1",
        "inputs": [{"fact_id": "new"}, {"fact_id": "old"}]}]
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": current["version_id"], "content": content})
    assert saved.status_code == 201
    latest = saved.json()["versions"][-1]
    assert next(item for item in latest["content"]["facts"] if item["fact_id"] == "old")["direction_context_unverified"] is True
    response = client.post(path + "/calculations/recalculate", headers=headers,
        json={"parent_version_id": latest["version_id"], "calculation_index": 0})
    assert response.status_code == 422
    assert "require review" in response.json()["message"]


def test_recalculation_refreshes_claim_calculation_candidates_without_changing_history(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""
    class Provider:
        name = "recalculation-candidate-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def cite(quotation): return {"source_id": source_id, "quotation": quotation}
            current, prior = "Revenue was 100 in 2023", "Revenue was 80 in 2022"
            return {"claims": [{"text": "Revenue grew 25%", "citations": [cite(current), cite(prior)]}],
                    "facts": [{"fact_id": "current", "label": "Revenue", "value": "100", "unit": "USD million", "currency": "USD", "period": "2023", "citation": cite(current)},
                              {"fact_id": "prior", "label": "Revenue", "value": "80", "unit": "USD million", "currency": "USD", "period": "2022", "citation": cite(prior)}],
                    "calculations": [{"formula": "growth_percent", "first_fact_id": "current", "second_fact_id": "prior"}]}, 10
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    account = client.post("/v1/auth/local/register", json={"email": "recalc-candidates@example.test", "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + account["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report",
        "text": "Revenue was 100 in 2023. Revenue was 80 in 2022.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue growth?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "recalc-candidates-001"},
        json={"parent_version_id": topic["versions"][0]["version_id"], "require_counterarguments": False}).json()
    op = receipt["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert status == "completed"
    original = client.get(path, headers=headers).json()["versions"][-1]
    assert original["content"]["claims"][0]["suggested_calculations"][0]["calculation_index"] == 0
    edited = deepcopy(original["content"])
    edited["calculations"][0]["value"] = "30"
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": original["version_id"], "content": edited})
    assert saved.status_code == 201
    changed = saved.json()["versions"][-1]
    assert not changed["content"]["claims"][0].get("suggested_calculations")
    recalculated = client.post(path + "/calculations/recalculate", headers=headers,
        json={"parent_version_id": changed["version_id"], "calculation_index": 0})
    assert recalculated.status_code == 201
    versions = recalculated.json()["versions"]
    assert versions[-1]["content"]["calculations"][0]["value"] == "25.00"
    assert versions[-1]["content"]["claims"][0]["suggested_calculations"][0]["status"] == "requires_semantic_review"
    assert not versions[-2]["content"]["claims"][0].get("suggested_calculations")
    assert versions[-3]["content"]["claims"][0]["suggested_calculations"]


@pytest.mark.parametrize("bad_first", [False, True])
def test_partial_support_review_records_omissions_and_leaves_other_claims_unreviewed(monkeypatch, bad_first):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""
    class Provider:
        name = "partial-review-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            if system == REVIEW_SYSTEM_PROMPT:
                import json
                assert [item["claim_index"] for item in json.loads(prompt)["claims"]] == [0]
                return {"reviews": [{"claim_index": 0, "verdict": "supports"}]}, 10
            cite = {"source_id": source_id, "quotation": "Revenue was 100"}
            good = {"text": "Revenue was 100", "citations": [cite]}
            bad = {"text": "Revenue was reported", "citations": [cite] * 4}
            return {"claims": [bad, good] if bad_first else [good, bad], "counterarguments": []}, 20
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    account = client.post("/v1/auth/local/register", json={"email": f"partial-review-{bad_first}@example.test", "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + account["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Revenue was 100.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "partial-review-coverage-001"},
        json={"parent_version_id": topic["versions"][0]["version_id"], "support_review": True, "max_model_calls": 2,
              "max_total_tokens": 9000, "require_counterarguments": False}).json()
    op = receipt["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(0.02)
    assert status == "completed"
    versions = client.get(path, headers=headers).json()["versions"]
    content = versions[-1]["content"]
    assert content["run"]["status"] == "partial" and content["run"]["support_review_status"] == "partial"
    reviewed, omitted = (1, 0) if bad_first else (0, 1)
    assert content["run"]["support_review_indexes"] == [reviewed]
    assert content["run"]["support_review_omissions"] == [{"claim_index": omitted, "reason": "not_reviewable"}]
    assert content["claims"][reviewed]["support_review"]["verdict"] == "supports"
    assert "support_review" not in content["claims"][omitted]
    checkpoint = versions[-2]["content"]["run"]
    assert checkpoint["support_review_omissions"] == [{"claim_index": omitted, "reason": "not_reviewable"}]
    assert checkpoint["support_review_method"] == content["run"]["support_review_method"]
    assert len(checkpoint["support_review_prompt_sha256"]) == 64
    assert checkpoint["support_review_indexes"] == [reviewed]
    assert checkpoint["support_review_segments"][0]["claim_index"] == reviewed


def test_isolated_review_respects_token_budget_and_checkpoints_each_result(monkeypatch):
    import json
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    calls = []
    class Provider:
        name = "isolated-review-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            if system == REVIEW_SYSTEM_PROMPT:
                payload = json.loads(prompt)
                assert len(payload["claims"]) == 1 and payload["claims"][0]["claim_index"] == 0
                calls.append(payload["claims"][0]["text"])
                return {"reviews":[{"claim_index":0,"verdict":"supports"}]}, 10
            def c(text): return {"text":text,"citations":[{"source_id":source_id,"quotation":text}]}
            return {"claims":[c("Revenue was 100"),c("Profit was 20")],"counterarguments":[c("Cash was 30")]}, 20
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    account = client.post("/v1/auth/local/register",json={"email":"isolated-budget@example.test","password":"correct horse battery staple!"}).json()
    headers = {"Authorization":"Bearer " + account["access_token"]}
    source_id = client.post("/v2/research/sources",headers=headers,json={"title":"Report","text":"Revenue was 100. Profit was 20. Cash was 30.",
        "permissions":{"store":True,"model_input":True,"display":True}}).json()["source_id"]
    topic = client.post("/v2/research/topics",headers=headers,json={"question":"Financial figures?","source_ids":[source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    payload = {"parent_version_id":topic["versions"][0]["version_id"], "support_review":True,
        "support_review_mode":"isolated", "max_model_calls":5, "max_total_tokens":4530}
    receipt = client.post(path+"/runs",headers={**headers,"Idempotency-Key":"isolated-budget"},json=payload)
    assert receipt.status_code == 202
    op = receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path+"/runs/"+op,headers=headers).json()["operation"]["status"]
        if status in {"completed","failed"}: break
        time.sleep(.02)
    assert status == "completed"
    versions = client.get(path,headers=headers).json()["versions"]
    content = versions[-1]["content"]; run = content["run"]
    assert calls == ["Revenue was 100", "Cash was 30"]
    assert run["status"] == "partial" and run["support_review_status"] == "partial"
    assert run["model_calls"] == 3 and run["total_tokens"] == 40 and run["support_review_tokens"] == 20
    assert run["support_review_indexes"] == [0,2] and run["support_review_omissions"] == [{"claim_index":1,"reason":"token_budget"}]
    assert content["claims"][0]["support_review"]["verdict"] == "supports"
    assert content["claims"][2]["support_review"]["verdict"] == "supports"
    assert "support_review" not in content["claims"][1]
    checkpoints = [v["content"] for v in versions if v["content"].get("run",{}).get("checkpoint") == "isolated_review"]
    assert len(checkpoints) == 5
    assert "support_review" not in checkpoints[0]["claims"][0]
    assert checkpoints[1]["run"]["support_review_omissions"][0]["reason"] == "in_progress"
    assert checkpoints[2]["claims"][0]["support_review"]["verdict"] == "supports"
    assert "support_review" not in checkpoints[2]["claims"][2]
    assert checkpoints[4]["claims"][2]["support_review"]["verdict"] == "supports"
    replay = client.post(path+"/runs",headers={**headers,"Idempotency-Key":"isolated-budget"},json=payload)
    assert replay.status_code == 202 and replay.headers["Idempotency-Replayed"] == "true" and len(calls) == 2


def test_isolated_review_cancellation_preserves_previous_checkpoint(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    entered, release = Event(), Event()
    reviews = []
    class Provider:
        name = "isolated-cancel-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            if system == REVIEW_SYSTEM_PROMPT:
                reviews.append(prompt)
                if len(reviews) == 2:
                    entered.set()
                    assert release.wait(5)
                return {"reviews":[{"claim_index":0,"verdict":"supports"}]}, 10
            def c(text): return {"text":text,"citations":[{"source_id":source_id,"quotation":text}]}
            return {"claims":[c("Revenue was 100"),c("Profit was 20")],"counterarguments":[]}, 20
    monkeypatch.setattr(api,"challenge_provider",Provider())
    client = TestClient(api.app)
    account = client.post("/v1/auth/local/register",json={"email":"isolated-cancel@example.test","password":"correct horse battery staple!"}).json()
    headers = {"Authorization":"Bearer " + account["access_token"]}
    source_id = client.post("/v2/research/sources",headers=headers,json={"title":"Report","text":"Revenue was 100. Profit was 20.",
        "permissions":{"store":True,"model_input":True,"display":True}}).json()["source_id"]
    topic = client.post("/v2/research/topics",headers=headers,json={"question":"Financial figures?","source_ids":[source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path+"/runs",headers={**headers,"Idempotency-Key":"isolated-cancel"},json={
        "parent_version_id":topic["versions"][0]["version_id"],"support_review":True,"support_review_mode":"isolated",
        "max_model_calls":3,"max_total_tokens":9000,"require_counterarguments":False}).json()["operation"]
    try:
        assert entered.wait(5)
        before = client.get(path,headers=headers).json()["versions"]
        checkpoint = before[-1]["content"]
        assert checkpoint["run"]["checkpoint"] == "isolated_review"
        assert checkpoint["claims"][0]["support_review"]["verdict"] == "supports"
        assert "support_review" not in checkpoint["claims"][1]
        stopped = client.post("/v1/operations/"+receipt["operation_id"]+"/cancel",
            headers={**headers,"Idempotency-Key":"cancel-isolated"},json={"cancel_token":receipt["cancel_token"]})
        assert stopped.status_code == 200 and stopped.json()["operation"]["status"] == "cancelled"
        release.set()
        for _ in range(100):
            if not api.operations._pending_provider_executions.get(receipt["operation_id"]): break
            time.sleep(.02)
        after = client.get(path,headers=headers).json()["versions"]
        assert len(after) == len(before)
        assert after[-1]["content"]["claims"][0]["support_review"]["verdict"] == "supports"
        assert "support_review" not in after[-1]["content"]["claims"][1]
    finally:
        release.set()


def test_isolated_review_does_not_overwrite_concurrent_user_revision(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    entered, release = Event(), Event()
    class Provider:
        name = "isolated-edit-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            if system == REVIEW_SYSTEM_PROMPT:
                self.reviews = getattr(self, "reviews", 0) + 1
                if self.reviews == 2:
                    entered.set()
                    assert release.wait(5)
                return {"reviews":[{"claim_index":0,"verdict":"supports"}]}, 10
            def c(text): return {"text":text,"citations":[{"source_id":source_id,"quotation":text}]}
            return {"claims":[c("Revenue was 100"),c("Profit was 20")],"counterarguments":[]},20
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    account = client.post("/v1/auth/local/register", json={"email":"isolated-edit@example.test","password":"correct horse battery staple!"}).json()
    headers = {"Authorization":"Bearer " + account["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title":"Report","text":"Revenue was 100. Profit was 20.",
        "permissions":{"store":True,"model_input":True,"display":True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question":"Financial figures?","source_ids":[source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path + "/runs", headers={**headers,"Idempotency-Key":"isolated-edit"}, json={
        "parent_version_id":topic["versions"][0]["version_id"],"support_review":True,"support_review_mode":"isolated",
        "max_model_calls":3,"max_total_tokens":9000,"require_counterarguments":False}).json()["operation"]
    try:
        assert entered.wait(5)
        checkpoint = client.get(path, headers=headers).json()["versions"][-1]
        assert checkpoint["content"]["claims"][0]["support_review"]["verdict"] == "supports"
        edited = deepcopy(checkpoint["content"]); edited["summary"] = "User revision"
        saved = client.post(path + "/versions", headers=headers, json={"parent_version_id":checkpoint["version_id"],"content":edited})
        assert saved.status_code == 201
        release.set()
        for _ in range(100):
            status = client.get(path + "/runs/" + receipt["operation_id"],headers=headers).json()["operation"]["status"]
            if status in {"failed","completed"}: break
            time.sleep(.02)
        assert status == "failed"
        latest = client.get(path, headers=headers).json()["versions"][-1]["content"]
        assert latest["summary"] == "User revision"
        assert latest["claims"][0]["support_review"]["verdict"] == "supports"
        assert "support_review" not in latest["claims"][1]
    finally:
        release.set()


def test_isolated_review_failure_keeps_prior_verdict_and_marks_remaining(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    review_calls = []
    class Provider:
        name = "isolated-failure-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            if system == REVIEW_SYSTEM_PROMPT:
                review_calls.append(prompt)
                index = 0 if len(review_calls) == 1 else 9
                return {"reviews":[{"claim_index":index,"verdict":"supports"}]}, 10
            def c(text): return {"text":text,"citations":[{"source_id":source_id,"quotation":text}]}
            return {"claims":[c("Revenue was 100"),c("Profit was 20"),c("Cash was 30")],"counterarguments":[]},20
    monkeypatch.setattr(api,"challenge_provider",Provider())
    client = TestClient(api.app)
    account = client.post("/v1/auth/local/register",json={"email":"isolated-failure@example.test","password":"correct horse battery staple!"}).json()
    headers = {"Authorization":"Bearer " + account["access_token"]}
    source_id = client.post("/v2/research/sources",headers=headers,json={"title":"Report","text":"Revenue was 100. Profit was 20. Cash was 30.",
        "permissions":{"store":True,"model_input":True,"display":True}}).json()["source_id"]
    topic = client.post("/v2/research/topics",headers=headers,json={"question":"Financial figures?","source_ids":[source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    receipt = client.post(path+"/runs",headers={**headers,"Idempotency-Key":"isolated-failure"},json={
        "parent_version_id":topic["versions"][0]["version_id"],"support_review":True,"support_review_mode":"isolated",
        "max_model_calls":4,"max_total_tokens":20000,"require_counterarguments":False}).json()["operation"]
    for _ in range(100):
        status = client.get(path+"/runs/"+receipt["operation_id"],headers=headers).json()["operation"]["status"]
        if status in {"completed","failed"}: break
        time.sleep(.02)
    assert status == "completed" and len(review_calls) == 2
    content = client.get(path,headers=headers).json()["versions"][-1]["content"]
    assert content["run"]["status"] == "partial" and content["run"]["support_review_status"] == "failed"
    assert content["run"]["support_review_failed_indexes"] == [1]
    assert content["run"]["support_review_omissions"] == [{"claim_index":1,"reason":"review_failed"},
                                                          {"claim_index":2,"reason":"review_failure"}]
    assert content["claims"][0]["support_review"]["verdict"] == "supports"
    assert all("support_review" not in content["claims"][i] for i in (1,2))
    assert content["run"]["total_tokens"] == 40 and content["run"]["model_calls"] == 3


def test_run_can_focus_on_saved_unique_quotation(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setattr(api, '_reserve_model_call', lambda *args, **kwargs: None)
    monkeypatch.setattr(api, '_settle_model_call', lambda *args, **kwargs: None)
    quote = 'The overlooked finding is 123 dollars.'
    prompts = []
    class Provider:
        name = 'focus-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            prompts.append(prompt)
            return {'claims': [{'text': 'The finding is 123 dollars.', 'citations': [{'source_id': source_id, 'quotation': quote}]}]}, 10
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    account = client.post('/v1/auth/local/register', json={'email': 'focus-run@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + account['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title': 'Report', 'text': 'Other material. ' * 500 + quote, 'permissions': {'store': True, 'model_input': True, 'display': True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'What is the finding?', 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    saved = client.post(path + '/versions', headers=headers, json={'parent_version_id': topic['versions'][-1]['version_id'], 'content': {'claims': [{'text': 'Check finding', 'citations': [{'source_id': source_id, 'quotation': quote}]}]}})
    assert saved.status_code == 201, saved.text
    parent = saved.json()['versions'][-1]['version_id']
    payload = {'parent_version_id': parent, 'focus_citation': {'claim_index': 0, 'citation_index': 0}, 'require_counterarguments': False}
    request_headers = {**headers, 'Idempotency-Key': 'focus-test-key'}
    invalid = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'focus-invalid-key'}, json={**payload, 'focus_citation': {'claim_index': 9, 'citation_index': 0}})
    assert invalid.status_code == 422 and not prompts
    started = client.post(path + '/runs', headers=request_headers, json=payload)
    assert started.status_code == 202, started.text
    operation_id = started.json()['operation']['operation_id']
    for _ in range(100):
        state = client.get(path + '/runs/' + operation_id, headers=headers).json()['operation']['status']
        if state in {'completed', 'failed'}: break
        time.sleep(0.02)
    assert state == 'completed' and len(prompts) == 1 and quote in prompts[0]
    snapshot = client.get(path + '/runs/' + operation_id + '/inputs', headers=headers).json()
    assert snapshot['budget']['focus_citation'] == payload['focus_citation']
    replay = client.post(path + '/runs', headers=request_headers, json=payload)
    assert replay.status_code == 202 and replay.headers['Idempotency-Replayed'] == 'true' and len(prompts) == 1


def test_run_passes_persisted_scope_and_records_hash(monkeypatch):
    from hashlib import sha256
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setattr(api, '_reserve_model_call', lambda *a, **k: None)
    monkeypatch.setattr(api, '_settle_model_call', lambda *a, **k: None)
    prompts = []
    class Provider:
        name = 'scope-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            prompts.append(prompt)
            return {'claims': []}, 10
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'scope-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title': 'Report', 'text': 'Oceania revenue 4321.', 'permissions': {'store': True, 'display': True, 'model_input': True}}).json()['source_id']
    scope = 'Oceania segment in 2024'
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'What happened?', 'scope': scope, 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    started = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'scope-request-key'}, json={'parent_version_id': topic['versions'][-1]['version_id']})
    assert started.status_code == 202, started.text
    operation_id = started.json()['operation']['operation_id']
    for _ in range(100):
        status = client.get(path + '/runs/' + operation_id, headers=headers).json()['operation']['status']
        if status in {'completed', 'failed'}: break
        time.sleep(0.02)
    assert status == 'completed' and len(prompts) == 1 and scope in prompts[0]
    inputs = client.get(path + '/runs/' + operation_id + '/inputs', headers=headers).json()
    assert inputs['scope_sha256'] == sha256(scope.encode()).hexdigest()
    assert scope not in str(inputs)


def test_historical_run_sends_cutoff_and_excludes_later_source(monkeypatch):
    import json
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setattr(api, '_reserve_model_call', lambda *a, **k: None)
    monkeypatch.setattr(api, '_settle_model_call', lambda *a, **k: None)
    prompts = []
    class Provider:
        name = 'cutoff-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            prompts.append(json.loads(prompt))
            return {'claims': []}, 10
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'cutoff-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    ids = []
    for title, date in [('Old', '2023-01-01T00:00:00Z'), ('Later', '2023-07-02T00:00:00Z')]:
        response = client.post('/v2/research/sources', headers=headers, json={'title': title, 'text': title + ' material.', 'published_at': date, 'permissions': {'store': True, 'display': True, 'model_input': True}})
        assert response.status_code == 201, response.text
        ids.append(response.json()['source_id'])
    cutoff = '2023-07-01T12:30:00.123456Z'
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'What was known?', 'as_of': cutoff, 'source_ids': ids}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    started = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'cutoff-request-key'}, json={'parent_version_id': topic['versions'][-1]['version_id']})
    assert started.status_code == 202, started.text
    op = started.json()['operation']['operation_id']
    for _ in range(100):
        status = client.get(path + '/runs/' + op, headers=headers).json()['operation']['status']
        if status in {'completed', 'failed'}: break
        time.sleep(0.02)
    assert status == 'completed' and len(prompts) == 1
    assert prompts[0]['research_cutoff'] == '2023-07-01T12:30:00.123456+00:00'
    assert [s['source_id'] for s in prompts[0]['sources']] == ['ref-1']
    assert 'Old material.' in prompts[0]['sources'][0]['text']
    assert all('Later material.' not in s['text'] for s in prompts[0]['sources'])
    segments = client.get(path, headers=headers).json()['versions'][-1]['content']['run']['selected_segments']
    assert segments[0]['source_id'] == ids[0] and segments[0]['model_source_id'] == 'ref-1'
    snapshot = client.get(path + '/runs/' + op + '/inputs', headers=headers).json()
    assert snapshot['as_of'] == prompts[0]['research_cutoff']


def test_source_discovery_only_sends_scope_when_explicitly_selected(monkeypatch):
    from uuid import uuid4
    from app.research_runs import discovery_query
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setattr(api, '_reserve_search_call', lambda *a, **k: None)
    queries = []
    class Search:
        supports_project_search = True
        def search(self, query, *, limit, deadline, cancel_event):
            queries.append(query)
            return ()
    monkeypatch.setattr(api, 'evidence_provider', Search())
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'discovery-scope-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    scope = 'Oceania FY2024'
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Compare revenue', 'scope': scope}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    def run(parent, key, extra):
        response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': key}, json={'parent_version_id': parent, **extra})
        assert response.status_code == 202, response.text
        op = response.json()['operation']['operation_id']
        for _ in range(100):
            status = client.get(path + '/runs/' + op, headers=headers).json()['operation']['status']
            if status in {'completed', 'failed'}: break
            time.sleep(0.02)
        assert status == 'completed'
        return op, client.get(path, headers=headers).json()['versions'][-1]['version_id']
    first_parent = topic['versions'][0]['version_id']
    first, parent = run(first_parent, 'scope-search-default', {})
    assert queries == ['Compare revenue']
    # An explicit false remains idempotent with requests predating the field.
    replay = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'scope-search-default'}, json={'parent_version_id': first_parent, 'include_scope_in_search': False})
    assert replay.status_code == 202 and replay.headers['Idempotency-Replayed'] == 'true'
    second, parent = run(parent, 'scope-search-opt-in', {'include_scope_in_search': True})
    assert queries == ['Compare revenue', 'Compare revenue Oceania FY2024']
    assert client.get(path + '/runs/' + second + '/inputs', headers=headers).json()['budget']['include_scope_in_search'] is True
    assert 'include_scope_in_search' not in client.get(path + '/runs/' + first + '/inputs', headers=headers).json()['budget']
    run(parent, 'question-override-search', {'question_override': 'Compare cash flow', 'include_scope_in_search': True})
    assert queries[-1] == 'Compare cash flow Oceania FY2024'
    bounded = discovery_query('问' * 80, '🌏' * 80, True)
    assert len(bounded) == 70 and bounded == '问' * 45 + ' ' + '🌏' * 24

@pytest.mark.parametrize('reported', [123, None])
def test_invalid_model_output_preserves_reported_usage_and_settles_budget(monkeypatch, reported):
    from app.phase3 import ProviderFailure
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    settlements = []
    class Provider:
        name = 'invalid-usage-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            raise ProviderFailure('invalid_provider_output', 'Invalid output.', retryable=False, reported_tokens=reported)
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    monkeypatch.setattr(api, '_reserve_model_call', lambda *args, **kwargs: None)
    monkeypatch.setattr(api, '_settle_model_call', lambda request, op, usage: settlements.append(usage))
    client = TestClient(api.app)
    account = client.post('/v1/auth/local/register', json={'email': f'invalid-usage-{reported}@example.test',
        'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + account['access_token']}
    source = client.post('/v2/research/sources', headers=headers, json={'title': 'Report', 'text': 'Revenue was 100.',
        'permissions': {'store': True, 'model_input': True, 'display': True}}).json()
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?', 'source_ids': [source['source_id']]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'invalid-usage'},
        json={'parent_version_id': topic['versions'][0]['version_id']})
    op = response.json()['operation']['operation_id']
    for _ in range(100):
        state = client.get(path + '/runs/' + op, headers=headers).json()['operation']['status']
        if state in {'completed', 'failed'}: break
        time.sleep(0.02)
    assert state == 'completed'
    run = client.get(path, headers=headers).json()['versions'][-1]['content']['run']
    assert run['error'] == 'invalid_provider_output' and run['status'] == 'partial'
    assert run['total_tokens'] == reported and run['generation_tokens'] == reported
    assert settlements == [reported]


def test_conflicting_currency_is_rechecked_on_edit_and_recalculation(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Provider:
        name = 'currency-conflict-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def fact(fid, value, year):
                return {'fact_id': fid, 'label': 'Revenue', 'value': value, 'unit': 'million',
                        'currency': 'USD', 'period': year, 'citation': {'source_id': source_id, 'quotation': 'EUR ' + value}}
            return {'facts': [fact('new', '100', '2023'), fact('old', '80', '2022')],
                    'calculations': [{'formula': 'growth_percent', 'first_fact_id': 'new', 'second_fact_id': 'old'}]}, 10
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    account = client.post('/v1/auth/local/register', json={'email': 'currency-conflict@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + account['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title': 'Report', 'text': 'Revenue EUR 100. Revenue EUR 80.',
        'permissions': {'store': True, 'model_input': True, 'display': True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?', 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    receipt = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'currency-conflict'},
        json={'parent_version_id': topic['versions'][0]['version_id']}).json()
    op = receipt['operation']['operation_id']
    for _ in range(100):
        status = client.get(path + '/runs/' + op, headers=headers).json()['operation']['status']
        if status in {'completed', 'failed'}: break
        time.sleep(.02)
    assert status == 'completed'
    current = client.get(path, headers=headers).json()['versions'][-1]
    assert current['content']['run']['status'] == 'partial'
    assert all(f['currency_conflict_unverified'] for f in current['content']['facts'])
    assert not current['content']['calculations']
    content = deepcopy(current['content'])
    inputs = [{key: f[key] for key in ('fact_id', 'value', 'unit', 'currency', 'period')} for f in content['facts']]
    content['calculations'] = [{'formula_id': 'growth_percent', 'formula_version': '1', 'inputs': inputs,
                                'expression': '(first / second - 1) * 100', 'value': '25', 'unit': 'percent'}]
    saved = client.post(path + '/versions', headers=headers, json={'parent_version_id': current['version_id'], 'content': content})
    assert saved.status_code == 201
    latest = saved.json()['versions'][-1]
    assert latest['content']['calculations'][0]['input_status'] == 'unverifiable'
    recalc = client.post(path + '/calculations/recalculate', headers=headers,
                         json={'parent_version_id': latest['version_id'], 'calculation_index': 0})
    assert recalc.status_code == 422
    fixed = deepcopy(latest['content'])
    for fact in fixed['facts']:
        fact['currency'] = 'EUR'
    saved = client.post(path + '/versions', headers=headers, json={'parent_version_id': latest['version_id'], 'content': fixed})
    assert saved.status_code == 201
    latest = saved.json()['versions'][-1]
    assert all(not fact.get('currency_conflict_unverified') for fact in latest['content']['facts'])
    recalc = client.post(path + '/calculations/recalculate', headers=headers,
                         json={'parent_version_id': latest['version_id'], 'calculation_index': 0})
    assert recalc.status_code == 201 and recalc.json()['versions'][-1]['content']['calculations'][0]['value'] == '25.00'


def test_unverified_period_comparability_keeps_automatic_research_partial(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Provider:
        name = 'period-grain-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def item(fid, val, period, quote):
                return {'fact_id':fid, 'label':'Metric', 'value':val, 'unit':'USD', 'currency':'USD', 'period':period,
                        'citation':{'source_id':source_id, 'quotation':quote}}
            return {'claims':[{'text':'Two values were reported.', 'citations':[{'source_id':source_id,'quotation':'Annual revenue was 100'}]}],
                    'facts':[item('annual','100','FY2024','Annual revenue was 100'), item('quarter','80','2023-Q4','Quarterly revenue was 80')],
                    'calculations':[{'formula':'growth_percent','first_fact_id':'annual','second_fact_id':'quarter'}]}, 10
    monkeypatch.setattr(api,'challenge_provider',Provider())
    client=TestClient(api.app)
    account=client.post('/v1/auth/local/register',json={'email':'period-grain@example.test','password':'correct horse battery staple!'}).json()
    headers={'Authorization':'Bearer '+account['access_token']}
    source_id=client.post('/v2/research/sources',headers=headers,json={'title':'Report','text':'Annual revenue was 100. Quarterly revenue was 80.',
        'permissions':{'store':True,'display':True,'model_input':True}}).json()['source_id']
    topic=client.post('/v2/research/topics',headers=headers,json={'question':'Compare values','source_ids':[source_id]}).json()
    path='/v2/research/topics/'+topic['topic_id']
    receipt=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'period-grain-test'},
        json={'parent_version_id':topic['versions'][0]['version_id'],'require_counterarguments':False}).json()
    op=receipt['operation']['operation_id']
    for _ in range(100):
        status=client.get(path+'/runs/'+op,headers=headers).json()['operation']['status']
        if status in {'completed','failed'}: break
        time.sleep(.02)
    assert status=='completed'
    content=client.get(path,headers=headers).json()['versions'][-1]['content']
    assert content['run']['status']=='partial'
    assert content['calculations'][0]['period_granularity_check']=='different_labels_verify_stock_or_flow'
    assert any('Period comparability is not established' in x for x in content['limitations'])


def test_reclassifying_findings_preserves_quotes_but_invalidates_review(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Provider:
        name = 'role-edit-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            if system == REVIEW_SYSTEM_PROMPT:
                return {'reviews': [{'claim_index': 0, 'verdict': 'supports'}]}, 10
            return {'claims': [
                {'text': 'Cash was 100.', 'citations': [{'source_id': source_id, 'quotation': 'Cash was 100.'}]},
                {'text': 'Revenue was reported.', 'citations': [{'source_id': source_id, 'quotation': 'Revenue | $ | 100 | | $ | 90'}]}]}, 20
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'role-edit-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title': 'Report',
        'text': 'Cash was 100.\n| [colspan=3] Revenue | $ | 100 | | [colspan=3] | $ | 90 |',
        'permissions': {'store': True, 'model_input': True, 'display': True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Review results', 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    receipt = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'role-edit'},
        json={'parent_version_id': topic['versions'][0]['version_id'], 'support_review': True, 'max_model_calls': 2, 'max_total_tokens': 9000}).json()
    op = receipt['operation']['operation_id']
    for _ in range(100):
        status = client.get(path + '/runs/' + op, headers=headers).json()['operation']['status']
        if status in {'completed', 'failed'}: break
        time.sleep(.02)
    assert status == 'completed'
    previous = client.get(path, headers=headers).json()['versions'][-1]
    before = previous['content']['claims']
    assert before[0]['support_review']['verdict'] == 'supports'
    assert before[1]['support_status'] == 'incomplete'
    content = deepcopy(previous['content'])
    for claim in content['claims']:
        claim['role'] = 'counterargument'
    saved = client.post(path + '/versions', headers=headers, json={'parent_version_id': previous['version_id'], 'content': content})
    assert saved.status_code == 201, saved.text
    latest = saved.json()['versions'][-1]
    for old, new in zip(before, latest['content']['claims']):
        assert new['role'] == 'counterargument' and new['text'] == old['text']
        assert new['citations'][0]['quotation'] == old['citations'][0]['quotation']
        assert new['authorship'] == 'user' and 'support_review' not in new
    assert latest['content']['claims'][1]['support_status'] == 'incomplete'
    assert saved.json()['versions'][-2]['content']['claims'] == before


def test_table_calculation_alignment_warning_keeps_run_partial(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Provider:
        name = 'table-alignment-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def fact(fid, value, year):
                return {'fact_id': fid, 'label': 'Revenue', 'value': value, 'unit': 'million', 'currency': 'USD',
                        'period': year, 'citation': {'source_id': source_id, 'quotation': 'USD ' + value}}
            return {'claims': [{'text': 'Revenue was reported.', 'citations': [{'source_id': source_id, 'quotation': 'Revenue | USD 100 | USD 90'}]}],
                    'facts': [fact('a','100','2024'), fact('b','90','2023')],
                    'calculations': [{'formula': 'growth_percent', 'first_fact_id': 'a', 'second_fact_id': 'b'}]}, 10
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'alignment-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title': 'Report',
        'text': '| Year Ended | 2024 | 2023 |\n| Revenue | USD 100 | USD 90 |',
        'permissions': {'store': True, 'model_input': True, 'display': True, 'export': True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Review revenue', 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    receipt = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'table-alignment'},
        json={'parent_version_id': topic['versions'][0]['version_id'], 'require_counterarguments': False}).json()
    op = receipt['operation']['operation_id']
    for _ in range(100):
        status = client.get(path + '/runs/' + op, headers=headers).json()['operation']['status']
        if status in {'completed', 'failed'}: break
        time.sleep(.02)
    assert status == 'completed'
    current = client.get(path, headers=headers).json()['versions'][-1]
    content = current['content']
    assert content['run']['status'] == 'partial'
    assert all(item['table_alignment_unverified'] for item in content['facts'])
    assert content['calculations'][0]['value'] == '11.11'
    exported = client.get(path + '/versions/' + current['version_id'] + '/export', headers=headers)
    assert exported.status_code == 200
    assert all(item['table_alignment_unverified'] for item in exported.json()['version']['content']['facts'])


def test_table_period_conflict_excludes_calculation_and_rechecks_edits(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Provider:
        name = 'table-period-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def fact(fid, value, year):
                return {'fact_id': fid, 'label': 'Revenue', 'value': value, 'unit': 'million', 'currency': 'USD',
                        'period': year, 'citation': {'source_id': source_id, 'quotation': 'USD ' + value}}
            return {'claims': [{'text': 'Revenue was reported.', 'citations': [{'source_id': source_id, 'quotation': 'Revenue | USD 100 | USD 90'}]}],
                    'facts': [fact('a','100','2023'), fact('b','90','2024')],
                    'calculations': [{'formula': 'growth_percent', 'first_fact_id': 'a', 'second_fact_id': 'b'}]}, 10
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'period-column-{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title':'Report', 'text':'| Year Ended | 2024 | 2023 |\n| Revenue | USD 100 | USD 90 |',
        'permissions': {'store': True, 'model_input': True, 'display': True, 'export': True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question':'Revenue?', 'source_ids':[source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    receipt = client.post(path + '/runs', headers={**headers, 'Idempotency-Key':'period-column'}, json={'parent_version_id': topic['versions'][0]['version_id'], 'require_counterarguments':False}).json()
    op = receipt['operation']['operation_id']
    for _ in range(100):
        status = client.get(path + '/runs/' + op, headers=headers).json()['operation']['status']
        if status in {'completed','failed'}: break
        time.sleep(.02)
    assert status == 'completed'
    current = client.get(path, headers=headers).json()['versions'][-1]
    content = current['content']
    assert content['run']['status'] == 'partial' and not content['calculations']
    assert [f['table_period_conflict']['column_year'] for f in content['facts']] == ['2024', '2023']
    exported = client.get(path + '/versions/' + current['version_id'] + '/export', headers=headers)
    assert exported.status_code == 200
    assert exported.json()['version']['content']['facts'][0]['table_period_conflict']['column_year'] == '2024'
    edited = deepcopy(content)
    inputs = [{k:f[k] for k in ('fact_id','value','unit','currency','period')} for f in edited['facts']]
    edited['calculations'] = [{'formula_id':'growth_percent','formula_version':'1','inputs':inputs, 'expression':'(first / second - 1) * 100','value':'11.11','unit':'percent'}]
    saved = client.post(path + '/versions', headers=headers, json={'parent_version_id':current['version_id'],'content':edited})
    assert saved.status_code == 201, saved.text
    latest = saved.json()['versions'][-1]
    assert latest['content']['calculations'][0]['input_status'] == 'unverifiable'
    assert client.post(path + '/calculations/recalculate', headers=headers, json={'parent_version_id':latest['version_id'],'calculation_index':0}).status_code == 422
    fixed = deepcopy(latest['content'])
    fixed['facts'][0]['period'],fixed['facts'][1]['period'] = '2024','2023'
    for fact in fixed['facts']: fact['table_period_conflict'] = {'declared_year':'2023','column_year':'2024'}
    saved = client.post(path + '/versions', headers=headers, json={'parent_version_id':latest['version_id'],'content':fixed})
    assert saved.status_code == 201, saved.text
    latest = saved.json()['versions'][-1]
    assert all(not fact.get('table_period_conflict') for fact in latest['content']['facts'])
    recalc = client.post(path + '/calculations/recalculate', headers=headers, json={'parent_version_id':latest['version_id'],'calculation_index':0})
    assert recalc.status_code == 201 and recalc.json()['versions'][-1]['content']['calculations'][0]['value'] == '11.11'
    assert all(f.get('table_alignment_unverified') for f in latest['content']['facts'])


def test_material_topic_accepts_versioned_run_questions_and_idempotency(monkeypatch):
    import json
    from hashlib import sha256
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    received = []
    class Provider:
        name = 'run-question-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            data = json.loads(prompt)
            received.append(data['question'])
            return {'claims':[{'text':'Cash was 100.', 'citations':[{'source_id': source_id, 'quotation':'Cash was 100.'}]}]}, 10
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email':f'run-question-{uuid4().hex}@example.test','password':'correct horse battery staple!'}).json()
    headers = {'Authorization':'Bearer '+auth['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title':'Report','text':'Cash was 100.',
        'permissions':{'store':True,'display':True,'model_input':True,'export':True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'source_ids':[source_id]}).json()
    assert topic['request']['question'] == ''
    path = '/v2/research/topics/' + topic['topic_id']
    def run(parent, key, question):
        body = {'parent_version_id':parent,'question_override':question,'require_counterarguments':False}
        response = client.post(path+'/runs', headers={**headers,'Idempotency-Key':key}, json=body)
        assert response.status_code == 202, response.text
        op = response.json()['operation']['operation_id']
        for _ in range(100):
            status = client.get(path+'/runs/'+op, headers=headers).json()['operation']['status']
            if status in {'completed','failed'}: break
            time.sleep(.02)
        assert status == 'completed'
        return body, op, client.get(path, headers=headers).json()
    first_body, first_op, updated = run(topic['versions'][0]['version_id'], 'question-one', '  What was cash?  ')
    first = updated['versions'][-1]
    assert received == ['What was cash?']
    assert first['content']['run']['question_override'] == 'What was cash?'
    inputs = client.get(path+'/runs/'+first_op+'/inputs', headers=headers).json()
    assert inputs['question_sha256'] == sha256(b'What was cash?').hexdigest()
    assert 'question_override' not in inputs['budget'] and 'What was cash?' not in str(inputs)
    replay = client.post(path+'/runs', headers={**headers,'Idempotency-Key':'question-one'}, json=first_body)
    assert replay.status_code == 202 and replay.json()['operation']['operation_id'] == first_op and len(received) == 1
    changed = {**first_body,'question_override':'A different question?'}
    assert client.post(path+'/runs', headers={**headers,'Idempotency-Key':'question-one'}, json=changed).status_code == 409
    _, _, updated = run(first['version_id'], 'question-two', 'What was profit?')
    assert received == ['What was cash?', 'What was profit?']
    assert updated['versions'][-2]['content']['run']['question_override'] == 'What was cash?'
    assert updated['versions'][-1]['content']['run']['question_override'] == 'What was profit?'
    assert updated['request']['question'] == ''
    exported = client.get(path+'/versions/'+first['version_id']+'/export', headers=headers).json()
    assert exported['version']['content']['run']['question_override'] == 'What was cash?'
    invalid = {'parent_version_id':updated['versions'][-1]['version_id'],'question_override':'   '}
    assert client.post(path+'/runs', headers={**headers,'Idempotency-Key':'question-empty'}, json=invalid).status_code == 422


def test_larger_provider_input_limit_requires_matching_research_budget(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    calls, reservations = [], []
    class Provider:
        name = "expanded-input-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=6000, max_output_tokens=4000)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            calls.append(prompt)
            return {"claims": [{"text": "Revenue was 100", "citations": [{"source_id": source_id, "quotation": "Revenue was 100"}]}]}, 20
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: reservations.append(kwargs["token_budget"]))
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    registered = client.post("/v1/auth/local/register", json={"email":"expanded-context@example.test", "password":"correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + registered["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title":"Report", "text":"Revenue was 100.", "permissions":{"store":True,"display":True,"model_input":True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question":"Revenue?", "source_ids":[source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    parent = topic["versions"][-1]["version_id"]
    for limit, expected_calls in ((9999, 0), (10000, 1)):
        receipt = client.post(path + "/runs", headers={**headers, "Idempotency-Key": f"expanded-{limit}"}, json={"parent_version_id":parent,"max_total_tokens":limit,"require_counterarguments":False})
        assert receipt.status_code == 202
        op = receipt.json()["operation"]["operation_id"]
        for _ in range(100):
            status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
            if status in ("completed", "failed"): break
            time.sleep(.02)
        assert status == "completed" and len(calls) == expected_calls
        result = client.get(path, headers=headers).json()["versions"][-1]
        assert result["content"]["run"]["provider_input_token_limit"] == 6000
        if not expected_calls: assert result["content"]["run"]["error"] == "provider_budget_exhausted"
        parent = result["version_id"]
    assert reservations == [10000]


def test_per_run_input_limit_is_bounded_recorded_and_reserved(monkeypatch):
    from app.openai_compatible import OpenAICompatibleLlmProvider
    from app.phase3 import ProviderPolicy
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.setenv("FORESIGHTNINE_RESEARCH_MAX_INPUT_TOKENS", "6000")
    calls, reservations = [], []
    provider = OpenAICompatibleLlmProvider(policy=ProviderPolicy(max_input_tokens=3000, max_output_tokens=1500, timeout_seconds=5))
    def generate(self, prompt, *, cancel_event, system, timeout_seconds):
        calls.append(self.policy.max_input_tokens)
        return {"claims": [{"text": "Revenue was 100", "citations": [{"source_id": source_id, "quotation": "Revenue was 100"}]}]}, 20
    monkeypatch.setattr(OpenAICompatibleLlmProvider, "generate_structured", generate)
    monkeypatch.setattr(api, "challenge_provider", provider)
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: reservations.append(kwargs["token_budget"]))
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    reg = client.post("/v1/auth/local/register", json={"email":"per-run-budget@example.test", "password":"correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + reg["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title":"Report", "text":"Revenue was 100.", "permissions":{"store":True,"display":True,"model_input":True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question":"Revenue?", "source_ids":[source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    payload = {"parent_version_id":topic["versions"][-1]["version_id"], "max_input_tokens_per_call":2500,
               "max_total_tokens":4000, "require_counterarguments":False}
    for invalid in (0, 6001, 12001, True):
        bad = client.post(path + "/runs", headers={**headers,"Idempotency-Key":f"invalid-cap-{invalid}"}, json={**payload,"max_input_tokens_per_call":invalid})
        assert bad.status_code == 422
    receipt = client.post(path + "/runs", headers={**headers,"Idempotency-Key":"per-run-cap"}, json=payload)
    assert receipt.status_code == 202
    op = receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + op, headers=headers).json()["operation"]["status"]
        if status in ("completed", "failed"): break
        time.sleep(.02)
    assert status == "completed" and calls == [2500] and reservations == [4000]
    snapshot = client.get(path + "/runs/" + op + "/inputs", headers=headers).json()
    assert snapshot["budget"]["max_input_tokens_per_call"] == 2500
    assert snapshot["provider_limits"]["max_input_tokens"] == 2500
    assert provider.policy.max_input_tokens == 3000
    replay = client.post(path + "/runs", headers={**headers,"Idempotency-Key":"per-run-cap"}, json=payload)
    assert replay.status_code == 202 and replay.headers["Idempotency-Replayed"] == "true"
    assert calls == [2500]
    # A subsequent configuration change must not hide an already accepted
    # operation or cause a duplicate provider request.
    monkeypatch.setenv("FORESIGHTNINE_RESEARCH_MAX_INPUT_TOKENS", "2000")
    replay = client.post(path + "/runs", headers={**headers,"Idempotency-Key":"per-run-cap"}, json=payload)
    assert replay.status_code == 202 and replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json()["operation"]["operation_id"] == op and calls == [2500]
    conflict = client.post(path + "/runs", headers={**headers,"Idempotency-Key":"per-run-cap"}, json={**payload,"max_input_tokens_per_call":2000})
    assert conflict.status_code == 409 and calls == [2500]
    fresh = client.post(path + "/runs", headers={**headers,"Idempotency-Key":"new-over-cap"}, json=payload)
    assert fresh.status_code == 422 and calls == [2500]


def test_related_source_sentence_is_reviewable_and_recomputed_after_edit(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    text = "Officials may adjust policy. They monitor labor market conditions and inflation pressures."
    first, second = "Officials may adjust policy.", "They monitor labor market conditions and inflation pressures."
    class Provider:
        name = "related-context-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            return {"claims":[{"text":"Officials may adjust policy and monitor labor market conditions.",
                    "citations":[{"source_id":source_id,"quotation":first}]}]}, 10
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    user = client.post("/v1/auth/local/register", json={"email":"related-context@example.test", "password":"correct horse battery staple!"}).json()
    headers = {"Authorization":"Bearer "+user["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title":"Report", "text":text,
        "permissions":{"store":True,"display":True,"model_input":True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question":"What do officials monitor?", "source_ids":[source_id]}).json()
    path = "/v2/research/topics/"+topic["topic_id"]
    response = client.post(path+"/runs", headers={**headers,"Idempotency-Key":"related-context-run"},
        json={"parent_version_id":topic["versions"][0]["version_id"], "require_counterarguments":False})
    assert response.status_code == 202
    op = response.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path+"/runs/"+op,headers=headers).json()["operation"]["status"]
        if status in {"completed","failed"}: break
        time.sleep(.02)
    assert status == "completed"
    version = client.get(path,headers=headers).json()["versions"][-1]
    content = version["content"]
    assert content["run"]["status"] == "partial"
    assert content["claims"][0]["citation_context_warnings"][0]["context_metric"] == "related unquoted sentence"
    edited = deepcopy(content)
    edited["claims"][0]["citations"].append({"source_id":source_id,"quotation":second})
    saved = client.post(path+"/versions",headers=headers,json={"parent_version_id":version["version_id"],"content":edited})
    assert saved.status_code == 201
    latest = saved.json()["versions"][-1]["content"]["claims"][0]
    assert not latest.get("citation_context_warnings") and latest["authorship"] == "user"
    assert content["claims"][0]["citation_context_warnings"]


def test_unquoted_dollar_figures_remain_partial_until_source_quotes_are_added(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    text = "Goods deficit narrowed $0.3 billion. Exports increased $5.8 billion; imports rose $7.1 billion."
    first, second = "Goods deficit narrowed $0.3 billion.", "Exports increased $5.8 billion; imports rose $7.1 billion."
    class Provider:
        name = "dollar-gap-test"
        policy = SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            return {"claims":[{"text":"Imports rose $7.1 billion and exports rose $5.8 billion.",
                    "citations":[{"source_id":source_id,"quotation":first}]}]},10
    monkeypatch.setattr(api,"challenge_provider",Provider())
    client=TestClient(api.app)
    user=client.post("/v1/auth/local/register",json={"email":"dollar-gap@example.test","password":"correct horse battery staple!"}).json()
    headers={"Authorization":"Bearer "+user["access_token"]}
    source_id=client.post("/v2/research/sources",headers=headers,json={"title":"Report","text":text,
        "permissions":{"store":True,"display":True,"model_input":True}}).json()["source_id"]
    topic=client.post("/v2/research/topics",headers=headers,json={"question":"Imports and exports?","source_ids":[source_id]}).json()
    path="/v2/research/topics/"+topic["topic_id"]
    receipt=client.post(path+"/runs",headers={**headers,"Idempotency-Key":"dollar-gap-test"},
        json={"parent_version_id":topic["versions"][0]["version_id"],"require_counterarguments":False})
    assert receipt.status_code==202
    op=receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        state=client.get(path+"/runs/"+op,headers=headers).json()["operation"]["status"]
        if state in {"completed","failed"}:break
        time.sleep(.02)
    assert state=="completed"
    version=client.get(path,headers=headers).json()["versions"][-1];content=version["content"]
    assert content["run"]["status"]=="partial"
    assert content["claims"][0]["uncited_dollar_amounts"]==["$7.1 billion","$5.8 billion"]
    edited=deepcopy(content);edited["claims"][0]["citations"].append({"source_id":source_id,"quotation":second})
    saved=client.post(path+"/versions",headers=headers,json={"parent_version_id":version["version_id"],"content":edited})
    assert saved.status_code==201
    assert not saved.json()["versions"][-1]["content"]["claims"][0].get("uncited_dollar_amounts")
    assert content["claims"][0]["uncited_dollar_amounts"]


def test_quarter_evidence_gap_is_recomputed_after_adding_quote(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    text = "The actual ratio was 12.4 percent in the fourth quarter of 2022. The horizon ends in 2025:Q1."
    first, second = "The actual ratio was 12.4 percent in the fourth quarter of 2022.", "The horizon ends in 2025:Q1."
    class Provider:
        name = "quarter-gap-test"
        policy = SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            return {"claims":[{"text":"The actual ratio was 12.4 percent in 2022:Q4 and the horizon ends in 2025:Q1.",
                    "citations":[{"source_id":source_id,"quotation":first}]}]},10
    monkeypatch.setattr(api,"challenge_provider",Provider())
    client=TestClient(api.app)
    user=client.post("/v1/auth/local/register",json={"email":"quarter-gap@example.test","password":"correct horse battery staple!"}).json()
    headers={"Authorization":"Bearer "+user["access_token"]}
    source_id=client.post("/v2/research/sources",headers=headers,json={"title":"Report","text":text,
        "permissions":{"store":True,"display":True,"model_input":True}}).json()["source_id"]
    topic=client.post("/v2/research/topics",headers=headers,json={"question":"What was the period?","source_ids":[source_id]}).json()
    path="/v2/research/topics/"+topic["topic_id"]
    receipt=client.post(path+"/runs",headers={**headers,"Idempotency-Key":"quarter-gap-test"},
        json={"parent_version_id":topic["versions"][0]["version_id"],"require_counterarguments":False})
    assert receipt.status_code==202
    op=receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        state=client.get(path+"/runs/"+op,headers=headers).json()["operation"]["status"]
        if state in {"completed","failed"}:break
        time.sleep(.02)
    assert state=="completed"
    version=client.get(path,headers=headers).json()["versions"][-1];content=version["content"]
    assert content["run"]["status"]=="partial" and content["claims"][0]["uncited_quarters"]==["2025:Q1"]
    edited=deepcopy(content);edited["claims"][0]["citations"].append({"source_id":source_id,"quotation":second})
    saved=client.post(path+"/versions",headers=headers,json={"parent_version_id":version["version_id"],"content":edited})
    assert saved.status_code==201
    assert not saved.json()["versions"][-1]["content"]["claims"][0].get("uncited_quarters")
    assert content["claims"][0]["uncited_quarters"]


def test_trade_balance_direction_warning_is_recomputed_after_edit(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    text = "The deficit reflected a decrease in the goods deficit and a decrease in the services surplus."
    class Provider:
        name = "balance-direction-test"
        policy = SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            return {"claims":[{"text":"Both changes would narrow the deficit.",
                    "citations":[{"source_id":source_id,"quotation":text}]}]},10
    monkeypatch.setattr(api,"challenge_provider",Provider())
    client=TestClient(api.app)
    user=client.post("/v1/auth/local/register",json={"email":"balance-direction@example.test","password":"correct horse battery staple!"}).json()
    headers={"Authorization":"Bearer "+user["access_token"]}
    source_id=client.post("/v2/research/sources",headers=headers,json={"title":"Trade","text":text,
        "permissions":{"store":True,"display":True,"model_input":True}}).json()["source_id"]
    topic=client.post("/v2/research/topics",headers=headers,json={"question":"What changed?","source_ids":[source_id]}).json()
    path="/v2/research/topics/"+topic["topic_id"]
    receipt=client.post(path+"/runs",headers={**headers,"Idempotency-Key":"balance-direction-test"},
        json={"parent_version_id":topic["versions"][0]["version_id"],"require_counterarguments":False})
    assert receipt.status_code==202
    op=receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        state=client.get(path+"/runs/"+op,headers=headers).json()["operation"]["status"]
        if state in {"completed","failed"}:break
        time.sleep(.02)
    assert state=="completed"
    version=client.get(path,headers=headers).json()["versions"][-1];content=version["content"]
    assert content["run"]["status"]=="partial" and content["claims"][0]["balance_direction_warnings"]
    edited=deepcopy(content)
    edited["claims"][0]["text"]="The goods deficit narrowed while the smaller services surplus widened the overall deficit."
    saved=client.post(path+"/versions",headers=headers,json={"parent_version_id":version["version_id"],"content":edited})
    assert saved.status_code==201 and not saved.json()["versions"][-1]["content"]["claims"][0].get("balance_direction_warnings")
    assert content["claims"][0]["balance_direction_warnings"]


def test_revision_contradiction_warning_survives_version_and_is_recomputed(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    text="The estimate reflects a downward revision to consumer spending."
    class Provider:
        name="revision-direction-test"
        policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            return {"claims":[{"text":"The downward revision is contrary to a claim that spending rose.",
                    "citations":[{"source_id":source_id,"quotation":text}]}]},10
    monkeypatch.setattr(api,"challenge_provider",Provider())
    client=TestClient(api.app)
    user=client.post("/v1/auth/local/register",json={"email":"revision-direction@example.test","password":"correct horse battery staple!"}).json()
    headers={"Authorization":"Bearer "+user["access_token"]}
    source_id=client.post("/v2/research/sources",headers=headers,json={"title":"Report","text":text,
        "permissions":{"store":True,"display":True,"model_input":True,"export":True}}).json()["source_id"]
    topic=client.post("/v2/research/topics",headers=headers,json={"question":"What changed?","source_ids":[source_id]}).json()
    path="/v2/research/topics/"+topic["topic_id"]
    receipt=client.post(path+"/runs",headers={**headers,"Idempotency-Key":"revision-direction-test"},
        json={"parent_version_id":topic["versions"][0]["version_id"],"require_counterarguments":False})
    assert receipt.status_code==202
    op=receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        state=client.get(path+"/runs/"+op,headers=headers).json()["operation"]["status"]
        if state in {"completed","failed"}:break
        time.sleep(.02)
    assert state=="completed"
    version=client.get(path,headers=headers).json()["versions"][-1];content=version["content"]
    assert content["run"]["status"]=="partial"
    assert content["claims"][0]["balance_direction_warnings"][0]["basis"]=="revision_vs_change"
    edited=deepcopy(content)
    edited["claims"][0]["text"]="The downward revision does not contradict growth."
    saved=client.post(path+"/versions",headers=headers,json={"parent_version_id":version["version_id"],"content":edited})
    assert saved.status_code==201 and not saved.json()["versions"][-1]["content"]["claims"][0].get("balance_direction_warnings")
    assert content["claims"][0]["balance_direction_warnings"]


def test_model_omitted_review_is_partial_and_returned_verdict_is_retained(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    class Provider:
        name="model-omitted-review-test"
        policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            if system==REVIEW_SYSTEM_PROMPT:
                return {"reviews":[{"claim_index":0,"verdict":"supports"}]},10
            cite={"source_id":source_id,"quotation":"Revenue was 100."}
            return {"claims":[{"text":"Revenue was 100.","citations":[cite]},
                              {"text":"The report stated revenue.","citations":[cite]}]},20
    monkeypatch.setattr(api,"challenge_provider",Provider())
    client=TestClient(api.app)
    user=client.post("/v1/auth/local/register",json={"email":"review-model-omission@example.test","password":"correct horse battery staple!"}).json()
    headers={"Authorization":"Bearer "+user["access_token"]}
    source_id=client.post("/v2/research/sources",headers=headers,json={"title":"Report","text":"Revenue was 100.",
        "permissions":{"store":True,"display":True,"model_input":True}}).json()["source_id"]
    topic=client.post("/v2/research/topics",headers=headers,json={"question":"Revenue?","source_ids":[source_id]}).json()
    path="/v2/research/topics/"+topic["topic_id"]
    receipt=client.post(path+"/runs",headers={**headers,"Idempotency-Key":"model-omitted-review"},json={
        "parent_version_id":topic["versions"][0]["version_id"],"require_counterarguments":False,
        "support_review":True,"max_model_calls":2,"max_total_tokens":9000})
    assert receipt.status_code==202
    op=receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        status=client.get(path+"/runs/"+op,headers=headers).json()["operation"]["status"]
        if status in {"completed","failed"}:break
        time.sleep(.02)
    assert status=="completed"
    versions=client.get(path,headers=headers).json()["versions"]
    content=versions[-1]["content"];run=content["run"]
    assert run["status"]=="partial" and run["support_review_status"]=="partial"
    assert run["support_review_indexes"]==[0,1] and run["support_review_omissions"]==[{"claim_index":1,"reason":"model_omitted"}]
    assert run["total_tokens"]==30 and run["model_calls"]==2
    assert content["claims"][0]["support_review"]["verdict"]=="supports"
    assert content["claims"][0]["support_review"]["independently_verified"] is False
    assert "support_review" not in content["claims"][1]
    assert versions[-2]["content"]["run"]["checkpoint"]=="generation"


def test_pdf_line_hyphen_caution_survives_claim_edits(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""
    class Provider:
        name = "pdf-hyphen-edit-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            return {"claims":[{"text":"The estimates are hypothetical.", "citations":[{"source_id":source_id,
                "quotation":"These are hypothetical estimates that may differ."}]}]}, 20
    monkeypatch.setattr(api,"challenge_provider",Provider())
    client=TestClient(api.app)
    reg=client.post("/v1/auth/local/register",json={"email":f"hyphen-edit-{uuid4().hex}@example.test","password":"correct horse battery staple!"}).json()
    headers={"Authorization":"Bearer "+reg["access_token"]}
    original="These are hypothetical esti-\nmates that may differ."
    source_id=client.post("/v2/research/sources",headers=headers,json={"title":"Report","text":original,
        "permissions":{"store":True,"model_input":True,"display":True}}).json()["source_id"]
    source=api.RESEARCH_SOURCES.get(source_id)
    api.RESEARCH_SOURCES.put(source_id,source.model_copy(update={"retrieval_method":"user_pdf"}))
    topic=client.post("/v2/research/topics",headers=headers,json={"question":"Are estimates hypothetical?","source_ids":[source_id]}).json()
    path="/v2/research/topics/"+topic["topic_id"]
    response=client.post(path+"/runs",headers={**headers,"Idempotency-Key":"hyphen-edit"},json={
        "parent_version_id":topic["versions"][0]["version_id"],"require_counterarguments":False})
    assert response.status_code==202, response.text
    op=response.json()["operation"]["operation_id"]
    for _ in range(100):
        status=client.get(path+"/runs/"+op,headers=headers).json()["operation"]["status"]
        if status in {"completed","failed"}:break
        time.sleep(.02)
    assert status=="completed"
    version=client.get(path,headers=headers).json()["versions"][-1]
    claim=version["content"]["claims"][0]
    assert claim["support_status"]=="incomplete"
    assert claim["citations"][0]["quotation_match"]=="pdf_line_hyphen_restored"
    edited=deepcopy(version["content"]);edited["claims"][0]["text"]="The projected estimates may differ."
    saved=client.post(path+"/versions",headers=headers,json={"parent_version_id":version["version_id"],"content":edited})
    assert saved.status_code==201, saved.text
    latest=saved.json()["versions"][-1]
    assert latest["content"]["claims"][0]["support_status"]=="incomplete"
    assert latest["content"]["claims"][0]["citations"][0]["quotation"]==original
    assert latest["content"]["claims"][0]["authorship"]=="user"
    assert saved.json()["versions"][-2]["content"]["claims"][0]["support_status"]=="incomplete"


def test_split_ellipsis_caution_survives_claim_edits(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""
    class Provider:
        name = "split-ellipsis-edit-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=6000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            return {"claims": [{"text": "Example revenue was 100.", "citations": [{"source_id": source_id,
                "quotation": "Issuer Example ... Revenue was 100"}]}]}, 20
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    reg = client.post("/v1/auth/local/register", json={"email": f"ellipsis-{uuid4().hex}@example.test",
        "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + reg["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report",
        "text": "Issuer Example. " + "Context. " * 140 + "Revenue was 100.",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Issuer and revenue?",
        "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    response = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "ellipsis-edit"}, json={
        "parent_version_id": topic["versions"][0]["version_id"], "require_counterarguments": False})
    assert response.status_code == 202, response.text
    operation = response.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + operation, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(.02)
    assert status == "completed"
    version = client.get(path, headers=headers).json()["versions"][-1]
    claim = version["content"]["claims"][0]
    assert claim["support_status"] == "incomplete"
    assert [c["quotation_match"] for c in claim["citations"]] == ["split_ellipsis", "split_ellipsis"]
    edited = deepcopy(version["content"])
    edited["claims"][0]["text"] = "Example reported revenue."
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": version["version_id"], "content": edited})
    assert saved.status_code == 201, saved.text
    assert saved.json()["versions"][-1]["content"]["claims"][0]["support_status"] == "incomplete"
    assert saved.json()["versions"][-2]["content"]["claims"][0]["support_status"] == "incomplete"


def test_restored_table_fact_cannot_be_calculated_or_clear_warning_by_edit(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""
    class Provider:
        name = "table-fact-edit-test"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            def fact(key, value, period):
                return {"fact_id": key, "label": "Revenue", "value": value, "unit": "USD million", "currency": "USD",
                        "period": period, "citation": {"source_id": source_id, "quotation": "Revenue | 100 | 80"}}
            return {"facts": [fact("current", "100", "2024"), fact("prior", "80", "2023")],
                    "calculations": [{"formula": "growth_percent", "first_fact_id": "current", "second_fact_id": "prior"}]}, 20
    monkeypatch.setattr(api, "challenge_provider", Provider())
    client = TestClient(api.app)
    reg = client.post("/v1/auth/local/register", json={"email": f"table-{uuid4().hex}@example.test", "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + reg["access_token"]}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report",
        "text": "| [colspan=3] Revenue | [colspan=2] 100 | | [colspan=2] 80 | |",
        "permissions": {"store": True, "model_input": True, "display": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"]
    response = client.post(path + "/runs", headers={**headers, "Idempotency-Key": "table-fact-edit"}, json={"parent_version_id": topic["versions"][0]["version_id"]})
    assert response.status_code == 202
    operation = response.json()["operation"]["operation_id"]
    for _ in range(100):
        status = client.get(path + "/runs/" + operation, headers=headers).json()["operation"]["status"]
        if status in {"completed", "failed"}: break
        time.sleep(.02)
    assert status == "completed"
    version = client.get(path, headers=headers).json()["versions"][-1]
    assert len(version["content"]["facts"]) == 2 and not version["content"]["calculations"]
    payload = {"parent_version_id": version["version_id"], "formula": "growth_percent", "first_fact_id": "current", "second_fact_id": "prior"}
    assert client.post(path + "/calculations", headers=headers, json=payload).status_code == 422
    edited = deepcopy(version["content"])
    edited["facts"][0].pop("quotation_match")
    edited["facts"][0]["label"] = "Edited revenue"
    edited["facts"][0]["table_confirmation"] = {"confirmed_by": "user", "fact_sha256": "forged"}
    saved = client.post(path + "/versions", headers=headers, json={"parent_version_id": version["version_id"], "content": edited})
    assert saved.status_code == 201, saved.text
    latest = saved.json()["versions"][-1]
    assert latest["content"]["facts"][0]["quotation_match"] == "table_markup_expanded"
    assert "table_confirmation" not in latest["content"]["facts"][0]
    assert client.post(path + "/calculations", headers=headers, json={**payload, "parent_version_id": latest["version_id"]}).status_code == 422
    confirm = lambda parent, fact_id: client.post(path + "/facts/confirm-table", headers=headers,
        json={"parent_version_id": parent, "fact_id": fact_id})
    assert confirm(latest["version_id"], "unknown").status_code == 422
    other = client.post("/v1/auth/local/register", json={"email": f"other-{uuid4().hex}@example.test", "password": "correct horse battery staple!"}).json()
    assert client.post(path + "/facts/confirm-table", headers={"Authorization": "Bearer " + other["access_token"]},
                       json={"parent_version_id": latest["version_id"], "fact_id": "current"}).status_code == 404
    first = confirm(latest["version_id"], "current")
    assert first.status_code == 201, first.text
    first_version = first.json()["versions"][-1]
    assert first_version["content"]["facts"][0]["table_confirmation"]["confirmed_by"] == "user"
    assert first_version["content"]["facts"][0]["verification"] == "user_edited_not_independently_verified"
    assert client.post(path + "/calculations", headers=headers, json={**payload, "parent_version_id": first_version["version_id"]}).status_code == 422
    second = confirm(first_version["version_id"], "prior")
    assert second.status_code == 201, second.text
    reviewed = second.json()["versions"][-1]
    calculation = client.post(path + "/calculations", headers=headers, json={**payload, "parent_version_id": reviewed["version_id"]})
    assert calculation.status_code == 201, calculation.text
    calculated = calculation.json()["versions"][-1]
    assert calculated["content"]["calculations"][-1]["value"] == "25.00"
    modified = deepcopy(calculated["content"])
    modified["facts"][0]["period"] = "2025"
    changed = client.post(path + "/versions", headers=headers, json={"parent_version_id": calculated["version_id"], "content": modified})
    assert changed.status_code == 201, changed.text
    changed_version = changed.json()["versions"][-1]
    assert "table_confirmation" not in changed_version["content"]["facts"][0]
    assert changed_version["content"]["calculations"][-1]["input_status"] == "unverifiable"
    assert confirm(latest["version_id"], "current").status_code == 409


def test_uncited_causal_claim_remains_partial_and_is_rechecked_on_edit(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    source_id = ''
    class Provider:
        name='causal-cue-test';policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            return {'claims':[{'text':'Revenue fell due to a supply shock.',
                'citations':[{'source_id':source_id,'quotation':'Revenue fell.'}]}]},10
    monkeypatch.setattr(api,'challenge_provider',Provider())
    client=TestClient(api.app)
    reg=client.post('/v1/auth/local/register',json={'email':f'cause-{uuid4().hex}@example.test','password':'correct horse battery staple!'}).json()
    headers={'Authorization':'Bearer '+reg['access_token']}
    source_id=client.post('/v2/research/sources',headers=headers,json={'title':'Report','text':'Revenue fell.',
        'permissions':{'store':True,'display':True,'model_input':True}}).json()['source_id']
    topic=client.post('/v2/research/topics',headers=headers,json={'question':'What changed?','source_ids':[source_id]}).json()
    path='/v2/research/topics/'+topic['topic_id']
    started=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'causal-cue-test'},json={
        'parent_version_id':topic['versions'][0]['version_id'],'require_counterarguments':False})
    assert started.status_code==202
    op=started.json()['operation']['operation_id']
    for _ in range(100):
        status=client.get(path+'/runs/'+op,headers=headers).json()['operation']['status']
        if status in {'completed','failed'}:break
        time.sleep(.02)
    assert status=='completed'
    version=client.get(path,headers=headers).json()['versions'][-1]
    assert version['content']['run']['status']=='partial'
    assert version['content']['claims'][0]['causal_attribution_unverified'] is True
    content=deepcopy(version['content']);content['claims'][0]['text']='Revenue fell.'
    saved=client.post(path+'/versions',headers=headers,json={'parent_version_id':version['version_id'],'content':content})
    assert saved.status_code==201,saved.text
    versions=saved.json()['versions']
    assert not versions[-1]['content']['claims'][0].get('causal_attribution_unverified')
    assert versions[-2]['content']['claims'][0]['causal_attribution_unverified'] is True


def test_inferred_negative_dollar_change_remains_unusable_for_recalculation_after_edit(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    source_id = ""
    class Provider:
        name = "amount-direction-test"
        policy = SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            def item(key,value,quote):
                return {"fact_id":key,"label":"spending change","value":value,"unit":"billions of dollars",
                    "currency":"USD","period":"2023-03","citation":{"source_id":source_id,"quotation":quote}}
            return {"facts":[item("goods","-36.7","a $36.7 billion decrease"),
                             item("services","44.9","increased $44.9 billion")],
                    "calculations":[{"formula":"difference","first_fact_id":"services","second_fact_id":"goods"}]},10
    monkeypatch.setattr(api,"challenge_provider",Provider())
    client=TestClient(api.app)
    account=client.post("/v1/auth/local/register",json={"email":f"amount-direction-{uuid4().hex}@example.test",
        "password":"correct horse battery staple!"}).json()
    headers={"Authorization":"Bearer "+account["access_token"]}
    source_id=client.post("/v2/research/sources",headers=headers,json={"title":"Report",
        "text":"Goods had a $36.7 billion decrease. Services increased $44.9 billion.",
        "permissions":{"store":True,"model_input":True,"display":True}}).json()["source_id"]
    topic=client.post("/v2/research/topics",headers=headers,json={"question":"Spending?","source_ids":[source_id]}).json()
    path="/v2/research/topics/"+topic["topic_id"]
    receipt=client.post(path+"/runs",headers={**headers,"Idempotency-Key":"amount-direction"},json={
        "parent_version_id":topic["versions"][0]["version_id"]}).json()["operation"]
    for _ in range(100):
        status=client.get(path+"/runs/"+receipt["operation_id"],headers=headers).json()["operation"]["status"]
        if status in {"completed","failed"}:break
        time.sleep(.02)
    assert status=="completed"
    current=client.get(path,headers=headers).json()["versions"][-1];content=deepcopy(current["content"])
    goods=next(item for item in content["facts"] if item["fact_id"]=="goods")
    assert goods.pop("direction_context_unverified") is True
    assert not content["calculations"] and content["run"]["status"]=="partial"
    content["calculations"]=[{"formula_id":"difference","formula_version":"1","inputs":[{"fact_id":"services"},{"fact_id":"goods"}]}]
    saved=client.post(path+"/versions",headers=headers,json={"parent_version_id":current["version_id"],"content":content})
    assert saved.status_code==201, saved.text
    latest=saved.json()["versions"][-1]
    assert next(item for item in latest["content"]["facts"] if item["fact_id"]=="goods")["direction_context_unverified"] is True
    recalculated=client.post(path+"/calculations/recalculate",headers=headers,json={"parent_version_id":latest["version_id"],"calculation_index":0})
    assert recalculated.status_code==422 and "direction and subject require review" in recalculated.json()["message"]
    spec={"parent_version_id":latest["version_id"],"formula":"difference","first_fact_id":"services","second_fact_id":"goods"}
    assert client.post(path+"/calculations",headers=headers,json=spec).status_code==422
    assert client.post(path+"/facts/confirm-direction",headers=headers,json={"parent_version_id":latest["version_id"],"fact_id":"services"}).status_code==422
    confirmed=client.post(path+"/facts/confirm-direction",headers=headers,json={"parent_version_id":latest["version_id"],"fact_id":"goods"})
    assert confirmed.status_code==201, confirmed.text
    reviewed=confirmed.json()["versions"][-1]
    reviewed_goods=next(item for item in reviewed["content"]["facts"] if item["fact_id"]=="goods")
    assert reviewed_goods["direction_context_unverified"] is True
    assert reviewed_goods["direction_confirmation"]["confirmed_by"]=="user"
    other=client.post("/v1/auth/local/register",json={"email":f"other-direction-{uuid4().hex}@example.test",
        "password":"correct horse battery staple!"}).json()
    other_headers={"Authorization":"Bearer "+other["access_token"]}
    assert client.post(path+"/facts/confirm-direction",headers=other_headers,json={"parent_version_id":reviewed["version_id"],"fact_id":"goods"}).status_code==404
    assert not next(item for item in confirmed.json()["versions"][-2]["content"]["facts"] if item["fact_id"]=="goods").get("direction_confirmation")
    assert client.post(path+"/facts/confirm-direction",headers=headers,json={"parent_version_id":latest["version_id"],"fact_id":"goods"}).status_code==409
    spec["parent_version_id"]=reviewed["version_id"]
    assert client.post(path+"/calculations",headers=other_headers,json=spec).status_code==404
    created=client.post(path+"/calculations",headers=headers,json=spec)
    assert created.status_code==201, created.text
    created_version=created.json()["versions"][-1]
    assert created_version["content"]["calculations"][-1]["value"]=="81.60"
    assert created_version["content"]["calculations"][-1]["input_status"]=="matched"
    assert client.post(path+"/calculations",headers=headers,json=spec).status_code==409
    recalculated=client.post(path+"/calculations/recalculate",headers=headers,json={"parent_version_id":created_version["version_id"],"calculation_index":0})
    assert recalculated.status_code==201, recalculated.text
    calculated=recalculated.json()["versions"][-1]
    assert calculated["content"]["calculations"][0]["input_status"]=="matched"
    changed=deepcopy(calculated["content"])
    changed_goods=next(item for item in changed["facts"] if item["fact_id"]=="goods")
    changed_goods["label"]="a different metric"
    saved_again=client.post(path+"/versions",headers=headers,json={"parent_version_id":calculated["version_id"],"content":changed})
    assert saved_again.status_code==201, saved_again.text
    edited=saved_again.json()["versions"][-1]
    assert not next(item for item in edited["content"]["facts"] if item["fact_id"]=="goods").get("direction_confirmation")
    assert edited["content"]["calculations"][0]["input_status"]=="unverifiable"
    assert client.post(path+"/calculations/recalculate",headers=headers,json={"parent_version_id":edited["version_id"],"calculation_index":0}).status_code==422
    forged=deepcopy(edited["content"])
    forged_goods=next(item for item in forged["facts"] if item["fact_id"]=="goods")
    from app.research_calculations import direction_confirmation_fingerprint
    forged_goods["direction_confirmation"]={"confirmed_by":"user","fact_sha256":direction_confirmation_fingerprint(forged_goods),"confirmed_at":"2026-01-01T00:00:00Z"}
    attempted=client.post(path+"/versions",headers=headers,json={"parent_version_id":edited["version_id"],"content":forged})
    assert attempted.status_code==201
    assert not next(item for item in attempted.json()["versions"][-1]["content"]["facts"] if item["fact_id"]=="goods").get("direction_confirmation")


def test_negative_change_candidate_can_be_attached_to_counterargument(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE","mock")
    source_id=""
    source_text="Services increased $33.3 billion. Goods had a decrease of $9.6 billion in spending."
    support="Services increased $33.3 billion"
    counter_quote="a decrease of $9.6 billion in spending"
    class Provider:
        name="negative-candidate-test"
        policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            return {"claims":[{"text":"Services increased $33.3 billion.","citations":[{"source_id":source_id,"quotation":support}]}],
                "counterarguments":[{"text":"Goods spending fell in nominal terms ($9.6 billion).","citations":[{"source_id":source_id,"quotation":support}]}],
                "facts":[{"fact_id":"goods","label":"Goods spending change","value":"-9.6","unit":"billion USD","currency":"USD","period":"2022-07",
                          "citation":{"source_id":source_id,"quotation":counter_quote}}]},20
    monkeypatch.setattr(api,"challenge_provider",Provider())
    client=TestClient(api.app)
    account=client.post("/v1/auth/local/register",json={"email":f"negative-candidate-{uuid4().hex}@example.test","password":"correct horse battery staple!"}).json()
    headers={"Authorization":"Bearer "+account["access_token"]}
    source_id=client.post("/v2/research/sources",headers=headers,json={"title":"Release","text":source_text,
        "permissions":{"store":True,"model_input":True,"display":True}}).json()["source_id"]
    topic=client.post("/v2/research/topics",headers=headers,json={"question":"Spending?","source_ids":[source_id]}).json()
    path="/v2/research/topics/"+topic["topic_id"]
    receipt=client.post(path+"/runs",headers={**headers,"Idempotency-Key":"negative-candidate"},json={
        "parent_version_id":topic["versions"][0]["version_id"]}).json()["operation"]
    for _ in range(100):
        state=client.get(path+"/runs/"+receipt["operation_id"],headers=headers).json()["operation"]["status"]
        if state in {"completed","failed"}:break
        time.sleep(.02)
    assert state=="completed"
    version=client.get(path,headers=headers).json()["versions"][-1];content=version["content"]
    counter=next(c for c in content["claims"] if c["role"]=="counterargument")
    assert counter["suggested_fact_evidence"][0]["quotation"]==counter_quote
    assert counter["suggested_fact_evidence"][0]["status"]=="requires_semantic_review"
    assert counter.get("uncited_dollar_amounts") and content["run"]["status"]=="partial"
    edited=deepcopy(content)
    next(c for c in edited["claims"] if c["role"]=="counterargument")["citations"].append({"source_id":source_id,"quotation":counter_quote})
    saved=client.post(path+"/versions",headers=headers,json={"parent_version_id":version["version_id"],"content":edited})
    assert saved.status_code==201,saved.text
    after=next(c for c in saved.json()["versions"][-1]["content"]["claims"] if c["role"]=="counterargument")
    assert not after.get("uncited_dollar_amounts") and not after.get("uncited_fact_values")
    assert after["authorship"]=="user" and after["support_status"]=="not_independently_verified"
    assert counter.get("uncited_dollar_amounts")


def test_pdf_line_hyphen_fact_requires_exact_source_selection_before_calculation(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE","mock")
    source_id=""
    class Provider:
        name="pdf-fact-caution"
        policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            return {"facts":[
              {"fact_id":"current","label":"Revenue","value":"100","unit":"USD million","currency":"USD","period":"2024",
               "citation":{"source_id":source_id,"quotation":"Revenue estimates were 100"}},
              {"fact_id":"prior","label":"Revenue","value":"80","unit":"USD million","currency":"USD","period":"2023",
               "citation":{"source_id":source_id,"quotation":"Prior revenue was 80"}},
              {"fact_id":"decline","label":"Change","value":"-5","unit":"percent","period":"2024",
               "citation":{"source_id":source_id,"quotation":"Loss estimates decreased 5 percent"}}],
              "calculations":[{"formula":"growth_percent","first_fact_id":"current","second_fact_id":"prior"}]},10
    monkeypatch.setattr(api,"challenge_provider",Provider())
    client=TestClient(api.app)
    account=client.post("/v1/auth/local/register",json={"email":f"pdf-fact-{uuid4().hex}@example.test","password":"correct horse battery staple!"}).json()
    headers={"Authorization":"Bearer "+account["access_token"]}
    source_id=client.post("/v2/research/sources",headers=headers,json={"title":"Report","text":"Revenue esti-\nmates were 100. Prior revenue was 80. Loss esti-\nmates decreased 5 percent.",
      "permissions":{"store":True,"display":True,"model_input":True}}).json()["source_id"]
    document=api.RESEARCH_SOURCES.get(source_id)
    api.RESEARCH_SOURCES.put(source_id,document.model_copy(update={"retrieval_method":"user_pdf"}))
    topic=client.post("/v2/research/topics",headers=headers,json={"question":"Compare revenue.","source_ids":[source_id]}).json()
    path="/v2/research/topics/"+topic["topic_id"]
    receipt=client.post(path+"/runs",headers={**headers,"Idempotency-Key":"pdf-fact"},json={"parent_version_id":topic["versions"][0]["version_id"]}).json()["operation"]
    for _ in range(100):
        status=client.get(path+"/runs/"+receipt["operation_id"],headers=headers).json()["operation"]["status"]
        if status in {"completed","failed"}:break
        time.sleep(.02)
    assert status=="completed"
    before=client.get(path,headers=headers).json()["versions"][-1]
    fact=next(f for f in before["content"]["facts"] if f["fact_id"]=="current")
    assert fact["quotation_match"]=="pdf_line_hyphen_restored" and not before["content"]["calculations"]
    decline=next(f for f in before["content"]["facts"] if f["fact_id"]=="decline")
    assert decline["direction_context_unverified"] and decline["quotation_match"]=="pdf_line_hyphen_restored"
    assert client.post(path+"/facts/confirm-direction",headers=headers,json={"parent_version_id":before["version_id"],"fact_id":"decline"}).status_code==422
    spec={"parent_version_id":before["version_id"],"formula":"growth_percent","first_fact_id":"current","second_fact_id":"prior"}
    assert client.post(path+"/calculations",headers=headers,json=spec).status_code==422
    edited=deepcopy(before["content"])
    selected=next(f for f in edited["facts"] if f["fact_id"]=="current")
    selected.pop("location");selected.pop("quotation_match")
    saved=client.post(path+"/versions",headers=headers,json={"parent_version_id":before["version_id"],"content":edited})
    assert saved.status_code==201,saved.text
    after=saved.json()["versions"][-1];selected=next(f for f in after["content"]["facts"] if f["fact_id"]=="current")
    assert selected["quotation_match"]=="exact" and selected["authorship"]=="user"
    spec["parent_version_id"]=after["version_id"]
    result=client.post(path+"/calculations",headers=headers,json=spec)
    assert result.status_code==201,result.text
    assert result.json()["versions"][-1]["content"]["calculations"][0]["value"]=="25.00"
    assert saved.json()["versions"][-2]["content"]["facts"][0]["quotation_match"]=="pdf_line_hyphen_restored"


def test_related_claim_candidate_can_fill_a_missing_counterargument_quote(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE","mock")
    source_id="";quote="Services increased $33.3 billion while goods decreased $9.6 billion."
    short="Services increased $33.3 billion"
    class Provider:
        name="related-claim-test"
        policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            return {"claims":[{"text":"Services rose while goods fell.","citations":[{"source_id":source_id,"quotation":quote}]}],
                    "counterarguments":[{"text":"Goods spending fell $9.6 billion.","citations":[{"source_id":source_id,"quotation":short}]}]},20
    monkeypatch.setattr(api,"challenge_provider",Provider())
    client=TestClient(api.app)
    account=client.post("/v1/auth/local/register",json={"email":f"claim-candidate-{uuid4().hex}@example.test","password":"correct horse battery staple!"}).json()
    headers={"Authorization":"Bearer "+account["access_token"]}
    source_id=client.post("/v2/research/sources",headers=headers,json={"title":"Report","text":quote,
        "permissions":{"store":True,"display":True,"model_input":True}}).json()["source_id"]
    topic=client.post("/v2/research/topics",headers=headers,json={"question":"Spending?","source_ids":[source_id]}).json()
    path="/v2/research/topics/"+topic["topic_id"]
    receipt=client.post(path+"/runs",headers={**headers,"Idempotency-Key":"related-claim"},json={
        "parent_version_id":topic["versions"][0]["version_id"]}).json()["operation"]
    for _ in range(100):
        status=client.get(path+"/runs/"+receipt["operation_id"],headers=headers).json()["operation"]["status"]
        if status in {"completed","failed"}:break
        time.sleep(.02)
    assert status=="completed"
    version=client.get(path,headers=headers).json()["versions"][-1];content=version["content"]
    counter=next(c for c in content["claims"] if c["role"]=="counterargument")
    assert counter["uncited_dollar_amounts"]==["$9.6 billion"]
    assert counter["suggested_related_quotations"][0]["quotation"]==quote
    assert content["run"]["status"]=="partial"
    edited=deepcopy(content);target=next(c for c in edited["claims"] if c["role"]=="counterargument")
    target["citations"].append({"source_id":source_id,"quotation":quote})
    saved=client.post(path+"/versions",headers=headers,json={"parent_version_id":version["version_id"],"content":edited})
    assert saved.status_code==201,saved.text
    target=next(c for c in saved.json()["versions"][-1]["content"]["claims"] if c["role"]=="counterargument")
    assert not target.get("uncited_dollar_amounts") and not target.get("suggested_related_quotations")
    assert target["authorship"]=="user" and target["support_status"]=="not_independently_verified"
    assert counter["uncited_dollar_amounts"]


def test_run_can_select_sources_without_changing_history_or_leaking_others(monkeypatch):
    import json
    from uuid import uuid4
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE","mock")
    observed=[]
    class Provider:
        name="selected-sources-test"
        policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            observed.append(json.loads(prompt))
            ref=observed[-1]["sources"][0]["source_id"]
            return {"claims":[{"text":"Selected document says revenue was 200.","citations":[{"source_id":ref,"quotation":"Revenue was 200."}]}]},10
    monkeypatch.setattr(api,"challenge_provider",Provider())
    client=TestClient(api.app)
    def auth():
        account=client.post("/v1/auth/local/register",json={"email":f"selected-{uuid4().hex}@example.test","password":"correct horse battery staple!"}).json()
        return {"Authorization":"Bearer "+account["access_token"]}
    headers,other=auth(),auth()
    def make(text,h=headers):
        return client.post("/v2/research/sources",headers=h,json={"title":"Report","text":text,
            "permissions":{"store":True,"display":True,"model_input":True,"export":True}}).json()["source_id"]
    first,second,foreign=make("Revenue was 100."),make("Revenue was 200."),make("Secret third source.",other)
    topic=client.post("/v2/research/topics",headers=headers,json={"question":"Revenue?","source_ids":[first,second]}).json()
    path="/v2/research/topics/"+topic["topic_id"]
    parent=topic["versions"][0]["version_id"]
    request={"parent_version_id":parent,"selected_source_ids":[second],"require_counterarguments":False,"max_search_calls":0}
    receipt=client.post(path+"/runs",headers={**headers,"Idempotency-Key":"select-one"},json=request)
    assert receipt.status_code==202,receipt.text
    op=receipt.json()["operation"]["operation_id"]
    for _ in range(100):
        status=client.get(path+"/runs/"+op,headers=headers).json()["operation"]["status"]
        if status in {"completed","failed"}:break
        time.sleep(.02)
    assert status=="completed" and len(observed)==1
    prompt=json.dumps(observed[0]);assert "Revenue was 200" in prompt and "Revenue was 100" not in prompt and "Secret third" not in prompt
    inputs=client.get(path+"/runs/"+op+"/inputs",headers=headers).json()
    assert [s["source_id"] for s in inputs["sources"]]==[second]
    updated=client.get(path,headers=headers).json()
    assert set(updated["versions"][-1]["source_ids"])=={first,second}
    assert set(updated["versions"][0]["source_ids"])=={first,second}
    replay=client.post(path+"/runs",headers={**headers,"Idempotency-Key":"select-one"},json=request)
    assert replay.status_code==202 and replay.headers["Idempotency-Replayed"]=="true" and len(observed)==1
    conflict=client.post(path+"/runs",headers={**headers,"Idempotency-Key":"select-one"},json={**request,"selected_source_ids":[first]})
    assert conflict.status_code==409
    new_parent=updated["versions"][-1]["version_id"]
    for selection in ([foreign],[first,first],[""]):
        rejected=client.post(path+"/runs",headers={**headers,"Idempotency-Key":uuid4().hex},json={"parent_version_id":new_parent,"selected_source_ids":selection})
        assert rejected.status_code==422
    none=client.post(path+"/runs",headers={**headers,"Idempotency-Key":"select-none"},json={"parent_version_id":new_parent,
        "selected_source_ids":[],"max_search_calls":0,"require_counterarguments":False})
    assert none.status_code==202
    op2=none.json()["operation"]["operation_id"]
    for _ in range(100):
        state=client.get(path+"/runs/"+op2,headers=headers).json()["operation"]["status"]
        if state in {"completed","failed"}:break
        time.sleep(.02)
    assert state=="completed" and len(observed)==1
    assert client.get(path+"/runs/"+op2+"/inputs",headers=headers).json()["sources"]==[]
    assert client.get(path,headers=headers).json()["versions"][-1]["content"]["run"]["status"]=="partial"


def test_related_percentage_candidate_can_replace_ambiguous_quotation(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE","mock")
    source_id="";table="Bank ratios, percent: actual 14.6, projected 15.0, minimum 12.6 percent."
    note="The projections are hypothetical."
    class Provider:
        name="related-percent-test"
        policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            return {"claims":[{"text":"Bank ratios are reported.","citations":[{"source_id":source_id,"quotation":table}]},
                    {"text":"The actual ratio was 14.6%, projected 15.0%, minimum 12.6%.",
                     "citations":[{"source_id":source_id,"quotation":note}]}]},20
    monkeypatch.setattr(api,"challenge_provider",Provider())
    client=TestClient(api.app)
    account=client.post("/v1/auth/local/register",json={"email":f"percent-candidate-{uuid4().hex}@example.test","password":"correct horse battery staple!"}).json()
    headers={"Authorization":"Bearer "+account["access_token"]}
    source_id=client.post("/v2/research/sources",headers=headers,json={"title":"Report","text":table+" "+note+" "+note,
        "permissions":{"store":True,"display":True,"model_input":True}}).json()["source_id"]
    topic=client.post("/v2/research/topics",headers=headers,json={"question":"Capital ratios?","source_ids":[source_id]}).json()
    path="/v2/research/topics/"+topic["topic_id"]
    receipt=client.post(path+"/runs",headers={**headers,"Idempotency-Key":"related-percent"},json={
        "parent_version_id":topic["versions"][0]["version_id"],"require_counterarguments":False}).json()["operation"]
    for _ in range(100):
        status=client.get(path+"/runs/"+receipt["operation_id"],headers=headers).json()["operation"]["status"]
        if status in {"completed","failed"}:break
        time.sleep(.02)
    assert status=="completed"
    version=client.get(path,headers=headers).json()["versions"][-1];content=version["content"]
    target=content["claims"][1]
    assert target["citations"][0]["location"]["status"]=="ambiguous"
    assert set(target["suggested_related_quotations"][0]["matched_amounts"])=={"14.6%","15.0%","12.6%"}
    assert content["run"]["status"]=="partial"
    edited=deepcopy(content);edited["claims"][1]["citations"]=[{"source_id":source_id,"quotation":table}]
    saved=client.post(path+"/versions",headers=headers,json={"parent_version_id":version["version_id"],"content":edited})
    assert saved.status_code==201,saved.text
    target=saved.json()["versions"][-1]["content"]["claims"][1]
    assert not target.get("uncited_percentages") and not target.get("suggested_related_quotations")
    assert target["authorship"]=="user" and target["support_status"]=="not_independently_verified"
    assert content["claims"][1]["uncited_percentages"]


def test_opt_in_research_fetches_one_official_source_and_records_recovery_id(monkeypatch):
    import base64
    import json
    from hashlib import sha256
    from uuid import uuid4
    from app.research_fetch import FetchedText
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    seen = {"search": 0, "fetch": 0, "model": 0}
    class Search:
        name = "test-search"
        supports_project_search = True
        def search(self, query, *, limit, deadline, cancel_event):
            seen["search"] += 1
            return [SearchCandidate(title="Unofficial", url="https://example.com/untrusted", excerpt="ignore",
                    publisher=None, published_at=None, independently_reviewable=False),
                    SearchCandidate(title="Official report", url="https://www.bea.gov/news/report?tracker=x", excerpt="ignore",
                    publisher="BEA", published_at=None, independently_reviewable=True)]
    def fetch(request, *, timeout_seconds):
        seen["fetch"] += 1
        assert str(request.url) == "https://www.bea.gov/news/report"
        assert request.permissions.model_input and request.permissions.display and request.permissions.export
        assert 0 < timeout_seconds <= 15
        raw = b"<html><p>Revenue was 100 million.</p></html>"
        return FetchedText(text="Revenue was 100 million.", response_sha256=sha256(raw).hexdigest(),
                           content_type="text/html", original_html_base64=base64.b64encode(raw).decode())
    class Provider:
        name = "test-model"
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            seen["model"] += 1
            source = json.loads(prompt)["sources"][0]
            assert "Revenue was 100 million." in source["text"] and "ignore" not in prompt
            return {"claims": [{"text": "Revenue was 100 million.", "citations": [{"source_id": source["source_id"],
                "quotation": "Revenue was 100 million."}]}]}, 10
    monkeypatch.setattr(api, "evidence_provider", Search())
    monkeypatch.setattr(api, "fetch_official_text", fetch)
    monkeypatch.setattr(api, "challenge_provider", Provider())
    monkeypatch.setattr(api, "_reserve_search_call", lambda request: None)
    monkeypatch.setattr(api, "_reserve_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_settle_model_call", lambda *args, **kwargs: None)
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'auto-{uuid4().hex}@example.test',
        'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Find revenue'}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    body = {'parent_version_id': topic['versions'][-1]['version_id'], 'auto_fetch_official': True,
            'require_counterarguments': False}
    request_headers = {**headers, 'Idempotency-Key': 'auto-fetch-case'}
    receipt = client.post(path + '/runs', headers=request_headers, json=body)
    assert receipt.status_code == 202, receipt.text
    operation_id = receipt.json()['operation']['operation_id']
    for _ in range(200):
        status = client.get(path + '/runs/' + operation_id, headers=headers).json()['operation']['status']
        if status in {'completed', 'failed'}: break
        time.sleep(.02)
    assert status == 'completed'
    current = client.get(path, headers=headers).json()['versions'][-1]
    snapshot = client.get(path + '/runs/' + operation_id + '/inputs', headers=headers).json()
    source_id = snapshot['planned_official_source_id']
    assert source_id in current['source_ids'] and current['content']['run']['official_fetch_calls'] == 1, (seen, current['content'])
    assert current['content']['claims'][0]['citations'][0]['source_id'] == source_id
    assert client.get('/v2/research/sources/' + source_id, headers=headers).status_code == 200
    original = client.get('/v2/research/sources/' + source_id + '/original', headers=headers)
    assert original.status_code == 200 and original.content == b"<html><p>Revenue was 100 million.</p></html>"
    exported = client.get(path + '/versions/' + current['version_id'] + '/export', headers=headers)
    assert exported.status_code == 200, exported.text
    assert exported.json()['version']['content']['claims'][0]['citations'][0]['source_id'] == source_id
    assert seen == {'search': 1, 'fetch': 1, 'model': 1}
    assert client.post(path + '/runs', headers=request_headers, json=body).headers['Idempotency-Replayed'] == 'true'
    assert seen == {'search': 1, 'fetch': 1, 'model': 1}


@pytest.mark.parametrize('extra,expected_status', [
    ({'as_of': '2024-01-01T00:00:00Z'}, 'historical_cutoff'),
    ({'selected_source_ids': []}, 'empty_selection'),
    ({'max_cost_usd': '0'}, 'cost_unknown'),
])
def test_auto_fetch_does_not_bypass_history_selection_or_cost(monkeypatch, extra, expected_status):
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Search:
        name = 'test-search'
        supports_project_search = True
        def search(self, query, *, limit, deadline, cancel_event):
            return [SearchCandidate(title='Official', url='https://www.bea.gov/news/report', excerpt='Unverified lead',
                publisher='BEA', published_at=date(2023, 1, 1), independently_reviewable=True)]
    monkeypatch.setattr(api, 'evidence_provider', Search())
    monkeypatch.setattr(api, '_reserve_search_call', lambda *args: None)
    def forbidden(*args, **kwargs):
        raise AssertionError('No source fetch or model call is authorized here')
    monkeypatch.setattr(api, 'fetch_official_text', forbidden)
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'autoguard-{uuid4().hex}@example.test',
        'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    topic_body = {'question': 'Research this'}
    if 'as_of' in extra: topic_body['as_of'] = extra['as_of']
    topic = client.post('/v2/research/topics', headers=headers, json=topic_body).json()
    path = '/v2/research/topics/' + topic['topic_id']
    body = {'parent_version_id': topic['versions'][-1]['version_id'], 'auto_fetch_official': True,
            **{key: value for key, value in extra.items() if key != 'as_of'}}
    receipt = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'auto-guard'}, json=body)
    assert receipt.status_code == 202, receipt.text
    operation_id = receipt.json()['operation']['operation_id']
    for _ in range(200):
        status = client.get(path + '/runs/' + operation_id, headers=headers).json()['operation']['status']
        if status in {'completed', 'failed'}: break
        time.sleep(.02)
    assert status == 'completed'
    result = client.get(path, headers=headers).json()['versions'][-1]['content']
    assert result['run']['official_fetch_calls'] == 0 and result['run']['official_fetch_status'] == expected_status
    assert not result['claims'] and not result['facts']


def test_fetched_source_remains_recoverable_if_model_process_fails(monkeypatch):
    from uuid import uuid4
    from app.research_fetch import FetchedText
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    calls = []
    class Search:
        name = 'test-search'
        supports_project_search = True
        def search(self, query, *, limit, deadline, cancel_event):
            return [SearchCandidate(title='Official', url='https://www.bea.gov/news/report', excerpt='Lead',
                publisher='BEA', published_at=date(2023, 1, 1), independently_reviewable=True)]
    def fetch(request, *, timeout_seconds):
        calls.append('fetch')
        return FetchedText('Source text', 'a' * 64, 'text/html')
    class BrokenModel:
        name = 'broken-model'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            raise RuntimeError('simulated provider process failure')
    monkeypatch.setattr(api, 'evidence_provider', Search())
    monkeypatch.setattr(api, 'fetch_official_text', fetch)
    monkeypatch.setattr(api, 'challenge_provider', BrokenModel())
    monkeypatch.setattr(api, '_reserve_search_call', lambda *args: None)
    monkeypatch.setattr(api, '_reserve_model_call', lambda *args, **kwargs: None)
    monkeypatch.setattr(api, '_settle_model_call', lambda *args, **kwargs: None)
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'auto-recover-{uuid4().hex}@example.test',
        'password': 'correct horse battery staple!'}).json()
    other = client.post('/v1/auth/local/register', json={'email': f'auto-other-{uuid4().hex}@example.test',
        'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Find evidence'}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    body = {'parent_version_id': topic['versions'][-1]['version_id'], 'auto_fetch_official': True}
    req_headers = {**headers, 'Idempotency-Key': 'recover-saved-page'}
    receipt = client.post(path + '/runs', headers=req_headers, json=body)
    operation_id = receipt.json()['operation']['operation_id']
    for _ in range(200):
        status = client.get(path + '/runs/' + operation_id, headers=headers).json()['operation']['status']
        if status in {'completed', 'failed'}: break
        time.sleep(.02)
    assert status == 'failed'
    snapshot = client.get(path + '/runs/' + operation_id + '/inputs', headers=headers).json()
    source_id = snapshot['planned_official_source_id']
    assert client.get('/v2/research/sources/' + source_id, headers=headers).status_code == 200
    assert client.get('/v2/research/sources/' + source_id, headers={'Authorization': 'Bearer ' + other['access_token']}).status_code == 404
    recovered = client.post(path + '/sources', headers=headers, json={'parent_version_id': body['parent_version_id'],
        'source_ids': [source_id]})
    assert recovered.status_code == 201 and source_id in recovered.json()['versions'][-1]['source_ids']
    replay = client.post(path + '/runs', headers=req_headers, json=body)
    assert replay.headers['Idempotency-Replayed'] == 'true' and calls == ['fetch']


def test_failed_official_fetch_keeps_a_lead_without_fabricating_evidence(monkeypatch):
    from uuid import uuid4
    from app.research_fetch import FetchFailure
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    calls = []
    class Search:
        name = 'test-search'
        supports_project_search = True
        def search(self, query, *, limit, deadline, cancel_event):
            return [SearchCandidate(title='Official', url='https://www.bea.gov/news/report', excerpt='Untrusted snippet',
                publisher=None, published_at=None, independently_reviewable=True)]
    def failed_fetch(request, *, timeout_seconds):
        calls.append('fetch')
        raise FetchFailure('source_timeout')
    monkeypatch.setattr(api, 'evidence_provider', Search())
    monkeypatch.setattr(api, 'fetch_official_text', failed_fetch)
    monkeypatch.setattr(api, '_reserve_search_call', lambda *args: None)
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'autofail-{uuid4().hex}@example.test',
        'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Find evidence'}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    receipt = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'auto-fail'}, json={
        'parent_version_id': topic['versions'][-1]['version_id'], 'auto_fetch_official': True})
    assert receipt.status_code == 202
    operation_id = receipt.json()['operation']['operation_id']
    for _ in range(200):
        status = client.get(path + '/runs/' + operation_id, headers=headers).json()['operation']['status']
        if status in {'completed', 'failed'}: break
        time.sleep(.02)
    assert status == 'completed' and calls == ['fetch']
    content = client.get(path, headers=headers).json()['versions'][-1]['content']
    assert content['run']['status'] == 'partial' and content['run']['official_fetch_status'] == 'failed'
    assert content['run']['official_fetch_calls'] == 1
    assert content['source_candidates'][0]['status'] == 'unverified'
    assert not content['claims'] and not content['facts'] and 'Untrusted snippet' not in str(content)
    snapshot = client.get(path + '/runs/' + operation_id + '/inputs', headers=headers).json()
    assert client.get('/v2/research/sources/' + snapshot['planned_official_source_id'], headers=headers).status_code == 404


def test_uncited_exact_date_stays_partial_and_is_rechecked_on_edit(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    source_id = ''
    class Provider:
        name = 'date-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            return {'claims': [{'text': 'On January 25, 2024 revenue was 100.', 'citations': [
                {'source_id': source_id, 'quotation': 'Revenue was 100.'}]}]}, 10
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    monkeypatch.setattr(api, '_reserve_model_call', lambda *a, **kw: None)
    monkeypatch.setattr(api, '_settle_model_call', lambda *a, **kw: None)
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'date-{uuid4().hex}@example.test',
        'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source = client.post('/v2/research/sources', headers=headers, json={'title': 'Release',
        'text': 'Published January 25, 2024. Revenue was 100.',
        'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True}}).json()
    source_id = source['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?', 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    receipt = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'date-test'}, json={
        'parent_version_id': topic['versions'][-1]['version_id'], 'require_counterarguments': False})
    operation_id = receipt.json()['operation']['operation_id']
    for _ in range(200):
        status = client.get(path + '/runs/' + operation_id, headers=headers).json()['operation']['status']
        if status in {'completed', 'failed'}: break
        time.sleep(.02)
    assert status == 'completed'
    current = client.get(path, headers=headers).json()['versions'][-1]
    assert current['content']['run']['status'] == 'partial'
    assert current['content']['claims'][0]['uncited_exact_dates'] == ['January 25, 2024']
    edited = deepcopy(current['content'])
    edited['claims'][0]['text'] += ' Reported.'
    edited['claims'][0]['uncited_exact_dates'] = []
    update = client.post(path + '/versions', headers=headers, json={'parent_version_id': current['version_id'], 'content': edited})
    assert update.status_code == 201, update.text
    next_version = update.json()['versions'][-1]
    assert next_version['content']['claims'][0]['uncited_exact_dates'] == ['January 25, 2024']
    exported = client.get(path + '/versions/' + next_version['version_id'] + '/export', headers=headers)
    assert exported.status_code == 200 and exported.json()['version']['content']['claims'][0]['uncited_exact_dates']


def test_opt_in_discovery_retrieves_pdf_as_owned_recoverable_source(monkeypatch):
    import base64, json
    from hashlib import sha256
    from uuid import uuid4
    from app.research_fetch import FetchedPdf
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    raw = b'%PDF-1.4 fixture'
    class Search:
        name = 'test-search'
        supports_project_search = True
        def search(self, query, *, limit, deadline, cancel_event):
            return [SearchCandidate(title='Official PDF', url='https://www.bea.gov/docs/report.PDF', excerpt='lead',
                publisher=None, published_at=None, independently_reviewable=True)]
    calls = []
    def fetch(request, *, timeout_seconds):
        calls.append(timeout_seconds)
        return FetchedPdf('Revenue was 100.', sha256(raw).hexdigest(), 'application/pdf', base64.b64encode(raw).decode())
    class Provider:
        name = 'test-model'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            source = json.loads(prompt)['sources'][0]
            return {'claims': [{'text': 'Revenue was 100.', 'citations': [{'source_id': source['source_id'],
                'quotation': 'Revenue was 100.'}]}]}, 10
    monkeypatch.setattr(api, 'evidence_provider', Search())
    monkeypatch.setattr(api, 'fetch_official_pdf', fetch)
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    monkeypatch.setattr(api, '_reserve_search_call', lambda *a: None)
    monkeypatch.setattr(api, '_reserve_model_call', lambda *a, **k: None)
    monkeypatch.setattr(api, '_settle_model_call', lambda *a, **k: None)
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'pdf-auto-{uuid4().hex}@example.test',
        'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?'}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    receipt = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'official-pdf'}, json={
        'parent_version_id': topic['versions'][-1]['version_id'], 'auto_fetch_official': True, 'require_counterarguments': False})
    assert receipt.status_code == 202
    operation_id = receipt.json()['operation']['operation_id']
    for _ in range(200):
        status = client.get(path + '/runs/' + operation_id, headers=headers).json()['operation']['status']
        if status in {'completed', 'failed'}: break
        time.sleep(.02)
    assert status == 'completed' and len(calls) == 1
    version = client.get(path, headers=headers).json()['versions'][-1]
    source_id = version['source_ids'][0]
    metadata = client.get('/v2/research/sources/' + source_id, headers=headers).json()
    assert metadata['kind'] == 'official' and metadata['retrieval_method'] == 'user_pdf' and metadata['pdf_text_order'] == 'layout'
    assert metadata['response_sha256'] == sha256(raw).hexdigest() and metadata['original_pdf_available']
    assert client.get('/v2/research/sources/' + source_id + '/original', headers=headers).content == raw
    assert version['content']['claims'][0]['citations'][0]['source_id'] == source_id


@pytest.mark.parametrize('configured', [False, True])
def test_auto_sec_filing_uses_configured_contact_or_falls_back(monkeypatch, configured):
    import base64, json
    from hashlib import sha256
    from uuid import uuid4
    from app.research_fetch import FetchedText
    from app.research_sec import SecFilingResult
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.delenv('SEC_USER_AGENT', raising=False)
    if configured: monkeypatch.setenv('FORESIGHTNINE_SEC_USER_AGENT', 'Fixture test@example.test')
    else: monkeypatch.delenv('FORESIGHTNINE_SEC_USER_AGENT', raising=False)
    sec_url = 'https://www.sec.gov/Archives/edgar/data/858877/000085887724000017/csco-20240727.htm'
    class Search:
        name = 'test-search'
        supports_project_search = True
        def search(self, query, *, limit, deadline, cancel_event):
            return [SearchCandidate(title='SEC filing', url=sec_url, excerpt='lead', publisher=None,
                    published_at=None, independently_reviewable=True),
                    SearchCandidate(title='BEA release', url='https://www.bea.gov/news/report', excerpt='lead',
                    publisher=None, published_at=None, independently_reviewable=True)]
    raw=b'<html><p>Revenue was 100.</p></html>'; calls=[]
    def sec_fetch(request, agent, *, timeout_seconds):
        calls.append('sec')
        assert configured and agent == 'Fixture test@example.test' and 0 < timeout_seconds <= 15
        return SecFilingResult('Revenue was 100.', sha256(raw).hexdigest(), base64.b64encode(raw).decode())
    def bea_fetch(request, *, timeout_seconds):
        calls.append('bea')
        assert not configured and 0 < timeout_seconds <= 15
        return FetchedText('Revenue was 100.', sha256(raw).hexdigest(), 'text/html', base64.b64encode(raw).decode())
    class Provider:
        name='test-model';policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            source=json.loads(prompt)['sources'][0]
            return {'claims':[{'text':'Revenue was 100.','citations':[{'source_id':source['source_id'],
                    'quotation':'Revenue was 100.'}]}]},10
    monkeypatch.setattr(api, 'evidence_provider', Search())
    monkeypatch.setattr(api, 'fetch_sec_filing', sec_fetch)
    monkeypatch.setattr(api, 'fetch_official_text', bea_fetch)
    monkeypatch.setattr(api, 'reserve_sec_request', lambda *args: True)
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    monkeypatch.setattr(api, '_reserve_search_call', lambda *args: None)
    monkeypatch.setattr(api, '_reserve_model_call', lambda *args, **kwargs: None)
    monkeypatch.setattr(api, '_settle_model_call', lambda *args, **kwargs: None)
    client=TestClient(api.app)
    auth=client.post('/v1/auth/local/register',json={'email':f'sec-auto-{uuid4().hex}@example.test',
        'password':'correct horse battery staple!'}).json();headers={'Authorization':'Bearer '+auth['access_token']}
    topic=client.post('/v2/research/topics',headers=headers,json={'question':'Revenue?'}).json()
    path='/v2/research/topics/'+topic['topic_id']
    receipt=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'sec-auto'},json={
        'parent_version_id':topic['versions'][-1]['version_id'],'auto_fetch_official':True,'require_counterarguments':False})
    assert receipt.status_code==202
    operation_id=receipt.json()['operation']['operation_id']
    for _ in range(200):
        status=client.get(path+'/runs/'+operation_id,headers=headers).json()['operation']['status']
        if status in {'completed','failed'}:break
        time.sleep(.02)
    assert status=='completed' and calls==(['sec'] if configured else ['bea'])
    version=client.get(path,headers=headers).json()['versions'][-1]
    source_id=version['source_ids'][0]
    source=client.get('/v2/research/sources/'+source_id,headers=headers).json()
    assert source['retrieval_method']==('sec_filing_html' if configured else 'official_html')
    assert client.get('/v2/research/sources/'+source_id+'/original',headers=headers).content==raw
    assert version['content']['run']['official_fetch_calls']==1
    assert any('SEC access is not configured' in note for note in version['content']['limitations']) is (not configured)
    assert 'test@example.test' not in json.dumps(version)


def test_auto_sec_filing_respects_shared_rate_limit(monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setenv('FORESIGHTNINE_SEC_USER_AGENT', 'Fixture test@example.test')
    class Search:
        name='test-search';supports_project_search=True
        def search(self,query,*,limit,deadline,cancel_event):
            return [SearchCandidate(title='SEC filing',url='https://www.sec.gov/Archives/edgar/data/858877/000085887724000017/csco-20240727.htm',
                excerpt='lead',publisher=None,published_at=None,independently_reviewable=True)]
    monkeypatch.setattr(api,'evidence_provider',Search())
    monkeypatch.setattr(api,'_reserve_search_call',lambda *args:None)
    monkeypatch.setattr(api,'reserve_sec_request',lambda *args:False)
    def forbidden(*args,**kwargs):raise AssertionError('rate-limited SEC request must not be made')
    monkeypatch.setattr(api,'fetch_sec_filing',forbidden)
    client=TestClient(api.app)
    auth=client.post('/v1/auth/local/register',json={'email':f'sec-limit-{uuid4().hex}@example.test',
        'password':'correct horse battery staple!'}).json();headers={'Authorization':'Bearer '+auth['access_token']}
    topic=client.post('/v2/research/topics',headers=headers,json={'question':'Revenue?'}).json()
    path='/v2/research/topics/'+topic['topic_id']
    receipt=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'sec-rate-limit'},json={
        'parent_version_id':topic['versions'][-1]['version_id'],'auto_fetch_official':True})
    operation_id=receipt.json()['operation']['operation_id']
    for _ in range(200):
        status=client.get(path+'/runs/'+operation_id,headers=headers).json()['operation']['status']
        if status in {'completed','failed'}:break
        time.sleep(.02)
    assert status=='completed'
    result=client.get(path,headers=headers).json()['versions'][-1]['content']
    assert result['run']['official_fetch_status']=='failed' and result['run']['official_fetch_calls']==0
    assert not result['claims'] and not result['facts']


@pytest.mark.parametrize('mode', ['followup_and_review', 'call_budget', 'followup_failure', 'unlocated', 'extra_fields', 'warning', 'extra_citation_leads'])
def test_counterpoint_followup_preserves_generation_and_respects_budgets(monkeypatch, mode):
    import json
    from uuid import uuid4
    from app.phase3 import ProviderFailure

    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    source_id = ''
    prompts = []
    class Provider:
        name = 'counterpoint-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=3000, max_output_tokens=1500)
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            payload = json.loads(prompt)
            prompts.append((system, payload))
            if system == REVIEW_SYSTEM_PROMPT:
                return {'reviews':[{'claim_index':i,'verdict':'supports'} for i in range(len(payload['claims']))]}, 80
            if 'counterpoint_request' in payload:
                if mode == 'followup_failure':
                    raise ProviderFailure('provider_unavailable','Follow-up failed.',retryable=False)
                assert payload['sources'] == prompts[0][1]['sources']
                assert payload['question'] == prompts[0][1]['question']
                if mode == 'unlocated':
                    return {'counterarguments':[{'text':'Invented.', 'citations':[
                        {'source_id':source_id,'quotation':'Invented source text.'}]}]}, 70
                if mode == 'extra_fields':
                    return {'counterarguments':[{'text':'Costs increased.','citations':[
                        {'source_id':source_id,'quotation':'Costs increased.'}]}],
                        'answer':{'secret':'untrusted extra'}, 'claims':[{'text':'Discard me'}]}, 70
                if mode == 'warning':
                    return {'counterarguments':[{'text':'Costs increased because of a supplier.', 'citations':[
                        {'source_id':source_id,'quotation':'Costs increased.'}]}]}, 70
                if mode == 'extra_citation_leads':
                    return {'counterarguments':[{'text':'Costs increased.', 'citations':[
                        {'source_id':source_id,'quotation':'Costs increased.'}]}],
                        'answer':{'inflation':{'text':'Untrusted extra assertion.', 'citations':[
                            {'source_id':source_id,'quotation':'Inflation was below 2 percent.'}]},
                            'secret':{'text':'Must not persist', 'citations':[
                                {'source_id':source_id,'quotation':'invented 2 percent.'}]}}}, 70
                return {'counterarguments':[{'text':'Costs also increased.', 'citations':[
                    {'source_id':source_id,'quotation':'Costs increased.'}]}]}, 70
            text = 'Revenue increased and inflation was below 2 percent.' if mode == 'extra_citation_leads' else 'Revenue increased.'
            return {'claims':[{'text':text, 'citations':[
                {'source_id':source_id,'quotation':'Revenue increased.'}]}]}, 50
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    monkeypatch.setattr(api, '_reserve_model_call', lambda *args, **kwargs: None)
    monkeypatch.setattr(api, '_settle_model_call', lambda *args, **kwargs: None)
    client = TestClient(api.app)
    reg = client.post('/v1/auth/local/register', json={'email':f'counter-{uuid4().hex}@example.test',
        'password':'correct horse battery staple!'}).json()
    headers = {'Authorization':'Bearer '+reg['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title':'Report',
        'text':'Revenue increased. Costs increased. Inflation was below 2 percent.', 'permissions':{'store':True,'display':True,'model_input':True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question':'Assess revenue.', 'source_ids':[source_id]}).json()
    path = '/v2/research/topics/'+topic['topic_id']
    budget = 1 if mode == 'call_budget' else 3
    body = {'parent_version_id':topic['versions'][0]['version_id'], 'counterpoint_followup':True,
            'max_model_calls':budget, 'max_total_tokens':10000, 'support_review':mode == 'followup_and_review'}
    response = client.post(path+'/runs', headers={**headers,'Idempotency-Key':'counterpoint-run'}, json=body)
    assert response.status_code == 202, response.text
    op = response.json()['operation']['operation_id']
    for _ in range(100):
        state = client.get(path+'/runs/'+op, headers=headers).json()['operation']['status']
        if state in ('completed','failed','cancelled'): break
        time.sleep(.02)
    assert state == 'completed'
    versions = client.get(path,headers=headers).json()['versions']
    content = versions[-1]['content']; run = content['run']
    assert content['claims'][0]['text'].startswith('Revenue increased')
    if mode == 'followup_and_review':
        assert len(prompts)==3 and run['counterpoint_status']=='completed'
        assert run['model_calls']==3 and run['generation_tokens']==50 and run['counterpoint_tokens']==70
        assert run['support_review_tokens']==80 and run['total_tokens']==200
        assert run['support_review_status']=='complete'
        assert any(x['role']=='counterargument' and x['text']=='Costs also increased.' for x in content['claims'])
        assert any(v['content']['run'].get('checkpoint')=='counterpoint' for v in versions)
        assert any(v['content']['run'].get('checkpoint')=='generation' for v in versions)
    elif mode == 'call_budget':
        assert len(prompts)==1 and run['counterpoint_status']=='budget_exhausted' and run['total_tokens']==50
    elif mode == 'followup_failure':
        assert len(prompts)==2 and run['counterpoint_status']=='failed' and run['total_tokens'] is None
        assert any(v['content']['run'].get('checkpoint')=='counterpoint' for v in versions)
    elif mode == 'unlocated':
        assert len(prompts)==2 and run['counterpoint_status']=='no_evidence' and run['total_tokens']==120
        assert all(x.get('role')!='counterargument' for x in content['claims'])
    elif mode == 'extra_fields':
        assert len(prompts)==2 and run['counterpoint_status']=='completed' and run['counterpoint_discarded_fields']==2
        assert 'Discard me' not in str(content) and 'untrusted extra' not in str(content)
        assert any(x.get('role')=='counterargument' and x['text']=='Costs increased.' for x in content['claims'])
        assert any('Extra counterpoint response fields' in x for x in content['limitations'])
    elif mode == 'warning':
        assert run['counterpoint_status']=='needs_review' and run['status']=='partial'
        assert any(x.get('role')=='counterargument' and x.get('causal_attribution_unverified') for x in content['claims'])
    else:
        claim=content['claims'][0]
        assert claim['uncited_percentages'] == ['2 percent']
        assert claim['suggested_related_quotations'][0]['quotation'] == 'Inflation was below 2 percent.'
        assert claim['suggested_related_quotations'][0]['from_claim_index'] == -1
        assert len(claim['citations']) == 1 and run['status'] == 'partial'
        assert 'Untrusted extra assertion' not in str(content) and 'Must not persist' not in str(content)
    replay = client.post(path+'/runs',headers={**headers,'Idempotency-Key':'counterpoint-run'},json=body)
    assert replay.status_code==202 and replay.json()['operation']['operation_id']==op


def test_counterpoint_cancellation_preserves_generation_checkpoint(monkeypatch):
    import json
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE','mock')
    entered,release=Event(),Event()
    source_id=''
    class Provider:
        name='counterpoint-cancel'
        policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            if 'counterpoint_request' in json.loads(prompt):
                entered.set(); assert release.wait(5)
                return {'counterarguments':[{'text':'Costs increased.', 'citations':[{'source_id':source_id,'quotation':'Costs increased.'}]}]}, 10
            return {'claims':[{'text':'Revenue increased.','citations':[{'source_id':source_id,'quotation':'Revenue increased.'}]}]}, 20
    monkeypatch.setattr(api,'challenge_provider',Provider())
    client=TestClient(api.app)
    reg=client.post('/v1/auth/local/register',json={'email':f'cancel-counter-{uuid4().hex}@example.test','password':'correct horse battery staple!'}).json()
    headers={'Authorization':'Bearer '+reg['access_token']}
    source_id=client.post('/v2/research/sources',headers=headers,json={'title':'Report','text':'Revenue increased. Costs increased.',
        'permissions':{'store':True,'display':True,'model_input':True}}).json()['source_id']
    topic=client.post('/v2/research/topics',headers=headers,json={'question':'Assess revenue.','source_ids':[source_id]}).json()
    path='/v2/research/topics/'+topic['topic_id']
    receipt=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'cancel-counterpoint'},json={
        'parent_version_id':topic['versions'][0]['version_id'],'counterpoint_followup':True,'max_model_calls':2,
        'max_total_tokens':10000}).json()['operation']
    try:
        assert entered.wait(5)
        before=client.get(path,headers=headers).json()['versions']
        assert before[-1]['content']['run']['checkpoint']=='counterpoint'
        assert before[-1]['content']['claims'][0]['text']=='Revenue increased.'
        stopped=client.post('/v1/operations/'+receipt['operation_id']+'/cancel',
            headers={**headers,'Idempotency-Key':'cancel-counterpoint-call'},json={'cancel_token':receipt['cancel_token']})
        assert stopped.status_code==200 and stopped.json()['operation']['status']=='cancelled'
        release.set()
        for _ in range(100):
            if not api.operations._pending_provider_executions.get(receipt['operation_id']):break
            time.sleep(.02)
        after=client.get(path,headers=headers).json()['versions']
        assert len(after)==len(before) and after[-1]['content']['run']['counterpoint_status']=='pending'
    finally:
        release.set()


def test_counterpoint_followup_never_overwrites_concurrent_edit(monkeypatch):
    import json
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE','mock')
    entered,release=Event(),Event()
    source_id=''
    class Provider:
        name='counterpoint-edit'
        policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            if 'counterpoint_request' in json.loads(prompt):
                entered.set(); assert release.wait(5)
                return {'counterarguments':[{'text':'Costs increased.','citations':[{'source_id':source_id,'quotation':'Costs increased.'}]}]},10
            return {'claims':[{'text':'Revenue increased.','citations':[{'source_id':source_id,'quotation':'Revenue increased.'}]}]},20
    monkeypatch.setattr(api,'challenge_provider',Provider())
    client=TestClient(api.app)
    reg=client.post('/v1/auth/local/register',json={'email':f'edit-counter-{uuid4().hex}@example.test','password':'correct horse battery staple!'}).json()
    headers={'Authorization':'Bearer '+reg['access_token']}
    source_id=client.post('/v2/research/sources',headers=headers,json={'title':'Report','text':'Revenue increased. Costs increased.',
        'permissions':{'store':True,'display':True,'model_input':True}}).json()['source_id']
    topic=client.post('/v2/research/topics',headers=headers,json={'question':'Assess revenue.','source_ids':[source_id]}).json()
    path='/v2/research/topics/'+topic['topic_id']
    receipt=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'edit-counterpoint'},json={
        'parent_version_id':topic['versions'][0]['version_id'],'counterpoint_followup':True,'max_model_calls':2,
        'max_total_tokens':10000}).json()['operation']
    try:
        assert entered.wait(5)
        checkpoint=client.get(path,headers=headers).json()['versions'][-1]
        assert checkpoint['content']['run']['checkpoint']=='counterpoint'
        edited=deepcopy(checkpoint['content']);edited['summary']='User revision'
        saved=client.post(path+'/versions',headers=headers,json={'parent_version_id':checkpoint['version_id'],'content':edited})
        assert saved.status_code==201
        release.set()
        for _ in range(100):
            status=client.get(path+'/runs/'+receipt['operation_id'],headers=headers).json()['operation']['status']
            if status in {'failed','completed'}:break
            time.sleep(.02)
        assert status=='failed'
        latest=client.get(path,headers=headers).json()['versions'][-1]['content']
        assert latest['summary']=='User revision' and not any(x.get('role')=='counterargument' for x in latest['claims'])
    finally:
        release.set()


@pytest.mark.parametrize('warned', [True, False])
def test_counterpoint_followup_respects_existing_evidence_warnings(monkeypatch, warned):
    import json
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE','mock')
    source_id=''; calls=[]
    class Provider:
        name='counterpoint-warning-test'
        policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            payload=json.loads(prompt);calls.append(payload)
            cite=lambda q:{'source_id':source_id,'quotation':q}
            if 'counterpoint_request' in payload:
                return {'counterarguments':[{'text':'Costs increased.','citations':[cite('Costs increased.')]}]}, 15
            text='Costs increased because of a supplier.' if warned else 'Costs increased.'
            return {'claims':[{'text':'Revenue increased.','citations':[cite('Revenue increased.')]}],
                    'counterarguments':[{'text':text,'citations':[cite('Costs increased.')]}]}, 20
    monkeypatch.setattr(api,'challenge_provider',Provider())
    client=TestClient(api.app)
    reg=client.post('/v1/auth/local/register',json={'email':f'warn-counter-{uuid4().hex}@example.test','password':'correct horse battery staple!'}).json()
    headers={'Authorization':'Bearer '+reg['access_token']}
    source_id=client.post('/v2/research/sources',headers=headers,json={'title':'Report','text':'Revenue increased. Costs increased.',
        'permissions':{'store':True,'display':True,'model_input':True}}).json()['source_id']
    topic=client.post('/v2/research/topics',headers=headers,json={'question':'Assess revenue.','source_ids':[source_id]}).json()
    path='/v2/research/topics/'+topic['topic_id']
    receipt=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'warn-counterpoint'},json={
        'parent_version_id':topic['versions'][0]['version_id'],'counterpoint_followup':True,'max_model_calls':2,
        'max_total_tokens':10000}).json()['operation']
    for _ in range(100):
        state=client.get(path+'/runs/'+receipt['operation_id'],headers=headers).json()['operation']['status']
        if state in ('completed','failed','cancelled'):break
        time.sleep(.02)
    assert state=='completed'
    content=client.get(path,headers=headers).json()['versions'][-1]['content']
    assert content['run']['counterpoint_status']==('completed' if warned else 'not_needed')
    assert len(calls)==(2 if warned else 1)
    counterpoints=[item for item in content['claims'] if item['role']=='counterargument']
    if warned:
        assert len(counterpoints)==2 and counterpoints[0]['causal_attribution_unverified']
        assert not counterpoints[1].get('causal_attribution_unverified')
        assert content['run']['status']=='partial'
    else:
        assert len(counterpoints)==1 and not counterpoints[0].get('causal_attribution_unverified')


@pytest.mark.parametrize('existing,expected_calls,expected_status', [(20,1,'capacity_reached'),(19,2,'partial')])
def test_counterpoint_followup_respects_counterargument_capacity(monkeypatch,existing,expected_calls,expected_status):
    import json
    from uuid import uuid4
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE','mock')
    source_id='';calls=[]
    class Provider:
        name='counterpoint-capacity-test'
        policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=3000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            data=json.loads(prompt); calls.append(data)
            cite=lambda q:{'source_id':source_id,'quotation':q}
            if 'counterpoint_request' in data:
                return {'counterarguments':[{'text':'Costs increased.','citations':[cite('Costs increased.')]} for _ in range(2)]},15
            return {'claims':[{'text':'Revenue increased.','citations':[cite('Revenue increased.')]}],
                    'counterarguments':[{'text':'Costs increased because of a supplier.','citations':[cite('Costs increased.')]} for _ in range(existing)]},20
    monkeypatch.setattr(api,'challenge_provider',Provider())
    client=TestClient(api.app)
    reg=client.post('/v1/auth/local/register',json={'email':f'capacity-counter-{uuid4().hex}@example.test','password':'correct horse battery staple!'}).json()
    headers={'Authorization':'Bearer '+reg['access_token']}
    source_id=client.post('/v2/research/sources',headers=headers,json={'title':'Report','text':'Revenue increased. Costs increased.',
        'permissions':{'store':True,'display':True,'model_input':True}}).json()['source_id']
    topic=client.post('/v2/research/topics',headers=headers,json={'question':'Assess revenue.','source_ids':[source_id]}).json()
    path='/v2/research/topics/'+topic['topic_id']
    receipt=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'capacity-counterpoint'},json={
        'parent_version_id':topic['versions'][0]['version_id'],'counterpoint_followup':True,'max_model_calls':2,'max_total_tokens':10000}).json()['operation']
    for _ in range(100):
        state=client.get(path+'/runs/'+receipt['operation_id'],headers=headers).json()['operation']['status']
        if state in ('completed','failed','cancelled'):break
        time.sleep(.02)
    assert state=='completed'
    content=client.get(path,headers=headers).json()['versions'][-1]['content']
    assert len(calls)==expected_calls and content['run']['counterpoint_status']==expected_status
    assert len([x for x in content['claims'] if x['role']=='counterargument'])==20
    if expected_status=='partial':
        assert any('result limit was reached' in item for item in content['limitations'])
        assert content['run']['status']=='partial'


def test_usable_counterpoint_excludes_separate_numeric_discrepancies():
    from app.research_runs import CLAIM_EVIDENCE_WARNINGS, has_usable_counterpoint
    item={'role':'counterargument','citations':[{'source_id':'s'}], 'support_status':'not_independently_verified'}
    assert has_usable_counterpoint([item])
    assert not has_usable_counterpoint([item],{0})
    for warning in CLAIM_EVIDENCE_WARNINGS:
        assert not has_usable_counterpoint([{**item,warning:True}])
    assert not has_usable_counterpoint([{**item,'support_status':'incomplete'}])
