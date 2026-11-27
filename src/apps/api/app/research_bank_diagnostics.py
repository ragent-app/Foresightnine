"""V4a US Major Bank 5-Pillar Diagnostics (0-LLM) & Gordon P/TBV vs ROTCE Valuation Engine.

All arithmetic executes inside an explicit 100-digit ``Decimal`` ``localcontext`` with
``ROUND_HALF_UP``. Thresholds load deterministically from ``v4a_bank_rules.json``
without issuer-specific special cases and strictly without FRED dependencies.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from functools import lru_cache
import json
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .research_bank_versions import BANK_DISCLOSURE_VERSION, BANK_VALUATION_VERSION
from .fin_data.adapters.sec_bank_disclosure import (
    BankDisclosureSnapshot,
    BankSubtype,
    TbvpsReconciliationBridge,
)

BankDiagnosticGroup = Literal[
    "profitability_leverage",
    "capital_adequacy",
    "securities_overhang",
    "credit_quality",
    "funding_liquidity",
]

RULES_PATH = Path(__file__).parent / "fin_data" / "adapters" / "v4a_bank_rules.json"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BankDiagnosticMetric(_FrozenModel):
    name: str
    group: BankDiagnosticGroup
    status: Literal["computed", "unavailable", "exempt_by_subtype"]
    value: str | None = None
    unit: str
    basis: str | None = None
    formula: str
    input_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


class BankDiagnosticFlag(_FrozenModel):
    id: str
    group: BankDiagnosticGroup
    severity: Literal["red", "yellow", "green", "info"]
    title: str
    detail: str
    metric_key: str
    metric_value: str | None = None
    threshold: str
    formula: str
    as_of: str | None = None


class BankDiagnosticsSummary(_FrozenModel):
    schema_version: Literal["v4a.1"] = "v4a.1"
    symbol: str
    cik: str
    bank_subtype: BankSubtype
    period_end_date: str
    as_of: str
    bank_quality_light: Literal["red", "yellow", "green"]
    bank_quality_score_breakdown: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, BankDiagnosticMetric] = Field(default_factory=dict)
    flags: list[BankDiagnosticFlag] = Field(default_factory=list)
    flag_counts: dict[str, int] = Field(default_factory=dict)
    thresholds_reference: dict[str, str] = Field(default_factory=dict)
    segment_mix_summary: dict[str, Any] = Field(default_factory=dict)
    comparability_warnings: list[dict[str, str]] = Field(default_factory=list)
    # Typed convenience fields for downstream V2 / Direction B / Squad anchors
    efficiency_ratio_gaap_pct: str | None = None
    efficiency_ratio_managed_pct: str | None = None
    ppnr_margin_pct: str | None = None
    nii_share_pct: str | None = None
    rotce_q_pct: str | None = None
    cet1_buffer_bps: str | None = None
    slr_buffer_bps: str | None = None
    htm_loss_pct_of_tce: str | None = None
    htm_after_tax_cet1_hit_bps: str | None = None
    pro_forma_cet1_ex_htm_pct: str | None = None
    provision_to_ppnr_pct: str | None = None
    acl_to_loans_pct: str | None = None
    nco_annualized_pct: str | None = None
    npl_to_loans_pct: str | None = None
    ldr_pct: str | None = None
    deposits_qoq_pct: str | None = None
    nib_deposit_share_pct: str | None = None


class BankGordonSensitivityCell(_FrozenModel):
    coe_pct: str
    terminal_g_pct: str
    implied_rotce_pct: str | None = None
    justified_p_to_tbv: str | None = None
    rotce_spread_pp: str | None = None


class BankValuationResult(_FrozenModel):
    schema_version: Literal["v4a.1"] = "v4a.1"
    symbol: str
    cik: str
    period_end_date: str
    as_of: str
    basis_validation_version: str | None = None
    price_usd: str | None = None
    market_cap_usd: str | None = None
    primary_tbvps_basis: Literal["reported_market_convention", "xbrl_std_fallback", "xbrl_standardized", "unavailable"]
    coe_pct: str
    terminal_g_pct: str
    p_to_tbv_primary: str | None = None
    p_to_tbv_xbrl_std: str | None = None
    p_to_bv: str | None = None
    p_to_e_ttm: str | None = None
    p_to_ppnr_ttm: str | None = None
    current_rotce_pct: str | None = None
    implied_rotce_pct: str | None = None
    rotce_spread_vs_implied_pp: str | None = None
    justified_p_to_tbv_at_current_rotce: str | None = None
    valuation_signal: Literal["attractive_discount", "fairly_valued", "rich_premium", "unavailable"] = "unavailable"
    sensitivity_matrix: list[BankGordonSensitivityCell] = Field(default_factory=list)
    tbvps_bridge: TbvpsReconciliationBridge
    comparability_warnings: list[dict[str, str]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _dec(val: Any) -> Decimal | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, Decimal):
        return val if val.is_finite() else None
    try:
        d = Decimal(str(val).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _q(val: Decimal | None, places: int = 2) -> str | None:
    if val is None or not val.is_finite():
        return None
    q = Decimal(10) ** (-places)
    return format(val.quantize(q, rounding=ROUND_HALF_UP), "f")


@lru_cache(maxsize=1)
def load_v4a_bank_rules() -> dict[str, Any]:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def _check_anti_future(snapshot: BankDisclosureSnapshot, as_of: date | None) -> str:
    eff_as_of = as_of.isoformat() if as_of is not None else snapshot.periodic_filed_at[:10]
    if as_of is not None:
        filed_s = (snapshot.periodic_filed_at or "")[:10]
        try:
            if filed_s and date.fromisoformat(filed_s) > as_of:
                raise ValueError(
                    f"anti_future_guard: snapshot filed_at {filed_s} exceeds as_of {as_of.isoformat()}"
                )
        except ValueError as err:
            if "anti_future_guard" in str(err):
                raise
    return eff_as_of


def _verified(snapshot: BankDisclosureSnapshot, flag: str, *, bridge: bool = False) -> bool:
    # Archives remain readable, but pre-validation snapshots cannot feed new results.
    target = snapshot.tbvps_bridge if bridge else snapshot
    return snapshot.schema_version == BANK_DISCLOSURE_VERSION and bool(getattr(target, flag, False))


def _build_comparability_warnings(
    snapshot: BankDisclosureSnapshot,
    rules: Mapping[str, Any],
) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    tbv_rules = rules.get("tbvps") or {}
    gap_thresh = _dec(tbv_rules.get("comparability_gap_warning_pct")) or Decimal("1.00")

    bridge = snapshot.tbvps_bridge
    reported_ok = _verified(snapshot, "tbvps_reported_verified", bridge=True)
    std_ok = _verified(snapshot, "tbvps_xbrl_std_verified", bridge=True)
    for ok, name in ((reported_ok, "reported"), (std_ok, "xbrl_std")):
        if not ok:
            reason = getattr(bridge, f"tbvps_{name}_verification_reason", None) or "basis not verified"
            warnings.append({"code": f"tbvps_{name}_unverified", "severity": "info", "message": reason})
    gap_pct = _dec(bridge.tbvps_gap_pct) if reported_ok and std_ok else None
    if gap_pct is not None and abs(gap_pct) > gap_thresh:
        warnings.append({"code": "tbvps_basis_difference", "severity": "info",
                         "message": f"Verified TBVPS bases differ by {gap_pct}%."})
    if snapshot.segments and snapshot.segments.segments_basis != snapshot.segments.consolidated_basis:
        warnings.append(
            {
                "code": "segments_managed_fte_vs_consolidated_gaap",
                "severity": "info",
                "message": (
                    f"Operating segments reported on {snapshot.segments.segments_basis} basis while consolidated P&L is "
                    f"{snapshot.segments.consolidated_basis}; reconciling offset preserved cleanly."
                ),
            }
        )
    return warnings


def evaluate_bank_diagnostics(
    snapshot: BankDisclosureSnapshot,
    *,
    rules: Mapping[str, Any] | None = None,
    as_of: date | None = None,
) -> BankDiagnosticsSummary:
    """Evaluate 5-pillar bank diagnostics with D4 tiered HTM escalation & subtype exemptions."""
    eff_as_of = _check_anti_future(snapshot, as_of)
    cfg = dict(load_v4a_bank_rules() if rules is None else rules)
    cap_rules = cfg.get("capital_and_htm") or {}
    prof_rules = cfg.get("profitability_and_credit") or {}
    exempt_map = (cfg.get("subtype_exemptions") or {}).get(snapshot.bank_subtype) or {}
    exempt_checks = set(exempt_map.get("exempt_commercial_red_checks") or [])

    with localcontext() as ctx:
        ctx.prec = 100

        metrics: dict[str, BankDiagnosticMetric] = {}
        flags: list[BankDiagnosticFlag] = []

        rev_gaap = _dec(snapshot.net_revenue_gaap_q_usd)
        rev_mgd = _dec(snapshot.net_revenue_managed_q_usd)
        nii_gaap = _dec(snapshot.nii_gaap_q_usd)
        exp_q = _dec(snapshot.noninterest_expense_q_usd)
        ppnr_gaap = _dec(snapshot.ppnr_gaap_q_usd)
        prov_q = _dec(snapshot.provision_q_usd)

        # ---- Pillar 1: Profitability & Operating Leverage ----
        eff_gaap = ((exp_q / rev_gaap) * Decimal("100")) if (exp_q is not None and rev_gaap and rev_gaap > 0) else None
        eff_mgd = ((exp_q / rev_mgd) * Decimal("100")) if (exp_q is not None and rev_mgd and rev_mgd > 0) else _dec(snapshot.efficiency_ratio_reported_pct)
        ppnr_margin = ((ppnr_gaap / rev_gaap) * Decimal("100")) if (ppnr_gaap is not None and rev_gaap and rev_gaap > 0) else None
        nii_share = ((nii_gaap / rev_gaap) * Decimal("100")) if (nii_gaap is not None and rev_gaap and rev_gaap > 0) else None
        # Ending TCE is not average TCE; neither an unverified disclosure nor
        # an inferred denominator can support a quarterly ROTCE.
        rotce_q = (_dec(snapshot.rotce_q_reported_pct)
                   if _verified(snapshot, "rotce_q_verified") else None)

        metrics["efficiency_ratio_gaap_pct"] = BankDiagnosticMetric(
            name="efficiency_ratio_gaap_pct",
            group="profitability_leverage",
            status="computed" if eff_gaap is not None else "unavailable",
            value=_q(eff_gaap, 2),
            unit="%",
            basis="gaap",
            formula="(Noninterest_Expense / Net_Revenue_GAAP) * 100",
            input_ids=["noninterest_expense_q_usd", "net_revenue_gaap_q_usd"],
        )
        metrics["ppnr_margin_pct"] = BankDiagnosticMetric(
            name="ppnr_margin_pct",
            group="profitability_leverage",
            status="computed" if ppnr_margin is not None else "unavailable",
            value=_q(ppnr_margin, 2),
            unit="%",
            basis="gaap",
            formula="(PPNR_GAAP / Net_Revenue_GAAP) * 100",
            input_ids=["ppnr_gaap_q_usd", "net_revenue_gaap_q_usd"],
        )
        metrics["nii_share_pct"] = BankDiagnosticMetric(
            name="nii_share_pct",
            group="profitability_leverage",
            status="computed" if nii_share is not None else "unavailable",
            value=_q(nii_share, 2),
            unit="%",
            basis="gaap",
            formula="(NII_GAAP / Net_Revenue_GAAP) * 100",
            input_ids=["nii_gaap_q_usd", "net_revenue_gaap_q_usd"],
        )
        metrics["rotce_q_pct"] = BankDiagnosticMetric(
            name="rotce_q_pct",
            group="profitability_leverage",
            status="computed" if rotce_q is not None else "unavailable",
            value=_q(rotce_q, 2),
            unit="%",
            basis="reported",
            formula="Verified reported quarterly ROTCE",
            input_ids=["rotce_q_reported_pct"],
            reason=None if rotce_q is not None else (getattr(snapshot, "rotce_q_verification_reason", None) or "Quarterly ROTCE is unverified or unavailable."),
        )

        # ---- Pillar 2: Capital Adequacy ----
        cet1_std = _dec(snapshot.cet1_standardized_pct)
        cet1_min = _dec(snapshot.cet1_reg_min_pct)
        cet1_buf_bps = ((cet1_std - cet1_min) * Decimal("100")) if (cet1_std is not None and cet1_min is not None) else None
        slr_pct = _dec(snapshot.slr_pct)
        slr_min = _dec(snapshot.slr_min_pct)
        slr_buf_bps = ((slr_pct - slr_min) * Decimal("100")) if (slr_pct is not None and slr_min is not None) else None

        metrics["cet1_buffer_bps"] = BankDiagnosticMetric(
            name="cet1_buffer_bps",
            group="capital_adequacy",
            status="computed" if cet1_buf_bps is not None else "unavailable",
            value=_q(cet1_buf_bps, 0),
            unit="bps",
            basis=snapshot.cet1_standardized_source or "standardized_basel3",
            formula="(CET1_Standardized_Pct - (4.5% + SCB + G-SIB)) * 100",
            input_ids=["cet1_standardized_pct", "cet1_reg_min_pct"],
        )

        # ---- Pillar 3: Securities Portfolio & HTM Overhang (D4 Tax-Adjusted CET1 Stress) ----
        htm_pnl = _dec(snapshot.htm_net_unrealized_pnl_usd)
        tce_for_htm = (_dec(snapshot.tbvps_bridge.tce_xbrl_std_usd) if _verified(snapshot, "tbvps_xbrl_std_verified", bridge=True) else None)
        rwa_std = _dec(snapshot.rwa_standardized_usd)
        tax_rate = _dec(cap_rules.get("htm_effective_tax_rate")) or Decimal("0.24")

        htm_loss_abs = max(Decimal("0"), -htm_pnl) if htm_pnl is not None else None
        htm_loss_pct_tce = ((htm_loss_abs / tce_for_htm) * Decimal("100")) if (htm_loss_abs is not None and tce_for_htm and tce_for_htm > 0) else None
        htm_after_tax_hit_bps: Decimal | None = None
        pro_forma_cet1: Decimal | None = None
        total_assets_d = _dec(snapshot.total_assets_usd)
        rwa_sane = (
            rwa_std is not None
            and rwa_std > 0
            and (total_assets_d is None or total_assets_d <= 0 or rwa_std >= total_assets_d * Decimal("0.05"))
        )
        if htm_loss_abs is not None and rwa_sane:
            assert rwa_std is not None
            after_tax_loss = htm_loss_abs * (Decimal("1") - tax_rate)
            htm_after_tax_hit_bps = (after_tax_loss / rwa_std) * Decimal("10000")
            if cet1_std is not None:
                pro_forma_cet1 = cet1_std - (htm_after_tax_hit_bps / Decimal("100"))

        metrics["htm_loss_pct_of_tce"] = BankDiagnosticMetric(
            name="htm_loss_pct_of_tce",
            group="securities_overhang",
            status="computed" if htm_loss_pct_tce is not None else "unavailable",
            value=_q(htm_loss_pct_tce, 2),
            unit="%",
            basis="pre_tax_htm_overhang",
            formula="(max(0, -(HTM_Fair_Value - HTM_Amortized_Cost)) / TCE) * 100",
            input_ids=["htm_net_unrealized_pnl_usd", "tbvps_bridge.tce_reported_usd"],
        )
        metrics["pro_forma_cet1_ex_htm_pct"] = BankDiagnosticMetric(
            name="pro_forma_cet1_ex_htm_pct",
            group="securities_overhang",
            status="computed" if pro_forma_cet1 is not None else "unavailable",
            value=_q(pro_forma_cet1, 2),
            unit="%",
            basis="after_tax_24pct_stress",
            formula="CET1_Std - ((max(0, -HTM_Net_Loss) * (1 - 0.24)) / RWA_Std) * 100",
            input_ids=["cet1_standardized_pct", "htm_net_unrealized_pnl_usd", "rwa_standardized_usd"],
        )

        # ---- Pillar 4: Credit Quality ----
        loans_gross = _dec(snapshot.total_loans_gross_usd)
        acl = _dec(snapshot.allowance_credit_losses_usd)
        nco_q = _dec(snapshot.net_charge_offs_q_usd)
        npl = _dec(snapshot.nonaccrual_loans_usd)

        prov_to_ppnr = ((prov_q / ppnr_gaap) * Decimal("100")) if (prov_q is not None and ppnr_gaap and ppnr_gaap > 0) else None
        acl_to_loans = ((acl / loans_gross) * Decimal("100")) if (acl is not None and loans_gross and loans_gross > 0) else None
        nco_ann = ((abs(nco_q) * Decimal("4") / loans_gross) * Decimal("100")) if (nco_q is not None and loans_gross and loans_gross > 0) else None
        npl_to_loans = ((npl / loans_gross) * Decimal("100")) if (npl is not None and loans_gross and loans_gross > 0) else None

        metrics["provision_to_ppnr_pct"] = BankDiagnosticMetric(
            name="provision_to_ppnr_pct",
            group="credit_quality",
            status="computed" if prov_to_ppnr is not None else "unavailable",
            value=_q(prov_to_ppnr, 2),
            unit="%",
            formula="(Provision_Q / PPNR_GAAP_Q) * 100",
            input_ids=["provision_q_usd", "ppnr_gaap_q_usd"],
        )
        metrics["acl_to_loans_pct"] = BankDiagnosticMetric(
            name="acl_to_loans_pct",
            group="credit_quality",
            status="computed" if acl_to_loans is not None else "unavailable",
            value=_q(acl_to_loans, 2),
            unit="%",
            formula="(ACL / Total_Loans_Gross) * 100",
            input_ids=["allowance_credit_losses_usd", "total_loans_gross_usd"],
        )

        # ---- Pillar 5: Funding & Deposit Liquidity ----
        deposits = _dec(snapshot.total_deposits_usd)
        deposits_pri = _dec(snapshot.total_deposits_prior_q_usd)
        deposits_nib = _dec(snapshot.deposits_noninterest_bearing_usd)

        ldr_pct = ((loans_gross / deposits) * Decimal("100")) if (loans_gross is not None and deposits and deposits > 0) else None
        dep_qoq_pct = (((deposits - deposits_pri) / deposits_pri) * Decimal("100")) if (deposits is not None and deposits_pri and deposits_pri > 0) else None
        nib_share_pct = ((deposits_nib / deposits) * Decimal("100")) if (deposits_nib is not None and deposits and deposits > 0) else None

        metrics["ldr_pct"] = BankDiagnosticMetric(
            name="ldr_pct",
            group="funding_liquidity",
            status="exempt_by_subtype" if "ldr_high_yellow" in exempt_checks else ("computed" if ldr_pct is not None else "unavailable"),
            value=_q(ldr_pct, 2) if "ldr_high_yellow" not in exempt_checks else None,
            unit="%",
            formula="(Total_Loans_Gross / Total_Deposits) * 100",
            input_ids=["total_loans_gross_usd", "total_deposits_usd"],
            reason="Commercial LDR stress exempt for investment_bank_bhc / custodian_or_broker_bank" if "ldr_high_yellow" in exempt_checks else None,
        )
        metrics["deposits_qoq_pct"] = BankDiagnosticMetric(
            name="deposits_qoq_pct",
            group="funding_liquidity",
            status="computed" if dep_qoq_pct is not None else "unavailable",
            value=_q(dep_qoq_pct, 2),
            unit="%",
            formula="((Deposits_Q - Deposits_Prior_Q) / Deposits_Prior_Q) * 100",
            input_ids=["total_deposits_usd", "total_deposits_prior_q_usd"],
        )

        # ---- Transparent Red / Yellow / Green Rule Evaluation (D4 Tiered HTM Escalation) ----
        red_cet1_bps = _dec(cap_rules.get("cet1_buffer_red_bps")) or Decimal("50")
        yel_cet1_bps = _dec(cap_rules.get("cet1_buffer_yellow_bps")) or Decimal("100")
        htm_yel = _dec(cap_rules.get("htm_loss_pct_of_tce_yellow")) or Decimal("15.0")
        htm_red_base = _dec(cap_rules.get("htm_loss_pct_of_tce_red_escalation_base")) or Decimal("30.0")
        stack_cet1_bps = _dec(cap_rules.get("htm_red_stack_cet1_buffer_lt_bps")) or Decimal("100")
        stack_dep_qoq = _dec(cap_rules.get("htm_red_stack_deposits_qoq_lte_pct")) or Decimal("-3.0")

        eff_yel = _dec(prof_rules.get("efficiency_ratio_yellow_pct")) or Decimal("68.0")
        rotce_yel = _dec(prof_rules.get("rotce_yellow_lt_pct")) or Decimal("10.0")
        prov_yel = _dec(prof_rules.get("provision_to_ppnr_yellow_pct")) or Decimal("30.0")
        prov_red = _dec(prof_rules.get("provision_to_ppnr_red_pct")) or Decimal("50.0")
        dep_yel = _dec(prof_rules.get("deposits_qoq_yellow_lte_pct")) or Decimal("-3.0")

        # 1. CET1 buffer flag
        if cet1_buf_bps is not None:
            if cet1_buf_bps < red_cet1_bps:
                sev_cet1: Literal["red", "yellow", "green"] = "red"
            elif cet1_buf_bps < yel_cet1_bps:
                sev_cet1 = "yellow"
            else:
                sev_cet1 = "green"
            flags.append(
                BankDiagnosticFlag(
                    id="cet1_regulatory_buffer",
                    group="capital_adequacy",
                    severity=sev_cet1,
                    title="CET1 Standardized Buffer vs Regulatory Minimum",
                    detail=f"CET1 Std { _q(cet1_std,2) }% vs Req Min { _q(cet1_min,2) }% -> {_q(cet1_buf_bps,0)} bps buffer.",
                    metric_key="cet1_buffer_bps",
                    metric_value=_q(cet1_buf_bps, 0),
                    threshold="red < 50 bps; yellow < 100 bps; green >= 100 bps",
                    formula="(CET1_Std - CET1_Reg_Min) * 100 bps",
                    as_of=eff_as_of,
                )
            )

        # 2. D4 Tiered HTM Unrealized Loss Escalation flag
        if htm_loss_pct_tce is not None:
            has_cap_or_dep_stress = (
                (cet1_buf_bps is not None and cet1_buf_bps < stack_cet1_bps)
                or (dep_qoq_pct is not None and dep_qoq_pct <= stack_dep_qoq)
            )
            if htm_loss_pct_tce >= htm_red_base and has_cap_or_dep_stress:
                sev_htm: Literal["red", "yellow", "green"] = "red"
                htm_detail = (
                    f"D4 Dual-Stress Escalation: HTM pre-tax loss { _q(htm_loss_pct_tce,2) }% of TCE (>=30%) "
                    f"stacked with tight CET1 buffer ({_q(cet1_buf_bps,0)} bps) or deposit outflow ({_q(dep_qoq_pct,2)}%). "
                    f"Pro-forma CET1 ex-HTM (24% tax): {_q(pro_forma_cet1,2)}%."
                )
            elif htm_loss_pct_tce >= htm_yel:
                sev_htm = "yellow"
                htm_detail = (
                    f"HTM pre-tax loss { _q(htm_loss_pct_tce,2) }% of TCE (>=15%) without stacked capital/deposit breach. "
                    f"Pro-forma after-tax CET1 ex-HTM: {_q(pro_forma_cet1,2)}% (hit -{_q(htm_after_tax_hit_bps,0)} bps)."
                )
            else:
                sev_htm = "green"
                htm_detail = f"HTM pre-tax loss { _q(htm_loss_pct_tce,2) }% of TCE (<15%); manageable rate overhang."
            flags.append(
                BankDiagnosticFlag(
                    id="htm_securities_overhang",
                    group="securities_overhang",
                    severity=sev_htm,
                    title="HTM Pre-Tax Unrealized Loss / TCE & Pro-Forma CET1 Stress",
                    detail=htm_detail,
                    metric_key="htm_loss_pct_of_tce",
                    metric_value=_q(htm_loss_pct_tce, 2),
                    threshold="red >= 30% TCE + (CET1 buf < 100bps OR deposits QoQ <= -3%); yellow >= 15%",
                    formula="(HTM_PreTax_Loss / TCE) * 100",
                    as_of=eff_as_of,
                )
            )

        # 3. Credit cost vs PPNR
        if prov_to_ppnr is not None:
            if prov_to_ppnr >= prov_red and "provision_to_ppnr_red" not in exempt_checks:
                sev_prov: Literal["red", "yellow", "green"] = "red"
            elif prov_to_ppnr >= prov_yel:
                sev_prov = "yellow"
            else:
                sev_prov = "green"
            flags.append(
                BankDiagnosticFlag(
                    id="credit_provision_absorption",
                    group="credit_quality",
                    severity=sev_prov,
                    title="Credit Provision Absorption of PPNR",
                    detail=f"Provision absorbs {_q(prov_to_ppnr,2)}% of GAAP PPNR (quarterly credit cost check).",
                    metric_key="provision_to_ppnr_pct",
                    metric_value=_q(prov_to_ppnr, 2),
                    threshold="red >= 50% (commercial); yellow >= 30%",
                    formula="(Provision_Q / PPNR_GAAP_Q) * 100",
                    as_of=eff_as_of,
                )
            )

        # 4. Operating Efficiency & ROTCE checks
        if eff_gaap is not None:
            sev_eff: Literal["red", "yellow", "green"] = "yellow" if eff_gaap >= eff_yel else "green"
            flags.append(
                BankDiagnosticFlag(
                    id="operating_efficiency_ratio",
                    group="profitability_leverage",
                    severity=sev_eff,
                    title="GAAP Efficiency Ratio",
                    detail=f"GAAP Efficiency Ratio at {_q(eff_gaap,2)}% (Managed/Reported: {_q(eff_mgd,2)}%).",
                    metric_key="efficiency_ratio_gaap_pct",
                    metric_value=_q(eff_gaap, 2),
                    threshold="yellow >= 68.0%",
                    formula="(Noninterest_Expense / Net_Revenue_GAAP) * 100",
                    as_of=eff_as_of,
                )
            )
        if rotce_q is not None:
            sev_rot: Literal["red", "yellow", "green"] = "yellow" if rotce_q < rotce_yel else "green"
            flags.append(
                BankDiagnosticFlag(
                    id="rotce_profitability",
                    group="profitability_leverage",
                    severity=sev_rot,
                    title="Return on Tangible Common Equity (ROTCE)",
                    detail=f"ROTCE at {_q(rotce_q,2)}% vs 10.0% Cost-of-Equity benchmark.",
                    metric_key="rotce_q_pct",
                    metric_value=_q(rotce_q, 2),
                    threshold="yellow < 10.0%",
                    formula="Reported ROTCE or annualized NI_to_Common / TCE",
                    as_of=eff_as_of,
                )
            )

        # 5. Deposit outflow check (when prior-Q available)
        if dep_qoq_pct is not None and dep_qoq_pct <= dep_yel:
            flags.append(
                BankDiagnosticFlag(
                    id="deposit_qoq_outflow",
                    group="funding_liquidity",
                    severity="yellow",
                    title="Sequential Deposit Contraction",
                    detail=f"Total deposits contracted {_q(dep_qoq_pct,2)}% QoQ.",
                    metric_key="deposits_qoq_pct",
                    metric_value=_q(dep_qoq_pct, 2),
                    threshold="yellow <= -3.0%",
                    formula="((Deposits_Q - Deposits_Prior_Q) / Deposits_Prior_Q) * 100",
                    as_of=eff_as_of,
                )
            )

        comp_warnings = _build_comparability_warnings(snapshot, cfg)
        for cw in comp_warnings:
            flags.append(
                BankDiagnosticFlag(
                    id=cw["code"],
                    group="capital_adequacy" if "tbvps" in cw["code"] else "profitability_leverage",
                    severity="info",
                    title="Basis / Comparability Notice",
                    detail=cw["message"],
                    metric_key="tbvps_gap_pct" if "tbvps" in cw["code"] else "segments_basis",
                    metric_value=snapshot.tbvps_bridge.tbvps_gap_pct if "tbvps" in cw["code"] else None,
                    threshold="info if |TBVPS gap| > 1.00% or RSU add-on > 0.5M or GAAP vs FTE mix",
                    formula="Reported Market Convention vs XBRL Standardized bridge",
                    as_of=eff_as_of,
                )
            )

        counts = {"red": 0, "yellow": 0, "green": 0, "info": 0}
        for fl in flags:
            counts[fl.severity] = counts.get(fl.severity, 0) + 1
        overall: Literal["red", "yellow", "green"] = (
            "red" if counts["red"] > 0 else ("yellow" if counts["yellow"] > 0 else "green")
        )

        seg_mix: dict[str, Any] = {}
        if snapshot.segments is not None:
            seg_mix = {
                "consolidated_basis": snapshot.segments.consolidated_basis,
                "segments_basis": snapshot.segments.segments_basis,
                "reconciliation_status": snapshot.segments.reconciliation_status,
                "reconciliation_delta_usd": snapshot.segments.reconciliation_delta_usd,
                "operating_segments": [
                    {
                        "segment_key": r.segment_key,
                        "display_name": r.display_name,
                        "net_revenue_usd": r.net_revenue_usd,
                        "revenue_share_pct": r.revenue_share_pct,
                        "ppnr_usd": r.ppnr_usd,
                        "net_income_usd": r.net_income_usd,
                        "reporting_basis": r.reporting_basis,
                    }
                    for r in snapshot.segments.operating_segments
                ],
            }

        return BankDiagnosticsSummary(
            symbol=snapshot.symbol,
            cik=snapshot.cik,
            bank_subtype=snapshot.bank_subtype,
            period_end_date=snapshot.period_end_date,
            as_of=eff_as_of,
            bank_quality_light=overall,
            bank_quality_score_breakdown={"counts": counts, "rule_source": "v4a_bank_rules.json"},
            metrics=metrics,
            flags=flags,
            flag_counts=counts,
            thresholds_reference={
                "cet1_buffer_bps": "RED < 50bps; YELLOW < 100bps; GREEN >= 100bps",
                "htm_overhang": "YELLOW >= 15% TCE; RED >= 30% TCE stacked with (CET1 buffer < 100bps OR deposits QoQ <= -3%)",
                "provision_to_ppnr": "RED >= 50% (commercial); YELLOW >= 30%",
                "efficiency_ratio": "YELLOW >= 68%",
                "rotce": "YELLOW < 10%",
            },
            segment_mix_summary=seg_mix,
            comparability_warnings=comp_warnings,
            efficiency_ratio_gaap_pct=_q(eff_gaap, 2),
            efficiency_ratio_managed_pct=_q(eff_mgd, 2),
            ppnr_margin_pct=_q(ppnr_margin, 2),
            nii_share_pct=_q(nii_share, 2),
            rotce_q_pct=_q(rotce_q, 2),
            cet1_buffer_bps=_q(cet1_buf_bps, 0),
            slr_buffer_bps=_q(slr_buf_bps, 0),
            htm_loss_pct_of_tce=_q(htm_loss_pct_tce, 2),
            htm_after_tax_cet1_hit_bps=_q(htm_after_tax_hit_bps, 0),
            pro_forma_cet1_ex_htm_pct=_q(pro_forma_cet1, 2),
            provision_to_ppnr_pct=_q(prov_to_ppnr, 2),
            acl_to_loans_pct=_q(acl_to_loans, 2),
            nco_annualized_pct=_q(nco_ann, 2),
            npl_to_loans_pct=_q(npl_to_loans, 2),
            ldr_pct=_q(ldr_pct, 2),
            deposits_qoq_pct=_q(dep_qoq_pct, 2),
            nib_deposit_share_pct=_q(nib_share_pct, 2),
        )


def evaluate_bank_valuation(
    snapshot: BankDisclosureSnapshot,
    *,
    price_usd: Decimal | str | int | float | None,
    coe_pct: Decimal | str | int | float = Decimal("10.0"),
    terminal_g_pct: Decimal | str | int | float = Decimal("3.0"),
    tbvps_basis: Literal["reported_market_convention", "xbrl_standardized"] = "reported_market_convention",
    rules: Mapping[str, Any] | None = None,
    as_of: date | None = None,
) -> BankValuationResult:
    """Evaluate dual-basis P/TBV and D5 Gordon P/TBV = (ROTCE - g) / (K_e - g) valuation anchor."""
    eff_as_of = _check_anti_future(snapshot, as_of)
    cfg = dict(load_v4a_bank_rules() if rules is None else rules)
    gordon_cfg = cfg.get("gordon_defaults") or {}

    with localcontext() as ctx:
        ctx.prec = 100

        px = _dec(price_usd)
        ke = _dec(coe_pct)
        g = _dec(terminal_g_pct)
        ke = ke if ke is not None else Decimal("10.0")
        g = g if g is not None else Decimal("3.0")

        bridge = snapshot.tbvps_bridge
        tbvps_rep = _dec(bridge.tbvps_reported_usd) if _verified(snapshot, "tbvps_reported_verified", bridge=True) else None
        tbvps_std = _dec(bridge.tbvps_xbrl_std_usd) if _verified(snapshot, "tbvps_xbrl_std_verified", bridge=True) else None
        if tbvps_basis == "xbrl_standardized":
            primary_tbvps = tbvps_std
            primary_mode: Literal["reported_market_convention", "xbrl_std_fallback", "xbrl_standardized", "unavailable"] = "xbrl_standardized"
        elif tbvps_rep is not None and tbvps_rep > 0:
            primary_tbvps = tbvps_rep
            primary_mode = "reported_market_convention"
        elif tbvps_std is not None and tbvps_std > 0:
            primary_tbvps = tbvps_std
            primary_mode = "xbrl_std_fallback"
        else:
            primary_tbvps = None
            primary_mode = "unavailable"

        shares_den = _dec(bridge.shares_ending_common)
        mcap = (px * shares_den) if (px is not None and px > 0 and shares_den and shares_den > 0) else None
        comm_eq = _dec(bridge.common_equity_usd)
        bvps_eff = _dec(bridge.bvps_reported_usd) or (
            (comm_eq / shares_den) if (comm_eq is not None and shares_den and shares_den > 0) else None
        )

        p_tbv_pri = (px / primary_tbvps) if (px is not None and px > 0 and primary_tbvps is not None and primary_tbvps > 0) else None
        p_tbv_std = (px / tbvps_std) if (px is not None and px > 0 and tbvps_std is not None and tbvps_std > 0) else None
        p_bv = (px / bvps_eff) if (px is not None and px > 0 and bvps_eff is not None and bvps_eff > 0) else None

        ni_ttm = _dec(snapshot.net_income_to_common_ttm_usd)
        if ni_ttm is None:
            ni_q = _dec(snapshot.net_income_to_common_q_usd)
            ni_ttm = (ni_q * Decimal("4")) if ni_q is not None else None
        ppnr_ttm = _dec(snapshot.ppnr_gaap_ttm_usd)
        if ppnr_ttm is None:
            ppnr_q = _dec(snapshot.ppnr_gaap_q_usd)
            ppnr_ttm = (ppnr_q * Decimal("4")) if ppnr_q is not None else None

        p_e_ttm = (mcap / ni_ttm) if (mcap is not None and ni_ttm is not None and ni_ttm > 0) else None
        p_ppnr_ttm = (mcap / ppnr_ttm) if (mcap is not None and ppnr_ttm is not None and ppnr_ttm > 0) else None

        rotce = (_dec(snapshot.rotce_q_reported_pct)
                 if _verified(snapshot, "rotce_q_verified") else None)

        notes: list[str] = []
        if rotce is None:
            notes.append(getattr(snapshot, "rotce_q_verification_reason", None) or "Quarterly ROTCE is unverified or unavailable.")
        if primary_tbvps is None:
            basis_reason = (getattr(bridge, "tbvps_xbrl_std_verification_reason", None)
                            if tbvps_basis == "xbrl_standardized" else
                            getattr(bridge, "tbvps_reported_verification_reason", None))
            notes.append(basis_reason or "TBVPS basis is unverified or unavailable.")
        if ke <= g:
            notes.append(f"Degenerate Gordon boundary guarded: K_e ({_q(ke,2)}%) <= terminal g ({_q(g,2)}%).")
        if primary_tbvps is None or primary_tbvps <= 0:
            notes.append("Non-positive TBVPS guarded: P/TBV and implied ROTCE unavailable.")

        def _gordon_calc(ke_val: Decimal, g_val: Decimal) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
            if ke_val <= g_val:
                return None, None, None
            imp: Decimal | None = None
            just: Decimal | None = None
            spd: Decimal | None = None
            if p_tbv_pri is not None:
                imp = g_val + p_tbv_pri * (ke_val - g_val)
            if rotce is not None:
                just = (rotce - g_val) / (ke_val - g_val)
                if imp is not None:
                    spd = rotce - imp
            return imp, just, spd

        imp_rotce, just_ptbv, spread_pp = _gordon_calc(ke, g)
        if spread_pp is None:
            signal: Literal["attractive_discount", "fairly_valued", "rich_premium", "unavailable"] = "unavailable"
        elif spread_pp >= Decimal("1.50"):
            signal = "attractive_discount"
        elif spread_pp <= Decimal("-1.50"):
            signal = "rich_premium"
        else:
            signal = "fairly_valued"

        coe_grid = [_dec(x) or Decimal("10.0") for x in (gordon_cfg.get("sensitivity_coe_grid") or ["9.0", "10.0", "11.0"])]
        g_grid = [_dec(x) or Decimal("3.0") for x in (gordon_cfg.get("sensitivity_g_grid") or ["2.0", "3.0", "4.0"])]
        matrix: list[BankGordonSensitivityCell] = []
        for c_k in coe_grid:
            for c_g in g_grid:
                m_imp, m_just, m_spd = _gordon_calc(c_k, c_g)
                matrix.append(
                    BankGordonSensitivityCell(
                        coe_pct=_q(c_k, 1) or "0.0",
                        terminal_g_pct=_q(c_g, 1) or "0.0",
                        implied_rotce_pct=_q(m_imp, 2),
                        justified_p_to_tbv=_q(m_just, 2),
                        rotce_spread_pp=_q(m_spd, 2),
                    )
                )

        return BankValuationResult(
            symbol=snapshot.symbol,
            cik=snapshot.cik,
            period_end_date=snapshot.period_end_date,
            as_of=eff_as_of,
            basis_validation_version=BANK_VALUATION_VERSION,
            price_usd=_q(px, 2),
            market_cap_usd=_q(mcap, 0),
            primary_tbvps_basis=primary_mode,
            coe_pct=_q(ke, 2) or "10.00",
            terminal_g_pct=_q(g, 2) or "3.00",
            p_to_tbv_primary=_q(p_tbv_pri, 2),
            p_to_tbv_xbrl_std=_q(p_tbv_std, 2),
            p_to_bv=_q(p_bv, 2),
            p_to_e_ttm=_q(p_e_ttm, 2),
            p_to_ppnr_ttm=_q(p_ppnr_ttm, 2),
            current_rotce_pct=_q(rotce, 2),
            implied_rotce_pct=_q(imp_rotce, 2),
            rotce_spread_vs_implied_pp=_q(spread_pp, 2),
            justified_p_to_tbv_at_current_rotce=_q(just_ptbv, 2),
            valuation_signal=signal,
            sensitivity_matrix=matrix,
            tbvps_bridge=bridge,
            comparability_warnings=_build_comparability_warnings(snapshot, cfg),
            notes=notes,
        )
