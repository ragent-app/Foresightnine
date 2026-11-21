"""A-share data: code parsing, Tushare, BaoStock (fake runner / fake worker), policy, entity resolution."""
from __future__ import annotations

import ast
import json
from datetime import date
from decimal import Decimal

import pytest

from app.fin_data import cn
from app.fin_data.adapters import baostock as bs_module
from app.fin_data.adapters.baostock import BaoStockAdapter, BaoStockFailure, report_periods, subprocess_runner
from app.fin_data.adapters.tushare import TushareAdapter
from app.fin_data.entity import resolve_cn, resolve_entity, wants_cn
from app.fin_data.policy import Denied, FinDataPolicy, PolicyRule, default_policy
from app.openai_compatible import _TransportFailure
from tests.fin_data.cn_helpers import TOKEN, FakeBaoStock, TushareFake, standard_tushare, ts_reply
from tests.fin_data.helpers import NOW, Reply, open_limiter

SH = cn.CnCode("600519", "SSE", False)
RANGE = {"start": date(2025, 11, 17), "end": date(2025, 11, 19)}


@pytest.fixture(autouse=True)
def fresh_cache():
    cn.clear_list_cache()
    yield
    cn.clear_list_cache()


def ts(fake, **kw) -> TushareAdapter:
    return TushareAdapter(token=TOKEN, credential_owner="user_byok", account_id="acct-1", transport=fake, limiter=open_limiter(),
                          policy=default_policy(), now_fn=lambda: NOW, **kw)


def bs(runner, policy=None) -> BaoStockAdapter:
    return BaoStockAdapter(runner=runner, limiter=open_limiter(), policy=policy or default_policy(), now_fn=lambda: NOW)


# ------------------------------------------------------------------ codes
@pytest.mark.parametrize("text,expected", [
    ("600519.SH", ("600519", "SSE", False)), ("600519.ss", ("600519", "SSE", False)), ("sh.600519", ("600519", "SSE", False)),
    ("SH600519", ("600519", "SSE", False)), ("SSE:600519", ("600519", "SSE", False)), ("000001.SZ", ("000001", "SZSE", False)),
    ("sz.300750", ("300750", "SZSE", False)), ("430047.BJ", ("430047", "BJSE", False)), ("bj.830799", ("830799", "BJSE", False)),
    ("600519", ("600519", "SSE", True)), ("688981", ("688981", "SSE", True)), ("000858", ("000858", "SZSE", True)),
    ("300750", ("300750", "SZSE", True)), ("830799", ("830799", "BJSE", True)), ("430047", ("430047", "BJSE", True)),
    ("920001", ("920001", "BJSE", True)), ("900901", ("900901", "SSE", True))])
def test_code_forms_and_exchange_inference(text, expected):
    got = cn.parse_cn_code(text)
    assert (got.code, got.exchange, got.inferred) == expected


@pytest.mark.parametrize("text", ["AAPL", "NASDAQ:123456", "12345", "1234567", "100000", "600519.HK", "", None, "HK:000700"])
def test_non_ashare_codes_are_rejected(text):
    assert cn.parse_cn_code(text) is None


def test_code_conversions():
    c = cn.parse_cn_code("sz.300750")
    assert (c.entity_id, c.ts_code, c.baostock_code) == ("SZSE:300750", "300750.SZ", "sz.300750")
    assert cn.parse_cn_code("430047").ts_code == "430047.BJ"


# ------------------------------------------------------------------ tushare: request/parse
def test_tushare_request_shape_never_leaks_token_and_stock_list_is_cached():
    fake = standard_tushare()
    a = ts(fake)
    r = a.stock_list()
    assert r.status == "ok" and len(r.data) == 4
    method, url, headers = fake.urls[0]
    assert method == "POST" and url == "https://api.tushare.pro" and TOKEN not in json.dumps(headers) and TOKEN not in url
    assert fake.calls[0]["api_name"] == "stock_basic" and fake.calls[0]["token"] == TOKEN and fake.calls[0]["params"] == {"list_status": "L"}
    assert "ts_code" in fake.calls[0]["fields"]
    moutai = next(e for e in r.data if e.symbol == "600519")
    assert (moutai.entity_id, moutai.exchange, moutai.currency, moutai.listing_date) == ("SSE:600519", "SSE", "CNY", date(2001, 8, 27))
    assert moutai.identifiers["ts_code"] == "600519.SH" and moutai.identifiers["board"] == "主板"
    assert next(e for e in r.data if e.symbol == "430047").exchange == "BJSE"
    again = a.stock_list()
    assert len(fake.calls) == 1 and "cache" in again.coverage.notes[0]
    assert TOKEN not in r.model_dump_json()


