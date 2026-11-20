import json
from datetime import date
from decimal import Decimal

import pytest

from app.fin_data.adapters.sec_edgar import (STANDARD_LINES, SecEdgarAdapter, classify_duration, last_n_fiscal_years,
                                             last_n_quarters)
from .helpers import FIX, NOW, UA, FakeTransport, Reply, efts_policy, json_reply, open_limiter


def adapter(transport, ua=UA, policy=None, limiter=None):
    kw = {"policy": policy} if policy else {}
    return SecEdgarAdapter(ua, transport=transport, limiter=limiter or open_limiter(), now_fn=lambda: NOW, **kw)


def facts_entries(tag, unit="USD"):
    doc = json.loads((FIX / "sec_companyfacts.json").read_text())
    return doc["facts"]["us-gaap"][tag]["units"][unit]


def test_submissions_filters_forms_and_dates():
    t = FakeTransport(json_reply("sec_submissions.json"))
    r = adapter(t).submissions("320193", forms=["10-K", "10-Q"], start=date(2024, 1, 1))
    assert r.status == "ok" and [f.form for f in r.data] == ["10-K", "10-Q", "10-Q"]
    assert t.calls[0][0] == "https://data.sec.gov/submissions/CIK0000320193.json"
    assert t.calls[0][1]["User-Agent"] == UA
    f = r.data[0]
    assert f.primary_document_url == "https://www.sec.gov/Archives/edgar/data/320193/000032019324000100/exm-20240928.htm"
    assert f.index_url.endswith("0000320193-24-000100-index.htm") and f.period_of_report == date(2024, 9, 28)
    assert f.accepted_at.tzinfo is not None
    assert r.permissions.export is True and r.provenance.response_sha256 and r.cost.provider_calls == 1


def test_submissions_amendment_matching_and_older_window_partial():
    t = FakeTransport(json_reply("sec_submissions.json"))
    r = adapter(t).submissions(320193, forms=["10-K"], start=date(2015, 1, 1))
    assert [f.form for f in r.data] == ["10-K", "10-K/A"]
    assert r.status == "partial" and r.reason == "older_filings_not_loaded"
    r2 = adapter(FakeTransport(json_reply("sec_submissions.json"))).submissions(320193, forms=["S-1"])
    assert r2.status == "empty" and r2.reason == "no_matching_filings"
    r3 = adapter(FakeTransport(json_reply("sec_submissions.json"))).submissions(320193, limit=2)
    assert r3.status == "partial" and r3.reason == "truncated" and r3.coverage.truncated


def test_requires_user_agent_and_policy_and_ratelimit():
    r = adapter(FakeTransport(Reply()), ua=None).submissions(1)
    assert r.status == "failed" and r.reason == "sec_contact_unconfigured"
    from app.fin_data.policy import FinDataPolicy
    d = adapter(FakeTransport(Reply()), policy=FinDataPolicy()).submissions(1)
    assert d.status == "denied" and d.reason == "not_in_policy"
    from app.fin_data.ratelimit import InProcessRateLimiter, ProviderLimit
    lim = InProcessRateLimiter({"sec_edgar": ProviderLimit(1.0, 1.0)})
    t = FakeTransport(json_reply("sec_submissions.json"))
    a = adapter(t, limiter=lim)
    assert a.submissions(320193).status == "ok"
    rl = a.submissions(320193)
    assert rl.status == "rate_limited" and rl.retryable and rl.suggested_actions[0].retry_after_seconds > 0
    assert len(t.calls) == 1


def test_http_error_mapping():
    assert adapter(FakeTransport(Reply(404))).submissions(5).status == "empty"
    assert adapter(FakeTransport(Reply(429, b"", {"Retry-After": "7"}))).submissions(5).status == "rate_limited"
    assert adapter(FakeTransport(Reply(503))).submissions(5).retryable is True
    assert adapter(FakeTransport(Reply(200, b"not json"))).submissions(5).reason == "invalid_response"
    with pytest.raises(ValueError):
        adapter(FakeTransport(Reply())).submissions("abc")


def test_companyfacts_tag_switch_per_period_and_restatement():
    r = adapter(FakeTransport(json_reply("sec_companyfacts.json"))).statement_lines(
        320193, lines=["revenue"], period="quarter", n=5)
    by_end = {s.period_end: s for s in r.data}
    assert by_end[date(2018, 3, 31)].reported_tag == "SalesRevenueNet"
    assert by_end[date(2019, 6, 30)].reported_tag == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert len(r.data) == 5  # Q1'18, Q1-Q3'19, Q1'20
    q1_20 = by_end[date(2020, 3, 31)]
    assert q1_20.value == Decimal("1450") and q1_20.accession == "A-21-1"  # latest filing wins
    assert q1_20.fiscal_label == "2020-Q1" and q1_20.frame == "CY2020Q1"  # label from first filing
    assert r.status == "ok" and r.coverage.actual == {"first": "2018-03-31", "last": "2020-03-31"}


