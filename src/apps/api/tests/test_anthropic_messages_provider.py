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


@pytest.mark.parametrize(('status', 'code'), [(400, 'provider_request_rejected'), (429, 'provider_rate_limited')])
def test_http_diagnostic_does_not_disclose_body(status, code):
    provider, requests = fixture(HttpResponse(status, json.dumps({'error':{'type':'invalid_request_error','param':'messages','message':'secret text'}}), {'request-id':'req-123'}), auth='x-api-key')
    observations = []
    with pytest.raises(ProviderFailure) as caught:
        provider.complete_turn(turn(), diagnostic_callback=observations.append)
    assert caught.value.code == code and caught.value.provider_response_received and len(requests) == 1
    assert observations[0]['provider_error']['status_code'] == status
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


def test_streaming_timeout_kinds_preserved_on_provider_failure_and_diagnostics(monkeypatch):
    import threading
    import time
    from app import openai_compatible
    from app.research_budget import ResearchExecutionBudget
    from app.research_coordinator import ResearchExecutionContext

    def run_stream_scenario(scenario: str):
        monkeypatch.setattr(openai_compatible, '_validated_addresses', lambda *a, **k: ('1.1.1.1',))
        monkeypatch.delenv('FORESIGHTNINE_LLM_CONNECT_TIMEOUT_SECONDS', raising=False)
        monkeypatch.delenv('FORESIGHTNINE_LLM_RESPONSE_TIMEOUT_SECONDS', raising=False)
        monkeypatch.delenv('FORESIGHTNINE_LLM_STREAM_IDLE_TIMEOUT_SECONDS', raising=False)
        release = threading.Event()

        class FakeSock:
            def settimeout(self, value): pass
            def shutdown(self, how): release.set()

        class FakeResponse:
            status = 200
            headers = {'Content-Type': 'text/event-stream'}
            length = None
            def __init__(self): self.calls = 0
            def read1(self, n):
                self.calls += 1
                if self.calls == 1:
                    return (
                        b'event: message_start\n'
                        b'data: {"type":"message_start","message":{"role":"assistant","content":[],"usage":{"input_tokens":0,"output_tokens":0}}}\n\n'
                        b'event: content_block_start\n'
                        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
                        b'event: content_block_delta\n'
                        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"SECRET_PARTIAL"}}\n\n'
                    )
                if scenario == 'stream_deadline_timeout':
                    while not release.wait(0.01):
                        return b': keepalive\n\n'
                else:
                    release.wait(2.0)
                raise OSError('closed')
            def read(self, n): return b''

        class FakeConn:
            def __init__(self, host, port, address, timeout):
                self.sock = FakeSock()
            def connect(self):
                if scenario == 'connect_timeout':
                    raise TimeoutError(110, 'SECRET_CONNECT_DETAIL')
            def request(self, *a, **k): pass
            def getresponse(self):
                if scenario == 'header_timeout':
                    release.wait(2.0)
                    raise OSError('closed')
                return FakeResponse()
            def close(self): release.set()

        monkeypatch.setattr(openai_compatible, '_PinnedHTTPSConnection', FakeConn)

        timeout_s = 5.0
        if scenario == 'header_timeout':
            monkeypatch.setenv('FORESIGHTNINE_LLM_RESPONSE_TIMEOUT_SECONDS', '0.05')
        elif scenario == 'stream_idle_timeout':
            monkeypatch.setenv('FORESIGHTNINE_LLM_STREAM_IDLE_TIMEOUT_SECONDS', '0.05')
        elif scenario == 'stream_deadline_timeout':
            monkeypatch.setenv('FORESIGHTNINE_LLM_STREAM_IDLE_TIMEOUT_SECONDS', '5.0')
            timeout_s = 0.08

        settings = SimpleNamespace(
            base_url='https://example.com/api/anthropic',
            model='configured-model',
            api_key='sk-secret-key',
            allow_private_network=False,
        )
        policy = SimpleNamespace(timeout_seconds=timeout_s, max_input_tokens=None, max_output_tokens=16384)
        provider = AnthropicMessagesProvider(
            settings, policy, anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=16384, stream_enabled=True,
        )
        ctx = ResearchExecutionContext(
            ResearchExecutionBudget(1, 0, None, time.monotonic() + 100.0), False,
            lambda: None, lambda: None, lambda *_: None, lambda *_: None, lambda: None, now=time.monotonic,
        )
        res, out = ctx.prepare_model_call(provider, 'SECRET_USER_Q', 'SECRET_SYS_P')
        before = ctx.start_model('call-' + scenario, res)
        stream_events = []
        secret_turn = ModelTurnInput('SECRET_SYS_P', 'SECRET_USER_Q', (), ())
        try:
            with pytest.raises(ProviderFailure) as caught:
                ctx.invoke_model(
                    'call-' + scenario,
                    provider.complete_turn,
                    secret_turn,
                    max_output_tokens=out,
                    on_stream_event=stream_events.append,
                )
        finally:
            release.set()
        err = caught.value
        assert err.code == 'provider_timeout'
        assert err.timeout_kind == scenario
        assert type(err.elapsed_ms) is int and err.elapsed_ms >= 0
        assert type(err.wire_bytes) is int and err.wire_bytes > 0
        expected_streamed = len('SECRET_PARTIAL'.encode('utf-8')) if scenario in ('stream_idle_timeout', 'stream_deadline_timeout') else 0
        assert err.streamed_bytes == expected_streamed
        diag = ctx.private_diagnostics['call-' + scenario]
        assert diag['transport']['timeout_kind'] == scenario
        assert diag['context']['wire_bytes'] == err.wire_bytes
        if scenario in ('stream_idle_timeout', 'stream_deadline_timeout'):
            assert err.provider_response_received is True
            assert diag['transport']['submission_state'] == 'response_received'
        else:
            assert err.provider_response_received is False
        dumped = repr(diag)
        for forbidden in ('SECRET_PARTIAL', 'SECRET_USER_Q', 'SECRET_SYS_P', 'SECRET_CONNECT_DETAIL', 'sk-secret-key'):
            assert forbidden not in dumped

    for name in ('connect_timeout', 'header_timeout', 'stream_idle_timeout', 'stream_deadline_timeout'):
        run_stream_scenario(name)


