from datetime import date
import json

import pytest

from app.research_historical_prices import HistoricalPriceConfig, fetch_historical_price_source


def configuration(**changes):
    data = {'provider': 'twelve_data', 'authorized': True,
            'permissions': {'store': True, 'model_input': True, 'display': True, 'export': False},
            'instruments': [{'instrument_id': 'index:test', 'symbol': 'TEST', 'exchange': 'EX',
                             'currency': 'USD', 'adjustment': 'none'}]}
    data.update(changes)
    return HistoricalPriceConfig.model_validate(data)


def payload(**meta_changes):
    meta = {'symbol': 'TEST', 'exchange': 'EX', 'currency': 'USD', 'interval': '1day'}
    meta.update(meta_changes)
    return json.dumps({'meta': meta, 'values': [{'datetime': '2024-01-02', 'close': '100.25'},
                                                {'datetime': '2024-01-03', 'close': '110.50'}]}).encode()


def call(config, fetch, persist=lambda source, provenance: (source, provenance)):
    return fetch_historical_price_source(config=config, instrument_id='index:test',
        start_date=date(2024, 1, 1), end_date=date(2024, 1, 31), api_key='secret', fetch=fetch, persist=persist)


def test_series_is_quotable_and_keeps_identity_and_actual_dates():
    requests = []
    def fetch(*args, **kwargs):
        requests.append((args, kwargs))
        return payload()
    source, provenance = call(configuration(), fetch)
    assert 'index:test | 2024-01-02 | daily close | 100.25 USD' in source.text
    assert provenance['actual_last_date'] == '2024-01-03'
    assert provenance['row_count'] == 2
    assert provenance['adjustment'] == 'none'
    assert 'secret' not in requests[0][0][0]
    assert not source.permissions.export


def test_entitlement_and_unique_mapping_required_before_network():
    def forbidden(*args, **kwargs):
        raise AssertionError('network was called')
    assert call(configuration(authorized=False), forbidden) is None
    assert call(configuration(permissions={'store': True, 'model_input': False, 'display': True}), forbidden) is None
    config = configuration()
    config.instruments.append(config.instruments[0])
    assert call(config, forbidden) is None


def test_identity_mismatch_is_not_evidence():
    with pytest.raises(ValueError, match='identity'):
        call(configuration(), lambda *a, **k: payload(exchange='OTHER'))


from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from app.phase3 import ProviderFailure
from app.research_historical_prices import LOOKBACK_VALUES, resolve_lookback


def nyse_config():
    return configuration(instruments=[{'instrument_id': 'index:test', 'symbol': 'TEST', 'exchange': 'NYSE',
                                       'currency': 'USD', 'adjustment': 'none',
                                       'exchange_timezone': 'America/New_York'}])


def series(*days):
    meta = {'symbol': 'TEST', 'exchange': 'NYSE', 'currency': 'USD', 'interval': '1day'}
    return json.dumps({'meta': meta, 'values': [{'datetime': d, 'close': '10.5'} for d in days]}).encode()


def run(end, now, days, start=date(2026, 11, 10)):
    urls = []
    def fetch(url, *a, **k):
        urls.append(url)
        return series(*days)
    result = fetch_historical_price_source(config=nyse_config(), instrument_id='index:test', start_date=start,
        end_date=end, api_key='k', fetch=fetch, now=now, persist=lambda s, p: (s, p))
    return result, urls


# 2026-11-19 is a Thursday; 21:00 UTC is 16:00 New York (EST), the close.
EVENING = datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc)   # 20:00 NY on 11-19, settled
AT_CLOSE = datetime(2026, 11, 19, 21, 10, tzinfo=timezone.utc)  # 16:10 NY, not settled
AFTER_SETTLE = datetime(2026, 11, 19, 21, 30, tzinfo=timezone.utc)


def test_end_date_after_close_is_clamped_to_today_and_request_is_exclusive():
    (source, prov), urls = run(date(2026, 11, 20), EVENING, ['2026-11-18', '2026-11-19'])
    query = parse_qs(urlparse(urls[0]).query)
    assert query['end_date'] == ['2026-11-20']  # effective 11-19 plus one day
    assert prov['clamped'] is True and prov['effective_end_date'] == '2026-11-19'
    assert prov['requested_end_date'] == '2026-11-20' and prov['end_date'] == '2026-11-20'
    assert prov['latest_completed_session'] == '2026-11-19' and prov['session_status'] == 'closed'
    assert prov['clock_now'] == EVENING.isoformat()
    assert source.title == 'index:test daily close (2026-11-18 to 2026-11-19)'


