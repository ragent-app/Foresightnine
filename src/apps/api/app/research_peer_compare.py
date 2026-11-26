"""V2 Peer Alignment & Relative Valuation Engine (Layer 1 0-LLM Decimal + Layer 2 Optional Attribution).

Implements the v2 consolidated specification:
- Two-gate SEC snapshot loader: lightweight ``submissions`` fingerprint check -> Postgres
  ``PEER_COMPANY_SNAPSHOTS`` batch lookup -> per-CIK Singleflight + ``BoundedSemaphore(2)``
  streaming parse of ``companyfacts`` projecting only ~16 standard lines (full ``filed_at``
  history, ~25-40KB/company) so ``512M / 1.0 vCPU`` containers never OOM and arbitrary
  historical ``as_of`` queries work with 0 secondary downloads.
- Shared Redis ``SecPacingLimiter`` (4.5-6 RPS + 429 global cooldown) — never collides with
  V1/Direction B radar.
- Strict SEC raw periods (no synthetic calendarization interpolation): TTM + latest single-Q YoY,
  explicit ``period_end_date``, ``period_basis`` (``TTM`` / ``FY``), ``accounting_standard``,
  and ``currency``, plus deterministic ``comparability_warnings``.
- 3-tier FPI compatibility (all dimensionless ratios preserved on 20-F FY; valuation isolated
  unless verified FX + ADR ratio is supplied), ``6000-6299``/``6300-6411``/``6798`` financial
  SIC gate (``quality_light="not_applicable"``), debt-free balance-sheet completeness guard,
  pure ``ttm_roic_pct`` (tax clamped ``[0%, 35%]``, ``Invested Capital <= 0`` guard),
  dual-class share count divergence guard (``> 15%`` vs DEI), loss-quarter ``shares_basic``
  fallback, and both ``ev_to_fcf_ttm`` & ``ev_to_fcf_ex_sbc_ttm`` + uniform Reverse-DCF
  (10% WACC / 3% terminal growth).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import re
import json
from pathlib import Path
from threading import BoundedSemaphore, Lock, RLock
from types import SimpleNamespace
from typing import Any, Callable, Literal, Mapping, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .fin_data.adapters.sec_edgar import (
    STANDARD_LINES,
    Period,
    cik10,
    select_line,
)
from .fin_data.adapters.sec_events.pacing import (
    SecPacingError,
    SecPacingLimiter,
    fetch_company_submissions,
    sec_get_json,
)
from .research_earnings_diagnostics import evaluate_sec_sic_gate
from .research_llm_primitives import invoke_and_parse_with_repair
from .storage import JsonDocumentRepository

QualityLight = Literal["green", "yellow", "red", "not_applicable"]
PeriodBasis = Literal["TTM", "FY", "partial"]
WarningSeverity = Literal["warning", "error", "info"]

_PEER_GROUPS_PATH = Path(__file__).resolve().parent / "fin_data" / "adapters" / "peer_groups.json"
_MAX_COMPANYFACTS_PARSE_CONCURRENCY = 2
_PARSE_SEMAPHORE = BoundedSemaphore(_MAX_COMPANYFACTS_PARSE_CONCURRENCY)
_SINGLEFLIGHT_LOCK = RLock()
_SINGLEFLIGHT_LOCKS: dict[str, Lock] = {}
_TICKER_MAP_LOCK = RLock()
_TICKER_MAP_CACHE: tuple[datetime, dict[str, tuple[str, str]]] | None = None


def resolve_ticker_to_cik(
    symbol: str,
    *,
    user_agent: str | None,
    limiter: SecPacingLimiter | None = None,
    policy: Any = None,
    transport: Any = None,
) -> tuple[str, str]:
    """Resolve US ticker symbol to ``(cik10, title)`` via cached SEC ``company_tickers.json``."""
    global _TICKER_MAP_CACHE
    sym_u = symbol.strip().upper().replace(".", "-")
    sym_raw = symbol.strip().upper()
    now = _utcnow()
    with _TICKER_MAP_LOCK:
        cached = _TICKER_MAP_CACHE
        if cached is not None and (now - cached[0]) < timedelta(hours=24):
            hit = cached[1].get(sym_u) or cached[1].get(sym_raw)
            if hit is not None:
                return hit

    doc, _sha = sec_get_json(
        "https://www.sec.gov/files/company_tickers.json",
        category="filings",
        user_agent=user_agent,
        limiter=limiter,
        policy=policy,
        transport=transport,
        max_bytes=15_000_000,
        large=True,
    )
    mapping: dict[str, tuple[str, str]] = {}
    if isinstance(doc, Mapping):
        for entry in doc.values():
            if not isinstance(entry, Mapping):
                continue
            t = str(entry.get("ticker") or "").strip().upper()
            c_raw = entry.get("cik_str")
            title = str(entry.get("title") or "").strip()
            if t and c_raw is not None:
                mapping[t] = (cik10(c_raw), title)
    with _TICKER_MAP_LOCK:
        _TICKER_MAP_CACHE = (now, mapping)
    hit = mapping.get(sym_u) or mapping.get(sym_raw)
    if hit is None:
        raise SecPacingError("ticker_not_found", f"Ticker {symbol!r} not found in SEC company_tickers.json.", retryable=False)
    return hit

_CURRENT_PROJECTION_VERSION = 5

_V2_PROJECTION_LINES: tuple[str, ...] = (
    "revenue",
    "cost_of_revenue",
    "gross_profit",
    "operating_income",
    "interest_expense",
    "net_income",
    "income_tax",
    "profit_before_tax",
    "shares_diluted",
    "shares_basic",
    "eps_diluted",
    "eps_basic",
    "cash",
    "short_term_investments",
    "accounts_receivable",
    "inventory",
    "accounts_payable",
    "short_term_debt",
    "long_term_debt",
    "ppe_net",
    "total_assets",
    "total_liabilities",
    "equity",
    "non_controlling_interests",
    "rd_expense",
    "sga_expense",
    "selling_expense",
    "admin_expense",
    "total_operating_costs",
    "depreciation_amortization",
    "operating_cash_flow",
    "capex",
    "stock_based_compensation",
    "share_repurchases",
    "dividends_paid",
    "sbc_tax_withholding",
    "unrecognized_sbc",
    "repurchased_shares",
    "rsu_grants_shares",
    # V3a 10-Q collision, footnote & finance-lease CapEx bridge lines (47 lines total)
    "rpo",
    "deferred_revenue_current",
    "deferred_revenue_noncurrent",
    "restructuring_charges",
    "impairment_charges",
    "purchase_commitments",
    "finance_lease_principal_payments",
    "finance_lease_rou_additions",
)

_ISO_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

_REVERSE_DCF_WACC = Decimal("0.10")
_REVERSE_DCF_TERMINAL_G = Decimal("0.03")
_REVERSE_DCF_YEARS = 5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:14]}"


def _dec(val: Any) -> Decimal | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        d = Decimal(str(val).strip().replace(",", "").replace("%", ""))
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _fmt_dec(val: Decimal | None, places: int = 2) -> str | None:
    if val is None or not val.is_finite():
        return None
    q = Decimal(10) ** (-places)
    return format(val.quantize(q, rounding=ROUND_HALF_UP), "f")


def _singleflight_lock(cik: str) -> Lock:
    with _SINGLEFLIGHT_LOCK:
        lk = _SINGLEFLIGHT_LOCKS.get(cik)
        if lk is None:
            lk = Lock()
            _SINGLEFLIGHT_LOCKS[cik] = lk
        return lk


# --------------------------------------------------------------------------- Pydantic Contracts

class _FrozenPeerModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CuratedPeerGroup(_FrozenPeerModel):
    group_id: str
    name: str
    symbols: list[str] = Field(default_factory=list)


class UserPeerGroup(_FrozenPeerModel):
    group_id: str = Field(default_factory=lambda: _new_id("pgrp"))
    account_id: str
    name: str = Field(min_length=1, max_length=120)
    symbols: list[str] = Field(min_length=2, max_length=6)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class FpiAdrMeta(_FrozenPeerModel):
    adr_ratio: str = "1"
    reporting_currency: str = "USD"
    notes: str | None = None


class PeerSuggestResponse(_FrozenPeerModel):
    symbol: str
    recommended_group: CuratedPeerGroup | None = None
    recommended_peers: list[str] = Field(default_factory=list)
    curated_groups: list[CuratedPeerGroup] = Field(default_factory=list)
    user_groups: list[UserPeerGroup] = Field(default_factory=list)


class ProjectedFactRow(_FrozenPeerModel):
    start: str | None = None
    end: str
    val: str
    filed: str | None = None
    form: str | None = None
    accn: str | None = None
    fy: int | None = None
    fp: str | None = None
    frame: str | None = None


class PeerCompanySnapshot(_FrozenPeerModel):
    """Lightweight (~25-40KB) projected SEC companyfacts snapshot with full ``filed_at`` history."""

    cik: str
    symbol: str
    company_name: str | None = None
    sic: str | None = None
    sic_description: str | None = None
    latest_periodic_accession: str | None = None
    xbrl_indexing_pending_accession: str | None = None
    latest_periodic_filed_at: str | None = None
    taxonomy: str = "us-gaap"
    currency: str = "USD"
    projection_version: int = 1
    # line_name -> tag -> unit -> list[ProjectedFactRow]
    facts_by_line: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = Field(default_factory=dict)
    dei_shares_outstanding: list[dict[str, Any]] = Field(default_factory=list)
    cached_at: datetime = Field(default_factory=_utcnow)


class ComparabilityWarning(_FrozenPeerModel):
    code: str
    severity: WarningSeverity = "warning"
    symbols: list[str] = Field(default_factory=list)
    message: str


class PeerCompanyMetrics(_FrozenPeerModel):
    """Deterministic financial, quality, and relative valuation row for one peer."""

    symbol: str
    cik: str
    company_name: str | None = None
    sic: str | None = None
    sector_category: str = "non_financial"
    is_financial_gated: bool = False
    is_comparable_for_median: bool = True
    non_comparable_reasons: list[str] = Field(default_factory=list)

    # Period & accounting anchor
    period_end_date: str | None = None
    fiscal_period_label: str | None = None
    period_basis: PeriodBasis = "partial"
    accounting_standard: str = "us-gaap"
    currency: str = "USD"
    price_usd: str | None = None
    price_as_of_date: str | None = None
    price_source: str | None = None

    # Scale
    market_cap_usd: str | None = None
    enterprise_value_usd: str | None = None
    net_cash_usd: str | None = None
    ttm_revenue_usd: str | None = None
    ttm_fcf_usd: str | None = None

    # Growth (%)
    ttm_revenue_yoy_pct: str | None = None
    latest_q_revenue_yoy_pct: str | None = None
    revenue_accel_pp: str | None = None

    # Profitability (%)
    ttm_gross_margin_pct: str | None = None
    ttm_operating_margin_pct: str | None = None
    ttm_net_margin_pct: str | None = None
    ttm_roic_pct: str | None = None

    # Cash quality
    ttm_fcf_margin_pct: str | None = None
    ocf_to_ni_ratio: str | None = None
    sbc_pct_of_fcf: str | None = None
    quality_light: QualityLight = "green"
    quality_notes: list[str] = Field(default_factory=list)

    # Relative valuation & Reverse-DCF
    pe_ttm: str | None = None
    ev_to_sales_ttm: str | None = None
    ev_to_ebit_ttm: str | None = None
    ev_to_fcf_ttm: str | None = None
    ev_to_fcf_ex_sbc_ttm: str | None = None
    reverse_dcf_implied_5y_fcf_cagr_pct: str | None = None

    # Ranks & spreads vs peer median
    ranks: dict[str, int] = Field(default_factory=dict)
    vs_median: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class PeerComparisonMatrix(_FrozenPeerModel):
    """Complete 0-LLM Layer 1 peer comparison output."""

    comparison_id: str = Field(default_factory=lambda: _new_id("cmp"))
    primary_symbol: str
    as_of: str | None = None
    exclude_non_comparable_from_median: bool = True
    reverse_dcf_assumptions: str = "Uniform WACC 10.0% / Terminal Growth 3.0% / 5Y explicit horizon (excludes company-specific beta)"
    companies: list[PeerCompanyMetrics] = Field(default_factory=list)
    peer_medians: dict[str, str | None] = Field(default_factory=dict)
    comparability_warnings: list[ComparabilityWarning] = Field(default_factory=list)
    primary_coordinate_summary: str = ""
    # Stable WatchTrigger metric_keys pre-wired for Direction B ThesisLedger
    primary_watch_trigger_metrics: dict[str, str | None] = Field(default_factory=dict)
    cache_hits: int = 0
    cache_misses: int = 0
    elapsed_ms: int = 0
    generated_at: datetime = Field(default_factory=_utcnow)


class PeerCitedMetric(_FrozenPeerModel):
    symbol: str
    metric_key: str
    value: str
    verified: bool = False


class _LlmPeerBriefPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=1200)
    valuation_vs_fundamentals: str = Field(min_length=1, max_length=1800)
    relative_risks: list[str] = Field(default_factory=list)
    cited_metrics: list[PeerCitedMetric] = Field(default_factory=list)


class PeerAttributionBrief(_FrozenPeerModel):
    """Optional Layer 2 on-demand LLM relative valuation attribution with server numeric gate."""

    comparison_id: str
    primary_symbol: str
    summary: str
    valuation_vs_fundamentals: str
    relative_risks: list[str] = Field(default_factory=list)
    cited_metrics: list[PeerCitedMetric] = Field(default_factory=list)
    verified_metric_count: int = 0
    total_cited_metric_count: int = 0
    tokens_used: int = 0
    model_used: str | None = None
    generated_at: datetime = Field(default_factory=_utcnow)


# --------------------------------------------------------------------------- Config Loading

def load_peer_groups_config() -> tuple[list[CuratedPeerGroup], dict[str, FpiAdrMeta]]:
    raw = json.loads(_PEER_GROUPS_PATH.read_text(encoding="utf-8"))
    groups = [CuratedPeerGroup.model_validate(g) for g in raw.get("groups") or []]
    fpi_meta = {
        sym.upper(): FpiAdrMeta.model_validate(meta)
        for sym, meta in (raw.get("fpi_adr_metadata") or {}).items()
    }
    return groups, fpi_meta


def suggest_peers_for_symbol(
    symbol: str,
    *,
    user_groups: Sequence[UserPeerGroup] = (),
) -> PeerSuggestResponse:
    sym_u = symbol.strip().upper()
    curated, _fpi = load_peer_groups_config()
    matched: CuratedPeerGroup | None = None
    for grp in curated:
        if sym_u in grp.symbols:
            matched = grp
            break
    peers = [s for s in (matched.symbols if matched else []) if s != sym_u]
    return PeerSuggestResponse(
        symbol=sym_u,
        recommended_group=matched,
        recommended_peers=peers,
        curated_groups=curated,
        user_groups=list(user_groups),
    )


# --------------------------------------------------------------------------- Two-Gate Snapshot Loader

def project_companyfacts_to_snapshot(
    *,
    cik: str,
    symbol: str,
    company_name: str | None,
    sic: str | None,
    sic_description: str | None,
    latest_periodic_accession: str | None,
    latest_periodic_filed_at: str | None,
    companyfacts_doc: Mapping[str, Any],
) -> PeerCompanySnapshot:
    """Project only ~16 standard lines (full ``filed_at`` history) from raw SEC ``companyfacts``."""
    facts_root = companyfacts_doc.get("facts") if isinstance(companyfacts_doc, Mapping) else None
    if not isinstance(facts_root, Mapping):
        facts_root = {}

    has_gaap = isinstance(facts_root.get("us-gaap"), Mapping) and bool(facts_root.get("us-gaap"))
    has_ifrs = isinstance(facts_root.get("ifrs-full"), Mapping) and bool(facts_root.get("ifrs-full"))
    taxonomy = "us-gaap" if has_gaap else ("ifrs-full" if has_ifrs else "us-gaap")
    tax_node = facts_root.get(taxonomy) if isinstance(facts_root.get(taxonomy), Mapping) else {}

    facts_by_line: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    detected_currency = "USD"
    currency_found = False

    for line_name in _V2_PROJECTION_LINES:
        spec = STANDARD_LINES.get(line_name)
        if spec is None:
            continue
        line_bucket: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for tag in spec.tags.get(taxonomy, ()):
            tag_node = tax_node.get(tag)
            units = tag_node.get("units") if isinstance(tag_node, Mapping) else None
            if not isinstance(units, Mapping):
                continue
            kept_units: dict[str, list[dict[str, Any]]] = {}
            for unit_key, rows in units.items():
                if not isinstance(rows, list) or not rows:
                    continue
                if spec.unit_kind == "money":
                    if not _ISO_CURRENCY_RE.match(unit_key):
                        continue
                    if not currency_found and line_name == "revenue":
                        detected_currency = unit_key
                        currency_found = True
                elif spec.unit_kind == "shares":
                    if unit_key != "shares":
                        continue
                elif spec.unit_kind == "per_share":
                    uk_low = unit_key.lower()
                    if "/" not in uk_low and "per" not in uk_low:
                        continue
                    if not currency_found and "/" in unit_key:
                        c_part = unit_key.split("/", 1)[0].strip()
                        if _ISO_CURRENCY_RE.match(c_part):
                            detected_currency = c_part
                            currency_found = True
                compact_rows: list[dict[str, Any]] = []
                for r in rows:
                    if not isinstance(r, Mapping) or r.get("val") is None or not r.get("end"):
                        continue
                    compact_rows.append(
                        {
                            "start": r.get("start"),
                            "end": r.get("end"),
                            "val": r.get("val"),
                            "filed": r.get("filed"),
                            "form": r.get("form"),
                            "accn": r.get("accn"),
                            "fy": r.get("fy"),
                            "fp": r.get("fp"),
                            "frame": r.get("frame"),
                        }
                    )
                if compact_rows:
                    kept_units[unit_key] = compact_rows
            if kept_units:
                line_bucket[tag] = kept_units
        if line_bucket:
            facts_by_line[line_name] = line_bucket

    # DEI EntityCommonStockSharesOutstanding (for dual-class / split divergence check)
    dei_rows: list[dict[str, Any]] = []
    dei_node = facts_root.get("dei") if isinstance(facts_root.get("dei"), Mapping) else {}
    sh_node = dei_node.get("EntityCommonStockSharesOutstanding") if isinstance(dei_node, Mapping) else None
    sh_units = sh_node.get("units") if isinstance(sh_node, Mapping) else None
    if isinstance(sh_units, Mapping) and isinstance(sh_units.get("shares"), list):
        for r in sh_units["shares"]:
            if isinstance(r, Mapping) and r.get("val") is not None and r.get("end"):
                dei_rows.append({"end": r.get("end"), "val": r.get("val"), "filed": r.get("filed"), "form": r.get("form")})

    return PeerCompanySnapshot(
        cik=cik10(cik),
        symbol=symbol.upper(),
        company_name=company_name,
        sic=sic,
        sic_description=sic_description,
        latest_periodic_accession=latest_periodic_accession,
        latest_periodic_filed_at=latest_periodic_filed_at,
        taxonomy=taxonomy,
        currency=detected_currency,
        projection_version=_CURRENT_PROJECTION_VERSION,
        facts_by_line=facts_by_line,
        dei_shares_outstanding=dei_rows,
    )


def _projection_target_tags_by_taxonomy() -> dict[str, set[str]]:
    """Dynamically derive all unique XBRL tags needed for ``_V2_PROJECTION_LINES`` plus DEI shares."""
    by_tax: dict[str, set[str]] = {
        "us-gaap": set(),
        "ifrs-full": set(),
        "dei": {"EntityCommonStockSharesOutstanding"},
    }
    for line_name in _V2_PROJECTION_LINES:
        spec = STANDARD_LINES.get(line_name)
        if spec is None:
            continue
        for tax, tags in spec.tags.items():
            by_tax.setdefault(tax, set()).update(tags)
    return by_tax


def extract_sliced_companyfacts_from_file(path: Any) -> dict[str, Any]:
    """Extract only the target XBRL tags from a 10-60MB SEC ``companyfacts.json`` file on disk.

    Avoids deserializing 500+ unneeded XBRL concepts (cutting peak RSS from ~250MB to ~25-35MB
    on 512M containers) using pure stdlib byte-index scanning + ``json.JSONDecoder.raw_decode``.
    """
    import mmap
    from pathlib import Path

    target_by_tax = _projection_target_tags_by_taxonomy()
    facts_out: dict[str, dict[str, Any]] = {}
    decoder = json.JSONDecoder()
    file_path = Path(path)
    file_size = file_path.stat().st_size
    if file_size == 0:
        return {"facts": facts_out}

    def _find_matching_brace(mm: mmap.mmap, open_pos: int) -> int:
        depth = 0
        in_str = False
        esc = False
        pos = open_pos
        n = len(mm)
        while pos < n:
            b = mm[pos]
            if in_str:
                if esc:
                    esc = False
                elif b == 92:  # '\\'
                    esc = True
                elif b == 34:  # '"'
                    in_str = False
            else:
                if b == 34:  # '"'
                    in_str = True
                elif b == 123:  # '{'
                    depth += 1
                elif b == 125:  # '}'
                    depth -= 1
                    if depth == 0:
                        return pos
            pos += 1
        return -1

    with file_path.open("rb") as fh:
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            facts_key = mm.find(b'"facts"')
            if facts_key == -1:
                return json.loads(mm[:].decode("utf-8"))
            facts_open = mm.find(b"{", facts_key + 7)
            if facts_open == -1:
                return {"facts": facts_out}

            for tax_name, wanted_tags in target_by_tax.items():
                if not wanted_tags:
                    continue
                tax_token = f'"{tax_name}"'.encode("ascii")
                search_pos = facts_open + 1
                tax_open = -1
                while True:
                    hit = mm.find(tax_token, search_pos)
                    if hit == -1:
                        break
                    p = hit + len(tax_token)
                    while p < file_size and mm[p] in (32, 9, 10, 13):
                        p += 1
                    if p < file_size and mm[p] == 58:
                        p += 1
                        while p < file_size and mm[p] in (32, 9, 10, 13):
                            p += 1
                        if p < file_size and mm[p] == 123:
                            tax_open = p
                            break
                    search_pos = hit + len(tax_token)
                if tax_open == -1:
                    continue
                tax_close = _find_matching_brace(mm, tax_open)
                if tax_close == -1:
                    continue

                tax_bucket: dict[str, Any] = {}
                for tag in wanted_tags:
                    tag_token = f'"{tag}"'.encode("ascii")
                    t_search = tax_open + 1
                    while t_search < tax_close:
                        t_hit = mm.find(tag_token, t_search, tax_close)
                        if t_hit == -1:
                            break
                        p = t_hit + len(tag_token)
                        while p < tax_close and mm[p] in (32, 9, 10, 13):
                            p += 1
                        if p < tax_close and mm[p] == 58:
                            p += 1
                            while p < tax_close and mm[p] in (32, 9, 10, 13):
                                p += 1
                            if p < tax_close and mm[p] == 123:
                                obj_end = _find_matching_brace(mm, p)
                                if obj_end != -1 and obj_end <= tax_close:
                                    try:
                                        snippet = mm[p : obj_end + 1].decode("utf-8")
                                        parsed, _ = decoder.raw_decode(snippet)
                                        if isinstance(parsed, dict) and isinstance(parsed.get("units"), dict):
                                            tax_bucket[tag] = parsed
                                            break
                                    except (UnicodeDecodeError, ValueError):
                                        pass
                        t_search = t_hit + len(tag_token)
                if tax_bucket:
                    facts_out[tax_name] = tax_bucket

    return {"facts": facts_out}


def load_or_fetch_company_snapshot(
    *,
    symbol: str,
    cik: str | None = None,
    user_agent: str | None,
    limiter: SecPacingLimiter | None,
    snapshot_cache: JsonDocumentRepository[Any] | None = None,
    cache_only: bool = False,
    min_projection_version: int = 2,
    submissions: Any = None,
    transport: Any = None,
    stream_transport: Any = None,
    policy: Any = None,
) -> tuple[PeerCompanySnapshot | None, bool]:
    """Two-gate snapshot loader with per-CIK Singleflight and ``BoundedSemaphore(2)``.

    Returns ``(snapshot, cache_hit)``.
    """
    sym_u = symbol.strip().upper()
    if cache_only and snapshot_cache is not None:
        if cik:
            hit = snapshot_cache.get(f"snap:{cik10(cik)}")
            if isinstance(hit, Mapping):
                snap = PeerCompanySnapshot.model_validate(hit)
                if snap.projection_version >= min_projection_version:
                    return snap, True
            return None, False
        # Avoid full-table scan when ticker map already has CIK in memory
        with _TICKER_MAP_LOCK:
            cached_map = _TICKER_MAP_CACHE[1] if _TICKER_MAP_CACHE is not None else None
        if cached_map:
            t_hit = cached_map.get(sym_u.replace(".", "-")) or cached_map.get(sym_u)
            if t_hit is not None:
                hit = snapshot_cache.get(f"snap:{cik10(t_hit[0])}")
                if isinstance(hit, Mapping):
                    snap = PeerCompanySnapshot.model_validate(hit)
                    if snap.projection_version >= min_projection_version:
                        return snap, True
                return None, False
        # O(1) symbol -> cik pointer written on snapshot save
        sym_alias = snapshot_cache.get(f"sym:{sym_u}")
        if isinstance(sym_alias, Mapping) and isinstance(sym_alias.get("cik"), str):
            hit = snapshot_cache.get(f"snap:{cik10(sym_alias['cik'])}")
            if isinstance(hit, Mapping):
                snap = PeerCompanySnapshot.model_validate(hit)
                if snap.projection_version >= min_projection_version:
                    return snap, True
            return None, False
        # Never full-table scan Postgres in cache_only mode; allow lightweight scan only on small InMemory test repos
        from .storage import InMemoryDocumentRepository
        if isinstance(snapshot_cache, InMemoryDocumentRepository):
            for _k, val in snapshot_cache.items():
                if isinstance(val, Mapping) and str(val.get("symbol") or "").upper() == sym_u:
                    snap = PeerCompanySnapshot.model_validate(val)
                    if snap.projection_version >= min_projection_version:
                        return snap, True
        return None, False

    if cik is None:
        resolved_cik, _name = resolve_ticker_to_cik(
            sym_u,
            user_agent=user_agent,
            limiter=limiter,
            policy=policy,
            transport=transport,
        )
        cik_norm = cik10(resolved_cik)
    else:
        cik_norm = cik10(cik)

    cache_key = f"snap:{cik_norm}"

    # Gate 1: Lightweight submissions fingerprint check (reuse injected submissions or 20m TTL cache)
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
    periodic_rows = [
        r for r in subs.rows if r.form.upper() in ("10-Q", "10-K", "20-F", "10-Q/A", "10-K/A", "20-F/A")
    ]
    latest_row = periodic_rows[0] if periodic_rows else None
    latest_acc = latest_row.accession if latest_row else None
    latest_filed = latest_row.filed_at.isoformat() if latest_row else None

    if snapshot_cache is not None:
        cached_raw = snapshot_cache.get(cache_key)
        if isinstance(cached_raw, Mapping):
            cached_snap = PeerCompanySnapshot.model_validate(cached_raw)
            if cached_snap.projection_version >= _CURRENT_PROJECTION_VERSION and (
                latest_acc is None or cached_snap.latest_periodic_accession == latest_acc
            ):
                return cached_snap, True

    # Gate 2: per-CIK Singleflight + BoundedSemaphore(2) streaming companyfacts parse
    with _singleflight_lock(cik_norm):
        # Double-check cache after acquiring singleflight lock
        if snapshot_cache is not None:
            cached_raw = snapshot_cache.get(cache_key)
            if isinstance(cached_raw, Mapping):
                cached_snap = PeerCompanySnapshot.model_validate(cached_raw)
                if cached_snap.projection_version >= _CURRENT_PROJECTION_VERSION and (
                    latest_acc is None or cached_snap.latest_periodic_accession == latest_acc
                ):
                    return cached_snap, True

        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_norm}.json"
        from .fin_data.adapters.sec_events.pacing import sec_get_to_tempfile
        from .research_memory_gate import heavy_memory_section

        tmp_path: Path | None = None
        try:
            # Download streaming to disk OUTSIDE heavy_memory_section (uses only ~64KB buffer)
            # so concurrent SEC fetches do not serialize on memory parsing semaphore!
            tmp_path, _ct, _sha = sec_get_to_tempfile(
                url,
                accept="application/json",
                category="financials",
                user_agent=user_agent,
                limiter=limiter,
                policy=policy,
                transport=transport,
                stream_transport=stream_transport,
                max_bytes=60_000_000,
                suffix=".json",
            )
            with heavy_memory_section(), _PARSE_SEMAPHORE:
                cf_doc = extract_sliced_companyfacts_from_file(tmp_path)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        with _PARSE_SEMAPHORE:
            # Guard against SEC submissions-vs-companyfacts 15-120m indexing lag:
            # Verify latest_acc (or latest_row.period_of_report) actually appears in cf_doc before advancing latest_periodic_accession!
            eff_latest_acc = latest_acc
            eff_latest_filed = latest_filed
            pending_xbrl_acc: str | None = None
            if latest_acc is not None and isinstance(cf_doc, Mapping):
                latest_report_end = (
                    latest_row.period_of_report.isoformat()
                    if (latest_row is not None and getattr(latest_row, "period_of_report", None) is not None)
                    else None
                )
                found_acc = False
                newest_fact_acc: str | None = None
                newest_fact_filed: str = ""
                for _tax, tag_dict in (cf_doc.get("facts") or {}).items():
                    if not isinstance(tag_dict, Mapping):
                        continue
                    for _tag, tag_obj in tag_dict.items():
                        units = tag_obj.get("units") if isinstance(tag_obj, Mapping) else None
                        if not isinstance(units, Mapping):
                            continue
                        for _u, rows_u in units.items():
                            if not isinstance(rows_u, list):
                                continue
                            for r_u in rows_u:
                                if not isinstance(r_u, Mapping):
                                    continue
                                r_acc = str(r_u.get("accn") or "").strip()
                                r_end = str(r_u.get("end") or "").strip()
                                r_filed = str(r_u.get("filed") or "").strip()
                                if r_acc == latest_acc or (latest_report_end and r_end == latest_report_end and r_filed >= (latest_filed or "")):
                                    found_acc = True
                                if r_acc and r_filed > newest_fact_filed:
                                    newest_fact_filed = r_filed
                                    newest_fact_acc = r_acc
                if not found_acc and newest_fact_acc:
                    pending_xbrl_acc = latest_acc
                    eff_latest_acc = newest_fact_acc
                    eff_latest_filed = newest_fact_filed or latest_filed
                elif not found_acc and not newest_fact_acc:
                    pending_xbrl_acc = latest_acc
                    eff_latest_acc = None

            snap = project_companyfacts_to_snapshot(
                cik=cik_norm,
                symbol=sym_u,
                company_name=subs.company_name,
                sic=subs.sic,
                sic_description=getattr(subs, "sic_description", None),
                latest_periodic_accession=eff_latest_acc,
                latest_periodic_filed_at=eff_latest_filed,
                companyfacts_doc=cf_doc if isinstance(cf_doc, Mapping) else {},
            )
            if pending_xbrl_acc is not None:
                snap = snap.model_copy(update={"xbrl_indexing_pending_accession": pending_xbrl_acc})
            # Release cf_doc reference immediately upon leaving scope
            del cf_doc

        if snapshot_cache is not None:
            snapshot_cache.put(cache_key, snap.model_dump(mode="json"))
            snapshot_cache.put(f"sym:{sym_u}", {"cik": cik_norm, "symbol": sym_u})
        return snap, False


# --------------------------------------------------------------------------- Deterministic Financial & Valuation Math

def _rebuild_facts_dict(snap: PeerCompanySnapshot) -> dict[str, Any]:
    """Reconstruct minimal ``{taxonomy: {tag: {units: ...}}}`` dict for ``select_line()``."""
    tax_bucket: dict[str, Any] = {}
    for _line_name, tag_map in snap.facts_by_line.items():
        for tag, unit_map in tag_map.items():
            tax_bucket[tag] = {"units": unit_map}
    return {snap.taxonomy: tax_bucket}


def _compute_ttm_from_quarters(quarters: Sequence[Period]) -> tuple[Decimal | None, date | None]:
    """Sum 4 contiguous quarterly periods (spanning 340..385 days)."""
    if len(quarters) < 4:
        return None, None
    ordered = sorted(quarters, key=lambda p: p.end, reverse=True)
    for i in range(len(ordered) - 3):
        win = list(reversed(ordered[i : i + 4]))  # oldest -> newest
        if not all(q.start is not None for q in win):
            continue
        contiguous = all(
            abs(((win[j + 1].start - win[j].end).days) - 1) <= 6  # type: ignore[operator]
            for j in range(3)
        )
        total_days = (win[3].end - win[0].start).days  # type: ignore[operator]
        if contiguous and 340 <= total_days <= 385:
            return sum((q.value for q in win), Decimal(0)), win[3].end
    return None, None


def _compute_yoy_pct(cur: Decimal | None, prev: Decimal | None) -> Decimal | None:
    if cur is None or prev is None or prev == 0:
        return None
    if prev < 0:
        return None
    return ((cur - prev) / abs(prev)) * Decimal("100")


def _solve_reverse_dcf_cagr_pct(
    *,
    ev: Decimal,
    ttm_fcf: Decimal,
    wacc: Decimal = _REVERSE_DCF_WACC,
    terminal_g: Decimal = _REVERSE_DCF_TERMINAL_G,
    years: int = _REVERSE_DCF_YEARS,
) -> Decimal | None:
    """Solve 5-year implied FCF CAGR (%) via deterministic binary search (no SciPy)."""
    if ev <= 0 or ttm_fcf <= 0 or wacc <= terminal_g:
        return None

    def _pv(g: Decimal) -> Decimal:
        total = Decimal(0)
        fcf = ttm_fcf
        one_plus_wacc = Decimal(1) + wacc
        one_plus_g = Decimal(1) + g
        for yr in range(1, years + 1):
            fcf = fcf * one_plus_g
            total += fcf / (one_plus_wacc ** yr)
        tv = (fcf * (Decimal(1) + terminal_g)) / (wacc - terminal_g)
        total += tv / (one_plus_wacc ** years)
        return total

    lo, hi = Decimal("-0.50"), Decimal("1.50")
    if _pv(lo) > ev or _pv(hi) < ev:
        return None
    for _ in range(52):
        mid = (lo + hi) / Decimal(2)
        if _pv(mid) < ev:
            lo = mid
        else:
            hi = mid
    return ((lo + hi) / Decimal(2)) * Decimal("100")


def compute_company_metrics_from_snapshot(
    snap: PeerCompanySnapshot,
    *,
    price_usd: Decimal | None = None,
    price_as_of_date: str | None = None,
    price_source: str | None = None,
    as_of: date | None = None,
    fpi_meta: FpiAdrMeta | None = None,
    fx_to_usd: Decimal | None = None,
) -> PeerCompanyMetrics:
    """Compute all deterministic V2 metrics for one company from its lightweight snapshot."""
    sic_gate = evaluate_sec_sic_gate(snap.sic, snap.sic_description)
    facts_dict = _rebuild_facts_dict(snap)
    tax = snap.taxonomy

    def _sel(line_name: str, kind: str, n: int = 10) -> list[Period]:
        spec = STANDARD_LINES.get(line_name)
        if spec is None:
            return []
        return select_line(facts_dict, spec, kind, n, derive_q4=True, taxonomy=tax, as_of=as_of)

    notes: list[str] = []
    non_comp_reasons: list[str] = []

    if sic_gate.is_gated:
        non_comp_reasons.append(f"financial_sic_gated:{sic_gate.sector_category}")

    rev_q = _sel("revenue", "quarter", 10)
    rev_y = _sel("revenue", "year", 4)
    cogs_q = _sel("cost_of_revenue", "quarter", 10)
    cogs_y = _sel("cost_of_revenue", "year", 4)
    gp_q = _sel("gross_profit", "quarter", 10)
    gp_y = _sel("gross_profit", "year", 4)
    oi_q = _sel("operating_income", "quarter", 10)
    oi_y = _sel("operating_income", "year", 4)
    int_q = _sel("interest_expense", "quarter", 10)
    int_y = _sel("interest_expense", "year", 4)
    ni_q = _sel("net_income", "quarter", 10)
    ni_y = _sel("net_income", "year", 4)
    tax_q = _sel("income_tax", "quarter", 10)
    tax_y = _sel("income_tax", "year", 4)
    pt_q = _sel("profit_before_tax", "quarter", 10)
    pt_y = _sel("profit_before_tax", "year", 4)
    ocf_q = _sel("operating_cash_flow", "quarter", 10)
    ocf_y = _sel("operating_cash_flow", "year", 4)
    capex_q = _sel("capex", "quarter", 10)
    capex_y = _sel("capex", "year", 4)
    sbc_q = _sel("stock_based_compensation", "quarter", 10)
    sbc_y = _sel("stock_based_compensation", "year", 4)

    # Determine TTM vs FY (20-F FPI fallback)
    ttm_rev, rev_end = _compute_ttm_from_quarters(rev_q)
    period_basis: PeriodBasis = "TTM"
    fiscal_label: str | None = None
    if ttm_rev is not None and rev_end is not None:
        period_end = rev_end
        fiscal_label = f"TTM {rev_end.isoformat()}"
        ttm_gp, _ = _compute_ttm_from_quarters(gp_q)
        if ttm_gp is None:
            ttm_cogs, _ = _compute_ttm_from_quarters(cogs_q)
            if ttm_cogs is not None:
                ttm_gp = ttm_rev - ttm_cogs
        ttm_oi, _ = _compute_ttm_from_quarters(oi_q)
        ttm_int, _ = _compute_ttm_from_quarters(int_q)
        ttm_ni, _ = _compute_ttm_from_quarters(ni_q)
        ttm_tax, _ = _compute_ttm_from_quarters(tax_q)
        ttm_pt, _ = _compute_ttm_from_quarters(pt_q)
        ttm_ocf, _ = _compute_ttm_from_quarters(ocf_q)
        ttm_capex, _ = _compute_ttm_from_quarters(capex_q)
        ttm_sbc, _ = _compute_ttm_from_quarters(sbc_q)
        # Prior TTM (4 quarters shifted back by 4)
        prev_ttm_rev, _ = _compute_ttm_from_quarters(rev_q[4:]) if len(rev_q) >= 8 else (None, None)
        ttm_rev_yoy = _compute_yoy_pct(ttm_rev, prev_ttm_rev)
        if ttm_rev_yoy is None and len(rev_y) >= 2:
            ttm_rev_yoy = _compute_yoy_pct(rev_y[0].value, rev_y[1].value)
        latest_q_rev_yoy = _compute_yoy_pct(rev_q[0].value, rev_q[4].value) if len(rev_q) >= 5 else None
    elif rev_y:
        period_basis = "FY"
        period_end = rev_y[0].end
        fiscal_label = rev_y[0].label or f"FY {period_end.isoformat()}"
        ttm_rev = rev_y[0].value
        ttm_gp = gp_y[0].value if gp_y else None
        if ttm_gp is None and cogs_y:
            ttm_gp = ttm_rev - cogs_y[0].value
        ttm_oi = oi_y[0].value if oi_y else None
        ttm_int = int_y[0].value if int_y else None
        ttm_ni = ni_y[0].value if ni_y else None
        ttm_tax = tax_y[0].value if tax_y else None
        ttm_pt = pt_y[0].value if pt_y else None
        ttm_ocf = ocf_y[0].value if ocf_y else None
        ttm_capex = capex_y[0].value if capex_y else None
        ttm_sbc = sbc_y[0].value if sbc_y else None
        ttm_rev_yoy = _compute_yoy_pct(rev_y[0].value, rev_y[1].value) if len(rev_y) >= 2 else None
        latest_q_rev_yoy = None
        notes.append("Quarterly XBRL unavailable; using latest annual (FY / 20-F) figures.")
        non_comp_reasons.append("fy_only_no_quarterly_xbrl")
    else:
        period_basis = "partial"
        period_end = None
        ttm_gp = ttm_oi = ttm_int = ttm_ni = ttm_tax = ttm_pt = ttm_ocf = ttm_capex = ttm_sbc = None
        ttm_rev_yoy = latest_q_rev_yoy = None
        non_comp_reasons.append("missing_revenue_xbrl")

    rev_accel = (latest_q_rev_yoy - ttm_rev_yoy) if (latest_q_rev_yoy is not None and ttm_rev_yoy is not None) else None

    # Dimensionless profitability & cash ratios (valid even for non-USD FPI!)
    gross_margin = (ttm_gp / ttm_rev * Decimal(100)) if (ttm_gp is not None and ttm_rev and ttm_rev > 0) else None
    op_margin = (ttm_oi / ttm_rev * Decimal(100)) if (ttm_oi is not None and ttm_rev and ttm_rev > 0) else None
    net_margin = (ttm_ni / ttm_rev * Decimal(100)) if (ttm_ni is not None and ttm_rev and ttm_rev > 0) else None

    ttm_fcf: Decimal | None = None
    if ttm_ocf is not None and ttm_capex is not None:
        # CapEx in SEC XBRL is positive outflow (PaymentsToAcquirePropertyPlantAndEquipment)
        ttm_fcf = ttm_ocf - abs(ttm_capex)
    fcf_margin = (ttm_fcf / ttm_rev * Decimal(100)) if (ttm_fcf is not None and ttm_rev and ttm_rev > 0) else None
    ocf_to_ni = (ttm_ocf / ttm_ni) if (ttm_ocf is not None and ttm_ni is not None and ttm_ni > 0) else None
    sbc_pct_fcf = (
        (abs(ttm_sbc) / ttm_fcf * Decimal(100))
        if (ttm_sbc is not None and ttm_fcf is not None and ttm_fcf > 0)
        else None
    )

    # Balance sheet (point-in-time)
    def _latest_bs(name: str) -> list[Period]:
        q_rows = _sel(name, "quarter", 6)
        return q_rows if q_rows else _sel(name, "year", 4)

    cash_rows = _latest_bs("cash")
    sti_rows = _latest_bs("short_term_investments")
    st_debt_rows = _latest_bs("short_term_debt")
    lt_debt_rows = _latest_bs("long_term_debt")
    assets_rows = _latest_bs("total_assets")
    liab_rows = _latest_bs("total_liabilities")
    eq_rows = _latest_bs("equity")
    nci_rows = _latest_bs("non_controlling_interests")

    cash_v = cash_rows[0].value if cash_rows else None
    sti_v = sti_rows[0].value if sti_rows else None
    total_cash = (
        (cash_v or Decimal(0)) + (sti_v or Decimal(0))
        if (cash_v is not None or sti_v is not None)
        else None
    )
    st_d = st_debt_rows[0].value if st_debt_rows else None
    lt_d = lt_debt_rows[0].value if lt_debt_rows else None
    if st_d is not None or lt_d is not None:
        total_debt: Decimal | None = (st_d or Decimal(0)) + (lt_d or Decimal(0))
    elif assets_rows and liab_rows:
        total_debt = Decimal(0)
        notes.append("Debt tags absent on complete balance sheet; treated as 0.")
    else:
        total_debt = None

    net_cash = (total_cash - total_debt) if (total_cash is not None and total_debt is not None) else None
    equity_v = eq_rows[0].value if eq_rows else None
    nci_v = nci_rows[0].value if nci_rows else Decimal(0)

    # Derive EBIT = profit_before_tax + interest_expense when OperatingIncomeLoss is omitted (common in US pharma)
    if not sic_gate.is_gated and ttm_oi is None and ttm_pt is not None:
        if ttm_int is not None:
            ttm_oi = ttm_pt + abs(ttm_int)
            notes.append("operating_income derived from profit_before_tax + interest_expense.")
        elif total_debt is not None and total_debt == 0:
            ttm_oi = ttm_pt
            notes.append("operating_income derived from profit_before_tax (debt-free).")
        else:
            ttm_oi = ttm_pt
            notes.append("EBIT approximated by profit_before_tax (interest_expense unavailable).")
        if op_margin is None and ttm_oi is not None and ttm_rev and ttm_rev > 0:
            op_margin = ttm_oi / ttm_rev * Decimal(100)

    # Pure ROIC with [0%, 35%] tax clamp and Invested Capital <= 0 guard
    ttm_roic: Decimal | None = None
    if not sic_gate.is_gated and ttm_oi is not None and total_debt is not None and equity_v is not None and total_cash is not None:
        if ttm_tax is not None and ttm_pt is not None and ttm_pt > 0:
            raw_eff = ttm_tax / ttm_pt
            eff_tax = min(Decimal("0.35"), max(Decimal("0.00"), raw_eff))
        else:
            eff_tax = Decimal("0.21")
            notes.append("ROIC tax rate defaulted to 21% statutory benchmark.")
        nopat = ttm_oi * (Decimal(1) - eff_tax)
        invested_capital = total_debt + equity_v - total_cash
        if invested_capital > 0:
            ttm_roic = (nopat / invested_capital) * Decimal(100)
        else:
            notes.append("ROIC unavailable: Invested Capital <= 0 (cash-rich or negative-equity capital structure).")

    # Quality light (4-state: financial SIC -> not_applicable ⚪)
    quality_notes: list[str] = []
    if sic_gate.is_gated:
        quality_light: QualityLight = "not_applicable"
        quality_notes.append(f"Financial sector ({sic_gate.sector_category}) requires specialized diagnostics (V4a).")
    else:
        red_flags = 0
        yellow_flags = 0
        if ttm_fcf is not None and ttm_fcf <= 0:
            red_flags += 1
            quality_notes.append("TTM Free Cash Flow is negative.")
        if ocf_to_ni is not None and ocf_to_ni < Decimal("0.50"):
            red_flags += 1
            quality_notes.append(f"OCF/NI severely weak at {ocf_to_ni:.3f}x (< 0.50x).")
        elif ocf_to_ni is not None and ocf_to_ni < Decimal("0.70"):
            if fcf_margin is not None and fcf_margin >= Decimal("15"):
                yellow_flags += 1
                quality_notes.append(
                    f"OCF/NI {ocf_to_ni:.3f}x (< 0.70x), cushioned by strong {fcf_margin:.1f}% FCF margin."
                )
            else:
                red_flags += 1
                quality_notes.append(f"OCF/NI weak at {ocf_to_ni:.3f}x (< 0.70x).")
        elif ocf_to_ni is not None and ocf_to_ni < Decimal("0.90"):
            yellow_flags += 1
            quality_notes.append(f"OCF/NI moderate at {ocf_to_ni:.3f}x (< 0.90x).")
        if sbc_pct_fcf is not None and sbc_pct_fcf > Decimal("35"):
            yellow_flags += 1
            quality_notes.append(f"SBC consumes {sbc_pct_fcf:.1f}% of TTM FCF.")
        if rev_accel is not None and rev_accel < Decimal("-5"):
            yellow_flags += 1
            quality_notes.append(f"Latest-quarter revenue decelerated {rev_accel:.1f}pp vs TTM.")
        quality_light = "red" if red_flags > 0 else ("yellow" if yellow_flags > 0 else "green")

    # Shares (diluted with loss-quarter basic fallback + DEI dual-class >15% divergence guard)
    sh_dil = _sel("shares_diluted", "quarter", 4) or _sel("shares_diluted", "year", 2)
    sh_bas = _sel("shares_basic", "quarter", 4) or _sel("shares_basic", "year", 2)
    shares_v: Decimal | None = None
    if sh_dil:
        shares_v = sh_dil[0].value
    elif sh_bas:
        shares_v = sh_bas[0].value
        notes.append("shares_diluted unavailable (e.g. anti-dilutive loss quarter); fell back to shares_basic.")

    # Cross-check vs DEI EntityCommonStockSharesOutstanding
    if shares_v is not None and shares_v > 0 and snap.dei_shares_outstanding:
        valid_dei = []
        for row in snap.dei_shares_outstanding:
            f_str = row.get("filed")
            if as_of is not None and isinstance(f_str, str):
                try:
                    if date.fromisoformat(f_str[:10]) > as_of:
                        continue
                except ValueError:
                    pass
            dv = _dec(row.get("val"))
            if dv is not None and dv > 0:
                valid_dei.append((str(row.get("end") or ""), dv))
        if valid_dei:
            valid_dei.sort(key=lambda x: x[0], reverse=True)
            dei_v = valid_dei[0][1]
            div_ratio = abs(dei_v - shares_v) / shares_v
            if div_ratio > Decimal("0.15"):
                notes.append(
                    f"share_count_divergence: DEI shares ({dei_v:,.0f}) differs {div_ratio * 100:.1f}% from income-statement shares ({shares_v:,.0f}); using income-statement diluted/basic count."
                )

    # FPI / non-USD currency & ADR ratio gate
    is_non_usd = snap.currency.upper() != "USD"
    currency_factor: Decimal | None = Decimal(1) if not is_non_usd else fx_to_usd
    adr_ratio = _dec(fpi_meta.adr_ratio) if fpi_meta else Decimal(1)
    if adr_ratio is None or adr_ratio <= 0:
        adr_ratio = Decimal(1)

    if is_non_usd:
        non_comp_reasons.append(f"non_usd_currency:{snap.currency}")
        if currency_factor is None:
            notes.append(
                f"currency_adr_unverified: reporting currency is {snap.currency} without verified FX rate; USD valuation multiples withheld to prevent silent distortion."
            )

    # Convert monetary scale to USD when verified
    def _to_usd(v: Decimal | None) -> Decimal | None:
        if v is None or currency_factor is None:
            return None
        return v * currency_factor

    mcap_usd: Decimal | None = None
    if price_usd is not None and price_usd > 0 and shares_v is not None and shares_v > 0:
        if not is_non_usd:
            mcap_usd = price_usd * shares_v
        elif currency_factor is not None:
            # ADR price * (ordinary_shares / adr_ratio)
            mcap_usd = price_usd * (shares_v / adr_ratio)
            notes.append(f"ADR market cap computed using adr_ratio={adr_ratio} and verified FX={currency_factor}.")

    ev_usd: Decimal | None = None
    if not sic_gate.is_gated and mcap_usd is not None and total_debt is not None and total_cash is not None and currency_factor is not None:
        ev_usd = mcap_usd + _to_usd(total_debt) + _to_usd(nci_v) - _to_usd(total_cash)  # type: ignore[operator]

    ttm_rev_usd = _to_usd(ttm_rev)
    ttm_oi_usd = _to_usd(ttm_oi)
    ttm_ni_usd = _to_usd(ttm_ni)
    ttm_fcf_usd = _to_usd(ttm_fcf)
    ttm_sbc_usd = _to_usd(ttm_sbc)
    net_cash_usd = _to_usd(net_cash)

    pe_ttm = (mcap_usd / ttm_ni_usd) if (mcap_usd is not None and ttm_ni_usd is not None and ttm_ni_usd > 0) else None
    ev_sales = (ev_usd / ttm_rev_usd) if (ev_usd is not None and ev_usd > 0 and ttm_rev_usd is not None and ttm_rev_usd > 0) else None
    ev_ebit = (ev_usd / ttm_oi_usd) if (ev_usd is not None and ev_usd > 0 and ttm_oi_usd is not None and ttm_oi_usd > 0) else None
    ev_fcf = (ev_usd / ttm_fcf_usd) if (ev_usd is not None and ev_usd > 0 and ttm_fcf_usd is not None and ttm_fcf_usd > 0) else None

    fcf_ex_sbc_usd = (ttm_fcf_usd - abs(ttm_sbc_usd)) if (ttm_fcf_usd is not None and ttm_sbc_usd is not None) else None
    ev_fcf_ex_sbc = (
        (ev_usd / fcf_ex_sbc_usd)
        if (ev_usd is not None and ev_usd > 0 and fcf_ex_sbc_usd is not None and fcf_ex_sbc_usd > 0)
        else None
    )

    rdcf_cagr: Decimal | None = None
    if not sic_gate.is_gated and ev_usd is not None and ttm_fcf_usd is not None:
        if ttm_fcf_usd <= 0:
            notes.append("Reverse-DCF unavailable: TTM FCF <= 0.")
        else:
            rdcf_cagr = _solve_reverse_dcf_cagr_pct(ev=ev_usd, ttm_fcf=ttm_fcf_usd)

    if price_usd is None:
        notes.append("quote_unavailable: market price not provided; valuation multiples omitted.")

    return PeerCompanyMetrics(
        symbol=snap.symbol,
        cik=snap.cik,
        company_name=snap.company_name,
        sic=snap.sic,
        sector_category=sic_gate.sector_category,
        is_financial_gated=sic_gate.is_gated,
        is_comparable_for_median=len(non_comp_reasons) == 0,
        non_comparable_reasons=non_comp_reasons,
        period_end_date=period_end.isoformat() if period_end else None,
        fiscal_period_label=fiscal_label,
        period_basis=period_basis,
        accounting_standard=snap.taxonomy,
        currency=snap.currency,
        price_usd=_fmt_dec(price_usd, 2),
        price_as_of_date=price_as_of_date,
        price_source=price_source,
        market_cap_usd=_fmt_dec(mcap_usd, 0),
        enterprise_value_usd=_fmt_dec(ev_usd, 0),
        net_cash_usd=_fmt_dec(net_cash_usd, 0),
        ttm_revenue_usd=_fmt_dec(ttm_rev_usd, 0),
        ttm_fcf_usd=_fmt_dec(ttm_fcf_usd, 0),
        ttm_revenue_yoy_pct=_fmt_dec(ttm_rev_yoy, 2),
        latest_q_revenue_yoy_pct=_fmt_dec(latest_q_rev_yoy, 2),
        revenue_accel_pp=_fmt_dec(rev_accel, 2),
        ttm_gross_margin_pct=_fmt_dec(gross_margin, 2),
        ttm_operating_margin_pct=_fmt_dec(op_margin, 2),
        ttm_net_margin_pct=_fmt_dec(net_margin, 2),
        ttm_roic_pct=_fmt_dec(ttm_roic, 2),
        ttm_fcf_margin_pct=_fmt_dec(fcf_margin, 2),
        ocf_to_ni_ratio=_fmt_dec(ocf_to_ni, 2),
        sbc_pct_of_fcf=_fmt_dec(sbc_pct_fcf, 1),
        quality_light=quality_light,
        quality_notes=quality_notes,
        pe_ttm=_fmt_dec(pe_ttm, 2),
        ev_to_sales_ttm=_fmt_dec(ev_sales, 2),
        ev_to_ebit_ttm=_fmt_dec(ev_ebit, 2),
        ev_to_fcf_ttm=_fmt_dec(ev_fcf, 2),
        ev_to_fcf_ex_sbc_ttm=_fmt_dec(ev_fcf_ex_sbc, 2),
        reverse_dcf_implied_5y_fcf_cagr_pct=_fmt_dec(rdcf_cagr, 2),
        notes=notes,
    )


# --------------------------------------------------------------------------- Matrix Assembly, Medians, Warnings & Ranks

_HIGHER_IS_BETTER: tuple[str, ...] = (
    "ttm_revenue_yoy_pct",
    "latest_q_revenue_yoy_pct",
    "ttm_gross_margin_pct",
    "ttm_operating_margin_pct",
    "ttm_fcf_margin_pct",
    "ttm_roic_pct",
    "ocf_to_ni_ratio",
)
_LOWER_IS_CHEAPER: tuple[str, ...] = (
    "pe_ttm",
    "ev_to_sales_ttm",
    "ev_to_ebit_ttm",
    "ev_to_fcf_ttm",
    "ev_to_fcf_ex_sbc_ttm",
    "reverse_dcf_implied_5y_fcf_cagr_pct",
)


def _median_decimal(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / Decimal(2)


def assemble_peer_matrix(
    *,
    primary_symbol: str,
    rows: Sequence[PeerCompanyMetrics],
    as_of: date | None = None,
    exclude_non_comparable_from_median: bool = True,
    cache_hits: int = 0,
    cache_misses: int = 0,
    elapsed_ms: int = 0,
) -> PeerComparisonMatrix:
    """Assemble medians, comparability warnings, ranks, spreads, and 1-line coordinate summary."""
    prim_u = primary_symbol.strip().upper()
    warnings: list[ComparabilityWarning] = []

    # 1. Period end date spread > 45 days
    dated = [(r.symbol, date.fromisoformat(r.period_end_date)) for r in rows if r.period_end_date]
    if len(dated) >= 2:
        min_sym, min_d = min(dated, key=lambda x: x[1])
        max_sym, max_d = max(dated, key=lambda x: x[1])
        gap_days = (max_d - min_d).days
        if gap_days > 45:
            warnings.append(
                ComparabilityWarning(
                    code="fiscal_calendar_mismatch",
                    severity="warning",
                    symbols=[min_sym, max_sym],
                    message=(
                        f"Fiscal period end dates span {gap_days} days "
                        f"({min_sym}: {min_d.isoformat()} vs {max_sym}: {max_d.isoformat()}); "
                        "raw SEC periods preserved without synthetic calendarization."
                    ),
                )
            )

    # 2. Cross-standard (US-GAAP vs IFRS) or non-USD currency
    non_gaap = [r.symbol for r in rows if r.accounting_standard != "us-gaap"]
    non_usd = [r.symbol for r in rows if r.currency.upper() != "USD"]
    if non_gaap or non_usd:
        warnings.append(
            ComparabilityWarning(
                code="accounting_or_currency_mismatch",
                severity="warning",
                symbols=sorted(set(non_gaap) | set(non_usd)),
                message=(
                    f"Peer group mixes accounting standards/currencies ({', '.join(sorted(set(non_gaap) | set(non_usd)))}). "
                    "Dimensionless margins/growth are preserved; unverified cross-currency multiples are withheld."
                ),
            )
        )

    # 3. Financial SIC mixed with non-financial peers
    fin_syms = [r.symbol for r in rows if r.is_financial_gated]
    non_fin_syms = [r.symbol for r in rows if not r.is_financial_gated]
    if fin_syms and non_fin_syms:
        warnings.append(
            ComparabilityWarning(
                code="financial_sic_mixed",
                severity="error",
                symbols=fin_syms,
                message=(
                    f"Financial/bank/REIT issuer(s) ({', '.join(fin_syms)}) mixed with non-financial peers; "
                    "industrial EV, FCF, and Reverse-DCF are disabled for financial issuers."
                ),
            )
        )

    # 4. Share count divergence notes
    div_syms = [r.symbol for r in rows if any("share_count_divergence" in n for n in r.notes)]
    if div_syms:
        warnings.append(
            ComparabilityWarning(
                code="share_count_divergence",
                severity="info",
                symbols=div_syms,
                message=f"DEI vs income-statement share count diverges >15% for {', '.join(div_syms)} (dual-class/split); using income-statement diluted/basic count.",
            )
        )

    # Compute peer medians (respecting exclude_non_comparable_from_median)
    median_pool = [r for r in rows if (r.is_comparable_for_median or not exclude_non_comparable_from_median)]
    if not median_pool:
        median_pool = list(rows)

    all_metric_keys = (*_HIGHER_IS_BETTER, *_LOWER_IS_CHEAPER)
    medians_dec: dict[str, Decimal | None] = {}
    medians_str: dict[str, str | None] = {}
    for mk in all_metric_keys:
        vals = [d for r in median_pool if (d := _dec(getattr(r, mk, None))) is not None]
        med = _median_decimal(vals)
        medians_dec[mk] = med
        medians_str[mk] = _fmt_dec(med, 2)

    # Compute per-company ranks & vs_median spreads
    enriched_rows: list[PeerCompanyMetrics] = []
    for r in rows:
        ranks: dict[str, int] = {}
        vs_med: dict[str, str] = {}
        for mk in all_metric_keys:
            rv = _dec(getattr(r, mk, None))
            if rv is None:
                continue
            peer_vals = [
                (p.symbol, d)
                for p in rows
                if (d := _dec(getattr(p, mk, None))) is not None
            ]
            reverse = mk in _HIGHER_IS_BETTER
            peer_vals.sort(key=lambda x: x[1], reverse=reverse)
            for idx, (sym_k, _v) in enumerate(peer_vals, 1):
                if sym_k == r.symbol:
                    ranks[mk] = idx
                    break
            med = medians_dec.get(mk)
            if med is not None:
                diff = rv - med
                vs_med[mk] = f"{diff:+.2f}"
        enriched_rows.append(r.model_copy(update={"ranks": ranks, "vs_median": vs_med}))

    # Build 1-line primary coordinate summary & stable Direction B WatchTrigger metric_keys
    prim_row = next((r for r in enriched_rows if r.symbol == prim_u), enriched_rows[0] if enriched_rows else None)
    coord_bits: list[str] = []
    trigger_metrics: dict[str, str | None] = {
        "peer_reverse_dcf_spread_bps": None,
        "peer_revenue_yoy_rank": None,
        "peer_fcf_margin_vs_median_pp": None,
    }
    if prim_row is not None:
        n_peers = len(enriched_rows)
        if "ttm_revenue_yoy_pct" in prim_row.ranks:
            rk = prim_row.ranks["ttm_revenue_yoy_pct"]
            trigger_metrics["peer_revenue_yoy_rank"] = str(rk)
            coord_bits.append(f"TTM增速第 {rk}/{n_peers}（{prim_row.ttm_revenue_yoy_pct}%）")
        if "ttm_fcf_margin_pct" in prim_row.ranks:
            rk_f = prim_row.ranks["ttm_fcf_margin_pct"]
            vs_f = prim_row.vs_median.get("ttm_fcf_margin_pct")
            trigger_metrics["peer_fcf_margin_vs_median_pp"] = vs_f
            coord_bits.append(f"FCF利润率第 {rk_f}/{n_peers}（{prim_row.ttm_fcf_margin_pct}%）")
        if "reverse_dcf_implied_5y_fcf_cagr_pct" in prim_row.vs_median:
            spread_pp = _dec(prim_row.vs_median["reverse_dcf_implied_5y_fcf_cagr_pct"])
            if spread_pp is not None:
                bps = (spread_pp * Decimal(100)).quantize(Decimal("1"))
                trigger_metrics["peer_reverse_dcf_spread_bps"] = format(bps, "f")
                label = "溢价" if bps >= 0 else "折价"
                coord_bits.append(f"Reverse-DCF 隐含增速较中位数{label} {bps:+} bps")
    coord_summary = " · ".join(coord_bits) if coord_bits else f"{prim_u} 同业坐标已计算（{len(enriched_rows)} 家）"

    return PeerComparisonMatrix(
        primary_symbol=prim_u,
        as_of=as_of.isoformat() if as_of else None,
        exclude_non_comparable_from_median=exclude_non_comparable_from_median,
        companies=enriched_rows,
        peer_medians=medians_str,
        comparability_warnings=warnings,
        primary_coordinate_summary=coord_summary,
        primary_watch_trigger_metrics=trigger_metrics,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
        elapsed_ms=elapsed_ms,
    )


# --------------------------------------------------------------------------- Layer 2 Optional AI Attribution Brief

def _verify_cited_metrics(
    cited: Sequence[PeerCitedMetric],
    matrix: PeerComparisonMatrix,
) -> tuple[list[PeerCitedMetric], int]:
    """Deterministically verify every LLM-cited metric against the Layer 1 matrix."""
    by_sym = {c.symbol.upper(): c for c in matrix.companies}
    verified_list: list[PeerCitedMetric] = []
    ok_count = 0
    for cm in cited:
        sym_key = cm.symbol.strip().upper()
        if sym_key in ("PEER_MEDIAN", "MEDIAN"):
            actual_raw = matrix.peer_medians.get(cm.metric_key)
        else:
            row = by_sym.get(sym_key)
            actual_raw = getattr(row, cm.metric_key, None) if row is not None else None
        ok = False
        if actual_raw is not None:
            a_dec = _dec(actual_raw)
            c_dec = _dec(cm.value)
            if a_dec is not None and c_dec is not None:
                ok = abs(a_dec - c_dec) <= Decimal("0.15")
            else:
                ok = str(actual_raw).strip() == str(cm.value).strip()
        if ok:
            ok_count += 1
        verified_list.append(cm.model_copy(update={"verified": ok}))
    return verified_list, ok_count


def generate_peer_attribution_brief(
    matrix: PeerComparisonMatrix,
    *,
    runner: Any,
    slot: str = "deep_reasoner",
    model_name: str | None = None,
    max_repairs: int = 1,
) -> PeerAttributionBrief:
    """On-demand single-turn LLM attribution grounded strictly in the Layer 1 matrix."""
    compact_payload = {
        "primary_symbol": matrix.primary_symbol,
        "primary_coordinate_summary": matrix.primary_coordinate_summary,
        "reverse_dcf_assumptions": matrix.reverse_dcf_assumptions,
        "comparability_warnings": [
            {"code": w.code, "severity": w.severity, "symbols": w.symbols, "message": w.message}
            for w in matrix.comparability_warnings
        ],
        "peer_medians": {k: v for k, v in matrix.peer_medians.items() if v is not None},
        "companies": [
            {
                "symbol": c.symbol,
                "period_end_date": c.period_end_date,
                "period_basis": c.period_basis,
                "accounting_standard": c.accounting_standard,
                "currency": c.currency,
                "quality_light": c.quality_light,
                "is_financial_gated": c.is_financial_gated,
                "ttm_revenue_yoy_pct": c.ttm_revenue_yoy_pct,
                "latest_q_revenue_yoy_pct": c.latest_q_revenue_yoy_pct,
                "ttm_gross_margin_pct": c.ttm_gross_margin_pct,
                "ttm_operating_margin_pct": c.ttm_operating_margin_pct,
                "ttm_fcf_margin_pct": c.ttm_fcf_margin_pct,
                "ttm_roic_pct": c.ttm_roic_pct,
                "sbc_pct_of_fcf": c.sbc_pct_of_fcf,
                "pe_ttm": c.pe_ttm,
                "ev_to_sales_ttm": c.ev_to_sales_ttm,
                "ev_to_ebit_ttm": c.ev_to_ebit_ttm,
                "ev_to_fcf_ttm": c.ev_to_fcf_ttm,
                "ev_to_fcf_ex_sbc_ttm": c.ev_to_fcf_ex_sbc_ttm,
                "reverse_dcf_implied_5y_fcf_cagr_pct": c.reverse_dcf_implied_5y_fcf_cagr_pct,
            }
            for c in matrix.companies
        ],
    }
    matrix_json = json.dumps(compact_payload, ensure_ascii=False, separators=(",", ":"))
    system_prompt = (
        "You are a buy-side Relative Valuation & Peer Comparison Analyst.\n"
        "Analyze ONLY the deterministic Layer 1 PeerComparisonMatrix JSON provided below.\n"
        "NEVER invent financial figures, consensus estimates, or stock prices not present in the matrix.\n"
        "If comparability_warnings exist (e.g. fiscal mismatch, IFRS/non-USD, financial SIC, negative FCF), explicitly note them.\n"
        "Keep the response concise and sharp. For EVERY key figure you cite (3-8 metrics), include an exact entry in `cited_metrics` with the exact `symbol`, `metric_key`, and numeric `value` from the matrix.\n\n"
        "Output ONLY valid JSON matching `_LlmPeerBriefPayload` (`extra='forbid'`):\n"
        "{\n"
        "  \"summary\": \"2-3 sentence executive takeaway on where primary_symbol stands vs peers\",\n"
        "  \"valuation_vs_fundamentals\": \"Does relative valuation / Reverse-DCF spread align with growth, margin, ROIC, and FCF quality?\",\n"
        "  \"relative_risks\": [\"concise relative risk bullet 1\", \"bullet 2\"],\n"
        "  \"cited_metrics\": [{\"symbol\": \"NVDA\", \"metric_key\": \"ttm_gross_margin_pct\", \"value\": \"74.67\"}]\n"
        "}"
    )
    user_prompt = (
        f"PRIMARY_SYMBOL: {matrix.primary_symbol}\n"
        f"LAYER1_DETERMINISTIC_PEER_MATRIX:\n{matrix_json}"
    )

    usage_totals = {"tokens": 0}
    conv_id = f"peer_brief_{matrix.comparison_id}_{uuid4().hex[:6]}"

    def _invoke_fn(messages: list[dict[str, Any]], attempt_idx: int) -> Any:
        req = SimpleNamespace(
            stage="peer_attribution_brief",
            role="peer_attribution_analyst",
            slot=slot,
            system_prompt=system_prompt,
            user_prompt=messages[-1]["content"] if messages else user_prompt,
            messages=messages,
            tools=(),
            turn_index=attempt_idx,
            conversation_id=conv_id,
            response_schema=None,
            response_schema_name=None,
            max_output_tokens=None,
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
        model_cls=_LlmPeerBriefPayload,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_repairs=max_repairs,
    )
    verified_metrics, ok_count = _verify_cited_metrics(parsed.cited_metrics, matrix)
    return PeerAttributionBrief(
        comparison_id=matrix.comparison_id,
        primary_symbol=matrix.primary_symbol,
        summary=parsed.summary,
        valuation_vs_fundamentals=parsed.valuation_vs_fundamentals,
        relative_risks=list(parsed.relative_risks)[:6],
        cited_metrics=verified_metrics,
        verified_metric_count=ok_count,
        total_cited_metric_count=len(verified_metrics),
        tokens_used=usage_totals["tokens"],
        model_used=model_name,
    )
