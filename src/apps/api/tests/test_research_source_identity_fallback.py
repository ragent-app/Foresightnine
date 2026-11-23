from datetime import datetime, timezone
from hashlib import sha256

from app.research_sources import ResearchSource, SourcePermissions
from app.research_run_insights import primary_filing_gap, run_coverage_map
from app.research_reconciliation import classify_source


def test_source_origin_and_targeted_filing_gap_are_consistent_and_permission_scoped():
    now = datetime.now(timezone.utc)
    def src(sid, title, text, url, method, kind='user_supplied', allow=True):
        return ResearchSource(source_id=sid, owner_id='owner', title=title, text=text, url=url,
            kind=kind, retrieval_method=method, obtained_at=now, content_sha256=sha256(text.encode()).hexdigest(),
            permissions=SourcePermissions(store=True, model_input=allow, display=True, export=False))
    filing = src('f', 'Example Corp 10-K', 'Example Corp annual revenue 2025 was 100 USD.', 'https://sec.gov/Archives/data/1/10k', 'sec_filing_html', 'official')
    news = src('n', 'article', 'Example revenue 2025 was 100 USD.', 'https://news.example.org/story', 'web_html')
    issuer = src('i', 'release', 'Example Corp release.', 'https://example.com/release', 'web_html')
    upload = src('u', 'manual copy', 'A note.', 'https://sec.gov/Archives/data/1/10k', 'user_pdf')
    by_id = {s.source_id: s for s in [filing, news, issuer, upload]}
    fact = {'fact_id':'fact-n', 'label':'Example revenue', 'value':'100', 'unit':'USD', 'currency':'USD', 'period':'2025',
            'source_id':'n', 'quotation':news.text, 'character_start':0, 'character_end':len(news.text)}
    state = {'facts':[fact], 'read_ranges':[{'source_id':'n','start':0,'end':len(news.text)}], 'events':[],
             'entity_candidates':{'EX': {'company':'Example Corp', 'symbol':'EX'}}}
    claims = [{'text':'Example revenue was 100 USD.', 'citations':[{'source_id':'n'}]}]
    coverage = run_coverage_map(state, by_id)
    docs = {doc['source_id']:doc for doc in coverage['documents']}
    assert [(classify_source(s), docs[s.source_id]['category']) for s in [filing, news, issuer, upload]] == [
        ('filing', 'filings'), ('news', 'news_web'), ('news', 'news_web'), ('user_upload', 'uploads')]
    assert docs['i']['origin'] == 'issuer_candidate_unverified'
    assert docs['n']['ingestion_kind'] == 'user_supplied' and docs['n']['retrieval_method'] == 'web_html'
    assert docs['n']['permissions']['export'] is False
    result = primary_filing_gap(state, by_id, claims)
    assert result and result['source_id'] == 'f' and result['metric'] == 'revenue' and result['period'] == '2025'
    denied = filing.model_copy(update={'permissions': filing.permissions.model_copy(update={'model_input':False})})
    assert primary_filing_gap(state, {**by_id, 'f': denied}, claims) is None
    primary_fact = {**fact, 'fact_id':'fact-f', 'source_id':'f', 'quotation':filing.text, 'character_end':len(filing.text)}
    assert primary_filing_gap({**state, 'facts':[fact, primary_fact]}, by_id,
                              claims + [{'citations':[{'source_id':'f'}]}]) is None


def test_primary_filing_never_uses_another_company_as_candidate_or_support():
    now = datetime.now(timezone.utc)
    def source(sid, title, text, method, url):
        return ResearchSource(source_id=sid, owner_id='owner', title=title, text=text, url=url,
            kind='official' if method == 'sec_filing_html' else 'user_supplied', retrieval_method=method,
            obtained_at=now, content_sha256=sha256(text.encode()).hexdigest(),
            permissions=SourcePermissions(store=True, model_input=True, display=True, export=True))
    news = source('news', 'Alpha Corp earnings', 'Alpha Corp revenue 2025 100 USD', 'web_html', 'https://news.example/a')
    a = source('a', 'Alpha Corp 10-K', 'Alpha Corp revenue 2025 100 USD', 'sec_filing_html', 'https://sec.gov/a')
    b = source('b', 'Beta Corp 10-K', 'Beta Corp revenue 2025 100 USD', 'sec_filing_html', 'https://sec.gov/b')
    def fact(s, label):
        return {'fact_id': 'fact-' + s.source_id, 'label': label, 'value':'100', 'unit':'USD', 'currency':'USD',
                'period':'2025', 'source_id':s.source_id, 'quotation':s.text, 'character_start':0, 'character_end':len(s.text)}
    state = {'facts':[fact(news, 'Alpha revenue'), fact(b, 'Beta revenue')], 'read_ranges':[], 'events':[],
             'entity_candidates':{'ALP':{'company':'Alpha Corp','symbol':'ALP'}, 'BET':{'company':'Beta Corp','symbol':'BET'}}}
    claims = [{'text':'Alpha revenue', 'citations':[{'source_id':'news'}]},
              {'text':'Beta revenue', 'citations':[{'source_id':'b'}]}]
    assert primary_filing_gap(state, {'news':news, 'b':b}, claims) is None
    gap = primary_filing_gap(state, {'news':news, 'a':a, 'b':b}, claims)
    assert gap and gap['source_id'] == 'a'
    assert primary_filing_gap({**state, 'facts': state['facts'] + [fact(a, 'Alpha revenue')]},
                              {'news':news, 'a':a, 'b':b}, claims + [{'text':'Alpha revenue', 'citations':[{'source_id':'a'}]}]) is None
