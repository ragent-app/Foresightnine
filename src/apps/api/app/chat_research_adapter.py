"""Chat Completions rendering for the neutral research turn contract."""
import inspect
import json
from .model_protocol import (
    ModelResponse,
    ModelToolCall,
    render_user_instructions,
    canonical_json_dumps,
    canonicalize_json_string,
    canonicalize_json_value,
)


class ChatResearchAdapter:
    def __init__(self, provider):
        self.provider = provider

    def __getattr__(self, name):
        return getattr(self.provider, name)

    @staticmethod
    def _render(turn):
        user_content = canonicalize_json_string(turn.user)
        messages = [{'role': 'system', 'content': turn.system}, {'role': 'user', 'content': user_content}]
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

    def estimate_turn_input(self, turn):
        messages, tools = self._render(turn)
        estimator = getattr(self.provider, 'estimate_native_input', None)
        if callable(estimator):
            return estimator(messages, tools)
        return len(canonical_json_dumps({'messages': messages, 'tools': tools}).encode('utf-8')) + 64

    def complete_turn(self, turn, *, cancel_event=None, timeout_seconds=None, diagnostic_callback=None,
                      max_output_tokens=None, on_submission=None, on_stream_event=None):
        messages, tools = self._render(turn)
        kwargs = {'cancel_event': cancel_event}
        if timeout_seconds is not None:
            kwargs['timeout_seconds'] = timeout_seconds
        if max_output_tokens is not None:
            kwargs['max_output_tokens'] = max_output_tokens
        try:
            parameters = inspect.signature(self.provider.complete).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
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
