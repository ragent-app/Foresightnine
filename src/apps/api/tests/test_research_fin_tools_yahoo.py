"""Fast-fail CN reachability, Yahoo fallback routing for A-shares/HK, capabilities and HK entity codes (no network)."""
from __future__ import annotations

import json

import pytest

from app.fin_data import cn
from app.fin_data.policy import default_policy, load_policy
from app.fin_data.reachability import ReachabilityProbe
from app.research_fin_tools import CompanyFinancialsRequest, MarketDataRequest, ResolveEntityRequest
from tests.fin_data.cn_helpers import TOKEN, FakeBaoStock, standard_tushare
from tests.fin_data.yahoo_helpers import chart, personal_dev_policy_json, reply
from tests.fin_runtime_helpers import FakeTransport, Reply, make_toolbox, memory_persist

PX = {"start_date": "2026-11-16", "end_date": "2026-11-19", "rationale": "prices"}
ROWS = [("2026-11-16", 1500.5, 1520.0, 1495.0, 1510.25, 25000, 1500.0), ("2026-11-17", 1510.0, 1530.0, 1505.0, 1525.0, 21000, 1515.0)]
HK_ROWS = [("2026-11-16", 520.0, 525.0, 515.0, 522.4, 1200000, 521.0)]


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_YAHOO_MIN_INTERVAL_SECONDS", "0")
    cn.clear_list_cache()
    yield
    cn.clear_list_cache()


def personal():
    return load_policy({"FORESIGHTNINE_FIN_DATA_POLICY": personal_dev_policy_json()})


def probe(*down):
    """A probe whose given providers are unreachable (connect raises); records the hosts tried."""
    hosts = {"api.tushare.pro": "tushare", "public-api.baostock.com": "baostock"}
    tried = []

    def connect(host, port, timeout):
        tried.append(hosts[host])
        if hosts[host] in down:
            raise TimeoutError("timed out")
    p = ReachabilityProbe(connect=connect)
    p.tried = tried
    return p


def box(*, tushare=None, baostock=None, yahoo=None, down=(), policy=None, market_key=None, **kw):
    return make_toolbox(market_key=market_key, user_agent=None, persist=memory_persist(), policy=policy,
                        tushare_key=TOKEN if tushare is not None else None, tushare_transport=tushare,
                        baostock_runner=baostock, cn_probe=probe(*down), yahoo_transport=yahoo, **kw)


def spec_for(b, **req):
    spec = b.market_spec(MarketDataRequest(**req), {})
    assert not isinstance(spec, dict), spec
    return spec


def yahoo_ok(rows=ROWS, **kw):
    return FakeTransport(reply(chart(rows, **kw)))


# ---------------------------------------------------------------- routing
def test_ashare_order_is_tushare_then_baostock_then_yahoo_and_yahoo_is_untouched_when_reachable():
    fake, bs, yh = standard_tushare(), FakeBaoStock(), yahoo_ok()
    b = box(tushare=fake, baostock=bs, yahoo=yh, policy=personal())
    assert b.market_data(spec_for(b, entity_id="SSE:600519", **PX)).result["provenance"]["provider"] == "tushare"
    assert not bs.requests and not yh.calls


def test_unreachable_tushare_is_skipped_without_a_call_and_baostock_serves():
    fake, bs, yh = standard_tushare(), FakeBaoStock(), yahoo_ok()
    b = box(tushare=fake, baostock=bs, yahoo=yh, down=("tushare",), policy=personal())
    out = b.market_data(spec_for(b, entity_id="SZSE:000001", **PX)).result
    assert out["provenance"]["provider"] == "baostock" and not fake.calls and not yh.calls


@pytest.mark.parametrize("entity,symbol", [("SSE:600519", "600519.SS"), ("SZSE:000001", "000001.SZ"), ("BJSE:430047", "430047.BJ")])
def test_both_unreachable_falls_to_yahoo_with_clear_provenance(entity, symbol):
    fake, bs, yh = standard_tushare(), FakeBaoStock(), yahoo_ok()
    b = box(tushare=fake, baostock=bs, yahoo=yh, down=("tushare", "baostock"), policy=personal())
    res = b.market_data(spec_for(b, entity_id=entity, **PX))
    out, stored = res.result, res.source.provenance
    assert out["status"] == "ok" and out["provenance"]["provider"] == "yahoo" and stored["currency"] == "CNY"
    assert symbol in yh.calls[0][0] and not fake.calls and not bs.requests
    assert out["permissions"]["export"] is False and out["rows"] == 2 and stored["adjustment"] == "splits"
    assert stored["credential_owner"] == "keyless_public_source" and "personal development use only" in stored["limitation"]


