"""Research-stage artifact assembly and evidence review preparation.

This boundary operates only on already authorized source snapshots. It makes
no external calls and does not turn model interpretations into verified facts.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from .research_analysis import AnalysisResult
from .research_calculations import suggest_percentage_calculations
from .research_evidence import (
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
from .research_sources import ResearchSource
from .research_structured_data import extract_structured_observations


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
