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


def followup_suggested(messages, kwargs):
    draft = tool_results(messages)[-1]
    assert draft['reason'] == 'core_evidence_path_available'
    action = draft['next_action']
    assert action['tool'] == 'search_library'
    return turn(action['tool'], action['arguments'], 'followup-search')


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

    def final_challenge(messages, kwargs):
        return submit(sid, role='counterargument', quotation='Costs were 60 USD.',
                      challenges_claim_ids=[seen['claim_id']])(messages, kwargs)

    env.install(Scripted(read_all(sid), record_revenue(sid), submit(sid), bad_challenge, good_challenge,
                         followup_suggested, read_all(sid), final_challenge))
    env.run()
    before = env.latest()['content']['claims'][0]
    env.run({'question_override': 'Find opposing evidence.'})
    claims = env.latest()['content']['claims']
    assert len(claims) == 2 and claims[0] == before
    assert json.dumps(claims[0], sort_keys=True) == json.dumps(before, sort_keys=True)
    assert claims[1]['role'] == 'counterargument' and claims[1]['challenges_claim_ids'] == [before['claim_id']]
    assert claims[1]['citations'][0]['quotation'] == 'Costs were 60 USD.'  # located through prior read ranges


def dispatch_settled_retry(operation_id):
    """Advance directly to the saved scheduling deadline, without a real backoff."""
    from datetime import datetime
    work = api.RESEARCH_WORK[operation_id]
    api._reconcile_research_attempt(operation_id, work, now=datetime.fromisoformat(work['resume_after']))
    return api.RESEARCH_RUN_INPUTS[operation_id]['runtime_continuation_operation_id']


def test_failed_second_turn_continues_in_place_without_duplicates(env, monkeypatch):
    # Exercise the durable handoff after a known-unsent failure.
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRIES', '0')
    sid = env.source_id

    def record_costs(messages, kwargs):
        return turn('record_facts', {'rationale': 'costs', 'facts': [{**FACT_ARGS, 'label': 'costs', 'value': 60, 'period': '2025',
                    'source_id': sid, 'quotation': 'Costs were 60 USD.'}]}, 'costs')

    def transport_failure(messages, kwargs):
        kwargs['diagnostic_callback']({'submission_state': 'not_started', 'stages': [{'stage': 'tls', 'result': 'error', 'elapsed_ms': 1}]})
        raise ProviderFailure('provider_transport_error', 'connection failed', retryable=True)

    costs = submit(sid, quotation='Costs were 60 USD.', role='counterargument')
    provider = Scripted(read_all(sid), record_revenue(sid), submit(sid), record_costs, transport_failure,
                        costs, followup_suggested, read_all(sid), costs)
    env.install(provider)
    env.run()
    second_id = env.start({'question_override': 'Check costs.'}).json()['operation']['operation_id']
    assert env.wait(second_id)['status'] == 'failed'
    progress = env.client.get(f'{env.path}/runs/{second_id}/progress', headers=env.headers).json()
    assert progress['logical_status'] == 'running' and len(env.latest()['content']['turns']) == 1
    checkpoint = api.RESEARCH_RUN_INPUTS[second_id]['runtime_checkpoint']
    assert any(event.get('external_state') == 'not_started' for event in checkpoint['events'])
    assert len(checkpoint['facts']) == 1
    third_id = dispatch_settled_retry(second_id)
    assert env.wait(third_id)['status'] == 'completed'
    final = env.latest()['content']
    assert len(final['turns']) == 2
    turn2 = final['turns'][-1]
    assert turn2['operation_ids'] == [second_id, third_id]
    # The synthetic claim prefixes a cost quote with 'Revenue was'; its relationship
    # to costs is not established, although the recorded cost fact is retained.
    assert turn2['status'] == 'partial' and turn2['index'] == 2 and turn2['question'] == 'Check costs.'
    assert turn2['gaps']
    assert len(final['facts']) == 3 and len({f['fact_id'] for f in final['facts']}) == 3
    assert len(final['claims']) == 2 and turn2['claim_ids'] == [final['claims'][1]['claim_id']]
    assert turn2['fact_ids'] == [final['facts'][2]['fact_id']]
    assert turn2['usage']['model_calls'] == 5  # The failed request never reached the provider and is not counted.
    assert sum(1 for event in turn2['events'] if event['kind'] == 'submission') == 1
    assert final['gaps'] == [g for g in turn2['gaps']]
    assert provider.calls == 9


