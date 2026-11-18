"""Merge independently validated analysis pages without rebinding references."""
from copy import deepcopy
from dataclasses import replace

from .research_analysis import AnalysisResult
from .research_gap_followup import remap_followup_artifacts


def merge_analysis_page(base: AnalysisResult, page: AnalysisResult, *, page_id: str,
                        retained: dict | None = None) -> AnalysisResult:
    is_repair = retained is not None
    retained = retained or {}
    fact_map, calc_map = {}, {}
    facts, calculations, omitted = remap_followup_artifacts(
        page.facts, page.calculations, operation_id=page_id,
        existing_fact_ids={x['fact_id'] for x in base.facts if isinstance(x.get('fact_id'), str)},
        existing_facts=base.facts, max_facts=len(page.facts), max_calculations=len(page.calculations),
        fact_id_map=fact_map, calculation_index_map=calc_map,
        retained_facts=retained.get('facts'), retained_calculations=set(retained.get('calculations', {})))
    claim_offset, counter_offset, calc_offset = len(base.claims), len(base.counterarguments), len(base.calculations)
    calc_map = {k: calc_offset + v for k, v in calc_map.items()} | retained.get('calculations', {})
    def has_unavailable_calculation(row):
        return any(type(i) is not int or i not in calc_map
                   for assertion in row.get('assertion_suggestions', []) for i in assertion.get('calculation_indexes', [])) or any(
            not isinstance(ref, dict) or type(ref.get('calculation_index')) is not int or ref['calculation_index'] not in calc_map
            for ref in row.get('suggested_calculations', []))
    def finding(row):
        row = deepcopy(row)
        for assertion in row.get('assertion_suggestions', []):
            assertion['calculation_indexes'] = [calc_map[i] for i in assertion.get('calculation_indexes', [])]
        row['suggested_calculations'] = [{'calculation_index': calc_map[x['calculation_index']]}
            for x in row.get('suggested_calculations', [])]
        return row
    claims, claim_map, rejected_refs = [], dict(retained.get('claims', {})), set()
    for index, row in enumerate(page.claims):
        if index in claim_map:
            continue
        if has_unavailable_calculation(row):
            omitted += 1
            rejected_refs.update(row.get('model_absence_refs', []))
            continue
        claim_map[index] = claim_offset + len(claims)
        claims.append(finding(row))
    counters, counter_map = [], dict(retained.get('counterarguments', {}))
    for index, row in enumerate(page.counterarguments):
        if index in counter_map:
            continue
        targets = row.get('suggested_responds_to_claim_indexes', [])
        if has_unavailable_calculation(row) or any(type(i) is not int or i not in claim_map for i in targets):
            omitted += 1
            continue
        copied = finding(row)
        copied['suggested_responds_to_claim_indexes'] = [claim_map[i] for i in targets]
        counter_map[index] = counter_offset + len(counters)
        counters.append(copied)
    if rejected_refs:
        for row in claims:
            if 'model_absence_refs' in row:
                row['model_absence_refs'] = [ref for ref in row['model_absence_refs'] if ref not in rejected_refs]
    questions = deepcopy(base.research_questions)
    qmap = dict(retained.get('research_questions', {}))
    for index, item in enumerate(page.research_questions):
        if index in qmap:
            continue
        copied = deepcopy(item)
        if any(type(i) is not int or i not in claim_map for i in item.get('claim_indexes', [])):
            omitted += 1
            continue
        copied['claim_indexes'] = [claim_map[i] for i in item.get('claim_indexes', [])]
        existing = [] if is_repair else [i for i, q in enumerate(questions) if q.get('question') == item.get('question')]
        if len(existing) == 1:
            qmap[index] = existing[0]
            q = questions[existing[0]]
            q['claim_indexes'] = list(dict.fromkeys([*q.get('claim_indexes', []), *copied['claim_indexes']]))
        else:
            qmap[index] = len(questions)
            questions.append(copied)
    links = deepcopy(base.requirement_links)
    link_map = dict(retained.get('requirement_links', {}))
    for link_index, link in enumerate(page.requirement_links):
        if link_index in link_map:
            continue
        copied = deepcopy(link)
        qi = copied.get('question_index')
        if qi is not None and qi not in qmap:
            continue
        copied['question_index'] = qmap.get(qi)
        old_facts, old_calcs = copied.get('fact_ids', []), copied.get('calculation_indexes', [])
        if any(x not in fact_map for x in old_facts) or any(x not in calc_map for x in old_calcs):
            continue
        copied['fact_ids'] = [fact_map[x] for x in old_facts]
        copied['calculation_indexes'] = [calc_map[x] for x in old_calcs]
        link_map[link_index] = len(links)
        links.append(copied)
    needs = deepcopy(base.evidence_needs)
    need_map = dict(retained.get('evidence_needs', {}))
    for need_index, need in enumerate(page.evidence_needs):
        if need_index in need_map:
            continue
        copied = deepcopy(need)
        index = copied.get('question_index')
        if index is not None:
            if type(index) is not int or index not in qmap:
                omitted += 1
                continue
            copied['question_index'] = qmap[index]
        need_map[need_index] = len(needs)
        needs.append(copied)
    # Absence observations use opaque observation_ref identifiers; claims
    # carry these references already, not page-local claim indexes.
    absences = deepcopy(base.absence_observations)
    absence_map = dict(retained.get('absence_observations', {}))
    for index, observation in enumerate(page.absence_observations):
        if index in absence_map:
            continue
        if observation.get('observation_ref') in rejected_refs:
            omitted += 1
            continue
        absence_map[index] = len(absences)
        absences.append(deepcopy(observation))
    absence_refs = {page.absence_observations[index].get('observation_ref'): base.absence_observations[ref].get('observation_ref')
                    for index, ref in retained.get('absence_observations', {}).items()}
    for row in claims:
        if 'model_absence_refs' in row:
            row['model_absence_refs'] = [absence_refs.get(ref, ref) for ref in row['model_absence_refs']]
    # Run-local IDs retain their call scope and point only to kept objects.
    scoped = deepcopy(base.structured_output_ids)
    pages = scoped.setdefault('pages', {})
    ids = page.structured_output_ids
    mapped = {'claims': {k:claim_map[v] for k,v in ids.get('claims', {}).items() if type(v) is int and v in claim_map},
              'counterarguments': {k:counter_map[v] for k,v in ids.get('counterarguments', {}).items() if type(v) is int and v in counter_map},
              'calculations': {k:calc_map[v] for k,v in ids.get('calculations', {}).items() if type(v) is int and v in calc_map},
              'facts': {key: fact_map[value] for key, value in ids.get('facts', {}).items() if value in fact_map},
              'evidence_needs': {key: need_map[value] for key, value in ids.get('evidence_needs', {}).items() if type(value) is int and value in need_map},
              'absence_observations': {key: absence_map[value] for key, value in ids.get('absence_observations', {}).items() if type(value) is int and value in absence_map},
              'requirement_links': {key: link_map[value] for key, value in ids.get('requirement_links', {}).items() if type(value) is int and value in link_map},
              'research_questions': {k:qmap[v] for k,v in ids.get('research_questions', {}).items() if type(v) is int and v in qmap}, 'evidence': {}}
    for key, refs in ids.get('evidence', {}).items():
        saved = []
        for ref in refs:
            row = deepcopy(ref)
            collection, idx = row.get('collection'), row.get('finding_index')
            if collection in ('claims','counterarguments') and type(idx) is int:
                mapping = claim_map if collection == 'claims' else counter_map
                if idx not in mapping: continue
                row['finding_index'] = mapping[idx]
            elif collection == 'facts':
                if row.get('fact_id') not in fact_map: continue
                row['fact_id'] = fact_map[row['fact_id']]
            else: continue
            saved.append(row)
        if saved: mapped['evidence'][key] = saved
    pages[page_id] = mapped
    statuses = (base.structured_output_status, page.structured_output_status)
    status = 'failed' if 'failed' in statuses else 'not_validated' if 'not_validated' in statuses else 'partial' if 'partial' in statuses or omitted else 'complete'
    return replace(base, claims=[*base.claims, *claims], counterarguments=[*base.counterarguments, *counters],
        facts=[*base.facts, *facts], calculations=[*base.calculations, *calculations], research_questions=questions,
        requirement_links=links, evidence_needs=needs,
        absence_observations=absences,
        discovered_requirements=[*base.discovered_requirements, *page.discovered_requirements],
        segments=[*base.segments, *page.segments], source_ids=list(dict.fromkeys([*base.source_ids, *page.source_ids])),
        limitations=list(dict.fromkeys([*base.limitations, *page.limitations, *(['Some page artifacts were omitted during reference mapping.'] if omitted else [])])),
        selection_diagnostics=[*base.selection_diagnostics, *page.selection_diagnostics],
        pdf_structure_diagnostics=[*base.pdf_structure_diagnostics, *page.pdf_structure_diagnostics],
        structured_output_status=status, structured_output_errors=list(dict.fromkeys([*base.structured_output_errors, *page.structured_output_errors])),
        structured_output_ids=scoped, work_page=deepcopy(page.work_page), unprocessed_ranges=deepcopy(page.unprocessed_ranges))


