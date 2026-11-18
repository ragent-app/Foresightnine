"""Requirement-specific counterpoint gating, using no provider calls."""
from threading import Event
from types import SimpleNamespace

from app.research_analysis import AnalysisResult
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext
from app.research_counterpoint_stage import execute_counterpoint
from app.research_runs import ResearchRunRequest


def test_counterpoint_for_one_requirement_does_not_satisfy_another():
    claims=[{'text':'First finding','role':'support'}, {'text':'Second finding','role':'support'},
            {'text':'First counterpoint','role':'counterargument','citations':[{'source_id':'s'}],
             'suggested_responds_to_claim_indexes':[0]}]
    analysis=AnalysisResult(claims=claims[:2],counterarguments=[claims[2]],limitations=[],source_ids=['s'],total_tokens=10,
        research_questions=[{'question':'First?', 'claim_indexes':[0]}, {'question':'Second?', 'claim_indexes':[1]}])
    context=ResearchExecutionContext(ResearchExecutionBudget(None,None,None,float('inf')),False,lambda:None,
        Event,lambda *_:None,lambda *_:None,lambda:None,calls=1)
    options=dict(request=ResearchRunRequest(parent_version_id='v',counterpoint_followup=True),analysis=analysis,
        claims=claims,leads=[],question='Q',scope='',sources=[],as_of=None,focus=None,planning_steps=[],
        base_prompt='{}',max_prompt_chars=1000,provider=SimpleNamespace(policy=None),generate=None,context=context,
        call_id='counter',checkpoint=lambda *_:None)
    assert execute_counterpoint(target_question='First?',**options).status=='not_needed'
    # Missing provider makes a new call unavailable; it must not be mislabeled
    # not_needed due to the other question's counterpoint.
    assert execute_counterpoint(target_question='Second?',**options).status=='unavailable'
    assert execute_counterpoint(target_question=None,**options).status=='not_needed'
