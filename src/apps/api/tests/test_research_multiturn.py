"""Multi-turn research: prior state as agent input, merge into versions, and user edits."""
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as api
from app.phase3 import ProviderFailure, ProviderPolicy
from app.research_runtime import GAP_CODES, run_research_agent
from app.model_protocol import ModelResponse
from tests.test_research_runtime import Model, context, source, turn

TEXT = 'Revenue was 100 USD. Revenue was 80 USD. Costs were 60 USD.'
STOP = ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')
Q1, Q2 = 'Revenue?', 'How much did revenue grow?'
FACT_ARGS = {'unit': 'USD', 'currency': 'USD', 'label': 'revenue'}


def tool_results(messages):
    return [json.loads(m['content']) for m in messages if m['role'] == 'tool']


def prior_of(messages):
    return json.loads(messages[1]['content']).get('prior_research')


class Scripted:
    """Chat-completions fake driven by one callable per model call."""
    name = 'multiturn-test'
    policy = ProviderPolicy(max_attempts=1, max_output_tokens=1000, timeout_seconds=5)

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = 0

    def complete(self, messages, tools, **kwargs):
        step = self.steps[self.calls]
        self.calls += 1
        return step(messages, kwargs)


def read_all(source_id):
    return lambda messages, kwargs: turn('read_sources', {'reads': [{'source_id': source_id}], 'rationale': 'read'}, 'read')


def record_revenue(source_id):
    def step(messages, kwargs):
        return turn('record_facts', {'rationale': 'record', 'facts': [
            {**FACT_ARGS, 'value': 100, 'period': '2025', 'source_id': source_id, 'quotation': 'Revenue was 100 USD.'},
            {**FACT_ARGS, 'value': 80, 'period': '2024', 'source_id': source_id, 'quotation': 'Revenue was 80 USD.'}]}, 'facts')
    return step


def submit(source_id, *, role='support', quotation='Revenue was 100 USD.', status='completed', claims=True, **extra):
    claim = {'text': 'Revenue was ' + quotation, 'source_id': source_id, 'quotation': quotation, 'role': role, **extra}
    def step(messages, kwargs):
        return turn('submit_research', {'report': 'Answer.', 'claims': [claim] if claims else [], 'limitations': [],
                                        'stopping_reason': 'answered', 'status': status}, 'submit')
    return step


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test',
                       'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={'title': 'Report', 'text': TEXT,
        'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True}}).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': Q1, 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']

    def install(provider):
        monkeypatch.setattr(api, 'challenge_provider', provider)

    def wait(operation_id):
        for _ in range(300):
            operation = client.get(f'{path}/runs/{operation_id}', headers=headers).json()['operation']
            if operation['status'] in ('completed', 'failed', 'cancelled'):
                return operation
            time.sleep(.01)
        raise AssertionError('run did not finish')

    def latest():
        return client.get(path, headers=headers).json()['versions'][-1]

    def start(body=None):
        body = {'parent_version_id': latest()['version_id'], 'discover_sources': False, **(body or {})}
        return client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex}, json=body)

    def run(body=None):
        response = start(body)
        assert response.status_code == 202, response.text
        operation_id = response.json()['operation']['operation_id']
        assert wait(operation_id)['status'] == 'completed'
        return operation_id

    return SimpleNamespace(client=client, headers=headers, path=path, source_id=source_id, topic=topic,
                           install=install, wait=wait, latest=latest, start=start, run=run)


