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


def question_parts(question: str, state: Mapping[str, Any], eligible_source_ids: set[str]) -> list[dict]:
    facts = {fact.get('fact_id'): fact for key in ('prior_facts', 'facts') for fact in (state.get(key) or []) if isinstance(fact, Mapping)}
    rows = []
    for item in state.get('question_parts') or []:
        if not isinstance(item, Mapping) or not item.get('question') or item['question'] not in question:
            continue
        refs = [ref for ref in item.get('references', []) if isinstance(ref, Mapping)
                and (facts.get(ref.get('fact_id'), {}).get('source_id') in eligible_source_ids
                     or (ref.get('source_id') and ref.get('source_id') in eligible_source_ids))]
        if len(refs) != len(item.get('references', [])):
            rows.append({'question': item['question'], 'status': 'unresolved', 'answer': '',
                         'reason': '相关依据当前不可用，无法展示已保存的回答。', 'references': []})
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
    checkpoints = question_parts(question, state, eligible_source_ids)
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
    saved_keys = {_part_key(p['question']) for p in state.get('question_parts', [])
                  if isinstance(p, Mapping) and isinstance(p.get('question'), str)}
    for checkpoint in checkpoints:
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
            if _part_key(checkpoint['question']) in saved_keys and checkpoint.get('reason') and not checkpoint.get('answer'):
                existing['reason'] = checkpoint['reason']
    if len(checkpoints) > 1 and not any(p['question'] == question for p in checkpoints):
        rows = [row for row in rows if row.get('question') != question]
    order = {_part_key(part['question']): index for index, part in enumerate(checkpoints)}
    rows.sort(key=lambda row: order.get(_part_key(row['question']), len(order)))
    return rows
