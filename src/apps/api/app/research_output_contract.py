"""Versioned generation contract and deterministic conversion to legacy analysis.

A draft is never evidence: the converted objects still pass source and numerical
checks in research_analysis. Invalid objects and all their dependants are omitted.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Evidence(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    source_id: str = Field(min_length=1, max_length=100)
    quotation: str = Field(min_length=1, max_length=1000)
    source_page: int | None = Field(default=None, ge=1, strict=True)


class Fact(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=300)
    value: str = Field(min_length=1, max_length=100)
    unit: str = Field(min_length=1, max_length=100)
    currency: str | None = None
    period: str = Field(min_length=1, max_length=100)
    evidence_id: str


class Calculation(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    formula: Literal['growth_percent', 'margin_percent', 'difference']
    first_fact_id: str
    second_fact_id: str


class Assertion(StrictModel):
    text: str = Field(min_length=1, max_length=1000)
    evidence_ids: list[str] = Field(max_length=10)
    calculation_ids: list[str] = Field(max_length=20)


class Finding(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=3000)
    scope_declaration: Literal['absence', 'other', 'unknown']
    assertions: list[Assertion] = Field(max_length=20)
    responds_to: list[str] = Field(max_length=20)


class Dimensions(StrictModel):
    entities: list[str] = Field(max_length=6)
    metrics: list[str] = Field(max_length=6)
    periods: list[str] = Field(max_length=6)
    units: list[str] = Field(max_length=6)
    qualifiers: list[str] = Field(max_length=6)


class Question(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=500)
    claim_ids: list[str] = Field(max_length=20)
    dimensions: Dimensions | None
    calculation_requested: bool | None
    retrieval_hints: list[str] = Field(max_length=6)


class EvidenceNeed(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    question_id: str | None
    requested_fact: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=300)
    source_kind: str = Field(min_length=1, max_length=160)
    action: str = Field(min_length=1, max_length=160)


class Absence(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=500)
    source_ids: list[str] = Field(max_length=20)
    scope: Literal['selected_excerpt', 'full_source', 'unknown']
    claim_ids: list[str] = Field(max_length=20)


class RequirementLink(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    question_id: str | None
    fact_ids: list[str] = Field(max_length=30)
    calculation_ids: list[str] = Field(max_length=20)
    source_ids: list[str] = Field(max_length=20)


class ResearchDraft(StrictModel):
    version: Literal['research-draft-v1']
    evidence: list[Evidence] = Field(max_length=100)
    facts: list[Fact] = Field(max_length=100)
    calculations: list[Calculation] = Field(max_length=50)
    claims: list[Finding] = Field(max_length=30)
    counterarguments: list[Finding] = Field(max_length=20)
    research_questions: list[Question] = Field(max_length=10)
    evidence_needs: list[EvidenceNeed] = Field(max_length=20)
    absence_observations: list[Absence] = Field(max_length=20)
    requirement_links: list[RequirementLink] = Field(max_length=20)
    limitations: list[str] = Field(max_length=30)


EXAMPLE = {'version':'research-draft-v1',
 'evidence':[{'id':'e1','source_id':'s1','quotation':'Revenue was $10 in 2024.','source_page':None}],
 'facts':[{'id':'f1','label':'Revenue','value':'10','unit':'dollars','currency':'USD','period':'2024','evidence_id':'e1'}],
 'calculations':[{'id':'k1','formula':'difference','first_fact_id':'f1','second_fact_id':'f1'}],
 'claims':[{'id':'c1','text':'Revenue was $10.','scope_declaration':'other','assertions':[{'text':'Revenue was $10.','evidence_ids':['e1'],'calculation_ids':['k1']}],'responds_to':[]}],
 'counterarguments':[{'id':'c2','text':'The excerpt limits the conclusion.','scope_declaration':'other','assertions':[{'text':'The excerpt limits the conclusion.','evidence_ids':['e1'],'calculation_ids':[]}],'responds_to':['c1']}],
 'research_questions':[{'id':'q1','question':'What was revenue?','claim_ids':['c1'],'dimensions':{'entities':['company'],'metrics':['revenue'],'periods':['2024'],'units':['dollars'],'qualifiers':[]},'calculation_requested':False,'retrieval_hints':['revenue']}],
 'evidence_needs':[{'id':'n1','question_id':'q1','requested_fact':'prior revenue','reason':'comparison','source_kind':'filing','action':'retrieve'}],
 'absence_observations':[{'id':'a1','text':'Prior revenue was not found in the excerpt.','source_ids':['s1'],'scope':'selected_excerpt','claim_ids':['c1']}],
 'requirement_links':[{'id':'l1','question_id':'q1','fact_ids':['f1'],'calculation_ids':['k1'],'source_ids':['s1']}],
 'limitations':['The excerpt is incomplete.']}


@dataclass(frozen=True)
class StructuredOutputContract:
    name: str
    version: str
    schema: dict
    example: dict

    def serialized_schema_bytes(self) -> int:
        return len(json.dumps(self.schema, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8'))


ANALYSIS_CONTRACT = StructuredOutputContract('research_draft_v1', 'research-draft-v1', ResearchDraft.model_json_schema(), EXAMPLE)
ResearchDraft.model_validate(EXAMPLE)


class DraftContractError(ValueError):
    """Controlled diagnostics never contain model values or unknown key names."""
    def __init__(self, errors: list[dict]):
        super().__init__('Invalid research draft')
        self.errors = errors


def _json_type(value: object) -> str:
    if value is None: return 'null'
    if isinstance(value, bool): return 'boolean'
    if isinstance(value, str): return 'string'
    if isinstance(value, (int, float)): return 'number'
    if isinstance(value, list): return 'array'
    if isinstance(value, dict): return 'object'
    return 'other'


def convert_draft(raw: object, available_fact_ids: set[str] | None = None) -> tuple[dict, list[str], dict]:
    """Validate independently; duplicate IDs and invalid dependencies fail closed.

    Array positions are translated only after validation. IDs never refer to a
    different surviving object. Unknown top-level fields reject the whole draft.
    """
    allowed = set(ResearchDraft.model_fields)
    if not isinstance(raw, dict):
        raise DraftContractError([{'type': 'envelope_type', 'actual_type': _json_type(raw)}])
    errors = []
    missing = allowed - raw.keys()
    if 'version' in missing:
        errors.append({'type': 'missing_version'})
    elif not isinstance(raw['version'], str):
        errors.append({'type': 'version_type', 'actual_type': _json_type(raw['version'])})
    elif raw['version'] != ANALYSIS_CONTRACT.version:
        errors.append({'type': 'version_mismatch'})
    missing_fields = sorted(missing - {'version'})
    if missing_fields:
        errors.append({'type': 'missing_fields', 'fields': missing_fields, 'count': len(missing_fields)})
    extra_count = sum(key not in allowed for key in raw)
    if extra_count:
        errors.append({'type': 'extra_fields', 'count': extra_count})
    if errors:
        raise DraftContractError(errors)
    types = {'evidence': Evidence, 'facts': Fact, 'calculations': Calculation,
             'claims': Finding, 'counterarguments': Finding, 'research_questions': Question, 'evidence_needs': EvidenceNeed, 'absence_observations': Absence, 'requirement_links': RequirementLink}
    limits = {'evidence':100, 'facts':100, 'calculations':50, 'claims':30, 'counterarguments':20, 'research_questions':10, 'evidence_needs':20, 'absence_observations':20, 'requirement_links':20}
    rows = {}
    problems = []
    for key, typ in types.items():
        value = raw[key]
        if not isinstance(value, list) or len(value) > limits[key]:
            raise DraftContractError([{'type': 'collection_type' if not isinstance(value, list) else 'collection_limit', 'field': key, 'actual_type': _json_type(value), **({'count': len(value)} if isinstance(value, list) else {})}])
        counts = Counter(x.get('id') for x in value if isinstance(x, dict) and isinstance(x.get('id'), str))
        valid = []
        for row in value:
            try:
                item = typ.model_validate(row)
                if counts[item.id] != 1:
                    raise ValueError('duplicate id')
                valid.append(item)
            except (ValidationError, ValueError):
                problems.append(key + ': invalid object or duplicate id')
        rows[key] = valid
    if not isinstance(raw['limitations'], list) or len(raw['limitations']) > 30 or any(not isinstance(x, str) for x in raw['limitations']):
        raise DraftContractError([{'type': 'invalid_limitations', 'field': 'limitations', 'actual_type': _json_type(raw['limitations']), **({'count': len(raw['limitations'])} if isinstance(raw['limitations'], list) else {})}])
    evidence = {e.id: e for e in rows['evidence']}
    collisions = {f.id for f in rows['facts']} & (available_fact_ids or set())
    if collisions: problems.append('facts: id collides with supplied fact')
    facts = [f for f in rows['facts'] if f.evidence_id in evidence and f.id not in collisions]
    if len(facts) != len(rows['facts']): problems.append('facts: invalid evidence reference')
    fact_ids = ({f.id for f in facts} | (available_fact_ids or set())) - collisions
    calcs = [c for c in rows['calculations'] if c.first_fact_id in fact_ids and c.second_fact_id in fact_ids]
    if len(calcs) != len(rows['calculations']): problems.append('calculations: invalid fact reference')
    calc_index = {c.id:i for i,c in enumerate(calcs)}
    def findings(items, claim_index=None):
        output = []
        for item in items:
            ids = list(dict.fromkeys(e for a in item.assertions for e in a.evidence_ids))
            if (not item.assertions or not ids or any(e not in evidence for e in ids) or
                any(c not in calc_index for a in item.assertions for c in a.calculation_ids) or
                (claim_index is None and item.responds_to) or
                (claim_index is not None and any(c not in claim_index for c in item.responds_to))):
                problems.append('finding: invalid dependency'); continue
            citations = [evidence[e].model_dump(exclude={'id'}, exclude_none=True) for e in ids]
            out = {'text':item.text,'scope_declaration':item.scope_declaration,'citations':citations,
                'calculation_indexes':list(dict.fromkeys(calc_index[c] for a in item.assertions for c in a.calculation_ids)), 'assertions':[{'text':a.text,
                'citation_indexes':[ids.index(e) for e in a.evidence_ids],
                'calculation_indexes':[calc_index[c] for c in a.calculation_ids]} for a in item.assertions]}
            if claim_index is not None: out['responds_to_claim_indexes'] = [claim_index[c] for c in item.responds_to]
            output.append((item.id,out))
        return output
    claims = findings(rows['claims'])
    counters = findings(rows['counterarguments'], {id:i for i,(id,_) in enumerate(claims)})
    claim_index = {id:i for i,(id,_) in enumerate(claims)}
    questions = []
    for q in rows['research_questions']:
        if any(id not in claim_index for id in q.claim_ids):
            problems.append('research_questions: invalid claim reference'); continue
        questions.append((q.id, {'question':q.question,'claim_indexes':[claim_index[id] for id in q.claim_ids],
            'dimensions':q.dimensions.model_dump() if q.dimensions else None,
            'calculation_requested':q.calculation_requested,'retrieval_hints':q.retrieval_hints}))
    question_index = {id:i for i,(id,_) in enumerate(questions)}
    needs = []
    need_ids = []
    for n in rows['evidence_needs']:
        if n.question_id is not None and n.question_id not in question_index:
            problems.append('evidence_needs: invalid question reference'); continue
        need_ids.append(n.id)
        needs.append({**n.model_dump(exclude={'id','question_id'}), 'question_index':question_index.get(n.question_id)})
    absences = []
    absence_ids = []
    for a in rows['absence_observations']:
        if any(id not in claim_index for id in a.claim_ids):
            problems.append('absence_observations: invalid claim reference'); continue
        absence_ids.append(a.id)
        absences.append({**a.model_dump(exclude={'id','claim_ids'}), 'claim_indexes':[claim_index[id] for id in a.claim_ids]})
    links = []
    link_ids = []
    for link in rows['requirement_links']:
        if ((link.question_id is not None and link.question_id not in question_index) or
            any(id not in fact_ids for id in link.fact_ids) or any(id not in calc_index for id in link.calculation_ids)):
            problems.append('requirement_links: invalid reference'); continue
        link_ids.append(link.id)
        links.append({'question_index':question_index.get(link.question_id),'fact_ids':link.fact_ids,
            'calculation_indexes':[calc_index[id] for id in link.calculation_ids], 'source_ids':link.source_ids})
    return {'research_questions':[x for _,x in questions], 'evidence_needs':needs,
            'absence_observations':absences, 'requirement_links':links,
            'claims':[x for _,x in claims], 'counterarguments':[x for _,x in counters],
            'facts':[{'fact_id':f.id,'label':f.label,'value':f.value,'unit':f.unit,'currency':f.currency,'period':f.period,
                      'citation':evidence[f.evidence_id].model_dump(exclude={'id'}, exclude_none=True)} for f in facts],
            'calculations':[c.model_dump(exclude={'id'}) for c in calcs], 'limitations':raw['limitations']}, problems, {'claims':[id for id,_ in claims], 'counterarguments':[id for id,_ in counters], 'calculations':[c.id for c in calcs], 'research_questions':[id for id,_ in questions], 'facts':[f.id for f in facts], 'evidence_needs':need_ids, 'absence_observations':absence_ids, 'requirement_links':link_ids, 'evidence':{id:e.model_dump(exclude={'id'}, exclude_none=True) for id,e in evidence.items()}, 'evidence_refs':{id: {'claims':[f.id for f in rows['claims'] if any(id in a.evidence_ids for a in f.assertions)], 'counterarguments':[f.id for f in rows['counterarguments'] if any(id in a.evidence_ids for a in f.assertions)], 'facts':[f.id for f in facts if f.evidence_id == id]} for id in evidence}}


def draft_slot_report(raw: object, validated_ids: dict) -> dict:
    """Describe original object positions and rejected dependency closure.

    IDs in the response are used only to resolve edges within this one draft;
    authority belongs to server-created collection/position slot IDs. No model
    text or arbitrary field names are returned.
    """
    collections = ('evidence', 'facts', 'calculations', 'claims', 'counterarguments',
                   'research_questions', 'evidence_needs', 'absence_observations', 'requirement_links')
    if not isinstance(raw, dict) or any(not isinstance(raw.get(c), list) for c in collections):
        return {'slots': [], 'rejected': [], 'dependencies': {}}
    slots = []
    by_id: dict[tuple[str, str], list[str]] = {}
    for collection in collections:
        for index, row in enumerate(raw[collection]):
            slot_id = f'{collection}-{index}'
            identifier = row.get('id') if isinstance(row, dict) else None
            if isinstance(identifier, str):
                by_id.setdefault((collection, identifier), []).append(slot_id)
            saved = validated_ids.get(collection, {})
            valid = isinstance(identifier, str) and isinstance(saved, dict) and identifier in saved
            slots.append({'slot_id': slot_id, 'collection': collection, 'index': index, 'valid': valid})
    dependencies: dict[str, list[str]] = {}
    def add(edges: list[str], collection: str, value: object) -> None:
        values = value if isinstance(value, list) else [value]
        for identifier in values:
            if isinstance(identifier, str):
                matches = by_id.get((collection, identifier), [])
                if len(matches) == 1 and matches[0] not in edges:
                    edges.append(matches[0])
    for slot in slots:
        row = raw[slot['collection']][slot['index']]
        if not isinstance(row, dict):
            continue
        edges: list[str] = []
        c = slot['collection']
        if c == 'facts': add(edges, 'evidence', row.get('evidence_id'))
        if c == 'calculations':
            add(edges, 'facts', row.get('first_fact_id')); add(edges, 'facts', row.get('second_fact_id'))
        if c in ('claims', 'counterarguments'):
            assertions = row.get('assertions')
            if isinstance(assertions, list):
                for assertion in assertions:
                    if isinstance(assertion, dict):
                        add(edges, 'evidence', assertion.get('evidence_ids'))
                        add(edges, 'calculations', assertion.get('calculation_ids'))
            if c == 'counterarguments': add(edges, 'claims', row.get('responds_to'))
        if c in ('research_questions', 'absence_observations'): add(edges, 'claims', row.get('claim_ids'))
        if c in ('evidence_needs', 'requirement_links'): add(edges, 'research_questions', row.get('question_id'))
        if c == 'requirement_links':
            add(edges, 'facts', row.get('fact_ids')); add(edges, 'calculations', row.get('calculation_ids'))
        dependencies[slot['slot_id']] = edges
    referenced = {edge for edges in dependencies.values() for edge in edges}
    for slot in slots:
        if slot['collection'] == 'evidence' and slot['slot_id'] not in referenced and not slot['valid']:
            slot['unused'] = True
    rejected = {slot['slot_id'] for slot in slots if not slot['valid'] and not slot.get('unused')}
    # Dependants cannot safely survive a rejected prerequisite.
    changed = True
    while changed:
        old = len(rejected)
        rejected.update(slot for slot, edges in dependencies.items() if rejected.intersection(edges))
        changed = len(rejected) != old
    return {'slots': slots, 'rejected': sorted(rejected), 'dependencies': dependencies}


def validated_slot_snapshots(raw: object, report: dict, stable_ids: dict, retained: dict) -> dict:
    """Build private DTOs solely for objects retained by final validation."""
    from copy import deepcopy
    types = {'evidence': Evidence, 'facts': Fact, 'calculations': Calculation, 'claims': Finding,
             'counterarguments': Finding, 'research_questions': Question, 'evidence_needs': EvidenceNeed,
             'absence_observations': Absence, 'requirement_links': RequirementLink}
    if not isinstance(raw, dict): return {}
    slots = report.get('slots', [])
    by_id = {}
    for slot in slots:
        row = raw[slot['collection']][slot['index']]
        if isinstance(row, dict) and isinstance(row.get('id'), str):
            key = (slot['collection'], row['id'])
            by_id.setdefault(key, []).append(slot['slot_id'])
    def translated(collection, value):
        if value is None: return None
        if isinstance(value, list): return [translated(collection, x) for x in value]
        matches = by_id.get((collection, value), []) if isinstance(value, str) else []
        return matches[0] if len(matches) == 1 else value
    result = {}
    from .research_generation_repair import fingerprint
    for slot in slots:
        if not slot.get('valid'): continue
        collection, position, sid = slot['collection'], slot['index'], slot['slot_id']
        try:
            dto = types[collection].model_validate(raw[collection][position]).model_dump()
        except ValidationError:
            continue
        original_id = dto['id']
        ref = stable_ids.get(collection, {}).get(original_id)
        if ref is None: continue
        dto['id'] = sid
        if collection == 'facts': dto['evidence_id'] = translated('evidence', dto['evidence_id'])
        if collection == 'calculations':
            for key in ('first_fact_id', 'second_fact_id'): dto[key] = translated('facts', dto[key])
        if collection in ('claims', 'counterarguments'):
            for assertion in dto['assertions']:
                assertion['evidence_ids'] = translated('evidence', assertion['evidence_ids'])
                assertion['calculation_ids'] = translated('calculations', assertion['calculation_ids'])
            if collection == 'counterarguments': dto['responds_to'] = translated('claims', dto['responds_to'])
        if collection in ('research_questions', 'absence_observations'): dto['claim_ids'] = translated('claims', dto['claim_ids'])
        if collection in ('evidence_needs', 'requirement_links'): dto['question_id'] = translated('research_questions', dto['question_id'])
        if collection == 'requirement_links':
            dto['fact_ids'] = translated('facts', dto['fact_ids'])
            dto['calculation_ids'] = translated('calculations', dto['calculation_ids'])
        rows = retained.get(collection)
        if collection == 'facts' and isinstance(rows, list):
            base = next((x for x in rows if x.get('fact_id') == ref), None)
        elif collection == 'evidence':
            # Evidence references are embedded in retained objects, not a separate row.
            base = ref
        else:
            base = rows[ref] if isinstance(rows, list) and type(ref) is int and 0 <= ref < len(rows) else None
        if base is None: continue
        result[sid] = {'collection': collection, 'slot_id': sid, 'retained_ref': deepcopy(ref),
                       'digest': fingerprint(base), 'dto': dto, 'dto_digest': fingerprint(dto)}
    return result
