"""Bounded stage handlers for a research run.

The API remains responsible for operation admission, persistence and lifecycle.
Stages receive only explicitly supplied capabilities and return recorded state;
they cannot expand source permissions or perform unbudgeted tool calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
import inspect
from time import monotonic
from typing import Any, Callable

from pydantic import ValidationError

from .operations import ApiProblem
from .phase3 import ProviderFailure
from .research_budget import ResearchExecutionBudget
from .research_analysis import (SYSTEM_PROMPT as RESEARCH_SYSTEM_PROMPT, AnalysisInterrupted,
                                AnalysisResult, InvalidAnalysis, analyze)
from .research_planning import PLAN_SYSTEM_PROMPT, parse_plan, parse_plan_actions, parse_plan_hints, parse_plan_requirements, plan_prompt
from .research_source_policy import prepare_sources
from .research_sources import ResearchSource

# Serialized input is conservatively bounded by UTF-8 bytes, with a separate
# allowance for message framing. Actual provider token counts remain external.
RESEARCH_INPUT_ENVELOPE_BYTES = 192


def research_prompt_budget(input_limit: int, system: str) -> int:
    """Return a fail-closed prompt byte limit including system and framing.

    One UTF-8 byte is conservatively charged as one input token. This is an
    admission bound, not a provider tokenizer or a claim of exact billing.
    """
    if type(input_limit) is not int or input_limit <= 0:
        return 0
    return max(0, min(48000, input_limit - len(system.encode('utf-8')) - RESEARCH_INPUT_ENVELOPE_BYTES))


def research_prompt_fits(prompt: str, system: str, input_limit: int) -> bool:
    return (type(input_limit) is int and input_limit >= len(system.encode('utf-8')) + RESEARCH_INPUT_ENVELOPE_BYTES
            and len(prompt.encode('utf-8')) <= research_prompt_budget(input_limit, system))


@dataclass
class ResearchExecutionContext:
    """Run-scoped budget and explicit lifecycle capabilities for stages."""
    budget: ResearchExecutionBudget
    cost_limited: bool
    ensure_running: Callable[[], None]
    cancel_event: Callable[[], Any]
    reserve: Callable[[str, int], None]
    settle: Callable[[str, int | None], None]
    reserve_search: Callable[[], None]
    now: Callable[[], float] = monotonic
    calls: int = 0
    usage: int | None = None
    # Separate conservative admission accounting from provider-reported usage.
    # Unknown calls consume their reservation; known calls consume reported use.
    accounted_tokens: int = 0
    search_calls: int = 0
    model_call_receipts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def search_exhaustion(self) -> str | None:
        return self.budget.search_exhaustion(calls=self.search_calls, now=self.now())

    def start_search(self) -> None:
        """Admit one discovery request; fetched documents use their own boundary."""
        self.ensure_running()
        if self.cost_limited:
            raise ProviderFailure('search_cost_unknown', 'Search pricing is unavailable for the requested cost cap.', retryable=False)
        if self.search_exhaustion():
            raise ProviderFailure('search_budget_exhausted', 'Search budget or time is exhausted.', retryable=False)
        self.reserve_search()
        self.search_calls += 1

    def model_exhaustion(self, per_call: int, *, required_calls: int = 1) -> str | None:
        return self.budget.model_exhaustion(calls=self.calls, usage=self.accounted_tokens,
            per_call=per_call, now=self.now(), required_calls=required_calls)

    def start_model(self, call_id: str, tokens: int) -> tuple[int, int | None, int]:
        self.ensure_running()
        if self.cost_limited:
            raise ProviderFailure('provider_budget_unknown', 'Model pricing is unavailable for the requested cost cap.', retryable=False)
        reason = self.model_exhaustion(tokens)
        if reason:
            raise ProviderFailure('provider_budget_unknown' if reason == 'provider_limit' else 'provider_budget_exhausted',
                                  'Model execution budget is unavailable or exhausted.', retryable=False)
        before = self.calls, self.usage, tokens
        if call_id in self.model_call_receipts:
            raise ValueError('Model call identifier was already used in this run.')
        self.reserve(call_id, tokens)
        self.calls += 1
        self.model_call_receipts[call_id] = {'provider': 'admitted', 'settlement': 'not_started', 'processing_error': None}
        return before

    def invoke_model(self, call_id: str, generate: Callable, *args: Any, **kwargs: Any) -> Any:
        receipt = self.model_call_receipts.get(call_id)
        if receipt is None or receipt['provider'] != 'admitted':
            raise ValueError('Model call was not admitted or was already invoked.')
        try:
            result = generate(*args, **kwargs)
        except BaseException:
            receipt['provider'] = 'unknown'
            raise
        receipt['provider'] = 'returned'
        return result

    def mark_model_processing_error(self, call_id: str, category: str) -> None:
        receipt = self.model_call_receipts.get(call_id)
        if receipt is not None and receipt['provider'] == 'returned' and category in {'contract_validation', 'post_response_processing_failure'}:
            receipt['processing_error'] = category

    def finish_model(self, call_id: str, tokens: int | None, before: tuple[int, int | None, int]) -> None:
        previous_calls, previous_usage, reserved = before
        receipt = self.model_call_receipts.get(call_id)
        settled = False
        try:
            self.settle(call_id, tokens)
            settled = True
        finally:
            known = type(tokens) is int and tokens >= 0
            if receipt is not None:
                receipt['settlement'] = ('settled' if settled and known and receipt['provider'] == 'returned' else 'unknown')
            self.accounted_tokens += tokens if settled and known else reserved
            reported = tokens if settled and known else None
            self.usage = (reported if previous_calls == 0 else
                          previous_usage + reported if previous_usage is not None and reported is not None else None)

    def abort_model_before_call(self, call_id: str, before: tuple[int, int | None, int]) -> None:
        """Release a reservation only when no provider invocation occurred."""
        receipt = self.model_call_receipts.get(call_id)
        if receipt is not None:
            receipt.update(provider='not_invoked', settlement='unknown')
        self.settle(call_id, 0)
        if receipt is not None:
            receipt['settlement'] = 'released'
        self.calls, self.usage = before[:2]


@dataclass(frozen=True)
class PlanningResult:
    status: str
    steps: tuple[str, ...] = ()
    tokens: int | None = None
    prompt_sha256: str | None = None
    error: str | None = None
    attempted: bool = False
    suggested_actions: tuple[str, ...] = ()
    retrieval_hints: tuple[str, ...] = ()
    requirements: tuple[dict, ...] = ()


def execute_planning(*, requested: bool, question: str, scope: str,
                     provider: Any, generate: Any, context: ResearchExecutionContext,
                     call_id: str, source_languages: tuple[str, ...] = ()) -> PlanningResult:
    """Plan over the permitted scope without granting new tool permissions."""
    if not requested:
        return PlanningResult('not_requested')
    if context.cost_limited:
        return PlanningResult('cost_unknown')
    if not question or not callable(generate):
        return PlanningResult('unavailable')
    policy = getattr(provider, 'policy', None)
    input_limit = getattr(policy, 'max_input_tokens', None)
    output_limit = getattr(policy, 'max_output_tokens', None)
    timeout = getattr(policy, 'timeout_seconds', None)
    if (not isinstance(input_limit, int) or not isinstance(output_limit, int) or
            input_limit <= 0 or output_limit <= 0 or not isinstance(timeout, (int, float)) or not 0 < timeout <= 180):
        return PlanningResult('unavailable')
    per_call = input_limit + output_limit
    reason = context.model_exhaustion(per_call, required_calls=2)
    if reason:
        return PlanningResult('unavailable' if reason == 'provider_limit' else 'budget_exhausted')
    prompt = plan_prompt(question, scope, source_languages)
    if not research_prompt_fits(prompt, PLAN_SYSTEM_PROMPT, input_limit):
        return PlanningResult('input_budget')
    attempted = False
    tokens: int | None = None
    prompt_hash: str | None = None
    try:
        if 'timeout_seconds' not in inspect.signature(generate).parameters:
            raise ProviderFailure('provider_budget_unknown', 'Provider limits cannot be enforced.', retryable=False)
        context.ensure_running()
        remaining = context.budget.deadline - context.now()
        if remaining <= 0:
            raise ProviderFailure('provider_budget_exhausted', 'Research time budget is exhausted.', retryable=False)
        before = context.start_model(call_id, per_call)
        attempted = True
        prompt_hash = sha256(prompt.encode('utf-8')).hexdigest()
        try:
            raw, reported = context.invoke_model(call_id, generate, prompt, cancel_event=context.cancel_event(), system=PLAN_SYSTEM_PROMPT,
                                     timeout_seconds=min(float(timeout), remaining))
            tokens = reported if type(reported) is int and reported >= 0 else None
            steps = tuple(parse_plan(raw))
            suggested_actions = parse_plan_actions(raw)
            retrieval_hints = parse_plan_hints(raw)
            requirements = parse_plan_requirements(raw)
        except ProviderFailure as error:
            tokens = error.reported_tokens if type(error.reported_tokens) is int and error.reported_tokens >= 0 else None
            raise
        finally:
            context.finish_model(call_id, tokens, before)
        return PlanningResult('completed', steps, tokens, prompt_hash, attempted=True, suggested_actions=suggested_actions, retrieval_hints=retrieval_hints, requirements=requirements)
    except (ProviderFailure, ValidationError, TypeError, ValueError, ApiProblem) as error:
        context.mark_model_processing_error(call_id, 'contract_validation' if isinstance(error, ValidationError)
                                            else 'post_response_processing_failure')
        context.ensure_running()
        code = error.code if isinstance(error, ProviderFailure) else 'planning_unavailable'
        return PlanningResult('failed', tokens=tokens, prompt_sha256=prompt_hash, error=code, attempted=attempted)


@dataclass(frozen=True)
class GenerationResult:
    analysis: AnalysisResult
    calls: int
    usage: int | None
    generation_tokens: int | None
    prompt_text: str | None
    max_prompt_chars: int
    unavailable_reason: str | None = None
    error_detail: str | None = None


def execute_generation(*, question: str, scope: str, sources: list[ResearchSource], as_of: datetime | None,
                       provider: Any, generate: Any, context: ResearchExecutionContext, call_id: str,
                       focus: tuple[str, int, int] | None, planning_steps: list[str],
                       require_counterarguments: bool, retrieval_hints: tuple[str, ...] = (),
                       planning_requirements: tuple[dict, ...] = ()) -> GenerationResult:
    """Run the first analysis with explicit source, budget and lifecycle capabilities."""
    attempted_prompt: dict[str, Any] = {}
    prompt_text: str | None = None
    generation_tokens: int | None = None
    unavailable_reason: str | None = None
    error_detail: str | None = None

    def generate_once(prompt: str) -> tuple[dict[str, Any], int | None]:
        nonlocal prompt_text, generation_tokens
        if context.cost_limited:
            raise ProviderFailure('provider_budget_unknown', 'Model pricing is not configured for the requested cost cap.', retryable=False)
        if not callable(generate):
            raise ProviderFailure('provider_unavailable', 'A structured research model is not configured.', retryable=False)
        policy = getattr(provider, 'policy', None)
        input_limit = getattr(policy, 'max_input_tokens', None)
        output_limit = getattr(policy, 'max_output_tokens', None)
        timeout = getattr(policy, 'timeout_seconds', None)
        try:
            parameters = inspect.signature(generate).parameters
        except (TypeError, ValueError) as error:
            raise ProviderFailure('provider_budget_unknown', 'Provider limits cannot be enforced.', retryable=False) from error
        if (not isinstance(input_limit, int) or not isinstance(output_limit, int) or
                input_limit <= 0 or output_limit <= 0 or not isinstance(timeout, (int, float)) or
                not 0 < timeout <= 180 or 'timeout_seconds' not in parameters):
            raise ProviderFailure('provider_budget_unknown', 'Provider limits cannot be enforced.', retryable=False)
        if not research_prompt_fits(prompt, RESEARCH_SYSTEM_PROMPT, input_limit):
            raise ProviderFailure('provider_budget_exhausted', 'Research input budget is exhausted.', retryable=False)
        token_reservation = input_limit + output_limit
        reason = context.model_exhaustion(token_reservation)
        if reason:
            raise ProviderFailure('provider_budget_unknown' if reason == 'provider_limit' else 'provider_budget_exhausted',
                                  'Model execution budget is unavailable or exhausted.', retryable=False)
        remaining = context.budget.deadline - context.now()
        if remaining <= 0:
            raise ProviderFailure('provider_budget_exhausted', 'Research time budget is exhausted.', retryable=False)
        before = context.start_model(call_id, token_reservation)
        prompt_text = prompt
        attempted_prompt.update({'prompt_sha256': sha256(prompt.encode('utf-8')).hexdigest(),
                                 'prompt_characters': len(prompt), 'prompt_bytes': len(prompt.encode('utf-8'))})
        try:
            payload, reported = context.invoke_model(call_id, generate, prompt, cancel_event=context.cancel_event(), system=RESEARCH_SYSTEM_PROMPT,
                                         timeout_seconds=min(float(timeout), remaining))
            generation_tokens = reported if type(reported) is int and reported >= 0 else None
            return payload, generation_tokens
        except ProviderFailure as error:
            generation_tokens = error.reported_tokens if type(error.reported_tokens) is int and error.reported_tokens >= 0 else None
            raise
        finally:
            context.finish_model(call_id, generation_tokens, before)

    max_input_tokens = getattr(getattr(provider, 'policy', None), 'max_input_tokens', 3000)
    # Selection can still report source coverage when provider limits are
    # unavailable; the actual call remains blocked by generate_once.
    selection_limit = max_input_tokens if type(max_input_tokens) is int and max_input_tokens > 0 else 3000
    max_prompt_chars = research_prompt_budget(selection_limit, RESEARCH_SYSTEM_PROMPT)
    try:
        analysis = analyze(question, sources, as_of, generate_once, max_prompt_chars=max_prompt_chars,
                           should_stop=lambda: context.now() >= context.budget.deadline,
                           on_prepare=lambda segments: attempted_prompt.update({'segments': segments}),
                           focus=focus, scope=scope, planning_steps=planning_steps,
                           require_counterarguments=require_counterarguments, retrieval_hints=retrieval_hints,
                           planning_requirements=planning_requirements)
    except (AnalysisInterrupted, ProviderFailure, InvalidAnalysis) as error:
        if isinstance(error, InvalidAnalysis):
            context.mark_model_processing_error(call_id, 'contract_validation')
        unavailable_reason = ('provider_budget_exhausted' if isinstance(error, AnalysisInterrupted) else
                              error.code if isinstance(error, ProviderFailure) else 'invalid_model_output')
        if isinstance(error, InvalidAnalysis):
            error_detail = str(error)
        selected_ids = list(dict.fromkeys(segment['source_id'] for segment in attempted_prompt.get('segments', [])))
        _, source_notes = prepare_sources(sources, as_of)
        if selected_ids:
            source_notes.append('Sources were selected for analysis, but the run did not produce valid research; check the run status and model-call record.')
        analysis = AnalysisResult([], [], [*source_notes, 'Research analysis could not be completed: ' + unavailable_reason],
                                  selected_ids, context.usage, **attempted_prompt)
    context.ensure_running()
    return GenerationResult(analysis, context.calls, context.usage, generation_tokens, prompt_text, max_prompt_chars,
                            unavailable_reason, error_detail)


def has_usable_research_counterpoint(claims: list[dict[str, Any]], facts: list[dict[str, Any]],
                                     calculations: list[dict[str, Any]]) -> bool:
    """Use the same evidence and numeric-discrepancy rule for selection and execution."""
    from .research_calculations import claim_calculation_differences
    from .research_evidence import claim_amount_differences
    from .research_runs import has_usable_counterpoint
    differences = [*claim_calculation_differences(claims, calculations, facts),
                   *claim_amount_differences(claims, facts)]
    return has_usable_counterpoint(claims, {item['claim_index'] for item in differences})
