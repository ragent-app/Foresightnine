from datetime import datetime, timezone
from decimal import Decimal

from app.research_calculations import CalculationRequest, calculate
from app.research_return_context import price_return_context
from app.research_sources import ResearchSource, SourcePermissions


def source(source_id, text, kind, provenance):
    return ResearchSource(source_id=source_id, owner_id='u', title=source_id, text=text, kind=kind,
                          obtained_at=datetime(2026, 1, 1, tzinfo=timezone.utc), content_sha256='hash',
                          permissions=SourcePermissions(store=True, display=True, model_input=True, export=True), provenance=provenance)


def test_returns_preserve_price_entry_dates_basis_and_unverified_issue_identity():
    issue = source('issue', 'IPO issue price 100 USD', 'user_supplied', {})
    market = source('price', 'IPO@NYSE | 2025-01-06 | 1d | O 117 H 118 L 116 C 117.07 V 100 USD', 'market_data', {'adjustment': 'raw'})
    f_start = {'fact_id': 'issue', 'value': '100', 'unit': 'USD', 'currency': 'USD', 'period': '2025-01-02', 'label': 'IPO issue price', 'source_id': 'issue', 'quotation': issue.text}
    f_end = {'fact_id': 'close', 'value': '117.07', 'unit': 'USD', 'currency': 'USD', 'period': '2025-01-06', 'label': 'closing price', 'source_id': 'price', 'quotation': market.text}
    keys = ('fact_id', 'value', 'unit', 'currency', 'period')
    calc = calculate(CalculationRequest(formula='growth_percent', first={k:f_end[k] for k in keys}, second={k:f_start[k] for k in keys}))
    assert Decimal(calc['value']) == Decimal('17.07')
    result = price_return_context(calc, {'issue': f_start, 'close': f_end}, {'issue': issue, 'price': market})
    assert result == {'entry_type': 'issue_price', 'start_date': '2025-01-02', 'end_date': '2025-01-06', 'start_price': '100', 'end_price': '117.07', 'instrument': 'IPO@NYSE', 'price_basis': 'issue_price → raw', 'identity_verified': False, 'start_price_type': 'issue_price', 'end_price_type': 'close'}
    for final, expected in [('87.15', '-12.85'), ('91.50', '-8.50')]:
        second = {k:f_start[k] for k in keys}
        first = {**{k:f_end[k] for k in keys}, 'value': final}
        assert calculate(CalculationRequest(formula='growth_percent', first=first, second=second))['value'] == expected


def test_close_return_context_uses_recorded_bar_identity_and_rejects_bad_date_or_close():
    market = source('bars', 'ABC@NYSE | 2025-01-03 | 1d | O 100 H 100 L 100 C 100 V 10 USD\nABC@NYSE | 2025-01-06 | 1d | O 90 H 90 L 90 C 90 V 10 USD', 'market_data', {'adjustment': 'raw'})
    lines = market.text.splitlines()
    first = {'fact_id': 'first', 'value': '100', 'unit': 'USD', 'currency': 'USD', 'period': '2025-01-03', 'label': 'close', 'source_id': 'bars', 'quotation': lines[0]}
    last = {'fact_id': 'last', 'value': '90', 'unit': 'USD', 'currency': 'USD', 'period': '2025-01-06', 'label': 'close', 'source_id': 'bars', 'quotation': lines[1]}
    keys = ('fact_id', 'value', 'unit', 'currency', 'period')
    calc = calculate(CalculationRequest(formula='growth_percent', first={k:last[k] for k in keys}, second={k:first[k] for k in keys}))
    context = price_return_context(calc, {'first': first, 'last': last}, {'bars': market})
    assert context['identity_verified'] is True
    assert (context['instrument'], context['entry_type'], context['price_basis']) == ('ABC@NYSE', 'close', 'raw')
    assert price_return_context(calc, {'first': {**first, 'period': '2025-01-02'}, 'last': last}, {'bars': market}) is None
    assert price_return_context(calc, {'first': first, 'last': {**last, 'value': '91'}}, {'bars': market}) is None


