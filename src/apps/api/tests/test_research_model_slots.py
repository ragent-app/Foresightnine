from __future__ import annotations

from datetime import datetime, timezone
import json

from fastapi.testclient import TestClient
import pytest

import app.main as main
import app.openai_compatible as compatible
from app.model_credentials import (
    AccountModelSettings,
    ModelConfigUpdate,
    ModelCredentialCipher,
    ModelCredentialError,
    StoredModelConfig,
)
from app.openai_compatible import OpenAICompatibleSettings
from app.provider_capabilities import CapabilityProfile, ChatGenerationParameters
from app.research_model_slots import (
    MODEL_SLOTS,
    ModelSlotBinding,
    ModelSlotConfig,
    ModelSlotError,
    ResolvedModelSlots,
    SlotCapabilities,
    primary_profile_from_settings,
    resolve_model_slots,
    slot_routing_summary,
)
from app.storage import InMemoryDocumentRepository


def _primary_binding(
    *,
    protocol: str = "openai_chat_completions",
    base_url: str = "https://primary.example.test/v1",
    model: str = "primary-model-v1",
    credential_ref: str = "account_primary",
    native_tools: bool | None = True,
    tools_verified: bool | None = True,
) -> ModelSlotBinding:
    caps = (
        SlotCapabilities(
            mode="json_object",
            native_tools=native_tools,
            context_tokens=131072,
            output_tokens=8192,
            input_counter="chars_div_3",
        )
        if native_tools is not None
        else None
    )
    return ModelSlotBinding(
        protocol=protocol,  # type: ignore[arg-type]
        base_url=base_url,
        model=model,
        credential_ref=credential_ref,
        capabilities=caps,
        tools_verified=tools_verified,
    )


def test_unset_slots_fall_back_to_primary_research_model() -> None:
    primary = _primary_binding()

    resolved_none = resolve_model_slots(primary, None)
    resolved_empty = resolve_model_slots(primary, ModelSlotConfig())

    for resolved in (resolved_none, resolved_empty):
        assert isinstance(resolved, ResolvedModelSlots)
        assert resolved.is_valid is True
        assert resolved.cross_family_bear_review is False
        for slot in MODEL_SLOTS:
            entry = resolved[slot]
            assert entry.fallback_used is True
            assert entry.source == "primary_fallback"
            assert entry.protocol == "openai_chat_completions"
            assert entry.base_url == "https://primary.example.test/v1"
            assert entry.model == "primary-model-v1"
            assert entry.credential_ref == "account_primary"
            assert entry.capability_valid is True
            assert entry.differs_from_primary is False

    partial = resolve_model_slots(
        primary,
        {
            "bear_reviewer": {
                "protocol": "anthropic_messages",
                "base_url": "https://reviewer.example.test/v1",
                "model": "reviewer-model-v2",
                "credential_ref": "env:BEAR_KEY",
                "capabilities": {"mode": "json_object", "native_tools": False},
            }
        },
    )
    assert partial.fast_extractor.fallback_used is True
    assert partial.deep_reasoner.fallback_used is True
    assert partial.writer.fallback_used is True
    assert partial.bear_reviewer.fallback_used is False
    assert partial.bear_reviewer.source == "slot_override"
    assert partial.bear_reviewer.differs_from_primary is True
    assert partial.cross_family_bear_review is True


