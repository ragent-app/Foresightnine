"""Evidence-derived labels for price returns; unknown identity stays explicit."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Mapping

from .research_sources import ResearchSource

_BAR = re.compile(r'^([^|]+)\s+\|\s+(\d{4}-\d{2}-\d{2})(?:T[^| ]+)?\s+\|\s+[^|]+\s+\|\s+O\s+([0-9.]+)\s+H\s+[0-9.]+\s+L\s+[0-9.]+\s+C\s+([0-9.]+)(?:\s|$)')
_OPEN = re.compile(r'\bopen(?:ing)?\b|开盘', re.I)
_CLOSE = re.compile(r'\bclos(?:e|ing)\b|收盘', re.I)
_ISSUE = re.compile(r'\b(?:issue|offering|ipo)\s+price\b|发行价|发行价格|发售价', re.I)


def price_return_context(calculation: Mapping[str, Any], facts: Mapping[str, Mapping[str, Any]], sources: Mapping[str, ResearchSource]) -> dict[str, Any] | None:
    if calculation.get('formula_id') != 'growth_percent':
        return None
    inputs = calculation.get('inputs') or []
    if len(inputs) != 2:
        return None
    endpoints = []
    for inp in inputs:
        fact = facts.get(inp.get('fact_id'))
        source = sources.get(fact.get('source_id')) if fact else None
        if not fact or not source:
            return None
        quote = str(fact.get('quotation') or '').strip()
        match = _BAR.match(quote) if source.kind == 'market_data' else None
        try:
            period = str(fact.get('period') or '')
            dated = re.fullmatch(r'(\d{4}-\d{2}-\d{2})(?:\s+[^0-9\n]+)?', period)
            if not dated:
                return None
            day = date.fromisoformat(dated.group(1)).isoformat()
            value = Decimal(str(fact['value']))
        except (ValueError, InvalidOperation, KeyError):
            return None
        provenance = source.provenance if isinstance(source.provenance, dict) else {}
        label = str(fact.get('label') or '')
        is_open, is_close = bool(_OPEN.search(label)), bool(_CLOSE.search(label))
        if match and match.group(2) == day and is_open != is_close and Decimal(match.group(3 if is_open else 4)) == value:
            instrument = match.group(1).strip().upper()
            basis = str(provenance.get('adjustment') or 'unknown')
            endpoints.append((day, str(value), instrument, basis, 'open' if is_open else 'close'))
        elif _ISSUE.search(str(fact.get('label') or '') + ' ' + quote):
            symbol = provenance.get('symbol')
            exchange = provenance.get('exchange')
            instrument = f'{symbol}@{exchange}'.upper() if symbol and exchange else None
            endpoints.append((day, str(value), instrument, 'issue_price', 'issue_price'))
        else:
            return None
    end, start = endpoints
    if end[0] < start[0] or (end[0] == start[0] and (start[4], end[4]) != ('open', 'close')):
        return None
    verified = bool(start[2] and end[2] and start[2] == end[2])
    return {'entry_type': start[4], 'start_date': start[0], 'end_date': end[0],
            'start_price': start[1], 'end_price': end[1], 'instrument': end[2],
            'price_basis': end[3] if start[3] == end[3] else f'{start[3]} → {end[3]}',
            'identity_verified': verified, 'start_price_type': start[4], 'end_price_type': end[4]}
