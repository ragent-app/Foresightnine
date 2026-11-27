"""Regression and hardening tests for main.py earnings squad orchestration glue (P0/P1/P2)."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from decimal import Decimal
import threading
import time
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from starlette.requests import Request

import app.main as api
from app.anthropic_messages import AnthropicMessagesProvider
from app.model_credentials import MessagesOptions, ModelConfigUpdate, ModelCredentialCipher, StoredModelConfig
from app.model_protocol import ModelResponse, ModelToolCall, ModelUsage
from app.openai_compatible import OpenAICompatibleLlmProvider, OpenAICompatibleSettings
from app.openai_responses import OpenAIResponsesProvider
from app.phase3 import ProviderFailure, ProviderPolicy
from app.provider_capabilities import ChatGenerationParameters
from app.research_budget import ResearchExecutionBudget
from app.research_earnings_squad import SlotBinding, SlotModelRequest, _compact_stale_worker_tool_messages
from app.research_fin_tools import FinDataToolbox, FinToolOutcome
from app.research_model_slots import ResolvedModelSlot
from app.research_sources import ResearchSource, SourceInput, SourcePermissions


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    api.reset_state()
    api.set_earnings_squad_runner_hook(None)
    yield
    api.set_earnings_squad_runner_hook(None)
    api.reset_state()


def _binding(slot: str = "fast_extractor", protocol: str = "openai_chat_completions") -> SlotBinding:
    return SlotBinding(
        slot=slot,  # type: ignore[arg-type]
        model="test-model",
        protocol=protocol,  # type: ignore[arg-type]
        native_tools=True,
        fallback_used=False,
    )


def _register_account(client: TestClient) -> tuple[Any, dict[str, str]]:
    email = f"hardening-{uuid4().hex[:10]}@example.test"
    resp = client.post(
        "/v1/auth/local/register",
        json={"email": email, "password": "correct horse battery staple!"},
    )
    assert resp.status_code in (200, 201), resp.text
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    account, _ = api.authenticate_local(api.ACCOUNTS, api.AUTH_SESSIONS, headers["Authorization"], now=api.lifecycle_clock.now())
    return account, headers


def _http_request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": raw_headers})


# --------------------------------------------------------------------------- 1. _AccountSlotModelRunner
def test_stale_evidence_compaction_reaches_provider_and_preserves_native_exchanges():
    turns = []

    class Provider:
        def complete_turn(self, turn):
            turns.append(turn)
            return ModelResponse(
                message={"role": "assistant", "content": "", "native_state": len(turns)},
                tool_calls=(), usage=ModelUsage(None, None, None), finish_reason="stop", stop_class="end",
            )

    runner = api._AccountSlotModelRunner(providers_by_slot={}, default_provider=Provider())
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "compare"}]
    original = json.dumps({"source_id": "annual", "text": "Company disclosure. " * 1200})
    for i in range(3):
        _compact_stale_worker_tool_messages(messages)
        runner.invoke(SlotModelRequest(
            slot="fast_extractor", stage="stage_2_workers", role="worker_segments",
            system_prompt="s", user_prompt="compare", messages=messages, binding=_binding(),
            turn_index=i, conversation_id="compare-filings",
        ))
        messages.extend([
            {"role": "assistant", "tool_calls": [{"id": f"read-{i}"}]},
            {"role": "tool", "tool_call_id": f"read-{i}", "content": original},
        ])
    assert turns[1].history[0].results[0].content == original
    old, recent = turns[2].history
    result = old.results[0]
    assert result.tool_call_id == "read-0"
    assert json.loads(result.content)["source_id"] == "annual"
    assert len(result.content.encode()) < len(original.encode()) / 10
    assert recent.results[0].content == original
    assert old.assistant.envelope["native_state"] == 1
    assert recent.assistant.envelope["native_state"] == 2


def test_account_slot_runner_forwards_runtime_schema_tokens_timeout_and_cancel():
    recorded_turns: list[Any] = []
    cancel_evt = threading.Event()

    class CapturingWireProvider:
        def complete_turn(self, turn):
            recorded_turns.append(turn)
            return ModelResponse(
                message={"role": "assistant", "content": '{"ok": true}'},
                tool_calls=(),
                usage=ModelUsage(10, 5, 15),
                finish_reason="stop",
                stop_class="end",
            )

    runner = api._AccountSlotModelRunner(
        providers_by_slot={},
        default_provider=CapturingWireProvider(),
        cancel_event=cancel_evt,
        slot_max_output_tokens={"writer": 20000},
    )

    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    runner.invoke(SlotModelRequest(
        slot="fast_extractor",
        stage="stage_2_workers",
        role="worker_segments",
        system_prompt="sys",
        user_prompt="extract",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "extract"}],
        tools=[],
        response_schema_name="WorkerOut",
        response_schema=schema,
        binding=_binding("fast_extractor"),
        conversation_id="conv-w1",
    ))
    runner.invoke(SlotModelRequest(
        slot="writer",
        stage="deep_report",
        role="dossier_writer",
        system_prompt="sys",
        user_prompt="write",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "write"}],
        tools=[],
        binding=_binding("writer"),
        conversation_id="conv-writer",
    ))

    assert len(recorded_turns) == 2
    t0, t1 = recorded_turns
    assert t0.response_schema == schema
    assert t0.response_schema_name == "WorkerOut"
    assert t0.max_output_tokens == api._SQUAD_STAGE_MAX_OUTPUT_TOKENS["stage_2_workers"]
    assert t0.timeout_seconds == api._SQUAD_STAGE_TIMEOUT_FLOORS["stage_2_workers"]
    assert t0.cancel_event is cancel_evt

    assert t1.max_output_tokens == 20000
    assert t1.timeout_seconds == api._SQUAD_STAGE_TIMEOUT_FLOORS["deep_report"]
    assert t1.cancel_event is cancel_evt


def test_account_slot_runner_clamps_timeout_to_budget_deadline_and_stops_when_exhausted():
    seen_timeouts: list[float] = []

    class DummyProvider:
        def complete_turn(self, turn):
            seen_timeouts.append(turn.timeout_seconds)
            return ModelResponse(
                message={"role": "assistant", "content": "{}"},
                tool_calls=(),
                usage=ModelUsage(1, 1, 2),
                finish_reason="stop",
                stop_class="end",
            )

    budget = ResearchExecutionBudget(None, None, None, time.monotonic() + 12.5)
    runner = api._AccountSlotModelRunner(
        providers_by_slot={},
        default_provider=DummyProvider(),
        budget=budget,
    )
    runner.invoke(SlotModelRequest(
        slot="bear_reviewer",
        stage="stage_3a_bear",
        role="bear_reviewer",
        system_prompt="sys",
        user_prompt="review",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "review"}],
        binding=_binding("bear_reviewer"),
    ))
    assert len(seen_timeouts) == 1
    assert 0 < seen_timeouts[0] <= 12.5

    expired_budget = ResearchExecutionBudget(None, None, None, time.monotonic() - 1.0)
    runner.bind_runtime(budget=expired_budget)
    with pytest.raises(ProviderFailure) as exc_info:
        runner.invoke(SlotModelRequest(
            slot="bear_reviewer",
            stage="stage_3a_bear",
            role="bear_reviewer",
            system_prompt="sys",
            user_prompt="review",
            messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "review"}],
            binding=_binding("bear_reviewer"),
        ))
    assert exc_info.value.code == "provider_budget_exhausted"
    assert exc_info.value.retryable is False


def test_account_slot_runner_extracts_anthropic_multipart_and_responses_text():
    class MultiProtocolProvider:
        def __init__(self, msg: dict[str, Any]):
            self.msg = msg

        def complete_turn(self, _turn):
            return ModelResponse(
                message=self.msg,
                tool_calls=(),
                usage=ModelUsage(10, 5, 15),
                finish_reason="end_turn",
                stop_class="end",
            )

    anthropic_runner = api._AccountSlotModelRunner(
        providers_by_slot={},
        default_provider=MultiProtocolProvider({
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "private chain"},
                {"type": "text", "text": '{"part": 1}'},
            ],
        }),
    )
    res_anth = anthropic_runner.invoke(SlotModelRequest(
        slot="fast_extractor",
        stage="stage_1_plan",
        role="lead",
        system_prompt="sys",
        user_prompt="plan",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "plan"}],
        binding=_binding("fast_extractor", "anthropic_messages"),
    ))
    assert res_anth.content == '{"part": 1}'

    responses_runner = api._AccountSlotModelRunner(
        providers_by_slot={},
        default_provider=MultiProtocolProvider({
            "role": "assistant",
            "output": [
                {"type": "reasoning", "id": "rs_1"},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": '{"resp": 2}'}]},
            ],
        }),
    )
    res_resp = responses_runner.invoke(SlotModelRequest(
        slot="deep_reasoner",
        stage="stage_1_plan",
        role="lead",
        system_prompt="sys",
        user_prompt="plan",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "plan"}],
        binding=_binding("deep_reasoner", "openai_responses"),
    ))
    assert res_resp.content == '{"resp": 2}'


def test_account_slot_runner_handles_non_tool_and_mixed_followups_with_anthropic_wire():
    import json

    sent_payloads: list[dict[str, Any]] = []

    class RecordingTransport:
        def __init__(self, responses: list[dict[str, Any]]) -> None:
            self._responses = list(responses)
            self.idx = 0

        def __call__(self, method, url, headers, body, timeout):
            from app.openai_compatible import HttpResponse
            sent_payloads.append(json.loads(body.decode("utf-8")))
            payload = self._responses[self.idx]
            self.idx += 1
            return HttpResponse(status_code=200, headers={}, body=json.dumps(payload))

    settings = OpenAICompatibleSettings.from_values(
        base_url="https://api.anthropic.example",
        model="claude-test",
        api_key="sk-ant-test-key-1234",
        allowlist="api.anthropic.example",
    )
    provider = AnthropicMessagesProvider(
        settings,
        ProviderPolicy(max_output_tokens=8192, timeout_seconds=120.0),
        anthropic_version="2023-06-01",
        auth_scheme="x-api-key",
        max_tokens=8192,
        transport=RecordingTransport([
            # Case A turn 0: invalid non-tool JSON
            {"id": "m1", "type": "message", "role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "not json"}], "usage": {"input_tokens": 5, "output_tokens": 2}},
            # Case A turn 1: repaired JSON
            {"id": "m2", "type": "message", "role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": '{"ok": true}'}], "usage": {"input_tokens": 8, "output_tokens": 3}},
            # Case B turn 0: tool call
            {"id": "m3", "type": "message", "role": "assistant", "stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "tu_1", "name": "sec_filing_tables", "input": {}}], "usage": {"input_tokens": 6, "output_tokens": 4}},
            # Case B turn 1: tool result + wrapup prompt -> final JSON
            {"id": "m4", "type": "message", "role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": '{"cards": []}'}], "usage": {"input_tokens": 9, "output_tokens": 5}},
        ]),
    )
    runner = api._AccountSlotModelRunner(providers_by_slot={}, default_provider=provider)

    # Case A: Schema repair follow-up turn without tool results
    r_a0 = runner.invoke(SlotModelRequest(
        slot="fast_extractor",
        stage="stage_1_plan",
        role="lead",
        system_prompt="sys",
        user_prompt="emit plan",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "emit plan"}],
        binding=_binding("fast_extractor", "anthropic_messages"),
        turn_index=0,
        conversation_id="repair-case",
    ))
    assert r_a0.content == "not json"

    r_a1 = runner.invoke(SlotModelRequest(
        slot="fast_extractor",
        stage="stage_1_plan",
        role="lead",
        system_prompt="sys",
        user_prompt="Fix JSON: missing key ok",
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "emit plan"},
            {"role": "assistant", "content": "not json"},
            {"role": "user", "content": "Fix JSON: missing key ok"},
        ],
        binding=_binding("fast_extractor", "anthropic_messages"),
        turn_index=1,
        conversation_id="repair-case",
    ))
    assert r_a1.content == '{"ok": true}'

    # Case B: Tool result + trailing user wrap-up turn together
    r_b0 = runner.invoke(SlotModelRequest(
        slot="fast_extractor",
        stage="stage_2_workers",
        role="worker_guidance",
        system_prompt="sys",
        user_prompt="analyze",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "analyze"}],
        tools=[{"name": "sec_filing_tables", "description": "Tables", "input_schema": {"type": "object", "properties": {}}}],
        binding=_binding("fast_extractor", "anthropic_messages"),
        turn_index=0,
        conversation_id="wrapup-case",
    ))
    assert len(r_b0.tool_calls) == 1

    r_b1 = runner.invoke(SlotModelRequest(
        slot="fast_extractor",
        stage="stage_2_workers",
        role="worker_guidance",
        system_prompt="sys",
        user_prompt="Tool limit reached. Emit final JSON now.",
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "analyze"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tu_1", "type": "function", "function": {"name": "sec_filing_tables", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "tu_1", "content": '{"rows": []}'},
            {"role": "user", "content": "Tool limit reached. Emit final JSON now."},
        ],
        tools=[],
        binding=_binding("fast_extractor", "anthropic_messages"),
        turn_index=1,
        conversation_id="wrapup-case",
    ))
    assert r_b1.content == '{"cards": []}'

    for payload in sent_payloads:
        for msg in payload["messages"]:
            assert msg["content"] != [], f"Empty Anthropic content list detected: {payload}"

    # Verify repair follow-up arrived cleanly as user text on payload[1]
    assert sent_payloads[1]["messages"][-1] == {"role": "user", "content": "Fix JSON: missing key ok"}
    # Verify wrap-up payload[3] combined tool_result AND trailing text block
    last_b1_blocks = sent_payloads[3]["messages"][-1]["content"]
    assert any(isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") == "tu_1" for b in last_b1_blocks)
    assert any(isinstance(b, dict) and b.get("type") == "text" and "Emit final JSON now." in b.get("text", "") for b in last_b1_blocks)


def test_account_slot_runner_exponential_backoff_retries_and_cancel_handling(monkeypatch):
    monkeypatch.setattr(api.random, "uniform", lambda _lo, _hi: 1.0)
    sleeps: list[float] = []

    class FlakyProvider:
        def __init__(self, fail_sequence: list[ProviderFailure]):
            self._fails = list(fail_sequence)
            self.calls = 0

        def complete_turn(self, _turn):
            self.calls += 1
            if self._fails:
                raise self._fails.pop(0)
            return ModelResponse(
                message={"role": "assistant", "content": '{"recovered": true}'},
                tool_calls=(),
                usage=ModelUsage(1, 1, 2),
                finish_reason="stop",
                stop_class="end",
            )

    req = SlotModelRequest(
        slot="fast_extractor",
        stage="stage_2_workers",
        role="worker_forensics",
        system_prompt="sys",
        user_prompt="run",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "run"}],
        binding=_binding("fast_extractor"),
    )

    flaky = FlakyProvider([
        ProviderFailure("provider_rate_limited", "429", retryable=True),
        ProviderFailure("provider_unavailable", "529", retryable=True),
    ])
    runner = api._AccountSlotModelRunner(
        providers_by_slot={},
        default_provider=flaky,
        max_transient_retries=2,
        retry_base_seconds=0.4,
        sleeper=sleeps.append,
    )
    resp = runner.invoke(req)
    assert resp.content == '{"recovered": true}'
    assert flaky.calls == 3
    assert len(sleeps) == 2
    assert sleeps == [5.0, 0.4]  # Independent 429 and transient failure schedules

    # Exhausting the independent 429 retry budget re-raises the last error
    sleeps.clear()
    always_429 = FlakyProvider([
        ProviderFailure("provider_rate_limited", "429 #1", retryable=True),
        ProviderFailure("provider_rate_limited", "429 #2", retryable=True),
        ProviderFailure("provider_rate_limited", "429 #3", retryable=True),
    ])
    runner_fail = api._AccountSlotModelRunner(
        providers_by_slot={},
        default_provider=always_429,
        max_rate_limit_retries=2,
        retry_base_seconds=0.1,
        sleeper=sleeps.append,
    )
    with pytest.raises(ProviderFailure) as exc_fail:
        runner_fail.invoke(req)
    assert "429 #3" in str(exc_fail.value)
    assert always_429.calls == 3

    # provider_cancelled is NEVER retried even when retryable=True
    sleeps.clear()
    cancelled_provider = FlakyProvider([
        ProviderFailure("provider_cancelled", "cancelled", retryable=True),
    ])
    runner_cancel = api._AccountSlotModelRunner(
        providers_by_slot={},
        default_provider=cancelled_provider,
        max_transient_retries=2,
        sleeper=sleeps.append,
    )
    with pytest.raises(ProviderFailure) as exc_cancel:
        runner_cancel.invoke(req)
    assert exc_cancel.value.code == "provider_cancelled"
    assert cancelled_provider.calls == 1
    assert sleeps == []

    # Setting cancel_event inside attempt 1 aborts backoff retry loop cleanly
    cancel_evt = threading.Event()

    class CancelDuringFailureProvider:
        def __init__(self) -> None:
            self.calls = 0

        def complete_turn(self, _turn):
            self.calls += 1
            cancel_evt.set()
            raise ProviderFailure("provider_rate_limited", "429 during cancel", retryable=True)

    sleeps.clear()
    c_prov = CancelDuringFailureProvider()
    runner_mid_cancel = api._AccountSlotModelRunner(
        providers_by_slot={},
        default_provider=c_prov,
        cancel_event=cancel_evt,
        max_transient_retries=2,
        sleeper=sleeps.append,
    )
    with pytest.raises(ProviderFailure):
        runner_mid_cancel.invoke(req)
    assert c_prov.calls == 1
    assert sleeps == []


def test_account_slot_runner_steering_cancel_redispatch_race_guard():
    stale_entered = threading.Event()
    release_stale = threading.Event()

    class RacyProvider:
        def complete_turn(self, turn):
            if turn.user == "OLD_ATTEMPT":
                stale_entered.set()
                release_stale.wait(timeout=2.0)
                return ModelResponse(
                    message={"role": "assistant", "content": '{"stale": true}'},
                    tool_calls=(),
                    usage=ModelUsage(1, 1, 2),
                    finish_reason="stop",
                    stop_class="end",
                )
            if len(turn.history) == 0:
                return ModelResponse(
                    message={"role": "assistant", "content": "NEW_MSG_0"},
                    tool_calls=(),
                    usage=ModelUsage(1, 1, 2),
                    finish_reason="stop",
                    stop_class="end",
                )
            return ModelResponse(
                message={"role": "assistant", "content": turn.history[0].assistant.envelope["content"] + "+FOLLOWUP"},
                tool_calls=(),
                usage=ModelUsage(1, 1, 2),
                finish_reason="stop",
                stop_class="end",
            )

    runner = api._AccountSlotModelRunner(providers_by_slot={}, default_provider=RacyProvider())
    stale_req = SlotModelRequest(
        slot="fast_extractor",
        stage="stage_2_workers",
        role="worker_forensics",
        system_prompt="sys",
        user_prompt="OLD_ATTEMPT",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "OLD_ATTEMPT"}],
        binding=_binding("fast_extractor"),
        turn_index=0,
        conversation_id="shared-conv",
    )
    new_req_0 = SlotModelRequest(
        slot="fast_extractor",
        stage="stage_2_workers",
        role="worker_forensics",
        system_prompt="sys",
        user_prompt="NEW_ATTEMPT",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "NEW_ATTEMPT"}],
        binding=_binding("fast_extractor"),
        turn_index=0,
        conversation_id="shared-conv",
    )
    new_req_1 = SlotModelRequest(
        slot="fast_extractor",
        stage="stage_2_workers",
        role="worker_forensics",
        system_prompt="sys",
        user_prompt="repair",
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "NEW_ATTEMPT"},
            {"role": "assistant", "content": "NEW_MSG_0"},
            {"role": "user", "content": "repair"},
        ],
        binding=_binding("fast_extractor"),
        turn_index=1,
        conversation_id="shared-conv",
    )

    t_stale = threading.Thread(target=lambda: runner.invoke(stale_req))
    t_stale.start()
    assert stale_entered.wait(timeout=2.0)

    r_new_0 = runner.invoke(new_req_0)
    assert r_new_0.content == "NEW_MSG_0"

    # Let the superseded thread finish AFTER the steering reset succeeded; it must NOT overwrite _turns
    release_stale.set()
    t_stale.join(timeout=2.0)

    r_new_1 = runner.invoke(new_req_1)
    assert r_new_1.content == "NEW_MSG_0+FOLLOWUP"


# --------------------------------------------------------------------------- 2. _build_squad_slot_provider & Timeout Floor
def test_build_squad_slot_provider_forwards_tools_verified_gen_params_and_messages_options(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.openai_compatible._require_public_dns", lambda host, port: ["93.184.216.34"])
    monkeypatch.delenv("FORESIGHTNINE_LLM_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("FORESIGHTNINE_SQUAD_LLM_TIMEOUT_SECONDS", raising=False)

    gen_primary = ChatGenerationParameters(temperature=0.2, reasoning_effort="medium")
    primary_settings = OpenAICompatibleSettings.from_account_values(
        base_url="https://api.example.test/v1",
        model="gpt-primary",
        api_key="sk-test-primary-1234",
    )
    from dataclasses import replace
    primary_settings = replace(primary_settings, tools_verified=True, generation_parameters=gen_primary)
    primary_config = StoredModelConfig(
        account_id="acct_12345678",
        mode="personal",
        protocol="anthropic_messages",
        messages_options=MessagesOptions(version="2023-06-01", auth_scheme="bearer", max_tokens=8192, effort="medium"),
        generation_parameters=gen_primary,
        base_url="https://api.example.test/v1",
        model="gpt-primary",
        encrypted_api_key="enc:" + ("a" * 40),
        key_hint="••••1234",
        updated_at=datetime.now(timezone.utc),
    )

    # 1) Anthropic Messages override inherits primary messages_options when omitted, respects slot overrides when given
    anth_slot = ResolvedModelSlot(
        slot="writer",
        role="dossier_writer",
        fallback_used=False,
        source="slot_override",
        protocol="anthropic_messages",
        base_url="https://api.example.test/v1",
        model="claude-writer",
        credential_ref="account_primary",
        native_tools_supported=True,
        capability_valid=True,
        endpoint_version="2024-10-22",
        messages_effort="high",
        differs_from_primary=True,
    )
    p_anth = api._build_squad_slot_provider("acct_12345678", anth_slot, primary_settings, primary_config)
    assert isinstance(p_anth, AnthropicMessagesProvider)
    assert p_anth.settings.tools_verified is True
    assert p_anth.settings.generation_parameters == gen_primary
    assert p_anth.anthropic_version == "2024-10-22"
    assert p_anth.auth_scheme == "bearer"
    assert p_anth.max_tokens == 8192
    assert p_anth.effort == "high"
    assert p_anth.policy.timeout_seconds >= 90.0

    # 2) OpenAI Responses override gets slot generation_parameters, tools_verified, and squad timeout floor
    gen_resp = ChatGenerationParameters(temperature=0.0, reasoning_effort="high")
    resp_slot = ResolvedModelSlot(
        slot="deep_reasoner",
        role="lead_coordinator",
        fallback_used=False,
        source="slot_override",
        protocol="openai_responses",
        base_url="https://api.example.test/v1",
        model="o3-reasoner",
        credential_ref="account_primary",
        native_tools_supported=True,
        capability_valid=True,
        generation_parameters=gen_resp,
        differs_from_primary=True,
    )
    p_resp = api._build_squad_slot_provider("acct_12345678", resp_slot, primary_settings, primary_config)
    assert isinstance(p_resp, OpenAIResponsesProvider)
    assert p_resp.settings.tools_verified is True
    assert p_resp.settings.generation_parameters == gen_resp
    assert p_resp.policy.timeout_seconds >= 90.0

    # 3) OpenAI Chat Completions override gets tools_verified, generation_parameters, and squad timeout floor
    gen_chat = ChatGenerationParameters(temperature=0.1, top_p=0.9)
    chat_slot = ResolvedModelSlot(
        slot="fast_extractor",
        role="worker_segments",
        fallback_used=False,
        source="slot_override",
        protocol="openai_chat_completions",
        base_url="https://api.example.test/v1",
        model="gpt-fast",
        credential_ref="account_primary",
        native_tools_supported=True,
        capability_valid=True,
        generation_parameters=gen_chat,
        differs_from_primary=True,
    )
    p_chat = api._build_squad_slot_provider("acct_12345678", chat_slot, primary_settings, primary_config)
    assert isinstance(p_chat, OpenAICompatibleLlmProvider)
    assert p_chat.settings.tools_verified is True
    assert p_chat.settings.generation_parameters == gen_chat
    assert p_chat.policy.timeout_seconds >= 90.0


def test_with_squad_provider_timeout_floor_does_not_mutate_shared_challenge_provider(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FORESIGHTNINE_LLM_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("FORESIGHTNINE_SQUAD_LLM_TIMEOUT_SECONDS", raising=False)
    shared = OpenAICompatibleLlmProvider(policy=ProviderPolicy(timeout_seconds=20.0))
    cloned = api._with_squad_provider_timeout_floor(shared)
    assert shared.policy.timeout_seconds == 20.0
    assert cloned is not shared
    assert cloned.policy.timeout_seconds >= 90.0


# --------------------------------------------------------------------------- 3. _topic_attached_sources_for_squad
def test_topic_attached_sources_filters_ineligible_simulated_fred_and_post_as_of():
    client = TestClient(api.app)
    account, headers = _register_account(client)
    t_before = datetime(2026, 6, 1, tzinfo=timezone.utc)
    t_as_of = datetime(2026, 7, 1, tzinfo=timezone.utc)
    t_after = datetime(2026, 8, 1, tzinfo=timezone.utc)

    s_eligible = api.research_sources.create(account.account_id, SourceInput(
        title="Eligible Q2 10-Q",
        text="Revenue grew 20% YoY.",
        kind="official",
        published_at=t_before,
        permissions=SourcePermissions(store=True, display=True, model_input=True, export=True),
    ))
    s_no_model = api.research_sources.create(account.account_id, SourceInput(
        title="No Model Input Source",
        text="Restricted text.",
        kind="user_supplied",
        published_at=t_before,
        permissions=SourcePermissions(store=True, display=True, model_input=False, export=False),
    ))
    s_no_display = api.research_sources.create(account.account_id, SourceInput(
        title="No Display Source",
        text="Hidden text.",
        kind="user_supplied",
        published_at=t_before,
        permissions=SourcePermissions(store=True, display=False, model_input=True, export=False),
    ))
    s_sim = api.research_sources.create(account.account_id, SourceInput(
        title="Simulated Draft",
        text="Synthetic scenario.",
        kind="user_supplied",
        is_simulated=True,
        published_at=t_before,
        permissions=SourcePermissions(store=True, display=True, model_input=True, export=True),
    ))
    s_fred = api.research_sources.create(account.account_id, SourceInput(
        title="FRED Series",
        text="GDP data",
        url="https://fred.stlouisfed.org/series/GDP",
        kind="official",
        published_at=t_before,
        permissions=SourcePermissions(store=True, display=True, model_input=True, export=True),
    ))
    s_future = api.research_sources.create(account.account_id, SourceInput(
        title="Future Filing",
        text="Post as_of release.",
        kind="official",
        published_at=t_after,
        permissions=SourcePermissions(store=True, display=True, model_input=True, export=True),
    ))

    topic_resp = client.post(
        "/v2/research/topics",
        headers=headers,
        json={
            "question": "Analyze ACME Q2",
            "as_of": t_as_of.isoformat(),
            "source_ids": [
                s_eligible.source_id,
                s_no_model.source_id,
                s_no_display.source_id,
                s_sim.source_id,
                s_fred.source_id,
                s_future.source_id,
            ],
        },
    )
    assert topic_resp.status_code == 201, topic_resp.text
    topic = api.research_topics.get(account.account_id, topic_resp.json()["topic_id"])

    source_by_id, texts, perms = api._topic_attached_sources_for_squad(account.account_id, topic)
    assert set(source_by_id.keys()) == {s_eligible.source_id}
    assert set(texts.keys()) == {s_eligible.source_id}
    assert set(perms.keys()) == {s_eligible.source_id}

    # Stricter request as_of excludes even s_eligible
    strict_as_of = datetime(2026, 5, 1, tzinfo=timezone.utc)
    s2, t2, p2 = api._topic_attached_sources_for_squad(account.account_id, topic, as_of=strict_as_of)
    assert s2 == {} and t2 == {} and p2 == {}


# --------------------------------------------------------------------------- 4. Startup / Periodic Orphan Reconciliation
def test_reconcile_research_work_transitions_orphaned_squad_when_research_work_empty():
    assert dict(api.RESEARCH_WORK) == {}

    client = TestClient(api.app)
    account, headers = _register_account(client)
    topic_resp = client.post("/v2/research/topics", headers=headers, json={"question": "Analyze ACME"})
    assert topic_resp.status_code == 201
    topic_id = topic_resp.json()["topic_id"]

    # Orphaned squad with checkpoint
    orphan_key = api._squad_state_key(account.account_id, topic_id)
    api.EARNINGS_SQUAD_STATES.put(orphan_key, {
        "account_id": account.account_id,
        "topic_id": topic_id,
        "run_id": "sqrun_orphan_1",
        "operation_id": "op_orphan_1",
        "run_mode": "earnings_squad",
        "status": "running",
        "checkpoint": {"run_id": "sqrun_orphan_1", "completed_stages": ["stage_0_prefetch", "stage_1_plan"]},
        "checkpoint_completed_stages": ["stage_0_prefetch", "stage_1_plan"],
        "org_events": [],
        "error": None,
        "updated_at": api.lifecycle_clock.now().isoformat(),
    })
    api.RESEARCH_RUN_INPUTS.put("op_orphan_1", {
        "run_mode": "earnings_squad",
        "squad_run_id": "sqrun_orphan_1",
    })
    api.TURN_STATUS.put(topic_id, {
        "owner_id": account.account_id,
        "topic_id": topic_id,
        "turn_id": "turn_orphan_1",
        "current_operation_id": "op_orphan_1",
        "logical_status": "running",
        "run_mode": "earnings_squad",
    })

    # Live in-process squad must remain untouched
    live_topic_id = "top_live_123456"
    live_key = api._squad_state_key(account.account_id, live_topic_id)
    api.EARNINGS_SQUAD_STATES.put(live_key, {
        "account_id": account.account_id,
        "topic_id": live_topic_id,
        "run_id": "sqrun_live_1",
        "operation_id": "op_live_1",
        "run_mode": "earnings_squad",
        "status": "running",
        "checkpoint_completed_stages": [],
        "org_events": [],
        "error": None,
        "updated_at": api.lifecycle_clock.now().isoformat(),
    })
    with api._ACTIVE_SQUAD_LOCK:
        api._ACTIVE_SQUAD_RUNS[(account.account_id, live_topic_id)] = {
            "run_id": "sqrun_live_1",
            "operation_id": "op_live_1",
            "orchestrator": None,
        }

    # RESEARCH_WORK is empty; _reconcile_research_work must still reconcile orphans!
    api._reconcile_research_work()

    reconciled_row = api.EARNINGS_SQUAD_STATES.get(orphan_key)
    assert reconciled_row["status"] == "failed"
    assert reconciled_row["error_code"] == "worker_interrupted_by_restart"
    assert reconciled_row["can_resume_checkpoint"] is True
    assert reconciled_row["checkpoint_completed_stages"] == ["stage_0_prefetch", "stage_1_plan"]
    assert reconciled_row["checkpoint"] == {"run_id": "sqrun_orphan_1", "completed_stages": ["stage_0_prefetch", "stage_1_plan"]}
    assert "stage_0_prefetch" in reconciled_row["error"]
    assert "Resume from checkpoint" in reconciled_row["error"]
    assert api.TURN_STATUS.get(topic_id)["logical_status"] == "failed"

    live_row = api.EARNINGS_SQUAD_STATES.get(live_key)
    assert live_row["status"] == "running"


def test_reconcile_startup_lifespan_and_topic_lock_release():
    with TestClient(api.app) as client:
        account, headers = _register_account(client)
        topic_resp = client.post("/v2/research/topics", headers=headers, json={"question": "Analyze NVDA"})
        assert topic_resp.status_code == 201
        topic_id = topic_resp.json()["topic_id"]

    # Simulate pre-existing orphaned running state + stale pending operation record on restart
    state_key = api._squad_state_key(account.account_id, topic_id)
    api.EARNINGS_SQUAD_STATES.put(state_key, {
        "account_id": account.account_id,
        "topic_id": topic_id,
        "run_id": "sqrun_restart_1",
        "operation_id": "op_restart_1",
        "run_mode": "earnings_squad",
        "status": "running",
        "checkpoint_completed_stages": ["stage_0_prefetch"],
        "org_events": [],
        "error": None,
        "updated_at": api.lifecycle_clock.now().isoformat(),
    })
    api.RESEARCH_RUN_INPUTS.put("op_restart_1", {
        "run_mode": "earnings_squad",
        "squad_run_id": "sqrun_restart_1",
    })
    # Simulate an operation record still marked pending in store before lease expiry
    api.operations.store._operations["op_restart_1"] = {
        "operation_id": "op_restart_1",
        "endpoint": f"/v1/research/topics/{topic_id}/runs",
        "owner_subject": f"account:{account.account_id}",
        "status": "running",
    }
    api.operations.store._sessions["op_restart_1"] = topic_id
    assert api._topic_has_active_run(account.account_id, topic_id) is True

    # Booting FastAPI lifespan runs startup orphan reconciliation
    with TestClient(api.app):
        row = api.EARNINGS_SQUAD_STATES.get(state_key)
        assert row is not None and row["status"] == "failed"
        assert api._topic_has_active_run(account.account_id, topic_id) is False


# --------------------------------------------------------------------------- 5. market_price_fn Wiring
def test_resolve_earnings_squad_runtime_wires_market_price_fn_and_graceful_fallback(monkeypatch: pytest.MonkeyPatch):
    client = TestClient(api.app)
    account, headers = _register_account(client)
    as_of_dt = datetime(2026, 6, 15, tzinfo=timezone.utc)
    topic_resp = client.post(
        "/v2/research/topics",
        headers=headers,
        json={"question": "Analyze AAPL Q2", "as_of": as_of_dt.isoformat()},
    )
    assert topic_resp.status_code == 201
    topic = api.research_topics.get(account.account_id, topic_resp.json()["topic_id"])

    seen_specs: list[Any] = []

    def fake_market_data(self, spec):
        seen_specs.append(spec)
        src = ResearchSource(
            source_id="src_mkt_1",
            owner_id=account.account_id,
            title="AAPL 1day bars",
            text="AAPL@NASDAQ | 2026-06-14 | 1day | O 185.00 H 188.00 L 184.50 C 187.45 V 1000000 USD",
            kind="market_data",
            content_sha256="a" * 64,
            obtained_at=datetime.now(timezone.utc),
            permissions=SourcePermissions(store=True, display=True, model_input=True, export=False),
        )
        return FinToolOutcome({"status": "ok", "source_id": "src_mkt_1"}, source=src)

    monkeypatch.setattr(FinDataToolbox, "market_data", fake_market_data)

    cp_store = api._RepoSquadCheckpointStore(
        account_id=account.account_id,
        topic_id=topic.topic_id,
        state_lock_key=api._squad_state_key(account.account_id, topic.topic_id),
    )
    orch, _summary = api._resolve_earnings_squad_runtime(
        account=account,
        topic=topic,
        request=None,
        http_request=_http_request(headers),
        operation_id="sqop_test_mkt",
        run_id="sqrun_test_mkt",
        checkpoint_store=cp_store,
        org_sink=lambda _ev: None,
    )
    assert orch._prefetch_deps.market_price_fn is not None

    price = orch._prefetch_deps.market_price_fn("cik:0000320193", "AAPL")
    assert price == Decimal("187.45")
    assert len(seen_specs) == 1
    assert seen_specs[0].symbol == "AAPL"
    assert seen_specs[0].end == as_of_dt.date()

    # Graceful None when market_data unavailable or raises
    monkeypatch.setattr(FinDataToolbox, "market_data", lambda self, spec: FinToolOutcome({"status": "unavailable"}))
    assert orch._prefetch_deps.market_price_fn("cik:0000320193", "AAPL") is None

    def boom_market_data(self, spec):
        raise RuntimeError("transient network failure")

    monkeypatch.setattr(FinDataToolbox, "market_data", boom_market_data)
    assert orch._prefetch_deps.market_price_fn("cik:0000320193", "AAPL") is None


# --------------------------------------------------------------------------- 6. Personal generation_parameters Regression (topic_11e55b1df00649e8865baf53f2b7ddd9)
def test_resolve_earnings_squad_runtime_personal_chat_generation_params_without_max_output_tokens(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("app.openai_compatible._require_public_dns", lambda host, port: ["93.184.216.34"])
    monkeypatch.setattr(api.model_settings, "cipher", ModelCredentialCipher(b"p" * 32))

    client = TestClient(api.app)
    account, headers = _register_account(client)
    topic_resp = client.post(
        "/v2/research/topics",
        headers=headers,
        json={"question": "Analyze MSFT Q2 FY26 earnings"},
    )
    assert topic_resp.status_code == 201, topic_resp.text
    topic = api.research_topics.get(account.account_id, topic_resp.json()["topic_id"])

    # 1) Exact real-world reproduction: personal openai_chat_completions config with ChatGenerationParameters
    # (which defines only reasoning_effort/temperature/top_p/seed and has no max_output_tokens attr).
    gen_params = ChatGenerationParameters(temperature=0.2, reasoning_effort="medium")
    assert not hasattr(gen_params, "max_output_tokens")
    api.model_settings.update(
        account.account_id,
        ModelConfigUpdate(
            mode="personal",
            protocol="openai_chat_completions",
            base_url="https://api.example.test/v1",
            model="gpt-4o",
            api_key="sk-test-personal-1234",
            generation_parameters=gen_params,
        ),
    )
    stored_cfg = api.model_settings._stored(account.account_id)
    assert stored_cfg is not None
    assert api.model_settings.record_tools_result(stored_cfg, True)

    cp_store = api._RepoSquadCheckpointStore(
        account_id=account.account_id,
        topic_id=topic.topic_id,
        state_lock_key=api._squad_state_key(account.account_id, topic.topic_id),
    )
    orch_real, _summary = api._resolve_earnings_squad_runtime(
        account=account,
        topic=topic,
        request=None,
        http_request=_http_request(headers),
        operation_id="sqop_personal_gen_params",
        run_id="sqrun_personal_gen_params",
        checkpoint_store=cp_store,
        org_sink=lambda _ev: None,
    )
    assert orch_real._model_runner._slot_max_output_tokens == {}
    assert orch_real._model_runner._resolve_max_output_tokens(
        SlotModelRequest(
            slot="fast_extractor",
            stage="stage_2_workers",
            role="worker_segments",
            system_prompt="sys",
            user_prompt="extract",
            messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "extract"}],
            binding=_binding("fast_extractor"),
        )
    ) == api._SQUAD_STAGE_MAX_OUTPUT_TOKENS["stage_2_workers"]

    # 2) Fallback resolution when generation_parameters exposes max_tokens=4096 (no max_output_tokens attr)
    real_settings, real_config = api.model_settings.runtime_configuration(account.account_id)
    assert real_settings is not None and real_config is not None
    max_tokens_gen = SimpleNamespace(max_tokens=4096)
    config_with_max_tokens = StoredModelConfig.model_construct(
        **{**real_config.model_dump(), "generation_parameters": max_tokens_gen}
    )
    monkeypatch.setattr(
        api.model_settings,
        "runtime_configuration",
        lambda _aid: (real_settings, config_with_max_tokens),
    )
    monkeypatch.setattr(api.model_settings, "primary_slot_profile", lambda _aid: None)

    orch_fallback, _summary2 = api._resolve_earnings_squad_runtime(
        account=account,
        topic=topic,
        request=None,
        http_request=_http_request(headers),
        operation_id="sqop_personal_max_tokens",
        run_id="sqrun_personal_max_tokens",
        checkpoint_store=cp_store,
        org_sink=lambda _ev: None,
    )
    assert orch_fallback._model_runner._slot_max_output_tokens == {
        "fast_extractor": 4096,
        "deep_reasoner": 4096,
        "bear_reviewer": 4096,
        "writer": 4096,
    }

    # 3) _build_squad_slot_provider anthropic_messages slot honors max_tokens / max_completion_tokens safely
    anth_slot = ResolvedModelSlot(
        slot="writer",
        role="dossier_writer",
        fallback_used=False,
        source="slot_override",
        protocol="anthropic_messages",
        base_url="https://api.example.test/v1",
        model="claude-writer",
        credential_ref="account_primary",
        native_tools_supported=True,
        capability_valid=True,
        differs_from_primary=True,
    )
    object.__setattr__(anth_slot, "generation_parameters", max_tokens_gen)
    p_anth = api._build_squad_slot_provider(account.account_id, anth_slot, real_settings, real_config)
    assert isinstance(p_anth, AnthropicMessagesProvider)
    assert p_anth.max_tokens == 4096


# --------------------------------------------------------------------------- 7. Live OpenAIResponses End-to-End Squad + Rejection Diagnostics (topic_11e55b1df00649e8865baf53f2b7ddd9)
def test_openai_responses_squad_e2e_and_safe_rejection_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import json
    import logging
    from app import openai_compatible, openai_responses
    from app.openai_compatible import HttpResponse

    monkeypatch.setattr(api, "_prewarm_v25_layer1_async", lambda _dossier: None)
    monkeypatch.setattr(openai_compatible, "_require_public_dns", lambda *_: None)
    monkeypatch.setattr(api.model_settings, "cipher", ModelCredentialCipher(b"r" * 32))
    client = TestClient(api.app)
    reg = client.post("/v1/auth/local/register", json={
        "email": f"{uuid4().hex}@example.test",
        "password": "correct horse battery staple!",
    })
    assert reg.status_code == 201
    headers = {"Authorization": "Bearer " + reg.json()["access_token"]}

    # 1) Configure personal openai_responses with reasoning_effort="medium" and verify tool support
    cfg_resp = client.put("/v1/account/model-config", headers=headers, json={
        "mode": "personal",
        "model": "gpt-5.6-terra",
        "protocol": "openai_responses",
        "base_url": "https://model.example.test/v1",
        "api_key": "sk-resp-e2e-secret",
        "generation_parameters": {"reasoning_effort": "medium"},
    })
    assert cfg_resp.status_code == 200
    monkeypatch.setattr(
        openai_responses,
        "_default_transport",
        lambda m, u, h, b, t, **kw: HttpResponse(200, json.dumps({
            "status": "completed",
            "output": [{
                "type": "function_call",
                "id": "fc_probe",
                "name": "verify_connection",
                "call_id": "call_probe",
                "arguments": '{"value":"ok"}',
            }],
        })),
    )
    assert client.post("/v1/account/model-config/test", headers=headers).json() == {"status": "ready"}

    src_resp = client.post("/v2/research/sources", headers=headers, json={
        "title": "ACME Q4 Earnings Release",
        "text": "ACME Q4 revenue rose 22% YoY to $1,500M driven by Cloud Subscriptions ($900M).",
        "permissions": {"store": True, "display": True, "model_input": True, "export": True},
    })
    assert src_resp.status_code == 201
    source_id = src_resp.json()["source_id"]

    topic_resp = client.post("/v2/research/topics", headers=headers, json={
        "question": "Analyze ACME Q4 earnings drivers",
        "source_ids": [source_id],
    })
    assert topic_resp.status_code in (200, 201)
    topic = topic_resp.json()
    topic_id = topic["topic_id"]

    # 2) Simulate HTTP 400 on Stage 1 due to unrecoverable 'input' rejection -> verify logs & safe rejection_details
    def fail_transport(m, u, h, b, t, **kw):
        return HttpResponse(400, json.dumps({
            "error": {
                "type": "invalid_request_error",
                "code": "invalid_value",
                "param": "input",
                "message": "Invalid input for Bearer sk-resp-e2e-secret",
            }
        }))

    monkeypatch.setattr(openai_responses, "_default_transport", fail_transport)
    caplog.set_level(logging.WARNING)
    bad_run = client.post(f"/v2/research/topics/{topic_id}/earnings-squad/run", headers=headers, json={
        "symbol": "ACME",
        "cik": "0001234567",
        "company_name": "ACME Corp",
        "execution_mode": "sync",
    })
    assert bad_run.status_code == 500
    assert "sk-resp-e2e-secret" not in caplog.text
    assert "provider_request_rejected" in caplog.text
    assert "'error_param': 'input'" in caplog.text or '"error_param": "input"' in caplog.text
    bad_dossier = client.get(f"/v2/research/topics/{topic_id}/earnings-squad/dossier", headers=headers).json()
    assert bad_dossier["status"] == "failed"
    assert bad_dossier["error"] == "Provider rejected the request."
    assert bad_dossier["rejection_details"]["status_code"] == 400
    assert bad_dossier["rejection_details"]["error_param"] == "input"
    assert "sk-resp-e2e-secret" not in json.dumps(bad_dossier)

    # 3) Full 6-stage OpenAIResponses squad run: Stage 1 -> Stage 2 tool loops -> Stage 2.5 -> Stage 3A -> Stage 3B Memo
    wires_seen: list[dict] = []

    def squad_responses_transport(m, u, h, b, t, **kw):
        wire = json.loads(b)
        wires_seen.append(wire)
        assert wire["reasoning"] == {"effort": "medium"}
        if wire.get("text", {}).get("format", {}).get("type") == "json_object":
            # Every OpenAI Responses json_object wire MUST contain 'json' in wire['input']!
            assert "json" in json.dumps(wire["input"]).lower()
        instr = wire.get("instructions") or ""
        if "ResearchDispatchPlan" in instr:
            payload = {
                "l1_pack_id": "earnings_review",
                "selected_l2_pack_id": None,
                "selected_l2_pack_reason": "Single filing review",
                "tasks": [{
                    "worker_role": "worker_segments",
                    "status": "planned",
                    "objective": "Verify ACME Q4 revenue",
                    "target_r_files": [],
                    "target_flag_ids": [],
                    "target_section_keys": [],
                    "soft_turn_limit": 3,
                    "round_number": 1,
                }],
                "cross_worker_hypotheses": [],
                "planning_notes": "Targeted segment check",
                "instruction_sequence": 0,
            }
            return HttpResponse(200, json.dumps({
                "status": "completed",
                "output": [{"type": "message", "id": "m_s1", "role": "assistant", "content": [{"type": "output_text", "text": json.dumps(payload)}]}],
                "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            }))
        if "EvidenceCardBatch" in instr:
            has_tool_result = any(item.get("type") == "function_call_output" for item in wire["input"])
            if not has_tool_result and wire.get("tools"):
                return HttpResponse(200, json.dumps({
                    "status": "completed",
                    "output": [
                        {"type": "reasoning", "id": "rs_w", "summary": [], "encrypted_content": "enc-worker-1"},
                        {
                            "type": "function_call",
                            "id": "fc_w",
                            "call_id": "call_read_1",
                            "name": "read_sources",
                            "arguments": json.dumps({"reads": [{"source_id": source_id, "start": 0, "end": 200}], "rationale": "Read release"}),
                        },
                    ],
                }))
            batch = {
                "task_id": "t_seg",
                "worker_role": "worker_segments",
                "round_number": 1,
                "status": "completed",
                "cards": [{
                    "card_id": "c_rev",
                    "worker_role": "worker_segments",
                    "topic_tag": "revenue",
                    "finding": "ACME Q4 revenue rose 22% YoY to $1,500M.",
                    "citations": [{"source_id": source_id, "quotation": "ACME Q4 revenue rose 22% YoY to $1,500M"}],
                }],
                "unresolved_gaps": [],
            }
            return HttpResponse(200, json.dumps({
                "status": "completed",
                "output": [{"type": "message", "id": "m_s2", "role": "assistant", "content": [{"type": "output_text", "text": json.dumps(batch)}]}],
            }))
        if "Stage25ReviewDecision" in instr:
            return HttpResponse(200, json.dumps({
                "status": "completed",
                "output": [{"type": "message", "id": "m_s25", "role": "assistant", "content": [{"type": "output_text", "text": '{"has_cross_worker_leads": false, "followup_tasks": [], "rationale": "Done"}'}]}],
            }))
        if "BearCaseBatch" in instr:
            bear = {
                "batch_id": "bb_1",
                "items": [{
                    "bear_id": "b_1",
                    "title": "Growth concentration",
                    "thesis_challenge": "Cloud Subscriptions accounts for $900M of $1,500M total revenue.",
                    "anchored_evidence_ids": ["c_rev"],
                    "anchor_kind": "evidence_card",
                    "next_quarter_invalidation_trigger": "Cloud Subscriptions growth decelerating below 15%.",
                }],
                "rejected_items": [],
                "reviewer_notes": "Anchored check.",
            }
            return HttpResponse(200, json.dumps({
                "status": "completed",
                "output": [{"type": "message", "id": "m_s3a", "role": "assistant", "content": [{"type": "output_text", "text": json.dumps(bear)}]}],
            }))
        return HttpResponse(200, json.dumps({
            "status": "completed",
            "output": [{"type": "message", "id": "m_s3b", "role": "assistant", "content": [{"type": "output_text", "text": "# ACME Q4 Earnings Memo\n\nRevenue reached $1,500M (+22% YoY) [c_rev]."}]}],
        }))

    monkeypatch.setattr(openai_responses, "_default_transport", squad_responses_transport)
    ok_run = client.post(f"/v2/research/topics/{topic_id}/earnings-squad/run", headers=headers, json={
        "symbol": "ACME",
        "cik": "0001234567",
        "company_name": "ACME Corp",
        "execution_mode": "sync",
    })
    assert ok_run.status_code == 200, ok_run.text
    ok_body = ok_run.json()
    assert ok_body["status"] == "completed"
    assert ok_body["rejection_details"] is None
    assert ok_body["dossier"]["evidence_cards"][0]["card_id"] == "c_rev"
    assert "ACME Q4 Earnings Memo" in ok_body["dossier"]["memo_markdown"]
    # Verify Stage 2 worker tool loop preserved encrypted reasoning & function_call_output pairing
    assert any(
        any(x.get("type") == "reasoning" and x.get("encrypted_content") == "enc-worker-1" for x in w["input"])
        and any(x.get("type") == "function_call_output" and x.get("call_id") == "call_read_1" for x in w["input"])
        for w in wires_seen
    )
