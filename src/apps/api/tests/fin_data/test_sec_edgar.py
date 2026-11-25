import json
from datetime import date
from decimal import Decimal

import pytest

from app.fin_data.adapters.sec_edgar import (STANDARD_LINES, SecEdgarAdapter, classify_duration, derive_period_metrics,
                                             last_n_fiscal_years, last_n_quarters)
from .helpers import FIX, NOW, UA, ConceptTransport, FakeTransport, Reply, efts_policy, json_reply, open_limiter


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
    r = adapter(ConceptTransport()).statement_lines(
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
    r = adapter(ConceptTransport()).statement_lines(
        320193, lines=["net_income", "eps_diluted", "total_assets", "capex"], period="year", n=2)
    assert r.status == "partial" and r.reason == "missing_lines" and "capex" in r.message
    lines = {s.line: s for s in r.data}
    assert lines["eps_diluted"].unit == "USD/shares" and lines["eps_diluted"].currency == "USD"
    assert lines["eps_diluted"].value == Decimal("1.25")
    assert lines["total_assets"].statement == "balance" and lines["total_assets"].period_start is None
    assert len([s for s in r.data if s.line == "total_assets"]) == 1  # only FY-labelled instant for year
    assert adapter(ConceptTransport()).statement_lines(1, lines=["nope"]).reason == "unknown_line"
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
    r = adapter(ConceptTransport()).statement_lines(
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


# ---- companyconcept per tag (specific lines) and the companyfacts size guard
def test_specific_lines_fetch_all_candidate_companyconcept_tags_in_priority_order():
    t = ConceptTransport()
    r = adapter(t).statement_lines(320193, lines=["revenue"], period="quarter", n=2)
    assert r.status == "ok" and len(r.data) == 2
    # Every candidate tag is fetched (no early stop: a later tag may carry the newest periods).
    tags = [u.rsplit("/", 1)[-1].removesuffix(".json") for u in t.concept_urls]
    assert tags == [x for x in STANDARD_LINES["revenue"].tags["us-gaap"]]
    assert t.concept_urls[0] == "https://data.sec.gov/api/xbrl/companyconcept/CIK0000320193/us-gaap/Revenues.json"
    assert all("companyfacts" not in u for u, _ in t.calls) and all(h["User-Agent"] == UA for _, h in t.calls)
    assert r.cost.provider_calls == len(tags) and r.provenance.response_sha256 and r.coverage.rows == 2
    # Only files that exist (not the 404 for Revenues) are listed, in fetch order.
    assert r.provenance.endpoints and r.provenance.endpoints[0] == t.concept_urls[1]
    assert set(r.provenance.endpoints) <= set(t.concept_urls) and t.concept_urls[0] not in r.provenance.endpoints


def test_tag_switch_newest_periods_come_from_lower_priority_tag():
    t = ConceptTransport()
    top, low = STANDARD_LINES["revenue"].tags["us-gaap"][:2]
    row = lambda end, start, filed, fp: {"start": start, "end": end, "val": 100, "accn": "a-" + end, "fy": 2000,
                                         "fp": fp, "form": "10-Q", "filed": filed}
    old = [row("2018-06-30", "2018-04-01", "2018-08-01", "Q2"), row("2018-09-30", "2018-07-01", "2018-11-01", "Q3")]
    new = [row("2024-06-30", "2024-04-01", "2024-08-01", "Q2"), row("2024-09-30", "2024-07-01", "2024-11-01", "Q3")]
    t.doc["facts"]["us-gaap"] = {top: {"units": {"USD": old}}, low: {"units": {"USD": new}}}
    r = adapter(t).statement_lines(320193, lines=["revenue"], period="quarter", n=2)
    assert r.status == "ok"
    assert sorted(s.period_end.isoformat() for s in r.data) == ["2024-06-30", "2024-09-30"]
    assert {s.reported_tag for s in r.data} == {low}


def test_concept_path_goes_through_the_pacing_limiter_per_request():
    from app.fin_data.ratelimit import InProcessRateLimiter, ProviderLimit
    lim = InProcessRateLimiter({"sec_edgar": ProviderLimit(1.0, 1.0)}, now=lambda: NOW)
    t = ConceptTransport()
    r = adapter(t, limiter=lim).statement_lines(320193, lines=["revenue"], period="quarter", n=5)
    assert r.status == "rate_limited" and r.retryable and len(t.concept_urls) == 1


def test_concept_path_all_tags_missing_is_empty_no_matching_lines_for_ifrs_fallback():
    t = ConceptTransport("sec_companyfacts.json")
    r = adapter(t).statement_lines(320193, lines=["capex"], period="year", n=2)
    assert r.status == "empty" and r.reason == "no_matching_lines"
    assert len(t.concept_urls) == 3  # all three us-gaap capex tags tried


def test_concept_failure_other_than_404_is_returned_not_swallowed():
    t = FakeTransport(Reply(503))
    r = adapter(t).statement_lines(320193, lines=["revenue"], period="quarter", n=2)
    assert r.status == "failed" and r.reason == "source_unavailable" and r.retryable and len(t.calls) == 1


def test_no_lines_falls_back_to_companyfacts_and_guards_parse_size(monkeypatch):
    import app.fin_data.adapters.sec_edgar as mod
    ok = adapter(FakeTransport(json_reply("sec_companyfacts.json"))).statement_lines(320193, period="year", n=1)
    assert ok.status in ("ok", "partial") and ok.data
    monkeypatch.setattr(mod, "MAX_COMPANYFACTS_BYTES", 1000)
    big = adapter(FakeTransport(Reply(200, b"{" + b" " * 2000 + b"}"))).statement_lines(320193, period="year", n=1)
    assert big.status == "failed" and big.reason == "source_too_large"
    assert "lines" in big.message and big.suggested_actions[0].action == "company_financials"


def test_companyfacts_size_guard_applies_to_the_streaming_path_and_stops_the_download(monkeypatch):
    import app.fin_data.adapters.sec_edgar as mod
    monkeypatch.setattr(mod, "MAX_COMPANYFACTS_BYTES", 5000)
    written = []

    def stream(method, url, headers, body, timeout, sink):
        for _ in range(100):
            sink.write(b" " * 1000)  # raises once the cap is crossed
            written.append(1)
        return Reply(200, b"")
    a = SecEdgarAdapter(UA, transport=FakeTransport(Reply()), stream_transport=stream, limiter=open_limiter(),
                        now_fn=lambda: NOW)
    r = a.statement_lines(320193, period="year", n=1)
    assert r.status == "failed" and r.reason == "source_too_large" and len(written) == 5


def test_expanded_standard_lines_coverage_and_shares_unit():
    expected_expanded = {
        "revenue", "cost_of_revenue", "gross_profit", "rd_expense", "sga_expense",
        "selling_expense", "admin_expense", "total_operating_costs", "operating_income",
        "interest_expense", "finance_expense", "profit_before_tax", "income_tax",
        "net_income", "ebitda", "eps_basic", "eps_diluted", "shares_basic", "shares_diluted",
        "cash", "short_term_investments", "accounts_receivable", "inventory", "current_assets",
        "ppe_net", "goodwill", "intangible_assets", "total_assets", "accounts_payable",
        "short_term_debt", "current_liabilities", "long_term_debt", "total_liabilities",
        "retained_earnings", "equity", "non_controlling_interests", "depreciation_amortization",
        "operating_cash_flow", "capex", "investing_cash_flow", "financing_cash_flow",
        "dividends_paid", "share_repurchases", "net_increase_in_cash",
    }
    assert len(STANDARD_LINES) >= 35
    assert expected_expanded <= set(STANDARD_LINES)
    assert STANDARD_LINES["revenue"].tags["us-gaap"][0] == "Revenues"

    t = ConceptTransport()
    q_row = lambda val: [{"start": "2024-01-01", "end": "2024-03-31", "val": val, "accn": "A-24-1",
                          "fy": 2024, "fp": "Q1", "form": "10-Q", "filed": "2024-05-01"}]
    b_row = lambda val: [{"end": "2024-03-31", "val": val, "accn": "A-24-1",
                          "fy": 2024, "fp": "Q1", "form": "10-Q", "filed": "2024-05-01"}]
    t.doc["facts"]["us-gaap"].update({
        "CostOfGoodsAndServicesSold": {"units": {"USD": q_row(600)}},
        "GrossProfit": {"units": {"USD": q_row(400)}},
        "ResearchAndDevelopmentExpense": {"units": {"USD": q_row(120)}},
        "SellingGeneralAndAdministrativeExpense": {"units": {"USD": q_row(80)}},
        "InterestExpense": {"units": {"USD": q_row(15)}},
        "IncomeTaxExpenseBenefit": {"units": {"USD": q_row(35)}},
        "EarningsBeforeInterestTaxesDepreciationAndAmortization": {"units": {"USD": q_row(240)}},
        "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": q_row(1000)}},
        "AccountsReceivableNetCurrent": {"units": {"USD": b_row(300)}},
        "InventoryNet": {"units": {"USD": b_row(200)}},
        "AssetsCurrent": {"units": {"USD": b_row(1500)}},
        "LiabilitiesCurrent": {"units": {"USD": b_row(700)}},
        "ShortTermBorrowings": {"units": {"USD": b_row(100)}},
        "LongTermDebtNoncurrent": {"units": {"USD": b_row(500)}},
        "DepreciationDepletionAndAmortization": {"units": {"USD": q_row(40)}},
        "NetCashProvidedByUsedInInvestingActivities": {"units": {"USD": q_row(-90)}},
        "NetCashProvidedByUsedInFinancingActivities": {"units": {"USD": q_row(-60)}},
        "PaymentsOfDividendsCommonStock": {"units": {"USD": q_row(25)}},
        "PaymentsForRepurchaseOfCommonStock": {"units": {"USD": q_row(35)}},
    })
    wanted = [
        "cost_of_revenue", "gross_profit", "rd_expense", "sga_expense", "interest_expense",
        "income_tax", "ebitda", "shares_diluted", "accounts_receivable", "inventory",
        "current_assets", "current_liabilities", "short_term_debt", "long_term_debt",
        "depreciation_amortization", "investing_cash_flow", "financing_cash_flow",
        "dividends_paid", "share_repurchases",
    ]
    r = adapter(t).statement_lines(320193, lines=wanted, period="quarter", n=1)
    assert r.status == "ok"
    by_line = {s.line: s for s in r.data}
    assert set(by_line) == set(wanted)
    assert by_line["shares_diluted"].unit == "shares" and by_line["shares_diluted"].currency is None
    assert by_line["shares_diluted"].value == Decimal("1000")
    assert by_line["ebitda"].value == Decimal("240")
    assert by_line["investing_cash_flow"].value == Decimal("-90")
    assert by_line["long_term_debt"].period_start is None


