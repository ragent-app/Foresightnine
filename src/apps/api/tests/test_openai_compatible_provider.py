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
    request_body = json.loads(calls[0][3])
    assert request_body["model"] == "test-model"
    prompt = json.loads(request_body["messages"][1]["content"])
    assert "same natural language as input.claim" in prompt["language"]
    constraints = prompt["response_constraints"]
    assert constraints["claims_count"] == "Return one to three concise claims."
    assert constraints["objections_count"] == "Return three to five distinct objections."
    assert constraints["missing_evidence_count"] == "Return two to five concrete evidence gaps."
    assert constraints["rubric_rationale_length"] == "Each rationale must be one concise sentence of at most 120 characters."
    assert constraints["readability"] == "Do not combine numbered items into prose; keep every item self-contained and non-repetitive."


def test_connection_probe_falls_back_to_minimal_chat_when_models_listing_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, dict[str, str], bytes, float]] = []

    def transport(method: str, url: str, headers: dict[str, str], body: bytes, timeout: float) -> HttpResponse:
        calls.append((method, url, headers, body, timeout))
        if method == "GET":
            return HttpResponse(404, "")
        return HttpResponse(200, json.dumps({"choices": [{"message": {"content": "OK"}}]}))

    value = provider(monkeypatch, transport)

    assert value.connection_ready() is True
    assert [(method, url) for method, url, *_rest in calls] == [
        ("GET", "https://llm.example.test/v1/models"),
        ("POST", "https://llm.example.test/v1/chat/completions"),
    ]
    probe = json.loads(calls[1][3])
    assert probe == {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 1,
        "stream": False,
    }
    assert calls[1][2]["Authorization"] == f"Bearer {_SECRET}"


def test_connection_probe_stays_unavailable_when_models_and_chat_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def transport(*_args: object) -> HttpResponse:
        nonlocal calls
        calls += 1
        return HttpResponse(401, "")

    assert provider(monkeypatch, transport).connection_ready() is False
    assert calls == 2


def test_cline_transport_type_metadata_is_removed_before_strict_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = valid_payload()
    payload["type"] = "object"
    value = provider(
        monkeypatch,
        lambda *_args: HttpResponse(200, json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]})),
    )
    draft = value.generate_challenge(request())
    assert draft.claims == ("The dated scenario is recorded as a hypothesis.",)


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


def test_environment_timeout_supports_slow_openai_compatible_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch)
    monkeypatch.setenv("FORESIGHTNINE_LLM_TIMEOUT_SECONDS", "120")
    built = OpenAICompatibleLlmProvider.from_environment(
        transport=lambda *_args: HttpResponse(200, json.dumps({"choices": [{"message": {"content": json.dumps(valid_payload())}}]})),
    )
    assert built.policy.timeout_seconds == 120
    built.generate_challenge(request())


def test_account_settings_inherit_the_bounded_deployment_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LLM_TIMEOUT_SECONDS", "120")
    settings = OpenAICompatibleSettings(
        base_url="https://llm.example.test/v1",
        model="test-model",
        allowed_hosts=frozenset({("llm.example.test", 443)}),
        api_key=_SECRET,
    )

    built = OpenAICompatibleLlmProvider(settings, transport=lambda *_args: HttpResponse(200, "{}"))

    assert built.policy.timeout_seconds == 120


def test_account_glm_coding_endpoint_uses_its_required_generation_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai_compatible, "_require_public_dns", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("FORESIGHTNINE_LLM_TIMEOUT_SECONDS", "120")
    settings = OpenAICompatibleSettings.from_account_values(
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        model="glm-5.3-flash",
        api_key=_SECRET,
    )
    captured: list[bytes] = []

    def transport(_method: str, _url: str, _headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        captured.append(body)
        return HttpResponse(200, json.dumps({"choices": [{"message": {"content": json.dumps(valid_payload())}}]}))

    built = OpenAICompatibleLlmProvider(settings, transport=transport)
    built.generate_challenge(request())
    payload = json.loads(captured[0])

    assert built.policy.timeout_seconds == 120
    assert built.policy.max_output_tokens == 4000
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "low"
    assert payload["do_sample"] is False


def test_environment_timeout_rejects_unbounded_or_invalid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch)
    monkeypatch.setenv("FORESIGHTNINE_LLM_TIMEOUT_SECONDS", "181")
    built = OpenAICompatibleLlmProvider.from_environment()
    with pytest.raises(ProviderFailure, match="configuration"):
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
    with pytest.raises(ProviderFailure, match="invalid JSON") as error:
        value.generate_challenge(request())
    assert error.value.__cause__ is None


