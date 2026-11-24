"""Chat Completions rendering for the neutral research turn contract."""
from collections.abc import Mapping
import inspect
import json
import math
from typing import Any
from .model_protocol import (
    ModelResponse,
    ModelToolCall,
    render_user_instructions,
    canonical_json_dumps,
    canonicalize_json_string,
    canonicalize_json_value,
    format_response_schema_instruction,
    merge_cancel_events,
    merge_optional_max_tokens,
    merge_optional_timeout,
)
from .phase3 import ProviderFailure
from .provider_capabilities import _strict_schema


class ChatResearchAdapter:
    def __init__(self, provider):
        self.provider = provider

    def __getattr__(self, name):
        return getattr(self.provider, name)

    @staticmethod
    def _render(turn, *, include_schema_instruction: bool = True):
        system_content = turn.system
        if include_schema_instruction:
            schema_note = format_response_schema_instruction(
                getattr(turn, 'response_schema', None),
                getattr(turn, 'response_schema_name', None),
                has_tools=bool(turn.tools),
            )
            if schema_note:
                system_content = f'{system_content}\n\n{schema_note}' if system_content else schema_note
        user_content = canonicalize_json_string(turn.user)
        messages = [{'role': 'system', 'content': system_content}, {'role': 'user', 'content': user_content}]
        for exchange in turn.history:
            messages.append(exchange.assistant.envelope)
            messages.extend(
                {
                    'role': 'tool',
                    'tool_call_id': result.tool_call_id,
                    'content': canonicalize_json_string(result.content),
                }
                for result in exchange.results
            )
            followup = getattr(exchange, 'user_followup', None)
            if isinstance(followup, str) and followup.strip():
                messages.append({'role': 'user', 'content': followup})
        raw_tail = getattr(turn, 'tail_note', None)
        tail_note = raw_tail if isinstance(raw_tail, str) and raw_tail.strip() else None
        if tail_note is not None:
            if turn.history:
                messages.append({'role': 'user', 'content': tail_note})
            else:
                messages[-1] = {'role': 'user', 'content': f'{user_content}\n\n{tail_note}'}
        for instruction in render_user_instructions(turn):
            messages.append({'role': 'user', 'content': instruction})
        tools = [
            {
                'type': 'function',
                'function': {
                    'name': tool.name,
                    'description': tool.description,
                    'parameters': canonicalize_json_value(tool.input_schema),
                },
            }
            for tool in turn.tools
        ]
        return messages, tools

    def _resolve_response_format(self, turn) -> tuple[dict[str, Any] | None, bool]:
        response_schema = getattr(turn, 'response_schema', None)
        response_schema_name = getattr(turn, 'response_schema_name', None)
        has_schema_request = response_schema is not None or (
            isinstance(response_schema_name, str) and bool(response_schema_name.strip())
        )
        if not has_schema_request:
            return None, False
        if turn.tools:
            return None, True
        schema_name = (
            response_schema_name.strip()
            if isinstance(response_schema_name, str) and response_schema_name.strip()
            else 'response'
        )
        cap_fn = getattr(self.provider, 'research_capacity', None)
        profile = cap_fn() if callable(cap_fn) else None
        if profile is not None and getattr(profile, 'mode', None) == 'json_schema' and isinstance(response_schema, Mapping):
            try:
                strict_schema = _strict_schema(dict(response_schema))
                return {
                    'type': 'json_schema',
                    'json_schema': {'name': schema_name, 'strict': True, 'schema': strict_schema},
                }, False
            except ProviderFailure:
                pass
        return {'type': 'json_object'}, True

    def estimate_turn_input(self, turn):
        wire_rf, include_note = self._resolve_response_format(turn)
        messages, tools = self._render(turn, include_schema_instruction=include_note)
        estimator = getattr(self.provider, 'estimate_native_input', None)
        if callable(estimator):
            base = estimator(messages, tools)
            if wire_rf is not None:
                return base + len(canonical_json_dumps({'response_format': wire_rf}).encode('utf-8'))
            return base
        wire_obj: dict[str, Any] = {'messages': messages, 'tools': tools}
        if wire_rf is not None:
            wire_obj['response_format'] = wire_rf
        return len(canonical_json_dumps(wire_obj).encode('utf-8')) + 64

    def complete_turn(self, turn, *, cancel_event=None, timeout_seconds=None, diagnostic_callback=None,
                      max_output_tokens=None, on_submission=None, on_stream_event=None):
        wire_rf, include_note = self._resolve_response_format(turn)
        messages, tools = self._render(turn, include_schema_instruction=include_note)
        effective_cancel = merge_cancel_events(cancel_event, getattr(turn, 'cancel_event', None))
        effective_timeout = merge_optional_timeout(timeout_seconds, getattr(turn, 'timeout_seconds', None))
        effective_max_output = merge_optional_max_tokens(max_output_tokens, getattr(turn, 'max_output_tokens', None))
        try:
            parameters = inspect.signature(self.provider.complete).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
        kwargs: dict[str, Any] = {}
        if accepts_kwargs or 'cancel_event' in parameters:
            kwargs['cancel_event'] = effective_cancel
        elif effective_cancel is not None and effective_cancel.is_set():
            raise ProviderFailure('provider_cancelled', 'Provider request was cancelled.', retryable=True)
        if effective_timeout is not None:
            if accepts_kwargs or 'timeout_seconds' in parameters:
                kwargs['timeout_seconds'] = effective_timeout
            elif (
                isinstance(effective_timeout, bool)
                or not isinstance(effective_timeout, (int, float))
                or not math.isfinite(effective_timeout)
                or effective_timeout <= 0
            ):
                raise ProviderFailure('provider_budget_exhausted', 'Provider time budget is exhausted.', retryable=False)
        if effective_max_output is not None:
            if accepts_kwargs or 'max_output_tokens' in parameters:
                kwargs['max_output_tokens'] = effective_max_output
            elif type(effective_max_output) is not int or effective_max_output < 1:
                raise ProviderFailure('provider_budget_exhausted', 'Research output budget is exhausted.', retryable=False)
        if wire_rf is not None and (accepts_kwargs or 'response_format' in parameters):
            kwargs['response_format'] = wire_rf
        for name, value in (('diagnostic_callback', diagnostic_callback), ('on_submission', on_submission),
                            ('on_stream_event', on_stream_event)):
            if value is not None and (accepts_kwargs or name in parameters):
                kwargs[name] = value
        response = self.provider.complete(messages, tools, **kwargs)
        calls = []
        for call in response.tool_calls:
            raw = call.arguments
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(args, dict):
                    args = None
            except (TypeError, ValueError):
                args = None
            calls.append(ModelToolCall(call.id, call.name, args, raw if isinstance(raw, str) else None))
        reason = response.finish_reason
        stop = 'tool_calls' if reason in ('tool_calls', 'function_call') else 'truncated' if reason in ('length', 'max_tokens') else 'end' if reason in ('stop', 'end_turn') else 'other'
        return ModelResponse(response.message, tuple(calls), response.usage, reason, stop)
