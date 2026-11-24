"""Offline tests for P0.3, P0.4, P0.5 & P1.1 Earnings Squad real-LLM hardening."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from threading import Event, Lock
from typing import Any

import pytest

from app.earnings_external_data import ExternalEarningsService
from app.fin_data.adapters.fmp import FmpEarningsAdapter
from app.fin_data.adapters.sec_edgar import SecEdgarAdapter
from app.fin_data.policy import default_policy
from app.model_protocol import ModelToolCall, ModelUsage
from app.openai_compatible import BinaryHttpResponse
from app.phase3 import ProviderFailure
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext
from app.research_earnings_diagnostics import PeriodFinancials, MetricValue
from app.research_earnings_squad import (
    BearCaseBatch,
    BearCaseItem,
    CallableSlotModelRunner,
    EarningsSquadOrchestrator,
    EvidenceCard,
    EvidenceCardBatch,
    EvidenceCitation,
    FinToolboxToolRunner,
    InMemorySquadCheckpointStore,
    OrgGraphEmitter,
    PrefetchDependencies,
    ResearchDispatchPlan,
    SlotModelRequest,
    SlotModelResponse,
    SquadCancelledError,
    SquadExecutionError,
    SquadExecutionLedger,
    SquadPrefetchRequest,
    Stage25ReviewDecision,
    UserSteeringInstruction,
    WorkerTask,
    build_isolated_bear_prompt,
    collect_valid_evidence_anchors,
)
from app.research_sources import ResearchSource, SourcePermissions
from tests.fin_data.helpers import NOW, UA, Reply, open_limiter
from tests.fin_runtime_helpers import make_toolbox


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
        interest_expense=MetricValue(value="20", fact_id=f"{label}:int", unit="USD millions", currency="USD"),
        net_income=MetricValue(value=ni, fact_id=f"{label}:ni", unit="USD millions", currency="USD"),
        ebitda=MetricValue(value=ebitda, fact_id=f"{label}:ebitda", unit="USD millions", currency="USD"),
        sbc=MetricValue(value=sbc, fact_id=f"{label}:sbc", unit="USD millions", currency="USD"),
        shares_diluted=MetricValue(value=shares, fact_id=f"{label}:sh", unit="millions of shares"),
        operating_cash_flow=MetricValue(value=ocf, fact_id=f"{label}:ocf", unit="USD millions", currency="USD"),
        capex=MetricValue(value=capex, fact_id=f"{label}:capex", unit="USD millions", currency="USD"),
        share_repurchases=MetricValue(value="100", fact_id=f"{label}:bb", unit="USD millions", currency="USD"),
        dividends_paid=MetricValue(value="50", fact_id=f"{label}:div", unit="USD millions", currency="USD"),
        cash=MetricValue(value=cash, fact_id=f"{label}:cash", unit="USD millions", currency="USD"),
        accounts_receivable=MetricValue(value=ar, fact_id=f"{label}:ar", unit="USD millions", currency="USD"),
        inventory=MetricValue(value=inv, fact_id=f"{label}:inv", unit="USD millions", currency="USD"),
        current_assets=MetricValue(value="3500", fact_id=f"{label}:ca", unit="USD millions", currency="USD"),
        accounts_payable=MetricValue(value=ap, fact_id=f"{label}:ap", unit="USD millions", currency="USD"),
        total_debt=MetricValue(value=debt, fact_id=f"{label}:debt", unit="USD millions", currency="USD"),
        current_liabilities=MetricValue(value="1800", fact_id=f"{label}:cl", unit="USD millions", currency="USD"),
    )


def _sample_periods() -> list[PeriodFinancials]:
    return [
        _period("Q4_2025", revenue="1200", cogs="480", op_inc="260", ni="220", ocf="150", capex="40", sbc="90", ar="600", inv="140", ap="180", cash="900", debt="400", shares="100", ebitda="310"),
        _period("Q3_2025", revenue="1150", cogs="450", op_inc="250", ni="200", ocf="220", capex="35", sbc="70", ar="480", inv="135", ap="175", cash="850", debt="410", shares="100", ebitda="295"),
        _period("Q2_2025", revenue="1100", cogs="440", op_inc="240", ni="190", ocf="210", capex="30", sbc="65", ar="450", inv="130", ap="170", cash="800", debt="420", shares="99", ebitda="285"),
        _period("Q1_2025", revenue="1050", cogs="420", op_inc="225", ni="180", ocf="200", capex="30", sbc="60", ar="430", inv="125", ap="165", cash="760", debt="430", shares="98", ebitda="270"),
        _period("Q4_2024", revenue="1000", cogs="380", op_inc="240", ni="200", ocf="230", capex="30", sbc="40", ar="400", inv="120", ap="160", cash="720", debt="450", shares="96", ebitda="275"),
    ]


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


def _offline_sec_fmp_deps(*, include_fmp_key: bool = True) -> tuple[PrefetchDependencies, _UrlRouter, _UrlRouter]:
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
                }
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
                        "John Smith, CFO: Multi-year enterprise contracts back-loaded billings."
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


def _make_context(max_calls: int | None = None) -> ResearchExecutionContext:
    lock = Lock()
    calls_ledger: dict[str, int | None] = {}

    def reserve(call_id: str, tokens: int | None) -> None:
        with lock:
            calls_ledger[call_id] = tokens

    def settle(call_id: str, tokens: int | None) -> None:
        with lock:
            calls_ledger[call_id] = tokens

    return ResearchExecutionContext(
        budget=ResearchExecutionBudget(max_calls, None, None, float("inf")),
        cost_limited=False,
        ensure_running=lambda: None,
        cancel_event=lambda: None,
        reserve=reserve,
        settle=settle,
        reserve_search=lambda: None,
    )


def _prefetched_snapshot(deps: PrefetchDependencies) -> tuple[Any, dict[str, str]]:
    orch = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(lambda _r: SlotModelResponse(content="unused")),
        prefetch_deps=deps,
    )
    st: dict[str, str] = {}
    sp: dict[str, dict[str, bool]] = {}
    ledger = SquadExecutionLedger(context=_make_context(), slot_bindings=orch.slot_bindings, cancel_event=orch._cancel_event)
    snap = orch._run_stage_0_prefetch(
        request=SquadPrefetchRequest(
            question="Analyze ACME Q4 2025 earnings.",
            cik="0001234567",
            symbol="ACME",
            period_label="Q4_2025",
            fiscal_year=2025,
            fiscal_quarter=4,
            period_financials=_sample_periods(),
        ),
        ledger=ledger,
        emitter=OrgGraphEmitter(sinks=[]),
        source_texts=st,
        source_permissions=sp,
    )
    return snap, st


# --------------------------------------------------------------------------- 1. Real topic_33f42407 payload reproduction
def test_topic_33f42407_stage1_payload_normalization_and_tool_filtering() -> None:
    """Reproduce exact topic_33f4240706c8458fa78b4c273d2c9c3d Stage 1 drift and pass with 0 repairs."""
    deps, _sec, _fmp = _offline_sec_fmp_deps(include_fmp_key=True)
    call_counts: dict[str, int] = {}

    drifted_stage1_text = (
        "Here is the bounded Stage 1 ResearchDispatchPlan JSON:\n"
        "```json\n"
        "{\n"
        '  "l1_pack_id": "earnings_review",\n'
        '  "selected_l2_pack_id": "kpi_saas_cloud",\n'
        '  "selected_l2_pack_reason": "Matched SIC 7372 SaaS/Cloud.",\n'
        '  "worker_tasks": [\n'
        "    {\n"
        '      "role": "WORKER_SEGMENTS",\n'
        '      "status": "PLANNED",\n'
        '      "objective": "Verify Cloud Subscriptions $850M and segment KPIs.",\n'
        '      "target_r_files": "R10.htm",\n'
        '      "allowed_tools": ["sec_filing_tables", "sec_fulltext_search", "read_sources"],\n'
        '      "extra_llm_note": "should be stripped cleanly"\n'
        "    },\n"
        "    {\n"
        '      "worker_role": "worker_forensics",\n'
        '      "status": "planned",\n'
        '      "objective": "Verify AR divergence and SBC/FCF red/yellow flags.",\n'
        '      "target_flag_ids": ["working_capital_ar_vs_revenue_growth_pp"]\n'
        "    },\n"
        "    {\n"
        '      "worker_role": "worker_guidance",\n'
        '      "status": "skipped"\n'
        "    }\n"
        "  ],\n"
        '  "dispatch_rationale": {\n'
        '    "summary": "Segment and working-capital diagnostics carry primary dispersion risk.",\n'
        '    "guidance_note": "Skipped for focused dispatch."\n'
        "  },\n"
        '  "cross_worker_hypotheses": "AR growth divergence vs Cloud billings cadence.",\n'
        '  "unexpected_wrapper_field": true\n'
        "}\n"
        "```"
    )

    def model_fn(mreq: SlotModelRequest) -> SlotModelResponse:
        call_counts[mreq.stage] = call_counts.get(mreq.stage, 0) + 1
        if mreq.stage == "stage_1_plan":
            return SlotModelResponse(content=drifted_stage1_text, usage=ModelUsage(40, 40, 80))
        if mreq.stage == "stage_2_workers":
            # Also exercise EvidenceCardBatch alias normalization (`evidence_cards`, `source`, `excerpt`, dict `finding`, extra key stripped)
            return SlotModelResponse(content=json.dumps({
                "evidence_cards": [
                    {
                        "card_id": f"c_{mreq.role}",
                        "worker_role": mreq.role,
                        "topic_tag": "verification",
                        "finding": {"detail": "Cloud Subscriptions grew 30.8% year-over-year to $850 million."},
                        "citations": [
                            {
                                "source": "src_8k_release_0001234567",
                                "excerpt": "Cloud Subscriptions grew 30.8% year-over-year to $850 million.",
                            }
                        ],
                        "supporting_fact_ids": "Q4_2025:rev",
                        "direction": "POSITIVE",
                    }
                ],
                "commentary_to_strip": "done",
            }))
        if mreq.stage == "stage_2_5_review":
            return SlotModelResponse(content=json.dumps({
                "has_cross_worker_leads": False,
                "followups": [],
                "rationale": {"decision": "Workers verified Cloud revenue and cash flow divergence."},
            }))
        if mreq.stage == "stage_3a_bear":
            payload = json.loads(mreq.user_prompt)
            anchors = payload["valid_anchors"]
            flag_id = payload["diagnostic_flags"][0]["flag_id"]
            assert flag_id in anchors
            return SlotModelResponse(content=json.dumps({
                "bear_cases": [
                    {
                        "bear_id": "bear_1",
                        "thesis_challenge": "AR surged well ahead of revenue while SBC eroded cash conversion.",
                        "anchor_ids": [flag_id],
                        "anchor_kind": "FLAG",
                        "next_quarter_invalidation_trigger": "DSO normalizes and OCF conversion exceeds 90%.",
                        "severity": "critical",
                    }
                ],
                "reviewer_notes": "Anchored bear case.",
            }))
        if mreq.stage == "stage_3b_gate_memo":
            return SlotModelResponse(content="# ACME Q4 2025 Institutional Earnings Memo\n- Cloud Subscriptions $850M (+30.8% YoY).")
        raise AssertionError(f"Unexpected stage {mreq.stage}")

    orch = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(model_fn),
        prefetch_deps=deps,
    )
    dossier = orch.run(SquadPrefetchRequest(
        question="Analyze ACME Q4 2025.",
        cik="0001234567",
        symbol="ACME",
        period_label="Q4_2025",
        fiscal_year=2025,
        fiscal_quarter=4,
        period_financials=_sample_periods(),
    ))

    assert dossier.locked is True
    assert call_counts["stage_1_plan"] == 1  # Zero repair turns needed on topic_33f42407 payload!
    assert [t.worker_role for t in dossier.dispatch_plan.tasks] == [
        "worker_segments", "worker_forensics", "worker_guidance"
    ]
    seg_task = dossier.dispatch_plan.tasks[0]
    assert seg_task.target_r_files == ["R10.htm"]
    # Unauthorized tool `sec_fulltext_search` stripped cleanly during normalization:
    assert seg_task.allowed_tools == ["sec_filing_tables", "read_sources"]
    assert "sec_fulltext_search" not in (seg_task.allowed_tools or [])
    gui_task = dossier.dispatch_plan.tasks[2]
    assert gui_task.status == "skipped"
    assert bool(gui_task.skip_reason and gui_task.skip_reason.strip())
    assert "Skipped:" in gui_task.objective
    assert "Segment and working-capital diagnostics" in dossier.dispatch_plan.planning_notes
    assert len(dossier.evidence_cards) == 2
    assert len(dossier.bear_cases) == 1
    assert dossier.bear_cases[0].severity == "high"
    assert all(d["repair_attempts"] == 0 for d in orch.repair_diagnostics)


# --------------------------------------------------------------------------- 2. Prompt Hardening, L1/L2 Injection, Bear Anchors, Writer Slimming
def test_stage_prompts_inject_l1_l2_pack_bear_anchors_and_slim_writer_payload() -> None:
    deps, _sec, _fmp = _offline_sec_fmp_deps(include_fmp_key=True)
    captured_reqs: list[SlotModelRequest] = []

    def model_fn(mreq: SlotModelRequest) -> SlotModelResponse:
        captured_reqs.append(mreq)
        if mreq.stage == "stage_1_plan":
            return SlotModelResponse(parsed=ResearchDispatchPlan(
                selected_l2_pack_id="kpi_saas_cloud",
                selected_l2_pack_reason="Matched SIC 7372.",
                tasks=[
                    WorkerTask(worker_role="worker_segments", objective="Segments", target_r_files=["R10.htm"]),
                    WorkerTask(worker_role="worker_forensics", objective="Forensics"),
                    WorkerTask(worker_role="worker_guidance", objective="Guidance"),
                ],
            ))
        if mreq.stage == "stage_2_workers":
            return SlotModelResponse(parsed=EvidenceCardBatch(
                task_id="t",
                worker_role=mreq.role,  # type: ignore[arg-type]
                cards=[
                    EvidenceCard(
                        card_id=f"c_{mreq.role}",
                        worker_role=mreq.role,  # type: ignore[arg-type]
                        topic_tag="kpi",
                        finding="Cloud Subscriptions grew 30.8% YoY to $850M.",
                        citations=[EvidenceCitation(
                            source_id="src_8k_release_0001234567",
                            quotation="Cloud Subscriptions grew 30.8% year-over-year to $850 million.",
                        )],
                    )
                ],
            ))
        if mreq.stage == "stage_2_5_review":
            return SlotModelResponse(parsed=Stage25ReviewDecision(has_cross_worker_leads=False))
        if mreq.stage == "stage_3a_bear":
            parsed = json.loads(mreq.user_prompt)
            anchors = parsed["valid_anchors"]
            return SlotModelResponse(parsed=BearCaseBatch(items=[
                BearCaseItem(
                    bear_id="bear_ok",
                    thesis_challenge="DSO divergence risk.",
                    anchored_evidence_ids=[anchors[0]],
                    anchor_kind="flag",
                    next_quarter_invalidation_trigger="DSO stabilizes.",
                )
            ]))
        if mreq.stage == "stage_3b_gate_memo":
            return SlotModelResponse(content="# Memo ACME\n- 分析摘要")
        raise AssertionError(mreq.stage)

    orch = EarningsSquadOrchestrator(model_runner=CallableSlotModelRunner(model_fn), prefetch_deps=deps)
    orch.run(SquadPrefetchRequest(
        question="请分析 ACME Q4 2025 财报与现金流质量。",
        cik="0001234567",
        symbol="ACME",
        period_label="Q4_2025",
        fiscal_year=2025,
        fiscal_quarter=4,
        period_financials=_sample_periods(),
    ))

    # Stage 1 checks
    s1 = next(r for r in captured_reqs if r.stage == "stage_1_plan")
    assert "extra='forbid'" in s1.system_prompt
    assert "selected_l2_pack_id" in s1.system_prompt
    assert "skip_reason" in s1.system_prompt
    s1_payload = json.loads(s1.user_prompt)
    assert s1_payload["method_pack_hint"]["suggested_l2_pack_id"] == "kpi_saas_cloud"
    assert s1_payload["snapshot_summary"]["filing_table_catalog_summary"] is not None
    assert "src_8k_release_0001234567" in [s["source_id"] for s in s1_payload["snapshot_summary"]["prefetched_sources"]]

    # Stage 2 Worker checks (exact schema keys + L1/L2 pack injection + Tool SOP)
    w_seg = next(r for r in captured_reqs if r.stage == "stage_2_workers" and r.role == "worker_segments")
    assert "extra='forbid'" in w_seg.system_prompt
    assert '"quotation"' in w_seg.system_prompt
    assert "sec_filing_tables" in w_seg.system_prompt
    assert "read_sources" in w_seg.system_prompt
    assert "guidance_and_nongaap_reconciler" in w_seg.system_prompt
    w_init = json.loads(w_seg.messages[1]["content"])
    pack = w_init["method_pack"]
    assert pack["l1_pack_id"] == "earnings_review"
    assert pack["l2_pack_id"] == "kpi_saas_cloud"
    assert len(pack["l1_evidence_requirements"]) >= 1
    assert len(pack["l1_unit_and_period_checks"]) >= 1
    assert any(k["id"] in ("rpo_crpo", "nrr_expansion", "deferred_revenue_billings") for k in pack["l2_kpis"])
    assert "earnings_review" in pack["rendered_prompt"]
    assert "SaaS" in pack["rendered_prompt"] or "NRR" in pack["rendered_prompt"]

    # Stage 3A Bear anchor injection + isolation checks
    bear_req = next(r for r in captured_reqs if r.stage == "stage_3a_bear")
    assert "extra='forbid'" in bear_req.system_prompt
    assert "valid_anchors" in bear_req.system_prompt
    bear_payload = json.loads(bear_req.user_prompt)
    assert len(bear_payload["valid_anchors"]) >= 2
    assert "c_worker_segments" in bear_payload["valid_anchors"]
    assert "diagnostic_flags" in bear_payload
    assert "cross_worker_hypotheses" not in bear_req.system_prompt + bear_req.user_prompt

    # Stage 3B Writer slimming & CJK language alignment
    w_req = next(r for r in captured_reqs if r.stage == "stage_3b_gate_memo")
    assert "简体中文" in w_req.system_prompt
    assert "report structure" in w_req.system_prompt.lower()
    w_payload = json.loads(w_req.user_prompt)["locked_dossier"]
    assert "raw_text" not in json.dumps(w_payload["snapshot"])
    assert "statements_summary" in w_payload["snapshot"]
    assert "redline_summary" in w_payload["snapshot"]

    # Direct unit verification of build_isolated_bear_prompt with explicit valid_anchors
    snap, _ = _prefetched_snapshot(deps)
    cards = [
        EvidenceCard(
            card_id="c_test_anchor",
            worker_role="worker_segments",
            topic_tag="seg",
            finding="Seg rev $850M",
            supporting_fact_ids=["Q4_2025:rev"],
        )
    ]
    sys_b, usr_b = build_isolated_bear_prompt(snap, cards, [])
    assert json.loads(usr_b)["valid_anchors"] == sorted(collect_valid_evidence_anchors(snap, cards, []))
    assert "c_test_anchor" in json.loads(usr_b)["valid_anchors"]
    assert "BearCaseBatch" in sys_b


# --------------------------------------------------------------------------- 3. Multi-Round Diagnostic Repair Retries (Attempt 2, 3, and 4)
def test_multi_round_diagnostic_repair_succeeds_on_attempt_2_3_and_4() -> None:
    deps, _sec, _fmp = _offline_sec_fmp_deps(include_fmp_key=True)
    stage_turns: dict[tuple[str, str], list[SlotModelRequest]] = {}

    def model_fn(mreq: SlotModelRequest) -> SlotModelResponse:
        key = (mreq.stage, mreq.role)
        hist = stage_turns.setdefault(key, [])
        idx = len(hist)
        hist.append(mreq)

        if mreq.stage == "stage_1_plan":
            if idx == 0:
                return SlotModelResponse(content="not json at all")
            if idx == 1:
                # Passes JSON parse but fails Pydantic (`nonexistent_role`)
                return SlotModelResponse(content=json.dumps({
                    "l1_pack_id": "earnings_review",
                    "tasks": [{"worker_role": "nonexistent_role", "objective": "Bad role"}],
                }))
            # Succeeds on 3rd call (repair 2/3)
            return SlotModelResponse(content=json.dumps({
                "l1_pack_id": "earnings_review",
                "selected_l2_pack_id": "kpi_saas_cloud",
                "selected_l2_pack_reason": "Repaired cleanly on attempt 3.",
                "tasks": [
                    {"worker_role": "worker_segments", "status": "planned", "objective": "Verify segments."},
                    {"worker_role": "worker_forensics", "status": "skipped", "skip_reason": "Focused test."},
                    {"worker_role": "worker_guidance", "status": "skipped", "skip_reason": "Focused test."},
                ],
                "instruction_sequence": 0,
            }))

        if mreq.stage == "stage_2_workers" and mreq.role == "worker_segments":
            if idx == 0:
                return SlotModelResponse(content="{not valid worker json")
            if idx == 1:
                # Fails EvidenceCitation Pydantic check (blank quotation!)
                return SlotModelResponse(content=json.dumps({
                    "task_id": "t",
                    "worker_role": "worker_segments",
                    "status": "completed",
                    "cards": [{
                        "card_id": "c1",
                        "worker_role": "worker_segments",
                        "topic_tag": "seg",
                        "finding": "Cloud $850M",
                        "citations": [{"source_id": "src_8k_release_0001234567", "quotation": ""}],
                    }],
                }))
            # Succeeds on 3rd call (repair 2/3)!
            return SlotModelResponse(content=json.dumps({
                "task_id": "t",
                "worker_role": "worker_segments",
                "status": "completed",
                "cards": [{
                    "card_id": "c1_repaired",
                    "worker_role": "worker_segments",
                    "topic_tag": "seg",
                    "finding": "Cloud Subscriptions grew 30.8% YoY to $850M.",
                    "citations": [{
                        "source_id": "src_8k_release_0001234567",
                        "quotation": "Cloud Subscriptions grew 30.8% year-over-year to $850 million.",
                    }],
                }],
            }))

        if mreq.stage == "stage_2_5_review":
            if idx == 0:
                return SlotModelResponse(content="not json")
            if idx == 1:
                # Missing required WorkerTask fields fails Stage25ReviewDecision validation!
                return SlotModelResponse(content=json.dumps({
                    "has_cross_worker_leads": True,
                    "followup_tasks": [
                        {"round_number": 2},
                    ],
                }))
            if idx == 2:
                # Invalid followup_tasks type fails Pydantic!
                return SlotModelResponse(content=json.dumps({
                    "has_cross_worker_leads": False,
                    "followup_tasks": "wrong_type",
                }))
            # Succeeds on 4th call (repair 3/3)!
            return SlotModelResponse(content=json.dumps({
                "has_cross_worker_leads": False,
                "followup_tasks": [],
                "rationale": "Repaired cleanly on repair 3/3 (attempt 4).",
            }))

        if mreq.stage == "stage_3a_bear":
            if idx == 0:
                return SlotModelResponse(content=json.dumps({
                    "items": [{
                        "bear_id": "b1",
                        "thesis_challenge": "Missing anchor fails Pydantic",
                        "anchored_evidence_ids": [],
                        "anchor_kind": "flag",
                        "next_quarter_invalidation_trigger": "N/A",
                    }]
                }))
            # Succeeds on 2nd call (repair 1/3)!
            anchors = json.loads(mreq.messages[1]["content"])["valid_anchors"]
            return SlotModelResponse(content=json.dumps({
                "items": [{
                    "bear_id": "b1_repaired",
                    "thesis_challenge": "SBC 60% of FCF undermines quality.",
                    "anchored_evidence_ids": [anchors[0]],
                    "anchor_kind": "flag",
                    "next_quarter_invalidation_trigger": "SBC < 30% of FCF.",
                    "severity": "high",
                }]
            }))

        if mreq.stage == "stage_3b_gate_memo":
            return SlotModelResponse(content="# Repaired Squad Memo\n- Cloud $850M.")
        raise AssertionError((mreq.stage, mreq.role, idx))

    orch = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(model_fn),
        prefetch_deps=deps,
        max_repair_attempts=3,
    )
    dossier = orch.run(SquadPrefetchRequest(
        question="Test multi-round repair retries",
        cik="0001234567",
        symbol="ACME",
        period_financials=_sample_periods(),
    ))

    assert len(stage_turns[("stage_1_plan", "lead_coordinator")]) == 3
    s1_repair_1 = stage_turns[("stage_1_plan", "lead_coordinator")][1]
    assert s1_repair_1.tools == []
    assert "SCHEMA REPAIR REQUIRED (attempt 1/3)" in s1_repair_1.user_prompt
    s1_repair_2 = stage_turns[("stage_1_plan", "lead_coordinator")][2]
    assert "SCHEMA REPAIR REQUIRED (attempt 2/3)" in s1_repair_2.user_prompt
    assert "nonexistent_role" in s1_repair_2.user_prompt or "tasks" in s1_repair_2.user_prompt

    assert len(stage_turns[("stage_2_workers", "worker_segments")]) == 3
    w_repair_2 = stage_turns[("stage_2_workers", "worker_segments")][2]
    assert w_repair_2.tools == []
    assert "cards.0.citations.0.quotation" in w_repair_2.user_prompt

    assert len(stage_turns[("stage_2_5_review", "lead_coordinator")]) == 4
    s25_repair_3 = stage_turns[("stage_2_5_review", "lead_coordinator")][3]
    assert "SCHEMA REPAIR REQUIRED (attempt 3/3)" in s25_repair_3.user_prompt

    assert len(stage_turns[("stage_3a_bear", "bear_reviewer")]) == 2
    assert dossier.evidence_cards[0].card_id == "c1_repaired"
    assert dossier.bear_cases[0].bear_id == "b1_repaired"

    diag_by_stage = {d["stage"]: d for d in orch.repair_diagnostics}
    assert diag_by_stage["stage_1_plan"]["repair_attempts"] == 2
    assert diag_by_stage["stage_2_workers"]["repair_attempts"] == 2
    assert diag_by_stage["stage_2_5_review"]["repair_attempts"] == 3
    assert diag_by_stage["stage_3a_bear"]["repair_attempts"] == 1
    assert all(d["succeeded"] is True for d in orch.repair_diagnostics)


# --------------------------------------------------------------------------- 4. Safe Per-Stage Fallbacks, Budget & Cancellation Respect
def test_safe_fallbacks_after_3_failed_repairs_and_writer_empty_fallback() -> None:
    deps, _sec, _fmp = _offline_sec_fmp_deps(include_fmp_key=False)
    calls: dict[tuple[str, str], int] = {}

    def always_bad_except_segments(mreq: SlotModelRequest) -> SlotModelResponse:
        key = (mreq.stage, mreq.role)
        calls[key] = calls.get(key, 0) + 1
        if mreq.stage == "stage_1_plan":
            return SlotModelResponse(content="<<<completely invalid stage 1>>>")
        if mreq.stage == "stage_2_workers":
            if mreq.role == "worker_segments":
                return SlotModelResponse(parsed=EvidenceCardBatch(
                    task_id="t_seg",
                    worker_role="worker_segments",
                    cards=[EvidenceCard(
                        card_id="c_seg_ok",
                        worker_role="worker_segments",
                        topic_tag="seg",
                        finding="Cloud Subscriptions grew 30.8% YoY to $850M.",
                        citations=[EvidenceCitation(
                            source_id="src_8k_release_0001234567",
                            quotation="Cloud Subscriptions grew 30.8% year-over-year to $850 million.",
                        )],
                    )],
                ))
            # worker_forensics exhausts all 1 + 3 repairs with invalid schema!
            return SlotModelResponse(content='{"cards": "not_a_list"}')
        if mreq.stage == "stage_2_5_review":
            return SlotModelResponse(content="{not json stage 2.5")
        if mreq.stage == "stage_3a_bear":
            return SlotModelResponse(content='{"items": [{"bear_id": "bad"}]}')
        if mreq.stage == "stage_3b_gate_memo":
            return SlotModelResponse(content="   ")  # Triggers deterministic markdown fallback + gap!
        raise AssertionError(mreq.stage)

    orch = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(always_bad_except_segments),
        prefetch_deps=deps,
        max_repair_attempts=3,
    )
    dossier = orch.run(SquadPrefetchRequest(
        question="Fallback resilience test for ACME",
        cik="0001234567",
        symbol="ACME",
        period_financials=_sample_periods(),
    ))

    # Exactly 1 initial + 3 repair attempts on each broken schema stage!
    assert calls[("stage_1_plan", "lead_coordinator")] == 4
    assert calls[("stage_2_workers", "worker_forensics")] == 4
    assert calls[("stage_2_5_review", "lead_coordinator")] == 4
    assert calls[("stage_3a_bear", "bear_reviewer")] == 4

    gap_codes = {g.code for g in dossier.open_gaps}
    assert "stage1_plan_fallback_used" in gap_codes
    assert "worker_partial_gap" in gap_codes
    assert "stage25_fallback_skipped" in gap_codes
    assert "stage3a_deterministic_fallback" in gap_codes
    assert "stage3b_writer_fallback_used" in gap_codes

    # Partial Survival kept worker_segments EvidenceCard AND generated anchored BearCases from Red/Yellow flags/redlines!
    assert [c.card_id for c in dossier.evidence_cards] == ["c_seg_ok"]
    assert len(dossier.bear_cases) >= 1
    assert any(b.bear_id.startswith("bear_fallback_") for b in dossier.bear_cases)
    assert "Buy-Side Earnings Memo: ACME" in dossier.memo_markdown and dossier.locked is True
    assert any(d["used_fallback"] is True for d in orch.repair_diagnostics)

    # Budget limit MUST NOT be bypassed by repair loops
    tight_ctx = _make_context(max_calls=2)
    orch_budget = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(always_bad_except_segments),
        prefetch_deps=deps,
        context=tight_ctx,
        max_repair_attempts=3,
    )
    with pytest.raises((SquadExecutionError, ProviderFailure)):
        orch_budget.run(SquadPrefetchRequest(question="Tight budget", cik="0001234567", symbol="ACME", period_financials=_sample_periods()))

    # Cancellation MUST stop immediately during repair
    cancel_ev = Event()

    def cancel_on_repair(mreq: SlotModelRequest) -> SlotModelResponse:
        if mreq.turn_index == 1:
            cancel_ev.set()
        return SlotModelResponse(content="bad")

    orch_cancel = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(cancel_on_repair),
        prefetch_deps=deps,
        cancel_event=cancel_ev,
        max_repair_attempts=3,
    )
    with pytest.raises(SquadCancelledError):
        orch_cancel.run(SquadPrefetchRequest(question="Cancel during repair", cik="0001234567", symbol="ACME", period_financials=_sample_periods()))


def test_steering_repair_exhaustion_falls_back_safely() -> None:
    deps, _sec, _fmp = _offline_sec_fmp_deps(include_fmp_key=True)
    steer_calls = 0
    steered_submitted = False
    orch_ref: list[EarningsSquadOrchestrator] = []

    def model_fn(mreq: SlotModelRequest) -> SlotModelResponse:
        nonlocal steer_calls, steered_submitted
        if mreq.stage == "stage_1_plan":
            return SlotModelResponse(parsed=ResearchDispatchPlan(
                selected_l2_pack_id="kpi_saas_cloud",
                tasks=[WorkerTask(worker_role="worker_segments", objective="Initial seg check")],
            ))
        if mreq.stage == "steering":
            steer_calls += 1
            return SlotModelResponse(content="{not valid steering decision")
        if mreq.stage == "stage_2_workers":
            if not steered_submitted:
                steered_submitted = True
                orch_ref[0].submit_steering_instruction("Also verify gross margin trend.")
            return SlotModelResponse(parsed=EvidenceCardBatch(
                task_id="t",
                worker_role=mreq.role,  # type: ignore[arg-type]
                cards=[EvidenceCard(
                    card_id=f"c_{mreq.role}",
                    worker_role=mreq.role,  # type: ignore[arg-type]
                    topic_tag="seg",
                    finding="Cloud Subscriptions $850M.",
                    supporting_fact_ids=["Q4_2025:rev"],
                )],
            ))
        if mreq.stage == "stage_2_5_review":
            return SlotModelResponse(parsed=Stage25ReviewDecision(has_cross_worker_leads=False))
        if mreq.stage == "stage_3a_bear":
            return SlotModelResponse(parsed=BearCaseBatch(items=[]))
        return SlotModelResponse(content="# Memo after steering fallback")

    orch = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(model_fn),
        prefetch_deps=deps,
        max_repair_attempts=2,
    )
    orch_ref.append(orch)
    dossier = orch.run(
        SquadPrefetchRequest(question="Steer test", cik="0001234567", symbol="ACME", period_financials=_sample_periods()),
    )
    assert steer_calls == 3  # 1 initial + 2 repairs
    assert any(g.code == "steering_fallback_used" for g in dossier.open_gaps)
    assert dossier.locked is True


# --------------------------------------------------------------------------- 5. Stage 0 Prefetched Sources Registered in FinToolboxToolRunner + Resume
def test_stage0_prefetched_sources_registered_and_readable_via_fintoolbox_runner() -> None:
    deps, sec_router, fmp_router = _offline_sec_fmp_deps(include_fmp_key=True)
    toolbox = make_toolbox(
        sec=sec_router,
        sec_stream=_stream_from_router(sec_router),
        fmp_key="fmp-secret-test-key",
        fmp_transport=fmp_router,
    )
    runner = FinToolboxToolRunner(toolbox)
    cp_store = InMemorySquadCheckpointStore()
    observed_prefetched_at_turn0: list[str] = []
    outline_output: dict[str, Any] = {}
    search_output: dict[str, Any] = {}

    def model_fn(mreq: SlotModelRequest) -> SlotModelResponse:
        if mreq.stage == "stage_1_plan":
            return SlotModelResponse(parsed=ResearchDispatchPlan(
                selected_l2_pack_id="kpi_saas_cloud",
                tasks=[
                    WorkerTask(worker_role="worker_segments", objective="Read prefetched 8-K & 10-Q sections"),
                    WorkerTask(worker_role="worker_guidance", objective="Check consensus via seeded entity_id"),
                ],
            ))
        if mreq.stage == "stage_2_workers" and mreq.role == "worker_segments":
            if mreq.turn_index == 0:
                observed_prefetched_at_turn0.extend(sorted(runner.source_by_id.keys()))
                return SlotModelResponse(tool_calls=(
                    ModelToolCall("tc_out", "outline_source", {"source_id": "src_sec_sections_0001234567-25-000101"}),
                    ModelToolCall("tc_src", "search_source", {"source_id": "src_sec_sections_0001234567-25-000101", "query": "remaining performance obligations"}),
                    ModelToolCall("tc_read", "read_sources", {"reads": [{"source_id": "src_8k_release_0001234567", "start": 0, "end": 50000}], "rationale": "Read full EX-99.1"}),
                ))
            tool_msgs = [json.loads(m["content"]) for m in mreq.messages if m.get("role") == "tool"]
            outline_output.update(tool_msgs[0])
            search_output.update(tool_msgs[1])
            return SlotModelResponse(parsed=EvidenceCardBatch(
                task_id="t_seg",
                worker_role="worker_segments",
                cards=[
                    EvidenceCard(
                        card_id="c_prefetched",
                        worker_role="worker_segments",
                        topic_tag="segment_growth",
                        finding="Cloud Subscriptions reached $850M (+30.8% YoY).",
                        citations=[
                            EvidenceCitation(
                                source_id="src_8k_release_0001234567",
                                quotation="Cloud Subscriptions grew 30.8% year-over-year to $850 million.",
                            ),
                            EvidenceCitation(
                                source_id="src_sec_sections_0001234567-25-000101",
                                quotation="remaining performance obligations reached $4.2 billion",
                            ),
                        ],
                    )
                ],
            ))
        if mreq.stage == "stage_2_workers" and mreq.role == "worker_guidance":
            if mreq.turn_index == 0:
                return SlotModelResponse(tool_calls=(
                    ModelToolCall("tc_con", "earnings_consensus", {"entity_id": "cik:0001234567"}),
                ))
            return SlotModelResponse(parsed=EvidenceCardBatch(
                task_id="t_gui",
                worker_role="worker_guidance",
                cards=[],
                status="completed",
            ))
        if mreq.stage == "stage_2_5_review":
            return SlotModelResponse(parsed=Stage25ReviewDecision(has_cross_worker_leads=False))
        if mreq.stage == "stage_3a_bear":
            return SlotModelResponse(parsed=BearCaseBatch(items=[]))
        return SlotModelResponse(content="# Memo with prefetched source verification")

    orch = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(model_fn),
        tool_runner=runner,
        prefetch_deps=deps,
        checkpoint_store=cp_store,
    )
    dossier = orch.run(
        SquadPrefetchRequest(
            question="Verify prefetched sources in FinToolboxToolRunner.",
            cik="0001234567",
            symbol="ACME",
            as_of=date(2025, 11, 10),
            period_financials=_sample_periods(),
        ),
        run_id="run_prefetch_reg",
    )

    assert "src_8k_release_0001234567" in observed_prefetched_at_turn0
    assert "src_sec_sections_0001234567-25-000101" in observed_prefetched_at_turn0
    assert "src_sec_sections_prior_0001234567-25-000051" in observed_prefetched_at_turn0
    assert "src_transcript_ACME_2025Q4" in observed_prefetched_at_turn0
    assert "cik:0001234567" in runner.entities

    # SEC Item outline parser returns structured Item headings + character offsets (not a 25-line stub)
    assert outline_output["status"] == "ok"
    assert any("Item 2" in entry["title"] or "Item 1A" in entry["title"] for entry in outline_output["entries"])
    assert search_output["status"] == "ok" and len(search_output["hits"]) >= 1
    assert len(dossier.gate_report.verified_card_ids) == 1

    # Verify checkpoint resume re-syncs Stage 0 sources into a brand-new FinToolboxToolRunner!
    fresh_runner = FinToolboxToolRunner(make_toolbox(sec=sec_router, sec_stream=_stream_from_router(sec_router)))
    assert fresh_runner.source_by_id == {}
    cp = cp_store.load("run_prefetch_reg")
    assert cp is not None
    rolled = cp.model_copy(update={
        "completed_stages": ["stage_0", "stage_1", "stage_2"],
        "stage25_executed": False,
        "stage25_decision": None,
        "locked_dossier": None,
    })
    cp_store.save(rolled)

    resumed_read_res: dict[str, Any] = {}

    def resume_model(mreq: SlotModelRequest) -> SlotModelResponse:
        if mreq.stage == "stage_2_5_review" and mreq.role == "lead_coordinator":
            return SlotModelResponse(parsed=Stage25ReviewDecision(
                has_cross_worker_leads=True,
                followup_tasks=[WorkerTask(
                    worker_role="worker_forensics",
                    round_number=2,
                    objective="Read prior 10-Q section via resumed tool runner",
                )],
            ))
        if mreq.stage in ("stage_2_workers", "stage_2_5_review") and mreq.role == "worker_forensics":
            if mreq.turn_index == 0:
                res = fresh_runner.call_tool("read_sources", {
                    "reads": [{"source_id": "src_sec_sections_prior_0001234567-25-000051", "start": 0, "end": 500}],
                    "rationale": "Read prior 10-Q sections after resume",
                }, worker_role="worker_forensics")
                resumed_read_res.update(res)
            return SlotModelResponse(parsed=EvidenceCardBatch(task_id="t_res", worker_role="worker_forensics", round_number=2, cards=[]))
        if mreq.stage == "stage_3a_bear":
            return SlotModelResponse(parsed=BearCaseBatch(items=[]))
        return SlotModelResponse(content="# Resumed Memo")

    orch_resume = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(resume_model),
        tool_runner=fresh_runner,
        prefetch_deps=deps,
        checkpoint_store=cp_store,
    )
    resumed_dossier = orch_resume.run(
        SquadPrefetchRequest(question="Resume", cik="0001234567", symbol="ACME", as_of=date(2025, 11, 10)),
        resume_from="run_prefetch_reg",
    )
    assert resumed_dossier.locked is True
    assert resumed_read_res["status"] == "ok"
    assert "Cloud Subscription revenue grew 26.0%" in resumed_read_res["reads"][0]["text"]


# --------------------------------------------------------------------------- 6. Wired Diagnostic Tools + Invalid Tool Args Recovery
def test_wired_diagnostic_tools_and_invalid_tool_arg_recovery() -> None:
    deps, sec_router, _fmp = _offline_sec_fmp_deps(include_fmp_key=False)
    toolbox = make_toolbox(sec=sec_router, sec_stream=_stream_from_router(sec_router))
    runner = FinToolboxToolRunner(
        toolbox,
        entities={"cik:0001234567": {"company": "Acme Cloud", "symbol": "ACME", "identifiers": {"cik": "0001234567"}}},
    )

    # 1) guidance_and_nongaap_reconciler wired + supports alias `ticker` & `period`
    gui_res = runner.call_tool(
        "guidance_and_nongaap_reconciler",
        {
            "ticker": "ACME",
            "period": "Q4_2025",
            "guidance_metrics": [
                {
                    "metric_name": "Revenue",
                    "actual": {"value": "1200", "unit": "USD millions", "currency": "USD"},
                    "consensus": {"value": "1185", "unit": "USD millions", "currency": "USD"},
                    "prior_guidance": {
                        "low": {"value": "1170", "unit": "USD millions", "currency": "USD"},
                        "high": {"value": "1190", "unit": "USD millions", "currency": "USD"},
                    },
                }
            ],
            "nongaap_bridges": [
                {
                    "metric_name": "Operating Income",
                    "period_label": "Q4_2025",
                    "gaap_value": {"value": "260", "unit": "USD millions", "currency": "USD"},
                    "reported_non_gaap_value": {"value": "350", "unit": "USD millions", "currency": "USD"},
                    "adjustments": [{"label": "SBC", "amount": "90"}],
                }
            ],
        },
        worker_role="worker_guidance",
    )
    assert gui_res["status"] == "ok"
    assert gui_res["guidance_evaluations"][0]["vs_consensus"]["outcome"] == "Beat"

    # 2) earnings_quality_diagnostics wired for worker_forensics
    sample_p = _sample_periods()
    eq_res = runner.call_tool(
        "earnings_quality_diagnostics",
        {
            "symbol": "ACME",
            "current": sample_p[0].model_dump(mode="json"),
            "prior_qoq": sample_p[1].model_dump(mode="json"),
            "prior_yoy": sample_p[-1].model_dump(mode="json"),
            "trailing_quarters": [p.model_dump(mode="json") for p in sample_p[:4]],
        },
        worker_role="worker_forensics",
    )
    assert eq_res["status"] == "ok"
    assert any(f["flag_id"] == "wc_ar_vs_revenue_growth" for f in eq_res["flags"])

    # 3) valuation_and_implied_expectations wired for worker_guidance
    val_res = runner.call_tool(
        "valuation_and_implied_expectations",
        {
            "symbol": "ACME",
            "close_price": {"value": "140", "unit": "USD per share", "currency": "USD"},
            "diluted_shares": {"value": "100", "unit": "millions of shares"},
            "cash": {"value": "900", "unit": "USD millions", "currency": "USD"},
            "total_debt": {"value": "400", "unit": "USD millions", "currency": "USD"},
            "ttm_revenue": {"value": "4500", "unit": "USD millions", "currency": "USD"},
            "ttm_ebitda": {"value": "1160", "unit": "USD millions", "currency": "USD"},
            "ttm_ebit": {"value": "975", "unit": "USD millions", "currency": "USD"},
            "ttm_net_income": {"value": "790", "unit": "USD millions", "currency": "USD"},
            "ttm_fcf": {"value": "645", "unit": "USD millions", "currency": "USD"},
            "ttm_sbc": {"value": "285", "unit": "USD millions", "currency": "USD"},
        },
        worker_role="worker_guidance",
    )
    assert val_res["status"] == "ok"
    assert val_res["enterprise_value"]["value"] == "13500.00"

    # 4) Invalid tool args return structured recovery hint (never crash)
    bad_tbl = runner.call_tool(
        "sec_filing_tables",
        {"ticker": "ACME", "accession": "short"},
        worker_role="worker_segments",
    )
    assert bad_tbl["status"] == "failed"
    assert bad_tbl["reason"] == "invalid_tool_arguments"
    assert "allowed_keys" in bad_tbl and "validation_errors" in bad_tbl and "hint" in bad_tbl

    # 5) Worker loop turns malformed tc.raw_arguments JSON into tool_arguments_invalid_json
    snap, _ = _prefetched_snapshot(deps)
    seen_tool_feedback: list[dict[str, Any]] = []

    def model_fn(mreq: SlotModelRequest) -> SlotModelResponse:
        if mreq.stage == "stage_1_plan":
            return SlotModelResponse(parsed=ResearchDispatchPlan(
                tasks=[WorkerTask(worker_role="worker_segments", objective="Test malformed tool raw_arguments", soft_turn_limit=3)]
            ))
        if mreq.stage == "stage_2_workers":
            if mreq.turn_index == 0:
                return SlotModelResponse(tool_calls=(
                    ModelToolCall("tc_bad_json", "read_sources", None, raw_arguments="{bad json"),
                ))
            seen_tool_feedback.extend(json.loads(m["content"]) for m in mreq.messages if m.get("role") == "tool")
            return SlotModelResponse(parsed=EvidenceCardBatch(task_id="t", worker_role="worker_segments", cards=[]))
        if mreq.stage == "stage_2_5_review":
            return SlotModelResponse(parsed=Stage25ReviewDecision(has_cross_worker_leads=False))
        if mreq.stage == "stage_3a_bear":
            return SlotModelResponse(parsed=BearCaseBatch(items=[]))
        return SlotModelResponse(content="# Memo")

    orch = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(model_fn),
        tool_runner=runner,
        prefetch_deps=PrefetchDependencies(precomputed_snapshot=snap),
    )
    orch.run(SquadPrefetchRequest(question="Bad tool JSON recovery", cik="0001234567", symbol="ACME"))
    assert len(seen_tool_feedback) == 1
    assert seen_tool_feedback[0]["reason"] == "tool_arguments_invalid_json"


# --------------------------------------------------------------------------- 7. Source Eligibility (as_of / model_input / FRED / simulated)
def test_source_eligibility_as_of_fred_model_input_and_simulated_isolation() -> None:
    deps, sec_router, _fmp = _offline_sec_fmp_deps(include_fmp_key=False)
    toolbox = make_toolbox(sec=sec_router, sec_stream=_stream_from_router(sec_router))
    as_of_d = date(2025, 11, 10)
    as_of_dt = datetime(2025, 11, 10, 23, 59, 59, tzinfo=timezone.utc)

    sources = [
        ResearchSource(
            source_id="src_ok",
            owner_id="acct-1",
            title="Valid Historical Filing",
            text="Valid historical revenue $1,200M.",
            kind="official",
            published_at=as_of_dt - timedelta(days=2),
            obtained_at=NOW,
            content_sha256="a" * 64,
            permissions=SourcePermissions(store=True, model_input=True, display=True, export=True),
        ),
        ResearchSource(
            source_id="src_no_model",
            owner_id="acct-1",
            title="Restricted Note",
            text="Do not feed to model.",
            kind="user_supplied",
            published_at=as_of_dt - timedelta(days=1),
            obtained_at=NOW,
            content_sha256="b" * 64,
            permissions=SourcePermissions(store=True, model_input=False, display=True, export=False),
        ),
        ResearchSource(
            source_id="src_sim",
            owner_id="acct-1",
            title="Simulated Scenario",
            text="Synthetic revenue $9,999M.",
            kind="user_supplied",
            is_simulated=True,
            published_at=as_of_dt - timedelta(days=1),
            obtained_at=NOW,
            content_sha256="c" * 64,
            permissions=SourcePermissions(store=True, model_input=True, display=True, export=True),
        ),
        ResearchSource(
            source_id="src_fred",
            owner_id="acct-1",
            title="FRED CPI",
            text="FRED time series restricted.",
            url="https://fred.stlouisfed.org/series/CPIAUCSL",
            kind="official",
            published_at=as_of_dt - timedelta(days=5),
            obtained_at=NOW,
            content_sha256="d" * 64,
            permissions=SourcePermissions(store=True, model_input=True, display=True, export=True),
        ),
        ResearchSource(
            source_id="src_future",
            owner_id="acct-1",
            title="Post Cutoff Filing",
            text="Future event leak.",
            kind="official",
            published_at=as_of_dt + timedelta(days=3),
            obtained_at=NOW,
            content_sha256="e" * 64,
            permissions=SourcePermissions(store=True, model_input=True, display=True, export=True),
        ),
    ]
    runner = FinToolboxToolRunner(toolbox, source_by_id={s.source_id: s for s in sources}, as_of=as_of_d)

    read_res = runner.call_tool(
        "read_sources",
        {
            "reads": [
                {"source_id": "src_ok", "start": 0, "end": 100},
                {"source_id": "src_no_model", "start": 0, "end": 100},
                {"source_id": "src_sim", "start": 0, "end": 100},
                {"source_id": "src_fred", "start": 0, "end": 100},
                {"source_id": "src_future", "start": 0, "end": 100},
            ],
            "rationale": "Test source policy enforcement",
        },
        worker_role="worker_segments",
    )
    by_sid = {r["source_id"]: r for r in read_res["reads"]}
    assert by_sid["src_ok"]["status"] == "ok"
    assert by_sid["src_no_model"]["status"] == "denied_or_unavailable" and by_sid["src_no_model"]["reason"] == "model_permission_missing"
    assert by_sid["src_sim"]["status"] == "denied_or_unavailable" and by_sid["src_sim"]["reason"] == "simulated"
    assert by_sid["src_fred"]["status"] == "denied_or_unavailable" and by_sid["src_fred"]["reason"] == "source_not_authorized"
    assert by_sid["src_future"]["status"] == "denied_or_unavailable" and by_sid["src_future"]["reason"] == "availability_unestablished"

    assert runner.call_tool("search_source", {"source_id": "src_fred", "query": "FRED"}, worker_role="worker_segments")["status"] == "denied_or_unavailable"
    assert runner.call_tool("outline_source", {"source_id": "src_future"}, worker_role="worker_segments")["status"] == "denied_or_unavailable"

    def model_fn(mreq: SlotModelRequest) -> SlotModelResponse:
        if mreq.stage == "stage_1_plan":
            return SlotModelResponse(parsed=ResearchDispatchPlan(
                tasks=[WorkerTask(worker_role="worker_segments", objective="Check valid source")]
            ))
        if mreq.stage == "stage_2_workers":
            return SlotModelResponse(parsed=EvidenceCardBatch(task_id="t", worker_role="worker_segments", cards=[
                EvidenceCard(
                    card_id="c_ok",
                    worker_role="worker_segments",
                    topic_tag="rev",
                    finding="Valid historical revenue $1,200M.",
                    citations=[EvidenceCitation(source_id="src_ok", quotation="Valid historical revenue $1,200M.")],
                ),
                EvidenceCard(
                    card_id="c_sneak_fred",
                    worker_role="worker_segments",
                    topic_tag="fred",
                    finding="Sneak FRED citation.",
                    citations=[EvidenceCitation(source_id="src_fred", quotation="FRED time series restricted.")],
                ),
            ]))
        if mreq.stage == "stage_2_5_review":
            return SlotModelResponse(parsed=Stage25ReviewDecision(has_cross_worker_leads=False))
        if mreq.stage == "stage_3a_bear":
            return SlotModelResponse(parsed=BearCaseBatch(items=[]))
        return SlotModelResponse(content="# Policy Clean Memo")

    orch = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(model_fn),
        tool_runner=runner,
        prefetch_deps=deps,
    )
    dossier = orch.run(SquadPrefetchRequest(
        question="Source policy test",
        cik="0001234567",
        symbol="ACME",
        as_of=as_of_d,
        period_financials=_sample_periods(),
    ))
    assert [c.card_id for c in dossier.evidence_cards] == ["c_ok"]
    assert any(g.code.startswith("source_excluded_") for g in dossier.open_gaps)
    prefetched_ids = {s.source_id for s in dossier.snapshot.prefetched_sources}
    assert "src_ok" in prefetched_ids
    assert prefetched_ids.isdisjoint({"src_no_model", "src_sim", "src_fred", "src_future"})


# --------------------------------------------------------------------------- 8. Mandatory Wrap-Up Turn on soft_turn_limit (2 and 1)
@pytest.mark.parametrize("limit", [2, 1])
def test_worker_soft_turn_limit_mandatory_wrapup_turn(limit: int) -> None:
    deps, sec_router, _fmp = _offline_sec_fmp_deps(include_fmp_key=False)
    toolbox = make_toolbox(sec=sec_router, sec_stream=_stream_from_router(sec_router))
    runner = FinToolboxToolRunner(toolbox)
    worker_reqs: list[SlotModelRequest] = []

    def model_fn(mreq: SlotModelRequest) -> SlotModelResponse:
        if mreq.stage == "stage_1_plan":
            return SlotModelResponse(parsed=ResearchDispatchPlan(
                tasks=[WorkerTask(worker_role="worker_segments", objective="Read 8-K and wrap up", soft_turn_limit=limit)]
            ))
        if mreq.stage == "stage_2_workers":
            worker_reqs.append(mreq)
            if mreq.turn_index == 0:
                return SlotModelResponse(tool_calls=(
                    ModelToolCall("tc_r", "read_sources", {"reads": [{"source_id": "src_8k_release_0001234567", "start": 0, "end": 500}], "rationale": "Read EX-99.1"}),
                ))
            assert mreq.tools == []
            assert "FINAL WRAP-UP TURN (tools disabled)" in mreq.user_prompt
            return SlotModelResponse(parsed=EvidenceCardBatch(
                task_id="t",
                worker_role="worker_segments",
                cards=[EvidenceCard(
                    card_id=f"c_wrap_{limit}",
                    worker_role="worker_segments",
                    topic_tag="seg",
                    finding="Cloud Subscriptions grew 30.8% YoY to $850M.",
                    citations=[EvidenceCitation(
                        source_id="src_8k_release_0001234567",
                        quotation="Cloud Subscriptions grew 30.8% year-over-year to $850 million.",
                    )],
                )],
            ))
        if mreq.stage == "stage_2_5_review":
            return SlotModelResponse(parsed=Stage25ReviewDecision(has_cross_worker_leads=False))
        if mreq.stage == "stage_3a_bear":
            return SlotModelResponse(parsed=BearCaseBatch(items=[]))
        return SlotModelResponse(content="# Wrap-up Memo")

    orch = EarningsSquadOrchestrator(
        model_runner=CallableSlotModelRunner(model_fn),
        tool_runner=runner,
        prefetch_deps=deps,
    )
    dossier = orch.run(SquadPrefetchRequest(
        question="Test wrap-up turn",
        cik="0001234567",
        symbol="ACME",
        period_financials=_sample_periods(),
    ))
    assert len(worker_reqs) == 2
    assert len(worker_reqs[0].tools) >= 1
    assert worker_reqs[1].tools == []
    assert [c.card_id for c in dossier.evidence_cards] == [f"c_wrap_{limit}"]


# --------------------------------------------------------------------------- 9. Worker Tool Schema Sanitization & Normalizer Resilience
def test_all_worker_tool_schemas_valid_and_lookaround_free() -> None:
    from app.research_earnings_squad import (
        WORKER_ALLOWED_TOOLS,
        _tool_def_dict,
    )
    from app.research_tools_registry import TOOL_REGISTRY

    def _walk(node: object) -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            if "pattern" in node and isinstance(node["pattern"], str) and "(?" in node["pattern"]:
                found.append(node["pattern"])
            for v in node.values():
                found.extend(_walk(v))
        elif isinstance(node, list):
            for item in node:
                found.extend(_walk(item))
        return found

    all_worker_tools = sorted({t for pool in WORKER_ALLOWED_TOOLS.values() for t in pool})
    assert len(all_worker_tools) >= 10
    for name in all_worker_tools:
        assert name in TOOL_REGISTRY
        tdef = _tool_def_dict(name)
        schema = tdef["input_schema"]
        assert isinstance(schema, dict) and schema.get("type") == "object"
        assert isinstance(schema.get("properties"), dict) and len(schema["properties"]) >= 1
        assert _walk(schema) == [], f"Lookaround regex left in worker tool schema {name}"


def test_squad_normalizers_clamp_turn_limit_blank_ids_and_rejected_bears() -> None:
    from app.research_earnings_squad import (
        _normalize_bear_batch,
        _normalize_evidence_card_dict,
        _normalize_worker_batch_payload,
        _normalize_worker_task_dict,
    )

    notes: list[str] = []
    norm_task = _normalize_worker_task_dict({
        "worker_role": "worker_segments",
        "objective": "Analyze segments",
        "soft_turn_limit": 12,
        "round_number": 5,
    }, notes=notes)
    assert norm_task["soft_turn_limit"] == 8
    assert norm_task["round_number"] == 2
    assert any("soft_turn_limit" in n for n in notes)

    task_obj = WorkerTask(worker_role="worker_segments", objective="Analyze segments")
    card = _normalize_evidence_card_dict({
        "card_id": "   ",
        "topic_tag": "rev",
        "finding": "Revenue was $100M.",
        "citations": [{"source_id": "s1", "quotation": "Revenue was $100M."}],
    }, role="worker_segments", round_number=1, idx=0, notes=notes)
    assert card["card_id"] == "worker_segments_r1_c1"

    batch = _normalize_worker_batch_payload({
        "task_id": "",
        "cards": [card],
    }, task=task_obj, role="worker_segments", notes=notes)
    assert batch["task_id"] == task_obj.task_id

    bear_batch = _normalize_bear_batch({
        "items": [],
        "rejected_items": [{
            "bear_id": "b1",
            "reason": "Missing citation",
            "extra_noise": "drop me",
        }],
    }, notes=notes)
    assert bear_batch["rejected_items"][0]["rejection_reason"] == "Missing citation"
    assert "extra_noise" not in bear_batch["rejected_items"][0]

