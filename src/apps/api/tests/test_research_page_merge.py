from dataclasses import asdict
from app.research_analysis import AnalysisResult
from app.research_page_merge import merge_analysis_page
from app.research_recovery import AnalysisSnapshot


def test_pages_remap_questions_needs_and_scoped_evidence_without_rebinding():
    base = AnalysisResult(claims=[{'text':'A','citations':[], 'model_absence_refs':['abs-a']}],
        counterarguments=[], limitations=[], source_ids=['a'], total_tokens=1,
        facts=[{'fact_id':'f1','value':'1','source_id':'a'}],
        research_questions=[{'question':'first','claim_indexes':[0]}],
        evidence_needs=[{'question_index':0,'question':'first'}],
        absence_observations=[{'observation_ref':'abs-a','source_ids':['a']}],
        structured_output_status='complete')
    page = AnalysisResult(claims=[{'text':'B','citations':[{'source_id':'b'}], 'model_absence_refs':['abs-b']}],
        counterarguments=[], limitations=[], source_ids=['b'], total_tokens=1,
        facts=[{'fact_id':'f1','value':'2','source_id':'b'}],
        research_questions=[{'question':'second','claim_indexes':[0]}],
        evidence_needs=[{'question_index':0,'question':'second'}],
        absence_observations=[{'observation_ref':'abs-b','source_ids':['b']}],
        structured_output_status='complete', structured_output_ids={
            'claims':{'c1':0}, 'facts':{'f1':'f1'}, 'research_questions':{'q1':0},
            'evidence_needs':{'n1':0}, 'absence_observations':{'a1':0},
            'evidence':{'e1':[{'collection':'claims','finding_index':0,'citation_index':0},
                              {'collection':'facts','fact_id':'f1'}]}})
    merged = merge_analysis_page(base, page, page_id='run:page:2')
    assert merged.research_questions[1]['claim_indexes'] == [1]
    assert merged.evidence_needs[1]['question_index'] == 1
    assert merged.claims[1]['model_absence_refs'] == ['abs-b']
    assert merged.absence_observations[1]['observation_ref'] == 'abs-b'
    mapped = merged.structured_output_ids['pages']['run:page:2']
    assert mapped['claims']['c1'] == 1 and mapped['research_questions']['q1'] == 1
    assert mapped['evidence_needs']['n1'] == 1 and mapped['absence_observations']['a1'] == 1
    assert mapped['facts']['f1'] == merged.facts[1]['fact_id'] != 'f1'
    assert mapped['evidence']['e1'][0]['finding_index'] == 1
    assert mapped['evidence']['e1'][1]['fact_id'] == merged.facts[1]['fact_id']
    assert AnalysisSnapshot.model_validate(asdict(merged)).model_dump()['evidence_needs'][1]['question_index'] == 1


def test_unavailable_calculation_removes_dependent_claim_and_links():
    base = AnalysisResult([], [], [], [], 1, structured_output_status='complete')
    page = AnalysisResult(claims=[{'text':'Dependent','citations':[],
        'assertion_suggestions':[{'text':'x','calculation_indexes':[0]}],
        'model_absence_refs':['abs-x']}], counterarguments=[{'text':'Counter',
        'suggested_responds_to_claim_indexes':[0]}], limitations=[], source_ids=[], total_tokens=1,
        calculations=[{'inputs':[{'fact_id':'missing'},{'fact_id':'missing'}]}],
        research_questions=[{'question':'Q','claim_indexes':[0]}],
        evidence_needs=[{'question_index':0,'question':'Q'}],
        absence_observations=[{'observation_ref':'abs-x'}], structured_output_status='complete',
        structured_output_ids={'claims':{'c':0}, 'counterarguments':{'d':0}, 'research_questions':{'q':0}})
    merged = merge_analysis_page(base, page, page_id='page')
    assert merged.claims == [] and merged.counterarguments == []
    assert merged.research_questions == [] and merged.evidence_needs == []
    assert merged.absence_observations == []
    assert merged.structured_output_ids['pages']['page']['claims'] == {}
    assert merged.structured_output_status == 'partial'


def test_repair_patch_rejects_unvalidated_or_extra_slots():
    import pytest
    from app.research_page_merge import merge_repair_patch
    base = AnalysisResult([], [], [], [], None, structured_output_status='complete')
    page = AnalysisResult([{'text': 'verified', 'citations': []}], [], [], [], None,
                          structured_output_status='complete', structured_output_ids={'claims': {'slot-1': 0}})
    merged = merge_repair_patch(base, page, page_id='repair',
                                targets=[{'collection': 'claims', 'slot_id': 'slot-1'}], dependencies=[])
    assert len(merged.claims) == 1
    with pytest.raises(ValueError):
        merge_repair_patch(base, page, page_id='repair',
                           targets=[{'collection': 'claims', 'slot_id': 'slot-2'}], dependencies=[])
    with pytest.raises(ValueError):
        merge_repair_patch(base, page, page_id='repair', targets=[],
                           dependencies=[{'collection': 'claims', 'slot_id': 'slot-1'}])


