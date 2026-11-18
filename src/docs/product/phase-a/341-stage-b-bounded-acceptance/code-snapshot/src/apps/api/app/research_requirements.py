"""Validate model-proposed requirement links against this run's artifacts.

These links establish identity and selected-text provenance only, never that a
fact or calculation semantically answers a requirement.
"""
from __future__ import annotations

from collections import Counter
from typing import Any


def combined_requirements(planned: tuple[dict, ...] | list[dict], analysis: Any) -> tuple[dict, ...]:
    """Use existing question identities, with planned suggestions taking priority."""
    candidates = [*planned, *(getattr(analysis, 'discovered_requirements', []) or [])]
    output = []
    seen: dict[str, int] = {}
    for item in candidates:
        if not isinstance(item, dict) or not isinstance(item.get('question'), str):
            continue
        key = item['question'].strip().casefold()
        if not key:
            continue
        if key not in seen:
            if len(output) == 10:
                continue
            seen[key] = len(output)
            output.append(dict(item))
        else:
            previous = output[seen[key]]
            hints = [*(previous.get('retrieval_hints') or []), *(item.get('retrieval_hints') or [])]
            previous['retrieval_hints'] = list(dict.fromkeys(value.strip()[:160] for value in hints
                                                             if isinstance(value, str) and value.strip()))[:6]
    return tuple(output)


def requirement_retrieval_facets(requirements: tuple[dict, ...] | list[dict]) -> list[str]:
    """Untrusted task dimensions for the existing bounded lexical index."""
    queues = []
    for item in requirements[:6]:
        if not isinstance(item, dict) or not isinstance(item.get('question'), str):
            continue
        dimensions = item.get('dimensions')
        parts = []
        if isinstance(dimensions, dict):
            for key in ('entities', 'metrics', 'periods', 'units', 'qualifiers'):
                values = dimensions.get(key)
                if isinstance(values, list):
                    parts.extend(value.strip()[:60] for value in values[:3] if isinstance(value, str) and value.strip())
        # The unabridged question remains separately in the model request and
        # the index objective. Put the dimensional hints first within the
        # index's bounded facet input so a long question cannot erase them.
        question = item['question'].strip()[:200]
        queues.append(([(' '.join([parts[0], question]))[:300], *parts[1:]] if parts else [question]))
    output = []
    # Each requirement gets one opportunity before another gets an extra
    # dimension; a term hit remains a retrieval hint, not a coverage verdict.
    for position in range(16):
        for queue in queues:
            if position < len(queue) and queue[position] and queue[position] not in output:
                output.append(queue[position])
                if len(output) >= 12:
                    return output
    return output


def followup_retrieval_hints(planned_hints: tuple[str, ...], requirements: tuple[dict, ...]) -> tuple[str, ...]:
    groups = [list(planned_hints), *[item.get('retrieval_hints', []) for item in requirements if isinstance(item, dict)]]
    output = []
    for index in range(6):
        for group in groups:
            if not isinstance(group, (list, tuple)) or index >= len(group) or not isinstance(group[index], str):
                continue
            hint = group[index].strip()[:160]
            if hint and hint not in output:
                output.append(hint)
                if len(output) == 6:
                    return tuple(output)
    return tuple(output)


def unresolved_requirement_indexes(analysis: Any, requirements: tuple[dict, ...] | list[dict]) -> dict[int, tuple[str, ...]]:
    """Identify recorded structural gaps, not semantic incompleteness."""
    questions = getattr(analysis, 'research_questions', []) or []
    links = getattr(analysis, 'requirement_links', []) or []
    normalize = lambda value: value.strip().casefold() if isinstance(value, str) else ''
    names = [normalize(q.get('question')) if isinstance(q, dict) else '' for q in questions]
    plan_counts = Counter(normalize(p.get('question')) for p in requirements if isinstance(p, dict))
    result: dict[int, tuple[str, ...]] = {}
    for proposal in requirements[:10]:
        if not isinstance(proposal, dict):
            continue
        name = normalize(proposal.get('question'))
        if not name or names.count(name) != 1 or plan_counts[name] != 1:
            continue
        index = names.index(name)
        related = [link for link in links if isinstance(link, dict) and normalize(link.get('question')) == name]
        dimensions = proposal.get('dimensions')
        # A concrete requested metric/period/value is a reason to seek facts;
        # subject-only qualitative questions need not invent numeric facts.
        needs_fact = isinstance(dimensions, dict) and any(dimensions.get(field) for field in ('metrics', 'periods', 'units'))
        reasons = []
        if needs_fact and not any(link.get('fact_ids') for link in related):
            reasons.append('no_linked_fact')
        if proposal.get('calculation_requested') is True and not any(link.get('calculation_indexes') for link in related):
            reasons.append('no_linked_calculation')
        if not any(link.get('source_ids') for link in related):
            reasons.append('no_linked_selected_source')
        if reasons:
            result[index] = tuple(reasons)
    for need in getattr(analysis, 'evidence_needs', []) or []:
        if not isinstance(need, dict) or type(need.get('question_index')) is not int:
            continue
        name = normalize(need.get('question'))
        # The model's original index can move after plan/follow-up merges.
        # Never attach a stale or ambiguous index to another question.
        if name and names.count(name) == 1:
            index = names.index(name)
            result[index] = tuple(dict.fromkeys([*result.get(index, ()), 'model_suggested_evidence_need']))
    return result


