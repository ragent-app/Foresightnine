"""Tests for Phase P3 warning severity grading in research_assessment."""
from __future__ import annotations

from time import monotonic

from app.research_analysis import AnalysisResult
from app.research_assessment import (
    ADVISORY_CLAIM_EVIDENCE_WARNINGS,
    BLOCKING_CLAIM_EVIDENCE_WARNINGS,
    CLAIM_EVIDENCE_WARNINGS,
    ResearchAssessment,
    assess_research_outcome,
)
from app.research_runs import ResearchRunRequest


def _base_claim(**overrides) -> dict:
    return {
        'text': 'Revenue rose 10 percent in Q4 2025.',
        'role': 'support',
        'citations': [{'source_id': 's1', 'quotation': 'Revenue rose 10 percent in Q4 2025.'}],
        'support_status': 'not_independently_verified',
        **overrides,
    }


def _assess(claims: list[dict], *, request=None) -> ResearchAssessment:
    req = request or ResearchRunRequest(parent_version_id='v1')
    analysis = AnalysisResult(claims, [], [], ['s1'], 1, structured_output_status='complete')
    return assess_research_outcome(
        request=req,
        analysis=analysis,
        claims=claims,
        source_states=[{
            'source_id': 's1',
            'eligibility': 'eligible',
            'initial_selection': 'complete',
            'analysis_status': 'produced',
        }],
        discovery_notes=[],
        review_status='not_requested',
        counterpoint_status='not_requested',
        gap_status='not_requested',
        gap_search_status='not_requested',
        planning_status='not_requested',
        deadline=monotonic() + 30.0,
        usage=100,
    )


def test_claim_evidence_warning_partition_is_exact_and_disjoint():
    assert set(BLOCKING_CLAIM_EVIDENCE_WARNINGS).isdisjoint(ADVISORY_CLAIM_EVIDENCE_WARNINGS)
    assert set(BLOCKING_CLAIM_EVIDENCE_WARNINGS) | set(ADVISORY_CLAIM_EVIDENCE_WARNINGS) == set(CLAIM_EVIDENCE_WARNINGS)
    assert 'causal_attribution_unverified' in ADVISORY_CLAIM_EVIDENCE_WARNINGS
    assert 'citation_context_warnings' in ADVISORY_CLAIM_EVIDENCE_WARNINGS
    assert 'balance_direction_warnings' in BLOCKING_CLAIM_EVIDENCE_WARNINGS
    assert 'uncited_quarters' in BLOCKING_CLAIM_EVIDENCE_WARNINGS


def test_advisory_warnings_do_not_block_completion_and_remain_transparent():
    claims = [
        _base_claim(
            causal_attribution_unverified=True,
            citation_context_warnings=[{'source_id': 's1', 'citation_index': 0, 'status': 'requires_review'}],
        ),
    ]
    assessment = _assess(claims)
    assert assessment.complete is True
    assert assessment.blocking_warnings == []
    assert [w['warning'] for w in assessment.advisory_warnings] == [
        'causal_attribution_unverified',
        'citation_context_warnings',
    ]
    assert assessment.metadata['blocking_warnings'] == []
    assert assessment.metadata['advisory_warning_names'] == [
        'causal_attribution_unverified',
        'citation_context_warnings',
    ]
    assert [w['warning'] for w in assessment.review_queue] == [
        'causal_attribution_unverified',
        'citation_context_warnings',
    ]
    assert any('causal explanation' in lim for lim in assessment.limitations)
    assert any('omit relevant context' in lim for lim in assessment.limitations)
    assert not any('The research is partial' in lim for lim in assessment.limitations)


def test_all_advisory_warnings_allow_complete_while_blocking_warnings_block():
    for warning in ADVISORY_CLAIM_EVIDENCE_WARNINGS:
        value = [{'status': 'requires_review'}] if warning.endswith('_warnings') else True
        res = _assess([_base_claim(**{warning: value})])
        assert res.complete is True, warning
        assert res.blocking_warnings == []
        assert [w['warning'] for w in res.advisory_warnings] == [warning]

    for warning in BLOCKING_CLAIM_EVIDENCE_WARNINGS:
        value = [{'status': 'requires_review'}] if warning.endswith('_warnings') else ['item']
        res = _assess([_base_claim(**{warning: value})])
        assert res.complete is False, warning
        assert [w['warning'] for w in res.blocking_warnings] == [warning]
        assert res.metadata['blocking_warning_names'] == [warning]
        assert any('The research is partial' in lim for lim in res.limitations)


def test_mixed_blocking_and_advisory_warnings_expose_both_in_metadata_and_review_queue():
    claims = [
        _base_claim(causal_attribution_unverified=True),
        _base_claim(uncited_quarters=['Q4 2025'], balance_direction_warnings=[{'basis': 'quoted_direction_conflict'}]),
    ]
    assessment = _assess(claims)
    assert assessment.complete is False
    assert assessment.metadata['blocking_warning_names'] == ['uncited_quarters', 'balance_direction_warnings']
    assert assessment.metadata['advisory_warning_names'] == ['causal_attribution_unverified']
    assert assessment.blocking_warnings[0]['claim_indexes'] == [1]
    assert assessment.advisory_warnings[0]['claim_indexes'] == [0]
    assert len(assessment.review_queue) == 3
