"""Offline end-to-end integration tests for V4a Batch 4 across Squad, V2, V2.5, V3a, Direction B, REST APIs, and Agent tools."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
import pytest

from app.fin_data.adapters.sec_bank_disclosure import (
    BankDisclosureSnapshot,
    BankSegmentRow,
    BankSegmentSnapshot,
    TbvpsReconciliationBridge,
)
from app.fin_data.adapters.sec_edgar import cik10
from app.fin_data.models import ToolResult
from app.main import (
    BANK_DISCLOSURE_SNAPSHOTS,
    PEER_COMPANY_SNAPSHOTS,
    _build_bank_delta_inputs,
    _v3a_bank_disclosure_loader,
    app,
    reset_state,
)
from app.model_protocol import ModelUsage
from app.research_bank_diagnostics import evaluate_bank_diagnostics, evaluate_bank_valuation
from app.research_call_collision import (
    ManagementFactualClaim,
    collide_bank_segment_claims,
)
from app.research_call_verification import run_call_verification_pipeline
from app.research_delta_review import compute_delta_stage0
from app.research_earnings_export import export_earnings_dossier
from app.research_earnings_squad import (
    DictSlotModelRunner,
    DossierGateReport,
    EarningsDossier,
    EarningsPrefetchSnapshot,
    EarningsSquadOrchestrator,
    OrgGraphEmitter,
    PrefetchDependencies,
    SlotBinding,
    SlotModelResponse,
    SlotUsageSummary,
    SquadExecutionLedger,
    SquadPrefetchRequest,
    WORKER_ALLOWED_TOOLS,
    _render_l1_l2_pack_for_worker,
    collect_valid_evidence_anchors,
    resolve_method_pack_context,
)
from app.research_fin_tools import (
    BankFundamentalsToolRequest,
    BankValuationPeerToolRequest,
    FinDataToolbox,
)
from app.research_governance_audit import compute_capital_allocation_audit, load_v25_governance_rules
from app.research_peer_compare import (
    PeerBankMetrics,
    PeerCompanySnapshot,
    assemble_bank_peer_matrix,
    compute_company_metrics_from_snapshot,
    generate_peer_attribution_brief,
)
from app.research_radar_scheduler import (
    ChangeInboxItem,
    FilingWatchSubscription,
    PeerWatchConfig,
    evaluate_peer_relative_watch,
)
from app.research_sources import ResearchSource, SourcePermissions
from app.research_thesis_ledger import WatchTrigger, build_thesis_draft_from_dossier
from app.research_tool_executor import TOOL_HANDLERS
from app.research_tools_registry import TOOL_REGISTRY


def _bank_peer_snapshot(
    *,
    symbol: str,
    cik: str,
    sic: str,
    deposits: str = "2000000000000",
    loans: str = "1200000000000",
    assets: str = "3500000000000",
    revenue: str = "30000000000",
    net_income_common: str = "6500000000",
    filed: str = "2026-10-31",
    end: str = "2026-09-30",
) -> PeerCompanySnapshot:
    def _row(val: str, form: str = "10-Q", fp: str = "Q3", fy: int = 2026, end_d: str = end, start_d: str | None = "2026-07-01") -> dict[str, Any]:
        out: dict[str, Any] = {
            "val": float(val),
            "form": form,
            "filed": filed,
            "end": end_d,
            "fy": fy,
            "fp": fp,
            "accn": f"{cik10(cik)}-26-000101",
        }
        if start_d is not None:
            out["start"] = start_d
        return out

    facts: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {
        "assets": {"Assets": {"USD": [_row(assets, start_d=None)]}},
        "total_assets": {"Assets": {"USD": [_row(assets, start_d=None)]}},
        "stockholders_equity": {"StockholdersEquity": {"USD": [_row("300000000000", start_d=None)]}},
        "common_shares_outstanding": {"CommonStockSharesOutstanding": {"shares": [_row("2800000000", start_d=None)]}},
        "revenue": {"Revenues": {"USD": [_row(revenue)]}},
        "bank_net_revenue": {"Revenues": {"USD": [_row(revenue)]}},
        "net_interest_income": {"InterestIncomeExpenseNet": {"USD": [_row("2000000000" if sic == "6211" else "16000000000")]}},
        "net_income": {"NetIncomeLoss": {"USD": [_row(net_income_common), _row(net_income_common, form="10-K", fp="FY", start_d="2025-10-01")]}},
        "net_income_common": {"NetIncomeLossAvailableToCommonStockholdersBasic": {"USD": [_row(net_income_common), _row(net_income_common, form="10-K", fp="FY", start_d="2025-10-01")]}},
        "operating_cash_flow": {"NetCashProvidedByUsedInOperatingActivities": {"USD": [_row("12000000000")]}},
        "dividends_paid": {"PaymentsOfDividendsCommonStock": {"USD": [_row("4000000000", form="10-K", fp="FY", start_d="2025-10-01")]}},
        "share_repurchases": {"PaymentsForRepurchaseOfCommonStock": {"USD": [_row("6000000000", form="10-K", fp="FY", start_d="2025-10-01")]}},
        "stock_based_compensation": {"ShareBasedCompensation": {"USD": [_row("500000000", form="10-K", fp="FY", start_d="2025-10-01")]}},
    }
    if Decimal(deposits) > 0:
        facts["deposits"] = {"Deposits": {"USD": [_row(deposits, start_d=None)]}}
    if Decimal(loans) > 0:
        facts["loans_net"] = {"LoansAndLeasesReceivableNetReportedAmount": {"USD": [_row(loans, start_d=None)]}}
    return PeerCompanySnapshot(
        cik=cik10(cik),
        symbol=symbol,
        company_name=f"{symbol} Holding Co.",
        sic=sic,
        sic_description="Commercial Banks" if sic.startswith("602") else "Security Brokers",
        latest_periodic_accession=f"{cik10(cik)}-26-000101",
        latest_periodic_filed_at=filed,
        projection_version=6,
        facts_by_line=facts,
    )


def _bank_disclosure_fixture(
    *,
    symbol: str = "JPM",
    cik: str = "0000019617",
    sic: str = "6021",
    bank_subtype: str = "universal_commercial",
    cet1_pct: str = "14.80",
    cet1_min: str = "12.50",
    htm_pnl: str = "-25831000000",
    tce_usd: str = "290000000000",
    rwa_usd: str = "1800000000000",
    tbvps_rep: str = "105.00",
    tbvps_std: str = "101.20",
    rotce_q: str = "20.00",
    dep_cur: str = "2400000000000",
    dep_pri: str = "2380000000000",
) -> BankDisclosureSnapshot:
    segs = BankSegmentSnapshot(
        cik=cik10(cik),
        symbol=symbol,
        accession=f"{cik10(cik)}-26-000101",
        r_file="R45.htm",
        source_url=f"https://www.sec.gov/Archives/edgar/data/{int(cik10(cik))}/R45.htm",
        period_kind="3M",
        period_end_date="2026-09-30",
        consolidated_basis="gaap",
        segments_basis="managed_fte" if symbol == "JPM" else "gaap",
        consolidated_net_revenue_usd="46000000000",
        fte_reconciling_adjustment_usd="-1100000000" if symbol == "JPM" else None,
        operating_segments=[
            BankSegmentRow(
                segment_key="ccb",
                display_name="Consumer & Community Banking",
                section_role="operating_segment",
                reporting_basis="managed_fte" if symbol == "JPM" else "gaap",
                net_revenue_usd="18500000000",
                pretax_income_usd="6200000000",
                net_income_usd="4700000000",
                segment_roe_or_rotce_pct="32.0",
                total_assets_usd="650000000000",
            ),
            BankSegmentRow(
                segment_key="cib",
                display_name="Commercial & Investment Bank",
                section_role="operating_segment",
                reporting_basis="managed_fte" if symbol == "JPM" else "gaap",
                net_revenue_usd="19500000000",
                pretax_income_usd="8100000000",
                net_income_usd="6100000000",
                segment_roe_or_rotce_pct="18.0",
                total_assets_usd="1500000000000",
            ),
        ],
        corporate_or_other=[
            BankSegmentRow(
                segment_key="corporate",
                display_name="Corporate",
                section_role="corporate_or_other",
                reporting_basis="managed_fte" if symbol == "JPM" else "gaap",
                net_revenue_usd="9100000000",
            )
        ],
        reconciling_items=(
            [
                BankSegmentRow(
                    segment_key="fte_elim",
                    display_name="Tax-equivalent adjustment",
                    section_role="reconciling_or_elimination",
                    reporting_basis="gaap",
                    net_revenue_usd="-1100000000",
                )
            ]
            if symbol == "JPM"
            else []
        ),
        reconciliation_delta_usd="0",
        reconciliation_status="exact",
    )
    return BankDisclosureSnapshot(
        cik=cik10(cik),
        symbol=symbol,
        company_name=f"{symbol} Corp.",
        sic=sic,
        bank_subtype=bank_subtype,  # type: ignore[arg-type]
        period_end_date="2026-09-30",
        fiscal_period_label="FY2026-Q3",
        periodic_form="10-Q",
        periodic_accession=f"{cik10(cik)}-26-000101",
        periodic_filed_at="2026-10-31",
        net_revenue_gaap_q_usd="46000000000",
        net_revenue_managed_q_usd="47100000000" if symbol == "JPM" else None,
        nii_gaap_q_usd="24000000000",
        noninterest_income_gaap_q_usd="22000000000",
        noninterest_expense_q_usd="24000000000",
        ppnr_gaap_q_usd="22000000000",
        provision_q_usd="2500000000",
        net_income_q_usd="13500000000",
        net_income_to_common_q_usd="13100000000",
        net_revenue_gaap_ttm_usd="180000000000",
        ppnr_gaap_ttm_usd="84000000000",
        net_income_to_common_ttm_usd="50000000000",
        total_assets_usd="4200000000000",
        total_deposits_usd=dep_cur,
        total_deposits_prior_q_usd=dep_pri,
        total_loans_gross_usd="1350000000000",
        allowance_credit_losses_usd="24000000000",
        nonaccrual_loans_usd="7000000000",
        net_charge_offs_q_usd="1800000000",
        cet1_standardized_pct=cet1_pct,
        cet1_standardized_source="10q_final",
        cet1_reg_min_pct=cet1_min,
        rwa_standardized_usd=rwa_usd,
        slr_pct="6.0",
        slr_min_pct="5.0",
        htm_net_unrealized_pnl_usd=htm_pnl,
        rotce_q_reported_pct=rotce_q,
        common_dividends_ttm_usd="14000000000",
        common_buybacks_ttm_usd="22000000000",
        tbvps_bridge=TbvpsReconciliationBridge(
            total_stockholders_equity_usd="360000000000",
            preferred_stock_usd="20000000000",
            common_equity_usd="340000000000",
            goodwill_usd="50000000000",
            tce_reported_usd=tce_usd,
            tce_xbrl_std_usd="283360000000",
            shares_ending_common="2800000000",
            shares_reported_denominator="2761904762",
            rsu_share_addon="0",
            tbvps_reported_usd=tbvps_rep,
            tbvps_reported_source="8k_supplement",
            tbvps_xbrl_std_usd=tbvps_std,
            tbvps_gap_pct="-3.62",
        ),
        segments=segs,
    )


class _RepoDictCache:
    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> Any:
        return self._store.get(key)

    def put(self, key: str, value: Any) -> None:
        if hasattr(value, "model_dump"):
            self._store[key] = value.model_dump(mode="json")
        elif isinstance(value, dict):
            self._store[key] = dict(value)
        else:
            self._store[key] = value


class _FakeSquadSecAdapter:
    def __init__(self, cik: str, symbol: str, sic: str, company_name: str) -> None:
        self._cik = cik10(cik)
        self._symbol = symbol
        self._sic = sic
        self._name = company_name
        self._ua = "ForesightNine/1.0 test@foresightnine.local"
        self._transport = None
        self._stream = None
        self._policy = None

    def _gate(self, _category: str) -> ToolResult:
        return ToolResult(status="empty", reason="no_data", message="mock empty")

    def statement_lines(self, *_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(status="empty", data=(), message="mock empty", reason="no_data")

    def _get(self, url: str, _cat: str, _err: str, large: bool = False):
        if "submissions/CIK" in url:
            return (
                {
                    "cik": self._cik,
                    "name": self._name,
                    "sic": self._sic,
                    "sicDescription": "Security Brokers & Dealers" if self._sic == "6211" else "Commercial Banks",
                    "tickers": [self._symbol],
                    "filings": {
                        "recent": {
                            "form": ["10-Q"],
                            "accessionNumber": [f"{self._cik}-26-000101"],
                            "filingDate": ["2026-10-31"],
                            "reportDate": ["2026-09-30"],
                            "primaryDocument": ["q3.htm"],
                        }
                    },
                },
                {"provider": "sec_edgar"},
                0,
                {"store": True, "display": True, "model_input": True, "export": True},
            )
        return None, {}, 0, {}


def _squad_bindings() -> dict[str, SlotBinding]:
    return {
        "fast_extractor": SlotBinding(slot="fast_extractor", model="fake-fast", native_tools=True),
        "deep_reasoner": SlotBinding(slot="deep_reasoner", model="fake-deep", native_tools=True),
        "bear_reviewer": SlotBinding(slot="bear_reviewer", model="fake-bear", native_tools=False),
        "writer": SlotBinding(slot="writer", model="fake-writer", native_tools=False),
    }


def _create_ephemeral_account(client: TestClient) -> tuple[str, dict[str, str]]:
    email = f"v4a_int_{datetime.now(timezone.utc).timestamp()}@test.local"
    r = client.post(
        "/v1/auth/local/register",
        json={"email": email, "password": "StrongPassword!123"},
    )
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    acct_id = r.json()["account"]["account_id"]
    return acct_id, {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- 1. Squad & D2 Cache Isolation (GS -> BLK)
def test_v4a_squad_gs_activated_and_blk_isolated_after_gs() -> None:
    snap_cache = _RepoDictCache()
    bank_cache = _RepoDictCache()

    gs_peer = _bank_peer_snapshot(
        symbol="GS",
        cik="0000886982",
        sic="6211",
        deposits="460000000000",
        loans="210000000000",
        assets="1750000000000",
    )
    gs_disc = _bank_disclosure_fixture(
        symbol="GS",
        cik="0000886982",
        sic="6211",
        bank_subtype="investment_bank_bhc",
        cet1_pct="14.20",
        cet1_min="13.50",
        htm_pnl="-1818000000",
        tbvps_rep="340.00",
        tbvps_std="338.10",
    )
    snap_cache.put(f"snap:{cik10('0000886982')}", gs_peer)
    bank_cache.put(f"bank:{cik10('0000886982')}:latest", gs_disc)

    # BLK shares SIC 6211 with GS, but has zero banking book (Deposits=0, Loans=0)
    blk_peer = _bank_peer_snapshot(
        symbol="BLK",
        cik="0001364742",
        sic="6211",
        deposits="0",
        loans="0",
        assets="138000000000",
    )
    snap_cache.put(f"snap:{cik10('0001364742')}", blk_peer)

    # Verify method pack cache keys do not cross-contaminate on SIC 6211
    gs_ctx = resolve_method_pack_context("6211", active_sector_plugin="us_large_bank_v4a")
    blk_ctx = resolve_method_pack_context("6211", active_sector_plugin=None)
    assert gs_ctx["l2_pack_id"] == "kpi_us_large_banks"
    assert blk_ctx["l2_pack_id"] is None

    # Full Stage 0 prefetch on GS -> Stage 0 prefetch on BLK in same process
    dummy_runner = DictSlotModelRunner({})
    orch_gs = EarningsSquadOrchestrator(
        model_runner=dummy_runner,
        slot_bindings=_squad_bindings(),
        prefetch_deps=PrefetchDependencies(
            peer_snapshot_cache=snap_cache,
            bank_disclosure_cache=bank_cache,
            market_price_fn=lambda _e, _s: "520.00",
        ),
    )
    req_gs = SquadPrefetchRequest(question="Analyze GS Q3 banking and trading results", cik="0000886982", symbol="GS", sic_code="6211")
    snap_gs = orch_gs._run_stage_0_prefetch(
        req_gs,
        ledger=SquadExecutionLedger(slot_bindings=orch_gs.slot_bindings),
        emitter=OrgGraphEmitter(),
        source_texts={},
        source_permissions={},
    )
    assert snap_gs.sector_applicability.active_sector_plugin == "us_large_bank_v4a"
    assert snap_gs.sector_applicability.bank_subtype == "investment_bank_bhc"
    assert snap_gs.bank_diagnostics is not None
    assert snap_gs.bank_valuation is not None and snap_gs.bank_valuation.p_to_tbv_primary is not None
    anchors_gs = collect_valid_evidence_anchors(snap_gs, [])
    assert any(a.startswith("bank_val:") for a in anchors_gs)
    assert any(a.startswith("bank_seg:") for a in anchors_gs)

    pack_gs = _render_l1_l2_pack_for_worker(snap_gs, l2_pack_id=None, worker_role="worker_segments")
    assert pack_gs["l2_pack_id"] == "kpi_us_large_banks"

    # Immediately run Stage 0 for BLK (same SIC 6211)
    orch_blk = EarningsSquadOrchestrator(
        model_runner=dummy_runner,
        slot_bindings=_squad_bindings(),
        prefetch_deps=PrefetchDependencies(
            peer_snapshot_cache=snap_cache,
            bank_disclosure_cache=bank_cache,
            market_price_fn=lambda _e, _s: "980.00",
        ),
    )
    req_blk = SquadPrefetchRequest(question="Analyze BLK Q3 AUM and flows", cik="0001364742", symbol="BLK", sic_code="6211")
    snap_blk = orch_blk._run_stage_0_prefetch(
        req_blk,
        ledger=SquadExecutionLedger(slot_bindings=orch_blk.slot_bindings),
        emitter=OrgGraphEmitter(),
        source_texts={},
        source_permissions={},
    )
    assert snap_blk.sector_applicability.is_gated is True
    assert snap_blk.sector_applicability.active_sector_plugin is None
    assert snap_blk.bank_disclosure is None
    pack_blk = _render_l1_l2_pack_for_worker(snap_blk, l2_pack_id="kpi_us_large_banks", worker_role="worker_segments")
    assert pack_blk["l2_pack_id"] is None

    assert "bank_fundamentals_diagnostics" in WORKER_ALLOWED_TOOLS["worker_segments"]
    assert "bank_valuation_and_peer_compare" in WORKER_ALLOWED_TOOLS["worker_guidance"]


# --------------------------------------------------------------------------- 2. V2 6-Bank Compare, Dual TBVPS Toggle & Brief Gate
def test_v4a_v2_bank_peer_compare_toggle_and_brief() -> None:
    banks = [
        ("JPM", "0000019617", "6021", "universal_commercial", "14.80", "12.50", "-25831000000", "105.00", "101.20", "220.00"),
        ("BAC", "0000070858", "6021", "universal_commercial", "10.90", "10.00", "-103913000000", "29.14", "28.90", "45.00"),
        ("WFC", "0000072971", "6021", "universal_commercial", "11.10", "9.00", "-43228000000", "43.50", "42.80", "70.00"),
        ("C", "0000831001", "6021", "universal_commercial", "13.20", "12.10", "-14296000000", "89.00", "87.50", "72.00"),
        ("GS", "0000886982", "6211", "investment_bank_bhc", "14.20", "13.50", "-1818000000", "340.00", "338.00", "520.00"),
        ("MS", "0000895421", "6211", "investment_bank_bhc", "15.10", "12.50", "-9545000000", "48.00", "47.20", "120.00"),
    ]
    legacy_rows = []
    bank_rows_rep: list[PeerBankMetrics] = []
    bank_rows_xbrl: list[PeerBankMetrics] = []
    for sym, cik, sic, sub, cet1, cmin, htm, trep, tstd, px in banks:
        ps = _bank_peer_snapshot(symbol=sym, cik=cik, sic=sic)
        legacy_rows.append(compute_company_metrics_from_snapshot(ps, price_usd=Decimal(px)))
        tce_override = "140000000000" if sym == "WFC" else ("201760000000" if sym == "BAC" else "290000000000")
        bd = _bank_disclosure_fixture(symbol=sym, cik=cik, sic=sic, bank_subtype=sub, cet1_pct=cet1, cet1_min=cmin, htm_pnl=htm, tce_usd=tce_override, tbvps_rep=trep, tbvps_std=tstd)
        diag = evaluate_bank_diagnostics(bd)
        val_r = evaluate_bank_valuation(bd, price_usd=Decimal(px), tbvps_basis="reported_market_convention")
        val_x = evaluate_bank_valuation(bd, price_usd=Decimal(px), tbvps_basis="xbrl_standardized")
        for target_list, val in ((bank_rows_rep, val_r), (bank_rows_xbrl, val_x)):
            target_list.append(PeerBankMetrics(
                symbol=sym,
                cik=bd.cik,
                company_name=bd.company_name,
                sic=bd.sic,
                bank_subtype=bd.bank_subtype,
                period_end_date=bd.period_end_date,
                fiscal_period_label=bd.fiscal_period_label,
                bank_quality_light=diag.bank_quality_light,
                price_usd=val.price_usd,
                market_cap_usd=val.market_cap_usd,
                tbvps_reported_usd=bd.tbvps_bridge.tbvps_reported_usd,
                tbvps_xbrl_std_usd=bd.tbvps_bridge.tbvps_xbrl_std_usd,
                tbvps_primary_usd=(
                    bd.tbvps_bridge.tbvps_xbrl_std_usd
                    if val.primary_tbvps_basis in ("xbrl_standardized", "xbrl_std_fallback")
                    else bd.tbvps_bridge.tbvps_reported_usd
                ),
                tbvps_basis=val.primary_tbvps_basis,
                tbvps_gap_pct=bd.tbvps_bridge.tbvps_gap_pct,
                p_to_tbv_primary=val.p_to_tbv_primary,
                p_to_tbv_xbrl_std=val.p_to_tbv_xbrl_std,
                p_to_bv=val.p_to_bv,
                p_to_e_ttm=val.p_to_e_ttm,
                p_to_ppnr_ttm=val.p_to_ppnr_ttm,
                rotce_q_pct=diag.rotce_q_pct,
                implied_rotce_pct=val.implied_rotce_pct,
                rotce_spread_vs_implied_pp=val.rotce_spread_vs_implied_pp,
                efficiency_ratio_gaap_pct=diag.efficiency_ratio_gaap_pct,
                ppnr_margin_pct=diag.ppnr_margin_pct,
                nii_share_pct=diag.nii_share_pct,
                cet1_standardized_pct=bd.cet1_standardized_pct,
                cet1_reg_min_pct=bd.cet1_reg_min_pct,
                cet1_buffer_bps=diag.cet1_buffer_bps,
                htm_loss_pct_of_tce=diag.htm_loss_pct_of_tce,
                pro_forma_cet1_ex_htm_pct=diag.pro_forma_cet1_ex_htm_pct,
                provision_to_ppnr_pct=diag.provision_to_ppnr_pct,
            ))

    mat_rep = assemble_bank_peer_matrix(
        primary_symbol="JPM",
        rows=legacy_rows,
        bank_rows=bank_rows_rep,
        tbvps_display_mode="reported_market_convention",
    )
    mat_xbrl = assemble_bank_peer_matrix(
        primary_symbol="JPM",
        rows=legacy_rows,
        bank_rows=bank_rows_xbrl,
        tbvps_display_mode="xbrl_standardized",
    )
    assert mat_rep.comparison_mode == "us_large_banks"
    assert len(mat_rep.bank_companies) == 6
    jpm_rep = next(b for b in mat_rep.bank_companies if b.symbol == "JPM")
    jpm_xbrl = next(b for b in mat_xbrl.bank_companies if b.symbol == "JPM")
    bac_rep = next(b for b in mat_rep.bank_companies if b.symbol == "BAC")
    wfc_rep = next(b for b in mat_rep.bank_companies if b.symbol == "WFC")
    assert jpm_rep.p_to_tbv_primary != jpm_xbrl.p_to_tbv_primary
    assert bac_rep.bank_quality_light == "red"
    assert wfc_rep.bank_quality_light == "yellow"

    # Brief numeric gate on bank_companies + bank_peer_medians
    brief_payload = {
        "summary": f"JPM trades at P/TBV {jpm_rep.p_to_tbv_primary}x vs ROTCE {jpm_rep.rotce_q_pct}%.",
        "valuation_vs_fundamentals": f"JPM CET1 buffer stands at {jpm_rep.cet1_buffer_bps} bps.",
        "relative_risks": ["Peer CET1 and P/TBV dispersed across 6 US major banks; monitor BAC HTM loss overhang."],
        "cited_metrics": [
            {"symbol": "JPM", "metric_key": "p_to_tbv_primary", "value": str(jpm_rep.p_to_tbv_primary)},
            {"symbol": "JPM", "metric_key": "cet1_buffer_bps", "value": str(jpm_rep.cet1_buffer_bps)},
        ],
    }

    class _BriefRunner:
        def invoke(self, _req: Any, **_kw: Any) -> SlotModelResponse:
            return SlotModelResponse(content=json.dumps(brief_payload), usage=ModelUsage(input_tokens=100, output_tokens=50, total_tokens=150))

    brief = generate_peer_attribution_brief(mat_rep, runner=_BriefRunner(), slot="deep_reasoner")
    assert str(jpm_rep.p_to_tbv_primary) in brief.summary
    assert brief.verified_metric_count == 2


# --------------------------------------------------------------------------- 3. V2.5 Governance Bank Capital Allocation
def test_v4a_v25_governance_bank_payout_ratios() -> None:
    snap = _bank_peer_snapshot(symbol="JPM", cik="0000019617", sic="6021", net_income_common="48000000000")
    rules, _sha, _comp = load_v25_governance_rules()
    ca = compute_capital_allocation_audit(snap, rules=rules, price_usd=Decimal("220.00"))
    assert ca.total_payout_pct_of_net_income_common is not None
    assert ca.sbc_pct_of_net_income_common is not None


# --------------------------------------------------------------------------- 4. V3a D6 Bank Segment Call Collision & Strict Basis Isolation
def test_v4a_v3a_call_segment_collision_0llm_and_unmatched_fallback() -> None:
    jpm_disc = _bank_disclosure_fixture(symbol="JPM", cik="0000019617")
    jpm_peer = _bank_peer_snapshot(symbol="JPM", cik="0000019617", sic="6021")

    claims = [
        ManagementFactualClaim(
            claim_id="mc_ccb_exact",
            metric_canonical="segment_revenue",
            kpi_key="Consumer & Community Banking revenue",
            scope="segment",
            basis="fte_managed",
            operator="approx",
            value_low="18500",
            value_high="18500",
            unit="usd_millions",
            source_id="src_call_jpm",
            verbatim_quote="Consumer & Community Banking generated $18.5 billion of revenue on a managed basis.",
            claim_summary="CCB revenue $18.5B (Managed)",
        ),
        ManagementFactualClaim(
            claim_id="mc_card_subline",
            metric_canonical="other",
            kpi_key="debit card interchange",
            scope="segment",
            basis="fte_managed",
            operator="approx",
            value_low="14",
            value_high="14",
            unit="percent",
            source_id="src_call_jpm",
            verbatim_quote="Debit card interchange fees grew 14% year over year.",
            claim_summary="Debit card interchange +14% YoY",
        ),
    ]
    matched, unmatched = collide_bank_segment_claims(
        claims=claims,
        bank_disclosure=jpm_disc.model_dump(mode="json"),
        call_default_basis="fte_managed",
        fiscal_period_key="2026Q3",
    )
    assert len(matched) == 1
    assert matched[0].finding_id.startswith("bseg_")
    assert matched[0].collision_kind == "corroborated"
    assert len(unmatched) == 1
    assert unmatched[0].claim_id == "mc_card_subline"

    # Full pipeline check: matched segment claims resolve via bank_v4a_matched; footnote loader runs ONLY for unmatched sub-lines
    fallback_calls: list[Any] = []
    call_src = ResearchSource(
        source_id="src_call_jpm_1",
        owner_id="acct_test",
        title="JPM Q3 2026 Earnings Call",
        text=(
            "Jamie Dimon - CEO: Welcome to JPMorgan Chase Q3 2026 earnings call.\n"
            "Jeremy Barnum - CFO: Note our segment results are presented on a managed FTE basis. "
            "Consumer & Community Banking generated $18.5 billion in net revenue in Q3.\n"
            "Question-and-Answer Session\n"
            "Analyst - Betsy Graseck: Can you update on Consumer & Community Banking revenue drivers?\n"
            "Jeremy Barnum - CFO: Consumer & Community Banking generated $18.5 billion in net revenue."
        ),
        kind="user_supplied",
        is_simulated=False,
        published_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
        obtained_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
        content_sha256="a" * 64,
        retrieval_method="user_supplied",
        permissions=SourcePermissions(store=True, model_input=True, display=True, export=True),
    )

    class _FakeCallRunner:
        def invoke(self, req: Any, **_kw: Any) -> SlotModelResponse:
            stage = getattr(req, "stage", "") or ""
            if stage in ("v3a_call_extract", "v3a_call_extract_remarks"):
                return SlotModelResponse(
                    content=json.dumps({
                        "call_default_basis": "fte_managed",
                        "factual_claims": [
                            {
                                "claim_id": "c_ccb",
                                "metric_canonical": "segment_revenue",
                                "kpi_key": "Consumer & Community Banking",
                                "scope": "segment",
                                "basis": "fte_managed",
                                "operator": "approx",
                                "value_low": "18500",
                                "value_high": "18500",
                                "unit": "usd_millions",
                                "source_id": "src_call_jpm_1",
                                "verbatim_quote": "Consumer & Community Banking generated $18.5 billion in net revenue",
                                "claim_summary": "CCB net revenue $18.5B managed",
                            }
                        ],
                        "verbal_commitments": [],
                        "qa_evasions": [],
                        "internal_contradictions": [],
                        "expectation_signals": [],
                        "skeptic_notes": [],
                    }),
                    usage=ModelUsage(input_tokens=100, output_tokens=50, total_tokens=150),
                )
            return SlotModelResponse(
                content=json.dumps({
                    "call_default_basis": "fte_managed",
                    "factual_claims": [],
                    "verbal_commitments": [],
                    "qa_evasions": [],
                    "internal_contradictions": [],
                    "expectation_signals": [],
                    "skeptic_notes": [],
                }),
                usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            )

    report, ledger, _vcl = run_call_verification_pipeline(
        account_id="acct_test",
        sources=[call_src],
        symbol_override="JPM",
        cik_override="0000019617",
        cache_only=False,
        model_runner=_FakeCallRunner(),
        snap_loader=lambda _c, _s: (jpm_peer, None),
        bank_disclosure_loader=lambda _sn, _subs, _ao: jpm_disc,
        footnote_fallback_loader=lambda *a, **kw: fallback_calls.append((a, kw)) or [],
    )
    assert ledger is not None and ledger.status == "bank_v4a_matched"
    assert any(f.finding_id.startswith("bseg_") for f in report.collision_findings)
    assert fallback_calls == []


# --------------------------------------------------------------------------- 5. Direction B & Export Markdown
def test_v4a_direction_b_delta_radar_thesis_and_export() -> None:
    bac_peer = _bank_peer_snapshot(symbol="BAC", cik="0000070858", sic="6021")
    bac_disc = _bank_disclosure_fixture(
        symbol="BAC",
        cik="0000070858",
        sic="6021",
        cet1_pct="10.90",
        cet1_min="10.00",
        htm_pnl="-103913000000",
        tce_usd="201760000000",
        dep_cur="1950000000000",
        dep_pri="2038000000000",
    )
    BANK_DISCLOSURE_SNAPSHOTS.put("bank:0000070858:latest", bac_disc.model_dump(mode="json"))
    PEER_COMPANY_SNAPSHOTS.put("sym:BAC", {"cik": "0000070858", "symbol": "BAC"})
    PEER_COMPANY_SNAPSHOTS.put("snap:0000070858", bac_peer.model_dump(mode="json"))

    obs, txt = _build_bank_delta_inputs(cik="0000070858", symbol="BAC", cache_only=True)
    assert obs.get("cet1_standardized_pct") == "10.90"
    assert obs.get("htm_loss_pct_of_tce") is not None
    assert "BAC" in txt

    delta_pkg = compute_delta_stage0(
        cik="0000070858",
        symbol="BAC",
        accession="0000070858-26-000550",
        form="10-Q",
        observed_metrics=obs,
    )
    assert delta_pkg.bank_metric_deltas

    # Radar CET1 buffer 25 bps hysteresis
    sub = FilingWatchSubscription(
        account_id="acct_rad",
        cik="0000070858",
        symbol="BAC",
        watched_channels=["peer_relative"],
        peer_watch_config=PeerWatchConfig(
            symbols=["BAC", "JPM"],
            triggers=[
                WatchTrigger(
                    kind="metric_rule",
                    description="BAC CET1 buffer >= 100 bps",
                    metric_key="peer_cet1_buffer_bps",
                    operator=">=",
                    threshold_decimal="100",
                )
            ],
        ),
    )
    diag = evaluate_bank_diagnostics(bac_disc)
    val = evaluate_bank_valuation(bac_disc, price_usd=Decimal("45.00"))
    mat_90 = assemble_bank_peer_matrix(
        primary_symbol="BAC",
        rows=[compute_company_metrics_from_snapshot(bac_peer, price_usd=Decimal("45.00")), compute_company_metrics_from_snapshot(_bank_peer_snapshot(symbol="JPM", cik="0000019617", sic="6021"), price_usd=Decimal("220.00"))],
        bank_rows=[
            PeerBankMetrics(symbol="BAC", cik="0000070858", company_name="BAC", bank_subtype="universal_commercial", period_end_date="2026-09-30", bank_quality_light=diag.bank_quality_light, cet1_buffer_bps="90", p_to_tbv_primary=val.p_to_tbv_primary),
            PeerBankMetrics(symbol="JPM", cik="0000019617", company_name="JPM", bank_subtype="universal_commercial", period_end_date="2026-09-30", bank_quality_light="green", cet1_buffer_bps="230", p_to_tbv_primary="2.10"),
        ],
    )
    first_items = evaluate_peer_relative_watch(sub=sub, ledger=None, existing_inbox=[], peer_compare_fn=lambda _s, _p: mat_90)
    assert len(first_items) == 1
    reviewed_items = [it.model_copy(update={"review_state": "accepted"}) for it in first_items]

    # Small additional drop of 10 bps (<25 bps hysteresis) stays suppressed; 30 bps drop re-emits
    mat_80 = assemble_bank_peer_matrix(
        primary_symbol="BAC",
        rows=mat_90.companies,
        bank_rows=[mat_90.bank_companies[0].model_copy(update={"cet1_buffer_bps": "80"}), mat_90.bank_companies[1]],
    )
    assert evaluate_peer_relative_watch(sub=sub, ledger=None, existing_inbox=reviewed_items, peer_compare_fn=lambda _s, _p: mat_80) == []
    mat_60 = assemble_bank_peer_matrix(
        primary_symbol="BAC",
        rows=mat_90.companies,
        bank_rows=[mat_90.bank_companies[0].model_copy(update={"cet1_buffer_bps": "60"}), mat_90.bank_companies[1]],
    )
    assert len(evaluate_peer_relative_watch(sub=sub, ledger=None, existing_inbox=reviewed_items, peer_compare_fn=lambda _s, _p: mat_60)) == 1

    # Thesis ledger & Markdown export with bank tables
    snap_full = EarningsPrefetchSnapshot(
        entity_id="cik:0000070858",
        symbol="BAC",
        cik="0000070858",
        company_name="Bank of America Corp.",
        sic_code="6021",
        period_label="FY2026-Q3",
        sector_applicability={"sic_code": "6021", "is_gated": True, "sector_category": "bank", "bank_subtype": "universal_commercial", "active_sector_plugin": "us_large_bank_v4a", "explanation": "Bank", "disabled_metrics": [], "recommended_alternatives": []},
        bank_disclosure=bac_disc,
        bank_diagnostics=diag,
        bank_valuation=val,
    )
    dossier = EarningsDossier(
        dossier_id="dos_bac_v4a",
        run_id="run_bac",
        entity_id="cik:0000070858",
        symbol="BAC",
        cik="0000070858",
        company_name="Bank of America Corp.",
        period_label="FY2026-Q3",
        sector_applicability=snap_full.sector_applicability,
        selected_l2_pack_id="kpi_us_large_banks",
        snapshot=snap_full,
        gate_report=DossierGateReport(passed=True),
        slot_usage_summary=SlotUsageSummary(slots={}),
        locked=True,
        memo_markdown="# BAC Memo\nCET1 10.90% with HTM -103913000000.",
    )
    thesis = build_thesis_draft_from_dossier(
        account_id="acct_rad",
        topic_id="top_bac",
        dossier=dossier.model_dump(mode="json"),
    )
    assert any(t.metric_key in ("peer_rotce_spread_pp", "cet1_buffer_bps") for c in thesis.claims for t in c.watch_triggers)

    exported = export_earnings_dossier(dossier, fmt="markdown")
    md_text = exported.text
    assert "V4a" in md_text or "CET1" in md_text
    assert "ROTCE" in md_text


# --------------------------------------------------------------------------- 6. REST Endpoints & 2 Bank Agent Tools
def test_v4a_rest_endpoints_and_two_agent_tools(monkeypatch: Any) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    reset_state()
    assert "bank_fundamentals_diagnostics" in TOOL_REGISTRY
    assert "bank_valuation_and_peer_compare" in TOOL_REGISTRY
    assert set(TOOL_HANDLERS) == set(TOOL_REGISTRY)

    bac_peer = _bank_peer_snapshot(symbol="BAC", cik="0000070858", sic="6021")
    jpm_peer = _bank_peer_snapshot(symbol="JPM", cik="0000019617", sic="6021")
    blk_peer = _bank_peer_snapshot(symbol="BLK", cik="0001364742", sic="6211", deposits="0", loans="0", assets="138000000000")
    bac_disc = _bank_disclosure_fixture(symbol="BAC", cik="0000070858", htm_pnl="-103913000000", tce_usd="201760000000", cet1_pct="10.90", cet1_min="10.00")
    jpm_disc = _bank_disclosure_fixture(symbol="JPM", cik="0000019617")

    snap_cache = _RepoDictCache()
    bank_cache = _RepoDictCache()
    for sym, cik, ps, bd in (
        ("BAC", "0000070858", bac_peer, bac_disc),
        ("JPM", "0000019617", jpm_peer, jpm_disc),
    ):
        snap_cache.put(f"sym:{sym}", {"cik": cik, "symbol": sym})
        snap_cache.put(f"snap:{cik}", ps)
        bank_cache.put(f"bank:{cik}:latest", bd)
    snap_cache.put("sym:BLK", {"cik": "0001364742", "symbol": "BLK"})
    snap_cache.put("snap:0001364742", blk_peer)

    # FinDataToolbox 2 new tools persist export=True official ResearchSources
    persisted: list[ResearchSource] = []

    def _persist(item, **kwargs):
        src = ResearchSource(
            source_id=f"src_test_{len(persisted) + 1}",
            owner_id="acct_tb",
            title=item.title,
            text=item.text,
            url=item.url,
            kind=item.kind,
            is_simulated=False,
            published_at=item.published_at,
            obtained_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
            content_sha256="b" * 64,
            retrieval_method=kwargs.get("retrieval_method", "bank_fundamentals_diagnostics"),
            permissions=item.permissions,
        )
        persisted.append(src)
        return src

    tb = FinDataToolbox(
        account_id="acct_tb",
        market_key=None,
        sec_user_agent="ForesightNine/1.0 test@foresightnine.local",
        persist=_persist,
        peer_snapshot_cache=snap_cache,
        bank_disclosure_cache=bank_cache,
    )
    out_diag = tb.bank_fundamentals_diagnostics(
        BankFundamentalsToolRequest(symbol="BAC", cache_only=True, rationale="Check BAC HTM & CET1")
    )
    assert out_diag.result["status"] == "ok"
    assert out_diag.source is not None and out_diag.source.permissions.export is True
    assert out_diag.source.retrieval_method == "bank_fundamentals_diagnostics"

    # Negative control via tool
    out_blk = tb.bank_fundamentals_diagnostics(
        BankFundamentalsToolRequest(symbol="BLK", cache_only=True, rationale="Verify asset manager rejected")
    )
    assert out_blk.result["status"] == "not_applicable"
    assert out_blk.result["is_bank_v4a"] is False

    # Peer valuation tool (cache_only with mocked quote helper)
    tb._latest_quote_usd_for_symbol = lambda sym, **_kw: (Decimal("45.00" if sym == "BAC" else "220.00"), "2026-11-20", "manual")  # type: ignore[method-assign]
    out_val = tb.bank_valuation_and_peer_compare(
        BankValuationPeerToolRequest(primary_symbol="BAC", peer_symbols=["JPM"], cache_only=True, rationale="Gordon P/TBV")
    )
    assert out_val.result["status"] == "ok"
    assert out_val.source is not None and out_val.source.retrieval_method == "bank_valuation_and_peer_compare"
    assert out_val.source.permissions.export is True

    # FastAPI REST endpoints
    with TestClient(app) as client:
        acct_id, headers = _create_ephemeral_account(client)
        app.state.bank_analysis_hooks = {
            "snapshot_cache": snap_cache,
            "bank_cache": bank_cache,
            "quote_resolver": lambda sym, _ao: (Decimal("45.00" if sym == "BAC" else "220.00"), "2026-11-20", "manual"),
        }
        app.state.peer_compare_hooks = app.state.bank_analysis_hooks
        try:
            r_bac = client.post(
                "/v2/research/banks/analyze",
                headers=headers,
                json={"symbol": "BAC", "cache_only": True, "coe_pct": "10.0", "terminal_g_pct": "3.0"},
            )
            assert r_bac.status_code == 200, r_bac.text
            body_bac = r_bac.json()
            assert body_bac["qualification"]["is_bank_v4a"] is True
            assert body_bac["diagnostics"]["bank_quality_light"] in ("red", "yellow")
            assert body_bac["valuation"]["p_to_tbv_primary"] is not None

            r_seg = client.get("/v2/research/banks/segments?symbol=BAC&cache_only=true", headers=headers)
            assert r_seg.status_code == 200, r_seg.text
            assert r_seg.json()["reconciliation_status"] == "exact"

            r_blk = client.post(
                "/v2/research/banks/analyze",
                headers=headers,
                json={"symbol": "BLK", "cache_only": True},
            )
            assert r_blk.status_code == 422

            r_cmp = client.post(
                "/v2/research/peers/compare",
                headers=headers,
                json={"primary_symbol": "JPM", "symbols": ["JPM", "BAC"], "cache_only": True},
            )
            assert r_cmp.status_code == 200, r_cmp.text
            assert r_cmp.json()["comparison_mode"] == "us_large_banks"
            assert len(r_cmp.json()["bank_companies"]) == 2

            r_watch = client.post(
                "/v2/research/peers/watch",
                headers=headers,
                json={
                    "primary_symbol": "BAC",
                    "symbols": ["BAC", "JPM"],
                    "metric_key": "peer_cet1_buffer_bps",
                    "operator": ">=",
                    "threshold_decimal": "100",
                },
            )
            assert r_watch.status_code == 200, r_watch.text
            assert r_watch.json()["peer_watch_config"]["triggers"][0]["metric_key"] == "peer_cet1_buffer_bps"
        finally:
            app.state.bank_analysis_hooks = None
            app.state.peer_compare_hooks = None
            client.delete("/v1/auth/local/account", headers=headers)
