"""A-share routing through the existing fin tools: resolve_entity, market_data, company_financials, capabilities."""
from __future__ import annotations

import json
from datetime import date

import pytest

from app.fin_data import cn
from app.fin_data.adapters.baostock import BaoStockFailure
from app.fin_data.policy import FinDataPolicy, PolicyRule, default_policy
from app.research_fin_tools import CompanyFinancialsRequest, MarketDataRequest, ResolveEntityRequest
from tests.fin_data.cn_helpers import TOKEN, FakeBaoStock, TushareFake, standard_tushare, ts_reply
from tests.fin_runtime_helpers import FakeTransport, Reply, json_reply, make_toolbox, memory_persist

PX = {"start_date": "2025-11-17", "end_date": "2025-11-19", "rationale": "prices"}


@pytest.fixture(autouse=True)
def fresh_cache():
    cn.clear_list_cache()
    yield
    cn.clear_list_cache()


def box(*, tushare=None, baostock=None, market_key=None, persist=None, policy=None, **kw):
    return make_toolbox(market_key=market_key, user_agent=None, persist=persist or memory_persist(), policy=policy,
                        tushare_key=TOKEN if tushare is not None else None, tushare_transport=tushare,
                        baostock_runner=baostock, **kw)


def spec_for(b, **req):
    spec = b.market_spec(MarketDataRequest(**req), {})
    assert not isinstance(spec, dict), spec
    return spec


# ---------------------------------------------------------------- market_data
def test_market_data_routes_cn_entity_to_tushare_and_persists_provenance_without_token():
    fake, persist = standard_tushare(), memory_persist()
    b = box(tushare=fake, persist=persist)
    out = b.market_data(spec_for(b, entity_id="SSE:600519", **PX))
    r = out.result
    assert r["status"] == "ok" and r["provenance"]["provider"] == "tushare" and fake.apis() == ["daily", "adj_factor"]
    src = persist.store[0]
    prov = src.provenance
    assert prov["provider"] == "tushare" and prov["credential_owner"] == "user_byok" and prov["owner_scoped"] is True
    assert prov["owner_account_id"] == "acct-1" and prov["currency"] == "CNY" and prov["adjustment"] == "none"
    assert prov["exchange"] == "SSE" and prov["coverage"]["currency"] == "CNY"
    assert "2025-11-17" in src.text and "ADJF 8.25" in src.text and "CNY" in src.text and "@SSE" in src.text
    assert src.permissions.export is False and src.permissions.store is True and src.kind == "market_data"
    assert TOKEN not in src.model_dump_json() and TOKEN not in json.dumps(r)


def test_cn_symbol_forms_and_exchange_inference_in_market_spec():
    b = box(tushare=standard_tushare())
    for req in ({"symbol": "600519"}, {"symbol": "600519.SH"}, {"symbol": "600519", "exchange": "SSE"}, {"entity_id": "SSE:600519"},
                {"symbol": "sh.600519"}):
        spec = spec_for(b, **req, **PX)
        assert (spec.symbol, spec.exchange, spec.entity_id, spec.currency) == ("600519", "SSE", "SSE:600519", "CNY")
    spec = spec_for(b, symbol="300750", **PX)
    assert (spec.exchange, spec.entity_id) == ("SZSE", "SZSE:300750")
    assert spec_for(b, symbol="430047", **PX).exchange == "BJSE"
    assert spec_for(b, symbol="ANT", exchange="SSE", **PX).entity_id == "SSE:ANT"  # not an A-share code: untouched


def test_lookback_uses_the_exchange_session_clamp():
    b = box(tushare=standard_tushare())
    spec = spec_for(b, entity_id="SZSE:000001", lookback="5d", rationale="x")
    assert spec.end == date(2026, 11, 19) and spec.exchange == "SZSE"


def test_intraday_is_not_supported_for_a_shares_without_calling_a_provider():
    fake = standard_tushare()
    b = box(tushare=fake)
    out = b.market_data(spec_for(b, entity_id="SSE:600519", interval="5min", **PX))
    assert out.result["status"] == "unavailable" and out.result["reason"] == "intraday_not_supported_cn" and not fake.calls


