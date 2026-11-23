"""Stateless Responses wire protocol for personal research connections."""
import json
from time import monotonic

from .model_protocol import ModelResponse, ModelToolCall, ModelUsage, canonical_json_dumps, render_user_instructions
from .openai_compatible import _build_transport_failure, _default_transport, _llm_timeout_from_environment
from .phase3 import ProviderFailure, ProviderPolicy
from .provider_capabilities import research_profile, resolve_output_token_limit
from .research_diagnostics import (
    report_diagnostic_write_failure, safe_usage_diagnostics, safe_wire_metadata, set_rejection_details, wire_manifest,
)
from .transport_diagnostics import TransportDiagnostics


def responses_endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith("/responses") else base + "/responses"


def _usage(data):
    if not isinstance(data, dict):
        return None
    def count(value):
        return value if type(value) is int and value >= 0 else None
    details = data.get("input_tokens_details")
    cached = count(details.get("cached_tokens")) if isinstance(details, dict) else None
    return ModelUsage(count(data.get("input_tokens")), count(data.get("output_tokens")),
                      count(data.get("total_tokens")), cache_read_input_tokens=cached)


def _parse_response(data, usage):
    status = data.get("status")
    if status == "incomplete":
        reason = (data.get("incomplete_details") or {}).get("reason")
        code = "provider_output_refused" if reason == "content_filter" else "provider_output_truncated"
        raise ProviderFailure(code, "Provider returned an incomplete response.", retryable=False)
    if status != "completed" or not isinstance(data.get("output"), list) or not data["output"]:
        raise ValueError()
    calls, text, output = [], [], []
    for item in data["output"]:
        kind = item["type"]
        if item.get("status") not in (None, "completed"):
            raise ValueError()
        if kind == "function_call":
            call_id, name, raw = item["call_id"], item["name"], item["arguments"]
            if not all(isinstance(v, str) and v for v in (call_id, name)) or not isinstance(raw, str):
                raise ValueError()
            try:
                args = json.loads(raw)
            except ValueError:
                args = None
            calls.append(ModelToolCall(call_id, name, args if isinstance(args, dict) else None, raw))
            output.append({k: item[k] for k in ("type", "id", "call_id", "name", "arguments", "status") if k in item})
        elif kind == "reasoning":
            if not isinstance(item.get("encrypted_content"), str) or not item["encrypted_content"]:
                raise ValueError()
            output.append({k: item[k] for k in ("type", "id", "summary", "encrypted_content") if k in item})
        elif kind == "message" and item.get("role") == "assistant":
            if not isinstance(item.get("content"), list):
                raise ValueError()
            for part in item["content"]:
                if part["type"] == "refusal":
                    raise ProviderFailure("provider_output_refused", "Provider declined the request.", retryable=False)
                if part["type"] != "output_text" or not isinstance(part.get("text"), str):
                    raise ValueError()
                text.append(part["text"])
            output.append({k: item[k] for k in ("type", "id", "role", "content", "phase", "status") if k in item})
        else:
            raise ValueError()
    if len({call.id for call in calls}) != len(calls):
        raise ValueError()
    stop = "tool_calls" if calls else "end"
    return ModelResponse({"role": "assistant", "content": "\n".join(text), "output": output},
                         tuple(calls), usage, "tool_calls" if calls else "stop", stop)