def test_derive_period_metrics_sec_quarters_ttm_growth_margins_and_fcf():
    from app.fin_data.models import StatementLine

    def line(stmt, name, start, end, val, *, tag="Tag", unit="USD", cur="USD", label=None):
        return StatementLine(
            entity_id="cik:0000320193", statement=stmt, line=name, reported_tag=tag, taxonomy="us-gaap",
            period_start=start, period_end=end, fiscal_label=label, value=Decimal(val), unit=unit, currency=cur,
            form="10-Q", filed_at=end, accession="A-1",
        )

    quarters = [
        (date(2024, 1, 1), date(2024, 3, 31), "100", "60", "25", "20", "30", "10"),
        (date(2024, 4, 1), date(2024, 6, 30), "120", "70", "30", "24", "35", "-12"),
        (date(2024, 7, 1), date(2024, 9, 30), "140", "80", "35", "28", "40", "15"),
        (date(2024, 10, 1), date(2024, 12, 31), "160", "90", "40", "32", "45", "15"),
        (date(2025, 1, 1), date(2025, 3, 31), "150", "85", "38", "30", "42", "12"),
    ]
    rows = []
    for s, e, rev, cost, op, ni, ocf, capex in quarters:
        rows.extend([
            line("income", "revenue", s, e, rev, tag="Revenues"),
            line("income", "cost_of_revenue", s, e, cost, tag="CostOfRevenue"),
            line("income", "operating_income", s, e, op, tag="OperatingIncomeLoss"),
            line("income", "net_income", s, e, ni, tag="NetIncomeLoss"),
            line("cashflow", "operating_cash_flow", s, e, ocf, tag="NetCashProvidedByUsedInOperatingActivities"),
            line("cashflow", "capex", s, e, capex, tag="PaymentsToAcquirePropertyPlantAndEquipment"),
            line("balance", "cash", None, e, "500", tag="CashAndCashEquivalentsAtCarryingValue"),
        ])

    derived = derive_period_metrics(rows)
    assert all(d.derived is True and d.derivation for d in derived)
    by_key = {(d.line, d.period_end): d for d in derived}

    # Margins & derived gross profit
    assert by_key[("gross_profit", date(2024, 3, 31))].value == Decimal("40")
    assert by_key[("gross_margin", date(2024, 3, 31))].value == Decimal("40.00")
    assert by_key[("gross_margin", date(2024, 3, 31))].unit == "percent"
    assert by_key[("gross_margin", date(2024, 3, 31))].currency is None
    assert by_key[("operating_margin", date(2024, 3, 31))].value == Decimal("25.00")
    assert by_key[("net_margin", date(2024, 3, 31))].value == Decimal("20.00")

    # FCF uses operating_cash_flow - abs(capex) regardless of capex sign
    assert by_key[("free_cash_flow", date(2024, 3, 31))].value == Decimal("20")
    assert by_key[("free_cash_flow", date(2024, 6, 30))].value == Decimal("23")

    # TTM sums (2024Q4 and 2025Q1), never computed for balance-sheet instant lines
    ttm_24 = by_key[("revenue_ttm", date(2024, 12, 31))]
    assert ttm_24.value == Decimal("520") and ttm_24.period_start == date(2024, 1, 1) and ttm_24.basis == "ttm"
    ttm_25 = by_key[("revenue_ttm", date(2025, 3, 31))]
    assert ttm_25.value == Decimal("570") and ttm_25.period_start == date(2024, 4, 1)
    assert ("cash_ttm", date(2024, 12, 31)) not in by_key

    # YoY and QoQ growth
    assert by_key[("revenue_yoy_growth", date(2025, 3, 31))].value == Decimal("50.00")
    assert by_key[("revenue_qoq_growth", date(2024, 6, 30))].value == Decimal("20.00")
    assert by_key[("cash_yoy_growth", date(2025, 3, 31))].value == Decimal("0.00")

    # Idempotent / non-cascading over derived outputs
    assert derive_period_metrics([*rows, *derived]) == derived
