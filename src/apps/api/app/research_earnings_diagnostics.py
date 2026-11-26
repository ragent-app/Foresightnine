"""Deterministic Decimal-based financial diagnostics, guidance/Non-GAAP reconciliation,
and valuation / Reverse-DCF expectations engines for US earnings analysis.

Design principles:
- All arithmetic uses ``Decimal`` inside an explicit 100-digit ``localcontext`` with
  ``ROUND_HALF_UP`` quantization. No binary floating point enters numeric outputs.
- Every output metric carries an explicit algebraic ``formula``, input provenance
  identifiers (``input_ids`` / ``xbrl_tags`` / ``source_ids``), and explicit status
  (``computed``, ``unavailable``, or ``disabled_by_sector``).
- Zero black-box composite scoring: diagnostics emit transparent Red / Yellow / Green
  flags against explicit thresholds with mandatory MD&A / footnote investigation prompts.
- SEC SIC sector gate detects Banks (6000-6199), Insurance (6300-6411), and REITs
  (6798) and disables inapplicable non-financial metrics with explicit rationale.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import math
import re
from typing import Any, Iterable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .fin_data.models import StatementLine
from .research_calculations import CalculationError, UnitScaleSpec, parse_unit_scale


MetricStatus = Literal["computed", "unavailable", "disabled_by_sector"]
FlagSeverity = Literal["red", "yellow", "green"]
SectorCategory = Literal["non_financial", "bank", "insurance", "reit", "financial_other"]
BankSubtypeOptional = Literal[
    "universal_commercial",
    "investment_bank_bhc",
    "custodian_or_broker_bank",
    "not_applicable",
]
PeriodType = Literal["quarter", "annual", "ttm"]
NewGuidanceDirection = Literal["Raise", "Maintain", "Cut", "Not Provided"]
SurpriseOutcome = Literal["Beat", "Inline", "Miss", "Unavailable"]
RangePosition = Literal["above_high", "within_range", "below_low", "midpoint_only", "unavailable"]
NonGaapCategory = Literal[
    "sbc",
    "amortization_intangibles",
    "restructuring",
    "ma_transaction",
    "impairment",
    "litigation",
    "tax_effect",
    "fx_or_fair_value",
    "other",
]

DEFAULT_QUARTER_DAYS = Decimal("91.25")  # 365 / 4 standard annualized quarter basis
DEFAULT_ANNUAL_DAYS = Decimal("365")
_MAX_DECIMAL_DIGITS = 30
_MAX_DECIMAL_EXPONENT = 30

# Transparent, non-black-box diagnostic threshold reference table.
DIAGNOSTIC_THRESHOLDS: dict[str, dict[str, str]] = {
    "ocf_to_ni": {
        "red": "< 0.80x (or OCF <= 0 when Net Income > 0)",
        "yellow": "0.80x to < 0.95x",
        "green": ">= 0.95x",
    },
    "fcf_to_ni": {
        "red": "< 0.50x (or FCF <= 0 when Net Income > 0)",
        "yellow": "0.50x to < 0.80x",
        "green": ">= 0.80x",
    },
    "sbc_to_fcf_pct": {
        "red": "> 30% (or SBC >= FCF turning FCF ex-SBC <= 0)",
        "yellow": "> 15% to 30%",
        "green": "<= 15% (with positive FCF ex-SBC)",
    },
    "sbc_to_revenue_pct": {
        "red": "> 15%",
        "yellow": "> 8% to 15%",
        "green": "Not emitted separately when <= 8%",
    },
    "ar_vs_revenue_growth_pp": {
        "red": "> +15.0 pp",
        "yellow": "> +5.0 pp to +15.0 pp",
        "green": "<= +5.0 pp",
    },
    "inventory_vs_cogs_or_revenue_growth_pp": {
        "red": "> +15.0 pp",
        "yellow": "> +5.0 pp to +15.0 pp",
        "green": "<= +5.0 pp",
    },
    "ccc_yoy_delta_days": {
        "red": "> +15.0 days",
        "yellow": "> +5.0 days to +15.0 days",
        "green": "<= 0.0 days",
    },
    "gross_margin_yoy_bps": {
        "red": "< -200 bps",
        "yellow": "-200 bps to < -50 bps",
        "green": ">= +50 bps",
    },
    "operating_margin_yoy_bps": {
        "red": "< -200 bps",
        "yellow": "-200 bps to < -50 bps",
        "green": ">= +50 bps",
    },
    "diluted_shares_yoy_pct": {
        "red": "> +3.0%",
        "yellow": "> +1.0% to +3.0%",
        "green": "<= 0.0%",
    },
    "payout_to_fcf_pct": {
        "red": "> 120%",
        "yellow": "> 100% to 120%",
        "green": "0% to 100%",
    },
    "net_debt_to_ttm_ebitda": {
        "red": "> 3.5x",
        "yellow": "> 2.0x to 3.5x",
        "green": "<= 1.0x (including Net Cash)",
    },
    "interest_coverage": {
        "red": "< 2.0x",
        "yellow": "2.0x to < 4.0x",
        "green": ">= 6.0x",
    },
    "current_ratio": {
        "red": "< 1.0x",
        "yellow": "1.0x to < 1.2x",
        "green": ">= 1.5x",
    },
}

_BANK_DISABLED_METRICS: tuple[str, ...] = (
    "gross_margin",
    "gross_margin_qoq_bps",
    "gross_margin_yoy_bps",
    "rd_pct",
    "inventory_turnover",
    "dso",
    "dio",
    "dpo",
    "ccc",
    "ar_vs_revenue_growth_pp",
    "inventory_vs_cogs_growth_pp",
    "inventory_vs_revenue_growth_pp",
    "standard_fcf",
    "ocf_to_ni",
    "fcf_to_ni",
    "fcf_margin_pct",
    "sbc_to_fcf_pct",
    "fcf_ex_sbc",
    "fcf_ex_sbc_margin_pct",
    "capex_to_revenue_pct",
    "payout_to_fcf_pct",
    "net_debt_to_ttm_ebitda",
    "interest_coverage",
    "current_ratio",
    "standard_ev_ebitda",
    "enterprise_value",
    "ev_to_sales",
    "ev_to_ntm_sales",
    "ev_to_ebitda",
    "ev_to_ntm_ebitda",
    "ev_to_ebit",
    "p_to_fcf",
    "fcf_yield_pct",
    "ev_to_fcf_ex_sbc",
    "fcf_ex_sbc_yield_pct",
    "reverse_dcf",
)

_INSURANCE_DISABLED_METRICS: tuple[str, ...] = (
    "gross_margin",
    "gross_margin_qoq_bps",
    "gross_margin_yoy_bps",
    "rd_pct",
    "inventory_turnover",
    "dso",
    "dio",
    "dpo",
    "ccc",
    "ar_vs_revenue_growth_pp",
    "inventory_vs_cogs_growth_pp",
    "inventory_vs_revenue_growth_pp",
    "standard_fcf",
    "ocf_to_ni",
    "fcf_to_ni",
    "fcf_margin_pct",
    "sbc_to_fcf_pct",
    "fcf_ex_sbc",
    "fcf_ex_sbc_margin_pct",
    "capex_to_revenue_pct",
    "payout_to_fcf_pct",
    "net_debt_to_ttm_ebitda",
    "current_ratio",
    "standard_ev_ebitda",
    "enterprise_value",
    "ev_to_sales",
    "ev_to_ntm_sales",
    "ev_to_ebitda",
    "ev_to_ntm_ebitda",
    "ev_to_ebit",
    "p_to_fcf",
    "fcf_yield_pct",
    "ev_to_fcf_ex_sbc",
    "fcf_ex_sbc_yield_pct",
    "reverse_dcf",
)

_REIT_DISABLED_METRICS: tuple[str, ...] = (
    "gross_margin",
    "gross_margin_qoq_bps",
    "gross_margin_yoy_bps",
    "rd_pct",
    "inventory_turnover",
    "dio",
    "dpo",
    "ccc",
    "inventory_vs_cogs_growth_pp",
    "inventory_vs_revenue_growth_pp",
    "standard_fcf",
    "fcf_to_ni",
    "fcf_margin_pct",
    "sbc_to_fcf_pct",
    "fcf_ex_sbc",
    "fcf_ex_sbc_margin_pct",
    "payout_to_fcf_pct",
    "standard_ev_ebitda",
    "ev_to_ebitda",
    "ev_to_ntm_ebitda",
    "p_to_fcf",
    "fcf_yield_pct",
    "ev_to_fcf_ex_sbc",
    "fcf_ex_sbc_yield_pct",
    "reverse_dcf",
)

_ADJUSTMENT_CATEGORY_PATTERNS: tuple[tuple[NonGaapCategory, re.Pattern[str]], ...] = (
    ("sbc", re.compile(r"\b(?:sbc|stock[- ]?based|share[- ]?based|equity[- ]?based)\b|股权激励|股份支付", re.I)),
    ("amortization_intangibles", re.compile(r"\bamortization\s+of\s+(?:acquired\s+)?intangibles?\b|\bintangible\s+amortization\b|无形资产摊销", re.I)),
    ("restructuring", re.compile(r"\brestructur|severance|workforce\s+reduction|facility\s+(?:exit|closure)|重组|裁员", re.I)),
    ("ma_transaction", re.compile(r"\b(?:acquisition|merger|m&a|deal|transaction|integration)\s+(?:costs?|expenses?|fees?)\b|并购", re.I)),
    ("impairment", re.compile(r"\b(?:impairment|goodwill\s+write[- ]?down|asset\s+write[- ]?down)\b|减值", re.I)),
    ("litigation", re.compile(r"\b(?:litigation|legal\s+settlement|regulatory\s+(?:fine|settlement))\b|诉讼|和解", re.I)),
    ("tax_effect", re.compile(r"\b(?:income\s+)?tax\s+(?:effect|impact|adjustments?|benefit|provision)\b|税务影响|所得税调整", re.I)),
    ("fx_or_fair_value", re.compile(r"\b(?:foreign\s+exchange|fx\s+(?:gain|loss)|fair\s+value|mark[- ]to[- ]market|unrealized\s+(?:gain|loss))\b|公允价值|汇兑", re.I)),
)

_STRUCTURALLY_RECURRING_CATEGORIES: frozenset[NonGaapCategory] = frozenset({
    "sbc",
    "amortization_intangibles",
})
_SOMETIMES_RECURRING_CATEGORIES: frozenset[NonGaapCategory] = frozenset({
    "restructuring",
    "ma_transaction",
    "litigation",
    "fx_or_fair_value",
})


class DiagnosticsError(ValueError):
    """Raised when financial diagnostic inputs are malformed or incompatible."""


def _to_decimal(raw: Any, *, field_name: str = "value") -> Decimal:
    if isinstance(raw, bool):
        raise DiagnosticsError(f"{field_name} must be a numeric Decimal, not bool.")
    if isinstance(raw, Decimal):
        val = raw
    elif isinstance(raw, int):
        val = Decimal(raw)
    elif isinstance(raw, float):
        if not math.isfinite(raw):
            raise DiagnosticsError(f"{field_name} must be finite.")
        val = Decimal(str(raw))
    elif isinstance(raw, str):
        cleaned = raw.strip().replace(",", "")
        if not cleaned:
            raise DiagnosticsError(f"{field_name} must not be blank.")
        try:
            val = Decimal(cleaned)
        except InvalidOperation as exc:
            raise DiagnosticsError(f"Invalid decimal for {field_name}: {raw!r}.") from exc
    else:
        raise DiagnosticsError(f"Unsupported type for {field_name}: {type(raw).__name__}.")
    if not val.is_finite():
        raise DiagnosticsError(f"{field_name} must be finite.")
    if len(val.as_tuple().digits) > _MAX_DECIMAL_DIGITS or abs(val.adjusted()) > _MAX_DECIMAL_EXPONENT:
        raise DiagnosticsError(f"{field_name} exceeds supported Decimal precision.")
    return val


def _quantize(value: Decimal, decimal_places: int) -> Decimal:
    quant = Decimal("1") if decimal_places == 0 else Decimal(1).scaleb(-decimal_places)
    with localcontext() as ctx:
        ctx.prec = 100
        rounded = value.quantize(quant, rounding=ROUND_HALF_UP)
        if rounded == 0:
            rounded = Decimal("0").quantize(quant)
        return rounded


def _dedup_preserve(items: Iterable[str | None]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


class MetricValue(BaseModel):
    """Scalar financial quantity carrying explicit provenance and optional unit/currency."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    value: Decimal
    fact_id: str | None = Field(default=None, min_length=1, max_length=240)
    source_id: str | None = Field(default=None, min_length=1, max_length=240)
    xbrl_tag: str | None = Field(default=None, min_length=1, max_length=240)
    unit: str | None = Field(default=None, min_length=1, max_length=100)
    currency: str | None = Field(default=None, min_length=1, max_length=16)
    period: str | None = Field(default=None, min_length=1, max_length=100)
    label: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("value", mode="before")
    @classmethod
    def _validate_value(cls, raw: Any) -> Decimal:
        return _to_decimal(raw, field_name="MetricValue.value")

    @field_validator("currency")
    @classmethod
    def _normalize_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        upper = value.strip().upper()
        return "CNY" if upper == "RMB" else upper

    def provenance_id(self, fallback_key: str) -> str:
        if self.fact_id:
            return self.fact_id
        if self.source_id:
            return f"{self.source_id}:{fallback_key}"
        return fallback_key


def _coerce_metric_value(raw: Any, *, fallback_fact_id: str) -> Any:
    if raw is None or isinstance(raw, MetricValue):
        return raw
    if isinstance(raw, dict):
        if "fact_id" not in raw or raw.get("fact_id") is None:
            return {**raw, "fact_id": fallback_fact_id}
        return raw
    return MetricValue(value=_to_decimal(raw, field_name=fallback_fact_id), fact_id=fallback_fact_id)


def _provenance_lists(items: Sequence[tuple[str, MetricValue]]) -> tuple[list[str], list[str], list[str]]:
    input_ids = _dedup_preserve(mv.provenance_id(role) for role, mv in items)
    xbrl_tags = _dedup_preserve(mv.xbrl_tag for _, mv in items)
    source_ids = _dedup_preserve(mv.source_id for _, mv in items)
    return input_ids, xbrl_tags, source_ids


def _parse_unit_safe(unit: str, currency: str | None) -> UnitScaleSpec:
    normalized = unit.replace("-per-", " per ").strip()
    try:
        return parse_unit_scale(normalized, currency)
    except CalculationError as exc:
        raise DiagnosticsError(str(exc)) from exc


def _resolve_currency_and_scale(
    pairs: Sequence[tuple[str, MetricValue]],
    *,
    default_currency: str | None = None,
    default_monetary_unit: str | None = None,
    expected_family: Literal["monetary", "shares", "any"] = "monetary",
) -> tuple[list[Decimal], str | None, str]:
    """Check currency consistency and align values to the finest declared scale."""
    if not pairs:
        return [], default_currency, default_monetary_unit or (default_currency or "units")

    currencies: set[str] = set()
    if default_currency:
        norm_def = "CNY" if default_currency.strip().upper() == "RMB" else default_currency.strip().upper()
        currencies.add(norm_def)

    specs: list[UnitScaleSpec | None] = []
    for _, mv in pairs:
        if mv.currency:
            currencies.add(mv.currency)
        if mv.unit:
            spec = _parse_unit_safe(mv.unit, mv.currency or default_currency)
            if expected_family != "any" and spec.family != expected_family:
                raise DiagnosticsError(
                    f"Unit {mv.unit!r} belongs to family {spec.family!r}, expected {expected_family!r}."
                )
            if spec.currency:
                currencies.add(spec.currency)
            specs.append(spec)
        else:
            specs.append(None)

    if len(currencies) > 1:
        raise DiagnosticsError(f"Cross-currency inputs cannot be combined: {sorted(currencies)}.")
    resolved_currency = next(iter(currencies), None)

    explicit_specs = [s for s in specs if s is not None]
    if not explicit_specs:
        unit_label = default_monetary_unit or (
            "shares" if expected_family == "shares" else (resolved_currency or "monetary_units")
        )
        return [mv.value for _, mv in pairs], resolved_currency, unit_label

    explicit_families = {s.family for s in explicit_specs}
    if len(explicit_families) != 1:
        raise DiagnosticsError(f"Incompatible unit families cannot be combined: {sorted(explicit_families)}.")

    if any(s is None for s in specs):
        # If default_monetary_unit is given for unitless items, parse it to check scale consistency.
        if expected_family == "monetary" and default_monetary_unit:
            def_spec = _parse_unit_safe(default_monetary_unit, resolved_currency)
            specs = [s if s is not None else def_spec for s in specs]
        elif expected_family == "shares":
            # Default unitless shares to the scale of explicit share items only if all explicit items match.
            exponents = {s.exponent for s in explicit_specs}
            if len(exponents) != 1:
                raise DiagnosticsError("Cannot mix unitless and multi-scale share inputs.")
            fill_spec = explicit_specs[0]
            specs = [s if s is not None else fill_spec for s in specs]
        else:
            exponents = {s.exponent for s in explicit_specs}
            if len(exponents) != 1:
                raise DiagnosticsError("Cannot mix unitless and multi-scale inputs.")
            fill_spec = explicit_specs[0]
            specs = [s if s is not None else fill_spec for s in specs]

    resolved_specs = [s for s in specs if s is not None]
    families = {s.family for s in resolved_specs}
    if len(families) != 1:
        raise DiagnosticsError(f"Incompatible unit families cannot be combined: {sorted(families)}.")
    target_exp = min(s.exponent for s in resolved_specs)
    target_spec = next(s for s in resolved_specs if s.exponent == target_exp)

    aligned: list[Decimal] = []
    with localcontext() as ctx:
        ctx.prec = 100
        for (_, mv), spec in zip(pairs, resolved_specs):
            exp_diff = spec.exponent - target_exp
            aligned.append(mv.value.scaleb(exp_diff) if exp_diff else mv.value)

    return aligned, resolved_currency, target_spec.canonical_unit


def _aligned_monetary_map(
    periods: Sequence[PeriodFinancials],
) -> tuple[dict[tuple[str, str], Decimal], str | None, str]:
    """Align all monetary quantities across periods to one consistent currency & finest scale."""
    monetary_fields = (
        "revenue",
        "cost_of_revenue",
        "gross_profit",
        "rd_expense",
        "sga_expense",
        "operating_income",
        "ebit",
        "interest_expense",
        "net_income",
        "ebitda",
        "depreciation_amortization",
        "sbc",
        "operating_cash_flow",
        "capex",
        "free_cash_flow",
        "share_repurchases",
        "dividends_paid",
        "cash",
        "short_term_investments",
        "accounts_receivable",
        "inventory",
        "current_assets",
        "accounts_payable",
        "short_term_debt",
        "long_term_debt",
        "total_debt",
        "current_liabilities",
        "minority_interest",
    )
    pairs: list[tuple[str, MetricValue]] = []
    keys: list[tuple[str, str]] = []
    default_curr: str | None = None
    default_unit: str | None = None
    for idx, period in enumerate(periods):
        tag = f"p{idx}:{period.period_label}"
        if period.currency:
            if default_curr and default_curr != period.currency:
                raise DiagnosticsError(f"Cross-currency period conflict: {default_curr} vs {period.currency}.")
            default_curr = period.currency
        if period.monetary_unit and default_unit is None:
            default_unit = period.monetary_unit
        for fname in monetary_fields:
            mv = getattr(period, fname)
            if mv is not None:
                keys.append((tag, fname))
                pairs.append((f"{period.period_label}.{fname}", mv))

    values, resolved_curr, resolved_unit = _resolve_currency_and_scale(
        pairs,
        default_currency=default_curr,
        default_monetary_unit=default_unit,
        expected_family="monetary",
    )
    return dict(zip(keys, values)), resolved_curr, resolved_unit


def _aligned_shares_map(periods: Sequence[PeriodFinancials]) -> dict[str, Decimal]:
    pairs: list[tuple[str, MetricValue]] = []
    keys: list[str] = []
    for idx, period in enumerate(periods):
        if period.shares_diluted is not None:
            keys.append(f"p{idx}:{period.period_label}")
            pairs.append((f"{period.period_label}.shares_diluted", period.shares_diluted))
    if not pairs:
        return {}
    values, _, _ = _resolve_currency_and_scale(pairs, expected_family="shares")
    return dict(zip(keys, values))


class DiagnosticMetric(BaseModel):
    """Single deterministic financial metric with formula, provenance, and status."""

    model_config = ConfigDict(extra="forbid")

    name: str
    status: MetricStatus
    value: Decimal | None = None
    unit: str
    currency: str | None = None
    formula: str
    input_ids: list[str] = Field(default_factory=list)
    xbrl_tags: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


class SectorApplicability(BaseModel):
    """SEC SIC sector gate assessment explaining disabled non-financial metrics."""

    model_config = ConfigDict(extra="forbid")

    sic_code: str | None = None
    sic_description: str | None = None
    sector_category: SectorCategory
    is_gated: bool
    disabled_metrics: list[str] = Field(default_factory=list)
    recommended_alternatives: list[str] = Field(default_factory=list)
    explanation: str
    bank_subtype: BankSubtypeOptional | None = None
    active_sector_plugin: Literal["us_large_bank_v4a"] | None = None


