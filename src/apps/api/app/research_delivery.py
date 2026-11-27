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
        instructions = [i for i in state.get('user_instructions', []) if isinstance(i, dict)]
        sequence = max((i.get('sequence', 0) for i in instructions if type(i.get('sequence')) is int), default=0)
        for plan in state.get('requirement_plan', []):
            if (not isinstance(plan, dict) or plan.get('instruction_sequence') != sequence
                    or not isinstance(plan.get('requirement_id'), str) or not isinstance(plan.get('deliverable'), str)):
                continue
            rows.append({'id': plan['requirement_id'], 'question': plan['deliverable'],
                         'status': 'missing', 'reason': 'This planned deliverable requires independent review.',
                         'references': [], 'core': plan.get('priority') == 'core',
                         'kind': 'generic:planned', 'plan': plan})
        from .research_semantic_review import candidates, parse_reviews
        current = {item['review_key']: item for item in candidates(rows, claims, state, source_by_id, as_of)}
        import json
        reviews = parse_reviews(json.dumps({'reviews': state.get('_semantic_reviews', [])}), list(current.values()))
        for row in rows:
            if not any(item['requirement_id'] == row.get('id') for item in current.values()):
                continue
            row['semantic_required'] = True
            matches = [r for r in reviews if r.get('requirement_id') == row['id']]
            # Conflicting judgments never upgrade an answer.
            if any(r.get('verdict') == 'contradicted' for r in matches):
                row.update(status='missing', reason='Evidence contradicts a candidate answer.', references=[])
                continue
            accepted = [r for r in matches if r.get('verdict') in ('supported', 'conditional')
                        and (r.get('verdict') != 'conditional' or
                             (r.get('condition') and r['condition'] in current[r['review_key']]['claim']))]
            if accepted:
                review = accepted[0]
                row.update(status='satisfied', reason=review.get('reason', ''),
                           references=[{'claim_id': review['claim_id']}],
                           verdict=review['verdict'], condition=review.get('condition'))
                continue
            # Supported premises that each answer part of a compound requirement are
            # shown as a partial answer; they never complete the requirement alone.
            partial = [r for r in matches if r.get('verdict') == 'partial']
            if partial:
                row.update(status='partial', reason=partial[0].get('reason', ''),
                           references=[{'claim_id': r['claim_id']} for r in partial], verdict='partial')
        # A broad question can contain several requested deliverables. Keep its
        # display consistent with the current core plan and its verified answers.
        core_plan = [row for row in rows if row.get('kind') == 'generic:planned' and row.get('core')]
        for row in rows:
            if not core_plan or row.get('kind') == 'generic:planned' or _part_key(row['question']) != _part_key(question):
                continue
            if row.get('status') == 'satisfied' and incomplete(core_plan):
                row.update(status='partial', reason='部分核心要求仍待核验。')
            elif row.get('status') == 'satisfied':
                for part in core_plan:
                    for ref in part.get('references', []):
                        if ref not in row['references']:
                            row['references'].append(ref)
        return rows
    except Exception:
        log.exception('Core requirement assessment failed')
        return [{'id': 'requirement-' + sha256(question.encode()).hexdigest()[:20], 'question': question,
                 'status': 'unavailable', 'reason': '无法评估核心要求；需要重新核验。', 'references': [], 'core': True}]


def incomplete(rows: list[dict]) -> bool:
    return any(row.get('core', True) and row.get('status') not in ('satisfied', 'not_applicable') for row in rows)


def assemble_parts(question: str, state: dict, source_by_id: dict, eligible_ids: set[str], rows: list[dict], claims: list[dict]) -> list[dict]:
    checkpoints = checkpoint_parts(question, state, eligible_ids, source_by_id)
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
        if not answer and not refs and checkpoint and any(r.get('calculation_id') for r in checkpoint.get('references', [])):
            # Preserve the checkpoint's explicit calculation identity for the
            # runtime to reconstruct a source-checked partial answer.
            refs = [dict(r) for r in checkpoint['references']]
            answer = checkpoint.get('answer', '')
        if not answer and not any(r.get('calculation_id') for r in refs) and checkpoint and any(any(r.get(k) and r.get(k) == x.get(k) for k in ('fact_id', 'source_id', 'claim_id', 'calculation_id')) for r in checkpoint.get('references', []) for x in refs):
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
        if not answer and any(r.get('source_id') and r.get('character_start') is not None for r in refs) and '明确未披露' in (requirement.get('reason') or ''):
            answer = requirement.get('reason') or ''
        if not answer and any(r.get('source_id') and r.get('character_start') is not None for r in refs) and status in ('partial', 'missing'):
            answer = requirement.get('reason') or ''
        part_status = 'answered' if status in ('satisfied', 'not_applicable') and answer else ('partial' if answer else 'unresolved')
        checkpoint_gap = next((p.get('reason') for p in state.get('question_parts', [])
                               if part_status != 'answered' and isinstance(p, dict) and p.get('reason')
                               and _part_key(str(p.get('question') or '')) == _part_key(excerpt)), None)
        requirement_copy = {k: requirement[k] for k in ('id', 'question', 'status', 'reason', 'references', 'core') if k in requirement}
        requirement_copy['references'] = refs
        result.append({'id': requirement['id'], 'question': excerpt, 'core': True,
                       'status': part_status, 'answer': answer,
                       'reason': '' if answer and answer == requirement.get('reason') else ((requirement.get('reason') or '') if part_status == 'answered' or any(r.get('source_id') and r.get('character_start') is not None for r in refs) else (checkpoint_gap or requirement.get('reason') or '')), 'references': refs,
                       'requirements': [requirement_copy]})
    return result
