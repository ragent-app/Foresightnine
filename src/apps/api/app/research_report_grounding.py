"""Conservative, deterministic checks for checkpointed report clauses.

Location and lexical checks are not semantic verification; findings remain reviewable.
"""
import re
from decimal import Decimal, InvalidOperation
from typing import Any

_NUMBER = re.compile(r'(?<![A-Za-z0-9])[-+]?\d[\d,]*(?:\.\d+)?(?![A-Za-z0-9])')
_CAUSE = re.compile(r'\b(?:caused|causes|because|due to|led to|driven by)\b|导致|造成|由于|因为|推动', re.I)
_INFERENCE = re.compile(r'\b(?:may|might|could|hypothesis|inference|scenario)\b|可能|假设|推测|情景', re.I)
_CAMEL = re.compile(r'\b[A-Z][a-z]+[A-Z][A-Za-z]*\b|\b[A-Z]{2,6}\b')
_IGNORE = {'USD', 'EUR', 'CNY', 'HKD', 'FY', 'SEC', 'ETF'}
_CONTRIBUTION = re.compile(r'\b(?:contribut(?:e|ed|ion)|account(?:s|ed)? for|attribut(?:able|ed))\b|贡献|归因于|占(?:增量|增长)', re.I)
_CHANGE_SCOPE = re.compile(r'\b(?:increase|growth|change|increment|delta|decrease|decline)\b|增量|增长|变化|下降|减少', re.I)


def change_contribution_disclosed(text: str) -> bool:
    """Necessary lexical screen, not proof that a disclosure supports a claim."""
    for sentence in re.split(r'[。；;!?！？]|\.(?!\d)', text):
        if re.search(r'\b(?:not|no|neither|without|cannot|unknown|undisclosed)\b|未|没有|不(?:是|包括|代表|能|可|披露)', sentence, re.I):
            continue
        # The contribution must be grammatically connected to the change,
        # rather than merely co-occurring with a nearby growth sentence.
        if (re.search(r'\bcontribut(?:e[ds]?|ion)\b[^.;!?]{0,100}\b(?:of|to|towards)\s+(?:the\s+)?(?:increase|growth|change|increment|decrease|decline)\b', sentence, re.I)
                or re.search(r'\b(?:increase|growth|change|increment|decrease|decline)\b\s+(?:included|reflects?|was attributable to)\b[^.;!?]{0,80}\bcontribution\b', sentence, re.I)
                or re.search(r'(?:对|占|贡献于)[^。；;]{0,50}(?:增量|增长|变化|下降|减少)[^。；;]{0,30}(?:贡献|占比|来自|归因)|(?:贡献|归因)[^。；;]{0,50}(?:增量|增长|变化|下降|减少)', sentence)):
            return True
    return False


def contribution_subject_disclosed(text: str, name: str) -> bool:
    """Require the stated subject in, or immediately before, the contribution sentence."""
    sentences = [s.strip() for s in re.split(r'[。；;!?！？]|\.(?!\d)', text) if s.strip()]
    pattern = r'(?<![A-Za-z0-9])' + re.escape(name) + r'(?![A-Za-z0-9])'
    for index, sentence in enumerate(sentences):
        if not change_contribution_disclosed(sentence):
            continue
        if re.search(pattern, sentence, re.I):
            return True
        if (index and re.match(r'^(?:the|it|its)\b', sentence, re.I)
                and not re.match(r'^The\s+[A-Z][a-z]+\b', sentence)
                and re.search(pattern, sentences[index - 1], re.I)):
            return True
    return False


def _numbers(text: str) -> set[Decimal]:
    out = set()
    text = re.sub(r'(?<=\d)-(?=\d)', ' ', text)
    for value in _NUMBER.findall(text):
        try:
            out.add(Decimal(value.replace(',', '')))
        except InvalidOperation:
            continue
    return out


