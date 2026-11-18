"""Automatic research admission with explicit optional limits; no network calls."""
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as api
from app.research_runs import ResearchRunRequest, resolve_automatic_research_request


@pytest.mark.parametrize('payload,available,expected', [
    ({}, True, (None, None, None, None, True, True, True, True, True)),
    ({}, False, (None, None, None, None, True, True, True, False, False)),
    ({'max_model_calls':1,'max_search_calls':0}, True, (1, 0, None, None, False, False, False, False, False)),
    ({'max_model_calls':0,'max_total_tokens':0,'max_elapsed_seconds':5,'plan_research':False,
      'gap_followup':False,'support_review':False,'gap_search':False,'auto_fetch_official':False}, True,
     (0, None, 0, 5, False, False, False, False, False)),
    ({'selected_source_ids':[]}, True, (None, None, None, None, True, True, True, False, False)),
    ({'gap_followup':False}, True, (None, None, None, None, True, False, True, False, True)),
])
def test_automatic_defaults_respect_explicit_values_and_bounds(payload, available, expected):
    original = ResearchRunRequest(parent_version_id='v1', **payload)
    result = resolve_automatic_research_request(original, search_available=available)
    assert (result.max_model_calls, result.max_search_calls, result.max_total_tokens, result.max_elapsed_seconds,
            result.plan_research, result.gap_followup, result.support_review, result.gap_search,
            result.auto_fetch_official) == expected
    for key, value in payload.items():
        assert getattr(result, key) == value
    for key in ('max_model_calls', 'max_search_calls', 'max_total_tokens', 'max_elapsed_seconds'):
        if key not in payload:
            assert getattr(result, key) is None


def test_automatic_admission_persists_effective_policy_and_replays_without_search(monkeypatch):
    monkeypatch.setattr(api, 'evidence_provider', SimpleNamespace(supports_project_search=False))
    client = TestClient(api.app)
    registered = client.post('/v1/auth/local/register', json={'email':f'auto-{uuid4().hex}@example.test',
        'password':'correct horse battery staple!'})
    assert registered.status_code == 201, registered.text
    headers={'Authorization':'Bearer '+registered.json()['access_token']}
    topic=client.post('/v2/research/topics',headers=headers,json={'question':'What changed in the published report?'})
    assert topic.status_code == 201, topic.text
    data=topic.json(); path=f"/v2/research/topics/{data['topic_id']}"; key='automatic-policy'
    payload={'parent_version_id':data['versions'][0]['version_id']}
    response=client.post(path+'/runs',headers={**headers,'Idempotency-Key':key},json=payload)
    assert response.status_code == 202,response.text
    operation=response.json()['operation']['operation_id']
    captured=client.get(path+'/runs/'+operation+'/inputs',headers=headers)
    assert captured.status_code == 200,captured.text
    effective=captured.json()['budget']
    assert all(effective.get(key) is None for key in ('max_model_calls','max_search_calls','max_total_tokens','max_elapsed_seconds'))
    assert effective['plan_research'] and effective['gap_followup'] and effective['support_review']
    assert not effective['gap_search'] and not effective['auto_fetch_official']
    replay=client.post(path+'/runs',headers={**headers,'Idempotency-Key':key},json=payload)
    assert replay.status_code==202 and replay.headers['Idempotency-Replayed']=='true'
    assert replay.json()['operation']['operation_id']==operation
    for _ in range(500):
        current=client.get(path+'/runs/'+operation,headers=headers).json()['operation']['status']
        if current in ('completed','failed','cancelled'): break
        time.sleep(.01)
    assert current=='completed',current
    run=client.get(path,headers=headers).json()['versions'][-1]['content']['run']
    assert run['status']=='partial' and run['search_status']=='unavailable'
    assert run['model_calls']==0


