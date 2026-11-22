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
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from pydantic import ValidationError

from .research_calculations import NumericFact
from .research_sources import ResearchSource
from .research_statement_extraction import ExtractStatementsRequest, extract_statements


MAX_OBSERVATIONS = 100
# A unit outside this known set is retained as a unit without assuming it is
# a currency. More currencies can be added via a validated provider contract.
_CURRENCIES = frozenset('USD EUR GBP JPY CNY HKD KRW CAD AUD CHF SGD INR TWD NZD SEK NOK DKK BRL ZAR'.split())


_PDF_TO_STANDARD_LINE: dict[str, str] = {
    'operating_profit': 'operating_income',
    'net_profit': 'net_income',
    'net_profit_attributable_parent': 'net_income',
    'cash_and_equivalents': 'cash',
    'inventories': 'inventory',
    'total_current_assets': 'current_assets',
    'total_current_liabilities': 'current_liabilities',
    'short_term_borrowings': 'short_term_debt',
    'long_term_borrowings': 'long_term_debt',
    'total_equity': 'equity',
    'equity_attributable_parent': 'equity',
}
_STMT_LINE_RE = re.compile(r'^(income|balance|cashflow)\.([A-Za-z0-9_]+)$')


def _currency_from_unit(unit: str) -> str | None:
    if unit in _CURRENCIES:
        return unit
    if '/' in unit:
        head = unit.split('/', 1)[0]
        if head in _CURRENCIES:
            return head
    return None


def _parse_sec_json_row(
    source: ResearchSource,
    raw: str,
    start: int,
    offset: int,
    *,
    entity_id: str,
    taxonomy: str,
    concept: str,
) -> dict | None:
    try:
        item = json.loads(raw, parse_float=Decimal)
        if not isinstance(item, dict):
            return None
        value, unit = item.get('val'), item.get('unit')
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
            return None
        if not isinstance(unit, str) or not unit or len(unit) > 100:
            return None
        end = date.fromisoformat(item['end'])
        start_date = date.fromisoformat(item['start']) if isinstance(item.get('start'), str) else None
        if start_date is not None and start_date > end:
            return None
        filed = date.fromisoformat(item['filed']) if isinstance(item.get('filed'), str) else None
        period = f'{start_date.isoformat()} to {end.isoformat()}' if start_date else end.isoformat()
        fact_id = 'structured_' + sha256(f'{source.source_id}:{start}:{offset}'.encode()).hexdigest()[:32]
        currency = _currency_from_unit(unit)
        numeric = NumericFact(fact_id=fact_id, value=value, unit=unit, currency=currency, period=period)
    except (ValueError, TypeError, KeyError, InvalidOperation, ValidationError, json.JSONDecodeError):
        return None
    metadata = {}
    for key in ('fy', 'fp', 'form', 'accn', 'frame'):
        val = item.get(key)
        if isinstance(val, (str, int)) and not isinstance(val, bool) and len(str(val)) <= 100:
            metadata[key] = val
    return {
        **numeric.model_dump(mode='json'),
        'label': source.title[:300],
        'source_id': source.source_id,
        'quotation': raw,
        'quotation_match': 'exact',
        'location': {'character_start': start, 'character_end': start + len(raw)},
        'verification': 'structured_source_row_not_independently_verified',
        'extraction_method': 'sec_company_concept_row_v1',
        'structured_metadata': {
            'entity_id': entity_id,
            'metric_id': f'{taxonomy}:{concept}',
            'period_start': start_date.isoformat() if start_date else None,
            'period_end': end.isoformat(),
            'filing_date': filed.isoformat() if filed else None,
            **metadata,
        },
    }


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
        parsed = _parse_sec_json_row(
            source, raw, start, offset,
            entity_id='SEC_CIK:' + entity_id, taxonomy=taxonomy, concept=concept,
        )
        if parsed is None:
            continue
        if len(rows) >= MAX_OBSERVATIONS:
            return rows, True
        rows.append(parsed)
    return rows, False


