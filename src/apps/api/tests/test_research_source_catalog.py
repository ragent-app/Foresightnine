import base64
from datetime import datetime, timezone
from hashlib import sha256
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as api
import app.research_fetch as research_fetch
import app.research_official_retrieval as official_retrieval
from app.openai_compatible import BinaryHttpResponse
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext
from app.research_discovery import collect_source_candidates, execute_source_search
from app.research_fetch import FetchFailure, FetchedDocument, FetchedText
from app.research_official_retrieval import execute_official_retrieval
from app.research_runs import ResearchRunRequest
from app.research_source_catalog import (
    SourceRouteRequest,
    canonical_source_url,
    official_document_capability,
    route_source_capabilities,
    source_capabilities,
)
from app.research_sources import ResearchSource, ResearchSources, SourcePermissions
from app.storage import InMemoryDocumentRepository


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


def test_open_public_https_catalog_and_policy_filters(monkeypatch):
    monkeypatch.delenv('FORESIGHTNINE_FETCH_ALLOWED_HOSTS', raising=False)
    monkeypatch.delenv('FORESIGHTNINE_FETCH_BLOCKED_HOSTS', raising=False)
    caps = {item.name: item for item in source_capabilities(search_available=False, sec_available=False)}
    assert caps['official_html_pdf'].configured is True
    assert caps['official_html_pdf'].hosts == ()
    assert caps['official_html_pdf'].coverage == 'open_public_https'
    assert caps['sec_filing'].hosts == ('www.sec.gov',)
    assert caps['sec_company_concept'].hosts == ('data.sec.gov',)

    assert official_document_capability('https://ir.nvidia.com/news/release', sec_available=False).name == 'official_html_pdf'
    assert official_document_capability('https://www.sec.gov/Archives/edgar/data/1/000000000000000001/a.htm', sec_available=False).name == 'sec_filing'
    assert official_document_capability('https://fred.stlouisfed.org/series/GDP', sec_available=True) is None
    assert official_document_capability('https://127.0.0.1/admin', sec_available=True) is None
    assert official_document_capability('http://ir.nvidia.com/news', sec_available=True) is None

    monkeypatch.setenv('FORESIGHTNINE_FETCH_BLOCKED_HOSTS', 'blocked.example.com')
    assert official_document_capability('https://sub.blocked.example.com/a', sec_available=True) is None
    monkeypatch.delenv('FORESIGHTNINE_FETCH_BLOCKED_HOSTS', raising=False)

    monkeypatch.setenv('FORESIGHTNINE_FETCH_ALLOWED_HOSTS', 'www.bea.gov,ir.nvidia.com')
    restricted_caps = {item.name: item for item in source_capabilities(sec_available=True)}
    assert restricted_caps['official_html_pdf'].coverage == 'restricted_hosts'
    assert restricted_caps['official_html_pdf'].hosts == ('ir.nvidia.com', 'www.bea.gov')
    assert official_document_capability('https://ir.nvidia.com/q4', sec_available=True) is not None
    assert official_document_capability('https://other.example.com/q4', sec_available=True) is None