def test_heterogeneous_four_slot_routing_across_wire_protocols() -> None:
    primary = _primary_binding()
    overrides = ModelSlotConfig(
        fast_extractor=ModelSlotBinding(
            protocol="openai_chat_completions",
            base_url="https://fast.example.test/v1/",
            model="extractor-v1",
            credential_ref="env:FAST_SLOT_KEY",
            generation_parameters=ChatGenerationParameters(temperature=0.0, seed=42),
            capabilities=SlotCapabilities(
                version="chat-v1",
                mode="json_schema",
                schema_dialect="required_nullable_v1",
                native_tools=True,
                context_tokens=128000,
                output_tokens=4096,
                generation_parameters=("temperature", "seed"),
            ),
        ),
        deep_reasoner=ModelSlotBinding(
            protocol="openai_responses",
            base_url="https://reasoner.example.test/v1",
            model="reasoner-v1",
            credential_ref="env:REASONER_SLOT_KEY",
            generation_parameters=ChatGenerationParameters(reasoning_effort="high"),
            capabilities=SlotCapabilities(
                version="responses-v1",
                mode="json_object",
                native_tools=False,
                context_tokens=200000,
                output_tokens=16384,
                generation_parameters=("reasoning_effort",),
            ),
        ),
        bear_reviewer=ModelSlotBinding(
            protocol="anthropic_messages",
            base_url="https://bear.example.test",
            model="adversarial-v1",
            credential_ref="vault:bear-key",
            messages_auth_scheme="x-api-key",
            messages_max_tokens=8192,
            messages_effort="high",
            capabilities=SlotCapabilities(
                version="2023-06-01",
                mode="json_object",
                native_tools=False,
                continuation=True,
                context_tokens=200000,
                output_tokens=8192,
            ),
        ),
        writer=ModelSlotBinding(
            protocol="openai_chat_completions",
            base_url="https://writer.example.test/v1",
            model="memo-writer-v1",
            credential_ref="env:WRITER_SLOT_KEY",
            capabilities=SlotCapabilities(
                version="chat-v1",
                mode="json_object",
                native_tools=False,
                context_tokens=131072,
                output_tokens=16384,
            ),
        ),
    )

    resolved = resolve_model_slots(primary, overrides)
    assert resolved.is_valid is True
    assert resolved.cross_family_bear_review is True
    assert resolved.fast_extractor.endpoint == "https://fast.example.test/v1/chat/completions"
    assert resolved.fast_extractor.base_url == "https://fast.example.test/v1"
    assert resolved.fast_extractor.native_tools_supported is True
    assert resolved.deep_reasoner.endpoint == "https://reasoner.example.test/v1/responses"
    assert resolved.deep_reasoner.native_tools_supported is False
    assert resolved.bear_reviewer.endpoint == "https://bear.example.test/v1/messages"
    assert resolved.bear_reviewer.messages_auth_scheme == "x-api-key"
    assert resolved.bear_reviewer.messages_max_tokens == 8192
    assert resolved.writer.endpoint == "https://writer.example.test/v1/chat/completions"
    assert all(resolved[slot].capability_valid for slot in MODEL_SLOTS)