def _fin_data_statement_observations(source: ResearchSource) -> tuple[list[dict], bool]:
    """Read structured statement rows persisted by fin_data ``company_financials`` (SEC / Tushare / BaoStock)."""
    prov = source.provenance if isinstance(getattr(source, 'provenance', None), Mapping) else {}
    prov_entity = prov.get('entity_id') if isinstance(prov.get('entity_id'), str) and prov.get('entity_id').strip() else None
    if not prov_entity and isinstance(prov.get('cik'), str) and prov['cik'].isdigit():
        prov_entity = 'cik:' + prov['cik'].zfill(10)
    provider = prov.get('provider') if isinstance(prov.get('provider'), str) else None
    extraction_method = (
        'sec_companyfacts_row_v1'
        if getattr(source, 'retrieval_method', None) == 'sec_companyfacts'
        else 'structured_financials_row_v1'
    )
    rows: list[dict] = []
    offset = 0
    for line in source.text.splitlines(keepends=True):
        raw = line.rstrip('\r\n')
        start = offset
        offset += len(line)
        if not raw or len(raw) > 2000:
            continue
        if raw.startswith('{') and getattr(source, 'retrieval_method', None) == 'sec_companyfacts':
            tax = str(prov.get('taxonomy') or 'us-gaap')
            concept = str(prov.get('concept') or 'Unknown')
            parsed_json = _parse_sec_json_row(
                source, raw, start, offset,
                entity_id=prov_entity or 'SEC_CIK:unknown', taxonomy=tax, concept=concept,
            )
            if parsed_json is not None:
                if len(rows) >= MAX_OBSERVATIONS:
                    return rows, True
                rows.append(parsed_json)
            continue
        parts = raw.split(' | ')
        if len(parts) < 7:
            continue
        entity_label = parts[0].strip()
        stmt_match = _STMT_LINE_RE.fullmatch(parts[1].strip())
        metric_id = parts[2].strip()
        start_s, sep, end_s = parts[3].strip().partition('..')
        if not entity_label or stmt_match is None or ':' not in metric_id or sep != '..':
            continue
        statement, std_line = stmt_match.groups()
        try:
            end_date = date.fromisoformat(end_s)
            start_date = date.fromisoformat(start_s) if start_s else None
            if start_date is not None and start_date > end_date:
                continue
        except ValueError:
            continue
        fiscal_label: str | None = None
        form: str | None = None
        filed_date: date | None = None
        accn: str | None = None
        basis: str | None = None
        value: Decimal | None = None
        unit: str | None = None
        derived = False
        derivation: str | None = None
        for seg in parts[4:]:
            s = seg.strip()
            if s.startswith('fiscal '):
                val = s[7:].strip()
                fiscal_label = None if val in ('', '-') else val
            elif s.startswith('form ') and ' filed ' in s:
                form_s, filed_s = s[5:].rsplit(' filed ', 1)
                form = None if form_s.strip() in ('', '-') else form_s.strip()
                filed_clean = filed_s.strip()
                if filed_clean not in ('', '-'):
                    try:
                        filed_date = date.fromisoformat(filed_clean)
                    except ValueError:
                        filed_date = None
            elif s.startswith('accn '):
                val = s[5:].strip()
                accn = None if val in ('', '-') else val
            elif s.startswith('basis '):
                val = s[6:].strip()
                basis = None if val in ('', '-') else val
            elif s.startswith('value '):
                val_s, v_sep, unit_s = s[6:].strip().partition(' ')
                if v_sep != ' ' or not unit_s.strip():
                    value = None
                    break
                try:
                    value = Decimal(val_s)
                except InvalidOperation:
                    value = None
                    break
                unit = unit_s.strip()
            elif s.startswith('derived: '):
                derived = True
                derivation = s[9:].strip()
        if value is None or unit is None or not value.is_finite() or len(unit) > 100:
            continue
        period = f'{start_date.isoformat()} to {end_date.isoformat()}' if start_date else end_date.isoformat()
        fact_id = 'structured_' + sha256(f'{source.source_id}:{start}:{offset}'.encode()).hexdigest()[:32]
        currency = _currency_from_unit(unit)
        try:
            numeric = NumericFact(fact_id=fact_id, value=value, unit=unit, currency=currency, period=period)
        except ValidationError:
            continue
        if len(rows) >= MAX_OBSERVATIONS:
            return rows, True
        taxonomy_name = metric_id.split(':', 1)[0]
        metadata: dict[str, Any] = {
            'entity_id': prov_entity or entity_label,
            'metric_id': metric_id,
            'standard_line': std_line,
            'statement': statement,
            'taxonomy': taxonomy_name,
            'period_start': start_date.isoformat() if start_date else None,
            'period_end': end_date.isoformat(),
            'filing_date': filed_date.isoformat() if filed_date else None,
        }
        if provider:
            metadata['provider'] = provider
        if fiscal_label:
            metadata['fiscal_label'] = fiscal_label
        if form:
            metadata['form'] = form
        if accn:
            metadata['accn'] = accn
        if basis:
            metadata['basis'] = basis
        if derived:
            metadata['derived'] = True
            if derivation:
                metadata['derivation'] = derivation
        rows.append({
            **numeric.model_dump(mode='json'),
            'label': f'{entity_label} {std_line.replace("_", " ")}'[:300],
            'source_id': source.source_id,
            'quotation': raw,
            'quotation_match': 'exact',
            'location': {'character_start': start, 'character_end': start + len(raw)},
            'verification': 'structured_source_row_not_independently_verified',
            'extraction_method': extraction_method,
            'structured_metadata': metadata,
        })
    return rows, False


