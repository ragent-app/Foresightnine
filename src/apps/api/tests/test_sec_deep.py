from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import json
from typing import Any

import pytest

from app.fin_data.adapters.sec_deep import (
    DualPeriodEarningsRelease,
    FilingRedlineReport,
    FilingSectionsBundle,
    FilingTableCatalog,
    FilingTableData,
    SecDeepAdapter,
    SectionRedlineDiff,
    cik_scoped_efts_search,
    fetch_filing_table,
    parse_filing_summary_xml,
    parse_r_html_table,
    sec_earnings_release,
    sec_filing_sections,
    sec_filing_tables,
    sec_redline_diff,
    split_filing_sections,
    strip_html_preserving_tables,
)
from app.fin_data.adapters.sec_edgar import SecEdgarAdapter
from app.fin_data.models import Filing, FullTextHit
from app.fin_data.policy import FinDataPolicy, PolicyRule, default_policy
from app.fin_data.ratelimit import InProcessRateLimiter, ProviderLimit
from tests.fin_data.helpers import NOW, Reply, UA, efts_policy, open_limiter


class RoutingTransport:
    """Offline URL router simulating SEC EDGAR HTTP endpoints."""

    def __init__(self, routes: dict[str, Reply | Exception] | None = None) -> None:
        self.routes: dict[str, Reply | Exception] = dict(routes or {})
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def add(self, url_or_substring: str, reply: Reply | Exception) -> None:
        self.routes[url_or_substring] = reply

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes, timeout: float) -> Reply:
        self.calls.append((method, url, dict(headers)))
        if url in self.routes:
            res = self.routes[url]
            if isinstance(res, Exception):
                raise res
            return res
        for key, res in self.routes.items():
            if key in url:
                if isinstance(res, Exception):
                    raise res
                return res
        return Reply(404, b"Not Found", {"content-type": "text/html"})


def make_adapter(transport: RoutingTransport, *, policy: FinDataPolicy | None = None, limiter=None) -> SecEdgarAdapter:
    return SecEdgarAdapter(
        UA,
        transport=transport,
        limiter=limiter or open_limiter(),
        policy=policy or default_policy(),
        now_fn=lambda: NOW,
    )


def html_reply(text: str | bytes, status: int = 200, content_type: str = "text/html; charset=utf-8") -> Reply:
    body = text.encode("utf-8") if isinstance(text, str) else text
    return Reply(status, body, {"content-type": content_type})


def xml_reply(text: str, status: int = 200) -> Reply:
    return Reply(status, text.encode("utf-8"), {"content-type": "application/xml; charset=utf-8"})


def raw_json_reply(payload: Any, status: int = 200) -> Reply:
    return Reply(status, json.dumps(payload).encode("utf-8"), {"content-type": "application/json"})


# --------------------------------------------------------------------------- Realistic SEC HTML / XML fixtures
EX99_1_CURRENT_HTML = """
<html>
<head>
  <style>.hidden { display: none; }</style>
  <script>console.log("ignore me");</script>
</head>
<body>
  <div style="display:none">Hidden XBRL tracking metadata</div>
  <h1>ACME CLOUD CORP. REPORTS THIRD QUARTER FISCAL 2025 RESULTS</h1>
  <p>SAN FRANCISCO, Nov. 1, 2025 — Acme Cloud Corp. today announced financial results for its third quarter ended September 30, 2025.</p>
  <ul>
    <li>Total revenue grew 24% year-over-year to $1,250 million.</li>
    <li>Non-GAAP operating income reached $310 million, or 24.8% operating margin.</li>
  </ul>
  <p>"We delivered accelerating enterprise cloud adoption while expanding free cash flow margins," said Jane Doe, CEO.</p>

  <h2>Financial Highlights</h2>
  <table>
    <tr>
      <th>Metric</th>
      <th colspan="2">Q3 2025</th>
      <th></th>
      <th colspan="2">Q3 2024</th>
    </tr>
    <tr>
      <td>Revenue ($ in millions)</td>
      <td>$</td><td>1,250</td>
      <td></td>
      <td>$</td><td>1,008</td>
    </tr>
    <tr>
      <td>GAAP Net Income (Loss)</td>
      <td>$</td><td>(42</td><td>)</td>
      <td>$</td><td>(65</td><td>)</td>
    </tr>
  </table>

  <h2>Business Outlook</h2>
  <p>For the fourth quarter of fiscal 2025, Acme expects revenue to be in the range of $1,310 million to $1,340 million, Non-GAAP gross margin of approximately 74.5%, and Non-GAAP diluted EPS between $0.82 and $0.86.</p>
  <p>For full year fiscal 2025, we expect capital expenditures of approximately $180 million and free cash flow margin of 22%.</p>

  <h2>Reconciliation of GAAP to Non-GAAP Financial Measures</h2>
  <p>The company provides Non-GAAP operating income and free cash flow to supplement GAAP results.</p>
  <table>
    <tr>
      <th>(in millions)</th>
      <th colspan="2">Three Months Ended Sep 30, 2025</th>
      <th colspan="2">Three Months Ended Sep 30, 2024</th>
    </tr>
    <tr>
      <td>GAAP operating income</td>
      <td>$</td><td>185</td>
      <td>$</td><td>110</td>
    </tr>
    <tr>
      <td>Stock-based compensation expense</td>
      <td></td><td>110</td>
      <td></td><td>95</td>
    </tr>
    <tr>
      <td>Amortization of acquired intangibles</td>
      <td></td><td>15</td>
      <td></td><td>15</td>
    </tr>
    <tr>
      <td>Non-GAAP operating income</td>
      <td>$</td><td>310</td>
      <td>$</td><td>220</td>
    </tr>
  </table>

  <h2>Forward-Looking Statements</h2>
  <p>This press release contains forward-looking statements within the meaning of the Private Securities Litigation Reform Act of 1995. Actual results may differ materially due to risks and uncertainties, and Acme undertakes no obligation to update these statements.</p>
</body>
</html>
"""

EX99_2_CURRENT_HTML = """
<html><body>
  <h2>CFO Commentary - Q3 Fiscal 2025</h2>
  <p>Remaining performance obligations rose 28% year-over-year to $4.8 billion.</p>
</body></html>
"""

EX99_1_PRIOR_HTML = """
<html><body>
  <h1>ACME CLOUD CORP. REPORTS SECOND QUARTER FISCAL 2025 RESULTS</h1>
  <p>SAN FRANCISCO, Aug. 1, 2025 — Acme Cloud Corp. announced results for its second quarter ended June 30, 2025.</p>
  <ul>
    <li>Total revenue was $1,180 million, up 21% year-over-year.</li>
  </ul>
  <h2>Business Outlook</h2>
  <p>For the third quarter of fiscal 2025, Acme expects revenue between $1,220 million and $1,245 million and Non-GAAP diluted EPS of $0.76 to $0.80.</p>
  <h2>Reconciliation of GAAP to Non-GAAP Financial Measures</h2>
  <table>
    <tr><th>(in millions)</th><th>Q2 2025</th></tr>
    <tr><td>GAAP net income</td><td>$140</td></tr>
    <tr><td>Stock-based compensation</td><td>$105</td></tr>
    <tr><td>Non-GAAP net income</td><td>$245</td></tr>
  </table>
</body></html>
"""

