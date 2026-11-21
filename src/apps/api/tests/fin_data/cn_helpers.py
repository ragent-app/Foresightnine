"""Hand-written A-share fixtures and fakes (no network): Tushare transport, BaoStock runner."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.fin_data.adapters.baostock import BaoStockFailure
from tests.fin_data.helpers import Reply

TOKEN = "tushare-secret-token-0123456789"

LIST_ROWS = [
    {"ts_code": "600519.SH", "symbol": "600519", "name": "贵州茅台", "area": "贵州", "industry": "白酒", "fullname": "贵州茅台酒股份有限公司",
     "enname": "Kweichow Moutai Co.,Ltd.", "cnspell": "gzmt", "market": "主板", "exchange": "SSE", "list_date": "20010827"},
    {"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行", "area": "深圳", "industry": "银行", "fullname": "平安银行股份有限公司",
     "enname": "Ping An Bank Co., Ltd.", "cnspell": "payh", "market": "主板", "exchange": "SZSE", "list_date": "19910403"},
    {"ts_code": "601318.SH", "symbol": "601318", "name": "中国平安", "area": "深圳", "industry": "保险", "fullname": "中国平安保险(集团)股份有限公司",
     "enname": "Ping An Insurance", "cnspell": "zgpa", "market": "主板", "exchange": "SSE", "list_date": "20070301"},
    {"ts_code": "430047.BJ", "symbol": "430047", "name": "诺思兰德", "area": "北京", "industry": "生物制药", "fullname": "北京诺思兰德生物技术股份有限公司",
     "enname": "", "cnspell": "nsld", "market": "北交所", "exchange": "BJSE", "list_date": "20200727"},
]

DAILY_ROWS = [  # newest first, like Tushare; 11-18 is a suspended day (zero volume)
    {"ts_code": "600519.SH", "trade_date": "20251119", "open": 1500.5, "high": 1520.0, "low": 1495.0, "close": 1510.25, "vol": 25000.0, "amount": 3777000.0},
    {"ts_code": "600519.SH", "trade_date": "20251118", "open": 1490.0, "high": 1505.0, "low": 1488.0, "close": 1500.0, "vol": 0.0, "amount": 0.0},
    {"ts_code": "600519.SH", "trade_date": "20251117", "open": 1480.0, "high": 1495.5, "low": 1475.0, "close": 1490.0, "vol": 31000.5, "amount": 4600000.0},
    {"ts_code": "600519.SH", "trade_date": "20251117", "open": 1480.0, "high": 1495.5, "low": 1475.0, "close": 1490.0, "vol": 31000.5, "amount": 4600000.0},
]
ADJ_ROWS = [{"ts_code": "600519.SH", "trade_date": d, "adj_factor": f} for d, f in
            (("20251119", 8.5), ("20251118", 8.5), ("20251117", 8.25))]


def income_rows() -> list[dict[str, Any]]:
    def row(end, ann, rev, *, flag="0", f_ann=None, op=None, n=None, eps=None, rtype="1"):
        return {"ts_code": "600519.SH", "ann_date": ann, "f_ann_date": f_ann or ann, "end_date": end, "report_type": rtype,
                "update_flag": flag, "total_revenue": rev, "revenue": rev - 1 if rev else None, "operate_profit": op,
                "n_income_attr_p": n, "diluted_eps": eps}
    return [
        row("20250331", "20250426", 100.0, op=60.0, n=45.0, eps=1.1), row("20250630", "20250820", 230.0, op=140.0, n=100.0, eps=2.4),
        row("20250930", "20251025", 390.0, op=250.0, n=170.0, eps=4.0),
        row("20251231", "20260320", 560.0, op=350.0, n=240.0, eps=5.7),
        row("20251231", "20260425", 565.0, flag="1", op=352.0, n=242.0, eps=5.8),               # restated FY2025 (latest revision)
        row("20251231", "20260301", 999.0, rtype="4"),                                           # adjusted report type: ignored
        row("20260331", "20260426", 120.0, op=70.0, n=50.0, eps=1.2), row("20260630", "20260820", 260.0, op=150.0, n=110.0, eps=2.5),
        row("20260930", "20261025", 430.0, op=270.0, n=185.0, eps=4.2),
        {"ts_code": "600519.SH", "ann_date": "20251101", "f_ann_date": "20251101", "end_date": "20240930", "report_type": "1", "update_flag": "0",
         "total_revenue": None, "revenue": 300.0, "operate_profit": None, "n_income_attr_p": None, "diluted_eps": None},
    ]


def balance_rows() -> list[dict[str, Any]]:
    return [{"ts_code": "600519.SH", "ann_date": "20261025", "f_ann_date": "20261025", "end_date": "20260930", "report_type": "1",
             "update_flag": "0", "total_assets": 3000.0, "total_liab": 700.0, "total_hldr_eqy_exc_min_int": 2200.0, "money_cap": 1500.0}]


def cashflow_rows() -> list[dict[str, Any]]:
    return [{"ts_code": "600519.SH", "ann_date": "20261025", "f_ann_date": "20261025", "end_date": "20260930", "report_type": "1",
             "update_flag": "0", "n_cashflow_act": 160.0, "c_pay_acq_const_fiolta": 12.5}]


def ts_reply(rows: list[dict[str, Any]] | None = None, *, code: int = 0, msg: str | None = None) -> Reply:
    if code:
        return Reply(200, json.dumps({"request_id": "r1", "code": code, "msg": msg, "data": None}, ensure_ascii=False).encode())
    names = list(dict.fromkeys(k for r in (rows or []) for k in r))
    # numbers are serialised as JSON numbers, as Tushare does
    body = {"request_id": "r1", "code": 0, "msg": None, "data": {"fields": names, "items": [[r.get(n) for n in names] for r in rows or []], "has_more": False}}
    return Reply(200, json.dumps(body, ensure_ascii=False).encode())


@dataclass
class TushareFake:
    """Serves one reply per api_name; records the decoded request bodies."""
    replies: dict[str, Any]
    calls: list[dict[str, Any]] = field(default_factory=list)
    urls: list[tuple[str, str, dict]] = field(default_factory=list)

    def __call__(self, method, url, headers, body, timeout):
        doc = json.loads(body)
        self.calls.append(doc)
        self.urls.append((method, url, dict(headers)))
        reply = self.replies.get(doc["api_name"])
        if reply is None:
            return ts_reply([])
        if isinstance(reply, Exception):
            raise reply
        return reply

    def apis(self) -> list[str]:
        return [c["api_name"] for c in self.calls]


def standard_tushare(**override) -> TushareFake:
    replies = {"stock_basic": ts_reply(LIST_ROWS), "daily": ts_reply(DAILY_ROWS), "adj_factor": ts_reply(ADJ_ROWS),
               "income": ts_reply(income_rows()), "balancesheet": ts_reply(balance_rows()), "cashflow": ts_reply(cashflow_rows())}
    replies.update(override)
    return TushareFake(replies)


# ---------------------------------------------------------------- BaoStock
BS_LIST = {"fields": ["code", "code_name", "ipoDate", "outDate", "type", "status"],
           "rows": [["sh.600519", "贵州茅台", "2001-08-27", "", "1", "1"], ["sh.000001", "上证指数", "1991-07-15", "", "2", "1"],
                    ["sz.000001", "平安银行", "1991-04-03", "", "1", "1"], ["sz.000999", "退市股", "2000-01-01", "2020-01-01", "1", "0"]]}
BS_KFIELDS = ["date", "code", "open", "high", "low", "close", "volume", "amount", "tradestatus"]


def bs_k(rows):
    return {"fields": BS_KFIELDS, "rows": rows}


BS_K_RAW = bs_k([["2025-11-17", "sh.600519", "1480.0", "1495.5", "1475.0", "1490.0", "3100050", "4600000", "1"],
                 ["2025-11-18", "sh.600519", "1490.0", "1505.0", "1488.0", "1500.0", "0", "0", "0"],
                 ["2025-11-19", "sh.600519", "1500.5", "1520.0", "1495.0", "1510.25", "2500000", "3777000", "1"]])
BS_K_BACK = bs_k([["2025-11-17", "sh.600519", "12000", "12100", "11900", "12292.5", "3100050", "4600000", "1"],
                  ["2025-11-18", "sh.600519", "12000", "12100", "11900", "12750.0", "0", "0", "0"],
                  ["2025-11-19", "sh.600519", "12000", "12100", "11900", "12837.125", "2500000", "3777000", "1"]])


def bs_financials(periods):
    profit, balance, cash = [], [], []
    for year, quarter in periods:
        stat = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}[quarter]
        pub = f"{year}-{('04-26', '08-20', '10-25', '04-28')[quarter - 1]}"
        # statDate ends are real dates; revenue/net profit are cumulative figures in this fixture
        profit.append({"year": year, "quarter": quarter, "fields": ["code", "pubDate", "statDate", "roeAvg", "npMargin", "gpMargin", "netProfit", "epsTTM", "MBRevenue", "totalShare", "liqaShare"],
                       "rows": [["sh.600519", pub, f"{year}-{stat}", "0.08", "0.52", "0.91", str(100 * quarter), "55.1", str(200 * quarter), "1.2e9", "1.2e9"]]})
        balance.append({"year": year, "quarter": quarter, "fields": ["code", "pubDate", "statDate", "currentRatio", "quickRatio", "cashRatio", "YOYLiability", "liabilityToAsset", "assetToEquity"],
                        "rows": [["sh.600519", pub, f"{year}-{stat}", "4.1", "3.2", "3.0", "0.1", "0.21", "1.27"]]})
        cash.append({"year": year, "quarter": quarter, "fields": ["code", "pubDate", "statDate", "CAToAsset", "NCAToAsset", "tangibleAssetToAsset", "ebitToInterest", "CFOToOR", "CFOToNP", "CFOToGr"],
                     "rows": [["sh.600519", pub, f"{year}-{stat}", "0.8", "0.2", "0.9", "99", "0.45", "0.9", "0.5"]]})
    return {"profit": profit, "balance": balance, "cash_flow": cash}


class FakeBaoStock:
    def __init__(self, *, fail: BaoStockFailure | None = None):
        self.requests: list[dict[str, Any]] = []
        self.fail = fail

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail
        op = request["op"]
        if op == "stock_basic":
            return BS_LIST
        if op == "k_data":
            return {"3": BS_K_RAW, "1": BS_K_BACK}
        if op == "financials":
            all_ = bs_financials([tuple(p) for p in request["periods"]])
            return {k: v for k, v in all_.items() if k in request["kinds"]}
        raise AssertionError(op)
