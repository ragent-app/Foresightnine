"""A-share helpers shared by the Tushare and BaoStock adapters.

Pure functions only: code normalisation, China's cumulative (year-to-date) reporting periods, single-quarter
derivation (explicit request only, always marked derived), revision selection, weekly/monthly aggregation of
daily bars and the in-process list cache. No network, no provider shapes.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Callable, Iterable, Sequence

from .models import Bar

CN_EXCHANGES = ("SSE", "SZSE", "BJSE")
CN_CURRENCY = "CNY"
ACCOUNTING_STANDARD = "中国企业会计准则 (PRC CAS)"
_SUFFIX = {"SH": "SSE", "SS": "SSE", "SSE": "SSE", "SZ": "SZSE", "SZSE": "SZSE", "BJ": "BJSE", "BJSE": "BJSE"}
_TS_SUFFIX = {"SSE": "SH", "SZSE": "SZ", "BJSE": "BJ"}
_BS_PREFIX = {"SSE": "sh", "SZSE": "sz", "BJSE": "bj"}
_YAHOO_SUFFIX = {"SSE": "SS", "SZSE": "SZ", "BJSE": "BJ"}
_FORMS = (re.compile(r"^(?P<c>\d{6})\s*[.:]\s*(?P<x>[A-Za-z]{2,4})$"),
          re.compile(r"^(?P<x>[A-Za-z]{2,4})\s*[.:]?\s*(?P<c>\d{6})$"),
          re.compile(r"^(?P<c>\d{6})$"))


@dataclass(frozen=True)
class CnCode:
    code: str
    exchange: str
    inferred: bool  # exchange derived from the code prefix, not stated by the caller

    @property
    def entity_id(self) -> str:
        return f"{self.exchange}:{self.code}"

    @property
    def ts_code(self) -> str:
        return f"{self.code}.{_TS_SUFFIX[self.exchange]}"

    @property
    def baostock_code(self) -> str:
        return f"{_BS_PREFIX[self.exchange]}.{self.code}"

    @property
    def yahoo_symbol(self) -> str:
        return f"{self.code}.{_YAHOO_SUFFIX[self.exchange]}"


@dataclass(frozen=True)
class HkCode:
    """Hong Kong stock code; ``code`` is the 4-digit-padded form Yahoo uses (0700, 9988, 02318 -> 2318)."""
    code: str
    exchange: str = "HKEX"
    inferred: bool = False

    @property
    def entity_id(self) -> str:
        return f"HKEX:{self.code}"

    @property
    def yahoo_symbol(self) -> str:
        return f"{self.code}.HK"


def exchange_for_code(code: str) -> str | None:
    """SSE 6xxxxx/9xxxxx (A and B shares, STAR 688), SZSE 0/2/3xxxxx, BJSE 4/8xxxxx and 920xxx."""
    if not re.fullmatch(r"\d{6}", code or ""):
        return None
    if code.startswith("920"):
        return "BJSE"
    return {"6": "SSE", "9": "SSE", "0": "SZSE", "2": "SZSE", "3": "SZSE", "4": "BJSE", "8": "BJSE"}.get(code[0])


def parse_cn_code(text: str | None) -> CnCode | None:
    """``600519.SH``, ``sh.600519``, ``SH600519``, ``SSE:600519`` or bare ``600519`` (exchange inferred)."""
    if not isinstance(text, str):
        return None
    raw = text.strip()
    for pattern in _FORMS:
        match = pattern.match(raw)
        if not match:
            continue
        code, suffix = match.group("c"), (match.groupdict().get("x") or "").upper()
        if suffix:
            exchange = _SUFFIX.get(suffix)
            if exchange is None:
                return None
            return CnCode(code, exchange, False)
        exchange = exchange_for_code(code)
        return CnCode(code, exchange, True) if exchange else None
    return None


_HK_FORMS = (re.compile(r"^(?P<c>\d{1,5})\s*[.:]\s*(?:HK|HKEX|SEHK)$", re.I),
             re.compile(r"^(?:HK|HKEX|SEHK)\s*[.:]?\s*(?P<c>\d{1,5})$", re.I))


def parse_hk_code(text: str | None, market: str | None = None) -> HkCode | None:
    """HK code from ``700.HK``, ``0700.HK``, ``HK:700``, ``HKEX:0700``; a bare 4-5 digit number only when it starts
    with 0 or the caller states market HK (so a year or an ID is not mistaken for a stock). Code 1..99999."""
    if not text:
        return None
    raw = text.strip()
    digits = None
    for pattern in _HK_FORMS:
        match = pattern.match(raw)
        if match:
            digits = match.group("c")
            break
    if digits is None and re.fullmatch(r"\d{1,5}", raw) and (
            (market or "").upper() == "HK" or (len(raw) >= 4 and raw.startswith("0"))):
        digits = raw
    if digits is None or not 1 <= int(digits) <= 99999:
        return None
    return HkCode(str(int(digits)).zfill(4), "HKEX", False)


def is_cn_exchange(exchange: str | None) -> bool:
    return bool(exchange) and exchange.upper() in CN_EXCHANGES


def contains_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


# ------------------------------------------------------------------ report periods (cumulative)
_YTD_LABEL = {3: "Q1 YTD", 6: "H1 YTD", 9: "9M YTD"}


def is_report_end(day: date) -> bool:
    return (day.month, day.day) in ((3, 31), (6, 30), (9, 30), (12, 31))


def ytd_label(end: date) -> str:
    return f"FY{end.year}" if end.month == 12 else f"{end.year}{_YTD_LABEL[end.month]}"


def quarter_label(end: date) -> str:
    return f"{end.year}Q{end.month // 3}"


def year_start(end: date) -> date:
    return date(end.year, 1, 1)


def quarter_start(end: date) -> date:
    return date(end.year, end.month - 2, 1)


def select_report_ends(ends: Iterable[date], period: str, n: int) -> list[date]:
    """Newest first. ``quarter`` = the last n reporting periods (Q1, H1, 9M, FY - each cumulative from 1 Jan);
    ``year`` = the last n fiscal-year ends."""
    valid = sorted({e for e in ends if is_report_end(e) and (period == "quarter" or e.month == 12)}, reverse=True)
    return valid[:n]


def derive_single_quarters(ytd: dict[date, Decimal], wanted_ends: Sequence[date]) -> tuple[list[tuple[date, Decimal, str]], list[str]]:
    """Single-quarter flow values from cumulative ones: Q1=Q1, Q2=H1-Q1, Q3=9M-H1, Q4=FY-9M.

    Returns ([(quarter_end, value, derivation)], skipped-notes). Q1 equals the reported figure and is not repeated."""
    out: list[tuple[date, Decimal, str]] = []
    skipped: list[str] = []
    for end in sorted(wanted_ends, reverse=True):
        if end.month == 3:
            continue
        prev = date(end.year, end.month - 3, 30 if end.month - 3 in (6, 9) else 31)
        if end in ytd and prev in ytd:
            out.append((end, ytd[end] - ytd[prev],
                        f"{ytd_label(end)} minus {ytd_label(prev)} (cumulative figures as reported)"))
        else:
            skipped.append(f"{quarter_label(end)} not derivable: {ytd_label(prev) if end in ytd else ytd_label(end)} missing")
    return out, skipped


# ------------------------------------------------------------------ revisions
def revision_order(row: dict[str, Any]) -> tuple:
    """Oldest to newest: actual announcement date, announcement date, update flag."""
    return (row.get("filed") or date.min, row.get("ann") or date.min, str(row.get("update_flag") or ""))


def pick_latest(rows: list[dict[str, Any]], field: str) -> tuple[dict[str, Any] | None, Decimal | None, list[str]]:
    """Latest revision with a value for ``field``; earlier *different* values are returned as ``filed:value``."""
    ordered = sorted((r for r in rows if r.get(field) is not None), key=revision_order, reverse=True)
    if not ordered:
        return None, None, []
    winner = ordered[0]
    superseded = [f"{(r.get('filed') or r.get('ann') or '?')}:{format(r[field], 'f')}"
                  for r in ordered[1:] if r[field] != winner[field]]
    return winner, winner[field], superseded


# ------------------------------------------------------------------ bars
def aggregate_bars(daily: Sequence[Bar], interval: str, latest_session: date) -> list[Bar]:
    """Weekly (ISO week) or monthly bars from final daily bars; ts_start is the first trading day of the period."""
    if interval == "1day":
        return list(daily)
    groups: dict[tuple, list[Bar]] = {}
    for bar in daily:
        d = bar.ts_start.date()
        key = d.isocalendar()[:2] if interval == "1week" else (d.year, d.month)
        groups.setdefault(key, []).append(bar)
    out = []
    for bars in groups.values():
        bars.sort(key=lambda b: b.ts_start)
        first, last = bars[0], bars[-1]
        d = last.ts_start.date()
        if interval == "1week":
            period_end = d + timedelta(days=4 - d.weekday()) if d.weekday() <= 4 else d
        else:
            period_end = date(d.year + (d.month == 12), d.month % 12 + 1, 1) - timedelta(days=1)
        vols = [b.volume for b in bars if b.volume is not None]
        out.append(Bar(entity_id=first.entity_id, interval=interval, ts_start=first.ts_start, open=first.open,
                       high=max(b.high for b in bars), low=min(b.low for b in bars), close=last.close,
                       volume=sum(vols, Decimal(0)) if vols else None, adj_factor=last.adj_factor,
                       adjustment=first.adjustment, session=first.session,
                       is_final=all(b.is_final for b in bars) and period_end <= latest_session))
    out.sort(key=lambda b: b.ts_start)
    return out


def session_open_ts(day: date, open_time: time, tz) -> datetime:
    return datetime.combine(day, open_time, tzinfo=tz)


# ------------------------------------------------------------------ list cache
_CACHE: dict[str, tuple[datetime, list]] = {}
_CACHE_LOCK = threading.Lock()
LIST_TTL_SECONDS = 6 * 3600


def cached_list(key: str, now: datetime, loader: Callable[[], list | None], ttl: int = LIST_TTL_SECONDS) -> tuple[list | None, bool]:
    """(items, from_cache). ``loader`` returning None means failure and is not cached."""
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1], True
    items = loader()
    if items is None:
        return None, False
    with _CACHE_LOCK:
        _CACHE[key] = (now + timedelta(seconds=ttl), items)
    return items, False


def clear_list_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


# ------------------------------------------------------------------ name matching
def match_listing(query: str, listing: Sequence[Any], limit: int = 10) -> list[tuple[Any, str]]:
    """Rank listing entries (``EntityRef``) against a Chinese/English name or abbreviation; never picks one."""
    q = query.strip().casefold()
    if not q:
        return []
    scored: list[tuple[int, str, Any, str]] = []
    for ref in listing:
        ids = ref.identifiers
        names = [(ref.company_name or "").casefold(), ids.get("fullname", "").casefold(), ids.get("enname", "").casefold()]
        spell = ids.get("cnspell", "").casefold()
        if q in names:
            scored.append((0, ref.entity_id, ref, "name_exact"))
        elif spell and q == spell:
            scored.append((1, ref.entity_id, ref, "name_exact"))
        elif any(n.startswith(q) for n in names if n) or (spell and spell.startswith(q) and len(q) >= 2):
            scored.append((2, ref.entity_id, ref, "name_prefix"))
        elif any(q in n for n in names if n):
            scored.append((3, ref.entity_id, ref, "name_contains"))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [(ref, basis) for _s, _i, ref, basis in scored[:limit]]
