"""Phase P1 runtime attention, pre-submit gate, salience compaction, and cache telemetry tests."""
from __future__ import annotations

import json
from dataclasses import asdict
from hashlib import sha256
from datetime import datetime, timezone
from types import SimpleNamespace

from app.anthropic_messages import AnthropicMessagesProvider
from app.model_protocol import ModelResponse, ModelToolCall, ModelTurnInput, ModelUsage, canonical_json_dumps, canonicalize_json_value
from app.openai_compatible import HttpResponse
from app.phase3 import ProviderPolicy
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext
from app.research_coverage_map import model_summary as raw_coverage_model_summary
from app.research_run_insights import WORKING_MEMORY_MAX_CHARS, build_working_memory_anchor
from app.research_runtime import (
    L1_LOW_WATER_RATIO,
    SYSTEM,
    TOOLS,
    _COVERAGE_UPDATING_TOOLS,
    _PROTECTED_COMPACT_TOOLS,
    compact_runtime_history,
    run_research_agent,
)
from app.research_sources import ResearchSource, SourcePermissions

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def _source(source_id: str = 's1', text: str = 'Revenue was 100 USD in FY2025. Gross margin rose 2pp.', *,
            kind: str = 'user_supplied', method: str = 'user_supplied', title: str = 'Filing',
            url: str | None = None) -> ResearchSource:
    return ResearchSource(
        source_id=source_id,
        owner_id='owner',
        title=title,
        text=text,
        kind=kind,
        url=url,
        retrieval_method=method,
        obtained_at=NOW,
        content_sha256=sha256(text.encode()).hexdigest(),
        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True),
    )


def _ctx(*, max_calls: int | None = None, max_tokens: int | None = None, cost_limited: bool = False) -> ResearchExecutionContext:
    return ResearchExecutionContext(
        ResearchExecutionBudget(max_calls, max_tokens, None, float('inf')),
        cost_limited,
        lambda: None,
        lambda: None,
        lambda *_: None,
        lambda *_: None,
        lambda: None,
    )


def _tool_response(calls: list[tuple[str, dict, str]], *, message_extras: dict | None = None,
                   usage: ModelUsage | None = None) -> ModelResponse:
    objs = tuple(ModelToolCall(cid, name, args, json.dumps(args)) for name, args, cid in calls)
    msg = {
        'role': 'assistant',
        'content': None,
        'tool_calls': [
            {'id': o.id, 'type': 'function', 'function': {'name': o.name, 'arguments': o.raw_arguments}}
            for o in objs
        ],
        **(message_extras or {}),
    }
    return ModelResponse(msg, objs, usage or ModelUsage(10, 5, 15), 'tool_calls', stop_class='tool_calls')


from tests.semantic_review_fixture import fixture_review


class NativeTurnRecorder:
    """Native complete_turn stub recording exact ModelTurnInput instances."""

    def __init__(self, script, *, review_verdict='supported'):
        self.review_verdict = review_verdict
        self.review_turns = []
        self._turns = iter(script)
        self.turns: list[ModelTurnInput] = []

    def complete_turn(self, turn: ModelTurnInput, **kwargs) -> ModelResponse:
        if not turn.tools and 'candidates' in json.loads(turn.user):
            self.review_turns.append(turn)
            return fixture_review(turn.user, self.review_verdict)
        self.turns.append(turn)
        nxt = next(self._turns)
        return nxt(turn, len(self.turns) - 1) if callable(nxt) else nxt


def _extract_tail_coverage(tail_note: str) -> dict:
    marker = '[Coverage]\n'
    start = tail_note.index(marker) + len(marker)
    end = tail_note.find('\n[', start)
    raw = tail_note[start:] if end == -1 else tail_note[start:end]
    return json.loads(raw)


def _extract_tail_anchor(tail_note: str) -> str:
    marker = '[Working Memory]\n'
    start = tail_note.index(marker) + len(marker)
    end = tail_note.find('\n[', start)
    return tail_note[start:] if end == -1 else tail_note[start:end]


