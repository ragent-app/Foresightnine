import json
from hashlib import sha256
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.model_protocol import ModelResponse, ModelToolCall, ModelUsage
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext
from app.research_runtime import run_research_agent
from app.research_sources import ResearchSource, SourcePermissions


def source(url=None, allowed=True):
    return ResearchSource(source_id='s1', owner_id='owner', title='Disclosure', text='Revenue was 100 USD.',
                          url=url, kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256=sha256(b'Revenue was 100 USD.').hexdigest(),
                          permissions=SourcePermissions(store=True, model_input=allowed, display=True, export=True))


def context():
    return ResearchExecutionContext(ResearchExecutionBudget(None, None, None, float('inf')), False,
        lambda: None, lambda: None, lambda *_: None, lambda *_: None, lambda: None)


def turn(name, args, ident):
    call = ModelToolCall(ident, name, json.dumps(args))
    return ModelResponse({'role': 'assistant', 'content': None, 'tool_calls': [{'id': ident, 'type': 'function',
        'function': {'name': name, 'arguments': call.arguments}}]}, (call,), ModelUsage(1, 1, 2), 'tool_calls')


class Model:
    def __init__(self, turns):
        self.turns = iter(turns)
        self.inputs = []
        self.review_verdict = 'insufficient'
    def complete(self, messages, tools, **kwargs):
        if not tools and 'candidates' in json.loads(messages[1]['content']):
            from tests.semantic_review_fixture import fixture_review
            return fixture_review(messages[1]['content'], self.review_verdict)
        self.inputs.append(json.loads(json.dumps(messages)))
        return next(self.turns)


def _last_tool_msg(messages):
    return [msg for msg in messages if msg.get('role') == 'tool'][-1]


def _last_tool(model, index=-1):
    return _last_tool_msg(model.inputs[index])


def test_read_feedback_and_submission_uses_call_association_and_server_ids():
    m = Model([turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'financial disclosure'}, 'a'),
               turn('submit_research', {'report': 'Revenue was 100 USD.', 'claims': [{'text': 'Revenue was 100 USD.', 'source_id': 's1',
                    'quotation': 'Revenue was 100 USD.', 'character_start': 0, 'character_end': 20, 'role': 'support'}], 'limitations': [], 'stopping_reason': 'answered'}, 'b')])
    m.review_verdict = 'supported'
    saved = []
    result = run_research_agent(question='Revenue?', sources=[source()], model=m, context=context(),
                                checkpoint=lambda s: saved.append(json.loads(json.dumps(s))))
    assert result.status == 'completed'
    assert result.claims[0]['claim_id'].startswith('claim-')
    assert _last_tool(m, 1)['tool_call_id'] == 'a'
    assert 'Revenue was 100 USD.' in _last_tool(m, 1)['content']
    assert any(s['pending'] and s['pending']['kind'] == 'model' for s in saved)
    assert saved[-1]['submission']['report'] == result.report


def test_fred_and_missing_permission_are_denied_at_read():
    for item in (source('https://fred.stlouisfed.org/series/GDP'), source(allowed=False)):
        m = Model([turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'a'),
                   ModelResponse({'role': 'assistant', 'content': 'Cannot proceed.'}, (), None, 'stop')])
        result = run_research_agent(question='Question', sources=[item], model=m, context=context())
        assert result.status == 'partial'
        assert 'denied_or_unavailable' in _last_tool(m, 1)['content']
        assert not result.source_ids


def test_unknown_external_result_is_not_replayed():
    m = Model([])
    state = {'messages': [], 'events': [], 'read_ranges': [], 'calculations': [], 'pending': {'kind': 'model', 'call_id': 'x'}}
    result = run_research_agent(question='Question', sources=[], model=m, context=context(), initial_state=state)
    assert result.reason == 'external_result_unknown'
    assert not m.inputs


def test_facts_and_calculation_are_source_linked():
    src = source()
    src.text = 'Revenue was 100 USD. Revenue was 80 USD.'
    from hashlib import sha256
    src.content_sha256 = sha256(src.text.encode()).hexdigest()
    m = Model([turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'compare'}, 'a'),
        turn('record_facts', {'facts': [
            {'label': 'revenue', 'value': 100, 'unit': 'USD', 'currency': 'USD', 'period': '2025', 'source_id': 's1',
             'quotation': 'Revenue was 100 USD.', 'character_start': 0, 'character_end': 20, 'content_sha256': src.content_sha256},
            {'label': 'revenue', 'value': 80, 'unit': 'USD', 'currency': 'USD', 'period': '2024', 'source_id': 's1',
             'quotation': 'Revenue was 80 USD.', 'character_start': 21, 'character_end': 40, 'content_sha256': src.content_sha256}], 'rationale': 'inputs'}, 'b')])
    original = m.complete
    # Record the subsequent calls as well as the two scripted turns.
    def wrapped(messages, tools, **kwargs):
        if len(m.inputs) < 2:
            return original(messages, tools, **kwargs)
        count = len(m.inputs)
        m.inputs.append(json.loads(json.dumps(messages)))
        if count == 2:
            facts = json.loads(_last_tool_msg(messages)['content'])['facts']
            return turn('calculate_financials', {'calculations': [{'formula': 'growth_percent',
                'first_fact_id': facts[0]['fact_id'], 'second_fact_id': facts[1]['fact_id']}], 'rationale': 'growth'}, 'c')
        return ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')
    m.complete = wrapped
    result = run_research_agent(question='Growth?', sources=[src], model=m, context=context())
    assert result.status == 'partial'
    assert len(result.facts) == 2
    assert result.calculations[0]['value'] == '25.00'
    assert result.calculations[0]['inputs'][0]['fact_id'] == result.facts[0]['fact_id']


