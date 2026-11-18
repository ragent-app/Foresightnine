import time
from uuid import uuid4
from types import SimpleNamespace

from fastapi.testclient import TestClient
import app.main as api
from tests.test_research_runtime import turn


def test_native_research_api_persists_citations_and_ledger(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Provider:
        name = 'native-api-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)
        def __init__(self): self.calls = 0
        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return turn('read_sources', {'reads': [{'source_id': source_id}], 'rationale': 'disclosure'}, 'read')
            return turn('submit_research', {'report': 'Revenue was 100 USD.', 'claims': [{'text': 'Revenue was 100 USD.', 'source_id': source_id, 'quotation': 'Revenue was 100 USD.'}], 'limitations': [], 'stopping_reason': 'answered'}, 'submit')
    provider = Provider()
    monkeypatch.setattr(api, 'challenge_provider', provider)
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title': 'Report', 'text': 'Revenue was 100 USD.', 'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?', 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={'parent_version_id': topic['versions'][0]['version_id'], 'discover_sources': False})
    assert response.status_code == 202, response.text
    operation_id = response.json()['operation']['operation_id']
    for _ in range(200):
        operation = client.get(path + '/runs/' + operation_id, headers=headers).json()['operation']
        if operation['status'] in ('completed', 'failed', 'cancelled'): break
        time.sleep(.01)
    assert operation['status'] == 'completed', operation
    content = client.get(path, headers=headers).json()['versions'][-1]['content']
    assert content['summary'] == 'Revenue was 100 USD.'
    assert content['claims'][0]['citations'][0]['source_id'] == source_id
    assert provider.calls == 2
    settlement = client.get(path + '/runs/' + operation_id + '/settlement', headers=headers).json()
    assert settlement['model_ledger']['settled_calls'] == 2
    assert 'runtime_checkpoint' not in client.get(path + '/runs/' + operation_id + '/inputs', headers=headers).json()


def test_completed_submission_recovers_without_another_model_call(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Provider:
        name = 'native-recovery-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)
        def __init__(self): self.calls = 0
        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return turn('read_sources', {'reads': [{'source_id': source_id}], 'rationale': 'disclosure'}, 'read')
            return turn('submit_research', {'report': 'Revenue was 100 USD.', 'claims': [{'text': 'Revenue was 100 USD.', 'source_id': source_id, 'quotation': 'Revenue was 100 USD.'}], 'limitations': [], 'stopping_reason': 'answered'}, 'submit')
    provider = Provider()
    monkeypatch.setattr(api, 'challenge_provider', provider)
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title': 'Report', 'text': 'Revenue was 100 USD.', 'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?', 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    original = api.ResearchVersionChain.save
    failed = [False]
    def fail_once(self, parent, content):
        if not failed[0]:
            failed[0] = True
            raise api.TopicVersionConflict()
        return original(self, parent, content)
    monkeypatch.setattr(api.ResearchVersionChain, 'save', fail_once)
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={'parent_version_id': topic['versions'][0]['version_id'], 'discover_sources': False, 'max_model_calls': 2})
    operation_id = response.json()['operation']['operation_id']
    for _ in range(200):
        state = client.get(path + '/runs/' + operation_id, headers=headers).json()['operation']['status']
        if state in ('completed', 'failed', 'cancelled'): break
        time.sleep(.01)
    assert state == 'failed'
    status = client.get(path + '/runs/' + operation_id + '/recovery-status', headers=headers).json()
    assert status['can_deliver'] and status['version_id'] == topic['versions'][0]['version_id']
    recovery = client.post(path + '/runs/' + operation_id + '/recover', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={})
    assert recovery.status_code == 202, recovery.text
    recovered_id = recovery.json()['operation']['operation_id']
    for _ in range(200):
        state = client.get(path + '/runs/' + recovered_id, headers=headers).json()['operation']['status']
        if state in ('completed', 'failed', 'cancelled'): break
        time.sleep(.01)
    assert state == 'completed'
    assert provider.calls == 2
    content = client.get(path, headers=headers).json()['versions'][-1]['content']
    assert content['summary'] == 'Revenue was 100 USD.'
    assert client.get(path + '/runs/' + recovered_id + '/settlement', headers=headers).json()['model_ledger']['settled_calls'] == 2
    again = client.post(path + '/runs/' + operation_id + '/continue', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={})
    assert again.status_code == 409


def test_partial_continue_respects_cumulative_budget_and_unknown_blocks(monkeypatch):
    from app.model_protocol import ModelResponse, ModelUsage
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Provider:
        name = 'native-continuation-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)
        def __init__(self): self.calls = 0
        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return turn('read_sources', {'reads': [{'source_id': source_id}], 'rationale': 'disclosure'}, 'read')
            return ModelResponse({'role': 'assistant', 'content': 'Not yet.'}, (), ModelUsage(1, 1, 2), 'stop')
    provider = Provider()
    monkeypatch.setattr(api, 'challenge_provider', provider)
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title': 'Report', 'text': 'Revenue was 100 USD.', 'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?', 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={'parent_version_id': topic['versions'][0]['version_id'], 'discover_sources': False, 'max_model_calls': 2})
    operation_id = response.json()['operation']['operation_id']
    for _ in range(200):
        state = client.get(path + '/runs/' + operation_id, headers=headers).json()['operation']['status']
        if state in ('completed', 'failed', 'cancelled'): break
        time.sleep(.01)
    assert state == 'completed'
    assert client.get(path, headers=headers).json()['versions'][-1]['content']['run']['status'] == 'partial'
    status = client.get(path + '/runs/' + operation_id + '/recovery-status', headers=headers).json()
    assert status['can_continue'] and status['version_id'] == client.get(path, headers=headers).json()['versions'][-1]['version_id']
    continued = client.post(path + '/runs/' + operation_id + '/continue', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={})
    assert continued.status_code == 202, continued.text
    continued_id = continued.json()['operation']['operation_id']
    for _ in range(200):
        state = client.get(path + '/runs/' + continued_id, headers=headers).json()['operation']['status']
        if state in ('completed', 'failed', 'cancelled'): break
        time.sleep(.01)
    assert state == 'completed'
    assert provider.calls == 2
    record = api.RESEARCH_RUN_INPUTS.get(continued_id)
    record['runtime_checkpoint']['pending'] = {'kind': 'model', 'call_id': 'unknown'}
    record['recovery_pending'] = [{'kind': 'model', 'call_id': 'unknown'}]
    api.RESEARCH_RUN_INPUTS.put(continued_id, record)
    blocked = client.post(path + '/runs/' + continued_id + '/continue', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={})
    assert blocked.status_code == 409
    assert provider.calls == 2


def test_returned_response_settles_original_account_after_continuation(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Provider:
        name = 'native-settlement-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)
        def __init__(self): self.calls = 0
        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return turn('read_sources', {'reads': [{'source_id': source_id}], 'rationale': 'disclosure'}, 'read')
            return turn('submit_research', {'report': 'Revenue was 100 USD.', 'claims': [{'text': 'Revenue was 100 USD.', 'source_id': source_id, 'quotation': 'Revenue was 100 USD.'}], 'limitations': [], 'stopping_reason': 'answered'}, 'submit')
    provider = Provider()
    monkeypatch.setattr(api, 'challenge_provider', provider)
    original_settle = api.billing_ledger.settle
    attempts = [0]
    def fail_once(*args, **kwargs):
        attempts[0] += 1
        if attempts[0] == 1:
            raise RuntimeError('settlement unavailable')
        return original_settle(*args, **kwargs)
    monkeypatch.setattr(api.billing_ledger, 'settle', fail_once)
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title': 'Report', 'text': 'Revenue was 100 USD.', 'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?', 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={'parent_version_id': topic['versions'][0]['version_id'], 'discover_sources': False, 'max_model_calls': 2})
    operation_id = response.json()['operation']['operation_id']
    for _ in range(200):
        state = client.get(path + '/runs/' + operation_id, headers=headers).json()['operation']['status']
        if state in ('completed', 'failed', 'cancelled'): break
        time.sleep(.01)
    assert state == 'completed'
    assert provider.calls == 1
    assert client.get(path + '/runs/' + operation_id + '/settlement', headers=headers).json()['model_ledger']['reserved_calls'] == 1
    status = client.get(path + '/runs/' + operation_id + '/recovery-status', headers=headers).json()
    assert status['can_continue'] and status['version_id'] == client.get(path, headers=headers).json()['versions'][-1]['version_id']
    continued = client.post(path + '/runs/' + operation_id + '/continue', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={})
    assert continued.status_code == 202, continued.text
    continued_id = continued.json()['operation']['operation_id']
    for _ in range(200):
        state = client.get(path + '/runs/' + continued_id, headers=headers).json()['operation']['status']
        if state in ('completed', 'failed', 'cancelled'): break
        time.sleep(.01)
    assert state == 'completed'
    settlement = client.get(path + '/runs/' + continued_id + '/settlement', headers=headers).json()['model_ledger']
    assert settlement['settled_calls'] == 2 and settlement['reserved_calls'] == 0
    assert provider.calls == 2


def test_discovery_candidates_are_visible_without_restricted_excerpts(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Search:
        supports_project_search = True
        def search(self, query, **kwargs):
            return [{'title': 'Allowed', 'url': 'https://example.com/report', 'excerpt': 'private lead text'},
                    {'title': 'FRED SECRET', 'url': 'https://fred.stlouisfed.org/series/GDP', 'excerpt': 'FRED SECRET'}]
    class Provider:
        name = 'native-discovery-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)
        def __init__(self): self.calls = 0
        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return turn('discover_sources', {'query': 'revenue', 'rationale': 'find reports'}, 'search')
            if self.calls == 2:
                assert 'FRED SECRET' not in str(messages)
                return turn('read_sources', {'reads': [{'source_id': source_id}], 'rationale': 'disclosure'}, 'read')
            return turn('submit_research', {'report': 'Revenue was 100 USD.', 'claims': [{'text': 'Revenue was 100 USD.', 'source_id': source_id, 'quotation': 'Revenue was 100 USD.'}], 'limitations': [], 'stopping_reason': 'answered'}, 'submit')
    monkeypatch.setattr(api, 'evidence_provider', Search())
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title': 'Report', 'text': 'Revenue was 100 USD.', 'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True}}).json()['source_id']
    restricted = client.post('/v2/research/sources', headers=headers, json={'title': 'Restricted', 'text': 'not authorized', 'permissions': {'store': True, 'display': True, 'model_input': False}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?', 'source_ids': [source_id, restricted]}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={'parent_version_id': topic['versions'][0]['version_id'], 'discover_sources': True, 'auto_fetch_official': False})
    operation_id = response.json()['operation']['operation_id']
    for _ in range(200):
        state = client.get(path + '/runs/' + operation_id, headers=headers).json()['operation']['status']
        if state in ('completed', 'failed', 'cancelled'): break
        time.sleep(.01)
    assert state == 'completed'
    content = client.get(path, headers=headers).json()['versions'][-1]['content']
    assert content['source_candidates'] == [{'title': 'Allowed', 'url': 'https://example.com/report', 'status': 'unverified', 'published_at': None}]
    assert 'FRED SECRET' not in str(content) and 'private lead text' not in str(content)
    assert any(row['source_id'] == restricted and row['eligibility'] == 'model_permission_missing' for row in content['run']['source_processing'])
