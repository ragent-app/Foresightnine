"""Prepare a bounded local follow-up from recorded, unresolved evidence cues.

The selected finding text is untrusted model/user data, not a new instruction
or a verified fact. External discovery is outside this module's authority.
"""
from __future__ import annotations

from dataclasses import dataclass

from .research_quality import CLAIM_EVIDENCE_WARNINGS


@dataclass(frozen=True)
class GapFocus:
    claim_indexes: tuple[int, ...]
    question_indexes: tuple[int, ...]
    question: str
    search_query: str


def build_gap_focus(question: str, claims: list[dict], research_questions: list[dict] | None = None,
                    max_items: int = 3) -> GapFocus | None:
    """Select findings with explicit recorded gaps, not topic-specific words."""
    if not isinstance(question, str) or not question.strip() or not 1 <= max_items <= 3:
        return None
    candidates: list[tuple[int, str]] = []
    for index, claim in enumerate(claims[:2000]):
        if not isinstance(claim, dict):
            continue
        text = claim.get('text')
        citations = claim.get('citations')
        review = claim.get('support_review')
        review_gap = isinstance(review, dict) and review.get('verdict') != 'supports'
        if (isinstance(text, str) and text.strip() and
                (not isinstance(citations, list) or not citations or claim.get('support_status') in ('unsupported', 'incomplete') or
                 any(claim.get(name) for name in CLAIM_EVIDENCE_WARNINGS) or review_gap)):
            candidates.append((index, text.strip()[:300]))
    unanswered = [(index, item['question'].strip()[:300]) for index, item in enumerate((research_questions or [])[:20])
                  if isinstance(item, dict) and isinstance(item.get('question'), str) and item['question'].strip()
                  and not item.get('claim_indexes')]
    selected_questions = unanswered[:max_items]
    selected = candidates[:max_items - len(selected_questions)]
    if not selected and not selected_questions:
        return None
    # Text here is untrusted model/user data, never system instructions and
    # never sent to an external search provider by this helper.
    snippets = '\n'.join([*(f'question [{index}] {text}' for index, text in selected_questions),
                          *(f'finding [{index}] {text}' for index, text in selected)])
    combined = (question.strip()[:3000] + '\nUntrusted prior subquestions and findings requiring additional source evidence; '
                'do not assume they are true or complete:\n' + snippets)
    # Sending this query outside the process requires separate user consent.
    # It may include user or model text derived from private materials.
    detail = (selected_questions or selected)[0][1]
    query = ' '.join((question.strip()[:29] + ' ' + detail[:40]).split())[:70]
    return GapFocus(tuple(index for index, _ in selected), tuple(index for index, _ in selected_questions), combined, query)


def remap_followup_artifacts(facts: list[dict], calculations: list[dict], *, operation_id: str,
                             existing_fact_ids: set[str], existing_facts: list[dict],
                             max_facts: int, max_calculations: int) -> tuple[list[dict], list[dict], int]:
    """Give follow-up facts unique IDs and preserve only valid calculations.

    This only changes identifiers, never values or evidence. Inline XBRL
    fingerprints are updated only if they were valid before remapping.
    """
    from collections import Counter
    from copy import deepcopy
    from hashlib import sha256
    from .research_calculations import direction_confirmation_fingerprint, xbrl_source_consistent

    counts = Counter(item.get('fact_id') for item in facts if isinstance(item, dict) and isinstance(item.get('fact_id'), str))
    mapping: dict[str, str] = {}
    existing_by_id: dict[str, list[dict]] = {}
    for item in existing_facts:
        if isinstance(item, dict) and isinstance(item.get('fact_id'), str):
            existing_by_id.setdefault(item['fact_id'], []).append(item)
    output_facts: list[dict] = []
    omitted = 0
    for index, fact in enumerate(facts):
        old_id = fact.get('fact_id') if isinstance(fact, dict) else None
        if not isinstance(old_id, str) or not old_id or counts[old_id] != 1:
            omitted += 1
            continue
        existing = existing_by_id.get(old_id, [])
        identity_keys = ('source_id', 'quotation', 'location', 'value', 'unit', 'currency', 'period',
                         'extraction_method', 'structured_metadata')
        if (fact.get('extraction_method') == 'sec_company_concept_row_v1' and len(existing) == 1 and
                all(existing[0].get(key) == fact.get(key) for key in identity_keys)):
            # The same server-derived source row was already retained. Reuse
            # it for explicit calculations without duplicating the evidence.
            mapping[old_id] = old_id
            continue
        if len(output_facts) >= max_facts:
            omitted += 1
            continue
        new_id = 'gap_' + sha256(f'{operation_id}:{index}:{old_id}'.encode()).hexdigest()[:32]
        if new_id in existing_fact_ids:
            omitted += 1
            continue
        proof_valid = xbrl_source_consistent(fact)
        copied = deepcopy(fact)
        copied['fact_id'] = new_id
        copied.pop('direction_confirmation', None)
        copied.pop('table_confirmation', None)
        if not proof_valid:
            copied.pop('xbrl_source_consistency', None)
        mapping[old_id] = new_id
        existing_fact_ids.add(new_id)
        output_facts.append(copied)
    for fact in output_facts:
        parent = fact.get('range_parent_id')
        if isinstance(parent, str):
            if parent in mapping:
                fact['range_parent_id'] = mapping[parent]
            else:
                fact.pop('range_parent_id', None)
        if isinstance(fact.get('xbrl_source_consistency'), dict):
            fact['xbrl_source_consistency']['fact_sha256'] = direction_confirmation_fingerprint(fact)
    output_calculations: list[dict] = []
    for calculation in calculations:
        inputs = calculation.get('inputs') if isinstance(calculation, dict) else None
        if (len(output_calculations) >= max_calculations or not isinstance(inputs, list) or len(inputs) != 2 or
                any(not isinstance(item, dict) or item.get('fact_id') not in mapping for item in inputs)):
            omitted += 1
            continue
        copied = deepcopy(calculation)
        for item in copied['inputs']:
            item['fact_id'] = mapping[item['fact_id']]
        output_calculations.append(copied)
    return output_facts, output_calculations, omitted


def merge_research_questions(original: list[dict], followup: list[dict], index_map: dict[int, int]) -> list[dict]:
    """Merge only text-identical model subquestions and validated references.

    Similar wording is not taken as semantic equivalence. All links remain
    model suggestions; no question is marked answered by this operation.
    """
    from copy import deepcopy
    output = deepcopy(original[:20])
    positions = {item.get('question', '').strip().casefold(): i for i, item in enumerate(output)
                 if isinstance(item, dict) and isinstance(item.get('question'), str)}
    for item in followup[:10]:
        if not isinstance(item, dict) or not isinstance(item.get('question'), str):
            continue
        text = item['question'].strip()
        if not text:
            continue
        refs = item.get('claim_indexes')
        mapped = [index_map[index] for index in refs if type(index) is int and index in index_map] if isinstance(refs, list) else []
        key = text.casefold()
        if key in positions:
            row = output[positions[key]]
            existing = row.get('claim_indexes') if isinstance(row.get('claim_indexes'), list) else []
            row['claim_indexes'] = list(dict.fromkeys([*existing, *mapped]))[:20]
        elif len(output) < 20:
            positions[key] = len(output)
            output.append({'question': text[:500], 'claim_indexes': list(dict.fromkeys(mapped))[:20]})
    return output