def unresolved_original_requirement(analysis: Any) -> bool:
    """A model's unlinked original-request need is a lead, never proven absence."""
    return any(isinstance(need, dict) and need.get('question_index') is None and
               need.get('question_origin') != 'unlinked_followup'
               for need in (getattr(analysis, 'evidence_needs', []) or []))


def evidence_need_details(analysis: Any) -> dict[int | None, tuple[str, ...]]:
    """Bounded untrusted gap descriptions, bound only to unique question identities."""
    questions = getattr(analysis, 'research_questions', []) or []
    names = [item.get('question', '').strip().casefold() if isinstance(item, dict) and isinstance(item.get('question'), str)
             else '' for item in questions]
    grouped: dict[int | None, list[str]] = {}
    for need in (getattr(analysis, 'evidence_needs', []) or [])[:20]:
        if not isinstance(need, dict) or need.get('question_origin') == 'unlinked_followup':
            continue
        original_index = need.get('question_index')
        if original_index is None:
            target = None
        else:
            name = need.get('question')
            name = name.strip().casefold() if isinstance(name, str) else ''
            if type(original_index) is not int or not name or names.count(name) != 1:
                continue
            target = names.index(name)
        parts = [f'{field}: {need[field].strip()[:160]}' for field in ('requested_fact', 'reason', 'source_kind', 'action')
                 if isinstance(need.get(field), str) and need[field].strip()]
        if parts:
            grouped.setdefault(target, []).append('; '.join(parts))
    return {key: tuple(values[:3]) for key, values in grouped.items()}


def validate_requirement_links(proposals: list[dict], questions: list[dict], facts: list[dict],
                               calculations: list[dict], segments: list[dict],
                               source_refs: dict[str, str], calculation_indexes: dict[int, int]) -> list[dict]:
    counts = Counter(f.get('fact_id') for f in facts if isinstance(f, dict) and isinstance(f.get('fact_id'), str))
    eligible: dict[str, dict] = {}
    for fact in facts:
        if not isinstance(fact, dict) or counts.get(fact.get('fact_id')) != 1:
            continue
        location = fact.get('location')
        if not isinstance(location, dict) or location.get('status') == 'ambiguous':
            continue
        start, end = location.get('character_start'), location.get('character_end')
        if type(start) is not int or type(end) is not int or start >= end:
            continue
        if any(isinstance(s, dict) and s.get('source_id') == fact.get('source_id') and
               type(s.get('character_start')) is int and type(s.get('character_end')) is int and
               s['character_start'] <= start < end <= s['character_end'] for s in segments):
            eligible[fact['fact_id']] = fact
    selected_sources = {s.get('source_id') for s in segments if isinstance(s, dict)}
    output: list[dict] = []
    for proposal in proposals:
        index = proposal.get('question_index')
        if index is not None and (type(index) is not int or not 0 <= index < len(questions)):
            continue
        question = questions[index].get('question') if index is not None else None
        fact_ids = list(dict.fromkeys(item for item in proposal.get('fact_ids', []) if item in eligible))
        calc_indexes: list[int] = []
        for proposed in proposal.get('calculation_indexes', []):
            target = calculation_indexes.get(proposed) if type(proposed) is int else None
            if target is None or not 0 <= target < len(calculations):
                continue
            inputs = calculations[target].get('inputs')
            if not isinstance(inputs, list) or len(inputs) != 2:
                continue
            ids = [item.get('fact_id') for item in inputs if isinstance(item, dict)]
            if len(ids) == 2 and all(item in eligible for item in ids):
                calc_indexes.append(target)
        source_ids = list(dict.fromkeys(source_refs[item] for item in proposal.get('source_ids', [])
                                        if item in source_refs and source_refs[item] in selected_sources))
        source_ids = list(dict.fromkeys([*source_ids, *(eligible[item]['source_id'] for item in fact_ids)]))
        output.append({'question_index': index, 'question': question, 'fact_ids': fact_ids,
                       'calculation_indexes': list(dict.fromkeys(calc_indexes)), 'source_ids': source_ids,
                       'status': 'model_suggested', 'semantic_status': 'not_independently_verified'})
    return output
