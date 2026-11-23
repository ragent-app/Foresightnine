"""Offline acceptance of durable research supervision and delivery."""
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import app.main as api
from tests.test_research_runtime import turn


def _create(client):
    auth = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test',
        'password': 'correct horse battery staple!'})
    assert auth.status_code == 201, auth.text
    headers = {'Authorization': 'Bearer ' + auth.json()['access_token']}
    response = client.post('/v2/research/sources', headers=headers, json={'title': 'Report',
        'text': 'Revenue was 100 USD.', 'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True}})
    assert response.status_code == 201, response.text
    source_id = response.json()['source_id']
    response = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?', 'source_ids': [source_id]})
    assert response.status_code == 201, response.text
    return headers, source_id, response.json()


def _wait(client, path, headers, operation_id):
    for _ in range(500):
        response = client.get(path + '/runs/' + operation_id, headers=headers)
        assert response.status_code == 200, response.text
        status = response.json()['operation']['status']
        if status in ('completed', 'failed', 'cancelled'):
            return status
        time.sleep(.01)
    raise AssertionError('operation did not settle')


def test_failed_version_write_reconciles_once_with_two_workers(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Provider:
        name = 'offline-supervisor'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)
        def __init__(self): self.calls = 0
        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return turn('read_sources', {'reads': [{'source_id': source_id}], 'rationale': 'disclosure'}, 'read')
            return turn('submit_research', {'report': 'Revenue was 100 USD.', 'claims': [{'text': 'Revenue was 100 USD.',
                'source_id': source_id, 'quotation': 'Revenue was 100 USD.', 'role': 'support'}],
                'limitations': [], 'stopping_reason': 'answered'}, 'submit')
    provider = Provider()
    monkeypatch.setattr(api, 'challenge_provider', provider)
    client = TestClient(api.app)
    headers, source_id, topic = _create(client)
    path = '/v2/research/topics/' + topic['topic_id']
    original_save = api.ResearchVersionChain.save
    attempts = []
    lock = Lock()
    def fail_once(self, parent, content):
        with lock:
            attempts.append(1)
            if len(attempts) == 1:
                raise api.TopicVersionConflict()
        return original_save(self, parent, content)
    monkeypatch.setattr(api.ResearchVersionChain, 'save', fail_once)
    start = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex},
        json={'parent_version_id': topic['versions'][0]['version_id'], 'discover_sources': False, 'max_model_calls': 2})
    assert start.status_code == 202, start.text
    operation_id = start.json()['operation']['operation_id']
    assert _wait(client, path, headers, operation_id) == 'failed'
    assert len(client.get(path, headers=headers).json()['versions']) == 1
    before = provider.calls
    barrier = Barrier(2)
    def worker():
        barrier.wait()
        api._reconcile_research_work()
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda _: worker(), range(2)))
    versions = client.get(path, headers=headers).json()['versions']
    assert len(versions) == 2
    assert versions[-1]['content']['turns'][-1]['report'] == 'Revenue was 100 USD.'
    assert provider.calls == before == 2
    progress = client.get(path + '/runs/' + operation_id + '/progress', headers=headers)
    assert progress.status_code == 200, progress.text
    assert progress.json()['logical_status'] == 'completed'
    assert progress.json()['version_id'] == versions[-1]['version_id']


def test_workers_admit_one_continuation_and_old_id_finds_result(monkeypatch):
    from app.phase3 import ProviderFailure
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Provider:
        name = 'offline-supervisor-continuation'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)
        def __init__(self): self.calls = 0
        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return turn('read_sources', {'reads': [{'source_id': source_id}], 'rationale': 'disclosure'}, 'read')
            if self.calls == 2:
                kwargs['diagnostic_callback']({'submission_state': 'not_started', 'stages': []})
                raise ProviderFailure('provider_rate_limited', 'simulated', retryable=True)
            return turn('submit_research', {'report': 'Revenue was 100 USD.', 'claims': [{'text': 'Revenue was 100 USD.',
                'source_id': source_id, 'quotation': 'Revenue was 100 USD.', 'role': 'support'}],
                'limitations': [], 'stopping_reason': 'answered'}, 'submit')
    provider = Provider()
    monkeypatch.setattr(api, 'challenge_provider', provider)
    client = TestClient(api.app)
    headers, source_id, topic = _create(client)
    path = '/v2/research/topics/' + topic['topic_id']
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex},
        json={'parent_version_id': topic['versions'][0]['version_id'], 'discover_sources': False, 'max_model_calls': 3})
    assert response.status_code == 202, response.text
    old_id = response.json()['operation']['operation_id']
    assert _wait(client, path, headers, old_id) == 'failed'
    work = api.RESEARCH_WORK.get(old_id)
    assert work is not None
    api.RESEARCH_WORK.put(old_id, {**work, 'resume_after': None})
    barrier = Barrier(2)
    def worker():
        barrier.wait()
        api._reconcile_research_work()
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda _: worker(), range(2)))
    for _ in range(500):
        progress = client.get(path + '/runs/' + old_id + '/progress', headers=headers)
        assert progress.status_code == 200, progress.text
        view = progress.json()
        if view['logical_status'] == 'completed':
            break
        time.sleep(.01)
    assert view['logical_status'] == 'completed', view
    assert view['current_operation_id'] != old_id
    versions = client.get(path, headers=headers).json()['versions']
    assert len(versions) == 2
    assert view['version_id'] == versions[-1]['version_id']
    assert provider.calls == 3