def test_invalid_message_json_does_not_retain_raw_model_output(monkeypatch: pytest.MonkeyPatch) -> None:
    response = json.dumps({"choices": [{"message": {"content": "private-model-output {"}}]})
    value = provider(monkeypatch, lambda *_args: HttpResponse(200, response))
    with pytest.raises(ProviderFailure, match="not valid JSON") as error:
        value.generate_challenge(request())
    assert error.value.__cause__ is None


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


def test_explicit_local_gateway_profile_allows_http_and_private_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LLM_BASE_URL", "http://host.docker.internal:10910/v1")
    monkeypatch.setenv("FORESIGHTNINE_LLM_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setenv("FORESIGHTNINE_LLM_ALLOWED_HOSTS", "host.docker.internal:10910")
    monkeypatch.setenv("FORESIGHTNINE_LLM_API_KEY", _SECRET)
    monkeypatch.setenv("FORESIGHTNINE_LLM_ALLOW_PRIVATE_NETWORK", "true")

    settings = OpenAICompatibleSettings.from_environment()
    assert settings.base_url == "http://host.docker.internal:10910/v1"
    assert settings.model == "deepseek/deepseek-v4-flash"
    assert settings.allow_private_network is True


def test_local_gateway_http_remains_rejected_without_explicit_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, base_url="http://host.docker.internal:10910/v1")
    monkeypatch.setenv("FORESIGHTNINE_LLM_ALLOWED_HOSTS", "host.docker.internal:10910")
    with pytest.raises(ProviderFailure, match="HTTPS"):
        OpenAICompatibleSettings.from_environment()


def test_default_transport_rejects_allowlisted_hostname_resolving_to_private_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        openai_compatible.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(openai_compatible._TransportFailure):
        openai_compatible._require_public_dns("llm.example.test", 443)


def test_fake_dns_proxy_addresses_require_public_doh_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_records = [
        (2, 1, 6, "", ("198.18.0.180", 443)),
        (10, 1, 6, "", ("fdfe:dcba:9876::ae", 443, 0, 0)),
    ]
    monkeypatch.setattr(openai_compatible.socket, "getaddrinfo", lambda *_args, **_kwargs: fake_records)
    monkeypatch.setattr(openai_compatible, "_public_doh_addresses", lambda _hostname: ("104.18.1.2", "2606:4700::6812:102"))

    assert openai_compatible._validated_addresses("models.example.test", 443) == (
        "198.18.0.180", "fdfe:dcba:9876::ae",
    )

    monkeypatch.setattr(openai_compatible, "_public_doh_addresses", lambda _hostname: ("127.0.0.1",))
    with pytest.raises(openai_compatible._TransportFailure):
        openai_compatible._validated_addresses("models.example.test", 443)


def test_personal_provider_never_inherits_platform_private_network_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LLM_ALLOW_PRIVATE_NETWORK", "true")
    monkeypatch.setattr(
        openai_compatible.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    settings = OpenAICompatibleSettings(
        base_url="https://models.example.test/v1",
        model="account-model",
        allowed_hosts=frozenset({("models.example.test", 443)}),
        api_key=_SECRET,
        allow_private_network=False,
    )
    provider = OpenAICompatibleLlmProvider(settings, policy=ProviderPolicy(max_attempts=1))
    with pytest.raises(ProviderFailure) as error:
        provider.generate_challenge(request())
    assert error.value.code == "provider_transport_error"


def test_pinned_https_connection_uses_validated_ip_and_original_tls_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    class Socket:
        def close(self) -> None:
            calls.append("closed")

    class Context:
        def wrap_socket(self, raw_socket: object, *, server_hostname: str):
            calls.append(("tls", raw_socket, server_hostname))
            return "tls-socket"

    monkeypatch.setattr(openai_compatible.socket, "create_connection", lambda address, timeout, source: calls.append(("tcp", address, timeout, source)) or Socket())
    monkeypatch.setattr(openai_compatible.ssl, "create_default_context", lambda: Context())
    connection = openai_compatible._PinnedHTTPSConnection("models.example.test", 443, "203.0.113.9", 4.5)
    connection.connect()
    assert calls[0] == ("tcp", ("203.0.113.9", 443), 4.5, None)
    assert calls[1][0] == "tls"
    assert calls[1][2] == "models.example.test"
    assert connection.sock == "tls-socket"


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
    assert json.loads(captured[0][3])["model"] == "glm-5.3-flash"
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
    assert body["reasoning_effort"] == "low"
    assert body["do_sample"] is False
    assert "temperature" not in body
    assert "top_p" not in body
    assert body["max_tokens"] == 1500
    assert body["response_format"] == {"type": "json_object"}


def test_glm_coding_account_profile_inherits_low_reasoning_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai_compatible, "_require_public_dns", lambda *_args, **_kwargs: None)
    monkeypatch.delenv("FORESIGHTNINE_GLM_CODING_REASONING_EFFORT", raising=False)

    settings = OpenAICompatibleSettings.from_account_values(
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        model="glm-5.3-flash",
        api_key=_SECRET,
    )

    assert settings.glm_generation is not None
    assert settings.glm_generation.reasoning_effort == "low"


