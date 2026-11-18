"""Permission-gated daily close evidence from explicitly configured instruments.

Configuration is separate from the staging market snapshot manifest: that
manifest does not grant storage, model input, display, or export rights.
Twelve Data period semantics: https://support.twelvedata.com/en/articles/5214728-getting-historical-data
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from threading import Event
from typing import Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .market_provider import MAX_VENDOR_BYTES
from .openai_compatible import _default_binary_transport, _TransportFailure
from .phase3 import ProviderFailure
from .research_sources import ResearchSource, SourceInput, SourcePermissions
from .transport_diagnostics import TransportDiagnostics


class HistoricalInstrument(BaseModel):
    model_config = ConfigDict(extra='forbid')
    instrument_id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=80)
    exchange: str = Field(min_length=1, max_length=80)
    currency: str = Field(min_length=1, max_length=16)
    quote_unit: str | None = Field(default=None, min_length=1, max_length=40)
    exchange_timezone: str = 'UTC'
    adjustment: str = Field(pattern='^(none|splits|dividends|all)$')

    @field_validator('exchange_timezone')
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError('Exchange timezone must be an IANA timezone.') from None
        return value


class HistoricalPriceConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # Operator must establish entitlement and each use right independently.
    provider: str = Field(pattern='^twelve_data$')
    authorized: bool = False
    permissions: SourcePermissions
    instruments: list[HistoricalInstrument]


def configured_historical_prices() -> tuple[HistoricalPriceConfig | None, str]:
    raw = os.environ.get('FORESIGHTNINE_RESEARCH_HISTORICAL_PRICES', '')
    key = os.environ.get('FORESIGHTNINE_TWELVEDATA_API_KEY', '').strip()
    if not raw or not key:
        return None, ''
    try:
        config = HistoricalPriceConfig.model_validate_json(raw)
    except ValidationError:
        return None, ''
    if (not config.authorized or not config.permissions.store or not config.permissions.model_input
            or not config.permissions.display or not config.instruments):
        return None, ''
    if len({item.instrument_id for item in config.instruments}) != len(config.instruments):
        return None, ''
    return config, key


def fetch_historical_price_source(*, config: HistoricalPriceConfig, instrument_id: str,
                                  start_date: date, end_date: date, api_key: str,
                                  persist: Callable[[SourceInput, dict], ResearchSource],
                                  timeout_seconds: float = 8.0, cancel_event: Event | None = None,
                                  fetch: Callable[..., bytes] | None = None) -> ResearchSource | None:
    """Fetch and persist only declared, authorized coverage; no coverage is inferred.

    ``persist`` receives the source and provenance and must durably store them.
    Returns None when configuration, permission or identity is unavailable.
    Network or malformed provider responses raise; callers must reconcile an
    unknown external outcome instead of replaying automatically.
    """
    matches = [item for item in config.instruments if item.instrument_id == instrument_id]
    if (not config.authorized or not config.permissions.store or not config.permissions.model_input
            or not config.permissions.display or not api_key or len(matches) != 1):
        return None
    if start_date > end_date:
        raise ValueError('Invalid historical price date range.')
    item = matches[0]
    if end_date >= datetime.now(ZoneInfo(item.exchange_timezone)).date():
        raise ProviderFailure('historical_price_session_unconfirmed', 'Choose an end date before the current exchange session date.', retryable=False)
    query = urlencode({'symbol': item.symbol, 'exchange': item.exchange, 'interval': '1day',
                       'start_date': start_date.isoformat(), 'end_date': end_date.isoformat(),
                       'order': 'ASC', 'adjust': item.adjustment})
    url = 'https://api.twelvedata.com/time_series?' + query
    headers = {'Authorization': 'apikey ' + api_key}
    if fetch is not None:
        payload = fetch(url, 'application/json', timeout_seconds, headers=headers)
    else:
        diagnostic = TransportDiagnostics()
        try:
            response = _default_binary_transport('GET', url, {**headers, 'Accept': 'application/json'}, b'',
                timeout_seconds, max_response_bytes=MAX_VENDOR_BYTES, diagnostics=diagnostic, cancel_event=cancel_event)
        except _TransportFailure:
            state = diagnostic.snapshot()['submission_state']
            failure = ProviderFailure('historical_price_transport_error', 'Historical price retrieval failed.', retryable=state == 'not_started')
            failure.external_result_unknown = state == 'started_unknown'
            raise failure from None
        if not 200 <= response.status_code < 300:
            raise ProviderFailure('historical_price_provider_rejected', 'The price provider could not serve this series.', retryable=False)
        payload = response.body
    if len(payload) > MAX_VENDOR_BYTES:
        raise ValueError('Historical price response exceeded size limit.')
    try:
        doc = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError('Historical price response is invalid.') from error
    if not isinstance(doc, dict) or not isinstance(doc.get('meta'), dict) or not isinstance(doc.get('values'), list):
        raise ValueError('Historical price provider returned no usable series.')
    meta = doc['meta']
    if (meta.get('symbol') != item.symbol or meta.get('exchange') != item.exchange
            or meta.get('currency') != item.currency or meta.get('interval') != '1day'):
        raise ValueError('Historical price provider identity does not match configured instrument.')
    rows: dict[date, Decimal] = {}
    for row in doc['values']:
        try:
            session = date.fromisoformat(row['datetime'])
            close = Decimal(str(row['close']))
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise ValueError('Historical price row is invalid.') from error
        if session < start_date or session > end_date or session in rows or not close.is_finite() or close <= 0:
            raise ValueError('Historical price row has an invalid date or value.')
        rows[session] = close
    if not rows or len(rows) >= 5000:
        raise ValueError('Historical price series is empty or may be truncated.')
    unit = f'{item.quote_unit} ({item.currency})' if item.quote_unit else item.currency
    text = '\n'.join(f'{item.instrument_id} | {session.isoformat()} | daily close | {close} {unit}'
                     for session, close in sorted(rows.items()))
    request = SourceInput(title=f'{item.instrument_id} daily close ({start_date} to {end_date})', text=text,
                          kind='market_data', permissions=config.permissions)
    provenance = {'provider': 'twelve_data', 'endpoint': 'time_series', 'instrument_id': item.instrument_id,
                  'symbol': item.symbol, 'exchange': item.exchange, 'currency': item.currency, 'quote_unit': item.quote_unit,
                  'interval': '1day', 'exchange_timezone': item.exchange_timezone,
                  'adjustment': item.adjustment, 'start_date': start_date.isoformat(),
                  'end_date': end_date.isoformat(), 'actual_first_date': min(rows).isoformat(),
                  'actual_last_date': max(rows).isoformat(), 'row_count': len(rows),
                  'provider_response_sha256': sha256(payload).hexdigest(),
                  'limitation': 'Provider coverage and revisions are not independently verified; missing sessions are not filled.'}
    return persist(request, provenance)
