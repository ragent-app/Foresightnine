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
    'revenue': (
        {'revenue', 'revenues', 'total revenue', 'total revenues', 'net revenue', 'net revenues',
         'net sales', 'total net sales', 'sales', 'turnover'},
        {'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', 'us-gaap:Revenues',
         'us-gaap:SalesRevenueNet', 'us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax',
         'ifrs-full:Revenue'},
    ),
    'cost of revenue': (
        {'cost of revenue', 'cost of revenues', 'cost of sales', 'cost of goods sold',
         'cost of services', 'cost of goods and services'},
        {'us-gaap:CostOfGoodsAndServicesSold', 'us-gaap:CostOfRevenue', 'us-gaap:CostOfGoodsSold',
         'us-gaap:CostOfServices', 'ifrs-full:CostOfSales'},
    ),
    'gross profit': (
        {'gross profit', 'gross margin'},
        {'us-gaap:GrossProfit', 'ifrs-full:GrossProfit'},
    ),
    'rd expense': (
        {'research and development', 'research and development expense', 'research and development expenses',
         'r d', 'r d expense', 'rd expense'},
        {'us-gaap:ResearchAndDevelopmentExpense',
         'us-gaap:ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost',
         'ifrs-full:ResearchAndDevelopmentExpense'},
    ),
    'sga expense': (
        {'selling general and administrative', 'selling general and administrative expense',
         'selling general and administrative expenses', 'sga', 'sga expense', 'sg a'},
        {'us-gaap:SellingGeneralAndAdministrativeExpense', 'ifrs-full:SellingGeneralAndAdministrativeExpense'},
    ),
    'selling expense': (
        {'selling expense', 'selling expenses', 'selling and marketing', 'selling and marketing expense',
         'selling and distribution expenses', 'distribution costs'},
        {'us-gaap:SellingAndMarketingExpense', 'us-gaap:SellingExpense',
         'ifrs-full:SellingExpense', 'ifrs-full:DistributionCosts'},
    ),
    'admin expense': (
        {'general and administrative', 'general and administrative expense',
         'general and administrative expenses', 'administrative expenses', 'admin expense'},
        {'us-gaap:GeneralAndAdministrativeExpense', 'ifrs-full:AdministrativeExpense'},
    ),
    'total operating costs': (
        {'operating expenses', 'total operating expenses', 'total operating costs',
         'costs and expenses', 'total costs and expenses'},
        {'us-gaap:OperatingExpenses', 'us-gaap:CostsAndExpenses', 'us-gaap:OperatingCostsAndExpenses',
         'ifrs-full:OperatingExpenses'},
    ),
    'operating income': (
        {'operating income', 'operating income loss', 'operating profit',
         'income from operations', 'profit from operations', 'profit from operating activities'},
        {'us-gaap:OperatingIncomeLoss', 'ifrs-full:ProfitLossFromOperatingActivities'},
    ),
    'interest expense': (
        {'interest expense', 'interest expense debt', 'interest and debt expense'},
        {'us-gaap:InterestExpense', 'us-gaap:InterestExpenseDebt', 'us-gaap:InterestAndDebtExpense',
         'ifrs-full:FinanceCosts', 'ifrs-full:InterestExpense'},
    ),
    'finance expense': (
        {'finance costs', 'finance expense', 'interest income expense net'},
        {'us-gaap:InterestIncomeExpenseNet', 'us-gaap:InterestIncomeExpenseNonoperatingNet',
         'ifrs-full:FinanceCosts'},
    ),
    'profit before tax': (
        {'income before income taxes', 'income before taxes', 'profit before tax',
         'profit before income tax', 'earnings before taxes', 'pretax income'},
        {'us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest',
         'us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments',
         'ifrs-full:ProfitLossBeforeTax'},
    ),
    'income tax': (
        {'income tax', 'income tax expense', 'income taxes', 'provision for income taxes', 'tax expense'},
        {'us-gaap:IncomeTaxExpenseBenefit', 'ifrs-full:IncomeTaxExpenseContinuingOperations'},
    ),
    'net income': (
        {'net income', 'net income loss', 'net income attributable to common shareholders',
         'net profit', 'net earnings', 'net loss', 'profit for the year', 'profit for the period'},
        {'us-gaap:NetIncomeLoss', 'us-gaap:ProfitLoss', 'ifrs-full:ProfitLoss'},
    ),
    'ebitda': (
        {'ebitda'},
        {'us-gaap:EarningsBeforeInterestTaxesDepreciationAndAmortization'},
    ),
    'eps basic': (
        {'basic earnings per share', 'basic eps', 'earnings per share basic', 'eps basic'},
        {'us-gaap:EarningsPerShareBasic', 'ifrs-full:BasicEarningsLossPerShare'},
    ),
    'eps diluted': (
        {'diluted earnings per share', 'diluted eps', 'earnings per share diluted', 'eps diluted'},
        {'us-gaap:EarningsPerShareDiluted', 'ifrs-full:DilutedEarningsLossPerShare'},
    ),
    'cash': (
        {'cash', 'cash and cash equivalents', 'cash and equivalents', 'cash and bank balances'},
        {'us-gaap:CashAndCashEquivalentsAtCarryingValue',
         'us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents',
         'ifrs-full:CashAndCashEquivalents'},
    ),
    'short term investments': (
        {'short term investments', 'marketable securities'},
        {'us-gaap:ShortTermInvestments', 'us-gaap:MarketableSecuritiesCurrent',
         'us-gaap:AvailableForSaleSecuritiesDebtSecuritiesCurrent', 'ifrs-full:OtherCurrentFinancialAssets'},
    ),
    'accounts receivable': (
        {'accounts receivable', 'accounts receivable net', 'trade receivables', 'trade and other receivables'},
        {'us-gaap:AccountsReceivableNetCurrent', 'us-gaap:AccountsNotesAndLoansReceivableNetCurrent',
         'us-gaap:ReceivablesNetCurrent', 'ifrs-full:TradeAndOtherCurrentReceivables'},
    ),
    'inventory': (
        {'inventory', 'inventories'},
        {'us-gaap:InventoryNet', 'us-gaap:InventoryFinishedGoodsNetOfReserves', 'ifrs-full:Inventories'},
    ),
    'current assets': (
        {'current assets', 'total current assets'},
        {'us-gaap:AssetsCurrent', 'ifrs-full:CurrentAssets'},
    ),
    'ppe net': (
        {'property plant and equipment', 'property plant and equipment net', 'ppe net'},
        {'us-gaap:PropertyPlantAndEquipmentNet', 'ifrs-full:PropertyPlantAndEquipment'},
    ),
    'goodwill': (
        {'goodwill'},
        {'us-gaap:Goodwill', 'ifrs-full:Goodwill'},
    ),
    'intangible assets': (
        {'intangible assets', 'intangible assets net'},
        {'us-gaap:IntangibleAssetsNetExcludingGoodwill', 'us-gaap:FiniteLivedIntangibleAssetsNet',
         'ifrs-full:IntangibleAssetsOtherThanGoodwill'},
    ),
    'total assets': (
        {'total assets', 'assets'},
        {'us-gaap:Assets', 'ifrs-full:Assets'},
    ),
    'accounts payable': (
        {'accounts payable', 'trade payables', 'trade and other payables'},
        {'us-gaap:AccountsPayableCurrent', 'us-gaap:AccountsPayableAndAccruedLiabilitiesCurrent',
         'ifrs-full:TradeAndOtherCurrentPayables'},
    ),
    'short term debt': (
        {'short term debt', 'short term borrowings', 'current debt', 'current portion of long term debt'},
        {'us-gaap:ShortTermBorrowings', 'us-gaap:DebtCurrent', 'us-gaap:LongTermDebtCurrent',
         'us-gaap:NotesPayableCurrent', 'ifrs-full:ShorttermBorrowings',
         'ifrs-full:CurrentPortionOfLongtermBorrowings'},
    ),
    'current liabilities': (
        {'current liabilities', 'total current liabilities'},
        {'us-gaap:LiabilitiesCurrent', 'ifrs-full:CurrentLiabilities'},
    ),
    'long term debt': (
        {'long term debt', 'long term borrowings', 'noncurrent debt'},
        {'us-gaap:LongTermDebtNoncurrent', 'us-gaap:LongTermDebt', 'us-gaap:LongTermNotesAndLoans',
         'ifrs-full:LongtermBorrowings', 'ifrs-full:NoncurrentPortionOfNoncurrentLoansReceived'},
    ),
    'total liabilities': (
        {'total liabilities', 'liabilities'},
        {'us-gaap:Liabilities', 'ifrs-full:Liabilities'},
    ),
    'retained earnings': (
        {'retained earnings', 'accumulated deficit'},
        {'us-gaap:RetainedEarningsAccumulatedDeficit', 'ifrs-full:RetainedEarnings'},
    ),
    'equity': (
        {'equity', 'total equity', 'stockholders equity', 'total stockholders equity',
         'shareholders equity', 'total shareholders equity'},
        {'us-gaap:StockholdersEquity',
         'us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest',
         'ifrs-full:Equity'},
    ),
    'non controlling interests': (
        {'noncontrolling interests', 'non controlling interests', 'minority interest'},
        {'us-gaap:MinorityInterest', 'ifrs-full:NoncontrollingInterests'},
    ),
    'depreciation amortization': (
        {'depreciation and amortization', 'depreciation depletion and amortization', 'depreciation'},
        {'us-gaap:DepreciationDepletionAndAmortization', 'us-gaap:DepreciationAndAmortization',
         'us-gaap:Depreciation', 'ifrs-full:DepreciationAndAmortisationExpense',
         'ifrs-full:AdjustmentsForDepreciationAndAmortisationExpense'},
    ),
    'operating cash flow': (
        {'operating cash flow', 'cash from operating activities', 'net cash provided by operating activities',
         'net cash used in operating activities', 'cash flows from operating activities'},
        {'us-gaap:NetCashProvidedByUsedInOperatingActivities',
         'us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations',
         'ifrs-full:CashFlowsFromUsedInOperatingActivities'},
    ),
    'capex': (
        {'capex', 'capital expenditures', 'capital expenditure', 'purchases of property and equipment',
         'payments to acquire property plant and equipment'},
        {'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', 'us-gaap:PaymentsToAcquireProductiveAssets',
         'ifrs-full:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities'},
    ),
    'investing cash flow': (
        {'investing cash flow', 'net cash used in investing activities',
         'net cash provided by investing activities', 'cash flows from investing activities'},
        {'us-gaap:NetCashProvidedByUsedInInvestingActivities',
         'us-gaap:NetCashProvidedByUsedInInvestingActivitiesContinuingOperations',
         'ifrs-full:CashFlowsFromUsedInInvestingActivities'},
    ),
    'financing cash flow': (
        {'financing cash flow', 'net cash used in financing activities',
         'net cash provided by financing activities', 'cash flows from financing activities'},
        {'us-gaap:NetCashProvidedByUsedInFinancingActivities',
         'us-gaap:NetCashProvidedByUsedInFinancingActivitiesContinuingOperations',
         'ifrs-full:CashFlowsFromUsedInFinancingActivities'},
    ),
    'dividends paid': (
        {'dividends paid', 'payments of dividends'},
        {'us-gaap:PaymentsOfDividendsCommonStock', 'us-gaap:PaymentsOfDividends',
         'us-gaap:PaymentsOfOrdinaryDividends',
         'ifrs-full:DividendsPaidClassifiedAsFinancingActivities', 'ifrs-full:DividendsPaid'},
    ),
    'share repurchases': (
        {'share repurchases', 'repurchases of common stock', 'payments for repurchase of common stock'},
        {'us-gaap:PaymentsForRepurchaseOfCommonStock', 'us-gaap:PaymentsForRepurchaseOfEquity',
         'ifrs-full:PaymentsToAcquireOrRedeemEntitysShares'},
    ),
    'net increase in cash': (
        {'net increase in cash', 'net change in cash and cash equivalents'},
        {'us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect',
         'us-gaap:CashAndCashEquivalentsPeriodIncreaseDecrease',
         'ifrs-full:IncreaseDecreaseInCashAndCashEquivalents'},
    ),
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
        ('xbrli:period', 'xbrli:instant'),
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
        if leaf not in {'xbrli:identifier', 'xbrli:startdate', 'xbrli:enddate', 'xbrli:instant', 'xbrli:measure'}:
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
                    duration_expected = {('xbrli:entity', 'xbrli:identifier'), ('xbrli:period', 'xbrli:startdate'),
                                         ('xbrli:period', 'xbrli:enddate')}
                    instant_expected = {('xbrli:entity', 'xbrli:identifier'), ('xbrli:period', 'xbrli:instant')}
                    valid = set(values) in (duration_expected, instant_expected)
                    target[active['id']] = values if not active['bad'] and valid else None
                else:
                    expected = {('xbrli:measure',)}
                    target[active['id']] = values.get(('xbrli:measure',)) if not active['bad'] and set(values) == expected else None
        if tag == 'ix:header' and self.header_depth:
            self.header_depth -= 1


