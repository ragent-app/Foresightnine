"""Offline integration: SEC release HTML through disclosure, valuation and peer tool."""
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace

from app.fin_data.adapters import sec_bank_disclosure
from app.fin_data.adapters.sec_events.pacing import SecPacingLimiter
from app.research_fin_tools import BankValuationPeerToolRequest, FinDataToolbox
from app.research_sources import ResearchSource
from app.research_peer_compare import PeerCompanySnapshot
import app.research_peer_compare as peer_compare


def _facts():
    instant = {'end': '2025-09-30', 'filed': '2025-10-15', 'form': '10-Q', 'val': 100_000_000}
    flow = {**instant, 'start': '2025-07-01', 'fy': 2025, 'fp': 'Q3'}
    return {
        'total_assets': {'Assets': {'USD': [instant]}},
        'deposits': {'Deposits': {'USD': [{**instant, 'val': 80_000_000}]}},
        'loans_gross': {'Loans': {'USD': [{**instant, 'val': 40_000_000}]}},
        'bank_net_revenue': {'RevenuesNetOfInterestExpense': {'USD': [flow]}},
        'cet1_ratio': {'CET1Ratio': {'pure': [{**instant, 'val': 0.13}]}},
    }


def _snapshot(symbol, cik):
    return PeerCompanySnapshot(cik=cik, symbol=symbol, company_name=symbol, sic='6021',
        sic_description='Commercial bank', latest_periodic_accession='0001000000-25-000001',
        latest_periodic_filed_at='2025-10-15', projection_version=6, facts_by_line=_facts())


def _submissions(cik):
    return {'cik': cik, 'filings': {'recent': {
        'accessionNumber': ['0001000000-25-000001', '0001000000-25-000002'],
        'filingDate': ['2025-10-15', '2025-10-16'], 'reportDate': ['2025-09-30'] * 2,
        'form': ['10-Q', '8-K'], 'items': [[], ['2.02']],
        'primaryDocument': ['filing.htm', 'release.htm']}}}


def _release(header, value):
    return f'''<html><body><table><tr><th>Consolidated results</th><th>3Q25</th><th>2Q25</th></tr>
      <tr><td></td>{header}</tr>
      <tr><td>Tangible book value per common share</td><td>$29.57</td><td>{value}</td></tr>
      <tr><td>Standardized CET1 capital ratio</td><td>11.0%</td><td>13.0%</td></tr>
      <tr><td>Return on average tangible common equity</td><td>20%</td><td>25%</td></tr>
    </table></body></html>'''


def test_html_disclosure_to_valuation_and_peer_excludes_invalid_bases(monkeypatch):
    snapshots = {s: _snapshot(s, str(i).zfill(10)) for i, s in enumerate(('GOOD', 'PERCENT', 'OLD'), 1)}
    releases = {
        'GOOD': _release('<td>2Q25</td><td>3Q 25</td>', '$30.60'),
        'PERCENT': _release('<td>2Q25</td><td>3Q 25</td>', '12.5%'),
        'OLD': _release('<td>1Q25</td><td>2Q 25</td>', '$30.60'),
    }
    # Fixed submissions and responses; no network or preconstructed bank disclosures.
    real_fetch = sec_bank_disclosure.fetch_bank_disclosure_snapshot
    observed = {}
    def fetch(*, snap, **kwargs):
        def transport(method, url, headers, body, timeout):
            if url.endswith('index.json'):
                return SimpleNamespace(status_code=200, headers={'content-type': 'application/json'},
                    body=json.dumps({'directory': {'item': [{'name': 'ex991.htm', 'type': 'EX-99.1'}]}}).encode())
            if url.endswith('ex991.htm'):
                return SimpleNamespace(status_code=200, headers={'content-type': 'text/html'}, body=releases[snap.symbol].encode())
            return SimpleNamespace(status_code=404, headers={}, body=b'')
        kwargs.update(submissions=_submissions(snap.cik), transport=transport, preloaded_10q_text='')
        result = real_fetch(snap=snap, **kwargs)
        observed[snap.symbol] = result[0]
        return result
    monkeypatch.setattr(sec_bank_disclosure, 'fetch_bank_disclosure_snapshot', fetch)
    monkeypatch.setattr(peer_compare, 'load_or_fetch_company_snapshot', lambda *, symbol, **kwargs: (snapshots[symbol], False))

    def persist(item, **kwargs):
        return ResearchSource(source_id='source', owner_id='test', title=item.title, text=item.text,
            url=item.url, kind=item.kind, is_simulated=False, published_at=item.published_at,
            obtained_at=datetime(2025, 10, 20, tzinfo=timezone.utc), content_sha256='a'*64,
            retrieval_method=kwargs['retrieval_method'], permissions=item.permissions)

    tb = FinDataToolbox(account_id='test', market_key=None, sec_user_agent='ForesightNine/1.0 test@example.com',
        persist=persist, sec_events_limiter=SecPacingLimiter(rate_per_sec=9, burst=100))
    tb._latest_quote_usd_for_symbol = lambda sym, **kw: (Decimal('61.20'), '2025-10-20', 'fixed')
    result = tb.bank_valuation_and_peer_compare(BankValuationPeerToolRequest(primary_symbol='GOOD',
        peer_symbols=['PERCENT', 'OLD'], as_of=date(2025, 10, 20), rationale='fixed offline integration')).result
    assert result['status'] == 'ok', result
    assert observed['GOOD'].tbvps_bridge.tbvps_reported_usd == '30.60'
    assert observed['PERCENT'].tbvps_bridge.tbvps_reported_usd is None
    assert observed['OLD'].tbvps_bridge.tbvps_reported_usd is None
    assert Decimal(result['primary_valuation']['p_to_tbv_primary']) == Decimal('2')
    companies = {r['symbol']: r for r in result['bank_companies']}
    assert Decimal(companies['GOOD']['p_to_tbv_primary']) == Decimal('2')
    assert 'p_to_tbv_primary' not in companies['PERCENT']
    assert 'p_to_tbv_primary' not in companies['OLD']
    assert Decimal(result['bank_peer_medians']['p_to_tbv_primary']) == Decimal('2')
    assert Decimal(companies['PERCENT']['cet1_standardized_pct']) == Decimal('13')
    assert Decimal(companies['OLD']['cet1_standardized_pct']) == Decimal('13')