def test_continuation_works_when_run_inputs_come_back_without_null_fields(env, monkeypatch):
    """PostgreSQL returns stored documents without null-valued keys (storage._drop_legacy_null_fields).

    Comparing those with freshly built values made every continuation look changed: recovery said
    `changed` for topics with sources and admission answered 409 (363/364 real acceptance).
    """
    from copy import deepcopy
    from app.storage import _drop_legacy_null_fields
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRIES', '0')
    repository = api.RESEARCH_RUN_INPUTS
    original_get = repository.get
    monkeypatch.setattr(repository, 'get', lambda key, default=None: _drop_legacy_null_fields(deepcopy(original_get(key, default))))
    sid = env.source_id

    def transport_failure(messages, kwargs):
        kwargs['diagnostic_callback']({'submission_state': 'not_started', 'stages': [{'stage': 'tls', 'result': 'timeout', 'elapsed_ms': 1}]})
        raise ProviderFailure('provider_timeout', 'connection timed out', retryable=True)

    env.install(Scripted(read_all(sid), transport_failure, submit(sid)))
    first_id = env.start().json()['operation']['operation_id']
    assert env.wait(first_id)['status'] == 'failed'
    assert env.latest()['content']['turns'] == []
    status = env.client.get(f'{env.path}/runs/{first_id}/recovery-status', headers=env.headers).json()
    assert status['can_continue'], status
    assert env.wait(dispatch_settled_retry(first_id))['status'] == 'completed'
    assert env.latest()['content']['turns'][0]['status'] == 'completed'


def test_continuation_works_when_failure_precedes_any_model_usage_and_null_fields_dropped(env, monkeypatch):
    """When an attempt fails before any provider usage is recorded, `_accounting['usage']` is None and stripped by PostgreSQL read normalization."""
    from copy import deepcopy
    from app.storage import _drop_legacy_null_fields
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRIES', '0')
    repository = api.RESEARCH_RUN_INPUTS
    original_get = repository.get
    monkeypatch.setattr(repository, 'get', lambda key, default=None: _drop_legacy_null_fields(deepcopy(original_get(key, default))))
    sid = env.source_id

    def immediate_failure(messages, kwargs):
        kwargs['diagnostic_callback']({'submission_state': 'not_started', 'stages': [{'stage': 'tls', 'result': 'timeout', 'elapsed_ms': 1}]})
        raise ProviderFailure('provider_timeout', 'connection timed out', retryable=True)

    env.install(Scripted(immediate_failure, read_all(sid), submit(sid)))
    first_id = env.start().json()['operation']['operation_id']
    assert env.wait(first_id)['status'] == 'failed'
    assert env.latest()['content']['turns'] == []
    status = env.client.get(f'{env.path}/runs/{first_id}/recovery-status', headers=env.headers).json()
    assert status['can_continue'], status
    assert env.wait(dispatch_settled_retry(first_id))['status'] == 'completed'
    assert env.latest()['content']['turns'][0]['status'] == 'completed'


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
    provider = Scripted(submit(sid, claims=False), submit(sid, claims=False, status='partial'),
                        followup_suggested, read_all(sid), submit(sid, claims=False, status='partial'))
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
    env.install(Scripted(partial, followup_suggested, read_all(sid), partial))
    env.run()
    content = env.latest()['content']
    gaps = content['turns'][0]['gaps']
    assert [gap['code'] for gap in gaps][:2] == ['no_cited_claims', 'model_reported']
    assert gaps[0]['message'] == GAP_CODES['no_cited_claims']
    assert gaps[1]['message'] == 'Only one filing was supplied.'
    assert content['gaps'] == gaps  # Open gaps are the latest turn's gaps.
    assert content['turns'][0]['stopping_note'] == 'out of material'
    assert all('semantic support' not in gap['message'] for gap in content['gaps'])
    assert any(event.get('name') == 'read_sources' for event in content['turns'][0]['events'])


