"""Deterministic run insights wired into the research runtime: reconciliation (I1), coverage map (I4), method packs (I5).

Pure functions over the runtime ``state`` and the run's sources.  They never call a model or the network, never
change a recorded fact, and surface bounded, content-free degraded diagnostics on internal errors rather than
failing a run.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import logging
from typing import Any

from .model_protocol import canonical_json_dumps
from .news.clustering import ClusterInput, cluster_items
from .research_coverage_map import (
    build_coverage_map,
    model_summary as coverage_model_summary_fn,
    ui_view as coverage_ui_view,
)
from .research_diagnostics import report_diagnostic_write_failure, safe_exception_diagnostic
from .research_methods import (
    load_packs,
    pack_fingerprint,
    render_for_prompt,
    resolve_active_packs,
    select_packs,
    tool_available_in_run,
)
from .research_reconciliation import (classify_source, model_summary as reconciliation_model_summary, reconcile,
                                      suggested_gaps as reconciliation_gaps, ui_view as reconciliation_ui_view)

MAX_CLUSTERED_SOURCES = 60
CLUSTER_TEXT_CHARS = 20_000
PROVENANCE_DROP = frozenset({'document_pages'})
VIEW_VERSION_KEY = 'version'
_logger = logging.getLogger(__name__)


def _degraded_diagnostic(operation: str, error: BaseException) -> dict[str, Any]:
    """Build and log a bounded, content-free diagnostic for a degraded insight helper."""
    diagnostic: dict[str, Any] = {
        'operation': operation,
        **safe_exception_diagnostic(error),
    }
    try:
        _logger.warning('research_insight_degraded metadata=%s', diagnostic)
    except Exception as log_error:
        report_diagnostic_write_failure(log_error)
    return diagnostic


def _safe_preserved_diagnostic(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    preserved: dict[str, Any] = {}
    for key in ('operation', 'exception_class'):
        val = raw.get(key)
        if isinstance(val, str) and val.isidentifier() and len(val) <= 128:
            preserved[key] = val
    loc = raw.get('location')
    if isinstance(loc, Mapping) and isinstance(loc.get('file'), str) and type(loc.get('line')) is int and loc['line'] >= 0:
        if loc['file'].endswith('.py') and '/' not in loc['file'] and '\\' not in loc['file'] and len(loc['file']) <= 128:
            preserved['location'] = {'file': loc['file'], 'line': loc['line']}
    return preserved or None


def _degraded_payload(operation: str, error: BaseException, *, versioned: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'status': 'degraded',
        'diagnostic': _degraded_diagnostic(operation, error),
    }
    if versioned:
        payload[VIEW_VERSION_KEY] = 1
    return payload


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
    except Exception as error:   # an analysis aid must never fail a research run
        return _degraded_payload('reconcile_run', error)


def reconciliation_for_model(report: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The bounded summary appended to a ``record_facts`` result; ``None`` when nothing was compared."""
    if not report:
        return None
    try:
        if report.get('status') == 'degraded' or not report.get('rows'):
            return None
        return reconciliation_model_summary(report)['reconciliation']
    except Exception as error:
        _degraded_diagnostic('reconciliation_for_model', error)
        return None


