"""Version-local task-to-evidence relations without semantic assertions.

All links come from already normalized, account-owned version data. A model's
subquestion or an identical passage is a lead, not proof of task coverage.
"""
from __future__ import annotations

import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any

_LIMIT = 50


def safe_model_evidence_needs(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:_LIMIT]:
        if not isinstance(item, dict):
            continue
        fields = {name: v.strip()[:500] for name in ('requested_fact', 'reason', 'source_kind', 'action')
                  if isinstance((v := item.get(name)), str) and v.strip()}
        index = item.get('question_index')
        if (not fields.get('requested_fact') or (index is not None and (type(index) is not int or index < 0)) or
                any(re.search(r'(?:https?://|www\.)', v, re.I) for v in fields.values())):
            continue
        question = item.get('question')
        question = question.strip()[:500] if isinstance(question, str) and question.strip() else None
        result.append({'question_index': index, 'question': question, **fields, 'origin': 'model_suggested',
                       **({'question_origin': 'unlinked_followup'} if item.get('question_origin') == 'unlinked_followup' else {})})
    return result


def derive_run_source_processing(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Record each stage's actual input ranges without renaming initial coverage."""
    initial = run.get('selected_segments')
    followup = run.get('gap_followup_segments')
    states = run.get('source_processing')
    if not isinstance(states, list) or not isinstance(initial, list):
        return []
    result = []
    for row in states[:_LIMIT]:
        if not isinstance(row, dict) or row.get('eligibility') != 'eligible':
            continue
        sid, length = row.get('source_id'), row.get('text_characters')
        if not isinstance(sid, str) or type(length) is not int or length < 0:
            continue
        stages = []
        all_segments = []
        for stage, segments in (('analysis', initial), ('gap_followup', followup)):
            if not isinstance(segments, list):
                continue
            ranges = []
            for item in segments:
                if not isinstance(item, dict) or item.get('source_id') != sid:
                    continue
                start, end = item.get('character_start'), item.get('character_end')
                if type(start) is int and type(end) is int and 0 <= start < end <= length:
                    span = {'start': start, 'end': end}
                    if span not in ranges:
                        ranges.append(span)
                        all_segments.append(item)
            if ranges:
                stages.append({'stage': stage, 'ranges': ranges})
        gap_status = run.get('gap_followup_status')
        unknown_followup = bool(run.get('gap_followup_requested')) and (
            gap_status not in {'completed', 'partial', 'no_evidence', 'not_needed', 'not_requested'}
            or (gap_status in {'completed', 'partial', 'no_evidence'} and not isinstance(followup, list)))
        coverage = 'unknown' if unknown_followup else initial_source_coverage(sid, length, all_segments)['status']
        result.append({'source_id': sid, 'stage_ranges': stages, 'coverage': coverage,
                       'meaning': 'analysis_and_gap_ranges_not_exhaustive_review'})
    return result


def derive_scope_assessment(*, run: dict[str, Any], claims: list[dict], limitations: list[str]) -> dict[str, Any]:
    """Describe observed input scope; never classify natural-language absence claims.

    Selection is not evidence that the model understood or exhaustively checked
    any source. Source identities and version-local indexes contain no source text.
    """
    states = run.get('source_processing')
    rows = [row for row in states if isinstance(row, dict)] if isinstance(states, list) else []
    sources = []
    for row in rows[:_LIMIT]:
        source_id = row.get('source_id')
        if not isinstance(source_id, str):
            continue
        selection = row.get('selection_status', row.get('initial_selection'))
        if selection not in {'complete', 'partial', 'not_selected', 'excluded'}:
            selection = 'unknown'
        status = row.get('analysis_status')
        if status not in {'produced', 'not_completed', 'not_started'}:
            status = 'unknown'
        provided = row.get('provided') if isinstance(row.get('provided'), bool) else None
        sources.append({'source_id': source_id, 'provided': provided,
                        'selection_status': selection, 'analysis_status': status})
    cumulative = run.get('run_source_processing')
    if isinstance(cumulative, list):
        by_source = {item.get('source_id'): item for item in cumulative if isinstance(item, dict)}
        for source in sources:
            row = by_source.get(source['source_id'])
            if row and any(stage.get('stage') == 'gap_followup' for stage in row.get('stage_ranges', []) if isinstance(stage, dict)):
                # It was selected somewhere in this run. Do not upgrade a
                # partial initial prompt to a complete single-source review.
                if source['selection_status'] in {'not_selected', 'unknown'}:
                    source['selection_status'] = 'partial'
    attempted = bool(run.get('operation_id'))
    if not sources or len(sources) != len(rows) or len(rows) > _LIMIT:
        scope = 'unknown'
    elif all(item['selection_status'] == 'complete' and item['analysis_status'] == 'produced' for item in sources):
        scope = 'complete_input_selected'
    else:
        scope = 'partial_or_unknown'
    claim_reviews = []
    for claim in claims[:_LIMIT]:
        if not isinstance(claim, dict) or not isinstance(claim.get('claim_id'), str):
            continue
        declaration = claim.get('scope_declaration')
        if declaration not in {'absence', 'other'}:
            declaration = 'unknown'
        review = claim.get('support_review') if isinstance(claim.get('support_review'), dict) else None
        review_declaration = review.get('scope_declaration') if review else None
        if review_declaration not in {'absence', 'other'}:
            review_declaration = 'unknown'
        absence = declaration == 'absence' or review_declaration == 'absence'
        claim_reviews.append({'claim_id': claim['claim_id'], 'declaration': declaration,
            'declaration_status': 'model_suggested' if declaration != 'unknown' else 'not_declared',
            'review_declaration': review_declaration,
            'review_status': ('not_requested' if not run.get('support_review_requested') else 'not_reviewed' if not review else
                              'not_declared' if review_declaration == 'unknown' else 'model_suggested'),
            'reported_claimed_scope': review.get('claimed_scope', 'unknown') if review else 'unknown',
            'available_review_scope': 'cited_excerpt_only' if review else 'unknown',
            'declaration_conflict': declaration != 'unknown' and review_declaration != 'unknown' and declaration != review_declaration,
            'assessment': 'not_established' if absence else
                          'not_independently_verified' if declaration == 'other' else 'unknown'})
    classified = sum(row['declaration'] != 'unknown' for row in claim_reviews)
    review_coverage = ('not_applicable' if not claims else 'not_declared' if not classified else
                       'complete_model_declarations' if classified == len(claims) and len(claims) <= _LIMIT else 'partial_model_declarations')
    reviewed = sum(row['review_status'] == 'model_suggested' for row in claim_reviews)
    review_scope_coverage = ('not_requested' if not run.get('support_review_requested') else 'not_applicable' if not claims else
                             'not_declared' if not reviewed else 'complete_model_declarations' if reviewed == len(claims) and len(claims) <= _LIMIT else 'partial_model_declarations')
    return {'schema_version': '1', 'meaning': 'input_selection_not_exhaustive_review',
            'scope': scope, 'attempted': attempted, 'sources': sources,
            'truncated': len(rows) > _LIMIT,
            'claim_scope_declarations': claim_reviews, 'claim_scope_coverage': review_coverage,
            'review_scope_coverage': review_scope_coverage,
            # This is a general review instruction, not a text classifier.
            'absence_conclusions': 'not_independently_verified',
            'claim_indexes_requiring_scope_review': list(range(min(len(claims), _LIMIT))) if attempted else [],
            'limitation_indexes_requiring_scope_review': list(range(min(len(limitations), _LIMIT))) if attempted else [],
            'indexes_truncated': len(claims) > _LIMIT or len(limitations) > _LIMIT}


def derive_evidence_needs(*, run: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    """Create actionable categories from recorded gaps without inventing sources."""
    needs = []
    if scope.get('attempted') and scope.get('scope') != 'complete_input_selected':
        needs.append({'evidence_type': 'unexamined_or_unselected_source_material',
                      'reason': 'source_selection_partial_or_unknown',
                      'action': 'inspect_or_supply_relevant_material',
                      'status': 'not_obtained_or_not_verified'})
    questions = run.get('research_questions')
    if isinstance(questions, list):
        indexes = [index for index, item in enumerate(questions) if isinstance(item, dict) and not item.get('claim_ids')]
        if indexes:
            needs.append({'evidence_type': 'material_for_unanswered_question',
                          'reason': 'no_recorded_finding_for_suggested_question',
                          'action': 'identify_and_supply_relevant_evidence',
                          'question_indexes': indexes[:_LIMIT], 'truncated': len(indexes) > _LIMIT,
                          'status': 'not_obtained_or_not_verified'})
    proposals = run.get('model_evidence_needs')
    if isinstance(proposals, list):
        for proposal in proposals[:_LIMIT]:
            if not isinstance(proposal, dict) or 'question_index' not in proposal:
                continue
            index = proposal.get('question_index')
            unlinked = proposal.get('question_origin') == 'unlinked_followup'
            name = proposal.get('question')
            if index is not None:
                needle = name.strip().casefold() if isinstance(name, str) else ''
                matches = [i for i, row in enumerate(questions or []) if isinstance(row, dict)
                           and isinstance(row.get('question'), str) and row['question'].strip().casefold() == needle] if needle else []
                if len(matches) == 1:
                    index = matches[0]
                else:
                    index, unlinked = None, True
            fields = {name: value.strip()[:500] for name in ('requested_fact', 'reason', 'source_kind', 'action')
                      if isinstance((value := proposal.get(name)), str) and value.strip()}
            if not fields.get('requested_fact'):
                continue
            # Suggestions are not source discovery; do not publish invented
            # locators supplied by a model as actionable source identifiers.
            if any(re.search(r'(?:https?://|www\.)', value, re.I) for value in fields.values()):
                continue
            needs.append({'evidence_type': 'model_suggested_material', 'model_question_index': index,
                          **({'question_origin': 'unlinked_followup'} if unlinked else {}),
                          'question_relation_status': 'not_verified',
                          **fields, 'origin': 'model_suggested', 'status': 'not_obtained_or_not_verified'})
    return {'schema_version': '1', 'meaning': 'review_actions_not_new_evidence', 'items': needs}


def derive_absence_assessment(*, run: dict[str, Any], scope: dict[str, Any], claims: list[dict] | None = None) -> dict[str, Any]:
    """Retain explicit model suggestions only as unestablished observations."""
    selected = {row['source_id']: row['selection_status'] for row in scope.get('sources', [])
                if isinstance(row, dict) and row.get('selection_status') in {'complete', 'partial'}}
    raw = run.get('model_absence_observations')
    observations = []
    if isinstance(raw, list):
        for item in raw[:_LIMIT]:
            if not isinstance(item, dict) or not isinstance(item.get('text'), str) or not item['text'].strip():
                continue
            source_ids = item.get('source_ids')
            source_ids = list(dict.fromkeys(sid for sid in source_ids if isinstance(sid, str) and sid in selected)) if isinstance(source_ids, list) else []
            declared = item.get('scope') if item.get('scope') in {'selected_excerpt', 'full_source', 'unknown'} else 'unknown'
            ref = item.get('observation_ref')
            claim_ids = [claim['claim_id'] for claim in (claims or [])
                         if isinstance(claim, dict) and isinstance(claim.get('claim_id'), str)
                         and isinstance(ref, str) and isinstance(claim.get('model_absence_refs'), list)
                         and ref in claim['model_absence_refs']]
            observations.append({'text': item['text'].strip()[:1000], 'source_ids': source_ids[:_LIMIT],
                                 'claim_ids': list(dict.fromkeys(claim_ids))[:_LIMIT],
                                 'declared_scope': declared, 'input_scope': scope.get('scope', 'unknown'),
                                 'origin': 'model_suggested', 'assessment': 'not_established'})
    return {'schema_version': '1', 'meaning': 'model_suggestions_not_verified_absence',
            'observations': observations, 'truncated': isinstance(raw, list) and len(raw) > _LIMIT}


def has_unresolved_task_gap(*, needs: dict[str, Any], absences: dict[str, Any], claims: list[dict]) -> bool:
    """Gate concrete reported gaps, without assigning relevance to unused sources."""
    if any(item.get('evidence_type') == 'model_suggested_material' and item.get('model_question_index') is None
           and item.get('question_origin') != 'unlinked_followup'
           for item in needs.get('items', []) if isinstance(item, dict)):
        return True
    # An explicitly emitted absence is a delivered observation even when the
    # model did not repeat it as a claim; it remains unestablished.
    return bool(absences.get('observations')) or any(
        isinstance(claim, dict) and (claim.get('scope_declaration') == 'absence' or
        (isinstance(claim.get('support_review'), dict) and claim['support_review'].get('scope_declaration') == 'absence'))
        for claim in claims)


def derive_completion_assessment(*, run: dict[str, Any], claims: list[dict]) -> dict[str, Any]:
    """Separate known execution gaps from unverified task and evidence quality."""
    requirements = run.get('requirement_coverage') if isinstance(run.get('requirement_coverage'), dict) else {}
    actions = requirements.get('requested_actions') if isinstance(requirements.get('requested_actions'), list) else []
    unmet = [item.get('action') for item in actions if isinstance(item, dict)
             and item.get('requirement_status') == 'unmet' and isinstance(item.get('action'), str)]
    needs = run.get('evidence_needs') if isinstance(run.get('evidence_needs'), dict) else {}
    absences = run.get('absence_assessment') if isinstance(run.get('absence_assessment'), dict) else {}
    reported_gap = has_unresolved_task_gap(needs=needs, absences=absences, claims=claims)
    structural_gaps = [row.get('requirement_id') for row in requirements.get('requirements', [])
                       if isinstance(row, dict) and any(gap in {'suggested_requirement_without_evidence_link',
                           'suggested_calculation_not_linked', 'calculation_input_unit_mismatch',
                           'calculation_input_currency_mismatch', 'calculation_input_period_mismatch',
                           'fact_unit_conflict', 'fact_currency_conflict', 'fact_value_conflict'} for gap in row.get('gaps', []))]
    evidence = run.get('evidence_links') if isinstance(run.get('evidence_links'), dict) else {}
    assertion_gaps = [row.get('claim_id') for row in evidence.get('claims', []) if isinstance(row, dict)
                      and isinstance(row.get('claim_id'), str) and any(
                          isinstance(part, dict) and part.get('link_status') in {'unlinked', 'invalid_or_unlocated'}
                          for part in (row.get('assertions') if isinstance(row.get('assertions'), list) else []))]
    return {'schema_version': '1',
            'execution_status': 'known_gap' if unmet or reported_gap or structural_gaps or assertion_gaps else 'no_recorded_execution_gap',
            'assertion_link_gaps': assertion_gaps[:_LIMIT],
            'unmet_requested_actions': unmet[:_LIMIT],
            'suggested_requirement_gaps': structural_gaps[:_LIMIT],
            'model_reported_task_gap': reported_gap,
            'task_coverage': 'not_independently_verified',
            'source_semantics': 'not_independently_verified',
            'meaning': 'execution_record_not_task_quality_verdict'}


def initial_source_coverage(source_id: str, text_length: int, segments: object) -> dict:
    """Count distinct selected code points; this is not evidence of comprehension."""
    result = {'method': 'initial_prompt_ranges_v1', 'text_characters': text_length,
              'selected_characters': None, 'status': 'unknown'}
    if not isinstance(segments, list) or text_length <= 0:
        return result
    intervals: list[tuple[int, int]] = []
    for item in segments:
        if not isinstance(item, dict) or item.get('source_id') != source_id:
            continue
        start, end = item.get('character_start'), item.get('character_end')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= text_length:
            return result
        intervals.append((start, end))
    if not intervals:
        return result
    intervals.sort()
    selected, left, right = 0, *intervals[0]
    for start, end in intervals[1:]:
        if start <= right:
            right = max(right, end)
        else:
            selected += right - left
            left, right = start, end
    selected += right - left
    result.update({'selected_characters': selected, 'status': 'complete' if selected == text_length else 'partial'})
    return result


def derive_requirement_coverage(*, run: dict[str, Any], has_user_question: bool,
                                facts: list[dict] | None = None, calculations: list[dict] | None = None) -> dict[str, Any]:
    calculation_rows = calculations or []
    original_facts = facts or []
    question_links = run.get('question_links') if isinstance(run.get('question_links'), dict) else {}
    evidence_links = run.get('evidence_links') if isinstance(run.get('evidence_links'), dict) else {}
    by_claim = {row.get('claim_id'): row for row in evidence_links.get('claims', [])
                if isinstance(row, dict) and isinstance(row.get('claim_id'), str)}
    rows = question_links.get('questions') if isinstance(question_links.get('questions'), list) else []
    requirements: list[dict[str, Any]] = [{
        'requirement_id': 'user_request', 'origin': 'user' if has_user_question else 'default_task',
        'question_index': None, 'finding_ids': [], 'source_ids': [], 'candidate_fact_ids': [],
        'candidate_calculation_indexes': [], 'counterpoints': [], 'relation_status': 'not_assessed',
        'semantic_status': 'not_independently_verified', 'gaps': ['request_decomposition_not_verified'],
    }]
    truncated = bool(question_links.get('truncated') or evidence_links.get('truncated') or len(rows) > _LIMIT)
    for row in rows[:_LIMIT]:
        if not isinstance(row, dict) or type(row.get('question_index')) is not int:
            continue
        findings = [item for item in row.get('finding_ids', []) if isinstance(item, str) and item in by_claim]
        fact_ids: list[str] = []
        calculations: list[int] = []
        for claim_id in findings:
            claim = by_claim[claim_id]
            for citation in claim.get('citations', []):
                if isinstance(citation, dict):
                    fact_ids.extend(item for item in citation.get('same_passage_fact_ids', []) if isinstance(item, str))
            for relation in claim.get('user_fact_links', []):
                if isinstance(relation, dict) and isinstance(relation.get('fact_id'), str):
                    fact_ids.append(relation['fact_id'])
            calculations.extend(item for item in claim.get('candidate_calculation_indexes', []) if type(item) is int)
        sources = [item for item in row.get('source_ids', []) if isinstance(item, str)]
        counters = [item for item in row.get('counterpoints', []) if isinstance(item, dict)]
        facts = list(dict.fromkeys(fact_ids))
        calculations = list(dict.fromkeys(calculations))
        truncated = truncated or any(len(items) > _LIMIT for items in (findings, sources, counters, facts, calculations))
        gaps = ([] if findings else ['no_recorded_finding'])
        if findings and not sources:
            gaps.append('no_recorded_source')
        if findings and run.get('require_counterarguments') is True and not counters:
            gaps.append('no_recorded_counterpoint_relation')
        requirements.append({'requirement_id': f"question:{row['question_index']}", 'origin': 'model_suggested',
            'question_index': row['question_index'], 'finding_ids': findings[:_LIMIT], 'source_ids': sources[:_LIMIT],
            'candidate_fact_ids': facts[:_LIMIT], 'candidate_calculation_indexes': calculations[:_LIMIT],
            'counterpoints': counters[:_LIMIT], 'relation_status': 'linked_suggestion' if findings else 'unlinked',
            'semantic_status': 'not_independently_verified', 'gaps': gaps})
    # A planning dimension or model link is a suggestion, not proof that any
    # requirement was satisfied. Rebind only to unique current identities.
    questions = run.get('research_questions') if isinstance(run.get('research_questions'), list) else []
    def key(value: object) -> str:
        return value.strip().casefold() if isinstance(value, str) else ''
    question_keys = [key(item.get('question')) if isinstance(item, dict) else '' for item in questions]
    question_counts = Counter(question_keys)
    proposals = run.get('planning_requirements') if isinstance(run.get('planning_requirements'), list) else []
    analysis_proposals = run.get('analysis_requirements') if isinstance(run.get('analysis_requirements'), list) else []
    for candidates in (proposals, analysis_proposals):
        counts = Counter(key(item.get('question')) for item in candidates if isinstance(item, dict))
        for proposal in candidates[:_LIMIT]:
            if not isinstance(proposal, dict):
                continue
            name = key(proposal.get('question'))
            if not name or question_counts[name] != 1 or counts[name] != 1:
                continue
            question_index = question_keys.index(name)
            target = next((item for item in requirements if item.get('question_index') == question_index), None)
            dimensions = proposal.get('dimensions')
            if target is None or not isinstance(dimensions, dict):
                continue
            existing = target.setdefault('dimensions', {})
            for field in ('entities', 'metrics', 'periods', 'units', 'qualifiers'):
                values = dimensions.get(field)
                if not existing.get(field) and isinstance(values, list):
                    existing[field] = [value.strip()[:200] for value in values[:20]
                                       if isinstance(value, str) and value.strip()]
            target['dimensions_status'] = 'model_suggested_not_verified'
            hints = proposal.get('retrieval_hints')
            if not target.get('retrieval_hints') and isinstance(hints, list):
                target['retrieval_hints'] = [value.strip()[:160] for value in hints[:6]
                                             if isinstance(value, str) and value.strip()]
                target['retrieval_hints_status'] = 'model_suggested_not_verified'
            if target.get('calculation_requested') is None and type(proposal.get('calculation_requested')) is bool:
                target['calculation_requested'] = proposal['calculation_requested']
    fact_rows = [item for item in original_facts if isinstance(item, dict) and isinstance(item.get('fact_id'), str)]
    fact_counts = Counter(item['fact_id'] for item in fact_rows)
    ranges: dict[str, list[tuple[int, int]]] = {}
    for row in run.get('run_source_processing', []):
        if not isinstance(row, dict) or not isinstance(row.get('source_id'), str):
            continue
        for stage in row.get('stage_ranges', []):
            if not isinstance(stage, dict):
                continue
            for span in stage.get('ranges', []):
                if isinstance(span, dict) and type(span.get('start')) is int and type(span.get('end')) is int:
                    ranges.setdefault(row['source_id'], []).append((span['start'], span['end']))
    valid_facts = {}
    for fact in fact_rows:
        loc = fact.get('location')
        if fact_counts[fact['fact_id']] != 1 or not isinstance(loc, dict) or loc.get('status') == 'ambiguous':
            continue
        start, end = loc.get('character_start'), loc.get('character_end')
        if type(start) is int and type(end) is int and any(a <= start < end <= b for a, b in ranges.get(fact.get('source_id'), [])):
            valid_facts[fact['fact_id']] = fact
    valid_calculations = set()
    for index, calculation in enumerate(calculation_rows):
        if not isinstance(calculation, dict) or not isinstance(calculation.get('inputs'), list):
            continue
        inputs = calculation['inputs']
        if len(inputs) == 2 and calculation.get('result_status') == 'matched' and all(
                isinstance(item, dict) and item.get('fact_id') in valid_facts and
                all(item.get(field) == valid_facts[item['fact_id']].get(field) for field in ('value', 'unit', 'currency', 'period'))
                for item in inputs):
            valid_calculations.add(index)
    links = run.get('model_requirement_links') if isinstance(run.get('model_requirement_links'), list) else []
    for link in links[:_LIMIT]:
        if not isinstance(link, dict):
            continue
        index, question = link.get('question_index'), link.get('question')
        if index is None and question is None:
            target = requirements[0]
        else:
            name = key(question)
            if type(index) is not int or not name or question_counts[name] != 1:
                continue
            final_index = question_keys.index(name)
            target = next((item for item in requirements if item.get('question_index') == final_index), None)
            if target is None:
                continue
        for field, allowed in (('fact_ids', valid_facts), ('calculation_indexes', valid_calculations), ('source_ids', ranges)):
            values = link.get(field)
            values = values if isinstance(values, list) else []
            checked = [value for value in values if (type(value) is int if field == 'calculation_indexes' else isinstance(value, str)) and value in allowed]
            dest = {'fact_ids': 'candidate_fact_ids', 'calculation_indexes': 'candidate_calculation_indexes', 'source_ids': 'source_ids'}[field]
            target[dest] = list(dict.fromkeys([*target[dest], *checked]))[:_LIMIT]
            if field == 'calculation_indexes':
                for value in values:
                    if type(value) is not int or not 0 <= value < len(calculation_rows):
                        continue
                    calculation = calculation_rows[value]
                    if not isinstance(calculation, dict) or not isinstance(calculation.get('inputs'), list):
                        continue
                    for item in calculation['inputs'][:20]:
                        fact = valid_facts.get(item.get('fact_id')) if isinstance(item, dict) else None
                        if not fact:
                            continue
                        for dimension in ('unit', 'currency', 'period'):
                            left, right = item.get(dimension), fact.get(dimension)
                            if isinstance(left, str) and left.strip() and isinstance(right, str) and right.strip() and left.strip().casefold() != right.strip().casefold():
                                target['gaps'] = list(dict.fromkeys([*target['gaps'], 'calculation_input_' + dimension + '_mismatch']))
        target['model_relation_status'] = 'suggested_not_independently_verified'
    for target in requirements[1:]:
        if 'dimensions_status' not in target:
            continue
        if not target['candidate_fact_ids'] and not target['finding_ids']:
            target['gaps'] = list(dict.fromkeys([*target['gaps'], 'suggested_requirement_without_evidence_link']))
        if target.get('calculation_requested') is True and not target['candidate_calculation_indexes']:
            target['gaps'] = list(dict.fromkeys([*target['gaps'], 'suggested_calculation_not_linked']))
    # Only compare explicit, identical entity/metric/period identities. Different
    # periods, unknown identity or missing values are not evidence of conflict.
    for target in requirements:
        groups: dict[tuple[str, str, str], list[dict]] = {}
        for fact_id in target.get('candidate_fact_ids', []):
            fact = valid_facts.get(fact_id)
            meta = fact.get('structured_metadata') if isinstance(fact, dict) else None
            if not isinstance(meta, dict):
                continue
            identity = (meta.get('entity_id'), meta.get('metric_id'), fact.get('period'))
            if not all(isinstance(item, str) and item.strip() for item in identity):
                continue
            group = tuple(item.strip().casefold() for item in identity)
            groups.setdefault(group, []).append(fact)
        conflicts = []
        for members in groups.values():
            if len(members) < 2:
                continue
            for dimension in ('unit', 'currency'):
                values = {value.strip().casefold() for fact in members
                          if isinstance((value := fact.get(dimension)), str) and value.strip()}
                if len(values) > 1:
                    conflicts.append({'dimension': dimension, 'fact_ids': [f['fact_id'] for f in members[:20]],
                                      'status': 'requires_context_review'})
                    target['gaps'] = list(dict.fromkeys([*target['gaps'], 'fact_' + dimension + '_conflict']))
            units = {(str(f.get('unit') or '').strip().casefold(), str(f.get('currency') or '').strip().casefold()) for f in members}
            values = set()
            for fact in members:
                try:
                    value = Decimal(str(fact.get('value')))
                    if value.is_finite():
                        values.add(value)
                except (InvalidOperation, ValueError, TypeError):
                    pass
            if len(units) == 1 and all(isinstance(f.get('unit'), str) and f['unit'].strip() and isinstance(f.get('currency'), str) and f['currency'].strip() for f in members) and len(values) > 1:
                conflicts.append({'dimension': 'value', 'fact_ids': [f['fact_id'] for f in members[:20]],
                                  'status': 'requires_revision_and_context_review'})
                target['gaps'] = list(dict.fromkeys([*target['gaps'], 'fact_value_conflict']))
        if conflicts:
            target['fact_conflicts'] = conflicts[:20]
    requested_actions = []
    for action, requested, status, completed in (
        ('planning', 'planning_requested', 'planning_status', {'completed'}),
        ('counterpoint', 'counterpoint_requested', 'counterpoint_status', {'completed', 'not_needed'}),
        ('gap_search', 'gap_search_requested', 'gap_search_status', {'completed', 'no_gaps'}),
        ('gap_followup', 'gap_followup_requested', 'gap_followup_status', {'completed', 'not_needed'}),
        ('support_review', 'support_review_requested', 'support_review_status', {'complete'}),
    ):
        if run.get(requested) is True:
            recorded_status = run.get(status)
            requested_actions.append({'action': action,
                'execution_status': recorded_status if isinstance(recorded_status, str) else 'unknown',
                'requirement_status': 'executed' if recorded_status in completed else 'unmet'})
    return {'schema_version': '1', 'meaning': 'recorded_links_not_semantic_verification',
            'truncated': truncated, 'requirements': requirements, 'requested_actions': requested_actions}
