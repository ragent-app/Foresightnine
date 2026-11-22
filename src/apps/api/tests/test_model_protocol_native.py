import json

import pytest

from app.openai_compatible import HttpResponse, OpenAICompatibleLlmProvider, OpenAICompatibleSettings
from app.phase3 import ProviderFailure


def provider(monkeypatch, response):
    monkeypatch.setenv('FORESIGHTNINE_LLM_ENDPOINT_VERSION', 'v1')
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': 'https://example.com/v1/chat/completions', 'model': 'deployment',
        'version': 'v1', 'mode': 'json_object', 'native_tools': True,
    }))
    settings = OpenAICompatibleSettings.from_values(base_url='https://example.com/v1', model='deployment', api_key='test', allowlist='example.com')
    sent = []
    def transport(method, url, headers, body, timeout):
        sent.append(json.loads(body))
        return HttpResponse(200, json.dumps(response))
    return OpenAICompatibleLlmProvider(settings, transport=transport), sent


def test_native_tool_turn_preserves_association_and_usage(monkeypatch):
    p, sent = provider(monkeypatch, {'choices': [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call_1', 'type': 'function', 'function': {'name': 'read', 'arguments': '{"id":1}'}}]}}], 'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}})
    result = p.complete([{'role': 'user', 'content': 'read'}], [{'type': 'function', 'function': {'name': 'read', 'parameters': {'type': 'object'}}}])
    assert result.tool_calls[0].id == 'call_1'
    assert result.message['tool_calls'][0]['function']['arguments'] == '{"id":1}'
    assert result.usage.total_tokens == 5
    assert result.finish_reason == 'tool_calls'
    assert 'max_tokens' not in sent[0]


def test_unknown_capability_fails_before_submission(monkeypatch):
    p, sent = provider(monkeypatch, {})
    monkeypatch.delenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE')
    with pytest.raises(ProviderFailure):
        p.complete([{'role': 'user', 'content': 'read'}], [{'type': 'function'}])
    assert sent == []


def test_parameters_and_only_allowed_continuation_are_preserved(monkeypatch):
    p, sent = provider(monkeypatch, {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'done', 'continuation_token': 'opaque', 'reasoning_content': 'private'}}]})
    profile = json.loads(__import__('os').environ['FORESIGHTNINE_LLM_CAPABILITY_PROFILE'])
    profile.update(generation_parameters=['temperature', 'vendor_option'], continuation=True, continuation_fields=['continuation_token'])
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps(profile))
    monkeypatch.setenv('FORESIGHTNINE_LLM_GENERATION_PARAMETERS', json.dumps({'temperature': 0.2, 'vendor_option': {'enabled': True}}))
    result = p.complete([{'role': 'user', 'content': 'hello'}], [])
    assert sent[0]['temperature'] == 0.2
    assert sent[0]['vendor_option'] == {'enabled': True}
    assert result.message['continuation_token'] == 'opaque'
    assert 'reasoning_content' not in result.message


def test_unapproved_parameter_reports_not_started(monkeypatch):
    p, sent = provider(monkeypatch, {})
    monkeypatch.setenv('FORESIGHTNINE_LLM_GENERATION_PARAMETERS', '{"temperature": 0.2}')
    observations = []
    with pytest.raises(ProviderFailure):
        p.complete([{'role': 'user', 'content': 'hello'}], [], diagnostic_callback=observations.append)
    assert observations[0]['submission_state'] == 'not_started'
    assert sent == []


def test_cancel_after_response_preserves_usage(monkeypatch):
    from threading import Event
    event = Event()
    p, _ = provider(monkeypatch, {})
    def transport(*args):
        event.set()
        return HttpResponse(200, json.dumps({'usage': {'total_tokens': 7}}))
    p.transport = transport
    with pytest.raises(ProviderFailure) as exc:
        p.complete([{'role': 'user', 'content': 'hello'}], [], cancel_event=event)
    assert exc.value.reported_tokens == 7
    assert exc.value.provider_response_received is True


