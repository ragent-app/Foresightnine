"""Assess research delivery using the shared evidence and run state.

This retains the current quality decisions behind one boundary; the existing
heuristics remain candidates for consolidation into structured assessments.
"""
from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

from .research_analysis import AnalysisResult
from .research_calculations import (calculation_input_status, calculation_result_status,
                                    claim_calculation_differences)
from .research_evidence import claim_amount_differences, evidence_differences
from .research_quality import CLAIM_EVIDENCE_WARNINGS, incomplete_requested_stages, model_review_outcomes
from .research_coverage import derive_absence_assessment, derive_evidence_needs, derive_scope_assessment, has_unresolved_task_gap
from .research_runs import ResearchRunRequest


@dataclass
class ResearchAssessment:
    complete: bool
    limitations: list[str]
    differences: list[dict]
    calculation_differences: list[dict]
    amount_differences: list[dict]


def assess_research_outcome(*, request: ResearchRunRequest, analysis: AnalysisResult,
                            claims: list[dict], source_states: list[dict],
                            discovery_notes: list[str], review_status: str,
                            counterpoint_status: str, gap_status: str, gap_search_status: str, planning_status: str,
                            deadline: float, usage: int | None) -> ResearchAssessment:
    """Evaluate the assembled result without making external calls."""
    differences = evidence_differences(analysis.facts)
    calculation_differences = claim_calculation_differences(claims, analysis.calculations, analysis.facts)
    amount_differences = claim_amount_differences(claims, analysis.facts)
    unverified_calculations = any(calculation_input_status(calculation, analysis.facts) != "matched"
                                  for calculation in analysis.calculations)
    unverified_calculation_results = any(calculation_result_status(calculation) != "matched"
                                         for calculation in analysis.calculations)
    calculation_fact_ids = {item.get('fact_id') for calculation in analysis.calculations
                            if isinstance(calculation, dict) for item in calculation.get('inputs', [])
                            if isinstance(item, dict) and isinstance(item.get('fact_id'), str)}
    missing_fact_dimensions = any(isinstance(fact, dict) and fact.get('fact_id') in calculation_fact_ids and any(
        not isinstance(fact.get(field), str) or not fact[field].strip()
        for field in ('unit', 'period')) for fact in analysis.facts)
    processing_incomplete = any(marker in note for note in analysis.limitations for marker in (
        "annotations were omitted", "A model-generated quotation", "A model-generated claim",
        "A requested calculation was omitted", "Period comparability is not established",
        "quotation location is ambiguous",
        "currency identifier is absent from its quotation", "unit scale is absent from its quotation",
        "percentage fact included a model-declared currency", "excluded from automatic calculations"))
    # Selection may omit material unrelated to this question. Its relevance is
    # unknown; it cannot by itself prove task failure or task completion.
    review_outcomes = model_review_outcomes(claims)
    review_supports_all = (review_status == "complete" and not any(
        review_outcomes[name] for name in ('does_not_support', 'contradicts', 'uncertain', 'not_reviewed')) and all(
        c.get('role') != 'counterargument' or not request.require_counterarguments or
        (isinstance(c.get('support_review'), dict) and c['support_review'].get('relevance') == 'relevant') for c in claims))
    incomplete_stages = incomplete_requested_stages({
        'planning_requested': request.plan_research, 'planning_status': planning_status,
        'counterpoint_requested': request.counterpoint_followup, 'counterpoint_status': counterpoint_status,
        'gap_search_requested': request.gap_search, 'gap_search_status': gap_search_status,
        'gap_followup_requested': request.gap_followup, 'gap_followup_status': gap_status,
        'support_review_requested': request.support_review, 'support_review_status': review_status,
    })
    scope_run = {'operation_id': 'current', 'source_processing': source_states,
                 'research_questions': analysis.research_questions,
                 'model_evidence_needs': analysis.evidence_needs,
                 'model_absence_observations': analysis.absence_observations}
    scope = derive_scope_assessment(run=scope_run, claims=claims, limitations=analysis.limitations)
    needs = derive_evidence_needs(run=scope_run, scope=scope)
    absences = derive_absence_assessment(run=scope_run, scope=scope)
    unresolved_task_gap = has_unresolved_task_gap(needs=needs, absences=absences, claims=claims)
    complete = bool(analysis.claims and (analysis.counterarguments or not request.require_counterarguments) and all(c["citations"] and c.get("support_status") != "incomplete" for c in claims)
                    and not processing_incomplete and not unresolved_task_gap and not missing_fact_dimensions and not unverified_calculations and not unverified_calculation_results and not differences and not calculation_differences and not amount_differences and
                    not any(c.get(warning) for c in claims for warning in CLAIM_EVIDENCE_WARNINGS) and
                    (not request.support_review or review_supports_all) and
                    not incomplete_stages and
                    not any(not item.get('claim_indexes') for item in analysis.research_questions))
    limitations = [*discovery_notes, *analysis.limitations]
    if unresolved_task_gap:
        limitations.append('A recorded task-related evidence need or absence observation is not established; review it and obtain relevant evidence.')
    if request.plan_research:
        limitations.append('The model-generated research plan is an unverified guide and may omit relevant questions.')
        if planning_status != 'completed':
            limitations.append('The requested research plan did not complete; the result used the original question.')
    if any(not item.get('claim_indexes') for item in analysis.research_questions):
        limitations.append('A model-suggested research question has no linked finding; its coverage remains unestablished.')
    if 'gap_search' in incomplete_stages:
        limitations.append('The requested gap-focused source search did not complete; existing findings remain available.')
    if 'gap_followup' in incomplete_stages:
        limitations.append('The requested evidence-gap follow-up did not complete; existing findings and gaps remain available.')
    if 'counterpoint' in incomplete_stages:
        limitations.append('The requested counterpoint follow-up did not complete; existing findings and gaps remain available.')
    if any(c.get('counterpoint_relevance_unverified') for c in claims):
        limitations.append('A counterpoint explicitly agrees with prior findings; review whether it provides a relevant opposing argument or limitation.')
    if any(c.get('prior_findings_scope_unverified') for c in claims):
        limitations.append('A counterpoint claims prior findings omit information, but the model received only part of those findings; review the full prior result.')
    if any(c.get('percentage_unit_unverified') for c in claims):
        limitations.append('A percentage matches a quoted number without an explicit percent unit; check the original unit and table context.')
    if any(c.get("source_scope_absence_unverified") for c in claims):
        limitations.append("A claim says a source lacks information, but only excerpts were analyzed; the full-source absence is not established.")
    if unverified_calculations:
        limitations.append("A calculation has inputs that cannot be verified against the saved facts; the result is provisional.")
    if missing_fact_dimensions:
        limitations.append("A recorded numeric fact has no unit or period; its use and comparisons require review.")
    if unverified_calculation_results:
        limitations.append("A calculation result could not be reproduced from its saved formula and inputs.")
    if not analysis.counterarguments:
        limitations.append("No counterarguments were provided; this does not establish that none exist.")
    if request.support_review:
        limitations.append("A second model assessment is not independent proof that evidence supports a claim.")
        if any(review_outcomes[name] for name in ('does_not_support', 'contradicts', 'uncertain', 'not_reviewed')):
            limitations.append('Some findings were not supported, were contradicted, remained uncertain or were not reviewed in the model assessment; inspect each recorded opinion and its evidence.')
        if request.require_counterarguments and any(
                c.get('role') == 'counterargument' and c.get('support_review', {}).get('relevance') != 'relevant' for c in claims):
            limitations.append('Counterpoint relevance was not supported by a complete model review; the relationship remains unverified.')
        if review_status != "complete":
            limitations.append("The requested model support review did not cover all claims.")
    if differences:
        limitations.append("Cross-source numeric differences require entity and accounting-basis review; they are not confirmed conflicts.")
    if calculation_differences:
        limitations.append("A claim percentage may differ from a deterministic calculation; review the wording and calculation.")
    if amount_differences:
        limitations.append("A claim amount may not match the cited numeric facts after unit conversion; review the source and wording.")
        if len(amount_differences) == 50:
            limitations.append("At most 50 amount review candidates are shown; additional candidates may exist.")
    if any(c.get("uncited_exact_dates") for c in claims):
        limitations.append("An exact date in a claim is absent from its located quotations; verify the date and its meaning.")
    if any(c.get("publication_date_warnings") for c in claims):
        limitations.append("A claimed publication date may differ from the source's declared date; review both.")
    if any(c.get("rate_unit_warnings") for c in claims):
        limitations.append("An interest rate level may be expressed as percentage points; review the unit and context.")
    if any(c.get("uncited_amounts") for c in claims):
        limitations.append("A matched amount fact passage is not fully covered by attached quotations; check evidence and context.")
    if any(c.get("uncited_claim_amounts") for c in claims):
        limitations.append("An explicit USD amount could not be confirmed from the attached quotations; check currency, units, source and any calculation.")
    if any(c.get("uncited_dollar_amounts") for c in claims):
        limitations.append("A dollar-symbol amount could not be matched in the attached quotations; check currency, units, source and any calculation.")
    if any(c.get("uncited_quarters") for c in claims):
        limitations.append("An explicit quarter and year could not be matched in the attached quotations; check the period and source context.")
    if any(c.get("balance_direction_warnings") for c in claims):
        limitations.append("A claimed direction or contradiction may require review; check revision versus change, definitions, signs and periods.")
    if any(c.get("uncited_fact_values") for c in claims):
        limitations.append("A cited source contains a matching fact outside the attached quotations; review the candidate and its meaning.")
    if any(c.get("uncited_percentage_bounds") for c in claims):
        limitations.append("A strict percentage upper bound is not present in the attached quotations; do not treat its threshold as an exact value.")
    if any(c.get("reused_percentages") for c in claims):
        limitations.append("A percentage repeats within a claim; check that each measure has its own supporting evidence.")
    if any(c.get("uncited_percentages") for c in claims):
        limitations.append("A claim percentage is absent from its quotations. It may be calculated; check the calculation and source before treating the claim as supported.")
    if any(c.get("uncited_basis_points") for c in claims):
        limitations.append("A claim's basis-point figure is absent from its quotations; check the source and any calculation.")
    if any(c.get("uncited_percentage_points") for c in claims):
        limitations.append("A claim percentage-point figure is absent from its quotations; check the source and any calculation.")
    if any(c.get("citation_context_warnings") for c in claims):
        limitations.append("A quotation may omit relevant context; review the source and claim.")
    if any(c.get("causal_attribution_unverified") for c in claims):
        limitations.append("A causal explanation has no corresponding causal wording in its located quotations; check the original evidence.")
    if len(differences) == 50:
        limitations.append("At most 50 candidate differences are shown.")
    if monotonic() > deadline:
        complete = False
        limitations.append("Research exceeded its time budget; the retained result is partial.")
    if request.max_total_tokens is not None and usage is not None and usage > request.max_total_tokens:
        complete = False
        limitations.append("Provider-reported token use exceeded the requested budget; the result is partial.")
    if not complete:
        limitations.append("The research is partial: review the recorded findings, requested steps and outstanding limitations.")
    return ResearchAssessment(complete, limitations, differences, calculation_differences, amount_differences)
