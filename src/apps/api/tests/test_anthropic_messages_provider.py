import json
from threading import Event
from types import SimpleNamespace
import pytest
from app.anthropic_messages import AnthropicMessagesProvider
from app.model_protocol import (ModelAssistantContinuation, ModelToolDefinition, ModelToolResult,
                                ModelTurnExchange, ModelTurnInput)
from app.openai_compatible import HttpResponse
from app.phase3 import ProviderFailure


def fixture(response, *, auth='bearer'):
    requests = []
    def transport(method, url, headers, body, timeout):
        requests.append((method, url, headers, json.loads(body), timeout))
        if isinstance(response, Exception): raise response
        return response
    settings = SimpleNamespace(base_url='https://example.com/api/anthropic', model='configured-model', api_key='secret', allow_private_network=False)
    policy = SimpleNamespace(timeout_seconds=10, max_input_tokens=None)
    return AnthropicMessagesProvider(settings, policy, anthropic_version='2023-06-01', auth_scheme=auth, max_tokens=100, transport=transport), requests


def turn(history=()):
    return ModelTurnInput('system instruction', 'research question', (ModelToolDefinition('lookup', 'Lookup', {'type':'object'}),), history)


def response(blocks, reason='tool_use', usage=None):
    return HttpResponse(200, json.dumps({'role':'assistant', 'content':blocks, 'stop_reason':reason,
        'usage': usage or {'input_tokens':10, 'output_tokens':2, 'cache_creation_input_tokens':3, 'cache_read_input_tokens':4}}))


def test_multi_round_exact_payload_and_cache_usage():
    blocks = [{'type':'thinking', 'thinking':'private', 'signature':'sig'}, {'type':'tool_use','id':'raw-1','name':'lookup','input':{'q':'A'}}, {'type':'tool_use','id':'raw-2','name':'lookup','input':{'q':'B'}}]
    provider, requests = fixture(response(blocks))
    result = provider.complete_turn(turn())
    assert result.usage.total_tokens == 19
    assert [x.id for x in result.tool_calls] == ['raw-1', 'raw-2']
    exchange = ModelTurnExchange(ModelAssistantContinuation(result.message), (ModelToolResult('raw-1', 'first'), ModelToolResult('raw-2', 'failed', True)))
    provider.complete_turn(turn((exchange,)))
    method, url, headers, wire, _ = requests[-1]
    assert (method, url) == ('POST', 'https://example.com/api/anthropic/v1/messages')
    assert headers['Authorization'] == 'Bearer secret'
    assert wire == {'model':'configured-model','system':'system instruction','max_tokens':100,
      'tools':[{'name':'lookup','description':'Lookup','input_schema':{'type':'object'}}],
      'messages':[{'role':'user','content':'research question'}, {'role':'assistant','content':blocks},
       {'role':'user','content':[{'type':'tool_result','tool_use_id':'raw-1','content':'first','is_error':False}, {'type':'tool_result','tool_use_id':'raw-2','content':'failed','is_error':True}]}]}


@pytest.mark.parametrize('blocks,reason,code', [([{'type':'text','text':'partial'}], 'max_tokens','provider_output_truncated'), ([{'type':'tool_use','id':'x','name':'lookup','input':'bad'}], 'tool_use','invalid_provider_output'), ([{'type':'tool_use','id':'x','name':'lookup','input':{}}], 'end_turn','invalid_provider_output')])
def test_invalid_or_truncated_never_return_tool_calls(blocks, reason, code):
    provider, _ = fixture(response(blocks, reason))
    with pytest.raises(ProviderFailure) as caught: provider.complete_turn(turn())
    assert caught.value.code == code
    assert caught.value.reported_tokens == 19


def test_unknown_submission_is_never_retried():
    provider, requests = fixture(TimeoutError())
    with pytest.raises(ProviderFailure) as caught: provider.complete_turn(turn())
    assert caught.value.code == 'provider_timeout'
    assert len(requests) == 1


