"""Offline entry-point checks; injected tools do not contact providers."""
import base64
import json
import time
from datetime import date
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
import app.main as api
import app.research_official_retrieval as retrieval
from app.research_acquisition import SYSTEM as ACQUISITION_SYSTEM
from app.research_analysis import SYSTEM_PROMPT as ANALYSIS_SYSTEM
from app.research_planning import PLAN_SYSTEM_PROMPT
from app.research_verification import REVIEW_SYSTEM_PROMPT
from app.research_fetch import FetchFailure, FetchedText
from app.research_recovery import RecoveryJournal
from tests.research_prompt_helpers import decode_research_prompt

QUESTION='What changed in this report?'
TEXT='Revenue increased.'


class FakeSearch:
    name='offline-search'
    supports_project_search=True
    def __init__(self): self.calls=0
    def search(self, query, *, limit, deadline, cancel_event):
        self.calls+=1
        return [SimpleNamespace(title='Official report',url='https://www.bea.gov/example',excerpt='A lead only.',
                                publisher='Official publisher',published_at=date(2024,1,1),independently_reviewable=True)]


class FakeProvider:
    name='offline-acquisition'
    policy=SimpleNamespace(timeout_seconds=5,max_input_tokens=6000,max_output_tokens=1500)
    def __init__(self): self.events=[]; self.last_payload=None
    def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
        payload=decode_research_prompt(prompt)
        if system==PLAN_SYSTEM_PROMPT:
            self.events.append('plan')
            return {'subquestions':[{'question':QUESTION}], 'suggested_actions':[], 'retrieval_hints':[]},20
        if system==ACQUISITION_SYSTEM:
            self.last_payload=payload
            target=payload['target']['requirement_id']
            ranges=payload['ranges']; candidates=payload['candidates']
            lead=next((row for row in candidates if row['candidate_id'].startswith('lead:')),None)
            if not self.events.count('discover'):
                action={'action':'discover','query':'official change'}
            elif lead is not None:
                action={'action':'retrieve','candidate_ids':[lead['candidate_id']]}
            elif not self.events.count('analysis') and ranges:
                ref=ranges[0]
                action={'action':'read','source_id':ref['source_id'],'character_start':ref['character_start'],
                        'character_end':ref['character_end']}
            elif not self.events.count('review'):
                action={'action':'review'}
            else:
                allowed=payload['capabilities'][0]['allowed_actions']
                action=({'action':'next_page'} if any(payload['omitted'].values()) and payload['page']==0 else
                        {'action':'deliver' if 'deliver' in allowed else 'deliver_partial'})
            self.events.append(action['action'])
            return {'requirement_id':target,'expected_gap':'Evidence needed', 'result_update':'Record outcome',
                    'reason':'Relevant next step',**action},20
        if system==REVIEW_SYSTEM_PROMPT:
            self.events.append('review')
            return {'reviews':[{'claim_index':i,'verdict':'supports'} for i in range(len(payload['claims']))]},20
        if system==ANALYSIS_SYSTEM:
            self.events.append('analysis')
            assert payload.get('sources'), 'No original source reached analysis'
            source_id=payload['sources'][0]['source_id']
            return {'research_questions':[{'question':QUESTION,'claim_indexes':[0]}],
                    'claims':[{'text':TEXT,'citations':[{'source_id':source_id,'quotation':TEXT}]}]},20
        raise AssertionError('Unexpected local model request')


def setup(monkeypatch, *, fail_fetch=False):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE','mock')
    monkeypatch.setenv('FORESIGHTNINE_OFFLINE_TESTS','1')
    monkeypatch.setattr(api,'_reserve_model_call',lambda *a,**k:None)
    monkeypatch.setattr(api,'_settle_model_call',lambda *a,**k:None)
    provider, search=FakeProvider(), FakeSearch()
    monkeypatch.setattr(api,'challenge_provider',provider)
    monkeypatch.setattr(api,'evidence_provider',search)
    def fetch(request, *, timeout_seconds):
        if fail_fetch: raise FetchFailure('source_timeout')
        raw=b'<p>Revenue increased.</p>'
        return FetchedText(text=TEXT,response_sha256=sha256(raw).hexdigest(),content_type='text/html',
                           original_html_base64=base64.b64encode(raw).decode())
    monkeypatch.setattr(retrieval,'fetch_official_text',fetch)
    client=TestClient(api.app)
    reg=client.post('/v1/auth/local/register',json={'email':f'integration-{uuid4().hex}@example.test',
        'password':'correct horse battery staple!'})
    assert reg.status_code==201,reg.text
    headers={'Authorization':'Bearer '+reg.json()['access_token']}
    topic=client.post('/v2/research/topics',headers=headers,json={'question':QUESTION})
    assert topic.status_code==201,topic.text
    path='/v2/research/topics/'+topic.json()['topic_id']
    return client,headers,path,topic.json()['versions'][-1]['version_id'],provider,search


