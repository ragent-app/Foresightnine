"""Region-neutral source capability descriptions, never grants of permission.

The catalog describes implementations already present. Routing is conservative:
unknown coverage remains unknown and no market is inferred from a question.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class SourceRouteRequest:
    entity: str | None = None
    market: str | None = None
    language: str | None = None
    material_type: str | None = None
    as_of: datetime | None = None
    permission: str | None = None
    available_only: bool = False


@dataclass(frozen=True)
class SourceCapability:
    name: str
    output_type: Literal['leads', 'document', 'structured_data']
    permission: str
    configured: bool
    markets: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    historical: bool = False
    hosts: tuple[str, ...] = ()
    coverage: str = 'unknown'


def source_capabilities(*, search_available: bool = False, sec_available: bool = False) -> tuple[SourceCapability, ...]:
    return (
        SourceCapability('project_search', 'leads', 'search', search_available),
        SourceCapability('official_html_pdf', 'document', 'official_retrieval', True,
                         hosts=('www.bea.gov', 'www.federalreserve.gov'), coverage='restricted_hosts'),
        SourceCapability('sec_filing', 'document', 'official_retrieval', sec_available,
                         markets=('US',), hosts=('www.sec.gov',), coverage='restricted_paths'),
        SourceCapability('sec_company_concept', 'structured_data', 'official_retrieval', sec_available,
                         markets=('US',), hosts=('data.sec.gov',), coverage='company_concept_only'),
    )


def route_source_capabilities(request: SourceRouteRequest, *, search_available: bool = False,
                              sec_available: bool = False) -> tuple[SourceCapability, ...]:
    """Return matching capabilities; empty coverage means unknown, not universal."""
    return tuple(cap for cap in source_capabilities(search_available=search_available, sec_available=sec_available)
                 if (not request.available_only or cap.configured)
                 and (request.permission is None or request.permission == cap.permission)
                 and (request.material_type is None or request.material_type == cap.output_type)
                 and (not request.market or not cap.markets or request.market.upper() in cap.markets)
                 and (not request.language or not cap.languages or request.language.lower() in cap.languages)
                 and (request.as_of is None or cap.historical or cap.output_type == 'leads'))


def canonical_source_url(url: str) -> str:
    """Drop only fragment; query parameters can identify distinct documents."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ''))


def official_document_capability(url: str, *, sec_available: bool) -> SourceCapability | None:
    parts = urlsplit(url)
    if parts.scheme.lower() != 'https' or parts.username is not None or parts.password is not None or parts.port not in (None, 443):
        return None
    return next((cap for cap in source_capabilities(sec_available=sec_available)
                 if cap.output_type == 'document' and parts.hostname in cap.hosts), None)