def clause_problem(text: str, quotations: list[str], facts: list[dict[str, Any]], calculations: list[dict[str, Any]]) -> str | None:
    evidence = ' '.join(quotations)
    if not evidence:
        return 'A located quotation is required.'
    # A current-period contribution is not necessarily a contribution to the
    # change between periods. Require an explicit change scope in the cited
    # text; lexical matching remains a necessary check, not semantic proof.
    if (_CONTRIBUTION.search(text) and _CHANGE_SCOPE.search(text)
            and not any(change_contribution_disclosed(q) for q in quotations)):
        return 'A contribution to a change requires an explicit change-scoped disclosure; current-period revenue alone is insufficient.'
    allowed = _numbers(evidence)
    for item in [*facts, *calculations]:
        allowed |= _numbers(' '.join(str(item.get(k) or '') for k in ('value', 'period', 'unit', 'currency')))
    missing = _numbers(text) - allowed
    # A negative value is often written as a positive magnitude with an explicit decline.
    if re.search(r'\b(?:fell|fall|declined|dropped|decreased|down|loss)\b|下跌|下降|下滑|减少|亏损', text, re.I):
        missing = {value for value in missing if -value not in allowed}
    if missing:
        return 'A number or date is absent from the cited evidence and linked inputs.'
    unit_context = evidence + ' ' + ' '.join(str(item.get('unit') or '') + ' ' + str(item.get('currency') or '') for item in [*facts, *calculations])
    for variants in (('%', 'percent', 'per cent', '百分之'), ('$', 'USD', 'US dollars'), ('€', 'EUR', 'euros'), ('¥', 'CNY', 'RMB', 'yuan')):
        if any(unit.casefold() in text.casefold() for unit in variants) and not any(unit.casefold() in unit_context.casefold() for unit in variants):
            return 'A unit or currency is absent from the cited evidence and linked inputs.'
    for entity in _CAMEL.findall(text):
        if entity not in _IGNORE and entity.casefold() not in evidence.casefold():
            return 'A named entity is absent from this clause’s cited evidence.'
    if re.search(r'\b(?:outperform|underperform|beat|lagged)\w*\b|跑赢|跑输|超越', text, re.I):
        contexts = [c.get('return_context') for c in calculations]
        if len(contexts) < 2 or any(not isinstance(c, dict) or not c.get('identity_verified') or
                not c.get('start_date') or not c.get('end_date') or c.get('entry_type') in (None, 'unknown') or
                c.get('price_basis') in (None, '', 'unknown') or not c.get('start_price_type') or not c.get('end_price_type') for c in contexts):
            return 'Return comparison requires two identified, comparable return calculations.'
        if len({(c['start_date'], c['end_date'], c['entry_type'], c['price_basis'], c['start_price_type'], c['end_price_type']) for c in contexts}) != 1:
            return 'Return periods, entry types, and price bases must match.'
        if any(not c.get('instrument') for c in contexts) or str(contexts[0]['instrument']).casefold() == str(contexts[1]['instrument']).casefold():
            return 'Return comparison requires two distinct identified instruments.'
        try:
            left, right = (Decimal(str(c.get('value'))) for c in calculations[:2])
        except (InvalidOperation, TypeError):
            return 'Return comparison values are unavailable.'
        if (re.search(r'\b(?:outperform|beat)\w*\b|跑赢|超越', text, re.I) and left <= right) or (re.search(r'\b(?:underperform|lagged)\w*\b|跑输', text, re.I) and left >= right):
            return 'Comparison direction conflicts with the linked returns.'
        names = [str(c['instrument']).casefold() for c in contexts[:2]]
        if any(name not in text.casefold() for name in names) or text.casefold().find(names[0]) > text.casefold().find(names[1]):
            return 'Return inputs must identify the compared instruments in narrative order.'
        spread = re.search(r'(?:\bby\s+|(?:跑赢|跑输|超越)[^\d]{0,20})(\d+(?:\.\d+)?)\s*(?:%|percentage points?|个百分点)', text, re.I)
        if spread and abs(Decimal(spread.group(1)) - abs(left - right)) > Decimal('0.02'):
            return 'Comparison spread conflicts with the linked returns.'
    up = re.compile(r'\b(?:rose|rise|rises|increased|increase|grew|growth)\b|上涨|上升|增长|增加', re.I)
    down = re.compile(r'\b(?:fell|fall|decreased|decrease|declined|decline|dropped)\b|下跌|下降|减少|下滑', re.I)
    if (up.search(text) and down.search(evidence) and not up.search(evidence)) or (down.search(text) and up.search(evidence) and not down.search(evidence)):
        return 'The claimed direction conflicts with the cited evidence.'
    if _CAUSE.search(text) and not (_CAUSE.search(evidence) or _INFERENCE.search(text)):
        return 'A causal interpretation needs cited causal evidence or an explicit inference label.'
    return None