def test_two_turns_accumulate_and_second_turn_calculates_from_prior_fact_ids(env):
    sid = env.source_id
    seen = {}

    def calculate(messages, kwargs):
        prior = prior_of(messages)
        seen['prior'] = prior
        ids = {fact['period']: fact['fact_id'] for fact in prior['facts']}
        return turn('calculate_financials', {'rationale': 'growth', 'calculations': [
            {'formula': 'growth_percent', 'first_fact_id': ids['2025'], 'second_fact_id': ids['2024']}]}, 'calc')

    def explain(messages, kwargs):
        assert tool_results(messages)[-1]['calculations'][0]['value'] == '25.00'
        return turn('submit_research', {'report': 'Revenue grew 25.00%.', 'claims': [], 'limitations': [],
                                        'stopping_reason': 'answered'}, 'submit')

    provider = Scripted(read_all(sid), record_revenue(sid), submit(sid), calculate, explain)
    env.install(provider)
    env.run()
    first = env.latest()
    assert first['content']['summary'] == '' and len(first['content']['turns']) == 1
    env.run({'question_override': Q2})
    version = env.latest()
    content = version['content']
    assert provider.calls == 5
    assert seen['prior']['note'].startswith('Prior records')
    assert [t['index'] for t in content['turns']] == [1, 2]
    assert [t['question'] for t in content['turns']] == [Q1, Q2]
    assert content['turns'][0]['question_origin'] == content['turns'][1]['question_origin'] == 'user'
    assert len(content['facts']) == 2 and len(content['calculations']) == 1
    turn1, turn2 = content['turns']
    assert content['calculations'][0]['turn_id'] == turn2['turn_id'] and turn2['calculation_ids'] == [content['calculations'][0]['calculation_id']]
    assert turn2['fact_ids'] == [] and turn2['claim_ids'] == []
    assert turn1['fact_ids'] == [fact['fact_id'] for fact in content['facts']]
    assert content['calculations'][0]['inputs'][0]['fact_id'] == content['facts'][0]['fact_id']
    # Agent calculations carry their input check when saved, not only after a later user save.
    assert content['calculations'][0]['input_status'] == 'matched'
    assert content['claims'][0]['turn_id'] == turn1['turn_id'] == first['content']['claims'][0]['turn_id']
    assert content['claims'] == first['content']['claims']
    assert turn2['report'] == 'Revenue grew 25.00%.' and turn2['status'] == 'completed'
    assert turn2['parent_version_id'] == first['version_id']
    assert turn1['usage']['model_calls'] == 3 and turn2['usage']['model_calls'] == 2


def test_follow_up_requires_a_question_and_inputs_expose_plain_questions(env):
    sid = env.source_id
    env.install(Scripted(read_all(sid), record_revenue(sid), submit(sid)))
    first_id = env.run()
    assert env.client.get(f'{env.path}/runs/{first_id}/inputs', headers=env.headers).json()['question'] == Q1
    refused = env.start()
    assert refused.status_code == 422 and 'follow-up question is required' in json.dumps(refused.json())
    env.install(Scripted(submit(sid, claims=False)))
    second_id = env.run({'question_override': Q2})
    inputs = env.client.get(f'{env.path}/runs/{second_id}/inputs', headers=env.headers).json()
    assert inputs['question'] == Q2 and inputs['turn']['index'] == 2
    assert 'question_sha256' in inputs


def test_counterargument_challenges_a_prior_claim_without_changing_it(env):
    sid = env.source_id
    seen = {}

    def bad_challenge(messages, kwargs):
        seen['claim_id'] = prior_of(messages)['claims'][0]['claim_id']
        return submit(sid, role='counterargument', quotation='Costs were 60 USD.', challenges_claim_ids=['claim-unknown'])(messages, kwargs)

    def good_challenge(messages, kwargs):
        assert tool_results(messages)[-1]['status'] == 'invalid'
        return submit(sid, role='counterargument', quotation='Costs were 60 USD.',
                      challenges_claim_ids=[seen['claim_id']])(messages, kwargs)

    env.install(Scripted(read_all(sid), record_revenue(sid), submit(sid), bad_challenge, good_challenge))
    env.run()
    before = env.latest()['content']['claims'][0]
    env.run({'question_override': 'Find opposing evidence.'})
    claims = env.latest()['content']['claims']
    assert len(claims) == 2 and claims[0] == before
    assert json.dumps(claims[0], sort_keys=True) == json.dumps(before, sort_keys=True)
    assert claims[1]['role'] == 'counterargument' and claims[1]['challenges_claim_ids'] == [before['claim_id']]
    assert claims[1]['citations'][0]['quotation'] == 'Costs were 60 USD.'  # located through prior read ranges


