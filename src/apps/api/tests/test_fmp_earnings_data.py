"""Unit tests for external earnings contracts, credential policy, FMP adapter, and graceful degradation."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256

import pytest

from app.earnings_external_data import (
    CONSENSUS_FALLBACK_SUGGESTIONS,
    EarningsConsensus,
    EarningsTranscript,
    ExternalEarningsProvider,
    ExternalEarningsService,
    Permissions,
    TRANSCRIPT_FALLBACK_SUGGESTIONS,
    default_third_party_permissions,
    parse_transcript_sections,
    redact_secrets,
    resolve_external_credential,
    sanitize_endpoint_url,
)
from app.fin_data.adapters.fmp import FmpEarningsAdapter
from app.fin_data.policy import FinDataPolicy, PolicyRule
from app.fin_data.ratelimit import InProcessRateLimiter, ProviderLimit
from app.openai_compatible import BinaryHttpResponse, _TransportFailure, _TransportTimeout

from tests.fin_data.helpers import FakeTransport, NOW, Reply, open_limiter


def _json_reply(payload: object, status: int = 200, headers: dict[str, str] | None = None) -> Reply:
    return Reply(
        status_code=status,
        body=json.dumps(payload).encode("utf-8"),
        headers=headers or {"content-type": "application/json"},
    )


class RouterTransport:
    """Offline HTTP transport routing by URL substring."""

    def __init__(self, routes: dict[str, Reply | Exception]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict[str, str], float]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes, timeout: float):
        self.calls.append((method, url, dict(headers), timeout))
        for pattern, outcome in self.routes.items():
            if pattern in url:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        return Reply(404, b"[]")


def _fmp(transport, **kwargs) -> FmpEarningsAdapter:
    return FmpEarningsAdapter(
        transport=transport,
        limiter=kwargs.pop("limiter", open_limiter()),
        now_fn=lambda: NOW,
        **kwargs,
    )


SAMPLE_TRANSCRIPT = """Presentation

Operator: Good day, everyone, and welcome to the Example Corp Q3 2026 Earnings Conference Call. At this time, I would like to turn the call over to Jane Doe, Investor Relations.

Jane Doe: Thank you, Operator. Good afternoon and thank you for joining us today.
Note: Forward-looking statements apply to all commentary below.

John Smith - CEO: Revenue grew 22% year over year driven by enterprise demand.

Question-and-Answer Session

Operator: We will now open the call for questions. Our first question comes from Alice Analyst.

Alice Analyst: Thanks for taking my question. How should we think about Q4 gross margin?