def test_tushare_daily_bars_unadjusted_with_adj_factor_lots_to_shares_and_filters():
    fake = standard_tushare()
    r = ts(fake).bars(SH, **RANGE)
    assert r.status == "ok" and fake.apis() == ["daily", "adj_factor"]
    assert fake.calls[0]["params"] == {"ts_code": "600519.SH", "end_date": "20251119", "start_date": "20251117"}
    assert [b.ts_start.date().isoformat() for b in r.data] == ["2025-11-17", "2025-11-19"]
    first = r.data[0]
    assert first.ts_start.isoformat() == "2025-11-17T09:30:00+08:00" and first.adjustment == "none"
    assert first.volume == Decimal("3100050.0") and first.adj_factor == Decimal("8.25") and first.close == Decimal("1490.0")
    assert r.coverage.currency == "CNY" and r.coverage.is_final is True and r.permissions.owner_scoped is True
    notes = " ".join(r.coverage.notes)
    assert "Dropped 1 duplicate rows" in notes and "Dropped 1 zero-volume rows" in notes and "unadjusted" in notes
    assert r.cost.provider_calls == 2 and TOKEN not in r.model_dump_json()


def test_tushare_session_clamp_marks_partial_and_range_after_latest_is_empty():
    r = ts(standard_tushare()).bars(SH, start=date(2025, 11, 17), end=date(2026, 11, 25))
    assert r.status == "partial" and r.reason == "range_clamped" and "2026-11-19" in r.message
    fake = standard_tushare()
    e = ts(fake).bars(SH, start=date(2026, 11, 20), end=date(2026, 11, 20))
    assert e.status == "empty" and e.reason == "range_after_latest_session" and not fake.calls


def test_tushare_weekly_aggregation_and_intraday_is_refused():
    r = ts(standard_tushare()).bars(SH, interval="1week", **RANGE)
    assert r.status == "ok" and len(r.data) == 1
    week = r.data[0]
    assert (week.open, week.high, week.low, week.close) == (Decimal("1480.0"), Decimal("1520.0"), Decimal("1475.0"), Decimal("1510.25"))
    assert week.volume == Decimal("3100050.0") + Decimal("2500000") and week.interval == "1week"
    fake = standard_tushare()
    intraday = ts(fake).bars(SH, interval="5min", start=date(2025, 11, 17))
    assert intraday.reason == "intraday_not_supported" and not fake.calls


def test_tushare_adj_factor_permission_gap_degrades_with_note():
    fake = standard_tushare(adj_factor=ts_reply(code=40001, msg="抱歉，您没有接口访问权限"))
    r = ts(fake).bars(SH, **RANGE)
    assert r.status == "ok" and all(b.adj_factor is None for b in r.data)
    assert any("adj_factor unavailable (tushare_permission_insufficient)" in n for n in r.coverage.notes)


# ------------------------------------------------------------------ tushare: errors
def test_tushare_permission_rate_limit_and_token_errors():
    perm = ts(standard_tushare(income=ts_reply(code=2002, msg=f"抱歉，您没有接口访问权限，权限的具体详情访问：https://tushare.pro ({TOKEN})")))
    r = perm.statement_lines(SH, lines=["revenue"])
    assert r.status == "failed" and r.reason == "tushare_permission_insufficient" and "'income'" in r.message and r.retryable is False
    assert TOKEN not in r.model_dump_json()
    minute = ts(standard_tushare(daily=ts_reply(code=40203, msg="抱歉，您每分钟最多访问该接口50次，权限的具体详情访问：...")))
    r = minute.bars(SH, **RANGE)
    assert r.status == "rate_limited" and r.retryable and r.suggested_actions[0].retry_after_seconds == 60.0 and "daily" in r.message
    bad = ts(standard_tushare(daily=ts_reply(code=2002, msg="您的token不对，请确认。")))
    assert bad.bars(SH, **RANGE).reason == "invalid_api_key"
    other = ts(standard_tushare(daily=ts_reply(code=50101, msg="内部错误")))
    assert other.bars(SH, **RANGE).reason == "provider_error"


