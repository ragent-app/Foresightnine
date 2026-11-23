"""Run-scoped wiring of ``ResearchLibrary`` for the ``search_library`` tool (plan W4).

The library itself (``research_library``) is pure computation.  This module owns the glue that must not live in
the library: which of a run's sources are searchable (the same eligibility rule as every other read tool), how
newly acquired sources and OCR versions are folded in (``add_source`` / ``replace_source``), optional protocol-neutral
embedding scoring via ``default_vector_scorer``, and how a hit is turned into something the model can act on
(``read_sources`` range, ``read_pages`` page, OCR verification note).
"""
from __future__ import annotations

from datetime import datetime
from collections import Counter
from functools import lru_cache
import logging
import os
import re
from typing import Any, Callable, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .research_diagnostics import safe_exception_diagnostic
from .research_embeddings import (
    DEFAULT_EMBEDDING_CACHE_SIZE,
    EMBEDDING_CACHE_SIZE_ENV,
    EmbeddingCache,
    EmbeddingClient,
    EmbeddingConfigError,
    EmbeddingSettings,
    EmbeddingVectorScorer,
    default_embedding_cache,
)
from .research_library import _UNSET, Hit, ResearchLibrary, VectorScorer, _Unset, _Tokenizer, _is_numeric_term
from .research_source_policy import source_eligibility
from .research_sources import ResearchSource

_logger = logging.getLogger(__name__)

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
    requirement_ids: list[str] | None = Field(default=None, max_length=10, description=(
        'Optional IDs of existing research evidence requirements to guide retrieval. Omit for current unmet core requirements.'))

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


def default_vector_scorer(
    environ: Mapping[str, str] | None = None,
    *,
    cache: EmbeddingCache | None = None,
    transport: Any = None,
) -> EmbeddingVectorScorer | None:
    """Return an ``EmbeddingVectorScorer`` when configured in ``environ``, else ``None``."""
    env = environ if environ is not None else os.environ
    try:
        settings = EmbeddingSettings.from_environ(env)
    except EmbeddingConfigError as error:
        _logger.warning("embedding_configuration_invalid metadata=%s", safe_exception_diagnostic(error))
        return None
    if settings is None:
        return None
    if cache is not None:
        resolved_cache = cache
    elif (env.get(EMBEDDING_CACHE_SIZE_ENV) or "").strip() and settings.cache_size != DEFAULT_EMBEDDING_CACHE_SIZE:
        resolved_cache = EmbeddingCache(settings.cache_size)
    else:
        resolved_cache = default_embedding_cache()
    return EmbeddingVectorScorer(EmbeddingClient(settings, cache=resolved_cache, transport=transport))


def create_research_library(
    sources: Iterable[ResearchSource] = (),
    *,
    ineligible: Mapping[str, str] | None = None,
    vector_scorer: VectorScorer | None | _Unset = _UNSET,
    environ: Mapping[str, str] | None = None,
) -> ResearchLibrary:
    """Create a ``ResearchLibrary`` wired with ``default_vector_scorer`` unless explicitly overridden."""
    resolved = default_vector_scorer(environ=environ) if vector_scorer is _UNSET else vector_scorer
    return ResearchLibrary(sources, ineligible=ineligible, vector_scorer=resolved)


