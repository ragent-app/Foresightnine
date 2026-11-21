"""BaoStock adapter (A-shares, keyless): stock list, daily k-lines, quarterly ratio data.

BaoStock speaks its own socket protocol through the ``baostock`` library (which imports pandas) and keeps a
global, non-thread-safe login. Every call therefore runs in a short-lived subprocess (``_baostock_worker.py``) so
pandas memory is released and the login never lives in the API process. The runner is a seam: tests inject a fake.

Coverage limits: BaoStock offers quarterly *ratios* (ROE, margins, liability/asset, cash-flow ratios) plus net profit
and main-business revenue; it does not publish absolute total assets, liabilities, equity or cash. Those lines are
reported as unavailable instead of being invented. No terms of use were found; see policy.py (needs review).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from .. import cn
from ..models import Bar, Coverage, Cost, EntityRef, Provenance, SuggestedAction, ToolResult
from ..policy import Denied, FinDataPolicy, load_policy
from ..ratelimit import default_limiter
from .base import StopWith, rate_limited_result, utcnow
from .cn_base import LineValue, bar_window, build_statement_lines, dec, parse_day
from .tushare import cn_aggregate

PROVIDER = "baostock"
TAXONOMY = "baostock"
WORKER = Path(__file__).with_name("_baostock_worker.py")
MARKER = "@@BAOSTOCK_RESULT@@"
MAX_PERIODS = 16
# BaoStock field -> (standard or extra line, statement, unit, flow). ``revenue`` is main-business revenue.
_PROFIT = {"MBRevenue": ("revenue", "income", "CNY", True, None), "netProfit": ("net_income", "income", "CNY", True, None),
           "roeAvg": ("roe_avg", "income", "ratio", False, "period_ratio"),
           "npMargin": ("net_margin", "income", "ratio", False, "period_ratio"),
           "gpMargin": ("gross_margin", "income", "ratio", False, "period_ratio"),
           "epsTTM": ("eps_ttm", "income", "CNY/share", False, "ttm")}
_BALANCE = {"liabilityToAsset": ("liability_to_asset", "balance", "ratio", False, "period_ratio"),
            "currentRatio": ("current_ratio", "balance", "ratio", False, "period_ratio"),
            "quickRatio": ("quick_ratio", "balance", "ratio", False, "period_ratio"),
            "assetToEquity": ("asset_to_equity", "balance", "ratio", False, "period_ratio")}
_CASH = {"CFOToNP": ("cfo_to_net_profit", "cashflow", "ratio", False, "period_ratio"),
         "CFOToOR": ("cfo_to_revenue", "cashflow", "ratio", False, "period_ratio")}
KIND_FIELDS = {"profit": _PROFIT, "balance": _BALANCE, "cash_flow": _CASH}
STANDARD_SUPPORTED = ("revenue", "net_income")
EXTRA_LINES = tuple(v[0] for m in KIND_FIELDS.values() for v in m.values() if v[0] not in STANDARD_SUPPORTED)


class BaoStockRunner(Protocol):
    def __call__(self, request: dict[str, Any], timeout: float) -> dict[str, Any]: ...


class BaoStockFailure(Exception):
    def __init__(self, reason: str, message: str, *, timed_out: bool = False) -> None:
        super().__init__(reason)
        self.reason, self.message, self.timed_out = reason, message, timed_out


def baostock_installed() -> bool:
    return importlib.util.find_spec("baostock") is not None


def subprocess_runner(request: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Run one request in a fresh interpreter (``-I``: no user site, no cwd imports); kill on timeout."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "VIRTUAL_ENV")}
    env.update({"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        done = subprocess.run([sys.executable, "-I", str(WORKER)], input=json.dumps(request).encode(), capture_output=True,
                              timeout=timeout, env=env, check=False)
    except subprocess.TimeoutExpired:
        raise BaoStockFailure("source_timeout", "BaoStock request timed out.", timed_out=True) from None
    except OSError:
        raise BaoStockFailure("source_unavailable", "BaoStock worker could not start.") from None
    out = done.stdout.decode("utf-8", "replace")
    line = next((l for l in reversed(out.splitlines()) if l.startswith(MARKER)), None)
    if line is None:
        raise BaoStockFailure("source_unavailable", f"BaoStock worker exited with code {done.returncode} and no result.")
    try:
        payload = json.loads(line[len(MARKER):])
    except ValueError:
        raise BaoStockFailure("invalid_response", "BaoStock worker returned invalid JSON.") from None
    if not payload.get("ok"):
        raise BaoStockFailure("provider_error", f"BaoStock error: {str(payload.get('error'))[:200]}")
    return payload["result"]


def default_runner() -> BaoStockRunner | None:
    return subprocess_runner if baostock_installed() else None


def report_periods(now: date, period: str, n: int, extra_back: int) -> list[tuple[int, int]]:
    """Newest-first (year, quarter) pairs to request; ``extra_back`` adds earlier ones for derivation."""
    q = (now.month - 1) // 3  # last fully elapsed quarter of this year (0 means last year's Q4)
    year, quarter = (now.year, q) if q else (now.year - 1, 4)
    out: list[tuple[int, int]] = []
    if period == "year":
        y = now.year - 1
        return [(y - i, 4) for i in range(min(n, MAX_PERIODS))]
    while len(out) < min(n + extra_back, MAX_PERIODS):
        out.append((year, quarter))
        year, quarter = (year, quarter - 1) if quarter > 1 else (year - 1, 4)
    return out


class BaoStockAdapter:
    def __init__(self, *, runner: BaoStockRunner | None = None, limiter=None, policy: FinDataPolicy | None = None,
                 now_fn: Callable[[], datetime] = utcnow, timeout: float = 15.0) -> None:
        self._runner = runner if runner is not None else default_runner()
        self._limiter = limiter or default_limiter()
        self._policy = policy or load_policy()
        self._now, self._timeout = now_fn, timeout

    def available(self, category: str = "bars_daily") -> bool:
        return self._runner is not None and not isinstance(self._gate(category, None), ToolResult)

    def _gate(self, category: str, exchange: str | None):
        if self._runner is None:
            return ToolResult.failure("failed", "baostock_not_installed", "The baostock library is not installed.")
        decision = self._policy.decide(PROVIDER, category, exchange, None, None, credential_owner="platform")
        if isinstance(decision, Denied):
            return ToolResult.failure("denied", decision.reason, decision.message)
        return decision

    def _call(self, request: dict[str, Any]) -> tuple[Any, Provenance, Cost]:
        decision = self._limiter.acquire(PROVIDER, 1)
        if not decision.allowed:
            raise StopWith(rate_limited_result(decision, "baostock"))
        cost = Cost(provider_calls=1)
        try:
            result = self._runner(request, self._timeout)  # type: ignore[misc]
        except BaoStockFailure as failure:
            status = "unknown" if failure.timed_out else "failed"
            raise StopWith(ToolResult.failure(status, failure.reason, failure.message, cost=cost,
                                              retryable=failure.reason == "source_unavailable")) from None
        digest = sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()
        return result, Provenance(provider=PROVIDER, endpoint=f"baostock:{request.get('op')}", retrieved_at=self._now(),
                                  response_sha256=digest), cost

    def _run(self, request: dict[str, Any]):
        try:
            return self._call(request), None
        except StopWith as stop:
            return None, stop.result

    # -- identity
    def stock_list(self) -> ToolResult:
        gate = self._gate("identity", None)
        if isinstance(gate, ToolResult):
            return gate
        captured: dict[str, Any] = {}

        def load():
            got, failure = self._run({"op": "stock_basic"})
            if failure is not None:
                captured["stop"] = failure
                return None
            result, prov, cost = got
            captured["prov"], captured["cost"] = prov, cost
            fields = result.get("fields") or []
            refs = []
            for row in result.get("rows") or []:
                rec = dict(zip(fields, row))
                if str(rec.get("type")) != "1" or str(rec.get("status")) != "1":  # stocks, currently listed
                    continue
                code = cn.parse_cn_code(str(rec.get("code") or ""))
                if code is None:
                    continue
                refs.append(EntityRef(entity_id=code.entity_id, company_name=rec.get("code_name") or None, symbol=code.code,
                                      exchange=code.exchange, currency=cn.CN_CURRENCY, asset_type="equity",
                                      identifiers={"baostock_code": code.baostock_code},
                                      listing_date=parse_day(rec.get("ipoDate"))))
            return refs
        refs, cached = cn.cached_list(f"{PROVIDER}:list", self._now(), load)
        if refs is None:
            return captured["stop"]
        return ToolResult(status="ok" if refs else "empty", reason="ok" if refs else "no_match", permissions=gate,
                          coverage=Coverage(rows=len(refs), data_as_of=self._now().date(),
                                            notes=["Served from the in-process listing cache."] if cached else []),
                          provenance=captured.get("prov"), cost=captured.get("cost") or Cost(), data=refs)

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
        got, failure = self._run({"op": "k_data", "code": code.baostock_code, "adjustflags": ["3", "1"],
                                  "start": start.isoformat() if start else "1990-01-01", "end": win.effective_end.isoformat()})
        if failure is not None:
            return failure
        result, prov, cost = got
        raw, back = self._by_date(result.get("3")), self._by_date(result.get("1"))
        notes = list(win.notes)
        dropped = {"rows with invalid or non-positive prices": 0, "rows outside the requested range": 0,
                   "suspended days (tradestatus 0 or zero volume)": 0}
        daily: list[Bar] = []
        for day in sorted(raw):
            row = raw[day]
            px = [dec(row.get(k)) for k in ("open", "high", "low", "close")]
            if any(p is None or p <= 0 for p in px) or px[1] < px[2]:
                dropped["rows with invalid or non-positive prices"] += 1
                continue
            if (start and day < start) or day > win.effective_end:
                dropped["rows outside the requested range"] += 1
                continue
            vol = dec(row.get("volume"))
            if str(row.get("tradestatus")) == "0" or (vol is not None and vol == 0):
                dropped["suspended days (tradestatus 0 or zero volume)"] += 1
                continue
            adj = None
            hfq = dec((back.get(day) or {}).get("close"))
            if hfq is not None and hfq > 0:
                adj = (hfq / px[3]).quantize(Decimal("0.000001"))
            daily.append(Bar(entity_id=eid, interval="1day", ts_start=datetime.combine(day, win.open_time, tzinfo=win.tz),
                             open=px[0], high=px[1], low=px[2], close=px[3], volume=vol, adj_factor=adj, adjustment="none",
                             session="regular", is_final=day <= win.latest))
        for label, count in dropped.items():
            if count:
                notes.append(f"Dropped {count} {label}.")
        notes.append("Prices are unadjusted (as traded, adjustflag=3). adj_factor is derived as back-adjusted close "
                     "(adjustflag=1) divided by close: forward-adjusted price = price * adj_factor / latest adj_factor.")
        bars = cn_aggregate(daily, interval, win.latest)
        if interval != "1day":
            notes.append(f"{interval} bars are aggregated locally from daily bars; ts_start is the first trading day.")
        if not bars:
            return ToolResult(status="empty", reason="no_bars", permissions=gate, provenance=prov, cost=cost,
                              coverage=Coverage(entity_id=eid, session_status=win.status, notes=notes))
        cov = Coverage(entity_id=eid, requested={"start": start.isoformat() if start else None, "end": end.isoformat() if end else None},
                       actual={"first": bars[0].ts_start.isoformat(), "last": bars[-1].ts_start.isoformat()}, rows=len(bars),
                       data_as_of=bars[-1].ts_start, currency=cn.CN_CURRENCY, session_status=win.status,
                       is_final=all(b.is_final for b in bars), notes=notes)
        reason = "range_clamped" if win.clamped else "ok"
        return ToolResult(status="ok" if reason == "ok" else "partial", reason=reason, coverage=cov, permissions=gate,
                          provenance=prov, cost=cost, data=bars,
                          message=f"Requested end was after the latest completed session ({win.latest.isoformat()})." if win.clamped else "",
                          suggested_actions=[SuggestedAction(action="market_data", hint=f"end_date<={win.latest.isoformat()}")] if win.clamped else [])

    @staticmethod
    def _by_date(block: Any) -> dict[date, dict[str, Any]]:
        out: dict[date, dict[str, Any]] = {}
        if not isinstance(block, dict):
            return out
        fields = block.get("fields") or []
        for row in block.get("rows") or []:
            rec = dict(zip(fields, row))
            day = parse_day(rec.get("date"))
            if day is not None:
                out[day] = rec
        return out

    # -- statements (ratios only; see module docstring)
    def statement_lines(self, code: cn.CnCode, *, lines: Iterable[str] | None = None, period: str = "quarter", n: int = 8,
                        derive_quarters: bool = False, entity_id: str | None = None) -> ToolResult:
        wanted = list(lines) if lines else list(STANDARD_SUPPORTED) + list(EXTRA_LINES)
        supported = [l for l in wanted if l in STANDARD_SUPPORTED or l in EXTRA_LINES]
        missing = [l for l in wanted if l not in supported]
        if period not in ("quarter", "year") or n < 1:
            return ToolResult.failure("failed", "invalid_request", "period must be quarter|year.")
        gate = self._gate("financials", code.exchange)
        if isinstance(gate, ToolResult):
            return gate
        if not supported:
            return ToolResult(status="empty", reason="no_matching_lines", permissions=gate,
                              message=f"BaoStock has no {', '.join(missing)}; it offers net_income, revenue (main business) and ratios only.")
        kinds = [k for k, fmap in KIND_FIELDS.items() if any(v[0] in supported for v in fmap.values())]
        periods = report_periods(self._now().date(), period, n, 3 if derive_quarters else 0)
        got, failure = self._run({"op": "financials", "code": code.baostock_code, "kinds": kinds,
                                  "periods": [list(p) for p in periods]})
        if failure is not None:
            return failure
        result, prov, cost = got
        eid = entity_id or code.entity_id
        values: dict[str, dict[date, LineValue]] = {}
        for kind in kinds:
            for block in result.get(kind) or []:
                fields = block.get("fields") or []
                for row in block.get("rows") or []:
                    rec = dict(zip(fields, row))
                    end = parse_day(rec.get("statDate"))
                    if end is None:
                        continue
                    filed = parse_day(rec.get("pubDate"))
                    for source_field, (line, statement, unit, flow, basis) in KIND_FIELDS[kind].items():
                        value = dec(rec.get(source_field))
                        if line in supported and value is not None:
                            values.setdefault(line, {})[end] = LineValue(value, source_field, statement, unit, flow, filed,
                                                                         "BaoStock quarterly data", (), basis)
        if not values:
            return ToolResult(status="empty", reason="no_matching_lines", permissions=gate, provenance=prov, cost=cost)
        out, notes = build_statement_lines(eid, values, taxonomy=TAXONOMY, period=period, n=n, derive_quarters=derive_quarters)
        notes.append("BaoStock provides net profit, main-business revenue (not total operating revenue) and quarterly "
                     "ratios only; whether its flow values are cumulative is not documented, so they are treated as "
                     "year-to-date like the primary reports. Check against filings before quoting.")
        if missing:
            notes.append(f"Not available from BaoStock: {', '.join(missing)}.")
        cov = Coverage(entity_id=eid, requested={"period": period, "n": str(n)}, rows=len(out), currency=cn.CN_CURRENCY,
                       data_as_of=max((l.filed_at for l in out if l.filed_at), default=None), notes=notes)
        partial = bool(missing)
        return ToolResult(status="partial" if partial else "ok", reason="source_gap" if partial else "ok", coverage=cov,
                          permissions=gate, provenance=prov, cost=cost, data=out,
                          message=f"Not available from BaoStock: {', '.join(missing)}." if partial else "")
