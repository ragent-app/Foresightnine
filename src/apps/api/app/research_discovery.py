"""Normalize discovery leads before they enter a research run.

Search results are untrusted leads, not sources or evidence. This module does
not fetch documents or authorize their use.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
import inspect
from typing import Any, Callable

from pydantic import ValidationError

from .phase3 import ClientSearchEvidenceProvider, ProviderFailure
from .operations import ApiProblem
from .research_coordinator import ResearchExecutionContext
from .research_source_catalog import canonical_source_url, source_capabilities


@dataclass(frozen=True)
class SearchLead:
    title: str
    url: str
    published_at: datetime | None
    excerpt: str | None
    publisher: str | None
    independently_reviewable: bool | None


def search_lead_url(raw: object) -> str | None:
    value = raw.get('url') if isinstance(raw, dict) else getattr(raw, 'url', None)
    return value if isinstance(value, str) else None


def normalize_search_lead(raw: object) -> SearchLead:
    """Read and validate a search result independently of its connector type."""
    def field(name: str) -> Any:
        return raw.get(name) if isinstance(raw, dict) else getattr(raw, name, None)

    title, url = field('title'), search_lead_url(raw)
    excerpt, publisher = field('excerpt'), field('publisher')
    published, reviewable = field('published_at'), field('independently_reviewable')
    if not isinstance(title, str) or not title.strip() or not isinstance(url, str) or not url.strip():
        raise ValueError('Search result is missing its title or URL.')
    if any(value is not None and (not isinstance(value, str) or not value.strip()) for value in (excerpt, publisher)):
        raise ValueError('Search result has invalid text metadata.')
    if reviewable is not None and type(reviewable) is not bool:
        raise ValueError('Search result has invalid reviewability metadata.')
    ClientSearchEvidenceProvider().validate_url(url)
    if isinstance(published, str):
        published = datetime.fromisoformat(published) if 'T' in published else date.fromisoformat(published)
    if isinstance(published, date) and not isinstance(published, datetime):
        published = datetime.combine(published, time.min, tzinfo=timezone.utc)
    if published is not None and (not isinstance(published, datetime) or published.utcoffset() is None):
        raise ValueError('Search result has an invalid publication time.')
    return SearchLead(title, url, published, excerpt, publisher, reviewable)


@dataclass
class DiscoveryCandidates:
    candidates: list[dict[str, Any]]
    omitted: int
    deferred_candidates: list[dict[str, Any]]
    invalid_count: int = 0


@dataclass
class DiscoveryResult:
    status: str
    candidates: list[dict[str, Any]]
    omitted: int = 0
    attempted: bool = False
    reason: str | None = None
    retryable: bool = False
    returned_count: int = 0
    provider_limit: int = 10  # Legacy alias: requested batch size, not a provider limit.
    request_limit: int = 10
    has_more: bool | None = None
    deferred_candidates: list[dict[str, Any]] = field(default_factory=list)
    invalid_count: int = 0


def execute_source_search(*, provider: Any, provider_name: str, query: str,
                          convert: Callable[[object, str], Any], as_of: datetime | None,
                          context: ResearchExecutionContext, existing_urls: set[str] | None = None,
                          capacity: int = 10, stage: str | None = None) -> DiscoveryResult:
    """Execute one authorized, bounded discovery step and return unverified leads."""
    if context.cost_limited:
        return DiscoveryResult('cost_unknown', [])
    if context.search_exhaustion():
        return DiscoveryResult('budget_exhausted', [])
    search = getattr(provider, 'search', None)
    available = bool(getattr(provider, 'supports_project_search', False) and callable(search))
    if not source_capabilities(search_available=available)[0].configured:
        return DiscoveryResult('unavailable', [])
    previous_calls = context.search_calls
    try:
        parameters = inspect.signature(search).parameters
        if 'deadline' not in parameters or 'cancel_event' not in parameters:
            raise ProviderFailure('search_budget_unsupported', 'Search budget cannot be enforced.', retryable=False)
        # Do not silently rewrite the question to satisfy provider limits.
        max_query = getattr(provider, "max_query_length", None)
        if isinstance(max_query, int) and len(query) > max_query:
            return DiscoveryResult("restricted", [], reason="query_too_long")
        context.start_search()
        raw = search(query, limit=10, deadline=context.budget.deadline, cancel_event=context.cancel_event())
        if not isinstance(raw, (list, tuple)):
            raise ProviderFailure('search_response_invalid', 'Search returned invalid results.', retryable=False)
        result = collect_source_candidates(raw, provider_name=provider_name, convert=convert,
            as_of=as_of, existing_urls=existing_urls, capacity=capacity, stage=stage)
        status = ('failed' if stage is None and not result.candidates else 'partial') if result.omitted else 'completed'
        return DiscoveryResult(status, result.candidates, result.omitted, attempted=True, returned_count=len(raw), provider_limit=10, request_limit=10, has_more=None, deferred_candidates=result.deferred_candidates, invalid_count=result.invalid_count)
    except (ProviderFailure, ApiProblem, TypeError, ValueError) as error:
        context.ensure_running()
        code = error.code if isinstance(error, ProviderFailure) else error.error_code if isinstance(error, ApiProblem) else None
        reason = {
            'search_timeout': 'timeout', 'search_provider_timeout': 'timeout',
            'search_provider_rate_limited': 'rate_limited', 'rate_limited': 'rate_limited',
            'search_provider_unavailable': 'unavailable', 'search_provider_unconfigured': 'unavailable',
            'search_response_invalid': 'invalid_response', 'search_provider_rejected': 'rejected',
            'search_budget_exhausted': 'budget_exhausted', 'search_query_invalid': 'query_or_provider_limit',
            'search_query_provider_limit': 'query_too_long',
        }.get(code, 'other_failure')
        return DiscoveryResult('failed', [], attempted=context.search_calls > previous_calls,
                               reason=reason, retryable=bool(getattr(error, 'retryable', False)))


def collect_source_candidates(raw_candidates: list | tuple, *, provider_name: str,
                              convert: Callable[[object, str], Any], as_of: datetime | None,
                              existing_urls: set[str] | None = None, capacity: int = 10,
                              stage: str | None = None) -> DiscoveryCandidates:
    """Validate, bound and sanitize search results without promoting them to evidence."""
    seen = {canonical_source_url(url) for url in existing_urls if isinstance(url, str)} if existing_urls is not None else set()
    candidates: list[dict[str, Any]] = []
    omitted = 0
    invalid_count = 0
    deferred: list[dict[str, Any]] = []
    for raw in raw_candidates:
        try:
            candidate = convert(raw, provider_name)
            ClientSearchEvidenceProvider().validate_url(str(candidate.url))
        except (ProviderFailure, ValidationError, ValueError):
            omitted += 1
            invalid_count += 1
            continue
        if as_of is not None and (candidate.published_at is None or
                candidate.published_at.date() >= as_of.astimezone(timezone.utc).date()):
            continue
        address = canonical_source_url(str(candidate.url))
        if address in seen:
            continue
        seen.add(address)
        entry = {'title': candidate.title, 'url': address,
            'published_at': candidate.published_at.isoformat() if candidate.published_at else None,
            'retrieved_at': candidate.retrieved_at.isoformat(), 'search_result_hash': candidate.search_result_hash,
            'search_provider': provider_name, 'status': 'unverified',
            **({'discovery_stage': stage} if stage else {})}
        if len(candidates) >= capacity:
            deferred.append(entry)
        else:
            candidates.append(entry)
    return DiscoveryCandidates(candidates, omitted, deferred, invalid_count)
