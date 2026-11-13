"""Derive bounded, version-local links from an existing research snapshot.

Links expose recorded provenance only. A shared passage does not establish
that a finding is true, that a fact belongs to its metric, or that either
supports the other semantically. No source content is loaded or exported.
"""
from __future__ import annotations

from collections import Counter
import json
from typing import Any
from uuid import uuid4


MAX_ENTRIES = 2000
MAX_CITATIONS = 10
MAX_INPUTS = 20
MAX_FACT_MATCHES = 20
MAX_QUESTION_LINKS = 50


def _location_key(source_id: object, location: object) -> tuple[str, int, int] | None:
    if not isinstance(source_id, str) or not source_id or not isinstance(location, dict):
        return None
    start, end = location.get('character_start'), location.get('character_end')
    if location.get('status') or type(start) is not int or type(end) is not int or not 0 <= start < end:
        return None
    return source_id, start, end


def normalize_claim_links(claims: list[dict], previous_claims: list[dict], *, model_generated: bool = False) -> None:
    """Assign server-controlled identities and retain only local claim links.

    Links express a recorded relationship, not semantic agreement or support.
    Legacy claims receive IDs in the next version; prior versions are untouched.
    """
    previous_ids = {entry.get('claim_id') for entry in previous_claims
                    if isinstance(entry, dict) and isinstance(entry.get('claim_id'), str)}
    # Generated checkpoints are created from an in-memory analysis, which
    # does not contain IDs assigned to the previous persisted checkpoint.
    # Reuse an ID only for a unique, exactly unchanged finding on both sides.
    # This keeps duplicate or ambiguous entries separate and does not infer
    # semantic equivalence between different wording or citations.
    def identity(item: object) -> str | None:
        if not isinstance(item, dict) or not isinstance(item.get('text'), str):
            return None
        try:
            return json.dumps([item.get('text'), item.get('role'), item.get('citations')],
                              ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        except (TypeError, ValueError):
            return None

    previous_keys = [identity(item) for item in previous_claims]
    current_keys = [identity(item) for item in claims]
    previous_counts = Counter(key for key in previous_keys if key is not None)
    current_counts = Counter(key for key in current_keys if key is not None)
    previous_by_key = {key: item.get('claim_id') for key, item in zip(previous_keys, previous_claims)
                       if key is not None and previous_counts[key] == 1 and isinstance(item, dict)
                       and isinstance(item.get('claim_id'), str)}
    used: set[str] = set()
    for claim, key in zip(claims, current_keys):
        if not isinstance(claim, dict):
            continue
        candidate = claim.get('claim_id')
        if not isinstance(candidate, str) or candidate not in previous_ids or candidate in used:
            candidate = previous_by_key.get(key) if model_generated and key is not None and current_counts[key] == 1 else None
            if candidate is None or candidate in used:
                candidate = 'claim_' + uuid4().hex
        claim['claim_id'] = candidate
        used.add(candidate)
    supports = [claim for claim in claims if isinstance(claim, dict) and claim.get('role') != 'counterargument']
    support_ids = {claim['claim_id'] for claim in supports}
    current_by_id = {claim['claim_id']: claim for claim in claims if isinstance(claim, dict)}
    previous_by_id = {claim['claim_id']: claim for claim in previous_claims
                      if isinstance(claim, dict) and isinstance(claim.get('claim_id'), str)}
    def same_finding(left: dict | None, right: dict | None) -> bool:
        return bool(left is not None and right is not None and all(
            left.get(key) == right.get(key) for key in ('text', 'role', 'citations')))
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        raw = claim.pop('responds_to_claim_ids', [])
        proposed_indexes = claim.pop('suggested_responds_to_claim_indexes', [])
        # Client-submitted suggestions are never trusted as model output.
        claim.pop('suggested_responds_to_claim_ids', None)
        claim.pop('responds_to_omitted', None)
        if claim.get('role') != 'counterargument':
            continue
        prior = previous_by_id.get(claim['claim_id'])
        suggested: list[str] = []
        if model_generated and isinstance(proposed_indexes, list):
            for index in proposed_indexes[:20]:
                if type(index) is int and 0 <= index < len(supports):
                    target = supports[index]['claim_id']
                    if target not in suggested:
                        suggested.append(target)
        elif same_finding(claim, prior):
            old_targets = prior.get('suggested_responds_to_claim_ids', []) if prior else []
            if isinstance(old_targets, list):
                for target in old_targets[:20]:
                    if (isinstance(target, str) and target in support_ids and target not in suggested and
                            same_finding(previous_by_id.get(target), current_by_id.get(target))):
                        suggested.append(target)
        if suggested:
            claim['suggested_responds_to_claim_ids'] = suggested
        if not isinstance(raw, list):
            claim['responds_to_omitted'] = 1
            continue
        kept: list[str] = []
        omitted = 0
        for target in raw[:20]:
            if isinstance(target, str) and target in support_ids and target not in kept:
                kept.append(target)
            else:
                omitted += 1
        omitted += max(0, len(raw) - 20)
        if kept:
            claim['responds_to_claim_ids'] = kept
        if omitted:
            claim['responds_to_omitted'] = omitted


def derive_evidence_links(claims: list[dict], facts: list[dict], calculations: list[dict]) -> dict[str, Any]:
    """Build links using only recorded source locations and explicit inputs.

    Indexes apply only to this immutable version. Missing or duplicate fact
    IDs do not become links. Exact passage matches are candidates requiring
    semantic and source review, not assertions of support.
    """
    facts_truncated = len(facts) > MAX_ENTRIES
    facts = facts[:MAX_ENTRIES]
    counts = Counter(fact.get('fact_id') for fact in facts
                     if isinstance(fact, dict) and isinstance(fact.get('fact_id'), str) and fact.get('fact_id'))
    by_location: dict[tuple[str, int, int], list[str]] = {}
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        fact_id = fact.get('fact_id')
        key = _location_key(fact.get('source_id'), fact.get('location'))
        if key is not None and isinstance(fact_id, str) and counts[fact_id] == 1:
            by_location.setdefault(key, []).append(fact_id)

    claim_links = []
    truncated = facts_truncated or len(claims) > MAX_ENTRIES or len(calculations) > MAX_ENTRIES
    for index, claim in enumerate(claims[:MAX_ENTRIES]):
        if not isinstance(claim, dict):
            continue
        raw_citations = claim.get('citations')
        citations = raw_citations if isinstance(raw_citations, list) else []
        truncated = truncated or len(citations) > MAX_CITATIONS
        links = []
        for citation_index, citation in enumerate(citations[:MAX_CITATIONS]):
            if not isinstance(citation, dict):
                continue
            source_id = citation.get('source_id')
            key = _location_key(source_id, citation.get('location'))
            same_passage = by_location.get(key, []) if key is not None else []
            truncated = truncated or len(same_passage) > MAX_FACT_MATCHES
            links.append({'citation_index': citation_index,
                          'source_id': source_id if isinstance(source_id, str) else None,
                          'location_recorded': key is not None,
                          'same_passage_fact_ids': same_passage[:MAX_FACT_MATCHES]})
        claim_links.append({'claim_index': index, 'claim_id': claim.get('claim_id'),
                            'responds_to_claim_ids': claim.get('responds_to_claim_ids', []),
                            'suggested_responds_to_claim_ids': claim.get('suggested_responds_to_claim_ids', []),
                            'user_fact_links': claim.get('user_fact_links', []),
                            'role': 'counterargument' if claim.get('role') == 'counterargument' else 'support',
                            'citations': links})

    calculation_links = []
    for index, calculation in enumerate(calculations[:MAX_ENTRIES]):
        if not isinstance(calculation, dict):
            continue
        inputs = calculation.get('inputs')
        inputs = inputs if isinstance(inputs, list) else []
        truncated = truncated or len(inputs) > MAX_INPUTS
        refs = []
        for item in inputs[:MAX_INPUTS]:
            fact_id = item.get('fact_id') if isinstance(item, dict) else None
            refs.append({'fact_id': fact_id if isinstance(fact_id, str) else None,
                         'fact_reference': 'unique' if isinstance(fact_id, str) and counts[fact_id] == 1 else 'missing_or_ambiguous'})
        calculation_links.append({'calculation_index': index, 'inputs': refs})
    return {'schema_version': '1', 'index_scope': 'research_version',
            'meaning': 'recorded_links_not_semantic_verification', 'truncated': truncated,
            'claims': claim_links, 'calculations': calculation_links}


def derive_question_links(questions: object, claims: list[dict]) -> dict[str, Any]:
    """Connect saved subquestions to their recorded findings, sources and counterpoints.

    Relations retain their original attribution: model-suggested subquestion
    links and counterpoint links remain suggestions. Neither presence nor absence
    of a link is a semantic coverage or evidence-quality verdict.
    """
    rows = questions if isinstance(questions, list) else []
    by_id = {item['claim_id']: item for item in claims[:MAX_ENTRIES]
             if isinstance(item, dict) and isinstance(item.get('claim_id'), str)}
    counterpoints = [item for item in claims[:MAX_ENTRIES]
                     if isinstance(item, dict) and item.get('role') == 'counterargument'
                     and isinstance(item.get('claim_id'), str)]
    truncated = len(rows) > 20 or len(claims) > MAX_ENTRIES
    result: list[dict[str, Any]] = []
    for index, question in enumerate(rows[:20]):
        if not isinstance(question, dict):
            continue
        raw_ids = question.get('claim_ids')
        ids = list(dict.fromkeys(item for item in raw_ids
                                 if isinstance(item, str) and item in by_id)) if isinstance(raw_ids, list) else []
        sources: list[str] = []
        for claim_id in ids:
            citations = by_id[claim_id].get('citations')
            if not isinstance(citations, list):
                continue
            for citation in citations[:MAX_CITATIONS]:
                source_id = citation.get('source_id') if isinstance(citation, dict) else None
                if isinstance(source_id, str) and source_id and source_id not in sources:
                    sources.append(source_id)
            truncated = truncated or len(citations) > MAX_CITATIONS
        related: list[dict[str, Any]] = []
        targets = set(ids)
        for counter in counterpoints:
            for field, attribution in (('responds_to_claim_ids', 'recorded'),
                                       ('suggested_responds_to_claim_ids', 'model_suggested')):
                links = counter.get(field)
                matched = list(dict.fromkeys(link for link in links
                                             if isinstance(link, str) and link in targets)) if isinstance(links, list) else []
                if matched:
                    counter_citations = counter.get('citations')
                    counter_sources = list(dict.fromkeys(citation['source_id'] for citation in counter_citations[:MAX_CITATIONS]
                        if isinstance(citation, dict) and isinstance(citation.get('source_id'), str) and citation['source_id'])) if isinstance(counter_citations, list) else []
                    truncated = truncated or (isinstance(counter_citations, list) and len(counter_citations) > MAX_CITATIONS)
                    related.append({'claim_id': counter['claim_id'], 'attribution': attribution,
                                    'finding_ids': matched[:MAX_QUESTION_LINKS], 'source_ids': counter_sources})
        truncated = truncated or len(ids) > MAX_QUESTION_LINKS or len(sources) > MAX_QUESTION_LINKS or len(related) > MAX_QUESTION_LINKS
        result.append({'question_index': index, 'finding_ids': ids[:MAX_QUESTION_LINKS],
                       'source_ids': sources[:MAX_QUESTION_LINKS],
                       'counterpoints': related[:MAX_QUESTION_LINKS]})
    return {'schema_version': '1', 'index_scope': 'research_version',
            'meaning': 'recorded_relationships_not_verified_coverage', 'truncated': truncated,
            'questions': result}


def normalize_user_fact_links(claims: list[dict], facts: list[dict], previous_claims: list[dict],
                              previous_facts: list[dict], *, model_generated: bool) -> None:
    """Keep only user-declared, version-local relations to unchanged facts."""
    def unique_facts(items: list[dict]) -> dict[str, dict]:
        counts = Counter(item.get('fact_id') for item in items
                         if isinstance(item, dict) and isinstance(item.get('fact_id'), str))
        return {item['fact_id']: item for item in items if isinstance(item, dict) and
                isinstance(item.get('fact_id'), str) and counts[item['fact_id']] == 1}

    current, previous = unique_facts(facts), unique_facts(previous_facts)
    prior_claims = {item['claim_id']: item for item in previous_claims if isinstance(item, dict) and
                    isinstance(item.get('claim_id'), str)}
    fact_fields = ('fact_id', 'label', 'value', 'unit', 'currency', 'period', 'source_id', 'quotation', 'location')
    claim_fields = ('text', 'role', 'citations')
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        prior = prior_claims.get(claim.get('claim_id'))
        same_claim = prior is not None and all(prior.get(key) == claim.get(key) for key in claim_fields)
        proposed = prior.get('user_fact_links', []) if model_generated and same_claim else (
            [] if model_generated else claim.get('user_fact_links', []))
        claim.pop('user_fact_links', None)
        claim.pop('user_fact_links_omitted', None)
        if not isinstance(proposed, list):
            claim['user_fact_links_omitted'] = 1
            continue
        kept: list[dict] = []
        seen: set[tuple[str, str]] = set()
        omitted = max(0, len(proposed) - 20)
        for link in proposed[:20]:
            fact_id = link.get('fact_id') if isinstance(link, dict) else None
            relation = link.get('relation') if isinstance(link, dict) else None
            valid = (isinstance(fact_id, str) and isinstance(relation, str) and relation in {'support', 'oppose'} and
                     fact_id in current and fact_id in previous and (prior is None or same_claim) and
                     all(current[fact_id].get(key) == previous[fact_id].get(key) for key in fact_fields))
            if not valid or (fact_id, relation) in seen:
                omitted += 1
                continue
            seen.add((fact_id, relation))
            kept.append({'fact_id': fact_id, 'relation': relation, 'attribution': 'user',
                         'status': 'not_independently_verified'})
        if kept:
            claim['user_fact_links'] = kept
        if omitted:
            claim['user_fact_links_omitted'] = omitted


def normalize_research_questions(raw: object, claims: list[dict], previous_raw: object,
                                 previous_claims: list[dict], *, model_generated: bool) -> list[dict]:
    """Convert bounded model index suggestions to version-local claim IDs.

    User versions inherit server-owned suggestions only while their targets
    retain the same text and citations. Links are suggestions, never coverage
    verdicts; unresolved indexes remain visibly unlinked.
    """
    supports = [claim for claim in claims if isinstance(claim, dict) and claim.get('role') != 'counterargument']
    current = {claim['claim_id']: claim for claim in supports if isinstance(claim.get('claim_id'), str)}
    previous = {claim['claim_id']: claim for claim in previous_claims
                if isinstance(claim, dict) and isinstance(claim.get('claim_id'), str)}
    rows = raw if model_generated else previous_raw
    if not isinstance(rows, list):
        return []
    output = []
    for row in rows[:20]:
        if not isinstance(row, dict) or not isinstance(row.get('question'), str) or not row['question'].strip():
            continue
        question = row['question'].strip()[:500]
        refs: list[str] = []
        if model_generated:
            indexes = row.get('claim_indexes')
            if isinstance(indexes, list):
                for index in indexes[:20]:
                    if type(index) is int and 0 <= index < len(supports):
                        claim_id = supports[index].get('claim_id')
                        if isinstance(claim_id, str) and claim_id not in refs:
                            refs.append(claim_id)
        else:
            ids = row.get('claim_ids')
            if isinstance(ids, list):
                for claim_id in ids[:20]:
                    if not isinstance(claim_id, str) or claim_id in refs or claim_id not in current or claim_id not in previous:
                        continue
                    old, new = previous[claim_id], current[claim_id]
                    if all(old.get(key) == new.get(key) for key in ('text', 'role', 'citations')):
                        refs.append(claim_id)
        output.append({'question': question, 'claim_ids': refs, 'origin': 'model_suggestion',
                       'status': 'linked_not_verified' if refs else 'no_linked_finding'})
    return output
