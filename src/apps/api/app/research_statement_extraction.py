"""Deterministic financial-statement extraction from annual / interim report text (no network, no model).

Markets without free XBRL (mainland China, Hong Kong, ...) publish statements in PDF reports. This module locates the
consolidated income statement, balance sheet and cash-flow statement in an already acquired source (using the page map
when present), parses the table rows of ``pdftotext -layout`` style text, detects unit, currency and period columns,
maps labels to a documented standard-line dictionary (``STANDARD_LINES``) and returns every row with an EXACT quotation
and character offsets so that ``record_facts`` can cite it directly. Nothing is recorded automatically and nothing is
inferred beyond the text: unit, column and OCR uncertainty is reported as warnings.

Parsing rules (summary)
-----------------------
* Titles: a short stand-alone line such as ``合并利润表`` / ``Consolidated Statement of Profit or Loss for the year ended
  ...``. Table-of-contents lines (dot leaders / trailing page number), notes headings and supplementary tables are not
  statements. Scope ranking: consolidated > unspecified > parent company (``母公司``/``Company``); a parent-company
  statement is returned only when nothing else exists, with a warning.
* A statement may span pages: following pages without a title of their own (or with ``（续）`` / ``(continued)``)
  continue it until its terminal line (total liabilities and equity; earnings per share or total comprehensive income;
  cash at end of period) or three pages.
* Row = label followed by numeric cells separated by 2+ spaces (single-space tails and ``|`` separators are accepted
  when the tail has amount-like tokens). The last K cells are period values (K from the header, else from the rows);
  extra leading small tokens are note references, never values. Parentheses / leading minus are negative; ``-`` ``—``
  are nil. Rows with fewer values than columns are aligned by character position against complete rows.
* Unit: ``单位：元`` / ``人民币千元`` / ``RMB'000`` / ``HK$ million`` / ``in thousands`` in the header or page head.
  Per-share lines are never scaled. No unit found -> whole currency units with an explicit warning.
* Periods: explicit headers (``2025年度``, ``Year ended 31 December 2025``, ``As at 30 June 2026``, ``2025年1-6月``) else
  relative labels (本期/上期, 期末/期初) resolved against a header date or the report title. Interim income and cash-flow
  statements are year-to-date cumulative (``cumulative_ytd``): single quarters must be derived explicitly.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import json
import re
from statistics import median
from typing import Any, Callable, Literal, Mapping
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator

STATEMENT_KINDS = ('income', 'balance', 'cash_flow')
MAX_LINES = 40
MAX_CONTINUATION_PAGES = 2
MAX_UNMATCHED_ROWS_PER_STATEMENT = 60
MAX_RESULT_CHARS = 30_000
MAX_QUOTATION_CHARS = 1_000
MIN_DATA_ROWS = 4
SYNTHETIC_BLOCK_CHARS = 6_000
SYNTHETIC_MAX_BLOCKS = 400
MAX_OCR_SUGGESTED_PAGES = 20
LOW_OCR_CONFIDENCE = 70.0


# --------------------------------------------------------------------------------------------------------------
# Standard-line dictionary
# --------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class StdLine:
    key: str
    statement: str
    description: str
    zh: tuple[str, ...] = ()                  # exact normalized Chinese labels
    zh_re: tuple[str, ...] = ()               # full-match regexes on normalized Chinese labels
    en: tuple[str, ...] = ()                  # full-match regexes on normalized English labels
    ctx: tuple[tuple[str, str], ...] = ()     # (heading regex, bare label regex): bare label valid only under such a heading
    per_share: bool = False


def _net_cash(activity: str) -> str:
    return (r'net cash(?: (?:flows?|generated|provided|used|from|in|by|and|for|inflows?|outflows?|net))* '
            + activity + r' activities')


_OWNERS_EN = (r'(?:owners|equity holders|shareholders|holders of ordinary shares|ordinary shareholders|equity shareholders) '
              r'of the (?:company|parent|parent company)')
_ATTR_HEAD = r'(?:profit|income|earnings).*attributable to|^attributable to'

STANDARD_LINES: tuple[StdLine, ...] = (
    # ---- income statement
    StdLine('revenue', 'income', 'Total / operating revenue (营业总收入, 营业收入, Revenue, Turnover)',
            zh=('营业总收入', '营业收入', '主营业务收入', '营业额', '收入'),
            en=(r'(?:total |net )?revenues?', r'turnover', r'revenues? from contracts? with customers', r'net sales', r'sales',
                r'total net revenues?')),
    StdLine('cost_of_revenue', 'income', 'Cost of revenue / sales (营业成本)',
            zh=('营业成本', '主营业务成本'),
            en=(r'cost of (?:sales|revenues?|goods sold|services|goods and services|sales and services)', r'direct costs?')),
    StdLine('gross_profit', 'income', 'Gross profit (毛利)', zh=('毛利', '毛利润'), en=(r'gross profit',)),
    StdLine('total_operating_costs', 'income', 'Total operating costs (营业总成本)', zh=('营业总成本',),
            en=(r'total operating (?:costs|expenses)',)),
    StdLine('selling_expense', 'income', 'Selling expenses (销售费用)', zh=('销售费用',),
            en=(r'selling and distribution (?:expenses|costs)', r'selling expenses', r'distribution (?:costs|expenses)')),
    StdLine('admin_expense', 'income', 'Administrative expenses (管理费用)', zh=('管理费用',),
            en=(r'(?:general and )?administrative expenses',)),
    StdLine('rd_expense', 'income', 'Research and development expenses (研发费用)', zh=('研发费用',),
            en=(r'research and development (?:expenses|costs|expenditure)',)),
    StdLine('finance_expense', 'income', 'Finance costs / expenses (财务费用)', zh=('财务费用',),
            en=(r'finance (?:costs?|expenses?)',)),
    StdLine('operating_profit', 'income', 'Operating profit (营业利润)', zh=('营业利润',),
            en=(r'operating (?:profit|income)', r'profit from operations', r'income from operations',
                r'profit from operating activities')),
    StdLine('profit_before_tax', 'income', 'Profit before income tax (利润总额)', zh=('利润总额',),
            en=(r'(?:profit|income|earnings) before (?:income )?tax(?:ation)?', r'profit before income tax(?:es)?')),
    StdLine('income_tax', 'income', 'Income tax expense (所得税费用)', zh=('所得税费用',),
            en=(r'income tax(?: expenses?| credit| charge)?', r'taxation', r'tax expenses?', r'income tax expense credit')),
    StdLine('net_profit', 'income', 'Net profit for the period (净利润)', zh=('净利润', '本期净利润'),
            en=(r'(?:net )?(?:profit|income|earnings) for the (?:year|period)', r'net profit', r'net income')),
    StdLine('net_profit_attributable_parent', 'income',
            'Net profit attributable to owners of the parent (归属于母公司所有者的净利润)',
            zh=('归属于母公司所有者的净利润', '归属于母公司股东的净利润', '归属于母公司的净利润', '归属于上市公司股东的净利润',
                '归属于母公司普通股股东的净利润', '归属于母公司所有者净利润'),
            en=(r'(?:net )?(?:profit|income|earnings)(?: for the (?:year|period))? attributable to (?:the )?' + _OWNERS_EN,
                r'(?:net )?(?:profit|income|earnings)(?: for the (?:year|period))? attributable to owners of the parent'),
            ctx=((_ATTR_HEAD, r'(?:the )?' + _OWNERS_EN), (_ATTR_HEAD, r'owners|equity holders'))),
    StdLine('net_profit_minority', 'income', 'Net profit attributable to non-controlling interests (少数股东损益)',
            zh=('少数股东损益',),
            en=(r'(?:net )?(?:profit|income|earnings)(?: for the (?:year|period))? attributable to non controlling interests?',),
            ctx=((_ATTR_HEAD, r'non controlling interests?'),)),
    StdLine('eps_basic', 'income', 'Basic earnings per share (基本每股收益)', per_share=True,
            zh=('基本每股收益',), en=(r'basic (?:earnings|loss) per (?:ordinary )?share',),
            ctx=((r'per (?:ordinary )?share|每股', r'basic'),)),
    StdLine('eps_diluted', 'income', 'Diluted earnings per share (稀释每股收益)', per_share=True,
            zh=('稀释每股收益',), en=(r'diluted (?:earnings|loss) per (?:ordinary )?share',),
            ctx=((r'per (?:ordinary )?share|每股', r'diluted'),)),
    StdLine('total_comprehensive_income', 'income', 'Total comprehensive income (综合收益总额)',
            zh=('综合收益总额',), en=(r'total comprehensive income(?: for the (?:year|period))?',)),
    # ---- balance sheet
    StdLine('cash_and_equivalents', 'balance', 'Cash and cash equivalents (货币资金)', zh=('货币资金',),
            en=(r'cash and cash equivalents', r'cash and bank balances', r'cash at bank and in hand', r'cash and cash balances')),
    StdLine('accounts_receivable', 'balance', 'Trade / accounts receivable (应收账款)', zh=('应收账款', '应收票据及应收账款'),
            en=(r'trade receivables', r'trade and (?:bills|other) receivables', r'accounts receivable')),
    StdLine('inventories', 'balance', 'Inventories (存货)', zh=('存货',), en=(r'inventories',)),
    StdLine('total_current_assets', 'balance', 'Total current assets (流动资产合计)', zh=('流动资产合计', '流动资产总计'),
            en=(r'total current assets',)),
    StdLine('goodwill', 'balance', 'Goodwill (商誉)', zh=('商誉',), en=(r'goodwill',)),
    StdLine('total_assets', 'balance', 'Total assets (资产总计)', zh=('资产总计', '资产合计'), en=(r'total assets',)),
    StdLine('short_term_borrowings', 'balance', 'Short-term borrowings (短期借款)', zh=('短期借款',),
            en=(r'short term (?:bank )?(?:borrowings|loans)',)),
    StdLine('total_current_liabilities', 'balance', 'Total current liabilities (流动负债合计)',
            zh=('流动负债合计', '流动负债总计'), en=(r'total current liabilities',)),
    StdLine('long_term_borrowings', 'balance', 'Long-term borrowings (长期借款)', zh=('长期借款',),
            en=(r'long term (?:bank )?(?:borrowings|loans)',)),
    StdLine('total_liabilities', 'balance', 'Total liabilities (负债合计)', zh=('负债合计', '负债总计'), en=(r'total liabilities',)),
    StdLine('equity_attributable_parent', 'balance', 'Equity attributable to owners of the parent (归属于母公司所有者权益合计)',
            zh=('归属于母公司所有者权益合计', '归属于母公司股东权益合计', '归属于母公司所有者权益', '归属于母公司股东权益',
                '归属于母公司所有者权益总计', '归属于上市公司股东的所有者权益', '归属于母公司股东的权益'),
            en=(r'(?:total )?equity attributable to (?:the )?' + _OWNERS_EN,
                r'(?:total )?equity attributable to owners of the parent'),
            ctx=((r'equity attributable to|^attributable to', r'(?:the )?' + _OWNERS_EN),)),
    StdLine('non_controlling_interests', 'balance', 'Non-controlling interests (少数股东权益)', zh=('少数股东权益',),
            en=(r'non controlling interests?',)),
    StdLine('total_equity', 'balance', 'Total equity (所有者权益合计)',
            zh=('所有者权益合计', '股东权益合计', '所有者权益总计', '股东权益总计', '权益合计'),
            en=(r'total equity', r'total shareholders equity', r'total owners equity')),
    StdLine('total_liabilities_and_equity', 'balance', 'Total liabilities and equity (负债和所有者权益总计)',
            zh_re=(r'负债(?:和|及)(?:所有者|股东)?权益(?:总计|合计)',), en=(r'total (?:liabilities and equity|equity and liabilities)',)),
    # ---- cash-flow statement
    StdLine('operating_cash_flow', 'cash_flow', 'Net cash from operating activities (经营活动产生的现金流量净额)',
            zh_re=(r'经营活动(?:产生|使用|所产生|所使用|产生或使用)?的?现金流量净额',), en=(_net_cash('operating'),)),
    StdLine('investing_cash_flow', 'cash_flow', 'Net cash from investing activities (投资活动产生的现金流量净额)',
            zh_re=(r'投资活动(?:产生|使用|所产生|所使用|产生或使用)?的?现金流量净额',), en=(_net_cash('investing'),)),
    StdLine('financing_cash_flow', 'cash_flow', 'Net cash from financing activities (筹资活动产生的现金流量净额)',
            zh_re=(r'筹资活动(?:产生|使用|所产生|所使用|产生或使用)?的?现金流量净额',), en=(_net_cash('financing'),)),
    StdLine('capex', 'cash_flow',
            'Capital expenditure: purchase of property, plant, equipment and other long-term assets (购建固定资产、无形资产和其他长期资产支付的现金)',
            zh_re=(r'(?:购建|购置|购买)固定资产(?:、无形资产和其他长期资产)?(?:所)?支付的现金',),
            en=(r'(?:(?:payments?|cash paid) (?:for|to acquire|on) )?(?:(?:purchases?|acquisitions?|additions?) (?:of|to) )?(?:the )?'
                r'property plant and equipment(?: and (?:intangible assets|other (?:long term|non current) assets|right of use assets))*',
                r'capital expenditures?', r'purchases? of fixed assets', r'payments? for capital expenditures?')),
    StdLine('net_increase_in_cash', 'cash_flow', 'Net increase in cash and cash equivalents (现金及现金等价物净增加额)',
            zh=('现金及现金等价物净增加额', '现金及现金等价物净变动额', '现金及现金等价物净增加'),
            en=(r'net (?:increase|decrease|increase decrease|decrease increase) in cash and cash equivalents',)),
    StdLine('cash_begin_of_period', 'cash_flow', 'Cash and cash equivalents at beginning of period (期初现金及现金等价物余额)',
            zh=('期初现金及现金等价物余额',),
            en=(r'cash and cash equivalents at (?:the )?(?:beginning|start) of (?:the )?(?:year|period)',)),
    StdLine('cash_end_of_period', 'cash_flow', 'Cash and cash equivalents at end of period (期末现金及现金等价物余额)',
            zh=('期末现金及现金等价物余额',),
            en=(r'cash and cash equivalents at (?:the )?end of (?:the )?(?:year|period)',)),
)
STANDARD_LINE_BY_KEY = {line.key: line for line in STANDARD_LINES}
_COMPILED = {
    line.key: (set(line.zh), [re.compile(p) for p in line.zh_re], [re.compile(p) for p in line.en],
               [(re.compile(h), re.compile(b)) for h, b in line.ctx])
    for line in STANDARD_LINES}
_CJK_RE = re.compile(r'[㐀-鿿]')


def _norm_label(label: str) -> str:
    s = unicodedata.normalize('NFKC', label).strip().lower().replace('’', "'")
    for _ in range(4):
        n = re.sub(r'^(?:[一二三四五六七八九十]+[、.]|\([一二三四五六七八九十\d]+\)\s*[、.]?|\d{1,2}[.、)])\s*', '', s)
        n = re.sub(r'^(?:其中|加|减|add|less|including|of which)\s*:\s*', '', n)
        if n == s:
            break
        s = n
    s = re.sub(r'\s*/?\s*\(\s*(?:loss(?:es)?|deficit|expenses?|income|charge|credit|decrease|increase)\s*\)\s*/?\s*', ' ', s)
    s = re.sub(r'\([^)]*\)', ' ', s)
    s = re.sub(r'["“”‘\']', '', s)
    s = s.replace('&', ' and ')
    s = re.sub(r'[/,;:\-\u2010-\u2013]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return re.sub(r'(?<=[㐀-鿿])\s+(?=[㐀-鿿])', '', s)


def match_standard_line(statement: str, label: str, heading: str | None = None) -> StdLine | None:
    """Standard line of ``statement`` that ``label`` denotes (``heading``: last heading line, normalized), or None."""
    norm = _norm_label(label)
    if not norm:
        return None
    cjk = bool(_CJK_RE.search(norm))
    for line in STANDARD_LINES:
        if line.statement != statement:
            continue
        zh, zh_re, en, _ = _COMPILED[line.key]
        if cjk:
            if norm in zh or any(p.fullmatch(norm) for p in zh_re):
                return line
        elif any(p.fullmatch(norm) for p in en):
            return line
    if heading:
        for line in STANDARD_LINES:
            if line.statement != statement:
                continue
            per_share_heading = 'per share' in heading or '每股' in heading
            for head_re, label_re in _COMPILED[line.key][3]:
                if not (head_re.search(heading) and label_re.fullmatch(norm)):
                    continue
                if per_share_heading != line.per_share:
                    continue
                if line.key.startswith('net_profit') and 'comprehensive' in heading:
                    continue
                if line.key.startswith('net_profit') and 'equity attributable' in heading:
                    continue
                return line
    return None


# --------------------------------------------------------------------------------------------------------------
# Titles
# --------------------------------------------------------------------------------------------------------------

_CN_TITLE = re.compile(
    r'^(?:[一二三四五六七八九十]+[、.．]\s*|\d{1,2}[、.．]\s*|[(（][一二三四五六七八九十\d]+[)）]\s*)?'
    r'(?P<scope>合并|母公司|本公司|公司)?\s*(?P<kind>资产负债表|利润表|损益表|综合收益表|现金流量表)(?P<tail>.*)$')
_CN_TAIL_OK = re.compile(r'^(?:\s*[（(]?\s*(?:续|续表|未经审计|已审计|经审计)\s*[)）]?|\s*\d{4}\s*年|\s*[（(]\s*除|\s*(?:及|和)(?:其他)?综合收益表'
                         r'|\s*[-—–:：]\s*\S|\s*编制单位|\s*单位|\s*[（(]\s*\d{4})')
_CN_TAIL_BAD = re.compile(r'补充资料|附注|项目注释|科目|编制基础|列报|说明')
_EN_PREFIX = r'(?:(?:the|unaudited|audited|condensed|consolidated|interim|group|company|summary|summarised|summarized)\s+)*'
_EN_KIND = (r'(?:statements? of (?:profit or loss(?: and other comprehensive income)?|profit and loss(?: and other comprehensive income)?'
            r'|comprehensive income|income|operations|earnings|financial position|financial condition|cash flows?)'
            r'|income statements?|balance sheets?|profit and loss accounts?|cash flow statements?)')
_EN_TITLE = re.compile(rf'^(?P<pre>{_EN_PREFIX})(?P<kind>{_EN_KIND})(?P<tail>.*)$')
_EN_TAIL_OK = re.compile(r'^(?:\s*[-–—:,]?\s*)?(?:\(?continued\)?|\(?unaudited\)?|\(?audited\)?|for the |as at |as of |at \d|'
                         r'and other comprehensive income|and comprehensive income|\(|of the company|of the group)')
_STOP_TITLE = re.compile(
    r'^(?:合并|母公司)?(?:所有者权益变动表|股东权益变动表)|财务报表附注|^附注|财务报表注释|现金流量表补充资料|补充资料'
    r'|^notes? to (?:the )?(?:consolidated |condensed |interim )*financial statements'
    r'|^(?:the )?(?:consolidated |condensed )*statements? of changes in (?:shareholders\'? |owners\'? )?equity', re.I)
_TOC_TAIL = re.compile(r'(?:\.{3,}|·{2,}|…+|\s\d{1,3}\s*$)')


@dataclass(frozen=True)
class _Title:
    kind: str          # income | balance | cash_flow | other
    scope: str         # consolidated | unspecified | parent_company
    continued: bool
    text: str


def _parse_title(line: str) -> _Title | None:
    s = line.strip()
    if not s or len(s) > 140:
        return None
    if len(s) <= 80 and _STOP_TITLE.search(s):
        return _Title('other', 'unspecified', False, s)
    n = unicodedata.normalize('NFKC', s)
    m = _CN_TITLE.match(n)
    if m:
        tail = m.group('tail').strip()
        if _CN_TAIL_BAD.search(tail) or (tail and not _CN_TAIL_OK.match(m.group('tail'))):
            return None
        if _TOC_TAIL.search(tail) and '年' not in tail:
            return None
        scope = {'合并': 'consolidated', '母公司': 'parent_company', '本公司': 'parent_company',
                 '公司': 'parent_company'}.get(m.group('scope') or '', 'unspecified')
        kind = {'资产负债表': 'balance', '现金流量表': 'cash_flow'}.get(m.group('kind'), 'income')
        return _Title(kind, scope, '续' in tail, s)
    low = re.sub(r'\s+', ' ', n.lower()).strip()
    low = re.sub(r'^\d{1,2}[.)]\s*', '', low)
    m = _EN_TITLE.match(low)
    if m:
        tail = m.group('tail')
        if tail.strip() and not _EN_TAIL_OK.match(tail):
            return None
        if _TOC_TAIL.search(tail) and 'ended' not in tail and 'as at' not in tail:
            return None
        pre, kind_text = m.group('pre'), m.group('kind')
        kind = ('cash_flow' if 'cash flow' in kind_text else
                'balance' if ('financial position' in kind_text or 'financial condition' in kind_text or 'balance sheet' in kind_text)
                else 'income')
        if 'consolidated' in pre or 'group' in pre:
            scope = 'consolidated'
        elif 'company' in pre or tail.strip().startswith('of the company'):
            scope = 'parent_company'
        else:
            scope = 'unspecified'
        return _Title(kind, scope, 'continued' in tail, s)
    return None


_SCOPE_RANK = {'consolidated': 0, 'unspecified': 1, 'parent_company': 2}


# --------------------------------------------------------------------------------------------------------------
# Row parsing
# --------------------------------------------------------------------------------------------------------------

_MINUS = '−–—－-'
_VALUE_RE = re.compile(r'^\(?[-−–－]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\)?$')
_NIL_RE = re.compile(r'^(?:[-−–—－]{1,3}|nil)$', re.I)
_AMOUNT_LIKE_RE = re.compile(r'[,.()]|^[-−–－]')
_NOTE_RE = re.compile(r'^(?:附注)?[一二三四五六七八九十]+[、.]\s*\d+(?:[(（]\d+[)）])?$|^\d{1,3}$|^\d{1,3}\s?[a-z]$|'
                      r'^[(（][a-z0-9ivx]{1,4}[)）]$|^\d{1,3}(?:[,，、]\s*\d{1,3})+$|^note\s*\d+$', re.I)
_HEADER_LABELS = {'', '项目', '附注', '注释', '科目', '注', 'note', 'notes', 'item', 'items', 'particulars'}
_NOISE_LABEL = re.compile(r'年度报告|半年度报告|季度报告|annual report|interim report|^page\b|第\s*\d+\s*页|公司代码|证券代码|stock code', re.I)
_LABEL_CHAR = re.compile(r'[A-Za-z㐀-鿿]')
_MARKER_START = re.compile(r'^(?:[一二三四五六七八九十]+[、.．]|[(（]|加[:：]|减[:：]|其中[:：]|add:|less:)', re.I)


@dataclass
class _Tok:
    text: str
    start: int
    end: int
    nil: bool = False


@dataclass
class _Row:
    page: int
    abs_start: int            # absolute offset of the first quoted character
    abs_end: int              # exclusive
    label: str
    toks: list[_Tok]
    note: str | None = None
    multi_line: bool = False
    heading: str | None = None
    closed: bool = False


def _pad_parens(s: str) -> str:
    s = re.sub(r'([(（])(\s+)(?=[\d\-−])', r'\2\1', s)
    return re.sub(r'(?<=\d)(\s+)([)）])', r'\2\1', s)


def _nfkc(text: str) -> str:
    return unicodedata.normalize('NFKC', text).strip()


def _is_nil(text: str) -> bool:
    return bool(_NIL_RE.match(_nfkc(text)))


def _is_value(text: str) -> bool:
    t = _nfkc(text)
    return bool(_VALUE_RE.match(t)) or bool(_NIL_RE.match(t))


def _bare_year(text: str) -> bool:
    return bool(re.fullmatch(r'(?:19|20)\d\d', text.strip()))


def _amount_like(text: str) -> bool:
    return bool(_AMOUNT_LIKE_RE.search(_nfkc(text))) or _is_nil(text)


def _split_row(line: str) -> tuple[str, list[_Tok], str | None] | None:
    """(label, value tokens, note) of one table line, or None when the line has no value tokens."""
    s = _pad_parens(line.replace('|', ' ').replace('　', '  '))
    cells = [(m.group(), m.start(), m.end()) for m in re.finditer(r'\S(?: ?\S)*', s)]
    if not cells:
        return None
    toks: list[_Tok] = []
    i = len(cells)
    while i > 0 and _is_value(cells[i - 1][0]):
        text, st, en = cells[i - 1]
        toks.append(_Tok(text, st, en, _is_nil(text)))
        i -= 1
    toks.reverse()
    label_cells = cells[:i]
    if not toks:
        text, st, _ = cells[-1]
        parts = [(m.group(), st + m.start(), st + m.end()) for m in re.finditer(r'\S+', text)]
        peeled: list[tuple[str, int, int]] = []
        while len(parts) > 1 and _is_value(parts[-1][0]):
            peeled.append(parts.pop())
        peeled.reverse()
        # leading plain integers (notes / numbering) before the first amount-like token stay with the label
        while peeled and not _amount_like(peeled[0][0]):
            parts.append(peeled.pop(0))
        if not peeled:
            return None
        toks = [_Tok(p[0], p[1], p[2], _is_nil(p[0])) for p in peeled]
        label_cells = cells[:-1] + [(' '.join(p[0] for p in parts), cells[-1][1], parts[-1][2])]
    note = None
    if len(label_cells) >= 2 and _NOTE_RE.match(_nfkc(label_cells[-1][0])):
        note = label_cells[-1][0].strip()
        label_cells = label_cells[:-1]
    return ' '.join(c[0] for c in label_cells).strip(), toks, note


def _to_decimal(text: str) -> tuple[Decimal, str] | None:
    """(value, plain decimal as stated) for a numeric token; parentheses / leading minus mean negative."""
    t = _nfkc(text)
    neg = False
    if t.startswith('(') and t.endswith(')'):
        neg, t = True, t[1:-1].strip()
    if t and t[0] in _MINUS:
        neg, t = True, t[1:].strip()
    plain = t.replace(',', '')
    try:
        value = Decimal(plain)
    except InvalidOperation:
        return None
    if value == 0:
        neg = False
    return (-value if neg else value), ('-' if neg else '') + plain


def _fmt(value: Decimal) -> str:
    return '0' if value == 0 else format(value.normalize(), 'f')


def _looks_open(label: str) -> bool:
    s = label.strip()
    if not s:
        return False
    if re.search(r'[和及与或、的以并，,]$', s):
        return True
    if re.search(r'(?:\b(?:of|and|to|in|from|on|for|the|with|by|or|at|a|an|net|total)\b|[,/\-–(])$', s, re.I):
        return True
    return s.count('(') > s.count(')') or s.count('（') > s.count('）')


def _join_label(a: str, b: str) -> str:
    if a and b and _CJK_RE.search(a[-1]) and _CJK_RE.search(b[0]):
        return a + b
    return (a + ' ' + b).strip()


def _is_heading_text(text: str) -> bool:
    low = _nfkc(text).lower()
    return bool(low.endswith(':') or 'attributable to' in low or 'per share' in low or '每股' in low)


@dataclass
class _Item:
    kind: str                 # row | header | text
    abs_start: int
    abs_end: int
    text: str
    row: _Row | None = None


def _classify_lines(page: int, lines: list[tuple[int, str]]) -> list[_Item]:
    items: list[_Item] = []
    for abs_start, text in lines:
        stripped = text.strip()
        if not stripped:
            continue
        lead = len(text) - len(text.lstrip())
        a0, a1 = abs_start + lead, abs_start + lead + len(stripped)
        parsed = _split_row(text)
        if parsed is None:
            items.append(_Item('text', a0, a1, stripped))
            continue
        label, toks, note = parsed
        all_years = all(_bare_year(t.text) for t in toks)
        if label.strip().lower() in _HEADER_LABELS or (all_years and len(toks) >= 2 and not _LABEL_CHAR.search(label)):
            items.append(_Item('header', a0, a1, stripped))
        elif not _LABEL_CHAR.search(label) or _NOISE_LABEL.search(label):
            items.append(_Item('text', a0, a1, stripped))
        else:
            items.append(_Item('row', a0, a1, stripped, _Row(page, a0, a1, label, toks, note)))
    return items


def _merge_multiline(items: list[_Item], kind: str) -> tuple[list[_Row], list[_Item]]:
    """Rows after joining wrapped labels, plus the header-zone items (everything before the first row)."""
    header_zone: list[_Item] = []
    rows: list[_Row] = []
    pending: _Item | None = None
    heading: str | None = None
    for it in items:
        if not rows and it.kind != 'row' and pending is None and not any(r for r in rows):
            if it.kind in ('header', 'text'):
                header_zone.append(it)
                continue
        if it.kind == 'header':
            continue
        if it.kind == 'row':
            assert it.row is not None
            row = it.row
            row.heading = heading
            if pending is not None:
                combined = _join_label(pending.text, row.label)
                if (not _MARKER_START.match(row.label)
                        and ((match_standard_line(kind, row.label, heading) is None
                              and match_standard_line(kind, combined, heading) is not None)
                             or _looks_open(pending.text) or row.label[:1].islower())):
                    row.label, row.abs_start, row.multi_line = combined, pending.abs_start, True
                pending = None
            rows.append(row)
            continue
        txt = it.text
        prev = rows[-1] if rows else None
        if (pending is None and prev is not None and not prev.closed and not _MARKER_START.match(txt)
                and not _is_heading_text(txt) and len(txt) <= 60
                and (_looks_open(prev.label)
                     or (match_standard_line(kind, prev.label, prev.heading) is None
                         and match_standard_line(kind, _join_label(prev.label, txt), prev.heading) is not None))):
            prev.label = _join_label(prev.label, txt)
            prev.abs_end = it.abs_end
            prev.multi_line = prev.closed = True
            continue
        if prev is not None:
            prev.closed = True
        if _is_heading_text(txt):
            heading, pending = _norm_label(txt), None
        elif pending is not None and _looks_open(pending.text):
            pending = _Item('text', pending.abs_start, it.abs_end, _join_label(pending.text, txt))
        else:
            pending = it
    return rows, header_zone


# --------------------------------------------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------------------------------------------

_CUR_CN = {'人民币': 'CNY', '港币': 'HKD', '港元': 'HKD', '美元': 'USD', '日元': 'JPY', '欧元': 'EUR', '英镑': 'GBP',
           '新加坡元': 'SGD', '新台币': 'TWD'}
_CUR_EN = {'rmb': 'CNY', 'cny': 'CNY', 'hk$': 'HKD', 'hkd': 'HKD', 'us$': 'USD', 'usd': 'USD', 's$': 'SGD',
           'sgd': 'SGD', 'jpy': 'JPY', 'gbp': 'GBP', 'eur': 'EUR', 'nt$': 'TWD', 'twd': 'TWD', '¥': 'CNY'}
_CN_SCALE = {'': 1, '千': 1_000, '万': 10_000, '百万': 1_000_000, '千万': 10_000_000, '亿': 100_000_000}
_EN_SCALE = {'000': 1_000, '0,000': 10_000, 'thousand': 1_000, 'thousands': 1_000, 'k': 1_000, 'million': 1_000_000,
             'millions': 1_000_000, 'mn': 1_000_000, 'm': 1_000_000, 'billion': 1_000_000_000, 'billions': 1_000_000_000,
             'bn': 1_000_000_000}
_CUR_EN_ALT = r'rmb|cny|hk\$|hkd|us\$|usd|s\$|sgd|jpy|gbp|eur|nt\$|twd|¥'
_UNIT_CN = re.compile(r'(?P<c1>人民币|港币|港元|美元|日元|欧元|英镑|新加坡元|新台币)?\s*(?P<s>百万|千万|亿|万|千)?\s*(?P<c2>港元|美元|人民币|日元|欧元|英镑|元)')
_UNIT_CN_CTX = re.compile(r'单位|金额|币种|[(（]\s*(?:人民币|港币|港元|美元)?\s*(?:百万|千万|亿|万|千)?元\s*[)）]'
                          r'|(?:人民币|港币|港元|美元)\s*(?:百万|千万|亿|万|千)元|(?:百万|千)(?:港元|美元|人民币)')
_UNIT_CN_BARE = re.compile(r'单位\s*[:：]?\s*元|金额单位[^元]{0,6}元|[(（]\s*元\s*[)）]|币种')
_UNIT_EN_A = re.compile(rf"(?P<c>{_CUR_EN_ALT})\s*['’]?\s*(?P<s>0,000|000|thousands?|millions?|billions?|mn|bn|m|k)\b", re.I)
_UNIT_EN_B = re.compile(rf"(?P<s>thousands?|millions?|billions?)\s+of\s+(?P<c>{_CUR_EN_ALT})", re.I)
_UNIT_EN_C = re.compile(r"(?:\bin|expressed in|amounts? in|\()\s*(?P<s>thousands?|millions?|billions?)\b", re.I)
_CUR_ONLY_EN = re.compile(rf"(?<![A-Za-z])(?P<c>{_CUR_EN_ALT})(?![A-Za-z])", re.I)


@dataclass
class _Unit:
    scale: int
    label: str
    currency: str | None
    basis: str            # stated | currency_only | assumed
    text: str = ''
    conflict: bool = False


def _detect_unit(lines: list[str]) -> _Unit:
    found: list[tuple[int, str, str | None, str]] = []   # (scale, stated_as, currency, line text)
    currency_hint: str | None = None
    for raw in lines:
        s = _nfkc(raw)
        if not s or len(s) > 300:
            continue
        if _CJK_RE.search(s) and _UNIT_CN_CTX.search(s):
            for m in _UNIT_CN.finditer(s):
                scale_word = m.group('s') or ''
                if m.group('c1') is None and not scale_word and not _UNIT_CN_BARE.search(s):
                    continue
                cur = _CUR_CN.get(m.group('c1') or '') or (_CUR_CN.get(m.group('c2')) if m.group('c2') != '元' else None)
                found.append((_CN_SCALE[scale_word], m.group().strip(), cur, s[:120]))
                break
        m = _UNIT_EN_A.search(s) or _UNIT_EN_B.search(s)
        if m:
            found.append((_EN_SCALE.get(m.group('s').lower(), 1), m.group().strip(), _CUR_EN.get(m.group('c').lower()), s[:120]))
        else:
            m = _UNIT_EN_C.search(s)
            if m:
                found.append((_EN_SCALE[m.group('s').lower()], m.group().strip(' ('), None, s[:120]))
        if currency_hint is None and len(s) < 120:
            m2 = _CUR_ONLY_EN.search(s)
            if m2:
                currency_hint = _CUR_EN.get(m2.group('c').lower())
            elif _CJK_RE.search(s):
                currency_hint = next((code for word, code in _CUR_CN.items() if word in s), None)
    if found:
        scale, label, cur, text = found[0]
        if cur is None:
            cur = next((f[2] for f in found if f[2]), None) or currency_hint
        return _Unit(scale, label, cur, 'stated', text, any(f[0] != scale for f in found[1:]))
    if currency_hint:
        return _Unit(1, currency_hint, currency_hint, 'currency_only')
    return _Unit(1, 'unknown', None, 'assumed')


def _scale_value(value: Decimal, unit: _Unit, std: StdLine | None, label: str) -> tuple[Decimal, bool]:
    per_share = bool(std is not None and std.per_share) or bool(re.search(r'每股|per share', label, re.I))
    if per_share or unit.scale == 1:
        return value, per_share
    with localcontext() as ctx:
        ctx.prec = 60
        return value * unit.scale, False


def _scale_label(unit: _Unit) -> str:
    names = {1: 'currency units', 1_000: 'thousands', 10_000: 'ten-thousands', 1_000_000: 'millions',
             10_000_000: 'ten-millions', 100_000_000: 'hundred-millions', 1_000_000_000: 'billions'}
    return names.get(unit.scale, f'x{unit.scale}')


# --------------------------------------------------------------------------------------------------------------
# Periods
# --------------------------------------------------------------------------------------------------------------

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MONTHS['sept'] = 9
_MON = '|'.join(sorted(_MONTHS, key=len, reverse=True))
_EN_DATE = (rf'(?:(?P<d1>\d{{1,2}})\s+(?P<m1>{_MON})\.?,?\s+(?P<y1>\d{{4}})'
            rf'|(?P<m2>{_MON})\.?\s+(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y2>\d{{4}}))')
_DUR_WORDS = {'year': 12, 'twelve months': 12, '12 months': 12, 'six months': 6, '6 months': 6, 'three months': 3,
              '3 months': 3, 'nine months': 9, '9 months': 9, 'half year': 6, 'period': 0}
_P_EN_DUR = re.compile(rf'(?P<w>year|twelve months|12 months|six months|6 months|three months|3 months|nine months|9 months|half year|period)'
                       rf'\s+ended\s+{_EN_DATE}', re.I)
_P_EN_INST = re.compile(_EN_DATE, re.I)
_P_CN_RANGE_FULL = re.compile(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*[至到\-—–~]\s*(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日')
_P_CN_MONTHS = re.compile(r'(\d{4})\s*年\s*(\d{1,2})\s*[-—–~至到]\s*(\d{1,2})\s*月')
_P_CN_FY = re.compile(r'(\d{4})\s*年\s*度(?!报)')
_P_CN_H1 = re.compile(r'(\d{4})\s*年\s*(?:上半年|半年度(?!报)|中期)')
_P_CN_Q = re.compile(r'(\d{4})\s*年\s*第?\s*([一二三四1-4])\s*季度(?!报)')
_P_CN_INST = re.compile(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日')
_Q_NUM = {'一': 1, '二': 2, '三': 3, '四': 4, '1': 1, '2': 2, '3': 3, '4': 4}
_REL_CUR = r'本期金额|本期发生额|本年金额|本年发生额|本期数|本年数|期末余额|期末数|年末余额|本期|本年|期末|本报告期'
_REL_PRI = r'上期金额|上期发生额|上年金额|上年同期|上年发生额|上期数|上年数|上年年末余额|上年年末|期初余额|期初数|年初余额|年初|上期|上年'
_REL_RE = re.compile(f'(?P<cur>{_REL_CUR})|(?P<pri>{_REL_PRI})')
_PRIOR_YEAR_END = re.compile(r'期初|年初|上年年末')


@dataclass
class _PTok:
    pos: int
    label: str
    end: date
    start: date | None
    kind: str           # duration | instant
    months: int | None


def _eom(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _month_start(end: date, months: int) -> date:
    y, m = end.year, end.month - months + 1
    while m < 1:
        y, m = y - 1, m + 12
    return date(y, m, 1)


def _en_date(m: re.Match[str]) -> date | None:
    if m.group('y1'):
        return _safe_date(int(m.group('y1')), _MONTHS[m.group('m1').lower()], int(m.group('d1')))
    return _safe_date(int(m.group('y2')), _MONTHS[m.group('m2').lower()], int(m.group('d2')))


def _period_tokens(text: str) -> list[_PTok]:
    s = _nfkc(text)
    toks: list[_PTok] = []
    taken: list[tuple[int, int]] = []

    def add(m: re.Match[str], end: date | None, start: date | None, kind: str, months: int | None) -> None:
        if end is None or any(m.start() < b and a < m.end() for a, b in taken):
            return
        taken.append((m.start(), m.end()))
        toks.append(_PTok(m.start(), m.group().strip(), end, start, kind, months))

    for m in _P_EN_DUR.finditer(s):
        end = _en_date(m)
        months = _DUR_WORDS[m.group('w').lower()]
        add(m, end, _month_start(end, months) if end and months else None, 'duration', months or None)
    for m in _P_CN_RANGE_FULL.finditer(s):
        y1, m1, d1, y2, m2, d2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4), int(m.group(5)), int(m.group(6))
        end, start = _safe_date(int(y2) if y2 else y1, m2, d2), _safe_date(y1, m1, d1)
        add(m, end, start, 'duration', (end.year - start.year) * 12 + end.month - start.month + 1 if end and start else None)
    for m in _P_CN_MONTHS.finditer(s):
        y, m1, m2 = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= m1 <= m2 <= 12:
            add(m, _eom(y, m2), date(y, m1, 1), 'duration', m2 - m1 + 1)
    for m in _P_CN_FY.finditer(s):
        add(m, date(int(m.group(1)), 12, 31), date(int(m.group(1)), 1, 1), 'duration', 12)
    for m in _P_CN_H1.finditer(s):
        add(m, date(int(m.group(1)), 6, 30), date(int(m.group(1)), 1, 1), 'duration', 6)
    for m in _P_CN_Q.finditer(s):
        y, q = int(m.group(1)), _Q_NUM[m.group(2)]
        add(m, _eom(y, q * 3), date(y, q * 3 - 2, 1), 'duration', 3)
    for m in _P_CN_INST.finditer(s):
        add(m, _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3))), None, 'instant', None)
    for m in _P_EN_INST.finditer(s):
        add(m, _en_date(m), None, 'instant', None)
    toks.sort(key=lambda t: t.pos)
    return toks


def _relative_labels(text: str) -> list[tuple[str, str]]:
    return [('current' if m.group('cur') else 'prior', m.group()) for m in _REL_RE.finditer(_nfkc(text))]


def _doc_anchor(text: str) -> _PTok | None:
    """Reporting period of the document from its title text (annual / interim / quarterly report)."""
    s = unicodedata.normalize('NFKC', text)
    patterns = (
        (r'(\d{4})\s*年\s*(?:年度报告|年报)', 12), (r'(\d{4})\s*年\s*(?:半年度报告|中期报告|半年报)', 6),
        (r'(\d{4})\s*年\s*第一季度报告', 3), (r'(\d{4})\s*年\s*第三季度报告', 9),
        (r'(?:annual report|annual results)\D{0,12}(\d{4})', 12), (r'(\d{4})\s+annual (?:report|results)', 12),
        (r'(?:interim report|interim results)\D{0,12}(\d{4})', 6), (r'(\d{4})\s+interim (?:report|results)', 6))
    best: tuple[int, _PTok] | None = None
    for pattern, months in patterns:
        m = re.search(pattern, s, re.I)
        if m and (best is None or m.start() < best[0]):
            end = _eom(int(m.group(1)), months)
            best = (m.start(), _PTok(m.start(), m.group().strip(), end, date(end.year, 1, 1), 'duration', months))
    return None if best is None else best[1]


def _fiscal_label(end: date, months: int | None) -> str:
    if months == 12:
        return f'FY{end.year}' if end.month == 12 else f'12M ended {end.isoformat()}'
    if months == 6 and end.month == 6:
        return f'H1 {end.year}'
    if months == 9 and end.month == 9:
        return f'9M {end.year} (Q1-Q3 cumulative)'
    if months == 3:
        return f'Q{(end.month - 1) // 3 + 1} {end.year} (3 months)'
    return f'{months}M ended {end.isoformat()}' if months else f'period ended {end.isoformat()}'


def _column(t: _PTok, label: str | None, kind: str, basis: str) -> dict[str, Any]:
    col: dict[str, Any] = {'label': label or t.label, 'period_end': t.end.isoformat(), 'basis': basis}
    if t.kind == 'instant' or kind == 'balance':
        col.update({'period_type': 'instant', 'fiscal_label': f'as at {t.end.isoformat()}'})
        return col
    col['period_type'] = 'duration'
    if t.months:
        col['months'] = t.months
    if t.start is not None:
        col['period_start'] = t.start.isoformat()
    col['fiscal_label'] = _fiscal_label(t.end, t.months)
    if t.months in (3, 6, 9) and t.start == date(t.end.year, 1, 1):
        col['cumulative_ytd'] = True
    return col


def _resolve_columns(kind: str, zone_texts: list[str], title_text: str, k: int, doc_anchor: _PTok | None
                     ) -> tuple[list[dict[str, Any]], list[str]]:
    """Period columns (exactly ``k``) from the header lines below the title, with warnings."""
    warnings: list[str] = []
    explicit: list[_PTok] = []
    relative: list[tuple[str, str]] = []
    bare_years: list[int] = []
    for text in zone_texts:
        explicit.extend(_period_tokens(text))
        relative.extend(_relative_labels(text))
        cells = [c.strip() for c in re.split(r'\s{2,}', _nfkc(text)) if c.strip()]
        years = [int(c) for c in cells if _bare_year(c)]
        if len(years) >= 2 and len(years) >= len(cells) - 2:
            bare_years.extend(years)
    title_tokens = _period_tokens(title_text)
    columns: list[dict[str, Any]] = []
    if len(explicit) >= 2 and not (bare_years and len(explicit) < len(bare_years)):
        columns = [_column(t, None, kind, 'header_explicit') for t in explicit[:k]]
        if len(relative) == len(columns):
            for col, (_, lab) in zip(columns, relative):
                col['label'] = f"{lab} ({col['label']})"
    elif len(bare_years) >= 2:
        template = next(iter(explicit or title_tokens), None) or doc_anchor
        for y in bare_years[:k]:
            if template is None:
                columns.append({'label': str(y), 'period_type': 'unknown', 'basis': 'bare_year_unresolved'})
                warnings.append('period_unresolved')
                continue
            end = _safe_date(y, template.end.month, template.end.day) or _eom(y, template.end.month)
            start = _month_start(end, template.months) if template.months else None
            columns.append(_column(_PTok(0, str(y), end, start, template.kind, template.months), str(y), kind,
                                   'header_year_with_period_date'))
    elif relative:
        anchor = next(iter(explicit or title_tokens), None)
        basis = 'header_date'
        if anchor is None and doc_anchor is not None:
            anchor, basis = doc_anchor, 'document_title'
        if anchor is None:
            columns = [{'label': lab, 'period_type': 'unknown', 'basis': 'relative_label_unresolved'} for _, lab in relative[:k]]
            warnings.append('period_unresolved')
        else:
            months = anchor.months or (anchor.end.month if anchor.kind == 'instant' else None)
            for role, lab in relative[:k]:
                if role == 'current':
                    end = anchor.end
                elif _PRIOR_YEAR_END.search(lab):
                    end = date(anchor.end.year - 1, 12, 31)
                else:
                    end = _safe_date(anchor.end.year - 1, anchor.end.month, anchor.end.day) or _eom(anchor.end.year - 1, anchor.end.month)
                start = _month_start(end, months) if (months and kind != 'balance') else None
                columns.append(_column(_PTok(0, lab, end, start, 'instant' if kind == 'balance' else 'duration', months),
                                       lab, kind, f'relative_label+{basis}'))
            if basis == 'document_title' or anchor.kind == 'instant':
                warnings.append('period_derived_from_report_date')
    elif len(explicit) == 1:
        columns = [_column(explicit[0], None, kind, 'header_explicit')]
    while len(columns) < k:
        columns.append({'label': None, 'period_type': 'unknown', 'basis': 'unlabelled'})
        warnings.append('column_unlabelled')
    return columns[:k], sorted(set(warnings))


# --------------------------------------------------------------------------------------------------------------
# Pages, candidates and groups
# --------------------------------------------------------------------------------------------------------------

@dataclass
class _Page:
    number: int
    start: int
    end: int
    status: str            # text | ocr | needs_ocr | other
    lines: list[tuple[int, str]] = field(default_factory=list)


def _split_lines(text: str, start: int, end: int) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    pos = start
    for line in text[start:end].split('\n'):
        out.append((pos, line))
        pos += len(line) + 1
    return out


def _build_pages(text: str, document_pages: Mapping[str, Any] | None) -> tuple[list[_Page], str]:
    pages: list[_Page] = []
    if document_pages is not None and isinstance(document_pages.get('pages'), list) and document_pages['pages']:
        for number, entry in enumerate(document_pages['pages'], 1):
            try:
                start, end, status = entry[0], entry[1], entry[2]
            except (TypeError, IndexError):
                pages.append(_Page(number, 0, 0, 'other'))
                continue
            if status in ('text', 'ocr') and type(start) is int and type(end) is int and 0 <= start <= end <= len(text):
                pages.append(_Page(number, start, end, status, _split_lines(text, start, end)))
            else:
                pages.append(_Page(number, start if type(start) is int else 0, end if type(end) is int else 0,
                                   'needs_ocr' if status == 'needs_ocr' else 'other'))
        return pages, str(document_pages.get('method') or 'pdf_pages')
    if '\f' in text:
        pos = 0
        for number, piece in enumerate(text.split('\f'), 1):
            if number > SYNTHETIC_MAX_BLOCKS:
                break
            pages.append(_Page(number, pos, pos + len(piece), 'text', _split_lines(text, pos, pos + len(piece))))
            pos += len(piece) + 1
        return pages, 'synthetic_form_feed'
    block: list[tuple[int, str]] = []
    size = 0
    for abs_start, line in _split_lines(text, 0, len(text)):
        block.append((abs_start, line))
        size += len(line) + 1
        if size >= SYNTHETIC_BLOCK_CHARS and len(pages) < SYNTHETIC_MAX_BLOCKS:
            pages.append(_Page(len(pages) + 1, block[0][0], block[-1][0] + len(block[-1][1]), 'text', block))
            block, size = [], 0
    if block:
        pages.append(_Page(len(pages) + 1, block[0][0], block[-1][0] + len(block[-1][1]), 'text', block))
    return pages, 'synthetic_blocks'


@dataclass
class _Segment:
    page: int
    lo: int
    hi: int


@dataclass
class _Group:
    rows: list[_Row]
    columns: list[dict[str, Any]]
    unit: _Unit
    warnings: list[str]
    terminal: bool
    k: int


@dataclass
class _Candidate:
    kind: str
    scope: str
    title: str
    segments: list[_Segment]
    terminal: bool = False
    matched: int = 0
    group: _Group | None = None

    @property
    def pages(self) -> list[int]:
        return sorted({s.page for s in self.segments})


_TERMINAL = {'balance': {'total_liabilities_and_equity'}, 'income': {'eps_basic', 'eps_diluted', 'total_comprehensive_income'},
             'cash_flow': {'cash_end_of_period'}}


def _anchors(rows: list[_Row], k: int) -> list[float] | None:
    full = [r for r in rows if len(r.toks) == k]
    if len(full) < 2 or k < 2:
        return None
    return [float(median([r.toks[i].end for r in full])) for i in range(k)]


def _decide_k(rows: list[_Row], n_header: int) -> int:
    counts = [len(r.toks) for r in rows]
    multi = [c for c in counts if c >= 2]
    k_rows = min(multi) if multi else (min(counts) if counts else 1)
    return n_header if n_header >= 2 else max(n_header, k_rows, 1)


def _build_group(pages: dict[int, _Page], segments: list[_Segment], kind: str, title_text: str,
                 doc_anchor: _PTok | None) -> _Group:
    all_rows: list[_Row] = []
    zone_texts: list[str] = []
    unit_lines: list[str] = [title_text]
    for si, seg in enumerate(segments):
        page = pages[seg.page]
        rows, zone = _merge_multiline(_classify_lines(seg.page, page.lines[seg.lo:seg.hi]), kind)
        if si == 0:
            zone_texts = [it.text for it in zone[1:]]       # zone[0] is the title line itself
            unit_lines += zone_texts + [t for _, t in page.lines[max(0, seg.lo - 8):seg.lo]] + [t for _, t in page.lines[:min(seg.lo, 30)]]
            zone_texts += [t for _, t in page.lines[max(0, seg.lo - 3):seg.lo] if _period_tokens(t) or _relative_labels(t)]
        all_rows.extend(rows)
    n_header = sum(1 for c in _resolve_columns(kind, zone_texts, title_text, 99, doc_anchor)[0] if c.get('basis') != 'unlabelled')
    k = _decide_k(all_rows, n_header)
    columns, warnings = _resolve_columns(kind, zone_texts, title_text, k, doc_anchor)
    unit = _detect_unit(unit_lines)
    if n_header == 0:
        warnings.append('columns_unlabelled')
    if unit.basis == 'assumed':
        warnings.append('unit_not_found')
    elif unit.basis == 'currency_only':
        warnings.append('unit_scale_not_stated')
    if unit.conflict:
        warnings.append('unit_conflict')
    keys = {std.key for r in all_rows if (std := match_standard_line(kind, r.label, r.heading)) is not None}
    return _Group(all_rows, columns, unit, sorted(set(warnings)), bool(keys & _TERMINAL[kind]), k)


def _find_candidates(pages: list[_Page]) -> list[_Candidate]:
    cands: list[_Candidate] = []
    for page in pages:
        if page.status not in ('text', 'ocr'):
            continue
        marks = [(idx, t) for idx, (_, text) in enumerate(page.lines) if (t := _parse_title(text)) is not None]
        for pos, (idx, title) in enumerate(marks):
            if title.kind == 'other':
                continue
            hi = marks[pos + 1][0] if pos + 1 < len(marks) else len(page.lines)
            seg = _Segment(page.number, idx, hi)
            prev = next((c for c in reversed(cands) if c.kind == title.kind and c.scope == title.scope
                         and c.segments[-1].page == page.number - 1), None) if title.continued else None
            if prev is not None:
                prev.segments.append(seg)
            else:
                cands.append(_Candidate(title.kind, title.scope, title.text, [seg]))
    return cands


def _head_region(page: _Page) -> _Segment | None:
    """Lines of a page before its first title (any statement or stop heading): continuation material."""
    hi = next((i for i, (_, text) in enumerate(page.lines) if _parse_title(text) is not None), len(page.lines))
    return _Segment(page.number, 0, hi) if hi > 0 else None


# --------------------------------------------------------------------------------------------------------------
# Request / main entry
# --------------------------------------------------------------------------------------------------------------

class ExtractStatementsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_id: str = Field(min_length=1, description='Authorized source_id of an annual / interim report (PDF with page map, or text).')
    statements: list[Literal['income', 'balance', 'cash_flow']] | None = Field(
        default=None, min_length=1, max_length=3,
        description='Statements to extract; default: those holding the requested lines, else all three.')
    lines: list[str] | None = Field(
        default=None, min_length=1, max_length=MAX_LINES,
        description='Standard line names to return (for example revenue, operating_profit, net_profit_attributable_parent, '
                    'total_assets, total_liabilities, operating_cash_flow, capex); default: every recognised line plus raw rows.')
    rationale: str = Field(min_length=1)

    @field_validator('statements')
    @classmethod
    def unique_statements(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else list(dict.fromkeys(value))

    @field_validator('lines')
    @classmethod
    def clean_lines(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = list(dict.fromkeys(v.strip().lower() for v in value if v.strip()))
        if not cleaned:
            raise ValueError('lines must contain at least one standard line name.')
        return cleaned


def _place_values(row: _Row, k: int, anchors: list[float] | None) -> tuple[list[_Tok | None], list[str], str | None]:
    """Period-column tokens of a row (note references removed) with warnings."""
    toks, warns, note, n = list(row.toks), [], row.note, len(row.toks)
    if n > k:
        surplus, toks = toks[:n - k], toks[n - k:]
        if all(_NOTE_RE.match(_nfkc(t.text)) and not _amount_like(t.text) for t in surplus):
            note = (note + ' ' if note else '') + ' '.join(t.text for t in surplus)
        else:
            warns.append('extra_leading_tokens')
        return list(toks), warns, note
    if n == k:
        return toks, warns, note
    placed: list[_Tok | None] = [None] * k
    if anchors is not None:
        assigned: list[int] = []
        for t in toks:
            dist = [abs(t.end - a) for a in anchors]
            j = min(range(k), key=lambda i: dist[i])
            second = sorted(dist)[1]
            if dist[j] > 10 or second - dist[j] < 3 or (assigned and j <= assigned[-1]):
                assigned = []
                break
            assigned.append(j)
        if len(assigned) == n:
            for t, j in zip(toks, assigned):
                placed[j] = t
            return placed, ['blank_column'], note
    for t, j in zip(toks, range(k - n, k)):
        placed[j] = t
    return placed, ['column_ambiguity'], note


def _ocr_fields(document_pages: Mapping[str, Any] | None, page: int,
                needs_check: Callable[[Mapping[str, Any], int], bool] | None) -> dict[str, Any]:
    if document_pages is None:
        return {}
    ocr = document_pages.get('ocr')
    meta = ocr.get(str(page)) if isinstance(ocr, Mapping) else None
    meta = meta if isinstance(meta, Mapping) else {}
    confidence, low = meta.get('mean_confidence'), meta.get('low_confidence_count')
    check = (needs_check(document_pages, page) if needs_check is not None else
             ((isinstance(confidence, (int, float)) and confidence < LOW_OCR_CONFIDENCE) or (type(low) is int and low > 0)))
    out: dict[str, Any] = {}
    if isinstance(confidence, (int, float)):
        out['ocr_mean_confidence'] = float(confidence)
    if meta.get('engine'):
        out['ocr_engine'] = meta['engine']
    if check:
        out['ocr_needs_check'] = True
    return out


def extract_statements(req: ExtractStatementsRequest, source: Any, *, document_pages: Mapping[str, Any] | None,
                       read_ranges: list[dict[str, Any]], read_budget_chars: int,
                       result_budget_chars: int = MAX_RESULT_CHARS,
                       needs_check: Callable[[Mapping[str, Any], int], bool] | None = None,
                       ocr_available: bool | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Locate, read (into ``read_ranges``) and parse statements of an already authorized ``source``."""
    text: str = source.text
    source_id: str = source.source_id
    unknown_lines = [l for l in (req.lines or []) if l not in STANDARD_LINE_BY_KEY]
    wanted_lines = [l for l in (req.lines or []) if l in STANDARD_LINE_BY_KEY]
    if req.lines is not None and not wanted_lines:
        return ({'status': 'invalid', 'error': 'unknown_lines', 'source_id': source_id, 'unknown_lines': unknown_lines,
                 'reason': 'None of the requested lines is a standard line name.',
                 'standard_lines': sorted(STANDARD_LINE_BY_KEY)}, None)
    if req.statements is not None:
        kinds = list(req.statements)
    elif wanted_lines:
        kinds = [k for k in STATEMENT_KINDS if any(STANDARD_LINE_BY_KEY[l].statement == k for l in wanted_lines)]
    else:
        kinds = list(STATEMENT_KINDS)

    pages, page_basis = _build_pages(text, document_pages)
    by_number = {p.number: p for p in pages}
    needs_ocr_pages = [p.number for p in pages if p.status == 'needs_ocr']
    doc_anchor = _doc_anchor(text[:30_000])

    def evaluate(cand: _Candidate) -> None:
        while True:
            cand.group = _build_group(by_number, cand.segments, cand.kind, cand.title, doc_anchor)
            cand.terminal = cand.group.terminal
            last = cand.segments[-1]
            nxt = by_number.get(last.page + 1)
            if (cand.terminal or len(cand.pages) > MAX_CONTINUATION_PAGES or nxt is None or nxt.status not in ('text', 'ocr')
                    or last.hi < len(by_number[last.page].lines)):
                break
            head = _head_region(nxt)
            if head is None or len(_merge_multiline(_classify_lines(nxt.number, nxt.lines[head.lo:head.hi]), cand.kind)[0]) < 3:
                break
            cand.segments.append(head)
        cand.matched = sum(1 for r in cand.group.rows if match_standard_line(cand.kind, r.label, r.heading) is not None)

    accepted: list[_Candidate] = []
    for cand in _find_candidates(pages):
        if cand.kind not in kinds:
            continue
        evaluate(cand)
        # A repeated title without a continuation marker on the page after an unfinished statement continues it.
        prev = next((c for c in reversed(accepted) if c.kind == cand.kind and c.scope == cand.scope
                     and c.pages[-1] == cand.pages[0] - 1 and not c.terminal), None)
        if prev is not None and len(prev.pages) + len(cand.pages) <= MAX_CONTINUATION_PAGES + 1:
            prev.segments.extend(cand.segments)
            evaluate(prev)
            continue
        if len(cand.group.rows) >= MIN_DATA_ROWS:
            accepted.append(cand)
    chosen: dict[str, _Candidate] = {}
    alternatives: dict[str, list[dict[str, Any]]] = {}
    for kind in kinds:
        ranked = sorted((c for c in accepted if c.kind == kind and len(c.group.rows) >= MIN_DATA_ROWS),
                        key=lambda c: (_SCOPE_RANK[c.scope], -c.matched, c.pages[0]))
        if ranked:
            chosen[kind] = ranked[0]
            alternatives[kind] = [{'scope': c.scope, 'pages': c.pages, 'title': c.title[:80]} for c in ranked[1:4]]

    # ---- read accounting: whole pages of the chosen statements, within the per-turn budget
    read_pages: set[int] = set()
    unread: list[dict[str, Any]] = []
    spent = 0
    for kind in kinds:
        cand = chosen.get(kind)
        if cand is None:
            continue
        need = [n for n in cand.pages if n not in read_pages]
        cost = sum(by_number[n].end - by_number[n].start + 1 for n in need)
        if spent + cost <= read_budget_chars:
            read_pages.update(need)
            spent += cost
        else:
            unread.append({'statement': kind, 'pages': cand.pages})
    read_spans: list[dict[str, Any]] = []
    run: list[int] = []
    for n in [*sorted(read_pages), None]:
        if run and (n is None or n != run[-1] + 1):
            span = {'source_id': source_id, 'start': by_number[run[0]].start, 'end': by_number[run[-1]].end}
            if span['end'] > span['start']:
                read_spans.append(span)
                if span not in read_ranges:
                    read_ranges.append(span)
            run = []
        if n is not None:
            run.append(n)

    # ---- rows
    unread_kinds = {u['statement'] for u in unread}
    statements_out: list[dict[str, Any]] = []
    needs_ocr_for: list[int] = []
    for kind in kinds:
        cand = chosen.get(kind)
        if cand is None or kind in unread_kinds:
            continue
        group = cand.group
        assert group is not None
        warnings = list(group.warnings)
        if cand.scope == 'parent_company':
            warnings.append('only_parent_company_statement_found')
        if not cand.terminal:
            warnings.append('statement_end_not_found')
            nxt = by_number.get(cand.segments[-1].page + 1)
            if nxt is not None and nxt.status == 'needs_ocr':
                needs_ocr_for.append(nxt.number)
        if any(by_number[p].status == 'ocr' for p in cand.pages):
            warnings.append('statement_pages_from_ocr')
        anchors = {pg: _anchors([r for r in group.rows if r.page == pg], group.k) for pg in {r.page for r in group.rows}}
        rows_out: list[dict[str, Any]] = []
        seen_keys: dict[str, int] = {}
        for row in group.rows:
            std = match_standard_line(kind, row.label, row.heading)
            placed, row_warns, note = _place_values(row, group.k, anchors.get(row.page))
            entry: dict[str, Any] = {'standard_line': std.key if std else None, 'label': row.label[:200]}
            if std is not None:
                seen_keys[std.key] = seen_keys.get(std.key, 0) + 1
                entry['primary'] = seen_keys[std.key] == 1
                if not entry['primary']:
                    row_warns.append('duplicate_standard_line')
            values: list[str | None] = []
            stated: list[str | None] = []
            nil_cols: list[int] = []
            per_share = False
            for idx, tok in enumerate(placed):
                dec = None if tok is None or tok.nil else _to_decimal(tok.text)
                if tok is not None and tok.nil:
                    nil_cols.append(idx)
                elif tok is not None and dec is None:
                    row_warns.append('unparsed_token')
                if dec is None:
                    values.append(None)
                    stated.append(None)
                    continue
                scaled, ps = _scale_value(dec[0], group.unit, std, row.label)
                per_share = per_share or ps
                values.append(_fmt(scaled))
                stated.append(dec[1])
            entry['value'] = values
            entry['value_as_stated'] = stated
            if nil_cols:
                entry['nil_columns'] = nil_cols
            if per_share:
                entry['per_share'] = True
            if note:
                entry['note_ref'] = note[:40]
            entry['page'] = row.page
            quote = text[row.abs_start:row.abs_end]
            if 0 < len(quote) <= MAX_QUOTATION_CHARS:
                entry.update({'quotation': quote, 'character_start': row.abs_start, 'character_end': row.abs_end})
            else:
                row_warns.append('quotation_unavailable')
            page_obj = by_number[row.page]
            entry['extraction_method'] = 'ocr' if page_obj.status == 'ocr' else 'document_table'
            if page_obj.status == 'ocr':
                entry.update(_ocr_fields(document_pages, row.page, needs_check))
                if entry.get('ocr_needs_check'):
                    row_warns.append('ocr_low_confidence')
            if row.multi_line:
                row_warns.append('label_spans_lines')
            if row_warns:
                entry['warnings'] = sorted(set(row_warns))
            rows_out.append(entry)
        statements_out.append({
            'statement': kind, 'scope': cand.scope, 'title': cand.title[:120], 'pages': cand.pages,
            'unit': {'scale': group.unit.scale, 'scale_label': _scale_label(group.unit), 'stated_as': group.unit.label,
                     'currency': group.unit.currency, 'basis': group.unit.basis,
                     **({'text': group.unit.text} if group.unit.text else {})},
            'columns': group.columns, 'terminal_reached': cand.terminal, 'rows': rows_out,
            **({'alternatives': alternatives[kind]} if alternatives.get(kind) else {}),
            **({'warnings': sorted(set(warnings))} if warnings else {}),
        })

    # ---- line filter
    for st in statements_out:
        all_rows = st['rows']
        matched = [r for r in all_rows if r['standard_line']]
        if req.lines is not None:
            keep = [r for r in matched if r['standard_line'] in wanted_lines]
        else:
            keep = matched + [r for r in all_rows if not r['standard_line']][:MAX_UNMATCHED_ROWS_PER_STATEMENT]
        st['rows_total'], st['rows_omitted'], st['rows'] = len(all_rows), len(all_rows) - len(keep), keep
    found_keys = {r['standard_line'] for st in statements_out for r in st['rows']}
    missing_lines = [l for l in wanted_lines if l not in found_keys] if wanted_lines and not unread else []
    missing = [k for k in kinds if k not in chosen]

    result: dict[str, Any] = {'source_id': source_id, 'page_basis': page_basis, 'page_count': len(pages),
                              'statements': statements_out, 'read_spans': read_spans, 'pages_read': sorted(read_pages)}
    if unknown_lines:
        result['unknown_lines'] = unknown_lines
    if missing_lines:
        result['lines_not_found'] = missing_lines
    if missing:
        result['statements_not_found'] = missing
    messages: list[str] = []
    if unread:
        result.update({'unread_statements': unread, 'truncated_by_limit': True,
                       'next_pages': sorted({n for u in unread for n in u['pages']})})
        messages.append('The per-turn read limit was reached: the statements in unread_statements were located but not read. '
                        f"Call extract_statements again in the next turn for them (pages {result['next_pages']}).")
    ocr_targets: list[int] = sorted(set(needs_ocr_for))[:MAX_OCR_SUGGESTED_PAGES]
    if not ocr_targets and missing and needs_ocr_pages:
        near = sorted({n for st in statements_out for p in st['pages'] for n in (p - 2, p - 1, p + 1, p + 2) if n in needs_ocr_pages})
        ocr_targets = (near or needs_ocr_pages)[:MAX_OCR_SUGGESTED_PAGES]
    if ocr_targets:
        result['needs_ocr_pages'] = ocr_targets
        if ocr_available is False:
            messages.append('Some pages that may hold statements are image-only and OCR is not available in this version.')
        else:
            result['suggested_actions'] = [{'tool': 'request_ocr', 'arguments': {'source_id': source_id, 'pages': ocr_targets}}]
            messages.append(f'Pages {ocr_targets} are image-only; call request_ocr with them, then extract_statements again on the '
                            'new source_id that request_ocr returns.')
    if not statements_out and not unread:
        result['status'] = 'needs_ocr' if ocr_targets else 'unavailable'
        if not ocr_targets:
            result['reason'] = 'statements_not_found'
            messages.append('No statement title with a numeric table was found. Use search_library for the statement titles to find '
                            'the pages, then read_pages; the statements may be in a separate financial-report document.')
    else:
        result['status'] = 'partial' if (missing or unread or missing_lines or needs_ocr_for) else 'completed'
        if missing and statements_out:
            messages.append(f'Not found: {missing}. Search their titles with search_library or check another document.')
    if any('only_parent_company_statement_found' in st.get('warnings', []) for st in statements_out):
        messages.append('Only a parent-company statement was found for some statements; do not present it as consolidated.')
    if messages:
        result['message'] = ' '.join(messages)
    if statements_out:
        result['instructions'] = ('Rows are NOT recorded. To cite a value call record_facts with the row quotation and '
                                  'character_start/character_end, value_as_stated as value, the statement unit scale_label as unit '
                                  '(per_share rows: the currency per share) and the currency; check warnings first.')
    _fit_result(result, max(2_000, result_budget_chars))
    summary = {'kind': 'extract_statements', 'source_id': source_id,
               'statements': [{'statement': st['statement'], 'scope': st['scope'], 'pages': st['pages'][:6], 'rows': len(st['rows'])}
                              for st in statements_out][:3],
               'pages_read': sorted(read_pages)[:20]}
    return result, summary


def _fit_result(result: dict[str, Any], limit: int) -> None:
    """Drop lowest-priority rows (unmatched, then duplicate standard lines, then the tail) until the JSON fits ``limit``."""
    def size() -> int:
        return len(json.dumps(result, ensure_ascii=False, separators=(',', ':')))

    trimmed = 0
    for tier in ('unmatched', 'duplicate', 'tail'):
        for st in result['statements']:
            while st['rows'] and size() > limit:
                rows = st['rows']
                if tier == 'unmatched':
                    idx = next((i for i in range(len(rows) - 1, -1, -1) if not rows[i]['standard_line']), None)
                elif tier == 'duplicate':
                    idx = next((i for i in range(len(rows) - 1, -1, -1) if rows[i].get('primary') is False), None)
                else:
                    idx = len(rows) - 1
                if idx is None:
                    break
                rows.pop(idx)
                st['rows_omitted'] = st.get('rows_omitted', 0) + 1
                trimmed += 1
    if trimmed:
        result['rows_trimmed_for_size'] = trimmed
