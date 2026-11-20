"""Live run progress (sanitized checkpoint view) and focus_citation reaching the agent."""
from datetime import date, datetime, timezone
import json
import threading
import time
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as api
from app.research_progress import build_progress, current_activity, sanitize_event, sanitize_events
from app.model_protocol import ModelResponse, ModelToolCall, ModelUsage
from app.research_runtime import SYSTEM, run_research_agent
from app.research_sources import ResearchSource, SourcePermissions
from app.research_structured_connector import StructuredQueryResult
from app.research_topics import ReadRange, ResearchTurn, TurnUsage
from tests.test_research_multiturn import (Q1, Q2, TEXT, Scripted, env, read_all, submit)  # noqa: F401
from tests.fin_data.helpers import ConceptTransport
from tests.fin_runtime_helpers import FakeTransport, json_reply, make_toolbox
from tests.test_research_runtime import Model, context, source, turn

PROGRESS_KEYS = {'operation_id', 'operation_status', 'turn', 'usage', 'current', 'interim', 'context_receipts', 'events', 'event_count',
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
        assert body['current']['kind'] == 'model'
        assert body['current']['phase'] == 'thinking'
        assert body['current']['retry_attempt'] is None
        assert isinstance(body['current']['started_at'], str) and body['current']['started_at']
        assert body['interim'] == {
            'leads_count': 0,
            'acquired_sources_count': 0,
            'read_sources_count': 1,
            'facts_count': 0,
            'calculations_count': 0,
        }
        assert body['started_at'] and body['updated_at'] and body['version_id'] is None
        assert [event['index'] for event in body['events']] == list(range(body['event_count']))
        assert [event['kind'] for event in body['events']] == ['model', 'tool'] and body['event_count'] == 2
        assert body['events'][1]['name'] == 'read_sources' and body['events'][1]['rationale'] == 'read'
        assert body['events'][1]['summary'] == {
            'kind': 'read_sources',
            'read_count': 1,
            'items': [{'source_id': env.source_id, 'title': 'Report', 'start': 0, 'end': len(TEXT), 'total_characters': len(TEXT)}],
        }
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
        assert body['current']['kind'] == 'tool'
        assert body['current']['name'] == 'discover_sources'
        assert body['current']['rationale'] == 'look'
        assert body['current']['target'] == 'revenue'
        assert isinstance(body['current']['started_at'], str) and body['current']['started_at']
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
               'arguments': {'text': 'SECRET-ARGS'}, 'text': 'SECRET-TEXT', 'message': {'content': 'SECRET-MSG'},
               'thinking': 'SECRET-THINK', 'redacted_thinking': 'SECRET-REDACT', 'signature': 'SECRET-SIG',
               'quotation': 'SECRET-QUOTE'}
    inputs['runtime_checkpoint']['events'] = [
        {'kind': 'model', 'finish_reason': 'tool_calls', 'usage': 2, 'usage_detail': {'input_tokens': 1, 'output_tokens': 1,
         'total_tokens': 2, 'cache_read_input_tokens': 5}, 'settlement': 'settled', 'external_state': 'returned', **hostile},
        {'kind': 'model_failure', 'code': 'provider_error', 'external_state': 'returned', **hostile},
        {'kind': 'tool', 'name': 'discover_sources', 'status': 'completed', 'reason': 'search_no_results',
         'rationale': 'x' * 500, 'denied_count': 2, 'denied_reasons': {'a': 1}, **hostile},
        {'kind': 'submission', 'status': 'completed', 'stopping_reason': 'SECRET-STOP', **hostile},
        {'kind': 'tool', 'name': 'read_sources', 'status': 'completed', 'rationale': 'read',
         'summary': {'kind': 'read_sources', 'read_count': 1, 'extra': 'SECRET-EXTRA',
                     'items': [{'source_id': 's1', 'title': 'Doc', 'start': 0, 'end': 10,
                                'total_characters': 20, 'text': 'SECRET-BODY', 'quotation': 'SECRET-Q'}]}},
        {'kind': 'mystery', **hostile},
        'not-an-event',
    ]
    api.RESEARCH_RUN_INPUTS.put(operation_id, inputs)
    response = env.client.get(f'{env.path}/runs/{operation_id}/progress', headers=env.headers)
    body = response.json()
    assert 'SECRET' not in response.text and body['event_count'] == 7
    assert [event['kind'] for event in body['events']] == ['model', 'model_failure', 'tool', 'submission', 'tool']
    assert [event['index'] for event in body['events']] == [0, 1, 2, 3, 4]
    assert body['events'][0] == {'index': 0, 'kind': 'model', 'finish_reason': 'tool_calls', 'usage': 2,
                                 'input_tokens': 1, 'output_tokens': 1, 'settlement': 'settled', 'note': None}
    assert body['events'][1] == {'index': 1, 'kind': 'model_failure', 'code': 'provider_error'}
    assert set(body['events'][2]) == {'index', 'kind', 'name', 'status', 'reason', 'rationale', 'denied_count', 'summary'}
    assert body['events'][2]['rationale'] == 'x' * 300 and body['events'][2]['denied_count'] == 2
    assert body['events'][2]['summary'] is None
    assert body['events'][3] == {'index': 3, 'kind': 'submission', 'status': 'completed'}
    assert body['events'][4]['summary'] == {
        'kind': 'read_sources',
        'read_count': 1,
        'items': [{'source_id': 's1', 'title': 'Doc', 'start': 0, 'end': 10, 'total_characters': 20}],
    }
    assert [e['index'] for e in env.client.get(f'{env.path}/runs/{operation_id}/progress?after=3', headers=env.headers).json()['events']] == [3, 4]


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
    assert body['interim'] == {
        'leads_count': 0,
        'acquired_sources_count': 0,
        'read_sources_count': 1,
        'facts_count': 0,
        'calculations_count': 0,
    }
    call_ids = [event['call_id'] for event in saved['events'] if 'call_id' in event]
    assert call_ids and not any(call_id in json.dumps(body) for call_id in call_ids)
    # Without a checkpoint, the saved turn is the source.
    inputs = stored_checkpoint(operation_id)
    del inputs['runtime_checkpoint']
    api.RESEARCH_RUN_INPUTS.put(operation_id, inputs)
    fallback = progress(env, operation_id)
    assert fallback['events'] == body['events'] and fallback['usage'] == body['usage'] and fallback['version_id'] == version['version_id']
    assert fallback['interim'] == body['interim']
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


