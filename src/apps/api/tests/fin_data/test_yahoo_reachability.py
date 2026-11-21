"""Reachability probe, Yahoo daily bars (parsing, error mapping, pacing), policy and HK code normalisation."""
from __future__ import annotations

import threading
from datetime import date
from decimal import Decimal

import pytest

from app.fin_data import cn
from app.fin_data.adapters.yahoo import Pacer, YahooAdapter
from app.fin_data.policy import FinDataPolicy, default_policy, load_policy
from app.fin_data.reachability import ReachabilityProbe, unreachable_message
from app.openai_compatible import _TransportTimeout
from tests.fin_data.helpers import NOW, FakeTransport, Reply, open_limiter
from tests.fin_data.yahoo_helpers import MOUTAI_EVENTS, MOUTAI_ROWS, chart, personal_dev_policy_json, reply

SSE = cn.CnCode("600519", "SSE", False)


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_YAHOO_MIN_INTERVAL_SECONDS", "0")


def personal_policy() -> FinDataPolicy:
    return load_policy({"FORESIGHTNINE_FIN_DATA_POLICY": personal_dev_policy_json()})


def adapter(transport, policy=None, **kw):
    return YahooAdapter(transport=transport, limiter=open_limiter(), policy=policy or personal_policy(), now_fn=lambda: NOW, **kw)