def test_settlement_failure_saves_response_and_recovery_does_not_resubmit():
    m = Model([turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'a')])
    c = context()
    original_settle = c.settle
    c.settle = lambda *_: (_ for _ in ()).throw(RuntimeError('ledger unavailable'))
    saved = []
    result = run_research_agent(question='Q', sources=[source()], model=m, context=c,
                                checkpoint=lambda s: saved.append(json.loads(json.dumps(s))))
    assert result.reason == 'settlement_failed'
    snapshot = saved[-1]
    assert snapshot['pending']['kind'] == 'model_returned'
    c.settle = original_settle
    m.turns = iter([ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
    resumed = run_research_agent(question='Q', sources=[source()], model=m, context=c, initial_state=snapshot)
    assert resumed.reason == 'no_submission'
    assert len(m.inputs) == 2  # The second request follows the recovered read; the first is never replayed.
    tool_msgs = [msg for msg in m.inputs[-1] if msg.get('role') == 'tool']
    assert tool_msgs and tool_msgs[-1]['tool_call_id'] == 'a'
    assert 'settlement_failed' in m.inputs[-1][-1]['content']


def test_tool_batch_cursor_resumes_without_model_replay():
    call = turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'a')
    saved = {'user': 'Q', 'history': [{'assistant': call.message, 'results': []}], 'events': [], 'read_ranges': [], 'facts': [], 'calculations': [],
             'pending': {'kind': 'tool_batch', 'call_id': 'model1', 'cursor': 0,
                         'calls': [{'id': c.id, 'name': c.name, 'arguments': json.loads(c.arguments), 'raw_arguments': c.arguments} for c in call.tool_calls]}}
    m = Model([ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
    result = run_research_agent(question='Q', sources=[source()], model=m, context=context(), initial_state=saved)
    assert result.reason == 'no_submission'
    assert len(m.inputs) == 1
    assert _last_tool(m, 0)['tool_call_id'] == 'a'


def test_same_action_with_changed_rationale_is_no_progress():
    m = Model([turn('news_search', {'query': 'Q', 'rationale': 'first'}, 'a'),
               turn('news_search', {'query': 'Q', 'rationale': 'different wording'}, 'b'),
               turn('news_search', {'query': 'Q', 'rationale': 'third wording'}, 'c')])
    result = run_research_agent(question='Q', sources=[], model=m, context=context())
    assert result.reason == 'no_progress'


def test_cancel_before_action_does_not_call_provider():
    c = context()
    c.ensure_running = lambda: (_ for _ in ()).throw(RuntimeError('cancelled'))
    m = Model([])
    try:
        run_research_agent(question='Q', sources=[], model=m, context=c)
    except RuntimeError as error:
        assert str(error) == 'cancelled'
    else:
        assert False, 'Cancellation should propagate to operation lifecycle.'
    assert not m.inputs


def test_unconfirmed_failure_usage_is_not_trusted():
    from app.phase3 import ProviderFailure
    class FailedModel:
        def complete(self, messages, tools, **kwargs):
            raise ProviderFailure('provider_timeout', 'unknown result', retryable=True, reported_tokens=17)
    saved = []
    c = context()
    result = run_research_agent(question='Q', sources=[], model=FailedModel(), context=c,
                                checkpoint=lambda s: saved.append(json.loads(json.dumps(s))))
    assert result.status == 'partial'
    assert c.usage is None
    assert saved[-1]['pending']['kind'] == 'model'


def test_quotation_offsets_are_resolved_and_ambiguous_matches_are_rejected():
    submit = {'report': 'Revenue.', 'claims': [{'text': 'Revenue', 'source_id': 's1', 'quotation': 'Revenue was 100 USD.', 'role': 'support'}],
              'limitations': [], 'stopping_reason': 'answered'}
    m = Model([turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'a'),
               turn('submit_research', submit, 'b')])
    m.review_verdict = 'supported'
    result = run_research_agent(question='Q', sources=[source()], model=m, context=context())
    assert result.claims[0]['citations'][0]['location'] == {'character_start': 0, 'character_end': 20}
    repeated = source()
    repeated.text = repeated.text + ' ' + repeated.text
    repeated.content_sha256 = sha256(repeated.text.encode()).hexdigest()
    m = Model([turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'a'),
               turn('submit_research', submit, 'b'), ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
    result = run_research_agent(question='Q', sources=[repeated], model=m, context=context())
    assert result.status == 'partial'
    assert 'candidate offsets' in _last_tool(m)['content']


def test_known_failed_response_is_saved_and_settled():
    from app.phase3 import ProviderFailure
    class FailedModel:
        def complete(self, messages, tools, **kwargs):
            error = ProviderFailure('invalid_provider_output', 'invalid response', retryable=False, reported_tokens=7)
            error.provider_response_received = True
            raise error
    saved = []
    c = context()
    result = run_research_agent(question='Q', sources=[], model=FailedModel(), context=c,
                                checkpoint=lambda s: saved.append(json.loads(json.dumps(s))))
    assert result.reason == 'invalid_provider_output'
    assert any((s.get('pending') or {}).get('kind') == 'model_returned' for s in saved)
    assert saved[-1]['model_receipts'][0]['settlement'] == 'settled'
    assert c.usage == 7


def test_acquire_known_failure_details_are_model_feedback():
    lead = {'url': 'https://example.com/report', 'title': 'Report'}
    m = Model([turn('news_search', {'query': 'report', 'rationale': 'find'}, 'a'),
               turn('acquire_sources', {'urls': [lead['url']], 'rationale': 'read'}, 'b'),
               ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
    result = run_research_agent(question='Q', sources=[], model=m, context=context(), discover=lambda _: [lead],
        acquire=lambda *_: (None, {'status': 'failed', 'notes': ['Unavailable'],
                                   'outcomes': [{'status': 'failed', 'external_state': 'returned'}]}))
    assert result.reason == 'no_submission'
    assert 'Unavailable' in _last_tool(m)['content']


def test_repeated_facts_reuse_id_and_partial_batch_commits_atomically():
    fact = {'label': 'revenue', 'value': 100, 'unit': 'USD', 'currency': 'USD', 'period': '2025',
            'source_id': 's1', 'quotation': 'Revenue was 100 USD.'}
    invalid = {**fact, 'value': 999}
    m = Model([turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'a'),
               turn('record_facts', {'facts': [fact, invalid], 'rationale': 'bad batch'}, 'b'),
               turn('record_facts', {'facts': [fact, fact], 'rationale': 'first'}, 'c'),
               turn('record_facts', {'facts': [fact, fact], 'rationale': 'again'}, 'd'),
               turn('record_facts', {'facts': [fact, fact], 'rationale': 'different'}, 'e'),
               turn('record_facts', {'facts': [fact, fact], 'rationale': 'yet another'}, 'f')])
    result = run_research_agent(question='Q', sources=[source()], model=m, context=context())
    assert result.reason == 'no_progress'
    assert len(result.facts) == 1
    first = json.loads(_last_tool(m, 2)['content'])
    second = json.loads(_last_tool(m, 3)['content'])['facts']
    assert len(first['failures']) == 1
    assert first['facts'][0]['fact_id'] == second[0]['fact_id'] == second[1]['fact_id'] == result.facts[0]['fact_id']


def test_malformed_arguments_first_and_later_do_not_reuse_rationale():
    malformed = ModelToolCall('bad', 'read_sources', '{bad')
    bad_turn = ModelResponse({'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'bad', 'type': 'function',
        'function': {'name': 'read_sources', 'arguments': '{bad'}}]}, (malformed,), ModelUsage(1, 1, 2), 'tool_calls')
    for turns in ([bad_turn], [turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'private previous rationale'}, 'ok'), bad_turn]):
        m = Model([*turns, ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
        result = run_research_agent(question='Q', sources=[source()], model=m, context=context())
        assert result.status == 'partial'
        assert json.loads(_last_tool(m)['content'])['status'] == 'invalid'
        event = next(e for e in result.events if e.get('tool_call_id') == 'bad')
        assert event['rationale'] is None


def test_fred_search_leads_are_removed_before_model_feedback_and_checkpoint():
    leads = [{'url': 'https://fred.stlouisfed.org/series/GDP', 'snippet': 'FRED SECRET CONTENT'},
             {'url': 'https://alfred.stlouisfed.org/series/GDP', 'snippet': 'ALFRED SECRET CONTENT'},
             {'url': 'https://api.stlouisfed.org/fred/series', 'snippet': 'API SECRET CONTENT'},
             {'url': 'https://example.com/report', 'title': 'Other'}]
    m = Model([turn('news_search', {'query': 'economy', 'rationale': 'discover'}, 'a'),
               ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
    saved = []
    outcome = run_research_agent(question='Q', sources=[], model=m, context=context(), discover=lambda _: leads,
                                 checkpoint=lambda s: saved.append(json.loads(json.dumps(s))))
    event = next(e for e in outcome.events if e.get('name') == 'news_search')
    assert event['denied_count'] == 3
    assert event['denied_reasons']['source_not_authorized'] == 3
    feedback = json.loads(_last_tool(m)['content'])
    assert feedback['denied_count'] == 3
    assert len(feedback['leads']) == 1
    assert 'SECRET CONTENT' not in json.dumps(saved)


def test_repeated_invalid_calls_stop_without_hidden_retry_limit():
    malformed = ModelToolCall('bad', 'read_sources', '{bad')
    bad_turn = ModelResponse({'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'bad', 'type': 'function',
        'function': {'name': 'read_sources', 'arguments': '{bad'}}]}, (malformed,), ModelUsage(1, 1, 2), 'tool_calls')
    for turns in ([bad_turn, bad_turn, bad_turn],
                  [turn('read_sources', {'reads': [], 'rationale': 'first'}, 'a'),
                   turn('read_sources', {'reads': [], 'rationale': 'changed'}, 'b'),
                   turn('read_sources', {'reads': [], 'rationale': 'changed again'}, 'c')]):
        result = run_research_agent(question='Q', sources=[], model=Model(turns), context=context())
        assert result.reason == 'no_progress'


def test_distinct_labels_in_same_quote_remain_distinct_facts():
    src = source()
    src.text = 'Revenue and cost were 100 USD.'
    base = {'value': 100, 'unit': 'USD', 'currency': 'USD', 'period': '2025',
            'source_id': 's1', 'quotation': src.text}
    m = Model([turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'a'),
               turn('record_facts', {'facts': [{**base, 'label': 'revenue'}, {**base, 'label': 'cost'}],
                                     'rationale': 'record both'}, 'b'),
               ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
    result = run_research_agent(question='Q', sources=[src], model=m, context=context())
    assert len(result.facts) == 2
    assert result.facts[0]['fact_id'] != result.facts[1]['fact_id']


def test_calculation_batch_is_atomic_when_later_item_is_invalid():
    facts = [{'fact_id': 'f1', 'value': '100', 'unit': 'USD', 'currency': 'USD', 'period': '2025'},
             {'fact_id': 'f2', 'value': '80', 'unit': 'USD', 'currency': 'USD', 'period': '2024'}]
    state = {'user': 'Q', 'history': [], 'events': [], 'read_ranges': [], 'facts': facts, 'calculations': [], 'pending': None}
    m = Model([turn('calculate_financials', {'calculations': [
        {'formula': 'growth_percent', 'first_fact_id': 'f1', 'second_fact_id': 'f2'},
        {'formula': 'difference', 'first_fact_id': 'missing', 'second_fact_id': 'f2'}], 'rationale': 'calculate'}, 'a'),
        ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
    result = run_research_agent(question='Q', sources=[], model=m, context=context(), initial_state=state)
    assert result.calculations == []
    assert json.loads(m.inputs[-1][-1]['content'])['status'] == 'invalid'


def test_neutral_provider_truncated_tool_call_never_runs():
    class Neutral:
        def estimate_turn_input(self, turn):
            return 10
        def complete_turn(self, turn, **kwargs):
            return ModelResponse({'opaque': ['private']}, (ModelToolCall('a', 'news_search',
                {'query': 'q', 'rationale': 'r'}),), ModelUsage(1, 1, 2), 'max_tokens', 'truncated')
    seen = []
    result = run_research_agent(question='Q', sources=[], model=Neutral(), context=context(),
                                discover=lambda q: seen.append(q))
    assert result.reason == 'incomplete_model_response'
    assert seen == []
    assert result.messages == []


def test_neutral_provider_ordered_results_and_opaque_continuation():
    class Neutral:
        def __init__(self):
            self.turns = []
        def estimate_turn_input(self, turn):
            return 10
        def complete_turn(self, turn, **kwargs):
            self.turns.append(turn)
            if len(self.turns) == 1:
                return ModelResponse({'opaque': ['first', {'x': 1}]}, (
                    ModelToolCall('a', 'read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'one'}),
                    ModelToolCall('b', 'read_sources', {'reads': [{'source_id': 'missing'}], 'rationale': 'two'})),
                    ModelUsage(1, 1, 2), 'tool_use', 'tool_calls')
            return ModelResponse({'opaque': []}, (), None, 'end_turn', 'end')
    model = Neutral()
    result = run_research_agent(question='Q', sources=[source()], model=model, context=context())
    assert result.reason == 'no_submission'
    exchange = model.turns[1].history[0]
    assert exchange.assistant.envelope == {'opaque': ['first', {'x': 1}]}
    assert [item.tool_call_id for item in exchange.results] == ['a', 'b']
    assert result.messages == []


def test_chat_bridge_preserves_not_started_diagnostics_and_releases_reservation():
    from app.phase3 import ProviderFailure
    class FailedBeforeSubmission:
        def complete(self, messages, tools, *, cancel_event=None, diagnostic_callback=None):
            diagnostic_callback({'submission_state': 'not_started', 'stages': []})
            raise ProviderFailure('provider_configuration_invalid', 'not submitted', retryable=False)
    snapshots = []
    c = context()
    result = run_research_agent(question='Q', sources=[], model=FailedBeforeSubmission(), context=c,
        checkpoint=lambda state: snapshots.append(json.loads(json.dumps(state))))
    assert result.reason == 'provider_configuration_invalid'
    assert snapshots[-1]['pending'] is None
    assert snapshots[-1]['events'][-1]['external_state'] == 'not_started'
    assert c.calls == 0


@pytest.mark.parametrize(('failure_stage', 'failures', 'expected_status', 'expected_attempts'), [
    ('dns', 1, 'partial', 3),
    ('dns', 3, 'partial', 4),
    # A TLS handshake failure is still before any request byte is written (362 acceptance).
    ('tls', 1, 'partial', 3),
    ('tls', 2, 'partial', 4),
    ('request_write', 1, 'partial', 2),
])
def test_messages_retries_only_the_unsent_turn(monkeypatch, failure_stage, failures, expected_status, expected_attempts):
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRIES', '2')
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRY_BASE_SECONDS', '0')
    from app.anthropic_messages import AnthropicMessagesProvider
    from app.openai_compatible import HttpResponse, _TransportFailure
    from app.phase3 import ProviderPolicy

    def tool_response(name, arguments):
        return HttpResponse(200, json.dumps({'role': 'assistant', 'stop_reason': 'tool_use',
            'content': [{'type': 'tool_use', 'id': name, 'name': name, 'input': arguments}],
            'usage': {'input_tokens': 1, 'output_tokens': 1}}), {})

    bodies = []
    def transport(method, url, headers, body, timeout, *, diagnostics, **kwargs):
        bodies.append(body)
        if 1 < len(bodies) <= failures + 1:
            if failure_stage == 'request_write':
                diagnostics.submission('started_unknown')
            with diagnostics.stage(failure_stage):
                raise _TransportFailure()
        diagnostics.submission('response_received')
        if len(bodies) == 1:
            return tool_response('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'Read disclosure.'})
        return tool_response('submit_research', {'report': 'Revenue was 100 USD.', 'claims': [
            {'text': 'Revenue was 100 USD.', 'source_id': 's1', 'quotation': 'Revenue was 100 USD.', 'role': 'support'}],
            'limitations': [], 'stopping_reason': 'answered'})

    monkeypatch.setattr('app.anthropic_messages._default_transport', transport)
    settings = SimpleNamespace(base_url='https://example.com', model='model', api_key='fixture', allow_private_network=False)
    provider = AnthropicMessagesProvider(settings, ProviderPolicy(retry_base_seconds=0),
        anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=100)
    c = context()
    c.budget = ResearchExecutionBudget(2, None, None, float('inf'))
    snapshots = []
    result = run_research_agent(question='Revenue?', sources=[source()], model=provider, context=c,
        checkpoint=lambda state: snapshots.append(json.loads(json.dumps(state))))

    assert result.status == expected_status
    assert result.source_ids == ['s1']
    assert len(bodies) == expected_attempts
    assert bodies[0] != bodies[1]
    assert all(body == bodies[1] for body in bodies[1:])
    if failure_stage in ('dns', 'tls') and failures <= 2:
        # The two-call cap allows the writer calls, but not independent review.
        assert result.report == ''
        assert c.calls == 2
        assert c.usage == 4
    elif failure_stage in ('dns', 'tls'):
        assert snapshots[-1]['pending'] is None
        assert c.calls == 1
    else:
        assert snapshots[-1]['pending']['kind'] == 'model'
        assert c.calls == 2


def test_messages_preflight_and_injected_transport_diagnostics():
    from app.anthropic_messages import AnthropicMessagesProvider
    from app.model_protocol import ModelTurnInput
    from app.phase3 import ProviderFailure
    settings = SimpleNamespace(base_url='https://example.com', model='model', api_key='', allow_private_network=False)
    policy = SimpleNamespace(timeout_seconds=10, max_input_tokens=None)
    def fail(*args):
        raise TimeoutError()
    provider = AnthropicMessagesProvider(settings, policy, anthropic_version='2023-06-01',
        auth_scheme='x-api-key', max_tokens=100, transport=fail)
    observations = []
    turn = ModelTurnInput('system', 'user', (), ())
    try:
        provider.complete_turn(turn, diagnostic_callback=observations.append)
    except ProviderFailure as error:
        assert error.code == 'provider_secret_missing'
    else:
        assert False
    assert observations[-1]['submission_state'] == 'not_started'
    settings.api_key = 'fixture'
    try:
        provider.complete_turn(turn, diagnostic_callback=observations.append)
    except ProviderFailure as error:
        assert error.code == 'provider_timeout'
    else:
        assert False
    assert observations[-1]['submission_state'] == 'started_unknown'
    usage = provider._usage(json.dumps({'usage': {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}}))
    assert usage.total_tokens == 6
    assert usage.cache_read_input_tokens is None
    ordinary = provider._usage(json.dumps({'usage': {'input_tokens': 4, 'output_tokens': 2}}))
    assert ordinary.total_tokens == 6 and ordinary.cache_read_input_tokens is None
    cached = provider._usage(json.dumps({'usage': {'input_tokens': 4, 'output_tokens': 2,
        'cache_creation_input_tokens': 3, 'cache_read_input_tokens': 5}}))
    assert cached.total_tokens == 14
    malformed = provider._usage(json.dumps({'usage': {'input_tokens': 4, 'output_tokens': 2,
        'cache_read_input_tokens': 'invalid'}}))
    assert malformed.total_tokens is None


def test_unauthorized_source_inventory_excludes_private_metadata():
    class Capture:
        def __init__(self):
            self.turn = None
        def estimate_turn_input(self, turn):
            return 10
        def complete_turn(self, turn, **kwargs):
            self.turn = turn
            return ModelResponse({'opaque': []}, (), None, 'end', 'end')
    private = source(allowed=False)
    private.title = 'secret title'
    private.text = 'secret document text'
    private.content_sha256 = 'secret digest'
    model = Capture()
    run_research_agent(question='Q', sources=[private], model=model, context=context())
    payload = json.loads(model.turn.user)
    assert payload['sources'][0].keys() == {'source_id', 'eligibility'}
    assert 'secret' not in model.turn.user


def test_messages_request_diagnostic_records_actual_output_cap():
    from types import SimpleNamespace
    from app.anthropic_messages import AnthropicMessagesProvider
    from app.model_protocol import ModelTurnInput
    from app.openai_compatible import HttpResponse
    from app.research_diagnostics import safe_diagnostic_context
    settings = SimpleNamespace(base_url='https://example.com', model='model', api_key='fixture', allow_private_network=False)
    policy = SimpleNamespace(timeout_seconds=10, max_input_tokens=None)
    def transport(*args):
        return HttpResponse(200, json.dumps({'role': 'assistant', 'content': [{'type': 'text', 'text': 'ok'}],
            'stop_reason': 'end_turn'}))
    provider = AnthropicMessagesProvider(settings, policy, anthropic_version='2023-06-01',
        auth_scheme='x-api-key', max_tokens=100, transport=transport)
    observations = []
    provider.complete_turn(ModelTurnInput('private system', 'private question', (), ()),
                           max_output_tokens=25, diagnostic_callback=observations.append)
    metadata = safe_diagnostic_context(**observations[-1]['request_metadata'])
    assert metadata['provider'] == 'anthropic_messages'
    assert metadata['parameters']['max_tokens'] == 25
    assert 'private' not in json.dumps(metadata)


@pytest.mark.parametrize('claim_role', [{}, {'role': 'interpretation'}, {'kind': 'interpretation'}])
def test_submission_claim_requires_support_or_counterargument_role(claim_role):
    claim = {'text': 'Revenue was 100 USD.', 'source_id': 's1', 'quotation': 'Revenue was 100 USD.', **claim_role}
    m = Model([turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'a'),
               turn('submit_research', {'report': 'Revenue.', 'claims': [claim], 'limitations': [], 'stopping_reason': 'answered'}, 'b'),
               ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
    result = run_research_agent(question='Q', sources=[source()], model=m, context=context())
    assert result.status == 'partial' and result.claims == []
    feedback = json.loads(_last_tool(m)['content'])
    assert feedback['status'] == 'invalid'


def test_submission_keeps_support_and_counterargument_roles_without_kind():
    text = source()
    text.text = 'Revenue was 100 USD. Costs rose sharply.'
    text.content_sha256 = sha256(text.text.encode()).hexdigest()
    claims = [{'text': 'Revenue was 100 USD.', 'source_id': 's1', 'quotation': 'Revenue was 100 USD.', 'role': 'support'},
              {'text': 'Costs rose sharply.', 'source_id': 's1', 'quotation': 'Costs rose sharply.', 'role': 'counterargument'}]
    m = Model([turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'a'),
               turn('submit_research', {'report': 'Mixed.', 'claims': claims, 'limitations': [], 'stopping_reason': 'answered'}, 'b')])
    m.review_verdict = 'supported'
    result = run_research_agent(question='What was revenue?', sources=[text], model=m, context=context())
    assert result.status == 'completed'
    assert [c['role'] for c in result.claims] == ['support', 'counterargument']
    assert all('kind' not in c for c in result.claims)


def test_presend_retry_settings_default_and_reject_invalid_values(monkeypatch):
    from app.research_runtime import presend_retry_settings
    monkeypatch.delenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRIES', raising=False)
    monkeypatch.delenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRY_BASE_SECONDS', raising=False)
    assert presend_retry_settings() == (4, 1.0)
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRIES', '6')
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRY_BASE_SECONDS', '0.5')
    assert presend_retry_settings() == (6, 0.5)
    for retries, base in (('-1', '1'), ('9', '1'), ('x', '1'), ('2', 'nan'), ('2', '11')):
        monkeypatch.setenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRIES', retries)
        monkeypatch.setenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRY_BASE_SECONDS', base)
        assert presend_retry_settings() == (4, 1.0)


def test_invalid_tool_arguments_are_reported_per_field_and_offsets_get_a_location_hint():
    from pydantic import ValidationError
    from app.research_tools_registry import FactsRequest, ProposedFact
    req = FactsRequest.model_validate({'rationale': 'r', 'facts': [{'label': 'x', 'unit': 'millions', 'period': 'FY',
                                                              'source_id': 's', 'quotation': 'q'}]})
    with pytest.raises(ValidationError) as error:
        ProposedFact.model_validate(req.facts[0])
    assert error.value.errors()[0]['loc'] == ('value',)
    schema = FactsRequest.model_json_schema()['properties']['facts']['items']['properties']
    assert 'Omit both offsets' in schema['character_start']['description']
    assert 'Required' in schema['value']['description']


@pytest.mark.parametrize(('url', 'expected'), [
    ('https://www.google.com/url?q=https://www.sec.gov/Archives/edgar/data/1/000000000000000001/a.htm&sa=U',
     'https://www.sec.gov/Archives/edgar/data/1/000000000000000001/a.htm'),
    ('https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.bea.gov%2Fnews&rut=1', 'https://www.bea.gov/news'),
    ('https://www.microsoft.com/en-us/investor', 'https://www.microsoft.com/en-us/investor'),
    ('https://www.google.com/search?q=revenue', 'https://www.google.com/search?q=revenue'),
    ('https://www.google.com/goto?url=CAESdwHEIkXX&gsessionid=x', None),
    ('https://www.google.com/url?q=https://www.google.com/other', None),
])
def test_search_redirect_links_are_unwrapped_or_dropped(url, expected):
    from app.research_discovery import RedirectLead, normalize_search_lead
    if expected is None:
        with pytest.raises(RedirectLead):
            normalize_search_lead({'title': 'Lead', 'url': url})
    else:
        assert normalize_search_lead({'title': 'Lead', 'url': url}).url == expected


def test_messages_transport_uses_the_configured_connect_timeout(monkeypatch):
    from app.anthropic_messages import AnthropicMessagesProvider
    from app.model_protocol import ModelTurnInput
    from app.openai_compatible import HttpResponse
    from app.phase3 import ProviderPolicy
    seen = []

    def transport(method, url, headers, body, timeout, *, diagnostics, **kwargs):
        seen.append((kwargs.get('connect_timeout_seconds'), kwargs.get('response_timeout_seconds')))
        diagnostics.submission('response_received')
        return HttpResponse(200, json.dumps({'role': 'assistant', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'ok'}],
                                             'usage': {'input_tokens': 1, 'output_tokens': 1}}), {})

    monkeypatch.setattr('app.anthropic_messages._default_transport', transport)
    settings = SimpleNamespace(base_url='https://example.com', model='model', api_key='fixture', allow_private_network=False)
    provider = AnthropicMessagesProvider(settings, ProviderPolicy(), anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=100)
    turn = ModelTurnInput('system', 'user', (), ())
    monkeypatch.delenv('FORESIGHTNINE_LLM_CONNECT_TIMEOUT_SECONDS', raising=False)
    provider.complete_turn(turn)
    monkeypatch.setenv('FORESIGHTNINE_LLM_CONNECT_TIMEOUT_SECONDS', '9')
    monkeypatch.setenv('FORESIGHTNINE_LLM_RESPONSE_TIMEOUT_SECONDS', '60')
    provider.complete_turn(turn)
    assert seen == [(15.0, None), (9.0, 60.0)]


def test_available_capabilities_derived_and_overridden_in_initial_prompt():
    from app.research_runtime import SYSTEM
    from tests.fin_runtime_helpers import make_toolbox
    stop = ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')
    off = {'market_data_available': False, 'market_data_key_required': False,
           'sec_financials_available': False, 'filings_available': False, 'issuer_documents_available': False,
           'cn_market_data_available': False, 'cn_financials_available': False,
           'cn_financials_limited': False, 'cn_data_provider': None,
           'cn_market_data_provider': None, 'cn_providers_unreachable': [], 'hk_market_data_available': False}

    m_default = Model([stop])
    run_research_agent(question='Q', sources=[], model=m_default, context=context())
    payload_default = json.loads(m_default.inputs[0][1]['content'])
    assert 'available_historical_price_instruments' not in payload_default
    default_caps = dict(payload_default['available_capabilities'])
    assert default_caps.pop('evidence_routes')
    assert default_caps == {
        'search_available': False, 'acquire_available': False, 'sec_available': False,
        'search_library_available': True, 'news_search_available': False, **off}

    m_derived = Model([stop])
    run_research_agent(question='Q', sources=[], model=m_derived, context=context(),
                       discover=lambda _: [], acquire=lambda *_: (None, {}), fin_data=make_toolbox())
    derived_caps = dict(json.loads(m_derived.inputs[0][1]['content'])['available_capabilities'])
    assert derived_caps.pop('evidence_routes')
    assert derived_caps == {
        'search_available': True, 'acquire_available': True, 'sec_available': True,
        'market_data_available': True, 'market_data_key_required': False,
        'sec_financials_available': True, 'filings_available': True,
        'search_library_available': True, 'news_search_available': True, 'news_search_providers': ['glm-coding-mcp-search'],
        'issuer_documents_available': False,
        'cn_market_data_available': False, 'cn_financials_available': False,
        'cn_financials_limited': False, 'cn_data_provider': None,
        'cn_market_data_provider': None, 'cn_providers_unreachable': [], 'hk_market_data_available': True}

    m_nokey = Model([stop])
    run_research_agent(question='Q', sources=[], model=m_nokey, context=context(), fin_data=make_toolbox(market_key=None))
    caps = json.loads(m_nokey.inputs[0][1]['content'])['available_capabilities']
    assert caps['market_data_available'] is False and caps['market_data_key_required'] is True

    m_custom = Model([stop])
    custom_caps = {'search_available': True, 'web_fetch_enabled': True, 'sec_available': False}
    run_research_agent(question='Q', sources=[], model=m_custom, context=context(), capabilities=custom_caps)
    payload_custom = json.loads(m_custom.inputs[0][1]['content'])
    custom_result = dict(payload_custom['available_capabilities'])
    assert custom_result.pop('evidence_routes')
    assert custom_result == {**custom_caps, **off, 'search_library_available': True, 'news_search_available': False}

    for token in ('available_capabilities', 'news_search', 'acquire_sources', 'resolve_entity', 'market_data',
                  'company_financials', 'filings', 'market_data_key_required', 'sec_financials_available', 'partial'):
        assert token in SYSTEM


def test_company_financials_success_unconfigured_restricted_and_unknown_recovery():
    from tests.fin_data.helpers import ConceptTransport
    from tests.fin_runtime_helpers import FakeTransport, Reply, json_reply, make_toolbox
    fin_args = {'cik': '320193', 'lines': ['revenue'], 'periods': 'last_4_quarters', 'rationale': 'Quarterly revenue.'}
    stop = ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')

    # 1. Success registers the source for read_sources and record_facts.
    t = ConceptTransport()
    m_ok = Model([turn('company_financials', fin_args, 'f1'), stop])
    sources_list = []
    res_ok = run_research_agent(question='Q', sources=sources_list, model=m_ok, context=context(), fin_data=make_toolbox(sec=t))
    fb = json.loads(_last_tool(m_ok, 1)['content'])
    assert fb['status'] in ('ok', 'partial') and fb['source_id'] == 'src_1' and res_ok.source_ids == ['src_1']
    assert len(sources_list) == 1 and sources_list[0].retrieval_method == 'sec_companyfacts' and sources_list[0].kind == 'official'

    # 2. Unconfigured SEC contact, and research cutoff / cost limit restrictions make no request.
    t2 = FakeTransport(Reply())
    m_unconf = Model([turn('company_financials', fin_args, 'f1'), stop])
    run_research_agent(question='Q', sources=[], model=m_unconf, context=context(), fin_data=make_toolbox(sec=t2, user_agent=None))
    assert json.loads(_last_tool(m_unconf, 1)['content']) == {
        'status': 'unavailable', 'reason': 'sec_contact_unconfigured_or_unavailable'}
    m_cutoff = Model([turn('company_financials', fin_args, 'f1'), stop])
    run_research_agent(question='Q', sources=[], model=m_cutoff, context=context(),
                       as_of=datetime(2025, 2, 1, tzinfo=timezone.utc),
                       fin_data=make_toolbox(sec=t2, disabled_reason='historical_cutoff'))
    assert json.loads(_last_tool(m_cutoff, 1)['content']) == {'status': 'restricted', 'reason': 'historical_cutoff'}
    assert not t2.calls

    # 3. An unknown SEC outcome is recorded as a failure, not retried, and the run continues.
    t3 = FakeTransport(OSError('boom'))
    m_unknown = Model([turn('company_financials', fin_args, 'f_u'), stop])
    res_unknown = run_research_agent(question='Q', sources=[], model=m_unknown, context=context(), fin_data=make_toolbox(sec=t3))
    assert len(m_unknown.inputs) == 2
    fed_back = json.loads(_last_tool(m_unknown, 1)['content'])
    assert fed_back['status'] == 'failed' and fed_back['reason'] == 'source_unavailable'
    assert any(e.get('name') == 'company_financials' and e.get('status') == 'failed' for e in res_unknown.events)
    assert len(t3.calls) == 1


def test_acquire_sources_distinguishes_disabled_undiscovered_and_granular_failures():
    # 1. acquire=None -> acquisition_not_authorized_or_disabled
    m_disabled = Model([
        turn('acquire_sources', {'urls': ['https://example.com/guessed'], 'rationale': 'fetch'}, 'a1'),
        ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop'),
    ])
    run_research_agent(question='Q', sources=[], model=m_disabled, context=context(), acquire=None)
    fb_disabled = json.loads(_last_tool(m_disabled, 1)['content'])
    assert fb_disabled['status'] == 'unavailable'
    assert fb_disabled['reason'] == 'acquisition_not_authorized_or_disabled'
    assert fb_disabled['results'][0]['reason'] == 'acquisition_not_authorized_or_disabled'

    # 2. Unsearched URL -> lead_not_discovered with hint
    acquire_calls = []
    m_unsearched = Model([
        turn('acquire_sources', {'urls': ['https://example.com/guessed'], 'rationale': 'fetch'}, 'a1'),
        ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop'),
    ])
    res_unsearched = run_research_agent(
        question='Q',
        sources=[],
        model=m_unsearched,
        context=context(),
        acquire=lambda u, t: acquire_calls.append((u, t)) or (None, {}),
    )
    assert acquire_calls == []
    fb_unsearched = json.loads(_last_tool(m_unsearched, 1)['content'])
    assert fb_unsearched['status'] == 'unavailable'
    assert fb_unsearched['reason'] == 'lead_not_discovered'
    assert 'news_search' in fb_unsearched['hint']
    assert fb_unsearched['results'][0]['reason'] == 'lead_not_discovered'
    assert 'search_candidates_not_acquired' not in [g['code'] for g in res_unsearched.gaps]

    # 3. Discovered leads forwarding granular failure reasons (sec_contact_unconfigured, http_forbidden_or_challenge, source_too_large)
    leads = [
        {'url': 'https://www.sec.gov/Archives/edgar/data/1/a.htm', 'title': 'SEC'},
        {'url': 'https://investor.example.com/q4', 'title': 'IR'},
        {'url': 'https://example.com/huge.pdf', 'title': 'PDF'},
    ]
    def acquire_by_url(url, title):
        if 'sec.gov' in url:
            return None, {
                'status': 'sec_contact_unconfigured',
                'notes': ['SEC access is not configured.'],
                'outcomes': [{'status': 'unconfigured', 'reason': 'sec_contact_unconfigured', 'external_state': 'not_started'}],
            }
        if 'investor.example.com' in url:
            return None, {
                'status': 'failed',
                'notes': ['Blocked by upstream WAF.'],
                'outcomes': [{'status': 'failed', 'reason': 'http_forbidden_or_challenge', 'external_state': 'returned'}],
            }
        return None, {
            'status': 'failed',
            'reason': 'source_too_large',
            'notes': ['Response exceeded 5 MB limit.'],
            'outcomes': [{'status': 'failed', 'reason': 'source_too_large', 'external_state': 'returned'}],
        }

    m_multi = Model([
        turn('news_search', {'query': 'NVDA', 'rationale': 'search'}, 'd1'),
        turn('acquire_sources', {'urls': [leads[0]['url'], leads[1]['url'], leads[2]['url']], 'rationale': 'fetch'}, 'a1'),
        ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop'),
    ])
    res_multi = run_research_agent(
        question='Q',
        sources=[],
        model=m_multi,
        context=context(),
        discover=lambda _: leads,
        acquire=acquire_by_url,
    )
    fb_multi = json.loads(_last_tool(m_multi, 2)['content'])
    assert fb_multi['status'] == 'unavailable'
    assert fb_multi['reason'] == 'source_cannot_be_acquired'
    assert fb_multi['failure_reasons'] == ['sec_contact_unconfigured', 'http_forbidden_or_challenge', 'source_too_large']
    assert [row['reason'] for row in fb_multi['results']] == [
        'sec_contact_unconfigured',
        'http_forbidden_or_challenge',
        'source_too_large',
    ]
    assert 'SEC access is not configured.' in fb_multi['notes']
    assert 'Blocked by upstream WAF.' in fb_multi['notes']
    assert len(fb_multi['outcomes']) == 3
    assert 'search_candidates_not_acquired' in [g['code'] for g in res_multi.gaps]

    # Single-URL granular failure surfaces that exact reason at top level too.
    m_single = Model([
        turn('news_search', {'query': 'NVDA', 'rationale': 'search'}, 'd1'),
        turn('acquire_sources', {'urls': [leads[1]['url']], 'rationale': 'fetch'}, 'a1'),
        ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop'),
    ])
    run_research_agent(
        question='Q',
        sources=[],
        model=m_single,
        context=context(),
        discover=lambda _: [leads[1]],
        acquire=acquire_by_url,
    )
    fb_single = json.loads(_last_tool(m_single, 2)['content'])
    assert fb_single['reason'] == 'http_forbidden_or_challenge'
    assert fb_single['failure_reasons'] == ['http_forbidden_or_challenge']


def test_partial_submission_does_not_publish_unverified_notes_across_recovery():
    notes = 'Checked SEC and web search; SEC was unconfigured and IR returned 403.'
    m = Model([
        turn('submit_research', {
            'report': f'  {notes}  ',
            'claims': [],
            'limitations': ['No primary filing could be acquired.'],
            'stopping_reason': 'all retrieval paths blocked',
            'status': 'partial',
        }, 's1'),
    ])
    saved = []
    outcome = run_research_agent(
        question='Compare revenue?',
        sources=[],
        model=m,
        context=context(),
        checkpoint=lambda s: saved.append(json.loads(json.dumps(s))),
    )
    assert outcome.status == 'partial'
    assert outcome.reason == 'submitted'
    assert outcome.report == ''
    assert notes not in outcome.partial_notes
    assert outcome.stopping_note == 'all retrieval paths blocked'
    assert saved[-1]['submission']['report'] == ''
    assert notes not in (saved[-1]['submission'].get('partial_notes') or '')

    recovered = run_research_agent(
        question='Compare revenue?',
        sources=[],
        model=Model([]),
        context=context(),
        initial_state=saved[-1],
    )
    assert recovered.status == 'partial'
    assert recovered.reason == 'submitted'
    assert recovered.report == ''
    assert recovered.partial_notes == outcome.partial_notes
    assert recovered.stopping_note == 'all retrieval paths blocked'


def test_web_retrieval_methods_and_turn_partial_notes_schema():
    from app.research_topics import ResearchTurn, TurnUsage

    for method in ('web_html', 'web_pdf'):
        src = ResearchSource(
            source_id=f's_{method}',
            owner_id='owner',
            title='Web doc',
            text='Web body',
            kind='user_supplied',
            obtained_at=datetime.now(timezone.utc),
            content_sha256='h',
            retrieval_method=method,
            permissions=SourcePermissions(store=True, model_input=True, display=True, export=False),
        )
        assert src.retrieval_method == method

    now = datetime.now(timezone.utc)
    turn_default = ResearchTurn(
        turn_id='t1',
        index=1,
        question='Q?',
        question_origin='user',
        operation_ids=['op1'],
        parent_version_id='v1',
        started_at=now,
        finished_at=now,
        status='partial',
        reason='submitted',
        usage=TurnUsage(model_calls=1, search_calls=0, total_tokens=10),
        search_status='not_requested',
        official_fetch_status='not_requested',
    )
    assert turn_default.partial_notes is None
    turn_with_notes = turn_default.model_copy(update={'partial_notes': 'Unverified note'})
    assert turn_with_notes.partial_notes == 'Unverified note'


def test_company_financials_rate_limit_allows_one_retry_and_stops_on_second_repeat():
    from tests.fin_data.helpers import ConceptTransport
    from tests.fin_runtime_helpers import FakeTransport, Reply, json_reply, make_toolbox
    fin_args = {'cik': '320193', 'lines': ['revenue'], 'periods': 'last_4_quarters', 'rationale': 'Quarterly revenue.'}
    stop = ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')

    # 1. The first call is rate limited by the global SEC pacer; one retry is allowed and succeeds.
    pace = iter([False])  # first SEC request is paced out; later ones (one per concept file) succeed
    t = ConceptTransport()
    m_ok = Model([turn('company_financials', fin_args, 'f1'),
                  turn('company_financials', {**fin_args, 'rationale': 'Retry after 1s SEC rate limit.'}, 'f2'), stop])
    saved = []
    res_ok = run_research_agent(question='Q', sources=[], model=m_ok, context=context(),
                                fin_data=make_toolbox(sec=t, reserve_sec=lambda: next(pace, True)),
                                checkpoint=lambda s: saved.append(json.loads(json.dumps(s))))
    first = json.loads(_last_tool(m_ok, 1)['content'])
    assert first['status'] == 'rate_limited' and first['retryable'] is True
    assert json.loads(_last_tool(m_ok, 2)['content'])['status'] in ('ok', 'partial')
    assert len(t.concept_urls) == 4 and len(saved[-1]['rate_limit_retries']) == 1

    # 2. A second consecutive rate limit keeps the signature in seen, so repeating again stops with no_progress.
    t2 = FakeTransport(Reply())
    m_fail = Model([turn('company_financials', fin_args, 'f1'),
                    turn('company_financials', {**fin_args, 'rationale': 'Retry once.'}, 'f2'),
                    turn('company_financials', {**fin_args, 'rationale': 'Retry twice.'}, 'f3'),
                    turn('company_financials', {**fin_args, 'rationale': 'Retry thrice.'}, 'f4')])
    res_fail = run_research_agent(question='Q', sources=[], model=m_fail, context=context(),
                                  fin_data=make_toolbox(sec=t2, reserve_sec=lambda: False))
    assert res_fail.status == 'partial' and res_fail.reason == 'no_progress'
    assert [g['code'] for g in res_fail.gaps] == ['no_progress'] and not t2.calls


def test_acquire_sources_rate_limit_allows_one_retry_and_blocks_second_repeat():
    lead = {'url': 'https://www.sec.gov/Archives/edgar/data/1045810/000104581025000023/nvda-20250126.htm', 'title': 'NVDA 10-K'}
    acquired_src = ResearchSource(
        source_id='source_acq_nvda',
        owner_id='owner',
        title='NVDA 10-K',
        text='Total revenue was $130,497 million.',
        url=lead['url'],
        kind='official',
        obtained_at=datetime.now(timezone.utc),
        content_sha256='acq-hash',
        provenance={'symbol': 'NVDA', 'cik': '1045810'},
        retrieval_method='sec_filing_html',
        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True),
    )

    attempts = []
    def acquire_flaky(url, title):
        attempts.append(url)
        if len(attempts) == 1:
            return None, {
                'status': 'failed',
                'outcomes': [{'status': 'failed', 'reason': 'source_rate_limited', 'external_state': 'returned'}],
            }
        return acquired_src, {'status': 'completed', 'outcomes': [{'status': 'retrieved', 'external_state': 'returned'}]}

    m_ok = Model([
        turn('news_search', {'query': 'NVDA 10-K', 'rationale': 'find filing'}, 'd1'),
        turn('acquire_sources', {'urls': [lead['url']], 'rationale': 'fetch'}, 'a1'),
        turn('acquire_sources', {'urls': [lead['url']], 'rationale': 'retry after rate limit'}, 'a2'),
        turn('read_sources', {'reads': [{'source_id': 'source_acq_nvda'}], 'rationale': 'read'}, 'r1'),
        turn('submit_research', {
            'report': 'Total revenue was $130,497 million.',
            'claims': [{
                'text': 'Total revenue was $130,497 million.',
                'source_id': 'source_acq_nvda',
                'quotation': 'Total revenue was $130,497 million.',
                'role': 'support',
            }],
            'limitations': [],
            'stopping_reason': 'answered',
        }, 's1'),
    ])
    res_ok = run_research_agent(
        question='NVDA revenue?',
        sources=[],
        model=m_ok,
        context=context(),
        discover=lambda _: [lead],
        acquire=acquire_flaky,
    )
    assert res_ok.status == 'partial'  # The synthetic reviewer rejects unsupported claims.
    assert len(attempts) == 2

    always_limited = []
    def acquire_limited(url, title):
        always_limited.append(url)
        return None, {
            'status': 'failed',
            'outcomes': [{'status': 'failed', 'reason': 'source_rate_limited', 'external_state': 'returned'}],
        }

    m_fail = Model([
        turn('news_search', {'query': 'NVDA 10-K', 'rationale': 'find filing'}, 'd1'),
        turn('acquire_sources', {'urls': [lead['url']], 'rationale': 'fetch'}, 'a1'),
        turn('acquire_sources', {'urls': [lead['url']], 'rationale': 'retry 1'}, 'a2'),
        turn('acquire_sources', {'urls': [lead['url']], 'rationale': 'retry 2'}, 'a3'),
        turn('acquire_sources', {'urls': [lead['url']], 'rationale': 'retry 3'}, 'a4'),
    ])
    res_fail = run_research_agent(
        question='NVDA revenue?',
        sources=[],
        model=m_fail,
        context=context(),
        discover=lambda _: [lead],
        acquire=acquire_limited,
    )
    assert res_fail.status == 'partial'
    assert res_fail.reason == 'no_progress'
    assert len(always_limited) == 2


def test_tools_and_system_document_outline_search_and_read_limits():
    from app.research_runtime import (
        MAX_READ_SOURCE_SPAN_CHARS,
        MAX_READ_SOURCES_BATCH_CHARS,
        SYSTEM,
        TOOLS,
    )

    assert MAX_READ_SOURCE_SPAN_CHARS == 25_000
    assert MAX_READ_SOURCES_BATCH_CHARS == 40_000
    by_name = {tool.name: tool for tool in TOOLS}
    assert {'outline_source', 'search_source', 'read_sources'} <= set(by_name)
    assert '25000' in by_name['read_sources'].description
    assert '40000' in by_name['read_sources'].description
    assert 'outline_source' in by_name['read_sources'].description
    assert 'search_source' in by_name['read_sources'].description
    for token in ('outline_source', 'search_source', 'read_sources', '25000', '40000', '[start, end]'):
        assert token in SYSTEM


def test_outline_source_extracts_boundaries_with_exact_offsets_and_bounds():
    filler = 'Narrative risk discussion line without section headings.\n' * 1200
    doc_text = (
        '# Annual Overview\n'
        'Intro text.\n'
        'Setext Section\n'
        '==============\n'
        'Table of Contents\n'
        '| Item 7. Management Discussion | 35 |\n'
        'PART I\n'
        'ITEM 1. BUSINESS\n'
        'Company overview.\n'
        'ITEM 1A. RISK FACTORS\n'
        'RISKS RELATED TO OPERATIONS\n'
        + filler
        + 'PART II\n'
        'ITEM 7. MANAGEMENT\'S DISCUSSION AND ANALYSIS\n'
        'Summary of operations.\n'
        'ITEM 8. FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA\n'
        '| [colspan=3] Consolidated Statements of Income |\n'
        '| Revenue | $ 130,497 | $ 60,922 |\n'
        'Consolidated Balance Sheets\n'
        'Note 1 - Organization and Summary of Significant Accounting Policies\n'
        '合并利润表\n'
    )
    big_src = source()
    big_src.source_id = 's_10k'
    big_src.title = 'NVDA 10-K'
    big_src.text = doc_text
    big_src.content_sha256 = 'hash-10k'

    plain_src = source()
    plain_src.source_id = 's_plain'
    plain_src.title = 'Plain'
    plain_src.text = 'This plain paragraph has no headings at all. Revenue was 100 USD.'
    plain_src.content_sha256 = 'hash-plain'

    m = Model([
        turn('outline_source', {'source_id': 's_10k', 'max_entries': 10, 'rationale': 'inspect 10-K outline'}, 'o1'),
        turn('outline_source', {'source_id': 's_plain'}, 'o2'),
        ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop'),
    ])
    res = run_research_agent(question='Q', sources=[big_src, plain_src], model=m, context=context())
    assert res.reason == 'no_submission'
    assert res.source_ids == []  # Navigation never marks ranges as read.

    outline_10k = json.loads(m.inputs[1][-1]['content'])
    assert outline_10k['status'] == 'completed'
    assert outline_10k['total_characters'] == len(doc_text)
    assert len(outline_10k['entries']) <= 10
    assert outline_10k['entries'] == sorted(outline_10k['entries'], key=lambda item: item['start'])
    titles = [entry['title'] for entry in outline_10k['entries']]
    assert 'Consolidated Statements of Income' in titles
    assert any(t.startswith('ITEM 8.') for t in titles)
    for entry in outline_10k['entries']:
        assert entry['line'] >= 1
        assert doc_text[entry['start']:].startswith(entry['title'])
        assert '| Revenue |' not in entry['title']

    outline_plain = json.loads(m.inputs[2][-1]['content'])
    assert outline_plain['status'] == 'completed'
    assert outline_plain['entries'] == []
    assert outline_plain['total_characters'] == len(plain_src.text)
    assert 'search_source' in outline_plain['note']


def test_search_source_supports_exact_fallbacks_bounds_and_requires_read_before_cite():
    filler = 'General disclosure paragraph about products and markets. ' * 20
    text = (
        filler
        + '\nConsolidated\n  Statements\tof Income\n'
        + '| Total revenue | $ | 130,497 |\n'
        + '| Operating income | $ | 81,453 |\n'
        + '| operating income margin | 62.4% |\n'
        + ''.join(f'repeat marker #{i} in appendix. ' for i in range(15))
    )
    src = source()
    src.source_id = 's_search'
    src.title = 'NVDA 10-K'
    src.text = text
    src.content_sha256 = 'hash-search'

    m = Model([
        turn('search_source', {'source_id': 's_search', 'query': 'Consolidated Statements of Income'}, 'q_ws'),
        turn('search_source', {'source_id': 's_search', 'query': 'Total revenue 130,497'}, 'q_tok'),
        turn('search_source', {'source_id': 's_search', 'query': 'Operating income', 'case_sensitive': True}, 'q_cs'),
        turn('search_source', {'source_id': 's_search', 'query': 'Operating income', 'case_sensitive': False}, 'q_ci'),
        turn('search_source', {'source_id': 's_search', 'query': 'repeat marker', 'max_hits': 3}, 'q_cap'),
        turn('search_source', {'source_id': 's_search', 'query': 'missing phrase xyz'}, 'q_none'),
        turn('search_source', {'source_id': 's_search', 'query': '   '}, 'q_blank'),
        turn('search_source', {'source_id': 's_search', 'query': 'Operating', 'max_hits': 25}, 'q_max'),
        turn('submit_research', {
            'report': 'Operating income was 81,453.',
            'claims': [{'text': 'Operating income', 'source_id': 's_search', 'quotation': '| Operating income | $ | 81,453 |', 'role': 'support'}],
            'limitations': [],
            'stopping_reason': 'test unread cite rejection',
        }, 'sub_unread'),
        ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop'),
    ])
    res = run_research_agent(question='Q', sources=[src], model=m, context=context())
    assert res.reason == 'no_submission'
    assert res.source_ids == []

    tool_msgs = [json.loads(_last_tool_msg(inp)['content']) for inp in m.inputs[1:]]
    ws_fb, tok_fb, cs_fb, ci_fb, cap_fb, none_fb, blank_fb, max_fb, unread_fb = tool_msgs

    assert ws_fb['match_mode'] == 'whitespace_normalized'
    assert ws_fb['total_matches'] == 1
    assert ws_fb['total_characters'] == len(text)
    hit_ws = ws_fb['hits'][0]
    assert text[hit_ws['start']:hit_ws['end']] in hit_ws['snippet']
    assert len(hit_ws['snippet']) <= 360

    assert tok_fb['match_mode'] == 'token_sequence'
    assert tok_fb['total_matches'] == 1
    hit_tok = tok_fb['hits'][0]
    assert text[hit_tok['start']:hit_tok['end']] == 'Total revenue | $ | 130,497'
    assert len(hit_tok['snippet']) <= 360

    assert cs_fb['match_mode'] == 'exact' and cs_fb['total_matches'] == 1
    assert ci_fb['match_mode'] == 'exact' and ci_fb['total_matches'] == 2

    assert cap_fb['total_matches'] == 15
    assert cap_fb['returned_hits'] == 3
    assert len(cap_fb['hits']) == 3
    assert cap_fb['truncated'] is True

    assert none_fb['match_mode'] == 'none'
    assert none_fb['total_matches'] == 0
    assert none_fb['hits'] == []
    assert none_fb['total_characters'] == len(text)

    assert blank_fb['status'] == 'invalid'
    assert max_fb['status'] == 'invalid'
    assert unread_fb['status'] == 'invalid'
    assert 'within read ranges' in unread_fb['reason']


def test_search_source_boundary_snippets_and_read_sources_ineligible_non_leakage():
    edge_text = 'FIRST_KEY ' + ('x' * 600) + ' LAST_KEY'
    src = source()
    src.source_id = 's_edge'
    src.text = edge_text

    hidden = source(allowed=False)
    hidden.source_id = 's_hidden'
    hidden.title = 'SECRET'
    hidden.text = 'SECRET ' * 5000

    m = Model([
        turn('search_source', {'source_id': 's_edge', 'query': 'FIRST_KEY'}, 'f1'),
        turn('search_source', {'source_id': 's_edge', 'query': 'LAST_KEY'}, 'f2'),
        turn('read_sources', {'reads': [{'source_id': 's_hidden', 'start': 0, 'end': 30_000}], 'rationale': 'probe'}, 'r_hidden'),
        ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop'),
    ])
    run_research_agent(question='Q', sources=[src, hidden], model=m, context=context())
    first_fb, last_fb, hidden_fb = (json.loads(_last_tool_msg(inp)['content']) for inp in m.inputs[1:])
    assert len(first_fb['hits'][0]['snippet']) == 300
    assert first_fb['hits'][0]['snippet'].startswith('FIRST_KEY')
    assert len(last_fb['hits'][0]['snippet']) == 300
    assert last_fb['hits'][0]['snippet'].endswith('LAST_KEY')
    assert hidden_fb['results'] == [{'source_id': 's_hidden', 'status': 'denied_or_unavailable'}]
    assert 'total_characters' not in json.dumps(hidden_fb)
    assert 'SECRET' not in json.dumps(hidden_fb)


def test_outline_and_search_enforce_eligibility_and_as_of_cutoff():
    cutoff = datetime(2025, 1, 15, tzinfo=timezone.utc)
    future_src = source()
    future_src.source_id = 's_future'
    future_src.title = 'SECRET FUTURE TITLE'
    future_src.text = '# SECRET HEADING\nSECRET_TOKEN revenue was 999 USD.'
    future_src.published_at = datetime(2025, 2, 1, tzinfo=timezone.utc)

    fred_src = source('https://fred.stlouisfed.org/series/GDP')
    fred_src.source_id = 's_fred'
    fred_src.title = 'SECRET FRED TITLE'
    fred_src.text = '# SECRET FRED\nSECRET_TOKEN GDP 100'
    fred_src.published_at = datetime(2024, 1, 1, tzinfo=timezone.utc)

    hidden_src = source(allowed=False)
    hidden_src.source_id = 's_hidden'
    hidden_src.title = 'SECRET HIDDEN TITLE'
    hidden_src.text = '# SECRET HIDDEN\nSECRET_TOKEN hidden 100'
    hidden_src.published_at = datetime(2024, 1, 1, tzinfo=timezone.utc)

    m = Model([
        turn('outline_source', {'source_id': 's_future'}, 'o1'),
        turn('search_source', {'source_id': 's_future', 'query': 'SECRET_TOKEN'}, 's1'),
        turn('outline_source', {'source_id': 's_fred'}, 'o2'),
        turn('search_source', {'source_id': 's_fred', 'query': 'SECRET_TOKEN'}, 's2'),
        turn('outline_source', {'source_id': 's_hidden'}, 'o3'),
        turn('search_source', {'source_id': 's_missing', 'query': 'anything'}, 's3'),
        ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop'),
    ])
    run_research_agent(
        question='Q',
        sources=[future_src, fred_src, hidden_src],
        model=m,
        context=context(),
        as_of=cutoff,
    )
    for inp in m.inputs[1:]:
        raw = _last_tool_msg(inp)['content']
        assert json.loads(raw)['status'] == 'denied_or_unavailable'
        assert 'SECRET' not in raw
        assert 'total_characters' not in raw


def test_read_sources_enforces_span_and_batch_caps_while_preserving_small_sources():
    from app.research_runtime import MAX_READ_SOURCE_SPAN_CHARS, MAX_READ_SOURCES_BATCH_CHARS

    # 1. Exact boundary: 25,000-char source succeeds with start=0, end=None.
    exact_src = source()
    exact_src.source_id = 's_exact'
    exact_src.text = 'A' * MAX_READ_SOURCE_SPAN_CHARS
    exact_src.content_sha256 = 'hash-exact'
    m_exact = Model([
        turn('read_sources', {'reads': [{'source_id': 's_exact'}], 'rationale': 'read exact limit'}, 'r_exact'),
        ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop'),
    ])
    res_exact = run_research_agent(question='Q', sources=[exact_src], model=m_exact, context=context())
    assert res_exact.source_ids == ['s_exact']
    fb_exact = json.loads(_last_tool(m_exact, 1)['content'])
    assert fb_exact['results'][0]['status'] == 'read'
    assert len(fb_exact['results'][0]['text']) == MAX_READ_SOURCE_SPAN_CHARS

    # 2. Long 60,000-char filing: oversized end=None, oversized slice, oversized batch (>40,000) are rejected
    #    with explicit status='invalid', total_characters, limits, and search/outline guidance;
    #    subsequent search_source -> targeted read_sources -> submit_research completes cleanly.
    prefix = 'ITEM 1A. RISK FACTORS\n' + ('Risk text line. ' * 2000)
    target_block = (
        '\nITEM 8. FINANCIAL STATEMENTS\n'
        'Consolidated Statements of Income\n'
        'Total revenue was $130,497 million in FY2025.\n'
    )
    suffix = 'Additional notes. ' * 1600
    long_text = prefix + target_block + suffix
    assert len(long_text) > MAX_READ_SOURCES_BATCH_CHARS
    long_src = source()
    long_src.source_id = 's_long'
    long_src.title = 'NVDA FY2025 10-K'
    long_src.text = long_text
    long_src.content_sha256 = 'hash-long'

    def scripted_steps(m_obj):
        # Yield oversized calls, then inspect via search_source, read targeted window, and submit.
        yield turn('read_sources', {'reads': [{'source_id': 's_long'}], 'rationale': 'blind full read'}, 'r_none')
        yield turn('read_sources', {'reads': [{'source_id': 's_long', 'start': 0, 'end': 30_000}], 'rationale': 'blind 30k'}, 'r_30k')
        yield turn('read_sources', {
            'reads': [
                {'source_id': 's_long', 'start': 0, 'end': 22_000},
                {'source_id': 's_long', 'start': 22_000, 'end': 44_000},
            ],
            'rationale': 'blind 44k batch',
        }, 'r_batch')
        yield turn('search_source', {'source_id': 's_long', 'query': 'Consolidated Statements of Income'}, 's_income')

    saved = []
    class GuidedModel:
        def __init__(self):
            self.inputs = []
            self.step = 0
            self._gen = scripted_steps(self)
        def complete(self, messages, tools, **kwargs):
            self.inputs.append(json.loads(json.dumps(messages)))
            if self.step < 4:
                self.step += 1
                return next(self._gen)
            last = json.loads(_last_tool_msg(messages)['content'])
            if self.step == 4:
                self.step += 1
                hit = last['hits'][0]
                win_start = max(0, hit['start'] - 100)
                win_end = min(last['total_characters'], hit['end'] + 400)
                return turn('read_sources', {'reads': [{'source_id': 's_long', 'start': win_start, 'end': win_end}], 'rationale': 'targeted read'}, 'r_target')
            return turn('submit_research', {
                'report': 'Total revenue was $130,497 million in FY2025.',
                'claims': [{
                    'text': 'FY2025 total revenue was $130,497 million.',
                    'source_id': 's_long',
                    'quotation': 'Total revenue was $130,497 million in FY2025.',
                    'role': 'support',
                }],
                'limitations': [],
                'stopping_reason': 'answered',
            }, 'sub_ok')

    gm = GuidedModel()
    res_long = run_research_agent(
        question='What was FY2025 revenue?',
        sources=[long_src],
        model=gm,
        context=context(),
        checkpoint=lambda s: saved.append(json.loads(json.dumps(s))),
    )
    assert res_long.status == 'partial'  # Retrieval checks do not establish semantic support.
    assert res_long.source_ids == ['s_long']

    for idx, expected_err in ((1, 'read_span_too_large'), (2, 'read_span_too_large'), (3, 'read_batch_too_large')):
        msg = _last_tool(gm, idx)
        fb = json.loads(msg['content'])
        assert fb['status'] == 'invalid'
        assert fb['error'] == expected_err
        assert fb['total_characters'] == len(long_text)
        assert fb['max_span_characters'] == MAX_READ_SOURCE_SPAN_CHARS
        assert fb['max_batch_characters'] == MAX_READ_SOURCES_BATCH_CHARS
        assert 'outline_source' in fb['reason'] and 'search_source' in fb['reason']
        assert '[start, end]' in fb['instruction']

    # Verify rejected oversized reads never polluted read_ranges before targeted read.
    read_events = [ev for ev in res_long.events if ev.get('kind') == 'tool' and ev.get('name') == 'read_sources']
    assert [ev['status'] for ev in read_events] == ['invalid', 'invalid', 'invalid', 'completed']
    assert len(saved[-1]['read_ranges']) == 1
    assert saved[-1]['read_ranges'][0]['end'] - saved[-1]['read_ranges'][0]['start'] <= 600


def test_below_watermark_runtime_keeps_history_prefix_byte_identical_for_kv_cache():
    from app.research_runtime import compact_runtime_history

    src = source()
    src.text = 'Revenue was 100 USD in FY2025. Revenue was 80 USD in FY2024. ' + ('Detail text. ' * 200)
    src.content_sha256 = sha256(src.text.encode()).hexdigest()

    m = Model([
        turn('outline_source', {'source_id': 's1'}, 'c1'),
        turn('search_source', {'source_id': 's1', 'query': 'Revenue was 100'}, 'c2'),
        turn('read_sources', {'reads': [{'source_id': 's1', 'start': 0, 'end': 80}], 'rationale': 'read metrics'}, 'c3'),
        turn('record_facts', {'facts': [{
            'label': 'revenue', 'value': 100, 'unit': 'USD', 'currency': 'USD', 'period': 'FY2025',
            'source_id': 's1', 'quotation': 'Revenue was 100 USD in FY2025.',
        }], 'rationale': 'record'}, 'c4'),
        turn('submit_research', {
            'report': 'Revenue was 100 USD in FY2025.',
            'claims': [{'text': 'Revenue was 100 USD in FY2025.', 'source_id': 's1',
                        'quotation': 'Revenue was 100 USD in FY2025.', 'role': 'support'}],
            'limitations': [],
            'stopping_reason': 'answered',
        }, 'c5'),
    ])
    m.review_verdict = 'supported'
    saved = []
    res = run_research_agent(
        question='Revenue?',
        sources=[src],
        model=m,
        context=context(),
        checkpoint=lambda s: saved.append(json.loads(json.dumps(s))),
    )
    assert res.status == 'completed'
    assert res.question_parts[0]['status'] == 'answered'
    assert 'compaction_count' not in saved[-1]
    assert len(m.inputs) == 5

    # Volatile tail_note sits only in the trailing user message; everything prior stays byte-identical across turns.
    def _cached_prefix(msgs):
        if len(msgs) > 2 and msgs[-1].get('role') == 'user':
            assert '[Coverage]' in msgs[-1]['content'] or '[Working Memory]' in msgs[-1]['content'] or '[Convergence]' in msgs[-1]['content']
            return msgs[:-1]
        return msgs

    for idx in range(1, len(m.inputs)):
        prev_prefix = _cached_prefix(m.inputs[idx - 1])
        curr_prefix = m.inputs[idx][:len(prev_prefix)]
        assert json.dumps(curr_prefix, sort_keys=True) == json.dumps(prev_prefix, sort_keys=True)

    # Direct helper check below watermark is a pure no-op.
    snapshot_before = json.dumps(saved[-1]['history'], sort_keys=True)
    report = compact_runtime_history(saved[-1], keep_recent=2, trigger_chars=120_000, force=False)
    assert report['compacted'] is False
    assert report['reason'] == 'below_threshold'
    assert report['cleared_count'] == 0
    assert json.dumps(saved[-1]['history'], sort_keys=True) == snapshot_before


def test_compact_runtime_history_handles_bulk_protected_idempotency_and_force_modes():
    from app.research_runtime import compact_runtime_history

    big_text = 'R' * 2_000
    state = {
        'user': '{}',
        'read_ranges': [{'source_id': 's1', 'start': 0, 'end': 2_000}],
        'facts': [{'fact_id': 'f1'}, {'fact_id': 'f2'}],
        'calculations': [{'calculation_id': 'calc1'}],
        'events': [
            {'kind': 'tool', 'tool_call_id': 't_search', 'name': 'search_source', 'status': 'completed'},
        ],
        'history': [
            {
                'assistant': {'role': 'assistant', 'content': [
                    {'type': 'tool_use', 'id': 't_read_anth', 'name': 'read_sources',
                     'input': {'reads': [{'source_id': 's1', 'start': 0, 'end': 2_000}], 'rationale': 'r'}},
                    {'type': 'tool_use', 'id': 't_search', 'name': 'search_source',
                     'input': {'source_id': 's1', 'query': 'Revenue'}},
                ]},
                'results': [
                    {
                        'tool_call_id': 't_read_anth',
                        'content': json.dumps({'results': [{'source_id': 's1', 'start': 0, 'end': 2_000,
                                                            'status': 'read', 'text': big_text}]}),
                        'is_error': False,
                    },
                    {
                        'tool_call_id': 't_search',
                        'content': json.dumps({'source_id': 's1', 'status': 'completed', 'query': 'Revenue',
                                               'total_matches': 3, 'hits': [{'start': 10, 'end': 17, 'snippet': 'S' * 900}]}),
                        'is_error': False,
                    },
                ],
            },
            {
                'assistant': {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 't_facts', 'type': 'function', 'function': {'name': 'record_facts', 'arguments': '{}'}},
                    {'id': 't_sec', 'type': 'function', 'function': {'name': 'sec_company_concept', 'arguments': '{}'}},
                    {'id': 't_tiny', 'type': 'function', 'function': {'name': 'read_sources', 'arguments': '{}'}},
                ]},
                'results': [
                    {
                        'tool_call_id': 't_facts',
                        'content': json.dumps({'facts': [{'fact_id': 'f1', 'quotation': 'Q' * 1_200}]}),
                        'is_error': False,
                    },
                    {
                        'tool_call_id': 't_sec',
                        'content': json.dumps({'status': 'acquired', 'source_id': 's_sec', 'note': 'X' * 1_000}),
                        'is_error': False,
                    },
                    {
                        'tool_call_id': 't_tiny',
                        'content': json.dumps({'results': [{'source_id': 's_tiny', 'status': 'read', 'text': 'ok'}]}),
                        'is_error': False,
                    },
                ],
            },
            {
                'assistant': {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 't_recent_1', 'type': 'function', 'function': {'name': 'read_sources', 'arguments': '{}'}},
                ]},
                'results': [
                    {
                        'tool_call_id': 't_recent_1',
                        'content': json.dumps({'results': [{'source_id': 's2', 'start': 0, 'end': 1_500,
                                                            'status': 'read', 'text': 'Z' * 1_500}]}),
                        'is_error': False,
                    },
                ],
            },
            {
                'assistant': {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 't_recent_2', 'type': 'function', 'function': {'name': 'outline_source', 'arguments': '{}'}},
                ]},
                'results': [
                    {
                        'tool_call_id': 't_recent_2',
                        'content': json.dumps({'source_id': 's2', 'entries': [{'title': 'H' * 1_200}]}),
                        'is_error': False,
                    },
                ],
            },
        ],
    }

    recent_snapshot = json.dumps(state['history'][-2:], sort_keys=True)
    res1 = compact_runtime_history(state, keep_recent=2, trigger_chars=3_000, force=False)
    assert res1['compacted'] is True
    assert res1['reason'] == 'context_compacted'
    assert res1['cleared_count'] == 3  # t_read_anth, t_search, t_sec (t_facts protected, t_tiny too small)
    assert res1['chars_saved'] > 3_000
    assert state['compaction_count'] == 1
    assert json.dumps(state['history'][-2:], sort_keys=True) == recent_snapshot

    # Protected record_facts and tiny read_sources stayed verbatim.
    facts_entry = json.loads(state['history'][1]['results'][0]['content'])
    assert 'facts' in facts_entry and 'cleared' not in facts_entry
    tiny_entry = json.loads(state['history'][1]['results'][2]['content'])
    assert 'results' in tiny_entry and 'cleared' not in tiny_entry

    # Compacted descriptors preserve fields and recovery hint.
    read_desc = json.loads(state['history'][0]['results'][0]['content'])
    assert read_desc['cleared'] is True
    assert read_desc['tool'] == 'read_sources'
    assert read_desc['reason'] == 'context_compacted'
    assert read_desc['source_ids'] == ['s1']
    assert read_desc['source_spans'] == [{'source_id': 's1', 'start': 0, 'end': 2_000}]
    assert read_desc['original_chars'] > 2_000
    assert read_desc['facts_recorded_count'] == 2
    assert 'read_ranges remain valid' in read_desc['hint']

    search_desc = json.loads(state['history'][0]['results'][1]['content'])
    assert search_desc['cleared'] is True and search_desc['tool'] == 'search_source'
    assert search_desc['query'] == 'Revenue' and search_desc['total_matches'] == 3

    # State anchors remain completely intact.
    assert len(state['read_ranges']) == 1
    assert len(state['facts']) == 2
    assert len(state['calculations']) == 1
    assert len(state['events']) == 1

    # Re-running at same cutoff skips already-cleared entries (zero mutation).
    snapshot_after_l1 = json.dumps(state['history'], sort_keys=True)
    res_idem = compact_runtime_history(state, keep_recent=2, trigger_chars=500, force=False)
    assert res_idem['compacted'] is False
    assert res_idem['cleared_count'] == 0
    assert state['compaction_count'] == 1
    assert json.dumps(state['history'], sort_keys=True) == snapshot_after_l1

    # L2 forced compaction with keep_recent=0 clears recent bulk items too, leaving already-cleared older items byte-identical.
    first_two_before = json.dumps(state['history'][:2], sort_keys=True)
    res_force = compact_runtime_history(state, keep_recent=0, force=True)
    assert res_force['compacted'] is True
    assert res_force['reason'] == 'continuation_compacted'
    assert res_force['cleared_count'] == 2  # t_recent_1, t_recent_2
    assert state['compaction_count'] == 2
    assert json.dumps(state['history'][:2], sort_keys=True) == first_two_before
    recent1_desc = json.loads(state['history'][2]['results'][0]['content'])
    assert recent1_desc['cleared'] is True and recent1_desc['reason'] == 'continuation_compacted'