def test_unknown_claimed_tool_is_not_replayed_on_recovery(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Provider:
        name = 'offline-supervisor-unknown'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)
        def __init__(self): self.calls = 0
        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            return turn('news_search', {'query': 'issuer disclosure', 'rationale': 'locate filing'}, 'lookup')
    provider = Provider()
    monkeypatch.setattr(api, 'challenge_provider', provider)
    client = TestClient(api.app)
    headers, source_id, topic = _create(client)
    path = '/v2/research/topics/' + topic['topic_id']
    real_claim = api.turn_instructions.claim_action
    calls = []
    def crash_after_start(*args, **kwargs):
        claimed = real_claim(*args, **kwargs)
        if claimed:
            calls.append('external request started, response lost')
            raise RuntimeError('simulated worker loss after external start')
        return claimed
    monkeypatch.setattr(api.turn_instructions, 'claim_action', crash_after_start)
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex},
        json={'parent_version_id': topic['versions'][0]['version_id'], 'discover_sources': False, 'max_model_calls': 3})
    assert response.status_code == 202, response.text
    old_id = response.json()['operation']['operation_id']
    assert _wait(client, path, headers, old_id) == 'failed'
    assert calls == ['external request started, response lost']
    for _ in range(500):
        api._reconcile_research_work()
        if not api.RESEARCH_WORK:
            break
        time.sleep(.01)
    assert not api.RESEARCH_WORK
    versions = client.get(path, headers=headers).json()['versions']
    assert len(versions) == 2, versions
    assert calls == ['external request started, response lost']
    assert provider.calls == 1


def test_lifespan_restart_resumes_without_browser_and_keeps_budget(monkeypatch):
    from app.phase3 import ProviderFailure
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    class Provider:
        name = 'offline-lifespan-continuation'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)
        def __init__(self): self.calls = 0
        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return turn('read_sources', {'reads': [{'source_id': source_id}], 'rationale': 'disclosure'}, 'read')
            if self.calls == 2:
                kwargs['diagnostic_callback']({'submission_state': 'not_started', 'stages': []})
                raise ProviderFailure('provider_rate_limited', 'simulated', retryable=True)
            return turn('submit_research', {'report': 'Revenue was 100 USD.', 'claims': [{'text': 'Revenue was 100 USD.',
                'source_id': source_id, 'quotation': 'Revenue was 100 USD.', 'role': 'support'}],
                'limitations': [], 'stopping_reason': 'answered'}, 'submit')
    provider = Provider()
    monkeypatch.setattr(api, 'challenge_provider', provider)
    with TestClient(api.app) as client:
        headers, source_id, topic = _create(client)
        path = '/v2/research/topics/' + topic['topic_id']
        start = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex},
            json={'parent_version_id': topic['versions'][0]['version_id'], 'discover_sources': False, 'max_model_calls': 2})
        assert start.status_code == 202, start.text
        old_id, turn_id = start.json()['operation']['operation_id'], start.json()['turn_id']
        assert _wait(client, path, headers, old_id) == 'failed'
        work = api.RESEARCH_WORK.get(old_id)
        assert work and work.get('resume_after')
        assert len(client.get(path, headers=headers).json()['versions']) == 1
    # No HTTP request or manual reconcile occurs while the restarted app runs.
    with TestClient(api.app) as client:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            summary = api.TURN_STATUS.get(topic['topic_id']) or {}
            if summary.get('logical_status') == 'completed':
                break
            time.sleep(.1)
        assert summary.get('logical_status') == 'completed', summary
        assert summary['turn_id'] == turn_id
        new_id = summary['current_operation_id']
        assert new_id != old_id
        statuses = client.get('/v2/research/turn-statuses', headers=headers)
        assert statuses.status_code == 200, statuses.text
        entry = next(item for item in statuses.json() if item['topic_id'] == topic['topic_id'])
        assert entry == {key: summary[key] for key in ('topic_id', 'turn_id', 'current_operation_id', 'logical_status', 'version_id')}
        versions = client.get(path, headers=headers).json()['versions']
        assert len(versions) == 2 and versions[-1]['version_id'] == entry['version_id']
        saved = versions[-1]['content']['turns'][-1]
        assert saved['turn_id'] == turn_id and old_id in saved['operation_ids'] and new_id in saved['operation_ids']
        assert saved['report'] == 'Revenue was 100 USD.'
        record = api.RESEARCH_RUN_INPUTS.get(new_id)
        assert record['runtime_request']['max_model_calls'] == 2
        assert record['runtime_checkpoint']['_accounting']['calls'] == 2
        assert provider.calls == 3  # Two settled calls and one confirmed pre-send failure.