John Smith - CEO: We expect gross margin expansion of 50 bps sequentially.
"""


# --------------------------------------------------------------------------- contracts & parsing
def test_data_contracts_use_decimal_sha256_and_default_export_false() -> None:
    c = EarningsConsensus(
        symbol=" aapl ",
        fiscal_period="2026Q3",
        period_end=date(2026, 6, 27),
        revenue_estimate_avg="93500000000.50",
        revenue_estimate_low=92000000000,
        revenue_estimate_high=95000000000,
        eps_estimate_avg=1.62,
        eps_estimate_low="1.54",
        eps_estimate_high="1.70",
        revenue_actual=None,
        eps_actual=None,
        num_analysts_revenue=28,
        num_analysts_eps=31,
        source_provider="fmp",
        as_of=NOW,
    )
    assert c.symbol == "AAPL"
    assert c.revenue_estimate_avg == Decimal("93500000000.50")
    assert c.eps_estimate_avg == Decimal("1.62")
    assert c.num_analysts == 31
    assert c.permissions == Permissions(store=True, display=True, model_input=True, export=False)
    assert c.permissions.export is False
    # Serialized JSON preserves Decimal strings without binary float drift
    wire = c.model_dump(mode="json")
    assert wire["eps_estimate_avg"] == "1.62"

    t = EarningsTranscript.from_raw_text(
        symbol="exm",
        fiscal_year=2026,
        fiscal_quarter=3,
        call_date=NOW,
        full_text=SAMPLE_TRANSCRIPT,
        source_provider="fmp",
    )
    assert t.symbol == "EXM"
    assert t.content_sha256 == sha256(SAMPLE_TRANSCRIPT.strip().encode("utf-8")).hexdigest()
    assert t.permissions.export is False
    assert t.speakers == ["Operator", "Jane Doe", "John Smith - CEO", "Alice Analyst"]
    assert [sec.name for sec in t.sections] == ["prepared_remarks", "qa"]
    assert "Note: Forward-looking statements" not in t.speakers
    assert any(turn.role == "executive" for turn in t.sections[0].turns)
    assert any(turn.speaker == "Alice Analyst" for turn in t.sections[1].turns)

    adapter = _fmp(FakeTransport(Reply()))
    assert isinstance(adapter, ExternalEarningsProvider)


def test_parse_transcript_sections_plain_text_fallback_avoids_false_speakers() -> None:
    plain = "Note: Unformatted release text.\nQ1: Revenue was $10B at 10:30 AM: strong demand."
    sections, speakers = parse_transcript_sections(plain)
    assert speakers == []
    assert len(sections) == 1 and sections[0].name == "full_transcript"
    assert "Unformatted release text" in sections[0].text


# --------------------------------------------------------------------------- credential policy (BYOK vs platform)
def test_strict_byok_blocks_platform_env_key_by_default_and_degrades_gracefully(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_FMP_API_KEY", "platform-secret-9999")
    monkeypatch.delenv("FORESIGHTNINE_FMP_ALLOW_PLATFORM_KEY", raising=False)

    transport = FakeTransport(Reply(200, b"[]"))
    service = ExternalEarningsService(provider=_fmp(transport))

    caps = service.capabilities()
    assert caps["consensus_available"] is False
    assert caps["transcript_available"] is False
    assert caps["allow_platform_key"] is False
    assert caps["reason"] == "byok_key_required"

    c_res = service.get_consensus("AAPL")
    assert c_res.status == "unavailable"
    assert c_res.degraded is True
    assert c_res.reason == "byok_key_required"
    assert c_res.permissions == Permissions.none()
    assert any("8-K Exhibit 99.1" in s for s in c_res.fallback_suggestions)
    assert "platform-secret-9999" not in c_res.model_dump_json()

    t_res = service.get_transcript("AAPL", fiscal_year=2026, fiscal_quarter=3)
    assert t_res.status == "unavailable"
    assert t_res.degraded is True
    assert t_res.reason == "byok_key_required"
    assert any("Upload" in s or "upload" in s for s in t_res.fallback_suggestions)
    assert not transport.calls

    # Direct adapter call as platform also denies when allow_platform_key is not enabled
    direct = _fmp(transport).fetch_consensus("AAPL", credential_owner="platform")
    assert direct.status == "denied"
    assert direct.reason == "license_platform_key_not_permitted"
    assert not transport.calls


def test_platform_key_works_when_explicitly_enabled_and_byok_takes_precedence() -> None:
    env = {
        "FORESIGHTNINE_FMP_API_KEY": "platform-dev-key-1111",
        "FORESIGHTNINE_FMP_ALLOW_PLATFORM_KEY": "true",
    }
    transport = RouterTransport(
        {
            "/stable/analyst-estimates": _json_reply(
                [
                    {
                        "symbol": "AAPL",
                        "date": "2026-06-27",
                        "revenueAvg": 93500000000,
                        "epsAvg": 1.62,
                        "numAnalystsEps": 25,
                    }
                ]
            ),
            "/stable/earnings": _json_reply([]),
        }
    )
    service = ExternalEarningsService(
        provider=_fmp(transport),
        env=env,
    )

    # 1. Uses platform key when allowed and no BYOK key provided
    plat_res = service.get_consensus("AAPL")
    assert plat_res.status == "ok"
    assert plat_res.credential_owner == "platform"
    assert plat_res.permissions == Permissions(
        store=True, display=True, model_input=True, export=False, owner_scoped=False
    )
    assert transport.calls[0][2]["apikey"] == "platform-dev-key-1111"

    # 2. User BYOK key overrides platform key and marks owner_scoped=True, export=False
    byok_res = service.get_consensus("AAPL", byok_api_key="user-byok-secret-2222", account_id="acct-42")
    assert byok_res.status == "ok"
    assert byok_res.credential_owner == "user_byok"
    assert byok_res.permissions == Permissions(
        store=True, display=True, model_input=True, export=False, owner_scoped=True
    )
    assert transport.calls[-2][2]["apikey"] == "user-byok-secret-2222"


def test_byok_resolver_and_explicit_policy_enforcement() -> None:
    transport = FakeTransport(
        _json_reply(
            [
                {
                    "symbol": "MSFT",
                    "date": "2026-06-30",
                    "revenueAvg": 70000000000,
                    "epsAvg": 3.45,
                }
            ]
        ),
        _json_reply([]),
    )
    cred = resolve_external_credential(
        byok_resolver=lambda acct, prov: f"resolved-{acct}-{prov}" if acct == "acct-9" else None,
        account_id="acct-9",
        env={"FORESIGHTNINE_FMP_API_KEY": "plat-x", "FORESIGHTNINE_FMP_ALLOW_PLATFORM_KEY": "false"},
    )
    assert cred.configured is True
    assert cred.credential_owner == "user_byok"
    assert "resolved-acct-9-fmp" not in repr(cred)
    assert "resolved-acct-9-fmp" not in str(cred)

    # Custom policy with no FMP rules denies access cleanly
    denied_service = ExternalEarningsService(
        provider=_fmp(transport, policy=FinDataPolicy(rules=[])),
        byok_resolver=lambda acct, prov: "user-secret-key",
        account_id="acct-9",
    )
    out = denied_service.get_consensus("MSFT")
    assert out.status == "unavailable"
    assert out.reason == "not_in_policy"
    assert out.provider_status == "denied"
    assert not transport.calls

    # Policy explicitly denying platform overrides allow_platform_key=True
    plat_denied_policy = FinDataPolicy(
        rules=[
            PolicyRule(
                provider="fmp",
                category="consensus",
                credential_owner="platform",
                deny=True,
                deny_reason="license_platform_key_not_permitted",
            )
        ]
    )
    plat_denied_svc = ExternalEarningsService(
        transport=transport,
        allow_platform_key=True,
        env={"FORESIGHTNINE_FMP_API_KEY": "plat-secret-123"},
        policy=plat_denied_policy,
    )
    out2 = plat_denied_svc.get_consensus("MSFT")
    assert out2.status == "unavailable"
    assert out2.reason == "license_platform_key_not_permitted"
    assert not transport.calls


# --------------------------------------------------------------------------- key redaction & safe diagnostics
def test_api_key_redacted_everywhere_in_urls_errors_and_logs(caplog) -> None:
    secret = "super-secret-fmp-key-xyz987"
    caplog.set_level(logging.DEBUG, logger="foresightnine.fin_data.fmp")

    # 1. URL sanitization & redact_secrets helper
    assert sanitize_endpoint_url(f"https://financialmodelingprep.com/stable/earnings?symbol=AAPL&apikey={secret}") == (
        "https://financialmodelingprep.com/stable/earnings?symbol=AAPL"
    )
    assert secret not in redact_secrets(f"failed url=...&apikey={secret} Authorization: apikey {secret}", (secret,))

    # 2. HTTP 401/403/429 echoing the key in response body
    t401 = FakeTransport(_json_reply({"Error Message": f"Invalid API KEY {secret}"}, status=401))
    r401 = _fmp(t401).fetch_consensus("AAPL", api_key=secret, credential_owner="user_byok")
    assert r401.status == "failed" and r401.reason == "invalid_api_key"
    assert secret not in r401.message
    assert secret not in json.dumps(r401.to_wire())
    # Key was never placed in request URL
    assert secret not in t401.calls[0][0]
    assert t401.calls[0][1]["apikey"] == secret

    t403 = FakeTransport(_json_reply({"Error Message": f"Special Endpoint requires Upgrade for key={secret}"}, status=403))
    r403 = _fmp(t403).fetch_consensus("AAPL", api_key=secret, credential_owner="user_byok")
    assert r403.status == "denied" and r403.reason == "provider_plan_restriction"
    assert secret not in r403.message

    t429 = FakeTransport(
        _json_reply({"Error Message": f"Limit Reach for apikey={secret}"}, status=429, headers={"Retry-After": "12"})
    )
    r429 = _fmp(t429).fetch_consensus("AAPL", api_key=secret, credential_owner="user_byok")
    assert r429.status == "rate_limited" and r429.retryable is True
    assert r429.suggested_actions[0].retry_after_seconds == 12.0
    assert secret not in r429.message

    # 3. Transport exception echoing secret is also scrubbed from logs and response
    t_err = FakeTransport(_TransportFailure(f"GET https://example.com?apikey={secret} failed"))
    r_err = _fmp(t_err).fetch_transcript("AAPL", fiscal_year=2026, fiscal_quarter=3, api_key=secret, credential_owner="user_byok")
    assert r_err.status == "unknown" and r_err.retryable is False
    assert secret not in r_err.message
    assert secret not in caplog.text


# --------------------------------------------------------------------------- consensus parsing (stable & v3)
def test_consensus_parsing_stable_merges_estimates_and_actuals() -> None:
    transport = RouterTransport(
        {
            "/stable/analyst-estimates": _json_reply(
                [
                    {
                        "symbol": "AAPL",
                        "date": "2026-09-26",
                        "revenueAvg": 102500000000,
                        "revenueLow": 100100000000,
                        "revenueHigh": 104900000000,
                        "epsAvg": 1.85,
                        "epsLow": 1.78,
                        "epsHigh": 1.92,
                        "numAnalystsRevenue": 30,
                        "numAnalystsEps": 34,
                    },
                    {
                        "symbol": "AAPL",
                        "date": "2026-06-27",
                        "revenueAvg": 94100000000,
                        "revenueLow": 92500000000,
                        "revenueHigh": 95800000000,
                        "epsAvg": 1.61,
                        "epsLow": 1.55,
                        "epsHigh": 1.68,
                        "numAnalystsRevenue": 29,
                        "numAnalystsEps": 33,
                    },
                ]
            ),
            "/stable/earnings": _json_reply(
                [
                    {
                        "symbol": "AAPL",
                        "date": "2026-07-30",
                        "epsActual": 1.67,
                        "epsEstimated": 1.61,
                        "revenueActual": 95200000000,
                        "revenueEstimated": 94100000000,
                        "lastUpdated": "2026-07-31",
                    }
                ]
            ),
        }
    )
    svc = ExternalEarningsService(provider=_fmp(transport))
    res = svc.get_consensus("AAPL", period="quarter", limit=4, byok_api_key="byok-key-1", account_id="acct-1")
    assert res.status == "ok" and res.degraded is False
    assert len(res.consensus) == 2

    q_future, q_reported = res.consensus[0], res.consensus[1]
    assert q_future.period_end == date(2026, 9, 26)
    assert q_future.fiscal_period == "2026Q3"
    assert q_future.revenue_actual is None and q_future.eps_actual is None
    assert q_future.eps_estimate_avg == Decimal("1.85")
    assert q_future.num_analysts == 34

    assert q_reported.period_end == date(2026, 6, 27)
    assert q_reported.reported_date == date(2026, 7, 30)
    assert q_reported.revenue_actual == Decimal("95200000000")
    assert q_reported.eps_actual == Decimal("1.67")
    assert q_reported.permissions.export is False
    assert q_reported.permissions.owner_scoped is True
    assert res.cost.provider_calls == 2
    assert len(res.provenance.endpoints) == 2
    assert all("byok-key-1" not in ep for ep in res.provenance.endpoints)


def test_consensus_parsing_v3_and_annual_does_not_mix_quarterly_actuals() -> None:
    transport = RouterTransport(
        {
            "/api/v3/analyst-estimates/NVDA": _json_reply(
                [
                    {
                        "symbol": "NVDA",
                        "date": "2027-01-31",
                        "estimatedRevenueAvg": 180000000000,
                        "estimatedRevenueLow": 175000000000,
                        "estimatedRevenueHigh": 188000000000,
                        "estimatedEpsAvg": 4.25,
                        "estimatedEpsLow": 4.05,
                        "estimatedEpsHigh": 4.50,
                        "numberAnalystEstimatedRevenue": 40,
                        "numberAnalystEstimatedEps": 42,
                    }
                ]
            ),
            "/api/v3/earnings-surprises/NVDA": _json_reply(
                [
                    {
                        "symbol": "NVDA",
                        "date": "2027-02-24",
                        "actualEarningResult": 1.15,
                        "estimatedEarning": 1.09,
                    }
                ]
            ),
        }
    )
    adapter = _fmp(transport, api_version="v3")
    res = adapter.fetch_consensus("NVDA", period="annual", api_key="byok-nvda", credential_owner="user_byok")
    assert res.status == "ok"
    row = res.data[0]
    assert row.fiscal_period == "FY2027"
    assert row.revenue_estimate_avg == Decimal("180000000000")
    assert row.eps_estimate_avg == Decimal("4.25")
    assert row.num_analysts == 42
    # Quarterly surprise row must NOT overwrite full-year annual actuals
    assert row.eps_actual is None


def test_consensus_partial_survival_when_actuals_endpoint_fails() -> None:
    transport = RouterTransport(
        {
            "/stable/analyst-estimates": _json_reply(
                [{"symbol": "MSFT", "date": "2026-06-30", "revenueAvg": 70000000000, "epsAvg": 3.45}]
            ),
            "/stable/earnings": _json_reply({"Error Message": "Exclusive Endpoint"}, status=403),
        }
    )
    res = ExternalEarningsService(provider=_fmp(transport)).get_consensus("MSFT", byok_api_key="byok-msft")
    assert res.status == "partial"
    assert res.degraded is False
    assert res.reason == "actuals_unavailable"
    assert len(res.consensus) == 1
    assert res.consensus[0].eps_estimate_avg == Decimal("3.45")


# --------------------------------------------------------------------------- transcript parsing (stable & v3)
def test_transcript_parsing_stable_explicit_and_latest_dates_lookup() -> None:
    transport = RouterTransport(
        {
            "/stable/earning-call-transcript-dates": _json_reply(
                [
                    {"quarter": 2, "fiscalYear": 2026, "date": "2026-04-30"},
                    {"quarter": 3, "fiscalYear": 2026, "date": "2026-07-30"},
                ]
            ),
            "/stable/earning-call-transcript?symbol=EXM&year=2026&quarter=3": _json_reply(
                [
                    {
                        "symbol": "EXM",
                        "quarter": 3,
                        "year": 2026,
                        "date": "2026-07-30 21:00:00",
                        "content": SAMPLE_TRANSCRIPT,
                    }
                ]
            ),
        }
    )
    svc = ExternalEarningsService(provider=_fmp(transport))
    # Latest lookup resolves dates first, then pulls Q3 2026
    res = svc.get_transcript("EXM", byok_api_key="byok-exm", account_id="acct-7")
    assert res.status == "ok" and res.degraded is False
    tr = res.transcript
    assert tr is not None
    assert tr.symbol == "EXM"
    assert tr.fiscal_year == 2026 and tr.fiscal_quarter == 3
    assert tr.call_date == datetime(2026, 7, 30, 21, 0, tzinfo=timezone.utc)
    assert tr.permissions == Permissions(store=True, display=True, model_input=True, export=False, owner_scoped=True)
    assert len(tr.content_sha256) == 64
    assert res.cost.provider_calls == 2
    assert any("earning-call-transcript-dates" in ep for ep in res.provenance.endpoints)


def test_transcript_parsing_v3_handles_q_string_and_binary_safe_error() -> None:
    t_v3 = FakeTransport(
        _json_reply(
            [
                {
                    "symbol": "AAPL",
                    "quarter": "Q2",
                    "fiscalYear": 2026,
                    "date": "2026-04-30",
                    "content": SAMPLE_TRANSCRIPT,
                }
            ]
        )
    )
    res = _fmp(t_v3, api_version="v3").fetch_transcript("AAPL", api_key="byok-aapl", credential_owner="user_byok")
    assert res.status == "ok"
    assert res.data[0].fiscal_quarter == 2
    assert res.data[0].call_date == date(2026, 4, 30)

    # Also verify BinaryHttpResponse with empty body and safe_error dict (real transport error shape)
    t_bin = FakeTransport(BinaryHttpResponse(403, b"", {}, {"type": "Special Endpoint subscription upgrade required"}))
    r_bin = _fmp(t_bin).fetch_transcript("AAPL", fiscal_year=2026, fiscal_quarter=2, api_key="byok-aapl", credential_owner="user_byok")
    assert r_bin.status == "denied" and r_bin.reason == "provider_plan_restriction"


# --------------------------------------------------------------------------- graceful degradation & error handling
def test_graceful_degradation_on_404_empty_429_rate_limit_and_timeouts() -> None:
    svc_404 = ExternalEarningsService(provider=_fmp(FakeTransport(Reply(404, b"[]"))))
    c_404 = svc_404.get_consensus("UNCOVERED", byok_api_key="byok-1")
    assert c_404.status == "unavailable" and c_404.degraded is True
    assert c_404.reason == "ticker_uncovered"
    assert c_404.provider_status == "empty"
    assert c_404.fallback_suggestions == list(CONSENSUS_FALLBACK_SUGGESTIONS)
    assert any(a.action == "sec_earnings_release" for a in c_404.suggested_actions)

    svc_empty = ExternalEarningsService(provider=_fmp(FakeTransport(Reply(200, b"[]"))))
    t_empty = svc_empty.get_transcript("UNCOVERED", fiscal_year=2026, fiscal_quarter=3, byok_api_key="byok-1")
    assert t_empty.status == "unavailable" and t_empty.degraded is True
    assert t_empty.reason == "transcript_unavailable"
    assert t_empty.fallback_suggestions == list(TRANSCRIPT_FALLBACK_SUGGESTIONS)
    assert any(a.action == "user_upload_transcript" for a in t_empty.suggested_actions)

    # 429 HTTP status
    svc_429 = ExternalEarningsService(
        provider=_fmp(FakeTransport(Reply(429, b'{"Error Message":"Limit Reach"}', {"Retry-After": "45"})))
    )
    c_429 = svc_429.get_consensus("AAPL", byok_api_key="byok-1")
    assert c_429.status == "unavailable" and c_429.degraded is True
    assert c_429.reason == "provider_rate_limited"
    assert c_429.provider_status == "rate_limited"
    assert c_429.retryable is True
    assert c_429.suggested_actions[0].retry_after_seconds == 45.0
    assert any("8-K Exhibit 99.1" in s for s in c_429.fallback_suggestions)

    # Local rate limiter isolation per BYOK account
    tight = InProcessRateLimiter({"fmp": ProviderLimit(rate_per_sec=0.001, burst=1.0, daily_quota=10)}, now=lambda: NOW)
    adapter = _fmp(
        FakeTransport(_json_reply([{"symbol": "AAPL", "date": "2026-06-27", "epsAvg": 1.62}])),
        limiter=tight,
    )
    assert adapter.fetch_consensus("AAPL", include_actuals=False, api_key="k1", credential_owner="user_byok", account_id="a1").status == "ok"
    rl = adapter.fetch_consensus("AAPL", include_actuals=False, api_key="k1", credential_owner="user_byok", account_id="a1")
    assert rl.status == "rate_limited" and rl.retryable is True
    # Separate account bucket still succeeds
    assert adapter.fetch_consensus("AAPL", include_actuals=False, api_key="k2", credential_owner="user_byok", account_id="a2").status == "ok"

    # HTTP 200 carrying FMP {"Error Message": "Limit Reach..."} also maps to rate_limited
    svc_200_rl = ExternalEarningsService(
        provider=_fmp(FakeTransport(_json_reply({"Error Message": "Limit Reach. Please upgrade your plan."})))
    )
    c_200_rl = svc_200_rl.get_consensus("AAPL", byok_api_key="byok-1")
    assert c_200_rl.status == "unavailable" and c_200_rl.provider_status == "rate_limited" and c_200_rl.retryable is True

    # Timeout degrades cleanly
    svc_tmo = ExternalEarningsService(provider=_fmp(FakeTransport(_TransportTimeout(timeout_kind="response_timeout"))))
    t_tmo = svc_tmo.get_transcript("AAPL", fiscal_year=2026, fiscal_quarter=3, byok_api_key="byok-1")
    assert t_tmo.status == "unavailable" and t_tmo.reason == "source_timeout" and t_tmo.retryable is False


def test_surprises_endpoint_and_empty_estimates_fallback() -> None:
    # Direct fetch_earnings_surprises
    t = FakeTransport(
        _json_reply(
            [
                {
                    "symbol": "AAPL",
                    "date": "2026-07-30",
                    "epsActual": 1.67,
                    "epsEstimated": 1.61,
                    "revenueActual": 95200000000,
                    "revenueEstimated": 94100000000,
                }
            ]
        )
    )
    res = _fmp(t).fetch_earnings_surprises("AAPL", api_key="byok-aapl", credential_owner="user_byok")
    assert res.status == "ok" and len(res.data) == 1
    assert res.data[0].eps_actual == Decimal("1.67")
    assert res.data[0].eps_estimate_avg == Decimal("1.61")
    assert res.data[0].permissions.export is False

    # When analyst-estimates returns [], fetch_consensus falls back to /stable/earnings surprises
    rt = RouterTransport(
        {
            "/stable/analyst-estimates": _json_reply([]),
            "/stable/earnings": _json_reply(
                [
                    {
                        "symbol": "SMCI",
                        "date": "2026-05-05",
                        "epsActual": 0.65,
                        "epsEstimated": 0.60,
                        "revenueActual": 5300000000,
                        "revenueEstimated": 5200000000,
                    }
                ]
            ),
        }
    )
    fallback_res = ExternalEarningsService(provider=_fmp(rt)).get_consensus("SMCI", byok_api_key="byok-smci")
    assert fallback_res.status == "ok" and len(fallback_res.consensus) == 1
    assert fallback_res.consensus[0].eps_actual == Decimal("0.65")
    assert any("earnings surprises" in n for n in fallback_res.coverage.notes)


def test_validation_and_strict_timeout_guards() -> None:
    with pytest.raises(ValueError):
        FmpEarningsAdapter(timeout=0)
    with pytest.raises(ValueError):
        FmpEarningsAdapter(timeout=-1.5)

    t = FakeTransport(Reply(200, b"[]"))
    a = _fmp(t)
    assert a.fetch_consensus("", api_key="k").reason == "invalid_request"
    assert a.fetch_transcript("AAPL", fiscal_year=2026, api_key="k").reason == "invalid_request"
    assert a.fetch_transcript("AAPL", fiscal_year=2026, fiscal_quarter=5, api_key="k").reason == "invalid_request"
    assert a.fetch_consensus("AAPL", api_key="k", timeout_seconds=0).reason == "time_budget"
    assert not t.calls
