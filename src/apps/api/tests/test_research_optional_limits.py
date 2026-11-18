"""Optional run limits and safe recovery, with local-only providers."""
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace
from uuid import uuid4
import time

import pytest
from fastapi.testclient import TestClient
import app.main as api
from app.research_budget import ResearchExecutionBudget
from app.research_recovery import RecoveryJournal
from app.research_runs import ResearchRunRequest, resolve_automatic_research_request


def test_unset_and_explicit_limits_including_zero_are_distinct():
    automatic=resolve_automatic_research_request(ResearchRunRequest(parent_version_id='v1'),search_available=True)
    assert automatic.max_model_calls is automatic.max_search_calls is automatic.max_total_tokens is automatic.max_elapsed_seconds is None
    assert automatic.plan_research and automatic.gap_followup and automatic.support_review and automatic.auto_fetch_official
    bounded=ResearchRunRequest(parent_version_id='v1',max_model_calls=0,max_search_calls=0,max_total_tokens=0,max_elapsed_seconds=0,max_cost_usd=0)
    assert (bounded.max_model_calls,bounded.max_search_calls,bounded.max_total_tokens,bounded.max_elapsed_seconds)==(0,0,0,0)
    large=ResearchRunRequest(parent_version_id='v1',max_model_calls=15,max_search_calls=10,max_total_tokens=80000,max_elapsed_seconds=900)
    assert large.max_total_tokens==80000 and large.max_elapsed_seconds==900
    unlimited=ResearchExecutionBudget(None,None,None,float('inf'))
    assert unlimited.model_exhaustion(calls=10,usage=50000,per_call=7000,now=10) is None
    assert unlimited.search_exhaustion(calls=10,now=10) is None
    assert ResearchExecutionBudget(0,None,None,float('inf')).model_exhaustion(calls=0,usage=0,per_call=1,now=1)=='call_budget'
    assert ResearchExecutionBudget(None,0,None,float('inf')).search_exhaustion(calls=0,now=1)=='search_budget'
    assert ResearchExecutionBudget(None,None,0,float('inf')).model_exhaustion(calls=0,usage=0,per_call=1,now=1)=='token_budget'
    assert ResearchExecutionBudget(None,None,None,0).model_exhaustion(calls=0,usage=0,per_call=1,now=1)=='time_budget'


def test_unlimited_recovery_keeps_usage_and_unknown_call_blocks(monkeypatch):
    monkeypatch.setattr(api,'_reserve_model_call',lambda *a,**k:None)
    monkeypatch.setattr(api,'_settle_model_call',lambda *a,**k:None)
    client=TestClient(api.app)
    registered=client.post('/v1/auth/local/register',json={'email':f'unlimited-{uuid4().hex}@example.test',
        'password':'correct horse battery staple!'})
    assert registered.status_code==201,registered.text
    headers={'Authorization':'Bearer '+registered.json()['access_token']}
    source=client.post('/v2/research/sources',headers=headers,json={'title':'Report','text':'Revenue increased.',
        'permissions':{'store':True,'model_input':True,'display':True,'export':True}}).json()['source_id']
    topic=client.post('/v2/research/topics',headers=headers,json={'question':'How did revenue change?','source_ids':[source]}).json()
    class Provider:
        name='local-unlimited-recovery'
        policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=6000,max_output_tokens=1500)
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            return {'claims':[{'text':'Revenue increased.','citations':[{'source_id':source,'quotation':'Revenue increased.'}]}]},20
    monkeypatch.setattr(api,'challenge_provider',Provider())
    original=RecoveryJournal.commit_checkpoint
    def interrupt(self,snapshot,*,committed_call_ids):
        original(self,snapshot,committed_call_ids=committed_call_ids)
        raise RuntimeError('local interruption after saved checkpoint')
    monkeypatch.setattr(RecoveryJournal,'commit_checkpoint',interrupt)
    path='/v2/research/topics/'+topic['topic_id']
    started=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'unlimited-start'},json={
        'parent_version_id':topic['versions'][0]['version_id'],'evidence_strategy':'coordinator','plan_research':False,'gap_followup':False,
        'support_review':False,'require_counterarguments':False,'max_search_calls':0})
    assert started.status_code==202,started.text
    op=started.json()['operation']['operation_id']
    def terminal(operation):
        for _ in range(500):
            status=client.get(path+'/runs/'+operation,headers=headers).json()['operation']['status']
            if status in ('completed','failed','cancelled'): return status
            time.sleep(.01)
        raise AssertionError('operation did not finish')
    assert terminal(op)=='failed'
    record=api.RESEARCH_RUN_INPUTS.get(op); budget=record['recovery_snapshot']['budget']
    assert budget.get('max_model_calls') is None and budget.get('max_total_tokens') is None and budget.get('deadline_at') is None
    assert budget['model_calls']==1 and budget['accounted_tokens']==20
    monkeypatch.setattr(RecoveryJournal,'commit_checkpoint',original)
    state=client.get(path+'/runs/'+op+'/recovery-status',headers=headers)
    assert state.json()['can_continue'],state.text
    resumed=client.post(path+'/runs/'+op+'/continue',headers={**headers,'Idempotency-Key':'unlimited-resume'},json={})
    assert resumed.status_code==202,resumed.text
    assert terminal(resumed.json()['operation']['operation_id'])=='completed'


def test_official_retrieval_processes_multiple_existing_batch_candidates(monkeypatch):
    import base64
    from hashlib import sha256
    from app.research_coordinator import ResearchExecutionContext
    from app.research_fetch import FetchedText
    import app.research_official_retrieval as retrieval
    seen=[]
    def fetch(request,*,timeout_seconds):
        seen.append(str(request.url)); raw=b'<p>Public data.</p>'
        return FetchedText(text='Public data.',response_sha256=sha256(raw).hexdigest(),
                           content_type='text/html',original_html_base64=base64.b64encode(raw).decode())
    monkeypatch.setattr(retrieval,'fetch_official_text',fetch)
    class Store:
        def create(self,owner,data,**kwargs): return SimpleNamespace(source_id=kwargs['source_id'])
    budget=ResearchExecutionBudget(None,None,None,float('inf'))
    context=ResearchExecutionContext(budget,False,lambda:None,Event,lambda *_:None,lambda *_:None,lambda:None)
    request=ResearchRunRequest(parent_version_id='v1',auto_fetch_official=True)
    candidates=[{'url':'https://fred.stlouisfed.org/series/X','title':'Restricted'},
        {'url':'https://www.bea.gov/news/a','title':'BEA'},
        {'url':'https://www.federalreserve.gov/news/b','title':'Fed'}]
    result=retrieval.execute_official_retrieval(request=request,question='Research',as_of=None,available_count=0,
        candidates=candidates,sources=[],owner_id='owner',operation_id='op',store=Store(),sec_agent_config=None,
        reserve_sec=lambda:True,context=context)
    assert result.status=='completed' and result.calls==2 and len(result.retrieved)==2
    assert result.retrieved[0][0].source_id != result.retrieved[1][0].source_id
    assert seen==['https://www.bea.gov/news/a','https://www.federalreserve.gov/news/b']