def test_cancel_before_submission():
    provider, requests = fixture(response([{'type':'text','text':'ok'}], 'end_turn'))
    event = Event(); event.set()
    with pytest.raises(ProviderFailure): provider.complete_turn(turn(), cancel_event=event)
    assert requests == []


def test_http_diagnostic_does_not_disclose_body():
    provider, _ = fixture(HttpResponse(400, json.dumps({'error':{'type':'invalid_request_error','param':'messages','message':'secret text'}}), {'request-id':'req-123'}), auth='x-api-key')
    observations = []
    with pytest.raises(ProviderFailure): provider.complete_turn(turn(), diagnostic_callback=observations.append)
    assert observations[0]['provider_error']['status_code'] == 400
    assert observations[0]['provider_error']['request_id'] == 'req-123'
    assert 'secret text' not in repr(observations)


def test_http_200_max_tokens_emits_safe_diagnostics_before_raising():
    from app.research_diagnostics import get_finish_reason
    secret_turn = ModelTurnInput(
        'SECRET_SYSTEM_PROMPT',
        'SECRET_USER_QUESTION',
        (ModelToolDefinition('lookup', 'Lookup', {'type': 'object'}),),
        (),
    )
    provider, _ = fixture(
        HttpResponse(
            200,
            json.dumps({
                'role': 'assistant',
                'content': [{'type': 'text', 'text': 'SECRET_PARTIAL_OUTPUT'}],
                'stop_reason': 'max_tokens',
                'usage': {
                    'input_tokens': 120,
                    'output_tokens': 100,
                    'cache_creation_input_tokens': 10,
                    'cache_read_input_tokens': 20,
                    'secret_note': 'SECRET_USAGE',
                },
            }),
            {'request-id': 'req-trunc-json'},
        ),
        auth='x-api-key',
    )
    observations = []
    with pytest.raises(ProviderFailure) as caught:
        provider.complete_turn(secret_turn, diagnostic_callback=observations.append)
    assert caught.value.code == 'provider_output_truncated'
    assert caught.value.provider_response_received is True
    assert caught.value.reported_tokens == 250
    assert get_finish_reason() == 'provider_length'
    assert len(observations) == 1
    obs = observations[0]
    assert obs['stop_reason'] == 'max_tokens'
    assert obs['finish_reason'] == 'provider_length'
    assert obs['max_tokens'] == 100
    assert obs['wire_bytes'] > 0
    assert obs['request_metadata']['parameters']['max_tokens'] == 100
    assert obs['request_metadata']['wire_bytes'] == obs['wire_bytes']
    assert obs['usage'] == {
        'input_tokens': 120,
        'output_tokens': 100,
        'total_tokens': 250,
        'cache_creation_input_tokens': 10,
        'cache_read_input_tokens': 20,
    }
    assert obs['provider_error']['status_code'] == 200
    assert obs['provider_error']['request_id'] == 'req-trunc-json'
    dumped = repr(observations)
    for forbidden in ('SECRET_SYSTEM_PROMPT', 'SECRET_USER_QUESTION', 'SECRET_PARTIAL_OUTPUT', 'SECRET_USAGE', 'secret'):
        assert forbidden not in dumped


