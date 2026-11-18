import json
from datetime import datetime, timezone

from app.model_protocol import ModelResponse, ModelToolCall, ModelUsage
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext
from app.research_runtime import run_research_agent
from app.research_sources import ResearchSource, SourcePermissions


def source(url=None, allowed=True):
    return ResearchSource(source_id='s1', owner_id='owner', title='Disclosure', text='Revenue was 100 USD.',
                          url=url, kind='user_supplied', obtained_at=datetime.now(timezone.utc), content_sha256='hash',
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
    def complete(self, messages, tools, **kwargs):
        self.inputs.append(json.loads(json.dumps(messages)))
        return next(self.turns)


def test_read_feedback_and_submission_uses_call_association_and_server_ids():
    m = Model([turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'financial disclosure'}, 'a'),
               turn('submit_research', {'report': 'Revenue was 100 USD.', 'claims': [{'text': 'Revenue was 100 USD.', 'source_id': 's1',
                    'quotation': 'Revenue was 100 USD.', 'character_start': 0, 'character_end': 20}], 'limitations': [], 'stopping_reason': 'answered'}, 'b')])
    saved = []
    result = run_research_agent(question='Revenue?', sources=[source()], model=m, context=context(),
                                checkpoint=lambda s: saved.append(json.loads(json.dumps(s))))
    assert result.status == 'completed'
    assert result.claims[0]['claim_id'].startswith('claim-')
    assert m.inputs[1][-1]['tool_call_id'] == 'a'
    assert 'Revenue was 100 USD.' in m.inputs[1][-1]['content']
    assert any(s['pending'] and s['pending']['kind'] == 'model' for s in saved)
    assert saved[-1]['submission']['report'] == result.report


def test_fred_and_missing_permission_are_denied_at_read():
    for item in (source('https://fred.stlouisfed.org/series/GDP'), source(allowed=False)):
        m = Model([turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'a'),
                   ModelResponse({'role': 'assistant', 'content': 'Cannot proceed.'}, (), None, 'stop')])
        result = run_research_agent(question='Question', sources=[item], model=m, context=context())
        assert result.status == 'partial'
        assert 'denied_or_unavailable' in m.inputs[1][-1]['content']
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
    src.content_sha256 = 'new-hash'
    m = Model([turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'compare'}, 'a'),
        turn('record_facts', {'facts': [
            {'label': 'revenue', 'value': 100, 'unit': 'USD', 'currency': 'USD', 'period': '2025', 'source_id': 's1',
             'quotation': 'Revenue was 100 USD.', 'character_start': 0, 'character_end': 20},
            {'label': 'revenue', 'value': 80, 'unit': 'USD', 'currency': 'USD', 'period': '2024', 'source_id': 's1',
             'quotation': 'Revenue was 80 USD.', 'character_start': 21, 'character_end': 40}], 'rationale': 'inputs'}, 'b')])
    original = m.complete
    # Record the subsequent calls as well as the two scripted turns.
    def wrapped(messages, tools, **kwargs):
        if len(m.inputs) < 2:
            return original(messages, tools, **kwargs)
        count = len(m.inputs)
        m.inputs.append(json.loads(json.dumps(messages)))
        if count == 2:
            facts = json.loads(messages[-1]['content'])['facts']
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
    assert m.inputs[-1][-1]['tool_call_id'] == 'a'


