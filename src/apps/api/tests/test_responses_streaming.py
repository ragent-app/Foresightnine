"""Responses connections stream live deltas; the result is the closing event's complete response."""
import json

import pytest

from app import openai_compatible, openai_responses
from app.model_protocol import ModelTurnInput
from app.openai_compatible import HttpResponse
from app.phase3 import ProviderFailure

REPORT = "## 结论\n\n**时点：**最近交易日上涨 31.48%。"
ARGS = json.dumps({"report": REPORT, "claims": [], "limitations": [], "stopping_reason": "answered"}, ensure_ascii=False)
FINAL = {"status": "completed", "output": [
    {"type": "reasoning", "id": "rs1", "summary": [], "encrypted_content": "opaque"},
    {"type": "message", "id": "m1", "role": "assistant", "content": [{"type": "output_text", "text": "先核对行情。"}]},
    {"type": "function_call", "id": "fc1", "name": "submit_research", "call_id": "call-1", "arguments": ARGS},
], "usage": {"input_tokens": 30, "output_tokens": 10, "total_tokens": 40}}


def provider(monkeypatch):
    monkeypatch.setattr(openai_compatible, "_require_public_dns", lambda *_: None)
    settings = openai_compatible.OpenAICompatibleSettings.from_account_values(
        base_url="https://model.example.test/v1", model="custom", api_key="fixture-secret")
    return openai_responses.OpenAIResponsesProvider(settings)


TURN = ModelTurnInput("system", "user", (), ())


def frames(*, final=True, split=7):
    events = [{"type": "response.created", "response": {"status": "in_progress"}},
              {"type": "response.output_text.delta", "item_id": "m1", "output_index": 1, "delta": "先核对行情。"},
              {"type": "response.output_item.added", "output_index": 2,
               "item": {"type": "function_call", "id": "fc1", "name": "submit_research", "call_id": "call-1", "arguments": ""}}]
    events += [{"type": "response.function_call_arguments.delta", "item_id": "fc1", "output_index": 2, "delta": ARGS[i:i + split]}
               for i in range(0, len(ARGS), split)]
    if final:
        events.append({"type": "response.completed", "response": FINAL})
    return events


def test_live_deltas_stream_and_the_closing_event_is_the_result(monkeypatch):
    model = provider(monkeypatch)
    seen = {}

    def stream(method, url, headers, body, timeout, *, on_frame, **kwargs):
        seen["wire"] = json.loads(body)
        seen["accept"] = headers["Accept"]
        for event in frames():
            on_frame(event["type"], json.dumps(event, ensure_ascii=False))
        return None

    monkeypatch.setattr(openai_responses, "_default_stream_transport", stream)
    monkeypatch.setattr(openai_responses, "_default_transport", lambda *a, **k: pytest.fail("no second request"))
    emitted = []
    result = model.complete_turn(TURN, on_stream_event=emitted.append)
    assert seen["wire"]["stream"] is True and seen["accept"] == "text/event-stream"
    draft = "".join(e["delta"] for e in emitted if e["channel"] == "draft_report")
    assert draft == REPORT
    assert [e["delta"] for e in emitted if e["channel"] == "text"] == ["先核对行情。"]
    assert any(e["channel"] == "tool_status" for e in emitted)
    assert [call.name for call in result.tool_calls] == ["submit_research"]
    assert result.tool_calls[0].arguments["report"] == REPORT
    assert result.usage.total_tokens == 40
    assert result.message["output"][0]["encrypted_content"] == "opaque", "reasoning is carried for the next call"


def test_an_endpoint_that_answers_with_json_is_read_as_before(monkeypatch):
    model = provider(monkeypatch)
    monkeypatch.setattr(openai_responses, "_default_stream_transport",
                        lambda *a, **k: HttpResponse(200, json.dumps(FINAL, ensure_ascii=False)))
    result = model.complete_turn(TURN, on_stream_event=lambda e: None)
    assert result.tool_calls[0].arguments["report"] == REPORT


