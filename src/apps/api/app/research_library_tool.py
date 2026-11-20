"""Run-scoped wiring of ``ResearchLibrary`` for the ``search_library`` tool (plan W4).

The library itself (``research_library``) is pure computation.  This module owns the glue that must not live in
the library: which of a run's sources are searchable (the same eligibility rule as every other read tool), how
newly acquired sources and OCR versions are folded in (``add_source`` / ``replace_source``), and how a hit is
turned into something the model can act on (``read_sources`` range, ``read_pages`` page, OCR verification note).
Nothing here touches the network or a model.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .research_library import Hit, ResearchLibrary
from .research_source_policy import source_eligibility
from .research_sources import ResearchSource

MAX_LIBRARY_HITS = 20
DEFAULT_LIBRARY_HITS = 8
MAX_LIBRARY_SOURCE_IDS = 50
MAX_COVERAGE_ROWS = 10
_MAX_VERSION_HOPS = 50

OCR_HIT_NOTE = ('This passage comes from OCR text; verify numbers against the original page image before recording '
                'facts from it.')


class SearchLibraryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=500, description=(
        'Natural-language keywords, numbers, dates or a phrase. Results are the best-matching passages across all '
        'authorized sources of this run (ranked), with their read locations; use search_source for an exact literal '
        'string inside one source.'))
    source_ids: list[str] | None = Field(default=None, max_length=MAX_LIBRARY_SOURCE_IDS, description=(
        'Optional: restrict the search to these source_ids (default: every authorized source of this run).'))
    max_hits: int = Field(default=DEFAULT_LIBRARY_HITS, ge=1, le=MAX_LIBRARY_HITS,
                          description=f'Maximum number of passages to return (1-{MAX_LIBRARY_HITS}).')
    rationale: str | None = Field(default=None, min_length=1, description='Optional brief reason for this search.')

    @field_validator('query')
    @classmethod
    def nonblank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('Query must not be empty.')
        return value


def head_source_id(source_id: str, versions: Mapping[str, str], known: Mapping[str, Any]) -> str:
    """Follow OCR version links (old id -> newer id) to the newest stored version."""
    for _ in range(_MAX_VERSION_HOPS):
        nxt = versions.get(source_id)
        if nxt is None or nxt not in known:
            break
        source_id = nxt
    return source_id


def sync_library(library: ResearchLibrary, source_by_id: Mapping[str, ResearchSource], versions: Mapping[str, str],
                 as_of: datetime | None) -> None:
    """Bring ``library`` in line with the run's current sources, incrementally.

    * an eligible source not yet indexed is added;
    * a source with a newer (OCR) version is swapped for its head with ``replace_source``;
    * a source that is not eligible is only remembered with its reason so coverage can explain the exclusion.
    """
    for source_id, source in source_by_id.items():
        head_id = head_source_id(source_id, versions, source_by_id)
        if head_id != source_id:
            head = source_by_id[head_id]
            if source_eligibility([head], as_of)[0][1] != 'eligible':
                continue
            if head_id in library:
                library.remove_source(source_id)
            elif source_id in library:
                library.replace_source(source_id, head)
            continue
        code = source_eligibility([source], as_of)[0][1]
        if code != 'eligible':
            library.note_ineligible(source_id, code)
            continue
        if source_id not in library:
            library.add_source(source)


def _page_check(source: ResearchSource | None, page: int | None, needs_check: Callable[[ResearchSource, int], bool] | None) -> bool:
    return bool(source is not None and page is not None and needs_check is not None and needs_check(source, page))


def hit_row(hit: Hit, source: ResearchSource | None, needs_check: Callable[[ResearchSource, int], bool] | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        'source_id': hit.source_id, 'title': hit.title, 'page': hit.page, 'score': round(hit.score, 4),
        'snippet': hit.snippet, 'matched_terms': list(hit.matched_terms), 'reasons': list(hit.reasons),
        # Exact locations: read the passage (or the whole page) before citing it.
        'read_sources': {'source_id': hit.source_id, 'start': hit.char_start, 'end': hit.char_end},
    }
    if hit.page is not None:
        row['read_pages'] = {'source_id': hit.source_id, 'first_page': hit.page, 'last_page': hit.page}
    if hit.context:
        row['table_header'] = hit.context
    if hit.extraction_method == 'ocr':
        row['extraction_method'] = 'ocr'
        row['verification_note'] = OCR_HIT_NOTE
        if _page_check(source, hit.page, needs_check):
            row['low_ocr_confidence'] = True
    return row


def compact_coverage(coverage: Mapping[str, Any]) -> dict[str, Any]:
    """Bounded model-facing form of ``ResearchLibrary.coverage``."""
    searched = [s for s in coverage.get('searched_sources') or [] if isinstance(s, Mapping)]
    with_gaps = [{'source_id': s['source_id'], 'title': s.get('title'), 'pages_total': s.get('pages_total'),
                  'unsearched_pages': s.get('unsearched_pages')}
                 for s in searched if s.get('unsearched_pages')]
    no_text = [s['source_id'] for s in searched if s.get('note') == 'no_extracted_text']
    excluded = [s for s in coverage.get('excluded_sources') or [] if isinstance(s, Mapping)]
    out: dict[str, Any] = {
        'sources_searched': len(searched), 'passages_indexed': sum(int(s.get('passages') or 0) for s in searched),
        'unsearched_page_count': coverage.get('unsearched_page_count', 0), 'complete': bool(coverage.get('complete')),
    }
    if with_gaps:
        out['sources_with_unsearched_pages'] = with_gaps[:MAX_COVERAGE_ROWS]
    if no_text:
        out['sources_without_text'] = no_text[:MAX_COVERAGE_ROWS]
    if excluded:
        out['sources_excluded'] = [{'source_id': e['source_id'], 'reason': e.get('reason')} for e in excluded[:MAX_COVERAGE_ROWS]]
        if len(excluded) > MAX_COVERAGE_ROWS:
            out['sources_excluded_more'] = len(excluded) - MAX_COVERAGE_ROWS
    if coverage.get('notes'):
        out['notes'] = list(coverage['notes'])[:4]
    return out


def run_search_library(req: SearchLibraryRequest, *, library: ResearchLibrary, source_by_id: Mapping[str, ResearchSource],
                       versions: Mapping[str, str], as_of: datetime | None,
                       needs_check: Callable[[ResearchSource, int], bool] | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Search the (already synced) library; returns the tool result and its event summary."""
    requested: list[str] | None = None
    if req.source_ids is not None:
        requested = list(dict.fromkeys(head_source_id(s, versions, source_by_id) for s in req.source_ids))
    hits = library.search(req.query, source_ids=requested, top_k=req.max_hits)
    coverage = compact_coverage(hits.coverage)
    rows = [hit_row(h, source_by_id.get(h.source_id), needs_check) for h in hits]
    result: dict[str, Any] = {'status': 'completed', 'query': req.query, 'returned_hits': len(rows), 'hits': rows,
                              'library_coverage': coverage}
    notes: list[str] = []
    if requested is not None and req.source_ids is not None and requested != list(dict.fromkeys(req.source_ids)):
        result['source_versions_applied'] = True
        notes.append('Some requested source_ids have a newer OCR version; the newer version was searched.')
    if not rows:
        result['reason'] = 'no_hits'
        notes.append('No passage matched this query.')
    if not coverage['complete']:
        notes.append('Not every page or source could be searched (see library_coverage); a miss here is not evidence '
                     'that the text is absent. Hits are leads: read the location with read_sources or read_pages before citing.')
    elif rows:
        notes.append('Hits are leads: read the location with read_sources or read_pages before citing.')
    if notes:
        result['message'] = ' '.join(notes)
    summary = {'kind': 'search_library', 'query': req.query[:120], 'hit_count': len(rows),
               'source_count': coverage['sources_searched'], 'unsearched_pages': int(coverage['unsearched_page_count'] or 0),
               'complete': coverage['complete'],
               'hits_preview': [{'title': str(r['title'])[:160], 'page': r['page'], 'source_id': r['source_id']} for r in rows[:5]]}
    return result, summary
