"""Shared, network-free source eligibility and run provenance boundary.

All consumers use the same permission, cutoff and source-cap rules. Summaries
record selection only; they do not verify source truth or model comprehension.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .research_coverage import initial_source_coverage
from .research_sources import ResearchSource

MAX_SOURCES = 20


def source_eligibility(sources: list[ResearchSource], as_of: datetime | None) -> list[tuple[ResearchSource, str]]:
    """Classify each selected source without relying on presentation text."""
    result: list[tuple[ResearchSource, str]] = []
    eligible_count = 0
    for source in sources:
        parsed = urlsplit(str(source.url)) if source.url else None
        host = (parsed.hostname or "").lower().rstrip(".") if parsed else ""
        is_fred = (host in {"fred.stlouisfed.org", "alfred.stlouisfed.org"} or
                   host.endswith(".fred.stlouisfed.org") or
                   (host == "api.stlouisfed.org" and parsed is not None and parsed.path.lower().startswith("/fred")))
        if source.is_simulated:
            code = 'simulated'
        elif is_fred:
            code = 'source_not_authorized'
        elif not source.permissions.model_input:
            code = 'model_permission_missing'
        elif not source.permissions.display:
            code = 'display_permission_missing'
        elif as_of is not None and (source.published_at is None or source.published_at > as_of or
                                    (source.revised_at is not None and source.revised_at > as_of)):
            code = 'availability_unestablished'
        elif eligible_count >= MAX_SOURCES:
            code = 'source_limit'
        else:
            code = 'eligible'
            eligible_count += 1
        result.append((source, code))
    return result


def prepare_sources(sources: list[ResearchSource], as_of: datetime | None) -> tuple[list[ResearchSource], list[str]]:
    eligible: list[ResearchSource] = []
    limitations: list[str] = []
    messages = {'simulated': 'simulated material is excluded from real research.',
                'source_not_authorized': 'FRED use is not authorized for this experiment.',
                'model_permission_missing': 'model processing permission is not granted.',
                'display_permission_missing': 'display permission is not granted.',
                'availability_unestablished': 'availability at the requested time is not established.',
                'source_limit': 'exceeds the source count budget; not processed.'}
    for source, code in source_eligibility(sources, as_of):
        if code != 'eligible':
            limitations.append(f"Source {source.source_id}: {messages[code]}")
        else:
            eligible.append(source)
            if as_of is not None:
                limitations.append(f"Source {source.source_id}: publication and revision history are user-declared and not independently verified.")
    return eligible, limitations


def summarize_sources(sources: list[ResearchSource], analyzed_ids: list[str], segments: object) -> list[dict[str, Any]]:
    return [{"source_id": s.source_id, "title": s.title,
             "url": urlunsplit((urlsplit(str(s.url)).scheme, urlsplit(str(s.url)).netloc,
                 urlsplit(str(s.url)).path, "", "")) if s.url else None,
             "event_at": s.event_at.isoformat() if s.event_at else None,
             "published_at": s.published_at.isoformat() if s.published_at else None,
             "obtained_at": s.obtained_at.isoformat(),
             "revised_at": s.revised_at.isoformat() if s.revised_at else None,
             "content_sha256": s.content_sha256, "is_simulated": s.is_simulated,
             "initial_prompt_coverage": initial_source_coverage(s.source_id, len(s.text), segments),
             "pdf_text_order": s.pdf_text_order,
             "derived_from_source_id": s.derived_from_source_id,
             "verification": "user_declared_not_verified"}
            for s in sources if s.source_id in analyzed_ids]


def summarize_source_processing(sources: list[ResearchSource], as_of: datetime | None,
                                segments: object) -> list[dict[str, Any]]:
    records = []
    for source, eligibility in source_eligibility(sources, as_of):
        row: dict[str, Any] = {'source_id': source.source_id, 'eligibility': eligibility}
        if eligibility == 'eligible':
            coverage = initial_source_coverage(source.source_id, len(source.text), segments)
            has_segment = isinstance(segments, list) and any(
                isinstance(item, dict) and item.get('source_id') == source.source_id for item in segments)
            row['initial_selection'] = ('not_selected' if isinstance(segments, list) and not has_segment
                                        else coverage['status'])
            row['selected_characters'] = coverage['selected_characters']
            row['text_characters'] = coverage['text_characters']
        else:
            row['initial_selection'] = 'excluded'
        records.append(row)
    return records
