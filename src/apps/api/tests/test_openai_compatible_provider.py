from __future__ import annotations

import json
from threading import Event

import pytest

import app.openai_compatible as openai_compatible
from app.phase3 import ChallengeInput, DeterministicMockLlmProvider, ProviderFailure, ProviderPolicy, UnavailableLlmProvider, build_llm_provider
from app.openai_compatible import GlmCodingGenerationSettings, HttpResponse, OpenAICompatibleLlmProvider, OpenAICompatibleSettings


_SECRET = "unit-test-only-provider-secret"


def request(*, cancel_event: Event | None = None) -> ChallengeInput:
    return ChallengeInput(
        operation_id="operation:openai-compatible",
        challenge_id="challenge:openai-compatible",
        thesis_id="thesis:openai-compatible",
        claim="A dated scenario with an observable causal chain.",
        turn=1,
        status="CHALLENGING",
        private_evidence_declared=False,
        evidence_ids=("evidence:public",),
        evidence_grades=(("evidence:public", "A"),),
        cancel_event=cancel_event,
    )


def valid_payload() -> dict[str, object]:
    criteria = (
        "premise_clarity", "time_boundary", "trigger_observability", "causal_chain",
        "counterfactuals", "evidence_quality", "asset_transmission", "already_priced",
        "falsifiability", "user_confirmation",
    )
    return {
        "claims": ["The dated scenario is recorded as a hypothesis."],
        "objections": ["A competing explanation or already-priced expectation could invalidate it."],
        "missing_evidence": ["Verify the base rate with a dated public source."],
        "questions": ["What observation by the deadline would falsify the causal chain?"],
        "rubric_scores": [{"criterion": criterion, "score": 3, "rationale": "Evidence-conservative test rationale."} for criterion in criteria],
        "unresolved_issues": ["The public base rate still needs independent review."],
        "citation_ids": ["evidence:public"],
        "public_fact_citation_ids": ["evidence:public"],
        "native_search_used": False,
    }


def configure(monkeypatch: pytest.MonkeyPatch, *, key: str | None = _SECRET, base_url: str = "https://llm.example.test/v1") -> None:
    monkeypatch.setenv("FORESIGHTNINE_LLM_BASE_URL", base_url)
    monkeypatch.setenv("FORESIGHTNINE_LLM_MODEL", "test-model")
    monkeypatch.setenv("FORESIGHTNINE_LLM_ALLOWED_HOSTS", "llm.example.test")
    if key is None:
        monkeypatch.delenv("FORESIGHTNINE_LLM_API_KEY", raising=False)
    else:
        monkeypatch.setenv("FORESIGHTNINE_LLM_API_KEY", key)


def provider(monkeypatch: pytest.MonkeyPatch, transport: object, *, key: str | None = _SECRET) -> OpenAICompatibleLlmProvider:
    configure(monkeypatch, key=key)
    return OpenAICompatibleLlmProvider.from_environment(
        policy=ProviderPolicy(max_attempts=2, timeout_seconds=0.25, retry_base_seconds=0),
        transport=transport,  # type: ignore[arg-type]
        sleeper=lambda _delay: None,
    )


def test_success_posts_chat_completion_and_returns_validated_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, dict[str, str], bytes, float]] = []

    def transport(method: str, url: str, headers: dict[str, str], body: bytes, timeout: float) -> HttpResponse:
        calls.append((method, url, headers, body, timeout))
        return HttpResponse(200, json.dumps({"choices": [{"message": {"content": json.dumps(valid_payload())}}]}))

    draft = provider(monkeypatch, transport).generate_challenge(request())
    assert draft.claims == ("The dated scenario is recorded as a hypothesis.",)
    assert draft.native_search_used is False
    assert calls[0][0:2] == ("POST", "https://llm.example.test/v1/chat/completions")
    assert json.loads(calls[0][3])["model"] == "test-model"


