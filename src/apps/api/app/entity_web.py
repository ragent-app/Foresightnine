"""Company web identity from Wikidata (free, CC0): official website, listing identifiers and multilingual labels.

Used by ``issuer_documents`` to find an issuer's own website without paid or terms-blocked sources, and usable later to
enrich entity resolution. Only the public Wikidata API and SPARQL endpoint are used, with the descriptive User-Agent
Wikimedia asks for (generic, no personal data), a process-wide minimum request interval, ``maxlag`` and a 24 hour cache.

Wikidata properties: P856 official website, P414 stock exchange, P249 ticker symbol (qualifier of P414).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlencode, urlsplit

from .news.gdelt import RequestPacer

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
CONTACT_ENV = "FORESIGHTNINE_WIKIMEDIA_CONTACT"
MIN_INTERVAL_ENV = "FORESIGHTNINE_WIKIDATA_MIN_INTERVAL_SECONDS"
DEFAULT_MIN_INTERVAL_SECONDS = 1.0
CACHE_TTL_SECONDS = 24 * 3600
FAILURE_TTL_SECONDS = 300
MAX_RESPONSE_BYTES = 1_000_000
MAX_SEARCH_HITS = 6
LABEL_LANGUAGES = ("en", "zh", "zh-hans", "zh-hant", "zh-hk", "zh-cn", "zh-tw")
ATTRIBUTION = "Company identity: Wikidata (CC0, https://www.wikidata.org/)"
_QID = re.compile(r"^Q[1-9]\d{0,12}$")
_CJK = re.compile(r"[㐀-鿿]")

# Wikidata exchange labels (English) -> research_clock exchange keys. Matching is by keyword on the label.
_EXCHANGE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("hong kong", "HKEX"), ("shanghai", "SSE"), ("shenzhen", "SZSE"), ("beijing stock", "BJSE"),
    ("nasdaq", "NASDAQ"), ("new york stock", "NYSE"), ("nyse", "NYSE"), ("tokyo", "TSE"), ("japan exchange", "TSE"),
    ("korea", "KRX"), ("taiwan", "TWSE"), ("london stock", "LSE"), ("toronto", "TSX"), ("national stock exchange of india", "NSE"),
    ("australian securities", "ASX"), ("frankfurt", "XETRA"), ("xetra", "XETRA"), ("euronext", "EURONEXT"),
)
EXCHANGE_MARKET = {"HKEX": "HK", "SSE": "CN", "SZSE": "CN", "BJSE": "CN", "NASDAQ": "US", "NYSE": "US", "TSE": "JP",
                   "KRX": "KR", "TWSE": "TW", "LSE": "GB", "TSX": "CA", "NSE": "IN", "ASX": "AU", "XETRA": "DE"}


def exchange_code_from_label(label: str | None) -> str | None:
    text = (label or "").lower()
    return next((code for keyword, code in _EXCHANGE_KEYWORDS if keyword in text), None)


def normalize_ticker(raw: str | None) -> str:
    """Comparable ticker: digits lose leading zeros ('0700' == '00700' == '700'); others upper-case without suffix."""
    text = (raw or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        head, _, tail = text.rpartition(".")
        if head and tail.isalpha() and len(tail) <= 3:
            text = head
    return text.lstrip("0") or "0" if text.isdigit() else text


def ticker_variants(raw: str) -> list[str]:
    base = normalize_ticker(raw)
    if not base:
        return []
    if base.isdigit():
        return list(dict.fromkeys([base, base.zfill(4), base.zfill(5), base.zfill(6)]))
    return [base]


@dataclass(frozen=True)
class Listing:
    exchange_qid: str | None
    exchange: str | None          # research_clock exchange key when recognised
    exchange_label: str | None
    tickers: tuple[str, ...]


@dataclass(frozen=True)
class IssuerProfile:
    qid: str
    label_en: str | None
    label_zh: str | None
    description: str | None
    websites: tuple[str, ...]      # preferred first; ended or deprecated statements excluded
    listings: tuple[Listing, ...]
    labels: tuple[str, ...] = ()

    def score(self, *, name: str | None, ticker: str | None, market: str | None) -> int:
        points = 0
        wanted = normalize_ticker(ticker)
        listed_markets = {EXCHANGE_MARKET.get(item.exchange or "") for item in self.listings}
        if wanted and any(normalize_ticker(t) == wanted for item in self.listings for t in item.tickers
                          if market is None or item.exchange is None or EXCHANGE_MARKET.get(item.exchange) == market):
            points += 10
        if market and market in listed_markets:
            points += 4
        if name and name.strip().lower() in {label.lower() for label in self.labels}:
            points += 3
        if self.listings:
            points += 1
        if self.websites:
            points += 2
        return points

    def to_wire(self) -> dict[str, Any]:
        return {"wikidata_id": self.qid, "name_en": self.label_en, "name_zh": self.label_zh,
                "description": self.description, "websites": list(self.websites[:3]),
                "listings": [{"exchange": item.exchange or item.exchange_label, "tickers": list(item.tickers)}
                             for item in self.listings[:6]]}


@dataclass
class WikidataResult:
    status: str                         # ok | empty | failed | rate_limited
    profiles: list[IssuerProfile] = field(default_factory=list)
    reason: str | None = None
    message: str = ""
    calls: int = 0


_CACHE: dict[tuple, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 256
_PROCESS_PACER = RequestPacer()


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _cache_get(key: tuple, now: float) -> Any | None:
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is None or hit[0] <= now:
            _CACHE.pop(key, None)
            return None
        return hit[1]


def _cache_put(key: tuple, value: Any, now: float, ttl: float) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            for stale in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:_CACHE_MAX // 4]:
                _CACHE.pop(stale, None)
        _CACHE[key] = (now + ttl, value)


def user_agent() -> str:
    contact = os.environ.get(CONTACT_ENV, "").strip()
    base = "ForesightNine-IssuerLookup/1.0 (financial research tool; low volume; reads company identity from Wikidata"
    return base + (f"; contact {contact[:120]}" if contact else "") + ")"


def _configured_interval() -> float:
    try:
        value = float(os.environ.get(MIN_INTERVAL_ENV, ""))
    except ValueError:
        return DEFAULT_MIN_INTERVAL_SECONDS
    return value if 0 <= value <= 60 else DEFAULT_MIN_INTERVAL_SECONDS


class _Failure(Exception):
    def __init__(self, status: str, reason: str, message: str) -> None:
        super().__init__(message)
        self.status, self.reason, self.message = status, reason, message


class WikidataClient:
    def __init__(self, *, transport: Callable[..., Any] | None = None, min_interval_seconds: float | None = None,
                 pacer: RequestPacer | None = None, timeout_seconds: float = 10.0,
                 clock: Callable[[], float] = time.time) -> None:
        if transport is None:
            from .openai_compatible import _default_binary_transport
            transport = _default_binary_transport
        self._transport = transport
        self._interval = _configured_interval() if min_interval_seconds is None else max(0.0, float(min_interval_seconds))
        self._pacer = pacer or _PROCESS_PACER
        self._timeout = timeout_seconds
        self._clock = clock
        self.calls = 0

    # -- transport
    def _get_json(self, url: str) -> Any:
        key = ("GET", url)
        cached = _cache_get(key, self._clock())
        if cached is not None:
            if isinstance(cached, _Failure):
                raise cached
            return cached
        self._pacer.wait(self._interval)
        self.calls += 1
        try:
            response = self._transport("GET", url, {"Accept": "application/json", "User-Agent": user_agent()}, b"",
                                       self._timeout, max_response_bytes=MAX_RESPONSE_BYTES)
        except Exception as error:
            timed_out = type(error).__name__ == "_TransportTimeout" or isinstance(error, TimeoutError)
            failure = _Failure("failed", "wikidata_timeout" if timed_out else "wikidata_unavailable",
                               "Wikidata did not answer in time." if timed_out else "Wikidata could not be reached.")
            _cache_put(key, failure, self._clock(), FAILURE_TTL_SECONDS)
            raise failure from None
        status = getattr(response, "status_code", 0)
        if status == 429:
            retry = {str(k).lower(): v for k, v in (getattr(response, "headers", None) or {}).items()}.get("retry-after", "")
            if isinstance(retry, str) and retry.strip().isdigit():
                self._pacer.penalize(min(float(retry.strip()), 300.0))
            failure = _Failure("rate_limited", "wikidata_rate_limited", "Wikidata asked for fewer requests (HTTP 429).")
            _cache_put(key, failure, self._clock(), FAILURE_TTL_SECONDS)
            raise failure
        if status >= 400:
            failure = _Failure("failed", "wikidata_unavailable", f"Wikidata returned HTTP {status}.")
            _cache_put(key, failure, self._clock(), FAILURE_TTL_SECONDS)
            raise failure
        body = response.body if isinstance(response.body, bytes) else str(response.body).encode("utf-8")
        try:
            data = json.loads(body[:MAX_RESPONSE_BYTES].decode("utf-8", errors="replace"))
        except (ValueError, RecursionError):
            raise _Failure("failed", "wikidata_malformed_response", "Wikidata returned malformed JSON.") from None
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            code = str(data["error"].get("code") or "")
            if code == "maxlag":
                raise _Failure("rate_limited", "wikidata_rate_limited", "Wikidata is lagging; retry later.")
            raise _Failure("failed", "wikidata_rejected", f"Wikidata rejected the request ({code[:60]}).")
        _cache_put(key, data, self._clock(), CACHE_TTL_SECONDS)
        return data

    @staticmethod
    def _api(**params: str) -> str:
        return f"{WIKIDATA_API}?{urlencode({**params, 'format': 'json', 'maxlag': '5'})}"

    # -- queries
    def _search(self, text: str, language: str) -> list[str]:
        data = self._get_json(self._api(action="wbsearchentities", search=text[:100], language=language,
                                        uselang=language, type="item", limit=str(MAX_SEARCH_HITS)))
        rows = data.get("search") if isinstance(data, dict) else None
        return [row["id"] for row in rows or [] if isinstance(row, dict) and isinstance(row.get("id"), str) and _QID.match(row["id"])]

    def _sparql_ticker(self, ticker: str) -> list[str]:
        variants = ticker_variants(ticker)
        if not variants:
            return []
        values = " ".join(json.dumps(v) for v in variants)
        query = ("SELECT DISTINCT ?item WHERE { VALUES ?t { " + values + " } ?item p:P414 ?s . ?s pq:P249 ?t . } LIMIT 8")
        data = self._get_json(f"{WIKIDATA_SPARQL}?{urlencode({'query': query, 'format': 'json'})}")
        rows = (((data or {}).get("results") or {}).get("bindings") or []) if isinstance(data, dict) else []
        found = []
        for row in rows:
            uri = ((row or {}).get("item") or {}).get("value") if isinstance(row, dict) else None
            qid = uri.rsplit("/", 1)[-1] if isinstance(uri, str) else ""
            if _QID.match(qid):
                found.append(qid)
        return found

    def _entities(self, ids: Sequence[str], props: str) -> dict[str, Any]:
        unique = [i for i in dict.fromkeys(ids) if _QID.match(i)][:40]
        if not unique:
            return {}
        data = self._get_json(self._api(action="wbgetentities", ids="|".join(unique), props=props,
                                        languages="|".join(LABEL_LANGUAGES)))
        entities = data.get("entities") if isinstance(data, dict) else None
        return entities if isinstance(entities, dict) else {}

    def lookup(self, *, name: str | None = None, ticker: str | None = None, market: str | None = None) -> WikidataResult:
        """Candidate issuer profiles for a company name and/or ticker, best match first. Never raises."""
        start_calls = self.calls
        try:
            ids: list[str] = []
            if name and name.strip():
                text = name.strip()
                languages = ["zh", "en"] if _CJK.search(text) else ["en", "zh"]
                for language in languages:
                    ids.extend(self._search(text, language))
                    if ids:
                        break
            if ticker and ticker.strip() and not ids:
                ids.extend(self._sparql_ticker(ticker))
            elif ticker and ticker.strip() and ids:
                # Also pull entities that carry exactly this ticker so a name match is confirmed or outranked.
                try:
                    ids.extend(self._sparql_ticker(ticker))
                except _Failure:
                    pass
            ids = list(dict.fromkeys(ids))
            if not ids:
                return WikidataResult("empty", reason="wikidata_no_match", message="Wikidata has no entity for this name or ticker.",
                                      calls=self.calls - start_calls)
            entities = self._entities(ids, "labels|descriptions|claims")
            exchange_ids = []
            for entity in entities.values():
                for statement in _claims(entity, "P414"):
                    qid = _item_id(statement)
                    if qid:
                        exchange_ids.append(qid)
            exchange_entities = self._entities(exchange_ids, "labels") if exchange_ids else {}
            profiles = [p for p in (_profile(qid, entities.get(qid), exchange_entities) for qid in ids) if p is not None]
        except _Failure as failure:
            return WikidataResult(failure.status, reason=failure.reason, message=failure.message, calls=self.calls - start_calls)
        profiles.sort(key=lambda p: -p.score(name=name, ticker=ticker, market=market))
        return WikidataResult("ok" if profiles else "empty", profiles=profiles,
                              reason=None if profiles else "wikidata_no_match", calls=self.calls - start_calls)


def _claims(entity: Any, prop: str) -> list[Mapping[str, Any]]:
    claims = (entity or {}).get("claims") if isinstance(entity, Mapping) else None
    rows = claims.get(prop) if isinstance(claims, Mapping) else None
    return [row for row in rows or [] if isinstance(row, Mapping) and row.get("rank") != "deprecated"]


def _snak_value(snak: Any) -> Any:
    if not isinstance(snak, Mapping) or snak.get("snaktype") != "value":
        return None
    return (snak.get("datavalue") or {}).get("value") if isinstance(snak.get("datavalue"), Mapping) else None


def _item_id(statement: Mapping[str, Any]) -> str | None:
    value = _snak_value(statement.get("mainsnak"))
    qid = value.get("id") if isinstance(value, Mapping) else None
    return qid if isinstance(qid, str) and _QID.match(qid) else None


def _ended(statement: Mapping[str, Any]) -> bool:
    qualifiers = statement.get("qualifiers")
    return isinstance(qualifiers, Mapping) and bool(qualifiers.get("P582"))   # P582 end time


def _label(entity: Mapping[str, Any], *languages: str) -> str | None:
    labels = entity.get("labels") if isinstance(entity.get("labels"), Mapping) else {}
    for language in languages:
        row = labels.get(language)
        if isinstance(row, Mapping) and isinstance(row.get("value"), str) and row["value"].strip():
            return row["value"].strip()[:200]
    return None


def _profile(qid: str, entity: Any, exchange_entities: Mapping[str, Any]) -> IssuerProfile | None:
    if not isinstance(entity, Mapping) or "missing" in entity:
        return None
    sites: list[tuple[int, str]] = []
    for statement in _claims(entity, "P856"):
        value = _snak_value(statement.get("mainsnak"))
        if isinstance(value, str) and value.lower().startswith("https://") and not _ended(statement) and len(value) <= 300:
            if urlsplit(value).hostname:
                sites.append((0 if statement.get("rank") == "preferred" else 1, value))
        elif isinstance(value, str) and value.lower().startswith("http://") and not _ended(statement) and len(value) <= 300:
            if urlsplit(value).hostname:
                sites.append((2, "https://" + value[7:]))   # issuer sites are fetched over HTTPS only
    sites.sort(key=lambda item: item[0])
    listings: list[Listing] = []
    for statement in _claims(entity, "P414"):
        if _ended(statement):
            continue
        exchange_qid = _item_id(statement)
        qualifiers = statement.get("qualifiers") if isinstance(statement.get("qualifiers"), Mapping) else {}
        tickers = tuple(dict.fromkeys(str(v).strip().upper() for q in qualifiers.get("P249", []) or []
                                      if isinstance(v := _snak_value(q), str) and v.strip()))
        exchange_entity = exchange_entities.get(exchange_qid) if exchange_qid else None
        label = _label(exchange_entity, "en") if isinstance(exchange_entity, Mapping) else None
        listings.append(Listing(exchange_qid, exchange_code_from_label(label), label, tickers))
    descriptions = entity.get("descriptions") if isinstance(entity.get("descriptions"), Mapping) else {}
    description = next((d["value"][:200] for d in (descriptions.get("en"), descriptions.get("zh")) if isinstance(d, Mapping)
                        and isinstance(d.get("value"), str)), None)
    all_labels = tuple(dict.fromkeys(row["value"] for row in (entity.get("labels") or {}).values()
                                     if isinstance(row, Mapping) and isinstance(row.get("value"), str)))
    return IssuerProfile(qid, _label(entity, "en"), _label(entity, "zh", "zh-hans", "zh-cn", "zh-hant", "zh-hk", "zh-tw"),
                         description, tuple(dict.fromkeys(url for _, url in sites)), tuple(listings), all_labels)
