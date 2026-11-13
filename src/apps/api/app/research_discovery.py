"""Normalize discovery leads before they enter a research run.

Search results are untrusted leads, not sources or evidence. This module does
not fetch documents or authorize their use.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError

from .phase3 import ClientSearchEvidenceProvider, ProviderFailure
from .operations import ApiProblem
from .research_coordinator import ResearchExecutionContext


@dataclass
class DiscoveryCandidates:
    candidates: list[dict[str, Any]]
    omitted: int


@dataclass
class DiscoveryResult:
    status: str
    candidates: list[dict[str, Any]]
    omitted: int = 0
    attempted: bool = False


def execute_source_search(*, provider: Any, provider_name: str, query: str,
                          convert: Callable[[object, str], Any], as_of: datetime | None,
                          context: ResearchExecutionContext, existing_urls: set[str] | None = None,
                          capacity: int = 5, stage: str | None = None) -> DiscoveryResult:
    """Execute one authorized, bounded discovery step and return unverified leads."""
    if context.cost_limited:
        return DiscoveryResult('cost_unknown', [])
    if context.search_exhaustion():
        return DiscoveryResult('budget_exhausted', [])
    search = getattr(provider, 'search', None)
    if not getattr(provider, 'supports_project_search', False) or not callable(search):
        return DiscoveryResult('unavailable', [])
    previous_calls = context.search_calls
    try:
        parameters = inspect.signature(search).parameters
        if 'deadline' not in parameters or 'cancel_event' not in parameters:
            raise ProviderFailure('search_budget_unsupported', 'Search budget cannot be enforced.', retryable=False)
        context.start_search()
        raw = search(query, limit=5, deadline=context.budget.deadline, cancel_event=context.cancel_event())
        if not isinstance(raw, (list, tuple)):
            raise ProviderFailure('search_response_invalid', 'Search returned invalid results.', retryable=False)
        result = collect_source_candidates(raw, provider_name=provider_name, convert=convert,
            as_of=as_of, existing_urls=existing_urls, capacity=capacity, stage=stage)
        status = ('failed' if stage is None and not result.candidates else 'partial') if result.omitted else 'completed'
        return DiscoveryResult(status, result.candidates, result.omitted, attempted=True)
    except (ProviderFailure, ApiProblem, TypeError, ValueError):
        context.ensure_running()
        return DiscoveryResult('failed', [], attempted=context.search_calls > previous_calls)


def collect_source_candidates(raw_candidates: list | tuple, *, provider_name: str,
                              convert: Callable[[object, str], Any], as_of: datetime | None,
                              existing_urls: set[str] | None = None, capacity: int = 5,
                              stage: str | None = None) -> DiscoveryCandidates:
    """Validate, bound and sanitize search results without promoting them to evidence."""
    seen = set(existing_urls) if existing_urls is not None else None
    candidates: list[dict[str, Any]] = []
    omitted = 0
    for raw in raw_candidates[:5]:
        try:
            candidate = convert(raw, provider_name)
            ClientSearchEvidenceProvider().validate_url(str(candidate.url))
        except (ProviderFailure, ValidationError, ValueError):
            omitted += 1
            continue
        if as_of is not None and (candidate.published_at is None or
                candidate.published_at.date() >= as_of.astimezone(timezone.utc).date()):
            continue
        parts = urlsplit(str(candidate.url))
        address = urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
        if seen is not None and address in seen:
            continue
        if len(candidates) >= capacity:
            omitted += 1
            continue
        if seen is not None:
            seen.add(address)
        candidates.append({'title': candidate.title, 'url': address,
            'published_at': candidate.published_at.isoformat() if candidate.published_at else None,
            'retrieved_at': candidate.retrieved_at.isoformat(), 'search_result_hash': candidate.search_result_hash,
            'search_provider': provider_name, 'status': 'unverified',
            **({'discovery_stage': stage} if stage else {})})
    return DiscoveryCandidates(candidates, omitted)