def test_tushare_transport_and_http_failures():
    r = ts(TushareFake({"daily": _TransportFailure("boom")})).bars(SH, **RANGE)
    assert r.status == "unknown" and r.retryable is False
    assert ts(TushareFake({"daily": Reply(502, b"bad")})).bars(SH, **RANGE).retryable is True
    assert ts(TushareFake({"daily": Reply(200, b"not json")})).bars(SH, **RANGE).reason == "invalid_response"


def test_tushare_local_rate_bucket_is_per_account():
    from app.fin_data.ratelimit import InProcessRateLimiter, ProviderLimit
    lim = InProcessRateLimiter({"tushare": ProviderLimit(1.0 / 60, 1.0, 100)}, now=lambda: NOW)
    fake = standard_tushare()

    def mk(acct):
        return TushareAdapter(token="tok-" + acct * 8, credential_owner="user_byok", account_id=acct, transport=fake, limiter=lim,
                              policy=default_policy(), now_fn=lambda: NOW)
    assert mk("a1").stock_list().status == "ok"
    cn.clear_list_cache()
    assert mk("a1").stock_list().status == "rate_limited"
    cn.clear_list_cache()
    assert mk("a2").stock_list().status == "ok"


# ------------------------------------------------------------------ tushare: statements
def test_statements_cumulative_periods_standard_lines_and_revisions():
    fake = standard_tushare()
    r = ts(fake).statement_lines(SH, period="quarter", n=8)
    assert r.status == "ok" and sorted(fake.apis()) == ["balancesheet", "cashflow", "income"]
    assert all(c["params"] == {"ts_code": "600519.SH", "report_type": "1"} for c in fake.calls)
    rev = {l.period_end: l for l in r.data if l.line == "revenue"}
    assert sorted(rev) == [date(2024, 9, 30), date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31),
                           date(2026, 6, 30), date(2026, 9, 30)]
    h1 = rev[date(2026, 6, 30)]
    assert h1.value == Decimal("260.0") and h1.basis == "ytd_cumulative" and h1.period_start == date(2026, 1, 1)
    assert h1.fiscal_label == "2026H1 YTD" and h1.reported_tag == "total_revenue" and h1.unit == "CNY" and h1.currency == "CNY"
    assert h1.filed_at == date(2026, 8, 20) and h1.derived is False and "企业会计准则" in h1.accounting_standard
    fy = rev[date(2025, 12, 31)]
    assert fy.value == Decimal("565.0") and fy.fiscal_label == "FY2025" and fy.filed_at == date(2026, 4, 25)
    assert fy.revision_count == 1 and fy.superseded == ["2026-03-20:560.0"]       # latest revision wins, earlier is recorded
    assert Decimal("999") not in [l.value for l in r.data]                        # adjusted report_type 4 ignored
    by_line = {l.line: l for l in r.data if l.period_end == date(2026, 9, 30)}
    assert by_line["net_income"].reported_tag == "n_income_attr_p" and by_line["eps_diluted"].unit == "CNY/share"
    assert by_line["total_assets"].basis == "point_in_time" and by_line["total_assets"].period_start is None
    assert by_line["equity"].reported_tag == "total_hldr_eqy_exc_min_int" and by_line["cash"].reported_tag == "money_cap"
    assert by_line["operating_cash_flow"].value == Decimal("160.0") and by_line["capex"].reported_tag == "c_pay_acq_const_fiolta"
    assert any("cumulative" in n for n in r.coverage.notes) and any("企业会计准则" in n for n in r.coverage.notes)
    assert not any(l.derived for l in r.data)


def test_revenue_falls_back_to_operating_revenue_when_total_is_missing():
    r = ts(standard_tushare()).statement_lines(SH, lines=["revenue"], period="quarter", n=12)
    old = next(l for l in r.data if l.period_end == date(2024, 9, 30))
    assert old.reported_tag == "revenue" and old.value == Decimal("300.0")


def test_single_quarters_are_derived_only_on_request_and_marked():
    r = ts(standard_tushare()).statement_lines(SH, lines=["revenue", "eps_diluted", "total_assets"], period="quarter", n=4, derive_quarters=True)
    derived = {l.period_end: l for l in r.data if l.derived and l.line == "revenue"}
    assert {k: v.value for k, v in derived.items()} == {date(2025, 12, 31): Decimal("175.0"), date(2026, 6, 30): Decimal("140.0"),
                                                         date(2026, 9, 30): Decimal("170.0")}
    q3 = derived[date(2026, 9, 30)]
    assert q3.period_start == date(2026, 7, 1) and q3.fiscal_label == "2026Q3" and q3.basis == "single_quarter_derived"
    assert "9M YTD" in q3.derivation and "H1 YTD" in q3.derivation
    assert not any(l.derived for l in r.data if l.line in ("eps_diluted", "total_assets"))
    assert any("per-share" in n for n in r.coverage.notes)
    assert next(l for l in r.data if l.line == "revenue" and not l.derived and l.period_end == date(2026, 9, 30)).value == Decimal("430.0")


