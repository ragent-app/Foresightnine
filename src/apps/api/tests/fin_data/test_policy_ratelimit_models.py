import json
from datetime import datetime, timedelta, timezone

import pytest

from app.fin_data.models import Cost, Coverage, Permissions, Provenance, ToolResult
from app.fin_data.policy import (Denied, FinDataPolicy, PolicyRule, bar_category, cache_scope_key, default_policy,
                                 load_policy)
from app.fin_data.ratelimit import InProcessRateLimiter, ProviderLimit, default_limits


def test_default_policy_sec_allows_everything_including_export():
    p = default_policy()
    for cat in ("identity", "filings", "financials", "ownership"):
        assert p.decide("sec_edgar", cat) == Permissions(store=True, display=True, model_input=True, export=True)


def test_default_policy_not_listed_denied():
    p = default_policy()
    d = p.decide("sec_edgar", "filing_search")
    assert isinstance(d, Denied) and d.reason == "not_in_policy"
    assert p.decide("alpha_vantage", "bars_daily").reason == "not_in_policy"
    assert "5332349" in p.notes and "pricing.md" in p.notes


def test_twelve_data_platform_key_denied_byok_owner_scoped_no_export():
    p = default_policy()
    d = p.decide("twelve_data", "bars_daily", "NASDAQ", "equity", "1day")
    assert isinstance(d, Denied) and d.reason == "license_platform_key_not_permitted"
    g = p.decide("twelve_data", "bars_intraday", "NASDAQ", None, "5min", credential_owner="user_byok")
    assert g == Permissions(store=True, display=True, model_input=True, export=False, owner_scoped=True)


def test_asx_never_displayable_even_byok_and_platform_denied():
    p = default_policy()
    g = p.decide("twelve_data", "bars_daily", "ASX", None, "1day", credential_owner="user_byok")
    assert isinstance(g, Permissions) and not g.display and not g.export and g.model_input
    assert isinstance(p.decide("twelve_data", "bars_daily", "ASX", None, "1day"), Denied)


def test_request_cannot_widen_permissions():
    p = default_policy()
    g = p.decide("sec_edgar", "filings", requested=Permissions(store=True, display=False, model_input=True, export=True))
    assert g == Permissions(store=True, display=False, model_input=True, export=True)
    narrow = FinDataPolicy(rules=[PolicyRule(provider="x", category="c", store=True)])
    g2 = narrow.decide("x", "c", requested=Permissions(store=True, display=True, model_input=True, export=True))
    assert g2 == Permissions(store=True)


def test_specific_rule_narrows_and_ties_intersect():
    p = FinDataPolicy(rules=[
        PolicyRule(provider="v", category="bars_daily", store=True, display=True, model_input=True, export=True),
        PolicyRule(provider="v", category="bars_daily", exchange="hkex", store=True, display=False, model_input=True),
        PolicyRule(provider="v", category="bars_daily", interval="1day", store=True, display=True, model_input=False)])
    g = p.decide("v", "bars_daily", "HKEX", None, "1day")  # two specificity-1 rules tie -> intersection
    assert g == Permissions(store=True)
    assert p.decide("v", "bars_daily", "NYSE", None, "1week").export is True


def test_bar_category_and_cache_scope():
    assert bar_category("1day") == "bars_daily" and bar_category("5min") == "bars_intraday"
    assert cache_scope_key(Permissions(store=True), None) == "global"
    assert cache_scope_key(Permissions(store=True, owner_scoped=True), "acct1") == "owner:acct1"
    with pytest.raises(ValueError):
        cache_scope_key(Permissions(owner_scoped=True), None)


def test_load_policy_env_and_invalid_denies_all():
    assert load_policy({}).version.endswith("default")
    custom = json.dumps({"version": "c", "rules": [{"provider": "sec_edgar", "category": "filings", "store": True}]})
    p = load_policy({"FORESIGHTNINE_FIN_DATA_POLICY": custom})
    assert p.decide("sec_edgar", "filings") == Permissions(store=True)
    assert isinstance(p.decide("sec_edgar", "financials"), Denied)
    bad = load_policy({"FORESIGHTNINE_FIN_DATA_POLICY": "{nope"})
    assert bad.load_error and isinstance(bad.decide("sec_edgar", "filings"), Denied)


def test_rate_limiter_bucket_wait_hint_and_refill():
    t = {"now": 0.0}
    clock = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    lim = InProcessRateLimiter({"p": ProviderLimit(rate_per_sec=1.0, burst=2.0)}, clock=lambda: t["now"], now=lambda: clock)
    assert lim.acquire("p").allowed and lim.acquire("p").allowed
    d = lim.acquire("p")
    assert not d.allowed and d.reason == "rate_limit" and d.wait_seconds == pytest.approx(1.0)
    t["now"] = 1.0
    assert lim.acquire("p").allowed
    assert lim.acquire("unlisted").allowed


def test_daily_quota_exhaustion_waits_until_utc_midnight_and_resets():
    clock = {"now": datetime(2026, 11, 20, 22, 0, tzinfo=timezone.utc)}
    lim = InProcessRateLimiter({"p": ProviderLimit(1000, 1000, daily_quota=2)}, now=lambda: clock["now"])
    assert lim.acquire("p").allowed and lim.acquire("p").daily_remaining == 0
    d = lim.acquire("p")
    assert not d.allowed and d.reason == "daily_quota" and d.wait_seconds == pytest.approx(7200)
    clock["now"] += timedelta(hours=3)
    assert lim.acquire("p").allowed


def test_default_limits_from_env():
    d = default_limits({"FORESIGHTNINE_TWELVEDATA_RATE_PER_MIN": "4", "FORESIGHTNINE_TWELVEDATA_DAILY_QUOTA": "bad"})
    assert d["sec_edgar"].rate_per_sec == 10
    assert d["twelve_data"].burst == 4 and d["twelve_data"].daily_quota == 800
    assert default_limits({})["twelve_data"].burst == 8


def test_tool_result_envelope_serialization_and_rules():
    r = ToolResult(status="ok", reason="ok", coverage=Coverage(rows=0, truncated=False),
                   permissions=Permissions(store=True), provenance=Provenance(
                       provider="p", endpoint="u", retrieved_at=datetime(2026, 11, 20, tzinfo=timezone.utc), response_sha256="ab"),
                   cost=Cost(provider_calls=1))
    wire = r.to_wire()
    assert set(wire) >= {"status", "reason", "message", "retryable", "suggested_actions", "coverage", "permissions",
                         "provenance", "source_ids", "cost"}
    assert wire["cost"] == {"provider_calls": 1, "credits": None}
    assert json.dumps(wire)
    with pytest.raises(ValueError):
        ToolResult(status="rate_limited", reason="x", retryable=False)
    with pytest.raises(ValueError):
        ToolResult(status="empty", reason="x", data=[1])
    with pytest.raises(ValueError):
        ToolResult(status="denied", reason="x", permissions=Permissions(store=True))
    with pytest.raises(ValueError):
        ToolResult(status="bogus", reason="x")
