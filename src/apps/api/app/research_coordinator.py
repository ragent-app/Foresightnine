"""Bounded stage handlers for a research run.

The API remains responsible for operation admission, persistence and lifecycle.
Stages receive only explicitly supplied capabilities and return recorded state;
they cannot expand source permissions or perform unbudgeted tool calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from hashlib import sha256
import inspect
import json
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


def research_prompt_budget(input_limit: int | None, system: str) -> int | None:
    """Return a fail-closed prompt byte limit including system and framing.

    One UTF-8 byte is conservatively charged as one input token. This is an
    admission bound, not a provider tokenizer or a claim of exact billing.
    """
    if input_limit is None:
        return None
    if type(input_limit) is not int or input_limit <= 0:
        return 0
    return max(0, input_limit - len(system.encode('utf-8')) - RESEARCH_INPUT_ENVELOPE_BYTES)


def research_prompt_fits(prompt: str, system: str, input_limit: int | None) -> bool:
    if input_limit is None:
        return True
    return (type(input_limit) is int and input_limit >= len(system.encode('utf-8')) + RESEARCH_INPUT_ENVELOPE_BYTES
            and len(prompt.encode('utf-8')) <= research_prompt_budget(input_limit, system))


def dual_input_estimate(provider: Any, prompt: str, system: str, contract=None):
    """Return both conservative UTF-8 byte safety bound and ceil(chars/3) heuristic estimate."""
    from .provider_capabilities import DualInputEstimate, estimate_heuristic_tokens
    raw_utf8 = len(prompt.encode('utf-8')) + len(system.encode('utf-8')) + RESEARCH_INPUT_ENVELOPE_BYTES
    dual_estimator = getattr(provider, 'estimate_research_input_dual', None)
    if callable(dual_estimator):
        return dual_estimator(prompt, system, contract)
    estimator = getattr(provider, 'estimate_research_input', None)
    utf8_estimate = estimator(prompt, system, contract) if callable(estimator) else raw_utf8
    if callable(estimator) and utf8_estimate > raw_utf8 + 512:
        heuristic = max(
            estimate_heuristic_tokens(prompt, envelope_chars=len(system) + RESEARCH_INPUT_ENVELOPE_BYTES),
            (utf8_estimate + 2) // 3,
        )
    else:
        heuristic = estimate_heuristic_tokens(prompt, envelope_chars=len(system) + RESEARCH_INPUT_ENVELOPE_BYTES)
    return DualInputEstimate(
        utf8_byte_upper_bound=utf8_estimate,
        heuristic_tokens=heuristic,
        char_count=len(prompt) + len(system),
    )


@dataclass
class ResearchExecutionContext:
    """Run-scoped budget and explicit lifecycle capabilities for stages."""
    budget: ResearchExecutionBudget
    cost_limited: bool
    ensure_running: Callable[[], None]
    cancel_event: Callable[[], Any]
    reserve: Callable[[str, int | None], None]
    settle: Callable[[str, int | None], None]
    reserve_search: Callable[[], None]
    now: Callable[[], float] = monotonic
    calls: int = 0
    usage: int | None = None
    # Separate conservative admission accounting from provider-reported usage.
    # Unknown calls consume their reservation; known calls consume reported use.
    accounted_tokens: int | None = 0
    search_calls: int = 0
    model_call_receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    private_diagnostics: dict[str, dict[str, Any]] = field(default_factory=dict)
    private_finish: dict[str, str] = field(default_factory=dict)
    response_error_usage: set[str] = field(default_factory=set)
    persist_private_diagnostics: Callable[[dict[str, dict[str, Any]]], None] | None = None
    entitlement_remaining: Callable[[], int | None] = lambda: None
    pending_reservation: dict[str, Any] = field(default_factory=dict)
    diagnostic_context: dict[str, Any] = field(default_factory=dict)
    diagnostic_write_failed: bool = False

    def prepare_model_call(self, provider: Any, prompt: str, system: str, contract=None) -> tuple[int | None, int | None]:
        """Return estimated reservation and output cap derived from actual budgets."""
        policy = getattr(provider, 'policy', None)
        explicit_input = getattr(policy, 'max_input_tokens', None)
        explicit_output = getattr(policy, 'max_output_tokens', None)
        dual = dual_input_estimate(provider, prompt, system, contract)
        estimate = dual.utf8_byte_upper_bound
        heuristic_estimate = dual.heuristic_tokens
        if explicit_input is not None and estimate > explicit_input:
            raise ProviderFailure('provider_budget_exhausted', 'Estimated input exceeds the configured per-call budget.', retryable=False)
        capacity = getattr(provider, 'research_capacity', None)
        profile = capacity() if callable(capacity) else None
        profile_output = getattr(profile, 'output_tokens', None)
        output = explicit_output if type(explicit_output) is int else profile_output
        if type(explicit_output) is int and type(profile_output) is int:
            output = min(explicit_output, profile_output)
        context_limit = getattr(profile, 'context_tokens', None)
        input_counter = getattr(profile, 'input_counter', None)
        context_estimate = None
        if type(context_limit) is int:
            if input_counter not in ('utf8_byte_upper_bound', 'chars_div_3'):
                raise ProviderFailure('provider_budget_unknown', 'Cannot verify context fit without a configured input counter.', retryable=False)
            context_estimate = heuristic_estimate if input_counter == 'chars_div_3' else estimate
            context_output = context_limit - context_estimate
            if context_output <= 0:
                raise ProviderFailure('provider_budget_unknown', 'Cannot verify context fit for this input.', retryable=False)
            output = min(output, context_output) if output is not None else context_output
        if self.budget.max_total_tokens is not None:
            if self.accounted_tokens is None:
                raise ProviderFailure('provider_budget_unknown', 'Prior model usage is unknown.', retryable=False)
            remaining = self.budget.max_total_tokens - self.accounted_tokens
            if remaining <= estimate:
                raise ProviderFailure('provider_budget_exhausted', 'Estimated input exhausts the task token budget.', retryable=False)
            task_output = remaining - estimate
            output = min(output, task_output) if output is not None else task_output
        reservation = estimate + output if type(output) is int else None
        entitlement = self.entitlement_remaining()
        if entitlement is not None:
            if reservation is None:
                raise ProviderFailure('provider_budget_unknown', 'A bounded per-call output limit is required for account admission.', retryable=False)
            if reservation > entitlement:
                raise ProviderFailure('provider_budget_exhausted', 'The planned call exceeds the available account token quota.', retryable=False)
        self.pending_reservation = {
            'method': 'utf8_wire_estimate',
            'estimated_input_tokens': estimate,
            'estimated_input_bytes_upper_bound': estimate,
            'estimated_input_tokens_heuristic': heuristic_estimate,
            'heuristic_method': dual.heuristic_method,
            **({'context_input_counter': input_counter, 'context_input_tokens': context_estimate}
               if type(context_limit) is int and context_estimate is not None else {}),
            'reserved_tokens': reservation,
            'output_limit': output,
        }
        return reservation, output

    def output_kwargs(self, generate: Callable, provider: Any, output: int | None) -> dict[str, int]:
        if output is None:
            return {}
        try:
            parameters = inspect.signature(generate).parameters
        except (TypeError, ValueError):
            parameters = {}
        if 'max_output_tokens' in parameters:
            return {'max_output_tokens': output}
        fixed = getattr(getattr(provider, 'policy', None), 'max_output_tokens', None)
        if type(fixed) is int and fixed <= output:
            return {}
        raise ProviderFailure('provider_budget_unknown', 'The provider cannot enforce the requested output reservation.', retryable=False)

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

    def model_exhaustion(self, per_call: int | None, *, required_calls: int = 1) -> str | None:
        return self.budget.model_exhaustion(calls=self.calls, usage=self.accounted_tokens,
            per_call=per_call, now=self.now(), required_calls=required_calls)

    def start_model(self, call_id: str, tokens: int | None) -> tuple[int, int | None, int | None]:
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
        if self.pending_reservation:
            self.private_diagnostics.setdefault(call_id, {})['budget_estimate'] = dict(self.pending_reservation)
            self.pending_reservation = {}
            self._persist_diagnostic(call_id)
        return before

    def _persist_diagnostic(self, call_id: str) -> None:
        if self.persist_private_diagnostics is None:
            return
        try:
            self.persist_private_diagnostics({call_id: self.private_diagnostics[call_id]})
        except Exception as error:
            self.diagnostic_write_failed = True
            receipt = self.model_call_receipts.get(call_id)
            if receipt is not None:
                receipt['diagnostic_status'] = 'failed'
            from .research_diagnostics import report_diagnostic_write_failure
            report_diagnostic_write_failure(error, operation_id=self.diagnostic_context.get('operation_id'))

    def invoke_model(self, call_id: str, generate: Callable, *args: Any, **kwargs: Any) -> Any:
        receipt = self.model_call_receipts.get(call_id)
        if receipt is None or receipt['provider'] != 'admitted':
            raise ValueError('Model call was not admitted or was already invoked.')
        from .research_diagnostics import (
            get_finish_reason,
            normalize_finish_reason,
            safe_diagnostic_context,
            safe_exception_diagnostic,
            safe_stop_reason,
            safe_usage_diagnostics,
            set_finish_reason,
        )
        metadata = dict(self.diagnostic_context)
        self.diagnostic_context.pop('source_range_fingerprint', None)
        self.diagnostic_context.pop('parent_call_id', None)
        metadata['call_id'] = call_id
        if args and isinstance(args[0], str):
            metadata['input_hash'] = sha256(args[0].encode('utf-8')).hexdigest()
        if isinstance(kwargs.get('system'), str):
            metadata['system_hash'] = sha256(kwargs['system'].encode('utf-8')).hexdigest()
        contract = kwargs.get('contract')
        if contract is not None:
            metadata['contract'] = getattr(contract, 'version', None)
        parameters = dict(metadata.get('parameters') or {})
        parameters.update({key: kwargs[key] for key in ('temperature', 'top_p', 'max_tokens', 'max_completion_tokens', 'seed') if key in kwargs})
        metadata['parameters'] = parameters
        self.private_diagnostics.setdefault(call_id, {})['context'] = safe_diagnostic_context(**metadata)
        self._persist_diagnostic(call_id)
        set_finish_reason(None)
        from .research_diagnostics import sanitize_stream_metrics, sanitize_transport_diagnostic
        turn_arg = args[0] if args else None
        raw_tools = getattr(turn_arg, 'tools', None)
        declared_tool_names = (
            frozenset(t.name for t in raw_tools if isinstance(getattr(t, 'name', None), str) and t.name)
            if isinstance(raw_tools, (tuple, list))
            else None
        ) or None
        def capture_transport(snapshot: object) -> None:
            if not isinstance(snapshot, dict):
                return
            diagnostic = self.private_diagnostics.setdefault(call_id, {})
            if 'submission_state' in snapshot:
                diagnostic['transport'] = sanitize_transport_diagnostic(snapshot)
            if isinstance(snapshot.get('request_metadata'), dict):
                diagnostic.setdefault('context', {}).update(safe_diagnostic_context(**snapshot['request_metadata']))
            if type(snapshot.get('wire_bytes')) is int and 0 <= snapshot['wire_bytes'] <= 10**9:
                diagnostic.setdefault('context', {})['wire_bytes'] = snapshot['wire_bytes']
            if type(snapshot.get('max_tokens')) is int and 0 < snapshot['max_tokens'] <= 10**9:
                diagnostic.setdefault('context', {}).setdefault('parameters', {}).setdefault('max_tokens', snapshot['max_tokens'])
            if isinstance(snapshot.get('stream_metrics'), dict):
                safe_metrics = sanitize_stream_metrics(snapshot['stream_metrics'], allowed_tool_names=declared_tool_names)
                if safe_metrics:
                    diagnostic['stream_metrics'] = safe_metrics
            if 'stop_reason' in snapshot:
                diagnostic['stop_reason'] = safe_stop_reason(snapshot.get('stop_reason'))
                diagnostic['finish'] = normalize_finish_reason(snapshot.get('stop_reason'))
            if 'usage' in snapshot:
                safe_usage = safe_usage_diagnostics(snapshot.get('usage'))
                if safe_usage:
                    diagnostic['usage'] = safe_usage
            if isinstance(snapshot.get('provider_error'), dict):
                from .research_diagnostics import sanitize_provider_error, safe_messages_error
                value = snapshot['provider_error']
                diagnostic['provider_error'] = sanitize_provider_error(value)
                diagnostic['provider_error'].update(safe_messages_error(value, value.get('status_code'),
                    {'request-id': value.get('request_id')}))
            self._persist_diagnostic(call_id)
        def _resolved_finish() -> str:
            finish_value = get_finish_reason()
            if finish_value == 'unknown':
                existing = self.private_diagnostics.get(call_id, {}).get('finish')
                if isinstance(existing, str) and existing != 'unknown':
                    return existing
            return finish_value
        try:
            try:
                supports_diagnostics = 'diagnostic_callback' in inspect.signature(generate).parameters
            except (TypeError, ValueError):
                supports_diagnostics = False
            if supports_diagnostics:
                kwargs['diagnostic_callback'] = capture_transport
            result = generate(*args, **kwargs)
        except BaseException as error:
            if (getattr(error, 'provider_response_received', False) is True and
                    type(getattr(error, 'reported_tokens', None)) is int and error.reported_tokens >= 0):
                self.response_error_usage.add(call_id)
            receipt['provider'] = ('returned' if getattr(error, 'provider_response_received', False) is True else 'unknown')
            if receipt['provider'] == 'returned':
                receipt['processing_error'] = (
                    'stream_timeout'
                    if isinstance(error, ProviderFailure) and error.code == 'provider_timeout'
                    else 'post_response_processing_failure'
                )
            finish_value = _resolved_finish()
            if isinstance(error, ProviderFailure) and error.code == 'invalid_provider_output':
                category = getattr(error, 'diagnostic_category', None)
                category = category if category in {'response_limit', 'envelope_parse', 'provider_shape', 'content_parse'} else 'provider_parse'
                self.private_diagnostics.setdefault(call_id, {}).update(category=category, errors=[], finish=finish_value)
                self._persist_diagnostic(call_id)
            self.private_diagnostics.setdefault(call_id, {}).update(finish=finish_value, exception=safe_exception_diagnostic(error))
            self._persist_diagnostic(call_id)
            raise
        self.private_finish[call_id] = _resolved_finish()
        self.private_diagnostics.setdefault(call_id, {})['finish'] = self.private_finish[call_id]
        self._persist_diagnostic(call_id)
        receipt['provider'] = 'returned'
        return result

    def record_validation_diagnostic(self, call_id: str, diagnostic: object, *, schema=None) -> None:
        if isinstance(diagnostic, dict) and diagnostic.get('category') in {'contract_validation', 'index_constraint'}:
            from .research_diagnostics import sanitize_diagnostic_errors
            safe = {'category': diagnostic['category'], 'errors': sanitize_diagnostic_errors(diagnostic.get('errors'), schema=schema)}
            self.private_diagnostics.setdefault(call_id, {}).update({**safe, 'finish': self.private_finish.get(call_id, 'unknown')})
            self._persist_diagnostic(call_id)

    def mark_model_processing_error(self, call_id: str, category: str) -> None:
        receipt = self.model_call_receipts.get(call_id)
        if receipt is not None and receipt['provider'] == 'returned' and category in {'contract_validation', 'post_response_processing_failure', 'stream_timeout'}:
            receipt['processing_error'] = category

    def finish_model(self, call_id: str, tokens: int | None, before: tuple[int, int | None, int | None]) -> None:
        previous_calls, previous_usage, reserved = before
        receipt = self.model_call_receipts.get(call_id)
        known = (type(tokens) is int and tokens >= 0 and receipt is not None and
                 (receipt['provider'] == 'returned' or call_id in self.response_error_usage))
        settled = False
        try:
            self.settle(call_id, tokens if known else None)
            settled = True
        finally:
            if receipt is not None:
                receipt['settlement'] = ('settled' if settled and known and receipt['provider'] == 'returned' else 'unknown')
            charge = tokens if settled and known else reserved
            self.accounted_tokens = self.accounted_tokens + charge if self.accounted_tokens is not None and charge is not None else None
            reported = tokens if settled and known else None
            self.usage = (reported if previous_calls == 0 else
                          previous_usage + reported if previous_usage is not None and reported is not None else None)

    def abort_model_before_call(self, call_id: str, before: tuple[int, int | None, int | None]) -> None:
        """Release a reservation only when no provider invocation occurred."""
        receipt = self.model_call_receipts.get(call_id)
        if receipt is not None:
            receipt.update(provider='not_invoked', settlement='unknown')
        self.settle(call_id, 0)
        if receipt is not None:
            receipt['settlement'] = 'released'
        self.calls, self.usage = before[:2]


from .research_planning import parse_plan_structured_queries


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
    structured_queries: tuple[dict, ...] = ()


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
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 600:
        return PlanningResult('unavailable')
    prompt = plan_prompt(question, scope, source_languages)
    legacy_input_limit = None if callable(getattr(provider, 'research_prompt_fits', None)) else input_limit
    if not research_prompt_fits(prompt, PLAN_SYSTEM_PROMPT, legacy_input_limit):
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
        per_call, output_cap = context.prepare_model_call(provider, prompt, PLAN_SYSTEM_PROMPT)
        output_kwargs = context.output_kwargs(generate, provider, output_cap)
        before = context.start_model(call_id, per_call)
        attempted = True
        prompt_hash = sha256(prompt.encode('utf-8')).hexdigest()
        try:
            raw, reported = context.invoke_model(call_id, generate, prompt, cancel_event=context.cancel_event(), system=PLAN_SYSTEM_PROMPT,
                                     timeout_seconds=min(float(timeout), remaining), **output_kwargs)
            tokens = reported if type(reported) is int and reported >= 0 else None
            steps = tuple(parse_plan(raw))
            suggested_actions = parse_plan_actions(raw)
            retrieval_hints = parse_plan_hints(raw)
            requirements = parse_plan_requirements(raw)
            structured_queries = parse_plan_structured_queries(raw)
        except ProviderFailure as error:
            tokens = error.reported_tokens if type(error.reported_tokens) is int and error.reported_tokens >= 0 else None
            raise
        finally:
            context.finish_model(call_id, tokens, before)
        return PlanningResult('completed', steps, tokens, prompt_hash, attempted=True, suggested_actions=suggested_actions, retrieval_hints=retrieval_hints, requirements=requirements, structured_queries=structured_queries)
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
    max_prompt_chars: int | None
    unavailable_reason: str | None = None
    error_detail: str | None = None
    continuation: dict[str, Any] | None = None


def execute_generation(*, question: str, scope: str, sources: list[ResearchSource], as_of: datetime | None,
                       provider: Any, generate: Any, context: ResearchExecutionContext, call_id: str,
                       focus: tuple[str, int, int] | None, planning_steps: list[str],
                       require_counterarguments: bool, retrieval_hints: tuple[str, ...] = (),
                       planning_requirements: tuple[dict, ...] = (), candidate_cursor: int = 0,
                       expected_page_binding: str | None = None,
                       prior_segments: list[dict] | None = None,
                       repair: dict[str, Any] | None = None) -> GenerationResult:
    """Run the first analysis with explicit source, budget and lifecycle capabilities."""
    attempted_prompt: dict[str, Any] = {}
    prompt_text: str | None = None
    generation_tokens: int | None = None
    unavailable_reason: str | None = None
    error_detail: str | None = None
    from .research_output_contract import ANALYSIS_CONTRACT
    from .research_generation_repair import make_continuation, validate_continuation, inject_slot_dependencies, fingerprint, split_complete_segments
    try:
        generate_parameters = inspect.signature(generate).parameters if callable(generate) else {}
    except (TypeError, ValueError):
        generate_parameters = {}
    selected_contract = ANALYSIS_CONTRACT if 'contract' in generate_parameters else None
    original_prior = repair.get('prior_segments') if isinstance(repair, dict) and 'prior_segments' in repair else prior_segments
    capacity_key = provider.research_capacity_key() if callable(getattr(provider, 'research_capacity_key', None)) else None
    binding = sha256(json.dumps({'question': question, 'scope': scope, 'as_of': as_of.isoformat() if as_of else None,
        'sources': [{'id': source.source_id, 'hash': sha256(source.text.encode('utf-8')).hexdigest(),
                     'metadata_hash': fingerprint(source.model_dump(mode='json')),
                     'permissions': source.permissions.model_dump(mode='json')} for source in sources],
        'focus': focus, 'planning_steps': planning_steps, 'retrieval_hints': retrieval_hints,
        'requirements': planning_requirements, 'cursor': candidate_cursor, 'page_binding': expected_page_binding,
        'require_counterarguments': require_counterarguments, 'prior_segments': original_prior,
        'contract': selected_contract.version if selected_contract is not None else 'legacy',
        'contract_schema': selected_contract.schema if selected_contract is not None else None,
        'system_hash': sha256(RESEARCH_SYSTEM_PROMPT.encode('utf-8')).hexdigest(), 'capacity_key': capacity_key},
        ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str).encode('utf-8')).hexdigest()
    split_item = None
    patch_item = None
    if repair is not None and repair.get('version') == 'generation-patch-v1':
        if repair.get('binding') != binding or not isinstance(repair.get('pending'), list) or len(repair['pending']) != 1:
            raise ValueError('Invalid patch continuation binding.')
        patch_item = repair['pending'][0]
    if repair is not None and repair.get('version') == 'generation-split-v1':
        if repair.get('binding') != binding or not isinstance(repair.get('pending'), list) or not repair['pending']:
            raise ValueError('Invalid split continuation binding.')
        split_item = repair['pending'][0]
        if not isinstance(split_item, dict) or not isinstance(split_item.get('allowed_units'), list) or not split_item['allowed_units']:
            raise ValueError('Invalid split scope.')
        focus = None
        expected_page_binding = None
    overhead_fn = getattr(provider, 'contract_overhead_bytes', None)
    contract_error = None
    try:
        contract_overhead = overhead_fn(selected_contract) if selected_contract is not None and callable(overhead_fn) else 0
        if type(contract_overhead) is not int or contract_overhead < 0:
            raise ProviderFailure('provider_configuration_invalid', 'Provider contract configuration is invalid.', retryable=False)
    except ProviderFailure as error:
        contract_error, contract_overhead = error, 0

    def generate_once(prompt: str) -> tuple[dict[str, Any], int | None]:
        nonlocal prompt_text, generation_tokens, binding
        if repair is not None and split_item is None and patch_item is None:
            if not validate_continuation(repair, binding):
                raise ProviderFailure('repair_binding_mismatch', 'The repair input or continuation changed.', retryable=False)
            data = json.loads(prompt)
            data['repair_instruction'] = {
                'errors': repair['pending'][0]['errors'],
                'instruction': 'Return a complete response using the supplied versioned schema and all collections.'}
            prompt = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        if patch_item is not None:
            data = json.loads(prompt)
            data['repair_instruction'] = {'targets': patch_item['targets'],
                'dependencies': [{'collection': d['collection'], 'slot_id': d['slot_id'], 'dto': d['dto']} for d in patch_item['dependencies']],
                'instruction': 'Return only target objects with their assigned IDs. Read-only dependencies are supplied for reference; do not return them. Keep every envelope collection.'}
            prompt = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        if contract_error is not None:
            raise contract_error
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
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 600 or 'timeout_seconds' not in parameters:
            raise ProviderFailure('provider_budget_unknown', 'Provider limits cannot be enforced.', retryable=False)
        legacy_limit = None if callable(getattr(provider, 'research_prompt_fits', None)) else input_limit
        if legacy_limit is not None and not research_prompt_fits(prompt, RESEARCH_SYSTEM_PROMPT, legacy_limit - contract_overhead):
            raise ProviderFailure('provider_budget_exhausted', 'Research input budget is exhausted.', retryable=False)
        token_reservation, output_cap = context.prepare_model_call(provider, prompt, RESEARCH_SYSTEM_PROMPT, selected_contract)
        output_kwargs = context.output_kwargs(generate, provider, output_cap)
        reason = context.model_exhaustion(token_reservation)
        if reason:
            raise ProviderFailure('provider_budget_unknown' if reason == 'provider_limit' else 'provider_budget_exhausted',
                                  'Model execution budget is unavailable or exhausted.', retryable=False)
        remaining = context.budget.deadline - context.now()
        if remaining <= 0:
            raise ProviderFailure('provider_budget_exhausted', 'Research time budget is exhausted.', retryable=False)
        before = context.start_model(call_id, token_reservation)
        context.diagnostic_context['source_range_fingerprint'] = sha256(json.dumps(attempted_prompt.get('segments', []), sort_keys=True, separators=(',', ':'), default=str).encode('utf-8')).hexdigest()
        if repair is not None:
            context.diagnostic_context['parent_call_id'] = repair.get('parent_call_id')
        else:
            context.diagnostic_context.pop('parent_call_id', None)
        prompt_text = prompt
        attempted_prompt.update({'prompt_sha256': sha256(prompt.encode('utf-8')).hexdigest(),
                                 'prompt_characters': len(prompt), 'prompt_bytes': len(prompt.encode('utf-8'))})
        try:
            contract_kwargs = {'contract': selected_contract} if selected_contract is not None else {}
            payload, reported = context.invoke_model(call_id, generate, prompt, cancel_event=context.cancel_event(), system=RESEARCH_SYSTEM_PROMPT,
                                         timeout_seconds=min(float(timeout), remaining), **contract_kwargs, **output_kwargs)
            generation_tokens = reported if type(reported) is int and reported >= 0 else None
            if patch_item is not None:
                payload = inject_slot_dependencies(payload, patch_item['targets'], patch_item['dependencies'])
                if payload is None:
                    error = InvalidAnalysis('Model output did not match assigned repair slots.')
                    error.diagnostic = {'category': 'contract_validation', 'errors': [{'type': 'slot_mismatch'}]}
                    raise error
            return payload, generation_tokens
        except ProviderFailure as error:
            generation_tokens = (error.reported_tokens if getattr(error, 'provider_response_received', False) is True
                                 and type(error.reported_tokens) is int and error.reported_tokens >= 0 else None)
            raise
        finally:
            context.finish_model(call_id, generation_tokens, before)

    max_input_tokens = getattr(getattr(provider, 'policy', None), 'max_input_tokens', None)
    capacity_fits = getattr(provider, 'research_prompt_fits', None)
    selection_limit = None if callable(capacity_fits) else max_input_tokens
    max_prompt_chars = research_prompt_budget(selection_limit - contract_overhead if type(selection_limit) is int else None, RESEARCH_SYSTEM_PROMPT)
    fit_callback = (lambda prompt: capacity_fits(prompt, RESEARCH_SYSTEM_PROMPT, selected_contract)) if callable(capacity_fits) else None
    try:
        analysis = analyze(question, sources, as_of, generate_once, max_prompt_chars=max_prompt_chars,
                           should_stop=lambda: context.now() >= context.budget.deadline,
                           on_prepare=lambda segments: attempted_prompt.update({'segments': segments}),
                           focus=focus, scope=scope, planning_steps=planning_steps,
                           require_counterarguments=require_counterarguments, retrieval_hints=retrieval_hints,
                           planning_requirements=planning_requirements, output_contract=selected_contract, prompt_fits=fit_callback,
                           candidate_cursor=candidate_cursor, expected_page_binding=expected_page_binding,
                           prior_segments=original_prior,
                           capacity_key=capacity_key,
                           on_page_prepare=lambda page, ranges: attempted_prompt.update(work_page=page, unprocessed_ranges=ranges),
                           allowed_units=split_item['allowed_units'] if split_item is not None else None)
    except (AnalysisInterrupted, ProviderFailure, InvalidAnalysis) as error:
        if isinstance(error, InvalidAnalysis):
            context.mark_model_processing_error(call_id, 'contract_validation')
            context.record_validation_diagnostic(call_id, error.diagnostic)
        unavailable_reason = ('provider_budget_exhausted' if isinstance(error, AnalysisInterrupted) else
                              error.code if isinstance(error, ProviderFailure) else 'invalid_model_output')
        if isinstance(error, InvalidAnalysis):
            error_detail = str(error)
        selected_ids = list(dict.fromkeys(segment['source_id'] for segment in attempted_prompt.get('segments', [])))
        _, source_notes = prepare_sources(sources, as_of)
        if selected_ids:
            source_notes.append('Sources were selected for analysis, but the run did not produce valid research; check the run status and model-call record.')
        structure_failed = isinstance(error, InvalidAnalysis) or (
            isinstance(error, ProviderFailure) and error.code == 'invalid_provider_output'
            and getattr(error, 'provider_response_received', False) is True)
        analysis = AnalysisResult([], [], [*source_notes, 'Research analysis could not be completed: ' + unavailable_reason],
                                  selected_ids, context.usage,
                                  structured_output_status='failed' if structure_failed else 'not_validated',
                                  output_contract_version=selected_contract.version if selected_contract is not None else None,
                                  **attempted_prompt)
    context.ensure_running()
    continuation = None
    receipt = context.model_call_receipts.get(call_id, {})
    if patch_item is not None:
        targets = patch_item.get('targets', [])
        validated_targets = all(isinstance(item, dict) and item.get('slot_id') in analysis.structured_output_ids.get(item.get('collection'), {}) for item in targets)
        complete = (prompt_text is not None and receipt.get('provider') == 'returned' and unavailable_reason is None
                    and not analysis.structured_output_errors and bool(targets) and validated_targets)
        if not complete and unavailable_reason is None:
            unavailable_reason = 'repair_scope_incomplete'
        failed_scope = ([{'kind': 'slots', 'targets': targets}] if not complete else [])
        analysis = replace(analysis, structured_output_status='partial',
                           work_page={**analysis.work_page, **({'repair_unprocessed': failed_scope} if failed_scope else {})},
                           limitations=[*analysis.limitations, 'Repaired candidates require the original task completeness checks.'])
    elif split_item is not None:
        requested_ranges = {(entry['source_id'], entry['character_start'], entry['character_end']) for entry in split_item['allowed_units']}
        processed_ranges = {(entry.get('source_id'), entry.get('character_start'), entry.get('character_end')) for entry in analysis.segments}
        complete = (prompt_text is not None and receipt.get('provider') == 'returned' and unavailable_reason is None
                    and not analysis.structured_output_errors and requested_ranges <= processed_ranges)
        if not complete and unavailable_reason is None:
            unavailable_reason = 'repair_scope_incomplete'
        remaining_scope = repair['pending'][1:] if complete else repair['pending']
        analysis = replace(analysis, work_page={**repair.get('work_page', {}), **({'repair_unprocessed': remaining_scope} if remaining_scope else {})},
                           structured_output_status='partial',
                           limitations=[*analysis.limitations, 'Split analysis requires the original task completeness checks.'])
        if complete and len(repair['pending']) > 1:
            continuation = {**repair, 'parent_call_id': call_id, 'pending': repair['pending'][1:]}
        elif unavailable_reason == 'provider_output_truncated' and receipt.get('provider') == 'returned':
            halves = split_complete_segments(attempted_prompt.get('segments', []))
            if halves and all(len(part) < len(split_item['allowed_units']) for part in halves):
                continuation = {**repair, 'parent_call_id': call_id, 'pending': [
                    *[{'kind': 'evidence_units', 'allowed_units': part} for part in halves], *repair['pending'][1:]]}
    elif unavailable_reason == 'provider_output_truncated' and receipt.get('provider') == 'returned':
        halves = split_complete_segments(attempted_prompt.get('segments', []))
        if halves:
            continuation = {'version': 'generation-split-v1', 'binding': binding, 'parent_call_id': call_id,
                            'pending': [{'kind': 'evidence_units', 'allowed_units': part} for part in halves],
                            'work_page': analysis.work_page, 'prior_segments': original_prior}
    if (repair is None or repair.get('version') == 'generation-repair-v1') and analysis.structured_output_status == 'partial' and receipt.get('provider') == 'returned':
        report = analysis.structured_output_ids.get('slot_report', {})
        rejected = set(report.get('rejected', []))
        slots = {item['slot_id']: item for item in report.get('slots', [])}
        edges = report.get('dependencies', {})
        needed = set()
        todo = list(rejected)
        while todo:
            current = todo.pop()
            for dependency in edges.get(current, []):
                if dependency not in rejected and dependency not in needed:
                    needed.add(dependency); todo.append(dependency)
        if rejected and all(sid in analysis.repair_private for sid in needed):
            targets = [{'collection': slots[sid]['collection'], 'slot_id': sid} for sid in sorted(rejected) if sid in slots]
            dependencies = [analysis.repair_private[sid] for sid in sorted(needed)]
            if len(targets) == len(rejected):
                continuation = {'version': 'generation-patch-v1', 'binding': binding, 'parent_call_id': call_id,
                    'pending': [{'kind': 'slots', 'targets': targets, 'dependencies': dependencies}],
                    'progress': fingerprint(sorted(rejected)), 'prior_segments': original_prior,
                    'seen': list(repair.get('seen', [])) if isinstance(repair, dict) else []}
    if split_item is None and patch_item is None and continuation is None and binding and receipt.get('provider') == 'returned':
        error_kind = ('contract_validation' if unavailable_reason == 'invalid_model_output' else
                      'provider_parse' if unavailable_reason == 'invalid_provider_output' else None)
        if error_kind:
            diagnostic_errors = context.private_diagnostics.get(call_id, {}).get('errors', [])
            continuation = make_continuation(binding=binding, parent_call_id=call_id, error=error_kind,
                                             previous=repair, diagnostics=diagnostic_errors,
                                             progress=sorted(analysis.structured_output_ids.keys()))
            if continuation is not None:
                continuation['prior_segments'] = original_prior
    analysis = replace(analysis, repair_private={})
    return GenerationResult(analysis, context.calls, context.usage, generation_tokens, prompt_text, max_prompt_chars,
                            unavailable_reason, error_detail, continuation)


def has_usable_research_counterpoint(claims: list[dict[str, Any]], facts: list[dict[str, Any]],
                                     calculations: list[dict[str, Any]]) -> bool:
    """Use the same evidence and numeric-discrepancy rule for selection and execution."""
    from .research_calculations import claim_calculation_differences
    from .research_evidence import claim_amount_differences
    from .research_runs import has_usable_counterpoint
    differences = [*claim_calculation_differences(claims, calculations, facts),
                   *claim_amount_differences(claims, facts)]
    return has_usable_counterpoint(claims, {item['claim_index'] for item in differences})
