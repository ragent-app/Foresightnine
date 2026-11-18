"""Fail-closed inspection of a narrow subset of Inline XBRL headers.

The resulting metadata comes from the supplied filing. It is not independent
confirmation of the filing, the issuer, or an accounting interpretation.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import re

_METRICS = {
    'revenue': ({'revenue', 'total revenue', 'net revenue', 'net sales', 'total net sales'},
                {'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'us-gaap:Revenues', 'us-gaap:SalesRevenueNet', 'ifrs-full:Revenue'}),
    'net income': ({'net income', 'net income loss', 'net income attributable to common shareholders'},
                   {'us-gaap:NetIncomeLoss', 'us-gaap:ProfitLoss'}),
    'operating income': ({'operating income', 'operating income loss'}, {'us-gaap:OperatingIncomeLoss'}),
}
_SCALE_WORDS = {3: ('thousand', 'thousands'), 6: ('million', 'millions'),
                9: ('billion', 'billions'), 12: ('trillion', 'trillions')}


def _unit_matches(unit: str, currency: str, scale: int) -> bool:
    code = currency.casefold()
    if scale == 0:
        return unit == code or currency == 'USD' and unit in {'us dollar', 'us dollars', 'dollar', 'dollars'}
    return any(unit in {word, f'{word} {code}', f'{code} {word}', f'{word} of {code}'}
               for word in _SCALE_WORDS.get(scale, ()))


class _HeaderReader(HTMLParser):
    _namespaces = {
        'ix': 'http://www.xbrl.org/2013/inlineXBRL',
        'xbrli': 'http://www.xbrl.org/2003/instance',
        'iso4217': 'http://www.xbrl.org/2003/iso4217',
        'ixt': 'http://www.xbrl.org/inlineXBRL/transformation/2020-02-12',
    }
    _context_paths = {
        ('xbrli:entity',), ('xbrli:entity', 'xbrli:identifier'),
        ('xbrli:period',), ('xbrli:period', 'xbrli:startdate'), ('xbrli:period', 'xbrli:enddate'),
    }

    def __init__(self, contexts: set[str], units: set[str]) -> None:
        super().__init__(convert_charrefs=True)
        self.wanted_contexts = contexts
        self.wanted_units = units
        self.contexts: dict[str, dict | None] = {}
        self.units: dict[str, str | None] = {}
        self.bad = False
        self.root_seen = False
        self.concept_prefixes: set[str] = set()
        self.header_depth = 0
        self.active: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = [key for key, _ in attrs]
        if tag == 'html':
            if self.root_seen or len(names) != len(set(names)):
                self.bad = True
            self.root_seen = True
            mapping = dict(attrs)
            if any(mapping.get('xmlns:' + key) != value for key, value in self._namespaces.items()):
                self.bad = True
            for prefix, pattern in {
                'us-gaap': r'https?://fasb\.org/us-gaap/20\d{2}',
                'ifrs-full': r'https://xbrl\.ifrs\.org/taxonomy/20\d{2}-\d{2}-\d{2}/ifrs-full',
            }.items():
                uri = mapping.get('xmlns:' + prefix)
                if uri is not None:
                    if re.fullmatch(pattern, uri):
                        self.concept_prefixes.add(prefix)
                    else:
                        self.bad = True
            if not self.concept_prefixes:
                self.bad = True
        elif any(name == 'xmlns' or name.startswith('xmlns:') for name in names):
            # A local namespace redefinition can change the meaning of a tag
            # or QName; do not attempt to resolve it heuristically.
            self.bad = True
        if tag == 'ix:header':
            self.header_depth += 1
            if self.header_depth != 1:
                self.bad = True
        if self.active is not None:
            self.active['stack'].append(tag)
            path = tuple(self.active['stack'][1:])
            allowed = self._context_paths if self.active['kind'] == 'context' else {('xbrli:measure',)}
            if path not in allowed or len(names) != len(set(names)):
                self.active['bad'] = True
            if path == ('xbrli:entity', 'xbrli:identifier'):
                if dict(attrs) != {'scheme': 'http://www.sec.gov/CIK'}:
                    self.active['bad'] = True
            elif attrs:
                self.active['bad'] = True
            return
        if tag in {'xbrli:context', 'xbrli:unit'}:
            identifier = dict(attrs).get('id')
            kind = 'context' if tag == 'xbrli:context' else 'unit'
            wanted = self.wanted_contexts if kind == 'context' else self.wanted_units
            if identifier in wanted:
                if not self.header_depth:
                    self.bad = True
                    return
                if len(attrs) != 1 or names != ['id']:
                    self.bad = True
                self.active = {'kind': kind, 'id': identifier, 'stack': [tag], 'values': {}, 'bad': False}

    def handle_data(self, data: str) -> None:
        if self.active is None or not data.strip():
            return
        path = tuple(self.active['stack'][1:])
        leaf = path[-1] if path else ''
        if leaf not in {'xbrli:identifier', 'xbrli:startdate', 'xbrli:enddate', 'xbrli:measure'}:
            self.active['bad'] = True
            return
        self.active['values'].setdefault(path, []).append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.active is not None:
            stack = self.active['stack']
            if not stack or stack[-1] != tag:
                self.active['bad'] = True
                return
            stack.pop()
            if not stack:
                active = self.active; self.active = None
                target = self.contexts if active['kind'] == 'context' else self.units
                if active['id'] in target:
                    self.bad = True
                values = {path: ''.join(parts).strip() for path, parts in active['values'].items()}
                if active['kind'] == 'context':
                    expected = {('xbrli:entity', 'xbrli:identifier'), ('xbrli:period', 'xbrli:startdate'),
                                ('xbrli:period', 'xbrli:enddate')}
                    target[active['id']] = values if not active['bad'] and set(values) == expected else None
                else:
                    expected = {('xbrli:measure',)}
                    target[active['id']] = values.get(('xbrli:measure',)) if not active['bad'] and set(values) == expected else None
        if tag == 'ix:header' and self.header_depth:
            self.header_depth -= 1


def resolve_inline_annual_facts(html: str, rows: list[dict]) -> list[dict]:
    """Resolve structurally simple numeric facts in the target table row."""
    if not rows or not isinstance(rows[-1].get('cells'), list):
        return []
    raw_facts = [(cell, fact) for cell in rows[-1]['cells'] for fact in cell.get('inline_facts', [])]
    if not raw_facts or len(raw_facts) > 20:
        return []
    contexts = {fact.get('contextref') for _, fact in raw_facts}
    units = {fact.get('unitref') for _, fact in raw_facts}
    if (any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,99}', value) for value in contexts | units)):
        return []
    parser = _HeaderReader(contexts, units)
    try:
        parser.feed(html); parser.close()
    except (ValueError, RecursionError):
        return []
    if parser.bad or not parser.root_seen or parser.header_depth or parser.active is not None:
        return []
    result = []
    for cell, item in raw_facts:
        if (len(cell.get('inline_facts', [])) != 1 or item.get('text') != cell.get('text') or
                not re.fullmatch(r'(?:us-gaap|ifrs-full):[A-Za-z][A-Za-z0-9]{0,120}', item.get('name', '')) or
                item['name'].split(':', 1)[0] not in parser.concept_prefixes or
                item.get('format') != 'ixt:num-dot-decimal' or item.get('sign') not in (None, '+') or
                not re.fullmatch(r'\d{1,2}', item.get('scale', '')) or not re.fullmatch(r'-?\d{1,2}', item.get('decimals', '')) or
                not re.fullmatch(r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?', item.get('text', ''))):
            continue
        if int(item['scale']) > 12:
            continue
        context = parser.contexts.get(item['contextref'])
        measure = parser.units.get(item['unitref'])
        if not isinstance(context, dict) or not isinstance(measure, str) or not re.fullmatch(r'iso4217:[A-Z]{3}', measure):
            continue
        identifier = context.get(('xbrli:entity', 'xbrli:identifier'), '')
        try:
            start = date.fromisoformat(context[('xbrli:period', 'xbrli:startdate')])
            end = date.fromisoformat(context[('xbrli:period', 'xbrli:enddate')])
            amount = Decimal(item['text'].replace(',', ''))
        except (ValueError, KeyError, InvalidOperation):
            continue
        if not re.fullmatch(r'\d{10}', identifier) or not 300 <= (end - start).days <= 380:
            continue
        result.append({'cell_start': cell['start'], 'cell_end': cell['end'], 'value': str(amount),
                       'concept': item['name'], 'currency': measure.split(':', 1)[1],
                       'scale': int(item['scale']), 'decimals': int(item['decimals']),
                       'entity_cik': identifier, 'period_start': start.isoformat(), 'period_end': end.isoformat(),
                       'context_ref': item['contextref'], 'unit_ref': item['unitref']})
    return result


def match_inline_fact(trace: dict, alignment: dict | None, *, value: Decimal, label: str,
                      unit: str, currency: str | None) -> dict | None:
    """Cross-check a model fact against a narrowly recognized inline fact.

    This confirms agreement with tags and table text in this source only. It
    does not authenticate the filing, establish accounting validity, or verify
    the source independently.
    """
    if (trace.get('status') != 'available' or not alignment or alignment.get('status') != 'matched' or
            not isinstance(currency, str) or not re.fullmatch(r'[A-Z]{3}', currency) or
            not isinstance(label, str) or not isinstance(unit, str)):
        return None
    rows = trace.get('rows')
    if not isinstance(rows, list) or not rows or not isinstance(rows[-1].get('cells'), list):
        return None
    def clean(text: str) -> str:
        text = text.replace('_', ' ')
        text = re.sub(r'\bfy\s*(?:19|20)\d{2}\b', ' ', text, flags=re.IGNORECASE)
        return ' '.join(re.sub(r'[^a-z0-9]+', ' ', text.casefold()).split())
    row_labels = [clean(cell.get('text', '')) for cell in rows[-1]['cells'] if cell.get('text')]
    if not row_labels:
        return None
    normalized_label, row_label, normalized_unit = clean(label), row_labels[0], clean(unit)
    matches = []
    for item in trace.get('inline_xbrl_annual_facts', []):
        if not isinstance(item, dict):
            continue
        metric = next((name for name, (labels, concepts) in _METRICS.items()
                       if normalized_label in labels and row_label in labels and item.get('concept') in concepts), None)
        known_label = any(normalized_label in labels or row_label in labels for labels, _ in _METRICS.values())
        if (metric is None and not known_label and normalized_label == row_label and
                len(row_label) >= 3 and re.search(r'[a-z]', row_label) and
                row_label not in {'total', 'amount', 'value', 'net'}):
            # This records the exact row and its tag; it does not infer that
            # the taxonomy concept's accounting meaning matches the row.
            metric = row_label
        try:
            same_value = Decimal(str(item.get('value'))) == value
        except InvalidOperation:
            continue
        scale = item.get('scale')
        if (metric and same_value and type(scale) is int and _unit_matches(normalized_unit, currency, scale) and
                item.get('decimals') == -scale and item.get('currency') == currency and
                item.get('period_end') == alignment.get('column_date') and
                item.get('cell_start') == alignment.get('value_column_start') and
                item.get('cell_end') == alignment.get('value_column_end')):
            matches.append({**item, 'metric': metric, 'source_sha256': trace.get('source_sha256')})
    return matches[0] if len(matches) == 1 else None
