"""Comprehensive offline unit tests for V1 Layer 1 deterministic SEC event engine."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import json
from threading import Event
from typing import Any

import pytest

from app.fin_data.adapters.sec_events import (
    L1RawRecord,
    SecPacingError,
    SecPacingLimiter,
    build_scale_anchor,
    evaluate_materiality_against_anchor,
    evaluate_share_pct_against_anchor,
    fetch_company_submissions,
    fetch_lazy_attachment,
    get_rules_taxonomy_subset_hash,
    l1_raw_cache_key,
    load_sec_events_rules,
    normalize_event_subtype,
    rescreen_from_l1_cache,
    resolve_affected_pillars,
    scan_sec_material_events,
)
from app.fin_data.adapters.sec_events.form4 import normalize_officer_tier, strip_xsl_prefix
from app.fin_data.adapters.sec_events.form8k import classify_8k_items, slice_8k_items
from app.fin_data.adapters.sec_events.form13dg import strip_13dg_xsl_prefix
from app.fin_data.adapters.sec_events.pacing import RawSubmissionRow, sec_get_json
from app.fin_data.policy import default_policy
from app.research_signals import (
    EventRunMetrics,
    FailedAccession,
    LazyAttachmentRef,
    RawEventCandidate,
    ScaleAnchor,
    SecEventScanResult,
    SuppressedEventRecord,
)
from app.storage import (
    InMemoryDocumentRepository,
    PostgresDocumentRepository,
    _drop_legacy_null_fields,
    postgres_unit_of_work,
    pydantic_json_dumper,
)
from tests.fin_data.helpers import Reply, UA


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class RoutingSecTransport:
    """Offline URL router simulating SEC EDGAR endpoints."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes: dict[str, Any] = dict(routes or {})
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.sequence_replies: dict[str, list[Any]] = {}

    def add(self, url_or_key: str, reply: Any) -> None:
        self.routes[url_or_key] = reply

    def add_sequence(self, url_or_key: str, replies: list[Any]) -> None:
        self.sequence_replies[url_or_key] = list(replies)

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes, timeout: float) -> Reply:
        self.calls.append((method, url, dict(headers)))
        for seq_key, seq_list in self.sequence_replies.items():
            if seq_key == url or seq_key in url:
                res = seq_list.pop(0) if len(seq_list) > 1 else seq_list[0]
                if isinstance(res, Exception):
                    raise res
                return res
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

    def call_count_matching(self, substring: str) -> int:
        return sum(1 for _m, u, _h in self.calls if substring in u)