def test_fast_extractor_requires_native_tools_fail_closed_and_flagging() -> None:
    primary_verified = _primary_binding(native_tools=True, tools_verified=True)

    # Explicit native_tools=False on fast_extractor fails closed in strict mode.
    non_tool_fast = ModelSlotBinding(
        protocol="openai_chat_completions",
        base_url="https://fast.example.test/v1",
        model="no-tools-model",
        credential_ref="env:FAST_KEY",
        capabilities=SlotCapabilities(mode="json_object", native_tools=False),
    )
    with pytest.raises(ModelSlotError) as exc_info:
        resolve_model_slots(primary_verified, {"fast_extractor": non_tool_fast})
    assert exc_info.value.code == "model_slot_capability_invalid"
    assert exc_info.value.slot == "fast_extractor"
    assert exc_info.value.resolved is not None
    assert exc_info.value.resolved.is_valid is False

    # strict=False flags clear error on fast_extractor while keeping valid slots intact.
    flagged = resolve_model_slots(primary_verified, {"fast_extractor": non_tool_fast}, strict=False)
    assert flagged.is_valid is False
    assert flagged.fast_extractor.capability_valid is False
    assert flagged.fast_extractor.native_tools_required is True
    assert flagged.fast_extractor.native_tools_supported is False
    assert "native tool calling" in flagged.fast_extractor.validation_errors[0]
    assert flagged.deep_reasoner.capability_valid is True

    # Unknown capability (native_tools=None) on fast_extractor also fails closed.
    unknown_fast = ModelSlotBinding(
        protocol="openai_chat_completions",
        base_url="https://fast.example.test/v1",
        model="unknown-cap-model",
        credential_ref="env:FAST_KEY",
    )
    with pytest.raises(ModelSlotError) as unknown_exc:
        resolve_model_slots(primary_verified, {"fast_extractor": unknown_fast})
    assert unknown_exc.value.slot == "fast_extractor"

    # Unverified personal settings falling back to primary fail fast_extractor,
    # unless fast_extractor itself is overridden with a tool-capable profile.
    unverified_settings = OpenAICompatibleSettings.from_values(
        base_url="https://primary.example.test/v1",
        model="reasoning-only-primary",
        api_key="runtime-secret-token-1234",
        allowlist="primary.example.test",
    )
    unverified_settings = OpenAICompatibleSettings(
        **{**unverified_settings.__dict__, "personal": True, "tools_verified": False}
    )
    with pytest.raises(ModelSlotError) as fallback_exc:
        resolve_model_slots(unverified_settings, None)
    assert fallback_exc.value.slot == "fast_extractor"

    overridden = resolve_model_slots(
        unverified_settings,
        {
            "fast_extractor": ModelSlotBinding(
                protocol="openai_chat_completions",
                base_url="https://fast.example.test/v1",
                model="verified-fast-model",
                credential_ref="env:FAST_KEY",
                capabilities=SlotCapabilities(mode="json_object", native_tools=True),
            )
        },
    )
    assert overridden.is_valid is True
    assert overridden.fast_extractor.fallback_used is False
    assert overridden.deep_reasoner.fallback_used is True
    assert overridden.deep_reasoner.capability_valid is True


def test_provider_capabilities_env_registry_and_deployment_precedence(monkeypatch) -> None:
    profiles = [
        {
            "protocol": "openai_chat_completions",
            "endpoint": "https://fast.example.test/v1/chat/completions",
            "model": "fast-env-model",
            "version": "v2",
            "mode": "json_schema",
            "schema_dialect": "required_nullable_v1",
            "native_tools": True,
            "context_tokens": 131072,
            "output_tokens": 16384,
            "input_counter": "chars_div_3",
            "generation_parameters": ["temperature"],
        },
        {
            "protocol": "anthropic_messages",
            "endpoint": "https://bear.example.test/v1/messages",
            "model": "bear-env-model",
            "version": "2023-06-01",
            "mode": "json_object",
            "native_tools": False,
            "continuation": True,
            "context_tokens": 200000,
            "output_tokens": 8192,
        },
    ]
    monkeypatch.setenv("FORESIGHTNINE_LLM_CAPABILITY_PROFILES", json.dumps(profiles))
    monkeypatch.setenv("FORESIGHTNINE_RESEARCH_MAX_OUTPUT_TOKENS", "6144")

    primary = ModelSlotBinding(
        protocol="openai_chat_completions",
        base_url="https://fast.example.test/v1",
        model="fast-env-model",
        credential_ref="platform_primary",
    )
    resolved = resolve_model_slots(
        primary,
        {
            "bear_reviewer": ModelSlotBinding(
                protocol="anthropic_messages",
                base_url="https://bear.example.test/v1",
                model="bear-env-model",
                credential_ref="env:BEAR_KEY",
                # Attempt to claim native_tools=True on a deployment marked native_tools=False:
                # deployment evidence must win.
                capabilities=SlotCapabilities(native_tools=True, output_tokens=12000),
            )
        },
    )
    assert resolved.fast_extractor.capability_source == "configured_profile"
    assert resolved.fast_extractor.native_tools_supported is True
    assert resolved.fast_extractor.capability_profile is not None
    assert resolved.fast_extractor.capability_profile.output_tokens == 6144
    assert resolved.fast_extractor.capability_profile.input_counter == "chars_div_3"
    assert resolved.bear_reviewer.capability_source == "configured_profile"
    assert resolved.bear_reviewer.native_tools_supported is False
    assert resolved.bear_reviewer.capability_profile is not None
    assert resolved.bear_reviewer.capability_profile.output_tokens == 6144

    # Attempting to bind fast_extractor to the deployment profile where native_tools=False must fail closed.
    with pytest.raises(ModelSlotError) as denied_exc:
        resolve_model_slots(
            primary,
            {
                "fast_extractor": ModelSlotBinding(
                    protocol="anthropic_messages",
                    base_url="https://bear.example.test/v1",
                    model="bear-env-model",
                    credential_ref="env:BEAR_KEY",
                    capabilities=SlotCapabilities(native_tools=True),
                )
            },
        )
    assert denied_exc.value.slot == "fast_extractor"

    # Malformed env capability registry fails closed cleanly.
    monkeypatch.setenv("FORESIGHTNINE_LLM_CAPABILITY_PROFILES", "{bad-json")
    with pytest.raises(ModelSlotError) as malformed_exc:
        resolve_model_slots(primary, None)
    assert malformed_exc.value.code == "model_slot_capability_invalid"