FILING_SUMMARY_XML = """<?xml version="1.0" encoding="utf-8"?>
<FilingSummary>
  <Version>3.24.3</Version>
  <MyReports>
    <Report instance="acme-20250930.htm">
      <HtmlFileName>R1.htm</HtmlFileName>
      <LongName>0000001 - Document - Cover Page</LongName>
      <ReportType>Sheet</ReportType>
      <Role>http://acme.com/role/Cover</Role>
      <ShortName>Cover Page</ShortName>
      <MenuCategory>Cover</MenuCategory>
      <Position>1</Position>
    </Report>
    <Report instance="acme-20250930.htm">
      <HtmlFileName>R2.htm</HtmlFileName>
      <LongName>1000001 - Statement - CONSOLIDATED STATEMENTS OF OPERATIONS</LongName>
      <ReportType>Sheet</ReportType>
      <Role>http://acme.com/role/IncomeStatement</Role>
      <ShortName>Consolidated Statements of Operations</ShortName>
      <MenuCategory>Statements</MenuCategory>
      <Position>2</Position>
    </Report>
    <Report instance="acme-20250930.htm">
      <HtmlFileName>R10.htm</HtmlFileName>
      <LongName>2000010 - Disclosure - Segment Information</LongName>
      <ReportType>Notes</ReportType>
      <Role>http://acme.com/role/SegmentInformation</Role>
      <ShortName>Segment Information</ShortName>
      <MenuCategory>Notes</MenuCategory>
      <Position>10</Position>
    </Report>
    <Report instance="acme-20250930.htm">
      <HtmlFileName>R32.htm</HtmlFileName>
      <LongName>3000032 - Disclosure - Segment Revenue Disaggregation (Tables)</LongName>
      <ReportType>Sheet</ReportType>
      <Role>http://acme.com/role/SegmentTables</Role>
      <ShortName>Segment Revenue Disaggregation (Tables)</ShortName>
      <MenuCategory>Tables</MenuCategory>
      <Position>32</Position>
    </Report>
    <Report>
      <LongName>All Reports</LongName>
      <ReportType>Book</ReportType>
    </Report>
  </MyReports>
</FilingSummary>
"""

R2_STATEMENT_HTML = """
<html>
<head><script type="text/javascript" src="Show.js"></script></head>
<body>
<span style="display:none;">v3.24.3</span>
<table class="report" border="0" cellspacing="2">
  <tr>
    <th class="tl" rowspan="2"><strong>CONSOLIDATED STATEMENTS OF OPERATIONS - USD ($)<br/>shares in Thousands, $ in Millions</strong></th>
    <th class="th" colspan="2">3 Months Ended</th>
    <th class="th" colspan="2">9 Months Ended</th>
  </tr>
  <tr>
    <th class="th">Sep. 30, 2025</th>
    <th class="th">Sep. 30, 2024</th>
    <th class="th">Sep. 30, 2025</th>
    <th class="th">Sep. 30, 2024</th>
  </tr>
  <tr class="re">
    <td class="pl"><a onclick="Show.showAR( this, 'defref_us-gaap_Revenues', window );">Total revenues</a></td>
    <td class="nump">$ 1,250<sup>(1)</sup></td>
    <td class="nump">$ 1,008</td>
    <td class="nump">$ 3,520</td>
    <td class="nump">$ 2,890</td>
  </tr>
  <tr class="ro">
    <td class="pl"><a onclick="Show.showAR( this, 'defref_us-gaap_CostOfRevenue', window );">Cost of revenue</a></td>
    <td class="nump">325</td>
    <td class="nump">272</td>
    <td class="nump">910</td>
    <td class="nump">780</td>
  </tr>
  <tr class="reu">
    <td class="pl"><a onclick="Show.showAR( this, 'defref_us-gaap_GrossProfit', window );">Gross margin</a></td>
    <td class="nump">925</td>
    <td class="nump">736</td>
    <td class="nump">2,610</td>
    <td class="nump">2,110</td>
  </tr>
  <tr class="ro">
    <td class="pl"><a onclick="Show.showAR( this, 'defref_acme_RestructuringAndOther', window );">Restructuring charge</a></td>
    <td class="nump">(18)</td>
    <td class="nump">&#8212;</td>
    <td class="nump">$ 12</td>
    <td class="nump">(5)</td>
  </tr>
  <tr class="rn">
    <td class="pl"><a onclick="Show.showAR( this, 'defref_us-gaap_EarningsPerShareDiluted', window );">Diluted (in dollars per share)</a></td>
    <td class="nump">$ 0.85</td>
    <td class="nump">$ 0.61</td>
    <td class="nump">$ 2.30</td>
    <td class="nump">$ 1.72</td>
  </tr>
  <tr class="rn">
    <td class="pl"><a onclick="Show.showAR( this, 'defref_us-gaap_WeightedAverageNumberOfDilutedSharesOutstanding', window );">Diluted (in shares)</a></td>
    <td class="nump">145,200</td>
    <td class="nump">142,000</td>
    <td class="nump">144,800</td>
    <td class="nump">141,500</td>
  </tr>
</table>
<div style="display:none;">
  <table class="authRefData" id="authRefData" style="display:none;">
    <tr><td>Internal XBRL definition should never leak into parsed rows</td><td>999999</td></tr>
  </table>
</div>
</body>
</html>
"""

R10_NOTE_NESTED_HTML = """
<html><body>
<table class="report">
  <tr><th class="tl">Segment Information (USD $) - $ in Millions</th></tr>
  <tr><td class="text">
    <p>Note 10. The Company operates in two reportable segments: Cloud Platform and Enterprise Services.</p>
    <table class="sub">
      <tr>
        <th>Segment</th>
        <th>Sep. 30, 2025</th>
        <th>Sep. 30, 2024</th>
      </tr>
      <tr>
        <td>Cloud Platform</td>
        <td>$ 980</td>
        <td>$ 760</td>
      </tr>
      <tr>
        <td>Enterprise Services</td>
        <td>$ 270</td>
        <td>$ 248</td>
      </tr>
      <tr>
        <td>Total revenue</td>
        <td>$ 1,250</td>
        <td>$ 1,008</td>
      </tr>
    </table>
  </td></tr>
</table>
</body></html>
"""

