"""Assemble a consistent, conservative delivery from assessed evidence."""
from hashlib import sha256
import re
import logging
from typing import Any

from . import research_run_insights as insights
from .research_partial_answer import question_parts as checkpoint_parts, _part_key

log = logging.getLogger(__name__)


def assess(question: str, state: dict, source_by_id: dict, *, as_of: Any = None, claims: list | tuple = ()) -> list[dict]:
    try:
        rows = insights.assess_core_requirements(question, state, source_by_id, as_of=as_of, claims=claims)
        if not rows:
            raise ValueError('empty requirement assessment')
        # A request to explain the previous answer can reuse a single located,
        # currently eligible prior claim; semantic interpretation remains reviewable.
        if re.fullmatch(r'\s*(?:explain (?:the|your|that|previous) answer|解释(?:之前|上面|这个)?(?:的)?(?:回答|结论))\s*[?.？。]?\s*', question, re.I):
            candidates = [claim for claim in claims if isinstance(claim, dict) and claim.get('role') == 'support' and claim.get('citations')]
            if len(candidates) == 1:
                row = candidates[0]
                return [{'id': 'requirement-' + sha256(question.encode()).hexdigest()[:20], 'question': question,
                         'status': 'satisfied', 'reason': '引用此前记录的结论；语义仍需复核。',
                         'references': [{'claim_id': row['claim_id'], 'origin': 'prior'}], 'core': True}]
        return rows
    except Exception:
        log.exception('Core requirement assessment failed')
        return [{'id': 'requirement-' + sha256(question.encode()).hexdigest()[:20], 'question': question,
                 'status': 'unavailable', 'reason': '无法评估核心要求；需要重新核验。', 'references': [], 'core': True}]


def incomplete(rows: list[dict]) -> bool:
    return any(row.get('core', True) and row.get('status') not in ('satisfied', 'not_applicable') for row in rows)


def assemble_parts(question: str, state: dict, source_by_id: dict, eligible_ids: set[str], rows: list[dict], claims: list[dict]) -> list[dict]:
    checkpoints = checkpoint_parts(question, state, eligible_ids)
    claim_by_id = {c.get('claim_id'): c for c in claims}
    facts = {f.get('fact_id'): f for key in ('prior_facts', 'facts') for f in state.get(key, []) if isinstance(f, dict)}
    calculations = {c.get('calculation_id'): c for key in ('prior_calculations', 'calculations') for c in state.get(key, []) if isinstance(c, dict)}
    charts = {c.get('chart_id'): c for c in state.get('charts', []) if isinstance(c, dict)}
    analyses = {a.get('analysis_id'): a for key in ('prior_market_analyses', 'market_analyses') for a in state.get(key, []) if isinstance(a, dict)}
    prior_ids = {key: {item.get(key) for item in state.get('prior_' + group, []) if isinstance(item, dict)} for key, group in (('fact_id', 'facts'), ('calculation_id', 'calculations'), ('analysis_id', 'market_analyses'))}
    result = []
    for requirement in rows:
        if not requirement.get('core', True):
            continue
        excerpt = requirement.get('question') or question
        refs = [dict(r, origin=r.get('origin') or ('prior' if any(r.get(k) in ids for k, ids in prior_ids.items() if r.get(k)) else 'current')) for r in requirement.get('references', []) if isinstance(r, dict)]
        status = requirement.get('status', 'missing')
        checkpoint = next((p for p in checkpoints if _part_key(str(p.get('question') or '')) == _part_key(excerpt) and p.get('status') != 'unresolved'), None)
        referenced_claims = [claim_by_id.get(r.get('claim_id')) for r in refs if r.get('claim_id')]
        answer = ' '.join(c['text'] for c in referenced_claims if c and c.get('text'))
        if not answer and status == 'satisfied' and len({r.get('source_id') for r in refs if r.get('source_id') in eligible_ids}) >= 2 and 'conflict' in requirement.get('reason', '').casefold():
            answer = '所列资料存在可核对的数值差异。'
        if not answer and checkpoint and any(any(r.get(k) and r.get(k) == x.get(k) for k in ('fact_id', 'source_id', 'claim_id', 'calculation_id')) for r in checkpoint.get('references', []) for x in refs):
            # Checkpoint answers are independently validated at write time and source-filtered above.
            answer = checkpoint.get('answer', '')
            refs.extend(r for r in checkpoint.get('references', []) if r not in refs)
        if not answer:
            snippets = []
            for ref in refs:
                fact = facts.get(ref.get('fact_id'))
                calc = calculations.get(ref.get('calculation_id'))
                chart = charts.get(ref.get('chart_id'))
                if chart:
                    snippets.append(chart.get('title') or 'Chart')
                if fact:
                    snippets.append(' '.join(str(fact.get(k) or '') for k in ('label', 'period', 'value', 'unit')).strip())
                analysis = analyses.get(ref.get('analysis_id'))
                if analysis:
                    data = analysis.get('result') or {}
                    bench = data.get('benchmark') or {}
                    if bench:
                        snippets.append(f"{data.get('instrument', '')} / {bench.get('benchmark_instrument', '')} {bench.get('aligned_start_date', '')}–{bench.get('aligned_end_date', '')}: {bench.get('asset_aligned_return_percent', '')}% / {bench.get('benchmark_aligned_return_percent', '')}%, excess {bench.get('excess_return_percent', '')} percentage points")
                    elif data.get('cumulative_return_percent') is not None:
                        snippets.append(f"{data['instrument']} {data['start_date']}–{data['end_date']} · {data['cumulative_return_percent']}%")
                if calc:
                    snippets.append(' '.join(str(calc.get(k) or '') for k in ('formula_id', 'value', 'unit')).strip())
            answer = '；'.join(x for x in snippets if x)
        if status == 'not_applicable' and not answer:
            answer = requirement.get('reason') or '不适用。'
        part_status = 'answered' if status in ('satisfied', 'not_applicable') and answer else ('partial' if answer else 'unresolved')
        checkpoint_gap = next((p.get('reason') for p in state.get('question_parts', [])
                               if part_status != 'answered' and isinstance(p, dict) and p.get('reason')
                               and _part_key(str(p.get('question') or '')) == _part_key(excerpt)), None)
        requirement_copy = {k: requirement[k] for k in ('id', 'question', 'status', 'reason', 'references', 'core') if k in requirement}
        requirement_copy['references'] = refs
        result.append({'id': requirement['id'], 'question': excerpt, 'core': True,
                       'status': part_status, 'answer': answer,
                       'reason': checkpoint_gap or requirement.get('reason') or '', 'references': refs,
                       'requirements': [requirement_copy]})
    # Preserve independently saved grounded components, without replacing assessed rows
    # or adding synthetic unresolved fragments that duplicate those rows.
    for checkpoint in checkpoints:
        if checkpoint.get('answer') and not any(_part_key(str(checkpoint.get('question') or '')) == _part_key(str(part.get('question') or '')) for part in result):
            result.append({**checkpoint, 'id': 'part-' + sha256(checkpoint['question'].encode()).hexdigest()[:20], 'core': True, 'requirements': []})
    return result