def test_prior_research_truncation_in_agent_input():
    from app.research_runtime import PRIOR_RESEARCH_TOKEN_BUDGET, _prior_research, _prior_tokens

    facts = [{'fact_id': f'fact-{i}', 'label': 'revenue', 'value': str(i), 'unit': 'USD', 'currency': 'USD', 'period': str(i),
              'source_id': 's1', 'quotation': 'q' * 400, 'character_start': 0, 'character_end': 20} for i in range(205)]
    claims = [{'claim_id': f'claim-{i}', 'role': 'support', 'text': 't' * 600, 'challenges_claim_ids': [],
               'citations': [{'source_id': 's1', 'quotation': 'q' * 400, 'location': {'character_start': 0, 'character_end': 20}}]}
              for i in range(101)]
    saved = []
    src = source()
    src.text = 'q' * 400
    src.content_sha256 = 'h'
    for row in facts:
        row['character_end'] = 400
    for row in claims:
        row['citations'][0]['location']['character_end'] = 400
    run_research_agent(question='Q2', sources=[src], model=Model([STOP]), context=context(), checkpoint=lambda s: saved.append(json.loads(json.dumps(s))),
        prior={'facts': facts, 'calculations': [], 'claims': claims, 'gaps': [{'code': 'no_progress', 'message': 'x'}],
               'read_ranges': [{'source_id': 's1', 'character_start': 0, 'character_end': 400},
                               {'source_id': 'gone', 'character_start': 0, 'character_end': 5}],
               'source_hashes': {'s1': 'h'}, 'review': 'confirmed', 'note': 'n' * 1500, 'summary': 's' * 3000})
    prior = json.loads(saved[0]['user'])['prior_research']
    assert _prior_tokens(prior) <= PRIOR_RESEARCH_TOKEN_BUDGET == 12_000
    # P0 preserved in full (bounded only by per-field caps).
    assert prior['user_review'] == {'review': 'confirmed', 'note': 'n' * 1000, 'summary': 's' * 2000}
    assert prior['open_gaps'] == [{'code': 'no_progress', 'message': 'x'}]
    # P1 keeps most recent facts with verbatim 300-char quotation (no mid-clipping).
    assert 0 < len(prior['facts']) < 200
    assert prior['facts'][-1]['fact_id'] == 'fact-204'
    assert all(len(f['quotation']) == 300 for f in prior['facts'])
    assert prior['prior_omitted']['facts']['count'] == 205 - len(prior['facts'])
    assert prior['prior_omitted']['claims']['count'] == 101 - len(prior['claims'])
    assert prior['prior_omitted'].get('read_ranges', {}).get('count', 0) >= 0
    assert saved[0]['prior_read_ranges'] == [{'source_id': 's1', 'start': 0, 'end': 400}]
    assert len(saved[0]['prior_claim_ids']) == 101  # Every prior claim stays challengeable.

    # With sufficient token_budget (100_000), explicit item caps keep last 200 facts & 100 claims.
    unbounded, _ = _prior_research(
        {'facts': facts, 'calculations': [], 'claims': claims, 'gaps': [], 'read_ranges': [{'source_id': 's1', 'character_start': 0, 'character_end': 400}], 'source_hashes': {'s1': 'h'}},
        [src],
        None,
        token_budget=100_000,
    )
    assert len(unbounded['facts']) == 200 and unbounded['facts'][0]['fact_id'] == 'fact-5' and unbounded['facts'][-1]['fact_id'] == 'fact-204'
    assert len(unbounded['claims']) == 100 and len(unbounded['claims'][0]['text']) == 500 and len(unbounded['claims'][0]['quotation']) == 300
    assert unbounded['truncated'] == {'facts': 5, 'calculations': 0, 'claims': 1, 'charts': 0}


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
        'calculations': [], 'gaps': [], 'source_hashes': {'s1': 'hash'}, 'read_ranges': [{'source_id': 's1', 'character_start': 0, 'character_end': 40}],
        'claims': [{'claim_id': 'claim-old', 'role': 'support', 'text': 'x', 'challenges_claim_ids': [],
                    'citations': [{'source_id': 's1', 'quotation': 'Revenue was 100 USD.', 'location': {'character_start': 0, 'character_end': 20}}]}]}
    src = source()
    src.text = 'Revenue was 100 USD. Revenue was 80 USD.'
    m = Model([turn('calculate_financials', {'rationale': 'g', 'calculations': [
                    {'formula': 'growth_percent', 'first_fact_id': 'fact-a', 'second_fact_id': 'fact-b'}]}, 'a'),
               turn('submit_research', {'report': 'Grew 25%.', 'claims': [], 'limitations': [], 'stopping_reason': 'answered'}, 'b')])
    result = run_research_agent(question='Q2', sources=[src], model=m, context=context(), prior=prior)
    # Q2 does not identify the object of the growth figure, so the calculation
    # remains available without asserting a completed answer.
    assert result.status == 'partial' and result.facts == [] and result.claims == []
    assert result.gaps
    assert result.calculations[0]['value'] == '25.00' and result.reason == 'submitted'
    assert result.stopping_note == 'answered'
    prior['claims'] = []
    m = Model([turn('submit_research', {'report': 'Grew 25%.', 'claims': [], 'limitations': [], 'stopping_reason': 'answered'}, 'b'),
               STOP])
    result = run_research_agent(question='Q2', sources=[src], model=m, context=context(), prior=prior)
    assert result.status == 'partial' and any('At least one source-backed claim' in item.get('content', '') for item in m.inputs[1] if item.get('role') == 'tool')


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


