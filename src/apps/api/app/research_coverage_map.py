"""Coverage and gap map of a research run (innovation I4).

Deterministic: every field is derived from what the runtime actually recorded (tool events, acquired sources,
read ranges, document page maps, failed source accesses, the research clock) and never from the model's own
account of what it did. Per data category it answers: was the material obtained, what was attempted, how
current is it relative to the research clock, how much of each document was read or could not be searched, which
accesses failed and why, and what is explicitly *not established*.

Robust to partial and interrupted runs and to older event shapes: unknown event kinds and malformed rows are
skipped, absent fields lower the information in the map but never raise.

Public entry points: ``build_coverage_map`` (from a runtime checkpoint ``state`` and/or explicit pieces),
``model_summary`` (compact model-facing dict) and ``ui_view`` (UI-facing structure).
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import json
import re
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from .research_clock import (
    holidays_modelled as clock_holidays_modelled,
    trading_days_between as clock_trading_days_between,
)

VERSION = 1
LOW_OCR_CONFIDENCE = 70.0

CATEGORIES: tuple[str, ...] = ("market_prices", "financial_statements", "filings", "news_web", "macro", "uploads", "other")
CATEGORY_LABELS = {
    "market_prices": {"zh": "行情价格", "en": "Market prices"},
    "financial_statements": {"zh": "财务报表", "en": "Financial statements"},
    "filings": {"zh": "申报与官方文件", "en": "Filings and official documents"},
    "news_web": {"zh": "新闻与网页", "en": "News and web"},
    "macro": {"zh": "宏观数据", "en": "Macro data"},
    "uploads": {"zh": "用户资料", "en": "User uploads"},
    "other": {"zh": "其他", "en": "Other"},
}
STATUSES = ("obtained", "partial", "not_found", "blocked_by_terms", "license_restricted", "tool_unavailable", "failed",
            "not_attempted")
STATUS_LABELS = {
    "obtained": {"zh": "已取得", "en": "Obtained"}, "partial": {"zh": "部分取得", "en": "Partial"},
    "not_found": {"zh": "未找到", "en": "Not found"}, "blocked_by_terms": {"zh": "条款禁止自动访问", "en": "Blocked by terms"},
    "license_restricted": {"zh": "受许可限制", "en": "License restricted"}, "tool_unavailable": {"zh": "工具不可用", "en": "Tool unavailable"},
    "failed": {"zh": "获取失败", "en": "Failed"}, "not_attempted": {"zh": "未尝试", "en": "Not attempted"},
}

# --------------------------------------------------------------------------------------------------
# Rule tables
# --------------------------------------------------------------------------------------------------

# Tool name -> category of the data it fetches. ``acquire_sources`` items are classified by host instead.
TOOL_CATEGORY: dict[str, str] = {
    "historical_prices": "market_prices", "market_data": "market_prices",
    "company_financials": "financial_statements", "sec_company_concept": "financial_statements", "sec_company_facts": "financial_statements",
    "filings": "filings", "issuer_documents": "filings", "sec_filings": "filings", "sec_full_text_search": "filings",
    "news_search": "news_web", "acquire_sources": "news_web",
}
_MACRO_TOOL = re.compile(r"^(?:macro|fred|world_?bank|imf|ecb|oecd|bls|census)", re.I)
_DATA_TOOLS = frozenset(TOOL_CATEGORY)

# Reason code -> status class used when nothing could be obtained. See research_runtime.TOOL_FAILURE_GUIDANCE.
REASON_STATUS: dict[str, str] = {
    "source_blocked_terms": "blocked_by_terms",
    "license_platform_key_not_permitted": "license_restricted", "source_not_authorized": "license_restricted",
    "license_restricted": "license_restricted", "not_exportable": "license_restricted", "source_not_storable": "license_restricted",
    "market_data_key_required": "tool_unavailable", "fin_data_unavailable": "tool_unavailable", "not_in_policy": "tool_unavailable",
    "invalid_api_key": "tool_unavailable", "provider_plan_restriction": "tool_unavailable", "daily_quota_exhausted": "tool_unavailable",
    "provider_rate_limited": "tool_unavailable", "rate_limited": "tool_unavailable", "time_budget": "tool_unavailable",
    "budget_exhausted": "tool_unavailable", "cost_unknown": "tool_unavailable", "provider_unavailable": "tool_unavailable",
    "no_ocr_engine": "tool_unavailable", "input_unavailable": "tool_unavailable",
    "no_bars": "not_found", "symbol_not_found": "not_found", "no_matching_filings": "not_found", "no_xbrl_facts": "not_found",
    "tag_not_reported": "not_found", "no_matching_lines": "not_found", "entity_not_found": "not_found", "cik_not_found": "not_found",
    "no_sec_filings_for_entity": "not_found", "search_no_results": "not_found", "not_found": "not_found",
    "unit_or_period_not_found_in_returned_records": "not_found", "range_after_latest_session": "not_found",
    "search_sources_denied": "not_found", "lead_not_discovered": "not_found",
}
# Priority when several failure classes coexist and nothing was obtained (most actionable first).
_STATUS_PRIORITY = ("blocked_by_terms", "license_restricted", "tool_unavailable", "failed", "not_found")

_OK_STATUSES = frozenset({"acquired", "completed", "created", "ok", "reused"})
_IGNORED_STATUSES = frozenset({"invalid", "duplicate_action", "duplicate", "pending", "started"})

PRIMARY_HOSTS = ("sec.gov", "hkexnews.hk", "cninfo.com.cn", "sse.com.cn", "szse.cn", "edinet-fsa.go.jp", "esma.europa.eu",
                 "sedarplus.ca", "dart.fss.or.kr")
MACRO_HOSTS = ("fred.stlouisfed.org", "federalreserve.gov", "bls.gov", "bea.gov", "census.gov", "stats.gov.cn", "pbc.gov.cn",
               "ecb.europa.eu", "imf.org", "worldbank.org", "oecd.org", "ons.gov.uk", "e-stat.go.jp", "ecos.bok.or.kr")
# FRED terms are not cleared for this project's agents (AGENTS.md): any FRED access is a license restriction.
LICENSE_RESTRICTED_HOSTS = ("fred.stlouisfed.org",)
MACRO_PROVIDERS = frozenset({"fred", "world_bank", "imf", "ecb", "oecd", "bls", "bea", "census"})
_PRICE_METRIC = re.compile(r"收盘|收市|开盘|首笔|最高|最低|\bclos(?:e|ing)\b|\bopen\b|\bhigh\b|\blow\b", re.I)


# --------------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------------


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, Mapping):
        return dict(obj)
    dump = getattr(obj, "model_dump", None)
    return dump(mode="json") if callable(dump) else {}


def _maps(rows: Any) -> list[dict[str, Any]]:
    return [_as_dict(r) for r in rows if isinstance(r, Mapping) or hasattr(r, "model_dump")] if isinstance(rows, (list, tuple)) else []


def _str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _host(url: Any) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    host = (urlparse(url).hostname or "").lower()
    return (host[4:] if host.startswith("www.") else host) or None


def _host_in(host: str | None, patterns: Iterable[str]) -> bool:
    return bool(host) and any(host == p or host.endswith("." + p) for p in patterns)


def _clip(text: Any, limit: int = 160) -> str | None:
    text = _str(text)
    if text is None:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _ranges(numbers: Sequence[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for n in sorted(set(numbers)):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return out


def _union(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(r for r in ranges if r[1] > r[0]):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _json_or_map(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def trading_days_between(after: date, up_to: date, exchange: str | None = None) -> int:
    """Trading days in ``(after, up_to]``, delegating to ``research_clock.trading_days_between``."""
    return clock_trading_days_between(after, up_to, exchange=exchange)


# --------------------------------------------------------------------------------------------------
# Source normalisation
# --------------------------------------------------------------------------------------------------


def _source_as_of(src: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """(date, kind) of the data a source holds, preferring the provider's actual last date over publication time."""
    prov = src.get("provenance") if isinstance(src.get("provenance"), Mapping) else {}
    for key in ("actual_last_date", "actual_last", "effective_end_date"):
        if _date(prov.get(key)):
            return _date(prov[key]).isoformat(), "actual_last_date"
    cov = prov.get("coverage") if isinstance(prov.get("coverage"), Mapping) else {}
    actual = cov.get("actual") if isinstance(cov.get("actual"), Mapping) else {}
    if _date(actual.get("last")):
        return _date(actual["last"]).isoformat(), "actual_last_date"
    if _date(cov.get("data_as_of")):
        return _date(cov["data_as_of"]).isoformat(), "data_as_of"
    if _date(src.get("published_at")):
        return _date(src["published_at"]).isoformat(), "published_at"
    return None, None


