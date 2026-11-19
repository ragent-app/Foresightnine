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


def _tiny_pdf_bytes() -> bytes:
    streams = [b'BT /F1 12 Tf 20 150 Td (Revenue 100) Tj ET', b'BT /F1 12 Tf 20 150 Td (Revenue 200) Tj ET']
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 7 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    objects.extend(b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream' for stream in streams)
    pdf = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f'{index} 0 obj\n'.encode() + obj + b'\nendobj\n')
    start = len(pdf)
    pdf.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        pdf.extend(f'{offset:010d} 00000 n \n'.encode())
    pdf.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode())
    return bytes(pdf)


def test_question_only_run_fetches_open_web_html_and_pdf_and_persists_partial_notes(monkeypatch):
    import app.research_fetch as research_fetch

    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.delenv('FORESIGHTNINE_FETCH_ALLOWED_HOSTS', raising=False)
    monkeypatch.delenv('FORESIGHTNINE_FETCH_BLOCKED_HOSTS', raising=False)
    monkeypatch.delenv('FORESIGHTNINE_SEC_USER_AGENT', raising=False)
    monkeypatch.delenv('SEC_USER_AGENT', raising=False)

    html_url = 'https://ir.example.com/news/q4'
    pdf_url = 'https://ir.example.com/download?id=99'
    pdf_bytes = _tiny_pdf_bytes()
    html_bytes = b'<html><p>Web revenue was 100 USD.</p></html>'

    class Search:
        supports_project_search = True

        def search(self, query, **kwargs):
            return [
                ProviderSearchCandidate('IR News', html_url, 'Unverified IR lead', None, None, True),
                ProviderSearchCandidate('IR PDF', pdf_url, 'Unverified PDF lead', None, None, True),
            ]

    def fake_transport(method, url, headers, body, timeout, **kwargs):
        if url == html_url:
            return BinaryHttpResponse(200, html_bytes, {'content-type': 'text/html; charset=utf-8'})
        if url == pdf_url:
            return BinaryHttpResponse(200, pdf_bytes, {'content-type': 'application/pdf'})
        raise AssertionError(f'Unexpected URL: {url}')

    partial_notes = 'Acquired IR HTML and PDF; SEC structured data is unconfigured in this environment.'
    seen_caps = {}

    class Provider:
        name = 'open-web-api-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)

        def complete(self, messages, tools, **kwargs):
            results = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
            stage = len(results)
            if stage == 0:
                payload = json.loads(messages[1]['content'])
                seen_caps['value'] = payload['available_capabilities']
                return turn('discover_sources', {'query': 'Example revenue', 'rationale': 'Find web sources.'}, 'd1')
            if stage == 1:
                return turn('acquire_sources', {'urls': [html_url, pdf_url], 'rationale': 'Fetch HTML and PDF.'}, 'a1')
            if stage == 2:
                acquired = results[1]['results']
                assert all(item['status'] == 'acquired' for item in acquired), results[1]
                self.html_sid = acquired[0]['source_id']
                self.pdf_sid = acquired[1]['source_id']
                return turn('read_sources', {'reads': [{'source_id': self.html_sid}, {'source_id': self.pdf_sid}],
                                             'rationale': 'Read both sources.'}, 'r1')
            return turn('submit_research', {
                'report': partial_notes,
                'claims': [
                    {'text': 'IR release states 100 USD revenue.', 'source_id': self.html_sid,
                     'quotation': 'Web revenue was 100 USD.', 'role': 'support'},
                    {'text': 'IR PDF page 2 states Revenue 200.', 'source_id': self.pdf_sid,
                     'quotation': 'Revenue 200', 'role': 'support'},
                ],
                'limitations': ['SEC structured data is unconfigured.'],
                'stopping_reason': 'partial_sources_only',
                'status': 'partial',
            }, 's1')

    monkeypatch.setattr(api, 'evidence_provider', Search())
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    monkeypatch.setattr(api, '_reserve_search_call', lambda *_: None)
    monkeypatch.setattr(research_fetch, '_validated_addresses', lambda host, port, *, allow_private=False: ('93.184.216.34',))
    monkeypatch.setattr(research_fetch, '_default_binary_transport', fake_transport)

    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test',
                                                        'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'What was revenue?'}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    run_resp = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex},
                           json={'parent_version_id': topic['versions'][0]['version_id']})
    assert run_resp.status_code == 202, run_resp.text
    operation_id = run_resp.json()['operation']['operation_id']
    for _ in range(200):
        op = client.get(path + '/runs/' + operation_id, headers=headers).json()['operation']
        if op['status'] in ('completed', 'failed', 'cancelled'):
            break
        time.sleep(.01)
    assert op['status'] == 'completed', op

    assert seen_caps['value']['search_available'] is True
    assert seen_caps['value']['acquire_available'] is True
    assert seen_caps['value']['web_fetch_enabled'] is True
    assert seen_caps['value']['sec_available'] is False
    assert seen_caps['value']['max_fetch_bytes'] == 5_000_000

    version = client.get(path, headers=headers).json()['versions'][-1]
    saved_turn = version['content']['turns'][-1]
    assert saved_turn['status'] == 'partial'
    assert saved_turn['report'] == ''
    assert saved_turn['partial_notes'] == partial_notes
    assert saved_turn['stopping_note'] == 'partial_sources_only'
    assert len(version['content']['claims']) == 2

    src_meta = {row['retrieval_method']: row for row in client.get(
        f"{path}/versions/{version['version_id']}/sources", headers=headers).json()}
    assert set(src_meta) == {'web_html', 'web_pdf'}
    assert src_meta['web_html']['original_html_available'] is True
    assert 'span markers' in src_meta['web_html']['extraction_disclosure']
    assert src_meta['web_pdf']['original_pdf_available'] is True
    assert src_meta['web_html']['permissions']['export'] is False
    assert src_meta['web_pdf']['permissions']['export'] is False

    pdf_id = src_meta['web_pdf']['source_id']
    pdf_full = client.get(f'/v2/research/sources/{pdf_id}', headers=headers).json()
    p2_start = pdf_full['text'].index('Revenue 200')
    pdf_ctx = client.get(f'/v2/research/sources/{pdf_id}/context', headers=headers,
                         params={'start': p2_start, 'end': p2_start + len('Revenue 200')})
    assert pdf_ctx.status_code == 200 and pdf_ctx.json()['page_start'] == 2 and pdf_ctx.json()['page_end'] == 2

    assert client.get(f"/v2/research/sources/{src_meta['web_html']['source_id']}/original", headers=headers).status_code == 403
    assert client.get(f'/v2/research/sources/{pdf_id}/original', headers=headers).status_code == 403
    assert client.get(f"{path}/versions/{version['version_id']}/export", headers=headers).status_code == 403
    meta_export = client.get(f"{path}/versions/{version['version_id']}/export/metadata", headers=headers)
    assert meta_export.status_code == 200 and meta_export.json()['claim_count'] == 2