def test_continue_after_provider_output_truncated_runs_l2_compaction_and_succeeds(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRIES', '0')
    quote = 'Revenue was 100 USD.'
    long_text = quote + ' ' + ('Risk factor filler text. ' * 250)  # > 6000 chars, well within 25k read_sources span cap
    seen_payloads: list[dict] = []

    class TruncatingThenRecoveringProvider:
        name = 'l2-compaction-continue-test'
        policy = ProviderPolicy(max_attempts=1, max_output_tokens=1000, timeout_seconds=5)

        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            seen_payloads.append({
                'bytes': len(json.dumps(messages, ensure_ascii=False).encode('utf-8')),
                'messages': messages,
            })
            if self.calls == 1:
                return turn('read_sources', {'reads': [{'source_id': source_id}], 'rationale': 'Read full filing.'}, 'r1')
            if self.calls == 2:
                err = ProviderFailure('provider_output_truncated', 'Provider output was truncated.', retryable=False, reported_tokens=4096)
                err.provider_response_received = True
                raise err
            # Call 3 runs after L2 compaction on /continue: tool result must be compacted and guidance injected via tail_note,
            # while recorded read_ranges still let submit_research locate the quotation verbatim.
            tool_msg = json.loads([m for m in messages if m['role'] == 'tool'][-1]['content'])
            assert tool_msg.get('cleared') is True and tool_msg.get('reason') == 'continuation_compacted', tool_msg
            prefix_payload = json.loads(messages[1]['content'])
            assert 'continuation_guidance' not in prefix_payload and 'recovery_guidance' not in prefix_payload, prefix_payload
            tail_msg = messages[-1]
            assert tail_msg['role'] == 'user' and tail_msg['content'].startswith('[Recovery context: '), tail_msg
            tail_payload = json.loads(tail_msg['content'][len('[Recovery context: '):-1])
            assert 'continuation_guidance' in tail_payload or 'recovery_guidance' in tail_payload, tail_payload
            return turn('submit_research', {
                'report': 'Revenue was 100 USD.',
                'claims': [{'text': 'Revenue was 100 USD.', 'source_id': source_id, 'quotation': quote, 'role': 'support'}],
                'limitations': [], 'stopping_reason': 'answered', 'status': 'completed',
            }, 's1')

    provider = TruncatingThenRecoveringProvider()
    monkeypatch.setattr(api, 'challenge_provider', provider)
    monkeypatch.setattr(api, '_reserve_model_call', lambda *args, **kwargs: None)
    monkeypatch.setattr(api, '_settle_model_call', lambda *args, **kwargs: None)
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={
        'title': 'Long Filing', 'text': long_text,
        'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True},
    }).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'What was revenue?', 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']

    run1 = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={
        'parent_version_id': topic['versions'][0]['version_id'], 'discover_sources': False,
    })
    assert run1.status_code == 202
    op1 = run1.json()['operation']['operation_id']
    for _ in range(300):
        op = client.get(f'{path}/runs/{op1}', headers=headers).json()['operation']
        if op['status'] in ('completed', 'failed', 'cancelled'):
            break
        time.sleep(.01)
    assert op['status'] == 'failed'
    assert client.get(path, headers=headers).json()['versions'][-1]['content']['turns'] == []
    cp1 = api.RESEARCH_RUN_INPUTS.get(op1)['runtime_checkpoint']
    assert len(cp1.get('failed_attempts', [])) == 1 and cp1['failed_attempts'][0]['reason'] == 'provider_output_truncated'

    rec = client.get(f'{path}/runs/{op1}/recovery-status', headers=headers).json()
    assert rec['can_continue'] is True and rec['can_deliver'] is False and rec['reason'] is None

    op2 = dispatch_settled_retry(op1)
    for _ in range(300):
        op = client.get(f'{path}/runs/{op2}', headers=headers).json()['operation']
        if op['status'] in ('completed', 'failed', 'cancelled'):
            break
        time.sleep(.01)
    assert op['status'] == 'completed'
    assert provider.calls == 3
    assert seen_payloads[2]['bytes'] < seen_payloads[1]['bytes']
    final = client.get(path, headers=headers).json()['versions'][-1]['content']
    assert final['turns'][-1]['status'] == 'completed'
    assert final['turns'][-1]['operation_ids'] == [op1, op2]
    assert final['claims'][0]['citations'][0]['quotation'] == quote


