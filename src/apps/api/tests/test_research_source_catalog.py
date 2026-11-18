from datetime import datetime, timezone
from hashlib import sha256
from threading import Event
from types import SimpleNamespace

from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext
from app.research_discovery import collect_source_candidates, execute_source_search
from app.research_fetch import FetchFailure, FetchedText
from app.research_official_retrieval import execute_official_retrieval
from app.research_runs import ResearchRunRequest
from app.research_source_catalog import SourceRouteRequest, canonical_source_url, route_source_capabilities
from app.research_sources import ResearchSource, SourcePermissions


def context():
    return ResearchExecutionContext(ResearchExecutionBudget(None, None, None, float('inf')), False,
                                    lambda: None, Event, lambda *_: None, lambda *_: None, lambda: None)


def candidate(url):
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return SimpleNamespace(url=url, title='Document', published_at=None, retrieved_at=now, search_result_hash='hash')


def test_catalog_routes_without_inventing_market_or_permissions():
    available = route_source_capabilities(SourceRouteRequest(material_type='document', available_only=True), sec_available=False)
    assert [item.name for item in available] == ['official_html_pdf']
    assert not route_source_capabilities(SourceRouteRequest(permission='unknown'), search_available=True, sec_available=True)
    assert canonical_source_url('https://www.bea.gov/a?id=1#top') == 'https://www.bea.gov/a?id=1'


def test_discovery_keeps_query_and_full_batch_without_asserting_exhaustion():
    rows = [candidate(f'https://example.com/a?id={i}#part') for i in range(8)]
    result = collect_source_candidates(rows, provider_name='test', convert=lambda x, _: x, as_of=None, capacity=10)
    assert len(result.candidates) == 8
    assert result.candidates[0]['url'].endswith('?id=0')
    class Provider:
        supports_project_search = True
        def search(self, query, *, limit, deadline, cancel_event):
            assert len(query) == 120 and limit == 10
            return rows
    result = execute_source_search(provider=Provider(), provider_name='test', query='q'*120,
                                   convert=lambda x, _: x, as_of=None, context=context())
    assert len(result.candidates) == 8 and result.returned_count == 8 and result.has_more is None


def test_retrieval_isolates_failure_and_preserves_versions(monkeypatch):
    import app.research_official_retrieval as module
    def fetch(request, *, timeout_seconds):
        if str(request.url).endswith('bad'):
            raise FetchFailure('fetch_failed')
        raw = b'new response'
        return FetchedText(text='new response', response_sha256=sha256(raw).hexdigest(),
                           content_type='text/html', original_html_base64='bmV3')
    monkeypatch.setattr(module, 'fetch_official_text', fetch)
    permissions = SourcePermissions(store=True, display=True, model_input=True, export=True)
    old = ResearchSource(source_id='old', owner_id='o', title='Old', text='old response',
                         url='https://www.bea.gov/a?id=1', kind='official', permissions=permissions,
                         obtained_at=datetime.now(timezone.utc), content_sha256=sha256(b'old response').hexdigest(),
                         response_sha256=sha256(b'old response').hexdigest())
    class Store:
        def create(self, owner, data, **kw):
            return ResearchSource(source_id=kw['source_id'], owner_id=owner, obtained_at=datetime.now(timezone.utc),
                                  content_sha256=sha256(data.text.encode()).hexdigest(),
                                  response_sha256=kw['response_sha256'], **data.model_dump())
    rows = [{'url':'https://www.bea.gov/bad','title':'Bad'},
            {'url':'https://www.bea.gov/a?id=1','title':'New'},
            {'url':'https://www.bea.gov/a?id=1#top','title':'Same'},
            {'url':'https://www.bea.gov/a?id=2','title':'Different query'}]
    result = execute_official_retrieval(request=ResearchRunRequest(parent_version_id='v', auto_fetch_official=True),
        question='research', as_of=None, available_count=1, candidates=rows, sources=[old], owner_id='o',
        operation_id='op', batch_id='gap-1', store=Store(), sec_agent_config=None, reserve_sec=lambda:True, context=context())
    assert result.status == 'partial'
    assert [x['status'] for x in result.outcomes] == ['failed', 'retrieved', 'duplicate', 'retrieved']
    assert [x['external_state'] for x in result.outcomes] == ['returned', 'returned', 'returned', 'returned']
    assert result.retrieved[0][0].source_id != old.source_id
    assert result.retrieved[1][0].source_id == result.retrieved[0][0].source_id
    assert result.retrieved[2][0].source_id != result.retrieved[0][0].source_id


