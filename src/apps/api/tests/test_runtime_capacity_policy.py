"""Offline black-box checks for optional research budgets and real ledger admission."""
import json
import time
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as api
from app.abuse import ModelCallQuota
from app.openai_compatible import HttpResponse, OpenAICompatibleLlmProvider, OpenAICompatibleSettings
from app.phase3 import ProviderPolicy
from app.research_output_contract import ANALYSIS_CONTRACT


def test_default_daily_quota_does_not_deny_21st_but_explicit_zero_does():
    unlimited = ModelCallQuota()
    assert all(unlimited.reserve(f'call-{i}', ['account:test']).allowed for i in range(21))
    assert not ModelCallQuota(limit=0).reserve('call-zero', ['account:test']).allowed
    limited = ModelCallQuota(limit=2)
    assert limited.reserve('a',['account:test']).allowed
    assert limited.reserve('b',['account:test']).allowed
    assert not limited.reserve('c',['account:test']).allowed


def test_unknown_capacity_default_provider_omits_artificial_output_cap():
    calls=[]
    settings=OpenAICompatibleSettings.from_values(base_url='https://llm.example.test/v1',model='fixture',
        api_key='fake',allowlist='llm.example.test')
    def transport(method,url,headers,body,timeout):
        calls.append(json.loads(body))
        return HttpResponse(200,json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}], 'usage':{'total_tokens':17}}))
    instance=OpenAICompatibleLlmProvider(settings,policy=ProviderPolicy(max_input_tokens=None,max_output_tokens=None,timeout_seconds=20),transport=transport)
    result,usage=instance.generate_structured('hello',system='test')
    assert result=={'ok':True} and usage==17
    assert len(calls)==1 and 'max_tokens' not in calls[0]


def test_hosted_research_without_run_budget_preserves_account_ledger(monkeypatch):
    class LocalProvider:
        name='local-hosted'
        policy=SimpleNamespace(max_input_tokens=None,max_output_tokens=None,timeout_seconds=20)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds,contract=None,max_output_tokens=None):
            from app.research_acquisition import SYSTEM as ACQUISITION_SYSTEM
            data=json.loads(prompt)
            if system == ACQUISITION_SYSTEM:
                target = data['target']['requirement_id']
                if data['ranges']:
                    ref = data['ranges'][0]
                    return {'requirement_id':target, 'action':'read', 'source_id':ref['source_id'],
                            'character_start':ref['character_start'], 'character_end':ref['character_end'],
                            'expected_gap':'Evidence', 'result_update':'Read evidence', 'reason':'Relevant source'},17
                return {'requirement_id':target, 'action':'deliver_partial', 'expected_gap':'Unknown',
                        'result_update':'Preserve result', 'reason':'No further supported action'},17
            assert contract is ANALYSIS_CONTRACT
            source_id=data['sources'][0]['source_id']
            draft={key:[] for key in ANALYSIS_CONTRACT.example if key!='version'}
            draft.update(version=ANALYSIS_CONTRACT.version,evidence=[{'id':'e','source_id':source_id,'quotation':'Revenue was 10 dollars.','source_page':None}],
                claims=[{'id':'c','text':'Revenue was 10 dollars.','scope_declaration':'other','assertions':[{'text':'Revenue was 10 dollars.','evidence_ids':['e'],'calculation_ids':[]}],'responds_to':[]}])
            return draft,17
    monkeypatch.setattr(api,'challenge_provider',LocalProvider())
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE','mock')
    client=TestClient(api.app)
    register=client.post('/v1/auth/local/register',json={'email':f'capacity-{uuid4().hex}@example.test','password':'correct horse battery staple!'})
    assert register.status_code==201,register.text
    headers={'Authorization':'Bearer '+register.json()['access_token']}
    source=client.post('/v2/research/sources',headers=headers,json={'title':'Report','text':'Revenue was 10 dollars.',
        'permissions':{'store':True,'display':True,'model_input':True,'export':True}})
    assert source.status_code==201,source.text
    topic=client.post('/v2/research/topics',headers=headers,json={'question':'What was revenue?','source_ids':[source.json()['source_id']]})
    assert topic.status_code==201,topic.text
    path='/v2/research/topics/'+topic.json()['topic_id']
    response=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'capacity-hosted'},json={'parent_version_id':topic.json()['versions'][0]['version_id'],
        'plan_research':False,'gap_followup':False,'support_review':False,'counterpoint_followup':False,'require_counterarguments':False,'discover_sources':False,'auto_fetch_official':False})
    assert response.status_code==202,response.text
    operation=response.json()['operation']['operation_id']
    for _ in range(300):
        state=client.get(path+'/runs/'+operation,headers=headers).json()['operation']['status']
        if state in {'completed','failed','cancelled'}:break
        time.sleep(.01)
    if state != 'completed':
        print('SAFE_ERRORS', [(e.get('error',{}).get('error_code'),e.get('error',{}).get('message')) for e in api.operations.store.get_events(operation) if e.get('event') == 'operation.error'])
    assert state=='completed'
    entries=[entry for key,entry in api.billing_ledger.usage.items() if key.startswith('model:'+operation)]
    assert entries and all(entry.status=='settled' and entry.actual_tokens==17 for entry in entries)
    assert all(entry.estimated_tokens>=17 for entry in entries)


def test_legacy_challenge_reserves_before_provider_with_derived_cap():
    from tests.test_openai_compatible_provider import request,valid_payload
    events=[]
    settings=OpenAICompatibleSettings.from_values(base_url='https://llm.example.test/v1',model='fixture',
        api_key='fake',allowlist='llm.example.test')
    def transport(method,url,headers,body,timeout):
        events.append(('send',json.loads(body)))
        return HttpResponse(200,json.dumps({'choices':[{'message':{'content':json.dumps(valid_payload())}}],
                                               'usage':{'total_tokens':17}}))
    instance=OpenAICompatibleLlmProvider(settings,policy=ProviderPolicy(max_input_tokens=None,max_output_tokens=None,timeout_seconds=20),transport=transport)
    def reserve(prompt,system):
        assert prompt and system
        events.append(('reserve',None))
        return 777
    instance.generate_challenge(request(),prepare_call=reserve)
    assert [event[0] for event in events]==['reserve','send']
    assert events[1][1]['max_tokens']==777