def test_l1_compaction_triggers_under_tight_context_and_record_facts_still_locates():
    quote_1 = 'FY2025 total revenue was $130,497 million.'
    quote_2 = 'FY2024 total revenue was $60,922 million.'
    doc_text = (
        quote_1 + ' ' + ('Filler segment one. ' * 900)
        + quote_2 + ' ' + ('Filler segment two. ' * 900)
        + 'Segment three closing notes. ' * 900
    )
    src = source()
    src.source_id = 's_comp'
    src.title = 'NVDA Filing'
    src.text = doc_text
    src.content_sha256 = sha256(src.text.encode()).hexdigest()

    q2_start = doc_text.index(quote_2)
    s3_start = doc_text.index('Segment three')

    class TightCapacityModel:
        policy = SimpleNamespace(timeout_seconds=10, max_input_tokens=None, max_output_tokens=1000)

        def __init__(self):
            self.turns = []
            self.step = 0

        def research_capacity(self):
            from app.research_runtime import SYSTEM, _TOOLS_SERIALIZED_CHARS
            # Keep ~45k chars available above SYSTEM + serialized tool schemas (~44k after user payload).
            context_tokens = int((45_000 + len(SYSTEM) + _TOOLS_SERIALIZED_CHARS) / (3 * 0.58))
            return SimpleNamespace(context_tokens=context_tokens, output_tokens=1000, input_counter='chars_div_3')

        def estimate_turn_input(self, turn_obj):
            return 100

        def complete_turn(self, turn_obj, **kwargs):
            self.turns.append(turn_obj)
            self.step += 1
            if self.step == 1:
                return ModelResponse(
                    {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'r1', 'name': 'read_sources',
                                                       'input': {'reads': [{'source_id': 's_comp', 'start': 0, 'end': 18_000}], 'rationale': 's1'}}]},
                    (ModelToolCall('r1', 'read_sources', {'reads': [{'source_id': 's_comp', 'start': 0, 'end': 18_000}], 'rationale': 's1'}),),
                    ModelUsage(10, 5, 15), 'tool_use', 'tool_calls',
                )
            if self.step == 2:
                return ModelResponse(
                    {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'r2', 'name': 'read_sources',
                                                       'input': {'reads': [{'source_id': 's_comp', 'start': q2_start, 'end': q2_start + 18_000}], 'rationale': 's2'}}]},
                    (ModelToolCall('r2', 'read_sources', {'reads': [{'source_id': 's_comp', 'start': q2_start, 'end': q2_start + 18_000}], 'rationale': 's2'}),),
                    ModelUsage(10, 5, 15), 'tool_use', 'tool_calls',
                )
            if self.step == 3:
                return ModelResponse(
                    {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'r3', 'name': 'read_sources',
                                                       'input': {'reads': [{'source_id': 's_comp', 'start': s3_start, 'end': min(len(doc_text), s3_start + 18_000)}], 'rationale': 's3'}}]},
                    (ModelToolCall('r3', 'read_sources', {'reads': [{'source_id': 's_comp', 'start': s3_start, 'end': min(len(doc_text), s3_start + 18_000)}], 'rationale': 's3'}),),
                    ModelUsage(10, 5, 15), 'tool_use', 'tool_calls',
                )
            if self.step == 4:
                # r1 (>45k total history now!) must be compacted to descriptor; r2 & r3 stay verbatim!
                r1_view = json.loads(turn_obj.history[0].results[0].content)
                r2_view = json.loads(turn_obj.history[1].results[0].content)
                r3_view = json.loads(turn_obj.history[2].results[0].content)
                assert r1_view['cleared'] is True and r1_view['tool'] == 'read_sources'
                assert r1_view['source_spans'] == [{'source_id': 's_comp', 'start': 0, 'end': 18_000}]
                assert 'cleared' not in r2_view and quote_2 in r2_view['results'][0]['text']
                assert 'cleared' not in r3_view

                # record_facts citing quote_1 from the compacted r1 slice MUST still locate via state['read_ranges']!
                return ModelResponse(
                    {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'f1', 'name': 'record_facts', 'input': {
                        'facts': [{
                            'label': 'FY2025 revenue', 'value': 130497, 'unit': 'millions', 'currency': 'USD',
                            'period': 'FY2025', 'source_id': 's_comp', 'quotation': quote_1,
                        }],
                        'rationale': 'record compacted slice fact',
                    }}]},
                    (ModelToolCall('f1', 'record_facts', {
                        'facts': [{
                            'label': 'FY2025 revenue', 'value': 130497, 'unit': 'millions', 'currency': 'USD',
                            'period': 'FY2025', 'source_id': 's_comp', 'quotation': quote_1,
                        }],
                        'rationale': 'record compacted slice fact',
                    }),),
                    ModelUsage(10, 5, 15), 'tool_use', 'tool_calls',
                )
            # Step 5: below watermark after L1 compaction -> prefix [0..3] must match step 4 verbatim!
            assert turn_obj.history[:3] == self.turns[3].history[:3]
            return ModelResponse(
                {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'sub1', 'name': 'submit_research', 'input': {
                    'report': 'FY2025 revenue was $130,497 million.',
                    'claims': [{'text': 'FY2025 revenue was $130,497 million.', 'source_id': 's_comp',
                                'quotation': quote_1, 'role': 'support'}],
                    'limitations': [],
                    'stopping_reason': 'answered',
                }}]},
                (ModelToolCall('sub1', 'submit_research', {
                    'report': 'FY2025 revenue was $130,497 million.',
                    'claims': [{'text': 'FY2025 revenue was $130,497 million.', 'source_id': 's_comp',
                                'quotation': quote_1, 'role': 'support'}],
                    'limitations': [],
                    'stopping_reason': 'answered',
                }),),
                ModelUsage(10, 5, 15), 'tool_use', 'tool_calls',
            )

    saved = []
    model = TightCapacityModel()
    outcome = run_research_agent(
        question='What was FY2025 revenue?',
        sources=[src],
        model=model,
        context=context(),
        checkpoint=lambda s: saved.append(json.loads(json.dumps(s))),
    )
    assert outcome.status == 'completed'
    assert len(outcome.facts) == 1
    assert outcome.facts[0]['character_start'] == 0
    assert saved[-1]['compaction_count'] == 1
    assert len(saved[-1]['read_ranges']) == 3