def test_non_streaming_timeout_kinds_preserved_on_provider_failure_and_diagnostics(monkeypatch):
    import threading
    from app import openai_compatible

    def run_case(scenario: str):
        monkeypatch.setattr(openai_compatible, '_validated_addresses', lambda *a, **k: ('1.1.1.1',))
        monkeypatch.delenv('FORESIGHTNINE_LLM_CONNECT_TIMEOUT_SECONDS', raising=False)
        monkeypatch.delenv('FORESIGHTNINE_LLM_RESPONSE_TIMEOUT_SECONDS', raising=False)
        release = threading.Event()

        class FakeSock:
            def settimeout(self, value): pass
            def shutdown(self, how): release.set()

        class FakeResponse:
            status = 200
            headers = {'Content-Type': 'application/json'}
            def read(self, n):
                if scenario == 'stream_deadline_timeout':
                    release.wait(2.0)
                    raise OSError('closed')
                return b'{}'

        class FakeConn:
            def __init__(self, host, port, address, timeout):
                self.sock = FakeSock()
            def connect(self):
                if scenario == 'connect_timeout':
                    raise TimeoutError(110, 'SECRET_CONNECT')
            def request(self, *a, **k): pass
            def getresponse(self):
                if scenario == 'header_timeout':
                    release.wait(2.0)
                    raise OSError('closed')
                return FakeResponse()
            def close(self): release.set()

        monkeypatch.setattr(openai_compatible, '_PinnedHTTPSConnection', FakeConn)
        timeout_s = 5.0
        if scenario == 'header_timeout':
            monkeypatch.setenv('FORESIGHTNINE_LLM_RESPONSE_TIMEOUT_SECONDS', '0.05')
        elif scenario == 'stream_deadline_timeout':
            timeout_s = 0.06

        settings = SimpleNamespace(
            base_url='https://example.com/api/anthropic',
            model='configured-model',
            api_key='sk-secret',
            allow_private_network=False,
        )
        policy = SimpleNamespace(timeout_seconds=timeout_s, max_input_tokens=None)
        provider = AnthropicMessagesProvider(
            settings, policy, anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=100, stream_enabled=False,
        )
        observations = []
        try:
            with pytest.raises(ProviderFailure) as caught:
                provider.complete_turn(turn(), diagnostic_callback=observations.append)
        finally:
            release.set()
        err = caught.value
        assert err.code == 'provider_timeout'
        assert err.timeout_kind == scenario
        assert type(err.elapsed_ms) is int and err.elapsed_ms >= 0
        assert type(err.wire_bytes) is int and err.wire_bytes > 0
        assert err.streamed_bytes is None
        assert observations[0]['timeout_kind'] == scenario

    for name in ('connect_timeout', 'header_timeout', 'stream_deadline_timeout'):
        run_case(name)