def test_fiscal_years_and_unknown_lines():
    r = ts(standard_tushare()).statement_lines(SH, lines=["net_income"], period="year", n=3)
    assert [(l.period_end.isoformat(), str(l.value)) for l in r.data] == [("2025-12-31", "242.0")]
    assert ts(standard_tushare()).statement_lines(SH, lines=["bogus"]).reason == "unknown_line"


def test_derivation_helper_skips_when_prior_cumulative_is_missing():
    ytd = {date(2026, 9, 30): Decimal(430), date(2026, 12, 31): Decimal(600)}
    out, skipped = cn.derive_single_quarters(ytd, [date(2026, 9, 30), date(2026, 12, 31)])
    assert [(e, v) for e, v, _ in out] == [(date(2026, 12, 31), Decimal(170))] and any("2026Q3 not derivable" in s for s in skipped)


# ------------------------------------------------------------------ BaoStock
def test_baostock_bars_use_runner_with_both_adjust_flags_and_derive_factor():
    runner = FakeBaoStock()
    r = bs(runner).bars(SH, **RANGE)
    assert runner.requests == [{"op": "k_data", "code": "sh.600519", "adjustflags": ["3", "1"], "start": "2025-11-17", "end": "2025-11-19"}]
    assert r.status == "ok" and [b.ts_start.date().isoformat() for b in r.data] == ["2025-11-17", "2025-11-19"]
    assert r.data[0].adjustment == "none" and r.data[0].adj_factor == Decimal("8.250000")  # 12292.5 / 1490
    assert r.data[1].volume == Decimal("2500000") and r.permissions.owner_scoped is False and r.permissions.export is False
    assert any("suspended" in n for n in r.coverage.notes)


def test_baostock_failures_and_missing_library():
    r = bs(FakeBaoStock(fail=BaoStockFailure("source_timeout", "timed out", timed_out=True))).bars(SH, **RANGE)
    assert r.status == "unknown" and r.reason == "source_timeout"
    r = bs(FakeBaoStock(fail=BaoStockFailure("provider_error", "BaoStock error: x"))).stock_list()
    assert r.status == "failed" and r.reason == "provider_error"
    missing = bs(FakeBaoStock())
    missing._runner = None
    assert missing.available() is False and missing.bars(SH, start=date(2025, 11, 17)).reason == "baostock_not_installed"


def test_baostock_stock_list_keeps_only_listed_stocks():
    r = bs(FakeBaoStock()).stock_list()
    assert sorted(e.entity_id for e in r.data) == ["SSE:600519", "SZSE:000001"] and r.data[0].currency == "CNY"


def test_baostock_statements_are_ratio_limited_and_say_so():
    runner = FakeBaoStock()
    r = bs(runner).statement_lines(SH, lines=["revenue", "net_income", "total_assets"], period="quarter", n=3)
    assert r.status == "partial" and "total_assets" in r.message
    assert runner.requests[0]["kinds"] == ["profit"] and len(runner.requests[0]["periods"]) == 3
    rev = next(l for l in r.data if l.line == "revenue")
    assert rev.reported_tag == "MBRevenue" and rev.basis == "ytd_cumulative" and rev.taxonomy == "baostock"
    everything = bs(FakeBaoStock()).statement_lines(SH, period="quarter", n=2)
    assert {"revenue", "net_income", "roe_avg", "liability_to_asset", "cfo_to_net_profit"} <= {l.line for l in everything.data}
    assert next(l for l in everything.data if l.line == "roe_avg").basis == "period_ratio"
    nothing = bs(FakeBaoStock()).statement_lines(SH, lines=["total_assets"])
    assert nothing.status == "empty" and nothing.reason == "no_matching_lines"


def test_report_periods_cover_latest_elapsed_quarters_and_extra_history():
    assert report_periods(date(2026, 11, 20), "quarter", 3, 0) == [(2026, 3), (2026, 2), (2026, 1)]
    assert report_periods(date(2026, 2, 1), "quarter", 2, 3) == [(2025, 4), (2025, 3), (2025, 2), (2025, 1), (2024, 4)]
    assert report_periods(date(2026, 11, 20), "year", 2, 0) == [(2025, 4), (2024, 4)]


