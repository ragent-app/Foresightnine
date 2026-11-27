"""Integration tests for Phase A2 Scheme 2 FastAPI routes, SSE org_graph_event, and multi-format export."""
from __future__ import annotations

import asyncio
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import io
import json
import socket
import threading
import time
from typing import Any
from uuid import uuid4
import xml.etree.ElementTree as ET
import zipfile

from fastapi.testclient import TestClient
import httpx
import pytest
import uvicorn

import app.main as api
from app.earnings_external_data import EarningsConsensus
from app.fin_data.models import Coverage, Permissions, Provenance
from app.fin_data.adapters.sec_deep import (
    DualPeriodEarningsRelease,
    EarningsReleaseExhibit,
    EarningsReleasePeriod,
    FilingRedlineReport,
    FilingReportEntry,
    FilingTableCatalog,
    ParsedHtmlTable,
    SectionRedlineDiff,
)
from app.model_protocol import ModelResponse, ModelToolCall, ModelUsage
from app.operations import ApiProblem
from app.research_earnings_diagnostics import (
    GuidanceAndNonGaapRequest,
    GuidanceMetricInput,
    GuidanceRangeInput,
    MetricValue,
    NonGaapAdjustmentItem,
    NonGaapBridgeInput,
    PeriodFinancials,
    ValuationRequest,
    earnings_quality_diagnostics,
    EarningsQualityRequest,
    guidance_and_nongaap_reconciler,
    valuation_and_implied_expectations,
)
from app.research_earnings_export import (
    ExportUnavailableError,
    XLSX_SHEET_NAMES,
    export_earnings_dossier,
)
from app.research_earnings_squad import (
    BearCaseBatch,
    BearCaseItem,
    ConsensusSnapshotSummary,
    DossierGateReport,
    EarningsDossier,
    EarningsPrefetchSnapshot,
    EarningsSquadOrchestrator,
    EvidenceCard,
    EvidenceCardBatch,
    EvidenceCitation,
    OrgEdgeEvent,
    OrgGraphEvent,
    PrefetchDependencies,
    PrefetchedSourceRef,
    ResearchDispatchPlan,
    SlotBinding,
    SlotModelRequest,
    SlotModelResponse,
    SlotUsageRecord,
    SlotUsageSummary,
    SquadGap,
    Stage25ReviewDecision,
    SteeringImpactDecision,
    TranscriptIndexEntry,
    WorkerTask,
    CallableSlotModelRunner,
)
from app.research_live_stream import (
    ResearchLiveBroker,
    format_org_graph_sse_frame,
    normalize_org_graph_event,
    parse_org_last_event_id,
)


_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


@pytest.fixture(autouse=True)
def _clean_api_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    # Dossier delivery tests use frozen filings. The independent governance
    # prewarmer would otherwise schedule a live SEC request after delivery.
    monkeypatch.setattr(api, "_prewarm_v25_layer1_async", lambda _dossier: None)
    api.reset_state()
    api.set_earnings_squad_runner_hook(None)
    yield
    api.set_earnings_squad_runner_hook(None)
    api.reset_state()


