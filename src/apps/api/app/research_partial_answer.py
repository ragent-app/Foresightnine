"""Preserve checkpointed question components and conservatively disclose gaps."""
import re
from collections.abc import Mapping
from typing import Any


def _requested_parts(question: str) -> list[str]:
    # Split enumeration syntax, never individual characters inside ordinary entity names.
    text = question.strip()
    introduced = re.search(r'(?:包括|包含|例如|分别是|include(?:s|d)?\s*:?)\s*', text, re.IGNORECASE)
    if introduced:
        text = text[introduced.end():]
    enumerated = bool(introduced or re.search(r'[、;；?？,，]|\b(?:and|or)\b|以及|并且|(?:^|\s)\d+[.)、]', text, re.IGNORECASE))
    if not enumerated:
        return [text] if text else []
    # 和/及 are only separators within an explicit enumeration and when not embedded
    # inside a contiguous Chinese word; 、/以及 and punctuation remain preferred.
    pattern = r'[、;；?？,，]|\b(?:and|or)\b|以及|并且|(?:^|\s)\d+[.)、]'
    parts = [p.strip(' \t\n.。:：') for p in re.split(pattern, text, flags=re.IGNORECASE) if p.strip(' \t\n.。:：')]
    # A final conjunction after a list separator commonly introduces the last
    # item; leave other occurrences inside names or ordinary prose intact.
    if introduced and len(parts) > 1:
        tail = re.fullmatch(r'(.{1,30})和(.{1,30})', parts[-1])
        if tail:
            left, right = tail.groups()
            shared = any(left[i:i + 2] in right for i in range(len(left) - 1))
            if (len(left) <= 4 and len(right) <= 4) or shared:
                parts[-1:] = [left, right]
    return parts


def _part_key(value: str) -> str:
    text = re.sub(r'\s+', ' ', value.strip(' \t\n.,，。;；?？:：、')).casefold()
    return re.sub(r'^(?:并且|以及|并(?=说明|解释|比较|分析|列出|判断|计算)|(?:and|also|then)\s+)', '', text).strip()


def question_parts(question: str, state: Mapping[str, Any], eligible_source_ids: set[str], source_by_id: Mapping[str, Any] | None = None) -> list[dict]:
    facts = {fact.get('fact_id'): fact for key in ('prior_facts', 'facts') for fact in (state.get(key) or []) if isinstance(fact, Mapping)}
    calculations = {c.get('calculation_id'): c for key in ('prior_calculations', 'calculations')
                    for c in (state.get(key) or []) if isinstance(c, Mapping)}
    if source_by_id is not None:
        from .research_evidence_requirements import _source_hashes
        hashes = _source_hashes(state)
    else:
        hashes = {}
    rows = []
    def calculation_inputs_visible(calc_id):
        calc = calculations.get(calc_id)
        inputs = calc.get('inputs') if calc else None
        return bool(inputs) and all(isinstance(i, Mapping) and i.get('fact_id') in facts
                                    and facts[i['fact_id']].get('source_id') in eligible_source_ids for i in inputs)
    def visible(ref):
        if not isinstance(ref, Mapping):
            return False
        if ref.get('calculation_id'):
            return calculation_inputs_visible(ref['calculation_id'])
        return (facts.get(ref.get('fact_id'), {}).get('source_id') in eligible_source_ids
                or bool(ref.get('source_id') and ref.get('source_id') in eligible_source_ids))
    for item in state.get('question_parts') or []:
        if not isinstance(item, Mapping) or not item.get('question') or item['question'] not in question:
            continue
        refs = [ref for ref in item.get('references', []) if visible(ref)]
        if source_by_id is not None:
            def current(ref):
                if ref.get('calculation_id'):
                    from .research_observations import verified_partial_calculation_ids
                    calc = calculations.get(ref['calculation_id'])
                    if not calc:
                        return False
                    ids = {i.get('fact_id') for i in calc.get('inputs', []) if isinstance(i, Mapping) and i.get('fact_id')}
                    if not ids or any(facts.get(i, {}).get('source_id') not in eligible_source_ids for i in ids):
                        return False
                    return ref['calculation_id'] in verified_partial_calculation_ids(
                        state, source_by_id, fact_ids=ids, calculation_ids={ref['calculation_id']})
                fact = facts.get(ref.get('fact_id'))
                sid = fact.get('source_id') if fact else ref.get('source_id')
                source = source_by_id.get(sid)
                if not source or (sid in hashes and hashes[sid] != getattr(source, 'content_sha256', None)):
                    return False
                start = fact.get('character_start') if fact else ref.get('character_start')
                end = fact.get('character_end') if fact else ref.get('character_end')
                if start is not None or end is not None:
                    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source.text):
                        return False
                    if fact and fact.get('quotation') != source.text[start:end]:
                        return False
                return True
            refs = [ref for ref in refs if current(ref)]
        if len(refs) != len(item.get('references', [])):
            rows.append({'question': item['question'], 'status': 'unresolved', 'answer': '',
                         'reason': '相关依据已变化或不可用，需要重新核验。' if source_by_id is not None else '相关依据当前不可用，无法展示已保存的回答。', 'references': []})
        else:
            rows.append(dict(item))
    for part in _requested_parts(question):
        # A saved exact excerpt covers that component; preserve additional uncovered text.
        remaining = part
        for row in rows:
            if _part_key(row['question']) == _part_key(part):
                remaining = ''
                break
            remaining = remaining.replace(row['question'], '', 1)
        remaining = remaining.strip(' \t\n.。:：')
        if remaining and not any(_part_key(part) == _part_key(row['question']) for row in rows):
            rows.append({'question': remaining, 'status': 'unresolved', 'answer': '',
                         'reason': '这一部分尚未回答；需要补充相关依据和具体条件。', 'references': []})
    return rows[:21]