def reconciliation_view(report: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not report:
        return None
    try:
        if report.get('status') == 'degraded':
            return {
                VIEW_VERSION_KEY: 1,
                'status': 'degraded',
                'diagnostic': _safe_preserved_diagnostic(report.get('diagnostic')),
            }
        return reconciliation_ui_view(report) if report.get('rows') else None
    except Exception as error:
        return _degraded_payload('reconciliation_view', error, versioned=True)


def reconciliation_gap_rows(report: Mapping[str, Any] | None, *, limit: int = 5) -> list[dict[str, str]]:
    """Gap rows (stable ``code`` + English message) for discrepancies, material first."""
    if not report:
        return []
    try:
        if report.get('status') == 'degraded':
            return []
        return [{'code': g['code'], 'message': g['message']} for g in reconciliation_gaps(report, limit=limit)]
    except Exception as error:
        _degraded_diagnostic('reconciliation_gap_rows', error)
        return []


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
    except Exception as error:
        return _degraded_payload('run_coverage_map', error)


def coverage_view(coverage: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not coverage:
        return None
    try:
        if coverage.get('status') == 'degraded':
            return {
                VIEW_VERSION_KEY: 1,
                'status': 'degraded',
                'diagnostic': _safe_preserved_diagnostic(coverage.get('diagnostic')),
            }
        return coverage_ui_view(coverage)
    except Exception as error:
        return _degraded_payload('coverage_view', error, versioned=True)


# --------------------------------------------------------------------------------------------- method packs

def method_pack_texts(question: str, available_capabilities: Mapping[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """Rendered pack text for the first user message and the records (id, version, fingerprint) kept with the run."""
    try:
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
    except Exception as error:
        _degraded_diagnostic('method_pack_texts', error)
        return [], []


def method_pack_records(question: str) -> list[dict[str, Any]]:
    """Which packs a question selects (for run-input records written before the run starts)."""
    return method_pack_texts(question, {})[1]


# --------------------------------------------------------------------------------------------- live attention & gate

WORKING_MEMORY_MAX_CHARS = 700

_OK_TOOL_EVENT_STATUSES = frozenset({'ok', 'completed', 'partial', 'acquired', 'created', 'reused'})
_FAIL_TOOL_EVENT_STATUSES = frozenset({
    'failed', 'unavailable', 'denied', 'denied_or_unavailable', 'restricted', 'empty', 'unknown', 'needs_ocr',
})
_SEARCH_TOOLS = frozenset({'news_search', 'search_library', 'search_source'})

_REQ_ACK_KEYWORDS: dict[str, tuple[str, ...]] = {
    'entity': ('entity', 'identifier', 'resolve', 'ticker', 'cik', '实体', '识别', '代码'),
    'entities': ('entity', 'entities', 'identifier', 'resolve', 'ticker', 'cik', '实体', '识别', '代码'),
    'statements': ('statement', 'financial', 'xbrl', '10-k', '10-q', 'filing', 'annual', 'interim', 'non-standardized', 'unaudited', '报表', '财报', '年报', '季报', '财务'),
    'aligned_statements': ('statement', 'financial', 'xbrl', '10-k', '10-q', 'filing', 'aligned', 'period', 'non-standardized', '报表', '财报', '财务', '对齐'),
    'filing_text': ('filing', '10-k', '10-q', 'md&a', 'disclosure', 'primary', 'text', 'source', 'document', '公告', '文件', '披露', '原文', '财报'),
    'offer_terms': ('offer', 'terms', 'filing', 'prospectus', 'price', 'ipo', '发行', '招股', '条款', '文件'),
    'official_release': ('official', 'release', 'agency', 'central bank', 'primary', 'source', '官方', '统计', '央行', '来源'),
    'revision': ('revision', 'revised', 'prior', 'definition', 'headline', 'core', '修订', '修正', '口径'),
    'derived_ratios': ('ratio', 'calculat', 'margin', 'growth', 'return', 'unaudited', 'derived', '计算', '比率', '利润率', '增长'),
    'prior_compare': ('compare', 'prior', 'calculat', 'change', 'margin', 'growth', 'unaudited', '对比', '比较', '计算', '变化'),
    'calc': ('calculat', 'change', 'annualized', 'unaudited', 'formula', '计算', '公式', '年化'),
    'returns': ('return', 'drawdown', 'volatility', 'calculat', 'unaudited', '收益', '回撤', '波动', '计算'),
    'price_series': ('price', 'market data', 'market-data', 'market_data', 'bars', 'series', 'key', '行情', '股价', '价格', '指数'),
    'first_trade': ('trade', 'open', 'high', 'low', 'close', 'volume', 'market data', 'market-data', 'market_data', 'price', '首日', '开盘', '收盘', '行情', '价格'),
    'benchmark': ('benchmark', 'index', 'etf', 'market data', 'market-data', 'market_data', '基准', '指数', '大盘'),
    'drivers': ('driver', 'news', 'catalyst', 'macro', 'headline', '驱动', '新闻', '催化'),
}
_GAP_ACK_CUES = (
    'gap', 'missing', 'unavail', 'not ', 'no ', 'without', 'blocked', 'unable', 'lack', 'skip',
    'omitted', 'cannot', 'could not', 'fail', 'limit', 'assumed', 'unverified', 'secondary',
    'not market data', 'key', '缺', '未', '无', '不可用', '限制', '受限', '无法', '未能', '假设', '二手',
)
_CONFLICT_CUES = (
    'discrepanc', 'conflict', 'reconcil', 'disagree', 'differ', 'inconsisten', 'versus', 'mismatch',
    '差异', '不一致', '分歧', '矛盾', '冲突',
)
_COUNTER_ACK_CUES = (
    'counter', 'contrary', 'oppos', 'alternative', 'disagree', 'no contrary', 'not found',
    '反证', '反方', '反向', '相反', '未发现',
)


def _user_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    raw = state.get('user')
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw.strip().startswith('{'):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def coverage_model_summary(
    state: Mapping[str, Any],
    source_by_id: Mapping[str, Any],
    *,
    library_coverage: Mapping[str, Any] | None = None,
    gaps: Sequence[Mapping[str, Any]] | None = None,
    max_statements: int = 5,
) -> dict[str, Any] | None:
    """Safely build ``research_coverage_map.model_summary()`` from current runtime state."""
    try:
        eval_state = state
        raw_events = state.get('events')
        if isinstance(raw_events, list) and any(
            isinstance(ev, Mapping) and ev.get('kind') == 'model_failure' and ev.get('external_state') == 'not_started'
            for ev in raw_events
        ):
            eval_state = dict(state)
            eval_state['events'] = [
                ev for ev in raw_events
                if not (
                    isinstance(ev, Mapping)
                    and ev.get('kind') == 'model_failure'
                    and ev.get('external_state') == 'not_started'
                )
            ]
        coverage = run_coverage_map(eval_state, source_by_id, library_coverage=library_coverage, gaps=gaps)
        if not coverage or coverage.get('status') == 'degraded':
            return None
        return coverage_model_summary_fn(coverage, max_statements=max_statements)
    except Exception as error:
        _degraded_diagnostic('coverage_model_summary', error)
        return None


def _tool_event_stats(state: Mapping[str, Any]) -> tuple[dict[str, int], dict[str, int], int]:
    ok_counts: dict[str, int] = {}
    fail_counts: dict[str, int] = {}
    search_actions = 0
    for ev in state.get('events') or ():
        if not isinstance(ev, Mapping) or ev.get('kind') != 'tool':
            continue
        if ev.get('duplicate'):
            continue
        name = ev.get('name') or ev.get('tool')
        status = ev.get('status')
        if not isinstance(name, str) or not name or not isinstance(status, str):
            continue
        if status in _OK_TOOL_EVENT_STATUSES:
            ok_counts[name] = ok_counts.get(name, 0) + 1
            if name in _SEARCH_TOOLS:
                search_actions += 1
        elif status in _FAIL_TOOL_EVENT_STATUSES:
            fail_counts[name] = fail_counts.get(name, 0) + 1
    return ok_counts, fail_counts, search_actions


def _requirement_satisfied(
    req_id: str,
    preferred_tools: Sequence[str],
    *,
    pack_id: str,
    ok_counts: Mapping[str, int],
    state: Mapping[str, Any],
    source_by_id: Mapping[str, Any],
    all_facts: Sequence[Mapping[str, Any]],
    all_calcs: Sequence[Mapping[str, Any]],
    all_reads: Sequence[Mapping[str, Any]],
) -> bool:
    if not preferred_tools:
        return True
    tools = list(preferred_tools)
    if 'calculate_financials' in tools:
        return len(all_calcs) > 0 or ok_counts.get('calculate_financials', 0) > 0
    if tools == ['resolve_entity']:
        entities = state.get('entity_candidates')
        return (isinstance(entities, Mapping) and len(entities) > 0) or ok_counts.get('resolve_entity', 0) > 0

    source_methods = {getattr(s, 'retrieval_method', None) for s in source_by_id.values() if s is not None}
    source_kinds = {getattr(s, 'kind', None) for s in source_by_id.values() if s is not None}
    market_src_count = sum(
        1
        for s in source_by_id.values()
        if s is not None and (
            getattr(s, 'kind', None) == 'market_data'
            or getattr(s, 'retrieval_method', None) in ('market_data', 'historical_prices')
        )
    )
    if tools == ['market_data']:
        if pack_id == 'ipo_or_major_event_day' and req_id == 'benchmark':
            return market_src_count >= 2 or ok_counts.get('market_data', 0) >= 2
        return market_src_count >= 1 or ok_counts.get('market_data', 0) >= 1

    if any(ok_counts.get(t, 0) > 0 for t in tools):
        return True
    if 'market_data' in tools and market_src_count >= 1:
        return True
    if 'company_financials' in tools and bool(source_methods & {'sec_companyconcept', 'sec_companyfacts', 'structured_financials'}):
        return True
    if 'filings' in tools and ('sec_filing_html' in source_methods or 'official_html' in source_methods):
        return True
    if 'acquire_sources' in tools and bool(state.get('acquired_source_ids')):
        return True
    if ('read_pages' in tools or 'search_source' in tools) and ok_counts.get('read_sources', 0) > 0 and len(all_reads) > 0 and len(all_facts) > 0:
        return True
    if req_id == 'official_release' and len(all_reads) > 0 and len(all_facts) > 0 and ('official' in source_kinds or 'user_supplied' in source_kinds):
        return True
    return False


def _requirement_acknowledged(req_id: str, preferred_tools: Sequence[str], lim_text: str, *, fail_counts: Mapping[str, int]) -> bool:
    if not lim_text:
        return False
    if req_id.lower() in lim_text or req_id.replace('_', ' ').lower() in lim_text:
        return True
    has_gap_cue = any(cue in lim_text for cue in _GAP_ACK_CUES)
    if not has_gap_cue:
        return False
    keywords = _REQ_ACK_KEYWORDS.get(req_id, ())
    if any(kw in lim_text for kw in keywords):
        return True
    if preferred_tools and any(t.replace('_', ' ') in lim_text or t in lim_text for t in preferred_tools):
        return True
    if preferred_tools and all(fail_counts.get(t, 0) > 0 for t in preferred_tools):
        return True
    return False


def _conflict_addressed(
    row: Mapping[str, Any],
    *,
    lim_text: str,
    report_text: str,
    claims: Sequence[Any],
) -> bool:
    metric = str(row.get('metric') or '').lower()
    metric_words = [w for w in (metric, metric.replace('_', ' ')) if w]
    entity = str(row.get('entity_hint') or '').lower()
    values = [v for v in (row.get('values') or ()) if isinstance(v, Mapping)]
    value_tokens = [
        str(v.get('value_as_stated') or v.get('value') or '').strip().lower()
        for v in values
        if str(v.get('value_as_stated') or v.get('value') or '').strip()
    ]
    row_sources = {v.get('source_id') for v in values if isinstance(v.get('source_id'), str) and v.get('source_id')}

    if lim_text and any(cue in lim_text for cue in _CONFLICT_CUES):
        if any(tok in lim_text for tok in (*metric_words, entity, *value_tokens) if tok) or len(value_tokens) == 0:
            return True
    if lim_text and sum(1 for tok in dict.fromkeys(value_tokens) if tok in lim_text) >= 2:
        return True

    if report_text and any(cue in report_text for cue in _CONFLICT_CUES):
        distinct_values_mentioned = sum(1 for tok in dict.fromkeys(value_tokens) if tok in report_text)
        if distinct_values_mentioned >= 2:
            return True
        if any(w in report_text for w in metric_words if w) and (not entity or entity in report_text or distinct_values_mentioned >= 1):
            return True

    for claim in claims:
        role = claim.get('role') if isinstance(claim, Mapping) else getattr(claim, 'role', None)
        sid = claim.get('source_id') if isinstance(claim, Mapping) else getattr(claim, 'source_id', None)
        if role == 'counterargument' and sid in row_sources:
            return True
    return False


def evaluate_method_and_reconciliation(
    state: Mapping[str, Any],
    source_by_id: Mapping[str, Any],
    *,
    report: str = '',
    claims: Sequence[Any] = (),
    limitations: Sequence[str] = (),
    has_fin_data: bool | None = None,
    reconciliation_report: Mapping[str, Any] | None = None,
    coverage_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate active method packs, counter-evidence, reconciliation conflicts, and runtime counts."""
    try:
        user_payload = _user_payload(state)
        caps = user_payload.get('available_capabilities') if isinstance(user_payload.get('available_capabilities'), Mapping) else {}
        packs = resolve_active_packs(state.get('method_packs'))
        ok_counts, fail_counts, search_actions = _tool_event_stats(state)
        all_facts = [f for f in [*(state.get('prior_facts') or []), *(state.get('facts') or [])] if isinstance(f, Mapping)]
        all_calcs = [c for c in [*(state.get('prior_calculations') or []), *(state.get('calculations') or [])] if isinstance(c, Mapping)]
        all_charts = [c for c in [*(state.get('prior_chart_ids') or []), *(state.get('charts') or [])]]
        all_reads = [r for r in [*(state.get('prior_read_ranges') or []), *(state.get('read_ranges') or [])] if isinstance(r, Mapping)]

        has_eligible_sources = len(source_by_id) > 0
        can_acquire_more = any(
            tool_available_in_run(t, caps, has_fin_data=has_fin_data, has_eligible_sources=has_eligible_sources)
            for t in ('acquire_sources', 'company_financials', 'filings', 'market_data', 'issuer_documents')
        )
        lim_text = ' '.join(str(item) for item in limitations if isinstance(item, str)).lower()
        report_text = str(report or '').lower()

        required_checks: list[dict[str, Any]] = []
        min_counter_actions = 0
        counter_look_for: list[str] = []
        for pack in packs:
            min_counter_actions = max(min_counter_actions, int(pack.counter_evidence.min_search_actions))
            for look in pack.counter_evidence.look_for:
                if look not in counter_look_for:
                    counter_look_for.append(look)
            for req in pack.evidence_requirements:
                if not req.required:
                    continue
                satisfied = _requirement_satisfied(
                    req.id,
                    req.preferred_tools,
                    pack_id=pack.id,
                    ok_counts=ok_counts,
                    state=state,
                    source_by_id=source_by_id,
                    all_facts=all_facts,
                    all_calcs=all_calcs,
                    all_reads=all_reads,
                )
                acknowledged = _requirement_acknowledged(req.id, req.preferred_tools, lim_text, fail_counts=fail_counts)
                if not req.preferred_tools:
                    satisfiable = True
                else:
                    avail_tools = [
                        t for t in req.preferred_tools
                        if tool_available_in_run(t, caps, has_fin_data=has_fin_data, has_eligible_sources=has_eligible_sources)
                    ]
                    if req.preferred_tools == ['calculate_financials']:
                        satisfiable = bool(avail_tools) and (len(all_facts) >= 2 or has_eligible_sources or can_acquire_more)
                    else:
                        # If every available preferred tool has already been tried and failed, do not nudge to retry it.
                        non_failed_avail = [t for t in avail_tools if fail_counts.get(t, 0) == 0 or ok_counts.get(t, 0) > 0]
                        satisfiable = bool(non_failed_avail)
                required_checks.append({
                    'pack_id': pack.id,
                    'id': req.id,
                    'description': req.description,
                    'preferred_tools': list(req.preferred_tools),
                    'fallback': req.fallback,
                    'satisfied': satisfied,
                    'acknowledged': acknowledged,
                    'satisfiable': satisfiable,
                })

        counter_claims = sum(
            1
            for claim in claims
            if (claim.get('role') if isinstance(claim, Mapping) else getattr(claim, 'role', None)) == 'counterargument'
        )
        counter_completed = search_actions + counter_claims
        counter_satisfied = counter_completed >= min_counter_actions if min_counter_actions > 0 else True
        counter_ack = bool(lim_text and any(cue in lim_text for cue in _COUNTER_ACK_CUES))
        search_tools_available = any(
            tool_available_in_run(t, caps, has_fin_data=has_fin_data, has_eligible_sources=has_eligible_sources)
            for t in _SEARCH_TOOLS
        )
        counter_satisfiable = min_counter_actions > 0 and search_tools_available

        rec_report = reconciliation_report if reconciliation_report is not None else reconcile_run(state, source_by_id)
        material_conflicts: list[dict[str, Any]] = []
        if isinstance(rec_report, Mapping) and rec_report.get('status') != 'degraded':
            for row in rec_report.get('rows') or ():
                if not isinstance(row, Mapping):
                    continue
                if row.get('status') == 'differs' and row.get('severity') == 'material':
                    addressed = _conflict_addressed(row, lim_text=lim_text, report_text=report_text, claims=claims)
                    material_conflicts.append({
                        'row_id': row.get('row_id'),
                        'entity': row.get('entity_hint'),
                        'metric': row.get('metric'),
                        'period': row.get('period'),
                        'values': [
                            {
                                'source_id': v.get('source_id'),
                                'source_class': v.get('source_class'),
                                'value': v.get('value_as_stated') or v.get('value'),
                                'currency': v.get('currency'),
                            }
                            for v in (row.get('values') or ())
                            if isinstance(v, Mapping)
                        ],
                        'reference': (row.get('reference_value') or {}).get('value') if isinstance(row.get('reference_value'), Mapping) else None,
                        'addressed': addressed,
                    })

        open_gaps: list[str] = []
        for g in reconciliation_gap_rows(rec_report, limit=5):
            label = g['code']
            if label not in open_gaps:
                open_gaps.append(label)
        cov_section = coverage_summary.get('coverage') if isinstance(coverage_summary, Mapping) else None
        if isinstance(cov_section, Mapping):
            for cat in cov_section.get('categories') or ():
                if isinstance(cat, Mapping) and isinstance(cat.get('failures'), Mapping):
                    for code in sorted(cat['failures']):
                        if code not in open_gaps:
                            open_gaps.append(code)
            for stmt in cov_section.get('not_established') or ():
                if isinstance(stmt, str) and 'unsearched' in stmt.lower() and 'unsearched_pages' not in open_gaps:
                    open_gaps.append('unsearched_pages')

        return {
            'active_packs': [p.id for p in packs],
            'required_checks': required_checks,
            'counter_evidence': {
                'required_actions': min_counter_actions,
                'completed_actions': counter_completed,
                'satisfied': counter_satisfied,
                'acknowledged': counter_ack,
                'satisfiable': counter_satisfiable,
                'look_for': counter_look_for[:4],
            },
            'material_conflicts': material_conflicts,
            'facts_count': len(all_facts),
            'calcs_count': len(all_calcs),
            'charts_count': len(all_charts),
            'sources_count': len(source_by_id),
            'open_gaps': open_gaps[:8],
        }
    except Exception as error:
        _degraded_diagnostic('evaluate_method_and_reconciliation', error)
        return {
            'active_packs': [],
            'required_checks': [],
            'counter_evidence': {'required_actions': 0, 'completed_actions': 0, 'satisfied': True, 'acknowledged': False, 'satisfiable': False, 'look_for': []},
            'material_conflicts': [],
            'facts_count': len(state.get('facts') or []) if isinstance(state, Mapping) and isinstance(state.get('facts'), list) else 0,
            'calcs_count': len(state.get('calculations') or []) if isinstance(state, Mapping) and isinstance(state.get('calculations'), list) else 0,
            'charts_count': 0,
            'sources_count': len(source_by_id) if isinstance(source_by_id, Mapping) else 0,
            'open_gaps': [],
        }


def evaluate_presubmit_gate(
    state: Mapping[str, Any],
    source_by_id: Mapping[str, Any],
    *,
    report: str = '',
    claims: Sequence[Any] = (),
    limitations: Sequence[str] = (),
    has_fin_data: bool | None = None,
    reconciliation_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Check whether ``submit_research`` has unmet required checks, counter-evidence, or material conflicts."""
    ev = evaluate_method_and_reconciliation(
        state,
        source_by_id,
        report=report,
        claims=claims,
        limitations=limitations,
        has_fin_data=has_fin_data,
        reconciliation_report=reconciliation_report,
    )
    missing_items: list[dict[str, Any]] = []
    for chk in ev['required_checks']:
        if chk['satisfied'] or chk['acknowledged'] or not chk['satisfiable']:
            continue
        missing_items.append({
            'kind': 'required_evidence',
            'pack_id': chk['pack_id'],
            'id': chk['id'],
            'description': chk['description'],
            'suggested_tools': chk['preferred_tools'],
            'fallback': chk['fallback'],
        })
    ce = ev['counter_evidence']
    if ce['required_actions'] > 0 and not ce['satisfied'] and not ce['acknowledged'] and ce['satisfiable']:
        missing_items.append({
            'kind': 'counter_evidence',
            'required_actions': ce['required_actions'],
            'completed_actions': ce['completed_actions'],
            'look_for': ce['look_for'],
        })
    unaddressed_conflicts = [c for c in ev['material_conflicts'] if not c['addressed']]
    return {
        'needs_attention': bool(missing_items or unaddressed_conflicts),
        'missing_items': missing_items,
        'unaddressed_conflicts': unaddressed_conflicts,
    }


def build_working_memory_anchor(
    state: Mapping[str, Any],
    source_by_id: Mapping[str, Any],
    *,
    evaluation: Mapping[str, Any] | None = None,
    coverage_summary: Mapping[str, Any] | None = None,
    max_chars: int = WORKING_MEMORY_MAX_CHARS,
) -> str:
    """Build a compact, deterministic runtime working-memory anchor bounded to ``<= 700`` chars."""
    limit = max(64, int(max_chars))
    try:
        ev = dict(evaluation) if evaluation is not None else evaluate_method_and_reconciliation(
            state, source_by_id, coverage_summary=coverage_summary,
        )
        lines: list[str] = [
            f"State: facts={ev['facts_count']} calcs={ev['calcs_count']} charts={ev['charts_count']} sources={ev['sources_count']}"
        ]
        if ev['active_packs']:
            checks = []
            for chk in ev['required_checks']:
                tag = 'ok' if chk['satisfied'] else ('n/a' if not chk['satisfiable'] else 'missing')
                checks.append(f"{chk['id']}:{tag}")
            packs_label = ','.join(ev['active_packs'])
            checks_str = ', '.join(checks)
            lines.append(f"MethodChecks({packs_label}): {checks_str}")
        ce = ev['counter_evidence']
        if ce['required_actions'] > 0 or ce['completed_actions'] > 0:
            ce_status = 'ok' if ce['satisfied'] else ('n/a' if not ce['satisfiable'] else 'needed')
            lines.append(f"CounterEvidence: {ce_status}({ce['completed_actions']}/{ce['required_actions']})")
        conflicts = ev['material_conflicts']
        if conflicts:
            metrics = sorted({f"{c.get('entity') or '?'}:{c.get('metric') or '?'}" for c in conflicts})
            lines.append(f"MaterialConflicts: {','.join(metrics[:4])}")
        if ev['open_gaps']:
            lines.append(f"OpenGaps: {','.join(ev['open_gaps'][:6])}")
        text = '\n'.join(lines)
        if len(text) <= limit:
            return text
        if len(lines) > 1:
            trimmed = list(lines)
            while len('\n'.join(trimmed)) > limit and len(trimmed) > 1:
                last = trimmed[-1]
                colon_idx = last.find(':')
                comma_idx = last.rfind(',')
                if comma_idx > colon_idx >= 0:
                    trimmed[-1] = last[:comma_idx].rstrip()
                else:
                    trimmed.pop()
            text = '\n'.join(trimmed)
        if len(text) <= limit:
            return text
        return text[:limit - 3].rstrip() + '...'
    except Exception as error:
        _degraded_diagnostic('build_working_memory_anchor', error)
        fallback = f'State: sources={len(source_by_id) if isinstance(source_by_id, Mapping) else 0}'
        return fallback[:limit]


def build_tail_note(
    state: Mapping[str, Any],
    source_by_id: Mapping[str, Any],
    *,
    include_live_attention: bool = False,
    library_coverage: Mapping[str, Any] | None = None,
    failure_payload: tuple[list[dict[str, Any]], str] | None = None,
    convergence_hint: str | None = None,
    has_fin_data: bool | None = None,
    anchor_max_chars: int = WORKING_MEMORY_MAX_CHARS,
) -> str | None:
    """Assemble the ephemeral ``ModelTurnInput.tail_note`` outside any cached prefix."""
    cov_summary: dict[str, Any] | None = None
    anchor: str | None = None
    if include_live_attention or failure_payload is not None:
        cov_summary = coverage_model_summary(state, source_by_id, library_coverage=library_coverage, max_statements=4)
        ev = evaluate_method_and_reconciliation(
            state,
            source_by_id,
            has_fin_data=has_fin_data,
            coverage_summary=cov_summary,
        )
        anchor = build_working_memory_anchor(
            state,
            source_by_id,
            evaluation=ev,
            coverage_summary=cov_summary,
            max_chars=anchor_max_chars,
        )

    if failure_payload is not None:
        recent, guidance = failure_payload
        note_payload: dict[str, Any] = (
            {'prior_failed_attempts': recent, 'recovery_guidance': guidance}
            if recent
            else {'recovery_guidance': guidance}
        )
        if cov_summary is not None and isinstance(cov_summary.get('coverage'), Mapping):
            note_payload['coverage'] = cov_summary['coverage']
        if anchor:
            note_payload['working_memory'] = anchor
        if convergence_hint:
            note_payload['convergence_hint'] = convergence_hint
        return f'[Recovery context: {json.dumps(note_payload, ensure_ascii=False, sort_keys=True)}]'

    if not include_live_attention and not convergence_hint:
        return None

    sections: list[str] = []
    if cov_summary is not None:
        sections.append('[Coverage]\n' + canonical_json_dumps(cov_summary))
    if anchor:
        sections.append('[Working Memory]\n' + anchor)
    if convergence_hint:
        sections.append('[Convergence]\n' + convergence_hint)
    return '\n'.join(sections) if sections else None