SAMPLE_10K_HTML = """
<html><body>
<ix:header><ix:hidden><ix:nonNumeric name="dei:EntityRegistrantName">Acme Cloud Corp.</ix:nonNumeric></ix:hidden></ix:header>
<div>TABLE OF CONTENTS</div>
<div>Item 1. Business .......... 3</div>
<div>Item 1A. Risk Factors .......... 8</div>
<div>Item 2. Properties .......... 22</div>
<div>Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations .......... 25</div>

<p>PART I</p>
<h2>Item 1. Business</h2>
<p>Acme Cloud Corp. provides enterprise observability and cloud database infrastructure to global customers across North America, Europe, and Asia Pacific. We operate a subscription-first business model with multi-year enterprise contracts.</p>

<h2>Item 1A. Risk Factors</h2>
<p>Our business involves significant risks. Investors should carefully consider the following risk factors before making an investment decision.</p>
<p>As of September 30, 2025, we employed 4,200 full-time employees across 14 offices globally.</p>
<p>New U.S. export controls on advanced cloud compute infrastructure could restrict our ability to serve certain international customers.</p>

<h2>Item 2. Properties</h2>
<p>Our corporate headquarters is located in San Francisco, California, where we lease approximately 180,000 square feet of office space.</p>

<p>PART II</p>
<h2>Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations</h2>
<p>Overview: Revenue increased 24% year-over-year in fiscal 2025 driven by net dollar retention of 118%.</p>

<h3>Liquidity and Capital Resources</h3>
<p>As of September 30, 2025, we had cash, cash equivalents, and marketable securities of $1,850 million and no outstanding borrowings under our $500 million revolving credit facility.</p>
<p>Operating cash flow for fiscal 2025 was $410 million, up from $305 million in fiscal 2024, reflecting higher operating profitability.</p>

<h3>Critical Accounting Estimates</h3>
<p>Revenue recognition for multi-element arrangements requires judgment in determining standalone selling prices (SSP) for software licenses and cloud subscriptions.</p>

<h2>Item 8. Financial Statements and Supplementary Data</h2>
<p>See Consolidated Financial Statements beginning on page F-1.</p>
</body></html>
"""

SAMPLE_10Q_HTML = """
<html><body>
<p>PART I - FINANCIAL INFORMATION</p>
<h2>Item 1. Financial Statements</h2>
<p>Condensed Consolidated Statements of Operations (Unaudited) for the three months ended June 30, 2025.</p>

<h2>Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations</h2>
<p>Revenue grew 21% year-over-year in the second quarter of fiscal 2025.</p>
<h3>Liquidity and Capital Resources</h3>
<p>As of June 30, 2025, cash and marketable securities totaled $1,620 million.</p>
<h3>Critical Accounting Estimates</h3>
<p>Our critical accounting estimates remain consistent with those described in our Annual Report on Form 10-K.</p>

<p>PART II - OTHER INFORMATION</p>
<h2>Item 1A. Risk Factors</h2>
<p>There have been no material changes to our risk factors except as updated below regarding regional data-residency mandates.</p>
<h2>Item 2. Unregistered Sales of Equity Securities and Use of Proceeds</h2>
<p>During the quarter ended June 30, 2025, we repurchased 1.2 million shares of common stock for $95 million.</p>
</body></html>
"""


# --------------------------------------------------------------------------- 1. Tests for sec_earnings_release
def test_sec_earnings_release_dual_period_raw_submissions_and_hints():
    cik = "0001234567"
    cur_acc = "0001234567-25-000105"
    amd_acc = "0001234567-25-000101"  # same period earlier 8-K superseded by 8-K/A
    other_acc = "0001234567-25-000090"  # Item 5.02, NOT Item 2.02
    pri_acc = "0001234567-25-000060"
    yoy_acc = "0001234567-24-000100"

    raw_subs = {
        "cik": cik,
        "filings": {
            "recent": {
                "accessionNumber": [cur_acc, amd_acc, other_acc, pri_acc, yoy_acc],
                "filingDate": ["2025-11-01", "2025-10-29", "2025-09-15", "2025-08-01", "2024-11-02"],
                "reportDate": ["2025-09-30", "2025-09-30", "2025-09-14", "2025-06-30", "2024-09-30"],
                "form": ["8-K/A", "8-K", "8-K", "8-K", "8-K"],
                "items": [["2.02", "9.01"], ["2.02", "9.01"], ["5.02"], ["2.02", "9.01"], ["2.02", "9.01"]],
                "primaryDocument": ["f8k_q3a.htm", "f8k_q3.htm", "f8k_502.htm", "f8k_q2.htm", "f8k_yoy.htm"],
            }
        },
    }

    t = RoutingTransport({
        f"/1234567/{cur_acc.replace('-', '')}/index.json": raw_json_reply({
            "directory": {
                "item": [
                    {"name": "f8k_q3a.htm", "type": "text.gif"},
                    {"name": "acme-ex99_1.htm", "type": "text.gif"},
                    {"name": "acme-ex99_2.htm", "type": "text.gif"},
                ]
            }
        }),
        f"/1234567/{cur_acc.replace('-', '')}/acme-ex99_1.htm": html_reply(EX99_1_CURRENT_HTML),
        f"/1234567/{cur_acc.replace('-', '')}/acme-ex99_2.htm": html_reply(EX99_2_CURRENT_HTML),
        f"/1234567/{pri_acc.replace('-', '')}/index.json": raw_json_reply({
            "directory": {
                "item": [
                    {"name": "f8k_q2.htm", "type": "text.gif"},
                    {"name": "d999ex991.htm", "type": "text.gif"},
                ]
            }
        }),
        f"/1234567/{pri_acc.replace('-', '')}/d999ex991.htm": html_reply(EX99_1_PRIOR_HTML),
    })

    res = sec_earnings_release(make_adapter(t), cik, submissions=raw_subs)
    assert res.status == "ok" and res.reason == "ok"
    assert len(res.data) == 1
    payload = res.data[0]
    assert isinstance(payload, DualPeriodEarningsRelease)
    assert payload.cik == cik
    assert payload.days_between_filings == (date(2025, 11, 1) - date(2025, 8, 1)).days

    # Current period paired with 8-K/A (not double-paired with the Oct 29 original 8-K)
    assert payload.current is not None
    assert payload.current.accession == cur_acc
    assert payload.current.ex99_1 is not None
    assert payload.current.ex99_2 is not None
    assert "Hidden XBRL" not in payload.current.ex99_1.text
    assert "ignore me" not in payload.current.ex99_1.text
    assert "| Revenue ($ in millions) | $1,250 | $1,008 |" in payload.current.ex99_1.text
    assert "| GAAP Net Income (Loss) | $(42) | $(65) |" in payload.current.ex99_1.text

    # Structured hints: management summary, Non-GAAP, and guidance (Safe Harbor excluded!)
    assert "accelerating enterprise cloud adoption" in payload.current.management_summary
    assert any("revenue grew 24%" in h.lower() for h in payload.current.management_highlights)
    assert len(payload.current.nongaap_reconciliations) >= 1
    assert any(ng.table is not None and ng.table.is_nongaap for ng in payload.current.nongaap_reconciliations)
    assert len(payload.current.guidance_passages) >= 1
    guidance_text = " ".join(g.text for g in payload.current.guidance_passages)
    assert "$1,310 million to $1,340 million" in guidance_text
    assert "Private Securities Litigation Reform Act" not in guidance_text
    assert payload.current.guidance_passages[0].horizon in ("next_quarter", "next_quarter_and_full_year")
    assert "revenue" in payload.current.guidance_passages[0].metrics_mentioned

    # Prior period paired with Q2 2025 (Aug 1), skipping Item 5.02 (Sep 15)
    assert payload.prior is not None
    assert payload.prior.accession == pri_acc
    assert payload.prior.ex99_1 is not None
    assert payload.prior.ex99_2 is None
    assert any("$1,220 million and $1,245 million" in g.text for g in payload.prior.guidance_passages)
    assert not any(other_acc.replace("-", "") in u for _, u, _ in t.calls)
    assert res.provenance is not None and len(res.provenance.endpoints) == 5
    assert res.cost.provider_calls == 5