def evaluate_sec_sic_gate(
    sic: str | int | None,
    sic_description: str | None = None,
) -> SectorApplicability:
    """Detect Banks (6000-6199), Insurance (6300-6411), and REITs (6798) by SEC SIC."""
    if sic is None or (isinstance(sic, str) and not sic.strip()):
        return SectorApplicability(
            sic_code=None,
            sic_description=sic_description,
            sector_category="non_financial",
            is_gated=False,
            disabled_metrics=[],
            recommended_alternatives=[],
            explanation="No SEC SIC code supplied; running full non-financial corporate diagnostics.",
        )

    if isinstance(sic, bool):
        raise DiagnosticsError("SIC code cannot be a boolean.")
    raw_text = str(sic).strip()
    if not raw_text.isdigit():
        raise DiagnosticsError(f"Invalid SEC SIC code: {sic!r}. Expected 4-digit numeric SIC.")
    sic_num = int(raw_text)
    if not (1 <= sic_num <= 9999):
        raise DiagnosticsError(f"SEC SIC code out of range (0001-9999): {sic!r}.")
    sic_code = f"{sic_num:04d}"

    if 6000 <= sic_num <= 6299:
        return SectorApplicability(
            sic_code=sic_code,
            sic_description=sic_description,
            sector_category="bank",
            is_gated=True,
            disabled_metrics=list(_BANK_DISABLED_METRICS),
            recommended_alternatives=[
                "Net Interest Margin (NIM)",
                "Efficiency Ratio (Non-Interest Expense / Revenue)",
                "CET1 Capital Ratio & Tier 1 Leverage",
                "Provision for Credit Losses / Total Loans",
                "Non-Performing Assets (NPA) Ratio",
                "ROTCE & P/TBV",
            ],
            explanation=(
                f"SEC SIC {sic_code} falls in Banking/Credit/Broker-Dealer/Asset Management (6000-6299). "
                "Disabled non-financial metrics (gross margin, inventory turnover/working capital days, "
                "standard EV/EBITDA, standard FCF, interest coverage, and Reverse-DCF) because funding "
                "interest and regulatory balance sheet flows make industrial cash flow and EV multiples invalid."
            ),
        )

    if 6300 <= sic_num <= 6411:
        return SectorApplicability(
            sic_code=sic_code,
            sic_description=sic_description,
            sector_category="insurance",
            is_gated=True,
            disabled_metrics=list(_INSURANCE_DISABLED_METRICS),
            recommended_alternatives=[
                "Combined Ratio (Loss Ratio + Expense Ratio)",
                "Underwriting Margin & Reserve Development",
                "Net Investment Income Yield",
                "RBC / Solvency Capital Adequacy",
                "P/BV ex-AOCI & Operating ROE",
            ],
            explanation=(
                f"SEC SIC {sic_code} falls in Insurance (6300-6411). "
                "Disabled non-financial metrics (gross margin, inventory turnover/working capital days, "
                "standard EV/EBITDA, standard FCF, and Reverse-DCF) because float liabilities, policy "
                "reserves, and investment portfolio income replace industrial COGS and FCF."
            ),
        )

    if sic_num == 6798:
        return SectorApplicability(
            sic_code=sic_code,
            sic_description=sic_description,
            sector_category="reit",
            is_gated=True,
            disabled_metrics=list(_REIT_DISABLED_METRICS),
            recommended_alternatives=[
                "FFO (Funds From Operations) & AFFO (Adjusted FFO)",
                "Same-Store NOI (SSNOI) Growth",
                "Occupancy Rate & Leasing Spreads",
                "Net Debt / EBITDAre & Fixed Charge Coverage",
                "P/FFO, P/AFFO & Implied Cap Rate",
            ],
            explanation=(
                f"SEC SIC {sic_code} identifies a REIT (6798). "
                "Disabled non-financial metrics (gross margin, inventory turnover/DIO/DPO/CCC, "
                "standard EV/EBITDA, standard FCF, and Reverse-DCF) because real-estate depreciation, "
                "development CapEx, and mandatory dividend payouts require FFO/AFFO and NOI valuation."
            ),
        )

    return SectorApplicability(
        sic_code=sic_code,
        sic_description=sic_description,
        sector_category="non_financial",
        is_gated=False,
        disabled_metrics=[],
        recommended_alternatives=[],
        explanation=f"SEC SIC {sic_code} is eligible for full non-financial corporate diagnostics and valuation.",
    )


def _metric_disabled(name: str, unit: str, formula: str, sector: SectorApplicability, *, currency: str | None = None) -> DiagnosticMetric:
    return DiagnosticMetric(
        name=name,
        status="disabled_by_sector",
        value=None,
        unit=unit,
        currency=currency,
        formula=formula,
        reason=sector.explanation,
    )


def _metric_unavailable(
    name: str,
    unit: str,
    formula: str,
    reason: str,
    *,
    currency: str | None = None,
    provenance: Sequence[tuple[str, MetricValue]] = (),
) -> DiagnosticMetric:
    input_ids, xbrl_tags, source_ids = _provenance_lists(provenance)
    return DiagnosticMetric(
        name=name,
        status="unavailable",
        value=None,
        unit=unit,
        currency=currency,
        formula=formula,
        input_ids=input_ids,
        xbrl_tags=xbrl_tags,
        source_ids=source_ids,
        reason=reason,
    )


def _metric_computed(
    name: str,
    value: Decimal,
    unit: str,
    formula: str,
    provenance: Sequence[tuple[str, MetricValue]],
    *,
    decimal_places: int,
    currency: str | None = None,
    reason: str | None = None,
) -> DiagnosticMetric:
    input_ids, xbrl_tags, source_ids = _provenance_lists(provenance)
    return DiagnosticMetric(
        name=name,
        status="computed",
        value=_quantize(value, decimal_places),
        unit=unit,
        currency=currency,
        formula=formula,
        input_ids=input_ids,
        xbrl_tags=xbrl_tags,
        source_ids=source_ids,
        reason=reason,
    )


# ============================================================================
# Engine 1: earnings_quality_diagnostics
# ============================================================================


class PeriodFinancials(BaseModel):
    """Structured financial statement snapshot for one reporting period."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    period_label: str = Field(min_length=1, max_length=100)
    period_type: PeriodType = "quarter"
    period_start: date | None = None
    period_end: date | None = None
    period_days: Decimal | None = None
    currency: str | None = Field(default=None, min_length=1, max_length=16)
    monetary_unit: str | None = Field(default=None, min_length=1, max_length=100)

    # Income Statement
    revenue: MetricValue | None = None
    cost_of_revenue: MetricValue | None = None
    gross_profit: MetricValue | None = None
    rd_expense: MetricValue | None = None
    sga_expense: MetricValue | None = None
    operating_income: MetricValue | None = None
    ebit: MetricValue | None = None
    interest_expense: MetricValue | None = None
    net_income: MetricValue | None = None
    ebitda: MetricValue | None = None
    depreciation_amortization: MetricValue | None = None
    sbc: MetricValue | None = None
    shares_diluted: MetricValue | None = None

    # Cash Flow Statement
    operating_cash_flow: MetricValue | None = None
    capex: MetricValue | None = None
    free_cash_flow: MetricValue | None = None
    share_repurchases: MetricValue | None = None
    dividends_paid: MetricValue | None = None

    # Balance Sheet
    cash: MetricValue | None = None
    short_term_investments: MetricValue | None = None
    accounts_receivable: MetricValue | None = None
    inventory: MetricValue | None = None
    current_assets: MetricValue | None = None
    accounts_payable: MetricValue | None = None
    short_term_debt: MetricValue | None = None
    long_term_debt: MetricValue | None = None
    total_debt: MetricValue | None = None
    current_liabilities: MetricValue | None = None
    minority_interest: MetricValue | None = None

    @field_validator("period_days", mode="before")
    @classmethod
    def _validate_period_days(cls, raw: Any) -> Decimal | None:
        if raw is None:
            return None
        val = _to_decimal(raw, field_name="period_days")
        if val <= 0 or val > Decimal("370"):
            raise DiagnosticsError("period_days must be > 0 and <= 370.")
        return val

    @field_validator("currency")
    @classmethod
    def _norm_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        upper = value.strip().upper()
        return "CNY" if upper == "RMB" else upper

    @model_validator(mode="before")
    @classmethod
    def _coerce_numeric_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        label = str(data.get("period_label") or "period").strip() or "period"
        copied = dict(data)
        metric_fields = (
            "revenue", "cost_of_revenue", "gross_profit", "rd_expense", "sga_expense",
            "operating_income", "ebit", "interest_expense", "net_income", "ebitda",
            "depreciation_amortization", "sbc", "shares_diluted", "operating_cash_flow",
            "capex", "free_cash_flow", "share_repurchases", "dividends_paid", "cash",
            "short_term_investments", "accounts_receivable", "inventory", "current_assets",
            "accounts_payable", "short_term_debt", "long_term_debt", "total_debt",
            "current_liabilities", "minority_interest",
        )
        for fname in metric_fields:
            if fname in copied and copied[fname] is not None:
                copied[fname] = _coerce_metric_value(copied[fname], fallback_fact_id=f"{label}:{fname}")
        return copied

    def effective_period_days(self) -> Decimal:
        if self.period_days is not None:
            return self.period_days
        if self.period_type in ("annual", "ttm"):
            return DEFAULT_ANNUAL_DAYS
        return DEFAULT_QUARTER_DAYS


def period_financials_from_statement_lines(
    lines: Sequence[StatementLine],
    period_end: date,
    *,
    period_label: str | None = None,
    period_type: PeriodType = "quarter",
    sbc: MetricValue | Decimal | str | None = None,
    total_debt: MetricValue | Decimal | str | None = None,
    free_cash_flow: MetricValue | Decimal | str | None = None,
) -> PeriodFinancials:
    """Adapt standard SEC ``StatementLine`` rows for one ``period_end`` into ``PeriodFinancials``."""
    selected: dict[str, StatementLine] = {}
    for item in lines:
        if item.period_end != period_end or not item.value.is_finite():
            continue
        # Prefer non-derived reported lines over derived lines if both exist for the same period_end.
        existing = selected.get(item.line)
        if existing is None or (existing.derived and not item.derived):
            selected[item.line] = item

    if not selected:
        raise DiagnosticsError(f"No StatementLine rows found for period_end={period_end.isoformat()}.")

    sample = next(iter(selected.values()))
    resolved_label = period_label or sample.fiscal_label or period_end.isoformat()
    payload: dict[str, Any] = {
        "period_label": resolved_label,
        "period_type": period_type,
        "period_end": period_end,
        "currency": sample.currency,
    }
    for item in selected.values():
        if item.period_start is not None and "period_start" not in payload:
            payload["period_start"] = item.period_start

    line_map = {
        "revenue": "revenue",
        "cost_of_revenue": "cost_of_revenue",
        "gross_profit": "gross_profit",
        "rd_expense": "rd_expense",
        "sga_expense": "sga_expense",
        "operating_income": "operating_income",
        "interest_expense": "interest_expense",
        "net_income": "net_income",
        "ebitda": "ebitda",
        "depreciation_amortization": "depreciation_amortization",
        "shares_diluted": "shares_diluted",
        "operating_cash_flow": "operating_cash_flow",
        "capex": "capex",
        "share_repurchases": "share_repurchases",
        "dividends_paid": "dividends_paid",
        "cash": "cash",
        "short_term_investments": "short_term_investments",
        "accounts_receivable": "accounts_receivable",
        "inventory": "inventory",
        "current_assets": "current_assets",
        "accounts_payable": "accounts_payable",
        "short_term_debt": "short_term_debt",
        "long_term_debt": "long_term_debt",
        "current_liabilities": "current_liabilities",
        "non_controlling_interests": "minority_interest",
    }
    for src_line, dest_field in line_map.items():
        row = selected.get(src_line)
        if row is None:
            continue
        payload[dest_field] = MetricValue(
            value=row.value,
            fact_id=f"{row.entity_id}:{row.line}:{row.period_end.isoformat()}",
            source_id=row.accession,
            xbrl_tag=f"{row.taxonomy}:{row.reported_tag}",
            unit=row.unit,
            currency=row.currency,
            period=resolved_label,
        )
    if sbc is not None:
        payload["sbc"] = sbc
    if total_debt is not None:
        payload["total_debt"] = total_debt
    if free_cash_flow is not None:
        payload["free_cash_flow"] = free_cash_flow
    return PeriodFinancials.model_validate(payload)


class EarningsQualityRequest(BaseModel):
    """Input payload for ``earnings_quality_diagnostics``."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    entity_id: str | None = Field(default=None, max_length=120)
    symbol: str | None = Field(default=None, max_length=32)
    sic: str | int | None = None
    sic_description: str | None = Field(default=None, max_length=200)
    current: PeriodFinancials
    prior_qoq: PeriodFinancials | None = None
    prior_yoy: PeriodFinancials | None = None
    ttm: PeriodFinancials | None = None
    trailing_quarters: list[PeriodFinancials] | None = Field(default=None, min_length=4, max_length=4)
    decimal_places: int = Field(default=2, ge=0, le=6)


class MarginBridgeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line: Literal["gross_margin", "rd_pct", "sga_pct", "operating_margin", "net_margin"]
    current_pct: DiagnosticMetric
    prior_qoq_pct: DiagnosticMetric | None = None
    prior_yoy_pct: DiagnosticMetric | None = None
    qoq_change_bps: DiagnosticMetric | None = None
    yoy_change_bps: DiagnosticMetric | None = None


class EarningsQualityCashFlowGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reported_or_derived_fcf: DiagnosticMetric
    ocf_to_ni: DiagnosticMetric
    fcf_to_ni: DiagnosticMetric
    fcf_margin_pct: DiagnosticMetric
    sbc_to_revenue_pct: DiagnosticMetric
    sbc_to_fcf_pct: DiagnosticMetric
    fcf_ex_sbc: DiagnosticMetric
    fcf_ex_sbc_margin_pct: DiagnosticMetric


