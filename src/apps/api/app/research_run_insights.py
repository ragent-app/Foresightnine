"""Deterministic run insights wired into the research runtime: reconciliation (I1), coverage map (I4), method packs (I5).

Pure functions over the runtime ``state`` and the run's sources.  They never call a model or the network, never
change a recorded fact, and degrade to ``None`` on malformed input rather than failing a run.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .news.clustering import ClusterInput, cluster_items
from .research_coverage_map import build_coverage_map, ui_view as coverage_ui_view
from .research_methods import load_packs, pack_fingerprint, render_for_prompt, select_packs
from .research_reconciliation import (classify_source, model_summary as reconciliation_model_summary, reconcile,
                                      suggested_gaps as reconciliation_gaps, ui_view as reconciliation_ui_view)

MAX_CLUSTERED_SOURCES = 60
CLUSTER_TEXT_CHARS = 20_000
PROVENANCE_DROP = frozenset({'document_pages'})
VIEW_VERSION_KEY = 'version'


# --------------------------------------------------------------------------------------------- reconciliation

def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, 'isoformat') else (str(value) if value else None)


def source_meta(source: Any, cluster_id: str | None = None) -> dict[str, Any]:
    """Metadata-only view of a source for reconciliation (no text, no page maps)."""
    provenance = getattr(source, 'provenance', None) or {}
    perms = getattr(source, 'permissions', None)
    meta: dict[str, Any] = {
        'source_id': source.source_id, 'kind': source.kind, 'retrieval_method': source.retrieval_method,
        'url': str(source.url) if getattr(source, 'url', None) is not None else None, 'title': source.title,
        'published_at': _iso(source.published_at),
        'provenance': {k: v for k, v in provenance.items() if k not in PROVENANCE_DROP} if isinstance(provenance, Mapping) else {},
        'permissions': perms.model_dump() if hasattr(perms, 'model_dump') else None,
    }
    if cluster_id:
        meta['cluster_id'] = cluster_id
    return meta


def news_clusters(source_by_id: Mapping[str, Any], source_ids: Sequence[str],
                  lead_clusters: Mapping[str, str] | None = None) -> dict[str, str]:
    """Syndication cluster per news-class source: body/title/URL clustering of acquired articles, merged with the
    cluster ids the news search already assigned to the leads they were acquired from."""
    lead_clusters = lead_clusters or {}
    items: list[ClusterInput] = []
    for sid in source_ids:
        source = source_by_id.get(sid)
        if source is None or classify_source(source_meta(source)) != 'news':
            continue
        items.append(ClusterInput(id=sid, url=str(source.url) if getattr(source, 'url', None) is not None else sid,
                                  title=source.title, text=source.text[:CLUSTER_TEXT_CHARS], published_at=source.published_at))
        if len(items) >= MAX_CLUSTERED_SOURCES:
            break
    if not items:
        return {}
    result = cluster_items(items)
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for item in items:
        body = 'body:' + result.item_cluster[item.id]
        find(body)
        lead = lead_clusters.get(item.id)
        if lead:
            parent[find('lead:' + lead)] = find(body)
    return {item.id: find('body:' + result.item_cluster[item.id]).split(':', 1)[1] for item in items}


def entity_context(state: Mapping[str, Any]) -> tuple[str | None, dict[str, str]]:
    """``(default_entity, aliases)`` from the entities resolved in this run.  A default entity is only given when
    exactly one entity was resolved; company-name aliases are safe for any number of entities."""
    candidates = state.get('entity_candidates') if isinstance(state.get('entity_candidates'), Mapping) else {}
    aliases: dict[str, str] = {}
    tokens: list[str] = []
    for info in candidates.values():
        if not isinstance(info, Mapping):
            continue
        symbol = info.get('symbol') if isinstance(info.get('symbol'), str) and info['symbol'].strip() else None
        company = info.get('company') if isinstance(info.get('company'), str) and info['company'].strip() else None
        token = (symbol or company or '').strip().upper() or None
        if token:
            tokens.append(token)
        if symbol and company and company.strip().upper() != symbol.strip().upper():
            aliases[company.strip().upper()] = symbol.strip().upper()
    default = tokens[0] if len(candidates) == 1 and len(tokens) == 1 else None
    return default, aliases


def reconcile_run(state: Mapping[str, Any], source_by_id: Mapping[str, Any]) -> dict[str, Any] | None:
    """Reconcile all facts of the run (and the prior turns' facts when present).  ``None`` when there are no facts."""
    try:
        facts: dict[str, Mapping[str, Any]] = {}
        for fact in [*(state.get('prior_facts') or []), *(state.get('facts') or [])]:
            if isinstance(fact, Mapping) and isinstance(fact.get('fact_id'), str):
                facts.setdefault(fact['fact_id'], fact)
        if not facts:
            return None
        ids = list(dict.fromkeys(f.get('source_id') for f in facts.values() if isinstance(f.get('source_id'), str)))
        lead_clusters = state.get('source_clusters') if isinstance(state.get('source_clusters'), Mapping) else {}
        clusters = news_clusters(source_by_id, ids, lead_clusters)
        metas = [source_meta(source_by_id[sid], clusters.get(sid)) for sid in ids if sid in source_by_id]
        default_entity, aliases = entity_context(state)
        return reconcile(list(facts.values()), metas, calculations=[c for c in state.get('calculations') or [] if isinstance(c, Mapping)],
                         cluster_of=lambda src: src.get('cluster_id'), entity_aliases=aliases, default_entity=default_entity)
    except Exception:   # an analysis aid must never fail a research run
        return None


def reconciliation_for_model(report: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The bounded summary appended to a ``record_facts`` result; ``None`` when nothing was compared."""
    if not report or not report.get('rows'):
        return None
    return reconciliation_model_summary(report)['reconciliation']


def reconciliation_view(report: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return reconciliation_ui_view(report) if report and report.get('rows') else None


def reconciliation_gap_rows(report: Mapping[str, Any] | None, *, limit: int = 5) -> list[dict[str, str]]:
    """Gap rows (stable ``code`` + English message) for discrepancies, material first."""
    if not report:
        return []
    return [{'code': g['code'], 'message': g['message']} for g in reconciliation_gaps(report, limit=limit)]


# --------------------------------------------------------------------------------------------- coverage map

def coverage_source_record(source: Any) -> dict[str, Any]:
    """Richer source record for the coverage map; ``text`` is referenced, not copied."""
    return {'source_id': source.source_id, 'title': source.title, 'url': str(source.url) if getattr(source, 'url', None) is not None else None,
            'kind': source.kind, 'retrieval_method': source.retrieval_method, 'published_at': _iso(source.published_at),
            'provenance': getattr(source, 'provenance', None) or {}, 'text': source.text}


def run_coverage_map(state: Mapping[str, Any], source_by_id: Mapping[str, Any], *, library_coverage: Mapping[str, Any] | None = None,
                     gaps: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any] | None:
    try:
        records = [coverage_source_record(s) for s in source_by_id.values()]
        facts = [f for f in [*(state.get('prior_facts') or []), *(state.get('facts') or [])] if isinstance(f, Mapping)]
        return build_coverage_map(state, sources=records, search_coverage=library_coverage, facts=facts, gaps=list(gaps or []))
    except Exception:
        return None


def coverage_view(coverage: Mapping[str, Any] | None) -> dict[str, Any] | None:
    try:
        return coverage_ui_view(coverage) if coverage else None
    except Exception:
        return None


# --------------------------------------------------------------------------------------------- method packs

def method_pack_texts(question: str, available_capabilities: Mapping[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """Rendered pack text for the first user message and the records (id, version, fingerprint) kept with the run."""
    selected = select_packs(question or '')
    if not selected:
        return [], []
    packs = load_packs()
    texts: list[str] = []
    records: list[dict[str, Any]] = []
    for item in selected:
        pack = packs[item.pack_id]
        texts.append(render_for_prompt(pack, available_capabilities=available_capabilities))
        records.append({'pack_id': pack.id, 'version': pack.version, 'fingerprint': pack_fingerprint(pack)})
    return texts, records


def method_pack_records(question: str) -> list[dict[str, Any]]:
    """Which packs a question selects (for run-input records written before the run starts)."""
    return method_pack_texts(question, {})[1]