def test_tushare_failure_falls_back_to_baostock_with_a_note():
    fake = standard_tushare(daily=ts_reply(code=2002, msg="抱歉，您没有接口访问权限"))
    runner, persist = FakeBaoStock(), memory_persist()
    b = box(tushare=fake, baostock=runner, persist=persist)
    out = b.market_data(spec_for(b, entity_id="SSE:600519", **PX))
    r = out.result
    assert r["status"] == "ok" and r["provenance"]["provider"] == "baostock"
    prov = persist.store[0].provenance
    assert prov["credential_owner"] == "keyless_public_source" and prov["owner_scoped"] is False
    assert any("tushare failed (tushare_permission_insufficient)" in n for n in prov["coverage"]["notes"])
    assert persist.store[0].permissions.export is False
    assert "ADJF 8.250000" in persist.store[0].text


def test_baostock_only_when_no_token_and_tushare_error_without_fallback_is_reported():
    runner = FakeBaoStock()
    b = box(baostock=runner)
    assert b.market_data(spec_for(b, symbol="600519", **PX)).result["provenance"]["provider"] == "baostock"
    only_ts = box(tushare=standard_tushare(daily=ts_reply(code=2002, msg="没有接口访问权限")))
    out = only_ts.market_data(spec_for(only_ts, symbol="600519", **PX)).result
    assert out["status"] == "failed" and out["reason"] == "tushare_permission_insufficient" and out["source_failures"]


def test_no_cn_source_gives_a_clear_message_and_a_twelve_key_still_serves_non_cn_symbols():
    b = box()
    out = b.market_data(spec_for(b, symbol="600519", **PX)).result
    assert out["status"] == "unavailable" and out["reason"] == "cn_market_data_unavailable" and "Tushare" in out["message"]
    td = FakeTransport(json_reply("td_time_series_ipo.json"))
    b2 = make_toolbox(market_key="byok-secret-123", twelve=td, baostock_runner=None)
    assert b2.market_data(spec_for(b2, entity_id="SSE:ANT", start_date="2026-11-16", end_date="2026-11-19", rationale="p")).result["status"] == "ok"
    assert td.calls  # US/non-A-share entities keep using Twelve Data


def test_cn_entities_prefer_cn_sources_over_a_twelve_key_and_us_entities_never_touch_them():
    fake, td = standard_tushare(), FakeTransport(json_reply("td_time_series_ipo.json"))
    b = box(tushare=fake, market_key="byok-secret-123", twelve=td)
    assert b.market_data(spec_for(b, entity_id="SSE:600519", **PX)).result["provenance"]["provider"] == "tushare" and not td.calls
    out = b.market_data(spec_for(b, entity_id="NASDAQ:AAPL", start_date="2026-11-16", end_date="2026-11-19", rationale="p"))
    assert td.calls and not [c for c in fake.calls if c["api_name"] == "daily" and c["params"]["ts_code"].startswith("AAPL")]
    assert out.result["provenance"]["provider"] == "twelve_data"


def test_policy_denial_of_tushare_uses_baostock_and_denying_both_is_reported():
    deny_ts = FinDataPolicy(rules=[*default_policy().rules,
                                   PolicyRule(provider="tushare", category="bars_daily", credential_owner="user_byok", deny=True, deny_reason="denied_by_policy")])
    fake = standard_tushare()
    b = box(tushare=fake, baostock=FakeBaoStock(), policy=deny_ts)
    assert b.market_data(spec_for(b, entity_id="SSE:600519", **PX)).result["provenance"]["provider"] == "baostock" and not fake.calls
    none = box(tushare=fake, baostock=FakeBaoStock(), policy=FinDataPolicy(rules=[]))
    assert none.market_data(spec_for(none, entity_id="SSE:600519", **PX)).result["reason"] == "cn_market_data_unavailable"


def test_baostock_timeout_is_unknown_and_not_replayed():
    runner = FakeBaoStock(fail=BaoStockFailure("source_timeout", "timed out", timed_out=True))
    b = box(baostock=runner)
    r = b.market_data(spec_for(b, symbol="600519", **PX)).result
    assert r["status"] == "unknown" and r["external_state"] == "unknown" and len(runner.requests) == 1