def test_failed_second_turn_continues_in_place_without_duplicates(env, monkeypatch):
    # Manual continuation is under test, so automatic pre-send retries are turned off.
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRIES', '0')
    sid = env.source_id

    def record_costs(messages, kwargs):
        return turn('record_facts', {'rationale': 'costs', 'facts': [{**FACT_ARGS, 'label': 'costs', 'value': 60, 'period': '2025',
                    'source_id': sid, 'quotation': 'Costs were 60 USD.'}]}, 'costs')

    def transport_failure(messages, kwargs):
        kwargs['diagnostic_callback']({'submission_state': 'not_started', 'stages': [{'stage': 'tls', 'result': 'error', 'elapsed_ms': 1}]})
        raise ProviderFailure('provider_transport_error', 'connection failed', retryable=True)

    provider = Scripted(read_all(sid), record_revenue(sid), submit(sid), record_costs, transport_failure,
                        submit(sid, quotation='Costs were 60 USD.', role='counterargument'))
    env.install(provider)
    env.run()
    second_id = env.run({'question_override': 'Compare costs.'})
    partial = env.latest()
    turn2 = partial['content']['turns'][-1]
    assert turn2['status'] == 'partial' and turn2['reason'] == 'provider_transport_error'
    assert [gap['code'] for gap in turn2['gaps']].count('provider_error') == 1
    assert len(turn2['fact_ids']) == 1 and len(partial['content']['facts']) == 3
    status = env.client.get(f'{env.path}/runs/{second_id}/recovery-status', headers=env.headers).json()
    assert status['can_continue'] and status['version_id'] == partial['version_id']
    continued = env.client.post(f'{env.path}/runs/{second_id}/continue', headers={**env.headers, 'Idempotency-Key': uuid4().hex}, json={})
    assert continued.status_code == 202, continued.text
    third_id = continued.json()['operation']['operation_id']
    assert env.wait(third_id)['status'] == 'completed'
    final = env.latest()['content']
    assert len(final['turns']) == 2
    turn2 = final['turns'][-1]
    assert turn2['operation_ids'] == [second_id, third_id]
    assert turn2['status'] == 'completed' and turn2['index'] == 2 and turn2['question'] == 'Compare costs.'
    assert len(final['facts']) == 3 and len({f['fact_id'] for f in final['facts']}) == 3
    assert len(final['claims']) == 2 and turn2['claim_ids'] == [final['claims'][1]['claim_id']]
    assert turn2['fact_ids'] == [final['facts'][2]['fact_id']]
    assert turn2['usage']['model_calls'] == 2  # The failed request never reached the provider and is not counted.
    assert sum(1 for event in turn2['events'] if event['kind'] == 'submission') == 1
    assert final['gaps'] == [g for g in turn2['gaps']]
    assert provider.calls == 6


def test_explanation_only_follow_up_completes_without_new_claims(env):
    sid = env.source_id
    env.install(Scripted(read_all(sid), submit(sid), submit(sid, claims=False)))
    env.run()
    env.run({'question_override': 'Explain the answer.'})
    content = env.latest()['content']
    assert content['turns'][-1]['status'] == 'completed' and len(content['claims']) == 1
    assert content['turns'][-1]['claim_ids'] == []


def test_completed_first_turn_without_any_cited_claim_is_refused(env):
    sid = env.source_id
    provider = Scripted(submit(sid, claims=False), submit(sid, claims=False, status='partial'))
    env.install(provider)
    env.run()
    turn1 = env.latest()['content']['turns'][0]
    assert turn1['status'] == 'partial' and turn1['reason'] == 'submitted'
    assert 'no_cited_claims' in [gap['code'] for gap in turn1['gaps']]


