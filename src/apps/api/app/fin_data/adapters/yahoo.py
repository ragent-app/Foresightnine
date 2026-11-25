"""Yahoo Finance chart adapter: daily bars for A-shares and Hong Kong stocks.

GET https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1&period2&interval=1d&events=div,splits

LICENCE: Yahoo's terms do not allow commercial or automated use of this data. The default fin_data policy therefore
denies provider ``yahoo`` (license_terms_restrict_commercial_use); an operator may allow it for personal development
use only (see policy.py), and it must be removed before any public release.

The endpoint is unofficial and keyless. When Yahoo answers 429 or demands a cookie/crumb (401), the adapter reports
``yahoo_rate_limited`` / ``yahoo_unavailable`` and stops; it deliberately does not obtain or replay cookies or crumbs.
Calls are paced (FORESIGHTNINE_YAHOO_MIN_INTERVAL_SECONDS, default 2) and carry a generic descriptive User-Agent.

Prices: ``close`` is the quote close as Yahoo reports it (split-adjusted, not dividend-adjusted). ``adjclose`` (split
and dividend adjusted) is kept as ``adj_factor = adjclose / close`` on each bar, so a dividend-adjusted price is
price * adj_factor; dividend and split events inside the range are listed in the coverage notes.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha256
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...openai_compatible import _TransportFailure, _TransportTimeout
from ..models import Bar, Coverage, Cost, Provenance, SuggestedAction, ToolResult
from ..policy import Denied, FinDataPolicy, load_policy
from ..ratelimit import default_limiter
from .base import StopWith, Transport, default_transport, header, rate_limited_result, retry_after, utcnow
from .cn_base import bar_window
from .tushare import cn_aggregate

PROVIDER = "yahoo"
BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"
MIN_INTERVAL_ENV = "FORESIGHTNINE_YAHOO_MIN_INTERVAL_SECONDS"
DEFAULT_MIN_INTERVAL = 2.0
USER_AGENT = "ForesightNine-research/1.0 (personal development use; daily bars)"
PRICE_QUANT = Decimal("0.0001")
ADJ_QUANT = Decimal("0.000001")
MAX_EVENT_NOTES = 8
EXCHANGE_CURRENCY = {"SSE": "CNY", "SZSE": "CNY", "BJSE": "CNY", "HKEX": "HKD", "NASDAQ": "USD", "NYSE": "USD"}


class _UsCode:
    """Lightweight US equity ticker adapter for ``YahooAdapter.bars()``."""

    __slots__ = ("symbol", "exchange", "entity_id", "yahoo_symbol")

    def __init__(self, symbol: str, exchange: str = "NASDAQ") -> None:
        sym = symbol.strip().upper()
        self.symbol = sym
        self.exchange = exchange
        self.entity_id = f"{exchange}:{sym}"
        self.yahoo_symbol = sym.replace(".", "-")
YAHOO_NOTE = ("Yahoo Finance daily bars (unofficial keyless endpoint, personal development use only; not exportable). "
              "Intraday bars are not provided. Trading holidays are not modelled, so a holiday simply has no bar.")


def min_interval() -> float:
    try:
        value = float(os.environ.get(MIN_INTERVAL_ENV, ""))
    except ValueError:
        return DEFAULT_MIN_INTERVAL
    return value if value >= 0 else DEFAULT_MIN_INTERVAL


class Pacer:
    """Spaces calls at least ``min_interval()`` apart across threads (reservation under a lock, sleep outside it)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> float:
        with self._lock:
            now = self._clock()
            slot = max(now, self._next)
            self._next = slot + min_interval()
        delay = slot - now
        if delay > 0:
            self._sleep(delay)
        return max(delay, 0.0)


_PACER = Pacer()


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = value if isinstance(value, Decimal) else Decimal(str(value))
    except ArithmeticError:
        return None
    return out if out.is_finite() else None


def _at(values: Any, index: int) -> Any:
    return values[index] if isinstance(values, list) and index < len(values) else None


def _tz(name: Any, fallback: ZoneInfo) -> ZoneInfo:
    if isinstance(name, str) and name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return fallback