def test_tool_batch_cursor_resumes_without_model_replay():
    call = turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'a')
    saved = {'user': 'Q', 'history': [{'assistant': call.message, 'results': []}], 'events': [], 'read_ranges': [], 'facts': [], 'calculations': [],
             'pending': {'kind': 'tool_batch', 'call_id': 'model1', 'cursor': 0,
                         'calls': [{'id': c.id, 'name': c.name, 'arguments': json.loads(c.arguments), 'raw_arguments': c.arguments} for c in call.tool_calls]}}
    m = Model([ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
    result = run_research_agent(question='Q', sources=[source()], model=m, context=context(), initial_state=saved)
    assert result.reason == 'no_submission'
    assert len(m.inputs) == 1
    assert m.inputs[0][-1]['tool_call_id'] == 'a'


def test_same_action_with_changed_rationale_is_no_progress():
    m = Model([turn('discover_sources', {'query': 'Q', 'rationale': 'first'}, 'a'),
               turn('discover_sources', {'query': 'Q', 'rationale': 'different wording'}, 'b')])
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
    submit = {'report': 'Revenue.', 'claims': [{'text': 'Revenue', 'source_id': 's1', 'quotation': 'Revenue was 100 USD.'}],
              'limitations': [], 'stopping_reason': 'answered'}
    m = Model([turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'a'),
               turn('submit_research', submit, 'b')])
    result = run_research_agent(question='Q', sources=[source()], model=m, context=context())
    assert result.claims[0]['character_start'] == 0
    assert result.claims[0]['character_end'] == 20
    repeated = source()
    repeated.text = repeated.text + ' ' + repeated.text
    m = Model([turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'a'),
               turn('submit_research', submit, 'b'), ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
    result = run_research_agent(question='Q', sources=[repeated], model=m, context=context())
    assert result.status == 'partial'
    assert 'candidate offsets' in m.inputs[-1][-1]['content']


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
    m = Model([turn('discover_sources', {'query': 'report', 'rationale': 'find'}, 'a'),
               turn('acquire_sources', {'urls': [lead['url']], 'rationale': 'read'}, 'b'),
               ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
    result = run_research_agent(question='Q', sources=[], model=m, context=context(), discover=lambda _: [lead],
        acquire=lambda *_: (None, {'status': 'failed', 'notes': ['Unavailable'],
                                   'outcomes': [{'status': 'failed', 'external_state': 'returned'}]}))
    assert result.reason == 'no_submission'
    assert 'Unavailable' in m.inputs[-1][-1]['content']


def test_repeated_facts_reuse_id_and_failed_batch_is_atomic():
    fact = {'label': 'revenue', 'value': 100, 'unit': 'USD', 'currency': 'USD', 'period': '2025',
            'source_id': 's1', 'quotation': 'Revenue was 100 USD.'}
    invalid = {**fact, 'value': 999}
    m = Model([turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'read'}, 'a'),
               turn('record_facts', {'facts': [fact, invalid], 'rationale': 'bad batch'}, 'b'),
               turn('record_facts', {'facts': [fact, fact], 'rationale': 'first'}, 'c'),
               turn('record_facts', {'facts': [fact, fact], 'rationale': 'again'}, 'd'),
               turn('record_facts', {'facts': [fact, fact], 'rationale': 'different'}, 'e')])
    result = run_research_agent(question='Q', sources=[source()], model=m, context=context())
    assert result.reason == 'no_progress'
    assert len(result.facts) == 1
    first = json.loads(m.inputs[3][-1]['content'])['facts']
    second = json.loads(m.inputs[4][-1]['content'])['facts']
    assert first[0]['fact_id'] == first[1]['fact_id'] == second[0]['fact_id'] == result.facts[0]['fact_id']


def test_malformed_arguments_first_and_later_do_not_reuse_rationale():
    malformed = ModelToolCall('bad', 'read_sources', '{bad')
    bad_turn = ModelResponse({'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'bad', 'type': 'function',
        'function': {'name': 'read_sources', 'arguments': '{bad'}}]}, (malformed,), ModelUsage(1, 1, 2), 'tool_calls')
    for turns in ([bad_turn], [turn('read_sources', {'reads': [{'source_id': 's1'}], 'rationale': 'private previous rationale'}, 'ok'), bad_turn]):
        m = Model([*turns, ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
        result = run_research_agent(question='Q', sources=[source()], model=m, context=context())
        assert result.status == 'partial'
        assert json.loads(m.inputs[-1][-1]['content'])['status'] == 'invalid'
        event = next(e for e in result.events if e.get('tool_call_id') == 'bad')
        assert event['rationale'] is None


def test_fred_search_leads_are_removed_before_model_feedback_and_checkpoint():
    leads = [{'url': 'https://fred.stlouisfed.org/series/GDP', 'snippet': 'FRED SECRET CONTENT'},
             {'url': 'https://alfred.stlouisfed.org/series/GDP', 'snippet': 'ALFRED SECRET CONTENT'},
             {'url': 'https://api.stlouisfed.org/fred/series', 'snippet': 'API SECRET CONTENT'},
             {'url': 'https://example.com/report', 'title': 'Other'}]
    m = Model([turn('discover_sources', {'query': 'economy', 'rationale': 'discover'}, 'a'),
               ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
    saved = []
    outcome = run_research_agent(question='Q', sources=[], model=m, context=context(), discover=lambda _: leads,
                                 checkpoint=lambda s: saved.append(json.loads(json.dumps(s))))
    event = next(e for e in outcome.events if e.get('name') == 'discover_sources')
    assert event['denied_count'] == 3
    assert event['denied_reasons']['source_not_authorized'] == 3
    feedback = json.loads(m.inputs[-1][-1]['content'])
    assert feedback['denied_count'] == 3
    assert len(feedback['leads']) == 1
    assert 'SECRET CONTENT' not in json.dumps(saved)


def test_repeated_invalid_calls_stop_without_hidden_retry_limit():
    malformed = ModelToolCall('bad', 'read_sources', '{bad')
    bad_turn = ModelResponse({'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'bad', 'type': 'function',
        'function': {'name': 'read_sources', 'arguments': '{bad'}}]}, (malformed,), ModelUsage(1, 1, 2), 'tool_calls')
    for turns in ([bad_turn, bad_turn],
                  [turn('read_sources', {'reads': [], 'rationale': 'first'}, 'a'),
                   turn('read_sources', {'reads': [], 'rationale': 'changed'}, 'b')]):
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
            return ModelResponse({'opaque': ['private']}, (ModelToolCall('a', 'discover_sources',
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


def test_messages_preflight_and_injected_transport_diagnostics():
    from types import SimpleNamespace
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
