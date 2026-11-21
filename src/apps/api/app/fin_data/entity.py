"""Entity resolution: SEC tickers (US issuers) plus Twelve Data symbol search (other markets).

Never picks for the caller: an ambiguous query returns every candidate with
``unique=False``. ``entity_id`` is ``EXCHANGE:SYMBOL`` when the exchange is known,
else ``cik:##########``; use ``parse_entity_id`` to map it back to provider symbols.
"""
from __future__ import annotations

import re
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict

from . import cn as cn_helpers
from .adapters.sec_edgar import SecEdgarAdapter
from .adapters.twelve_data import TwelveDataAdapter
from .models import Coverage, EntityRef, Permissions, SuggestedAction, ToolResult

MatchBasis = Literal["cik_exact", "ticker_exact", "symbol_exact", "name_exact", "name_prefix", "name_contains", "provider_search"]

MARKETS: dict[str, frozenset[str]] = {
    "US": frozenset({"NYSE", "NASDAQ"}), "CN": frozenset({"SSE", "SZSE", "BJSE"}), "HK": frozenset({"HKEX"}),
    "JP": frozenset({"TSE"}), "KR": frozenset({"KRX"}), "TW": frozenset({"TWSE"}), "GB": frozenset({"LSE"}),
    "IN": frozenset({"NSE"}), "CA": frozenset({"TSX"}), "AU": frozenset({"ASX"}), "DE": frozenset({"XETRA"}),
}


class EntityCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)
    entity: EntityRef
    match_basis: MatchBasis
    unique: bool = False
    sources: tuple[str, ...] = ()


def parse_entity_id(entity_id: str) -> dict[str, str | None]:
    if entity_id.startswith("cik:") and entity_id[4:].isdigit():
        return {"exchange": None, "symbol": None, "cik": entity_id[4:]}
    exchange, _, symbol = entity_id.partition(":")
    return {"exchange": exchange or None, "symbol": symbol or None, "cik": None}


def _basis(query: str, symbol: str | None, name: str | None, cik: str | None, from_search: bool) -> MatchBasis:
    q = query.strip().casefold()
    if cik and q.lstrip("0") == cik.lstrip("0") and q.isdigit():
        return "cik_exact"
    if symbol and q == symbol.casefold():
        return "ticker_exact" if not from_search else "symbol_exact"
    n = (name or "").casefold()
    if n == q:
        return "name_exact"
    if n.startswith(q):
        return "name_prefix"
    if q in n:
        return "name_contains"
    return "provider_search"


def wants_cn(query: str, market: str | None) -> bool:
    """A-share lookup is attempted for a CN code, Chinese text, or an explicit CN market (never for every query)."""
    q = query.strip()
    return (market or "").upper() == "CN" or cn_helpers.parse_cn_code(q) is not None or cn_helpers.contains_cjk(q)


def resolve_cn(query: str, listings: list[tuple[str, Callable[[], ToolResult]]]) -> ToolResult:
    """A-share candidates from the first provider listing that answers (Tushare, else BaoStock).

    A code resolves to one security (exchange inferred from the prefix when absent); names rank every match and are
    never auto-picked. If no listing is reachable a code still yields an unverified candidate."""
    parsed = cn_helpers.parse_cn_code(query)
    notes: list[str] = []
    answered: tuple[str, ToolResult] | None = None
    for name, load in listings:
        res = load()
        if res.status in ("ok", "empty"):
            answered = (name, res)
            break
        notes.append(f"{name} listing unavailable: {res.status}/{res.reason}.")
    if answered is None:
        if parsed is not None:
            ref = EntityRef(entity_id=parsed.entity_id, symbol=parsed.code, exchange=parsed.exchange,
                            currency=cn_helpers.CN_CURRENCY, asset_type="equity")
            cov = Coverage(rows=1, notes=notes + ["Code format and exchange inferred locally; not verified against a listing."])
            return ToolResult(status="partial", reason="source_gap", coverage=cov,
                              data=[EntityCandidate(entity=ref, match_basis="ticker_exact", sources=("cn_code_format",))])
        return ToolResult.failure("failed", "no_cn_listing_available", " ".join(notes) or "No A-share listing source is available.")
    name, res = answered
    refs = list(res.data)
    if parsed is not None:
        hit = [r for r in refs if r.entity_id == parsed.entity_id or (r.symbol == parsed.code and parsed.inferred)]
        matches = [(r, "ticker_exact") for r in hit]
        if parsed.inferred and not hit:
            notes.append("No listed security with this code; exchange was inferred from the code prefix.")
    else:
        matches = cn_helpers.match_listing(query, refs)
    cands = [EntityCandidate(entity=r, match_basis=b, sources=(name,)) for r, b in matches]
    cov = Coverage(rows=len(cands), notes=notes)
    if not cands:
        return ToolResult(status="empty", reason="no_match", coverage=cov, permissions=res.permissions, provenance=res.provenance)
    return ToolResult(status="ok", reason="ok", coverage=cov, permissions=res.permissions, provenance=res.provenance, data=cands)


