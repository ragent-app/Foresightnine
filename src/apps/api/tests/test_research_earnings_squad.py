"""Comprehensive offline unit & integration tests for Phase A2 Scheme 2 Research Squad."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from threading import Barrier, Event, Lock
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError
import pytest

from app.earnings_external_data import ExternalEarningsService
from app.fin_data.adapters.fmp import FmpEarningsAdapter
from app.fin_data.adapters.sec_edgar import SecEdgarAdapter
from app.fin_data.models import Coverage, Permissions
from app.fin_data.policy import FinDataPolicy, PolicyRule, default_policy
from app.research_methods import compose_earnings_method_pack
from app.research_model_slots import resolve_model_slots
from app.model_protocol import ModelToolCall, ModelUsage
from app.openai_compatible import BinaryHttpResponse
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext
from app.research_earnings_diagnostics import (
    EarningsQualityRequest,
    GuidanceMetricInput,
    MetricValue,
    NonGaapAdjustmentItem,
    NonGaapBridgeInput,
    PeriodFinancials,
    earnings_quality_diagnostics,
)
from app.research_earnings_squad import (
    BearCaseBatch,
    BearCaseItem,
    CallableSlotModelRunner,
    DISALLOWED_WORKER_TOOLS,
    DictSlotModelRunner,
    EarningsDossier,
    EarningsPrefetchSnapshot,
    EarningsSquadOrchestrator,
    EvidenceCard,
    EvidenceCardBatch,
    EvidenceCitation,
    FinToolboxToolRunner,
    InMemorySquadCheckpointStore,
    JsonFileSquadCheckpointStore,
    OrgGraphEvent,
    PrefetchDependencies,
    ResearchDispatchPlan,
    SlotModelRequest,
    SlotModelResponse,
    SquadCancelledError,
    SquadCheckpoint,
    SquadConfigurationError,
    SquadGap,
    SquadPrefetchRequest,
    SquadToolViolationError,
    Stage25ReviewDecision,
    SteeringImpactDecision,
    UserSteeringInstruction,
    ValuationAndImpliedExpectationsResult,
    WORKER_ALLOWED_TOOLS,
    WorkerRole,
    WorkerTask,
    build_isolated_bear_prompt,
    gate_bear_cases,
    gate_evidence_cards,
    render_deep_report,
    resolve_method_pack_context,
    resolve_squad_slots,
)
from tests.fin_data.helpers import NOW, UA, FakeTransport, Reply, open_limiter
from tests.fin_runtime_helpers import make_toolbox


# --------------------------------------------------------------------------- Helpers & Fixtures
def _period(
    label: str,
    *,
    revenue: str,
    cogs: str,
    op_inc: str,
    ni: str,
    ocf: str,
    capex: str,
    sbc: str,
    ar: str,
    inv: str,
    ap: str,
    cash: str,
    debt: str,
    shares: str,
    ebitda: str,
    interest: str = "20",
    buybacks: str = "100",
    dividends: str = "50",
) -> PeriodFinancials:
    return PeriodFinancials(
        period_label=label,
        period_type="quarter",
        currency="USD",
        monetary_unit="USD millions",
        revenue=MetricValue(value=revenue, fact_id=f"{label}:rev", unit="USD millions", currency="USD"),
        cost_of_revenue=MetricValue(value=cogs, fact_id=f"{label}:cogs", unit="USD millions", currency="USD"),
        gross_profit=MetricValue(
            value=str(Decimal(revenue) - Decimal(cogs)),
            fact_id=f"{label}:gp",
            unit="USD millions",
            currency="USD",
        ),
        rd_expense=MetricValue(value="200", fact_id=f"{label}:rd", unit="USD millions", currency="USD"),
        sga_expense=MetricValue(value="180", fact_id=f"{label}:sga", unit="USD millions", currency="USD"),
        operating_income=MetricValue(value=op_inc, fact_id=f"{label}:ebit", unit="USD millions", currency="USD"),
        ebit=MetricValue(value=op_inc, fact_id=f"{label}:ebit2", unit="USD millions", currency="USD"),
        interest_expense=MetricValue(value=interest, fact_id=f"{label}:int", unit="USD millions", currency="USD"),
        net_income=MetricValue(value=ni, fact_id=f"{label}:ni", unit="USD millions", currency="USD"),
        ebitda=MetricValue(value=ebitda, fact_id=f"{label}:ebitda", unit="USD millions", currency="USD"),
        sbc=MetricValue(value=sbc, fact_id=f"{label}:sbc", unit="USD millions", currency="USD"),
        shares_diluted=MetricValue(value=shares, fact_id=f"{label}:sh", unit="millions of shares"),
        operating_cash_flow=MetricValue(value=ocf, fact_id=f"{label}:ocf", unit="USD millions", currency="USD"),
        capex=MetricValue(value=capex, fact_id=f"{label}:capex", unit="USD millions", currency="USD"),
        share_repurchases=MetricValue(value=buybacks, fact_id=f"{label}:bb", unit="USD millions", currency="USD"),
        dividends_paid=MetricValue(value=dividends, fact_id=f"{label}:div", unit="USD millions", currency="USD"),
        cash=MetricValue(value=cash, fact_id=f"{label}:cash", unit="USD millions", currency="USD"),
        accounts_receivable=MetricValue(value=ar, fact_id=f"{label}:ar", unit="USD millions", currency="USD"),
        inventory=MetricValue(value=inv, fact_id=f"{label}:inv", unit="USD millions", currency="USD"),
        current_assets=MetricValue(value="3500", fact_id=f"{label}:ca", unit="USD millions", currency="USD"),
        accounts_payable=MetricValue(value=ap, fact_id=f"{label}:ap", unit="USD millions", currency="USD"),
        total_debt=MetricValue(value=debt, fact_id=f"{label}:debt", unit="USD millions", currency="USD"),
        current_liabilities=MetricValue(value="1800", fact_id=f"{label}:cl", unit="USD millions", currency="USD"),
    )


def _sample_periods() -> list[PeriodFinancials]:
    # Current has AR growth > Revenue growth and SBC > 30% of FCF -> triggers deterministic Red/Yellow flags!
    return [
        _period("Q4_2025", revenue="1200", cogs="480", op_inc="260", ni="220", ocf="150", capex="40", sbc="90", ar="600", inv="140", ap="180", cash="900", debt="400", shares="100", ebitda="310"),
        _period("Q3_2025", revenue="1150", cogs="450", op_inc="250", ni="200", ocf="220", capex="35", sbc="70", ar="480", inv="135", ap="175", cash="850", debt="410", shares="100", ebitda="295"),
        _period("Q2_2025", revenue="1100", cogs="440", op_inc="240", ni="190", ocf="210", capex="30", sbc="65", ar="450", inv="130", ap="170", cash="800", debt="420", shares="99", ebitda="285"),
        _period("Q1_2025", revenue="1050", cogs="420", op_inc="225", ni="180", ocf="200", capex="30", sbc="60", ar="430", inv="125", ap="165", cash="760", debt="430", shares="98", ebitda="270"),
        _period("Q4_2024", revenue="1000", cogs="380", op_inc="240", ni="200", ocf="230", capex="30", sbc="40", ar="400", inv="120", ap="160", cash="720", debt="450", shares="96", ebitda="275"),
    ]


def _make_context(max_calls: int | None = None, max_tokens: int | None = None) -> ResearchExecutionContext:
    lock = Lock()
    calls_ledger: dict[str, int | None] = {}

    def reserve(call_id: str, tokens: int | None) -> None:
        with lock:
            calls_ledger[call_id] = tokens

    def settle(call_id: str, tokens: int | None) -> None:
        with lock:
            calls_ledger[call_id] = tokens

    return ResearchExecutionContext(
        budget=ResearchExecutionBudget(max_calls, None, max_tokens, float("inf")),
        cost_limited=False,
        ensure_running=lambda: None,
        cancel_event=lambda: None,
        reserve=reserve,
        settle=settle,
        reserve_search=lambda: None,
    )


class _UrlRouter:
    """Thread-safe offline URL pattern router for SEC & FMP tests."""

    def __init__(self, routes: dict[str, Reply]) -> None:
        self.routes = routes
        self._lock = Lock()
        self.calls: list[str] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> Reply:
        with self._lock:
            self.calls.append(url)
        for pattern, reply in self.routes.items():
            if pattern in url:
                return reply
        return Reply(404, b"{}", {"content-type": "application/json"})


def _stream_from_router(router: _UrlRouter):
    def _stream(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float, sink: Any) -> BinaryHttpResponse:
        reply = router(method, url, headers, body, timeout)
        if reply.status_code == 200:
            sink.write(reply.body)
        return BinaryHttpResponse(reply.status_code, b"", reply.headers)
    return _stream


def _sec_and_fmp_deps(*, include_fmp_key: bool = True) -> tuple[PrefetchDependencies, _UrlRouter, _UrlRouter]:
    cik = "0001234567"
    acc_10q_cur = "0001234567-25-000101"
    acc_10q_pri = "0001234567-25-000051"
    acc_8k_cur = "0001234567-25-000102"
    acc_8k_pri = "0001234567-25-000052"

    sub_doc = {
        "cik": cik,
        "name": "Acme Cloud Systems Inc.",
        "sic": "7372",
        "sicDescription": "Services-Prepackaged Software",
        "tickers": ["ACME"],
        "filings": {
            "recent": {
                "accessionNumber": [acc_8k_cur, acc_10q_cur, acc_8k_pri, acc_10q_pri],
                "filingDate": ["2025-11-05", "2025-11-06", "2025-08-05", "2025-08-06"],
                "reportDate": ["2025-09-30", "2025-09-30", "2025-06-30", "2025-06-30"],
                "acceptanceDateTime": [
                    "2025-11-05T16:30:00.000Z",
                    "2025-11-06T16:30:00.000Z",
                    "2025-08-05T16:30:00.000Z",
                    "2025-08-06T16:30:00.000Z",
                ],
                "form": ["8-K", "10-Q", "8-K", "10-Q"],
                "items": [["2.02", "9.01"], [], ["2.02", "9.01"], []],
                "primaryDocument": ["acme8k_cur.htm", "acme10q_cur.htm", "acme8k_pri.htm", "acme10q_pri.htm"],
                "primaryDocDescription": ["8-K", "10-Q", "8-K", "10-Q"],
            }
        },
    }
    filing_summary_xml = """<?xml version="1.0"?>
    <FilingSummary>
      <MyReports>
        <Report><ShortName>Condensed Consolidated Statements of Operations</ShortName><LongName>Statements of Operations</LongName><MenuCategory>Statements</MenuCategory><Position>2</Position><HtmlFileName>R2.htm</HtmlFileName></Report>
        <Report><ShortName>Segment Disaggregation</ShortName><LongName>Note 10 - Segment Disaggregation</LongName><MenuCategory>Notes</MenuCategory><Position>10</Position><HtmlFileName>R10.htm</HtmlFileName></Report>
      </MyReports>
    </FilingSummary>"""
    r10_htm = """<html><body><table class="report">
    <tr><th colspan="3">Segment Disaggregation ($ in millions)</th></tr>
    <tr><th>Segment</th><th>Three Months Ended Sep 30, 2025</th><th>Three Months Ended Sep 30, 2024</th></tr>
    <tr><td>Cloud Subscriptions</td><td>$850</td><td>$650</td></tr>
    <tr><td>Professional Services</td><td>$350</td><td>$350</td></tr>
    </table></body></html>"""
    cur_10q_html = """<html><body>
    <p><b>PART I - FINANCIAL INFORMATION</b></p>
    <p><b>Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations</b></p>
    <p>Cloud Subscription revenue grew 30.8% YoY to $850 million while remaining performance obligations reached $4.2 billion.</p>
    <p>Accounts receivable expanded due to extended enterprise payment terms on multi-year deployments.</p>
    <p><b>PART II - OTHER INFORMATION</b></p>
    <p><b>Item 1A. Risk Factors</b></p>
    <p>Extended enterprise payment terms and rising stock-based compensation of $90 million may pressure free cash flow conversion.</p>
    </body></html>"""
    pri_10q_html = """<html><body>
    <p><b>PART I - FINANCIAL INFORMATION</b></p>
    <p><b>Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations</b></p>
    <p>Cloud Subscription revenue grew 26.0% YoY to $650 million while remaining performance obligations reached $3.5 billion.</p>
    <p><b>PART II - OTHER INFORMATION</b></p>
    <p><b>Item 1A. Risk Factors</b></p>
    <p>General enterprise demand fluctuations may impact quarterly billings.</p>
    </body></html>"""
    ex99_cur = """<html><body>
    <h1>Acme Cloud Reports Record Q4 2025 Revenue of $1,200 Million</h1>
    <p>Cloud Subscriptions grew 30.8% year-over-year to $850 million.</p>
    <h2>Financial Outlook</h2>
    <p>For Q1 2026, management expects total revenue between $1,220 million and $1,250 million.</p>
    </body></html>"""
    ex99_pri = """<html><body>
    <h1>Acme Cloud Reports Q3 2025 Revenue of $1,150 Million</h1>
    <h2>Financial Outlook</h2>
    <p>For Q4 2025, management expects total revenue between $1,170 million and $1,190 million.</p>
    </body></html>"""

    sec_router = _UrlRouter({
        f"submissions/CIK{cik}.json": Reply(200, json.dumps(sub_doc).encode(), {"content-type": "application/json"}),
        f"{acc_8k_cur.replace('-', '')}/index.json": Reply(
            200,
            json.dumps({"directory": {"item": [{"name": "ex99-1.htm", "type": "EX-99.1"}]}}).encode(),
            {"content-type": "application/json"},
        ),
        f"{acc_8k_pri.replace('-', '')}/index.json": Reply(
            200,
            json.dumps({"directory": {"item": [{"name": "ex99-1.htm", "type": "EX-99.1"}]}}).encode(),
            {"content-type": "application/json"},
        ),
        f"{acc_8k_cur.replace('-', '')}/ex99-1.htm": Reply(200, ex99_cur.encode(), {"content-type": "text/html"}),
        f"{acc_8k_pri.replace('-', '')}/ex99-1.htm": Reply(200, ex99_pri.encode(), {"content-type": "text/html"}),
        f"{acc_10q_cur.replace('-', '')}/FilingSummary.xml": Reply(200, filing_summary_xml.encode(), {"content-type": "application/xml"}),
        f"{acc_10q_cur.replace('-', '')}/R10.htm": Reply(200, r10_htm.encode(), {"content-type": "text/html"}),
        f"{acc_10q_cur.replace('-', '')}/acme10q_cur.htm": Reply(200, cur_10q_html.encode(), {"content-type": "text/html"}),
        f"{acc_10q_pri.replace('-', '')}/acme10q_pri.htm": Reply(200, pri_10q_html.encode(), {"content-type": "text/html"}),
    })

    fmp_router = _UrlRouter({
        "/stable/analyst-estimates": Reply(
            200,
            json.dumps([
                {
                    "symbol": "ACME",
                    "date": "2026-03-31",
                    "revenueAvg": 1245000000,
                    "revenueLow": 1220000000,
                    "revenueHigh": 1270000000,
                    "epsAvg": 1.35,
                    "numAnalystsRevenue": 18,
                    "numAnalystsEps": 19,
                },
                {
                    "symbol": "ACME",
                    "date": "2025-09-30",
                    "revenueAvg": 1185000000,
                    "epsAvg": 1.25,
                    "numAnalystsEps": 18,
                },
            ]).encode(),
            {"content-type": "application/json"},
        ),
        "/stable/earnings": Reply(
            200,
            json.dumps([
                {
                    "symbol": "ACME",
                    "date": "2025-11-05",
                    "fiscalDateEnding": "2025-09-30",
                    "revenueEstimated": 1185000000,
                    "revenueActual": 1200000000,
                    "epsEstimated": 1.25,
                    "epsActual": 1.31,
                }
            ]).encode(),
            {"content-type": "application/json"},
        ),
        "/stable/earning-call-transcript": Reply(
            200,
            json.dumps([
                {
                    "symbol": "ACME",
                    "year": 2025,
                    "quarter": 4,
                    "date": "2025-11-05",
                    "content": (
                        "Presentation\n"
                        "Jane Doe, CEO: Cloud Subscriptions grew 30.8% YoY to $850M and NRR reached 121%.\n"
                        "Question-and-Answer Session\n"
                        "Analyst: Why did accounts receivable rise faster than revenue?\n"
                        "John Smith, CFO: Multi-year enterprise contracts back-loaded billings into September."
                    ),
                }
            ]).encode(),
            {"content-type": "application/json"},
        ),
    })

    sec_adapter = SecEdgarAdapter(
        UA,
        transport=sec_router,
        stream_transport=_stream_from_router(sec_router),
        limiter=open_limiter(),
        now_fn=lambda: NOW,
    )
    policy = default_policy()
    fmp_adapter = FmpEarningsAdapter(
        api_key="fmp-secret-test-key" if include_fmp_key else None,
        credential_owner="user_byok",
        account_id="acct-1",
        transport=fmp_router,
        limiter=open_limiter(),
        policy=policy,
        now_fn=lambda: NOW,
    )
    ext_service = ExternalEarningsService(
        provider=fmp_adapter,
        byok_resolver=(lambda _acct, _pid: "fmp-secret-test-key") if include_fmp_key else None,
        account_id="acct-1",
        policy=policy,
    )
    deps = PrefetchDependencies(
        sec_adapter=sec_adapter,
        external_earnings=ext_service,
        market_price_fn=lambda _eid, _sym: Decimal("140.00"),
    )
    return deps, sec_router, fmp_router


def _default_dispatch_plan(l2_pack: str | None = "kpi_saas_cloud") -> ResearchDispatchPlan:
    return ResearchDispatchPlan(
        selected_l2_pack_id=l2_pack,
        selected_l2_pack_reason="Matched SIC 7372 SaaS/Cloud.",
        tasks=[
            WorkerTask(
                task_id="task_seg",
                worker_role="worker_segments",
                objective="Extract segment revenue & RPO/NRR KPIs from R10.htm and 8-K.",
                target_r_files=["R10.htm"],
            ),
            WorkerTask(
                task_id="task_for",
                worker_role="worker_forensics",
                objective="Investigate AR divergence and SBC/FCF flags + Risk Factor redline.",
                target_flag_ids=["working_capital_ar_vs_revenue_growth_pp", "earnings_quality_sbc_to_fcf_pct"],
            ),
            WorkerTask(
                task_id="task_gui",
                worker_role="worker_guidance",
                objective="Compare Q4 actuals vs prior guidance/consensus and check Q&A.",
            ),
        ],
        cross_worker_hypotheses=["Verify whether AR surge aligns with multi-year RPO commentary."],
    )


def _sample_card(
    card_id: str,
    role: WorkerRole,
    *,
    source_id: str = "src_8k_release_0001234567",
    quotation: str = "Cloud Subscriptions grew 30.8% year-over-year to $850 million.",
    fact_id: str = "Q4_2025:rev",
) -> EvidenceCard:
    return EvidenceCard(
        card_id=card_id,
        worker_role=role,
        topic_tag="segment_and_quality",
        finding=f"{role} verified finding backed by {fact_id}.",
        supporting_fact_ids=[fact_id],
        citations=[EvidenceCitation(source_id=source_id, quotation=quotation, location="EX-99.1")],
        caveat="Single-quarter observation.",
    )


# --------------------------------------------------------------------------- 1. Strict Contracts & Gate
def test_strict_contracts_immutability_and_tool_scope_validation() -> None:
    snap = EarningsPrefetchSnapshot(
        entity_id="cik:0001234567",
        symbol="ACME",
        cik="0001234567",
        sic_code="7372",
        sector_applicability=resolve_method_pack_context("7372") and earnings_quality_diagnostics(
            EarningsQualityRequest(entity_id="cik:0001234567", sic="7372", current=_sample_periods()[0])
        ).sector_applicability,
        statements=_sample_periods(),
    )
    assert snap.snapshot_sha256
    with pytest.raises(ValidationError):
        snap.symbol = "OTHER"  # frozen immutable

    # ValuationAndImpliedExpectationsResult alias
    assert ValuationAndImpliedExpectationsResult.__name__ == "ValuationAndExpectationsResult"

    # Skipped task must provide skip_reason
    with pytest.raises(ValidationError):
        WorkerTask(worker_role="worker_guidance", status="skipped", objective="Check transcript", skip_reason="   ")

    # Worker cannot be granted out-of-scope or write tools
    with pytest.raises(ValidationError):
        WorkerTask(worker_role="worker_segments", objective="Read transcript", allowed_tools=["earnings_transcript"])
    with pytest.raises(ValidationError):
        WorkerTask(worker_role="worker_forensics", objective="Submit", allowed_tools=["submit_research"])

    # Duplicate worker roles rejected in ResearchDispatchPlan
    with pytest.raises(ValidationError):
        ResearchDispatchPlan(tasks=[
            WorkerTask(worker_role="worker_segments", objective="A"),
            WorkerTask(worker_role="worker_segments", objective="B"),
        ])

    # BearCaseItem rejects empty anchors
    with pytest.raises(ValidationError):
        BearCaseItem(
            thesis_challenge="Margins might decline.",
            anchored_evidence_ids=[],
            anchor_kind="flag",
            next_quarter_invalidation_trigger="Gross margin < 58%",
        )


def test_bear_case_and_citation_gates_reject_generic_or_unverified_items() -> None:
    periods = _sample_periods()
    diag = earnings_quality_diagnostics(
        EarningsQualityRequest(
            entity_id="cik:0001234567",
            symbol="ACME",
            sic="7372",
            current=periods[0],
            prior_qoq=periods[1],
            prior_yoy=periods[4],
        )
    )
    snap = EarningsPrefetchSnapshot(
        entity_id="cik:0001234567",
        symbol="ACME",
        cik="0001234567",
        sic_code="7372",
        sector_applicability=diag.sector_applicability,
        statements=periods,
        diagnostics=diag,
    )
    valid_flag_id = snap.flags[0].flag_id

    good_card = _sample_card("c_valid", "worker_segments", source_id="src_1", quotation="Cloud grew 30.8% YoY to $850M")
    bad_quote_card = _sample_card(
        "c_bad_quote",
        "worker_forensics",
        source_id="src_1",
        quotation="Fabricated quotation not present in source",
        fact_id="",
    )
    empty_prov_card = EvidenceCard(
        card_id="c_empty",
        worker_role="worker_guidance",
        topic_tag="ungrounded",
        finding="Unsupported claim",
        supporting_fact_ids=[],
        citations=[],
    )
    # Remove fact_id on bad_quote_card so dropping its unverified citation leaves zero provenance
    bad_quote_card = bad_quote_card.model_copy(update={"supporting_fact_ids": []})

    batches = [
        EvidenceCardBatch(task_id="t1", worker_role="worker_segments", cards=[good_card]),
        EvidenceCardBatch(task_id="t2", worker_role="worker_forensics", cards=[bad_quote_card]),
        EvidenceCardBatch(task_id="t3", worker_role="worker_guidance", cards=[empty_prov_card]),
    ]
    verified_cards, gate_report, gate_gaps = gate_evidence_cards(
        batches,
        snap,
        source_texts={"src_1": "In Q4 2025, Cloud grew 30.8% YoY to $850M with NRR 121%."},
        source_permissions={"src_1": {"store": True, "display": True, "model_input": True, "export": False}},
    )
    assert [c.card_id for c in verified_cards] == ["c_valid"]
    assert set(gate_report.dropped_card_ids) == {"c_bad_quote", "c_empty"}
    assert gate_report.exportable is False
    assert gate_report.non_exportable_sources == ["src_1"]
    assert len(gate_gaps) == 2

    raw_bears = [
        BearCaseItem(
            bear_id="bear_good",
            thesis_challenge=f"Receivables growth outpaced revenue per {valid_flag_id}, risking FCF conversion.",
            anchored_evidence_ids=[valid_flag_id],
            anchor_kind="flag",
            next_quarter_invalidation_trigger="AR YoY growth - Revenue YoY growth <= +5.0 pp in Q1 2026.",
        ),
        BearCaseItem(
            bear_id="bear_fake_anchor",
            thesis_challenge="Competition could compress SaaS margins over time.",
            anchored_evidence_ids=["nonexistent_anchor_999"],
            anchor_kind="flag",
            next_quarter_invalidation_trigger="Operating margin expands next quarter.",
        ),
        BearCaseItem(
            bear_id="bear_generic_platitude",
            thesis_challenge="Macroeconomic headwinds and market volatility may impact results.",
            anchored_evidence_ids=[valid_flag_id],
            anchor_kind="flag",
            next_quarter_invalidation_trigger="Macro stabilizes.",
        ),
        {"bear_id": "bear_missing_anchor", "thesis_challenge": "Generic bear", "anchored_evidence_ids": [], "anchor_kind": "flag", "next_quarter_invalidation_trigger": "N/A"},
    ]
    accepted, rejected, bear_gaps = gate_bear_cases(raw_bears, snap, verified_cards)
    assert [b.bear_id for b in accepted] == ["bear_good"]
    assert {r.bear_id for r in rejected} == {"bear_fake_anchor", "bear_generic_platitude", "bear_missing_anchor"}
    assert len(bear_gaps) == 3


# --------------------------------------------------------------------------- 2. 6-Stage End-to-End, Tools, Deep Report, Slots, SIC Gate
def test_full_6_stage_orchestration_with_real_stage0_and_on_demand_deep_report() -> None:
    deps, sec_router, fmp_router = _sec_and_fmp_deps(include_fmp_key=True)
    req = SquadPrefetchRequest(
        question="Analyze ACME Q4 2025 earnings quality, segments, redline risks, and Reverse-DCF.",
        cik="0001234567",
        symbol="ACME",
        period_label="Q4_2025",
        fiscal_year=2025,
        fiscal_quarter=4,
        period_financials=_sample_periods(),
        guidance_inputs=[
            GuidanceMetricInput(
                metric_name="Revenue",
                actual=MetricValue(value="1200", unit="USD millions", currency="USD"),
                prior_guidance={
                    "low": MetricValue(value="1170", unit="USD millions", currency="USD"),
                    "high": MetricValue(value="1190", unit="USD millions", currency="USD"),
                },
                consensus=MetricValue(value="1185", unit="USD millions", currency="USD"),
                new_guidance={
                    "low": MetricValue(value="1220", unit="USD millions", currency="USD"),
                    "high": MetricValue(value="1250", unit="USD millions", currency="USD"),
                },
            )
        ],
        nongaap_bridges=[
            NonGaapBridgeInput(
                metric_name="Operating Income",
                period_label="Q4_2025",
                gaap_value=MetricValue(value="260", unit="USD millions", currency="USD"),
                reported_non_gaap_value=MetricValue(value="350", unit="USD millions", currency="USD"),
                adjustments=[NonGaapAdjustmentItem(label="Stock-based compensation", amount="90")],
            )
        ],
    )

    seen_slots: list[tuple[str, str, str | None]] = []
    bear_prompts: list[str] = []
    writer_calls: list[str] = []
    tool_calls_log: list[tuple[str, str]] = []

    class FakeToolRunner:
        def call_tool(self, tool_name: str, arguments: dict[str, Any], *, worker_role: WorkerRole | None = None) -> dict[str, Any]:
            tool_calls_log.append((worker_role or "none", tool_name))
            return {"status": "ok", "tool": tool_name, "data": f"result_for_{tool_name}"}

    def model_handler(mreq: SlotModelRequest) -> SlotModelResponse:
        seen_slots.append((mreq.stage, mreq.slot, mreq.binding.model))
        if mreq.stage == "stage_1_plan":
            return SlotModelResponse(parsed=_default_dispatch_plan(), usage=ModelUsage(100, 50, 150))
        if mreq.stage == "stage_2_workers":
            if mreq.turn_index == 0:
                tname = {
                    "worker_segments": "sec_filing_tables",
                    "worker_forensics": "sec_fulltext_search",
                    "worker_guidance": "earnings_transcript",
                }[mreq.role]
                return SlotModelResponse(
                    tool_calls=(ModelToolCall(f"tc_{mreq.role}", tname, {"query": "test"}),),
                    usage=ModelUsage(40, 20, 60),
                )
            card = _sample_card(
                f"card_{mreq.role}",
                mreq.role,  # type: ignore[arg-type]
                source_id="src_8k_release_0001234567",
                quotation="Cloud Subscriptions grew 30.8% year-over-year to $850 million.",
            )
            return SlotModelResponse(
                parsed=EvidenceCardBatch(task_id="t", worker_role=mreq.role, cards=[card]),  # type: ignore[arg-type]
                usage=ModelUsage(50, 40, 90),
            )
        if mreq.stage == "stage_2_5_review":
            return SlotModelResponse(
                parsed=Stage25ReviewDecision(has_cross_worker_leads=False, rationale="All leads covered."),
                usage=ModelUsage(30, 15, 45),
            )
        if mreq.stage == "stage_3a_bear":
            bear_prompts.append(mreq.system_prompt + "\n" + mreq.user_prompt)
            assert mreq.tools == []
            parsed = json.loads(mreq.user_prompt)
            flag_id = parsed["diagnostic_flags"][0]["flag_id"]
            return SlotModelResponse(
                parsed=BearCaseBatch(items=[
                    BearCaseItem(
                        bear_id="bear_1",
                        thesis_challenge=f"Flag {flag_id} indicates deteriorating working capital / SBC burden.",
                        anchored_evidence_ids=[flag_id, "card_worker_forensics"],
                        anchor_kind="flag",
                        next_quarter_invalidation_trigger="FCF ex-SBC margin > 10% and AR growth <= revenue growth + 5pp.",
                        supporting_numbers=["90", "600"],
                    ),
                    BearCaseItem(
                        bear_id="bear_2",
                        thesis_challenge="Item 1A Risk Factors newly warned on extended enterprise payment terms.",
                        anchored_evidence_ids=["redline:item_1a_risk_factors"],
                        anchor_kind="redline",
                        next_quarter_invalidation_trigger="DSO normalizes below 140 days.",
                    ),
                ]),
                usage=ModelUsage(80, 60, 140),
            )
        if mreq.stage == "stage_3b_gate_memo":
            writer_calls.append("memo")
            assert mreq.tools == []
            return SlotModelResponse(
                content="# Memo: ACME Q4 2025\nRevenue $1,200M (+20% YoY); AR divergence flagged.",
                usage=ModelUsage(90, 70, 160),
            )
        if mreq.stage == "deep_report":
            writer_calls.append("deep_report")
            assert mreq.tools == []
            return SlotModelResponse(
                content="# Deep Report: ACME Q4 2025\nFull tables, Reverse-DCF, and anchored bear case.",
                usage=ModelUsage(120, 110, 230),
            )
        raise AssertionError(f"Unexpected stage {mreq.stage}")

    events: list[OrgGraphEvent] = []
    ctx = _make_context()
    orch = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(model_handler),
        tool_runner=FakeToolRunner(),
        prefetch_deps=deps,
        primary_profile={"model": "primary-reasoner", "native_tools": True},
        slot_overrides={
            "fast_extractor": {"model": "fast-worker-model", "native_tools": True},
            "bear_reviewer": {"model": "cross-family-bear-model", "native_tools": False},
        },
        org_sinks=[events.append],
        context=ctx,
    )

    dossier = orch.run(req, run_id="run-e2e-1")
    assert isinstance(dossier, EarningsDossier)
    assert dossier.locked is True
    assert dossier.deep_report_markdown is None
    assert "ACME Q4 2025" in dossier.memo_markdown
    assert dossier.snapshot.earnings_release is not None
    assert dossier.snapshot.filing_table_catalog is not None
    assert dossier.snapshot.redline_summary is not None
    assert dossier.snapshot.redline_summary.total_added + dossier.snapshot.redline_summary.total_revised >= 1
    assert dossier.snapshot.consensus_summary.status == "available"
    assert dossier.snapshot.transcript_index.status == "available"
    assert dossier.snapshot.valuation is not None
    assert dossier.snapshot.valuation.reverse_dcf.status == "computed"
    assert len(dossier.evidence_cards) == 3
    assert len(dossier.bear_cases) == 2
    assert dossier.gate_report.exportable is False  # FMP BYOK has export=False
    assert dossier.slot_usage_summary.stage_0_model_calls == 0
    assert dossier.slot_usage_summary.slots["fast_extractor"].model == "fast-worker-model"
    assert dossier.slot_usage_summary.slots["fast_extractor"].fallback_used is False
    assert dossier.slot_usage_summary.slots["deep_reasoner"].model == "primary-reasoner"
    assert dossier.slot_usage_summary.slots["deep_reasoner"].fallback_used is True
    assert dossier.slot_usage_summary.slots["bear_reviewer"].model == "cross-family-bear-model"
    assert ctx.calls == dossier.slot_usage_summary.total_calls

    # Bear Reviewer isolated prompt: NO bull narrative or memo
    assert len(bear_prompts) == 1
    assert "Memo: ACME" not in bear_prompts[0]
    assert "cross_worker_hypotheses" not in bear_prompts[0]

    # Immutable dossier rejects mutation
    with pytest.raises(ValidationError):
        dossier.memo_markdown = "tampered"  # type: ignore[misc]

    # On-demand Deep Report generates once and caches on repeated calls (0 tool calls)
    dossier_deep = orch.render_deep_report(dossier)
    assert dossier_deep.deep_report_markdown is not None
    assert "Deep Report: ACME Q4 2025" in dossier_deep.deep_report_markdown
    assert dossier_deep.dossier_id == dossier.dossier_id
    assert dossier_deep.dossier_sha256 == dossier.dossier_sha256
    assert writer_calls == ["memo", "deep_report"]

    # Calling again on either dossier_deep OR original dossier uses cache (0 extra writer calls)
    dossier_deep_2 = orch.render_deep_report(dossier_deep)
    dossier_deep_3 = orch.render_deep_report(dossier)
    assert dossier_deep_2.deep_report_markdown == dossier_deep.deep_report_markdown
    assert dossier_deep_3.deep_report_markdown == dossier_deep.deep_report_markdown
    assert writer_calls == ["memo", "deep_report"]
    assert ("writer", "sec_filing_tables") not in tool_calls_log
    assert sec_router.calls and fmp_router.calls

    # OrgGraphEvent strict monotonic sequencing 1..N
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    assert any(e.action_kind == "DELEGATING" and e.edge_event and not e.edge_event.is_review for e in events)
    assert any(e.action_kind == "WAITING_SUBORDINATES" for e in events)
    assert any(e.action_kind == "SEARCHING" and e.node_id == "worker_forensics" for e in events)
    assert any(e.action_kind == "READING" and e.node_id == "worker_segments" for e in events)
    assert any(e.edge_event and e.edge_event.is_review and e.edge_event.to_node_id == "lead_coordinator" for e in events)
    assert any(e.edge_event and e.edge_event.is_review and e.edge_event.to_node_id == "bear_reviewer" for e in events)


def test_stage2_parallel_execution_tool_isolation_turn_limit_and_partial_survival() -> None:
    periods = _sample_periods()
    diag = earnings_quality_diagnostics(EarningsQualityRequest(entity_id="cik:1", sic="7372", current=periods[0], prior_yoy=periods[4]))
    snap = EarningsPrefetchSnapshot(
        entity_id="cik:1",
        symbol="ACME",
        cik="0000000001",
        sic_code="7372",
        sector_applicability=diag.sector_applicability,
        statements=periods,
        diagnostics=diag,
    )

    barrier = Barrier(2, timeout=2.0)
    executed_tools: list[tuple[str, str]] = []
    events: list[OrgGraphEvent] = []

    class RecordingToolRunner:
        def call_tool(self, tool_name: str, arguments: dict[str, Any], *, worker_role: WorkerRole | None = None) -> dict[str, Any]:
            executed_tools.append((worker_role or "", tool_name))
            return {"status": "unavailable", "reason": "byok_key_required", "message": "BYOK key needed."}

    def model_fn(mreq: SlotModelRequest) -> SlotModelResponse:
        if mreq.stage == "stage_1_plan":
            return SlotModelResponse(parsed=ResearchDispatchPlan(
                selected_l2_pack_id="kpi_saas_cloud",
                tasks=[
                    WorkerTask(worker_role="worker_segments", objective="Segments", soft_turn_limit=2),
                    WorkerTask(worker_role="worker_forensics", objective="Forensics", soft_turn_limit=2),
                    WorkerTask(worker_role="worker_guidance", objective="Guidance", soft_turn_limit=4),
                ],
            ))
        if mreq.stage == "stage_2_workers":
            if mreq.role == "worker_guidance":
                raise RuntimeError("FMP upstream 504 timeout")
            if mreq.role == "worker_segments":
                barrier.wait()  # Proves worker_segments and worker_forensics truly execute in parallel!
                if mreq.turn_index == 0:
                    # Attempt an unauthorized tool (earnings_transcript) + disallowed write tool (submit_research)
                    return SlotModelResponse(tool_calls=(
                        ModelToolCall("tc1", "earnings_transcript", {}),
                        ModelToolCall("tc2", "submit_research", {}),
                    ))
                return SlotModelResponse(parsed=EvidenceCardBatch(
                    task_id="t",
                    worker_role="worker_segments",
                    cards=[_sample_card("c_seg", "worker_segments", quotation="x")],
                ))
            if mreq.role == "worker_forensics":
                barrier.wait()
                # Runaway worker that keeps asking for sec_fulltext_search until soft_turn_limit=2 stops it!
                return SlotModelResponse(tool_calls=(
                    ModelToolCall(f"tc_f_{mreq.turn_index}", "sec_fulltext_search", {"query": "tariff"}),
                ))
        if mreq.stage == "stage_2_5_review":
            return SlotModelResponse(parsed=Stage25ReviewDecision(has_cross_worker_leads=False))
        if mreq.stage == "stage_3a_bear":
            return SlotModelResponse(parsed=BearCaseBatch(items=[
                BearCaseItem(
                    bear_id="b1",
                    thesis_challenge="AR surge flagged with >15pp divergence.",
                    anchored_evidence_ids=["c_seg"],
                    anchor_kind="flag",
                    next_quarter_invalidation_trigger="AR normalized.",
                )
            ]))
        if mreq.stage == "stage_3b_gate_memo":
            return SlotModelResponse(content="Memo with partial survival.")
        raise AssertionError(mreq.stage)

    orch = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(model_fn),
        tool_runner=RecordingToolRunner(),
        prefetch_deps=PrefetchDependencies(precomputed_snapshot=snap),
        org_sinks=[events.append],
    )
    dossier = orch.run(SquadPrefetchRequest(question="Test parallel + partial survival", symbol="ACME"))

    # Unauthorized tools were NEVER executed by tool_runner
    assert ("worker_segments", "earnings_transcript") not in executed_tools
    assert ("worker_segments", "submit_research") not in executed_tools
    # worker_segments survived and emitted its card; worker_forensics hit soft_turn_limit=2 cleanly; worker_guidance failed cleanly
    assert [c.card_id for c in dossier.evidence_cards] == ["c_seg"]
    gap_codes = {g.code for g in dossier.open_gaps}
    assert "worker_failed" in gap_codes
    assert "worker_partial_gap" in gap_codes
    assert any(e.action_kind == "BLOCKED" and e.node_id == "worker_segments" for e in events)
    assert any(e.action_kind == "FAILED" and e.node_id == "worker_guidance" for e in events)
    assert any(e.action_kind == "NEEDS_USER" and e.node_id == "worker_forensics" for e in events)


def test_stage_2_5_hard_cap_one_followup_round() -> None:
    periods = _sample_periods()
    diag = earnings_quality_diagnostics(EarningsQualityRequest(entity_id="cik:1", sic="7372", current=periods[0]))
    snap = EarningsPrefetchSnapshot(
        entity_id="cik:1",
        symbol="ACME",
        cik="0000000001",
        sic_code="7372",
        sector_applicability=diag.sector_applicability,
        statements=periods,
        diagnostics=diag,
    )
    round_calls: list[tuple[str, int]] = []

    def model_fn(mreq: SlotModelRequest) -> SlotModelResponse:
        if mreq.stage == "stage_1_plan":
            return SlotModelResponse(parsed=ResearchDispatchPlan(
                tasks=[WorkerTask(worker_role="worker_segments", objective="Initial seg")]
            ))
        if mreq.stage == "stage_2_workers":
            round_calls.append((mreq.role, 1))
            return SlotModelResponse(parsed=EvidenceCardBatch(
                task_id="t1",
                worker_role="worker_segments",
                cards=[_sample_card("c_r1", "worker_segments")],
            ))
        if mreq.stage == "stage_2_5_review" and mreq.role == "lead_coordinator":
            round_calls.append(("stage_2_5_lead", 1))
            # Even if Lead returns followup_tasks, only 1 round executes
            return SlotModelResponse(parsed=Stage25ReviewDecision(
                has_cross_worker_leads=True,
                rationale="Verify deferred revenue footnote.",
                followup_tasks=[WorkerTask(worker_role="worker_forensics", objective="Check footnote", round_number=2)],
            ))
        if mreq.role == "worker_forensics":
            round_calls.append((mreq.role, 2))
            return SlotModelResponse(parsed=EvidenceCardBatch(
                task_id="t2",
                worker_role="worker_forensics",
                round_number=2,
                cards=[_sample_card("c_r2", "worker_forensics")],
            ))
        if mreq.stage == "stage_3a_bear":
            return SlotModelResponse(parsed=BearCaseBatch(items=[]))
        if mreq.stage == "stage_3b_gate_memo":
            return SlotModelResponse(content="Memo")
        raise AssertionError(f"Unexpected {mreq.stage} / {mreq.role}")

    orch = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(model_fn),
        prefetch_deps=PrefetchDependencies(precomputed_snapshot=snap),
    )
    dossier = orch.run(SquadPrefetchRequest(question="Followup cap test"))
    assert round_calls.count(("stage_2_5_lead", 1)) == 1
    assert round_calls.count(("worker_forensics", 2)) == 1
    assert {c.card_id for c in dossier.evidence_cards} == {"c_r1", "c_r2"}


def test_slot_validation_bank_sic_gating_and_deep_report_tool_violation() -> None:
    # fast_extractor without native_tools fails closed
    with pytest.raises(SquadConfigurationError):
        resolve_squad_slots(
            primary_profile={"model": "m1", "native_tools": True},
            slot_overrides={"fast_extractor": {"model": "m2", "native_tools": False}},
        )

    # Bank SIC 6021 propagates disabled metrics & skips L2 non-financial pack
    bank_period = _period("Q4_2025", revenue="5000", cogs="0", op_inc="1800", ni="1200", ocf="1400", capex="50", sbc="100", ar="0", inv="0", ap="0", cash="8000", debt="10000", shares="3000", ebitda="2000")
    deps = PrefetchDependencies()
    req = SquadPrefetchRequest(
        question="Analyze JPM Q4 2025 bank earnings",
        entity_id="cik:0000019617",
        cik="0000019617",
        symbol="JPM",
        sic_code="6021",
        sic_description="National Commercial Banks",
        period_financials=[bank_period],
        close_price="220",
    )

    def model_fn(mreq: SlotModelRequest) -> SlotModelResponse:
        if mreq.stage == "stage_1_plan":
            payload = json.loads(mreq.user_prompt)
            assert payload["method_pack_hint"]["suggested_l2_pack_id"] is None
            assert payload["snapshot_summary"]["sector_applicability"]["sector_category"] == "bank"
            return SlotModelResponse(parsed=ResearchDispatchPlan(
                selected_l2_pack_id=None,
                tasks=[
                    WorkerTask(worker_role="worker_forensics", objective="Inspect NIM and CET1 instead of FCF"),
                    WorkerTask(worker_role="worker_guidance", status="skipped", objective="Transcript", skip_reason="No FMP key configured"),
                ],
            ))
        if mreq.stage == "stage_2_workers":
            return SlotModelResponse(parsed=EvidenceCardBatch(
                task_id="t1",
                worker_role="worker_forensics",
                cards=[_sample_card("c_bank", "worker_forensics", quotation="NIM", fact_id="Q4_2025:rev")],
            ))
        if mreq.stage == "stage_2_5_review":
            return SlotModelResponse(parsed=Stage25ReviewDecision(has_cross_worker_leads=False))
        if mreq.stage == "stage_3a_bear":
            return SlotModelResponse(parsed=BearCaseBatch(items=[]))
        if mreq.stage == "stage_3b_gate_memo":
            return SlotModelResponse(content="Bank Memo: Non-financial metrics disabled.")
        if mreq.stage == "deep_report":
            # Attempt a retrieval tool call in deep_report -> MUST fail closed!
            return SlotModelResponse(tool_calls=(ModelToolCall("bad_tc", "sec_filing_tables", {}),))
        raise AssertionError(mreq.stage)

    orch = EarningsSquadOrchestrator(model_runner=CallableSlotModelRunner(model_fn), prefetch_deps=deps)
    dossier = orch.run(req)
    assert dossier.sector_applicability.is_gated is True
    assert dossier.sector_applicability.sector_category == "bank"
    assert dossier.selected_l2_pack_id is None
    assert any(g.code == "worker_skipped" for g in dossier.open_gaps)

    with pytest.raises(SquadToolViolationError):
        render_deep_report(dossier, model_runner=CallableSlotModelRunner(model_fn))


# --------------------------------------------------------------------------- 3. Subtask-level Checkpoint Save & Resume
def test_subtask_checkpoint_save_and_resume_skips_completed_stages_and_workers(tmp_path: Any) -> None:
    deps, sec_router, fmp_router = _sec_and_fmp_deps(include_fmp_key=True)
    file_store = JsonFileSquadCheckpointStore(tmp_path / "checkpoints")
    run_id = "run-cp-resume-1"

    req = SquadPrefetchRequest(
        question="Resume test",
        cik="0001234567",
        symbol="ACME",
        period_financials=_sample_periods(),
    )

    calls_phase_1: list[str] = []

    def runner_phase_1(mreq: SlotModelRequest) -> SlotModelResponse:
        calls_phase_1.append(f"{mreq.stage}:{mreq.role}")
        if mreq.stage == "stage_1_plan":
            return SlotModelResponse(parsed=_default_dispatch_plan())
        if mreq.stage == "stage_2_workers":
            if mreq.role == "worker_guidance":
                # Simulate transient crash inside worker_guidance
                raise RuntimeError("Simulated crash")
            return SlotModelResponse(parsed=EvidenceCardBatch(
                task_id="t",
                worker_role=mreq.role,  # type: ignore[arg-type]
                cards=[_sample_card(f"card_{mreq.role}_v1", mreq.role)],  # type: ignore[arg-type]
            ))
        if mreq.stage == "stage_2_5_review":
            return SlotModelResponse(parsed=Stage25ReviewDecision(has_cross_worker_leads=False))
        if mreq.stage == "stage_3a_bear":
            # Simulate crash right at Stage 3A after Stage 2 saved partial failure
            raise SquadCancelledError("Interrupted before Stage 3A")
        raise AssertionError(mreq.stage)

    orch_1 = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(runner_phase_1),
        prefetch_deps=deps,
        checkpoint_store=file_store,
    )
    with pytest.raises(SquadCancelledError):
        orch_1.run(req, run_id=run_id)

    saved_cp = file_store.load(run_id)
    assert saved_cp is not None
    assert "stage_0" in saved_cp.completed_stages
    assert "stage_1" in saved_cp.completed_stages
    assert "stage_2" in saved_cp.completed_stages
    assert saved_cp.worker_batches["round1:worker_segments"].status == "completed"
    assert saved_cp.worker_batches["round1:worker_forensics"].status == "completed"
    assert saved_cp.worker_batches["round1:worker_guidance"].status == "failed"

    # Now mutate checkpoint to simulate interruption mid-Stage 2 (stage_2 NOT yet marked completed,
    # worker_segments & worker_forensics completed, worker_guidance pending/failed).
    mid_stage2_cp = saved_cp.model_copy(update={
        "completed_stages": ["stage_0", "stage_1"],
        "stage_status": {"stage_0": "completed", "stage_1": "completed"},
    })
    file_store.save(mid_stage2_cp)

    sec_calls_before_resume = len(sec_router.calls)
    fmp_calls_before_resume = len(fmp_router.calls)
    calls_phase_2: list[str] = []

    def runner_phase_2(mreq: SlotModelRequest) -> SlotModelResponse:
        calls_phase_2.append(f"{mreq.stage}:{mreq.role}")
        if mreq.stage == "stage_2_workers":
            assert mreq.role == "worker_guidance", "Already-completed workers must NOT be re-run!"
            return SlotModelResponse(parsed=EvidenceCardBatch(
                task_id="t_gui",
                worker_role="worker_guidance",
                cards=[_sample_card("card_worker_guidance_resumed", "worker_guidance")],
            ))
        if mreq.stage == "stage_2_5_review":
            return SlotModelResponse(parsed=Stage25ReviewDecision(has_cross_worker_leads=False))
        if mreq.stage == "stage_3a_bear":
            return SlotModelResponse(parsed=BearCaseBatch(items=[
                BearCaseItem(
                    bear_id="bear_resumed",
                    thesis_challenge="AR divergence in Stage 0 snapshot.",
                    anchored_evidence_ids=["card_worker_segments_v1"],
                    anchor_kind="flag",
                    next_quarter_invalidation_trigger="AR <= Rev growth + 5pp.",
                )
            ]))
        if mreq.stage == "stage_3b_gate_memo":
            return SlotModelResponse(content="Resumed Memo complete.")
        raise AssertionError(f"Unexpected call on resume: {mreq.stage}:{mreq.role}")

    orch_2 = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(runner_phase_2),
        prefetch_deps=deps,
        checkpoint_store=file_store,
    )
    resumed_dossier = orch_2.run(req, resume_from=run_id)

    # Zero external Stage 0 API calls and zero Stage 1 / completed worker calls on resume!
    assert len(sec_router.calls) == sec_calls_before_resume
    assert len(fmp_router.calls) == fmp_calls_before_resume
    assert "stage_1_plan:lead_coordinator" not in calls_phase_2
    assert "stage_2_workers:worker_segments" not in calls_phase_2
    assert "stage_2_workers:worker_forensics" not in calls_phase_2
    assert "stage_2_workers:worker_guidance" in calls_phase_2
    assert {c.card_id for c in resumed_dossier.evidence_cards} == {
        "card_worker_segments_v1",
        "card_worker_forensics_v1",
        "card_worker_guidance_resumed",
    }


# --------------------------------------------------------------------------- 4. Lead-Directed Mid-Run Steering
def test_mid_run_steering_cancels_and_redispatches_only_affected_worker() -> None:
    periods = _sample_periods()
    diag = earnings_quality_diagnostics(EarningsQualityRequest(entity_id="cik:1", sic="7372", current=periods[0]))
    snap = EarningsPrefetchSnapshot(
        entity_id="cik:1",
        symbol="ACME",
        cik="0000000001",
        sic_code="7372",
        sector_applicability=diag.sector_applicability,
        statements=periods,
        diagnostics=diag,
    )

    worker_exec_counts: dict[str, int] = {"worker_segments": 0, "worker_forensics": 0, "worker_guidance": 0}
    forensics_started_turn0 = Event()
    steering_cancelled_seen = Event()
    lock = Lock()
    events: list[OrgGraphEvent] = []

    def track_event(ev: OrgGraphEvent) -> None:
        events.append(ev)
        if ev.stage == "steering" and ev.node_id == "worker_forensics" and ev.runtime_state == "cancelled":
            steering_cancelled_seen.set()

    class SlowForensicsToolRunner:
        def call_tool(self, tool_name: str, arguments: dict[str, Any], *, worker_role: WorkerRole | None = None) -> dict[str, Any]:
            if worker_role == "worker_forensics":
                # Stay in-flight until Lead emits cancellation event so cancellation is deterministically exercised
                steering_cancelled_seen.wait(timeout=2.0)
            return {"status": "ok", "tool": tool_name}

    orch_holder: dict[str, EarningsSquadOrchestrator] = {}

    def model_fn(mreq: SlotModelRequest) -> SlotModelResponse:
        if mreq.stage == "stage_1_plan":
            return SlotModelResponse(parsed=_default_dispatch_plan())
        if mreq.stage == "stage_2_workers":
            if mreq.role in ("worker_segments", "worker_guidance"):
                with lock:
                    worker_exec_counts[mreq.role] += 1
                return SlotModelResponse(parsed=EvidenceCardBatch(
                    task_id="t",
                    worker_role=mreq.role,  # type: ignore[arg-type]
                    cards=[_sample_card(f"card_{mreq.role}", mreq.role)],  # type: ignore[arg-type]
                ))
            if mreq.role == "worker_forensics":
                steering_seq = None
                if mreq.turn_index == 0:
                    try:
                        steering_seq = json.loads(mreq.user_prompt).get("task", {}).get("steering_instruction_sequence", 0)
                    except ValueError:
                        steering_seq = None
                if mreq.turn_index == 0 and steering_seq == 0:
                    with lock:
                        worker_exec_counts["worker_forensics"] += 1
                    forensics_started_turn0.set()
                    return SlotModelResponse(tool_calls=(ModelToolCall("tc_f", "sec_redline_diff", {}),))
                with lock:
                    worker_exec_counts["worker_forensics"] += 1
                return SlotModelResponse(parsed=EvidenceCardBatch(
                    task_id="t",
                    worker_role="worker_forensics",
                    cards=[_sample_card("card_forensics_steered", "worker_forensics")],
                ))
        if mreq.stage == "steering":
            return SlotModelResponse(parsed=SteeringImpactDecision(
                affected_workers=["worker_forensics"],
                cancel_running_workers=["worker_forensics"],
                updated_tasks=[WorkerTask(
                    worker_role="worker_forensics",
                    objective="Steered: inspect lease liabilities and redline.",
                    steering_instruction_sequence=1,
                )],
                rationale="User requested deep check on lease footnotes; segments and guidance unaffected.",
            ))
        if mreq.stage == "stage_2_5_review":
            return SlotModelResponse(parsed=Stage25ReviewDecision(has_cross_worker_leads=False))
        if mreq.stage == "stage_3a_bear":
            return SlotModelResponse(parsed=BearCaseBatch(items=[
                BearCaseItem(
                    bear_id="bear_steered",
                    thesis_challenge="Steered lease liability & forensics evidence.",
                    anchored_evidence_ids=["card_forensics_steered"],
                    anchor_kind="redline",
                    next_quarter_invalidation_trigger="Lease footnote debt reduced.",
                )
            ]))
        if mreq.stage == "stage_3b_gate_memo":
            return SlotModelResponse(content="Steered Memo.")
        raise AssertionError(f"Unexpected {mreq.stage}:{mreq.role}")

    steering_submitted = Event()

    def on_worker_done(role: WorkerRole, _batch: EvidenceCardBatch, _cp: SquadCheckpoint) -> None:
        if role in ("worker_segments", "worker_guidance") and not steering_submitted.is_set():
            steering_submitted.set()
            forensics_started_turn0.wait(timeout=1.5)
            orch_holder["orch"].submit_steering_instruction("Check lease footnotes in forensics!")

    orch = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(model_fn),
        tool_runner=SlowForensicsToolRunner(),
        prefetch_deps=PrefetchDependencies(precomputed_snapshot=snap),
        org_sinks=[track_event],
    )
    orch_holder["orch"] = orch

    dossier = orch.run(req := SquadPrefetchRequest(question="Mid-run steering test"), on_worker_complete=on_worker_done)
    assert req.question == "Mid-run steering test"
    # Unrelated completed workers ran ONLY once!
    assert worker_exec_counts["worker_segments"] == 1
    assert worker_exec_counts["worker_guidance"] == 1
    # Only worker_forensics was cancelled and re-dispatched
    assert worker_exec_counts["worker_forensics"] == 2
    assert {c.card_id for c in dossier.evidence_cards} == {
        "card_worker_segments",
        "card_worker_guidance",
        "card_forensics_steered",
    }
    assert any(e.stage == "steering" and e.node_id == "worker_forensics" and e.runtime_state == "cancelled" for e in events)
    assert any(e.stage == "steering" and e.edge_event and e.edge_event.edge_kind == "steering" and e.edge_event.to_node_id == "worker_forensics" for e in events)


def test_post_stage2_steering_on_resume_reruns_only_affected_worker() -> None:
    periods = _sample_periods()
    diag = earnings_quality_diagnostics(EarningsQualityRequest(entity_id="cik:1", sic="7372", current=periods[0]))
    snap = EarningsPrefetchSnapshot(
        entity_id="cik:1",
        symbol="ACME",
        cik="0000000001",
        sic_code="7372",
        sector_applicability=diag.sector_applicability,
        statements=periods,
        diagnostics=diag,
    )
    store = InMemorySquadCheckpointStore()
    run_id = "run-post-steer"

    def initial_model(mreq: SlotModelRequest) -> SlotModelResponse:
        if mreq.stage == "stage_1_plan":
            return SlotModelResponse(parsed=_default_dispatch_plan())
        if mreq.stage == "stage_2_workers":
            return SlotModelResponse(parsed=EvidenceCardBatch(
                task_id="t", worker_role=mreq.role, cards=[_sample_card(f"c_{mreq.role}_1", mreq.role)]  # type: ignore[arg-type]
            ))
        if mreq.stage == "stage_2_5_review":
            return SlotModelResponse(parsed=Stage25ReviewDecision(has_cross_worker_leads=False))
        if mreq.stage == "stage_3a_bear":
            return SlotModelResponse(parsed=BearCaseBatch(items=[]))
        if mreq.stage == "stage_3b_gate_memo":
            return SlotModelResponse(content="Initial Memo")
        raise AssertionError(mreq.stage)

    orch1 = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(initial_model),
        prefetch_deps=PrefetchDependencies(precomputed_snapshot=snap),
        checkpoint_store=store,
    )
    orch1.run(SquadPrefetchRequest(question="Initial"), run_id=run_id)
    cp = store.load(run_id)
    assert cp is not None

    rerun_roles: list[str] = []

    def steered_model(mreq: SlotModelRequest) -> SlotModelResponse:
        if mreq.stage == "steering":
            return SlotModelResponse(parsed=SteeringImpactDecision(
                affected_workers=["worker_guidance"],
                updated_tasks=[WorkerTask(
                    worker_role="worker_guidance",
                    objective="Re-check guidance after new instruction.",
                    steering_instruction_sequence=1,
                )],
            ))
        if mreq.stage == "stage_2_workers":
            rerun_roles.append(mreq.role)
            return SlotModelResponse(parsed=EvidenceCardBatch(
                task_id="t",
                worker_role="worker_guidance",
                cards=[_sample_card("c_worker_guidance_2", "worker_guidance")],
            ))
        if mreq.stage == "stage_2_5_review":
            return SlotModelResponse(parsed=Stage25ReviewDecision(has_cross_worker_leads=False))
        if mreq.stage == "stage_3a_bear":
            return SlotModelResponse(parsed=BearCaseBatch(items=[]))
        if mreq.stage == "stage_3b_gate_memo":
            return SlotModelResponse(content="Updated Steered Memo")
        raise AssertionError(mreq.stage)

    orch2 = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(steered_model),
        prefetch_deps=PrefetchDependencies(precomputed_snapshot=snap),
        checkpoint_store=store,
    )
    dossier2 = orch2.run(
        SquadPrefetchRequest(question="Initial"),
        resume_from=cp,
        user_instructions=[UserSteeringInstruction(sequence=1, text="Also verify Q1 guidance midpoint.")],
    )
    assert rerun_roles == ["worker_guidance"]
    assert {c.card_id for c in dossier2.evidence_cards} == {
        "c_worker_segments_1",
        "c_worker_forensics_1",
        "c_worker_guidance_2",
    }
    assert "Updated Steered Memo" in dossier2.memo_markdown


# --------------------------------------------------------------------------- 5. Thread-Safe Budget, Cancellation, Async & Org Graph
def test_budget_enforcement_cancellation_and_async_execution() -> None:
    periods = _sample_periods()
    diag = earnings_quality_diagnostics(EarningsQualityRequest(entity_id="cik:1", sic="7372", current=periods[0]))
    snap = EarningsPrefetchSnapshot(
        entity_id="cik:1",
        symbol="ACME",
        cik="0000000001",
        sic_code="7372",
        sector_applicability=diag.sector_applicability,
        statements=periods,
        diagnostics=diag,
    )

    def ok_model(mreq: SlotModelRequest) -> SlotModelResponse:
        if mreq.stage == "stage_1_plan":
            return SlotModelResponse(parsed=ResearchDispatchPlan(
                tasks=[WorkerTask(worker_role="worker_segments", objective="Segments")]
            ), usage=ModelUsage(10, 10, 20))
        if mreq.stage == "stage_2_workers":
            return SlotModelResponse(parsed=EvidenceCardBatch(
                task_id="t", worker_role="worker_segments", cards=[_sample_card("c1", "worker_segments")]
            ), usage=ModelUsage(10, 10, 20))
        if mreq.stage == "stage_2_5_review":
            return SlotModelResponse(parsed=Stage25ReviewDecision(has_cross_worker_leads=False), usage=ModelUsage(10, 10, 20))
        if mreq.stage == "stage_3a_bear":
            return SlotModelResponse(parsed=BearCaseBatch(items=[]), usage=ModelUsage(10, 10, 20))
        if mreq.stage == "stage_3b_gate_memo":
            return SlotModelResponse(content="Async Memo", usage=ModelUsage(10, 10, 20))
        if mreq.stage == "deep_report":
            return SlotModelResponse(content="Async Deep Report", usage=ModelUsage(10, 10, 20))
        raise AssertionError(mreq.stage)

    # Async run + render_deep_report_async
    orch_async = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(ok_model),
        prefetch_deps=PrefetchDependencies(precomputed_snapshot=snap),
    )

    async def _run_both() -> EarningsDossier:
        d = await orch_async.run_async(SquadPrefetchRequest(question="Async test"))
        return await orch_async.render_deep_report_async(d)

    dossier_async = asyncio.run(_run_both())
    assert dossier_async.memo_markdown == "Async Memo"
    assert dossier_async.deep_report_markdown == "Async Deep Report"
    assert dossier_async.slot_usage_summary.total_calls == 5

    # Cancellation stops execution cleanly
    cancel_ev = Event()
    cancel_ev.set()
    orch_cancel = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(ok_model),
        prefetch_deps=PrefetchDependencies(precomputed_snapshot=snap),
        cancel_event=cancel_ev,
    )
    with pytest.raises(SquadCancelledError):
        orch_cancel.run(SquadPrefetchRequest(question="Cancel test"))

    # Model call budget cap enforced across slots
    tight_ctx = _make_context(max_calls=2)
    orch_budget = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(ok_model),
        prefetch_deps=PrefetchDependencies(precomputed_snapshot=snap),
        context=tight_ctx,
    )
    with pytest.raises(Exception):
        orch_budget.run(SquadPrefetchRequest(question="Budget exhaustion test"))


def test_integration_with_a2_model_slots_method_packs_and_fmp_degradation() -> None:
    # 1. Heterogeneous 3-protocol ResolvedModelSlots -> resolve_squad_slots
    resolved = resolve_model_slots(
        primary_profile={
            "protocol": "openai_chat_completions",
            "base_url": "https://api.primary.example/v1",
            "model": "reasoner-v1",
            "credential_ref": "acct_primary",
            "tools_verified": True,
        },
        slot_overrides={
            "fast_extractor": {
                "protocol": "openai_responses",
                "base_url": "https://api.fast.example/v1",
                "model": "fast-extractor-v1",
                "credential_ref": "acct_fast",
                "capabilities": {"native_tools": True, "mode": "json_object"},
            },
            "bear_reviewer": {
                "protocol": "anthropic_messages",
                "base_url": "https://api.bear.example",
                "model": "bear-cross-family",
                "credential_ref": "acct_bear",
            },
        },
    )
    squad_slots = resolve_squad_slots(resolved_slots=resolved)
    assert squad_slots["fast_extractor"].model == "fast-extractor-v1"
    assert squad_slots["fast_extractor"].protocol == "openai_responses"
    assert squad_slots["fast_extractor"].fallback_used is False
    assert squad_slots["bear_reviewer"].model == "bear-cross-family"
    assert squad_slots["bear_reviewer"].protocol == "anthropic_messages"
    assert squad_slots["deep_reasoner"].fallback_used is True
    assert squad_slots["writer"].fallback_used is True

    # Invalid fast_extractor (native_tools=False) via resolve_model_slots fails closed as SquadConfigurationError
    with pytest.raises(SquadConfigurationError):
        resolve_squad_slots(
            primary_profile={
                "protocol": "openai_chat_completions",
                "base_url": "https://api.primary.example/v1",
                "model": "reasoner-v1",
                "tools_verified": False,
            },
        )

    # 2. L1 + L2 method pack composition integration
    saas_ctx = resolve_method_pack_context("7372")
    assert saas_ctx["l2_pack_id"] == "kpi_saas_cloud"
    semis_ctx = resolve_method_pack_context("3674")
    assert semis_ctx["l2_pack_id"] == "kpi_semis_hardware"
    bank_ctx = resolve_method_pack_context("6021")
    assert bank_ctx["l2_pack_id"] is None
    composed_saas = compose_earnings_method_pack("7372")
    assert composed_saas.industry_pack_id == "kpi_saas_cloud"

    # 3. Stage 0 graceful degradation when FMP BYOK key is absent
    deps_no_fmp, _, _ = _sec_and_fmp_deps(include_fmp_key=False)
    orch = EarningsSquadOrchestrator(
        model_runner=DictSlotModelRunner({
            "lead_coordinator": lambda r: (
                _default_dispatch_plan() if r.stage == "stage_1_plan"
                else Stage25ReviewDecision(has_cross_worker_leads=False)
            ),
            "fast_extractor": lambda r: EvidenceCardBatch(
                task_id="t", worker_role=r.role, cards=[_sample_card(f"c_{r.role}", r.role)]  # type: ignore[arg-type]
            ),
            "bear_reviewer": lambda _r: BearCaseBatch(items=[]),
            "writer": lambda _r: "Degraded Memo without FMP.",
        }),
        prefetch_deps=deps_no_fmp,
        resolved_slots=resolved,
    )
    dossier = orch.run(SquadPrefetchRequest(
        question="ACME degraded FMP test",
        cik="0001234567",
        symbol="ACME",
        period_financials=_sample_periods(),
    ))
    assert dossier.snapshot.consensus_summary.status == "unavailable"
    assert dossier.snapshot.transcript_index.status == "unavailable"
    assert any(g.code.startswith("consensus_") for g in dossier.open_gaps)
    assert any(g.code.startswith("transcript_") for g in dossier.open_gaps)
    assert dossier.snapshot.earnings_release is not None
    assert dossier.gate_report.exportable is True  # Only SEC public sources present!


def test_fin_toolbox_tool_runner_and_org_graph_invariants() -> None:
    deps, sec_router, _ = _sec_and_fmp_deps(include_fmp_key=False)
    toolbox = make_toolbox(sec=sec_router, sec_stream=_stream_from_router(sec_router))
    runner = FinToolboxToolRunner(
        toolbox,
        entities={"cik:0001234567": {"company": "Acme Cloud Systems Inc.", "symbol": "ACME", "identifiers": {"cik": "0001234567"}}},
    )

    # Disallowed / out-of-scope tools fail closed inside FinToolboxToolRunner
    with pytest.raises(SquadToolViolationError):
        runner.call_tool("submit_research", {}, worker_role="worker_segments")
    with pytest.raises(SquadToolViolationError):
        runner.call_tool("earnings_transcript", {"symbol": "ACME"}, worker_role="worker_segments")

    events: list[OrgGraphEvent] = []

    def model_fn(mreq: SlotModelRequest) -> SlotModelResponse:
        if mreq.stage == "stage_1_plan":
            return SlotModelResponse(parsed=ResearchDispatchPlan(
                selected_l2_pack_id="kpi_saas_cloud",
                tasks=[WorkerTask(worker_role="worker_segments", objective="Fetch R10.htm and quote Cloud Subscriptions")],
            ))
        if mreq.stage == "stage_2_workers":
            if mreq.turn_index == 0:
                return SlotModelResponse(tool_calls=(
                    ModelToolCall("tc_r10", "sec_filing_tables", {
                        "cik": "0001234567",
                        "accession": "0001234567-25-000101",
                        "r_file": "R10.htm",
                    }),
                ))
            if mreq.turn_index == 1:
                last_tool = json.loads(mreq.messages[-1]["content"])
                sid = last_tool["source_id"]
                return SlotModelResponse(tool_calls=(
                    ModelToolCall("tc_search", "search_source", {"source_id": sid, "query": "Cloud Subscriptions"}),
                ))
            tool_msg = [m for m in mreq.messages if m.get("role") == "tool"]
            first_res = json.loads(tool_msg[0]["content"])
            sid = first_res["source_id"]
            return SlotModelResponse(parsed=EvidenceCardBatch(
                task_id="t",
                worker_role="worker_segments",
                cards=[_sample_card("c_r10", "worker_segments", source_id=sid, quotation="Cloud Subscriptions | $850 | $650")],
            ))
        if mreq.stage == "stage_2_5_review":
            return SlotModelResponse(parsed=Stage25ReviewDecision(has_cross_worker_leads=False))
        if mreq.stage == "stage_3a_bear":
            return SlotModelResponse(parsed=BearCaseBatch(items=[
                BearCaseItem(
                    bear_id="b_r10",
                    thesis_challenge="Professional services flat at $350M while Cloud drove growth.",
                    anchored_evidence_ids=["c_r10"],
                    anchor_kind="flag",
                    next_quarter_invalidation_trigger="Cloud Subscriptions YoY > 25%.",
                )
            ]))
        if mreq.stage == "stage_3b_gate_memo":
            return SlotModelResponse(content="Memo grounded in R10.htm.")
        raise AssertionError(mreq.stage)

    orch = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(model_fn),
        tool_runner=runner,
        prefetch_deps=PrefetchDependencies(sec_adapter=deps.sec_adapter),
        org_sinks=[events.append],
    )
    dossier = orch.run(SquadPrefetchRequest(
        question="Analyze ACME R10 segments",
        cik="0001234567",
        symbol="ACME",
        period_financials=_sample_periods(),
    ))
    assert dossier.gate_report.passed is True
    assert dossier.evidence_cards[0].card_id == "c_r10"
    assert len(dossier.bear_cases) == 1

    # Verify all OrgGraphEvent invariants
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    valid_actions = {
        "READING", "SEARCHING", "INSPECTING", "DELEGATING", "WAITING_SUBORDINATES",
        "RESPONDING", "NEEDS_USER", "BLOCKED", "FAILED",
    }
    for ev in events:
        assert ev.stage and ev.node_id and ev.role and ev.display_name and ev.current_activity
        assert ev.action_kind in valid_actions
        if ev.edge_event is not None:
            assert ev.edge_event.from_node_id and ev.edge_event.to_node_id
            assert isinstance(ev.edge_event.is_review, bool)


def test_resolve_squad_slots_accepts_model_slot_config_and_sic_matches_method_packs():
    from app.research_model_slots import ModelSlotBinding, ModelSlotConfig, SlotCapabilities
    from app.research_earnings_squad import resolve_method_pack_context, resolve_squad_slots

    # ModelSlotConfig with non-tool fast_extractor must fail closed even with lightweight primary_profile
    bad_cfg = ModelSlotConfig(
        fast_extractor=ModelSlotBinding(
            protocol="openai_chat_completions",
            base_url="https://fast.example.test/v1",
            model="bad-fast",
            capabilities=SlotCapabilities(mode="json_object", native_tools=False),
        )
    )
    with pytest.raises(SquadConfigurationError):
        resolve_squad_slots(primary_profile={"model": "p1", "native_tools": True}, slot_overrides=bad_cfg)

    # Valid partial ModelSlotConfig applies override and inherits lightweight primary for unset slots
    partial_cfg = ModelSlotConfig(
        bear_reviewer=ModelSlotBinding(
            protocol="anthropic_messages",
            base_url="https://anthropic.example.com",
            model="bear-override",
            messages_auth_scheme="x-api-key",
            messages_max_tokens=2048,
        )
    )
    bindings = resolve_squad_slots(primary_profile={"model": "p1", "native_tools": True}, slot_overrides=partial_cfg)
    assert bindings["bear_reviewer"].model == "bear-override"
    assert bindings["bear_reviewer"].protocol == "anthropic_messages"
    assert bindings["bear_reviewer"].fallback_used is False
    assert bindings["fast_extractor"].model == "p1"
    assert bindings["fast_extractor"].fallback_used is True

    # SIC 4813 & 7370 must match app.research_methods.match_industry_pack_by_sic without divergent fallback
    ctx_4813 = resolve_method_pack_context("4813")
    assert ctx_4813["l2_pack_id"] is None
    if ctx_4813["composed_pack"] is not None:
        assert ctx_4813["composed_pack"].industry_pack_id is None

    ctx_7370 = resolve_method_pack_context("7370")
    assert ctx_7370["l2_pack_id"] == "kpi_internet_ads"
    if ctx_7370["composed_pack"] is not None:
        assert ctx_7370["composed_pack"].industry_pack_id == "kpi_internet_ads"


def test_fetch_sec_submissions_meta_and_build_periods_from_sec_regressions():
    from app.research_earnings_squad import _build_periods_from_sec, _fetch_sec_submissions_meta, _pick_current_prior_periodic_filings

    cik = "0000019617"
    # Simulate 45 noisy Form 4 / 424B2 / 8-K filings ahead of current and prior 10-Q filings
    accs = [f"{cik}-26-{900000 - i:06d}" for i in range(47)]
    forms = ["424B2"] * 25 + ["4"] * 10 + ["8-K"] * 10 + ["10-Q", "10-Q"]
    # Insert second 10-Q at index 46 and first 10-Q at index 45
    forms[45] = "10-Q"
    forms[46] = "10-Q"
    fdates = [f"2026-11-{max(1, 20 - (i // 3)):02d}" for i in range(45)] + ["2026-11-03", "2026-08-06"]
    rdates = [""] * 45 + ["2026-09-30", "2026-06-30"]
    pdocs = [f"doc_{i}.htm" for i in range(47)]

    sub_doc = {
        "cik": cik,
        "name": "JPMORGAN CHASE & CO",
        "sic": "6021",
        "sicDescription": "National Commercial Banks",
        "tickers": ["JPM"],
        "filings": {
            "recent": {
                "accessionNumber": accs,
                "form": forms,
                "filingDate": fdates,
                "reportDate": rdates,
                "acceptanceDateTime": [""] * 47,
                "primaryDocument": pdocs,
                "primaryDocDescription": forms,
            }
        },
    }
    facts_doc = {
        "cik": int(cik),
        "entityName": "Sample Corp",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {"start": "2026-07-01", "end": "2026-09-30", "val": 1200000000, "fy": 2026, "fp": "Q3", "form": "10-Q", "filed": "2026-11-03", "accn": accs[45]},
                            {"start": "2026-04-01", "end": "2026-06-30", "val": 1100000000, "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2026-08-06", "accn": accs[46]},
                        ]
                    }
                },
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            {"start": "2026-07-01", "end": "2026-09-30", "val": 240000000, "fy": 2026, "fp": "Q3", "form": "10-Q", "filed": "2026-11-03", "accn": accs[45]},
                            {"start": "2026-04-01", "end": "2026-06-30", "val": 210000000, "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2026-08-06", "accn": accs[46]},
                        ]
                    }
                },
                "CashAndCashEquivalentsAtCarryingValue": {
                    "units": {
                        "USD": [
                            {"end": "2026-09-30", "val": 5000000000, "fy": 2026, "fp": "FY", "form": "10-Q", "filed": "2026-11-03", "accn": accs[45]},
                        ]
                    }
                },
            }
        },
    }

    router = _UrlRouter({
        f"submissions/CIK{cik}.json": Reply(200, json.dumps(sub_doc).encode(), {"content-type": "application/json"}),
        f"companyfacts/CIK{cik}.json": Reply(200, json.dumps(facts_doc).encode(), {"content-type": "application/json"}),
    })
    sec = SecEdgarAdapter(UA, transport=router, stream_transport=_stream_from_router(router), limiter=open_limiter(), now_fn=lambda: NOW)

    meta, _raw_doc, filings = _fetch_sec_submissions_meta(sec, cik)
    assert meta["sic"] == "6021"
    cur_f, pri_f = _pick_current_prior_periodic_filings(filings, as_of=None)
    assert cur_f is not None and cur_f.accession == accs[45]
    assert pri_f is not None and pri_f.accession == accs[46]

    periods, derived = _build_periods_from_sec(sec, cik, entity_id=f"cik:{cik}", as_of=date(2026, 11, 20))
    assert len(periods) == 2
    assert periods[0].period_label == "2026-Q3"
    assert periods[0].revenue is not None and periods[0].revenue.value == Decimal("1200000000")
    assert periods[0].cash is not None and periods[0].cash.value == Decimal("5000000000")
    assert isinstance(derived, dict)
    assert "statement_lines_count" in derived
    # Ensure derived_metrics passes strict EarningsPrefetchSnapshot Pydantic validation
    snap = EarningsPrefetchSnapshot(
        entity_id=f"cik:{cik}",
        symbol="JPM",
        cik=cik,
        sector_applicability=earnings_quality_diagnostics(EarningsQualityRequest(entity_id=f"cik:{cik}", sic="6021", current=periods[0])).sector_applicability,
        statements=periods,
        derived_metrics=derived,
    )
    assert snap.derived_metrics["statement_lines_count"] == derived["statement_lines_count"]


def test_invoke_and_parse_with_repair_concise_retry_and_fallback_on_provider_truncation() -> None:
    """Verify _invoke_and_parse_with_repair retries concisely on provider_output_truncated and uses fallback_fn."""
    from app.phase3 import ProviderFailure
    from app.research_earnings_diagnostics import evaluate_sec_sic_gate
    from app.research_earnings_squad import (
        OrgGraphEmitter,
        SquadExecutionLedger,
        _fallback_bear_batch,
    )

    seen_prompts: list[list[dict[str, Any]]] = []

    def _retry_succeeds(mreq: SlotModelRequest) -> SlotModelResponse:
        seen_prompts.append(list(mreq.messages))
        if len(seen_prompts) == 1:
            raise ProviderFailure("provider_output_truncated", "Provider output was truncated.", retryable=False)
        return SlotModelResponse(
            content=json.dumps({
                "batch_id": "bear_retry_ok",
                "items": [],
                "rejected_items": [],
                "reviewer_notes": "Concise retry succeeded.",
            }),
            usage=ModelUsage(40, 20, 60),
        )

    orch = EarningsSquadOrchestrator(model_runner=CallableSlotModelRunner(_retry_succeeds))
    ledger = SquadExecutionLedger(slot_bindings=orch._slot_bindings)
    emitter = OrgGraphEmitter()
    parsed, used_fb, last_err = orch._invoke_and_parse_with_repair(
        slot="bear_reviewer",
        stage="stage_3a_bear",
        role="bear_reviewer",
        node_id="bear_reviewer",
        system_prompt="sys",
        user_prompt="usr",
        model_cls=BearCaseBatch,
        ledger=ledger,
        emitter=emitter,
        fallback_fn=lambda _exc: BearCaseBatch(batch_id="fb", items=[], rejected_items=[]),
    )
    assert used_fb is False and last_err is None
    assert parsed.batch_id == "bear_retry_ok"
    assert len(seen_prompts) == 2
    assert len(seen_prompts[1]) == 2
    assert "provider output token limit" in seen_prompts[1][1]["content"]

    def _always_truncates(_mreq: SlotModelRequest) -> SlotModelResponse:
        raise ProviderFailure("provider_output_truncated", "Provider output was truncated.", retryable=False)

    orch_fb = EarningsSquadOrchestrator(model_runner=CallableSlotModelRunner(_always_truncates))
    ledger_fb = SquadExecutionLedger(slot_bindings=orch_fb._slot_bindings)
    emitter_fb = OrgGraphEmitter()
    snap = EarningsPrefetchSnapshot(
        entity_id="cik:0000070858",
        symbol="BAC",
        cik="0000070858",
        sector_applicability=evaluate_sec_sic_gate("6021").model_copy(
            update={"active_sector_plugin": "us_large_bank_v4a", "bank_subtype": "universal_commercial"}
        ),
        statements=[],
        derived_metrics={},
    )
    fb_batch, fb_used, fb_err = orch_fb._invoke_and_parse_with_repair(
        slot="bear_reviewer",
        stage="stage_3a_bear",
        role="bear_reviewer",
        node_id="bear_reviewer",
        system_prompt="sys",
        user_prompt="usr",
        model_cls=BearCaseBatch,
        ledger=ledger_fb,
        emitter=emitter_fb,
        fallback_fn=lambda _exc: _fallback_bear_batch(snap, [], []),
    )
    assert fb_used is True and fb_err is not None
    assert isinstance(fb_batch, BearCaseBatch)


def test_stage2_worker_truncation_retries_and_survives_wrapup() -> None:
    """Verify Stage 2 _execute_worker_task survives provider_output_truncated without raising."""
    from app.phase3 import ProviderFailure
    from app.research_earnings_diagnostics import evaluate_sec_sic_gate
    from app.research_earnings_squad import OrgGraphEmitter, SquadExecutionLedger

    snap = EarningsPrefetchSnapshot(
        entity_id="cik:0000070858",
        symbol="BAC",
        cik="0000070858",
        sector_applicability=evaluate_sec_sic_gate("6021"),
        statements=[],
        derived_metrics={},
    )
    req = SquadPrefetchRequest(entity_id="cik:0000070858", symbol="BAC", cik="0000070858", question="Analyze BAC.")
    task = WorkerTask(
        worker_role="worker_forensics",
        objective="Check credit quality.",
        allowed_tools=[],
        soft_turn_limit=1,
    )

    calls_retry: list[int] = []

    def _worker_recovers(mreq: SlotModelRequest) -> SlotModelResponse:
        calls_retry.append(len(mreq.messages))
        if len(calls_retry) == 1:
            raise ProviderFailure("provider_output_truncated", "Provider output was truncated.", retryable=False)
        return SlotModelResponse(
            content=json.dumps({
                "task_id": task.task_id,
                "worker_role": "worker_forensics",
                "round_number": 1,
                "status": "completed",
                "cards": [],
                "unresolved_gaps": [],
            }),
            usage=ModelUsage(20, 10, 30),
        )

    orch_ok = EarningsSquadOrchestrator(model_runner=CallableSlotModelRunner(_worker_recovers))
    batch_ok = orch_ok._execute_worker_task(
        request=req,
        snapshot=snap,
        task=task,
        l2_pack_id=None,
        ledger=SquadExecutionLedger(slot_bindings=orch_ok._slot_bindings),
        emitter=OrgGraphEmitter(),
        worker_cancel=Event(),
        source_texts={},
        source_permissions={},
    )
    assert batch_ok.status == "completed" and len(calls_retry) == 2

    def _worker_always_truncates(_mreq: SlotModelRequest) -> SlotModelResponse:
        raise ProviderFailure("provider_output_truncated", "Provider output was truncated.", retryable=False)

    orch_partial = EarningsSquadOrchestrator(model_runner=CallableSlotModelRunner(_worker_always_truncates))
    batch_partial = orch_partial._execute_worker_task(
        request=req,
        snapshot=snap,
        task=task,
        l2_pack_id=None,
        ledger=SquadExecutionLedger(slot_bindings=orch_partial._slot_bindings),
        emitter=OrgGraphEmitter(),
        worker_cancel=Event(),
        source_texts={},
        source_permissions={},
    )
    assert batch_partial.status == "partial"
    assert batch_partial.cards == []
    assert batch_partial.unresolved_gaps and "truncated" in batch_partial.unresolved_gaps[0].lower()


def test_org_edge_event_clamps_long_skipped_label() -> None:
    from app.research_earnings_squad import OrgEdgeEvent

    ev = OrgEdgeEvent(
        from_node_id="lead_coordinator",
        to_node_id="worker_guidance",
        label="Skipped: " + ("x" * 300),
    )
    assert ev.label is not None
    assert len(ev.label) == 240
    assert ev.label.endswith("...")