def test_sse_max_tokens_emits_safe_diagnostics_before_raising(monkeypatch):
    from app.research_diagnostics import get_finish_reason
    monkeypatch.setenv('FORESIGHTNINE_LLM_ENDPOINT_VERSION', 'v1')
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': 'https://example.com/api/anthropic/v1/messages',
        'model': 'configured-model',
        'version': 'v1',
        'mode': 'json_object',
        'native_tools': True,
        'stream': True,
    }))
    frames = [
        ('message_start', {'type': 'message_start', 'message': {'role': 'assistant', 'content': [], 'usage': {'input_tokens': 80, 'output_tokens': 1, 'cache_read_input_tokens': 40}}}),
        ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''}}),
        ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': 'SECRET_THINKING'}}),
        ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'signature_delta', 'signature': 'SECRET_SIG'}}),
        ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
        ('content_block_start', {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'text', 'text': ''}}),
        ('content_block_delta', {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': 'SECRET_STREAM_TEXT'}}),
        ('content_block_stop', {'type': 'content_block_stop', 'index': 1}),
        ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'max_tokens'}, 'usage': {'output_tokens': 100}}),
        ('message_stop', {'type': 'message_stop'}),
    ]

    def mock_stream(method, url, headers, body, timeout, on_frame):
        for ev, payload in frames:
            on_frame(ev, json.dumps(payload))
        return None

    settings = SimpleNamespace(base_url='https://example.com/api/anthropic', model='configured-model', api_key='sk-secret', allow_private_network=False)
    policy = SimpleNamespace(timeout_seconds=10, max_input_tokens=None)
    provider = AnthropicMessagesProvider(settings, policy, anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=100, stream_transport=mock_stream)
    observations = []
    stream_events = []
    secret_turn = ModelTurnInput('SECRET_SYSTEM_PROMPT', 'SECRET_USER_QUESTION', (), ())
    with pytest.raises(ProviderFailure) as caught:
        provider.complete_turn(secret_turn, on_stream_event=stream_events.append, diagnostic_callback=observations.append)
    assert caught.value.code == 'provider_output_truncated'
    assert caught.value.provider_response_received is True
    assert caught.value.reported_tokens == 220
    assert get_finish_reason() == 'provider_length'
    assert len(observations) == 1
    obs = observations[0]
    assert obs['stop_reason'] == 'max_tokens'
    assert obs['finish_reason'] == 'provider_length'
    assert obs['max_tokens'] == 100
    assert obs['wire_bytes'] > 0
    assert obs['request_metadata']['stream'] is True
    assert obs['request_metadata']['wire_bytes'] == obs['wire_bytes']
    assert obs['usage'] == {
        'input_tokens': 80,
        'output_tokens': 100,
        'total_tokens': 220,
        'cache_read_input_tokens': 40,
    }
    dumped = repr(observations)
    for forbidden in ('SECRET_SYSTEM_PROMPT', 'SECRET_USER_QUESTION', 'SECRET_THINKING', 'SECRET_SIG', 'SECRET_STREAM_TEXT', 'sk-secret'):
        assert forbidden not in dumped


def test_failed_build_response_and_unrecognized_stop_reason_are_bounded():
    provider, _ = fixture(HttpResponse(200, json.dumps({
        'role': 'assistant',
        'content': [],
        'stop_reason': 'SECRET_STOP_REASON',
        'usage': {'input_tokens': 11, 'output_tokens': 22},
    })))
    observations = []
    with pytest.raises(ProviderFailure) as caught:
        provider.complete_turn(turn(), diagnostic_callback=observations.append)
    assert caught.value.code == 'invalid_provider_output'
    assert len(observations) == 1
    assert observations[0]['stop_reason'] == 'unrecognized'
    assert observations[0]['finish_reason'] == 'unknown'
    assert observations[0]['usage'] == {'input_tokens': 11, 'output_tokens': 22, 'total_tokens': 33}
    assert 'SECRET_STOP_REASON' not in repr(observations)


