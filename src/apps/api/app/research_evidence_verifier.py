"""Requirement-specific source comparison and conservative review feedback.

These functions do not perform network/model calls or grant permissions. Metadata
and model observations are inputs, not proof of semantic equivalence or truth.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any

_DIMENSIONS = ('entity', 'metric', 'period', 'unit', 'currency', 'methodology', 'document_type', 'market', 'language')
_PERMISSIONS = ('store', 'display', 'model_input', 'export')


def _values(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def compare_requirement_candidates(requirement: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compare explicit verified metadata for a single requirement.

    A matching label is only an exact metadata match, never semantic support.
    Candidate metadata must have status ``verified`` to assert compatibility.
    Permissions are evaluated separately for the requested operations.
    """
    dimensions = requirement.get('dimensions') if isinstance(requirement.get('dimensions'), dict) else {}
    uses = [item for item in requirement.get('required_permissions', [])
            if item in _PERMISSIONS] if isinstance(requirement.get('required_permissions'), list) else []
    as_of = _date(requirement.get('as_of'))
    output = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or not isinstance(candidate.get('candidate_id'), str):
            continue
        metadata = candidate.get('metadata') if isinstance(candidate.get('metadata'), dict) else {}
        checks: dict[str, str] = {}
        reasons: list[str] = []
        for field in _DIMENSIONS:
            requested = dimensions.get(field)
            needed = _values(requested.get('value')) if isinstance(requested, dict) else _values(requested)
            requested_namespace = requested.get('namespace') if isinstance(requested, dict) else None
            fact = metadata.get(field)
            if not needed:
                checks[field] = 'not_requested'
            elif not isinstance(fact, dict) or fact.get('status') != 'verified' or not _values(fact.get('value')):
                checks[field] = 'unknown'
            else:
                available = {value.casefold() for value in _values(fact['value'])}
                # Exact identity is a machine comparison only. A mismatch is
                # decisive only for fields whose vocabulary is declared exact.
                if all(value.casefold() in available for value in needed):
                    checks[field] = 'match'
                elif (fact.get('complete') is True and fact.get('comparison') == 'canonical_exact'
                      and isinstance(requested_namespace, str) and bool(requested_namespace)
                      and requested_namespace == fact.get('namespace')
                      and field in {'entity', 'period', 'unit', 'currency', 'document_type', 'market', 'language'}):
                    checks[field] = 'mismatch'
                else:
                    checks[field] = 'unknown'
        permission = candidate.get('permissions') if isinstance(candidate.get('permissions'), dict) else {}
        checks['permissions'] = ('not_requested' if not uses else 'mismatch' if any(permission.get(use) is False for use in uses)
                                 else 'match' if all(permission.get(use) is True for use in uses) else 'unknown')
        state = candidate.get('availability')
        checks['availability'] = 'match' if state == 'available' else 'mismatch' if state in {'unavailable', 'unconfigured', 'restricted'} else 'unknown'
        for field in ('publication', 'revision'):
            if as_of is None:
                checks[field] = 'not_requested' if requirement.get('as_of') is None else 'unknown'
                continue
            stamp = _date(metadata.get(field + '_at'))
            # A current published version alone does not prove absence of later
            # revisions; both publication and revision records are needed.
            if stamp is None or metadata.get(field + '_status') != 'verified':
                checks[field] = 'unknown'
            else:
                checks[field] = 'match' if stamp <= as_of else 'mismatch'
        checks['independence'] = 'unknown'  # Multiple URLs/hashes are not independent sources.
        for name, status in checks.items():
            if status in {'mismatch', 'unknown'}:
                reasons.append(f'{name}_{status}')
        # A lead is never a source already read or an independently corroborating item.
        role = candidate.get('evidence_role')
        role = role if role in {'primary_document', 'structured_record', 'lead', 'unknown'} else 'unknown'
        disposition = ('rejected' if 'mismatch' in checks.values() else
                       'unknown' if any(value == 'unknown' for key, value in checks.items() if key != 'independence') else 'eligible')
        output.append({'candidate_id': candidate['candidate_id'], 'disposition': disposition,
                       'dimensions': checks, 'reasons': reasons, 'evidence_role': role,
                       'meaning': 'metadata_comparison_not_semantic_support'})
    return output


def claim_fingerprint(claim: dict[str, Any], source_hashes: dict[str, str]) -> str:
    """Bind review to claim text, source version and exact citation locations."""
    citations = []
    for citation in claim.get('citations', []) if isinstance(claim.get('citations'), list) else []:
        if isinstance(citation, dict):
            sid = citation.get('source_id')
            citations.append({'source_id': sid, 'source_hash': source_hashes.get(sid),
                              'location': citation.get('location'), 'quotation': citation.get('quotation')})
    payload = {'text': claim.get('text'), 'role': claim.get('role'), 'citations': citations,
               'responds_to_claim_ids': claim.get('responds_to_claim_ids'),
               'suggested_responds_to_claim_ids': claim.get('suggested_responds_to_claim_ids'),
               'suggested_responds_to_claim_indexes': claim.get('suggested_responds_to_claim_indexes')}
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str).encode()).hexdigest()


