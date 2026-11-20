from datetime import date, datetime, timezone
from decimal import Decimal

from app.fin_data.adapters.sec_edgar import SecEdgarAdapter
from app.fin_data.adapters.twelve_data import TwelveDataAdapter
from app.fin_data.entity import parse_entity_id, resolve_entity
from app.fin_data.policy import FinDataPolicy, PolicyRule
from .helpers import NOW, UA, FakeTransport, Reply, json_reply, open_limiter

BYOK = dict(api_key="secret-key-123", credential_owner="user_byok", account_id="acct-1")


def td(transport, **kw):
    return TwelveDataAdapter(transport=transport, limiter=open_limiter(), now_fn=lambda: NOW, **kw)


def test_platform_key_denied_without_calling_provider(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_TWELVEDATA_API_KEY", "platform-key")
    t = FakeTransport(json_reply("td_time_series_ipo.json"))
    r = td(t).bars("ANT", exchange="SSE")
    assert r.status == "denied" and r.reason == "license_platform_key_not_permitted" and not t.calls


def test_byok_requires_explicit_key_and_account(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_TWELVEDATA_API_KEY", "platform-key")
    t = FakeTransport(json_reply("td_time_series_ipo.json"))
    assert td(t).bars("ANT", exchange="SSE", credential_owner="user_byok", account_id="a").reason == "api_key_not_configured"
    assert td(t).bars("ANT", exchange="SSE", api_key="k", credential_owner="user_byok").reason == "account_required"
    assert not t.calls


def test_daily_bars_exclusive_end_clamp_final_and_key_not_in_url():
    t = FakeTransport(json_reply("td_time_series_ipo.json"))
    r = td(t).bars("ANT", exchange="SSE", start=date(2026, 11, 16), end=date(2026, 11, 20), **BYOK)
    url, headers = t.calls[0]
    assert "end_date=2026-11-20" in url  # clamped end 11-19, +1 day exclusive
    assert "start_date=2026-11-16" in url and "secret-key-123" not in url
    assert headers["Authorization"] == "apikey secret-key-123"
    assert [b.ts_start.date() for b in r.data] == [date(2026, 11, d) for d in (16, 17, 18, 19)]  # 11-20 row filtered
    assert r.status == "partial" and r.reason == "range_clamped"
    assert r.suggested_actions[0].hint == "end_date<=2026-11-19"
    assert all(b.is_final for b in r.data) and r.coverage.is_final is True
    assert r.coverage.session_status == "open" and r.cost.credits == 1
    assert r.permissions.owner_scoped and not r.permissions.export
    assert any("delayed" in n for n in r.coverage.notes)
    b = r.data[0]
    assert b.ts_start.tzinfo is not None and b.open == Decimal("68.88") and b.volume == Decimal("185000000")
    assert "secret-key-123" not in str(r.to_wire())


def test_include_partial_marks_today_not_final():
    r = td(FakeTransport(json_reply("td_time_series_ipo.json"))).bars(
        "ANT", exchange="SSE", end=date(2026, 11, 20), include_partial=True, **BYOK)
    assert r.status == "ok" and r.data[-1].ts_start.date() == date(2026, 11, 20) and r.data[-1].is_final is False
    assert r.data[-2].is_final and r.coverage.is_final is False


def test_range_after_latest_session_is_empty_with_hint():
    r = td(FakeTransport(Reply())).bars("ANT", exchange="SSE", start=date(2026, 11, 20), **BYOK)
    assert r.status == "empty" and r.reason == "range_after_latest_session"
    assert "2026-11-19" in r.message


def test_intraday_drops_zero_volume_placeholders_and_counts():
    t = FakeTransport(json_reply("td_time_series_intraday_ipo.json"))
    r = td(t).bars("ANT", exchange="SSE", interval="30min", start=date(2026, 11, 18), end=date(2026, 11, 19), **BYOK)
    assert len(r.data) == 3 and all(b.volume > 0 for b in r.data)
    assert any("Dropped 2 zero-volume placeholder intraday bars" in n for n in r.coverage.notes)
    assert r.data[0].ts_start.isoformat() == "2026-11-18T09:30:00+08:00" and r.data[0].session == "regular"
    assert "interval=30min" in t.calls[0][0]


def test_invalid_request_and_provider_errors():
    a = td(FakeTransport(Reply()))
    assert a.bars("ANT", interval="2min", **BYOK).reason == "invalid_request"
    err = lambda code, msg: Reply(200, ('{"code":%d,"message":"%s","status":"error"}' % (code, msg)).encode())
    assert td(FakeTransport(err(429, "run out of API credits"))).bars("ANT", exchange="SSE", **BYOK).status == "rate_limited"
    r = td(FakeTransport(err(403, "available on Grow plan; secret-key-123"))).bars("ANT", exchange="SSE", **BYOK)
    assert r.status == "denied" and r.reason == "provider_plan_restriction" and "secret-key-123" not in r.message
    assert td(FakeTransport(err(404, "symbol not found"))).bars("ANT", exchange="SSE", **BYOK).status == "empty"


def test_transport_failure_is_unknown_not_retryable():
    from app.openai_compatible import _TransportFailure
    r = td(FakeTransport(_TransportFailure("x"))).bars("ANT", exchange="SSE", **BYOK)
    assert r.status == "unknown" and r.retryable is False


def test_byok_rate_limit_is_per_account():
    from app.fin_data.ratelimit import InProcessRateLimiter, ProviderLimit
    lim = InProcessRateLimiter({"twelve_data": ProviderLimit(1.0 / 60, 1.0, 100)}, now=lambda: NOW)
    t = FakeTransport(json_reply("td_symbol_search.json"))
    a = TwelveDataAdapter(transport=t, limiter=lim, now_fn=lambda: NOW)
    assert a.symbol_search("EXM", api_key="k", credential_owner="user_byok", account_id="a1").status == "ok"
    assert a.symbol_search("EXM", api_key="k", credential_owner="user_byok", account_id="a1").status == "rate_limited"
    assert a.symbol_search("EXM", api_key="k2", credential_owner="user_byok", account_id="a2").status == "ok"


def test_symbol_search_normalizes_exchange_and_asset_type():
    r = td(FakeTransport(json_reply("td_symbol_search.json"))).symbol_search("EXM", **BYOK)
    ids = {e.entity_id: e for e in r.data}
    assert set(ids) == {"NASDAQ:EXM", "HKEX:EXM", "OTC:EXM"}
    assert ids["NASDAQ:EXM"].exchange == "NASDAQ" and ids["OTC:EXM"].exchange is None
    assert ids["OTC:EXM"].identifiers["exchange_raw"] == "OTC" and ids["OTC:EXM"].asset_type == "etf"


# ------------------------------------------------------------------ entity
def make(sec_json="sec_tickers.json", td_json="td_symbol_search.json"):
    sec = SecEdgarAdapter(UA, transport=FakeTransport(json_reply(sec_json)), limiter=open_limiter(), now_fn=lambda: NOW)
    tw = td(FakeTransport(json_reply(td_json)))
    return sec, tw


def test_resolve_entity_ambiguous_returns_all_never_picks():
    sec, tw = make()
    r = resolve_entity("EXM", sec=sec, twelve=tw, twelve_kwargs=BYOK)
    assert r.reason == "multiple_candidates" and r.status == "ok"
    ids = [c.entity.entity_id for c in r.data]
    assert "NASDAQ:EXM" in ids and "HKEX:EXM" in ids and len(ids) >= 3
    assert all(not c.unique for c in r.data)
    nasdaq = next(c for c in r.data if c.entity.entity_id == "NASDAQ:EXM")
    assert nasdaq.entity.identifiers["cik"] == "0000320193" and set(nasdaq.sources) == {"twelve_data", "sec_edgar"}
    assert nasdaq.match_basis == "ticker_exact"
    assert not r.permissions.export  # narrowed by the more restrictive source


def test_resolve_entity_market_filter_makes_unique():
    sec, tw = make()
    r = resolve_entity("EXM", market="HK", sec=sec, twelve=tw, twelve_kwargs=BYOK)
    assert [c.entity.entity_id for c in r.data] == ["HKEX:EXM"] and r.data[0].unique and r.reason == "ok"
    assert r.data[0].entity.currency == "HKD"
    assert resolve_entity("EXM", market="XX").reason == "unknown_market"


def test_resolve_entity_sec_only_when_twelve_denied_and_names():
    sec = SecEdgarAdapter(UA, transport=FakeTransport(json_reply("sec_tickers.json")), limiter=open_limiter(), now_fn=lambda: NOW)
    tw = td(FakeTransport(Reply()))  # platform key -> denied
    r = resolve_entity("example corp", market="US", sec=sec, twelve=tw)
    assert r.status == "partial" and r.reason == "source_gap"
    assert [c.entity.entity_id for c in r.data] == ["cik:0000320193"] and r.data[0].match_basis == "name_exact"
    assert any("denied" in n for n in r.coverage.notes)
    assert parse_entity_id("cik:0000320193")["cik"] == "0000320193"
    assert parse_entity_id("HKEX:0700") == {"exchange": "HKEX", "symbol": "0700", "cik": None}


def test_resolve_entity_asset_type_and_no_source():
    sec, tw = make()
    r = resolve_entity("EXM", asset_type="etf", sec=sec, twelve=tw, twelve_kwargs=BYOK)
    assert [c.entity.entity_id for c in r.data] == ["OTC:EXM"]
    assert resolve_entity("EXM").reason == "no_source_available"