def test_sec_earnings_release_filing_objects_index_htm_fallback_and_yoy_mode():
    cik = "320193"
    cur_acc = "0000320193-25-000200"
    non202_acc = "0000320193-25-000150"
    q2_acc = "0000320193-25-000100"
    yoy_acc = "0000320193-24-000200"

    filings = [
        Filing(
            entity_id="cik:0000320193",
            form="8-K",
            filed_at=date(2025, 11, 1),
            period_of_report=date(2025, 9, 30),
            accession=cur_acc,
            primary_document_url=f"https://www.sec.gov/Archives/edgar/data/320193/{cur_acc.replace('-', '')}/primary.htm",
        ),
        Filing(
            entity_id="cik:0000320193",
            form="8-K",
            filed_at=date(2025, 9, 20),
            accession=non202_acc,
            primary_document_url=f"https://www.sec.gov/Archives/edgar/data/320193/{non202_acc.replace('-', '')}/primary.htm",
        ),
        Filing(
            entity_id="cik:0000320193",
            form="8-K",
            filed_at=date(2025, 8, 1),
            period_of_report=date(2025, 6, 30),
            accession=q2_acc,
            primary_document_url=f"https://www.sec.gov/Archives/edgar/data/320193/{q2_acc.replace('-', '')}/primary.htm",
        ),
        Filing(
            entity_id="cik:0000320193",
            form="8-K",
            filed_at=date(2024, 11, 1),
            period_of_report=date(2024, 9, 30),
            accession=yoy_acc,
            primary_document_url=f"https://www.sec.gov/Archives/edgar/data/320193/{yoy_acc.replace('-', '')}/primary.htm",
        ),
    ]

    # Opaque exhibit filename 'opaque_release.htm' resolved via {accession}-index.htm!
    index_htm = """
    <html><body>
      <div>Items: Item 2.02: Results of Operations and Financial Condition</div>
      <table>
        <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
        <tr><td>1</td><td>8-K</td><td>primary.htm</td><td>8-K</td><td>10000</td></tr>
        <tr><td>2</td><td>EARNINGS RELEASE</td><td>opaque_release.htm</td><td>EX-99.1</td><td>50000</td></tr>
      </table>
    </body></html>
    """
    t = RoutingTransport({
        f"/320193/{cur_acc.replace('-', '')}/index.json": raw_json_reply({
            "directory": {"item": [{"name": f"{cur_acc}-index.htm"}, {"name": "opaque_release.htm"}]}
        }),
        f"/320193/{cur_acc.replace('-', '')}/{cur_acc}-index.htm": html_reply(index_htm),
        f"/320193/{cur_acc.replace('-', '')}/primary.htm": html_reply("<p>Item 2.02 Results of Operations and Financial Condition</p>"),
        f"/320193/{cur_acc.replace('-', '')}/opaque_release.htm": html_reply(EX99_1_CURRENT_HTML),
        f"/320193/{non202_acc.replace('-', '')}/index.json": raw_json_reply({"directory": {"item": [{"name": "primary.htm"}]}}),
        f"/320193/{non202_acc.replace('-', '')}/primary.htm": html_reply("<p>Item 7.01 Regulation FD Disclosure</p>"),
        f"/320193/{q2_acc.replace('-', '')}/index.json": raw_json_reply({"directory": {"item": [{"name": "ex99-1.htm"}]}}),
        f"/320193/{q2_acc.replace('-', '')}/ex99-1.htm": html_reply(EX99_1_PRIOR_HTML),
        f"/320193/{yoy_acc.replace('-', '')}/index.json": raw_json_reply({"directory": {"item": [{"name": "ex99_1.htm"}]}}),
        f"/320193/{yoy_acc.replace('-', '')}/ex99_1.htm": html_reply(EX99_1_PRIOR_HTML),
    })

    res = sec_earnings_release(make_adapter(t), cik, submissions=filings, pairing_mode="yoy")
    assert res.status == "ok"
    payload = res.data[0]
    assert payload.current is not None and payload.current.ex99_1 is not None
    assert payload.current.ex99_1.filename == "opaque_release.htm"
    assert payload.prior is not None and payload.prior.accession == yoy_acc
    assert payload.days_between_filings == 365


