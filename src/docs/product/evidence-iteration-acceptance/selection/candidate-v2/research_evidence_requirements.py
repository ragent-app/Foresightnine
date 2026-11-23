"""Conservative, deterministic checks for evidence needed by a research question.

These checks establish structural coverage, not semantic truth. Evidence must already
be recorded by the runtime; tool-call counts and model-written limitations are not proof.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
import json
import re
from typing import Any

from .research_source_policy import source_eligibility
from .research_partial_answer import _requested_parts, _part_key

_METRICS = {
    'revenue': ('revenue', 'sales', '营业收入', '营收', '收入'),
    'diluted_eps': ('diluted eps', 'diluted earnings per share', '稀释每股收益', '稀释eps', '稀释 eps'),
    'basic_eps': ('basic eps', 'basic earnings per share', '基本每股收益', '基本eps', '基本 eps'),
    'eps': ('eps', 'earnings per share', '每股收益'),
}
_COMPARE = ('compare', 'comparison', 'growth', 'change', 'versus', ' vs ', '同比', '增长', '变化', '比较', '对比', '两年', '跨期')
_BENCHMARK = ('benchmark', 'outperform', 'underperform', 'excess return', '基准', '跑赢', '跑输', '超额收益', '大盘')
_COUNTER = ('counterargument', 'counter evidence', 'counter-evidence', 'alternative explanation', '反证', '反方', '替代解释')


def _get(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, Mapping) else getattr(item, key, default)


def _metric(text: str) -> str | None:
    t = text.lower()
    for name, words in _METRICS.items():
        if any(word in t for word in words):
            return name
    return None


def _eligible(source: Any, as_of: datetime | None) -> bool:
    if source is None:
        return False
    try:
        return source_eligibility([source], as_of)[0][1] == 'eligible'
    except (AttributeError, TypeError, ValueError):
        return False


def _ref_fact(f: Mapping[str, Any]) -> dict[str, Any]:
    return {'fact_id': f['fact_id']}


def _distinct_facts(facts: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    # Different model labels do not make the same recorded source observation independent.
    seen = set()
    out = []
    for fact in facts:
        key = tuple(str(fact.get(k) or '').casefold() for k in
                    ('source_id', 'character_start', 'character_end', 'period', 'value', 'unit', 'currency'))
        if key not in seen:
            seen.add(key)
            out.append(fact)
    return out


def _period_key(s: str) -> tuple[int, str] | None:
    year = re.search(r'(?<!\d)((?:19|20)\d{2})(?!\d)', s)
    if not year:
        return None
    t = s.lower().replace(' ', '')
    if any(x in t for x in ('ytd', '累计', 'year-to-date', 'nine months', 'ninemonths', '9months')):
        return None  # Cumulative windows need an explicit comparable duration.
    half = re.search(r'(?:h([12])|([12])h)', t)
    if half:
        return int(year.group()), 'h' + next(x for x in half.groups() if x)
    quarter = re.search(r'(?:q([1-4])|([1-4])q|第([一二三四1-4])季度)', t)
    if quarter:
        return int(year.group()), 'q' + next(x for x in quarter.groups() if x)
    if any(x in t for x in ('half', 'h1', 'h2', '半年', '6months', 'sixmonths', 'ytd', '累计')):
        return int(year.group()), 'cumulative'
    if any(x in t for x in ('fy', 'annual', 'year', '年度', '年报', '全年')) or re.fullmatch(r'(?:19|20)\d{2}年?', t):
        return int(year.group()), 'annual'
    return None


def _valid_facts(state: Mapping[str, Any], sources: Mapping[str, Any], as_of: datetime | None) -> list[Mapping[str, Any]]:
    out = []
    hashes = _source_hashes(state)
    for f in [*(state.get('prior_facts') or []), *(state.get('facts') or [])]:
        if not isinstance(f, Mapping) or not f.get('fact_id') or not _eligible(sources.get(f.get('source_id')), as_of):
            continue
        source = sources[f['source_id']]
        if f['source_id'] in hashes and hashes[f['source_id']] != getattr(source, 'content_sha256', None):
            continue
        start, end = f.get('character_start'), f.get('character_end')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source.text):
            continue
        if f.get('quotation') != source.text[start:end]:
            continue
        out.append(f)
    return out


def _source_hashes(state: Mapping[str, Any]) -> dict:
    try:
        user = json.loads(state.get('user') or '{}')
    except (TypeError, ValueError):
        user = {}
    if not isinstance(user, Mapping):
        user = {}
    snapshots = [*(user.get('sources') or []), *(state.get('_sources') or [])]
    hashes = {r.get('source_id'): r.get('content_sha256') for r in snapshots if isinstance(r, Mapping)}
    hashes.update(state.get('source_hashes') or {})
    return hashes


def _read_negative_spans(state: Mapping[str, Any], sources: Mapping[str, Any], as_of: datetime | None):
    """Only inspect explicitly read text bound to the current source version."""
    hashes = _source_hashes(state)
    for read in [*(state.get('read_ranges') or []), *(state.get('prior_read_ranges') or [])]:
        if not isinstance(read, Mapping):
            continue
        sid, start, end = read.get('source_id'), read.get('start'), read.get('end')
        source = sources.get(sid)
        if (not _eligible(source, as_of) or not hashes.get(sid) or hashes.get(sid) != getattr(source, 'content_sha256', None)
                or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source.text)):
            continue
        # Full sentence boundaries are required: a clipped negation is ambiguous.
        text = source.text[start:end]
        for match in re.finditer(r'[^.!?。！？]+[.!?。！？]', text):
            a, b = start + match.start(), start + match.end()
            if (a and source.text[a - 1] not in '.!?。！？\n' and a == start) or (b == end and end < len(source.text) and source.text[end - 1] not in '.!?。！？'):
                continue
            yield source, text[match.start():match.end()].strip(), {'source_id': sid, 'character_start': a, 'character_end': b}


def _unambiguous_subject(text: str, names: set[str]) -> bool:
    """Do not inherit a subject across text naming another possible entity."""
    if len(names) != 1:
        return False
    ignored = {'prior', 'previous', 'current', 'no', 'two', 'the', 'this', 'adjusted', 'benchmark',
               'revenue', 'diluted', 'basic', 'eps', 'usd', 'fy', 'full', 'issuer', 'it'}
    tokens = {m.casefold() for m in re.findall(r'(?<![A-Za-z0-9])(?:[A-Z][a-z]{1,}|[A-Z]{2,})(?![A-Za-z0-9])', text)} - ignored
    return tokens == names


def _fully_read(state: Mapping[str, Any], source: Any) -> bool:
    return any(isinstance(r, Mapping) and r.get('source_id') == source.source_id
               and r.get('start') == 0 and type(r.get('end')) is int and r['end'] >= len(source.text)
               for r in [*(state.get('read_ranges') or []), *(state.get('prior_read_ranges') or [])])


def assess(question: str, state: Mapping[str, Any], sources: Mapping[str, Any], *, as_of: datetime | None = None,
           claims: Sequence[Any] = (), conflicts: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    q = question.strip()
    if not q:
        return []
    lower = q.lower()
    segments = _requested_parts(q)
    def claim_identifies(name: str, claim_text: str, citation: Any) -> bool:
        quote = str(_get(citation, 'quotation', ''))
        if re.search(r'(?<![a-z0-9])' + re.escape(name) + r'(?![a-z0-9])', (claim_text + ' ' + quote).casefold()):
            return True
        source = sources.get(_get(citation, 'source_id'))
        provenance = _get(source, 'provenance', {}) or {}
        # A symbol supplied by a structured filing adapter can establish issuer
        # identity; a caller-supplied title alone is not identity evidence.
        return (_eligible(source, as_of) and _get(source, 'retrieval_method') == 'sec_filing_html'
                and isinstance(provenance, Mapping) and str(provenance.get('symbol') or '').casefold() == name)

    def located(cit: Any) -> bool:
        sid, quote = _get(cit, 'source_id'), _get(cit, 'quotation')
        source = sources.get(sid)
        if not _eligible(source, as_of) or not isinstance(quote, str) or not quote:
            return False
        return any(isinstance(r, Mapping) and r.get('source_id') == sid and type(r.get('start')) is int
                   and type(r.get('end')) is int and quote in source.text[r['start']:r['end']]
                   for r in [*(state.get('prior_read_ranges') or []), *(state.get('read_ranges') or [])])
    facts = _valid_facts(state, sources, as_of)
    by_id = {f['fact_id']: f for f in facts}
    calcs = [c for c in [*(state.get('prior_calculations') or []), *(state.get('calculations') or [])] if isinstance(c, Mapping)]
    parts = [p for p in state.get('question_parts') or [] if isinstance(p, Mapping)]
    rows: list[dict[str, Any]] = []

    def add(kind: str, text: str, status: str, reason: str, refs: list[dict[str, Any]], **extra: Any) -> None:
        stable = sha256((q + '\0' + kind).encode()).hexdigest()[:16]
        rows.append({'id': 'req-' + stable, 'question': text, 'status': status, 'reason': reason,
                     'references': refs, 'core': True, 'kind': kind, **extra})

    metrics = [name for name, words in _METRICS.items() if any(w in lower for w in words)]
    if 'diluted_eps' in metrics or 'basic_eps' in metrics:
        metrics = [x for x in metrics if x != 'eps']
    metric_requests = []
    for fragment in segments:
        named_metrics = [metric for metric in metrics if any(word in fragment.casefold() for word in _METRICS[metric])]
        if not named_metrics and len(metrics) == 1 and any(c in fragment.casefold() for c in _COMPARE):
            named_metrics = metrics  # A later growth/change clause inherits its sole metric.
        metric_requests.extend((metric, fragment) for metric in named_metrics)
    previous_comparison_years: set[int] = set()
    for metric, fragment in metric_requests:
        compare = any(c in fragment.casefold() for c in _COMPARE)
        # Sentence-initial action words and technical metadata are not issuer names.
        name_stop = {'what', 'which', 'how', 'can', 'could', 'will', 'would', 'does', 'did', 'is', 'are', 'compare', 'comparison', 'explain', 'record', 'calculate',
                     'show', 'find', 'describe', 'summarize', 'analyse', 'analyze', 'provide', 'identify',
                     'revenue', 'revenues', 'sales', 'eps', 'diluted', 'basic', 'fy', 'usd', 'eur', 'please', 'both', 'then', 'include',
                     'annual', 'quarterly', 'fiscal', 'year', 'years', 'current', 'previous', 'growth',
                     'evidence', 'source', 'sources', 'total', 'financial', 'results'}
        names = {n.casefold() for n in re.findall(r'(?<![A-Za-z0-9_])[A-Z][A-Za-z]{1,}(?![A-Za-z0-9_])', q)
                 if n.casefold() not in name_stop}
        single = re.match(r'^([A-Z])\s+(?=[一-龥])', q)
        if single:
            names.add(single.group(1).casefold())
        def correct_entity(f: Mapping[str, Any]) -> bool:
            if not names:
                return True
            src = sources.get(f.get('source_id'))
            provenance = getattr(src, 'provenance', None) or {}
            trusted_symbol = provenance.get('symbol') if getattr(src, 'retrieval_method', None) == 'sec_filing_html' else None
            evidence = ' '.join(str(x or '') for x in (f.get('entity'), f.get('entity_id'), f.get('label'), f.get('quotation'), trusted_symbol)).casefold()
            return all(re.search(r'(?<![a-z0-9])' + re.escape(name) + r'(?![a-z0-9])', evidence) for name in names)
        requested_years = {int(y) for y in re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', fragment)}
        if compare:
            if requested_years:
                previous_comparison_years = requested_years if len(requested_years) == 2 else set()
            elif previous_comparison_years and not re.search(r'\b(?:quarter|months?|weeks?)\b|季度|月度|周度', fragment, re.I):
                requested_years = previous_comparison_years
        else:
            previous_comparison_years = set()
        matching = [f for f in facts if _metric(str(f.get('label', ''))) == metric and correct_entity(f)
                    and (not requested_years or (_period_key(str(f.get('period', '')))
                         and _period_key(str(f.get('period', '')))[0] in requested_years))]
        matching = _distinct_facts(matching)
        refs = [_ref_fact(f) for f in matching]
        status, reason = ('partial', 'Recorded evidence exists; comparable periods and the requested calculation still need verification.') if matching else ('missing', 'No eligible recorded fact matches this metric.')
        requested_quarter = re.search(r'(?i)\bq([1-4])\b|第([一二三四1-4])季度', q)
        if requested_quarter:
            quarter = requested_quarter.group(1) or requested_quarter.group(2)
            matching = [f for f in matching if (key := _period_key(str(f.get('period', '')))) and key[1] == 'q' + quarter]
            refs = [_ref_fact(f) for f in matching]
            if not matching:
                status, reason = 'missing', 'No eligible recorded fact matches the requested metric and quarter.'
        if not compare and matching:
            found_years = {period[0] for f in matching if (period := _period_key(str(f.get('period', ''))))}
            if (re.search(r'\b(?:both|two)\b|两(?:个|年|期)', fragment, re.I) and len(found_years) < 2) or not requested_years.issubset(found_years):
                status, reason = 'partial', 'Located facts do not cover all requested years.'
            else:
                status, reason = 'satisfied', 'Eligible, located facts cover the requested metric and years; semantic interpretation remains subject to review.'
        elif compare:
            accepted_calcs = []
            for calc in calcs:
                inputs = calc.get('inputs') or []
                ids = [i.get('fact_id') for i in inputs if isinstance(i, Mapping)]
                pair = [by_id[x] for x in ids if x in by_id]
                if len(pair) != 2 or len(set(ids)) != 2 or any(_metric(str(f.get('label', ''))) != metric or not correct_entity(f) for f in pair):
                    continue
                periods = [_period_key(str(f.get('period', ''))) for f in pair]
                if (not all(periods) or periods[0][1] != periods[1][1] or periods[0][0] <= periods[1][0]
                        or requested_quarter and periods[0][1] != 'q' + quarter):
                    continue
                if requested_years and not requested_years.issubset({p[0] for p in periods}):
                    continue
                if not all(f.get('unit') for f in pair) or pair[0].get('currency') != pair[1].get('currency'):
                    continue
                if pair[0]['unit'] != pair[1]['unit']:
                    from .research_calculations import calculation_result_status
                    if (not calc.get('scale_adjustments') or calculation_result_status(calc) != 'matched' or
                        any(any(inp.get(k) != f.get(k) for k in ('value', 'unit', 'currency', 'period'))
                            for inp, f in zip(inputs, pair))):
                        continue
                # Explicit entity/scope labels must agree; missing entity is not silently inferred across sources.
                def entity_key(f: Mapping[str, Any]) -> str:
                    provenance = getattr(sources[f['source_id']], 'provenance', None) or {}
                    return str(f.get('entity_id') or f.get('entity') or provenance.get('entity_id')
                               or provenance.get('cik') or provenance.get('instrument_id') or '').strip().casefold()
                entities = [entity_key(f) for f in pair]
                if entities[0] != entities[1] or (not entities[0] and pair[0]['source_id'] != pair[1]['source_id']):
                    continue
                # Compare label prefixes as a second guard against different issuers in a shared document.
                def label_scope(f: Mapping[str, Any]) -> str:
                    label = str(f.get('label', '')).lower()
                    for word in _METRICS[metric]:
                        label = label.replace(word, '')
                    return re.sub(r'\b(?:fy)?(?:19|20)\d{2}\b|[^a-z一-龥]+', '', label)
                if label_scope(pair[0]) != label_scope(pair[1]):
                    continue
                if calc.get('input_status') in ('invalid', 'stale', 'missing') or calc.get('period_order_check') == 'not_checked' or calc.get('period_granularity_check') == 'not_checked':
                    continue
                formula = str(calc.get('formula_id') or '')
                if formula not in ('growth_percent', 'subtract', 'difference', 'percentage_point_change'):
                    continue
                from .research_calculations import calculation_result_status
                if (calculation_result_status(dict(calc)) != 'matched'
                        or any(any(inp.get(k) != f.get(k) for k in ('value', 'unit', 'currency', 'period')) for inp, f in zip(inputs, pair))):
                    continue
                accepted_calcs.append((calc, pair))
            wants_rate = bool(re.search(r'growth rate|percent growth|growth percent|增长率|增速|同比增长率', fragment, re.I))
            wants_difference = bool(re.search(r'absolute (?:change|difference)|difference in|每股(?:增加|减少|差额)|绝对(?:变化|差额)|百分点', fragment, re.I))
            selected = [(c, p) for c, p in accepted_calcs if (wants_rate == wants_difference
                        or (c.get('formula_id') == 'growth_percent') == wants_rate)]
            if selected:
                refs = []
                for calc, pair in selected:
                    for ref in [*map(_ref_fact, pair), {'calculation_id': calc.get('calculation_id')}]:
                        if ref not in refs:
                            refs.append(ref)
                has_difference = any(c.get('formula_id') != 'growth_percent' for c, _ in selected)
                has_rate = any(c.get('formula_id') == 'growth_percent' for c, _ in selected)
                if wants_rate and wants_difference and not (has_rate and has_difference):
                    status, reason = 'partial', '已展示可核验的变化；请求的相对增幅和原单位差额尚未全部计算。'
                elif not wants_rate and not wants_difference and not has_difference:
                    status, reason = 'satisfied', '已保存相对变化；原单位差额尚无已保存的可核验计算。'
                else:
                    status, reason = 'satisfied', 'Two ordered, comparable recorded inputs are bound to saved calculations; entity and scope still require semantic review.'
            if status == 'satisfied' and re.search(r'\b(?:sustain(?:able|ability|ed)?|durable|persist(?:ent)?)\b|持续性|可持续|能否持续', fragment, re.I):
                status, reason = 'partial', 'The historical change is calculated; its sustainability needs independent supporting and opposing evidence and review conditions.'
        if not compare and not matching:
            # A located direct quotation may answer a single-period disclosure without manufacturing a fact.
            for claim in claims:
                text = str(_get(claim, 'text', ''))
                citations = _get(claim, 'citations', []) or []
                if (_get(claim, 'role') == 'support' and _metric(text) == metric
                    and all(str(y) in text or any(str(y) in str(_get(c, 'quotation', '')) for c in citations) for y in requested_years)
                    and all(any(claim_identifies(name, text, c) for c in citations) for name in names)
                    and any(located(c) for c in citations) and _get(claim, 'claim_id')):
                    refs = [{'claim_id': _get(claim, 'claim_id')}]
                    status, reason = 'satisfied', 'A located claim addresses the metric and requested period; semantic support needs review.'
                    break
        if status in ('partial', 'missing') and requested_years:
            missing_years = requested_years - {period[0] for f in matching if (period := _period_key(str(f.get('period', ''))))}
            if missing_years:
                label = metric.replace('_', ' ').replace('eps', 'EPS')
                years = ', '.join(str(year) for year in sorted(missing_years))
                localized = {'diluted_eps': '稀释 EPS', 'basic_eps': '基本 EPS', 'eps': 'EPS', 'revenue': '收入'}.get(metric, label)
                reason = (f'缺少 {years} 年同口径的{localized}依据。' if re.search(r'[一-龥]', q)
                          else f'Missing comparable {label} evidence for {years}.')
        asks_disclosure = bool(re.search(r'是否(?:有|已)?披露|(?:did|does|was|is)\b.{0,60}\bdisclos', fragment, re.I))
        if (compare or asks_disclosure) and status in ('partial', 'missing') and len(requested_years) in (1, 2):
            for source, sentence, ref in _read_negative_spans(state, sources, as_of):
                # A relative period is usable only when this same fully read source
                # identifies the requested subject and both comparison years.
                if (not _fully_read(state, source) or not _unambiguous_subject(source.text, names)
                        or not all(str(y) in source.text for y in requested_years)):
                    continue
                clauses = re.split(r'[,，;；]', sentence)
                clause = next((part for part in clauses if _metric(part) == metric
                               and re.search(r'\b(?:not disclosed|not reported|no disclosure)\b|未披露|没有披露', part, re.I)
                               and len([name for name, words in _METRICS.items() if name != 'eps' and any(w in part.casefold() for w in words)]) == 1), None)
                if not clause:
                    continue
                years_in_clause = {int(y) for y in re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', clause)}
                if len(years_in_clause) == 1 and years_in_clause <= requested_years:
                    missing_year = next(iter(years_in_clause))
                elif len(requested_years) == 2 and not years_in_clause and re.search(r'\b(?:prior|previous|preceding)\b|上年|上期', clause, re.I):
                    missing_year = min(requested_years)
                elif len(requested_years) == 2 and not years_in_clause and re.search(r'\bcurrent\b|本年|本期', clause, re.I):
                    missing_year = max(requested_years)
                else:
                    continue
                # Resolve "prior" only from a completely read document, not an unrelated fragment.
                label = {'diluted_eps': '稀释 EPS', 'basic_eps': '基本 EPS', 'eps': 'EPS', 'revenue': '收入'}.get(metric, metric)
                reason = f'已读取的这份材料明确未披露 {missing_year} 年{label}。'
                if compare:
                    reason += '因此无法计算该指标的变化。'
                elif asks_disclosure:
                    status = 'satisfied'
                refs.append(ref)
                break
        relevant_conflicts = [c for c in conflicts if _metric(str(c.get('metric', ''))) == metric
                              and (not requested_years or any(str(y) in str(c.get('period', '')) for y in requested_years))
                              and (not names or str(c.get('entity_hint') or '').casefold() in names)]
        asks_conflict = any(c in lower for c in ('conflict', 'disagree', 'discrepancy', '冲突', '不一致', '分歧'))
        if asks_conflict and not relevant_conflicts:
            status, reason = ('partial' if matching else 'missing'), 'The requested conflict has not been established by comparable independent evidence.'
        if relevant_conflicts:
            # A question asking whether a disagreement exists is answered by the conflict itself.
            status, reason = ('satisfied', 'The requested conflict is established; retain each source and value.') if asks_conflict else ('conflict', 'Material conflicting evidence for this metric remains unresolved.')
            if asks_conflict:
                refs = [{'source_id': v['source_id']} for c in relevant_conflicts for v in c.get('values', [])
                        if isinstance(v, Mapping) and v.get('source_id') in sources and _eligible(sources[v['source_id']], as_of)]
        add('financial:' + metric + ':' + str(segments.index(fragment)), fragment, status, reason, refs, metric=metric)

    if any(x in lower for x in _BENCHMARK):
        from .research_return_context import price_return_context
        from .research_calculations import calculation_result_status
        returns = [c for c in calcs if isinstance(c.get('return_context'), Mapping) and c.get('calculation_id')
                   and price_return_context(c, by_id, sources) == c['return_context']
                   and calculation_result_status(dict(c)) == 'matched'
                   and all(isinstance(inp, Mapping) and inp.get('fact_id') in by_id
                           and all(inp.get(k) == by_id[inp['fact_id']].get(k) for k in ('value', 'unit', 'currency', 'period'))
                           for inp in c.get('inputs', []))]
        valid = None
        for i, left in enumerate(returns):
            for right in returns[i + 1:]:
                a, b = left['return_context'], right['return_context']
                if not a.get('identity_verified') or not b.get('identity_verified') or not a.get('instrument') or not b.get('instrument') or str(a['instrument']).upper() == str(b['instrument']).upper():
                    continue
                if not all(re.search(r'(?<![A-Za-z0-9])' + re.escape(str(x.get('instrument')).split('@')[0]) + r'(?![A-Za-z0-9])', q, re.I) for x in (a, b)):
                    continue
                if not a.get('start_date') or not a.get('end_date') or a['start_date'] > a['end_date'] or (a['start_date'] == a['end_date'] and not all(c.get('start_price_type') == 'open' and c.get('end_price_type') == 'close' for c in (a, b))) or (a['start_date'], a['end_date']) != (b.get('start_date'), b.get('end_date')):
                    continue
                if not a.get('price_basis') or a['price_basis'] == 'unknown' or a['price_basis'] != b.get('price_basis') or a.get('entry_type') != b.get('entry_type') or a.get('entry_type') in ('issue_price', 'unknown', None):
                    continue
                raw_requested = any(w in lower for w in ('unadjusted', '未复权', '原始价格'))
                adjusted_requested = not raw_requested and any(w in lower for w in ('adjusted', '调整', '复权'))
                if (raw_requested and a['price_basis'] != 'raw') or (adjusted_requested and a['price_basis'] != 'adjusted'):
                    continue
                explicit_dates = re.findall(r'\d{4}-\d{2}-\d{2}', q)
                if explicit_dates and (len(explicit_dates) != 2 or tuple(explicit_dates) != (a['start_date'], a['end_date'])):
                    continue
                if any(not all(inp.get('fact_id') in by_id for inp in c.get('inputs', []) if isinstance(inp, Mapping)) or len(c.get('inputs', [])) < 2 for c in (left, right)):
                    continue
                valid = (left, right)
                break
            if valid:
                break
        market_refs = []
        target_refs = []
        if not valid:
            from .research_market_analytics import AnalyzeMarketSeriesRequest, analyze_market_series
            for entry in [*(state.get('prior_market_analyses') or []), *(state.get('market_analyses') or [])]:
                if not isinstance(entry, Mapping):
                    continue
                try:
                    request = AnalyzeMarketSeriesRequest.model_validate(entry.get('request'))
                    actual = analyze_market_series(request, sources=sources, as_of=as_of, allow_simulated=False)
                    benchmark = actual.get('benchmark') or {}
                    target_symbol = str(actual.get('instrument') or '').split('@')[0]
                    bench_symbol = str(benchmark.get('benchmark_instrument') or '').split('@')[0]
                    dates = (benchmark.get('aligned_start_date'), benchmark.get('aligned_end_date'))
                    def named(symbol: str) -> bool:
                        return bool(symbol and re.search(r'(?<![A-Za-z0-9])' + re.escape(symbol) + r'(?![A-Za-z0-9])', q, re.I))
                    explicit_dates = re.findall(r'\d{4}-\d{2}-\d{2}', q)
                    raw_requested = any(w in lower for w in ('unadjusted', '未复权', '原始价格'))
                    adjusted_requested = not raw_requested and any(w in lower for w in ('adjusted', '调整', '复权'))
                    provenance = actual['provenance']
                    basis = provenance['price_basis_applied']
                    if (not named(target_symbol) or adjusted_requested and basis != 'adjusted'
                            or raw_requested and basis not in ('raw', 'unadjusted') or actual != entry.get('result')):
                        continue
                    if (not request.benchmark_source_id and actual['bar_count'] >= 2 and entry.get('analysis_id')
                            and (not explicit_dates or explicit_dates == [actual['start_date'], actual['end_date']])):
                        target_refs = [{'analysis_id': entry['analysis_id']}]
                        continue
                    reason = (request.benchmark_selection_reason or '').strip().lower()
                    justified = bool(bench_symbol and bench_symbol.lower() in reason and len(reason) >= 20 and
                                     any(w in reason for w in ('index', 'market', 'sector', 'industry', '指数', '市场', '行业')))
                    # Explicit benchmark tokens must match; unnamed choices require a recorded selection reason.
                    explicit_tickers = {x.upper() for x in re.findall(r'(?<![A-Za-z0-9])[A-Z]{2,6}(?![A-Za-z0-9])', q)} - {'IPO', 'EPS', 'USD', 'ETF', 'FY'}
                    roles_match = named(target_symbol) and (named(bench_symbol) or (len(explicit_tickers) <= 1 and justified))
                    basis_mismatch = basis != (provenance.get('benchmark_provenance') or {}).get('price_basis_applied')
                    if (not roles_match or basis_mismatch or target_symbol.upper() == bench_symbol.upper() or benchmark.get('aligned_bar_count', 0) < 2
                        or not all(dates) or dates[0] >= dates[1] or (explicit_dates and (len(explicit_dates) != 2 or tuple(explicit_dates) != dates))):
                        continue
                    market_refs = [{'analysis_id': entry['analysis_id']}] if entry.get('analysis_id') else [{'source_id': sid} for sid in actual['source_ids']]
                    break
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
        add('market:benchmark', q, 'satisfied' if valid or market_refs else 'missing',
            'Distinct instruments have returns over the same dates and price basis; verify their requested roles.' if valid or market_refs else 'Need distinct target and benchmark returns over at least two common dates, using the same price basis.',
            [{'calculation_id': c['calculation_id']} for c in valid] if valid else market_refs)
        if not valid and not market_refs:
            for source, sentence, ref in _read_negative_spans(state, sources, as_of):
                if re.search(r'\bno benchmark (?:observation|data|values?)\b|没有基准(?:观测|数据)', sentence, re.I):
                    names = [n for n in re.findall(r'(?<![A-Za-z0-9_])[A-Z][A-Za-z]{1,}(?![A-Za-z0-9_])', q)
                             if n.casefold() not in {'compare', 'benchmark', 'adjusted'}]
                    if not _fully_read(state, source) or not _unambiguous_subject(source.text, {n.casefold() for n in names}):
                        continue
                    row = rows[-1]
                    row['reason'] = '已读取的材料明确没有基准观测，因此无法计算基准收益或超额收益。'
                    row['references'] = [ref]
                    for same_source, duplicate, duplicate_ref in _read_negative_spans(state, sources, as_of):
                        if (same_source.source_id == source.source_id and names
                                and all(re.search(r'(?<![a-z0-9])' + re.escape(n.casefold()) + r'(?![a-z0-9])', duplicate.casefold()) for n in names)
                                and re.search(r'\b(?:two calls both return|both calls return|duplicate observations?)\b|两次.{0,12}(?:相同|重复)', duplicate, re.I)):
                            row['reason'] = f'已读取的材料说明两次观测均为 {names[0]}，且明确没有基准观测，因此无法计算基准收益或超额收益。'
                            row['references'].append(duplicate_ref)
                            break
                    break
        if not (valid or market_refs):
            add('market:target', '标的在可用交易窗口内的收益', 'satisfied' if target_refs else 'missing',
                'Standalone target return is verified; matching benchmark observations are still needed.' if target_refs else '缺少可核验的标的收益计算。', target_refs)

    if any(x in lower for x in _COUNTER):
        valid = [c for c in claims if _get(c, 'role') == 'counterargument' and _get(c, 'challenges_claim_ids') and
                 any(located(cit) for cit in (_get(c, 'citations', []) or []))]
        negative = []
        for search in state.get('counter_searches') or []:
            if not isinstance(search, Mapping) or search.get('scope') != 'document' or search.get('status') != 'completed' or search.get('matches') != 0:
                continue
            target, hypothesis, query, sid = (str(search.get(k) or '').strip() for k in ('target', 'hypothesis', 'query', 'source_id'))
            linked = bool(target and (target.casefold() in lower or any(target == _get(c, 'claim_id') or target.casefold() == str(_get(c, 'text', '')).casefold() for c in claims)))
            if linked and hypothesis and query and _eligible(sources.get(sid), as_of):
                negative.append(search)
        if valid:
            status, reason = 'satisfied', 'A cited counterargument explicitly identifies the challenged claim.'
            refs = [{'claim_id': _get(c, 'claim_id')} for c in valid if _get(c, 'claim_id')]
        elif negative:
            item = negative[0]
            status, reason = 'satisfied', f"Targeted search for {item['hypothesis']} found no match for {item['query']} in source {item['source_id']}; this does not exclude evidence elsewhere."
            refs = [{'source_id': item['source_id']}]
        else:
            status, reason, refs = 'missing', 'A targeted counterargument or scoped negative search needs an identified claim, alternative and eligible source; a generic search is insufficient.', []
        add('counterevidence', q, status, reason, refs)

    # A request solely to draw or transform a chart is fulfilled by a saved,
    # source-authorized chart of the requested kind, not by an unrelated claim.
    chart_task = bool(re.fullmatch(r'(?i)(?:draw|create|make|convert|change|update|redraw|绘制|生成|转换|改为)[^?？;；]{0,100}\b(?:chart|graph|map|图表|图|图形)(?:[^?？;；]{0,60})', q))
    chart_refs = []
    if chart_task and not metrics and not any(x in lower for x in (*_BENCHMARK, *_COUNTER)):
        for chart in state.get('charts') or []:
            if not isinstance(chart, Mapping) or not chart.get('chart_id') or not chart.get('source_ids'):
                continue
            if not all(_eligible(sources.get(sid), as_of) for sid in chart['source_ids']):
                continue
            kind = chart.get('chart_type')
            if any(x in lower for x in ('causal', '因果', '传导')) and kind != 'causal_graph':
                continue
            if any(x in lower for x in ('line', '折线')) and kind != 'line':
                continue
            if any(x in lower for x in ('convert', 'change', '转换', '改为')) and not chart.get('replaces_chart_id'):
                continue
            if 'billion' in lower and not any(axis.get('scale') == 'billions' for axis in chart.get('axes', []) if isinstance(axis, Mapping)):
                continue
            chart_refs.append({'chart_id': chart['chart_id']})
        add('chart', q, 'satisfied' if chart_refs else 'missing',
            'A saved chart uses eligible sources; assumptions remain labelled.' if chart_refs else 'A source-backed chart of the requested kind is required.', chart_refs)

    # Preserve independently requested clauses that did not select a method.
    handled = {fragment for _, fragment in metric_requests}
    unknown = [] if chart_task and chart_refs else [x for x in segments if x not in handled
               and not any(w in x.lower() for w in (*_BENCHMARK, *_COUNTER))]
    if not rows:
        unknown = [q]
    for fragment in unknown:
        if re.search(r'\b(?:drivers?|drove|driven|causes?|contribut(?:ion|ed))\b|驱动|原因|归因|贡献', fragment, re.I):
            for calc in calcs:
                if calc.get('formula_id') != 'contribution_to_change_percent' or not calc.get('calculation_id'):
                    continue
                inputs = calc.get('inputs') or []
                if len(inputs) != 3 or any(not isinstance(i, Mapping) or i.get('fact_id') not in by_id for i in inputs):
                    continue
                contribution, current, prior = [by_id[i['fact_id']] for i in inputs]
                from .research_report_grounding import change_contribution_disclosed
                quote = contribution.get('quotation') or ''
                years = [re.search(r'(?<!\d)(?:19|20)\d{2}(?!\d)', str(f.get('period') or '')) for f in (current, prior)]
                if (not change_contribution_disclosed(quote)
                        or any(not y or y.group() not in quote for y in years)
                        or str(current.get('label') or '').casefold().strip() != str(prior.get('label') or '').casefold().strip()):
                    continue
                labels = [re.match(r'^([A-Za-z][A-Za-z0-9.-]{2,})\b', str(f.get('label') or '')) for f in (contribution, current, prior)]
                name = labels[0].group(1) if all(labels) and len({m.group(1).casefold() for m in labels}) == 1 else None
                if not name or not all(re.search(r'(?<![A-Za-z0-9])' + re.escape(name) + r'(?![A-Za-z0-9])', f.get('quotation') or '', re.I) for f in (contribution, current, prior)):
                    continue
                from .research_report_grounding import contribution_subject_disclosed
                if not contribution_subject_disclosed(quote, name):
                    continue
                from .research_calculations import calculation_input_status, calculation_result_status
                if calculation_input_status(dict(calc), facts) != 'matched' or calculation_result_status(dict(calc)) != 'matched':
                    continue
                refs = [_ref_fact(by_id[i['fact_id']]) for i in inputs] + [{'calculation_id': calc['calculation_id']}]
                add('contribution:' + fragment, fragment, 'partial',
                    'A disclosed contribution and change were calculated; issuer identity, accounting scope, remaining drivers and sustainability still require review.', refs)
                break
            else:
                pass
            if rows and rows[-1].get('kind') == 'contribution:' + fragment:
                continue
            # Preserve a located direct disclosure even when the two-period
            # denominator is missing; it cannot establish a percentage share.
            from .research_report_grounding import change_contribution_disclosed, contribution_subject_disclosed
            names = {x.casefold() for x in re.findall(r'(?<![A-Za-z0-9])[A-Z][A-Za-z]{2,}(?![A-Za-z0-9])', q)
                     if x.casefold() not in {'can', 'could', 'what', 'which', 'how', 'does', 'will', 'compare'}}
            direct = [c for c in claims if _get(c, 'role') == 'support' and _get(c, 'claim_id')
                      and change_contribution_disclosed(str(_get(c, 'text', '')))
                      and any(located(cit) and change_contribution_disclosed(str(_get(cit, 'quotation', '')))
                              and all(contribution_subject_disclosed(str(_get(cit, 'quotation', '')), name) for name in names)
                              for cit in (_get(c, 'citations', []) or []))]
            if len(direct) == 1:
                add('contribution:' + fragment, fragment, 'partial',
                    'A contribution is disclosed; the comparable denominator, remaining drivers and sustainability require evidence and review.',
                    [{'claim_id': _get(direct[0], 'claim_id')}])
                continue
        if re.search(r'\b(?:cite|citation|citations|reference|references|sources)\b|引用|引文|来源', fragment, re.I):
            relevant_ids = {ref.get('fact_id') for row in rows if row.get('kind', '').startswith('financial:')
                            for ref in row.get('references', []) if ref.get('fact_id')}
            relevant = [f for f in facts if not metric_requests or f.get('fact_id') in relevant_ids]
            refs = [_ref_fact(f) for f in _distinct_facts(relevant)]
            if refs:
                add('evidence:' + fragment, fragment, 'satisfied',
                    'Recorded facts link to eligible original source ranges; semantic support remains subject to review.', refs)
                continue
        # A model checkpoint is only a candidate: references must survive current source and cutoff checks.
        matched = [p for p in parts if _part_key(str(p.get('question') or '')) == _part_key(fragment) and p.get('status') == 'answered' and p.get('answer')]
        refs = [ref for p in matched for ref in p.get('references', []) if isinstance(ref, Mapping) and ref.get('fact_id') in by_id]
        claims_by_id = {_get(c, 'claim_id'): c for c in claims if _get(c, 'claim_id')}
        for p in matched:
            for ref in p.get('references', []):
                if not isinstance(ref, Mapping):
                    continue
                claim = claims_by_id.get(ref.get('claim_id'))
                if claim and _get(claim, 'role') == 'support' and any(located(c) for c in (_get(claim, 'citations', []) or [])):
                    refs.append({'claim_id': ref['claim_id']})
                sid, start, end = ref.get('source_id'), ref.get('character_start'), ref.get('character_end')
                src = sources.get(sid)
                if (_eligible(src, as_of) and type(start) is int and type(end) is int and 0 <= start < end <= len(src.text)
                    and any(isinstance(r, Mapping) and r.get('source_id') == sid and r.get('start', -1) <= start < end <= r.get('end', -1)
                            for r in [*(state.get('prior_read_ranges') or []), *(state.get('read_ranges') or [])])):
                    refs.append({'source_id': sid, 'character_start': start, 'character_end': end})
        stop = {'what', 'which', 'when', 'where', 'how', 'does', 'did', 'the', 'this', 'that', 'with', 'from', 'have', 'were', 'was', 'are', 'is', 'for', 'and', 'explain', 'summarize', 'disclose', 'provide', 'please'}
        terms = {w for w in re.findall(r'[a-z0-9]{3,}', fragment.lower()) if w not in stop}
        cjk_terms = {fragment[i:i+2] for i in range(len(fragment)-1) if re.fullmatch(r'[一-龥]{2}', fragment[i:i+2])}
        if terms or cjk_terms:
            for claim in claims:
                claim_text = str(_get(claim, 'text', ''))
                text_terms = set(re.findall(r'[a-z0-9]{3,}', claim_text.lower()))
                overlap = bool(terms & text_terms or any(w in claim_text for w in cjk_terms))
                absent = bool(re.search(r'\b(?:not disclosed|no disclosure|not mentioned)\b|未披露|没有披露|未提及', claim_text, re.I))
                complete = any(_eligible(sources.get(_get(c, 'source_id')), as_of) and
                               any(isinstance(r, Mapping) and r.get('source_id') == _get(c, 'source_id') and r.get('start') == 0
                                   and r.get('end', 0) >= len(sources[_get(c, 'source_id')].text)
                                   for r in [*(state.get('prior_read_ranges') or []), *(state.get('read_ranges') or [])])
                               for c in (_get(claim, 'citations', []) or []))
                conditions_requested = any(w in fragment.lower() for w in ('condition', '条件', '限定'))
                conditions_addressed = any(w in claim_text.lower() for w in ('condition', 'subject to', 'provided that', '条件', '须', '需', '前提', '批准'))
                if (overlap and (not conditions_requested or conditions_addressed) and (not absent or complete) and _get(claim, 'role') == 'support' and _get(claim, 'claim_id')
                    and any(located(c) for c in (_get(claim, 'citations', []) or []))):
                    refs.append({'claim_id': _get(claim, 'claim_id')})
        add('generic:' + fragment, fragment, 'satisfied' if refs else 'missing',
            'A checkpoint binds the complete question to eligible recorded facts; semantic support needs review.' if refs else 'The complete question needs an answer linked to eligible evidence; coverage cannot be inferred from tool calls.', refs)
    return rows
