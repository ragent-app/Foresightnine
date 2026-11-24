from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext

import pytest

from app.fin_data.models import StatementLine
from app.research_earnings_diagnostics import (
    DiagnosticsError,
    EarningsQualityRequest,
    GuidanceAndNonGaapRequest,
    GuidanceMetricInput,
    GuidanceRangeInput,
    MetricValue,
    NonGaapAdjustmentItem,
    NonGaapBridgeInput,
    PeriodFinancials,
    ValuationRequest,
    earnings_quality_diagnostics,
    evaluate_sec_sic_gate,
    guidance_and_nongaap_reconciler,
    period_financials_from_statement_lines,
    valuation_and_implied_expectations,
)


def _mv(val: str, fid: str, *, unit: str | None = "USD millions", currency: str | None = "USD", tag: str | None = None, src: str | None = None) -> MetricValue:
    return MetricValue(value=Decimal(val), fact_id=fid, unit=unit, currency=currency, xbrl_tag=tag, source_id=src)


def _healthy_period(
    label: str,
    *,
    rev: str,
    cogs: str,
    rd: str,
    sga: str,
    op_inc: str,
    int_exp: str,
    ni: str,
    da: str,
    sbc: str,
    ocf: str,
    capex: str,
    buybacks: str,
    divs: str,
    shares: str,
    cash: str,
    sti: str,
    ar: str,
    inv: str,
    ap: str,
    ca: str,
    cl: str,
    st_debt: str,
    lt_debt: str,
    period_type: str = "quarter",
    period_days: str | None = None,
) -> PeriodFinancials:
    return PeriodFinancials(
        period_label=label,
        period_type=period_type,
        period_days=Decimal(period_days) if period_days else None,
        currency="USD",
        monetary_unit="USD millions",
        revenue=_mv(rev, f"{label}:rev", tag="us-gaap:Revenues", src=f"src_{label}"),
        cost_of_revenue=_mv(cogs, f"{label}:cogs", tag="us-gaap:CostOfRevenue", src=f"src_{label}"),
        rd_expense=_mv(rd, f"{label}:rd", tag="us-gaap:ResearchAndDevelopmentExpense"),
        sga_expense=_mv(sga, f"{label}:sga", tag="us-gaap:SellingGeneralAndAdministrativeExpense"),
        operating_income=_mv(op_inc, f"{label}:opinc", tag="us-gaap:OperatingIncomeLoss"),
        interest_expense=_mv(int_exp, f"{label}:int", tag="us-gaap:InterestExpense"),
        net_income=_mv(ni, f"{label}:ni", tag="us-gaap:NetIncomeLoss"),
        depreciation_amortization=_mv(da, f"{label}:da", tag="us-gaap:DepreciationAndAmortization"),
        sbc=_mv(sbc, f"{label}:sbc", tag="us-gaap:ShareBasedCompensation"),
        operating_cash_flow=_mv(ocf, f"{label}:ocf", tag="us-gaap:NetCashProvidedByUsedInOperatingActivities"),
        capex=_mv(capex, f"{label}:capex", tag="us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"),
        share_repurchases=_mv(buybacks, f"{label}:buybacks"),
        dividends_paid=_mv(divs, f"{label}:divs"),
        shares_diluted=MetricValue(
            value=Decimal(shares),
            fact_id=f"{label}:shares",
            unit="millions shares",
            xbrl_tag="us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
        ),
        cash=_mv(cash, f"{label}:cash"),
        short_term_investments=_mv(sti, f"{label}:sti"),
        accounts_receivable=_mv(ar, f"{label}:ar"),
        inventory=_mv(inv, f"{label}:inv"),
        accounts_payable=_mv(ap, f"{label}:ap"),
        current_assets=_mv(ca, f"{label}:ca"),
        current_liabilities=_mv(cl, f"{label}:cl"),
        short_term_debt=_mv(st_debt, f"{label}:stdebt"),
        long_term_debt=_mv(lt_debt, f"{label}:ltdebt"),
    )


# ============================================================================
# Engine 1: Earnings Quality Diagnostics
# ============================================================================