def merge_repair_patch(base: AnalysisResult, page: AnalysisResult, *, page_id: str,
                       targets: list[dict], dependencies: list[dict]) -> AnalysisResult:
    """Merge only independently validated repair slots; ambiguous bindings fail closed.

    The validation pipeline must expose only objects that survived evidence and
    numerical checks in structured_output_ids.  Until a dependency can be
    mapped without rebinding its references, a patch with retained dependencies
    is rejected rather than duplicating or silently changing them.
    """
    import json
    from hashlib import sha256
    ids = page.structured_output_ids
    collections = ('claims', 'counterarguments', 'facts', 'calculations', 'research_questions',
                   'evidence_needs', 'absence_observations', 'requirement_links')
    requested = {(x.get('collection'), x.get('slot_id')) for x in targets if isinstance(x, dict)}
    dependency_keys = {(x.get('collection'), x.get('slot_id')) for x in dependencies if isinstance(x, dict)}
    allowed_collections = (*collections, 'evidence')
    if (len(requested) != len(targets) or len(dependency_keys) != len(dependencies)
            or requested & dependency_keys or any(c not in allowed_collections or not isinstance(i, str)
                                                    for c, i in requested | dependency_keys)):
        raise ValueError('Invalid repair slots.')
    retained = {}
    for name in collections:
        mapping = ids.get(name, {})
        if not isinstance(mapping, dict):
            raise ValueError('Invalid validated slot mapping.')
        expected = {slot for collection, slot in requested | dependency_keys if collection == name}
        if set(mapping) != expected:
            raise ValueError('Repair slots do not match validated objects.')
        rows = getattr(page, name)
        values = list(mapping.values())
        actual = [row.get('fact_id') for row in rows] if name == 'facts' else list(range(len(rows)))
        if len(set(values)) != len(values) or set(values) != set(actual):
            raise ValueError('Repair object mapping is incomplete or ambiguous.')
    evidence_slots = {slot for collection, slot in requested | dependency_keys if collection == 'evidence'}
    evidence_map = ids.get('evidence', {})
    if not isinstance(evidence_map, dict) or set(evidence_map) != evidence_slots or any(not isinstance(v, list) or not v for v in evidence_map.values()):
        raise ValueError('Evidence slots do not match validated references.')
    def evidence_values(analysis, refs):
        values = set()
        for item in refs:
            if not isinstance(item, dict): raise ValueError('Invalid evidence reference.')
            kind = item.get('collection')
            if kind == 'facts':
                rows = [row for row in analysis.facts if row.get('fact_id') == item.get('fact_id')]
                if len(rows) != 1: raise ValueError('Invalid evidence fact.')
                row = rows[0]
            elif kind in {'claims', 'counterarguments'}:
                rows = getattr(analysis, kind)
                index, citation = item.get('finding_index'), item.get('citation_index')
                if type(index) is not int or not 0 <= index < len(rows): raise ValueError('Invalid evidence finding.')
                citations = rows[index].get('citations', [])
                if type(citation) is not int or not 0 <= citation < len(citations): raise ValueError('Invalid evidence citation.')
                row = citations[citation]
            else: raise ValueError('Invalid evidence collection.')
            value = (row.get('source_id'), row.get('quotation'))
            if not all(isinstance(x, str) and x for x in value): raise ValueError('Invalid evidence content.')
            values.add(value)
        return values
    for dep in dependencies:
        name, slot, ref = dep['collection'], dep['slot_id'], dep.get('retained_ref')
        if name == 'evidence':
            if not isinstance(ref, list) or dep.get('digest') != sha256(json.dumps(ref, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest():
                raise ValueError('Retained evidence changed.')
            if evidence_values(base, ref) != evidence_values(page, evidence_map[slot]):
                raise ValueError('Retained evidence content changed.')
            continue
        rows = getattr(base, name)
        if name == 'facts':
            matches = [row for row in rows if row.get('fact_id') == ref]
            if len(matches) != 1:
                raise ValueError('Retained fact is ambiguous.')
            row = matches[0]
        else:
            if type(ref) is not int or not 0 <= ref < len(rows):
                raise ValueError('Retained index is invalid.')
            row = rows[ref]
        digest = sha256(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if dep.get('digest') != digest:
            raise ValueError('Retained dependency changed.')
        retained.setdefault(name, {})[ids[name][slot]] = ref
    merged = merge_analysis_page(base, page, page_id=page_id, retained=retained)
    mapped = merged.structured_output_ids['pages'][page_id]
    if any(set(mapped.get(name, {})) != {slot for collection, slot in requested | dependency_keys if collection == name}
           for name in collections):
        raise ValueError('A repair slot failed reference mapping.')
    if set(mapped.get('evidence', {})) != evidence_slots:
        raise ValueError('Evidence references failed mapping.')
    report = base.structured_output_ids.get('slot_report', {})
    rejected = set(report.get('rejected', [])) if isinstance(report, dict) else set()
    if rejected and rejected == {slot for _, slot in requested} and set(base.structured_output_errors).issubset({'shape_or_reference'}) and not page.structured_output_errors:
        merged = replace(merged, structured_output_status='complete', structured_output_errors=[])
    return merged


def rebind_repair_continuation(continuation: dict, page: AnalysisResult, merged: AnalysisResult, *, page_id: str) -> dict:
    """Bind a later page's retained slots to their uniquely mapped merged objects."""
    import json
    from hashlib import sha256
    result = deepcopy(continuation)
    saved = merged.structured_output_ids.get('pages', {}).get(page_id, {})
    for item in result.get('pending', []):
        for dep in item.get('dependencies', []):
            name, old = dep.get('collection'), dep.get('retained_ref')
            local = page.structured_output_ids.get(name, {})
            global_map = saved.get(name, {})
            if not isinstance(local, dict) or not isinstance(global_map, dict):
                raise ValueError('Missing page dependency mapping.')
            matches = [key for key, value in local.items() if value == old and key in global_map]
            if len(matches) != 1:
                raise ValueError('Ambiguous page dependency mapping.')
            ref = deepcopy(global_map[matches[0]])
            if name == 'evidence':
                row = ref
            elif name == 'facts':
                rows = [value for value in merged.facts if value.get('fact_id') == ref]
                if len(rows) != 1: raise ValueError('Ambiguous retained fact.')
                row = rows[0]
            else:
                rows = getattr(merged, name, None)
                if not isinstance(rows, list) or type(ref) is not int or not 0 <= ref < len(rows):
                    raise ValueError('Invalid retained dependency.')
                row = rows[ref]
            dep['retained_ref'] = ref
            dep['digest'] = sha256(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return result