def test_open_web_retrieval_upgrades_http_sets_export_false_and_preserves_official_and_sec_isolation(monkeypatch):
    monkeypatch.delenv('FORESIGHTNINE_FETCH_ALLOWED_HOSTS', raising=False)
    monkeypatch.delenv('FORESIGHTNINE_FETCH_BLOCKED_HOSTS', raising=False)
    store = ResearchSources(InMemoryDocumentRepository())
    pdf_bytes = b'%PDF-1.4\nfixture'
    html_bytes = b'<html><p>NVIDIA Q4 revenue 22.1 billion</p></html>'

    def fake_transport(method, url, headers, body, timeout, **kwargs):
        if url == 'https://ir.nvidia.com/news/q4':
            return BinaryHttpResponse(200, html_bytes, {'content-type': 'text/html; charset=utf-8'})
        if url == 'https://ir.nvidia.com/download?id=99':
            return BinaryHttpResponse(200, b'', {'content-type': 'text/plain'})
        if url == 'https://www.bea.gov/redirect-out':
            return BinaryHttpResponse(302, b'', {'location': 'https://cdn.example.org/mirror.html'})
        if url == 'https://cdn.example.org/mirror.html':
            return BinaryHttpResponse(200, b'<html><p>Mirrored text</p></html>', {'content-type': 'text/html'})
        if url == 'https://www.bea.gov/official':
            return BinaryHttpResponse(200, b'<html><p>Official BEA release</p></html>', {'content-type': 'text/html'})
        if url == 'https://blocked.example.com/403':
            return BinaryHttpResponse(403, b'', {})
        raise AssertionError(f'Unexpected transport URL: {url}')

    # Mock fetch_source_document cleanly for PDF without pdftotext while driving real _default_binary_transport for HTML/redirect/403.
    real_fetch_doc = research_fetch.fetch_source_document

    def patched_fetch_doc(req, transport=None, *, timeout_seconds=15.0):
        if str(req.url) == 'https://ir.nvidia.com/download?id=99':
            return FetchedDocument(
                text='NVIDIA 10-K summary',
                response_sha256=sha256(pdf_bytes).hexdigest(),
                content_type='application/pdf',
                final_url='https://files.nvidia.com/10k-file',
                original_pdf_base64=base64.b64encode(pdf_bytes).decode('ascii'),
                redirect_count=1,
            )
        return real_fetch_doc(req, transport=transport, timeout_seconds=timeout_seconds)

    dns_checked = []
    monkeypatch.setattr(
        research_fetch,
        '_validated_addresses',
        lambda host, port, *, allow_private=False: dns_checked.append((host, port, allow_private)) or ('93.184.216.34',),
    )
    monkeypatch.setattr(research_fetch, '_default_binary_transport', fake_transport)
    monkeypatch.setattr(official_retrieval, 'fetch_source_document', patched_fetch_doc)

    candidates = [
        {'url': 'https://www.sec.gov/Archives/edgar/data/1045810/000104581024000029/nvda-20240128.htm', 'title': 'SEC 10-K'},
        {'url': 'http://ir.nvidia.com/news/q4', 'title': 'NVIDIA Q4'},
        {'url': 'https://ir.nvidia.com/download?id=99', 'title': 'NVIDIA PDF No Suffix'},
        {'url': 'https://www.bea.gov/official', 'title': 'BEA Official'},
        {'url': 'https://www.bea.gov/redirect-out', 'title': 'BEA Redirected External'},
        {'url': 'https://fred.stlouisfed.org/series/GDP', 'title': 'FRED'},
        {'url': 'https://blocked.example.com/403', 'title': 'WAF 403'},
    ]
    result = execute_official_retrieval(
        request=ResearchRunRequest(parent_version_id='v1', auto_fetch_official=True),
        question='Compare revenue',
        as_of=None,
        available_count=0,
        candidates=candidates,
        sources=[],
        owner_id='owner-1',
        operation_id='op-open-1',
        store=store,
        sec_agent_config=None,
        reserve_sec=lambda: True,
        context=context(),
    )
    assert dns_checked == [('cdn.example.org', 443, False)]
    assert result.status == 'partial'
    assert [row['status'] for row in result.outcomes] == [
        'unconfigured', 'retrieved', 'retrieved', 'retrieved', 'retrieved', 'restricted', 'failed'
    ]
    assert result.outcomes[0]['reason'] == 'sec_contact_unconfigured'
    assert result.outcomes[5]['reason'] == 'source_blocked_policy'
    assert result.outcomes[6]['reason'] == 'http_forbidden_or_challenge'
    assert result.outcomes[6]['external_state'] == 'returned'

    web_html, web_pdf, official_html, redirected_web = [item[0] for item in result.retrieved]
    assert web_html.url and str(web_html.url) == 'https://ir.nvidia.com/news/q4'
    assert web_html.kind == 'user_supplied'
    assert web_html.retrieval_method == 'web_html'
    assert web_html.permissions == SourcePermissions(store=True, display=True, model_input=True, export=False)
    assert web_html.provenance['final_url'] == 'https://ir.nvidia.com/news/q4'
    assert web_html.provenance['web_retrieval']['content_type'] == 'text/html'

    assert web_pdf.kind == 'user_supplied'
    assert web_pdf.retrieval_method == 'web_pdf'
    assert web_pdf.permissions == SourcePermissions(store=True, display=True, model_input=True, export=False)
    assert web_pdf.provenance['final_url'] == 'https://files.nvidia.com/10k-file'
    assert web_pdf.provenance['web_retrieval']['redirect_count'] == 1

    assert official_html.kind == 'official'
    assert official_html.retrieval_method == 'official_html'
    assert official_html.permissions == SourcePermissions(store=True, display=True, model_input=True, export=True)

    # An official host redirecting onto an external third-party domain is downgraded to web_html with export=False.
    assert redirected_web.kind == 'user_supplied'
    assert redirected_web.retrieval_method == 'web_html'
    assert redirected_web.permissions.export is False
    assert redirected_web.provenance['final_url'] == 'https://cdn.example.org/mirror.html'