def test_earnings_quality_diagnostics_exact_decimal_outputs_and_provenance():
    # Current 2025Q3: Rev=1000, COGS=400 (GM=60%), RD=100 (10%), SGA=150 (15%), OpInc=350 (35%), NI=280 (28%)
    # OCF=320, CapEx=40 -> FCF=280, SBC=28 -> FCF ex-SBC=252
    # Working capital (90-day explicit period for clean exact fractions):
    # AR=200 -> DSO=18.00, Inv=100 -> DIO=22.50, AP=80 -> DPO=18.00, CCC=22.50, InvTurn=(400*(365/90))/100=16.22
    cur = _healthy_period(
        "2025Q3",
        rev="1000", cogs="400", rd="100", sga="150", op_inc="350", int_exp="10", ni="280", da="50",
        sbc="28", ocf="320", capex="40", buybacks="100", divs="40", shares="98",
        cash="300", sti="200", ar="200", inv="100", ap="80", ca="900", cl="450", st_debt="50", lt_debt="250",
        period_days="90",
    )
    # Prior QoQ 2025Q2: Rev=950, COGS=399 -> GM=58%, RD=95 (10%), SGA=152 (16%), OpInc=304 (32%), NI=247 (26%)
    qoq = _healthy_period(
        "2025Q2",
        rev="950", cogs="399", rd="95", sga="152", op_inc="304", int_exp="10", ni="247", da="50",
        sbc="28", ocf="270", capex="40", buybacks="90", divs="40", shares="99",
        cash="250", sti="200", ar="190", inv="106.4", ap="79.8", ca="850", cl="440", st_debt="50", lt_debt="260",
        period_days="90",
    )
    # Prior YoY 2024Q3: Rev=800, COGS=360 -> GM=55%, RD=96 (12%), SGA=144 (18%), OpInc=200 (25%), NI=160 (20%)
    # AR=160 (+25% YoY == Rev +25% YoY -> 0pp), Inv=90 (+11.11% YoY vs COGS +11.11% -> 0pp), AP=72, DSO=18, DIO=22.5, DPO=18, CCC=22.5
    yoy = _healthy_period(
        "2024Q3",
        rev="800", cogs="360", rd="96", sga="144", op_inc="200", int_exp="12", ni="160", da="40",
        sbc="24", ocf="190", capex="35", buybacks="60", divs="32", shares="100",
        cash="200", sti="150", ar="160", inv="90", ap="72", ca="750", cl="420", st_debt="60", lt_debt="290",
        period_days="90",
    )
    # TTM EBITDA = 1500
    ttm = _healthy_period(
        "TTM_2025Q3",
        rev="3800", cogs="1600", rd="380", sga="600", op_inc="1220", int_exp="42", ni="960", da="280",
        sbc="110", ocf="1180", capex="150", buybacks="350", divs="150", shares="98.5",
        cash="300", sti="200", ar="200", inv="100", ap="80", ca="900", cl="450", st_debt="50", lt_debt="250",
        period_type="ttm",
    )

    res = earnings_quality_diagnostics(
        EarningsQualityRequest(
            entity_id="cik:0000123456",
            symbol="ACME",
            sic="3571",
            sic_description="Electronic Computers",
            current=cur,
            prior_qoq=qoq,
            prior_yoy=yoy,
            ttm=ttm,
        )
    )

    assert res.sector_applicability.sector_category == "non_financial"
    assert res.sector_applicability.is_gated is False
    assert "composite_score" not in res.model_dump()

    # Group A: Margin Bridge
    mb = {item.line: item for item in res.margin_bridge}
    assert mb["gross_margin"].current_pct.value == Decimal("60.00")
    assert mb["gross_margin"].qoq_change_bps.value == Decimal("200.00")  # 60% - 58% = +200 bps
    assert mb["gross_margin"].yoy_change_bps.value == Decimal("500.00")  # 60% - 55% = +500 bps
    assert mb["rd_pct"].current_pct.value == Decimal("10.00")
    assert mb["rd_pct"].qoq_change_bps.value == Decimal("0.00")
    assert mb["rd_pct"].yoy_change_bps.value == Decimal("-200.00")  # 10% - 12% = -200 bps
    assert mb["sga_pct"].current_pct.value == Decimal("15.00")
    assert mb["sga_pct"].qoq_change_bps.value == Decimal("-100.00")  # 15% - 16% = -100 bps
    assert mb["sga_pct"].yoy_change_bps.value == Decimal("-300.00")  # 15% - 18% = -300 bps
    assert mb["operating_margin"].current_pct.value == Decimal("35.00")
    assert mb["operating_margin"].qoq_change_bps.value == Decimal("300.00")
    assert mb["operating_margin"].yoy_change_bps.value == Decimal("1000.00")
    assert mb["net_margin"].current_pct.value == Decimal("28.00")
    assert mb["net_margin"].qoq_change_bps.value == Decimal("200.00")
    assert mb["net_margin"].yoy_change_bps.value == Decimal("800.00")

    # Provenance check on margin bridge
    assert "2025Q3:rev" in mb["gross_margin"].current_pct.input_ids
    assert "2025Q3:cogs" in mb["gross_margin"].current_pct.input_ids
    assert "us-gaap:Revenues" in mb["gross_margin"].current_pct.xbrl_tags
    assert "src_2025Q3" in mb["gross_margin"].current_pct.source_ids

    # Group B: Earnings Quality & True FCF
    eq = res.earnings_quality
    assert eq.reported_or_derived_fcf.value == Decimal("280.00")  # 320 - 40
    assert eq.ocf_to_ni.value == Decimal("1.14")  # 320 / 280 = 1.142857...
    assert eq.fcf_to_ni.value == Decimal("1.00")  # 280 / 280
    assert eq.fcf_margin_pct.value == Decimal("28.00")
    assert eq.sbc_to_revenue_pct.value == Decimal("2.80")
    assert eq.sbc_to_fcf_pct.value == Decimal("10.00")  # 28 / 280
    assert eq.fcf_ex_sbc.value == Decimal("252.00")  # 280 - 28
    assert eq.fcf_ex_sbc_margin_pct.value == Decimal("25.20")

    # Group C: Working Capital Forensics
    wc = res.working_capital
    assert wc.ar_yoy_growth_pct.value == Decimal("25.00")
    assert wc.revenue_yoy_growth_pct.value == Decimal("25.00")
    assert wc.ar_vs_revenue_growth_pp.value == Decimal("0.00")
    assert wc.inventory_yoy_growth_pct.value == Decimal("11.11")
    assert wc.cogs_yoy_growth_pct.value == Decimal("11.11")
    assert wc.inventory_vs_cogs_growth_pp.value == Decimal("0.00")
    assert wc.inventory_vs_revenue_growth_pp.value == Decimal("-13.89")
    assert wc.dso.current.value == Decimal("18.00")
    assert wc.dio.current.value == Decimal("22.50")
    assert wc.dpo.current.value == Decimal("18.00")
    assert wc.ccc.current.value == Decimal("22.50")
    assert wc.ccc.yoy_delta.value == Decimal("0.00")
    # QoQ DIO was 106.4 / 399 * 90 = 24.00 -> delta = 22.50 - 24.00 = -1.50 days
    assert wc.dio.prior_qoq.value == Decimal("24.00")
    assert wc.dio.qoq_delta.value == Decimal("-1.50")
    assert wc.inventory_turnover.current.value == Decimal("16.22")  # (400 * 365/90) / 100

    # Group D: Capital Allocation & Dilution
    ca = res.capital_allocation
    assert ca.capex_to_revenue_pct.value == Decimal("4.00")
    assert ca.capital_returned.value == Decimal("140.00")
    assert ca.payout_to_fcf_pct.value == Decimal("50.00")  # 140 / 280
    assert ca.diluted_shares_yoy_pct.value == Decimal("-2.00")  # 98 / 100 - 1

    # Group E: Balance Sheet & Leverage
    bs = res.balance_sheet
    assert bs.total_cash.value == Decimal("500.00")  # 300 + 200
    assert bs.total_debt.value == Decimal("300.00")  # 50 + 250
    assert bs.net_debt.value == Decimal("-200.00")
    assert bs.net_cash.value == Decimal("200.00")
    assert bs.position == "net_cash"
    # TTM EBITDA = 1220 + 280 = 1500; Net Debt / TTM EBITDA = -200 / 1500 = -0.13
    assert bs.net_debt_to_ttm_ebitda.value == Decimal("-0.13")
    assert bs.interest_coverage.value == Decimal("35.00")  # 350 / 10
    assert bs.current_ratio.value == Decimal("2.00")  # 900 / 450

    # Healthy company should have zero red/yellow flags and multiple green flags with MD&A prompts
    assert res.flag_counts["red"] == 0
    assert res.flag_counts["yellow"] == 0
    assert res.flag_counts["green"] > 0
    for flag in res.flags:
        assert len(flag.investigation_prompt) >= 10
        assert flag.input_ids

    # JSON serialization preserves Decimals as strings without binary float leakage
    dumped = res.model_dump(mode="json")
    assert dumped["earnings_quality"]["fcf_ex_sbc"]["value"] == "252.00"


def test_earnings_quality_ttm_ebitda_rollup_from_four_trailing_quarters():
    quarters = [
        PeriodFinancials(period_label=f"Q{i}", operating_income=Decimal("100"), depreciation_amortization=Decimal("25"))
        for i in range(1, 5)
    ]
    cur = PeriodFinancials(
        period_label="Q4",
        cash=Decimal("100"),
        total_debt=Decimal("600"),
        operating_income=Decimal("100"),
        depreciation_amortization=Decimal("25"),
    )
    res = earnings_quality_diagnostics(
        EarningsQualityRequest(current=cur, trailing_quarters=quarters)
    )
    # TTM EBITDA = 4 * (100 + 25) = 500; Net Debt = 600 - 100 = 500 -> 1.00x
    assert res.balance_sheet.net_debt_to_ttm_ebitda.status == "computed"
    assert res.balance_sheet.net_debt_to_ttm_ebitda.value == Decimal("1.00")


