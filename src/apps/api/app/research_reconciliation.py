"""Multi-source reconciliation of recorded research facts (innovation I1).

Deterministic and model-free. Given the facts a run recorded (plus the metadata of the sources they were
read from) this module groups facts that measure the *same thing* -- same metric, same period, same entity,
same currency/unit family -- and compares their values with per-metric tolerance rules. Nothing here ever
corrects a value: the output only shows where sources disagree, which source class each value came from and
which value is the preferred reference (exchange data / filings beat news), so the research agent, the
report and the UI can surface the disagreement.

Design rules (see ``METRIC_RULES`` / ``TOLERANCES`` below for the tables):

* Merge only when sure. A fact without a recognised metric, an exact period, an entity hint (ticker or
  name) or, for money-like metrics, a currency is *not* merged with anything; it is counted in
  ``stats['unmerged']`` by reason. Different qualifiers (after-hours, estimate, diluted, ...) and
  different currencies never merge; values are never converted across currencies.
* Scale normalisation (thousand/million/billion, 万/亿/万亿, % vs ratio vs bps) is applied before comparing.
* ``rounding_only`` is credited only when the coarser value is exactly the finer value rounded half-up to
  the coarser value's stated precision (114.99 vs 115.099998 is a real difference; 645.04 vs 645.039978 is
  rounding).
* ``differs`` means "not explained by rounding". ``within_tolerance`` / ``severity`` ('minor' | 'material')
  grade the gap with the per-metric tolerance, so a 0.1% price gap is still reported but ranked below a
  material one.
* Syndicated copies count once: ``cluster_of(source) -> cluster_id | None`` (provided by the news module)
  collapses sources into one independent source; without it every distinct ``source_id`` is independent.

Inputs are plain mappings (JSON shaped, e.g. ``ResearchFact.model_dump()`` / ``ResearchSource.model_dump()``
or the run exports); objects with the same attributes also work.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
import hashlib
import re
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse

VERSION = 1
LOW_OCR_CONFIDENCE = 70.0

STATUS_CONSISTENT = "consistent"
STATUS_ROUNDING = "rounding_only"
STATUS_DIFFERS = "differs"
_STATUS_RANK = {STATUS_CONSISTENT: 0, STATUS_ROUNDING: 1, STATUS_DIFFERS: 2}

SOURCE_CLASSES = ("market_data", "filing", "issuer", "news", "user_upload", "other")

# --------------------------------------------------------------------------------------------------
# Rule tables
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricRule:
    key: str
    pattern: str
    kind: str                       # price | per_share | count | money
    primary_order: tuple[str, ...]  # source classes that may serve as the reference, best first
    zh: str
    en: str
    timeless: bool = False          # one value per entity regardless of period (IPO price, shares offered)


_MKT = ("market_data", "filing", "issuer")
_FIL = ("filing", "issuer", "market_data")

# Order matters: a matched span is blanked before later rules run (so "previous close" is not also "close",
# "net income" is not also "income"). More than one distinct metric in a label => ambiguous => no merge.
METRIC_RULES: tuple[MetricRule, ...] = (
    MetricRule("prev_close", r"前收盘?|昨收|previous close|prior close|prev\.? ?close", "price", _MKT, "前收盘价", "Previous close"),
    MetricRule("ipo_price", r"发行价|发售价|ipo ?price|offer(?:ing)? price|issue price|ipo定价|定价为", "price", _FIL, "发行价", "IPO price", True),
    MetricRule("close", r"收盘|收市|\bclos(?:e|ing)\b", "price", _MKT, "收盘价", "Close"),
    MetricRule("open", r"开盘|首笔|first[- ]trade|opening (?:price|trade|print)|\bopen(?: price)?\b", "price", _MKT, "开盘价", "Open"),
    MetricRule("high", r"最高|\bhigh\b", "price", _MKT, "最高价", "High"),
    MetricRule("low", r"最低|\blow\b", "price", _MKT, "最低价", "Low"),
    MetricRule("turnover", r"成交额|成交金额|turnover", "money", _MKT, "成交额", "Turnover"),
    MetricRule("volume", r"成交量|交易量|成交股数|trading volume|shares traded|traded shares|\bvolume\b", "count", _MKT, "成交量", "Volume"),
    MetricRule("shares_offered", r"发行股数|发行股份|发售股数|shares offered|shares sold|shares in (?:the )?offering|offering size", "count", _FIL, "发行股数", "Shares offered", True),
    MetricRule("proceeds", r"募资|募集|筹资|proceeds|raised|gross proceeds", "money", _FIL, "募资额", "Proceeds", True),
    # Valuation multiples & return ratios (matched before underlying components)
    MetricRule("ev_ebitda", r"\bev\s*/\s*ebitda\b|\bev_ebitda\b|\bev\s+ebitda\b|enterprise\s+value\s*(?:/|to)\s*ebitda|企业价值倍数", "ratio", _FIL, "EV/EBITDA", "EV/EBITDA"),
    MetricRule("ev_sales", r"\bev\s*/\s*(?:sales|revenue)\b|\bev_sales\b|\bev\s+sales\b|enterprise\s+value\s*(?:/|to)\s*(?:sales|revenue)", "ratio", _FIL, "EV/Sales", "EV/Sales"),
    MetricRule("pe_ratio", r"\bp\s*/\s*e(?:\s+ratio)?\b|\bpe[- _]ratio\b|price[- ]to[- ]earnings(?:\s+ratio)?|市盈率", "ratio", _FIL, "市盈率", "P/E ratio"),
    MetricRule("pb_ratio", r"\bp\s*/\s*b(?:\s+ratio)?\b|\bpb[- _]ratio\b|price[- ]to[- ]book(?:\s+ratio)?|市净率", "ratio", _FIL, "市净率", "P/B ratio"),
    MetricRule("ps_ratio", r"\bp\s*/\s*s(?:\s+ratio)?\b|\bps[- _]ratio\b|price[- ]to[- ]sales(?:\s+ratio)?|市销率", "ratio", _FIL, "市销率", "P/S ratio"),
    MetricRule("debt_to_equity", r"debt[- ]to[- ]equity|\bdebt_to_equity\b|产权比率|债务股本比", "ratio", _FIL, "产权比率", "Debt to equity"),
    MetricRule("current_ratio", r"current\s+ratio|\bcurrent_ratio\b|流动比率", "ratio", _FIL, "流动比率", "Current ratio"),
    MetricRule("enterprise_value", r"enterprise\s+value|\benterprise_value\b|企业价值", "money", _FIL, "企业价值", "Enterprise value"),
    MetricRule("market_cap", r"市值|market cap(?:italization)?|market value|valuation|估值", "money", _FIL, "市值", "Market cap"),
    # Margins & percentage return/yield metrics
    MetricRule("gross_margin", r"毛利率|gross margin", "money", _FIL, "毛利率", "Gross margin"),
    MetricRule("net_margin", r"净利率|net margin", "money", _FIL, "净利率", "Net margin"),
    MetricRule("operating_margin", r"营业利润率|经营利润率|operating margin", "money", _FIL, "营业利润率", "Operating margin"),
    MetricRule("ebitda_margin", r"ebitda\s*margin|ebitda率|息税折旧摊销前利润率", "money", _FIL, "EBITDA利润率", "EBITDA margin"),
    MetricRule("fcf_margin", r"(?:free\s+cash\s+flow|fcf)\s+margin|自由现金流利润率", "money", _FIL, "自由现金流利润率", "FCF margin"),
    MetricRule("roe", r"\broe\b|return\s+on\s+equity|净资产收益率", "money", _FIL, "净资产收益率", "ROE"),
    MetricRule("roic", r"\broic\b|return\s+on\s+invested\s+capital|投入资本回报率", "money", _FIL, "投入资本回报率", "ROIC"),
    MetricRule("roa", r"\broa\b|return\s+on\s+assets|总资产收益率", "money", _FIL, "总资产收益率", "ROA"),
    MetricRule("dividend_yield", r"dividend\s+yield|\bdividend_yield\b|股息率", "money", _FIL, "股息率", "Dividend yield"),
    # Income statement lines (ordered so composite phrases blank before base words)
    MetricRule("cost_of_revenue", r"cost\s+of\s+(?:revenue|sales|goods\s+sold|services|goods\s+and\s+services)|\bcost_of_revenue\b|营业成本|主营业务成本|销售成本", "money", _FIL, "营业成本", "Cost of revenue"),
    MetricRule("rd_expense", r"research\s+and\s+development(?:\s+(?:expenses?|costs?|expenditures?))?|r\s*&\s*d\s+(?:expenses?|costs?)|\brd\s+expense\b|\brd_expense\b|研发费用|研发支出|研发开支", "money", _FIL, "研发费用", "R&D expense"),
    MetricRule("sga_expense", r"selling,?\s*general\s+and\s+administrative(?:\s+(?:expenses?|costs?))?|sg\s*&\s*a(?:\s+(?:expenses?|costs?))?|\bsga\s+expense\b|\bsga_expense\b|销售及管理费用", "money", _FIL, "销售及管理费用", "SG&A expense"),
    MetricRule("selling_expense", r"selling(?:\s+and\s+(?:marketing|distribution))?\s+(?:expenses?|costs?)|\bselling_expense\b|销售费用", "money", _FIL, "销售费用", "Selling expense"),
    MetricRule("admin_expense", r"(?:general\s+and\s+)?administrative\s+(?:expenses?|costs?)|\badmin\s+expense\b|\badmin_expense\b|管理费用", "money", _FIL, "管理费用", "Admin expense"),
    MetricRule("total_operating_costs", r"(?:total\s+)?operating\s+(?:costs|expenses)|\btotal_operating_costs\b|营业总成本|营业费用", "money", _FIL, "营业总成本", "Total operating costs"),
    MetricRule("ebitda", r"\bebitda\b|earnings\s+before\s+interest,?\s*taxes,?\s*depreciation\s+and\s+amortization|息税折旧摊销前利润", "money", _FIL, "EBITDA", "EBITDA"),
    MetricRule("interest_expense", r"interest\s+(?:expenses?|costs?)|\binterest_expense\b|利息费用|利息支出", "money", _FIL, "利息费用", "Interest expense"),
    MetricRule("finance_expense", r"finance\s+(?:expenses?|costs?)|\bfinance_expense\b|财务费用", "money", _FIL, "财务费用", "Finance expense"),
    MetricRule("depreciation_amortization", r"depreciation\s+(?:and|&)\s+amortization|depreciation\s+amortization|\bdepreciation_amortization\b|折旧(?:与|及)摊销|固定资产折旧", "money", _FIL, "折旧与摊销", "Depreciation & amortization"),
    MetricRule("profit_before_tax", r"(?:profit|income|earnings)\s+before\s+(?:income\s+)?tax(?:es|ation)?|\bprofit_before_tax\b|pre[- ]?tax\s+(?:income|profit)|利润总额|税前利润", "money", _FIL, "利润总额", "Profit before tax"),
    MetricRule("income_tax", r"income\s+tax(?:\s+(?:expenses?|benefit|provision))?|\bincome_tax\b|所得税费用|所得税", "money", _FIL, "所得税费用", "Income tax"),
    MetricRule("net_income", r"净利润|净亏损|归母净利润|归属于母公司(?:所有者的)?净利润|net income|net profit|net loss|net earnings|\bnet_income\b", "money", _FIL, "净利润", "Net income"),
    MetricRule("operating_income", r"营业利润|经营利润|营业收益|operating income|operating profit|operating loss|\boperating_income\b", "money", _FIL, "营业利润", "Operating income"),
    MetricRule("gross_profit", r"毛利润|毛利(?!率)|gross profit|\bgross_profit\b", "money", _FIL, "毛利", "Gross profit"),
    MetricRule("eps_basic", r"basic\s+(?:eps|earnings\s+per\s+share|loss\s+per\s+share)|\beps\s+basic\b|\beps_basic\b|基本每股收益", "per_share", _FIL, "基本每股收益", "Basic EPS"),
    MetricRule("eps_diluted", r"diluted\s+(?:eps|earnings\s+per\s+share|loss\s+per\s+share)|\beps\s+diluted\b|\beps_diluted\b|稀释每股收益", "per_share", _FIL, "稀释每股收益", "Diluted EPS"),
    MetricRule("eps", r"每股收益|每股盈利|\beps\b|earnings per share", "per_share", _FIL, "每股收益", "EPS"),
    MetricRule("shares_basic", r"(?:weighted\s+average\s+)?basic\s+shares(?:\s+outstanding)?|\bshares\s+basic\b|\bshares_basic\b|基本股数|加权平均基本股数", "count", _FIL, "基本股数", "Basic shares"),
    MetricRule("shares_diluted", r"(?:weighted\s+average\s+)?diluted\s+shares(?:\s+outstanding)?|\bshares\s+diluted\b|\bshares_diluted\b|稀释股数|稀释后股数|摊薄股数", "count", _FIL, "稀释股数", "Diluted shares"),
    # Cash-flow statement lines (ordered before `cash`)
    MetricRule("free_cash_flow", r"free\s+cash\s+flow|\bfree_cash_flow\b|\bfcf\b|自由现金流", "money", _FIL, "自由现金流", "Free cash flow"),
    MetricRule("operating_cash_flow", r"(?:net\s+)?(?:cash\s+(?:provided\s+by|from)\s+operating\s+activities|operating\s+cash\s+flow|\bcfo\b)|\boperating_cash_flow\b|经营活动(?:产生的)?现金流量净额|经营现金流", "money", _FIL, "经营现金流", "Operating cash flow"),
    MetricRule("investing_cash_flow", r"(?:net\s+)?(?:cash\s+(?:used\s+in|from)\s+investing\s+activities|investing\s+cash\s+flow)|\binvesting_cash_flow\b|投资活动(?:产生的)?现金流量净额|投资现金流", "money", _FIL, "投资现金流", "Investing cash flow"),
    MetricRule("financing_cash_flow", r"(?:net\s+)?(?:cash\s+(?:used\s+in|from)\s+financing\s+activities|financing\s+cash\s+flow)|\bfinancing_cash_flow\b|筹资活动(?:产生的)?现金流量净额|筹资现金流", "money", _FIL, "筹资现金流", "Financing cash flow"),
    MetricRule("net_increase_in_cash", r"net\s+(?:increase|decrease|change)\s+in\s+cash(?:\s+and\s+cash\s+equivalents)?|\bnet_increase_in_cash\b|现金及现金等价物净增加额", "money", _FIL, "现金净增加额", "Net increase in cash"),
    MetricRule("capex", r"capital\s+expenditures?|\bcapex\b|资本支出|资本开支|购建固定资产(?:、无形资产和其他长期资产)?支付的现金", "money", _FIL, "资本开支", "Capex"),
    MetricRule("dividends_paid", r"dividends?\s+paid|cash\s+dividends|\bdividends_paid\b|支付股利|现金分红|分红派息", "money", _FIL, "支付股利", "Dividends paid"),
    MetricRule("share_repurchases", r"share\s+repurchases?|stock\s+repurchases?|\bbuybacks?\b|\bshare_repurchases\b|股份回购|股票回购", "money", _FIL, "股份回购", "Share repurchases"),
    # Balance sheet lines
    MetricRule("short_term_investments", r"short[- ]?term\s+investments?|marketable\s+securities|\bshort_term_investments\b|短期投资|交易性金融资产", "money", _FIL, "短期投资", "Short-term investments"),
    MetricRule("cash", r"cash\s+and\s+(?:cash\s+)?equivalents|\bcash\b|货币资金|现金及现金等价物|现金余额", "money", _FIL, "货币资金", "Cash"),
    MetricRule("accounts_receivable", r"accounts?\s+receivable|trade\s+receivables?|\baccounts_receivable\b|应收账款|应收票据及应收账款", "money", _FIL, "应收账款", "Accounts receivable"),
    MetricRule("inventory", r"\binventor(?:y|ies)\b|存货", "money", _FIL, "存货", "Inventory"),
    MetricRule("current_assets", r"(?:total\s+)?current\s+assets|\bcurrent_assets\b|流动资产(?:合计|总计)?", "money", _FIL, "流动资产", "Current assets"),
    MetricRule("ppe_net", r"(?:net\s+)?property,?\s*plant,?\s*(?:and|&)\s*equipment|\bppe\s+net\b|\bppe_net\b|固定资产", "money", _FIL, "固定资产", "PP&E net"),
    MetricRule("goodwill", r"\bgoodwill\b|商誉", "money", _FIL, "商誉", "Goodwill"),
    MetricRule("intangible_assets", r"intangible\s+assets?|\bintangible_assets\b|无形资产", "money", _FIL, "无形资产", "Intangible assets"),
    MetricRule("total_assets", r"total\s+assets|\btotal_assets\b|资产总计|资产合计|总资产", "money", _FIL, "总资产", "Total assets"),
    MetricRule("accounts_payable", r"accounts?\s+payable|trade\s+payables?|\baccounts_payable\b|应付账款", "money", _FIL, "应付账款", "Accounts payable"),
    MetricRule("short_term_debt", r"short[- ]?term\s+(?:debt|borrowings?)|\bshort_term_debt\b|短期借款|短期债务", "money", _FIL, "短期债务", "Short-term debt"),
    MetricRule("long_term_debt", r"long[- ]?term\s+(?:debt|borrowings?)|\blong_term_debt\b|长期借款|长期债务|应付债券", "money", _FIL, "长期债务", "Long-term debt"),
    MetricRule("net_debt", r"net\s+debt|\bnet_debt\b|净债务|净负债", "money", _FIL, "净债务", "Net debt"),
    MetricRule("total_debt", r"total\s+debt|\btotal_debt\b|总债务|有息负债", "money", _FIL, "总债务", "Total debt"),
    MetricRule("current_liabilities", r"(?:total\s+)?current\s+liabilities|\bcurrent_liabilities\b|流动负债(?:合计|总计)?", "money", _FIL, "流动负债", "Current liabilities"),
    MetricRule("total_liabilities", r"total\s+liabilities|\btotal_liabilities\b|负债合计|负债总计|总负债", "money", _FIL, "总负债", "Total liabilities"),
    MetricRule("retained_earnings", r"retained\s+earnings|\bretained_earnings\b|未分配利润|留存收益", "money", _FIL, "未分配利润", "Retained earnings"),
    MetricRule("non_controlling_interests", r"non[- ]?controlling\s+interests?|minority\s+interests?|\bnon_controlling_interests\b|少数股东权益", "money", _FIL, "少数股东权益", "Non-controlling interests"),
    MetricRule("equity", r"(?:stockholders'?|shareholders'?|owners'?)\s+equity|total\s+equity|\bequity\b|所有者权益(?:合计|总计)?|股东权益(?:合计|总计)?", "money", _FIL, "股东权益", "Equity"),
    MetricRule("revenue", r"营业收入|营收|总收入|销售收入|收入|revenues?|net sales|\bsales\b", "money", _FIL, "营收", "Revenue"),
)
_RULE_BY_KEY = {rule.key: rule for rule in METRIC_RULES}
_COMPILED_RULES = tuple((rule, re.compile(rule.pattern, re.I)) for rule in METRIC_RULES)
_MARGIN_KEYS = frozenset({"gross_margin", "net_margin", "operating_margin", "ebitda_margin", "fcf_margin"})
_PERCENT_METRIC_KEYS = _MARGIN_KEYS | frozenset({"roe", "roic", "roa", "dividend_yield"})

# Labels that turn a "high"/"low"/"open"-style label into a *range* statement, which is not a daily value.
_RANGE_SCOPE = re.compile(
    r"52[- ]?(?:week|wk|w)|52周|all[- ]time|历史|年内|年度|record|创纪录|近期高点|阶段|年初至今|\bytd\b|区间|since|自.*以来", re.I)
_CHANGE_WORDS = re.compile(r"涨幅|涨跌|跌幅|变动|回撤|增长|同比|环比|change|growth|gain|jump|surge|yoy|qoq|\bup\b|\bdown\b|rose|fell|%", re.I)

QUALIFIER_RULES: tuple[tuple[str, str], ...] = (
    ("after_hours", r"盘后|after[- ]?hours?|extended[- ]hours?|post[- ]?market"),
    ("pre_market", r"盘前|pre[- ]?market"),
    ("adjusted_price", r"复权|adjusted (?:close|price)|split[- ]adjusted"),
    ("diluted", r"稀释|diluted"),
    ("non_gaap", r"non[- ]?gaap|非\s?gaap|非公认"),
    ("estimate", r"预期|预测|预估|估计|forecast|estimate|guidance|consensus|expected|projected|目标价"),
    ("range_scope", _RANGE_SCOPE.pattern),
    ("run_rate", r"run[- ]?rate|年化|annuali[sz]ed"),
)
_COMPILED_QUALIFIERS = tuple((key, re.compile(pat, re.I)) for key, pat in QUALIFIER_RULES)

# (relative tolerance, absolute tolerance) per value kind. Rounding is judged separately and first.
# abs applies to percent (percentage points) and per_share; rel to the others.
TOLERANCES: dict[str, dict[str, Decimal]] = {
    "price": {"rel": Decimal("0.005")},        # 0.5%
    "per_share": {"rel": Decimal("0.01")},
    "count": {"rel": Decimal("0.01")},         # 1% (consolidated vs primary-venue volume differ legitimately)
    "money": {"rel": Decimal("0.01")},
    "ratio": {"rel": Decimal("0.01")},
    "percent": {"abs": Decimal("0.1")},        # 0.1 percentage point
}

PRIMARY_HOSTS = ("sec.gov", "hkexnews.hk", "cninfo.com.cn", "sse.com.cn", "szse.cn", "edinet-fsa.go.jp",
                 "esma.europa.eu", "sedarplus.ca", "dart.fss.or.kr", "federalreserve.gov", "bls.gov", "stats.gov.cn",
                 "pbc.gov.cn", "ecb.europa.eu", "imf.org", "worldbank.org", "nasdaq.com/market-activity")
_STOP_TICKERS = frozenset({
    "IPO", "USD", "EUR", "GBP", "JPY", "CNY", "RMB", "HKD", "KRW", "CAD", "ET", "EST", "EDT", "UTC", "EPS", "GAAP", "YOY",
    "QOQ", "FY", "TTM", "YTD", "AI", "US", "USA", "ETF", "NYSE", "NASDAQ", "SEC", "CEO", "CFO", "EBIT", "EBITDA", "PE",
    "PB", "PS", "EV", "ROE", "ROA", "ROIC", "FCF", "SGA", "RD", "PPE", "CAGR", "SMA", "RSI", "GDP", "CPI", "PPI", "PMI",
    "FOMC", "FED", "VWAP", "OHLC", "HK", "CN", "AM", "PM", "NA", "NM", "OK",
    "IPOS", "S1", "ADR", "NAV", "BPS", "PP", "ANN", "TBD", "IV",
})
_GENERIC_NAME_WORDS = frozenset({
    "The", "Total", "Net", "First", "Day", "Opening", "Closing", "Intraday", "Daily", "Trading", "Gross", "Full", "Last",
    "Latest", "Stock", "Share", "Shares", "Price", "Company", "Revenue", "Sales", "Market", "Initial", "Public", "Offering",
    "Quarter", "Annual", "Year", "Fiscal", "Reported", "Estimated", "Peak", "Final", "Low", "High", "Open", "Close", "Volume",
    "Free", "Operating", "Investing", "Financing", "Current", "Short", "Long", "Term", "Diluted", "Basic", "Capital",
    "Interest", "Income", "Cost", "Expenses", "Expense", "Cash", "Equity", "Assets", "Liabilities", "Debt", "Dividend",
    "Yield", "Ratio", "Enterprise", "Value", "Retained", "Earnings", "Goodwill", "Inventory", "Accounts", "Receivable",
    "Payable", "Intangible", "Research", "Selling", "General", "Administrative", "Finance", "Depreciation", "Amortization",
    "Minority", "Non",
})
_GENERIC_PREFIX = re.compile(r"常规|时段|交易|口径|累计|累积|期间|首日|当日|全年|最新|前\d+个月|本季|上季|去年|今年|合计|总计|当周|全稀释|盘中|盘后|该公司|公司|同期")

_CURRENCY_TOKENS: tuple[tuple[str, str], ...] = (
    ("USD", r"(?<![a-z])usd(?![a-z])|us\$|美元|美金|(?<![a-z])\$"),
    ("EUR", r"(?<![a-z])eur(?![a-z])|€|欧元"),
    ("GBP", r"(?<![a-z])gbp(?![a-z])|£|英镑"),
    ("JPY", r"(?<![a-z])jpy(?![a-z])|日元|日圆"),
    ("CNY", r"(?<![a-z])cny(?![a-z])|(?<![a-z])rmb(?![a-z])|人民币|(?<![港美日韩欧加澳])元"),
    ("HKD", r"(?<![a-z])hkd(?![a-z])|hk\$|港元|港币"),
    ("KRW", r"(?<![a-z])krw(?![a-z])|韩元|₩"),
    ("CAD", r"(?<![a-z])cad(?![a-z])|c\$|加元"),
    ("CHF", r"(?<![a-z])chf(?![a-z])|瑞郎"),
    ("INR", r"(?<![a-z])inr(?![a-z])|卢比"),
    ("AUD", r"(?<![a-z])aud(?![a-z])|a\$|澳元"),
)
_COMPILED_CURRENCY = tuple((code, re.compile(pat, re.I)) for code, pat in _CURRENCY_TOKENS)

_SCALE_WORDS: tuple[tuple[str, Decimal], ...] = (
    (r"万亿", Decimal("1e12")), (r"trillion|\btn\b", Decimal("1e12")), (r"十亿", Decimal("1e9")),
    (r"billion|\bbn\b", Decimal("1e9")), (r"亿", Decimal("1e8")), (r"百万", Decimal("1e6")),
    (r"million|\bmn\b|\bmm\b", Decimal("1e6")), (r"万", Decimal("1e4")), (r"thousand|千", Decimal("1e3")),
)
_SUFFIX_SCALE = {"万亿": Decimal("1e12"), "亿": Decimal("1e8"), "万": Decimal("1e4"), "千": Decimal("1e3"),
                 "trillion": Decimal("1e12"), "tn": Decimal("1e12"), "t": Decimal("1e12"),
                 "billion": Decimal("1e9"), "bn": Decimal("1e9"), "b": Decimal("1e9"),
                 "million": Decimal("1e6"), "mn": Decimal("1e6"), "mm": Decimal("1e6"), "m": Decimal("1e6"),
                 "thousand": Decimal("1e3"), "k": Decimal("1e3")}
_NUMBER = re.compile(
    r"^\s*([+\-−]?)\s*(US\$|HK\$|[$€£¥₩])?\s*([+\-−]?\d[\d,]*(?:\.\d+)?|\.\d+)\s*"
    r"(万亿|亿|万|千|trillion|billion|million|thousand|tn|bn|mm|mn|t|b|m|k)?\s*(%|％)?\s*$", re.I)
_PERCENT_UNIT = re.compile(r"percent|percentage|%|％|pct|百分|个点|\bpp\b", re.I)
_BPS_UNIT = re.compile(r"\bbps\b|basis point|基点", re.I)
_RATIO_UNIT = re.compile(r"\bratio\b|\bfraction\b|\bdecimal\b|比率", re.I)
_PER_SHARE_UNIT = re.compile(r"per share|/ ?share|每股", re.I)
_SHARES_UNIT = re.compile(r"shares?\b|股", re.I)

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_MONTH_RE = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATE_PATTERNS = (
    re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)"),
    re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?"),
    re.compile(_MONTH_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", re.I),
    re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH_RE + r",?\s+(\d{4})", re.I),
)
_RANGE_CONNECTORS = re.compile(r"\bto\b|through|thru|~|～|至|到|—|–|\s-\s", re.I)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, Mapping):
        return dict(obj)
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return {k: getattr(obj, k) for k in dir(obj) if not k.startswith("_") and not callable(getattr(obj, k, None))}


# --------------------------------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------------------------------


def _valid_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _find_dates(text: str) -> list[date]:
    found: list[tuple[int, date]] = []
    taken: list[tuple[int, int]] = []
    for index, pattern in enumerate(_DATE_PATTERNS):
        for match in pattern.finditer(text):
            if any(match.start() < e and s < match.end() for s, e in taken):
                continue
            g = match.groups()
            if index in (0, 1):
                parsed = _valid_date(int(g[0]), int(g[1]), int(g[2]))
            elif index == 2:
                parsed = _valid_date(int(g[2]), _MONTHS[g[0].lower()[:3]], int(g[1]))
            else:
                parsed = _valid_date(int(g[2]), _MONTHS[g[1].lower()[:3]], int(g[0]))
            if parsed is not None:
                taken.append((match.start(), match.end()))
                found.append((match.start(), parsed))
    return [d for _, d in sorted(found)]


_FY_WORDS = re.compile(r"(?<![a-z])fy|fiscal|财年|财政年度|财季", re.I)
_QUARTER_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "1": 1, "2": 2, "3": 3, "4": 4}


def _parse_quarter(s: str) -> str | None:
    m = re.search(r"(?<![A-Za-z0-9])(?:fy)?\s*[’']?(20\d{2}|\d{2})\s*[- ]?\s*q([1-4])(?!\d)", s, re.I)
    if m:
        year, q = m.group(1), m.group(2)
    else:
        m = (re.search(r"(?<![A-Za-z0-9])q([1-4])\s*[- ]?\s*(?:fy)?\s*[’']?(20\d{2}|\d{2})(?!\d)", s, re.I))
        if m:
            q, year = m.group(1), m.group(2)
        else:
            m = re.search(r"(20\d{2})\s*年?\s*第?\s*([一二三四1-4])\s*季", s)
            if not m:
                return None
            year, q = m.group(1), str(_QUARTER_NUM[m.group(2)])
    year = year if len(year) == 4 else f"20{year}"
    return f"{year}Q{q}"


def normalize_period(text: str | None) -> str | None:
    """Canonical period key, or None when empty/ambiguous.

    ``2026-11-19`` (also ``Nov. 19, 2026``, ``2026年11月19日``), ``2025-11-19..2026-11-19`` (range), ``W2026-11-02`` (week),
    ``2026-09`` (month), ``2026Q3`` / ``FY2026Q3`` (quarter, FY marker kept apart), ``FY2026``, ``2026H1``, ``2026-9M``,
    ``Y2026`` (bare calendar year). A quarter without a year is ambiguous (None).
    """
    if not isinstance(text, str) or not text.strip():
        return None
    s = text.strip()
    dates = _find_dates(s)
    if len(dates) == 1:
        if re.search(r"当周|\bweek\b", s, re.I):
            return f"W{dates[0].isoformat()}"
        return dates[0].isoformat()
    if len(dates) == 2 and _RANGE_CONNECTORS.search(s):
        return f"{dates[0].isoformat()}..{dates[1].isoformat()}"
    if dates:
        return None
    fy = bool(_FY_WORDS.search(s))
    prefix = "FY" if fy else ""
    quarter = _parse_quarter(s)
    if quarter:
        return f"{prefix}{quarter}"
    if re.search(r"(?<![A-Za-z0-9])q[1-4](?![0-9])", s, re.I) or re.search(r"第?[一二三四]季度", s):
        return None  # a quarter without a year is ambiguous
    half = re.search(r"(?<![A-Za-z0-9])(?:(\d{4})\s*)?h([12])(?:\s*(\d{4}))?(?![0-9])|(\d{4})\s*年?\s*(上|下)半年", s, re.I)
    if half:
        if half.group(5):
            return f"{prefix}{half.group(4)}H{1 if half.group(5) == '上' else 2}"
        year = half.group(1) or half.group(3)
        return f"{prefix}{year}H{half.group(2)}" if year else None
    nine = re.search(r"(?:前\s*(?:9|九|三)\s*(?:个月|季度?)|nine months|9m|first nine months|9 months)", s, re.I)
    year = re.search(r"(?<!\d)(20\d{2})(?!\d)", s)
    if nine and year:
        return f"{prefix}{year.group(1)}-9M"
    ym = (re.search(r"(?<!\d)(\d{4})[-/.](\d{1,2})(?![\d-])", s) or re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月(?!\s*\d*\s*[日号])", s))
    if ym and 1 <= int(ym.group(2)) <= 12:
        return f"{ym.group(1)}-{int(ym.group(2)):02d}"
    mon = re.search(_MONTH_RE + r",?\s+(\d{4})", s, re.I)
    if mon:
        return f"{mon.group(2)}-{_MONTHS[mon.group(1).lower()[:3]]:02d}"
    fy_year = re.search(r"(?:fy|fiscal(?: year)?)\s*[’']?(\d{4}|\d{2})(?!\d)|(\d{4})\s*(?:财年|财政年度|年度)", s, re.I)
    if fy_year:
        y = fy_year.group(1) or fy_year.group(2)
        return f"FY{y if len(y) == 4 else '20' + y}"
    if year and re.fullmatch(r"\s*(?:calendar\s*(?:year)?\s*)?(20\d{2})\s*年?\s*(?:全年|整年|全年度)?\s*", s, re.I):
        return f"Y{year.group(1)}"
    return None


@dataclass(frozen=True)
class NormValue:
    value: Decimal            # base units; percent kind = percentage points
    kind: str                 # price | per_share | count | money | percent
    currency: str | None
    decimals: int             # decimals as stated in the source text
    resolution: Decimal       # smallest step implied by stated precision, in normalised units
    scale: Decimal


def parse_currency(*texts: str | None) -> str | None:
    """First currency code found in the given texts (field, unit, quotation); None if none or conflicting."""
    seen: list[str] = []
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        for code, pattern in _COMPILED_CURRENCY:
            if pattern.search(text) and code not in seen:
                seen.append(code)
    return seen[0] if len(seen) == 1 else None


def _unit_scale(unit: str) -> Decimal | None:
    hits = {scale for pattern, scale in _SCALE_WORDS if re.search(pattern, unit, re.I)}
    # "万亿" also matches "万" and "亿": the larger single hit wins.
    if not hits:
        return None
    return max(hits)


def normalize_value(value: Any, unit: str | None, metric_kind: str, currency: str | None = None) -> NormValue | None:
    """Parse ``value`` + ``unit`` into base units. None when unparseable or scale conflicts."""
    raw = value if isinstance(value, str) else (str(value) if isinstance(value, (int, float, Decimal)) else "")
    if metric_kind == "ratio":
        raw = re.sub(r"\s*(?:x|倍|times)\s*$", "", raw, flags=re.I)
    match = _NUMBER.match(raw)
    if not match:
        return None
    sign, sym, num, suffix, pct_mark = match.groups()
    try:
        number = Decimal(num.replace(",", "").replace("−", "-"))
    except InvalidOperation:
        return None
    if sign in ("-", "−"):
        number = -number
    decimals = len(num.split(".")[1]) if "." in num else 0
    unit_text = unit or ""
    suffix_scale = _SUFFIX_SCALE.get(suffix.lower()) if suffix else None
    unit_scale = _unit_scale(unit_text)
    if suffix_scale is not None and unit_scale is not None and suffix_scale != unit_scale:
        return None
    scale = suffix_scale or unit_scale or Decimal(1)
    sym_currency = {"$": "USD", "US$": "USD", "HK$": "HKD", "€": "EUR", "£": "GBP", "₩": "KRW"}.get(sym or "")
    cur = parse_currency(currency, unit_text) or sym_currency
    is_pct = bool(pct_mark) or bool(_PERCENT_UNIT.search(unit_text))
    if is_pct:
        return NormValue(number, "percent", None, decimals, Decimal(1).scaleb(-decimals), Decimal(1))
    if _BPS_UNIT.search(unit_text):
        return NormValue(number / 100, "percent", None, decimals + 2, Decimal(1).scaleb(-decimals) / 100, Decimal(1))
    if _RATIO_UNIT.search(unit_text) and metric_kind != "ratio":
        # a ratio is only comparable with percentages: express it as percentage points
        return NormValue(number * 100, "percent", None, max(decimals - 2, 0), Decimal(1).scaleb(-decimals) * 100, Decimal(1))
    base = number * scale
    resolution = scale * Decimal(1).scaleb(-decimals)
    return NormValue(base, metric_kind, cur, decimals, resolution, scale)


def _blank(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


def detect_metrics(label: str) -> tuple[list[str], list[tuple[int, int]]]:
    """Distinct metric keys found in ``label`` in rule order, with matched spans."""
    work = label or ""
    keys: list[str] = []
    spans: list[tuple[int, int]] = []
    for rule, pattern in _COMPILED_RULES:
        while True:
            match = pattern.search(work)
            if not match or match.end() == match.start():
                break
            if rule.key not in keys:
                keys.append(rule.key)
            spans.append(match.span())
            work = _blank(work, *match.span())
    return keys, spans


def detect_qualifiers(label: str, unit: str | None = None) -> tuple[str, ...]:
    text = f"{label or ''} {unit or ''}"
    found = [key for key, pattern in _COMPILED_QUALIFIERS if pattern.search(text)]
    return tuple(sorted(found))


_TIMING_KEYS = ("close", "open", "high", "low")


def _drop_timing_words(keys: list[str]) -> list[str]:
    """"Market cap at close": close/open only say *when*; the single non-price metric is the measure."""
    measures = [k for k in keys if _RULE_BY_KEY[k].kind != "price"]
    if len(measures) == 1 and all(k in _TIMING_KEYS for k in keys if k != measures[0]):
        return measures
    return keys


def timing_qualifiers(label: str) -> tuple[str, ...]:
    keys, _ = detect_metrics(label)
    if len(_drop_timing_words(keys)) == len(keys):
        return ()
    return tuple(f"at_{k}" for k in keys if k in _TIMING_KEYS)


def derive_metric(label: str, unit: str | None, *, norm_kind_hint: str | None = None) -> tuple[str | None, str | None]:
    """(metric key, reason it is None). Percent-unit facts become ``<metric>_pct``; ambiguous labels give None."""
    keys, spans = detect_metrics(label)
    if not keys:
        return None, "no_metric"
    keys = _drop_timing_words(keys)
    if len(keys) > 1:
        return None, "ambiguous_metric"
    key = keys[0]
    rule = _RULE_BY_KEY[key]
    if key in ("high", "low", "open", "close") and _RANGE_SCOPE.search(label):
        return None, "range_scope"
    unit_text = unit or ""
    pct = bool(_PERCENT_UNIT.search(unit_text) or _BPS_UNIT.search(unit_text) or _RATIO_UNIT.search(unit_text))
    if key in _PERCENT_METRIC_KEYS:
        return (key, None) if pct else (None, "margin_without_percent_unit")
    if rule.kind == "ratio":
        if bool(_PERCENT_UNIT.search(unit_text) or _BPS_UNIT.search(unit_text)):
            if _CHANGE_WORDS.search(label):
                return f"{key}_pct", None
            return None, "percent_semantics_unknown"
        if parse_currency(unit_text):
            return None, "ratio_with_currency_unit"
        return key, None
    if pct:
        if _CHANGE_WORDS.search(label):
            return f"{key}_pct", None
        return None, "percent_semantics_unknown"
    if rule.kind == "count" and parse_currency(unit_text) and not _SHARES_UNIT.search(unit_text):
        return None, "count_with_currency_unit"
    if rule.kind in ("price", "per_share") and _SHARES_UNIT.search(unit_text) and not _PER_SHARE_UNIT.search(unit_text) \
            and not parse_currency(unit_text):
        return None, "price_with_share_unit"
    return key, None


_EXCH_SYMBOL = re.compile(r"(?<![A-Za-z0-9])(?:[A-Z]{2,6}):([A-Z][A-Z0-9.\-]{0,9})(?![A-Za-z0-9])")
_SYMBOL_AT = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9.\-]{0,9})@[A-Z]{2,6}(?![A-Za-z0-9])")
_TICKER = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,5})(?![A-Za-z])")


_NAME_FOLLOWERS = frozenset({"ipo", "stock", "shares", "share", "inc", "corp", "pbc", "plc", "ltd", "co", "group", "holdings",
                             "ordinary", "class", "adr", "s", "first-day", "first"})


def _name_candidates(label: str) -> list[str]:
    """Capitalised words that can name an issuer. A sentence-initial capital only counts when it is followed by a
    metric word or an issuer-style word (``Anthropic IPO ...``, ``Anthropic revenue``), never for ``Fully diluted ...``."""
    out: list[str] = []
    tokens = list(re.finditer(r"[A-Za-z][A-Za-z'\-]*", label))
    for index, tok in enumerate(tokens):
        word = tok.group(0)
        if not re.fullmatch(r"[A-Z][a-z]{2,}(?:[A-Z][a-z]+)?", word) or word in _GENERIC_NAME_WORDS or detect_metrics(word)[0]:
            continue
        if index == 0:
            nxt = tokens[index + 1].group(0).lower().rstrip("'s") if index + 1 < len(tokens) else ""
            if not (nxt in _NAME_FOLLOWERS or (nxt and detect_metrics(nxt)[0]) or nxt == ""):
                continue
        out.append(word)
    return out


_PARENTHETICAL = re.compile(r"[（(\[【][^）)\]】]*[）)\]】]")


def entity_hint(label: str, *, quotation: str | None = None, source: Mapping[str, Any] | None = None,
                metric_spans: Sequence[tuple[int, int]] = ()) -> str | None:
    """Best-effort entity token (upper-case ticker or name); None when absent or more than one candidate.

    Parenthetical remarks and the publisher's own name (``StockTitan口径``) never count as the entity.
    """
    original = label or ""
    label = _PARENTHETICAL.sub(" ", original)
    host = source_host(source) if source is not None else None
    publisher = host.split(".")[-2].upper() if host and host.count(".") >= 1 else None
    provenance = _get(source, "provenance") if source is not None else None
    if isinstance(provenance, Mapping):
        sym = provenance.get("symbol")
        if isinstance(sym, str) and sym.strip():
            return sym.strip().upper()
        instrument = provenance.get("instrument_id") or provenance.get("entity_id")
        if isinstance(instrument, str) and ":" in instrument:
            return instrument.split(":", 1)[1].strip().upper() or None
    for text in (label,):
        if not text:
            continue
        symbols = list(dict.fromkeys(m.group(1) for m in _EXCH_SYMBOL.finditer(text)))
        if len(symbols) == 1:
            return symbols[0]
    tickers = [t for t in dict.fromkeys(m.group(1) for m in _TICKER.finditer(label or "")) if t not in _STOP_TICKERS
               and not re.fullmatch(r"Q[1-4]|FY\d*|H[12]", t)]
    if len(tickers) == 1:
        return tickers[0]
    if len(tickers) > 1:
        return None
    names = [w for w in _name_candidates(label) if w.upper() != publisher]
    if len(set(names)) == 1:
        return names[0].upper()
    if len(set(names)) > 1:
        return None
    # Chinese company prefix before the first metric word
    if metric_spans:
        first = min((s for s, _ in metric_spans), default=0)
        prefix = re.sub(r"[\s（(\[【].*$", "", original[:first]).strip()
        prefix = _GENERIC_PREFIX.sub("", prefix)
        if re.fullmatch(r"[一-鿿]{2,8}", prefix or ""):
            return prefix
    return None


# --------------------------------------------------------------------------------------------------
# Source classification
# --------------------------------------------------------------------------------------------------


def source_host(source: Any) -> str | None:
    url = _get(source, "url")
    if isinstance(url, str) and url:
        host = (urlparse(url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else (host or None)
    host = _get(source, "host")
    if isinstance(host, str) and host:
        host = host.lower()
        return host[4:] if host.startswith("www.") else host
    return None


def _host_matches(host: str | None, patterns: Iterable[str]) -> bool:
    return bool(host) and any(host == p or host.endswith("." + p) for p in patterns)


def classify_source(source: Any, *, issuer_hosts: Iterable[str] = ()) -> str:
    """Map source metadata onto a reconciliation source class (see ``SOURCE_CLASSES``)."""
    kind = _get(source, "kind")
    method = _get(source, "retrieval_method")
    provenance = _get(source, "provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    host = source_host(source)
    title = str(_get(source, "title") or "")
    if kind == "market_data" or method in ("market_data", "historical_prices") or provenance.get("provider") == "twelve_data":
        return "market_data"
    if kind is None and method is None and not host and re.search(r"daily close|\bbars\b", title, re.I):
        return "market_data"
    if method in ("sec_companyconcept", "sec_companyfacts", "sec_filing_html") or _host_matches(host, PRIMARY_HOSTS) \
            or (host and (host.endswith(".gov") or ".gov." in host)):
        return "filing"
    if kind == "user_supplied" or method in ("user_supplied", "user_pdf", "user_html"):
        return "user_upload"
    # NB: runtime ``kind == "official"`` marks every fetched web page, news included; only the official-site
    # retrieval path (or a caller supplied host) identifies the issuer's own site.
    if method == "official_html" or _host_matches(host, issuer_hosts):
        return "issuer"
    if host or method in ("web_html", "web_pdf"):
        return "news"
    return "other"


def _as_of(source: Any) -> str | None:
    prov = _get(source, "provenance")
    if isinstance(prov, Mapping):
        for key in ("actual_last_date", "actual_last", "effective_end_date", "end_date"):
            if isinstance(prov.get(key), str):
                return prov[key]
        cov = prov.get("coverage")
        if isinstance(cov, Mapping):
            actual = cov.get("actual")
            if isinstance(actual, Mapping) and isinstance(actual.get("last"), str):
                return actual["last"]
            if cov.get("data_as_of"):
                return str(cov["data_as_of"])
    return None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


# --------------------------------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------------------------------


def _quantize_to(value: Decimal, resolution: Decimal) -> Decimal:
    return ((value / resolution).quantize(Decimal(1), rounding=ROUND_HALF_UP)) * resolution


def explained_by_rounding(a: NormValue, b: NormValue) -> bool:
    """True when the coarser value equals the finer one rounded half-up to the coarser stated precision."""
    if a.value == b.value:
        return False
    coarse, fine = (a, b) if a.resolution >= b.resolution else (b, a)
    if coarse.resolution == fine.resolution:
        return False
    return _quantize_to(fine.value, coarse.resolution) == coarse.value


def compare_values(a: NormValue, b: NormValue, metric_kind: str) -> dict[str, Any]:
    """Status of two normalised values of the same kind: exact -> consistent; rounding; else differs + grade."""
    diff = abs(a.value - b.value)
    if a.value == b.value:
        return {"status": STATUS_CONSISTENT, "abs": Decimal(0), "rel": Decimal(0), "within_tolerance": True, "severity": None}
    base = max(abs(a.value), abs(b.value)) or Decimal(1)
    rel = diff / base
    if explained_by_rounding(a, b):
        return {"status": STATUS_ROUNDING, "abs": diff, "rel": rel, "within_tolerance": True, "severity": None}
    tol = TOLERANCES.get(metric_kind, TOLERANCES["money"])
    within = diff <= tol["abs"] if "abs" in tol else rel <= tol["rel"]
    return {"status": STATUS_DIFFERS, "abs": diff, "rel": rel, "within_tolerance": within,
            "severity": "minor" if within else "material"}


# --------------------------------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------------------------------


@dataclass
class _Item:
    fact_id: str
    source_id: str
    label: str
    metric: str
    kind: str
    period: str | None
    entity: str | None
    qualifiers: tuple[str, ...]
    norm: NormValue
    value_text: str
    unit: str
    source: dict[str, Any]
    source_class: str
    host: str | None
    cluster: str | None
    quotation: str
    fact: dict[str, Any]
    order: int


def _fmt(d: Decimal) -> str:
    return format(d.normalize(), "f") if d else "0"


def _group_key(item: _Item) -> tuple:
    currency = item.norm.currency if item.kind in ("price", "money", "per_share") else None
    return (item.metric, item.period, item.entity, item.qualifiers, item.kind, currency)


def _excerpt(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _canonical_url(source: Mapping[str, Any]) -> str | None:
    url = _get(source, "url")
    if not isinstance(url, str) or not url:
        return None
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return f"{host}{parts.path.rstrip('/')}" if host else None


def _independent_key(item: _Item) -> str:
    """Syndication cluster, else the canonical URL (the same page fetched twice is one source), else the source id."""
    if item.cluster:
        return f"cluster:{item.cluster}"
    url = _canonical_url(item.source)
    return f"url:{url}" if url else f"source:{item.source_id}"


_FY_PERIOD_RE = re.compile(r"^FY(20\d{2})$")
_DATE_PERIOD_RE = re.compile(r"^(20\d{2})-(\d{2})-(\d{2})$")


def _build_fiscal_alignment_hints(items: Sequence[_Item]) -> list[dict[str, Any]]:
    """Surface potential ``FY20XX`` vs ``20XX-MM-DD`` matches without merging them."""
    buckets: dict[tuple[Any, ...], list[_Item]] = {}
    for item in items:
        if item.period is None:
            continue
        currency = item.norm.currency if item.kind in ("price", "money", "per_share") else None
        buckets.setdefault((item.metric, item.entity, item.qualifiers, item.kind, currency), []).append(item)

    hints: list[dict[str, Any]] = []
    for (metric, entity, qualifiers, _kind, currency), bucket in buckets.items():
        fy_by_year: dict[str, list[_Item]] = {}
        date_by_year: dict[str, dict[str, list[_Item]]] = {}
        for item in bucket:
            period = item.period or ""
            fy_match = _FY_PERIOD_RE.fullmatch(period)
            if fy_match:
                fy_by_year.setdefault(fy_match.group(1), []).append(item)
                continue
            date_match = _DATE_PERIOD_RE.fullmatch(period)
            if date_match:
                date_by_year.setdefault(date_match.group(1), {}).setdefault(period, []).append(item)
        for year in sorted(set(fy_by_year) & set(date_by_year)):
            fy_items = fy_by_year[year]
            fiscal_period = f"FY{year}"
            for date_period in sorted(date_by_year[year]):
                date_items = date_by_year[year][date_period]
                hints.append({
                    "entity": entity,
                    "metric": metric,
                    "currency": currency,
                    "qualifiers": list(qualifiers),
                    "fiscal_period": fiscal_period,
                    "date_period": date_period,
                    "fiscal_fact_ids": [it.fact_id for it in fy_items],
                    "date_fact_ids": [it.fact_id for it in date_items],
                    "merged": False,
                    "message": (
                        f"{entity} {metric} has separate {fiscal_period} and {date_period} facts"
                        + (f" ({currency})" if currency else "")
                        + "; verify fiscal year-end alignment before comparing (not merged)."
                    ),
                })
    hints.sort(key=lambda h: (h["entity"], h["metric"], h["currency"] or "", h["fiscal_period"], h["date_period"]))
    return hints


def reconcile(facts: Iterable[Any], sources: Iterable[Any] = (), *, calculations: Iterable[Any] = (),
              cluster_of: Callable[[Any], str | None] | None = None,
              entity_aliases: Mapping[str, str] | None = None, default_entity: str | None = None,
              issuer_hosts: Iterable[str] = (), top_n_gaps: int = 5) -> dict[str, Any]:
    """Compare recorded facts across sources. Returns a JSON-serialisable report; see module docstring.

    ``facts``: fact mappings (label, value, unit, currency, period, source_id, quotation, location, fact_id,
    extraction_method, ocr_mean_confidence, ...). ``sources``: source mappings keyed by ``source_id``
    (kind, retrieval_method, url, title, published_at, provenance, permissions). ``cluster_of`` maps a source
    to a syndication cluster id (or None). ``entity_aliases`` maps name/ticker variants onto one canonical
    token (``{"ANTHROPIC": "ANT"}``); ``default_entity`` is used for facts that carry no entity hint at all
    (only safe when the whole topic is about one issuer; it is off by default).
    """
    aliases = {k.upper(): v.upper() for k, v in (entity_aliases or {}).items()}
    by_id: dict[str, dict[str, Any]] = {}
    for source in sources or ():
        sd = _as_dict(source)
        if isinstance(sd.get("source_id"), str):
            by_id[sd["source_id"]] = sd
    class_cache: dict[str, str] = {}
    cluster_cache: dict[str, str | None] = {}
    unmerged: Counter[str] = Counter()
    items: list[_Item] = []
    total = 0
    for order, raw in enumerate(facts or ()):
        total += 1
        fact = _as_dict(raw)
        label = str(fact.get("label") or "")
        source_id = str(fact.get("source_id") or "")
        source = by_id.get(source_id, {"source_id": source_id})
        unit = str(fact.get("unit") or "")
        quotation = str(fact.get("quotation") or "")
        keys, spans = detect_metrics(label)
        metric, why = derive_metric(label, unit)
        if metric is None:
            unmerged[why or "no_metric"] += 1
            continue
        base_key = metric[:-4] if metric.endswith("_pct") else metric
        rule = _RULE_BY_KEY[base_key]
        kind = "percent" if metric.endswith("_pct") or base_key in _PERCENT_METRIC_KEYS else rule.kind
        norm = normalize_value(fact.get("value"), unit, kind, fact.get("currency") if isinstance(fact.get("currency"), str) else None)
        if norm is None:
            unmerged["unparseable_value"] += 1
            continue
        if norm.kind != kind:
            kind = norm.kind
            if kind == "percent" and not metric.endswith("_pct") and base_key not in _PERCENT_METRIC_KEYS:
                metric = f"{base_key}_pct"
        if kind in ("price", "money", "per_share") and norm.currency is None:
            unmerged["currency_unknown"] += 1
            continue
        timeless = rule.timeless and kind != "percent"
        period = None
        if not timeless:
            period = normalize_period(fact.get("period")) or normalize_period(label)
            if period is None:
                unmerged["period_unknown"] += 1
                continue
        entity = entity_hint(label, quotation=quotation, source=source, metric_spans=spans) or default_entity
        if entity is not None:
            entity = aliases.get(entity.upper(), entity.upper())
        if entity is None:
            unmerged["entity_unknown"] += 1
            continue
        if source_id not in class_cache:
            class_cache[source_id] = classify_source(source, issuer_hosts=issuer_hosts)
            cluster_cache[source_id] = (cluster_of(source) if cluster_of is not None and source_id in by_id else None)
        qualifiers = tuple(sorted(set(detect_qualifiers(label, unit)) | set(timing_qualifiers(label))))
        if base_key in ("net_income", "operating_income") and kind == "money" and norm.value > 0 \
                and re.search(r"loss|亏损", label, re.I) and not re.search(r"profit|income|盈利|利润", re.sub(r"net (?:income|profit|loss)|operating (?:income|profit|loss)|净亏损|营业亏损", "", label, flags=re.I), re.I):
            norm = NormValue(-norm.value, norm.kind, norm.currency, norm.decimals, norm.resolution, norm.scale)
        if "range_scope" in qualifiers and base_key in ("high", "low", "open", "close"):
            unmerged["range_scope"] += 1
            continue
        items.append(_Item(
            fact_id=str(fact.get("fact_id") or f"fact#{order}"), source_id=source_id, label=label, metric=metric, kind=kind,
            period=period, entity=entity, qualifiers=qualifiers, norm=norm, value_text=str(fact.get("value")), unit=unit,
            source=source, source_class=class_cache[source_id], host=source_host(source), cluster=cluster_cache[source_id],
            quotation=quotation, fact=fact, order=order))

    calc_inputs: dict[str, list[str]] = {}
    for calc in calculations or ():
        cd = _as_dict(calc)
        for inp in cd.get("inputs") or []:
            fid = _get(inp, "fact_id")
            if isinstance(fid, str):
                calc_inputs.setdefault(fid, []).append(str(cd.get("calculation_id") or ""))

    groups: dict[tuple, list[_Item]] = {}
    for item in items:
        groups.setdefault(_group_key(item), []).append(item)

    rows: list[dict[str, Any]] = []
    singles = 0
    for key, members in groups.items():
        row = _build_row(key, members, calc_inputs)
        if row is None:
            singles += 1
            continue
        rows.append(row)
    rows.sort(key=lambda r: (-_STATUS_RANK[r["status"]], 0 if r["severity"] == "material" else 1, r["metric"], r["period"] or "", r["entity_hint"]))
    counts = Counter(r["status"] for r in rows)
    fiscal_alignment_hints = _build_fiscal_alignment_hints(items)
    report = {
        "version": VERSION,
        "rows": rows,
        "stats": {"facts_total": total, "facts_considered": len(items), "groups": len(groups), "rows": len(rows),
                  "single_source_groups_skipped": singles, "by_status": {s: counts.get(s, 0) for s in _STATUS_RANK},
                  "unmerged": dict(sorted(unmerged.items()))},
        "limits": ["Only facts recorded in this run are compared; a missing row means the facts were not comparable or "
                   "only one source reported the figure, not that the figure is confirmed.",
                   "Reference values prefer exchange data and filings; no value is corrected automatically."],
        "fiscal_alignment_hints": fiscal_alignment_hints,
    }
    report["suggested_gaps"] = suggested_gaps(report, limit=top_n_gaps)
    return report


def _worst_pair(values: list[_Item]) -> tuple[dict[str, Any], tuple[_Item, _Item] | None]:
    """Worst pairwise comparison; ``values`` is ordered reference-first so ties favour pairs with the reference."""
    worst: dict[str, Any] | None = None
    pair = None
    for i in range(len(values)):
        for j in range(i + 1, len(values)):
            cmp = compare_values(values[i].norm, values[j].norm, values[i].kind)
            if worst is None or (_STATUS_RANK[cmp["status"]], cmp["abs"]) > (_STATUS_RANK[worst["status"]], worst["abs"]):
                worst, pair = cmp, (values[i], values[j])
    assert worst is not None
    return worst, pair


def _build_row(key: tuple, members: list[_Item], calc_inputs: dict[str, list[str]]) -> dict[str, Any] | None:
    metric, period, entity, qualifiers, kind, currency = key
    independent = {_independent_key(m) for m in members}
    # one comparable value per (independent source, value); keep every fact id for traceability
    unique: dict[tuple[str, Decimal], list[_Item]] = {}
    for m in members:
        unique.setdefault((_independent_key(m), m.norm.value), []).append(m)
    reps = [group[0] for group in unique.values()]
    reps.sort(key=lambda m: m.order)
    distinct_values = {r.norm.value for r in reps}
    if len(independent) < 2 and len(distinct_values) < 2:
        return None  # one source repeating itself: nothing to reconcile
    base_key = metric[:-4] if metric.endswith("_pct") else metric
    rule = _RULE_BY_KEY[base_key]
    primary_order = rule.primary_order
    primaries = [r for r in reps if r.source_class in primary_order]
    reference: _Item | None = None
    ref_reason = "no_primary_source"
    if primaries:
        primaries.sort(key=lambda r: (primary_order.index(r.source_class), r.norm.resolution, r.order))
        reference = primaries[0]
        ref_reason = f"preferred_{reference.source_class}"
        if len({p.norm.value for p in primaries}) > 1 and any(
                compare_values(reference.norm, p.norm, kind)["status"] == STATUS_DIFFERS for p in primaries[1:]):
            ref_reason += ";primary_sources_disagree"
    ordered = ([reference] + [r for r in reps if r is not reference]) if reference is not None else reps
    worst, pair = _worst_pair(ordered)
    values: list[dict[str, Any]] = []
    for rep in ordered:
        dupes = unique[(_independent_key(rep), rep.norm.value)]
        vs = None
        if reference is not None and rep is not reference:
            c = compare_values(reference.norm, rep.norm, kind)
            vs = {"status": c["status"], "abs": _fmt(c["abs"]), "rel": _fmt(c["rel"].quantize(Decimal("0.000001"))),
                  "within_tolerance": c["within_tolerance"], "severity": c["severity"]}
        fact = rep.fact
        ocr_conf = fact.get("ocr_mean_confidence")
        values.append({
            "fact_id": rep.fact_id, "source_id": rep.source_id, "source_class": rep.source_class, "host": rep.host,
            "value": _fmt(rep.norm.value), "value_as_stated": rep.value_text, "unit": rep.unit or None,
            "currency": rep.norm.currency, "decimals": rep.norm.decimals,
            "published_at": _iso(_get(rep.source, "published_at")), "as_of": _as_of(rep.source),
            "quotation_excerpt": _excerpt(rep.quotation), "label": rep.label, "cluster_id": rep.cluster,
            "location": fact.get("location") if isinstance(fact.get("location"), Mapping) else None,
            "extraction_method": fact.get("extraction_method"), "ocr_mean_confidence": ocr_conf,
            "low_ocr_confidence": isinstance(ocr_conf, (int, float)) and ocr_conf < LOW_OCR_CONFIDENCE,
            "is_reference": rep is reference, "vs_reference": vs, "duplicate_fact_ids": [d.fact_id for d in dupes[1:]],
            "export_allowed": _export_allowed(rep.source),
        })
    status = worst["status"]
    difference = None
    if pair is not None:
        a, b = pair
        difference = {"abs": _fmt(worst["abs"]), "rel": _fmt(worst["rel"].quantize(Decimal("0.000001"))),
                      "unit": "percentage_points" if kind == "percent" else None,
                      "between": [a.fact_id, b.fact_id]}
    hosts = sorted({r.host for g in unique.values() for r in g if r.host})
    flags = []
    if len(independent) < 2:
        flags.append("single_source_internal_conflict")
    if any(v["low_ocr_confidence"] for v in values):
        flags.append("low_ocr_confidence")
    if reference is None:
        flags.append("no_primary_source")
    if "primary_sources_disagree" in ref_reason:
        flags.append("primary_sources_disagree")
    if status == STATUS_DIFFERS and len(independent) >= 2 and len({r.source_class for r in reps}) == 1 and reps[0].source_class == "news":
        flags.append("news_only")
    row_id = "recon-" + hashlib.sha256(repr((metric, period, entity, qualifiers, kind, currency)).encode()).hexdigest()[:12]
    row = {
        "row_id": row_id, "metric": metric, "metric_label": {"zh": rule.zh + ("变动(%)" if metric.endswith("_pct") and base_key not in _PERCENT_METRIC_KEYS else ""),
                                                             "en": rule.en + (" change (%)" if metric.endswith("_pct") and base_key not in _PERCENT_METRIC_KEYS else "")},
        "kind": kind, "period": period, "entity_hint": entity, "qualifiers": list(qualifiers), "currency": currency,
        "values": values,
        "reference_value": ({"value": _fmt(reference.norm.value), "fact_id": reference.fact_id, "source_id": reference.source_id,
                             "source_class": reference.source_class, "reason": ref_reason} if reference else
                            {"value": None, "reason": ref_reason}),
        "status": status, "severity": worst["severity"], "within_tolerance": worst["within_tolerance"], "difference": difference,
        "tolerance": {k: str(v) for k, v in TOLERANCES.get(kind, {}).items()},
        "independent_source_count": len(independent), "distinct_hosts": hosts, "flags": flags,
        "affected_calculations": affected_for(unique, calc_inputs),
    }
    return row


def affected_for(unique: Mapping[Any, list[_Item]], calc_inputs: Mapping[str, list[str]]) -> list[str]:
    return sorted({c for group in unique.values() for m in group for c in calc_inputs.get(m.fact_id, []) if c})


def _export_allowed(source: Mapping[str, Any]) -> bool | None:
    perms = _get(source, "permissions")
    if isinstance(perms, Mapping) and "export" in perms:
        return bool(perms["export"])
    return None


# --------------------------------------------------------------------------------------------------
# Consumers: gaps, model summary, UI view
# --------------------------------------------------------------------------------------------------


def _describe_value(v: Mapping[str, Any]) -> str:
    cur = f" {v['currency']}" if v.get("currency") else ""
    host = v.get("host") or v.get("source_id")
    return f"{v['value_as_stated'] or v['value']}{cur} ({v['source_class']}: {host}, {v['fact_id']})"


def suggested_gaps(report: Mapping[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    """Gap/counterpoint hints for ``differs`` rows (material first). Codes are stable; message is the English fallback."""
    out = []
    for row in report.get("rows", []):
        if row["status"] != STATUS_DIFFERS:
            continue
        ref = row["reference_value"]
        out.append({
            "code": "source_discrepancy_material" if row["severity"] == "material" else "source_discrepancy_minor",
            "row_id": row["row_id"], "metric": row["metric"], "period": row["period"], "entity": row["entity_hint"],
            "fact_ids": [v["fact_id"] for v in row["values"]],
            "message": (f"{row['entity_hint']} {row['metric']} {row['period'] or ''}: sources disagree "
                        f"({'; '.join(_describe_value(v) for v in row['values'])}); "
                        + (f"reference {ref['value']} from {ref['source_class']}." if ref.get("value") is not None
                           else "no exchange or filing value to use as reference.")).replace("  ", " "),
        })
        if len(out) >= limit:
            break
    return out


def model_summary(report: Mapping[str, Any], *, top_n: int = 5, max_chars: int = 2400) -> dict[str, Any]:
    """Compact, model-facing summary: counts and the top discrepancies. Facts are not altered."""
    rows = list(report.get("rows", []))
    discrepancies = []
    for row in rows:
        if row["status"] != STATUS_DIFFERS:
            continue
        ref = row["reference_value"]
        discrepancies.append({
            "row_id": row["row_id"], "entity": row["entity_hint"], "metric": row["metric"], "period": row["period"],
            "severity": row["severity"], "independent_sources": row["independent_source_count"],
            "values": [{"fact_id": v["fact_id"], "source_id": v["source_id"], "class": v["source_class"], "host": v["host"],
                        "value": v["value_as_stated"], "currency": v["currency"], "reference": v["is_reference"]}
                       for v in row["values"]],
            "reference": ref.get("value"), "reference_reason": ref.get("reason"), "difference": row["difference"],
            "flags": row["flags"], "affected_calculations": row["affected_calculations"],
        })
    discrepancies = discrepancies[:top_n]
    hints = list(report.get("fiscal_alignment_hints") or ())[:top_n]
    summary: dict[str, Any] = {
        "reconciliation": {
            "checked_groups": len(rows), "by_status": report.get("stats", {}).get("by_status", {}),
            "discrepancies": discrepancies,
            **({"fiscal_alignment_hints": hints} if hints else {}),
            "guidance": ("Different sources report different values for the discrepancies listed. Do not average or pick silently: "
                         "cite the reference value (exchange data or filing) when available, state the other value and its source, "
                         "and record the difference as a gap or counterpoint."),
        }}
    while len(repr(summary)) > max_chars and (summary["reconciliation"]["discrepancies"] or summary["reconciliation"].get("fiscal_alignment_hints")):
        if summary["reconciliation"]["discrepancies"]:
            summary["reconciliation"]["discrepancies"].pop()
        elif summary["reconciliation"].get("fiscal_alignment_hints"):
            summary["reconciliation"]["fiscal_alignment_hints"].pop()
        summary["reconciliation"]["truncated"] = True
    return summary


def ui_view(report: Mapping[str, Any]) -> dict[str, Any]:
    """UI-facing structure: one table row per reconciliation, one cell per independent value."""
    rows = []
    for row in report.get("rows", []):
        period = row["period"]
        title = {"zh": f"{row['entity_hint']} {row['metric_label']['zh']}" + (f" · {period}" if period else ""),
                 "en": f"{row['entity_hint']} {row['metric_label']['en']}" + (f" · {period}" if period else "")}
        cells = [{
            "fact_id": v["fact_id"], "source_id": v["source_id"], "source_class": v["source_class"], "host": v["host"],
            "display_value": f"{v['value_as_stated']}" + (f" {v['currency']}" if v["currency"] else ""),
            "published_at": v["published_at"], "as_of": v["as_of"], "quotation_excerpt": v["quotation_excerpt"],
            "is_reference": v["is_reference"], "deviation": v["vs_reference"], "low_ocr_confidence": v["low_ocr_confidence"],
            "export_allowed": v["export_allowed"], "location": v["location"],
        } for v in row["values"]]
        rows.append({"row_id": row["row_id"], "title": title, "status": row["status"], "severity": row["severity"],
                     "badge": ("differs_material" if row["status"] == STATUS_DIFFERS and row["severity"] == "material"
                               else "differs_minor" if row["status"] == STATUS_DIFFERS else row["status"]),
                     "cells": cells, "reference": row["reference_value"], "difference": row["difference"],
                     "independent_source_count": row["independent_source_count"], "distinct_hosts": row["distinct_hosts"],
                     "flags": row["flags"], "affected_calculations": row["affected_calculations"]})
    return {"version": VERSION, "counts": report.get("stats", {}).get("by_status", {}), "rows": rows,
            "fiscal_alignment_hints": list(report.get("fiscal_alignment_hints") or []),
            "unmerged": report.get("stats", {}).get("unmerged", {}), "limits": report.get("limits", [])}