def test_protocol_parameter_constraints_and_capability_validation() -> None:
    primary = _primary_binding()

    # anthropic_messages forbids ChatGenerationParameters
    with pytest.raises(ValueError):
        ModelSlotBinding(
            protocol="anthropic_messages",
            base_url="https://bear.example.test/v1",
            model="m-msg",
            generation_parameters=ChatGenerationParameters(temperature=0.2),
        )

    # openai_responses forbids seed
    with pytest.raises(ValueError):
        ModelSlotBinding(
            protocol="openai_responses",
            base_url="https://resp.example.test/v1",
            model="m-resp",
            generation_parameters=ChatGenerationParameters(seed=7),
        )

    # non-Messages forbids messages_* options
    with pytest.raises(ValueError):
        ModelSlotBinding(
            protocol="openai_chat_completions",
            base_url="https://chat.example.test/v1",
            model="m-chat",
            messages_auth_scheme="x-api-key",
        )

    # Unsupported generation parameter relative to CapabilityProfile fails validation
    bad_param_slot = ModelSlotBinding(
        protocol="openai_chat_completions",
        base_url="https://chat.example.test/v1",
        model="m-chat",
        generation_parameters=ChatGenerationParameters(temperature=0.2, top_p=0.9),
        capabilities=SlotCapabilities(
            mode="json_object",
            native_tools=True,
            generation_parameters=("temperature",),
        ),
    )
    flagged = resolve_model_slots(primary, {"fast_extractor": bad_param_slot}, strict=False)
    assert flagged.is_valid is False
    assert "top_p" in flagged.fast_extractor.validation_errors[0]

    # json_schema without schema_dialect fails validation
    bad_dialect = ModelSlotBinding(
        protocol="openai_chat_completions",
        base_url="https://chat.example.test/v1",
        model="m-chat",
        capabilities=SlotCapabilities(mode="json_schema", native_tools=True),
    )
    flagged_dialect = resolve_model_slots(primary, {"writer": bad_dialect}, strict=False)
    assert flagged_dialect.is_valid is False
    assert "required_nullable_v1" in flagged_dialect.writer.validation_errors[0]

    # anthropic_messages with continuation=False fails on fast_extractor
    no_cont = ModelSlotBinding(
        protocol="anthropic_messages",
        base_url="https://msg.example.test/v1",
        model="m-msg",
        capabilities=SlotCapabilities(mode="json_object", native_tools=True, continuation=False),
    )
    flagged_cont = resolve_model_slots(primary, {"fast_extractor": no_cont}, strict=False)
    assert flagged_cont.is_valid is False
    assert "continuation" in flagged_cont.fast_extractor.validation_errors[0]

    # Unknown slot key in slot_overrides is rejected fail-closed
    with pytest.raises(ModelSlotError):
        resolve_model_slots(primary, {"unknown_slot": _primary_binding()})