def test_glm_coding_profile_default_budget_allows_thinking_and_full_rubric(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[bytes] = []

    def transport(_method: str, _url: str, _headers: dict[str, str], body: bytes, _timeout: float) -> HttpResponse:
        captured.append(body)
        return HttpResponse(200, json.dumps({"choices": [{"message": {"content": json.dumps(valid_payload())}}]}))

    monkeypatch.setenv("FORESIGHTNINE_GLM_CODING_API_KEY", _SECRET)
    OpenAICompatibleLlmProvider.from_glm_coding_environment(transport=transport).generate_challenge(request())

    assert json.loads(captured[0])["max_tokens"] == 4000


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
        model="glm-5.3-flash",
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
    with pytest.raises(ProviderFailure, match="fixed to glm-5.3-flash") as error:
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

    with pytest.raises(ProviderFailure, match="fixed to glm-5.3-flash") as error:
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

    with pytest.raises(ProviderFailure, match="fixed to glm-5.3-flash") as error:
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


@pytest.mark.parametrize("post_fails", [False, True])
def test_transport_falls_back_before_submission_only(monkeypatch: pytest.MonkeyPatch, post_fails: bool) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(openai_compatible, "_validated_addresses", lambda *_args, **_kwargs: ("2606:4700::1111", "1.1.1.1"))

    class Sock:
        def settimeout(self, _timeout: float) -> None:
            pass

    class Response:
        status = 200
        headers: dict[str, str] = {}
        def read(self, _size: int) -> bytes:
            return b'{"ok": true}'

    class Connection:
        def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
            assert host == "models.example.test" and port == 443
            assert 0 < timeout <= 5
            self.address = address
            self.sock = Sock()
        def connect(self) -> None:
            calls.append(("connect", self.address))
            if ":" in self.address:
                raise OSError(101, "network unreachable")
        def close(self) -> None:
            calls.append(("close", self.address))
        def request(self, *_args: object, **_kwargs: object) -> None:
            calls.append(("request", self.address))
            if post_fails:
                raise OSError("request failure")
        def getresponse(self) -> Response:
            return Response()

    monkeypatch.setattr(openai_compatible, "_PinnedHTTPSConnection", Connection)
    if post_fails:
        with pytest.raises(openai_compatible._TransportFailure):
            openai_compatible._default_transport("POST", "https://models.example.test/v1/chat/completions", {}, b'{}', 10)
    else:
        assert openai_compatible._default_transport("POST", "https://models.example.test/v1/chat/completions", {}, b'{}', 10).status_code == 200
    assert [item for item in calls if item[0] == "request"] == [("request", "1.1.1.1")]
    assert ("close", "2606:4700::1111") in calls

@pytest.mark.parametrize("reported,expected", [(123, 123), (None, None), (-1, None), (True, None), ("123", None)])
def test_invalid_structured_output_retains_only_valid_reported_usage(monkeypatch, reported, expected):
    secret_in_output = "private-invalid-content"
    body = json.dumps({"choices": [{"message": {"content": secret_in_output}}],
                       "usage": {"total_tokens": reported}})
    instance = provider(monkeypatch, lambda *_: HttpResponse(200, body))
    with pytest.raises(ProviderFailure) as raised:
        instance.generate_structured("prompt")
    assert raised.value.code == "invalid_provider_output"
    assert raised.value.reported_tokens == expected
    assert secret_in_output not in str(raised.value)
    assert raised.value.__cause__ is None



def test_research_input_budget_is_bounded_and_does_not_change_shared_provider(monkeypatch):
    configure(monkeypatch)
    provider = OpenAICompatibleLlmProvider.from_environment()
    default_limit = provider.policy.max_input_tokens
    assert provider.for_research() is provider
    monkeypatch.setenv("FORESIGHTNINE_RESEARCH_MAX_INPUT_TOKENS", "6000")
    research_provider = provider.for_research()
    assert research_provider.policy.max_input_tokens == 6000
    assert provider.policy.max_input_tokens == default_limit
    assert research_provider.transport is provider.transport
    for raw in ("0", "12001", "-1", "3.5", "not-a-number"):
        monkeypatch.setenv("FORESIGHTNINE_RESEARCH_MAX_INPUT_TOKENS", raw)
        with pytest.raises(ProviderFailure, match="configuration"):
            provider.for_research()
