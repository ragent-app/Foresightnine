"""Make permission-preserving next-step suggestions for unverified retrieval gaps.

Suggestions never invoke tools or assert that an absent passage is undisclosed.
"""
from __future__ import annotations
from typing import Any, Mapping
from .research_evidence_selection import evidence_uses, lead_uses


def gap_routes(requirements: list[Mapping[str, Any]], texts: list[str], *,
               coverage: Mapping[str, Any], explicit_empty: bool,
               capabilities: Mapping[str, bool]) -> list[dict[str, Any]]:
    found: set[str] = set()
    for text in texts:
        found.update(lead_uses(text))
    excluded = coverage.get('sources_excluded') or []
    rows = []
    for req in requirements:
        missing = sorted(set(evidence_uses(req)) - found)
        if not missing:
            continue
        tools = ['search_library']
        if not explicit_empty:
            if any(role.startswith('market_') for role in missing) and capabilities.get('market_data'):
                tools.append('market_data')
            if any(role in {'periods', 'attribution', 'condition', 'performance'} for role in missing):
                if capabilities.get('filings'):
                    tools.append('filings')
                if capabilities.get('issuer_documents'):
                    tools.append('issuer_documents')
        if capabilities.get('news_search'):
            tools.append('news_search')
        if not explicit_empty and capabilities.get('acquire_sources') and len(tools) > 1:
            tools.append('acquire_sources')
        status = 'permission_excluded' if excluded and not coverage.get('sources_searched') else 'not_recalled'
        if not coverage.get('complete', True):
            status = 'search_incomplete'
        rows.append({'requirement_id': req.get('id'), 'missing_uses': missing,
                     'status': status, 'candidate_tools': tools,
                     'note': 'Retrieval cues are unverified; read selected locations. Discovery yields leads; acquisition requires a permitted URL and existing run authorization.'})
    return rows