def test_gap_codes_for_uncited_partial_and_model_reported_limitations(env):
    sid = env.source_id

    def partial(messages, kwargs):
        return turn('submit_research', {'report': 'Not answered.', 'claims': [], 'limitations': ['Only one filing was supplied.'],
                                        'stopping_reason': 'out of material', 'status': 'partial'}, 'submit')
    env.install(Scripted(partial))
    env.run()
    content = env.latest()['content']
    gaps = content['turns'][0]['gaps']
    assert [gap['code'] for gap in gaps][:2] == ['no_cited_claims', 'model_reported']
    assert gaps[0]['message'] == GAP_CODES['no_cited_claims']
    assert gaps[1]['message'] == 'Only one filing was supplied.'
    assert content['gaps'] == gaps  # Open gaps are the latest turn's gaps.
    assert content['turns'][0]['stopping_note'] == 'out of material'
    assert all('semantic support' not in gap['message'] for gap in content['gaps'])
    assert 'source_selection_partial' in {gap['code'] for gap in gaps}  # No source text was read.


def test_prior_research_truncation_in_agent_input():
    facts = [{'fact_id': f'fact-{i}', 'label': 'revenue', 'value': str(i), 'unit': 'USD', 'currency': 'USD', 'period': str(i),
              'source_id': 's1', 'quotation': 'q' * 400, 'character_start': 0, 'character_end': 20} for i in range(205)]
    claims = [{'claim_id': f'claim-{i}', 'role': 'support', 'text': 't' * 600, 'challenges_claim_ids': [],
               'citations': [{'source_id': 's1', 'quotation': 'q' * 400, 'location': {'character_start': 0, 'character_end': 20}}]}
              for i in range(101)]
    saved = []
    src = source()
    src.content_sha256 = 'h'
    run_research_agent(question='Q2', sources=[src], model=Model([STOP]), context=context(), checkpoint=lambda s: saved.append(json.loads(json.dumps(s))),
        prior={'facts': facts, 'calculations': [], 'claims': claims, 'gaps': [{'code': 'no_progress', 'message': 'x'}],
               'read_ranges': [{'source_id': 's1', 'character_start': 0, 'character_end': 20},
                               {'source_id': 'gone', 'character_start': 0, 'character_end': 5}],
               'source_hashes': {'s1': 'h'}, 'review': 'confirmed', 'note': 'n' * 1500, 'summary': 's' * 3000})
    prior = json.loads(saved[0]['user'])['prior_research']
    assert len(prior['facts']) == 200 and prior['facts'][0]['fact_id'] == 'fact-5' and prior['facts'][-1]['fact_id'] == 'fact-204'
    assert prior['truncated'] == {'facts': 5, 'calculations': 0, 'claims': 1}
    assert len(prior['facts'][0]['quotation']) == 300 and len(prior['claims'][0]['text']) == 500
    assert len(prior['claims'][0]['quotation']) == 300 and len(prior['claims']) == 100
    assert prior['user_review'] == {'review': 'confirmed', 'note': 'n' * 1000, 'summary': 's' * 2000}
    assert prior['open_gaps'] == [{'code': 'no_progress', 'message': 'x'}]
    assert prior['read_ranges'] == [{'source_id': 's1', 'character_start': 0, 'character_end': 20}]
    assert saved[0]['prior_read_ranges'] == [{'source_id': 's1', 'start': 0, 'end': 20}]
    assert len(saved[0]['prior_claim_ids']) == 101  # Every prior claim stays challengeable.


def test_prior_read_ranges_require_unchanged_content_and_no_truncation_key_when_small():
    src = source()
    src.content_sha256 = 'new-hash'
    saved = []
    run_research_agent(question='Q', sources=[src], model=Model([STOP]), context=context(), checkpoint=lambda s: saved.append(json.loads(json.dumps(s))),
        prior={'facts': [], 'calculations': [], 'claims': [], 'gaps': [], 'source_hashes': {'s1': 'old-hash'},
               'read_ranges': [{'source_id': 's1', 'character_start': 0, 'character_end': 20}]})
    prior = json.loads(saved[0]['user'])['prior_research']
    assert prior['read_ranges'] == [] and 'truncated' not in prior and saved[0]['prior_read_ranges'] == []


