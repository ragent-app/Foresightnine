"""Tests for deterministic market-series analytics (research_market_analytics.py)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import math

import pytest

from app.research_market_analytics import (
    AnalyzeMarketSeriesRequest,
    MarketAnalyticsError,
    analyze_market_series,
)
from app.research_sources import ResearchSource, SourcePermissions


def _market_source(
    source_id: str,
    text: str,
    *,
    kind: str = "market_data",
    retrieval_method: str = "market_data",
    published_at: datetime | None = None,
    is_simulated: bool = False,
    model_input: bool = True,
    display: bool = True,
    url: str | None = None,
    provenance: dict | None = None,
) -> ResearchSource:
    return ResearchSource(
        source_id=source_id,
        owner_id="user-1",
        title=f"Series {source_id}",
        text=text,
        url=url,
        kind=kind,  # type: ignore[arg-type]
        is_simulated=is_simulated,
        published_at=published_at,
        obtained_at=datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc),
        content_sha256="sha-" + source_id,
        retrieval_method=retrieval_method,  # type: ignore[arg-type]
        permissions=SourcePermissions(store=True, model_input=model_input, display=display, export=True),
        provenance=provenance or {},
    )


def _generate_trading_dates(start: date, count: int) -> list[date]:
    days: list[date] = []
    cur = start
    while len(days) < count:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def test_full_analytics_returns_cagr_volatility_drawdown_and_technicals():
    # Build > 3.1 years of weekday dates so 5d, 1m, 3m, 6m, ytd, 1y, 3y, sma20, sma60, rsi14 are all available.
    dates = _generate_trading_dates(date(2023, 1, 2), 950)
    # Piecewise deterministic path with a sharp drawdown and full recovery.
    lines = ["# Daily bars for TEST@NASDAQ"]
    closes: list[Decimal] = []
    for idx, d in enumerate(dates):
        if idx < 400:
            price = Decimal("100") + Decimal(idx) * Decimal("0.25")  # ramps 100 -> 199.75
        elif idx < 500:
            # Drops ~40% peak to trough (200 -> 120)
            price = Decimal("200") - Decimal(idx - 400) * Decimal("0.8")
        elif idx < 650:
            # Recovers above 200 (120.8 -> 240.0)
            price = Decimal("120.8") + Decimal(idx - 500) * Decimal("0.8")
        else:
            # Steady upward slope at end (guarantees SMA20 > SMA60 and RSI > 50)
            price = Decimal("240.0") + Decimal(idx - 650) * Decimal("0.5")
        closes.append(price)
        lines.append(
            f"TEST@NASDAQ | {d.isoformat()} | 1d | O {price} H {price + 1} L {price - 1} C {price} V 1000000 USD"
        )

    src = _market_source("src-mkt", "\n".join(lines), provenance={"provider": "yahoo", "exchange": "NASDAQ"})
    req = AnalyzeMarketSeriesRequest.model_validate({"source_id": "src-mkt"})
    out = analyze_market_series(req, sources={"src-mkt": src})

    assert out["symbol"] == "TEST"
    assert out["exchange"] == "NASDAQ"
    assert out["currency"] == "USD"
    assert out["bar_count"] == 950
    assert out["start_date"] == dates[0].isoformat()
    assert out["end_date"] == dates[-1].isoformat()

    # All 7 standard period returns populated cleanly.
    for period in ("5d", "1m", "3m", "6m", "ytd", "1y", "3y"):
        assert out["returns"][period] is not None, f"Missing period {period}: {out['unavailable_periods']}"
        assert Decimal(out["returns"][period]["return_percent"]) > 0

    # Max drawdown accurately identifies peak at idx 400 (200.00) -> trough idx 499 (120.8 -> ~-39.60%) and recovery.
    dd = out["max_drawdown"]
    assert Decimal(dd["drawdown_percent"]) < Decimal("-35")
    assert dd["peak_date"] == dates[400].isoformat()
    assert dd["trough_date"] == dates[499].isoformat()
    assert dd["recovered"] is True
    assert dd["recovery_date"] is not None and dd["recovery_date"] > dd["trough_date"]

    # Technical indicators populated & consistent with monotonically rising final 300 bars.
    tech = out["technicals"]
    assert tech["sma_20"] is not None
    assert tech["sma_60"] is not None
    assert Decimal(tech["sma_20"]) > Decimal(tech["sma_60"])
    assert tech["rsi_14"] == "100.00"

    assert Decimal(out["cagr_percent"]) > 0
    assert Decimal(out["annualized_volatility_percent"]) > 0


def test_rsi14_and_volatility_exact_properties():
    # Monotonic falling 20 days -> RSI14 == 0.00; geometric doubling -> volatility == 0.00.
    dates = _generate_trading_dates(date(2026, 1, 5), 25)
    falling = [
        f"AAA@NYSE | {d.isoformat()} | 1d | O 100 H 100 L 90 C {Decimal('100') - Decimal(i)} V 500 USD"
        for i, d in enumerate(dates)
    ]
    src_down = _market_source("src-down", "\n".join(falling))
    out_down = analyze_market_series(
        AnalyzeMarketSeriesRequest(source_id="src-down"),
        sources={"src-down": src_down},
    )
    assert out_down["technicals"]["rsi_14"] == "0.00"
    assert out_down["technicals"]["sma_60"] is None
    assert out_down["technicals"]["sma_20"] is not None

    # Constant +1% daily return -> zero sample variance -> annualized_volatility == 0.00, max_drawdown == 0.00.
    p = Decimal("100")
    const_lines = []
    for d in dates:
        const_lines.append(f"BBB@NYSE | {d.isoformat()} | daily close | {p} USD")
        p = p * Decimal("1.01")
    src_const = _market_source(
        "src-const",
        "\n".join(const_lines),
        kind="user_supplied",
        retrieval_method="historical_prices",
        published_at=datetime(2026, 2, 15, tzinfo=timezone.utc),
    )
    out_const = analyze_market_series(
        AnalyzeMarketSeriesRequest(source_id="src-const"),
        sources={"src-const": src_const},
    )
    assert out_const["annualized_volatility_percent"] == "0.00"
    assert out_const["max_drawdown"]["drawdown_percent"] == "0.00"
    assert out_const["technicals"]["rsi_14"] == "100.00"


def test_adj_factor_forward_adjustment_vs_raw_closes():
    # Simulate 2-for-1 stock split on day 20 using cumulative adj_factor (1.0 -> 2.0) with unadjusted closes halving 100 -> 50.
    dates = _generate_trading_dates(date(2026, 1, 5), 30)
    lines = []
    for idx, d in enumerate(dates):
        raw_close = Decimal("100") if idx < 15 else Decimal("50")
        adjf = Decimal("1.0") if idx < 15 else Decimal("2.0")
        lines.append(
            f"600519.SH@SSE | {d.isoformat()} | 1d | O {raw_close} H {raw_close} L {raw_close} C {raw_close} V 1000 CNY ADJF {adjf}"
        )
    src = _market_source(
        "src-cn-split",
        "\n".join(lines),
        provenance={"provider": "tushare", "adjustment": "none", "exchange": "SSE"},
    )

    adj_out = analyze_market_series(
        AnalyzeMarketSeriesRequest(source_id="src-cn-split", price_basis="adjusted"),
        sources={"src-cn-split": src},
    )
    assert adj_out["cumulative_return_percent"] == "0.00"
    assert adj_out["max_drawdown"]["drawdown_percent"] == "0.00"
    assert adj_out["provenance"]["adjustment_method"] == "adj_factor_forward_normalized_to_end"
    assert adj_out["provenance"]["adj_factor_present"] is True

    raw_out = analyze_market_series(
        AnalyzeMarketSeriesRequest(source_id="src-cn-split", price_basis="raw"),
        sources={"src-cn-split": src},
    )
    assert raw_out["cumulative_return_percent"] == "-50.00"
    assert raw_out["max_drawdown"]["drawdown_percent"] == "-50.00"
    assert raw_out["provenance"]["adjustment_method"] == "none_raw_close"


def test_benchmark_excess_return_beta_and_cross_calendar_alignment():
    # Asset follows exactly 2x daily returns of benchmark on overlapping dates; benchmark has an extra holiday bar omitted cleanly.
    dates = _generate_trading_dates(date(2026, 1, 5), 25)
    bench_rets = [Decimal("0.01") if i % 2 == 0 else Decimal("-0.005") for i in range(len(dates) - 1)]
    bench_prices = [Decimal("100")]
    asset_prices = [Decimal("100")]
    for r in bench_rets:
        bench_prices.append(bench_prices[-1] * (Decimal("1") + r))
        asset_prices.append(asset_prices[-1] * (Decimal("1") + Decimal("2") * r))

    asset_lines = [
        f"BABA@NYSE | {d.isoformat()} | 1d | O {c} H {c} L {c} C {c} V 1000 USD"
        for i, (d, c) in enumerate(zip(dates, asset_prices))
        if i != 10  # simulate missing/holiday bar in asset on dates[10]
    ]
    bench_lines = [
        f"SPY@NYSE | {d.isoformat()} | 1d | O {c} H {c} L {c} C {c} V 5000 USD"
        for d, c in zip(dates, bench_prices)
    ]
    src_a = _market_source("src-a", "\n".join(asset_lines))
    src_b = _market_source("src-b", "\n".join(bench_lines))

    out = analyze_market_series(
        AnalyzeMarketSeriesRequest(source_id="src-a", benchmark_source_id="src-b"),
        sources={"src-a": src_a, "src-b": src_b},
    )
    bm = out["benchmark"]
    assert bm is not None
    assert bm["benchmark_source_id"] == "src-b"
    assert bm["aligned_bar_count"] == len(dates) - 1
    diff_rounded = Decimal(bm["asset_aligned_return_percent"]) - Decimal(bm["benchmark_aligned_return_percent"])
    assert abs(diff_rounded - Decimal(bm["excess_return_percent"])) <= Decimal("0.01")
    assert Decimal(bm["beta"]) > Decimal("1.5")
    assert Decimal(bm["correlation"]) > Decimal("0.9")


def test_as_of_clamping_and_fail_closed_validation():
    dates = _generate_trading_dates(date(2026, 1, 5), 10)
    bars = "\n".join(
        f"NVDA@NASDAQ | {d.isoformat()} | 1d | O 100 H 105 L 95 C {100 + i} V 1000 USD"
        for i, d in enumerate(dates)
    )
    src = _market_source("src-nvda", bars, published_at=datetime(2025, 1, 1, tzinfo=timezone.utc), provenance={"exchange": "NASDAQ"})

    # as_of midpoint clamps later bars out without lookahead.
    cutoff = datetime(dates[4].year, dates[4].month, dates[4].day, 21, 0, tzinfo=timezone.utc)
    clamped = analyze_market_series(
        AnalyzeMarketSeriesRequest(source_id="src-nvda"),
        sources={"src-nvda": src},
        as_of=cutoff,
    )
    assert clamped["bar_count"] == 5
    assert clamped["end_date"] == dates[4].isoformat()
    assert clamped["latest_close"] == "104.00"
    assert clamped["provenance"]["clamped_to_as_of"] is True
    assert clamped["provenance"]["excluded_post_as_of_rows"] == 5

    # as_of earlier than every bar -> fail closed.
    too_early = datetime(2025, 12, 31, 21, 0, tzinfo=timezone.utc)
    with pytest.raises(MarketAnalyticsError, match="no market bars on or before as_of"):
        analyze_market_series(
            AnalyzeMarketSeriesRequest(source_id="src-nvda"),
            sources={"src-nvda": src},
            as_of=too_early,
        )

    # Simulated / unpermitted / FRED / non-market prose sources fail closed.
    sim = _market_source("src-sim", bars, is_simulated=True)
    with pytest.raises(MarketAnalyticsError, match="simulated"):
        analyze_market_series(AnalyzeMarketSeriesRequest(source_id="src-sim"), sources={"src-sim": sim})

    hidden = _market_source("src-hidden", bars, display=False)
    with pytest.raises(MarketAnalyticsError, match="permissions"):
        analyze_market_series(AnalyzeMarketSeriesRequest(source_id="src-hidden"), sources={"src-hidden": hidden})

    fred = _market_source("src-fred", bars, url="https://fred.stlouisfed.org/series/DGS10")
    with pytest.raises(MarketAnalyticsError, match="FRED"):
        analyze_market_series(AnalyzeMarketSeriesRequest(source_id="src-fred"), sources={"src-fred": fred})

    prose = _market_source("src-prose", "Revenue rose 10% YoY.", kind="official", retrieval_method="sec_filing_html")
    with pytest.raises(MarketAnalyticsError, match="only accepts persisted market_data"):
        analyze_market_series(AnalyzeMarketSeriesRequest(source_id="src-prose"), sources={"src-prose": prose})

    intraday = _market_source(
        "src-intra",
        "NVDA@NASDAQ | 2026-01-05T10:00:00Z | 5m | O 100 H 101 L 99 C 100 V 100 USD",
    )
    with pytest.raises(MarketAnalyticsError, match="unsupported interval"):
        analyze_market_series(AnalyzeMarketSeriesRequest(source_id="src-intra"), sources={"src-intra": intraday})


def test_currency_and_cutoff_boundaries_for_market_comparisons():
    def bars(symbol, currency1, currency2):
        return (f'{symbol}@X | 2026-02-02 | 1d | O 10 H 10 L 10 C 10 V 100 {currency1} ADJF 1\n'
                f'{symbol}@X | 2026-02-04 | 1d | O 12 H 12 L 12 C 12 V 100 {currency2} ADJF 1')
    known_date = datetime(2026, 2, 5, tzinfo=timezone.utc)
    cutoff = datetime(2026, 3, 1, tzinfo=timezone.utc)
    mixed = _market_source('mixed', bars('AAA', 'USD', 'EUR'), published_at=known_date)
    with pytest.raises(MarketAnalyticsError, match='currency'):
        analyze_market_series(AnalyzeMarketSeriesRequest(source_id='mixed'), sources={'mixed':mixed}, as_of=cutoff)

    usd = _market_source('usd', bars('AAA', 'USD', 'USD'), published_at=known_date)
    eur = _market_source('eur', bars('BBB', 'EUR', 'EUR'), published_at=known_date)
    sources = {'usd':usd, 'eur':eur}
    # Each return is valid in its own stated currency; no implicit FX conversion.
    assert analyze_market_series(AnalyzeMarketSeriesRequest(source_id='usd'), sources=sources, as_of=cutoff)['cumulative_return_percent'] == '20.00'
    assert analyze_market_series(AnalyzeMarketSeriesRequest(source_id='eur'), sources=sources, as_of=cutoff)['cumulative_return_percent'] == '20.00'
    with pytest.raises(MarketAnalyticsError, match='same known currency'):
        analyze_market_series(AnalyzeMarketSeriesRequest(source_id='usd', benchmark_source_id='eur'), sources=sources, as_of=cutoff)
    unknown = _market_source('unknown', bars('CCC', '', ''), published_at=known_date)
    with pytest.raises(MarketAnalyticsError, match='same known currency'):
        analyze_market_series(AnalyzeMarketSeriesRequest(source_id='usd', benchmark_source_id='unknown'), sources={**sources, 'unknown':unknown}, as_of=cutoff)
    unpublished = _market_source('undated', bars('AAA', 'USD', 'USD'))
    with pytest.raises(MarketAnalyticsError, match='availability_unestablished'):
        analyze_market_series(AnalyzeMarketSeriesRequest(source_id='undated'), sources={'undated':unpublished}, as_of=cutoff)
    later = _market_source('later', bars('AAA', 'USD', 'USD'), published_at=datetime(2026, 4, 1, tzinfo=timezone.utc))
    with pytest.raises(MarketAnalyticsError, match='availability_unestablished'):
        analyze_market_series(AnalyzeMarketSeriesRequest(source_id='later'), sources={'later':later}, as_of=cutoff)
