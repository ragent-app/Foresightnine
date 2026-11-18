import json
import time
from uuid import uuid4
from types import SimpleNamespace

from fastapi.testclient import TestClient
import app.main as api
from tests.test_research_runtime import turn
from app.glm_mcp_search import SearchCandidate as ProviderSearchCandidate
from app.openai_compatible import BinaryHttpResponse


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
            return turn('submit_research', {'report': 'Revenue was 100 USD.', 'claims': [{'text': 'Revenue was 100 USD.', 'source_id': source_id, 'quotation': 'Revenue was 100 USD.', 'role': 'support'}], 'limitations': [], 'stopping_reason': 'answered'}, 'submit')
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
    assert content['summary'] == ''  # The user owns the summary; the answer is the turn's report.
    assert content['turns'][0]['report'] == 'Revenue was 100 USD.'
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
            return turn('submit_research', {'report': 'Revenue was 100 USD.', 'claims': [{'text': 'Revenue was 100 USD.', 'source_id': source_id, 'quotation': 'Revenue was 100 USD.', 'role': 'support'}], 'limitations': [], 'stopping_reason': 'answered'}, 'submit')
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
    assert content['turns'][-1]['report'] == 'Revenue was 100 USD.'
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
    assert client.get(path, headers=headers).json()['versions'][-1]['content']['turns'][-1]['status'] == 'partial'
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
            return turn('submit_research', {'report': 'Revenue was 100 USD.', 'claims': [{'text': 'Revenue was 100 USD.', 'source_id': source_id, 'quotation': 'Revenue was 100 USD.', 'role': 'support'}], 'limitations': [], 'stopping_reason': 'answered'}, 'submit')
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
            return [ProviderSearchCandidate('Allowed', 'https://example.com/report#section', 'private lead text', None, None, False),
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
            return turn('submit_research', {'report': 'Revenue was 100 USD.', 'claims': [{'text': 'Revenue was 100 USD.', 'source_id': source_id, 'quotation': 'Revenue was 100 USD.', 'role': 'support'}], 'limitations': [], 'stopping_reason': 'answered'}, 'submit')
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
    assert [row['source_id'] for row in content['sources']] == [source_id]  # A source without model permission is never read.


def test_question_only_run_can_discover_and_cite_historical_prices(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setenv('FORESIGHTNINE_TWELVEDATA_API_KEY', 'fixture')
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_HISTORICAL_PRICES', json.dumps({
        'provider': 'twelve_data', 'authorized': True,
        'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True},
        'instruments': [{'instrument_id': 'INDEX:DEMO', 'symbol': 'DEMO', 'exchange': 'DEMO EXCHANGE',
                         'currency': 'USD', 'adjustment': 'none'}],
    }))
    quotes = [f'INDEX:DEMO | 2020-01-0{day} | daily close | {price} USD'
              for day, price in ((3, 125), (2, 100))]

    class Search:
        supports_project_search = True

        def search(self, query, **kwargs):
            return (ProviderSearchCandidate('Index methodology', 'https://example.com/index', 'Unverified methodology', None, None, True),)

    class Provider:
        name = 'historical-price-api-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)

        def complete(self, messages, tools, **kwargs):
            results = [json.loads(message['content']) for message in messages if message['role'] == 'tool']
            stage = len(results)
            if stage == 0:
                assert json.loads(messages[1]['content'])['available_historical_price_instruments'] == ['INDEX:DEMO']
                return turn('discover_sources', {'query': 'DEMO index', 'rationale': 'Find context.'}, 'search')
            if stage == 1:
                assert results[0]['leads'][0]['url'] == 'https://example.com/index'
                return turn('historical_prices', {'instrument_id': 'INDEX:DEMO', 'start_date': '2020-01-02',
                            'end_date': '2020-01-03', 'rationale': 'Get closing observations.'}, 'prices')
            source_id = results[1]['source_id']
            if stage == 2:
                return turn('read_sources', {'reads': [{'source_id': source_id}], 'rationale': 'Read the observations.'}, 'read')
            if stage == 3:
                assert all(quote in results[2]['results'][0]['text'] for quote in quotes)
                return turn('record_facts', {'rationale': 'Record the two closes.', 'facts': [
                    {'label': 'daily close', 'period': day, 'value': price, 'unit': 'USD', 'currency': 'USD',
                     'source_id': source_id, 'quotation': quote}
                    for day, price, quote in zip(('2020-01-03', '2020-01-02'), (125, 100), quotes, strict=True)
                ]}, 'facts')
            if stage == 4:
                first, second = results[3]['facts']
                return turn('calculate_financials', {'rationale': 'Compare the closes.', 'calculations': [
                    {'formula': 'growth_percent', 'first_fact_id': first['fact_id'], 'second_fact_id': second['fact_id']}
                ]}, 'calculation')
            assert results[4]['calculations'][0]['value'] == '25.00'
            return turn('submit_research', {'report': 'The close rose from 100 to 125.', 'claims': [
                {'text': 'Both closing values are sourced.', 'source_id': source_id, 'quotation': quotes[0], 'role': 'support'},
            ], 'limitations': [], 'stopping_reason': 'answered'}, 'submit')

    def fetch(*args, **kwargs):
        return BinaryHttpResponse(200, json.dumps({
            'meta': {'symbol': 'DEMO', 'exchange': 'DEMO EXCHANGE', 'interval': '1day', 'currency': 'USD'},
            'values': [{'datetime': '2020-01-02', 'close': '100'}, {'datetime': '2020-01-03', 'close': '125'}],
        }).encode())

    monkeypatch.setattr(api, 'evidence_provider', Search())
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    monkeypatch.setattr('app.research_historical_prices._default_binary_transport', fetch)
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test',
                       'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'How did the index move?'}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex},
                           json={'parent_version_id': topic['versions'][0]['version_id']})
    assert response.status_code == 202, response.text
    operation_id = response.json()['operation']['operation_id']
    for _ in range(200):
        operation = client.get(path + '/runs/' + operation_id, headers=headers).json()['operation']
        if operation['status'] in ('completed', 'failed', 'cancelled'):
            break
        time.sleep(.01)
    assert operation['status'] == 'completed', operation
    version = client.get(path, headers=headers).json()['versions'][-1]
    assert version['content']['turns'][-1]['status'] == 'completed'
    assert version['content']['calculations'][0]['value'] == '25.00'
    exported = client.get(path + '/versions/' + version['version_id'] + '/export', headers=headers)
    assert exported.status_code == 200, exported.text
    assert exported.json()['source_provenance'][0]['retrieval_method'] == 'historical_prices'