def _event_notes(events: Any, tz: ZoneInfo, first: date, last: date, currency: str | None) -> list[str]:
    notes: list[str] = []
    if not isinstance(events, dict):
        return notes
    for kind, label in (("dividends", "Dividend"), ("splits", "Split")):
        block = events.get(kind)
        found: list[tuple[date, str]] = []
        for key, item in (block.items() if isinstance(block, dict) else []):
            if not isinstance(item, dict):
                continue
            stamp = item.get("date") if isinstance(item.get("date"), int) else (int(key) if str(key).isdigit() else None)
            if stamp is None:
                continue
            try:
                day = datetime.fromtimestamp(stamp, tz).date()
            except (OverflowError, OSError, ValueError):
                continue
            if not first <= day <= last:
                continue
            if kind == "dividends":
                amount = _dec(item.get("amount"))
                found.append((day, f"{amount if amount is not None else '?'} {currency or ''}".strip()))
            else:
                text = item.get("splitRatio") or (f"{item.get('numerator')}:{item.get('denominator')}"
                                                   if item.get("numerator") is not None else "?")
                found.append((day, str(text)))
        found.sort()
        if found:
            shown = "; ".join(f"{d.isoformat()} {v}" for d, v in found[:MAX_EVENT_NOTES])
            more = f" (+{len(found) - MAX_EVENT_NOTES} more)" if len(found) > MAX_EVENT_NOTES else ""
            notes.append(f"{label} events in range ({len(found)}): {shown}{more}.")
    return notes


def parse_chart(doc: Any) -> tuple[dict[str, Any], list[dict[str, Any]], Any]:
    """(meta, raw rows [{ts, o, h, l, c, v, adj}], events) from a chart document; ValueError on a wrong shape."""
    chart = doc.get("chart") if isinstance(doc, dict) else None
    results = chart.get("result") if isinstance(chart, dict) else None
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise ValueError("unexpected chart shape")
    result = results[0]
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    stamps = result.get("timestamp")
    stamps = stamps if isinstance(stamps, list) else []
    indicators = result.get("indicators") if isinstance(result.get("indicators"), dict) else {}
    quote = (indicators.get("quote") or [{}])[0] if isinstance(indicators.get("quote"), list) else {}
    quote = quote if isinstance(quote, dict) else {}
    adj_block = indicators.get("adjclose")
    adj = adj_block[0].get("adjclose") if isinstance(adj_block, list) and adj_block and isinstance(adj_block[0], dict) else None
    rows = []
    for i, stamp in enumerate(stamps):
        if not isinstance(stamp, int) or isinstance(stamp, bool):
            continue
        rows.append({"ts": stamp, "o": _at(quote.get("open"), i), "h": _at(quote.get("high"), i),
                     "l": _at(quote.get("low"), i), "c": _at(quote.get("close"), i), "v": _at(quote.get("volume"), i),
                     "adj": _at(adj, i)})
    return meta, rows, result.get("events")


def _map_http(status: int, body: bytes, headers, prov, cost) -> ToolResult | None:
    """Failure envelope for a non-usable HTTP reply, or None when the body should be parsed."""
    text = body[:400].decode("utf-8", "replace")
    lowered = text.lower()
    if status == 429 or lowered.startswith("too many requests"):
        wait = retry_after(headers) or 60.0
        return ToolResult.failure("rate_limited", "yahoo_rate_limited",
                                  "Yahoo Finance rate-limited this server (HTTP 429); retry later or use another source.",
                                  retryable=True, provenance=prov, cost=cost,
                                  suggested_actions=[SuggestedAction(action="wait", retry_after_seconds=wait)])
    if status in (401, 403) or "unauthorized" in lowered or "crumb" in lowered:
        return ToolResult.failure("failed", "yahoo_unavailable",
                                  "Yahoo Finance refused the keyless request (it requires cookie/crumb authentication, "
                                  "which this tool does not circumvent). Use another source.", provenance=prov, cost=cost)
    if status == 404:
        return ToolResult.failure("empty", "no_data", "Yahoo Finance has no data for this symbol.", provenance=prov, cost=cost)
    if not 200 <= status < 300:
        return ToolResult.failure("failed", "yahoo_unavailable", f"Yahoo Finance returned HTTP {status}.",
                                  retryable=status >= 500, provenance=prov, cost=cost)
    return None


