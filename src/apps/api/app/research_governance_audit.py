"""V2.5 Management Incentives, Real Capital Allocation & Guidance Credibility Engine.

Architecture:
- Fact Store vs Rules Engine separation: Postgres stores ONLY objective projected facts
  (``PEER_COMPANY_SNAPSHOTS`` v3, ``DEF14A_FACT_SNAPSHOTS``, ``GUIDANCE_CREDIBILITY_LEDGER``).
  Layer 1 deterministic audits are computed in-memory (<5ms) against externalized
  ``v25_governance_rules.json`` (Pydantic ``extra='forbid'`` + precompiled regexes + ``rules_sha256``),
  so changing thresholds or anchor dictionaries requires ZERO SEC re-downloads, ZERO LLM re-runs,
  and ZERO DB migrations.
- Pillar 1 (0-LLM): 3-5Y SBC net dilution (arbitrary-ratio chained split adjustment directly on raw
  ``shares_diluted`` filing rows), buyback-to-SBC ratio, SBC tax withholding, unrecognized SBC overhang,
  and True Net Shareholder Yield with financial SIC gating and FPI graceful degradation.
- Pillar 2 (Layer 1 0-LLM ``*_htm.xml`` + Layer 2 on-demand CD&A): parses SEC Pay-vs-Performance iXBRL
  with ``<xbrldi:explicitMember>`` dimension resolution (multi-PEO safe), 5-tier incentive anchor grading,
  YoY anchor downgrade diff, Founder-CEO exemption, MNPI hard red flag; optional Layer 2 CD&A brief
  (<=20KB anchor-sliced text, built-in Skeptic counterview, dual numeric + verbatim quote gate).
- Pillar 3 (Dual-Track Guidance Credibility): Track A 0-LLM aggregation from Direction A locked
  ``EarningsDossier.guidance_evaluations``; Track B on-demand 1-shot 6-quarter ``8-K Item 2.02``
  ``Exhibit 99.1`` backfill with basis-locked (``gaap`` vs ``non_gaap``) 0-LLM scoring.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import json
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any, Literal, Mapping, Sequence
from uuid import uuid4
import xml.etree.ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field

from .fin_data.adapters.sec_edgar import STANDARD_LINES, Period, cik10, select_line
from .fin_data.adapters.sec_events.pacing import (
    SecPacingError,
    SecPacingLimiter,
    fetch_company_submissions,
    sec_get_bytes,
    sec_get_json,
    sec_get_text,
)
from .research_earnings_diagnostics import evaluate_sec_sic_gate
from .research_llm_primitives import (
    check_negation_context,
    find_quotation_span,
    invoke_and_parse_with_repair,
)
from .research_peer_compare import (
    PeerCompanySnapshot,
    load_or_fetch_company_snapshot,
    resolve_ticker_to_cik,
)
from .storage import JsonDocumentRepository

GovernanceLight = Literal["green", "yellow", "red", "not_applicable", "unavailable"]
AnchorTierKey = Literal[
    "capital_efficiency",
    "per_share_cash_gaap",
    "relative_tsr",
    "revenue_or_nongaap",
    "weak_adjusted",
    "unclassified",
]
GuidanceBasis = Literal["gaap", "non_gaap", "unknown"]
GuidanceVerdict = Literal["beat", "inline", "miss", "basis_mismatch_unscored", "pending_actual"]
CredibilityProfile = Literal[
    "chronic_sandbagger",
    "high_credibility",
    "mixed",
    "guidance_miss_prone",
    "insufficient_history",
]

_RULES_PATH = Path(__file__).resolve().parent / "fin_data" / "adapters" / "v25_governance_rules.json"
_MAX_DEF14A_XML_BYTES = 5_000_000
_MAX_DEF14A_HTML_BYTES = 8_000_000
_MAX_CDA_PROMPT_CHARS = 20_000
_MAX_8K_EX991_BYTES = 2_500_000
_MAX_8K_SLICE_CHARS = 3_500


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:14]}"


def _dec(val: Any) -> Decimal | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        d = Decimal(str(val).strip().replace(",", "").replace("%", "").replace("$", ""))
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _fmt_dec(val: Decimal | None, places: int = 2) -> str | None:
    if val is None or not val.is_finite():
        return None
    q = Decimal(10) ** (-places)
    return format(val.quantize(q, rounding=ROUND_HALF_UP), "f")


def _strip_html_to_text(raw_html: str) -> str:
    no_script = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw_html)
    no_tags = re.sub(r"(?s)<[^>]+>", " ", no_script)
    cleaned = (
        no_tags.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&#8217;", "'")
        .replace("&#8220;", '"')
        .replace("&#8221;", '"')
    )
    return re.sub(r"\s+", " ", cleaned).strip()


# --------------------------------------------------------------------------- Externalized Rules Engine

class _FrozenGovModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AnchorTierRule(_FrozenGovModel):
    rank: int
    label: str
    patterns: list[str] = Field(default_factory=list)


class CapitalAllocationRules(_FrozenGovModel):
    sbc_fcf_yellow_pct: float = 20.0
    sbc_fcf_red_pct: float = 40.0
    net_dilution_3y_yellow_pct: float = 2.0
    net_dilution_3y_red_pct: float = 5.0
    fake_buyback_min_buyback_usd: float = 100_000_000.0
    fake_buyback_net_dilution_floor_pct: float = 0.0
    ma_single_year_share_jump_pct: float = 10.0
    split_ratio_tolerance_pct: float = 2.0


class IncentiveGovernanceRules(_FrozenGovModel):
    founder_ceo_comp_exemption_usd: float = 2_000_000.0
    pay_tsr_divergence_cap_jump_pct: float = 30.0
    pay_tsr_divergence_tsr_drop_pct: float = -15.0
    anchor_tiers: dict[str, AnchorTierRule] = Field(default_factory=dict)


class GuidanceCredibilityRules(_FrozenGovModel):
    min_quarters_for_rating: int = 3
    inline_band_pct: float = 0.5
    sandbagger_min_beat_rate_pct: float = 75.0
    sandbagger_min_avg_surprise_pct: float = 3.0
    high_credibility_min_beat_or_inline_pct: float = 75.0
    miss_prone_min_miss_rate_pct: float = 40.0


class RadarAlertRules(_FrozenGovModel):
    net_dilution_yoy_worsen_pp: float = 1.5


class V25GovernanceRules(_FrozenGovModel):
    rules_version: str
    capital_allocation: CapitalAllocationRules
    incentive_governance: IncentiveGovernanceRules
    guidance_credibility: GuidanceCredibilityRules
    radar_alerts: RadarAlertRules


_COMPILED_RULES_CACHE: tuple[str, V25GovernanceRules, dict[str, list[re.Pattern[str]]]] | None = None


def load_v25_governance_rules(path: Path | None = None) -> tuple[V25GovernanceRules, str, dict[str, list[re.Pattern[str]]]]:
    """Load and compile externalized V2.5 rules; returns ``(rules, rules_sha256, compiled_anchor_regexes)``."""
    global _COMPILED_RULES_CACHE
    target = path or _RULES_PATH
    raw_bytes = target.read_bytes()
    digest = sha256(raw_bytes).hexdigest()[:16]
    if path is None and _COMPILED_RULES_CACHE is not None and _COMPILED_RULES_CACHE[0] == digest:
        return _COMPILED_RULES_CACHE[1], digest, _COMPILED_RULES_CACHE[2]
    rules = V25GovernanceRules.model_validate(json.loads(raw_bytes.decode("utf-8")))
    compiled: dict[str, list[re.Pattern[str]]] = {}
    for tier_key, tier_rule in rules.incentive_governance.anchor_tiers.items():
        compiled[tier_key] = [re.compile(pat, re.IGNORECASE) for pat in tier_rule.patterns]
    if path is None:
        _COMPILED_RULES_CACHE = (digest, rules, compiled)
    return rules, digest, compiled


# --------------------------------------------------------------------------- Pydantic Fact & Audit Contracts

class YearlyCapitalPoint(_FrozenGovModel):
    fiscal_year: int
    period_end: str
    diluted_shares_split_adjusted: str | None = None
    sbc_usd: str | None = None
    buybacks_usd: str | None = None
    dividends_usd: str | None = None
    fcf_usd: str | None = None
    sbc_pct_of_fcf: str | None = None


class CapitalAllocationAudit(_FrozenGovModel):
    quality_light: GovernanceLight = "green"
    is_financial_gated: bool = False
    currency: str = "USD"
    sbc_ttm_usd: str | None = None
    sbc_pct_of_revenue: str | None = None
    sbc_pct_of_fcf: str | None = None
    sbc_tax_withholding_usd: str | None = None
    unrecognized_sbc_usd: str | None = None
    unrecognized_sbc_pct_of_mcap: str | None = None
    buybacks_ttm_usd: str | None = None
    dividends_ttm_usd: str | None = None
    buyback_to_sbc_ratio: str | None = None
    net_diluted_share_change_1y_pct: str | None = None
    net_diluted_share_change_3y_pct: str | None = None
    net_diluted_share_change_5y_pct: str | None = None
    true_net_shareholder_yield_pct: str | None = None
    fake_buyback_flag: bool = False
    possible_ma_equity_issuance: bool = False
    split_adjustments_applied: list[str] = Field(default_factory=list)
    yearly_series: list[YearlyCapitalPoint] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PvpYearRow(_FrozenGovModel):
    fiscal_year: str
    peo_member: str = "default_peo"
    peo_total_comp_usd: str | None = None
    peo_cap_usd: str | None = None
    non_peo_avg_cap_usd: str | None = None
    company_tsr: str | None = None
    peer_tsr: str | None = None
    net_income_usd: str | None = None
    csm_value: str | None = None


class Def14aMeasureItem(_FrozenGovModel):
    raw_label: str
    cleaned_label: str
    is_company_selected_measure: bool = False


class Def14aFactSnapshot(_FrozenGovModel):
    """Pure objective SEC DEF 14A ``*_htm.xml`` fact snapshot (no derived judgments)."""

    cik: str
    symbol: str
    accession: str
    filed_at: str
    form: str = "DEF 14A"
    primary_doc_url: str | None = None
    xml_url: str | None = None
    disclosure_regime: Literal["us_pvp_ixbrl", "fpi_20f_limited", "pre_pvp_mandate_unavailable", "xml_unavailable"] = "us_pvp_ixbrl"
    company_selected_measure: str | None = None
    measures: list[Def14aMeasureItem] = Field(default_factory=list)
    pvp_rows: list[PvpYearRow] = Field(default_factory=list)
    insider_trading_policy_adopted: bool | None = None
    mnpi_timed_for_comp_flag: bool | None = None
    cached_at: datetime = Field(default_factory=_utcnow)


class ClassifiedAnchor(_FrozenGovModel):
    cleaned_label: str
    raw_label: str
    tier: AnchorTierKey
    tier_rank: int
    tier_label: str
    is_csm: bool = False


class IncentiveGovernanceAudit(_FrozenGovModel):
    quality_light: GovernanceLight = "green"
    disclosure_regime: str = "us_pvp_ixbrl"
    latest_accession: str | None = None
    latest_filed_at: str | None = None
    prior_accession: str | None = None
    company_selected_measure: str | None = None
    highest_anchor_tier: AnchorTierKey = "unclassified"
    highest_anchor_label: str = "未分类"
    classified_anchors: list[ClassifiedAnchor] = Field(default_factory=list)
    anchor_downgraded_yoy: bool = False
    removed_anchors_yoy: list[str] = Field(default_factory=list)
    added_anchors_yoy: list[str] = Field(default_factory=list)
    founder_ceo_exempt: bool = False
    pay_tsr_divergent: bool = False
    mnpi_red_flag: bool = False
    insider_trading_policy_adopted: bool | None = None
    latest_peo_total_comp_usd: str | None = None
    latest_peo_cap_usd: str | None = None
    pvp_rows: list[PvpYearRow] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class GuidanceCredibilityRecord(_FrozenGovModel):
    record_id: str = Field(default_factory=lambda: _new_id("gcr"))
    period_label: str
    metric_key: str
    basis: GuidanceBasis = "unknown"
    guidance_low: str | None = None
    guidance_mid: str | None = None
    guidance_high: str | None = None
    actual_value: str | None = None
    actual_basis: GuidanceBasis = "unknown"
    source_kind: Literal["direction_a_dossier", "8k_backfill"] = "direction_a_dossier"
    source_accession: str | None = None
    disclosed_at: str | None = None
    verbatim_quote: str | None = None
    quote_verified: bool = False


class GuidanceCredibilityLedger(_FrozenGovModel):
    """Per-CIK objective guidance history ledger (one document per CIK; never scanned via .items())."""

    cik: str
    symbol: str
    records: list[GuidanceCredibilityRecord] = Field(default_factory=list)
    backfilled_accessions: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=_utcnow)


class ScoredGuidanceRow(_FrozenGovModel):
    period_label: str
    metric_key: str
    basis: GuidanceBasis
    guidance_mid: str | None = None
    actual_value: str | None = None
    surprise_pct: str | None = None
    verdict: GuidanceVerdict
    source_kind: str
    source_accession: str | None = None
    disclosed_at: str | None = None


class GuidanceCredibilityAudit(_FrozenGovModel):
    quality_light: GovernanceLight = "unavailable"
    profile: CredibilityProfile = "insufficient_history"
    scored_quarters: int = 0
    total_records: int = 0
    beat_count: int = 0
    inline_count: int = 0
    miss_count: int = 0
    beat_rate_pct: str | None = None
    inline_rate_pct: str | None = None
    miss_rate_pct: str | None = None
    avg_surprise_vs_mid_pct: str | None = None
    rows: list[ScoredGuidanceRow] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class GovernanceAuditReport(_FrozenGovModel):
    """Complete Layer 1 (0-LLM) V2.5 audit report computed live in-memory from fact stores + external rules."""

    audit_id: str = Field(default_factory=lambda: _new_id("gov"))
    cik: str
    symbol: str
    company_name: str | None = None
    as_of: str | None = None
    rules_version: str
    rules_sha256: str
    overall_light: GovernanceLight = "green"
    headline_anchor_summary: str = ""
    capital_allocation: CapitalAllocationAudit
    incentive_governance: IncentiveGovernanceAudit
    guidance_credibility: GuidanceCredibilityAudit
    stale_projection: bool = False
    elapsed_ms: int = 0
    generated_at: datetime = Field(default_factory=_utcnow)


class GovernanceCitedMetric(_FrozenGovModel):
    metric_key: str
    value: str
    verified: bool = False


class GovernanceCitedQuote(_FrozenGovModel):
    quotation: str
    verified: bool = False
    negation_warning: str | None = None


class _LlmCdaBriefPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=1200)
    incentive_alignment_assessment: str = Field(min_length=1, max_length=1800)
    capital_allocation_verdict: str = Field(min_length=1, max_length=1400)
    skeptic_counterview: str = Field(min_length=1, max_length=1200)
    cited_metrics: list[GovernanceCitedMetric] = Field(default_factory=list)
    cited_quotes: list[GovernanceCitedQuote] = Field(default_factory=list)


class CdaAttributionBrief(_FrozenGovModel):
    """Layer 2 on-demand CD&A deep attribution with built-in Skeptic + dual server verification."""

    cik: str
    symbol: str
    def14a_accession: str
    rules_sha256: str
    prompt_version: str = "v2.5.1"
    summary: str
    incentive_alignment_assessment: str
    capital_allocation_verdict: str
    skeptic_counterview: str
    cited_metrics: list[GovernanceCitedMetric] = Field(default_factory=list)
    cited_quotes: list[GovernanceCitedQuote] = Field(default_factory=list)
    verified_metric_count: int = 0
    total_cited_metric_count: int = 0
    verified_quote_count: int = 0
    total_cited_quote_count: int = 0
    open_gaps: list[str] = Field(default_factory=list)
    tokens_used: int = 0
    model_used: str | None = None
    generated_at: datetime = Field(default_factory=_utcnow)


# --------------------------------------------------------------------------- Pillar 1: 3-5Y Split-Adjusted Capital Allocation

def _rebuild_facts_dict(snap: PeerCompanySnapshot) -> dict[str, Any]:
    tax_bucket: dict[str, Any] = {}
    for _line_name, tag_map in snap.facts_by_line.items():
        for tag, unit_map in tag_map.items():
            tax_bucket[tag] = {"units": unit_map}
    return {snap.taxonomy: tax_bucket}


def _chain_split_adjusted_annual_shares(
    snap: PeerCompanySnapshot,
    *,
    as_of: date | None,
    split_tol_pct: Decimal,
) -> tuple[list[tuple[int, str, Decimal]], list[str]]:
    """Build split-adjusted annual diluted shares directly from raw filing rows (handles any ratio e.g. 10:1, 3:2)."""
    line_bucket = snap.facts_by_line.get("shares_diluted") or snap.facts_by_line.get("shares_basic") or {}
    # Collect annual rows grouped by filing (filed, accn) -> {end_date: val}
    by_filing: dict[tuple[str, str], dict[str, Decimal]] = {}
    for _tag, unit_map in line_bucket.items():
        rows = unit_map.get("shares") or []
        for r in rows:
            if not isinstance(r, Mapping):
                continue
            st_s = r.get("start")
            en_s = r.get("end")
            if not st_s or not en_s:
                continue
            try:
                st_d = date.fromisoformat(str(st_s)[:10])
                en_d = date.fromisoformat(str(en_s)[:10])
            except ValueError:
                continue
            days = (en_d - st_d).days
            if not (330 <= days <= 390):
                continue
            f_s = str(r.get("filed") or "")[:10]
            if as_of is not None:
                if f_s:
                    try:
                        if date.fromisoformat(f_s) > as_of:
                            continue
                    except ValueError:
                        pass
                elif en_d > as_of:
                    continue
            val = _dec(r.get("val"))
            if val is None or val <= 0:
                continue
            accn = str(r.get("accn") or f_s or en_s)
            fk = (f_s, accn)
            by_filing.setdefault(fk, {})
            if str(en_s)[:10] not in by_filing[fk]:
                by_filing[fk][str(en_s)[:10]] = val

    if not by_filing:
        return [], []

    # Sort newest filing first; walk backwards chaining cumulative split multiplier via overlapping ends
    ordered_filings = sorted(by_filing.items(), key=lambda x: x[0], reverse=True)
    adjusted_by_end: dict[str, Decimal] = {}
    split_notes: list[str] = []
    tol = split_tol_pct / Decimal(100)

    for (f_s, _accn), end_map in ordered_filings:
        if not adjusted_by_end:
            for en_k, v in end_map.items():
                adjusted_by_end[en_k] = v
            continue
        # Find overlapping fiscal year end to detect split factor
        overlap_ratios: list[Decimal] = []
        for en_k, old_v in end_map.items():
            if en_k in adjusted_by_end and old_v > 0:
                overlap_ratios.append(adjusted_by_end[en_k] / old_v)
        factor = Decimal(1)
        if overlap_ratios:
            # Use most recent overlap ratio
            cand = overlap_ratios[0]
            if abs(cand - Decimal(1)) > tol:
                factor = cand
                split_notes.append(f"Applied chained split factor {factor:.4f}x from filing {f_s or 'older'}.")
        for en_k, old_v in end_map.items():
            if en_k not in adjusted_by_end:
                adjusted_by_end[en_k] = old_v * factor

    # Sort newest -> oldest, take up to 6 annual points
    points: list[tuple[int, str, Decimal]] = []
    for en_k, v in sorted(adjusted_by_end.items(), key=lambda x: x[0], reverse=True)[:6]:
        try:
            fy = int(en_k[:4])
        except ValueError:
            continue
        points.append((fy, en_k, v))
    return points, split_notes


def compute_capital_allocation_audit(
    snap: PeerCompanySnapshot,
    *,
    rules: V25GovernanceRules,
    price_usd: Decimal | None = None,
    as_of: date | None = None,
    fx_to_usd: Decimal | None = None,
) -> CapitalAllocationAudit:
    """Pure 0-LLM 3-5Y SBC net dilution & capital allocation audit."""
    sic_gate = evaluate_sec_sic_gate(snap.sic, snap.sic_description)
    facts_dict = _rebuild_facts_dict(snap)
    tax = snap.taxonomy
    ca_rules = rules.capital_allocation
    notes: list[str] = []

    def _sel(line_name: str, kind: str, n: int = 8) -> list[Period]:
        spec = STANDARD_LINES.get(line_name)
        if spec is None:
            return []
        return select_line(facts_dict, spec, kind, n, derive_q4=True, taxonomy=tax, as_of=as_of)

    rev_y = _sel("revenue", "year", 6)
    ocf_y = _sel("operating_cash_flow", "year", 6)
    capex_y = _sel("capex", "year", 6)
    sbc_y = _sel("stock_based_compensation", "year", 6)
    buy_y = _sel("share_repurchases", "year", 6)
    div_y = _sel("dividends_paid", "year", 6)
    tax_wh_y = _sel("sbc_tax_withholding", "year", 4)
    unrec_bs = _sel("unrecognized_sbc", "quarter", 2) or _sel("unrecognized_sbc", "year", 2)

    is_non_usd = snap.currency.upper() != "USD"
    fx = Decimal(1) if not is_non_usd else fx_to_usd

    def _to_usd(v: Decimal | None) -> Decimal | None:
        return (v * fx) if (v is not None and fx is not None) else None

    # Split-adjusted annual share series (newest -> oldest)
    share_pts, split_notes = _chain_split_adjusted_annual_shares(
        snap, as_of=as_of, split_tol_pct=Decimal(str(ca_rules.split_ratio_tolerance_pct))
    )

    # Check for single-year >10% jump (possible M&A equity issuance)
    possible_ma = False
    ma_thresh = Decimal(str(ca_rules.ma_single_year_share_jump_pct))
    for i in range(len(share_pts) - 1):
        cur_sh = share_pts[i][2]
        prev_sh = share_pts[i + 1][2]
        if prev_sh > 0:
            yoy_sh = ((cur_sh - prev_sh) / prev_sh) * Decimal(100)
            if yoy_sh > ma_thresh:
                possible_ma = True
                notes.append(
                    f"possible_ma_equity_issuance: FY{share_pts[i][0]} split-adjusted shares jumped {yoy_sh:+.1f}% YoY."
                )

    def _sh_change(years_back: int) -> Decimal | None:
        if len(share_pts) <= years_back:
            return None
        cur_sh = share_pts[0][2]
        old_sh = share_pts[years_back][2]
        if old_sh <= 0:
            return None
        return ((cur_sh - old_sh) / old_sh) * Decimal(100)

    sh_1y = _sh_change(1)
    sh_3y = _sh_change(3)
    sh_5y = _sh_change(5)

    # Map annual flows by year end for multi-year table
    by_end_ocf = {p.end.isoformat(): p.value for p in ocf_y}
    by_end_cap = {p.end.isoformat(): abs(p.value) for p in capex_y}
    by_end_sbc = {p.end.isoformat(): abs(p.value) for p in sbc_y}
    by_end_buy = {p.end.isoformat(): abs(p.value) for p in buy_y}
    by_end_div = {p.end.isoformat(): abs(p.value) for p in div_y}
    by_end_sh = {en: v for _fy, en, v in share_pts}

    all_ends = sorted(
        set(by_end_sbc) | set(by_end_buy) | set(by_end_ocf) | set(by_end_sh),
        reverse=True,
    )[:5]
    yearly_series: list[YearlyCapitalPoint] = []
    for en in all_ends:
        ocf_v = by_end_ocf.get(en)
        cap_v = by_end_cap.get(en)
        fcf_v = (ocf_v - cap_v) if (ocf_v is not None and cap_v is not None) else None
        sbc_v = by_end_sbc.get(en)
        sbc_fcf_y = (
            (sbc_v / fcf_v * Decimal(100))
            if (not sic_gate.is_gated and sbc_v is not None and fcf_v is not None and fcf_v > 0)
            else None
        )
        yearly_series.append(
            YearlyCapitalPoint(
                fiscal_year=int(en[:4]),
                period_end=en,
                diluted_shares_split_adjusted=_fmt_dec(by_end_sh.get(en), 0),
                sbc_usd=_fmt_dec(_to_usd(sbc_v), 0),
                buybacks_usd=_fmt_dec(_to_usd(by_end_buy.get(en)), 0),
                dividends_usd=_fmt_dec(_to_usd(by_end_div.get(en)), 0),
                fcf_usd=_fmt_dec(_to_usd(fcf_v), 0) if not sic_gate.is_gated else None,
                sbc_pct_of_fcf=_fmt_dec(sbc_fcf_y, 1),
            )
        )

    # Latest annual (or TTM) headline metrics
    latest_rev = rev_y[0].value if rev_y else None
    latest_sbc = abs(sbc_y[0].value) if sbc_y else None
    latest_buy = abs(buy_y[0].value) if buy_y else None
    latest_div = abs(div_y[0].value) if div_y else None
    latest_tax_wh = abs(tax_wh_y[0].value) if tax_wh_y else None
    latest_unrec = abs(unrec_bs[0].value) if unrec_bs else None
    latest_fcf = (
        (ocf_y[0].value - abs(capex_y[0].value))
        if (ocf_y and capex_y and ocf_y[0].end == capex_y[0].end)
        else None
    )

    sbc_pct_rev = (latest_sbc / latest_rev * Decimal(100)) if (latest_sbc is not None and latest_rev and latest_rev > 0) else None
    sbc_pct_fcf = (
        (latest_sbc / latest_fcf * Decimal(100))
        if (not sic_gate.is_gated and latest_sbc is not None and latest_fcf is not None and latest_fcf > 0)
        else None
    )
    buy_to_sbc = (latest_buy / latest_sbc) if (latest_buy is not None and latest_sbc is not None and latest_sbc > 0) else None

    # Market-cap anchored metrics (only when USD price & shares available and currency verified)
    mcap_usd: Decimal | None = None
    if not is_non_usd and price_usd is not None and price_usd > 0 and share_pts:
        mcap_usd = price_usd * share_pts[0][2]

    true_yield: Decimal | None = None
    unrec_pct_mcap: Decimal | None = None
    if mcap_usd is not None and mcap_usd > 0:
        net_return_usd = (_to_usd(latest_buy) or Decimal(0)) + (_to_usd(latest_div) or Decimal(0)) - (_to_usd(latest_sbc) or Decimal(0))
        if latest_buy is not None or latest_div is not None or latest_sbc is not None:
            true_yield = (net_return_usd / mcap_usd) * Decimal(100)
        if latest_unrec is not None:
            unrec_pct_mcap = (_to_usd(latest_unrec) / mcap_usd) * Decimal(100)  # type: ignore[operator]

    # Fake buyback detection: spent >= threshold on buybacks yet 3Y net diluted shares still grew > floor
    buy_usd = _to_usd(latest_buy)
    fake_buyback = bool(
        buy_usd is not None
        and buy_usd >= Decimal(str(ca_rules.fake_buyback_min_buyback_usd))
        and sh_3y is not None
        and sh_3y > Decimal(str(ca_rules.fake_buyback_net_dilution_floor_pct))
        and not possible_ma
    )
    if fake_buyback:
        notes.append(f"fake_buyback_detected: spent ${buy_usd:,.0f} on buybacks yet 3Y diluted shares grew {sh_3y:+.2f}%.")

    # Quality light
    red = 0
    yellow = 0
    if fake_buyback:
        red += 1
    if sbc_pct_fcf is not None:
        if sbc_pct_fcf >= Decimal(str(ca_rules.sbc_fcf_red_pct)):
            red += 1
            notes.append(f"SBC consumes {sbc_pct_fcf:.1f}% of FCF (>= {ca_rules.sbc_fcf_red_pct:.0f}%).")
        elif sbc_pct_fcf >= Decimal(str(ca_rules.sbc_fcf_yellow_pct)):
            yellow += 1
            notes.append(f"SBC consumes {sbc_pct_fcf:.1f}% of FCF (>= {ca_rules.sbc_fcf_yellow_pct:.0f}%).")
    if sh_3y is not None and not possible_ma:
        if sh_3y >= Decimal(str(ca_rules.net_dilution_3y_red_pct)):
            red += 1
            notes.append(f"3Y net diluted share count expanded {sh_3y:+.2f}% (>= {ca_rules.net_dilution_3y_red_pct:.1f}%).")
        elif sh_3y >= Decimal(str(ca_rules.net_dilution_3y_yellow_pct)):
            yellow += 1
            notes.append(f"3Y net diluted share count expanded {sh_3y:+.2f}% (>= {ca_rules.net_dilution_3y_yellow_pct:.1f}%).")

    if sic_gate.is_gated:
        notes.append(f"Financial sector ({sic_gate.sector_category}): SBC/FCF hidden; share dilution & buybacks preserved.")

    light: GovernanceLight = "red" if red > 0 else ("yellow" if yellow > 0 else "green")
    return CapitalAllocationAudit(
        quality_light=light,
        is_financial_gated=sic_gate.is_gated,
        currency=snap.currency,
        sbc_ttm_usd=_fmt_dec(_to_usd(latest_sbc), 0),
        sbc_pct_of_revenue=_fmt_dec(sbc_pct_rev, 2),
        sbc_pct_of_fcf=_fmt_dec(sbc_pct_fcf, 1),
        sbc_tax_withholding_usd=_fmt_dec(_to_usd(latest_tax_wh), 0),
        unrecognized_sbc_usd=_fmt_dec(_to_usd(latest_unrec), 0),
        unrecognized_sbc_pct_of_mcap=_fmt_dec(unrec_pct_mcap, 2),
        buybacks_ttm_usd=_fmt_dec(buy_usd, 0),
        dividends_ttm_usd=_fmt_dec(_to_usd(latest_div), 0),
        buyback_to_sbc_ratio=_fmt_dec(buy_to_sbc, 2),
        net_diluted_share_change_1y_pct=_fmt_dec(sh_1y, 2),
        net_diluted_share_change_3y_pct=_fmt_dec(sh_3y, 2),
        net_diluted_share_change_5y_pct=_fmt_dec(sh_5y, 2),
        true_net_shareholder_yield_pct=_fmt_dec(true_yield, 2),
        fake_buyback_flag=fake_buyback,
        possible_ma_equity_issuance=possible_ma,
        split_adjustments_applied=split_notes,
        yearly_series=yearly_series,
        notes=notes,
    )


# --------------------------------------------------------------------------- Pillar 2: DEF 14A *_htm.xml Parser & Rules

def _clean_measure_label(raw: str) -> str:
    s = re.sub(r"<[^>]+>", " ", raw)
    s = re.sub(r"[\u2022\u2023\u25e6\u2043\u2219•·▪■●◆▶►*]+", " ", s)
    s = s.strip().strip('"\'“”‘’()[].,;:- ')
    return re.sub(r"\s+", " ", s).strip()


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse_def14a_ixbrl_xml(
    xml_bytes: bytes,
    *,
    cik: str,
    symbol: str,
    accession: str,
    filed_at: str,
    form: str = "DEF 14A",
    primary_doc_url: str | None = None,
    xml_url: str | None = None,
) -> Def14aFactSnapshot:
    """Securely parse SEC DEF 14A ``*_htm.xml`` with ``<xbrldi:explicitMember>`` dimension resolution."""
    if len(xml_bytes) > _MAX_DEF14A_XML_BYTES:
        raise ValueError(f"DEF 14A XML exceeds {_MAX_DEF14A_XML_BYTES} bytes cap.")
    head_probe = xml_bytes[:4096].upper()
    if b"<!DOCTYPE" in head_probe or b"<!ENTITY" in head_probe:
        raise ValueError("Rejected XML containing DOCTYPE/ENTITY declaration (XXE guard).")

    root = ET.fromstring(xml_bytes)

    # 1. Build context_id -> (period_end_year, dimensions_dict)
    contexts: dict[str, tuple[str, dict[str, str]]] = {}
    for elem in root.iter():
        if _local_tag(elem.tag) != "context":
            continue
        cid = elem.attrib.get("id")
        if not cid:
            continue
        period_end = ""
        dims: dict[str, str] = {}
        for sub in elem.iter():
            lt = _local_tag(sub.tag)
            if lt in ("endDate", "instant") and sub.text:
                period_end = sub.text.strip()[:4]
            elif lt == "explicitMember" and sub.text:
                axis = sub.attrib.get("dimension", "axis")
                dims[axis.rsplit(":", 1)[-1]] = sub.text.strip().rsplit(":", 1)[-1]
        contexts[cid] = (period_end, dims)

    # 2. Extract ecd:* facts grouped by (fiscal_year, peo_member)
    pvp_buckets: dict[tuple[str, str], dict[str, str]] = {}
    measures_seen: dict[str, Def14aMeasureItem] = {}
    csm_name: str | None = None
    insider_policy: bool | None = None
    mnpi_flag: bool | None = None

    for elem in root.iter():
        lt = _local_tag(elem.tag)
        txt = (elem.text or "").strip()
        if not txt:
            continue
        cid = elem.attrib.get("contextRef", "")
        fy, dims = contexts.get(cid, ("", {}))

        if lt == "CoSelectedMeasureName":
            cleaned = _clean_measure_label(txt)
            if cleaned:
                csm_name = cleaned
                measures_seen[cleaned.lower()] = Def14aMeasureItem(
                    raw_label=txt[:200], cleaned_label=cleaned[:160], is_company_selected_measure=True
                )
        elif lt == "MeasureName":
            cleaned = _clean_measure_label(txt)
            if cleaned and cleaned.lower() not in measures_seen:
                measures_seen[cleaned.lower()] = Def14aMeasureItem(
                    raw_label=txt[:200], cleaned_label=cleaned[:160], is_company_selected_measure=False
                )
        elif lt == "InsiderTrdPoliciesProcAdoptedFlag":
            insider_policy = txt.lower() in ("true", "1", "yes")
        elif lt == "MnpiDiscTimedForCompValFlag":
            mnpi_flag = txt.lower() in ("true", "1", "yes")
        elif lt in (
            "PeoTotalCompAmt",
            "PeoActuallyPaidCompAmt",
            "NonPeoNeoAvgCompActuallyPaidAmt",
            "TotalShareholderRtnAmt",
            "PeerGroupTotalShareholderRtnAmt",
            "NetIncomeLoss",
            "CoSelectedMeasureAmt",
        ):
            if not fy:
                continue
            peo_mem = dims.get("IndividualAxis") or dims.get("ExecutiveCategoryAxis") or "default_peo"
            bucket = pvp_buckets.setdefault((fy, peo_mem), {})
            val = _dec(txt)
            if val is not None:
                bucket[lt] = format(val, "f")

    pvp_rows: list[PvpYearRow] = []
    for (fy, peo_mem), b in sorted(pvp_buckets.items(), key=lambda x: x[0], reverse=True):
        pvp_rows.append(
            PvpYearRow(
                fiscal_year=fy,
                peo_member=peo_mem,
                peo_total_comp_usd=b.get("PeoTotalCompAmt"),
                peo_cap_usd=b.get("PeoActuallyPaidCompAmt"),
                non_peo_avg_cap_usd=b.get("NonPeoNeoAvgCompActuallyPaidAmt"),
                company_tsr=b.get("TotalShareholderRtnAmt"),
                peer_tsr=b.get("PeerGroupTotalShareholderRtnAmt"),
                net_income_usd=b.get("NetIncomeLoss"),
                csm_value=b.get("CoSelectedMeasureAmt"),
            )
        )

    return Def14aFactSnapshot(
        cik=cik10(cik),
        symbol=symbol.upper(),
        accession=accession,
        filed_at=filed_at,
        form=form,
        primary_doc_url=primary_doc_url,
        xml_url=xml_url,
        disclosure_regime="us_pvp_ixbrl",
        company_selected_measure=csm_name,
        measures=list(measures_seen.values()),
        pvp_rows=pvp_rows,
        insider_trading_policy_adopted=insider_policy,
        mnpi_timed_for_comp_flag=mnpi_flag,
    )


def load_or_fetch_def14a_snapshots(
    *,
    cik: str,
    symbol: str,
    user_agent: str | None,
    limiter: SecPacingLimiter | None,
    def14a_cache: JsonDocumentRepository[Any] | None = None,
    submissions: Any = None,
    as_of: date | None = None,
    cache_only: bool = False,
    policy: Any = None,
    transport: Any = None,
    stream_transport: Any = None,
) -> tuple[Def14aFactSnapshot | None, Def14aFactSnapshot | None]:
    """Load latest 2 DEF 14A objective fact snapshots (latest, prior) via ``*_htm.xml``."""
    cik_norm = cik10(cik)
    sym_u = symbol.strip().upper()

    if as_of is not None and as_of < date(2023, 1, 1):
        return (
            Def14aFactSnapshot(
                cik=cik_norm,
                symbol=sym_u,
                accession="pre_pvp_mandate",
                filed_at=as_of.isoformat(),
                disclosure_regime="pre_pvp_mandate_unavailable",
            ),
            None,
        )

    if cache_only and def14a_cache is not None:
        raw = def14a_cache.get(f"def14a:{cik_norm}")
        if isinstance(raw, Mapping):
            snaps = [Def14aFactSnapshot.model_validate(x) for x in (raw.get("snapshots") or [])]
            if as_of is not None:
                snaps = [s for s in snaps if s.filed_at <= as_of.isoformat()]
            return (snaps[0] if len(snaps) >= 1 else None, snaps[1] if len(snaps) >= 2 else None)
        return None, None

    subs = submissions if submissions is not None else fetch_company_submissions(
        cik_norm,
        user_agent=user_agent,
        max_pages=0,
        limiter=limiter,
        policy=policy,
        transport=transport,
        stream_transport=stream_transport,
        use_ttl_cache=True,
    )

    if getattr(subs, "is_fpi", False) and not any(r.form.upper() in ("DEF 14A", "DEF 14A/A") for r in subs.rows):
        fpi_snap = Def14aFactSnapshot(
            cik=cik_norm,
            symbol=sym_u,
            accession="fpi_exempt",
            filed_at=(as_of or _utcnow().date()).isoformat(),
            form="20-F",
            disclosure_regime="fpi_20f_limited",
        )
        return fpi_snap, None

    def_rows = [
        r for r in subs.rows
        if r.form.upper() in ("DEF 14A", "DEF 14A/A")
        and (as_of is None or r.filed_at <= as_of)
    ][:2]
    if not def_rows:
        return None, None

    cached_by_acc: dict[str, Def14aFactSnapshot] = {}
    if def14a_cache is not None:
        raw = def14a_cache.get(f"def14a:{cik_norm}")
        if isinstance(raw, Mapping):
            for x in raw.get("snapshots") or []:
                try:
                    s = Def14aFactSnapshot.model_validate(x)
                    cached_by_acc[s.accession] = s
                except Exception:
                    continue

    out_snaps: list[Def14aFactSnapshot] = []
    cik_int = str(int(cik_norm))
    changed = False
    for row in def_rows:
        if row.accession in cached_by_acc:
            out_snaps.append(cached_by_acc[row.accession])
            continue
        acc_nodash = row.accession.replace("-", "")
        base_dir = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}"
        primary_url = f"{base_dir}/{row.primary_document}" if row.primary_document else None
        xml_url: str | None = None
        try:
            idx_doc, _ = sec_get_json(
                f"{base_dir}/index.json",
                category="filings",
                user_agent=user_agent,
                limiter=limiter,
                policy=policy,
                transport=transport,
                stream_transport=stream_transport,
            )
            items = ((idx_doc or {}).get("directory") or {}).get("item") or []
            for it in items:
                name = str(it.get("name") or "")
                if name.lower().endswith("_htm.xml"):
                    xml_url = f"{base_dir}/{name}"
                    break
        except Exception:
            xml_url = None

        if xml_url:
            try:
                xml_bytes, _sha, _ct = sec_get_bytes(
                    xml_url,
                    category="filings",
                    user_agent=user_agent,
                    limiter=limiter,
                    policy=policy,
                    transport=transport,
                    stream_transport=stream_transport,
                    max_bytes=_MAX_DEF14A_XML_BYTES,
                )
                snap = parse_def14a_ixbrl_xml(
                    xml_bytes,
                    cik=cik_norm,
                    symbol=sym_u,
                    accession=row.accession,
                    filed_at=row.filed_at.isoformat(),
                    form=row.form,
                    primary_doc_url=primary_url,
                    xml_url=xml_url,
                )
                out_snaps.append(snap)
                cached_by_acc[snap.accession] = snap
                changed = True
                continue
            except Exception:
                pass

        fallback_snap = Def14aFactSnapshot(
            cik=cik_norm,
            symbol=sym_u,
            accession=row.accession,
            filed_at=row.filed_at.isoformat(),
            form=row.form,
            primary_doc_url=primary_url,
            disclosure_regime="xml_unavailable",
        )
        out_snaps.append(fallback_snap)
        cached_by_acc[fallback_snap.accession] = fallback_snap
        changed = True

    if changed and def14a_cache is not None:
        all_cached = sorted(cached_by_acc.values(), key=lambda s: s.filed_at, reverse=True)[:4]
        def14a_cache.put(
            f"def14a:{cik_norm}",
            {"cik": cik_norm, "symbol": sym_u, "snapshots": [s.model_dump(mode="json") for s in all_cached]},
        )

    return (out_snaps[0] if len(out_snaps) >= 1 else None, out_snaps[1] if len(out_snaps) >= 2 else None)


def _classify_anchor_label(
    label: str,
    rules: V25GovernanceRules,
    compiled: Mapping[str, list[re.Pattern[str]]],
) -> tuple[AnchorTierKey, int, str]:
    tiers = rules.incentive_governance.anchor_tiers
    # Match in descending rank order (capital_efficiency -> weak_adjusted)
    ordered = sorted(tiers.items(), key=lambda kv: kv[1].rank, reverse=True)
    for key, rule in ordered:
        for pat in compiled.get(key, ()):
            if pat.search(label):
                return key, rule.rank, rule.label  # type: ignore[return-value]
    return "unclassified", 0, "未分类指标"


def evaluate_incentive_governance(
    latest: Def14aFactSnapshot | None,
    prior: Def14aFactSnapshot | None,
    *,
    rules: V25GovernanceRules,
    compiled_anchors: Mapping[str, list[re.Pattern[str]]],
) -> IncentiveGovernanceAudit:
    """Pure in-memory (<1ms) evaluation of DEF 14A incentive anchors & governance flags."""
    if latest is None:
        return IncentiveGovernanceAudit(
            quality_light="unavailable",
            disclosure_regime="xml_unavailable",
            notes=["No DEF 14A proxy statement found for this issuer."],
        )
    if latest.disclosure_regime in ("fpi_20f_limited", "pre_pvp_mandate_unavailable", "xml_unavailable"):
        msg = {
            "fpi_20f_limited": "Foreign Private Issuer (20-F): exempt from US DEF 14A Pay-vs-Performance disclosure.",
            "pre_pvp_mandate_unavailable": "Historical as_of predates SEC Pay-vs-Performance iXBRL mandate (2023).",
            "xml_unavailable": "DEF 14A found but structured *_htm.xml iXBRL not available.",
        }[latest.disclosure_regime]
        return IncentiveGovernanceAudit(
            quality_light="unavailable",
            disclosure_regime=latest.disclosure_regime,
            latest_accession=latest.accession,
            latest_filed_at=latest.filed_at,
            notes=[msg],
        )

    notes: list[str] = []
    classified: list[ClassifiedAnchor] = []
    for m in latest.measures:
        t_key, t_rank, t_label = _classify_anchor_label(m.cleaned_label, rules, compiled_anchors)
        classified.append(
            ClassifiedAnchor(
                cleaned_label=m.cleaned_label,
                raw_label=m.raw_label,
                tier=t_key,
                tier_rank=t_rank,
                tier_label=t_label,
                is_csm=m.is_company_selected_measure,
            )
        )
    classified.sort(key=lambda a: (a.is_csm, a.tier_rank), reverse=True)
    highest = max(classified, key=lambda a: a.tier_rank) if classified else None
    highest_tier: AnchorTierKey = highest.tier if highest else "unclassified"
    highest_label = highest.tier_label if highest else "未分类"

    # YoY anchor diff vs prior DEF 14A
    anchor_downgraded = False
    removed_yoy: list[str] = []
    added_yoy: list[str] = []
    if prior is not None and prior.disclosure_regime == "us_pvp_ixbrl" and prior.measures:
        prev_set = {m.cleaned_label.lower(): m.cleaned_label for m in prior.measures}
        curr_set = {m.cleaned_label.lower(): m.cleaned_label for m in latest.measures}
        removed_yoy = [v for k, v in prev_set.items() if k not in curr_set]
        added_yoy = [v for k, v in curr_set.items() if k not in prev_set]
        prev_max_rank = max(
            (_classify_anchor_label(m.cleaned_label, rules, compiled_anchors)[1] for m in prior.measures),
            default=0,
        )
        curr_max_rank = highest.tier_rank if highest else 0
        if prev_max_rank >= 4 and curr_max_rank < prev_max_rank:
            anchor_downgraded = True
            notes.append(
                f"INCENTIVE_ANCHOR_DOWNGRADED: highest incentive tier dropped from rank {prev_max_rank} to {curr_max_rank}."
            )

    # Founder-CEO exemption & Pay-TSR divergence
    ig_rules = rules.incentive_governance
    latest_pvp = latest.pvp_rows[0] if latest.pvp_rows else None
    peo_total = _dec(latest_pvp.peo_total_comp_usd) if latest_pvp else None
    peo_cap = _dec(latest_pvp.peo_cap_usd) if latest_pvp else None
    founder_exempt = bool(
        peo_total is not None
        and peo_total < Decimal(str(ig_rules.founder_ceo_comp_exemption_usd))
        and (peo_cap is None or abs(peo_cap) < Decimal(str(ig_rules.founder_ceo_comp_exemption_usd)) * Decimal("2"))
    )
    if founder_exempt:
        notes.append("Founder-CEO low-compensation profile detected; Pay-TSR divergence alert exempted.")

    pay_tsr_div = False
    if not founder_exempt and len(latest.pvp_rows) >= 2:
        # Compare latest 2 years for same PEO member
        same_peo = [r for r in latest.pvp_rows if r.peo_member == latest.pvp_rows[0].peo_member]
        if len(same_peo) >= 2:
            c0, c1 = _dec(same_peo[0].peo_cap_usd), _dec(same_peo[1].peo_cap_usd)
            t0, t1 = _dec(same_peo[0].company_tsr), _dec(same_peo[1].company_tsr)
            if c0 is not None and c1 is not None and c1 > 0 and t0 is not None and t1 is not None and t1 > 0:
                cap_jump = (c0 - c1) / c1 * Decimal(100)
                tsr_chg = (t0 - t1) / t1 * Decimal(100)
                if cap_jump >= Decimal(str(ig_rules.pay_tsr_divergence_cap_jump_pct)) and tsr_chg <= Decimal(
                    str(ig_rules.pay_tsr_divergence_tsr_drop_pct)
                ):
                    pay_tsr_div = True
                    notes.append(f"Pay-TSR divergence: PEO CAP jumped {cap_jump:+.1f}% while TSR fell {tsr_chg:+.1f}%.")

    mnpi_red = bool(latest.mnpi_timed_for_comp_flag is True)
    if mnpi_red:
        notes.append("MNPI_TIMING_RED_FLAG: Issuer disclosed MNPI timing overlap with executive equity awards.")

    red = int(mnpi_red) + int(anchor_downgraded) + int(pay_tsr_div)
    yellow = int(highest_tier in ("weak_adjusted", "unclassified") and bool(classified))
    light: GovernanceLight = "red" if red > 0 else ("yellow" if yellow > 0 else "green")

    return IncentiveGovernanceAudit(
        quality_light=light,
        disclosure_regime=latest.disclosure_regime,
        latest_accession=latest.accession,
        latest_filed_at=latest.filed_at,
        prior_accession=prior.accession if prior else None,
        company_selected_measure=latest.company_selected_measure,
        highest_anchor_tier=highest_tier,
        highest_anchor_label=highest_label,
        classified_anchors=classified,
        anchor_downgraded_yoy=anchor_downgraded,
        removed_anchors_yoy=removed_yoy,
        added_anchors_yoy=added_yoy,
        founder_ceo_exempt=founder_exempt,
        pay_tsr_divergent=pay_tsr_div,
        mnpi_red_flag=mnpi_red,
        insider_trading_policy_adopted=latest.insider_trading_policy_adopted,
        latest_peo_total_comp_usd=latest_pvp.peo_total_comp_usd if latest_pvp else None,
        latest_peo_cap_usd=latest_pvp.peo_cap_usd if latest_pvp else None,
        pvp_rows=list(latest.pvp_rows)[:6],
        notes=notes,
    )


# --------------------------------------------------------------------------- Pillar 3: Guidance Credibility Ledger

def extract_guidance_records_from_dossier(
    dossier_dict: Mapping[str, Any],
    *,
    source_accession: str | None = None,
) -> list[GuidanceCredibilityRecord]:
    """Track A (0-LLM): extract objective guidance evaluations from a locked Direction A EarningsDossier."""
    snap = dossier_dict.get("snapshot") if isinstance(dossier_dict.get("snapshot"), Mapping) else dossier_dict
    gn = snap.get("guidance_nongaap") if isinstance(snap, Mapping) else None
    if not isinstance(gn, Mapping):
        return []
    evals = gn.get("guidance_evaluations")
    if not isinstance(evals, list):
        return []
    period_label = str(gn.get("period_label") or snap.get("period_label") or "")
    disclosed_at = str(dossier_dict.get("generated_at") or "")[:10] or None
    out: list[GuidanceCredibilityRecord] = []
    for ev in evals:
        if not isinstance(ev, Mapping):
            continue
        m_name = str(ev.get("metric_name") or "").strip()
        if not m_name:
            continue
        basis_raw = str(ev.get("basis") or "").lower()
        basis: GuidanceBasis = "non_gaap" if "non" in basis_raw else ("gaap" if "gaap" in basis_raw else "unknown")
        vs_prior = ev.get("vs_prior_guidance") if isinstance(ev.get("vs_prior_guidance"), Mapping) else {}
        ng_dir = ev.get("new_guidance_direction") if isinstance(ev.get("new_guidance_direction"), Mapping) else {}
        actual_v = ev.get("actual_value")
        g_low = vs_prior.get("prior_low") or ng_dir.get("new_low")
        g_mid = vs_prior.get("prior_mid") or ng_dir.get("new_mid")
        g_high = vs_prior.get("prior_high") or ng_dir.get("new_high")
        if g_mid is None and g_low is not None and g_high is not None:
            dl, dh = _dec(g_low), _dec(g_high)
            if dl is not None and dh is not None:
                g_mid = format((dl + dh) / Decimal(2), "f")
        if g_mid is None and actual_v is None:
            continue
        out.append(
            GuidanceCredibilityRecord(
                period_label=period_label or str(ev.get("period_label") or "unknown"),
                metric_key=m_name,
                basis=basis,
                guidance_low=str(g_low) if g_low is not None else None,
                guidance_mid=str(g_mid) if g_mid is not None else None,
                guidance_high=str(g_high) if g_high is not None else None,
                actual_value=str(actual_v) if actual_v is not None else None,
                actual_basis=basis if actual_v is not None else "unknown",
                source_kind="direction_a_dossier",
                source_accession=source_accession,
                disclosed_at=disclosed_at,
            )
        )
    return out


def upsert_guidance_ledger_records(
    ledger_repo: JsonDocumentRepository[Any],
    *,
    cik: str,
    symbol: str,
    new_records: Sequence[GuidanceCredibilityRecord],
    backfilled_accessions: Sequence[str] = (),
) -> GuidanceCredibilityLedger:
    """Idempotently merge new guidance records into per-CIK ``GUIDANCE_CREDIBILITY_LEDGER``."""
    cik_norm = cik10(cik)
    key = f"gcl:{cik_norm}"
    raw = ledger_repo.get(key)
    existing = (
        GuidanceCredibilityLedger.model_validate(raw)
        if isinstance(raw, Mapping)
        else GuidanceCredibilityLedger(cik=cik_norm, symbol=symbol.upper())
    )
    by_sig: dict[tuple[str, str, str], GuidanceCredibilityRecord] = {
        (r.period_label.lower(), r.metric_key.lower(), r.basis): r for r in existing.records
    }
    for r in new_records:
        sig = (r.period_label.lower(), r.metric_key.lower(), r.basis)
        by_sig[sig] = r
    merged_accs = sorted(set(existing.backfilled_accessions) | set(backfilled_accessions))
    updated = GuidanceCredibilityLedger(
        cik=cik_norm,
        symbol=symbol.upper(),
        records=sorted(by_sig.values(), key=lambda x: (x.disclosed_at or "", x.period_label), reverse=True)[:40],
        backfilled_accessions=merged_accs,
        updated_at=_utcnow(),
    )
    ledger_repo.put(key, updated.model_dump(mode="json"))
    return updated


def evaluate_guidance_credibility(
    ledger: GuidanceCredibilityLedger | None,
    *,
    rules: V25GovernanceRules,
    as_of: date | None = None,
) -> GuidanceCredibilityAudit:
    """Pure in-memory (<1ms) basis-locked scoring of guidance credibility."""
    gc_rules = rules.guidance_credibility
    if ledger is None or not ledger.records:
        return GuidanceCredibilityAudit(
            quality_light="unavailable",
            profile="insufficient_history",
            notes=["No guidance history recorded yet; run Direction A earnings research or 6Q 8-K backfill."],
        )

    rows_src = list(ledger.records)
    if as_of is not None:
        cutoff = as_of.isoformat()
        rows_src = [r for r in rows_src if not r.disclosed_at or r.disclosed_at <= cutoff]

    scored: list[ScoredGuidanceRow] = []
    surprises: list[Decimal] = []
    beats = inlines = misses = 0
    band = Decimal(str(gc_rules.inline_band_pct))

    for r in rows_src:
        g_mid = _dec(r.guidance_mid)
        act = _dec(r.actual_value)
        if act is None or g_mid is None or g_mid == 0:
            scored.append(
                ScoredGuidanceRow(
                    period_label=r.period_label,
                    metric_key=r.metric_key,
                    basis=r.basis,
                    guidance_mid=r.guidance_mid,
                    actual_value=r.actual_value,
                    verdict="pending_actual",
                    source_kind=r.source_kind,
                    source_accession=r.source_accession,
                    disclosed_at=r.disclosed_at,
                )
            )
            continue
        if r.basis != "unknown" and r.actual_basis != "unknown" and r.basis != r.actual_basis:
            scored.append(
                ScoredGuidanceRow(
                    period_label=r.period_label,
                    metric_key=r.metric_key,
                    basis=r.basis,
                    guidance_mid=r.guidance_mid,
                    actual_value=r.actual_value,
                    verdict="basis_mismatch_unscored",
                    source_kind=r.source_kind,
                    source_accession=r.source_accession,
                    disclosed_at=r.disclosed_at,
                )
            )
            continue

        pct = (act - g_mid) / abs(g_mid) * Decimal(100)
        surprises.append(pct)
        if pct > band:
            verdict: GuidanceVerdict = "beat"
            beats += 1
        elif pct < -band:
            verdict = "miss"
            misses += 1
        else:
            verdict = "inline"
            inlines += 1
        scored.append(
            ScoredGuidanceRow(
                period_label=r.period_label,
                metric_key=r.metric_key,
                basis=r.basis,
                guidance_mid=r.guidance_mid,
                actual_value=r.actual_value,
                surprise_pct=_fmt_dec(pct, 2),
                verdict=verdict,
                source_kind=r.source_kind,
                source_accession=r.source_accession,
                disclosed_at=r.disclosed_at,
            )
        )

    n_scored = beats + inlines + misses
    notes: list[str] = []
    if n_scored < gc_rules.min_quarters_for_rating:
        notes.append(f"Only {n_scored} scored quarter(s) (< {gc_rules.min_quarters_for_rating} minimum); rating withheld.")
        return GuidanceCredibilityAudit(
            quality_light="unavailable",
            profile="insufficient_history",
            scored_quarters=n_scored,
            total_records=len(rows_src),
            beat_count=beats,
            inline_count=inlines,
            miss_count=misses,
            rows=scored[:12],
            notes=notes,
        )

    n_dec = Decimal(n_scored)
    beat_rate = Decimal(beats) / n_dec * Decimal(100)
    inline_rate = Decimal(inlines) / n_dec * Decimal(100)
    miss_rate = Decimal(misses) / n_dec * Decimal(100)
    avg_surp = sum(surprises, Decimal(0)) / n_dec

    if beat_rate >= Decimal(str(gc_rules.sandbagger_min_beat_rate_pct)) and avg_surp >= Decimal(
        str(gc_rules.sandbagger_min_avg_surprise_pct)
    ):
        profile: CredibilityProfile = "chronic_sandbagger"
        light: GovernanceLight = "green"
    elif miss_rate >= Decimal(str(gc_rules.miss_prone_min_miss_rate_pct)):
        profile = "guidance_miss_prone"
        light = "red"
    elif (beat_rate + inline_rate) >= Decimal(str(gc_rules.high_credibility_min_beat_or_inline_pct)):
        profile = "high_credibility"
        light = "green"
    else:
        profile = "mixed"
        light = "yellow"

    return GuidanceCredibilityAudit(
        quality_light=light,
        profile=profile,
        scored_quarters=n_scored,
        total_records=len(rows_src),
        beat_count=beats,
        inline_count=inlines,
        miss_count=misses,
        beat_rate_pct=_fmt_dec(beat_rate, 1),
        inline_rate_pct=_fmt_dec(inline_rate, 1),
        miss_rate_pct=_fmt_dec(miss_rate, 1),
        avg_surprise_vs_mid_pct=_fmt_dec(avg_surp, 2),
        rows=scored[:12],
        notes=notes,
    )


# --------------------------------------------------------------------------- Unified Layer 1 Assembly (0-LLM, <5ms)

def assemble_governance_audit_report(
    *,
    snap: PeerCompanySnapshot,
    def14a_latest: Def14aFactSnapshot | None,
    def14a_prior: Def14aFactSnapshot | None,
    guidance_ledger: GuidanceCredibilityLedger | None,
    price_usd: Decimal | None = None,
    as_of: date | None = None,
    fx_to_usd: Decimal | None = None,
    elapsed_ms: int = 0,
    rules_path: Path | None = None,
) -> GovernanceAuditReport:
    """Assemble full Layer 1 V2.5 report live in-memory from pure fact stores + externalized rules."""
    rules, rules_sha, compiled = load_v25_governance_rules(rules_path)
    cap_audit = compute_capital_allocation_audit(
        snap, rules=rules, price_usd=price_usd, as_of=as_of, fx_to_usd=fx_to_usd
    )
    inc_audit = evaluate_incentive_governance(
        def14a_latest, def14a_prior, rules=rules, compiled_anchors=compiled
    )
    gui_audit = evaluate_guidance_credibility(guidance_ledger, rules=rules, as_of=as_of)

    lights = [cap_audit.quality_light, inc_audit.quality_light, gui_audit.quality_light]
    if "red" in lights:
        overall: GovernanceLight = "red"
    elif "yellow" in lights:
        overall = "yellow"
    elif "green" in lights:
        overall = "green"
    else:
        overall = "unavailable"

    bits: list[str] = []
    if cap_audit.net_diluted_share_change_3y_pct is not None:
        v_s = cap_audit.net_diluted_share_change_3y_pct
        sign = "+" if not v_s.startswith("-") else ""
        bits.append(f"3Y净股本 {sign}{v_s}%")
    if cap_audit.sbc_pct_of_fcf is not None:
        bits.append(f"SBC/FCF {cap_audit.sbc_pct_of_fcf}%")
    if inc_audit.disclosure_regime == "us_pvp_ixbrl":
        bits.append(f"高管锚: {inc_audit.company_selected_measure or inc_audit.highest_anchor_label}")
    elif inc_audit.disclosure_regime == "fpi_20f_limited":
        bits.append("FPI豁免DEF14A")
    if gui_audit.scored_quarters > 0 and gui_audit.beat_rate_pct is not None:
        bits.append(f"指引兑现 {gui_audit.beat_count}/{gui_audit.scored_quarters}超 ({gui_audit.beat_rate_pct}%)")
    else:
        bits.append("指引台账待回溯")

    return GovernanceAuditReport(
        cik=snap.cik,
        symbol=snap.symbol,
        company_name=snap.company_name,
        as_of=as_of.isoformat() if as_of else None,
        rules_version=rules.rules_version,
        rules_sha256=rules_sha,
        overall_light=overall,
        headline_anchor_summary=" · ".join(bits),
        capital_allocation=cap_audit,
        incentive_governance=inc_audit,
        guidance_credibility=gui_audit,
        stale_projection=snap.projection_version < 3,
        elapsed_ms=elapsed_ms,
    )


# --------------------------------------------------------------------------- Layer 2: CD&A Brief & 6Q 8-K Backfill

_CDA_ANCHOR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"compensation discussion and analysis|executive summary", re.IGNORECASE),
    re.compile(r"annual (?:cash )?incentive|short[- ]term incentive", re.IGNORECASE),
    re.compile(r"long[- ]term (?:equity )?incentive|performance (?:stock|share) units|psu", re.IGNORECASE),
    re.compile(r"clawback|stock ownership guidelines|say[- ]on[- ]pay", re.IGNORECASE),
)


def slice_cda_anchor_windows(plain_text: str, *, max_total_chars: int = _MAX_CDA_PROMPT_CHARS) -> str:
    """Deterministically slice up to 4 CD&A anchor windows from DEF 14A plain text (<= 20KB)."""
    if not plain_text:
        return ""
    if len(plain_text) <= max_total_chars:
        return plain_text
    per_win = max_total_chars // 4
    windows: list[str] = []
    used_ranges: list[tuple[int, int]] = []
    for pat in _CDA_ANCHOR_PATTERNS:
        m = pat.search(plain_text)
        if not m:
            continue
        start = max(0, m.start() - 300)
        end = min(len(plain_text), start + per_win)
        if any(not (end <= u0 or start >= u1) for u0, u1 in used_ranges):
            continue
        used_ranges.append((start, end))
        windows.append(plain_text[start:end].strip())
    if not windows:
        return plain_text[:max_total_chars]
    return "\n\n---\n\n".join(windows)[:max_total_chars]


def _verify_gov_cited_metrics(
    cited: Sequence[GovernanceCitedMetric],
    report: GovernanceAuditReport,
) -> tuple[list[GovernanceCitedMetric], int]:
    flat: dict[str, Any] = {
        **report.capital_allocation.model_dump(mode="json"),
        **report.incentive_governance.model_dump(mode="json"),
        **report.guidance_credibility.model_dump(mode="json"),
    }
    verified: list[GovernanceCitedMetric] = []
    ok_cnt = 0
    for cm in cited:
        actual = flat.get(cm.metric_key)
        ok = False
        if actual is not None:
            ad, cd = _dec(actual), _dec(cm.value)
            if ad is not None and cd is not None:
                ok = abs(ad - cd) <= Decimal("0.20")
            else:
                ok = str(actual).strip().lower() == str(cm.value).strip().lower()
        if ok:
            ok_cnt += 1
        verified.append(cm.model_copy(update={"verified": ok}))
    return verified, ok_cnt


def generate_cda_attribution_brief(
    report: GovernanceAuditReport,
    *,
    cda_excerpt: str,
    runner: Any,
    model_name: str | None = None,
    max_repairs: int = 1,
) -> CdaAttributionBrief:
    """Layer 2 on-demand CD&A deep attribution with built-in Skeptic + numeric & verbatim quote gates."""
    compact_facts = {
        "symbol": report.symbol,
        "headline_anchor_summary": report.headline_anchor_summary,
        "capital_allocation": {
            "quality_light": report.capital_allocation.quality_light,
            "sbc_pct_of_revenue": report.capital_allocation.sbc_pct_of_revenue,
            "sbc_pct_of_fcf": report.capital_allocation.sbc_pct_of_fcf,
            "buyback_to_sbc_ratio": report.capital_allocation.buyback_to_sbc_ratio,
            "net_diluted_share_change_3y_pct": report.capital_allocation.net_diluted_share_change_3y_pct,
            "true_net_shareholder_yield_pct": report.capital_allocation.true_net_shareholder_yield_pct,
            "fake_buyback_flag": report.capital_allocation.fake_buyback_flag,
        },
        "incentive_governance": {
            "quality_light": report.incentive_governance.quality_light,
            "company_selected_measure": report.incentive_governance.company_selected_measure,
            "highest_anchor_tier": report.incentive_governance.highest_anchor_tier,
            "anchor_downgraded_yoy": report.incentive_governance.anchor_downgraded_yoy,
            "founder_ceo_exempt": report.incentive_governance.founder_ceo_exempt,
            "mnpi_red_flag": report.incentive_governance.mnpi_red_flag,
            "latest_peo_total_comp_usd": report.incentive_governance.latest_peo_total_comp_usd,
        },
        "guidance_credibility": {
            "profile": report.guidance_credibility.profile,
            "scored_quarters": report.guidance_credibility.scored_quarters,
            "beat_rate_pct": report.guidance_credibility.beat_rate_pct,
            "avg_surprise_vs_mid_pct": report.guidance_credibility.avg_surprise_vs_mid_pct,
        },
    }
    facts_json = json.dumps(compact_facts, ensure_ascii=False, separators=(",", ":"))
    sliced_cda = slice_cda_anchor_windows(cda_excerpt, max_total_chars=_MAX_CDA_PROMPT_CHARS)

    system_prompt = (
        "You are a buy-side Governance, Executive Compensation & Capital Allocation Analyst.\n"
        "Analyze ONLY the Layer 1 deterministic facts JSON and the DEF 14A CD&A excerpt below.\n"
        "NEVER invent numbers or quotes. Include a sharp `skeptic_counterview` challenging the bull/bear governance narrative.\n"
        "For 3-6 key figures cited, include exact `metric_key` and `value` from the facts JSON in `cited_metrics`.\n"
        "For 1-3 key claims from the CD&A text, include exact verbatim substrings (15-220 chars) from the excerpt in `cited_quotes`.\n\n"
        "Output ONLY valid JSON matching `_LlmCdaBriefPayload` (`extra='forbid'`):\n"
        "{\n"
        '  "summary": "2-3 sentence executive takeaway",\n'
        '  "incentive_alignment_assessment": "Are executive incentives truly aligned with per-share value creation?",\n'
        '  "capital_allocation_verdict": "Is buyback genuinely shrinking share count or masking SBC dilution?",\n'
        '  "skeptic_counterview": "Strongest counter-argument against the primary governance conclusion",\n'
        '  "cited_metrics": [{"metric_key": "sbc_pct_of_fcf", "value": "5.7"}],\n'
        '  "cited_quotes": [{"quotation": "exact verbatim substring from CD&A excerpt"}]\n'
        "}"
    )
    user_prompt = f"LAYER1_FACTS_JSON:\n{facts_json}\n\nDEF14A_CDA_EXCERPT:\n{sliced_cda or '(no CD&A excerpt provided)'}"

    usage_totals = {"tokens": 0}
    conv_id = f"v25_cda_{report.cik}_{uuid4().hex[:6]}"

    def _invoke_fn(messages: list[dict[str, Any]], attempt_idx: int) -> Any:
        req = SimpleNamespace(
            stage="v25_governance_cda_audit",
            role="governance_cda_analyst",
            slot="deep_reasoner",
            system_prompt=system_prompt,
            user_prompt=messages[-1]["content"] if messages else user_prompt,
            messages=messages,
            tools=(),
            turn_index=attempt_idx,
            conversation_id=conv_id,
            response_schema=None,
            response_schema_name=None,
            max_output_tokens=16384,
        )
        resp = runner.invoke(req)
        u = getattr(resp, "usage", None)
        if isinstance(u, Mapping):
            usage_totals["tokens"] += int(
                u.get("total_tokens") or (int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0))
            )
        elif u is not None:
            usage_totals["tokens"] += int(
                getattr(u, "total_tokens", 0)
                or (int(getattr(u, "input_tokens", 0) or 0) + int(getattr(u, "output_tokens", 0) or 0))
            )
        return resp

    parsed, _repairs = invoke_and_parse_with_repair(
        invoke_fn=_invoke_fn,
        model_cls=_LlmCdaBriefPayload,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_repairs=max_repairs,
    )

    v_metrics, ok_m = _verify_gov_cited_metrics(parsed.cited_metrics, report)
    v_quotes: list[GovernanceCitedQuote] = []
    ok_q = 0
    open_gaps: list[str] = []
    for cq in parsed.cited_quotes:
        match = find_quotation_span(cq.quotation, sliced_cda) if sliced_cda else None
        if match is not None:
            neg_warn = check_negation_context(match)
            ok_q += 1
            v_quotes.append(cq.model_copy(update={"verified": True, "negation_warning": neg_warn}))
        else:
            open_gaps.append(f"unverified_quote_dropped:{cq.quotation[:60]}")
            v_quotes.append(cq.model_copy(update={"verified": False}))

    for vm in v_metrics:
        if not vm.verified:
            open_gaps.append(f"unverified_metric:{vm.metric_key}={vm.value}")

    return CdaAttributionBrief(
        cik=report.cik,
        symbol=report.symbol,
        def14a_accession=report.incentive_governance.latest_accession or "unknown",
        rules_sha256=report.rules_sha256,
        summary=parsed.summary,
        incentive_alignment_assessment=parsed.incentive_alignment_assessment,
        capital_allocation_verdict=parsed.capital_allocation_verdict,
        skeptic_counterview=parsed.skeptic_counterview,
        cited_metrics=v_metrics,
        cited_quotes=v_quotes,
        verified_metric_count=ok_m,
        total_cited_metric_count=len(v_metrics),
        verified_quote_count=ok_q,
        total_cited_quote_count=len(v_quotes),
        open_gaps=open_gaps,
        tokens_used=usage_totals["tokens"],
        model_used=model_name,
    )


class _LlmBackfillRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accession: str
    period_label: str
    metric_key: str
    basis: GuidanceBasis = "unknown"
    guidance_low: str | None = None
    guidance_mid: str | None = None
    guidance_high: str | None = None
    actual_value: str | None = None
    actual_basis: GuidanceBasis = "unknown"
    verbatim_quote: str = Field(min_length=8, max_length=320)


class _LlmBackfillPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[_LlmBackfillRow] = Field(default_factory=list)


_OUTLOOK_HEADING_RE = re.compile(
    r"\b(?:outlook|financial\s+guidance|business\s+outlook|guidance\s+for)\b.{0,180}?(?:expected|revenue|billion|million|gross\s+margin|eps)",
    re.IGNORECASE | re.DOTALL,
)
_OUTLOOK_RE = re.compile(r"\b(outlook|guidance|expects?|project(?:s|ed)|next quarter|full[- ]year)\b", re.IGNORECASE)


def slice_8k_outlook_excerpt(plain_text: str, *, max_chars: int = _MAX_8K_SLICE_CHARS) -> str:
    """Slice the quantitative Outlook/Guidance window from an 8-K Exhibit 99.1 release."""
    if len(plain_text) <= max_chars:
        return plain_text
    m = _OUTLOOK_HEADING_RE.search(plain_text)
    if m:
        start = max(0, m.start() - 150)
        return plain_text[start : start + max_chars].strip()
    m2 = _OUTLOOK_RE.search(plain_text)
    if not m2:
        return plain_text[:max_chars]
    start = max(0, m2.start() - 350)
    return plain_text[start : start + max_chars].strip()


def backfill_guidance_from_8k_releases(
    *,
    cik: str,
    symbol: str,
    releases: Sequence[tuple[str, str, str]],  # [(accession, filed_at, plain_text)]
    runner: Any,
    ledger_repo: JsonDocumentRepository[Any] | None = None,
    max_repairs: int = 1,
) -> tuple[list[GuidanceCredibilityRecord], int, list[str]]:
    """Track B (on-demand): 1-shot batch extraction of 4-6Q guidance from 8-K Ex-99.1 with quote verification."""
    if not releases:
        return [], 0, ["no_8k_releases_provided"]

    sliced_map: dict[str, tuple[str, str]] = {}
    blocks: list[str] = []
    for acc, filed_at, text in list(releases)[:6]:
        ex = slice_8k_outlook_excerpt(text, max_chars=_MAX_8K_SLICE_CHARS)
        sliced_map[acc] = (filed_at, ex)
        blocks.append(f"=== ACCESSION: {acc} | FILED: {filed_at} ===\n{ex}")

    system_prompt = (
        "You are a buy-side Earnings Guidance Extractor.\n"
        "From the SEC Form 8-K Exhibit 99.1 excerpts below, extract quantitative forward revenue/EPS/margin guidance "
        "AND matching actuals if present. Strictly tag `basis` as `gaap`, `non_gaap`, or `unknown`.\n"
        "Every row MUST include an exact `verbatim_quote` substring copied from that accession's excerpt.\n"
        "Output ONLY valid JSON matching `_LlmBackfillPayload` (`extra='forbid'`):\n"
        '{"records":[{"accession":"...","period_label":"Q3 FY26","metric_key":"revenue_usd_m","basis":"gaap",'
        '"guidance_low":"32000","guidance_mid":"32500","guidance_high":"33000","actual_value":null,'
        '"actual_basis":"unknown","verbatim_quote":"exact substring"}]}'
    )
    user_prompt = "\n\n".join(blocks)
    usage_totals = {"tokens": 0}
    conv_id = f"v25_gbf_{cik10(cik)}_{uuid4().hex[:6]}"

    def _invoke_fn(messages: list[dict[str, Any]], attempt_idx: int) -> Any:
        req = SimpleNamespace(
            stage="v25_guidance_backfill",
            role="guidance_backfill_extractor",
            slot="deep_reasoner",
            system_prompt=system_prompt,
            user_prompt=messages[-1]["content"] if messages else user_prompt,
            messages=messages,
            tools=(),
            turn_index=attempt_idx,
            conversation_id=conv_id,
            response_schema=None,
            response_schema_name=None,
            max_output_tokens=16384,
        )
        resp = runner.invoke(req)
        u = getattr(resp, "usage", None)
        if isinstance(u, Mapping):
            usage_totals["tokens"] += int(
                u.get("total_tokens") or (int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0))
            )
        elif u is not None:
            usage_totals["tokens"] += int(
                getattr(u, "total_tokens", 0)
                or (int(getattr(u, "input_tokens", 0) or 0) + int(getattr(u, "output_tokens", 0) or 0))
            )
        return resp

    parsed, _ = invoke_and_parse_with_repair(
        invoke_fn=_invoke_fn,
        model_cls=_LlmBackfillPayload,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_repairs=max_repairs,
    )

    verified_records: list[GuidanceCredibilityRecord] = []
    open_gaps: list[str] = []
    for row in parsed.records:
        if row.accession not in sliced_map:
            open_gaps.append(f"unknown_accession_dropped:{row.accession}")
            continue
        filed_at, ex_text = sliced_map[row.accession]
        match = find_quotation_span(row.verbatim_quote, ex_text)
        if match is None:
            open_gaps.append(f"unverified_quote_dropped:{row.accession}:{row.metric_key}")
            continue
        g_mid = row.guidance_mid
        if g_mid is None and row.guidance_low is not None and row.guidance_high is not None:
            dl, dh = _dec(row.guidance_low), _dec(row.guidance_high)
            if dl is not None and dh is not None:
                g_mid = format((dl + dh) / Decimal(2), "f")
        verified_records.append(
            GuidanceCredibilityRecord(
                period_label=row.period_label,
                metric_key=row.metric_key,
                basis=row.basis,
                guidance_low=row.guidance_low,
                guidance_mid=g_mid,
                guidance_high=row.guidance_high,
                actual_value=row.actual_value,
                actual_basis=row.actual_basis,
                source_kind="8k_backfill",
                source_accession=row.accession,
                disclosed_at=filed_at,
                verbatim_quote=row.verbatim_quote,
                quote_verified=True,
            )
        )

    if ledger_repo is not None and verified_records:
        upsert_guidance_ledger_records(
            ledger_repo,
            cik=cik,
            symbol=symbol,
            new_records=verified_records,
            backfilled_accessions=list(sliced_map.keys()),
        )

    return verified_records, usage_totals["tokens"], open_gaps