def test_question_only_run_calls_sec_company_concept_and_cites_sec_source(monkeypatch):
    import threading
    import app.research_structured_connector as connector
    from app.research_sec import SecResult

    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setenv('FORESIGHTNINE_SEC_USER_AGENT', 'Example Research contact@example.test')

    class Search:
        supports_project_search = True

        def search(self, query, **kwargs):
            return []

    entered, release = threading.Event(), threading.Event()
    quote = '{"val": 130497000000, "unit": "USD", "start": "2024-01-29", "end": "2025-01-26", "filed": "2025-02-26", "form": "10-K"}'

    def fake_identities(req, agent):
        assert agent == 'Example Research contact@example.test'
        return {
            'candidates': [{'cik': '0001045810', 'ticker': 'NVDA', 'name': 'NVIDIA CORP', 'exchange': None}],
            'source_url': 'https://www.sec.gov/files/company_tickers.json',
            'retrieved_at': '2026-01-01T00:00:00Z',
            'response_sha256': 'idhash',
            'limitation': 'current mapping',
        }

    def fake_concept(req, agent):
        assert req.cik == '0001045810' and req.tag == 'Revenues'
        entered.set()
        assert release.wait(10)
        return SecResult(
            url='https://data.sec.gov/api/xbrl/companyconcept/CIK0001045810/us-gaap/Revenues.json',
            title='NVIDIA CORP: Revenues (USD)',
            text=f'SEC XBRL companyconcept\n{quote}',
            response_sha256='secsha256',
        )

    seen_caps = {}

    class Provider:
        name = 'sec-concept-api-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)

        def complete(self, messages, tools, **kwargs):
            results = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
            stage = len(results)
            if stage == 0:
                payload = json.loads(messages[1]['content'])
                seen_caps['value'] = payload['available_capabilities']
                assert 'contact@example.test' not in json.dumps(payload)
                return turn('sec_company_concept', {
                    'entity_query': 'NVDA',
                    'taxonomy': 'us-gaap',
                    'tag': 'Revenues',
                    'unit': 'USD',
                    'period_start': '2024-01-29',
                    'period_end': '2025-01-26',
                    'rationale': 'Get NVDA FY25 SEC XBRL revenue.',
                }, 'sec1')
            if stage == 1:
                assert results[0]['status'] == 'acquired', results[0]
                self.sec_sid = results[0]['source_id']
                return turn('read_sources', {'reads': [{'source_id': self.sec_sid}], 'rationale': 'Read SEC data.'}, 'r1')
            assert quote in results[1]['results'][0]['text']
            return turn('submit_research', {
                'report': 'NVIDIA FY25 revenue was 130,497,000,000 USD per SEC XBRL.',
                'claims': [{
                    'text': 'NVIDIA FY25 revenue was 130,497,000,000 USD.',
                    'source_id': self.sec_sid,
                    'quotation': quote,
                    'role': 'support',
                }],
                'limitations': [],
                'stopping_reason': 'answered',
                'status': 'completed',
            }, 's1')

    monkeypatch.setattr(api, 'evidence_provider', Search())
    monkeypatch.setattr(api, 'challenge_provider', Provider())
    monkeypatch.setattr(api, '_reserve_search_call', lambda *_: None)
    monkeypatch.setattr(api, 'reserve_sec_request', lambda *_: True)
    monkeypatch.setattr(connector, 'search_sec_identities', fake_identities)
    monkeypatch.setattr(connector, 'fetch_sec_concept', fake_concept)

    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test',
                                                        'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'What was NVDA FY25 revenue?'}).json()
    path = '/v2/research/topics/' + topic['topic_id']

    try:
        run_resp = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex},
                               json={'parent_version_id': topic['versions'][0]['version_id']})
        assert run_resp.status_code == 202, run_resp.text
        operation_id = run_resp.json()['operation']['operation_id']
        assert entered.wait(10)
        prog = client.get(path + '/runs/' + operation_id + '/progress', headers=headers)
        assert prog.status_code == 200
        current = prog.json()['current']
        assert current['kind'] == 'tool' and current['name'] == 'sec_company_concept'
        assert current['rationale'] == 'Get NVDA FY25 SEC XBRL revenue.'
        assert current['target'] == 'NVDA · us-gaap:Revenues (USD)'
        assert isinstance(current['started_at'], str) and current['started_at']
    finally:
        release.set()

    for _ in range(200):
        op = client.get(path + '/runs/' + operation_id, headers=headers).json()['operation']
        if op['status'] in ('completed', 'failed', 'cancelled'):
            break
        time.sleep(.01)
    assert op['status'] == 'completed', op
    assert seen_caps['value']['sec_available'] is True

    version = client.get(path, headers=headers).json()['versions'][-1]
    assert version['content']['turns'][-1]['status'] == 'completed'
    assert version['content']['turns'][-1]['official_fetch_status'] == 'completed'
    assert version['content']['turns'][-1]['partial_notes'] is None
    sec_sid = version['content']['claims'][0]['citations'][0]['source_id']
    assert sec_sid in version['source_ids']
    sec_source = client.get(f'/v2/research/sources/{sec_sid}', headers=headers).json()
    assert sec_source['retrieval_method'] == 'sec_companyconcept' and sec_source['kind'] == 'official'
    assert 'contact@example.test' not in json.dumps(sec_source)

    exported = client.get(f"{path}/versions/{version['version_id']}/export", headers=headers)
    assert exported.status_code == 200
    assert exported.json()['source_provenance'][0]['retrieval_method'] == 'sec_companyconcept'
    assert 'contact@example.test' not in exported.text

    # When auto_fetch_official=False, sec_available is False and calling sec_company_concept is denied.
    class DeniedProvider:
        name = 'sec-denied-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)

        def complete(self, messages, tools, **kwargs):
            results = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
            if not results:
                payload = json.loads(messages[1]['content'])
                assert payload['available_capabilities']['sec_available'] is False
                return turn('sec_company_concept', {
                    'entity_query': 'NVDA', 'taxonomy': 'us-gaap', 'tag': 'Revenues', 'unit': 'USD',
                    'period_start': '2024-01-29', 'period_end': '2025-01-26', 'rationale': 'Test gate.',
                }, 'sec_denied')
            assert results[0] == {'status': 'restricted', 'reason': 'permission_denied'}
            return turn('submit_research', {
                'report': 'SEC tool permission denied.', 'claims': [], 'limitations': ['Retrieval disabled'],
                'stopping_reason': 'permission_denied', 'status': 'partial',
            }, 's_denied')

    monkeypatch.setattr(api, 'challenge_provider', DeniedProvider())
    denied_run = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={
        'parent_version_id': version['version_id'], 'question_override': 'Check again', 'auto_fetch_official': False,
    })
    assert denied_run.status_code == 202
    denied_op_id = denied_run.json()['operation']['operation_id']
    for _ in range(200):
        op = client.get(path + '/runs/' + denied_op_id, headers=headers).json()['operation']
        if op['status'] in ('completed', 'failed', 'cancelled'):
            break
        time.sleep(.01)
    assert op['status'] == 'completed'
    denied_turn = client.get(path, headers=headers).json()['versions'][-1]['content']['turns'][-1]
    assert denied_turn['status'] == 'partial' and denied_turn['report'] == ''
    assert denied_turn['official_fetch_status'] == 'not_requested'
    assert denied_turn['partial_notes'] == 'SEC tool permission denied.'


