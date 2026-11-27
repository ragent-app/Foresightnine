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


def structured_partial_notes(state: Mapping[str, Any], sources: Mapping[str, Any], as_of=None, *, fact_ids=None, calculation_ids=None) -> str:
    return _structured_partial_observations(state, sources, as_of, fact_ids=fact_ids, calculation_ids=calculation_ids)[0]


def verified_partial_calculation_ids(state: Mapping[str, Any], sources: Mapping[str, Any], as_of=None, *, fact_ids=None, calculation_ids=None) -> set[str]:
    """Return identities whose receipts and located, authorized inputs passed the same checks as rendering."""
    return _structured_partial_observations(state, sources, as_of, fact_ids=fact_ids, calculation_ids=calculation_ids)[1]


def _structured_partial_observations(state: Mapping[str, Any], sources: Mapping[str, Any], as_of=None, *, fact_ids=None, calculation_ids=None) -> tuple[str, set[str]]:
    """Render only source-bound observations and reproducible calculations.

    Quotation is presented as source text, not a semantic answer. Model-supplied
    labels, periods and interpretations are deliberately excluded.
    """
    from .research_calculations import calculation_input_status, calculation_result_status

    facts = [f for key in ('facts', 'prior_facts') for f in state.get(key, []) if isinstance(f, Mapping)]
    # Previous-turn records are only relevant when a current calculation or
    # explicit requirement references them.
    if fact_ids is None:
        fact_ids = {f.get('fact_id') for f in state.get('facts', []) if isinstance(f, Mapping)}
        fact_ids.update(i.get('fact_id') for c in state.get('calculations', []) if isinstance(c, Mapping)
                        for i in c.get('inputs', []) if isinstance(i, Mapping))
    facts = [f for f in facts if f.get('fact_id') in fact_ids]
    hashes = _source_hashes(state)
    reads = [r for key in ('read_ranges', 'prior_read_ranges', 'focus_ranges')
             for r in state.get(key, []) if isinstance(r, Mapping)]
    from decimal import Decimal, InvalidOperation
    from .research_analysis import _quoted_numbers
    selected = []
    verified_calculations = set()
    lines = []
    seen = set()
    for fact in facts:
        sid = fact.get('source_id')
        source = sources.get(sid)
        start, end, quote = fact.get('character_start'), fact.get('character_end'), fact.get('quotation')
        if (not fact.get('fact_id') or source is None or source_eligibility([source], as_of)[0][1] != 'eligible'
                or sha256(source.text.encode()).hexdigest() != source.content_sha256
                or hashes.get(sid, source.content_sha256) != source.content_sha256
                or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(source.text) or not isinstance(quote, str)
                or source.text[start:end] != quote):
            continue
        ranges = [r for r in reads if r.get('source_id') == sid and type(r.get('start')) is int
                  and type(r.get('end')) is int and r['start'] <= start < end <= r['end']]
        if not ranges:
            continue
        try:
            if Decimal(str(fact.get('value'))) not in _quoted_numbers(quote):
                continue
        except (InvalidOperation, ValueError):
            continue
        selected.append(fact)
        # Include bounded read context around a cell or fragment, so an
        # adjacent condition is not silently presented as absent.
        read = max(ranges, key=lambda r: min(end + 150, r['end']) - max(start - 150, r['start']))
        left, right = max(read['start'], start - 150), min(read['end'], end + 150)
        context = source.text[left:right].replace('\n', ' ')
        key = (sid, left, right)
        if key not in seen:
            lines.append(f"[{sid} {left}:{right}] “{context}”")
            seen.add(key)
    safe_ids = {f['fact_id'] for f in selected}
    # Shared inputs do not imply that a historical calculation answers this
    # question. Prior calculations must be selected by their own identity.
    calculations = (state.get('calculations', []) if calculation_ids is None else
                    [c for key in ('prior_calculations', 'calculations') for c in state.get(key, [])
                     if isinstance(c, Mapping) and c.get('calculation_id') in calculation_ids])
    for calc in calculations:
        if not isinstance(calc, Mapping):
            continue
        ids = [i.get('fact_id') for i in calc.get('inputs', []) if isinstance(i, Mapping)]
        if (not ids or not set(ids) <= safe_ids or calculation_input_status(calc, selected) != 'matched'
                or calculation_result_status(calc) != 'matched'):
            continue
        if calc.get('calculation_id'):
            verified_calculations.add(calc['calculation_id'])
        line = f"{calc.get('formula_id')}: {calc.get('value')} {calc.get('unit') or ''} ({', '.join(ids)})".strip()
        if line not in seen:
            lines.append(line)
            seen.add(line)
    return (('来源摘录及计算（语义和适用范围尚未独立核验）：\n' + '\n'.join(lines)) if lines else '', verified_calculations)


def deduplicate_reviewed_claims(claims: list[dict], reviews: list[dict] = ()) -> list[dict]:
    """Remove only verbatim sentences already contained in a reviewed composite.

    The composite must include all the granular claim's cited locations; unique
    evidence and qualifying text therefore survive.
    """
    def locations(claim):
        return {(c.get('source_id'), (c.get('location') or {}).get('character_start'),
                 (c.get('location') or {}).get('character_end')) for c in claim.get('citations', [])}
    def sentences(text):
        return [s.strip() for s in re.split(r'(?<=[.!?])\s+|(?<=[。！？])\s*', text.strip()) if s.strip()]
    result = []
    for index, claim in enumerate(claims):
        text = claim.get('text', '').strip()
        own_locations = locations(claim)
        redundant = any(other is not claim and (len(other.get('text', '').strip()) > len(text)
                        and text in sentences(other.get('text', ''))
                        or other.get('text', '').strip() == text and other_index < index)
                        and own_locations and own_locations <= locations(other)
                        and claim.get('role') == other.get('role') for other_index, other in enumerate(claims))
        # A supported answer to the same requirement may subsume a partial
        # finding only when its cited source spans cover every granular span.
        if not redundant and own_locations:
            partial_requirements = {r.get('requirement_id') for r in reviews
                                    if r.get('claim_id') == claim.get('claim_id') and r.get('verdict') == 'partial'}
            def covers(big):
                big_locations = locations(big)
                return all(any(sid == bsid and type(start) is int and type(end) is int
                               and type(bstart) is int and type(bend) is int and bstart <= start < end <= bend
                               for bsid, bstart, bend in big_locations) for sid, start, end in own_locations)
            other_verdict = any(r.get('claim_id') == claim.get('claim_id') and r.get('verdict') != 'partial' for r in reviews)
            redundant = bool(partial_requirements) and not other_verdict and any(
                other is not claim and other.get('role') == claim.get('role') and covers(other)
                and partial_requirements <= {r.get('requirement_id') for r in reviews
                                            if r.get('claim_id') == other.get('claim_id')
                                            and r.get('verdict') == 'supported'}
                for other in claims)
        if not redundant:
            result.append(claim)
    return result
