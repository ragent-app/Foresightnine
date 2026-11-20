"""Account-aware capabilities, runtime binding stability and turn usage status (BYOK market data)."""
import time
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as api
from app.model_protocol import ModelUsage
from tests.test_research_runtime import turn
from tests.test_research_runtime_api import _save_twelve_data_key

PERMS = {'store': True, 'display': True, 'model_input': True, 'export': True}


def _login(client):
    auth = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    return {'Authorization': 'Bearer ' + auth['access_token']}


def test_capabilities_follow_the_accounts_own_market_key_and_ignore_platform_config(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setenv('FORESIGHTNINE_SEC_USER_AGENT', 'ForesightNine Tests tests@example.test')
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_HISTORICAL_PRICES', 'twelve_data')
    monkeypatch.setenv('FORESIGHTNINE_TWELVE_DATA_API_KEY', 'platform-key-9999')
    client = TestClient(api.app)
    without_key, with_key = _login(client), _login(client)
    caps = client.get('/v2/research/capabilities', headers=without_key).json()
    assert caps['market_data_available'] is False and caps['market_data_key_required'] is True
    assert caps['sec_financials_available'] is True and caps['filings_available'] is True
    assert 'historical_prices_available' not in caps
    _save_twelve_data_key(client, with_key, 'personal-key-1234')
    caps = client.get('/v2/research/capabilities', headers=with_key).json()
    assert caps['market_data_available'] is True and caps['market_data_key_required'] is False
    assert 'personal-key-1234' not in str(caps)
    # The other account is unaffected by the first account's key.
    assert client.get('/v2/research/capabilities', headers=without_key).json()['market_data_available'] is False


def test_capabilities_without_sec_contact_report_sec_tools_unavailable(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.delenv('FORESIGHTNINE_SEC_USER_AGENT', raising=False)
    monkeypatch.delenv('SEC_USER_AGENT', raising=False)
    client = TestClient(api.app)
    caps = client.get('/v2/research/capabilities', headers=_login(client)).json()
    assert caps['sec_financials_available'] is False and caps['filings_available'] is False


def _binding(monkeypatch=None):
    provider = SimpleNamespace(name='binding-test', policy=None, settings=SimpleNamespace(base_url='http://m', model='m'))
    return api._runtime_provider_binding(provider, SimpleNamespace(state=SimpleNamespace()))


def test_runtime_binding_is_deterministic_and_independent_of_platform_price_config(monkeypatch):
    first = _binding()
    assert first == _binding()
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_HISTORICAL_PRICES', 'twelve_data')
    monkeypatch.setenv('FORESIGHTNINE_TWELVE_DATA_API_KEY', 'platform-key-9999')
    assert _binding() == first


def _run(client, headers, path, parent, **body):
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex},
                           json={'parent_version_id': parent, 'discover_sources': False, **body})
    assert response.status_code == 202, response.text
    operation_id = response.json()['operation']['operation_id']
    for _ in range(300):
        state = client.get(path + '/runs/' + operation_id, headers=headers).json()['operation']['status']
        if state in ('completed', 'failed', 'cancelled'):
            break
        time.sleep(.01)
    return operation_id, state


def _setup(monkeypatch, provider):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setattr(api, 'challenge_provider', provider)
    client = TestClient(api.app)
    headers = _login(client)
    source_id = client.post('/v2/research/sources', headers=headers, json={'title': 'Report', 'text': 'Revenue was 100 USD.', 'permissions': PERMS}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?', 'source_ids': [source_id]}).json()
    return client, headers, source_id, topic, '/v2/research/topics/' + topic['topic_id']


class _Provider:
    name = 'usage-status-test'
    policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)

    def __init__(self, source_id_box, first_usage):
        self.calls, self.box, self.first_usage = 0, source_id_box, first_usage

    def complete(self, messages, tools, **kwargs):
        self.calls += 1
        sid = self.box[0]
        if self.calls == 1:
            response = turn('read_sources', {'reads': [{'source_id': sid}], 'rationale': 'disclosure'}, 'read')
            return replace(response, usage=self.first_usage)
        return turn('submit_research', {'report': 'Revenue was 100 USD.', 'claims': [{'text': 'Revenue was 100 USD.', 'source_id': sid, 'quotation': 'Revenue was 100 USD.', 'role': 'support'}], 'limitations': [], 'stopping_reason': 'answered'}, 'submit')


def test_turn_usage_reports_known_lower_bound_when_a_call_has_unknown_usage(monkeypatch):
    box = [None]
    provider = _Provider(box, None)
    client, headers, source_id, topic, path = _setup(monkeypatch, provider)
    box[0] = source_id
    _op, state = _run(client, headers, path, topic['versions'][0]['version_id'])
    assert state == 'completed'
    usage = client.get(path, headers=headers).json()['versions'][-1]['content']['turns'][0]['usage']
    assert usage['usage_status'] == 'partial_unknown'
    assert usage['model_calls'] >= 1 and usage['total_tokens'] > 0  # the reserved estimate, shown instead of 0


def test_turn_usage_is_complete_when_every_call_reports_usage(monkeypatch):
    box = [None]
    provider = _Provider(box, ModelUsage(1, 1, 2))
    client, headers, source_id, topic, path = _setup(monkeypatch, provider)
    box[0] = source_id
    _op, state = _run(client, headers, path, topic['versions'][0]['version_id'])
    assert state == 'completed'
    usage = client.get(path, headers=headers).json()['versions'][-1]['content']['turns'][0]['usage']
    assert usage['usage_status'] == 'complete' and usage['model_calls'] == 2 and usage['total_tokens'] == 4


def test_resume_binding_survives_platform_price_env_changes(monkeypatch):
    """A partial run continues after the retired platform price env var changes (binding no longer reads it)."""
    box = [None]
    provider = _Provider(box, ModelUsage(1, 1, 2))
    client, headers, source_id, topic, path = _setup(monkeypatch, provider)
    box[0] = source_id
    op, state = _run(client, headers, path, topic['versions'][0]['version_id'], max_model_calls=1)
    assert state == 'completed'
    assert client.get(path, headers=headers).json()['versions'][-1]['content']['turns'][-1]['status'] == 'partial'
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_HISTORICAL_PRICES', 'twelve_data')
    status = client.get(path + '/runs/' + op + '/recovery-status', headers=headers).json()
    assert status['can_continue'], status
    continued = client.post(path + '/runs/' + op + '/continue', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={})
    assert continued.status_code == 202, continued.text