def test_v1_search_candidates_skips_opaque_redirect_lead_while_invalid_keeps_fallback(monkeypatch):
    from datetime import date

    usable = ProviderSearchCandidate(
        'Usable filing',
        'https://example.com/filing',
        'Valid excerpt.',
        'Example',
        date(2026, 8, 1),
        True,
    )
    trailing_opaque_redirect = ProviderSearchCandidate(
        'Opaque Google redirect',
        'https://www.google.com/goto?url=CAESdwHEIkXX&gsessionid=x',
        'Opaque.',
        'Google',
        None,
        False,
    )

    class SearchWithRedirect:
        name = 'redirect-filter-test'
        supports_project_search = True

        def search(self, query, *, limit):
            return [usable, trailing_opaque_redirect]

    monkeypatch.setattr(api, 'evidence_provider', SearchWithRedirect())
    monkeypatch.setattr(api, '_reserve_search_call', lambda *_: None)
    client = TestClient(api.app)

    ok = client.get('/v1/search/candidates', params={'query': 'nvda revenue', 'limit': 5})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body['project_search_available'] is True
    assert body['fallback'] == 'client_bundle'
    assert len(body['candidates']) == 1
    assert body['candidates'][0]['url'] == 'https://example.com/filing'

    class SearchWithMalformed:
        name = 'malformed-test'
        supports_project_search = True

        def search(self, query, *, limit):
            return [usable, {'title': 'Broken', 'url': 'javascript:alert(1)'}]

    monkeypatch.setattr(api, 'evidence_provider', SearchWithMalformed())
    fallback = client.get('/v1/search/candidates', params={'query': 'nvda revenue', 'limit': 5})
    assert fallback.status_code == 200, fallback.text
    assert fallback.json() == {
        'schema_version': 'v1',
        'provider': 'malformed-test',
        'project_search_available': False,
        'fallback': 'client_bundle',
        'candidates': [],
    }


