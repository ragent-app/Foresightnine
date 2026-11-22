"""Assess research delivery using the shared evidence and run state.

This retains the current quality decisions behind one boundary; the existing
heuristics remain candidates for consolidation into structured assessments.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from .research_analysis import AnalysisResult
from .research_calculations import (calculation_input_status, calculation_result_status,
                                    claim_calculation_differences)
from .research_evidence import claim_amount_differences, evidence_differences
from .research_quality import CLAIM_EVIDENCE_WARNINGS, incomplete_requested_stages, model_review_outcomes
from .research_coverage import derive_absence_assessment, derive_evidence_needs, derive_scope_assessment, has_unresolved_task_gap
from .research_runs import ResearchRunRequest


BLOCKING_CLAIM_EVIDENCE_WARNINGS: tuple[str, ...] = (
    'percentage_unit_unverified',
    'uncited_exact_dates',
    'publication_date_warnings',
    'rate_unit_warnings',
    'uncited_amounts',
    'uncited_claim_amounts',
    'uncited_dollar_amounts',
    'uncited_quarters',
    'balance_direction_warnings',
    'uncited_percentages',
    'uncited_percentage_bounds',
    'reused_percentages',
    'uncited_basis_points',
    'uncited_percentage_points',
    'uncited_fact_values',
)
ADVISORY_CLAIM_EVIDENCE_WARNINGS: tuple[str, ...] = (
    'counterpoint_relevance_unverified',
    'prior_findings_scope_unverified',
    'source_scope_absence_unverified',
    'causal_attribution_unverified',
    'citation_context_warnings',
)
assert set(BLOCKING_CLAIM_EVIDENCE_WARNINGS).isdisjoint(ADVISORY_CLAIM_EVIDENCE_WARNINGS)
assert set(BLOCKING_CLAIM_EVIDENCE_WARNINGS) | set(ADVISORY_CLAIM_EVIDENCE_WARNINGS) == set(CLAIM_EVIDENCE_WARNINGS)


def _collect_claim_warnings(claims: list[dict], warning_names: tuple[str, ...], *, severity: str) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for name in warning_names:
        indexes = [i for i, claim in enumerate(claims) if isinstance(claim, dict) and claim.get(name)]
        if indexes:
            collected.append({
                'warning': name,
                'severity': severity,
                'claim_indexes': indexes,
            })
    return collected


@dataclass
class ResearchAssessment:
    complete: bool
    limitations: list[str]
    differences: list[dict]
    calculation_differences: list[dict]
    amount_differences: list[dict]
    blocking_warnings: list[dict[str, Any]] = field(default_factory=list)
    advisory_warnings: list[dict[str, Any]] = field(default_factory=list)
    review_queue: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def assess_research_outcome(*, request: ResearchRunRequest, analysis: AnalysisResult,
                            claims: list[dict], source_states: list[dict],
                            discovery_notes: list[str], review_status: str,
                            counterpoint_status: str, gap_status: str, gap_search_status: str, planning_status: str,
                            deadline: float, usage: int | None) -> ResearchAssessment:
    """Evaluate the assembled result without making external calls."""
    require_counterarguments = bool(getattr(request, 'require_counterarguments', False))
    plan_research = bool(getattr(request, 'plan_research', False))
    counterpoint_followup = bool(getattr(request, 'counterpoint_followup', False))
    gap_search = bool(getattr(request, 'gap_search', False))
    gap_followup = bool(getattr(request, 'gap_followup', False))
    support_review = bool(getattr(request, 'support_review', False))
    max_total_tokens = getattr(request, 'max_total_tokens', None)

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
        c.get('role') != 'counterargument' or not require_counterarguments or
        (isinstance(c.get('support_review'), dict) and c['support_review'].get('relevance') == 'relevant') for c in claims))
    incomplete_stages = incomplete_requested_stages({
        'planning_requested': plan_research, 'planning_status': planning_status,
        'counterpoint_requested': counterpoint_followup, 'counterpoint_status': counterpoint_status,
        'gap_search_requested': gap_search, 'gap_search_status': gap_search_status,
        'gap_followup_requested': gap_followup, 'gap_followup_status': gap_status,
        'support_review_requested': support_review, 'support_review_status': review_status,
    })
    scope_run = {'operation_id': 'current', 'source_processing': source_states,
                 'research_questions': analysis.research_questions,
                 'model_evidence_needs': analysis.evidence_needs,
                 'model_absence_observations': analysis.absence_observations}
    scope = derive_scope_assessment(run=scope_run, claims=claims, limitations=analysis.limitations)
    needs = derive_evidence_needs(run=scope_run, scope=scope)
    absences = derive_absence_assessment(run=scope_run, scope=scope)
    unresolved_task_gap = has_unresolved_task_gap(needs=needs, absences=absences, claims=claims)
    blocking_warnings = _collect_claim_warnings(claims, BLOCKING_CLAIM_EVIDENCE_WARNINGS, severity='blocking')
    advisory_warnings = _collect_claim_warnings(claims, ADVISORY_CLAIM_EVIDENCE_WARNINGS, severity='advisory')
    review_queue = [*blocking_warnings, *advisory_warnings]
    metadata = {
        'blocking_warnings': blocking_warnings,
        'advisory_warnings': advisory_warnings,
        'blocking_warning_names': [item['warning'] for item in blocking_warnings],
        'advisory_warning_names': [item['warning'] for item in advisory_warnings],
        'review_queue': review_queue,
    }
    complete = bool(analysis.structured_output_status == 'complete' and analysis.claims and (analysis.counterarguments or not require_counterarguments) and all(c["citations"] and c.get("support_status") != "incomplete" for c in claims)
                    and not processing_incomplete and not unresolved_task_gap and not missing_fact_dimensions and not unverified_calculations and not unverified_calculation_results and not differences and not calculation_differences and not amount_differences and
                    not blocking_warnings and
                    (not support_review or review_supports_all) and
                    not incomplete_stages and
                    not any(not item.get('claim_indexes') for item in analysis.research_questions))
    limitations = [*discovery_notes, *analysis.limitations]
    if analysis.structured_output_status == 'failed':
        limitations.append('The structured model response did not pass validation; no valid research result was produced.')
    elif analysis.structured_output_status == 'not_validated':
        limitations.append('No complete model output was available for structured validation.')
    elif analysis.structured_output_status == 'partial':
        limitations.append('Some structured model output was excluded after validation; retained findings require review.')
        if not (analysis.claims or analysis.counterarguments or analysis.facts or analysis.calculations):
            limitations.append('No valid research results survived structured output validation.')
    if unresolved_task_gap:
        limitations.append('A recorded task-related evidence need or absence observation is not established; review it and obtain relevant evidence.')
    if plan_research:
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
    if support_review:
        limitations.append("A second model assessment is not independent proof that evidence supports a claim.")
        if any(review_outcomes[name] for name in ('does_not_support', 'contradicts', 'uncertain', 'not_reviewed')):
            limitations.append('Some findings were not supported, were contradicted, remained uncertain or were not reviewed in the model assessment; inspect each recorded opinion and its evidence.')
        if require_counterarguments and any(
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
    if max_total_tokens is not None and usage is not None and usage > max_total_tokens:
        complete = False
        limitations.append("Provider-reported token use exceeded the requested budget; the result is partial.")
    if not complete:
        limitations.append("The research is partial: review the recorded findings, requested steps and outstanding limitations.")
    return ResearchAssessment(
        complete,
        limitations,
        differences,
        calculation_differences,
        amount_differences,
        blocking_warnings=blocking_warnings,
        advisory_warnings=advisory_warnings,
        review_queue=review_queue,
        metadata=metadata,
    )
