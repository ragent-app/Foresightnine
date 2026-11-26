"""Offline unit & edge-case tests for V2 Peer Alignment & Relative Valuation Engine."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from types import SimpleNamespace
from typing import Any

from app.research_earnings_diagnostics import evaluate_sec_sic_gate
from app.research_peer_compare import (
    FpiAdrMeta,
    PeerCompanySnapshot,
    assemble_peer_matrix,
    compute_company_metrics_from_snapshot,
    generate_peer_attribution_brief,
    load_peer_groups_config,
    project_companyfacts_to_snapshot,
    suggest_peers_for_symbol,
)


def _q_rows(
    values_by_end: list[tuple[str, str, int | float, str]],
    *,
    form: str = "10-Q",
) -> list[dict[str, Any]]:
    """Build quarterly duration rows [(start, end, val, filed), ...]."""
    out = []
    for idx, (st, en, val, filed) in enumerate(values_by_end):
        out.append(
            {
                "start": st,
                "end": en,
                "val": val,
                "filed": filed,
                "form": form,
                "accn": f"0000000000-26-{idx:06d}",
                "fy": int(en[:4]),
                "fp": f"Q{(idx % 4) + 1}",
            }
        )
    return out


def _inst_rows(values_by_end: list[tuple[str, int | float, str]], *, form: str = "10-Q") -> list[dict[str, Any]]:
    return [
        {"end": en, "val": val, "filed": filed, "form": form, "accn": f"0000000000-26-{i:06d}"}
        for i, (en, val, filed) in enumerate(values_by_end)
    ]


def _make_us_companyfacts(
    *,
    rev_q: list[tuple[str, str, float, str]],
    gp_q: list[tuple[str, str, float, str]] | None = None,
    oi_q: list[tuple[str, str, float, str]] | None = None,
    ni_q: list[tuple[str, str, float, str]] | None = None,
    tax_q: list[tuple[str, str, float, str]] | None = None,
    pt_q: list[tuple[str, str, float, str]] | None = None,
    ocf_q: list[tuple[str, str, float, str]] | None = None,
    capex_q: list[tuple[str, str, float, str]] | None = None,
    sbc_q: list[tuple[str, str, float, str]] | None = None,
    cash: float | None = 20_000_000_000,
    lt_debt: float | None = 10_000_000_000,
    assets: float | None = 100_000_000_000,
    liabilities: float | None = 40_000_000_000,
    equity: float | None = 60_000_000_000,
    shares_diluted: float | None = 1_000_000_000,
    shares_basic: float | None = 980_000_000,
    dei_shares: float | None = None,
    bs_end: str = "2026-06-30",
    bs_filed: str = "2026-07-30",
) -> dict[str, Any]:
    gaap: dict[str, Any] = {
        "Revenues": {"units": {"USD": _q_rows(rev_q)}},
    }
    if gp_q is not None:
        gaap["GrossProfit"] = {"units": {"USD": _q_rows(gp_q)}}
    if oi_q is not None:
        gaap["OperatingIncomeLoss"] = {"units": {"USD": _q_rows(oi_q)}}
    if ni_q is not None:
        gaap["NetIncomeLoss"] = {"units": {"USD": _q_rows(ni_q)}}
    if tax_q is not None:
        gaap["IncomeTaxExpenseBenefit"] = {"units": {"USD": _q_rows(tax_q)}}
    if pt_q is not None:
        gaap["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"] = {
            "units": {"USD": _q_rows(pt_q)}
        }
    if ocf_q is not None:
        gaap["NetCashProvidedByUsedInOperatingActivities"] = {"units": {"USD": _q_rows(ocf_q)}}
    if capex_q is not None:
        gaap["PaymentsToAcquirePropertyPlantAndEquipment"] = {"units": {"USD": _q_rows(capex_q)}}
    if sbc_q is not None:
        gaap["ShareBasedCompensation"] = {"units": {"USD": _q_rows(sbc_q)}}

    if cash is not None:
        gaap["CashAndCashEquivalentsAtCarryingValue"] = {"units": {"USD": _inst_rows([(bs_end, cash, bs_filed)])}}
    if lt_debt is not None:
        gaap["LongTermDebtNoncurrent"] = {"units": {"USD": _inst_rows([(bs_end, lt_debt, bs_filed)])}}
    if assets is not None:
        gaap["Assets"] = {"units": {"USD": _inst_rows([(bs_end, assets, bs_filed)])}}
    if liabilities is not None:
        gaap["Liabilities"] = {"units": {"USD": _inst_rows([(bs_end, liabilities, bs_filed)])}}
    if equity is not None:
        gaap["StockholdersEquity"] = {"units": {"USD": _inst_rows([(bs_end, equity, bs_filed)])}}
    if shares_diluted is not None:
        gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = {
            "units": {"shares": _q_rows([(rev_q[-1][0], rev_q[-1][1], shares_diluted, rev_q[-1][3])])}
        }
    if shares_basic is not None:
        gaap["WeightedAverageNumberOfSharesOutstandingBasic"] = {
            "units": {"shares": _q_rows([(rev_q[-1][0], rev_q[-1][1], shares_basic, rev_q[-1][3])])}
        }

    facts: dict[str, Any] = {"us-gaap": gaap}
    if dei_shares is not None:
        facts["dei"] = {
            "EntityCommonStockSharesOutstanding": {
                "units": {"shares": _inst_rows([(bs_end, dei_shares, bs_filed)])}
            }
        }
    return {"facts": facts}


def _standard_8q(base_rev: float, growth: float = 1.25) -> list[tuple[str, str, float, str]]:
    """8 contiguous quarters: 2024Q3..2026Q2."""
    prev = [
        ("2024-07-01", "2024-09-30", base_rev, "2024-10-30"),
        ("2024-10-01", "2024-12-31", base_rev, "2025-02-15"),
        ("2025-01-01", "2025-03-31", base_rev, "2025-04-30"),
        ("2025-04-01", "2025-06-30", base_rev, "2025-07-30"),
    ]
    cur = [
        ("2025-07-01", "2025-09-30", base_rev * growth, "2025-10-30"),
        ("2025-10-01", "2025-12-31", base_rev * growth, "2026-02-15"),
        ("2026-01-01", "2026-03-31", base_rev * growth, "2026-04-30"),
        ("2026-04-01", "2026-06-30", base_rev * growth, "2026-07-30"),
    ]
    return prev + cur


def test_peer_groups_config_and_suggest() -> None:
    groups, fpi = load_peer_groups_config()
    assert len(groups) >= 10
    assert "TSM" in fpi and fpi["TSM"].adr_ratio == "5" and fpi["TSM"].reporting_currency == "TWD"

    sugg = suggest_peers_for_symbol("nvda")
    assert sugg.symbol == "NVDA"
    assert sugg.recommended_group is not None
    assert "AMD" in sugg.recommended_peers
    assert "NVDA" not in sugg.recommended_peers


def test_edge_case_1_tsm_fpi_twd_20f_isolation_and_verified_fx() -> None:
    """TSM 20-F IFRS + TWD: dimensionless margins preserved; USD multiples withheld without FX, computed with FX+ADR."""
    cf_tsm = {
        "facts": {
            "ifrs-full": {
                "Revenue": {
                    "units": {
                        "TWD": [
                            {"start": "2024-01-01", "end": "2024-12-31", "val": 2_000_000_000_000, "filed": "2025-04-15", "form": "20-F"},
                            {"start": "2025-01-01", "end": "2025-12-31", "val": 2_600_000_000_000, "filed": "2026-04-15", "form": "20-F"},
                        ]
                    }
                },
                "GrossProfit": {
                    "units": {
                        "TWD": [
                            {"start": "2025-01-01", "end": "2025-12-31", "val": 1_430_000_000_000, "filed": "2026-04-15", "form": "20-F"},
                        ]
                    }
                },
                "ProfitLossFromOperatingActivities": {
                    "units": {
                        "TWD": [
                            {"start": "2025-01-01", "end": "2025-12-31", "val": 1_170_000_000_000, "filed": "2026-04-15", "form": "20-F"},
                        ]
                    }
                },
                "ProfitLoss": {
                    "units": {
                        "TWD": [
                            {"start": "2025-01-01", "end": "2025-12-31", "val": 1_040_000_000_000, "filed": "2026-04-15", "form": "20-F"},
                        ]
                    }
                },
                "CashFlowsFromUsedInOperatingActivities": {
                    "units": {
                        "TWD": [
                            {"start": "2025-01-01", "end": "2025-12-31", "val": 1_500_000_000_000, "filed": "2026-04-15", "form": "20-F"},
                        ]
                    }
                },
                "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": {
                    "units": {
                        "TWD": [
                            {"start": "2025-01-01", "end": "2025-12-31", "val": 900_000_000_000, "filed": "2026-04-15", "form": "20-F"},
                        ]
                    }
                },
                "CashAndCashEquivalents": {"units": {"TWD": [{"end": "2025-12-31", "val": 1_800_000_000_000, "filed": "2026-04-15", "form": "20-F"}]}},
                "NoncurrentPortionOfNoncurrentBorrowings": {"units": {"TWD": [{"end": "2025-12-31", "val": 900_000_000_000, "filed": "2026-04-15", "form": "20-F"}]}},
                "Assets": {"units": {"TWD": [{"end": "2025-12-31", "val": 6_000_000_000_000, "filed": "2026-04-15", "form": "20-F"}]}},
                "Liabilities": {"units": {"TWD": [{"end": "2025-12-31", "val": 2_000_000_000_000, "filed": "2026-04-15", "form": "20-F"}]}},
                "Equity": {"units": {"TWD": [{"end": "2025-12-31", "val": 4_000_000_000_000, "filed": "2026-04-15", "form": "20-F"}]}},
                "AdjustedWeightedAverageShares": {
                    "units": {"shares": [{"start": "2025-01-01", "end": "2025-12-31", "val": 25_930_000_000, "filed": "2026-04-15", "form": "20-F"}]}
                },
            }
        }
    }
    snap = project_companyfacts_to_snapshot(
        cik="0001046179",
        symbol="TSM",
        company_name="TSMC",
        sic="3674",
        sic_description="Semiconductors",
        latest_periodic_accession="0001046179-26-000010",
        latest_periodic_filed_at="2026-04-15",
        companyfacts_doc=cf_tsm,
    )
    assert snap.taxonomy == "ifrs-full"
    assert snap.currency == "TWD"

    # Without verified FX -> dimensionless ratios present, USD multiples withheld
    m_no_fx = compute_company_metrics_from_snapshot(
        snap,
        price_usd=Decimal("180.00"),
        fpi_meta=FpiAdrMeta(adr_ratio="5", reporting_currency="TWD"),
        fx_to_usd=None,
    )
    assert m_no_fx.period_basis == "FY"
    assert m_no_fx.ttm_revenue_yoy_pct == "30.00"
    assert m_no_fx.ttm_gross_margin_pct == "55.00"
    assert m_no_fx.ttm_operating_margin_pct == "45.00"
    assert m_no_fx.pe_ttm is None
    assert m_no_fx.ev_to_fcf_ttm is None
    assert m_no_fx.is_comparable_for_median is False
    assert any("currency_adr_unverified" in n for n in m_no_fx.notes)

    # With verified FX (1 TWD = 0.031 USD) + ADR ratio 5 -> multiples computed accurately
    m_fx = compute_company_metrics_from_snapshot(
        snap,
        price_usd=Decimal("180.00"),
        fpi_meta=FpiAdrMeta(adr_ratio="5", reporting_currency="TWD"),
        fx_to_usd=Decimal("0.031"),
    )
    assert m_fx.pe_ttm is not None
    assert m_fx.ev_to_fcf_ttm is not None
    assert m_fx.reverse_dcf_implied_5y_fcf_cagr_pct is not None


def test_edge_case_2_intc_negative_fcf_and_loss_quarter_shares_basic_fallback() -> None:
    """INTC: negative FCF + missing diluted shares -> basic shares fallback, red quality, Reverse-DCF unavailable."""
    rev = _standard_8q(12_000_000_000, growth=0.95)
    ocf = [(s, e, 2_000_000_000, f) for s, e, _, f in rev]
    capex = [(s, e, 5_000_000_000, f) for s, e, _, f in rev]  # FCF = -3B/q -> -12B TTM
    ni = [(s, e, -1_000_000_000, f) for s, e, _, f in rev]
    cf = _make_us_companyfacts(
        rev_q=rev,
        oi_q=ni,
        ni_q=ni,
        ocf_q=ocf,
        capex_q=capex,
        shares_diluted=None,  # anti-dilutive in loss quarter
        shares_basic=4_200_000_000,
    )
    snap = project_companyfacts_to_snapshot(
        cik="0000050863",
        symbol="INTC",
        company_name="Intel",
        sic="3674",
        sic_description="Semiconductors",
        latest_periodic_accession="acc-intc",
        latest_periodic_filed_at="2026-07-30",
        companyfacts_doc=cf,
    )
    m = compute_company_metrics_from_snapshot(snap, price_usd=Decimal("22.00"))
    assert m.market_cap_usd == "92400000000"  # 22 * 4.2B via shares_basic fallback
    assert any("shares_basic" in n for n in m.notes)
    assert m.quality_light == "red"
    assert m.ev_to_fcf_ttm is None
    assert m.reverse_dcf_implied_5y_fcf_cagr_pct is None
    assert any("Reverse-DCF unavailable" in n for n in m.notes)


def test_edge_case_3_googl_dual_class_dei_share_divergence() -> None:
    """GOOGL dual-class: DEI shares diverge >15% from diluted shares -> warning surfaced."""
    rev = _standard_8q(80_000_000_000, growth=1.14)
    ocf = [(s, e, 28_000_000_000, f) for s, e, _, f in rev]
    capex = [(s, e, 10_000_000_000, f) for s, e, _, f in rev]
    ni = [(s, e, 22_000_000_000, f) for s, e, _, f in rev]
    cf = _make_us_companyfacts(
        rev_q=rev,
        oi_q=ni,
        ni_q=ni,
        ocf_q=ocf,
        capex_q=capex,
        shares_diluted=12_400_000_000,
        dei_shares=5_800_000_000,  # single share class on cover page (>50% divergence)
    )
    snap = project_companyfacts_to_snapshot(
        cik="0001652044",
        symbol="GOOGL",
        company_name="Alphabet",
        sic="7370",
        sic_description="Computer Programming",
        latest_periodic_accession="acc-googl",
        latest_periodic_filed_at="2026-07-30",
        companyfacts_doc=cf,
    )
    m = compute_company_metrics_from_snapshot(snap, price_usd=Decimal("180.00"))
    assert any("share_count_divergence" in n for n in m.notes)
    matrix = assemble_peer_matrix(primary_symbol="GOOGL", rows=[m])
    assert any(w.code == "share_count_divergence" for w in matrix.comparability_warnings)


def test_edge_case_4_financial_sic_6211_and_6021_gating() -> None:
    """GS (6211) & JPM (6021) gated: quality_light=not_applicable, EV/ROIC/Reverse-DCF disabled, mixed error."""
    assert evaluate_sec_sic_gate("6211").is_gated is True
    assert evaluate_sec_sic_gate("6211").sector_category == "bank"
    assert evaluate_sec_sic_gate("6021").is_gated is True

    rev = _standard_8q(13_000_000_000, growth=1.10)
    ni = [(s, e, 3_200_000_000, f) for s, e, _, f in rev]
    ocf = [(s, e, 4_000_000_000, f) for s, e, _, f in rev]
    capex = [(s, e, 500_000_000, f) for s, e, _, f in rev]
    cf = _make_us_companyfacts(rev_q=rev, oi_q=ni, ni_q=ni, ocf_q=ocf, capex_q=capex)

    gs_snap = project_companyfacts_to_snapshot(
        cik="0000886982",
        symbol="GS",
        company_name="Goldman Sachs",
        sic="6211",
        sic_description="Security Brokers",
        latest_periodic_accession="acc-gs",
        latest_periodic_filed_at="2026-07-30",
        companyfacts_doc=cf,
    )
    tech_snap = project_companyfacts_to_snapshot(
        cik="0001045810",
        symbol="NVDA",
        company_name="NVIDIA",
        sic="3674",
        sic_description="Semiconductors",
        latest_periodic_accession="acc-nvda",
        latest_periodic_filed_at="2026-07-30",
        companyfacts_doc=cf,
    )
    gs_m = compute_company_metrics_from_snapshot(gs_snap, price_usd=Decimal("500.00"))
    nvda_m = compute_company_metrics_from_snapshot(tech_snap, price_usd=Decimal("120.00"))

    assert gs_m.is_financial_gated is True
    assert gs_m.quality_light == "not_applicable"
    assert gs_m.enterprise_value_usd is None
    assert gs_m.ttm_roic_pct is None
    assert gs_m.reverse_dcf_implied_5y_fcf_cagr_pct is None
    assert gs_m.pe_ttm is not None  # P/E still valid for banks

    matrix = assemble_peer_matrix(primary_symbol="NVDA", rows=[nvda_m, gs_m])
    assert any(w.code == "financial_sic_mixed" and w.severity == "error" for w in matrix.comparability_warnings)


def test_edge_case_5_as_of_point_in_time_zero_lookahead() -> None:
    """Historical as_of strictly excludes quarters filed after as_of using the same cached snapshot."""
    rev = _standard_8q(10_000_000_000, growth=1.50)
    # 2026-06-30 quarter filed 2026-07-30; as_of=2026-05-15 must stop at 2026-03-31 (filed 2026-04-30)
    cf = _make_us_companyfacts(rev_q=rev)
    snap = project_companyfacts_to_snapshot(
        cik="0001045810",
        symbol="NVDA",
        company_name="NVIDIA",
        sic="3674",
        sic_description="Semiconductors",
        latest_periodic_accession="acc-nvda",
        latest_periodic_filed_at="2026-07-30",
        companyfacts_doc=cf,
    )
    m_now = compute_company_metrics_from_snapshot(snap, price_usd=Decimal("100.00"))
    m_past = compute_company_metrics_from_snapshot(snap, price_usd=Decimal("100.00"), as_of=date(2026, 5, 15))

    assert m_now.period_end_date == "2026-06-30"
    assert m_now.ttm_revenue_usd == "60000000000"  # 4 * 15B
    assert m_past.period_end_date == "2026-03-31"
    assert m_past.ttm_revenue_usd == "55000000000"  # 3 * 15B + 1 * 10B (zero lookahead!)


def test_edge_case_6_debt_free_balance_sheet_completeness_guard() -> None:
    """Absent debt tags -> 0 only when Assets & Liabilities are both present; otherwise EV withheld."""
    rev = _standard_8q(10_000_000_000, growth=1.20)
    ocf = [(s, e, 4_000_000_000, f) for s, e, _, f in rev]
    capex = [(s, e, 500_000_000, f) for s, e, _, f in rev]

    cf_complete = _make_us_companyfacts(
        rev_q=rev, ocf_q=ocf, capex_q=capex, lt_debt=None, assets=80_000_000_000, liabilities=20_000_000_000
    )
    snap_ok = project_companyfacts_to_snapshot(
        cik="0000000001", symbol="DEBTFREE", company_name="DebtFree", sic="7372", sic_description="Software",
        latest_periodic_accession="a1", latest_periodic_filed_at="2026-07-30", companyfacts_doc=cf_complete,
    )
    m_ok = compute_company_metrics_from_snapshot(snap_ok, price_usd=Decimal("100.00"))
    assert m_ok.enterprise_value_usd == "80000000000"  # 100B mcap + 0 debt - 20B cash
    assert m_ok.reverse_dcf_implied_5y_fcf_cagr_pct is not None

    cf_incomplete = _make_us_companyfacts(
        rev_q=rev, ocf_q=ocf, capex_q=capex, lt_debt=None, assets=None, liabilities=None
    )
    snap_bad = project_companyfacts_to_snapshot(
        cik="0000000002", symbol="INCOMP", company_name="IncompleteBS", sic="7372", sic_description="Software",
        latest_periodic_accession="a2", latest_periodic_filed_at="2026-07-30", companyfacts_doc=cf_incomplete,
    )
    m_bad = compute_company_metrics_from_snapshot(snap_bad, price_usd=Decimal("100.00"))
    assert m_bad.enterprise_value_usd is None
    assert m_bad.reverse_dcf_implied_5y_fcf_cagr_pct is None


def test_edge_case_7_negative_invested_capital_roic_sbc_and_layer2_brief_verification() -> None:
    """Negative Invested Capital -> ROIC unavailable; EV/FCF_ex_SBC computed; Layer 2 brief verifies cited_metrics."""
    rev = _standard_8q(20_000_000_000, growth=1.15)
    oi = [(s, e, 8_000_000_000, f) for s, e, _, f in rev]
    ni = [(s, e, 6_500_000_000, f) for s, e, _, f in rev]
    tax = [(s, e, 1_500_000_000, f) for s, e, _, f in rev]
    pt = [(s, e, 8_000_000_000, f) for s, e, _, f in rev]
    ocf = [(s, e, 9_000_000_000, f) for s, e, _, f in rev]
    capex = [(s, e, 1_000_000_000, f) for s, e, _, f in rev]  # TTM FCF = 32B
    sbc = [(s, e, 2_000_000_000, f) for s, e, _, f in rev]    # TTM SBC = 8B -> FCF ex SBC = 24B

    # Cash (50B) > Debt (5B) + Equity (30B) -> Invested Capital = -15B <= 0
    cf_cashrich = _make_us_companyfacts(
        rev_q=rev, oi_q=oi, ni_q=ni, tax_q=tax, pt_q=pt, ocf_q=ocf, capex_q=capex, sbc_q=sbc,
        cash=50_000_000_000, lt_debt=5_000_000_000, equity=30_000_000_000,
    )
    snap1 = project_companyfacts_to_snapshot(
        cik="0000000010", symbol="CASHRICH", company_name="CashRich", sic="7372", sic_description="Software",
        latest_periodic_accession="a10", latest_periodic_filed_at="2026-07-30", companyfacts_doc=cf_cashrich,
    )
    m1 = compute_company_metrics_from_snapshot(snap1, price_usd=Decimal("400.00"))
    assert m1.ttm_roic_pct is None
    assert any("Invested Capital <= 0" in n for n in m1.notes)
    assert m1.ev_to_fcf_ttm is not None
    assert m1.ev_to_fcf_ex_sbc_ttm is not None
    assert Decimal(m1.ev_to_fcf_ex_sbc_ttm) > Decimal(m1.ev_to_fcf_ttm)

    # Normal peer with positive Invested Capital
    cf_norm = _make_us_companyfacts(
        rev_q=rev, oi_q=oi, ni_q=ni, tax_q=tax, pt_q=pt, ocf_q=ocf, capex_q=capex, sbc_q=sbc,
        cash=10_000_000_000, lt_debt=20_000_000_000, equity=60_000_000_000,
    )
    snap2 = project_companyfacts_to_snapshot(
        cik="0000000011", symbol="NORM", company_name="NormalCo", sic="7372", sic_description="Software",
        latest_periodic_accession="a11", latest_periodic_filed_at="2026-07-30", companyfacts_doc=cf_norm,
    )
    m2 = compute_company_metrics_from_snapshot(snap2, price_usd=Decimal("320.00"))
    assert m2.ttm_roic_pct is not None

    matrix = assemble_peer_matrix(primary_symbol="CASHRICH", rows=[m1, m2])
    assert matrix.primary_watch_trigger_metrics["peer_revenue_yoy_rank"] is not None
    assert matrix.primary_watch_trigger_metrics["peer_reverse_dcf_spread_bps"] is not None

    # Fake runner returning 2 exact metrics + 1 hallucinated metric
    class _FakeRunner:
        def invoke(self, _req: Any) -> Any:
            payload = {
                "summary": "CASHRICH trades at a premium to NORM on EV/FCF.",
                "valuation_vs_fundamentals": "Both grow at 15% TTM with 25% SBC/FCF.",
                "relative_risks": ["SBC dilution reduces true FCF yield."],
                "cited_metrics": [
                    {"symbol": "CASHRICH", "metric_key": "ev_to_fcf_ttm", "value": m1.ev_to_fcf_ttm},
                    {"symbol": "NORM", "metric_key": "ttm_roic_pct", "value": m2.ttm_roic_pct},
                    {"symbol": "CASHRICH", "metric_key": "ttm_revenue_yoy_pct", "value": "99.99"},
                ],
            }
            return SimpleNamespace(content=json.dumps(payload), usage={"total_tokens": 1234})

    brief = generate_peer_attribution_brief(matrix, runner=_FakeRunner(), model_name="fake-model")
    assert brief.verified_metric_count == 2
    assert brief.total_cited_metric_count == 3
    assert brief.cited_metrics[0].verified is True
    assert brief.cited_metrics[1].verified is True
    assert brief.cited_metrics[2].verified is False
    assert brief.tokens_used == 1234


def test_v2_api_suggest_groups_compare_and_brief_end_to_end(monkeypatch: Any) -> None:
    """API integration test: suggest, user groups CRUD, cache-backed compare, and Layer 2 brief."""
    from fastapi.testclient import TestClient
    from app import main as api_main

    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    api_main.reset_state()

    rev_a = _standard_8q(25_000_000_000, growth=1.40)
    rev_b = _standard_8q(10_000_000_000, growth=1.15)
    for sym, cik, rev_rows in [("NVDA", "0001045810", rev_a), ("AMD", "0000002488", rev_b)]:
        oi = [(s, e, v * 0.40, f) for s, e, v, f in rev_rows]
        ni = [(s, e, v * 0.32, f) for s, e, v, f in rev_rows]
        ocf = [(s, e, v * 0.38, f) for s, e, v, f in rev_rows]
        capex = [(s, e, v * 0.04, f) for s, e, v, f in rev_rows]
        sbc = [(s, e, v * 0.05, f) for s, e, v, f in rev_rows]
        cf = _make_us_companyfacts(rev_q=rev_rows, oi_q=oi, ni_q=ni, ocf_q=ocf, capex_q=capex, sbc_q=sbc)
        snap = project_companyfacts_to_snapshot(
            cik=cik,
            symbol=sym,
            company_name=sym,
            sic="3674",
            sic_description="Semiconductors",
            latest_periodic_accession=f"acc-{sym}",
            latest_periodic_filed_at="2026-07-30",
            companyfacts_doc=cf,
        )
        api_main.PEER_COMPANY_SNAPSHOTS.put(f"snap:{cik}", snap.model_dump(mode="json"))

    class _FakeBriefRunner:
        def invoke(self, _req: Any) -> Any:
            return SimpleNamespace(
                content=json.dumps({
                    "summary": "NVDA leads AMD on TTM revenue growth.",
                    "valuation_vs_fundamentals": "Premium Reverse-DCF is backed by faster growth.",
                    "relative_risks": ["High expectations priced into NVDA."],
                    "cited_metrics": [
                        {"symbol": "NVDA", "metric_key": "ttm_revenue_yoy_pct", "value": "40.00"},
                        {"symbol": "AMD", "metric_key": "ttm_revenue_yoy_pct", "value": "15.00"},
                    ],
                }),
                usage={"total_tokens": 555},
            )

    api_main.app.state.peer_compare_hooks = {
        "quote_resolver": lambda sym, _as_of: (Decimal("120.00") if sym == "NVDA" else Decimal("140.00"), "2026-07-31", "test"),
        "model_runner": _FakeBriefRunner(),
    }

    try:
        client = TestClient(api_main.app)
        reg = client.post(
            "/v1/auth/local/register",
            json={"email": "v2peer@example.com", "password": "Password123!"},
        )
        assert reg.status_code in (200, 201), reg.text
        token = reg.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Suggest
        sugg = client.get("/v2/research/peers/suggest?symbol=NVDA", headers=headers)
        assert sugg.status_code == 200
        assert "AMD" in sugg.json()["recommended_peers"]

        # 2. Custom group CRUD
        created = client.post(
            "/v2/research/peers/groups",
            headers=headers,
            json={"name": "My AI Semis", "symbols": ["NVDA", "AMD"]},
        )
        assert created.status_code == 200
        gid = created.json()["group_id"]
        listed = client.get("/v2/research/peers/groups", headers=headers)
        assert listed.status_code == 200 and any(g["group_id"] == gid for g in listed.json())

        # 3. Compare (cache_only=True hits our seeded snapshots with 0 network)
        cmp_res = client.post(
            "/v2/research/peers/compare",
            headers=headers,
            json={"primary_symbol": "NVDA", "symbols": ["NVDA", "AMD"], "cache_only": True},
        )
        assert cmp_res.status_code == 200, cmp_res.text
        body = cmp_res.json()
        assert body["primary_symbol"] == "NVDA"
        assert len(body["companies"]) == 2
        assert body["cache_hits"] == 2
        cmp_id = body["comparison_id"]

        # 4. Layer 2 Brief by comparison_id
        brief_res = client.post(
            "/v2/research/peers/compare/brief",
            headers=headers,
            json={"comparison_id": cmp_id},
        )
        assert brief_res.status_code == 200, brief_res.text
        b = brief_res.json()
        assert b["verified_metric_count"] == 2
        assert b["total_cited_metric_count"] == 2
        assert b["tokens_used"] == 555

        # 5. Delete group
        deleted = client.delete(f"/v2/research/peers/groups/{gid}", headers=headers)
        assert deleted.status_code == 200 and deleted.json()["deleted"] is True

        # 6. Peer relative watch subscription
        watch_res = client.post(
            "/v2/research/peers/watch",
            headers=headers,
            json={
                "primary_symbol": "NVDA",
                "symbols": ["NVDA", "AMD"],
                "metric_key": "peer_reverse_dcf_spread_bps",
                "operator": "<=",
                "threshold_decimal": "1500",
            },
        )
        assert watch_res.status_code == 200, watch_res.text
        wbody = watch_res.json()
        assert "peer_relative" in wbody["watched_channels"]
        assert wbody["peer_watch_config"]["symbols"] == ["NVDA", "AMD"]
    finally:
        api_main.app.state.peer_compare_hooks = None


def test_plan_b_p0_pharma_ebit_derivation_capex_tag_and_quality_light_boundary() -> None:
    """Verify profit_before_tax + interest_expense EBIT derivation, LLY CapEx tag, and 0.697x OCF/NI yellow tier."""
    rev = _standard_8q(15_000_000_000, growth=1.30)
    # No OperatingIncomeLoss; only profit_before_tax (4B/q) + interest_expense (0.2B/q) + tax (0.6B/q -> 15% eff tax)
    pt = [(s, e, 4_000_000_000, f) for s, e, _, f in rev]
    int_exp = [(s, e, 200_000_000, f) for s, e, _, f in rev]
    tax = [(s, e, 600_000_000, f) for s, e, _, f in rev]
    ni = [(s, e, 3_400_000_000, f) for s, e, _, f in rev]
    # OCF/NI = 2.37B / 3.4B = 0.697x (< 0.70x), CapEx = 0.2B/q -> FCF = 2.17B/q (~11.1% or >15% if rev lower)
    ocf = [(s, e, 2_370_000_000, f) for s, e, _, f in rev]
    capex_other = [(s, e, 100_000_000, f) for s, e, _, f in rev]

    cf = _make_us_companyfacts(rev_q=rev, oi_q=None, ni_q=ni, tax_q=tax, pt_q=pt, ocf_q=ocf, capex_q=None)
    cf["facts"]["us-gaap"]["InterestExpense"] = {"units": {"USD": _q_rows(int_exp)}}
    cf["facts"]["us-gaap"]["PaymentsToAcquireOtherPropertyPlantAndEquipment"] = {"units": {"USD": _q_rows(capex_other)}}

    snap = project_companyfacts_to_snapshot(
        cik="0000059478",
        symbol="LLY",
        company_name="Eli Lilly",
        sic="2834",
        sic_description="Pharmaceuticals",
        latest_periodic_accession="acc-lly",
        latest_periodic_filed_at="2026-07-30",
        companyfacts_doc=cf,
    )
    assert snap.projection_version >= 2
    m = compute_company_metrics_from_snapshot(snap, price_usd=Decimal("800.00"))
    # EBIT derived = (4B + 0.2B) * 4 = 16.8B -> operating margin = 16.8 / 78 = 21.54%
    assert m.ttm_operating_margin_pct == "21.54"
    assert any("profit_before_tax + interest_expense" in n for n in m.notes)
    # ROIC uses 15% real eff tax (NOT 21% default!)
    assert m.ttm_roic_pct is not None
    assert not any("21% statutory" in n for n in m.notes)
    # LLY CapEx tag picked up -> FCF computed
    assert m.ttm_fcf_margin_pct is not None

    # High-FCF-margin (40%) + 0.697x OCF/NI -> yellow (NOT red) with 3-decimal display
    rev_nv = _standard_8q(10_000_000_000, growth=1.50)  # 60B TTM
    ni_nv = [(s, e, 10_000_000_000, f) for s, e, _, f in rev_nv]  # 40B TTM
    ocf_nv = [(s, e, 6_970_000_000, f) for s, e, _, f in rev_nv]  # 27.88B -> 0.697x NI
    cap_nv = [(s, e, 500_000_000, f) for s, e, _, f in rev_nv]    # 2B -> FCF 25.88B (43.1% margin)
    cf_nv = _make_us_companyfacts(rev_q=rev_nv, oi_q=ni_nv, ni_q=ni_nv, ocf_q=ocf_nv, capex_q=cap_nv)
    snap_nv = project_companyfacts_to_snapshot(
        cik="0001045810", symbol="NVDA", company_name="NVIDIA", sic="3674", sic_description="Semis",
        latest_periodic_accession="acc-nv", latest_periodic_filed_at="2026-07-30", companyfacts_doc=cf_nv,
    )
    m_nv = compute_company_metrics_from_snapshot(snap_nv, price_usd=Decimal("150.00"))
    assert m_nv.quality_light == "yellow"
    assert any("0.697x" in n for n in m_nv.quality_notes)


def test_plan_b_p2_peer_relative_radar_trigger_and_hysteresis() -> None:
    """Verify 0-LLM peer_relative watch trigger fires on breach and respects hysteresis after user review."""
    from app.fin_data.adapters.yahoo import _UsCode
    from app.research_radar_scheduler import (
        FilingWatchSubscription,
        PeerWatchConfig,
        evaluate_peer_relative_watch,
    )
    from app.research_thesis_ledger import WatchTrigger

    assert _UsCode("brk.b").yahoo_symbol == "BRK-B"

    sub = FilingWatchSubscription(
        account_id="acc_1",
        cik="0001045810",
        symbol="NVDA",
        watched_channels=["peer_relative"],
        peer_watch_config=PeerWatchConfig(
            symbols=["NVDA", "AMD"],
            triggers=[
                WatchTrigger(
                    kind="metric_rule",
                    description="Reverse-DCF spread ceiling",
                    metric_key="peer_reverse_dcf_spread_bps",
                    operator="<=",
                    threshold_decimal="1500",
                )
            ],
        ),
    )

    def _fake_matrix(spread_bps: str) -> Any:
        return SimpleNamespace(
            primary_symbol="NVDA",
            primary_coordinate_summary=f"Reverse-DCF +{spread_bps} bps",
            primary_watch_trigger_metrics={"peer_reverse_dcf_spread_bps": spread_bps},
            companies=[
                SimpleNamespace(symbol="NVDA", period_end_date="2026-07-26"),
                SimpleNamespace(symbol="AMD", period_end_date="2026-09-26"),
            ],
        )

    # 1. Within healthy bound (1200 <= 1500) -> no alert
    assert evaluate_peer_relative_watch(sub=sub, ledger=None, existing_inbox=[], peer_compare_fn=lambda _s, _p: _fake_matrix("1200")) == []

    # 2. Breaches bound (1800 > 1500) -> fires 1 peer_relative inbox item
    items = evaluate_peer_relative_watch(sub=sub, ledger=None, existing_inbox=[], peer_compare_fn=lambda _s, _p: _fake_matrix("1800"))
    assert len(items) == 1
    assert items[0].source_channel == "peer_relative"
    assert items[0].scorecard_diff["compare_url"] == "/compare?primary=NVDA&peers=AMD"

    # 3. After user dismisses, small wiggle (+100 bps to 1900) stays quiet (hysteresis < 250 bps)
    reviewed = items[0].model_copy(update={"review_state": "dismissed_with_note"})
    assert evaluate_peer_relative_watch(sub=sub, ledger=None, existing_inbox=[reviewed], peer_compare_fn=lambda _s, _p: _fake_matrix("1900")) == []

    # 4. Material worsening (+300 bps to 2100 >= 1800 + 250) re-alerts
    re_items = evaluate_peer_relative_watch(sub=sub, ledger=None, existing_inbox=[reviewed], peer_compare_fn=lambda _s, _p: _fake_matrix("2100"))
    assert len(re_items) == 1


def test_v4a1_source_unavailable_maps_to_valid_503_contract_with_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify SecPacingError('source_unavailable') on /peers/compare and /peers/watch returns 503 + Retry-After + valid error_code."""
    from fastapi.testclient import TestClient
    import app.main as api_main
    from app.fin_data.adapters.sec_events.pacing import SecPacingError

    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    client = TestClient(api_main.app)
    reg = client.post(
        "/v1/auth/local/register",
        json={"email": "v4a1_503@example.com", "password": "Password123!"},
    )
    assert reg.status_code in (200, 201), reg.text
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise SecPacingError("source_unavailable", "SEC EDGAR upstream timeout.", retryable=True, retry_after_seconds=12.0)

    monkeypatch.setattr(api_main, "load_or_fetch_company_snapshot", _boom)
    monkeypatch.setattr(api_main, "resolve_ticker_to_cik", _boom)
    api_main.PEER_COMPANY_SNAPSHOTS.put("sym:BROKEN", {"cik": "0009999999", "symbol": "BROKEN"})
    try:
        r_cmp = client.post(
            "/v2/research/peers/compare",
            headers=headers,
            json={"primary_symbol": "NVDA", "symbols": ["NVDA", "AMD"], "cache_only": False},
        )
        assert r_cmp.status_code == 503, r_cmp.text
        assert r_cmp.headers.get("retry-after") == "12"
        body_cmp = r_cmp.json()
        assert body_cmp["error_code"] == "internal_error"
        assert body_cmp["retryable"] is True
        assert any(d.get("message") == "source_unavailable" for d in body_cmp.get("details") or [])

        r_watch = client.post(
            "/v2/research/peers/watch",
            headers=headers,
            json={"primary_symbol": "NO_CACHE_SYM", "symbols": ["NO_CACHE_SYM", "AMD"]},
        )
        assert r_watch.status_code == 503, r_watch.text
        assert r_watch.headers.get("retry-after") == "12"
        body_watch = r_watch.json()
        assert body_watch["error_code"] == "internal_error"
        assert any(d.get("message") == "source_unavailable" for d in body_watch.get("details") or [])
    finally:
        api_main.PEER_COMPANY_SNAPSHOTS.delete("sym:BROKEN")
        client.delete("/v1/auth/local/account", headers=headers)