def test_repair_patch_reuses_readonly_claim_without_duplicate_or_rebinding():
    import hashlib
    import json
    import pytest
    from app.research_page_merge import merge_repair_patch
    claim = {'text': 'verified', 'citations': [{'source_id': 's'}]}
    digest = hashlib.sha256(json.dumps(claim, ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':')).encode()).hexdigest()
    base = AnalysisResult([claim], [], [], ['s'], None, structured_output_status='partial')
    page = AnalysisResult([claim], [{'text': 'counter', 'citations': [],
        'suggested_responds_to_claim_indexes': [0]}], [], ['s'], None,
        structured_output_status='complete', structured_output_ids={
            'claims': {'claim-slot': 0}, 'counterarguments': {'counter-slot': 0}})
    dependency = {'collection': 'claims', 'slot_id': 'claim-slot', 'retained_ref': 0, 'digest': digest}
    target = {'collection': 'counterarguments', 'slot_id': 'counter-slot'}
    merged = merge_repair_patch(base, page, page_id='repair', targets=[target], dependencies=[dependency])
    assert merged.claims == [claim]
    assert merged.counterarguments[0]['suggested_responds_to_claim_indexes'] == [0]
    with pytest.raises(ValueError):
        merge_repair_patch(base, page, page_id='repair', targets=[target],
                           dependencies=[{**dependency, 'digest': '0' * 64}])


def test_repair_patch_maps_evidence_fact_and_calculation_dependencies():
    import hashlib
    import json
    from app.research_page_merge import merge_repair_patch
    def digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(',', ':')).encode()).hexdigest()
    fact = {'fact_id': 'f1', 'source_id': 's', 'quotation': 'Revenue was 10.', 'value': '10'}
    calc = {'inputs': [{'fact_id': 'f1'}, {'fact_id': 'f1'}], 'result': 0}
    evidence = [{'collection': 'facts', 'fact_id': 'f1'}]
    base = AnalysisResult([], [], [], ['s'], None, facts=[fact], calculations=[calc], structured_output_status='partial')
    page = AnalysisResult([{'text': 'No change.', 'citations': [{'source_id': 's', 'quotation': 'Revenue was 10.'}],
        'assertion_suggestions': [{'calculation_indexes': [0]}]}], [], [], ['s'], None,
        facts=[fact], calculations=[calc], structured_output_status='complete', structured_output_ids={
            'facts': {'fact-slot': 'f1'}, 'calculations': {'calc-slot': 0}, 'claims': {'claim-slot': 0},
            'evidence': {'evidence-slot': evidence}})
    dependencies = [{'collection': 'facts', 'slot_id': 'fact-slot', 'retained_ref': 'f1', 'digest': digest(fact)},
                    {'collection': 'calculations', 'slot_id': 'calc-slot', 'retained_ref': 0, 'digest': digest(calc)},
                    {'collection': 'evidence', 'slot_id': 'evidence-slot', 'retained_ref': evidence, 'digest': digest(evidence)}]
    merged = merge_repair_patch(base, page, page_id='repair',
        targets=[{'collection': 'claims', 'slot_id': 'claim-slot'}], dependencies=dependencies)
    assert merged.facts == [fact] and merged.calculations == [calc]
    assert merged.claims[0]['assertion_suggestions'][0]['calculation_indexes'] == [0]
    assert merged.structured_output_ids['pages']['repair']['evidence']['evidence-slot'] == evidence


def test_later_page_repair_dependencies_rebind_to_merged_indexes():
    import hashlib
    import json
    from app.research_page_merge import rebind_repair_continuation
    first = AnalysisResult([{'text': 'first', 'citations': []}], [], [], [], None, structured_output_status='complete')
    page = AnalysisResult([{'text': 'second', 'citations': []}], [], [], [], None,
                          structured_output_status='partial', structured_output_ids={'claims': {'original-id': 0}})
    merged = merge_analysis_page(first, page, page_id='page-2')
    state = {'pending': [{'dependencies': [{'collection': 'claims', 'slot_id': 'claims-0',
                                            'retained_ref': 0, 'digest': 'old'}]}]}
    rebound = rebind_repair_continuation(state, page, merged, page_id='page-2')
    dep = rebound['pending'][0]['dependencies'][0]
    assert dep['retained_ref'] == 1
    assert dep['digest'] == hashlib.sha256(json.dumps(merged.claims[1], ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert state['pending'][0]['dependencies'][0]['retained_ref'] == 0