class WorkingCapitalItemWithDeltas(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current: DiagnosticMetric
    prior_qoq: DiagnosticMetric | None = None
    prior_yoy: DiagnosticMetric | None = None
    qoq_delta: DiagnosticMetric | None = None
    yoy_delta: DiagnosticMetric | None = None


class WorkingCapitalForensicsGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ar_yoy_growth_pct: DiagnosticMetric
    revenue_yoy_growth_pct: DiagnosticMetric
    ar_vs_revenue_growth_pp: DiagnosticMetric
    inventory_yoy_growth_pct: DiagnosticMetric
    cogs_yoy_growth_pct: DiagnosticMetric
    inventory_vs_cogs_growth_pp: DiagnosticMetric
    inventory_vs_revenue_growth_pp: DiagnosticMetric
    inventory_turnover: WorkingCapitalItemWithDeltas
    dso: WorkingCapitalItemWithDeltas
    dio: WorkingCapitalItemWithDeltas
    dpo: WorkingCapitalItemWithDeltas
    ccc: WorkingCapitalItemWithDeltas


class CapitalAllocationGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capex_to_revenue_pct: DiagnosticMetric
    capital_returned: DiagnosticMetric
    payout_to_fcf_pct: DiagnosticMetric
    diluted_shares_yoy_pct: DiagnosticMetric


class BalanceSheetLeverageGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_cash: DiagnosticMetric
    total_debt: DiagnosticMetric
    net_debt: DiagnosticMetric
    net_cash: DiagnosticMetric
    position: Literal["net_cash", "net_debt", "balanced", "unavailable"]
    net_debt_to_ttm_ebitda: DiagnosticMetric
    interest_coverage: DiagnosticMetric
    current_ratio: DiagnosticMetric


class DiagnosticFlag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flag_id: str
    group: Literal["margin_bridge", "earnings_quality", "working_capital", "capital_allocation", "balance_sheet"]
    severity: FlagSeverity
    metric_name: str
    observed_value: Decimal
    unit: str
    threshold_rule: str
    summary: str
    investigation_prompt: str = Field(min_length=10)
    input_ids: list[str] = Field(default_factory=list)


class EarningsQualityDiagnosticsResult(BaseModel):
    """Output of ``earnings_quality_diagnostics`` (strictly zero black-box score)."""

    model_config = ConfigDict(extra="forbid")

    entity_id: str | None = None
    symbol: str | None = None
    period_label: str
    monetary_unit: str
    currency: str | None = None
    sector_applicability: SectorApplicability
    margin_bridge: list[MarginBridgeItem]
    earnings_quality: EarningsQualityCashFlowGroup
    working_capital: WorkingCapitalForensicsGroup
    capital_allocation: CapitalAllocationGroup
    balance_sheet: BalanceSheetLeverageGroup
    flags: list[DiagnosticFlag]
    flag_counts: dict[FlagSeverity, int]
    thresholds_reference: dict[str, dict[str, str]]


class _PeriodAccessor:
    """Helper binding a PeriodFinancials to its aligned monetary and share values."""

    def __init__(
        self,
        tag: str,
        period: PeriodFinancials,
        money_map: dict[tuple[str, str], Decimal],
        shares_map: dict[str, Decimal],
    ) -> None:
        self.tag = tag
        self.period = period
        self.money_map = money_map
        self.shares_map = shares_map

    def val(self, field: str) -> Decimal | None:
        return self.money_map.get((self.tag, field))

    def mv(self, field: str) -> MetricValue | None:
        return getattr(self.period, field)

    def pair(self, field: str) -> tuple[str, MetricValue] | None:
        item = self.mv(field)
        return (f"{self.period.period_label}.{field}", item) if item is not None else None

    def shares(self) -> Decimal | None:
        return self.shares_map.get(self.tag)

    def gross_profit_resolved(self) -> tuple[Decimal | None, list[tuple[str, MetricValue]], str]:
        gp = self.val("gross_profit")
        if gp is not None:
            p = self.pair("gross_profit")
            return gp, ([p] if p else []), "gross_profit"
        rev = self.val("revenue")
        cogs = self.val("cost_of_revenue")
        if rev is not None and cogs is not None:
            prov = [x for x in (self.pair("revenue"), self.pair("cost_of_revenue")) if x is not None]
            return rev - cogs, prov, "(revenue - cost_of_revenue)"
        return None, [], "gross_profit"

    def fcf_resolved(self) -> tuple[Decimal | None, list[tuple[str, MetricValue]], str, str | None]:
        fcf = self.val("free_cash_flow")
        if fcf is not None:
            p = self.pair("free_cash_flow")
            return fcf, ([p] if p else []), "free_cash_flow", None
        ocf = self.val("operating_cash_flow")
        capex = self.val("capex")
        if ocf is not None and capex is not None:
            prov = [x for x in (self.pair("operating_cash_flow"), self.pair("capex")) if x is not None]
            return ocf - abs(capex), prov, "operating_cash_flow - abs(capex)", None
        missing = "operating_cash_flow" if ocf is None else "capex"
        return None, [], "operating_cash_flow - abs(capex)", f"Missing {missing} (or free_cash_flow)."

    def ebit_resolved(self) -> tuple[Decimal | None, list[tuple[str, MetricValue]], str]:
        ebit = self.val("ebit")
        if ebit is not None:
            p = self.pair("ebit")
            return ebit, ([p] if p else []), "ebit"
        op_inc = self.val("operating_income")
        if op_inc is not None:
            p = self.pair("operating_income")
            return op_inc, ([p] if p else []), "operating_income"
        return None, [], "operating_income"

    def ebitda_resolved(self) -> tuple[Decimal | None, list[tuple[str, MetricValue]], str]:
        ebitda = self.val("ebitda")
        if ebitda is not None:
            p = self.pair("ebitda")
            return ebitda, ([p] if p else []), "ebitda"
        ebit, ebit_prov, ebit_expr = self.ebit_resolved()
        da = self.val("depreciation_amortization")
        if ebit is not None and da is not None:
            p_da = self.pair("depreciation_amortization")
            prov = [*ebit_prov, *([p_da] if p_da else [])]
            return ebit + abs(da), prov, f"{ebit_expr} + abs(depreciation_amortization)"
        return None, [], "ebitda"


def _compute_margin_ratio(
    acc: _PeriodAccessor,
    line: Literal["gross_margin", "rd_pct", "sga_pct", "operating_margin", "net_margin"],
) -> tuple[Decimal | None, list[tuple[str, MetricValue]], str, str | None]:
    rev = acc.val("revenue")
    rev_pair = acc.pair("revenue")
    if rev is None or rev_pair is None:
        return None, [], f"{line} / revenue * 100", f"Missing revenue for {acc.period.period_label}."
    if rev <= 0:
        return None, [rev_pair], f"{line} / revenue * 100", f"Revenue <= 0 for {acc.period.period_label}; margin undefined."

    if line == "gross_margin":
        num, num_prov, num_expr = acc.gross_profit_resolved()
        if num is None:
            return None, [rev_pair], "gross_profit / revenue * 100", f"Missing gross_profit and cost_of_revenue for {acc.period.period_label}."
    else:
        field_name = {
            "rd_pct": "rd_expense",
            "sga_pct": "sga_expense",
            "operating_margin": "operating_income",
            "net_margin": "net_income",
        }[line]
        num = acc.val(field_name)
        p = acc.pair(field_name)
        num_prov = [p] if p else []
        num_expr = field_name
        if num is None:
            return None, [rev_pair], f"{field_name} / revenue * 100", f"Missing {field_name} for {acc.period.period_label}."

    prov = [*num_prov, rev_pair]
    formula = f"({num_expr} / revenue) * 100"
    with localcontext() as ctx:
        ctx.prec = 100
        ratio = num / rev
    return ratio, prov, formula, None


def _build_margin_bridge(
    cur: _PeriodAccessor,
    qoq: _PeriodAccessor | None,
    yoy: _PeriodAccessor | None,
    sector: SectorApplicability,
    decimal_places: int,
) -> list[MarginBridgeItem]:
    lines: tuple[Literal["gross_margin", "rd_pct", "sga_pct", "operating_margin", "net_margin"], ...] = (
        "gross_margin",
        "rd_pct",
        "sga_pct",
        "operating_margin",
        "net_margin",
    )
    items: list[MarginBridgeItem] = []
    disabled_set = set(sector.disabled_metrics)

    for line in lines:
        if line in disabled_set:
            items.append(
                MarginBridgeItem(
                    line=line,
                    current_pct=_metric_disabled(line, "percent", f"({line}_num / revenue) * 100", sector),
                    prior_qoq_pct=_metric_disabled(f"{line}_qoq", "percent", f"({line}_num / revenue) * 100", sector)
                    if qoq is not None
                    else None,
                    prior_yoy_pct=_metric_disabled(f"{line}_yoy", "percent", f"({line}_num / revenue) * 100", sector)
                    if yoy is not None
                    else None,
                    qoq_change_bps=_metric_disabled(f"{line}_qoq_bps", "bps", "(cur_ratio - qoq_ratio) * 10000", sector)
                    if qoq is not None
                    else None,
                    yoy_change_bps=_metric_disabled(f"{line}_yoy_bps", "bps", "(cur_ratio - yoy_ratio) * 10000", sector)
                    if yoy is not None
                    else None,
                )
            )
            continue

        cur_ratio, cur_prov, cur_formula, cur_err = _compute_margin_ratio(cur, line)
        cur_metric = (
            _metric_computed(line, cur_ratio * Decimal(100), "percent", cur_formula, cur_prov, decimal_places=decimal_places)
            if cur_ratio is not None
            else _metric_unavailable(line, "percent", cur_formula, cur_err or "Unavailable", provenance=cur_prov)
        )

        qoq_metric: DiagnosticMetric | None = None
        qoq_bps: DiagnosticMetric | None = None
        if qoq is not None:
            q_ratio, q_prov, q_formula, q_err = _compute_margin_ratio(qoq, line)
            qoq_metric = (
                _metric_computed(
                    f"{line}_qoq", q_ratio * Decimal(100), "percent", q_formula, q_prov, decimal_places=decimal_places
                )
                if q_ratio is not None
                else _metric_unavailable(f"{line}_qoq", "percent", q_formula, q_err or "Unavailable", provenance=q_prov)
            )
            bps_formula = f"({line}_cur_ratio - {line}_qoq_ratio) * 10000"
            if cur_ratio is not None and q_ratio is not None:
                with localcontext() as ctx:
                    ctx.prec = 100
                    diff_bps = (cur_ratio - q_ratio) * Decimal(10000)
                qoq_bps = _metric_computed(
                    f"{line}_qoq_bps", diff_bps, "bps", bps_formula, [*cur_prov, *q_prov], decimal_places=decimal_places
                )
            else:
                qoq_bps = _metric_unavailable(
                    f"{line}_qoq_bps",
                    "bps",
                    bps_formula,
                    cur_err or q_err or "Missing current or QoQ ratio.",
                    provenance=[*cur_prov, *q_prov],
                )

        yoy_metric: DiagnosticMetric | None = None
        yoy_bps: DiagnosticMetric | None = None
        if yoy is not None:
            y_ratio, y_prov, y_formula, y_err = _compute_margin_ratio(yoy, line)
            yoy_metric = (
                _metric_computed(
                    f"{line}_yoy", y_ratio * Decimal(100), "percent", y_formula, y_prov, decimal_places=decimal_places
                )
                if y_ratio is not None
                else _metric_unavailable(f"{line}_yoy", "percent", y_formula, y_err or "Unavailable", provenance=y_prov)
            )
            bps_formula = f"({line}_cur_ratio - {line}_yoy_ratio) * 10000"
            if cur_ratio is not None and y_ratio is not None:
                with localcontext() as ctx:
                    ctx.prec = 100
                    diff_bps = (cur_ratio - y_ratio) * Decimal(10000)
                yoy_bps = _metric_computed(
                    f"{line}_yoy_bps", diff_bps, "bps", bps_formula, [*cur_prov, *y_prov], decimal_places=decimal_places
                )
            else:
                yoy_bps = _metric_unavailable(
                    f"{line}_yoy_bps",
                    "bps",
                    bps_formula,
                    cur_err or y_err or "Missing current or YoY ratio.",
                    provenance=[*cur_prov, *y_prov],
                )

        items.append(
            MarginBridgeItem(
                line=line,
                current_pct=cur_metric,
                prior_qoq_pct=qoq_metric,
                prior_yoy_pct=yoy_metric,
                qoq_change_bps=qoq_bps,
                yoy_change_bps=yoy_bps,
            )
        )
    return items


def _build_earnings_quality_group(
    cur: _PeriodAccessor,
    sector: SectorApplicability,
    *,
    monetary_unit: str,
    currency: str | None,
    decimal_places: int,
) -> EarningsQualityCashFlowGroup:
    disabled = set(sector.disabled_metrics)

    def maybe_disabled(name: str, unit: str, formula: str, *, curr: str | None = None) -> DiagnosticMetric | None:
        if name in disabled or ("standard_fcf" in disabled and ("fcf" in name or name == "free_cash_flow")):
            return _metric_disabled(name, unit, formula, sector, currency=curr)
        return None

    rev = cur.val("revenue")
    rev_pair = cur.pair("revenue")
    ni = cur.val("net_income")
    ni_pair = cur.pair("net_income")
    ocf = cur.val("operating_cash_flow")
    ocf_pair = cur.pair("operating_cash_flow")
    sbc = cur.val("sbc")
    sbc_pair = cur.pair("sbc")
    fcf, fcf_prov, fcf_formula, fcf_err = cur.fcf_resolved()

    # 1. Reported or derived FCF
    fcf_metric = maybe_disabled("free_cash_flow", monetary_unit, fcf_formula, curr=currency)
    if fcf_metric is None:
        if fcf is not None:
            fcf_metric = _metric_computed(
                "free_cash_flow", fcf, monetary_unit, fcf_formula, fcf_prov, decimal_places=decimal_places, currency=currency
            )
        else:
            fcf_metric = _metric_unavailable(
                "free_cash_flow", monetary_unit, fcf_formula, fcf_err or "FCF inputs missing.", currency=currency, provenance=fcf_prov
            )

    # 2. OCF / NI
    ocf_to_ni = maybe_disabled("ocf_to_ni", "ratio", "operating_cash_flow / net_income")
    if ocf_to_ni is None:
        if ocf is None or ocf_pair is None or ni is None or ni_pair is None:
            prov = [p for p in (ocf_pair, ni_pair) if p is not None]
            ocf_to_ni = _metric_unavailable(
                "ocf_to_ni", "ratio", "operating_cash_flow / net_income", "Missing operating_cash_flow or net_income.", provenance=prov
            )
        elif ni <= 0:
            ocf_to_ni = _metric_unavailable(
                "ocf_to_ni", "ratio", "operating_cash_flow / net_income", "Net income <= 0; OCF/NI cash conversion ratio is not meaningful.", provenance=[ocf_pair, ni_pair]
            )
        else:
            with localcontext() as ctx:
                ctx.prec = 100
                ocf_to_ni = _metric_computed(
                    "ocf_to_ni", ocf / ni, "ratio", "operating_cash_flow / net_income", [ocf_pair, ni_pair], decimal_places=decimal_places
                )

    # 3. FCF / NI
    fcf_to_ni = maybe_disabled("fcf_to_ni", "ratio", f"({fcf_formula}) / net_income")
    if fcf_to_ni is None:
        if fcf is None or ni is None or ni_pair is None:
            prov = [*fcf_prov, *([ni_pair] if ni_pair else [])]
            fcf_to_ni = _metric_unavailable(
                "fcf_to_ni", "ratio", f"({fcf_formula}) / net_income", fcf_err or "Missing net_income.", provenance=prov
            )
        elif ni <= 0:
            fcf_to_ni = _metric_unavailable(
                "fcf_to_ni", "ratio", f"({fcf_formula}) / net_income", "Net income <= 0; FCF/NI ratio is not meaningful.", provenance=[*fcf_prov, ni_pair]
            )
        else:
            with localcontext() as ctx:
                ctx.prec = 100
                fcf_to_ni = _metric_computed(
                    "fcf_to_ni", fcf / ni, "ratio", f"({fcf_formula}) / net_income", [*fcf_prov, ni_pair], decimal_places=decimal_places
                )

    # 4. FCF Margin
    fcf_margin = maybe_disabled("fcf_margin_pct", "percent", f"({fcf_formula}) / revenue * 100")
    if fcf_margin is None:
        if fcf is None or rev is None or rev_pair is None:
            fcf_margin = _metric_unavailable(
                "fcf_margin_pct", "percent", f"({fcf_formula}) / revenue * 100", fcf_err or "Missing revenue.", provenance=[*fcf_prov, *([rev_pair] if rev_pair else [])]
            )
        elif rev <= 0:
            fcf_margin = _metric_unavailable(
                "fcf_margin_pct", "percent", f"({fcf_formula}) / revenue * 100", "Revenue <= 0; FCF margin undefined.", provenance=[*fcf_prov, rev_pair]
            )
        else:
            with localcontext() as ctx:
                ctx.prec = 100
                fcf_margin = _metric_computed(
                    "fcf_margin_pct", (fcf / rev) * Decimal(100), "percent", f"({fcf_formula}) / revenue * 100", [*fcf_prov, rev_pair], decimal_places=decimal_places
                )

    # 5. SBC / Revenue
    sbc_to_rev = maybe_disabled("sbc_to_revenue_pct", "percent", "(sbc / revenue) * 100")
    if sbc_to_rev is None:
        if sbc is None or sbc_pair is None or rev is None or rev_pair is None:
            prov = [p for p in (sbc_pair, rev_pair) if p is not None]
            sbc_to_rev = _metric_unavailable(
                "sbc_to_revenue_pct", "percent", "(sbc / revenue) * 100", "Missing sbc or revenue.", provenance=prov
            )
        elif rev <= 0:
            sbc_to_rev = _metric_unavailable(
                "sbc_to_revenue_pct", "percent", "(sbc / revenue) * 100", "Revenue <= 0; SBC/Revenue undefined.", provenance=[sbc_pair, rev_pair]
            )
        else:
            with localcontext() as ctx:
                ctx.prec = 100
                sbc_to_rev = _metric_computed(
                    "sbc_to_revenue_pct", (sbc / rev) * Decimal(100), "percent", "(sbc / revenue) * 100", [sbc_pair, rev_pair], decimal_places=decimal_places
                )

    # 6. SBC / FCF
    sbc_to_fcf = maybe_disabled("sbc_to_fcf_pct", "percent", f"sbc / ({fcf_formula}) * 100")
    if sbc_to_fcf is None:
        if sbc is None or sbc_pair is None or fcf is None:
            prov = [*([sbc_pair] if sbc_pair else []), *fcf_prov]
            sbc_to_fcf = _metric_unavailable(
                "sbc_to_fcf_pct", "percent", f"sbc / ({fcf_formula}) * 100", "Missing sbc or FCF.", provenance=prov
            )
        elif fcf <= 0:
            sbc_to_fcf = _metric_unavailable(
                "sbc_to_fcf_pct", "percent", f"sbc / ({fcf_formula}) * 100", "FCF <= 0; SBC/FCF percentage is not meaningful (inspect FCF ex-SBC).", provenance=[sbc_pair, *fcf_prov]
            )
        else:
            with localcontext() as ctx:
                ctx.prec = 100
                sbc_to_fcf = _metric_computed(
                    "sbc_to_fcf_pct", (sbc / fcf) * Decimal(100), "percent", f"sbc / ({fcf_formula}) * 100", [sbc_pair, *fcf_prov], decimal_places=decimal_places
                )

    # 7. FCF ex-SBC & FCF ex-SBC Margin
    fcf_ex_sbc_formula = f"({fcf_formula}) - sbc"
    fcf_ex_sbc = maybe_disabled("fcf_ex_sbc", monetary_unit, fcf_ex_sbc_formula, curr=currency)
    fcf_ex_sbc_margin = maybe_disabled("fcf_ex_sbc_margin_pct", "percent", f"({fcf_ex_sbc_formula}) / revenue * 100")
    if fcf_ex_sbc is None:
        if fcf is not None and sbc is not None and sbc_pair is not None:
            val_ex = fcf - sbc
            prov_ex = [*fcf_prov, sbc_pair]
            fcf_ex_sbc = _metric_computed(
                "fcf_ex_sbc", val_ex, monetary_unit, fcf_ex_sbc_formula, prov_ex, decimal_places=decimal_places, currency=currency
            )
            if fcf_ex_sbc_margin is None:
                if rev is not None and rev_pair is not None and rev > 0:
                    with localcontext() as ctx:
                        ctx.prec = 100
                        fcf_ex_sbc_margin = _metric_computed(
                            "fcf_ex_sbc_margin_pct", (val_ex / rev) * Decimal(100), "percent", f"({fcf_ex_sbc_formula}) / revenue * 100", [*prov_ex, rev_pair], decimal_places=decimal_places
                        )
                else:
                    fcf_ex_sbc_margin = _metric_unavailable(
                        "fcf_ex_sbc_margin_pct", "percent", f"({fcf_ex_sbc_formula}) / revenue * 100", "Missing or non-positive revenue.", provenance=prov_ex
                    )
        else:
            prov_ex = [*fcf_prov, *([sbc_pair] if sbc_pair else [])]
            reason = fcf_err or "Stock-based compensation (sbc) not provided."
            fcf_ex_sbc = _metric_unavailable("fcf_ex_sbc", monetary_unit, fcf_ex_sbc_formula, reason, currency=currency, provenance=prov_ex)
            if fcf_ex_sbc_margin is None:
                fcf_ex_sbc_margin = _metric_unavailable(
                    "fcf_ex_sbc_margin_pct", "percent", f"({fcf_ex_sbc_formula}) / revenue * 100", reason, provenance=prov_ex
                )

    return EarningsQualityCashFlowGroup(
        reported_or_derived_fcf=fcf_metric,
        ocf_to_ni=ocf_to_ni,
        fcf_to_ni=fcf_to_ni,
        fcf_margin_pct=fcf_margin,
        sbc_to_revenue_pct=sbc_to_rev,
        sbc_to_fcf_pct=sbc_to_fcf,
        fcf_ex_sbc=fcf_ex_sbc,
        fcf_ex_sbc_margin_pct=fcf_ex_sbc_margin,
    )


def _yoy_growth_metric(
    name: str,
    cur: _PeriodAccessor,
    yoy: _PeriodAccessor | None,
    field: str,
    decimal_places: int,
) -> tuple[DiagnosticMetric, Decimal | None, list[tuple[str, MetricValue]]]:
    formula = f"({field}_cur / {field}_yoy - 1) * 100"
    if yoy is None:
        return _metric_unavailable(name, "percent", formula, "Prior YoY period not provided."), None, []
    cur_v, cur_p = cur.val(field), cur.pair(field)
    yoy_v, yoy_p = yoy.val(field), yoy.pair(field)
    prov = [p for p in (cur_p, yoy_p) if p is not None]
    if cur_v is None or yoy_v is None:
        return _metric_unavailable(name, "percent", formula, f"Missing {field} in current or YoY period.", provenance=prov), None, prov
    if yoy_v <= 0:
        return _metric_unavailable(name, "percent", formula, f"Prior YoY {field} <= 0; growth rate undefined.", provenance=prov), None, prov
    with localcontext() as ctx:
        ctx.prec = 100
        g_pct = (cur_v / yoy_v - Decimal(1)) * Decimal(100)
    return _metric_computed(name, g_pct, "percent", formula, prov, decimal_places=decimal_places), g_pct, prov


def _wc_days_value(
    acc: _PeriodAccessor,
    metric: Literal["inventory_turnover", "dso", "dio", "dpo"],
) -> tuple[Decimal | None, list[tuple[str, MetricValue]], str, str | None]:
    p_days = acc.period.effective_period_days()
    days_str = str(p_days)
    if metric == "dso":
        ar, ar_p = acc.val("accounts_receivable"), acc.pair("accounts_receivable")
        rev, rev_p = acc.val("revenue"), acc.pair("revenue")
        prov = [p for p in (ar_p, rev_p) if p is not None]
        formula = f"(accounts_receivable / revenue) * {days_str}"
        if ar is None or rev is None:
            return None, prov, formula, f"Missing accounts_receivable or revenue for {acc.period.period_label}."
        if rev <= 0:
            return None, prov, formula, f"Revenue <= 0 for {acc.period.period_label}."
        with localcontext() as ctx:
            ctx.prec = 100
            return (ar / rev) * p_days, prov, formula, None

    cogs, cogs_p = acc.val("cost_of_revenue"), acc.pair("cost_of_revenue")
    if metric == "dio":
        inv, inv_p = acc.val("inventory"), acc.pair("inventory")
        prov = [p for p in (inv_p, cogs_p) if p is not None]
        formula = f"(inventory / cost_of_revenue) * {days_str}"
        if inv is None or cogs is None:
            return None, prov, formula, f"Missing inventory or cost_of_revenue for {acc.period.period_label}."
        if cogs <= 0:
            return None, prov, formula, f"cost_of_revenue <= 0 for {acc.period.period_label}."
        with localcontext() as ctx:
            ctx.prec = 100
            return (inv / cogs) * p_days, prov, formula, None

    if metric == "inventory_turnover":
        inv, inv_p = acc.val("inventory"), acc.pair("inventory")
        prov = [p for p in (cogs_p, inv_p) if p is not None]
        formula = f"(cost_of_revenue * (365 / {days_str})) / inventory"
        if inv is None or cogs is None:
            return None, prov, formula, f"Missing inventory or cost_of_revenue for {acc.period.period_label}."
        if inv <= 0:
            return None, prov, formula, f"Inventory <= 0 for {acc.period.period_label}; turnover undefined."
        with localcontext() as ctx:
            ctx.prec = 100
            ann_cogs = cogs * (DEFAULT_ANNUAL_DAYS / p_days)
            return ann_cogs / inv, prov, formula, None

    # dpo
    ap, ap_p = acc.val("accounts_payable"), acc.pair("accounts_payable")
    prov = [p for p in (ap_p, cogs_p) if p is not None]
    formula = f"(accounts_payable / cost_of_revenue) * {days_str}"
    if ap is None or cogs is None:
        return None, prov, formula, f"Missing accounts_payable or cost_of_revenue for {acc.period.period_label}."
    if cogs <= 0:
        return None, prov, formula, f"cost_of_revenue <= 0 for {acc.period.period_label}."
    with localcontext() as ctx:
        ctx.prec = 100
        return (ap / cogs) * p_days, prov, formula, None


def _build_wc_series(
    metric: Literal["inventory_turnover", "dso", "dio", "dpo", "ccc"],
    unit: str,
    cur: _PeriodAccessor,
    qoq: _PeriodAccessor | None,
    yoy: _PeriodAccessor | None,
    sector: SectorApplicability,
    decimal_places: int,
) -> WorkingCapitalItemWithDeltas:
    disabled = set(sector.disabled_metrics)
    delta_unit = "turns" if metric == "inventory_turnover" else "days"
    base_formula = "DSO + DIO - DPO" if metric == "ccc" else f"{metric}_formula"

    if metric in disabled:
        return WorkingCapitalItemWithDeltas(
            current=_metric_disabled(metric, unit, base_formula, sector),
            prior_qoq=_metric_disabled(f"{metric}_qoq", unit, base_formula, sector) if qoq is not None else None,
            prior_yoy=_metric_disabled(f"{metric}_yoy", unit, base_formula, sector) if yoy is not None else None,
            qoq_delta=_metric_disabled(f"{metric}_qoq_delta", delta_unit, f"{metric}_cur - {metric}_qoq", sector)
            if qoq is not None
            else None,
            yoy_delta=_metric_disabled(f"{metric}_yoy_delta", delta_unit, f"{metric}_cur - {metric}_yoy", sector)
            if yoy is not None
            else None,
        )

    def eval_period(acc: _PeriodAccessor) -> tuple[Decimal | None, list[tuple[str, MetricValue]], str, str | None]:
        if metric != "ccc":
            return _wc_days_value(acc, metric)
        dso_v, dso_p, _, dso_e = _wc_days_value(acc, "dso")
        dio_v, dio_p, _, dio_e = _wc_days_value(acc, "dio")
        dpo_v, dpo_p, _, dpo_e = _wc_days_value(acc, "dpo")
        prov = [*dso_p, *dio_p, *dpo_p]
        if dso_v is None or dio_v is None or dpo_v is None:
            errs = "; ".join(e for e in (dso_e, dio_e, dpo_e) if e)
            return None, prov, "DSO + DIO - DPO", errs
        return dso_v + dio_v - dpo_v, prov, "DSO + DIO - DPO", None

    cur_v, cur_p, cur_f, cur_e = eval_period(cur)
    cur_m = (
        _metric_computed(metric, cur_v, unit, cur_f, cur_p, decimal_places=decimal_places)
        if cur_v is not None
        else _metric_unavailable(metric, unit, cur_f, cur_e or "Unavailable", provenance=cur_p)
    )

    def eval_delta(comp: _PeriodAccessor | None, suffix: str) -> tuple[DiagnosticMetric | None, DiagnosticMetric | None]:
        if comp is None:
            return None, None
        c_v, c_p, c_f, c_e = eval_period(comp)
        comp_m = (
            _metric_computed(f"{metric}_{suffix}", c_v, unit, c_f, c_p, decimal_places=decimal_places)
            if c_v is not None
            else _metric_unavailable(f"{metric}_{suffix}", unit, c_f, c_e or "Unavailable", provenance=c_p)
        )
        d_formula = f"{metric}_cur - {metric}_{suffix}"
        if cur_v is not None and c_v is not None:
            delta_m = _metric_computed(
                f"{metric}_{suffix}_delta", cur_v - c_v, delta_unit, d_formula, [*cur_p, *c_p], decimal_places=decimal_places
            )
        else:
            delta_m = _metric_unavailable(
                f"{metric}_{suffix}_delta",
                delta_unit,
                d_formula,
                cur_e or c_e or "Missing period value.",
                provenance=[*cur_p, *c_p],
            )
        return comp_m, delta_m

    qoq_m, qoq_d = eval_delta(qoq, "qoq")
    yoy_m, yoy_d = eval_delta(yoy, "yoy")
    return WorkingCapitalItemWithDeltas(current=cur_m, prior_qoq=qoq_m, prior_yoy=yoy_m, qoq_delta=qoq_d, yoy_delta=yoy_d)


def _build_working_capital_group(
    cur: _PeriodAccessor,
    qoq: _PeriodAccessor | None,
    yoy: _PeriodAccessor | None,
    sector: SectorApplicability,
    decimal_places: int,
) -> WorkingCapitalForensicsGroup:
    disabled = set(sector.disabled_metrics)

    rev_g_m, rev_g_v, rev_g_p = _yoy_growth_metric("revenue_yoy_growth_pct", cur, yoy, "revenue", decimal_places)

    if "ar_vs_revenue_growth_pp" in disabled:
        ar_g_m = _metric_disabled("ar_yoy_growth_pct", "percent", "(ar_cur / ar_yoy - 1) * 100", sector)
        ar_gap_m = _metric_disabled("ar_vs_revenue_growth_pp", "pp", "ar_yoy_growth_pct - revenue_yoy_growth_pct", sector)
    else:
        ar_g_m, ar_g_v, ar_g_p = _yoy_growth_metric("ar_yoy_growth_pct", cur, yoy, "accounts_receivable", decimal_places)
        gap_formula = "ar_yoy_growth_pct - revenue_yoy_growth_pct"
        if ar_g_v is not None and rev_g_v is not None:
            ar_gap_m = _metric_computed(
                "ar_vs_revenue_growth_pp", ar_g_v - rev_g_v, "pp", gap_formula, [*ar_g_p, *rev_g_p], decimal_places=decimal_places
            )
        else:
            ar_gap_m = _metric_unavailable(
                "ar_vs_revenue_growth_pp",
                "pp",
                gap_formula,
                ar_g_m.reason or rev_g_m.reason or "Missing AR or revenue YoY growth.",
                provenance=[*ar_g_p, *rev_g_p],
            )

    if "inventory_vs_cogs_growth_pp" in disabled:
        inv_g_m = _metric_disabled("inventory_yoy_growth_pct", "percent", "(inventory_cur / inventory_yoy - 1) * 100", sector)
        cogs_g_m = _metric_disabled("cogs_yoy_growth_pct", "percent", "(cogs_cur / cogs_yoy - 1) * 100", sector)
        inv_cogs_gap = _metric_disabled("inventory_vs_cogs_growth_pp", "pp", "inventory_yoy_growth_pct - cogs_yoy_growth_pct", sector)
        inv_rev_gap = _metric_disabled("inventory_vs_revenue_growth_pp", "pp", "inventory_yoy_growth_pct - revenue_yoy_growth_pct", sector)
    else:
        inv_g_m, inv_g_v, inv_g_p = _yoy_growth_metric("inventory_yoy_growth_pct", cur, yoy, "inventory", decimal_places)
        cogs_g_m, cogs_g_v, cogs_g_p = _yoy_growth_metric("cogs_yoy_growth_pct", cur, yoy, "cost_of_revenue", decimal_places)
        if inv_g_v is not None and cogs_g_v is not None:
            inv_cogs_gap = _metric_computed(
                "inventory_vs_cogs_growth_pp",
                inv_g_v - cogs_g_v,
                "pp",
                "inventory_yoy_growth_pct - cogs_yoy_growth_pct",
                [*inv_g_p, *cogs_g_p],
                decimal_places=decimal_places,
            )
        else:
            inv_cogs_gap = _metric_unavailable(
                "inventory_vs_cogs_growth_pp",
                "pp",
                "inventory_yoy_growth_pct - cogs_yoy_growth_pct",
                inv_g_m.reason or cogs_g_m.reason or "Missing inventory or COGS YoY growth.",
                provenance=[*inv_g_p, *cogs_g_p],
            )
        if inv_g_v is not None and rev_g_v is not None:
            inv_rev_gap = _metric_computed(
                "inventory_vs_revenue_growth_pp",
                inv_g_v - rev_g_v,
                "pp",
                "inventory_yoy_growth_pct - revenue_yoy_growth_pct",
                [*inv_g_p, *rev_g_p],
                decimal_places=decimal_places,
            )
        else:
            inv_rev_gap = _metric_unavailable(
                "inventory_vs_revenue_growth_pp",
                "pp",
                "inventory_yoy_growth_pct - revenue_yoy_growth_pct",
                inv_g_m.reason or rev_g_m.reason or "Missing inventory or revenue YoY growth.",
                provenance=[*inv_g_p, *rev_g_p],
            )

    return WorkingCapitalForensicsGroup(
        ar_yoy_growth_pct=ar_g_m,
        revenue_yoy_growth_pct=rev_g_m,
        ar_vs_revenue_growth_pp=ar_gap_m,
        inventory_yoy_growth_pct=inv_g_m,
        cogs_yoy_growth_pct=cogs_g_m,
        inventory_vs_cogs_growth_pp=inv_cogs_gap,
        inventory_vs_revenue_growth_pp=inv_rev_gap,
        inventory_turnover=_build_wc_series("inventory_turnover", "turns", cur, qoq, yoy, sector, decimal_places),
        dso=_build_wc_series("dso", "days", cur, qoq, yoy, sector, decimal_places),
        dio=_build_wc_series("dio", "days", cur, qoq, yoy, sector, decimal_places),
        dpo=_build_wc_series("dpo", "days", cur, qoq, yoy, sector, decimal_places),
        ccc=_build_wc_series("ccc", "days", cur, qoq, yoy, sector, decimal_places),
    )


def _build_capital_allocation_group(
    cur: _PeriodAccessor,
    yoy: _PeriodAccessor | None,
    sector: SectorApplicability,
    *,
    monetary_unit: str,
    currency: str | None,
    decimal_places: int,
) -> CapitalAllocationGroup:
    disabled = set(sector.disabled_metrics)
    rev = cur.val("revenue")
    rev_p = cur.pair("revenue")
    capex = cur.val("capex")
    capex_p = cur.pair("capex")

    if "capex_to_revenue_pct" in disabled:
        capex_rev = _metric_disabled("capex_to_revenue_pct", "percent", "(abs(capex) / revenue) * 100", sector)
    elif capex is None or capex_p is None or rev is None or rev_p is None:
        capex_rev = _metric_unavailable(
            "capex_to_revenue_pct", "percent", "(abs(capex) / revenue) * 100", "Missing capex or revenue.", provenance=[p for p in (capex_p, rev_p) if p]
        )
    elif rev <= 0:
        capex_rev = _metric_unavailable(
            "capex_to_revenue_pct", "percent", "(abs(capex) / revenue) * 100", "Revenue <= 0.", provenance=[capex_p, rev_p]
        )
    else:
        with localcontext() as ctx:
            ctx.prec = 100
            capex_rev = _metric_computed(
                "capex_to_revenue_pct", (abs(capex) / rev) * Decimal(100), "percent", "(abs(capex) / revenue) * 100", [capex_p, rev_p], decimal_places=decimal_places
            )

    buybacks = cur.val("share_repurchases")
    buybacks_p = cur.pair("share_repurchases")
    divs = cur.val("dividends_paid")
    divs_p = cur.pair("dividends_paid")
    fcf, fcf_prov, fcf_formula, fcf_err = cur.fcf_resolved()

    cap_ret_formula = "abs(share_repurchases) + abs(dividends_paid)"
    cap_ret_prov = [p for p in (buybacks_p, divs_p) if p is not None]
    if buybacks is None and divs is None:
        cap_returned_val: Decimal | None = None
        cap_returned = _metric_unavailable(
            "capital_returned", monetary_unit, cap_ret_formula, "Missing both share_repurchases and dividends_paid.", currency=currency
        )
    else:
        b_abs = abs(buybacks) if buybacks is not None else Decimal(0)
        d_abs = abs(divs) if divs is not None else Decimal(0)
        cap_returned_val = b_abs + d_abs
        note = None
        if buybacks is None:
            note = "share_repurchases omitted and treated as 0."
        elif divs is None:
            note = "dividends_paid omitted and treated as 0."
        cap_returned = _metric_computed(
            "capital_returned", cap_returned_val, monetary_unit, cap_ret_formula, cap_ret_prov, decimal_places=decimal_places, currency=currency, reason=note
        )

    payout_formula = f"(abs(share_repurchases) + abs(dividends_paid)) / ({fcf_formula}) * 100"
    if "payout_to_fcf_pct" in disabled or "standard_fcf" in disabled:
        payout_fcf = _metric_disabled("payout_to_fcf_pct", "percent", payout_formula, sector)
    elif cap_returned_val is None or fcf is None:
        payout_fcf = _metric_unavailable(
            "payout_to_fcf_pct",
            "percent",
            payout_formula,
            cap_returned.reason or fcf_err or "Missing capital returned or FCF.",
            provenance=[*cap_ret_prov, *fcf_prov],
        )
    elif fcf <= 0:
        payout_fcf = _metric_unavailable(
            "payout_to_fcf_pct",
            "percent",
            payout_formula,
            "FCF <= 0; payout / FCF ratio is not meaningful.",
            provenance=[*cap_ret_prov, *fcf_prov],
        )
    else:
        with localcontext() as ctx:
            ctx.prec = 100
            payout_fcf = _metric_computed(
                "payout_to_fcf_pct",
                (cap_returned_val / fcf) * Decimal(100),
                "percent",
                payout_formula,
                [*cap_ret_prov, *fcf_prov],
                decimal_places=decimal_places,
            )

    shares_formula = "(shares_diluted_cur / shares_diluted_yoy - 1) * 100"
    if yoy is None:
        shares_yoy = _metric_unavailable("diluted_shares_yoy_pct", "percent", shares_formula, "Prior YoY period not provided.")
    else:
        sh_cur = cur.shares()
        sh_yoy = yoy.shares()
        sh_prov = [p for p in (cur.pair("shares_diluted"), yoy.pair("shares_diluted")) if p is not None]
        if sh_cur is None or sh_yoy is None:
            shares_yoy = _metric_unavailable(
                "diluted_shares_yoy_pct", "percent", shares_formula, "Missing shares_diluted in current or YoY period.", provenance=sh_prov
            )
        elif sh_yoy <= 0:
            shares_yoy = _metric_unavailable(
                "diluted_shares_yoy_pct", "percent", shares_formula, "Prior YoY shares_diluted <= 0.", provenance=sh_prov
            )
        else:
            with localcontext() as ctx:
                ctx.prec = 100
                shares_yoy = _metric_computed(
                    "diluted_shares_yoy_pct",
                    (sh_cur / sh_yoy - Decimal(1)) * Decimal(100),
                    "percent",
                    shares_formula,
                    sh_prov,
                    decimal_places=decimal_places,
                )

    return CapitalAllocationGroup(
        capex_to_revenue_pct=capex_rev,
        capital_returned=cap_returned,
        payout_to_fcf_pct=payout_fcf,
        diluted_shares_yoy_pct=shares_yoy,
    )


def _resolve_cash_and_debt(
    acc: _PeriodAccessor,
) -> tuple[Decimal | None, list[tuple[str, MetricValue]], str, Decimal | None, list[tuple[str, MetricValue]], str]:
    cash = acc.val("cash")
    sti = acc.val("short_term_investments")
    cash_prov = [p for p in (acc.pair("cash"), acc.pair("short_term_investments")) if p is not None]
    if cash is not None:
        total_cash = cash + (sti if sti is not None else Decimal(0))
        cash_formula = "cash + short_term_investments" if sti is not None else "cash"
    elif sti is not None:
        total_cash = sti
        cash_formula = "short_term_investments"
    else:
        total_cash = None
        cash_formula = "cash + short_term_investments"

    td = acc.val("total_debt")
    if td is not None:
        total_debt = td
        p_td = acc.pair("total_debt")
        debt_prov = [p_td] if p_td else []
        debt_formula = "total_debt"
    else:
        st_d = acc.val("short_term_debt")
        lt_d = acc.val("long_term_debt")
        debt_prov = [p for p in (acc.pair("short_term_debt"), acc.pair("long_term_debt")) if p is not None]
        if st_d is not None or lt_d is not None:
            total_debt = (st_d if st_d is not None else Decimal(0)) + (lt_d if lt_d is not None else Decimal(0))
            debt_formula = "short_term_debt + long_term_debt"
        else:
            total_debt = None
            debt_formula = "total_debt (or short_term_debt + long_term_debt)"

    return total_cash, cash_prov, cash_formula, total_debt, debt_prov, debt_formula


def _build_balance_sheet_group(
    cur: _PeriodAccessor,
    ttm_acc: _PeriodAccessor | None,
    trailing_accs: list[_PeriodAccessor] | None,
    sector: SectorApplicability,
    *,
    monetary_unit: str,
    currency: str | None,
    decimal_places: int,
) -> BalanceSheetLeverageGroup:
    disabled = set(sector.disabled_metrics)
    t_cash, cash_prov, cash_f, t_debt, debt_prov, debt_f = _resolve_cash_and_debt(cur)

    total_cash_m = (
        _metric_computed("total_cash", t_cash, monetary_unit, cash_f, cash_prov, decimal_places=decimal_places, currency=currency)
        if t_cash is not None
        else _metric_unavailable("total_cash", monetary_unit, cash_f, "Missing cash & equivalents.", currency=currency)
    )
    total_debt_m = (
        _metric_computed("total_debt", t_debt, monetary_unit, debt_f, debt_prov, decimal_places=decimal_places, currency=currency)
        if t_debt is not None
        else _metric_unavailable("total_debt", monetary_unit, debt_f, "Missing total_debt / short_term_debt / long_term_debt.", currency=currency)
    )

    net_debt_val: Decimal | None = None
    nd_prov = [*debt_prov, *cash_prov]
    nd_formula = f"({debt_f}) - ({cash_f})"
    nc_formula = f"({cash_f}) - ({debt_f})"
    if t_debt is not None and t_cash is not None:
        net_debt_val = t_debt - t_cash
        net_cash_val = t_cash - t_debt
        net_debt_m = _metric_computed(
            "net_debt", net_debt_val, monetary_unit, nd_formula, nd_prov, decimal_places=decimal_places, currency=currency
        )
        net_cash_m = _metric_computed(
            "net_cash", net_cash_val, monetary_unit, nc_formula, nd_prov, decimal_places=decimal_places, currency=currency
        )
        if net_debt_val < 0:
            position: Literal["net_cash", "net_debt", "balanced", "unavailable"] = "net_cash"
        elif net_debt_val > 0:
            position = "net_debt"
        else:
            position = "balanced"
    else:
        net_debt_m = _metric_unavailable(
            "net_debt", monetary_unit, nd_formula, "Missing total_debt or total_cash.", currency=currency, provenance=nd_prov
        )
        net_cash_m = _metric_unavailable(
            "net_cash", monetary_unit, nc_formula, "Missing total_debt or total_cash.", currency=currency, provenance=nd_prov
        )
        position = "unavailable"

    # Resolve TTM EBITDA for Net Debt / TTM EBITDA
    ttm_ebitda_val: Decimal | None = None
    ttm_ebitda_prov: list[tuple[str, MetricValue]] = []
    ttm_ebitda_expr = "ttm_ebitda"
    if ttm_acc is not None:
        ttm_ebitda_val, ttm_ebitda_prov, ttm_ebitda_expr = ttm_acc.ebitda_resolved()
    elif cur.period.period_type in ("annual", "ttm"):
        ttm_ebitda_val, ttm_ebitda_prov, ttm_ebitda_expr = cur.ebitda_resolved()
    elif trailing_accs is not None and len(trailing_accs) == 4:
        q_vals: list[Decimal] = []
        q_provs: list[tuple[str, MetricValue]] = []
        q_exprs: list[str] = []
        for q in trailing_accs:
            qv, qp, qe = q.ebitda_resolved()
            if qv is not None:
                q_vals.append(qv)
                q_provs.extend(qp)
                q_exprs.append(qe)
        if len(q_vals) == 4:
            ttm_ebitda_val = sum(q_vals, Decimal(0))
            ttm_ebitda_prov = q_provs
            ttm_ebitda_expr = "sum_4q(ebitda)"

    nd_ebitda_formula = f"({nd_formula}) / ({ttm_ebitda_expr})"
    if "net_debt_to_ttm_ebitda" in disabled or "standard_ev_ebitda" in disabled:
        nd_ebitda_m = _metric_disabled("net_debt_to_ttm_ebitda", "multiple", nd_ebitda_formula, sector)
    elif net_debt_val is None or ttm_ebitda_val is None:
        nd_ebitda_m = _metric_unavailable(
            "net_debt_to_ttm_ebitda",
            "multiple",
            nd_ebitda_formula,
            "Missing Net Debt or TTM EBITDA (pass ttm snapshot, 4 trailing_quarters, or annual period).",
            provenance=[*nd_prov, *ttm_ebitda_prov],
        )
    elif ttm_ebitda_val <= 0:
        nd_ebitda_m = _metric_unavailable(
            "net_debt_to_ttm_ebitda",
            "multiple",
            nd_ebitda_formula,
            "TTM EBITDA <= 0; Net Debt / TTM EBITDA is not meaningful.",
            provenance=[*nd_prov, *ttm_ebitda_prov],
        )
    else:
        with localcontext() as ctx:
            ctx.prec = 100
            nd_ebitda_m = _metric_computed(
                "net_debt_to_ttm_ebitda",
                net_debt_val / ttm_ebitda_val,
                "multiple",
                nd_ebitda_formula,
                [*nd_prov, *ttm_ebitda_prov],
                decimal_places=decimal_places,
            )

    # Interest Coverage (EBIT / Interest Expense)
    ebit_val, ebit_prov, ebit_expr = cur.ebit_resolved()
    int_exp = cur.val("interest_expense")
    int_pair = cur.pair("interest_expense")
    ic_formula = f"{ebit_expr} / abs(interest_expense)"
    if "interest_coverage" in disabled:
        ic_m = _metric_disabled("interest_coverage", "multiple", ic_formula, sector)
    elif ebit_val is None or int_exp is None or int_pair is None:
        ic_m = _metric_unavailable(
            "interest_coverage",
            "multiple",
            ic_formula,
            "Missing EBIT/operating_income or interest_expense.",
            provenance=[*ebit_prov, *([int_pair] if int_pair else [])],
        )
    elif int_exp == 0:
        ic_m = _metric_unavailable(
            "interest_coverage",
            "multiple",
            ic_formula,
            "Interest expense is zero; interest coverage ratio is undefined.",
            provenance=[*ebit_prov, int_pair],
        )
    else:
        with localcontext() as ctx:
            ctx.prec = 100
            ic_m = _metric_computed(
                "interest_coverage",
                ebit_val / abs(int_exp),
                "multiple",
                ic_formula,
                [*ebit_prov, int_pair],
                decimal_places=decimal_places,
            )

    # Current Ratio
    ca, ca_p = cur.val("current_assets"), cur.pair("current_assets")
    cl, cl_p = cur.val("current_liabilities"), cur.pair("current_liabilities")
    cr_prov = [p for p in (ca_p, cl_p) if p is not None]
    cr_formula = "current_assets / current_liabilities"
    if "current_ratio" in disabled:
        cr_m = _metric_disabled("current_ratio", "ratio", cr_formula, sector)
    elif ca is None or cl is None:
        cr_m = _metric_unavailable("current_ratio", "ratio", cr_formula, "Missing current_assets or current_liabilities.", provenance=cr_prov)
    elif cl <= 0:
        cr_m = _metric_unavailable("current_ratio", "ratio", cr_formula, "current_liabilities <= 0; current ratio undefined.", provenance=cr_prov)
    else:
        with localcontext() as ctx:
            ctx.prec = 100
            cr_m = _metric_computed("current_ratio", ca / cl, "ratio", cr_formula, cr_prov, decimal_places=decimal_places)

    return BalanceSheetLeverageGroup(
        total_cash=total_cash_m,
        total_debt=total_debt_m,
        net_debt=net_debt_m,
        net_cash=net_cash_m,
        position=position,
        net_debt_to_ttm_ebitda=nd_ebitda_m,
        interest_coverage=ic_m,
        current_ratio=cr_m,
    )


def _evaluate_diagnostic_flags(
    cur: _PeriodAccessor,
    sector: SectorApplicability,
    margin_bridge: list[MarginBridgeItem],
    eq: EarningsQualityCashFlowGroup,
    wc: WorkingCapitalForensicsGroup,
    ca: CapitalAllocationGroup,
    bs: BalanceSheetLeverageGroup,
) -> list[DiagnosticFlag]:
    flags: list[DiagnosticFlag] = []
    disabled = set(sector.disabled_metrics)

    # 1. Earnings Quality: OCF / NI
    ni = cur.val("net_income")
    ocf = cur.val("operating_cash_flow")
    if "ocf_to_ni" not in disabled:
        if ni is not None and ni > 0 and ocf is not None and ocf <= 0:
            flags.append(
                DiagnosticFlag(
                    flag_id="eq_ocf_negative_with_positive_ni",
                    group="earnings_quality",
                    severity="red",
                    metric_name="operating_cash_flow",
                    observed_value=ocf,
                    unit=eq.reported_or_derived_fcf.unit,
                    threshold_rule="OCF <= 0 while Net Income > 0",
                    summary="Operating cash flow is non-positive despite positive GAAP Net Income.",
                    investigation_prompt=(
                        "Inspect Cash Flow Statement working capital lines (receivables, inventory, payables, "
                        "deferred revenue) and MD&A Liquidity section for non-cash gains or working-capital cash drain."
                    ),
                    input_ids=_dedup_preserve([
                        cur.pair("operating_cash_flow")[1].provenance_id("operating_cash_flow") if cur.pair("operating_cash_flow") else None,
                        cur.pair("net_income")[1].provenance_id("net_income") if cur.pair("net_income") else None,
                    ]),
                )
            )
        elif eq.ocf_to_ni.status == "computed" and eq.ocf_to_ni.value is not None:
            v = eq.ocf_to_ni.value
            if v < Decimal("0.80"):
                sev: FlagSeverity = "red"
                rule = "OCF / NI < 0.80x"
                summary = f"Cash conversion weak: OCF / Net Income is {v}x (< 0.80x threshold)."
            elif v < Decimal("0.95"):
                sev = "yellow"
                rule = "0.80x <= OCF / NI < 0.95x"
                summary = f"Moderate cash conversion lag: OCF / Net Income is {v}x (< 0.95x)."
            else:
                sev = "green"
                rule = "OCF / NI >= 0.95x"
                summary = f"Healthy cash conversion: OCF / Net Income is {v}x (>= 0.95x)."
            flags.append(
                DiagnosticFlag(
                    flag_id="eq_ocf_to_ni",
                    group="earnings_quality",
                    severity=sev,
                    metric_name="ocf_to_ni",
                    observed_value=v,
                    unit="ratio",
                    threshold_rule=rule,
                    summary=summary,
                    investigation_prompt=(
                        "Verify Cash Flow Statement operating adjustments (D&A, deferred taxes, working capital changes) "
                        "and MD&A Cash Flows discussion to confirm whether accruals or timing explain conversion."
                    ),
                    input_ids=eq.ocf_to_ni.input_ids,
                )
            )

    # 2. Earnings Quality: FCF / NI
    fcf_val = eq.reported_or_derived_fcf.value
    if "fcf_to_ni" not in disabled and "standard_fcf" not in disabled:
        if ni is not None and ni > 0 and fcf_val is not None and fcf_val <= 0:
            flags.append(
                DiagnosticFlag(
                    flag_id="eq_fcf_nonpositive_with_positive_ni",
                    group="earnings_quality",
                    severity="red",
                    metric_name="free_cash_flow",
                    observed_value=fcf_val,
                    unit=eq.reported_or_derived_fcf.unit,
                    threshold_rule="FCF <= 0 while Net Income > 0",
                    summary="Free Cash Flow is non-positive despite positive GAAP Net Income.",
                    investigation_prompt=(
                        "Check MD&A Capital Expenditures & Free Cash Flow commentary and Property/Equipment footnote "
                        "to separate growth CapEx cycles from deteriorating cash generation."
                    ),
                    input_ids=eq.reported_or_derived_fcf.input_ids,
                )
            )
        elif eq.fcf_to_ni.status == "computed" and eq.fcf_to_ni.value is not None:
            v = eq.fcf_to_ni.value
            if v < Decimal("0.50"):
                sev, rule = "red", "FCF / NI < 0.50x"
                summary = f"Low FCF conversion: FCF / Net Income is {v}x (< 0.50x)."
            elif v < Decimal("0.80"):
                sev, rule = "yellow", "0.50x <= FCF / NI < 0.80x"
                summary = f"Below-target FCF conversion: FCF / Net Income is {v}x (0.50x-0.80x)."
            else:
                sev, rule = "green", "FCF / NI >= 0.80x"
                summary = f"Strong FCF conversion: FCF / Net Income is {v}x (>= 0.80x)."
            flags.append(
                DiagnosticFlag(
                    flag_id="eq_fcf_to_ni",
                    group="earnings_quality",
                    severity=sev,
                    metric_name="fcf_to_ni",
                    observed_value=v,
                    unit="ratio",
                    threshold_rule=rule,
                    summary=summary,
                    investigation_prompt=(
                        "Review CapEx intensity vs D&A in the Cash Flow Statement and MD&A CapEx guidance "
                        "to determine maintenance vs expansion investment."
                    ),
                    input_ids=eq.fcf_to_ni.input_ids,
                )
            )

    # 3. SBC burden vs FCF and Revenue
    if "sbc_to_fcf_pct" not in disabled and "standard_fcf" not in disabled:
        if fcf_val is not None and fcf_val > 0 and eq.fcf_ex_sbc.value is not None and eq.fcf_ex_sbc.value <= 0:
            flags.append(
                DiagnosticFlag(
                    flag_id="eq_sbc_exceeds_fcf",
                    group="earnings_quality",
                    severity="red",
                    metric_name="fcf_ex_sbc",
                    observed_value=eq.fcf_ex_sbc.value,
                    unit=eq.fcf_ex_sbc.unit,
                    threshold_rule="FCF > 0 and FCF ex-SBC <= 0 (SBC >= 100% of FCF)",
                    summary="Stock-based compensation consumes 100%+ of FCF, turning true FCF ex-SBC non-positive.",
                    investigation_prompt=(
                        "Read the Stock-Based Compensation footnote (grants, RSU vesting, weighted-average assumptions) "
                        "and Non-GAAP reconciliation in 8-K Ex-99.1 to assess real economic owner earnings."
                    ),
                    input_ids=eq.fcf_ex_sbc.input_ids,
                )
            )
        elif eq.sbc_to_fcf_pct.status == "computed" and eq.sbc_to_fcf_pct.value is not None:
            v = eq.sbc_to_fcf_pct.value
            if v > Decimal("30"):
                sev, rule = "red", "SBC / FCF > 30%"
                summary = f"Heavy SBC overhang: SBC equals {v}% of FCF (> 30% threshold)."
            elif v > Decimal("15"):
                sev, rule = "yellow", "15% < SBC / FCF <= 30%"
                summary = f"Elevated SBC burden: SBC equals {v}% of FCF (15%-30%)."
            else:
                sev, rule = "green", "SBC / FCF <= 15%"
                summary = f"Contained SBC burden: SBC equals {v}% of FCF (<= 15%)."
            flags.append(
                DiagnosticFlag(
                    flag_id="eq_sbc_to_fcf",
                    group="earnings_quality",
                    severity=sev,
                    metric_name="sbc_to_fcf_pct",
                    observed_value=v,
                    unit="percent",
                    threshold_rule=rule,
                    summary=summary,
                    investigation_prompt=(
                        "Inspect the Stock-Based Compensation footnote and share repurchase disclosures to check "
                        "whether buybacks merely offset SBC dilution rather than shrinking share count."
                    ),
                    input_ids=eq.sbc_to_fcf_pct.input_ids,
                )
            )

    if eq.sbc_to_revenue_pct.status == "computed" and eq.sbc_to_revenue_pct.value is not None:
        v = eq.sbc_to_revenue_pct.value
        if v > Decimal("15"):
            flags.append(
                DiagnosticFlag(
                    flag_id="eq_sbc_to_revenue",
                    group="earnings_quality",
                    severity="red",
                    metric_name="sbc_to_revenue_pct",
                    observed_value=v,
                    unit="percent",
                    threshold_rule="SBC / Revenue > 15%",
                    summary=f"Excessive SBC relative to revenue: {v}% (> 15%).",
                    investigation_prompt=(
                        "Examine the Equity Compensation footnote (unrecognized compensation cost and amortization period) "
                        "and headcount/executive compensation disclosures in MD&A and proxy statements."
                    ),
                    input_ids=eq.sbc_to_revenue_pct.input_ids,
                )
            )
        elif v > Decimal("8"):
            flags.append(
                DiagnosticFlag(
                    flag_id="eq_sbc_to_revenue",
                    group="earnings_quality",
                    severity="yellow",
                    metric_name="sbc_to_revenue_pct",
                    observed_value=v,
                    unit="percent",
                    threshold_rule="8% < SBC / Revenue <= 15%",
                    summary=f"Meaningful SBC relative to revenue: {v}% (8%-15%).",
                    investigation_prompt=(
                        "Check Stock-Based Compensation footnote to see SBC allocation across COGS, R&D, and SG&A "
                        "and trajectory of annual equity grant run-rate."
                    ),
                    input_ids=eq.sbc_to_revenue_pct.input_ids,
                )
            )

    # 4. Margin Bridge YoY checks
    margin_map = {item.line: item for item in margin_bridge}
    for m_line, flag_id, prompt in (
        (
            "gross_margin",
            "mb_gross_margin_yoy",
            "Inspect MD&A Results of Operations gross margin bridge for pricing, mix shift, utilization, or tariff/input cost drivers.",
        ),
        (
            "operating_margin",
            "mb_operating_margin_yoy",
            "Inspect MD&A operating expense drivers (R&D, SG&A, restructuring, one-time charges) and segment margin disclosures.",
        ),
    ):
        item = margin_map.get(m_line)
        if item and item.yoy_change_bps and item.yoy_change_bps.status == "computed" and item.yoy_change_bps.value is not None:
            bps = item.yoy_change_bps.value
            if bps < Decimal("-200"):
                sev, rule = "red", "YoY margin change < -200 bps"
                summary = f"Sharp {m_line} YoY compression of {bps} bps (< -200 bps)."
            elif bps < Decimal("-50"):
                sev, rule = "yellow", "-200 bps <= YoY margin change < -50 bps"
                summary = f"Moderate {m_line} YoY compression of {bps} bps (-200 to -50 bps)."
            elif bps >= Decimal("50"):
                sev, rule = "green", "YoY margin change >= +50 bps"
                summary = f"Meaningful {m_line} YoY expansion of +{bps} bps (>= +50 bps)."
            else:
                sev = None  # flat (-50 to +50 bps): no flag needed
                rule = ""
                summary = ""
            if sev is not None:
                flags.append(
                    DiagnosticFlag(
                        flag_id=flag_id,
                        group="margin_bridge",
                        severity=sev,
                        metric_name=f"{m_line}_yoy_bps",
                        observed_value=bps,
                        unit="bps",
                        threshold_rule=rule,
                        summary=summary,
                        investigation_prompt=prompt,
                        input_ids=item.yoy_change_bps.input_ids,
                    )
                )

    # 5. Working Capital Forensics
    if wc.ar_vs_revenue_growth_pp.status == "computed" and wc.ar_vs_revenue_growth_pp.value is not None:
        gap = wc.ar_vs_revenue_growth_pp.value
        if gap > Decimal("15"):
            sev, rule = "red", "AR YoY growth - Revenue YoY growth > +15.0 pp"
            summary = f"Accounts receivable grew {gap} pp faster than revenue (> +15 pp divergence)."
        elif gap > Decimal("5"):
            sev, rule = "yellow", "+5.0 pp < AR - Revenue YoY growth <= +15.0 pp"
            summary = f"Accounts receivable outpaced revenue by {gap} pp (+5 to +15 pp)."
        else:
            sev, rule = "green", "AR - Revenue YoY growth <= +5.0 pp"
            summary = f"Receivables growth well aligned with revenue ({gap} pp gap)."
        flags.append(
            DiagnosticFlag(
                flag_id="wc_ar_vs_revenue_growth",
                group="working_capital",
                severity=sev,
                metric_name="ar_vs_revenue_growth_pp",
                observed_value=gap,
                unit="pp",
                threshold_rule=rule,
                summary=summary,
                investigation_prompt=(
                    "Review Revenue Recognition (ASC 606) and Accounts Receivable / Allowance for Credit Losses footnotes "
                    "plus MD&A DSO commentary for extended customer terms, billing linearity, or unbilled receivables."
                ),
                input_ids=wc.ar_vs_revenue_growth_pp.input_ids,
            )
        )

    inv_gap_metric = (
        wc.inventory_vs_cogs_growth_pp
        if wc.inventory_vs_cogs_growth_pp.status == "computed"
        else wc.inventory_vs_revenue_growth_pp
    )
    if inv_gap_metric.status == "computed" and inv_gap_metric.value is not None:
        gap = inv_gap_metric.value
        basis_label = "COGS" if inv_gap_metric.name == "inventory_vs_cogs_growth_pp" else "Revenue"
        if gap > Decimal("15"):
            sev, rule = "red", f"Inventory YoY growth - {basis_label} YoY growth > +15.0 pp"
            summary = f"Inventory expanded {gap} pp faster than {basis_label} (> +15 pp buildup)."
        elif gap > Decimal("5"):
            sev, rule = "yellow", f"+5.0 pp < Inventory - {basis_label} YoY growth <= +15.0 pp"
            summary = f"Inventory outpaced {basis_label} by {gap} pp (+5 to +15 pp)."
        else:
            sev, rule = "green", f"Inventory - {basis_label} YoY growth <= +5.0 pp"
            summary = f"Inventory growth disciplined vs {basis_label} ({gap} pp gap)."
        flags.append(
            DiagnosticFlag(
                flag_id="wc_inventory_divergence",
                group="working_capital",
                severity=sev,
                metric_name=inv_gap_metric.name,
                observed_value=gap,
                unit="pp",
                threshold_rule=rule,
                summary=summary,
                investigation_prompt=(
                    "Check the Inventory footnote (raw materials, WIP, finished goods split and lower-of-cost-or-NRV "
                    "write-downs) and MD&A supply-chain/demand commentary for intentional buffer build vs demand slowdown."
                ),
                input_ids=inv_gap_metric.input_ids,
            )
        )

    if wc.ccc.yoy_delta and wc.ccc.yoy_delta.status == "computed" and wc.ccc.yoy_delta.value is not None:
        d_ccc = wc.ccc.yoy_delta.value
        if d_ccc > Decimal("15"):
            sev, rule = "red", "CCC YoY delta > +15.0 days"
            summary = f"Cash Conversion Cycle lengthened by {d_ccc} days YoY (> +15 days)."
        elif d_ccc > Decimal("5"):
            sev, rule = "yellow", "+5.0 days < CCC YoY delta <= +15.0 days"
            summary = f"Cash Conversion Cycle lengthened by {d_ccc} days YoY (+5 to +15 days)."
        elif d_ccc <= Decimal("0"):
            sev, rule = "green", "CCC YoY delta <= 0.0 days"
            summary = f"Cash Conversion Cycle improved or held steady ({d_ccc} days YoY)."
        else:
            sev = None
            rule = ""
            summary = ""
        if sev is not None:
            flags.append(
                DiagnosticFlag(
                    flag_id="wc_ccc_yoy_delta",
                    group="working_capital",
                    severity=sev,
                    metric_name="ccc_yoy_delta",
                    observed_value=d_ccc,
                    unit="days",
                    threshold_rule=rule,
                    summary=summary,
                    investigation_prompt=(
                        "Decompose DSO, DIO, and DPO drivers in MD&A Working Capital and Liquidity sections "
                        "to pinpoint whether customer collections, inventory turnover, or vendor terms shifted."
                    ),
                    input_ids=wc.ccc.yoy_delta.input_ids,
                )
            )

    # 6. Capital Allocation & Dilution
    if ca.diluted_shares_yoy_pct.status == "computed" and ca.diluted_shares_yoy_pct.value is not None:
        dil = ca.diluted_shares_yoy_pct.value
        if dil > Decimal("3"):
            sev, rule = "red", "Diluted shares YoY change > +3.0%"
            summary = f"Material share dilution of +{dil}% YoY (> +3%)."
        elif dil > Decimal("1"):
            sev, rule = "yellow", "+1.0% < Diluted shares YoY change <= +3.0%"
            summary = f"Net share dilution of +{dil}% YoY (+1% to +3%)."
        elif dil <= Decimal("0"):
            sev, rule = "green", "Diluted shares YoY change <= 0.0%"
            summary = f"Net share count stable or shrinking ({dil}% YoY)."
        else:
            sev = None
            rule = ""
            summary = ""
        if sev is not None:
            flags.append(
                DiagnosticFlag(
                    flag_id="ca_diluted_shares_yoy",
                    group="capital_allocation",
                    severity=sev,
                    metric_name="diluted_shares_yoy_pct",
                    observed_value=dil,
                    unit="percent",
                    threshold_rule=rule,
                    summary=summary,
                    investigation_prompt=(
                        "Review EPS footnote (anti-dilutive securities excluded from diluted EPS, convertibles/warrants) "
                        "and Stockholders' Equity / Share Repurchase Plan footnotes."
                    ),
                    input_ids=ca.diluted_shares_yoy_pct.input_ids,
                )
            )

    if ca.payout_to_fcf_pct.status == "computed" and ca.payout_to_fcf_pct.value is not None:
        payout = ca.payout_to_fcf_pct.value
        if payout > Decimal("120"):
            sev, rule = "red", "(Buybacks + Dividends) / FCF > 120%"
            summary = f"Capital returns ({payout}% of FCF) materially exceed FCF generation (> 120%)."
        elif payout > Decimal("100"):
            sev, rule = "yellow", "100% < (Buybacks + Dividends) / FCF <= 120%"
            summary = f"Capital returns ({payout}% of FCF) modestly exceed current FCF."
        else:
            sev, rule = "green", "0% <= (Buybacks + Dividends) / FCF <= 100%"
            summary = f"Capital returns ({payout}% of FCF) are fully funded by organic FCF."
        flags.append(
            DiagnosticFlag(
                flag_id="ca_payout_to_fcf",
                group="capital_allocation",
                severity=sev,
                metric_name="payout_to_fcf_pct",
                observed_value=payout,
                unit="percent",
                threshold_rule=rule,
                summary=summary,
                investigation_prompt=(
                    "Check Debt & Capital Resources MD&A and Statement of Cash Flows financing section to verify "
                    "whether dividends/buybacks are funded by excess cash balances or new debt issuance."
                ),
                input_ids=ca.payout_to_fcf_pct.input_ids,
            )
        )

    # 7. Balance Sheet & Leverage
    if bs.net_debt_to_ttm_ebitda.status == "computed" and bs.net_debt_to_ttm_ebitda.value is not None:
        lev = bs.net_debt_to_ttm_ebitda.value
        if lev > Decimal("3.5"):
            sev, rule = "red", "Net Debt / TTM EBITDA > 3.5x"
            summary = f"Elevated leverage: Net Debt / TTM EBITDA is {lev}x (> 3.5x)."
        elif lev > Decimal("2.0"):
            sev, rule = "yellow", "2.0x < Net Debt / TTM EBITDA <= 3.5x"
            summary = f"Moderate-high leverage: Net Debt / TTM EBITDA is {lev}x (2.0x-3.5x)."
        elif lev <= Decimal("1.0"):
            sev, rule = "green", "Net Debt / TTM EBITDA <= 1.0x"
            summary = f"Conservative balance sheet: Net Debt / TTM EBITDA is {lev}x (<= 1.0x)."
        else:
            sev = None
            rule = ""
            summary = ""
        if sev is not None:
            flags.append(
                DiagnosticFlag(
                    flag_id="bs_net_debt_to_ebitda",
                    group="balance_sheet",
                    severity=sev,
                    metric_name="net_debt_to_ttm_ebitda",
                    observed_value=lev,
                    unit="multiple",
                    threshold_rule=rule,
                    summary=summary,
                    investigation_prompt=(
                        "Inspect Debt footnote for maturity schedule, fixed vs floating rate mix, covenant headroom, "
                        "and off-balance-sheet lease/purchase commitments in Commitments & Contingencies."
                    ),
                    input_ids=bs.net_debt_to_ttm_ebitda.input_ids,
                )
            )

    if bs.interest_coverage.status == "computed" and bs.interest_coverage.value is not None:
        ic = bs.interest_coverage.value
        if ic < Decimal("2.0"):
            sev, rule = "red", "EBIT / Interest Expense < 2.0x"
            summary = f"Tight interest coverage: {ic}x (< 2.0x)."
        elif ic < Decimal("4.0"):
            sev, rule = "yellow", "2.0x <= EBIT / Interest Expense < 4.0x"
            summary = f"Moderate interest coverage: {ic}x (2.0x-4.0x)."
        elif ic >= Decimal("6.0"):
            sev, rule = "green", "EBIT / Interest Expense >= 6.0x"
            summary = f"Strong interest coverage: {ic}x (>= 6.0x)."
        else:
            sev = None
            rule = ""
            summary = ""
        if sev is not None:
            flags.append(
                DiagnosticFlag(
                    flag_id="bs_interest_coverage",
                    group="balance_sheet",
                    severity=sev,
                    metric_name="interest_coverage",
                    observed_value=ic,
                    unit="multiple",
                    threshold_rule=rule,
                    summary=summary,
                    investigation_prompt=(
                        "Check Debt & Interest Expense footnotes and MD&A refinancing plans for upcoming maturities "
                        "and sensitivity to benchmark interest rate resets."
                    ),
                    input_ids=bs.interest_coverage.input_ids,
                )
            )

    if bs.current_ratio.status == "computed" and bs.current_ratio.value is not None:
        cr = bs.current_ratio.value
        if cr < Decimal("1.0"):
            sev, rule = "red", "Current Ratio < 1.0x"
            summary = f"Current liabilities exceed current assets: Current Ratio {cr}x (< 1.0x)."
        elif cr < Decimal("1.2"):
            sev, rule = "yellow", "1.0x <= Current Ratio < 1.2x"
            summary = f"Thin short-term liquidity buffer: Current Ratio {cr}x (1.0x-1.2x)."
        elif cr >= Decimal("1.5"):
            sev, rule = "green", "Current Ratio >= 1.5x"
            summary = f"Healthy short-term liquidity: Current Ratio {cr}x (>= 1.5x)."
        else:
            sev = None
            rule = ""
            summary = ""
        if sev is not None:
            flags.append(
                DiagnosticFlag(
                    flag_id="bs_current_ratio",
                    group="balance_sheet",
                    severity=sev,
                    metric_name="current_ratio",
                    observed_value=cr,
                    unit="ratio",
                    threshold_rule=rule,
                    summary=summary,
                    investigation_prompt=(
                        "Inspect Current Liabilities and Debt footnotes (short-term debt vs deferred revenue / customer "
                        "deposits) and undrawn revolving credit capacity in MD&A Liquidity & Capital Resources."
                    ),
                    input_ids=bs.current_ratio.input_ids,
                )
            )

    return flags


def earnings_quality_diagnostics(request: EarningsQualityRequest) -> EarningsQualityDiagnosticsResult:
    """Run deterministic 5-group earnings quality diagnostics with SEC SIC gate and R/Y/G flags."""
    sector = evaluate_sec_sic_gate(request.sic, request.sic_description)

    all_periods: list[PeriodFinancials] = [request.current]
    qoq_idx: int | None = None
    yoy_idx: int | None = None
    ttm_idx: int | None = None
    trailing_indices: list[int] = []

    if request.prior_qoq is not None:
        qoq_idx = len(all_periods)
        all_periods.append(request.prior_qoq)
    if request.prior_yoy is not None:
        yoy_idx = len(all_periods)
        all_periods.append(request.prior_yoy)
    if request.ttm is not None:
        ttm_idx = len(all_periods)
        all_periods.append(request.ttm)
    if request.trailing_quarters is not None:
        for q in request.trailing_quarters:
            trailing_indices.append(len(all_periods))
            all_periods.append(q)

    money_map, resolved_currency, resolved_unit = _aligned_monetary_map(all_periods)
    shares_map = _aligned_shares_map(all_periods)

    def accessor(idx: int) -> _PeriodAccessor:
        p = all_periods[idx]
        return _PeriodAccessor(f"p{idx}:{p.period_label}", p, money_map, shares_map)

    cur_acc = accessor(0)
    qoq_acc = accessor(qoq_idx) if qoq_idx is not None else None
    yoy_acc = accessor(yoy_idx) if yoy_idx is not None else None
    ttm_acc = accessor(ttm_idx) if ttm_idx is not None else None
    trailing_accs = [accessor(i) for i in trailing_indices] if trailing_indices else None

    margin_bridge = _build_margin_bridge(cur_acc, qoq_acc, yoy_acc, sector, request.decimal_places)
    eq_group = _build_earnings_quality_group(
        cur_acc, sector, monetary_unit=resolved_unit, currency=resolved_currency, decimal_places=request.decimal_places
    )
    wc_group = _build_working_capital_group(cur_acc, qoq_acc, yoy_acc, sector, request.decimal_places)
    ca_group = _build_capital_allocation_group(
        cur_acc, yoy_acc, sector, monetary_unit=resolved_unit, currency=resolved_currency, decimal_places=request.decimal_places
    )
    bs_group = _build_balance_sheet_group(
        cur_acc, ttm_acc, trailing_accs, sector, monetary_unit=resolved_unit, currency=resolved_currency, decimal_places=request.decimal_places
    )

    flags = _evaluate_diagnostic_flags(cur_acc, sector, margin_bridge, eq_group, wc_group, ca_group, bs_group)
    counts: dict[FlagSeverity, int] = {
        "red": sum(1 for f in flags if f.severity == "red"),
        "yellow": sum(1 for f in flags if f.severity == "yellow"),
        "green": sum(1 for f in flags if f.severity == "green"),
    }

    return EarningsQualityDiagnosticsResult(
        entity_id=request.entity_id,
        symbol=request.symbol,
        period_label=request.current.period_label,
        monetary_unit=resolved_unit,
        currency=resolved_currency,
        sector_applicability=sector,
        margin_bridge=margin_bridge,
        earnings_quality=eq_group,
        working_capital=wc_group,
        capital_allocation=ca_group,
        balance_sheet=bs_group,
        flags=flags,
        flag_counts=counts,
        thresholds_reference=DIAGNOSTIC_THRESHOLDS,
    )


# ============================================================================
# Engine 2: guidance_and_nongaap_reconciler
# ============================================================================


class GuidanceRangeInput(BaseModel):
    """Low / Mid / High guidance range (from 8-K Exhibit 99.1)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    low: MetricValue | None = None
    mid: MetricValue | None = None
    high: MetricValue | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_bounds(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        copied = dict(data)
        for key in ("low", "mid", "high"):
            if key in copied and copied[key] is not None:
                copied[key] = _coerce_metric_value(copied[key], fallback_fact_id=f"guidance:{key}")
        return copied

    @model_validator(mode="after")
    def _validate_range(self) -> GuidanceRangeInput:
        if self.mid is None and (self.low is None or self.high is None):
            raise ValueError("GuidanceRangeInput requires either 'mid' or both 'low' and 'high'.")
        if self.low is not None and self.high is not None:
            aligned, _, _ = _resolve_currency_and_scale(
                [("guidance.low", self.low), ("guidance.high", self.high)], expected_family="any"
            )
            if aligned[0] > aligned[1]:
                raise ValueError("GuidanceRangeInput 'low' cannot exceed 'high'.")
            if self.mid is not None:
                aligned3, _, _ = _resolve_currency_and_scale(
                    [("guidance.low", self.low), ("guidance.mid", self.mid), ("guidance.high", self.high)],
                    expected_family="any",
                )
                if not (aligned3[0] <= aligned3[1] <= aligned3[2]):
                    raise ValueError("GuidanceRangeInput 'mid' must lie between 'low' and 'high'.")
        return self

    def items_for_alignment(self, prefix: str) -> list[tuple[str, MetricValue]]:
        items: list[tuple[str, MetricValue]] = []
        if self.low is not None:
            items.append((f"{prefix}.low", self.low))
        if self.mid is not None:
            items.append((f"{prefix}.mid", self.mid))
        if self.high is not None:
            items.append((f"{prefix}.high", self.high))
        return items


class GuidanceMetricInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    metric_name: str = Field(min_length=1, max_length=120)
    period_label: str | None = Field(default=None, max_length=100)
    actual: MetricValue | None = None
    prior_guidance: GuidanceRangeInput | None = None
    consensus: MetricValue | None = None
    new_guidance: GuidanceRangeInput | None = None
    new_guidance_period_label: str | None = Field(default=None, max_length=100)
    new_guidance_benchmark: GuidanceRangeInput | MetricValue | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_scalars(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        copied = dict(data)
        m_name = str(copied.get("metric_name") or "metric").strip()
        for key in ("actual", "consensus"):
            if key in copied and copied[key] is not None:
                copied[key] = _coerce_metric_value(copied[key], fallback_fact_id=f"{m_name}:{key}")
        bench = copied.get("new_guidance_benchmark")
        if bench is not None and not isinstance(bench, (GuidanceRangeInput, MetricValue)):
            if isinstance(bench, dict) and any(k in bench for k in ("low", "mid", "high")):
                pass
            else:
                copied["new_guidance_benchmark"] = _coerce_metric_value(
                    bench, fallback_fact_id=f"{m_name}:new_guidance_benchmark"
                )
        return copied


class NonGaapAdjustmentItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=200)
    amount: MetricValue
    category: NonGaapCategory | None = None
    is_recurring: bool | None = None
    prior_periods_present: int | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="before")
    @classmethod
    def _coerce_amount(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        copied = dict(data)
        label = str(copied.get("label") or "adjustment").strip()
        if "amount" in copied and copied["amount"] is not None:
            copied["amount"] = _coerce_metric_value(copied["amount"], fallback_fact_id=f"adj:{label}")
        return copied


class NonGaapBridgeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    metric_name: str = Field(min_length=1, max_length=120)
    period_label: str = Field(min_length=1, max_length=100)
    gaap_value: MetricValue
    reported_non_gaap_value: MetricValue
    adjustments: list[NonGaapAdjustmentItem] = Field(default_factory=list)
    rounding_tolerance: Decimal = Field(default=Decimal("0"), ge=0)

    @field_validator("rounding_tolerance", mode="before")
    @classmethod
    def _validate_tol(cls, raw: Any) -> Decimal:
        val = _to_decimal(raw, field_name="rounding_tolerance")
        if val < 0:
            raise DiagnosticsError("rounding_tolerance must be >= 0.")
        return val

    @model_validator(mode="before")
    @classmethod
    def _coerce_values(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        copied = dict(data)
        m_name = str(copied.get("metric_name") or "nongaap").strip()
        if "gaap_value" in copied and copied["gaap_value"] is not None:
            copied["gaap_value"] = _coerce_metric_value(copied["gaap_value"], fallback_fact_id=f"{m_name}:gaap")
        if "reported_non_gaap_value" in copied and copied["reported_non_gaap_value"] is not None:
            copied["reported_non_gaap_value"] = _coerce_metric_value(
                copied["reported_non_gaap_value"], fallback_fact_id=f"{m_name}:non_gaap"
            )
        return copied


class GuidanceAndNonGaapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    entity_id: str | None = Field(default=None, max_length=120)
    symbol: str | None = Field(default=None, max_length=32)
    period_label: str = Field(min_length=1, max_length=100)
    guidance_metrics: list[GuidanceMetricInput] = Field(default_factory=list)
    nongaap_bridges: list[NonGaapBridgeInput] = Field(default_factory=list)
    inline_tolerance_pct: Decimal = Field(default=Decimal("0"), ge=0, le=Decimal("10"))
    maintain_tolerance_pct: Decimal = Field(default=Decimal("0"), ge=0, le=Decimal("10"))
    decimal_places: int = Field(default=2, ge=0, le=6)


class SurpriseComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: MetricStatus
    benchmark_type: Literal["prior_guidance_midpoint", "consensus"]
    outcome: SurpriseOutcome
    actual_value: Decimal | None = None
    benchmark_value: Decimal | None = None
    guidance_low: Decimal | None = None
    guidance_high: Decimal | None = None
    range_position: RangePosition = "unavailable"
    abs_diff: Decimal | None = None
    pct_diff: Decimal | None = None
    unit: str
    currency: str | None = None
    formula: str
    input_ids: list[str] = Field(default_factory=list)
    xbrl_tags: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


class NewGuidanceDirectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: NewGuidanceDirection
    new_low: Decimal | None = None
    new_mid: Decimal | None = None
    new_high: Decimal | None = None
    benchmark_mid: Decimal | None = None
    abs_change: Decimal | None = None
    pct_change: Decimal | None = None
    unit: str
    currency: str | None = None
    formula: str
    input_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


class GuidanceMetricEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_name: str
    period_label: str | None = None
    vs_prior_guidance: SurpriseComparison
    vs_consensus: SurpriseComparison
    new_guidance_direction: NewGuidanceDirectionResult


class ClassifiedNonGaapAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    amount: Decimal
    unit: str
    currency: str | None = None
    category: NonGaapCategory
    recurring_flag: bool
    recurring_reason: str | None = None
    input_ids: list[str] = Field(default_factory=list)


class NonGaapWarningFlag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flag_id: str
    severity: FlagSeverity
    metric_name: str
    adjustment_label: str | None = None
    category: str | None = None
    summary: str
    investigation_prompt: str = Field(min_length=10)
    input_ids: list[str] = Field(default_factory=list)


class NonGaapBridgeVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_name: str
    period_label: str
    unit: str
    currency: str | None = None
    gaap_value: Decimal
    reported_non_gaap_value: Decimal
    adjustments_sum: Decimal
    computed_non_gaap_value: Decimal
    discrepancy: Decimal
    ties: bool
    tie_status: Literal["exact_tie", "ties_within_rounding_tolerance", "mismatch"]
    uplift_ratio: DiagnosticMetric
    uplift_pct: DiagnosticMetric
    classified_adjustments: list[ClassifiedNonGaapAdjustment]
    warnings: list[NonGaapWarningFlag]
    formula: str
    input_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class GuidanceAndNonGaapResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str | None = None
    symbol: str | None = None
    period_label: str
    guidance_evaluations: list[GuidanceMetricEvaluation]
    nongaap_verifications: list[NonGaapBridgeVerification]
    warnings: list[NonGaapWarningFlag]


def _resolve_guidance_range_midpoint(
    rng: GuidanceRangeInput,
    aligned_map: dict[str, Decimal],
    prefix: str,
) -> tuple[Decimal | None, Decimal, Decimal | None, str]:
    low = aligned_map.get(f"{prefix}.low")
    mid = aligned_map.get(f"{prefix}.mid")
    high = aligned_map.get(f"{prefix}.high")
    if mid is not None:
        return low, mid, high, "explicit_mid"
    assert low is not None and high is not None
    with localcontext() as ctx:
        ctx.prec = 100
        calc_mid = (low + high) / Decimal(2)
    return low, calc_mid, high, "(low + high) / 2"


def _classify_surprise(
    actual: Decimal,
    benchmark: Decimal,
    inline_tol_pct: Decimal,
) -> tuple[SurpriseOutcome, Decimal, Decimal | None]:
    abs_diff = actual - benchmark
    if benchmark == 0:
        if abs_diff > 0:
            return "Beat", abs_diff, None
        if abs_diff < 0:
            return "Miss", abs_diff, None
        return "Inline", abs_diff, Decimal(0)
    with localcontext() as ctx:
        ctx.prec = 100
        pct_diff = (abs_diff / abs(benchmark)) * Decimal(100)
    if abs(pct_diff) <= inline_tol_pct:
        outcome: SurpriseOutcome = "Inline"
    elif abs_diff > 0:
        outcome = "Beat"
    else:
        outcome = "Miss"
    return outcome, abs_diff, pct_diff


def _evaluate_guidance_metric(
    item: GuidanceMetricInput,
    *,
    inline_tol_pct: Decimal,
    maintain_tol_pct: Decimal,
    decimal_places: int,
) -> GuidanceMetricEvaluation:
    # Align current-period items (actual, prior_guidance, consensus) separately from next-period items
    # if needed, or all together if compatible. Aligning all pairs together verifies shared currency/scale.
    pairs: list[tuple[str, MetricValue]] = []
    if item.actual is not None:
        pairs.append(("actual", item.actual))
    if item.prior_guidance is not None:
        pairs.extend(item.prior_guidance.items_for_alignment("prior_guidance"))
    if item.consensus is not None:
        pairs.append(("consensus", item.consensus))
    if item.new_guidance is not None:
        pairs.extend(item.new_guidance.items_for_alignment("new_guidance"))
    if item.new_guidance_benchmark is not None:
        if isinstance(item.new_guidance_benchmark, GuidanceRangeInput):
            pairs.extend(item.new_guidance_benchmark.items_for_alignment("new_bench"))
        else:
            pairs.append(("new_bench.mid", item.new_guidance_benchmark))

    aligned_vals, resolved_curr, resolved_unit = _resolve_currency_and_scale(pairs, expected_family="any")
    aligned_map = dict(zip((k for k, _ in pairs), aligned_vals))

    # 1. vs Prior 8-K 99.1 Guidance Midpoint
    prior_prov = [
        (k, mv)
        for k, mv in pairs
        if k == "actual" or k.startswith("prior_guidance.")
    ]
    prior_in_ids, prior_xbrl, prior_srcs = _provenance_lists(prior_prov)
    if item.actual is None or item.prior_guidance is None:
        reason = "Actual missing." if item.actual is None else "Prior 8-K Exhibit 99.1 guidance not provided."
        vs_prior = SurpriseComparison(
            status="unavailable",
            benchmark_type="prior_guidance_midpoint",
            outcome="Unavailable",
            unit=resolved_unit,
            currency=resolved_curr,
            formula="actual - guidance_midpoint; (actual - guidance_midpoint) / |guidance_midpoint| * 100",
            input_ids=prior_in_ids,
            xbrl_tags=prior_xbrl,
            source_ids=prior_srcs,
            reason=reason,
        )
    else:
        actual_v = aligned_map["actual"]
        g_low, g_mid, g_high, mid_expr = _resolve_guidance_range_midpoint(
            item.prior_guidance, aligned_map, "prior_guidance"
        )
        outcome, abs_diff, pct_diff = _classify_surprise(actual_v, g_mid, inline_tol_pct)
        if g_low is not None and g_high is not None:
            if actual_v > g_high:
                rng_pos: RangePosition = "above_high"
            elif actual_v < g_low:
                rng_pos = "below_low"
            else:
                rng_pos = "within_range"
        else:
            rng_pos = "midpoint_only"
        vs_prior = SurpriseComparison(
            status="computed",
            benchmark_type="prior_guidance_midpoint",
            outcome=outcome,
            actual_value=_quantize(actual_v, decimal_places),
            benchmark_value=_quantize(g_mid, decimal_places),
            guidance_low=_quantize(g_low, decimal_places) if g_low is not None else None,
            guidance_high=_quantize(g_high, decimal_places) if g_high is not None else None,
            range_position=rng_pos,
            abs_diff=_quantize(abs_diff, decimal_places),
            pct_diff=_quantize(pct_diff, decimal_places) if pct_diff is not None else None,
            unit=resolved_unit,
            currency=resolved_curr,
            formula=f"midpoint={mid_expr}; abs=actual-midpoint; pct=(actual-midpoint)/|midpoint|*100",
            input_ids=prior_in_ids,
            xbrl_tags=prior_xbrl,
            source_ids=prior_srcs,
            reason="Prior guidance midpoint is zero; percentage surprise undefined." if pct_diff is None else None,
        )

    # 2. vs Consensus
    cons_prov = [
        (k, mv)
        for k, mv in pairs
        if k in ("actual", "consensus")
    ]
    cons_in_ids, cons_xbrl, cons_srcs = _provenance_lists(cons_prov)
    if item.actual is None or item.consensus is None:
        reason = "Actual missing." if item.actual is None else "Consensus estimate unavailable."
        vs_cons = SurpriseComparison(
            status="unavailable",
            benchmark_type="consensus",
            outcome="Unavailable",
            unit=resolved_unit,
            currency=resolved_curr,
            formula="actual - consensus; (actual - consensus) / |consensus| * 100",
            input_ids=cons_in_ids,
            xbrl_tags=cons_xbrl,
            source_ids=cons_srcs,
            reason=reason,
        )
    else:
        actual_v = aligned_map["actual"]
        cons_v = aligned_map["consensus"]
        outcome, abs_diff, pct_diff = _classify_surprise(actual_v, cons_v, inline_tol_pct)
        vs_cons = SurpriseComparison(
            status="computed",
            benchmark_type="consensus",
            outcome=outcome,
            actual_value=_quantize(actual_v, decimal_places),
            benchmark_value=_quantize(cons_v, decimal_places),
            range_position="unavailable",
            abs_diff=_quantize(abs_diff, decimal_places),
            pct_diff=_quantize(pct_diff, decimal_places) if pct_diff is not None else None,
            unit=resolved_unit,
            currency=resolved_curr,
            formula="abs=actual-consensus; pct=(actual-consensus)/|consensus|*100",
            input_ids=cons_in_ids,
            xbrl_tags=cons_xbrl,
            source_ids=cons_srcs,
            reason="Consensus estimate is zero; percentage surprise undefined." if pct_diff is None else None,
        )

    # 3. New Guidance Direction (Raise / Maintain / Cut / Not Provided)
    new_prov = [(k, mv) for k, mv in pairs if k.startswith("new_guidance.") or k.startswith("new_bench.")]
    new_in_ids, _, new_srcs = _provenance_lists(new_prov)
    if item.new_guidance is None:
        new_dir = NewGuidanceDirectionResult(
            direction="Not Provided",
            unit=resolved_unit,
            currency=resolved_curr,
            formula="new_guidance_mid - benchmark_mid",
            input_ids=new_in_ids,
            source_ids=new_srcs,
            reason="Management did not provide new guidance for this metric.",
        )
    else:
        n_low, n_mid, n_high, n_expr = _resolve_guidance_range_midpoint(item.new_guidance, aligned_map, "new_guidance")
        if item.new_guidance_benchmark is None:
            new_dir = NewGuidanceDirectionResult(
                direction="Not Provided",
                new_low=_quantize(n_low, decimal_places) if n_low is not None else None,
                new_mid=_quantize(n_mid, decimal_places),
                new_high=_quantize(n_high, decimal_places) if n_high is not None else None,
                unit=resolved_unit,
                currency=resolved_curr,
                formula=f"new_mid={n_expr}; requires benchmark (prior same-horizon guidance or consensus) for direction",
                input_ids=new_in_ids,
                source_ids=new_srcs,
                reason="New guidance values recorded, but no comparison benchmark was provided to determine Raise/Maintain/Cut.",
            )
        else:
            if isinstance(item.new_guidance_benchmark, GuidanceRangeInput):
                _, b_mid, _, b_expr = _resolve_guidance_range_midpoint(
                    item.new_guidance_benchmark, aligned_map, "new_bench"
                )
            else:
                b_mid = aligned_map["new_bench.mid"]
                b_expr = "benchmark_value"
            abs_chg = n_mid - b_mid
            if b_mid == 0:
                pct_chg: Decimal | None = None
                if abs_chg > 0:
                    direction: NewGuidanceDirection = "Raise"
                elif abs_chg < 0:
                    direction = "Cut"
                else:
                    direction = "Maintain"
            else:
                with localcontext() as ctx:
                    ctx.prec = 100
                    pct_chg = (abs_chg / abs(b_mid)) * Decimal(100)
                if abs(pct_chg) <= maintain_tol_pct:
                    direction = "Maintain"
                elif abs_chg > 0:
                    direction = "Raise"
                else:
                    direction = "Cut"
            new_dir = NewGuidanceDirectionResult(
                direction=direction,
                new_low=_quantize(n_low, decimal_places) if n_low is not None else None,
                new_mid=_quantize(n_mid, decimal_places),
                new_high=_quantize(n_high, decimal_places) if n_high is not None else None,
                benchmark_mid=_quantize(b_mid, decimal_places),
                abs_change=_quantize(abs_chg, decimal_places),
                pct_change=_quantize(pct_chg, decimal_places) if pct_chg is not None else None,
                unit=resolved_unit,
                currency=resolved_curr,
                formula=f"new_mid={n_expr}; bench_mid={b_expr}; change=new_mid-bench_mid",
                input_ids=new_in_ids,
                source_ids=new_srcs,
            )

    return GuidanceMetricEvaluation(
        metric_name=item.metric_name,
        period_label=item.period_label,
        vs_prior_guidance=vs_prior,
        vs_consensus=vs_cons,
        new_guidance_direction=new_dir,
    )


def _classify_adjustment(adj: NonGaapAdjustmentItem) -> tuple[NonGaapCategory, bool, str | None]:
    cat = adj.category
    if cat is None:
        cat = "other"
        for candidate, pat in _ADJUSTMENT_CATEGORY_PATTERNS:
            if pat.search(adj.label):
                cat = candidate
                break

    if adj.is_recurring is True:
        return cat, True, "Explicitly marked as recurring adjustment across periods."
    if adj.is_recurring is False:
        return cat, False, None

    if adj.prior_periods_present is not None and adj.prior_periods_present >= 2:
        return (
            cat,
            True,
            f"Adjustment '{adj.label}' appeared in {adj.prior_periods_present} prior periods (repeated exclusion).",
        )
    if cat in _STRUCTURALLY_RECURRING_CATEGORIES:
        return (
            cat,
            True,
            f"Category '{cat}' is a structurally recurring operating/non-cash expense routinely excluded in Non-GAAP.",
        )
    if cat in _SOMETIMES_RECURRING_CATEGORIES and adj.prior_periods_present == 1:
        return (
            cat,
            True,
            f"Category '{cat}' has appeared in consecutive periods (check SEC Reg G / Item 10(e) recurring-cost rule).",
        )
    return cat, False, None


def _verify_nongaap_bridge(
    bridge: NonGaapBridgeInput,
    *,
    decimal_places: int,
) -> NonGaapBridgeVerification:
    pairs: list[tuple[str, MetricValue]] = [
        ("gaap_value", bridge.gaap_value),
        ("reported_non_gaap_value", bridge.reported_non_gaap_value),
    ]
    for idx, adj in enumerate(bridge.adjustments):
        pairs.append((f"adjustment[{idx}]:{adj.label}", adj.amount))

    aligned_vals, resolved_curr, resolved_unit = _resolve_currency_and_scale(pairs, expected_family="any")
    gaap_v = aligned_vals[0]
    rep_ng_v = aligned_vals[1]
    adj_vals = aligned_vals[2:]

    adj_sum = sum(adj_vals, Decimal(0))
    computed_ng = gaap_v + adj_sum
    discrepancy = rep_ng_v - computed_ng

    if discrepancy == 0:
        ties = True
        tie_status: Literal["exact_tie", "ties_within_rounding_tolerance", "mismatch"] = "exact_tie"
    elif abs(discrepancy) <= bridge.rounding_tolerance:
        ties = True
        tie_status = "ties_within_rounding_tolerance"
    else:
        ties = False
        tie_status = "mismatch"

    all_in_ids, _, all_srcs = _provenance_lists(pairs)
    warnings: list[NonGaapWarningFlag] = []

    if not ties:
        warnings.append(
            NonGaapWarningFlag(
                flag_id="nongaap_bridge_arithmetic_mismatch",
                severity="red",
                metric_name=bridge.metric_name,
                summary=(
                    f"Non-GAAP bridge arithmetic does not tie: GAAP ({gaap_v}) + adjustments ({adj_sum}) "
                    f"= {computed_ng}, vs reported Non-GAAP {rep_ng_v} (discrepancy {discrepancy})."
                ),
                investigation_prompt=(
                    "Re-check 8-K Exhibit 99.1 Non-GAAP reconciliation table for omitted line items, "
                    "sign inversions, per-share rounding effects, or diluted share count differences."
                ),
                input_ids=all_in_ids,
            )
        )

    # Uplift ratio: (Non-GAAP - GAAP) / |GAAP|
    uplift_formula = "(reported_non_gaap_value - gaap_value) / abs(gaap_value)"
    uplift_pct_formula = "((reported_non_gaap_value - gaap_value) / abs(gaap_value)) * 100"
    base_prov = pairs[:2]
    if gaap_v == 0:
        uplift_ratio_m = _metric_unavailable(
            "nongaap_uplift_ratio", "ratio", uplift_formula, "GAAP value is zero; uplift ratio undefined.", provenance=base_prov
        )
        uplift_pct_m = _metric_unavailable(
            "nongaap_uplift_pct", "percent", uplift_pct_formula, "GAAP value is zero; uplift percentage undefined.", provenance=base_prov
        )
    else:
        with localcontext() as ctx:
            ctx.prec = 100
            u_ratio = (rep_ng_v - gaap_v) / abs(gaap_v)
            u_pct = u_ratio * Decimal(100)
        uplift_ratio_m = _metric_computed(
            "nongaap_uplift_ratio", u_ratio, "ratio", uplift_formula, base_prov, decimal_places=decimal_places
        )
        uplift_pct_m = _metric_computed(
            "nongaap_uplift_pct", u_pct, "percent", uplift_pct_formula, base_prov, decimal_places=decimal_places
        )
        if gaap_v < 0 < rep_ng_v:
            warnings.append(
                NonGaapWarningFlag(
                    flag_id="nongaap_loss_to_profit_conversion",
                    severity="red",
                    metric_name=bridge.metric_name,
                    summary=f"Non-GAAP adjustments convert a GAAP loss ({gaap_v}) into a Non-GAAP profit ({rep_ng_v}).",
                    investigation_prompt=(
                        "Examine every Non-GAAP add-back in 8-K Ex-99.1 and 10-Q footnotes to evaluate whether "
                        "core operating profitability exists without exclusions."
                    ),
                    input_ids=all_in_ids,
                )
            )
        elif u_ratio > Decimal("0.50"):
            warnings.append(
                NonGaapWarningFlag(
                    flag_id="nongaap_high_uplift_ratio",
                    severity="red",
                    metric_name=bridge.metric_name,
                    summary=f"Large Non-GAAP uplift ratio of {uplift_ratio_m.value}x (+{uplift_pct_m.value}% over GAAP).",
                    investigation_prompt=(
                        "Assess sustainability and cash vs non-cash nature of add-backs in the 8-K Non-GAAP "
                        "reconciliation and related 10-Q/10-K expense footnotes."
                    ),
                    input_ids=all_in_ids,
                )
            )
        elif u_ratio > Decimal("0.25"):
            warnings.append(
                NonGaapWarningFlag(
                    flag_id="nongaap_moderate_uplift_ratio",
                    severity="yellow",
                    metric_name=bridge.metric_name,
                    summary=f"Notable Non-GAAP uplift ratio of {uplift_ratio_m.value}x (+{uplift_pct_m.value}% over GAAP).",
                    investigation_prompt=(
                        "Review Non-GAAP reconciliation disclosures in 8-K Ex-99.1 to quantify recurring add-backs "
                        "like SBC and intangible amortization."
                    ),
                    input_ids=all_in_ids,
                )
            )

    classified: list[ClassifiedNonGaapAdjustment] = []
    for idx, (adj, adj_v) in enumerate(zip(bridge.adjustments, adj_vals)):
        cat, rec_flag, rec_reason = _classify_adjustment(adj)
        adj_in_ids = [adj.amount.provenance_id(f"adjustment[{idx}]:{adj.label}")]
        classified.append(
            ClassifiedNonGaapAdjustment(
                label=adj.label,
                amount=_quantize(adj_v, decimal_places),
                unit=resolved_unit,
                currency=resolved_curr,
                category=cat,
                recurring_flag=rec_flag,
                recurring_reason=rec_reason,
                input_ids=adj_in_ids,
            )
        )
        if rec_flag:
            sev: FlagSeverity = "red" if (adj.prior_periods_present or 0) >= 2 and cat in _SOMETIMES_RECURRING_CATEGORIES else "yellow"
            warnings.append(
                NonGaapWarningFlag(
                    flag_id=f"nongaap_recurring_adjustment_{cat}",
                    severity=sev,
                    metric_name=bridge.metric_name,
                    adjustment_label=adj.label,
                    category=cat,
                    summary=f"Recurring Non-GAAP adjustment '{adj.label}' ({adj_v}): {rec_reason}",
                    investigation_prompt=(
                        f"Verify '{adj.label}' in 8-K Non-GAAP disclosures and 10-Q/10-K footnotes under SEC Reg G / "
                        "C&DI 100.01 (check multi-quarter frequency and true economic cost)."
                    ),
                    input_ids=adj_in_ids,
                )
            )

    return NonGaapBridgeVerification(
        metric_name=bridge.metric_name,
        period_label=bridge.period_label,
        unit=resolved_unit,
        currency=resolved_curr,
        gaap_value=_quantize(gaap_v, decimal_places),
        reported_non_gaap_value=_quantize(rep_ng_v, decimal_places),
        adjustments_sum=_quantize(adj_sum, decimal_places),
        computed_non_gaap_value=_quantize(computed_ng, decimal_places),
        discrepancy=_quantize(discrepancy, decimal_places),
        ties=ties,
        tie_status=tie_status,
        uplift_ratio=uplift_ratio_m,
        uplift_pct=uplift_pct_m,
        classified_adjustments=classified,
        warnings=warnings,
        formula="computed_non_gaap = gaap_value + sum(adjustments); discrepancy = reported_non_gaap - computed_non_gaap",
        input_ids=all_in_ids,
        source_ids=all_srcs,
    )


def guidance_and_nongaap_reconciler(request: GuidanceAndNonGaapRequest) -> GuidanceAndNonGaapResult:
    """Deterministically evaluate Beat/Miss vs 8-K guidance & consensus and verify Non-GAAP bridges."""
    guidance_evals = [
        _evaluate_guidance_metric(
            item,
            inline_tol_pct=request.inline_tolerance_pct,
            maintain_tol_pct=request.maintain_tolerance_pct,
            decimal_places=request.decimal_places,
        )
        for item in request.guidance_metrics
    ]
    nongaap_evals = [
        _verify_nongaap_bridge(bridge, decimal_places=request.decimal_places)
        for bridge in request.nongaap_bridges
    ]
    all_warnings = [w for bridge_res in nongaap_evals for w in bridge_res.warnings]
    return GuidanceAndNonGaapResult(
        entity_id=request.entity_id,
        symbol=request.symbol,
        period_label=request.period_label,
        guidance_evaluations=guidance_evals,
        nongaap_verifications=nongaap_evals,
        warnings=all_warnings,
    )


# ============================================================================
# Engine 3: valuation_and_implied_expectations
# ============================================================================


class ValuationRequest(BaseModel):
    """Input payload for ``valuation_and_implied_expectations``."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    entity_id: str | None = Field(default=None, max_length=120)
    symbol: str | None = Field(default=None, max_length=32)
    sic: str | int | None = None
    sic_description: str | None = Field(default=None, max_length=200)
    currency: str | None = Field(default=None, min_length=1, max_length=16)
    monetary_unit: str | None = Field(default=None, min_length=1, max_length=100)

    # Market & Capital Structure
    close_price: MetricValue | None = None
    diluted_shares: MetricValue | None = None
    cash: MetricValue | None = None
    short_term_investments: MetricValue | None = None
    total_debt: MetricValue | None = None
    short_term_debt: MetricValue | None = None
    long_term_debt: MetricValue | None = None
    minority_interest: MetricValue | None = None
    preferred_stock: MetricValue | None = None

    # TTM Fundamentals
    ttm_revenue: MetricValue | None = None
    ttm_ebitda: MetricValue | None = None
    ttm_ebit: MetricValue | None = None
    ttm_depreciation_amortization: MetricValue | None = None
    ttm_net_income: MetricValue | None = None
    ttm_eps: MetricValue | None = None
    ttm_ocf: MetricValue | None = None
    ttm_capex: MetricValue | None = None
    ttm_fcf: MetricValue | None = None
    ttm_sbc: MetricValue | None = None

    # Optional NTM Consensus
    ntm_eps: MetricValue | None = None
    ntm_net_income: MetricValue | None = None
    ntm_revenue: MetricValue | None = None
    ntm_ebitda: MetricValue | None = None

    # Reverse-DCF Assumptions
    forecast_years: int = Field(default=5, ge=1, le=20)
    wacc: Decimal = Field(default=Decimal("0.09"))
    terminal_growth: Decimal = Field(default=Decimal("0.03"))
    terminal_method: Literal["gordon_growth", "exit_multiple"] = "gordon_growth"
    terminal_multiple: Decimal | None = Field(default=None)
    wacc_step: Decimal = Field(default=Decimal("0.01"))
    terminal_growth_step: Decimal = Field(default=Decimal("0.005"))
    decimal_places: int = Field(default=2, ge=0, le=6)

    @field_validator("wacc", "terminal_growth", "terminal_multiple", "wacc_step", "terminal_growth_step", mode="before")
    @classmethod
    def _validate_dcf_decimals(cls, raw: Any) -> Decimal | None:
        if raw is None:
            return None
        return _to_decimal(raw, field_name="dcf_parameter")

    @field_validator("currency")
    @classmethod
    def _norm_curr(cls, value: str | None) -> str | None:
        if value is None:
            return None
        upper = value.strip().upper()
        return "CNY" if upper == "RMB" else upper

    @model_validator(mode="after")
    def _validate_dcf_bounds(self) -> ValuationRequest:
        if not (Decimal("0") < self.wacc < Decimal("1")):
            raise ValueError("wacc must be in (0, 1) (e.g. Decimal('0.09') for 9%).")
        if not (Decimal("-0.05") <= self.terminal_growth < Decimal("1")):
            raise ValueError("terminal_growth must be in [-0.05, 1) (e.g. Decimal('0.03') for 3%).")
        if not (Decimal("0") < self.wacc_step <= Decimal("0.10")):
            raise ValueError("wacc_step must be in (0, 0.10].")
        if not (Decimal("0") < self.terminal_growth_step <= Decimal("0.05")):
            raise ValueError("terminal_growth_step must be in (0, 0.05].")
        if self.terminal_method == "exit_multiple":
            if self.terminal_multiple is None or self.terminal_multiple <= 0 or self.terminal_multiple > Decimal("200"):
                raise ValueError("terminal_multiple in (0, 200] is required when terminal_method='exit_multiple'.")
        elif self.terminal_multiple is not None and (self.terminal_multiple <= 0 or self.terminal_multiple > Decimal("200")):
            raise ValueError("terminal_multiple must be in (0, 200].")
        return self

    @model_validator(mode="before")
    @classmethod
    def _coerce_numeric_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        copied = dict(data)
        fields = (
            "close_price", "diluted_shares", "cash", "short_term_investments",
            "total_debt", "short_term_debt", "long_term_debt", "minority_interest", "preferred_stock",
            "ttm_revenue", "ttm_ebitda", "ttm_ebit", "ttm_depreciation_amortization",
            "ttm_net_income", "ttm_eps", "ttm_ocf", "ttm_capex", "ttm_fcf", "ttm_sbc",
            "ntm_eps", "ntm_net_income", "ntm_revenue", "ntm_ebitda",
        )
        for fname in fields:
            if fname in copied and copied[fname] is not None:
                copied[fname] = _coerce_metric_value(copied[fname], fallback_fact_id=f"valuation:{fname}")
        return copied


class ReverseDcfSensitivityCell(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wacc_pct: Decimal
    terminal_growth_pct: Decimal
    status: MetricStatus
    implied_5y_fcf_cagr_pct: Decimal | None = None
    implied_exit_fcf_multiple: Decimal | None = None
    reason: str | None = None


class ReverseDcfResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: MetricStatus
    reason: str | None = None
    terminal_method: Literal["gordon_growth", "exit_multiple"]
    forecast_years: int
    wacc_pct: Decimal
    terminal_growth_pct: Decimal
    terminal_multiple_input: Decimal | None = None
    target_ev: Decimal | None = None
    base_ttm_fcf: Decimal | None = None
    implied_5y_fcf_cagr_pct: Decimal | None = None
    implied_year_n_fcf: Decimal | None = None
    pv_explicit_fcf: Decimal | None = None
    pv_terminal_value: Decimal | None = None
    terminal_value_share_pct: Decimal | None = None
    implied_exit_fcf_multiple: Decimal | None = None
    ev_residual_pct: Decimal | None = None
    wacc_axis_pct: list[Decimal] = Field(default_factory=list)
    terminal_growth_axis_pct: list[Decimal] = Field(default_factory=list)
    sensitivity_matrix: list[list[ReverseDcfSensitivityCell]] = Field(default_factory=list)
    formula: str
    input_ids: list[str] = Field(default_factory=list)


class ValuationAndExpectationsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str | None = None
    symbol: str | None = None
    monetary_unit: str
    currency: str | None = None
    sector_applicability: SectorApplicability
    market_cap: DiagnosticMetric
    enterprise_value: DiagnosticMetric
    pe_ttm: DiagnosticMetric
    pe_forward: DiagnosticMetric
    ev_to_sales: DiagnosticMetric
    ev_to_ntm_sales: DiagnosticMetric
    ev_to_ebitda: DiagnosticMetric
    ev_to_ntm_ebitda: DiagnosticMetric
    ev_to_ebit: DiagnosticMetric
    p_to_fcf: DiagnosticMetric
    fcf_yield_pct: DiagnosticMetric
    fcf_ex_sbc: DiagnosticMetric
    ev_to_fcf_ex_sbc: DiagnosticMetric
    fcf_ex_sbc_yield_pct: DiagnosticMetric
    reverse_dcf: ReverseDcfResult


def _dcf_ev_for_cagr(
    fcf0: Decimal,
    cagr: Decimal,
    years: int,
    wacc: Decimal,
    *,
    terminal_method: Literal["gordon_growth", "exit_multiple"],
    terminal_growth: Decimal,
    terminal_multiple: Decimal | None,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    """Return ``(total_ev, pv_explicit, pv_terminal, fcf_n, exit_multiple)`` inside prec=100."""
    one = Decimal(1)
    g_factor = one + cagr
    disc_factor = one + wacc
    pv_explicit = Decimal(0)
    fcf_t = fcf0
    disc_t = one
    for _ in range(years):
        fcf_t = fcf_t * g_factor
        disc_t = disc_t * disc_factor
        pv_explicit += fcf_t / disc_t
    fcf_n = fcf_t
    if terminal_method == "exit_multiple":
        assert terminal_multiple is not None
        exit_mult = terminal_multiple
        tv = fcf_n * exit_mult
    else:
        spread = wacc - terminal_growth
        exit_mult = (one + terminal_growth) / spread
        tv = fcf_n * exit_mult
    pv_terminal = tv / disc_t
    return pv_explicit + pv_terminal, pv_explicit, pv_terminal, fcf_n, exit_mult


def _solve_reverse_dcf_cagr(
    *,
    target_ev: Decimal,
    fcf0: Decimal,
    years: int,
    wacc: Decimal,
    terminal_method: Literal["gordon_growth", "exit_multiple"],
    terminal_growth: Decimal,
    terminal_multiple: Decimal | None,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]:
    """Solve ``V(cagr) = target_ev`` via deterministic Decimal bisection."""
    with localcontext() as ctx:
        ctx.prec = 100
        lo = Decimal("-0.999")
        for _ in range(25):
            ev_lo, *_ = _dcf_ev_for_cagr(
                fcf0, lo, years, wacc,
                terminal_method=terminal_method, terminal_growth=terminal_growth, terminal_multiple=terminal_multiple,
            )
            if ev_lo <= target_ev:
                break
            lo = (lo - Decimal(1)) / Decimal(2)  # halve distance toward -1.0

        hi = Decimal("2.0")
        for _ in range(25):
            ev_hi, *_ = _dcf_ev_for_cagr(
                fcf0, hi, years, wacc,
                terminal_method=terminal_method, terminal_growth=terminal_growth, terminal_multiple=terminal_multiple,
            )
            if ev_hi >= target_ev:
                break
            hi *= Decimal(2)

        if ev_lo > target_ev or ev_hi < target_ev:
            raise DiagnosticsError("Reverse-DCF root outside solvable CAGR bracket (-99.99999% to +6.7e7%).")

        mid = Decimal(0)
        for _ in range(140):
            mid = (lo + hi) / Decimal(2)
            ev_mid, *_ = _dcf_ev_for_cagr(
                fcf0, mid, years, wacc,
                terminal_method=terminal_method, terminal_growth=terminal_growth, terminal_multiple=terminal_multiple,
            )
            if ev_mid == target_ev or (hi - lo) < Decimal("1e-24"):
                break
            if ev_mid < target_ev:
                lo = mid
            else:
                hi = mid

        ev_final, pv_exp, pv_term, fcf_n, exit_mult = _dcf_ev_for_cagr(
            fcf0, mid, years, wacc,
            terminal_method=terminal_method, terminal_growth=terminal_growth, terminal_multiple=terminal_multiple,
        )
        residual_pct = (abs(ev_final - target_ev) / target_ev) * Decimal(100)
        return mid, pv_exp, pv_term, fcf_n, exit_mult, residual_pct


def valuation_and_implied_expectations(request: ValuationRequest) -> ValuationAndExpectationsResult:
    """Compute valuation multiples and deterministic bisection Reverse-DCF from market & TTM inputs."""
    sector = evaluate_sec_sic_gate(request.sic, request.sic_description)
    disabled = set(sector.disabled_metrics)

    # 1. Resolve shared currency & monetary scale across monetary inputs
    monetary_fields = (
        "cash", "short_term_investments", "total_debt", "short_term_debt", "long_term_debt",
        "minority_interest", "preferred_stock", "ttm_revenue", "ttm_ebitda", "ttm_ebit",
        "ttm_depreciation_amortization", "ttm_net_income", "ttm_ocf", "ttm_capex", "ttm_fcf",
        "ttm_sbc", "ntm_net_income", "ntm_revenue", "ntm_ebitda",
    )
    mon_pairs: list[tuple[str, MetricValue]] = []
    for fname in monetary_fields:
        mv = getattr(request, fname)
        if mv is not None:
            mon_pairs.append((fname, mv))

    # Also validate currencies on per-share price/EPS without letting per-share unit affect monetary exponent.
    per_share_currencies: set[str] = set()
    for ps_name in ("close_price", "ttm_eps", "ntm_eps"):
        ps_mv: MetricValue | None = getattr(request, ps_name)
        if ps_mv is not None:
            if ps_mv.currency:
                per_share_currencies.add(ps_mv.currency)
            if ps_mv.unit:
                norm_u = ps_mv.unit.replace("-per-", " per ").strip()
                if re.search(r"(?:per\s+share|/\s*shares?\b|每股)", norm_u, re.I):
                    spec = _parse_unit_safe(norm_u, ps_mv.currency or request.currency)
                else:
                    spec = _parse_unit_safe(norm_u, ps_mv.currency or request.currency)
                if spec.currency:
                    per_share_currencies.add(spec.currency)

    mon_vals, mon_curr, mon_unit = _resolve_currency_and_scale(
        mon_pairs,
        default_currency=request.currency,
        default_monetary_unit=request.monetary_unit,
        expected_family="monetary",
    )
    if mon_curr is not None and per_share_currencies and per_share_currencies != {mon_curr}:
        raise DiagnosticsError(
            f"Cross-currency conflict between fundamentals ({mon_curr}) and per-share inputs ({sorted(per_share_currencies)})."
        )
    if len(per_share_currencies) > 1:
        raise DiagnosticsError(f"Cross-currency conflict among per-share inputs: {sorted(per_share_currencies)}.")

    resolved_curr = mon_curr or next(iter(per_share_currencies), None)
    if mon_pairs:
        first_mon_spec = (
            _parse_unit_safe(mon_unit, resolved_curr)
            if any(mv.unit for _, mv in mon_pairs) or request.monetary_unit
            else None
        )
        mon_exp = first_mon_spec.exponent if first_mon_spec is not None else 0
    else:
        mon_exp = 0
        if request.monetary_unit:
            mon_exp = _parse_unit_safe(request.monetary_unit, resolved_curr).exponent
            mon_unit = request.monetary_unit
        elif resolved_curr:
            mon_unit = resolved_curr

    mval: dict[str, Decimal] = dict(zip((k for k, _ in mon_pairs), mon_vals))

    def mpair(name: str) -> tuple[str, MetricValue] | None:
        mv = getattr(request, name)
        return (f"valuation.{name}", mv) if mv is not None else None

    dp = request.decimal_places

    # 2. Market Cap = close_price * diluted_shares (aligned to monetary exponent)
    px_mv = request.close_price
    sh_mv = request.diluted_shares
    px_pair = mpair("close_price")
    sh_pair = mpair("diluted_shares")
    mcap_val: Decimal | None = None
    mcap_prov = [p for p in (px_pair, sh_pair) if p is not None]
    mcap_formula = "close_price * diluted_shares"
    if px_mv is None or sh_mv is None:
        mcap_m = _metric_unavailable(
            "market_cap", mon_unit, mcap_formula, "Missing close_price or diluted_shares.", currency=resolved_curr, provenance=mcap_prov
        )
    elif px_mv.value <= 0 or sh_mv.value <= 0:
        mcap_m = _metric_unavailable(
            "market_cap", mon_unit, mcap_formula, "close_price and diluted_shares must both be positive.", currency=resolved_curr, provenance=mcap_prov
        )
    else:
        if sh_mv.unit:
            sh_spec = _parse_unit_safe(sh_mv.unit, None)
            if sh_spec.family != "shares":
                raise DiagnosticsError(f"diluted_shares unit {sh_mv.unit!r} must be a share count unit.")
            scale_shift = sh_spec.exponent - mon_exp
        else:
            # When diluted_shares omits unit, it is assumed to match the monetary unit scale.
            scale_shift = 0
        with localcontext() as ctx:
            ctx.prec = 100
            mcap_val = (px_mv.value * sh_mv.value).scaleb(scale_shift) if scale_shift else (px_mv.value * sh_mv.value)
        mcap_m = _metric_computed(
            "market_cap", mcap_val, mon_unit, mcap_formula, mcap_prov, decimal_places=dp, currency=resolved_curr
        )

    # 3. Balance Sheet components for Enterprise Value
    cash_v = mval.get("cash")
    sti_v = mval.get("short_term_investments")
    cash_prov = [p for p in (mpair("cash"), mpair("short_term_investments")) if p is not None]
    total_cash_v: Decimal | None = None
    if cash_v is not None:
        total_cash_v = cash_v + (sti_v if sti_v is not None else Decimal(0))
        cash_expr = "cash + short_term_investments" if sti_v is not None else "cash"
    elif sti_v is not None:
        total_cash_v = sti_v
        cash_expr = "short_term_investments"
    else:
        cash_expr = "cash"

    td_v = mval.get("total_debt")
    if td_v is not None:
        total_debt_v: Decimal | None = td_v
        debt_prov = [p for p in (mpair("total_debt"),) if p is not None]
        debt_expr = "total_debt"
    else:
        st_v, lt_v = mval.get("short_term_debt"), mval.get("long_term_debt")
        debt_prov = [p for p in (mpair("short_term_debt"), mpair("long_term_debt")) if p is not None]
        if st_v is not None or lt_v is not None:
            total_debt_v = (st_v if st_v is not None else Decimal(0)) + (lt_v if lt_v is not None else Decimal(0))
            debt_expr = "short_term_debt + long_term_debt"
        elif mval.get("total_assets") is not None and mval.get("total_liabilities") is not None:
            # Balance sheet is demonstrably present with no reported short/long-term debt tags -> debt-free issuer
            total_debt_v = Decimal(0)
            debt_expr = "0 (no debt tags on complete balance sheet)"
        else:
            total_debt_v = None
            debt_expr = "total_debt"

    mi_v = mval.get("minority_interest", Decimal(0))
    pref_v = mval.get("preferred_stock", Decimal(0))
    mi_pref_prov = [p for p in (mpair("minority_interest"), mpair("preferred_stock")) if p is not None]

    ev_formula = f"market_cap + {debt_expr} + minority_interest + preferred_stock - ({cash_expr})"
    ev_prov = [*mcap_prov, *debt_prov, *mi_pref_prov, *cash_prov]
    ev_val: Decimal | None = None
    if "enterprise_value" in disabled:
        ev_m = _metric_disabled("enterprise_value", mon_unit, ev_formula, sector, currency=resolved_curr)
    elif mcap_val is None or total_cash_v is None or total_debt_v is None:
        missing = [
            lbl
            for lbl, v in (("market_cap", mcap_val), ("cash", total_cash_v), ("total_debt", total_debt_v))
            if v is None
        ]
        ev_m = _metric_unavailable(
            "enterprise_value",
            mon_unit,
            ev_formula,
            f"Missing inputs for Enterprise Value: {', '.join(missing)}.",
            currency=resolved_curr,
            provenance=ev_prov,
        )
    else:
        ev_val = mcap_val + total_debt_v + mi_v + pref_v - total_cash_v
        ev_m = _metric_computed(
            "enterprise_value", ev_val, mon_unit, ev_formula, ev_prov, decimal_places=dp, currency=resolved_curr
        )

    # 4. P/E TTM & Forward
    ttm_ni_v = mval.get("ttm_net_income")
    ttm_ni_p = mpair("ttm_net_income")
    ttm_eps_mv = request.ttm_eps
    ttm_eps_p = mpair("ttm_eps")
    if mcap_val is not None and ttm_ni_v is not None and ttm_ni_p is not None:
        if ttm_ni_v <= 0:
            pe_ttm_m = _metric_unavailable(
                "pe_ttm", "multiple", "market_cap / ttm_net_income", "TTM Net Income <= 0; P/E TTM not meaningful.", provenance=[*mcap_prov, ttm_ni_p]
            )
        else:
            with localcontext() as ctx:
                ctx.prec = 100
                pe_ttm_m = _metric_computed(
                    "pe_ttm", mcap_val / ttm_ni_v, "multiple", "market_cap / ttm_net_income", [*mcap_prov, ttm_ni_p], decimal_places=dp
                )
    elif px_mv is not None and px_mv.value > 0 and ttm_eps_mv is not None and ttm_eps_p is not None:
        if ttm_eps_mv.value <= 0:
            pe_ttm_m = _metric_unavailable(
                "pe_ttm", "multiple", "close_price / ttm_eps", "TTM EPS <= 0; P/E TTM not meaningful.", provenance=[p for p in (px_pair, ttm_eps_p) if p]
            )
        else:
            with localcontext() as ctx:
                ctx.prec = 100
                pe_ttm_m = _metric_computed(
                    "pe_ttm", px_mv.value / ttm_eps_mv.value, "multiple", "close_price / ttm_eps", [p for p in (px_pair, ttm_eps_p) if p], decimal_places=dp
                )
    else:
        pe_ttm_m = _metric_unavailable(
            "pe_ttm", "multiple", "market_cap / ttm_net_income (or close_price / ttm_eps)", "Missing price/market_cap or TTM earnings.", provenance=[*mcap_prov, *[p for p in (ttm_ni_p, ttm_eps_p) if p]]
        )

    ntm_eps_mv = request.ntm_eps
    ntm_eps_p = mpair("ntm_eps")
    ntm_ni_v = mval.get("ntm_net_income")
    ntm_ni_p = mpair("ntm_net_income")
    if px_mv is not None and px_mv.value > 0 and ntm_eps_mv is not None and ntm_eps_p is not None:
        if ntm_eps_mv.value <= 0:
            pe_fwd_m = _metric_unavailable(
                "pe_forward", "multiple", "close_price / ntm_eps", "NTM consensus EPS <= 0; Forward P/E not meaningful.", provenance=[p for p in (px_pair, ntm_eps_p) if p]
            )
        else:
            with localcontext() as ctx:
                ctx.prec = 100
                pe_fwd_m = _metric_computed(
                    "pe_forward", px_mv.value / ntm_eps_mv.value, "multiple", "close_price / ntm_eps", [p for p in (px_pair, ntm_eps_p) if p], decimal_places=dp
                )
    elif mcap_val is not None and ntm_ni_v is not None and ntm_ni_p is not None:
        if ntm_ni_v <= 0:
            pe_fwd_m = _metric_unavailable(
                "pe_forward", "multiple", "market_cap / ntm_net_income", "NTM consensus Net Income <= 0.", provenance=[*mcap_prov, ntm_ni_p]
            )
        else:
            with localcontext() as ctx:
                ctx.prec = 100
                pe_fwd_m = _metric_computed(
                    "pe_forward", mcap_val / ntm_ni_v, "multiple", "market_cap / ntm_net_income", [*mcap_prov, ntm_ni_p], decimal_places=dp
                )
    else:
        pe_fwd_m = _metric_unavailable(
            "pe_forward", "multiple", "close_price / ntm_eps", "NTM consensus EPS / Net Income or close_price unavailable.", provenance=[p for p in (px_pair, ntm_eps_p, ntm_ni_p) if p]
        )

    # Helper for EV multiples
    def ev_multiple(
        metric_name: str,
        denom: Decimal | None,
        denom_prov: list[tuple[str, MetricValue]],
        denom_label: str,
    ) -> DiagnosticMetric:
        formula = f"enterprise_value / {denom_label}"
        if metric_name in disabled or ("standard_ev_ebitda" in disabled and "ebitda" in metric_name):
            return _metric_disabled(metric_name, "multiple", formula, sector)
        prov = [*ev_prov, *denom_prov]
        if ev_val is None:
            return _metric_unavailable(metric_name, "multiple", formula, ev_m.reason or "Enterprise Value unavailable.", provenance=prov)
        if denom is None:
            return _metric_unavailable(metric_name, "multiple", formula, f"Missing {denom_label}.", provenance=prov)
        if denom <= 0:
            return _metric_unavailable(metric_name, "multiple", formula, f"{denom_label} <= 0; multiple not meaningful.", provenance=prov)
        with localcontext() as ctx:
            ctx.prec = 100
            return _metric_computed(metric_name, ev_val / denom, "multiple", formula, prov, decimal_places=dp)

    ttm_rev_v = mval.get("ttm_revenue")
    ttm_rev_p = [p for p in (mpair("ttm_revenue"),) if p]
    ev_sales_m = ev_multiple("ev_to_sales", ttm_rev_v, ttm_rev_p, "ttm_revenue")

    ntm_rev_v = mval.get("ntm_revenue")
    ntm_rev_p = [p for p in (mpair("ntm_revenue"),) if p]
    ev_ntm_sales_m = ev_multiple("ev_to_ntm_sales", ntm_rev_v, ntm_rev_p, "ntm_revenue")

    # TTM EBITDA (direct or EBIT + D&A)
    ttm_ebit_v = mval.get("ttm_ebit")
    ttm_ebit_p = [p for p in (mpair("ttm_ebit"),) if p]
    ttm_da_v = mval.get("ttm_depreciation_amortization")
    ttm_da_p = [p for p in (mpair("ttm_depreciation_amortization"),) if p]
    if mval.get("ttm_ebitda") is not None:
        ttm_ebitda_v: Decimal | None = mval["ttm_ebitda"]
        ttm_ebitda_p = [p for p in (mpair("ttm_ebitda"),) if p]
        ttm_ebitda_lbl = "ttm_ebitda"
    elif ttm_ebit_v is not None and ttm_da_v is not None:
        ttm_ebitda_v = ttm_ebit_v + abs(ttm_da_v)
        ttm_ebitda_p = [*ttm_ebit_p, *ttm_da_p]
        ttm_ebitda_lbl = "(ttm_ebit + abs(ttm_da))"
    else:
        ttm_ebitda_v = None
        ttm_ebitda_p = []
        ttm_ebitda_lbl = "ttm_ebitda"

    ev_ebitda_m = ev_multiple("ev_to_ebitda", ttm_ebitda_v, ttm_ebitda_p, ttm_ebitda_lbl)
    ntm_ebitda_v = mval.get("ntm_ebitda")
    ntm_ebitda_p = [p for p in (mpair("ntm_ebitda"),) if p]
    ev_ntm_ebitda_m = ev_multiple("ev_to_ntm_ebitda", ntm_ebitda_v, ntm_ebitda_p, "ntm_ebitda")
    ev_ebit_m = ev_multiple("ev_to_ebit", ttm_ebit_v, ttm_ebit_p, "ttm_ebit")

    # 5. TTM FCF & FCF ex-SBC multiples / yields
    if mval.get("ttm_fcf") is not None:
        ttm_fcf_v: Decimal | None = mval["ttm_fcf"]
        ttm_fcf_p = [p for p in (mpair("ttm_fcf"),) if p]
        ttm_fcf_expr = "ttm_fcf"
    elif mval.get("ttm_ocf") is not None and mval.get("ttm_capex") is not None:
        ttm_fcf_v = mval["ttm_ocf"] - abs(mval["ttm_capex"])
        ttm_fcf_p = [p for p in (mpair("ttm_ocf"), mpair("ttm_capex")) if p]
        ttm_fcf_expr = "(ttm_ocf - abs(ttm_capex))"
    else:
        ttm_fcf_v = None
        ttm_fcf_p = []
        ttm_fcf_expr = "ttm_fcf"

    if "standard_fcf" in disabled or "p_to_fcf" in disabled:
        p_fcf_m = _metric_disabled("p_to_fcf", "multiple", f"market_cap / {ttm_fcf_expr}", sector)
        fcf_yield_m = _metric_disabled("fcf_yield_pct", "percent", f"({ttm_fcf_expr} / market_cap) * 100", sector)
    else:
        if mcap_val is None or ttm_fcf_v is None:
            p_fcf_m = _metric_unavailable(
                "p_to_fcf", "multiple", f"market_cap / {ttm_fcf_expr}", "Missing market_cap or TTM FCF.", provenance=[*mcap_prov, *ttm_fcf_p]
            )
            fcf_yield_m = _metric_unavailable(
                "fcf_yield_pct", "percent", f"({ttm_fcf_expr} / market_cap) * 100", "Missing market_cap or TTM FCF.", provenance=[*mcap_prov, *ttm_fcf_p]
            )
        else:
            with localcontext() as ctx:
                ctx.prec = 100
                fcf_yield_m = _metric_computed(
                    "fcf_yield_pct", (ttm_fcf_v / mcap_val) * Decimal(100), "percent", f"({ttm_fcf_expr} / market_cap) * 100", [*mcap_prov, *ttm_fcf_p], decimal_places=dp
                )
            if ttm_fcf_v <= 0:
                p_fcf_m = _metric_unavailable(
                    "p_to_fcf", "multiple", f"market_cap / {ttm_fcf_expr}", "TTM FCF <= 0; P/FCF multiple not meaningful.", provenance=[*mcap_prov, *ttm_fcf_p]
                )
            else:
                with localcontext() as ctx:
                    ctx.prec = 100
                    p_fcf_m = _metric_computed(
                        "p_to_fcf", mcap_val / ttm_fcf_v, "multiple", f"market_cap / {ttm_fcf_expr}", [*mcap_prov, *ttm_fcf_p], decimal_places=dp
                    )

    ttm_sbc_v = mval.get("ttm_sbc")
    ttm_sbc_p = [p for p in (mpair("ttm_sbc"),) if p]
    fcf_ex_sbc_expr = f"{ttm_fcf_expr} - ttm_sbc"
    if "standard_fcf" in disabled or "fcf_ex_sbc" in disabled:
        fcf_ex_sbc_v: Decimal | None = None
        fcf_ex_sbc_m = _metric_disabled("fcf_ex_sbc", mon_unit, fcf_ex_sbc_expr, sector, currency=resolved_curr)
        ev_fcf_ex_sbc_m = _metric_disabled("ev_to_fcf_ex_sbc", "multiple", f"enterprise_value / ({fcf_ex_sbc_expr})", sector)
        fcf_ex_sbc_yield_m = _metric_disabled("fcf_ex_sbc_yield_pct", "percent", f"(({fcf_ex_sbc_expr}) / market_cap) * 100", sector)
    elif ttm_fcf_v is None or ttm_sbc_v is None:
        fcf_ex_sbc_v = None
        prov = [*ttm_fcf_p, *ttm_sbc_p]
        fcf_ex_sbc_m = _metric_unavailable("fcf_ex_sbc", mon_unit, fcf_ex_sbc_expr, "Missing TTM FCF or TTM SBC.", currency=resolved_curr, provenance=prov)
        ev_fcf_ex_sbc_m = _metric_unavailable("ev_to_fcf_ex_sbc", "multiple", f"enterprise_value / ({fcf_ex_sbc_expr})", "Missing TTM FCF or TTM SBC.", provenance=[*ev_prov, *prov])
        fcf_ex_sbc_yield_m = _metric_unavailable("fcf_ex_sbc_yield_pct", "percent", f"(({fcf_ex_sbc_expr}) / market_cap) * 100", "Missing market_cap, TTM FCF, or TTM SBC.", provenance=[*mcap_prov, *prov])
    else:
        fcf_ex_sbc_v = ttm_fcf_v - ttm_sbc_v
        prov = [*ttm_fcf_p, *ttm_sbc_p]
        fcf_ex_sbc_m = _metric_computed("fcf_ex_sbc", fcf_ex_sbc_v, mon_unit, fcf_ex_sbc_expr, prov, decimal_places=dp, currency=resolved_curr)
        ev_fcf_ex_sbc_m = ev_multiple("ev_to_fcf_ex_sbc", fcf_ex_sbc_v, prov, f"({fcf_ex_sbc_expr})")
        if mcap_val is not None:
            with localcontext() as ctx:
                ctx.prec = 100
                fcf_ex_sbc_yield_m = _metric_computed(
                    "fcf_ex_sbc_yield_pct", (fcf_ex_sbc_v / mcap_val) * Decimal(100), "percent", f"(({fcf_ex_sbc_expr}) / market_cap) * 100", [*mcap_prov, *prov], decimal_places=dp
                )
        else:
            fcf_ex_sbc_yield_m = _metric_unavailable(
                "fcf_ex_sbc_yield_pct", "percent", f"(({fcf_ex_sbc_expr}) / market_cap) * 100", "Missing market_cap.", provenance=[*mcap_prov, *prov]
            )

    # 6. Deterministic Reverse-DCF + 3x3 Sensitivity Matrix
    rdcf_prov_list = [*ev_prov, *ttm_fcf_p]
    rdcf_input_ids, _, _ = _provenance_lists(rdcf_prov_list)
    rdcf_formula = (
        f"EV = sum(t=1..{request.forecast_years}, FCF0*(1+c)^t/(1+WACC)^t) + "
        + (
            "FCF_N * terminal_multiple / (1+WACC)^N"
            if request.terminal_method == "exit_multiple"
            else "FCF_N*(1+g)/((WACC-g)*(1+WACC)^N)"
        )
    )
    wacc_pct_disp = _quantize(request.wacc * Decimal(100), dp)
    tg_pct_disp = _quantize(request.terminal_growth * Decimal(100), dp)

    def unavailable_rdcf(status: MetricStatus, reason: str) -> ReverseDcfResult:
        return ReverseDcfResult(
            status=status,
            reason=reason,
            terminal_method=request.terminal_method,
            forecast_years=request.forecast_years,
            wacc_pct=wacc_pct_disp,
            terminal_growth_pct=tg_pct_disp,
            terminal_multiple_input=request.terminal_multiple,
            target_ev=_quantize(ev_val, dp) if ev_val is not None else None,
            base_ttm_fcf=_quantize(ttm_fcf_v, dp) if ttm_fcf_v is not None else None,
            formula=rdcf_formula,
            input_ids=rdcf_input_ids,
        )

    if "reverse_dcf" in disabled or "standard_fcf" in disabled:
        reverse_dcf = unavailable_rdcf("disabled_by_sector", sector.explanation)
    elif mcap_val is None or ev_val is None:
        reverse_dcf = unavailable_rdcf(
            "unavailable",
            ev_m.reason or mcap_m.reason or "Market price, diluted shares, or Enterprise Value missing for Reverse-DCF.",
        )
    elif ev_val <= 0:
        reverse_dcf = unavailable_rdcf(
            "unavailable",
            f"Enterprise Value <= 0 ({_quantize(ev_val, dp)}); Reverse-DCF implied growth rate is undefined for net-cash-negative-EV firms.",
        )
    elif ttm_fcf_v is None:
        reverse_dcf = unavailable_rdcf("unavailable", "TTM FCF is missing; Reverse-DCF cannot run.")
    elif ttm_fcf_v <= 0:
        reverse_dcf = unavailable_rdcf(
            "unavailable",
            f"TTM FCF <= 0 ({_quantize(ttm_fcf_v, dp)}); Reverse-DCF cannot compound non-positive base FCF.",
        )
    elif request.terminal_method == "gordon_growth" and request.wacc <= request.terminal_growth:
        reverse_dcf = unavailable_rdcf(
            "unavailable",
            f"WACC ({wacc_pct_disp}%) must exceed terminal growth ({tg_pct_disp}%) for Gordon growth terminal value.",
        )
    else:
        cagr, pv_exp, pv_term, fcf_n, exit_mult, residual_pct = _solve_reverse_dcf_cagr(
            target_ev=ev_val,
            fcf0=ttm_fcf_v,
            years=request.forecast_years,
            wacc=request.wacc,
            terminal_method=request.terminal_method,
            terminal_growth=request.terminal_growth,
            terminal_multiple=request.terminal_multiple,
        )
        wacc_grid = [request.wacc - request.wacc_step, request.wacc, request.wacc + request.wacc_step]
        tg_grid = [
            request.terminal_growth - request.terminal_growth_step,
            request.terminal_growth,
            request.terminal_growth + request.terminal_growth_step,
        ]
        matrix: list[list[ReverseDcfSensitivityCell]] = []
        for w_cell in wacc_grid:
            row_cells: list[ReverseDcfSensitivityCell] = []
            for g_cell in tg_grid:
                w_disp = _quantize(w_cell * Decimal(100), dp)
                g_disp = _quantize(g_cell * Decimal(100), dp)
                if w_cell <= Decimal(0):
                    row_cells.append(
                        ReverseDcfSensitivityCell(
                            wacc_pct=w_disp,
                            terminal_growth_pct=g_disp,
                            status="unavailable",
                            reason="WACC <= 0%.",
                        )
                    )
                elif w_cell <= g_cell:
                    row_cells.append(
                        ReverseDcfSensitivityCell(
                            wacc_pct=w_disp,
                            terminal_growth_pct=g_disp,
                            status="unavailable",
                            reason="WACC <= terminal growth rate.",
                        )
                    )
                else:
                    # Sensitivity matrix always evaluates WACC x terminal_growth under Gordon growth.
                    c_cell, _, _, _, em_cell, _ = _solve_reverse_dcf_cagr(
                        target_ev=ev_val,
                        fcf0=ttm_fcf_v,
                        years=request.forecast_years,
                        wacc=w_cell,
                        terminal_method="gordon_growth",
                        terminal_growth=g_cell,
                        terminal_multiple=None,
                    )
                    row_cells.append(
                        ReverseDcfSensitivityCell(
                            wacc_pct=w_disp,
                            terminal_growth_pct=g_disp,
                            status="computed",
                            implied_5y_fcf_cagr_pct=_quantize(c_cell * Decimal(100), dp),
                            implied_exit_fcf_multiple=_quantize(em_cell, dp),
                        )
                    )
            matrix.append(row_cells)

        with localcontext() as ctx:
            ctx.prec = 100
            tv_share = (pv_term / ev_val) * Decimal(100)

        reverse_dcf = ReverseDcfResult(
            status="computed",
            terminal_method=request.terminal_method,
            forecast_years=request.forecast_years,
            wacc_pct=wacc_pct_disp,
            terminal_growth_pct=tg_pct_disp,
            terminal_multiple_input=request.terminal_multiple,
            target_ev=_quantize(ev_val, dp),
            base_ttm_fcf=_quantize(ttm_fcf_v, dp),
            implied_5y_fcf_cagr_pct=_quantize(cagr * Decimal(100), dp),
            implied_year_n_fcf=_quantize(fcf_n, dp),
            pv_explicit_fcf=_quantize(pv_exp, dp),
            pv_terminal_value=_quantize(pv_term, dp),
            terminal_value_share_pct=_quantize(tv_share, dp),
            implied_exit_fcf_multiple=_quantize(exit_mult, dp),
            ev_residual_pct=_quantize(residual_pct, 8),
            wacc_axis_pct=[_quantize(w * Decimal(100), dp) for w in wacc_grid],
            terminal_growth_axis_pct=[_quantize(g * Decimal(100), dp) for g in tg_grid],
            sensitivity_matrix=matrix,
            formula=rdcf_formula,
            input_ids=rdcf_input_ids,
        )

    return ValuationAndExpectationsResult(
        entity_id=request.entity_id,
        symbol=request.symbol,
        monetary_unit=mon_unit,
        currency=resolved_curr,
        sector_applicability=sector,
        market_cap=mcap_m,
        enterprise_value=ev_m,
        pe_ttm=pe_ttm_m,
        pe_forward=pe_fwd_m,
        ev_to_sales=ev_sales_m,
        ev_to_ntm_sales=ev_ntm_sales_m,
        ev_to_ebitda=ev_ebitda_m,
        ev_to_ntm_ebitda=ev_ntm_ebitda_m,
        ev_to_ebit=ev_ebit_m,
        p_to_fcf=p_fcf_m,
        fcf_yield_pct=fcf_yield_m,
        fcf_ex_sbc=fcf_ex_sbc_m,
        ev_to_fcf_ex_sbc=ev_fcf_ex_sbc_m,
        fcf_ex_sbc_yield_pct=fcf_ex_sbc_yield_m,
        reverse_dcf=reverse_dcf,
    )
