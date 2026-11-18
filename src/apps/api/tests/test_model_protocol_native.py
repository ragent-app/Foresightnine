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
