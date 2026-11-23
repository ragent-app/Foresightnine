"""Region-neutral source capability descriptions, never grants of permission.

The catalog describes implementations already present. Routing is conservative:
unknown coverage remains unknown and no market is inferred from a question.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from .research_source_terms import terms_decision
from .research_fetch import fetch_allowed_hosts, is_host_blocked, is_sec_host, validate_public_https_url


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
    allowed_hosts = fetch_allowed_hosts()
    hosts = tuple(sorted(allowed_hosts)) if allowed_hosts else ()
    coverage = 'restricted_hosts' if allowed_hosts else 'open_public_https'
    return (
        SourceCapability('project_search', 'leads', 'search', search_available),
        SourceCapability('official_html_pdf', 'document', 'official_retrieval', True,
                         hosts=hosts, coverage=coverage),
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
    try:
        hostname = validate_public_https_url(url, allow_fragment=True, check_sec_path=False)
    except ValueError:
        return None
    if is_host_blocked(hostname) or not terms_decision(url).allowed:
        return None
    caps = source_capabilities(sec_available=sec_available)
    if is_sec_host(hostname):
        if hostname != 'www.sec.gov':
            return None
        return next((cap for cap in caps if cap.name == 'sec_filing'), None)
    web_cap = next((cap for cap in caps if cap.name == 'official_html_pdf'), None)
    if web_cap is None:
        return None
    if web_cap.hosts and hostname not in web_cap.hosts:
        return None
    return web_cap


def run_evidence_routes(caps: dict, *, available_source: bool) -> list[dict[str, str]]:
    """The catalog is descriptive. Authorization remains with the configured tool."""
    routes = []

    def offer(tool: str, evidence: str, limit: str) -> None:
        routes.append({'tool': tool, 'provides': evidence, 'scope': limit})

    if available_source:
        offer('search_library / read_sources', 'Located passages from current authorized documents',
              'Check the source version, effective date, exceptions and selected coverage.')
    if caps.get('news_search_available'):
        offer('news_search', 'Public leads for missing evidence or a counterargument',
              'Public keywords only. A search hit is not the underlying document.')
    if caps.get('acquire_available'):
        offer('acquire_sources', 'Original text from a permitted discovered or user-supplied URL',
              'Subject to source terms, access, availability and the research cutoff.')
    if caps.get('filings_available') or caps.get('issuer_documents_available'):
        offer(' / '.join(k for k in ('filings', 'issuer_documents') if caps.get(k + '_available')),
              'Issuer or regulatory document discovery', 'Confirm issuer, jurisdiction, effective version and dates.')
    if caps.get('sec_financials_available') or caps.get('cn_financials_available'):
        offer('company_financials', 'Issuer-reported structured figures',
              'Inspect available markets, periods, units and line definitions in the tool results.')
    if any(caps.get(k) for k in ('market_data_available', 'cn_market_data_available', 'hk_market_data_available')):
        offer('market_data', 'Instrument price and volume observations', 'Coverage and freshness depend on the selected connector.')
    offer('calculate_financials', 'Reproducible calculations from located input facts',
          'Match dates, units, currency, instrument and formula. Keep assumptions distinct from observations.')
    return routes
