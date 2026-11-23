import json
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from app import main, openai_compatible, openai_responses
from app.model_credentials import ModelCredentialCipher
from app.openai_compatible import HttpResponse


def test_personal_responses_probe_and_multiturn_research(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.setattr(openai_compatible, "_require_public_dns", lambda *_: None)
    monkeypatch.setattr(main.model_settings, "cipher", ModelCredentialCipher(b"p" * 32))
    wires = []
    def transport(method, url, headers, body, timeout, **kwargs):
        assert method == "POST" and url == "https://model.example.test/v1/responses"
        assert headers["Authorization"] == "Bearer fixture-secret"
        if kwargs["on_submission"]:
            kwargs["on_submission"]()
        wire = json.loads(body)
        assert wire["store"] is False and wire["reasoning"] == {"effort": "low"}
        assert wire["model"] == "custom" and "messages" not in wire and "reasoning_effort" not in wire
        tools = {t["name"] for t in wire["tools"] if t["type"] == "function" and t["strict"] is False}
        wires.append(wire)
        if len(wires) == 1:
            assert tools == {"verify_connection"}
            name, args = "verify_connection", {"value": "ok"}
        elif len(wires) == 2:
            assert "read_sources" in tools
            name, args = "read_sources", {"reads": [{"source_id": source_id}], "rationale": "filing"}
        else:
            assert "submit_research" in tools
            assert any(x.get("encrypted_content") == "opaque" for x in wire["input"])
            assert any(x.get("type") == "function_call" and x["call_id"] == "call-2" for x in wire["input"])
            assert any(x.get("type") == "function_call_output" and x["call_id"] == "call-2" and source_id in x["output"] for x in wire["input"])
            name, args = "submit_research", {"report": "Revenue was 100 USD.", "claims": [{
                "text": "Revenue was 100 USD.", "source_id": source_id, "quotation": "Revenue was 100 USD.", "role": "support"
            }], "limitations": [], "stopping_reason": "answered"}
        return HttpResponse(200, json.dumps({"status": "completed", "output": [
            {"type": "reasoning", "id": "rs1", "summary": [], "encrypted_content": "opaque"},
            {"type": "function_call", "id": "fc1", "name": name, "call_id": f"call-{len(wires)}", "arguments": json.dumps(args)},
        ], "usage": {"input_tokens": 30, "output_tokens": 10, "total_tokens": 40, "input_tokens_details": {"cached_tokens": 8}}}))

    monkeypatch.setattr(openai_responses, "_default_transport", transport)
    client = TestClient(main.app)
    auth = client.post("/v1/auth/local/register", json={"email": f"{uuid4().hex}@example.test",
                        "password": "correct horse battery staple!"}).json()
    headers = {"Authorization": "Bearer " + auth["access_token"]}
    config = {"mode": "personal", "model": "custom", "protocol": "openai_responses",
              "base_url": "https://model.example.test/v1/responses", "api_key": "fixture-secret",
              "generation_parameters": {"reasoning_effort": "low"}}
    path = "/v1/account/model-config"
    assert client.put(path, headers=headers, json=config).status_code == 200
    assert client.post(path + "/test", headers=headers).json() == {"status": "ready"}
    source_id = client.post("/v2/research/sources", headers=headers, json={"title": "Report", "text": "Revenue was 100 USD.",
        "permissions": {"store": True, "display": True, "model_input": True, "export": True}}).json()["source_id"]
    topic = client.post("/v2/research/topics", headers=headers, json={"question": "Revenue?", "source_ids": [source_id]}).json()
    path = "/v2/research/topics/" + topic["topic_id"] + "/runs"
    run = client.post(path, headers={**headers, "Idempotency-Key": uuid4().hex},
                      json={"parent_version_id": topic["versions"][0]["version_id"], "discover_sources": False})
    assert run.status_code == 202, run.text
    op = run.json()["operation"]
    for _ in range(200):
        op = client.get(path + "/" + op["operation_id"], headers=headers).json()["operation"]
        if op["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(.01)
    assert op["status"] == "completed" and len(wires) == 3
    assert main.RESEARCH_RUN_INPUTS[op["operation_id"]]["private_validation_diagnostics"]


def test_responses_rejection_and_incomplete_usage(monkeypatch):
    import pytest
    from app.model_protocol import ModelTurnInput
    from app.phase3 import ProviderFailure

    monkeypatch.setattr(openai_compatible, "_require_public_dns", lambda *_: None)
    settings = openai_compatible.OpenAICompatibleSettings.from_account_values(
        base_url="https://model.example.test/v1", model="custom", api_key="fixture-secret")
    provider = openai_responses.OpenAIResponsesProvider(settings)
    turn = ModelTurnInput("system", "user", (), ())
    responses = iter([
        HttpResponse(400, '{"error":{"param":"reasoning.effort","message":"private provider text"}}'),
        HttpResponse(200, '{"status":"incomplete","incomplete_details":{"reason":"max_output_tokens"},"usage":{"total_tokens":42}}'),
    ])
    def transport(method, url, headers, body, timeout, **kwargs):
        wire = json.loads(body)
        assert wire["max_output_tokens"] == 20 and "tools" not in wire and "reasoning" not in wire
        return next(responses)
    monkeypatch.setattr(openai_responses, "_default_transport", transport)
    with pytest.raises(ProviderFailure) as caught:
        provider.complete_turn(turn, max_output_tokens=20)
    assert (caught.value.upstream_status, caught.value.parameter) == (400, "reasoning.effort")
    with pytest.raises(ProviderFailure) as caught:
        provider.complete_turn(turn, max_output_tokens=20)
    assert caught.value.code == "provider_output_truncated" and caught.value.reported_tokens == 42
    assert caught.value.provider_response_received
