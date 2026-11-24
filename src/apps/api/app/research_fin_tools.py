"""Model-visible financial data tools (resolve_entity, market_data, company_financials, filings).

Thin runtime layer over ``fin_data``: validates requests, resolves lookbacks and entities, gates every
external call (run budget, abuse quota, SEC global pacing, per-account Twelve Data bucket), persists
results as research sources and returns W6-shaped results. It holds no per-run mutable state; the
runtime keeps the entity candidate cache in its checkpoint and passes it in.

Credentials: market prices use the account's own Twelve Data key (``credential_owner='user_byok'``) or, only when
the fin_data policy explicitly allows it, the platform key (``'platform'``); the caller resolves which one.
The default policy denies the platform key.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import math
import re
from datetime import date, datetime, timezone
from time import monotonic
from typing import Any, Callable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .earnings_external_data import (
    ExternalEarningsService,
    FMP_ALLOW_PLATFORM_KEY_ENV,
    FMP_API_KEY_ENV,
    redact_secrets,
)
from .fin_data import cn as cn_helpers
from .fin_data.adapters.baostock import BaoStockAdapter, default_runner as baostock_default_runner
from .fin_data.adapters.fmp import FmpEarningsAdapter
from .fin_data.adapters.sec_deep import (
    DualPeriodEarningsRelease,
    FilingRedlineReport,
    FilingSectionsBundle,
    FilingTableCatalog,
    FilingTableData,
    cik_scoped_efts_search,
    fetch_filing_table,
    sec_earnings_release as sec_deep_earnings_release,
    sec_filing_sections as sec_deep_filing_sections,
    sec_filing_tables as sec_deep_filing_tables,
    sec_redline_diff as sec_deep_redline_diff,
)
from .fin_data.adapters.sec_edgar import STANDARD_LINES, SecEdgarAdapter, cik10
from .fin_data.adapters.sec_events import (
    EventCategory,
    SecPacingError,
    SecPacingLimiter,
    scan_sec_material_events,
)
from .fin_data.adapters.tushare import TushareAdapter
from .fin_data.adapters.twelve_data import TwelveDataAdapter
from .fin_data.adapters.yahoo import YahooAdapter
from .fin_data.reachability import UNREACHABLE_REASON, ReachabilityProbe, default_probe, unreachable_message
from .fin_data.entity import MARKETS, parse_entity_id, resolve_cn, resolve_entity as _resolve_entity, wants_cn
from .fin_data.models import INTERVALS, INTRADAY_INTERVALS, Permissions, ToolResult, normalize_exchange
from .fin_data.policy import Denied, FinDataPolicy, load_policy
from .fin_data.ratelimit import RateDecision, default_limiter
from .research_clock import exchange_session
from .research_earnings_diagnostics import (
    EarningsQualityRequest,
    GuidanceAndNonGaapRequest,
    ValuationRequest,
)
from .research_historical_prices import LOOKBACK_VALUES, resolve_lookback
from .research_sec import SecFailure, validate_user_agent
from .research_source_catalog import canonical_source_url
from .research_sources import ResearchSource, SourceInput, SourcePermissions

MARKET_KEY_REQUIRED_MESSAGE = (
    'No Twelve Data API key is saved for this account, so market price data is unavailable. Tell the user to add '
    'their own Twelve Data key in Settings (Market data) to enable it. Meanwhile use prices stated in filings or '
    'news and label them in the report as not coming from market data.')
UNKNOWN_READ_MESSAGE = ('The outcome of this read-only request is unknown (no confirmed response) and it was not retried '
                        'automatically. Use another source or report the gap.')
LINE_NAMES = tuple(STANDARD_LINES)
PERIODS = {'last_8_quarters': ('quarter', 8), 'last_4_quarters': ('quarter', 4),
           'last_3_fiscal_years': ('year', 3), 'last_5_fiscal_years': ('year', 5)}
US_EXCHANGES = MARKETS['US']
SEC_MIN_REMAINING_SECONDS = 15.0
BYOK_MAX_TIMEOUT_SECONDS = 8.0
TUSHARE_TIMEOUT_SECONDS = 15.0
BAOSTOCK_TIMEOUT_SECONDS = 15.0  # second line of defence behind the reachability probe
YAHOO_TIMEOUT_SECONDS = 10.0
CN_KEY_MESSAGE = (
    'No A-share data source is available: the user can add their own Tushare token in Settings (Market data), '
    'or the operator can enable the keyless BaoStock source. Meanwhile use prices and figures stated in the issuer\'s '
    'reports or news and label them as not coming from market data.')
CN_NAME_LOOKUP_MESSAGE = (
    'A-share name lookup needs a Tushare or BaoStock listing, and none is reachable or enabled from this server '
    '(they are reachable only from mainland China networks or through a proxy). Pass the 6-digit code (for example '
    '600519 or 000001.SZ) to resolve_entity or market_data, or use issuer_documents / Wikidata to find the listing code.')
_AUTO = object()


# --------------------------------------------------------------------------- requests
class ResolveEntityRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=100, description='Company or security name, ticker, CIK or ISIN.')
    market: Literal['US', 'CN', 'HK', 'JP', 'KR', 'TW', 'GB', 'IN', 'CA', 'AU', 'DE'] | None = Field(
        default=None, description='Optional market to narrow candidates.')
    asset_type: Literal['equity', 'etf', 'index', 'fund', 'crypto', 'fx', 'other'] | None = None
    rationale: str = Field(min_length=1)


class MarketDataRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    entity_id: str | None = Field(default=None, max_length=64, description='entity_id returned by resolve_entity.')
    symbol: str | None = Field(default=None, max_length=32, description='Provider symbol; use with exchange when no entity_id.')
    exchange: str | None = Field(default=None, max_length=16)
    interval: Literal['1min', '5min', '15min', '30min', '1h', '1day', '1week', '1month'] = Field(
        default='1day', description='Bar size; intraday sizes are mainly for event-day analysis and may be delayed.')
    start_date: date | None = Field(default=None, description='Inclusive start; with end_date, or use lookback instead.')
    end_date: date | None = Field(default=None, description='Inclusive end; clamped to the latest completed session.')
    lookback: Literal['5d', '1m', '3m', '6m', 'ytd', '1y', '3y', '5y', '10y'] | None = Field(
        default=None, description='Named window ending at the latest completed session; alternative to dates.')
    rationale: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid(self) -> 'MarketDataRequest':
        if not self.entity_id and not self.symbol:
            raise ValueError('Give entity_id (from resolve_entity) or symbol with exchange.')
        if self.lookback is not None:
            if self.start_date is not None or self.end_date is not None:
                raise ValueError('Give either lookback or start_date/end_date, not both.')
        elif self.start_date is None:
            raise ValueError('Give lookback, or start_date (with optional end_date).')
        elif self.end_date is not None and self.start_date > self.end_date:
            raise ValueError('start_date is after end_date.')
        return self


class CompanyFinancialsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    entity_id: str | None = Field(default=None, max_length=64, description='entity_id from resolve_entity.')
    cik: str | None = Field(default=None, pattern=r'^\d{1,10}$')
    ticker: str | None = Field(default=None, max_length=16, description='Exact US ticker when no entity_id or cik.')
    lines: list[Literal[LINE_NAMES]] | None = Field(  # type: ignore[valid-type]
        default=None, max_length=len(LINE_NAMES), description='Standard line names; omit for all.')
    periods: Literal['last_8_quarters', 'last_4_quarters', 'last_3_fiscal_years', 'last_5_fiscal_years'] = 'last_8_quarters'
    derive_q4: bool = Field(default=False, description='Derive single-quarter flow values from cumulative figures (US: Q4 = FY minus Q1-Q3; A-shares: Q2/Q3/Q4 from year-to-date figures); rows are marked derived.')
    rationale: str = Field(min_length=1)

    @model_validator(mode='after')
    def one_identity(self) -> 'CompanyFinancialsRequest':
        if not (self.entity_id or self.cik or self.ticker):
            raise ValueError('Give entity_id, cik or ticker.')
        return self


class FilingsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    entity_id: str | None = Field(default=None, max_length=64)
    cik: str | None = Field(default=None, pattern=r'^\d{1,10}$')
    ticker: str | None = Field(default=None, max_length=16)
    action: Literal['list', 'search'] = Field(
        default='list', description="'list' filings of one company; 'search' is EDGAR full-text search (may be disabled).")
    forms: list[str] | None = Field(default=None, max_length=20, description='Form types such as 10-K, 10-Q, 8-K.')
    since: date | None = None
    until: date | None = None
    limit: int = Field(default=20, ge=1, le=50)
    query: str | None = Field(default=None, max_length=200, description='Required for action=search.')
    rationale: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid(self) -> 'FilingsRequest':
        if self.action == 'search':
            if not self.query or not self.query.strip():
                raise ValueError('query is required for action=search.')
        elif not (self.entity_id or self.cik or self.ticker):
            raise ValueError('Give entity_id, cik or ticker for action=list.')
        if self.since and self.until and self.since > self.until:
            raise ValueError('since is after until.')
        return self


class SecEarningsReleaseRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    entity_id: str | None = Field(default=None, max_length=64, description='entity_id from resolve_entity.')
    cik: str | None = Field(default=None, pattern=r'^\d{1,10}$')
    ticker: str | None = Field(default=None, max_length=16, description='Exact US ticker when no entity_id or cik.')
    as_of: date | None = Field(default=None, description='Optional cutoff date restricting eligible 8-K filings.')
    pairing_mode: Literal['sequential', 'yoy'] = Field(
        default='sequential',
        description='Prior quarter pairing mode: latest sequential 8-K or same quarter one year prior.',
    )
    include_ex99_2: bool = Field(
        default=True,
        description='Also fetch Exhibit 99.2 (remarks or presentation) when present in the 8-K directory.',
    )
    max_candidates: int = Field(default=15, ge=2, le=40)
    rationale: str = Field(min_length=1)

    @model_validator(mode='after')
    def one_identity(self) -> 'SecEarningsReleaseRequest':
        if not (self.entity_id or self.cik or self.ticker):
            raise ValueError('Give entity_id, cik or ticker.')
        return self


class SecFilingTablesRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    entity_id: str | None = Field(default=None, max_length=64, description='entity_id from resolve_entity.')
    cik: str | None = Field(default=None, pattern=r'^\d{1,10}$')
    ticker: str | None = Field(default=None, max_length=16, description='Exact US ticker when no entity_id or cik.')
    accession: str = Field(min_length=18, max_length=24, description='SEC accession number of the 10-Q or 10-K filing.')
    category: Literal['Statements', 'Notes', 'Tables', 'Cover', 'Details', 'Other'] | None = Field(
        default=None,
        description='Optional FilingSummary report group filter when browsing the catalog.',
    )
    r_file: str | None = Field(
        default=None,
        max_length=24,
        pattern=r'^[Rr]\d{1,5}\.html?$',
        description='Optional R-file name (for example R2.htm) to fetch and parse deterministically; omit to list the catalog.',
    )
    rationale: str = Field(min_length=1)

    @model_validator(mode='after')
    def one_identity(self) -> 'SecFilingTablesRequest':
        if not (self.entity_id or self.cik or self.ticker):
            raise ValueError('Give entity_id, cik or ticker.')
        return self


class SecFilingSectionsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    entity_id: str | None = Field(default=None, max_length=64)
    cik: str | None = Field(default=None, pattern=r'^\d{1,10}$')
    ticker: str | None = Field(default=None, max_length=16)
    url: str | None = Field(
        default=None,
        max_length=500,
        description='Direct SEC Archives filing URL (www.sec.gov) to fetch and split into Item sections.',
    )
    source_id: str | None = Field(
        default=None,
        max_length=80,
        description='Optional already-acquired source_id containing filing text or HTML.',
    )
    form: str | None = Field(default=None, max_length=16, description='Optional SEC form type hint such as 10-K or 10-Q.')
    section_keys: list[str] | None = Field(
        default=None,
        max_length=20,
        description='Optional filter of section keys (for example item_1a_risk_factors, mda, liquidity_and_capital_resources, critical_accounting_estimates).',
    )
    rationale: str = Field(min_length=1)

    @model_validator(mode='after')
    def has_target(self) -> 'SecFilingSectionsRequest':
        if not (self.url or self.source_id):
            raise ValueError('Give url or source_id.')
        return self


class SecRedlineDiffRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    current_source_id: str | None = Field(default=None, max_length=80)
    prior_source_id: str | None = Field(default=None, max_length=80)
    current_url: str | None = Field(default=None, max_length=500)
    prior_url: str | None = Field(default=None, max_length=500)
    current_form: str | None = Field(default=None, max_length=16)
    prior_form: str | None = Field(default=None, max_length=16)
    section_keys: list[str] | None = Field(
        default=None,
        max_length=20,
        description='Optional subset of section keys to compare across periods.',
    )
    filter_rolling_noise: bool = Field(
        default=True,
        description='Filter pure period/date rolling edits while retaining substantive disclosure changes.',
    )
    similarity_threshold: float = Field(default=0.45, ge=0.1, le=0.95)
    rationale: str = Field(min_length=1)

    @model_validator(mode='after')
    def has_pair(self) -> 'SecRedlineDiffRequest':
        if not (self.current_source_id or self.current_url):
            raise ValueError('Give current_source_id or current_url.')
        if not (self.prior_source_id or self.prior_url):
            raise ValueError('Give prior_source_id or prior_url.')
        return self


class SecFulltextSearchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    entity_id: str | None = Field(default=None, max_length=64)
    cik: str | None = Field(
        default=None,
        pattern=r'^\d{1,10}$',
        description='Single 1 to 10 digit SEC CIK; required when entity_id or ticker is not provided.',
    )
    ticker: str | None = Field(default=None, max_length=16, description='Exact US ticker resolved to a single SEC CIK.')
    query: str = Field(min_length=1, max_length=200, description='Full-text query scoped strictly to the resolved company CIK.')
    forms: list[str] | None = Field(default=None, max_length=20, description='Optional form filter such as 10-K, 10-Q, 8-K.')
    since: date | None = None
    until: date | None = None
    limit: int = Field(default=25, ge=1, le=100)
    rationale: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_scope(self) -> 'SecFulltextSearchRequest':
        if not (self.entity_id or self.cik or self.ticker):
            raise ValueError('Single-company scope required: give entity_id, cik, or ticker.')
        if self.since and self.until and self.since > self.until:
            raise ValueError('since is after until.')
        return self


class EarningsConsensusRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    entity_id: str | None = Field(default=None, max_length=64)
    symbol: str | None = Field(default=None, max_length=32, description='Company ticker symbol.')
    period: Literal['quarter', 'annual'] = Field(default='quarter', description='Consensus estimate cadence.')
    limit: int = Field(default=8, ge=1, le=20)
    include_actuals: bool = Field(default=True, description='Include historical reported actuals vs consensus estimates when available.')
    rationale: str = Field(min_length=1)

    @model_validator(mode='after')
    def has_target(self) -> 'EarningsConsensusRequest':
        if not (self.entity_id or self.symbol):
            raise ValueError('Give entity_id or symbol.')
        return self


class EarningsTranscriptRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    entity_id: str | None = Field(default=None, max_length=64)
    symbol: str | None = Field(default=None, max_length=32, description='Company ticker symbol.')
    fiscal_year: int | None = Field(default=None, ge=1990, le=2100)
    fiscal_quarter: int | None = Field(default=None, ge=1, le=4)
    rationale: str = Field(min_length=1)

    @model_validator(mode='after')
    def has_target(self) -> 'EarningsTranscriptRequest':
        if not (self.entity_id or self.symbol):
            raise ValueError('Give entity_id or symbol.')
        return self


class EarningsQualityToolRequest(EarningsQualityRequest):
    rationale: str = Field(min_length=1, max_length=500, description='Why these earnings quality diagnostics are needed.')


class GuidanceAndNonGaapToolRequest(GuidanceAndNonGaapRequest):
    rationale: str = Field(min_length=1, max_length=500, description='Why this guidance and Non-GAAP reconciliation is needed.')


class ValuationToolRequest(ValuationRequest):
    rationale: str = Field(min_length=1, max_length=500, description='Why this valuation and implied expectations analysis is needed.')


SecEventCategoryInput = Literal['8k', 'form4', '13dg', '6k']

_SEC_EVENT_CATEGORY_MAP: dict[str, EventCategory] = {
    '8k': 'material_8k',
    'material_8k': 'material_8k',
    'form4': 'insider_trade',
    'insider_trade': 'insider_trade',
    '13dg': 'ownership_13dg',
    'ownership_13dg': 'ownership_13dg',
    '6k': 'foreign_6k',
    'foreign_6k': 'foreign_6k',
}


def normalize_sec_event_categories(categories: Sequence[str] | None) -> list[EventCategory] | None:
    """Map short (``8k``, ``form4``, ``13dg``, ``6k``) or canonical Layer 1 category names."""
    if not categories:
        return None
    mapped: list[EventCategory] = []
    seen: set[EventCategory] = set()
    for raw in categories:
        key = str(raw).strip().lower()
        cat = _SEC_EVENT_CATEGORY_MAP.get(key)
        if cat is None:
            raise ValueError(f'Unsupported SEC event category: {raw!r}.')
        if cat not in seen:
            seen.add(cat)
            mapped.append(cat)
    return mapped or None


class SecMaterialEventsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    entity_id: str | None = Field(default=None, max_length=64, description='entity_id from resolve_entity.')
    cik: str | None = Field(default=None, pattern=r'^\d{1,10}$')
    ticker: str | None = Field(default=None, max_length=16, description='Exact US ticker when no entity_id or cik.')
    categories: list[SecEventCategoryInput] | None = Field(
        default=None,
        max_length=4,
        description="Optional event categories ('8k', 'form4', '13dg', '6k'); omit to scan all categories.",
    )
    lookback_days: int = Field(default=90, ge=1, le=730, description='Trailing calendar day window ending at as_of.')
    as_of: date | None = Field(default=None, description='Optional cutoff date (inclusive on filed_at) preventing look-ahead.')
    include_suppressed: bool = Field(default=False, description='Include deterministic Layer 1 filtered noise records.')
    rationale: str = Field(default='', max_length=500, description='Why material SEC events are needed.')

    @model_validator(mode='after')
    def one_identity(self) -> 'SecMaterialEventsRequest':
        if not (self.entity_id or self.cik or self.ticker):
            raise ValueError('Give entity_id, cik or ticker.')
        return self


# --------------------------------------------------------------------------- outcome
@dataclass
class FinToolOutcome:
    result: dict[str, Any]
    source: ResearchSource | None = None
    leads: list[dict[str, Any]] = field(default_factory=list)
    entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    extra_keys: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MarketSpec:
    entity_id: str | None
    symbol: str
    exchange: str | None          # normalized research_clock code when known
    provider_exchange: str | None
    currency: str | None
    interval: str
    start: date | None
    end: date | None

    @property
    def key(self) -> str:
        return 'market_data|' + '|'.join([(self.entity_id or f'?:{self.symbol}').upper(), self.interval,
                                         self.start.isoformat() if self.start else '', self.end.isoformat() if self.end else ''])


def _fmt(value: Any) -> str:
    return format(value, 'f') if hasattr(value, 'as_tuple') else str(value)


def _norm_entity_id(value: str) -> str:
    text = value.strip()
    if text.lower().startswith('cik:') and text[4:].isdigit():
        return 'cik:' + text[4:].zfill(10)
    return text.upper()


def wire(res: ToolResult, **extra: Any) -> dict[str, Any]:
    """W6 envelope without the data payload."""
    out: dict[str, Any] = {'status': res.status, 'reason': res.reason}
    if res.message:
        out['message'] = res.message
    if res.retryable:
        out['retryable'] = True
    if res.suggested_actions:
        out['suggested_actions'] = [a.model_dump(mode='json', exclude_none=True) for a in res.suggested_actions]
    if res.coverage is not None:
        out['coverage'] = res.coverage.model_dump(mode='json', exclude_none=True)
    out['permissions'] = res.permissions.model_dump(mode='json')
    if res.provenance is not None:
        out['provenance'] = res.provenance.model_dump(mode='json', exclude_none=True)
    out['cost'] = res.cost.model_dump(mode='json')
    out.update(extra)
    if res.status == 'unknown':
        out['external_state'] = 'unknown'
        out['message'] = UNKNOWN_READ_MESSAGE
    return out


class _GatedLimiter:
    """Rate limiter seam: counts every external call like the old loaders, then applies provider pacing."""

    def __init__(self, reserve_call: Callable[[], None], reserve_sec: Callable[[], bool], inner) -> None:
        self._reserve_call, self._reserve_sec, self._inner = reserve_call, reserve_sec, inner

    def acquire(self, provider: str, cost: int = 1) -> RateDecision:
        try:
            self._reserve_call()
        except Exception as error:  # ApiProblem quota/abuse errors; wait hint only
            wait = getattr(error, 'retry_after', None)
            return RateDecision(False, float(wait) if isinstance(wait, (int, float)) and wait > 0 else 60.0, 'rate_limit')
        if provider == 'sec_edgar':
            # Existing global ~1 request/second SEC pacing; never the adapter's 10 req/s default.
            return RateDecision(True) if self._reserve_sec() else RateDecision(False, 1.0, 'rate_limit')
        return self._inner.acquire(provider, cost)


class FinDataToolbox:
    def __init__(self, *, account_id: str, market_key: str | None, credential_owner: str = 'user_byok', sec_user_agent: str | None,
                 persist: Callable[..., ResearchSource], reserve_call: Callable[[], None] = lambda: None,
                 reserve_sec: Callable[[], bool] = lambda: True, remaining_seconds: Callable[[], float] = lambda: float('inf'),
                 ensure_running: Callable[[], None] = lambda: None, now: Callable[[], datetime] | None = None,
                 disabled_reason: str | None = None, policy: FinDataPolicy | None = None, limiter=None,
                 sec_transport=None, sec_stream_transport=None, twelve_transport=None, cancel_event=None,
                 tushare_key: str | None = None, tushare_owner: str = 'user_byok', tushare_transport=None,
                 baostock_runner: Any = _AUTO, cn_probe: ReachabilityProbe | None = None, yahoo_transport=None,
                 on_result: Callable[[str, dict[str, Any]], None] | None = None,
                 fmp_key: str | None = None, fmp_owner: str = 'user_byok', fmp_transport=None,
                 l1_cache: Any = None, sec_events_limiter: SecPacingLimiter | None = None) -> None:
        self.account_id, self._market_key = account_id, market_key or None
        self._owner = credential_owner if credential_owner in ('platform', 'user_byok') else 'user_byok'
        try:
            self._sec_ua: str | None = validate_user_agent(sec_user_agent)
        except SecFailure:
            self._sec_ua = None
        self._persist, self._remaining, self._ensure_running = persist, remaining_seconds, ensure_running
        self._reserve_call = reserve_call
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.disabled_reason = disabled_reason
        self._policy = policy or load_policy()
        self._limiter = _GatedLimiter(reserve_call, reserve_sec, limiter or default_limiter())
        self._sec_transport, self._sec_stream, self._td_transport = sec_transport, sec_stream_transport, twelve_transport
        self._cancel = cancel_event
        self._ts_key = tushare_key or None
        self._ts_owner = tushare_owner if tushare_owner in ('platform', 'user_byok') else 'user_byok'
        self._ts_transport = tushare_transport
        self._bs_injected = baostock_runner is not _AUTO
        self._bs_runner = baostock_default_runner() if baostock_runner is _AUTO else baostock_runner
        self._cn_probe, self._yahoo_transport = cn_probe, yahoo_transport
        self.on_result = on_result  # (tool name, model-visible result) for run level accounting
        self._fmp_key = fmp_key.strip() if isinstance(fmp_key, str) and fmp_key.strip() else None
        self._fmp_owner = fmp_owner if fmp_owner in ('platform', 'user_byok') else 'user_byok'
        self._fmp_transport = fmp_transport
        self._l1_cache = l1_cache
        self._sec_events_limiter = sec_events_limiter

    # -- capability flags (first user message; tools stay static)
    @property
    def enabled(self) -> bool:
        return self.disabled_reason is None

    def capabilities(self) -> dict[str, Any]:
        sec = self.enabled and self._sec_ua is not None
        provider = self.cn_provider() if self.enabled else None
        fin_provider = self.cn_provider('financials') if self.enabled else None
        unreachable = self.cn_unreachable() if self.enabled else []
        yahoo_cn = self.enabled and self._yahoo().available('bars_daily', 'SSE')
        yahoo_hk = self.enabled and self._yahoo().available('bars_daily', 'HKEX')
        return {'market_data_available': self.enabled and self._market_key is not None,
                'market_data_key_required': self.enabled and self._market_key is None,
                'sec_financials_available': sec, 'filings_available': sec,
                'cn_market_data_available': provider is not None or yahoo_cn,
                'cn_market_data_provider': provider or ('yahoo' if yahoo_cn else None),
                'cn_financials_available': fin_provider is not None,
                'cn_financials_limited': fin_provider == 'baostock',
                'cn_data_provider': provider,
                'cn_providers_unreachable': unreachable,
                'hk_market_data_available': yahoo_hk or (self.enabled and self._market_key is not None)}

    def earnings_capabilities(self) -> dict[str, Any]:
        """Earnings/external-provider flags kept separate from ``capabilities()`` core contract keys."""
        key_ready = self.enabled and self._fmp_key is not None
        consensus_ok = key_ready and any(
            not isinstance(self._policy.decide('fmp', cat, credential_owner=self._fmp_owner), Denied)
            for cat in ('consensus', 'earnings_consensus')
        )
        transcript_ok = key_ready and any(
            not isinstance(self._policy.decide('fmp', cat, credential_owner=self._fmp_owner), Denied)
            for cat in ('transcripts', 'earnings_transcript')
        )
        return {
            'earnings_consensus_available': consensus_ok,
            'earnings_transcript_available': transcript_ok,
            'fmp_key_configured': bool(self._fmp_key),
            'fmp_credential_owner': self._fmp_owner if self._fmp_key else None,
        }

    # -- adapters
    def _sec(self) -> SecEdgarAdapter:
        kwargs: dict[str, Any] = {}
        if self._sec_transport is not None:
            kwargs['transport'] = self._sec_transport
        if self._sec_stream is not None:
            kwargs['stream_transport'] = self._sec_stream
        elif self._sec_transport is None:
            from .fin_data.adapters.base import default_stream_transport
            kwargs['stream_transport'] = default_stream_transport(cancel_event=self._cancel)
        return SecEdgarAdapter(self._sec_ua, limiter=self._limiter, policy=self._policy, now_fn=self._now, **kwargs)

    def _td(self, timeout: float) -> TwelveDataAdapter:
        kwargs: dict[str, Any] = {}
        if self._td_transport is not None:
            kwargs['transport'] = self._td_transport
        else:
            from .fin_data.adapters.base import _default_binary_transport

            def send(method, url, headers, body, tmo):
                return _default_binary_transport(method, url, headers, body, tmo, max_response_bytes=2_000_000,
                                                 cancel_event=self._cancel)
            kwargs['transport'] = send
        return TwelveDataAdapter(api_key=self._market_key, credential_owner=self._owner, account_id=self.account_id,
                                 limiter=self._limiter, policy=self._policy, now_fn=self._now, timeout=timeout, **kwargs)

    # -- A-share providers: Tushare with the account's token first, keyless BaoStock as the fallback
    def _tushare(self, timeout: float = TUSHARE_TIMEOUT_SECONDS) -> TushareAdapter:
        kwargs: dict[str, Any] = {}
        if self._ts_transport is not None:
            kwargs['transport'] = self._ts_transport
        else:
            from .fin_data.adapters.base import _default_binary_transport

            def send(method, url, headers, body, tmo):
                return _default_binary_transport(method, url, headers, body, tmo, max_response_bytes=8_000_000,
                                                 cancel_event=self._cancel)
            kwargs['transport'] = send
        return TushareAdapter(token=self._ts_key, credential_owner=self._ts_owner, account_id=self.account_id,
                              limiter=self._limiter, policy=self._policy, now_fn=self._now, timeout=timeout, **kwargs)

    def _baostock(self, timeout: float = BAOSTOCK_TIMEOUT_SECONDS) -> BaoStockAdapter:
        return BaoStockAdapter(runner=self._bs_runner, limiter=self._limiter, policy=self._policy, now_fn=self._now,
                               timeout=timeout)

    def _yahoo(self) -> YahooAdapter:
        if self._yahoo_transport is not None:
            transport = self._yahoo_transport
        else:
            from .fin_data.adapters.base import _default_binary_transport

            def transport(method, url, headers, body, tmo):
                return _default_binary_transport(method, url, headers, body, tmo, max_response_bytes=5_000_000,
                                                 cancel_event=self._cancel)
        return YahooAdapter(transport=transport, limiter=self._limiter, policy=self._policy, now_fn=self._now,
                            timeout=min(YAHOO_TIMEOUT_SECONDS, max(1.0, self._remaining())))

    def _reachable(self, provider: str) -> bool:
        """Cached TCP reachability of a mainland-only provider; injected transports/runners (tests) are not probed."""
        if self._cn_probe is None:
            if (provider == 'tushare' and self._ts_transport is not None) or (provider == 'baostock' and self._bs_injected):
                return True
            return default_probe().reachable(provider)
        return self._cn_probe.reachable(provider)

    def _cn_providers(self, category: str) -> list[tuple[str, Any, bool]]:
        """Policy/credential-available A-share providers in routing order with their reachability."""
        remaining = self._remaining()
        out: list[tuple[str, Any, bool]] = []
        if self._ts_key is not None:
            ts = self._tushare(min(TUSHARE_TIMEOUT_SECONDS, max(1.0, remaining)))
            if ts.available(category):
                out.append(('tushare', ts, self._reachable('tushare')))
        if self._bs_runner is not None:
            bs = self._baostock(min(BAOSTOCK_TIMEOUT_SECONDS, max(1.0, remaining)))
            if bs.available(category):
                out.append(('baostock', bs, self._reachable('baostock')))
        return out

    def cn_provider(self, category: str = 'bars_daily') -> str | None:
        return next((name for name, _a, ok in self._cn_providers(category) if ok), None)

    def cn_unreachable(self) -> list[str]:
        names: list[str] = []
        for category in ('bars_daily', 'financials'):
            names.extend(n for n, _a, ok in self._cn_providers(category) if not ok and n not in names)
        return names

    def _cn_chain_info(self, category: str) -> tuple[list[tuple[str, Any]], list[str]]:
        providers = self._cn_providers(category)
        return [(n, a) for n, a, ok in providers if ok], [n for n, _a, ok in providers if not ok]

    def _cn_chain(self, category: str) -> list[tuple[str, Any]]:
        return self._cn_chain_info(category)[0]

    @staticmethod
    def _unreachable_outcome(unreachable: list[str]) -> FinToolOutcome:
        return FinToolOutcome({'status': 'unavailable', 'reason': UNREACHABLE_REASON,
                               'message': unreachable_message(unreachable), 'unreachable_providers': list(unreachable)})

    def _blocked(self) -> dict[str, Any] | None:
        if self.disabled_reason is not None:
            return {'status': 'restricted', 'reason': self.disabled_reason}
        return None

    def _sec_blocked(self) -> dict[str, Any] | None:
        blocked = self._blocked()
        if blocked:
            return blocked
        if self._sec_ua is None:
            return {'status': 'unavailable', 'reason': 'sec_contact_unconfigured_or_unavailable'}
        self._ensure_running()
        if self._remaining() < SEC_MIN_REMAINING_SECONDS:
            return {'status': 'unavailable', 'reason': 'time_budget',
                    'message': 'The remaining run time is too short for another SEC request.'}
        return None

    def _source_permissions(self, perms: Permissions) -> SourcePermissions:
        return SourcePermissions(store=perms.store, display=perms.display, model_input=perms.model_input, export=perms.export)

    # -- entity helpers
    @staticmethod
    def _info(entity_id: str, entities: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        eid = _norm_entity_id(entity_id)
        cached = dict(entities.get(eid) or {})
        parsed = parse_entity_id(eid)
        ids = cached.get('identifiers') or {}
        return {'entity_id': eid, 'symbol': cached.get('symbol') or parsed['symbol'],
                'exchange': cached.get('exchange') or parsed['exchange'],
                'exchange_raw': ids.get('exchange_raw'), 'currency': cached.get('currency'),
                'cik': ids.get('cik') or parsed['cik'], 'name': cached.get('company')}

    # -- resolve_entity
    def resolve_entity(self, req: ResolveEntityRequest, entities: Mapping[str, Mapping[str, Any]]) -> FinToolOutcome:
        blocked = self._blocked()
        cn_wanted = blocked is None and wants_cn(req.query, req.market)
        cn_chain, cn_down = self._cn_chain_info('identity') if cn_wanted else ([], [])
        code_query = cn_helpers.parse_cn_code(req.query) is not None
        hk_query = req.market in (None, 'HK') and cn_helpers.parse_hk_code(req.query, req.market) is not None
        cn_name_gap = cn_wanted and not code_query and not hk_query and not cn_chain
        unverified_code = cn_wanted and code_query and not cn_chain and bool(cn_down)  # unreachable listing: code format only
        if blocked is None and self._sec_ua is None and self._market_key is None and not cn_chain and not hk_query \
                and not unverified_code:
            blocked = ({'status': 'unavailable', 'reason': 'cn_name_lookup_unavailable', 'message': CN_NAME_LOOKUP_MESSAGE,
                        **({'unreachable_providers': cn_down} if cn_down else {})} if cn_name_gap
                       else {'status': 'unavailable', 'reason': 'no_identity_source_available'})
        if blocked is None and self._sec_ua is not None:
            self._ensure_running()
            if self._remaining() < SEC_MIN_REMAINING_SECONDS:
                blocked = {'status': 'unavailable', 'reason': 'time_budget'}
        if blocked:
            return FinToolOutcome(blocked)
        td = self._td(min(BYOK_MAX_TIMEOUT_SECONDS, max(1.0, self._remaining()))) if self._market_key else None
        cn_res = (resolve_cn(req.query, [(name, adapter.stock_list) for name, adapter in cn_chain]) if cn_chain
                  else resolve_cn(req.query, []) if unverified_code else None)
        res = _resolve_entity(req.query, req.market, req.asset_type, cn=cn_res,
                              sec=self._sec() if self._sec_ua else None, twelve=td,
                              twelve_kwargs={'api_key': self._market_key, 'credential_owner': self._owner,
                                             'account_id': self.account_id} if td else None)
        cands, cache = [], {}
        for c in res.data:
            e = c.entity
            row = {'entity_id': e.entity_id, 'company': e.company_name,
                   'security': {k: v for k, v in {'symbol': e.symbol, 'exchange': e.exchange, 'currency': e.currency,
                                                  'asset_type': e.asset_type,
                                                  'listing_date': e.listing_date.isoformat() if e.listing_date else None}.items() if v},
                   'identifiers': dict(e.identifiers), 'match_basis': c.match_basis, 'unique': c.unique,
                   'sources': list(c.sources)}
            cands.append(row)
            cache[e.entity_id] = {'company': e.company_name, 'symbol': e.symbol, 'exchange': e.exchange,
                                  'currency': e.currency, 'asset_type': e.asset_type, 'identifiers': dict(e.identifiers)}
        notes = []
        if self._market_key is None:
            notes.append('No Twelve Data key on this account: only SEC-registered US issuers%s can be matched; other '
                         'markets need the user to add their own Twelve Data key in Settings.'
                         % (' and A-shares (Tushare/BaoStock)' if self.cn_provider('identity') else ''))
        if cn_name_gap:
            if res.status in ('empty', 'failed'):
                return FinToolOutcome({'status': 'unavailable', 'reason': 'cn_name_lookup_unavailable',
                                       'message': CN_NAME_LOOKUP_MESSAGE, 'cost': wire(res)['cost'],
                                       **({'unreachable_providers': cn_down} if cn_down else {})})
            notes.append(CN_NAME_LOOKUP_MESSAGE)
        out = wire(res, candidates=cands[:20])
        if notes:
            out['notes'] = notes
        return FinToolOutcome(out, entities=cache)

    # -- market_data
    @staticmethod
    def _cn_code(symbol: str | None, exchange: str | None) -> cn_helpers.CnCode | None:
        """A-share code from a symbol (6 digits, or with a suffix/prefix) and an optional CN exchange."""
        if not symbol:
            return None
        parsed = cn_helpers.parse_cn_code(symbol)
        if parsed is None:
            return None
        if exchange and cn_helpers.is_cn_exchange(exchange) and re.fullmatch(r'\d{6}', symbol.strip()):
            return cn_helpers.CnCode(parsed.code, exchange.upper(), False)
        return parsed

    def market_spec(self, req: MarketDataRequest, entities: Mapping[str, Mapping[str, Any]]) -> MarketSpec | dict[str, Any]:
        info = self._info(req.entity_id, entities) if req.entity_id else {
            'entity_id': None, 'symbol': req.symbol, 'exchange': req.exchange, 'exchange_raw': None,
            'currency': None, 'cik': None, 'name': None}
        symbol = (req.symbol if not req.entity_id else info['symbol'])
        if not symbol:
            return {'status': 'failed', 'reason': 'entity_not_resolved',
                    'message': 'This entity has no tradable symbol known in this run; call resolve_entity first or pass symbol and exchange.'}
        raw_ex = info['exchange_raw'] or info['exchange'] or (req.exchange if not req.entity_id else None)
        ex_code = normalize_exchange(info['exchange']) if info['exchange'] else (normalize_exchange(req.exchange) if req.exchange else None)
        entity_id = info['entity_id'] if info['entity_id'] and not str(info['entity_id']).startswith('cik:') else (
            f'{ex_code}:{symbol.upper()}' if ex_code else None)
        currency = info['currency']
        if ex_code is None or cn_helpers.is_cn_exchange(ex_code):
            cn_code = self._cn_code(symbol, ex_code)
            if cn_code is not None:
                symbol, ex_code, entity_id, currency = cn_code.code, cn_code.exchange, cn_code.entity_id, cn_helpers.CN_CURRENCY
                raw_ex = raw_ex if cn_helpers.is_cn_exchange(raw_ex) else cn_code.exchange
            elif ex_code is None:
                hk = cn_helpers.parse_hk_code(symbol)
                if hk is not None:
                    symbol, ex_code, entity_id, currency = hk.code, 'HKEX', hk.entity_id, 'HKD'
                    raw_ex = raw_ex or 'HKEX'
        start, end = req.start_date, req.end_date
        if req.lookback is not None:
            session = exchange_session(ex_code)
            start, end = resolve_lookback(req.lookback, now=self._now(), exchange=ex_code,
                                          timezone_name=session.timezone if session else 'UTC')
        return MarketSpec(entity_id, symbol, ex_code, raw_ex, currency, req.interval, start, end)

    @staticmethod
    def _hk_code(spec: MarketSpec) -> cn_helpers.HkCode | None:
        return cn_helpers.parse_hk_code(spec.symbol, 'HK') if spec.exchange == 'HKEX' else None

    def market_data(self, spec: MarketSpec) -> FinToolOutcome:
        blocked = self._blocked()
        if blocked:
            return FinToolOutcome(blocked)
        cn_code = self._cn_code(spec.symbol, spec.exchange) if cn_helpers.is_cn_exchange(spec.exchange) else None
        if cn_code is not None:
            chain, unreachable = self._cn_chain_info('bars_daily')
            yahoo = self._yahoo()
            if yahoo.available('bars_daily', cn_code.exchange):
                chain = [*chain, ('yahoo', yahoo)]  # last resort: unofficial keyless source, personal-dev policy only
            if chain:
                return self._cn_market_data(spec, cn_code, chain)
            if self._market_key is None:
                if unreachable:
                    return self._unreachable_outcome(unreachable)
                return FinToolOutcome({'status': 'unavailable', 'reason': 'cn_market_data_unavailable',
                                       'message': CN_KEY_MESSAGE})
        hk_code = self._hk_code(spec)
        if hk_code is not None:
            yahoo = self._yahoo()
            yahoo_ok = yahoo.available('bars_daily', 'HKEX')
            if self._market_key is None and yahoo_ok:
                return self._cn_market_data(spec, hk_code, [('yahoo', yahoo)])
            if self._market_key is not None:
                out = self._td_market_data(spec)
                if out.result.get('status') in ('ok', 'partial') or not yahoo_ok:
                    return out
                fallback = self._cn_market_data(spec, hk_code, [('yahoo', yahoo)],
                                                [f"twelve_data failed ({out.result.get('reason')}); Yahoo Finance tried next."])
                return fallback
        if self._market_key is None:
            return FinToolOutcome({'status': 'unavailable', 'reason': 'market_data_key_required',
                                   'message': MARKET_KEY_REQUIRED_MESSAGE})
        return self._td_market_data(spec)

    def _td_market_data(self, spec: MarketSpec) -> FinToolOutcome:
        self._ensure_running()
        remaining = self._remaining()
        if remaining <= 0:
            return FinToolOutcome({'status': 'unavailable', 'reason': 'time_budget'})
        adapter = self._td(min(BYOK_MAX_TIMEOUT_SECONDS, remaining))
        res = adapter.bars(spec.symbol, exchange=spec.provider_exchange or spec.exchange, interval=spec.interval,
                           start=spec.start, end=spec.end, entity_id=spec.entity_id, account_id=self.account_id,
                           credential_owner=self._owner)
        if res.status not in ('ok', 'partial') or not res.data:
            return FinToolOutcome(wire(res))
        return self._bars_outcome(res, spec, provider='twelve_data', endpoint='time_series', owner=self._owner)

    def _cn_market_data(self, spec: MarketSpec, code: Any, chain: list[tuple[str, Any]],
                        prior_failures: list[str] | None = None) -> FinToolOutcome:
        if spec.interval in INTRADAY_INTERVALS:
            return FinToolOutcome({'status': 'unavailable', 'reason': 'intraday_not_supported_cn',
                                   'message': 'Free A-share and Hong Kong sources provide daily, weekly and monthly bars only; use '
                                              'interval 1day, 1week or 1month.'})
        self._ensure_running()
        if self._remaining() <= 0:
            return FinToolOutcome({'status': 'unavailable', 'reason': 'time_budget'})
        failures: list[str] = list(prior_failures or [])
        last: ToolResult | None = None
        for name, adapter in chain:
            res = adapter.bars(code, interval=spec.interval, start=spec.start, end=spec.end, entity_id=spec.entity_id)
            if res.status in ('ok', 'partial') and res.data:
                if failures:
                    res = res.model_copy(update={'coverage': res.coverage.model_copy(
                        update={'notes': [*res.coverage.notes, *failures]}) if res.coverage else res.coverage})
                owner = self._ts_owner if name == 'tushare' else 'keyless_public_source'
                note = {'tushare': 'Third-party A-share data under the account\'s own Tushare token, personal use only; not shared '
                                   'across accounts and not exportable. Prices are as traded; adj_factor carries the adjustment.',
                        'yahoo': 'Yahoo Finance daily bars via its unofficial keyless chart endpoint, allowed by the configured data '
                                 'policy for personal development use only (Yahoo terms do not allow commercial use); not '
                                 'exportable. close is split-adjusted; adj_factor = adjclose / close.'}.get(name) or (
                        'BaoStock public source (no terms of use found; personal development use assumed, review before '
                        'public release); not exportable. Prices are as traded; adj_factor is derived.')
                endpoint = {'tushare': 'daily', 'yahoo': 'v8/finance/chart'}.get(name, 'query_history_k_data_plus')
                return self._bars_outcome(res, spec, provider=name, endpoint=endpoint, owner=owner, note=note)
            last = res
            if res.status == 'empty':
                break
            failures.append(f'{name} failed ({res.reason}); next source tried.' if (name, adapter) != chain[-1] else f'{name} failed ({res.reason}).')
        out = wire(last) if last is not None else {'status': 'unavailable', 'reason': 'cn_market_data_unavailable'}
        if failures:
            out['source_failures'] = failures
        return FinToolOutcome(out)

    def _bars_outcome(self, res: ToolResult, spec: MarketSpec, *, provider: str, endpoint: str, owner: str,
                      note: str | None = None) -> FinToolOutcome:
        bars, cov, prov = res.data, res.coverage, res.provenance
        currency = (cov.currency if cov else None) or spec.currency
        ex_label = spec.exchange or spec.provider_exchange or '?'
        label = f'{spec.symbol.upper()}@{ex_label}'
        daily = spec.interval not in INTRADAY_INTERVALS
        lines = []
        for b in bars:
            ts = b.ts_start.date().isoformat() if daily else b.ts_start.isoformat()
            vol = _fmt(b.volume) if b.volume is not None else '-'
            lines.append(f'{label} | {ts} | {spec.interval} | O {_fmt(b.open)} H {_fmt(b.high)} L {_fmt(b.low)} '
                         f'C {_fmt(b.close)} V {vol}' + (f' {currency}' if currency else '')
                         + (f' ADJF {_fmt(b.adj_factor)}' if b.adj_factor is not None else ''))
        first, last = lines[0].split(' | ')[1], lines[-1].split(' | ')[1]
        provenance = {
            'provider': provider, 'endpoint': endpoint, 'credential_owner': owner,
            'owner_account_id': self.account_id if res.permissions.owner_scoped else None,
            'owner_scoped': bool(res.permissions.owner_scoped), 'entity_id': spec.entity_id,
            'symbol': spec.symbol, 'exchange': ex_label, 'currency': currency, 'interval': spec.interval,
            'adjustment': bars[0].adjustment, 'requested_start_date': spec.start.isoformat() if spec.start else None,
            'requested_end_date': spec.end.isoformat() if spec.end else None, 'actual_first': first, 'actual_last': last,
            'row_count': len(bars), 'is_final': cov.is_final if cov else None, 'session_status': cov.session_status if cov else None,
            'provider_response_sha256': prov.response_sha256 if prov else None,
            'retrieved_at': prov.retrieved_at.isoformat() if prov else None,
            'coverage': cov.model_dump(mode='json', exclude_none=True) if cov else None,
            'permissions': res.permissions.model_dump(mode='json'),
            'limitation': note or (
                'Provider data under the user\'s own Twelve Data key, personal use only; not shared across '
                'accounts and not exportable. Provider coverage and revisions are not independently verified.'
                if owner == 'user_byok' else
                'Provider data under the operator\'s platform Twelve Data key, allowed by the configured data policy '
                '(personal development use). Provider coverage and revisions are not independently verified.')}
        try:
            source = self._persist(
                SourceInput(title=f'{label} {spec.interval} bars ({first} to {last})', text='\n'.join(lines),
                            kind='market_data', permissions=self._source_permissions(res.permissions)),
                retrieval_method='market_data', response_sha256=provenance['provider_response_sha256'],
                response_content_type='application/json', provenance=provenance)
        except (ValueError, ValidationError):
            return FinToolOutcome({'status': 'denied', 'reason': 'source_not_storable',
                                   'message': 'The data policy does not permit storing this series.'})
        out = wire(res, source_id=source.source_id, title=source.title, total_characters=len(source.text),
                   content_sha256=source.content_sha256, rows=len(bars),
                   line_format='<SYMBOL>@<EXCHANGE> | <ts> | <interval> | O <o> H <h> L <l> C <c> V <v> <currency> [ADJF <adjustment factor>]')
        out['usage_note'] = ('Quote exact lines with read_sources and record_facts. '
                             + ('Data is owner-scoped and not exportable.' if res.permissions.owner_scoped else 'Data is not exportable.'))
        extra = []
        if cov and cov.actual.get('last') and spec.start is not None:
            extra.append(MarketSpec(spec.entity_id, spec.symbol, spec.exchange, spec.provider_exchange, spec.currency,
                                    spec.interval, spec.start, date.fromisoformat(last[:10])).key)
        return FinToolOutcome(out, source=source, extra_keys=extra)

    # -- shared SEC identity
    def _cik(self, req: Any, entities: Mapping[str, Mapping[str, Any]], sec: SecEdgarAdapter) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Return (info, failure-result). Never guesses: only an exact, unique ticker is accepted."""
        if getattr(req, 'cik', None):
            return {'cik': cik10(req.cik), 'entity_id': 'cik:' + cik10(req.cik), 'symbol': None, 'name': None}, None
        if getattr(req, 'entity_id', None):
            info = self._info(req.entity_id, entities)
            if info['cik']:
                return {'cik': info['cik'], 'entity_id': info['entity_id'], 'symbol': info['symbol'], 'name': info['name']}, None
            if info['exchange'] and normalize_exchange(info['exchange']) not in US_EXCHANGES:
                return None, {'status': 'failed', 'reason': 'no_sec_filings_for_entity',
                              'message': 'SEC data covers US-registered issuers; this entity is listed elsewhere. Use news_search and acquire_sources for its official disclosures.'}
            symbol = info['symbol']
        else:
            symbol = req.ticker
        if not symbol:
            return None, {'status': 'failed', 'reason': 'entity_not_resolved',
                          'message': 'Call resolve_entity first and pass its entity_id.'}
        found = sec.company_identity(symbol)
        if found.status not in ('ok', 'empty'):
            return None, wire(found)
        exact = [c for c in found.data if (c.symbol or '').upper() == symbol.upper()]
        if len(exact) != 1:
            return None, {'status': 'failed', 'reason': 'entity_ambiguous' if exact else 'entity_not_found',
                          'message': 'Could not identify exactly one SEC issuer; call resolve_entity and pass an entity_id.',
                          'candidates': [{'entity_id': c.entity_id, 'company': c.company_name, 'symbol': c.symbol} for c in found.data[:10]]}
        c = exact[0]
        return {'cik': c.identifiers['cik'], 'entity_id': c.entity_id, 'symbol': c.symbol, 'name': c.company_name}, None

    @staticmethod
    def _identity_key(req: Any) -> str:
        if getattr(req, 'cik', None):
            return 'cik:' + req.cik.zfill(10)
        if getattr(req, 'entity_id', None):
            return _norm_entity_id(req.entity_id)
        return 'ticker:' + (req.ticker or '').upper()

    def financials_key(self, req: CompanyFinancialsRequest) -> str:
        return '|'.join(['company_financials', self._identity_key(req), req.periods, ','.join(sorted(req.lines or LINE_NAMES)),
                         str(req.derive_q4)])

    # -- company_financials
    def company_financials(self, req: CompanyFinancialsRequest, entities: Mapping[str, Mapping[str, Any]]) -> FinToolOutcome:
        cn_code = self._cn_target(req)
        if cn_code is not None:
            return self._cn_company_financials(req, cn_code, entities)
        blocked = self._sec_blocked()
        if blocked:
            return FinToolOutcome(blocked)
        sec = self._sec()
        info, failure = self._cik(req, entities, sec)
        if failure:
            return FinToolOutcome(failure)
        period, n = PERIODS[req.periods]
        res = sec.statement_lines(info['cik'], lines=req.lines, period=period, n=n,
                                  derive_q4=req.derive_q4 and period == 'quarter', entity_id=info['entity_id'])
        taxonomy = 'us-gaap'
        if res.status == 'empty' and res.reason == 'no_matching_lines':
            alt = sec.statement_lines(info['cik'], lines=req.lines, period=period, n=n, taxonomy='ifrs-full',
                                      derive_q4=req.derive_q4 and period == 'quarter', entity_id=info['entity_id'])
            if alt.status in ('ok', 'partial'):
                res, taxonomy = alt, 'ifrs-full'
        if res.status not in ('ok', 'partial') or not res.data:
            return FinToolOutcome(wire(res))
        label = info['symbol'] or info['entity_id']
        lines = []
        for s in sorted(res.data, key=lambda x: (x.statement, x.line, x.period_end), reverse=False):
            lines.append(
                f"{label} | {s.statement}.{s.line} | {s.taxonomy}:{s.reported_tag} | "
                f"{s.period_start.isoformat() if s.period_start else ''}..{s.period_end.isoformat()} | "
                f"fiscal {s.fiscal_label or '-'} | form {s.form or '-'} filed {s.filed_at.isoformat() if s.filed_at else '-'} | "
                f"accn {s.accession or '-'} | value {_fmt(s.value)} {s.unit}"
                + (f' | derived: {s.derivation}' if s.derived else ''))
        cik = info['cik']
        prov = res.provenance
        endpoints = list(prov.endpoints) if prov and prov.endpoints else ([prov.endpoint] if prov and prov.endpoint else [])
        provenance = {
            'provider': 'sec_edgar', 'endpoint': endpoints[0] if endpoints else None, 'endpoints': endpoints,
            'provider_calls': res.cost.provider_calls, 'credential_owner': 'platform_public_data',
            'entity_id': info['entity_id'], 'cik': cik, 'taxonomy': taxonomy, 'periods': req.periods,
            'lines': sorted({s.line for s in res.data}), 'derive_q4': req.derive_q4, 'row_count': len(lines),
            'provider_response_sha256': prov.response_sha256 if prov else None,
            'retrieved_at': prov.retrieved_at.isoformat() if prov else None,
            'coverage': res.coverage.model_dump(mode='json', exclude_none=True) if res.coverage else None,
            'permissions': res.permissions.model_dump(mode='json'),
            'limitations': ['SEC XBRL facts are standardized by a tag-priority mapping; check reported_tag per line.',
                            'Filing date is not exact public availability; restatements win (latest filed value).']}
        try:
            source = self._persist(
                SourceInput(title=f'{label} SEC XBRL standardized financials ({req.periods})', text='\n'.join(lines),
                            url=endpoints[0] if endpoints else f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json', kind='official',
                            permissions=self._source_permissions(res.permissions)),
                retrieval_method='sec_companyfacts', response_sha256=provenance['provider_response_sha256'],
                response_content_type='application/json', provenance=provenance)
        except (ValueError, ValidationError):
            return FinToolOutcome({'status': 'denied', 'reason': 'source_not_storable'})
        out = wire(res, source_id=source.source_id, title=source.title, total_characters=len(source.text),
                   content_sha256=source.content_sha256, rows=len(lines), entity_id=info['entity_id'],
                   line_format='<ticker> | <statement>.<line> | <taxonomy>:<reported_tag> | <start>..<end> | fiscal <label> | form <form> filed <date> | accn <accession> | value <v> <unit>',
                   usage_note='Quote exact lines with read_sources and record_facts; period, tag, form and accession are in each line.')
        return FinToolOutcome(out, source=source, entities={info['entity_id']: {'company': info['name'], 'symbol': info['symbol'],
                                                                              'identifiers': {'cik': cik}}})

    # -- A-share financials
    def _cn_target(self, req: CompanyFinancialsRequest) -> cn_helpers.CnCode | None:
        if req.cik:
            return None
        if req.entity_id:
            return cn_helpers.parse_cn_code(req.entity_id.strip()) if not req.entity_id.lower().startswith('cik:') else None
        return cn_helpers.parse_cn_code(req.ticker)

    def _cn_company_financials(self, req: CompanyFinancialsRequest, code: cn_helpers.CnCode,
                               entities: Mapping[str, Mapping[str, Any]]) -> FinToolOutcome:
        blocked = self._blocked()
        if blocked:
            return FinToolOutcome(blocked)
        chain, unreachable = self._cn_chain_info('financials')
        if not chain:
            if unreachable:
                return self._unreachable_outcome(unreachable)
            return FinToolOutcome({'status': 'unavailable', 'reason': 'cn_financials_unavailable', 'message': CN_KEY_MESSAGE})
        self._ensure_running()
        if self._remaining() <= 0:
            return FinToolOutcome({'status': 'unavailable', 'reason': 'time_budget'})
        period, n = PERIODS[req.periods]
        derive = req.derive_q4 and period == 'quarter'
        failures: list[str] = []
        last: ToolResult | None = None
        name = ''
        res: ToolResult | None = None
        for name, adapter in chain:
            res = adapter.statement_lines(code, lines=req.lines, period=period, n=n, derive_quarters=derive,
                                          entity_id=code.entity_id)
            if res.status in ('ok', 'partial') and res.data:
                break
            last = res
            failures.append(f'{name} failed ({res.reason}).')
            res = None
        if res is None:
            out = wire(last) if last is not None else {'status': 'unavailable', 'reason': 'cn_financials_unavailable'}
            if failures:
                out['source_failures'] = failures
            return FinToolOutcome(out)
        if failures:
            res = res.model_copy(update={'coverage': res.coverage.model_copy(
                update={'notes': [*res.coverage.notes, *[f + ' Fell back to the next source.' for f in failures]]}) if res.coverage else res.coverage})
        info = self._info(code.entity_id, entities)
        label = code.code
        lines = []
        for s in sorted(res.data, key=lambda x: (x.statement, x.line, x.period_end, x.derived)):
            lines.append(
                f"{label} | {s.statement}.{s.line} | {s.taxonomy}:{s.reported_tag} | "
                f"{s.period_start.isoformat() if s.period_start else ''}..{s.period_end.isoformat()} | "
                f"fiscal {s.fiscal_label or '-'} | form {s.form or '-'} filed {s.filed_at.isoformat() if s.filed_at else '-'} | "
                f"basis {s.basis or '-'} | value {_fmt(s.value)} {s.unit}"
                + (f' | derived: {s.derivation}' if s.derived else '')
                + (f" | superseded: {'; '.join(s.superseded)}" if s.superseded else ''))
        prov = res.provenance
        endpoints = list(prov.endpoints) if prov and prov.endpoints else ([prov.endpoint] if prov and prov.endpoint else [])
        owner = self._ts_owner if name == 'tushare' else 'keyless_public_source'
        provenance = {
            'provider': name, 'endpoint': endpoints[0] if endpoints else None, 'endpoints': endpoints,
            'provider_calls': res.cost.provider_calls, 'credential_owner': owner,
            'owner_account_id': self.account_id if res.permissions.owner_scoped else None,
            'owner_scoped': bool(res.permissions.owner_scoped), 'entity_id': code.entity_id, 'taxonomy': res.data[0].taxonomy,
            'accounting_standard': cn_helpers.ACCOUNTING_STANDARD, 'currency': cn_helpers.CN_CURRENCY,
            'periods': req.periods, 'lines': sorted({s.line for s in res.data}), 'derive_quarters': derive,
            'row_count': len(lines), 'provider_response_sha256': prov.response_sha256 if prov else None,
            'retrieved_at': prov.retrieved_at.isoformat() if prov else None,
            'coverage': res.coverage.model_dump(mode='json', exclude_none=True) if res.coverage else None,
            'permissions': res.permissions.model_dump(mode='json'),
            'limitations': [
                'Third-party structured data, not the filed report itself; check key figures against the issuer\'s report.',
                'Q1/H1/9M/FY flow values are cumulative year-to-date as reported; single quarters only when marked derived.',
                'Where a period was restated the latest revision is shown; filing date is the provider\'s announcement date.']}
        try:
            source = self._persist(
                SourceInput(title=f'{label} A-share financials via {name} ({req.periods})', text='\n'.join(lines),
                            kind='official', permissions=self._source_permissions(res.permissions)),
                retrieval_method='structured_financials', response_sha256=provenance['provider_response_sha256'],
                response_content_type='application/json', provenance=provenance)
        except (ValueError, ValidationError):
            return FinToolOutcome({'status': 'denied', 'reason': 'source_not_storable'})
        out = wire(res, source_id=source.source_id, title=source.title, total_characters=len(source.text),
                   content_sha256=source.content_sha256, rows=len(lines), entity_id=code.entity_id,
                   line_format='<code> | <statement>.<line> | <taxonomy>:<provider_field> | <start>..<end> | fiscal <label> | form <report> filed <date> | basis <ytd_cumulative|single_quarter_derived|point_in_time|period_ratio|ttm> | value <v> <unit> [| derived: ...] [| superseded: <filed>:<value>]',
                   usage_note='Quote exact lines with read_sources and record_facts. Flow values are year-to-date cumulative unless basis says single_quarter_derived.',
                   provider=name)
        return FinToolOutcome(out, source=source, entities={code.entity_id: {
            'company': info['name'], 'symbol': code.code, 'exchange': code.exchange, 'currency': cn_helpers.CN_CURRENCY}})

    # -- filings
    def filings_key(self, req: FilingsRequest) -> str:
        who = self._identity_key(req) if req.action == 'list' else 'fulltext:' + (req.query or '').strip().lower()
        return '|'.join(['filings', req.action, who, ','.join(sorted(f.upper() for f in req.forms or [])),
                         req.since.isoformat() if req.since else '', req.until.isoformat() if req.until else '', str(req.limit)])

    def filings(self, req: FilingsRequest, entities: Mapping[str, Mapping[str, Any]]) -> FinToolOutcome:
        blocked = self._sec_blocked()
        if blocked:
            return FinToolOutcome(blocked)
        sec = self._sec()
        leads: list[dict[str, Any]] = []
        if req.action == 'search':
            res = sec.fulltext_search(req.query or '', start=req.since, end=req.until, forms=req.forms, limit=req.limit)
            if res.status == 'denied':
                return FinToolOutcome({'status': 'denied', 'reason': res.reason,
                                       'message': ("EDGAR full-text search is not enabled by this deployment's data policy. "
                                                   "Use filings with action='list' for a company, or news_search.")})
            rows = []
            for h in res.data:
                rows.append({k: v for k, v in {
                    'entity_id': h.entity_id, 'company': h.display_name, 'form': h.form,
                    'filed_at': h.filed_at.isoformat() if h.filed_at else None, 'accession': h.accession,
                    'primary_document_url': h.url, 'description': h.description}.items() if v})
                if h.url:
                    leads.append({'url': canonical_source_url(h.url), 'title': f'{h.display_name or h.entity_id} {h.form or ""} {h.filed_at or ""}'.strip(),
                                  'published_at': f'{h.filed_at.isoformat()}T00:00:00+00:00' if h.filed_at else None})
            return FinToolOutcome(wire(res, filings=rows, lead_note=self._lead_note(rows)), leads=leads)
        info, failure = self._cik(req, entities, sec)
        if failure:
            return FinToolOutcome(failure)
        res = sec.submissions(info['cik'], forms=req.forms, start=req.since, end=req.until, limit=req.limit,
                              entity_id=info['entity_id'])
        if res.status not in ('ok', 'partial'):
            return FinToolOutcome(wire(res))
        label = info['name'] or info['symbol'] or info['entity_id']
        rows = []
        for f in res.data:
            rows.append({k: v for k, v in {
                'form': f.form, 'filed_at': f.filed_at.isoformat(),
                'accepted_at': f.accepted_at.isoformat() if f.accepted_at else None,
                'period_of_report': f.period_of_report.isoformat() if f.period_of_report else None,
                'accession': f.accession, 'primary_document_url': f.primary_document_url, 'index_url': f.index_url,
                'description': f.description}.items() if v})
            if f.primary_document_url:
                title = f'{label} {f.form} filed {f.filed_at.isoformat()}' + (f' (period {f.period_of_report})' if f.period_of_report else '')
                leads.append({'url': canonical_source_url(f.primary_document_url), 'title': title[:300],
                              'published_at': f'{f.filed_at.isoformat()}T00:00:00+00:00'})
        return FinToolOutcome(wire(res, entity_id=info['entity_id'], filings=rows, lead_note=self._lead_note(rows)), leads=leads,
                              entities={info['entity_id']: {'company': info['name'], 'symbol': info['symbol'],
                                                            'identifiers': {'cik': info['cik']}}})

    @staticmethod
    def _lead_note(rows: list[dict[str, Any]]) -> str:
        return ('primary_document_url values can be passed directly to acquire_sources; '
                'then use outline_source / read_sources on the acquired document.') if rows else ''

    # -- Phase A1 SEC Deep & External Earnings tools
    @staticmethod
    def _release_period_lines(period: Any) -> list[str]:
        out: list[str] = [
            f"=== {period.period_role.upper()} 8-K EARNINGS RELEASE ({period.accession}) ===",
            f"filed_at: {period.filed_at.isoformat()} | period_of_report: {period.period_of_report.isoformat() if period.period_of_report else '-'} | items: {','.join(period.items) or '-'}",
            f"index_json_url: {period.index_json_url}",
        ]
        if period.gaps:
            out.append(f"period_gaps: {'; '.join(period.gaps)}")
        if period.management_summary:
            out.append("--- Management Summary ---")
            out.append(period.management_summary)
        if period.management_highlights:
            out.append("--- Management Highlights ---")
            out.extend(f"* {h}" for h in period.management_highlights)
        if period.guidance_passages:
            out.append("--- Forward Guidance Passages ---")
            for idx, gp in enumerate(period.guidance_passages, 1):
                meta = [p for p in (gp.heading, f"horizon={gp.horizon}" if gp.horizon else None, f"exhibit={gp.source_exhibit}") if p]
                out.append(f"[Guidance {idx}] {' | '.join(meta)}")
                out.append(gp.text)
        if period.nongaap_reconciliations:
            out.append("--- Non-GAAP Reconciliations ---")
            for idx, ng in enumerate(period.nongaap_reconciliations, 1):
                out.append(f"[Non-GAAP {idx}] {ng.title or 'Reconciliation'} ({ng.source_exhibit})")
                if ng.table and ng.table.raw_markdown:
                    out.append(ng.table.raw_markdown)
                elif ng.text:
                    out.append(ng.text)
        for ex in (period.ex99_1, period.ex99_2):
            if ex is None:
                continue
            out.append(f"--- {ex.exhibit_type} ({ex.filename}) ---")
            out.append(f"url: {ex.url}")
            out.append(ex.text)
        return out

    @classmethod
    def _format_earnings_release_text(cls, payload: DualPeriodEarningsRelease, label: str) -> str:
        header = [
            f"SEC Dual-Period 8-K Item 2.02 Earnings Release: {label} (CIK {payload.cik})",
            f"pairing_mode: {payload.pairing_mode} | days_between_filings: {payload.days_between_filings if payload.days_between_filings is not None else '-'}",
        ]
        if payload.gaps:
            header.append(f"gaps: {'; '.join(payload.gaps)}")
        lines = [*header, ""]
        for period in (payload.current, payload.prior):
            if period is not None:
                lines.extend(cls._release_period_lines(period))
                lines.append("")
        return "\n".join(lines).strip()

    @staticmethod
    def _format_single_table_lines(tbl: FilingTableData, *, prefix: str = "") -> list[str]:
        lines = [
            f"{prefix}Table: {tbl.title} ({tbl.r_file})",
            f"url: {tbl.url} | currency: {tbl.currency or '-'} | unit_scale: {tbl.unit_scale or '-'} | scale_multiplier: {tbl.scale_multiplier if tbl.scale_multiplier is not None else '-'}",
        ]
        if tbl.unit_header_note:
            lines.append(f"unit_note: {tbl.unit_header_note}")
        if tbl.periods:
            lines.append("periods: " + " | ".join(tbl.periods))
        if tbl.columns:
            col_headers = [c.header for c in tbl.columns]
            lines.append("line_item | " + " | ".join(col_headers))
        for row in tbl.rows:
            tag_prefix = f"[{row.xbrl_tag}] " if row.xbrl_tag else ""
            indent = "  " * min(row.indent_level, 4)
            vals = [c.raw_text for c in row.cells]
            lines.append(f"{indent}{tag_prefix}{row.label}" + (" | " + " | ".join(vals) if vals else ""))
        if tbl.narrative_text:
            lines.append("--- Footnote / Narrative ---")
            lines.append(tbl.narrative_text)
        return lines

    @classmethod
    def _format_filing_table_text(cls, tbl: FilingTableData) -> str:
        lines = cls._format_single_table_lines(tbl)
        for idx, sub in enumerate(tbl.subtables, 1):
            lines.append("")
            lines.extend(cls._format_single_table_lines(sub, prefix=f"[Subtable {idx}] "))
        return "\n".join(lines).strip()

    @staticmethod
    def _compact_table_rows(tbl: FilingTableData) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for r in tbl.rows:
            rows.append({
                'row_index': r.row_index,
                'label': r.label,
                'is_header': r.is_header,
                'is_abstract': r.is_abstract,
                'is_total': r.is_total,
                'xbrl_tag': r.xbrl_tag,
                'values_by_column': dict(r.values_by_column),
                'numeric_values': {
                    c.column_header or f'col_{c.column_index}': str(c.numeric_value)
                    for c in r.cells
                    if c.numeric_value is not None
                },
            })
        return rows

    @staticmethod
    def _sec_provenance(res: ToolResult, info: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
        prov = res.provenance
        endpoints = list(prov.endpoints) if prov and prov.endpoints else ([prov.endpoint] if prov and prov.endpoint else [])
        out: dict[str, Any] = {
            'provider': 'sec_edgar',
            'endpoint': endpoints[0] if endpoints else None,
            'endpoints': endpoints,
            'provider_calls': res.cost.provider_calls,
            'credential_owner': 'platform_public_data',
            'entity_id': info.get('entity_id'),
            'cik': info.get('cik'),
            'provider_response_sha256': prov.response_sha256 if prov else None,
            'retrieved_at': prov.retrieved_at.isoformat() if prov else None,
            'coverage': res.coverage.model_dump(mode='json', exclude_none=True) if res.coverage else None,
            'permissions': res.permissions.model_dump(mode='json'),
        }
        out.update(extra)
        return out

    def sec_material_events_key(self, req: SecMaterialEventsRequest) -> str:
        cats = ','.join(sorted(set(req.categories))) if req.categories else 'all'
        return '|'.join([
            'sec_material_events',
            self._identity_key(req),
            cats,
            str(req.lookback_days),
            req.as_of.isoformat() if req.as_of else '',
            str(req.include_suppressed),
        ])

    @staticmethod
    def _format_sec_material_events_digest(
        scan_result: Any,
        source_texts: Mapping[str, str],
        *,
        label: str,
        max_total_chars: int = 180_000,
        max_entry_chars: int = 12_000,
    ) -> tuple[str, dict[str, str]]:
        lines: list[str] = [
            f"SEC Layer 1 Material Events Scan: {label} (CIK {scan_result.cik})",
            (
                f"as_of: {scan_result.as_of.isoformat() if scan_result.as_of else '-'} | "
                f"lookback_days: {scan_result.lookback_days} | "
                f"is_fpi: {scan_result.is_fpi} | fpi_exempt_from_form4: {scan_result.fpi_exempt_from_form4} | "
                f"rules_version: {scan_result.rules_version} | parser_version: {scan_result.parser_version}"
            ),
        ]
        if scan_result.scale_anchor is not None:
            sa = scan_result.scale_anchor
            lines.append(
                f"scale_anchor: taxonomy={sa.taxonomy_used or '-'} | currency={sa.currency or '-'} | "
                f"ttm_rev={sa.latest_ttm_revenue or '-'} | net_inc={sa.latest_net_income or '-'} | "
                f"assets={sa.latest_total_assets or '-'} | float_usd={sa.public_float_usd or '-'} | "
                f"shares={sa.shares_outstanding or '-'} | basis={sa.share_unit_basis}"
            )
        if scan_result.open_gaps:
            lines.append("open_gaps: " + "; ".join(scan_result.open_gaps))
        lines.append("")
        if not scan_result.target_candidates:
            lines.append("No material non-earnings SEC event candidates matched the active window and categories.")
        else:
            lines.append(f"=== TARGET CANDIDATES ({len(scan_result.target_candidates)}) ===")
            for cand in scan_result.target_candidates:
                lines.append(
                    f"[{cand.candidate_id}] {cand.form} | filed {cand.filed_at.isoformat()} | "
                    f"event_date {cand.event_date.isoformat() if cand.event_date else '-'} | "
                    f"accn {cand.accession} | items {','.join(cand.items) or '-'} | "
                    f"critical {','.join(cand.statutory_critical_items) or '-'} | source_id {cand.source_id}"
                )
                lines.append(f"url: {cand.sec_url}")
                if cand.excerpt:
                    snippet = cand.excerpt[:max_entry_chars]
                    if len(cand.excerpt) > max_entry_chars:
                        snippet += "\n...[truncated]"
                    lines.append(snippet)
                lines.append("")

        if scan_result.recent_context_candidates:
            lines.append(f"=== RECENT CONTEXT ({len(scan_result.recent_context_candidates)}) ===")
            for ctx_c in scan_result.recent_context_candidates:
                lines.append(
                    f"[context] {ctx_c.form} | filed {ctx_c.filed_at.isoformat()} | "
                    f"accn {ctx_c.accession} | items {','.join(ctx_c.items) or '-'}"
                )
            lines.append("")

        compact_texts: dict[str, str] = {}
        compact_budget = 24_000
        compact_used = 0
        # Keep supplementary exhibit/source texts cleanly registered and bounded.
        candidate_sids = {c.source_id for c in scan_result.target_candidates}
        ordered_sids = sorted(source_texts.keys(), key=lambda s: (0 if s.endswith(':ex16') else 1 if s in candidate_sids else 2, s))
        for sid in ordered_sids:
            raw_txt = source_texts.get(sid) or ""
            if not raw_txt:
                continue
            rem_compact = compact_budget - compact_used
            if rem_compact > 0:
                clipped = raw_txt if (len(raw_txt) <= rem_compact or sid.endswith(':ex16')) else (raw_txt[: max(0, rem_compact - 24)].rstrip() + "\n...[compact cap]")
                compact_texts[sid] = clipped
                compact_used += len(clipped)
            if sid not in candidate_sids:
                lines.append(f"=== EXHIBIT / SUPPLEMENT SOURCE ({sid}) ===")
                lines.append(raw_txt[:max_entry_chars])
                lines.append("")

        full_text = "\n".join(lines).strip()
        if len(full_text) > max_total_chars:
            full_text = full_text[: max_total_chars - 32].rstrip() + "\n...[digest budget cap reached]"
        return full_text, compact_texts

    def sec_material_events(
        self,
        req: SecMaterialEventsRequest,
        entities: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> FinToolOutcome:
        blocked = self._sec_blocked()
        if blocked:
            return FinToolOutcome(blocked)
        sec = self._sec()
        info, failure = self._cik(req, entities or {}, sec)
        if failure:
            return FinToolOutcome(failure)

        try:
            self._reserve_call()
        except Exception as err:
            wait = getattr(err, 'retry_after', None)
            return FinToolOutcome({
                'status': 'rate_limited',
                'reason': 'rate_limit',
                'message': str(err),
                'retryable': True,
                **({'retry_after_seconds': float(wait)} if isinstance(wait, (int, float)) and wait > 0 else {}),
            })

        try:
            mapped_cats = normalize_sec_event_categories(req.categories)
        except ValueError as err:
            return FinToolOutcome({'status': 'failed', 'reason': 'invalid_request', 'message': str(err)})

        rem_sec = self._remaining()
        deadline_ts = (monotonic() + max(0.0, rem_sec)) if math.isfinite(rem_sec) else None
        events_limiter = self._sec_events_limiter or SecPacingLimiter()

        try:
            scan_result, source_texts = scan_sec_material_events(
                info['cik'],
                user_agent=self._sec_ua,
                as_of=req.as_of,
                lookback_days=req.lookback_days,
                categories=mapped_cats,
                l1_cache=self._l1_cache,
                transport=self._sec_transport,
                stream_transport=self._sec_stream,
                limiter=events_limiter,
                policy=self._policy,
                now_fn=self._now,
                deadline=deadline_ts,
                cancel_event=self._cancel,
                symbol=info.get('symbol'),
                entity_id=info.get('entity_id'),
            )
        except SecPacingError as err:
            if err.reason == 'sec_contact_unconfigured':
                return FinToolOutcome({'status': 'unavailable', 'reason': err.reason, 'message': err.message})
            if err.reason in ('forbidden', 'invalid_sec_url') or 'denied' in err.reason or 'policy' in err.reason:
                return FinToolOutcome({'status': 'denied', 'reason': err.reason, 'message': err.message})
            if err.reason == 'provider_rate_limited':
                return FinToolOutcome({
                    'status': 'rate_limited',
                    'reason': err.reason,
                    'message': err.message,
                    'retryable': True,
                    **({'retry_after_seconds': err.retry_after_seconds} if err.retry_after_seconds is not None else {}),
                })
            return FinToolOutcome({
                'status': 'unavailable' if err.retryable else 'failed',
                'reason': err.reason,
                'message': err.message,
                'retryable': err.retryable,
            })

        sub_gap = next((g for g in scan_result.open_gaps if g.startswith('submissions_unavailable:')), None)
        if (
            sub_gap is not None
            and not scan_result.target_candidates
            and not scan_result.recent_context_candidates
            and not scan_result.suppressed_events
            and scan_result.scale_anchor is None
        ):
            sub_reason = sub_gap.split(':', 1)[1]
            status_str = 'unavailable' if sub_reason in (
                'source_timeout', 'source_unavailable', 'provider_rate_limited', 'deadline_exceeded', 'cancelled',
            ) else 'failed'
            return FinToolOutcome({
                'status': status_str,
                'reason': sub_reason,
                'entity_id': scan_result.entity_id,
                'cik': scan_result.cik,
                'open_gaps': list(scan_result.open_gaps),
                'metrics': scan_result.metrics.model_dump(mode='json', exclude_none=True),
            })

        pol_perm = self._policy.decide('sec_edgar', 'filings')
        if isinstance(pol_perm, Denied):
            return FinToolOutcome({'status': 'denied', 'reason': pol_perm.reason, 'message': pol_perm.message})

        label = str(scan_result.company_name or scan_result.symbol or info.get('name') or info.get('symbol') or scan_result.entity_id)
        digest_text, compact_texts = self._format_sec_material_events_digest(
            scan_result,
            source_texts,
            label=label,
        )
        digest_sha = sha256(digest_text.encode('utf-8')).hexdigest()

        filed_dates = [
            c.filed_at
            for c in (*scan_result.target_candidates, *scan_result.recent_context_candidates)
        ]
        pub_day = max(filed_dates) if filed_dates else (scan_result.as_of or req.as_of or self._now().date())
        pub_dt = datetime(pub_day.year, pub_day.month, pub_day.day, tzinfo=timezone.utc)
        sub_url = f"https://data.sec.gov/submissions/CIK{scan_result.cik}.json"
        prov_meta: dict[str, Any] = {
            'provider': 'sec_edgar',
            'endpoint': sub_url,
            'endpoints': [sub_url],
            'provider_calls': scan_result.metrics.total_filings_inspected,
            'credential_owner': 'platform_public_data',
            'entity_id': scan_result.entity_id,
            'cik': scan_result.cik,
            'rules_version': scan_result.rules_version,
            'parser_version': scan_result.parser_version,
            'provider_response_sha256': digest_sha,
            'retrieved_at': self._now().isoformat(),
            'permissions': pol_perm.model_dump(mode='json'),
        }
        try:
            source = self._persist(
                SourceInput(
                    title=f"{label} SEC Material Events ({scan_result.lookback_days}d scan)",
                    text=digest_text,
                    url=sub_url,
                    kind='official',
                    published_at=pub_dt,
                    permissions=self._source_permissions(pol_perm),
                ),
                retrieval_method='sec_material_events',
                response_sha256=digest_sha,
                response_content_type='text/plain',
                provenance=prov_meta,
            )
        except (ValueError, ValidationError):
            return FinToolOutcome({'status': 'denied', 'reason': 'source_not_storable'})

        leads: list[dict[str, Any]] = []
        for cand in scan_result.target_candidates:
            leads.append({
                'url': canonical_source_url(cand.sec_url),
                'title': f"{label} {cand.form} ({cand.filed_at.isoformat()}) {','.join(cand.items)}".strip()[:300],
                'published_at': f"{cand.filed_at.isoformat()}T00:00:00+00:00",
            })

        is_partial = bool(scan_result.failed_accessions or scan_result.open_gaps)
        suppressed_out = (
            [s.model_dump(mode='json', exclude_none=True) for s in scan_result.suppressed_events]
            if req.include_suppressed
            else []
        )
        out: dict[str, Any] = {
            'status': 'partial' if is_partial else 'ok',
            'reason': 'partial_scan' if is_partial else 'ok',
            'permissions': pol_perm.model_dump(mode='json'),
            'provenance': prov_meta,
            'cost': {'provider_calls': int(scan_result.metrics.total_filings_inspected)},
            'source_id': source.source_id,
            'title': source.title,
            'total_characters': len(source.text),
            'content_sha256': source.content_sha256,
            'cik': scan_result.cik,
            'entity_id': scan_result.entity_id,
            'symbol': scan_result.symbol,
            'company_name': scan_result.company_name,
            'as_of': scan_result.as_of.isoformat() if scan_result.as_of else None,
            'lookback_days': scan_result.lookback_days,
            'is_fpi': scan_result.is_fpi,
            'fpi_exempt_from_form4': scan_result.fpi_exempt_from_form4,
            'rules_version': scan_result.rules_version,
            'parser_version': scan_result.parser_version,
            'scale_anchor': scan_result.scale_anchor.model_dump(mode='json', exclude_none=True) if scan_result.scale_anchor else None,
            'timing_context': dict(scan_result.timing_context),
            'historical_precedent': dict(scan_result.historical_precedent),
            'candidates': [c.model_dump(mode='json', exclude_none=True) for c in scan_result.target_candidates],
            'recent_context_candidates': [c.model_dump(mode='json', exclude_none=True) for c in scan_result.recent_context_candidates],
            'suppressed': suppressed_out,
            'failed': [f.model_dump(mode='json', exclude_none=True) for f in scan_result.failed_accessions],
            'processed_accessions': dict(scan_result.processed_accessions),
            'open_gaps': list(scan_result.open_gaps),
            'metrics': scan_result.metrics.model_dump(mode='json', exclude_none=True),
            'source_texts': compact_texts,
            'usage_note': 'Quote candidate or exhibit passages from source_id via outline_source / search_source / read_sources.',
        }
        return FinToolOutcome(
            out,
            source=source,
            leads=leads,
            entities={
                scan_result.entity_id: {
                    'company': scan_result.company_name or info.get('name'),
                    'symbol': scan_result.symbol or info.get('symbol'),
                    'identifiers': {'cik': scan_result.cik},
                }
            },
        )

    def earnings_release_key(self, req: SecEarningsReleaseRequest) -> str:
        return '|'.join([
            'sec_earnings_release',
            self._identity_key(req),
            req.pairing_mode,
            str(req.include_ex99_2),
            req.as_of.isoformat() if req.as_of else '',
        ])

    def sec_earnings_release(self, req: SecEarningsReleaseRequest, entities: Mapping[str, Mapping[str, Any]]) -> FinToolOutcome:
        blocked = self._sec_blocked()
        if blocked:
            return FinToolOutcome(blocked)
        sec = self._sec()
        info, failure = self._cik(req, entities, sec)
        if failure:
            return FinToolOutcome(failure)
        res = sec_deep_earnings_release(
            sec,
            info['cik'],
            as_of=req.as_of,
            pairing_mode=req.pairing_mode,
            include_ex99_2=req.include_ex99_2,
            max_candidates=req.max_candidates,
        )
        if res.status not in ('ok', 'partial') or not res.data:
            return FinToolOutcome(wire(res, entity_id=info['entity_id']))
        payload: DualPeriodEarningsRelease = res.data[0]
        label = info['name'] or info['symbol'] or info['entity_id']
        text = self._format_earnings_release_text(payload, str(label))
        cur = payload.current
        pri = payload.prior
        url = (
            cur.ex99_1.url if cur and cur.ex99_1
            else cur.index_json_url if cur
            else pri.index_json_url if pri
            else None
        )
        pub_at: datetime | None = None
        if cur is not None:
            pub_at = cur.accepted_at or datetime(cur.filed_at.year, cur.filed_at.month, cur.filed_at.day, tzinfo=timezone.utc)
        prov_meta = self._sec_provenance(
            res,
            info,
            pairing_mode=payload.pairing_mode,
            current_accession=cur.accession if cur else None,
            prior_accession=pri.accession if pri else None,
        )
        title_periods = " vs ".join(p.accession for p in (cur, pri) if p is not None)
        try:
            source = self._persist(
                SourceInput(
                    title=f"{label} SEC 8-K Item 2.02 Earnings Release ({title_periods})",
                    text=text,
                    url=url,
                    kind='official',
                    published_at=pub_at,
                    permissions=self._source_permissions(res.permissions),
                ),
                retrieval_method='sec_earnings_release',
                response_sha256=prov_meta['provider_response_sha256'],
                response_content_type='text/html',
                provenance=prov_meta,
            )
        except (ValueError, ValidationError):
            return FinToolOutcome({'status': 'denied', 'reason': 'source_not_storable'})

        def _period_summary(p: Any) -> dict[str, Any] | None:
            if p is None:
                return None
            return {
                'accession': p.accession,
                'filed_at': p.filed_at.isoformat(),
                'period_of_report': p.period_of_report.isoformat() if p.period_of_report else None,
                'ex99_1_url': p.ex99_1.url if p.ex99_1 else None,
                'ex99_2_url': p.ex99_2.url if p.ex99_2 else None,
                'nongaap_table_count': len(p.nongaap_reconciliations),
                'guidance_passage_count': len(p.guidance_passages),
                'gaps': list(p.gaps),
            }

        out = wire(
            res,
            source_id=source.source_id,
            title=source.title,
            total_characters=len(source.text),
            content_sha256=source.content_sha256,
            entity_id=info['entity_id'],
            cik=info['cik'],
            pairing_mode=payload.pairing_mode,
            current_period=_period_summary(cur),
            prior_period=_period_summary(pri),
            gaps=list(payload.gaps),
            usage_note='Use outline_source / read_sources on source_id to quote management remarks, Non-GAAP reconciliations, or guidance.',
        )
        return FinToolOutcome(
            out,
            source=source,
            entities={info['entity_id']: {'company': info['name'], 'symbol': info['symbol'], 'identifiers': {'cik': info['cik']}}},
        )

    def filing_tables_key(self, req: SecFilingTablesRequest) -> str | None:
        if not req.r_file:
            return None
        return '|'.join(['sec_filing_tables', self._identity_key(req), req.accession.strip(), req.r_file.upper()])

    def sec_filing_tables(self, req: SecFilingTablesRequest, entities: Mapping[str, Mapping[str, Any]]) -> FinToolOutcome:
        blocked = self._sec_blocked()
        if blocked:
            return FinToolOutcome(blocked)
        sec = self._sec()
        info, failure = self._cik(req, entities, sec)
        if failure:
            return FinToolOutcome(failure)

        if not req.r_file:
            res = sec_deep_filing_tables(sec, info['cik'], req.accession, category=req.category)
            if res.status not in ('ok', 'partial') or not res.data:
                return FinToolOutcome(wire(res, entity_id=info['entity_id']))
            catalog: FilingTableCatalog = res.data[0]

            def _entry(r: Any) -> dict[str, Any]:
                return {
                    'r_file': r.r_file,
                    'short_name': r.short_name,
                    'long_name': r.long_name,
                    'menu_category': r.menu_category,
                    'normalized_category': r.normalized_category,
                    'url': r.url,
                }

            out = wire(
                res,
                entity_id=info['entity_id'],
                cik=info['cik'],
                accession=catalog.accession,
                filing_summary_url=catalog.filing_summary_url,
                report_count=catalog.report_count,
                statements=[_entry(r) for r in catalog.statements],
                notes=[_entry(r) for r in catalog.notes],
                tables=[_entry(r) for r in catalog.tables],
                other=[_entry(r) for r in catalog.other],
                usage_note='Pass an r_file (for example R2.htm) to sec_filing_tables to fetch and parse that structured table.',
            )
            return FinToolOutcome(
                out,
                entities={info['entity_id']: {'company': info['name'], 'symbol': info['symbol'], 'identifiers': {'cik': info['cik']}}},
            )

        res = fetch_filing_table(sec, info['cik'], req.accession, req.r_file)
        if res.status not in ('ok', 'partial') or not res.data:
            return FinToolOutcome(wire(res, entity_id=info['entity_id']))
        tbl: FilingTableData = res.data[0]
        label = info['name'] or info['symbol'] or info['entity_id']
        text = self._format_filing_table_text(tbl)
        prov_meta = self._sec_provenance(
            res,
            info,
            accession=tbl.accession,
            r_file=tbl.r_file,
            currency=tbl.currency,
            unit_scale=tbl.unit_scale,
            scale_multiplier=tbl.scale_multiplier,
            periods=list(tbl.periods),
        )
        try:
            source = self._persist(
                SourceInput(
                    title=f"{label} SEC {tbl.accession} {tbl.r_file}: {tbl.title}",
                    text=text,
                    url=tbl.url,
                    kind='official',
                    permissions=self._source_permissions(res.permissions),
                ),
                retrieval_method='sec_filing_table',
                response_sha256=prov_meta['provider_response_sha256'],
                response_content_type='text/html',
                provenance=prov_meta,
            )
        except (ValueError, ValidationError):
            return FinToolOutcome({'status': 'denied', 'reason': 'source_not_storable'})

        out = wire(
            res,
            source_id=source.source_id,
            title=source.title,
            total_characters=len(source.text),
            content_sha256=source.content_sha256,
            entity_id=info['entity_id'],
            cik=info['cik'],
            accession=tbl.accession,
            r_file=tbl.r_file,
            table_title=tbl.title,
            currency=tbl.currency,
            unit_scale=tbl.unit_scale,
            scale_multiplier=tbl.scale_multiplier,
            periods=list(tbl.periods),
            columns=[c.model_dump(mode='json', exclude_none=True) for c in tbl.columns],
            row_count=len(tbl.rows),
            rows=self._compact_table_rows(tbl),
            subtable_count=len(tbl.subtables),
            usage_note='Use numeric_values and unit_scale directly in calculations or quote rows from source_id via read_sources.',
        )
        return FinToolOutcome(
            out,
            source=source,
            entities={info['entity_id']: {'company': info['name'], 'symbol': info['symbol'], 'identifiers': {'cik': info['cik']}}},
        )

    @staticmethod
    def _section_summaries(bundle: FilingSectionsBundle, section_keys: Sequence[str] | None = None) -> list[dict[str, Any]]:
        wanted = {k.strip() for k in (section_keys or ()) if k and k.strip()}
        out: list[dict[str, Any]] = []
        for key in bundle.section_order:
            if wanted and key not in wanted:
                continue
            sec = bundle.sections.get(key)
            if sec is None:
                continue
            out.append({
                'section_key': sec.section_key,
                'title': sec.title,
                'item_number': sec.item_number,
                'part': sec.part,
                'char_start': sec.char_start,
                'char_end': sec.char_end,
                'char_count': sec.char_count,
                'word_count': sec.word_count,
                'paragraph_count': len(sec.paragraphs),
            })
        return out

    def sec_filing_sections(
        self,
        req: SecFilingSectionsRequest,
        entities: Mapping[str, Mapping[str, Any]],
        *,
        source_by_id: Mapping[str, ResearchSource] | None = None,
    ) -> FinToolOutcome:
        if req.source_id:
            blocked = self._blocked()
            if blocked:
                return FinToolOutcome(blocked)
            src = (source_by_id or {}).get(req.source_id)
            if src is None:
                return FinToolOutcome({'status': 'failed', 'reason': 'source_not_found', 'message': f"Source {req.source_id} was not found in this run."})
            res = sec_deep_filing_sections(None, text=src.text, form=req.form)
            if res.status not in ('ok', 'partial') or not res.data:
                return FinToolOutcome(wire(res, source_id=src.source_id))
            bundle: FilingSectionsBundle = res.data[0]
            sections = self._section_summaries(bundle, req.section_keys)
            return FinToolOutcome(
                wire(
                    res,
                    source_id=src.source_id,
                    form=bundle.form,
                    section_order=[s['section_key'] for s in sections],
                    sections=sections,
                    usage_note='Use search_source or read_sources on source_id to read specific sections verbatim.',
                )
            )

        blocked = self._sec_blocked()
        if blocked:
            return FinToolOutcome(blocked)
        sec = self._sec()
        res = sec_deep_filing_sections(sec, url=req.url, form=req.form)
        if res.status not in ('ok', 'partial') or not res.data:
            return FinToolOutcome(wire(res))
        bundle = res.data[0]
        prov_info: dict[str, Any] = {'entity_id': req.entity_id, 'cik': cik10(req.cik) if req.cik else None}
        prov_meta = self._sec_provenance(res, prov_info, form=bundle.form, section_order=list(bundle.section_order))
        form_label = bundle.form or req.form or 'Filing'
        try:
            source = self._persist(
                SourceInput(
                    title=f"SEC {form_label} Sections ({len(bundle.section_order)} sections)",
                    text=bundle.clean_text,
                    url=req.url,
                    kind='official',
                    permissions=self._source_permissions(res.permissions),
                ),
                retrieval_method='sec_filing_sections',
                response_sha256=prov_meta['provider_response_sha256'],
                response_content_type='text/html',
                provenance=prov_meta,
            )
        except (ValueError, ValidationError):
            return FinToolOutcome({'status': 'denied', 'reason': 'source_not_storable'})

        sections = self._section_summaries(bundle, req.section_keys)
        out = wire(
            res,
            source_id=source.source_id,
            title=source.title,
            total_characters=len(source.text),
            content_sha256=source.content_sha256,
            form=bundle.form,
            section_order=[s['section_key'] for s in sections],
            sections=sections,
            usage_note='Pass char_start/char_end offsets from sections directly to read_sources(source_id=...) and quote verbatim.',
        )
        return FinToolOutcome(out, source=source)

    def _load_section_bundle(
        self,
        *,
        source_id: str | None,
        url: str | None,
        form: str | None,
        source_by_id: Mapping[str, ResearchSource],
    ) -> tuple[FilingSectionsBundle | None, dict[str, Any] | None]:
        if source_id:
            src = source_by_id.get(source_id)
            if src is None:
                return None, {'status': 'failed', 'reason': 'source_not_found', 'message': f"Source {source_id} was not found in this run."}
            res = sec_deep_filing_sections(None, text=src.text, form=form)
            if res.status not in ('ok', 'partial') or not res.data:
                return None, wire(res, source_id=source_id)
            return res.data[0], None
        blocked = self._sec_blocked()
        if blocked:
            return None, blocked
        res = sec_deep_filing_sections(self._sec(), url=url, form=form)
        if res.status not in ('ok', 'partial') or not res.data:
            return None, wire(res, url=url)
        return res.data[0], None

    def sec_redline_diff(
        self,
        req: SecRedlineDiffRequest,
        *,
        source_by_id: Mapping[str, ResearchSource] | None = None,
    ) -> FinToolOutcome:
        blocked = self._blocked()
        if blocked:
            return FinToolOutcome(blocked)
        pool = source_by_id or {}
        cur_bundle, cur_err = self._load_section_bundle(
            source_id=req.current_source_id,
            url=req.current_url,
            form=req.current_form,
            source_by_id=pool,
        )
        if cur_err or cur_bundle is None:
            return FinToolOutcome(cur_err or {'status': 'failed', 'reason': 'filing_load_failed'})
        pri_bundle, pri_err = self._load_section_bundle(
            source_id=req.prior_source_id,
            url=req.prior_url,
            form=req.prior_form,
            source_by_id=pool,
        )
        if pri_err or pri_bundle is None:
            return FinToolOutcome(pri_err or {'status': 'failed', 'reason': 'filing_load_failed'})

        report = sec_deep_redline_diff(
            cur_bundle,
            pri_bundle,
            section_keys=req.section_keys,
            filter_rolling_noise=req.filter_rolling_noise,
            similarity_threshold=req.similarity_threshold,
        )
        assert isinstance(report, FilingRedlineReport)
        return FinToolOutcome({
            'status': 'ok',
            'reason': 'ok',
            'filter_rolling_noise': req.filter_rolling_noise,
            'total_added': report.total_added,
            'total_removed': report.total_removed,
            'total_revised': report.total_revised,
            'total_filtered_noise': report.total_filtered_noise,
            'sections': {k: v.model_dump(mode='json') for k, v in report.section_diffs.items()},
        })

    def sec_fulltext_search(self, req: SecFulltextSearchRequest, entities: Mapping[str, Mapping[str, Any]]) -> FinToolOutcome:
        blocked = self._sec_blocked()
        if blocked:
            return FinToolOutcome(blocked)
        sec = self._sec()
        info, failure = self._cik(req, entities, sec)
        if failure:
            return FinToolOutcome(failure)
        res = cik_scoped_efts_search(
            sec,
            info['cik'],
            req.query,
            start=req.since,
            end=req.until,
            forms=req.forms,
            limit=req.limit,
        )
        if res.status == 'denied':
            return FinToolOutcome(wire(res, entity_id=info['entity_id'], cik=info['cik']))
        rows = []
        leads: list[dict[str, Any]] = []
        for h in res.data:
            rows.append({
                k: v for k, v in {
                    'entity_id': h.entity_id,
                    'company': h.display_name,
                    'form': h.form,
                    'filed_at': h.filed_at.isoformat() if h.filed_at else None,
                    'period_of_report': h.period_of_report.isoformat() if h.period_of_report else None,
                    'accession': h.accession,
                    'document': h.document,
                    'primary_document_url': h.url,
                    'description': h.description,
                }.items() if v
            })
            if h.url:
                leads.append({
                    'url': canonical_source_url(h.url),
                    'title': f'{h.display_name or h.entity_id} {h.form or ""} {h.filed_at or ""}'.strip(),
                    'published_at': f'{h.filed_at.isoformat()}T00:00:00+00:00' if h.filed_at else None,
                })
        return FinToolOutcome(
            wire(res, entity_id=info['entity_id'], cik=info['cik'], hits=rows, lead_note=self._lead_note(rows)),
            leads=leads,
            entities={info['entity_id']: {'company': info['name'], 'symbol': info['symbol'], 'identifiers': {'cik': info['cik']}}},
        )

    def _fmp_service(self) -> ExternalEarningsService:
        env: dict[str, str] = {}
        allow_plat: bool = False
        if self._fmp_key and self._fmp_owner == 'platform':
            allow_plat = True
            env = {FMP_API_KEY_ENV: self._fmp_key, FMP_ALLOW_PLATFORM_KEY_ENV: 'true'}
        adapter = FmpEarningsAdapter(
            api_key=self._fmp_key if self._fmp_owner == 'user_byok' else None,
            credential_owner=self._fmp_owner,
            account_id=self.account_id,
            allow_platform_key=allow_plat,
            env=env,
            transport=self._fmp_transport,
            limiter=self._limiter,
            policy=self._policy,
            now_fn=self._now,
        )
        return ExternalEarningsService(
            provider=adapter,
            allow_platform_key=allow_plat,
            env=env,
            account_id=self.account_id,
            policy=self._policy,
        )

    def _earnings_symbol(self, req: Any, entities: Mapping[str, Mapping[str, Any]]) -> tuple[str | None, str | None, dict[str, Any] | None]:
        if req.symbol:
            return req.symbol.strip().upper(), req.entity_id, None
        if req.entity_id:
            info = self._info(req.entity_id, entities)
            if info['symbol']:
                return str(info['symbol']).strip().upper(), info['entity_id'], None
        return None, None, {'status': 'failed', 'reason': 'entity_not_resolved', 'message': 'Supply ticker symbol or an entity_id with a ticker symbol from resolve_entity.'}

    def _external_failure_outcome(self, res: Any) -> FinToolOutcome:
        secrets = (self._fmp_key,) if self._fmp_key else ()
        status = 'denied' if res.provider_status == 'denied' else res.status
        out: dict[str, Any] = {
            'status': status,
            'reason': res.reason or 'unavailable',
            'message': redact_secrets(res.message or '', secrets),
            'degraded': bool(res.degraded),
            'provider': res.provider,
            'fallback_suggestions': list(res.fallback_suggestions),
            'suggested_actions': [a.model_dump(mode='json', exclude_none=True) for a in res.suggested_actions],
        }
        if res.retryable:
            out['retryable'] = True
        if res.provider_status:
            out['provider_status'] = res.provider_status
        return FinToolOutcome(out)

    def earnings_consensus_key(self, req: EarningsConsensusRequest) -> str:
        who = req.symbol.strip().upper() if req.symbol else _norm_entity_id(req.entity_id or '')
        return '|'.join(['earnings_consensus', who, req.period, str(req.limit), str(req.include_actuals)])

    def earnings_consensus(self, req: EarningsConsensusRequest, entities: Mapping[str, Mapping[str, Any]]) -> FinToolOutcome:
        blocked = self._blocked()
        if blocked:
            return FinToolOutcome(blocked)
        symbol, entity_id, failure = self._earnings_symbol(req, entities)
        if failure or not symbol:
            return FinToolOutcome(failure or {'status': 'failed', 'reason': 'entity_not_resolved'})
        service = self._fmp_service()
        res = service.get_consensus(
            symbol,
            period=req.period,
            limit=req.limit,
            include_actuals=req.include_actuals,
            byok_api_key=self._fmp_key if self._fmp_owner == 'user_byok' else None,
            account_id=self.account_id,
        )
        if res.status not in ('ok', 'partial') or not res.consensus:
            return self._external_failure_outcome(res)

        lines = []
        for c in res.consensus:
            period_end_str = c.period_end.isoformat() if c.period_end else '-'
            reported_str = c.reported_date.isoformat() if c.reported_date else '-'
            lines.append(
                f"{c.symbol} | period {c.fiscal_period or '-'} | period_end {period_end_str} | "
                f"reported {reported_str} | "
                f"rev_est_avg {c.revenue_estimate_avg if c.revenue_estimate_avg is not None else '-'} | "
                f"eps_est_avg {c.eps_estimate_avg if c.eps_estimate_avg is not None else '-'} | "
                f"rev_actual {c.revenue_actual if c.revenue_actual is not None else '-'} | "
                f"eps_actual {c.eps_actual if c.eps_actual is not None else '-'} | "
                f"analysts {c.num_analysts if c.num_analysts is not None else '-'}"
            )
        prov = res.provenance
        secrets = (self._fmp_key,) if self._fmp_key else ()
        endpoints = [redact_secrets(u, secrets) for u in ((prov.endpoints if prov and prov.endpoints else []) or ([prov.endpoint] if prov and prov.endpoint else []))]
        prov_meta = {
            'provider': res.provider,
            'endpoint': endpoints[0] if endpoints else None,
            'endpoints': endpoints,
            'credential_owner': res.credential_owner,
            'owner_account_id': self.account_id if res.permissions.owner_scoped else None,
            'owner_scoped': bool(res.permissions.owner_scoped),
            'symbol': symbol,
            'entity_id': entity_id,
            'period': req.period,
            'provider_response_sha256': prov.response_sha256 if prov else None,
            'retrieved_at': prov.retrieved_at.isoformat() if prov else None,
            'permissions': res.permissions.model_dump(mode='json'),
        }
        try:
            source = self._persist(
                SourceInput(
                    title=f"{symbol} Street Earnings Consensus ({req.period})",
                    text="\n".join(lines),
                    url=endpoints[0] if endpoints and not (self._fmp_key and self._fmp_key in endpoints[0]) else None,
                    kind='market_data',
                    permissions=self._source_permissions(res.permissions),
                ),
                retrieval_method='fmp_consensus',
                response_sha256=prov_meta['provider_response_sha256'],
                response_content_type='application/json',
                provenance=prov_meta,
            )
        except (ValueError, ValidationError):
            return FinToolOutcome({'status': 'denied', 'reason': 'source_not_storable'})

        return FinToolOutcome({
            'status': res.status,
            'reason': res.reason or 'ok',
            'source_id': source.source_id,
            'title': source.title,
            'total_characters': len(source.text),
            'content_sha256': source.content_sha256,
            'symbol': symbol,
            'provider': res.provider,
            'credential_owner': res.credential_owner,
            'permissions': res.permissions.model_dump(mode='json'),
            'rows': len(res.consensus),
            'consensus': [c.model_dump(mode='json') for c in res.consensus],
            'usage_note': 'Quote lines via read_sources / record_facts. Data is not exportable.' + (' Data is owner-scoped.' if res.permissions.owner_scoped else ''),
        }, source=source)

    def earnings_transcript_key(self, req: EarningsTranscriptRequest) -> str:
        who = req.symbol.strip().upper() if req.symbol else _norm_entity_id(req.entity_id or '')
        return '|'.join(['earnings_transcript', who, str(req.fiscal_year or ''), str(req.fiscal_quarter or '')])

    def earnings_transcript(self, req: EarningsTranscriptRequest, entities: Mapping[str, Mapping[str, Any]]) -> FinToolOutcome:
        blocked = self._blocked()
        if blocked:
            return FinToolOutcome(blocked)
        symbol, entity_id, failure = self._earnings_symbol(req, entities)
        if failure or not symbol:
            return FinToolOutcome(failure or {'status': 'failed', 'reason': 'entity_not_resolved'})
        service = self._fmp_service()
        res = service.get_transcript(
            symbol,
            fiscal_year=req.fiscal_year,
            fiscal_quarter=req.fiscal_quarter,
            byok_api_key=self._fmp_key if self._fmp_owner == 'user_byok' else None,
            account_id=self.account_id,
        )
        if res.status not in ('ok', 'partial') or res.transcript is None:
            return self._external_failure_outcome(res)

        tr = res.transcript
        header = [
            f"{tr.symbol} Earnings Call Transcript (FY{tr.fiscal_year or '-'} Q{tr.fiscal_quarter or '-'})",
            f"call_date: {tr.call_date.isoformat() if tr.call_date else '-'} | speakers: {', '.join(tr.speakers) or '-'}",
            "",
        ]
        body: list[str] = []
        for idx, sec_item in enumerate(tr.sections, 1):
            label_parts = [f"[{idx}] {sec_item.name.upper()}"]
            if sec_item.title:
                label_parts.append(sec_item.title)
            if sec_item.speakers:
                label_parts.append(f"speakers: {', '.join(sec_item.speakers)}")
            body.append(" | ".join(label_parts))
            body.append(sec_item.text)
            body.append("")
        text = "\n".join(header + (body if body else [tr.full_text])).strip()
        prov = res.provenance
        secrets = (self._fmp_key,) if self._fmp_key else ()
        endpoints = [redact_secrets(u, secrets) for u in ((prov.endpoints if prov and prov.endpoints else []) or ([prov.endpoint] if prov and prov.endpoint else []))]
        prov_meta = {
            'provider': res.provider,
            'endpoint': endpoints[0] if endpoints else None,
            'endpoints': endpoints,
            'credential_owner': res.credential_owner,
            'owner_account_id': self.account_id if res.permissions.owner_scoped else None,
            'owner_scoped': bool(res.permissions.owner_scoped),
            'symbol': tr.symbol,
            'entity_id': entity_id,
            'fiscal_year': tr.fiscal_year,
            'fiscal_quarter': tr.fiscal_quarter,
            'provider_response_sha256': prov.response_sha256 if prov else None,
            'retrieved_at': prov.retrieved_at.isoformat() if prov else None,
            'permissions': res.permissions.model_dump(mode='json'),
        }
        pub_dt: datetime | None = None
        if isinstance(tr.call_date, datetime):
            pub_dt = tr.call_date if tr.call_date.utcoffset() is not None else tr.call_date.replace(tzinfo=timezone.utc)
        elif isinstance(tr.call_date, date):
            pub_dt = datetime(tr.call_date.year, tr.call_date.month, tr.call_date.day, tzinfo=timezone.utc)
        try:
            source = self._persist(
                SourceInput(
                    title=f"{tr.symbol} Earnings Call Transcript (FY{tr.fiscal_year or '-'} Q{tr.fiscal_quarter or '-'})",
                    text=text,
                    url=endpoints[0] if endpoints and not (self._fmp_key and self._fmp_key in endpoints[0]) else None,
                    kind='market_data',
                    published_at=pub_dt,
                    permissions=self._source_permissions(res.permissions),
                ),
                retrieval_method='fmp_transcript',
                response_sha256=prov_meta['provider_response_sha256'],
                response_content_type='application/json',
                provenance=prov_meta,
            )
        except (ValueError, ValidationError):
            return FinToolOutcome({'status': 'denied', 'reason': 'source_not_storable'})

        return FinToolOutcome({
            'status': res.status,
            'reason': res.reason or 'ok',
            'source_id': source.source_id,
            'title': source.title,
            'total_characters': len(source.text),
            'content_sha256': source.content_sha256,
            'symbol': tr.symbol,
            'fiscal_year': tr.fiscal_year,
            'fiscal_quarter': tr.fiscal_quarter,
            'call_date': tr.call_date.isoformat() if tr.call_date else None,
            'section_count': len(tr.sections),
            'speakers': list(tr.speakers),
            'provider': res.provider,
            'credential_owner': res.credential_owner,
            'permissions': res.permissions.model_dump(mode='json'),
            'usage_note': 'Use outline_source / read_sources on source_id to quote prepared remarks or Q&A. Data is not exportable.',
        }, source=source)