def review_fingerprint(claim: dict[str, Any], source_hashes: dict[str, str], claims: list[dict[str, Any]]) -> str:
    """Bind a counterpoint assessment to the current related claim contents too."""
    related: list[str] = []
    indexes = claim.get('suggested_responds_to_claim_indexes')
    if isinstance(indexes, list):
        for index in indexes:
            if type(index) is int and 0 <= index < len(claims) and isinstance(claims[index], dict):
                related.append(claim_fingerprint(claims[index], source_hashes))
    ids = [*(claim.get('responds_to_claim_ids') or []), *(claim.get('suggested_responds_to_claim_ids') or [])]
    for identifier in ids:
        matches = [item for item in claims if isinstance(item, dict) and item.get('claim_id') == identifier]
        if len(matches) == 1:
            related.append(claim_fingerprint(matches[0], source_hashes))
    base = claim_fingerprint(claim, source_hashes)
    if not related:
        return base
    return sha256(json.dumps([base, related], separators=(',', ':')).encode()).hexdigest()


def derive_verification_feedback(requirements: list[dict[str, Any]], claims: list[dict[str, Any]],
                                 links: list[dict[str, Any]], *, source_hashes: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Return unresolved issues to the acquisition loop, never a truth verdict.

    Review records without an exact source-bound fingerprint are treated as
    stale/unbound. Existing legacy reviews remain visible but cannot resolve a
    newly changed claim. Conflicts and counterpoints remain explicit leads.
    """
    hashes = source_hashes or {}
    by_id = {item['claim_id']: item for item in claims if isinstance(item, dict) and isinstance(item.get('claim_id'), str)}
    evidence = {item['claim_id']: item for item in links if isinstance(item, dict) and isinstance(item.get('claim_id'), str)}
    result = []
    for requirement in requirements:
        if not isinstance(requirement, dict) or not isinstance(requirement.get('requirement_id'), str):
            continue
        ids = [value for value in requirement.get('finding_ids', []) if isinstance(value, str)] if isinstance(requirement.get('finding_ids'), list) else []
        issues: list[str] = []
        reviews = []
        mechanical = 'not_recorded'
        if not ids:
            issues.append('no_linked_claim')
        for claim_id in ids:
            claim, link = by_id.get(claim_id), evidence.get(claim_id)
            if claim is None or link is None:
                issues.append('claim_or_link_missing')
                continue
            located = link.get('citation_location_status') == 'all_recorded'
            mechanical = 'location_recorded' if mechanical in {'not_recorded', 'location_recorded'} and located else 'incomplete'
            if not located:
                issues.append('citation_location_missing_or_ambiguous')
            citation_sources = [c.get('source_id') for c in claim.get('citations', []) if isinstance(c, dict)] if isinstance(claim.get('citations'), list) else []
            fp = review_fingerprint(claim, hashes, claims)
            bound = bool(citation_sources) and all(isinstance(sid, str) and isinstance(hashes.get(sid), str) and hashes[sid] for sid in citation_sources)
            review = claim.get('support_review') if isinstance(claim.get('support_review'), dict) else {}
            verdict = review.get('verdict') if bound and review.get('claim_fingerprint') == fp else None
            if verdict not in {'supports', 'does_not_support', 'contradicts', 'uncertain'}:
                verdict = 'not_reviewed_or_stale'
            if verdict != 'supports':
                issues.append('semantic_' + verdict)
            reviews.append({'claim_id': claim_id, 'claim_fingerprint': fp, 'model_opinion': verdict,
                            'independently_verified': False, 'scope': 'cited_excerpt_only',
                            'relationship_status': 'not_independently_verified'})
        if requirement.get('conflict') or requirement.get('conflicts'):
            issues.append('conflict_requires_resolution')
        counters = requirement.get('counterpoints')
        if requirement.get('counterpoint_required') and not counters:
            issues.append('counterpoint_not_recorded')
        if isinstance(counters, list):
            for relation in counters:
                if not isinstance(relation, dict):
                    issues.append('counterpoint_requires_assessment')
                    continue
                opposing = by_id.get(relation.get('claim_id'))
                targets = relation.get('finding_ids')
                if (not opposing or not isinstance(targets, list) or not set(targets).intersection(ids)):
                    issues.append('counterpoint_requires_assessment')
                    continue
                ref_sources = [c.get('source_id') for c in opposing.get('citations', []) if isinstance(c, dict)] if isinstance(opposing.get('citations'), list) else []
                bound = bool(ref_sources) and all(isinstance(sid, str) and isinstance(hashes.get(sid), str) and hashes[sid] for sid in ref_sources)
                reviewed = opposing.get('support_review') if isinstance(opposing.get('support_review'), dict) else {}
                if (not bound or reviewed.get('claim_fingerprint') != review_fingerprint(opposing, hashes, claims)
                        or reviewed.get('verdict') != 'supports' or reviewed.get('relevance') != 'relevant'
                        or reviewed.get('relevance_context_complete') is not True):
                    issues.append('counterpoint_requires_assessment')
        issues = list(dict.fromkeys(issues))
        actions = []
        if any(x in issues for x in ('no_linked_claim', 'claim_or_link_missing', 'citation_location_missing_or_ambiguous')):
            actions.append('find_or_read_relevant_source')
        if any(x.startswith('semantic_') for x in issues):
            actions.append('review_original_or_seek_alternative_evidence')
        if 'conflict_requires_resolution' in issues:
            actions.append('investigate_conflicting_sources')
        if any(x.startswith('counterpoint_') for x in issues):
            actions.append('investigate_counterpoint')
        result.append({'requirement_id': requirement['requirement_id'], 'mechanical_status': mechanical,
                       'semantic_status': 'model_opinion_not_independent' if reviews else 'unknown',
                       'issues': issues, 'next_actions': list(dict.fromkeys(actions)), 'claim_reviews': reviews})
    return result