def test_continue_after_incomplete_model_response_clears_stopped_and_compacts(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    quote = 'Revenue was 100 USD.'
    long_text = quote + ' ' + ('Supplemental note. ' * 200)
    payloads: list[int] = []

    class IncompleteThenOkProvider:
        name = 'incomplete-continue-test'
        policy = ProviderPolicy(max_attempts=1, max_output_tokens=1000, timeout_seconds=5)

        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            payloads.append(len(json.dumps(messages, ensure_ascii=False).encode('utf-8')))
            if self.calls == 1:
                return turn('read_sources', {'reads': [{'source_id': source_id}], 'rationale': 'Read source.'}, 'r1')
            if self.calls == 2:
                return turn('record_facts', {'rationale': 'Record fact.', 'facts': [
                    {**FACT_ARGS, 'value': 100, 'period': '2025', 'source_id': source_id, 'quotation': quote},
                ]}, 'f1')
            if self.calls == 3:
                return ModelResponse({'role': 'assistant', 'content': 'Truncated half JSON'}, (), None, 'max_tokens', 'truncated')
            tool_contents = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
            assert tool_contents[0].get('cleared') is True and tool_contents[0].get('reason') == 'continuation_compacted'
            assert 'facts' in tool_contents[1]
            return turn('submit_research', {
                'report': 'Revenue was 100 USD.',
                'claims': [{'text': 'Revenue was 100 USD.', 'source_id': source_id, 'quotation': quote, 'role': 'support'}],
                'limitations': [], 'stopping_reason': 'answered', 'status': 'completed',
            }, 's1')

    provider = IncompleteThenOkProvider()
    monkeypatch.setattr(api, 'challenge_provider', provider)
    monkeypatch.setattr(api, '_reserve_model_call', lambda *args, **kwargs: None)
    monkeypatch.setattr(api, '_settle_model_call', lambda *args, **kwargs: None)
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={
        'title': 'Long Filing', 'text': long_text,
        'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True},
    }).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?', 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']

    run1 = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={
        'parent_version_id': topic['versions'][0]['version_id'], 'discover_sources': False,
    })
    assert run1.status_code == 202
    op1 = run1.json()['operation']['operation_id']
    for _ in range(300):
        op = client.get(f'{path}/runs/{op1}', headers=headers).json()['operation']
        if op['status'] in ('completed', 'failed', 'cancelled'):
            break
        time.sleep(.01)
    assert op['status'] == 'failed'
    assert not client.get(path, headers=headers).json()['versions'][-1]['content']['turns']
    assert api.RESEARCH_RUN_INPUTS.get(op1)['runtime_checkpoint'].get('stopped') == 'incomplete_model_response'

    op2 = dispatch_settled_retry(op1)
    for _ in range(300):
        op = client.get(f'{path}/runs/{op2}', headers=headers).json()['operation']
        if op['status'] in ('completed', 'failed', 'cancelled'):
            break
        time.sleep(.01)
    assert op['status'] == 'completed'
    assert provider.calls == 4
    assert payloads[3] < payloads[2]
    assert client.get(path, headers=headers).json()['versions'][-1]['content']['turns'][-1]['status'] == 'completed'