def _title_range_end(title: str | None) -> str | None:
    match = re.search(r"\((\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})\)", title or "")
    return match.group(2) if match else None


def _instrument_of(src: Mapping[str, Any], action_target: str | None) -> str | None:
    prov = src.get("provenance") if isinstance(src.get("provenance"), Mapping) else {}
    for key in ("instrument_id", "entity_id"):
        if _str(prov.get(key)):
            return prov[key]
    if _str(prov.get("symbol")):
        return f"{prov['symbol']}@{prov.get('exchange') or '?'}"
    if action_target:
        return action_target
    match = re.match(r"([A-Za-z0-9.\-:@]+) (?:daily close|\d+\w+ bars)", _str(src.get("title")) or "")
    return match.group(1) if match else None


def _category_of_source(src: Mapping[str, Any], action_tool: str | None, issuer_hosts: Sequence[str] = ()) -> str:
    kind, method = src.get("kind"), src.get("retrieval_method")
    prov = src.get("provenance") if isinstance(src.get("provenance"), Mapping) else {}
    host = _host(src.get("url"))
    title = _str(src.get("title")) or ""
    if kind == "market_data" or method in ("market_data", "historical_prices") or action_tool in ("historical_prices", "market_data") \
            or prov.get("provider") == "twelve_data":
        return "market_prices"
    if kind is None and method is None and not host and re.search(r"daily close|\bbars\b", title, re.I):
        return "market_prices"
    if method in ("user_supplied", "user_pdf", "user_html") or (kind == "user_supplied" and not method):
        return "uploads"
    if method in ("sec_companyconcept", "sec_companyfacts", "structured_financials") or action_tool in ("company_financials", "sec_company_concept", "sec_company_facts"):
        return "financial_statements"
    if method == "sec_filing_html" or action_tool in ("filings", "sec_filings") or _host_in(host, PRIMARY_HOSTS):
        return "filings"
    if _str(prov.get("provider")) in MACRO_PROVIDERS or _host_in(host, MACRO_HOSTS) or (action_tool and _MACRO_TOOL.match(action_tool)):
        return "macro"
    from .research_reconciliation import classify_source
    if classify_source(src, issuer_hosts=issuer_hosts) == "issuer":
        return "filings"
    if host or method in ("web_html", "web_pdf", "official_html") or action_tool == "acquire":
        return "news_web"
    return "other"


def _category_of_host(host: str | None) -> str:
    if _host_in(host, MACRO_HOSTS):
        return "macro"
    if _host_in(host, PRIMARY_HOSTS):
        return "filings"
    return "news_web"


def _split_action(key: str) -> tuple[str, str | None, list[str]]:
    parts = key.split("|")
    return parts[0], (parts[1] if len(parts) > 1 else None), parts[2:]


# --------------------------------------------------------------------------------------------------
# The map
# --------------------------------------------------------------------------------------------------


def _clock_info(clock: Mapping[str, Any]) -> dict[str, Any]:
    exchanges = [e for e in (clock.get("exchanges") or []) if isinstance(e, Mapping)]
    sessions = {str(e.get("exchange")): _date(e.get("last_completed_session")) for e in exchanges if e.get("exchange")}
    latest = max((d for d in sessions.values() if d), default=None)
    earliest = min((d for d in sessions.values() if d), default=None)
    return {"captured_at": _str(clock.get("captured_at")), "utc_date": _date(clock.get("utc_date") or clock.get("captured_at")),
            "sessions": sessions, "latest_session": latest, "earliest_session": earliest,
            "exchange_session_status": {str(e.get("exchange")): e.get("session_status") for e in exchanges if e.get("exchange")}}