def start(client,headers,path,parent,key):
    response=client.post(path+'/runs',headers={**headers,'Idempotency-Key':key},json={
        'parent_version_id':parent,'plan_research':True,'discover_sources':True,'auto_fetch_official':True,
        'gap_followup':True,'gap_search':True,'support_review':True,'require_counterarguments':False,
        'max_model_calls':20,'max_total_tokens':200000,'max_search_calls':3,'max_elapsed_seconds':180})
    assert response.status_code==202,response.text
    return response.json()['operation']['operation_id']


def terminal(client,headers,path,op):
    for _ in range(500):
        response=client.get(path+'/runs/'+op,headers=headers)
        assert response.status_code==200,response.text
        status=response.json()['operation']['status']
        if status in {'completed','failed','cancelled'}: return status
        time.sleep(.01)
    pytest.fail('Timed out waiting for mocked operation')


def test_zero_upload_discovers_retrieves_reads_and_reviews(monkeypatch):
    client,headers,path,parent,provider,search=setup(monkeypatch)
    op=start(client,headers,path,parent,'acquisition-flow')
    assert terminal(client,headers,path,op)=='completed'
    assert search.calls==1
    assert provider.events[:7]==['plan','discover','retrieve','read','analysis','review','review']
    assert provider.events[-1]=='deliver' and 'next_page' in provider.events
    content=client.get(path,headers=headers).json()['versions'][-1]['content']
    assert content['claims'] and content['claims'][0]['citations'][0]['quotation']==TEXT
    assert content['claims'][0]['support_review']['verdict']=='supports'
    assert content['run']['acquisition_decisions'][-1]['action']=='deliver', json.dumps({'record':content['run']['acquisition_decisions'][-1], 'omitted':provider.last_payload['omitted'], 'target':provider.last_payload['target'], 'groups':{k:len(provider.last_payload[k]) for k in ('requirements','candidates','ranges','feedback')}}, ensure_ascii=False)


def test_unknown_fetch_stops_without_repeating(monkeypatch):
    client,headers,path,parent,provider,search=setup(monkeypatch,fail_fetch=True)
    op=start(client,headers,path,parent,'acquisition-unknown')
    assert terminal(client,headers,path,op)=='completed'
    assert provider.events==['plan','discover','retrieve'] and search.calls==1
    settlement=client.get(path+'/runs/'+op+'/settlement',headers=headers)
    assert settlement.status_code==200 and settlement.json()['unresolved_stage_count']>0


def test_preanalysis_discovery_checkpoint_resumes_without_duplicate_search(monkeypatch):
    client,headers,path,parent,provider,search=setup(monkeypatch)
    original=RecoveryJournal.commit_checkpoint
    interrupted=[]
    def interrupt(self,snapshot,*,committed_call_ids):
        original(self,snapshot,committed_call_ids=committed_call_ids)
        if 'source_discovery' in snapshot.completed_stages and snapshot.plan['generation_status']=='not_started' and not interrupted:
            interrupted.append(True)
            raise RuntimeError('offline interruption after saved discovery')
    monkeypatch.setattr(RecoveryJournal,'commit_checkpoint',interrupt)
    op=start(client,headers,path,parent,'acquisition-interrupt')
    assert terminal(client,headers,path,op)=='failed' and search.calls==1 and interrupted
    state=client.get(path+'/runs/'+op+'/recovery-status',headers=headers)
    assert state.status_code==200 and state.json()['can_continue'],state.text
    resumed=client.post(path+'/runs/'+op+'/continue',headers={**headers,'Idempotency-Key':'acquisition-continue'},json={})
    assert resumed.status_code==202,resumed.text
    assert terminal(client,headers,path,resumed.json()['operation']['operation_id'])=='completed'
    assert search.calls==1
    assert 'analysis' in provider.events and 'review' in provider.events


def test_negative_review_changes_next_decision_to_new_evidence(monkeypatch):
    client,headers,path,parent,_provider,search=setup(monkeypatch)
    class NegativeProvider(FakeProvider):
        def generate_structured(self,prompt,*,cancel_event,system,timeout_seconds):
            payload=decode_research_prompt(prompt)
            if system==REVIEW_SYSTEM_PROMPT:
                self.events.append('review')
                return {'reviews':[{'claim_index':i,'verdict':'contradicts'} for i in range(len(payload['claims']))]},20
            if system==ACQUISITION_SYSTEM and self.events.count('review') >= 2:
                issues=[issue for row in payload['feedback'] for issue in row.get('issues', [])]
                assert 'semantic_contradicts' in issues or 'unresolved_child_requirements' in issues, payload['feedback']
                self.events.append('followup_discover')
                return {'requirement_id':payload['target']['requirement_id'],'action':'discover',
                        'query':'new contradictory evidence','expected_gap':'Resolve contradiction',
                        'result_update':'Check independent source','reason':'Review contradicted the claim'},20
            return super().generate_structured(prompt,cancel_event=cancel_event,system=system,timeout_seconds=timeout_seconds)
    provider=NegativeProvider()
    monkeypatch.setattr(api,'challenge_provider',provider)
    # The next decision is observable even if the follow-up provider has no
    # additional leads; it must not reissue the original search query.
    queries=[]
    def search_local(query,*,limit,deadline,cancel_event):
        queries.append(query)
        if len(queries)>1: return []
        return [SimpleNamespace(title='Official report',url='https://www.bea.gov/example',excerpt='A lead only.',
                                publisher='Official publisher',published_at=date(2024,1,1),independently_reviewable=True)]
    monkeypatch.setattr(search,'search',search_local)
    op=start(client,headers,path,parent,'acquisition-review-replan')
    assert terminal(client,headers,path,op)=='completed'
    assert queries==['official change','new contradictory evidence']
    assert 'followup_discover' in provider.events


