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
import re
from datetime import date, datetime, timezone
from typing import Any, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .fin_data import cn as cn_helpers
from .fin_data.adapters.baostock import BaoStockAdapter, default_runner as baostock_default_runner
from .fin_data.adapters.sec_edgar import STANDARD_LINES, SecEdgarAdapter, cik10
from .fin_data.adapters.tushare import TushareAdapter
from .fin_data.adapters.twelve_data import TwelveDataAdapter
from .fin_data.adapters.yahoo import YahooAdapter
from .fin_data.reachability import UNREACHABLE_REASON, ReachabilityProbe, default_probe, unreachable_message
from .fin_data.entity import MARKETS, parse_entity_id, resolve_cn, resolve_entity as _resolve_entity, wants_cn
from .fin_data.models import INTERVALS, INTRADAY_INTERVALS, Permissions, ToolResult, normalize_exchange
from .fin_data.policy import FinDataPolicy, load_policy
from .fin_data.ratelimit import RateDecision, default_limiter
from .research_clock import exchange_session
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
                 on_result: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.account_id, self._market_key = account_id, market_key or None
        self._owner = credential_owner if credential_owner in ('platform', 'user_byok') else 'user_byok'
        try:
            self._sec_ua: str | None = validate_user_agent(sec_user_agent)
        except SecFailure:
            self._sec_ua = None
        self._persist, self._remaining, self._ensure_running = persist, remaining_seconds, ensure_running
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

