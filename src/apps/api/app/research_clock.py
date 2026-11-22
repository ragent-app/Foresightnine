"""Run-scoped research clock and regular exchange sessions.

The clock is captured once when a research run is created and kept in the
run's first model message, so every model call in the run (including resumed
calls) sees the same values. It must never be placed in the static system
prompt or tool definitions, which are shared prefix-cache material.

Exchange sessions use regular trading hours only. Deterministic full-day
exchange holidays are modelled for 2024–2027 across major exchanges
(``NYSE``, ``NASDAQ``, ``SSE``, ``SZSE``, ``BJSE``, ``HKEX``, ``TSE``,
``KRX``, ``LSE``); other exchanges and years treat weekdays as trading days.
Half days and auction phases are not modelled.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Literal
from zoneinfo import ZoneInfo

SessionStatus = Literal['pre_open', 'open', 'lunch_break', 'closed', 'non_trading_day']

# Providers publish the final daily bar shortly after the close; until then the
# day is treated as unfinished even though trading has stopped.
DAILY_BAR_SETTLE_MINUTES = 30

HOLIDAY_YEARS: frozenset[int] = frozenset({2024, 2025, 2026, 2027})


def _dates(*isos: str) -> frozenset[date]:
    return frozenset(date.fromisoformat(raw) for raw in isos)


_US_HOLIDAYS: dict[int, frozenset[date]] = {
    2024: _dates(
        '2024-01-01', '2024-01-15', '2024-02-19', '2024-03-29', '2024-05-27',
        '2024-06-19', '2024-07-04', '2024-09-02', '2024-11-28', '2024-12-25',
    ),
    2025: _dates(
        '2025-01-01', '2025-01-09', '2025-01-20', '2025-02-17', '2025-04-18',
        '2025-05-26', '2025-06-19', '2025-07-04', '2025-09-01', '2025-11-27',
        '2025-12-25',
    ),
    2026: _dates(
        '2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03', '2026-05-25',
        '2026-06-19', '2026-07-03', '2026-09-07', '2026-11-26', '2026-12-25',
    ),
    2027: _dates(
        '2027-01-01', '2027-01-18', '2027-02-15', '2027-03-26', '2027-05-31',
        '2027-06-18', '2027-07-05', '2027-09-06', '2027-11-25', '2027-12-24',
    ),
}

_CN_HOLIDAYS: dict[int, frozenset[date]] = {
    2024: _dates(
        '2024-01-01',
        '2024-02-09', '2024-02-12', '2024-02-13', '2024-02-14', '2024-02-15', '2024-02-16',
        '2024-04-04', '2024-04-05',
        '2024-05-01', '2024-05-02', '2024-05-03',
        '2024-06-10',
        '2024-09-16', '2024-09-17',
        '2024-10-01', '2024-10-02', '2024-10-03', '2024-10-04', '2024-10-07',
    ),
    2025: _dates(
        '2025-01-01',
        '2025-01-28', '2025-01-29', '2025-01-30', '2025-01-31', '2025-02-03', '2025-02-04',
        '2025-04-04',
        '2025-05-01', '2025-05-02', '2025-05-05',
        '2025-06-02',
        '2025-10-01', '2025-10-02', '2025-10-03', '2025-10-06', '2025-10-07', '2025-10-08',
    ),
    2026: _dates(
        '2026-01-01', '2026-01-02',
        '2026-02-16', '2026-02-17', '2026-02-18', '2026-02-19', '2026-02-20', '2026-02-23',
        '2026-04-06',
        '2026-05-01', '2026-05-04', '2026-05-05',
        '2026-06-19',
        '2026-09-25',
        '2026-10-01', '2026-10-02', '2026-10-05', '2026-10-06', '2026-10-07',
    ),
    2027: _dates(
        '2027-01-01',
        '2027-02-05', '2027-02-08', '2027-02-09', '2027-02-10', '2027-02-11', '2027-02-12',
        '2027-04-05',
        '2027-05-03', '2027-05-04', '2027-05-05',
        '2027-06-09',
        '2027-09-15',
        '2027-10-01', '2027-10-04', '2027-10-05', '2027-10-06', '2027-10-07',
    ),
}

_HKEX_HOLIDAYS: dict[int, frozenset[date]] = {
    2024: _dates(
        '2024-01-01', '2024-02-12', '2024-02-13', '2024-03-29', '2024-04-01',
        '2024-04-04', '2024-05-01', '2024-05-15', '2024-06-10', '2024-07-01',
        '2024-09-18', '2024-10-01', '2024-10-11', '2024-12-25', '2024-12-26',
    ),
    2025: _dates(
        '2025-01-01', '2025-01-29', '2025-01-30', '2025-01-31', '2025-04-04',
        '2025-04-18', '2025-04-21', '2025-05-01', '2025-05-05', '2025-07-01',
        '2025-10-01', '2025-10-07', '2025-10-29', '2025-12-25', '2025-12-26',
    ),
    2026: _dates(
        '2026-01-01', '2026-02-17', '2026-02-18', '2026-02-19', '2026-04-03',
        '2026-04-06', '2026-04-07', '2026-05-01', '2026-05-25', '2026-06-19',
        '2026-07-01', '2026-10-01', '2026-10-19', '2026-12-25',
    ),
    2027: _dates(
        '2027-01-01', '2027-02-08', '2027-02-09', '2027-03-26', '2027-03-29',
        '2027-04-05', '2027-05-13', '2027-06-09', '2027-07-01', '2027-09-16',
        '2027-10-01', '2027-10-08', '2027-12-27',
    ),
}

_TSE_HOLIDAYS: dict[int, frozenset[date]] = {
    2024: _dates(
        '2024-01-01', '2024-01-02', '2024-01-03', '2024-01-08', '2024-02-12',
        '2024-02-23', '2024-03-20', '2024-04-29', '2024-05-03', '2024-05-06',
        '2024-07-15', '2024-08-12', '2024-09-16', '2024-09-23', '2024-10-14',
        '2024-11-04', '2024-12-31',
    ),
    2025: _dates(
        '2025-01-01', '2025-01-02', '2025-01-03', '2025-01-13', '2025-02-11',
        '2025-02-24', '2025-03-20', '2025-04-29', '2025-05-05', '2025-05-06',
        '2025-07-21', '2025-08-11', '2025-09-15', '2025-09-23', '2025-10-13',
        '2025-11-03', '2025-11-24', '2025-12-31',
    ),
    2026: _dates(
        '2026-01-01', '2026-01-02', '2026-01-12', '2026-02-11', '2026-02-23',
        '2026-03-20', '2026-04-29', '2026-05-04', '2026-05-05', '2026-05-06',
        '2026-07-20', '2026-08-11', '2026-09-21', '2026-09-22', '2026-09-23',
        '2026-10-12', '2026-11-03', '2026-11-23', '2026-12-31',
    ),
    2027: _dates(
        '2027-01-01', '2027-01-11', '2027-02-11', '2027-02-23', '2027-03-22',
        '2027-04-29', '2027-05-03', '2027-05-04', '2027-05-05', '2027-07-19',
        '2027-08-11', '2027-09-20', '2027-09-23', '2027-10-11', '2027-11-03',
        '2027-11-23', '2027-12-31',
    ),
}

_KRX_HOLIDAYS: dict[int, frozenset[date]] = {
    2024: _dates(
        '2024-01-01', '2024-02-09', '2024-02-12', '2024-03-01', '2024-04-10',
        '2024-05-01', '2024-05-06', '2024-05-15', '2024-06-06', '2024-08-15',
        '2024-09-16', '2024-09-17', '2024-09-18', '2024-10-01', '2024-10-03',
        '2024-10-09', '2024-12-25', '2024-12-31',
    ),
    2025: _dates(
        '2025-01-01', '2025-01-27', '2025-01-28', '2025-01-29', '2025-01-30',
        '2025-03-03', '2025-05-01', '2025-05-05', '2025-05-06', '2025-06-03',
        '2025-06-06', '2025-08-15', '2025-10-03', '2025-10-06', '2025-10-07',
        '2025-10-08', '2025-10-09', '2025-12-25', '2025-12-31',
    ),
    2026: _dates(
        '2026-01-01', '2026-02-16', '2026-02-17', '2026-02-18', '2026-03-02',
        '2026-05-01', '2026-05-05', '2026-05-25', '2026-06-03', '2026-07-17',
        '2026-08-17', '2026-09-24', '2026-09-25', '2026-10-05', '2026-10-09',
        '2026-12-25', '2026-12-31',
    ),
    2027: _dates(
        '2027-01-01', '2027-02-08', '2027-02-09', '2027-03-01', '2027-05-03',
        '2027-05-05', '2027-05-13', '2027-07-19', '2027-08-16', '2027-09-14',
        '2027-09-15', '2027-09-16', '2027-10-04', '2027-10-11', '2027-12-27',
        '2027-12-31',
    ),
}

_LSE_HOLIDAYS: dict[int, frozenset[date]] = {
    2024: _dates(
        '2024-01-01', '2024-03-29', '2024-04-01', '2024-05-06', '2024-05-27',
        '2024-08-26', '2024-12-25', '2024-12-26',
    ),
    2025: _dates(
        '2025-01-01', '2025-04-18', '2025-04-21', '2025-05-05', '2025-05-26',
        '2025-08-25', '2025-12-25', '2025-12-26',
    ),
    2026: _dates(
        '2026-01-01', '2026-04-03', '2026-04-06', '2026-05-04', '2026-05-25',
        '2026-08-31', '2026-12-25', '2026-12-28',
    ),
    2027: _dates(
        '2027-01-01', '2027-03-26', '2027-03-29', '2027-05-03', '2027-05-31',
        '2027-08-30', '2027-12-27', '2027-12-28',
    ),
}

EXCHANGE_HOLIDAYS: dict[str, dict[int, frozenset[date]]] = {
    'NYSE': _US_HOLIDAYS,
    'NASDAQ': _US_HOLIDAYS,
    'SSE': _CN_HOLIDAYS,
    'SZSE': _CN_HOLIDAYS,
    'BJSE': _CN_HOLIDAYS,
    'HKEX': _HKEX_HOLIDAYS,
    'TSE': _TSE_HOLIDAYS,
    'KRX': _KRX_HOLIDAYS,
    'LSE': _LSE_HOLIDAYS,
}


@dataclass(frozen=True)
class ExchangeSession:
    code: str
    name: str
    timezone: str
    open: time
    close: time
    lunch: tuple[time, time] | None = None
    weekend: tuple[int, ...] = (5, 6)  # Monday=0


# Regular sessions of the main global markets (registry version below). Codes
# follow the names used by market data providers where a common one exists.
EXCHANGE_REGISTRY_VERSION = '2026-11-21'
EXCHANGES: dict[str, ExchangeSession] = {item.code: item for item in (
    ExchangeSession('NYSE', 'New York Stock Exchange', 'America/New_York', time(9, 30), time(16, 0)),
    ExchangeSession('NASDAQ', 'Nasdaq', 'America/New_York', time(9, 30), time(16, 0)),
    ExchangeSession('TSX', 'Toronto Stock Exchange', 'America/Toronto', time(9, 30), time(16, 0)),
    ExchangeSession('LSE', 'London Stock Exchange', 'Europe/London', time(8, 0), time(16, 30)),
    ExchangeSession('XETRA', 'Deutsche Boerse Xetra', 'Europe/Berlin', time(9, 0), time(17, 30)),
    ExchangeSession('EURONEXT', 'Euronext Paris/Amsterdam', 'Europe/Paris', time(9, 0), time(17, 30)),
    ExchangeSession('SSE', 'Shanghai Stock Exchange', 'Asia/Shanghai', time(9, 30), time(15, 0), (time(11, 30), time(13, 0))),
    ExchangeSession('SZSE', 'Shenzhen Stock Exchange', 'Asia/Shanghai', time(9, 30), time(15, 0), (time(11, 30), time(13, 0))),
    ExchangeSession('BJSE', 'Beijing Stock Exchange', 'Asia/Shanghai', time(9, 30), time(15, 0), (time(11, 30), time(13, 0))),
    ExchangeSession('HKEX', 'Hong Kong Exchanges', 'Asia/Hong_Kong', time(9, 30), time(16, 0), (time(12, 0), time(13, 0))),
    ExchangeSession('TSE', 'Tokyo Stock Exchange', 'Asia/Tokyo', time(9, 0), time(15, 30), (time(11, 30), time(12, 30))),
    ExchangeSession('KRX', 'Korea Exchange', 'Asia/Seoul', time(9, 0), time(15, 30)),
    ExchangeSession('TWSE', 'Taiwan Stock Exchange', 'Asia/Taipei', time(9, 0), time(13, 30)),
    ExchangeSession('NSE', 'National Stock Exchange of India', 'Asia/Kolkata', time(9, 15), time(15, 30)),
    ExchangeSession('ASX', 'Australian Securities Exchange', 'Australia/Sydney', time(10, 0), time(16, 0)),
)}


def exchange_session(code: str | None) -> ExchangeSession | None:
    if not isinstance(code, str):
        return None
    return EXCHANGES.get(code.strip().upper())


def _exchange_code(exchange: str | ExchangeSession | None) -> str | None:
    if isinstance(exchange, ExchangeSession):
        return exchange.code.upper()
    if isinstance(exchange, str) and exchange.strip():
        return exchange.strip().upper()
    return None


def holidays_modelled(
    exchange: str | ExchangeSession | None,
    year: int | None = None,
    *,
    years: Iterable[int] | None = None,
) -> bool:
    """Return True only when full-day holidays are explicitly modelled for the exchange and year(s)."""
    code = _exchange_code(exchange)
    if code is None:
        return False
    table = EXCHANGE_HOLIDAYS.get(code)
    if table is None:
        return False
    if years is not None:
        year_list = list(years)
        return bool(year_list) and all(y in table for y in year_list)
    if year is not None:
        return year in table
    return True


def is_holiday(day: date, exchange: str | ExchangeSession | None) -> bool:
    """Return True when ``day`` is a known full-day holiday for ``exchange``."""
    code = _exchange_code(exchange)
    if code is None:
        return False
    year_table = EXCHANGE_HOLIDAYS.get(code, {}).get(day.year)
    return day in year_table if year_table is not None else False


def _local(now: datetime, session: ExchangeSession) -> datetime:
    if now.tzinfo is None:
        raise ValueError('Research clock requires a timezone-aware time.')
    return now.astimezone(ZoneInfo(session.timezone))


def _is_weekday(day: date, session: ExchangeSession | str) -> bool:
    resolved = exchange_session(session) if isinstance(session, str) else session
    weekend = resolved.weekend if resolved is not None else (5, 6)
    return day.weekday() not in weekend


def is_trading_day(day: date, session: ExchangeSession | str) -> bool:
    """Return True when ``day`` is a regular trading day (not weekend and not a modelled holiday)."""
    return _is_weekday(day, session) and not is_holiday(day, session)


def session_status(now: datetime, session: ExchangeSession) -> SessionStatus:
    local = _local(now, session)
    if not is_trading_day(local.date(), session):
        return 'non_trading_day'
    current = local.time()
    if current < session.open:
        return 'pre_open'
    if current >= session.close:
        return 'closed'
    if session.lunch and session.lunch[0] <= current < session.lunch[1]:
        return 'lunch_break'
    return 'open'


def last_completed_session(now: datetime, session: ExchangeSession,
                           settle_minutes: int = DAILY_BAR_SETTLE_MINUTES) -> date:
    """Latest trading day whose regular session closed at least ``settle_minutes`` ago."""
    local = _local(now, session)
    close_today = datetime.combine(local.date(), session.close, tzinfo=local.tzinfo)
    day = local.date()
    if not (is_trading_day(day, session) and local >= close_today + timedelta(minutes=settle_minutes)):
        day -= timedelta(days=1)
    while not is_trading_day(day, session):
        day -= timedelta(days=1)
    return day


def trading_days_between(
    after: date,
    up_to: date,
    exchange: str | ExchangeSession | None = None,
) -> int:
    """Trading days in ``(after, up_to]``.

    When ``exchange`` resolves to a known session, skips both exchange weekends and
    any modelled full-day holidays in that exchange's calendar; otherwise skips
    Saturday/Sunday weekdays only.
    """
    if up_to <= after:
        return 0
    session = exchange if isinstance(exchange, ExchangeSession) else exchange_session(exchange)
    total_days = (up_to - after).days
    if session is None:
        return sum(1 for i in range(1, total_days + 1) if (after + timedelta(days=i)).weekday() < 5)
    return sum(1 for i in range(1, total_days + 1) if is_trading_day(after + timedelta(days=i), session))


def build_research_clock(now: datetime, exchanges: Iterable[str] | None = None) -> dict:
    """A compact clock for the run's first model message (date granularity plus audit minute)."""
    utc_now = now.astimezone(timezone.utc)
    codes = list(dict.fromkeys(code.upper() for code in (exchanges or EXCHANGES) if exchange_session(code)))
    rows = []
    for code in codes:
        session = EXCHANGES[code]
        rows.append({
            'exchange': code,
            'local_date': _local(now, session).date().isoformat(),
            'session_status': session_status(now, session),
            'last_completed_session': last_completed_session(now, session).isoformat(),
        })
    return {
        'captured_at': utc_now.replace(second=0, microsecond=0).isoformat().replace('+00:00', 'Z'),
        'utc_date': utc_now.date().isoformat(),
        'exchange_registry_version': EXCHANGE_REGISTRY_VERSION,
        'note': (
            'Regular sessions only; 2024-2027 full-day exchange holidays modelled for '
            'NYSE, NASDAQ, SSE, SZSE, BJSE, HKEX, TSE, KRX, LSE (half days not modelled).'
        ),
        'exchanges': rows,
    }
