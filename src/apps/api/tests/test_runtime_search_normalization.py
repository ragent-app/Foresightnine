import base64
import json
import pytest
from app.glm_mcp_search import GlmCodingMcpSearchEvidenceProvider, SearchCandidate
from app.model_protocol import ModelResponse
from app.openai_compatible import HttpResponse
from app.phase3 import ClientSearchEvidenceProvider, ProviderFailure
from app.research_discovery import RedirectLead, normalize_search_lead, search_lead_url, unwrap_redirect_url
from app.research_runtime import run_research_agent
from tests.test_research_runtime import Model, context, turn


def test_search_merges_leads_and_preserves_query_identity():
    model = Model([turn('discover_sources', {'query': 'one', 'rationale': 'find'}, 'a'),
                   turn('discover_sources', {'query': 'two', 'rationale': 'find more'}, 'b'),
                   ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
    snapshots = []
    def discover(query):
        if query == 'one':
            return [SearchCandidate('First', 'https://EXAMPLE.com/report?q=1#part', 'text', None, None, False)]
        return [{'title': 'Second', 'url': 'https://example.com/report?q=2'}]
    run_research_agent(question='Q', sources=[], model=model, context=context(), discover=discover,
                       checkpoint=lambda s: snapshots.append(json.loads(json.dumps(s))))
    assert [lead['url'] for lead in snapshots[-1]['leads']] == [
        'https://example.com/report?q=1', 'https://example.com/report?q=2']


def test_all_invalid_response_disables_connector_without_another_search():
    model = Model([turn('discover_sources', {'query': 'one', 'rationale': 'find'}, 'a'),
                   turn('discover_sources', {'query': 'two', 'rationale': 'try again'}, 'b'),
                   ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')])
    calls = []
    def discover(query):
        calls.append(query)
        return [{'bad': 'row'}]
    result = run_research_agent(question='Q', sources=[], model=model, context=context(), discover=discover)
    assert calls == ['one']
    events = [e for e in result.events if e.get('name') == 'discover_sources']
    assert [e['reason'] for e in events] == ['search_response_invalid', 'search_response_invalid']


def _b64_bing(target: str, *, urlsafe: bool = True, strip_pad: bool = True) -> str:
    raw = target.encode('utf-8')
    token = base64.urlsafe_b64encode(raw) if urlsafe else base64.b64encode(raw)
    text = token.decode('ascii')
    return 'a1' + (text.rstrip('=') if strip_pad else text)


@pytest.mark.parametrize(('url', 'expected'), [
    (f"https://www.bing.com/ck/a?!&&p=1&u={_b64_bing('https://www.sec.gov/Archives/edgar/data/1/a.htm')}&ntb=1",
     'https://www.sec.gov/Archives/edgar/data/1/a.htm'),
    (f"https://cn.bing.com/ck/a?u={_b64_bing('http://www.bea.gov/news/release?id=1&v=2', urlsafe=False, strip_pad=False)}",
     'http://www.bea.gov/news/release?id=1&v=2'),
    ('https://r.search.yahoo.com/_ylt=Awr9/RV=2/RE=170/RO=10/RU=https%3a%2f%2fwww.sec.gov%2ffiles%2f10k.htm/RK=2/RS=xyz',
     'https://www.sec.gov/files/10k.htm'),
    ('https://r.search.yahoo.co.jp/_ylt=abc/RV=2/RU=https%3A%2F%2Ffinance.yahoo.com%2Fquote%2FNVDA/RK=2/RS=xyz',
     'https://finance.yahoo.com/quote/NVDA'),
    ('https://search.yahoo.com/search?p=nvidia+revenue', 'https://search.yahoo.com/search?p=nvidia+revenue'),
    ('https://www.google.com/url?target=https%3A%2F%2Finvestor.nvidia.com%2Fnews', 'https://investor.nvidia.com/news'),
    ('https://www.google.com/goto?url=https%3A%2F%2Fwww.sec.gov%2F10k.htm', 'https://www.sec.gov/10k.htm'),
    ('https://www.google.com/aclk?adurl=https://www.example.com/landing', 'https://www.example.com/landing'),
    ('https://html.duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.bea.gov%2Fnews', 'https://www.bea.gov/news'),
])
def test_unwrap_redirect_url_recovers_bing_yahoo_and_query_redirectors(url, expected):
    assert unwrap_redirect_url(url) == expected
    assert normalize_search_lead({'title': 'Lead', 'url': url}).url == expected


@pytest.mark.parametrize('url', [
    'https://www.google.com/goto?url=CAESdwHEIkXX&gsessionid=x',
    f"https://www.bing.com/ck/a?u={_b64_bing('opaque-not-a-url')}",
    'https://www.bing.com/ck/a?u=a1***not_base64***',
    'https://www.bing.com/ck/a?p=no_u_param',
    f"https://www.bing.com/ck/a?u={_b64_bing('https://www.bing.com/search?q=nvda')}",
    'https://r.search.yahoo.com/_ylt=Awr9/RV=2/RU=opaque_token/RK=2/RS=xyz',
    'https://r.search.yahoo.com/_ylt=Awr9/RV=2/RK=2/RS=xyz',
    'https://r.search.yahoo.com/_ylt=Awr9/RV=2/RU=https%3A%2F%2Fwww.google.com%2Fother/RK=2/RS=xyz',
    'https://www.baidu.com/link?url=EqLzOpaqueToken123',
])
def test_unwrap_redirect_url_raises_redirect_lead_on_opaque_or_search_engine_targets(url):
    with pytest.raises(RedirectLead):
        unwrap_redirect_url(url)
    with pytest.raises(RedirectLead):
        normalize_search_lead({'title': 'Lead', 'url': url})


def test_unwrap_redirect_url_rejects_private_targets_and_search_lead_url_exposes_wrapped_fred():
    private_bing = f"https://www.bing.com/ck/a?u={_b64_bing('http://127.0.0.1/admin')}"
    with pytest.raises(ProviderFailure, match='private or reserved') as error:
        unwrap_redirect_url(private_bing)
    assert error.value.code == 'unsafe_evidence_url'
    fred_google = 'https://www.google.com/url?q=https://fred.stlouisfed.org/series/GDP'
    assert search_lead_url({'url': fred_google}) == 'https://fred.stlouisfed.org/series/GDP'


def test_glm_opaque_goto_batch_does_not_poison_search_invalid_response():
    def rpc(msg_id: int, result: dict) -> HttpResponse:
        return HttpResponse(200, json.dumps({'jsonrpc': '2.0', 'id': msg_id, 'result': result}), {'mcp-session-id': 's1'})

    def mcp_search_replies(rows: list[dict]) -> list[HttpResponse]:
        return [
            rpc(1, {'protocolVersion': '2025-03-26'}),
            HttpResponse(202, ''),
            rpc(2, {'tools': [{'name': 'web_search_prime', 'inputSchema': {'type': 'object', 'properties': {'search_query': {'type': 'string'}, 'count': {'type': 'integer'}}}}]}),
            rpc(3, {'content': [{'type': 'text', 'text': json.dumps({'search_result': rows})}]}),
        ]

    replies = mcp_search_replies([
        {'title': 'Opaque 1', 'link': 'https://www.google.com/goto?url=CAESdwHEIkXX&gsessionid=x', 'content': 'One.'},
        {'title': 'Opaque 2', 'link': 'https://www.google.com/goto?url=CAESother123&gsessionid=y', 'content': 'Two.'},
    ]) + mcp_search_replies([
        {'title': 'Opaque 3', 'link': 'https://www.google.com/goto?url=CAESmixed&gsessionid=z', 'content': 'Three.'},
        {'title': 'Bing filing', 'link': f"https://www.bing.com/ck/a?u={_b64_bing('https://www.sec.gov/nvda.htm')}", 'content': 'Filing.', 'date': '2026-08-01T12:00:00Z'},
        {'title': 'Fallback target', 'link': 'https://www.google.com/goto?url=CAESfallback', 'target_url': 'https://investor.nvidia.com/news', 'content': 'PR.', 'publish_date': '2026-08-02'},
        {'title': 'Wrapped FRED', 'link': 'https://www.google.com/url?q=https://fred.stlouisfed.org/series/GDP', 'content': 'Restricted.'},
    ])

    provider = GlmCodingMcpSearchEvidenceProvider('synthetic-secret', transport=lambda *_args: replies.pop(0), sleeper=lambda _delay: None)
    model = Model([
        turn('discover_sources', {'query': 'first opaque batch', 'rationale': 'find'}, 'c1'),
        turn('discover_sources', {'query': 'second mixed batch', 'rationale': 'find more'}, 'c2'),
        ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop'),
    ])

    snapshots = []
    outcome = run_research_agent(
        question='Compare revenue',
        sources=[],
        model=model,
        context=context(),
        discover=lambda q: provider.search(q, limit=5),
        checkpoint=lambda s: snapshots.append(json.loads(json.dumps(s))),
    )

    events = [e for e in outcome.events if e.get('name') == 'discover_sources']
    assert len(events) == 2
    assert events[0]['status'] == 'completed'
    assert events[0]['reason'] == 'search_sources_denied'
    assert events[0]['denied_reasons'] == {'source_not_authorized': 0, 'invalid_lead': 0, 'redirect_link': 2}
    assert events[1]['status'] == 'completed'
    assert events[1]['denied_reasons'] == {'source_not_authorized': 1, 'invalid_lead': 0, 'redirect_link': 1}
    assert [lead['url'] for lead in snapshots[-1]['leads']] == [
        'https://www.sec.gov/nvda.htm',
        'https://investor.nvidia.com/news',
    ]
    assert [lead['published_at'] for lead in snapshots[-1]['leads']] == [
        '2026-08-01T00:00:00+00:00',
        '2026-08-02T00:00:00+00:00',
    ]


@pytest.mark.parametrize('url', [
    'http://0x7f.1/',
    'http://0177.0.0.1/',
    'http://0x7f000001/',
    'http://0x08.0x08.0x08.0x08/',
    'http://999.0.0.1/',
    'https://intranet/',
    'https://service.internal/health',
    'https://1.0.0.127.in-addr.arpa/',
    'https://app.test/',
    'https://app.invalid/',
    'https://sub.localhost/',
    'https://local\uff48ost/',
    'https://bad..example.com/',
    'https://\u200b.example.com/',
    'https://xn--zzzzzzzz.example/',
    'http://[64:ff9b::127.0.0.1]/',
    'http://[64:ff9b:1::127.0.0.1]/',
    'http://[100::1]/',
    'http://100.64.0.1/',
])
def test_search_validation_rejects_obfuscated_non_public_and_nat64_urls(url):
    provider = ClientSearchEvidenceProvider()
    with pytest.raises(ProviderFailure) as error:
        provider.validate_url(url)
    assert error.value.code == 'unsafe_evidence_url'
    with pytest.raises(ProviderFailure) as lead_error:
        normalize_search_lead({'title': 'Unsafe Lead', 'url': url})
    assert lead_error.value.code == 'unsafe_evidence_url'


@pytest.mark.parametrize('url', [
    'https://www.sec.gov./Archives/edgar/data/1/a.htm',
    'https://bücher.example/report',
    'https://[2606:4700:4700::1111]/',
    'https://8.8.8.8/dns',
])
def test_search_validation_allows_public_idn_trailing_dot_and_public_ip_literals(url):
    ClientSearchEvidenceProvider().validate_url(url)
    assert normalize_search_lead({'title': 'Public Lead', 'url': url}).url == url


def test_redirect_target_validation_rejects_nat64_and_discard_resolved_addresses():
    provider = ClientSearchEvidenceProvider()
    for address in ('64:ff9b::127.0.0.1', '64:ff9b:1::1', '100::1'):
        with pytest.raises(ProviderFailure) as error:
            provider.validate_redirect_target('https://public.example/next', resolver=lambda _host, _port, addr=address: [addr])
        assert error.value.code == 'unsafe_evidence_url'