def test_deployment_ref_resolution_from_registry_and_env(monkeypatch) -> None:
    primary = _primary_binding()
    registry = {
        "tier-fast": {
            "protocol": "openai_chat_completions",
            "base_url": "https://fast-pool.example.test/v1",
            "model": "fast-pool-model",
            "credential_ref": "vault:fast-pool",
            "capabilities": {"mode": "json_object", "native_tools": True},
        },
        "tier-bear": {
            "protocol": "anthropic_messages",
            "base_url": "https://bear-pool.example.test/v1",
            "model": "bear-pool-model",
            "credential_ref": "vault:bear-pool",
            "capabilities": {"mode": "json_object", "native_tools": False},
        },
    }
    resolved = resolve_model_slots(
        primary,
        ModelSlotConfig(
            fast_extractor=ModelSlotBinding(deployment_ref="tier-fast"),
            bear_reviewer=ModelSlotBinding(deployment_ref="tier-bear"),
        ),
        deployment_registry=registry,
    )
    assert resolved.fast_extractor.deployment_ref == "tier-fast"
    assert resolved.fast_extractor.model == "fast-pool-model"
    assert resolved.fast_extractor.credential_ref == "vault:fast-pool"
    assert resolved.bear_reviewer.model == "bear-pool-model"
    assert resolved.cross_family_bear_review is True

    # Unknown deployment_ref fails closed
    with pytest.raises(ModelSlotError):
        resolve_model_slots(
            primary,
            ModelSlotConfig(writer=ModelSlotBinding(deployment_ref="missing-tier")),
            deployment_registry=registry,
        )

    # Env deployment registry also works
    monkeypatch.setenv("FORESIGHTNINE_MODEL_SLOT_DEPLOYMENTS", json.dumps(registry))
    env_resolved = resolve_model_slots(
        primary,
        ModelSlotConfig(fast_extractor=ModelSlotBinding(deployment_ref="tier-fast")),
    )
    assert env_resolved.fast_extractor.base_url == "https://fast-pool.example.test/v1"


def test_secret_safety_never_exposes_raw_secrets_or_ssrf_urls() -> None:
    raw_secret = "secret-api-value-" + "987654321"
    settings = OpenAICompatibleSettings.from_values(
        base_url="https://primary.example.test/v1",
        model="primary-model",
        api_key=raw_secret,
        allowlist="primary.example.test",
    )
    settings = OpenAICompatibleSettings(**{**settings.__dict__, "personal": True, "tools_verified": True})
    resolved = resolve_model_slots(
        settings,
        {
            "bear_reviewer": {
                "protocol": "anthropic_messages",
                "base_url": "https://bear.example.test/v1",
                "model": "bear-model",
                "credential_ref": "vault:bear-cred",
            }
        },
    )
    summary = slot_routing_summary(resolved)
    dumped_summary = json.dumps(summary)
    dumped_resolved = resolved.model_dump_json()
    assert raw_secret not in dumped_summary
    assert raw_secret not in dumped_resolved

    # Raw secret fields are rejected without echoing the secret value in exception text
    for bad_key in ("api_key", "secret", "access_token", "encrypted_api_key"):
        with pytest.raises(ValueError) as exc_info:
            ModelSlotBinding.model_validate({
                "protocol": "openai_chat_completions",
                "base_url": "https://fast.example.test/v1",
                "model": "fast-model",
                bad_key: raw_secret,
            })
        assert raw_secret not in str(exc_info.value)

    with pytest.raises(ModelSlotError) as dict_exc:
        resolve_model_slots(
            {"protocol": "openai_chat_completions", "base_url": "https://fast.example.test/v1", "model": "m", "api_key": raw_secret},
            None,
        )
    assert raw_secret not in str(dict_exc.value)

    # SSRF / userinfo / query / raw credential_ref rejection
    for bad_url in (
        "http://insecure.example.test/v1",
        "https://user:pass@fast.example.test/v1",
        "https://fast.example.test/v1?api_key=leaked",
        "https://localhost/v1",
        "https://127.0.0.1/v1",
        "https://169.254.169.254/v1",
        "https://metadata.google.internal/v1",
    ):
        with pytest.raises(ValueError):
            ModelSlotBinding(
                protocol="openai_chat_completions",
                base_url=bad_url,
                model="fast-model",
            )

    for bad_ref in ("Bearer abc123456789", "sk-1234567890abcdef", "ref?token=abc", "has space"):
        with pytest.raises(ValueError):
            ModelSlotBinding(
                protocol="openai_chat_completions",
                base_url="https://fast.example.test/v1",
                model="fast-model",
                credential_ref=bad_ref,
            )


