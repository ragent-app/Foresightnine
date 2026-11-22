"""Deterministic market-series analytics consumed from persisted market_data / historical_prices sources."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re
from typing import Any, Literal, Mapping
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .research_clock import EXCHANGES
from .research_historical_prices import _shift_months
from .research_source_policy import is_fred_source
from .research_sources import ResearchSource

PriceBasis = Literal["adjusted", "raw"]

PERIOD_KEYS = ("5d", "1m", "3m", "6m", "ytd", "1y", "3y")

_BAR_LINE_RE = re.compile(
    r"^(?P<instrument>[^\s|]+)\s*\|\s*"
    r"(?P<ts>[^|]+?)\s*\|\s*"
    r"(?P<interval>[^|]+?)\s*\|\s*"
    r"O\s+(?P<open>[^\s]+)\s+"
    r"H\s+(?P<high>[^\s]+)\s+"
    r"L\s+(?P<low>[^\s]+)\s+"
    r"C\s+(?P<close>[^\s]+)\s+"
    r"V\s+(?P<volume>[^\s]+)"
    r"(?:\s+(?P<tail>.*))?$"
)

_HIST_PRICE_LINE_RE = re.compile(
    r"^(?P<instrument>[^\s|]+)\s*\|\s*"
    r"(?P<date>\d{4}-\d{2}-\d{2})\s*\|\s*"
    r"(?P<series_type>[^|]+?)\s*\|\s*"
    r"(?P<close>[^\s]+)"
    r"(?:\s+(?P<unit>.+))?$"
)

_DAILY_INTERVALS = frozenset({"1d", "d", "day", "daily", "daily close"})


class MarketAnalyticsError(ValueError):
    """Raised when market series inputs violate provenance, permission, as_of, or data rules."""


class AnalyzeMarketSeriesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source_id: str = Field(
        min_length=1,
        max_length=200,
        description="Persisted market_data or historical_prices source ID for the target instrument.",
    )
    benchmark_source_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Optional persisted benchmark market_data or historical_prices source ID for excess return and beta.",
    )
    start_date: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Optional inclusive start date YYYY-MM-DD filter.",
    )
    end_date: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Optional inclusive end date YYYY-MM-DD filter (also clamped to as_of).",
    )
    price_basis: PriceBasis = Field(
        default="adjusted",
        description="Use forward-adjusted closes when ADJF is available ('adjusted') or raw unadjusted closes ('raw').",
    )
    decimal_places: int = Field(default=2, ge=0, le=6)

    @model_validator(mode="after")
    def _validate_dates(self) -> "AnalyzeMarketSeriesRequest":
        if self.start_date is not None:
            date.fromisoformat(self.start_date)
        if self.end_date is not None:
            date.fromisoformat(self.end_date)
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date.")
        return self


@dataclass(frozen=True)
class _ParsedBar:
    instrument: str
    symbol: str
    exchange: str | None
    bar_date: date
    bar_dt: datetime | None
    interval: str
    raw_close: Decimal
    adj_factor: Decimal | None
    volume: Decimal | None
    currency: str | None
    close: Decimal = Decimal("0")


def _plain_decimal_str(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _quantize_str(value: Decimal, decimal_places: int) -> str:
    quant = Decimal("1") if decimal_places == 0 else Decimal(f"1e-{decimal_places}")
    with localcontext() as ctx:
        ctx.prec = 100
        ctx.rounding = ROUND_HALF_UP
        rounded = value.quantize(quant, rounding=ROUND_HALF_UP)
        if rounded == 0:
            rounded = Decimal("0").quantize(quant)
        return format(rounded, "f")


def _parse_decimal(raw: str, *, field_name: str, positive: bool = False, non_negative: bool = False) -> Decimal:
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation as exc:
        raise MarketAnalyticsError(f"Invalid decimal value for {field_name}: {raw!r}.") from exc
    if not value.is_finite():
        raise MarketAnalyticsError(f"Non-finite decimal value for {field_name}: {raw!r}.")
    if positive and value <= 0:
        raise MarketAnalyticsError(f"{field_name} must be positive; got {raw!r}.")
    if non_negative and value < 0:
        raise MarketAnalyticsError(f"{field_name} must be non-negative; got {raw!r}.")
    return value


def _split_instrument(instrument: str, provenance: Mapping[str, Any]) -> tuple[str, str | None]:
    if "@" in instrument:
        sym, ex = instrument.split("@", 1)
        return sym.strip(), (ex.strip().upper() or None)
    prov_exchange = provenance.get("exchange")
    ex = str(prov_exchange).strip().upper() if prov_exchange else None
    return instrument.strip(), (ex or None)


def _parse_timestamp(raw_ts: str) -> tuple[date, datetime | None]:
    raw_ts = raw_ts.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_ts):
        return date.fromisoformat(raw_ts), None
    normalized = raw_ts.replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise MarketAnalyticsError(f"Unsupported timestamp format in market bar: {raw_ts!r}.") from exc
    if dt.tzinfo is None:
        return dt.date(), None
    return dt.date(), dt


def _parse_tail_tokens(tail: str | None) -> tuple[str | None, Decimal | None]:
    if not tail:
        return None, None
    tokens = tail.strip().split()
    currency: str | None = None
    adj_factor: Decimal | None = None
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok == "ADJF" and idx + 1 < len(tokens):
            adj_factor = _parse_decimal(tokens[idx + 1], field_name="ADJF", positive=True)
            idx += 2
            continue
        if re.fullmatch(r"[A-Z]{3}", tok) and currency is None:
            currency = tok
        idx += 1
    return currency, adj_factor


def _parse_source_bars(source: ResearchSource) -> list[_ParsedBar]:
    provenance = source.provenance if isinstance(source.provenance, Mapping) else {}
    bars: list[_ParsedBar] = []
    instruments: set[str] = set()
    for raw_line in source.text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        bar_match = _BAR_LINE_RE.match(line)
        if bar_match:
            instrument = bar_match.group("instrument").strip()
            interval = bar_match.group("interval").strip().lower()
            if interval not in _DAILY_INTERVALS:
                raise MarketAnalyticsError(
                    f"Source {source.source_id!r} uses unsupported interval {bar_match.group('interval')!r}; daily bars ('1d') are required."
                )
            bar_date, bar_dt = _parse_timestamp(bar_match.group("ts"))
            close = _parse_decimal(bar_match.group("close"), field_name="close", positive=True)
            volume = _parse_decimal(bar_match.group("volume"), field_name="volume", non_negative=True)
            # Validate O/H/L syntactically as finite numbers too.
            _parse_decimal(bar_match.group("open"), field_name="open", positive=True)
            _parse_decimal(bar_match.group("high"), field_name="high", positive=True)
            _parse_decimal(bar_match.group("low"), field_name="low", positive=True)
            currency, adj_factor = _parse_tail_tokens(bar_match.group("tail"))
            symbol, exchange = _split_instrument(instrument, provenance)
            instruments.add(instrument)
            bars.append(
                _ParsedBar(
                    instrument=instrument,
                    symbol=symbol,
                    exchange=exchange,
                    bar_date=bar_date,
                    bar_dt=bar_dt,
                    interval="1d",
                    raw_close=close,
                    adj_factor=adj_factor,
                    volume=volume,
                    currency=currency or (str(provenance.get("currency")).upper() if provenance.get("currency") else None),
                )
            )
            continue

        hist_match = _HIST_PRICE_LINE_RE.match(line)
        if hist_match:
            series_type = hist_match.group("series_type").strip().lower()
            if series_type != "daily close":
                raise MarketAnalyticsError(
                    f"Source {source.source_id!r} contains unsupported series type {hist_match.group('series_type')!r}; expected 'daily close'."
                )
            instrument = hist_match.group("instrument").strip()
            bar_date = date.fromisoformat(hist_match.group("date"))
            close = _parse_decimal(hist_match.group("close"), field_name="close", positive=True)
            unit_raw = (hist_match.group("unit") or "").strip()
            currency = unit_raw.upper() if re.fullmatch(r"[A-Za-z]{3}", unit_raw) else (
                str(provenance.get("currency")).upper() if provenance.get("currency") else (unit_raw or None)
            )
            symbol, exchange = _split_instrument(instrument, provenance)
            instruments.add(instrument)
            bars.append(
                _ParsedBar(
                    instrument=instrument,
                    symbol=symbol,
                    exchange=exchange,
                    bar_date=bar_date,
                    bar_dt=None,
                    interval="1d",
                    raw_close=close,
                    adj_factor=None,
                    volume=None,
                    currency=currency,
                )
            )
            continue

        if "|" in line and any(ch.isdigit() for ch in line):
            raise MarketAnalyticsError(
                f"Malformed market data row in source {source.source_id!r}: {line[:120]!r}."
            )

    if not bars:
        raise MarketAnalyticsError(
            f"Source {source.source_id!r} does not contain any parseable daily market_data or historical_prices bars."
        )
    if len(instruments) > 1:
        raise MarketAnalyticsError(
            f"Source {source.source_id!r} mixes multiple instruments ({sorted(instruments)}); single-instrument source required."
        )
    return bars


def _exchange_cutoff_date(as_of: datetime, exchange: str | None) -> date:
    if exchange and exchange in EXCHANGES:
        return as_of.astimezone(ZoneInfo(EXCHANGES[exchange].timezone)).date()
    return as_of.date()


def _validate_and_load_series(
    source_id: str,
    sources: Mapping[str, ResearchSource],
    *,
    as_of: datetime | None,
    allow_simulated: bool,
    start_date: str | None,
    end_date: str | None,
    price_basis: PriceBasis,
) -> tuple[ResearchSource, list[_ParsedBar], dict[str, Any]]:
    source = sources.get(source_id)
    if source is None:
        raise MarketAnalyticsError(f"Unknown source_id {source_id!r}.")
    if source.url is not None and is_fred_source(str(source.url)):
        raise MarketAnalyticsError(f"Source {source_id!r} is disallowed under FRED policy restrictions.")
    if not allow_simulated and source.is_simulated:
        raise MarketAnalyticsError(f"Source {source_id!r} is simulated and allow_simulated is False.")
    if not source.permissions.model_input or not source.permissions.display:
        raise MarketAnalyticsError(
            f"Source {source_id!r} lacks required model_input/display permissions for market analytics."
        )
    if source.kind != "market_data" and source.retrieval_method not in ("market_data", "historical_prices"):
        raise MarketAnalyticsError(
            f"Source {source_id!r} has kind={source.kind!r} / retrieval_method={source.retrieval_method!r}; "
            "analyze_market_series only accepts persisted market_data or historical_prices sources."
        )

    if as_of is not None:
        if source.published_at is not None and source.published_at > as_of:
            raise MarketAnalyticsError(f"Source {source_id!r} was published after as_of.")
        if source.revised_at is not None and source.revised_at > as_of:
            raise MarketAnalyticsError(f"Source {source_id!r} was revised after as_of.")

    parsed = _parse_source_bars(source)

    # Sort chronologically and ensure one bar per date (keep last if identical timestamp ordering).
    by_date: dict[date, _ParsedBar] = {}
    for bar in sorted(parsed, key=lambda b: (b.bar_date, b.bar_dt or datetime.min.replace(tzinfo=timezone.utc))):
        by_date[bar.bar_date] = bar
    chronological = [by_date[d] for d in sorted(by_date)]

    provenance = source.provenance if isinstance(source.provenance, Mapping) else {}
    source_exchange = chronological[-1].exchange or (
        str(provenance.get("exchange")).strip().upper() if provenance.get("exchange") else None
    )

    clamped_to_as_of = False
    excluded_post_as_of_rows = 0
    as_of_cutoff: date | None = None
    if as_of is not None:
        as_of_cutoff = _exchange_cutoff_date(as_of, source_exchange)
        eligible_bars: list[_ParsedBar] = []
        for bar in chronological:
            if bar.bar_dt is not None:
                if bar.bar_dt <= as_of and bar.bar_date <= as_of_cutoff:
                    eligible_bars.append(bar)
                else:
                    excluded_post_as_of_rows += 1
                    clamped_to_as_of = True
            else:
                if bar.bar_date <= as_of_cutoff:
                    eligible_bars.append(bar)
                else:
                    excluded_post_as_of_rows += 1
                    clamped_to_as_of = True
        chronological = eligible_bars
        if not chronological:
            raise MarketAnalyticsError(
                f"Source {source_id!r} has no market bars on or before as_of ({as_of_cutoff.isoformat()})."
            )

    if start_date is not None:
        start_d = date.fromisoformat(start_date)
        chronological = [b for b in chronological if b.bar_date >= start_d]
    if end_date is not None:
        end_d = date.fromisoformat(end_date)
        chronological = [b for b in chronological if b.bar_date <= end_d]
    if not chronological:
        raise MarketAnalyticsError(
            f"Source {source_id!r} has no market bars within requested window [{start_date}, {end_date}]."
        )

    source_adjustment = str(provenance.get("adjustment") or "").strip().lower() or None
    provider = str(provenance.get("provider") or "").strip().lower() or None

    adj_factors = [b.adj_factor for b in chronological if b.adj_factor is not None]
    has_full_adjf = len(adj_factors) == len(chronological)
    if adj_factors and not has_full_adjf:
        raise MarketAnalyticsError(
            f"Source {source_id!r} has ADJF on some bars but not all; cannot mix adjusted and unadjusted rows."
        )

    if price_basis == "raw":
        if source_adjustment in ("all", "dividends", "forward", "backward") and not has_full_adjf:
            raise MarketAnalyticsError(
                f"Source {source_id!r} already carries provider adjustment={source_adjustment!r} without raw unadjusted closes."
            )
        adjustment_method = "none_raw_close"
        price_basis_applied = "raw"
        resolved = [
            _ParsedBar(**{**b.__dict__, "close": b.raw_close})
            for b in chronological
        ]
    else:
        if has_full_adjf:
            last_adj = chronological[-1].adj_factor
            assert last_adj is not None and last_adj > 0
            with localcontext() as ctx:
                ctx.prec = 100
                resolved = [
                    _ParsedBar(
                        **{
                            **b.__dict__,
                            "close": b.raw_close * (b.adj_factor / last_adj),  # type: ignore[operator]
                        }
                    )
                    for b in chronological
                ]
            adjustment_method = (
                "adj_factor_total_return_normalized_to_end"
                if source_adjustment == "splits" or provider == "yahoo"
                else "adj_factor_forward_normalized_to_end"
            )
            price_basis_applied = "adjusted"
        elif source_adjustment in ("all", "dividends", "splits", "forward", "backward"):
            adjustment_method = f"provider_pre_adjusted_{source_adjustment}"
            price_basis_applied = "adjusted"
            resolved = [
                _ParsedBar(**{**b.__dict__, "close": b.raw_close})
                for b in chronological
            ]
        else:
            adjustment_method = "unadjusted_close_only"
            price_basis_applied = "unadjusted"
            resolved = [
                _ParsedBar(**{**b.__dict__, "close": b.raw_close})
                for b in chronological
            ]

    meta = {
        "source_id": source.source_id,
        "instrument": resolved[-1].instrument,
        "symbol": resolved[-1].symbol,
        "exchange": source_exchange,
        "interval": "1d",
        "currency": resolved[-1].currency,
        "retrieval_method": source.retrieval_method,
        "kind": source.kind,
        "provider": provider,
        "source_adjustment": source_adjustment,
        "price_basis_requested": price_basis,
        "price_basis_applied": price_basis_applied,
        "adjustment_method": adjustment_method,
        "adj_factor_present": has_full_adjf,
        "adj_factor_min": _plain_decimal_str(min(adj_factors)) if adj_factors else None,
        "adj_factor_max": _plain_decimal_str(max(adj_factors)) if adj_factors else None,
        "as_of": as_of.isoformat() if as_of else None,
        "as_of_cutoff_date": as_of_cutoff.isoformat() if as_of_cutoff else None,
        "clamped_to_as_of": clamped_to_as_of,
        "excluded_post_as_of_rows": excluded_post_as_of_rows,
        "published_at": source.published_at.isoformat() if source.published_at else None,
        "revised_at": source.revised_at.isoformat() if source.revised_at else None,
        "obtained_at": source.obtained_at.isoformat(),
    }
    return source, resolved, meta


def _find_anchor_bar(bars: list[_ParsedBar], target_date: date) -> tuple[_ParsedBar | None, str | None]:
    # Latest bar on or before target_date, within <= 14 calendar days so sparse data holes do not distort periods.
    candidates = [b for b in bars if b.bar_date <= target_date]
    if candidates:
        anchor = candidates[-1]
        if (target_date - anchor.bar_date).days > 14:
            return None, "insufficient_continuous_history"
        return anchor, None
    # Allow <= 3 day weekend/holiday offset only at the beginning of a series (e.g. target falls on Sunday).
    if bars and (bars[0].bar_date - target_date).days <= 3:
        return bars[0], None
    return None, "insufficient_history"


def _compute_period_returns(
    bars: list[_ParsedBar],
    decimal_places: int,
) -> tuple[dict[str, dict[str, Any] | None], dict[str, str]]:
    returns: dict[str, dict[str, Any] | None] = {p: None for p in PERIOD_KEYS}
    unavailable: dict[str, str] = {}
    end_bar = bars[-1]

    def _entry(start_bar: _ParsedBar) -> dict[str, Any]:
        with localcontext() as ctx:
            ctx.prec = 100
            ctx.rounding = ROUND_HALF_UP
            ret = ((end_bar.close / start_bar.close) - Decimal("1")) * Decimal("100")
        return {
            "return_percent": _quantize_str(ret, decimal_places),
            "start_date": start_bar.bar_date.isoformat(),
            "end_date": end_bar.bar_date.isoformat(),
            "start_close": _quantize_str(start_bar.close, decimal_places),
            "end_close": _quantize_str(end_bar.close, decimal_places),
        }

    # 5d trading-session return.
    if len(bars) >= 6:
        anchor_5d = bars[-6]
        if (end_bar.bar_date - anchor_5d.bar_date).days <= 21:
            returns["5d"] = _entry(anchor_5d)
        else:
            unavailable["5d"] = "insufficient_continuous_history"
    else:
        unavailable["5d"] = "insufficient_history"

    # Calendar month/year lookbacks: 1m, 3m, 6m, 1y, 3y.
    month_map = {"1m": 1, "3m": 3, "6m": 6, "1y": 12, "3y": 36}
    for key, months in month_map.items():
        target = _shift_months(end_bar.bar_date, months)
        anchor, reason = _find_anchor_bar(bars, target)
        if anchor is None or anchor.bar_date >= end_bar.bar_date:
            unavailable[key] = reason or "insufficient_history"
        else:
            returns[key] = _entry(anchor)

    # YTD return anchored on prior year's last available close.
    jan_1 = date(end_bar.bar_date.year, 1, 1)
    prior_year_bars = [b for b in bars if b.bar_date < jan_1]
    if prior_year_bars:
        ytd_anchor = prior_year_bars[-1]
        if ytd_anchor.bar_date.year == end_bar.bar_date.year - 1:
            returns["ytd"] = _entry(ytd_anchor)
        else:
            unavailable["ytd"] = "prior_year_close_unavailable"
    else:
        unavailable["ytd"] = "prior_year_close_unavailable"

    return returns, unavailable


def _compute_cagr(
    bars: list[_ParsedBar],
    decimal_places: int,
) -> tuple[str | None, str | None, str | None]:
    if len(bars) < 2:
        return None, None, "insufficient_history"
    span_days = (bars[-1].bar_date - bars[0].bar_date).days
    if span_days < 30:
        return None, None, "span_under_30_days"
    with localcontext() as ctx:
        ctx.prec = 100
        ctx.rounding = ROUND_HALF_UP
        years = Decimal(span_days) / Decimal("365.25")
        ratio = bars[-1].close / bars[0].close
        cagr = ((ratio.ln(ctx) / years).exp(ctx) - Decimal("1")) * Decimal("100")
    return _quantize_str(cagr, decimal_places), _quantize_str(years, 4), None


def _daily_simple_returns(bars: list[_ParsedBar]) -> list[Decimal]:
    with localcontext() as ctx:
        ctx.prec = 100
        return [(bars[i].close / bars[i - 1].close) - Decimal("1") for i in range(1, len(bars))]


def _compute_volatility(
    bars: list[_ParsedBar],
    decimal_places: int,
    *,
    periods_per_year: int = 252,
) -> tuple[str | None, int, str | None]:
    daily_returns = _daily_simple_returns(bars)
    n = len(daily_returns)
    if n < 2:
        return None, n, "insufficient_return_observations"
    with localcontext() as ctx:
        ctx.prec = 100
        ctx.rounding = ROUND_HALF_UP
        mean = sum(daily_returns, Decimal("0")) / Decimal(n)
        variance = sum(((r - mean) * (r - mean) for r in daily_returns), Decimal("0")) / Decimal(n - 1)
        daily_vol = variance.sqrt(ctx)
        annualized = daily_vol * Decimal(periods_per_year).sqrt(ctx) * Decimal("100")
    return _quantize_str(annualized, decimal_places), n, None


def _compute_max_drawdown(bars: list[_ParsedBar], decimal_places: int) -> dict[str, Any]:
    with localcontext() as ctx:
        ctx.prec = 100
        ctx.rounding = ROUND_HALF_UP
        peak_bar = bars[0]
        worst_dd = Decimal("0")
        worst_peak = bars[0]
        worst_trough = bars[0]
        worst_trough_idx = 0
        for idx, bar in enumerate(bars):
            if bar.close > peak_bar.close:
                peak_bar = bar
            dd = (bar.close / peak_bar.close) - Decimal("1")
            if dd < worst_dd:
                worst_dd = dd
                worst_peak = peak_bar
                worst_trough = bar
                worst_trough_idx = idx

        recovery_date: str | None = None
        recovered = worst_dd == 0
        if worst_dd < 0:
            for bar in bars[worst_trough_idx + 1 :]:
                if bar.close >= worst_peak.close:
                    recovery_date = bar.bar_date.isoformat()
                    recovered = True
                    break

        return {
            "drawdown_percent": _quantize_str(worst_dd * Decimal("100"), decimal_places),
            "peak_date": worst_peak.bar_date.isoformat(),
            "peak_close": _quantize_str(worst_peak.close, decimal_places),
            "trough_date": worst_trough.bar_date.isoformat(),
            "trough_close": _quantize_str(worst_trough.close, decimal_places),
            "recovered": recovered,
            "recovery_date": recovery_date,
        }


def _compute_technicals(bars: list[_ParsedBar], decimal_places: int) -> dict[str, Any]:
    with localcontext() as ctx:
        ctx.prec = 100
        ctx.rounding = ROUND_HALF_UP
        last_close = bars[-1].close

        def _sma(window: int) -> tuple[str | None, str | None]:
            if len(bars) < window:
                return None, None
            avg = sum((b.close for b in bars[-window:]), Decimal("0")) / Decimal(window)
            diff_pct = ((last_close / avg) - Decimal("1")) * Decimal("100")
            return _quantize_str(avg, decimal_places), _quantize_str(diff_pct, decimal_places)

        sma_20, price_vs_sma20 = _sma(20)
        sma_60, price_vs_sma60 = _sma(60)

        rsi_14: str | None = None
        if len(bars) >= 15:
            deltas = [bars[i].close - bars[i - 1].close for i in range(1, len(bars))]
            init_gains = [d if d > 0 else Decimal("0") for d in deltas[:14]]
            init_losses = [-d if d < 0 else Decimal("0") for d in deltas[:14]]
            avg_gain = sum(init_gains, Decimal("0")) / Decimal("14")
            avg_loss = sum(init_losses, Decimal("0")) / Decimal("14")
            for d in deltas[14:]:
                gain = d if d > 0 else Decimal("0")
                loss = -d if d < 0 else Decimal("0")
                avg_gain = (avg_gain * Decimal("13") + gain) / Decimal("14")
                avg_loss = (avg_loss * Decimal("13") + loss) / Decimal("14")
            if avg_loss == 0 and avg_gain == 0:
                rsi = Decimal("50")
            elif avg_loss == 0:
                rsi = Decimal("100")
            else:
                rs = avg_gain / avg_loss
                rsi = Decimal("100") - (Decimal("100") / (Decimal("1") + rs))
            rsi_14 = _quantize_str(rsi, decimal_places)

        return {
            "sma_20": sma_20,
            "price_vs_sma20_percent": price_vs_sma20,
            "sma_60": sma_60,
            "price_vs_sma60_percent": price_vs_sma60,
            "rsi_14": rsi_14,
        }


def _compute_benchmark_comparison(
    asset_bars: list[_ParsedBar],
    bench_bars: list[_ParsedBar],
    bench_meta: dict[str, Any],
    decimal_places: int,
) -> dict[str, Any]:
    asset_map = {b.bar_date: b for b in asset_bars}
    bench_map = {b.bar_date: b for b in bench_bars}
    common_dates = sorted(set(asset_map) & set(bench_map))
    if len(common_dates) < 2:
        raise MarketAnalyticsError(
            "Asset and benchmark series have fewer than 2 overlapping trading dates in the requested window."
        )

    aligned_asset = [asset_map[d] for d in common_dates]
    aligned_bench = [bench_map[d] for d in common_dates]

    with localcontext() as ctx:
        ctx.prec = 100
        ctx.rounding = ROUND_HALF_UP
        asset_cum = ((aligned_asset[-1].close / aligned_asset[0].close) - Decimal("1")) * Decimal("100")
        bench_cum = ((aligned_bench[-1].close / aligned_bench[0].close) - Decimal("1")) * Decimal("100")
        excess_cum = asset_cum - bench_cum

    asset_period_rets, _ = _compute_period_returns(aligned_asset, decimal_places)
    bench_period_rets, _ = _compute_period_returns(aligned_bench, decimal_places)
    excess_by_period: dict[str, str | None] = {}
    with localcontext() as ctx:
        ctx.prec = 100
        ctx.rounding = ROUND_HALF_UP
        for key in PERIOD_KEYS:
            a_item = asset_period_rets.get(key)
            b_item = bench_period_rets.get(key)
            if a_item is not None and b_item is not None:
                diff = Decimal(a_item["return_percent"]) - Decimal(b_item["return_percent"])
                excess_by_period[key] = _quantize_str(diff, decimal_places)
            else:
                excess_by_period[key] = None

    ra = _daily_simple_returns(aligned_asset)
    rb = _daily_simple_returns(aligned_bench)
    m = len(ra)
    beta_str: str | None = None
    corr_str: str | None = None
    if m >= 2:
        with localcontext() as ctx:
            ctx.prec = 100
            ctx.rounding = ROUND_HALF_UP
            mean_a = sum(ra, Decimal("0")) / Decimal(m)
            mean_b = sum(rb, Decimal("0")) / Decimal(m)
            cov = sum(((ra[i] - mean_a) * (rb[i] - mean_b) for i in range(m)), Decimal("0")) / Decimal(m - 1)
            var_a = sum(((ra[i] - mean_a) * (ra[i] - mean_a) for i in range(m)), Decimal("0")) / Decimal(m - 1)
            var_b = sum(((rb[i] - mean_b) * (rb[i] - mean_b) for i in range(m)), Decimal("0")) / Decimal(m - 1)
            if var_b > 0:
                beta_str = _quantize_str(cov / var_b, max(decimal_places, 4))
                if var_a > 0:
                    corr = cov / (var_a.sqrt(ctx) * var_b.sqrt(ctx))
                    corr_str = _quantize_str(corr, max(decimal_places, 4))

    return {
        "benchmark_source_id": bench_meta["source_id"],
        "benchmark_instrument": bench_meta["instrument"],
        "benchmark_currency": bench_meta["currency"],
        "benchmark_adjustment_method": bench_meta["adjustment_method"],
        "aligned_bar_count": len(common_dates),
        "aligned_observations": m,
        "aligned_start_date": common_dates[0].isoformat(),
        "aligned_end_date": common_dates[-1].isoformat(),
        "asset_aligned_return_percent": _quantize_str(asset_cum, decimal_places),
        "benchmark_aligned_return_percent": _quantize_str(bench_cum, decimal_places),
        "excess_return_percent": _quantize_str(excess_cum, decimal_places),
        "beta": beta_str,
        "correlation": corr_str,
        "excess_returns_by_period": excess_by_period,
    }


def analyze_market_series(
    request: AnalyzeMarketSeriesRequest,
    *,
    sources: Mapping[str, ResearchSource],
    as_of: datetime | None = None,
    allow_simulated: bool = False,
) -> dict[str, Any]:
    """Deterministically analyze persisted daily market_data / historical_prices bars."""
    source, bars, meta = _validate_and_load_series(
        request.source_id,
        sources,
        as_of=as_of,
        allow_simulated=allow_simulated,
        start_date=request.start_date,
        end_date=request.end_date,
        price_basis=request.price_basis,
    )

    returns, unavailable_periods = _compute_period_returns(bars, request.decimal_places)
    cagr_pct, cagr_years, cagr_unavailable = _compute_cagr(bars, request.decimal_places)
    ann_vol_pct, vol_obs, vol_unavailable = _compute_volatility(bars, request.decimal_places)
    max_drawdown = _compute_max_drawdown(bars, request.decimal_places)
    technicals = _compute_technicals(bars, request.decimal_places)

    with localcontext() as ctx:
        ctx.prec = 100
        ctx.rounding = ROUND_HALF_UP
        cumulative_return = ((bars[-1].close / bars[0].close) - Decimal("1")) * Decimal("100")

    benchmark_result: dict[str, Any] | None = None
    bench_meta: dict[str, Any] | None = None
    used_source_ids = [source.source_id]
    is_simulated = source.is_simulated

    if request.benchmark_source_id is not None:
        bench_source, bench_bars, bench_meta = _validate_and_load_series(
            request.benchmark_source_id,
            sources,
            as_of=as_of,
            allow_simulated=allow_simulated,
            start_date=request.start_date,
            end_date=request.end_date,
            price_basis=request.price_basis,
        )
        benchmark_result = _compute_benchmark_comparison(
            bars,
            bench_bars,
            bench_meta,
            request.decimal_places,
        )
        if bench_source.source_id not in used_source_ids:
            used_source_ids.append(bench_source.source_id)
        is_simulated = is_simulated or bench_source.is_simulated

    provenance = {
        **meta,
        "benchmark_provenance": bench_meta,
        "unavailable_reasons": {
            **unavailable_periods,
            **({"cagr": cagr_unavailable} if cagr_unavailable else {}),
            **({"annualized_volatility": vol_unavailable} if vol_unavailable else {}),
        },
    }

    return {
        "source_id": source.source_id,
        "source_ids": used_source_ids,
        "instrument": meta["instrument"],
        "symbol": meta["symbol"],
        "exchange": meta["exchange"],
        "interval": "1d",
        "currency": meta["currency"],
        "is_simulated": is_simulated,
        "request_window": {
            "start_date": request.start_date,
            "end_date": request.end_date,
        },
        "start_date": bars[0].bar_date.isoformat(),
        "end_date": bars[-1].bar_date.isoformat(),
        "bar_count": len(bars),
        "first_close": _quantize_str(bars[0].close, request.decimal_places),
        "latest_close": _quantize_str(bars[-1].close, request.decimal_places),
        "latest_volume": _plain_decimal_str(bars[-1].volume) if bars[-1].volume is not None else None,
        "cumulative_return_percent": _quantize_str(cumulative_return, request.decimal_places),
        "returns": returns,
        "unavailable_periods": unavailable_periods,
        "cagr_percent": cagr_pct,
        "cagr_years": cagr_years,
        "annualized_volatility_percent": ann_vol_pct,
        "volatility_observations": vol_obs,
        "annualization_periods": 252,
        "max_drawdown": max_drawdown,
        "technicals": technicals,
        "benchmark": benchmark_result,
        "provenance": provenance,
    }