def test_generic_profile_success_excludes_glm_generation_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[bytes] = []

    def transport(_method: str, _url: str, _headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        captured.append(body)
        return HttpResponse(200, json.dumps({"choices": [{"message": {"content": json.dumps(valid_payload())}}]}))

    configure(monkeypatch)
    provider_value = OpenAICompatibleLlmProvider.from_environment(
        policy=ProviderPolicy(max_attempts=1),
        transport=transport,
    )
    provider_value.generate_challenge(request())

    body = json.loads(captured[0])
    assert set(body) == {"model", "messages", "max_tokens", "response_format"}
    assert not {"thinking", "reasoning_effort", "do_sample", "temperature", "top_p"}.intersection(body)


def test_missing_key_fails_closed_without_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def transport(*_args: object) -> HttpResponse:
        nonlocal calls
        calls += 1
        raise AssertionError("transport must not be called without an environment secret")

    with pytest.raises(ProviderFailure, match="secret"):
        provider(monkeypatch, transport, key=None).generate_challenge(request())
    assert calls == 0


def test_opt_in_selector_builds_server_provider_but_missing_secret_stays_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, key=None)
    monkeypatch.setenv("FORESIGHTNINE_LLM_PROVIDER", "openai_compatible")
    built = build_llm_provider()
    assert isinstance(built, OpenAICompatibleLlmProvider)
    with pytest.raises(ProviderFailure, match="secret"):
        built.generate_challenge(request())


@pytest.mark.parametrize(
    ("provider", "environment", "public_hardening"),
    [
        ("unknown-provider-selector", "development", ""),
        ("mock", "production", ""),
        ("deterministic_mock", "development", "true"),
    ],
)
def test_selector_rejects_unknown_or_hardened_mock_without_simulation(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    environment: str,
    public_hardening: str,
) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LLM_PROVIDER", provider)
    monkeypatch.setenv("FORESIGHTNINE_ENV", environment)
    monkeypatch.setenv("FORESIGHTNINE_PUBLIC_HARDENING", public_hardening)

    built = build_llm_provider()

    assert isinstance(built, UnavailableLlmProvider)
    with pytest.raises(ProviderFailure, match="unavailable") as error:
        built.generate_challenge(request())
    assert provider not in str(error.value)
    assert environment not in str(error.value)
    if public_hardening:
        assert public_hardening not in str(error.value)


def test_selector_keeps_development_default_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORESIGHTNINE_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("FORESIGHTNINE_ENV", raising=False)
    monkeypatch.delenv("FORESIGHTNINE_PUBLIC_HARDENING", raising=False)

    built = build_llm_provider()

    assert isinstance(built, DeterministicMockLlmProvider)
    assert built.generate_challenge(request()).provenance == "provider_output_validated; native_search_not_verified"


def test_invalid_json_fails_closed_at_completion_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    value = provider(monkeypatch, lambda *_args: HttpResponse(200, "not-json"))
    with pytest.raises(ProviderFailure, match="invalid JSON"):
        value.generate_challenge(request())


def test_429_is_retried_only_within_bounded_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def transport(*_args: object) -> HttpResponse:
        nonlocal attempts
        attempts += 1
        return HttpResponse(429, "rate limited")

    with pytest.raises(ProviderFailure, match="rate limit"):
        provider(monkeypatch, transport).generate_challenge(request())
    assert attempts == 2


def test_timeout_is_retried_only_within_bounded_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def transport(*_args: object) -> HttpResponse:
        nonlocal attempts
        attempts += 1
        raise TimeoutError()

    with pytest.raises(ProviderFailure, match="timed out"):
        provider(monkeypatch, transport).generate_challenge(request())
    assert attempts == 2