def test_a_refused_stream_request_is_asked_again_whole_and_not_streamed_again(monkeypatch):
    model = provider(monkeypatch)
    attempts = []

    def stream(*a, **k):
        attempts.append("stream")
        return HttpResponse(400, "", {}, {"param": "stream"})

    def whole(method, url, headers, body, timeout, **kwargs):
        attempts.append("whole")
        assert "stream" not in json.loads(body) and headers["Accept"] == "application/json"
        return HttpResponse(200, json.dumps(FINAL, ensure_ascii=False))

    monkeypatch.setattr(openai_responses, "_default_stream_transport", stream)
    monkeypatch.setattr(openai_responses, "_default_transport", whole)
    model.complete_turn(TURN, on_stream_event=lambda e: None)
    model.complete_turn(TURN, on_stream_event=lambda e: None)
    assert attempts == ["stream", "whole", "whole"]


def test_a_stream_without_its_closing_event_has_no_result(monkeypatch):
    model = provider(monkeypatch)

    def stream(method, url, headers, body, timeout, *, on_frame, **kwargs):
        for event in frames(final=False):
            on_frame(event["type"], json.dumps(event, ensure_ascii=False))
        return None

    monkeypatch.setattr(openai_responses, "_default_stream_transport", stream)
    with pytest.raises(ProviderFailure) as caught:
        model.complete_turn(TURN, on_stream_event=lambda e: None)
    assert caught.value.code == "invalid_provider_output"


def test_a_failed_stream_is_a_provider_failure_and_a_server_error_is_not_retried(monkeypatch):
    model = provider(monkeypatch)

    def failed(method, url, headers, body, timeout, *, on_frame, **kwargs):
        on_frame("response.failed", json.dumps({"type": "response.failed", "response": {"status": "failed"}}))
        return None

    monkeypatch.setattr(openai_responses, "_default_stream_transport", failed)
    with pytest.raises(ProviderFailure) as caught:
        model.complete_turn(TURN, on_stream_event=lambda e: None)
    assert caught.value.code == "provider_unavailable"

    monkeypatch.setattr(openai_responses, "_default_stream_transport", lambda *a, **k: HttpResponse(503, "", {}, {}))
    monkeypatch.setattr(openai_responses, "_default_transport", lambda *a, **k: pytest.fail("a 503 is not re-sent"))
    with pytest.raises(ProviderFailure) as caught:
        model.complete_turn(TURN, on_stream_event=lambda e: None)
    assert caught.value.code == "provider_request_rejected" and caught.value.retryable


def test_without_a_live_listener_the_request_is_not_streamed(monkeypatch):
    model = provider(monkeypatch)
    monkeypatch.setattr(openai_responses, "_default_stream_transport", lambda *a, **k: pytest.fail("not streamed"))
    monkeypatch.setattr(openai_responses, "_default_transport",
                        lambda method, url, headers, body, timeout, **kw: HttpResponse(200, json.dumps(FINAL, ensure_ascii=False)))
    assert model.complete_turn(TURN).tool_calls[0].name == "submit_research"


def test_without_a_closing_event_the_finished_items_are_the_result(monkeypatch):
    model = provider(monkeypatch)

    def stream(method, url, headers, body, timeout, *, on_frame, **kwargs):
        for event in frames(final=False):
            on_frame(event["type"], json.dumps(event, ensure_ascii=False))
        for index, item in enumerate(FINAL["output"]):
            on_frame("response.output_item.done", json.dumps({"type": "response.output_item.done", "output_index": index, "item": item}, ensure_ascii=False))
        return None

    monkeypatch.setattr(openai_responses, "_default_stream_transport", stream)
    result = model.complete_turn(TURN, on_stream_event=lambda e: None)
    assert result.tool_calls[0].arguments["report"] == REPORT and result.usage is None
