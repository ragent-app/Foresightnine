"""Normalize discovery leads before they enter a research run.

Search results are untrusted leads, not sources or evidence. This module does
not fetch documents or authorize their use.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError

from .phase3 import ClientSearchEvidenceProvider, ProviderFailure


@dataclass
class DiscoveryCandidates:
    candidates: list[dict[str, Any]]
    omitted: int


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
