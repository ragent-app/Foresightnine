"""Integration tests for Phase A1 tool wiring: policy, credentials, registry, executor, and capabilities."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as api
from app.fin_data.models import Coverage, Provenance
from app.fin_data.policy import Denied, FinDataPolicy, PolicyRule, default_policy, load_policy
from app.market_credentials import (
    AccountMarketDataSettings,
    EarningsProviderUpdate,
    MARKET_PROVIDER_ORDER,
    MarketCredentialCipher,
    MarketDataConfigUpdate,
    resolve_fmp_key,
)
from app.model_protocol import ModelResponse, canonicalize_json_value
from app.openai_compatible import BinaryHttpResponse
from app.research_coverage_map import REASON_STATUS, TOOL_CATEGORY
from app.research_runtime import SYSTEM, TOOLS, run_research_agent
from app.research_sources import ResearchSource, SourcePermissions
from app.research_tool_executor import TOOL_HANDLERS
from app.research_tool_presentation import available_groups, definitions as presentation_definitions
from app.research_tools_registry import (
    ALL_TOOL_DEFINITIONS,
    BULK_COMPACT_TOOLS,
    COVERAGE_UPDATING_TOOLS,
    FIN_TOOLS,
    TOOL_INPUT_MODELS,
    TOOL_REGISTRY,
    TOOL_SPECS,
)
from app.storage import InMemoryDocumentRepository

from tests.fin_data.helpers import NOW, Reply, open_limiter
from tests.fin_runtime_helpers import ACCOUNT, make_toolbox, memory_persist
from tests.test_research_runtime import Model, context, turn

STOP = ModelResponse({'role': 'assistant', 'content': 'done'}, (), None, 'stop')

A1_TOOL_NAMES = {
    'sec_earnings_release',
    'sec_filing_tables',
    'sec_filing_sections',
    'sec_redline_diff',
    'sec_fulltext_search',
    'earnings_consensus',
    'earnings_transcript',
    'earnings_quality_diagnostics',
    'guidance_and_nongaap_reconciler',
    'valuation_and_implied_expectations',
}

A1_FIN_TOOL_NAMES = {
    'sec_earnings_release',
    'sec_filing_tables',
    'sec_filing_sections',
    'sec_redline_diff',
    'sec_fulltext_search',
    'earnings_consensus',
    'earnings_transcript',
}


class _RouterTransport:
    """URL pattern router for deterministic offline SEC & FMP HTTP simulation."""

    def __init__(self, routes: dict[str, Reply]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float):
        self.calls.append((method, url, dict(headers)))
        for pattern, reply in self.routes.items():
            if pattern in url:
                return reply
        return Reply(404, b'{}', {'content-type': 'application/json'})


def _stream_from_router(router: _RouterTransport):
    def _stream(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float, sink):
        reply = router(method, url, headers, body, timeout)
        if reply.status_code == 200:
            sink.write(reply.body)
        return BinaryHttpResponse(reply.status_code, b'', reply.headers)

    return _stream


def _json_reply(payload: object, status: int = 200) -> Reply:
    return Reply(status, json.dumps(payload).encode('utf-8'), {'content-type': 'application/json'})


def _html_reply(html: str, status: int = 200) -> Reply:
    return Reply(status, html.encode('utf-8'), {'content-type': 'text/html; charset=utf-8'})


def _xml_reply(xml: str, status: int = 200) -> Reply:
    return Reply(status, xml.encode('utf-8'), {'content-type': 'application/xml; charset=utf-8'})


def _last_tool_result(model: Model, index: int = 1) -> dict:
    tool_msgs = [m for m in model.inputs[index] if m.get('role') == 'tool']
    return json.loads(tool_msgs[-1]['content'])


# --------------------------------------------------------------------------- 1. Registry, presentation & coverage
def test_registry_presentation_and_coverage_map_wiring() -> None:
    assert A1_TOOL_NAMES <= set(TOOL_REGISTRY)
    assert set(TOOL_HANDLERS) == set(TOOL_REGISTRY)
    assert set(TOOL_INPUT_MODELS) == set(TOOL_REGISTRY)
    assert TOOLS[-1].name == 'submit_research'
    assert A1_FIN_TOOL_NAMES <= FIN_TOOLS
    assert FIN_TOOLS <= BULK_COMPACT_TOOLS
    assert A1_TOOL_NAMES <= COVERAGE_UPDATING_TOOLS

    dumped = json.dumps([t.__dict__ for t in TOOLS], default=str) + SYSTEM
    assert '2025' not in dumped and '2026' not in dumped

    for spec, definition in zip(TOOL_SPECS, ALL_TOOL_DEFINITIONS, strict=True):
        if spec.name in A1_TOOL_NAMES:
            assert definition.input_schema == canonicalize_json_value(spec.input_model.model_json_schema())
            assert spec.description.strip()

    caps_none = {'available_capabilities': {'filings_available': False, 'earnings_consensus_available': False, 'earnings_transcript_available': False}}
    groups_none = available_groups({'user': json.dumps(caps_none)})
    assert set(groups_none['earnings_analysis']) == {
        'earnings_quality_diagnostics',
        'guidance_and_nongaap_reconciler',
        'valuation_and_implied_expectations',
    }
    assert 'sec_filing_sections' in groups_none['disclosures']
    assert 'sec_redline_diff' in groups_none['disclosures']
    assert 'sec_earnings_release' not in groups_none['disclosures']

    caps_all = {'available_capabilities': {'filings_available': True, 'earnings_consensus_available': True, 'earnings_transcript_available': True}}
    state_all = {'user': json.dumps(caps_all), 'active_tool_groups': []}
    groups_all = available_groups(state_all)
    assert A1_FIN_TOOL_NAMES <= set(groups_all['disclosures']) | set(groups_all['earnings_analysis'])

    dynamic_model = SimpleNamespace(research_capacity=lambda: SimpleNamespace(dynamic_tool_definitions=True, native_tools=True))
    base_names = {t.name for t in presentation_definitions(state_all, dynamic_model)}
    assert 'earnings_consensus' not in base_names
    assert 'sec_earnings_release' not in base_names

    activated = presentation_definitions({**state_all, 'active_tool_groups': ['disclosures', 'earnings_analysis']}, dynamic_model)
    activated_names = {t.name for t in activated}
    assert A1_TOOL_NAMES <= activated_names

    assert TOOL_CATEGORY['sec_earnings_release'] == 'filings'
    assert TOOL_CATEGORY['sec_fulltext_search'] == 'filings'
    assert TOOL_CATEGORY['earnings_consensus'] == 'financial_statements'
    assert TOOL_CATEGORY['earnings_transcript'] == 'news_web'
    assert REASON_STATUS['byok_key_required'] == 'tool_unavailable'
    assert REASON_STATUS['no_earnings_releases'] == 'not_found'


# --------------------------------------------------------------------------- 2. Policy rules & EFTS scoping
def test_policy_fmp_and_cik_scoped_efts_gates() -> None:
    pol = default_policy()

    # Unscoped EFTS filing_search stays denied; single-CIK filing_search_cik is allowed.
    denied_global = pol.decide('sec_edgar', 'filing_search')
    assert isinstance(denied_global, Denied) and denied_global.reason == 'not_in_policy'
    allowed_cik = pol.decide('sec_edgar', 'filing_search_cik')
    assert not isinstance(allowed_cik, Denied) and allowed_cik.export is True

    # FMP rules: user_byok allowed with export=False, owner_scoped=True; platform denied by default.
    for cat in ('consensus', 'transcripts', 'earnings_consensus', 'earnings_transcript'):
        byok_perm = pol.decide('fmp', cat, credential_owner='user_byok')
        assert not isinstance(byok_perm, Denied)
        assert byok_perm.store and byok_perm.display and byok_perm.model_input
        assert byok_perm.export is False and byok_perm.owner_scoped is True

        plat_denied = pol.decide('fmp', cat, credential_owner='platform')
        assert isinstance(plat_denied, Denied) and plat_denied.reason == 'license_platform_key_not_permitted'

    opted_in = load_policy({'FORESIGHTNINE_FMP_ALLOW_PLATFORM_KEY': 'true'})
    for cat in ('consensus', 'transcripts', 'earnings_consensus', 'earnings_transcript'):
        plat_perm = opted_in.decide('fmp', cat, credential_owner='platform')
        assert not isinstance(plat_perm, Denied)
        assert plat_perm.export is False and plat_perm.owner_scoped is False


# --------------------------------------------------------------------------- 3. FMP credentials & routes
def test_fmp_credentials_resolution_and_routes(monkeypatch) -> None:
    repo = InMemoryDocumentRepository()
    cipher = MarketCredentialCipher(b'k' * 32)
    settings = AccountMarketDataSettings(repo, cipher)

    # Without any key or platform opt-in -> (None, None)
    assert resolve_fmp_key(
        'account_alpha_1',
        settings,
        env={'FORESIGHTNINE_FMP_API_KEY': 'platform-secret-9999'},
    ) == (None, None)

    # With platform opt-in env flag -> resolves platform key cleanly
    assert resolve_fmp_key(
        'account_alpha_1',
        settings,
        env={'FORESIGHTNINE_FMP_API_KEY': 'platform-secret-9999', 'FORESIGHTNINE_FMP_ALLOW_PLATFORM_KEY': 'true'},
    ) == ('platform-secret-9999', 'platform')

    # Account-level FMP BYOK key wins over platform key and never exposes plaintext in summaries/storage
    summary = settings.update_fmp(
        'account_alpha_1',
        EarningsProviderUpdate(enabled=True, api_key='user-fmp-byok-7777'),
        now=NOW,
    )
    assert summary.personal_key_configured is True
    assert summary.key_hint == '••••7777'
    assert 'user-fmp-byok-7777' not in summary.model_dump_json()
    assert 'user-fmp-byok-7777' not in repo['account_alpha_1'].model_dump_json()

    assert resolve_fmp_key(
        'account_alpha_1',
        settings,
        env={'FORESIGHTNINE_FMP_API_KEY': 'platform-secret-9999', 'FORESIGHTNINE_FMP_ALLOW_PLATFORM_KEY': 'true'},
    ) == ('user-fmp-byok-7777', 'user_byok')

    # Disabling FMP prevents fallback to platform key
    settings.update_fmp('account_alpha_1', EarningsProviderUpdate(enabled=False), now=NOW)
    assert resolve_fmp_key(
        'account_alpha_1',
        settings,
        env={'FORESIGHTNINE_FMP_API_KEY': 'platform-secret-9999', 'FORESIGHTNINE_FMP_ALLOW_PLATFORM_KEY': 'true'},
    ) == (None, None)

    # FastAPI endpoints preserve existing market provider config and wire FMP capabilities per account
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.delenv('FORESIGHTNINE_FMP_API_KEY', raising=False)
    monkeypatch.delenv('FORESIGHTNINE_FMP_ALLOW_PLATFORM_KEY', raising=False)
    api.reset_state()
    monkeypatch.setattr(api.market_data_settings, 'cipher', MarketCredentialCipher(b'm' * 32))
    client = TestClient(api.app)

    tok1 = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()['access_token']
    tok2 = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()['access_token']
    h1 = {'Authorization': f'Bearer {tok1}'}
    h2 = {'Authorization': f'Bearer {tok2}'}

    cfg_body = {
        'fallback_order': list(MARKET_PROVIDER_ORDER),
        'providers': [{'provider_id': pid, 'enabled': True} for pid in MARKET_PROVIDER_ORDER],
        'fmp': {'provider_id': 'fmp', 'enabled': True, 'api_key': 'account1-fmp-secret-4321'},
    }
    saved = client.put('/v1/account/market-data-config', json=cfg_body, headers=h1)
    assert saved.status_code == 200, saved.text
    assert 'account1-fmp-secret-4321' not in saved.text
    assert saved.json()['earnings_providers'][0]['personal_key_configured'] is True
    assert saved.json()['earnings_providers'][0]['key_hint'] == '••••4321'

    caps1 = client.get('/v2/research/capabilities', headers=h1).json()
    caps2 = client.get('/v2/research/capabilities', headers=h2).json()
    assert caps1['earnings_consensus_available'] is True and caps1['earnings_transcript_available'] is True
    assert caps1['fmp_credential_owner'] == 'user_byok'
    assert 'account1-fmp-secret-4321' not in str(caps1)
    assert caps2['earnings_consensus_available'] is False and caps2['earnings_transcript_available'] is False


# --------------------------------------------------------------------------- 4. Executor dispatch: all 5 SEC deep tools
def test_runtime_executes_all_five_sec_deep_tools() -> None:
    cik = '0000320193'
    acc_cur_8k = '0000320193-25-000105'
    acc_pri_8k = '0000320193-25-000075'
    acc_10q = '0000320193-25-000110'

    subs = {
        'cik': cik,
        'name': 'Apple Inc.',
        'tickers': ['AAPL'],
        'filings': {
            'recent': {
                'accessionNumber': [acc_cur_8k, acc_pri_8k],
                'filingDate': ['2025-10-31', '2025-07-31'],
                'reportDate': ['2025-09-27', '2025-06-28'],
                'form': ['8-K', '8-K'],
                'items': [['2.02', '9.01'], ['2.02', '9.01']],
                'primaryDocument': ['aapl-8k.htm', 'aapl-prior8k.htm'],
            }
        },
    }
    ex99_1_cur = (
        '<html><body><h1>Apple Reports Fourth Quarter Results</h1>'
        '<p>Record quarterly revenue of $102.5 billion, up 8% year over year.</p>'
        '<h2>Reconciliation of GAAP to Non-GAAP Financial Measures</h2>'
        '<table><tr><th>Metric</th><th>Q4</th></tr><tr><td>GAAP Operating Income</td><td>$31,200</td></tr>'
        '<tr><td>Stock-based compensation</td><td>$2,800</td></tr><tr><td>Non-GAAP Operating Income</td><td>$34,000</td></tr></table>'
        '<h2>Business Outlook</h2>'
        '<p>For the December quarter, we expect revenue growth between 6% and 8% and gross margin between 46.5% and 47.5%.</p>'
        '</body></html>'
    )
    ex99_2_cur = '<html><body><p>CFO Commentary: Services reached an all-time revenue record.</p></body></html>'
    ex99_1_pri = '<html><body><h1>Apple Reports Third Quarter Results</h1><p>Revenue was $94.9 billion.</p></body></html>'

    filing_summary = """<?xml version="1.0"?>
    <FilingSummary><MyReports>
      <Report><ShortName>Condensed Consolidated Statements of Operations</ShortName><LongName>0002 - Statement - Statements of Operations</LongName><MenuCategory>Statements</MenuCategory><Position>2</Position><HtmlFileName>R2.htm</HtmlFileName></Report>
      <Report><ShortName>Note 3 Revenue</ShortName><LongName>0008 - Disclosure - Revenue</LongName><MenuCategory>Notes</MenuCategory><Position>8</Position><HtmlFileName>R8.htm</HtmlFileName></Report>
    </MyReports></FilingSummary>"""

    r2_html = """<html><body><table class="report">
      <tr><th class="tl" colspan="3"><div><strong>Condensed Consolidated Statements of Operations - USD ($)</strong><br/>shares in Millions, $ in Millions</div></th></tr>
      <tr><th class="th"></th><th class="th">Sep 27, 2025</th><th class="th">Sep 28, 2024</th></tr>
      <tr><td class="pl"><a onclick="Show.showAR(this,'defref_us-gaap_Revenues');">Net sales</a></td><td class="nump">$ 102,500</td><td class="nump">$ 94,930</td></tr>
      <tr><td class="pl"><a onclick="Show.showAR(this,'defref_us-gaap_NetIncomeLoss');">Net income</a></td><td class="nump">26,100</td><td class="nump">23,600</td></tr>
    </table></body></html>"""

    prior_10q_html = (
        '<html><body>'
        '<h2>Item 1A. Risk Factors</h2>'
        '<p>On June 28, 2025, our quarterly cash dividend was $0.25 per share and revenue was $94,930 million.</p>'
        '<p>Global component supply concentration could delay product launches.</p>'
        '<h2>Item 2. Management\'s Discussion and Analysis of Financial Condition and Results of Operations</h2>'
        '<p>Services net sales grew strongly during Q3 FY2025.</p>'
        '<h3>Liquidity and Capital Resources</h3>'
        '<p>We held $140 billion in cash and marketable securities.</p>'
        '<h3>Critical Accounting Estimates</h3>'
        '<p>Warranty reserve estimates rely on historical failure rates.</p>'
        '</body></html>'
    )
    current_10q_html = (
        '<html><body>'
        '<h2>Item 1A. Risk Factors</h2>'
        '<p>On September 27, 2025, our quarterly cash dividend was $0.26 per share and revenue was $102,500 million.</p>'
        '<p>Global component supply concentration could delay product launches.</p>'
        '<p>Newly enacted cross-border semiconductor export restrictions may adversely affect regional shipments.</p>'
        '<h2>Item 2. Management\'s Discussion and Analysis of Financial Condition and Results of Operations</h2>'
        '<p>Services net sales grew strongly during Q4 FY2025.</p>'
        '<h3>Liquidity and Capital Resources</h3>'
        '<p>We held $145 billion in cash and marketable securities.</p>'
        '<h3>Critical Accounting Estimates</h3>'
        '<p>Warranty reserve estimates rely on historical failure rates.</p>'
        '</body></html>'
    )

    efts_payload = {
        'hits': {
            'total': {'value': 2},
            'hits': [
                {
                    '_id': f'{acc_10q}:aapl-10q.htm',
                    '_source': {
                        'ciks': [cik],
                        'display_names': ['Apple Inc.'],
                        'form': '10-Q',
                        'file_date': '2025-11-01',
                        'adsh': acc_10q,
                    },
                },
                {
                    '_id': '0000789019-25-000010:msft-10q.htm',
                    '_source': {
                        'ciks': ['0000789019'],
                        'display_names': ['Microsoft Corp'],
                        'form': '10-Q',
                        'file_date': '2025-10-30',
                        'adsh': '0000789019-25-000010',
                    },
                },
            ],
        }
    }

    router = _RouterTransport({
        f'https://data.sec.gov/submissions/CIK{cik}.json': _json_reply(subs),
        f"/320193/{acc_cur_8k.replace('-', '')}/index.json": _json_reply({
            'directory': {'item': [{'name': 'ex99-1.htm'}, {'name': 'ex99-2.htm'}]}
        }),
        f"/320193/{acc_cur_8k.replace('-', '')}/ex99-1.htm": _html_reply(ex99_1_cur),
        f"/320193/{acc_cur_8k.replace('-', '')}/ex99-2.htm": _html_reply(ex99_2_cur),
        f"/320193/{acc_pri_8k.replace('-', '')}/index.json": _json_reply({
            'directory': {'item': [{'name': 'ex99-1.htm'}]}
        }),
        f"/320193/{acc_pri_8k.replace('-', '')}/ex99-1.htm": _html_reply(ex99_1_pri),
        f"/320193/{acc_10q.replace('-', '')}/FilingSummary.xml": _xml_reply(filing_summary),
        f"/320193/{acc_10q.replace('-', '')}/R2.htm": _html_reply(r2_html),
        'https://www.sec.gov/Archives/edgar/data/320193/prior10q.htm': _html_reply(prior_10q_html),
        'https://www.sec.gov/Archives/edgar/data/320193/current10q.htm': _html_reply(current_10q_html),
        'efts.sec.gov/LATEST/search-index': _json_reply(efts_payload),
    })

    persist = memory_persist()
    box = make_toolbox(sec=router, sec_stream=_stream_from_router(router), persist=persist)

    # Sequential scripted run calling all 5 SEC deep tools + unscoped filings denial check
    class _Script:
        def __init__(self):
            self.step = 0
            self.results: list[dict] = []
            self.sec_src_ids: list[str] = []

        def complete(self, messages, tools, **kwargs):
            if self.step > 0:
                res = json.loads([m for m in messages if m.get('role') == 'tool'][-1]['content'])
                self.results.append(res)
                if res.get('source_id') and self.step in (4, 5):
                    self.sec_src_ids.append(res['source_id'])
            self.step += 1
            if self.step == 1:
                return turn('sec_earnings_release', {'cik': cik, 'pairing_mode': 'sequential', 'rationale': 'earnings release'}, 'c1')
            if self.step == 2:
                return turn('sec_filing_tables', {'cik': cik, 'accession': acc_10q, 'rationale': 'table catalog'}, 'c2')
            if self.step == 3:
                return turn('sec_filing_tables', {'cik': cik, 'accession': acc_10q, 'r_file': 'R2.htm', 'rationale': 'income table'}, 'c3')
            if self.step == 4:
                return turn('sec_filing_sections', {'cik': cik, 'url': 'https://www.sec.gov/Archives/edgar/data/320193/prior10q.htm', 'form': '10-Q', 'rationale': 'prior sections'}, 'c4')
            if self.step == 5:
                return turn('sec_filing_sections', {'cik': cik, 'url': 'https://www.sec.gov/Archives/edgar/data/320193/current10q.htm', 'form': '10-Q', 'rationale': 'current sections'}, 'c5')
            if self.step == 6:
                return turn('sec_redline_diff', {'current_source_id': self.sec_src_ids[1], 'prior_source_id': self.sec_src_ids[0], 'rationale': 'redline'}, 'c6')
            if self.step == 7:
                return turn('sec_fulltext_search', {'cik': cik, 'query': 'export restrictions', 'forms': ['10-Q'], 'rationale': 'cik search'}, 'c7')
            if self.step == 8:
                return turn('filings', {'action': 'search', 'query': 'export restrictions', 'rationale': 'unscoped blocked'}, 'c8')
            return STOP

    script = _Script()
    out = run_research_agent(question='AAPL SEC deep check', sources=[], model=script, context=context(), fin_data=box)
    r_er, r_cat, r_r2, r_sec1, r_sec2, r_red, r_efts, r_unscoped = script.results

    assert r_er['status'] == 'ok' and r_er['source_id'] in out.source_ids
    assert r_er['current_period']['nongaap_table_count'] >= 1 and r_er['current_period']['guidance_passage_count'] >= 1
    assert r_er['prior_period']['accession'] == acc_pri_8k

    assert r_cat['status'] == 'ok' and r_cat['report_count'] == 2
    assert r_cat['statements'][0]['r_file'] == 'R2.htm'

    assert r_r2['status'] == 'ok' and r_r2['currency'] == 'USD' and r_r2['unit_scale'] == 'millions'
    assert any(
        k.endswith('Sep 27, 2025') and v == '102500'
        for row in r_r2['rows']
        for k, v in row['numeric_values'].items()
    )
    assert r_r2['source_id'] in out.source_ids

    assert r_sec1['status'] == 'ok' and 'item_1a_risk_factors' in r_sec1['section_order']
    assert 'liquidity_and_capital_resources' in r_sec2['section_order']

    assert r_red['status'] == 'ok'
    risk_diff = r_red['sections']['item_1a_risk_factors']
    assert risk_diff['filtered_noise_count'] >= 1
    assert any('semiconductor export restrictions' in item['current_text'] for item in risk_diff['added'])

    assert r_efts['status'] == 'partial'  # 1 out-of-scope MSFT hit dropped
    assert len(r_efts['hits']) == 1 and r_efts['hits'][0]['entity_id'] == f'cik:{cik}'
    assert any('ciks=0000320193' in url for _, url, _ in router.calls)

    # Unscoped filings search stays denied by default_policy
    assert r_unscoped['status'] == 'denied' and r_unscoped['reason'] == 'not_in_policy'


# --------------------------------------------------------------------------- 5. Executor dispatch: FMP tools
def test_runtime_executes_fmp_tools_with_byok_platform_and_graceful_degradation(monkeypatch) -> None:
    monkeypatch.setenv('FORESIGHTNINE_FMP_API_KEY', 'env-plat-should-not-leak')
    monkeypatch.delenv('FORESIGHTNINE_FMP_ALLOW_PLATFORM_KEY', raising=False)

    fmp_router = _RouterTransport({
        '/stable/analyst-estimates': _json_reply([
            {'symbol': 'NVDA', 'date': '2026-01-25', 'revenueAvg': 38200000000, 'epsAvg': 0.85, 'numAnalystsEps': 40}
        ]),
        '/stable/earnings': _json_reply([]),
        '/stable/earning-call-transcript': _json_reply([
            {
                'symbol': 'NVDA',
                'year': 2025,
                'quarter': 4,
                'date': '2025-02-26 17:00:00',
                'content': 'Presentation\nJensen Huang - CEO: Demand remains extraordinary.\nQuestion-and-Answer Session\nAnalyst: Margin outlook?\nColette Kress - CFO: Mid-70s gross margins.',
            }
        ]),
    })

    # A) Unconfigured FMP key -> graceful degradation with SEC fallback, zero HTTP calls
    box_none = make_toolbox(fmp_key=None, fmp_transport=fmp_router)
    m_none = Model([
        turn('earnings_consensus', {'symbol': 'NVDA', 'rationale': 'consensus'}, 'c1'),
        turn('earnings_transcript', {'symbol': 'NVDA', 'rationale': 'transcript'}, 'c2'),
        STOP,
    ])
    run_research_agent(question='NVDA earnings', sources=[], model=m_none, context=context(), fin_data=box_none)
    msgs_none = [json.loads(m['content']) for m in m_none.inputs[-1] if m.get('role') == 'tool']
    assert msgs_none[0]['status'] == 'unavailable' and msgs_none[0]['reason'] == 'byok_key_required'
    assert msgs_none[0]['degraded'] is True and any(a['action'] == 'sec_earnings_release' for a in msgs_none[0]['suggested_actions'])
    assert msgs_none[1]['status'] == 'unavailable' and msgs_none[1]['reason'] == 'byok_key_required'
    assert not fmp_router.calls

    # B) Platform owner under default_policy() -> denied
    box_plat_denied = make_toolbox(fmp_key='plat-secret-0001', fmp_owner='platform', fmp_transport=fmp_router, policy=default_policy())
    m_pd = Model([turn('earnings_consensus', {'symbol': 'NVDA', 'rationale': 'plat check'}, 'c1'), STOP])
    run_research_agent(question='NVDA consensus', sources=[], model=m_pd, context=context(), fin_data=box_plat_denied)
    assert _last_tool_result(m_pd)['status'] == 'denied'
    assert _last_tool_result(m_pd)['reason'] == 'license_platform_key_not_permitted'
    assert not fmp_router.calls

    # C) User BYOK key -> persists non-exportable, owner-scoped sources, zero raw key leakage
    persist = memory_persist()
    box_byok = make_toolbox(fmp_key='user-fmp-byok-secret-99', fmp_owner='user_byok', fmp_transport=fmp_router, persist=persist)
    m_ok = Model([
        turn('earnings_consensus', {'symbol': 'NVDA', 'rationale': 'consensus'}, 'c1'),
        turn('earnings_transcript', {'symbol': 'NVDA', 'fiscal_year': 2025, 'fiscal_quarter': 4, 'rationale': 'transcript'}, 'c2'),
        STOP,
    ])
    out_ok = run_research_agent(question='NVDA consensus and transcript', sources=[], model=m_ok, context=context(), fin_data=box_byok)
    res_con, res_tr = [json.loads(m['content']) for m in m_ok.inputs[-1] if m.get('role') == 'tool']
    assert res_con['status'] == 'ok' and res_con['source_id'] in out_ok.source_ids
    assert res_con['permissions']['export'] is False and res_con['permissions']['owner_scoped'] is True
    assert res_tr['status'] == 'ok' and res_tr['source_id'] in out_ok.source_ids and res_tr['section_count'] >= 2
    assert 'user-fmp-byok-secret-99' not in json.dumps([res_con, res_tr])
    assert all('user-fmp-byok-secret-99' not in src.model_dump_json() for src in persist.store)
    assert all(src.permissions.export is False for src in persist.store)

    # D) Opted-in platform policy -> allowed with export=False, owner_scoped=False
    opted_policy = load_policy({'FORESIGHTNINE_FMP_ALLOW_PLATFORM_KEY': 'true'})
    box_plat_ok = make_toolbox(fmp_key='plat-dev-secret-11', fmp_owner='platform', fmp_transport=fmp_router, policy=opted_policy)
    assert box_plat_ok.earnings_capabilities()['earnings_consensus_available'] is True
    m_po = Model([turn('earnings_consensus', {'symbol': 'NVDA', 'rationale': 'dev plat check'}, 'c1'), STOP])
    run_research_agent(question='NVDA consensus plat', sources=[], model=m_po, context=context(), fin_data=box_plat_ok)
    res_po = _last_tool_result(m_po)
    assert res_po['status'] == 'ok' and res_po['permissions']['export'] is False and res_po['permissions']['owner_scoped'] is False

    # E) Regression: /stable/earnings surprise rows with period_end=None do not crash on .isoformat()
    from app.research_fin_tools import EarningsConsensusRequest
    surprise_only_router = _RouterTransport({
        '/stable/analyst-estimates': _json_reply([]),
        '/stable/earnings': _json_reply([{'symbol': 'NVDA', 'date': '2025-11-20', 'epsEstimated': 0.75, 'epsActual': 0.81}]),
    })
    surprise_persist = memory_persist()
    box_surprise = make_toolbox(fmp_key='user-surprise-key-1', fmp_owner='user_byok', fmp_transport=surprise_only_router, persist=surprise_persist)
    outcome_surprise = box_surprise.earnings_consensus(EarningsConsensusRequest(symbol='NVDA', rationale='surprise fallback'), {})
    assert outcome_surprise.result['status'] == 'ok'
    assert 'period_end - | reported 2025-11-20' in surprise_persist.store[0].text


# --------------------------------------------------------------------------- 6. Executor dispatch: all 3 diagnostics
def test_runtime_executes_all_three_diagnostics_tools() -> None:
    eq_args = {
        'symbol': 'TECH',
        'current': {
            'period_label': 'Q3',
            'currency': 'USD',
            'monetary_unit': 'USD millions',
            'revenue': '1000',
            'cost_of_revenue': '400',
            'operating_income': '250',
            'net_income': '200',
            'operating_cash_flow': '280',
            'capex': '40',
            'sbc': '30',
        },
        'rationale': 'earnings quality checks',
    }
    gn_args = {
        'symbol': 'TECH',
        'period_label': 'Q3',
        'guidance_metrics': [
            {
                'metric_name': 'revenue',
                'actual': {'value': '1000', 'fact_id': 'rev_act', 'unit': 'USD millions', 'currency': 'USD'},
                'prior_guidance': {
                    'low': {'value': '960', 'fact_id': 'pg_low', 'unit': 'USD millions', 'currency': 'USD'},
                    'high': {'value': '990', 'fact_id': 'pg_high', 'unit': 'USD millions', 'currency': 'USD'},
                },
                'consensus': {'value': '980', 'fact_id': 'con_rev', 'unit': 'USD millions', 'currency': 'USD'},
                'new_guidance': {
                    'low': {'value': '1020', 'fact_id': 'ng_low', 'unit': 'USD millions', 'currency': 'USD'},
                    'high': {'value': '1050', 'fact_id': 'ng_high', 'unit': 'USD millions', 'currency': 'USD'},
                },
                'new_guidance_benchmark': {'value': '990', 'fact_id': 'ng_bench', 'unit': 'USD millions', 'currency': 'USD'},
            }
        ],
        'nongaap_bridges': [
            {
                'metric_name': 'Net Income',
                'period_label': 'Q3',
                'gaap_value': '200',
                'reported_non_gaap_value': '230',
                'adjustments': [{'label': 'Stock-based compensation', 'amount': '30'}],
            }
        ],
        'rationale': 'guidance beat/raise and GAAP bridge',
    }
    val_args = {
        'symbol': 'TECH',
        'sic': '7372',
        'currency': 'USD',
        'monetary_unit': 'USD millions',
        'close_price': {'value': '100', 'fact_id': 'px', 'unit': 'USD per share', 'currency': 'USD'},
        'diluted_shares': {'value': '100', 'fact_id': 'sh', 'unit': 'millions shares'},
        'cash': '1000',
        'total_debt': '500',
        'ttm_revenue': '4000',
        'ttm_ebitda': '1000',
        'ttm_ebit': '850',
        'ttm_net_income': '700',
        'ttm_fcf': '650',
        'rationale': 'multiples and reverse DCF',
    }

    m = Model([
        turn('earnings_quality_diagnostics', eq_args, 'd1'),
        turn('guidance_and_nongaap_reconciler', gn_args, 'd2'),
        turn('valuation_and_implied_expectations', val_args, 'd3'),
        STOP,
    ])
    run_research_agent(question='TECH diagnostics', sources=[], model=m, context=context(), fin_data=make_toolbox())
    r_eq, r_gn, r_val = [json.loads(msg['content']) for msg in m.inputs[-1] if msg.get('role') == 'tool']

    assert r_eq['status'] == 'ok' and r_eq['analysis_id'].startswith('earnings-diag-')
    assert Decimal(str(r_eq['earnings_quality']['reported_or_derived_fcf']['value'])) == Decimal('240.00')

    assert r_gn['status'] == 'ok'
    rev_eval = r_gn['guidance_evaluations'][0]
    assert rev_eval['vs_prior_guidance']['outcome'] == 'Beat'
    assert rev_eval['new_guidance_direction']['direction'] == 'Raise'
    assert r_gn['nongaap_verifications'][0]['ties'] is True

    assert r_val['status'] == 'ok'
    assert Decimal(str(r_val['enterprise_value']['value'])) == Decimal('9500.00')
    assert r_val['reverse_dcf']['status'] == 'computed'