def test_failed_attempts_and_partial_notes_recorded_across_truncation_context_incomplete_and_no_submission(monkeypatch):
    from app.anthropic_messages import AnthropicMessagesProvider
    from app.openai_compatible import HttpResponse
    from app.phase3 import ProviderFailure, ProviderPolicy

    src = source()
    src.text = 'Revenue was 100 USD. Revenue was 80 USD.'

    # 1. Real AnthropicMessagesProvider hitting stop_reason='max_tokens' after reading a source:
    #    records failed_attempts with stop_reason='max_tokens', usage, history_chars, and deterministic partial_notes.
    call_idx = {'n': 0}

    def transport(method, url, headers, body, timeout, *, diagnostics, **kwargs):
        call_idx['n'] += 1
        diagnostics.submission('response_received')
        if call_idx['n'] == 1:
            return HttpResponse(200, json.dumps({
                'role': 'assistant',
                'stop_reason': 'tool_use',
                'content': [{'type': 'tool_use', 'id': 'r1', 'name': 'read_sources',
                             'input': {'reads': [{'source_id': 's1'}], 'rationale': 'read'}}],
                'usage': {'input_tokens': 10, 'output_tokens': 5},
            }), {})
        return HttpResponse(200, json.dumps({
            'role': 'assistant',
            'stop_reason': 'max_tokens',
            'content': [{'type': 'thinking', 'thinking': 'long thinking...', 'signature': 'sig'}],
            'usage': {'input_tokens': 50, 'output_tokens': 100},
        }), {})

    monkeypatch.setattr('app.anthropic_messages._default_transport', transport)
    settings = SimpleNamespace(base_url='https://example.com', model='model', api_key='fixture', allow_private_network=False)
    provider = AnthropicMessagesProvider(settings, ProviderPolicy(retry_base_seconds=0),
        anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=100)
    saved = []
    res_trunc = run_research_agent(
        question='Compare revenue?',
        sources=[src],
        model=provider,
        context=context(),
        checkpoint=lambda s: saved.append(json.loads(json.dumps(s))),
    )
    assert res_trunc.status == 'partial'
    assert res_trunc.reason == 'provider_output_truncated'
    assert res_trunc.partial_notes is not None
    assert 'provider_output_truncated' in res_trunc.partial_notes
    assert '已收集资料 1 份' in res_trunc.partial_notes
    assert '已读片段 1 段' in res_trunc.partial_notes
    assert '已记录事实 0 条' in res_trunc.partial_notes
    assert '已完成计算 0 项' in res_trunc.partial_notes
    final_cp = saved[-1]
    assert final_cp['pending'] is None
    assert len(final_cp['failed_attempts']) == 1
    attempt = final_cp['failed_attempts'][0]
    assert attempt['reason'] == 'provider_output_truncated'
    assert attempt['finish_reason'] == 'max_tokens'
    assert attempt['usage'] == 150
    assert attempt['history_chars'] > 0
    assert isinstance(attempt['call_id'], str) and attempt['call_id']
    assert isinstance(attempt['at'], str) and attempt['at']

    # Resuming from that checkpoint keeps turn.user byte-identical to state['user'] (I-FIN-CTX-001)
    # and passes prior_failed_attempts + recovery_guidance via turn.tail_note.
    original_user = final_cp['user']

    class CaptureResume:
        def __init__(self):
            self.turns = []

        def estimate_turn_input(self, t):
            return 10

        def complete_turn(self, t, **kwargs):
            self.turns.append(t)
            return ModelResponse(
                {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 's_ok', 'name': 'submit_research', 'input': {
                    'report': 'Revenue was 100 USD.',
                    'claims': [{'text': 'Revenue was 100 USD.', 'source_id': 's1',
                                'quotation': 'Revenue was 100 USD.', 'role': 'support'}],
                    'limitations': [],
                    'stopping_reason': 'answered',
                }}]},
                (ModelToolCall('s_ok', 'submit_research', {
                    'report': 'Revenue was 100 USD.',
                    'claims': [{'text': 'Revenue was 100 USD.', 'source_id': 's1',
                                'quotation': 'Revenue was 100 USD.', 'role': 'support'}],
                    'limitations': [],
                    'stopping_reason': 'answered',
                }),),
                ModelUsage(20, 10, 30), 'tool_use', 'tool_calls',
            )

    resumed_saved = []
    capture = CaptureResume()
    res_ok = run_research_agent(
        question='Compare revenue?',
        sources=[src],
        model=capture,
        context=context(),
        initial_state=final_cp,
        checkpoint=lambda s: resumed_saved.append(json.loads(json.dumps(s))),
    )
    assert res_ok.status == 'partial'  # One period does not answer a comparison.
    assert res_ok.partial_notes is not None
    assert resumed_saved[-1]['user'] == original_user
    assert capture.turns[0].user == original_user
    tail_note = capture.turns[0].tail_note or ''
    assert tail_note.startswith('[Recovery context: ')
    tail_payload = json.loads(tail_note[len('[Recovery context: '):-1])
    assert tail_payload['prior_failed_attempts'][0]['reason'] == 'provider_output_truncated'
    assert 'search_source' in tail_payload['recovery_guidance']

    # 2. received=False truncation and context_limit still save failed_attempts & clear pending!
    for code in ('provider_output_truncated', 'provider_context_limit'):
        class UnconfirmedTruncModel:
            def complete(self, messages, tools, **kwargs):
                raise ProviderFailure(code, f'{code} msg', retryable=False)

        cp_list = []
        out = run_research_agent(
            question='Q',
            sources=[src],
            model=UnconfirmedTruncModel(),
            context=context(),
            checkpoint=lambda s: cp_list.append(json.loads(json.dumps(s))),
        )
        assert out.reason == code
        assert out.partial_notes is not None and code in out.partial_notes
        assert cp_list[-1]['pending'] is None
        assert cp_list[-1]['failed_attempts'][-1]['reason'] == code

    # 3. incomplete_model_response & no_submission record failed_attempts and partial_notes.
    class IncompleteModel:
        def estimate_turn_input(self, t):
            return 10

        def complete_turn(self, t, **kwargs):
            return ModelResponse({'role': 'assistant', 'content': []}, (), ModelUsage(5, 8, 13), 'max_tokens', 'truncated')

    inc_saved = []
    out_inc = run_research_agent(
        question='Q',
        sources=[src],
        model=IncompleteModel(),
        context=context(),
        checkpoint=lambda s: inc_saved.append(json.loads(json.dumps(s))),
    )
    assert out_inc.reason == 'incomplete_model_response'
    assert out_inc.partial_notes is not None and 'incomplete_model_response' in out_inc.partial_notes
    assert inc_saved[-1]['failed_attempts'][-1]['reason'] == 'incomplete_model_response'
    assert inc_saved[-1]['failed_attempts'][-1]['finish_reason'] == 'max_tokens'
    assert inc_saved[-1]['failed_attempts'][-1]['usage'] == 13

    no_sub_saved = []
    out_nosub = run_research_agent(
        question='Q',
        sources=[src],
        model=Model([ModelResponse({'role': 'assistant', 'content': 'stopping early'}, (), ModelUsage(4, 3, 7), 'stop')]),
        context=context(),
        checkpoint=lambda s: no_sub_saved.append(json.loads(json.dumps(s))),
    )
    assert out_nosub.reason == 'no_submission'
    assert out_nosub.partial_notes is not None and 'no_submission' in out_nosub.partial_notes
    assert no_sub_saved[-1]['failed_attempts'][-1]['reason'] == 'no_submission'
    assert no_sub_saved[-1]['failed_attempts'][-1]['usage'] == 7

    # 4. settlement_failed records failed_attempts & generates informative partial_notes.
    c_settle = context()
    c_settle.settle = lambda *_: (_ for _ in ()).throw(RuntimeError('ledger down'))
    settle_saved = []
    out_settle = run_research_agent(
        question='Q',
        sources=[src],
        model=Model([turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'r_settle')]),
        context=c_settle,
        checkpoint=lambda s: settle_saved.append(json.loads(json.dumps(s))),
    )
    assert out_settle.reason == 'settlement_failed'
    assert out_settle.partial_notes is not None and 'settlement_failed' in out_settle.partial_notes
    assert settle_saved[-1]['failed_attempts'][-1]['reason'] == 'settlement_failed'