def sync_library(
    library: ResearchLibrary,
    source_by_id: Mapping[str, ResearchSource],
    versions: Mapping[str, str],
    as_of: datetime | None,
    *,
    vector_scorer: VectorScorer | None | _Unset = _UNSET,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Bring ``library`` in line with the run's current sources, incrementally.

    * an eligible source not yet indexed is added;
    * a source with a newer (OCR) version is swapped for its head with ``replace_source``;
    * a source that is not eligible is only remembered with its reason so coverage can explain the exclusion.
    """
    if vector_scorer is not _UNSET:
        if library.vector_scorer is not vector_scorer:
            library.attach_vector_scorer(vector_scorer)  # type: ignore[arg-type]
    elif not library.vector_scorer_explicit and library.vector_scorer is None:
        resolved = default_vector_scorer(environ=environ)
        if resolved is not None:
            library.attach_vector_scorer(resolved)
    # Remove stale entries before any retrieval, including revoked and removed sources.
    for indexed_id in library.source_ids:
        current = source_by_id.get(indexed_id)
        if current is None or head_source_id(indexed_id, versions, source_by_id) != indexed_id:
            library.remove_source(indexed_id)
        elif source_eligibility([current], as_of)[0][1] != 'eligible':
            library.remove_source(indexed_id)
    for source_id, source in source_by_id.items():
        head_id = head_source_id(source_id, versions, source_by_id)
        if head_id != source_id:
            head = source_by_id[head_id]
            if source_eligibility([head], as_of)[0][1] != 'eligible':
                continue
            # The current head is processed independently in this loop.
            continue
        code = source_eligibility([source], as_of)[0][1]
        if code != 'eligible':
            library.note_ineligible(source_id, code)
            continue
        if source_id not in library:
            library.add_source(source)
        else:
            indexed = library.indexed_source(source_id)
            if indexed is not None and (indexed.text != source.text or indexed.provenance != source.provenance
                                        or indexed.content_sha256 != source.content_sha256):
                library.replace_source(source_id, source)


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
    if isinstance(coverage.get('retrieval_mode'), str):
        out['retrieval_mode'] = coverage['retrieval_mode']
    if type(coverage.get('embedding_cache_hits')) is int:
        out['embedding_cache_hits'] = coverage['embedding_cache_hits']
    if type(coverage.get('embedding_cache_misses')) is int:
        out['embedding_cache_misses'] = coverage['embedding_cache_misses']
    if isinstance(coverage.get('vector_fallback_reason'), str):
        out['vector_fallback_reason'] = coverage['vector_fallback_reason']
    return out


def requirement_search_query(requirement: Mapping[str, Any]) -> str:
    """Build retrieval cues from the existing assessed requirement, never from answer labels."""
    question = str(requirement.get('question') or '').strip()
    kind = str(requirement.get('kind') or '')
    metric = requirement.get('metric')
    hints: list[str] = []
    if kind.startswith('financial:'):
        if isinstance(metric, str) and metric:
            hints.append(metric.replace('_', ' '))
        hints.append('financial statement comparable periods change increase drivers 财务报表 可比期间 变化 增长 原因')
    elif kind.startswith('contribution:'):
        hints.append('drivers contribution change 原因 驱动 贡献 变化')
    elif kind == 'counterevidence':
        hints.append('risk decline uncertainty alternative 风险 下滑 不确定 反证')
    elif kind.startswith('market:'):
        hints.append('price trading date benchmark 价格 交易日 基准')
    return ' '.join([*hints, question])[:500]


def run_search_library(req: SearchLibraryRequest, *, library: ResearchLibrary, source_by_id: Mapping[str, ResearchSource],
                       versions: Mapping[str, str], as_of: datetime | None,
                       needs_check: Callable[[ResearchSource, int], bool] | None = None,
                       requirements: Iterable[Mapping[str, Any]] = (),
                       selection_mode: str = 'baseline', expand_requirements: bool = True,
                       selection_max_bytes: int | None = None,
                       read_ranges: Iterable[Mapping[str, Any]] = ()) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Retrieve broad candidates and distribute slots across actual unmet requirements.

    Lexical matches are leads only; neither selection nor absence proves a requirement satisfied.
    """
    requested: list[str] | None = None
    if req.source_ids is not None:
        requested = list(dict.fromkeys(head_source_id(s, versions, source_by_id) for s in req.source_ids))
    available = [r for r in requirements if isinstance(r, Mapping) and isinstance(r.get('id'), str)
                 and isinstance(r.get('question'), str) and r['question'].strip()]
    if req.requirement_ids is not None:
        known = {r['id'] for r in available}
        if any(rid not in known for rid in req.requirement_ids):
            return {'status': 'invalid', 'reason': 'unknown_requirement_id'}, None
        wanted = set(req.requirement_ids)
        active = [r for r in available if r['id'] in wanted]
    else:
        active = [r for r in available if r.get('core', True) and r.get('status') not in ('satisfied', 'not_applicable')]
    active = active[:10]
    # Query is always first; each requirement has its own retrieval opportunity.
    # The broad pool remains bounded; requirement expansion uses local BM25 only.
    primary = library.search(req.query, source_ids=requested, top_k=max(50, req.max_hits * 5))
    queues = [(r['id'], library.search(requirement_search_query(r), source_ids=requested,
                                      top_k=max(50, req.max_hits * 5), use_vector=False)) for r in active] if expand_requirements else []
    # Only previously read, still eligible text may suggest a precise follow-up.
    cues: list[str] = []
    for span in read_ranges:
        source = source_by_id.get(str(span.get('source_id') or ''))
        start, end = span.get('start'), span.get('end')
        if not source or source_eligibility([source], as_of)[0][1] != 'eligible':
            continue
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(source.text):
            continue
        fragment = source.text[start:end]
        for match in re.findall(r'(?i)\b(?:note|section|clause)\s+\d+[a-z]?\b|(?:附注|条款)\s*\d+', fragment):
            if match not in cues:
                cues.append(match)
        if len(cues) >= 5:
            break
    cue_queries = [(cue, library.search(cue, source_ids=requested, top_k=max(50, req.max_hits * 5), use_vector=False))
                   for cue in cues[:5]] if expand_requirements else []
    candidates: dict[tuple[str, int, int], dict[str, Any]] = {}
    def add(hit: Hit, rid: str | None) -> None:
        key = (hit.source_id, hit.char_start, hit.char_end)
        entry = candidates.setdefault(key, {'hit': hit, 'requirement_ids': []})
        if rid is not None and rid not in entry['requirement_ids']:
            entry['requirement_ids'].append(rid)
    for hit in primary:
        add(hit, None)
    for rid, hits in queues:
        for hit in hits:
            add(hit, rid)
    for _, hits in cue_queries:
        for hit in hits:
            add(hit, None)
    selected: list[tuple[str, int, int]] = []
    selected_text: set[str] = set()
    seen_terms: set[str] = set()
    tokenizer = _Tokenizer()
    reasons: dict[tuple[str, int, int], str] = {}
    ordered = list(candidates)
    @lru_cache(maxsize=1100)
    def body(key: tuple[str, int, int]) -> str:
        source = source_by_id.get(key[0])
        return source.text[key[1]:key[2]] if source else ''
    @lru_cache(maxsize=1100)
    def eligible(key: tuple[str, int, int]) -> bool:
        source = source_by_id.get(key[0])
        return bool(source and source_eligibility([source], as_of)[0][1] == 'eligible')
    @lru_cache(maxsize=1100)
    def terms(key: tuple[str, int, int]) -> set[str]:
        return {term for term in tokenizer.terms(body(key)) if not _is_numeric_term(term)}
    def pick(key: tuple[str, int, int], reason: str) -> None:
        selected.append(key)
        selected_text.add(body(key))
        seen_terms.update(terms(key))
        reasons[key] = reason
    # Use the assessed requirement's kind and text to distinguish retrieval
    # goals, then favor precisely locatable source material. A numeric value,
    # period or condition is only a selection cue, never proof of relevance.
    for rid, hits in queues:
        possible = [key for key in ordered if key not in selected and eligible(key) and body(key) not in selected_text]
        if not possible or len(selected) >= req.max_hits:
            break
        rank = {key: i for i, key in enumerate(ordered)}
        requirement_rank = {(h.source_id, h.char_start, h.char_end): i for i, h in enumerate(hits)}
        requirement = next(row for row in active if row['id'] == rid)
        query_terms = {t for t in tokenizer.terms(requirement_search_query(requirement)) if not _is_numeric_term(t)}
        def selection_key(item: tuple[str, int, int]) -> tuple[int, int, int, int]:
            item_terms = terms(item)
            text = body(item)
            source = source_by_id[item[0]]
            # Distinct evidence-bearing text is preferable to long generic
            # commentary. Count kinds of cues, never magnitude or identity.
            numeric = bool(re.search(r'\d', text))
            period = bool(re.search(r'(?<!\d)(?:19|20)\d{2}(?!\d)|quarter|year|季度|年度|年', text, re.I))
            conditional = bool(re.search(r'\b(?:if|unless|subject to|provided|until|before|after|may|risk|decline)\b|如果|除非|须|条件|截至|之前|之后|风险|下滑', text, re.I))
            direct = source.kind in ('official', 'market_data')
            specificity = int(numeric and period) + int(conditional) + int(direct)
            overlap = len(item_terms & query_terms)
            novel = min(len(item_terms - seen_terms), 24)
            return ((specificity, int(overlap > 0), novel, -rank[item]) if selected else
                    (int(overlap > 0), specificity, -requirement_rank.get(item, len(ordered)), -rank[item]))
        key = max(possible, key=selection_key)
        pick(key, 'requirement_retrieval' if rid in candidates[key]['requirement_ids'] else 'diverse_candidate')
    for key in ordered:
        if len(selected) >= req.max_hits:
            break
        if key not in selected and eligible(key) and body(key) not in selected_text:
            pick(key, 'query_retrieval')
    selection_diagnostics = None
    if selection_mode in ('combination', 'anchored'):
        from .research_evidence_selection import select_evidence_candidates
        eligible_keys = [key for key in ordered if eligible(key) and body(key)]
        pool = []
        for rank, key in enumerate(eligible_keys):
            provenance = source_by_id[key[0]].provenance
            row = {'source_id': key[0], 'start': key[1], 'end': key[2], 'text': body(key), 'rank': rank, 'header': candidates[key]['hit'].context}
            for field in ('entity', 'currency', 'period_scope'):
                value = provenance.get(field)
                if isinstance(value, str) and value.strip():
                    row[field] = value.strip()
            pool.append(row)
        indices, selection_diagnostics = select_evidence_candidates(
            pool, active, max_hits=req.max_hits, max_bytes=selection_max_bytes,
            baseline_indices=[eligible_keys.index(key) for key in selected if key in eligible_keys] if selection_mode == 'anchored' else None)
        selected = [eligible_keys[i] for i in indices]
        selected_text = {body(key) for key in selected}
        reasons = {key: 'combination_retrieval' for key in selected}
    elif selection_mode != 'baseline':
        raise ValueError('unknown selection mode')
    trace: list[dict[str, Any]] = []
    for key in ordered:
        hit = candidates[key]['hit']
        if key in reasons:
            decision, reason = 'selected', reasons[key]
        elif not eligible(key):
            decision, reason = 'omitted', 'source_ineligible'
        elif body(key) and body(key) in selected_text:
            decision, reason = 'omitted', 'identical_passage'
        else:
            decision, reason = 'omitted', 'capacity'
        trace.append({'source_id': hit.source_id, 'start': hit.char_start, 'end': hit.char_end,
                      'requirement_ids': candidates[key]['requirement_ids'], 'decision': decision, 'reason': reason})
    rows = []
    for key in selected:
        entry = candidates[key]
        row = hit_row(entry['hit'], source_by_id.get(key[0]), needs_check)
        if active:
            row['requirement_ids'] = entry['requirement_ids']
            row['requirement_match_status'] = 'retrieval_only_unverified'
        rows.append(row)
    coverage = compact_coverage(primary.coverage)
    omitted = Counter(item['reason'] for item in trace if item['decision'] == 'omitted')
    result: dict[str, Any] = {'status': 'completed', 'query': req.query, 'returned_hits': len(rows), 'hits': rows,
                              'library_coverage': coverage, 'selection_trace': trace,
                              'selection_summary': {'candidates': len(trace), 'selected': len(selected),
                                                    'omitted_by_reason': dict(omitted),
                                                    'requirements_searched': len(active)},
                              'requirement_queries': [{'requirement_id': r['id'], 'query': requirement_search_query(r),
                                                       'reason': 'unmet_or_requested_requirement'} for r in active] if expand_requirements else []}
    if cue_queries:
        result['read_cue_queries'] = [cue for cue, _ in cue_queries]
    if selection_diagnostics is not None:
        result['combination_diagnostics'] = selection_diagnostics
        found = {role for i in indices for role in selection_diagnostics['uses'][i]}
        missing = sorted({role for group in selection_diagnostics['needed_uses'] for role in group} - found)
        if missing:
            result['unverified_missing_uses'] = missing
            result['next_step'] = ('Read the selected locations and search authorized local sources for the missing uses. '
                                   'If permitted and available, use existing discovery or acquisition tools; '
                                   'an absent search result does not establish nondisclosure.')

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
        notes.append('Hits and requirement associations are retrieval leads, not verified support; read the location with read_sources or read_pages before citing. Scores from different queries are not directly comparable.')
    if notes:
        result['message'] = ' '.join(notes)
    summary = {'kind': 'search_library', 'query': req.query[:120], 'hit_count': len(rows),
               'source_count': coverage['sources_searched'], 'unsearched_pages': int(coverage['unsearched_page_count'] or 0),
               'complete': coverage['complete'],
               'hits_preview': [{'title': str(r['title'])[:160], 'page': r['page'], 'source_id': r['source_id']} for r in rows[:5]]}
    if isinstance(coverage.get('retrieval_mode'), str):
        summary['retrieval_mode'] = coverage['retrieval_mode']
    if type(coverage.get('embedding_cache_hits')) is int:
        summary['embedding_cache_hits'] = coverage['embedding_cache_hits']
    if type(coverage.get('embedding_cache_misses')) is int:
        summary['embedding_cache_misses'] = coverage['embedding_cache_misses']
    return result, summary