def _normalize_exchange(raw: Any) -> str | None:
    try:
        from .fin_data.models import normalize_exchange
    except Exception:  # pragma: no cover - the data layer is optional for this module
        return None
    return normalize_exchange(raw) if isinstance(raw, str) else None


def _staleness(as_of: str | None, clock: Mapping[str, Any], exchange: str | None, *, price_like: bool) -> dict[str, Any] | None:
    day, utc = _date(as_of), clock.get("utc_date")
    if day is None or not clock or (utc is None and clock.get("latest_session") is None):
        return None
    session = clock["sessions"].get(exchange) if exchange else None
    basis = "exchange" if session else "earliest_across_exchanges"
    ref_exchange = exchange if exchange and clock["sessions"].get(exchange) else None
    # Unknown exchange: judge against the *earliest* last completed session among the clock's exchanges, so data is
    # only called stale when it lags every market (never because an earlier-closing Asian exchange is already a day on).
    session = session or clock.get("earliest_session")
    years_spanned = (
        range(day.year, session.year + 1)
        if (session and day < session)
        else ((session.year,) if session else ())
    )
    modelled = bool(ref_exchange and session and clock_holidays_modelled(ref_exchange, years=years_spanned))
    out: dict[str, Any] = {"reference_exchange": ref_exchange,
                           "reference_basis": basis, "reference_session": session.isoformat() if session else None,
                           "days_before_clock_date": (utc - day).days if utc else None,
                           "days_before_last_session": (session - day).days if session else None,
                           "trading_days_before_last_session": trading_days_between(day, session, exchange=ref_exchange) if session else None,
                           "holidays_modelled": modelled}
    out["is_stale"] = (out["trading_days_before_last_session"] or 0) >= 1 if price_like and session else None
    return out


