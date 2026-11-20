from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main


def _client(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    main.reset_state()
    client = TestClient(main.app)
    token = client.post("/v1/auth/local/register", json={"email": "caps@example.test", "password": "correct horse battery staple!"}).json()["access_token"]
    return client, {"Authorization": f"Bearer {token}"}


def test_capabilities_report_sec_and_research_model_without_configuration_values(monkeypatch) -> None:
    monkeypatch.delenv("FORESIGHTNINE_SEC_USER_AGENT", raising=False)
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.delenv("FORESIGHTNINE_FETCH_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("FORESIGHTNINE_FETCH_BLOCKED_HOSTS", raising=False)
    monkeypatch.setenv("FORESIGHTNINE_RESEARCH_MODEL_PROTOCOL", "anthropic_messages")
    monkeypatch.setenv("FORESIGHTNINE_RESEARCH_LLM_MODEL", "research-model")
    client, headers = _client(monkeypatch)
    assert client.get("/v2/research/capabilities").status_code == 401
    body = client.get("/v2/research/capabilities", headers=headers).json()
    assert body["sec_available"] is False
    assert body["web_fetch_enabled"] is True
    sec = {item["name"]: item for item in body["sources"]}
    assert sec["sec_filing"]["configured"] is False and sec["sec_filing"]["hosts"] == ["www.sec.gov"]
    assert sec["sec_company_concept"]["configured"] is False and sec["sec_company_concept"]["hosts"] == ["data.sec.gov"]
    assert sec["official_html_pdf"]["configured"] is True
    assert sec["official_html_pdf"]["coverage"] == "open_public_https"
    assert sec["official_html_pdf"]["hosts"] == []
    assert client.get("/v1/account", headers=headers).json()["research_model"] == "research-model"

    monkeypatch.setenv("FORESIGHTNINE_SEC_USER_AGENT", "Example Research contact@example.test")
    monkeypatch.setenv("FORESIGHTNINE_FETCH_ALLOWED_HOSTS", "www.bea.gov,ir.nvidia.com")
    monkeypatch.setenv("FORESIGHTNINE_FETCH_BLOCKED_HOSTS", "secret-internal-block.example.test")
    body = client.get("/v2/research/capabilities", headers=headers).json()
    assert body["sec_available"] is True
    assert body["web_fetch_enabled"] is False
    sec = {item["name"]: item for item in body["sources"]}
    assert sec["sec_filing"]["configured"] is True
    assert sec["sec_company_concept"]["configured"] is True
    assert sec["official_html_pdf"]["coverage"] == "restricted_hosts"
    assert sec["official_html_pdf"]["hosts"] == ["ir.nvidia.com", "www.bea.gov"]
    assert "contact@example.test" not in str(body)
    assert "secret-internal-block.example.test" not in str(body)


def test_messages_research_provider_respects_max_output_tokens_and_capability_override(monkeypatch) -> None:
    import json
    from app.openai_compatible import HttpResponse, OpenAICompatibleSettings
    from app.research_budget import ResearchExecutionBudget
    from app.research_coordinator import ResearchExecutionContext

    monkeypatch.setenv("FORESIGHTNINE_RESEARCH_MAX_OUTPUT_TOKENS", "16384")
    monkeypatch.setenv("FORESIGHTNINE_MESSAGES_VERSION", "2023-06-01")
    monkeypatch.setenv("FORESIGHTNINE_MESSAGES_AUTH_SCHEME", "bearer")
    monkeypatch.setenv("FORESIGHTNINE_MESSAGES_NATIVE_TOOLS", "true")
    monkeypatch.setenv("FORESIGHTNINE_MESSAGES_CONTINUATION", "true")
    monkeypatch.setenv("FORESIGHTNINE_MESSAGES_CAPABILITY_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("FORESIGHTNINE_MESSAGES_CAPABILITY_MODEL", "custom-reasoning-endpoint-model")
    monkeypatch.setenv("FORESIGHTNINE_MESSAGES_CAPABILITY_VERSION", "2023-06-01")

    settings = OpenAICompatibleSettings.from_values(
        base_url="https://models.example.test/v1",
        model="custom-reasoning-endpoint-model",
        api_key="fixture-key",
        allowlist="models.example.test",
    )
    provider = main._messages_research_provider(settings)
    assert provider.max_tokens == 16384
    assert provider.policy.max_output_tokens == 16384

    monkeypatch.setenv("FORESIGHTNINE_LLM_ENDPOINT_VERSION", "2023-06-01")
    monkeypatch.setenv("FORESIGHTNINE_LLM_CAPABILITY_PROFILE", json.dumps({
        "endpoint": "https://models.example.test/v1/messages",
        "model": "custom-reasoning-endpoint-model",
        "version": "2023-06-01",
        "mode": "json_object",
        "native_tools": True,
        "context_tokens": 131072,
        "output_tokens": 8192,
        "input_counter": "chars_div_3",
    }))
    ctx = ResearchExecutionContext(
        ResearchExecutionBudget(1, 0, None, 100), False,
        lambda: None, lambda: None, lambda *_: None, lambda *_: None, lambda: None, now=lambda: 0,
    )
    _, output_cap = ctx.prepare_model_call(provider, "x" * 180000, "sys")
    assert output_cap == 8192
    assert ctx.pending_reservation["estimated_input_tokens_heuristic"] == (180000 + 3 + 192 + 2) // 3

    sent = []
    provider.transport = lambda m, u, h, b, t: (
        sent.append(json.loads(b)),
        HttpResponse(200, json.dumps({"role": "assistant", "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5}})),
    )[1]
    from app.model_protocol import ModelTurnInput
    provider.complete_turn(ModelTurnInput("sys", "q", (), ()), max_output_tokens=output_cap)
    assert sent[-1]["max_tokens"] == 8192


def test_messages_provider_work_context_and_explicit_cache_control_do_not_break_admission(monkeypatch) -> None:
    import json
    from app.model_protocol import ModelTurnInput
    from app.openai_compatible import HttpResponse, OpenAICompatibleSettings
    from app.research_budget import ResearchExecutionBudget
    from app.research_coordinator import ResearchExecutionContext

    monkeypatch.setenv("FORESIGHTNINE_RESEARCH_MAX_OUTPUT_TOKENS", "16384")
    monkeypatch.setenv("FORESIGHTNINE_RESEARCH_LLM_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("FORESIGHTNINE_MESSAGES_VERSION", "2023-06-01")
    monkeypatch.setenv("FORESIGHTNINE_MESSAGES_AUTH_SCHEME", "bearer")
    monkeypatch.setenv("FORESIGHTNINE_MESSAGES_NATIVE_TOOLS", "true")
    monkeypatch.setenv("FORESIGHTNINE_MESSAGES_CONTINUATION", "true")
    monkeypatch.setenv("FORESIGHTNINE_MESSAGES_CAPABILITY_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("FORESIGHTNINE_MESSAGES_CAPABILITY_MODEL", "custom-reasoning-endpoint-model")
    monkeypatch.setenv("FORESIGHTNINE_MESSAGES_CAPABILITY_VERSION", "2023-06-01")

    settings = OpenAICompatibleSettings.from_values(
        base_url="https://models.example.test/v1",
        model="custom-reasoning-endpoint-model",
        api_key="fixture-key",
        allowlist="models.example.test",
    )
    provider = main._messages_research_provider(settings)
    base_key = provider.research_capacity_key()

    monkeypatch.setenv("FORESIGHTNINE_LLM_ENDPOINT_VERSION", "2023-06-01")
    monkeypatch.setenv("FORESIGHTNINE_LLM_CAPABILITY_PROFILE", json.dumps({
        "endpoint": "https://models.example.test/v1/messages",
        "model": "custom-reasoning-endpoint-model",
        "version": "2023-06-01",
        "mode": "json_object",
        "native_tools": True,
        "work_context_tokens": 32000,
        "output_tokens": 16384,
        "input_counter": "chars_div_3",
        "explicit_cache_control": True,
    }))
    assert provider.research_capacity_key() != base_key

    ctx = ResearchExecutionContext(
        ResearchExecutionBudget(1, 0, None, 100), False,
        lambda: None, lambda: None, lambda *_: None, lambda *_: None, lambda: None, now=lambda: 0,
    )
    # 180k chars exceeds work_context_tokens=32k, yet admission must NOT reject (only context_tokens gates admission).
    reservation, output_cap = ctx.prepare_model_call(provider, "x" * 180000, "sys")
    assert output_cap == 16384
    assert reservation is not None and reservation > 180000
    assert "context_input_tokens" not in ctx.pending_reservation

    sent = []
    provider.transport = lambda m, u, h, b, t: (
        sent.append(json.loads(b)),
        HttpResponse(200, json.dumps({"role": "assistant", "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5}})),
    )[1]
    provider.complete_turn(ModelTurnInput("sys", "q", (), (), tail_note="tail"), max_output_tokens=output_cap)
    assert sent[-1]["system"] == [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]
    assert sent[-1]["messages"] == [{
        "role": "user",
        "content": [
            {"type": "text", "text": "q", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "tail"},
        ],
    }]