def test_sec_earnings_release_missing_and_malformed_exhibits_partial_and_empty():
    cik = "0001234567"
    cur_acc = "0001234567-25-000105"
    pri_acc = "0001234567-25-000060"

    raw_subs = {
        "filings": {
            "recent": {
                "accessionNumber": [cur_acc, pri_acc],
                "filingDate": ["2025-11-01", "2025-08-01"],
                "form": ["8-K", "8-K"],
                "items": [["2.02"], ["2.02"]],
                "primaryDocument": ["cur.htm", "pri.htm"],
            }
        }
    }
    # Current has malformed HTML with Windows-1252 bytes; prior is missing EX-99.1 completely
    malformed_cp1252 = b"<html><body><p>Acme\x92s Q3 revenue was $500M.<b>Unclosed bold<div>Outlook: expects $520M revenue"
    t = RoutingTransport({
        f"/1234567/{cur_acc.replace('-', '')}/index.json": raw_json_reply({
            "directory": {"item": [{"name": "ex991.htm"}]}
        }),
        f"/1234567/{cur_acc.replace('-', '')}/ex991.htm": html_reply(malformed_cp1252, content_type="text/html"),
        f"/1234567/{pri_acc.replace('-', '')}/index.json": raw_json_reply({
            "directory": {"item": [{"name": "pri.htm"}]}  # No EX-99.1!
        }),
    })
    res = sec_earnings_release(make_adapter(t), cik, submissions=raw_subs)
    assert res.status == "partial"
    payload = res.data[0]
    assert payload.current is not None and payload.current.ex99_1 is not None
    assert "Acme's Q3 revenue" in payload.current.ex99_1.text
    assert payload.prior is not None and payload.prior.ex99_1 is None
    assert "missing_ex99_1" in payload.prior.gaps
    assert "prior_missing_ex99_1" in payload.gaps

    # Now both periods have missing exhibits -> empty status
    t_empty = RoutingTransport({
        f"/1234567/{cur_acc.replace('-', '')}/index.json": raw_json_reply({"directory": {"item": [{"name": "cur.htm"}]}}),
        f"/1234567/{pri_acc.replace('-', '')}/index.json": raw_json_reply({"directory": {"item": [{"name": "pri.htm"}]}}),
    })
    res_empty = sec_earnings_release(make_adapter(t_empty), cik, submissions=raw_subs)
    assert res_empty.status == "empty" and res_empty.reason == "missing_ex99_1"


# --------------------------------------------------------------------------- 2. Tests for sec_filing_tables & fetch_filing_table
def test_sec_filing_tables_catalog_grouping_and_r_htm_parsing():
    cik = "320193"
    acc = "0000320193-25-000100"
    nodash = acc.replace("-", "")
    t = RoutingTransport({
        f"/320193/{nodash}/FilingSummary.xml": xml_reply(FILING_SUMMARY_XML),
        f"/320193/{nodash}/R2.htm": html_reply(R2_STATEMENT_HTML),
        f"/320193/{nodash}/R10.htm": html_reply(R10_NOTE_NESTED_HTML),
    })
    adapter = make_adapter(t)

    cat_res = sec_filing_tables(adapter, cik, acc)
    assert cat_res.status == "ok"
    cat = cat_res.data[0]
    assert isinstance(cat, FilingTableCatalog)
    assert cat.report_count == 4  # Book "All Reports" without R*.htm skipped
    assert [r.r_file for r in cat.statements] == ["R2.htm"]
    assert [r.r_file for r in cat.notes] == ["R10.htm"]
    assert [r.r_file for r in cat.tables] == ["R32.htm"]
    assert [r.r_file for r in cat.other] == ["R1.htm"]
    assert [r.r_file for r in cat.find("segment")] == ["R10.htm", "R32.htm"]
    assert [r.r_file for r in cat.find("segment", category="Tables")] == ["R32.htm"]

    # Fetch R2.htm statement table
    r2_res = fetch_filing_table(adapter, cik, acc, "R2.htm")
    assert r2_res.status == "ok"
    tbl = r2_res.data[0]
    assert isinstance(tbl, FilingTableData)
    assert tbl.currency == "USD"
    assert tbl.unit_scale == "millions"
    assert tbl.scale_multiplier == 1_000_000
    assert len(tbl.columns) == 4
    assert tbl.columns[0].header == "3 Months Ended | Sep. 30, 2025"
    assert tbl.columns[0].period_kind == "3M"
    assert tbl.columns[0].period_end == date(2025, 9, 30)
    assert tbl.columns[2].period_kind == "9M"

    rows_by_label = {r.label: r for r in tbl.rows}
    # Footnote (1) stripped safely for numeric parsing while preserving Decimal value
    rev = rows_by_label["Total revenues"]
    assert rev.xbrl_tag == "us-gaap_Revenues"
    assert rev.cells[0].numeric_value == Decimal("1250")
    assert rev.cells[3].numeric_value == Decimal("2890")
    # Check restructuring row: negative (18), em-dash -> None, and "$ 12" is NOT corrupted by footnote regex!
    restr = rows_by_label["Restructuring charge"]
    assert restr.cells[0].numeric_value == Decimal("-18")
    assert restr.cells[1].numeric_value is None
    assert restr.cells[2].numeric_value == Decimal("12")
    assert restr.cells[3].numeric_value == Decimal("-5")
    # Row-level unit override: EPS (units) vs shares (thousands)
    eps = rows_by_label["Diluted (in dollars per share)"]
    assert eps.cells[0].numeric_value == Decimal("0.85")
    assert eps.cells[0].scale == "units"
    assert eps.cells[0].currency == "USD"
    shares = rows_by_label["Diluted (in shares)"]
    assert shares.cells[0].numeric_value == Decimal("145200")
    assert shares.cells[0].currency == "shares"
    assert shares.cells[0].scale == "thousands"
    # Hidden authRefData table must NEVER appear
    assert all("999999" not in c.raw_text for r in tbl.rows for c in r.cells)
    assert r2_res.provenance is not None and r2_res.provenance.response_sha256

    # Fetch R10.htm Note with nested subtable + narrative text
    r10_res = fetch_filing_table(adapter, cik, acc, "R10.htm")
    assert r10_res.status == "ok"
    note_tbl = r10_res.data[0]
    assert "two reportable segments" in note_tbl.narrative_text
    assert note_tbl.currency == "USD"
    assert note_tbl.unit_scale == "millions"
    note_rows = {r.label: r for r in note_tbl.rows}
    assert note_rows["Cloud Platform"].cells[0].numeric_value == Decimal("980")
    assert note_rows["Total revenue"].is_total is True
    assert note_rows["Total revenue"].cells[0].numeric_value == Decimal("1250")


def test_sec_filing_tables_rejects_traversal_and_malformed_xml():
    t = RoutingTransport({
        "/320193/000032019325000100/FilingSummary.xml": xml_reply("<!DOCTYPE foo [<!ENTITY xxe 'bad'>]><FilingSummary/>"),
    })
    adapter = make_adapter(t)
    bad_xml = sec_filing_tables(adapter, "320193", "0000320193-25-000100")
    assert bad_xml.status == "failed" and bad_xml.reason == "invalid_response"

    bad_r = fetch_filing_table(adapter, "320193", "0000320193-25-000100", "../../etc/passwd")
    assert bad_r.status == "failed" and bad_r.reason == "invalid_request"
    assert len(t.calls) == 1  # No network call attempted for unsafe r_file