def test_continue_after_provider_timeout_preserves_unread_sources_and_injects_timeout_guidance(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRIES', '0')
    quote = 'Revenue was 100 USD.'
    long_text = quote + ' ' + ('Market commentary note. ' * 200)
    seen_calls: list[list[dict]] = []

    class TimeoutThenRecoverProvider:
        name = 'timeout-continue-test'
        policy = ProviderPolicy(max_attempts=1, max_output_tokens=16384, timeout_seconds=120)

        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            seen_calls.append(messages)
            if self.calls == 1:
                return turn('read_sources', {'reads': [{'source_id': source_id}], 'rationale': 'Read source.'}, 'r1')
            if self.calls == 2:
                err = ProviderFailure('provider_timeout', 'Stream timed out mid-generation.', retryable=True)
                err.provider_response_received = True
                err.timeout_kind = 'stream_deadline_timeout'
                err.elapsed_ms = 120000
                err.wire_bytes = 18000
                err.streamed_bytes = 1024
                raise err
            if self.calls == 3:
                # On 1st /continue with facts=[], read_sources result must be preserved (not cleared),
                # and timeout guidance must instruct concise reasoning + incremental record_facts via tail_note.
                tool_msgs = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
                assert len(tool_msgs) == 1
                assert 'cleared' not in tool_msgs[0]
                assert quote in tool_msgs[0]['results'][0]['text']
                prefix_payload = json.loads(messages[1]['content'])
                assert 'recovery_guidance' not in prefix_payload
                assert 'prior_failed_attempts' not in prefix_payload
                tail_msg = messages[-1]
                assert tail_msg['role'] == 'user' and tail_msg['content'].startswith('[Recovery context: '), tail_msg
                tail_payload = json.loads(tail_msg['content'][len('[Recovery context: '):-1])
                guidance = tail_payload.get('recovery_guidance', '')
                assert 'timed out mid-generation' in guidance and 'record_facts' in guidance
                return turn('record_facts', {'rationale': 'Record key fact.', 'facts': [
                    {**FACT_ARGS, 'value': 100, 'period': '2025', 'source_id': source_id, 'quotation': quote},
                ]}, 'f1')
            return turn('submit_research', {
                'report': 'Revenue was 100 USD.',
                'claims': [{'text': 'Revenue was 100 USD.', 'source_id': source_id, 'quotation': quote, 'role': 'support'}],
                'limitations': [], 'stopping_reason': 'answered', 'status': 'completed',
            }, 's1')

    provider = TimeoutThenRecoverProvider()
    monkeypatch.setattr(api, 'challenge_provider', provider)
    monkeypatch.setattr(api, '_reserve_model_call', lambda *args, **kwargs: None)
    monkeypatch.setattr(api, '_settle_model_call', lambda *args, **kwargs: None)
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    source_id = client.post('/v2/research/sources', headers=headers, json={
        'title': 'Market Filing', 'text': long_text,
        'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True},
    }).json()['source_id']
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Revenue?', 'source_ids': [source_id]}).json()
    path = '/v2/research/topics/' + topic['topic_id']

    run1 = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex}, json={
        'parent_version_id': topic['versions'][0]['version_id'], 'discover_sources': False,
    })
    assert run1.status_code == 202
    op1 = run1.json()['operation']['operation_id']
    for _ in range(300):
        op = client.get(f'{path}/runs/{op1}', headers=headers).json()['operation']
        if op['status'] in ('completed', 'failed', 'cancelled'):
            break
        time.sleep(.01)
    assert op['status'] == 'failed'
    cp1 = api.RESEARCH_RUN_INPUTS.get(op1)['runtime_checkpoint']
    assert len(cp1['failed_attempts']) == 1 and cp1['failed_attempts'][0]['reason'] == 'provider_timeout'
    assert not client.get(path, headers=headers).json()['versions'][-1]['content']['turns']

    op2 = dispatch_settled_retry(op1)
    for _ in range(300):
        op = client.get(f'{path}/runs/{op2}', headers=headers).json()['operation']
        if op['status'] in ('completed', 'failed', 'cancelled'):
            break
        time.sleep(.01)
    assert op['status'] == 'completed'
    assert provider.calls == 4
    final = client.get(path, headers=headers).json()['versions'][-1]['content']
    assert final['turns'][-1]['status'] == 'completed'
    assert final['turns'][-1]['operation_ids'] == [op1, op2]
    assert final['claims'][0]['citations'][0]['quotation'] == quote