def test_timeout_output_cap_mismatch_warning_is_non_blocking(caplog):
    import logging

    settings = SimpleNamespace(
        base_url='https://example.com/api/anthropic',
        model='configured-model',
        api_key='sk-secret-do-not-log',
        allow_private_network=False,
    )
    ok_resp = response([{'type': 'text', 'text': 'ok'}], 'end_turn')

    with caplog.at_level(logging.WARNING, logger='app.anthropic_messages'):
        caplog.clear()
        provider = AnthropicMessagesProvider(
            settings,
            SimpleNamespace(timeout_seconds=120, max_input_tokens=None),
            anthropic_version='2023-06-01',
            auth_scheme='x-api-key',
            max_tokens=16384,
            transport=lambda *a: ok_resp,
        )
        result = provider.complete_turn(turn())
        assert result.message['content'] == [{'type': 'text', 'text': 'ok'}]
        warnings = [rec.message for rec in caplog.records if 'llm_timeout_output_cap_mismatch' in rec.message]
        assert len(warnings) >= 1
        assert 'max_output_tokens=16384' in warnings[0]
        assert 'timeout_seconds=120.0' in warnings[0]
        assert 'sk-secret-do-not-log' not in warnings[0]
        assert 'configured-model' not in warnings[0]

        caplog.clear()
        aligned = AnthropicMessagesProvider(
            settings,
            SimpleNamespace(timeout_seconds=300, max_input_tokens=None),
            anthropic_version='2023-06-01',
            auth_scheme='x-api-key',
            max_tokens=16384,
            transport=lambda *a: ok_resp,
        )
        aligned.complete_turn(turn())
        assert not [rec for rec in caplog.records if 'llm_timeout_output_cap_mismatch' in rec.message]


def test_tail_note_placement_preserves_prefix_and_alternation(monkeypatch):
    from app.chat_research_adapter import ChatResearchAdapter

    monkeypatch.delenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', raising=False)
    sent = []
    settings = SimpleNamespace(base_url='https://example.com/api/anthropic', model='configured-model', api_key='secret', allow_private_network=False)
    policy = SimpleNamespace(timeout_seconds=300, max_input_tokens=None)
    provider = AnthropicMessagesProvider(
        settings, policy, anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=100,
        transport=lambda m, u, h, b, t: (sent.append(json.loads(b)), response([{'type': 'text', 'text': 'ok'}], 'end_turn'))[1],
    )

    # Empty/whitespace tail_note keeps wire payload identical.
    provider.complete_turn(ModelTurnInput('system instruction', 'research question', (), (), tail_note='   '))
    assert sent[-1]['messages'] == [{'role': 'user', 'content': 'research question'}]

    # No history + non-empty tail_note attaches cleanly to messages[0].
    no_hist_turn = ModelTurnInput('system instruction', 'research question', (), (), tail_note='Keep reasoning concise.')
    provider.complete_turn(no_hist_turn)
    assert sent[-1]['messages'] == [{
        'role': 'user',
        'content': [
            {'type': 'text', 'text': 'research question'},
            {'type': 'text', 'text': 'Keep reasoning concise.'},
        ],
    }]
    assert no_hist_turn.user == 'research question'

    # Non-empty history + tail_note keeps messages[0] byte-identical and appends to last user tool_result turn.
    exchange = ModelTurnExchange(
        ModelAssistantContinuation({'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call-1', 'name': 'lookup', 'input': {'q': 'A'}}]}),
        (ModelToolResult('call-1', 'first result'),),
    )
    with_hist_turn = ModelTurnInput(
        'system instruction',
        'research question',
        (ModelToolDefinition('lookup', 'Lookup', {'type': 'object'}),),
        (exchange,),
        tail_note='Prior call timed out; batch record_facts first.',
    )
    provider.complete_turn(with_hist_turn)
    wire_msgs = sent[-1]['messages']
    assert [m['role'] for m in wire_msgs] == ['user', 'assistant', 'user']
    assert wire_msgs[0] == {'role': 'user', 'content': 'research question'}
    assert wire_msgs[1] == exchange.assistant.envelope
    assert wire_msgs[2] == {
        'role': 'user',
        'content': [
            {'type': 'tool_result', 'tool_use_id': 'call-1', 'content': 'first result', 'is_error': False},
            {'type': 'text', 'text': 'Prior call timed out; batch record_facts first.'},
        ],
    }

    # ChatResearchAdapter also forwards tail_note at the tail without mutating initial user prompt when history exists.
    chat_msgs_no_hist, _ = ChatResearchAdapter._render(no_hist_turn)
    assert chat_msgs_no_hist == [
        {'role': 'system', 'content': 'system instruction'},
        {'role': 'user', 'content': 'research question\n\nKeep reasoning concise.'},
    ]
    chat_msgs_hist, _ = ChatResearchAdapter._render(with_hist_turn)
    assert chat_msgs_hist == [
        {'role': 'system', 'content': 'system instruction'},
        {'role': 'user', 'content': 'research question'},
        exchange.assistant.envelope,
        {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'first result'},
        {'role': 'user', 'content': 'Prior call timed out; batch record_facts first.'},
    ]