@pytest.fixture
def live_server():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        api.app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        timeout_graceful_shutdown=0.5,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while not server.started and time.monotonic() < deadline:
        if not thread.is_alive():
            break
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2.0)
        sock.close()
        raise RuntimeError("uvicorn test server failed to start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=3.0)
        try:
            sock.close()
        except OSError:
            pass


def _register_user(client: TestClient) -> dict[str, str]:
    resp = client.post(
        "/v1/auth/local/register",
        json={
            "email": f"u-{uuid4().hex[:10]}@example.test",
            "password": "correct horse battery staple!",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _create_topic(client: TestClient, headers: dict[str, str], *, question: str = "Analyze Q2 FY26 ACME earnings quality", source_ids: list[str] | None = None) -> str:
    resp = client.post(
        "/v2/research/topics",
        headers=headers,
        json={
            "question": question,
            "source_ids": source_ids or [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["topic_id"]


def _parse_sse_frames(raw_lines: list[str]) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    event_name: str | None = None
    event_id: str | None = None
    data_lines: list[str] = []
    for line in raw_lines:
        if line == "":
            if event_name is not None or data_lines:
                payload = "\n".join(data_lines)
                frames.append({
                    "event": event_name,
                    "id": event_id,
                    "data": json.loads(payload) if payload else None,
                })
                event_name = None
                event_id = None
                data_lines = []
            continue
        if line.startswith(":"):
            frames.append({"comment": line[1:].strip()})
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("id:"):
            event_id = line[3:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if event_name is not None or data_lines:
        payload = "\n".join(data_lines)
        frames.append({
            "event": event_name,
            "id": event_id,
            "data": json.loads(payload) if payload else None,
        })
    return frames


def _build_rich_dossier(*, restrict_byok_export: bool = True, include_deep_report: bool = False) -> EarningsDossier:
    p_cur = PeriodFinancials(
        period_label="Q2 FY26",
        period_type="quarter",
        period_end=date(2026, 6, 30),
        currency="USD",
        monetary_unit="USD millions",
        revenue=MetricValue(value=Decimal("1000"), unit="USD millions", currency="USD", fact_id="f:rev_q2"),
        cost_of_revenue=MetricValue(value=Decimal("420"), unit="USD millions", currency="USD"),
        gross_profit=MetricValue(value=Decimal("580"), unit="USD millions", currency="USD"),
        rd_expense=MetricValue(value=Decimal("160"), unit="USD millions", currency="USD"),
        sga_expense=MetricValue(value=Decimal("190"), unit="USD millions", currency="USD"),
        operating_income=MetricValue(value=Decimal("230"), unit="USD millions", currency="USD"),
        ebit=MetricValue(value=Decimal("230"), unit="USD millions", currency="USD"),
        interest_expense=MetricValue(value=Decimal("10"), unit="USD millions", currency="USD"),
        net_income=MetricValue(value=Decimal("180"), unit="USD millions", currency="USD"),
        ebitda=MetricValue(value=Decimal("270"), unit="USD millions", currency="USD"),
        depreciation_amortization=MetricValue(value=Decimal("40"), unit="USD millions", currency="USD"),
        sbc=MetricValue(value=Decimal("60"), unit="USD millions", currency="USD"),
        operating_cash_flow=MetricValue(value=Decimal("150"), unit="USD millions", currency="USD"),
        capex=MetricValue(value=Decimal("30"), unit="USD millions", currency="USD"),
        free_cash_flow=MetricValue(value=Decimal("120"), unit="USD millions", currency="USD"),
        cash=MetricValue(value=Decimal("900"), unit="USD millions", currency="USD"),
        accounts_receivable=MetricValue(value=Decimal("260"), unit="USD millions", currency="USD"),
        inventory=MetricValue(value=Decimal("140"), unit="USD millions", currency="USD"),
        current_assets=MetricValue(value=Decimal("1400"), unit="USD millions", currency="USD"),
        accounts_payable=MetricValue(value=Decimal("110"), unit="USD millions", currency="USD"),
        total_debt=MetricValue(value=Decimal("300"), unit="USD millions", currency="USD"),
        current_liabilities=MetricValue(value=Decimal("500"), unit="USD millions", currency="USD"),
        shares_diluted=MetricValue(value=Decimal("100"), unit="millions shares"),
    )
    p_yoy = PeriodFinancials(
        period_label="Q2 FY25",
        period_type="quarter",
        period_end=date(2025, 6, 30),
        currency="USD",
        monetary_unit="USD millions",
        revenue=MetricValue(value=Decimal("850"), unit="USD millions", currency="USD"),
        cost_of_revenue=MetricValue(value=Decimal("320"), unit="USD millions", currency="USD"),
        gross_profit=MetricValue(value=Decimal("530"), unit="USD millions", currency="USD"),
        operating_income=MetricValue(value=Decimal("210"), unit="USD millions", currency="USD"),
        net_income=MetricValue(value=Decimal("170"), unit="USD millions", currency="USD"),
        operating_cash_flow=MetricValue(value=Decimal("200"), unit="USD millions", currency="USD"),
        capex=MetricValue(value=Decimal("25"), unit="USD millions", currency="USD"),
        accounts_receivable=MetricValue(value=Decimal("175"), unit="USD millions", currency="USD"),
        inventory=MetricValue(value=Decimal("90"), unit="USD millions", currency="USD"),
        accounts_payable=MetricValue(value=Decimal("95"), unit="USD millions", currency="USD"),
        shares_diluted=MetricValue(value=Decimal("98"), unit="millions shares"),
    )
    diag = earnings_quality_diagnostics(EarningsQualityRequest(
        entity_id="cik:0000320193",
        symbol="ACME",
        sic="7372",
        current=p_cur,
        prior_yoy=p_yoy,
    ))
    guidance = guidance_and_nongaap_reconciler(GuidanceAndNonGaapRequest(
        entity_id="cik:0000320193",
        symbol="ACME",
        period_label="Q2 FY26",
        guidance_metrics=[
            GuidanceMetricInput(
                metric_name="revenue",
                period_label="Q2 FY26",
                actual=MetricValue(value=Decimal("1000"), unit="USD millions", currency="USD", fact_id="f:rev_q2"),
                prior_guidance=GuidanceRangeInput(
                    low=MetricValue(value=Decimal("960"), unit="USD millions", currency="USD"),
                    high=MetricValue(value=Decimal("990"), unit="USD millions", currency="USD"),
                ),
                consensus=MetricValue(value=Decimal("987654.32"), unit="USD millions", currency="USD"),
                new_guidance=GuidanceRangeInput(
                    low=MetricValue(value=Decimal("950"), unit="USD millions", currency="USD"),
                    high=MetricValue(value=Decimal("980"), unit="USD millions", currency="USD"),
                ),
            ),
        ],
        nongaap_bridges=[
            NonGaapBridgeInput(
                metric_name="operating_income",
                period_label="Q2 FY26",
                gaap_value=MetricValue(value=Decimal("230"), unit="USD millions", currency="USD"),
                reported_non_gaap_value=MetricValue(value=Decimal("295"), unit="USD millions", currency="USD"),
                adjustments=[
                    NonGaapAdjustmentItem(
                        label="Stock-based compensation",
                        amount=MetricValue(value=Decimal("60"), unit="USD millions", currency="USD"),
                        category="sbc",
                        prior_periods_present=4,
                    ),
                    NonGaapAdjustmentItem(
                        label="Restructuring charge",
                        amount=MetricValue(value=Decimal("5"), unit="USD millions", currency="USD"),
                        category="restructuring",
                    ),
                ],
            ),
        ],
    ))
    valuation = valuation_and_implied_expectations(ValuationRequest(
        entity_id="cik:0000320193",
        symbol="ACME",
        sic="7372",
        currency="USD",
        monetary_unit="USD millions",
        close_price=MetricValue(value=Decimal("50"), unit="USD", currency="USD"),
        diluted_shares=p_cur.shares_diluted,
        cash=p_cur.cash,
        total_debt=p_cur.total_debt,
        ttm_revenue=MetricValue(value=Decimal("3800"), unit="USD millions", currency="USD"),
        ttm_ebitda=MetricValue(value=Decimal("1000"), unit="USD millions", currency="USD"),
        ttm_ebit=MetricValue(value=Decimal("840"), unit="USD millions", currency="USD"),
        ttm_net_income=MetricValue(value=Decimal("680"), unit="USD millions", currency="USD"),
        ttm_fcf=MetricValue(value=Decimal("450"), unit="USD millions", currency="USD"),
        ttm_sbc=MetricValue(value=Decimal("220"), unit="USD millions", currency="USD"),
        ntm_revenue=MetricValue(value=Decimal("987654.32"), unit="USD millions", currency="USD"),
        ntm_eps=MetricValue(value=Decimal("9.87"), unit="USD", currency="USD"),
    ))
    release = DualPeriodEarningsRelease(
        entity_id="cik:0000320193",
        cik="0000320193",
        current=EarningsReleasePeriod(
            period_role="current",
            accession="0000320193-26-000055",
            filed_at=date(2026, 7, 29),
            index_json_url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000055/index.json",
            ex99_1=EarningsReleaseExhibit(
                exhibit_type="EX-99.1",
                filename="ex99-1.htm",
                url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000055/ex99-1.htm",
                text="ACME Q2 FY26 Cloud revenue reached $720M, up 24% YoY; total revenue $1,000M.",
                tables=[
                    ParsedHtmlTable(
                        title="Segment Revenue Breakdown",
                        headers=["Segment", "Q2 FY26 ($M)", "Q2 FY25 ($M)"],
                        rows=[["Cloud Subscriptions", "720", "580"], ["Services", "280", "270"]],
                    )
                ],
                content_sha256="a" * 64,
            ),
        ),
    )
    catalog = FilingTableCatalog(
        entity_id="cik:0000320193",
        cik="0000320193",
        accession="0000320193-26-000055",
        filing_summary_url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000055/FilingSummary.xml",
        report_count=1,
        statements=[
            FilingReportEntry(
                r_file="R2.htm",
                short_name="Consolidated Statements of Operations",
                long_name="0000002 - Statement - Consolidated Statements of Operations",
                menu_category="Statements",
                normalized_category="Statements",
                url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000055/R2.htm",
            )
        ],
        all_reports=[
            FilingReportEntry(
                r_file="R2.htm",
                short_name="Consolidated Statements of Operations",
                long_name="0000002 - Statement - Consolidated Statements of Operations",
                menu_category="Statements",
                normalized_category="Statements",
                url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000055/R2.htm",
            )
        ],
    )
    byok_perm = Permissions(store=True, display=True, model_input=True, export=not restrict_byok_export, owner_scoped=True)
    consensus_summary = ConsensusSnapshotSummary(
        status="available",
        source_id="fmp_consensus",
        provider="fmp",
        permissions={"store": True, "display": True, "model_input": True, "export": not restrict_byok_export},
        records=[
            EarningsConsensus(
                symbol="ACME",
                fiscal_period="NTM",
                revenue_estimate_avg=Decimal("987654.32"),
                eps_estimate_avg=Decimal("9.87"),
                source_provider="fmp",
                permissions=byok_perm,
            )
        ],
    )
    transcript_index = TranscriptIndexEntry(
        status="available",
        symbol="ACME",
        fiscal_year=2026,
        fiscal_quarter=2,
        sections=["prepared_remarks", "qa"],
        speakers=["Jane Doe (CEO)", "SECRET_CFO_NAME"],
        source_id="fmp_transcript",
        provider="fmp",
        permissions={"store": True, "display": True, "model_input": True, "export": not restrict_byok_export},
    )
    snap = EarningsPrefetchSnapshot(
        entity_id="cik:0000320193",
        symbol="ACME",
        cik="0000320193",
        company_name="ACME Cloud Corp.",
        sic_code="7372",
        sic_description="Prepackaged Software",
        period_label="Q2 FY26",
        sector_applicability=diag.sector_applicability,
        statements=[p_cur, p_yoy],
        diagnostics=diag,
        flags=diag.flags,
        earnings_release=release,
        filing_accession="0000320193-26-000055",
        filing_table_catalog=catalog,
        redline_summary=FilingRedlineReport(
            section_diffs={"item_1a": SectionRedlineDiff(section_key="item_1a", has_substantive_changes=True)},
            total_added=2,
            total_removed=1,
            total_revised=3,
        ),
        guidance_nongaap=guidance,
        valuation=valuation,
        consensus_summary=consensus_summary,
        transcript_index=transcript_index,
        prefetched_sources=[
            PrefetchedSourceRef(
                source_id="sec_ex99_1",
                title="ACME Q2 FY26 8-K EX-99.1",
                kind="official",
                retrieval_method="sec_edgar",
                content_sha256="a" * 64,
                permissions={"store": True, "display": True, "model_input": True, "export": True},
                total_characters=90,
            ),
            PrefetchedSourceRef(
                source_id="fmp_transcript",
                title="ACME Q2 FY26 FMP Call Transcript",
                kind="market_data",
                retrieval_method="fmp_transcript",
                content_sha256="b" * 64,
                permissions={"store": True, "display": True, "model_input": True, "export": not restrict_byok_export},
                total_characters=200,
            ),
        ],
    )
    sec_card = EvidenceCard(
        card_id="card_seg_1",
        worker_role="worker_segments",
        topic_tag="cloud_revenue",
        finding="Cloud Subscriptions grew +24% YoY to $720M, driving 72% of total $1,000M revenue.",
        supporting_fact_ids=["f:rev_q2"],
        citations=[
            EvidenceCitation(
                source_id="sec_ex99_1",
                quotation="ACME Q2 FY26 Cloud revenue reached $720M, up 24% YoY; total revenue $1,000M.",
                location="EX-99.1 p.1",
                content_sha256="a" * 64,
            )
        ],
        numeric_claims=[{"metric": "Cloud Revenue", "value": 720, "unit": "USD millions", "yoy": "+24%"}],
    )
    byok_card = EvidenceCard(
        card_id="card_trans_1",
        worker_role="worker_guidance",
        topic_tag="call_qa_tone",
        finding="SECRET_CFO_NAME acknowledged pipeline slippage on call.",
        supporting_fact_ids=["fmp_transcript:qa1"],
        citations=[
            EvidenceCitation(
                source_id="fmp_transcript",
                quotation="SECRET_TRANSCRIPT_QUOTE_XYZ about Q3 billings deceleration.",
                location="QA Turn 3",
                content_sha256="b" * 64,
            )
        ],
    )
    anchor_id = diag.flags[0].flag_id if diag.flags else "card_seg_1"
    bear = BearCaseItem(
        bear_id="bear_wc_1",
        thesis_challenge="Working capital absorption lowered OCF/NI conversion below 85% while Q3 guide midpoint trimmed.",
        anchored_evidence_ids=[anchor_id, "guidance:revenue"],
        anchor_kind="flag",
        next_quarter_invalidation_trigger="Q3 FY26 DSO improves >= 5 days and OCF/NI recovers above 1.0x.",
        supporting_numbers=["DSO elevated YoY", "OCF $150M vs NI $180M"],
        severity="high",
    )
    gate_report = DossierGateReport(
        passed=True,
        verified_card_ids=["card_seg_1", "card_trans_1"],
        verified_bear_ids=["bear_wc_1"],
        exportable=not restrict_byok_export,
        non_exportable_sources=["fmp_consensus", "fmp_transcript"] if restrict_byok_export else [],
    )
    usage_slots = {
        slot: SlotUsageRecord(slot=slot, model="test-model", protocol="openai_chat_completions", fallback_used=False, calls=1, total_tokens=100)
        for slot in ("fast_extractor", "deep_reasoner", "bear_reviewer", "writer")
    }
    memo_md = (
        "# Buy-Side Earnings Memo: ACME (Q2 FY26)\n\n"
        "- Cloud revenue reached $720M (+24% YoY) out of $1,000M total revenue.\n"
        "- Transcript callout: SECRET_TRANSCRIPT_QUOTE_XYZ from SECRET_CFO_NAME.\n"
        "- Consensus revenue estimate tracked at 987654.32 via fmp_consensus.\n"
    )
    deep_md = (
        "# Deep Earnings Research Report: ACME (Q2 FY26)\n\n"
        "## Executive Thesis\n"
        "Cloud revenue hit $720M (+24% YoY).\n"
        "Call note: SECRET_TRANSCRIPT_QUOTE_XYZ.\n"
    ) if include_deep_report else None

    return EarningsDossier(
        dossier_id="dossier_acme_q2",
        run_id="sqrun_acme_q2",
        entity_id="cik:0000320193",
        symbol="ACME",
        cik="0000320193",
        company_name="ACME Cloud Corp.",
        period_label="Q2 FY26",
        selected_l2_pack_id="kpi_saas_cloud",
        snapshot=snap,
        dispatch_plan=ResearchDispatchPlan(
            selected_l2_pack_id="kpi_saas_cloud",
            selected_l2_pack_reason="Matched SIC 7372 to kpi_saas_cloud.",
            tasks=[
                WorkerTask(worker_role="worker_segments", objective="Extract cloud segment KPIs."),
                WorkerTask(worker_role="worker_forensics", objective="Inspect working capital & margins."),
                WorkerTask(worker_role="worker_guidance", objective="Check guidance vs prior & consensus."),
            ],
        ),
        evidence_cards=[sec_card, byok_card],
        bear_cases=[bear],
        open_gaps=[
            SquadGap(stage="stage_0_prefetch", code="sample_gap", message="CRPO breakdown omitted in EX-99.1.")
        ],
        sector_applicability=diag.sector_applicability,
        gate_report=gate_report,
        memo_markdown=memo_md,
        deep_report_markdown=deep_md,
        slot_usage_summary=SlotUsageSummary(
            slots=usage_slots,
            total_calls=4,
            total_tokens=400,
            stage_0_model_calls=0,
            deep_report_tool_calls=0,
        ),
        locked=True,
    )


def _read_xlsx_workbook(raw_bytes: bytes) -> tuple[list[str], dict[str, list[list[str]]]]:
    """Parse OpenXML .xlsx bytes strictly using stdlib zipfile + ElementTree."""
    with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
        names = set(zf.namelist())
        for req in ("[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/styles.xml"):
            assert req in names, f"Missing required OpenXML part: {req}"
        # Validate every XML part is well-formed XML
        for member in names:
            if member.endswith(".xml") or member.endswith(".rels"):
                ET.fromstring(zf.read(member))

        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in names:
            sst_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in sst_root.findall(f"{_NS_MAIN}si"):
                parts = [t.text or "" for t in si.iter(f"{_NS_MAIN}t")]
                shared_strings.append("".join(parts))

        rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rel_targets: dict[str, str] = {}
        for rel in rels_root:
            rid = rel.attrib.get("Id")
            target = rel.attrib.get("Target")
            if rid and target:
                rel_targets[rid] = f"xl/{target.lstrip('/')}"

        wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
        sheet_names: list[str] = []
        sheet_rows: dict[str, list[list[str]]] = {}
        for sheet_el in wb_root.findall(f".//{_NS_MAIN}sheet"):
            title = sheet_el.attrib["name"]
            rid = sheet_el.attrib.get(f"{_NS_REL}id")
            assert rid and rid in rel_targets
            sheet_names.append(title)
            ws_root = ET.fromstring(zf.read(rel_targets[rid]))
            rows_parsed: list[list[str]] = []
            for row_el in ws_root.findall(f".//{_NS_MAIN}row"):
                row_vals: list[str] = []
                for c_el in row_el.findall(f"{_NS_MAIN}c"):
                    ctype = c_el.attrib.get("t", "n")
                    v_el = c_el.find(f"{_NS_MAIN}v")
                    val_text = v_el.text if v_el is not None and v_el.text is not None else ""
                    if ctype == "s" and val_text.isdigit():
                        row_vals.append(shared_strings[int(val_text)])
                    else:
                        row_vals.append(val_text)
                rows_parsed.append(row_vals)
            sheet_rows[title] = rows_parsed
    return sheet_names, sheet_rows


# --------------------------------------------------------------------------- 1. Export Formats & Permission Enforcement
def test_multi_format_export_and_strict_export_false_redaction():
    dossier = _build_rich_dossier(restrict_byok_export=True, include_deep_report=True)

    # Markdown (Deep Report & Memo)
    md_deep = export_earnings_dossier(dossier, fmt="markdown", report_mode="deep_report")
    assert md_deep.report_mode == "deep_report"
    assert md_deep.media_type == "text/markdown; charset=utf-8"
    assert md_deep.filename.endswith(".md")
    assert md_deep.exportable_unrestricted is False
    assert set(md_deep.redacted_sources) >= {"fmp_consensus", "fmp_transcript"}
    md_text = md_deep.text
    assert "## Export Permissions & Redaction Notice" in md_text
    assert "EXPORT REDACTION ACTIVE" in md_text
    assert "Cloud Subscriptions grew +24% YoY" in md_text
    assert "ACME Q2 FY26 Cloud revenue reached $720M" in md_text
    assert "CRPO breakdown omitted in EX-99.1." in md_text
    # Strict non-leakage checks
    assert "SECRET_TRANSCRIPT_QUOTE_XYZ" not in md_text
    assert "SECRET_CFO_NAME" not in md_text
    assert "987654.32" not in md_text

    # Memo explicit mode check
    md_memo = export_earnings_dossier(dossier, fmt="markdown", report_mode="memo")
    assert md_memo.report_mode == "memo"
    assert "Buy-Side Earnings Memo" in md_memo.text
    assert "SECRET_TRANSCRIPT_QUOTE_XYZ" not in md_memo.text
    assert "SECRET_CFO_NAME" not in md_memo.text

    # CSV export
    csv_res = export_earnings_dossier(dossier, fmt="csv")
    assert csv_res.media_type == "text/csv; charset=utf-8"
    assert csv_res.filename.endswith(".csv")
    csv_text = csv_res.text
    assert "SECRET_TRANSCRIPT_QUOTE_XYZ" not in csv_text
    assert "SECRET_CFO_NAME" not in csv_text
    assert "987654.32" not in csv_text
    parsed_rows = list(csv.reader(io.StringIO(csv_text)))
    sections = {row[0] for row in parsed_rows[1:]}
    assert {
        "export_notice",
        "export_redactions",
        "valuation",
        "quarterly_financials",
        "earnings_quality",
        "segments_and_kpis",
        "guidance_vs_prior_consensus",
        "nongaap_reconciliation",
        "bear_cases",
        "open_gaps",
    } <= sections

    # XLSX multi-sheet workbook export (valid OpenXML zip with exact 6 sheets)
    xlsx_res = export_earnings_dossier(dossier, fmt="xlsx")
    assert xlsx_res.media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert xlsx_res.filename.endswith(".xlsx")
    sheet_names, sheet_rows = _read_xlsx_workbook(xlsx_res.content_bytes)
    assert tuple(sheet_names) == XLSX_SHEET_NAMES

    all_xlsx_text = "\n".join(
        " | ".join(cell for cell in row)
        for rows in sheet_rows.values()
        for row in rows
    )
    assert "EXPORT REDACTION ACTIVE" in all_xlsx_text
    assert "fmp_consensus" in all_xlsx_text
    assert "fmp_transcript" in all_xlsx_text
    assert "Cloud Subscriptions grew +24% YoY" in all_xlsx_text
    assert "SECRET_TRANSCRIPT_QUOTE_XYZ" not in all_xlsx_text
    assert "SECRET_CFO_NAME" not in all_xlsx_text
    assert "987654.32" not in all_xlsx_text

    # Redaction notice appears explicitly in both Summary & Valuation and Bear Case & Sources
    summary_text = "\n".join(" ".join(r) for r in sheet_rows["Summary & Valuation"])
    bear_text = "\n".join(" ".join(r) for r in sheet_rows["Bear Case & Sources"])
    assert "Export Permissions & Redaction Notice" in summary_text
    assert "Export Permissions & Redaction Notice" in bear_text


def test_export_deep_report_unavailable_raises_and_unrestricted_notice():
    dossier = _build_rich_dossier(restrict_byok_export=False, include_deep_report=False)
    with pytest.raises(ExportUnavailableError):
        export_earnings_dossier(dossier, fmt="markdown", report_mode="deep_report")

    auto_res = export_earnings_dossier(dossier, fmt="markdown", report_mode="auto")
    assert auto_res.report_mode == "memo"
    assert auto_res.exportable_unrestricted is True
    assert "0 redactions applied" in auto_res.text


# --------------------------------------------------------------------------- 2. Broker org_graph_event Support
def test_broker_org_graph_event_serialization_dedup_and_backpressure():
    async def scenario():
        broker = ResearchLiveBroker(queue_maxsize=4)
        loop = asyncio.get_running_loop()
        sub = broker.subscribe("sqop-1", account_id="acc-1", loop=loop)

        # Flood queue with transient stream deltas first
        for seq in range(8):
            broker.publish_delta("sqop-1", call_seq=1, channel="thinking", delta=f"delta-{seq}", seq=seq)
        await asyncio.sleep(0)
        assert sub.queue.qsize() == 4

        ev1 = OrgGraphEvent(
            sequence=1,
            stage="stage_1_plan",
            node_id="lead_coordinator",
            role="lead_coordinator",
            display_name="Lead Coordinator",
            runtime_state="running",
            action_kind="DELEGATING",
            current_activity="Dispatching worker_segments.",
            edge_event=OrgEdgeEvent(
                from_node_id="lead_coordinator",
                to_node_id="worker_segments",
                is_review=False,
                edge_kind="dispatch",
            ),
            slot="deep_reasoner",
        )
        ev2 = OrgGraphEvent(
            sequence=2,
            stage="stage_3a_bear",
            node_id="bear_reviewer",
            role="bear_reviewer",
            display_name="Bear Reviewer",
            runtime_state="completed",
            action_kind="INSPECTING",
            current_activity="Completed adversarial review.",
            edge_event=OrgEdgeEvent(
                from_node_id="bear_reviewer",
                to_node_id="lead_coordinator",
                is_review=True,
                edge_kind="review",
            ),
            slot="bear_reviewer",
        )
        # Durable org_graph_event evicts transient deltas and is preserved
        assert broker.publish_org_graph_event("sqop-1", ev1) is not None
        # Duplicate sequence <= 1 is dropped cleanly
        assert broker.publish_org_graph_event("sqop-1", ev1) is None
        assert broker.publish_org_graph_event("sqop-1", ev2) is not None
        await asyncio.sleep(0)

        drained: list[dict[str, Any]] = []
        while not sub.queue.empty():
            drained.append(sub.queue.get_nowait())
        org_frames = [f for f in drained if f["event"] == "org_graph_event"]
        assert len(org_frames) == 2
        assert org_frames[0]["id"] == "org:1"
        assert org_frames[0]["data"]["action_kind"] == "DELEGATING"
        assert org_frames[0]["data"]["edge_event"] == {
            "from_node_id": "lead_coordinator",
            "to_node_id": "worker_segments",
            "is_review": False,
            "edge_kind": "dispatch",
        }
        assert org_frames[1]["id"] == "org:2"
        assert org_frames[1]["data"]["edge_event"]["is_review"] is True

        # Format & cursor helpers
        sse_str = format_org_graph_sse_frame("sqop-1", ev2)
        assert sse_str.startswith("event: org_graph_event\nid: org:2\ndata: ")
        assert parse_org_last_event_id("org:7") == 7
        with pytest.raises(ApiProblem) as exc_info:
            parse_org_last_event_id("org:bad")
        assert exc_info.value.status_code == 400

        # Invalid action_kind rejected
        with pytest.raises(ValueError):
            normalize_org_graph_event({
                "sequence": 3,
                "stage": "stage_1",
                "node_id": "lead",
                "role": "lead",
                "display_name": "Lead",
                "runtime_state": "running",
                "action_kind": "NOT_A_VALID_ACTION",
                "current_activity": "test",
            })

        broker.unsubscribe(sub)

    asyncio.run(scenario())


# --------------------------------------------------------------------------- 3. End-to-End FastAPI Squad Endpoints + Deep Report + Auth + Export
def _make_api_squad_hook(
    *,
    exportable_source_id: str,
    restricted_source_id: str,
    writer_invokes_tool_on_deep: bool = False,
):
    call_log: list[tuple[str, str, str]] = []
    dossier_seed = _build_rich_dossier(restrict_byok_export=True, include_deep_report=False)
    snap = dossier_seed.snapshot.model_copy(
        update={
            "prefetched_sources": [
                PrefetchedSourceRef(
                    source_id=exportable_source_id,
                    title="ACME SEC EX-99.1 Upload",
                    kind="official",
                    retrieval_method="sec_edgar",
                    content_sha256="a" * 64,
                    permissions={"store": True, "display": True, "model_input": True, "export": True},
                    total_characters=90,
                ),
                PrefetchedSourceRef(
                    source_id=restricted_source_id,
                    title="Restricted BYOK Notes",
                    kind="user_supplied",
                    retrieval_method="user_note",
                    content_sha256="c" * 64,
                    permissions={"store": True, "display": True, "model_input": True, "export": False},
                    total_characters=60,
                ),
            ]
        }
    )

    def model_fn(mreq: SlotModelRequest) -> SlotModelResponse:
        call_log.append((mreq.stage, mreq.role, mreq.slot))
        if mreq.role == "lead_coordinator" and mreq.stage == "stage_1_plan":
            return SlotModelResponse(
                parsed=ResearchDispatchPlan(
                    selected_l2_pack_id="kpi_saas_cloud",
                    selected_l2_pack_reason="SIC 7372 SaaS KPIs.",
                    tasks=[
                        WorkerTask(worker_role="worker_segments", objective="Extract cloud segment KPIs."),
                        WorkerTask(worker_role="worker_forensics", objective="Review working capital."),
                        WorkerTask(worker_role="worker_guidance", objective="Review guidance & restricted note."),
                    ],
                ),
                usage=ModelUsage(10, 10, 20),
            )
        if mreq.role == "worker_segments":
            return SlotModelResponse(
                parsed=EvidenceCardBatch(
                    task_id="t1",
                    worker_role="worker_segments",
                    cards=[
                        EvidenceCard(
                            card_id="card_seg_api",
                            worker_role="worker_segments",
                            topic_tag="cloud_revenue",
                            finding="Cloud revenue grew +24% YoY to $720M in Q2 FY26.",
                            supporting_fact_ids=["f:rev_q2"],
                            citations=[
                                EvidenceCitation(
                                    source_id=exportable_source_id,
                                    quotation="ACME Q2 FY26 Cloud revenue reached $720M, up 24% YoY",
                                )
                            ],
                        )
                    ],
                ),
                usage=ModelUsage(10, 10, 20),
            )
        if mreq.role == "worker_forensics":
            return SlotModelResponse(
                parsed=EvidenceCardBatch(
                    task_id="t2",
                    worker_role="worker_forensics",
                    cards=[
                        EvidenceCard(
                            card_id="card_for_api",
                            worker_role="worker_forensics",
                            topic_tag="working_capital",
                            finding="OCF/NI fell below 0.85 due to AR expansion.",
                            supporting_fact_ids=[snap.flags[0].flag_id if snap.flags else "f:rev_q2"],
                            citations=[
                                EvidenceCitation(
                                    source_id=exportable_source_id,
                                    quotation="ACME Q2 FY26 Cloud revenue reached $720M",
                                )
                            ],
                        )
                    ],
                ),
                usage=ModelUsage(10, 10, 20),
            )
        if mreq.role == "worker_guidance":
            return SlotModelResponse(
                parsed=EvidenceCardBatch(
                    task_id="t3",
                    worker_role="worker_guidance",
                    cards=[
                        EvidenceCard(
                            card_id="card_gui_restricted",
                            worker_role="worker_guidance",
                            topic_tag="restricted_note",
                            finding="CONFIDENTIAL_NON_EXPORTABLE_NOTE regarding internal channel checks.",
                            supporting_fact_ids=[f"{restricted_source_id}:note1"],
                            citations=[
                                EvidenceCitation(
                                    source_id=restricted_source_id,
                                    quotation="CONFIDENTIAL_NON_EXPORTABLE_NOTE verbatim excerpt.",
                                )
                            ],
                        )
                    ],
                ),
                usage=ModelUsage(10, 10, 20),
            )
        if mreq.stage == "stage_2_5_review" and mreq.role == "lead_coordinator":
            return SlotModelResponse(
                parsed=Stage25ReviewDecision(has_cross_worker_leads=False, rationale="No follow-up needed."),
                usage=ModelUsage(5, 5, 10),
            )
        if mreq.role == "bear_reviewer":
            anchor = snap.flags[0].flag_id if snap.flags else "card_for_api"
            return SlotModelResponse(
                parsed=BearCaseBatch(
                    items=[
                        BearCaseItem(
                            bear_id="bear_api_1",
                            thesis_challenge="OCF conversion lag and DSO increase threaten Q3 FCF margin above 10%.",
                            anchored_evidence_ids=[anchor],
                            anchor_kind="flag",
                            next_quarter_invalidation_trigger="DSO narrows by 5 days in Q3 FY26.",
                            supporting_numbers=["DSO elevated", "OCF $150M vs NI $180M"],
                        )
                    ]
                ),
                usage=ModelUsage(10, 10, 20),
            )
        if mreq.role == "writer" and mreq.stage == "stage_3b_gate_memo":
            return SlotModelResponse(
                content=(
                    "# Memo: ACME (Q2 FY26)\n"
                    "- Cloud revenue hit $720M (+24% YoY).\n"
                    "- Restricted note: CONFIDENTIAL_NON_EXPORTABLE_NOTE.\n"
                ),
                usage=ModelUsage(10, 10, 20),
            )
        if mreq.role == "writer" and mreq.stage == "deep_report":
            if writer_invokes_tool_on_deep:
                return SlotModelResponse(tool_calls=(ModelToolCall("tc_bad", "sec_filing_tables", {}),))
            return SlotModelResponse(
                content=(
                    "# Deep Report: ACME (Q2 FY26)\n"
                    "- Identical facts: Cloud revenue $720M (+24% YoY), Total revenue $1,000M.\n"
                    "- Bear thesis: OCF conversion lag.\n"
                    "- Excluded check: CONFIDENTIAL_NON_EXPORTABLE_NOTE.\n"
                ),
                usage=ModelUsage(15, 25, 40),
            )
        raise AssertionError(f"Unexpected model call: {mreq}")

    def hook(**_kwargs):
        return {
            "model_runner": CallableSlotModelRunner(model_fn),
            "prefetch_deps": PrefetchDependencies(
                precomputed_snapshot=snap,
                initial_source_texts={
                    exportable_source_id: "ACME Q2 FY26 Cloud revenue reached $720M, up 24% YoY; total revenue $1,000M.",
                    restricted_source_id: "CONFIDENTIAL_NON_EXPORTABLE_NOTE verbatim excerpt.",
                },
                initial_source_permissions={
                    exportable_source_id: {"store": True, "display": True, "model_input": True, "export": True},
                    restricted_source_id: {"store": True, "display": True, "model_input": True, "export": False},
                },
            ),
            "slot_bindings": {
                slot: SlotBinding(slot=slot, model=f"m-{slot}", protocol="openai_chat_completions", native_tools=True, fallback_used=False)
                for slot in ("fast_extractor", "deep_reasoner", "bear_reviewer", "writer")
            },
        }

    return hook, call_log


def test_squad_api_run_dossier_deep_report_export_and_auth_isolation():
    client = TestClient(api.app)
    headers_a = _register_user(client)
    headers_b = _register_user(client)

    # Create two sources: one exportable SEC document and one export=False BYOK note
    src_ok = client.post(
        "/v2/research/sources",
        headers=headers_a,
        json={
            "title": "ACME SEC EX-99.1",
            "text": "ACME Q2 FY26 Cloud revenue reached $720M, up 24% YoY; total revenue $1,000M.",
            "permissions": {"store": True, "display": True, "model_input": True, "export": True},
        },
    ).json()["source_id"]
    src_restricted = client.post(
        "/v2/research/sources",
        headers=headers_a,
        json={
            "title": "Restricted BYOK Note",
            "text": "CONFIDENTIAL_NON_EXPORTABLE_NOTE verbatim excerpt.",
            "permissions": {"store": True, "display": True, "model_input": True, "export": False},
        },
    ).json()["source_id"]

    topic_id = _create_topic(client, headers_a, source_ids=[src_ok, src_restricted])

    # Auth enforcement before any run
    for method, suffix, body in (
        ("POST", "run", {}),
        ("GET", "dossier", None),
        ("POST", "deep-report", {}),
        ("GET", "export", None),
        ("POST", "steer", {"text": "focus on FCF"}),
        ("GET", "events", None),
    ):
        url = f"/v2/research/topics/{topic_id}/earnings-squad/{suffix}"
        unauth = client.post(url, json=body) if method == "POST" else client.get(url)
        assert unauth.status_code in (401, 403), f"{method} {suffix} unauth got {unauth.status_code}"
        cross = client.post(url, headers=headers_b, json=body) if method == "POST" else client.get(url, headers=headers_b)
        assert cross.status_code == 404, f"{method} {suffix} cross-account got {cross.status_code}"

    # Configure heterogeneous slot overrides via account model settings (zero secrets in summary)
    slot_resp = client.put(
        "/v1/account/model-config",
        headers=headers_a,
        json={
            "mode": "platform",
            "model_slots": {
                "fast_extractor": {
                    "protocol": "openai_chat_completions",
                    "base_url": "https://api.example.com/v1",
                    "model": "fast-worker-model",
                    "tools_verified": True,
                },
                "deep_reasoner": {
                    "protocol": "openai_chat_completions",
                    "base_url": "https://api.example.com/v1",
                    "model": "deep-lead-model",
                },
                "bear_reviewer": {
                    "protocol": "anthropic_messages",
                    "base_url": "https://anthropic.example.com",
                    "model": "bear-cross-family-model",
                    "messages_auth_scheme": "x-api-key",
                    "messages_max_tokens": 2048,
                },
                "writer": {
                    "protocol": "openai_chat_completions",
                    "base_url": "https://api.example.com/v1",
                    "model": "writer-synthesis-model",
                },
            },
        },
    )
    assert slot_resp.status_code == 200, slot_resp.text

    hook, call_log = _make_api_squad_hook(exportable_source_id=src_ok, restricted_source_id=src_restricted)
    api.set_earnings_squad_runner_hook(hook)

    # 1. Trigger sync squad run
    run_resp = client.post(
        f"/v2/research/topics/{topic_id}/earnings-squad/run",
        headers=headers_a,
        json={"symbol": "ACME", "sic_code": "7372", "execution_mode": "sync"},
    )
    assert run_resp.status_code == 200, run_resp.text
    run_data = run_resp.json()
    assert run_data["status"] == "completed"
    assert run_data["dossier"]["locked"] is True
    assert run_data["dossier"]["deep_report_markdown"] is None
    assert run_data["slot_routing_summary"]["cross_family_bear_review"] is True
    assert run_data["slot_routing_summary"]["slots"]["fast_extractor"]["model"] == "fast-worker-model"
    assert [e["sequence"] for e in run_data["org_events"]] == sorted(e["sequence"] for e in run_data["org_events"])
    assert any(e.get("edge_event") and e["edge_event"]["is_review"] is True for e in run_data["org_events"])

    # 2. Fetch dossier endpoint
    dos_resp = client.get(f"/v2/research/topics/{topic_id}/earnings-squad/dossier", headers=headers_a)
    assert dos_resp.status_code == 200, dos_resp.text
    dos_data = dos_resp.json()
    assert dos_data["dossier"]["dossier_id"] == run_data["dossier"]["dossier_id"]
    assert dos_data["checkpoint_completed_stages"] == ["stage_0", "stage_1", "stage_2", "stage_2_5", "stage_3a", "stage_3b"]

    # 3. On-demand Deep Report generation + cache reuse (0 retrieval calls)
    calls_before_deep = len(call_log)
    deep1 = client.post(f"/v2/research/topics/{topic_id}/earnings-squad/deep-report", headers=headers_a, json={})
    assert deep1.status_code == 200, deep1.text
    d1 = deep1.json()
    assert d1["cached"] is False
    assert d1["deep_report_tool_calls"] == 0
    assert len(call_log) == calls_before_deep + 1
    assert "Identical facts: Cloud revenue $720M" in d1["deep_report_markdown"]

    deep2 = client.post(f"/v2/research/topics/{topic_id}/earnings-squad/deep-report", headers=headers_a, json={})
    assert deep2.status_code == 200, deep2.text
    d2 = deep2.json()
    assert d2["cached"] is True
    assert len(call_log) == calls_before_deep + 1  # zero extra model calls when cached

    # 4. Multi-format exports with strict export=False redaction enforcement
    md_exp = client.get(f"/v2/research/topics/{topic_id}/earnings-squad/export?format=markdown", headers=headers_a)
    assert md_exp.status_code == 200
    assert md_exp.headers["content-type"].startswith("text/markdown")
    assert "attachment; filename=" in md_exp.headers["content-disposition"]
    assert md_exp.headers["content-disposition"].endswith('.md"')
    assert md_exp.headers["x-export-redacted"] == "true"
    assert "CONFIDENTIAL_NON_EXPORTABLE_NOTE" not in md_exp.text
    assert "Export Permissions & Redaction Notice" in md_exp.text

    csv_exp = client.get(f"/v2/research/topics/{topic_id}/earnings-squad/export?format=csv", headers=headers_a)
    assert csv_exp.status_code == 200
    assert csv_exp.headers["content-type"].startswith("text/csv")
    assert csv_exp.headers["content-disposition"].endswith('.csv"')
    assert "CONFIDENTIAL_NON_EXPORTABLE_NOTE" not in csv_exp.text
    assert "987654.32" not in csv_exp.text

    xlsx_exp = client.get(f"/v2/research/topics/{topic_id}/earnings-squad/export?format=xlsx", headers=headers_a)
    assert xlsx_exp.status_code == 200
    assert xlsx_exp.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert xlsx_exp.headers["content-disposition"].endswith('.xlsx"')
    sheet_names, sheet_rows = _read_xlsx_workbook(xlsx_exp.content)
    assert tuple(sheet_names) == XLSX_SHEET_NAMES
    xlsx_dump = "\n".join(" ".join(r) for rows in sheet_rows.values() for r in rows)
    assert "CONFIDENTIAL_NON_EXPORTABLE_NOTE" not in xlsx_dump
    assert "Export Permissions & Redaction Notice" in xlsx_dump

    # Invalid export format -> 422
    bad_exp = client.get(f"/v2/research/topics/{topic_id}/earnings-squad/export?format=xml", headers=headers_a)
    assert bad_exp.status_code == 422

    # 5. Terminal SSE replay + Last-Event-ID cursor resumption
    sse_all = client.get(f"/v2/research/topics/{topic_id}/earnings-squad/events", headers=headers_a)
    assert sse_all.status_code == 200
    assert sse_all.headers["content-type"].startswith("text/event-stream")
    frames = _parse_sse_frames(sse_all.text.splitlines())
    org_frames = [f for f in frames if f["event"] == "org_graph_event"]
    assert len(org_frames) >= 5
    assert frames[-1]["event"] == "run.terminal"
    assert frames[-1]["data"]["operation_status"] == "completed"
    first_frame = org_frames[0]["data"]
    for req_key in ("sequence", "stage", "node_id", "role", "display_name", "runtime_state", "action_kind", "current_activity", "blocker_text", "edge_event"):
        assert req_key in first_frame

    mid_seq = org_frames[2]["data"]["sequence"]
    sse_resumed = client.get(
        f"/v2/research/topics/{topic_id}/earnings-squad/events",
        headers={**headers_a, "Last-Event-ID": f"org:{mid_seq}"},
    )
    resumed_frames = [f for f in _parse_sse_frames(sse_resumed.text.splitlines()) if f["event"] == "org_graph_event"]
    assert all(f["data"]["sequence"] > mid_seq for f in resumed_frames)


def test_deep_report_endpoint_rejects_writer_retrieval_tool_calls():
    client = TestClient(api.app)
    headers = _register_user(client)
    topic_id = _create_topic(client, headers)

    hook, _ = _make_api_squad_hook(
        exportable_source_id="src_ok",
        restricted_source_id="src_restr",
        writer_invokes_tool_on_deep=True,
    )
    api.set_earnings_squad_runner_hook(hook)

    run_resp = client.post(f"/v2/research/topics/{topic_id}/earnings-squad/run", headers=headers, json={})
    assert run_resp.status_code == 200

    bad_deep = client.post(f"/v2/research/topics/{topic_id}/earnings-squad/deep-report", headers=headers, json={"force": True})
    assert bad_deep.status_code == 422
    assert bad_deep.json()["error_code"] == "invalid_request"
    assert "zero retrieval tool calls" in bad_deep.json()["message"]


# --------------------------------------------------------------------------- 4. Async Run + Mid-Run Steering + Live SSE org_graph_event
def test_async_squad_run_live_sse_org_events_and_mid_run_steering(live_server):
    client = TestClient(api.app)
    headers = _register_user(client)
    topic_id = _create_topic(client, headers)

    dossier_seed = _build_rich_dossier(restrict_byok_export=False)
    snap = dossier_seed.snapshot
    segments_done = threading.Event()
    forensics_started = threading.Event()
    steering_accepted = threading.Event()
    exec_counts = {"worker_segments": 0, "worker_forensics": 0, "worker_guidance": 0}
    lock = threading.Lock()

    class BlockingToolRunner:
        def call_tool(self, tool_name: str, arguments: dict[str, Any], *, worker_role: str | None = None) -> dict[str, Any]:
            if worker_role == "worker_forensics":
                forensics_started.set()
                # Keep this worker active until the client has actually queued steering.
                assert steering_accepted.wait(timeout=30.0)
            return {"status": "ok", "tool": tool_name}

    def model_fn(mreq: SlotModelRequest) -> SlotModelResponse:
        if mreq.role == "lead_coordinator" and mreq.stage == "stage_1_plan":
            return SlotModelResponse(
                parsed=ResearchDispatchPlan(
                    selected_l2_pack_id="kpi_saas_cloud",
                    tasks=[
                        WorkerTask(worker_role="worker_segments", objective="Segments"),
                        WorkerTask(worker_role="worker_forensics", objective="Forensics"),
                        WorkerTask(worker_role="worker_guidance", objective="Guidance"),
                    ],
                )
            )
        if mreq.role == "lead_coordinator" and mreq.stage == "steering":
            return SlotModelResponse(
                parsed=SteeringImpactDecision(
                    instruction_sequences=[1],
                    affected_workers=["worker_forensics"],
                    cancel_running_workers=["worker_forensics"],
                    updated_tasks=[
                        WorkerTask(
                            worker_role="worker_forensics",
                            objective="Re-check inventory vs COGS redline per steering",
                            steering_instruction_sequence=1,
                        )
                    ],
                    rationale="Only worker_forensics affected by inventory steering.",
                )
            )
        if mreq.role == "worker_segments":
            with lock:
                exec_counts["worker_segments"] += 1
            segments_done.set()
            return SlotModelResponse(
                parsed=EvidenceCardBatch(
                    task_id="t_seg",
                    worker_role="worker_segments",
                    cards=[
                        EvidenceCard(
                            card_id="card_seg_live",
                            worker_role="worker_segments",
                            topic_tag="cloud",
                            finding="Cloud revenue +24% YoY.",
                            supporting_fact_ids=["f:rev_q2"],
                        )
                    ],
                )
            )
        if mreq.role == "worker_guidance":
            with lock:
                exec_counts["worker_guidance"] += 1
            return SlotModelResponse(
                parsed=EvidenceCardBatch(
                    task_id="t_gui",
                    worker_role="worker_guidance",
                    cards=[
                        EvidenceCard(
                            card_id="card_gui_live",
                            worker_role="worker_guidance",
                            topic_tag="guidance",
                            finding="Q3 guide trimmed conservatively.",
                            supporting_fact_ids=["f:rev_q2"],
                        )
                    ],
                )
            )
        if mreq.role == "worker_forensics":
            steering_seq = 0
            if mreq.turn_index == 0:
                try:
                    steering_seq = int(json.loads(mreq.user_prompt).get("task", {}).get("steering_instruction_sequence", 0))
                except ValueError:
                    steering_seq = 0
            if mreq.turn_index == 0 and steering_seq == 0:
                with lock:
                    exec_counts["worker_forensics"] += 1
                return SlotModelResponse(tool_calls=(ModelToolCall("tc_f1", "sec_redline_diff", {}),))
            with lock:
                exec_counts["worker_forensics"] += 1
            return SlotModelResponse(
                parsed=EvidenceCardBatch(
                    task_id="t_for",
                    worker_role="worker_forensics",
                    cards=[
                        EvidenceCard(
                            card_id="card_for_steered",
                            worker_role="worker_forensics",
                            topic_tag="inventory_redline",
                            finding="Inventory rose +55% YoY vs +31% COGS per steering check.",
                            supporting_fact_ids=["f:rev_q2"],
                        )
                    ],
                )
            )
        if mreq.role == "lead_coordinator" and mreq.stage == "stage_2_5_review":
            return SlotModelResponse(parsed=Stage25ReviewDecision(has_cross_worker_leads=False))
        if mreq.role == "bear_reviewer":
            anchor = snap.flags[0].flag_id if snap.flags else "card_for_steered"
            return SlotModelResponse(
                parsed=BearCaseBatch(
                    items=[
                        BearCaseItem(
                            bear_id="bear_live_1",
                            thesis_challenge="Inventory grew +55% YoY outpacing COGS by +24pp.",
                            anchored_evidence_ids=[anchor],
                            anchor_kind="flag",
                            next_quarter_invalidation_trigger="Inventory growth normalizes below revenue growth in Q3.",
                        )
                    ]
                )
            )
        if mreq.role == "writer":
            return SlotModelResponse(content="# Memo: ACME Steered Live\n- Inventory +55% YoY checked.")
        raise AssertionError(f"Unexpected request: {mreq}")

    def hook(org_sink=None, checkpoint_store=None, **_kwargs):
        return {
            "orchestrator": EarningsSquadOrchestrator(
                model_runner=CallableSlotModelRunner(model_fn),
                tool_runner=BlockingToolRunner(),
                prefetch_deps=PrefetchDependencies(precomputed_snapshot=snap),
                checkpoint_store=checkpoint_store,
                org_sinks=[org_sink] if org_sink is not None else [],
            )
        }

    api.set_earnings_squad_runner_hook(hook)

    # Start async background squad run -> 202
    start_resp = client.post(
        f"/v2/research/topics/{topic_id}/earnings-squad/run",
        headers=headers,
        json={"execution_mode": "async", "symbol": "ACME"},
    )
    assert start_resp.status_code == 202, start_resp.text
    assert start_resp.json()["status"] == "running"

    assert segments_done.wait(timeout=3.0)
    assert forensics_started.wait(timeout=3.0)

    # Topic deletion blocked with 409 while squad run active
    del_resp = client.delete(f"/v2/research/topics/{topic_id}", headers=headers)
    assert del_resp.status_code == 409

    async def stream_and_steer():
        async with httpx.AsyncClient(base_url=live_server, timeout=10.0) as async_client:
            async with async_client.stream(
                "GET",
                f"/v2/research/topics/{topic_id}/earnings-squad/events",
                headers=headers,
            ) as sse_resp:
                assert sse_resp.status_code == 200

                # Issue mid-run steering while SSE consumer is attached
                steer_resp = await async_client.post(
                    f"/v2/research/topics/{topic_id}/earnings-squad/steer",
                    headers=headers,
                    json={"text": "Re-examine inventory vs COGS redline divergence"},
                )
                assert steer_resp.status_code == 202
                assert steer_resp.json()["status"] == "queued"
                steering_accepted.set()

                lines: list[str] = []
                async for line in sse_resp.aiter_lines():
                    lines.append(line)
                    if line == "" and lines and any(l.startswith("event: run.terminal") for l in lines[-5:]):
                        break
                return _parse_sse_frames(lines)

    frames = asyncio.run(stream_and_steer())
    org_events = [f["data"] for f in frames if f.get("event") == "org_graph_event"]
    assert len(org_events) >= 6
    assert any(ev["stage"] == "steering" and ev["node_id"] == "worker_forensics" and ev["runtime_state"] == "cancelled" for ev in org_events)
    assert frames[-1]["event"] == "run.terminal"
    assert frames[-1]["data"]["operation_status"] == "completed"

    # Unrelated completed workers did NOT re-run; only worker_forensics re-ran once steered
    assert exec_counts["worker_segments"] == 1
    assert exec_counts["worker_guidance"] == 1
    assert exec_counts["worker_forensics"] == 2

    final_dos = client.get(f"/v2/research/topics/{topic_id}/earnings-squad/dossier", headers=headers).json()
    assert final_dos["status"] == "completed"
    card_ids = {c["card_id"] for c in final_dos["dossier"]["evidence_cards"]}
    assert "card_for_steered" in card_ids
    assert len(final_dos["steering_instructions"]) == 1


# --------------------------------------------------------------------------- 5. Account Slot Runner Adapter & Post-Run Steer Resume
def test_account_slot_model_runner_multi_turn_and_post_run_steer_resume():
    # Direct unit test of _AccountSlotModelRunner bridging complete_turn
    calls: list[Any] = []

    class FakeWireProvider:
        def complete_turn(self, turn):
            calls.append(turn)
            if len(turn.history) == 0:
                return ModelResponse(
                    message={"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "sec_filing_tables", "arguments": "{}"}}]},
                    tool_calls=(ModelToolCall("c1", "sec_filing_tables", {}),),
                    usage=ModelUsage(10, 5, 15),
                    finish_reason="tool_calls",
                    stop_class="tool_calls",
                )
            return ModelResponse(
                message={"role": "assistant", "content": '{"ok": true}'},
                tool_calls=(),
                usage=ModelUsage(12, 8, 20),
                finish_reason="stop",
                stop_class="end",
            )

    runner = api._AccountSlotModelRunner(providers_by_slot={"fast_extractor": FakeWireProvider()}, default_provider=None)
    binding = SlotBinding(slot="fast_extractor", model="fake", protocol="openai_chat_completions", native_tools=True, fallback_used=False)
    r0 = runner.invoke(SlotModelRequest(
        slot="fast_extractor",
        stage="stage_2_workers",
        role="worker_segments",
        system_prompt="sys",
        user_prompt="init",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "init"}],
        tools=[{"name": "sec_filing_tables", "description": "Tables", "input_schema": {"type": "object", "properties": {}}}],
        binding=binding,
        turn_index=0,
    ))
    assert len(r0.tool_calls) == 1
    r1 = runner.invoke(SlotModelRequest(
        slot="fast_extractor",
        stage="stage_2_workers",
        role="worker_segments",
        system_prompt="sys",
        user_prompt='{"status": "ok"}',
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "init"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "sec_filing_tables", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": '{"status": "ok"}'},
        ],
        tools=[{"name": "sec_filing_tables", "description": "Tables", "input_schema": {"type": "object", "properties": {}}}],
        binding=binding,
        turn_index=1,
    ))
    assert r1.content == '{"ok": true}'
    assert len(calls[1].history) == 1
    assert calls[1].history[0].results[0].tool_call_id == "c1"


def test_bank_sic_sector_gating_propagates_across_exports():
    bank_cur = PeriodFinancials(
        period_label="Q2 FY26",
        period_type="quarter",
        period_end=date(2026, 6, 30),
        currency="USD",
        monetary_unit="USD millions",
        revenue=MetricValue(value=Decimal("2500"), unit="USD millions", currency="USD"),
        net_income=MetricValue(value=Decimal("700"), unit="USD millions", currency="USD"),
    )
    diag = earnings_quality_diagnostics(EarningsQualityRequest(
        entity_id="cik:0000019617",
        symbol="BANK",
        sic="6021",
        sic_description="National Commercial Banks",
        current=bank_cur,
    ))
    val = valuation_and_implied_expectations(ValuationRequest(
        entity_id="cik:0000019617",
        symbol="BANK",
        sic="6021",
        currency="USD",
        monetary_unit="USD millions",
        close_price=MetricValue(value=Decimal("40"), unit="USD", currency="USD"),
        diluted_shares=MetricValue(value=Decimal("100"), unit="millions shares"),
        ttm_net_income=MetricValue(value=Decimal("2600"), unit="USD millions", currency="USD"),
    ))
    snap = EarningsPrefetchSnapshot(
        entity_id="cik:0000019617",
        symbol="BANK",
        cik="0000019617",
        company_name="Sample National Bank",
        sic_code="6021",
        period_label="Q2 FY26",
        sector_applicability=diag.sector_applicability,
        statements=[bank_cur],
        diagnostics=diag,
        valuation=val,
    )
    dossier = EarningsDossier(
        dossier_id="dossier_bank_q2",
        run_id="sqrun_bank_q2",
        entity_id="cik:0000019617",
        symbol="BANK",
        cik="0000019617",
        company_name="Sample National Bank",
        period_label="Q2 FY26",
        snapshot=snap,
        evidence_cards=[
            EvidenceCard(
                card_id="card_bank_1",
                worker_role="worker_forensics",
                topic_tag="nim_capital",
                finding="SIC 6021 bank requires NIM and CET1 instead of EV/EBITDA or FCF.",
                supporting_fact_ids=["f:bank_nim"],
            )
        ],
        bear_cases=[
            BearCaseItem(
                bear_id="bear_bank_1",
                thesis_challenge="Provision cycle risk weighs on ROTCE near 10% threshold.",
                anchored_evidence_ids=["card_bank_1"],
                anchor_kind="flag",
                next_quarter_invalidation_trigger="Net charge-offs remain below 35bps in Q3 FY26.",
            )
        ],
        sector_applicability=diag.sector_applicability,
        gate_report=DossierGateReport(passed=True, verified_card_ids=["card_bank_1"], verified_bear_ids=["bear_bank_1"], exportable=True),
        memo_markdown="# Memo: Sample National Bank (Q2 FY26)\n- Sector gated (`bank`, SIC 6021).",
        slot_usage_summary=SlotUsageSummary(
            slots={
                slot: SlotUsageRecord(slot=slot, model="m", protocol="openai_chat_completions", fallback_used=False)
                for slot in ("fast_extractor", "deep_reasoner", "bear_reviewer", "writer")
            },
            total_calls=1,
            total_tokens=50,
            stage_0_model_calls=0,
            deep_report_tool_calls=0,
        ),
    )

    md = export_earnings_dossier(dossier, fmt="markdown")
    assert "`bank`" in md.text
    assert "**Sector Gated**: `True`" in md.text

    csv_res = export_earnings_dossier(dossier, fmt="csv")
    assert "disabled_by_sector" in csv_res.text

    xlsx_res = export_earnings_dossier(dossier, fmt="xlsx")
    names, sheets = _read_xlsx_workbook(xlsx_res.content_bytes)
    assert tuple(names) == XLSX_SHEET_NAMES
    eq_dump = "\n".join(" ".join(r) for r in sheets["Earnings Quality"])
    assert "disabled_by_sector" in eq_dump


def test_post_run_steer_persists_for_resume_and_skips_completed_workers():
    client = TestClient(api.app)
    headers = _register_user(client)
    topic_id = _create_topic(client, headers)

    dossier_seed = _build_rich_dossier(restrict_byok_export=False)
    snap = dossier_seed.snapshot
    counts = {"worker_segments": 0, "worker_forensics": 0, "worker_guidance": 0}

    def model_fn(mreq: SlotModelRequest) -> SlotModelResponse:
        if mreq.role == "lead_coordinator" and mreq.stage == "stage_1_plan":
            return SlotModelResponse(
                parsed=ResearchDispatchPlan(
                    selected_l2_pack_id="kpi_saas_cloud",
                    tasks=[
                        WorkerTask(worker_role="worker_segments", objective="Segments"),
                        WorkerTask(worker_role="worker_forensics", objective="Forensics"),
                        WorkerTask(worker_role="worker_guidance", objective="Guidance"),
                    ],
                )
            )
        if mreq.role == "lead_coordinator" and mreq.stage == "steering":
            return SlotModelResponse(
                parsed=SteeringImpactDecision(
                    instruction_sequences=[1],
                    affected_workers=["worker_guidance"],
                    updated_tasks=[
                        WorkerTask(
                            worker_role="worker_guidance",
                            objective="Re-check Q3 guidance bridge after steer",
                            steering_instruction_sequence=1,
                        )
                    ],
                    rationale="Only guidance affected.",
                )
            )
        if mreq.role in counts:
            counts[mreq.role] += 1
            return SlotModelResponse(
                parsed=EvidenceCardBatch(
                    task_id=f"t_{mreq.role}",
                    worker_role=mreq.role,
                    cards=[
                        EvidenceCard(
                            card_id=f"card_{mreq.role}_{counts[mreq.role]}",
                            worker_role=mreq.role,
                            topic_tag=mreq.role,
                            finding=f"Finding from {mreq.role} run #{counts[mreq.role]}",
                            supporting_fact_ids=["f:rev_q2"],
                        )
                    ],
                )
            )
        if mreq.role == "lead_coordinator" and mreq.stage == "stage_2_5_review":
            return SlotModelResponse(parsed=Stage25ReviewDecision(has_cross_worker_leads=False))
        if mreq.role == "bear_reviewer":
            anchor = snap.flags[0].flag_id if snap.flags else "card_worker_segments_1"
            return SlotModelResponse(
                parsed=BearCaseBatch(
                    items=[
                        BearCaseItem(
                            bear_id="bear_resume_1",
                            thesis_challenge="Q3 revenue guidance midpoint cut while SBC remains 6% of revenue.",
                            anchored_evidence_ids=[anchor],
                            anchor_kind="flag",
                            next_quarter_invalidation_trigger="Q3 growth recovers above 20% YoY.",
                        )
                    ]
                )
            )
        return SlotModelResponse(content="# Memo: Post-Run Steer Resume\n- Guidance updated.")

    api.set_earnings_squad_runner_hook(
        lambda **_k: {
            "model_runner": CallableSlotModelRunner(model_fn),
            "prefetch_deps": PrefetchDependencies(precomputed_snapshot=snap),
        }
    )

    r1 = client.post(f"/v2/research/topics/{topic_id}/earnings-squad/run", headers=headers, json={})
    assert r1.status_code == 200
    assert counts == {"worker_segments": 1, "worker_forensics": 1, "worker_guidance": 1}

    steer = client.post(
        f"/v2/research/topics/{topic_id}/earnings-squad/steer",
        headers=headers,
        json={"text": "Re-evaluate Q3 guidance midpoint cut only"},
    )
    assert steer.status_code == 202
    assert steer.json()["status"] == "persisted_for_resume"

    # Wait for checkpoint resume re-run -> ONLY worker_guidance runs again; segments & forensics stay skipped!
    r2 = client.post(
        f"/v2/research/topics/{topic_id}/earnings-squad/run",
        headers=headers,
        json={"resume_from_checkpoint": True},
    )
    assert r2.status_code == 200
    assert counts == {"worker_segments": 1, "worker_forensics": 1, "worker_guidance": 2}
    cards = {c["card_id"] for c in r2.json()["dossier"]["evidence_cards"]}
    assert "card_worker_guidance_2" in cards
    assert "card_worker_segments_1" in cards


def test_export_redacts_transcript_and_valuation_bear_anchors_and_extra_source_permissions():
    base = _build_rich_dossier(restrict_byok_export=True, include_deep_report=True)
    dossier = base.model_copy(
        update={
            "bear_cases": [
                BearCaseItem(
                    bear_id="bear_trans_anchor",
                    thesis_challenge="SECRET_CFO_NAME hinted Q3 deceleration on Q&A.",
                    anchored_evidence_ids=["transcript:qa"],
                    anchor_kind="guidance_gap",
                    next_quarter_invalidation_trigger="Management reaffirms Q3 billings > 20%.",
                ),
                BearCaseItem(
                    bear_id="bear_val_anchor",
                    thesis_challenge="Valuation forward multiple built on 987654.32 NTM revenue is stretched.",
                    anchored_evidence_ids=["valuation:pe_forward"],
                    anchor_kind="valuation_gap",
                    next_quarter_invalidation_trigger="Forward revenue revisions exceed +5%.",
                ),
            ]
        }
    )

    # Test export=False on bear anchors + extra_source_permissions revoking sec_ex99_1
    extra_perms = {"sec_ex99_1": {"export": False}}
    md = export_earnings_dossier(dossier, fmt="markdown", report_mode="auto", extra_source_permissions=extra_perms)
    assert "SECRET_CFO_NAME" not in md.text
    assert "987654.32" not in md.text
    assert "ACME Q2 FY26 8-K EX-99.1" not in md.text
    assert "ACME Q2 FY26 Cloud revenue reached $720M" not in md.text

    xlsx_res = export_earnings_dossier(dossier, fmt="xlsx", extra_source_permissions=extra_perms)
    names, sheet_rows = _read_xlsx_workbook(xlsx_res.content_bytes)
    assert tuple(names) == XLSX_SHEET_NAMES
    all_xlsx = "\n".join(" ".join(r) for rows in sheet_rows.values() for r in rows)
    assert "SECRET_CFO_NAME" not in all_xlsx
    assert "987654.32" not in all_xlsx
    assert "ACME Q2 FY26 8-K EX-99.1" not in all_xlsx

    # Sheet 6 Source Provenance & Export Permissions reflects extra_source_permissions
    s6_rows = sheet_rows["Bear Case & Sources"]
    prov_rows = [r for r in s6_rows if len(r) >= 5 and r[0] == "sec_ex99_1" and r[2] == "official"]
    assert len(prov_rows) == 1
    assert prov_rows[0][1] == "[REDACTED: export=False]"
    assert prov_rows[0][4] == "FALSE"
    red_rows = [r for r in s6_rows if len(r) >= 3 and r[0] == "sec_ex99_1" and r[1].startswith(("topic_source", "evidence_"))]
    assert len(red_rows) >= 1


def test_platform_mode_partial_model_slots_without_hook(monkeypatch: pytest.MonkeyPatch):
    client = TestClient(api.app)
    headers = _register_user(client)
    topic_id = _create_topic(client, headers)

    # Save partial model_slots in platform mode (bear_reviewer only; fast_extractor/deep_reasoner/writer unset)
    put_resp = client.put(
        "/v1/account/model-config",
        headers=headers,
        json={
            "mode": "platform",
            "model_slots": {
                "bear_reviewer": {
                    "protocol": "anthropic_messages",
                    "base_url": "https://anthropic.example.com",
                    "model": "bear-cross-family-model",
                    "messages_auth_scheme": "x-api-key",
                    "messages_max_tokens": 2048,
                }
            },
        },
    )
    assert put_resp.status_code == 200

    # Verify slot resolution with NO hook succeeds cleanly when platform challenge_provider supports complete_turn
    class PlatformScriptedProvider:
        name = "platform-scripted"

        def complete_turn(self, turn):
            if "ResearchDispatchPlan" in turn.system or "Lead Coordinator" in turn.system:
                if "Stage 2.5" in turn.system or "stage_2_5" in turn.user:
                    return ModelResponse(
                        {"role": "assistant", "content": json.dumps({"has_cross_worker_leads": False, "rationale": "ok"})},
                        (),
                        ModelUsage(5, 5, 10),
                        "stop",
                        "end",
                    )
                return ModelResponse(
                    {
                        "role": "assistant",
                        "content": json.dumps({
                            "selected_l2_pack_id": "kpi_saas_cloud",
                            "tasks": [
                                {"worker_role": "worker_segments", "objective": "Extract segments"},
                                {"worker_role": "worker_forensics", "objective": "Extract forensics"},
                                {"worker_role": "worker_guidance", "objective": "Extract guidance"},
                            ],
                        }),
                    },
                    (),
                    ModelUsage(10, 10, 20),
                    "stop",
                    "end",
                )
            if "bear_reviewer" in turn.system:
                return ModelResponse(
                    {
                        "role": "assistant",
                        "content": json.dumps({
                            "items": [
                                {
                                    "bear_id": "bear_plat_1",
                                    "thesis_challenge": "SBC 6% margin drag weighs on GAAP operating leverage.",
                                    "anchored_evidence_ids": ["card_p_seg"],
                                    "anchor_kind": "flag",
                                    "next_quarter_invalidation_trigger": "SBC falls below 4% of revenue.",
                                }
                            ]
                        }),
                    },
                    (),
                    ModelUsage(10, 10, 20),
                    "stop",
                    "end",
                )
            if "writer slot" in turn.system:
                return ModelResponse(
                    {"role": "assistant", "content": "# Platform Partial Slots Memo\n- Completed."},
                    (),
                    ModelUsage(10, 10, 20),
                    "stop",
                    "end",
                )
            return ModelResponse(
                {
                    "role": "assistant",
                    "content": json.dumps({
                        "task_id": "t_plat",
                        "worker_role": "worker_segments",
                        "cards": [
                            {
                                "card_id": "card_p_seg" if "worker_segments" in turn.system else f"card_p_{uuid4().hex[:6]}",
                                "worker_role": "worker_segments",
                                "topic_tag": "summary",
                                "finding": "Revenue $1,000M with 6% SBC.",
                                "supporting_fact_ids": ["f:rev_q2"],
                            }
                        ],
                    }),
                },
                (),
                ModelUsage(10, 10, 20),
                "stop",
                "end",
            )

    monkeypatch.setattr(api, "challenge_provider", PlatformScriptedProvider())
    resp = client.post(f"/v2/research/topics/{topic_id}/earnings-squad/run", headers=headers, json={})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "completed"
    assert data["slot_routing_summary"]["slots"]["bear_reviewer"]["configured"] is True
    assert data["slot_routing_summary"]["slots"]["bear_reviewer"]["model"] == "bear-cross-family-model"
    assert data["slot_routing_summary"]["slots"]["fast_extractor"]["fallback_used"] is True
    assert data["slot_routing_summary"]["slots"]["deep_reasoner"]["fallback_used"] is True

    # Conversely, invalid fast_extractor without native_tools fails closed with 422
    bad_resp = client.post(
        f"/v2/research/topics/{topic_id}/earnings-squad/run",
        headers=headers,
        json={
            "slot_overrides": {
                "fast_extractor": {
                    "protocol": "openai_chat_completions",
                    "base_url": "https://fast.example.test/v1",
                    "model": "no-tools-fast",
                    "capabilities": {"mode": "json_object", "native_tools": False},
                }
            }
        },
    )
    assert bad_resp.status_code == 422




def test_saved_bank_report_gets_a_traceable_correction_across_read_and_export():
    from tests.test_v4a_bank_diagnostics import _snap_fixture

    client = TestClient(api.app)
    headers = _register_user(client)
    topic_id = _create_topic(client, headers)
    owner, _ = api.authenticate_local(api.ACCOUNTS, api.AUTH_SESSIONS, headers["Authorization"], now=api.lifecycle_clock.now())
    key = api._squad_state_key(owner.account_id, topic_id)
    base = f"/v2/research/topics/{topic_id}/earnings-squad"
    raw = _build_rich_dossier().model_dump(mode="json")
    raw["snapshot"]["bank_disclosure"] = {**_snap_fixture().model_dump(mode="json"), "schema_version": "v4a.1"}
    raw["memo_markdown"] = "Outdated bank valuation 9.53x"
    row = {"account_id": owner.account_id, "topic_id": topic_id, "run_id": raw["run_id"],
           "operation_id": "bank-archive", "status": "completed", "dossier": raw}
    api.EARNINGS_SQUAD_STATES.put(key, row)
    current = client.get(f"{base}/dossier", headers=headers)
    assert current.status_code == 200, current.text
    dossier = current.json()["dossier"]
    assert dossier["supersedes_sha256"] == raw["dossier_sha256"]
    assert dossier["snapshot"]["bank_valuation"] is None
    original_fact = raw["snapshot"]["statements"][0]["revenue"]
    current_fact = dossier["snapshot"]["statements"][0]["revenue"]
    assert (current_fact["value"], current_fact["fact_id"]) == (original_fact["value"], original_fact["fact_id"])
    assert "9.53x" not in dossier["memo_markdown"]
    assert client.post(f"{base}/deep-report", json={}, headers=headers).status_code == 409
    for fmt in ["markdown", "csv", "xlsx"]:
        exported = client.get(f"{base}/export", headers=headers, params={"format": fmt})
        assert exported.status_code == 200
        assert exported.headers["x-dossier-id"] == dossier["dossier_id"]
    params = {"original_sha256": raw["dossier_sha256"]}
    api.EARNINGS_SQUAD_STATES.put(key, {**row, "dossier": None})
    replay = client.get(f"{base}/dossier", params=params, headers=headers)
    assert replay.json()["dossier"]["dossier_sha256"] == dossier["dossier_sha256"]
    archived = client.get(f"{base}/dossier", params={**params, "revision": "original"}, headers=headers)
    assert archived.json()["dossier"]["memo_markdown"] == raw["memo_markdown"]
    assert client.get(f"{base}/dossier", params=params, headers=_register_user(client)).status_code == 404

    # Starting fresh must also archive a report that nobody has opened.
    second_topic = _create_topic(client, headers)
    second_key = api._squad_state_key(owner.account_id, second_topic)
    api.EARNINGS_SQUAD_STATES.put(second_key, {**row, "topic_id": second_topic})
    hook, _ = _make_api_squad_hook(exportable_source_id="public", restricted_source_id="private")
    api.set_earnings_squad_runner_hook(hook)
    second_base = f"/v2/research/topics/{second_topic}/earnings-squad"
    fresh = client.post(f"{second_base}/run", json={}, headers=headers)
    assert fresh.status_code == 200, fresh.text
    archived = client.get(f"{second_base}/dossier", params={**params, "revision": "original"}, headers=headers)
    assert archived.json()["dossier"]["memo_markdown"] == raw["memo_markdown"]


@pytest.mark.parametrize(("source_value", "disposition"), [
    ("$30.60", "unchanged"), ("$29.57", "corrected"), (None, "unverified"),
])
def test_prior_bank_edition_rechecks_saved_cells_before_read_and_export(source_value, disposition):
    from tests.test_v4a_bank_diagnostics import _snap_fixture

    client = TestClient(api.app)
    headers = _register_user(client)
    topic_id = _create_topic(client, headers)
    owner, _ = api.authenticate_local(api.ACCOUNTS, api.AUTH_SESSIONS, headers["Authorization"], now=api.lifecycle_clock.now())
    key = api._squad_state_key(owner.account_id, topic_id)
    base = f"/v2/research/topics/{topic_id}/earnings-squad"

    raw = _build_rich_dossier().model_dump(mode="json")
    snap = raw["snapshot"]
    bank = _snap_fixture(tbvps_rep="30.60", rotce_q=None).model_dump(mode="json")
    release = snap["earnings_release"]
    bank.update(schema_version="v4a.2", cik=release["cik"], period_end_date="2026-06-30",
                cet1_standardized_pct="13.00", cet1_standardized_source="10q_final",
                earnings_8k_accession=release["current"]["accession"])
    snap["bank_disclosure"] = bank
    assert snap["bank_valuation"] is None
    if source_value:
        release["current"]["ex99_1"]["tables"] = [{
            "title": "Consolidated results (USD per share)",
            "headers": ["", "2Q26", "1Q26"],
            "rows": [["Tangible book value per common share", source_value, "$28.00"]],
        }]
    else:
        snap["earnings_release"] = None
    raw["memo_markdown"] = "Saved edition using the historic bank basis."
    raw["dossier_sha256"] = ""
    raw = EarningsDossier.model_validate(raw).model_dump(mode="json")
    api.EARNINGS_SQUAD_STATES.put(key, {"account_id": owner.account_id, "topic_id": topic_id,
        "run_id": raw["run_id"], "status": "completed", "operation_id": "recheck", "dossier": raw})

    read = client.get(f"{base}/dossier", headers=headers)
    assert read.status_code == 200, read.text
    current = read.json()["dossier"]
    exported = client.get(f"{base}/export", headers=headers, params={"format": "markdown"})
    assert exported.status_code == 200
    assert exported.headers["x-dossier-id"] == current["dossier_id"]
    if disposition == "unchanged":
        assert current["dossier_sha256"] == raw["dossier_sha256"]
        assert raw["memo_markdown"] in exported.text
        assert "bank_restatement" not in api.EARNINGS_SQUAD_STATES.get(key)
    else:
        assert current["supersedes_sha256"] == raw["dossier_sha256"]
        assert raw["memo_markdown"] not in exported.text
        note = current["memo_markdown"]
        verified_bank = current["snapshot"]["bank_disclosure"]
        assert verified_bank["cet1_standardized_pct"] == "13.00"
        if disposition == "corrected":
            assert "29.57美元" in note and "有误" in note
            assert verified_bank["tbvps_bridge"]["tbvps_reported_usd"] == "29.57"
        else:
            assert "资料不足" in note
            assert verified_bank["tbvps_bridge"]["tbvps_reported_usd"] is None
        historic = client.get(f"{base}/dossier", headers=headers, params={"revision": "original",
                              "original_sha256": raw["dossier_sha256"]})
        assert historic.json()["dossier"]["memo_markdown"] == raw["memo_markdown"]
