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


def gap_context_refs(target: GapFocus, analysis, claims: list[dict]) -> list[dict]:
    """Locate existing evidence relevant to a gap, without expanding visibility."""
    refs = []
    for index in target.claim_indexes:
        if type(index) is not int or not 0 <= index < len(claims) or not isinstance(claims[index], dict):
            continue
        for citation in claims[index].get('citations', []):
            if isinstance(citation, dict):
                refs.append(citation)
    questions = [analysis.research_questions[index].get('question') for index in target.question_indexes
                 if 0 <= index < len(analysis.research_questions) and isinstance(analysis.research_questions[index], dict)]
    fact_ids = {fact_id for link in analysis.requirement_links if isinstance(link, dict) and link.get('question') in questions
                for fact_id in link.get('fact_ids', []) if isinstance(fact_id, str)}
    refs.extend(fact for fact in analysis.facts if isinstance(fact, dict) and fact.get('fact_id') in fact_ids)
    output = []
    for item in refs[:50]:
        location = item.get('location')
        if not isinstance(location, dict) or location.get('status') == 'ambiguous':
            continue
        sid, start, end = item.get('source_id'), location.get('character_start'), location.get('character_end')
        if isinstance(sid, str) and type(start) is int and type(end) is int and 0 <= start < end:
            output.append({'source_id': sid, 'character_start': start, 'character_end': end})
    return output


def build_gap_focus(question: str, claims: list[dict], research_questions: list[dict] | None = None,
                    max_items: int = 3, requirement_gaps: dict[int, tuple[str, ...]] | None = None,
                    original_gap: bool = False, need_details: dict[int | None, tuple[str, ...]] | None = None,
                    preferred_question: str | None = None) -> GapFocus | None:
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
                  and (not item.get('claim_indexes') or index in (requirement_gaps or {}))]
    if preferred_question:
        preferred = [(index, text) for index, text in unanswered if text == preferred_question.strip()[:300]]
        if preferred:
            unanswered = preferred
    selected_questions = unanswered[:max_items]
    selected = candidates[:max_items - len(selected_questions)]
    if not selected and not selected_questions:
        if claims and not original_gap:
            return None
        # A successful analysis with no retained finding still leaves the
        # user's original request unresolved, even without a model plan.
        reason = ('A model suggested an unresolved evidence need for the original request.' if original_gap else
                  'No finding was retained; inspect the authorized source evidence for the original request.')
        details = '\n'.join((need_details or {}).get(None, ()))
        return GapFocus((), (), question.strip() + '\n' + reason + ('\nUntrusted evidence needs: ' + details if details else ''),
                        ' '.join(question.strip().split()))
    # Text here is untrusted model/user data, never system instructions and
    # never sent to an external search provider by this helper.
    snippets = '\n'.join([*(['A model suggested an unresolved evidence need for the original request.'] if original_gap else []),
                          *(f'question [{index}] {text}' +
                            (f" (recorded missing links: {', '.join(requirement_gaps[index])})"
                             if requirement_gaps and index in requirement_gaps else '')
                            for index, text in selected_questions),
                          *(f'finding [{index}] {text}' for index, text in selected)])
    needs = [*(need_details or {}).get(None, ()),
             *(detail for index, _ in selected_questions for detail in (need_details or {}).get(index, ()))]
    if needs:
        snippets += '\nUntrusted model-suggested evidence needs:\n' + '\n'.join(needs[:6])
    combined = (question.strip() + '\nUntrusted prior subquestions and findings requiring additional source evidence; '
                'do not assume they are true or complete:\n' + snippets)
    # Sending this query outside the process requires separate user consent.
    # It may include user or model text derived from private materials.
    detail = (selected_questions or selected)[0][1]
    query = ' '.join(detail.split())
    return GapFocus(tuple(index for index, _ in selected), tuple(index for index, _ in selected_questions), combined, query)


def remap_followup_artifacts(facts: list[dict], calculations: list[dict], *, operation_id: str,
                             existing_fact_ids: set[str], existing_facts: list[dict],
                             max_facts: int, max_calculations: int,
                             fact_id_map: dict[str, str] | None = None,
                             calculation_index_map: dict[int, int] | None = None,
                             retained_facts: dict[str, str] | None = None,
                             retained_calculations: set[int] | None = None) -> tuple[list[dict], list[dict], int]:
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
        if old_id in (retained_facts or {}):
            mapping[old_id] = retained_facts[old_id]
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
    for original_index, calculation in enumerate(calculations):
        if original_index in (retained_calculations or set()):
            continue
        inputs = calculation.get('inputs') if isinstance(calculation, dict) else None
        if (len(output_calculations) >= max_calculations or not isinstance(inputs, list) or len(inputs) != 2 or
                any(not isinstance(item, dict) or item.get('fact_id') not in mapping for item in inputs)):
            omitted += 1
            continue
        copied = deepcopy(calculation)
        for item in copied['inputs']:
            item['fact_id'] = mapping[item['fact_id']]
        if calculation_index_map is not None:
            calculation_index_map[original_index] = len(output_calculations)
        output_calculations.append(copied)
    if fact_id_map is not None:
        fact_id_map.update(mapping)
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
