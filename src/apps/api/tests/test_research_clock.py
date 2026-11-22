from datetime import date, datetime, timezone

import pytest

from app.research_clock import (EXCHANGES, build_research_clock, exchange_session, last_completed_session,
                                session_status)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_registry_lookup_is_case_insensitive_and_rejects_unknown():
    assert exchange_session(' nyse ') is EXCHANGES['NYSE']
    assert exchange_session('NOPE') is None and exchange_session(None) is None


def test_naive_time_is_rejected():
    with pytest.raises(ValueError):
        session_status(datetime(2026, 11, 19, 12), EXCHANGES['NYSE'])


def test_nyse_phases_in_winter():
    s = EXCHANGES['NYSE']
    assert session_status(utc(2026, 11, 19, 13, 29), s) == 'pre_open'  # 08:29 EST
    assert session_status(utc(2026, 11, 19, 14, 30), s) == 'open'      # 09:30 EST
    assert session_status(utc(2026, 11, 19, 20, 59), s) == 'open'
    assert session_status(utc(2026, 11, 19, 21, 0), s) == 'closed'     # 16:00 EST


def test_weekend_is_non_trading_day():
    s = EXCHANGES['NYSE']
    assert session_status(utc(2026, 11, 21, 15), s) == 'non_trading_day'
    assert session_status(utc(2026, 11, 22, 15), s) == 'non_trading_day'


def test_lunch_break_for_asian_exchanges():
    assert session_status(utc(2026, 11, 19, 4, 0), EXCHANGES['SSE']) == 'lunch_break'  # 12:00 CST
    assert session_status(utc(2026, 11, 19, 3, 0), EXCHANGES['SSE']) == 'open'          # 11:00
    assert session_status(utc(2026, 11, 19, 5, 0), EXCHANGES['SSE']) == 'open'          # 13:00
    assert session_status(utc(2026, 11, 19, 3, 30), EXCHANGES['HKEX']) == 'open'          # 11:30 HKT
    assert session_status(utc(2026, 11, 19, 4, 30), EXCHANGES['HKEX']) == 'lunch_break'
    assert session_status(utc(2026, 11, 19, 2, 45), EXCHANGES['TSE']) == 'lunch_break'  # 11:45 JST


def test_dst_changes_utc_offset_of_nyse_open():
    s = EXCHANGES['NYSE']
    # Fri 2026-03-06 is EST (UTC-5); Mon 2026-03-09 is EDT (UTC-4) after the Mar 8 change.
    assert session_status(utc(2026, 3, 6, 14, 29), s) == 'pre_open'
    assert session_status(utc(2026, 3, 6, 14, 30), s) == 'open'
    assert session_status(utc(2026, 3, 9, 13, 29), s) == 'pre_open'
    assert session_status(utc(2026, 3, 9, 13, 30), s) == 'open'
    # Autumn: EDT until Nov 1, 2026.
    assert session_status(utc(2026, 10, 30, 20, 0), s) == 'closed'
    assert session_status(utc(2026, 11, 2, 20, 59), s) == 'open'


def test_dst_changes_utc_offset_of_lse_open():
    s = EXCHANGES['LSE']
    assert session_status(utc(2026, 3, 27, 7, 59), s) == 'pre_open'  # GMT
    assert session_status(utc(2026, 3, 27, 8, 0), s) == 'open'
    assert session_status(utc(2026, 3, 30, 6, 59), s) == 'pre_open'  # BST from Mar 29
    assert session_status(utc(2026, 3, 30, 7, 0), s) == 'open'
    assert session_status(utc(2026, 3, 30, 15, 30), s) == 'closed'


def test_last_completed_session_requires_settle_minutes():
    s = EXCHANGES['NYSE']
    assert last_completed_session(utc(2026, 11, 19, 21, 29), s) == date(2026, 11, 18)
    assert last_completed_session(utc(2026, 11, 19, 21, 30), s) == date(2026, 11, 19)
    assert last_completed_session(utc(2026, 11, 19, 16, 0), s) == date(2026, 11, 18)
    assert last_completed_session(utc(2026, 11, 19, 21, 1), s, settle_minutes=0) == date(2026, 11, 19)


def test_last_completed_session_skips_weekends():
    s = EXCHANGES['NYSE']
    assert last_completed_session(utc(2026, 11, 20, 22), s) == date(2026, 11, 20)  # Fri after settle
    assert last_completed_session(utc(2026, 11, 21, 15), s) == date(2026, 11, 20)  # Sat
    assert last_completed_session(utc(2026, 11, 22, 15), s) == date(2026, 11, 20)  # Sun
    assert last_completed_session(utc(2026, 11, 23, 14), s) == date(2026, 11, 20)  # Mon pre-open