def test_sec_sic_gate_disables_inapplicable_metrics_for_banks_insurance_reits():
    cur = _healthy_period(
        "2025Q3",
        rev="1000", cogs="400", rd="50", sga="300", op_inc="250", int_exp="100", ni="200", da="20",
        sbc="25", ocf="100", capex="20", buybacks="50", divs="50", shares="102",
        cash="5000", sti="0", ar="1000", inv="0", ap="200", ca="6000", cl="7000", st_debt="1000", lt_debt="2000",
    )
    yoy = _healthy_period(
        "2024Q3",
        rev="900", cogs="350", rd="45", sga="280", op_inc="225", int_exp="90", ni="180", da="20",
        sbc="20", ocf="90", capex="18", buybacks="40", divs="45", shares="100",
        cash="4500", sti="0", ar="600", inv="0", ap="180", ca="5500", cl="6500", st_debt="900", lt_debt="1900",
    )

    # 1. Bank SIC 6021 (National Commercial Banks)
    bank_res = earnings_quality_diagnostics(
        EarningsQualityRequest(sic="6021", sic_description="National Commercial Banks", current=cur, prior_yoy=yoy)
    )
    assert bank_res.sector_applicability.sector_category == "bank"
    assert bank_res.sector_applicability.is_gated is True
    assert "Net Interest Margin (NIM)" in bank_res.sector_applicability.recommended_alternatives

    mb_bank = {item.line: item for item in bank_res.margin_bridge}
    assert mb_bank["gross_margin"].current_pct.status == "disabled_by_sector"
    assert mb_bank["gross_margin"].current_pct.value is None
    assert mb_bank["gross_margin"].yoy_change_bps.status == "disabled_by_sector"
    assert mb_bank["net_margin"].current_pct.status == "computed"  # Net margin remains available
    assert bank_res.working_capital.inventory_turnover.current.status == "disabled_by_sector"
    assert bank_res.working_capital.dso.current.status == "disabled_by_sector"
    assert bank_res.working_capital.ar_vs_revenue_growth_pp.status == "disabled_by_sector"
    assert bank_res.earnings_quality.reported_or_derived_fcf.status == "disabled_by_sector"
    assert bank_res.earnings_quality.fcf_ex_sbc.status == "disabled_by_sector"
    assert bank_res.balance_sheet.net_debt_to_ttm_ebitda.status == "disabled_by_sector"
    assert bank_res.balance_sheet.interest_coverage.status == "disabled_by_sector"
    assert bank_res.balance_sheet.current_ratio.status == "disabled_by_sector"
    # Disabled metrics must not generate false industrial red flags (like OCF/NI or current_ratio)
    flag_metrics = {f.metric_name for f in bank_res.flags}
    assert "ocf_to_ni" not in flag_metrics
    assert "current_ratio" not in flag_metrics
    assert "ar_vs_revenue_growth_pp" not in flag_metrics

    # 2. Insurance SIC 6331
    ins_gate = evaluate_sec_sic_gate(6331, "Fire, Marine & Casualty Insurance")
    assert ins_gate.sector_category == "insurance"
    assert ins_gate.is_gated is True
    assert "gross_margin" in ins_gate.disabled_metrics
    assert "standard_ev_ebitda" in ins_gate.disabled_metrics

    # 3. REIT SIC 6798
    reit_res = earnings_quality_diagnostics(
        EarningsQualityRequest(sic=6798, sic_description="Real Estate Investment Trusts", current=cur, prior_yoy=yoy)
    )
    assert reit_res.sector_applicability.sector_category == "reit"
    assert reit_res.sector_applicability.is_gated is True
    assert any("FFO" in alt for alt in reit_res.sector_applicability.recommended_alternatives)
    assert {item.line: item for item in reit_res.margin_bridge}["gross_margin"].current_pct.status == "disabled_by_sector"
    assert reit_res.working_capital.inventory_turnover.current.status == "disabled_by_sector"
    assert reit_res.earnings_quality.fcf_ex_sbc.status == "disabled_by_sector"
    assert reit_res.balance_sheet.net_debt_to_ttm_ebitda.status == "disabled_by_sector"

    with pytest.raises(DiagnosticsError):
        evaluate_sec_sic_gate("BAD_SIC")