# --------------------------------------------------------------------------- 3. Tests for sec_filing_sections & sec_redline_diff
def test_sec_filing_sections_splits_10k_and_10q_avoiding_toc_and_part2_item2():
    b_10k = split_filing_sections(SAMPLE_10K_HTML, form="10-K")
    assert isinstance(b_10k, FilingSectionsBundle)
    # Hidden <ix:header> stripped
    assert "EntityRegistrantName" not in b_10k.clean_text
    assert "item_1_business" in b_10k.sections
    assert "item_1a_risk_factors" in b_10k.sections
    assert "item_2_properties" in b_10k.sections
    assert "mda" in b_10k.sections
    assert "liquidity_and_capital_resources" in b_10k.sections
    assert "critical_accounting_estimates" in b_10k.sections
    assert "item_8_financial_statements" in b_10k.sections

    # Verify TOC entry was NOT chosen for Risk Factors or MD&A
    rf = b_10k.sections["item_1a_risk_factors"]
    assert "significant risks" in rf.text
    assert ".........." not in rf.text
    liq = b_10k.sections["liquidity_and_capital_resources"]
    assert "$1,850 million" in liq.text
    assert "standalone selling prices" not in liq.text  # stopped cleanly at Critical Accounting Estimates!
    cae = b_10k.sections["critical_accounting_estimates"]
    assert "standalone selling prices" in cae.text

    # 10-Q: Part I Item 2 is MD&A; Part II Item 2 is Unregistered Sales (must NOT overwrite MD&A!)
    b_10q = split_filing_sections(SAMPLE_10Q_HTML, form="10-Q")
    assert "part1_item1_financial_statements" in b_10q.sections
    assert "mda" in b_10q.sections
    assert "21% year-over-year" in b_10q.sections["mda"].text
    assert "part2_item2_unregistered_sales" in b_10q.sections
    assert "repurchased 1.2 million shares" in b_10q.sections["part2_item2_unregistered_sales"].text
    assert "item_1a_risk_factors" in b_10q.sections
    assert "liquidity_and_capital_resources" in b_10q.sections
    assert "critical_accounting_estimates" in b_10q.sections


def test_sec_filing_sections_fetch_via_url():
    url = "https://www.sec.gov/Archives/edgar/data/1234567/000123456725000105/acme-10k.htm"
    t = RoutingTransport({url: html_reply(SAMPLE_10K_HTML)})
    res = sec_filing_sections(make_adapter(t), url=url, form="10-K")
    assert res.status == "ok"
    assert res.provenance is not None and res.provenance.endpoint == url
    assert "mda" in res.data[0].sections


def test_sec_redline_diff_filters_rolling_noise_and_captures_substantive_changes():
    prior_risk = """
    Item 1A. Risk Factors
    Our business involves significant risks that could affect our financial condition.
    As of June 30, 2025, we employed 4,050 full-time employees across 14 offices globally.
    For the second quarter of 2025, our top five customers accounted for 18% of revenue.
    We previously relied on third-party data centers with legacy regional contracts that expired in May 2025.
    Competition in cloud observability remains intense and could pressure pricing.
    """
    current_risk = """
    Item 1A. Risk Factors
    Our business involves significant risks that could affect our financial condition.
    As of September 30, 2025, we employed 4,200 full-time employees across 14 offices globally.
    For the third quarter of 2025, our top five customers accounted for 19% of revenue.
    Competition in cloud observability has intensified due to bundled hyperscaler offerings and could materially pressure pricing and renewal rates.
    New U.S. export controls on advanced AI infrastructure could restrict international shipments.
    """

    diff = sec_redline_diff(current_risk, prior_risk, section_key="item_1a_risk_factors")
    assert isinstance(diff, SectionRedlineDiff)
    assert diff.has_substantive_changes is True
    # Identical paragraphs: heading + "Our business involves..."
    assert diff.unchanged_count == 2
    # Pure rolling date/number updates ("June 30, 2025 / 4,050" -> "Sep 30, 2025 / 4,200"; "second quarter / 18%" -> "third quarter / 19%") filtered!
    assert diff.filtered_noise_count == 2
    assert len(diff.added) == 1
    assert "export controls" in (diff.added[0].current_text or "")
    assert len(diff.removed) == 1
    assert "legacy regional contracts" in (diff.removed[0].prior_text or "")
    assert len(diff.revised) == 1
    assert "bundled hyperscaler" in (diff.revised[0].current_text or "")
    assert diff.revised[0].redline_markup is not None
    assert "[+" in diff.revised[0].redline_markup and "[-" in diff.revised[0].redline_markup

    # Turning off noise filtering surfaces those 2 rolling paragraphs as revised
    unfiltered = sec_redline_diff(current_risk, prior_risk, filter_rolling_noise=False)
    assert isinstance(unfiltered, SectionRedlineDiff)
    assert unfiltered.filtered_noise_count == 0
    assert len(unfiltered.revised) == 3

    # Bundle-to-bundle diff across sections
    b_cur = split_filing_sections(SAMPLE_10K_HTML, form="10-K")
    b_pri = split_filing_sections(SAMPLE_10Q_HTML, form="10-Q")
    report = sec_redline_diff(b_cur, b_pri, section_keys=["item_1a_risk_factors", "liquidity_and_capital_resources"])
    assert isinstance(report, FilingRedlineReport)
    assert "item_1a_risk_factors" in report.section_diffs
    assert "liquidity_and_capital_resources" in report.section_diffs


# --------------------------------------------------------------------------- 4. Tests for cik_scoped_efts_search
def test_cik_scoped_efts_search_validation_scoping_and_policy():
    t = RoutingTransport({
        "efts.sec.gov/LATEST/search-index": raw_json_reply({
            "hits": {
                "total": {"value": 3},
                "hits": [
                    {
                        "_id": "0000320193-25-000100:aapl-10k.htm",
                        "_source": {
                            "ciks": ["320193"],
                            "display_names": ["Apple Inc."],
                            "form": "10-K",
                            "file_date": "2025-11-01",
                            "period_ending": "2025-09-27",
                            "adsh": "0000320193-25-000100",
                            "file_type": "10-K",
                            "file_description": "10-K",
                        },
                    },
                    {
                        # Out-of-scope rogue hit for another company must be filtered out!
                        "_id": "0000789019-25-000050:msft-10k.htm",
                        "_source": {
                            "ciks": ["0000789019"],
                            "display_names": ["Microsoft Corp"],
                            "form": "10-K",
                            "file_date": "2025-10-30",
                            "adsh": "0000789019-25-000050",
                        },
                    },
                ],
            }
        })
    })

    # Empty/deny policy blocks CIK-scoped EFTS search, while default_policy permits filing_search_cik
    # (even though default_policy still blocks unscoped global filing_search)
    denied = cik_scoped_efts_search(make_adapter(t, policy=FinDataPolicy(rules=[])), "320193", "tariff")
    assert denied.status == "denied" and denied.reason == "not_in_policy"
    assert len(t.calls) == 0

    default_adapter = make_adapter(t, policy=default_policy())
    assert default_policy().decide("sec_edgar", "filing_search").reason == "not_in_policy"  # type: ignore[union-attr]
    allowed_default = cik_scoped_efts_search(default_adapter, "320193", "tariff")
    assert allowed_default.status in ("ok", "partial")
    assert len(t.calls) == 1
    t.calls.clear()

    # Strict single CIK validation rejects blank, zero, multi-CIK, and non-numeric values
    allowed_adapter = make_adapter(t, policy=efts_policy())
    for bad_cik in (None, "", "   ", "0", "0000000000", "abc", "12345678901", "320193,789019", ["320193"]):
        bad_res = cik_scoped_efts_search(allowed_adapter, bad_cik, "tariff")  # type: ignore[arg-type]
        assert bad_res.status == "failed" and bad_res.reason == "invalid_request"
    assert cik_scoped_efts_search(allowed_adapter, "320193", "   ").reason == "invalid_request"
    assert cik_scoped_efts_search(allowed_adapter, "320193", "tariff", start=date(2025, 5, 1), end=date(2025, 1, 1)).reason == "invalid_request"
    assert len(t.calls) == 0

    # Valid single-CIK query sends ciks=0000320193 and drops out-of-scope hits
    res = cik_scoped_efts_search(
        allowed_adapter,
        320193,
        "tariff supply chain",
        start=date(2025, 1, 1),
        end=date(2025, 11, 1),
        forms=["10-K", "10-Q"],
    )
    assert res.status == "partial"  # total=3 vs 1 kept hit
    url = t.calls[0][1]
    assert "ciks=0000320193" in url
    assert "forms=10-K%2C10-Q" in url
    assert "startdt=2025-01-01" in url and "enddt=2025-11-01" in url
    assert len(res.data) == 1
    hit = res.data[0]
    assert isinstance(hit, FullTextHit)
    assert hit.ciks == ["0000320193"]
    assert hit.entity_id == "cik:0000320193"
    assert any("Dropped 1 hit(s)" in n for n in res.coverage.notes)