def test_completion_counts_prior_claims_and_prior_facts_resolve_for_calculation():
    prior = {'facts': [
        {'fact_id': 'fact-a', 'label': 'revenue', 'value': '100', 'unit': 'USD', 'currency': 'USD', 'period': '2025', 'source_id': 's1',
         'quotation': 'Revenue was 100 USD.', 'character_start': 0, 'character_end': 20},
        {'fact_id': 'fact-b', 'label': 'revenue', 'value': '80', 'unit': 'USD', 'currency': 'USD', 'period': '2024', 'source_id': 's1',
         'quotation': 'Revenue was 80 USD.', 'character_start': 21, 'character_end': 40}],
        'calculations': [], 'gaps': [], 'source_hashes': {}, 'read_ranges': [],
        'claims': [{'claim_id': 'claim-old', 'role': 'support', 'text': 'x', 'challenges_claim_ids': [],
                    'citations': [{'source_id': 's1', 'quotation': 'Revenue was 100 USD.', 'location': {'character_start': 0, 'character_end': 20}}]}]}
    src = source()
    src.text = 'Revenue was 100 USD. Revenue was 80 USD.'
    m = Model([turn('calculate_financials', {'rationale': 'g', 'calculations': [
                    {'formula': 'growth_percent', 'first_fact_id': 'fact-a', 'second_fact_id': 'fact-b'}]}, 'a'),
               turn('submit_research', {'report': 'Grew 25%.', 'claims': [], 'limitations': [], 'stopping_reason': 'answered'}, 'b')])
    result = run_research_agent(question='Q2', sources=[src], model=m, context=context(), prior=prior)
    assert result.status == 'completed' and result.facts == [] and result.claims == []
    assert result.calculations[0]['value'] == '25.00' and result.reason == 'submitted'
    assert result.stopping_note == 'answered'
    prior['claims'] = []
    m = Model([turn('submit_research', {'report': 'Grew 25%.', 'claims': [], 'limitations': [], 'stopping_reason': 'answered'}, 'b'),
               STOP])
    result = run_research_agent(question='Q2', sources=[src], model=m, context=context(), prior=prior)
    assert result.status == 'partial' and 'At least one source-backed claim' in m.inputs[1][-1]['content']


def test_user_edits_mark_claims_and_protect_server_records(env):
    sid = env.source_id
    env.install(Scripted(read_all(sid), submit(sid)))
    env.run()
    version = env.latest()
    content = version['content']
    claim_id = content['claims'][0]['claim_id']

    def save(edited, parent=None):
        return env.client.post(env.path + '/versions', headers=env.headers,
                               json={'parent_version_id': (parent or env.latest())['version_id'], 'content': edited})

    edited = json.loads(json.dumps(content))
    edited['claims'][0]['text'] = 'Revenue was 100 USD, as stated.'
    saved = save(edited)
    assert saved.status_code == 201, saved.text
    claim = saved.json()['versions'][-1]['content']['claims'][0]
    assert claim['edited_by_user'] is True and claim['claim_id'] == claim_id and claim['turn_id'] == content['turns'][0]['turn_id']
    # Unchanged claims keep their agent attribution.
    unchanged = save(json.loads(json.dumps(saved.json()['versions'][-1]['content'])))
    assert unchanged.status_code == 201
    # Server-produced records are immutable.
    for field, value in (('turns', []), ('sources', []), ('source_candidates', [{'title': 'x', 'url': 'https://example.com'}]),
                         ('gaps', [{'code': 'made_up', 'message': 'x'}])):
        forbidden = json.loads(json.dumps(env.latest()['content']))
        forbidden[field] = value
        response = save(forbidden)
        assert response.status_code == 422 and 'Server-produced research records cannot be edited.' in response.text, field
    tampered = json.loads(json.dumps(env.latest()['content']))
    tampered['turns'][0]['report'] = 'Rewritten.'
    assert save(tampered).status_code == 422
    # A new user-authored claim receives a server ID and no turn.
    added = json.loads(json.dumps(env.latest()['content']))
    added['claims'].append({'role': 'counterargument', 'text': 'Costs are high.', 'challenges_claim_ids': [claim_id], 'citations': [
        {'source_id': sid, 'quotation': 'Costs were 60 USD.', 'location': {'character_start': 41, 'character_end': 59}}]})
    response = save(added)
    assert response.status_code == 201, response.text
    new = response.json()['versions'][-1]['content']['claims'][-1]
    assert new['claim_id'].startswith('claim-') and new['turn_id'] is None and new['edited_by_user'] is True
    assert new['challenges_claim_ids'] == [claim_id]
    # A citation whose location does not match its quotation is refused; removing claims is allowed.
    broken = json.loads(json.dumps(env.latest()['content']))
    broken['claims'][-1]['citations'][0]['location'] = {'character_start': 0, 'character_end': 10}
    assert save(broken).status_code == 422
    dangling = json.loads(json.dumps(env.latest()['content']))
    dangling['claims'][-1]['challenges_claim_ids'] = ['claim-missing']
    assert save(dangling).status_code == 422
    removed = json.loads(json.dumps(env.latest()['content']))
    removed['claims'] = []
    assert save(removed).status_code == 201