def test_explicit_cache_control_wire_formatting(monkeypatch):
    sent = []
    settings = SimpleNamespace(base_url='https://example.com/api/anthropic', model='configured-model', api_key='secret', allow_private_network=False)
    policy = SimpleNamespace(timeout_seconds=300, max_input_tokens=None)
    provider = AnthropicMessagesProvider(
        settings, policy, anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=100,
        transport=lambda m, u, h, b, t: (sent.append(json.loads(b)), response([{'type': 'text', 'text': 'ok'}], 'end_turn'))[1],
    )
    tools = (
        ModelToolDefinition('lookup', 'Lookup', {'type': 'object'}),
        ModelToolDefinition('record', 'Record', {'type': 'object'}),
    )
    exchange = ModelTurnExchange(
        ModelAssistantContinuation({'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'c1', 'name': 'lookup', 'input': {'q': 'A'}}]}),
        (ModelToolResult('c1', 'res-1'),),
    )

    # explicit_cache_control=False leaves wire format byte-identical to default.
    monkeypatch.setenv('FORESIGHTNINE_LLM_ENDPOINT_VERSION', 'v1')
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': 'https://example.com/api/anthropic/v1/messages',
        'model': 'configured-model',
        'version': 'v1',
        'mode': 'json_object',
        'explicit_cache_control': False,
    }))
    provider.complete_turn(ModelTurnInput('sys', 'q', tools, (exchange,)))
    off_wire = sent[-1]
    assert off_wire['system'] == 'sys'
    assert all('cache_control' not in t for t in off_wire['tools'])
    assert off_wire['messages'][0] == {'role': 'user', 'content': 'q'}
    assert 'cache_control' not in off_wire['messages'][-1]['content'][-1]

    # explicit_cache_control=True without history & with tail_note marks system, last tool, and stable user block only.
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': 'https://example.com/api/anthropic/v1/messages',
        'model': 'configured-model',
        'version': 'v1',
        'mode': 'json_object',
        'explicit_cache_control': True,
    }))
    provider.complete_turn(ModelTurnInput('sys', 'q', tools, (), tail_note='volatile tail'))
    on_no_hist = sent[-1]
    assert on_no_hist['system'] == [{'type': 'text', 'text': 'sys', 'cache_control': {'type': 'ephemeral'}}]
    assert 'cache_control' not in on_no_hist['tools'][0]
    assert on_no_hist['tools'][1]['cache_control'] == {'type': 'ephemeral'}
    assert on_no_hist['messages'] == [{
        'role': 'user',
        'content': [
            {'type': 'text', 'text': 'q', 'cache_control': {'type': 'ephemeral'}},
            {'type': 'text', 'text': 'volatile tail'},
        ],
    }]

    # explicit_cache_control=True with history & tail_note marks last tool_result before volatile tail_note.
    provider.complete_turn(ModelTurnInput('sys', 'q', tools, (exchange,), tail_note='volatile tail'))
    on_hist = sent[-1]
    assert on_hist['system'] == [{'type': 'text', 'text': 'sys', 'cache_control': {'type': 'ephemeral'}}]
    assert on_hist['tools'][1]['cache_control'] == {'type': 'ephemeral'}
    assert on_hist['messages'][0] == {'role': 'user', 'content': 'q'}
    assert on_hist['messages'][2] == {
        'role': 'user',
        'content': [
            {'type': 'tool_result', 'tool_use_id': 'c1', 'content': 'res-1', 'is_error': False, 'cache_control': {'type': 'ephemeral'}},
            {'type': 'text', 'text': 'volatile tail'},
        ],
    }

    # explicit_cache_control=True with history and no tail_note marks last tool_result.
    provider.complete_turn(ModelTurnInput('sys', 'q', tools, (exchange,)))
    on_hist_clean = sent[-1]
    assert on_hist_clean['messages'][2]['content'][-1]['cache_control'] == {'type': 'ephemeral'}