def _worker(tmp_path, body: str):
    path = tmp_path / "worker.py"
    path.write_text(body)
    return path


def test_subprocess_runner_parses_marker_line_ignoring_library_noise(tmp_path, monkeypatch):
    worker = _worker(tmp_path, "import sys, json\nreq = json.loads(sys.stdin.read())\nprint('login success!')\n"
                               "print('@@BAOSTOCK_RESULT@@' + json.dumps({'ok': True, 'result': {'echo': req['op']}}))\n")
    monkeypatch.setattr(bs_module, "WORKER", worker)
    assert subprocess_runner({"op": "stock_basic"}, 10.0) == {"echo": "stock_basic"}


def test_subprocess_runner_reports_worker_errors_timeouts_and_crashes(tmp_path, monkeypatch):
    monkeypatch.setattr(bs_module, "WORKER", _worker(
        tmp_path, "import json\nprint('@@BAOSTOCK_RESULT@@' + json.dumps({'ok': False, 'error': 'RuntimeError: login failed'}))\n"))
    with pytest.raises(BaoStockFailure) as caught:
        subprocess_runner({"op": "x"}, 10.0)
    assert caught.value.reason == "provider_error" and "login failed" in caught.value.message
    monkeypatch.setattr(bs_module, "WORKER", _worker(tmp_path, "import time\ntime.sleep(30)\n"))
    with pytest.raises(BaoStockFailure) as caught:
        subprocess_runner({"op": "x"}, 0.5)
    assert caught.value.timed_out and caught.value.reason == "source_timeout"
    monkeypatch.setattr(bs_module, "WORKER", _worker(tmp_path, "raise SystemExit(3)\n"))
    with pytest.raises(BaoStockFailure) as caught:
        subprocess_runner({"op": "x"}, 10.0)
    assert caught.value.reason == "source_unavailable"


def test_real_worker_script_is_valid_python_and_exits_cleanly_without_the_library(tmp_path):
    import subprocess
    import sys
    ast.parse(bs_module.WORKER.read_text())
    if bs_module.baostock_installed():
        pytest.skip("baostock installed; do not touch the network in tests")
    done = subprocess.run([sys.executable, "-I", str(bs_module.WORKER)], input=b'{"op":"stock_basic"}', capture_output=True, timeout=30)
    line = next(l for l in done.stdout.decode().splitlines() if l.startswith(bs_module.MARKER))
    assert json.loads(line[len(bs_module.MARKER):])["ok"] is False


# ------------------------------------------------------------------ policy
def test_default_policy_decisions_for_tushare_and_baostock():
    p = default_policy()
    for cat in ("identity", "bars_daily", "financials"):
        denied = p.decide("tushare", cat, "SSE", credential_owner="platform")
        assert isinstance(denied, Denied) and denied.reason == "license_platform_key_not_permitted"
        own = p.decide("tushare", cat, "SSE", credential_owner="user_byok")
        assert (own.store, own.display, own.model_input, own.export, own.owner_scoped) == (True, True, True, False, True)
        free = p.decide("baostock", cat, "SZSE", credential_owner="platform")
        assert (free.store, free.display, free.model_input, free.export, free.owner_scoped) == (True, True, True, False, False)
    assert "NEEDS REVIEW" in next(r for r in p.rules if r.provider == "baostock").notes
    assert isinstance(p.decide("tushare", "bars_intraday", credential_owner="user_byok"), Denied)  # not declared