def test_default_90k_and_max_input_tokens_watermarks_and_giant_protected_force():
    from app.research_runtime import DEFAULT_L1_COMPACT_TRIGGER_CHARS, compact_runtime_history

    assert DEFAULT_L1_COMPACT_TRIGGER_CHARS == 90_000

    # 1. Default 90,000-char L1 watermark in run_research_agent:
    #    4 x 20,000-char reads (~82k history) stay verbatim; 5th read pushes >90k and compacts older turns!
    doc_text = ''.join(f'[CHUNK_{i:02d}] ' + ('X' * 19_989) for i in range(6))
    src = source()
    src.source_id = 's_big'
    src.text = doc_text
    src.content_sha256 = 'hash-big'

    class SixReadsModel:
        def __init__(self):
            self.step = 0
            self.turns = []

        def estimate_turn_input(self, turn_obj):
            return 100

        def complete_turn(self, turn_obj, **kwargs):
            self.turns.append(turn_obj)
            self.step += 1
            if self.step <= 6:
                start = (self.step - 1) * 20_000
                return ModelResponse(
                    {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': f'r{self.step}', 'name': 'read_sources',
                                                       'input': {'reads': [{'source_id': 's_big', 'start': start, 'end': start + 20_000}], 'rationale': f'read {self.step}'}}]},
                    (ModelToolCall(f'r{self.step}', 'read_sources',
                                   {'reads': [{'source_id': 's_big', 'start': start, 'end': start + 20_000}], 'rationale': f'read {self.step}'}),),
                    ModelUsage(10, 5, 15), 'tool_use', 'tool_calls',
                )
            return ModelResponse(
                {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'sub', 'name': 'submit_research', 'input': {
                    'report': 'Verified chunk 0 and chunk 5.',
                    'claims': [{'text': 'Chunk 0 seen.', 'source_id': 's_big', 'quotation': '[CHUNK_00]', 'role': 'support'}],
                    'limitations': [],
                    'stopping_reason': 'answered',
                }}]},
                (ModelToolCall('sub', 'submit_research', {
                    'report': 'Verified chunk 0 and chunk 5.',
                    'claims': [{'text': 'Chunk 0 seen.', 'source_id': 's_big', 'quotation': '[CHUNK_00]', 'role': 'support'}],
                    'limitations': [],
                    'stopping_reason': 'answered',
                }),),
                ModelUsage(10, 5, 15), 'tool_use', 'tool_calls',
            )

    m_big = SixReadsModel()
    saved_big = []
    res_big = run_research_agent(
        question='Inspect big filing?',
        sources=[src],
        model=m_big,
        context=context(),
        checkpoint=lambda s: saved_big.append(json.loads(json.dumps(s))),
    )
    assert res_big.status == 'partial'  # The fixture only supports one chunk, not its report.
    # Before step 5 (after 4 x 20k = ~82k history), zero compaction! Before step 6 (5 x 20k = ~102k > 90k), L1 triggers once!
    assert 'cleared' not in m_big.turns[4].history[0].results[0].content
    assert json.loads(m_big.turns[5].history[0].results[0].content)['cleared'] is True
    assert json.loads(m_big.turns[5].history[2].results[0].content)['cleared'] is True
    assert 'cleared' not in m_big.turns[5].history[3].results[0].content  # keep_recent=2
    assert 'cleared' not in m_big.turns[5].history[4].results[0].content
    # Step 7 is below 90k again -> steps 0..4 stay byte-identical to step 6!
    assert m_big.turns[6].history[:5] == m_big.turns[5].history[:5]
    assert saved_big[-1]['compaction_count'] == 1

    # 2. Policy max_input_tokens tightens L1 trigger_chars so 4 x 6k reads compact instead of overflowing 25k limit.
    small_doc = ''.join(f'[PART_{i}] ' + ('Y' * 5_991) for i in range(4))
    small_src = source()
    small_src.source_id = 's_pol'
    small_src.text = small_doc
    small_src.content_sha256 = 'hash-pol'

    class PolicyTightModel:
        policy = SimpleNamespace(timeout_seconds=10, max_input_tokens=24_000, max_output_tokens=1000)

        def __init__(self):
            self.step = 0
            self.turns = []

        def estimate_turn_input(self, turn_obj):
            return len(json.dumps([{'role': 'user', 'content': turn_obj.user}], ensure_ascii=False)) + sum(
                len(r.content) for ex in turn_obj.history for r in ex.results
            )

        def complete_turn(self, turn_obj, **kwargs):
            self.turns.append(turn_obj)
            self.step += 1
            if self.step <= 3:
                start = (self.step - 1) * 6_000
                return ModelResponse(
                    {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': f'p{self.step}', 'name': 'read_sources',
                                                       'input': {'reads': [{'source_id': 's_pol', 'start': start, 'end': start + 6_000}], 'rationale': 'read'}}]},
                    (ModelToolCall(f'p{self.step}', 'read_sources',
                                   {'reads': [{'source_id': 's_pol', 'start': start, 'end': start + 6_000}], 'rationale': 'read'}),),
                    ModelUsage(10, 5, 15), 'tool_use', 'tool_calls',
                )
            assert json.loads(turn_obj.history[0].results[0].content)['cleared'] is True
            return ModelResponse(
                {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'psub', 'name': 'submit_research', 'input': {
                    'report': 'Part 0 seen.',
                    'claims': [{'text': 'Part 0.', 'source_id': 's_pol', 'quotation': '[PART_0]', 'role': 'support'}],
                    'limitations': [],
                    'stopping_reason': 'answered',
                }}]},
                (ModelToolCall('psub', 'submit_research', {
                    'report': 'Part 0 seen.',
                    'claims': [{'text': 'Part 0.', 'source_id': 's_pol', 'quotation': '[PART_0]', 'role': 'support'}],
                    'limitations': [],
                    'stopping_reason': 'answered',
                }),),
                ModelUsage(10, 5, 15), 'tool_use', 'tool_calls',
            )

    res_pol = run_research_agent(question='Check policy?', sources=[small_src], model=PolicyTightModel(), context=context())
    assert res_pol.status == 'partial'  # This fixture only cites an unrelated marker.

    # 3. Protected tool > 8000 chars stays protected under force=False, compacts only under force=True.
    prot_state = {
        'history': [
            {
                'assistant': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'rf1', 'name': 'record_facts', 'input': {}}]},
                'results': [{'tool_call_id': 'rf1', 'content': json.dumps({'facts': [{'q': 'A' * 9_000}]}), 'is_error': False}],
            },
            {
                'assistant': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'rf2', 'name': 'submit_research', 'input': {}}]},
                'results': [{'tool_call_id': 'rf2', 'content': 'ok', 'is_error': False}],
            },
        ],
        'facts': [{'fact_id': 'f1'}],
        'events': [],
    }
    no_force = compact_runtime_history(prot_state, keep_recent=1, trigger_chars=1_000, force=False)
    assert no_force['compacted'] is False
    yes_force = compact_runtime_history(prot_state, keep_recent=1, force=True)
    assert yes_force['compacted'] is True
    assert json.loads(prot_state['history'][0]['results'][0]['content'])['cleared'] is True