def test_invalid_acquisition_decision_is_checkpointed_then_repaired_before_read(monkeypatch):
    client, headers, _, _, _, _ = setup(monkeypatch)
    source = client.post('/v2/research/sources', headers=headers, json={'title': 'Report', 'text': TEXT,
        'permissions': {'store': True, 'display': True, 'model_input': True, 'export': True}})
    assert source.status_code == 201
    topic = client.post('/v2/research/topics', headers=headers, json={'question': QUESTION,
        'source_ids': [source.json()['source_id']]})
    assert topic.status_code == 201
    path = '/v2/research/topics/' + topic.json()['topic_id']
    class Provider:
        name = 'local-decision-repair'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=6000, max_output_tokens=1500)
        def __init__(self): self.events = []
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            payload = decode_research_prompt(prompt)
            if system == ACQUISITION_SYSTEM:
                self.events.append('decision')
                if self.events.count('decision') == 1: return {}, 20
                if 'analysis' not in self.events:
                    unit = payload['ranges'][0]
                    return {'requirement_id': payload['target']['requirement_id'], 'expected_gap': 'Evidence needed',
                        'result_update': 'Read source', 'reason': 'Relevant', 'action': 'read',
                        'source_id': unit['source_id'], 'character_start': unit['character_start'],
                        'character_end': unit['character_end']}, 20
                return {'requirement_id': payload['target']['requirement_id'], 'expected_gap': 'None',
                        'result_update': 'Stop', 'reason': 'Partial', 'action': 'deliver_partial'}, 20
            if system == ANALYSIS_SYSTEM:
                self.events.append('analysis')
                return {'claims': [{'text': TEXT, 'citations': [{'source_id': payload['sources'][0]['source_id'],
                    'quotation': TEXT}]}]}, 20
            raise AssertionError('Unexpected model stage')
    provider = Provider()
    monkeypatch.setattr(api, 'challenge_provider', provider)
    saved = []
    original = RecoveryJournal.commit_checkpoint
    def capture(self, snapshot, *, committed_call_ids):
        original(self, snapshot, committed_call_ids=committed_call_ids)
        if snapshot.plan.get('acquisition_continuation'):
            saved.append(snapshot.model_dump(mode='json'))
            raise RuntimeError('simulated process interruption after committed decision')
    monkeypatch.setattr(RecoveryJournal, 'commit_checkpoint', capture)
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'decision-repair'}, json={
        'parent_version_id': topic.json()['versions'][-1]['version_id'], 'evidence_strategy': 'delegated',
        'plan_research': False, 'discover_sources': False, 'auto_fetch_official': False,
        'counterpoint_followup': False, 'gap_followup': False, 'support_review': False,
        'max_model_calls': 4, 'max_search_calls': 0, 'max_elapsed_seconds': 180})
    assert response.status_code == 202, response.text
    operation = response.json()['operation']['operation_id']
    assert terminal(client, headers, path, operation) == 'failed'
    assert provider.events == ['decision'] and len(saved) == 1
    monkeypatch.setattr(RecoveryJournal, 'commit_checkpoint', original)
    resumed = client.post(f'{path}/runs/{operation}/continue', headers={**headers, 'Idempotency-Key': 'decision-resume'}, json={})
    assert resumed.status_code == 202, resumed.text
    assert terminal(client, headers, path, resumed.json()['operation']['operation_id']) == 'completed'
    assert provider.events[:3] == ['decision', 'decision', 'analysis']


@pytest.mark.parametrize('kind', ['unknown', 'refusal'])
def test_acquisition_unknown_and_refusal_do_not_replay(monkeypatch, kind):
    from app.phase3 import ProviderFailure
    client, headers, path, parent, _, _ = setup(monkeypatch)
    class Provider:
        name = 'local-decision-failure'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=6000, max_output_tokens=1500)
        def __init__(self): self.calls = 0
        def generate_structured(self, prompt, *, cancel_event, system, timeout_seconds):
            self.calls += 1
            error = ProviderFailure('provider_timeout' if kind == 'unknown' else 'provider_output_refused', 'safe',
                                    retryable=False, reported_tokens=None if kind == 'unknown' else 10)
            if kind == 'refusal': error.provider_response_received = True
            raise error
    provider = Provider()
    monkeypatch.setattr(api, 'challenge_provider', provider)
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': 'decision-' + kind}, json={
        'parent_version_id': parent, 'evidence_strategy': 'delegated', 'plan_research': False,
        'max_model_calls': 3, 'max_search_calls': 0, 'max_elapsed_seconds': 180})
    assert response.status_code == 202
    assert terminal(client, headers, path, response.json()['operation']['operation_id']) == 'completed'
    assert provider.calls == 1