def test_slot_routing_summary_serialization_for_api_and_diagnostics() -> None:
    cap = CapabilityProfile(
        endpoint="https://primary.example.test/v1/chat/completions",
        model="primary-model",
        version="v1",
        mode="json_object",
        native_tools=True,
        context_tokens=131072,
        output_tokens=8192,
        input_counter="chars_div_3",
    )
    resolved = resolve_model_slots(
        cap,
        {
            "bear_reviewer": ModelSlotBinding(
                protocol="anthropic_messages",
                base_url="https://alt.example.test/v1",
                model="alt-reviewer",
                credential_ref="env:ALT_KEY",
            )
        },
    )
    summary = slot_routing_summary(resolved)
    round_tripped = json.loads(json.dumps(summary))
    assert round_tripped["schema_version"] == "v1"
    assert round_tripped["is_valid"] is True
    assert round_tripped["cross_family_bear_review"] is True
    assert round_tripped["configured_slots"] == ["bear_reviewer"]
    assert round_tripped["fallback_slots"] == ["fast_extractor", "deep_reasoner", "writer"]
    assert round_tripped["heterogeneous_slots"] == ["bear_reviewer"]
    assert round_tripped["slots"]["fast_extractor"]["endpoint_host"] == "primary.example.test"
    assert round_tripped["slots"]["fast_extractor"]["fallback_used"] is True
    assert round_tripped["slots"]["bear_reviewer"]["endpoint_host"] == "alt.example.test"
    assert round_tripped["slots"]["bear_reviewer"]["credential_ref"] == "env:ALT_KEY"

    # Config-only summary without primary_profile marks unset slots cleanly without spurious errors
    config_only_summary = slot_routing_summary(
        ModelSlotConfig(
            bear_reviewer=ModelSlotBinding(
                protocol="anthropic_messages",
                base_url="https://alt.example.test/v1",
                model="alt-reviewer",
                credential_ref="env:ALT_KEY",
            )
        )
    )
    assert config_only_summary["is_valid"] is True
    assert config_only_summary["configured_slots"] == ["bear_reviewer"]
    assert config_only_summary["fallback_slots"] == ["fast_extractor", "deep_reasoner", "writer"]
    assert config_only_summary["slots"]["fast_extractor"]["source"] == "primary_fallback"