def test_mid_stream_timeout_after_zero_message_start_reports_none_and_stream_metrics(monkeypatch):
    import time
    from app.openai_compatible import _TransportTimeout
    from app.research_budget import ResearchExecutionBudget
    from app.research_coordinator import ResearchExecutionContext

    monkeypatch.setenv('FORESIGHTNINE_LLM_ENDPOINT_VERSION', 'v1')
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': 'https://example.com/api/anthropic/v1/messages',
        'model': 'configured-model',
        'version': 'v1',
        'mode': 'json_object',
        'native_tools': True,
        'stream': True,
    }))

    def mock_abort_stream(method, url, headers, body, timeout, *, on_frame, **_kwargs):
        on_frame('message_start', json.dumps({
            'type': 'message_start',
            'message': {'role': 'assistant', 'content': [], 'usage': {'input_tokens': 0, 'output_tokens': 0}},
        }))
        on_frame('content_block_start', json.dumps({
            'type': 'content_block_start',
            'index': 0,
            'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''},
        }))
        on_frame('content_block_delta', json.dumps({
            'type': 'content_block_delta',
            'index': 0,
            'delta': {'type': 'thinking_delta', 'thinking': 'SECRET_THOUGHT'},
        }))
        on_frame('content_block_delta', json.dumps({
            'type': 'content_block_delta',
            'index': 0,
            'delta': {'type': 'signature_delta', 'signature': 'SIG'},
        }))
        on_frame('content_block_stop', json.dumps({'type': 'content_block_stop', 'index': 0}))
        on_frame('content_block_start', json.dumps({
            'type': 'content_block_start',
            'index': 1,
            'content_block': {'type': 'tool_use', 'id': 'tu-1', 'name': 'read_sources'},
        }))
        on_frame('content_block_delta', json.dumps({
            'type': 'content_block_delta',
            'index': 1,
            'delta': {'type': 'input_json_delta', 'partial_json': '{"SECRET_TOOL_ARG":"secret"}'},
        }))
        on_frame('content_block_start', json.dumps({
            'type': 'content_block_start',
            'index': 2,
            'content_block': {'type': 'tool_use', 'id': 'tu-2', 'name': 'SECRET_UNDECLARED_TOOL'},
        }))
        raise _TransportTimeout(timeout_kind='stream_deadline_timeout')

    settings = SimpleNamespace(
        base_url='https://example.com/api/anthropic',
        model='configured-model',
        api_key='sk-secret',
        allow_private_network=False,
    )
    policy = SimpleNamespace(timeout_seconds=300, max_input_tokens=None, max_output_tokens=16384)
    provider = AnthropicMessagesProvider(
        settings, policy, anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=16384, stream_transport=mock_abort_stream,
    )
    ctx = ResearchExecutionContext(
        ResearchExecutionBudget(1, 0, None, time.monotonic() + 100.0), False,
        lambda: None, lambda: None, lambda *_: None, lambda *_: None, lambda: None, now=time.monotonic,
    )
    res, out = ctx.prepare_model_call(provider, 'question', 'system')
    before = ctx.start_model('call-zero-abort', res)
    turn_obj = ModelTurnInput(
        'system',
        'question',
        (ModelToolDefinition('read_sources', 'Read', {'type': 'object'}),),
        (),
    )
    with pytest.raises(ProviderFailure) as caught:
        ctx.invoke_model('call-zero-abort', provider.complete_turn, turn_obj, max_output_tokens=out, on_stream_event=lambda _ev: None)
    err = caught.value
    assert err.code == 'provider_timeout'
    assert err.provider_response_received is True
    assert err.reported_tokens is None
    assert err.timeout_kind == 'stream_deadline_timeout'

    ctx.finish_model('call-zero-abort', err.reported_tokens, before)
    assert ctx.model_call_receipts['call-zero-abort'] == {
        'provider': 'returned',
        'settlement': 'unknown',
        'processing_error': 'stream_timeout',
        'reported_tokens': None,
        'reserved_estimate_tokens': before[2],
    }
    assert ctx.usage is None
    diag = ctx.private_diagnostics['call-zero-abort']
    assert 'usage' not in diag
    metrics = diag['stream_metrics']
    assert metrics['frames_received'] == 8
    assert type(metrics['ttft_ms']) is int and metrics['ttft_ms'] >= 0
    assert type(metrics['last_frame_elapsed_ms']) is int and metrics['last_frame_elapsed_ms'] >= metrics['ttft_ms']
    expected_think = len('SECRET_THOUGHT'.encode('utf-8')) + len('SIG'.encode('utf-8'))
    expected_tool = len('{"SECRET_TOOL_ARG":"secret"}'.encode('utf-8'))
    assert metrics['block_bytes_by_type'] == {
        'thinking': expected_think,
        'text': 0,
        'tool_use': expected_tool,
        'redacted_thinking': 0,
    }
    assert metrics['reassembled_bytes'] == expected_think + expected_tool
    assert metrics['active_tool_names'] == ['read_sources', 'unrecognized']
    dumped = repr(diag) + repr(ctx.model_call_receipts)
    for forbidden in ('SECRET_THOUGHT', 'SECRET_TOOL_ARG', 'SECRET_UNDECLARED_TOOL', 'sk-secret'):
        assert forbidden not in dumped