def test_open_web_export_false_blocks_full_export_and_original_download(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    client = TestClient(api.app)
    reg = client.post('/v1/auth/local/register', json={
        'email': f'webexport@example.test',
        'password': 'correct horse battery staple!',
    }).json()
    headers = {'Authorization': 'Bearer ' + reg['access_token']}
    account_id = client.get('/v1/account', headers=headers).json()['account']['account_id']

    raw = b'<html><p>Web revenue 50 billion.</p></html>'
    monkeypatch.setattr(
        official_retrieval,
        'fetch_source_document',
        lambda req, **kw: FetchedDocument(
            text='Web revenue 50 billion.',
            response_sha256=sha256(raw).hexdigest(),
            content_type='text/html',
            final_url=str(req.url),
            original_html_base64=base64.b64encode(raw).decode('ascii'),
        ),
    )
    retrieved = execute_official_retrieval(
        request=ResearchRunRequest(parent_version_id='v1', auto_fetch_official=True),
        question='Check web source',
        as_of=None,
        available_count=0,
        candidates=[{'url': 'https://ir.example.com/q4', 'title': 'Web Release'}],
        sources=[],
        owner_id=account_id,
        operation_id='op-export-check',
        store=api.research_sources,
        sec_agent_config=None,
        reserve_sec=lambda: True,
        context=context(),
    )
    assert retrieved.status == 'completed'
    source_id = retrieved.source.source_id
    assert client.get(f'/v2/research/sources/{source_id}', headers=headers).status_code == 200
    assert client.get(f'/v2/research/sources/{source_id}/original', headers=headers).status_code == 403
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'Web?', 'source_ids': [source_id]}).json()
    version_id = topic['versions'][-1]['version_id']
    assert client.get(f"/v2/research/topics/{topic['topic_id']}/versions/{version_id}/export", headers=headers).status_code == 403
    assert client.get(f"/v2/research/topics/{topic['topic_id']}/versions/{version_id}/export/metadata", headers=headers).status_code == 200


@pytest.mark.parametrize("sec_agent_config,expected_status,expected_reason", [
    (None, "unconfigured", "sec_contact_unconfigured"),
    ("Example Research contact@example.test", "restricted", "source_unavailable"),
])
def test_non_filing_sec_hosts_fail_closed_without_open_web_fallback(
    monkeypatch, sec_agent_config, expected_status, expected_reason
):
    monkeypatch.delenv('FORESIGHTNINE_FETCH_ALLOWED_HOSTS', raising=False)
    monkeypatch.delenv('FORESIGHTNINE_FETCH_BLOCKED_HOSTS', raising=False)
    assert official_document_capability('https://efts.sec.gov/LATEST/search-index', sec_available=True) is None
    assert official_document_capability('https://sec.gov./Archives/edgar/data/1/000000000000000001/a.htm', sec_available=True) is None
    assert official_document_capability('https://data.sec.gov/api/xbrl/companyconcept/CIK0000789019/us-gaap/Revenues.json', sec_available=True) is None

    store = ResearchSources(InMemoryDocumentRepository())
    fetched_urls = []
    monkeypatch.setattr(
        official_retrieval,
        'fetch_source_document',
        lambda req, **kw: fetched_urls.append(str(req.url)) or FetchedDocument(
            text='Open web text',
            response_sha256=sha256(b'Open web text').hexdigest(),
            content_type='text/html',
            final_url=str(req.url),
            original_html_base64=base64.b64encode(b'Open web text').decode('ascii'),
        ),
    )
    candidates = [
        {'url': 'https://efts.sec.gov/LATEST/search-index', 'title': 'SEC EFTS'},
        {'url': 'https://sec.gov./Archives/edgar/data/1/000000000000000001/a.htm', 'title': 'SEC Apex'},
        {'url': 'https://ir.example.com/news/q4', 'title': 'Open Web'},
    ]
    result = execute_official_retrieval(
        request=ResearchRunRequest(parent_version_id='v1', auto_fetch_official=True),
        question='Check SEC isolation',
        as_of=None,
        available_count=0,
        candidates=candidates,
        sources=[],
        owner_id='owner-sec',
        operation_id='op-sec-isolation',
        store=store,
        sec_agent_config=sec_agent_config,
        reserve_sec=lambda: True,
        context=context(),
    )
    assert result.status == 'completed'
    assert fetched_urls == ['https://ir.example.com/news/q4']
    assert [row['status'] for row in result.outcomes[:2]] == [expected_status, expected_status]
    assert [row['reason'] for row in result.outcomes[:2]] == [expected_reason, expected_reason]
    assert result.outcomes[2]['status'] == 'retrieved'



