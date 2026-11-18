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
