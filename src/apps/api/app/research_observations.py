"""Conservative preservation of exact, located observations in a delivery."""
from hashlib import sha256
import re
from typing import Any, Mapping

from .research_evidence_requirements import _source_hashes
from .research_source_policy import source_eligibility


def deterministic_observation_ids(claims, state: Mapping[str, Any], sources: Mapping[str, Any], as_of=None) -> set[str]:
    """Keep full-sentence quotations, not interpretations or excerpt fragments.

    A supporting observation also needs an existing numerical fact at the same
    location. A counterargument remains a located quotation whose relationship
    to the challenged claim still requires review.
    """
    hashes = _source_hashes(state)
    reads = [r for key in ('read_ranges', 'prior_read_ranges', 'focus_ranges')
             for r in state.get(key, []) if isinstance(r, Mapping)]
    facts = [f for key in ('facts', 'prior_facts') for f in state.get(key, []) if isinstance(f, Mapping)]
    result = set()
    for claim in claims:
        if not isinstance(claim, Mapping) or not claim.get('claim_id'):
            continue
        text = claim.get('text')
        if not isinstance(text, str) or not text or text[-1] not in '.!?。！？':
            continue
        for citation in claim.get('citations', []):
            if not isinstance(citation, Mapping) or citation.get('quotation') != text:
                continue
            sid = citation.get('source_id')
            source = sources.get(sid)
            location = citation.get('location') or {}
            start, end = location.get('character_start'), location.get('character_end')
            if (source is None or source_eligibility([source], as_of)[0][1] != 'eligible'
                    or sha256(source.text.encode()).hexdigest() != source.content_sha256
                    or hashes.get(sid, source.content_sha256) != source.content_sha256
                    or type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= len(source.text) or source.text[start:end] != text):
                continue
            prefix = source.text[:start].rstrip(' \t\r')
            if prefix and prefix[-1] not in '.!?。！？\n':
                continue
            if end < len(source.text) and not source.text[end].isspace():
                continue
            # An immediately following exception can change even an otherwise
            # exact sentence. Keep it out until the surrounding terms are reviewed.
            following = source.text[end:].lstrip()
            if re.match(r'(?:however\b|except\b|unless\b|only\b|subject to\b|provided that\b|但|但是|除非|仅限|前提)', following, re.I):
                continue
            if not any(r.get('source_id') == sid and type(r.get('start')) is int and type(r.get('end')) is int
                       and r['start'] <= start < end <= r['end'] for r in reads):
                continue
            if claim.get('role') == 'counterargument' or any(
                    f.get('source_id') == sid and f.get('quotation') == text
                    and f.get('character_start') == start and f.get('character_end') == end
                    and f.get('verification') == 'numeric_and_location_match_semantics_not_verified'
                    for f in facts):
                result.add(claim['claim_id'])
                break
    return result