def test_mid_stream_nonzero_usage_and_normal_stream_metrics(monkeypatch):
    import time
    from app.openai_compatible import _TransportTimeout
    from app.research_budget import ResearchExecutionBudget
    from app.research_coordinator import ResearchExecutionContext

    monkeypatch.setenv('FORESIGHTNINE_LLM_ENDPOINT_VERSION', 'v1')
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': 'https://example.com/api/anthropic/v1/messages',
        'model': 'configured-model',
        'version': 'v1',
        'mode': 'json_object',
        'native_tools': True,
        'stream': True,
    }))
    settings = SimpleNamespace(
        base_url='https://example.com/api/anthropic',
        model='configured-model',
        api_key='sk-secret',
        allow_private_network=False,
    )
    policy = SimpleNamespace(timeout_seconds=300, max_input_tokens=None, max_output_tokens=16384)

    # Case A: positive counts from message_start before abort are preserved.
    def nonzero_abort(method, url, headers, body, timeout, *, on_frame, **_kwargs):
        on_frame('message_start', json.dumps({
            'type': 'message_start',
            'message': {'role': 'assistant', 'content': [], 'usage': {'input_tokens': 120, 'output_tokens': 1}},
        }))
        raise _TransportTimeout(timeout_kind='stream_idle_timeout')

    p_nonzero = AnthropicMessagesProvider(
        settings, policy, anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=16384, stream_transport=nonzero_abort,
    )
    with pytest.raises(ProviderFailure) as caught:
        p_nonzero.complete_turn(turn(), on_stream_event=lambda _ev: None)
    assert caught.value.reported_tokens == 121

    # Case B: normal completion persists sanitized stream_metrics alongside usage.
    def normal_stream(method, url, headers, body, timeout, *, on_frame, **_kwargs):
        on_frame('message_start', json.dumps({
            'type': 'message_start',
            'message': {'role': 'assistant', 'content': [], 'usage': {'input_tokens': 0, 'output_tokens': 0}},
        }))
        on_frame('content_block_start', json.dumps({
            'type': 'content_block_start',
            'index': 0,
            'content_block': {'type': 'text', 'text': 'hello'},
        }))
        on_frame('content_block_stop', json.dumps({'type': 'content_block_stop', 'index': 0}))
        on_frame('message_delta', json.dumps({
            'type': 'message_delta',
            'delta': {'stop_reason': 'end_turn'},
            'usage': {'input_tokens': 15, 'output_tokens': 5},
        }))
        on_frame('message_stop', json.dumps({'type': 'message_stop'}))
        return None

    p_ok = AnthropicMessagesProvider(
        settings, policy, anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=16384, stream_transport=normal_stream,
    )
    ctx = ResearchExecutionContext(
        ResearchExecutionBudget(1, 0, None, time.monotonic() + 100.0), False,
        lambda: None, lambda: None, lambda *_: None, lambda *_: None, lambda: None, now=time.monotonic,
    )
    res, out = ctx.prepare_model_call(p_ok, 'question', 'system')
    before = ctx.start_model('call-ok', res)
    result = ctx.invoke_model('call-ok', p_ok.complete_turn, turn(), max_output_tokens=out, on_stream_event=lambda _ev: None)
    ctx.finish_model('call-ok', result.usage.total_tokens, before)
    diag = ctx.private_diagnostics['call-ok']
    assert diag['usage'] == {'input_tokens': 15, 'output_tokens': 5, 'total_tokens': 20}
    assert diag['stream_metrics']['frames_received'] == 5
    assert diag['stream_metrics']['block_bytes_by_type'] == {'thinking': 0, 'text': 5, 'tool_use': 0, 'redacted_thinking': 0}
    assert diag['stream_metrics']['reassembled_bytes'] == 5
    assert diag['stream_metrics']['active_tool_names'] == []


