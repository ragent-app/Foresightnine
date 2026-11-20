"""No-network capability, cost-bound and response-state tests."""
import json
from dataclasses import dataclass
import pytest
from app.openai_compatible import HttpResponse
from app.phase3 import ProviderFailure
from tests.test_openai_compatible_provider import provider

@dataclass
class Contract:
    name: str = 'test_draft_v1'
    version: str = 'v1'
    schema: object = None
    def __post_init__(self):
        if self.schema is None: self.schema = {'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}


def profile(monkeypatch, mode, *, version='v1', endpoint='https://llm.example.test/v1/chat/completions'):
    monkeypatch.setenv('FORESIGHTNINE_LLM_ENDPOINT_VERSION',version)
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE',json.dumps({'endpoint':endpoint,'model':'test-model','version':version,'mode':mode,'schema_dialect':'required_nullable_v1'}))


def envelope(message, reason='stop', usage=5):
    return json.dumps({'choices':[{'finish_reason':reason,'message':message}],'usage':{'total_tokens':usage}})


@pytest.mark.parametrize('mode', ['json_object','json_schema','tool'])
def test_explicit_capability_one_request_and_virtual_tool(monkeypatch, mode):
    calls=[]
    def transport(method,url,headers,body,timeout):
        calls.append(json.loads(body))
        message = ({'tool_calls':[{'type':'function','function':{'name':'test_draft_v1','arguments':'{"ok":true}'}}]}
                   if mode == 'tool' else {'content':'{"ok":true}'})
        return HttpResponse(200,envelope(message,reason='tool_calls' if mode == 'tool' else 'stop'))
    instance=provider(monkeypatch,transport); profile(monkeypatch,mode)
    result,tokens=instance.generate_structured('x',system='s',contract=Contract())
    assert result=={'ok':True} and tokens==5 and len(calls)==1
    if mode=='json_schema': assert calls[0]['response_format']['json_schema']['strict'] is True
    if mode=='tool': assert calls[0]['tool_choice']['function']['name']=='test_draft_v1'
    if mode=='json_object': assert calls[0]['response_format']=={'type':'json_object'}


def test_unknown_or_mismatched_version_defaults_json_object(monkeypatch):
    calls=[]
    instance=provider(monkeypatch,lambda *args: (calls.append(json.loads(args[3])) or HttpResponse(200,envelope({'content':'{"ok":true}'}))))
    profile(monkeypatch,'json_schema'); monkeypatch.setenv('FORESIGHTNINE_LLM_ENDPOINT_VERSION','other')
    instance.generate_structured('x',system='s',contract=Contract())
    assert calls[0]['response_format']=={'type':'json_object'} and len(calls)==1


def test_contract_overhead_enforced_before_submission(monkeypatch):
    from dataclasses import replace
    from tests.test_research_capacity_unset import context
    calls=[]; instance=provider(monkeypatch,lambda *args: calls.append(args))
    instance.policy=replace(instance.policy,max_input_tokens=3000)
    profile(monkeypatch,'json_schema')
    ctx,_ = context()
    with pytest.raises(ProviderFailure) as exc:
        ctx.prepare_model_call(instance, 'x'*2500, 's', Contract(schema={'description':'x'*2000}))
    assert exc.value.code=='provider_budget_exhausted' and calls==[]


@pytest.mark.parametrize('reason,message,code', [('length',{'content':'{}'},'provider_output_truncated'),('stop',{'refusal':'private'},'provider_output_refused')])
def test_incomplete_or_refused_no_retry_usage_preserved(monkeypatch,reason,message,code):
    calls=[]; instance=provider(monkeypatch,lambda *args:(calls.append(1) or HttpResponse(200,envelope(message,reason=reason,usage=31))))
    with pytest.raises(ProviderFailure) as exc:
        instance.generate_structured('x',system='s',contract=Contract())
    assert exc.value.code==code and exc.value.reported_tokens==31 and len(calls)==1
    assert 'private' not in str(exc.value)


def test_dual_input_estimation_separates_utf8_bound_and_chars_div_3():
    from app.provider_capabilities import (
        DualInputEstimate,
        estimate_dual_input,
        estimate_heuristic_tokens,
        estimate_utf8_byte_upper_bound,
    )
    assert estimate_heuristic_tokens('') == 0
    assert estimate_heuristic_tokens('a') == 1
    assert estimate_heuristic_tokens('abc') == 1
    assert estimate_heuristic_tokens('abcd') == 2
    assert estimate_utf8_byte_upper_bound('中文') == 6
    assert estimate_heuristic_tokens('中文') == 1
    english = 'a' * 242082
    dual = estimate_dual_input(english, envelope_bytes=64, envelope_chars=0)
    assert isinstance(dual, DualInputEstimate)
    assert dual.utf8_byte_upper_bound == 242146
    assert dual.heuristic_tokens == 80694
    assert dual.char_count == 242082
    assert dual.heuristic_method == 'ceil_chars_div_3'


def test_capability_profile_chars_div_3_context_fit_and_dual_reservation(monkeypatch):
    from types import SimpleNamespace
    from app.phase3 import ProviderPolicy
    from app.provider_capabilities import configured_profile
    from tests.test_research_capacity_unset import context

    endpoint = 'https://custom-endpoint.example.test/v1/messages'
    monkeypatch.setenv('FORESIGHTNINE_LLM_ENDPOINT_VERSION', 'v1')
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': endpoint,
        'model': 'custom-model-v1',
        'version': 'v1',
        'mode': 'json_object',
        'context_tokens': 131072,
        'output_tokens': 16384,
        'input_counter': 'chars_div_3',
    }))
    loaded = configured_profile(endpoint, 'custom-model-v1')
    assert loaded is not None and loaded.input_counter == 'chars_div_3'

    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': endpoint,
        'model': 'custom-model-v1',
        'version': 'v1',
        'mode': 'json_object',
        'input_counter': 'unsupported_counter',
    }))
    with pytest.raises(ProviderFailure) as invalid_exc:
        configured_profile(endpoint, 'custom-model-v1')
    assert invalid_exc.value.code == 'provider_configuration_invalid'

    monkeypatch.delenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', raising=False)
    provider_double = type('P', (), {
        'policy': ProviderPolicy(max_output_tokens=16384),
        'research_capacity': lambda self: SimpleNamespace(output_tokens=16384, context_tokens=131072, input_counter='chars_div_3'),
    })()
    ctx, _ = context(entitlement=50_000_000)
    prompt = 'x' * 240_000
    system = 'sys'
    reservation, output = ctx.prepare_model_call(provider_double, prompt, system)
    expected_utf8 = 240_000 + 3 + 192
    expected_heuristic = (expected_utf8 + 2) // 3
    assert output == 16384
    assert reservation == expected_utf8 + 16384
    assert ctx.pending_reservation['estimated_input_bytes_upper_bound'] == expected_utf8
    assert ctx.pending_reservation['estimated_input_tokens_heuristic'] == expected_heuristic
    assert ctx.pending_reservation['context_input_counter'] == 'chars_div_3'
    assert ctx.pending_reservation['context_input_tokens'] == expected_heuristic