class YahooAdapter:
    def __init__(self, *, transport: Transport | None = None, limiter=None, policy: FinDataPolicy | None = None,
                 now_fn: Callable[[], datetime] = utcnow, timeout: float = 10.0, pacer: Pacer | None = None) -> None:
        self._transport = transport or default_transport(5_000_000)
        self._limiter = limiter or default_limiter()
        self._policy = policy or load_policy()
        self._now, self._timeout = now_fn, timeout
        self._pacer = pacer or _PACER

    def _gate(self, exchange: str | None):
        decision = self._policy.decide(PROVIDER, "bars_daily", exchange, None, None, credential_owner="platform")
        if isinstance(decision, Denied):
            return ToolResult.failure("denied", decision.reason, decision.message)
        return decision

    def available(self, category: str = "bars_daily", exchange: str | None = None) -> bool:
        return category == "bars_daily" and not isinstance(self._gate(exchange), ToolResult)

    def _fetch(self, symbol: str, period1: int, period2: int) -> tuple[Any, Provenance, Cost]:
        decision = self._limiter.acquire(PROVIDER, 1)
        if not decision.allowed:
            raise StopWith(rate_limited_result(decision, "yahoo"))
        self._pacer.wait()
        cost = Cost(provider_calls=1)
        url = f"{BASE}{symbol}?period1={period1}&period2={period2}&interval=1d&events=div%2Csplits"
        try:
            reply = self._transport("GET", url, {"Accept": "application/json", "User-Agent": USER_AGENT}, b"", self._timeout)
        except _TransportTimeout:
            raise StopWith(ToolResult.failure("failed", "source_timeout", "Yahoo Finance request timed out.",
                                              retryable=True, cost=cost)) from None
        except (_TransportFailure, OSError) as error:
            reason = "source_too_large" if str(error) == "response too large" else "yahoo_unavailable"
            raise StopWith(ToolResult.failure("failed", reason, "Yahoo Finance request failed.", retryable=reason != "source_too_large",
                                              cost=cost)) from None
        prov = Provenance(provider=PROVIDER, endpoint=f"{BASE}{symbol}", retrieved_at=self._now(),
                          response_sha256=sha256(reply.body).hexdigest())
        failure = _map_http(reply.status_code, reply.body, reply.headers, prov, cost)
        if failure is not None:
            raise StopWith(failure)
        try:
            doc = json.loads(reply.body, parse_float=Decimal)
        except (ValueError, UnicodeDecodeError):
            raise StopWith(ToolResult.failure("failed", "invalid_response", "Yahoo Finance returned invalid JSON.",
                                              provenance=prov, cost=cost)) from None
        error = doc.get("chart", {}).get("error") if isinstance(doc, dict) and isinstance(doc.get("chart"), dict) else None
        if isinstance(error, dict):
            code, text = str(error.get("code") or ""), str(error.get("description") or "")
            if code.lower() == "not found" or "no data found" in text.lower():
                raise StopWith(ToolResult.failure("empty", "no_data", "Yahoo Finance has no data for this symbol.",
                                                  provenance=prov, cost=cost))
            mapped = _map_http(401 if ("unauthorized" in code.lower() or "crumb" in text.lower()) else
                               429 if "too many" in text.lower() else 200, text.encode(), {}, prov, cost)
            raise StopWith(mapped or ToolResult.failure("failed", "yahoo_unavailable",
                                                        f"Yahoo Finance error: {code} {text}"[:300], provenance=prov, cost=cost))
        return doc, prov, cost

    def bars(self, code, *, interval: str = "1day", start: date | None = None, end: date | None = None,
             entity_id: str | None = None, include_partial: bool = False) -> ToolResult:
        """``code``: a ``cn.CnCode``, ``cn.HkCode``, or US ticker string (auto-wrapped as ``_UsCode``)."""
        if isinstance(code, str):
            code = _UsCode(code)
        if interval not in ("1day", "1week", "1month"):
            return ToolResult.failure("failed", "intraday_not_supported",
                                      "Yahoo daily source provides daily, weekly and monthly bars only.")
        if start and end and start > end:
            return ToolResult.failure("failed", "invalid_request", "start must not be after end.")
        gate = self._gate(code.exchange)
        if isinstance(gate, ToolResult):
            return gate
        win = bar_window(self._now(), code.exchange, start, end, include_partial)
        notes = [YAHOO_NOTE, *win.notes[1:]]
        eid = entity_id or code.entity_id
        if start and start > win.effective_end:
            return ToolResult(status="empty", reason="range_after_latest_session", permissions=gate,
                              message=f"Latest allowed end date is {win.latest.isoformat()}.",
                              suggested_actions=[SuggestedAction(action="market_data", hint=f"end_date<={win.latest.isoformat()}")],
                              coverage=Coverage(entity_id=eid, session_status=win.status, notes=notes))
        first_day = start or date(1990, 1, 1)
        # period1 inclusive / period2 exclusive epoch seconds at local midnight; rows are filtered again by local date.
        period1 = int(datetime.combine(first_day, dtime.min, tzinfo=win.tz).timestamp())
        period2 = int(datetime.combine(win.effective_end + timedelta(days=1), dtime.min, tzinfo=win.tz).timestamp())
        try:
            doc, prov, cost = self._fetch(code.yahoo_symbol, period1, period2)
        except StopWith as stop:
            return stop.result
        try:
            meta, raw_rows, events = parse_chart(doc)
        except ValueError:
            return ToolResult.failure("failed", "invalid_response", "Unexpected Yahoo Finance chart shape.",
                                      provenance=prov, cost=cost)
        tz = _tz(meta.get("exchangeTimezoneName"), win.tz)
        currency = str(meta["currency"]).upper() if isinstance(meta.get("currency"), str) and meta.get("currency") else \
            EXCHANGE_CURRENCY.get(code.exchange)
        dropped = {"rows with null or non-positive prices": 0, "rows outside the requested range": 0,
                   "placeholder rows with zero volume": 0, "duplicate dates": 0}
        by_day: dict[date, Bar] = {}
        for row in raw_rows:
            try:
                day = datetime.fromtimestamp(row["ts"], tz).date()
            except (OverflowError, OSError, ValueError):
                continue
            px = [_dec(row[k]) for k in ("o", "h", "l", "c")]
            if any(p is None or p <= 0 for p in px) or px[1] < px[2]:
                dropped["rows with null or non-positive prices"] += 1
                continue
            if (start and day < start) or day > win.effective_end:
                dropped["rows outside the requested range"] += 1
                continue
            vol = _dec(row["v"])
            if vol is not None and vol == 0:
                dropped["placeholder rows with zero volume"] += 1
                continue
            if day in by_day:
                dropped["duplicate dates"] += 1
                continue
            o, h, l, c = (p.quantize(PRICE_QUANT, rounding=ROUND_HALF_UP) for p in px)
            adj = _dec(row["adj"])
            factor = (adj / px[3]).quantize(ADJ_QUANT) if adj is not None and adj > 0 else None
            by_day[day] = Bar(entity_id=eid, interval="1day", ts_start=datetime.combine(day, win.open_time, tzinfo=win.tz),
                              open=o, high=h, low=l, close=c, volume=vol.to_integral_value() if vol is not None else None,
                              adj_factor=factor, adjustment="splits", session="regular", is_final=day <= win.latest)
        for label, count in dropped.items():
            if count:
                notes.append(f"Dropped {count} {label}.")
        daily = [by_day[d] for d in sorted(by_day)]
        bars = cn_aggregate(daily, interval, win.latest)
        if daily:
            notes.append("close is Yahoo's quote close (split-adjusted, not dividend-adjusted); adj_factor = adjclose / close "
                         "(dividend- and split-adjusted price = price * adj_factor).")
            notes.extend(_event_notes(events, tz, min(by_day), max(by_day), currency))
        if interval != "1day" and bars:
            notes.append(f"{interval} bars are aggregated locally from daily bars; ts_start is the first trading day.")
        if not bars:
            return ToolResult(status="empty", reason="no_bars", permissions=gate, provenance=prov, cost=cost,
                              coverage=Coverage(entity_id=eid, session_status=win.status, notes=notes))
        cov = Coverage(entity_id=eid, requested={"start": start.isoformat() if start else None, "end": end.isoformat() if end else None},
                       actual={"first": bars[0].ts_start.isoformat(), "last": bars[-1].ts_start.isoformat()}, rows=len(bars),
                       data_as_of=bars[-1].ts_start, currency=currency, session_status=win.status,
                       is_final=all(b.is_final for b in bars), notes=notes)
        reason = "range_clamped" if win.clamped else "ok"
        return ToolResult(status="ok" if reason == "ok" else "partial", reason=reason, coverage=cov, permissions=gate,
                          provenance=prov, cost=cost, data=bars,
                          message=f"Requested end was after the latest completed session ({win.latest.isoformat()})." if win.clamped else "",
                          suggested_actions=[SuggestedAction(action="market_data", hint=f"end_date<={win.latest.isoformat()}")] if win.clamped else [])