def _count_anthropic_breakpoints(wire: dict) -> int:
    count = 0
    if isinstance(wire.get('system'), list):
        count += sum(1 for b in wire['system'] if isinstance(b, dict) and 'cache_control' in b)
    for tool in wire.get('tools') or ():
        if isinstance(tool, dict) and 'cache_control' in tool:
            count += 1
    for msg in wire.get('messages') or ():
        content = msg.get('content')
        if isinstance(content, list):
            count += sum(1 for b in content if isinstance(b, dict) and 'cache_control' in b)
    return count


def test_rolling_cache_breakpoints_prefix_stability_and_schema_normalization(monkeypatch):
    raw_bodies = []
    sent_wires = []
    settings = SimpleNamespace(base_url='https://example.com/api/anthropic', model='configured-model', api_key='secret', allow_private_network=False)
    policy = SimpleNamespace(timeout_seconds=300, max_input_tokens=None)
    provider = AnthropicMessagesProvider(
        settings, policy, anthropic_version='2023-06-01', auth_scheme='x-api-key', max_tokens=100,
        transport=lambda m, u, h, b, t: (
            raw_bodies.append(b),
            sent_wires.append(json.loads(b)),
            response([{'type': 'text', 'text': 'ok'}], 'end_turn'),
        )[2],
    )
    monkeypatch.setenv('FORESIGHTNINE_LLM_ENDPOINT_VERSION', 'v1')
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': 'https://example.com/api/anthropic/v1/messages',
        'model': 'configured-model',
        'version': 'v1',
        'mode': 'json_object',
        'explicit_cache_control': True,
    }))

    tools_a = (
        ModelToolDefinition('lookup', 'Lookup', {'type': 'object', 'properties': {'z': {'type': 'string'}, 'a': {'type': 'number'}}}),
        ModelToolDefinition('record', 'Record', {'properties': {'second': {'type': 'integer'}, 'first': {'type': 'boolean'}}, 'type': 'object'}),
    )
    tools_b = (
        ModelToolDefinition('lookup', 'Lookup', {'properties': {'a': {'type': 'number'}, 'z': {'type': 'string'}}, 'type': 'object'}),
        ModelToolDefinition('record', 'Record', {'type': 'object', 'properties': {'first': {'type': 'boolean'}, 'second': {'type': 'integer'}}}),
    )

    ex1 = ModelTurnExchange(
        ModelAssistantContinuation({'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 't1', 'signature': 's1'}, {'type': 'tool_use', 'id': 'c1', 'name': 'lookup', 'input': {'z': '1', 'a': 2}}]}),
        (ModelToolResult('c1', 'res-1'),),
    )
    ex2 = ModelTurnExchange(
        ModelAssistantContinuation({'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'c2', 'name': 'lookup', 'input': {'a': 3, 'z': '4'}}]}),
        (ModelToolResult('c2', 'res-2'),),
    )
    ex3 = ModelTurnExchange(
        ModelAssistantContinuation({'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'c3', 'name': 'lookup', 'input': {'z': '5', 'a': 6}}]}),
        (ModelToolResult('c3', 'res-3'),),
    )

    # Turn 1 (1 exchange) -> 3 breakpoints (system, last tool, ex1 results); tail_note uncached.
    provider.complete_turn(ModelTurnInput('sys', 'q', tools_a, (ex1,), tail_note='volatile-1'))
    w1 = sent_wires[-1]
    assert _count_anthropic_breakpoints(w1) == 3
    assert w1['messages'][2]['content'][-1] == {'type': 'text', 'text': 'volatile-1'}
    assert w1['messages'][2]['content'][-2]['cache_control'] == {'type': 'ephemeral'}

    # Turn 2 (2 exchanges) -> 4 breakpoints (system, last tool, ex1 results, ex2 results); ex1 matches Turn 1 cached prefix; tail_note uncached.
    provider.complete_turn(ModelTurnInput('sys', 'q', tools_a, (ex1, ex2), tail_note='volatile-2'))
    w2 = sent_wires[-1]
    assert _count_anthropic_breakpoints(w2) == 4
    assert w2['messages'][2] == {'role': 'user', 'content': [w1['messages'][2]['content'][0]]}
    assert w2['messages'][2]['content'][-1]['cache_control'] == {'type': 'ephemeral'}
    assert w2['messages'][4]['content'][-2]['cache_control'] == {'type': 'ephemeral'}
    assert w2['messages'][4]['content'][-1] == {'type': 'text', 'text': 'volatile-2'}

    # Turn 3 (3 exchanges) -> still <= 4 breakpoints; rolling window moves from ex1 to (ex2, ex3).
    provider.complete_turn(ModelTurnInput('sys', 'q', tools_a, (ex1, ex2, ex3), tail_note='volatile-3'))
    w3 = sent_wires[-1]
    assert _count_anthropic_breakpoints(w3) == 4
    assert 'cache_control' not in w3['messages'][2]['content'][-1]
    assert w3['messages'][4] == {'role': 'user', 'content': [w2['messages'][4]['content'][0]]}
    assert w3['messages'][4]['content'][-1]['cache_control'] == {'type': 'ephemeral'}
    assert w3['messages'][6]['content'][-2]['cache_control'] == {'type': 'ephemeral'}
    assert w3['messages'][6]['content'][-1] == {'type': 'text', 'text': 'volatile-3'}

    # Reordered schema keys produce byte-identical HTTP wire body and sorted in-memory schema keys.
    provider.complete_turn(ModelTurnInput('sys', 'q', tools_b, (ex1, ex2, ex3), tail_note='volatile-3'))
    assert raw_bodies[-1] == raw_bodies[-2]
    assert list(sent_wires[-1]['tools'][0]['input_schema'].keys()) == ['properties', 'type']
    assert list(sent_wires[-1]['tools'][0]['input_schema']['properties'].keys()) == ['a', 'z']


def test_thinking_blocks_never_receive_cache_control(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LLM_ENDPOINT_VERSION', 'v1')
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': 'https://example.com/api/anthropic/v1/messages',
        'model': 'configured-model',
        'version': 'v1',
        'mode': 'json_object',
        'explicit_cache_control': True,
    }))
    provider, _ = fixture(response([{'type': 'text', 'text': 'ok'}], 'end_turn'))
    ex = ModelTurnExchange(
        ModelAssistantContinuation({
            'role': 'assistant',
            'content': [
                {'type': 'text', 'text': 'reasoning summary'},
                {'type': 'thinking', 'thinking': 'hidden', 'signature': 'sig'},
            ],
        }),
        (),
    )
    wire = provider._wire(ModelTurnInput('sys', 'q', (), (ex,)))
    asst_blocks = wire['messages'][1]['content']
    assert 'cache_control' not in asst_blocks[1]
    assert asst_blocks[0]['cache_control'] == {'type': 'ephemeral'}


def test_anthropic_usage_captures_nested_cache_creation_and_hit_ratio():
    usage = AnthropicMessagesProvider._usage_from_dict({
        'input_tokens': 20,
        'output_tokens': 10,
        'cache_read_input_tokens': 50,
        'cache_creation': {'ephemeral_5m_input_tokens': 20, 'ephemeral_1h_input_tokens': 10},
    })
    assert usage.cache_creation_input_tokens == 30
    assert usage.cache_read_input_tokens == 50
    assert usage.total_tokens == 110
    assert usage.cache_hit_ratio == pytest.approx(0.5)
