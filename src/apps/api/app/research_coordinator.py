"""Bounded stage handlers for a research run.

The API remains responsible for operation admission, persistence and lifecycle.
Stages receive only explicitly supplied capabilities and return recorded state;
they cannot expand source permissions or perform unbudgeted tool calls.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import inspect
from time import monotonic
from typing import Any, Callable

from pydantic import ValidationError

from .operations import ApiProblem
from .phase3 import ProviderFailure
from .research_budget import ResearchExecutionBudget
from .research_planning import PLAN_SYSTEM_PROMPT, parse_plan, plan_prompt


@dataclass(frozen=True)
class PlanningResult:
    status: str
    steps: tuple[str, ...] = ()
    tokens: int | None = None
    prompt_sha256: str | None = None
    error: str | None = None
    attempted: bool = False


def execute_planning(*, requested: bool, question: str, scope: str, sources_available: bool,
                     provider: Any, generate: Any, budget: ResearchExecutionBudget,
                     calls: int, usage: int | None, cost_limited: bool, call_id: str,
                     ensure_running: Callable[[], None], cancel_event: Callable[[], Any],
                     reserve: Callable[[str, int], None], settle: Callable[[str, int | None], None],
                     now: Callable[[], float] = monotonic) -> PlanningResult:
    """Plan over the permitted scope without granting new tool permissions."""
    if not requested:
        return PlanningResult('not_requested')
    if cost_limited:
        return PlanningResult('cost_unknown')
    if not question or not sources_available or not callable(generate):
        return PlanningResult('unavailable')
    policy = getattr(provider, 'policy', None)
    input_limit = getattr(policy, 'max_input_tokens', None)
    output_limit = getattr(policy, 'max_output_tokens', None)
    timeout = getattr(policy, 'timeout_seconds', None)
    if (not isinstance(input_limit, int) or not isinstance(output_limit, int) or
            input_limit <= 0 or output_limit <= 0 or not isinstance(timeout, (int, float)) or not 0 < timeout <= 180):
        return PlanningResult('unavailable')
    per_call = input_limit + output_limit
    reason = budget.model_exhaustion(calls=calls, usage=usage, per_call=per_call, now=now(), required_calls=2)
    if reason:
        return PlanningResult('unavailable' if reason == 'provider_limit' else 'budget_exhausted')
    prompt = plan_prompt(question, scope)
    if len(prompt.encode('utf-8')) + len(PLAN_SYSTEM_PROMPT.encode('utf-8')) + 512 > input_limit:
        return PlanningResult('input_budget')
    attempted = False
    tokens: int | None = None
    prompt_hash: str | None = None
    try:
        if 'timeout_seconds' not in inspect.signature(generate).parameters:
            raise ProviderFailure('provider_budget_unknown', 'Provider limits cannot be enforced.', retryable=False)
        ensure_running()
        remaining = budget.deadline - now()
        if remaining <= 0:
            raise ProviderFailure('provider_budget_exhausted', 'Research time budget is exhausted.', retryable=False)
        reserve(call_id, per_call)
        attempted = True
        prompt_hash = sha256(prompt.encode('utf-8')).hexdigest()
        try:
            raw, reported = generate(prompt, cancel_event=cancel_event(), system=PLAN_SYSTEM_PROMPT,
                                     timeout_seconds=min(float(timeout), remaining))
            tokens = reported if type(reported) is int and reported >= 0 else None
            steps = tuple(parse_plan(raw))
        except ProviderFailure as error:
            tokens = error.reported_tokens if type(error.reported_tokens) is int and error.reported_tokens >= 0 else None
            raise
        finally:
            settle(call_id, tokens)
        return PlanningResult('completed', steps, tokens, prompt_hash, attempted=True)
    except (ProviderFailure, ValidationError, TypeError, ValueError, ApiProblem) as error:
        ensure_running()
        code = error.code if isinstance(error, ProviderFailure) else 'planning_unavailable'
        return PlanningResult('failed', tokens=tokens, prompt_sha256=prompt_hash, error=code, attempted=attempted)
