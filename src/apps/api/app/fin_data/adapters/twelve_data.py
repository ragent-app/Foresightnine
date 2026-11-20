"""Twelve Data adapter: symbol search and OHLCV bars.

Licence: free/individual plans are personal non-commercial. The default policy denies the platform
key and allows only a user's own key (``credential_owner="user_byok"``); see policy.py notes.
Endpoints: https://api.twelvedata.com/symbol_search and /time_series (end_date is exclusive).
The API key travels in the Authorization header and is never logged or placed in results.
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...research_clock import exchange_session, last_completed_session, session_status
from ..models import (INTERVALS, INTRADAY_INTERVALS, Bar, Coverage, Cost, EntityRef, Permissions, SuggestedAction,
                      ToolResult, normalize_asset_type, normalize_exchange)
from ..policy import Denied, FinDataPolicy, bar_category, load_policy
from ..ratelimit import default_limiter
from .base import StopWith, Transport, default_transport, request_json, utcnow, scrub

PROVIDER = "twelve_data"
BASE = "https://api.twelvedata.com"
API_KEY_ENV = "FORESIGHTNINE_TWELVEDATA_API_KEY"
MAX_OUTPUT = 5000
DELAY_NOTE = "Twelve Data free-tier data may be delayed and is not real-time; confirm data_as_of."
ADJUSTMENTS = ("none", "splits", "dividends", "all")


def _dec(value: Any) -> Decimal | None:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def _prev_weekday(day: date) -> date:
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


class TwelveDataAdapter:
    def __init__(self, *, api_key: str | None = None, credential_owner: str = "platform",
                 account_id: str | None = None, transport: Transport | None = None, limiter=None,
                 policy: FinDataPolicy | None = None, now_fn: Callable[[], datetime] = utcnow,
                 timeout: float = 15.0) -> None:
        self._default_key, self._default_owner, self._default_account = api_key, credential_owner, account_id
        self._transport = transport or default_transport(2_000_000)
        self._limiter = limiter or default_limiter()
        self._policy = policy or load_policy()
        self._now, self._timeout = now_fn, timeout

    # -- credentials and gates
    def _credentials(self, api_key, owner, account) -> tuple[str | None, str, str | None]:
        owner = owner or self._default_owner
        key = api_key or self._default_key
        account = account or self._default_account
        if key is None and owner == "platform":
            key = os.environ.get(API_KEY_ENV, "").strip() or None  # platform key only; never used for BYOK
        return key, owner, account

    def _gate(self, category, exchange, asset_type, interval, key, owner, account):
        decision = self._policy.decide(PROVIDER, category, exchange, asset_type, interval, credential_owner=owner)
        if isinstance(decision, Denied):
            return ToolResult.failure("denied", decision.reason, decision.message)
        if not key:
            return ToolResult.failure("failed", "api_key_not_configured", "No Twelve Data API key is configured.")
        if decision.owner_scoped and not account:
            return ToolResult.failure("failed", "account_required", "Owner-scoped data requires the owner account id.")
        return decision

    def _call(self, path: str, params: dict, key: str, owner: str, account: str | None, credits: int | None):
        limiter_key = PROVIDER if owner == "platform" else f"{PROVIDER}:byok:{account}"
        doc, prov, cost = request_json(provider=PROVIDER, url=f"{BASE}{path}?{urlencode(params)}",
                                       headers={"Authorization": f"apikey {key}"}, transport=self._transport,
                                       timeout=self._timeout, limiter=self._limiter, now_fn=self._now, credits=credits,
                                       unknown_on_transport_error=True, secrets=(key,), limiter_key=limiter_key)
        if isinstance(doc, dict) and doc.get("status") == "error":
            raise StopWith(self._provider_error(doc, prov, cost, key))
        return doc, prov, cost

    @staticmethod
    def _provider_error(doc: dict, prov, cost, key: str) -> ToolResult:
        code, msg = doc.get("code"), scrub(str(doc.get("message", "")), (key,))
        if code == 429:
            return ToolResult.failure("rate_limited", "provider_rate_limited", msg, retryable=True, provenance=prov, cost=cost,
                                      suggested_actions=[SuggestedAction(action="wait", retry_after_seconds=60.0)])
        if code == 404 or "not found" in msg.lower():
            return ToolResult.failure("empty", "symbol_not_found", msg, provenance=prov, cost=cost)
        if code == 403 or "plan" in msg.lower():
            return ToolResult.failure("denied", "provider_plan_restriction", msg, provenance=prov, cost=cost)
        if code == 401:
            return ToolResult.failure("failed", "invalid_api_key", "The Twelve Data API key was rejected.", provenance=prov, cost=cost)
        if code == 400:
            return ToolResult.failure("failed", "invalid_request", msg, provenance=prov, cost=cost)
        return ToolResult.failure("failed", "provider_error", msg, retryable=isinstance(code, int) and code >= 500,
                                  provenance=prov, cost=cost)

    # -- symbol search
    def symbol_search(self, query: str, *, outputsize: int = 10, api_key: str | None = None,
                      credential_owner: str | None = None, account_id: str | None = None) -> ToolResult:
        key, owner, account = self._credentials(api_key, credential_owner, account_id)
        gate = self._gate("identity", None, None, None, key, owner, account)
        if isinstance(gate, ToolResult):
            return gate
        try:
            doc, prov, cost = self._call("/symbol_search", {"symbol": query, "outputsize": min(max(outputsize, 1), 30)},
                                         key, owner, account, None)
        except StopWith as stop:
            return stop.result
        rows = doc.get("data") if isinstance(doc, dict) else None
        if not isinstance(rows, list):
            return ToolResult.failure("failed", "invalid_response", "Unexpected symbol_search shape.", provenance=prov, cost=cost)
        refs = []
        for r in rows:
            if not isinstance(r, dict) or not r.get("symbol"):
                continue
            raw_ex = r.get("exchange")
            ex = normalize_exchange(raw_ex) or normalize_exchange(r.get("mic_code"))
            ids = {k: str(v) for k, v in (("exchange_raw", raw_ex), ("mic", r.get("mic_code")),
                                          ("country", r.get("country")), ("instrument_type", r.get("instrument_type")),
                                          ("exchange_timezone", r.get("exchange_timezone"))) if v}
            refs.append(EntityRef(entity_id=f"{ex or str(raw_ex or '?').upper()}:{r['symbol']}",
                                  company_name=r.get("instrument_name"), symbol=str(r["symbol"]), exchange=ex,
                                  currency=r.get("currency"), asset_type=normalize_asset_type(r.get("instrument_type")),
                                  identifiers=ids))
        cov = Coverage(rows=len(refs), data_as_of=prov.retrieved_at.date(), notes=[DELAY_NOTE.split(";")[0]])
        if not refs:
            return ToolResult(status="empty", reason="no_match", coverage=cov, permissions=gate, provenance=prov, cost=cost)
        return ToolResult(status="ok", reason="ok", coverage=cov, permissions=gate, provenance=prov, cost=cost, data=refs)

    # -- bars
    def bars(self, symbol: str, *, exchange: str | None = None, interval: str = "1day", start: date | None = None,
             end: date | None = None, adjust: str = "none", entity_id: str | None = None, asset_type: str | None = None,
             include_partial: bool = False, api_key: str | None = None, credential_owner: str | None = None,
             account_id: str | None = None) -> ToolResult:
        """``end`` is inclusive for the caller; the provider's exclusive end_date is handled here."""
        if interval not in INTERVALS or adjust not in ADJUSTMENTS or not symbol.strip():
            return ToolResult.failure("failed", "invalid_request",
                                      f"interval must be one of {', '.join(INTERVALS)}; adjust one of {', '.join(ADJUSTMENTS)}.")
        if start and end and start > end:
            return ToolResult.failure("failed", "invalid_request", "start must not be after end.")
        key, owner, account = self._credentials(api_key, credential_owner, account_id)
        ex_code = normalize_exchange(exchange) if exchange else None
        gate = self._gate(bar_category(interval), ex_code or (exchange or "").upper() or None, asset_type, interval,
                          key, owner, account)
        if isinstance(gate, ToolResult):
            return gate
        now = self._now()
        sess = exchange_session(ex_code)
        notes = [DELAY_NOTE]
        if sess is not None:
            latest, status = last_completed_session(now, sess), session_status(now, sess)
            today_local = now.astimezone(ZoneInfo(sess.timezone)).date()
        else:
            latest, status = _prev_weekday(now.date()), None
            today_local = now.date()
            notes.append("Exchange session unknown; the end clamp is approximated by the previous weekday (UTC).")
        requested_end = end or today_local
        effective_end = requested_end if include_partial and requested_end <= today_local else min(requested_end, latest)
        clamped = effective_end < requested_end
        suggestion = [SuggestedAction(action="market_data", hint=f"end_date<={latest.isoformat()}")]
        if start and start > effective_end:
            return ToolResult(status="empty", reason="range_after_latest_session", suggested_actions=suggestion,
                              message=f"Latest allowed end date is {latest.isoformat()}.", permissions=gate,
                              coverage=Coverage(entity_id=entity_id, requested=self._req(start, end), session_status=status,
                                                notes=notes))
        params = {"symbol": symbol, "interval": interval, "order": "ASC", "outputsize": MAX_OUTPUT, "adjust": adjust,
                  "end_date": (effective_end + timedelta(days=1)).isoformat(), "timezone": "Exchange"}
        if exchange:
            params["exchange"] = exchange
        if start:
            params["start_date"] = start.isoformat()
        try:
            doc, prov, cost = self._call("/time_series", params, key, owner, account, 1)
        except StopWith as stop:
            return stop.result
        meta, values = (doc.get("meta"), doc.get("values")) if isinstance(doc, dict) else (None, None)
        if not isinstance(meta, dict) or not isinstance(values, list):
            return ToolResult.failure("failed", "invalid_response", "Provider returned no usable series.", provenance=prov, cost=cost)
        ex_code = ex_code or normalize_exchange(meta.get("exchange")) or normalize_exchange(meta.get("mic_code"))
        sess = sess or exchange_session(ex_code)
        tz = self._zone(meta.get("exchange_timezone"), sess)
        eid = entity_id or f"{ex_code or meta.get('exchange') or '?'}:{symbol}"
        bars, dropped = self._parse(values, eid, interval, adjust, tz, sess, start, effective_end, now, latest)
        n_trunc = len(values) >= MAX_OUTPUT
        for label, count in dropped.items():
            if count:
                notes.append(f"Dropped {count} {label}.")
        if clamped:
            notes.append(f"Requested end {requested_end} clamped to last completed session {effective_end}.")
        if not include_partial and sess is not None and requested_end > latest:
            notes.append("Unfinished session excluded; use include_partial for today's bars (marked is_final=false).")
        notes.append(f"Provider end_date is exclusive; sent {(effective_end + timedelta(days=1)).isoformat()} and filtered locally.")
        if not bars:
            return ToolResult(status="empty", reason="no_bars", suggested_actions=suggestion if clamped else [],
                              permissions=gate, provenance=prov, cost=cost,
                              coverage=Coverage(entity_id=eid, requested=self._req(start, end), session_status=status, notes=notes))
        last = bars[-1]
        all_final = all(b.is_final for b in bars)
        cov = Coverage(entity_id=eid, requested=self._req(start, end),
                       actual={"first": bars[0].ts_start.isoformat(), "last": last.ts_start.isoformat()},
                       rows=len(bars), data_as_of=last.ts_start, currency=(str(meta["currency"]) if meta.get("currency") else None),
                       session_status=status, is_final=all_final,
                       truncated=n_trunc, notes=notes)
        reason = "truncated" if n_trunc else "range_clamped" if clamped else "ok"
        return ToolResult(status="ok" if reason == "ok" else "partial", reason=reason, coverage=cov, permissions=gate,
                          provenance=prov, cost=cost, data=bars,
                          suggested_actions=suggestion if clamped else [],
                          message="" if reason == "ok" else {"truncated": "Provider output limit reached; later bars may be missing.",
                                                             "range_clamped": "Requested end was after the latest completed session."}[reason])

    @staticmethod
    def _req(start, end) -> dict[str, str | None]:
        return {"start": start.isoformat() if start else None, "end": end.isoformat() if end else None}

    @staticmethod
    def _zone(name, sess) -> ZoneInfo:
        for candidate in (name, sess.timezone if sess else None, "UTC"):
            try:
                return ZoneInfo(candidate)
            except (TypeError, ValueError, ZoneInfoNotFoundError):
                continue
        return ZoneInfo("UTC")

    def _parse(self, values, eid, interval, adjust, tz, sess, start, end, now, latest):
        dropped = {"zero-volume placeholder intraday bars": 0, "rows with invalid or non-positive prices": 0,
                   "duplicate rows": 0, "rows outside the requested range": 0}
        intraday = interval in INTRADAY_INTERVALS
        has_volume = any((_dec(v.get("volume")) or 0) > 0 for v in values if isinstance(v, dict))
        seen, out = set(), []
        for row in values:
            if not isinstance(row, dict) or not isinstance(row.get("datetime"), str):
                dropped["rows with invalid or non-positive prices"] += 1
                continue
            text = row["datetime"]
            try:
                local = datetime.fromisoformat(text)
            except ValueError:
                dropped["rows with invalid or non-positive prices"] += 1
                continue
            px = [_dec(row.get(k)) for k in ("open", "high", "low", "close")]
            if any(p is None or p <= 0 for p in px) or px[1] < px[2]:
                dropped["rows with invalid or non-positive prices"] += 1
                continue
            vol = _dec(row.get("volume"))
            day = local.date()
            if (start and day < start) or day > end:
                dropped["rows outside the requested range"] += 1
                continue
            if intraday and has_volume and vol is not None and vol == 0:
                dropped["zero-volume placeholder intraday bars"] += 1
                continue
            if not intraday and local.time() == time(0, 0) and sess is not None:
                local = datetime.combine(day, sess.open)
            ts = local.replace(tzinfo=tz) if local.tzinfo is None else local
            if ts in seen:
                dropped["duplicate rows"] += 1
                continue
            seen.add(ts)
            session = None
            if sess is not None:
                session = "regular" if (not intraday or sess.open <= ts.astimezone(tz).time() < sess.close) else "extended"
            out.append(Bar(entity_id=eid, interval=interval, ts_start=ts, open=px[0], high=px[1], low=px[2], close=px[3],
                           volume=vol, adjustment=adjust, session=session,
                           is_final=self._final(interval, day, latest)))
        out.sort(key=lambda b: b.ts_start)
        return out, dropped

    @staticmethod
    def _final(interval: str, day: date, latest: date) -> bool:
        if interval == "1week":
            return day + timedelta(days=4) <= latest  # week bars start Monday; final once Friday settled
        if interval == "1month":
            nxt = date(day.year + (day.month == 12), day.month % 12 + 1, 1)
            return nxt - timedelta(days=1) <= latest
        return day <= latest
