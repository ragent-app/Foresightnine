from dataclasses import asdict
import pytest

from app.research_analysis import AnalysisResult
from app.research_recovery import AnalysisSnapshot, CoordinatorState, RecoveryBudget, RecoverySnapshot, StageState, _validate_acquisition_snapshot
from app.research_runs import ResearchRunRequest


def fixture(action=None, *, due=True):
    request=ResearchRunRequest(parent_version_id='v',plan_research=True,discover_sources=True,
                               auto_fetch_official=True,gap_followup=True,support_review=True)
    analysis=AnalysisSnapshot.model_validate(asdict(AnalysisResult([],[],[],[],None)))
    attempted=['planning'] if due else ['planning','acquisition_decision']
    if action is not None:
        action = {**action, 'requirement_id':'r'}
    records=[] if due else [{'action':action['action'],'requirement_id':'r','result_status':'selected'}]
    coordinator=CoordinatorState(attempted=attempted,outcomes={stage:'completed' for stage in attempted},omissions={},actions_used=len(attempted),
                                 acquisition_due=due,acquisition_current=action,acquisition_records=records)
    state=StageState(request=request.model_dump(mode='json'),planning_result={'status':'completed'},max_prompt_chars=0,
        generation_status='not_started',question_origin='user',effective_question='Q',discovered=[],search_receipts=[],
        discovery_notes=[],search_status='not_started',official_fetch_status='not_started',official_fetch_calls=0,workflow_receipts=[])
    snapshot=RecoverySnapshot(schema_version='4',checkpoint_version_id='v',next_stage='acquisition_decision' if due else 'analysis',
        completed_stages=attempted.copy(),analysis=analysis.model_dump(),claims=[],planning_steps=[],source_ids=['s'],
        source_fingerprints=[{'source_id':'s'}],budget=RecoveryBudget(model_calls=1,search_calls=0,accounted_tokens=10),plan={})
    return snapshot,state,coordinator,request,analysis


def check(values, has_analysis=False):
    snapshot,state,coordinator,request,analysis=values
    _validate_acquisition_snapshot(snapshot=snapshot,state=state,coordinator=coordinator,request=request,
                                   analysis_data=analysis,has_analysis=has_analysis)


def test_schema4_preanalysis_is_empty_and_stage_is_explicit():
    values=fixture()
    check(values)
    values[0].completed_stages.append('analysis')
    with pytest.raises(ValueError): check(values)
    values=fixture()
    values[1].base_prompt_text='fabricated'
    with pytest.raises(ValueError): check(values)


def test_schema4_read_is_bound_to_source_and_permissions():
    values=fixture({'action':'read','source_id':'s','character_start':0,'character_end':10},due=False)
    check(values)
    values[2].acquisition_current['source_id']='other'
    with pytest.raises(ValueError): check(values)


def test_schema4_unauthorized_retrieval_and_unselected_decision_fail_closed():
    values=fixture({'action':'retrieve','candidate_ids':['lead:0']},due=False)
    values[0].next_stage='source_retrieval'
    values[1].discovered=[{'title':'Lead'}]
    check(values)
    values[3].selected_source_ids=[]
    with pytest.raises(ValueError): check(values)
    values=fixture({'action':'retrieve','candidate_ids':['lead:2']},due=False)
    values[0].next_stage='source_retrieval'
    with pytest.raises(ValueError): check(values)


def test_schema4_next_page_and_delivery_use_validated_saved_decision():
    values=fixture({'action':'next_page'},due=False)
    values[0].next_stage='acquisition_decision'
    check(values)
    values[2].acquisition_records[-1]['action']='discover'
    with pytest.raises(ValueError): check(values)
    values=fixture({'action':'deliver'},due=False)
    values[0].next_stage='delivery_assessment'
    values[2].acquisition_records[-1]['result_status']='stopped'
    with pytest.raises(ValueError): check(values)


def test_restoration_strategy_is_explicit_for_new_and_legacy_snapshots():
    from hashlib import sha256
    from types import SimpleNamespace
    from app.research_recovery import restore_stage_state
    from app.operations import ApiProblem
    snapshot,state,coordinator,request,analysis=fixture()
    state.coordinator=coordinator.model_dump()
    snapshot.plan=state.model_dump()
    inputs={'budget':request.model_dump(mode='json'), 'question_sha256':sha256(b'Q').hexdigest(),
            'scope_sha256':None,'as_of':None}
    topic=SimpleNamespace(request=SimpleNamespace(question='Q',scope='',as_of=None))
    restored=restore_stage_state(snapshot=snapshot,inputs=inputs,topic=topic)
    assert restored.request.evidence_strategy=='delegated' and restored.state.generation_status=='not_started'
    bad=snapshot.model_copy(deep=True)
    del bad.plan['request']['evidence_strategy']
    with pytest.raises(ApiProblem): restore_stage_state(snapshot=bad,inputs=inputs,topic=topic)
    legacy=snapshot.model_copy(deep=True)
    legacy.schema_version='3'
    legacy.completed_stages=['planning','source_discovery','source_retrieval','analysis']
    legacy.next_stage='delivery_assessment'
    legacy.plan['generation_status']='completed'
    legacy.plan['base_prompt_text']='{}'
    legacy.plan['coordinator']={'attempted':legacy.completed_stages, 'outcomes':{},'omissions':{},'actions_used':4}
    legacy.plan['request'].pop('evidence_strategy')
    old_inputs={**inputs,'budget':{k:v for k,v in inputs['budget'].items() if k!='evidence_strategy'}}
    restored=restore_stage_state(snapshot=legacy,inputs=old_inputs,topic=topic)
    assert restored.request.evidence_strategy=='coordinator'
    legacy.plan['request']['evidence_strategy']='delegated'
    with pytest.raises(ApiProblem): restore_stage_state(snapshot=legacy,inputs=old_inputs,topic=topic)
