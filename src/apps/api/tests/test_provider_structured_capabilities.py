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
