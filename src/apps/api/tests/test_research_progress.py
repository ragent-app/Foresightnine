"""Live run progress (sanitized checkpoint view) and focus_citation reaching the agent."""
import json
import threading
import time
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as api
from app.research_progress import sanitize_events
from app.model_protocol import ModelResponse
from app.research_runtime import SYSTEM, run_research_agent
from tests.test_research_multiturn import (Q1, Q2, TEXT, Scripted, env, read_all, submit)  # noqa: F401
from tests.test_research_runtime import Model, context, source, turn

PROGRESS_KEYS = {'operation_id', 'operation_status', 'turn', 'usage', 'current', 'events', 'event_count',
                 'started_at', 'updated_at', 'version_id'}


def progress(env, operation_id, after=None):
    suffix = '' if after is None else f'?after={after}'
    response = env.client.get(f'{env.path}/runs/{operation_id}/progress{suffix}', headers=env.headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == PROGRESS_KEYS
    return body


def start_blocked(env):
    """Start a run whose second model call blocks until the returned event is set."""
    started, release = threading.Event(), threading.Event()
    sid = env.source_id

    def blocked(messages, kwargs):
        started.set()
        assert release.wait(10)
        return submit(sid)(messages, kwargs)

    env.install(Scripted(read_all(sid), blocked))
    response = env.start()
    assert response.status_code == 202, response.text
    return response.json()['operation']['operation_id'], started, release


def test_progress_while_running_is_incremental(env):
    operation_id, started, release = start_blocked(env)
    try:
        assert started.wait(10)
        body = progress(env, operation_id)
        assert body['operation_status'] == 'running'
        assert body['turn'] == {'index': 1, 'question': Q1}
        assert body['usage']['model_calls'] >= 1 and body['usage']['search_calls'] == 0
        assert body['current'] == {'kind': 'model'}
        assert body['started_at'] and body['updated_at'] and body['version_id'] is None
        assert [event['index'] for event in body['events']] == list(range(body['event_count']))
        assert [event['kind'] for event in body['events']] == ['model', 'tool'] and body['event_count'] == 2
        assert body['events'][1]['name'] == 'read_sources' and body['events'][1]['rationale'] == 'read'
        later = progress(env, operation_id, after=1)
        assert [event['index'] for event in later['events']] == [1] and later['event_count'] == 2
        assert progress(env, operation_id, after=2)['events'] == []
        assert env.client.get(f'{env.path}/runs/{operation_id}/progress?after=-1', headers=env.headers).status_code == 422
    finally:
        release.set()
    assert env.wait(operation_id)['status'] == 'completed'


def test_progress_reports_the_tool_in_flight(env, monkeypatch):
    sid = env.source_id
    started, release = threading.Event(), threading.Event()

    def blocked_search(query, **kwargs):
        started.set()
        assert release.wait(10)
        return []

    monkeypatch.setattr(api, 'evidence_provider', SimpleNamespace(supports_project_search=True, search=blocked_search))
    env.install(Scripted(lambda m, k: turn('discover_sources', {'query': 'revenue', 'rationale': 'look'}, 'd'), submit(sid)))
    try:
        response = env.start({'discover_sources': True, 'max_search_calls': 1})
        assert response.status_code == 202, response.text
        operation_id = response.json()['operation']['operation_id']
        assert started.wait(10)
        body = progress(env, operation_id)
        assert body['current'] == {'kind': 'tool', 'name': 'discover_sources'}
        assert body['usage']['search_calls'] == 1
    finally:
        release.set()
    env.wait(operation_id)


def stored_checkpoint(operation_id):
    return api.RESEARCH_RUN_INPUTS.get(operation_id)


def test_events_are_whitelisted_and_unknown_kinds_dropped(env):
    sid = env.source_id
    env.install(Scripted(read_all(sid), submit(sid)))
    operation_id = env.run()
    inputs = stored_checkpoint(operation_id)
    hostile = {'call_id': 'SECRET-CALL', 'tool_call_ids': ['SECRET-CALL'], 'tool_call_id': 'SECRET-CALL',
               'arguments': {'text': 'SECRET-ARGS'}, 'text': 'SECRET-TEXT', 'message': {'content': 'SECRET-MSG'}}
    inputs['runtime_checkpoint']['events'] = [
        {'kind': 'model', 'finish_reason': 'tool_calls', 'usage': 2, 'usage_detail': {'input_tokens': 1, 'output_tokens': 1,
         'total_tokens': 2, 'cache_read_input_tokens': 5}, 'settlement': 'settled', 'external_state': 'returned', **hostile},
        {'kind': 'model_failure', 'code': 'provider_error', 'external_state': 'returned', **hostile},
        {'kind': 'tool', 'name': 'discover_sources', 'status': 'completed', 'reason': 'search_no_results',
         'rationale': 'x' * 500, 'denied_count': 2, 'denied_reasons': {'a': 1}, **hostile},
        {'kind': 'submission', 'status': 'completed', 'stopping_reason': 'SECRET-STOP', **hostile},
        {'kind': 'mystery', **hostile},
        'not-an-event',
    ]
    api.RESEARCH_RUN_INPUTS.put(operation_id, inputs)
    response = env.client.get(f'{env.path}/runs/{operation_id}/progress', headers=env.headers)
    body = response.json()
    assert 'SECRET' not in response.text and body['event_count'] == 6
    assert [event['kind'] for event in body['events']] == ['model', 'model_failure', 'tool', 'submission']
    assert [event['index'] for event in body['events']] == [0, 1, 2, 3]
    assert body['events'][0] == {'index': 0, 'kind': 'model', 'finish_reason': 'tool_calls', 'usage': 2,
                                 'input_tokens': 1, 'output_tokens': 1, 'settlement': 'settled'}
    assert body['events'][1] == {'index': 1, 'kind': 'model_failure', 'code': 'provider_error'}
    assert set(body['events'][2]) == {'index', 'kind', 'name', 'status', 'reason', 'rationale', 'denied_count'}
    assert body['events'][2]['rationale'] == 'x' * 300 and body['events'][2]['denied_count'] == 2
    assert body['events'][3] == {'index': 3, 'kind': 'submission', 'status': 'completed'}
    assert [e['index'] for e in env.client.get(f'{env.path}/runs/{operation_id}/progress?after=3', headers=env.headers).json()['events']] == [3]


def test_completed_run_progress_matches_saved_turn(env):
    sid = env.source_id
    env.install(Scripted(read_all(sid), submit(sid)))
    operation_id = env.run()
    version = env.latest()
    saved = version['content']['turns'][-1]
    body = progress(env, operation_id)
    assert body['operation_status'] == 'completed' and body['current'] is None
    assert body['version_id'] == version['version_id']
    assert body['events'] == sanitize_events(saved['events']) and body['event_count'] == len(saved['events']) == 4
    assert body['usage'] == saved['usage'] and body['usage']['model_calls'] == 2
    assert body['turn'] == {'index': 1, 'question': Q1}
    call_ids = [event['call_id'] for event in saved['events'] if 'call_id' in event]
    assert call_ids and not any(call_id in json.dumps(body) for call_id in call_ids)
    # Without a checkpoint, the saved turn is the source.
    inputs = stored_checkpoint(operation_id)
    del inputs['runtime_checkpoint']
    api.RESEARCH_RUN_INPUTS.put(operation_id, inputs)
    fallback = progress(env, operation_id)
    assert fallback['events'] == body['events'] and fallback['usage'] == body['usage'] and fallback['version_id'] == version['version_id']
    assert progress(env, operation_id, after=3)['events'] == [body['events'][3]]


def test_progress_is_owner_scoped(env):
    sid = env.source_id
    env.install(Scripted(read_all(sid), submit(sid)))
    operation_id = env.run()
    other = TestClient(api.app)
    auth = other.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test',
                      'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    assert other.get(f'{env.path}/runs/{operation_id}/progress', headers=headers).status_code == 404
    assert env.client.get(f'{env.path}/runs/op-missing/progress', headers=env.headers).status_code == 404
    assert env.client.get(f'{env.path}/runs/{operation_id}/progress').status_code in (401, 403)


def test_focus_citation_reaches_the_agent_and_its_range_counts_as_read(env):
    sid = env.source_id
    seen = {}

    def second_turn(messages, kwargs):
        seen['user'] = json.loads(messages[1]['content'])
        # No read_sources call: the focused range alone must license this exact quotation.
        return submit(sid, quotation='Revenue was 100 USD.', character_start=0, character_end=20)(messages, kwargs)

    env.install(Scripted(read_all(sid), submit(sid), second_turn))
    env.run()
    claim_id = env.latest()['content']['claims'][0]['claim_id']
    operation_id = env.run({'question_override': Q2, 'focus_citation': {'claim_id': claim_id, 'citation_index': 0}})
    assert seen['user']['focus_citation'] == {'claim_id': claim_id, 'source_id': sid, 'quotation': 'Revenue was 100 USD.',
                                              'character_start': 0, 'character_end': 20}
    content = env.latest()['content']
    assert content['turns'][-1]['status'] == 'completed' and len(content['claims']) == 2
    assert 'focus_citation' in SYSTEM
    # The focus is part of the persisted conversation, so a continuation keeps it.
    assert json.loads(stored_checkpoint(operation_id)['runtime_checkpoint']['user'])['focus_citation']['claim_id'] == claim_id


def test_focus_range_alone_licenses_a_quotation_and_survives_a_checkpoint():
    focus = {'claim_id': 'claim-1', 'source_id': 's1', 'quotation': 'Revenue was 100 USD.', 'character_start': 0, 'character_end': 20}
    claim = {'text': 'Revenue was 100 USD.', 'source_id': 's1', 'quotation': 'Revenue was 100 USD.',
             'character_start': 0, 'character_end': 20, 'role': 'support'}
    submission = {'report': 'x', 'claims': [claim], 'limitations': [], 'stopping_reason': 'answered'}
    saved = []
    model = Model([turn('submit_research', submission, 'b')])
    result = run_research_agent(question='Revenue?', sources=[source()], model=model, context=context(), focus=focus,
                                checkpoint=lambda state: saved.append(deepcopy(state)))
    assert result.status == 'completed'
    assert json.loads(saved[0]['user'])['focus_citation'] == focus and saved[0]['focus_ranges'][0]['end'] == 20
    # Without the focus the same unread offsets are refused.
    stop = ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')
    unfocused = Model([turn('submit_research', submission, 'b'), stop])
    run_research_agent(question='Revenue?', sources=[source()], model=unfocused, context=context())
    assert 'previously read range' in unfocused.inputs[1][-1]['content']
    # A continuation restores the same focus from the checkpoint, not from the call arguments.
    state = next(deepcopy(item) for item in saved if item['pending'] and item['pending']['kind'] == 'tool_batch')
    again = run_research_agent(question='Revenue?', sources=[source()], model=Model([]), context=context(), initial_state=state)
    assert again.status == 'completed' and again.claims[0]['citations'][0]['location'] == {'character_start': 0, 'character_end': 20}
