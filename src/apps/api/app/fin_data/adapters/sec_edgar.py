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
from decimal import Decimal, InvalidOperation
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
    unit_kind: str        # money | per_share
    tags: dict[str, tuple[str, ...]]


def _spec(line, statement, flow, unit_kind, us, ifrs) -> LineSpec:
    return LineSpec(line, statement, flow, unit_kind, {"us-gaap": tuple(us), "ifrs-full": tuple(ifrs)})


STANDARD_LINES: dict[str, LineSpec] = {s.line: s for s in (
    _spec("revenue", "income", True, "money",
          ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
           "RevenueFromContractWithCustomerIncludingAssessedTax"], ["Revenue"]),
    _spec("operating_income", "income", True, "money", ["OperatingIncomeLoss"], ["ProfitLossFromOperatingActivities"]),
    _spec("net_income", "income", True, "money", ["NetIncomeLoss", "ProfitLoss"], ["ProfitLoss"]),
    _spec("eps_diluted", "income", True, "per_share", ["EarningsPerShareDiluted"], ["DilutedEarningsLossPerShare"]),
    _spec("total_assets", "balance", False, "money", ["Assets"], ["Assets"]),
    _spec("total_liabilities", "balance", False, "money", ["Liabilities"], ["Liabilities"]),
    _spec("equity", "balance", False, "money",
          ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"], ["Equity"]),
    _spec("operating_cash_flow", "cashflow", True, "money",
          ["NetCashProvidedByUsedInOperatingActivities",
           "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"], ["CashFlowsFromUsedInOperatingActivities"]),
    _spec("capex", "cashflow", True, "money",
          ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
          ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"]),
    _spec("cash", "balance", False, "money",
          ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
          ["CashAndCashEquivalents"]),
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
        periods.extend(_derive_q4(raw, tag, unit, {p.end for p in periods}))
    return sorted(periods, key=lambda p: p.end, reverse=True)


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
    ok = [u for u in names if (("/" in u or "per" in u.lower()) if unit_kind == "per_share" else
                               ("/" not in u and "share" not in u.lower() and "per" not in u.lower()))]
    if not ok:
        return None
    return "USD" if "USD" in ok else sorted(ok)[0]


def _currency(unit: str) -> str | None:
    head = unit.split("/")[0]
    return head if re.fullmatch(r"[A-Z]{3}", head) else None


def select_line(facts: dict, spec: LineSpec, kind: str, n: int, *, derive_q4: bool, taxonomy: str) -> list[Period]:
    """Per period choose the first tag in priority order that reported it (handles tag switches)."""
    chosen: dict[date, Period] = {}
    for tag in spec.tags.get(taxonomy, ()):
        node = facts.get(taxonomy, {}).get(tag)
        units = node.get("units") if isinstance(node, dict) else None
        if not isinstance(units, dict):
            continue
        unit = _pick_unit(units, spec.unit_kind)
        if unit is None:
            continue
        for p in _collect(units[unit], kind, flow=spec.flow, derive_q4=derive_q4 and spec.unit_kind == "money",
                          tag=tag, unit=unit):
            cur = chosen.get(p.end)
            # Same period under several tags: the latest filing wins; ties keep tag priority.
            if cur is None or (p.filed or date.min) > (cur.filed or date.min):
                chosen[p.end] = p
    return sorted(chosen.values(), key=lambda p: p.end, reverse=True)[:n]


# --------------------------------------------------------------------- adapter
class SecEdgarAdapter:
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