def merge_checkpoint_parts(question: str, state: Mapping[str, Any], eligible_source_ids: set[str], assessed: list[dict], source_by_id: Mapping[str, Any] | None = None) -> list[dict]:
    """Add checkpoint details without replacing assessment identities or satisfied results."""
    checkpoints = question_parts(question, state, eligible_source_ids, source_by_id)
    if source_by_id is not None:
        facts = {f.get('fact_id'): f for key in ('facts', 'prior_facts') for f in state.get(key, []) if isinstance(f, Mapping)}
        for part in checkpoints:
            for ref in part.get('references', []):
                fact = facts.get(ref.get('fact_id'))
                if not fact:
                    continue
                source = source_by_id.get(fact.get('source_id'))
                start, end = fact.get('character_start'), fact.get('character_end')
                if (not source or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source.text)
                    or (fact.get('quotation') and source.text[start:end] != fact['quotation'])):
                    part.update(status='unresolved', answer='', reason='相关依据已变化或不可用，需要重新核验。', references=[])
                    break
    rows = [dict(row) for row in assessed]
    whole_assessed = any(_part_key(str(row.get('question', ''))) == _part_key(question)
                         and row.get('requirements') for row in assessed)
    saved_keys = {_part_key(p['question']) for p in state.get('question_parts', [])
                  if isinstance(p, Mapping) and isinstance(p.get('question'), str)}
    for checkpoint in checkpoints:
        if whole_assessed and _part_key(checkpoint['question']) != _part_key(question):
            continue
        existing = next((row for row in rows if _part_key(row.get('question', '')) == _part_key(checkpoint['question'])), None)
        if existing is None:
            rows.append(dict(checkpoint))
        elif checkpoint.get('reason') == '相关依据已变化或不可用，需要重新核验。':
            existing.update(status='unresolved', answer='', reason=checkpoint['reason'], references=[])
        elif existing.get('status') != 'answered':
            # Preserve the assessment's requirements and ID; only fill in actual
            # checkpointed work when the assessment has not supplied an answer.
            if checkpoint.get('answer') and not existing.get('answer'):
                existing['answer'] = checkpoint['answer']
                missing_core = any(r.get('core', True) and r.get('status') not in ('satisfied', 'not_applicable')
                                   for r in existing.get('requirements', []))
                existing['status'] = 'partial' if missing_core else checkpoint['status']
                existing['references'] = checkpoint.get('references', [])
            if _part_key(checkpoint['question']) in saved_keys and checkpoint.get('reason') and not checkpoint.get('answer') and not existing.get('references'):
                existing['reason'] = checkpoint['reason']
    if len(checkpoints) > 1 and not any(p['question'] == question for p in checkpoints):
        # A whole-question assessment can encode a relationship across the
        # apparent clauses (for example a target and its benchmark). Splitting
        # on "and" must not discard that jointly required evidence.
        rows = [row for row in rows if row.get('question') != question or row.get('requirements')]
    order = {_part_key(part['question']): index for index, part in enumerate(checkpoints)}
    rows.sort(key=lambda row: order.get(_part_key(row['question']), len(order)))
    return rows
