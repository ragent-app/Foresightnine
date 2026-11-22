"""Anthropic Messages wire protocol for research turns."""
from __future__ import annotations
import json
import logging
import socket
from time import monotonic
from typing import Any, Callable
from urllib.parse import urlsplit
from .model_protocol import (
    ModelResponse,
    ModelToolCall,
    ModelUsage,
    canonical_json_dumps,
    canonicalize_json_value,
)
from .openai_compatible import (
    _MAX_RESPONSE_BYTES,
    _SseLineParser,
    _TransportFailure,
    _TransportTimeout,
    _build_transport_failure,
    _default_stream_transport,
    _default_transport,
    _invoke_stream_transport,
    _transport_timeout_from_environment,
)
from .phase3 import ProviderFailure
from .research_stream_extract import TopLevelReportExtractor
from .transport_diagnostics import TransportDiagnostics

_logger = logging.getLogger(__name__)


def _endpoint(base):
    base = base.rstrip('/')
    path = urlsplit(base).path.rstrip('/')
    if path.endswith('/v1/messages'):
        return base
    return base + ('/messages' if path.endswith('/v1') else '/v1/messages')


class AnthropicMessagesProvider:
    name = 'anthropic_messages'
    def __init__(self, settings, policy, *, anthropic_version, auth_scheme, max_tokens, transport=None, stream_transport=None, stream_enabled=False):
        if not isinstance(anthropic_version, str) or not anthropic_version or any(c in anthropic_version for c in '\r\n') or auth_scheme not in ('x-api-key', 'bearer') or type(max_tokens) is not int or max_tokens <= 0 or type(stream_enabled) is not bool:
            raise ProviderFailure('provider_configuration_invalid', 'Messages configuration is invalid.', retryable=False)
        self.settings, self.policy = settings, policy
        self.anthropic_version, self.auth_scheme, self.max_tokens = anthropic_version, auth_scheme, max_tokens
        self.transport = transport
        self.stream_transport = stream_transport
        self.stream_enabled = stream_enabled
        from .research_diagnostics import warn_if_timeout_output_mismatch
        warn_if_timeout_output_mismatch(
            self.max_tokens,
            getattr(policy, 'timeout_seconds', None),
            streaming=stream_enabled,
            logger=_logger,
        )

    def research_capacity(self):
        from .provider_capabilities import configured_profile
        return configured_profile(_endpoint(self.settings.base_url), self.settings.model)

    def research_capacity_key(self):
        from hashlib import sha256
        from dataclasses import asdict
        profile = self.research_capacity()
        binding = {'endpoint': _endpoint(self.settings.base_url), 'model': self.settings.model,
                   'version': self.anthropic_version, 'auth': self.auth_scheme,
                   'max_tokens': self.max_tokens, 'stream_enabled': self.stream_enabled,
                   'profile': asdict(profile) if profile else None,
                   'input_budget': self.policy.max_input_tokens}
        return sha256(json.dumps(binding, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    @staticmethod
    def _usage_from_dict(u):
        if not isinstance(u, dict):
            return None
        def count(key):
            v = u.get(key)
            return v if type(v) is int and v >= 0 else None
        inp, out = count('input_tokens'), count('output_tokens')
        creation, read = count('cache_creation_input_tokens'), count('cache_read_input_tokens')
        if creation is None and 'cache_creation_input_tokens' not in u and isinstance(u.get('cache_creation'), dict):
            nested = u['cache_creation']
            if nested and all(type(v) is int and v >= 0 for v in nested.values()):
                creation = sum(nested.values())
        total = count('total_tokens')
        if total is None and inp is not None and out is not None:
            cache_keys = ('cache_creation_input_tokens', 'cache_read_input_tokens')
            if all(u.get(key) is None or count(key) is not None for key in cache_keys):
                total = inp + out + (creation or 0) + (read or 0)
        return ModelUsage(inp, out, total, creation, read)

    @classmethod
    def _usage(cls, body):
        try:
            obj = json.loads(body)
            return cls._usage_from_dict(obj.get('usage'))
        except (ValueError, TypeError, AttributeError):
            return None

    @staticmethod
    def _build_response(blocks, reason, usage):
        if not isinstance(blocks, list) or not blocks:
            raise ValueError()
        if reason == 'max_tokens':
            raise ProviderFailure('provider_output_truncated', 'Provider output was truncated.', retryable=False)
        if reason == 'refusal':
            raise ProviderFailure('provider_output_refused', 'Provider declined the request.', retryable=False)
        if reason == 'model_context_window_exceeded':
            raise ProviderFailure('provider_context_limit', 'Provider context limit was reached.', retryable=False)
        if reason not in ('end_turn', 'tool_use', 'stop_sequence'):
            raise ValueError()
        calls = []
        safe_blocks = []
        for block in blocks:
            kind = block['type']
            if kind == 'text':
                if not isinstance(block.get('text'), str):
                    raise ValueError()
                safe_blocks.append({'type': kind, 'text': block['text']})
            elif kind == 'tool_use':
                if not all(isinstance(block.get(k), str) and block[k] for k in ('id', 'name')) or not isinstance(block.get('input'), dict):
                    raise ValueError()
                calls.append(ModelToolCall(block['id'], block['name'], block['input']))
                safe_blocks.append({'type': kind, 'id': block['id'], 'name': block['name'], 'input': block['input']})
            elif kind == 'thinking':
                if not isinstance(block.get('thinking'), str) or not isinstance(block.get('signature'), str):
                    raise ValueError()
                safe_blocks.append({'type': kind, 'thinking': block['thinking'], 'signature': block['signature']})
            elif kind == 'redacted_thinking':
                if not isinstance(block.get('data'), str):
                    raise ValueError()
                safe_blocks.append({'type': kind, 'data': block['data']})
            else:
                raise ValueError()
        if len({c.id for c in calls}) != len(calls) or (reason == 'tool_use') != bool(calls):
            raise ValueError()
        return ModelResponse({'role': 'assistant', 'content': safe_blocks}, tuple(calls), usage, reason, 'tool_calls' if calls else 'end')

    @staticmethod
    def _mark_message_cache_control(msg: dict[str, Any]) -> dict[str, Any] | None:
        """Return a copy of ``msg`` with cache_control on its last cacheable block, or None."""
        content = msg.get('content')
        if isinstance(content, list) and content:
            for block_idx in range(len(content) - 1, -1, -1):
                block = content[block_idx]
                if not isinstance(block, dict):
                    continue
                if block.get('type') in ('thinking', 'redacted_thinking'):
                    continue
                copied_content = list(content)
                copied_content[block_idx] = {**block, 'cache_control': {'type': 'ephemeral'}}
                return {**msg, 'content': copied_content}
            return None
        if isinstance(content, str) and content:
            return {
                **msg,
                'content': [{'type': 'text', 'text': content, 'cache_control': {'type': 'ephemeral'}}],
            }
        return None

    @classmethod
    def _mark_last_prefix_cache_control(
        cls,
        messages: list[dict[str, Any]],
        *,
        max_breakpoints: int = 2,
    ) -> None:
        """Mark the last stable content block and up to one earlier rolling user boundary."""
        if max_breakpoints <= 0 or not messages:
            return
        last_idx: int | None = None
        for idx in range(len(messages) - 1, -1, -1):
            updated = cls._mark_message_cache_control(messages[idx])
            if updated is not None:
                messages[idx] = updated
                last_idx = idx
                break
        if last_idx is None or max_breakpoints < 2:
            return
        for idx in range(last_idx - 1, 0, -1):
            if messages[idx].get('role') != 'user':
                continue
            updated = cls._mark_message_cache_control(messages[idx])
            if updated is not None:
                messages[idx] = updated
                return

    def _wire(self, turn, cap=None, *, profile=None):
        effective_profile = profile if profile is not None else self.research_capacity()
        explicit_cache = bool(
            effective_profile is not None and getattr(effective_profile, 'explicit_cache_control', False) is True
        )
        raw_tail = getattr(turn, 'tail_note', None)
        tail_note = raw_tail if isinstance(raw_tail, str) and raw_tail.strip() else None

        if turn.history:
            messages: list[dict[str, Any]] = [{'role': 'user', 'content': turn.user}]
            for exchange in turn.history:
                messages.append(exchange.assistant.envelope)
                results_content: list[dict[str, Any]] = [
                    {'type': 'tool_result', 'tool_use_id': r.tool_call_id, 'content': r.content, 'is_error': r.is_error}
                    for r in exchange.results
                ]
                messages.append({'role': 'user', 'content': results_content})
            if explicit_cache:
                used_breakpoints = (1 if turn.system else 0) + (1 if turn.tools else 0)
                self._mark_last_prefix_cache_control(messages, max_breakpoints=max(0, min(2, 4 - used_breakpoints)))
            if tail_note is not None:
                last_content = messages[-1]['content']
                if isinstance(last_content, list):
                    messages[-1] = {**messages[-1], 'content': [*last_content, {'type': 'text', 'text': tail_note}]}
        else:
            if explicit_cache:
                user_blocks: list[dict[str, Any]] = [
                    {'type': 'text', 'text': turn.user, 'cache_control': {'type': 'ephemeral'}}
                ]
                if tail_note is not None:
                    user_blocks.append({'type': 'text', 'text': tail_note})
                messages = [{'role': 'user', 'content': user_blocks}]
            elif tail_note is not None:
                messages = [{'role': 'user', 'content': [{'type': 'text', 'text': turn.user}, {'type': 'text', 'text': tail_note}]}]
            else:
                messages = [{'role': 'user', 'content': turn.user}]

        system_payload: Any = (
            [{'type': 'text', 'text': turn.system, 'cache_control': {'type': 'ephemeral'}}]
            if explicit_cache and turn.system
            else turn.system
        )
        wire: dict[str, Any] = {
            'model': self.settings.model,
            'system': system_payload,
            'messages': messages,
            'max_tokens': min(self.max_tokens, cap) if cap is not None else self.max_tokens,
        }
        if turn.tools:
            wire_tools = [
                {
                    'name': t.name,
                    'description': t.description,
                    'input_schema': canonicalize_json_value(t.input_schema),
                }
                for t in turn.tools
            ]
            if explicit_cache:
                wire_tools[-1]['cache_control'] = {'type': 'ephemeral'}
            wire['tools'] = wire_tools
        return wire

    def estimate_turn_input_dual(self, turn):
        from .provider_capabilities import estimate_dual_input
        serialized = canonical_json_dumps(self._wire(turn))
        return estimate_dual_input(serialized, envelope_bytes=64, envelope_chars=0)

    def estimate_turn_input(self, turn):
        return self.estimate_turn_input_dual(turn).utf8_byte_upper_bound

    def complete_turn(self, turn, *, cancel_event=None, timeout_seconds=None, diagnostic_callback=None,
                      max_output_tokens=None, on_submission=None,
                      on_stream_event: Callable[[dict[str, Any]], None] | None = None,
                      stream_transport=None):
        from .provider_capabilities import resolve_output_token_limit
        from .research_diagnostics import (
            normalize_finish_reason,
            report_diagnostic_write_failure,
            safe_messages_error,
            safe_provider_error,
            safe_stop_reason,
            safe_usage_diagnostics,
            safe_wire_metadata,
            sanitize_stream_metrics,
            set_finish_reason,
            warn_if_timeout_output_mismatch,
        )
        set_finish_reason(None)
        call_started_at = monotonic()
        diagnostics = TransportDiagnostics()
        response = None
        wire = None
        wire_bytes: int | None = None
        streaming = False
        transport_entered = False
        _UNSET = object()
        observed_stop_reason: Any = _UNSET
        observed_usage = None
        diagnostic_emitted = False

        saw_message_start = False
        saw_message_stop = False
        saw_message_delta_usage = False
        stream_role: str | None = None
        stream_stop_reason: str | None = None
        usage_raw: dict[str, Any] = {}
        block_acc: dict[int, dict[str, Any]] = {}
        reassembled_bytes = 0
        block_bytes_by_type: dict[str, int] = {
            'thinking': 0,
            'text': 0,
            'tool_use': 0,
            'redacted_thinking': 0,
        }
        active_tool_names: list[str] = []
        frames_received = 0
        ttft_ms: int | None = None
        last_frame_elapsed_ms: int | None = None
        allowed_tool_names = frozenset(
            t.name for t in getattr(turn, 'tools', ()) if isinstance(getattr(t, 'name', None), str) and t.name
        )

        def usable_stream_usage() -> ModelUsage | None:
            if not usage_raw:
                return None
            usage_obj = self._usage_from_dict(usage_raw)
            if usage_obj is None:
                return None
            if saw_message_delta_usage or any(
                type(val) is int and val > 0
                for val in (
                    usage_obj.input_tokens,
                    usage_obj.output_tokens,
                    usage_obj.total_tokens,
                    usage_obj.cache_creation_input_tokens,
                    usage_obj.cache_read_input_tokens,
                )
            ):
                return usage_obj
            return None

        def partial_reported_tokens() -> int | None:
            usage_obj = usable_stream_usage()
            return usage_obj.total_tokens if usage_obj is not None else None

        def emit_diagnostics() -> None:
            nonlocal diagnostic_emitted
            if diagnostic_emitted:
                return
            diagnostic_emitted = True
            if not diagnostic_callback:
                return
            try:
                observation = diagnostics.snapshot()
                observation['elapsed_ms'] = max(0, int((monotonic() - call_started_at) * 1000))
                if streaming:
                    observation['streamed_bytes'] = reassembled_bytes
                if (streaming and transport_entered) or frames_received > 0:
                    observation['stream_metrics'] = sanitize_stream_metrics(
                        {
                            'frames_received': frames_received,
                            'ttft_ms': ttft_ms,
                            'last_frame_elapsed_ms': last_frame_elapsed_ms,
                            'reassembled_bytes': reassembled_bytes,
                            'block_bytes_by_type': dict(block_bytes_by_type),
                            'active_tool_names': list(active_tool_names),
                        },
                        allowed_tool_names=allowed_tool_names or None,
                    )
                effective_stop_reason = (
                    observed_stop_reason
                    if observed_stop_reason is not _UNSET
                    else (stream_stop_reason if stream_stop_reason is not None else _UNSET)
                )
                effective_usage = observed_usage if observed_usage is not None else usable_stream_usage()
                if wire is not None:
                    meta = safe_wire_metadata(wire, wire_bytes=wire_bytes)
                    meta['provider'] = 'anthropic_messages'
                    observation['request_metadata'] = meta
                    if type(wire.get('max_tokens')) is int:
                        observation['max_tokens'] = wire['max_tokens']
                if type(wire_bytes) is int and wire_bytes >= 0:
                    observation['wire_bytes'] = wire_bytes
                if effective_stop_reason is not _UNSET:
                    observation['stop_reason'] = safe_stop_reason(effective_stop_reason)
                    observation['finish_reason'] = normalize_finish_reason(effective_stop_reason)
                safe_usage = safe_usage_diagnostics(effective_usage)
                if safe_usage:
                    observation['usage'] = safe_usage
                if response is not None:
                    details = response.safe_error or (safe_provider_error(response.body) if not 200 <= response.status_code < 300 else {})
                    observation['provider_error'] = safe_messages_error(details, response.status_code, dict(response.headers))
                diagnostic_callback(observation)
            except Exception as error:
                report_diagnostic_write_failure(error)

        def emit_stream_safe(payload: dict[str, Any]) -> None:
            if on_stream_event is not None:
                on_stream_event(payload)

        def add_reassembled_bytes(text_chunk: str, block_type: str | None = None) -> None:
            nonlocal reassembled_bytes
            chunk_bytes = len(text_chunk.encode('utf-8'))
            if reassembled_bytes + chunk_bytes > _MAX_RESPONSE_BYTES:
                raise ValueError()
            reassembled_bytes += chunk_bytes
            if block_type in block_bytes_by_type:
                block_bytes_by_type[block_type] += chunk_bytes

        def handle_sse_frame(event_name: str, data: str) -> None:
            nonlocal saw_message_start, saw_message_stop, saw_message_delta_usage
            nonlocal stream_role, stream_stop_reason, frames_received, ttft_ms, last_frame_elapsed_ms
            diagnostics.submission('response_received')
            frames_received += 1
            now_ms = max(0, int((monotonic() - call_started_at) * 1000))
            last_frame_elapsed_ms = now_ms
            stripped = data.strip()
            if event_name == 'ping':
                return
            if event_name == 'error':
                raise ProviderFailure('provider_unavailable', 'Provider stream returned an error.', retryable=False)
            if not stripped:
                return
            payload = json.loads(stripped)
            if not isinstance(payload, dict):
                raise ValueError()
            ev_type = payload.get('type') or event_name
            if ev_type == 'ping':
                return
            if ev_type == 'error':
                raise ProviderFailure('provider_unavailable', 'Provider stream returned an error.', retryable=False)
            if ev_type == 'message_start':
                msg = payload.get('message')
                if not isinstance(msg, dict) or msg.get('role') != 'assistant':
                    raise ValueError()
                saw_message_start = True
                stream_role = 'assistant'
                if isinstance(msg.get('stop_reason'), str):
                    stream_stop_reason = msg['stop_reason']
                if isinstance(msg.get('usage'), dict):
                    usage_raw.update({k: v for k, v in msg['usage'].items() if v is not None})
                return
            if ev_type == 'content_block_start':
                idx = payload.get('index')
                block = payload.get('content_block')
                if type(idx) is not int or not 0 <= idx < 64 or idx in block_acc or not isinstance(block, dict):
                    raise ValueError()
                btype = block.get('type')
                if btype == 'text':
                    if ttft_ms is None:
                        ttft_ms = now_ms
                    initial = block.get('text', '')
                    if not isinstance(initial, str):
                        raise ValueError()
                    if initial:
                        add_reassembled_bytes(initial, 'text')
                        emit_stream_safe({'channel': 'text', 'delta': initial})
                    block_acc[idx] = {'type': 'text', 'parts': [initial] if initial else [], 'stopped': False}
                elif btype == 'thinking':
                    if ttft_ms is None:
                        ttft_ms = now_ms
                    init_think = block.get('thinking', '')
                    init_sig = block.get('signature', '')
                    if not isinstance(init_think, str) or not isinstance(init_sig, str):
                        raise ValueError()
                    if init_think:
                        add_reassembled_bytes(init_think, 'thinking')
                        emit_stream_safe({'channel': 'thinking', 'delta': init_think})
                    if init_sig:
                        add_reassembled_bytes(init_sig, 'thinking')
                    block_acc[idx] = {
                        'type': 'thinking',
                        'thinking_parts': [init_think] if init_think else [],
                        'signature_parts': [init_sig] if init_sig else [],
                        'stopped': False,
                    }
                elif btype == 'redacted_thinking':
                    if ttft_ms is None:
                        ttft_ms = now_ms
                    data_val = block.get('data')
                    if not isinstance(data_val, str):
                        raise ValueError()
                    add_reassembled_bytes(data_val, 'redacted_thinking')
                    block_acc[idx] = {'type': 'redacted_thinking', 'data': data_val, 'stopped': False}
                elif btype == 'tool_use':
                    if ttft_ms is None:
                        ttft_ms = now_ms
                    block_id, block_name = block.get('id'), block.get('name')
                    if not isinstance(block_id, str) or not block_id or not isinstance(block_name, str) or not block_name:
                        raise ValueError()
                    init_input = block.get('input')
                    if init_input is not None and not isinstance(init_input, dict):
                        raise ValueError()
                    if allowed_tool_names:
                        safe_tool_name = block_name if block_name in allowed_tool_names else 'unrecognized'
                    else:
                        safe_tool_name = block_name if block_name.isascii() and block_name.replace('_', '').isalnum() and block_name[0].islower() else 'unrecognized'
                    if safe_tool_name not in active_tool_names and len(active_tool_names) < 32:
                        active_tool_names.append(safe_tool_name)
                    status_msg = (
                        '正在起草研究报告并整理引文，完成后将自动核验原文位置…'
                        if block_name == 'submit_research'
                        else f'正在准备工具参数：{block_name}'
                    )
                    emit_stream_safe({'channel': 'tool_status', 'delta': status_msg})
                    block_acc[idx] = {
                        'type': 'tool_use',
                        'id': block_id,
                        'name': block_name,
                        'initial_input': init_input if isinstance(init_input, dict) else {},
                        'json_parts': [],
                        'extractor': TopLevelReportExtractor() if block_name == 'submit_research' else None,
                        'stopped': False,
                    }
                else:
                    raise ValueError()
                return
            if ev_type == 'content_block_delta':
                idx = payload.get('index')
                delta = payload.get('delta')
                if type(idx) is not int or idx not in block_acc or block_acc[idx]['stopped'] or not isinstance(delta, dict):
                    raise ValueError()
                entry = block_acc[idx]
                dtype = delta.get('type')
                if dtype == 'text_delta':
                    chunk = delta.get('text')
                    if entry['type'] != 'text' or not isinstance(chunk, str):
                        raise ValueError()
                    if ttft_ms is None:
                        ttft_ms = now_ms
                    if chunk:
                        add_reassembled_bytes(chunk, 'text')
                        entry['parts'].append(chunk)
                        emit_stream_safe({'channel': 'text', 'delta': chunk})
                elif dtype == 'thinking_delta':
                    chunk = delta.get('thinking')
                    if entry['type'] != 'thinking' or not isinstance(chunk, str):
                        raise ValueError()
                    if ttft_ms is None:
                        ttft_ms = now_ms
                    if chunk:
                        add_reassembled_bytes(chunk, 'thinking')
                        entry['thinking_parts'].append(chunk)
                        emit_stream_safe({'channel': 'thinking', 'delta': chunk})
                elif dtype == 'signature_delta':
                    chunk = delta.get('signature')
                    if entry['type'] != 'thinking' or not isinstance(chunk, str):
                        raise ValueError()
                    if ttft_ms is None:
                        ttft_ms = now_ms
                    if chunk:
                        add_reassembled_bytes(chunk, 'thinking')
                        entry['signature_parts'].append(chunk)
                elif dtype == 'input_json_delta':
                    chunk = delta.get('partial_json')
                    if entry['type'] != 'tool_use' or not isinstance(chunk, str):
                        raise ValueError()
                    if ttft_ms is None:
                        ttft_ms = now_ms
                    if chunk:
                        add_reassembled_bytes(chunk, 'tool_use')
                        entry['json_parts'].append(chunk)
                        if entry['extractor'] is not None:
                            draft_delta = entry['extractor'].feed(chunk)
                            if draft_delta:
                                emit_stream_safe({'channel': 'draft_report', 'delta': draft_delta})
                else:
                    raise ValueError()
                return
            if ev_type == 'content_block_stop':
                idx = payload.get('index')
                if type(idx) is not int or idx not in block_acc:
                    raise ValueError()
                block_acc[idx]['stopped'] = True
                return
            if ev_type == 'message_delta':
                delta = payload.get('delta')
                if isinstance(delta, dict) and isinstance(delta.get('stop_reason'), str):
                    stream_stop_reason = delta['stop_reason']
                if isinstance(payload.get('usage'), dict):
                    clean_usage = {k: v for k, v in payload['usage'].items() if v is not None}
                    if clean_usage:
                        saw_message_delta_usage = True
                        usage_raw.update(clean_usage)
                return
            if ev_type == 'message_stop':
                saw_message_stop = True
                return

        active_stream_transport = stream_transport or self.stream_transport
        streamed_sse = False
        try:
            try:
                if not self.settings.api_key:
                    raise ProviderFailure('provider_secret_missing', 'Provider secret is not configured.', retryable=False)
                if max_output_tokens is not None and (type(max_output_tokens) is not int or max_output_tokens < 1):
                    raise ProviderFailure('provider_budget_exhausted', 'Output budget is exhausted.', retryable=False)
                if cancel_event is not None and cancel_event.is_set():
                    raise ProviderFailure('provider_cancelled', 'Provider request was cancelled.', retryable=True)
                timeout = min(self.policy.timeout_seconds, timeout_seconds) if timeout_seconds is not None else self.policy.timeout_seconds
                if timeout <= 0:
                    raise ProviderFailure('provider_budget_exhausted', 'Time budget is exhausted.', retryable=False)
                profile = self.research_capacity()
                streaming = on_stream_event is not None and (self.stream_enabled or (profile is not None and profile.stream is True))
                connect_timeout = _transport_timeout_from_environment("FORESIGHTNINE_LLM_CONNECT_TIMEOUT_SECONDS", 15.0)
                response_timeout = _transport_timeout_from_environment("FORESIGHTNINE_LLM_RESPONSE_TIMEOUT_SECONDS", None)
                idle_timeout = (
                    _transport_timeout_from_environment("FORESIGHTNINE_LLM_STREAM_IDLE_TIMEOUT_SECONDS", 45.0)
                    if streaming
                    else 45.0
                )
                effective_max_tokens = resolve_output_token_limit(
                    self.max_tokens,
                    max_output_tokens,
                    getattr(self.policy, 'max_output_tokens', None),
                    getattr(profile, 'output_tokens', None),
                )
                warn_if_timeout_output_mismatch(
                    effective_max_tokens,
                    timeout,
                    streaming=streaming,
                    logger=_logger,
                )
                wire = self._wire(turn, effective_max_tokens, profile=profile)
                if streaming:
                    wire['stream'] = True
                body = canonical_json_dumps(wire).encode()
                wire_bytes = len(body)
                headers = {
                    'Accept': 'text/event-stream' if streaming else 'application/json',
                    'Content-Type': 'application/json',
                    'anthropic-version': self.anthropic_version,
                }
                headers['x-api-key' if self.auth_scheme == 'x-api-key' else 'Authorization'] = self.settings.api_key if self.auth_scheme == 'x-api-key' else 'Bearer ' + self.settings.api_key

                transport_entered = True
                if streaming:
                    if active_stream_transport is not None:
                        if on_submission:
                            on_submission()
                        diagnostics.submission('started_unknown')
                        fallback = _invoke_stream_transport(
                            active_stream_transport,
                            'POST',
                            _endpoint(self.settings.base_url),
                            headers,
                            body,
                            timeout,
                            on_frame=handle_sse_frame,
                            cancel_event=cancel_event,
                            on_submission=on_submission,
                            diagnostics=diagnostics,
                        )
                        diagnostics.submission('response_received')
                        if fallback is not None:
                            response = fallback
                        else:
                            streamed_sse = True
                    elif self.transport is None:
                        fallback = _default_stream_transport(
                            'POST',
                            _endpoint(self.settings.base_url),
                            headers,
                            body,
                            timeout,
                            on_frame=handle_sse_frame,
                            allow_private_network=self.settings.allow_private_network,
                            diagnostics=diagnostics,
                            connect_timeout_seconds=connect_timeout,
                            idle_timeout_seconds=idle_timeout,
                            response_timeout_seconds=response_timeout,
                            cancel_event=cancel_event,
                            on_submission=on_submission,
                        )
                        if fallback is not None:
                            response = fallback
                        else:
                            streamed_sse = True
                    else:
                        if on_submission:
                            on_submission()
                        diagnostics.submission('started_unknown')
                        response = self.transport('POST', _endpoint(self.settings.base_url), headers, body, timeout)
                        diagnostics.submission('response_received')
                        ct = str(getattr(response, 'headers', {}).get('content-type', '')).split(';', 1)[0].strip().lower()
                        if 200 <= response.status_code < 300 and ct == 'text/event-stream':
                            parser = _SseLineParser()
                            for item in [*parser.feed_text(response.body), *parser.finish()]:
                                if item[0] == 'frame':
                                    handle_sse_frame(item[1], item[2])
                            response = None
                            streamed_sse = True
                else:
                    if self.transport is None:
                        # Same connect/response limits as the OpenAI-compatible transport; without them the
                        # Messages path fell back to a 5 s connect timeout that slow TLS handshakes behind a
                        # proxy often exceeded (364 acceptance). Pre-send failures are retried, never resent.
                        response = _default_transport('POST', _endpoint(self.settings.base_url), headers, body, timeout, allow_private_network=self.settings.allow_private_network, diagnostics=diagnostics,
                            connect_timeout_seconds=connect_timeout,
                            response_timeout_seconds=response_timeout,
                            cancel_event=cancel_event, on_submission=on_submission)
                    else:
                        if on_submission:
                            on_submission()
                        diagnostics.submission('started_unknown')
                        response = self.transport('POST', _endpoint(self.settings.base_url), headers, body, timeout)
                        diagnostics.submission('response_received')
            except ProviderFailure as error:
                if transport_entered and diagnostics.snapshot()['submission_state'] == 'response_received':
                    error.provider_response_received = True
                    if error.reported_tokens is None:
                        error.reported_tokens = partial_reported_tokens()
                raise
            except (_TransportFailure, TimeoutError, socket.timeout) as error:
                failure = _build_transport_failure(
                    error,
                    diagnostics,
                    cancelled=bool(cancel_event and cancel_event.is_set()),
                    message='Provider request ended without a usable result.',
                    started_at=call_started_at,
                    wire_bytes=wire_bytes,
                    streamed_bytes=reassembled_bytes if streaming else None,
                )
                if failure.provider_response_received:
                    failure.reported_tokens = partial_reported_tokens()
                raise failure from None
            except (ValueError, TypeError, KeyError, AttributeError):
                if not transport_entered:
                    raise
                failure = ProviderFailure('invalid_provider_output', 'Provider returned an invalid response.', retryable=False)
                failure.provider_response_received = True
                failure.reported_tokens = partial_reported_tokens()
                raise failure from None

            if streamed_sse:
                if cancel_event and cancel_event.is_set():
                    if stream_stop_reason is not None:
                        observed_stop_reason = stream_stop_reason
                        set_finish_reason(stream_stop_reason)
                    observed_usage = usable_stream_usage()
                    failure = ProviderFailure('provider_cancelled', 'Provider result discarded.', retryable=True)
                    failure.provider_response_received = True
                    failure.reported_tokens = observed_usage.total_tokens if observed_usage else None
                    failure.timeout_kind = None
                    failure.elapsed_ms = max(0, int((monotonic() - call_started_at) * 1000))
                    failure.wire_bytes = wire_bytes
                    failure.streamed_bytes = reassembled_bytes
                    raise failure
                if not saw_message_stop:
                    if stream_stop_reason is not None:
                        observed_stop_reason = stream_stop_reason
                        set_finish_reason(stream_stop_reason)
                    observed_usage = usable_stream_usage()
                    failure = ProviderFailure('provider_transport_error', 'Provider stream ended unexpectedly.', retryable=True)
                    failure.provider_response_received = True
                    failure.reported_tokens = observed_usage.total_tokens if observed_usage else None
                    failure.timeout_kind = None
                    failure.elapsed_ms = max(0, int((monotonic() - call_started_at) * 1000))
                    failure.wire_bytes = wire_bytes
                    failure.streamed_bytes = reassembled_bytes
                    raise failure
                try:
                    observed_stop_reason = stream_stop_reason
                    observed_usage = usable_stream_usage()
                    set_finish_reason(stream_stop_reason)
                    emit_diagnostics()
                    if not saw_message_start or stream_role != 'assistant':
                        raise ValueError()
                    blocks = []
                    for idx in sorted(block_acc):
                        entry = block_acc[idx]
                        btype = entry['type']
                        if btype == 'text':
                            blocks.append({'type': 'text', 'text': ''.join(entry['parts'])})
                        elif btype == 'thinking':
                            blocks.append({'type': 'thinking', 'thinking': ''.join(entry['thinking_parts']), 'signature': ''.join(entry['signature_parts'])})
                        elif btype == 'redacted_thinking':
                            blocks.append({'type': 'redacted_thinking', 'data': entry['data']})
                        elif btype == 'tool_use':
                            if entry['json_parts']:
                                parsed_input = json.loads(''.join(entry['json_parts']))
                                if not isinstance(parsed_input, dict):
                                    raise ValueError()
                            else:
                                parsed_input = entry['initial_input']
                            blocks.append({'type': 'tool_use', 'id': entry['id'], 'name': entry['name'], 'input': parsed_input})
                    if len(json.dumps(blocks, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > _MAX_RESPONSE_BYTES:
                        raise ValueError()
                    return self._build_response(blocks, stream_stop_reason, observed_usage)
                except ProviderFailure as error:
                    error.provider_response_received = True
                    if error.reported_tokens is None:
                        error.reported_tokens = partial_reported_tokens()
                    raise
                except (ValueError, TypeError, KeyError, AttributeError):
                    failure = ProviderFailure('invalid_provider_output', 'Provider returned an invalid response.', retryable=False)
                    failure.provider_response_received = True
                    failure.reported_tokens = partial_reported_tokens()
                    raise failure from None

            assert response is not None
            if cancel_event and cancel_event.is_set():
                failure = ProviderFailure('provider_cancelled', 'Provider result discarded.', retryable=True)
                failure.provider_response_received = True
                usage = self._usage(response.body) if len(response.body.encode()) <= _MAX_RESPONSE_BYTES else None
                observed_usage = usage
                failure.reported_tokens = usage.total_tokens if usage else None
                raise failure
            if not 200 <= response.status_code < 300:
                failure = ProviderFailure('provider_request_rejected', 'Provider rejected the request.', retryable=False)
                failure.provider_response_received = True
                raise failure
            try:
                if len(response.body.encode()) > _MAX_RESPONSE_BYTES:
                    raise ValueError()
                envelope = json.loads(response.body)
                if isinstance(envelope, dict):
                    raw_reason = envelope.get('stop_reason')
                    observed_stop_reason = raw_reason if (raw_reason is None or isinstance(raw_reason, str)) else '<invalid>'
                    set_finish_reason(raw_reason)
                    observed_usage = self._usage_from_dict(envelope.get('usage'))
                    emit_diagnostics()
                blocks, reason = envelope['content'], envelope['stop_reason']
                if envelope.get('role') != 'assistant':
                    raise ValueError()
                usage = observed_usage if observed_usage is not None else self._usage(response.body)
                return self._build_response(blocks, reason, usage)
            except ProviderFailure as error:
                error.provider_response_received = True
                usage = observed_usage if observed_usage is not None else (
                    self._usage(response.body) if len(response.body.encode()) <= _MAX_RESPONSE_BYTES else None
                )
                error.reported_tokens = usage.total_tokens if usage else None
                raise
            except (ValueError, TypeError, KeyError, AttributeError):
                failure = ProviderFailure('invalid_provider_output', 'Provider returned an invalid response.', retryable=False)
                failure.provider_response_received = True
                usage = observed_usage if observed_usage is not None else (
                    self._usage(response.body) if len(response.body.encode()) <= _MAX_RESPONSE_BYTES else None
                )
                failure.reported_tokens = usage.total_tokens if usage else None
                raise failure from None
        finally:
            emit_diagnostics()