def test_all_eight_tools_current_activity_and_summaries():
    acq_url = 'https://www.sec.gov/Archives/edgar/data/1/a.htm?api_key=SECRET-KEY'
    ir_url = 'https://nvidianews.nvidia.com/news/q4?token=SECRET-TOKEN'
    fred_url = 'https://fred.stlouisfed.org/series/GDP'
    acq_source = ResearchSource(
        source_id='s_acq',
        owner_id='owner',
        title='NVDA 10-K Filing',
        text='Revenue was 130 USD in 2025. Revenue was 100 USD in 2024.',
        url='https://www.sec.gov/Archives/edgar/data/1/a.htm',
        kind='official',
        obtained_at=datetime.now(timezone.utc),
        content_sha256='h-acq',
        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True),
    )
    leads = [
        {'url': 'https://www.sec.gov/Archives/edgar/data/1/a.htm?api_key=SECRET-KEY', 'title': 'NVDA 10-K Filing', 'published_at': '2025-02-26T00:00:00Z'},
        {'url': ir_url, 'title': 'NVIDIA Q4 Release', 'published_at': '2025-02-26T00:00:00Z'},
        {'url': fred_url, 'title': 'FRED SECRET', 'published_at': '2025-02-26T00:00:00Z'},
    ]
    recorded_activities = []

    def on_checkpoint(state):
        act = current_activity(state)
        if act is not None:
            recorded_activities.append(act)

    class ScriptedEight:
        def __init__(self):
            self.step = 0

        def complete(self, messages, tools, **kwargs):
            self.step += 1
            if self.step == 1:
                return turn('discover_sources', {'query': 'NVDA FY2025 revenue', 'rationale': 'Find disclosures'}, 'c1')
            if self.step == 2:
                return turn('acquire_sources', {'urls': [acq_url, ir_url], 'rationale': 'Fetch leads'}, 'c2')
            if self.step == 3:
                return turn('company_financials', {
                    'cik': '1045810', 'lines': ['revenue'], 'periods': 'last_4_quarters', 'rationale': 'Get SEC XBRL',
                }, 'c3')
            if self.step == 4:
                return turn('market_data', {
                    'entity_id': 'SSE:ANT', 'start_date': '2026-11-16', 'end_date': '2026-11-19', 'rationale': 'Get prices',
                }, 'c4')
            if self.step == 5:
                return turn('read_sources', {'reads': [{'source_id': 's_acq', 'start': 0, 'end': len(acq_source.text)}], 'rationale': 'Read 10-K'}, 'c5')
            if self.step == 6:
                return turn('record_facts', {
                    'facts': [
                        {'label': '总收入', 'value': 130, 'unit': 'USD', 'currency': 'USD', 'period': '2025',
                         'source_id': 's_acq', 'quotation': 'Revenue was 130 USD in 2025.'},
                        {'label': '上年收入', 'value': 100, 'unit': 'USD', 'currency': 'USD', 'period': '2024',
                         'source_id': 's_acq', 'quotation': 'Revenue was 100 USD in 2024.'},
                    ],
                    'rationale': 'Record revenue figures',
                }, 'c6')
            if self.step == 7:
                facts = json.loads(messages[-1]['content'])['facts']
                return turn('calculate_financials', {
                    'calculations': [{'formula': 'growth_percent', 'first_fact_id': facts[0]['fact_id'], 'second_fact_id': facts[1]['fact_id']}],
                    'rationale': 'Compute revenue growth',
                }, 'c7')
            return turn('submit_research', {
                'report': 'SECRET-UNVERIFIED-REPORT Revenue grew 30%.',
                'claims': [{'text': 'SECRET-CLAIM Revenue was 130 USD.', 'source_id': 's_acq',
                            'quotation': 'Revenue was 130 USD in 2025.', 'role': 'support'}],
                'limitations': [],
                'stopping_reason': 'SECRET-STOPPING answered',
            }, 'c8')

    def acquire_fn(url, title):
        if 'sec.gov' in url:
            return acq_source, {'status': 'completed', 'outcomes': [{'status': 'retrieved'}]}
        return None, {'status': 'failed', 'outcomes': [{'status': 'failed', 'reason': 'http_forbidden_or_challenge'}]}

    saved = []
    def save_and_record(state):
        saved.append(deepcopy(state))
        on_checkpoint(state)

    outcome = run_research_agent(
        question='NVDA revenue?',
        sources=[],
        model=ScriptedEight(),
        context=context(),
        discover=lambda q: leads,
        acquire=acquire_fn,
        fin_data=make_toolbox(twelve=FakeTransport(json_reply('td_time_series_ipo.json')),
                              sec=ConceptTransport()),
        checkpoint=save_and_record,
    )
    assert outcome.status == 'completed'
    serialized_activities = json.dumps(recorded_activities, ensure_ascii=False)
    for secret in ('SECRET-KEY', 'SECRET-TOKEN', 'user:pass', 'FRED SECRET', 'SECRET-UNVERIFIED-REPORT', 'SECRET-CLAIM', 'SECRET-STOPPING'):
        assert secret not in serialized_activities

    tool_acts = {act['name']: act for act in recorded_activities if act['kind'] == 'tool'}
    assert tool_acts['discover_sources']['target'] == 'NVDA FY2025 revenue'
    assert tool_acts['discover_sources']['rationale'] == 'Find disclosures'
    assert tool_acts['acquire_sources']['target'] == 'www.sec.gov、nvidianews.nvidia.com'
    assert tool_acts['company_financials']['target'] == '1045810 · last_4_quarters'
    assert tool_acts['market_data']['target'] == 'SSE:ANT · 1day (2026-11-16 ~ 2026-11-19)'
    assert tool_acts['read_sources']['target'] == f'《NVDA 10-K Filing》0–{len(acq_source.text)} 字'
    assert tool_acts['record_facts']['target'] == '总收入、上年收入（2 项）'
    assert tool_acts['calculate_financials']['target'] == '增长率（1 项）'
    assert tool_acts['submit_research']['target'] == '正在核验引文位置与研究结论'
    assert tool_acts['submit_research']['rationale'] is None
    for act in tool_acts.values():
        assert isinstance(act['started_at'], str) and act['started_at']

    sanitized = sanitize_events(outcome.events)
    tool_events = {ev['name']: ev for ev in sanitized if ev['kind'] == 'tool'}
    assert tool_events['discover_sources']['summary'] == {
        'kind': 'discover_sources',
        'found_count': 3,
        'eligible_count': 2,
        'denied_count': 1,
        'leads_preview': [
            {'title': 'NVDA 10-K Filing', 'host': 'www.sec.gov', 'published_at': '2025-02-26T00:00:00+00:00'},
            {'title': 'NVIDIA Q4 Release', 'host': 'nvidianews.nvidia.com', 'published_at': '2025-02-26T00:00:00+00:00'},
        ],
    }
    assert tool_events['acquire_sources']['summary'] == {
        'kind': 'acquire_sources',
        'acquired_count': 1,
        'failed_count': 1,
        'items': [
            {'title': 'NVDA 10-K Filing', 'host': 'www.sec.gov', 'status': 'acquired', 'reason': None, 'total_characters': len(acq_source.text)},
            {'title': 'NVIDIA Q4 Release', 'host': 'nvidianews.nvidia.com', 'status': 'failed', 'reason': 'http_forbidden_or_challenge', 'total_characters': None},
        ],
    }
    for tool, target in (('company_financials', '1045810 · last_4_quarters'),
                         ('market_data', 'SSE:ANT · 1day (2026-11-16 ~ 2026-11-19)')):
        summary = tool_events[tool]['summary']
        assert summary['kind'] == 'data_tool' and summary['tool'] == tool and summary['target'] == target
        assert summary['status'] in ('ok', 'partial') and summary['title'] and summary['total_characters'] > 0
    assert tool_events['read_sources']['summary'] == {
        'kind': 'read_sources',
        'read_count': 1,
        'items': [{'source_id': 's_acq', 'title': 'NVDA 10-K Filing', 'start': 0, 'end': len(acq_source.text), 'total_characters': len(acq_source.text)}],
    }
    assert tool_events['record_facts']['summary'] == {
        'kind': 'record_facts',
        'recorded_count': 2,
        'facts_preview': [
            {'label': '总收入', 'value': '130', 'unit': 'USD', 'currency': 'USD', 'period': '2025'},
            {'label': '上年收入', 'value': '100', 'unit': 'USD', 'currency': 'USD', 'period': '2024'},
        ],
    }
    assert tool_events['calculate_financials']['summary'] == {
        'kind': 'calculate_financials',
        'calculated_count': 1,
        'calcs_preview': [
            {'formula_id': 'growth_percent', 'value': '30.00', 'unit': 'percent', 'currency': None, 'expression': '(first / second - 1) * 100'},
        ],
    }

    # Interim counts match between final checkpoint and reconstructed ResearchTurn.
    final_cp = saved[-1]
    prog_cp = build_progress(operation_id='op1', operation_status='completed', inputs={'question': 'NVDA revenue?'},
                             checkpoint=final_cp, saved_turn=None, version_id='v2', after=0)
    saved_turn = ResearchTurn(
        turn_id='t1',
        index=1,
        question='NVDA revenue?',
        question_origin='user',
        operation_ids=['op1'],
        parent_version_id='v1',
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        status='completed',
        reason='submitted',
        fact_ids=[f['fact_id'] for f in outcome.facts],
        calculation_ids=[c['calculation_id'] for c in outcome.calculations],
        usage=TurnUsage(model_calls=8, search_calls=3, total_tokens=16),
        search_status='completed',
        official_fetch_status='completed',
        read_ranges=[ReadRange(source_id='s_acq', character_start=0, character_end=len(acq_source.text))],
        events=outcome.events,
    )
    prog_turn = build_progress(operation_id='op1', operation_status='completed', inputs={'question': 'NVDA revenue?'},
                               checkpoint=None, saved_turn=saved_turn, version_id='v2', after=0)
    expected_interim = {
        'leads_count': 2,
        'acquired_sources_count': 3,
        'read_sources_count': 1,
        'facts_count': 2,
        'calculations_count': 1,
    }
    assert prog_cp['interim'] == expected_interim
    assert prog_turn['interim'] == expected_interim
    prog_empty = build_progress(operation_id='op1', operation_status='pending', inputs={'question': 'NVDA revenue?'},
                                checkpoint=None, saved_turn=None, version_id=None, after=0)
    assert prog_empty['interim'] == {
        'leads_count': 0,
        'acquired_sources_count': 0,
        'read_sources_count': 0,
        'facts_count': 0,
        'calculations_count': 0,
    }