# ---------------------------------------------------------------- company_financials
def fin(**kw):
    return CompanyFinancialsRequest(rationale="financials", **kw)


def test_company_financials_for_a_share_persists_cumulative_lines_with_basis_and_standard():
    fake, persist = standard_tushare(), memory_persist()
    b = box(tushare=fake, persist=persist)
    out = b.company_financials(fin(entity_id="SSE:600519", lines=["revenue", "net_income"], periods="last_4_quarters"), {})
    r = out.result
    assert r["status"] == "ok" and r["provider"] == "tushare" and r["entity_id"] == "SSE:600519" and fake.apis() == ["income"]
    src = persist.store[0]
    assert src.retrieval_method == "structured_financials" and src.kind == "official"
    lines = src.text.splitlines()
    h1 = next(l for l in lines if "income.revenue" in l and "2026-01-01..2026-06-30" in l)
    assert "fiscal 2026H1 YTD" in h1 and "basis ytd_cumulative" in h1 and "value 260.0 CNY" in h1 and "tushare-cas:total_revenue" in h1
    assert "filed 2026-08-20" in h1
    fy = next(l for l in lines if "2025-12-31" in l and "income.revenue" in l)
    assert "value 565.0" in fy and "superseded: 2026-03-20:560.0" in fy
    prov = src.provenance
    assert "企业会计准则" in prov["accounting_standard"] and prov["credential_owner"] == "user_byok" and prov["currency"] == "CNY"
    assert any("企业会计准则" in n for n in prov["coverage"]["notes"]) and not any("derived: " in l for l in lines)
    assert TOKEN not in src.model_dump_json()


def test_derive_flag_produces_marked_single_quarters_for_a_shares():
    b = box(tushare=standard_tushare(), persist=memory_persist())
    out = b.company_financials(fin(entity_id="SSE:600519", lines=["revenue"], periods="last_4_quarters", derive_q4=True), {})
    text = out.source.text
    derived = [l for l in text.splitlines() if "derived:" in l]
    assert derived and all("basis single_quarter_derived" in l for l in derived)
    assert any("2026-07-01..2026-09-30" in l and "value 170.0" in l for l in derived)


def test_ticker_and_exchange_prefixed_codes_route_to_cn_and_cik_stays_sec():
    fake = standard_tushare()
    b = box(tushare=fake)
    for key in ({"ticker": "600519.SH"}, {"ticker": "sh.600519"}, {"entity_id": "SSE:600519"}):
        assert b.company_financials(fin(lines=["net_income"], **key), {}).result["entity_id"] == "SSE:600519"
    sec = FakeTransport(Reply())
    sec_box = make_toolbox(market_key=None, sec=sec, baostock_runner=None)
    out = sec_box.company_financials(fin(cik="320193", lines=["revenue"]), {})
    assert sec.calls and out.result.get("entity_id") != "SSE:600519"


def test_financials_falls_back_to_baostock_ratios_and_flags_the_limit():
    b = box(tushare=standard_tushare(income=ts_reply(code=2002, msg="没有接口访问权限"), balancesheet=ts_reply(code=2002, msg="没有接口访问权限"),
                                     cashflow=ts_reply(code=2002, msg="没有接口访问权限")),
            baostock=FakeBaoStock(), persist=memory_persist())
    out = b.company_financials(fin(entity_id="SSE:600519", lines=["revenue", "total_assets"], periods="last_4_quarters"), {})
    r = out.result
    assert r["status"] == "partial" and r["provider"] == "baostock" and "total_assets" in r["message"]
    assert any("tushare failed (tushare_permission_insufficient)" in n for n in r["coverage"]["notes"])
    assert "baostock:MBRevenue" in out.source.text


def test_financials_without_any_cn_source_and_when_disabled():
    out = box().company_financials(fin(entity_id="SSE:600519"), {})
    assert out.result["status"] == "unavailable" and out.result["reason"] == "cn_financials_unavailable"
    off = box(tushare=standard_tushare(), disabled_reason="historical_cutoff")
    assert off.company_financials(fin(entity_id="SSE:600519"), {}).result == {"status": "restricted", "reason": "historical_cutoff"}