def test_offline_ipo_records_preserve_pricing_and_same_day_open_close():
    issue = source('issue', 'at a public offering price of $83.00 per share', 'user_supplied', {})
    bar = source('bar', 'ANT@NASDAQ | 2026-11-19 | 1day | O 111.5 H 115.099998 L 92.72000 C 97.17000 V 857312300 USD', 'market_data', {'adjustment': 'raw'})
    ipo = {'fact_id': 'ipo', 'value': '83', 'unit': 'USD', 'currency': 'USD', 'period': '2026-11-18 定价', 'label': 'IPO 发行价', 'source_id': 'issue', 'quotation': issue.text}
    opening = {'fact_id': 'open', 'value': '111.5', 'unit': 'USD', 'currency': 'USD', 'period': '2026-11-19', 'label': 'ANT 上市首日开盘价', 'source_id': 'bar', 'quotation': bar.text}
    closing = {'fact_id': 'close', 'value': '97.17', 'unit': 'USD', 'currency': 'USD', 'period': '2026-11-19', 'label': 'ANT 上市首日收盘价', 'source_id': 'bar', 'quotation': bar.text}
    keys = ('fact_id', 'value', 'unit', 'currency', 'period')
    facts = {f['fact_id']: f for f in (ipo, opening, closing)}
    sources = {'issue': issue, 'bar': bar}
    def context(start, end):
        raw = {'formula_id': 'growth_percent', 'inputs': [end, start]}
        return price_return_context(raw, facts, sources)
    priced_calc = CalculationRequest(formula='growth_percent', first={k:closing[k] for k in keys}, second={k:ipo[k] for k in keys})
    assert calculate(priced_calc)['value'] == '17.07'
    second_day = {**closing, 'fact_id': 'day2', 'period': '2026-11-20', 'value': '88.91'}
    assert calculate(CalculationRequest(formula='growth_percent', first={k:second_day[k] for k in keys}, second={k:closing[k] for k in keys}))['value'] == '-8.50'
    priced = context(ipo, closing)
    assert (priced['start_date'], priced['end_date'], priced['entry_type'], priced['identity_verified']) == ('2026-11-18', '2026-11-19', 'issue_price', False)
    intraday = context(opening, closing)
    assert (intraday['start_price_type'], intraday['end_price_type'], intraday['identity_verified']) == ('open', 'close', True)
    req = CalculationRequest(formula='growth_percent', first={k:closing[k] for k in keys}, second={k:opening[k] for k in keys})
    assert calculate(req, verified_intraday=True)['value'] == '-12.85'
    from app.research_calculations import CalculationError
    import pytest
    with pytest.raises(CalculationError):
        calculate(req)
    assert context(closing, opening) is None


def test_calculation_tool_accepts_source_verified_same_day_open_close():
    from types import SimpleNamespace
    from app.research_calculations import calculation_input_status
    from app.research_tool_executor import _exec_calculate_financials
    from app.research_tools_registry import CalculateRequest
    bar = source('bar', 'ANT@NASDAQ | 2026-11-19 | 1day | O 111.5 H 115.099998 L 92.72000 C 97.17000 V 857312300 USD', 'market_data', {'adjustment': 'none'})
    facts = [dict(fact_id='open', value='111.5', label='ANT 首日开盘价', unit='USD', currency='USD', period='2026-11-19', source_id='bar', quotation=bar.text), dict(fact_id='close', value='97.17', label='ANT 首日收盘价', unit='USD', currency='USD', period='2026-11-19', source_id='bar', quotation=bar.text)]
    for fact in facts:
        fact.update(character_start=0, character_end=len(bar.text))
    ctx = SimpleNamespace(as_of=None, state={'facts': facts, 'calculations': []}, source_by_id={'bar': bar}, deps=SimpleNamespace(calculate=calculate, calculation_input_status=calculation_input_status))
    req = CalculateRequest.model_validate({'rationale': 'Compare open and close', 'calculations': [{'formula': 'growth_percent', 'first_fact_id': 'close', 'second_fact_id': 'open'}]})
    result = _exec_calculate_financials(ctx, SimpleNamespace(req=req)).result['calculations'][0]
    assert result['value'] == '-12.85'
    assert result['return_context']['start_price_type'] == 'open'
    assert result['return_context']['end_price_type'] == 'close'
    assert ctx.state['calculations'][0]['calculation_id'] == result['calculation_id']