def test_automatic_question_only_discovers_and_fetches_allowed_source_without_model(monkeypatch):
    import base64
    from hashlib import sha256
    from app.glm_mcp_search import SearchCandidate
    from app.research_fetch import FetchedText
    import app.research_official_retrieval as retrieval
    observed = []
    class Search:
        name = 'local-search'
        supports_project_search = True
        def search(self, query, *, limit, deadline, cancel_event):
            observed.append(('search', query))
            return [SearchCandidate(title='Restricted FRED',url='https://fred.stlouisfed.org/series/X',
                    excerpt='lead',publisher='FRED',published_at=None,independently_reviewable=True),
                    SearchCandidate(title='Official',url='https://www.bea.gov/news/report',
                    excerpt='lead',publisher='BEA',published_at=None,independently_reviewable=True)]
    def fetch(request, *, timeout_seconds):
        observed.append(('fetch', str(request.url)))
        assert str(request.url)=='https://www.bea.gov/news/report'
        assert request.permissions.model_input and request.permissions.store
        raw=b'<p>Revenue was 100 million.</p>'
        return FetchedText(text='Revenue was 100 million.',response_sha256=sha256(raw).hexdigest(),
                           content_type='text/html',original_html_base64=base64.b64encode(raw).decode())
    monkeypatch.setattr(api,'evidence_provider',Search())
    monkeypatch.setattr(retrieval,'fetch_official_text',fetch)
    monkeypatch.setattr(api,'_reserve_search_call',lambda request:None)
    client=TestClient(api.app)
    registered=client.post('/v1/auth/local/register',json={'email':f'discover-{uuid4().hex}@example.test',
        'password':'correct horse battery staple!'})
    assert registered.status_code==201, registered.text
    headers={'Authorization':'Bearer '+registered.json()['access_token']}
    topic=client.post('/v2/research/topics',headers=headers,json={'question':'Find official revenue.'}).json()
    path='/v2/research/topics/'+topic['topic_id']
    started=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'automatic-official'},
        json={'parent_version_id':topic['versions'][0]['version_id'],'max_model_calls':0})
    assert started.status_code==202,started.text
    op=started.json()['operation']['operation_id']
    for _ in range(500):
        status=client.get(path+'/runs/'+op,headers=headers).json()['operation']['status']
        if status in ('completed','failed','cancelled'): break
        time.sleep(.01)
    assert status=='completed',status
    version=client.get(path,headers=headers).json()['versions'][-1]
    assert version['content']['run']['official_fetch_calls']==1
    assert version['content']['run']['model_calls']==0
    assert version['content']['run']['status']=='partial'
    assert observed==[('search','Find official revenue.'),('fetch','https://www.bea.gov/news/report')]


def test_explicit_empty_selection_blocks_implicit_search_but_keeps_explicit_budget(monkeypatch):
    class Search:
        supports_project_search = True
        def search(self, *args, **kwargs):
            raise AssertionError('implicit external discovery must not start')
    monkeypatch.setattr(api, 'evidence_provider', Search())
    client=TestClient(api.app)
    reg=client.post('/v1/auth/local/register',json={'email':f'empty-{uuid4().hex}@example.test',
        'password':'correct horse battery staple!'})
    headers={'Authorization':'Bearer '+reg.json()['access_token']}
    topic=client.post('/v2/research/topics',headers=headers,json={'question':'Explain this issue.'}).json()
    path='/v2/research/topics/'+topic['topic_id']
    resp=client.post(path+'/runs',headers={**headers,'Idempotency-Key':'empty-selection'},json={
        'parent_version_id':topic['versions'][0]['version_id'],'selected_source_ids':[],'max_model_calls':0})
    assert resp.status_code==202,resp.text
    op=resp.json()['operation']['operation_id']
    captured=client.get(path+'/runs/'+op+'/inputs',headers=headers).json()['budget']
    assert captured.get('max_search_calls') is None and not captured['discover_sources'] and not captured['auto_fetch_official']
    for _ in range(500):
        status=client.get(path+'/runs/'+op,headers=headers).json()['operation']['status']
        if status in ('completed','failed','cancelled'): break
        time.sleep(.01)
    assert status=='completed'
    manual=resolve_automatic_research_request(ResearchRunRequest(parent_version_id='v1',
        selected_source_ids=[],max_search_calls=1),search_available=True)
    assert manual.max_search_calls==1 and not manual.auto_fetch_official


def test_explicit_gap_search_resolves_missing_dependency_but_rejects_conflict():
    from pydantic import ValidationError
    incoming=ResearchRunRequest(parent_version_id='v1',gap_search=True)
    resolved=resolve_automatic_research_request(incoming,search_available=True)
    assert resolved.gap_search and resolved.gap_followup
    with pytest.raises(ValidationError):
        ResearchRunRequest(parent_version_id='v1',gap_search=True,gap_followup=False)
    with pytest.raises(ValidationError):
        ResearchRunRequest(parent_version_id='v1',gap_search=True,max_search_calls=0)