def _structured_fixture(monkeypatch, rows=None, failure=None):
    import json
    import app.research_structured_connector as bridge
    from app.research_sec import SecResult
    query = bridge.StructuredQuery(source_kind='sec_company_concept', parameters={
        'entity_query': 'EXAMPLE', 'taxonomy': 'us-gaap', 'tag': 'Revenue', 'unit': 'USD',
        'period_start': '2023-01-01', 'period_end': '2023-12-31'})
    monkeypatch.setattr(bridge, 'search_sec_identities', lambda *_: {'candidates': [
        {'cik': '0000000001', 'ticker': 'EXAMPLE', 'name': 'Example Inc', 'exchange': None}],
        'source_url': 'u', 'retrieved_at': 't', 'response_sha256': 'h'})
    def concept(request, agent):
        if failure:
            raise failure
        return SecResult(url='u', title='t', response_sha256='r',
                         text='SEC data\n' + '\n'.join(json.dumps(r) for r in rows))
    monkeypatch.setattr(bridge, 'fetch_sec_concept', concept)
    created = []
    class Store:
        def create(self, *a, **k):
            created.append(1)
            raise AssertionError('must not persist')
    kwargs = dict(owner_id='o', operation_id='op', store=Store(), sec_agent_config='agent test@example.com',
                  reserve_sec=lambda: True, context=context(), permitted=True)
    return bridge, query, kwargs, created


def test_sec_period_mismatch_lists_available_periods(monkeypatch):
    rows = [{'val': i, 'unit': 'USD', 'start': f'20{10+i}-01-01', 'end': f'20{10+i}-12-31', 'fy': 2010 + i,
             'fp': 'FY', 'form': '10-K', 'filed': f'20{11+i}-02-01'} for i in range(13)]
    rows.append({'val': 1, 'unit': 'USD', 'start': '2023-02-01', 'end': '2024-01-31', 'filed': '2024-03-01'})
    bridge, query, kwargs, created = _structured_fixture(monkeypatch, rows)
    result = bridge.execute_structured_query(query, **kwargs)
    assert result.reason == 'unit_or_period_not_found_in_returned_records', result.reason
    assert result.status == 'unknown'
    assert len(result.available_periods) == 12 and not created
    assert result.available_periods[0]['end'] == '2024-01-31'
    assert 'fy' not in result.available_periods[0]
    ends = [p['end'] for p in result.available_periods]
    assert ends == sorted(ends, reverse=True)


def test_sec_tag_not_reported_and_unit_absent(monkeypatch):
    from app.research_sec import SecFailure
    bridge, query, kwargs, _ = _structured_fixture(monkeypatch, failure=SecFailure('source_tag_not_found'))
    result = bridge.execute_structured_query(query, **kwargs)
    assert (result.status, result.reason) == ('unknown', 'tag_not_reported')
    bridge, query, kwargs, _ = _structured_fixture(monkeypatch, failure=SecFailure('source_no_matching_facts', ['shares']))
    result = bridge.execute_structured_query(query, **kwargs)
    assert result.status == 'unknown' and result.available_units == ['shares']