def test_prior_research_priority_bucketing_dedupes_includes_failed_sources_and_omits_transparently():
    from types import SimpleNamespace
    from app.provider_capabilities import CapabilityProfile
    from app.research_runtime import PRIOR_RESEARCH_TOKEN_BUDGET, _prior_research, _prior_research_token_budget, _prior_tokens

    assert _prior_research_token_budget(None) == PRIOR_RESEARCH_TOKEN_BUDGET == 12_000
    tight_profile = CapabilityProfile(
        endpoint='https://api.example.com', model='m', version='v1', mode='tool',
        schema_dialect='required_nullable_v1', context_tokens=16_000, work_context_tokens=16_000,
    )
    assert _prior_research_token_budget(SimpleNamespace(research_capacity=lambda: tight_profile)) == 4_000 < PRIOR_RESEARCH_TOKEN_BUDGET

    src_long = source()
    src_long.text = 'X' * 5000
    src_long.content_sha256 = 'h-long'

    # Duplicate facts/calcs keep only the most recent equivalent item; calculations are not starved by facts.
    facts = [
        {'fact_id': 'f-old', 'label': 'revenue', 'value': '100', 'unit': 'USD', 'currency': 'USD', 'period': '2025Q1',
         'source_id': 's1', 'quotation': 'Q1 ' * 60, 'character_start': 0, 'character_end': 10},
        {'fact_id': 'f-dup', 'label': 'revenue', 'value': '100', 'unit': 'USD', 'currency': 'USD', 'period': '2025Q1',
         'source_id': 's1', 'quotation': 'Q1 latest', 'character_start': 0, 'character_end': 10},
    ] + [
        {'fact_id': f'f-extra-{i}', 'label': f'metric_{i}', 'value': str(i), 'unit': 'USD', 'currency': 'USD', 'period': '2025Q2',
         'source_id': 's1', 'quotation': f'quote-{i} ' + ('x' * 240), 'character_start': 10, 'character_end': 30}
        for i in range(18)
    ]
    calcs = [
        {'calculation_id': 'c-old', 'formula_id': 'margin', 'value': 0.25, 'unit': 'ratio', 'inputs': [{'fact_id': 'f-dup'}]},
        {'calculation_id': 'c-dup', 'formula_id': 'margin', 'value': 0.25, 'unit': 'ratio', 'inputs': [{'fact_id': 'f-dup'}]},
        {'calculation_id': 'c-keep', 'formula_id': 'growth', 'value': 0.15, 'unit': 'ratio', 'inputs': [{'fact_id': 'f-extra-0'}]},
    ]
    claims = [
        {'claim_id': f'cl-{i}', 'role': 'support', 'text': f'Claim body {i} ' + ('y' * 250), 'challenges_claim_ids': [],
         'citations': [{'source_id': 's1', 'quotation': f'cite-{i} ' + ('z' * 150), 'location': {'character_start': 0, 'character_end': 12}}]}
        for i in range(12)
    ]
    # Build real located quotations so the prior evidence can be verified.
    chunks = []
    offset = 0
    for row in facts:
        quote = row['quotation']
        row['character_start'], row['character_end'] = offset, offset + len(quote)
        chunks.append(quote)
        offset += len(quote)
    for row in claims:
        citation = row['citations'][0]
        quote = citation['quotation']
        citation['location'] = {'character_start': offset, 'character_end': offset + len(quote)}
        chunks.append(quote)
        offset += len(quote)
    src_long.text = ''.join(chunks)
    read_ranges = ([{'source_id': 's1', 'character_start': 0, 'character_end': len(src_long.text)}] +
                   [{'source_id': 's1', 'character_start': i * 100, 'character_end': i * 100 + 50} for i in range(29)])
    failed_accesses = [{'url': 'https://example.com/10k', 'host': 'example.com', 'status': 'failed', 'reason': 'http_forbidden_or_challenge'}]

    compact_prior, _ = _prior_research(
        {
            'note': 'Follow up on guidance.',
            'review': 'revised',
            'summary': 'Need margin reconciliation.',
            'gaps': [{'code': 'missing_filing', 'message': '10-Q missing'}],
            'failed_source_accesses': failed_accesses,
            'source_hashes': {'s1': 'h-long'},
            'facts': facts,
            'calculations': calcs,
            'claims': claims,
            'read_ranges': read_ranges,
        },
        [src_long],
        None,
        token_budget=3500,
    )
    assert _prior_tokens(compact_prior) <= 3500
    # P0 fields remain intact.
    assert compact_prior['failed_source_accesses'] == failed_accesses
    assert compact_prior['open_gaps'] == [{'code': 'missing_filing', 'message': '10-Q missing'}]
    assert compact_prior['user_review']['review'] == 'revised'
    # Deduped facts/calcs keep the newer copy, and calculations survive alongside facts.
    fact_ids = [f['fact_id'] for f in compact_prior['facts']]
    calc_ids = [c['calculation_id'] for c in compact_prior['calculations']]
    assert 'f-old' not in fact_ids
    assert 'c-old' not in calc_ids
    assert 'c-keep' in calc_ids and 'c-dup' in calc_ids
    # P2 claims kept are never mid-clipped; omitted lower-priority items are accounted for.
    assert 0 < len(compact_prior['claims']) < 12
    for cl in compact_prior['claims']:
        assert cl['text'].startswith('Claim body ')
        assert len(cl['text']) in (len('Claim body 0 ') + 250, len('Claim body 10 ') + 250)
    omitted = compact_prior['prior_omitted']
    assert omitted['claims']['count'] == 12 - len(compact_prior['claims'])
    assert omitted['read_ranges']['count'] == 30 - len(compact_prior['read_ranges'])