def test_last_completed_session_across_dst_and_timezone_date_line():
    ny, lse = EXCHANGES['NYSE'], EXCHANGES['LSE']
    assert last_completed_session(utc(2026, 3, 9, 20, 29), ny) == date(2026, 3, 6)
    assert last_completed_session(utc(2026, 3, 9, 20, 30), ny) == date(2026, 3, 9)
    assert last_completed_session(utc(2026, 3, 6, 21, 30), ny) == date(2026, 3, 6)  # EST: 16:30
    assert last_completed_session(utc(2026, 3, 30, 15, 59), lse) == date(2026, 3, 27)
    assert last_completed_session(utc(2026, 3, 30, 16, 0), lse) == date(2026, 3, 30)
    # Same instant: Tokyo is already on Friday 06:30 (pre-open); Thursday is its last completed session.
    instant = utc(2026, 11, 19, 21, 30)
    assert last_completed_session(instant, EXCHANGES['TSE']) == date(2026, 11, 19)
    assert session_status(instant, EXCHANGES['TSE']) == 'pre_open'


def test_build_research_clock_structure():
    clock = build_research_clock(utc(2026, 11, 19, 21, 37, 45, 123), ['nyse', 'LSE', 'nyse', 'BOGUS'])
    assert clock['captured_at'] == '2026-11-19T21:37:00Z'
    assert clock['utc_date'] == '2026-11-19'
    assert clock['exchange_registry_version']
    assert [row['exchange'] for row in clock['exchanges']] == ['NYSE', 'LSE']
    nyse = clock['exchanges'][0]
    assert nyse == {'exchange': 'NYSE', 'local_date': '2026-11-19', 'session_status': 'closed',
                    'last_completed_session': '2026-11-19'}
    assert clock['exchanges'][1]['session_status'] == 'closed'


def test_build_research_clock_defaults_to_all_exchanges():
    clock = build_research_clock(utc(2026, 11, 21, 12))
    assert len(clock['exchanges']) == len(EXCHANGES)
    assert all(row['last_completed_session'] == '2026-11-20' or row['exchange'] in ('TSE', 'KRX', 'ASX', 'NSE', 'SSE', 'SZSE', 'HKEX', 'TWSE')
               for row in clock['exchanges'])
    assert {row['session_status'] for row in clock['exchanges']} <= {'non_trading_day', 'closed'}


def test_exchange_holiday_calendars_2024_to_2027_and_session_helpers():
    from app.research_clock import holidays_modelled, is_holiday, is_trading_day, trading_days_between

    # All 9 target exchanges are modelled for 2024-2027; outside years or unmodelled exchanges return False cleanly.
    for ex_code in ('NYSE', 'NASDAQ', 'SSE', 'SZSE', 'BJSE', 'HKEX', 'TSE', 'KRX', 'LSE'):
        assert ex_code in EXCHANGES
        assert holidays_modelled(ex_code, years=(2024, 2025, 2026, 2027)) is True
        assert holidays_modelled(ex_code, years=(2028,)) is False
        assert holidays_modelled(ex_code, years=(2023,)) is False
    assert holidays_modelled('ASX', years=(2026,)) is False

    # NYSE Thanksgiving 2026-11-26 (Thursday) during US regular trading hours -> non_trading_day, last session Wed 2026-11-25.
    thanksgiving_utc = utc(2026, 11, 26, 16, 0)
    assert is_holiday(date(2026, 11, 26), 'NYSE') is True
    assert is_trading_day(date(2026, 11, 26), 'NYSE') is False
    assert session_status(thanksgiving_utc, EXCHANGES['NYSE']) == 'non_trading_day'
    assert last_completed_session(thanksgiving_utc, EXCHANGES['NYSE']) == date(2026, 11, 25)
    # Jimmy Carter National Day of Mourning 2025-01-09.
    assert is_holiday(date(2025, 1, 9), 'NYSE') is True
    assert is_holiday(date(2025, 1, 9), 'NASDAQ') is True

    # China (SSE/SZSE/BJSE) 2026 National Day holiday & 2027 Spring Festival; HKEX 2027 Good Friday; TSE Mountain Day; KRX Constitution Day; LSE Early May Bank Holiday.
    assert is_holiday(date(2026, 10, 1), 'SSE') is True
    assert is_holiday(date(2027, 2, 8), 'SZSE') is True
    assert is_holiday(date(2027, 2, 9), 'BJSE') is True
    assert is_holiday(date(2027, 3, 26), 'HKEX') is True
    assert is_holiday(date(2026, 8, 11), 'TSE') is True
    assert is_holiday(date(2026, 7, 17), 'KRX') is True
    assert is_holiday(date(2026, 5, 4), 'LSE') is True

    # Trading days between skips holidays when exchange + year are covered.
    assert trading_days_between(date(2026, 11, 25), date(2026, 11, 27), exchange='NYSE') == 1
    assert trading_days_between(date(2026, 11, 25), date(2026, 11, 27)) == 2
    assert trading_days_between(date(2026, 12, 31), date(2027, 1, 4), exchange='NYSE') == 1  # skips 2027-01-01 + weekend