def test_start_research_run_uses_pace_sec_request_and_honors_reserve_override(monkeypatch):
    from datetime import date, datetime, timezone
    import math
    import pytest
    import app.research_official_retrieval as retrieval
    import app.research_structured_connector as connector
    from app.research_sec import SecFilingResult, SecResult

    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setenv('FORESIGHTNINE_SEC_USER_AGENT', 'Example Research contact@example.test')
    monkeypatch.setattr(api, '_reserve_search_call', lambda *_: None)
    assert api.reserve_sec_request is api._DEFAULT_RESERVE_SEC_REQUEST

    # Pre-seed an immediate SEC slot so un-paced reserve_sec_request would return False.
    api.SOURCE_RATE_LIMITS.put('sec', {'requested_at': datetime.now(timezone.utc).isoformat()})

    pace_calls = []

    def fake_pace(repository, *, cancel_event=None, deadline=None, **kwargs):
        pace_calls.append({
            'is_shared_repo': repository is api.SOURCE_RATE_LIMITS,
            'has_cancel_event': cancel_event is not None,
            'deadline': deadline,
        })
        return True

    monkeypatch.setattr(api, 'pace_sec_request', fake_pace)

    filing_url = 'https://www.sec.gov/Archives/edgar/data/1045810/000104581025000001/nvda-20250126.htm'

    class Search:
        supports_project_search = True

        def search(self, query, **kwargs):
            return [ProviderSearchCandidate('NVIDIA Form 10-K', filing_url, 'Annual filing.', 'SEC EDGAR', date(2025, 2, 26), True)]

    rev_quote = '{"val": 130497000000, "unit": "USD", "start": "2024-01-29", "end": "2025-01-26", "filed": "2025-02-26", "form": "10-K"}'
    ni_quote = '{"val": 72880000000, "unit": "USD", "start": "2024-01-29", "end": "2025-01-26", "filed": "2025-02-26", "form": "10-K"}'

    def fake_identities(req, agent):
        return {
            'candidates': [{'cik': '0001045810', 'ticker': 'NVDA', 'name': 'NVIDIA CORP', 'exchange': None}],
            'source_url': 'https://www.sec.gov/files/company_tickers.json',
            'retrieved_at': '2026-01-01T00:00:00Z',
            'response_sha256': 'idhash',
            'limitation': 'current mapping',
        }

    def fake_concept(req, agent):
        quote = rev_quote if req.tag == 'Revenues' else ni_quote
        return SecResult(
            url=f'https://data.sec.gov/api/xbrl/companyconcept/CIK0001045810/us-gaap/{req.tag}.json',
            title=f'NVIDIA CORP: {req.tag} (USD)',
            text=f'SEC XBRL companyconcept\n{quote}',
            response_sha256=f'sha-{req.tag}',
        )

    def fake_filing(req, agent, **kwargs):
        return SecFilingResult('NVIDIA Form 10-K excerpt.', 'sec-filing-sha', 'PGh0bWw+PC9odG1sPg==')

    monkeypatch.setattr(api, 'evidence_provider', Search())
    monkeypatch.setattr(connector, 'search_sec_identities', fake_identities)
    monkeypatch.setattr(connector, 'fetch_sec_concept', fake_concept)
    monkeypatch.setattr(retrieval, 'fetch_sec_filing', fake_filing)

    class PacedProvider:
        name = 'sec-pace-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)

        def complete(self, messages, tools, **kwargs):
            results = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
            stage = len(results)
            if stage == 0:
                return turn('discover_sources', {'query': 'NVDA 10-K', 'rationale': 'Find SEC filing.'}, 'd1')
            if stage == 1:
                return turn('sec_company_concept', {
                    'entity_query': 'NVDA', 'taxonomy': 'us-gaap', 'tag': 'Revenues', 'unit': 'USD',
                    'period_start': '2024-01-29', 'period_end': '2025-01-26', 'rationale': 'Fetch revenue.',
                }, 'sec1')
            if stage == 2:
                assert results[1]['status'] == 'acquired', results[1]
                self.rev_sid = results[1]['source_id']
                return turn('sec_company_concept', {
                    'entity_query': 'NVDA', 'taxonomy': 'us-gaap', 'tag': 'NetIncomeLoss', 'unit': 'USD',
                    'period_start': '2024-01-29', 'period_end': '2025-01-26', 'rationale': 'Fetch net income.',
                }, 'sec2')
            if stage == 3:
                assert results[2]['status'] == 'acquired', results[2]
                return turn('acquire_sources', {'urls': [filing_url], 'rationale': 'Acquire 10-K.'}, 'a1')
            if stage == 4:
                assert results[3]['results'][0]['status'] == 'acquired', results[3]
                return turn('read_sources', {'reads': [{'source_id': self.rev_sid}], 'rationale': 'Read SEC revenue.'}, 'r1')
            return turn('submit_research', {
                'report': 'NVDA FY25 revenue was 130,497,000,000 USD.',
                'claims': [{
                    'text': 'NVDA FY25 revenue was 130,497,000,000 USD.',
                    'source_id': self.rev_sid,
                    'quotation': rev_quote,
                    'role': 'support',
                }],
                'limitations': [],
                'stopping_reason': 'answered',
                'status': 'completed',
            }, 's1')

    monkeypatch.setattr(api, 'challenge_provider', PacedProvider())
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={
        'email': f'{uuid4().hex}@example.test',
        'password': 'correct horse battery staple!',
    }).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'What were NVDA FY25 revenue and net income?'}).json()
    path = '/v2/research/topics/' + topic['topic_id']

    run_resp = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={
        'parent_version_id': topic['versions'][0]['version_id'],
        'max_elapsed_seconds': 60,
    })
    assert run_resp.status_code == 202, run_resp.text
    op_id = run_resp.json()['operation']['operation_id']
    for _ in range(200):
        op = client.get(path + '/runs/' + op_id, headers=headers).json()['operation']
        if op['status'] in ('completed', 'failed', 'cancelled'):
            break
        time.sleep(.01)
    assert op['status'] == 'completed', op
    assert len(pace_calls) == 5  # 2 per sec_company_concept call + 1 for SEC acquire_sources
    assert all(call['is_shared_repo'] and call['has_cancel_event'] and math.isfinite(call['deadline']) for call in pace_calls)

    # Monkeypatching api.reserve_sec_request overrides pace_sec_request for backward compatibility.
    reserve_calls = []

    def fake_reserve(repo):
        reserve_calls.append(repo is api.SOURCE_RATE_LIMITS)
        return len(reserve_calls) == 1

    monkeypatch.setattr(api, 'reserve_sec_request', fake_reserve)
    monkeypatch.setattr(api, 'pace_sec_request', lambda *a, **k: pytest.fail('pace_sec_request must not be called when reserve_sec_request is patched'))

    class ReserveOverrideProvider:
        name = 'sec-reserve-override-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)

        def complete(self, messages, tools, **kwargs):
            results = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
            if not results:
                return turn('sec_company_concept', {
                    'entity_query': 'NVDA', 'taxonomy': 'us-gaap', 'tag': 'Revenues', 'unit': 'USD',
                    'period_start': '2024-01-29', 'period_end': '2025-01-26', 'rationale': 'Verify override.',
                }, 'sec_override')
            assert results[0]['status'] == 'failed' and results[0]['reason'] == 'source_rate_limited', results[0]
            return turn('submit_research', {
                'report': 'SEC request hit monkeypatched reserve limit.',
                'claims': [],
                'limitations': ['source_rate_limited'],
                'stopping_reason': 'source_rate_limited',
                'status': 'partial',
            }, 's_override')

    monkeypatch.setattr(api, 'challenge_provider', ReserveOverrideProvider())
    latest_version_id = client.get(path, headers=headers).json()['versions'][-1]['version_id']
    override_run = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={
        'parent_version_id': latest_version_id,
        'question_override': 'Check override behavior',
    })
    assert override_run.status_code == 202, override_run.text
    override_op_id = override_run.json()['operation']['operation_id']
    for _ in range(200):
        op2 = client.get(path + '/runs/' + override_op_id, headers=headers).json()['operation']
        if op2['status'] in ('completed', 'failed', 'cancelled'):
            break
        time.sleep(.01)
    assert op2['status'] == 'completed', op2
    assert len(reserve_calls) >= 2 and all(reserve_calls)
    override_turn = client.get(path, headers=headers).json()['versions'][-1]['content']['turns'][-1]
    assert override_turn['official_fetch_status'] == 'failed'