def test_submit_report_and_system_declare_restricted_gfm_markdown():
    from app.research_runtime import REPORT_FORMAT_GUIDANCE, SYSTEM, TOOLS, SubmitRequest

    description = SubmitRequest.model_fields['report'].description
    assert description == REPORT_FORMAT_GUIDANCE
    submit_tool = next(tool for tool in TOOLS if tool.name == 'submit_research')
    assert submit_tool.input_schema['properties']['report']['description'] == REPORT_FORMAT_GUIDANCE
    assert REPORT_FORMAT_GUIDANCE in SYSTEM
    for token in ('restricted GFM Markdown', 'GFM tables', 'raw HTML', '![](...)', '$130.5B', 'single $'):
        assert token in REPORT_FORMAT_GUIDANCE
    # Storage contract unchanged: report remains a bounded plain string field.
    assert SubmitRequest.model_fields['report'].annotation is str
    assert SubmitRequest.model_json_schema()['properties']['report']['maxLength'] == 100000


def test_provider_timeout_recorded_across_exit_paths_with_metadata(monkeypatch):
    from app.phase3 import ProviderFailure
    from app.research_runtime import _user_with_failure_awareness

    src = source()

    # 1. received=True timeout (model_returned error branch) records failed_attempts + bounded metadata,
    #    and survives recovery if interrupted while pending['kind'] == 'model_returned'.
    class StreamTimeoutModel:
        def estimate_turn_input(self, t):
            return 10

        def complete_turn(self, t, **kwargs):
            err = ProviderFailure('provider_timeout', 'Stream timed out.', retryable=True, reported_tokens=None)
            err.provider_response_received = True
            err.timeout_kind = 'stream_deadline_timeout'
            err.elapsed_ms = 120000
            err.wire_bytes = 88610
            err.streamed_bytes = 4096
            raise err

    saved_received = []
    out_received = run_research_agent(
        question='Analyze trend',
        sources=[src],
        model=StreamTimeoutModel(),
        context=context(),
        checkpoint=lambda s: saved_received.append(json.loads(json.dumps(s))),
    )
    assert out_received.status == 'partial'
    assert out_received.reason == 'provider_timeout'
    cp_received = saved_received[-1]
    assert cp_received['pending'] is None
    assert len(cp_received['failed_attempts']) == 1
    attempt = cp_received['failed_attempts'][0]
    assert attempt['reason'] == 'provider_timeout'
    assert attempt['timeout_kind'] == 'stream_deadline_timeout'
    assert attempt['elapsed_ms'] == 120000
    assert attempt['wire_bytes'] == 88610
    assert attempt['streamed_bytes'] == 4096

    # Crash between pending='model_returned' save and finish_model still retains metadata on recovery.
    model_returned_cp = next(s for s in saved_received if (s.get('pending') or {}).get('kind') == 'model_returned')
    assert model_returned_cp['pending']['error']['timeout_kind'] == 'stream_deadline_timeout'
    resumed_saved = []
    resumed_out = run_research_agent(
        question='Analyze trend',
        sources=[src],
        model=StreamTimeoutModel(),
        context=context(),
        initial_state=model_returned_cp,
        checkpoint=lambda s: resumed_saved.append(json.loads(json.dumps(s))),
    )
    assert resumed_out.reason == 'provider_timeout'
    assert resumed_saved[-1]['failed_attempts'][-1]['timeout_kind'] == 'stream_deadline_timeout'
    assert resumed_saved[-1]['failed_attempts'][-1]['wire_bytes'] == 88610

    # Transient failure awareness forwards bounded timeout metadata and timeout guidance.
    aware = json.loads(_user_with_failure_awareness(json.dumps({'question': 'Analyze trend'}), cp_received))
    assert aware['prior_failed_attempts'][0]['timeout_kind'] == 'stream_deadline_timeout'
    assert aware['prior_failed_attempts'][0]['elapsed_ms'] == 120000
    assert aware['prior_failed_attempts'][0]['wire_bytes'] == 88610
    assert aware['prior_failed_attempts'][0]['streamed_bytes'] == 4096
    assert 'record_facts' in aware['recovery_guidance']

    # 2. received=False (started_unknown) preserves pending['kind']=='model' AND records failed_attempts.
    class HeaderTimeoutModel:
        def estimate_turn_input(self, t):
            return 10

        def complete_turn(self, t, **kwargs):
            err = ProviderFailure('provider_timeout', 'Header timed out.', retryable=True)
            err.timeout_kind = 'header_timeout'
            err.elapsed_ms = 45000
            err.wire_bytes = 1234
            err.streamed_bytes = 0
            raise err

    saved_unknown = []
    out_unknown = run_research_agent(
        question='Q',
        sources=[src],
        model=HeaderTimeoutModel(),
        context=context(),
        checkpoint=lambda s: saved_unknown.append(json.loads(json.dumps(s))),
    )
    assert out_unknown.reason == 'provider_timeout'
    assert saved_unknown[-1]['pending']['kind'] == 'model'
    assert saved_unknown[-1]['failed_attempts'][-1]['reason'] == 'provider_timeout'
    assert saved_unknown[-1]['failed_attempts'][-1]['timeout_kind'] == 'header_timeout'
    assert saved_unknown[-1]['failed_attempts'][-1]['elapsed_ms'] == 45000
    assert saved_unknown[-1]['failed_attempts'][-1]['wire_bytes'] == 1234
    assert saved_unknown[-1]['failed_attempts'][-1]['streamed_bytes'] == 0

    # 3. not_started after pre-send retries exhaust: records failed_attempts and derives stage fallback from call_diag.
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRIES', '1')
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_PRESEND_RETRY_BASE_SECONDS', '0')

    class ConnectTimeoutModel:
        def __init__(self):
            self.calls = 0

        def estimate_turn_input(self, t):
            return 10

        def complete_turn(self, t, *, diagnostic_callback=None, **kwargs):
            self.calls += 1
            if diagnostic_callback is not None:
                diagnostic_callback({
                    'submission_state': 'not_started',
                    'stages': [{'stage': 'tls', 'result': 'timeout', 'elapsed_ms': 15000}],
                    'wire_bytes': 512,
                })
            raise ProviderFailure('provider_timeout', 'TLS handshake timed out.', retryable=True)

    connect_model = ConnectTimeoutModel()
    saved_presend = []
    out_presend = run_research_agent(
        question='Q',
        sources=[src],
        model=connect_model,
        context=context(),
        checkpoint=lambda s: saved_presend.append(json.loads(json.dumps(s))),
    )
    assert out_presend.reason == 'provider_timeout'
    assert connect_model.calls == 2
    assert saved_presend[-1]['pending'] is None
    assert len(saved_presend[-1]['failed_attempts']) == 1
    presend_attempt = saved_presend[-1]['failed_attempts'][0]
    assert presend_attempt['reason'] == 'provider_timeout'
    assert presend_attempt['timeout_kind'] == 'connect_timeout'
    assert presend_attempt['elapsed_ms'] == 15000
    assert presend_attempt['wire_bytes'] == 512

    # 4. Graceful degradation when error & call_diag have no timeout metadata at all.
    class BareTimeoutModel:
        def complete(self, messages, tools, **kwargs):
            raise ProviderFailure('provider_timeout', 'Bare timeout.', retryable=True)

    saved_bare = []
    out_bare = run_research_agent(
        question='Q',
        sources=[src],
        model=BareTimeoutModel(),
        context=context(),
        checkpoint=lambda s: saved_bare.append(json.loads(json.dumps(s))),
    )
    assert out_bare.reason == 'provider_timeout'
    bare_attempt = saved_bare[-1]['failed_attempts'][-1]
    assert bare_attempt['reason'] == 'provider_timeout'
    for optional_key in ('timeout_kind', 'elapsed_ms', 'wire_bytes', 'streamed_bytes'):
        assert optional_key not in bare_attempt