def _pdf_statement_observations(source: ResearchSource) -> tuple[list[dict], bool]:
    """Extract uniform structured observations from PDF report statements via ``extract_statements``."""
    prov = source.provenance if isinstance(getattr(source, 'provenance', None), Mapping) else {}
    entity_id = prov.get('entity_id') if isinstance(prov.get('entity_id'), str) and prov.get('entity_id').strip() else None
    pre: Mapping[str, Any] | None = None
    verify_offsets = True
    if isinstance(prov.get('extract_statements'), Mapping) and isinstance(prov['extract_statements'].get('statements'), list):
        pre = prov['extract_statements']
    elif isinstance(prov.get('statements'), list):
        pre = prov
    elif getattr(source, 'retrieval_method', None) == 'extract_statements' and source.text.lstrip().startswith('{'):
        try:
            loaded = json.loads(source.text)
            if isinstance(loaded, dict) and isinstance(loaded.get('statements'), list):
                pre = loaded
                verify_offsets = False
        except ValueError:
            pre = None

    if pre is not None:
        statements = pre.get('statements') or []
    else:
        doc_pages = prov.get('document_pages')
        if not (isinstance(doc_pages, Mapping) and isinstance(doc_pages.get('pages'), list)):
            doc_pages = None
        payload, _summary = extract_statements(
            ExtractStatementsRequest(source_id=source.source_id, rationale='structured_observation_extraction'),
            source,
            document_pages=doc_pages,
            read_ranges=[],
            read_budget_chars=max(len(source.text) + 1024, 200_000),
            result_budget_chars=100_000,
        )
        statements = payload.get('statements') or []

    rows: list[dict] = []
    for st in statements:
        if not isinstance(st, Mapping):
            continue
        raw_stmt = str(st.get('statement') or '')
        statement = 'cashflow' if raw_stmt == 'cash_flow' else raw_stmt
        unit_info = st.get('unit') if isinstance(st.get('unit'), Mapping) else {}
        raw_cur = unit_info.get('currency')
        currency = raw_cur if isinstance(raw_cur, str) and raw_cur in _CURRENCIES else None
        columns = st.get('columns') if isinstance(st.get('columns'), list) else []
        for row in st.get('rows') or []:
            if not isinstance(row, Mapping):
                continue
            raw_line = row.get('standard_line')
            if not isinstance(raw_line, str) or not raw_line or row.get('primary') is False:
                continue
            quotation = row.get('quotation')
            c_start, c_end = row.get('character_start'), row.get('character_end')
            if not isinstance(quotation, str) or not quotation:
                continue
            if verify_offsets:
                if not (type(c_start) is int and type(c_end) is int and
                        0 <= c_start < c_end <= len(source.text) and
                        source.text[c_start:c_end] == quotation):
                    continue
            if row.get('per_share'):
                unit = f'{currency}/share' if currency else 'per_share'
            elif currency:
                unit = currency
            else:
                unit = str(unit_info.get('stated_as') or unit_info.get('scale_label') or 'currency units')[:100]
            std_line = _PDF_TO_STANDARD_LINE.get(raw_line, raw_line)
            values = row.get('value') if isinstance(row.get('value'), list) else []
            for col_idx, col in enumerate(columns):
                if col_idx >= len(values) or values[col_idx] is None or not isinstance(col, Mapping):
                    continue
                end_s = col.get('period_end')
                if not isinstance(end_s, str):
                    continue
                try:
                    end_date = date.fromisoformat(end_s)
                    start_s = col.get('period_start')
                    start_date = date.fromisoformat(start_s) if isinstance(start_s, str) else None
                    if start_date is not None and start_date > end_date:
                        continue
                    value = Decimal(str(values[col_idx]))
                except (ValueError, InvalidOperation):
                    continue
                period = f'{start_date.isoformat()} to {end_date.isoformat()}' if start_date else end_date.isoformat()
                loc_key = f'{c_start}:{c_end}' if (type(c_start) is int and type(c_end) is int) else sha256(quotation.encode()).hexdigest()[:16]
                fact_id = 'structured_' + sha256(f'{source.source_id}:{loc_key}:{col_idx}'.encode()).hexdigest()[:32]
                try:
                    numeric = NumericFact(fact_id=fact_id, value=value, unit=unit, currency=currency, period=period)
                except ValidationError:
                    continue
                if len(rows) >= MAX_OBSERVATIONS:
                    return rows, True
                row_label = str(row.get('label') or raw_line.replace('_', ' '))[:300]
                metadata: dict[str, Any] = {
                    'entity_id': entity_id,
                    'metric_id': f'statement_extraction:{raw_line}',
                    'standard_line': std_line,
                    'statement_line': raw_line,
                    'statement': statement,
                    'period_start': start_date.isoformat() if start_date else None,
                    'period_end': end_date.isoformat(),
                    'filing_date': None,
                }
                if st.get('scope'):
                    metadata['scope'] = st['scope']
                if row.get('page') is not None:
                    metadata['page'] = row['page']
                if col.get('fiscal_label'):
                    metadata['fiscal_label'] = col['fiscal_label']
                if col.get('basis'):
                    metadata['period_basis'] = col['basis']
                if col.get('cumulative_ytd'):
                    metadata['cumulative_ytd'] = True
                if unit_info.get('basis'):
                    metadata['unit_basis'] = unit_info['basis']
                if row.get('extraction_method'):
                    metadata['statement_extraction_method'] = row['extraction_method']
                if row.get('ocr_needs_check'):
                    metadata['ocr_needs_check'] = True
                rows.append({
                    **numeric.model_dump(mode='json'),
                    'label': row_label,
                    'source_id': source.source_id,
                    'quotation': quotation,
                    'quotation_match': 'exact',
                    'location': ({'character_start': c_start, 'character_end': c_end}
                                 if type(c_start) is int and type(c_end) is int else {}),
                    'verification': 'structured_source_row_not_independently_verified',
                    'extraction_method': 'extract_statements_pdf_v1',
                    'structured_metadata': metadata,
                })
    return rows, False


_ADAPTERS: dict[str, Callable[[ResearchSource], tuple[list[dict], bool]]] = {
    'sec_companyconcept': _sec_company_concept,
    'sec_companyfacts': _fin_data_statement_observations,
    'structured_financials': _fin_data_statement_observations,
    'user_pdf': _pdf_statement_observations,
    'web_pdf': _pdf_statement_observations,
    'extract_statements': _pdf_statement_observations,
}


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
