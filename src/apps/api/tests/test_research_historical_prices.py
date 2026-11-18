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