def report_problems(report: str, claims: list[dict], calculations: list[dict] | None = None) -> list[dict]:
    """Check measurable assertions against relevant, locally cited clauses."""
    problems = []
    for line in report.splitlines():
        line = line.strip()
        if not line or re.fullmatch(r'\[\[(?:table|chart):[A-Za-z0-9_-]+\]\]', line):
            continue
        for clause in re.split(r'\.(?!\d)|[。;；\n]', line):
            clause = clause.strip(' -*|')
            if not clause:
                continue
            entities = {e.casefold() for e in _CAMEL.findall(clause) if e not in _IGNORE}
            relevant = [c for c in claims if not entities or any(e in (c.get('text', '') + ' ' + ' '.join(x.get('quotation', '') for x in c.get('citations', []))).casefold() for e in entities)]
            if not _numbers(clause) and not entities and not _CAUSE.search(clause):
                continue
            calc_by_id = {c.get('calculation_id'): c for c in calculations or []}
            checks = []
            for claim in relevant:
                evidence = [claim.get('text', ''), *(x.get('quotation', '') for x in claim.get('citations', []))]
                linked = [calc_by_id[k] for k in claim.get('calculation_ids', []) if k in calc_by_id]
                checks.append(clause_problem(clause, evidence, [], linked))
            if not checks or all(checks):
                # A unique deterministic growth result can support a concise growth
                # sentence; its input facts remain linked by the saved calculation.
                growth = re.search(r'\b(?:grew|growth|increased|increase)\b|增长|同比', clause, re.I)
                numeric = _numbers(clause)
                candidates = []
                if growth and len(numeric) == 1 and not entities:
                    for calc in calculations or []:
                        if 'growth' not in str(calc.get('formula_id', '')):
                            continue
                        labels = [str(x).casefold() for x in calc.get('_grounding_labels', []) if x]
                        if not labels or not any(label in clause.casefold() for label in labels):
                            continue
                        if _numbers(str(calc.get('value', ''))) == numeric and str(calc.get('unit', '')).lower() in ('percent', '%'):
                            candidates.append(calc)
                if len(candidates) != 1:
                    problems.append({'clause': clause[:300], 'reason': checks[0] if checks else 'No relevant cited evidence for this clause.'})
    return problems


def recover_draft_claims(state: dict, source_by_id: dict, is_eligible) -> list[dict]:
    facts = {f.get('fact_id'): f for key in ('facts', 'prior_facts') for f in state.get(key, [])}
    calculations = {c.get('calculation_id'): c for key in ('calculations', 'prior_calculations') for c in state.get(key, [])}
    result = []
    for draft in state.get('draft_claims', []):
        citations = draft.get('citations', [])
        valid = bool(citations)
        for cit in citations:
            source = source_by_id.get(cit.get('source_id'))
            loc = cit.get('location', {})
            start, end = loc.get('character_start'), loc.get('character_end')
            if not source or not is_eligible(source) or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source.text) or source.text[start:end] != cit.get('quotation'):
                valid = False
                break
        linked_facts = list(draft.get('fact_ids', []))
        linked_calcs = []
        for calc_id in draft.get('calculation_ids', []):
            calc = calculations.get(calc_id)
            if not calc:
                valid = False
                break
            linked_calcs.append(calc)
            linked_facts.extend(x.get('fact_id') for x in calc.get('inputs', []) if isinstance(x, dict))
        rows = []
        for fact_id in linked_facts:
            fact = facts.get(fact_id)
            source = source_by_id.get(fact.get('source_id')) if fact else None
            if not source or not is_eligible(source):
                valid = False
                break
            start, end = fact.get('character_start'), fact.get('character_end')
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source.text) or (fact.get('quotation') and source.text[start:end] != fact['quotation']):
                valid = False
                break
            rows.append(fact)
        if valid and not clause_problem(draft.get('text', ''), [c['quotation'] for c in citations], rows, linked_calcs):
            result.append({k: draft[k] for k in ('claim_id', 'text', 'role', 'citations', 'challenges_claim_ids', 'fact_ids', 'calculation_ids')})
    return result


def eligible_prior_claims(state: dict, sources: dict, is_eligible) -> list[dict]:
    result = []
    for claim in state.get('prior_claims', []):
        citations = claim.get('citations') or []
        if not citations:
            continue
        valid = True
        for citation in citations:
            source = sources.get(citation.get('source_id'))
            location = citation.get('location') or {}
            start, end = location.get('character_start'), location.get('character_end')
            if not source or not is_eligible(source) or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source.text) or source.text[start:end] != citation.get('quotation'):
                valid = False
                break
        if valid:
            result.append(claim)
    return result


def eligible_calculations(state: dict, sources: dict, is_eligible) -> list[dict]:
    facts = {fact.get('fact_id'): fact for key in ('facts', 'prior_facts') for fact in state.get(key, [])}
    accepted = []
    for calc in [*state.get('prior_calculations', []), *state.get('calculations', [])]:
        inputs = calc.get('inputs') or []
        if not inputs or calc.get('input_status') in ('invalid', 'stale', 'missing'):
            continue
        valid = True
        labels = []
        for input_item in inputs:
            fact = facts.get(input_item.get('fact_id'))
            source = sources.get(fact.get('source_id')) if fact else None
            start, end = (fact.get('character_start'), fact.get('character_end')) if fact else (None, None)
            if not source or not is_eligible(source) or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source.text) or (fact.get('quotation') and fact['quotation'] != source.text[start:end]):
                valid = False
                break
            labels.append(fact.get('label'))
        if valid:
            accepted.append({**calc, '_grounding_labels': labels})
    return accepted