def test_before_settle_clamps_to_previous_session():
    (_, prov), urls = run(date(2026, 11, 19), AT_CLOSE, ['2026-11-17', '2026-11-18'])
    assert prov['effective_end_date'] == '2026-11-18' and prov['clamped'] is True
    assert parse_qs(urlparse(urls[0]).query)['end_date'] == ['2026-11-19']
    (_, prov), _ = run(date(2026, 11, 19), AFTER_SETTLE, ['2026-11-19'])
    assert prov['effective_end_date'] == '2026-11-19' and prov['clamped'] is False


def test_past_end_date_is_not_clamped_and_inclusive_day_requested():
    (_, prov), urls = run(date(2025, 12, 31), EVENING, ['2025-12-30', '2025-12-31'], start=date(2025, 12, 1))
    assert prov['clamped'] is False
    assert parse_qs(urlparse(urls[0]).query)['end_date'] == ['2026-01-01']


def test_rows_after_effective_end_are_dropped_not_rejected():
    (source, prov), _ = run(date(2026, 11, 20), EVENING, ['2026-11-18', '2026-11-19', '2026-11-20'])
    assert prov['actual_last_date'] == '2026-11-19' and prov['row_count'] == 2
    assert '2026-11-20' not in source.text


def test_rows_before_start_or_duplicated_remain_invalid():
    with pytest.raises(ValueError):
        run(date(2026, 11, 20), EVENING, ['2026-11-01'])
    with pytest.raises(ValueError):
        run(date(2026, 11, 20), EVENING, ['2026-11-18', '2026-11-18'])


def test_range_after_latest_session_reports_hint():
    with pytest.raises(ProviderFailure) as info:
        run(date(2026, 11, 25), EVENING, [], start=date(2026, 11, 23))
    assert info.value.code == 'historical_price_range_after_latest_session'
    assert info.value.hints == {'latest_allowed_end_date': '2026-11-19'}
    assert '2026-11-19' in str(info.value) and info.value.retryable is False


def test_unknown_exchange_falls_back_to_timezone_previous_weekday():
    config = configuration(instruments=[{'instrument_id': 'index:test', 'symbol': 'TEST', 'exchange': 'EX',
                                         'currency': 'USD', 'adjustment': 'none', 'exchange_timezone': 'America/New_York'}])
    payload_ = json.dumps({'meta': {'symbol': 'TEST', 'exchange': 'EX', 'currency': 'USD', 'interval': '1day'},
                           'values': [{'datetime': '2026-11-18', 'close': '1'}]}).encode()
    source, prov = fetch_historical_price_source(config=config, instrument_id='index:test',
        start_date=date(2026, 11, 10), end_date=date(2026, 11, 30), api_key='k', now=EVENING,
        fetch=lambda *a, **k: payload_, persist=lambda s, p: (s, p))
    assert prov['effective_end_date'] == '2026-11-18'  # previous weekday before local 11-19
    assert prov['session_status'] is None


def test_resolve_lookback_values():
    assert LOOKBACK_VALUES == ('5d', '1m', '3m', '6m', 'ytd', '1y', '3y', '5y', '10y')
    kw = dict(now=EVENING, exchange='NYSE', timezone_name='America/New_York')
    assert resolve_lookback('5d', **kw) == (date(2026, 11, 13), date(2026, 11, 19))
    assert resolve_lookback('1m', **kw) == (date(2026, 10, 19), date(2026, 11, 19))
    assert resolve_lookback('3m', **kw) == (date(2026, 8, 19), date(2026, 11, 19))
    assert resolve_lookback('ytd', **kw) == (date(2026, 1, 1), date(2026, 11, 19))
    assert resolve_lookback('1y', **kw) == (date(2025, 11, 19), date(2026, 11, 19))
    assert resolve_lookback('10y', **kw)[0] == date(2016, 11, 19)
    # month-end clamp and weekend now
    sat = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)  # Monday 11-... ; end = Mon 8-31 is open -> prior Fri
    start, end = resolve_lookback('6m', now=sat, exchange=None, timezone_name='UTC')
    assert end == date(2026, 8, 28) and start == date(2026, 2, 28)
    with pytest.raises(ValueError):
        resolve_lookback('2w', **kw)
