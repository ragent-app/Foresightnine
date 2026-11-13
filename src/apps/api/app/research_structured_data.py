"""Versioned adapters from authorized structured sources to research facts.

Only explicitly supported source formats are parsed. Extracted values and
metadata reflect a source snapshot, not source authenticity, historical
availability, accounting interpretation or independent verification.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from typing import Callable
from urllib.parse import urlsplit

from pydantic import ValidationError

from .research_calculations import NumericFact
from .research_sources import ResearchSource


MAX_OBSERVATIONS = 100
# A unit outside this known set is retained as a unit without assuming it is
# a currency. More currencies can be added via a validated provider contract.
_CURRENCIES = frozenset('USD EUR GBP JPY CNY HKD KRW CAD AUD CHF SGD INR TWD NZD SEK NOK DKK BRL ZAR'.split())


def _sec_company_concept(source: ResearchSource) -> tuple[list[dict], bool]:
    """Read exact JSON observation lines from the SEC connector's snapshot."""
    # The source URL is produced by the SEC adapter; retain the explicitly
    # requested entity and concept rather than inferring identity from labels.
    address = urlsplit(str(source.url)) if source.url else None
    match = re.fullmatch(r'/api/xbrl/companyconcept/CIK(\d{10})/(us-gaap|ifrs-full|dei)/([A-Za-z][A-Za-z0-9_]*)\.json',
                         address.path) if address and address.hostname == 'data.sec.gov' else None
    if match is None:
        return [], False
    entity_id, taxonomy, concept = match.groups()
    rows: list[dict] = []
    offset = 0
    for line in source.text.splitlines(keepends=True):
        raw = line.rstrip('\r\n')
        start = offset
        offset += len(line)
        if not raw.startswith('{') or len(raw) > 1000:
            continue
        try:
            item = json.loads(raw, parse_float=Decimal)
            if not isinstance(item, dict):
                continue
            value, unit = item.get('val'), item.get('unit')
            if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
                continue
            if not isinstance(unit, str) or not unit or len(unit) > 100:
                continue
            end = date.fromisoformat(item['end'])
            start_date = date.fromisoformat(item['start']) if isinstance(item.get('start'), str) else None
            if start_date is not None and start_date > end:
                continue
            filed = date.fromisoformat(item['filed']) if isinstance(item.get('filed'), str) else None
            period = f'{start_date.isoformat()} to {end.isoformat()}' if start_date else end.isoformat()
            fact_id = 'structured_' + sha256(f'{source.source_id}:{start}:{offset}'.encode()).hexdigest()[:32]
            currency = unit if unit in _CURRENCIES else None
            numeric = NumericFact(fact_id=fact_id, value=value, unit=unit, currency=currency, period=period)
        except (ValueError, TypeError, KeyError, InvalidOperation, ValidationError, json.JSONDecodeError):
            continue
        if len(rows) >= MAX_OBSERVATIONS:
            return rows, True
        metadata = {}
        for key in ('fy', 'fp', 'form', 'accn', 'frame'):
            value = item.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and len(str(value)) <= 100:
                metadata[key] = value
        rows.append({**numeric.model_dump(mode='json'), 'label': source.title[:300], 'source_id': source.source_id,
                     'quotation': raw, 'quotation_match': 'exact',
                     'location': {'character_start': start, 'character_end': start + len(raw)},
                     'verification': 'structured_source_row_not_independently_verified',
                     'extraction_method': 'sec_company_concept_row_v1',
                     'structured_metadata': {'entity_id': 'SEC_CIK:' + entity_id, 'metric_id': taxonomy + ':' + concept,
                                             'period_start': start_date.isoformat() if start_date else None,
                                             'period_end': end.isoformat(),
                                             'filing_date': filed.isoformat() if filed else None, **metadata}})
    return rows, False


_ADAPTERS: dict[str, Callable[[ResearchSource], tuple[list[dict], bool]]] = {'sec_companyconcept': _sec_company_concept}


def extract_structured_observations(sources: list[ResearchSource], *, max_facts: int = 200) -> tuple[list[dict], bool]:
    """Extract bounded observations; explicitly report a capacity cutoff."""
    groups: list[list[dict]] = []
    truncated = False
    for source in sources:
        adapter = _ADAPTERS.get(source.retrieval_method)
        if adapter is None or source.is_simulated or not source.permissions.model_input or not source.permissions.display:
            continue
        rows, source_truncated = adapter(source)
        truncated = truncated or source_truncated
        if rows:
            groups.append(rows)
    # Interleave provider-ordered observations so a long first source cannot
    # consume the entire run budget before other selected sources are seen.
    # This is allocation fairness, not a claim that the rows are comparable.
    limit = max(0, max_facts)
    truncated = truncated or sum(map(len, groups)) > limit
    output: list[dict] = []
    for index in range(max((len(rows) for rows in groups), default=0)):
        for rows in groups:
            if len(output) >= limit:
                return output, truncated
            if index < len(rows):
                output.append(rows[index])
    return output, truncated