def test_account_model_settings_slot_persistence_and_api_snapshot(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    monkeypatch.setattr(compatible, "_require_public_dns", lambda *_args, **_kwargs: None)
    cipher = ModelCredentialCipher(b"s" * 32)
    repo = InMemoryDocumentRepository()
    service = AccountModelSettings(repo, cipher)
    raw_personal_key = "personal-primary-" + "secret-4321"

    # Personal primary setup without slots preserves exact legacy snapshot shape
    saved = service.update(
        "account_123456",
        ModelConfigUpdate(
            mode="personal",
            base_url="https://primary.example.test/v1",
            model="primary-model",
            api_key=raw_personal_key,
        ),
        now=datetime(2026, 11, 24, tzinfo=timezone.utc),
    )
    assert "model_slots" not in saved.model_dump(exclude_none=True)
    assert "model_slots_summary" not in saved.model_dump(exclude_none=True)

    # Mark primary tools verified, then assign slots without wiping verification
    _, stored = service.runtime_configuration("account_123456")
    assert stored is not None
    assert service.record_tools_result(stored, True) is True

    slot_config = ModelSlotConfig(
        bear_reviewer=ModelSlotBinding(
            protocol="anthropic_messages",
            base_url="https://bear.example.test/v1",
            model="bear-model",
            credential_ref="env:BEAR_KEY",
        )
    )
    updated_slots = service.update_model_slots(
        "account_123456",
        slot_config,
        now=datetime(2026, 11, 24, 1, tzinfo=timezone.utc),
    )
    assert updated_slots.research_tools_verified_at is not None
    assert updated_slots.model_slots == slot_config
    assert updated_slots.model_slots_summary is not None
    assert updated_slots.model_slots_summary.is_valid is True
    assert updated_slots.model_slots_summary.cross_family_bear_review is True
    assert raw_personal_key not in updated_slots.model_dump_json()
    assert raw_personal_key not in json.dumps(service.slot_routing_summary("account_123456"))

    # Switching to platform mode preserves saved slots; clearing removes them
    platform_resp = service.update("account_123456", ModelConfigUpdate(mode="platform"))
    assert platform_resp.mode == "platform"
    assert platform_resp.model_slots == slot_config
    assert platform_resp.model_slots_summary is not None

    cleared_resp = service.update("account_123456", ModelConfigUpdate(mode="platform", clear_model_slots=True))
    assert "model_slots" not in cleared_resp.model_dump(exclude_none=True)
    assert service.model_slots("account_123456") is None

    # Private network escape hatch is rejected for account-saved slots
    with pytest.raises(ModelCredentialError) as private_exc:
        service.update_model_slots(
            "account_123456",
            ModelSlotConfig(
                writer=ModelSlotBinding(
                    protocol="openai_chat_completions",
                    base_url="http://host.docker.internal:8080/v1",
                    model="local-writer",
                    allow_private_network=True,
                )
            ),
        )
    assert private_exc.value.status_code == 422

    # Backwards compatibility: legacy StoredModelConfig dict without model_slots loads cleanly
    legacy = StoredModelConfig.model_validate({
        "account_id": "account_legacy1",
        "mode": "platform",
        "updated_at": datetime(2026, 11, 20, tzinfo=timezone.utc),
    })
    assert legacy.model_slots is None

    # End-to-end API route test with TestClient
    monkeypatch.setattr(main.model_settings, "cipher", ModelCredentialCipher(b"p" * 32))
    main.reset_state()
    client = TestClient(main.app)
    auth = client.post(
        "/v1/auth/local/register",
        json={"email": "slots@example.test", "password": "correct horse battery staple!"},
    ).json()
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    put_resp = client.put(
        "/v1/account/model-config",
        headers=headers,
        json={
            "mode": "platform",
            "model_slots": {
                "bear_reviewer": {
                    "protocol": "anthropic_messages",
                    "base_url": "https://bear.example.test/v1",
                    "model": "bear-model",
                    "credential_ref": "env:BEAR_KEY",
                }
            },
        },
    )
    assert put_resp.status_code == 200, put_resp.text
    body = put_resp.json()
    assert body["model_slots_summary"]["configured_slots"] == ["bear_reviewer"]
    assert body["model_slots_summary"]["fallback_slots"] == ["fast_extractor", "deep_reasoner", "writer"]

    # Primary profile helper builds non-secret binding from stored personal config
    primary_from_svc = service.primary_slot_profile("account_123456")
    assert primary_from_svc is None  # currently in platform mode
    service.update("account_123456", ModelConfigUpdate(mode="personal"))
    primary_from_svc = service.primary_slot_profile("account_123456")
    assert primary_from_svc is not None
    assert primary_from_svc.model == "primary-model"
    assert primary_from_svc.credential_ref == "account_primary"
    assert primary_from_svc.tools_verified is True
    assert primary_profile_from_settings(service.runtime_settings("account_123456")).model == "primary-model"