def test_unreachable_and_yahoo_denied_returns_immediately_with_the_named_providers():
    fake, bs, yh = standard_tushare(), FakeBaoStock(), yahoo_ok()
    b = box(tushare=fake, baostock=bs, yahoo=yh, down=("tushare", "baostock"))  # default policy: yahoo denied
    out = b.market_data(spec_for(b, symbol="600519", **PX)).result
    assert out["status"] == "unavailable" and out["reason"] == "cn_provider_unreachable"
    assert "Tushare and BaoStock" in out["message"] and "mainland China networks or through a proxy" in out["message"]
    assert out["unreachable_providers"] == ["tushare", "baostock"] and not fake.calls and not bs.requests and not yh.calls
    only_bs = box(baostock=bs, down=("baostock",)).market_data(spec_for(b, symbol="600519", **PX)).result
    assert only_bs["reason"] == "cn_provider_unreachable" and only_bs["unreachable_providers"] == ["baostock"]


def test_yahoo_failure_after_cn_providers_reports_the_chain():
    yh = FakeTransport(Reply(429, b"Too Many Requests"))
    b = box(baostock=FakeBaoStock(), yahoo=yh, down=("baostock",), policy=personal())
    out = b.market_data(spec_for(b, symbol="600519", **PX)).result
    assert out["status"] == "rate_limited" and out["reason"] == "yahoo_rate_limited"


def test_financials_fail_fast_when_unreachable_and_yahoo_never_serves_financials():
    b = box(tushare=standard_tushare(), baostock=FakeBaoStock(), down=("tushare", "baostock"), policy=personal())
    out = b.company_financials(CompanyFinancialsRequest(entity_id="SSE:600519", rationale="f"), {}).result
    assert out["reason"] == "cn_provider_unreachable" and out["unreachable_providers"] == ["tushare", "baostock"]


def test_default_policy_without_any_cn_source_keeps_the_existing_message():
    out = box().market_data(spec_for(box(), symbol="600519", **PX)).result
    assert out["reason"] == "cn_market_data_unavailable"


def test_hkex_without_a_twelve_key_uses_yahoo_and_default_policy_keeps_the_key_message():
    yh = yahoo_ok(HK_ROWS, symbol="0700.HK", currency="HKD", tz="Asia/Hong_Kong", hh=9, mm=30)
    b = box(yahoo=yh, policy=personal())
    res = b.market_data(spec_for(b, symbol="700", exchange="HKEX", **PX))
    out = res.result
    assert out["status"] == "ok" and out["provenance"]["provider"] == "yahoo" and res.source.provenance["currency"] == "HKD"
    assert "0700.HK" in yh.calls[0][0] and res.source.provenance["exchange"] == "HKEX"
    plain = box(yahoo=yahoo_ok())
    assert plain.market_data(spec_for(plain, symbol="700", exchange="HKEX", **PX)).result["reason"] == "market_data_key_required"


def test_hkex_twelve_data_failure_falls_back_to_yahoo_and_success_does_not_touch_yahoo():
    yh = yahoo_ok(HK_ROWS, symbol="0700.HK", currency="HKD", tz="Asia/Hong_Kong")
    bad_td = FakeTransport(Reply(500, b"{}"))
    b = box(yahoo=yh, policy=personal(), market_key="k", twelve=bad_td)
    out = b.market_data(spec_for(b, symbol="0700", exchange="HKEX", **PX)).result
    assert out["provenance"]["provider"] == "yahoo" and bad_td.calls
    assert any("twelve_data failed" in n for n in out["coverage"]["notes"])
    yh2 = yahoo_ok(HK_ROWS)
    from tests.fin_runtime_helpers import json_reply
    good = box(yahoo=yh2, policy=personal(), market_key="k", twelve=FakeTransport(json_reply("td_time_series_ipo.json")))
    assert good.market_data(spec_for(good, entity_id="HKEX:700", **PX)).result["provenance"]["provider"] == "twelve_data"
    assert not yh2.calls
    no_yahoo = box(yahoo=yahoo_ok(), market_key="k", twelve=FakeTransport(Reply(500, b"{}")))
    assert no_yahoo.market_data(spec_for(no_yahoo, symbol="700", exchange="HKEX", **PX)).result["status"] == "failed"