def test_sec_company_concept_syncs_official_fetch_status_with_priority_guard(monkeypatch):
    import app.research_official_retrieval as retrieval
    import app.research_structured_connector as connector
    from app.research_sec import SecFilingResult, SecResult

    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setenv('FORESIGHTNINE_SEC_USER_AGENT', 'Example Research contact@example.test')
    monkeypatch.setattr(api, '_reserve_search_call', lambda *_: None)
    monkeypatch.setattr(api, 'reserve_sec_request', lambda *_: True)

    filing_url = 'https://www.sec.gov/Archives/edgar/data/1045810/000104581025000001/nvda-20250126.htm'
    rev_quote = '{"val": 130497000000, "unit": "USD", "start": "2024-01-29", "end": "2025-01-26", "filed": "2025-02-26", "form": "10-K"}'

    class Search:
        supports_project_search = True

        def search(self, query, **kwargs):
            return [ProviderSearchCandidate('NVIDIA Form 10-K', filing_url, 'Annual filing.', 'SEC EDGAR', None, True)]

    def fake_identities(req, agent):
        return {
            'candidates': [{'cik': '0001045810', 'ticker': 'NVDA', 'name': 'NVIDIA CORP', 'exchange': None}],
            'source_url': 'https://www.sec.gov/files/company_tickers.json',
            'retrieved_at': '2026-01-01T00:00:00Z',
            'response_sha256': 'idhash',
            'limitation': 'current mapping',
        }

    def fake_concept(req, agent):
        text = 'SEC XBRL companyconcept\n' if req.tag == 'WrongTag' else f'SEC XBRL companyconcept\n{rev_quote}'
        return SecResult(
            url=f'https://data.sec.gov/api/xbrl/companyconcept/CIK0001045810/us-gaap/{req.tag}.json',
            title=f'NVIDIA CORP: {req.tag} (USD)',
            text=text,
            response_sha256=f'sha-{req.tag}',
        )

    def fake_filing(req, agent, **kwargs):
        return SecFilingResult('NVIDIA Form 10-K revenue 130497.', 'filing-sha', 'PGh0bWw+PC9odG1sPg==')

    monkeypatch.setattr(api, 'evidence_provider', Search())
    monkeypatch.setattr(connector, 'search_sec_identities', fake_identities)
    monkeypatch.setattr(connector, 'fetch_sec_concept', fake_concept)
    monkeypatch.setattr(retrieval, 'fetch_sec_filing', fake_filing)

    # Turn 1: unknown SEC tag first, then valid SEC tag -> official_fetch_status becomes 'completed'.
    class Turn1Provider:
        name = 'sec-status-turn1'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)

        def complete(self, messages, tools, **kwargs):
            results = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
            if len(results) == 0:
                return turn('sec_company_concept', {
                    'entity_query': 'NVDA', 'taxonomy': 'us-gaap', 'tag': 'WrongTag', 'unit': 'USD',
                    'period_start': '2024-01-29', 'period_end': '2025-01-26', 'rationale': 'First guess.',
                }, 's1')
            if len(results) == 1:
                assert results[0]['status'] == 'unknown'
                return turn('sec_company_concept', {
                    'entity_query': 'NVDA', 'taxonomy': 'us-gaap', 'tag': 'Revenues', 'unit': 'USD',
                    'period_start': '2024-01-29', 'period_end': '2025-01-26', 'rationale': 'Correct tag.',
                }, 's2')
            if len(results) == 2:
                self.sec_sid = results[1]['source_id']
                return turn('read_sources', {'reads': [{'source_id': self.sec_sid}], 'rationale': 'Read.'}, 'r1')
            return turn('submit_research', {
                'report': 'NVDA FY25 revenue was 130,497,000,000 USD.',
                'claims': [{'text': 'Revenue was 130,497,000,000 USD.', 'source_id': self.sec_sid, 'quotation': rev_quote, 'role': 'support'}],
                'limitations': [], 'stopping_reason': 'answered', 'status': 'completed',
            }, 'sub1')

    monkeypatch.setattr(api, 'challenge_provider', Turn1Provider())
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'NVDA FY25 revenue?'}).json()
    path = '/v2/research/topics/' + topic['topic_id']

    run1 = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={
        'parent_version_id': topic['versions'][0]['version_id'],
    })
    assert run1.status_code == 202
    op1 = run1.json()['operation']['operation_id']
    for _ in range(200):
        status = client.get(path + '/runs/' + op1, headers=headers).json()['operation']['status']
        if status in ('completed', 'failed', 'cancelled'):
            break
        time.sleep(.01)
    assert status == 'completed'
    v1 = client.get(path, headers=headers).json()['versions'][-1]
    assert v1['content']['turns'][-1]['official_fetch_status'] == 'completed'

    # Turn 2: acquire_sources succeeds ('completed'), then sec_company_concept returns 'unknown' -> keeps 'completed'.
    class Turn2Provider:
        name = 'sec-status-turn2'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)

        def complete(self, messages, tools, **kwargs):
            results = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
            if len(results) == 0:
                return turn('discover_sources', {'query': 'NVDA 10-K', 'rationale': 'Find 10-K.'}, 'd1')
            if len(results) == 1:
                return turn('acquire_sources', {'urls': [filing_url], 'rationale': 'Acquire 10-K.'}, 'a1')
            if len(results) == 2:
                self.filing_sid = results[1]['results'][0]['source_id']
                return turn('sec_company_concept', {
                    'entity_query': 'NVDA', 'taxonomy': 'us-gaap', 'tag': 'WrongTag', 'unit': 'USD',
                    'period_start': '2024-01-29', 'period_end': '2025-01-26', 'rationale': 'Check concept.',
                }, 's_miss')
            if len(results) == 3:
                assert results[2]['status'] == 'unknown'
                return turn('read_sources', {'reads': [{'source_id': self.filing_sid}], 'rationale': 'Read 10-K.'}, 'r2')
            return turn('submit_research', {
                'report': '10-K reports 130497.',
                'claims': [{'text': '10-K reports 130497.', 'source_id': self.filing_sid, 'quotation': 'NVIDIA Form 10-K revenue 130497.', 'role': 'support'}],
                'limitations': [], 'stopping_reason': 'answered', 'status': 'completed',
            }, 'sub2')

    monkeypatch.setattr(api, 'challenge_provider', Turn2Provider())
    run2 = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={
        'parent_version_id': v1['version_id'],
        'question_override': 'Check 10-K filing.',
    })
    assert run2.status_code == 202
    op2 = run2.json()['operation']['operation_id']
    for _ in range(200):
        status = client.get(path + '/runs/' + op2, headers=headers).json()['operation']['status']
        if status in ('completed', 'failed', 'cancelled'):
            break
        time.sleep(.01)
    assert status == 'completed'
    v2 = client.get(path, headers=headers).json()['versions'][-1]
    assert v2['content']['turns'][-1]['official_fetch_status'] == 'completed'
