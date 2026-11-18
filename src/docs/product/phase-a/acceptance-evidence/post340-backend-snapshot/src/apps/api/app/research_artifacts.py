"""Research-stage artifact assembly and evidence review preparation.

This boundary operates only on already authorized source snapshots. It makes
no external calls and does not turn model interpretations into verified facts.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from typing import Any

from .research_analysis import AnalysisResult
from .research_calculations import claim_calculation_differences, suggest_percentage_calculations
from .research_evidence import (
    claim_amount_differences, evidence_differences,
    flag_citation_subject_context, flag_component_direction, flag_counterpoint_agreement,
    flag_partial_source_absence, flag_publication_date_mismatches, flag_rate_level_point_warnings,
    flag_truncated_previous_findings, flag_uncited_basis_points, flag_uncited_causal_attribution,
    flag_uncited_dollar_amounts, flag_uncited_exact_dates, flag_uncited_percentage_points,
    flag_uncited_quarters, flag_uncited_usd_amounts, suggest_cross_source_comparison_evidence,
    suggest_excerpt_dates, suggest_percentage_evidence, suggest_related_with_supplemental_leads,
    suggest_uncited_numeric_facts, suggest_usd_amount_evidence,
)
from .research_gap_followup import merge_research_questions
from .research_source_policy import prepare_sources
from .research_coverage import safe_model_evidence_needs
from .research_sources import ResearchSource
from .research_structured_data import extract_structured_observations
from .research_topics import ResearchContent
from .research_coordinator import PlanningResult
from .research_planning import PLAN_SYSTEM_PROMPT
from .research_runs import ResearchRunRequest


def build_run_core(*, question_origin: str, default_task: str | None, question_override: str | None,
                   analysis: AnalysisResult, source_processing: list[dict], planning_requested: bool,
                   planning: PlanningResult, request: ResearchRunRequest, budget_accounted_tokens: int,
                   search_receipts: list[dict], workflow_receipts: list[dict], generation_status: str) -> dict[str, Any]:
    """Common stage metadata, without changing the legacy run record format."""
    return {'question_origin': question_origin,
            **({'default_task': default_task} if default_task is not None else {}),
            **({'question_override': question_override} if question_override is not None else {}),
            'research_questions': analysis.research_questions,
            'planning_requirements': list(getattr(planning, 'requirements', ())),
            'analysis_requirements': list(getattr(analysis, 'discovered_requirements', [])),
            'selection_diagnostics': list(getattr(analysis, 'selection_diagnostics', [])),
            'model_requirement_links': list(getattr(analysis, 'requirement_links', [])),
            'model_evidence_needs': safe_model_evidence_needs(getattr(analysis, 'evidence_needs', [])),
            'model_absence_observations': list(getattr(analysis, 'absence_observations', [])),
            'source_processing': source_processing,
            'available_fact_ids': analysis.available_fact_ids,
            'settled_model_budget_accounted_tokens': budget_accounted_tokens,
            'source_search_receipts': [dict(item) for item in search_receipts],
            'workflow_receipts': [dict(item) for item in workflow_receipts],
            'generation_status': generation_status,
            'planning_requested': planning_requested, 'planning_status': planning.status,
            'planning_steps': list(planning.steps), 'planning_tokens': planning.tokens,
            'planning_prompt_sha256': planning.prompt_sha256, 'planning_error': planning.error,
            'planning_system_sha256': sha256(PLAN_SYSTEM_PROMPT.encode('utf-8')).hexdigest() if planning_requested else None,
            # Checkpoints may precede optional stages. Record the full request
            # even there; the caller overrides these defaults as stages run.
            'counterpoint_requested': request.counterpoint_followup,
            'counterpoint_status': 'not_started' if request.counterpoint_followup else 'not_requested',
            'gap_search_requested': request.gap_search,
            'gap_search_status': 'not_started' if request.gap_search else 'not_requested',
            'gap_followup_requested': request.gap_followup,
            'gap_followup_status': 'not_started' if request.gap_followup else 'not_requested',
            'support_review_requested': request.support_review,
            'support_review_status': 'not_started' if request.support_review else 'not_requested',
            'require_counterarguments': request.require_counterarguments}


def assemble_research_content(*, source_candidates: list[dict], sources: list[dict], analysis: AnalysisResult,
                              claims: list[dict], limitations: list[str], run: dict,
                              summary: str = '', differences: list[dict] | None = None,
                              calculation_differences: list[dict] | None = None,
                              amount_differences: list[dict] | None = None) -> ResearchContent:
    """Build one consistent artifact snapshot for checkpoints and delivery."""
    return ResearchContent(summary=summary, source_candidates=source_candidates, sources=sources,
        facts=analysis.facts, calculations=analysis.calculations, claims=claims, limitations=limitations, run=run,
        evidence_differences=evidence_differences(analysis.facts) if differences is None else differences,
        calculation_differences=(claim_calculation_differences(claims, analysis.calculations, analysis.facts)
                                 if calculation_differences is None else calculation_differences),
        amount_differences=(claim_amount_differences(claims, analysis.facts)
                            if amount_differences is None else amount_differences))


def assemble_initial_artifacts(analysis: AnalysisResult, sources: list[ResearchSource],
                               as_of: datetime | None, planning_steps: list[str]) -> AnalysisResult:
    eligible, _ = prepare_sources(sources, as_of)
    observed, truncated = extract_structured_observations(
        [source for source in eligible if source.source_id in analysis.source_ids],
        max_facts=max(0, min(200, 2000 - len(analysis.facts))))
    existing_ids = {fact.get('fact_id') for fact in analysis.facts if isinstance(fact, dict)}
    observed = [fact for fact in observed if fact['fact_id'] not in existing_ids]
    if observed:
        note = 'Structured observations were extracted from a stored source snapshot; identity, timing and financial meaning are not independently verified.'
        analysis = replace(analysis, facts=[*analysis.facts, *observed],
                           limitations=list(dict.fromkeys([*analysis.limitations, note])))
    if truncated:
        analysis = replace(analysis, limitations=[*analysis.limitations,
            'Some structured source observations were not processed because a source or run limit was reached.'])
    if planning_steps:
        planned = [{'question': step, 'claim_indexes': []} for step in planning_steps]
        analysis = replace(analysis, research_questions=merge_research_questions(
            planned, analysis.research_questions, {i: i for i in range(len(analysis.claims))}))
    return analysis


def prepare_claim_evidence(claims: list[dict[str, Any]], facts: list[dict[str, Any]],
                           calculations: list[dict[str, Any]], sources: list[ResearchSource],
                           comparison_sources: list[ResearchSource], date_sources: list[ResearchSource] | None,
                           segments: object, supplemental_citation_leads: list[dict[str, Any]], *,
                           check_source_scope: bool) -> list[dict[str, Any]]:
    """Apply shared checks while callers explicitly set their authorized scope."""
    claims = suggest_percentage_evidence(claims, facts)
    claims = flag_uncited_percentage_points(flag_uncited_basis_points(claims))
    claims = suggest_percentage_calculations(claims, calculations, facts)
    claims = flag_uncited_quarters(flag_uncited_dollar_amounts(flag_uncited_usd_amounts(claims, sources), sources), sources)
    claims = suggest_usd_amount_evidence(claims, facts, sources)
    claims = suggest_uncited_numeric_facts(claims, facts, sources)
    claims = suggest_cross_source_comparison_evidence(claims, facts, comparison_sources)
    claims = suggest_related_with_supplemental_leads(claims, sources, supplemental_citation_leads)
    claims = flag_rate_level_point_warnings(flag_publication_date_mismatches(claims, sources))
    claims = flag_component_direction(flag_citation_subject_context(claims, sources), sources)
    claims = flag_uncited_exact_dates(flag_uncited_causal_attribution(claims, sources), sources)
    if date_sources is not None:
        claims = suggest_excerpt_dates(claims, date_sources, segments)
    if check_source_scope:
        claims = flag_partial_source_absence(claims, sources, segments)
    claims = flag_counterpoint_agreement(claims)
    return flag_truncated_previous_findings(claims)


def prepare_run_claims(analysis: AnalysisResult, sources: list[ResearchSource], as_of: datetime | None,
                       supplemental_citation_leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply shared checks after generation or supplementation."""
    claims = [{**item, 'role': 'support'} for item in analysis.claims]
    claims.extend({**item, 'role': 'counterargument'} for item in analysis.counterarguments)
    eligible, _ = prepare_sources(sources, as_of)
    return prepare_claim_evidence(claims, analysis.facts, analysis.calculations, sources,
                                  [source for source in eligible if source.source_id in analysis.source_ids],
                                  eligible, analysis.segments, supplemental_citation_leads,
                                  check_source_scope=True)