def test_model_note_extraction_excludes_thinking_signatures_and_tool_json():
    class NeutralProviders:
        def __init__(self):
            self.step = 0

        def estimate_turn_input(self, turn_input):
            return 10

        def complete_turn(self, turn_input, **kwargs):
            self.step += 1
            if self.step == 1:
                # Anthropic Messages blocks: thinking, redacted_thinking, visible text, tool_use
                msg = {
                    'role': 'assistant',
                    'content': [
                        {'type': 'thinking', 'thinking': 'SECRET-THINKING-TEXT', 'signature': 'SECRET-SIGNATURE'},
                        {'type': 'redacted_thinking', 'data': 'SECRET-REDACTED'},
                        {'type': 'text', 'text': '  已定位公司文件，准备读取第 1 段原文。  '},
                        {'type': 'tool_use', 'id': 'call_1', 'name': 'read_sources',
                         'input': {'reads': [{'source_id': 's1'}], 'rationale': 'SECRET-TOOL-JSON'}},
                    ],
                }
                return ModelResponse(msg, (ModelToolCall('call_1', 'read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}),),
                                     ModelUsage(10, 5, 15), 'tool_use', 'tool_calls')
            if self.step == 2:
                # OpenAI-compatible message with string content > 600 chars + reasoning_content
                msg = {
                    'role': 'assistant',
                    'content': '过程说明：' + ('x' * 700),
                    'reasoning_content': 'SECRET-REASONING-CONTENT',
                    'tool_calls': [{'id': 'call_2', 'type': 'function', 'function': {'name': 'submit_research', 'arguments': '{}'}}],
                }
                return ModelResponse(msg, (ModelToolCall('call_2', 'submit_research', {
                    'report': 'Revenue was 100 USD.',
                    'claims': [{'text': 'Revenue was 100 USD.', 'source_id': 's1', 'quotation': 'Revenue was 100 USD.', 'role': 'support'}],
                    'limitations': [],
                    'stopping_reason': 'answered',
                }),), ModelUsage(12, 6, 18), 'tool_calls', 'tool_calls')
            raise AssertionError('unexpected step')

    outcome = run_research_agent(question='Revenue?', sources=[source()], model=NeutralProviders(), context=context())
    assert outcome.status == 'completed'
    sanitized = sanitize_events(outcome.events)
    model_events = [ev for ev in sanitized if ev['kind'] == 'model']
    assert len(model_events) == 2
    assert model_events[0]['note'] == '已定位公司文件，准备读取第 1 段原文。'
    assert model_events[1]['note'] == ('过程说明：' + ('x' * 700))[:600]
    assert len(model_events[1]['note']) == 600
    dumped = json.dumps(sanitized, ensure_ascii=False)
    for forbidden in ('SECRET-THINKING-TEXT', 'SECRET-SIGNATURE', 'SECRET-REDACTED', 'SECRET-TOOL-JSON', 'SECRET-REASONING-CONTENT', SYSTEM):
        assert forbidden not in dumped

    # Whitespace-only or thinking-only yields note=None
    class ThinkingOnly:
        def estimate_turn_input(self, turn_input):
            return 10

        def complete_turn(self, turn_input, **kwargs):
            return ModelResponse({
                'role': 'assistant',
                'content': [
                    {'type': 'thinking', 'thinking': 'SECRET-ONLY', 'signature': 'SIG'},
                    {'type': 'text', 'text': '   \n  '},
                ],
            }, (), ModelUsage(1, 1, 2), 'end_turn', 'end')

    empty_outcome = run_research_agent(question='Q', sources=[], model=ThinkingOnly(), context=context())
    assert sanitize_events(empty_outcome.events)[0]['note'] is None


def test_model_presend_retry_records_retrying_phase_and_attempt(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRIES', '2')
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRY_BASE_SECONDS', '0')
    from app.anthropic_messages import AnthropicMessagesProvider
    from app.openai_compatible import HttpResponse, _TransportFailure
    from app.phase3 import ProviderPolicy

    attempts = 0
    observed_models = []

    def transport(method, url, headers, body, timeout, *, diagnostics, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            with diagnostics.stage('dns'):
                raise _TransportFailure()
        diagnostics.submission('response_received')
        return HttpResponse(200, json.dumps({
            'role': 'assistant',
            'stop_reason': 'tool_use',
            'content': [{'type': 'tool_use', 'id': 'sub1', 'name': 'submit_research', 'input': {
                'report': 'Done.',
                'claims': [],
                'limitations': ['No data'],
                'stopping_reason': 'none',
                'status': 'partial',
            }}],
            'usage': {'input_tokens': 2, 'output_tokens': 3},
        }), {})

    monkeypatch.setattr('app.anthropic_messages._default_transport', transport)
    settings = SimpleNamespace(base_url='https://example.com', model='model', api_key='fixture', allow_private_network=False)
    provider = AnthropicMessagesProvider(settings, ProviderPolicy(retry_base_seconds=0),
                                         anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=100)

    def capture(state):
        act = current_activity(state)
        if act and act['kind'] == 'model':
            observed_models.append(act)

    outcome = run_research_agent(question='Q', sources=[], model=provider, context=context(), checkpoint=capture)
    assert outcome.status == 'partial'
    assert attempts == 2
    assert any(item['phase'] == 'thinking' and item['retry_attempt'] is None for item in observed_models)
    assert any(item['phase'] == 'retrying' and item['retry_attempt'] == 1 for item in observed_models)


def test_read_sources_never_leaks_text_even_when_display_false():
    restricted = ResearchSource(
        source_id='s_nodisplay',
        owner_id='owner',
        title='Confidential Filing Title',
        text='CONFIDENTIAL-RAW-SOURCE-TEXT-12345',
        kind='user_supplied',
        obtained_at=datetime.now(timezone.utc),
        content_sha256='h-nodisp',
        permissions=SourcePermissions(store=True, model_input=True, display=False, export=False),
    )
    eligible = ResearchSource(
        source_id='s_ok',
        owner_id='owner',
        title='Public Filing Title',
        text='PUBLIC-RAW-SOURCE-BODY-TEXT-67890',
        kind='user_supplied',
        obtained_at=datetime.now(timezone.utc),
        content_sha256='h-ok',
        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True),
    )
    saved = []
    m = Model([
        turn('read_sources', {'reads': [{'source_id': 's_nodisplay'}, {'source_id': 's_ok'}], 'rationale': 'read both'}, 'r1'),
        ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop'),
    ])
    outcome = run_research_agent(
        question='Q',
        sources=[restricted, eligible],
        model=m,
        context=context(),
        checkpoint=lambda s: saved.append(deepcopy(s)),
    )
    activities = [current_activity(cp) for cp in saved if current_activity(cp) is not None]
    sanitized = sanitize_events(outcome.events)
    prog = build_progress(operation_id='op', operation_status='completed', inputs={'question': 'Q'},
                          checkpoint=saved[-1], saved_turn=None, version_id=None, after=0)
    payload_dump = json.dumps({'activities': activities, 'events': sanitized, 'progress': prog}, ensure_ascii=False)
    assert 'CONFIDENTIAL-RAW-SOURCE-TEXT-12345' not in payload_dump
    assert 'Confidential Filing Title' not in payload_dump
    assert 'PUBLIC-RAW-SOURCE-BODY-TEXT-67890' not in payload_dump
    read_ev = next(ev for ev in sanitized if ev['kind'] == 'tool' and ev['name'] == 'read_sources')
    assert read_ev['summary'] == {
        'kind': 'read_sources',
        'read_count': 1,
        'items': [{'source_id': 's_ok', 'title': 'Public Filing Title', 'start': 0, 'end': len(eligible.text), 'total_characters': len(eligible.text)}],
    }


def test_legacy_checkpoint_and_events_backward_compatibility_and_2kb_cap():
    legacy_cp = {
        'pending': {'kind': 'model', 'call_id': 'old-call'},
        'leads': [{'url': 'https://example.com/1', 'title': 'Lead 1'}],
        'acquired_lead_sources': {'https://example.com/1': 's1'},
        'read_ranges': [{'source_id': 's1', 'start': 0, 'end': 10}],
        'facts': [{'fact_id': 'f1'}],
        'calculations': [],
        'events': [
            {'kind': 'model', 'finish_reason': 'tool_calls', 'usage': 10, 'usage_detail': {'input_tokens': 4, 'output_tokens': 6}, 'settlement': 'settled'},
            {'kind': 'tool', 'name': 'read_sources', 'status': 'completed', 'rationale': 'legacy read'},
            {'kind': 'submission', 'status': 'completed'},
        ],
    }
    prog = build_progress(operation_id='op-old', operation_status='running', inputs={'question': 'Old Q'},
                          checkpoint=legacy_cp, saved_turn=None, version_id=None, after=0)
    assert prog['current'] == {'kind': 'model', 'phase': None, 'retry_attempt': None, 'started_at': None}
    assert prog['interim'] == {'leads_count': 1, 'acquired_sources_count': 1, 'read_sources_count': 1, 'facts_count': 1, 'calculations_count': 0}
    assert prog['events'][0]['note'] is None
    assert prog['events'][1]['summary'] is None

    legacy_tool_cp = {'pending': {'kind': 'tool_batch', 'cursor': 0, 'calls': [{'id': 'c1', 'name': 'record_facts'}]}}
    assert current_activity(legacy_tool_cp) == {'kind': 'tool', 'name': 'record_facts', 'rationale': None, 'target': None, 'started_at': None}

    # Multi-byte UTF-8 summary is strictly clamped below 2 KB.
    heavy_title = '中' * 160
    heavy_event = {
        'kind': 'tool',
        'name': 'discover_sources',
        'status': 'completed',
        'rationale': '搜索',
        'summary': {
            'kind': 'discover_sources',
            'found_count': 5,
            'eligible_count': 5,
            'denied_count': 0,
            'leads_preview': [
                {'title': heavy_title, 'host': 'investor.example.com', 'published_at': '2026-01-01T00:00:00+00:00'}
                for _ in range(5)
            ],
        },
    }
    sanitized_heavy = sanitize_event(heavy_event)
    assert sanitized_heavy is not None and sanitized_heavy['summary'] is not None
    encoded = json.dumps(sanitized_heavy['summary'], ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    assert len(encoded) < 2048


def test_api_progress_endpoint_exposes_enriched_l1_fields(env):
    sid = env.source_id
    fact_quote = TEXT[:20]

    class RichApiProvider:
        name = 'rich-api-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)

        def __init__(self):
            self.step = 0

        def estimate_turn_input(self, turn_input):
            return 100

        def complete_turn(self, turn_input, **kwargs):
            self.step += 1
            if self.step == 1:
                return ModelResponse(
                    {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'SECRET-INNER', 'signature': 'S'},
                                                      {'type': 'text', 'text': '先读取已上传资料。'}]},
                    (ModelToolCall('c1', 'read_sources', {'reads': [{'source_id': sid}], 'rationale': 'Read source'}),),
                    ModelUsage(10, 5, 15), 'tool_use', 'tool_calls',
                )
            if self.step == 2:
                return ModelResponse(
                    {'role': 'assistant', 'content': [{'type': 'text', 'text': '已阅读资料，记录核心数字。'}]},
                    (ModelToolCall('c2', 'record_facts', {'facts': [
                        {'label': 'revenue', 'value': 100, 'unit': 'USD', 'currency': 'USD', 'period': '2025',
                         'source_id': sid, 'quotation': fact_quote}
                    ], 'rationale': 'Record revenue'}),),
                    ModelUsage(12, 6, 18), 'tool_use', 'tool_calls',
                )
            return ModelResponse(
                {'role': 'assistant', 'content': []},
                (ModelToolCall('c3', 'submit_research', {
                    'report': 'Revenue was 100 USD.',
                    'claims': [{'text': 'Revenue was 100 USD.', 'source_id': sid, 'quotation': fact_quote, 'role': 'support'}],
                    'limitations': [],
                    'stopping_reason': 'answered',
                }),),
                ModelUsage(15, 7, 22), 'tool_use', 'tool_calls',
            )

    env.install(RichApiProvider())
    operation_id = env.run()
    body = progress(env, operation_id)
    assert body['operation_status'] == 'completed'
    assert body['interim'] == {
        'leads_count': 0,
        'acquired_sources_count': 0,
        'read_sources_count': 1,
        'facts_count': 1,
        'calculations_count': 0,
    }
    assert body['events'][0]['note'] == '先读取已上传资料。'
    assert 'SECRET-INNER' not in json.dumps(body)
    assert body['events'][1]['summary']['kind'] == 'read_sources'
    assert body['events'][3]['summary'] == {
        'kind': 'record_facts',
        'recorded_count': 1,
        'facts_preview': [{'label': 'revenue', 'value': '100', 'unit': 'USD', 'currency': 'USD', 'period': '2025'}],
    }
    saved_turn = env.latest()['content']['turns'][-1]
    assert body['events'] == sanitize_events(saved_turn['events'])


def test_acquire_sources_target_and_summary_strip_url_credentials_and_query():
    saved = []
    m = Model([
        turn('acquire_sources', {'urls': ['https://user:SECRET-PASS@investor.example.com/report?token=SECRET-Q'], 'rationale': 'fetch'}, 'a1'),
        ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop'),
    ])
    outcome = run_research_agent(question='Q', sources=[], model=m, context=context(),
                                 checkpoint=lambda s: saved.append(deepcopy(s)))
    activities = [current_activity(cp) for cp in saved if current_activity(cp) is not None]
    dumped = json.dumps({'activities': activities, 'events': sanitize_events(outcome.events)}, ensure_ascii=False)
    assert 'SECRET-PASS' not in dumped and 'SECRET-Q' not in dumped
    assert any(a['kind'] == 'tool' and a['target'] == 'investor.example.com' for a in activities)


def test_saved_turn_interim_deduplicates_repeat_leads_and_acquisitions():
    repeated_events = [
        {
            'kind': 'tool',
            'name': 'discover_sources',
            'status': 'completed',
            'summary': {
                'kind': 'discover_sources',
                'found_count': 1,
                'eligible_count': 1,
                'denied_count': 0,
                'leads_preview': [{'title': '10-K', 'host': 'www.sec.gov', 'published_at': '2025-02-26T00:00:00+00:00'}],
            },
        },
        {
            'kind': 'tool',
            'name': 'discover_sources',
            'status': 'completed',
            'summary': {
                'kind': 'discover_sources',
                'found_count': 1,
                'eligible_count': 1,
                'denied_count': 0,
                'leads_preview': [{'title': '10-K', 'host': 'www.sec.gov', 'published_at': '2025-02-26T00:00:00+00:00'}],
            },
        },
        {
            'kind': 'tool',
            'name': 'acquire_sources',
            'status': 'completed',
            'summary': {
                'kind': 'acquire_sources',
                'acquired_count': 1,
                'failed_count': 0,
                'items': [{'title': '10-K', 'host': 'www.sec.gov', 'status': 'acquired', 'reason': None, 'total_characters': 100}],
            },
        },
        {
            'kind': 'tool',
            'name': 'acquire_sources',
            'status': 'completed',
            'summary': {
                'kind': 'acquire_sources',
                'acquired_count': 1,
                'failed_count': 0,
                'items': [{'title': '10-K', 'host': 'www.sec.gov', 'status': 'acquired', 'reason': None, 'total_characters': 100}],
            },
        },
        {
            'kind': 'tool',
            'name': 'sec_company_concept',
            'status': 'completed',
            'summary': {
                'kind': 'sec_company_concept',
                'entity': 'NVDA',
                'taxonomy': 'us-gaap',
                'tag': 'Revenues',
                'unit': 'USD',
                'status': 'acquired',
                'title': 'NVDA Revenues',
                'total_characters': 50,
            },
        },
        {
            'kind': 'tool',
            'name': 'sec_company_concept',
            'status': 'completed',
            'summary': {
                'kind': 'sec_company_concept',
                'entity': 'NVDA',
                'taxonomy': 'us-gaap',
                'tag': 'Revenues',
                'unit': 'USD',
                'status': 'acquired',
                'title': 'NVDA Revenues',
                'total_characters': 50,
            },
        },
    ]
    saved_turn = ResearchTurn(
        turn_id='t-dedupe',
        index=1,
        question='Q?',
        question_origin='user',
        operation_ids=['op-dedupe'],
        parent_version_id='v1',
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        status='completed',
        reason='submitted',
        usage=TurnUsage(model_calls=1, search_calls=2, total_tokens=10),
        search_status='completed',
        official_fetch_status='completed',
        read_ranges=[ReadRange(source_id='s1', character_start=0, character_end=10), ReadRange(source_id='s1', character_start=10, character_end=20)],
        events=repeated_events,
    )
    prog = build_progress(operation_id='op-dedupe', operation_status='completed', inputs={'question': 'Q?'},
                          checkpoint=None, saved_turn=saved_turn, version_id='v2', after=0)
    assert prog['interim'] == {
        'leads_count': 1,
        'acquired_sources_count': 2,
        'read_sources_count': 1,
        'facts_count': 0,
        'calculations_count': 0,
    }


def test_progress_exposes_whitelisted_context_receipts_without_leaking_secrets():
    import json

    cp = {
        'status': 'awaiting_model',
        'user': json.dumps({'question': 'Q?'}),
        'context_receipts': [
            {
                'trigger': 'l1_in_turn',
                'reason': 'token_watermark',
                'before_chars': 95_000,
                'after_chars': 25_000,
                'chars_saved': 70_000,
                'cleared_results_count': 3,
                'cleared_thinking_turns': 2,
                'cleared_thinking_chars': 12_000,
                'omitted_spans': [{'source_id': 's1', 'start': 0, 'end': 5000}],
                'preserved_facts_count': 4,
                'prefix_reset': False,
                'cleared_tool_call_ids': ['tc-secret-1', 'tc-secret-2', 'tc-secret-3'],
                'call_id': 'call-secret-abc',
                'raw_text': 'TOP_SECRET_PROMPT_LEAK',
                'at': '2026-11-19T22:00:00Z',
            },
            {
                'trigger': 'l2_continuation',
                'reason': 'continuation_compacted',
                'before_chars': 40_000,
                'after_chars': 15_000,
                'chars_saved': 25_000,
                'cleared_results_count': 1,
                'cleared_thinking_turns': 1,
                'cleared_thinking_chars': 3_000,
                'omitted_spans': [{'source_id': 's1', 'start': 5000, 'end': 9000}, {'source_id': 's2', 'start': 0, 'end': 2000}],
                'preserved_facts_count': 6,
                'prefix_reset': True,
                'at': '2026-11-19T22:01:00Z',
            },
        ],
    }
    prog = build_progress(operation_id='op-ctx', operation_status='running', inputs={'question': 'Q?'},
                          checkpoint=cp, saved_turn=None, version_id='v1', after=0)
    receipts = prog['context_receipts']
    assert receipts['count'] == 2
    assert receipts['total_chars_saved'] == 95_000
    assert len(receipts['recent']) == 2
    assert receipts['recent'][0] == {
        'trigger': 'l1_in_turn',
        'reason': 'token_watermark',
        'before_chars': 95_000,
        'after_chars': 25_000,
        'chars_saved': 70_000,
        'cleared_results_count': 3,
        'cleared_thinking_turns': 2,
        'cleared_thinking_chars': 12_000,
        'omitted_spans_count': 1,
        'preserved_facts_count': 4,
        'prefix_reset': False,
        'at': '2026-11-19T22:00:00Z',
    }
    assert receipts['recent'][1]['omitted_spans_count'] == 2
    assert receipts['recent'][1]['prefix_reset'] is True
    serialized = json.dumps(prog)
    for forbidden in ('call-secret-abc', 'tc-secret-1', 'TOP_SECRET_PROMPT_LEAK', 'cleared_tool_call_ids', 'call_id'):
        assert forbidden not in serialized

    empty_prog = build_progress(operation_id='op-empty', operation_status='queued', inputs={'question': 'Q?'},
                                checkpoint=None, saved_turn=None, version_id='v1', after=0)
    assert empty_prog['context_receipts'] == {'count': 0, 'total_chars_saved': 0, 'recent': []}