def test_companyfacts_multi_line_partial_and_units():
    r = adapter(FakeTransport(json_reply("sec_companyfacts.json"))).statement_lines(
        320193, lines=["net_income", "eps_diluted", "total_assets", "capex"], period="year", n=2)
    assert r.status == "partial" and r.reason == "missing_lines" and "capex" in r.message
    lines = {s.line: s for s in r.data}
    assert lines["eps_diluted"].unit == "USD/shares" and lines["eps_diluted"].currency == "USD"
    assert lines["eps_diluted"].value == Decimal("1.25")
    assert lines["total_assets"].statement == "balance" and lines["total_assets"].period_start is None
    assert len([s for s in r.data if s.line == "total_assets"]) == 1  # only FY-labelled instant for year
    assert adapter(FakeTransport(json_reply("sec_companyfacts.json"))).statement_lines(1, lines=["nope"]).reason == "unknown_line"
    assert set(STANDARD_LINES) >= {"revenue", "operating_income", "net_income", "eps_diluted", "total_assets",
                                   "total_liabilities", "equity", "operating_cash_flow", "capex", "cash"}


def test_last_n_quarters_distinct_and_q4_derived_only_on_request():
    e = facts_entries("RevenueFromContractWithCustomerExcludingAssessedTax")
    q = last_n_quarters(e, 2)
    assert [p.end for p in q] == [date(2020, 3, 31), date(2019, 9, 30)]
    allq = last_n_quarters(e, 10)
    assert len(allq) == 4 and all(not p.derived for p in allq)  # the 6-month YTD row is not a quarter
    derived = last_n_quarters(e, 10, derive_q4=True)
    q4 = next(p for p in derived if p.derived)
    assert q4.end == date(2019, 12, 31) and q4.value == Decimal("1400") and q4.start == date(2019, 10, 1)
    assert q4.label == "2019-Q4" and "FY minus" in q4.derivation
    y = last_n_fiscal_years(e, 3)
    assert [p.end for p in y] == [date(2019, 12, 31)] and y[0].value == Decimal("5000")


def test_derived_q4_surfaces_in_statement_lines_marked():
    r = adapter(FakeTransport(json_reply("sec_companyfacts.json"))).statement_lines(
        320193, lines=["revenue"], period="quarter", n=8, derive_q4=True)
    d = [s for s in r.data if s.derived]
    assert len(d) == 1 and d[0].period_end == date(2019, 12, 31) and d[0].value == Decimal("1400")


def test_classify_duration():
    assert classify_duration(date(2024, 1, 1), date(2024, 3, 31)) == "quarter"
    assert classify_duration(date(2024, 1, 1), date(2024, 6, 30)) is None
    assert classify_duration(date(2023, 12, 31), date(2024, 12, 28)) == "year"
    assert classify_duration(None, date(2024, 1, 1)) == "instant"


def test_frames_parse_sort_filter_and_validation():
    t = FakeTransport(json_reply("sec_frames.json"))
    r = adapter(t).frames("us-gaap", "Revenues", "USD", "CY2023Q4")
    assert t.calls[0][0] == "https://data.sec.gov/api/xbrl/frames/us-gaap/Revenues/USD/CY2023Q4.json"
    assert [x.entity_name for x in r.data] == ["Big Co", "Mid Co", "Small Co"]
    assert r.data[0].entity_id == "cik:0000002222" and r.data[0].value == Decimal("9000")
    r2 = adapter(FakeTransport(json_reply("sec_frames.json"))).frames("us-gaap", "Revenues", "USD", "CY2023Q4", ciks=[1111], limit=5)
    assert [x.cik for x in r2.data] == ["0000001111"]
    r3 = adapter(FakeTransport(json_reply("sec_frames.json"))).frames("us-gaap", "Revenues", "USD", "CY2023Q4", limit=2)
    assert r3.status == "partial" and r3.coverage.truncated
    assert adapter(FakeTransport(Reply())).frames("us-gaap", "../x", "USD", "CY2023Q4").reason == "invalid_request"


def test_efts_parse_and_policy_gate():
    d = adapter(FakeTransport(json_reply("sec_efts.json"))).fulltext_search("earnings")
    assert d.status == "denied" and d.reason == "not_in_policy"
    t = FakeTransport(json_reply("sec_efts.json"))
    r = adapter(t, policy=efts_policy()).fulltext_search("going concern", start=date(2024, 1, 1), end=date(2024, 12, 31),
                                                         forms=["8-K", "10-K"])
    url = t.calls[0][0]
    assert url.startswith("https://efts.sec.gov/LATEST/search-index?q=going%20concern") and "dateRange=custom" in url
    assert "startdt=2024-01-01" in url and "forms=8-K%2C10-K" in url
    assert r.status == "partial" and r.coverage.truncated and "undocumented" in " ".join(r.coverage.notes)
    h = r.data[0]
    assert h.entity_id == "cik:0000320193" and h.document == "d12345dex991.htm" and h.form == "8-K"
    assert h.url == "https://www.sec.gov/Archives/edgar/data/320193/000119312524000123/d12345dex991.htm"
    assert r.data[1].ciks == ["0000111111", "0000222222"]


def test_company_identity_reuses_sec_ticker_search():
    r = adapter(FakeTransport(json_reply("sec_tickers.json"))).company_identity("example")
    assert r.status == "ok" and {e.symbol for e in r.data} == {"EXM", "EXMT"}
    assert r.data[0].identifiers["cik"] == "0000320193"
    assert adapter(FakeTransport(json_reply("sec_tickers.json"))).company_identity("zzzz").status == "empty"