def test_non_success_diagnostic_does_not_expose_error_body(monkeypatch):
    p, _ = provider(monkeypatch, {})
    p.transport = lambda *args: HttpResponse(400, json.dumps({'error': {'message': 'PRIVATE_SECRET', 'code': 'invalid_request'}}))
    observed = []
    with pytest.raises(ProviderFailure):
        p.complete([{'role': 'user', 'content': 'hello'}], [], diagnostic_callback=observed.append)
    assert observed[0]['submission_state'] == 'response_received'
    assert 'provider_error' in observed[0]
    assert 'PRIVATE_SECRET' not in json.dumps(observed)


def test_openai_compatible_parses_prompt_cache_usage_fields_nonstream_and_sse(monkeypatch):
    from dataclasses import asdict
    from app.model_protocol import ModelUsage

    # Standard OpenAI prompt_tokens_details.cached_tokens (prompt_tokens includes cached_tokens).
    p, _ = provider(monkeypatch, {
        'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'done'}}],
        'usage': {
            'prompt_tokens': 1000,
            'completion_tokens': 100,
            'total_tokens': 1100,
            'prompt_tokens_details': {'cached_tokens': 800, 'audio_tokens': 0},
        },
    })
    res = p.complete([{'role': 'user', 'content': 'hello'}], [])
    assert res.usage == ModelUsage(1000, 100, 1100, None, 800)
    assert res.usage.cache_read_input_tokens == 800
    assert res.usage.cache_creation_input_tokens is None
    assert res.usage.cache_hit_ratio == pytest.approx(0.8)
    assert res.usage.usage_cache_summary() == {
        'total_input_tokens': 1000,
        'cache_read_input_tokens': 800,
        'cache_creation_input_tokens': None,
        'cache_hit_ratio': pytest.approx(0.8),
    }
    # Existing dataclass serialization contract keeps the exact 5 fields.
    assert set(asdict(res.usage).keys()) == {
        'input_tokens', 'output_tokens', 'total_tokens',
        'cache_creation_input_tokens', 'cache_read_input_tokens',
    }

    # Explicit cache_read_input_tokens + cache_creation_input_tokens (proxy/Anthropic-style over OpenAI wire).
    p2, _ = provider(monkeypatch, {
        'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'done'}}],
        'usage': {
            'prompt_tokens': 20,
            'completion_tokens': 10,
            'total_tokens': 110,
            'cache_creation_input_tokens': 30,
            'cache_read_input_tokens': 50,
        },
    })
    res2 = p2.complete([{'role': 'user', 'content': 'hello'}], [])
    assert res2.usage == ModelUsage(20, 10, 110, 30, 50)
    assert res2.usage.cache_hit_ratio == pytest.approx(0.5)

    # SSE stream parses prompt_tokens_details.cached_tokens and ignores empty {} tail usage frames.
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': 'https://example.com/v1/chat/completions',
        'model': 'deployment',
        'version': 'v1',
        'mode': 'json_object',
        'native_tools': True,
        'stream': True,
        'usage': True,
    }))
    p_sse, _ = provider(monkeypatch, {})
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': 'https://example.com/v1/chat/completions',
        'model': 'deployment',
        'version': 'v1',
        'mode': 'json_object',
        'native_tools': True,
        'stream': True,
        'usage': True,
    }))
    p_sse.stream_transport = lambda m, u, h, b, t, on_frame: (
        on_frame('', json.dumps({'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}]})),
        on_frame('', json.dumps({'choices': [], 'usage': {'prompt_tokens': 200, 'completion_tokens': 50, 'total_tokens': 250, 'prompt_tokens_details': {'cached_tokens': 150}}})),
        on_frame('', json.dumps({'choices': [], 'usage': {}})),
        on_frame('', '[DONE]'),
    )[-1] and None
    res_sse = p_sse.complete([{'role': 'user', 'content': 'hello'}], [], on_stream_event=lambda _ev: None)
    assert res_sse.usage == ModelUsage(200, 50, 250, None, 150)
    assert res_sse.usage.cache_hit_ratio == pytest.approx(0.75)

    # Invalid cache counts are rejected cleanly.
    bad = OpenAICompatibleLlmProvider._usage_from_dict({
        'prompt_tokens': 10,
        'completion_tokens': 5,
        'total_tokens': 15,
        'prompt_tokens_details': {'cached_tokens': True},
        'cache_creation_input_tokens': -1,
    })
    assert bad == ModelUsage(10, 5, 15, None, None)
    assert bad.cache_hit_ratio is None
    assert ModelUsage(0, 0, 0, 0, 0).cache_hit_ratio is None


def test_chat_research_adapter_deterministic_serialization_and_prefix_stability(monkeypatch):
    from app.chat_research_adapter import ChatResearchAdapter
    from app.model_protocol import (
        ModelAssistantContinuation,
        ModelToolDefinition,
        ModelToolResult,
        ModelTurnExchange,
        ModelTurnInput,
    )

    monkeypatch.setenv('FORESIGHTNINE_LLM_ENDPOINT_VERSION', 'v1')
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': 'https://example.com/v1/chat/completions',
        'model': 'deployment',
        'version': 'v1',
        'mode': 'json_object',
        'native_tools': True,
    }))
    settings = OpenAICompatibleSettings.from_values(
        base_url='https://example.com/v1',
        model='deployment',
        api_key='test',
        allowlist='example.com',
    )
    raw_bodies = []

    def transport(method, url, headers, body, timeout):
        raw_bodies.append(body)
        return HttpResponse(200, json.dumps({
            'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'done'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
        }))

    adapter = ChatResearchAdapter(OpenAICompatibleLlmProvider(settings, transport=transport))

    schema_a = {'type': 'object', 'properties': {'z': {'type': 'string'}, 'a': {'type': 'number'}}}
    schema_b = {'properties': {'a': {'type': 'number'}, 'z': {'type': 'string'}}, 'type': 'object'}
    user_a = json.dumps({'question': 'Q', 'available_capabilities': {'z_cap': False, 'a_cap': True}})
    user_b = json.dumps({'available_capabilities': {'a_cap': True, 'z_cap': False}, 'question': 'Q'}, indent=2)
    tool_res_a = json.dumps({'status': 'ok', 'meta': {'b': 2, 'a': 1}})
    tool_res_b = json.dumps({'meta': {'a': 1, 'b': 2}, 'status': 'ok'}, indent=2)

    ex_a = ModelTurnExchange(
        ModelAssistantContinuation({'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{"a":1}'}}]}),
        (ModelToolResult('c1', tool_res_a),),
    )
    ex_b = ModelTurnExchange(
        ModelAssistantContinuation({'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{"a":1}'}}]}),
        (ModelToolResult('c1', tool_res_b),),
    )

    turn_a = ModelTurnInput('sys', user_a, (ModelToolDefinition('lookup', 'Lookup', schema_a),), (ex_a,), tail_note='tail-1')
    turn_b = ModelTurnInput('sys', user_b, (ModelToolDefinition('lookup', 'Lookup', schema_b),), (ex_b,), tail_note='tail-1')

    msgs_a, tools_a = ChatResearchAdapter._render(turn_a)
    msgs_b, tools_b = ChatResearchAdapter._render(turn_b)
    assert msgs_a == msgs_b
    assert msgs_a[1]['content'] == '{"available_capabilities":{"a_cap":true,"z_cap":false},"question":"Q"}'
    assert msgs_a[3]['content'] == '{"meta":{"a":1,"b":2},"status":"ok"}'
    assert list(tools_a[0]['function']['parameters'].keys()) == ['properties', 'type']
    assert list(tools_a[0]['function']['parameters']['properties'].keys()) == ['a', 'z']
    assert adapter.estimate_turn_input(turn_a) == adapter.estimate_turn_input(turn_b)

    adapter.complete_turn(turn_a)
    adapter.complete_turn(turn_b)
    assert raw_bodies[-1] == raw_bodies[-2]

    # Prefix stability when tail_note changes across consecutive turns:
    turn_no_hist = ModelTurnInput('sys', user_a, (ModelToolDefinition('lookup', 'Lookup', schema_a),), ())
    msgs_t0, _ = ChatResearchAdapter._render(turn_no_hist)
    assert msgs_a[:len(msgs_t0)] == msgs_t0
    assert msgs_a[-1] == {'role': 'user', 'content': 'tail-1'}
