"""Tushare Pro adapter (A-shares): stock list, daily bars with adjustment factors, statements.

POST https://api.tushare.pro  body {api_name, token, params, fields}. The token travels only in the request body
and is never logged, echoed in messages or stored. Licence: a user's own token (``user_byok``) is allowed for that
user; the platform token is denied by the default policy (see policy.py). Provider error codes are mapped from
the message text because the documented code list is incomplete (see ``_map_error``).
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any, Callable, Iterable

from ...openai_compatible import _TransportFailure, _TransportTimeout
from .. import cn
from ..models import Bar, Coverage, Cost, EntityRef, Provenance, StatementLine, SuggestedAction, ToolResult
from ..policy import Denied, FinDataPolicy, load_policy
from ..ratelimit import default_limiter
from .base import StopWith, Transport, default_transport, rate_limited_result, scrub, utcnow
from .cn_base import LineValue, bar_window, build_statement_lines, dec, parse_day
from .sec_edgar import STANDARD_LINES, derive_period_metrics

PROVIDER = "tushare"
BASE = "https://api.tushare.pro"
TOKEN_ENV = "FORESIGHTNINE_TUSHARE_TOKEN"
TAXONOMY = "tushare-cas"
LIST_FIELDS = "ts_code,symbol,name,area,industry,fullname,enname,cnspell,market,exchange,list_date"
BAR_FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount"
SHARES_PER_LOT = Decimal(100)
REPORT_TYPE_LABELS = {"1": "合并报表 (consolidated, as originally reported)", "4": "合并报表 (adjusted)"}
_STATEMENT_COMMON = "ts_code,ann_date,f_ann_date,end_date,report_type,update_flag"
# standard line -> (api_name, extra fields in priority order). The first non-null field wins; the field name
# used is recorded as the reported tag.
LINE_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    # ---- income statement (preserve original keys and tag priorities)
    "revenue": ("income", ("total_revenue", "revenue")),               # 营业总收入, else 营业收入
    "cost_of_revenue": ("income", ("oper_cost",)),                     # 营业成本
    "rd_expense": ("income", ("rd_exp",)),                             # 研发费用
    "selling_expense": ("income", ("sell_exp",)),                      # 销售费用
    "admin_expense": ("income", ("admin_exp",)),                       # 管理费用
    "total_operating_costs": ("income", ("total_cogs",)),              # 营业总成本
    "operating_income": ("income", ("operate_profit",)),               # 营业利润
    "interest_expense": ("income", ("fin_exp_int_exp", "int_exp")),    # 财务费用:利息费用 / 利息支出
    "finance_expense": ("income", ("fin_exp",)),                       # 财务费用
    "profit_before_tax": ("income", ("total_profit",)),                # 利润总额
    "income_tax": ("income", ("income_tax",)),                         # 所得税费用
    "net_income": ("income", ("n_income_attr_p",)),                    # 归属于母公司所有者的净利润
    "ebitda": ("income", ("ebitda",)),                                 # 息税折旧摊销前利润
    "eps_basic": ("income", ("basic_eps",)),                           # 基本每股收益
    "eps_diluted": ("income", ("diluted_eps",)),                       # 稀释每股收益
    # ---- balance sheet
    "cash": ("balancesheet", ("money_cap",)),                          # 货币资金
    "short_term_investments": ("balancesheet", ("trad_asset",)),       # 交易性金融资产
    "accounts_receivable": ("balancesheet", ("accounts_receiv", "accounts_receiv_bill")),  # 应收账款
    "inventory": ("balancesheet", ("inventories",)),                   # 存货
    "current_assets": ("balancesheet", ("total_cur_assets",)),         # 流动资产合计
    "ppe_net": ("balancesheet", ("fix_assets",)),                      # 固定资产
    "goodwill": ("balancesheet", ("goodwill",)),                       # 商誉
    "intangible_assets": ("balancesheet", ("intang_assets",)),         # 无形资产
    "total_assets": ("balancesheet", ("total_assets",)),               # 资产总计
    "accounts_payable": ("balancesheet", ("accounts_pay",)),           # 应付账款
    "short_term_debt": ("balancesheet", ("st_borr",)),                 # 短期借款
    "current_liabilities": ("balancesheet", ("total_cur_liab",)),      # 流动负债合计
    "long_term_debt": ("balancesheet", ("lt_borr", "bond_payable")),   # 长期借款, else 应付债券
    "total_liabilities": ("balancesheet", ("total_liab",)),            # 负债合计
    "retained_earnings": ("balancesheet", ("undistr_porfit",)),        # 未分配利润
    "equity": ("balancesheet", ("total_hldr_eqy_exc_min_int",)),       # 归属于母公司股东权益合计
    "non_controlling_interests": ("balancesheet", ("minority_int",)),  # 少数股东权益
    # ---- cash-flow statement
    "depreciation_amortization": ("cashflow", ("depr_fa_coga_dpba",)), # 固定资产折旧等
    "operating_cash_flow": ("cashflow", ("n_cashflow_act",)),          # 经营活动现金流量净额
    "capex": ("cashflow", ("c_pay_acq_const_fiolta",)),                # 购建固定资产、无形资产和其他长期资产支付的现金
    "investing_cash_flow": ("cashflow", ("n_cashflow_inv_act",)),      # 投资活动现金流量净额
    "financing_cash_flow": ("cashflow", ("n_cash_flows_fnc_act",)),    # 筹资活动现金流量净额
    "net_increase_in_cash": ("cashflow", ("n_incr_cash_cash_equ",)),   # 现金及现金等价物净增加额
}
_unknown = [l for l in LINE_FIELDS if l not in STANDARD_LINES]
assert not _unknown, _unknown


def _mapped_error(code: Any, msg: str, api: str, prov, cost, secret: str) -> ToolResult:
    text = scrub(msg, (secret,))
    lowered = msg.lower()
    if "每分钟" in msg or "每小时" in msg or "每天" in msg or "每日" in msg or "per minute" in lowered or str(code) == "40203":
        wait = 3600.0 if ("每小时" in msg or "每天" in msg or "每日" in msg) else 60.0
        reason = "rate_limited"
        return ToolResult.failure("rate_limited", reason, f"Tushare rate limit on API {api}: {text}", retryable=True,
                                  provenance=prov, cost=cost,
                                  suggested_actions=[SuggestedAction(action="wait", retry_after_seconds=wait)])
    if "token" in lowered and ("不对" in msg or "无效" in msg or "invalid" in lowered or "错误" in msg):
        return ToolResult.failure("failed", "invalid_api_key", "The Tushare token was rejected.", provenance=prov, cost=cost)
    if "权限" in msg or "积分" in msg or "permission" in lowered or "point" in lowered or str(code) in ("40001", "2002"):
        return ToolResult.failure(
            "failed", "tushare_permission_insufficient",
            f"The Tushare account lacks permission or points for API '{api}' ({text}). It needs a higher points tier; "
            "retrying will not help.", provenance=prov, cost=cost)
    return ToolResult.failure("failed", "provider_error", f"Tushare error on API {api}: {text}", provenance=prov, cost=cost)


class TushareAdapter:
    STANDARD_LINES = STANDARD_LINES
    LINE_FIELDS = LINE_FIELDS

    def __init__(self, *, token: str | None = None, credential_owner: str = "platform", account_id: str | None = None,
                 transport: Transport | None = None, limiter=None, policy: FinDataPolicy | None = None,
                 now_fn: Callable[[], datetime] = utcnow, timeout: float = 15.0) -> None:
        self._token, self._owner, self._account = token, credential_owner, account_id
        self._transport = transport or default_transport(8_000_000)
        self._limiter = limiter or default_limiter()
        self._policy = policy or load_policy()
        self._now, self._timeout = now_fn, timeout

    # -- gates and transport
    def _credentials(self) -> tuple[str | None, str]:
        token = self._token
        if token is None and self._owner == "platform":
            token = os.environ.get(TOKEN_ENV, "").strip() or None
        return token, self._owner

    def _gate(self, category: str, exchange: str | None):
        token, owner = self._credentials()
        decision = self._policy.decide(PROVIDER, category, exchange, None, None, credential_owner=owner)
        if isinstance(decision, Denied):
            return ToolResult.failure("denied", decision.reason, decision.message)
        if not token:
            return ToolResult.failure("failed", "api_key_not_configured", "No Tushare token is configured.")
        if decision.owner_scoped and not self._account:
            return ToolResult.failure("failed", "account_required", "Owner-scoped data requires the owner account id.")
        return decision

    def available(self, category: str = "bars_daily") -> bool:
        return not isinstance(self._gate(category, None), ToolResult)

    def _call(self, api: str, params: dict[str, str], fields: str) -> tuple[list[dict[str, Any]], Provenance, Cost]:
        token, owner = self._credentials()
        limiter_key = PROVIDER if owner == "platform" else f"{PROVIDER}:byok:{self._account}"
        decision = self._limiter.acquire(limiter_key, 1)
        if not decision.allowed:
            raise StopWith(rate_limited_result(decision, "tushare"))
        cost = Cost(provider_calls=1)
        endpoint = f"{BASE}/{api}"
        body = json.dumps({"api_name": api, "token": token, "params": params, "fields": fields}).encode()
        try:
            reply = self._transport("POST", BASE, {"Content-Type": "application/json", "Accept": "application/json"},
                                    body, self._timeout)
        except _TransportTimeout:
            raise StopWith(ToolResult.failure("unknown", "source_timeout", "Tushare request timed out.", cost=cost)) from None
        except (_TransportFailure, OSError) as error:
            if str(error) == "response too large":
                raise StopWith(ToolResult.failure("failed", "source_too_large", "Tushare response exceeded the size limit.",
                                                  cost=cost)) from None
            raise StopWith(ToolResult.failure("unknown", "source_unavailable", "Tushare request failed.", cost=cost)) from None
        prov = Provenance(provider=PROVIDER, endpoint=endpoint, retrieved_at=self._now(),
                          response_sha256=sha256(reply.body).hexdigest())
        if reply.status_code == 429:
            raise StopWith(ToolResult.failure("rate_limited", "provider_rate_limited", "Tushare returned HTTP 429.",
                                              retryable=True, provenance=prov, cost=cost,
                                              suggested_actions=[SuggestedAction(action="wait", retry_after_seconds=60.0)]))
        if not 200 <= reply.status_code < 300:
            raise StopWith(ToolResult.failure("failed", "source_unavailable", f"Tushare returned HTTP {reply.status_code}.",
                                              retryable=reply.status_code >= 500, provenance=prov, cost=cost))
        try:
            doc = json.loads(reply.body, parse_float=Decimal)
        except (ValueError, UnicodeDecodeError):
            raise StopWith(ToolResult.failure("failed", "invalid_response", "Tushare returned invalid JSON.",
                                              provenance=prov, cost=cost)) from None
        if not isinstance(doc, dict):
            raise StopWith(ToolResult.failure("failed", "invalid_response", "Unexpected Tushare response.", provenance=prov, cost=cost))
        code = doc.get("code")
        if code not in (0, "0"):
            raise StopWith(_mapped_error(code, str(doc.get("msg") or ""), api, prov, cost, token or ""))
        data = doc.get("data") or {}
        names, items = data.get("fields"), data.get("items")
        if not isinstance(names, list) or not isinstance(items, list):
            raise StopWith(ToolResult.failure("failed", "invalid_response", "Tushare response has no fields/items.",
                                              provenance=prov, cost=cost))
        rows = [dict(zip(names, item)) for item in items if isinstance(item, list)]
        return rows, prov, cost

    # -- identity
    def stock_list(self) -> ToolResult:
        """Listed A-shares (status L) as ``EntityRef`` list; cached per process (per owner account for BYOK)."""
        gate = self._gate("identity", None)
        if isinstance(gate, ToolResult):
            return gate
        scope = f"tushare:{self._owner}:{self._account if gate.owner_scoped else '-'}"
        captured: dict[str, Any] = {}

        def load():
            try:
                rows, prov, cost = self._call("stock_basic", {"list_status": "L"}, LIST_FIELDS)
            except StopWith as stop:
                captured["stop"] = stop.result
                return None
            captured["prov"], captured["cost"] = prov, cost
            return [r for r in (self._entity(row) for row in rows) if r is not None]
        refs, cached = cn.cached_list(scope, self._now(), load)
        if refs is None:
            return captured["stop"]
        cov = Coverage(rows=len(refs), data_as_of=self._now().date(),
                       notes=["Served from the in-process listing cache."] if cached else [])
        return ToolResult(status="ok" if refs else "empty", reason="ok" if refs else "no_match", coverage=cov,
                          permissions=gate, provenance=captured.get("prov"), cost=captured.get("cost") or Cost(), data=refs)

    @staticmethod
    def _entity(row: dict[str, Any]) -> EntityRef | None:
        ts_code = str(row.get("ts_code") or "")
        parsed = cn.parse_cn_code(ts_code)
        if parsed is None:
            return None
        ids = {k: str(row[k]) for k in ("area", "industry", "fullname", "enname", "cnspell") if row.get(k)}
        ids["ts_code"] = ts_code
        if row.get("market"):
            ids["board"] = str(row["market"])
        listed = parse_day(row.get("list_date"))
        return EntityRef(entity_id=parsed.entity_id, company_name=row.get("name"), symbol=parsed.code,
                         exchange=parsed.exchange, currency=cn.CN_CURRENCY, asset_type="equity", identifiers=ids,
                         listing_date=listed)

    # -- bars
    def bars(self, code: cn.CnCode, *, interval: str = "1day", start: date | None = None, end: date | None = None,
             entity_id: str | None = None, include_partial: bool = False) -> ToolResult:
        if interval not in ("1day", "1week", "1month"):
            return ToolResult.failure("failed", "intraday_not_supported",
                                      "Free A-share sources provide daily, weekly and monthly bars only.")
        if start and end and start > end:
            return ToolResult.failure("failed", "invalid_request", "start must not be after end.")
        gate = self._gate("bars_daily", code.exchange)
        if isinstance(gate, ToolResult):
            return gate
        win = bar_window(self._now(), code.exchange, start, end, include_partial)
        eid = entity_id or code.entity_id
        if start and start > win.effective_end:
            return ToolResult(status="empty", reason="range_after_latest_session", permissions=gate,
                              message=f"Latest allowed end date is {win.latest.isoformat()}.",
                              suggested_actions=[SuggestedAction(action="market_data", hint=f"end_date<={win.latest.isoformat()}")],
                              coverage=Coverage(entity_id=eid, session_status=win.status, notes=win.notes))
        params = {"ts_code": code.ts_code, "end_date": win.effective_end.strftime("%Y%m%d")}
        if start:
            params["start_date"] = start.strftime("%Y%m%d")
        notes = list(win.notes)
        try:
            rows, prov, cost = self._call("daily", params, BAR_FIELDS)
        except StopWith as stop:
            return stop.result
        factors: dict[date, Decimal] = {}
        total_cost = cost
        try:
            frows, _fprov, fcost = self._call("adj_factor", params, "ts_code,trade_date,adj_factor")
            total_cost = Cost(provider_calls=cost.provider_calls + fcost.provider_calls)
            factors = {d: f for r in frows if (d := parse_day(r.get("trade_date"))) and (f := dec(r.get("adj_factor")))}
        except StopWith as stop:
            reason = stop.result.reason
            notes.append(f"adj_factor unavailable ({reason}): bars carry no adjustment factor; prices are unadjusted.")
        dropped = {"rows with invalid or non-positive prices": 0, "duplicate rows": 0, "rows outside the requested range": 0,
                   "zero-volume rows (suspended)": 0}
        seen: set[date] = set()
        daily: list[Bar] = []
        for row in rows:
            day = parse_day(row.get("trade_date"))
            px = [dec(row.get(k)) for k in ("open", "high", "low", "close")]
            if day is None or any(p is None or p <= 0 for p in px) or px[1] < px[2]:
                dropped["rows with invalid or non-positive prices"] += 1
                continue
            if (start and day < start) or day > win.effective_end:
                dropped["rows outside the requested range"] += 1
                continue
            if day in seen:
                dropped["duplicate rows"] += 1
                continue
            vol = dec(row.get("vol"))
            if vol is not None and vol == 0:
                dropped["zero-volume rows (suspended)"] += 1
                continue
            seen.add(day)
            daily.append(Bar(entity_id=eid, interval="1day", ts_start=datetime.combine(day, win.open_time, tzinfo=win.tz),
                             open=px[0], high=px[1], low=px[2], close=px[3],
                             volume=None if vol is None else vol * SHARES_PER_LOT, adj_factor=factors.get(day),
                             adjustment="none", session="regular", is_final=day <= win.latest))
        daily.sort(key=lambda b: b.ts_start)
        for label, count in dropped.items():
            if count:
                notes.append(f"Dropped {count} {label}.")
        notes.append("Prices are unadjusted (as traded). adj_factor is Tushare's cumulative adjustment factor: "
                     "forward-adjusted price = price * adj_factor / latest adj_factor. Volume is converted from lots "
                     "(100 shares) to shares.")
        bars = cn_aggregate(daily, interval, win.latest)
        if interval != "1day":
            notes.append(f"{interval} bars are aggregated locally from daily bars; ts_start is the first trading day.")
        if not bars:
            return ToolResult(status="empty", reason="no_bars", permissions=gate, provenance=prov, cost=total_cost,
                              coverage=Coverage(entity_id=eid, session_status=win.status, notes=notes))
        cov = Coverage(entity_id=eid, requested={"start": start.isoformat() if start else None, "end": end.isoformat() if end else None},
                       actual={"first": bars[0].ts_start.isoformat(), "last": bars[-1].ts_start.isoformat()}, rows=len(bars),
                       data_as_of=bars[-1].ts_start, currency=cn.CN_CURRENCY, session_status=win.status,
                       is_final=all(b.is_final for b in bars), notes=notes)
        reason = "range_clamped" if win.clamped else "ok"
        return ToolResult(status="ok" if reason == "ok" else "partial", reason=reason, coverage=cov, permissions=gate,
                          provenance=prov, cost=total_cost, data=bars,
                          message=f"Requested end was after the latest completed session ({win.latest.isoformat()})." if win.clamped else "",
                          suggested_actions=[SuggestedAction(action="market_data", hint=f"end_date<={win.latest.isoformat()}")] if win.clamped else [])

    # -- statements
    def statement_lines(self, code: cn.CnCode, *, lines: Iterable[str] | None = None, period: str = "quarter", n: int = 8,
                        derive_quarters: bool = False, entity_id: str | None = None) -> ToolResult:
        wanted = list(lines) if lines else list(LINE_FIELDS)
        unknown = [l for l in wanted if l not in STANDARD_LINES]
        if unknown or period not in ("quarter", "year") or n < 1:
            return ToolResult.failure("failed", "unknown_line" if unknown else "invalid_request",
                                      f"Unsupported standard lines for Tushare: {', '.join(unknown)}." if unknown else "period must be quarter|year.")
        gate = self._gate("financials", code.exchange)
        if isinstance(gate, ToolResult):
            return gate
        eid = entity_id or code.entity_id
        supported = [l for l in wanted if l in LINE_FIELDS]
        unmapped = [l for l in wanted if l not in LINE_FIELDS]
        if not supported:
            return ToolResult(
                status="empty", reason="no_matching_lines", permissions=gate,
                coverage=Coverage(entity_id=eid, rows=0, currency=cn.CN_CURRENCY,
                                  notes=[f"No Tushare field mapping for standard line(s): {', '.join(unmapped)}."]),
                message=f"Tushare has no direct mapping for: {', '.join(unmapped)}.")
        apis: dict[str, list[str]] = {}
        for line in supported:
            api, fields = LINE_FIELDS[line]
            apis.setdefault(api, [])
            apis[api].extend(f for f in fields if f not in apis[api])
        values: dict[str, dict[date, LineValue]] = {line: {} for line in supported}
        notes: list[str] = []
        if unmapped:
            notes.append(f"No Tushare field mapping for standard line(s): {', '.join(unmapped)}.")
        calls, hashes, endpoints, last_prov = 0, [], [], None
        failures: list[ToolResult] = []
        for api, fields in apis.items():
            try:
                rows, prov, cost = self._call(api, {"ts_code": code.ts_code, "report_type": "1"},
                                              f"{_STATEMENT_COMMON},{','.join(fields)}")
            except StopWith as stop:
                calls += stop.result.cost.provider_calls
                failures.append(stop.result)
                notes.append(f"{api} unavailable: {stop.result.reason}.")
                continue
            calls += cost.provider_calls
            hashes.append(prov.response_sha256 or "")
            endpoints.append(prov.endpoint)
            last_prov = prov
            self._collect(rows, api, supported, values)
        if not any(values.values()):
            if failures:
                return failures[0].model_copy(update={"cost": Cost(provider_calls=calls)})
            return ToolResult(status="empty", reason="no_matching_lines", permissions=gate, provenance=last_prov,
                              cost=Cost(provider_calls=calls))
        out, more = build_statement_lines(eid, values, taxonomy=TAXONOMY, period=period, n=n, derive_quarters=derive_quarters)
        notes.extend(more)
        digest = sha256("\n".join(sorted(hashes)).encode()).hexdigest() if hashes else None
        prov = Provenance(provider=PROVIDER, endpoint=endpoints[0] if endpoints else BASE, retrieved_at=self._now(),
                          response_sha256=digest, endpoints=endpoints)
        cov = Coverage(entity_id=eid, requested={"period": period, "n": str(n)}, rows=len(out), currency=cn.CN_CURRENCY,
                       data_as_of=max((l.filed_at for l in out if l.filed_at), default=None), notes=notes)
        partial = bool(failures) or bool(unmapped)
        reason = "source_gap" if failures else ("missing_lines" if unmapped else "ok")
        msg = ("Some statements could not be fetched; see notes." if failures else
               (f"No Tushare mapping for: {', '.join(unmapped)}." if unmapped else ""))
        return ToolResult(status="partial" if partial else "ok", reason=reason, coverage=cov,
                          permissions=gate, provenance=prov, cost=Cost(provider_calls=calls), data=out,
                          message=msg)

    @staticmethod
    def _collect(rows: list[dict[str, Any]], api: str, wanted: list[str], values: dict[str, dict[date, LineValue]]) -> None:
        by_end: dict[date, list[dict[str, Any]]] = {}
        for row in rows:
            end = parse_day(row.get("end_date"))
            if end is None or str(row.get("report_type") or "1") != "1":
                continue
            parsed = dict(row)
            parsed["filed"] = parse_day(row.get("f_ann_date")) or parse_day(row.get("ann_date"))
            parsed["ann"] = parse_day(row.get("ann_date"))
            for key, value in list(parsed.items()):
                if key not in ("filed", "ann", "update_flag", "report_type", "end_date", "ann_date", "f_ann_date", "ts_code"):
                    parsed[key] = dec(value)
            by_end.setdefault(end, []).append(parsed)
        for line in wanted:
            line_api, fields = LINE_FIELDS[line]
            if line_api != api:
                continue
            spec = STANDARD_LINES[line]
            unit = "CNY/share" if spec.unit_kind == "per_share" else ("shares" if spec.unit_kind == "shares" else "CNY")
            for end, group in by_end.items():
                for field_name in fields:
                    winner, value, superseded = cn.pick_latest(group, field_name)
                    if value is None:
                        continue
                    values[line][end] = LineValue(value, field_name, spec.statement, unit, spec.flow, winner.get("filed"),
                                                  REPORT_TYPE_LABELS.get(str(winner.get("report_type") or "1"), "合并报表"),
                                                  tuple(superseded))
                    break


def cn_aggregate(daily: list[Bar], interval: str, latest: date) -> list[Bar]:
    return cn.aggregate_bars(daily, interval, latest)