# ---------------------------------------------------------------- probe
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_probe_caches_for_ttl_then_reprobes_and_passes_the_env_timeout(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_CN_PROBE_TIMEOUT_SECONDS", "2.5")
    clock, calls = Clock(), []

    def connect(host, port, timeout):
        calls.append((host, port, timeout))
        raise TimeoutError("timed out")
    probe = ReachabilityProbe(connect=connect, clock=clock)
    first = probe.check("baostock")
    assert not first.reachable and first.error == "TimeoutError" and calls == [("public-api.baostock.com", 10030, 2.5)]
    assert not probe.reachable("baostock") and len(calls) == 1
    clock.t += 599
    probe.reachable("baostock")
    assert len(calls) == 1
    clock.t += 2  # past the 10 minute default
    probe.reachable("baostock")
    assert len(calls) == 2
    assert probe.reachable("tushare") is False and calls[-1][:2] == ("api.tushare.pro", 443)  # providers cached separately
    assert probe.reachable("yahoo") is True  # not a probed provider


def test_probe_default_timeout_and_ttl_env(monkeypatch):
    clock, calls = Clock(), []
    probe = ReachabilityProbe(connect=lambda h, p, t: calls.append(t), clock=clock)
    assert probe.reachable("tushare") and calls == [5.0]
    monkeypatch.setenv("FORESIGHTNINE_CN_PROBE_CACHE_SECONDS", "10")
    clock.t += 11
    probe.reachable("tushare")
    assert len(calls) == 2


def test_only_one_probe_runs_at_a_time_per_provider():
    started, release, calls = threading.Event(), threading.Event(), []

    def connect(host, port, timeout):
        calls.append(host)
        started.set()
        release.wait(5)
        raise OSError("unreachable")
    probe = ReachabilityProbe(connect=connect)
    results = []
    threads = [threading.Thread(target=lambda: results.append(probe.reachable("baostock"))) for _ in range(5)]
    for t in threads:
        t.start()
    assert started.wait(5)
    release.set()
    for t in threads:
        t.join(5)
    assert results == [False] * 5 and len(calls) == 1


def test_unreachable_message_names_providers_and_network_requirement():
    one = unreachable_message(["baostock"])
    assert "BaoStock is not reachable" in one and "mainland China networks or through a proxy" in one
    assert "Tushare and BaoStock are not reachable" in unreachable_message(["tushare", "baostock"])


# ---------------------------------------------------------------- policy
def test_default_policy_denies_yahoo_and_personal_dev_example_allows_it_without_export():
    denied = default_policy().decide("yahoo", "bars_daily", "SSE")
    assert denied.reason == "license_terms_restrict_commercial_use"
    assert default_policy().decide("yahoo", "bars_daily", "HKEX").reason == "license_terms_restrict_commercial_use"
    allowed = personal_policy().decide("yahoo", "bars_daily", "HKEX")
    assert (allowed.store, allowed.display, allowed.model_input, allowed.export) == (True, True, True, False)
    assert personal_policy().load_error is None
    res = adapter(FakeTransport(reply(chart(MOUTAI_ROWS))), default_policy()).bars(SSE, start=date(2026, 11, 16), end=date(2026, 11, 19))
    assert res.status == "denied" and res.reason == "license_terms_restrict_commercial_use"


# ---------------------------------------------------------------- parsing
def test_parses_bars_nulls_zero_volume_adjclose_events_and_clamps_to_the_last_completed_session():
    transport = FakeTransport(reply(chart(MOUTAI_ROWS, events=MOUTAI_EVENTS)))
    res = adapter(transport).bars(SSE, start=date(2026, 11, 16), end=date(2026, 11, 20))
    assert res.status == "partial" and res.reason == "range_clamped"
    days = [b.ts_start.date().isoformat() for b in res.data]
    assert days == ["2026-11-16", "2026-11-18", "2026-11-19"]  # zero volume, null open and the partial today are dropped
    first = res.data[0]
    assert (first.open, first.close, first.volume) == (Decimal("1500.5000"), Decimal("1510.2500"), Decimal(25000))
    assert first.ts_start.tzinfo is not None and first.ts_start.hour == 9 and first.ts_start.minute == 30
    assert first.adj_factor == (Decimal("1500.0") / Decimal("1510.25")).quantize(Decimal("0.000001")) and first.adjustment == "splits"
    assert res.data[2].adj_factor is None  # null adjclose
    assert res.coverage.currency == "CNY" and res.coverage.actual["last"].startswith("2026-11-19")
    notes = " ".join(res.coverage.notes)
    assert "Dropped 1 placeholder rows with zero volume" in notes and "Dropped 1 rows with null or non-positive prices" in notes
    assert "Dropped 1 rows outside the requested range" in notes and "Dividend events in range (1): 2026-11-17 27.6735 CNY" in notes
    assert "Split events in range (1): 2026-11-18 2:1" in notes
    url, headers = transport.calls[0]
    assert "600519.SS" in url and "interval=1d" in url and "events=div%2Csplits" in url
    assert "ForesightNine" in headers["User-Agent"] and "Mozilla" not in headers["User-Agent"]
    # period1 inclusive local midnight of the start day; period2 exclusive midnight after the clamped end
    from tests.fin_data.yahoo_helpers import ts
    assert f"period1={ts('2026-11-16', hh=0, mm=0)}" in url and f"period2={ts('2026-11-20', hh=0, mm=0)}" in url


def test_start_is_inclusive_and_weekly_aggregation_is_local():
    res = adapter(FakeTransport(reply(chart(MOUTAI_ROWS)))).bars(SSE, start=date(2026, 11, 18), end=date(2026, 11, 19))
    assert [b.ts_start.date().isoformat() for b in res.data] == ["2026-11-18", "2026-11-19"] and res.status == "ok"
    week = adapter(FakeTransport(reply(chart(MOUTAI_ROWS)))).bars(SSE, interval="1week", start=date(2026, 11, 16), end=date(2026, 11, 19))
    assert len(week.data) == 1 and week.data[0].high == Decimal("1540.0000")


def test_hong_kong_currency_timezone_and_symbol():
    rows = [("2026-11-18", 520.0, 525.0, 515.0, 522.4, 1200000, 521.0)]
    transport = FakeTransport(reply(chart(rows, symbol="0700.HK", currency="HKD", tz="Asia/Hong_Kong")))
    res = adapter(transport).bars(cn.parse_hk_code("700.HK"), start=date(2026, 11, 18), end=date(2026, 11, 19))
    assert res.status == "ok" and res.coverage.currency == "HKD" and "0700.HK" in transport.calls[0][0]
    assert res.data[0].entity_id == "HKEX:0700" and res.data[0].ts_start.utcoffset().total_seconds() == 8 * 3600


def test_timestamp_in_utc_late_evening_converts_to_the_local_date():
    # 2026-11-17 16:30 UTC is already 2026-11-18 00:30 in Shanghai
    from datetime import datetime, timezone
    stamp = int(datetime(2026, 11, 17, 16, 30, tzinfo=timezone.utc).timestamp())
    doc = chart([("2026-11-18", 1, 2, 1, 1.5, 10, 1.5)])
    doc["chart"]["result"][0]["timestamp"] = [stamp]
    res = adapter(FakeTransport(reply(doc))).bars(SSE, start=date(2026, 11, 18), end=date(2026, 11, 19))
    assert [b.ts_start.date().isoformat() for b in res.data] == ["2026-11-18"]


def test_empty_after_filtering_and_range_after_latest_session():
    only_zero = chart([("2026-11-18", 1, 2, 1, 1.5, 0, 1.5)])
    assert adapter(FakeTransport(reply(only_zero))).bars(SSE, start=date(2026, 11, 18), end=date(2026, 11, 19)).reason == "no_bars"
    transport = FakeTransport(reply(only_zero))
    late = adapter(transport).bars(SSE, start=date(2026, 11, 25), end=date(2026, 11, 26))
    assert late.status == "empty" and late.reason == "range_after_latest_session" and not transport.calls


def test_intraday_is_rejected_without_a_call():
    transport = FakeTransport(reply(chart(MOUTAI_ROWS)))
    assert adapter(transport).bars(SSE, interval="5min", start=date(2026, 11, 16)).reason == "intraday_not_supported"
    assert not transport.calls


# ---------------------------------------------------------------- errors
@pytest.mark.parametrize("response,status,reason", [
    (Reply(429, b"Too Many Requests", {"Retry-After": "30"}), "rate_limited", "yahoo_rate_limited"),
    (Reply(200, b"Too Many Requests"), "rate_limited", "yahoo_rate_limited"),
    (Reply(401, b'{"finance":{"error":{"code":"Unauthorized","description":"Invalid Crumb"}}}'), "failed", "yahoo_unavailable"),
    (Reply(200, b'{"chart":{"result":null,"error":{"code":"Unauthorized","description":"Invalid Crumb"}}}'), "failed", "yahoo_unavailable"),
    (Reply(500, b"oops"), "failed", "yahoo_unavailable"),
    (Reply(404, b'{"chart":{"result":null,"error":{"code":"Not Found","description":"No data found, symbol may be delisted"}}}'), "empty", "no_data"),
    (Reply(200, b'{"chart":{"result":null,"error":{"code":"Not Found","description":"No data found, symbol may be delisted"}}}'), "empty", "no_data"),
    (Reply(200, b"<html>"), "failed", "invalid_response"),
    (Reply(200, b'{"chart":{"result":[]}}'), "failed", "invalid_response"),
])
def test_http_and_provider_errors_map_to_clear_reasons(response, status, reason):
    res = adapter(FakeTransport(response)).bars(SSE, start=date(2026, 11, 16), end=date(2026, 11, 19))
    assert (res.status, res.reason) == (status, reason)
    if reason == "yahoo_unavailable" and "rumb" in response.body.decode() or response.status_code == 401:
        assert "does not circumvent" in res.message
    if status == "rate_limited":
        assert res.retryable and res.suggested_actions[0].retry_after_seconds > 0


def test_transport_timeout_and_failure():
    assert adapter(FakeTransport(_TransportTimeout("t"))).bars(SSE, start=date(2026, 11, 16)).reason == "source_timeout"
    assert adapter(FakeTransport(OSError("boom"))).bars(SSE, start=date(2026, 11, 16)).reason == "yahoo_unavailable"


def test_pacer_spaces_calls_by_the_configured_interval(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_YAHOO_MIN_INTERVAL_SECONDS", "2")
    now, sleeps = [100.0], []
    pacer = Pacer(clock=lambda: now[0], sleep=sleeps.append)
    assert pacer.wait() == 0.0
    assert pacer.wait() == 2.0 and pacer.wait() == 4.0 and sleeps == [2.0, 4.0]
    now[0] += 10
    assert pacer.wait() == 0.0


# ---------------------------------------------------------------- HK codes
@pytest.mark.parametrize("text,market,expected", [
    ("0700", None, "0700"), ("700.HK", None, "0700"), ("0700.HK", None, "0700"), ("HK:700", None, "0700"),
    ("hkex:9988", None, "9988"), ("00700", None, "0700"), ("700", "HK", "0700"), ("2318.hk", None, "2318"),
    ("700", None, None), ("2024", None, None), ("600519", None, None), ("000001.SZ", None, None), ("AAPL", "HK", None),
])
def test_hk_code_normalisation(text, market, expected):
    got = cn.parse_hk_code(text, market)
    assert (got.code if got else None) == expected
    if got:
        assert got.entity_id == f"HKEX:{expected}" and got.yahoo_symbol == f"{expected}.HK"


def test_yahoo_symbols_for_a_share_exchanges():
    assert [cn.CnCode("600519", "SSE", False).yahoo_symbol, cn.CnCode("000001", "SZSE", False).yahoo_symbol,
            cn.CnCode("430047", "BJSE", False).yahoo_symbol] == ["600519.SS", "000001.SZ", "430047.BJ"]