def test_research_execution_context_captures_max_tokens_diagnostics_for_json_and_sse(monkeypatch):
    from app.research_budget import ResearchExecutionBudget
    from app.research_coordinator import ResearchExecutionContext

    def run_case(streaming: bool):
        if streaming:
            monkeypatch.setenv('FORESIGHTNINE_LLM_ENDPOINT_VERSION', 'v1')
            monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
                'endpoint': 'https://example.com/api/anthropic/v1/messages',
                'model': 'configured-model',
                'version': 'v1',
                'mode': 'json_object',
                'native_tools': True,
                'stream': True,
            }))
            def mock_stream(method, url, headers, body, timeout, on_frame):
                on_frame('message_start', json.dumps({'type': 'message_start', 'message': {'role': 'assistant', 'content': [], 'usage': {'input_tokens': 30, 'output_tokens': 1}}}))
                on_frame('content_block_start', json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': 'SECRET_PART'}}))
                on_frame('content_block_stop', json.dumps({'type': 'content_block_stop', 'index': 0}))
                on_frame('message_delta', json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'max_tokens'}, 'usage': {'output_tokens': 100}}))
                on_frame('message_stop', json.dumps({'type': 'message_stop'}))
                return None
            provider = AnthropicMessagesProvider(
                SimpleNamespace(base_url='https://example.com/api/anthropic', model='configured-model', api_key='secret', allow_private_network=False),
                SimpleNamespace(timeout_seconds=10, max_input_tokens=None, max_output_tokens=100),
                anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=100, stream_transport=mock_stream,
            )
        else:
            monkeypatch.delenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', raising=False)
            provider, _ = fixture(response([{'type': 'text', 'text': 'SECRET_PART'}], reason='max_tokens', usage={'input_tokens': 30, 'output_tokens': 100}))
            provider.policy = SimpleNamespace(timeout_seconds=10, max_input_tokens=None, max_output_tokens=100)

        ctx = ResearchExecutionContext(
            ResearchExecutionBudget(1, 0, None, 100), False,
            lambda: None, lambda: None, lambda *_: None, lambda *_: None, lambda: None, now=lambda: 0,
        )
        res, out = ctx.prepare_model_call(provider, 'user question', 'system prompt')
        before = ctx.start_model('call-trunc', res)
        kwargs = {'max_output_tokens': out}
        if streaming:
            kwargs['on_stream_event'] = lambda _ev: None
        with pytest.raises(ProviderFailure) as caught:
            ctx.invoke_model('call-trunc', provider.complete_turn, turn(), **kwargs)
        ctx.finish_model('call-trunc', caught.value.reported_tokens, before)
        diag = ctx.private_diagnostics['call-trunc']
        assert diag['finish'] == 'provider_length'
        assert diag['stop_reason'] == 'max_tokens'
        assert diag['usage'] == {'input_tokens': 30, 'output_tokens': 100, 'total_tokens': 130}
        assert diag['context']['wire_bytes'] > 0
        assert diag['context']['parameters']['max_tokens'] == 100
        assert diag['budget_estimate']['estimated_input_tokens_heuristic'] > 0
        assert 'SECRET_PART' not in repr(diag)

    run_case(False)
    run_case(True)


def test_dual_input_estimation_and_capability_output_cap(monkeypatch):
    long_turn = ModelTurnInput('system prompt', 'Revenue grew steadily. ' * 400, (), ())
    sent = []
    settings = SimpleNamespace(base_url='https://example.com/api/anthropic', model='arbitrary-model-id', api_key='secret', allow_private_network=False)
    policy = SimpleNamespace(timeout_seconds=10, max_input_tokens=None, max_output_tokens=16384)
    provider = AnthropicMessagesProvider(
        settings, policy, anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=16384,
        transport=lambda m, u, h, b, t: (sent.append(json.loads(b)), response([{'type': 'text', 'text': 'ok'}], 'end_turn'))[1],
    )
    dual = provider.estimate_turn_input_dual(long_turn)
    assert provider.estimate_turn_input(long_turn) == dual.utf8_byte_upper_bound
    assert dual.utf8_byte_upper_bound == dual.char_count + 64
    assert dual.heuristic_tokens == (dual.char_count + 2) // 3
    assert dual.heuristic_tokens * 3 >= dual.char_count

    monkeypatch.setenv('FORESIGHTNINE_LLM_ENDPOINT_VERSION', 'v1')
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': 'https://example.com/api/anthropic/v1/messages',
        'model': 'arbitrary-model-id',
        'version': 'v1',
        'mode': 'json_object',
        'output_tokens': 8192,
    }))
    provider.complete_turn(long_turn)
    assert sent[-1]['max_tokens'] == 8192