def test_native_claim_roles_persist_and_counterargument_survives_user_edit(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    text = 'Revenue was 100 USD. Costs rose sharply.'
    class Provider:
        name = 'native-role-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)
        def __init__(self): self.calls = 0
        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return turn('read_sources', {'reads': [{'source_id': source_id}], 'rationale': 'disclosure'}, 'read')
            return turn('submit_research', {'report': 'Mixed evidence.', 'claims': [
                {'text': 'Revenue was 100 USD.', 'source_id': source_id, 'quotation': 'Revenue was 100 USD.', 'role': 'support'},
                {'text': 'Costs rose sharply.', 'source_id': source_id, 'quotation': 'Costs rose sharply.', 'role': 'counterargument'}],
                'limitations': [], 'stopping_reason': 'answered'}, 'submit')
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title': 'Report', 'text': text, 'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True}}).json()['source_id']
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
    latest = client.get(path, headers=headers).json()['versions'][-1]
    claims = latest['content']['claims']
    assert [c['role'] for c in claims] == ['support', 'counterargument']
    assert all('kind' not in c and c['citations'][0]['source_id'] == source_id for c in claims)
    # An untouched resubmission and an edited counterargument both pass user-version validation.
    edited = json.loads(json.dumps(latest['content']))
    edited['claims'][1]['text'] = 'Costs rose sharply, which weakens the margin conclusion.'
    saved = client.post(path + '/versions', headers=headers, json={'parent_version_id': latest['version_id'], 'content': edited})
    assert saved.status_code == 201, saved.text
    saved_claims = saved.json()['versions'][-1]['content']['claims']
    assert saved_claims[1]['role'] == 'counterargument'
    assert saved_claims[1]['text'].startswith('Costs rose sharply, which')
    assert saved_claims[1]['citations'][0]['quotation'] == 'Costs rose sharply.'
    assert saved_claims[1]['citations'][0]['location']['character_start'] == 21
    again = client.post(path + '/versions', headers=headers, json={'parent_version_id': saved.json()['versions'][-1]['version_id'], 'content': edited})
    assert again.status_code == 201, again.text