def build_coverage_map(state: Mapping[str, Any] | None = None, *, events: Sequence[Any] | None = None,
                       sources: Sequence[Any] | None = None, read_ranges: Sequence[Any] | None = None,
                       document_pages: Mapping[str, Any] | None = None,
                       failed_source_accesses: Sequence[Any] | None = None, acquired_actions: Mapping[str, Any] | None = None,
                       research_clock: Mapping[str, Any] | str | None = None, search_coverage: Mapping[str, Any] | None = None,
                       facts: Sequence[Any] | None = None, gaps: Sequence[Any] | None = None,
                       available_instruments: Sequence[str] | None = None) -> dict[str, Any]:
    """Build the per-category coverage map.

    ``state`` is a runtime checkpoint (``events``, ``_sources``, ``read_ranges``, ``failed_source_accesses``,
    ``acquired_actions``, ``user`` JSON with ``research_clock`` / ``available_historical_price_instruments``,
    ``pending``); each explicit keyword overrides the matching piece. ``sources`` should be the richer source
    records (title, url, kind, retrieval_method, provenance incl. ``document_pages``); checkpoint ``_sources``
    rows only carry ids and timestamps and are merged underneath. ``document_pages`` maps ``source_id`` to a
    document page map when not inside ``provenance``. ``search_coverage`` is ``ResearchLibrary.coverage()``.
    ``facts``/``gaps`` (optional) enable the "no exchange price data for X" statements.
    """
    state = state if isinstance(state, Mapping) else {}
    user = _json_or_map(state.get("user"))
    clock_raw = research_clock if research_clock is not None else user.get("research_clock")
    clock = _clock_info(_json_or_map(clock_raw))
    instruments_known = list(available_instruments) if available_instruments is not None else (
        [i for i in user.get("available_historical_price_instruments", []) if isinstance(i, str)]
        if isinstance(user.get("available_historical_price_instruments"), list) else None)
    ev = _maps(events if events is not None else state.get("events"))
    ranges_in = _maps(read_ranges if read_ranges is not None else state.get("read_ranges"))
    failed_in = _maps(failed_source_accesses if failed_source_accesses is not None else state.get("failed_source_accesses"))
    actions = acquired_actions if isinstance(acquired_actions, Mapping) else (
        state.get("acquired_actions") if isinstance(state.get("acquired_actions"), Mapping) else {})
    gap_codes = {str(g.get("code")) for g in _maps(gaps) if g.get("code")} if gaps is not None else set()

    # ---- sources: richer records over checkpoint metadata, keyed by id ------------------------------------------------
    by_id: dict[str, dict[str, Any]] = {}
    for row in _maps(state.get("_sources")) + _maps(sources):
        sid = _str(row.get("source_id"))
        if sid:
            merged = by_id.setdefault(sid, {})
            merged.update({k: v for k, v in row.items() if v is not None or k not in merged})
    for sid, key_ids in ((s, k) for k, ids in actions.items() if isinstance(ids, list) for s in ids if isinstance(s, str)):
        by_id.setdefault(sid, {"source_id": sid})["_action"] = key_ids
    for lead_url, sid in (state.get("acquired_lead_sources") or {}).items() if isinstance(state.get("acquired_lead_sources"), Mapping) else ():
        if isinstance(sid, str):
            by_id.setdefault(sid, {"source_id": sid}).setdefault("url", lead_url)
    for sid in state.get("acquired_source_ids") or []:
        if isinstance(sid, str):
            by_id.setdefault(sid, {"source_id": sid})

    # ---- read ranges: checkpoint rows, else derived from read_sources events ---------------------------------------
    reads: dict[str, list[tuple[int, int]]] = {}
    totals: dict[str, int] = {}
    for row in ranges_in:
        if _str(row.get("source_id")) and isinstance(row.get("start"), int) and isinstance(row.get("end"), int):
            reads.setdefault(row["source_id"], []).append((row["start"], row["end"]))
    derive_reads = not ranges_in
    for e in ev:
        summary = e.get("summary") if isinstance(e.get("summary"), Mapping) else {}
        if e.get("name") == "read_sources":
            for item in summary.get("items") or []:
                if isinstance(item, Mapping) and _str(item.get("source_id")):
                    if isinstance(item.get("total_characters"), int):
                        totals[item["source_id"]] = item["total_characters"]
                    if derive_reads and isinstance(item.get("start"), int) and isinstance(item.get("end"), int):
                        reads.setdefault(item["source_id"], []).append((item["start"], item["end"]))

    ocr_requests: dict[str, dict[str, Any]] = {}
    for e in ev:
        ocr = e.get("ocr") if isinstance(e.get("ocr"), Mapping) else None
        if ocr is None and e.get("kind") == "ocr":
            ocr = e
        if ocr and _str(ocr.get("source_id")):
            slot = ocr_requests.setdefault(ocr["source_id"], {"status": None, "pages": set()})
            slot["status"] = ocr.get("status") or slot["status"]
            slot["pages"].update(p for p in (ocr.get("pages") or []) if isinstance(p, int))
            if _str(ocr.get("previous_source_id")):
                ocr_requests.setdefault(ocr["previous_source_id"], {"status": slot["status"], "pages": set()})["pages"].update(slot["pages"])

    # ---- per-category scratch space ----------------------------------------------------------------------------------
    cats: dict[str, dict[str, Any]] = {c: {"attempts": [], "counts": Counter(), "failures": [], "sources": [], "events": 0,
                                           "partial_signals": [], "denied_reasons": Counter(), "ok_events": 0} for c in CATEGORIES}

    for sid, src in by_id.items():
        action_tool, action_target = None, None
        if isinstance(src.get("_action"), str):
            action_tool, action_target, _ = _split_action(src["_action"])
            if action_tool == "acquire" and action_target and not src.get("url"):
                src["url"] = action_target
        cat = _category_of_source(src, action_tool, state.get("issuer_site_hosts") or ())
        src["_category"], src["_action_tool"], src["_action_target"] = cat, action_tool, action_target
        cats[cat]["sources"].append(sid)

    def attempt(cat: str, tool: str, status: str, reason: str | None, target: str | None, purpose: str | None, **extra: Any) -> None:
        key = (tool, status, reason, target)
        for row in cats[cat]["attempts"]:
            if (row["tool"], row["status"], row["reason"], row["target"]) == key:
                row["count"] += 1
                return
        cats[cat]["attempts"].append({"tool": tool, "status": status, "reason": reason, "target": target, "purpose": purpose,
                                      "count": 1, **{k: v for k, v in extra.items() if v is not None}})

    run_tool_counts: Counter[str] = Counter()
    model_failures: Counter[str] = Counter()
    submission: dict[str, Any] | None = None
    model_calls = 0
    for e in ev:
        kind = e.get("kind")
        if kind == "model":
            model_calls += 1
            continue
        if kind == "model_failure":
            model_failures[_str(e.get("code")) or "unknown"] += 1
            continue
        if kind == "submission":
            submission = {"status": e.get("status"), "stopping_reason": _clip(e.get("stopping_reason"), 300)}
            continue
        if kind != "tool":
            continue
        name = _str(e.get("name") or e.get("tool")) or "unknown"
        status = _str(e.get("status")) or "unknown"
        run_tool_counts[f"{name}:{status}"] += 1
        cat = TOOL_CATEGORY.get(name) or ("macro" if _MACRO_TOOL.match(name) else None)
        if cat is None or status in _IGNORED_STATUSES:
            continue
        summary = e.get("summary") if isinstance(e.get("summary"), Mapping) else {}
        reason = _str(e.get("reason"))
        purpose = _clip(e.get("rationale"))
        c = cats[cat]
        c["events"] += 1
        if name == "news_search":
            found, denied = summary.get("found_count"), e.get("denied_count", summary.get("denied_count"))
            eligible = summary.get("eligible_count")
            c["counts"]["discover_calls"] += 1
            for key, val in (("leads_found", found), ("leads_denied", denied), ("leads_eligible", eligible)):
                if isinstance(val, int):
                    c["counts"][key] += val
            if isinstance(e.get("denied_reasons"), Mapping):
                for r, n in e["denied_reasons"].items():
                    if isinstance(n, int) and n > 0:
                        c["denied_reasons"][str(r)] += n
            attempt(cat, name, status, reason, None, purpose, leads_found=found, leads_denied=denied)
            if isinstance(found, int) and found == 0:
                c["failures"].append({"reason": "search_no_results", "host": None, "url": None, "title": None, "tool": name})
            elif isinstance(eligible, int) and eligible == 0 and isinstance(denied, int) and denied > 0:
                c["failures"].append({"reason": "search_sources_denied", "host": None, "url": None, "title": None, "tool": name})
            else:
                c["ok_events"] += 1
            continue
        if name == "acquire_sources":
            items = [i for i in (summary.get("items") or []) if isinstance(i, Mapping)]
            attempt(cat, name, status, reason, None, purpose, items=len(items) or None)
            c["counts"]["acquire_calls"] += 1
            for item in items:
                host = _str(item.get("host"))
                icat = _category_of_host(host)
                target = cats[icat]
                ok = item.get("status") == "acquired" or (item.get("reason") is None and item.get("status") in _OK_STATUSES)
                target["counts"]["acquire_items"] += 1
                if ok:
                    target["counts"]["acquired"] += 1
                    target["ok_events"] += 1
                    target.setdefault("acquired_hosts", []).append(host)
                else:
                    target["counts"]["acquire_failed"] += 1
                    target["failures"].append({"reason": _str(item.get("reason")) or "source_unavailable", "host": host, "url": None,
                                               "title": _clip(item.get("title"), 120), "tool": name, "_from_event": True})
            if not items and status not in _OK_STATUSES and reason:
                c["failures"].append({"reason": reason, "host": None, "url": None, "title": None, "tool": name})
            continue
        # financial data / price / filings tools
        target = (_clip(summary.get("instrument_id") or summary.get("title") or summary.get("entity")
                        or summary.get("tag") or summary.get("kind"), 120))
        attempt(cat, name, status, reason, target, purpose, end_date=summary.get("end_date"), start_date=summary.get("start_date"))
        if status in _OK_STATUSES or status == "partial":
            c["counts"]["ok_calls"] += 1
            c["ok_events"] += 1
            if status == "partial":
                c["partial_signals"].append(f"{name}_partial")
        else:
            c["failures"].append({"reason": reason or status, "host": None, "url": None, "title": target, "tool": name,
                                  "event_status": status})

    # failed accesses recorded by the runtime (they carry the url); event items without url fill the remainder
    for row in failed_in:
        host = _str(row.get("host")) or _host(row.get("url"))
        cat = _category_of_host(host)
        cats[cat]["failures"].append({"reason": _str(row.get("reason")) or "source_unavailable", "host": host, "url": _str(row.get("url")),
                                      "title": None, "tool": "acquire_sources", "_from_state": True})
    for cat, c in cats.items():
        state_rows = [f for f in c["failures"] if f.get("_from_state")]
        event_rows = [f for f in c["failures"] if f.get("_from_event")]
        other_rows = [f for f in c["failures"] if not f.get("_from_state") and not f.get("_from_event")]
        keep_events: list[dict[str, Any]] = []
        budget = Counter((f["host"], f["reason"]) for f in state_rows)
        for f in event_rows:
            key = (f["host"], f["reason"])
            if budget[key] > 0:
                budget[key] -= 1
                continue
            keep_events.append(f)
        c["failures"] = [{k: v for k, v in f.items() if not k.startswith("_")} for f in state_rows + keep_events + other_rows]

    # ---- documents ---------------------------------------------------------------------------------------------------
    search_info: dict[str, dict[str, Any]] = {}
    excluded_from_search: dict[str, str] = {}
    if isinstance(search_coverage, Mapping):
        for row in search_coverage.get("searched_sources") or []:
            if isinstance(row, Mapping) and _str(row.get("source_id")):
                search_info[row["source_id"]] = dict(row)
        for row in search_coverage.get("excluded_sources") or []:
            if isinstance(row, Mapping) and _str(row.get("source_id")):
                excluded_from_search[row["source_id"]] = str(row.get("reason") or "excluded")

    # This is an unverified domain hint, not evidence that a site is the issuer.
    from .research_reconciliation import classify_source
    candidates = state.get('entity_candidates') if isinstance(state.get('entity_candidates'), Mapping) else {}
    names = {str(info.get('company') or '').strip() for info in candidates.values() if isinstance(info, Mapping)}
    filing_headers = [re.sub(r'[^a-z0-9]', '', str(src.get('title') or '').casefold() + str(src.get('text') or '')[:15000].casefold())
                      for src in by_id.values() if classify_source(src, issuer_hosts=state.get('issuer_site_hosts') or ()) == 'filing']
    possible_issuer_tokens = set()
    for name in names:
        full = re.sub(r'[^a-z0-9]', '', name.casefold())
        first = re.match(r'[a-z0-9]{4,}', name.casefold())
        if first and full and any(full in header for header in filing_headers):
            possible_issuer_tokens.add(first.group())
    documents: dict[str, dict[str, Any]] = {}
    for sid, src in by_id.items():
        cat = src["_category"]
        prov = src.get("provenance") if isinstance(src.get("provenance"), Mapping) else {}
        pages = (document_pages or {}).get(sid) if isinstance(document_pages, Mapping) else None
        pages = pages if isinstance(pages, Mapping) else prov.get("document_pages")
        pages = pages if isinstance(pages, Mapping) and isinstance(pages.get("pages"), list) else None
        text = src.get("text") if isinstance(src.get("text"), str) else None
        covg = src.get("initial_prompt_coverage") if isinstance(src.get("initial_prompt_coverage"), Mapping) else {}
        total = len(text) if text is not None else (covg.get("text_characters") if isinstance(covg.get("text_characters"), int) else totals.get(sid))
        merged_reads = _union(reads.get(sid, []))
        chars_read = sum(e - s for s, e in merged_reads if total is None) if total is None else sum(
            max(0, min(e, total) - max(s, 0)) for s, e in merged_reads)
        source_class = classify_source(src, issuer_hosts=state.get("issuer_site_hosts") or ())
        host_labels = (_host(src.get("url")) or "").split(".")
        origin = ("issuer_candidate_unverified" if source_class == "news" and any(token in host_labels for token in possible_issuer_tokens)
                  else {"filing": "regulatory_filing", "issuer": "verified_issuer", "news": "website_unverified",
                        "market_data": "data_provider", "user_upload": "user_upload"}.get(source_class, "unknown"))
        doc: dict[str, Any] = {"origin": origin, "source_class": source_class, "retrieval_method": src.get("retrieval_method"),
                               "ingestion_kind": src.get("kind"), "permissions": src.get("permissions"),
                               "source_id": sid, "category": cat, "title": _clip(src.get("title"), 160), "url": _str(src.get("url")),
                               "host": _host(src.get("url")), "chars_total": total, "chars_read": chars_read,
                               "read_fraction": round(chars_read / total, 4) if total else None, "read_range_count": len(merged_reads),
                               "pages": None, "ocr": None, "search": None}
        if pages is not None:
            doc["pages"] = _page_summary(pages, merged_reads, ocr_requests.get(sid))
            doc["method"] = pages.get("method")
        if sid in ocr_requests:
            doc["ocr"] = {"status": ocr_requests[sid]["status"], "pages_requested": sorted(ocr_requests[sid]["pages"])}
        if sid in search_info:
            unsearched = search_info[sid].get("unsearched_pages") or {}
            doc["search"] = {"passages": search_info[sid].get("passages"), "pages_total": search_info[sid].get("pages_total"),
                             "unsearched_pages": {k: v for k, v in unsearched.items() if isinstance(v, Mapping) and v.get("count")},
                             "note": search_info[sid].get("note")}
        elif sid in excluded_from_search:
            doc["search"] = {"excluded": excluded_from_search[sid]}
        documents[sid] = doc

    # ---- per-category assembly ---------------------------------------------------------------------------------------
    run_status = ("submitted" if submission and submission.get("status") == "completed"
                  else "interrupted" if model_failures else "incomplete")
    out_cats: list[dict[str, Any]] = []
    not_established: list[dict[str, Any]] = []
    for cat in CATEGORIES:
        c = cats[cat]
        docs = [documents[s] for s in c["sources"] if s in documents]
        entry = _assemble_category(cat, c, docs, by_id, actions, clock, ev, gap_codes)
        out_cats.append(entry)
        not_established.extend(entry["not_established"])

    market_statements = _market_gap_statements(facts, by_id, cats["market_prices"], instruments_known, ev, gap_codes, bool(user) or available_instruments is not None)
    for st in market_statements:
        not_established.append(st)
        out_cats[CATEGORIES.index("market_prices")]["not_established"].append(st)
    if run_status != "submitted":
        not_established.append({"code": "run_not_submitted", "category": None, "params": {"run_status": run_status, "model_failures": dict(model_failures)},
                                "message": "The run ended before submitting research; this map shows only the material collected so far."})

    return {
        "version": VERSION,
        "run": {"status": run_status, "model_calls": model_calls, "model_failures": dict(model_failures), "submission": submission,
                "pending": _clip((state.get("pending") or {}).get("kind"), 40) if isinstance(state.get("pending"), Mapping) else None,
                "tool_event_counts": dict(sorted(run_tool_counts.items()))},
        "research_clock": {"captured_at": clock["captured_at"], "utc_date": clock["utc_date"].isoformat() if clock["utc_date"] else None,
                           "latest_completed_session": clock["latest_session"].isoformat() if clock["latest_session"] else None,
                           "available": bool(clock["sessions"] or clock["utc_date"])},
        "categories": out_cats,
        "documents": [documents[s] for s in documents if documents[s]["chars_read"] or documents[s]["pages"] or documents[s]["chars_total"]],
        "not_established": not_established,
        "status_counts": dict(Counter(c["status"] for c in out_cats)),
    }