def test_parallel_read_sources_turn_budget_and_resumed_tool_batch_enforcement():
    from app.research_runtime import MAX_READ_SOURCES_TURN_CHARS, MAX_TURN_TOOL_RESULT_CHARS

    assert MAX_READ_SOURCES_TURN_CHARS == 40_000
    assert MAX_TURN_TOOL_RESULT_CHARS == 60_000

    doc_text = ('A' * 25_000) + ('B' * 25_000) + ('C' * 25_000)
    src = source()
    src.source_id = 's_turn'
    src.text = doc_text
    src.content_sha256 = 'hash-turn'

    # 1. Same turn issuing 25k + 15k + 5k parallel read_sources calls:
    #    First 2 succeed (40k total); 3rd exceeds 40k turn budget -> status='invalid', is_error=True, read_ranges unpolluted.
    a1 = {'reads': [{'source_id': 's_turn', 'start': 0, 'end': 25_000}], 'rationale': 'part 1'}
    a2 = {'reads': [{'source_id': 's_turn', 'start': 25_000, 'end': 40_000}], 'rationale': 'part 2'}
    a3 = {'reads': [{'source_id': 's_turn', 'start': 40_000, 'end': 45_000}], 'rationale': 'part 3'}
    c1 = ModelToolCall('r1', 'read_sources', json.dumps(a1))
    c2 = ModelToolCall('r2', 'read_sources', json.dumps(a2))
    c3 = ModelToolCall('r3', 'read_sources', json.dumps(a3))
    parallel_turn = ModelResponse(
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'r1', 'type': 'function', 'function': {'name': 'read_sources', 'arguments': c1.arguments}},
            {'id': 'r2', 'type': 'function', 'function': {'name': 'read_sources', 'arguments': c2.arguments}},
            {'id': 'r3', 'type': 'function', 'function': {'name': 'read_sources', 'arguments': c3.arguments}},
        ]},
        (c1, c2, c3),
        ModelUsage(10, 5, 15),
        'tool_calls',
    )
    m = Model([parallel_turn, ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
    saved = []
    out = run_research_agent(
        question='Read chunks?',
        sources=[src],
        model=m,
        context=context(),
        checkpoint=lambda s: saved.append(json.loads(json.dumps(s))),
    )
    assert out.reason == 'no_submission'
    assert saved[-1]['read_ranges'] == [
        {'source_id': 's_turn', 'start': 0, 'end': 25_000},
        {'source_id': 's_turn', 'start': 25_000, 'end': 40_000},
    ]
    r3_msg = next(msg for msg in m.inputs[1] if msg.get('role') == 'tool' and msg.get('tool_call_id') == 'r3')
    r3_payload = json.loads(r3_msg['content'])
    assert r3_payload['status'] == 'invalid'
    assert r3_payload['error'] == 'read_turn_budget_exceeded'
    assert r3_payload['remaining_turn_characters'] == 0
    assert 'record_facts' in r3_payload['instruction']
    assert 'search_source' in r3_payload['instruction']
    assert saved[-1]['history'][0]['results'][2]['is_error'] is True

    # 2. Resumed tool_batch checkpoint (cursor=1 after r1=25k already ran) STILL enforces 40k turn budget!
    resumed_state = {
        'user': json.dumps({'question': 'Q'}),
        'history': [{'assistant': parallel_turn.message, 'results': [
            {'tool_call_id': 'r1', 'content': json.dumps({'results': [{'source_id': 's_turn', 'start': 0, 'end': 25_000, 'status': 'read', 'text': 'A' * 25_000}]}), 'is_error': False},
        ]}],
        'events': [],
        'read_ranges': [{'source_id': 's_turn', 'start': 0, 'end': 25_000}],
        'facts': [],
        'calculations': [],
        'charts': [],
        'pending': {
            'kind': 'tool_batch',
            'call_id': 'm1',
            'cursor': 1,
            'calls': [
                {'id': 'r1', 'name': 'read_sources', 'arguments': a1, 'raw_arguments': None},
                {'id': 'r2_big', 'name': 'read_sources', 'arguments': {'reads': [{'source_id': 's_turn', 'start': 25_000, 'end': 45_000}], 'rationale': 'too big'}, 'raw_arguments': None},
            ],
        },
    }
    m_resumed = Model([ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
    saved_res = []
    run_research_agent(
        question='Q',
        sources=[src],
        model=m_resumed,
        context=context(),
        initial_state=resumed_state,
        checkpoint=lambda s: saved_res.append(json.loads(json.dumps(s))),
    )
    assert saved_res[-1]['read_ranges'] == [{'source_id': 's_turn', 'start': 0, 'end': 25_000}]
    r2_res = json.loads(saved_res[-1]['history'][0]['results'][1]['content'])
    assert r2_res['status'] == 'invalid' and r2_res['error'] == 'read_turn_budget_exceeded'
    assert r2_res['remaining_turn_characters'] == 15_000

    # 3. General MAX_TURN_TOOL_RESULT_CHARS=60_000 blocks bulk tools above 60k while keeping record_facts allowed.
    fact_quote = 'Fact quote 100 USD.'
    src_bulk = source()
    src_bulk.source_id = 's_bulk'
    src_bulk.text = fact_quote + ' ' + ('Z' * 20_000)
    calls_bulk = [
        {'id': 'b1', 'name': 'read_sources', 'arguments': {'reads': [{'source_id': 's_bulk', 'start': 0, 'end': 20_000}], 'rationale': 'b1'}, 'raw_arguments': None},
        {'id': 'b2', 'name': 'outline_source', 'arguments': {'source_id': 's_bulk', 'rationale': 'b2'}, 'raw_arguments': None},
        {'id': 'b3', 'name': 'record_facts', 'arguments': {'facts': [{'label': 'num', 'value': 100, 'unit': 'USD', 'currency': 'USD', 'period': '2025', 'source_id': 's_bulk', 'quotation': fact_quote}], 'rationale': 'f'}, 'raw_arguments': None},
    ]
    bulk_state = {
        'user': json.dumps({'question': 'Q'}),
        'history': [{'assistant': {'role': 'assistant', 'content': []}, 'results': [
            {'tool_call_id': 'prior_bulk', 'content': json.dumps({'results': [{'text': 'X' * 60_500}]}), 'is_error': False},
        ]}],
        'events': [{'kind': 'tool', 'tool_call_id': 'prior_bulk', 'name': 'search_source', 'status': 'completed'}],
        'read_ranges': [{'source_id': 's_bulk', 'start': 0, 'end': 200}],
        'facts': [],
        'calculations': [],
        'charts': [],
        'pending': {'kind': 'tool_batch', 'call_id': 'mbulk', 'cursor': 1, 'calls': calls_bulk},
    }
    m_bulk = Model([ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
    saved_bulk = []
    run_research_agent(
        question='Q',
        sources=[src_bulk],
        model=m_bulk,
        context=context(),
        initial_state=bulk_state,
        checkpoint=lambda s: saved_bulk.append(json.loads(json.dumps(s))),
    )
    outline_res = json.loads(saved_bulk[-1]['history'][0]['results'][1]['content'])
    assert outline_res['status'] == 'invalid' and outline_res['error'] == 'turn_tool_result_budget_exceeded'
    assert saved_bulk[-1]['history'][0]['results'][1]['is_error'] is True
    assert len(saved_bulk[-1]['facts']) == 1  # Protected record_facts still succeeded!


def test_compact_runtime_history_clears_old_thinking_and_writes_context_receipts():
    from app.research_runtime import MAX_CONTEXT_RECEIPTS, compact_runtime_history

    state = {
        'user': '{}',
        'read_ranges': [{'source_id': 's1', 'start': 0, 'end': 1_500}],
        'facts': [{'fact_id': 'f1'}],
        'calculations': [],
        'events': [],
        'history': [
            {
                'assistant': {'role': 'assistant', 'content': [
                    {'type': 'thinking', 'thinking': 'T' * 2_000, 'signature': 'sig1'},
                    {'type': 'redacted_thinking', 'data': 'R' * 400},
                    {'type': 'tool_use', 'id': 'c1', 'name': 'read_sources', 'input': {'reads': [{'source_id': 's1', 'start': 0, 'end': 1_500}]}},
                ]},
                'results': [
                    {'tool_call_id': 'c1', 'content': json.dumps({'results': [{'source_id': 's1', 'start': 0, 'end': 1_500, 'text': 'D' * 1_500}]}), 'is_error': False},
                ],
            },
            {
                # Small tool result (<800 chars), but has a consumed thinking block -> still strips thinking!
                'assistant': {'role': 'assistant', 'content': [
                    {'type': 'thinking', 'thinking': 'S' * 1_000, 'signature': 'sig2'},
                    {'type': 'tool_use', 'id': 'c2', 'name': 'record_facts', 'input': {}},
                ]},
                'results': [
                    {'tool_call_id': 'c2', 'content': json.dumps({'facts': [{'fact_id': 'f1'}]}), 'is_error': False},
                ],
            },
            {
                # Only thinking block (no non-thinking block) -> safety guard keeps it intact.
                'assistant': {'role': 'assistant', 'content': [
                    {'type': 'thinking', 'thinking': 'ONLY_THINK', 'signature': 'sig3'},
                ]},
                'results': [
                    {'tool_call_id': 'c3', 'content': 'ok', 'is_error': False},
                ],
            },
            {
                # Recent turn inside keep_recent=1 -> untouched!
                'assistant': {'role': 'assistant', 'content': [
                    {'type': 'thinking', 'thinking': 'RECENT_THINK', 'signature': 'sig4'},
                    {'type': 'tool_use', 'id': 'c4', 'name': 'read_sources', 'input': {}},
                ]},
                'results': [
                    {'tool_call_id': 'c4', 'content': json.dumps({'results': [{'source_id': 's1', 'start': 1_500, 'end': 3_000, 'text': 'E' * 1_500}]}), 'is_error': False},
                ],
            },
        ],
    }

    report = compact_runtime_history(state, keep_recent=1, trigger_chars=1_000, force=False)
    assert report['compacted'] is True
    assert report['cleared_count'] == 1
    assert report['cleared_thinking_turns'] == 2
    assert report['cleared_thinking_chars'] > 3_000
    assert report['omitted_spans'] == [{'source_id': 's1', 'start': 0, 'end': 1_500}]

    # c1 & c2 thinking stripped while tool_use preserved; c3 safety-preserved; c4 recent untouched.
    assert [b['type'] for b in state['history'][0]['assistant']['content']] == ['tool_use']
    assert [b['type'] for b in state['history'][1]['assistant']['content']] == ['tool_use']
    assert [b['type'] for b in state['history'][2]['assistant']['content']] == ['thinking']
    assert [b['type'] for b in state['history'][3]['assistant']['content']] == ['thinking', 'tool_use']

    assert len(state['context_receipts']) == 1
    receipt = state['context_receipts'][0]
    assert receipt['trigger'] == 'l1_in_turn'
    assert receipt['cleared_tool_call_ids'] == ['c1']
    assert receipt['cleared_thinking_turns'] == 2
    assert receipt['omitted_spans'] == [{'source_id': 's1', 'start': 0, 'end': 1_500}]
    assert receipt['preserved_facts_count'] == 1
    assert receipt['prefix_reset'] is True
    assert receipt['after_chars'] < receipt['before_chars']
    assert receipt['chars_saved'] == receipt['before_chars'] - receipt['after_chars']
    assert isinstance(receipt['at'], str) and receipt['at']

    # Verify receipts are capped at MAX_CONTEXT_RECEIPTS (20).
    for idx in range(25):
        state['history'].insert(0, {
            'assistant': {'role': 'assistant', 'content': [
                {'type': 'thinking', 'thinking': f'T{idx}' * 100, 'signature': 'sig'},
                {'type': 'tool_use', 'id': f'cx_{idx}', 'name': 'search_source', 'input': {}},
            ]},
            'results': [{'tool_call_id': f'cx_{idx}', 'content': 'ok', 'is_error': False}],
        })
        compact_runtime_history(state, keep_recent=1, force=True, trigger='l1_in_turn')
    assert len(state['context_receipts']) == MAX_CONTEXT_RECEIPTS


def test_work_context_tokens_and_tail_note_preserve_messages_zero_byte_identical(monkeypatch):
    from app.anthropic_messages import AnthropicMessagesProvider
    from app.openai_compatible import HttpResponse
    from app.phase3 import ProviderPolicy
    from app.provider_capabilities import CapabilityProfile

    # 1. work_context_tokens triggers L1 at ~0.55 * work_context_tokens * 3 - fixed_chars without context_tokens.
    doc_text = ''.join(f'[W_{i:02d}] ' + ('X' * 9_993) for i in range(4))
    src = source()
    src.source_id = 's_wc'
    src.text = doc_text
    src.content_sha256 = 'hash-wc'

    class WorkContextModel:
        policy = SimpleNamespace(timeout_seconds=10, max_input_tokens=None, max_output_tokens=1000)

        def __init__(self):
            self.step = 0
            self.turns = []

        def research_capacity(self):
            # context_tokens=None (so admission unaffected), work_context_tokens=22_000 -> ~36,300 - ~18,800 ≈ 17,500 trigger
            return CapabilityProfile(
                endpoint='https://example.com/v1/messages',
                model='wc-model',
                version='2023-06-01',
                mode='json_object',
                context_tokens=None,
                work_context_tokens=22_000,
                input_counter='chars_div_3',
                native_tools=True,
                stream=False,
                continuation=True,
            )

        def estimate_turn_input(self, turn_obj):
            return 100

        def complete_turn(self, turn_obj, **kwargs):
            self.turns.append(turn_obj)
            self.step += 1
            if self.step <= 3:
                start = (self.step - 1) * 10_000
                return ModelResponse(
                    {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': f'w{self.step}', 'name': 'read_sources',
                                                       'input': {'reads': [{'source_id': 's_wc', 'start': start, 'end': start + 10_000}], 'rationale': 'r'}}]},
                    (ModelToolCall(f'w{self.step}', 'read_sources', {'reads': [{'source_id': 's_wc', 'start': start, 'end': start + 10_000}], 'rationale': 'r'}),),
                    ModelUsage(10, 5, 15), 'tool_use', 'tool_calls',
                )
            return ModelResponse(
                {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'sub', 'name': 'submit_research', 'input': {
                    'report': 'Seen [W_00].',
                    'claims': [{'text': 'Seen.', 'source_id': 's_wc', 'quotation': '[W_00]', 'role': 'support'}],
                    'limitations': [], 'stopping_reason': 'answered',
                }}]},
                (ModelToolCall('sub', 'submit_research', {
                    'report': 'Seen [W_00].',
                    'claims': [{'text': 'Seen.', 'source_id': 's_wc', 'quotation': '[W_00]', 'role': 'support'}],
                    'limitations': [], 'stopping_reason': 'answered',
                }),),
                ModelUsage(10, 5, 15), 'tool_use', 'tool_calls',
            )

    wc_model = WorkContextModel()
    wc_saved = []
    wc_out = run_research_agent(
        question='Check work context?',
        sources=[src],
        model=wc_model,
        context=context(),
        checkpoint=lambda s: wc_saved.append(json.loads(json.dumps(s))),
    )
    assert wc_out.status == 'partial'  # A marker alone does not answer the question.
    assert json.loads(wc_model.turns[3].history[0].results[0].content)['cleared'] is True
    assert wc_saved[-1]['context_receipts'][0]['trigger'] == 'l1_in_turn'

    # 2. Real AnthropicMessagesProvider wire check: messages[0] stays byte-identical to state['user'] on recovery
    #    when history is non-empty, and tail_note is appended to the last user tool_result message.
    captured_wires: list[dict] = []
    call_n = {'n': 0}

    def wire_transport(method, url, headers, body, timeout, *, diagnostics, **kwargs):
        call_n['n'] += 1
        captured_wires.append(json.loads(body))
        diagnostics.submission('response_received')
        if call_n['n'] == 1:
            return HttpResponse(200, json.dumps({
                'role': 'assistant',
                'stop_reason': 'tool_use',
                'content': [{'type': 'tool_use', 'id': 'r1', 'name': 'read_sources',
                             'input': {'reads': [{'source_id': 's_wc', 'start': 0, 'end': 100}], 'rationale': 'read'}}],
                'usage': {'input_tokens': 10, 'output_tokens': 5},
            }), {})
        if call_n['n'] == 2:
            raise TimeoutError('deadline')
        return HttpResponse(200, json.dumps({
            'role': 'assistant',
            'stop_reason': 'tool_use',
            'content': [{'type': 'tool_use', 'id': 'sub1', 'name': 'submit_research', 'input': {
                'report': 'Done.',
                'claims': [{'text': 'Done.', 'source_id': 's_wc', 'quotation': '[W_00]', 'role': 'support'}],
                'limitations': [], 'stopping_reason': 'answered',
            }}],
            'usage': {'input_tokens': 20, 'output_tokens': 10},
        }), {})

    monkeypatch.setattr('app.anthropic_messages._default_transport', wire_transport)
    provider = AnthropicMessagesProvider(
        SimpleNamespace(base_url='https://example.com', model='model', api_key='fixture', allow_private_network=False),
        ProviderPolicy(retry_base_seconds=0),
        anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=1000,
    )
    saved_wire = []
    out_to = run_research_agent(
        question='Check KV prefix?',
        sources=[src],
        model=provider,
        context=context(),
        checkpoint=lambda s: saved_wire.append(json.loads(json.dumps(s))),
    )
    assert out_to.reason == 'provider_timeout'
    cp_to = saved_wire[-1]
    out_ok = run_research_agent(
        question='Check KV prefix?',
        sources=[src],
        model=provider,
        context=context(),
        initial_state=cp_to,
    )
    assert out_ok.status == 'partial'  # The placeholder source does not answer the question.
    assert captured_wires[2]['messages'][0] == captured_wires[1]['messages'][0]
    assert captured_wires[2]['messages'][0]['content'] == cp_to['user']
    tail_blocks = captured_wires[2]['messages'][-1]['content']
    assert tail_blocks[0]['type'] == 'tool_result'
    assert tail_blocks[-1]['type'] == 'text'
    assert 'provider_timeout' in tail_blocks[-1]['text']


def test_provider_timeout_produces_dedicated_gap_code_and_unknown_settlement():
    from app.phase3 import ProviderFailure
    from app.research_runtime import GAP_CODES, provider_gap

    assert 'provider_timeout' in GAP_CODES
    assert provider_gap(ProviderFailure('provider_timeout', 'Stream deadline exceeded.', retryable=True)) == {
        'code': 'provider_timeout',
        'message': 'Stream deadline exceeded.',
    }
    assert provider_gap({'code': 'provider_timeout', 'message': 'Idle timeout.'}) == {
        'code': 'provider_timeout',
        'message': 'Idle timeout.',
    }
    assert provider_gap(ProviderFailure('provider_transport_error', 'Connection reset.', retryable=True)) == {
        'code': 'provider_error',
        'message': 'Connection reset.',
    }
    assert provider_gap(GAP_CODES['provider_no_result']) == {
        'code': 'provider_no_result',
        'message': GAP_CODES['provider_no_result'],
    }

    # Mid-stream timeout with provider_response_received=True and reported_tokens=None (Item 3.1):
    # hits the saved model_returned error dict branch, produces gap code 'provider_timeout',
    # settles with settlement='unknown' (never 0-token settled), and leaves pending=None.
    class MidStreamTimeoutModel:
        def complete(self, messages, tools, **kwargs):
            err = ProviderFailure('provider_timeout', 'Stream aborted before usage delta.', retryable=True, reported_tokens=None)
            err.provider_response_received = True
            err.timeout_kind = 'stream_idle_timeout'
            raise err

    saved: list[dict] = []
    ctx = context()
    out = run_research_agent(
        question='Summarize filing?',
        sources=[source()],
        model=MidStreamTimeoutModel(),
        context=ctx,
        checkpoint=lambda s: saved.append(json.loads(json.dumps(s))),
    )
    assert out.status == 'partial'
    assert out.reason == 'provider_timeout'
    assert out.gaps == [{'code': 'provider_timeout', 'message': 'Stream aborted before usage delta.'}]
    assert saved[-1]['pending'] is None
    assert saved[-1]['failed_attempts'][-1]['reason'] == 'provider_timeout'
    assert saved[-1]['model_receipts'][-1]['settlement'] == 'unknown'
    assert saved[-1]['events'][-1]['usage'] is None
    assert ctx.usage is None


def test_checkpointed_question_parts_survive_interruption_and_reopen_without_leaking_revoked_evidence():
    model = Model([
        turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read disclosure'}, 'a'),
        turn('record_facts', {'facts': [{'label': 'Revenue', 'value': 100, 'unit': 'USD', 'currency': 'USD',
             'period': '2025', 'source_id': 's1', 'quotation': 'Revenue was 100 USD.'}], 'rationale': 'record revenue'}, 'b'),
    ])
    original = model.complete
    def complete(messages, tools, **kwargs):
        if len(model.inputs) < 2:
            return original(messages, tools, **kwargs)
        count = len(model.inputs)
        model.inputs.append(json.loads(json.dumps(messages)))
        if count == 2:
            fact_id = json.loads(_last_tool_msg(messages)['content'])['facts'][0]['fact_id']
            return turn('checkpoint_question_part', {'question': 'Revenue', 'answer': 'The filing reports revenue of 100 USD.', 'fact_ids': [fact_id]}, 'c')
        if count == 3:
            return turn('checkpoint_question_part', {'question': 'entry timing', 'unresolved': 'A current price and entry criteria are missing.'}, 'd')
        return ModelResponse({'role': 'assistant', 'content': 'interrupted'}, (), None, 'stop')
    model.complete = complete
    snapshots = []
    question = 'Revenue and entry timing?'
    out = run_research_agent(question=question, sources=[source()], model=model, context=context(),
                             checkpoint=lambda s: snapshots.append(json.loads(json.dumps(s))))
    assert out.status == 'partial'
    assert out.question_parts[0]['answer'] == 'The filing reports revenue of 100 USD.'
    assert out.question_parts[0]['references'][0]['origin'] == 'current'
    assert out.question_parts[1]['reason'] == 'A current price and entry criteria are missing.'
    assert any(len(s.get('question_parts', [])) == 2 for s in snapshots)
    from app.research_topics import ResearchQuestionPart
    restored = [ResearchQuestionPart.model_validate(row).model_dump() for row in out.question_parts]
    assert restored[0]['references'][0]['fact_id'] == out.facts[0]['fact_id']
    from app.research_partial_answer import question_parts
    revoked = question_parts(question, snapshots[-1], set())
    assert revoked[0]['answer'] == '' and revoked[0]['references'] == []
    assert '和平' in question_parts('和平公司涉及什么风险？', {}, set())[0]['question']


def test_partial_question_enumerations_preserve_unsplit_entities_and_fill_only_uncovered_parts():
    from app.research_partial_answer import question_parts
    question = '请研究，包括风险、入场和持有？'
    empty = question_parts(question, {}, set())
    assert [p['question'] for p in empty] == ['风险', '入场', '持有']
    state = {'question_parts': [{'question': '风险', 'status': 'unresolved', 'answer': '',
                                 'reason': '缺少依据', 'references': []}]}
    assert [p['question'] for p in question_parts(question, state, set())] == ['风险', '入场', '持有']
    assert [p['question'] for p in question_parts('和平公司涉及什么风险？', {}, set())] == ['和平公司涉及什么风险']
    assert [p['question'] for p in question_parts('Revenue and entry timing?', {}, set())] == ['Revenue', 'entry timing']


def test_report_draft_retains_valid_clauses_after_rejection_and_rechecks_permissions():
    src = source()
    src.text = '\n'.join(f'Revenue was {n} USD.' for n in range(100, 125))
    src.content_sha256 = sha256(src.text.encode()).hexdigest()
    model = Model([
        turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'a'),
        turn('checkpoint_report_draft', {'clauses': [
            *[{'text': f'Revenue was {n} USD.', 'role': 'support', 'citations': [{'source_id': 's1', 'quotation': f'Revenue was {n} USD.'}]} for n in range(100, 125)],
            {'text': 'BlackRock revenue was 999 USD.', 'role': 'support', 'citations': [{'source_id': 's1', 'quotation': 'Revenue was 100 USD.'}]},
        ]}, 'b'),
        ModelResponse({'role': 'assistant', 'content': 'interrupted'}, (), None, 'stop'),
    ])
    saved = []
    out = run_research_agent(question='What was revenue?', sources=[src], model=model, context=context(),
                             checkpoint=lambda state: saved.append(json.loads(json.dumps(state))))
    feedback = json.loads(_last_tool(model, 2)['content'])
    assert len(feedback['accepted']) == 25 and len(feedback['rejected']) == 1
    # Located drafts are retained in the checkpoint, but cannot become answer
    # claims without independent semantic review.
    assert out.claims == []
    assert all('BlackRock' not in claim['text'] for claim in out.claims)
    from app.research_report_grounding import recover_draft_claims
    assert len(recover_draft_claims(saved[-1], {'s1': src}, lambda s: True)) == 25
    assert recover_draft_claims(saved[-1], {'s1': src}, lambda s: False) == []
    src.text = src.text.replace('Revenue was 100 USD.', 'Revenue was 999 USD.')
    assert len(recover_draft_claims(saved[-1], {'s1': src}, lambda s: True)) == 24


def test_mixed_facts_checkpoint_replay_preserves_valid_entries_and_exact_source_spans():
    from hashlib import sha256
    text = 'Revenue 100.00 USD\nEPS 2.5 USD per share\nRepeated 8\nRepeated 8'
    src = source().model_copy(update={'text': text, 'content_sha256': sha256(text.encode()).hexdigest()})
    eps_start = text.index('EPS')
    eps_end = text.index('\nRepeated')
    base = {'unit': 'USD', 'currency': 'USD', 'period': '2025', 'source_id': 's1'}
    revenue = {**base, 'label': 'revenue', 'value': 100, 'quotation': 'Revenue 100 USD'}
    eps = {**base, 'label': 'eps', 'value': 2.5, 'unit': 'USD/share',
           'character_start': eps_start, 'character_end': eps_end, 'content_sha256': src.content_sha256}
    invalid = {**eps, 'label': 'wrong hash', 'content_sha256': '0' * 64}
    ambiguous = {**base, 'label': 'ambiguous', 'value': 8, 'quotation': 'Repeated 8'}
    rows = [revenue, eps, invalid, ambiguous, {'label': 'malformed'}, 42]
    turns = [turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'a'),
             turn('record_facts', {'facts': rows, 'rationale': 'record'}, 'b'),
             ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')]
    snapshots = []
    model = Model(turns)
    outcome = run_research_agent(question='Q', sources=[src], model=model, context=context(),
                                 checkpoint=lambda s: snapshots.append(json.loads(json.dumps(s))))
    feedback = json.loads(_last_tool(model)['content'])
    assert len(feedback['facts']) == len(outcome.facts) == 2
    assert [item['index'] for item in feedback['failures']] == [2, 3, 4, 5]
    assert feedback['facts'][0]['quotation'] == 'Revenue 100.00 USD'
    assert feedback['facts'][1]['quotation'] == text[eps_start:eps_end]
    committed = next(s for s in snapshots if len(s.get('facts', [])) == 2 and s['history'][-1]['results'])
    assert [f['fact_id'] for f in committed['facts']] == [f['fact_id'] for f in feedback['facts']]
    model2 = Model(turns)
    outcome2 = run_research_agent(question='Q', sources=[src], model=model2, context=context())
    assert [f['fact_id'] for f in outcome2.facts] == [f['fact_id'] for f in outcome.facts]

    from decimal import Decimal
    from app.research_calculations import CalculationError, NumericFact, align_facts_scale
    fact = NumericFact(fact_id='a', value=Decimal('2.5'), unit='USD/share', currency='USD', period='2025')
    aligned, _, _, _ = align_facts_scale([fact, fact.model_copy(update={'fact_id': 'b', 'unit': 'USD per share'})])
    assert aligned == [Decimal('2.5'), Decimal('2.5')]
    with pytest.raises(CalculationError):
        align_facts_scale([fact, fact.model_copy(update={'fact_id': 'b', 'unit': 'USD millions', 'value': Decimal('3')})])


def test_report_draft_replacement_is_atomic_and_retry_does_not_duplicate():
    from app.research_tool_executor import ToolExecutionContext, _exec_checkpoint_report_draft
    from app.research_tools_registry import CheckpointReportDraftRequest
    src = source()
    ctx = ToolExecutionContext(question='Revenue?', state={'read_ranges': [{'source_id': 's1', 'start': 0, 'end': len(src.text)}]},
                               sources=[src], source_by_id={'s1': src}, context=context(), as_of=None, seen=set())
    def call(clauses):
        return _exec_checkpoint_report_draft(ctx, SimpleNamespace(req=CheckpointReportDraftRequest(clauses=clauses))).result
    original = {'text': 'Revenue was 100 USD.', 'role': 'support', 'citations': [{'source_id': 's1', 'quotation': src.text}]}
    first = call([original])
    old_id = first['accepted'][0]['claim_id']
    rejected = call([{**original, 'text': 'Revenue was 999 USD.', 'replaces_claim_id': old_id}])
    assert rejected['rejected'] and ctx.state['draft_claims'][0]['claim_id'] == old_id
    patch = {**original, 'text': 'Revenue: 100 USD.', 'replaces_claim_id': old_id}
    new_id = call([patch])['accepted'][0]['claim_id']
    assert new_id != old_id
    assert call([patch])['accepted'][0]['claim_id'] == new_id
    assert [claim['claim_id'] for claim in ctx.state['draft_claims']] == [new_id]


def test_repeated_dated_statement_facts_keep_their_id_and_distinct_measurements():
    from hashlib import sha256
    text = 'FY2024 net income was USD 40 million; adjusted net income was also USD 40 million.'
    src = source().model_copy(update={'text': text, 'content_sha256': sha256(text.encode()).hexdigest()})
    base = {'source_id': src.source_id, 'period': 'FY2024', 'value': 40, 'unit': 'USD million',
            'currency': 'USD', 'quotation': text}
    model = Model([
        turn('read_sources', {'reads': [{'source_id': src.source_id}], 'rationale': 'Inspect disclosure.'}, 'read'),
        turn('record_facts', {'facts': [{**base, 'label': 'Annual net income'}], 'rationale': 'Record annual result.'}, 'first'),
        turn('record_facts', {'facts': [{**base, 'label': 'net income'}, {**base, 'label': 'adjusted net income'}],
                              'rationale': 'Retry and add adjusted measure.'}, 'retry'),
        ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop'),
    ])
    outcome = run_research_agent(question='Check both reported income measures.', sources=[src], model=model, context=context())
    assert [f['label'] for f in outcome.facts] == ['Annual net income', 'adjusted net income']
    assert len({f['fact_id'] for f in outcome.facts}) == 2