def test_export_contains_every_turn_with_its_question(env):
    sid = env.source_id
    env.install(Scripted(read_all(sid), submit(sid), submit(sid, claims=False)))
    env.run()
    env.run({'question_override': Q2})
    version = env.latest()
    exported = env.client.get(f"{env.path}/versions/{version['version_id']}/export", headers=env.headers)
    assert exported.status_code == 200, exported.text
    turns = exported.json()['version']['content']['turns']
    assert [(t['index'], t['question']) for t in turns] == [(1, Q1), (2, Q2)]
    metadata = env.client.get(f"{env.path}/versions/{version['version_id']}/export/metadata", headers=env.headers).json()
    assert metadata['turn_count'] == 2 and metadata['claim_count'] == 1


def test_confirmations_report_missing_table_metadata(env):
    sid = env.source_id
    env.install(Scripted(read_all(sid), record_revenue(sid), submit(sid)))
    env.run()
    version = env.latest()
    for route in ('confirm-direction', 'confirm-table'):
        response = env.client.post(f"{env.path}/facts/{route}", headers=env.headers, json={
            'parent_version_id': version['version_id'], 'fact_id': version['content']['facts'][0]['fact_id']})
        assert response.status_code == 422 and 'no table metadata' in response.text


def test_focus_citation_names_a_saved_claim(env):
    sid = env.source_id
    env.install(Scripted(read_all(sid), submit(sid), submit(sid, claims=False)))
    env.run()
    claim_id = env.latest()['content']['claims'][0]['claim_id']
    assert env.start({'question_override': Q2, 'focus_citation': {'claim_id': 'claim-missing', 'citation_index': 0}}).status_code == 422
    assert env.start({'question_override': Q2, 'focus_citation': {'claim_id': claim_id, 'citation_index': 3}}).status_code == 422
    env.run({'question_override': Q2, 'focus_citation': {'claim_id': claim_id, 'citation_index': 0}})
    assert env.latest()['content']['turns'][-1]['focus_citation'] == {'claim_id': claim_id, 'citation_index': 0}


def test_delivering_a_saved_partial_submission_replaces_its_turn(env):
    sid = env.source_id
    provider = Scripted(read_all(sid), submit(sid, status='partial'))
    env.install(provider)
    first_id = env.run()
    saved = env.latest()
    assert saved['content']['turns'][0]['status'] == 'partial' and len(saved['content']['claims']) == 1
    status = env.client.get(f'{env.path}/runs/{first_id}/recovery-status', headers=env.headers).json()
    assert status['can_deliver'] and not status['can_continue']
    delivered = env.client.post(f'{env.path}/runs/{first_id}/recover', headers={**env.headers, 'Idempotency-Key': uuid4().hex}, json={})
    assert delivered.status_code == 202, delivered.text
    second_id = delivered.json()['operation']['operation_id']
    assert env.wait(second_id)['status'] == 'completed'
    content = env.latest()['content']
    assert provider.calls == 2 and len(content['turns']) == 1
    assert content['turns'][0]['operation_ids'] == [first_id, second_id]
    assert len(content['claims']) == 1 and content['turns'][0]['claim_ids'] == [content['claims'][0]['claim_id']]