# ---------------------------------------------------------------- resolve_entity
def test_resolve_entity_by_chinese_name_without_sec_or_twelve_and_entity_flows_to_market_data():
    fake = standard_tushare()
    b = box(tushare=fake)
    out = b.resolve_entity(ResolveEntityRequest(query="平安", rationale="find"), {})
    r = out.result
    assert r["status"] == "ok" and r["reason"] == "multiple_candidates"
    assert [c["entity_id"] for c in r["candidates"]] == ["SZSE:000001", "SSE:601318"] and not any(c["unique"] for c in r["candidates"])
    assert r["candidates"][0]["security"]["currency"] == "CNY" and r["candidates"][0]["sources"] == ["tushare"]
    assert "SSE:601318" in out.entities and out.entities["SSE:601318"]["company"] == "中国平安"
    one = b.resolve_entity(ResolveEntityRequest(query="600519.SH", rationale="find"), {}).result
    assert [c["entity_id"] for c in one["candidates"]] == ["SSE:600519"] and one["candidates"][0]["unique"]
    assert one["candidates"][0]["identifiers"]["ts_code"] == "600519.SH"


def test_resolve_entity_does_not_query_cn_sources_for_us_queries_and_blocks_without_sources():
    fake = standard_tushare()
    b = box(tushare=fake)
    assert b.resolve_entity(ResolveEntityRequest(query="AAPL", rationale="find"), {}).result["reason"] == "no_identity_source_available"
    assert not fake.calls
    assert box().resolve_entity(ResolveEntityRequest(query="600519", rationale="find"), {}).result["reason"] == "no_identity_source_available"


def test_resolve_entity_uses_baostock_listing_without_a_token():
    out = box(baostock=FakeBaoStock()).resolve_entity(ResolveEntityRequest(query="平安银行", rationale="find"), {})
    assert [c["entity_id"] for c in out.result["candidates"]] == ["SZSE:000001"] and out.result["candidates"][0]["sources"] == ["baostock"]


# ---------------------------------------------------------------- capabilities
def test_capabilities_report_cn_flags_and_active_provider():
    caps = box().capabilities()
    assert (caps["cn_market_data_available"], caps["cn_financials_available"], caps["cn_data_provider"]) == (False, False, None)
    ts = box(tushare=standard_tushare()).capabilities()
    assert (ts["cn_market_data_available"], ts["cn_financials_available"], ts["cn_financials_limited"], ts["cn_data_provider"]) == (True, True, False, "tushare")
    keyless = box(baostock=FakeBaoStock()).capabilities()
    assert (keyless["cn_data_provider"], keyless["cn_financials_limited"], keyless["cn_market_data_available"]) == ("baostock", True, True)
    both = box(tushare=standard_tushare(), baostock=FakeBaoStock()).capabilities()
    assert both["cn_data_provider"] == "tushare"
    off = box(tushare=standard_tushare(), disabled_reason="historical_cutoff").capabilities()
    assert off["cn_market_data_available"] is False and off["cn_data_provider"] is None
    assert box(baostock=FakeBaoStock(), policy=FinDataPolicy(rules=[])).capabilities()["cn_data_provider"] is None
    assert box(tushare=standard_tushare(), market_key="k").capabilities()["market_data_available"] is True  # unchanged flag


def test_capabilities_are_json_serialisable_for_the_model_prompt():
    caps = box(tushare=standard_tushare()).capabilities()
    assert json.dumps(caps)  # JSON-serialisable for the model prompt


# ---------------------------------------------------------------- main.py wiring
class _Settings:
    def __init__(self, token):
        self.token = token

    def runtime(self, _account):
        return [], ({'tushare': self.token} if self.token else {})


def _perm_policy():
    rules = [PolicyRule(provider='tushare', category=c, credential_owner=o, store=True, display=True, model_input=True,
                        owner_scoped=(o == 'user_byok')) for c in ('identity', 'bars_daily', 'financials') for o in ('platform', 'user_byok')]
    return FinDataPolicy(rules=rules)