def resolve_entity(query: str, market: str | None = None, asset_type: str | None = None, *,
                   sec: SecEdgarAdapter | None = None, twelve: TwelveDataAdapter | None = None,
                   twelve_kwargs: dict | None = None, cn: ToolResult | None = None) -> ToolResult:
    """Return candidates in ``data`` (list of ``EntityCandidate``); status ok with reason
    ``multiple_candidates`` when ambiguous. Provider failures degrade to notes when the other source answered."""
    q = query.strip()
    if not q:
        return ToolResult.failure("failed", "invalid_request", "query must not be blank.")
    market_key = market.strip().upper() if market else None
    exchanges = MARKETS.get(market_key) if market_key else None
    if market_key and exchanges is None:
        return ToolResult.failure("failed", "unknown_market", f"Unknown market {market!r}; use one of {', '.join(MARKETS)}.")
    want_type = asset_type.strip().lower() if asset_type else None
    notes: list[str] = []
    sec_res = td_res = None
    if sec is not None and market_key in (None, "US"):
        sec_res = sec.company_identity(q)
    if twelve is not None:
        td_res = twelve.symbol_search(q, **(twelve_kwargs or {}))
    for label, res in (("SEC", sec_res), ("Twelve Data", td_res), ("A-share listing", cn)):
        if res is not None and res.status not in ("ok", "empty") and not (res is cn and res.status == "partial"):
            notes.append(f"{label} unavailable: {res.status}/{res.reason}.")
    if cn is not None and cn.coverage is not None:
        notes.extend(cn.coverage.notes)

    merged: dict[str, dict] = {}
    td_entities = [e for e in (td_res.data if td_res and td_res.status == "ok" else [])
                   if (exchanges is None or e.exchange in exchanges) and (want_type is None or e.asset_type == want_type)]
    for e in td_entities:
        merged[e.entity_id] = {"entity": e, "basis": _basis(q, e.symbol, e.company_name, None, True), "sources": ["twelve_data"]}
    for c in (cn.data if cn is not None and cn.status in ("ok", "partial") else []):
        e = c.entity
        if (exchanges is not None and e.exchange not in exchanges) or want_type not in (None, e.asset_type):
            continue
        item = merged.get(e.entity_id)
        if item is None:
            merged[e.entity_id] = {"entity": e, "basis": c.match_basis, "sources": list(c.sources)}
        else:  # Twelve Data already knows this security: enrich with the listing's names and identifiers
            ids = {**item["entity"].identifiers, **e.identifiers}
            item["entity"] = item["entity"].model_copy(update={"identifiers": ids, "company_name": item["entity"].company_name or e.company_name,
                                                               "listing_date": item["entity"].listing_date or e.listing_date})
            item["sources"].extend(c.sources)
            item["basis"] = c.match_basis if c.match_basis == "ticker_exact" else item["basis"]
    for e in (sec_res.data if sec_res and sec_res.status == "ok" else []):
        if want_type not in (None, "equity"):
            continue
        cik = e.identifiers.get("cik")
        basis = _basis(q, e.symbol, e.company_name, cik, False)
        us_match = next((k for k, v in merged.items() if v["entity"].symbol and e.symbol
                         and v["entity"].symbol.upper() == e.symbol.upper() and v["entity"].exchange in MARKETS["US"]), None)
        if us_match:  # same US security seen by both sources: enrich, do not duplicate
            item = merged[us_match]
            ids = {**item["entity"].identifiers, **e.identifiers}
            item["entity"] = item["entity"].model_copy(update={"identifiers": ids})
            item["sources"].append("sec_edgar")
            item["basis"] = basis if basis in ("cik_exact", "ticker_exact") else item["basis"]
        else:
            merged[e.entity_id] = {"entity": e, "basis": basis, "sources": ["sec_edgar"]}
    unique = len(merged) == 1
    cands = [EntityCandidate(entity=v["entity"], match_basis=v["basis"], unique=unique, sources=tuple(v["sources"]))
             for v in merged.values()]
    order = {"cik_exact": 0, "ticker_exact": 0, "symbol_exact": 1, "name_exact": 1, "name_prefix": 2, "name_contains": 3,
             "provider_search": 4}
    cands.sort(key=lambda c: (order[c.match_basis], c.entity.entity_id))
    oks = [r for r in (sec_res, td_res, cn) if r is not None and r.status in ("ok", "empty", "partial")]
    perms = oks[0].permissions if oks else Permissions.none()
    for r in oks[1:]:
        perms = perms.narrow(r.permissions)
    prov = next((r.provenance for r in (sec_res, td_res, cn) if r is not None and r.status == "ok"), None)
    cov = Coverage(rows=len(cands), notes=notes + ["Candidates are never auto-selected; pass an entity_id explicitly."])
    if not cands:
        if not (sec_res or td_res or cn) or all(r is None or r.status not in ("ok", "empty", "partial") for r in (sec_res, td_res, cn)):
            return ToolResult.failure("failed", "no_source_available", "No identity source answered. " + " ".join(notes), retryable=True)
        return ToolResult(status="empty", reason="no_match", coverage=cov, permissions=perms, provenance=prov)
    ambiguous = len(cands) > 1
    status = "partial" if notes else "ok"
    return ToolResult(status=status, reason="multiple_candidates" if ambiguous else ("source_gap" if notes else "ok"),
                      message="Several securities match; choose one by entity_id." if ambiguous else "",
                      suggested_actions=[SuggestedAction(action="resolve_entity", hint="add market or asset_type")] if ambiguous else [],
                      coverage=cov, permissions=perms, provenance=prov, data=cands)
