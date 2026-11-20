"""``news_search``: merge GLM MCP web search and GDELT into annotated, clustered news leads (plan W5).

Providers implement ``name``, ``supports_time_filter`` and ``search(query, *, since, until, languages, limit)``
returning a ``ToolResult`` whose ``data`` is a list of ``NewsLead``. Wrappers adapt the existing GLM and GDELT
clients. Leads are metadata only: ``acquirable`` says whether site terms permit automated retrieval of the body.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any, Protocol, Sequence

from ..fin_data.models import Coverage, Cost, Permissions, Provenance, SuggestedAction, ToolResult
from ..research_discovery import RedirectLead, unwrap_redirect_url
from ..research_source_terms import terms_decision
from .clustering import ClusterInput, canonical_news_url, cluster_items
from .gdelt import GDELT_ATTRIBUTION, GdeltClient, NewsLead

_CJK = re.compile(r"[㐀-鿿]")
_OFFICIAL_SUFFIXES = (".gov", ".gov.cn", ".gov.uk", ".gov.hk", ".go.jp", ".go.kr", ".europa.eu", ".int", ".mil")
_OFFICIAL_HOSTS = ("sec.gov", "federalreserve.gov", "ecb.europa.eu", "boj.or.jp", "pbc.gov.cn", "stats.gov.cn", "hkexnews.hk",
                   "bankofengland.co.uk", "imf.org", "worldbank.org", "oecd.org", "bis.org")


QUOTA_REASONS = frozenset({"search_quota_exhausted"})
RATE_REASONS = frozenset({"search_rate_limited", "search_provider_rate_limited", "gdelt_rate_limited"})
LIMIT_REASONS = QUOTA_REASONS | RATE_REASONS


def limited_failure_result(error: Any, prov: Provenance, cost: Cost) -> ToolResult:
    """Convert a search ``ProviderFailure`` into a ``ToolResult`` keeping its code, message and reset time."""
    code = str(getattr(error, "code", "search_provider_unavailable"))
    retryable = bool(getattr(error, "retryable", True))
    message = str(error)[:300] if hasattr(error, "code") else "Search provider failed."
    reset_at = getattr(error, "reset_at", None)
    if code in QUOTA_REASONS and isinstance(reset_at, str) and reset_at:
        message = f"{message} (quota resets at {reset_at})"[:400]
    status = "rate_limited" if code in RATE_REASONS else "failed"
    if code in QUOTA_REASONS:
        retryable = False
    return ToolResult.failure(status, code, message, retryable=retryable or status == "rate_limited", provenance=prov, cost=cost)


class NewsProvider(Protocol):
    name: str
    supports_time_filter: bool

    def search(self, query: str, *, since: datetime | date | None, until: datetime | date | None,
               languages: Sequence[str] | None, limit: int) -> ToolResult: ...


def classify_source_type(domain: str | None, default: str) -> str:
    host = (domain or "").lower()
    if host and (host.endswith(_OFFICIAL_SUFFIXES) or any(host == h or host.endswith("." + h) for h in _OFFICIAL_HOSTS)):
        return "official"
    return default


def guess_language(text: str) -> str | None:
    """Only script detection (CJK); returns None rather than guessing among Latin-script languages."""
    letters = [c for c in text if c.isalpha()]
    if letters and len(_CJK.findall(text)) / len(letters) >= 0.3:
        return "zh"
    return None


class GdeltNewsProvider:
    name = "gdelt"
    supports_time_filter = True   # on GDELT's first-seen time, not publication time

    def __init__(self, client: GdeltClient | None = None) -> None:
        self.client = client or GdeltClient()

    def search(self, query, *, since=None, until=None, languages=None, limit=50) -> ToolResult:
        return self.client.search(query, since=since, until=until, languages=languages, limit=limit)


class GlmNewsProvider:
    """Adapter over ``GlmCodingMcpSearchEvidenceProvider.search`` (no provider-side time filter)."""
    name = "glm-coding-mcp-search"
    supports_time_filter = False

    def __init__(self, provider: Any = None) -> None:
        if provider is None:
            from ..glm_mcp_search import GlmCodingMcpSearchEvidenceProvider
            provider = GlmCodingMcpSearchEvidenceProvider.from_environment()
        self.provider = provider

    def search(self, query, *, since=None, until=None, languages=None, limit=10) -> ToolResult:
        now = datetime.now(timezone.utc)
        prov = Provenance(provider=self.name, endpoint="glm-coding-mcp", retrieved_at=now)
        if getattr(self.provider, "supports_project_search", True) is False:
            return ToolResult.failure("denied", "search_provider_unconfigured", "GLM web search is not configured.", provenance=prov)
        try:
            candidates = self.provider.search(query, limit=max(1, min(int(limit), 10)))
        except Exception as error:   # ProviderFailure carries safe code/retryable; anything else is opaque
            return limited_failure_result(error, prov, Cost(provider_calls=1))
        leads: list[NewsLead] = []
        for c in candidates or ():
            published = getattr(c, "published_at", None)
            if isinstance(published, datetime):
                published_dt, precision = published, "datetime"
            elif isinstance(published, date):
                published_dt, precision = datetime(published.year, published.month, published.day, tzinfo=timezone.utc), "date"
            else:
                published_dt, precision = None, None
            title = getattr(c, "title", "") or ""
            leads.append(NewsLead(url=c.url, title=title, excerpt=getattr(c, "excerpt", None), publisher=getattr(c, "publisher", None),
                                  language=guess_language(title), published_at=published_dt, published_precision=precision,
                                  providers=[self.name]))
        if not leads:
            return ToolResult(status="empty", reason="no_results", message="Search returned no candidates.", provenance=prov, cost=Cost(provider_calls=1))
        return ToolResult(status="ok", reason="ok", coverage=Coverage(rows=len(leads)), provenance=prov, cost=Cost(provider_calls=1),
                          permissions=Permissions(store=True, display=True, model_input=True, export=False), data=leads)


def _utc(value: datetime | date | None, end: bool = False) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def default_providers() -> list[NewsProvider]:
    providers: list[NewsProvider] = []
    try:
        glm = GlmNewsProvider()
        if getattr(glm.provider, "supports_project_search", False):
            providers.append(glm)
    except Exception:
        pass
    providers.append(GdeltNewsProvider())
    return providers


def _merge(leads: list[NewsLead]) -> list[NewsLead]:
    merged: dict[str, NewsLead] = {}
    for lead in leads:
        key = canonical_news_url(lead.url)
        current = merged.get(key)
        if current is None:
            merged[key] = lead.model_copy(update={"canonical_url": key})
            continue
        update: dict[str, Any] = {"providers": sorted(set(current.providers) | set(lead.providers)),
                                  "attribution": sorted(set(current.attribution) | set(lead.attribution))}
        for field in ("published_at", "published_precision", "seen_at", "excerpt", "publisher", "domain", "language", "source_country"):
            if getattr(current, field) is None and getattr(lead, field) is not None:
                update[field] = getattr(lead, field)
        if current.source_type == "unknown" and lead.source_type != "unknown":
            update["source_type"] = lead.source_type
        merged[key] = current.model_copy(update=update)
    return list(merged.values())


def _annotate(lead: NewsLead, entities: Sequence[str]) -> NewsLead:
    decision = terms_decision(lead.url)
    redirect = False
    try:
        unwrap_redirect_url(lead.url)
    except RedirectLead:
        redirect = True
    except Exception:
        pass
    host = lead.domain
    if not host:
        m = re.match(r"https?://([^/:?#]+)", lead.url, re.I)
        host = m.group(1).lower() if m else None
    haystack = f"{lead.title} {lead.excerpt or ''}".lower()
    return lead.model_copy(update={
        "domain": host, "terms_status": decision.status, "terms_reason": decision.reason_code,
        "acquirable": decision.allowed and not redirect, "unresolved_redirect": redirect,
        "source_type": classify_source_type(host, lead.source_type),
        "matched_entities": [e for e in entities if e.lower() in haystack]})


def _in_window(lead: NewsLead, since: datetime | None, until: datetime | None) -> bool | None:
    """True/False for leads with a publication time; None when undated."""
    if lead.published_at is None:
        return None
    moment = _utc(lead.published_at)
    if lead.published_precision == "date":
        day = moment.date()
        return (since is None or day >= since.date()) and (until is None or day <= until.date())
    return (since is None or moment >= since) and (until is None or moment <= until)


def _lead_sort_key(lead: NewsLead) -> float:
    moment = _utc(lead.published_at) or _utc(lead.seen_at)
    return -(moment.timestamp() if moment else 0.0)


def news_search(query: str, *, since: datetime | date | None = None, until: datetime | date | None = None,
                languages: Sequence[str] | None = None, entities: Sequence[str] | None = None,
                source_types: Sequence[str] | None = None, limit: int = 20,
                providers: Sequence[NewsProvider] | None = None) -> ToolResult:
    """Search news across providers; returns clusters (``data`` items are ``NewsGroup`` dicts) with coverage notes."""
    now = datetime.now(timezone.utc)
    if not isinstance(query, str) or not query.strip():
        return ToolResult.failure("failed", "news_query_invalid", "Query is empty.")
    limit = max(1, min(int(limit), 100))
    entity_list = [e.strip() for e in entities or () if isinstance(e, str) and e.strip()]
    full_query = query.strip()
    extra = [e for e in entity_list if e.lower() not in full_query.lower()]
    if extra:
        full_query += " " + " ".join(f'"{e}"' if " " in e else e for e in extra)
    chosen = list(providers) if providers is not None else default_providers()
    if not chosen:
        return ToolResult.failure("failed", "no_news_provider", "No news provider is configured.")
    since_utc, until_utc = _utc(since), _utc(until)
    if until_utc is not None and not isinstance(until, datetime):
        until_utc = until_utc.replace(hour=23, minute=59, second=59)

    answered: list[str] = []
    failed: list[tuple[str, str, bool, str, str]] = []
    leads: list[NewsLead] = []
    calls = 0
    provider_filtered: list[str] = []
    for provider in chosen:
        try:
            result = provider.search(full_query, since=since, until=until, languages=languages, limit=min(250, limit * 2))
        except Exception:
            failed.append((provider.name, "provider_exception", True, "failed", ""))
            continue
        calls += result.cost.provider_calls or 1
        if result.status in ("ok", "partial", "empty"):
            answered.append(provider.name)
            if provider.supports_time_filter and (since is not None or until is not None):
                provider_filtered.append(provider.name)
            for lead in result.data:
                if isinstance(lead, NewsLead):
                    leads.append(lead.model_copy(update={"time_filter": "provider_seen_at" if provider.supports_time_filter and (since or until) and lead.published_at is None else lead.time_filter}))
        else:
            failed.append((provider.name, result.reason, result.retryable, result.status, result.message))

    notes: list[str] = []
    notes.append("Providers answered: " + (", ".join(answered) or "none") + ".")
    for name, reason, retryable, status, detail in failed:
        notes.append(f"Provider {name} {status} ({reason}{'; retryable' if retryable else ''})"
                     + (f": {detail[:300]}" if detail and reason in LIMIT_REASONS else "") + ".")
    gdelt_used = any(p.name == "gdelt" for p in chosen)
    if gdelt_used:
        notes.append(GDELT_ATTRIBUTION)

    prov = Provenance(provider="news_search", endpoint="news_search", retrieved_at=now, endpoints=[p.name for p in chosen])
    cost = Cost(provider_calls=calls)
    requested = {"start": since_utc.isoformat() if since_utc else None, "end": until_utc.isoformat() if until_utc else None}
    if not answered:
        reasons = {reason for _, reason, *_ in failed}
        if failed and reasons <= LIMIT_REASONS:
            # Every provider is quota/rate limited: say so (with each provider's reason, message and reset time).
            reason = "search_quota_exhausted" if reasons & QUOTA_REASONS else "search_rate_limited"
            retryable = any(r for _, _, r, _, _ in failed)
            status = "rate_limited" if retryable else "failed"
            detail = "; ".join(f"{name}: {why}" + (f" - {msg[:300]}" if msg else "") for name, why, _, _, msg in failed)
            return ToolResult.failure(status, reason, "Search is limited: " + detail, retryable=retryable,
                                      suggested_actions=[SuggestedAction(action="retry", retry_after_seconds=5.0)] if retryable else [],
                                      provenance=prov, cost=cost, coverage=Coverage(requested=requested, notes=notes))
        statuses = {s for _, _, _, s, _ in failed}
        status = "rate_limited" if statuses == {"rate_limited"} else "failed"
        return ToolResult.failure(status, "all_providers_failed", " ".join(notes), retryable=any(r for _, _, r, _, _ in failed),
                                  suggested_actions=[SuggestedAction(action="retry", retry_after_seconds=5.0)] if status == "rate_limited" else [],
                                  provenance=prov, cost=cost, coverage=Coverage(requested=requested, notes=notes))

    merged = _merge(leads)
    annotated = [_annotate(l, entity_list) for l in merged]

    kept: list[NewsLead] = []
    dropped = undated = 0
    type_dropped = lang_dropped = 0
    wanted_types = {t.lower() for t in source_types or ()}
    wanted_langs = {l.lower()[:2] for l in languages or ()}
    for lead in annotated:
        verdict = _in_window(lead, since_utc, until_utc) if (since_utc or until_utc) else True
        if verdict is None:
            undated += 1
        elif verdict is False:
            dropped += 1
            continue
        elif since_utc or until_utc:
            lead = lead.model_copy(update={"time_filter": "local_published_at"})
        if wanted_types and lead.source_type != "unknown" and lead.source_type not in wanted_types:
            type_dropped += 1
            continue
        if wanted_langs and lead.language and lead.language.lower()[:2] not in wanted_langs:
            lang_dropped += 1
            continue
        kept.append(lead)

    if since_utc or until_utc:
        notes.append(f"Time window: provider-side on first-seen time for {', '.join(provider_filtered) or 'no provider'}; "
                     f"local filter on provider-stated publication dates for the rest. Kept {len(kept)}, dropped {dropped} "
                     f"outside the window, {undated} have no known publication date (kept; published time unknown, not guessed).")
        local_only = [n for n in answered if n not in provider_filtered]
        if local_only:
            notes.append(f"Provider(s) {', '.join(local_only)} do not support time filtering; results were filtered locally.")
    if type_dropped:
        notes.append(f"{type_dropped} leads dropped by source_types filter.")
    if lang_dropped:
        notes.append(f"{lang_dropped} leads dropped by language filter (leads of unknown language are kept).")

    result = cluster_items([ClusterInput(id=str(i), url=l.url, title=l.title, text=l.excerpt if l.excerpt and len(l.excerpt) >= 200 else None,
                                         published_at=l.published_at, seen_at=l.seen_at, domain=l.domain) for i, l in enumerate(kept)])
    notes.extend(result.notes)
    groups: list[dict[str, Any]] = []
    for cluster in result.clusters:
        members = [kept[int(i)].model_copy(update={"cluster_id": cluster.cluster_id}) for i in cluster.member_ids]
        groups.append({"cluster_id": cluster.cluster_id, "independent_source_count": cluster.independent_source_count,
                       "independent_sources": list(cluster.independent_sources), "syndicated_from": list(cluster.syndicated_from),
                       "representative": members[0], "members": members})
    truncated = len(groups) > limit
    groups = groups[:limit]
    groups.sort(key=lambda g: _lead_sort_key(g["representative"]))
    notes.append(f"{sum(len(g['members']) for g in groups)} leads in {len(groups)} clusters.")
    attribution = [GDELT_ATTRIBUTION] if gdelt_used else []
    if not groups:
        return ToolResult(status="empty", reason="no_news_found", message=" ".join(notes), provenance=prov, cost=cost,
                          coverage=Coverage(requested=requested, rows=0, notes=notes))
    partial = bool(failed)
    return ToolResult(status="partial" if partial else "ok", reason="provider_failed" if partial else "ok",
                      message=" ".join(attribution), retryable=False, coverage=Coverage(requested=requested, rows=len(groups), truncated=truncated, notes=notes),
                      permissions=Permissions(store=True, display=True, model_input=True, export=False), provenance=prov, cost=cost, data=groups)
