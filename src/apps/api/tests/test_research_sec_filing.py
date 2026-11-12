import base64
import gzip
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
import app.main as api
from app.openai_compatible import HttpResponse, _TransportTimeout
from app.research_fetch import FetchSourceRequest, FetchFailure, fetch_official_text
from app.research_sec import SecFailure, fetch_sec_filing

URL = 'https://www.sec.gov/Archives/edgar/data/858877/000085887724000017/csco-20240727.htm'

def request(url=URL):
    return FetchSourceRequest(url=url, title='SEC filing', permissions={'store': True, 'display': True, 'model_input': True, 'export': True})

@pytest.mark.parametrize('url', ['https://www.sec.gov/Archives/edgar/data/1/123/foo.htm',
    'https://www.sec.gov/Archives/edgar/data/1/000000000000000001/foo.htm?x=1',
    'https://www.sec.gov/Archives/edgar/data/1/000000000000000001/%66oo.htm',
    'https://www.sec.gov/Archives/edgar/data/1/000000000000000001/foo.xml',
    'https://www.sec.gov.evil.test/Archives/edgar/data/1/000000000000000001/foo.htm',
    'https://www.sec.gov/files/company_tickers.json'])
def test_sec_fetch_accepts_only_direct_filing_paths(url):
    with pytest.raises(ValidationError): request(url)

def test_sec_filing_preserves_original_and_uses_configured_contact():
    path = Path(__file__).resolve().parents[3] / 'docs/product/phase-a/60-reserved-full-review/cisco-2024-sec.html.gz'
    raw = gzip.decompress(path.read_bytes())
    def transport(method, url, headers, body, timeout):
        assert method == 'GET' and url == URL and headers['User-Agent'] == 'Example Research team@example.test'
        assert 'Authorization' not in headers and timeout == 15.0
        return HttpResponse(200, raw.decode('utf-8'), {'content-type':'text/html; charset=utf-8'})
    result = fetch_sec_filing(request(), 'Example Research team@example.test', transport)
    assert result.response_sha256 == sha256(raw).hexdigest() and base64.b64decode(result.original_base64) == raw
    assert '53,803' in result.text and '56,998' in result.text
    with pytest.raises(SecFailure, match='sec_contact_unconfigured'):
        fetch_sec_filing(request(), None, transport)
    with pytest.raises(FetchFailure):
        fetch_official_text(request(), lambda *args: pytest.fail('SEC must use SEC client'))

def test_sec_filing_rejects_redirects_timeouts_and_wrong_formats():
    with pytest.raises(SecFailure, match='source_unavailable'):
        fetch_sec_filing(request(), 'Example x@example.test', lambda *args: HttpResponse(302, '', {'location':'https://127.0.0.1'}))
    with pytest.raises(SecFailure, match='source_rate_limited'):
        fetch_sec_filing(request(), 'Example x@example.test', lambda *args: HttpResponse(429, '', {}))
    with pytest.raises(SecFailure, match='source_format_unsupported'):
        fetch_sec_filing(request(), 'Example x@example.test', lambda *args: HttpResponse(200, '{}', {'content-type':'application/json'}))
    def timeout(*args): raise _TransportTimeout()
    with pytest.raises(SecFailure, match='source_timeout'):
        fetch_sec_filing(request(), 'Example x@example.test', timeout)

def test_sec_filing_endpoint_requires_contact_and_enforces_source_permissions(monkeypatch):
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE','mock')
    monkeypatch.delenv('SEC_USER_AGENT', raising=False)
    monkeypatch.delenv('FORESIGHTNINE_SEC_USER_AGENT', raising=False)
    client = TestClient(api.app)
    def account():
        session=client.post('/v1/auth/local/register',json={'email':f'{uuid4().hex}@example.test','password':'correct horse battery staple!'}).json()
        return {'Authorization':'Bearer '+session['access_token']}
    owner, other = account(), account()
    body = {'url':URL,'title':'Filing','permissions':{'store':True,'display':True,'model_input':True,'export':True}}
    assert client.post('/v2/research/sources/fetch',headers=owner,json=body).status_code == 503
    monkeypatch.setenv('FORESIGHTNINE_SEC_USER_AGENT','Example team@example.test')
    monkeypatch.setattr(api,'reserve_sec_request',lambda *_: True)
    raw=b'<html><p>Revenue 100</p></html>'
    monkeypatch.setattr(api,'fetch_sec_filing',lambda req,agent: fetch_sec_filing(req,agent,lambda *args: HttpResponse(200,raw.decode(),{'content-type':'text/html'})))
    response=client.post('/v2/research/sources/fetch',headers=owner,json=body)
    assert response.status_code == 201, response.text
    item=response.json(); assert item['retrieval_method']=='sec_filing_html' and item['response_sha256']==sha256(raw).hexdigest()
    assert item['original_html_available'] and 'original_html_base64' not in item and 'text' not in item
    path='/v2/research/sources/'+item['source_id']+'/original'
    download=client.get(path,headers=owner)
    assert download.status_code==200 and download.content==raw and download.headers['content-type']=='application/octet-stream'
    assert client.get(path,headers=other).status_code==404
    assert client.post('/v2/research/sources/fetch',headers=owner,json={**body,'permissions':{'store':False}}).status_code==422


def test_sec_filing_respects_caller_timeout_budget():
    seen = []
    def transport(method, url, headers, body, timeout):
        seen.append(timeout)
        return HttpResponse(200, '<html><p>Revenue 100</p></html>', {'content-type': 'text/html'})
    result = fetch_sec_filing(request(), 'Fixture test@example.test', transport, timeout_seconds=3.0)
    assert result.text and seen == [3.0]
    for value in (0, -1, 16):
        with pytest.raises(SecFailure, match='source_timeout'):
            fetch_sec_filing(request(), 'Fixture test@example.test', transport, timeout_seconds=value)
    assert seen == [3.0]


def test_sec_filing_timeout_includes_html_extraction(monkeypatch):
    from threading import Event
    from time import monotonic
    entered, release, finished = Event(), Event(), Event()
    def slow_extract(upload):
        entered.set()
        try:
            release.wait(2)
            return ('Revenue 100', 'a' * 64)
        finally:
            finished.set()
    monkeypatch.setattr('app.research_sec.extract_uploaded_html', slow_extract)
    calls = []
    def transport(*args):
        calls.append(1)
        return HttpResponse(200, '<html><p>Revenue 100</p></html>', {'content-type': 'text/html'})
    started = monotonic()
    try:
        with pytest.raises(SecFailure, match='source_timeout'):
            fetch_sec_filing(request(), 'Fixture test@example.test', transport, timeout_seconds=.05)
        assert entered.is_set() and monotonic() - started < 1 and calls == [1]
    finally:
        release.set()
        assert finished.wait(2)
