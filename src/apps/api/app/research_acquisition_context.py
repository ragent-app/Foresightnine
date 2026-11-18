"""Adapt in-flight research artifacts to the acquisition decision contract.

These are run-local candidate links, not coverage proofs or persisted IDs.
Only material already eligible for model input is included in claim summaries.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit

from .research_evidence_verifier import claim_fingerprint, derive_verification_feedback
from .research_provenance import derive_evidence_links, derive_question_links
from .research_requirements import combined_requirements
from .research_source_policy import prepare_sources
from .research_sources import ResearchSource
from .research_structured_data import extract_structured_observations

_DIMENSIONS = {'entities': 'entity', 'metrics': 'metric', 'periods': 'period', 'units': 'unit',
               'currencies': 'currency', 'methodologies': 'methodology', 'document_types': 'document_type',
               'markets': 'market', 'languages': 'language'}


def build_acquisition_context(*, question: str, as_of: datetime | None, planned: list[dict],
                              analysis: Any, claims: list[dict], sources: list[ResearchSource],
                              discovered: list[dict], require_counterarguments: bool) -> dict[str, Any]:
    """Return permission-safe candidate comparisons and reviewable feedback inputs."""
    # The analyzer still batches 20 sources. Decision context pages all
    # individually authorized sources rather than silently hiding later ones.
    eligible = [source for source in sources if prepare_sources([source], as_of)[0]]
    allowed_ids = {source.source_id for source in eligible}
    hashes = {source.source_id: source.content_sha256 for source in eligible}
    # Keep indexes intact; assign deterministic *run-local* IDs, never persist
    # these as domain IDs or claim any question has been semantically answered.
    mapped_claims = []
    for index, raw in enumerate(claims):
        if not isinstance(raw, dict):
            mapped_claims.append({})
            continue
        claim = deepcopy(raw)
        identifier = claim.get('claim_id')
        if not isinstance(identifier, str) or not identifier:
            identifier = 'runclaim:' + sha256(f'{index}:{claim_fingerprint(claim, hashes)}'.encode()).hexdigest()[:24]
        claim['claim_id'] = identifier
        mapped_claims.append(claim)
    support_count = len(getattr(analysis, 'claims', []) or []) if analysis is not None else 0
    link_claims = deepcopy(mapped_claims)
    for row in link_claims:
        indexes = row.get('suggested_responds_to_claim_indexes')
        if isinstance(indexes, list):
            row['suggested_responds_to_claim_ids'] = [mapped_claims[i]['claim_id'] for i in indexes
                if type(i) is int and 0 <= i < min(support_count, len(mapped_claims)) and 'claim_id' in mapped_claims[i]]
    questions = deepcopy(getattr(analysis, 'research_questions', []) or []) if analysis is not None else []
    for row in questions:
        if not isinstance(row, dict):
            continue
        indexes = row.get('claim_indexes')
        if isinstance(indexes, list):
            row['claim_ids'] = [mapped_claims[i]['claim_id'] for i in indexes
                if type(i) is int and 0 <= i < min(support_count, len(mapped_claims)) and 'claim_id' in mapped_claims[i]]
    question_links = derive_question_links(questions, link_claims)['questions']
    timestamp = as_of.isoformat() if as_of else None
    requirements = [{'requirement_id': 'user_request', 'question': question, 'as_of': timestamp,
                     'required_permissions': ['model_input', 'display'],
                     'counterpoint_required': require_counterarguments, 'finding_ids': [], 'counterpoints': [],
                     'relation_status': 'not_established'}]
    planned_items = combined_requirements(planned, analysis) if analysis is not None else tuple(planned)
    seen: set[str] = set()
    for item in planned_items:
        if not isinstance(item, dict) or not isinstance(item.get('question'), str):
            continue
        text = item['question'].strip()
        if not text or text in seen:
            continue
        seen.add(text)
        matches = [i for i, row in enumerate(questions) if isinstance(row, dict) and row.get('question') == text]
        link = next((row for row in question_links if len(matches) == 1 and row.get('question_index') == matches[0]), {})
        raw_dimensions = item.get('dimensions') if isinstance(item.get('dimensions'), dict) else {}
        dimensions = {_DIMENSIONS[key]: value for key, value in raw_dimensions.items() if key in _DIMENSIONS}
        entry = {**item, 'dimensions': dimensions, 'requirement_id': 'question:' + sha256(text.encode()).hexdigest()[:24],
                 'finding_ids': link.get('finding_ids', []), 'counterpoints': link.get('counterpoints', []),
                 'relation_status': 'model_suggested_not_verified', 'counterpoint_required': require_counterarguments,
                 'required_permissions': ['model_input', 'display'], 'as_of': timestamp}
        requirements.append(entry)
        if text == question.strip() and len(matches) == 1:
            requirements[0].update(finding_ids=entry['finding_ids'], counterpoints=entry['counterpoints'],
                                   relation_status='model_suggested_not_verified')
    links = derive_evidence_links(mapped_claims, getattr(analysis, 'facts', []) if analysis else [],
                                  getattr(analysis, 'calculations', []) if analysis else [])['claims']
    if len(requirements) > 1:
        requirements[0]['child_requirement_ids'] = [item['requirement_id'] for item in requirements[1:]]
        requirements[0]['decomposition_status'] = 'model_suggested_not_independently_verified'
    feedback = derive_verification_feedback(requirements, mapped_claims, links, source_hashes=hashes)
    if len(requirements) > 1:
        parent = feedback[0]
        children = feedback[1:]
        # A parent can summarize explicit child links, but decomposition is
        # never claimed exhaustive or independently verified.
        if all(item.get('finding_ids') for item in requirements[1:]):
            parent['issues'] = [issue for issue in parent['issues']
                                if issue not in {'no_linked_claim', 'counterpoint_not_recorded'}]
            parent['next_actions'] = [action for action in parent['next_actions']
                                      if action not in {'find_or_read_relevant_source', 'investigate_counterpoint'}]
            if any(child.get('issues') for child in children):
                parent['issues'].append('unresolved_child_requirements')
                parent['next_actions'].append('investigate_child_requirements')
            parent['semantic_status'] = 'child_model_opinions_not_independent'
        parent['decomposition_status'] = 'model_suggested_not_independently_verified'
        parent['child_requirement_ids'] = requirements[0]['child_requirement_ids']
    # The decision agent needs what was claimed, but source text and quotations
    # must not cross a source's model-input/display or cutoff boundary.
    for row in feedback:
        summaries = []
        for review in row.get('claim_reviews', []):
            claim = next((item for item in mapped_claims if item.get('claim_id') == review.get('claim_id')), None)
            if not claim:
                continue
            citations = claim.get('citations') if isinstance(claim.get('citations'), list) else []
            cited_ids = [item.get('source_id') for item in citations if isinstance(item, dict)]
            if any(not isinstance(sid, str) or sid not in allowed_ids for sid in cited_ids):
                summaries.append({'claim_id': review['claim_id'], 'status': 'source_access_restricted'})
                continue
            summaries.append({'claim_id': review['claim_id'], 'text': str(claim.get('text', ''))[:1000],
                              'role': claim.get('role', 'support'), 'source_ids': list(dict.fromkeys(cited_ids)),
                              'model_opinion': review.get('model_opinion'), 'semantic_status': 'not_independently_verified'})
        row['claim_summaries'] = summaries
    candidates = []
    for index, item in enumerate(discovered):
        if not isinstance(item, dict) or item.get('status') == 'retrieved_unverified':
            continue
        try:
            host = urlsplit(item.get('url', '')).hostname
        except (TypeError, ValueError):
            host = None
        lead_metadata = {'host': host, 'published_at': item.get('published_at'),
                         'retrieved_at': item.get('retrieved_at'), 'provider': item.get('search_provider'),
                         'verification': 'provider_reported_not_verified'}
        candidates.append({'candidate_id': f'lead:{index}', 'title': str(item.get('title', ''))[:200],
                           'availability': 'unknown', 'metadata': {}, 'lead_metadata': lead_metadata,
                           'evidence_role': 'lead'})
    for source in eligible:
        metadata: dict[str, Any] = {}
        if source.retrieval_method == 'sec_companyconcept':
            observations, _truncated = extract_structured_observations([source])
            values: dict[str, list[str]] = {'entity': [], 'metric': [], 'period': [], 'unit': [], 'currency': []}
            for observation in observations:
                structured = observation.get('structured_metadata', {})
                for field, value in (('entity', structured.get('entity_id')), ('metric', structured.get('metric_id')),
                                     ('period', observation.get('period')), ('unit', observation.get('unit')),
                                     ('currency', observation.get('currency'))):
                    if isinstance(value, str) and value and value not in values[field]:
                        values[field].append(value)
            for field, found in values.items():
                if found:
                    metadata[field] = {'value': found, 'status': 'verified', 'complete': False,
                                       'comparison': 'canonical_exact',
                                       'namespace': {'entity':'sec-cik', 'metric':'xbrl-concept',
                                                     'period':'iso-period', 'unit':'xbrl-unit',
                                                     'currency':'iso4217'}[field]}
        candidates.append({'candidate_id': 'source:' + source.source_id, 'source_id': source.source_id,
                           'title': source.title[:200], 'availability': 'available',
                           'permissions': source.permissions.model_dump(), 'metadata': metadata,
                           'evidence_role': 'structured_record' if source.retrieval_method == 'sec_companyconcept' else
                                            'primary_document' if source.kind == 'official' else 'unknown'})
    return {'requirements': requirements, 'feedback': feedback, 'candidates': candidates,
            'source_hashes': hashes, 'meaning': 'run_local_candidate_relations_not_semantic_coverage'}
