"""V4a US Major Bank & BHC Three-Source Disclosure & Segment Extractor (0-LLM Deterministic).

Fuses three official free SEC EDGAR primary sources into an immutable ``BankDisclosureSnapshot``:
1. Tier A: ``PeerCompanySnapshot`` v6 (projected XBRL companyfacts + accounting identities + date-aligned guards).
2. Tier B: ``10-Q / 10-K`` ``FilingSummary.xml`` + targeted ``R*.htm`` (Income Statement ``R2``, Balance Sheet ``R4`` with
   VIE-section stop guard, Business Segments ``(Details)`` with R3-R7 zero-delta conservation, Regulatory Capital
   ``(Details)`` with R8 BHC-parent filter ex ``Bank, N.A.`` and R4b ratio normalization, and HTM Securities).
3. Tier C: ``8-K Item 2.02 EX-99.1/99.2/99.3`` (row-aligned HTML tables, skipping image-only slide decks) +
   256KB streaming ``10-Q Item 2 MD&A`` scan (via ``sec_get_to_tempfile()`` or preloaded Stage 0 ``c_bundle``)
   for disclosures omitted from 8-K (e.g., GS TBVPS/ROTE, BAC/C SCB + G-SIB CET1 minimum requirements).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import os
import re
from typing import Any, Mapping, Sequence
from typing_extensions import Literal

from pydantic import BaseModel, ConfigDict, Field

from ...research_bank_versions import BANK_DISCLOSURE_VERSION
from .sec_deep import (
    DualPeriodEarningsRelease,
    EarningsReleasePeriod,
    FilingReportEntry,
    FilingTableCatalog,
    FilingTableCell,
    FilingTableData,
    FilingTableRow,
    ParsedHtmlTable,
    SecDeepAdapter,
    normalize_sec_ratio_pct,
    parse_filing_summary_xml,
    parse_r_html_table,
    sec_earnings_release,
)
from .sec_edgar import cik10
from .sec_events.pacing import (
    SecPacingLimiter,
    fetch_company_submissions,
    sec_get_json,
    sec_get_text,
    sec_get_to_tempfile,
)

BankSubtype = Literal[
    "universal_commercial",
    "investment_bank_bhc",
    "custodian_or_broker_bank",
    "not_applicable",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _d(val: Any) -> Decimal | None:
    if val is None or isinstance(val, bool):
        return None
    txt = str(val).strip()
    if not txt or txt in ("—", "–", "-", "--", "N/A", "n/a", "nil"):
        return None
    neg = False
    if txt.startswith("(") and txt.endswith(")"):
        neg = True
        txt = txt[1:-1].strip()
    txt = txt.replace(",", "").replace("$", "").replace("%", "").strip()
    if not txt:
        return None
    try:
        dec = Decimal(txt)
    except (InvalidOperation, ValueError):
        return None
    if not dec.is_finite():
        return None
    return -dec if neg else dec


def _qstr(val: Decimal | None, places: int = 2) -> str | None:
    if val is None or not val.is_finite():
        return None
    q = Decimal(10) ** (-places)
    return format(val.quantize(q, rounding=ROUND_HALF_UP), "f")


def _dstr(val: Decimal | None) -> str | None:
    if val is None or not val.is_finite():
        return None
    if val == val.to_integral_value():
        return format(val.quantize(Decimal("1")), "f")
    return format(val.normalize(), "f")


def _parse_date(val: Any) -> date | None:
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, str):
        s = val.strip()[:10]
        try:
            return date.fromisoformat(s)
        except ValueError:
            return None
    return None


class _FrozenBankModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BankSegmentRow(_FrozenBankModel):
    segment_key: str
    display_name: str
    section_role: Literal[
        "operating_segment",
        "corporate_or_other",
        "reconciling_or_elimination",
        "operating_segments_subtotal",
    ]
    xbrl_axis_member: str | None = None
    reporting_basis: Literal["gaap", "managed_fte"] = "gaap"
    net_revenue_usd: str | None = None
    nii_usd: str | None = None
    noninterest_income_usd: str | None = None
    noninterest_expense_usd: str | None = None
    ppnr_usd: str | None = None
    provision_usd: str | None = None
    pretax_income_usd: str | None = None
    net_income_usd: str | None = None
    avg_equity_or_tce_usd: str | None = None
    total_assets_usd: str | None = None
    avg_loans_usd: str | None = None
    avg_deposits_usd: str | None = None
    segment_roe_or_rotce_pct: str | None = None
    segment_efficiency_pct: str | None = None
    revenue_share_pct: str | None = None
    net_revenue_yoy_pct: str | None = None
    ppnr_yoy_pct: str | None = None


class BankSegmentSnapshot(_FrozenBankModel):
    cik: str
    symbol: str
    accession: str
    r_file: str
    source_url: str
    period_kind: Literal["3M", "9M", "12M"]
    period_end_date: str
    prior_yoy_period_end_date: str | None = None
    consolidated_basis: Literal["gaap", "fte"]
    segments_basis: Literal["gaap", "managed_fte"]
    consolidated_net_revenue_usd: str
    fte_reconciling_adjustment_usd: str | None = None
    operating_segments: list[BankSegmentRow] = Field(default_factory=list)
    corporate_or_other: list[BankSegmentRow] = Field(default_factory=list)
    reconciling_items: list[BankSegmentRow] = Field(default_factory=list)
    reconciliation_delta_usd: str = "0"
    reconciliation_status: Literal["exact", "within_rounding", "mismatch"] = "exact"
    dropped_footnote_columns: int = 0
    footnotes: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class TbvpsReconciliationBridge(_FrozenBankModel):
    total_stockholders_equity_usd: str | None = None
    preferred_stock_usd: str | None = None
    preferred_apic_adj_usd: str | None = None
    common_equity_usd: str | None = None
    goodwill_usd: str | None = None
    intangibles_gross_xbrl_usd: str | None = None
    msr_excluded_from_intangibles_usd: str | None = None
    intangibles_ex_msr_usd: str | None = None
    dtl_intangibles_addback_usd: str | None = None
    other_tce_adjustments_usd: str | None = None
    tce_reported_usd: str | None = None
    tce_xbrl_std_usd: str | None = None
    shares_ending_common: str | None = None
    shares_reported_denominator: str | None = None
    rsu_share_addon: str | None = None
    bvps_reported_usd: str | None = None
    tbvps_reported_usd: str | None = None
    tbvps_reported_source: Literal[
        "8k_supplement",
        "10q_mda",
        "tce_div_reported_basic_shares",
        "unavailable",
    ] = "unavailable"
    tbvps_xbrl_std_usd: str | None = None
    tbvps_gap_pct: str | None = None
    denominator_note: str | None = None
    tbvps_reported_verified: bool = False
    tbvps_reported_verification_reason: str = "unverified"
    tbvps_xbrl_std_verified: bool = False
    tbvps_xbrl_std_verification_reason: str = "unverified"
    tbvps_reported_provenance: dict[str, str] = Field(default_factory=dict)
    tbvps_xbrl_std_provenance: dict[str, str] = Field(default_factory=dict)


class BankDisclosureSnapshot(_FrozenBankModel):
    schema_version: Literal["v4a.1", "v4a.2", "v4a.3"] = BANK_DISCLOSURE_VERSION
    cik: str
    symbol: str
    company_name: str
    sic: str
    bank_subtype: BankSubtype
    period_end_date: str
    fiscal_period_label: str
    periodic_form: Literal["10-Q", "10-K"]
    periodic_accession: str
    periodic_filed_at: str
    earnings_8k_accession: str | None = None
    earnings_8k_filed_at: str | None = None
    earnings_8k_exhibits_used: list[str] = Field(default_factory=list)
    xbrl_companyfacts_fresh: bool = True

    # Income statement (3M current + TTM, USD)
    net_revenue_gaap_q_usd: str
    net_revenue_managed_q_usd: str | None = None
    nii_gaap_q_usd: str
    nii_fte_q_usd: str | None = None
    noninterest_income_gaap_q_usd: str
    noninterest_expense_q_usd: str
    ppnr_gaap_q_usd: str
    ppnr_managed_q_usd: str | None = None
    provision_q_usd: str
    provision_ttm_usd: str | None = None
    net_income_q_usd: str
    net_income_to_common_q_usd: str
    net_revenue_gaap_ttm_usd: str | None = None
    ppnr_gaap_ttm_usd: str | None = None
    net_income_to_common_ttm_usd: str | None = None

    # Balance sheet & credit quality (USD)
    total_assets_usd: str
    total_deposits_usd: str | None = None
    total_deposits_prior_q_usd: str | None = None
    deposits_noninterest_bearing_usd: str | None = None
    total_loans_gross_usd: str | None = None
    allowance_credit_losses_usd: str | None = None
    nonaccrual_loans_usd: str | None = None
    net_charge_offs_q_usd: str | None = None
    avg_tangible_common_equity_q_usd: str | None = None
    aoci_usd: str | None = None
    common_dividends_ttm_usd: str | None = None
    common_buybacks_ttm_usd: str | None = None

    # Regulatory capital (BHC consolidated parent, percent normalized per R4b)
    cet1_capital_usd: str | None = None
    rwa_standardized_usd: str | None = None
    cet1_standardized_pct: str | None = None
    cet1_standardized_source: Literal["10q_final", "8k_preliminary", "xbrl_companyfacts"] | None = None
    cet1_preliminary_8k_pct: str | None = None
    cet1_advanced_pct: str | None = None
    cet1_reg_min_pct: str | None = None
    cet1_scb_pct: str | None = None
    cet1_gsib_surcharge_pct: str | None = None
    tier1_leverage_pct: str | None = None
    slr_pct: str | None = None
    slr_min_pct: str | None = None

    # HTM / AFS securities unrealized P&L (10-Q Notes / Details, USD)
    htm_amortized_cost_usd: str | None = None
    htm_fair_value_usd: str | None = None
    htm_acl_usd: str | None = None
    htm_gross_unrealized_gain_usd: str | None = None
    htm_gross_unrealized_loss_usd: str | None = None
    htm_net_unrealized_pnl_usd: str | None = None
    afs_net_unrealized_pnl_usd: str | None = None

    # Disclosed headline ratios & bridges
    efficiency_ratio_reported_pct: str | None = None
    nim_fte_pct: str | None = None
    rotce_q_reported_pct: str | None = None
    rotce_q_verified: bool = False
    rotce_q_verification_reason: str = "unverified"
    rotce_q_provenance: dict[str, str] = Field(default_factory=dict)
    rotce_ytd_reported_pct: str | None = None
    tbvps_bridge: TbvpsReconciliationBridge
    segments: BankSegmentSnapshot | None = None

    provenance_sources: list[dict[str, str]] = Field(default_factory=list)
    metric_provenance: dict[str, dict[str, str]] = Field(default_factory=dict)
    data_gaps: list[str] = Field(default_factory=list)
    cached_at: datetime = Field(default_factory=_utcnow)


class BankQualificationResult(_FrozenBankModel):
    is_bank_v4a: bool
    bank_subtype: BankSubtype = "not_applicable"
    active_sector_plugin: Literal["us_large_bank_v4a"] | None = None
    sic: str | None = None
    reason: str
    loans_to_assets_pct: str | None = None
    deposits_to_assets_pct: str | None = None
    noninterest_share_pct: str | None = None


# --------------------------------------------------------------------------- Helper utilities for PeerCompanySnapshot (Tier A)

def _extract_latest_instant_from_snapshot(
    snap: Any,
    line_name: str,
    *,
    as_of: date | None = None,
    required_unit: str | None = None,
    target_end: date | None = None,
    max_end_drift_days: int | None = None,
) -> tuple[Decimal | None, date | None, str | None, str | None]:
    """Return ``(val, end_date, accn, tag)`` from ``PeerCompanySnapshot.facts_by_line[line_name]``."""
    facts_by_line = getattr(snap, "facts_by_line", None) or {}
    line_bucket = facts_by_line.get(line_name)
    if not isinstance(line_bucket, Mapping):
        return None, None, None, None

    best: tuple[date, date, int, Decimal, str | None, str] | None = None
    for idx_tag, (tag, units_map) in enumerate(line_bucket.items()):
        if not isinstance(units_map, Mapping):
            continue
        unit_keys = [required_unit] if required_unit else units_map.keys()
        for uk in unit_keys:
            rows = units_map.get(uk) or []
            for r in rows:
                if not isinstance(r, Mapping) or r.get("start") is not None:
                    continue
                end_d = _parse_date(r.get("end"))
                if end_d is None:
                    continue
                filed_d = _parse_date(r.get("filed"))
                if required_unit and as_of and filed_d is None:
                    continue
                if as_of is not None and filed_d is not None and filed_d > as_of:
                    continue
                if target_end is not None and max_end_drift_days is not None:
                    if abs((end_d - target_end).days) > max_end_drift_days:
                        continue
                val = _d(r.get("val"))
                if val is None:
                    continue
                accn = str(r.get("accn") or "").strip() or None
                cand = (end_d, filed_d or date.min, -idx_tag, val, accn, tag)
                if best is None or cand[:3] > best[:3]:
                    best = cand
        if best is not None and target_end is not None:
            # Respect ordered tag priority when within target_end alignment
            break
    if best is None:
        return None, None, None, None
    return best[3], best[0], best[4], best[5]


def _extract_prior_instant_from_snapshot(
    snap: Any,
    line_name: str,
    *,
    latest_end: date,
    min_days: int = 75,
    max_days: int = 115,
    as_of: date | None = None,
) -> Decimal | None:
    facts_by_line = getattr(snap, "facts_by_line", None) or {}
    line_bucket = facts_by_line.get(line_name)
    if not isinstance(line_bucket, Mapping):
        return None
    best: tuple[date, date, Decimal] | None = None
    for _tag, units_map in line_bucket.items():
        if not isinstance(units_map, Mapping):
            continue
        for _uk, rows in units_map.items():
            for r in rows or []:
                if not isinstance(r, Mapping) or r.get("start") is not None:
                    continue
                end_d = _parse_date(r.get("end"))
                if end_d is None:
                    continue
                diff = (latest_end - end_d).days
                if not (min_days <= diff <= max_days):
                    continue
                filed_d = _parse_date(r.get("filed"))
                if as_of is not None and filed_d is not None and filed_d > as_of:
                    continue
                val = _d(r.get("val"))
                if val is None:
                    continue
                cand = (end_d, filed_d or date.min, val)
                if best is None or cand[:2] > best[:2]:
                    best = cand
    return best[2] if best else None


def _extract_quarter_and_ttm_flow_from_snapshot(
    snap: Any,
    line_name: str,
    *,
    as_of: date | None = None,
) -> tuple[Decimal | None, Decimal | None, date | None, str | None]:
    """Return ``(latest_quarter_val, ttm_val, latest_end_date, fiscal_label)`` from projected facts."""
    facts_by_line = getattr(snap, "facts_by_line", None) or {}
    line_bucket = facts_by_line.get(line_name)
    if not isinstance(line_bucket, Mapping):
        return None, None, None, None

    # Pick first tag with usable flow periods
    for _tag, units_map in line_bucket.items():
        if not isinstance(units_map, Mapping):
            continue
        for _uk, rows in units_map.items():
            q_by_end: dict[date, tuple[date, Decimal, str | None]] = {}
            ytd_rows: list[tuple[date, date, date, Decimal]] = []
            fy_rows: list[tuple[date, date, date, Decimal]] = []
            for r in rows or []:
                if not isinstance(r, Mapping):
                    continue
                s_d = _parse_date(r.get("start"))
                e_d = _parse_date(r.get("end"))
                if s_d is None or e_d is None:
                    continue
                filed_d = _parse_date(r.get("filed")) or date.min
                if as_of is not None and filed_d != date.min and filed_d > as_of:
                    continue
                val = _d(r.get("val"))
                if val is None:
                    continue
                days = (e_d - s_d).days
                lbl = f"FY{r['fy']}-{r['fp']}" if r.get("fy") and r.get("fp") else None
                if 78 <= days <= 105:
                    prev = q_by_end.get(e_d)
                    if prev is None or filed_d >= prev[0]:
                        q_by_end[e_d] = (filed_d, val, lbl)
                elif 160 <= days <= 290:
                    ytd_rows.append((s_d, e_d, filed_d, val))
                elif 340 <= days <= 380:
                    fy_rows.append((s_d, e_d, filed_d, val))

            # Derive missing Q4 or Q2/Q3 standalone from YTD minus prior YTD when needed
            # Step 1: 9M / 6M minus prior 6M / 3M when standalone Q missing
            for s_d, e_d, _fd, ytd_val in sorted(ytd_rows, key=lambda x: x[1]):
                if e_d in q_by_end:
                    continue
                days = (e_d - s_d).days
                # Direct match against either standalone Q1 (if ~180d YTD) or shorter YTD sharing start
                prior_match: Decimal | None = None
                if 160 <= days <= 200:
                    # 6M YTD - Q1
                    q1_ends = [qe for qe in q_by_end if 70 <= (e_d - qe).days <= 105 and (qe - s_d).days <= 105]
                    if q1_ends:
                        prior_match = q_by_end[max(q1_ends)][1]
                elif 250 <= days <= 290:
                    # 9M YTD - 6M YTD
                    p6 = [pv for ps, pe, _pfd, pv in ytd_rows if ps == s_d and 160 <= (pe - ps).days <= 200]
                    if p6:
                        prior_match = p6[-1]
                if prior_match is not None:
                    q_by_end[e_d] = (date.min, ytd_val - prior_match, None)

            # Step 2: FY - 9M YTD => Q4
            for s_d, e_d, _fd, fy_val in sorted(fy_rows, key=lambda x: x[1]):
                if e_d in q_by_end:
                    continue
                p9 = [pv for ps, pe, _pfd, pv in ytd_rows if ps == s_d and 250 <= (pe - ps).days <= 290]
                if p9:
                    q_by_end[e_d] = (date.min, fy_val - p9[-1], None)

            if not q_by_end:
                continue
            ordered = sorted(q_by_end.items(), key=lambda kv: kv[0], reverse=True)
            latest_end, (_fd, latest_q, latest_lbl) = ordered[0]
            ttm_val: Decimal | None = None
            if len(ordered) >= 4:
                ttm_val = sum((ordered[i][1][1] for i in range(4)), Decimal("0"))
            elif fy_rows:
                # Try FY + latest_YTD - prior_YTD when fewer than 4 standalone quarters exist
                fy_latest = max(fy_rows, key=lambda x: x[1])
                if latest_end > fy_latest[1]:
                    cur_ytd = [yv for ys, ye, _yfd, yv in ytd_rows if ye == latest_end]
                    if cur_ytd:
                        prior_end_target = date(latest_end.year - 1, latest_end.month, min(28, latest_end.day))
                        pri_ytd = [
                            yv for ys, ye, _yfd, yv in ytd_rows if abs((ye - prior_end_target).days) <= 10
                        ]
                        if pri_ytd:
                            ttm_val = fy_latest[3] + cur_ytd[-1] - pri_ytd[-1]
                elif latest_end == fy_latest[1]:
                    ttm_val = fy_latest[3]
            return latest_q, ttm_val, latest_end, latest_lbl
    return None, None, None, None


def _extract_dei_shares_from_snapshot(
    snap: Any,
    *,
    target_end: date | None,
    max_drift_days: int = 45,
    as_of: date | None = None,
) -> tuple[Decimal | None, date | None]:
    rows = getattr(snap, "dei_shares_outstanding", None) or []
    best: tuple[date, date, Decimal] | None = None
    for r in rows:
        if not isinstance(r, Mapping):
            continue
        end_d = _parse_date(r.get("end"))
        if end_d is None:
            continue
        filed_d = _parse_date(r.get("filed"))
        if as_of is not None and filed_d is not None and filed_d > as_of:
            continue
        if target_end is not None and abs((end_d - target_end).days) > max_drift_days:
            continue
        val = _d(r.get("val"))
        if val is None or val <= 0:
            continue
        cand = (end_d, filed_d or date.min, val)
        if best is None or cand[:2] > best[:2]:
            best = cand
    return (best[2], best[0]) if best else (None, None)


# --------------------------------------------------------------------------- D2 Bank Qualification Gate

def qualify_bank_sector_applicability(
    *,
    sic: str | int | None,
    snap: Any | None = None,
    disclosure: BankDisclosureSnapshot | None = None,
    as_of: date | None = None,
) -> BankQualificationResult:
    """Post-XBRL generic 3-subtype Bank Qualification Gate (D2).

    Only activates for SIC 6020-6036 or SIC 6211 with real banking book (Deposits > 0, Loans > 0,
    Deposits/Assets >= 15%). Strictly blocks card/consumer finance (6199/6141 AXP), pure asset managers
    (6211 BLK), insurance (6300-6411 PGR), REITs (6798 PLD), and non-financial issuers.
    """
    sic_str = str(sic or "").strip()
    digits = re.sub(r"\D", "", sic_str)[:4]
    if not digits:
        return BankQualificationResult(is_bank_v4a=False, sic=None, reason="missing_sic")
    sic_int = int(digits)

    # Hard SIC allowlist: commercial/savings/trust banks (6020-6036) & BHC broker-dealers (6211)
    if not ((6020 <= sic_int <= 6036) or sic_int == 6211):
        return BankQualificationResult(
            is_bank_v4a=False,
            sic=digits,
            reason=f"sic_{digits}_outside_bank_v4a_allowlist",
        )

    assets: Decimal | None = None
    deposits: Decimal | None = None
    loans: Decimal | None = None
    net_rev: Decimal | None = None
    nii: Decimal | None = None

    if disclosure is not None:
        assets = _d(disclosure.total_assets_usd)
        deposits = _d(disclosure.total_deposits_usd)
        loans = _d(disclosure.total_loans_gross_usd)
        net_rev = _d(disclosure.net_revenue_gaap_q_usd)
        nii = _d(disclosure.nii_gaap_q_usd)
    elif snap is not None:
        assets, _, _, _ = _extract_latest_instant_from_snapshot(snap, "total_assets", as_of=as_of)
        deposits, _, _, _ = _extract_latest_instant_from_snapshot(snap, "deposits", as_of=as_of)
        loans, _, _, _ = _extract_latest_instant_from_snapshot(snap, "loans_gross", as_of=as_of)
        if loans is None:
            loans, _, _, _ = _extract_latest_instant_from_snapshot(snap, "loans_net", as_of=as_of)
        net_rev, _, _, _ = _extract_quarter_and_ttm_flow_from_snapshot(snap, "bank_net_revenue", as_of=as_of)
        nii, _, _, _ = _extract_quarter_and_ttm_flow_from_snapshot(snap, "net_interest_income", as_of=as_of)
        if net_rev is None and nii is not None:
            nonint, _, _, _ = _extract_quarter_and_ttm_flow_from_snapshot(snap, "noninterest_income", as_of=as_of)
            if nonint is not None:
                net_rev = nii + nonint

    dep_ratio = (deposits / assets) if (deposits is not None and assets and assets > 0) else None
    loan_ratio = (loans / assets) if (loans is not None and assets and assets > 0) else None
    nonint_share = ((net_rev - nii) / net_rev) if (net_rev and net_rev > 0 and nii is not None) else None

    dep_pct = _qstr(dep_ratio * Decimal("100"), 2) if dep_ratio is not None else None
    loan_pct = _qstr(loan_ratio * Decimal("100"), 2) if loan_ratio is not None else None
    ni_share_pct = _qstr(nonint_share * Decimal("100"), 2) if nonint_share is not None else None

    # Enforce BHC banking book gate for SIC 6211 (blocks BLK and pure fee brokerages)
    if sic_int == 6211:
        if deposits is None or deposits <= 0 or loans is None or loans <= 0 or dep_ratio is None or dep_ratio < Decimal("0.15"):
            return BankQualificationResult(
                is_bank_v4a=False,
                sic=digits,
                reason="sic_6211_without_bhc_banking_book",
                loans_to_assets_pct=loan_pct,
                deposits_to_assets_pct=dep_pct,
                noninterest_share_pct=ni_share_pct,
            )

    # 3-subtype routing
    if sic_int == 6211 and loan_ratio is not None and loan_ratio < Decimal("0.35") and nonint_share is not None and nonint_share >= Decimal("0.60"):
        subtype: BankSubtype = "investment_bank_bhc"
        reason = "qualified_investment_bank_bhc"
    elif loan_ratio is not None and loan_ratio < Decimal("0.25"):
        subtype = "custodian_or_broker_bank"
        reason = "qualified_custodian_or_broker_bank"
    else:
        subtype = "universal_commercial"
        reason = "qualified_universal_commercial_bank"

    return BankQualificationResult(
        is_bank_v4a=True,
        bank_subtype=subtype,
        active_sector_plugin="us_large_bank_v4a",
        sic=digits,
        reason=reason,
        loans_to_assets_pct=loan_pct,
        deposits_to_assets_pct=dep_pct,
        noninterest_share_pct=ni_share_pct,
    )


# --------------------------------------------------------------------------- R3-R7 (Details) Bank Segment Extractor

def _scale_cell_money(cell: FilingTableCell, table_mult: int | None, table_note: str | None = None) -> Decimal | None:
    if cell.numeric_value is None:
        return None
    if cell.is_percent or cell.scale == "percent":
        return None
    mult_map = {"billions": 1_000_000_000, "millions": 1_000_000, "thousands": 1_000, "units": 1}
    if cell.scale and str(cell.scale).lower() in mult_map:
        mult = mult_map[str(cell.scale).lower()]
    elif table_mult is not None:
        mult = table_mult
    else:
        note_l = (table_note or "").lower()
        if "usd ($)" in note_l and not any(u in note_l for u in ("million", "thousand", "billion")):
            mult = 1
        elif abs(cell.numeric_value) >= Decimal("100000000"):
            mult = 1
        else:
            mult = 1_000_000
    return cell.numeric_value * Decimal(mult)


def _select_segment_columns(
    table: FilingTableData,
    *,
    preferred_kind: Literal["3M", "9M", "12M"] = "3M",
) -> tuple[int | None, int | None, Literal["3M", "9M", "12M"], date | None, date | None]:
    """R3: Choose current period column & optional YoY column by period kind + core P&L row coverage."""
    core_row_re = re.compile(
        r"(?:net\s+revenue|total\s+revenue|revenues?|net\s+interest|noninterest|provision|expense|net\s+income)",
        re.IGNORECASE,
    )
    delta_hdr_re = re.compile(r"(?:\$\s*o/\(u\)|%\s*change|change|\bbps\b|variance)", re.IGNORECASE)

    # Check coverage per column index using table.rows (excluding abstract header rows)
    data_rows = [r for r in table.rows if not r.is_abstract and not r.is_header and core_row_re.search(r.label)]
    col_stats: list[tuple[int, str, date | None, int, bool]] = []
    for col in table.columns:
        if delta_hdr_re.search(col.header):
            continue
        p_kind = col.period_kind or ""
        p_end = col.period_end
        if p_end is None:
            continue
        cov = sum(
            1 for r in data_rows
            if col.index < len(r.cells) and r.cells[col.index].numeric_value is not None and not r.cells[col.index].is_percent
        )
        is_ratio_col = "%" in col.header or "percent" in str(col.unit or "").lower()
        col_stats.append((col.index, p_kind, p_end, cov, is_ratio_col))

    # Filter sparse helper columns (e.g., JPM Jun. 30 Visa-only column has cov < 5)
    dense = [c for c in col_stats if not c[4] and c[3] >= 5]
    if not dense:
        dense = [c for c in col_stats if not c[4] and c[3] >= 3]
    if not dense:
        return None, None, preferred_kind, None, None

    avail_kinds = {c[1] for c in dense}
    chosen_kind: Literal["3M", "9M", "12M"] = preferred_kind
    if preferred_kind not in avail_kinds:
        for fallback_k in ("3M", "12M", "9M"):
            if fallback_k in avail_kinds:
                chosen_kind = fallback_k  # type: ignore[assignment]
                break
        else:
            return None, None, preferred_kind, None, None

    kind_cols = [c for c in dense if c[1] == chosen_kind]
    max_end = max(c[2] for c in kind_cols if c[2] is not None)
    # First dense column with max_end is the primary GAAP/Managed P&L column (avoids ROE ratio cols if second)
    cur_cands = [c for c in kind_cols if c[2] == max_end]
    cur_idx = cur_cands[0][0]

    yoy_idx: int | None = None
    yoy_end: date | None = None
    yoy_cands = [
        c for c in kind_cols
        if c[2] is not None and 355 <= (max_end - c[2]).days <= 375
    ]
    if yoy_cands:
        yoy_idx = yoy_cands[0][0]
        yoy_end = yoy_cands[0][2]

    return cur_idx, yoy_idx, chosen_kind, max_end, yoy_end


def _clean_segment_stub_name(raw_stub: str) -> str:
    """R6: Parse clean segment display name from pipe-delimited SEC Details stub text."""
    parts = [p.strip() for p in raw_stub.split("|") if p.strip()]
    kept: list[str] = []
    for p in parts:
        cleaned = re.sub(r"\s*\[Member\]\s*$", "", p, flags=re.IGNORECASE).strip()
        if not cleaned or cleaned.lower() in ("operating segments", "segments", "business segments", "segment"):
            continue
        if cleaned not in kept:
            kept.append(cleaned)
    if not kept:
        return re.sub(r"\s*\[Member\]\s*$", "", raw_stub, flags=re.IGNORECASE).strip()
    return " / ".join(kept)


def _slugify_segment_key(name: str) -> str:
    low = name.lower()
    # Canonical short aliases for common US major bank segments
    alias_checks = (
        ("consumer & community banking", "ccb"),
        ("commercial & investment bank", "cib"),
        ("asset & wealth management", "awm"),
        ("consumer banking", "cb"),
        ("global wealth & investment management", "gwim"),
        ("global banking & markets", "gbm"),
        ("global banking", "gb"),
        ("global markets", "gm"),
        ("platform solutions", "ps"),
        ("consumer banking and lending", "cbl"),
        ("commercial banking", "cb_wfc"),
        ("wealth and investment management", "wim"),
        ("institutional securities", "is"),
        ("wealth management", "wm"),
        ("investment management", "im"),
        ("u.s. personal banking", "uspb"),
        ("services", "services"),
        ("markets", "markets"),
        ("banking", "banking"),
        ("wealth", "wealth"),
        ("all other", "all_other"),
        ("corporate", "corporate"),
    )
    for phrase, key in alias_checks:
        if phrase in low:
            return key
    slug = re.sub(r"[^a-z0-9]+", "_", low).strip("_")
    return slug[:40] or "segment"


def _classify_segment_section_role(
    raw_stub: str,
    clean_name: str,
    axis_member: str | None,
) -> Literal[
    "operating_segment",
    "corporate_or_other",
    "reconciling_or_elimination",
    "operating_segments_subtotal",
    "non_segment_disclosure",
]:
    ax = (axis_member or "").lower()
    low_clean = clean_name.lower()
    low_raw = re.sub(r"\s*\[member\]", "", raw_stub, flags=re.IGNORECASE).strip().lower()
    if "counterpartynameaxis" in ax or "disposalgroupclassificationaxis" in ax or "visamember" in ax:
        return "non_segment_disclosure"
    if re.search(r"^(?:reconciling\s+items?|intersegment\s+eliminations?|eliminations?)$", low_clean) or any(
        k in ax for k in ("materialreconcilingitemsmember", "intersegmenteliminationmember")
    ):
        return "reconciling_or_elimination"
    if "corporate/other" in low_clean or low_clean in ("corporate", "all other", "corporate & other", "corporate and other") or any(
        k in ax for k in ("corporatenonsegmentmember", "corporatereconcilingitemsandeliminationsmember", "corporateandother")
    ):
        # JPM TR[64] is 'Corporate'; BAC 'All Other'; WFC 'Corporate'
        if "reconcil" in low_clean or "eliminat" in low_clean:
            return "reconciling_or_elimination"
        return "corporate_or_other"
    if low_raw in ("operating segments", "reportable segments", "operating segments [member]"):
        return "operating_segments_subtotal"
    if "reconciling item" in low_clean or "intersegment elimination" in low_clean:
        return "reconciling_or_elimination"
    return "operating_segment"


def _extract_pnl_metrics_from_rows(
    rows: Sequence[FilingTableRow],
    *,
    col_idx: int,
    yoy_col_idx: int | None,
    table_mult: int | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def _pick(tag_pats: tuple[str, ...], label_pats: tuple[str, ...], *, exclude_label_re: re.Pattern[str] | None = None) -> tuple[Decimal | None, Decimal | None, bool]:
        # Pass 1: exact/priority XBRL tag match
        for pat in tag_pats:
            rx = re.compile(pat, re.IGNORECASE)
            for row in rows:
                if row.is_abstract or col_idx >= len(row.cells):
                    continue
                if exclude_label_re and exclude_label_re.search(row.label):
                    continue
                tag = row.xbrl_tag or ""
                if rx.search(tag):
                    cur = _scale_cell_money(row.cells[col_idx], table_mult)
                    if cur is not None:
                        pri = (
                            _scale_cell_money(row.cells[yoy_col_idx], table_mult)
                            if (yoy_col_idx is not None and yoy_col_idx < len(row.cells))
                            else None
                        )
                        is_fte = "fulltaxequivalent" in tag.lower() or "fte" in row.label.lower()
                        return cur, pri, is_fte
        # Pass 2: label regex match
        for pat in label_pats:
            rx = re.compile(pat, re.IGNORECASE)
            for row in rows:
                if row.is_abstract or col_idx >= len(row.cells):
                    continue
                if exclude_label_re and exclude_label_re.search(row.label):
                    continue
                if rx.search(row.label):
                    cur = _scale_cell_money(row.cells[col_idx], table_mult)
                    if cur is not None:
                        pri = (
                            _scale_cell_money(row.cells[yoy_col_idx], table_mult)
                            if (yoy_col_idx is not None and yoy_col_idx < len(row.cells))
                            else None
                        )
                        is_fte = "fte" in row.label.lower() or "tax-equivalent" in row.label.lower()
                        return cur, pri, is_fte
        return None, None, False

    def _pick_ratio(tag_pats: tuple[str, ...], label_pats: tuple[str, ...]) -> Decimal | None:
        for pat in tag_pats:
            rx = re.compile(pat, re.IGNORECASE)
            for row in rows:
                if row.is_abstract or col_idx >= len(row.cells):
                    continue
                if rx.search(row.xbrl_tag or "") and row.cells[col_idx].numeric_value is not None:
                    return normalize_sec_ratio_pct(row.cells[col_idx].numeric_value, raw_text=row.cells[col_idx].raw_text)
        for pat in label_pats:
            rx = re.compile(pat, re.IGNORECASE)
            for row in rows:
                if row.is_abstract or col_idx >= len(row.cells):
                    continue
                if rx.search(row.label) and row.cells[col_idx].numeric_value is not None:
                    return normalize_sec_ratio_pct(row.cells[col_idx].numeric_value, raw_text=row.cells[col_idx].raw_text)
        return None

    rev, rev_yoy, rev_fte = _pick(
        (
            r"RevenuesNetOfInterestExpense",
            r"RevenuesNetOfInterestExpenseFullTaxEquivalentBasis",
            r"^us-gaap_Revenues$",
            r"RevenueFromContractWithCustomerExcludingAssessedTax",
        ),
        (
            r"^(?:total\s+)?net\s+revenues?(?:\s*\(fte\))?$",
            r"^(?:total\s+)?revenues?,?\s*net\s+of\s+interest",
            r"^total\s+revenues?$",
        ),
        exclude_label_re=re.compile(r"intersegment|excluding|memo", re.IGNORECASE),
    )
    nii, _, _ = _pick(
        (r"InterestIncomeExpenseNet", r"InterestIncomeExpenseNetFullTaxEquivalentBasis"),
        (r"^net\s+interest\s+income(?:\s*\(fte\))?$",),
    )
    nonint_inc, _, _ = _pick(
        (r"^us-gaap_NoninterestIncome$", r"NoninterestRevenue"),
        (r"^noninterest\s+(?:income|revenue)$", r"^total\s+noninterest\s+income$"),
    )
    nonint_exp, _, _ = _pick(
        (r"^us-gaap_NoninterestExpense$",),
        (r"^(?:total\s+)?non[- ]?interest\s+expenses?$", r"^(?:total\s+)?operating\s+expenses?$"),
    )
    prov, _, _ = _pick(
        (r"ProvisionForLoanLeaseAndOtherLosses", r"ProvisionForCreditLosses"),
        (r"^provision\s+for\s+credit\s+losses", r"^provision\s+for\s+loan", r"credit\s+loss\s+provision"),
    )
    pbt, _, _ = _pick(
        (r"IncomeLossFromContinuingOperationsBeforeIncomeTaxes",),
        (r"income\s+before\s+income\s+tax", r"pre-?tax\s+(?:income|earnings)"),
    )
    # R6: Strict NetIncomeLoss priority over ContinuingOperationsIncludingPortionAttributableToNoncontrollingInterest!
    ni, _, _ = _pick(
        (
            r"^us-gaap_NetIncomeLoss$",
            r"^us-gaap_NetIncomeLossAvailableToCommonStockholdersBasic$",
            r"ProfitLoss$",
            r"IncomeLossFromContinuingOperationsIncludingPortionAttributableToNoncontrollingInterest",
        ),
        (r"^net\s+(?:income|earnings)(?:\s+applicable\s+to\s+common)?$",),
    )
    fte_adj, _, _ = _pick(
        (r"FullTaxEquivalentBasisAfterTax", r"TaxableEquivalentAdjustment"),
        (r"taxable[- ]equivalent\s+adjustment", r"fte\s+adjustment"),
    )
    assets, _, _ = _pick(
        (r"^us-gaap_Assets$",),
        (r"^(?:total|period-end)\s+assets$",),
    )
    avg_eq, _, _ = _pick(
        (r"AllocatedCapital", r"AverageCommonEquity", r"AverageStockholdersEquity"),
        (r"allocated\s+(?:common\s+)?equity", r"average\s+(?:common|allocated)\s+equity"),
    )
    roe = _pick_ratio(
        (r"ReturnOnAllocatedCapital", r"ReturnOnAverageCommonEquity", r"ReturnOnEquity"),
        (r"\broe\b|\brotce\b|return\s+on\s+(?:allocated|tangible\s+common|common)\s+equity",),
    )
    eff = _pick_ratio(
        (r"EfficiencyRatio", r"OverheadRatio"),
        (r"(?:efficiency|overhead)\s+ratio",),
    )

    # Derive rev from nii + nonint_inc if direct tag missing; derive PPNR when both rev & exp present
    if rev is None and nii is not None and nonint_inc is not None:
        rev = nii + nonint_inc
    if nonint_inc is None and rev is not None and nii is not None:
        nonint_inc = rev - nii
    ppnr = (rev - nonint_exp) if (rev is not None and nonint_exp is not None) else None
    if ppnr is not None and prov is None and pbt is not None:
        prov = ppnr - pbt
    if rev is not None and nonint_exp is not None and eff is None and rev > 0:
        eff = (nonint_exp / rev) * Decimal("100")

    out.update({
        "rev": rev,
        "rev_yoy": rev_yoy,
        "rev_fte": rev_fte,
        "nii": nii,
        "nonint_inc": nonint_inc,
        "nonint_exp": nonint_exp,
        "ppnr": ppnr,
        "prov": prov,
        "pbt": pbt,
        "ni": ni,
        "fte_adj": fte_adj,
        "assets": assets,
        "avg_eq": avg_eq,
        "roe": roe,
        "eff": eff,
    })
    return out


def extract_bank_segment_snapshot(
    table: FilingTableData,
    *,
    cik: str,
    symbol: str,
    accession: str,
    source_url: str,
    preferred_kind: Literal["3M", "9M", "12M"] = "3M",
) -> BankSegmentSnapshot | None:
    """R3-R7: Parse a SEC ``(Details)`` Business Segment table into zero-delta ``BankSegmentSnapshot``."""
    cur_idx, yoy_idx, chosen_kind, cur_end, yoy_end = _select_segment_columns(table, preferred_kind=preferred_kind)
    if cur_idx is None or cur_end is None:
        return None

    # Partition rows by tr.rh block headers
    block0_rows: list[FilingTableRow] = []
    blocks: list[tuple[str, str | None, list[FilingTableRow]]] = []
    cur_header: str | None = None
    cur_axis: str | None = None
    cur_rows: list[FilingTableRow] = []
    for row in table.rows:
        is_rh = bool(
            row.is_abstract
            and all(not c.raw_text for c in row.cells)
            and not row.label.rstrip().endswith(":")
        )
        if is_rh:
            if cur_header is not None:
                blocks.append((cur_header, cur_axis, cur_rows))
            cur_header = row.label
            cur_axis = row.xbrl_tag
            cur_rows = []
        else:
            if cur_header is None:
                block0_rows.append(row)
            else:
                cur_rows.append(row)
    if cur_header is not None:
        blocks.append((cur_header, cur_axis, cur_rows))

    # Extract Consolidated Block 0 metrics (or from operating_segments_subtotal when Block 0 lacks rev)
    b0 = _extract_pnl_metrics_from_rows(
        block0_rows, col_idx=cur_idx, yoy_col_idx=yoy_idx, table_mult=table.scale_multiplier,
    )
    consolidated_rev: Decimal | None = b0["rev"]
    consolidated_basis: Literal["gaap", "fte"] = "fte" if b0["rev_fte"] else "gaap"
    fte_adj: Decimal | None = b0["fte_adj"]

    op_segs: list[BankSegmentRow] = []
    corp_other: list[BankSegmentRow] = []
    reconciling: list[BankSegmentRow] = []
    warnings: list[str] = []
    seen_keys: set[str] = set()

    for raw_stub, axis_member, blk_rows in blocks:
        clean_name = _clean_segment_stub_name(raw_stub)
        role = _classify_segment_section_role(raw_stub, clean_name, axis_member)
        if role == "non_segment_disclosure":
            continue
        m = _extract_pnl_metrics_from_rows(
            blk_rows, col_idx=cur_idx, yoy_col_idx=yoy_idx, table_mult=table.scale_multiplier,
        )
        # Skip sparse 1-2 line fragment blocks (e.g., JPM TR[21]/24/28 holding only noninterest expense)
        if m["rev"] is None and m["ni"] is None:
            continue
        if role == "operating_segments_subtotal":
            if consolidated_rev is None and m["rev"] is not None:
                consolidated_rev = m["rev"]
            continue

        seg_key = _slugify_segment_key(clean_name)
        if seg_key in seen_keys:
            seg_key = f"{seg_key}_{len(seen_keys)}"
        seen_keys.add(seg_key)

        if fte_adj is None and m["fte_adj"] is not None:
            fte_adj = m["fte_adj"]

        rev_yoy_pct = (
            ((m["rev"] - m["rev_yoy"]) / abs(m["rev_yoy"])) * Decimal("100")
            if (m["rev"] is not None and m["rev_yoy"] is not None and abs(m["rev_yoy"]) > 0)
            else None
        )
        seg_row = BankSegmentRow(
            segment_key=seg_key,
            display_name=clean_name,
            section_role=role,
            xbrl_axis_member=axis_member,
            reporting_basis="managed_fte" if (m["rev_fte"] or role == "operating_segment" and (reconciling or consolidated_basis == "fte")) else "gaap",
            net_revenue_usd=_dstr(m["rev"]),
            nii_usd=_dstr(m["nii"]),
            noninterest_income_usd=_dstr(m["nonint_inc"]),
            noninterest_expense_usd=_dstr(m["nonint_exp"]),
            ppnr_usd=_dstr(m["ppnr"]),
            provision_usd=_dstr(m["prov"]),
            pretax_income_usd=_dstr(m["pbt"]),
            net_income_usd=_dstr(m["ni"]),
            avg_equity_or_tce_usd=_dstr(m["avg_eq"]),
            total_assets_usd=_dstr(m["assets"]),
            segment_roe_or_rotce_pct=_qstr(m["roe"], 2),
            segment_efficiency_pct=_qstr(m["eff"], 2),
            net_revenue_yoy_pct=_qstr(rev_yoy_pct, 2),
        )
        if role == "corporate_or_other":
            corp_other.append(seg_row)
        elif role == "reconciling_or_elimination":
            reconciling.append(seg_row)
        else:
            op_segs.append(seg_row)

    # Fallback: if no consolidated rev in Block 0, sum operating + corporate + reconciling
    sum_parts = (
        sum((_d(r.net_revenue_usd) or Decimal("0")) for r in op_segs)
        + sum((_d(r.net_revenue_usd) or Decimal("0")) for r in corp_other)
        + sum((_d(r.net_revenue_usd) or Decimal("0")) for r in reconciling)
    )
    if consolidated_rev is None:
        if not op_segs:
            return None
        consolidated_rev = sum_parts
        warnings.append("consolidated_revenue_inferred_from_segment_sum")

    # R5: Determine segments reporting basis (managed_fte when reconciling has FTE offset or Block 0 is FTE)
    has_managed_reconciling = any(
        r.net_income_usd is not None
        and _d(r.net_income_usd) == Decimal("0")
        and (_d(r.net_revenue_usd) or Decimal("0")) < 0
        and "eliminat" not in r.display_name.lower()
        for r in reconciling
    )
    fn_blob = " ".join(table.footnotes.values()).lower()
    segments_basis: Literal["gaap", "managed_fte"] = (
        "managed_fte"
        if (consolidated_basis == "fte" or has_managed_reconciling or "fully taxable" in fn_blob or "managed basis" in fn_blob)
        else "gaap"
    )
    if segments_basis == "managed_fte":
        op_segs = [r.model_copy(update={"reporting_basis": "managed_fte"}) for r in op_segs]
        if fte_adj is None and has_managed_reconciling:
            neg_reconc = sum(
                (_d(r.net_revenue_usd) or Decimal("0"))
                for r in reconciling
                if r.net_income_usd is not None
                and _d(r.net_income_usd) == Decimal("0")
                and "eliminat" not in r.display_name.lower()
            )
            if neg_reconc < 0:
                fte_adj = -neg_reconc

    # Compute revenue_share_pct across operating segments
    op_rev_total = sum((_d(r.net_revenue_usd) or Decimal("0")) for r in op_segs)
    if op_rev_total > 0:
        op_segs = [
            r.model_copy(
                update={
                    "revenue_share_pct": _qstr(((_d(r.net_revenue_usd) or Decimal("0")) / op_rev_total) * Decimal("100"), 2)
                }
            )
            for r in op_segs
        ]

    # R7: Zero-delta conservation gate
    delta = consolidated_rev - sum_parts
    abs_delta = abs(delta)
    if abs_delta == 0:
        rec_status: Literal["exact", "within_rounding", "mismatch"] = "exact"
    elif abs_delta <= Decimal("2000000"):
        rec_status = "within_rounding"
    else:
        rec_status = "mismatch"
        warnings.append(f"segment_reconciliation_mismatch_usd:{_dstr(delta)}")

    return BankSegmentSnapshot(
        cik=cik10(cik),
        symbol=symbol.upper(),
        accession=accession,
        r_file=table.r_file,
        source_url=source_url,
        period_kind=chosen_kind,
        period_end_date=cur_end.isoformat(),
        prior_yoy_period_end_date=yoy_end.isoformat() if yoy_end else None,
        consolidated_basis=consolidated_basis,
        segments_basis=segments_basis,
        consolidated_net_revenue_usd=_dstr(consolidated_rev) or "0",
        fte_reconciling_adjustment_usd=_dstr(fte_adj),
        operating_segments=op_segs,
        corporate_or_other=corp_other,
        reconciling_items=reconciling,
        reconciliation_delta_usd=_dstr(delta) or "0",
        reconciliation_status=rec_status,
        dropped_footnote_columns=table.dropped_footnote_columns,
        footnotes=dict(table.footnotes),
        warnings=warnings,
    )


# --------------------------------------------------------------------------- Tier B Parsers (R4 BS, RegCap R8, HTM Securities)

def parse_r4_balance_sheet_bank(table: FilingTableData) -> dict[str, Any]:
    """Parse ``R4.htm`` Balance Sheet strictly stopping before Consolidated VIEs supplemental block."""
    if table.currency and table.currency.upper() != "USD":
        return {}
    # Find newest instant column matching balance sheet date
    bs_col: int | None = None
    bs_end: date | None = None
    for col in table.columns:
        if col.period_end is None:
            continue
        cov = sum(1 for r in table.rows if not r.is_abstract and col.index < len(r.cells) and r.cells[col.index].numeric_value is not None)
        if cov < 5:
            continue
        if bs_end is None or col.period_end > bs_end:
            bs_end = col.period_end
            bs_col = col.index
    if bs_col is None or bs_end is None:
        return {}

    # Slice rows strictly up to 'Total liabilities and stockholders' equity' OR 'Variable interest entities' section!
    main_rows: list[FilingTableRow] = []
    for row in table.rows:
        low = row.label.lower()
        if re.search(r"consolidated\s+variable\s+interest\s+entities|variable\s+interest\s+entities\s*\(", low):
            break
        main_rows.append(row)
        if re.search(r"total\s+liabilities.*(?:stockholders|shareholders).*equity", low):
            break

    def _find_money(
        tag_pats: tuple[str, ...],
        label_pats: tuple[str, ...],
        *,
        exclude_re: re.Pattern[str] | None = None,
        fallback_note: str | None = None,
    ) -> Decimal | None:
        for pat in tag_pats:
            rx = re.compile(pat, re.IGNORECASE)
            for r in main_rows:
                if r.is_abstract or bs_col >= len(r.cells):
                    continue
                if exclude_re and exclude_re.search(r.label):
                    continue
                if rx.search(r.xbrl_tag or ""):
                    cell = r.cells[bs_col]
                    # A par-value annotation in the row label may be mistaken for
                    # the data cell's unit by the HTML parser. The statement's
                    # explicit money scale applies to the reported stock balance.
                    if "preferredstock" in pat.lower() and "par value" in r.label.lower() and cell.scale == "units" and table.scale_multiplier and cell.numeric_value is not None:
                        v = cell.numeric_value * Decimal(table.scale_multiplier)
                    else:
                        v = _scale_cell_money(cell, table.scale_multiplier, table.unit_header_note)
                    if v is not None:
                        return abs(v) if "allowance" in pat.lower() else v
        for pat in label_pats:
            rx = re.compile(pat, re.IGNORECASE)
            for r in main_rows:
                if r.is_abstract or bs_col >= len(r.cells):
                    continue
                if exclude_re and exclude_re.search(r.label):
                    continue
                if rx.search(r.label):
                    v = _scale_cell_money(r.cells[bs_col], table.scale_multiplier, table.unit_header_note)
                    if v is not None:
                        return v
        return None

    total_assets = _find_money((r"^us-gaap_Assets$",), (r"^total\s+assets$",))
    deposits = _find_money((r"^us-gaap_Deposits$",), (r"^total\s+deposits$", r"^deposits$"))
    loans_gross = _find_money(
        (r"FinancingReceivableExcludingAccruedInterestBeforeAllowanceForCreditLoss", r"LoansAndLeasesReceivableGrossCarryingAmount"),
        (r"^loans\s*(?:\(|net|\b)", r"^total\s+loans"),
        exclude_re=re.compile(r"allowance|held\s+for\s+sale|accrued|net\s+of\s+allowance|investment\s+securit", re.IGNORECASE),
    )
    acl = _find_money(
        (r"FinancingReceivableAllowanceForCreditLosses", r"LoansAndLeasesReceivableAllowance"),
        (r"^allowance\s+for\s+(?:loan|credit)\s+losses",),
        exclude_re=re.compile(r"net\s+of\s+allowance|investment\s+securit|debt\s+securit|loans,?\s*net", re.IGNORECASE),
    )
    if acl is not None:
        acl = abs(acl)
    if loans_gross is None:
        loans_net = _find_money(
            (r"LoansAndLeasesReceivableNetReportedAmount", r"FinancingReceivableExcludingAccruedInterestAfterAllowanceForCreditLoss"),
            (r"^loans,?\s*net",),
            exclude_re=re.compile(r"held\s+for\s+sale|accrued", re.IGNORECASE),
        )
        if loans_net is not None:
            loans_gross = loans_net + (acl or Decimal("0"))
    preferred = _find_money(
        (
            r"PreferredStockIncludingAdditionalPaidInCapital",
            r"PreferredStockLiquidationPreferenceValue",
            r"PreferredStockValue",
        ),
        (r"^preferred\s+stock",),
    )
    total_equity = _find_money(
        (r"^us-gaap_StockholdersEquity$", r"StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
        (r"^total\s+(?:[\w\s&.,'-]{0,40})?(?:stockholders|shareholders)['’]?\s+equity$",),
        exclude_re=re.compile(r"liabilities|noncontrolling", re.IGNORECASE),
    )
    common_equity = _find_money(
        (r"^us-gaap_CommonStockholdersEquity$",),
        (r"^total\s+common\s+(?:stockholders|shareholders)['’]?\s+equity$",),
    )
    if total_equity is None and common_equity is not None and preferred is not None:
        total_equity = common_equity + preferred
    if common_equity is None and total_equity is not None and preferred is not None:
        common_equity = total_equity - preferred
    goodwill = _find_money((r"^us-gaap_Goodwill$",), (r"^goodwill$",), exclude_re=re.compile(r"servicing|intangible", re.IGNORECASE))
    intangibles = _find_money(
        (r"^us-gaap_IntangibleAssetsNetExcludingGoodwill$", r"FiniteLivedIntangibleAssetsNet"),
        (r"^other\s+intangible\s+assets", r"^intangible\s+assets"),
        exclude_re=re.compile(r"goodwill", re.IGNORECASE),
    )
    msr = _find_money((r"ServicingAsset",), (r"^mortgage\s+servicing\s+rights",), exclude_re=re.compile(r"goodwill", re.IGNORECASE))
    if goodwill is not None and intangibles is None:
        gw_and_int = _find_money((), (r"^goodwill\s+and\s+(?:other\s+)?intangible",))
        if gw_and_int is not None and gw_and_int >= goodwill:
            intangibles = gw_and_int - goodwill

    # A monetary common-stock row often mentions outstanding share counts in
    # its caption. Its numeric cells still contain dollars, not shares.
    share_counts: set[Decimal] = set()
    for row in main_rows:
        tag = (row.xbrl_tag or "").lower()
        label = row.label.strip().lower()
        dedicated_row = bool(re.fullmatch(
            r"(?:number of )?common (?:stock )?shares outstanding(?:\s*\([^)]*\))?", label,
        ))
        if not (tag.endswith("commonstocksharesoutstanding") or (not tag and dedicated_row)):
            continue
        if bs_col >= len(row.cells):
            continue
        count = row.cells[bs_col].numeric_value
        unit_text = " ".join((label, table.unit_header_note or "", table.title)).lower()
        scales = set(re.findall(r"\bshares?\s*(?:in|\()\s*(millions?|thousands?|units?)\b", unit_text))
        if count is not None and count > 0 and len(scales) == 1:
            scale = scales.pop().rstrip("s")
            share_counts.add(count * {"million": 1_000_000, "thousand": 1_000, "unit": 1}[scale])
    shares_end = share_counts.pop() if len(share_counts) == 1 else None

    return {
        "period_end_date": bs_end,
        "total_assets": total_assets,
        "deposits": deposits,
        "loans_gross": loans_gross,
        "acl": acl,
        "preferred_stock": preferred,
        "total_equity": total_equity,
        "common_equity": common_equity,
        "goodwill": goodwill,
        "intangibles": intangibles,
        "msr": msr,
        "shares_ending_common": shares_end,
    }


def parse_r2_income_statement_bank(table: FilingTableData) -> dict[str, Any]:
    """Parse ``R2.htm`` Income Statement for 3M current GAAP metrics and optional YTD (for Citigroup TTM)."""
    res: dict[str, Any] = {}
    for pk in ("3M", "9M", "6M", "12M"):
        c_idx, _yoy_idx, chosen_k, c_end, _ = _select_segment_columns(table, preferred_kind=pk)  # type: ignore[arg-type]
        if c_idx is not None and c_end is not None and chosen_k == pk:
            m = _extract_pnl_metrics_from_rows(
                table.rows, col_idx=c_idx, yoy_col_idx=None, table_mult=table.scale_multiplier,
            )
            res[pk] = {"end": c_end, **m}
            if pk in ("9M", "6M"):
                # Also grab prior year same-duration YTD col if present in same R2 table!
                for col in table.columns:
                    if col.period_kind == pk and col.period_end is not None and 355 <= (c_end - col.period_end).days <= 375:
                        mp = _extract_pnl_metrics_from_rows(
                            table.rows, col_idx=col.index, yoy_col_idx=None, table_mult=table.scale_multiplier,
                        )
                        res[f"{pk}_prior"] = {"end": col.period_end, **mp}
                        break
    return res


def parse_reg_capital_details_table(tables: Sequence[FilingTableData]) -> dict[str, Any]:
    """R8 + R4b: Extract BHC-parent Basel III CET1 / Req Min / SCB / G-SIB / SLR / RWA from Details tables."""
    exclude_sub_re = re.compile(
        r"(?:bank,?\s*n\.?a\.?|national\s+association|reportable\s+legal\s+entit|depository\s+institution|bank\s+subsidiar)",
        re.IGNORECASE,
    )
    out: dict[str, Any] = {}

    for table in tables:
        # Pick newest valid period column (skip minimum requirement col separately!)
        best_col: int | None = None
        best_end: date | None = None
        min_req_col: int | None = None
        for col in table.columns:
            hl = col.header.lower()
            if "minimum" in hl or "required" in hl:
                if min_req_col is None:
                    min_req_col = col.index
                continue
            if col.period_end is not None and (best_end is None or col.period_end > best_end):
                best_end = col.period_end
                best_col = col.index
        if best_col is None and table.columns:
            best_col = 0

        cur_block = ""
        in_sub_bank = False
        cur_approach: str = ""
        for row in table.rows:
            is_rh = bool(
                row.is_abstract
                and all(not c.raw_text for c in row.cells)
                and not row.label.rstrip().endswith(":")
            )
            if is_rh:
                cur_block = row.label
                low_b = cur_block.lower()
                in_sub_bank = bool(exclude_sub_re.search(low_b))
                if "standardized" in low_b:
                    cur_approach = "standardized"
                elif "advanced" in low_b:
                    cur_approach = "advanced"
                else:
                    cur_approach = ""
                continue
            if in_sub_bank:
                continue
            if best_col is None or best_col >= len(row.cells):
                continue
            cell = row.cells[best_col]
            if cell.numeric_value is None:
                continue

            tag = (row.xbrl_tag or "").lower()
            lbl = row.label.lower()
            col_hdr_l = table.columns[best_col].header.lower() if best_col < len(table.columns) else ""
            is_req_context = any(
                k in s for s in (lbl, cur_block.lower(), tag, col_hdr_l)
                for k in ("minimum", "requirement", "capitaladequacyminimum", "requiredforcapitaladequacy")
            )

            # Check Regulatory Minimum column first if row has one
            if min_req_col is not None and min_req_col < len(row.cells) and row.cells[min_req_col].numeric_value is not None:
                if "common equity tier 1" in lbl or "cet1" in lbl or "common" in tag:
                    min_val = normalize_sec_ratio_pct(row.cells[min_req_col].numeric_value, raw_text=row.cells[min_req_col].raw_text)
                    if Decimal("4") <= min_val <= Decimal("25"):
                        if out.get("cet1_reg_min_pct") is None or (min_val > Decimal("4.5") and out["cet1_reg_min_pct"] <= Decimal("4.5")):
                            out["cet1_reg_min_pct"] = min_val

            # Ratios (R4b normalized)
            if "commonequitytieronecapitalratio" in tag or "tieronecommoncapitalratio" in tag or (
                ("cet1" in lbl or "common equity tier 1" in lbl) and ("ratio" in lbl or "%" in cell.raw_text or cell.numeric_value < Decimal("1"))
            ):
                ratio_val = normalize_sec_ratio_pct(cell.numeric_value, raw_text=cell.raw_text)
                if Decimal("4") <= ratio_val <= Decimal("35"):
                    if is_req_context:
                        # Prefer buffer-inclusive requirements (> 4.5%) over bare 4.5% Basel floor
                        if out.get("cet1_reg_min_pct") is None or (ratio_val > Decimal("4.5") and out["cet1_reg_min_pct"] <= Decimal("4.5")):
                            out["cet1_reg_min_pct"] = ratio_val
                    elif cur_approach == "advanced" or "advanced" in lbl:
                        out.setdefault("cet1_advanced_pct", ratio_val)
                    elif cur_approach == "standardized" or "standardized" in lbl or "cet1_standardized_pct" not in out:
                        out.setdefault("cet1_standardized_pct", ratio_val)
            elif ("stress" in lbl and "buffer" in lbl) or "stresscapitalbuffer" in tag or re.search(r"\bscb\b", lbl):
                scb = normalize_sec_ratio_pct(cell.numeric_value, raw_text=cell.raw_text)
                if Decimal("1") <= scb <= Decimal("15") and "advanced" not in lbl:
                    out.setdefault("cet1_scb_pct", scb)
            elif "g-sib" in lbl or "gsib" in lbl or "gsibsurcharge" in tag or "globalsystemicallyimportantbankgsibsurcharge" in tag:
                gsib = normalize_sec_ratio_pct(cell.numeric_value, raw_text=cell.raw_text)
                if Decimal("0.5") <= gsib <= Decimal("10") and "advanced" not in lbl:
                    out.setdefault("cet1_gsib_surcharge_pct", gsib)
            elif ("supplementary" in lbl and "leverage" in lbl) or "supplementaryleverageratio" in tag:
                slr = normalize_sec_ratio_pct(cell.numeric_value, raw_text=cell.raw_text)
                if Decimal("2") <= slr <= Decimal("20") and not is_req_context:
                    out.setdefault("slr_pct", slr)
            elif "tieroneleverage" in tag or ("tier 1 leverage" in lbl and "ratio" in lbl):
                t1 = normalize_sec_ratio_pct(cell.numeric_value, raw_text=cell.raw_text)
                if Decimal("2") <= t1 <= Decimal("25") and not is_req_context:
                    out.setdefault("tier1_leverage_pct", t1)
            elif ("commonequitytieronecapital" in tag or tag.endswith("commonequitytier1capital")) and "ratio" not in tag and "minimum" not in tag:
                out.setdefault("cet1_capital_usd", _scale_cell_money(cell, table.scale_multiplier, table.unit_header_note))
            elif (
                ("riskweightedassets" in tag or "risk-weighted assets" in lbl)
                and "ratio" not in tag
                and "ratio" not in lbl
                and "percent" not in tag
                and "%" not in cell.raw_text
                and "as a %" not in lbl
                and not is_req_context
            ):
                v_rwa = _scale_cell_money(cell, table.scale_multiplier, table.unit_header_note)
                if v_rwa is not None and v_rwa >= Decimal("1000000000"):
                    if cur_approach == "standardized" or "cet1_rwa" not in out:
                        out["cet1_rwa"] = v_rwa

    if out.get("cet1_scb_pct") is not None:
        derived_min = Decimal("4.5") + out["cet1_scb_pct"] + (out.get("cet1_gsib_surcharge_pct") or Decimal("0"))
        if out.get("cet1_reg_min_pct") is None or out["cet1_reg_min_pct"] <= Decimal("4.5"):
            out["cet1_reg_min_pct"] = derived_min
    return out


def parse_htm_securities_details(tables: Sequence[FilingTableData]) -> dict[str, Any]:
    """Extract Total Held-to-Maturity Amortized Cost, Fair Value & Gross Unrealized Gain/Loss from Details R*.htm."""
    out: dict[str, Any] = {}
    for table in tables:
        col_idx: int | None = None
        best_end: date | None = None
        # Look for separate columns ("Amortized Cost", "Fair Value", "Gross Unrealized Gains/Losses") OR row-based HTM lines
        ac_col = fv_col = gg_col = gl_col = None
        for col in table.columns:
            hl = col.header.lower()
            if "amortized cost" in hl:
                ac_col = col.index
            elif "fair value" in hl:
                fv_col = col.index
            elif "unrealized gain" in hl:
                gg_col = col.index
            elif "unrealized loss" in hl:
                gl_col = col.index
            if col.period_end is not None and (best_end is None or col.period_end > best_end):
                best_end = col.period_end
                col_idx = col.index

        in_htm_block = False
        for row in table.rows:
            if row.is_abstract:
                low_b = (row.label + " " + (row.xbrl_tag or "")).lower()
                in_htm_block = any(k in low_b for k in ("heldtomaturity", "held-to-maturity", "held to maturity"))
                continue
            low = row.label.lower()
            tag = (row.xbrl_tag or "").lower()
            is_total_htm = (
                in_htm_block and row.label.lower().startswith("total")
            ) or "held-to-maturity securities" in low or "total held-to-maturity" in low

            if ac_col is not None and fv_col is not None and is_total_htm:
                if ac_col < len(row.cells) and fv_col < len(row.cells):
                    ac = _scale_cell_money(row.cells[ac_col], table.scale_multiplier, table.unit_header_note)
                    fv = _scale_cell_money(row.cells[fv_col], table.scale_multiplier, table.unit_header_note)
                    if ac and fv:
                        out["htm_amortized_cost_usd"] = ac
                        out["htm_fair_value_usd"] = fv
                        if gg_col is not None and gg_col < len(row.cells):
                            out["htm_gross_unrealized_gain_usd"] = _scale_cell_money(row.cells[gg_col], table.scale_multiplier, table.unit_header_note)
                        if gl_col is not None and gl_col < len(row.cells):
                            out["htm_gross_unrealized_loss_usd"] = _scale_cell_money(row.cells[gl_col], table.scale_multiplier, table.unit_header_note)
                        break

            # Row-based HTM summary (common in SEC Details where Amortized Cost and Fair Value are separate rows)
            if col_idx is not None and col_idx < len(row.cells) and (in_htm_block or "heldtomaturity" in tag or "held-to-maturity" in low):
                val = _scale_cell_money(row.cells[col_idx], table.scale_multiplier, table.unit_header_note)
                if val is None:
                    continue
                if (
                    "amortized cost" in low
                    or "carrying" in low
                    or tag.endswith("heldtomaturitysecurities")
                    or "amortizedcost" in tag
                    or (
                        "debtsecuritiesheldtomaturity" in tag
                        and "allowance" not in low
                        and "credit loss" not in low
                        and not any(x in tag for x in ("fairvalue", "maturityallowance", "heldtomaturityallowance", "allowanceforcreditlossexcluding", "holdinggain", "holdingloss", "unrealizedgain", "unrealizedloss", "yield", "restricted"))
                    )
                ) and "fair" not in low:
                    out.setdefault("htm_amortized_cost_usd", val)
                elif "fair value" in low or "fairvalue" in tag:
                    out.setdefault("htm_fair_value_usd", val)
                elif "allowance" in low or "credit loss" in low:
                    out.setdefault("htm_acl_usd", abs(val))
                elif "net unrealized" in low:
                    out.setdefault("htm_net_unrealized_pnl_usd", val)

        if out.get("htm_amortized_cost_usd") and out.get("htm_fair_value_usd"):
            break

    ac_v = out.get("htm_amortized_cost_usd")
    fv_v = out.get("htm_fair_value_usd")
    if ac_v is not None and fv_v is not None:
        out["htm_net_unrealized_pnl_usd"] = fv_v - ac_v
    return out


# --------------------------------------------------------------------------- Tier C Extractors (8-K Tables + Streaming 10-Q MD&A)

def _bank_metric_unit_evidence(key: str, raw: str, marker: str, scope: str) -> dict[str, str] | None:
    """Bind units to this cell, retaining the disclosure text used to establish them."""
    text = " ".join((marker, raw)).strip()
    scope = re.sub(r"\s+", " ", scope).strip()
    if not re.fullmatch(r"\s*(?:(?:USD|US\$|\$)\s*)?\(?[+-]?[\d,]+(?:\.\d+)?\)?\s*%?\s*", text, re.I):
        return None
    if key not in ("tbvps_usd", "bvps_usd"):
        if re.search(r"\$|\bUSD\b", text, re.I):
            return None
        if "%" in text:
            return {"unit": "percent", "unit_evidence": text, "unit_scope": "cell"}
        if re.search(r"(?i)\bpercent(?:age)?s?\b|%", scope) and not re.search(r"(?i)\bexcept\s+(?:for\s+)?(?:percent|percentage)", scope):
            return {"unit": "percent", "unit_evidence": scope, "unit_scope": "table"}
        return None
    if "%" in text or re.search(r"(?i)\bpercent(?:age)?s?\b|%", scope):
        return None
    combined = " ".join((scope, text))
    if re.search(r"(?i)\b(?:EUR|GBP|CAD|JPY|CHF|AUD|NZD|CNY|RMB|HKD|SGD|KRW|INR|SEK|NOK|DKK|BRL|MXN|ZAR|euros?|pounds?|yen|yuan|sterling|(?:canadian|australian|hong kong) dollars?)\b|[€£¥₩]", combined):
        return None
    if (re.search(r"\b(?i:currency|in)\s+(?!USD\b)[A-Z]{3}\b", scope)
            or re.search(r"(?i)\bcurrency\s+(?:unknown|unspecified|undisclosed)\b", scope)):
        return None
    scale = re.search(r"(?i)\b(?:in|\$\s*in|usd\s*in)\s+(?:the\s+)?(thousands?|millions?|billions?)\b|\b(thousands?|millions?|billions?)\s+of\s+(?:usd|dollars?)\b", scope)
    except_share = re.search(r"(?i)\bexcept\s+(?:for\s+)?(?:per[ -]share|share\s+and\s+per[ -]share)\b", scope)
    if scale and not except_share:
        return None
    cell_currency = bool(re.search(r"(?i)\$|\bUSD\b", text))
    scope_currency = bool(re.search(r"(?i)\b(?:USD|US\s+dollars?|dollars?)\b|\$", scope))
    if not (cell_currency or scope_currency):
        return None
    return {"unit": "USD/share", "unit_evidence": text if cell_currency else scope,
            "unit_scope": "cell" if cell_currency else "table", "scale_evidence": scale.group(0) if scale else "unscaled per share"}


_BANK_QUARTER_PATTERN = r"\b(?:[1-4]\s*q|q\s*[1-4])\s*['’]?\s*(?:\d{4}|\d{2})\b"
_BANK_QUARTER_TOKEN = re.compile(_BANK_QUARTER_PATTERN, re.I)
_BANK_DATE_PATTERN = r"\b(?:[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{4})\b"
_BANK_PERIOD_TOKEN = re.compile(rf"{_BANK_QUARTER_PATTERN}|{_BANK_DATE_PATTERN}|\b(?:h[12]|[12]h|fy|ytd)\s*['’]?\s*(?:\d{{4}}|\d{{2}})\b", re.I)
_BANK_DURATION = re.compile(r"\b(?:quarters?|years?|annual|year.to.date|ytd|(?:three|six|nine|twelve|3|6|9|12)\s+months?)\s*(?:ended|ending|to.date)?\b", re.I)
_BANK_NONQUARTER = re.compile(r"\b(?:annual|years?|year.to.date|ytd|(?:six|nine|twelve|6|9|12)\s+months?)\b", re.I)
# Common within-table metric group labels do not introduce new columns.
# Other standalone headings are boundaries unless followed by a new period row.
_BANK_METRIC_GROUP = re.compile(
    r"^(?:per (?:common )?share|(?:average )?common shares outstanding(?: \(in (?:thousands|millions)\))?|"
    r"balance sheet averages|ratios and other metrics|"
    r"(?:consolidated )?(?:regulatory )?capital ratios|"
    r"(?:average )?tangible common (?:shareholders'? )?equity(?:\s*[-–—]\s*(?:period end|average))?|"
    r"less|plus)\s*:\s*(?:\([a-z0-9]+\))?$", re.I)


def _bank_target_quarter(cell: str, target_end: date) -> bool:
    if not _BANK_QUARTER_TOKEN.fullmatch(cell.strip()):
        return False
    compact = re.sub(r"[\s'’]", "", cell).lower()
    q = (target_end.month - 1) // 3 + 1
    return compact in {f"{q}q{target_end.year}", f"q{q}{target_end.year}",
                       f"{q}q{target_end.year % 100:02d}", f"q{q}{target_end.year % 100:02d}"}


def _period_table_metrics(period: EarningsReleasePeriod | None, target_end: date) -> dict[str, Any]:
    """Extract exact-period consolidated metrics; ambiguous layouts remain unavailable."""
    if period is None or not (target_end <= period.filed_at and (period.filed_at - target_end).days <= 90):
        return {}
    month_names = (target_end.strftime("%B"), target_end.strftime("%b"))
    date_re = re.compile(rf"^(?:{'|'.join(month_names)}|sept)\.?\s+{target_end.day},?\s+{target_end.year}$", re.I) if target_end.month == 9 else re.compile(rf"^(?:{'|'.join(month_names)})\.?\s+{target_end.day},?\s+{target_end.year}$", re.I)
    out: dict[str, Any] = {}
    conflicts: set[str] = set()
    for ex in (period.ex99_1, period.ex99_2, period.ex99_3):
        if ex is None or ex.is_image_deck:
            continue
        for tbl in ex.tables:
            context = " ".join([tbl.title or "", *tbl.headers, *(r[0] for r in tbl.rows[:8] if r)]).lower()
            if not any(x in context for x in ("consolidated", "firmwide", "reconciliations of tangible", "net income applicable to common shareholders of")):
                continue
            if any(x in context for x in ("business segment", "by segment")):
                continue
            header = None
            col = 0
            is_quarter = False
            duration_groups: list[str] = []
            for row in [tbl.headers, *tbl.rows]:
                # A single HTML table may contain multiple differently ordered sections.
                # A new period header replaces, or invalidates, the preceding column map.
                # Period-like headers always replace the prior binding, even when
                # their period is unsupported. This prevents a later section from
                # inheriting a column position from an earlier one.
                cells = [cell.strip() for cell in row[1:]]
                period_header = any(_BANK_PERIOD_TOKEN.search(cell) for cell in cells)
                year_count = sum(bool(re.fullmatch(r"(?:19|20)\d{2}", cell)) for cell in cells)
                year_header = year_count >= 2 or (year_count == 1 and bool(row and re.search(r"(?i)\b(?:year|annual)\b", row[0])))
                section_header = bool(row and _BANK_DURATION.search(" ".join(row)))
                if period_header or year_header or section_header:
                    matches = [(i, _bank_target_quarter(cell, target_end)) for i, cell in enumerate(row[1:], 1)
                               if _bank_target_quarter(cell, target_end) or date_re.fullmatch(cell.strip())]
                    # Explicit annual/YTD durations cannot be treated as quarterly
                    # observations, even if a column also bears a quarter label.
                    nonquarter = _BANK_NONQUARTER.search(" ".join(row))
                    if section_header and not period_header and not year_header:
                        # Carry grouped duration headings to their following date or
                        # quarter row, preserving the scope of each column.
                        duration_groups = []
                        group = row[0] if row else ""
                        for cell in row:
                            if cell.strip():
                                group = cell
                            duration_groups.append(group)
                    if len(matches) == 1:
                        match_col, match_quarter = matches[0]
                        group = duration_groups[match_col] if match_col < len(duration_groups) else ""
                        group_nonquarter = _BANK_NONQUARTER.search(group)
                        header = row if not nonquarter and not group_nonquarter else None
                        if header is not None:
                            col, is_quarter = match_col, match_quarter
                        duration_groups = []
                    else:
                        header = None
                        if period_header or year_header:
                            duration_groups = []
                    continue
                # A standalone section label is a boundary, not a data row.
                if row and row[0].strip() and not any(c.strip() for c in row[1:]):
                    if not _BANK_METRIC_GROUP.fullmatch(row[0].strip()):
                        header = None
                        duration_groups = []
                    continue
                if header is None:
                    continue
                if col >= len(row) or not row:
                    continue
                label = re.sub(r"\s*\((?:[a-z]|\d+)\)\s*$", "", row[0].strip().lower())
                key = None
                if re.fullmatch(r"tangible book value per (?:common )?share(?:\s*[-–—]\s*non-gaap)?", label):
                    key = "tbvps_usd"
                elif re.fullmatch(r"book value per (?:common )?share(?:\s*[-–—]\s*gaap)?", label):
                    key = "bvps_usd"
                elif is_quarter and re.fullmatch(r"return on (?:average )?tangible common equity(?:\s*[-–—]\s*non-gaap)?", label):
                    key = "rotce_q_pct"
                elif re.fullmatch(r"(?:standardized (?:common equity tier 1|cet1)(?: capital)? ratio|(?:common equity tier 1|cet1)(?: capital)? ratio \(standardized\))", label):
                    key = "cet1_std_pct"
                elif is_quarter and re.fullmatch(r"(?:net interest (?:margin|yield)|nim) \(fte\)(?:\s*[-–—]\s*non-gaap)?", label):
                    key = "nim_fte_pct"
                elif is_quarter and re.fullmatch(r"(?:efficiency|overhead) ratio(?:\s*[-–—]\s*(?:gaap|non-gaap))?", label):
                    key = "efficiency_pct"
                if key is None:
                    continue
                raw_value = row[col]
                marker = ""
                value = _d(re.sub(r"(?i)^\s*(?:USD|US\$)\s*", "", raw_value))
                # Currency symbols may occupy a separate cell in HTML tables.
                if (value is None and raw_value.strip() in ("", "$", "US$", "USD")
                        and col + 1 < len(row) and col + 1 < len(header) and not header[col + 1].strip()):
                    marker, raw_value = raw_value, row[col + 1]
                    value = _d(re.sub(r"(?i)^\s*(?:USD|US\$)\s*", "", raw_value))
                scope = " ".join([tbl.title or "", tbl.headers[0] if tbl.headers else "",
                                  tbl.headers[col] if col < len(tbl.headers) else "",
                                  header[0] if header else "", header[col], row[0]])
                unit_evidence = _bank_metric_unit_evidence(key, raw_value, marker, scope)
                if value is None or unit_evidence is None:
                    continue
                if key in out and out[key] != value:
                    conflicts.add(key)
                elif key not in out:
                    out[key] = value
                    out[key + "_provenance"] = {
                        "source": "8k_supplement", "accession": period.accession, "url": ex.url,
                        "exhibit": ex.exhibit_type, "period_end": target_end.isoformat(),
                        "column": header[col], "row": row[0], "raw_value": raw_value,
                        **unit_evidence,
                    }
    for key in conflicts:
        del out[key], out[key + "_provenance"]
    return out


def _extract_8k_supplement_metrics(period: EarningsReleasePeriod | None, target_end: date | None = None) -> dict[str, Any]:
    """Only return period-aligned metrics; unverified legacy heuristics are ineligible."""
    if period is None:
        return {}
    result = _period_table_metrics(period, target_end) if target_end is not None else {}
    result["exhibits_used"] = list(dict.fromkeys(v["exhibit"] for k, v in result.items() if k.endswith("_provenance")))
    return result


def scan_10q_mda_bank_disclosures_from_text(clean_text: str) -> dict[str, Any]:
    """Extract GS TBVPS/ROTE & BAC/C SCB/G-SIB/CET1 min requirement from 10-Q MD&A text (supports % and 'percent')."""
    out: dict[str, Any] = {}
    if not clean_text:
        return out
    m_tbv = re.search(
        r"tangible\s+book\s+value\s+per\s+(?:common\s+)?share\s*\$?\s*([0-9,]+\.\d{2})",
        clean_text,
        re.IGNORECASE,
    )
    if m_tbv:
        v = _d(m_tbv.group(1))
        if v and Decimal("1") <= v <= Decimal("2000"):
            out["tbvps_reported_usd"] = v
    m_bv = re.search(
        r"book\s+value\s+per\s+(?:common\s+)?share\s*\$?\s*([0-9,]+\.\d{2})",
        clean_text,
        re.IGNORECASE,
    )
    if m_bv:
        v = _d(m_bv.group(1))
        if v and Decimal("1") <= v <= Decimal("2500"):
            out["bvps_reported_usd"] = v
    m_tce = re.search(
        r"tangible\s+common\s+(?:shareholders['’]?|stockholders['’]?)\s+equity\s*\$?\s*([0-9,]{4,})",
        clean_text,
        re.IGNORECASE,
    )
    if m_tce:
        v = _d(m_tce.group(1))
        if v and v > Decimal("1000"):
            out["tce_reported_usd"] = v * Decimal("1000000")
    m_rote = re.search(
        r"\bROT[CE]{1,2}\b[^0-9\n]{0,20}([0-9]{1,2}\.\d{1,2})\s*(?:%|percent\b)",
        clean_text,
        re.IGNORECASE,
    )
    if m_rote:
        v = _d(m_rote.group(1))
        if v and Decimal("-50") <= v <= Decimal("60"):
            out["rotce_q_reported_pct"] = v

    # BAC / C 10-Q CET1 minimum requirement & SCB wording ('%' or 'percent')
    m_min = re.search(
        r"(?:(?:standardized\s+)?CET1\s+(?:Capital\s+)?(?:minimum\s+)?(?:ratio\s+)?requirement(?:s)?|Common\s+equity\s+tier\s+1\s+\(CET1\)\s+minimum\s+requirement)"
        r"[^0-9\n]{0,30}(\d{1,2}\.\d{1,2})\s*(?:%|percent)",
        clean_text,
        re.IGNORECASE,
    )
    if m_min:
        v = _d(m_min.group(1))
        if v and Decimal("4.5") <= v <= Decimal("25"):
            out["cet1_reg_min_pct"] = v
    m_scb = re.search(
        r"(?:\bSCB\b|stress\s+capital\s+buffer)[^0-9%\n]{0,20}(\d\.\d{1,2})\s*(?:%|percent)",
        clean_text,
        re.IGNORECASE,
    )
    if m_scb:
        v = _d(m_scb.group(1))
        if v and Decimal("1") <= v <= Decimal("15"):
            out["cet1_scb_pct"] = v
    m_gsib = re.search(
        r"(?:method\s+[12]\s+)?G-?SIB\s+(?:capital\s+)?surcharge[^0-9%\n]{0,20}(\d\.\d{1,2})\s*(?:%|percent)",
        clean_text,
        re.IGNORECASE,
    )
    if m_gsib:
        v = _d(m_gsib.group(1))
        if v and Decimal("0.5") <= v <= Decimal("10"):
            out["cet1_gsib_surcharge_pct"] = v
    m_cet1_mda = re.search(
        r"(?:standardized\s+)?cet1\s+(?:capital\s+)?ratio\s+(?:was|of)\s*(\d{1,2}\.\d{1,2})\s*(?:%|percent)",
        clean_text,
        re.IGNORECASE,
    )
    if m_cet1_mda:
        v = _d(m_cet1_mda.group(1))
        if v and Decimal("5") <= v <= Decimal("30"):
            out["cet1_standardized_pct"] = v
    m_cover_sh = re.search(
        r"([0-9][0-9,]{5,})\s+shares\s+of\s+common\s+stock(?:\s+of[^.\n]{0,60})?\s+(?:were\s+)?outstanding",
        clean_text,
        re.IGNORECASE,
    )
    if m_cover_sh:
        v = _d(m_cover_sh.group(1))
        if v and v > Decimal("10000000"):
            out["cover_common_shares_outstanding"] = v
    return out


def stream_scan_10q_mda_for_bank_gaps(
    doc_url: str,
    *,
    user_agent: str,
    limiter: SecPacingLimiter,
    policy: Any | None = None,
    transport: Any | None = None,
    stream_transport: Any | None = None,
) -> dict[str, Any]:
    """Stream 10-Q/10-K primary HTML in 256KB chunks via ``sec_get_to_tempfile()`` without loading 15MB into RAM."""
    tmp_path, _ct, _sha = sec_get_to_tempfile(
        doc_url,
        category="filings",
        user_agent=user_agent,
        limiter=limiter,
        policy=policy,
        transport=transport,
        stream_transport=stream_transport,
        max_bytes=60_000_000,
        suffix=".htm",
    )
    merged: dict[str, Any] = {}
    try:
        carry = ""
        with open(tmp_path, "r", encoding="utf-8", errors="ignore") as fh:
            while True:
                chunk = fh.read(262_144)
                if not chunk:
                    break
                text = carry + re.sub(
                    r"\s+",
                    " ",
                    re.sub(r"&nbsp;|&#160;|&#8217;", " ", re.sub(r"<[^>]+>", " ", chunk)),
                )
                found = scan_10q_mda_bank_disclosures_from_text(text)
                for k, v in found.items():
                    merged.setdefault(k, v)
                carry = text[-1200:]
                if all(k in merged for k in ("tbvps_reported_usd", "rotce_q_reported_pct", "cet1_reg_min_pct")):
                    break
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return merged


# --------------------------------------------------------------------------- Main Orchestrated Three-Source Loader

def _parse_r5_parenthetical(table: FilingTableData | None) -> dict[str, Decimal | None]:
    """Extract ending common shares (Issued - Treasury) and optional HTM FV / Amortized Cost from Parenthetical BS."""
    if table is None:
        return {}
    bs_col: int | None = None
    bs_end: date | None = None
    for col in table.columns:
        if col.period_end is None:
            continue
        if bs_end is None or col.period_end > bs_end:
            bs_end = col.period_end
            bs_col = col.index
    if bs_col is None:
        return {}

    issued: Decimal | None = None
    treasury: Decimal | None = None
    outstanding: Decimal | None = None
    htm_fv: Decimal | None = None
    htm_ac: Decimal | None = None
    for row in table.rows:
        if row.is_abstract or bs_col >= len(row.cells):
            continue
        cell = row.cells[bs_col]
        if cell.numeric_value is None:
            continue
        tag = (row.xbrl_tag or "").lower()
        lbl = row.label.lower()
        if "commonstocksharesoutstanding" in tag or ("common stock" in lbl and "outstanding" in lbl and "treasury" not in lbl):
            sc = 1 if cell.numeric_value > Decimal("100000000") else (table.scale_multiplier or 1_000_000)
            outstanding = cell.numeric_value * Decimal(sc)
        elif "commonstocksharesissued" in tag or ("common stock" in lbl and "issued" in lbl):
            sc = 1 if cell.numeric_value > Decimal("100000000") else (table.scale_multiplier or 1_000_000)
            issued = cell.numeric_value * Decimal(sc)
        elif "treasurystockcommonshares" in tag or ("treasury stock" in lbl and "share" in lbl):
            sc = 1 if cell.numeric_value > Decimal("100000000") else (table.scale_multiplier or 1_000_000)
            treasury = abs(cell.numeric_value) * Decimal(sc)
        elif "heldtomaturitysecuritiesfairvalue" in tag or ("held-to-maturity" in lbl and "fair value" in lbl):
            htm_fv = _scale_cell_money(cell, table.scale_multiplier, table.unit_header_note)
        elif "debtsecuritiesheldtomaturity" in tag and ("amortized" in lbl or "afterallowance" in tag) and "fair value" not in lbl:
            htm_ac = _scale_cell_money(cell, table.scale_multiplier, table.unit_header_note)

    if outstanding is None and issued is not None and treasury is not None and issued > treasury:
        outstanding = issued - treasury
    return {
        "shares_ending_common": outstanding,
        "htm_fair_value_usd": htm_fv,
        "htm_amortized_cost_usd": htm_ac,
    }


def _find_best_catalog_entry(
    catalog: FilingTableCatalog,
    *,
    kind: Literal["income", "balance", "balance_parenthetical", "segments", "regcap_ratio", "regcap_req", "htm"],
) -> FilingReportEntry | None:
    if kind == "income":
        for r in catalog.statements:
            sn = (r.short_name + " " + r.long_name).lower()
            if any(k in sn for k in ("operations", "income", "earnings")) and "comprehensive" not in sn:
                return r
        return None
    if kind == "balance":
        for r in catalog.statements:
            sn = (r.short_name + " " + r.long_name).lower()
            if ("balance sheet" in sn or "financial condition" in sn or "financial position" in sn) and "parenthetical" not in sn:
                return r
        return None
    if kind == "balance_parenthetical":
        for r in catalog.statements:
            sn = (r.short_name + " " + r.long_name).lower()
            if ("balance sheet" in sn or "financial condition" in sn or "financial position" in sn) and "parenthetical" in sn:
                return r
        return None
    if kind == "segments":
        # Priority 1: SEC machine-generated Details summary table (exclude narrative/goodwill/loans/sub-tables)
        seg_excl_re = re.compile(
            r"narrative|goodwill|intangible|loan|allowance|credit|reconcil.*total\s+asset|"
            r"by\s+geographic|asset.*by|depreciation|revenue\s+and\s+expenses|revenue\s+by|"
            r"receivable|\btax|components\s+of\s+other|trading\s+revenues|carried\s+interest|fee\s+waivers",
            re.IGNORECASE,
        )
        detail_cands = [
            r for r in catalog.all_reports
            if (r.normalized_category == "Details" or "(detail" in r.short_name.lower())
            and any(k in (r.short_name + " " + r.long_name).lower() for k in ("segment", "line of business", "lines of business"))
            and not seg_excl_re.search(r.short_name)
        ]
        detail_cands.sort(
            key=lambda r: (
                -int(any(k in r.short_name.lower() for k in ("result", "operations", "summary", "selected financial", "profitability", "overall"))),
                int("parenthetical" in r.short_name.lower() or "reconcil" in r.short_name.lower()),
                r.position or 999,
            )
        )
        if detail_cands:
            return detail_cands[0]
        table_cands = [
            r for r in catalog.tables
            if any(k in (r.short_name + " " + r.long_name).lower() for k in ("segment", "line of business"))
        ]
        return table_cands[0] if table_cands else None
    if kind == "regcap_ratio":
        cands = [
            r for r in catalog.all_reports
            if (r.normalized_category == "Details" or "(detail" in r.short_name.lower())
            and any(k in (r.short_name + " " + r.long_name).lower() for k in ("regulatory capital", "capital ratio", "capital metrics", "basel", "firm's regulatory capital", "regulatory requirements"))
            and not any(bad in r.short_name.lower() for bad in ("subsidiar", "broker-dealer", "requirement", "buffer", "restriction", "cash", "narrative"))
        ]
        cands.sort(key=lambda r: (0 if any(k in r.short_name.lower() for k in ("metric", "ratio", "information", "firm")) else 1, r.position or 999))
        return cands[0] if cands else None
    if kind == "regcap_req":
        cands = [
            r for r in catalog.all_reports
            if (r.normalized_category == "Details" or "(detail" in r.short_name.lower())
            and any(k in (r.short_name + " " + r.long_name).lower() for k in ("capital requirement", "capital and leverage requirement", "capital ratio requirement", "capital buffer", "capital component", "minimum required regulatory capital", "regulatory requirements"))
            and not any(bad in r.short_name.lower() for bad in ("subsidiar", "broker-dealer", "narrative"))
        ]
        cands.sort(key=lambda r: (0 if ("buffer" in r.short_name.lower() or "minimum" in r.short_name.lower() or "leverage requirement" in r.short_name.lower()) else 1, r.position or 999))
        return cands[0] if cands else None
    if kind == "htm":
        cands = [
            r for r in catalog.all_reports
            if (r.normalized_category == "Details" or "(detail" in r.short_name.lower())
            and (
                re.search(r"\bhtm\b", r.short_name + " " + r.long_name, re.IGNORECASE)
                or any(k in (r.short_name + " " + r.long_name).lower() for k in ("held-to-maturity", "held to maturity", "investment securities", "debt securities"))
            )
            and any(k in (r.short_name + " " + r.long_name).lower() for k in ("amortized", "fair value", "outstanding", "afs and htm", "held-to-maturity", "debt securities"))
            and not any(bad in r.short_name.lower() for bad in ("contractual maturit", "maturities of", "aging", "narrative", "realized", "credit quality", "purchases", "pledged", "transfer"))
        ]
        cands.sort(
            key=lambda r: (
                0 if ("outstanding" in r.short_name.lower() or "amortized costs and estimated fair values" in r.short_name.lower() or "afs and htm" in r.short_name.lower() or r.short_name.lower().endswith("debt securities (details)") or "debt securities held-to-maturity" in r.short_name.lower()) else 1,
                0 if (re.search(r"\bhtm\b", r.short_name, re.IGNORECASE) or "held-to-maturity" in r.short_name.lower() or "held to maturity" in r.short_name.lower()) else 2,
                r.position or 999,
            )
        )
        return cands[0] if cands else None
    return None


def fetch_bank_disclosure_snapshot(
    *,
    snap: Any,
    user_agent: str,
    limiter: SecPacingLimiter,
    bank_cache: Any | None = None,
    submissions: Mapping[str, Any] | Any | None = None,
    as_of: date | None = None,
    cache_only: bool = False,
    policy: Any | None = None,
    transport: Any | None = None,
    stream_transport: Any | None = None,
    preloaded_earnings_release: DualPeriodEarningsRelease | None = None,
    preloaded_10q_text: str | None = None,
    preloaded_catalog: FilingTableCatalog | None = None,
    force_refresh: bool = False,
) -> tuple[BankDisclosureSnapshot | None, bool]:
    """Build or load cached ``BankDisclosureSnapshot`` combining XBRL + 10-Q R*.htm + 8-K + streaming MD&A."""
    cik_norm = cik10(getattr(snap, "cik", ""))
    sym_u = str(getattr(snap, "symbol", "")).strip().upper()
    as_of_key = as_of.isoformat() if as_of else "latest"

    if not force_refresh and bank_cache is not None:
        cached_ptr = bank_cache.get(f"bank:{cik_norm}:{as_of_key}")
        if isinstance(cached_ptr, Mapping) and cached_ptr.get("schema_version") == BANK_DISCLOSURE_VERSION:
            try:
                return BankDisclosureSnapshot.model_validate(cached_ptr), True
            except Exception:
                pass
    if cache_only:
        return None, False

    adapter = SecDeepAdapter(
        user_agent=user_agent,
        transport=transport,
        stream_transport=stream_transport,
        policy=policy,
    )
    # Reuse submissions if supplied; else fetch once
    subs_res = submissions
    if subs_res is None:
        subs_res = fetch_company_submissions(
            cik_norm,
            user_agent=user_agent,
            max_pages=0,
            limiter=limiter,
            policy=policy,
            transport=transport,
            stream_transport=stream_transport,
            use_ttl_cache=True,
        )

    # Identify newest periodic filing (10-Q or 10-K) <= as_of
    periodic_row: Any = None
    periodic_acc: str | None = None
    periodic_form = "10-Q"
    periodic_filed = str(getattr(snap, "latest_periodic_filed_at", None) or _utcnow().date().isoformat())
    periodic_primary_doc: str | None = None
    subs_rows = getattr(subs_res, "rows", None)
    if subs_rows:
        p_cands = [
            r for r in subs_rows
            if str(getattr(r, "form", "")).upper() in ("10-Q", "10-K", "10-Q/A", "10-K/A")
            and (as_of is None or r.filed_at <= as_of)
        ]
        if p_cands:
            p_cands.sort(key=lambda r: (r.filed_at, r.accession), reverse=True)
            periodic_row = p_cands[0]
            periodic_acc = periodic_row.accession
            periodic_form = str(periodic_row.form or "10-Q").removesuffix("/A").upper()
            if periodic_row.filed_at is not None:
                periodic_filed = periodic_row.filed_at.isoformat()
            periodic_primary_doc = periodic_row.primary_document
    elif isinstance(subs_res, Mapping):
        rec = (subs_res.get("filings") or {}).get("recent") if isinstance(subs_res.get("filings"), Mapping) else None
        if isinstance(rec, Mapping):
            accs = rec.get("accessionNumber") or []
            forms = rec.get("form") or []
            fdates = rec.get("filingDate") or []
            pdocs = rec.get("primaryDocument") or []
            p_raw_cands: list[tuple[date, str, str, str | None]] = []
            for idx_s, acc_s in enumerate(accs):
                fm_s = str(forms[idx_s] if idx_s < len(forms) else "").upper()
                if fm_s not in ("10-Q", "10-K", "10-Q/A", "10-K/A"):
                    continue
                fd_d = _parse_date(fdates[idx_s] if idx_s < len(fdates) else None)
                if fd_d is None or (as_of is not None and fd_d > as_of):
                    continue
                pd_s = str(pdocs[idx_s]) if idx_s < len(pdocs) and pdocs[idx_s] else None
                p_raw_cands.append((fd_d, str(acc_s), fm_s.removesuffix("/A"), pd_s))
            if p_raw_cands:
                p_raw_cands.sort(key=lambda x: (x[0], x[1]), reverse=True)
                fd_d, periodic_acc, periodic_form, periodic_primary_doc = p_raw_cands[0]
                periodic_filed = fd_d.isoformat()

    if periodic_acc is None:
        periodic_acc = getattr(snap, "latest_periodic_accession", None)
    if periodic_form not in ("10-Q", "10-K"):
        periodic_form = "10-Q"

    data_gaps: list[str] = []
    provenance_sources: list[dict[str, str]] = []

    # Step 1: Fetch FilingSummary.xml and targeted R*.htm (<= 5 files) for newest 10-Q/10-K
    catalog = preloaded_catalog
    r2_parsed: FilingTableData | None = None
    r4_parsed: FilingTableData | None = None
    seg_snapshot: BankSegmentSnapshot | None = None
    regcap_tables: list[FilingTableData] = []
    htm_tables: list[FilingTableData] = []

    if periodic_acc:
        dashed_p = periodic_acc if "-" in periodic_acc else f"{periodic_acc[:10]}-{periodic_acc[10:12]}-{periodic_acc[12:]}"
        nodash_p = dashed_p.replace("-", "")
        base_p = f"https://www.sec.gov/Archives/edgar/data/{int(cik_norm)}/{nodash_p}"
        if catalog is None:
            try:
                fs_url = f"{base_p}/FilingSummary.xml"
                fs_xml, _ct, _sha = sec_get_text(
                    fs_url,
                    category="filings",
                    user_agent=user_agent,
                    limiter=limiter,
                    policy=policy,
                    transport=transport,
                    max_bytes=5_000_000,
                )
                if fs_xml:
                    catalog = parse_filing_summary_xml(fs_xml, cik=cik_norm, accession=dashed_p, filing_summary_url=fs_url)
                    provenance_sources.append({"source": "10q_filing_summary", "accession": dashed_p, "url": fs_url})
            except Exception as exc:
                data_gaps.append(f"filing_summary_unavailable:{type(exc).__name__}")

        if catalog is not None:
            def _load_r(entry: FilingReportEntry | None) -> FilingTableData | None:
                if entry is None:
                    return None
                r_url = f"{base_p}/{entry.r_file}"
                try:
                    html, _ct_r, _sha_r = sec_get_text(
                        r_url,
                        category="filings",
                        user_agent=user_agent,
                        limiter=limiter,
                        policy=policy,
                        transport=transport,
                        max_bytes=4_000_000,
                    )
                    if not html:
                        return None
                    provenance_sources.append({"source": f"r_file:{entry.r_file}", "accession": dashed_p, "url": r_url})
                    return parse_r_html_table(
                        html,
                        r_file=entry.r_file,
                        url=r_url,
                        content_sha256=_sha_r,
                        entity_id=cik_norm,
                        accession=dashed_p,
                    )
                except Exception as exc_r:
                    data_gaps.append(f"r_file_failed:{entry.r_file}:{type(exc_r).__name__}")
                    return None

            r2_entry = _find_best_catalog_entry(catalog, kind="income")
            r4_entry = _find_best_catalog_entry(catalog, kind="balance")
            r5_entry = _find_best_catalog_entry(catalog, kind="balance_parenthetical")
            seg_entry = _find_best_catalog_entry(catalog, kind="segments")
            rc_ratio_entry = _find_best_catalog_entry(catalog, kind="regcap_ratio")
            rc_req_entry = _find_best_catalog_entry(catalog, kind="regcap_req")
            htm_entry = _find_best_catalog_entry(catalog, kind="htm")

            r2_parsed = _load_r(r2_entry)
            r4_parsed = _load_r(r4_entry)
            r5_parsed = _load_r(r5_entry) if r5_entry is not None else None
            seg_tbl = _load_r(seg_entry)
            if seg_tbl is not None and seg_entry is not None:
                seg_snapshot = extract_bank_segment_snapshot(
                    seg_tbl,
                    cik=cik_norm,
                    symbol=sym_u,
                    accession=dashed_p,
                    source_url=f"{base_p}/{seg_entry.r_file}",
                    preferred_kind="12M" if periodic_form == "10-K" else "3M",
                )
            t_rc1 = _load_r(rc_ratio_entry)
            if t_rc1 is not None:
                regcap_tables.append(t_rc1)
            if rc_req_entry is not None and (rc_ratio_entry is None or rc_req_entry.r_file != rc_ratio_entry.r_file):
                t_rc2 = _load_r(rc_req_entry)
                if t_rc2 is not None:
                    regcap_tables.append(t_rc2)
            t_htm = _load_r(htm_entry)
            if t_htm is not None:
                htm_tables.append(t_htm)

    # Step 2: Merge Tier A (PeerCompanySnapshot v6) & Tier B (R2 + R4 + R5 + RegCap + HTM)
    r2_info = parse_r2_income_statement_bank(r2_parsed) if r2_parsed is not None else {}
    r4_info = parse_r4_balance_sheet_bank(r4_parsed) if r4_parsed is not None else {}
    r5_info = _parse_r5_parenthetical(locals().get("r5_parsed"))
    rc_info = parse_reg_capital_details_table(regcap_tables) if regcap_tables else {}
    htm_info = parse_htm_securities_details(htm_tables) if htm_tables else {}
    if htm_info.get("htm_fair_value_usd") is None and r5_info.get("htm_fair_value_usd") is not None:
        htm_info["htm_fair_value_usd"] = r5_info["htm_fair_value_usd"]
        if htm_info.get("htm_amortized_cost_usd") is not None:
            htm_info["htm_net_unrealized_pnl_usd"] = htm_info["htm_fair_value_usd"] - htm_info["htm_amortized_cost_usd"]

    # Detect companyfacts lag (e.g., Citigroup max_filed < periodic_filed by > 30d or explicit pending flag)
    xbrl_fresh = not bool(getattr(snap, "xbrl_indexing_pending_accession", None))
    snap_end_check = _extract_quarter_and_ttm_flow_from_snapshot(snap, "bank_net_revenue", as_of=as_of)[2]
    r4_end: date | None = r4_info.get("period_end_date")
    if r4_end is not None and (snap_end_check is None or (r4_end - snap_end_check).days > 45):
        xbrl_fresh = False
        data_gaps.append("xbrl_companyfacts_lagging_tier_b_primary")

    # Flow P&L (prefer Tier B R2 when fresh discrepancy or XBRL lagged; also verify with Tier A TTM)
    r2_q = r2_info.get("3M") or {}
    x_rev_q, x_rev_ttm, x_rev_end, x_rev_lbl = _extract_quarter_and_ttm_flow_from_snapshot(snap, "bank_net_revenue", as_of=as_of)
    x_nii_q, _, _, _ = _extract_quarter_and_ttm_flow_from_snapshot(snap, "net_interest_income", as_of=as_of)
    x_nonint_inc_q, _, _, _ = _extract_quarter_and_ttm_flow_from_snapshot(snap, "noninterest_income", as_of=as_of)
    x_nonint_exp_q, _, _, _ = _extract_quarter_and_ttm_flow_from_snapshot(snap, "noninterest_expense", as_of=as_of)
    x_prov_q, x_prov_ttm, _, _ = _extract_quarter_and_ttm_flow_from_snapshot(snap, "provision_for_credit_losses", as_of=as_of)
    x_pbt_q, _, _, _ = _extract_quarter_and_ttm_flow_from_snapshot(snap, "profit_before_tax", as_of=as_of)
    x_ni_q, _, _, _ = _extract_quarter_and_ttm_flow_from_snapshot(snap, "net_income", as_of=as_of)
    x_ni_comm_q, x_ni_comm_ttm, _, _ = _extract_quarter_and_ttm_flow_from_snapshot(snap, "net_income_common", as_of=as_of)
    x_pdiv_q, x_pdiv_ttm, _, _ = _extract_quarter_and_ttm_flow_from_snapshot(snap, "preferred_dividends", as_of=as_of)
    _, x_div_ttm, _, _ = _extract_quarter_and_ttm_flow_from_snapshot(snap, "dividends_paid", as_of=as_of)
    _, x_bbk_ttm, _, _ = _extract_quarter_and_ttm_flow_from_snapshot(snap, "share_repurchases", as_of=as_of)

    net_rev_gaap_q = (r2_q.get("rev") if (not xbrl_fresh or x_rev_q is None) else x_rev_q) or r2_q.get("rev") or Decimal("0")
    nii_gaap_q = (r2_q.get("nii") if (not xbrl_fresh or x_nii_q is None) else x_nii_q) or r2_q.get("nii") or Decimal("0")
    nonint_inc_q = (r2_q.get("nonint_inc") if (not xbrl_fresh or x_nonint_inc_q is None) else x_nonint_inc_q) or r2_q.get("nonint_inc") or (net_rev_gaap_q - nii_gaap_q)
    # Re-ensure accounting identity NetRev = NII + Noninterest when NII+Nonint available cleanly
    if nii_gaap_q != 0 and nonint_inc_q != 0 and abs(net_rev_gaap_q - (nii_gaap_q + nonint_inc_q)) > Decimal("2000000"):
        if r2_q.get("rev") is not None:
            net_rev_gaap_q = r2_q["rev"]
        else:
            net_rev_gaap_q = nii_gaap_q + nonint_inc_q

    nonint_exp_q = (r2_q.get("nonint_exp") if (not xbrl_fresh or x_nonint_exp_q is None) else x_nonint_exp_q) or r2_q.get("nonint_exp") or Decimal("0")
    ppnr_gaap_q = net_rev_gaap_q - nonint_exp_q

    # Accounting identity for Provision: derive PPNR - PBT when direct XBRL deviates >1% (BAC/MS/C off-BS credit) or is absent (GS release)
    pbt_use = (r2_q.get("pbt") if not xbrl_fresh else x_pbt_q) or r2_q.get("pbt")
    prov_direct = (r2_q.get("prov") if not xbrl_fresh else x_prov_q) or r2_q.get("prov")
    prov_derived = (ppnr_gaap_q - pbt_use) if (pbt_use is not None) else None
    if prov_derived is not None and (prov_direct is None or (abs(prov_derived - prov_direct) > max(Decimal("5000000"), abs(prov_derived) * Decimal("0.01")))):
        provision_q = prov_derived
    else:
        provision_q = prov_direct if prov_direct is not None else Decimal("0")

    ni_total_q = (r2_q.get("ni") if not xbrl_fresh else x_ni_q) or r2_q.get("ni") or Decimal("0")
    if not xbrl_fresh and r2_q.get("ni") is not None:
        ni_comm_q = r2_q["ni"]
    elif x_ni_comm_q is not None:
        ni_comm_q = x_ni_comm_q
    elif x_pdiv_q is not None:
        ni_comm_q = ni_total_q - abs(x_pdiv_q)
    else:
        ni_comm_q = ni_total_q

    # Citigroup 9M-lag TTM assembly: TTM = YTD_cur + FY_prev - YTD_prev
    rev_ttm = x_rev_ttm if xbrl_fresh else None
    ppnr_ttm: Decimal | None = None
    ni_comm_ttm = x_ni_comm_ttm if xbrl_fresh else None
    prov_ttm = x_prov_ttm if xbrl_fresh else None
    if xbrl_fresh and x_rev_ttm is not None:
        _, exp_ttm, _, _ = _extract_quarter_and_ttm_flow_from_snapshot(snap, "noninterest_expense", as_of=as_of)
        if exp_ttm is not None:
            ppnr_ttm = x_rev_ttm - exp_ttm
        if ni_comm_ttm is None:
            _, ni_tot_ttm, _, _ = _extract_quarter_and_ttm_flow_from_snapshot(snap, "net_income", as_of=as_of)
            if ni_tot_ttm is not None:
                ni_comm_ttm = ni_tot_ttm - abs(x_pdiv_ttm or Decimal("0"))
    elif not xbrl_fresh and r2_info.get("9M") and r2_info.get("9M_prior"):
        y_cur = r2_info["9M"]
        y_pri = r2_info["9M_prior"]
        # Read latest full fiscal year from companyfacts (even if lagged 2025, FY2025 is complete!)
        fy_rev = _extract_latest_fy_from_snapshot(snap, "bank_net_revenue", as_of=as_of)
        fy_exp = _extract_latest_fy_from_snapshot(snap, "noninterest_expense", as_of=as_of)
        fy_ni = _extract_latest_fy_from_snapshot(snap, "net_income_common", as_of=as_of) or _extract_latest_fy_from_snapshot(snap, "net_income", as_of=as_of)
        fy_prov = _extract_latest_fy_from_snapshot(snap, "provision_for_credit_losses", as_of=as_of)
        if fy_rev is not None and y_cur.get("rev") is not None and y_pri.get("rev") is not None:
            rev_ttm = y_cur["rev"] + fy_rev - y_pri["rev"]
        if fy_exp is not None and y_cur.get("nonint_exp") is not None and y_pri.get("nonint_exp") is not None and rev_ttm is not None:
            exp_ttm_d = y_cur["nonint_exp"] + fy_exp - y_pri["nonint_exp"]
            ppnr_ttm = rev_ttm - exp_ttm_d
        if fy_ni is not None and y_cur.get("ni") is not None and y_pri.get("ni") is not None:
            ni_comm_ttm = y_cur["ni"] + fy_ni - y_pri["ni"]
        if fy_prov is not None and y_cur.get("prov") is not None and y_pri.get("prov") is not None:
            prov_ttm = y_cur["prov"] + fy_prov - y_pri["prov"]

    # Step 3: Balance sheet, Equity & Preferred Stock & Shares (date-aligned + face-value trap guards!)
    period_end_date = r4_end or r2_q.get("end") or x_rev_end or _utcnow().date()
    total_assets = r4_info.get("total_assets") or _extract_latest_instant_from_snapshot(snap, "total_assets", as_of=as_of)[0] or Decimal("0")
    total_deposits = r4_info.get("deposits") or _extract_latest_instant_from_snapshot(snap, "deposits", as_of=as_of)[0]
    deposits_prior_q = _extract_prior_instant_from_snapshot(snap, "deposits", latest_end=period_end_date, as_of=as_of)
    deposits_nib = _extract_latest_instant_from_snapshot(snap, "deposits_noninterest_bearing", as_of=as_of, target_end=period_end_date, max_end_drift_days=15)[0]
    loans_gross = r4_info.get("loans_gross") or _extract_latest_instant_from_snapshot(snap, "loans_gross", as_of=as_of, target_end=period_end_date, max_end_drift_days=15)[0]
    acl = r4_info.get("acl") or _extract_latest_instant_from_snapshot(snap, "allowance_for_credit_losses", as_of=as_of, target_end=period_end_date, max_end_drift_days=15)[0]
    if acl is not None:
        acl = abs(acl)
    nonaccrual = _extract_latest_instant_from_snapshot(snap, "nonaccrual_loans", as_of=as_of, target_end=period_end_date, max_end_drift_days=15)[0]
    nco_q, _, _, _ = _extract_quarter_and_ttm_flow_from_snapshot(snap, "net_charge_offs", as_of=as_of)
    aoci, _, _, _ = _extract_latest_instant_from_snapshot(snap, "aoci", as_of=as_of, target_end=period_end_date, max_end_drift_days=15)

    bridge_r4 = r4_info if r4_end == period_end_date else {}
    def aligned_money(line: str) -> Decimal | None:
        return _extract_latest_instant_from_snapshot(
            snap, line, as_of=as_of, required_unit="USD", target_end=period_end_date, max_end_drift_days=0,
        )[0]

    total_equity = bridge_r4.get("total_equity")
    if total_equity is None:
        total_equity = aligned_money("equity")
    pref_stock_r4 = bridge_r4.get("preferred_stock")
    pref_stock_xbrl = aligned_money("preferred_stock")
    # Face-value-zero guard: if preferred_stock == 0 while preferred dividends > 0, trust R4.htm!
    if pref_stock_xbrl == Decimal("0") and (x_pdiv_q is not None and abs(x_pdiv_q) > 0):
        pref_stock_xbrl = None
    preferred_stock = pref_stock_r4 if pref_stock_r4 is not None else pref_stock_xbrl
    common_equity = bridge_r4.get("common_equity") if bridge_r4.get("common_equity") is not None else (total_equity - preferred_stock if total_equity is not None and preferred_stock is not None else None)

    goodwill = bridge_r4.get("goodwill") if bridge_r4.get("goodwill") is not None else aligned_money("goodwill")
    intangibles_ex_msr = bridge_r4.get("intangibles") if bridge_r4.get("intangibles") is not None else aligned_money("intangible_assets")
    msr = bridge_r4.get("msr")

    # Ending common shares must be measured at the balance-sheet date.
    sh_usg, _, _, _ = _extract_latest_instant_from_snapshot(
        snap, "common_shares_outstanding", as_of=as_of, required_unit="shares", target_end=period_end_date, max_end_drift_days=0,
    )
    sh_dei, _ = _extract_dei_shares_from_snapshot(snap, target_end=period_end_date, max_drift_days=45, as_of=as_of)
    sh_r4 = bridge_r4.get("shares_ending_common")
    sh_basic, _, _, _ = _extract_quarter_and_ttm_flow_from_snapshot(snap, "shares_basic", as_of=as_of)

    # Step 4: Tier C (8-K EX-99.1/2/3 tables + 10-Q MD&A streaming fallback)
    er = preloaded_earnings_release
    if er is None and subs_res is not None:
        try:
            if hasattr(subs_res, "rows"):
                subs_arg: Any = [
                    {
                        "accession": r.accession,
                        "form": r.form,
                        "filed_at": r.filed_at,
                        "accepted_at": r.accepted_at,
                        "period_of_report": r.period_of_report,
                        "primary_document_url": r.primary_document_url,
                        "index_url": r.index_url,
                        "description": r.description,
                        "items": r.items,
                    }
                    for r in subs_res.rows
                ]
            else:
                subs_arg = subs_res
            er_tool = sec_earnings_release(adapter.sec_adapter, cik_norm, submissions=subs_arg, as_of=as_of, pairing_mode="sequential", max_candidates=10)
            if er_tool.status in ("ok", "partial") and er_tool.data:
                er = er_tool.data[0]
                provenance_sources.append({"source": "8k_earnings_release", "accession": er.current.accession if er.current else ""})
        except Exception as exc_8k:
            data_gaps.append(f"8k_release_unavailable:{type(exc_8k).__name__}")

    er_cur = er.current if er is not None and er.cik.lstrip("0") == cik_norm.lstrip("0") else None
    if er_cur is not None and as_of and er_cur.filed_at > as_of:
        er_cur = None
    er_metrics = _extract_8k_supplement_metrics(er_cur, period_end_date)

    # Tier C2: 10-Q MD&A scan when 8-K omits TBVPS/ROTE (GS) or RegCap/SCB/Min (BAC/C/GS) or shares fell through
    need_tbvps_or_rote = er_metrics.get("tbvps_usd") is None or er_metrics.get("rotce_q_pct") is None
    need_regcap_min = rc_info.get("cet1_reg_min_pct") is None or rc_info.get("cet1_reg_min_pct") <= Decimal("4.5")
    need_cet1 = rc_info.get("cet1_standardized_pct") is None and er_metrics.get("cet1_std_pct") is None
    need_shares = sh_usg is None and sh_r4 is None and sh_dei is None
    mda_metrics: dict[str, Any] = {}
    if need_tbvps_or_rote or need_regcap_min or need_cet1 or need_shares:
        if preloaded_10q_text:
            mda_metrics = scan_10q_mda_bank_disclosures_from_text(preloaded_10q_text)
        elif periodic_acc and periodic_primary_doc:
            nodash_p = periodic_acc.replace("-", "")
            doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik_norm)}/{nodash_p}/{periodic_primary_doc}"
            try:
                mda_metrics = stream_scan_10q_mda_for_bank_gaps(
                    doc_url,
                    user_agent=user_agent,
                    limiter=limiter,
                    policy=policy,
                    transport=transport,
                    stream_transport=stream_transport,
                )
                provenance_sources.append({"source": "10q_mda_stream_scan", "accession": periodic_acc, "url": doc_url})
            except Exception as exc_mda:
                data_gaps.append(f"10q_mda_scan_failed:{type(exc_mda).__name__}")

    # Only shares measured at the balance-sheet date are eligible for this bridge.
    shares_ending = sh_usg if sh_usg is not None else sh_r4
    components = (common_equity, goodwill, intangibles_ex_msr)
    tce_xbrl_std = common_equity - goodwill - intangibles_ex_msr if all(v is not None for v in components) else None
    tbvps_xbrl_std = tce_xbrl_std / shares_ending if tce_xbrl_std is not None and shares_ending is not None and shares_ending > 0 else None
    if tbvps_xbrl_std is None:
        data_gaps.append("tbvps_standardized_missing_aligned_inputs")

    # Reconcile CET1 (10-Q final takes precedence over 8-K preliminary for identical period)
    cet1_prelim_8k = er_metrics.get("cet1_std_pct")
    cet1_10q = rc_info.get("cet1_standardized_pct") or mda_metrics.get("cet1_standardized_pct")
    cet1_xbrl_pct: Decimal | None = None
    x_cet1, _, _, _ = _extract_latest_instant_from_snapshot(snap, "cet1_ratio", as_of=as_of, target_end=period_end_date, max_end_drift_days=15)
    if x_cet1 is not None:
        cet1_xbrl_pct = normalize_sec_ratio_pct(x_cet1, raw_text=str(x_cet1))

    if cet1_10q is not None:
        cet1_std_final = cet1_10q
        cet1_source: Literal["10q_final", "8k_preliminary", "xbrl_companyfacts"] | None = "10q_final"
    elif cet1_xbrl_pct is not None:
        cet1_std_final = cet1_xbrl_pct
        cet1_source = "xbrl_companyfacts"
    elif cet1_prelim_8k is not None:
        cet1_std_final = cet1_prelim_8k
        cet1_source = "8k_preliminary"
    else:
        cet1_std_final = None
        cet1_source = None

    cet1_scb_final = rc_info.get("cet1_scb_pct") or mda_metrics.get("cet1_scb_pct")
    cet1_gsib_final = rc_info.get("cet1_gsib_surcharge_pct") or mda_metrics.get("cet1_gsib_surcharge_pct")
    rc_min = rc_info.get("cet1_reg_min_pct")
    mda_min = mda_metrics.get("cet1_reg_min_pct")
    if rc_min is not None and rc_min > Decimal("4.5"):
        cet1_min_final = rc_min
    elif mda_min is not None and mda_min > Decimal("4.5"):
        cet1_min_final = mda_min
    elif cet1_scb_final is not None:
        cet1_min_final = Decimal("4.5") + cet1_scb_final + (cet1_gsib_final or Decimal("0"))
    else:
        cet1_min_final = rc_min or mda_min

    # A reported ratio must be an actual, period-aligned consolidated disclosure.
    tbvps_evidence = er_metrics.get("tbvps_usd_provenance") or {}
    tbvps_reported = (er_metrics.get("tbvps_usd") if tbvps_evidence.get("unit") == "USD/share"
                       and tbvps_evidence.get("unit_evidence") and tbvps_evidence.get("scale_evidence") else None)
    tbvps_src = "8k_supplement" if tbvps_reported is not None else "unavailable"
    if tbvps_reported is None:
        data_gaps.append("tbvps_reported_no_period_aligned_consolidated_table")
    tbvps_gap_pct = ((tbvps_xbrl_std - tbvps_reported) / tbvps_reported * Decimal("100")
                     if tbvps_xbrl_std is not None and tbvps_reported is not None and tbvps_reported > 0 else None)
    bridge = TbvpsReconciliationBridge(
        total_stockholders_equity_usd=_dstr(total_equity), preferred_stock_usd=_dstr(preferred_stock),
        common_equity_usd=_dstr(common_equity), goodwill_usd=_dstr(goodwill),
        intangibles_ex_msr_usd=_dstr(intangibles_ex_msr), msr_excluded_from_intangibles_usd=_dstr(msr),
        tce_xbrl_std_usd=_dstr(tce_xbrl_std), shares_ending_common=_dstr(shares_ending),
        tbvps_reported_usd=_qstr(tbvps_reported, 2), tbvps_reported_source=tbvps_src,
        bvps_reported_usd=_qstr(er_metrics.get("bvps_usd"), 2),
        tbvps_xbrl_std_usd=_qstr(tbvps_xbrl_std, 2), tbvps_gap_pct=_qstr(tbvps_gap_pct, 2),
        tbvps_reported_verified=tbvps_reported is not None,
        tbvps_reported_verification_reason="period_aligned_consolidated_table_with_unit_evidence" if tbvps_reported is not None else "no_period_aligned_consolidated_table_with_unit_evidence",
        tbvps_xbrl_std_verified=tbvps_xbrl_std is not None,
        tbvps_xbrl_std_verification_reason="aligned_components" if tbvps_xbrl_std is not None else "missing_aligned_inputs",
        tbvps_reported_provenance=er_metrics.get("tbvps_usd_provenance", {}),
        tbvps_xbrl_std_provenance={"period_end": period_end_date.isoformat(), "common_equity": "filing_balance_sheet" if bridge_r4.get("common_equity") is not None or (bridge_r4.get("total_equity") is not None and bridge_r4.get("preferred_stock") is not None) else "aligned_companyfacts_or_mixed", "goodwill": "filing_balance_sheet" if bridge_r4.get("goodwill") is not None else "aligned_companyfacts", "intangibles": "filing_balance_sheet" if bridge_r4.get("intangibles") is not None else "aligned_companyfacts", "shares": "aligned_companyfacts" if sh_usg is not None else "filing_balance_sheet", "unit": "USD/share"} if tbvps_xbrl_std is not None else {},
    )
    rotce_evidence = er_metrics.get("rotce_q_pct_provenance") or {}
    rotce_q = (er_metrics.get("rotce_q_pct") if rotce_evidence.get("unit") == "percent"
               and rotce_evidence.get("unit_evidence") else None)
    if rotce_q is None:
        data_gaps.append("rotce_q_no_period_aligned_consolidated_table")
    eff_reported = er_metrics.get("efficiency_pct")
    # Managed revenue/PPNR from 10-Q segments if R*.htm indicated FTE reconciling offset
    net_rev_managed = None
    ppnr_managed = None
    if seg_snapshot is not None and seg_snapshot.fte_reconciling_adjustment_usd:
        adj = _d(seg_snapshot.fte_reconciling_adjustment_usd) or Decimal("0")
        net_rev_managed = net_rev_gaap_q + adj
        ppnr_managed = ppnr_gaap_q + adj

    # Qualifying subclass
    tmp_qual = qualify_bank_sector_applicability(
        sic=getattr(snap, "sic", None),
        snap=snap,
        as_of=as_of,
    )
    # Refine subclass if R4 gave better Loans/Assets & GAAP P&L gave exact NII share
    if tmp_qual.is_bank_v4a and net_rev_gaap_q > 0 and total_assets > 0 and loans_gross is not None:
        ni_sh = (net_rev_gaap_q - nii_gaap_q) / net_rev_gaap_q
        lr = loans_gross / total_assets
        if str(getattr(snap, "sic", "")) == "6211" and lr < Decimal("0.35") and ni_sh >= Decimal("0.60"):
            bank_subtype: BankSubtype = "investment_bank_bhc"
        elif lr < Decimal("0.25"):
            bank_subtype = "custodian_or_broker_bank"
        else:
            bank_subtype = "universal_commercial"
    else:
        bank_subtype = tmp_qual.bank_subtype

    fiscal_lbl = x_rev_lbl or f"{period_end_date.isoformat()[:7]}"
    cet1_cap_raw = rc_info.get("cet1_capital_usd") or _extract_latest_instant_from_snapshot(snap, "cet1_capital", as_of=as_of)[0]
    if cet1_cap_raw is not None and cet1_cap_raw <= 0:
        cet1_cap_raw = None
    rwa_raw = rc_info.get("cet1_rwa") or _extract_latest_instant_from_snapshot(snap, "rwa", as_of=as_of)[0]
    if rwa_raw is not None and total_assets is not None and total_assets > 0:
        if not (total_assets * Decimal("0.05") <= rwa_raw <= total_assets * Decimal("2.5")):
            rwa_raw = None
    if rwa_raw is None and cet1_cap_raw is not None and cet1_std_final is not None and cet1_std_final > 0:
        derived_rwa = (cet1_cap_raw / (cet1_std_final / Decimal("100"))).quantize(Decimal("1"))
        if total_assets is None or total_assets <= 0 or (total_assets * Decimal("0.05") <= derived_rwa <= total_assets * Decimal("2.5")):
            rwa_raw = derived_rwa

    out = BankDisclosureSnapshot(
        cik=cik_norm,
        symbol=sym_u,
        company_name=str(getattr(snap, "company_name", None) or sym_u),
        sic=str(getattr(snap, "sic", None) or "6021"),
        bank_subtype=bank_subtype,
        period_end_date=period_end_date.isoformat(),
        fiscal_period_label=fiscal_lbl,
        periodic_form=periodic_form,  # type: ignore[arg-type]
        periodic_accession=periodic_acc or "unknown",
        periodic_filed_at=periodic_filed,
        earnings_8k_accession=er_cur.accession if er_cur else None,
        earnings_8k_filed_at=er_cur.filed_at.isoformat() if er_cur else None,
        earnings_8k_exhibits_used=list(er_metrics.get("exhibits_used") or []),
        xbrl_companyfacts_fresh=xbrl_fresh,
        net_revenue_gaap_q_usd=_dstr(net_rev_gaap_q) or "0",
        net_revenue_managed_q_usd=_dstr(net_rev_managed),
        nii_gaap_q_usd=_dstr(nii_gaap_q) or "0",
        nii_fte_q_usd=_dstr(nii_gaap_q + (_d(seg_snapshot.fte_reconciling_adjustment_usd) or Decimal("0"))) if (seg_snapshot and seg_snapshot.fte_reconciling_adjustment_usd) else None,
        noninterest_income_gaap_q_usd=_dstr(nonint_inc_q) or "0",
        noninterest_expense_q_usd=_dstr(nonint_exp_q) or "0",
        ppnr_gaap_q_usd=_dstr(ppnr_gaap_q) or "0",
        ppnr_managed_q_usd=_dstr(ppnr_managed),
        provision_q_usd=_dstr(provision_q) or "0",
        provision_ttm_usd=_dstr(prov_ttm),
        net_income_q_usd=_dstr(ni_total_q) or "0",
        net_income_to_common_q_usd=_dstr(ni_comm_q) or "0",
        net_revenue_gaap_ttm_usd=_dstr(rev_ttm),
        ppnr_gaap_ttm_usd=_dstr(ppnr_ttm),
        net_income_to_common_ttm_usd=_dstr(ni_comm_ttm),
        total_assets_usd=_dstr(total_assets) or "0",
        total_deposits_usd=_dstr(total_deposits),
        total_deposits_prior_q_usd=_dstr(deposits_prior_q),
        deposits_noninterest_bearing_usd=_dstr(deposits_nib),
        total_loans_gross_usd=_dstr(loans_gross),
        allowance_credit_losses_usd=_dstr(acl),
        nonaccrual_loans_usd=_dstr(nonaccrual),
        net_charge_offs_q_usd=_dstr(nco_q),
        avg_tangible_common_equity_q_usd=None,
        aoci_usd=_dstr(aoci),
        common_dividends_ttm_usd=_dstr(abs(x_div_ttm)) if x_div_ttm is not None else None,
        common_buybacks_ttm_usd=_dstr(abs(x_bbk_ttm)) if x_bbk_ttm is not None else None,
        cet1_capital_usd=_dstr(cet1_cap_raw),
        rwa_standardized_usd=_dstr(rwa_raw),
        cet1_standardized_pct=_qstr(cet1_std_final, 2),
        cet1_standardized_source=cet1_source,
        cet1_preliminary_8k_pct=_qstr(cet1_prelim_8k, 2),
        cet1_advanced_pct=_qstr(rc_info.get("cet1_advanced_pct"), 2),
        cet1_reg_min_pct=_qstr(cet1_min_final, 2),
        cet1_scb_pct=_qstr(cet1_scb_final, 2),
        cet1_gsib_surcharge_pct=_qstr(cet1_gsib_final, 2),
        tier1_leverage_pct=_qstr(rc_info.get("tier1_leverage_pct"), 2),
        slr_pct=_qstr(rc_info.get("slr_pct"), 2),
        slr_min_pct="5.00" if cet1_gsib_final else None,
        htm_amortized_cost_usd=_dstr(htm_info.get("htm_amortized_cost_usd")),
        htm_fair_value_usd=_dstr(htm_info.get("htm_fair_value_usd")),
        htm_acl_usd=_dstr(htm_info.get("htm_acl_usd")),
        htm_gross_unrealized_gain_usd=_dstr(htm_info.get("htm_gross_unrealized_gain_usd")),
        htm_gross_unrealized_loss_usd=_dstr(htm_info.get("htm_gross_unrealized_loss_usd")),
        htm_net_unrealized_pnl_usd=_dstr(htm_info.get("htm_net_unrealized_pnl_usd")),
        efficiency_ratio_reported_pct=_qstr(eff_reported, 2),
        nim_fte_pct=_qstr(er_metrics.get("nim_fte_pct"), 2),
        rotce_q_reported_pct=_qstr(rotce_q, 2),
        rotce_q_verified=rotce_q is not None,
        rotce_q_verification_reason="period_aligned_consolidated_table_with_unit_evidence" if rotce_q is not None else "no_period_aligned_consolidated_table_with_unit_evidence",
        rotce_q_provenance=er_metrics.get("rotce_q_pct_provenance", {}),
        tbvps_bridge=bridge,
        segments=seg_snapshot,
        provenance_sources=provenance_sources,
        metric_provenance={k.removesuffix("_provenance"): v for k, v in er_metrics.items() if k.endswith("_provenance")},
        data_gaps=list(dict.fromkeys(data_gaps)),
    )

    if bank_cache is not None and as_of is None:
        bank_cache.put(f"bank:{cik_norm}:{as_of_key}", out.model_dump(mode="json"))
    return out, False


def _extract_latest_fy_from_snapshot(snap: Any, line_name: str, *, as_of: date | None = None) -> Decimal | None:
    facts_by_line = getattr(snap, "facts_by_line", None) or {}
    line_bucket = facts_by_line.get(line_name)
    if not isinstance(line_bucket, Mapping):
        return None
    best: tuple[date, date, Decimal] | None = None
    for _tag, units_map in line_bucket.items():
        if not isinstance(units_map, Mapping):
            continue
        for _uk, rows in units_map.items():
            for r in rows or []:
                if not isinstance(r, Mapping):
                    continue
                s_d = _parse_date(r.get("start"))
                e_d = _parse_date(r.get("end"))
                if s_d is None or e_d is None or not (340 <= (e_d - s_d).days <= 380):
                    continue
                filed_d = _parse_date(r.get("filed")) or date.min
                if as_of is not None and filed_d != date.min and filed_d > as_of:
                    continue
                val = _d(r.get("val"))
                if val is None:
                    continue
                cand = (e_d, filed_d, val)
                if best is None or cand[:2] > best[:2]:
                    best = cand
    return best[2] if best else None
