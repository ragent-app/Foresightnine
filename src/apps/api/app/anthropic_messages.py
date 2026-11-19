"""Anthropic Messages wire protocol for research turns."""
from __future__ import annotations
import json
import socket
from urllib.parse import urlsplit
from .model_protocol import ModelResponse, ModelToolCall, ModelUsage
from .openai_compatible import _default_transport, _transport_timeout_from_environment, _TransportFailure, _TransportTimeout, _MAX_RESPONSE_BYTES
from .phase3 import ProviderFailure
from .transport_diagnostics import TransportDiagnostics


def _endpoint(base):
    base = base.rstrip('/')
    path = urlsplit(base).path.rstrip('/')
    if path.endswith('/v1/messages'):
        return base
    return base + ('/messages' if path.endswith('/v1') else '/v1/messages')


class AnthropicMessagesProvider:
    name = 'anthropic_messages'
    def __init__(self, settings, policy, *, anthropic_version, auth_scheme, max_tokens, transport=None):
        if not isinstance(anthropic_version, str) or not anthropic_version or any(c in anthropic_version for c in '\r\n') or auth_scheme not in ('x-api-key', 'bearer') or type(max_tokens) is not int or max_tokens <= 0:
            raise ProviderFailure('provider_configuration_invalid', 'Messages configuration is invalid.', retryable=False)
        self.settings, self.policy = settings, policy
        self.anthropic_version, self.auth_scheme, self.max_tokens = anthropic_version, auth_scheme, max_tokens
        self.transport = transport

    def research_capacity(self):
        from .provider_capabilities import configured_profile
        return configured_profile(_endpoint(self.settings.base_url), self.settings.model)

    def research_capacity_key(self):
        from hashlib import sha256
        from dataclasses import asdict
        profile = self.research_capacity()
        binding = {'endpoint': _endpoint(self.settings.base_url), 'model': self.settings.model,
                   'version': self.anthropic_version, 'auth': self.auth_scheme,
                   'max_tokens': self.max_tokens, 'profile': asdict(profile) if profile else None,
                   'input_budget': self.policy.max_input_tokens}
        return sha256(json.dumps(binding, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    @staticmethod
    def _usage(body):
        try:
            obj = json.loads(body)
            u = obj.get('usage')
            if not isinstance(u, dict): return None
            def count(key):
                v = u.get(key)
                return v if type(v) is int and v >= 0 else None
            inp, out = count('input_tokens'), count('output_tokens')
            creation, read = count('cache_creation_input_tokens'), count('cache_read_input_tokens')
            total = count('total_tokens')
            if total is None and inp is not None and out is not None:
                cache_keys = ('cache_creation_input_tokens', 'cache_read_input_tokens')
                if all(u.get(key) is None or count(key) is not None for key in cache_keys):
                    total = inp + out + (creation or 0) + (read or 0)
            return ModelUsage(inp, out, total, creation, read)
        except (ValueError, TypeError, AttributeError):
            return None

    def _wire(self, turn, cap=None):
        messages = [{'role': 'user', 'content': turn.user}]
        for exchange in turn.history:
            messages.append(exchange.assistant.envelope)
            messages.append({'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': r.tool_call_id, 'content': r.content, 'is_error': r.is_error} for r in exchange.results]})
        wire = {'model': self.settings.model, 'system': turn.system, 'messages': messages, 'max_tokens': min(self.max_tokens, cap) if cap is not None else self.max_tokens}
        if turn.tools:
            wire['tools'] = [{'name': t.name, 'description': t.description, 'input_schema': t.input_schema} for t in turn.tools]
        return wire

    def estimate_turn_input(self, turn):
        return len(json.dumps(self._wire(turn), ensure_ascii=False).encode()) + 64

    def complete_turn(self, turn, *, cancel_event=None, timeout_seconds=None, diagnostic_callback=None, max_output_tokens=None, on_submission=None):
        diagnostics = TransportDiagnostics()
        response = None
        wire = None
        try:
            if not self.settings.api_key:
                raise ProviderFailure('provider_secret_missing', 'Provider secret is not configured.', retryable=False)
            if max_output_tokens is not None and (type(max_output_tokens) is not int or max_output_tokens < 1):
                raise ProviderFailure('provider_budget_exhausted', 'Output budget is exhausted.', retryable=False)
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderFailure('provider_cancelled', 'Provider request was cancelled.', retryable=True)
            wire = self._wire(turn, max_output_tokens)
            body = json.dumps(wire, ensure_ascii=False, separators=(',', ':')).encode()
            headers = {'Accept': 'application/json', 'Content-Type': 'application/json', 'anthropic-version': self.anthropic_version}
            headers['x-api-key' if self.auth_scheme == 'x-api-key' else 'Authorization'] = self.settings.api_key if self.auth_scheme == 'x-api-key' else 'Bearer ' + self.settings.api_key
            timeout = min(self.policy.timeout_seconds, timeout_seconds) if timeout_seconds is not None else self.policy.timeout_seconds
            if timeout <= 0:
                raise ProviderFailure('provider_budget_exhausted', 'Time budget is exhausted.', retryable=False)
            if self.transport is None:
                # Same connect/response limits as the OpenAI-compatible transport; without them the
                # Messages path fell back to a 5 s connect timeout that slow TLS handshakes behind a
                # proxy often exceeded (364 acceptance). Pre-send failures are retried, never resent.
                response = _default_transport('POST', _endpoint(self.settings.base_url), headers, body, timeout, allow_private_network=self.settings.allow_private_network, diagnostics=diagnostics,
                    connect_timeout_seconds=_transport_timeout_from_environment("FORESIGHTNINE_LLM_CONNECT_TIMEOUT_SECONDS", 15.0),
                    response_timeout_seconds=_transport_timeout_from_environment("FORESIGHTNINE_LLM_RESPONSE_TIMEOUT_SECONDS", None),
                    cancel_event=cancel_event, on_submission=on_submission)
            else:
                if on_submission: on_submission()
                diagnostics.submission('started_unknown')
                response = self.transport('POST', _endpoint(self.settings.base_url), headers, body, timeout)
                diagnostics.submission('response_received')
        except (_TransportFailure, TimeoutError, socket.timeout) as error:
            code = 'provider_cancelled' if cancel_event and cancel_event.is_set() else 'provider_timeout' if isinstance(error, (TimeoutError, socket.timeout, _TransportTimeout)) else 'provider_transport_error'
            failure = ProviderFailure(code, 'Provider request ended without a usable result.', retryable=True)
            failure.provider_response_received = diagnostics.snapshot()['submission_state'] == 'response_received'
            raise failure from None
        finally:
            if diagnostic_callback:
                try:
                    observation = diagnostics.snapshot()
                    if wire is not None:
                        from .research_diagnostics import safe_wire_metadata
                        observation['request_metadata'] = {**safe_wire_metadata(wire), 'provider': 'anthropic_messages'}
                    if response is not None:
                        from .research_diagnostics import safe_messages_error, safe_provider_error
                        details = response.safe_error or (safe_provider_error(response.body) if not 200 <= response.status_code < 300 else {})
                        observation['provider_error'] = safe_messages_error(details, response.status_code, dict(response.headers))
                    diagnostic_callback(observation)
                except Exception:
                    pass
        if cancel_event and cancel_event.is_set():
            failure = ProviderFailure('provider_cancelled', 'Provider result discarded.', retryable=True)
            failure.provider_response_received = True
            usage = self._usage(response.body) if len(response.body.encode()) <= _MAX_RESPONSE_BYTES else None
            failure.reported_tokens = usage.total_tokens if usage else None
            raise failure
        if not 200 <= response.status_code < 300:
            failure = ProviderFailure('provider_request_rejected', 'Provider rejected the request.', retryable=False)
            failure.provider_response_received = True
            raise failure
        try:
            if len(response.body.encode()) > _MAX_RESPONSE_BYTES: raise ValueError()
            envelope = json.loads(response.body)
            blocks, reason = envelope['content'], envelope['stop_reason']
            if envelope.get('role') != 'assistant' or not isinstance(blocks, list) or not blocks: raise ValueError()
            if reason == 'max_tokens': raise ProviderFailure('provider_output_truncated', 'Provider output was truncated.', retryable=False)
            if reason == 'refusal': raise ProviderFailure('provider_output_refused', 'Provider declined the request.', retryable=False)
            if reason == 'model_context_window_exceeded': raise ProviderFailure('provider_context_limit', 'Provider context limit was reached.', retryable=False)
            if reason not in ('end_turn', 'tool_use', 'stop_sequence'): raise ValueError()
            calls = []
            safe_blocks = []
            for block in blocks:
                kind = block['type']
                if kind == 'text':
                    if not isinstance(block.get('text'), str): raise ValueError()
                    safe_blocks.append({'type': kind, 'text': block['text']})
                elif kind == 'tool_use':
                    if not all(isinstance(block.get(k), str) and block[k] for k in ('id', 'name')) or not isinstance(block.get('input'), dict): raise ValueError()
                    calls.append(ModelToolCall(block['id'], block['name'], block['input']))
                    safe_blocks.append({'type': kind, 'id': block['id'], 'name': block['name'], 'input': block['input']})
                elif kind == 'thinking':
                    if not isinstance(block.get('thinking'), str) or not isinstance(block.get('signature'), str): raise ValueError()
                    safe_blocks.append({'type': kind, 'thinking': block['thinking'], 'signature': block['signature']})
                elif kind == 'redacted_thinking':
                    if not isinstance(block.get('data'), str): raise ValueError()
                    safe_blocks.append({'type': kind, 'data': block['data']})
                else: raise ValueError()
            if len({c.id for c in calls}) != len(calls) or (reason == 'tool_use') != bool(calls): raise ValueError()
            usage = self._usage(response.body)
            return ModelResponse({'role': 'assistant', 'content': safe_blocks}, tuple(calls), usage, reason, 'tool_calls' if calls else 'end')
        except ProviderFailure as error:
            error.provider_response_received = True
            usage = self._usage(response.body) if len(response.body.encode()) <= _MAX_RESPONSE_BYTES else None
            error.reported_tokens = usage.total_tokens if usage else None
            raise
        except (ValueError, TypeError, KeyError, AttributeError):
            failure = ProviderFailure('invalid_provider_output', 'Provider returned an invalid response.', retryable=False)
            failure.provider_response_received = True
            usage = self._usage(response.body) if len(response.body.encode()) <= _MAX_RESPONSE_BYTES else None
            failure.reported_tokens = usage.total_tokens if usage else None
            raise failure from None