def test_red_yellow_green_flag_triggers_and_investigation_prompts():
    # Construct a distressed/aggressive quarter triggering RED flags across groups:
    # OCF/NI = 60/100 = 0.60x (<0.80 Red)
    # FCF = 60 - 20 = 40; FCF/NI = 0.40x (<0.50 Red); SBC=45 -> SBC > FCF (FCF ex-SBC=-5 Red)
    # SBC/Rev = 45/200 = 22.5% (>15% Red)
    # Gross Margin: Cur 40% vs YoY 45% -> -500 bps (< -200 bps Red)
    # AR +40% vs Rev +10% -> +30 pp (> +15 pp Red)
    # Inv +35% vs COGS +10% -> +25 pp (> +15 pp Red)
    # Diluted shares +5% (> +3% Red)
    # Capital returned 60 / FCF 40 = 150% (> 120% Red)
    # Net debt 500 / TTM EBITDA 100 = 5.0x (> 3.5x Red)
    # Interest coverage: OpInc 20 / Int 15 = 1.33x (< 2.0x Red)
    # Current ratio: 180 / 200 = 0.90x (< 1.0x Red)
    cur_red = _healthy_period(
        "2025Q3",
        rev="220", cogs="132", rd="30", sga="38", op_inc="20", int_exp="15", ni="100", da="10",
        sbc="45", ocf="60", capex="20", buybacks="50", divs="10", shares="105",
        cash="100", sti="0", ar="140", inv="140", ap="40", ca="180", cl="200", st_debt="100", lt_debt="500",
        period_days="90",
    )
    yoy_red = _healthy_period(
        "2024Q3",
        rev="200", cogs="110", rd="25", sga="30", op_inc="35", int_exp="10", ni="80", da="10",
        sbc="20", ocf="90", capex="15", buybacks="10", divs="5", shares="100",
        cash="150", sti="0", ar="100", inv="100", ap="50", ca="300", cl="150", st_debt="50", lt_debt="200",
        period_days="90",
    )
    ttm_red = _healthy_period(
        "TTM",
        rev="850", cogs="500", rd="100", sga="150", op_inc="60", int_exp="50", ni="200", da="40",
        sbc="140", ocf="220", capex="80", buybacks="80", divs="20", shares="103",
        cash="100", sti="0", ar="140", inv="135", ap="40", ca="180", cl="200", st_debt="100", lt_debt="500",
        period_type="ttm",
    )
    red_res = earnings_quality_diagnostics(
        EarningsQualityRequest(sic="7372", current=cur_red, prior_yoy=yoy_red, ttm=ttm_red)
    )
    red_ids = {f.flag_id: f for f in red_res.flags if f.severity == "red"}
    assert "eq_ocf_to_ni" in red_ids
    assert "eq_fcf_to_ni" in red_ids
    assert "eq_sbc_exceeds_fcf" in red_ids
    assert "eq_sbc_to_revenue" in red_ids
    assert "mb_gross_margin_yoy" in red_ids
    assert "wc_ar_vs_revenue_growth" in red_ids
    assert "wc_inventory_divergence" in red_ids
    assert "wc_ccc_yoy_delta" in red_ids
    assert "ca_diluted_shares_yoy" in red_ids
    assert "ca_payout_to_fcf" in red_ids
    assert "bs_net_debt_to_ebitda" in red_ids
    assert "bs_interest_coverage" in red_ids
    assert "bs_current_ratio" in red_ids
    for flag in red_ids.values():
        assert any(kw in flag.investigation_prompt for kw in ("MD&A", "footnote", "Statement", "Footnotes", "ASC"))

    # Now test YELLOW bands:
    # OCF/NI = 85/100 = 0.85x (Yellow), FCF=70 -> FCF/NI=0.70x (Yellow), SBC=15 -> SBC/FCF=21.43% (Yellow)
    # SBC/Rev = 15/110 = 13.64% (Yellow)
    # GM: Cur 44% vs YoY 45% -> -100 bps (Yellow)
    # AR +20% vs Rev +10% -> +10 pp (Yellow), Inv +20% vs COGS +12% -> +8 pp (Yellow)
    # Shares +2% (Yellow), Payout 77 / 70 = 110% (Yellow)
    # Net debt 250 / TTM EBITDA 100 = 2.5x (Yellow), IntCov 30/10 = 3.0x (Yellow), Current ratio 110/100 = 1.10x (Yellow)
    cur_yellow = _healthy_period(
        "2025Q3",
        rev="110", cogs="61.6", rd="10", sga="8.4", op_inc="30", int_exp="10", ni="100", da="10",
        sbc="15", ocf="85", capex="15", buybacks="60", divs="17", shares="102",
        cash="50", sti="0", ar="120", inv="120", ap="45", ca="110", cl="100", st_debt="50", lt_debt="250",
        period_days="90",
    )
    yoy_yellow = _healthy_period(
        "2024Q3",
        rev="100", cogs="55.0", rd="10", sga="8.0", op_inc="27", int_exp="10", ni="80", da="10",
        sbc="10", ocf="85", capex="15", buybacks="20", divs="10", shares="100",
        cash="60", sti="0", ar="100", inv="100", ap="45", ca="140", cl="100", st_debt="50", lt_debt="200",
        period_days="90",
    )
    ttm_yellow = _healthy_period(
        "TTM",
        rev="420", cogs="235", rd="40", sga="35", op_inc="60", int_exp="35", ni="150", da="40",
        sbc="50", ocf="250", capex="60", buybacks="100", divs="40", shares="101",
        cash="50", sti="0", ar="120", inv="120", ap="45", ca="110", cl="100", st_debt="50", lt_debt="250",
        period_type="ttm",
    )
    yellow_res = earnings_quality_diagnostics(
        EarningsQualityRequest(current=cur_yellow, prior_yoy=yoy_yellow, ttm=ttm_yellow)
    )
    yellow_ids = {f.flag_id for f in yellow_res.flags if f.severity == "yellow"}
    assert {
        "eq_ocf_to_ni",
        "eq_fcf_to_ni",
        "eq_sbc_to_fcf",
        "eq_sbc_to_revenue",
        "mb_gross_margin_yoy",
        "wc_ar_vs_revenue_growth",
        "wc_inventory_divergence",
        "ca_diluted_shares_yoy",
        "ca_payout_to_fcf",
        "bs_net_debt_to_ebitda",
        "bs_interest_coverage",
        "bs_current_ratio",
    }.issubset(yellow_ids)


def test_scale_alignment_and_statement_line_adapter():
    # Mix USD millions and USD thousands across inputs and verify exact scale alignment
    cur = PeriodFinancials(
        period_label="2025Q3",
        currency="USD",
        revenue=_mv("1000", "rev", unit="USD millions"),
        cost_of_revenue=_mv("400000", "cogs", unit="USD thousands"),
        operating_cash_flow=_mv("300", "ocf", unit="USD millions"),
        capex=_mv("50000", "capex", unit="USD thousands"),
        sbc=_mv("25", "sbc", unit="USD millions"),
    )
    res = earnings_quality_diagnostics(EarningsQualityRequest(current=cur))
    assert res.monetary_unit == "USD thousands"
    assert res.earnings_quality.reported_or_derived_fcf.value == Decimal("250000.00")
    assert res.earnings_quality.fcf_ex_sbc.value == Decimal("225000.00")
    mb = {x.line: x for x in res.margin_bridge}
    assert mb["gross_margin"].current_pct.value == Decimal("60.00")

    # Cross-currency mismatch fails closed
    bad_cur = PeriodFinancials(
        period_label="2025Q3",
        revenue=_mv("1000", "rev", unit="USD millions", currency="USD"),
        net_income=_mv("100", "ni", unit="EUR millions", currency="EUR"),
    )
    with pytest.raises(DiagnosticsError):
        earnings_quality_diagnostics(EarningsQualityRequest(current=bad_cur))

    # Interop with SEC StatementLine objects
    d_end = date(2025, 9, 30)
    lines = [
        StatementLine(
            entity_id="cik:0000320193", statement="income", line="revenue",
            reported_tag="Revenues", taxonomy="us-gaap",
            period_start=date(2025, 7, 1), period_end=d_end, fiscal_label="2025-Q3",
            value=Decimal("500"), unit="USD", currency="USD", accession="0000320193-25-000100",
        ),
        StatementLine(
            entity_id="cik:0000320193", statement="income", line="gross_profit",
            reported_tag="GrossProfit", taxonomy="us-gaap",
            period_start=date(2025, 7, 1), period_end=d_end, fiscal_label="2025-Q3",
            value=Decimal("300"), unit="USD", currency="USD", accession="0000320193-25-000100",
        ),
        StatementLine(
            entity_id="cik:0000320193", statement="cashflow", line="operating_cash_flow",
            reported_tag="NetCashProvidedByUsedInOperatingActivities", taxonomy="us-gaap",
            period_start=date(2025, 7, 1), period_end=d_end, fiscal_label="2025-Q3",
            value=Decimal("150"), unit="USD", currency="USD", accession="0000320193-25-000100",
        ),
        StatementLine(
            entity_id="cik:0000320193", statement="cashflow", line="capex",
            reported_tag="PaymentsToAcquirePropertyPlantAndEquipment", taxonomy="us-gaap",
            period_start=date(2025, 7, 1), period_end=d_end, fiscal_label="2025-Q3",
            value=Decimal("30"), unit="USD", currency="USD", accession="0000320193-25-000100",
        ),
    ]
    pf = period_financials_from_statement_lines(lines, d_end, sbc=Decimal("15"))
    res_sl = earnings_quality_diagnostics(EarningsQualityRequest(current=pf))
    assert res_sl.earnings_quality.fcf_ex_sbc.value == Decimal("105.00")
    assert "cik:0000320193:operating_cash_flow:2025-09-30" in res_sl.earnings_quality.reported_or_derived_fcf.input_ids


