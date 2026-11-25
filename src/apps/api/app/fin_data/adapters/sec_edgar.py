"""SEC EDGAR adapter: identity, submissions, companyfacts, frames and full-text search.

Endpoints (all public, fair-access rules: declared User-Agent with contact, <=10 req/s):
- https://www.sec.gov/files/company_tickers.json
- https://data.sec.gov/submissions/CIK##########.json
- https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
- https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json
- https://efts.sec.gov/LATEST/search-index  (undocumented-but-public; policy category
  ``filing_search``, not in the default policy)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from hashlib import sha256
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlencode

from ...openai_compatible import HttpResponse, _TransportFailure
from ...research_sec import SecFailure, SecIdentityRequest, search_sec_identities, validate_user_agent
from ..models import (Coverage, EntityRef, Filing, FrameRow, FullTextHit, Permissions, StatementLine,
                      SuggestedAction, ToolResult)
from ..policy import Denied, FinDataPolicy, load_policy
from ..ratelimit import default_limiter
from .base import StopWith, StreamTransport, Transport, default_stream_transport, default_transport, request_json, utcnow

PROVIDER = "sec_edgar"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{tag}.json"
# json.load of a whole companyfacts document costs several times its size in Python objects; refuse above this
# size so a large issuer cannot exhaust the 512 MB API container. Specific lines use small companyconcept files.
MAX_COMPANYFACTS_BYTES = 60_000_000


class _CappedSink:
    """Sink wrapper that aborts the download once more than ``limit`` bytes arrive."""

    def __init__(self, sink, limit: int) -> None:
        self._sink, self._limit, self._size = sink, limit, 0

    def write(self, data: bytes) -> int:
        self._size += len(data)
        if self._size > self._limit:
            raise _TransportFailure("response too large")
        return self._sink.write(data)

    def __getattr__(self, name):
        return getattr(self._sink, name)


def cik10(value: str | int) -> str:
    text = str(value).strip().upper().removeprefix("CIK")
    if not text.isdigit() or int(text) == 0 or len(text) > 10:
        raise ValueError("CIK must be 1 to 10 digits and nonzero")
    return text.zfill(10)


def cik_entity_id(cik: str | int) -> str:
    return f"cik:{cik10(cik)}"


# ---------------------------------------------------------------- line mapping
@dataclass(frozen=True)
class LineSpec:
    line: str
    statement: str
    flow: bool            # duration (flow) vs instant (balance sheet)
    unit_kind: str        # money | per_share | shares
    tags: dict[str, tuple[str, ...]]


def _spec(line, statement, flow, unit_kind, us, ifrs) -> LineSpec:
    return LineSpec(line, statement, flow, unit_kind, {"us-gaap": tuple(us), "ifrs-full": tuple(ifrs)})


STANDARD_LINES: dict[str, LineSpec] = {s.line: s for s in (
    # ---- income statement (preserve original keys and tag priorities)
    _spec("revenue", "income", True, "money",
          ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
           "RevenueFromContractWithCustomerIncludingAssessedTax"], ["Revenue"]),
    _spec("cost_of_revenue", "income", True, "money",
          ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold", "CostOfServices"],
          ["CostOfSales"]),
    _spec("gross_profit", "income", True, "money", ["GrossProfit"], ["GrossProfit"]),
    _spec("rd_expense", "income", True, "money",
          ["ResearchAndDevelopmentExpense", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"],
          ["ResearchAndDevelopmentExpense"]),
    _spec("sga_expense", "income", True, "money",
          ["SellingGeneralAndAdministrativeExpense"], ["SellingGeneralAndAdministrativeExpense"]),
    _spec("selling_expense", "income", True, "money",
          ["SellingAndMarketingExpense", "SellingExpense"], ["SellingExpense", "DistributionCosts"]),
    _spec("admin_expense", "income", True, "money",
          ["GeneralAndAdministrativeExpense"], ["AdministrativeExpense"]),
    _spec("total_operating_costs", "income", True, "money",
          ["OperatingExpenses", "CostsAndExpenses", "OperatingCostsAndExpenses"], ["OperatingExpenses"]),
    _spec("operating_income", "income", True, "money", ["OperatingIncomeLoss"], ["ProfitLossFromOperatingActivities"]),
    _spec("interest_expense", "income", True, "money",
          ["InterestExpense", "InterestExpenseDebt", "InterestAndDebtExpense"],
          ["FinanceCosts", "InterestExpense"]),
    _spec("finance_expense", "income", True, "money",
          ["InterestIncomeExpenseNet", "InterestIncomeExpenseNonoperatingNet"], ["FinanceCosts"]),
    _spec("profit_before_tax", "income", True, "money",
          ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
           "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
          ["ProfitLossBeforeTax"]),
    _spec("income_tax", "income", True, "money", ["IncomeTaxExpenseBenefit"], ["IncomeTaxExpenseContinuingOperations"]),
    _spec("net_income", "income", True, "money", ["NetIncomeLoss", "ProfitLoss"], ["ProfitLoss"]),
    _spec("ebitda", "income", True, "money", ["EarningsBeforeInterestTaxesDepreciationAndAmortization"], []),
    _spec("eps_basic", "income", True, "per_share", ["EarningsPerShareBasic"], ["BasicEarningsLossPerShare"]),
    _spec("eps_diluted", "income", True, "per_share", ["EarningsPerShareDiluted"], ["DilutedEarningsLossPerShare"]),
    _spec("shares_basic", "income", True, "shares",
          ["WeightedAverageNumberOfSharesOutstandingBasic"], ["WeightedAverageShares"]),
    _spec("shares_diluted", "income", True, "shares",
          ["WeightedAverageNumberOfDilutedSharesOutstanding"], ["AdjustedWeightedAverageShares"]),
    # ---- balance sheet
    _spec("cash", "balance", False, "money",
          ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
          ["CashAndCashEquivalents"]),
    _spec("short_term_investments", "balance", False, "money",
          ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
          ["OtherCurrentFinancialAssets"]),
    _spec("accounts_receivable", "balance", False, "money",
          ["AccountsReceivableNetCurrent", "AccountsNotesAndLoansReceivableNetCurrent", "ReceivablesNetCurrent"],
          ["TradeAndOtherCurrentReceivables"]),
    _spec("inventory", "balance", False, "money",
          ["InventoryNet", "InventoryFinishedGoodsNetOfReserves"], ["Inventories"]),
    _spec("current_assets", "balance", False, "money", ["AssetsCurrent"], ["CurrentAssets"]),
    _spec("ppe_net", "balance", False, "money", ["PropertyPlantAndEquipmentNet"], ["PropertyPlantAndEquipment"]),
    _spec("goodwill", "balance", False, "money", ["Goodwill"], ["Goodwill"]),
    _spec("intangible_assets", "balance", False, "money",
          ["IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet"],
          ["IntangibleAssetsOtherThanGoodwill"]),
    _spec("total_assets", "balance", False, "money", ["Assets"], ["Assets"]),
    _spec("accounts_payable", "balance", False, "money",
          ["AccountsPayableCurrent", "AccountsPayableAndAccruedLiabilitiesCurrent"],
          ["TradeAndOtherCurrentPayables"]),
    _spec("short_term_debt", "balance", False, "money",
          ["ShortTermBorrowings", "DebtCurrent", "LongTermDebtCurrent", "NotesPayableCurrent"],
          ["ShorttermBorrowings", "CurrentPortionOfLongtermBorrowings"]),
    _spec("current_liabilities", "balance", False, "money", ["LiabilitiesCurrent"], ["CurrentLiabilities"]),
    _spec("long_term_debt", "balance", False, "money",
          ["LongTermDebtNoncurrent", "LongTermDebt", "LongTermNotesAndLoans"],
          ["LongtermBorrowings", "NoncurrentPortionOfNoncurrentLoansReceived"]),
    _spec("total_liabilities", "balance", False, "money", ["Liabilities"], ["Liabilities"]),
    _spec("retained_earnings", "balance", False, "money", ["RetainedEarningsAccumulatedDeficit"], ["RetainedEarnings"]),
    _spec("equity", "balance", False, "money",
          ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"], ["Equity"]),
    _spec("non_controlling_interests", "balance", False, "money", ["MinorityInterest"], ["NoncontrollingInterests"]),
    # ---- cash-flow statement
    _spec("depreciation_amortization", "cashflow", True, "money",
          ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization", "Depreciation"],
          ["DepreciationAndAmortisationExpense", "AdjustmentsForDepreciationAndAmortisationExpense"]),
    _spec("stock_based_compensation", "cashflow", True, "money",
          ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
          ["ExpenseFromSharebasedPaymentTransactionsWithEmployees", "AdjustmentsForSharebasedPayments"]),
    _spec("operating_cash_flow", "cashflow", True, "money",
          ["NetCashProvidedByUsedInOperatingActivities",
           "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"], ["CashFlowsFromUsedInOperatingActivities"]),
    _spec("capex", "cashflow", True, "money",
          ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
           "PaymentsToAcquireOtherPropertyPlantAndEquipment"],
          ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"]),
    _spec("investing_cash_flow", "cashflow", True, "money",
          ["NetCashProvidedByUsedInInvestingActivities",
           "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations"], ["CashFlowsFromUsedInInvestingActivities"]),
    _spec("financing_cash_flow", "cashflow", True, "money",
          ["NetCashProvidedByUsedInFinancingActivities",
           "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations"], ["CashFlowsFromUsedInFinancingActivities"]),
    _spec("dividends_paid", "cashflow", True, "money",
          ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends", "PaymentsOfOrdinaryDividends"],
          ["DividendsPaidClassifiedAsFinancingActivities", "DividendsPaid"]),
    _spec("share_repurchases", "cashflow", True, "money",
          ["PaymentsForRepurchaseOfCommonStock", "PaymentsForRepurchaseOfEquity"],
          ["PaymentsToAcquireOrRedeemEntitysShares"]),
    _spec("net_increase_in_cash", "cashflow", True, "money",
          ["CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
           "CashAndCashEquivalentsPeriodIncreaseDecrease"],
          ["IncreaseDecreaseInCashAndCashEquivalents"]),
)}


# ------------------------------------------------------------ period selection
@dataclass(frozen=True)
class Period:
    start: date | None
    end: date
    kind: str                 # quarter | year
    value: Decimal
    unit: str
    tag: str
    label: str | None         # "<fy>-<fp>" from the filing that first reported the period
    form: str | None
    filed: date | None
    accession: str | None
    frame: str | None
    derived: bool = False
    derivation: str | None = None


def _d(value: Any) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _entries(raw: Iterable[dict]) -> list[dict]:
    out = []
    for item in raw:
        val = item.get("val") if isinstance(item, dict) else None
        if isinstance(val, bool) or not isinstance(val, (int, float, Decimal)):
            continue
        try:
            value = Decimal(str(val))
        except InvalidOperation:
            continue
        end = _d(item.get("end"))
        if end is None or not value.is_finite():
            continue
        out.append({**item, "_val": value, "_start": _d(item.get("start")), "_end": end,
                    "_filed": _d(item.get("filed")) or date.min})
    return out


def classify_duration(start: date | None, end: date) -> str | None:
    if start is None:
        return "instant"
    days = (end - start).days
    if 78 <= days <= 105:
        return "quarter"
    if 340 <= days <= 380:
        return "year"
    return None


def _label(entry: dict) -> str | None:
    return f"{entry['fy']}-{entry['fp']}" if entry.get("fy") and entry.get("fp") else None


def _period(group: list[dict], kind: str, tag: str, unit: str) -> Period:
    latest = max(group, key=lambda e: (e["_filed"], str(e.get("accn", ""))))
    first = min(group, key=lambda e: (e["_filed"], str(e.get("accn", ""))))
    return Period(latest["_start"], latest["_end"], kind, latest["_val"], unit, tag, _label(first), latest.get("form"),
                  latest["_filed"] if latest["_filed"] != date.min else None, latest.get("accn"), latest.get("frame"))


def _collect(raw: Iterable[dict], kind: str, *, flow: bool, derive_q4: bool, tag: str = "", unit: str = "") -> list[Period]:
    groups: dict[tuple, list[dict]] = {}
    for e in _entries(raw):
        groups.setdefault((e["_start"], e["_end"]), []).append(e)
    periods: list[Period] = []
    for (start, end), group in groups.items():
        cls = classify_duration(start, end)
        if flow:
            if cls == kind:
                periods.append(_period(group, kind, tag, unit))
        elif cls == "instant":
            first = min(group, key=lambda e: (e["_filed"], str(e.get("accn", ""))))
            if kind == "quarter" or first.get("fp") == "FY":
                periods.append(_period(group, kind, tag, unit))
    if flow and kind == "quarter" and derive_q4:
        have_ends = {p.end for p in periods}
        ytd_derived = _derive_ytd_quarters(raw, tag, unit, have_ends)
        periods.extend(ytd_derived)
        have_ends.update(p.end for p in ytd_derived)
        periods.extend(_derive_q4(raw, tag, unit, have_ends))
    return sorted(periods, key=lambda p: p.end, reverse=True)


def _derive_ytd_quarters(raw: Iterable[dict], tag: str, unit: str, have_ends: set[date]) -> list[Period]:
    """Derive standalone Q2/Q3/Q4 cash-flow/flow quarters from SEC 10-Q/10-K YTD cumulative durations.

    US 10-Q cash flow statements typically disclose only 6M YTD (Q2) and 9M YTD (Q3) cumulative
    durations rather than standalone ~90d quarters:
      - Q2 = 6M YTD - Q1
      - Q3 = 9M YTD - 6M YTD
      - Q4 = FY - 9M YTD
    All arithmetic uses exact Decimal values on the same XBRL tag and unit, marked derived=True.
    """
    groups: dict[tuple[date | None, date], list[dict]] = {}
    for e in _entries(raw):
        groups.setdefault((e["_start"], e["_end"]), []).append(e)

    quarters: list[Period] = []
    halves: list[Period] = []
    nines: list[Period] = []
    years: list[Period] = []
    for (start, end), group in groups.items():
        if start is None:
            continue
        days = (end - start).days
        if 78 <= days <= 105:
            quarters.append(_period(group, "quarter", tag, unit))
        elif 160 <= days <= 205:
            halves.append(_period(group, "half", tag, unit))
        elif 250 <= days <= 295:
            nines.append(_period(group, "nine_months", tag, unit))
        elif 340 <= days <= 380:
            years.append(_period(group, "year", tag, unit))

    near = lambda a, b: a is not None and b is not None and abs((a - b).days) <= 5
    seen_ends = set(have_ends)
    out: list[Period] = []

    for half in sorted(halves, key=lambda p: p.end, reverse=True):
        if half.end in seen_ends or half.start is None:
            continue
        q1 = next(
            (q for q in quarters if near(q.start, half.start) and 60 <= (half.end - q.end).days <= 115),
            None,
        )
        if q1 is None:
            continue
        fy_prefix = half.label.split("-")[0] if half.label else None
        out.append(Period(
            q1.end + timedelta(days=1), half.end, "quarter", half.value - q1.value,
            unit, tag, f"{fy_prefix}-Q2" if fy_prefix else None, half.form, half.filed,
            half.accession, None, True, "6M YTD minus Q1, same tag",
        ))
        seen_ends.add(half.end)

    for nine in sorted(nines, key=lambda p: p.end, reverse=True):
        if nine.end in seen_ends or nine.start is None:
            continue
        half = next(
            (h for h in halves if near(h.start, nine.start) and 60 <= (nine.end - h.end).days <= 115),
            None,
        )
        if half is None:
            continue
        fy_prefix = nine.label.split("-")[0] if nine.label else None
        out.append(Period(
            half.end + timedelta(days=1), nine.end, "quarter", nine.value - half.value,
            unit, tag, f"{fy_prefix}-Q3" if fy_prefix else None, nine.form, nine.filed,
            nine.accession, None, True, "9M YTD minus 6M YTD, same tag",
        ))
        seen_ends.add(nine.end)

    for annual in sorted(years, key=lambda p: p.end, reverse=True):
        if annual.end in seen_ends or annual.start is None:
            continue
        nine = next(
            (n for n in nines if near(n.start, annual.start) and 60 <= (annual.end - n.end).days <= 115),
            None,
        )
        if nine is None:
            continue
        fy_prefix = annual.label.split("-")[0] if annual.label else None
        out.append(Period(
            nine.end + timedelta(days=1), annual.end, "quarter", annual.value - nine.value,
            unit, tag, f"{fy_prefix}-Q4" if fy_prefix else None, annual.form, annual.filed,
            annual.accession, None, True, "FY minus 9M YTD, same tag",
        ))
        seen_ends.add(annual.end)

    return out


def _derive_q4(raw: Iterable[dict], tag: str, unit: str, have_ends: set[date]) -> list[Period]:
    quarters = {p.start: p for p in _collect(raw, "quarter", flow=True, derive_q4=False, tag=tag, unit=unit)}
    out = []
    near = lambda a, b: a is not None and b is not None and abs((a - b).days) <= 5
    for annual in _collect(raw, "year", flow=True, derive_q4=False, tag=tag, unit=unit):
        if annual.end in have_ends:
            continue
        chain, cursor = [], annual.start
        for _ in range(3):
            match = next((q for s, q in quarters.items() if near(s, cursor) and annual.start <= q.end < annual.end), None)
            if match is None:
                break
            chain.append(match)
            cursor = match.end + timedelta(days=1)
        if len(chain) == 3 and 60 <= (annual.end - chain[2].end).days <= 110:
            out.append(Period(chain[2].end + timedelta(days=1), annual.end, "quarter",
                              annual.value - sum(q.value for q in chain), unit, tag,
                              (annual.label.split("-")[0] + "-Q4") if annual.label else None, annual.form,
                              annual.filed, annual.accession, None, True, "FY minus (Q1+Q2+Q3), same tag"))
    return out


def last_n_quarters(entries: Iterable[dict], n: int, *, flow: bool = True, derive_q4: bool = False,
                    tag: str = "", unit: str = "") -> list[Period]:
    """Distinct quarterly periods (by end date), newest first. Q4 is derived only on request and marked."""
    return _collect(entries, "quarter", flow=flow, derive_q4=derive_q4, tag=tag, unit=unit)[:n]


def last_n_fiscal_years(entries: Iterable[dict], n: int, *, flow: bool = True, tag: str = "", unit: str = "") -> list[Period]:
    return _collect(entries, "year", flow=flow, derive_q4=False, tag=tag, unit=unit)[:n]


def _pick_unit(units: dict, unit_kind: str) -> str | None:
    names = [u for u, rows in units.items() if isinstance(rows, list) and rows]
    if unit_kind == "per_share":
        ok = [u for u in names if "/" in u or "per" in u.lower()]
        if not ok:
            return None
        return "USD/shares" if "USD/shares" in ok else ("USD" if "USD" in ok else sorted(ok)[0])
    if unit_kind == "shares":
        ok = [u for u in names if "/" not in u and "per" not in u.lower() and "share" in u.lower()]
        if not ok:
            return None
        return "shares" if "shares" in ok else sorted(ok)[0]
    ok = [u for u in names if "/" not in u and "share" not in u.lower() and "per" not in u.lower()]
    if not ok:
        return None
    return "USD" if "USD" in ok else sorted(ok)[0]


def _currency(unit: str) -> str | None:
    head = unit.split("/")[0]
    return head if re.fullmatch(r"[A-Z]{3}", head) else None


def _quantize(value: Decimal, decimal_places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimal_places), rounding=ROUND_HALF_UP)


def _period_class(line: StatementLine) -> str | None:
    spec = STANDARD_LINES.get(line.line)
    if spec is None:
        return None
    if not spec.flow:
        return "instant" if line.period_start is None else None
    if line.period_start is None:
        return None
    if line.basis == "ytd_cumulative" and line.period_start == date(line.period_end.year, 1, 1):
        if line.period_end.month in (3, 6, 9, 12):
            return f"ytd_{line.period_end.month}"
        return None
    days = (line.period_end - line.period_start).days
    if 78 <= days <= 105:
        return "quarter"
    if 340 <= days <= 380:
        return "year"
    return None


def _is_single_quarter_flow(line: StatementLine) -> bool:
    cls = _period_class(line)
    return cls in ("quarter", "ytd_3")


def _derive_margins_and_fcf(
    by_period: dict[tuple[str, str, date | None, date, str | None, str, str | None], dict[str, StatementLine]],
    decimal_places: int,
) -> list[StatementLine]:
    derived: list[StatementLine] = []
    ordered_periods = sorted(
        by_period.items(),
        key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or date.min, kv[0][3], kv[0][4] or "", kv[0][5], kv[0][6] or ""),
    )
    for (eid, taxonomy, p_start, p_end, _basis, unit, currency), bucket in ordered_periods:
        ocf = bucket.get("operating_cash_flow")
        capex = bucket.get("capex")
        if ocf is not None and capex is not None and STANDARD_LINES["operating_cash_flow"].unit_kind == "money":
            derived.append(StatementLine(
                entity_id=eid, statement="cashflow", line="free_cash_flow",
                reported_tag=f"{ocf.reported_tag}-{capex.reported_tag}", taxonomy=taxonomy,
                period_start=p_start, period_end=p_end, fiscal_label=ocf.fiscal_label or capex.fiscal_label,
                value=ocf.value - abs(capex.value), unit=unit, currency=currency,
                form=ocf.form or capex.form,
                filed_at=max((d for d in (ocf.filed_at, capex.filed_at) if d is not None), default=None),
                accession=ocf.accession or capex.accession, derived=True,
                derivation=f"operating_cash_flow ({ocf.value}) - abs(capex ({capex.value}))",
                basis=ocf.basis, accounting_standard=ocf.accounting_standard,
            ))

        rev = bucket.get("revenue")
        if rev is None:
            continue
        gp = bucket.get("gross_profit")
        cost = bucket.get("cost_of_revenue")
        if gp is None and cost is not None and cost.value >= 0:
            gp = StatementLine(
                entity_id=eid, statement="income", line="gross_profit",
                reported_tag=f"{rev.reported_tag}-{cost.reported_tag}", taxonomy=taxonomy,
                period_start=p_start, period_end=p_end, fiscal_label=rev.fiscal_label,
                value=rev.value - cost.value, unit=unit, currency=currency,
                form=rev.form or cost.form,
                filed_at=max((d for d in (rev.filed_at, cost.filed_at) if d is not None), default=None),
                accession=rev.accession or cost.accession, derived=True,
                derivation=f"revenue ({rev.value}) - cost_of_revenue ({cost.value})",
                basis=rev.basis, accounting_standard=rev.accounting_standard,
            )
            derived.append(gp)
        if rev.value <= 0:
            continue
        margin_basis = "period_ratio" if rev.basis else None
        for target_line, source_line, obj in (
            ("gross_margin", "gross_profit", gp),
            ("operating_margin", "operating_income", bucket.get("operating_income")),
            ("net_margin", "net_income", bucket.get("net_income")),
        ):
            if obj is None:
                continue
            pct = _quantize((obj.value / rev.value) * Decimal(100), decimal_places)
            derived.append(StatementLine(
                entity_id=eid, statement="income", line=target_line,
                reported_tag=f"{obj.reported_tag}/{rev.reported_tag}", taxonomy=taxonomy,
                period_start=p_start, period_end=p_end, fiscal_label=rev.fiscal_label,
                value=pct, unit="percent", currency=None,
                form=rev.form or obj.form,
                filed_at=max((d for d in (rev.filed_at, obj.filed_at) if d is not None), default=None),
                accession=rev.accession or obj.accession, derived=True,
                derivation=f"{source_line} ({obj.value}) / revenue ({rev.value}) * 100",
                basis=margin_basis, accounting_standard=rev.accounting_standard,
            ))
    return derived


def _derive_ttm_rows(
    series_items: list[StatementLine],
    eid: str,
    taxonomy: str,
    line_name: str,
    unit: str,
    currency: str | None,
) -> list[StatementLine]:
    spec = STANDARD_LINES.get(line_name)
    if spec is None or not spec.flow or spec.unit_kind != "money":
        return []
    out: list[StatementLine] = []
    covered_ends: set[date] = set()
    # Path 1: 4 contiguous single quarters.
    quarters_by_end: dict[date, StatementLine] = {}
    for item in series_items:
        if _is_single_quarter_flow(item):
            cur = quarters_by_end.get(item.period_end)
            if cur is None or (cur.derived and not item.derived):
                quarters_by_end[item.period_end] = item
    quarters = [quarters_by_end[d] for d in sorted(quarters_by_end)]
    for i in range(len(quarters) - 3):
        win = quarters[i:i + 4]
        assert all(q.period_start is not None for q in win)
        contiguous = all(
            abs(((win[j + 1].period_start - win[j].period_end).days) - 1) <= 5  # type: ignore[operator]
            for j in range(3)
        )
        total_days = (win[3].period_end - win[0].period_start).days  # type: ignore[operator]
        if not contiguous or not 340 <= total_days <= 385:
            continue
        covered_ends.add(win[3].period_end)
        out.append(StatementLine(
            entity_id=eid, statement=spec.statement, line=f"{line_name}_ttm",
            reported_tag=win[3].reported_tag, taxonomy=taxonomy,
            period_start=win[0].period_start, period_end=win[3].period_end,
            fiscal_label=f"TTM {win[3].period_end.isoformat()}",
            value=sum((q.value for q in win), Decimal(0)), unit=unit, currency=currency,
            form=win[3].form,
            filed_at=max((q.filed_at for q in win if q.filed_at is not None), default=None),
            accession=win[3].accession, derived=True,
            derivation=f"TTM sum of 4 quarters ({win[0].period_end.isoformat()}..{win[3].period_end.isoformat()})",
            basis="ttm", accounting_standard=win[3].accounting_standard,
        ))
    # Path 2: YTD cumulative series (A-shares without derive_quarters): TTM = cur_YTD + prev_FY - prev_YTD.
    ytd_by_end = {
        item.period_end: item for item in series_items
        if item.basis == "ytd_cumulative" and item.period_start == date(item.period_end.year, 1, 1)
    }
    for end, cur in sorted(ytd_by_end.items()):
        if end in covered_ends or end.month not in (3, 6, 9):
            continue
        prev_fy = ytd_by_end.get(date(end.year - 1, 12, 31))
        prev_ytd = next(
            (v for d, v in ytd_by_end.items() if d.year == end.year - 1 and d.month == end.month),
            None,
        )
        if prev_fy is None or prev_ytd is None:
            continue
        out.append(StatementLine(
            entity_id=eid, statement=spec.statement, line=f"{line_name}_ttm",
            reported_tag=cur.reported_tag, taxonomy=taxonomy,
            period_start=prev_ytd.period_end + timedelta(days=1), period_end=end,
            fiscal_label=f"TTM {end.isoformat()}",
            value=cur.value + prev_fy.value - prev_ytd.value, unit=unit, currency=currency,
            form=cur.form,
            filed_at=max((d for d in (cur.filed_at, prev_fy.filed_at, prev_ytd.filed_at) if d is not None), default=None),
            derived=True,
            derivation=(f"TTM from YTD ({cur.fiscal_label or end.isoformat()}) + "
                        f"{prev_fy.fiscal_label or prev_fy.period_end.isoformat()} - "
                        f"{prev_ytd.fiscal_label or prev_ytd.period_end.isoformat()}"),
            basis="ttm", accounting_standard=cur.accounting_standard,
        ))
    return out


def _derive_growth_rows(
    series_items: list[StatementLine],
    eid: str,
    taxonomy: str,
    line_name: str,
    decimal_places: int,
) -> list[StatementLine]:
    spec = STANDARD_LINES.get(line_name)
    if spec is None:
        return []
    out: list[StatementLine] = []
    seen_yoy: set[tuple[date | None, date, str | None]] = set()
    seen_qoq: set[tuple[date | None, date, str | None]] = set()
    ordered = sorted(series_items, key=lambda s: (s.period_end, s.period_start or date.min))
    for cur in ordered:
        cls = _period_class(cur)
        if cls is None:
            continue
        # YoY growth
        yoy_candidates: list[StatementLine] = []
        for prev in ordered:
            if prev.period_end >= cur.period_end:
                continue
            p_cls = _period_class(prev)
            if cls.startswith("ytd_"):
                if p_cls == cls and prev.period_end.year == cur.period_end.year - 1 and prev.period_end.month == cur.period_end.month:
                    yoy_candidates.append(prev)
            elif cls == "quarter" and p_cls in ("quarter", "ytd_3"):
                if 330 <= (cur.period_end - prev.period_end).days <= 400:
                    yoy_candidates.append(prev)
            elif p_cls == cls and 330 <= (cur.period_end - prev.period_end).days <= 400:
                yoy_candidates.append(prev)
        if yoy_candidates:
            prev = min(
                yoy_candidates,
                key=lambda p: (
                    0 if (cls == "instant" and cur.form and p.form == cur.form) else 1,
                    abs((cur.period_end - p.period_end).days - 365),
                    int(p.derived),
                ),
            )
            yoy_key = (cur.period_start, cur.period_end, cur.basis)
            if prev.value > 0 and yoy_key not in seen_yoy:
                seen_yoy.add(yoy_key)
                pct = _quantize(((cur.value / prev.value) - Decimal(1)) * Decimal(100), decimal_places)
                out.append(StatementLine(
                    entity_id=eid, statement=spec.statement, line=f"{line_name}_yoy_growth",
                    reported_tag=cur.reported_tag, taxonomy=taxonomy,
                    period_start=cur.period_start, period_end=cur.period_end, fiscal_label=cur.fiscal_label,
                    value=pct, unit="percent", currency=None,
                    form=cur.form, filed_at=cur.filed_at, accession=cur.accession, derived=True,
                    derivation=f"YoY growth vs {prev.period_end.isoformat()} ({prev.value})",
                    basis=cur.basis, accounting_standard=cur.accounting_standard,
                ))
        # QoQ growth (single-quarter flows or quarterly instant snapshots; never cumulative H1/9M/FY)
        qoq_candidates: list[StatementLine] = []
        if _is_single_quarter_flow(cur):
            for prev in ordered:
                if prev.period_end >= cur.period_end or not _is_single_quarter_flow(prev):
                    continue
                if cur.period_start is not None and abs((cur.period_start - prev.period_end).days - 1) <= 5:
                    qoq_candidates.append(prev)
        elif cls == "instant":
            for prev in ordered:
                if prev.period_end >= cur.period_end or _period_class(prev) != "instant":
                    continue
                if 60 <= (cur.period_end - prev.period_end).days <= 120:
                    qoq_candidates.append(prev)
        if qoq_candidates:
            prev = min(
                qoq_candidates,
                key=lambda p: (abs((cur.period_end - p.period_end).days - 91), int(p.derived)),
            )
            qoq_key = (cur.period_start, cur.period_end, cur.basis)
            if prev.value > 0 and qoq_key not in seen_qoq:
                seen_qoq.add(qoq_key)
                pct = _quantize(((cur.value / prev.value) - Decimal(1)) * Decimal(100), decimal_places)
                out.append(StatementLine(
                    entity_id=eid, statement=spec.statement, line=f"{line_name}_qoq_growth",
                    reported_tag=cur.reported_tag, taxonomy=taxonomy,
                    period_start=cur.period_start, period_end=cur.period_end, fiscal_label=cur.fiscal_label,
                    value=pct, unit="percent", currency=None,
                    form=cur.form, filed_at=cur.filed_at, accession=cur.accession, derived=True,
                    derivation=f"QoQ growth vs {prev.period_end.isoformat()} ({prev.value})",
                    basis=cur.basis, accounting_standard=cur.accounting_standard,
                ))
    return out


def derive_period_metrics(lines: Iterable[StatementLine], *, decimal_places: int = 2) -> list[StatementLine]:
    """Deterministically compute FCF, margins, TTM sums, and YoY/QoQ growth from standard StatementLines.

    All outputs use ``Decimal`` arithmetic and carry ``derived=True`` with an explicit ``derivation`` string.
    Does not mutate or cascade over already-derived metric lines.
    """
    if not 0 <= decimal_places <= 8:
        raise ValueError("decimal_places must be between 0 and 8")
    # Deduplicate base standard lines (keeping non-derived over derived for the exact same period & basis).
    deduped: dict[tuple[str, str, str, date | None, date, str | None, str, str | None], StatementLine] = {}
    for item in lines:
        if item.line not in STANDARD_LINES or not item.value.is_finite():
            continue
        # Only feed reported base lines and derived single-quarter lines into period derivations;
        # never cascade over outputs previously produced by derive_period_metrics().
        if (
            item.derived
            and item.basis != "single_quarter_derived"
            and not (item.derivation or "").startswith(("FY minus", "6M YTD minus", "9M YTD minus"))
        ):
            continue
        key = (item.entity_id, item.taxonomy, item.line, item.period_start, item.period_end,
               item.basis, item.unit, item.currency)
        cur = deduped.get(key)
        if cur is None or (cur.derived and not item.derived):
            deduped[key] = item
    if not deduped:
        return []

    by_period: dict[tuple[str, str, date | None, date, str | None, str, str | None], dict[str, StatementLine]] = {}
    by_series: dict[tuple[str, str, str, str, str | None], list[StatementLine]] = {}
    for item in deduped.values():
        spec = STANDARD_LINES[item.line]
        if spec.unit_kind == "money":
            p_key = (item.entity_id, item.taxonomy, item.period_start, item.period_end,
                     item.basis, item.unit, item.currency)
            by_period.setdefault(p_key, {})[item.line] = item
        s_key = (item.entity_id, item.taxonomy, item.line, item.unit, item.currency)
        by_series.setdefault(s_key, []).append(item)

    with localcontext() as ctx:
        ctx.prec = 100
        out: list[StatementLine] = []
        out.extend(_derive_margins_and_fcf(by_period, decimal_places))
        ordered_series = sorted(
            by_series.items(),
            key=lambda kv: (kv[0][0], kv[0][1], kv[0][2], kv[0][3], kv[0][4] or ""),
        )
        for (eid, taxonomy, line_name, unit, currency), series_items in ordered_series:
            out.extend(_derive_ttm_rows(series_items, eid, taxonomy, line_name, unit, currency))
            out.extend(_derive_growth_rows(series_items, eid, taxonomy, line_name, decimal_places))
    out.sort(key=lambda s: (s.statement, s.line, s.period_end, s.period_start or date.min))
    return out


def select_line(
    facts: dict,
    spec: LineSpec,
    kind: str,
    n: int,
    *,
    derive_q4: bool,
    taxonomy: str,
    as_of: date | None = None,
) -> list[Period]:
    """Per period choose the first tag in priority order that reported it (handles tag switches).

    When ``as_of`` is provided, strictly filters out facts with ``filed > as_of`` (or
    ``filed is None and end > as_of``) before period selection to guarantee zero lookahead leakage.
    """
    chosen: dict[date, Period] = {}
    for tag in spec.tags.get(taxonomy, ()):
        node = facts.get(taxonomy, {}).get(tag)
        units = node.get("units") if isinstance(node, dict) else None
        if not isinstance(units, dict):
            continue
        unit = _pick_unit(units, spec.unit_kind)
        if unit is None:
            continue
        raw_rows = units[unit]
        if as_of is not None:
            filtered_rows = []
            for row in raw_rows:
                if not isinstance(row, dict):
                    continue
                f_d = _d(row.get("filed"))
                e_d = _d(row.get("end"))
                if f_d is not None:
                    if f_d > as_of:
                        continue
                elif e_d is not None and e_d > as_of:
                    continue
                filtered_rows.append(row)
            raw_rows = filtered_rows
        for p in _collect(raw_rows, kind, flow=spec.flow, derive_q4=derive_q4 and spec.unit_kind == "money",
                          tag=tag, unit=unit):
            if as_of is not None and p.filed is not None and p.filed > as_of:
                continue
            cur = chosen.get(p.end)
            # Same period under several tags: the latest filing wins; ties keep tag priority.
            if cur is None or (p.filed or date.min) > (cur.filed or date.min):
                chosen[p.end] = p
    return sorted(chosen.values(), key=lambda p: p.end, reverse=True)[:n]


# --------------------------------------------------------------------- adapter
class SecEdgarAdapter:
    STANDARD_LINES = STANDARD_LINES

    def __init__(self, user_agent: str | None, *, transport: Transport | None = None, limiter=None,
                 policy: FinDataPolicy | None = None, now_fn: Callable[[], datetime] = utcnow,
                 timeout: float = 15.0, stream_transport: StreamTransport | None = None) -> None:
        self._ua = user_agent
        self._transport = transport or default_transport()
        # Large JSON (companyfacts for big issuers exceeds 5 MB) streams to a temp file. An injected plain
        # transport without a stream transport keeps the old in-memory behaviour (tests, fakes).
        self._stream = stream_transport if stream_transport is not None else (
            default_stream_transport() if transport is None else None)
        self._limiter = limiter or default_limiter()
        self._policy = policy or load_policy()
        self._now = now_fn
        self._timeout = timeout

    # -- gates
    def _gate(self, category: str) -> Permissions | ToolResult:
        decision = self._policy.decide(PROVIDER, category)
        if isinstance(decision, Denied):
            return ToolResult.failure("denied", decision.reason, decision.message)
        try:
            validate_user_agent(self._ua)
        except SecFailure as error:
            return ToolResult.failure("failed", error.code, "SEC requires a declared User-Agent with contact details.")
        return decision

    def _get(self, url: str, category: str, not_found_reason: str = "not_found", *, large: bool = False,
             max_bytes: int | None = None):
        gate = self._gate(category)
        if isinstance(gate, ToolResult):
            raise StopWith(gate)
        transport, stream = self._transport, (self._stream if large else None)
        if max_bytes is not None:
            if stream is not None:
                inner_stream = stream
                stream = lambda method, address, headers, body, timeout, sink: inner_stream(
                    method, address, headers, body, timeout, _CappedSink(sink, max_bytes))
            else:
                inner = self._transport

                def transport(method, address, headers, body, timeout):
                    reply = inner(method, address, headers, body, timeout)
                    if len(reply.body or b"") > max_bytes:
                        raise _TransportFailure("response too large")
                    return reply
        doc, prov, cost = request_json(provider=PROVIDER, url=url, headers={"User-Agent": self._ua or ""},
                                       transport=transport, timeout=self._timeout, limiter=self._limiter,
                                       now_fn=self._now, not_found_reason=not_found_reason,
                                       stream_transport=stream)
        return doc, prov, cost, gate

    # -- identity
    def company_identity(self, query: str) -> ToolResult:
        gate = self._gate("identity")
        if isinstance(gate, ToolResult):
            return gate
        decision = self._limiter.acquire(PROVIDER)
        if not decision.allowed:
            from .base import rate_limited_result
            return rate_limited_result(decision, PROVIDER)

        def send(method, url, headers, body, timeout):
            reply = self._transport(method, url, headers, body, timeout)
            return HttpResponse(reply.status_code, reply.body.decode("utf-8", "replace"), reply.headers)
        try:
            found = search_sec_identities(SecIdentityRequest(query=query), self._ua, send)
        except SecFailure as error:
            status = "rate_limited" if error.code == "source_rate_limited" else "failed"
            return ToolResult.failure(status, error.code, "SEC ticker index lookup failed.",
                                      retryable=error.code in ("source_rate_limited", "source_timeout", "source_unavailable"))
        from ..models import Cost, Provenance
        prov = Provenance(provider=PROVIDER, endpoint=TICKERS_URL, retrieved_at=self._now(),
                          response_sha256=found["response_sha256"])
        refs = [EntityRef(entity_id=cik_entity_id(c["cik"]), company_name=c["name"], symbol=c["ticker"],
                          identifiers={"cik": c["cik"]}) for c in found["candidates"]]
        cov = Coverage(rows=len(refs), notes=[found["limitation"]])
        status, reason = ("ok", "ok") if refs else ("empty", "no_match")
        return ToolResult(status=status, reason=reason, coverage=cov, permissions=gate, provenance=prov,
                          cost=Cost(provider_calls=1), data=refs)

    # -- submissions
    def submissions(self, cik: str | int, *, forms: Iterable[str] | None = None, start: date | None = None,
                    end: date | None = None, limit: int = 100, include_amendments: bool = True,
                    entity_id: str | None = None) -> ToolResult:
        c = cik10(cik)
        eid = entity_id or cik_entity_id(c)
        try:
            doc, prov, cost, perms = self._get(f"https://data.sec.gov/submissions/CIK{c}.json", "filings", "cik_not_found",
                                              large=True)
        except StopWith as stop:
            return stop.result
        recent = (doc.get("filings") or {}).get("recent") if isinstance(doc, dict) else None
        if not isinstance(recent, dict) or not isinstance(recent.get("accessionNumber"), list):
            return ToolResult.failure("failed", "invalid_response", "Unexpected submissions shape.", provenance=prov, cost=cost)
        wanted = {f.strip().upper() for f in forms} if forms else None
        col = lambda name, i: (recent.get(name) or [None] * (i + 1))[i] if i < len(recent.get(name) or []) else None
        rows: list[Filing] = []
        earliest: date | None = None
        for i, acc in enumerate(recent["accessionNumber"]):
            filed = _d(col("filingDate", i))
            form = col("form", i)
            if filed is None or not isinstance(form, str) or not isinstance(acc, str):
                continue
            earliest = filed if earliest is None or filed < earliest else earliest
            base = form.upper().removesuffix("/A") if include_amendments else form.upper()
            if wanted is not None and form.upper() not in wanted and base not in wanted:
                continue
            if (start and filed < start) or (end and filed > end):
                continue
            nodash = acc.replace("-", "")
            primary = col("primaryDocument", i)
            accepted = col("acceptanceDateTime", i)
            try:
                accepted_dt = datetime.fromisoformat(accepted.replace("Z", "+00:00")) if accepted else None
            except ValueError:
                accepted_dt = None
            if accepted_dt is not None and accepted_dt.tzinfo is None:
                accepted_dt = accepted_dt.replace(tzinfo=timezone.utc)
            base_url = f"https://www.sec.gov/Archives/edgar/data/{int(c)}/{nodash}"
            rows.append(Filing(entity_id=eid, form=form, filed_at=filed, accepted_at=accepted_dt,
                               period_of_report=_d(col("reportDate", i)), accession=acc,
                               primary_document_url=f"{base_url}/{primary}" if primary else None,
                               index_url=f"{base_url}/{acc}-index.htm",
                               description=col("primaryDocDescription", i) or None))
        rows.sort(key=lambda f: (f.filed_at, f.accession), reverse=True)
        truncated = len(rows) > limit
        rows = rows[:limit]
        older = bool((doc.get("filings") or {}).get("files"))
        notes = ["Filing date only; acceptance time is in accepted_at when provided."]
        partial_reason = None
        if older and (start is None or (earliest and start < earliest)):
            notes.append(f"Only the recent-filings window (earliest {earliest}) was loaded; older filing files exist.")
            if start is not None:
                partial_reason = "older_filings_not_loaded"
        cov = Coverage(entity_id=eid, requested={"start": start and start.isoformat(), "end": end and end.isoformat()},
                       actual={"first": rows[-1].filed_at.isoformat() if rows else None,
                               "last": rows[0].filed_at.isoformat() if rows else None},
                       rows=len(rows), data_as_of=prov.retrieved_at.date(), is_final=True, truncated=truncated, notes=notes)
        if not rows:
            return ToolResult(status="empty", reason="no_matching_filings", coverage=cov, permissions=perms,
                              provenance=prov, cost=cost)
        status, reason = ("partial", partial_reason or "truncated") if (partial_reason or truncated) else ("ok", "ok")
        return ToolResult(status=status, reason=reason, coverage=cov, permissions=perms, provenance=prov, cost=cost, data=rows)

    # -- companyfacts
    def _concept_facts(self, c: str, wanted: list[str], *, period: str, n: int, derive_q4: bool, taxonomy: str):
        """Build a companyfacts-shaped ``facts`` dict from per-tag companyconcept files (small, in priority order).

        Per line, ALL candidate tags are fetched (a few per line; a company may switch tags over time, so
        stopping early could return stale periods); each tag is fetched at most once. Returns
        ``(facts, provenance, cost, permissions)`` or a failing ``ToolResult``. A 404 for a tag only means
        the issuer never reported it.
        """
        from ..models import Cost, Provenance
        facts: dict[str, dict] = {taxonomy: {}}
        tried: set[str] = set()
        hashes: list[str] = []
        urls: list[str] = []
        calls = 0
        perms: Permissions | None = None
        for name in wanted:
            spec = STANDARD_LINES[name]
            for tag in spec.tags.get(taxonomy, ()):
                if tag in tried:
                    continue
                tried.add(tag)
                url = CONCEPT_URL.format(cik=c, taxonomy=taxonomy, tag=tag)
                calls += 1
                try:
                    doc, prov, cost, perms = self._get(url, "financials", "no_xbrl_facts")
                except StopWith as stop:
                    if stop.result.reason == "no_xbrl_facts":
                        continue
                    return stop.result
                units = doc.get("units") if isinstance(doc, dict) else None
                if not isinstance(units, dict):
                    return ToolResult.failure("failed", "invalid_response", "Unexpected companyconcept shape.",
                                              provenance=prov, cost=Cost(provider_calls=calls))
                facts[taxonomy][tag] = doc
                hashes.append(prov.response_sha256 or "")
                urls.append(url)
        gate = perms or self._gate("financials")
        if isinstance(gate, ToolResult):
            return gate
        digest = sha256("\n".join(sorted(hashes)).encode()).hexdigest() if hashes else None
        prov = Provenance(provider=PROVIDER, endpoint=urls[0] if urls else CONCEPT_URL.format(cik=c, taxonomy=taxonomy, tag="*"),
                          retrieved_at=self._now(), response_sha256=digest, endpoints=urls)
        return facts, prov, Cost(provider_calls=calls), gate

    def statement_lines(self, cik: str | int, *, lines: Iterable[str] | None = None, period: str = "quarter",
                        n: int = 8, derive_q4: bool = False, taxonomy: str = "us-gaap",
                        entity_id: str | None = None) -> ToolResult:
        if period not in ("quarter", "year") or n < 1 or taxonomy not in ("us-gaap", "ifrs-full"):
            return ToolResult.failure("failed", "invalid_request", "period must be quarter|year, n>=1, taxonomy us-gaap|ifrs-full.")
        wanted = list(lines) if lines else list(STANDARD_LINES)
        unknown = [l for l in wanted if l not in STANDARD_LINES]
        if unknown:
            return ToolResult.failure("failed", "unknown_line", f"Unknown standard lines: {', '.join(unknown)}.",
                                      suggested_actions=[SuggestedAction(action="company_financials",
                                                                         hint="lines in " + ",".join(STANDARD_LINES))])
        c = cik10(cik)
        eid = entity_id or cik_entity_id(c)
        if lines:
            loaded = self._concept_facts(c, wanted, period=period, n=n, derive_q4=derive_q4, taxonomy=taxonomy)
            if isinstance(loaded, ToolResult):
                return loaded
            facts, prov, cost, perms = loaded
            if not facts.get(taxonomy):
                return ToolResult(status="empty", reason="no_matching_lines", permissions=perms, provenance=prov, cost=cost,
                                  coverage=Coverage(entity_id=eid, rows=0),
                                  message="No requested line was reported under the standard tag mapping.")
        else:
            try:
                doc, prov, cost, perms = self._get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{c}.json", "financials",
                                                   "no_xbrl_facts", large=True, max_bytes=MAX_COMPANYFACTS_BYTES)
            except StopWith as stop:
                if stop.result.reason == "source_too_large":
                    return ToolResult.failure(
                        "failed", "source_too_large",
                        f"The full companyfacts document exceeds {MAX_COMPANYFACTS_BYTES // 1_000_000} MB; request specific "
                        "lines (for example lines=['revenue','net_income']) to use the small per-concept files.",
                        cost=stop.result.cost,
                        suggested_actions=[SuggestedAction(action="company_financials", hint="pass lines=[...] from " + ",".join(STANDARD_LINES))])
                return stop.result
            facts = doc.get("facts") if isinstance(doc, dict) else None
            if not isinstance(facts, dict):
                return ToolResult.failure("failed", "invalid_response", "Unexpected companyfacts shape.", provenance=prov, cost=cost)
        out: list[StatementLine] = []
        short, missing = [], []
        for name in wanted:
            spec = STANDARD_LINES[name]
            kind = period
            periods = select_line(facts, spec, kind, n, derive_q4=derive_q4 and period == "quarter", taxonomy=taxonomy)
            if not periods:
                missing.append(name)
                continue
            if len(periods) < n:
                short.append(name)
            for p in periods:
                out.append(StatementLine(entity_id=eid, statement=spec.statement, line=name, reported_tag=p.tag,
                                         taxonomy=taxonomy, period_start=p.start, period_end=p.end, fiscal_label=p.label,
                                         value=p.value, unit=p.unit, currency=_currency(p.unit), form=p.form,
                                         filed_at=p.filed, accession=p.accession, frame=p.frame,
                                         derived=p.derived, derivation=p.derivation))
        out.sort(key=lambda s: (s.line, s.period_end), reverse=False)
        notes = [f"Requested last {n} {period}s per line; one tag chosen per period by priority; latest filed value "
                 f"(restatements win); fiscal_label comes from the filing that first reported the period."]
        if any(s.derived for s in out):
            notes.append("Derived Q4 rows are annual minus Q1-Q3 and are marked derived=true.")
        ends = [s.period_end for s in out]
        filed = [s.filed_at for s in out if s.filed_at]
        cov = Coverage(entity_id=eid, requested={"start": None, "end": None, "periods": f"last_{n}_{period}s"},
                       actual={"first": min(ends).isoformat() if ends else None, "last": max(ends).isoformat() if ends else None},
                       rows=len(out), data_as_of=max(filed) if filed else None, is_final=True, notes=notes)
        if not out:
            return ToolResult(status="empty", reason="no_matching_lines", coverage=cov, permissions=perms,
                              provenance=prov, cost=cost,
                              message="No requested line was reported under the standard tag mapping.")
        if missing or short:
            msg = (f"No data for: {', '.join(missing)}. " if missing else "") + \
                  (f"Fewer than {n} periods for: {', '.join(short)}." if short else "")
            return ToolResult(status="partial", reason="missing_lines" if missing else "fewer_periods_than_requested",
                              message=msg.strip(), coverage=cov, permissions=perms, provenance=prov, cost=cost, data=out)
        return ToolResult(status="ok", reason="ok", coverage=cov, permissions=perms, provenance=prov, cost=cost, data=out)

    # -- frames
    def frames(self, taxonomy: str, tag: str, unit: str, period: str, *, ciks: Iterable[str | int] | None = None,
               limit: int = 500) -> ToolResult:
        if (not re.fullmatch(r"[a-z0-9-]+", taxonomy) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", tag)
                or not re.fullmatch(r"[A-Za-z0-9-]+", unit) or not re.fullmatch(r"CY\d{4}(Q[1-4])?I?", period)):
            return ToolResult.failure("failed", "invalid_request",
                                      "Use taxonomy, tag, unit (e.g. USD, USD-per-shares) and period like CY2023Q4I.")
        url = f"https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json"
        try:
            doc, prov, cost, perms = self._get(url, "financials", "frame_not_found", large=True)
        except StopWith as stop:
            return stop.result
        data = doc.get("data") if isinstance(doc, dict) else None
        if not isinstance(data, list):
            return ToolResult.failure("failed", "invalid_response", "Unexpected frames shape.", provenance=prov, cost=cost)
        only = {cik10(c) for c in ciks} if ciks else None
        rows: list[FrameRow] = []
        for item in data:
            try:
                cik = cik10(item["cik"])
                if only is not None and cik not in only:
                    continue
                rows.append(FrameRow(entity_id=cik_entity_id(cik), cik=cik, entity_name=item.get("entityName"),
                                     period_end=date.fromisoformat(item["end"]), value=Decimal(str(item["val"])),
                                     accession=item.get("accn"), location=item.get("loc")))
            except (KeyError, ValueError, TypeError, InvalidOperation):
                continue
        rows.sort(key=lambda r: r.value, reverse=True)
        truncated = len(rows) > limit
        rows = rows[:limit]
        cov = Coverage(requested={"period": period}, actual={"first": None, "last": None}, rows=len(rows),
                       data_as_of=prov.retrieved_at.date(), truncated=truncated,
                       notes=["One value per company for the calendar-aligned frame; sorted by value descending.",
                              f"Frame holds {len(data)} points before filtering."])
        if not rows:
            return ToolResult(status="empty", reason="no_matching_rows", coverage=cov, permissions=perms, provenance=prov, cost=cost)
        return ToolResult(status="partial" if truncated else "ok", reason="truncated" if truncated else "ok",
                          coverage=cov, permissions=perms, provenance=prov, cost=cost, data=rows)

    # -- full-text search
    def fulltext_search(self, query: str, *, start: date | None = None, end: date | None = None,
                        forms: Iterable[str] | None = None, limit: int = 50) -> ToolResult:
        if not query.strip():
            return ToolResult.failure("failed", "invalid_request", "query must not be blank.")
        params = {"q": query}
        if start or end:
            params.update({"dateRange": "custom", "startdt": (start or date(2001, 1, 1)).isoformat(),
                           "enddt": (end or self._now().date()).isoformat()})
        if forms:
            params["forms"] = ",".join(forms)
        try:
            doc, prov, cost, perms = self._get(f"{EFTS_URL}?{urlencode(params, quote_via=quote)}", "filing_search")
        except StopWith as stop:
            return stop.result
        hits = ((doc.get("hits") or {}).get("hits")) if isinstance(doc, dict) else None
        if not isinstance(hits, list):
            return ToolResult.failure("failed", "invalid_response", "Unexpected full-text search shape.", provenance=prov, cost=cost)
        total = ((doc["hits"].get("total") or {}).get("value"))
        rows: list[FullTextHit] = []
        for h in hits[:limit]:
            src = h.get("_source") or {}
            ciks = [cik10(c) for c in src.get("ciks") or [] if str(c).isdigit()]
            acc = src.get("adsh")
            document = str(h.get("_id", "")).partition(":")[2] or None
            url = (f"https://www.sec.gov/Archives/edgar/data/{int(ciks[0])}/{acc.replace('-', '')}/{document}"
                   if ciks and acc and document else None)
            names = src.get("display_names") or []
            rows.append(FullTextHit(entity_id=cik_entity_id(ciks[0]) if ciks else None, ciks=ciks,
                                    display_name=names[0] if names else None, form=src.get("form"),
                                    filed_at=_d(src.get("file_date")), period_of_report=_d(src.get("period_ending")),
                                    accession=acc, document=document, document_type=src.get("file_type"),
                                    description=src.get("file_description"), url=url))
        truncated = isinstance(total, int) and total > len(rows)
        cov = Coverage(requested={"start": start and start.isoformat(), "end": end and end.isoformat()},
                       rows=len(rows), data_as_of=prov.retrieved_at.date(), truncated=truncated,
                       notes=["EDGAR full-text search is an undocumented-but-public endpoint "
                              "(https://efts.sec.gov/LATEST/search-index); coverage and shape may change.",
                              f"Provider reported {total} total hits."])
        if not rows:
            return ToolResult(status="empty", reason="no_hits", coverage=cov, permissions=perms, provenance=prov, cost=cost)
        return ToolResult(status="partial" if truncated else "ok", reason="truncated" if truncated else "ok",
                          coverage=cov, permissions=perms, provenance=prov, cost=cost, data=rows)