# --------------------------------------------------------------------------- 5. Facade, policy/as_of/edge cases & rate limit
def test_sec_earnings_release_auto_submissions_as_of_ex9910_and_ex99_2_404():
    cik = "0001234567"
    acc_nov = "0001234567-25-000105"
    acc_aug = "0001234567-25-000060"
    acc_may = "0001234567-25-000020"

    subs_json = {
        "cik": cik,
        "filings": {
            "recent": {
                "accessionNumber": [acc_nov, acc_aug, acc_may],
                "filingDate": ["2025-11-01", "2025-08-01", "2025-05-01"],
                "form": ["8-K", "8-K", "8-K"],
                "items": [["2.02"], ["2.02"], ["2.02"]],
                "primaryDocument": ["nov.htm", "aug.htm", "may.htm"],
            }
        },
    }
    t = RoutingTransport({
        f"https://data.sec.gov/submissions/CIK{cik}.json": raw_json_reply(subs_json),
        f"/1234567/{acc_aug.replace('-', '')}/index.json": raw_json_reply({
            "directory": {"item": [{"name": "ex9910.htm"}, {"name": "ex99-1.htm"}, {"name": "ex99-2.htm"}]}
        }),
        f"/1234567/{acc_aug.replace('-', '')}/ex99-1.htm": html_reply(EX99_1_PRIOR_HTML),
        # ex99-2.htm intentionally returns 404 -> non-fatal gap, ex99-1 preserved
        f"/1234567/{acc_may.replace('-', '')}/index.json": raw_json_reply({
            "directory": {"item": [{"name": "ex991.htm"}]}
        }),
        f"/1234567/{acc_may.replace('-', '')}/ex991.htm": html_reply(EX99_1_PRIOR_HTML),
    })

    # as_of=2025-09-01 excludes Nov 1, pairing Aug 1 (current) with May 1 (prior)
    res = sec_earnings_release(make_adapter(t), cik, as_of=date(2025, 9, 1))
    assert res.status == "partial"
    payload = res.data[0]
    assert payload.current is not None and payload.current.accession == acc_aug
    assert payload.current.ex99_1 is not None and payload.current.ex99_1.filename == "ex99-1.htm"
    assert payload.current.ex99_2 is None
    assert any("ex99_2_fetch_failed" in g for g in payload.current.gaps)
    assert payload.prior is not None and payload.prior.accession == acc_may

    # Policy / User-Agent enforcement
    no_ua = SecEdgarAdapter(None, transport=t, limiter=open_limiter(), now_fn=lambda: NOW)
    assert sec_earnings_release(no_ua, cik).reason == "sec_contact_unconfigured"
    no_pol = SecEdgarAdapter(UA, transport=t, policy=FinDataPolicy(), limiter=open_limiter(), now_fn=lambda: NOW)
    assert sec_earnings_release(no_pol, cik).status == "denied"


def test_wrapped_headings_reordered_redline_and_efts_errors():
    wrapped_html = """
    <html><body>
      <div><b>Item 1A.</b><br/><b>Risk Factors</b></div>
      <p>Supply chain disruptions could delay hardware deliveries across North America and Europe.</p>
      <div><b>Item 7.</b><br/><b>Management's Discussion and Analysis of Financial Condition and Results of Operations</b></div>
      <p>Demand remained strong throughout the year.</p>
      <div><b>Liquidity and Capital Resources</b></div>
      <p>Cash reserves stood at $900 million at year end.</p>
    </body></html>
    """
    bundle = split_filing_sections(wrapped_html, form="10-K")
    assert "item_1a_risk_factors" in bundle.sections
    assert "mda" in bundle.sections
    assert "liquidity_and_capital_resources" in bundle.sections

    # Reordered paragraphs (identical + pure date roll) must NOT show up as added/removed
    prior = [
        "Our credit facility matures in May 2025 with $400 million undrawn.",
        "Customer concentration in government agencies poses renewal risks.",
    ]
    current = [
        "Customer concentration in government agencies poses renewal risks.",
        "Our credit facility matures in May 2026 with $450 million undrawn.",
    ]
    diff = sec_redline_diff(current, prior)
    assert isinstance(diff, SectionRedlineDiff)
    assert diff.has_substantive_changes is False
    assert diff.unchanged_count == 1
    assert diff.filtered_noise_count == 1

    # Non-SEC URL rejected safely
    bad_sec = sec_filing_sections(make_adapter(RoutingTransport()), url="https://evil.example.com/10k.htm")
    assert bad_sec.status == "failed" and bad_sec.reason == "invalid_request"

    # EFTS empty hits, CIK prefix normalization, and HTTP 429 handling
    t_empty = RoutingTransport({
        "efts.sec.gov/LATEST/search-index": raw_json_reply({"hits": {"total": {"value": 0}, "hits": []}}),
    })
    res_empty = cik_scoped_efts_search(make_adapter(t_empty, policy=efts_policy()), "CIK320193", "impairment")
    assert res_empty.status == "empty" and res_empty.reason == "no_hits"
    assert "ciks=0000320193" in t_empty.calls[0][1]

    t_429 = RoutingTransport({"efts.sec.gov": Reply(429, b"", {"Retry-After": "9"})})
    res_429 = cik_scoped_efts_search(make_adapter(t_429, policy=efts_policy()), "320193", "impairment")
    assert res_429.status == "rate_limited" and res_429.retryable is True


