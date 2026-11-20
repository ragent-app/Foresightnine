"""GDELT DOC 2.0 API client (article list mode) returning W6 ``ToolResult`` envelopes.

Licence: GDELT states its datasets may be used without fee for academic, commercial or governmental purposes,
provided any use or redistribution cites the GDELT Project with a link to https://www.gdeltproject.org/. The About
page does not name the DOC 2.0 API explicitly; it is treated as covered. Attribution is attached to every lead and
to the result coverage notes. GDELT returns article *metadata* only; bodies must be fetched through the normal fetch
path subject to each publisher's terms (research_source_terms).

``seendate`` is when GDELT first saw the article, not when it was published; it is stored as ``seen_at``.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field

from ..fin_data.models import Coverage, Permissions, Provenance, SuggestedAction, ToolResult, Cost

GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_ATTRIBUTION = "Source: The GDELT Project (https://www.gdeltproject.org/)"
MIN_INTERVAL_ENV = "FORESIGHTNINE_GDELT_MIN_INTERVAL_SECONDS"
DEFAULT_MIN_INTERVAL_SECONDS = 5.0
MAX_RECORDS = 250
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_TEXT = 1000

# ISO 639-1 -> GDELT ``sourcelang`` names (GDELT also accepts ISO 639-2 codes; names are the documented form).
_GDELT_LANGUAGES = {"en": "english", "zh": "chinese", "ja": "japanese", "ko": "korean", "de": "german",
                    "fr": "french", "es": "spanish", "pt": "portuguese", "ru": "russian", "ar": "arabic",
                    "it": "italian", "nl": "dutch", "tr": "turkish", "hi": "hindi", "id": "indonesian"}
_LANG_NAME_TO_ISO = {name: code for code, name in _GDELT_LANGUAGES.items()}
_LANG_NAME_TO_ISO.update({"chinese (simplified)": "zh", "chinese (traditional)": "zh", "mandarin": "zh"})


class NewsLead(BaseModel):
    """A news article lead: metadata only, never evidence until fetched and verified."""
    model_config = ConfigDict(extra="forbid")
    url: str
    canonical_url: str | None = None
    title: str
    domain: str | None = None
    language: str | None = None          # ISO 639-1 where known
    source_country: str | None = None
    excerpt: str | None = None
    publisher: str | None = None
    providers: list[str] = Field(default_factory=list)
    published_at: datetime | None = None   # only when a provider states a publication time
    published_precision: str | None = None  # "date" | "datetime"
    seen_at: datetime | None = None        # GDELT first-seen time (UTC); NOT the publication time
    source_type: str = "unknown"
    terms_status: str | None = None
    terms_reason: str | None = None
    acquirable: bool | None = None
    unresolved_redirect: bool = False
    matched_entities: list[str] = Field(default_factory=list)
    time_filter: str | None = None         # how the time window was applied to this lead
    cluster_id: str | None = None
    attribution: list[str] = Field(default_factory=list)


def configured_min_interval(environ: Mapping[str, str] | None = None) -> float:
    raw = (environ if environ is not None else os.environ).get(MIN_INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_MIN_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MIN_INTERVAL_SECONDS
    return value if 0 <= value <= 3600 else DEFAULT_MIN_INTERVAL_SECONDS


class RequestPacer:
    """Process-wide minimum spacing between GDELT requests."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, sleeper: Callable[[float], None] = time.sleep) -> None:
        self._clock, self._sleeper = clock, sleeper
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self, interval: float) -> float:
        with self._lock:
            now = self._clock()
            delay = max(0.0, self._next_allowed - now)
            if delay > 0:
                self._sleeper(delay)
            self._next_allowed = max(now, self._next_allowed) + delay + interval
            return delay

    def penalize(self, seconds: float) -> None:
        with self._lock:
            self._next_allowed = max(self._next_allowed, self._clock() + seconds)


_PROCESS_PACER = RequestPacer()


