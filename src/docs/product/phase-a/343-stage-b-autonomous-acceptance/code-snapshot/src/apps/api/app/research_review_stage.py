"""Budgeted model evidence-review stage with explicit checkpoint capability."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .operations import ApiProblem
from .phase3 import ProviderFailure
from .research_analysis import AnalysisInterrupted, AnalysisResult
from .research_calculations import claim_calculation_differences
from .research_coordinator import ResearchExecutionContext, research_prompt_budget, research_prompt_fits
from .research_evidence import claim_amount_differences
from .research_runs import ResearchRunRequest
from .research_sources import ResearchSource
from .research_verification import (ISOLATED_REVIEW_METHOD, REVIEW_METHOD, REVIEW_SYSTEM_PROMPT, InvalidReview,
    apply_partial_support_review, parse_isolated_support_review_details, prepare_isolated_support_reviews,
    prepare_support_review, review_record)


@dataclass
class ReviewStageResult:
    claims: list[dict[str, Any]]
    status: str = 'not_requested'
    tokens: int | None = None
    error: str | None = None
    prompt_sha256: str | None = None
    segments: list[dict[str, Any]] = field(default_factory=list)
    indexes: list[int] = field(default_factory=list)
    omissions: list[dict[str, Any]] = field(default_factory=list)
    failed_indexes: list[int] = field(default_factory=list)
    isolated_records: list[dict[str, Any]] = field(default_factory=list)


def execute_support_review(*, request: ResearchRunRequest, analysis: AnalysisResult, claims: list[dict[str, Any]],
                           sources: list[ResearchSource], provider: Any, generate: Any,
                           context: ResearchExecutionContext, operation_id: str,
                           checkpoint: Callable[[ReviewStageResult, str], None]) -> ReviewStageResult:
    """Review cited findings in a single batch or bounded separate calls."""
    result = ReviewStageResult(claims, omissions=[{'claim_index': i, 'reason': 'not_started'}
                                                  for i in range(len(claims))] if request.support_review else [])
    if not request.support_review:
        return result
    result.status = 'not_reviewable'
    if context.calls and claims and request.max_model_calls is not None and context.calls >= request.max_model_calls:
        result.status = 'budget_exhausted'
        result.omissions = [{'claim_index': i, 'reason': 'call_budget'} for i in range(len(claims))]
        return result
    policy = getattr(provider, 'policy', None)
    input_limit, output_limit = getattr(policy, 'max_input_tokens', None), getattr(policy, 'max_output_tokens', None)
    timeout = getattr(policy, 'timeout_seconds', None)
    if not context.calls or not claims or not isinstance(input_limit, int) or not isinstance(output_limit, int) or not isinstance(timeout, (int, float)):
        return result
    per_call = input_limit + output_limit
    budget = research_prompt_budget(input_limit, REVIEW_SYSTEM_PROMPT)
    differences = [*claim_calculation_differences(claims, analysis.calculations, analysis.facts),
                   *claim_amount_differences(claims, analysis.facts)]
    indexes = {item['claim_index'] for item in differences}
    review_claims = [{**claim, **({'numeric_discrepancy_for_review': True} if i in indexes else {})}
                     for i, claim in enumerate(claims)]
    allowed_sources = [source for source in sources if source.source_id in analysis.source_ids]
    if request.support_review_mode == 'isolated':
        try:
            plan = prepare_isolated_support_reviews(review_claims, allowed_sources, budget,
                should_stop=lambda: context.now() >= context.budget.deadline or context.cancel_event().is_set())
        except AnalysisInterrupted:
            context.ensure_running()
            result.status = 'budget_exhausted'
            result.omissions = [{'claim_index': i, 'reason': 'time_budget'} for i in range(len(claims))]
            return result
        result.omissions = list(plan.omissions) + [{'claim_index': batch.indexes[0], 'reason': 'not_started'} for batch in plan.batches]
        result.omissions.sort(key=lambda item: item['claim_index'])
        result.isolated_records = [{'claim_index': batch.indexes[0], 'prompt_sha256': batch.prompt_sha256,
                                    'segments': batch.segments, 'status': 'not_started'} for batch in plan.batches]
        result.claims = [{key: value for key, value in claim.items() if key != 'support_review'} for claim in claims]
        usages: list[int | None] = []
        def omission(index: int, reason: str | None) -> None:
            result.omissions = [item for item in result.omissions if item['claim_index'] != index]
            if reason is not None:
                result.omissions.append({'claim_index': index, 'reason': reason})
            result.omissions.sort(key=lambda item: item['claim_index'])
        if plan.batches:
            checkpoint(result, 'isolated_review')
            result.status = 'partial'
        for position, batch in enumerate(plan.batches):
            index = batch.indexes[0]
            context.ensure_running()
            reason = context.model_exhaustion(per_call)
            if reason:
                for pending in plan.batches[position:]:
                    omission(pending.indexes[0], reason)
                if not result.indexes:
                    result.status = 'budget_exhausted'
                break
            call_id = f'{operation_id}:support-review:{index}'
            reported: int | None = None
            try:
                if not research_prompt_fits(batch.prompt, REVIEW_SYSTEM_PROMPT, input_limit):
                    raise ProviderFailure('provider_budget_exhausted', 'Research input budget is exhausted.', retryable=False)
                before = context.start_model(call_id, per_call)
                remaining = context.budget.deadline - context.now()
                if remaining <= 0:
                    context.abort_model_before_call(call_id, before)
                    for pending in plan.batches[position:]:
                        omission(pending.indexes[0], 'time_budget')
                    if not result.indexes:
                        result.status = 'budget_exhausted'
                    break
                result.indexes.append(index)
                result.segments.extend(batch.segments)
                result.isolated_records[position]['status'] = 'in_progress'
                omission(index, 'in_progress')
                try:
                    checkpoint(result, 'isolated_review')
                except BaseException:
                    context.abort_model_before_call(call_id, before)
                    raise
                remaining = context.budget.deadline - context.now()
                if remaining <= 0:
                    context.abort_model_before_call(call_id, before)
                    result.indexes.pop()
                    if batch.segments:
                        del result.segments[-len(batch.segments):]
                    result.isolated_records[position]['status'] = 'not_started'
                    for pending in plan.batches[position:]:
                        omission(pending.indexes[0], 'time_budget')
                    if not result.indexes:
                        result.status = 'budget_exhausted'
                    break
                try:
                    raw, reported_raw = context.invoke_model(call_id, generate, batch.prompt, cancel_event=context.cancel_event(),
                        system=REVIEW_SYSTEM_PROMPT, timeout_seconds=min(float(timeout), remaining))
                    reported = reported_raw if type(reported_raw) is int and reported_raw >= 0 else None
                except ProviderFailure as error:
                    reported = error.reported_tokens if type(error.reported_tokens) is int and error.reported_tokens >= 0 else None
                    raise
                finally:
                    context.finish_model(call_id, reported, before)
                    usages.append(reported)
                    result.tokens = sum(usages) if all(value is not None for value in usages) else None
                context.ensure_running()
                mapped, verdict = parse_isolated_support_review_details(batch, raw)
                result.claims[mapped] = {**result.claims[mapped], 'support_review':
                    review_record(batch, mapped, verdict.verdict, ISOLATED_REVIEW_METHOD, relevance=verdict.relevance,
                                  scope_declaration=verdict.scope_declaration, claimed_scope=verdict.claimed_scope)}
                result.isolated_records[position].update(status='completed', reported_tokens=reported)
                omission(index, None)
                checkpoint(result, 'isolated_review')
            except (ProviderFailure, InvalidReview, ApiProblem) as error:
                context.ensure_running()
                if isinstance(error, InvalidReview):
                    context.mark_model_processing_error(call_id, 'contract_validation')
                    context.record_validation_diagnostic(call_id, error.diagnostic)
                if isinstance(error, ApiProblem) and error.error_code == 'lifecycle_conflict':
                    raise
                result.status = 'failed'
                result.error = (error.code if isinstance(error, ProviderFailure) else
                                'review_invalid_output' if isinstance(error, InvalidReview) else 'review_unavailable')
                result.failed_indexes.append(index)
                result.isolated_records[position].update(status='failed', reported_tokens=reported)
                omission(index, 'review_failed')
                for pending in plan.batches[position + 1:]:
                    omission(pending.indexes[0], 'review_failure')
                break
        if result.status not in {'failed', 'budget_exhausted'} and plan.batches:
            result.status = 'partial' if result.omissions else 'complete'
        return result

    reason = context.model_exhaustion(per_call)
    if reason:
        result.status = 'budget_exhausted'
        result.omissions = [{'claim_index': i, 'reason': reason} for i in range(len(claims))]
        return result
    batch = prepare_support_review(review_claims, allowed_sources, budget)
    result.omissions = batch.omissions
    if not batch.indexes:
        return result
    result.prompt_sha256, result.segments, result.indexes = batch.prompt_sha256, batch.segments, batch.indexes
    checkpoint(result, 'generation')
    call_id = f'{operation_id}:support-review'
    try:
        context.ensure_running()
        remaining = context.budget.deadline - context.now()
        if remaining <= 0:
            raise ProviderFailure('provider_budget_exhausted', 'Research time budget is exhausted.', retryable=False)
        if not research_prompt_fits(batch.prompt, REVIEW_SYSTEM_PROMPT, input_limit):
            raise ProviderFailure('provider_budget_exhausted', 'Research input budget is exhausted.', retryable=False)
        before = context.start_model(call_id, per_call)
        try:
            raw, reported = context.invoke_model(call_id, generate, batch.prompt, cancel_event=context.cancel_event(), system=REVIEW_SYSTEM_PROMPT,
                                     timeout_seconds=min(float(timeout), remaining))
            result.tokens = reported if type(reported) is int and reported >= 0 else None
        except ProviderFailure as error:
            result.tokens = error.reported_tokens if type(error.reported_tokens) is int and error.reported_tokens >= 0 else None
            raise
        finally:
            context.finish_model(call_id, result.tokens, before)
        result.claims, missing = apply_partial_support_review(claims, batch, raw)
        result.omissions.extend({'claim_index': index, 'reason': 'model_omitted'} for index in missing)
        result.status = 'partial' if batch.omitted or missing else 'complete'
    except (ProviderFailure, InvalidReview, ApiProblem) as error:
        if isinstance(error, InvalidReview):
            context.mark_model_processing_error(call_id, 'contract_validation')
            context.record_validation_diagnostic(call_id, error.diagnostic)
        result.status = 'failed'
        result.failed_indexes = batch.indexes
        result.error = (error.code if isinstance(error, ProviderFailure) else
                        'review_invalid_output' if isinstance(error, InvalidReview) else 'review_unavailable')
    return result
