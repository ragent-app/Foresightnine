"""Public acquisition contract: local providers only; no network."""
from types import SimpleNamespace

import pytest
import app.main as api
from app.research_acquisition import AcquisitionResult
from tests.test_research_recovery_acceptance import _setup, _terminal


@pytest.mark.parametrize('case', ['zero_budget', 'decision_failure'])
def test_no_analysis_yields_partial_without_external_calls(monkeypatch, case):
    client, headers, _, path, parent = _setup(monkeypatch)
    calls = []
    class Provider:
        name = 'no-network-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=6000, max_output_tokens=1500)
        def generate_structured(self, *args, **kwargs):
            calls.append('model')
            raise AssertionError('unexpected model call')
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    monkeypatch.setattr(api, 'evidence_provider', SimpleNamespace(supports_project_search=False, name='no-network-test'))
    if case == 'decision_failure':
        monkeypatch.setattr(api, 'execute_decision', lambda **kwargs: AcquisitionResult('failed'))
    payload = {'parent_version_id': parent, 'max_model_calls': 0 if case == 'zero_budget' else 1,
               'max_search_calls': 0, 'plan_research': False, 'gap_followup': False,
               'support_review': False, 'discover_sources': False, 'auto_fetch_official': False,
               'require_counterarguments': False}
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'acquisition-' + case}, json=payload)
    assert response.status_code == 202, response.text
    operation_id = response.json()['operation']['operation_id']
    assert _terminal(client, path, headers, operation_id) == 'completed'
    content = client.get(path, headers=headers).json()['versions'][-1]['content']
    assert content['run']['status'] == 'partial'
    assert not calls


def test_idempotency_preserves_explicit_empty_and_disabled(monkeypatch):
    client, headers, _, path, parent = _setup(monkeypatch)
    payload = {'parent_version_id': parent, 'max_model_calls': 0, 'max_search_calls': 0,
               'selected_source_ids': [], 'discover_sources': False, 'auto_fetch_official': False}
    key = {**headers, 'Idempotency-Key': 'acquisition-explicit-empty'}
    first = client.post(path + '/runs', headers=key, json=payload)
    assert first.status_code == 202, first.text
    assert client.post(path + '/runs', headers=key, json=payload).json()['operation']['operation_id'] == first.json()['operation']['operation_id']
    changed = client.post(path + '/runs', headers=key, json={**payload, 'selected_source_ids': None})
    assert changed.status_code == 409
    operation_id = first.json()['operation']['operation_id']
    assert _terminal(client, path, headers, operation_id) == 'completed'
    inputs = client.get(f'{path}/runs/{operation_id}/inputs', headers=headers)
    assert inputs.status_code == 200
    assert inputs.json()['budget']['max_model_calls'] == 0


def test_private_decision_not_returned_by_inputs_or_authorized_export(monkeypatch):
    client, headers, source_id, path, parent = _setup(monkeypatch)
    payload = {'parent_version_id': parent, 'max_model_calls': 0, 'max_search_calls': 0,
               'discover_sources': False, 'auto_fetch_official': False}
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'acquisition-private'}, json=payload)
    assert response.status_code == 202, response.text
    operation_id = response.json()['operation']['operation_id']
    assert _terminal(client, path, headers, operation_id) == 'completed'
    record = api.RESEARCH_RUN_INPUTS.get(operation_id)
    record['private_acquisition_decisions'] = [{'proposal': {'query': 'PRIVATE_QUERY_SENTINEL', 'reason': 'PRIVATE_REASON_SENTINEL'},
                                                'comparison': [{'url': 'https://private.example/secret'}]}]
    api.RESEARCH_RUN_INPUTS.put(operation_id, record)
    inputs = client.get(f'{path}/runs/{operation_id}/inputs', headers=headers)
    topic = client.get(path, headers=headers)
    assert inputs.status_code == topic.status_code == 200
    version = topic.json()['versions'][-1]
    exported = client.get(f"{path}/versions/{version['version_id']}/export", headers=headers)
    assert exported.status_code == 200, exported.text
    for result in (inputs, topic, exported):
        assert 'PRIVATE_QUERY_SENTINEL' not in result.text
        assert 'PRIVATE_REASON_SENTINEL' not in result.text
        assert 'private.example' not in result.text
    assert source_id in {row['source_id'] for row in exported.json()['source_provenance']}
