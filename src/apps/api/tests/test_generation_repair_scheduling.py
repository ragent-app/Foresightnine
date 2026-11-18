"""Repair continuation remains an independently admitted analysis stage."""
from types import SimpleNamespace
from app.research_orchestrator import CoordinationObservation, ResearchOrchestrator


def test_pending_analysis_precedes_acquisition_and_followups():
    observation = CoordinationObservation(source_count=1, candidate_count=0,
        has_analysis=True, claim_count=1, has_counterpoint=False, has_gaps=True,
        generation_failed=False, analysis_pending=True)
    orchestrator = ResearchOrchestrator(context=SimpleNamespace(), workflow=SimpleNamespace(),
        request=SimpleNamespace(), question=True, observe=lambda: observation, handlers={},
        attempted={'planning', 'analysis'}, acquisition_enabled=True, acquisition_due=True)
    assert orchestrator._choose(observation)[0] == 'analysis'
