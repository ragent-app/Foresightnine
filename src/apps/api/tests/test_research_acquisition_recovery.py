from dataclasses import asdict
from types import SimpleNamespace
import pytest

from app.research_analysis import AnalysisResult
from app.research_recovery import AnalysisSnapshot, CoordinatorState, RecoveryBudget, RecoverySnapshot, StageState, _validate_acquisition_snapshot


def fixture(action=None, *, due=True):
    # The staged pipeline's flags were removed from the public request; the legacy
    # validator below still reads them from a request-shaped object.
    request=SimpleNamespace(parent_version_id='v',plan_research=True,discover_sources=True,auto_fetch_official=True,
                            gap_followup=True,support_review=True,selected_source_ids=None,
                            model_dump=lambda mode='python':{'parent_version_id':'v','plan_research':True,'discover_sources':True,
                                'auto_fetch_official':True,'gap_followup':True,'support_review':True})
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