def _page_summary(pages: Mapping[str, Any], reads: list[tuple[int, int]], ocr_request: Mapping[str, Any] | None) -> dict[str, Any]:
    status_pages: dict[str, list[int]] = {}
    read_pages: list[int] = []
    low_conf: list[int] = []
    ocr_meta = pages.get("ocr") if isinstance(pages.get("ocr"), Mapping) else {}
    for number, entry in enumerate(pages["pages"], 1):
        if not isinstance(entry, (list, tuple)) or len(entry) < 3:
            continue
        start, end, status = entry[0], entry[1], str(entry[2])
        status_pages.setdefault(status, []).append(number)
        if status in ("text", "ocr") and isinstance(start, int) and isinstance(end, int) and any(s < end and start < e for s, e in reads):
            read_pages.append(number)
        meta = ocr_meta.get(str(number)) if isinstance(ocr_meta, Mapping) else None
        if status == "ocr" and isinstance(meta, Mapping) and isinstance(meta.get("mean_confidence"), (int, float)) \
                and meta["mean_confidence"] < LOW_OCR_CONFIDENCE:
            low_conf.append(number)
    unsearched = {k: _ranges(v) for k, v in status_pages.items() if k in ("needs_ocr", "not_included", "failed")}
    return {"total": len(pages["pages"]), "page_count_declared": pages.get("page_count"),
            "page_count_complete": pages.get("page_count_complete", True), "text_limit_reached": bool(pages.get("text_limit_reached")),
            "by_status": {k: len(v) for k, v in sorted(status_pages.items())}, "read": len(read_pages), "read_ranges": _ranges(read_pages),
            "unread_text_pages": sum(len(status_pages.get(k, [])) for k in ("text", "ocr")) - len(read_pages),
            "unsearched_pages": unsearched, "unsearched_count": sum(len(v) for k, v in status_pages.items() if k in unsearched),
            "ocr_low_confidence_pages": low_conf, "original_retained": pages.get("original_retained")}