# ============================================================================
# Engine 2: Guidance & Non-GAAP Reconciler
# ============================================================================


def test_guidance_beat_miss_and_new_guidance_directions():
    req = GuidanceAndNonGaapRequest(
        entity_id="cik:0001045810",
        symbol="NVDA",
        period_label="2025Q3",
        guidance_metrics=[
            # Revenue: actual $50.5B vs prior 8-K 99.1 guidance $48,000M - $50,000M (mid $49,000M) & consensus $49,500M
            # New Q4 guidance: $53B - $55B (mid $54B) vs Q4 consensus $52B -> Raise
            GuidanceMetricInput(
                metric_name="revenue",
                period_label="2025Q3",
                actual=_mv("50.5", "rev_act", unit="USD billions", src="8k_q3"),
                prior_guidance=GuidanceRangeInput(
                    low=_mv("48000", "rev_g_lo", unit="USD millions", src="8k_q2"),
                    high=_mv("50000", "rev_g_hi", unit="USD millions", src="8k_q2"),
                ),
                consensus=_mv("49500", "rev_cons", unit="USD millions", src="fmp_est"),
                new_guidance=GuidanceRangeInput(
                    low=_mv("53", "rev_ng_lo", unit="USD billions", src="8k_q3"),
                    high=_mv("55", "rev_ng_hi", unit="USD billions", src="8k_q3"),
                ),
                new_guidance_benchmark=_mv("52000", "rev_q4_cons", unit="USD millions", src="fmp_est"),
            ),
            # EPS: actual $1.90 vs prior guidance $1.95-$2.05 (mid $2.00) -> Miss (-5.00%) & below_low
            # vs consensus $1.85 -> Beat (+2.70%)
            # New guidance narrowed $2.05-$2.15 (mid $2.10) vs benchmark $2.10 -> Maintain
            GuidanceMetricInput(
                metric_name="non_gaap_eps",
                period_label="2025Q3",
                actual=_mv("1.90", "eps_act", unit="USD per share"),
                prior_guidance=GuidanceRangeInput(
                    low=_mv("1.95", "eps_g_lo", unit="USD per share"),
                    high=_mv("2.05", "eps_g_hi", unit="USD per share"),
                ),
                consensus=_mv("1.85", "eps_cons", unit="USD per share"),
                new_guidance=GuidanceRangeInput(
                    low=_mv("2.05", "eps_ng_lo", unit="USD per share"),
                    high=_mv("2.15", "eps_ng_hi", unit="USD per share"),
                ),
                new_guidance_benchmark=GuidanceRangeInput(
                    low=_mv("2.00", "eps_bg_lo", unit="USD per share"),
                    high=_mv("2.20", "eps_bg_hi", unit="USD per share"),
                ),
            ),
            # Gross margin: new guidance cut (71.0%-72.0% mid 71.5% vs benchmark 73.0% -> Cut)
            GuidanceMetricInput(
                metric_name="gross_margin",
                period_label="2025Q3",
                actual=_mv("73.5", "gm_act", unit="percent", currency=None),
                prior_guidance=GuidanceRangeInput(
                    mid=_mv("73.5", "gm_mid", unit="percent", currency=None),
                ),
                new_guidance=GuidanceRangeInput(
                    low=_mv("71.0", "gm_ng_lo", unit="percent", currency=None),
                    high=_mv("72.0", "gm_ng_hi", unit="percent", currency=None),
                ),
                new_guidance_benchmark=_mv("73.0", "gm_bench", unit="percent", currency=None),
            ),
            # No new guidance provided
            GuidanceMetricInput(
                metric_name="operating_expenses",
                period_label="2025Q3",
                actual=_mv("4000", "opex_act", unit="USD millions"),
                prior_guidance=GuidanceRangeInput(mid=_mv("4100", "opex_mid", unit="USD millions")),
            ),
        ],
    )
    res = guidance_and_nongaap_reconciler(req)
    evals = {e.metric_name: e for e in res.guidance_evaluations}

    # Revenue (aligned to finest common scale: USD millions)
    rev = evals["revenue"]
    assert rev.vs_prior_guidance.outcome == "Beat"
    assert rev.vs_prior_guidance.range_position == "above_high"
    assert rev.vs_prior_guidance.benchmark_value == Decimal("49000.00")
    assert rev.vs_prior_guidance.abs_diff == Decimal("1500.00")
    assert rev.vs_prior_guidance.pct_diff == Decimal("3.06")  # 1500 / 49000 * 100
    assert rev.vs_consensus.outcome == "Beat"
    assert rev.vs_consensus.abs_diff == Decimal("1000.00")
    assert rev.vs_consensus.pct_diff == Decimal("2.02")  # 1000 / 49500 * 100
    assert rev.new_guidance_direction.direction == "Raise"
    assert rev.new_guidance_direction.abs_change == Decimal("2000.00")
    assert rev.new_guidance_direction.pct_change == Decimal("3.85")

    # EPS
    eps = evals["non_gaap_eps"]
    assert eps.vs_prior_guidance.outcome == "Miss"
    assert eps.vs_prior_guidance.range_position == "below_low"
    assert eps.vs_prior_guidance.abs_diff == Decimal("-0.10")
    assert eps.vs_prior_guidance.pct_diff == Decimal("-5.00")
    assert eps.vs_consensus.outcome == "Beat"
    assert eps.vs_consensus.abs_diff == Decimal("0.05")
    assert eps.vs_consensus.pct_diff == Decimal("2.70")
    assert eps.new_guidance_direction.direction == "Maintain"
    assert eps.new_guidance_direction.abs_change == Decimal("0.00")

    # Gross margin
    gm = evals["gross_margin"]
    assert gm.vs_prior_guidance.outcome == "Inline"
    assert gm.vs_prior_guidance.range_position == "midpoint_only"
    assert gm.vs_consensus.status == "unavailable"
    assert gm.vs_consensus.outcome == "Unavailable"
    assert gm.new_guidance_direction.direction == "Cut"
    assert gm.new_guidance_direction.abs_change == Decimal("-1.50")

    # Operating expenses (no new guidance)
    assert evals["operating_expenses"].new_guidance_direction.direction == "Not Provided"


