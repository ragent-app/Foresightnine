"""Regression tests for Direction A Q1-Q5 quality, cost, TTM FCF, citation gate, and UX polish."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import json

from app.fin_data.adapters.sec_edgar import (
    _collect,
    derive_period_metrics,
)
from app.fin_data.models import StatementLine
from app.fin_data.policy import load_policy
from app.research_earnings_diagnostics import (
    SectorApplicability,
)
from app.research_earnings_squad import (
    EarningsSquadOrchestrator,
    EarningsPrefetchSnapshot,
    EvidenceCard,
    EvidenceCardBatch,
    EvidenceCitation,
    ModelUsage,
    OrgGraphEmitter,
    PrefetchedSourceRef,
    SlotModelRequest,
    SlotModelResponse,
    SquadExecutionLedger,
    SquadPrefetchRequest,
    WorkerTask,
    _build_source_footnotes,
    _build_writer_payload,
    _build_writer_system_prompt,
    _compact_stale_worker_tool_messages,
    _compact_tool_output_for_worker,
    _quotation_matches_source,
    build_isolated_bear_prompt,
    gate_evidence_cards,
    resolve_squad_slots,
)
from app.fin_data.adapters.sec_deep import RedlineParagraphDiff, SectionRedlineDiff, FilingRedlineReport
from threading import Event


def _minimal_snapshot() -> EarningsPrefetchSnapshot:
    return EarningsPrefetchSnapshot(
        snapshot_id="snap_test_polish",
        entity_id="us-cik:0001045810",
        symbol="NVDA",
        cik="0001045810",
        company_name="NVIDIA CORP",
        period_label="2027-Q3",
        sector_applicability=SectorApplicability(
            sector_category="non_financial",
            is_gated=False,
            explanation="Standard non-financial company.",
        ),
        prefetched_sources=[
            PrefetchedSourceRef(
                source_id="source_d5374c44abcd",
                kind="official",
                title="NVDA 8-K EX-99.1 Earnings Release (2026-11-17)",
                retrieval_method="sec_edgar",
                content_sha256="a" * 64,
                permissions={"store": True, "display": True, "model_input": True, "export": True},
            ),
        ],
    )


# ---- Q1: Tool capping, Worker sliding compaction, and Bear prompt slimming ----

def test_q1_compact_tool_output_caps_tables_and_text() -> None:
    huge_rows = [[f"Row {i}", f"${i},000"] for i in range(150)]
    out = _compact_tool_output_for_worker(
        "sec_filing_tables",
        {"status": "ok", "tables": [{"title": "Segment Revenue", "rows": huge_rows, "markdown": "X" * 15000}]},
    )
    tbl = out["tables"][0]
    assert len(tbl["rows"]) == 80
    assert tbl["rows_truncated"] is True
    assert tbl["total_rows"] == 150
    assert len(tbl["markdown"]) < 10100


def test_q1_worker_sliding_compaction_keeps_latest_turn_verbatim() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "init"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_sources", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"status": "ok", "source_id": "src_old", "text": "A" * 8000})},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "read_sources", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c2", "content": json.dumps({"status": "ok", "source_id": "src_recent", "text": "B" * 8000})},
    ]
    _compact_stale_worker_tool_messages(messages, keep_recent_tool_turns=1)
    old_parsed = json.loads(messages[3]["content"])
    recent_parsed = json.loads(messages[5]["content"])
    assert old_parsed["compacted_for_context"] is True
    assert old_parsed["source_id"] == "src_old"
    assert len(messages[3]["content"]) < 1000
    assert "compacted_for_context" not in recent_parsed
    assert len(recent_parsed["text"]) == 8000


def test_q1_bear_prompt_compacts_giant_redline() -> None:
    snap = _minimal_snapshot()
    giant_paras = [
        RedlineParagraphDiff(
            change_type="added",
            section_key="item_1a",
            current_text="Risk factor detail " + ("R" * 4000),
            prior_text=None,
        )
        for _ in range(25)
    ]
    snap = snap.model_copy(update={
        "redline_summary": FilingRedlineReport(
            section_diffs={
                "item_1a": SectionRedlineDiff(
                    section_key="item_1a",
                    has_substantive_changes=True,
                    added=giant_paras,
                )
            },
            total_added=25,
        )
    })
    _, user_prompt = build_isolated_bear_prompt(snap, [], [], question="分析NVIDIA距今最新的财报")
    assert len(user_prompt) < 12000


# ---- Q2: SEC EDGAR YTD -> QTD Cash Flow Differencing & TTM Derivation ----

def test_q2_sec_ytd_cashflow_derives_q2_q3_q4_and_ttm() -> None:
    # Realistic US 10-Q/10-K cumulative YTD cash flow entries (Q1=90d, 6M=180d, 9M=270d, FY=365d)
    raw_ocf = [
        {"start": "2025-01-27", "end": "2025-04-27", "val": 20000, "fy": 2026, "fp": "Q1", "form": "10-Q", "filed": "2025-05-28", "accn": "acc-q1"},
        {"start": "2025-01-27", "end": "2025-07-27", "val": 45000, "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2025-08-27", "accn": "acc-q2"},
        {"start": "2025-01-27", "end": "2025-10-26", "val": 75000, "fy": 2026, "fp": "Q3", "form": "10-Q", "filed": "2025-11-20", "accn": "acc-q3"},
        {"start": "2025-01-27", "end": "2026-01-25", "val": 110000, "fy": 2026, "fp": "FY", "form": "10-K", "filed": "2026-02-26", "accn": "acc-fy"},
    ]
    quarters = _collect(raw_ocf, "quarter", flow=True, derive_q4=True, tag="NetCashProvidedByUsedInOperatingActivities", unit="USD")
    by_end = {p.end: p for p in quarters}
    assert by_end[date(2025, 4, 27)].value == Decimal("20000")
    assert by_end[date(2025, 4, 27)].derived is False
    assert by_end[date(2025, 7, 27)].value == Decimal("25000")
    assert by_end[date(2025, 7, 27)].derived is True
    assert by_end[date(2025, 7, 27)].derivation == "6M YTD minus Q1, same tag"
    assert by_end[date(2025, 10, 26)].value == Decimal("30000")
    assert by_end[date(2025, 10, 26)].derivation == "9M YTD minus 6M YTD, same tag"
    assert by_end[date(2026, 1, 25)].value == Decimal("35000")
    assert by_end[date(2026, 1, 25)].derivation == "FY minus 9M YTD, same tag"

    stmt_lines = [
        StatementLine(
            entity_id="us-cik:0001045810",
            statement="cashflow",
            line="operating_cash_flow",
            reported_tag=p.tag,
            taxonomy="us-gaap",
            period_start=p.start,
            period_end=p.end,
            fiscal_label=p.label,
            value=p.value,
            unit="USD",
            currency="USD",
            form=p.form,
            filed_at=p.filed,
            accession=p.accession,
            derived=p.derived,
            derivation=p.derivation,
        )
        for p in quarters
    ]
    derived = derive_period_metrics(stmt_lines)
    ttm_rows = [r for r in derived if r.line == "operating_cash_flow_ttm"]
    assert len(ttm_rows) == 1
    assert ttm_rows[0].value == Decimal("110000")


# ---- Q3: Worker partial status with delivered cards maps to completed ----

class _SinglePartialWorkerRunner:
    def invoke(self, req: SlotModelRequest) -> SlotModelResponse:
        payload = {
            "task_id": "wt_partial_1",
            "worker_role": "worker_guidance",
            "round_number": 1,
            "status": "partial",
            "cards": [
                {
                    "card_id": "card_g1",
                    "worker_role": "worker_guidance",
                    "topic_tag": "guidance",
                    "finding": "Q4 FY27 revenue guidance is $65.0B +/- 2%.",
                    "citations": [{"source_id": "source_d5374c44abcd", "quotation": "Revenue is expected to be $65.0 billion"}],
                    "supporting_fact_ids": [],
                }
            ],
            "unresolved_gaps": ["FMP consensus unavailable"],
        }
        return SlotModelResponse(content=json.dumps(payload), usage=ModelUsage(input_tokens=100, output_tokens=50, total_tokens=150))


def test_q3_worker_partial_with_cards_emits_completed_runtime_state() -> None:
    squad = EarningsSquadOrchestrator(
        model_runner=_SinglePartialWorkerRunner(),
        slot_bindings=resolve_squad_slots(),
    )
    snap = _minimal_snapshot()
    req = SquadPrefetchRequest(question="分析NVDA最新财报", symbol="NVDA")
    task = WorkerTask(
        task_id="wt_partial_1",
        worker_role="worker_guidance",
        objective="Extract Q4 guidance",
        allowed_tools=["read_sources"],
        soft_turn_limit=2,
    )
    emitter = OrgGraphEmitter()
    ledger = SquadExecutionLedger(slot_bindings=resolve_squad_slots())
    batch = squad._execute_worker_task(
        request=req,
        snapshot=snap,
        task=task,
        l2_pack_id="kpi_semis_hardware",
        ledger=ledger,
        emitter=emitter,
        worker_cancel=Event(),
        source_texts={"source_d5374c44abcd": "Revenue is expected to be $65.0 billion, plus or minus 2%."},
        source_permissions={},
    )
    assert batch.status == "partial"
    assert len(batch.cards) == 1
    last_ev = emitter.events[-1]
    assert last_ev.node_id == "worker_guidance"
    assert last_ev.runtime_state == "completed"
    assert last_ev.action_kind == "RESPONDING"
    assert "1 gap(s)" in last_ev.current_activity


# ---- Q4: Normalized Citation Gate & Single-CIK EFTS Policy Inheritance ----

def test_q4_citation_gate_handles_8k_table_formatting_but_blocks_hallucination() -> None:
    src_text = "Non-GAAP gross margin | $ 115,800 | 73.6% — Q3 FY27\nFree cash flow | $ 22,100"
    assert _quotation_matches_source("Non-GAAP gross margin $115,800 73.6% - Q3 FY27", src_text) is True
    assert _quotation_matches_source("Non-GAAP gross margin $999,999 73.6% - Q3 FY27", src_text) is False

    snap = _minimal_snapshot()
    batch = EvidenceCardBatch(
        task_id="t1",
        worker_role="worker_guidance",
        status="completed",
        cards=[
            EvidenceCard(
                card_id="card_nongaap_bridge",
                worker_role="worker_guidance",
                topic_tag="nongaap",
                finding="Non-GAAP gross margin was 73.6%.",
                citations=[
                    EvidenceCitation(
                        source_id="source_d5374c44abcd",
                        quotation="Non-GAAP gross margin $115,800 73.6% - Q3 FY27",
                    )
                ],
            )
        ],
    )
    verified, report, _ = gate_evidence_cards([batch], snap, source_texts={"source_d5374c44abcd": src_text})
    assert len(verified) == 1
    assert report.passed is True


def test_q4_policy_inherits_filing_search_cik_from_filings() -> None:
    legacy_json = json.dumps({
        "version": "legacy-dev",
        "rules": [
            {"provider": "sec_edgar", "category": "filings", "store": True, "display": True, "model_input": True, "export": True}
        ],
    })
    pol = load_policy({"FORESIGHTNINE_FIN_DATA_POLICY": legacy_json})
    cik_decision = pol.decide("sec_edgar", "filing_search_cik")
    assert getattr(cik_decision, "model_input", False) is True
    assert getattr(cik_decision, "export", False) is True
    unscoped = pol.decide("sec_edgar", "filing_search")
    assert getattr(unscoped, "reason", None) == "not_in_policy"

    explicit_deny_json = json.dumps({
        "version": "explicit-deny",
        "rules": [
            {"provider": "sec_edgar", "category": "filings", "store": True, "display": True, "model_input": True, "export": True},
            {"provider": "sec_edgar", "category": "filing_search_cik", "deny": True, "deny_reason": "operator_disabled"},
        ],
    })
    pol_deny = load_policy({"FORESIGHTNINE_FIN_DATA_POLICY": explicit_deny_json})
    assert getattr(pol_deny.decide("sec_edgar", "filing_search_cik"), "reason", None) == "operator_disabled"


# ---- Q5: Bear Language Lock & Writer Human-Readable Footnotes ----

def test_q5_bear_chinese_lock_and_writer_clean_footnotes() -> None:
    snap = _minimal_snapshot()
    sys_bear, _ = build_isolated_bear_prompt(snap, [], [], question="分析NVIDIA距今最新的财报")
    assert "Simplified Chinese (简体中文)" in sys_bear

    dossier_core = {
        "symbol": "NVDA",
        "company_name": "NVIDIA CORP",
        "snapshot": snap,
        "evidence_cards": [],
    }
    footnotes = _build_source_footnotes({
        "symbol": "NVDA",
        "snapshot": {"prefetched_sources": [r.model_dump(mode="json") for r in snap.prefetched_sources]},
        "evidence_cards": [],
    })
    assert footnotes == [{
        "ref": "[1]",
        "source_id": "source_d5374c44abcd",
        "label": "NVDA 8-K EX-99.1 Earnings Release (2026-11-17)",
        "kind": "official",
    }]

    writer_sys = _build_writer_system_prompt(mode="memo", sic_code="3674", question="分析NVIDIA距今最新的财报")
    assert "NEVER expose internal engineering identifiers" in writer_sys
    assert "Gate 状态: exportable: true, passed: false" in writer_sys
    assert "核心结论与预期差（指引与 Non-GAAP 口径）" in writer_sys
    writer_payload = json.loads(_build_writer_payload(dossier_core, mode="memo"))
    assert writer_payload["source_footnotes"][0]["ref"] == "[1]"


def test_q1_role_scoped_worker_snapshot_omits_unused_sections() -> None:
    from app.research_earnings_squad import _compact_snapshot_for_worker

    snap = _minimal_snapshot()
    seg = _compact_snapshot_for_worker(snap, worker_role="worker_segments")
    forensics = _compact_snapshot_for_worker(snap, worker_role="worker_forensics")
    guidance = _compact_snapshot_for_worker(snap, worker_role="worker_guidance")

    assert "redline_summary" not in seg and "consensus_records" not in seg
    assert "consensus_records" not in forensics and "transcript_index" not in forensics
    assert "filing_table_catalog" not in guidance and "redline_summary" not in guidance
    assert "consensus_status" in guidance and "transcript_index" in guidance
