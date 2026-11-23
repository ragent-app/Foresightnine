"""Acceptance of current account, recovery, export and XBRL version boundaries."""
import base64
import json
import time
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as api
from app.model_protocol import ModelResponse, ModelUsage
from tests.test_research_runtime import turn
from tests.test_research_table_provenance import source_for, xbrl_fixture


def register(client):
    auth = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test',
                       'password': 'correct horse battery staple!'})
    assert auth.status_code == 201, auth.text
    return {'Authorization': 'Bearer ' + auth.json()['access_token']}


def terminal(client, path, headers, op):
    for _ in range(300):
        response = client.get(f'{path}/runs/{op}', headers=headers)
        assert response.status_code == 200, response.text
        state = response.json()['operation']['status']
        if state in ('completed', 'failed', 'cancelled'):
            return state
        time.sleep(.01)
    raise AssertionError('run did not complete')


def test_partial_recovery_account_revision_export_and_xbrl_versions(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    html = xbrl_fixture()
    row = next(line for line in source_for(html).text.splitlines() if '100' in line)
    marker = 'private-diagnostic-' + uuid4().hex
    class Provider:
        name = 'debt-acceptance'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)
        def __init__(self): self.calls = 0
        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return turn('read_sources', {'reads': [{'source_id': sid}], 'rationale': 'read'}, 'read')
            if self.calls == 2:
                facts = [{'label': 'Total revenue FY' + year, 'value': val, 'unit': 'USD million',
                          'currency': 'USD', 'period': 'FY' + year + ' (year ended ' + date + ')',
                          'source_id': sid, 'quotation': row}
                         for val, year, date in [('100', '2024', 'July 27, 2024'), ('90', '2023', 'July 29, 2023')]]
                return turn('record_facts', {'facts': facts, 'rationale': 'record'}, 'facts')
            if self.calls == 3:
                return turn('read_sources', {'bad_argument': marker}, 'bad')
            return ModelResponse({'role': 'assistant', 'content': 'Not yet.'}, (), ModelUsage(1, 1, 2), 'stop')
    provider = Provider()
    monkeypatch.setattr(api, 'challenge_provider', provider)
    client = TestClient(api.app)
    headers, other = register(client), register(client)
    uploaded = client.post('/v2/research/sources/html', headers=headers, json={
        'title': 'Filing', 'html_base64': base64.b64encode(html.encode()).decode(),
        'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True}})
    assert uploaded.status_code == 201, uploaded.text
    sid = uploaded.json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Compare revenue', 'source_ids': [sid]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    start = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={
        'parent_version_id': topic['versions'][0]['version_id'], 'discover_sources': False, 'max_model_calls': 4})
    assert start.status_code == 202, start.text
    op = start.json()['operation']['operation_id']
    assert terminal(client, path, headers, op) == 'completed'
    status = client.get(f'{path}/runs/{op}/recovery-status', headers=headers).json()
    assert status['can_continue'], status
    assert marker in json.dumps(api.RESEARCH_RUN_INPUTS.get(op))
    for suffix in ('recovery-status', 'settlement', 'inputs'):
        assert client.get(f'{path}/runs/{op}/{suffix}', headers=other).status_code in (403, 404)
    assert client.post(f'{path}/runs/{op}/continue', headers={**other, 'Idempotency-Key': uuid4().hex}, json={}).status_code in (403, 404)
    original = client.get(path, headers=headers).json()['versions'][-1]
    facts = original['content']['facts']
    assert len(facts) == 2 and all(f.get('xbrl_source_consistency') for f in facts), facts
    exported = client.get(path + '/versions/' + original['version_id'] + '/export', headers=headers)
    assert exported.status_code == 200, exported.text
    assert marker not in exported.text
    assert 'runtime_checkpoint' not in exported.text
    assert facts[0]['xbrl_source_consistency']['fact_sha256'] in exported.text
    content = deepcopy(original['content'])
    content['facts'][0]['label'] += ' edited'
    edit = client.post(path + '/versions', headers=headers, json={'parent_version_id': original['version_id'], 'content': content})
    assert edit.status_code == 201, edit.text
    new = edit.json()['versions'][-1]
    assert new['content']['facts'][0]['xbrl_source_consistency'] is None
    assert new['content']['facts'][1]['xbrl_source_consistency']
    reopened = client.get(path, headers=headers).json()['versions']
    assert reopened[-2]['content']['facts'][0]['xbrl_source_consistency']
    assert reopened[-1]['content']['facts'][0]['xbrl_source_consistency'] is None
    edited_export = client.get(path + '/versions/' + new['version_id'] + '/export', headers=headers)
    assert edited_export.status_code == 200, edited_export.text
    assert facts[0]['xbrl_source_consistency']['fact_sha256'] not in edited_export.text
    after = client.get(f'{path}/runs/{op}/recovery-status', headers=headers).json()
    assert not after['can_continue'] and after['reason'] == 'changed', after
    blocked = client.post(f'{path}/runs/{op}/continue', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={})
    assert blocked.status_code == 409, blocked.text
    assert provider.calls == 4