def test_nongaap_bridge_tie_mismatch_uplift_and_recurring_warnings():
    req = GuidanceAndNonGaapRequest(
        symbol="SNOW",
        period_label="2025Q3",
        nongaap_bridges=[
            # 1. Exact tie, negative GAAP -> positive Non-GAAP, recurring SBC + recurring restructuring
            NonGaapBridgeInput(
                metric_name="Net Income",
                period_label="2025Q3",
                gaap_value=_mv("-200", "ni_gaap", unit="USD millions"),
                reported_non_gaap_value=_mv("100", "ni_ng", unit="USD millions"),
                adjustments=[
                    NonGaapAdjustmentItem(label="Stock-based compensation", amount=_mv("250", "adj_sbc", unit="USD millions")),
                    NonGaapAdjustmentItem(
                        label="Restructuring and severance charges",
                        amount=_mv("70", "adj_restruct", unit="USD millions"),
                        prior_periods_present=3,
                    ),
                    NonGaapAdjustmentItem(
                        label="Income tax effect of adjustments",
                        amount=_mv("-20", "adj_tax", unit="USD millions"),
                    ),
                ],
            ),
            # 2. Arithmetic mismatch (GAAP 100 + 25 + 15 = 140 != reported 150 -> discrepancy +10)
            # Plus genuine one-time goodwill impairment (not recurring)
            NonGaapBridgeInput(
                metric_name="Operating Income",
                period_label="2025Q3",
                gaap_value=_mv("100", "oi_gaap", unit="USD millions"),
                reported_non_gaap_value=_mv("150", "oi_ng", unit="USD millions"),
                adjustments=[
                    NonGaapAdjustmentItem(label="Amortization of acquired intangibles", amount=_mv("25", "adj_amort", unit="USD millions")),
                    NonGaapAdjustmentItem(
                        label="Goodwill impairment write-down",
                        amount=_mv("15", "adj_imp", unit="USD millions"),
                        prior_periods_present=0,
                    ),
                ],
            ),
            # 3. Within rounding tolerance tie (EPS rounding $0.01)
            NonGaapBridgeInput(
                metric_name="Diluted EPS",
                period_label="2025Q3",
                gaap_value=_mv("1.00", "eps_g", unit="USD per share"),
                reported_non_gaap_value=_mv("1.26", "eps_ng", unit="USD per share"),
                adjustments=[
                    NonGaapAdjustmentItem(label="Stock-based compensation", amount=_mv("0.25", "eps_sbc", unit="USD per share")),
                ],
                rounding_tolerance=Decimal("0.01"),
            ),
        ],
    )
    res = guidance_and_nongaap_reconciler(req)
    bridges = {b.metric_name: b for b in res.nongaap_verifications}

    # Bridge 1: Exact tie, loss-to-profit conversion, uplift = (100 - (-200)) / |-200| = 1.50 (150%)
    b1 = bridges["Net Income"]
    assert b1.ties is True
    assert b1.tie_status == "exact_tie"
    assert b1.adjustments_sum == Decimal("300.00")
    assert b1.computed_non_gaap_value == Decimal("100.00")
    assert b1.discrepancy == Decimal("0.00")
    assert b1.uplift_ratio.value == Decimal("1.50")
    assert b1.uplift_pct.value == Decimal("150.00")
    b1_warn_ids = {w.flag_id for w in b1.warnings}
    assert "nongaap_loss_to_profit_conversion" in b1_warn_ids
    assert "nongaap_recurring_adjustment_sbc" in b1_warn_ids
    assert "nongaap_recurring_adjustment_restructuring" in b1_warn_ids
    # Tax effect should not be flagged as recurring
    assert "nongaap_recurring_adjustment_tax_effect" not in b1_warn_ids

    # Bridge 2: Mismatch flagged!
    b2 = bridges["Operating Income"]
    assert b2.ties is False
    assert b2.tie_status == "mismatch"
    assert b2.computed_non_gaap_value == Decimal("140.00")
    assert b2.discrepancy == Decimal("10.00")
    assert b2.uplift_ratio.value == Decimal("0.50")
    b2_warn_ids = {w.flag_id for w in b2.warnings}
    assert "nongaap_bridge_arithmetic_mismatch" in b2_warn_ids
    assert "nongaap_recurring_adjustment_amortization_intangibles" in b2_warn_ids
    # One-time impairment with prior_periods_present=0 should NOT be flagged recurring
    assert "nongaap_recurring_adjustment_impairment" not in b2_warn_ids

    # Bridge 3: Ties within rounding tolerance
    b3 = bridges["Diluted EPS"]
    assert b3.ties is True
    assert b3.tie_status == "ties_within_rounding_tolerance"
    assert b3.discrepancy == Decimal("0.01")


# ============================================================================
# Engine 3: Valuation & Reverse-DCF
# ============================================================================


