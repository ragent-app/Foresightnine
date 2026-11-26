"""Offline tests for Batch 3: V4a 5-pillar bank diagnostics, Gordon P/TBV & kpi_us_large_banks L2 pack."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import pytest

from app.fin_data.adapters.sec_bank_disclosure import BankDisclosureSnapshot, TbvpsReconciliationBridge
from app.research_bank_diagnostics import evaluate_bank_diagnostics, evaluate_bank_valuation
from app.research_earnings_diagnostics import evaluate_sec_sic_gate
from app.research_methods import (
    MAX_COMPOSED_PACK_CHARS,
    MAX_INDUSTRY_PACK_CHARS,
    compose_earnings_method_pack,
    get_industry_pack,
    render_industry_pack_for_prompt,
)


def _snap_fixture(
    *,
    symbol: str = "BAC",
    sic: str = "6021",
    bank_subtype: str = "universal_commercial",
    cet1_pct: str = "10.90",
    cet1_min: str = "10.00",
    htm_pnl: str = "-103913000000",
    tce_rep: str = "201760000000",
    rwa_std: str = "1820000000000",
    prov_q: str = "1269000000",
    ppnr_q: str = "10236000000",
    rev_q: str = "29958000000",
    nii_q: str = "16449000000",
    exp_q: str = "19722000000",
    ni_comm_q: str = "6541000000",
    ni_comm_ttm: str = "26000000000",
    ppnr_ttm: str = "40000000000",
    tbvps_rep: str | None = "29.14",
    tbvps_std: str = "29.03",
    rotce_q: str | None = "12.60",
    shares_end: str = "6922700000",
    shares_rep: str = "6922700000",
    dep_cur: str = "2038758000000",
    dep_pri: str | None = "2050000000000",
    loans_gross: str = "1251260000000",
    filed_at: str = "2026-11-01",
) -> BankDisclosureSnapshot:
    gap_pct = None
    if tbvps_rep is not None:
        gap = ((Decimal(tbvps_std) - Decimal(tbvps_rep)) / Decimal(tbvps_rep)) * Decimal("100")
        gap_pct = f"{gap:.2f}"
    rsu = str(max(Decimal("0"), Decimal(shares_rep) - Decimal(shares_end)))
    return BankDisclosureSnapshot(
        cik="0000070858",
        symbol=symbol,
        company_name=f"{symbol} Corp",
        sic=sic,
        bank_subtype=bank_subtype,  # type: ignore[arg-type]
        period_end_date="2026-09-30",
        fiscal_period_label="FY2026-Q3",
        periodic_form="10-Q",
        periodic_accession="0000070858-26-000548",
        periodic_filed_at=filed_at,
        net_revenue_gaap_q_usd=rev_q,
        nii_gaap_q_usd=nii_q,
        noninterest_income_gaap_q_usd=str(Decimal(rev_q) - Decimal(nii_q)),
        noninterest_expense_q_usd=exp_q,
        ppnr_gaap_q_usd=ppnr_q,
        provision_q_usd=prov_q,
        net_income_q_usd=ni_comm_q,
        net_income_to_common_q_usd=ni_comm_q,
        ppnr_gaap_ttm_usd=ppnr_ttm,
        net_income_to_common_ttm_usd=ni_comm_ttm,
        total_assets_usd="3427704000000",
        total_deposits_usd=dep_cur,
        total_deposits_prior_q_usd=dep_pri,
        total_loans_gross_usd=loans_gross,
        allowance_credit_losses_usd="13005000000",
        cet1_standardized_pct=cet1_pct,
        cet1_standardized_source="10q_final",
        cet1_reg_min_pct=cet1_min,
        rwa_standardized_usd=rwa_std,
        htm_net_unrealized_pnl_usd=htm_pnl,
        rotce_q_reported_pct=rotce_q,
        tbvps_bridge=TbvpsReconciliationBridge(
            total_stockholders_equity_usd="296749000000",
            preferred_stock_usd="24996000000",
            common_equity_usd="271753000000",
            goodwill_usd="69021000000",
            tce_reported_usd=tce_rep,
            tce_xbrl_std_usd=tce_rep,
            shares_ending_common=shares_end,
            shares_reported_denominator=shares_rep,
            rsu_share_addon=rsu,
            tbvps_reported_usd=tbvps_rep,
            tbvps_reported_source="8k_supplement" if tbvps_rep else "unavailable",
            tbvps_xbrl_std_usd=tbvps_std,
            tbvps_gap_pct=gap_pct,
        ),
    )


def test_v4a3_d4_htm_tiered_escalation_bac_red_vs_wfc_yellow_and_subtypes() -> None:
    """BAC dual stress (HTM>=30% + CET1 buffer < 100bps) triggers RED; WFC single stress (buffer +153bps) stays YELLOW."""
    bac = evaluate_bank_diagnostics(_snap_fixture(
        symbol="BAC", cet1_pct="10.90", cet1_min="10.00", htm_pnl="-103913000000", tce_rep="201760000000", rwa_std="1820000000000",
    ))
    assert bac.bank_quality_light == "red"
    htm_flag_bac = next(f for f in bac.flags if f.id == "htm_securities_overhang")
    assert htm_flag_bac.severity == "red"
    assert Decimal(bac.htm_loss_pct_of_tce or "0") > Decimal("50.0")
    assert bac.cet1_buffer_bps == "90"
    # 24% tax adjustment: 103,913 * 0.76 / 1,820,000 = 4.34% (434 bps) -> 10.90 - 4.34 = 6.56%
    assert Decimal("6.50") <= Decimal(bac.pro_forma_cet1_ex_htm_pct or "0") <= Decimal("6.65")

    wfc = evaluate_bank_diagnostics(_snap_fixture(
        symbol="WFC", cet1_pct="10.03", cet1_min="8.50", htm_pnl="-43228000000", tce_rep="133447000000", rwa_std="1340000000000",
    ))
    assert wfc.bank_quality_light == "yellow"
    htm_flag_wfc = next(f for f in wfc.flags if f.id == "htm_securities_overhang")
    assert htm_flag_wfc.severity == "yellow"
    assert wfc.cet1_buffer_bps == "153"

    # WFC with deposit contraction <= -3.0% ALSO escalates to RED!
    wfc_run = evaluate_bank_diagnostics(_snap_fixture(
        symbol="WFC", cet1_pct="10.03", cet1_min="8.50", htm_pnl="-43228000000", tce_rep="133447000000",
        dep_cur="1920000000000", dep_pri="2000000000000",  # -4.0% QoQ
    ))
    assert wfc_run.bank_quality_light == "red"

    # Investment bank BHC (GS) w/ high non-commercial ratios: exempt from LDR & negative provision supported cleanly
    gs_diag = evaluate_bank_diagnostics(_snap_fixture(
        symbol="GS", sic="6211", bank_subtype="investment_bank_bhc",
        cet1_pct="12.40", cet1_min="11.40", htm_pnl="-1818000000", tce_rep="100927000000",
        prov_q="-200000000", ppnr_q="7371000000", exp_q="11308000000", rev_q="18679000000",
        tbvps_rep="341.43", tbvps_std="349.71", shares_end="288600000", shares_rep="295600000",
    ))
    assert gs_diag.metrics["ldr_pct"].status == "exempt_by_subtype"
    assert gs_diag.comparability_warnings and gs_diag.comparability_warnings[0]["code"] == "tbvps_denominator_rsu_adjustment"
    assert Decimal(gs_diag.provision_to_ppnr_pct or "0") < 0


def test_v4a3_gordon_ptbv_valuation_3x3_matrix_rsu_and_anti_future_guard() -> None:
    """Verify D3 Reported TBVPS primary, D5 Gordon 3x3 matrix (Ke=10%/g=3%), degenerate boundaries & anti-future."""
    gs = _snap_fixture(
        symbol="GS", sic="6211", bank_subtype="investment_bank_bhc",
        tbvps_rep="341.43", tbvps_std="349.71", rotce_q="20.50",
        shares_end="288600000", shares_rep="295600000",
    )
    # Anti-future guard rejects as_of earlier than filed_at
    with pytest.raises(ValueError, match="anti_future_guard"):
        evaluate_bank_diagnostics(gs, as_of=date(2026, 10, 1))
    with pytest.raises(ValueError, match="anti_future_guard"):
        evaluate_bank_valuation(gs, price_usd="682.86", as_of=date(2026, 10, 1))

    # Px = 682.86 -> P/TBV primary = 2.00x -> Implied ROTCE = 3.0 + 2.0*(10.0 - 3.0) = 17.00%; spread = +3.50pp
    val = evaluate_bank_valuation(gs, price_usd="682.86")
    assert val.primary_tbvps_basis == "reported_market_convention"
    assert val.p_to_tbv_primary == "2.00"
    assert val.p_to_tbv_xbrl_std == "1.95"
    assert val.implied_rotce_pct == "17.00"
    assert val.rotce_spread_vs_implied_pp == "3.50"
    assert val.justified_p_to_tbv_at_current_rotce == "2.50"  # (20.5 - 3) / (10 - 3)
    assert val.valuation_signal == "attractive_discount"
    assert len(val.sensitivity_matrix) == 9  # 3x3 matrix

    # Degenerate Ke <= g guard
    val_deg = evaluate_bank_valuation(gs, price_usd="682.86", coe_pct="3.0", terminal_g_pct="3.5")
    assert val_deg.implied_rotce_pct is None and val_deg.valuation_signal == "unavailable"
    assert any("Degenerate Gordon boundary" in n for n in val_deg.notes)

    # Unavailable Reported TBVPS falls back to XBRL-Standardized transparently
    no_rep = _snap_fixture(tbvps_rep=None, tbvps_std="50.00")
    val_fb = evaluate_bank_valuation(no_rep, price_usd="75.00")
    assert val_fb.primary_tbvps_basis == "xbrl_std_fallback"
    assert val_fb.p_to_tbv_primary == "1.50"


def test_v4a3_kpi_us_large_banks_pack_no_truncation_cross_domain_and_no_blk_poisoning() -> None:
    """Verify kpi_us_large_banks pack limits, cross-domain rejection & GS(6211)->BLK(6211) isolation."""
    pack = get_industry_pack("kpi_us_large_banks")
    assert pack is not None
    rendered_all = render_industry_pack_for_prompt(pack, available_capabilities={
        "sec_financials_available": True, "filings_available": True, "market_data_available": True,
    })
    rendered_empty = render_industry_pack_for_prompt(pack, available_capabilities={})
    assert len(rendered_all) <= MAX_INDUSTRY_PACK_CHARS and "...[truncated]" not in rendered_all
    assert len(rendered_empty) <= MAX_INDUSTRY_PACK_CHARS and "...[truncated]" not in rendered_empty

    # GS (6211 + active_sector_plugin="us_large_bank_v4a") gets bank pack & keeps industrial metrics gated
    gs_comp = compose_earnings_method_pack("6211", active_sector_plugin="us_large_bank_v4a")
    assert gs_comp.is_sector_gated is True
    assert gs_comp.industry_pack_id == "kpi_us_large_banks"
    assert gs_comp.selection_source == "sector_plugin"
    assert len(gs_comp.rendered_prompt) <= MAX_COMPOSED_PACK_CHARS
    assert "...[truncated]" not in gs_comp.rendered_prompt
    assert "[SECTOR GATE:" in gs_comp.rendered_prompt and "kpi_us_large_banks" in gs_comp.rendered_prompt

    # Immediate call for BLK (same SIC 6211, NO active_sector_plugin) MUST stay pure sector_gated with zero poisoning!
    blk_comp = compose_earnings_method_pack("6211", active_sector_plugin=None)
    assert blk_comp.is_sector_gated is True
    assert blk_comp.industry_pack is None
    assert blk_comp.selection_source == "sector_gated"

    # Cross-domain overrides must be rejected cleanly
    with pytest.raises(ValueError):
        compose_earnings_method_pack("6021", "kpi_saas_cloud", active_sector_plugin="us_large_bank_v4a")
    with pytest.raises(ValueError):
        compose_earnings_method_pack("7372", "kpi_us_large_banks")

    # SectorApplicability remains backward compatible while supporting new optional fields
    gate = evaluate_sec_sic_gate("6021")
    assert gate.is_gated is True and gate.bank_subtype is None and gate.active_sector_plugin is None
