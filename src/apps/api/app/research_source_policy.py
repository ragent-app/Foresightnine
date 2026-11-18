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


def is_fred_source(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower().rstrip('.')
    return (host in {'fred.stlouisfed.org', 'alfred.stlouisfed.org'}
            or host.endswith('.fred.stlouisfed.org')
            or (host == 'api.stlouisfed.org' and parsed.path.lower().startswith('/fred')))


def source_eligibility(sources: list[ResearchSource], as_of: datetime | None) -> list[tuple[ResearchSource, str]]:
    """Classify each selected source without relying on presentation text."""
    result: list[tuple[ResearchSource, str]] = []
    for source in sources:
        if source.is_simulated:
            code = 'simulated'
        elif source.url is not None and is_fred_source(str(source.url)):
            code = 'source_not_authorized'
        elif not source.permissions.model_input:
            code = 'model_permission_missing'
        elif not source.permissions.display:
            code = 'display_permission_missing'
        elif as_of is not None and (source.published_at is None or source.published_at > as_of or
                                    (source.revised_at is not None and source.revised_at > as_of)):
            code = 'availability_unestablished'
        else:
            code = 'eligible'
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
                                segments: object, *, generation_status: str = 'unknown') -> list[dict[str, Any]]:
    records = []
    for source, eligibility in source_eligibility(sources, as_of):
        row: dict[str, Any] = {'source_id': source.source_id, 'eligibility': eligibility, 'provided': True}
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
        row['selection_status'] = row['initial_selection']
        # Selection observations are version-local; they do not establish
        # semantic support, comprehension, or source absence.
        source_segments = [item for item in segments if isinstance(item, dict) and item.get('source_id') == source.source_id] if isinstance(segments, list) else []
        if source_segments and all(isinstance(item.get('context_unit_ids'), list) and
                                   isinstance(item.get('retrieval_facet_indexes'), list) for item in source_segments):
            groups = {group for item in source_segments for group in item['context_unit_ids'] if isinstance(group, str)}
            facets = {facet for item in source_segments for facet in item['retrieval_facet_indexes'] if type(facet) is int and facet >= 0}
            row['selection_details'] = {'selected_unit_count': len(groups), 'matched_facet_indexes': sorted(facets),
                                        'unmatched_requirement_indexes': None, 'budget_limited': None, 'structure_unknown': None}
        if eligibility != 'eligible':
            row['selection_gap'] = 'excluded'
            row['analysis_status'] = 'not_started'
        elif generation_status == 'not_started':
            row['selection_gap'] = 'not_started'
            row['analysis_status'] = 'not_started'
        else:
            row['selection_gap'] = ('not_selected_reason_unknown' if isinstance(segments, list) and not has_segment else None)
            row['analysis_status'] = ('produced' if has_segment and generation_status == 'completed' else
                                      'not_completed' if generation_status in ('failed', 'partial', 'cancelled') else 'unknown')
        records.append(row)
    return records


def source_language_hints(sources: list[ResearchSource], as_of: datetime | None) -> tuple[str, ...]:
    """Coarse script labels from eligible material, never raw text or claims."""
    scripts: set[str] = set()
    for source, eligibility in source_eligibility(sources, as_of):
        if eligibility != 'eligible':
            continue
        sample = source.text[:4000]
        if any('a' <= ch.lower() <= 'z' for ch in sample):
            scripts.add('latin')
        if any('\u3400' <= ch <= '\u9fff' for ch in sample):
            scripts.add('cjk')
        if any('\uac00' <= ch <= '\ud7af' for ch in sample):
            scripts.add('hangul')
        if any('\u3040' <= ch <= '\u30ff' for ch in sample):
            scripts.add('japanese_kana')
    return tuple(sorted(scripts))