def parse_seendate(raw: Any) -> datetime | None:
    """GDELT ``YYYYMMDDTHHMMSSZ`` (UTC) -> aware datetime."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    try:
        if len(text) == 16 and text[8] == "T" and text[15] == "Z":
            return datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if len(text) == 14 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return None


def _gdelt_stamp(value: datetime | date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        moment = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return moment.strftime("%Y%m%d%H%M%S")


def _clean(value: Any, limit: int = _MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:limit] or None


def _or_group(parts: Sequence[str]) -> str:
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"


def build_query(query: str, *, languages: Sequence[str] | None = None, domains: Sequence[str] | None = None) -> str:
    """Append ``sourcelang:`` / ``domain:`` operators; unknown language codes are skipped (never invented)."""
    parts = [query.strip()]
    names = []
    for lang in languages or ():
        name = _GDELT_LANGUAGES.get(str(lang).strip().lower()[:2]) if len(str(lang).strip()) <= 5 else None
        if name is None and str(lang).strip().lower() in _LANG_NAME_TO_ISO:
            name = str(lang).strip().lower()
        if name and f"sourcelang:{name}" not in names:
            names.append(f"sourcelang:{name}")
    if names:
        parts.append(_or_group(names))
    doms = [f"domain:{d.strip().lower()}" for d in domains or () if isinstance(d, str) and d.strip() and " " not in d.strip()]
    if doms:
        parts.append(_or_group(doms))
    return " ".join(parts)


class GdeltClient:
    name = "gdelt"

    def __init__(self, *, transport: Callable[..., Any] | None = None, min_interval_seconds: float | None = None,
                 pacer: RequestPacer | None = None, timeout_seconds: float = 20.0,
                 now: Callable[[], datetime] | None = None) -> None:
        if transport is None:
            from ..openai_compatible import _default_binary_transport
            transport = _default_binary_transport
        self._transport = transport
        self._interval = configured_min_interval() if min_interval_seconds is None else max(0.0, float(min_interval_seconds))
        self._pacer = pacer or _PROCESS_PACER
        self._timeout = timeout_seconds
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _provenance(self, url: str, body: bytes | None) -> Provenance:
        return Provenance(provider="gdelt", endpoint=GDELT_ENDPOINT, retrieved_at=self._now(),
                          response_sha256=hashlib.sha256(body).hexdigest() if body else None, endpoints=[url])

    def search(self, query: str, *, since: datetime | date | None = None, until: datetime | date | None = None,
               languages: Sequence[str] | None = None, domains: Sequence[str] | None = None,
               limit: int = 50, sort: str = "DateDesc") -> ToolResult:
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            return ToolResult.failure("failed", "gdelt_query_invalid", "GDELT query is empty or too long.")
        limit = max(1, min(int(limit), MAX_RECORDS))
        params: dict[str, str] = {"query": build_query(query, languages=languages, domains=domains), "mode": "ArtList",
                                  "format": "json", "maxrecords": str(limit), "sort": sort}
        start, end = _gdelt_stamp(since), _gdelt_stamp(until)
        if start:
            params["startdatetime"] = start
        if end:
            params["enddatetime"] = end
        url = f"{GDELT_ENDPOINT}?{urlencode(params)}"
        requested = {"start": start, "end": end}
        self._pacer.wait(self._interval)
        try:
            response = self._transport("GET", url, {"Accept": "application/json", "User-Agent": "ForesightNine/1.0"},
                                       b"", self._timeout, max_response_bytes=_MAX_RESPONSE_BYTES)
        except Exception:  # transport errors are deliberately opaque
            return ToolResult.failure("failed", "gdelt_unavailable", "GDELT could not be reached.", retryable=True,
                                      provenance=self._provenance(url, None), cost=Cost(provider_calls=1))
        body: bytes = response.body if isinstance(response.body, bytes) else str(response.body).encode("utf-8")
        status = response.status_code
        prov = self._provenance(url, body)
        cost = Cost(provider_calls=1)
        text = body[:_MAX_RESPONSE_BYTES].decode("utf-8", errors="replace").strip()
        if status == 429 or (status < 300 and self._looks_rate_limited(text)):
            backoff = max(self._interval, 5.0) * 2
            retry_header = {str(k).lower(): v for k, v in (getattr(response, "headers", None) or {}).items()}.get("retry-after", "")
            if isinstance(retry_header, str) and retry_header.strip().isdigit():
                backoff = max(backoff, min(float(retry_header.strip()), 3600.0))
            self._pacer.penalize(backoff)
            return ToolResult.failure("rate_limited", "gdelt_rate_limited",
                                      f"GDELT answered HTTP 429 (too many requests); the service is reachable but asked for fewer requests. Retry after about {int(backoff)} seconds.",
                                      retryable=True, suggested_actions=[SuggestedAction(action="retry", retry_after_seconds=backoff)],
                                      provenance=prov, cost=cost)
        if status >= 500:
            return ToolResult.failure("failed", "gdelt_unavailable", f"GDELT returned HTTP {status}.", retryable=True, provenance=prov, cost=cost)
        if status >= 400:
            return ToolResult.failure("failed", "gdelt_rejected", f"GDELT rejected the request (HTTP {status}).", provenance=prov, cost=cost)
        if not text:
            return self._empty(requested, prov, cost)
        try:
            payload = json.loads(text, strict=False)
        except (ValueError, RecursionError):
            # Non-JSON plain text is GDELT's error channel (e.g. "The specified phrase is too short.").
            if not text.startswith(("{", "[")) and len(text) < 400:
                return ToolResult.failure("failed", "gdelt_query_rejected", "GDELT rejected the query: " + text[:200],
                                          provenance=prov, cost=cost)
            return ToolResult.failure("failed", "gdelt_malformed_response", "GDELT returned malformed JSON.",
                                      retryable=True, provenance=prov, cost=cost)
        articles = payload.get("articles") if isinstance(payload, dict) else None
        if articles is None and isinstance(payload, dict):
            return self._empty(requested, prov, cost)
        if not isinstance(articles, list):
            return ToolResult.failure("failed", "gdelt_malformed_response", "GDELT response has no article list.",
                                      retryable=True, provenance=prov, cost=cost)
        leads: list[NewsLead] = []
        skipped = 0
        for row in articles:
            lead = self._lead(row)
            if lead is None:
                skipped += 1
            else:
                leads.append(lead)
        if not leads:
            return self._empty(requested, prov, cost) if not skipped else ToolResult.failure(
                "failed", "gdelt_malformed_response", "No GDELT article had a usable url and title.", retryable=True, provenance=prov, cost=cost)
        seen = sorted(l.seen_at for l in leads if l.seen_at)
        notes = [GDELT_ATTRIBUTION, "seen_at is when GDELT saw the article, not its publication time.",
                 "GDELT supplies metadata only; article bodies must be fetched subject to each publisher's terms."]
        if skipped:
            notes.append(f"{skipped} malformed GDELT rows were skipped.")
        coverage = Coverage(requested=requested, rows=len(leads), notes=notes, truncated=len(articles) >= limit,
                            actual={"first": seen[0].isoformat() if seen else None, "last": seen[-1].isoformat() if seen else None})
        return ToolResult(status="partial" if skipped else "ok", reason="gdelt_partial_rows" if skipped else "ok",
                          message=GDELT_ATTRIBUTION, coverage=coverage,
                          permissions=Permissions(store=True, display=True, model_input=True, export=False),
                          provenance=prov, cost=cost, data=leads)

    @staticmethod
    def _looks_rate_limited(text: str) -> bool:
        lowered = text[:500].lower()
        return not lowered.startswith(("{", "[")) and ("limit requests" in lowered or "too many requests" in lowered
                                                         or "rate limit" in lowered)

    @staticmethod
    def _empty(requested: dict, prov: Provenance, cost: Cost) -> ToolResult:
        return ToolResult(status="empty", reason="gdelt_no_articles", message="GDELT returned no articles for this query and window. " + GDELT_ATTRIBUTION,
                          coverage=Coverage(requested=requested, rows=0, notes=[GDELT_ATTRIBUTION]), provenance=prov, cost=cost)

    @staticmethod
    def _lead(row: Any) -> NewsLead | None:
        if not isinstance(row, dict):
            return None
        url, title = _clean(row.get("url"), 2048), _clean(row.get("title"), 500)
        if not url or not title or not url.lower().startswith(("http://", "https://")):
            return None
        language = _clean(row.get("language"), 40)
        iso = _LANG_NAME_TO_ISO.get(language.lower()) if language else None
        return NewsLead(url=url, title=title, domain=(_clean(row.get("domain"), 255) or "").lower() or None,
                        language=iso or (language.lower() if language else None), source_country=_clean(row.get("sourcecountry"), 80),
                        seen_at=parse_seendate(row.get("seendate")), providers=["gdelt"], source_type="news",
                        attribution=[GDELT_ATTRIBUTION])