def _forward_dcf_ev(fcf0: Decimal, cagr: Decimal, years: int, wacc: Decimal, g: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 100
        pv = Decimal(0)
        for t in range(1, years + 1):
            pv += (fcf0 * ((Decimal(1) + cagr) ** t)) / ((Decimal(1) + wacc) ** t)
        fcf_n = fcf0 * ((Decimal(1) + cagr) ** years)
        tv = fcf_n * (Decimal(1) + g) / (wacc - g)
        pv += tv / ((Decimal(1) + wacc) ** years)
        return pv


def test_valuation_multiples_and_reverse_dcf_exact_numerical_accuracy():
    # Plant exact 12.50% 5-year FCF CAGR at WACC=9%, g=3%, FCF0 = 1000
    planted_cagr = Decimal("0.125")
    fcf0 = Decimal("1000")
    wacc = Decimal("0.09")
    g = Decimal("0.03")
    exact_ev = _forward_dcf_ev(fcf0, planted_cagr, 5, wacc, g)

    # Set cash = 5000, debt = 2000, minority_interest = 500, preferred = 0 -> Net Debt = -2500
    # So Market Cap = exact_ev + 2500; with 100M shares, close_price = (exact_ev + 2500) / 100
    shares_m = Decimal("100")
    with localcontext() as ctx:
        ctx.prec = 100
        mcap = exact_ev + Decimal("2500")
        close_px = (mcap / shares_m).quantize(Decimal("0.000001"))

    req = ValuationRequest(
        entity_id="cik:0000123456",
        symbol="TECH",
        sic="7372",
        currency="USD",
        monetary_unit="USD millions",
        close_price=MetricValue(value=close_px, fact_id="px_latest", unit="USD per share", currency="USD"),
        diluted_shares=MetricValue(value=Decimal("100000000"), fact_id="sh_q3", unit="shares"),  # 100M raw shares!
        cash=_mv("4000", "cash"),
        short_term_investments=_mv("1000", "sti"),
        total_debt=_mv("2000", "debt"),
        minority_interest=_mv("500", "mi"),
        ttm_revenue=_mv("5000", "rev"),
        ttm_ebit=_mv("1200", "ebit"),
        ttm_depreciation_amortization=_mv("300", "da"),  # TTM EBITDA = 1500
        ttm_net_income=_mv("900", "ni"),
        ttm_fcf=_mv("1000", "fcf"),
        ttm_sbc=_mv("200", "sbc"),  # FCF ex-SBC = 800
        ntm_eps=_mv("11.00", "ntm_eps", unit="USD per share"),
        ntm_revenue=_mv("6000", "ntm_rev"),
    )
    res = valuation_and_implied_expectations(req)

    # Check cross-unit Market Cap alignment: 100M shares (unit="shares") * close_px -> USD millions!
    assert res.monetary_unit == "USD millions"
    assert res.market_cap.status == "computed"
    with localcontext() as ctx:
        ctx.prec = 100
        expected_mcap_q = mcap.quantize(Decimal("0.01"))
        expected_ev_q = exact_ev.quantize(Decimal("0.01"))
    assert abs(res.market_cap.value - expected_mcap_q) <= Decimal("0.01")
    assert abs(res.enterprise_value.value - expected_ev_q) <= Decimal("0.01")

    # Verify multiples
    assert res.ev_to_sales.value == (exact_ev / Decimal("5000")).quantize(Decimal("0.01"))
    assert res.ev_to_ebitda.value == (exact_ev / Decimal("1500")).quantize(Decimal("0.01"))
    assert res.ev_to_ebit.value == (exact_ev / Decimal("1200")).quantize(Decimal("0.01"))
    assert res.p_to_fcf.value == (mcap / Decimal("1000")).quantize(Decimal("0.01"))
    assert res.fcf_ex_sbc.value == Decimal("800.00")
    assert res.ev_to_fcf_ex_sbc.value == (exact_ev / Decimal("800")).quantize(Decimal("0.01"))
    assert res.pe_ttm.value == (mcap / Decimal("900")).quantize(Decimal("0.01"))
    assert res.pe_forward.value == (close_px / Decimal("11.00")).quantize(Decimal("0.01"))
    assert res.fcf_yield_pct.value == ((Decimal("1000") / mcap) * 100).quantize(Decimal("0.01"))
    assert res.fcf_ex_sbc_yield_pct.value == ((Decimal("800") / mcap) * 100).quantize(Decimal("0.01"))

    # Verify Reverse-DCF bisection recovered EXACT 12.50% CAGR
    rdcf = res.reverse_dcf
    assert rdcf.status == "computed"
    assert rdcf.implied_5y_fcf_cagr_pct == Decimal("12.50")
    assert rdcf.ev_residual_pct is not None and rdcf.ev_residual_pct < Decimal("0.0000001")
    assert rdcf.implied_exit_fcf_multiple == Decimal("17.17")  # 1.03 / 0.06 = 17.1666...
    assert rdcf.pv_explicit_fcf + rdcf.pv_terminal_value - rdcf.target_ev in (Decimal("0.00"), Decimal("0.01"), Decimal("-0.01"))

    # Verify 3x3 WACC x terminal-growth sensitivity matrix
    assert rdcf.wacc_axis_pct == [Decimal("8.00"), Decimal("9.00"), Decimal("10.00")]
    assert rdcf.terminal_growth_axis_pct == [Decimal("2.50"), Decimal("3.00"), Decimal("3.50")]
    assert len(rdcf.sensitivity_matrix) == 3
    assert all(len(row) == 3 for row in rdcf.sensitivity_matrix)
    # Center cell (9.00% WACC, 3.00% g) equals base 12.50%
    assert rdcf.sensitivity_matrix[1][1].implied_5y_fcf_cagr_pct == Decimal("12.50")
    # Monotonicity: higher WACC requires higher FCF CAGR; higher terminal growth lowers required FCF CAGR
    assert rdcf.sensitivity_matrix[0][1].implied_5y_fcf_cagr_pct < rdcf.sensitivity_matrix[1][1].implied_5y_fcf_cagr_pct
    assert rdcf.sensitivity_matrix[2][1].implied_5y_fcf_cagr_pct > rdcf.sensitivity_matrix[1][1].implied_5y_fcf_cagr_pct
    assert rdcf.sensitivity_matrix[1][0].implied_5y_fcf_cagr_pct > rdcf.sensitivity_matrix[1][1].implied_5y_fcf_cagr_pct
    assert rdcf.sensitivity_matrix[1][2].implied_5y_fcf_cagr_pct < rdcf.sensitivity_matrix[1][1].implied_5y_fcf_cagr_pct


def test_reverse_dcf_negative_cagr_and_exit_multiple_mode():
    # Test negative implied FCF CAGR (-5.50%) when market prices in FCF contraction
    fcf0 = Decimal("100")
    neg_cagr = Decimal("-0.055")
    ev_neg = _forward_dcf_ev(fcf0, neg_cagr, 5, Decimal("0.09"), Decimal("0.03"))
    res_neg = valuation_and_implied_expectations(
        ValuationRequest(
            close_price=ev_neg / Decimal("10"),
            diluted_shares=Decimal("10"),
            cash=Decimal("0"),
            total_debt=Decimal("0"),
            ttm_fcf=fcf0,
        )
    )
    assert res_neg.reverse_dcf.status == "computed"
    assert res_neg.reverse_dcf.implied_5y_fcf_cagr_pct == Decimal("-5.50")

    # Test exit_multiple mode (planted 10% CAGR with 15x exit multiple at 9% WACC)
    with localcontext() as ctx:
        ctx.prec = 100
        c = Decimal("0.10")
        r = Decimal("0.09")
        pv_exp = sum(fcf0 * ((Decimal(1) + c) ** t) / ((Decimal(1) + r) ** t) for t in range(1, 6))
        fcf5 = fcf0 * ((Decimal(1) + c) ** 5)
        ev_exit = pv_exp + (fcf5 * Decimal("15")) / ((Decimal(1) + r) ** 5)

    res_exit = valuation_and_implied_expectations(
        ValuationRequest(
            close_price=ev_exit / Decimal("10"),
            diluted_shares=Decimal("10"),
            cash=Decimal("0"),
            total_debt=Decimal("0"),
            ttm_fcf=fcf0,
            terminal_method="exit_multiple",
            terminal_multiple=Decimal("15"),
        )
    )
    assert res_exit.reverse_dcf.status == "computed"
    assert res_exit.reverse_dcf.implied_5y_fcf_cagr_pct == Decimal("10.00")
    assert res_exit.reverse_dcf.implied_exit_fcf_multiple == Decimal("15.00")


def test_valuation_degrades_gracefully_on_negative_fcf_missing_price_and_sic_gate():
    # 1. Negative TTM FCF -> Reverse-DCF unavailable, P/FCF unavailable, FCF Yield negative
    neg_fcf = valuation_and_implied_expectations(
        ValuationRequest(
            close_price=Decimal("50"),
            diluted_shares=Decimal("100"),
            cash=Decimal("500"),
            total_debt=Decimal("1000"),
            ttm_revenue=Decimal("2000"),
            ttm_fcf=Decimal("-150"),
        )
    )
    assert neg_fcf.market_cap.value == Decimal("5000.00")
    assert neg_fcf.enterprise_value.value == Decimal("5500.00")
    assert neg_fcf.ev_to_sales.value == Decimal("2.75")
    assert neg_fcf.fcf_yield_pct.value == Decimal("-3.00")
    assert neg_fcf.p_to_fcf.status == "unavailable"
    assert neg_fcf.reverse_dcf.status == "unavailable"
    assert "TTM FCF <= 0" in (neg_fcf.reverse_dcf.reason or "")

    # 2. Zero TTM FCF -> Reverse-DCF unavailable
    zero_fcf = valuation_and_implied_expectations(
        ValuationRequest(
            close_price=Decimal("50"),
            diluted_shares=Decimal("100"),
            cash=Decimal("500"),
            total_debt=Decimal("1000"),
            ttm_fcf=Decimal("0"),
        )
    )
    assert zero_fcf.reverse_dcf.status == "unavailable"
    assert "TTM FCF <= 0" in (zero_fcf.reverse_dcf.reason or "")

    # 3. Missing market price / EV -> graceful unavailable
    no_px = valuation_and_implied_expectations(
        ValuationRequest(
            diluted_shares=Decimal("100"),
            cash=Decimal("500"),
            total_debt=Decimal("1000"),
            ttm_fcf=Decimal("200"),
        )
    )
    assert no_px.market_cap.status == "unavailable"
    assert no_px.enterprise_value.status == "unavailable"
    assert no_px.reverse_dcf.status == "unavailable"

    # 4. Negative EV (net cash > market cap) -> Reverse-DCF unavailable
    neg_ev = valuation_and_implied_expectations(
        ValuationRequest(
            close_price=Decimal("10"),
            diluted_shares=Decimal("100"),
            cash=Decimal("2000"),
            total_debt=Decimal("100"),
            ttm_fcf=Decimal("50"),
        )
    )
    assert neg_ev.enterprise_value.value == Decimal("-900.00")
    assert neg_ev.reverse_dcf.status == "unavailable"
    assert "Enterprise Value <= 0" in (neg_ev.reverse_dcf.reason or "")

    # 5. Bank & REIT SIC gate disables EV/EBITDA, standard FCF multiples, and Reverse-DCF
    reit_val = valuation_and_implied_expectations(
        ValuationRequest(
            sic="6798",
            close_price=Decimal("80"),
            diluted_shares=Decimal("100"),
            cash=Decimal("200"),
            total_debt=Decimal("3000"),
            ttm_net_income=Decimal("400"),
            ttm_ebitda=Decimal("800"),
            ttm_fcf=Decimal("500"),
        )
    )
    assert reit_val.sector_applicability.sector_category == "reit"
    assert reit_val.market_cap.status == "computed"
    assert reit_val.pe_ttm.status == "computed"
    assert reit_val.ev_to_ebitda.status == "disabled_by_sector"
    assert reit_val.p_to_fcf.status == "disabled_by_sector"
    assert reit_val.reverse_dcf.status == "disabled_by_sector"


def test_negative_ocf_zero_gaap_and_input_validation_boundaries():
    # OCF <= 0 and FCF <= 0 with positive Net Income trigger specific red flags
    cur = PeriodFinancials(
        period_label="2025Q3",
        revenue=Decimal("500"),
        net_income=Decimal("50"),
        operating_cash_flow=Decimal("-20"),
        capex=Decimal("10"),
    )
    res = earnings_quality_diagnostics(EarningsQualityRequest(current=cur))
    red_ids = {f.flag_id for f in res.flags if f.severity == "red"}
    assert "eq_ocf_negative_with_positive_ni" in red_ids
    assert "eq_fcf_nonpositive_with_positive_ni" in red_ids

    # Zero GAAP value in Non-GAAP bridge degrades uplift cleanly
    zero_gaap = guidance_and_nongaap_reconciler(
        GuidanceAndNonGaapRequest(
            period_label="2025Q3",
            nongaap_bridges=[
                NonGaapBridgeInput(
                    metric_name="Net Income",
                    period_label="2025Q3",
                    gaap_value=Decimal("0"),
                    reported_non_gaap_value=Decimal("25"),
                    adjustments=[
                        NonGaapAdjustmentItem(label="Stock-based compensation", amount=Decimal("15"), is_recurring=False),
                        NonGaapAdjustmentItem(label="Custom adjustment", amount=Decimal("10"), is_recurring=True),
                    ],
                )
            ],
        )
    )
    bridge = zero_gaap.nongaap_verifications[0]
    assert bridge.ties is True
    assert bridge.uplift_ratio.status == "unavailable"
    assert [c.recurring_flag for c in bridge.classified_adjustments] == [False, True]

    # GuidanceRangeInput rejects low > high and mid outside [low, high]
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GuidanceRangeInput(low=Decimal("110"), high=Decimal("100"))
    with pytest.raises(ValidationError):
        GuidanceRangeInput(low=Decimal("100"), mid=Decimal("120"), high=Decimal("110"))

    # Cross-family mismatch in guidance_and_nongaap_reconciler fails closed
    with pytest.raises(DiagnosticsError):
        guidance_and_nongaap_reconciler(
            GuidanceAndNonGaapRequest(
                period_label="2025Q3",
                guidance_metrics=[
                    GuidanceMetricInput(
                        metric_name="revenue",
                        actual=_mv("100", "a", unit="USD millions"),
                        consensus=MetricValue(value=Decimal("10"), fact_id="c", unit="percent"),
                    )
                ],
            )
        )

    # MetricValue rejects bool and NaN
    with pytest.raises(ValidationError):
        MetricValue(value=True)
    with pytest.raises(ValidationError):
        MetricValue(value=Decimal("NaN"))


def test_valuation_sensitivity_wacc_lte_growth_and_bank_sic_gate():
    # Bank SIC 6021 disables EV, EV/EBITDA, FCF multiples, and Reverse-DCF while keeping Market Cap & P/E
    bank_val = valuation_and_implied_expectations(
        ValuationRequest(
            sic="6021",
            close_price=Decimal("120"),
            diluted_shares=Decimal("50"),
            cash=Decimal("2000"),
            total_debt=Decimal("5000"),
            ttm_net_income=Decimal("600"),
            ttm_ebitda=Decimal("1000"),
            ttm_fcf=Decimal("500"),
        )
    )
    assert bank_val.sector_applicability.sector_category == "bank"
    assert bank_val.market_cap.value == Decimal("6000.00")
    assert bank_val.pe_ttm.value == Decimal("10.00")
    assert bank_val.enterprise_value.status == "disabled_by_sector"
    assert bank_val.ev_to_ebitda.status == "disabled_by_sector"
    assert bank_val.p_to_fcf.status == "disabled_by_sector"
    assert bank_val.reverse_dcf.status == "disabled_by_sector"

    # Low WACC with wide step creates WACC <= terminal_growth in upper-right cell of 3x3 sensitivity matrix
    low_wacc = valuation_and_implied_expectations(
        ValuationRequest(
            close_price=Decimal("100"),
            diluted_shares=Decimal("20"),
            cash=Decimal("0"),
            total_debt=Decimal("0"),
            ttm_fcf=Decimal("100"),
            wacc=Decimal("0.05"),
            terminal_growth=Decimal("0.03"),
            wacc_step=Decimal("0.02"),
            terminal_growth_step=Decimal("0.01"),
        )
    )
    rdcf = low_wacc.reverse_dcf
    assert rdcf.status == "computed"
    # Row 0 is WACC=3.00%: g=2.00% works, g=3.00% and g=4.00% must degrade gracefully
    assert rdcf.sensitivity_matrix[0][0].status == "computed"
    assert rdcf.sensitivity_matrix[0][1].status == "unavailable"
    assert rdcf.sensitivity_matrix[0][2].status == "unavailable"
    assert rdcf.sensitivity_matrix[1][1].status == "computed"
    assert rdcf.sensitivity_matrix[2][2].status == "computed"
