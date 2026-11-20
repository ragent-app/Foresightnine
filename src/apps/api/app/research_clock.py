"""Run-scoped research clock and regular exchange sessions.

The clock is captured once when a research run is created and kept in the
run's first model message, so every model call in the run (including resumed
calls) sees the same values. It must never be placed in the static system
prompt or tool definitions, which are shared prefix-cache material.

Exchange sessions use regular trading hours only. Holidays are not modelled:
a holiday is reported as an ordinary trading day, which is safe because the
provider then simply returns no row for that day. Half days and auction
phases are not modelled either.
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
EXCHANGE_REGISTRY_VERSION = '2026-11-20'
EXCHANGES: dict[str, ExchangeSession] = {item.code: item for item in (
    ExchangeSession('NYSE', 'New York Stock Exchange', 'America/New_York', time(9, 30), time(16, 0)),
    ExchangeSession('NASDAQ', 'Nasdaq', 'America/New_York', time(9, 30), time(16, 0)),
    ExchangeSession('TSX', 'Toronto Stock Exchange', 'America/Toronto', time(9, 30), time(16, 0)),
    ExchangeSession('LSE', 'London Stock Exchange', 'Europe/London', time(8, 0), time(16, 30)),
    ExchangeSession('XETRA', 'Deutsche Boerse Xetra', 'Europe/Berlin', time(9, 0), time(17, 30)),
    ExchangeSession('EURONEXT', 'Euronext Paris/Amsterdam', 'Europe/Paris', time(9, 0), time(17, 30)),
    ExchangeSession('SSE', 'Shanghai Stock Exchange', 'Asia/Shanghai', time(9, 30), time(15, 0), (time(11, 30), time(13, 0))),
    ExchangeSession('SZSE', 'Shenzhen Stock Exchange', 'Asia/Shanghai', time(9, 30), time(15, 0), (time(11, 30), time(13, 0))),
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


def _local(now: datetime, session: ExchangeSession) -> datetime:
    if now.tzinfo is None:
        raise ValueError('Research clock requires a timezone-aware time.')
    return now.astimezone(ZoneInfo(session.timezone))


def _is_weekday(day: date, session: ExchangeSession) -> bool:
    return day.weekday() not in session.weekend


def session_status(now: datetime, session: ExchangeSession) -> SessionStatus:
    local = _local(now, session)
    if not _is_weekday(local.date(), session):
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
    """Latest weekday whose regular session closed at least ``settle_minutes`` ago."""
    local = _local(now, session)
    close_today = datetime.combine(local.date(), session.close, tzinfo=local.tzinfo)
    day = local.date()
    if not (_is_weekday(day, session) and local >= close_today + timedelta(minutes=settle_minutes)):
        day -= timedelta(days=1)
    while not _is_weekday(day, session):
        day -= timedelta(days=1)
    return day


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
        'note': 'Regular sessions only; exchange holidays are not modelled.',
        'exchanges': rows,
    }