def _assemble_category(cat: str, c: dict[str, Any], docs: list[dict[str, Any]], by_id: Mapping[str, dict[str, Any]],
                       actions: Mapping[str, Any], clock: Mapping[str, Any], events: list[dict[str, Any]],
                       gap_codes: set[str]) -> dict[str, Any]:
    failures = c["failures"]
    reasons = Counter(f["reason"] for f in failures)
    sources = [by_id[s] for s in c["sources"]]
    obtained_count = max(len(sources), c["counts"].get("acquired", 0) if cat in ("news_web", "filings", "macro") else 0)
    if cat in ("market_prices", "financial_statements") and not sources:
        obtained_count = c["counts"].get("ok_calls", 0)
    partial_signals = list(c["partial_signals"])
    for d in docs:
        pg = d.get("pages") or {}
        if pg.get("unsearched_count"):
            partial_signals.append("unsearched_pages")
        if pg.get("text_limit_reached"):
            partial_signals.append("text_limit_reached")
        if pg.get("page_count_complete") is False:
            partial_signals.append("page_count_incomplete")
    for src in sources:
        prov = src.get("provenance") if isinstance(src.get("provenance"), Mapping) else {}
        cov = prov.get("coverage") if isinstance(prov.get("coverage"), Mapping) else {}
        if cov.get("truncated") or prov.get("clamped"):
            partial_signals.append("truncated_or_clamped")

    restricted_hosts = [s for s in sources if _host_in(_host(s.get("url")), LICENSE_RESTRICTED_HOSTS)]
    for src in restricted_hosts:   # FRED etc.: obtained text exists but is not cleared for agent use
        failures.append({"reason": "license_restricted", "host": _host(src.get("url")), "url": src.get("url"), "title": None, "tool": "acquire_sources"})
    if cat == "market_prices" and "market_data_key_required" in gap_codes and not reasons.get("market_data_key_required"):
        failures.append({"reason": "market_data_key_required", "host": None, "url": None, "title": None, "tool": "gap"})
    if cat == "market_prices" and "historical_prices_unavailable" in gap_codes and not sources:
        failures.append({"reason": "no_bars", "host": None, "url": None, "title": None, "tool": "gap"})
    reasons = Counter(f["reason"] for f in failures)
    failure_classes = Counter(REASON_STATUS.get(f["reason"], "failed") for f in failures)

    if obtained_count and not (restricted_hosts and len(restricted_hosts) == obtained_count):
        status = "partial" if (failures or partial_signals) else "obtained"
    elif restricted_hosts and obtained_count:
        status = "license_restricted"
    elif failures:
        status = next((s for s in _STATUS_PRIORITY if failure_classes.get(s)), "failed")
    elif c["attempts"]:
        status = "not_found"
    else:
        status = "not_attempted"

    # data as of / staleness
    instruments: list[dict[str, Any]] = []
    dates: list[tuple[str, str]] = []
    for src in sources:
        as_of, kind = _source_as_of(src)
        if cat == "market_prices":
            key = src.get("_action")
            tool, target, rest = _split_action(key) if isinstance(key, str) else (None, None, [])
            if as_of is None and len(rest) >= 2:
                as_of, kind = rest[1], "requested_end_date"
            if as_of is None and _title_range_end(src.get("title")):
                as_of, kind = _title_range_end(src["title"]), "title_range_end"
            prov = src.get("provenance") if isinstance(src.get("provenance"), Mapping) else {}
            exchange = _normalize_exchange(prov.get("exchange"))
            instruments.append({"source_id": src["source_id"], "instrument": _instrument_of(src, target), "exchange": exchange,
                                "interval": prov.get("interval") or ("1day" if tool == "historical_prices" else None),
                                "data_as_of": as_of, "data_as_of_kind": kind,
                                "staleness": _staleness(as_of, clock, exchange, price_like=True)})
        if as_of:
            dates.append((as_of, kind or "unknown"))
    if cat == "market_prices" and not dates:       # no source rows at all: fall back to the tool events' own dates
        for row in c["attempts"]:
            if row["status"] in _OK_STATUSES and _date(row.get("end_date")):
                dates.append((_date(row["end_date"]).isoformat(), "requested_end_date"))
    data_as_of = max(dates)[0] if dates else None
    as_of_kind = next((k for d, k in sorted(dates, reverse=True) if d == data_as_of), None) if dates else None
    min_as_of = min(dates)[0] if dates else None
    exch = next((i["exchange"] for i in instruments if i["data_as_of"] == data_as_of and i["exchange"]), None)
    unknown_dates = sum(1 for s in sources if _source_as_of(s)[0] is None and not (cat == "market_prices"))
    obtained_at = [s.get("obtained_at") for s in sources if _str(s.get("obtained_at"))]

    not_established: list[dict[str, Any]] = []
    label = CATEGORY_LABELS[cat]["en"].lower()
    if status == "not_attempted":
        if cat in ("macro", "uploads", "other"):
            pass   # nothing was asked for; a statement would only add noise
        else:
            not_established.append({"code": "category_not_attempted", "category": cat, "params": {},
                                "message": f"No {label} were requested in this run; nothing is established for this category."})
    elif status not in ("obtained", "partial"):
        not_established.append({"code": f"category_{status}", "category": cat, "params": {"reasons": dict(reasons)},
                                "message": f"No {label} could be obtained ({status}; reasons: {', '.join(sorted(reasons)) or 'none recorded'}); "
                                           f"absence of data here is not evidence that none exists."})
    elif status == "partial":
        if failures:
            hosts = sorted({f["host"] for f in failures if f.get("host")})
            not_established.append({"code": "accesses_failed", "category": cat, "params": {"count": len(failures), "reasons": dict(reasons), "hosts": hosts[:10]},
                                    "message": f"{len(failures)} access(es) for {label} failed ({', '.join(f'{r} x{n}' for r, n in sorted(reasons.items()))}); "
                                               f"what those sources say is not established."})
    for d in docs:
        pg = d.get("pages")
        if pg and pg.get("unsearched_count"):
            not_established.append({"code": "unsearched_pages", "category": cat, "params": {"source_id": d["source_id"], "pages": pg["unsearched_pages"]},
                                    "message": f"{pg['unsearched_count']} page(s) of '{d['title']}' have no extracted text (not OCR'd, excluded or failed); "
                                               f"a search miss there is not evidence of absence."})
        if pg and pg.get("text_limit_reached"):
            not_established.append({"code": "text_limit_reached", "category": cat, "params": {"source_id": d["source_id"]},
                                    "message": f"'{d['title']}' was truncated at the text limit; later pages were not extracted."})
        if pg and pg.get("ocr_low_confidence_pages"):
            not_established.append({"code": "ocr_low_confidence", "category": cat, "params": {"source_id": d["source_id"], "pages": pg["ocr_low_confidence_pages"]},
                                    "message": f"OCR text on page(s) {pg['ocr_low_confidence_pages']} of '{d['title']}' has low confidence."})
    unread = [d for d in docs if d["category"] in ("news_web", "filings", "macro", "uploads", "other") and d["chars_read"] == 0 and d.get("chars_total")]
    if unread:
        not_established.append({"code": "acquired_not_read", "category": cat, "params": {"source_ids": [d["source_id"] for d in unread][:10]},
                                "message": f"{len(unread)} acquired source(s) were never read; their content is not established."})
    if cat == "market_prices" and instruments:
        for i in instruments:
            st = i["staleness"]
            if st and st["is_stale"]:
                not_established.append({"code": "market_data_stale", "category": cat, "params": {"instrument": i["instrument"], "data_as_of": i["data_as_of"], **st},
                                        "message": f"{i['instrument']} data ends {i['data_as_of']}, {st['trading_days_before_last_session']} trading day(s) before the last completed session ({st['reference_session']})."})
    if cat in ("news_web", "filings", "macro") and unknown_dates and status in ("obtained", "partial"):
        not_established.append({"code": "published_dates_unknown", "category": cat, "params": {"count": unknown_dates},
                                "message": f"Publication dates are unknown for {unknown_dates} source(s); how current they are is not established."})

    return {
        "category": cat, "label": CATEGORY_LABELS[cat], "status": status, "status_label": STATUS_LABELS[status],
        "partial_signals": sorted(set(partial_signals)),
        "attempts": c["attempts"], "counts": dict(c["counts"]),
        "denied_lead_reasons": dict(c["denied_reasons"]),
        "sources_obtained": obtained_count, "source_ids": list(c["sources"]),
        "data_as_of": data_as_of, "data_as_of_kind": as_of_kind, "data_as_of_min": min_as_of if min_as_of != data_as_of else None,
        "latest_obtained_at": max(obtained_at) if obtained_at else None, "published_dates_unknown": unknown_dates or None,
        "staleness": _staleness(data_as_of, clock, exch, price_like=(cat == "market_prices")),
        "instruments": instruments or None,
        "failures": failures, "failure_reasons": dict(reasons),
        "documents": [d["source_id"] for d in docs],
        "not_established": not_established,
    }


