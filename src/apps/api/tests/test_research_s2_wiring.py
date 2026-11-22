"""S2 wiring: search_library, news_search, published dates, reconciliation, coverage map, method packs, run views."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as api
from app.fin_data.models import Coverage, Cost, Permissions, Provenance, ToolResult
from app.news.gdelt import GDELT_ATTRIBUTION, NewsLead
from app.model_protocol import ModelResponse
from app.research_fetch import FetchSourceRequest, fetch_source_document
from app.research_ocr import OcrOutcome
from app.research_runtime import SYSTEM, TOOLS, run_research_agent
from app.research_sources import ResearchSource, SourcePermissions
from tests.test_research_runtime import Model, context, turn
from tests.test_research_library import paged

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
STOP = ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')
PERMS = SourcePermissions(store=True, model_input=True, display=True, export=True)


def src(source_id, text, *, title=None, kind='user_supplied', method='user_supplied', url=None, provenance=None):
    return ResearchSource(source_id=source_id, owner_id='o', title=title or source_id, text=text, kind=kind, url=url,
                          retrieval_method=method, obtained_at=NOW, content_sha256='h' + source_id, permissions=PERMS,
                          provenance=provenance or {})


def feedback(model, index=1):
    tool_msgs = [m for m in model.inputs[index] if m.get('role') == 'tool']
    return json.loads(tool_msgs[-1]['content'])


# --------------------------------------------------------------------------------------------- tools registry

def test_tools_static_order_and_no_discover_sources():
    names = [t.name for t in TOOLS]
    assert 'discover_sources' not in names
    assert names.index('search_source') < names.index('search_library') < names.index('submit_research')
    assert names.index('news_search') < names.index('acquire_sources')
    assert names[-1] == 'submit_research'
    assert 'discover_sources' not in SYSTEM and '2026' not in SYSTEM


# --------------------------------------------------------------------------------------------- search_library

def test_search_library_hits_map_to_reads_and_report_coverage():
    text, pages = paged([('text', 'Quarterly revenue was 4215 million dollars. Filler words follow here. '), ('needs_ocr', ''),
                         ('text', 'Segment note about unrelated matters.')])
    doc = src('doc', text, title='Annual report', provenance={'document_pages': {'page_count': 3, 'pages': pages, 'version': 2, 'method': 'pdftotext_layout_paged'}})
    other = src('other', 'Plain note: revenue guidance was withdrawn.')
    m = Model([turn('search_library', {'query': 'revenue 4215', 'rationale': 'find'}, 'a'), STOP])
    run_research_agent(question='Q', sources=[doc, other], model=m, context=context())
    fb = feedback(m)
    assert fb['status'] == 'completed' and fb['hits'][0]['source_id'] == 'doc'
    hit = fb['hits'][0]
    assert text[hit['read_sources']['start']:hit['read_sources']['end']].count('4215') == 1
    assert hit['read_pages'] == {'source_id': 'doc', 'first_page': 1, 'last_page': 1}
    cov = fb['library_coverage']
    assert cov['unsearched_page_count'] == 1 and cov['complete'] is False
    assert cov['sources_with_unsearched_pages'][0]['source_id'] == 'doc'


def test_search_library_excludes_ineligible_and_reports_reason():
    private = src('private', 'secret revenue 4215')
    private = private.model_copy(update={'permissions': SourcePermissions(store=True, model_input=False, display=True)})
    ok = src('ok', 'revenue 4215 public')
    m = Model([turn('search_library', {'query': 'revenue 4215', 'rationale': 'find'}, 'a'), STOP])
    run_research_agent(question='Q', sources=[private, ok], model=m, context=context())
    fb = feedback(m)
    assert [h['source_id'] for h in fb['hits']] == ['ok']
    assert fb['library_coverage']['sources_excluded'] == [{'source_id': 'private', 'reason': 'model_permission_missing'}]


def test_search_library_follows_ocr_version_and_marks_ocr_hits():
    text, pages = paged([('text', 'Cover page text only. '), ('needs_ocr', '')])
    old = src('pdf', text, provenance={'document_pages': {'page_count': 2, 'pages': pages, 'version': 2, 'method': 'm'}})
    new_text, new_pages = paged([('text', 'Cover page text only. '), ('ocr', 'Operating margin was 17.5 percent.')])
    new = src('pdf2', new_text, provenance={'document_pages': {'page_count': 2, 'pages': new_pages, 'version': 2, 'method': 'm',
                                                              'ocr': {'2': {'mean_confidence': 40.0, 'engine': 'fake'}}}})
    calls = []

    def ocr(source, pages_):
        calls.append(pages_)
        return OcrOutcome('completed', source=new, pages=[{'page': 2, 'status': 'ocr'}])

    m = Model([turn('search_library', {'query': 'operating margin', 'rationale': 'a'}, 'a'),
               turn('request_ocr', {'source_id': 'pdf', 'pages': [2], 'rationale': 'ocr'}, 'b'),
               turn('search_library', {'query': 'operating margin', 'rationale': 'a'}, 'c'), STOP])
    run_research_agent(question='Q', sources=[old], model=m, context=context(), ocr_pages=ocr)
    first, third = feedback(m, 1), feedback(m, 3)
    assert first['returned_hits'] == 0 and first['library_coverage']['unsearched_page_count'] == 1
    hit = third['hits'][0]
    assert hit['source_id'] == 'pdf2' and hit['extraction_method'] == 'ocr' and 'verify' in hit['verification_note']
    assert hit.get('low_ocr_confidence') is True
    assert third['library_coverage']['sources_searched'] == 1   # old version replaced, not duplicated


def test_repeated_search_library_is_a_duplicate_until_state_changes():
    d = src('d', 'revenue 100')
    m = Model([turn('search_library', {'query': 'revenue', 'rationale': 'a'}, 'a'),
               turn('search_library', {'query': 'revenue', 'rationale': 'b'}, 'b'), STOP])
    run_research_agent(question='Q', sources=[d], model=m, context=context())
    assert feedback(m, 2)['status'] == 'duplicate_action'


# --------------------------------------------------------------------------------------------- news_search

class FakeNews:
    name = 'gdelt'
    supports_time_filter = True

    def __init__(self, leads):
        self.leads = leads

    def search(self, query, *, since=None, until=None, languages=None, limit=10):
        return ToolResult(status='ok', reason='ok', coverage=Coverage(rows=len(self.leads)),
                          provenance=Provenance(provider='gdelt', endpoint='fake', retrieved_at=NOW), cost=Cost(provider_calls=1),
                          permissions=Permissions(store=True, display=True, model_input=True), data=self.leads)


def test_news_search_leads_feed_acquire_and_attribution_and_blocked_leads():
    leads = [NewsLead(url='https://news.example.com/a', title='ANT shares jump in debut', providers=['gdelt'], source_type='news',
                      domain='news.example.com', seen_at=NOW, attribution=[GDELT_ATTRIBUTION]),
             NewsLead(url='https://www.hkex.com.hk/news/x', title='HKEX story on ANT debut', providers=['gdelt'], source_type='news',
                      domain='www.hkex.com.hk', seen_at=NOW)]
    got = src('s1', 'ANT shares jump', method='web_html', kind='official', url='https://news.example.com/a')
    acquired = []

    def acquire(url, title):
        acquired.append(url)
        return got, {}

    m = Model([turn('news_search', {'query': 'ANT debut', 'languages': ['en'], 'rationale': 'news'}, 'n'),
               turn('acquire_sources', {'urls': ['https://news.example.com/a'], 'rationale': 'get'}, 'a'),
               turn('acquire_sources', {'urls': ['https://www.hkex.com.hk/news/x'], 'rationale': 'get'}, 'b'), STOP])
    saved = []
    out = run_research_agent(question='Q', sources=[], model=m, context=context(), news_providers=[FakeNews(leads)], acquire=acquire,
                             checkpoint=lambda s: saved.append(json.loads(json.dumps(s))))
    fb = feedback(m)
    assert fb['status'] == 'completed' and fb['attribution'] == GDELT_ATTRIBUTION and len(fb['leads']) == 2
    assert fb['clusters'] and all('lead_urls' in c for c in fb['clusters'])
    by_url = {l['url']: l for l in fb['leads']}
    assert by_url['https://www.hkex.com.hk/news/x'].get('acquirable') is False
    assert feedback(m, 2)['results'][0]['status'] == 'acquired' and acquired == ['https://news.example.com/a']
    assert feedback(m, 3)['results'][0]['reason'] == 'source_blocked_terms'
    assert out.attributions and out.attributions[0]['id'] == 'gdelt'
    assert saved[-1]['source_clusters']['s1']
    ev = next(e for e in out.events if e.get('name') == 'news_search')
    assert ev['summary']['kind'] == 'news_search' and ev['summary']['attributions'] == ['gdelt']


def test_news_search_unavailable_without_providers_and_counts_one_search_call():
    m = Model([turn('news_search', {'query': 'q', 'rationale': 'r'}, 'n'), STOP])
    run_research_agent(question='Q', sources=[], model=m, context=context())
    assert feedback(m)['reason'] == 'search_not_authorized_or_unavailable'
    ctx = context()
    m2 = Model([turn('news_search', {'query': 'q', 'rationale': 'r'}, 'n'), STOP])
    run_research_agent(question='Q', sources=[], model=m2, context=ctx, discover=lambda q: [{'title': 'T', 'url': 'https://a.example.com/x'}])
    assert ctx.search_calls == 1


def test_news_search_redirect_titles_keep_unresolved_titles():
    hits = [{'title': 'Opaque story', 'url': 'https://www.google.com/goto?url=AbCdEf123456'}]
    m = Model([turn('news_search', {'query': 'q', 'rationale': 'r'}, 'n'), STOP])
    run_research_agent(question='Q', sources=[], model=m, context=context(), discover=lambda q: hits)
    fb = feedback(m)
    assert fb['unresolved_titles'] == ['Opaque story'] and fb['reason'] == 'search_sources_denied' and fb['leads'] == []


# --------------------------------------------------------------------------------------------- published date

def test_fetch_extracts_published_date_from_head_only():
    html = ('<html><head><meta property="article:published_time" content="2026-11-19T21:30:00-05:00">'
            '<title>t</title></head><body><p>' + 'Body text. ' * 50 + '</p></body></html>')
    body = html.encode()

    import app.research_fetch as rf
    from app.openai_compatible import BinaryHttpResponse
    doc = rf.fetch_source_document(
        rf.FetchSourceRequest(url='https://example.org/story', title='t', permissions={'store': True}),
        lambda *a: BinaryHttpResponse(200, body, {'content-type': 'text/html'}))
    assert doc.published_date and doc.published_date['published_at'].startswith('2026-11-19T21:30:00')
    import app.research_fetch as rf
    info = rf._published_date_info(body, None)
    assert info['published_at'].startswith('2026-11-19T21:30:00') and info['source'].startswith('meta') and info['confidence'] == 'high'
    assert rf._published_date_info(b'<html><body>no date at all</body></html>', None) is None
    # only the head of a huge document is parsed
    huge = b'<html><head></head><body>' + b'x' * (rf.PUBLISHED_DATE_HEAD_BYTES + 10) + b'<time datetime="2026-01-01">late</time>'
    assert rf._published_date_info(huge, None) is None


# --------------------------------------------------------------------------------------------- reconciliation + coverage

def fact_args(source_id, quote, value):
    return {'label': 'ANT closing price', 'value': value, 'unit': 'USD', 'currency': 'USD', 'period': '2026-11-19',
            'source_id': source_id, 'quotation': quote}


def reconciliation_run(extra_turns=()):
    exch = src('exch', 'ANT closing price 115.10 USD on 2026-11-19.', kind='market_data', method='market_data', title='ANT bars')
    news = src('news', 'ANT closing price 114.99 USD on 2026-11-19, CBS reported.', kind='official', method='web_html',
               url='https://www.cbsnews.com/x')
    m = Model([turn('read_sources', {'reads': [{'source_id': 'exch'}, {'source_id': 'news'}], 'rationale': 'read'}, 'r'),
               turn('record_facts', {'facts': [fact_args('exch', 'ANT closing price 115.10 USD', '115.10')], 'rationale': 'f1'}, 'f1'),
               turn('record_facts', {'facts': [fact_args('news', 'ANT closing price 114.99 USD', '114.99')], 'rationale': 'f2'}, 'f2'),
               *extra_turns])
    return m, [exch, news]


def test_reconciliation_appended_to_record_facts_and_gap_and_view_at_submission():
    sub = turn('submit_research', {'report': 'ANT closed at 115.10.', 'claims': [{'text': 'Close 115.10', 'source_id': 'exch',
               'quotation': 'ANT closing price 115.10 USD', 'role': 'support'}], 'limitations': [], 'stopping_reason': 'done'}, 's')
    m, sources = reconciliation_run([sub])
    out = run_research_agent(question='Q', sources=sources, model=m, context=context())
    first, second = feedback(m, 2), feedback(m, 3)
    assert 'reconciliation' not in first                      # one source only: nothing to compare
    rec = second['reconciliation']
    assert rec['by_status']['differs'] == 1 and rec['discrepancies'][0]['reference'] in ('115.1', '115.10')
    assert len(json.dumps(rec)) <= 2600
    assert out.status == 'completed'
    assert any(g['code'] in ('source_discrepancy_material', 'source_discrepancy_minor') for g in out.gaps)
    row = out.reconciliation['rows'][0]
    assert row['status'] == 'differs' and {c['source_class'] for c in row['cells']} == {'market_data', 'news'}
    assert out.coverage_map['version'] == 1 and out.coverage_map['rows']


def test_reconciliation_not_added_without_facts_and_coverage_map_on_partial_end():
    m = Model([turn('market_data', {'symbol': 'ANT', 'exchange': 'NYSE', 'lookback': '5d', 'rationale': 'p'}, 'p'), STOP])
    out = run_research_agent(question='Q', sources=[], model=m, context=context())
    assert out.status == 'partial' and out.reconciliation is None
    assert out.coverage_map is not None and out.coverage_map['run']['status'] in ('interrupted', 'incomplete')
    assert any(c['category'] == 'market_prices' for c in out.coverage_map['rows'])


# --------------------------------------------------------------------------------------------- method packs

def test_method_pack_in_first_message_after_clock_not_in_system_and_recorded():
    from app.research_clock import build_research_clock
    saved = []
    m = Model([STOP])
    out = run_research_agent(question='Review the latest quarterly earnings and revenue guidance', sources=[], model=m, context=context(),
                             clock=build_research_clock(NOW), news_providers=[FakeNews([])],
                             checkpoint=lambda s: saved.append(json.loads(json.dumps(s))))
    first = json.loads(saved[-1]['user'])
    keys = list(first)
    assert keys.index('research_clock') < keys.index('method_packs') < keys.index('available_capabilities')
    assert 'METHOD PACK earnings_review@' in first['method_packs'][0]
    assert first['available_capabilities']['search_library_available'] is True and first['available_capabilities']['news_search_available'] is True
    assert 'METHOD PACK' not in SYSTEM and not any('METHOD PACK' in t.description for t in TOOLS)
    assert out.method_packs and out.method_packs[0]['pack_id'] == 'earnings_review' and '@' in out.method_packs[0]['fingerprint']
    nopack = []
    run_research_agent(question='hello there', sources=[], model=Model([STOP]), context=context(), checkpoint=lambda s: nopack.append(json.loads(json.dumps(s))))
    assert 'method_packs' not in json.loads(nopack[-1]['user'])


# --------------------------------------------------------------------------------------------- API level

def test_api_run_saves_new_turn_fields(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')

    class Provider:
        name = 'native-s2-test'
        policy = SimpleNamespace(timeout_seconds=5, max_input_tokens=None, max_output_tokens=1000)

        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return turn('read_sources', {'reads': [{'source_id': ids[0]}, {'source_id': ids[1]}], 'rationale': 'r'}, 'read')
            if self.calls == 2:
                return turn('record_facts', {'facts': [fact_args(ids[0], 'ANT closing price 115.10 USD', '115.10'),
                                                       fact_args(ids[1], 'ANT closing price 114.99 USD', '114.99')], 'rationale': 'f'}, 'rf')
            return turn('submit_research', {'report': 'ANT closed at 115.10.', 'claims': [{'text': 'Close 115.10', 'source_id': ids[0],
                        'quotation': 'ANT closing price 115.10 USD', 'role': 'support'}], 'limitations': [], 'stopping_reason': 'answered'}, 'sub')

    monkeypatch.setattr(api, 'challenge_provider', Provider())
    client = TestClient(api.app)
    auth = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    perms = {'store': True, 'display': True, 'model_input': True, 'export': True}
    ids = [client.post('/v2/research/sources', headers=headers, json={'title': t, 'text': x, 'permissions': perms}).json()['source_id']
           for t, x in (('Exchange note', 'ANT closing price 115.10 USD on 2026-11-19.'), ('Press note', 'ANT closing price 114.99 USD on 2026-11-19.'))]
    topic = client.post('/v2/research/topics', headers=headers, json={'question': 'ANT earnings revenue review', 'source_ids': ids}).json()
    path = '/v2/research/topics/' + topic['topic_id']
    response = client.post(path + '/runs', headers={**headers, 'Idempotency-Key': uuid4().hex},
                           json={'parent_version_id': topic['versions'][0]['version_id'], 'discover_sources': False})
    assert response.status_code == 202, response.text
    op = response.json()['operation']['operation_id']
    import time
    for _ in range(300):
        status = client.get(path + '/runs/' + op, headers=headers).json()['operation']['status']
        if status in ('completed', 'failed', 'cancelled'):
            break
        time.sleep(.01)
    assert status == 'completed'
    turn_json = client.get(path, headers=headers).json()['versions'][-1]['content']['turns'][0]
    assert turn_json['coverage_map']['version'] == 1
    assert turn_json['method_packs'] and turn_json['method_packs'][0]['pack_id'] == 'earnings_review'
    assert turn_json['attributions'] == []
    rec = turn_json['reconciliation']
    assert rec['rows'][0]['status'] == 'differs'
    assert any(g['code'].startswith('source_discrepancy') for g in turn_json['gaps'])
    inputs = client.get(path + '/runs/' + op + '/inputs', headers=headers).json()
    assert inputs['method_packs'][0]['pack_id'] == 'earnings_review'
