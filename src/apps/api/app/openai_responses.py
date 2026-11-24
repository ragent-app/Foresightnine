"""Stateless Responses wire protocol for personal research connections."""
from collections.abc import Mapping
import json
import math
from time import monotonic

from .model_protocol import (
    ModelResponse,
    ModelToolCall,
    ModelUsage,
    _UNRELATED_REJECTION_PARAMETERS,
    canonical_json_dumps,
    ensure_json_word_in_messages,
    extract_assistant_text,
    format_response_schema_instruction,
    merge_cancel_events,
    merge_optional_max_tokens,
    merge_optional_timeout,
    render_user_instructions,
)
from .openai_compatible import (
    _build_transport_failure, _default_stream_transport, _default_transport, _llm_timeout_from_environment,
)
from .phase3 import ProviderFailure, ProviderPolicy
from .provider_capabilities import _strict_schema, research_profile, resolve_output_token_limit
from .research_diagnostics import (
    log_provider_rejection, report_diagnostic_write_failure, safe_rejection_details,
    safe_usage_diagnostics, safe_wire_metadata, set_rejection_details, wire_manifest,
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
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise ValueError()
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
            enc = item.get("encrypted_content")
            if enc is not None and not isinstance(enc, str):
                raise ValueError()
            if isinstance(enc, str) and enc:
                rec = {k: item[k] for k in ("type", "id", "summary", "encrypted_content") if k in item}
                if "summary" not in rec or not isinstance(rec["summary"], list):
                    rec["summary"] = []
                output.append(rec)
        elif kind == "message" and item.get("role") == "assistant":
            if not isinstance(item.get("content"), list):
                raise ValueError()
            for part in item["content"]:
                if not isinstance(part, dict):
                    raise ValueError()
                if part.get("type") == "refusal":
                    raise ProviderFailure("provider_output_refused", "Provider declined the request.", retryable=False)
                if part.get("type") not in ("output_text", "text") or not isinstance(part.get("text"), str):
                    raise ValueError()
                text.append(part["text"])
            output.append({k: item[k] for k in ("type", "id", "role", "content", "phase", "status") if k in item})
        else:
            continue
    if len({call.id for call in calls}) != len(calls):
        raise ValueError()
    stop = "tool_calls" if calls else "end"
    return ModelResponse({"role": "assistant", "content": "\n".join(text), "output": output},
                         tuple(calls), usage, "tool_calls" if calls else "stop", stop)


class _StreamCollector:
    """Turns Responses stream events into live deltas; the final object comes from the closing event."""

    def result(self):
        """The closing event's response; failing that, the finished output items in order (usage unknown)."""
        if self.final is not None:
            return self.final
        if not self.done_items:
            raise ValueError()
        return {"status": "completed", "output": [self.done_items[i] for i in sorted(self.done_items)]}

    def __init__(self, emit):
        self.emit = emit
        self.final = None
        self.calls = {}
        self.done_items = {}

    def _call(self, payload):
        key = payload.get("item_id") or payload.get("output_index")
        return self.calls.get(key) or self.calls.get(payload.get("output_index"))

    def frame(self, event_name, data):
        from .research_stream_extract import TopLevelReportExtractor
        text = data.strip()
        if not text or text == "[DONE]":
            return
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError()
        kind = payload.get("type") or event_name
        if kind == "error" or kind == "response.failed":
            raise ProviderFailure("provider_unavailable", "Provider stream returned an error.", retryable=False)
        if kind in ("response.completed", "response.incomplete"):
            if not isinstance(payload.get("response"), dict):
                raise ValueError()
            self.final = payload["response"]
            return
        if kind == "response.output_item.done":
            item, index = payload.get("item"), payload.get("output_index")
            if isinstance(item, dict) and type(index) is int:
                self.done_items[index] = item
            return
        if kind == "response.output_item.added":
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            if item.get("type") == "function_call" and isinstance(item.get("name"), str):
                entry = {"name": item["name"], "extractor": TopLevelReportExtractor() if item["name"] == "submit_research" else None}
                for key in (item.get("id"), payload.get("output_index")):
                    if key is not None:
                        self.calls[key] = entry
                self.emit({"channel": "tool_status", "delta": "正在起草研究报告并整理引文，完成后将自动核验原文位置…"
                           if item["name"] == "submit_research" else f"正在准备工具参数：{item['name']}"})
            return
        delta = payload.get("delta")
        if not isinstance(delta, str) or not delta:
            return
        if kind == "response.function_call_arguments.delta":
            entry = self._call(payload)
            if entry and entry["extractor"] is not None:
                draft = entry["extractor"].feed(delta)
                if draft:
                    self.emit({"channel": "draft_report", "delta": draft})
        elif kind == "response.output_text.delta":
            self.emit({"channel": "text", "delta": delta})
        elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            self.emit({"channel": "thinking", "delta": delta})


class OpenAIResponsesProvider:
    name = "openai_responses"

    def __init__(self, settings):
        self.settings = settings
        self.policy = ProviderPolicy(timeout_seconds=_llm_timeout_from_environment())
        # Set once an endpoint refuses streaming; this provider then asks for whole responses.
        self.stream_refused = False
        # Set once an endpoint rejects include=["reasoning.encrypted_content"].
        self.include_refused = False

    def research_capacity(self):
        return research_profile(self.settings, responses_endpoint(self.settings.base_url), self.name, "responses-v1")

    def generation_parameters_json(self):
        params = self.settings.generation_parameters
        return params.model_dump_json(exclude_none=True) if params else ""

    def _wire(
        self,
        turn,
        cap=None,
        *,
        include_text_format: bool = True,
        include_reasoning_encrypted: bool = True,
        include_sampling_params: bool = True,
    ):
        inputs = [{"role": "user", "content": turn.user}]
        for exchange in turn.history:
            envelope = exchange.assistant.envelope
            output_items = envelope.get("output") if isinstance(envelope, Mapping) else None
            if isinstance(output_items, list):
                call_ids_in_out = {
                    item.get("call_id")
                    for item in output_items
                    if isinstance(item, Mapping)
                    and item.get("type") == "function_call"
                    and isinstance(item.get("call_id"), str)
                    and item.get("call_id")
                }
                result_ids = {
                    result.tool_call_id
                    for result in exchange.results
                    if isinstance(result.tool_call_id, str) and result.tool_call_id
                }
                paired_ids = call_ids_in_out & result_ids
                for item in output_items:
                    if not isinstance(item, Mapping):
                        continue
                    kind = item.get("type")
                    if kind == "reasoning":
                        if not item.get("encrypted_content"):
                            continue
                        rec = dict(item)
                        if "summary" not in rec or not isinstance(rec["summary"], list):
                            rec["summary"] = []
                        inputs.append(rec)
                    elif kind == "function_call":
                        if item.get("call_id") in paired_ids:
                            inputs.append(dict(item))
                    else:
                        inputs.append(dict(item))
                inputs.extend(
                    {"type": "function_call_output", "call_id": result.tool_call_id, "output": result.content}
                    for result in exchange.results
                    if result.tool_call_id in paired_ids
                )
            elif isinstance(envelope, Mapping):
                text = extract_assistant_text(envelope)
                if text:
                    inputs.append({"role": "assistant", "content": text})
            followup = getattr(exchange, "user_followup", None)
            if isinstance(followup, str) and followup.strip():
                inputs.append({"role": "user", "content": followup})
        if turn.tail_note:
            inputs.append({"role": "user", "content": turn.tail_note})
        inputs.extend({"role": "user", "content": instruction} for instruction in render_user_instructions(turn))

        response_schema = getattr(turn, "response_schema", None)
        response_schema_name = getattr(turn, "response_schema_name", None)
        has_schema_request = response_schema is not None or (
            isinstance(response_schema_name, str) and bool(response_schema_name.strip())
        )
        instructions_text = turn.system
        text_format = None
        schema_note = None
        if has_schema_request:
            schema_name = (
                response_schema_name.strip()
                if isinstance(response_schema_name, str) and response_schema_name.strip()
                else "response"
            )
            if include_text_format and not turn.tools:
                profile = self.research_capacity()
                if profile is not None and getattr(profile, "mode", None) == "json_schema" and isinstance(response_schema, Mapping):
                    try:
                        strict_schema = _strict_schema(dict(response_schema))
                        text_format = {
                            "type": "json_schema",
                            "name": schema_name,
                            "strict": True,
                            "schema": strict_schema,
                        }
                    except ProviderFailure:
                        text_format = {"type": "json_object"}
                        schema_note = format_response_schema_instruction(
                            response_schema, schema_name, has_tools=False
                        )
                else:
                    text_format = {"type": "json_object"}
                    schema_note = format_response_schema_instruction(
                        response_schema, schema_name, has_tools=False
                    )
            else:
                schema_note = format_response_schema_instruction(
                    response_schema, schema_name, has_tools=bool(turn.tools)
                )
        if schema_note:
            instructions_text = f"{instructions_text}\n\n{schema_note}" if instructions_text else schema_note
        if text_format is not None and text_format.get("type") == "json_object":
            inputs = ensure_json_word_in_messages(
                inputs,
                roles=("user",),
                text_part_type="input_text",
            )

        wire = {
            "model": self.settings.model,
            "instructions": instructions_text,
            "input": inputs,
            "store": False,
        }
        if include_reasoning_encrypted and not self.include_refused:
            wire["include"] = ["reasoning.encrypted_content"]
        if text_format is not None:
            wire["text"] = {"format": text_format}
        if turn.tools:
            wire["tools"] = [{"type": "function", "name": tool.name, "description": tool.description,
                              "parameters": tool.input_schema, "strict": False} for tool in turn.tools]
        params = self.settings.generation_parameters
        if params:
            if params.reasoning_effort is not None:
                wire["reasoning"] = {"effort": params.reasoning_effort}
            if include_sampling_params:
                wire.update(params.model_dump(include={"temperature", "top_p"}, exclude_none=True))
        if cap is not None:
            wire["max_output_tokens"] = cap
        return wire

    def estimate_turn_input(self, turn):
        return len(canonical_json_dumps(self._wire(turn)).encode()) + 64

    def _classify_400_fallback(
        self,
        wire: dict,
        response,
        *,
        retried_without_include: bool,
        retried_without_sampling: bool,
        retried_without_text: bool,
    ) -> str | None:
        if getattr(response, "status_code", None) not in (400, 422):
            return None
        details = getattr(response, "rejection_summary", None) or safe_rejection_details(
            getattr(response, "body", b""),
            status_code=response.status_code,
            safe_error=getattr(response, "safe_error", None),
        )
        safe_err = getattr(response, "safe_error", None) or {}
        err_param = str(details.get("error_param") or safe_err.get("messages_parameter") or "")
        err_msg_lower = str(details.get("message") or "").lower()
        if (
            "include" in wire
            and not retried_without_include
            and (
                err_param == "include"
                or "encrypted_content" in err_msg_lower
                or ("include" in err_msg_lower and ("unknown" in err_msg_lower or "unsupported" in err_msg_lower))
            )
        ):
            return "strip_include"
        if (
            "reasoning" in wire
            and ("temperature" in wire or "top_p" in wire)
            and not retried_without_sampling
            and (
                err_param in ("temperature", "top_p")
                or (
                    err_param in ("reasoning", "reasoning.effort", "")
                    and ("temperature" in err_msg_lower or "top_p" in err_msg_lower)
                )
            )
        ):
            return "strip_sampling"
        if (
            "text" in wire
            and not retried_without_text
            and err_param not in _UNRELATED_REJECTION_PARAMETERS
        ):
            return "strip_text"
        return None

    def complete_turn(self, turn, *, cancel_event=None, timeout_seconds=None, diagnostic_callback=None,
                      max_output_tokens=None, on_submission=None, on_stream_event=None):
        diag = TransportDiagnostics()
        started = monotonic()
        usage = None
        response = None
        wire = body = None
        effective_cancel = merge_cancel_events(cancel_event, getattr(turn, "cancel_event", None))
        effective_timeout_arg = merge_optional_timeout(timeout_seconds, getattr(turn, "timeout_seconds", None))
        effective_max_out_arg = merge_optional_max_tokens(max_output_tokens, getattr(turn, "max_output_tokens", None))
        try:
            if not self.settings.api_key or (turn.tools and not self.settings.tools_verified):
                raise ProviderFailure("provider_configuration_invalid", "Responses connection is not ready.", retryable=False)
            if effective_cancel is not None and effective_cancel.is_set():
                raise ProviderFailure("provider_cancelled", "Request cancelled.", retryable=False)
            timeout = self.policy.timeout_seconds
            if effective_timeout_arg is not None:
                if (
                    isinstance(effective_timeout_arg, bool)
                    or not isinstance(effective_timeout_arg, (int, float))
                    or not math.isfinite(effective_timeout_arg)
                    or effective_timeout_arg <= 0
                ):
                    raise ProviderFailure("provider_budget_exhausted", "Model budget exhausted.", retryable=False)
                timeout = min(timeout, float(effective_timeout_arg))
            if timeout <= 0 or (effective_max_out_arg is not None and (type(effective_max_out_arg) is not int or effective_max_out_arg <= 0)):
                raise ProviderFailure("provider_budget_exhausted", "Model budget exhausted.", retryable=False)
            cap = resolve_output_token_limit(effective_max_out_arg, self.policy.max_output_tokens)

            include_text_format = True
            include_reasoning_encrypted = not self.include_refused
            include_sampling_params = True
            retried_without_include = False
            retried_without_sampling = False
            retried_without_text = False

            def _build_and_check_wire() -> tuple[dict, bytes]:
                next_wire = self._wire(
                    turn,
                    cap,
                    include_text_format=include_text_format,
                    include_reasoning_encrypted=include_reasoning_encrypted,
                    include_sampling_params=include_sampling_params,
                )
                next_body = canonical_json_dumps(next_wire).encode()
                if self.policy.max_input_tokens is not None and len(next_body) > self.policy.max_input_tokens:
                    raise ProviderFailure("provider_budget_exhausted", "Input exceeds the per-call budget.", retryable=False)
                return next_wire, next_body

            def _maybe_fallback_response(resp, *, from_stream: bool) -> bool:
                nonlocal include_reasoning_encrypted, include_sampling_params, include_text_format
                nonlocal retried_without_include, retried_without_sampling, retried_without_text, wire, body
                action = self._classify_400_fallback(
                    wire,
                    resp,
                    retried_without_include=retried_without_include,
                    retried_without_sampling=retried_without_sampling,
                    retried_without_text=retried_without_text,
                )
                if action is None:
                    return False
                if from_stream and action == "strip_text":
                    details = getattr(resp, "rejection_summary", None) or safe_rejection_details(
                        getattr(resp, "body", b""),
                        status_code=getattr(resp, "status_code", None),
                        safe_error=getattr(resp, "safe_error", None),
                    )
                    err_param = str(details.get("error_param") or "")
                    err_msg = str(details.get("message") or "").lower()
                    if err_param not in ("text", "text.format", "response_format", "input") and "json" not in err_msg and "text" not in err_msg:
                        return False
                if action == "strip_include":
                    self.include_refused = True
                    include_reasoning_encrypted = False
                    retried_without_include = True
                elif action == "strip_sampling":
                    include_sampling_params = False
                    retried_without_sampling = True
                elif action == "strip_text":
                    include_text_format = False
                    retried_without_text = True
                wire, body = _build_and_check_wire()
                return True

            wire, body = _build_and_check_wire()
            url = responses_endpoint(self.settings.base_url)
            auth = {"Authorization": "Bearer " + self.settings.api_key, "Content-Type": "application/json"}
            if on_stream_event is not None and not self.stream_refused:
                # Live deltas only; the result is the closing event's complete response object.
                collector = _StreamCollector(on_stream_event)
                stream_body = canonical_json_dumps({**wire, "stream": True}).encode()
                response = _default_stream_transport("POST", url, {**auth, "Accept": "text/event-stream"}, stream_body, timeout,
                    on_frame=collector.frame, allow_private_network=self.settings.allow_private_network,
                    diagnostics=diag, cancel_event=effective_cancel, on_submission=on_submission)
                if response is None:
                    final = collector.result()
                    usage = _usage(final.get("usage"))
                    if effective_cancel is not None and effective_cancel.is_set():
                        raise ProviderFailure("provider_cancelled", "Request cancelled.", retryable=False)
                    return _parse_response(final, usage)
                if response.status_code in (400, 422):
                    if not _maybe_fallback_response(response, from_stream=True):
                        # A request refused as invalid was not carried out: ask again for a whole response.
                        self.stream_refused = True
                    response = None
            while True:
                if response is None:
                    response = _default_transport("POST", url, {**auth, "Accept": "application/json"},
                        body, timeout, diagnostics=diag, cancel_event=effective_cancel, on_submission=on_submission,
                        allow_private_network=self.settings.allow_private_network)
                if 200 <= response.status_code < 300:
                    break
                if _maybe_fallback_response(response, from_stream=False):
                    response = None
                    continue
                failure = ProviderFailure("provider_request_rejected", "Provider rejected the request.",
                                          retryable=response.status_code == 429 or response.status_code >= 500)
                set_rejection_details(failure, response)
                if failure.code == "provider_request_rejected":
                    log_provider_rejection("openai_responses", failure, context={"model": self.settings.model})
                raise failure
            data = json.loads(response.body)
            usage = _usage(data.get("usage"))
            if effective_cancel is not None and effective_cancel.is_set():
                raise ProviderFailure("provider_cancelled", "Request cancelled.", retryable=False)
            return _parse_response(data, usage)
        except OSError as error:
            raise _build_transport_failure(error, diag, cancelled=bool(effective_cancel and effective_cancel.is_set()),
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
