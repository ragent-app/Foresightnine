"""Derive bounded, version-local links from an existing research snapshot.

Links expose recorded provenance only. A shared passage does not establish
that a finding is true, that a fact belongs to its metric, or that either
supports the other semantically. No source content is loaded or exported.
"""
from __future__ import annotations

from collections import Counter
from typing import Any
from uuid import uuid4


MAX_ENTRIES = 2000
MAX_CITATIONS = 10
MAX_INPUTS = 20
MAX_FACT_MATCHES = 20


def _location_key(source_id: object, location: object) -> tuple[str, int, int] | None:
    if not isinstance(source_id, str) or not source_id or not isinstance(location, dict):
        return None
    start, end = location.get('character_start'), location.get('character_end')
    if location.get('status') or type(start) is not int or type(end) is not int or not 0 <= start < end:
        return None
    return source_id, start, end


def normalize_claim_links(claims: list[dict], previous_claims: list[dict]) -> None:
    """Assign server-controlled identities and retain only local claim links.

    Links express a recorded relationship, not semantic agreement or support.
    Legacy claims receive IDs in the next version; prior versions are untouched.
    """
    previous_ids = {entry.get('claim_id') for entry in previous_claims
                    if isinstance(entry, dict) and isinstance(entry.get('claim_id'), str)}
    used: set[str] = set()
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        candidate = claim.get('claim_id')
        if not isinstance(candidate, str) or candidate not in previous_ids or candidate in used:
            candidate = 'claim_' + uuid4().hex
        claim['claim_id'] = candidate
        used.add(candidate)
    support_ids = {claim['claim_id'] for claim in claims if isinstance(claim, dict)
                   and claim.get('role') != 'counterargument'}
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        raw = claim.pop('responds_to_claim_ids', [])
        claim.pop('responds_to_omitted', None)
        if claim.get('role') != 'counterargument':
            continue
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