def test_research_output_token_overrides_work_without_model_brand_assumptions(monkeypatch):
    from app.provider_capabilities import configured_research_max_output_tokens, resolve_output_token_limit
    monkeypatch.delenv('FORESIGHTNINE_RESEARCH_MAX_OUTPUT_TOKENS', raising=False)
    assert configured_research_max_output_tokens() is None
    monkeypatch.setenv('FORESIGHTNINE_RESEARCH_MAX_OUTPUT_TOKENS', '16384')
    assert configured_research_max_output_tokens() == 16384
    for bad in ('0', '-10', 'not-an-int'):
        monkeypatch.setenv('FORESIGHTNINE_RESEARCH_MAX_OUTPUT_TOKENS', bad)
        with pytest.raises(ProviderFailure) as exc:
            configured_research_max_output_tokens()
        assert exc.value.code == 'provider_configuration_invalid'
    assert resolve_output_token_limit(None, 16384, 8192, None) == 8192
    assert resolve_output_token_limit(None, None) is None


def test_capability_profile_work_context_tokens_and_explicit_cache_control_validation(monkeypatch):
    from app.phase3 import ProviderPolicy
    from app.provider_capabilities import configured_profile
    from tests.test_research_capacity_unset import context

    endpoint = 'https://custom-endpoint.example.test/v1/messages'
    monkeypatch.setenv('FORESIGHTNINE_LLM_ENDPOINT_VERSION', 'v1')
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': endpoint,
        'model': 'custom-model-v1',
        'version': 'v1',
        'mode': 'json_object',
        'output_tokens': 16384,
        'work_context_tokens': 64000,
        'explicit_cache_control': True,
    }))
    loaded = configured_profile(endpoint, 'custom-model-v1')
    assert loaded is not None
    assert loaded.work_context_tokens == 64000
    assert loaded.explicit_cache_control is True

    # Default explicit_cache_control is False and work_context_tokens is None when omitted.
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': endpoint,
        'model': 'custom-model-v1',
        'version': 'v1',
        'mode': 'json_object',
    }))
    defaults = configured_profile(endpoint, 'custom-model-v1')
    assert defaults is not None
    assert defaults.work_context_tokens is None
    assert defaults.explicit_cache_control is False

    for bad_work in (0, -1, True, 1.5, '64000', None):
        monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
            'endpoint': endpoint,
            'model': 'custom-model-v1',
            'version': 'v1',
            'mode': 'json_object',
            'work_context_tokens': bad_work,
        }))
        with pytest.raises(ProviderFailure) as exc:
            configured_profile(endpoint, 'custom-model-v1')
        assert exc.value.code == 'provider_configuration_invalid'

    for bad_cache in ('true', 1, 0, None):
        monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
            'endpoint': endpoint,
            'model': 'custom-model-v1',
            'version': 'v1',
            'mode': 'json_object',
            'explicit_cache_control': bad_cache,
        }))
        with pytest.raises(ProviderFailure) as exc:
            configured_profile(endpoint, 'custom-model-v1')
        assert exc.value.code == 'provider_configuration_invalid'

    # work_context_tokens MUST NOT trigger billing/input-admission rejection in prepare_model_call.
    monkeypatch.setenv('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', json.dumps({
        'endpoint': endpoint,
        'model': 'custom-model-v1',
        'version': 'v1',
        'mode': 'json_object',
        'output_tokens': 16384,
        'work_context_tokens': 32000,
        'input_counter': 'chars_div_3',
    }))
    profile_obj = configured_profile(endpoint, 'custom-model-v1')
    provider_double = type('P', (), {
        'policy': ProviderPolicy(max_output_tokens=16384),
        'research_capacity': lambda self: profile_obj,
    })()
    ctx, _ = context(entitlement=50_000_000)
    reservation, output = ctx.prepare_model_call(provider_double, 'x' * 150_000, 'sys')
    assert output == 16384
    assert reservation == 150_000 + 3 + 192 + 16384
    assert 'context_input_tokens' not in ctx.pending_reservation