_NUM_BODY_RE = r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
_PLAIN_NUM_RE = re.compile(_NUM_BODY_RE)
_PAREN_NUM_RE = re.compile(rf'\(({_NUM_BODY_RE})\)')


def _parse_inline_amount(cell_text: object, fact_text: object, sign: object) -> Decimal | None:
    if not isinstance(cell_text, str) or not isinstance(fact_text, str) or sign not in (None, '+', '-'):
        return None
    cell_s = cell_text.strip()
    fact_s = fact_text.strip()
    try:
        if sign == '-':
            if not _PLAIN_NUM_RE.fullmatch(fact_s):
                return None
            if cell_s not in {fact_s, f'({fact_s})', f'-{fact_s}', f'−{fact_s}'}:
                return None
            return -Decimal(fact_s.replace(',', ''))
        if cell_s != fact_s:
            return None
        if _PLAIN_NUM_RE.fullmatch(fact_s):
            return Decimal(fact_s.replace(',', ''))
        if sign is None:
            match = _PAREN_NUM_RE.fullmatch(fact_s)
            if match:
                return -Decimal(match.group(1).replace(',', ''))
    except InvalidOperation:
        return None
    return None


def resolve_inline_facts(
    html: str,
    rows: list[dict],
    *,
    period_kinds: tuple[str, ...] | frozenset[str] = ('annual', 'quarter', 'instant'),
) -> list[dict]:
    """Resolve structurally simple numeric Inline XBRL facts (annual, quarterly 10-Q, instant, negative)."""
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
    allowed_kinds = set(period_kinds)
    result = []
    for cell, item in raw_facts:
        if (len(cell.get('inline_facts', [])) != 1 or
                not re.fullmatch(r'(?:us-gaap|ifrs-full):[A-Za-z][A-Za-z0-9]{0,120}', item.get('name', '')) or
                item['name'].split(':', 1)[0] not in parser.concept_prefixes or
                item.get('format') != 'ixt:num-dot-decimal' or
                not re.fullmatch(r'\d{1,2}', item.get('scale', '')) or not re.fullmatch(r'-?\d{1,2}', item.get('decimals', ''))):
            continue
        amount = _parse_inline_amount(cell.get('text'), item.get('text'), item.get('sign'))
        if amount is None or int(item['scale']) > 12:
            continue
        context = parser.contexts.get(item['contextref'])
        measure = parser.units.get(item['unitref'])
        if not isinstance(context, dict) or not isinstance(measure, str) or not re.fullmatch(r'iso4217:[A-Z]{3}', measure):
            continue
        identifier = context.get(('xbrli:entity', 'xbrli:identifier'), '')
        if not re.fullmatch(r'\d{10}', identifier):
            continue
        try:
            if ('xbrli:period', 'xbrli:instant') in context:
                start: date | None = None
                end = date.fromisoformat(context[('xbrli:period', 'xbrli:instant')])
                period_kind = 'instant'
            else:
                start = date.fromisoformat(context[('xbrli:period', 'xbrli:startdate')])
                end = date.fromisoformat(context[('xbrli:period', 'xbrli:enddate')])
                if start > end:
                    continue
                days = (end - start).days
                if 300 <= days <= 380:
                    period_kind = 'annual'
                elif 78 <= days <= 105:
                    period_kind = 'quarter'
                else:
                    continue
        except (ValueError, KeyError):
            continue
        if period_kind not in allowed_kinds:
            continue
        result.append({
            'cell_start': cell['start'], 'cell_end': cell['end'], 'value': str(amount),
            'concept': item['name'], 'currency': measure.split(':', 1)[1],
            'scale': int(item['scale']), 'decimals': int(item['decimals']),
            'entity_cik': identifier,
            'period_start': start.isoformat() if start is not None else None,
            'period_end': end.isoformat(),
            'period_kind': period_kind,
            'context_ref': item['contextref'], 'unit_ref': item['unitref'],
        })
    return result


def resolve_inline_annual_facts(html: str, rows: list[dict]) -> list[dict]:
    """Resolve structurally simple annual numeric facts in the target table row."""
    return [
        {k: v for k, v in item.items() if k != 'period_kind'}
        for item in resolve_inline_facts(html, rows, period_kinds=('annual',))
    ]


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
        text = re.sub(r'\b(?:fy\s*(?:19|20)\d{2}|q[1-4]\s*(?:fy\s*)?(?:19|20)\d{2}|(?:19|20)\d{2}\s*q[1-4])\b',
                      ' ', text, flags=re.IGNORECASE)
        return ' '.join(re.sub(r'[^a-z0-9]+', ' ', text.casefold()).split())
    row_labels = [clean(cell.get('text', '')) for cell in rows[-1]['cells'] if cell.get('text')]
    if not row_labels:
        return None
    normalized_label, row_label, normalized_unit = clean(label), row_labels[0], clean(unit)
    candidates = trace.get('inline_xbrl_facts') or trace.get('inline_xbrl_annual_facts', [])
    expected_kind = alignment.get('period_kind')
    matches = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        if expected_kind is not None and item.get('period_kind', 'annual') != expected_kind:
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