def test_hk_suffix_symbol_without_exchange_is_recognised_in_the_spec():
    b = box(policy=personal(), yahoo=yahoo_ok())
    spec = spec_for(b, symbol="700.HK", lookback="1m", rationale="p")
    assert (spec.exchange, spec.symbol, spec.entity_id, spec.currency) == ("HKEX", "0700", "HKEX:0700", "HKD")


# ---------------------------------------------------------------- capabilities
def test_capabilities_reflect_actual_reachability_and_yahoo_policy():
    b = box(tushare=standard_tushare(), baostock=FakeBaoStock(), down=("tushare", "baostock"))
    caps = b.capabilities()
    assert (caps["cn_market_data_available"], caps["cn_financials_available"], caps["cn_data_provider"]) == (False, False, None)
    assert caps["cn_providers_unreachable"] == ["tushare", "baostock"] and caps["hk_market_data_available"] is False
    y = box(tushare=standard_tushare(), baostock=FakeBaoStock(), down=("tushare", "baostock"), policy=personal()).capabilities()
    assert y["cn_market_data_available"] is True and y["cn_market_data_provider"] == "yahoo"
    assert y["cn_financials_available"] is False and y["cn_data_provider"] is None and y["hk_market_data_available"] is True
    half = box(tushare=standard_tushare(), baostock=FakeBaoStock(), down=("tushare",)).capabilities()
    assert half["cn_data_provider"] == "baostock" and half["cn_financials_limited"] is True and half["cn_providers_unreachable"] == ["tushare"]
    ok = box(tushare=standard_tushare()).capabilities()
    assert ok["cn_market_data_provider"] == "tushare" and ok["cn_providers_unreachable"] == []
    assert box(market_key="k").capabilities()["hk_market_data_available"] is True
    off = box(policy=personal(), disabled_reason="historical_cutoff").capabilities()
    assert off["hk_market_data_available"] is False and off["cn_market_data_available"] is False
    assert json.dumps(y)


def test_probe_is_consulted_before_calls_and_cached_across_tool_calls():
    p = probe("tushare", "baostock")
    b = make_toolbox(market_key=None, user_agent=None, persist=memory_persist(), tushare_key=TOKEN, tushare_transport=standard_tushare(),
                     baostock_runner=FakeBaoStock(), cn_probe=p)
    for _ in range(3):
        b.market_data(spec_for(b, symbol="600519", **PX))
        b.capabilities()
    assert sorted(p.tried) == ["baostock", "tushare"]  # one probe per provider


# ---------------------------------------------------------------- entities
def test_hk_codes_resolve_to_hkex_candidates_without_any_other_source():
    b = box(policy=personal())
    for query, market in (("0700", None), ("700.HK", None), ("HK:700", None), ("700", "HK")):
        out = b.resolve_entity(ResolveEntityRequest(query=query, market=market, rationale="find"), {})
        cands = out.result["candidates"]
        assert [c["entity_id"] for c in cands] == ["HKEX:0700"], query
        assert cands[0]["security"]["exchange"] == "HKEX" and cands[0]["security"]["currency"] == "HKD"
        assert cands[0]["identifiers"]["yahoo_symbol"] == "0700.HK" and "HKEX:0700" in out.entities
    assert "company_name" not in cands[0] or not cands[0].get("company")


def test_chinese_name_without_a_reachable_listing_asks_for_the_code():
    b = box(tushare=standard_tushare(), baostock=FakeBaoStock(), down=("tushare", "baostock"))
    out = b.resolve_entity(ResolveEntityRequest(query="贵州茅台", rationale="find"), {}).result
    assert out["status"] == "unavailable" and out["reason"] == "cn_name_lookup_unavailable"
    assert "6-digit code" in out["message"] and out["unreachable_providers"] == ["tushare", "baostock"]
    with_td = box(tushare=standard_tushare(), baostock=FakeBaoStock(), down=("tushare", "baostock"), market_key="k",
                  twelve=FakeTransport(Reply(200, b'{"data":[],"status":"ok"}')))
    got = with_td.resolve_entity(ResolveEntityRequest(query="贵州茅台", rationale="find"), {}).result
    assert got["reason"] == "cn_name_lookup_unavailable"


def test_six_digit_code_with_unreachable_listing_still_yields_an_unverified_candidate():
    fake = standard_tushare()
    b = box(tushare=fake, baostock=FakeBaoStock(), down=("tushare", "baostock"))
    out = b.resolve_entity(ResolveEntityRequest(query="600519", rationale="find"), {})
    assert [c["entity_id"] for c in out.result["candidates"]] == ["SSE:600519"] and not fake.calls
    assert any("inferred" in n for n in out.result["coverage"]["notes"])