def test_instruction_during_tool_batch_skips_remaining_calls_with_paired_results():
    source = _source(text='Revenue was 100 USD in FY2025.')
    recorder = NativeTurnRecorder([
        _tool_response([
            ('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'c1'),
            ('search_source', {'source_id': 's1', 'query': 'Revenue'}, 'c2'),
        ]),
        ModelResponse({'role': 'assistant', 'content': 'stop'}, (), ModelUsage(5, 2, 7), 'stop', stop_class='stop'),
    ])
    snapshots = []
    def sync(state, boundary):
        if boundary == 'tool' and any(event.get('tool_call_id') == 'c1' for event in state['events']):
            state['user_instructions'] = [{'sequence': 1, 'text': 'Use the annual figure.'}]
        return bool(state.get('user_instructions')) and state.get('model_instruction_sequence', 0) < 1

    out = run_research_agent(question='Revenue?', sources=[source], model=recorder, context=_ctx(),
                             sync_instructions=sync, checkpoint=lambda state: snapshots.append(json.loads(json.dumps(state))))
    assert out.reason == 'no_submission'
    tool_events = [e for e in out.events if e.get('kind') == 'tool']
    assert [(e['tool_call_id'], e['status']) for e in tool_events] == [('c1', 'completed'), ('c2', 'skipped')]
    assert [item.tool_call_id for item in recorder.turns[1].history[-1].results] == ['c1', 'c2']
    assert snapshots[-1]['acknowledged_instruction_sequence'] == 1


def test_unread_core_followup_has_concrete_nonrepeating_authorized_path():
    from app.research_tool_executor import ToolExecutionContext, _unread_core_path
    source = _source(text='The filing describes revenue of 100 USD.')
    state = {'read_ranges': []}
    ctx = ToolExecutionContext(question='What is revenue?', state=state, sources=[source],
                               source_by_id={'s1': source}, context=_ctx(), as_of=None, seen=set())
    requirements = [{'core': True, 'status': 'missing', 'reason': 'Revenue needs evidence.',
                     'references': [{'source_id': 's1', 'character_start': 0, 'character_end': 12}]}]
    path = _unread_core_path(ctx, requirements)
    assert path['tool'] == 'read_sources'
    assert path['arguments']['reads'][0]['source_id'] == 's1'
    assert _unread_core_path(ctx, requirements)['tool'] == 'search_library'
    assert _unread_core_path(ctx, requirements) is None
    assert _unread_core_path(ctx, [{'core': True, 'status': 'satisfied'}]) is None


def test_proposed_external_followup_respects_capability_budget_and_deduplication():
    from app.research_tool_executor import ToolExecutionContext, _proposed_core_path
    from app.research_tools_registry import SubmissionNextAction
    proposal = SubmissionNextAction(tool='news_search', arguments={'query': 'annual disclosure', 'rationale': 'locate primary evidence'},
                                    requirement='annual disclosure', expected_evidence='A source lead')
    state = {'read_ranges': [], 'events': []}
    ctx = ToolExecutionContext(question='Find the disclosure', state=state, sources=[], source_by_id={},
                               context=_ctx(), as_of=None, seen=set(), news_providers=[object()])
    assert _proposed_core_path(ctx, [proposal])['tool'] == 'news_search'
    assert _proposed_core_path(ctx, [proposal]) is None
    ctx.state['presubmit_paths'] = []
    ctx.news_providers = []
    assert _proposed_core_path(ctx, [proposal]) is None
    ctx.news_providers = [object()]
    ctx.context.budget = ResearchExecutionBudget(None, 0, None, float('inf'))
    assert _proposed_core_path(ctx, [proposal]) is None


def test_instruction_arriving_at_submit_keeps_candidate_as_draft():
    source = _source(text='Revenue was 100 USD in FY2025.')
    recorder = NativeTurnRecorder([
        _tool_response([('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'r1')]),
        _tool_response([('submit_research', {'report': 'The figure is unavailable.', 'claims': [],
                                             'limitations': ['Incomplete.'], 'stopping_reason': 'incomplete',
                                             'status': 'partial'}, 's1')]),
        ModelResponse({'role': 'assistant', 'content': 'stop'}, (), ModelUsage(5, 2, 7), 'stop', stop_class='stop'),
    ])
    snapshots = []
    def sync(state, boundary):
        if boundary == 'submit':
            assert state.get('submission') and state.get('pending') is None
            state['user_instructions'] = [{'sequence': 1, 'text': 'Use the latest period.'}]
        return bool(state.get('user_instructions')) and state.get('model_instruction_sequence', 0) < 1
    result = run_research_agent(question='Summarize the file.', sources=[source], model=recorder,
                                context=_ctx(), sync_instructions=sync,
                                checkpoint=lambda state: snapshots.append(json.loads(json.dumps(state))))
    assert result.reason == 'no_submission'
    assert snapshots[-1]['submission_drafts']
    assert not snapshots[-1].get('submission')
    receipt = recorder.turns[-1].history[-1].results[-1]
    assert receipt.tool_call_id == 's1' and json.loads(receipt.content)['status'] == 'skipped'


def test_submission_records_unresolved_user_instruction_in_delivery():
    recorder = NativeTurnRecorder([_tool_response([('submit_research', {
        'report': 'The requested figure could not be verified.', 'claims': [],
        'limitations': ['No authorized source was available.'], 'stopping_reason': 'sources unavailable',
        'status': 'partial', 'instruction_results': [{'sequence': 1, 'status': 'unresolved',
                                                       'explanation': 'The requested date is not documented.'}],
    }, 's1')])])
    snapshots = []
    def sync(state, boundary):
        state['user_instructions'] = [{'sequence': 1, 'text': 'Use the requested date.'}]
        return boundary != 'model' and state.get('model_instruction_sequence', 0) < 1
    result = run_research_agent(question='Analyze the figure.', sources=[], model=recorder, context=_ctx(),
                                sync_instructions=sync,
                                checkpoint=lambda state: snapshots.append(json.loads(json.dumps(state))))
    assert any('requested date is not documented' in row['message'] for row in result.gaps)
    assert snapshots[-1]['submission']['instruction_reviews'][0]['status'] == 'unresolved'


# --------------------------------------------------------------------------------------------- tail_note & KV-cache prefix

def test_live_tail_note_coverage_and_working_memory_on_tool_updates_and_compaction():
    src = _source(text='Q4 2025 revenue was 100 USD. Guidance noted pricing headwinds.')
    recorder = NativeTurnRecorder([
        _tool_response([('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read release'}, 'c1')]),
        _tool_response([('record_facts', {'facts': [{
            'label': 'revenue', 'value': 100, 'unit': 'USD', 'currency': 'USD', 'period': 'Q4 2025',
            'source_id': 's1', 'quotation': 'Q4 2025 revenue was 100 USD.',
        }], 'rationale': 'record q4 revenue'}, 'c2')]),
        ModelResponse({'role': 'assistant', 'content': 'stop'}, (), ModelUsage(5, 2, 7), 'stop', stop_class='stop'),
    ])
    out = run_research_agent(
        question='Analyze Q4 2025 earnings and EPS',
        sources=[src],
        model=recorder,
        context=_ctx(),
    )
    assert out.reason == 'no_submission'
    assert len(recorder.turns) == 3

    # Turn 0 stays clean without tail_note so Turn 0 is an exact prefix of Turn 1.
    assert recorder.turns[0].tail_note is None

    # Turn 1 (after read_sources) and Turn 2 (after record_facts) inject live Coverage + Working Memory.
    for turn_idx, expected_facts in ((1, 'facts=0'), (2, 'facts=1')):
        note = recorder.turns[turn_idx].tail_note
        assert isinstance(note, str)
        assert '[Coverage]\n' in note and '[Working Memory]\n' in note
        cov_payload = _extract_tail_coverage(note)
        assert 'coverage' in cov_payload
        assert cov_payload['coverage']['categories'][0]['category'] == 'uploads'
        anchor = _extract_tail_anchor(note)
        assert len(anchor) <= WORKING_MEMORY_MAX_CHARS
        assert expected_facts in anchor
        assert 'MethodChecks(earnings_review):' in anchor
        assert 'CounterEvidence:' in anchor

    # Check direct L1-compaction trigger on tail_note (without prior coverage-updating tools).
    long_history = [
        {
            'assistant': {
                'role': 'assistant',
                'content': None,
                'tool_calls': [{'id': f'o{i}', 'type': 'function', 'function': {'name': 'outline_source', 'arguments': '{}'}}],
            },
            'results': [{'tool_call_id': f'o{i}', 'content': json.dumps({'source_id': 's1', 'pad': 'X' * 3_500})}],
        }
        for i in range(4)
    ]
    resumed_state = {
        'user': json.dumps({'question': 'Analyze Q4 2025 earnings'}, ensure_ascii=False),
        'history': long_history,
        'events': [],
        'read_ranges': [],
        'facts': [],
        'calculations': [],
        'method_packs': [{'pack_id': 'earnings_review', 'version': 1, 'fingerprint': 'fp'}],
    }

    class SmallBudgetModel(NativeTurnRecorder):
        policy = SimpleNamespace(max_input_tokens=45_000)

    small_recorder = SmallBudgetModel([
        ModelResponse({'role': 'assistant', 'content': 'done'}, (), ModelUsage(5, 2, 7), 'stop', stop_class='stop'),
    ])
    saved = []
    run_research_agent(
        question='Analyze Q4 2025 earnings',
        sources=[src],
        model=small_recorder,
        context=_ctx(),
        initial_state=resumed_state,
        checkpoint=lambda s: saved.append(json.loads(json.dumps(s))),
    )
    assert saved[-1].get('compaction_count', 0) >= 1
    post_compact_note = small_recorder.turns[0].tail_note
    assert isinstance(post_compact_note, str)
    assert '[Coverage]\n' in post_compact_note and '[Working Memory]\n' in post_compact_note
    assert len(_extract_tail_anchor(post_compact_note)) <= WORKING_MEMORY_MAX_CHARS

    # Oversized anchor scenario remains deterministically bounded <= WORKING_MEMORY_MAX_CHARS.
    huge_eval = {
        'active_packs': ['earnings_review', 'company_comparison', 'macro_release'],
        'required_checks': [
            {'id': f'very_long_requirement_identifier_{i}', 'satisfied': False, 'satisfiable': True}
            for i in range(18)
        ],
        'counter_evidence': {'required_actions': 2, 'completed_actions': 0, 'satisfied': False, 'satisfiable': True},
        'material_conflicts': [
            {'entity': f'Entity{i}LongName', 'metric': f'metric_name_{i}'} for i in range(12)
        ],
        'facts_count': 25,
        'calcs_count': 6,
        'charts_count': 2,
        'sources_count': 7,
        'open_gaps': [f'long_open_gap_code_identifier_{i}' for i in range(15)],
    }
    bounded_a = build_working_memory_anchor({}, {'s1': src}, evaluation=huge_eval)
    bounded_b = build_working_memory_anchor({}, {'s1': src}, evaluation=huge_eval)
    assert bounded_a == bounded_b
    assert len(bounded_a) <= WORKING_MEMORY_MAX_CHARS


def test_kv_cache_prefix_byte_identical_across_turns_and_anthropic_wire(monkeypatch):
    src = _source(text='Revenue was 100 USD in Q4 2025. Margin improved to 40%. Guidance risk: competition.')
    recorder = NativeTurnRecorder([
        _tool_response([('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'c1')]),
        _tool_response([('record_facts', {'facts': [{
            'label': 'revenue', 'value': 100, 'unit': 'USD', 'currency': 'USD', 'period': 'Q4 2025',
            'source_id': 's1', 'quotation': 'Revenue was 100 USD in Q4 2025.',
        }], 'rationale': 'fact'}, 'c2')]),
        _tool_response([('search_source', {'source_id': 's1', 'query': 'competition'}, 'c3')]),
        _tool_response([('submit_research', {
            'report': 'Revenue was 100 USD.',
            'claims': [{'text': 'Revenue was 100 USD in Q4 2025.', 'source_id': 's1',
                        'quotation': 'Revenue was 100 USD in Q4 2025.', 'role': 'support'}],
            'limitations': ['No structured financial statement tool used.'],
            'stopping_reason': 'answered',
        }, 'c4')]),
    ])
    out = run_research_agent(question='Revenue Q4?', sources=[src], model=recorder, context=_ctx())
    assert out.status == 'completed'
    assert len(recorder.turns) == 4

    base_user = recorder.turns[0].user
    base_tools = json.dumps([asdict(t) for t in recorder.turns[0].tools], sort_keys=True)
    for idx, turn_obj in enumerate(recorder.turns):
        assert turn_obj.system is SYSTEM
        assert turn_obj.user == base_user
        assert json.dumps([asdict(t) for t in turn_obj.tools], sort_keys=True) == base_tools
        if idx > 0:
            prev_hist = recorder.turns[idx - 1].history
            curr_prefix = turn_obj.history[:len(prev_hist)]
            assert canonical_json_dumps([asdict(x) for x in curr_prefix]) == canonical_json_dumps([asdict(x) for x in prev_hist])
            # Dynamic state appears strictly in tail_note, never leaking into history/user/system/tools.
            assert isinstance(turn_obj.tail_note, str) and '[Working Memory]' in turn_obj.tail_note
            assert '[Working Memory]' not in json.dumps([asdict(x) for x in turn_obj.history])

    # Verify Anthropic wire layout places cache_control breakpoints only on stable prefix blocks,
    # keeping trailing tail_note out of cached blocks across turns.
    bodies: list[dict] = []

    def fake_transport(method, url, headers, body, timeout, *, diagnostics, **kwargs):
        diagnostics.submission('response_received')
        bodies.append(json.loads(body))
        call_idx = len(bodies)
        if call_idx == 1:
            payload = {'role': 'assistant', 'stop_reason': 'tool_use',
                       'content': [{'type': 'tool_use', 'id': 'a1', 'name': 'read_sources',
                                    'input': {'reads': [{'source_id': 's1'}], 'rationale': 'read'}}],
                       'usage': {'input_tokens': 100, 'output_tokens': 10}}
        elif call_idx == 2:
            payload = {'role': 'assistant', 'stop_reason': 'tool_use',
                       'content': [{'type': 'tool_use', 'id': 'a2', 'name': 'record_facts',
                                    'input': {'facts': [{'label': 'revenue', 'value': 100, 'unit': 'USD',
                                                         'currency': 'USD', 'period': 'Q4 2025',
                                                         'source_id': 's1', 'quotation': 'Revenue was 100 USD in Q4 2025.'}],
                                              'rationale': 'fact'}}],
                       'usage': {'input_tokens': 20, 'output_tokens': 10, 'cache_read_input_tokens': 100}}
        else:
            payload = {'role': 'assistant', 'stop_reason': 'tool_use',
                       'content': [{'type': 'tool_use', 'id': 'a3', 'name': 'submit_research',
                                    'input': {'report': 'Revenue was 100 USD.',
                                              'claims': [{'text': 'Revenue was 100 USD.', 'source_id': 's1',
                                                          'quotation': 'Revenue was 100 USD in Q4 2025.', 'role': 'support'}],
                                              'limitations': [], 'stopping_reason': 'answered'}}],
                       'usage': {'input_tokens': 20, 'output_tokens': 10, 'cache_read_input_tokens': 150}}
        return HttpResponse(200, json.dumps(payload), {})

    monkeypatch.setattr('app.anthropic_messages._default_transport', fake_transport)
    settings = SimpleNamespace(base_url='https://example.com', model='m', api_key='k', allow_private_network=False)
    provider = AnthropicMessagesProvider(settings, ProviderPolicy(retry_base_seconds=0),
                                         anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=200)
    res = run_research_agent(question='Revenue Q4?', sources=[src], model=provider, context=_ctx())
    assert res.status == 'completed' and len(bodies) == 3
    assert bodies[0]['system'] == bodies[1]['system'] == bodies[2]['system']
    assert bodies[0]['tools'] == bodies[1]['tools'] == bodies[2]['tools']
    # Initial user message (bodies[k]['messages'][0]) is cached and stays identical across all turns.
    assert bodies[0]['messages'][0] == bodies[1]['messages'][0] == bodies[2]['messages'][0]
    # Body 1 & Body 2 share the exact same assistant[1] and tool_result block before Body 1's trailing tail_note text block.
    assert bodies[2]['messages'][1] == bodies[1]['messages'][1]
    assert bodies[2]['messages'][2]['content'][0] == bodies[1]['messages'][2]['content'][0]
    tail_block_b1 = bodies[1]['messages'][2]['content'][-1]
    tail_block_b2 = bodies[2]['messages'][4]['content'][-1]
    assert tail_block_b1['text'].startswith('[Coverage]')
    assert tail_block_b2['text'].startswith('[Coverage]')
    assert 'cache_control' not in tail_block_b1 and 'cache_control' not in tail_block_b2


# --------------------------------------------------------------------------------------------- pre-submit gate

def _tool_results_for(turn_input: ModelTurnInput) -> list[dict]:
    if not turn_input.history:
        return []
    return [json.loads(r.content) for r in turn_input.history[-1].results]


def test_presubmit_gate_nudges_once_for_unmet_method_pack_then_accepts():
    src = _source(text='Q4 2025 revenue was 100 USD. Q4 2024 revenue was 80 USD. Risk: margin pressure.')

    # Path A: Model receives needs_attention once, ignores follow-up and resubmits identical args -> accepted on 2nd try.
    identical_submit = {
        'report': 'Q4 2025 revenue was 100 USD.',
        'claims': [{'text': 'Revenue was 100 USD.', 'source_id': 's1',
                    'quotation': 'Q4 2025 revenue was 100 USD.', 'role': 'support'}],
        'limitations': [],
        'stopping_reason': 'answered',
    }
    captured: list[dict] = []
    recorder_a = NativeTurnRecorder([
        _tool_response([('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'r1')]),
        _tool_response([('submit_research', identical_submit, 's_first')]),
        lambda ti, _: (captured.append(_tool_results_for(ti)[-1]),
                       _tool_response([('submit_research', identical_submit, 's_second')]))[1],
    ])
    saved_a = []
    out_a = run_research_agent(
        question='Analyze Q4 2025 earnings and EPS',
        sources=[src],
        model=recorder_a,
        context=_ctx(),
        checkpoint=lambda s: saved_a.append(json.loads(json.dumps(s))),
    )
    assert out_a.status == 'partial'
    assert captured and captured[0]['status'] == 'needs_attention'
    assert captured[0]['presubmit_nudges'] == 1
    kinds = {item['kind'] for item in captured[0]['missing_items']}
    assert 'required_evidence' in kinds and 'counter_evidence' in kinds
    assert saved_a[-1]['presubmit_nudges'] == 1

    # Path B: Model reacts to needs_attention with search_source + calculate_financials, then submits cleanly.
    recorder_b = NativeTurnRecorder([
        _tool_response([('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'r1')]),
        _tool_response([('record_facts', {'facts': [
            {'label': 'revenue', 'value': 100, 'unit': 'USD', 'currency': 'USD', 'period': 'Q4 2025',
             'source_id': 's1', 'quotation': 'Q4 2025 revenue was 100 USD.'},
            {'label': 'revenue', 'value': 80, 'unit': 'USD', 'currency': 'USD', 'period': 'Q4 2024',
             'source_id': 's1', 'quotation': 'Q4 2024 revenue was 80 USD.'},
        ], 'rationale': 'facts'}, 'f1')]),
        _tool_response([('submit_research', identical_submit, 's1_try')]),
        lambda ti, _: _tool_response([
            ('search_source', {'source_id': 's1', 'query': 'Risk'}, 'cnt1'),
            ('calculate_financials', {'calculations': [{'formula': 'growth_percent',
                                                        'first_fact_id': ti and out_fact_id(ti, 0),
                                                        'second_fact_id': ti and out_fact_id(ti, 1)}],
                                      'rationale': 'growth'}, 'calc1'),
        ]),
        _tool_response([('submit_research', identical_submit, 's2_try')]),
    ])

    def out_fact_id(ti: ModelTurnInput, idx: int) -> str:
        res = json.loads(ti.history[1].results[0].content)
        return res['facts'][idx]['fact_id']

    out_b = run_research_agent(
        question='Analyze Q4 2025 earnings and EPS',
        sources=[src],
        model=recorder_b,
        context=_ctx(),
    )
    assert out_b.status == 'partial'
    assert len(out_b.calculations) == 1
    assert any(ev.get('name') == 'search_source' for ev in out_b.events)


def test_presubmit_gate_catches_unaddressed_material_conflict_and_allows_resolution_or_resubmit():
    s1 = _source('s1', 'Alpha revenue 100 USD in 2025.', kind='official', method='sec_companyfacts', title='SEC Filing')
    s2 = _source('s2', 'Alpha revenue 250 USD in 2025.', kind='official', method='web_html',
                 url='https://www.cbsnews.com/x', title='News Summary')

    captured: list[dict] = []
    recorder = NativeTurnRecorder([
        _tool_response([('read_sources', {'reads': [{'source_id': 's1'}, {'source_id': 's2'}], 'rationale': 'read'}, 'r1')]),
        _tool_response([('record_facts', {'facts': [
            {'label': 'Alpha revenue', 'value': 100, 'unit': 'USD', 'currency': 'USD', 'period': '2025',
             'source_id': 's1', 'quotation': 'Alpha revenue 100 USD in 2025.'},
            {'label': 'Alpha revenue', 'value': 250, 'unit': 'USD', 'currency': 'USD', 'period': '2025',
             'source_id': 's2', 'quotation': 'Alpha revenue 250 USD in 2025.'},
        ], 'rationale': 'record conflicting values'}, 'f1')]),
        _tool_response([('submit_research', {
            'report': 'Alpha revenue was 100 USD.',
            'claims': [{'text': 'Revenue was 100 USD.', 'source_id': 's1',
                        'quotation': 'Alpha revenue 100 USD in 2025.', 'role': 'support'}],
            'limitations': [],
            'stopping_reason': 'answered',
        }, 'sub1')]),
        lambda ti, _: (
            captured.append(_tool_results_for(ti)[-1]),
            _tool_response([('submit_research', {
                'report': 'Alpha revenue differed between sources (100 USD vs 250 USD discrepancy).',
                'claims': [
                    {'text': 'Filing states 100 USD.', 'source_id': 's1',
                     'quotation': 'Alpha revenue 100 USD in 2025.', 'role': 'support'},
                    {'text': 'Summary states 250 USD.', 'source_id': 's2',
                     'quotation': 'Alpha revenue 250 USD in 2025.', 'role': 'counterargument'},
                ],
                'limitations': ['Sources disagree on 2025 revenue (100 vs 250 USD).'],
                'stopping_reason': 'answered',
            }, 'sub2')]),
        )[1],
    ])
    out = run_research_agent(question='What is Alpha revenue?', sources=[s1, s2], model=recorder, context=_ctx())
    assert out.status == 'partial'  # The two conflicting values remain unresolved.
    assert len(recorder.turns) == 4
    assert 'MaterialConflicts: ALPHA:revenue' in (recorder.turns[2].tail_note or '')
    assert captured[0]['status'] == 'needs_attention'
    assert captured[0]['unaddressed_conflicts']
    assert captured[0]['unaddressed_conflicts'][0]['metric'] == 'revenue'


def test_presubmit_gate_honors_explicit_gap_acknowledgment_and_resource_limits():
    src = _source(text='Q4 2025 revenue was 100 USD.')

    # 1. Explicit gap acknowledgment in limitations avoids needs_attention entirely.
    ack_recorder = NativeTurnRecorder([
        _tool_response([('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'r1')]),
        _tool_response([('submit_research', {
            'report': 'Q4 2025 revenue was 100 USD.',
            'claims': [{'text': 'Q4 2025 revenue was 100 USD.', 'source_id': 's1',
                        'quotation': 'Q4 2025 revenue was 100 USD.', 'role': 'support'}],
            'limitations': [
                'No SEC XBRL financial statement or filing was provided.',
                'No prior period comparison calculation was possible.',
                'No counter-evidence or alternative view search was possible.',
            ],
            'stopping_reason': 'answered_with_disclosed_gaps',
        }, 's_ack')]),
    ])
    saved_ack = []
    out_ack = run_research_agent(
        question='Analyze Q4 2025 earnings and EPS',
        sources=[src],
        model=ack_recorder,
        context=_ctx(),
        checkpoint=lambda s: saved_ack.append(json.loads(json.dumps(s))),
    )
    assert out_ack.status == 'partial'  # A limitation does not answer the missing EPS.
    assert len(ack_recorder.turns) == 2
    assert not saved_ack[-1].get('presubmit_nudges')

    # 2. Exhausted call budget (max_calls=2 reached on Turn 1 submit) accepts immediately without extra nudge.
    budget_recorder = NativeTurnRecorder([
        _tool_response([('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'r1')]),
        _tool_response([('submit_research', {
            'report': 'Q4 2025 revenue was 100 USD.',
            'claims': [{'text': 'Q4 2025 revenue was 100 USD.', 'source_id': 's1',
                        'quotation': 'Q4 2025 revenue was 100 USD.', 'role': 'support'}],
            'limitations': [],
            'stopping_reason': 'answered',
        }, 's_budget')]),
    ])
    saved_budget = []
    out_budget = run_research_agent(
        question='Analyze Q4 2025 earnings and EPS',
        sources=[src],
        model=budget_recorder,
        context=_ctx(max_calls=2),
        checkpoint=lambda s: saved_budget.append(json.loads(json.dumps(s))),
    )
    assert out_budget.status == 'partial'  # Budget exhaustion retains the same missing core.
    assert len(budget_recorder.turns) == 2
    assert not saved_budget[-1].get('presubmit_nudges')

    # 3. Unavailable tools (no market_data, no search, no eligible sources) do not trigger futile nudges.
    from app.research_run_insights import evaluate_presubmit_gate
    unavail_gate = evaluate_presubmit_gate(
        {
            'user': json.dumps({'question': 'What was the S&P 500 market trend this week?', 'available_capabilities': {}}, ensure_ascii=False),
            'method_packs': [{'pack_id': 'market_trend', 'version': 1, 'fingerprint': 'fp'}],
            'facts': [],
            'calculations': [],
            'events': [],
        },
        {},
        has_fin_data=False,
    )
    assert unavail_gate['needs_attention'] is False
    assert unavail_gate['missing_items'] == []


# --------------------------------------------------------------------------------------------- salience L1 compaction

def test_salience_compaction_tier_ordering_and_hysteresis_with_deterministic_stubs():
    def _exchange(idx: int, name: str, result_payload: dict, *, args: dict | None = None,
                  thinking: str | None = None, is_error: bool = False) -> dict:
        call_id = f'call_{idx}'
        if thinking:
            msg: dict = {
                'role': 'assistant',
                'content': [
                    {'type': 'thinking', 'thinking': thinking, 'signature': 'sig-' + str(idx)},
                    {'type': 'tool_use', 'id': call_id, 'name': name, 'input': args or {}},
                ],
            }
        else:
            msg = {
                'role': 'assistant',
                'content': None,
                'tool_calls': [{'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args or {})}}],
            }
        return {
            'assistant': msg,
            'results': [{'tool_call_id': call_id, 'content': json.dumps(result_payload), **({'is_error': True} if is_error else {})}],
        }

    # Older turns placed intentionally out of tier order:
    # idx 0: Tier 3 - unacquired news_search leads (high salience!)
    # idx 1: Tier 3 - bulky read_sources from s_undigested (no recorded facts yet)
    # idx 2: Tier 2 - low-yield empty news_search
    # idx 3: Tier 1 - consumed thinking + bulky read_sources from s_digested (facts recorded!)
    # idx 4: Tier 1 - news_search whose lead URLs were already acquired
    # idx 5: recent (keep_recent=1) - untouched!
    acquired_url = 'https://example.com/already-acquired'
    unacquired_url = 'https://example.com/unacquired-lead'
    history = [
        _exchange(0, 'news_search', {
            'status': 'completed',
            'leads': [{'url': unacquired_url, 'title': 'Important lead'}],
            'pad': 'L' * 3_400,
        }),
        _exchange(1, 'read_sources', {
            'results': [{'source_id': 's_undigested', 'status': 'read', 'start': 0, 'end': 3400, 'text': 'U' * 3_400}],
        }),
        _exchange(2, 'news_search', {
            'status': 'completed',
            'leads': [],
            'pad': 'E' * 3_400,
        }),
        _exchange(3, 'read_sources', {
            'results': [{'source_id': 's_digested', 'status': 'read', 'start': 0, 'end': 3400, 'text': 'D' * 3_400}],
        }, thinking='Long chain of thought already used on turn 3. ' * 30),
        _exchange(4, 'news_search', {
            'status': 'completed',
            'leads': [{'url': acquired_url, 'title': 'Acquired lead'}],
            'pad': 'A' * 3_400,
        }),
        _exchange(5, 'read_sources', {
            'results': [{'source_id': 's_recent', 'status': 'read', 'start': 0, 'end': 1000, 'text': 'R' * 1_000}],
        }),
    ]
    state = {
        'history': history,
        'facts': [{'fact_id': 'f1', 'source_id': 's_digested', 'label': 'rev', 'value': 100}],
        'acquired_source_ids': ['s_acq'],
        'acquired_lead_sources': {acquired_url: 's_acq'},
        'acquired_actions': {'acquire|' + acquired_url: {'status': 'acquired'}},
        'cache_usage': {'input_tokens': 500, 'cache_read_input_tokens': 300, 'cache_creation_input_tokens': 200, 'cache_hit_ratio': 0.6},
        'events': [],
    }

    # 1. Below trigger -> byte-identical no-op.
    snap = json.dumps(state['history'], sort_keys=True)
    noop = compact_runtime_history(state, keep_recent=1, trigger_chars=80_000, force=False)
    assert noop['compacted'] is False and noop['reason'] == 'below_threshold'
    assert json.dumps(state['history'], sort_keys=True) == snap

    # 2. Cross trigger such that low_water hysteresis saves >= MIN_COMPACTION_SAVINGS_CHARS (8,000)
    #    using Tier 1 (3 items) + Tier 2 (1 item), leaving both Tier 3 items untouched.
    from app.research_runtime import _history_serialized_chars
    total_chars = _history_serialized_chars(history)
    trigger = total_chars - 500
    target_low = int(trigger * L1_LOW_WATER_RATIO)
    assert total_chars - target_low > 8_500

    res = compact_runtime_history(state, keep_recent=1, trigger_chars=trigger, force=False)
    assert res['compacted'] is True
    assert res['cleared_by_tier']['tier1'] == 3  # idx 3 thinking + idx 3 read_sources + idx 4 news_search
    assert res['cleared_by_tier']['tier2'] == 1  # idx 2 empty news_search
    assert res['cleared_by_tier']['tier3'] == 0  # idx 0 unacquired leads & idx 1 undigested read preserved!
    assert res['cleared_call_ids'] == ['call_3', 'call_4', 'call_2']
    assert 'cleared' not in json.loads(state['history'][0]['results'][0]['content'])
    assert 'cleared' not in json.loads(state['history'][1]['results'][0]['content'])
    assert all(b.get('type') != 'thinking' for b in state['history'][3]['assistant']['content'])

    # Deterministic sort_keys=True stub fields: source_ids, source_spans, facts_recorded_count.
    stub_raw = state['history'][3]['results'][0]['content']
    stub = json.loads(stub_raw)
    assert stub_raw == json.dumps(stub, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    assert stub['cleared'] is True
    assert stub['source_ids'] == ['s_digested']
    assert stub['source_spans'] == [{'source_id': 's_digested', 'start': 0, 'end': 3400}]
    assert stub['facts_recorded_count'] == 1

    # Receipt records tier counts & cache snapshot.
    receipt = state['context_receipts'][-1]
    assert receipt['cleared_by_tier'] == res['cleared_by_tier']
    assert receipt['low_water_chars'] == target_low
    assert receipt['cache_usage']['cache_read_input_tokens'] == 300
    assert state['history'][5]['results'][0]['content']  # keep_recent untouched

    # 3. Tighter trigger clears remaining Tier 3 bulk items too (and stops cleanly below hard limit).
    res_t3 = compact_runtime_history(state, keep_recent=1, trigger_chars=4_000, force=False)
    assert res_t3['compacted'] is True
    assert res_t3['cleared_by_tier']['tier3'] == 2
    assert json.loads(state['history'][0]['results'][0]['content'])['cleared'] is True
    assert json.loads(state['history'][1]['results'][0]['content'])['cleared'] is True


# --------------------------------------------------------------------------------------------- cache telemetry & convergence hint

def test_cache_telemetry_accumulated_in_usage_diagnostics_receipts_and_outcome():
    src = _source(text='Revenue was 100 USD in FY2025.')
    recorder = NativeTurnRecorder([
        _tool_response(
            [('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'c1')],
            usage=ModelUsage(input_tokens=100, output_tokens=20, total_tokens=120,
                             cache_creation_input_tokens=80, cache_read_input_tokens=0),
        ),
        _tool_response(
            [('submit_research', {
                'report': 'Revenue was 100 USD.',
                'claims': [{'text': 'Revenue was 100 USD.', 'source_id': 's1',
                            'quotation': 'Revenue was 100 USD in FY2025.', 'role': 'support'}],
                'limitations': [], 'stopping_reason': 'answered',
            }, 'c2')],
            usage=ModelUsage(input_tokens=15, output_tokens=25, total_tokens=40,
                             cache_creation_input_tokens=0, cache_read_input_tokens=180),
        ),
    ])
    saved: list[dict] = []
    ctx = _ctx()
    out = run_research_agent(
        question='What was revenue?',
        sources=[src],
        model=recorder,
        context=ctx,
        checkpoint=lambda s: saved.append(json.loads(json.dumps(s))),
    )
    assert out.status == 'completed'
    # Normalized total input tokens per ModelUsage: Call 1 (tot=120, out=20) = 100, Call 2 (tot=40 < 15+180) = 195; sum = 295. The independent review adds 4 input and 3 output tokens.
    expected = {
        'input_tokens': 299,
        'output_tokens': 48,
        'cache_read_input_tokens': 180,
        'cache_creation_input_tokens': 80,
        'cache_hit_ratio': round(180 / 295, 6),
        'calls_with_usage': 3,
        'calls_with_cache_telemetry': 2,
    }
    assert out.cache_usage == expected
    assert saved[-1]['cache_usage'] == expected
    assert [r['submission_state'] for r in saved[-1]['_context_input_records']] == ['response_received', 'response_received']
    assert all(r['stage'] == 'constructed' for r in saved[-1]['_context_input_records'])

    receipts = saved[-1]['model_receipts']
    assert len(receipts) == 3
    assert receipts[2]['purpose'] == 'semantic_review'
    assert receipts[0]['cache_read_input_tokens'] == 0 and receipts[0]['cache_creation_input_tokens'] == 80
    assert receipts[1]['cache_read_input_tokens'] == 180 and receipts[1]['input_tokens'] == 195
    assert receipts[1]['cache_hit_ratio'] == 180 / 195

    diag_items = list(ctx.private_diagnostics.values())
    assert len(diag_items) == 3
    assert sum(':semantic-review:' in key for key in ctx.private_diagnostics) == 1
    assert diag_items[1]['cache_telemetry']['cache_read_input_tokens'] == 180
    assert diag_items[2]['cumulative_cache_usage'] == expected

    model_events = [ev for ev in out.events if ev.get('kind') == 'model']
    assert [ev['cache_telemetry']['cache_read_input_tokens'] for ev in model_events] == [0, 180]


def test_three_consecutive_zero_yield_turns_emit_tail_note_convergence_hint():
    src = _source(text='Revenue was 100 USD in FY2025. Costs were 60 USD.')
    recorder = NativeTurnRecorder([
        # Turn 0 -> zero yield #1
        _tool_response([('outline_source', {'source_id': 's1'}, 'o1')]),
        # Turn 1 -> zero yield #2
        _tool_response([('search_source', {'source_id': 's1', 'query': 'Revenue'}, 'q1')]),
        # Turn 2 -> zero yield #3
        _tool_response([('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'r1')]),
        # Turn 3 receives convergence hint, records a fact -> resets zero_yield_turns!
        _tool_response([('record_facts', {'facts': [{
            'label': 'revenue', 'value': 100, 'unit': 'USD', 'currency': 'USD', 'period': 'FY2025',
            'source_id': 's1', 'quotation': 'Revenue was 100 USD in FY2025.',
        }], 'rationale': 'record'}, 'f1')]),
        # Turn 4 sees convergence hint cleared!
        _tool_response([('submit_research', {
            'report': 'Revenue was 100 USD.',
            'claims': [{'text': 'Revenue was 100 USD.', 'source_id': 's1',
                        'quotation': 'Revenue was 100 USD in FY2025.', 'role': 'support'}],
            'limitations': [], 'stopping_reason': 'answered',
        }, 'sub')]),
    ])
    saved: list[dict] = []
    out = run_research_agent(
        question='What was revenue?',
        sources=[src],
        model=recorder,
        context=_ctx(),
        checkpoint=lambda s: saved.append(json.loads(json.dumps(s))),
    )
    assert out.status == 'completed'
    assert len(recorder.turns) == 5
    assert recorder.turns[0].tail_note is None
    assert recorder.turns[1].tail_note is None  # outline_source only (1 zero-yield turn)
    assert recorder.turns[2].tail_note is None  # search_source only (2 zero-yield turns)
    assert isinstance(recorder.turns[3].tail_note, str)
    assert '[Convergence]' in recorder.turns[3].tail_note
    assert 'submit_research' in recorder.turns[3].tail_note
    assert isinstance(recorder.turns[4].tail_note, str)
    assert '[Convergence]' not in recorder.turns[4].tail_note
    assert saved[-1]['zero_yield_turns'] == 0


# --------------------------------------------------------------------------------------------- dynamic mid-run method pack activation

def test_activate_method_pack_tool_is_immutable_canonical_and_activates_checks_mid_run():
    from app.research_methods import MAX_PACK_CHARS, get_pack, pack_fingerprint

    assert isinstance(TOOLS, tuple)
    names = [t.name for t in TOOLS]
    assert 'activate_method_pack' in names
    assert names.index('activate_method_pack') < names.index('submit_research')
    assert names[-1] == 'submit_research'
    assert 'activate_method_pack' in _COVERAGE_UPDATING_TOOLS
    assert 'activate_method_pack' in _PROTECTED_COMPACT_TOOLS

    act_tool = next(t for t in TOOLS if t.name == 'activate_method_pack')
    assert act_tool.input_schema == canonicalize_json_value(act_tool.input_schema)
    assert 'METHOD PACK' not in act_tool.description

    src = _source(text='Q4 2025 revenue was 100 USD. Guidance noted margin risk.')
    earnings_pack = get_pack('earnings_review')
    assert earnings_pack is not None
    expected_fp = pack_fingerprint(earnings_pack)

    captured_results: list[dict] = []
    recorder = NativeTurnRecorder([
        # Turn 0: generic question had no initial method pack; model activates earnings_review mid-run.
        _tool_response([('activate_method_pack', {'pack_id': 'earnings_review', 'rationale': 'question turns out to be earnings'}, 'act1')]),
        # Turn 1: read + record facts, then try submit without counter-evidence or calcs -> blocked by pre-submit gate!
        lambda ti, _: (
            captured_results.append(_tool_results_for(ti)[-1]),
            _tool_response([('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'r1')]),
        )[1],
        _tool_response([('submit_research', {
            'report': 'Q4 2025 revenue was 100 USD.',
            'claims': [{'text': 'Q4 2025 revenue was 100 USD.', 'source_id': 's1',
                        'quotation': 'Q4 2025 revenue was 100 USD.', 'role': 'support'}],
            'limitations': [],
            'stopping_reason': 'answered',
        }, 'sub1')]),
        lambda ti, _: (
            captured_results.append(_tool_results_for(ti)[-1]),
            _tool_response([('submit_research', {
                'report': 'Q4 2025 revenue was 100 USD.',
                'claims': [{'text': 'Q4 2025 revenue was 100 USD.', 'source_id': 's1',
                            'quotation': 'Q4 2025 revenue was 100 USD.', 'role': 'support'}],
                'limitations': [],
                'stopping_reason': 'answered',
            }, 'sub2')]),
        )[1],
    ], review_verdict='weaker')
    saved: list[dict] = []
    out = run_research_agent(
        question='Summarize this uploaded file.',
        sources=[src],
        model=recorder,
        context=_ctx(),
        checkpoint=lambda s: saved.append(json.loads(json.dumps(s))),
    )
    assert out.status == 'partial'  # One revenue claim does not summarize the whole filing.
    assert len(recorder.turns) == 4

    # Initial user message had NO method_packs; KV-cache prefix stayed byte-identical across every turn!
    base_user = recorder.turns[0].user
    assert 'method_packs' not in json.loads(base_user)
    base_tools = canonical_json_dumps([asdict(t) for t in recorder.turns[0].tools])
    for idx, turn_obj in enumerate(recorder.turns):
        assert turn_obj.system is SYSTEM
        assert turn_obj.user == base_user
        assert canonical_json_dumps([asdict(t) for t in turn_obj.tools]) == base_tools
        if idx > 0:
            prev_hist = recorder.turns[idx - 1].history
            curr_prefix = turn_obj.history[:len(prev_hist)]
            assert canonical_json_dumps([asdict(x) for x in curr_prefix]) == canonical_json_dumps([asdict(x) for x in prev_hist])

    # Activation tool result contains bounded rendered pack text + metadata
    act_res = captured_results[0]
    assert act_res['status'] == 'activated'
    assert act_res['pack_id'] == 'earnings_review'
    assert act_res['fingerprint'] == expected_fp
    assert len(act_res['method_pack_text']) <= MAX_PACK_CHARS
    assert 'METHOD PACK earnings_review@' in act_res['method_pack_text']
    assert act_res['min_search_actions'] == earnings_pack.counter_evidence.min_search_actions

    # Turn 1 tail_note immediately incorporates the newly activated pack's checks & counter-evidence!
    assert isinstance(recorder.turns[1].tail_note, str)
    anchor_t1 = _extract_tail_anchor(recorder.turns[1].tail_note)
    assert 'MethodChecks(earnings_review):' in anchor_t1
    assert f'CounterEvidence: needed(0/{earnings_pack.counter_evidence.min_search_actions})' in anchor_t1

    # Pre-submit gate immediately enforced the newly activated pack before resubmit accepted!
    gate_res = captured_results[1]
    assert gate_res['status'] == 'needs_attention'
    assert any(item['kind'] == 'counter_evidence' for item in gate_res['missing_items'])

    # Activated pack fingerprint recorded in state & RuntimeOutcome
    assert len(out.method_packs) == 1
    assert out.method_packs[0]['pack_id'] == 'earnings_review'
    assert out.method_packs[0]['fingerprint'] == expected_fp
    assert out.method_packs[0]['activated_mid_run'] is True


def test_activate_method_pack_handles_unknown_pack_and_deduplicates_existing():
    src = _source(text='Revenue was 100 USD.')
    captured: list[dict] = []
    recorder = NativeTurnRecorder([
        _tool_response([('activate_method_pack', {'pack_id': 'no_such_pack', 'rationale': 'try bad'}, 'bad')]),
        lambda ti, _: (
            captured.append(_tool_results_for(ti)[-1]),
            _tool_response([('activate_method_pack', {'pack_id': 'earnings_review', 'rationale': 'already active'}, 'dup')]),
        )[1],
        lambda ti, _: (
            captured.append(_tool_results_for(ti)[-1]),
            ModelResponse({'role': 'assistant', 'content': 'stop'}, (), ModelUsage(5, 2, 7), 'stop', stop_class='stop'),
        )[1],
    ])
    out = run_research_agent(
        question='Analyze Q4 2025 earnings and EPS',
        sources=[src],
        model=recorder,
        context=_ctx(),
    )
    assert captured[0]['status'] == 'invalid'
    assert 'Unknown method pack' in captured[0]['reason']
    assert captured[1]['status'] == 'already_active'
    assert len(out.method_packs) == 1
    assert out.method_packs[0]['pack_id'] == 'earnings_review'