class OpenAIResponsesProvider:
    name = "openai_responses"

    def __init__(self, settings):
        self.settings = settings
        self.policy = ProviderPolicy(timeout_seconds=_llm_timeout_from_environment())

    def research_capacity(self):
        return research_profile(self.settings, responses_endpoint(self.settings.base_url), self.name, "responses-v1")

    def generation_parameters_json(self):
        params = self.settings.generation_parameters
        return params.model_dump_json(exclude_none=True) if params else ""

    def _wire(self, turn, cap=None):
        inputs = [{"role": "user", "content": turn.user}]
        for exchange in turn.history:
            inputs.extend(exchange.assistant.envelope["output"])
            inputs.extend({"type": "function_call_output", "call_id": result.tool_call_id, "output": result.content}
                          for result in exchange.results)
        if turn.tail_note:
            inputs.append({"role": "user", "content": turn.tail_note})
        inputs.extend({"role": "user", "content": instruction} for instruction in render_user_instructions(turn))
        wire = {"model": self.settings.model, "instructions": turn.system, "input": inputs,
                "store": False, "include": ["reasoning.encrypted_content"]}
        if turn.tools:
            wire["tools"] = [{"type": "function", "name": tool.name, "description": tool.description,
                              "parameters": tool.input_schema, "strict": False} for tool in turn.tools]
        params = self.settings.generation_parameters
        if params:
            if params.reasoning_effort is not None:
                wire["reasoning"] = {"effort": params.reasoning_effort}
            wire.update(params.model_dump(include={"temperature", "top_p"}, exclude_none=True))
        if cap is not None:
            wire["max_output_tokens"] = cap
        return wire

    def estimate_turn_input(self, turn):
        return len(canonical_json_dumps(self._wire(turn)).encode()) + 64

    def complete_turn(self, turn, *, cancel_event=None, timeout_seconds=None, diagnostic_callback=None,
                      max_output_tokens=None, on_submission=None):
        diag = TransportDiagnostics()
        started = monotonic()
        usage = None
        response = None
        wire = body = None
        try:
            if not self.settings.api_key or (turn.tools and not self.settings.tools_verified):
                raise ProviderFailure("provider_configuration_invalid", "Responses connection is not ready.", retryable=False)
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderFailure("provider_cancelled", "Request cancelled.", retryable=False)
            timeout = self.policy.timeout_seconds
            if timeout_seconds is not None:
                timeout = min(timeout, timeout_seconds)
            if timeout <= 0 or (max_output_tokens is not None and max_output_tokens <= 0):
                raise ProviderFailure("provider_budget_exhausted", "Model budget exhausted.", retryable=False)
            cap = resolve_output_token_limit(max_output_tokens, self.policy.max_output_tokens)
            wire = self._wire(turn, cap)
            body = canonical_json_dumps(wire).encode()
            if self.policy.max_input_tokens is not None and len(body) > self.policy.max_input_tokens:
                raise ProviderFailure("provider_budget_exhausted", "Input exceeds the per-call budget.", retryable=False)
            response = _default_transport("POST", responses_endpoint(self.settings.base_url),
                {"Authorization": "Bearer " + self.settings.api_key, "Content-Type": "application/json", "Accept": "application/json"},
                body, timeout, diagnostics=diag, cancel_event=cancel_event, on_submission=on_submission,
                allow_private_network=self.settings.allow_private_network)
            if not 200 <= response.status_code < 300:
                failure = ProviderFailure("provider_request_rejected", "Provider rejected the request.",
                                          retryable=response.status_code == 429 or response.status_code >= 500)
                set_rejection_details(failure, response)
                raise failure
            data = json.loads(response.body)
            usage = _usage(data.get("usage"))
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderFailure("provider_cancelled", "Request cancelled.", retryable=False)
            return _parse_response(data, usage)
        except OSError as error:
            raise _build_transport_failure(error, diag, cancelled=bool(cancel_event and cancel_event.is_set()),
                message="Responses request ended without a usable result.", started_at=started,
                wire_bytes=len(body) if body else None, streamed_bytes=None) from None
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            failure = ProviderFailure("invalid_provider_output", "Invalid Responses envelope.", retryable=False)
            failure.provider_response_received = response is not None
            failure.reported_tokens = usage.total_tokens if usage else None
            raise failure from None
        except ProviderFailure as error:
            error.provider_response_received = response is not None
            error.reported_tokens = usage.total_tokens if usage else None
            raise
        finally:
            if diagnostic_callback is not None:
                try:
                    observation = diag.snapshot()
                    if body is not None:
                        observation["wire_manifest"] = wire_manifest(wire, body)
                        observation["request_metadata"] = safe_wire_metadata(wire, wire_bytes=len(body))
                    if usage:
                        observation["usage"] = safe_usage_diagnostics(usage)
                    diagnostic_callback(observation)
                except Exception as error:
                    report_diagnostic_write_failure(error)