def test_tushare_resolver_is_policy_gated_like_twelve_data(monkeypatch):
    import app.main as api
    monkeypatch.setenv('FORESIGHTNINE_TUSHARE_TOKEN', 'platform-token-123456')
    monkeypatch.setattr(api, 'market_data_settings', _Settings(None))
    assert api._resolve_tushare_key('acct', policy=default_policy()) == (None, None)
    assert api._resolve_tushare_key('acct', policy=_perm_policy()) == ('platform-token-123456', 'platform')
    monkeypatch.setattr(api, 'market_data_settings', _Settings('own-token-987654'))
    assert api._resolve_tushare_key('acct', policy=default_policy()) == ('own-token-987654', 'user_byok')
    assert api._resolve_tushare_key('acct', policy=_perm_policy()) == ('own-token-987654', 'user_byok')
    monkeypatch.delenv('FORESIGHTNINE_TUSHARE_TOKEN')
    monkeypatch.setattr(api, 'market_data_settings', _Settings(None))
    assert api._resolve_tushare_key('acct', policy=_perm_policy()) == (None, None)


def test_capabilities_endpoint_reports_cn_flags_per_account(monkeypatch):
    from uuid import uuid4

    from fastapi.testclient import TestClient

    import app.main as api
    monkeypatch.setenv('FORESIGHTNINE_LOCAL_AUTH_MODE', 'mock')
    monkeypatch.setattr('app.fin_data.adapters.baostock.baostock_installed', lambda: False)
    client = TestClient(api.app)

    def login():
        auth = client.post('/v1/auth/local/register', json={'email': f'{uuid4().hex}@example.test', 'password': 'correct horse battery staple!'}).json()
        return {'Authorization': 'Bearer ' + auth['access_token']}
    plain, owner = login(), login()
    caps = client.get('/v2/research/capabilities', headers=plain).json()
    assert caps['cn_market_data_available'] is False and caps['cn_data_provider'] is None
    from app.market_credentials import MARKET_PROVIDER_ORDER, MarketCredentialCipher
    api.market_data_settings.cipher = MarketCredentialCipher(b'k' * 32)
    providers = [{'provider_id': pid, 'enabled': True, **({'api_key': 'tushare-token-0123456789'} if pid == 'tushare' else {})}
                 for pid in MARKET_PROVIDER_ORDER]
    assert client.put('/v1/account/market-data-config', headers=owner,
                      json={'fallback_order': list(MARKET_PROVIDER_ORDER), 'providers': providers}).status_code == 200
    caps = client.get('/v2/research/capabilities', headers=owner).json()
    assert caps['cn_market_data_available'] is True and caps['cn_financials_available'] is True and caps['cn_data_provider'] == 'tushare'
    assert 'tushare-token' not in str(caps)
    assert client.get('/v2/research/capabilities', headers=plain).json()['cn_data_provider'] is None


def test_tushare_connection_test_helper_never_returns_diagnostics(monkeypatch):
    import app.main as api
    from app.fin_data.adapters import tushare as module
    real = module.TushareAdapter
    fake = standard_tushare()
    monkeypatch.setattr(module, 'TushareAdapter', lambda **kw: real(**{**kw, 'transport': fake}))
    assert api._test_tushare_token('acct-1', None) == 'unavailable'
    assert api._test_tushare_token('acct-1', TOKEN) == 'ready'
    bad = standard_tushare(daily=ts_reply(code=2002, msg='您的token不对，请确认。'))
    monkeypatch.setattr(module, 'TushareAdapter', lambda **kw: real(**{**kw, 'transport': bad}))
    assert api._test_tushare_token('acct-1', TOKEN) == 'unavailable'


def test_structured_financials_sources_count_as_financial_statements_in_coverage():
    from app.research_coverage_map import _category_of_source
    assert _category_of_source({'kind': 'official', 'retrieval_method': 'structured_financials', 'provenance': {'provider': 'tushare'}},
                               'company_financials') == 'financial_statements'
    assert _category_of_source({'kind': 'market_data', 'retrieval_method': 'market_data', 'provenance': {'provider': 'baostock'}},
                               'market_data') == 'market_prices'
