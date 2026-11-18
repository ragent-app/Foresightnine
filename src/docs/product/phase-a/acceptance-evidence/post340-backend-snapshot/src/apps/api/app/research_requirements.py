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
    seen = set()
    for item in candidates:
        if not isinstance(item, dict) or not isinstance(item.get('question'), str):
            continue
        key = item['question'].strip().casefold()
        if key and key not in seen:
            seen.add(key)
            output.append(item)
        if len(output) == 10:
            break
    return tuple(output)


def requirement_retrieval_facets(requirements: tuple[dict, ...] | list[dict]) -> list[str]:
    """Untrusted task dimensions for the existing bounded lexical index."""
    output = []
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
        parts.append(item['question'].strip()[:300])
        facet = ' '.join(parts)[:300]
        if facet and facet not in output:
            output.append(facet)
    return output


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
    return result


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