def test_platform_token_requires_policy_and_never_comes_from_byok(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_TUSHARE_TOKEN", "platform-token-123456")
    fake = standard_tushare()
    denied = TushareAdapter(credential_owner="platform", transport=fake, limiter=open_limiter(), policy=default_policy(), now_fn=lambda: NOW)
    assert denied.stock_list().reason == "license_platform_key_not_permitted" and not fake.calls
    allow = FinDataPolicy(rules=[PolicyRule(provider="tushare", category="identity", credential_owner="platform", store=True, display=True, model_input=True)])
    ok = TushareAdapter(credential_owner="platform", transport=fake, limiter=open_limiter(), policy=allow, now_fn=lambda: NOW)
    first = ok.stock_list()
    assert first.status == "ok" and fake.calls[0]["token"] == "platform-token-123456" and first.permissions.owner_scoped is False
    byok = TushareAdapter(credential_owner="user_byok", account_id="a", transport=TushareFake({}), limiter=open_limiter(),
                          policy=default_policy(), now_fn=lambda: NOW)
    assert byok.stock_list().reason == "api_key_not_configured"  # the platform env token is never used for BYOK


def test_policy_can_deny_baostock():
    deny = FinDataPolicy(rules=[PolicyRule(provider="baostock", category="bars_daily", deny=True, deny_reason="denied_by_policy")])
    adapter = bs(FakeBaoStock(), policy=deny)
    assert adapter.available() is False and adapter.bars(SH, start=date(2025, 11, 17)).reason == "denied_by_policy"


# ------------------------------------------------------------------ entity resolution
def listing(adapter):
    return [("tushare", adapter.stock_list)]


def test_resolve_by_code_forms_never_guesses_and_enriches_from_listing():
    a = ts(standard_tushare())
    for q in ("600519.SH", "sh.600519", "600519", "SSE:600519"):
        r = resolve_cn(q, listing(a))
        assert [c.entity.entity_id for c in r.data] == ["SSE:600519"] and r.data[0].match_basis == "ticker_exact"
        assert r.data[0].entity.company_name == "贵州茅台" and r.data[0].sources == ("tushare",)
    assert resolve_cn("600000", listing(a)).status == "empty"


def test_resolve_by_chinese_name_returns_all_candidates_in_rank_order():
    a = ts(standard_tushare())
    r = resolve_cn("平安", listing(a))
    assert [c.entity.entity_id for c in r.data] == ["SZSE:000001", "SSE:601318"]
    assert [c.match_basis for c in r.data] == ["name_prefix", "name_contains"]
    exact = resolve_cn("贵州茅台", listing(a))
    assert exact.data[0].match_basis == "name_exact" and len(exact.data) == 1
    assert resolve_cn("payh", listing(a)).data[0].entity.entity_id == "SZSE:000001"
    assert resolve_cn("贵州茅台酒股份有限公司", listing(a)).data[0].entity.symbol == "600519"


def test_resolve_entity_merges_cn_candidates_ambiguity_and_market_filter():
    a = ts(standard_tushare())
    r = resolve_entity("平安", cn=resolve_cn("平安", listing(a)))
    assert r.reason == "multiple_candidates" and all(not c.unique for c in r.data) and len(r.data) == 2
    one = resolve_entity("贵州茅台", market="CN", cn=resolve_cn("贵州茅台", listing(a)))
    assert one.reason == "ok" and one.data[0].unique and one.data[0].entity.exchange == "SSE"
    assert resolve_entity("贵州茅台", market="US", cn=resolve_cn("贵州茅台", listing(a))).status == "empty"


def test_resolve_falls_back_to_baostock_listing_then_to_unverified_code():
    def broken():
        return ts(TushareFake({"stock_basic": ts_reply(code=2002, msg="没有接口访问权限")})).stock_list()
    r = resolve_cn("平安银行", [("tushare", broken), ("baostock", bs(FakeBaoStock()).stock_list)])
    assert [c.entity.entity_id for c in r.data] == ["SZSE:000001"] and r.data[0].sources == ("baostock",)
    assert any("tushare listing unavailable" in n for n in r.coverage.notes)
    unverified = resolve_cn("688981", [("tushare", broken)])
    assert unverified.status == "partial" and unverified.data[0].entity.entity_id == "SSE:688981"
    assert unverified.data[0].sources == ("cn_code_format",) and "not verified" in " ".join(unverified.coverage.notes)
    assert resolve_cn("某某公司", [("tushare", broken)]).status == "failed"


def test_wants_cn_only_for_codes_chinese_text_or_cn_market():
    assert wants_cn("600519", None) and wants_cn("贵州茅台", None) and wants_cn("Moutai", "CN") and wants_cn("sh.600519", None)
    assert not wants_cn("AAPL", None) and not wants_cn("Apple", "US") and not wants_cn("Kweichow Moutai", None)


def test_beijing_exchange_is_registered_with_beijing_hours():
    from app.research_clock import EXCHANGES, exchange_session
    bse = exchange_session("BJSE")
    assert bse is not None and bse.timezone == "Asia/Shanghai" and str(bse.open) == "09:30:00" and str(bse.close) == "15:00:00"
    assert bse.lunch is not None and "BJSE" in EXCHANGES