def _json_reply(payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> Reply:
    hdrs = {"content-type": "application/json", **(headers or {})}
    return Reply(status, json.dumps(payload).encode("utf-8"), hdrs)


def _html_reply(html: str, status: int = 200) -> Reply:
    return Reply(status, html.encode("utf-8"), {"content-type": "text/html; charset=utf-8"})


def _xml_reply(xml: str, status: int = 200) -> Reply:
    return Reply(status, xml.encode("utf-8"), {"content-type": "application/xml; charset=utf-8"})


def _open_sec_limiter() -> SecPacingLimiter:
    return SecPacingLimiter(rate_per_sec=9.0, burst=1000.0, clock=FakeClock(), sleep_fn=lambda _s: None)


def _make_submissions(
    *,
    cik: str = "0000320193",
    name: str = "Acme Corp",
    tickers: list[str] | None = None,
    rows: list[dict[str, Any]] | None = None,
    files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cols: dict[str, list[Any]] = {
        "accessionNumber": [],
        "filingDate": [],
        "reportDate": [],
        "acceptanceDateTime": [],
        "form": [],
        "primaryDocument": [],
        "primaryDocDescription": [],
        "items": [],
    }
    has_items = False
    for r in rows or []:
        cols["accessionNumber"].append(r["accession"])
        cols["filingDate"].append(r["filed_at"])
        cols["reportDate"].append(r.get("report_date", r["filed_at"]))
        cols["acceptanceDateTime"].append(r.get("accepted_at", f"{r['filed_at']}T20:05:00.000Z"))
        cols["form"].append(r["form"])
        cols["primaryDocument"].append(r.get("primary_doc", "doc.htm"))
        cols["primaryDocDescription"].append(r.get("description", ""))
        if "items" in r:
            has_items = True
            cols["items"].append(r["items"])
        else:
            cols["items"].append([])
    if not has_items and rows and all("items_absent" in r for r in rows):
        del cols["items"]
    return {
        "cik": str(int(cik)),
        "name": name,
        "tickers": tickers if tickers is not None else ["ACME"],
        "exchanges": ["NASDAQ"],
        "sic": "3571",
        "filings": {
            "recent": cols,
            "files": files or [],
        },
    }


# --------------------------------------------------------------------------- 1. Externalized rules & taxonomy
def test_externalized_rules_schema_and_taxonomy_hash_decoupling() -> None:
    rules = load_sec_events_rules()
    assert rules.rules_version == "v1"
    assert rules.form4.dust_floor_usd == Decimal("25000")
    assert len(rules.event_subtypes) == 16
    assert "8.01" in rules.form8k.keep_all_items
    assert set(rules.form8k.statutory_critical_items) == {"1.03", "2.04", "4.02"}
    assert "not material" in rules.negation_markers

    assert normalize_event_subtype(rules, "leadership_change") == "leadership_change"
    assert normalize_event_subtype(rules, "legacy_unknown_v0") == "other_material"
    assert resolve_affected_pillars(rules, "insider_open_market_buy") == ["valuation", "capital_allocation"]
    assert resolve_affected_pillars(rules, "unmapped_subtype") == ["risk"]

    # Modifying Layer 1 thresholds / passive list / 6-K noise / Layer 3 negation markers must NOT change L2 taxonomy hash
    base_hash = get_rules_taxonomy_subset_hash(rules)
    tweaked = load_sec_events_rules(
        overrides={
            "form4": {"dust_floor_usd": "50000"},
            "form13dg": {"passive_institution_whitelist": ["vanguard"]},
            "negation_markers": ["however"],
        }
    )
    assert get_rules_taxonomy_subset_hash(tweaked) == base_hash


# --------------------------------------------------------------------------- 2. Pacing limiter, 429 Retry-After, Redis/PG & submissions pagination
def test_pacing_limiter_waits_cleanly_at_4_5_rps_and_supports_cancel_and_deadline() -> None:
    clock = FakeClock(start=100.0)
    limiter = SecPacingLimiter(rate_per_sec=4.5, burst=4.5, clock=clock, sleep_fn=clock.sleep)

    # First 4 tokens consume burst immediately (0 sleep)
    for _ in range(4):
        limiter.acquire()
    assert clock.sleeps == []

    # 5th request waits cleanly (~0.111s) instead of raising non-waiting burst error
    limiter.acquire()
    assert len(clock.sleeps) == 1
    assert 0.10 <= clock.sleeps[0] <= 0.13

    # Deadline exceeded fails fast without sleeping past deadline
    with pytest.raises(SecPacingError) as err_dl:
        limiter.acquire(deadline=clock.now + 0.01)
    assert err_dl.value.reason == "deadline_exceeded"

    # Cancel event aborts immediately
    cancel = Event()
    cancel.set()
    with pytest.raises(SecPacingError) as err_cancel:
        limiter.acquire(cancel_event=cancel)
    assert err_cancel.value.reason == "cancelled"


def test_sec_get_json_handles_429_retry_after_and_redis_postgres_fallbacks() -> None:
    clock = FakeClock(start=500.0)
    limiter = SecPacingLimiter(rate_per_sec=4.5, burst=4.5, clock=clock, sleep_fn=clock.sleep)
    transport = RoutingSecTransport()
    transport.add_sequence(
        "https://data.sec.gov/submissions/CIK0000320193.json",
        [
            Reply(429, b"Too Many Requests", {"Retry-After": "2"}),
            _json_reply({"cik": "320193", "filings": {"recent": {"accessionNumber": []}}}),
        ],
    )
    doc, _sha = sec_get_json(
        "https://data.sec.gov/submissions/CIK0000320193.json",
        user_agent=UA,
        limiter=limiter,
        transport=transport,
        clock=clock,
        sleep_fn=clock.sleep,
    )
    assert doc["cik"] == "320193"
    assert any(s >= 2.0 for s in clock.sleeps)

    # Optional Redis pacing test
    class FakeRedis:
        def __init__(self) -> None:
            self.eval_calls = 0

        def eval(self, script, numkeys, *args):
            self.eval_calls += 1
            return [0, 150_000] if self.eval_calls == 1 else [1, 0]

    rclock = FakeClock(start=10.0)
    r_client = FakeRedis()
    r_lim = SecPacingLimiter(clock=rclock, sleep_fn=rclock.sleep, redis_client=r_client)
    r_lim.acquire()
    assert r_client.eval_calls == 2
    assert rclock.sleeps == [pytest.approx(0.15, abs=0.01)]

    # Optional short-tx Postgres pacing test (opens and closes independent connection per acquire)
    pg_calls: list[str] = []

    class FakePgCursor:
        def __init__(self, row_val):
            self.row_val = row_val
            self.step = 0

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return None

        def execute(self, sql, params=None):
            pg_calls.append(sql)
            self.step += 1

        def fetchone(self):
            if self.step == 2:
                return (self.row_val,)
            if self.step == 3:
                return (1000.0,)
            return None

    class FakePgConn:
        def __init__(self, next_ts: str | None) -> None:
            self.next_ts = next_ts
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            self.closed = True

        def cursor(self):
            return FakePgCursor(self.next_ts)

    pg_clock = FakeClock(start=100.0)
    conns: list[FakePgConn] = []

    def pg_factory(_dsn: str):
        c = FakePgConn(None)
        conns.append(c)
        return c

    pg_lim = SecPacingLimiter(clock=pg_clock, sleep_fn=pg_clock.sleep, postgres_dsn="postgresql://x/y", postgres_connection_factory=pg_factory)
    pg_lim.acquire()
    assert len(conns) == 1 and conns[0].closed
    assert any("pg_advisory_xact_lock" in sql for sql in pg_calls)


def test_raw_submissions_preserves_items_and_paginates_filings_files() -> None:
    transport = RoutingSecTransport()
    recent = _make_submissions(
        rows=[
            {
                "accession": "0000320193-25-000010",
                "filed_at": "2025-11-10",
                "form": "8-K",
                "primary_doc": "k8.htm",
                "items": ["2.02", "5.02", "9.01"],
            }
        ],
        files=[
            {
                "name": "CIK0000320193-submissions-001.json",
                "filingCount": 1,
                "filingFrom": "2025-01-01",
                "filingTo": "2025-10-01",
            },
            {
                "name": "CIK0000320193-submissions-002.json",
                "filingCount": 1,
                "filingFrom": "2020-01-01",
                "filingTo": "2024-01-01",
            },
        ],
    )
    page1 = _make_submissions(
        rows=[
            {
                "accession": "0000320193-25-000002",
                "filed_at": "2025-08-15",
                "form": "8-K",
                "primary_doc": "old8k.htm",
                "items": ["8.01"],
            }
        ]
    )["filings"]["recent"]
    transport.add("https://data.sec.gov/submissions/CIK0000320193.json", _json_reply(recent))
    transport.add("https://data.sec.gov/submissions/CIK0000320193-submissions-001.json", _json_reply(page1))

    res = fetch_company_submissions(
        "320193",
        user_agent=UA,
        fetch_since_date=date(2025, 8, 1),
        as_of=date(2025, 11, 20),
        limiter=_open_sec_limiter(),
        transport=transport,
    )
    assert res.pages_fetched == 1
    assert [r.accession for r in res.rows] == ["0000320193-25-000010", "0000320193-25-000002"]
    assert res.rows[0].items == ["2.02", "5.02", "9.01"]
    assert res.rows[1].items == ["8.01"]
    # submissions-002 was before fetch_since_date so never fetched
    assert transport.call_count_matching("submissions-002") == 0


# --------------------------------------------------------------------------- 3. ScaleAnchor: XBRL, as_of, IFRS fallback, currency mismatch, DEI & FPI ADR
def test_scale_anchor_us_gaap_as_of_truncation_and_dei_facts() -> None:
    transport = RoutingSecTransport()
    rev_q = [
        {"start": "2024-10-01", "end": "2024-12-31", "val": 100, "filed": "2025-02-01", "accn": "0001", "form": "10-K"},
        {"start": "2025-01-01", "end": "2025-03-31", "val": 110, "filed": "2025-05-01", "accn": "0002", "form": "10-Q"},
        {"start": "2025-04-01", "end": "2025-06-30", "val": 120, "filed": "2025-08-01", "accn": "0003", "form": "10-Q"},
        {"start": "2025-07-01", "end": "2025-09-30", "val": 130, "filed": "2025-11-01", "accn": "0004", "form": "10-Q"},
        # Future filing AFTER as_of (must be truncated!)
        {"start": "2025-10-01", "end": "2025-12-31", "val": 999, "filed": "2026-02-01", "accn": "0005", "form": "10-K"},
    ]
    ni_q = [
        {"start": "2024-10-01", "end": "2024-12-31", "val": 20, "filed": "2025-02-01", "accn": "0001"},
        {"start": "2025-01-01", "end": "2025-03-31", "val": 25, "filed": "2025-05-01", "accn": "0002"},
        {"start": "2025-04-01", "end": "2025-06-30", "val": 30, "filed": "2025-08-01", "accn": "0003"},
        {"start": "2025-07-01", "end": "2025-09-30", "val": 35, "filed": "2025-11-01", "accn": "0004"},
    ]
    assets = [
        {"end": "2025-09-30", "val": 2000, "filed": "2025-11-01", "accn": "0004"},
        {"end": "2025-12-31", "val": 9999, "filed": "2026-02-01", "accn": "0005"},
    ]
    dei_pf = [
        {"end": "2024-06-28", "val": 5000000000, "filed": "2025-02-01", "accn": "0001"},
        {"end": "2025-06-30", "val": 8888888888, "filed": "2026-02-01", "accn": "0005"},
    ]
    dei_shares = [
        {"end": "2025-10-25", "val": 150000000, "filed": "2025-11-01", "accn": "0004"},
    ]

    transport.add("/companyconcept/CIK0000320193/us-gaap/Revenues.json", _json_reply({"units": {"USD": rev_q}}))
    transport.add("/companyconcept/CIK0000320193/us-gaap/NetIncomeLoss.json", _json_reply({"units": {"USD": ni_q}}))
    transport.add("/companyconcept/CIK0000320193/us-gaap/Assets.json", _json_reply({"units": {"USD": assets}}))
    transport.add("/companyconcept/CIK0000320193/dei/EntityPublicFloat.json", _json_reply({"units": {"USD": dei_pf}}))
    transport.add(
        "/companyconcept/CIK0000320193/dei/EntityCommonStockSharesOutstanding.json",
        _json_reply({"units": {"shares": dei_shares}}),
    )

    anchor = build_scale_anchor(
        "320193",
        as_of_filed_date=date(2025, 11, 15),
        is_fpi=False,
        user_agent=UA,
        limiter=_open_sec_limiter(),
        transport=transport,
    )
    assert anchor.taxonomy_used == "us-gaap"
    assert anchor.currency == "USD"
    assert anchor.latest_ttm_revenue == "460"
    assert anchor.latest_net_income == "110"
    assert anchor.latest_total_assets == "2000"
    assert anchor.public_float_usd == "5000000000"
    assert anchor.public_float_as_of_date == date(2024, 6, 28)
    assert anchor.shares_outstanding == "150000000"
    assert anchor.share_unit_basis == "common_shares"

    # Currency mismatch detection
    mismatch = evaluate_materiality_against_anchor(Decimal("50"), "EUR", anchor, metric="revenue")
    assert mismatch.status == "currency_mismatch"
    ok_check = evaluate_materiality_against_anchor(Decimal("46"), "USD", anchor, metric="revenue")
    assert ok_check.status == "ok" and ok_check.ratio == "0.1000"


def test_scale_anchor_ifrs_fallback_fpi_adr_guard_and_missing_dei_gaps() -> None:
    transport = RoutingSecTransport()
    # us-gaap 404s; ifrs-full has TWD facts; dei returns 404
    transport.add(
        "/companyconcept/CIK0001046179/ifrs-full/Revenue.json",
        _json_reply({"units": {"TWD": [{"start": "2024-01-01", "end": "2024-12-31", "val": 2800000000, "filed": "2025-04-01"}]}}),
    )
    transport.add(
        "/companyconcept/CIK0001046179/ifrs-full/ProfitLoss.json",
        _json_reply({"units": {"TWD": [{"start": "2024-01-01", "end": "2024-12-31", "val": 1100000000, "filed": "2025-04-01"}]}}),
    )
    transport.add(
        "/companyconcept/CIK0001046179/ifrs-full/Assets.json",
        _json_reply({"units": {"TWD": [{"end": "2024-12-31", "val": 6000000000, "filed": "2025-04-01"}]}}),
    )
    transport.add(
        "/companyconcept/CIK0001046179/dei/EntityCommonStockSharesOutstanding.json",
        _json_reply({"units": {"shares": [{"end": "2024-12-31", "val": 25000000000, "filed": "2025-04-01"}]}}),
    )

    anchor = build_scale_anchor(
        "0001046179",
        as_of_filed_date=date(2025, 11, 1),
        is_fpi=True,
        user_agent=UA,
        limiter=_open_sec_limiter(),
        transport=transport,
    )
    assert anchor.taxonomy_used == "ifrs-full"
    assert anchor.currency == "TWD"
    assert anchor.latest_ttm_revenue == "2800000000"
    assert anchor.public_float_usd is None
    assert anchor.share_unit_basis == "ordinary_vs_adr_unverified"
    assert "missing_dei_EntityPublicFloat" in anchor.scale_anchor_gaps
    assert "fpi_share_unit_basis_ordinary_vs_adr_unverified" in anchor.scale_anchor_gaps

    # Guard must refuse dividing ADS shares by ordinary shares on FPI
    pct_guard = evaluate_share_pct_against_anchor(Decimal("5000000"), anchor)
    assert pct_guard.status == "unverified_adr_ratio"


# --------------------------------------------------------------------------- 4. Form 8-K: Mixed 2.02, 8.01, Cover/Signatures, EX-16.1, EX-99 tables, EX-10/4 Lazy
def test_form8k_mixed_202_dual_route_cover_stripping_ex16_ex99_and_lazy_cap() -> None:
    rules = load_sec_events_rules()
    assert classify_8k_items(["2.02", "9.01"], rules)[0] == "routed_to_earnings_engine"
    assert classify_8k_items(["2.02", "7.01", "9.01"], rules)[0] == "routed_to_earnings_engine"
    assert classify_8k_items(["5.07", "9.01"], rules)[0] == "low_materiality_filtered"
    assert classify_8k_items(["8.01"], rules)[0] == "keep"

    dec, kept, mixed = classify_8k_items(["2.02", "5.02", "9.01"], rules)
    assert dec == "keep" and kept == ["5.02"] and mixed is True

    # Realistic 8-K HTML with Cover Page, mixed Item 2.02 + Item 5.02 + Item 4.01 + Item 8.01, and SIGNATURES
    mixed_html = """
    <html><body>
      <p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</p>
      <p>FORM 8-K CURRENT REPORT</p>
      <p>Check the appropriate box below if the Form 8-K filing is intended to satisfy Rule 425...</p>
      <p>Item 2.02 Results of Operations and Financial Condition.</p>
      <p>On November 10, 2025, Acme Corp issued a press release announcing Q3 financial results (Exhibit 99.1).</p>
      <p>Item 5.02 Departure of Directors or Certain Officers; Election of Directors.</p>
      <p>Effective November 12, 2025, Chief Financial Officer Alice Smith resigned to pursue other opportunities. The Board appointed Bob Jones as Interim CFO. See Exhibit 10.1 separation agreement.</p>
      <p>Item 4.01 Changes in Registrant's Certifying Accountant.</p>
      <p>On November 10, 2025, the Audit Committee dismissed OldCPA LLP. See Exhibit 16.1.</p>
      <p>Item 8.01 Other Events.</p>
      <p>The Company announced a $500 million share repurchase authorization. See Exhibit 99.2.</p>
      <p>Item 9.01 Financial Statements and Exhibits.</p>
      <p>Exhibit 99.1 Earnings Press Release; Exhibit 16.1 Letter from OldCPA; Exhibit 10.1 Separation Agreement.</p>
      <p>SIGNATURES</p>
      <p>Pursuant to the requirements of the Securities Exchange Act of 1934, the registrant has duly caused this report to be signed.</p>
    </body></html>
    """
    ex16_html = "<html><body><p>November 10, 2025</p><p>Securities and Exchange Commission</p><p>We have read Item 4.01 and disagree with the characterization regarding internal control over financial reporting.</p></body></html>"
    ex99_2_html = """
    <html><body>
      <p>Acme Corp Announces $500 Million Share Repurchase Program.</p>
      <p>The Board authorized up to $500 million in open-market repurchases through December 2026.</p>
      <table>
        <tr><th>Tranche</th><th>Amount</th></tr>
        <tr><td>Q4 2025 Target</td><td>$250 million</td></tr>
      </table>
    </body></html>
    """

    transport = RoutingSecTransport()
    subs = _make_submissions(
        rows=[
            {
                "accession": "0000320193-25-000101",
                "filed_at": "2025-11-12",
                "form": "8-K",
                "primary_doc": "mixed8k.htm",
                "items": ["2.02", "5.02", "4.01", "8.01", "9.01"],
            },
            {
                "accession": "0000320193-25-000102",
                "filed_at": "2025-11-11",
                "form": "8-K",
                "primary_doc": "earn8k.htm",
                "items": ["2.02", "9.01"],
            },
            {
                "accession": "0000320193-25-000103",
                "filed_at": "2025-11-10",
                "form": "8-K",
                "primary_doc": "vote8k.htm",
                "items": ["5.07"],
            },
        ]
    )
    transport.add("https://data.sec.gov/submissions/CIK0000320193.json", _json_reply(subs))
    transport.add("/320193/000032019325000101/mixed8k.htm", _html_reply(mixed_html))
    transport.add(
        "/320193/000032019325000101/index.json",
        _json_reply(
            {
                "directory": {
                    "item": [
                        {"name": "mixed8k.htm", "type": "8-K"},
                        {"name": "ex16_1.htm", "type": "EX-16.1", "description": "Auditor letter"},
                        {"name": "ex99_1.htm", "type": "EX-99.1", "description": "Earnings Release"},
                        {"name": "ex99_2.htm", "type": "EX-99.2", "description": "Buyback Release"},
                        {"name": "ex10_1.htm", "type": "EX-10.1", "description": "Separation Agreement"},
                        {"name": "ex4_1.htm", "type": "EX-4.1", "description": "Senior Notes Indenture"},
                    ]
                }
            }
        ),
    )
    transport.add("/320193/000032019325000101/ex16_1.htm", _html_reply(ex16_html))
    transport.add("/320193/000032019325000101/ex99_2.htm", _html_reply(ex99_2_html))
    transport.add("/320193/000032019325000101/ex10_1.htm", _html_reply("<p>Full separation contract terms: $4.5M severance.</p>"))
    transport.add("/320193/000032019325000101/ex4_1.htm", _html_reply("A" * 3_500_000))

    res, stexts = scan_sec_material_events(
        "320193",
        user_agent=UA,
        as_of=date(2025, 11, 20),
        categories=["material_8k"],
        include_scale_anchor=False,
        enable_13dg_efts_backfill=False,
        limiter=_open_sec_limiter(),
        transport=transport,
    )
    assert len(res.target_candidates) == 1
    cand = res.target_candidates[0]
    assert cand.items == ["5.02", "4.01", "8.01"]
    assert cand.facts["mixed_earnings_202_stripped"] is True
    # Cover page, SIGNATURES, and Item 2.02 text MUST NOT appear in excerpt!
    assert "UNITED STATES SECURITIES" not in cand.excerpt
    assert "SIGNATURES" not in cand.excerpt
    assert "Q3 financial results" not in cand.excerpt
    assert "Alice Smith resigned" in cand.excerpt
    assert "disagree with the characterization" in cand.excerpt  # 100% EX-16.1
    assert "| Tranche | Amount |" in cand.excerpt  # EX-99.2 table preserved

    # Pure 2.02 and 5.07 suppressed with 0 primary HTML fetches!
    supp_reasons = {s.accession: s.suppression_reason for s in res.suppressed_events}
    assert supp_reasons["0000320193-25-000102"] == "routed_to_earnings_engine"
    assert supp_reasons["0000320193-25-000103"] == "low_materiality_filtered"
    assert transport.call_count_matching("earn8k.htm") == 0
    assert transport.call_count_matching("vote8k.htm") == 0

    # EX-10.1 and EX-4.1 were NEVER pre-downloaded during Layer 1 scan
    assert transport.call_count_matching("ex10_1.htm") == 0
    assert transport.call_count_matching("ex4_1.htm") == 0
    lazy_roles = {ref.attachment_role: ref for ref in cand.lazy_attachments}
    assert set(lazy_roles) == {"ex10_material_contract", "ex4_debt_indenture"}

    # Lazy loader succeeds for normal EX-10.1 and enforces 2–3MB cap on oversized EX-4.1
    loaded_10 = fetch_lazy_attachment(
        lazy_roles["ex10_material_contract"],
        user_agent=UA,
        limiter=_open_sec_limiter(),
        transport=transport,
    )
    assert "$4.5M severance" in loaded_10.text
    with pytest.raises(SecPacingError) as err_big:
        fetch_lazy_attachment(
            lazy_roles["ex4_debt_indenture"],
            user_agent=UA,
            max_bytes=2_500_000,
            limiter=_open_sec_limiter(),
            transport=transport,
        )
    assert err_big.value.reason == "source_too_large"


# --------------------------------------------------------------------------- 5. Form 4: ALL 7 pitfalls
def test_form4_all_pitfalls_and_14d_cluster_and_12m_baseline() -> None:
    rules = load_sec_events_rules()
    assert strip_xsl_prefix("xslF345X05/wf-form4_123.xml") == "wf-form4_123.xml"
    assert normalize_officer_tier(is_officer=True, is_director=False, is_ten_percent_owner=False, officer_title="Senior Vice President, Engineering", rules=rules) == "vp_other_officer"
    assert normalize_officer_tier(is_officer=True, is_director=False, is_ten_percent_owner=False, officer_title="President and CEO", rules=rules) == "ceo_cfo_chair"
    assert normalize_officer_tier(is_officer=True, is_director=False, is_ten_percent_owner=False, officer_title="EVP & Chief Operating Officer", rules=rules) == "c_suite_evp"

    # Case A: CFO split-fill missing price (MUST exempt from $25K floor!) + multi-owner + negative 10b5-1 footnote
    f4_missing_px_xml = """<?xml version="1.0"?>
    <ownershipDocument>
      <documentType>4</documentType>
      <periodOfReport>2025-11-10</periodOfReport>
      <aff10b5One>0</aff10b5One>
      <issuer><issuerCik>0000320193</issuerCik><issuerName>Acme Corp</issuerName><issuerTradingSymbol>ACME</issuerTradingSymbol></issuer>
      <reportingOwner>
        <reportingOwnerId><rptOwnerCik>0001111111</rptOwnerCik><rptOwnerName>Jane CFO</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer><isTenPercentOwner>0</isTenPercentOwner><officerTitle>Chief Financial Officer</officerTitle></reportingOwnerRelationship>
      </reportingOwner>
      <reportingOwner>
        <reportingOwnerId><rptOwnerCik>0002222222</rptOwnerCik><rptOwnerName>Jane CFO Family Trust</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>0</isOfficer><isTenPercentOwner>0</isTenPercentOwner><isOther>1</isOther><otherText>Family Trust</otherText></reportingOwnerRelationship>
      </reportingOwner>
      <nonDerivativeTable>
        <nonDerivativeTransaction>
          <securityTitle><value>Common Stock</value></securityTitle>
          <transactionDate><value>2025-11-10</value></transactionDate>
          <transactionCoding><transactionCode>P</transactionCode><equitySwapInvolved>0</equitySwapInvolved></transactionCoding>
          <transactionAmounts>
            <transactionShares><value>500</value></transactionShares>
            <transactionPricePerShare><footnoteId id="F1"/></transactionPricePerShare>
            <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
          </transactionAmounts>
          <postTransactionAmounts><sharesOwnedFollowingTransaction><value>10500</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
          <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeTransaction>
      </nonDerivativeTable>
      <footnotes>
        <footnote id="F1">Weighted average purchase price $19.50-$20.10. Transaction was not effected pursuant to a Rule 10b5-1 trading plan.</footnote>
      </footnotes>
    </ownershipDocument>"""

    # Case B: CEO partial cashless exercise (M=10000, S=14000 -> 10000 cashless + 4000 discretionary sale) + 100% exit (post=0) + separate I dust ($15K) + D dust split ($15K + $15K = $30K? No, separate days/codes)
    f4_partial_ms_xml = """<?xml version="1.0"?>
    <ownershipDocument>
      <documentType>4</documentType>
      <periodOfReport>2025-11-12</periodOfReport>
      <issuer><issuerCik>0000320193</issuerCik><issuerName>Acme Corp</issuerName><issuerTradingSymbol>ACME</issuerTradingSymbol></issuer>
      <reportingOwner>
        <reportingOwnerId><rptOwnerCik>0003333333</rptOwnerCik><rptOwnerName>Bob CEO</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>1</isOfficer><officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship>
      </reportingOwner>
      <nonDerivativeTable>
        <nonDerivativeTransaction>
          <securityTitle><value>Common Stock</value></securityTitle>
          <transactionDate><value>2025-11-12</value></transactionDate>
          <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
          <transactionAmounts><transactionShares><value>10000</value></transactionShares><transactionPricePerShare><value>5.00</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
          <postTransactionAmounts><sharesOwnedFollowingTransaction><value>14000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
          <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeTransaction>
        <nonDerivativeTransaction>
          <securityTitle><value>Common Stock</value></securityTitle>
          <transactionDate><value>2025-11-12</value></transactionDate>
          <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
          <transactionAmounts><transactionShares><value>14000</value></transactionShares><transactionPricePerShare><value>20.00</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
          <postTransactionAmounts><sharesOwnedFollowingTransaction><value>0</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
          <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeTransaction>
        <nonDerivativeTransaction>
          <securityTitle><value>Common Stock</value></securityTitle>
          <transactionDate><value>2025-11-12</value></transactionDate>
          <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
          <transactionAmounts><transactionShares><value>500</value></transactionShares><transactionPricePerShare><value>20.00</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
          <postTransactionAmounts><sharesOwnedFollowingTransaction><value>2000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
          <ownershipNature><directOrIndirectOwnership><value>I</value></directOrIndirectOwnership><natureOfOwnership><value>By Spouse</value></natureOfOwnership></ownershipNature>
        </nonDerivativeTransaction>
      </nonDerivativeTable>
      <derivativeTable>
        <derivativeTransaction>
          <securityTitle><value>Stock Option</value></securityTitle>
          <transactionDate><value>2025-11-12</value></transactionDate>
          <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
          <transactionAmounts><transactionShares><value>10000</value></transactionShares><transactionPricePerShare><value>5.00</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
          <underlyingSecurity><underlyingSecurityShares><value>10000</value></underlyingSecurityShares></underlyingSecurity>
          <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </derivativeTransaction>
      </derivativeTable>
    </ownershipDocument>"""

    # Case C: Director same-day split fills ($15K Direct + $15K Indirect must NOT merge, whereas two $15K Direct DO merge!)
    f4_split_di_xml = """<?xml version="1.0"?>
    <ownershipDocument>
      <documentType>4</documentType>
      <periodOfReport>2025-11-14</periodOfReport>
      <issuer><issuerCik>0000320193</issuerCik><issuerName>Acme Corp</issuerName><issuerTradingSymbol>ACME</issuerTradingSymbol></issuer>
      <reportingOwner>
        <reportingOwnerId><rptOwnerCik>0004444444</rptOwnerCik><rptOwnerName>Dir One</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>0</isOfficer></reportingOwnerRelationship>
      </reportingOwner>
      <nonDerivativeTable>
        <nonDerivativeTransaction>
          <securityTitle><value>Common Stock</value></securityTitle>
          <transactionDate><value>2025-11-14</value></transactionDate>
          <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
          <transactionAmounts><transactionShares><value>750</value></transactionShares><transactionPricePerShare><value>20.00</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
          <postTransactionAmounts><sharesOwnedFollowingTransaction><value>1750</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
          <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeTransaction>
        <nonDerivativeTransaction>
          <securityTitle><value>Common Stock</value></securityTitle>
          <transactionDate><value>2025-11-14</value></transactionDate>
          <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
          <transactionAmounts><transactionShares><value>750</value></transactionShares><transactionPricePerShare><value>20.00</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
          <postTransactionAmounts><sharesOwnedFollowingTransaction><value>2500</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
          <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeTransaction>
        <nonDerivativeTransaction>
          <securityTitle><value>Common Stock</value></securityTitle>
          <transactionDate><value>2025-11-14</value></transactionDate>
          <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
          <transactionAmounts><transactionShares><value>500</value></transactionShares><transactionPricePerShare><value>20.00</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
          <postTransactionAmounts><sharesOwnedFollowingTransaction><value>500</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
          <ownershipNature><directOrIndirectOwnership><value>I</value></directOrIndirectOwnership><natureOfOwnership><value>By LLC</value></natureOfOwnership></ownershipNature>
        </nonDerivativeTransaction>
      </nonDerivativeTable>
    </ownershipDocument>"""

    # Case D: 4/A amendment amending Dir One's 2025-11-14 Form 4 (must dedup cluster count!)
    f4_amend_xml = f4_split_di_xml.replace("<documentType>4</documentType>", "<documentType>4/A</documentType>").replace(
        "<ownershipDocument>", "<ownershipDocument><dateOfOriginalSubmission>2025-11-14</dateOfOriginalSubmission>"
    )

    # Case E: 10b5-1 via footnote (suppressed) + full M=S cashless exercise (suppressed)
    f4_10b5_xml = """<?xml version="1.0"?>
    <ownershipDocument>
      <documentType>4</documentType>
      <periodOfReport>2025-11-15</periodOfReport>
      <issuer><issuerCik>0000320193</issuerCik><issuerName>Acme Corp</issuerName><issuerTradingSymbol>ACME</issuerTradingSymbol></issuer>
      <reportingOwner>
        <reportingOwnerId><rptOwnerCik>0005555555</rptOwnerCik><rptOwnerName>Plan Seller</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>VP Sales</officerTitle></reportingOwnerRelationship>
      </reportingOwner>
      <nonDerivativeTable>
        <nonDerivativeTransaction>
          <securityTitle><value>Common Stock</value></securityTitle>
          <transactionDate><value>2025-11-15</value></transactionDate>
          <transactionCoding><transactionCode>S</transactionCode><footnoteId id="F2"/></transactionCoding>
          <transactionAmounts><transactionShares><value>5000</value></transactionShares><transactionPricePerShare><value>30.00</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
          <postTransactionAmounts><sharesOwnedFollowingTransaction><value>10000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
          <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeTransaction>
      </nonDerivativeTable>
      <footnotes><footnote id="F2">Sold pursuant to a Rule 10b5-1 trading plan adopted on May 1, 2025.</footnote></footnotes>
    </ownershipDocument>"""

    transport = RoutingSecTransport()
    subs = _make_submissions(
        rows=[
            {"accession": "0000320193-25-000201", "filed_at": "2025-11-10", "form": "4", "primary_doc": "xslF345X05/f4_cfo.xml"},
            {"accession": "0000320193-25-000202", "filed_at": "2025-11-12", "form": "4", "primary_doc": "xslF345X05/f4_ceo.xml"},
            {"accession": "0000320193-25-000203", "filed_at": "2025-11-14", "form": "4", "primary_doc": "xslF345X05/f4_dir.xml"},
            {"accession": "0000320193-25-000204", "filed_at": "2025-11-15", "form": "4/A", "primary_doc": "xslF345X05/f4_dir_a.xml"},
            {"accession": "0000320193-25-000205", "filed_at": "2025-11-16", "form": "4", "primary_doc": "xslF345X05/f4_plan.xml"},
        ]
    )
    transport.add("https://data.sec.gov/submissions/CIK0000320193.json", _json_reply(subs))
    transport.add("/320193/000032019325000201/f4_cfo.xml", _xml_reply(f4_missing_px_xml))
    transport.add("/320193/000032019325000202/f4_ceo.xml", _xml_reply(f4_partial_ms_xml))
    transport.add("/320193/000032019325000203/f4_dir.xml", _xml_reply(f4_split_di_xml))
    transport.add("/320193/000032019325000204/f4_dir_a.xml", _xml_reply(f4_amend_xml))
    transport.add("/320193/000032019325000205/f4_plan.xml", _xml_reply(f4_10b5_xml))

    res, _stexts = scan_sec_material_events(
        "320193",
        user_agent=UA,
        as_of=date(2025, 11, 20),
        categories=["insider_trade"],
        include_scale_anchor=False,
        limiter=_open_sec_limiter(),
        transport=transport,
    )
    # XSLT prefix stripped on every fetch!
    assert all("xslF345X05" not in u for _m, u, _h in transport.calls)

    by_acc = {c.accession: c for c in res.target_candidates}
    assert "0000320193-25-000205" not in by_acc
    assert any(s.accession == "0000320193-25-000205" and s.suppression_reason == "routine_10b5_1_plan" for s in res.suppressed_events)

    # Case A checks: missing price exempt + multi-owner + negative 10b5-1 not misclassified
    cfo_c = by_acc["0000320193-25-000201"]
    assert len(cfo_c.facts["reporting_owners"]) == 2
    assert cfo_c.facts["primary_owner"]["officer_tier"] == "ceo_cfo_chair"
    assert cfo_c.facts["has_missing_price_exemption"] is True
    assert cfo_c.facts["aggregated_trades"][0]["price_per_share"] is None if "price_per_share" in cfo_c.facts["aggregated_trades"][0] else cfo_c.facts["aggregated_trades"][0]["price_status"] == "missing_or_footnote_only"
    assert cfo_c.facts["behavioral_baseline"]["is_first_open_market_buy_in_12m"] is True

    # Case B checks: partial cashless M+S split + 100% exit + Indirect $10K filtered
    ceo_c = by_acc["0000320193-25-000202"]
    assert len(ceo_c.facts["aggregated_trades"]) == 1  # Indirect $10K (< $25K) filtered out!
    ceo_t = ceo_c.facts["aggregated_trades"][0]
    assert ceo_t["is_cashless_exercise"] is True
    assert ceo_t["cashless_split_kind"] == "partial_excess_discretionary_sale"
    assert ceo_t["cashless_exercise_shares"] == "10000"
    assert ceo_t["discretionary_net_sale_shares"] == "4000"
    assert ceo_t["discretionary_value_usd"] == "80000.00"
    assert ceo_t["is_100pct_exit"] is True
    assert ceo_t["pct_holding_change"] == "100.00"

    # Case C + D checks: D ($30K merged) kept, I ($10K) filtered, 4/A links to 4 and dedups cluster
    dir_a = by_acc["0000320193-25-000204"]
    assert dir_a.amends_accession == "0000320193-25-000203"
    assert dir_a.event_thread_key == by_acc["0000320193-25-000203"].event_thread_key
    assert len(dir_a.facts["aggregated_trades"]) == 1
    assert dir_a.facts["aggregated_trades"][0]["direct_or_indirect"] == "D"
    assert dir_a.facts["aggregated_trades"][0]["total_value_usd"] == "30000.00"
    # Cluster of buys: Jane CFO + Dir One = 2 distinct insiders (NOT 3 from double-counting 4 + 4/A!)
    assert dir_a.facts["cluster_context"]["distinct_insiders"] == 2
    assert dir_a.facts["cluster_context"]["is_cluster"] is True


# --------------------------------------------------------------------------- 6. Schedule 13D/G: 2024-12-18+ SCHEDULE 13D/G + legacy SC 13G + conversion + EFTS backfill
def test_form13dg_modern_and_legacy_conversion_passive_filter_and_efts_backfill() -> None:
    assert strip_13dg_xsl_prefix("xslSCHEDULE_13D_X01/primary_doc.xml") == "primary_doc.xml"
    assert strip_13dg_xsl_prefix("xslSCHEDULE_13G_X01/primary_doc.xml") == "primary_doc.xml"

    vanguard_13g_xml = """<?xml version="1.0"?>
    <edgarSubmission>
      <formData>
        <coverPageHeaderReportingPersonDetails>
          <reportingPersonName>The Vanguard Group</reportingPersonName>
          <typeOfReportingPerson>IA</typeOfReportingPerson>
          <aggregateAmountOwned>1000000</aggregateAmountOwned>
          <percentOfClass>8.1</percentOfClass>
        </coverPageHeaderReportingPersonDetails>
      </formData>
    </edgarSubmission>"""
    vanguard_13ga_small = vanguard_13g_xml.replace("<percentOfClass>8.1</percentOfClass>", "<percentOfClass>8.4</percentOfClass>")

    activist_13g_legacy = """<html><body>
      <p>NAMES OF REPORTING PERSONS</p><p>Apex Value Partners LP</p>
      <p>TYPE OF REPORTING PERSON</p><p>PN</p>
      <p>AGGREGATE AMOUNT BENEFICIALLY OWNED BY EACH REPORTING PERSON</p><p>6,000,000</p>
      <p>PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW (9)</p><p>6.2%</p>
      <p>Item 4. Purpose of Transaction</p><p>Acquired for investment purposes.</p>
    </body></html>"""

    activist_13d_modern = """<?xml version="1.0"?>
    <edgarSubmission>
      <formData>
        <coverPageHeaderReportingPersonDetails>
          <reportingPersonName>Apex Value Partners LP</reportingPersonName>
          <typeOfReportingPerson>PN</typeOfReportingPerson>
          <aggregateAmountOwned>9500000</aggregateAmountOwned>
          <percentOfClass>9.8</percentOfClass>
        </coverPageHeaderReportingPersonDetails>
        <item4><purposeOfTransaction>Converting from Schedule 13G to seek two board seats and strategic review.</purposeOfTransaction></item4>
        <item6><contractsArrangements>Holds cash-settled total return swap (TRS) referencing 1,500,000 additional shares.</contractsArrangements></item6>
      </formData>
    </edgarSubmission>"""

    transport = RoutingSecTransport()
    subs = _make_submissions(
        rows=[
            {"accession": "0000999999-25-000001", "filed_at": "2025-09-10", "form": "SC 13G", "primary_doc": "apex_13g.htm"},
            {"accession": "0001104659-25-000010", "filed_at": "2025-10-15", "form": "SCHEDULE 13G", "primary_doc": "xslSCHEDULE_13G_X01/vg_13g.xml"},
            {"accession": "0001104659-25-000020", "filed_at": "2025-11-05", "form": "SCHEDULE 13G/A", "primary_doc": "xslSCHEDULE_13G_X01/vg_13ga.xml"},
        ]
    )
    transport.add("https://data.sec.gov/submissions/CIK0000320193.json", _json_reply(subs))
    transport.add("/320193/000099999925000001/apex_13g.htm", _html_reply(activist_13g_legacy))
    transport.add("/320193/000110465925000010/vg_13g.xml", _xml_reply(vanguard_13g_xml))
    transport.add("/320193/000110465925000020/vg_13ga.xml", _xml_reply(vanguard_13ga_small))
    # EFTS backfill supplies missing SCHEDULE 13D filed by Apex Value Partners!
    transport.add(
        "efts.sec.gov/LATEST/search-index",
        _json_reply(
            {
                "hits": {
                    "total": {"value": 1},
                    "hits": [
                        {
                            "_id": "0000999999-25-000050:xslSCHEDULE_13D_X01/apex_13d.xml",
                            "_source": {
                                "ciks": ["0000320193"],
                                "adsh": "0000999999-25-000050",
                                "form": "SCHEDULE 13D",
                                "file_date": "2025-11-14",
                            },
                        }
                    ],
                }
            }
        ),
    )
    transport.add("/320193/000099999925000050/apex_13d.xml", _xml_reply(activist_13d_modern))

    res, _stexts = scan_sec_material_events(
        "320193",
        user_agent=UA,
        as_of=date(2025, 11, 20),
        categories=["ownership_13dg"],
        include_scale_anchor=False,
        enable_13dg_efts_backfill=True,
        limiter=_open_sec_limiter(),
        transport=transport,
    )
    assert all("xslSCHEDULE_13" not in u for _m, u, _h in transport.calls)
    # Vanguard 13G (initial passive <10%) and 13G/A (+0.3 pp < 1.0 pp) suppressed;
    # Apex SC 13G (non-passive PN) and Apex SCHEDULE 13D (EFTS backfill, 13G->13D conversion) both kept!
    supp_13 = {s.accession: s.suppression_reason for s in res.suppressed_events}
    assert supp_13["0001104659-25-000010"] == "passive_13g_routine_threshold"
    assert supp_13["0001104659-25-000020"] == "passive_13g_minor_change"
    assert [c.accession for c in res.target_candidates] == [
        "0000999999-25-000050",
        "0000999999-25-000001",
    ]
    c13d = res.target_candidates[0]
    assert c13d.accession == "0000999999-25-000050"
    assert c13d.form == "SCHEDULE 13D"
    assert c13d.facts["is_13g_to_13d_conversion"] is True
    assert c13d.facts["has_cash_settled_derivatives_or_trs"] is True
    assert c13d.facts["delta_percentage_points"] == "3.60"
    assert c13d.amends_accession == "0000999999-25-000001"


# --------------------------------------------------------------------------- 7. FPI 6-K: FPI detection, earnings routing, buyback/monthly noise, and material 6-K
def test_form6k_fpi_detection_earnings_routing_and_noise_filtering() -> None:
    transport = RoutingSecTransport()
    subs = _make_submissions(
        cik="0001046179",
        name="Taiwan Semi ADR",
        tickers=["TSM"],
        rows=[
            {"accession": "0001046179-25-000001", "filed_at": "2025-10-16", "form": "6-K", "primary_doc": "q3_6k.htm", "description": "Q3 Financial Results"},
            {"accession": "0001046179-25-000002", "filed_at": "2025-11-08", "form": "6-K", "primary_doc": "rev_6k.htm", "description": "October Monthly Revenue Report"},
            {"accession": "0001046179-25-000003", "filed_at": "2025-11-10", "form": "6-K", "primary_doc": "buyback_6k.htm", "description": "Next Day Disclosure Return"},
            {"accession": "0001046179-25-000004", "filed_at": "2025-11-15", "form": "6-K", "primary_doc": "ceo_6k.htm", "description": "Material Event"},
            {"accession": "0001046179-25-000000", "filed_at": "2025-04-17", "form": "20-F", "primary_doc": "f20.htm"},
        ],
    )
    transport.add("https://data.sec.gov/submissions/CIK0001046179.json", _json_reply(subs))
    transport.add("/1046179/000104617925000001/q3_6k.htm", _html_reply("<p>Form 6-K: Company Announces Third Quarter Financial Results and Earnings Release.</p>"))
    transport.add("/1046179/000104617925000002/rev_6k.htm", _html_reply("<p>Form 6-K: October 2025 Monthly Revenue Report: net sales NT$300B.</p>"))
    transport.add("/1046179/000104617925000003/buyback_6k.htm", _html_reply("<p>Next Day Disclosure Return regarding daily share repurchase of 1,000,000 shares.</p>"))
    transport.add("/1046179/000104617925000004/ceo_6k.htm", _html_reply("<p>Form 6-K: Board announces strategic $10B US fab expansion and CFO succession.</p>"))

    res, _stexts = scan_sec_material_events(
        "0001046179",
        user_agent=UA,
        as_of=date(2025, 11, 20),
        categories=["foreign_6k", "insider_trade"],
        include_scale_anchor=False,
        enable_13dg_efts_backfill=False,
        limiter=_open_sec_limiter(),
        transport=transport,
    )
    assert res.is_fpi is True
    assert res.fpi_exempt_from_form4 is True
    supp = {s.accession: s.suppression_reason for s in res.suppressed_events}
    assert supp["0001046179-25-000001"] == "routed_to_earnings_engine"
    assert supp["0001046179-25-000002"] == "routine_6k_monthly_revenue"
    assert supp["0001046179-25-000003"] == "routine_6k_buyback_return"
    assert [c.accession for c in res.target_candidates] == ["0001046179-25-000004"]


# --------------------------------------------------------------------------- 8. Orchestrator: L1 short-tx cache, 0-SEC rule reload, dual-window, cap=18, failed exclusion & null round-trip
def test_orchestrator_short_tx_l1_cache_rescreen_cap18_as_of_and_failed_accessions() -> None:
    transport = RoutingSecTransport()
    rows: list[dict[str, Any]] = [
        # Future filing (after as_of=2025-11-20 -> MUST be excluded!)
        {"accession": "0000320193-25-000999", "filed_at": "2025-11-25", "form": "8-K", "primary_doc": "future.htm", "items": ["4.02"]},
        # Context window filing (2025-08-10 is within [target_start-30d, target_start) for lookback=90 around 2025-11-20: target_start=2025-08-22)
        {"accession": "0000320193-25-000800", "filed_at": "2025-08-10", "form": "8-K", "primary_doc": "ctx8k.htm", "items": ["5.02"]},
        # Failing accession (HTTP 500 -> NEVER written to processed_accessions!)
        {"accession": "0000320193-25-000777", "filed_at": "2025-11-18", "form": "8-K", "primary_doc": "broken.htm", "items": ["8.01"]},
        # Mixed 2.02+5.02 already processed in "earnings" channel (MUST NOT be skipped by V1!)
        {"accession": "0000320193-25-000700", "filed_at": "2025-11-17", "form": "8-K", "primary_doc": "mix.htm", "items": ["2.02", "5.02"]},
        # Older statutory critical 4.02 (must beat newer non-critical 8.01s when cap=18 triggers!)
        {"accession": "0000320193-25-000600", "filed_at": "2025-09-01", "form": "8-K", "primary_doc": "crit402.htm", "items": ["4.02"]},
    ]
    # Add 20 recent 8.01s in Oct-Nov 2025 to trigger soft cap = 18
    for idx in range(20):
        acc = f"0000320193-25-0005{idx:02d}"
        filed = f"2025-10-{(idx % 25) + 1:02d}"
        doc = f"ev_{idx}.htm"
        rows.append({"accession": acc, "filed_at": filed, "form": "8-K", "primary_doc": doc, "items": ["8.01"]})
        transport.add(f"/320193/{acc.replace('-', '')}/{doc}", _html_reply(f"<p>Item 8.01 Other Events.</p><p>Update {idx}.</p><p>SIGNATURES</p>"))

    transport.add("https://data.sec.gov/submissions/CIK0000320193.json", _json_reply(_make_submissions(rows=rows)))
    transport.add("/320193/000032019325000800/ctx8k.htm", _html_reply("<p>Item 5.02 CEO transition.</p><p>SIGNATURES</p>"))
    transport.add("/320193/000032019325000777/broken.htm", Reply(500, b"Internal Error"))
    transport.add("/320193/000032019325000700/mix.htm", _html_reply("<p>Item 2.02 Q3.</p><p>Item 5.02 CFO departed.</p><p>SIGNATURES</p>"))
    transport.add("/320193/000032019325000600/crit402.htm", _html_reply("<p>Item 4.02 Non-Reliance on prior financials.</p><p>SIGNATURES</p>"))

    # Spy repository verifying get_many / put_many (no .items() full scan!)
    class SpyMemoryRepo(InMemoryDocumentRepository[Any]):
        def __init__(self) -> None:
            super().__init__()
            self.get_many_calls = 0
            self.put_many_calls = 0

        def get_many(self, keys):
            self.get_many_calls += 1
            return super().get_many(keys)

        def put_many(self, items):
            self.put_many_calls += 1
            return super().put_many(items)

        def items(self):
            raise AssertionError("Must never call .items() on global L1 cache!")

    repo = SpyMemoryRepo()
    res, stexts = scan_sec_material_events(
        "320193",
        user_agent=UA,
        as_of=date(2025, 11, 20),
        lookback_days=90,
        categories=["material_8k"],
        seen_accessions={"0000320193-25-000700": {"earnings": "done"}},
        l1_cache=repo,
        include_scale_anchor=False,
        enable_13dg_efts_backfill=False,
        limiter=_open_sec_limiter(),
        transport=transport,
    )

    assert repo.get_many_calls == 1
    assert repo.put_many_calls == 1
    assert len(res.target_candidates) == 18
    assert res.metrics.l1_overflow_deferred == 4  # 20 + 1(mix) + 1(4.02) = 22 -> 18 kept, 4 deferred
    assert any("overflow_deferred:" in g for g in res.open_gaps)
    # Statutory critical 4.02 (from Sept 1) MUST be ranked #1 despite older date!
    assert res.target_candidates[0].accession == "0000320193-25-000600"
    assert res.target_candidates[0].statutory_critical_items == ["4.02"]
    # Mixed 8-K processed despite earnings channel already seeing it
    assert any(c.accession == "0000320193-25-000700" for c in res.target_candidates)
    # Context window populated
    assert [c.accession for c in res.recent_context_candidates] == ["0000320193-25-000800"]
    assert res.recent_context_candidates[0].is_context_only is True
    # Future filing excluded completely
    assert transport.call_count_matching("future.htm") == 0
    # Failed accession present in failed_accessions and NEVER in processed_accessions!
    assert [f.accession for f in res.failed_accessions] == ["0000320193-25-000777"]
    assert "0000320193-25-000777" not in res.processed_accessions
    assert all(c.source_id in stexts for c in res.target_candidates)

    # Re-run scan with warm L1 cache (0 filing HTML re-fetches for cached records!)
    calls_before = len(transport.calls)
    res2, _ = scan_sec_material_events(
        "320193",
        user_agent=UA,
        as_of=date(2025, 11, 20),
        lookback_days=90,
        categories=["material_8k"],
        l1_cache=repo,
        include_scale_anchor=False,
        enable_13dg_efts_backfill=False,
        limiter=_open_sec_limiter(),
        transport=transport,
    )
    assert res2.metrics.l1_cache_hits == 23  # 20 + mix + 4.02 + ctx
    assert res2.metrics.l1_cache_misses == 1  # only the failed one retries!
    # Only submissions + failed broken.htm were hit on network!
    new_urls = [u for _m, u, _h in transport.calls[calls_before:]]
    assert all("submissions" in u or "broken.htm" in u for u in new_urls)


def test_l1_postgres_batch_short_tx_and_drop_legacy_null_fields_roundtrip() -> None:
    # 1. Verify _drop_legacy_null_fields lossless round-trip across ALL Layer 1 & research_signals models
    sample_anchor = ScaleAnchor(
        cik="0000320193",
        as_of_filed_date=None,
        taxonomy_used=None,
        currency=None,
        latest_ttm_revenue=None,
        latest_net_income=None,
        latest_total_assets=None,
        public_float_usd=None,
        public_float_as_of_date=None,
        shares_outstanding=None,
        shares_outstanding_as_of_date=None,
        scale_anchor_gaps=["missing_dei_EntityPublicFloat"],
    )
    sample_lazy = LazyAttachmentRef(
        source_id="lazy:1",
        filename="ex10.htm",
        exhibit_type=None,
        attachment_role="ex10_material_contract",
        sec_url="https://www.sec.gov/Archives/edgar/data/320193/0001/ex10.htm",
        description=None,
    )
    sample_cand = RawEventCandidate(
        candidate_id="sig_1",
        cik="0000320193",
        symbol=None,
        company_name=None,
        category="material_8k",
        form="8-K",
        accession="0000320193-25-000001",
        amends_accession=None,
        event_thread_key="8k:0000320193:2025-11-10:8.01",
        filed_at=date(2025, 11, 10),
        event_date=None,
        reporting_lag_days=None,
        items=["8.01"],
        excerpt="Item 8.01 text.",
        source_id="src:1",
        sec_url="https://www.sec.gov/x.htm",
        filename=None,
        content_sha256=None,
        lazy_attachments=[sample_lazy],
    )
    sample_scan = SecEventScanResult(
        cik="0000320193",
        entity_id="cik:0000320193",
        symbol=None,
        company_name=None,
        as_of=None,
        scale_anchor=sample_anchor,
        target_candidates=[sample_cand],
        suppressed_events=[
            SuppressedEventRecord(
                accession="0000320193-25-000002",
                form="8-K",
                filed_at=date(2025, 11, 9),
                category="material_8k",
                suppression_reason="low_materiality_filtered",
                summary_hint=None,
            )
        ],
        failed_accessions=[
            FailedAccession(
                accession="0000320193-25-000003",
                form="4",
                filed_at=None,
                error_reason="source_timeout",
            )
        ],
        metrics=EventRunMetrics(model_used=None, protocol_used=None),
    )
    dumped_scan = pydantic_json_dumper(sample_scan)
    # Inject explicit legacy None keys before _drop_legacy_null_fields
    dumped_with_nulls = {**dumped_scan, "symbol": None, "company_name": None, "as_of": None}
    assert SecEventScanResult.model_validate(_drop_legacy_null_fields(dumped_with_nulls)) == sample_scan

    l1_rec = L1RawRecord(cik="0000320193", accession="0000320193-25-000001", category="material_8k", form="8-K", filed_at="2025-11-10")
    l1_null_injected = {**pydantic_json_dumper(l1_rec), "raw_8k": None, "raw_form4": None, "raw_13dg": None, "raw_6k": None}
    assert L1RawRecord.model_validate(_drop_legacy_null_fields(l1_null_injected)) == l1_rec

    # 2. Verify PostgresDocumentRepository batch get_many / put_many short-tx compatibility
    class FakeCursor:
        def __init__(self, conn) -> None:
            self.conn = conn

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return None

        def execute(self, sql, params):
            self.conn.calls.append((sql, params))

        def fetchall(self):
            return self.conn.all_results.pop(0)

        def fetchone(self):
            return self.conn.one_results.pop(0)

    class FakeConn:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []
            self.all_results: list[list[Any]] = [
                [
                    (
                        l1_raw_cache_key("0000320193", "0000320193-25-000001", "v1"),
                        l1_null_injected,
                        1,
                    )
                ]
            ]
            self.one_results: list[Any] = [(2,)]
            self.opened = 0

        def __enter__(self):
            self.opened += 1
            return self

        def __exit__(self, *_a):
            return None

        def cursor(self):
            return FakeCursor(self)

    conn = FakeConn()
    pg_repo = PostgresDocumentRepository(
        "postgresql://db.example.test/app",
        "sec_events_l1_raw",
        connection_factory=lambda _dsn: conn,
    )
    rules = load_sec_events_rules()
    # Confirm rescreen_from_l1_cache with Postgres-loaded raw record and local rules hot-swap (0 SEC calls!)
    loaded = pg_repo.get_many([l1_raw_cache_key("0000320193", "0000320193-25-000001", "v1")])
    assert len(loaded) == 1
    # Verify ANY(%s) single-query batch read
    assert "ANY(%s)" in conn.calls[0][0]
    pg_repo.put_many({l1_raw_cache_key("0000320193", "0000320193-25-000002", "v1"): l1_rec})
    assert conn.opened == 2  # 1 short tx for get_many, 1 short tx for put_many

    # Local rule hot-reload over raw Form 4 cache (0 SEC requests!)
    from app.fin_data.adapters.sec_events.form4 import parse_form4_xml_text

    f4_xml = """<?xml version="1.0"?>
    <ownershipDocument><documentType>4</documentType><periodOfReport>2025-11-10</periodOfReport>
      <issuer><issuerCik>0000320193</issuerCik><issuerName>Acme</issuerName><issuerTradingSymbol>ACME</issuerTradingSymbol></issuer>
      <reportingOwner><reportingOwnerId><rptOwnerCik>0007777777</rptOwnerCik><rptOwnerName>Officer X</rptOwnerName></reportingOwnerId><reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>VP Finance</officerTitle></reportingOwnerRelationship></reportingOwner>
      <nonDerivativeTable><nonDerivativeTransaction><securityTitle><value>Common</value></securityTitle><transactionDate><value>2025-11-10</value></transactionDate><transactionCoding><transactionCode>P</transactionCode></transactionCoding><transactionAmounts><transactionShares><value>1000</value></transactionShares><transactionPricePerShare><value>30.00</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts><postTransactionAmounts><sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction></postTransactionAmounts><ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature></nonDerivativeTransaction></nonDerivativeTable>
    </ownershipDocument>"""
    raw_f4 = parse_form4_xml_text(
        f4_xml,
        cik="0000320193",
        accession="0000320193-25-000333",
        form="4",
        filed_at=date(2025, 11, 11),
        sec_url="https://www.sec.gov/f4.xml",
        filename="f4.xml",
        content_sha256="abc",
        rules=rules,
    )
    row_f4 = RawSubmissionRow(cik="0000320193", accession="0000320193-25-000333", nodash="000032019325000333", form="4", filed_at=date(2025, 11, 11))
    l1_map = {"0000320193-25-000333": L1RawRecord(cik="0000320193", accession="0000320193-25-000333", category="insider_trade", form="4", filed_at="2025-11-11", raw_form4=raw_f4)}

    r_25k, _ = rescreen_from_l1_cache(cik="0000320193", rows=[row_f4], l1_records=l1_map, rules=rules, as_of=date(2025, 11, 20))
    assert len(r_25k.target_candidates) == 1  # $30K >= $25K -> kept

    rules_50k = load_sec_events_rules(overrides={"form4": {"dust_floor_usd": "50000"}})
    r_50k, _ = rescreen_from_l1_cache(cik="0000320193", rows=[row_f4], l1_records=l1_map, rules=rules_50k, as_of=date(2025, 11, 20))
    assert len(r_50k.target_candidates) == 0  # $30K < $50K -> suppressed with 0 SEC requests!
    assert r_50k.suppressed_events[0].suppression_reason == "below_dust_floor"


# --------------------------------------------------------------------------- 9. Additional Pitfall Edge Cases (M>S, aff10b5One, 8-K missing items, *-index.htm fallback, 13G >=1pp, 6-K board notice, semi-annual IFRS)
def test_additional_edge_cases_across_forms_and_scale_anchor() -> None:
    rules = load_sec_events_rules()
    from app.fin_data.adapters.sec_events.form4 import parse_form4_xml_text, process_form4_batch
    from app.fin_data.adapters.sec_events.form6k import classify_6k_text

    # 1. Partial M > S sell-to-cover (M=10000, S=4200 -> 0 discretionary net sale -> suppressed as cashless_option_exercise)
    #    plus <aff10b5One>1</aff10b5One> XML flag check
    m_gt_s_xml = """<?xml version="1.0"?>
    <ownershipDocument><documentType>4</documentType><periodOfReport>2025-11-12</periodOfReport>
      <issuer><issuerCik>0000320193</issuerCik><issuerName>Acme</issuerName><issuerTradingSymbol>ACME</issuerTradingSymbol></issuer>
      <reportingOwner><reportingOwnerId><rptOwnerCik>0006666666</rptOwnerCik><rptOwnerName>Exec Cover</rptOwnerName></reportingOwnerId><reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>COO</officerTitle></reportingOwnerRelationship></reportingOwner>
      <nonDerivativeTable>
        <nonDerivativeTransaction>
          <securityTitle><value>Common</value></securityTitle><transactionDate><value>2025-11-12</value></transactionDate>
          <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
          <transactionAmounts><transactionShares><value>10000</value></transactionShares><transactionPricePerShare><value>10</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
          <postTransactionAmounts><sharesOwnedFollowingTransaction><value>10000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
          <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeTransaction>
        <nonDerivativeTransaction>
          <securityTitle><value>Common</value></securityTitle><transactionDate><value>2025-11-12</value></transactionDate>
          <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
          <transactionAmounts><transactionShares><value>4200</value></transactionShares><transactionPricePerShare><value>50</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
          <postTransactionAmounts><sharesOwnedFollowingTransaction><value>5800</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
          <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeTransaction>
      </nonDerivativeTable>
    </ownershipDocument>"""
    raw_cover = parse_form4_xml_text(
        m_gt_s_xml,
        cik="0000320193",
        accession="0000320193-25-000881",
        form="4",
        filed_at=date(2025, 11, 13),
        sec_url="https://www.sec.gov/m_gt_s.xml",
        filename="m_gt_s.xml",
        content_sha256="x",
        rules=rules,
    )
    aff_xml = m_gt_s_xml.replace("<documentType>4</documentType>", "<documentType>4</documentType><aff10b5One>1</aff10b5One>")
    raw_aff = parse_form4_xml_text(
        aff_xml,
        cik="0000320193",
        accession="0000320193-25-000882",
        form="4",
        filed_at=date(2025, 11, 14),
        sec_url="https://www.sec.gov/aff.xml",
        filename="aff.xml",
        content_sha256="y",
        rules=rules,
    )
    assert raw_aff.document_aff_10b5_1 is True
    assert all(t.plan_10b5_1_source == "xml_flag" and t.is_10b5_1 for t in raw_aff.table_1_transactions)

    f4_cands, f4_supp, _t, _b = process_form4_batch(
        [raw_cover, raw_aff],
        rules,
        target_start=date(2025, 8, 1),
        context_start=date(2025, 7, 1),
        as_of=date(2025, 11, 20),
    )
    assert f4_cands == []
    assert {s.accession: s.suppression_reason for s in f4_supp} == {
        "0000320193-25-000881": "cashless_option_exercise",
        "0000320193-25-000882": "routine_10b5_1_plan",
    }

    # 2. 6-K Date of Board Meeting to approve quarterly results is NOT misrouted to earnings engine
    assert classify_6k_text("Date of Board Meeting to Approve Third Quarter Financial Results", rules) == "routine_6k_board_meeting_notice"

    # 3. 8-K missing items in submissions + 404 index.json falling back to *-index.htm for EX-16.1
    transport = RoutingSecTransport()
    subs = _make_submissions(
        rows=[
            {
                "accession": "0000320193-25-000901",
                "filed_at": "2025-11-10",
                "form": "8-K",
                "primary_doc": "main8k.htm",
                "items_absent": True,
            }
        ]
    )
    transport.add("https://data.sec.gov/submissions/CIK0000320193.json", _json_reply(subs))
    transport.add(
        "/320193/000032019325000901/main8k.htm",
        _html_reply("<p>Cover</p><p>Item 4.01 Changes in Certifying Accountant.</p><p>Dismissed auditor.</p><p>SIGNATURES</p>"),
    )
    transport.add(
        "/320193/000032019325000901/0000320193-25-000901-index.htm",
        _html_reply("<table><tr><td>1</td><td>Letter</td><td>ex16.htm</td><td>EX-16.1</td><td>1200</td></tr></table>"),
    )
    transport.add(
        "/320193/000032019325000901/ex16.htm",
        _html_reply("<p>We agree with the statements made by Acme Corp in Item 4.01.</p>"),
    )
    res_8k, _ = scan_sec_material_events(
        "320193",
        user_agent=UA,
        as_of=date(2025, 11, 20),
        categories=["material_8k"],
        include_scale_anchor=False,
        enable_13dg_efts_backfill=False,
        limiter=_open_sec_limiter(),
        transport=transport,
    )
    assert len(res_8k.target_candidates) == 1
    assert res_8k.target_candidates[0].items == ["4.01"]
    assert "We agree with the statements" in res_8k.target_candidates[0].excerpt

    # 4. Passive 13G/A with >= 1.0 pp jump is KEPT!
    vg_13g = """<?xml version="1.0"?><edgarSubmission><formData><coverPageHeaderReportingPersonDetails><reportingPersonName>The Vanguard Group</reportingPersonName><typeOfReportingPerson>IA</typeOfReportingPerson><aggregateAmountOwned>1000000</aggregateAmountOwned><percentOfClass>6.0</percentOfClass></coverPageHeaderReportingPersonDetails></formData></edgarSubmission>"""
    vg_13ga_big = vg_13g.replace("<percentOfClass>6.0</percentOfClass>", "<percentOfClass>7.5</percentOfClass>")
    t13 = RoutingSecTransport()
    t13.add(
        "https://data.sec.gov/submissions/CIK0000320193.json",
        _json_reply(
            _make_submissions(
                rows=[
                    {"accession": "0001104659-25-000001", "filed_at": "2025-09-01", "form": "SCHEDULE 13G", "primary_doc": "vg1.xml"},
                    {"accession": "0001104659-25-000002", "filed_at": "2025-11-10", "form": "SCHEDULE 13G/A", "primary_doc": "vg2.xml"},
                ]
            )
        ),
    )
    t13.add("/320193/000110465925000001/vg1.xml", _xml_reply(vg_13g))
    t13.add("/320193/000110465925000002/vg2.xml", _xml_reply(vg_13ga_big))
    res_13, _ = scan_sec_material_events(
        "320193",
        user_agent=UA,
        as_of=date(2025, 11, 20),
        categories=["ownership_13dg"],
        include_scale_anchor=False,
        enable_13dg_efts_backfill=False,
        limiter=_open_sec_limiter(),
        transport=t13,
    )
    assert [c.accession for c in res_13.target_candidates] == ["0001104659-25-000002"]
    assert res_13.target_candidates[0].facts["delta_percentage_points"] == "1.50"

    # 5. Semi-annual IFRS ScaleAnchor TTM calculation (H1_2025 + FY_2024 - H1_2024)
    semi_facts = {
        "ifrs-full": {
            "Revenue": {
                "units": {
                    "EUR": [
                        {"start": "2024-01-01", "end": "2024-06-30", "val": 400, "filed": "2024-08-10", "accn": "1"},
                        {"start": "2024-01-01", "end": "2024-12-31", "val": 1000, "filed": "2025-03-01", "accn": "2"},
                        {"start": "2025-01-01", "end": "2025-06-30", "val": 550, "filed": "2025-08-10", "accn": "3"},
                    ]
                }
            }
        }
    }
    semi_anchor = build_scale_anchor("0001046179", as_of_filed_date=date(2025, 11, 1), is_fpi=True, facts_override=semi_facts)
    assert semi_anchor.taxonomy_used == "ifrs-full"
    assert semi_anchor.currency == "EUR"
    assert semi_anchor.latest_ttm_revenue == "1150"