def _market_gap_statements(facts: Sequence[Any] | None, by_id: Mapping[str, dict[str, Any]], market: Mapping[str, Any],
                           instruments_known: list[str] | None, events: list[dict[str, Any]], gap_codes: set[str],
                           have_tool_info: bool) -> list[dict[str, Any]]:
    """Entities whose prices the run quotes from non-exchange sources while no exchange series for them was obtained."""
    if not facts:
        return []
    from .research_reconciliation import classify_source, detect_metrics, entity_hint

    covered: set[str] = set()
    for sid in market["sources"]:
        src = by_id.get(sid, {})
        prov = src.get("provenance") if isinstance(src.get("provenance"), Mapping) else {}
        for key in ("symbol", "instrument_id", "entity_id"):
            val = _str(prov.get(key))
            if val:
                covered.add(val.split(":")[-1].split("@")[0].upper())
        action = src.get("_action_target") or ""
        if action:
            covered.add(str(action).split(":")[-1].split("@")[0].upper())
        title_symbol = re.match(r"([A-Za-z0-9.\-]+)(?:@|:)?\S* (?:daily close|1day|\w+ bars)", _str(src.get("title")) or "")
        if title_symbol:
            covered.add(title_symbol.group(1).split(":")[-1].upper())
    missing: dict[str, int] = {}
    for raw in facts:
        fact = _as_dict(raw)
        label = str(fact.get("label") or "")
        keys, spans = detect_metrics(label)
        if not _PRICE_METRIC.search(label) or not set(keys) & {"close", "open", "high", "low", "prev_close"}:
            continue
        src = by_id.get(str(fact.get("source_id")), {})
        if classify_source(src) == "market_data":
            continue
        entity = entity_hint(label, source=src, metric_spans=spans)
        if entity and entity not in covered:
            missing[entity] = missing.get(entity, 0) + 1
    key_required = (any(e.get("reason") == "market_data_key_required" for e in events if isinstance(e, Mapping))
                    or "market_data_key_required" in gap_codes)
    tool_failed = any(f["reason"] in REASON_STATUS and REASON_STATUS[f["reason"]] == "tool_unavailable" for f in market["failures"])
    out = []
    known_symbols = {i.split(":")[-1].split("@")[0].upper() for i in (instruments_known or [])}
    for entity, count in sorted(missing.items()):
        if key_required:
            reason = "market_data_key_required"
            detail = "a personal market-data key is required"
        elif tool_failed:
            reason = "tool_unavailable"
            detail = "the market-data tool was unavailable"
        elif instruments_known is not None and entity not in known_symbols:
            reason = "instrument_not_in_tool_coverage"
            detail = f"the price tool covers only configured symbols ({', '.join(sorted(instruments_known)) or 'none'})"
        elif have_tool_info:
            reason = "not_requested"
            detail = "no exchange price series was requested for it"
        else:
            reason = "no_exchange_series"
            detail = "no exchange price series was obtained for it"
        out.append({"code": "no_market_data_for_entity", "category": "market_prices",
                    "params": {"entity": entity, "reason": reason, "quoted_facts": count,
                               "configured_instruments": sorted(instruments_known) if instruments_known is not None else None},
                    "message": f"No exchange price data for {entity}: {detail}; its prices in this run come from news or other non-exchange sources."})
    return out


