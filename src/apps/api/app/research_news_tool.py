"""Run wiring of the ``news_search`` tool (plan W5): provider adapters, request model and lead conversion.

``app.news.search.news_search`` merges providers (the configured GLM web search and GDELT), annotates terms and
clusters syndicated copies.  This module adapts the run's existing ``discover(query)`` callable to that provider
protocol (keeping the old lead normalisation: redirect unwrapping, FRED exclusion, invalid-lead accounting),
converts the clustered result into the run's ``state['leads']`` rows and a bounded model-facing result, and collects
the attributions that must travel with the result (GDELT).  No network access happens here except through the
providers handed in.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .fin_data.models import Cost, Permissions, Provenance, ToolResult
from .news.gdelt import GDELT_ATTRIBUTION, GdeltClient, NewsLead
from .news.search import GdeltNewsProvider, LIMIT_REASONS, NewsProvider, guess_language, limited_failure_result, news_search
from .phase3 import ProviderFailure
from .research_discovery import RedirectLead, normalize_search_lead, search_lead_url
from .research_source_catalog import canonical_source_url
from .research_source_policy import is_fred_source

GDELT_URL = 'https://www.gdeltproject.org/'
GDELT_ENABLED_ENV = 'FORESIGHTNINE_GDELT_ENABLED'
MAX_NEWS_LEADS = 30
DEFAULT_NEWS_LEADS = 10
MEMBERS_SHOWN_PER_CLUSTER = 3
MAX_NOTES = 6
GLM_PROVIDER_NAME = 'glm-coding-mcp-search'
ATTRIBUTIONS = {'gdelt': {'id': 'gdelt', 'text': GDELT_ATTRIBUTION, 'url': GDELT_URL}}
_BENIGN_SEARCH_FAILURES = frozenset({'search_query_invalid', 'search_provider_unconfigured', 'search_query_provider_limit',
                                     'search_response_invalid', 'search_provider_error', *LIMIT_REASONS})


class NewsSearchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=500, description=(
        'What to look for. Returns unverified news and web leads (clustered by story), not evidence; acquire a lead '
        'with acquire_sources and read it before citing.'))
    since: date | None = Field(default=None, description='Optional earliest publication date (YYYY-MM-DD).')
    until: date | None = Field(default=None, description='Optional latest publication date (YYYY-MM-DD).')
    languages: list[str] | None = Field(default=None, max_length=4, description=(
        'Optional language codes such as ["en","zh"]; query both to cross-check across languages.'))
    limit: int = Field(default=DEFAULT_NEWS_LEADS, ge=1, le=MAX_NEWS_LEADS, description=f'Maximum number of story clusters (1-{MAX_NEWS_LEADS}).')
    rationale: str = Field(min_length=1)

    @model_validator(mode='after')
    def ordered_window(self) -> 'NewsSearchRequest':
        if self.since is not None and self.until is not None and self.until < self.since:
            raise ValueError('until must be on or after since.')
        if self.languages is not None and any((not isinstance(x, str)) or not 2 <= len(x.strip()) <= 12 for x in self.languages):
            raise ValueError('languages must be short language codes such as "en" or "zh".')
        return self


def gdelt_enabled(environ: Mapping[str, str] | None = None) -> bool:
    raw = (environ if environ is not None else os.environ).get(GDELT_ENABLED_ENV, '1').strip().lower()
    return raw not in ('0', 'false', 'no', 'off', '')


def default_news_providers(discover: Callable[[str], Any] | None, *, gdelt: NewsProvider | None = None,
                           environ: Mapping[str, str] | None = None) -> list[NewsProvider]:
    """GLM web search (the run's ``discover``) plus GDELT unless disabled by ``FORESIGHTNINE_GDELT_ENABLED=0``."""
    providers: list[NewsProvider] = []
    if discover is not None:
        providers.append(DiscoverCallableProvider(discover))
    if gdelt is not None:
        providers.append(gdelt)
    elif discover is not None and gdelt_enabled(environ):
        providers.append(GdeltNewsProvider(GdeltClient()))
    return providers


class DiscoverCallableProvider:
    """Adapt ``discover(query) -> candidates`` to the ``NewsProvider`` protocol.

    Candidate normalisation is the one the run always used (redirect links unwrapped or counted as unresolved,
    FRED excluded, malformed candidates counted).  The provider keeps the last call's accounting on itself so the
    runtime can report it and apply the unknown-outcome rule for search failures.
    """
    supports_time_filter = False

    def __init__(self, discover: Callable[[str], Any], name: str = GLM_PROVIDER_NAME) -> None:
        self.discover, self.name = discover, name
        self.reset()

    def reset(self) -> None:
        self.failure: ProviderFailure | None = None
        self.invalid_response = False
        self.found_count = 0
        self.denied: dict[str, int] = {'source_not_authorized': 0, 'invalid_lead': 0, 'redirect_link': 0}
        self.unresolved_titles: list[str] = []

    def search(self, query: str, *, since=None, until=None, languages=None, limit: int = 10) -> ToolResult:
        self.reset()
        prov = Provenance(provider=self.name, endpoint='run-search', retrieved_at=datetime.now(timezone.utc))
        cost = Cost(provider_calls=1)
        try:
            found = self.discover(query)
        except ProviderFailure as error:
            self.failure = error
            return limited_failure_result(error, prov, cost)
        if not isinstance(found, (list, tuple)):
            found = [None]
        self.found_count = len(found)
        leads: list[NewsLead] = []
        for raw in found:
            try:
                url = search_lead_url(raw)
                if url and is_fred_source(url):
                    self.denied['source_not_authorized'] += 1
                    continue
                candidate = normalize_search_lead(raw)
            except RedirectLead:
                self.denied['redirect_link'] += 1
                title = raw.get('title') if isinstance(raw, dict) else getattr(raw, 'title', None)
                if isinstance(title, str) and title.strip() and len(self.unresolved_titles) < 5:
                    self.unresolved_titles.append(title.strip()[:160])
                continue
            except (ValueError, ProviderFailure):
                self.denied['invalid_lead'] += 1
                continue
            leads.append(NewsLead(url=candidate.url, title=candidate.title, excerpt=candidate.excerpt, publisher=candidate.publisher,
                                  language=guess_language(candidate.title), published_at=candidate.published_at,
                                  published_precision='datetime' if candidate.published_at else None, providers=[self.name]))
        if found and self.denied['invalid_lead'] == len(found):
            self.invalid_response = True
            return ToolResult.failure('failed', 'search_response_invalid', 'Search returned results in an invalid format.',
                                      provenance=prov, cost=cost)
        if not leads:
            return ToolResult(status='empty', reason='no_results', message='Search returned no usable candidates.', provenance=prov, cost=cost)
        return ToolResult(status='ok', reason='ok', provenance=prov, cost=cost,
                          permissions=Permissions(store=True, display=True, model_input=True, export=False), data=leads)


@dataclass
class NewsOutcome:
    result: dict[str, Any]
    summary: dict[str, Any] | None = None
    lead_rows: list[dict[str, Any]] = field(default_factory=list)
    attributions: list[dict[str, str]] = field(default_factory=list)
    denied_count: int | None = None
    denied_reasons: dict[str, int] | None = None
    unknown_failure: ProviderFailure | None = None   # a search call whose outcome is unknown (never retried)
    invalid_response: bool = False


def _clip(value: Any, limit: int) -> str:
    return str(value)[:limit]


def _host(url: str) -> str:
    m = re.match(r'https?://([^/:?#]+)', url or '', re.I)
    return m.group(1).lower() if m else ''


def _lead_row(lead: NewsLead, url: str) -> dict[str, Any]:
    row: dict[str, Any] = {'url': url, 'title': lead.title[:500],
                           'published_at': lead.published_at.isoformat() if lead.published_at else None}
    if lead.seen_at:
        row['seen_at'] = lead.seen_at.isoformat()
    for key, value in (('domain', lead.domain), ('language', lead.language), ('cluster_id', lead.cluster_id),
                       ('source_type', lead.source_type if lead.source_type != 'unknown' else None),
                       ('providers', list(lead.providers) or None)):
        if value:
            row[key] = value
    if lead.acquirable is False:
        row.update(acquirable=False, terms_status=lead.terms_status)
    return row


def run_news_search(req: NewsSearchRequest, *, providers: Sequence[NewsProvider], as_of: datetime | None = None,
                    blocked_message: str | None = None, redirect_message: str | None = None) -> NewsOutcome:
    """Run one ``news_search`` call and convert it for the runtime.  Never raises on provider errors."""
    until = req.until
    notes: list[str] = []
    if as_of is not None and (until is None or until > as_of.date()):
        until = as_of.date()
        notes.append(f'The window was limited to the research cutoff ({until.isoformat()}).')
    for provider in providers:
        if hasattr(provider, 'reset'):
            provider.reset()   # type: ignore[attr-defined]
    tr = news_search(req.query, since=req.since, until=until, languages=req.languages, limit=req.limit, providers=list(providers))
    adapters = [p for p in providers if isinstance(p, DiscoverCallableProvider)]
    denied = {'source_not_authorized': 0, 'invalid_lead': 0, 'redirect_link': 0}
    unresolved: list[str] = []
    found_extra = 0
    for adapter in adapters:
        for key, value in adapter.denied.items():
            denied[key] += value
        unresolved.extend(adapter.unresolved_titles)
        found_extra += sum(adapter.denied.values())
    outcome = NewsOutcome(result={})
    if tr.status in ('failed', 'rate_limited', 'denied', 'unknown'):
        # Only a call that produced nothing at all is blocked/aborted by these signals; a provider failing next
        # to another that answered just leaves a partial result.
        outcome.invalid_response = any(a.invalid_response for a in adapters)
        for adapter in adapters:
            failure = adapter.failure
            if failure is not None and (getattr(failure, 'provider_response_received', False) is not True
                                        and failure.code not in _BENIGN_SEARCH_FAILURES):
                outcome.unknown_failure = failure
        failure = next((a.failure for a in adapters if a.failure is not None), None)
        limited = tr.reason in LIMIT_REASONS
        reason = ('search_response_invalid' if outcome.invalid_response else
                  tr.reason if limited else (failure.code if failure is not None else tr.reason))
        limited_failure = limited or (failure is not None and failure.code in LIMIT_REASONS)
        outcome.result = {'status': 'rate_limited' if (limited_failure and tr.status == 'rate_limited') else 'failed', 'reason': reason}
        if tr.message and not outcome.invalid_response and (failure is None or limited_failure):
            outcome.result['message'] = tr.message[:600]
        elif failure is not None and failure.code == 'search_provider_error' and str(failure):
            outcome.result['message'] = str(failure)[:400]
        if outcome.invalid_response:
            outcome.denied_count, outcome.denied_reasons = sum(denied.values()), denied
            outcome.result.update(denied_count=outcome.denied_count, denied_reasons=denied)
        if tr.retryable:
            outcome.result['retryable'] = True
        return outcome

    groups = [g for g in tr.data if isinstance(g, Mapping)]
    clusters: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    leads_out: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    found_total = 0
    terms_blocked = 0
    gdelt_leads = False
    for group in groups:
        members = [m for m in group.get('members', []) if isinstance(m, NewsLead)]
        found_total += len(members)
        shown_urls: list[str] = []
        shown = 0
        for lead in members:
            if lead.unresolved_redirect:
                denied['redirect_link'] += 1
                if lead.title.strip() and len(unresolved) < 5:
                    unresolved.append(lead.title.strip()[:160])
                continue
            if is_fred_source(lead.url):
                denied['source_not_authorized'] += 1
                continue
            try:
                url = canonical_source_url(lead.url)
            except ValueError:
                denied['invalid_lead'] += 1
                continue
            if shown >= MEMBERS_SHOWN_PER_CLUSTER or url in seen_urls:
                continue
            seen_urls.add(url)
            row = _lead_row(lead, url)
            if lead.acquirable is False:
                terms_blocked += 1
            if 'gdelt' in lead.providers:
                gdelt_leads = True
            rows.append(row)
            shown_urls.append(url)
            shown += 1
            out = dict(row)
            out['excerpt'] = _clip(lead.excerpt, 240) if lead.excerpt else None
            if out['excerpt'] is None:
                del out['excerpt']
            leads_out.append(out)
        if shown_urls:
            cluster: dict[str, Any] = {'cluster_id': group.get('cluster_id'),
                                       'independent_source_count': group.get('independent_source_count'),
                                       'lead_urls': shown_urls}
            if group.get('syndicated_from'):
                cluster['syndicated_from'] = list(group['syndicated_from'])
            more = len(members) - shown
            if more > 0:
                cluster['more_copies'] = more
            clusters.append(cluster)
    answered_gdelt = any('Providers answered:' in n and 'gdelt' in n for n in (tr.coverage.notes if tr.coverage else []))
    if gdelt_leads or answered_gdelt:
        outcome.attributions.append(dict(ATTRIBUTIONS['gdelt']))
    denied_total = sum(denied.values())
    found_count = found_total + found_extra
    reason = 'search_no_results' if found_count == 0 else ('search_sources_denied' if not rows else None)
    result: dict[str, Any] = {'status': 'completed', 'reason': reason, 'leads': leads_out, 'clusters': clusters,
                              'denied_count': denied_total, 'denied_reasons': denied,
                              'warning': 'Unverified leads are not evidence.'}
    if tr.status == 'partial':
        result['providers_partial'] = True
    all_notes = [*notes, *(tr.coverage.notes if tr.coverage else []), *([tr.message] if tr.message and tr.status == 'failed' else [])]
    pruned = [n for n in dict.fromkeys(all_notes) if n and n != GDELT_ATTRIBUTION][:MAX_NOTES]
    if pruned:
        result['notes'] = [_clip(n, 300) for n in pruned]
    if outcome.attributions:
        result['attribution'] = GDELT_ATTRIBUTION
    if terms_blocked:
        result['terms_blocked_count'] = terms_blocked
        if blocked_message:
            result['message'] = blocked_message
    if unresolved:
        result['unresolved_titles'] = unresolved[:5]
        result['message'] = redirect_message or result.get('message')
        if result['message'] is None:
            del result['message']
    outcome.result = result
    outcome.lead_rows = rows
    outcome.denied_count, outcome.denied_reasons = denied_total, denied
    outcome.summary = {
        'kind': 'news_search', 'found_count': found_count, 'eligible_count': len(rows), 'denied_count': denied_total,
        'cluster_count': len(clusters),
        'leads_preview': [{'title': _clip(r['title'], 160), 'host': _host(r['url']), 'published_at': r.get('published_at')} for r in rows[:5]],
    }
    if outcome.attributions:
        outcome.summary['attributions'] = [a['id'] for a in outcome.attributions]
    return outcome
