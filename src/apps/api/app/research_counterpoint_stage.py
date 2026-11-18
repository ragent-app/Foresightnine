"""Bounded counterpoint stage for an existing authorized research snapshot."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable

from .operations import ApiProblem
from .phase3 import ProviderFailure
from .research_analysis import (SYSTEM_PROMPT, AnalysisInterrupted, AnalysisResult, InvalidAnalysis, analyze)
from .research_artifacts import prepare_run_claims
from .research_calculations import claim_calculation_differences
from .research_coordinator import ResearchExecutionContext, research_prompt_budget, research_prompt_fits, has_usable_research_counterpoint
from .research_counterpoint import extract_answer_candidates, filter_counterpoint_rows, prepare_counterpoint_prompt
from .research_evidence import claim_amount_differences
from .research_runs import ResearchRunRequest, has_usable_counterpoint
from .research_sources import ResearchSource


@dataclass
class CounterpointResult:
    analysis: AnalysisResult
    claims: list[dict[str, Any]]
    leads: list[dict[str, Any]]
    status: str
    tokens: int | None = None
    prompt_sha256: str | None = None
    error: str | None = None
    discarded_fields: int = 0
    omitted_rows: int = 0


def execute_counterpoint(*, request: ResearchRunRequest, analysis: AnalysisResult, claims: list[dict[str, Any]],
                         leads: list[dict[str, Any]], question: str, scope: str, sources: list[ResearchSource],
                         as_of: datetime | None, focus: tuple[str, int, int] | None,
                         planning_steps: list[str], base_prompt: str | None, max_prompt_chars: int,
                         provider: Any, generate: Any, context: ResearchExecutionContext, call_id: str,
                         checkpoint: Callable[[str], None], target_question: str | None = None) -> CounterpointResult:
    """Find a cited opposing candidate without expanding authorized source scope."""
    result = CounterpointResult(analysis, claims, leads, 'not_requested')
    if not request.counterpoint_followup:
        return result
    result.status = 'not_needed' if context.calls and analysis.claims else 'unavailable'
    has_counterpoint = has_usable_research_counterpoint(claims, analysis.facts, analysis.calculations)
    if target_question is not None:
        # Only an explicitly linked counterpoint can satisfy this requirement.
        # Ambiguous question/claim relations stay unresolved, never guessed.
        questions = [row for row in analysis.research_questions if isinstance(row, dict)
                     and row.get('question') == target_question]
        target_indexes = set(questions[0].get('claim_indexes', [])) if len(questions) == 1 else set()
        differences = [*claim_calculation_differences(claims, analysis.calculations, analysis.facts),
                       *claim_amount_differences(claims, analysis.facts)]
        excluded = {item['claim_index'] for item in differences}
        excluded.update(i for i, claim in enumerate(claims) if not target_indexes.intersection(
            claim.get('suggested_responds_to_claim_indexes', []) if isinstance(claim, dict) else []))
        has_counterpoint = bool(target_indexes) and has_usable_counterpoint(claims, excluded)
    if not context.calls or not analysis.claims or has_counterpoint:
        return result
    if len(analysis.counterarguments) >= 20:
        result.status = 'capacity_reached'
        return result
    if base_prompt is None:
        return result
    policy = getattr(provider, 'policy', None)
    input_limit, output_limit = getattr(policy, 'max_input_tokens', None), getattr(policy, 'max_output_tokens', None)
    timeout = getattr(policy, 'timeout_seconds', None)
    if not isinstance(timeout, (int, float)):
        result.status = 'unavailable'
        return result
    per_call = input_limit + output_limit if type(input_limit) is int and type(output_limit) is int else None
    reason = context.model_exhaustion(per_call) if context.budget.max_total_tokens is None else None
    if reason:
        result.status = 'unavailable' if reason == 'provider_limit' else 'budget_exhausted'
        return result
    followup = prepare_counterpoint_prompt(base_prompt, analysis.claims,
        research_prompt_budget(None if callable(getattr(provider, 'research_prompt_fits', None)) else input_limit, SYSTEM_PROMPT))
    if followup is None or not research_prompt_fits(followup.prompt, SYSTEM_PROMPT, None if callable(getattr(provider, 'research_prompt_fits', None)) else input_limit):
        result.status = 'input_budget'
        return result
    result.prompt_sha256 = followup.prompt_sha256
    checkpoint(result.prompt_sha256)
    try:
        context.ensure_running()
        remaining = context.budget.deadline - context.now()
        if remaining <= 0:
            raise ProviderFailure('provider_budget_exhausted', 'Research time budget is exhausted.', retryable=False)
        reservation, output_cap = context.prepare_model_call(provider, followup.prompt, SYSTEM_PROMPT)
        output_kwargs = context.output_kwargs(generate, provider, output_cap)
        before = context.start_model(call_id, reservation)
        try:
            raw, reported = context.invoke_model(call_id, generate, followup.prompt, cancel_event=context.cancel_event(), system=SYSTEM_PROMPT,
                                     timeout_seconds=min(float(timeout), remaining), **output_kwargs)
            result.tokens = reported if type(reported) is int and reported >= 0 else None
        except ProviderFailure as error:
            result.tokens = error.reported_tokens if type(error.reported_tokens) is int and error.reported_tokens >= 0 else None
            raise
        finally:
            context.finish_model(call_id, result.tokens, before)
        context.ensure_running()
        if not isinstance(raw, dict) or 'counterarguments' not in raw:
            raise InvalidAnalysis('Counterpoint output lacks the requested field.')
        allowed = {'counterarguments', 'limitations'}
        result.discarded_fields = len(set(raw) - allowed)
        restricted = {key: raw[key] for key in allowed if key in raw}
        filtered = filter_counterpoint_rows(restricted['counterarguments'])
        if filtered is None:
            raise InvalidAnalysis('Counterpoint rows exceed the allowed structure or limit.')
        restricted['counterarguments'], result.omitted_rows = filtered
        from hashlib import sha256
        from .research_output_contract import ANALYSIS_CONTRACT
        original_hashes = analysis.analyzed_source_hashes
        current_hashes = {source.source_id: sha256(source.text.encode('utf-8')).hexdigest() for source in sources
                          if source.source_id in original_hashes}
        if original_hashes and current_hashes != original_hashes:
            raise InvalidAnalysis('Counterpoint source content changed.')
        if not original_hashes and (not analysis.prompt_sha256 or not analysis.segments):
            raise InvalidAnalysis('Counterpoint source context cannot be verified.')
        prompt_contract = ANALYSIS_CONTRACT if analysis.output_contract_version == ANALYSIS_CONTRACT.version else None
        if analysis.output_contract_version not in (None, ANALYSIS_CONTRACT.version):
            raise InvalidAnalysis('Counterpoint prompt contract is unsupported.')
        def check(payload: dict, tokens: int | None) -> AnalysisResult:
            return analyze(question, sources, as_of, lambda _: (payload, tokens), max_prompt_chars=max_prompt_chars,
                should_stop=lambda: context.now() >= context.budget.deadline, focus=focus, scope=scope,
                counterargument_reference_count=followup.prior_findings_included, planning_steps=planning_steps,
                require_counterarguments=request.require_counterarguments or request.counterpoint_followup, output_contract=prompt_contract, legacy_payload=True)
        checked = check(restricted, result.tokens)
        if checked.prompt_sha256 != analysis.prompt_sha256 or checked.segments != analysis.segments:
            raise InvalidAnalysis('Counterpoint source selection changed.')
        raw_leads = extract_answer_candidates(raw)
        if raw_leads:
            try:
                # Claim leads do not use the counterargument reference bound.
                checked_leads = analyze(question, sources, as_of, lambda _: ({'claims': raw_leads}, None),
                    max_prompt_chars=max_prompt_chars, should_stop=lambda: context.now() >= context.budget.deadline,
                    focus=focus, scope=scope, planning_steps=planning_steps,
                    require_counterarguments=request.require_counterarguments or request.counterpoint_followup, output_contract=prompt_contract, legacy_payload=True)
                result.leads = checked_leads.claims if (checked_leads.prompt_sha256 == analysis.prompt_sha256 and checked_leads.segments == analysis.segments) else []
            except (InvalidAnalysis, AnalysisInterrupted):
                result.leads = []
        first_followup_index = len(analysis.claims) + len(analysis.counterarguments)
        room = 20 - len(analysis.counterarguments)
        retained = [{**item, **({'prior_findings_truncated': True} if followup.truncated_findings else {})}
                    for item in checked.counterarguments[:room]]
        truncated = len(checked.counterarguments) > room
        result.analysis = replace(analysis, counterarguments=[*analysis.counterarguments, *retained],
            limitations=list(dict.fromkeys([*analysis.limitations, *checked.limitations,
                'Counterpoint follow-up uses a model and is not independent verification.',
                *(['Extra counterpoint response fields were not used as findings; any citation leads require review.'] if result.discarded_fields else []),
                *(['Some malformed counterpoints were omitted; review the remaining results.'] if result.omitted_rows else []),
                *(['Some follow-up counterpoints were omitted because the result limit was reached.'] if truncated else [])])))
        result.claims = prepare_run_claims(result.analysis, sources, as_of, result.leads)
        extra_claims = result.claims[first_followup_index:]
        differences = [*claim_calculation_differences(result.claims, result.analysis.calculations, result.analysis.facts),
                       *claim_amount_differences(result.claims, result.analysis.facts)]
        excluded = {item['claim_index'] - first_followup_index for item in differences if item['claim_index'] >= first_followup_index}
        result.status = ('no_evidence' if not any(item.get('citations') for item in retained) else
                         'partial' if truncated or result.omitted_rows else
                         'needs_review' if not has_usable_counterpoint(extra_claims, excluded) else 'completed')
    except (ProviderFailure, InvalidAnalysis, AnalysisInterrupted, ApiProblem) as error:
        context.ensure_running()
        if isinstance(error, InvalidAnalysis):
            context.mark_model_processing_error(call_id, 'contract_validation')
        result.status = 'failed'
        result.error = error.code if isinstance(error, ProviderFailure) else 'counterpoint_unavailable'
    return result