def test_sec_deep_adapter_facade_and_rate_limiting():
    lim = InProcessRateLimiter({"sec_edgar": ProviderLimit(1.0, 1.0)}, now=lambda: NOW)
    t = RoutingTransport({
        "FilingSummary.xml": xml_reply(FILING_SUMMARY_XML),
    })
    deep = SecDeepAdapter(UA, transport=t, limiter=lim, policy=efts_policy(), now_fn=lambda: NOW)

    r1 = deep.filing_tables("320193", "0000320193-25-000100")
    assert r1.status == "ok"
    r2 = deep.cik_scoped_efts_search("320193", "backlog")
    assert r2.status == "rate_limited" and r2.retryable is True
    assert len(t.calls) == 1


def test_sec_earnings_release_single_newline_safe_harbor_preserves_summary_and_guidance():
    # Apple-style EX-99.1 HTML where consecutive <p>/<div> elements produce single-\n lines
    # (no blank lines between headline, CEO quote, guidance, and Safe Harbor boilerplate).
    cik = "0000320193"
    acc = "0000320193-26-000024"
    single_nl_html = (
        "<html><body>"
        "<p>Apple Reports Fourth Quarter Results</p>"
        "<p>CUPERTINO, Calif. — Apple today announced financial results for its fiscal 2026 fourth quarter ended September 26, 2026, posting quarterly revenue of $109.2 billion.</p>"
        "<p>\"Today, Apple is pleased to report a September quarter revenue record with strong Services momentum,\" said Tim Cook, Apple's CEO.</p>"
        "<p>\"We expect December quarter total company revenue to grow between 7% and 9% year over year with gross margin between 47% and 48%,\" said Kevan Parekh, Apple's CFO.</p>"
        "<p>This press release contains forward-looking statements, within the meaning of the Private Securities Litigation Reform Act of 1995, and actual results could differ materially from expectations.</p>"
        "<p>Apple Investor Relations Contact: ir@apple.com</p>"
        "</body></html>"
    )
    t = RoutingTransport({
        f"https://data.sec.gov/submissions/CIK{cik}.json": raw_json_reply({
            "cik": cik,
            "filings": {
                "recent": {
                    "accessionNumber": [acc],
                    "filingDate": ["2026-10-30"],
                    "reportDate": ["2026-09-26"],
                    "form": ["8-K"],
                    "items": [["2.02", "9.01"]],
                    "primaryDocument": ["a8-k.htm"],
                }
            },
        }),
        f"/320193/{acc.replace('-', '')}/index.json": raw_json_reply({
            "directory": {"item": [{"name": "a8-kex991q4202609262026.htm", "type": "text.gif"}]}
        }),
        f"/320193/{acc.replace('-', '')}/a8-kex991q4202609262026.htm": html_reply(single_nl_html),
    })

    res = sec_earnings_release(make_adapter(t), cik)
    assert res.status == "partial"  # only current quarter present
    cur = res.data[0].current
    assert cur is not None
    assert "Apple Reports Fourth Quarter Results" in cur.management_summary
    assert "$109.2 billion" in cur.management_summary
    assert "Private Securities Litigation Reform Act" not in cur.management_summary
    assert any("Tim Cook" in h for h in cur.management_highlights)
    assert len(cur.guidance_passages) >= 1
    guidance_all = " ".join(g.text for g in cur.guidance_passages)
    assert "7% and 9%" in guidance_all
    assert "Private Securities Litigation Reform Act" not in guidance_all


def test_sec_earnings_release_resolves_dot_html_index_page():
    """Verify *-index.html (not just *-index.htm) is parsed when index.json lists issuer filenames."""
    cik = "0001045810"
    acc = "0001045810-26-000088"
    nodash = acc.replace("-", "")
    index_html = (
        "<html><body><p>Items: Item 2.02, Item 9.01</p>"
        "<table><tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>"
        "<tr><td>2</td><td>EX-99.1</td><td>q3fy27pr.htm</td><td>EX-99.1</td><td>348063</td></tr>"
        "<tr><td>3</td><td>EX-99.2</td><td>q3fy27cfocommentary.htm</td><td>EX-99.2</td><td>251464</td></tr>"
        "</table></body></html>"
    )
    pr_html = (
        "<html><body><h1>NVIDIA Announces Financial Results for Third Quarter</h1>"
        "<p>Revenue was $57.0 billion, up 62% from a year ago.</p>"
        "<h2>Outlook</h2><p>Revenue for the fourth quarter is expected to be $65.0 billion, plus or minus 2%.</p>"
        "</body></html>"
    )
    cfo_html = "<html><body><p>Data Center revenue reached $51.2 billion.</p></body></html>"

    t = RoutingTransport({
        f"https://data.sec.gov/submissions/CIK{cik}.json": raw_json_reply({
            "cik": cik,
            "filings": {
                "recent": {
                    "accessionNumber": [acc],
                    "filingDate": ["2026-11-17"],
                    "reportDate": ["2026-10-25"],
                    "form": ["8-K"],
                    "items": [["2.02", "9.01"]],
                    "primaryDocument": ["nvda-20261117.htm"],
                }
            },
        }),
        f"/1045810/{nodash}/index.json": raw_json_reply({
            "directory": {
                "item": [
                    {"name": f"{acc}-index-headers.html", "type": "text.gif"},
                    {"name": f"{acc}-index.html", "type": "text.gif"},
                    {"name": "q3fy27cfocommentary.htm", "type": "text.gif"},
                    {"name": "q3fy27pr.htm", "type": "text.gif"},
                ]
            }
        }),
        f"/1045810/{nodash}/{acc}-index.html": html_reply(index_html),
        f"/1045810/{nodash}/q3fy27pr.htm": html_reply(pr_html),
        f"/1045810/{nodash}/q3fy27cfocommentary.htm": html_reply(cfo_html),
    })

    res = sec_earnings_release(make_adapter(t), cik)
    assert res.status == "partial"  # single period fixture
    cur = res.data[0].current
    assert cur is not None
    assert cur.ex99_1 is not None
    assert cur.ex99_1.filename == "q3fy27pr.htm"
    assert "$57.0 billion" in cur.management_summary
    assert cur.ex99_2 is not None
    assert cur.ex99_2.filename == "q3fy27cfocommentary.htm"
    assert len(cur.guidance_passages) >= 1
    assert "$65.0 billion" in cur.guidance_passages[0].text