def test_https_allowlist_and_secret_non_leakage(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[dict[str, str], bytes]] = []

    def transport(_method: str, _url: str, headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        calls.append((headers, body))
        return HttpResponse(200, json.dumps({"choices": [{"message": {"content": json.dumps(valid_payload())}}]}))

    configure(monkeypatch)
    value = OpenAICompatibleLlmProvider.from_environment(
        policy=ProviderPolicy(max_attempts=1),
        transport=transport,
    )
    assert _SECRET not in repr(value)
    assert _SECRET not in str(value)
    value.generate_challenge(request())
    assert calls and _SECRET in calls[0][0]["Authorization"]
    assert _SECRET not in calls[0][1].decode("utf-8")
    with pytest.raises(ProviderFailure):
        configure(monkeypatch, base_url="http://llm.example.test/v1")
        OpenAICompatibleLlmProvider.from_environment().generate_challenge(request())
    with pytest.raises(ProviderFailure):
        configure(monkeypatch, base_url="https://not-allowlisted.example.test/v1")
        OpenAICompatibleLlmProvider.from_environment().generate_challenge(request())


def test_default_transport_rejects_allowlisted_hostname_resolving_to_private_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        openai_compatible.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(openai_compatible._TransportFailure):
        openai_compatible._require_public_dns("llm.example.test", 443)


def test_glm_coding_profile_pins_official_endpoint_and_keeps_secret_server_side(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, str, dict[str, str], bytes]] = []

    def transport(method: str, url: str, headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        captured.append((method, url, headers, body))
        return HttpResponse(200, json.dumps({"choices": [{"message": {"content": json.dumps(valid_payload())}}]}))

    monkeypatch.setenv("FORESIGHTNINE_GLM_CODING_API_KEY", _SECRET)
    value = OpenAICompatibleLlmProvider.from_glm_coding_environment(
        policy=ProviderPolicy(max_attempts=1),
        transport=transport,
    )
    value.generate_challenge(request())

    assert captured[0][0:2] == ("POST", "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions")
    assert json.loads(captured[0][3])["model"] == "glm-5.3"
    assert _SECRET not in captured[0][3].decode("utf-8")
    assert _SECRET not in repr(value)


def test_glm_coding_profile_sends_deterministic_generation_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[bytes] = []

    def transport(_method: str, _url: str, _headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        captured.append(body)
        return HttpResponse(200, json.dumps({"choices": [{"message": {"content": json.dumps(valid_payload())}}]}))

    monkeypatch.setenv("FORESIGHTNINE_GLM_CODING_API_KEY", _SECRET)
    OpenAICompatibleLlmProvider.from_glm_coding_environment(
        policy=ProviderPolicy(max_attempts=1),
        transport=transport,
    ).generate_challenge(request())

    body = json.loads(captured[0])
    assert set(body) == {
        "model",
        "messages",
        "thinking",
        "reasoning_effort",
        "do_sample",
        "max_tokens",
        "response_format",
    }
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"
    assert body["do_sample"] is False
    assert "temperature" not in body
    assert "top_p" not in body
    assert body["max_tokens"] == 1500
    assert body["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FORESIGHTNINE_GLM_CODING_THINKING", "maybe"),
        ("FORESIGHTNINE_GLM_CODING_THINKING", "disabled"),
        ("FORESIGHTNINE_GLM_CODING_THINKING", "false"),
        ("FORESIGHTNINE_GLM_CODING_REASONING_EFFORT", "ultra"),
        ("FORESIGHTNINE_GLM_CODING_DO_SAMPLE", "yes"),
    ],
)
def test_glm_coding_profile_rejects_invalid_server_generation_config(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    calls = 0

    def transport(*_args: object) -> HttpResponse:
        nonlocal calls
        calls += 1
        raise AssertionError("invalid configuration must not call the provider")

    monkeypatch.setenv("FORESIGHTNINE_GLM_CODING_API_KEY", _SECRET)
    monkeypatch.setenv(name, value)
    provider = OpenAICompatibleLlmProvider.from_glm_coding_environment(
        policy=ProviderPolicy(max_attempts=1),
        transport=transport,
    )
    with pytest.raises(ProviderFailure) as error:
        provider.generate_challenge(request())
    assert error.value.code == "provider_configuration_invalid"
    assert error.value.retryable is False
    assert calls == 0


def test_glm_coding_profile_rejects_disabled_direct_generation_before_transport() -> None:
    calls = 0

    def transport(*_args: object) -> HttpResponse:
        nonlocal calls
        calls += 1
        raise AssertionError("disabled thinking must not call the provider")

    settings = OpenAICompatibleSettings(
        base_url="https://llm.example.test/v1",
        model="glm-5.3",
        allowed_hosts=frozenset({("llm.example.test", 443)}),
        api_key=_SECRET,
        glm_generation=GlmCodingGenerationSettings(thinking_enabled=False),
    )
    value = OpenAICompatibleLlmProvider(settings, policy=ProviderPolicy(max_attempts=1), transport=transport)

    with pytest.raises(ProviderFailure, match="thinking") as error:
        value.generate_challenge(request())
    assert error.value.code == "provider_configuration_invalid"
    assert error.value.retryable is False
    assert calls == 0


def test_glm_coding_profile_from_values_rejects_model_override_when_generation_is_present() -> None:
    with pytest.raises(ProviderFailure, match="fixed to glm-5.3") as error:
        OpenAICompatibleSettings.from_values(
            base_url="https://llm.example.test/v1",
            model="glm-tenant-approved",
            api_key=_SECRET,
            allowlist="llm.example.test",
            glm_generation=GlmCodingGenerationSettings(),
        )
    assert error.value.code == "provider_configuration_invalid"
    assert error.value.retryable is False


def test_glm_coding_profile_rejects_direct_settings_model_override_before_transport() -> None:
    calls = 0

    def transport(*_args: object) -> HttpResponse:
        nonlocal calls
        calls += 1
        raise AssertionError("invalid model configuration must not call the provider")

    settings = OpenAICompatibleSettings(
        base_url="https://llm.example.test/v1",
        model="glm-tenant-approved",
        allowed_hosts=frozenset({("llm.example.test", 443)}),
        api_key=_SECRET,
        glm_generation=GlmCodingGenerationSettings(),
    )
    value = OpenAICompatibleLlmProvider(settings, policy=ProviderPolicy(max_attempts=1), transport=transport)

    with pytest.raises(ProviderFailure, match="fixed to glm-5.3") as error:
        value.generate_challenge(request())
    assert error.value.code == "provider_configuration_invalid"
    assert error.value.retryable is False
    assert calls == 0


def test_glm_coding_profile_rejects_model_override_before_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def transport(*_args: object) -> HttpResponse:
        nonlocal calls
        calls += 1
        raise AssertionError("invalid model configuration must not call the provider")

    monkeypatch.setenv("FORESIGHTNINE_GLM_CODING_API_KEY", _SECRET)
    monkeypatch.setenv("FORESIGHTNINE_GLM_CODING_MODEL", "glm-tenant-approved")
    monkeypatch.setenv("FORESIGHTNINE_LLM_BASE_URL", "https://untrusted.example.test/v1")
    value = OpenAICompatibleLlmProvider.from_glm_coding_environment(transport=transport)

    with pytest.raises(ProviderFailure, match="fixed to glm-5.3") as error:
        value.generate_challenge(request())
    assert error.value.code == "provider_configuration_invalid"
    assert error.value.retryable is False
    assert calls == 0


def test_glm_coding_selector_stays_fail_closed_without_its_server_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LLM_PROVIDER", "glm_coding")
    monkeypatch.delenv("FORESIGHTNINE_GLM_CODING_API_KEY", raising=False)
    built = build_llm_provider()

    assert isinstance(built, OpenAICompatibleLlmProvider)
    with pytest.raises(ProviderFailure, match="secret"):
        built.generate_challenge(request())


def test_5xx_is_retried_then_fails_without_accepting_a_result(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def transport(*_args: object) -> HttpResponse:
        nonlocal attempts
        attempts += 1
        return HttpResponse(503, "service unavailable")

    with pytest.raises(ProviderFailure) as error:
        provider(monkeypatch, transport).generate_challenge(request())
    assert error.value.code == "provider_unavailable"
    assert error.value.retryable is True
    assert attempts == 2


def test_citation_mismatch_fails_closed_at_adapter_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = valid_payload()
    payload["citation_ids"] = ["evidence:not-attached"]
    attempts = 0

    def transport(*_args: object) -> HttpResponse:
        nonlocal attempts
        attempts += 1
        return HttpResponse(200, json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]}))

    with pytest.raises(ProviderFailure) as error:
        provider(monkeypatch, transport).generate_challenge(request())
    assert error.value.code == "citation_mismatch"
    assert error.value.retryable is False
    assert attempts == 1


def test_reasoning_content_is_not_exposed_as_challenge_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    reasoning = "private chain of thought that is not evidence"

    def transport(*_args: object) -> HttpResponse:
        return HttpResponse(
            200,
            json.dumps({"choices": [{"message": {"content": json.dumps(valid_payload()), "reasoning_content": reasoning}}]}),
        )

    draft = provider(monkeypatch, transport).generate_challenge(request())
    assert reasoning not in repr(draft)