def test_search_defers_over_capacity_without_treating_it_as_invalid():
    rows = [candidate(f'https://example.com/a?n={i}') for i in range(4)]
    batch = collect_source_candidates(rows, provider_name='test', convert=lambda x, _: x, as_of=None, capacity=2)
    assert len(batch.candidates) == len(batch.deferred_candidates) == 2
    assert batch.omitted == batch.invalid_count == 0


def test_sec_structured_bridge_requires_permission_and_verified_identity_and_period(monkeypatch):
    import json
    import app.research_structured_connector as bridge
    from app.research_sec import SecResult
    query = bridge.StructuredQuery(source_kind='sec_company_concept', parameters={
        'entity_query':'EXAMPLE', 'taxonomy':'us-gaap', 'tag':'Revenue', 'unit':'USD',
        'period_start':'2023-01-01', 'period_end':'2023-12-31'})
    calls = []
    def identity(request, agent):
        calls.append('identity')
        return {'candidates':[{'cik':'0000000001','ticker':'EXAMPLE','name':'Example Inc','exchange':None}],
                'source_url':'https://www.sec.gov/files/company_tickers.json',
                'retrieved_at':'2024-01-01T00:00:00Z','response_sha256':'identity-hash'}
    def concept(request, agent):
        calls.append('concept')
        return SecResult(url='https://data.sec.gov/api/xbrl/companyconcept/CIK0000000001/us-gaap/Revenue.json',
                         title='Example revenue', response_sha256='response-hash',
                         text='SEC data\n'+json.dumps({'val':1,'unit':'USD','start':'2023-01-01','end':'2023-12-31','filed':'2024-02-01'}))
    monkeypatch.setattr(bridge, 'search_sec_identities', identity)
    monkeypatch.setattr(bridge, 'fetch_sec_concept', concept)
    class Store:
        def create(self, owner, data, **kw):
            return ResearchSource(source_id=kw['source_id'], owner_id=owner, obtained_at=datetime.now(timezone.utc),
                content_sha256=sha256(data.text.encode()).hexdigest(), response_sha256=kw['response_sha256'],
                retrieval_method=kw['retrieval_method'], provenance=kw['provenance'], **data.model_dump())
    kwargs = dict(owner_id='o',operation_id='op',store=Store(),sec_agent_config='agent test@example.com',
                  reserve_sec=lambda:True,context=context())
    denied = bridge.execute_structured_query(query, permitted=False, **kwargs)
    assert denied.status == 'restricted' and denied.external_state == 'not_started'
    assert calls == []
    result = bridge.execute_structured_query(query, permitted=True, **kwargs)
    assert result.status == 'completed' and result.external_state == 'returned' and calls == ['identity','concept']
    assert result.source.provenance['semantic_status'] == 'not_verified'
    assert result.source.published_at is None
    assert result.source.provenance['identity_receipt']['response_sha256'] == 'identity-hash'
    unmatched = query.model_copy(update={'parameters':{**query.parameters,'period_end':'2022-12-31'}})
    assert bridge.execute_structured_query(unmatched, permitted=True, **kwargs).reason == 'incomplete_or_invalid_query'
    monkeypatch.setattr(bridge, 'search_sec_identities', lambda *_: {'candidates':[
        {'cik':'1','ticker':'EXAMPLE','name':'One'}, {'cik':'2','ticker':'EXAMPLE','name':'Two'}]})
    assert bridge.execute_structured_query(query, permitted=True, **kwargs).reason == 'identity_not_unique'


def test_structured_timeout_is_unknown_and_invalid_response_returned(monkeypatch):
    import app.research_structured_connector as bridge
    from app.research_sec import SecFailure
    query=bridge.StructuredQuery(source_kind='sec_company_concept',parameters={'entity_query':'A',
        'taxonomy':'us-gaap','tag':'Revenue','unit':'USD','period_start':'2023-01-01','period_end':'2023-12-31'})
    kwargs=dict(owner_id='o',operation_id='op',store=None,sec_agent_config='agent test@example.com',
                reserve_sec=lambda:True,context=context(),permitted=True)
    def fail(code):
        def invoke(*_): raise SecFailure(code)
        return invoke
    monkeypatch.setattr(bridge,'search_sec_identities',fail('source_timeout'))
    assert bridge.execute_structured_query(query,**kwargs).external_state == 'unknown'
    monkeypatch.setattr(bridge,'search_sec_identities',fail('source_invalid'))
    assert bridge.execute_structured_query(query,**kwargs).external_state == 'returned'