# --------------------------------------------------------------------------------------------------
# Consumers
# --------------------------------------------------------------------------------------------------


def model_summary(coverage: Mapping[str, Any], *, max_statements: int = 8) -> dict[str, Any]:
    """Compact model-facing view: one line per attempted category plus the explicit "not established" list."""
    lines = []
    for cat in coverage.get("categories", []):
        if cat["status"] == "not_attempted":
            continue
        st = cat.get("staleness") or {}
        line: dict[str, Any] = {"category": cat["category"], "status": cat["status"], "obtained": cat["sources_obtained"]}
        if cat.get("data_as_of"):
            line["data_as_of"] = cat["data_as_of"]
        if st.get("trading_days_before_last_session") is not None and cat["category"] == "market_prices":
            line["trading_days_behind"] = st["trading_days_before_last_session"]
        if cat["failure_reasons"]:
            line["failures"] = cat["failure_reasons"]
        if cat["partial_signals"]:
            line["partial"] = cat["partial_signals"]
        lines.append(line)
    not_attempted = [c["category"] for c in coverage.get("categories", []) if c["status"] == "not_attempted"]
    return {"coverage": {"run_status": coverage.get("run", {}).get("status"), "categories": lines, "not_attempted": not_attempted,
                         "not_established": [s["message"] for s in coverage.get("not_established", [])[:max_statements]],
                         "note": "Generated deterministically from run events; state these limits instead of implying completeness."}}


def ui_view(coverage: Mapping[str, Any]) -> dict[str, Any]:
    """UI-facing structure: one row per data category with status badge, currency of data, attempts and failures."""
    rows = []
    for cat in coverage.get("categories", []):
        st = cat.get("staleness") or {}
        rows.append({
            "category": cat["category"], "label": cat["label"], "status": cat["status"], "status_label": cat["status_label"],
            "data_as_of": cat.get("data_as_of"), "data_as_of_kind": cat.get("data_as_of_kind"),
            "trading_days_behind": st.get("trading_days_before_last_session"), "days_behind_clock": st.get("days_before_clock_date"),
            "is_stale": st.get("is_stale"), "sources_obtained": cat["sources_obtained"], "attempts": cat["attempts"],
            "failures": cat["failures"], "documents": cat["documents"], "instruments": cat.get("instruments"),
            "not_established": [s["message"] for s in cat["not_established"]],
        })
    return {"version": VERSION, "run": coverage.get("run"), "research_clock": coverage.get("research_clock"), "rows": rows,
            "documents": coverage.get("documents", []), "status_counts": coverage.get("status_counts", {})}
