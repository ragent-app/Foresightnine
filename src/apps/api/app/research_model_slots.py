"""Protocol-first 4-slot heterogeneous model routing and capability validation.

Each research squad role maps to one logical slot:
- ``fast_extractor``: Parallel read-only extraction workers (requires native tool calling).
- ``deep_reasoner``: Lead coordinator & Stage 2.5 cross-worker follow-up.
- ``bear_reviewer``: Independent adversarial bear reviewer (supports cross-family review).
- ``writer``: Earnings dossier synthesis into Memo / Deep Report modes.

Slots bind to explicit wire-protocol profiles/deployment references without
hardcoded model brands or default models. Any unset slot falls back to the
account or deployment primary research model. Capabilities are validated via
``provider_capabilities.py`` and raw secrets never enter bindings, summaries, or
errors.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict
import json
import os
import re
from types import SimpleNamespace
from typing import Any, Callable, Literal
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .anthropic_messages import _endpoint as _messages_endpoint
from .openai_compatible import (
    _LOCAL_GATEWAY_HOSTS,
    OpenAICompatibleSettings,
    _endpoint as _chat_endpoint,
    _normalize_hostname,
)
from .openai_responses import responses_endpoint as _responses_endpoint
from .phase3 import ProviderFailure
from .provider_capabilities import (
    CapabilityProfile,
    ChatGenerationParameters,
    VALID_INPUT_COUNTERS,
    configured_profile,
    configured_research_max_output_tokens,
    research_profile,
    resolve_output_token_limit,
)


ModelSlot = Literal["fast_extractor", "deep_reasoner", "bear_reviewer", "writer"]
MODEL_SLOTS: tuple[ModelSlot, ...] = (
    "fast_extractor",
    "deep_reasoner",
    "bear_reviewer",
    "writer",
)

ModelProtocol = Literal["openai_chat_completions", "openai_responses", "anthropic_messages"]
MODEL_PROTOCOLS: tuple[ModelProtocol, ...] = (
    "openai_chat_completions",
    "openai_responses",
    "anthropic_messages",
)

CapabilityMode = Literal["json_schema", "tool", "json_object"]
InputCounter = Literal["utf8_byte_upper_bound", "chars_div_3"]
CapabilitySource = Literal[
    "configured_profile",
    "explicit_binding",
    "verified_personal",
    "unknown_default",
    "unavailable",
]

SLOT_ROLES: Mapping[ModelSlot, str] = {
    "fast_extractor": "Worker A/B/C parallel extraction (requires native tool calling)",
    "deep_reasoner": "Lead Coordinator & Stage 2.5 cross-worker follow-up",
    "bear_reviewer": "Independent Adversarial Bear Reviewer (allows cross-family review)",
    "writer": "Earnings Dossier synthesis (Memo / Deep Report)",
}
SLOT_REQUIRES_NATIVE_TOOLS: Mapping[ModelSlot, bool] = {
    "fast_extractor": True,
    "deep_reasoner": False,
    "bear_reviewer": False,
    "writer": False,
}

_SLOT_DEPLOYMENTS_ENV = "FORESIGHTNINE_MODEL_SLOT_DEPLOYMENTS"
_FORBIDDEN_SECRET_KEYS = frozenset({
    "api_key",
    "apikey",
    "encrypted_api_key",
    "secret",
    "secret_key",
    "access_token",
    "auth_token",
    "password",
    "authorization",
    "bearer_token",
})
_METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata.azure.internal"})
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_RAW_SECRET_PREFIX_RE = re.compile(
    r"^(?:bearer\s+|basic\s+|apikey\s+|sk-[a-z0-9_-]{8,}|gh[po]_[a-z0-9]{8,}|akia[0-9a-z]{8,}|aiza[0-9a-z_-]{8,}|xox[bp]-[a-z0-9-]{8,})",
    re.IGNORECASE,
)
_KV_SECRET_RE = re.compile(
    r"\b(apikey|api_key|token|access_token|secret|password)\s*[:=]\s*(?:bearer\s+|apikey\s+)?[^\s,;\"'<>]+",
    re.IGNORECASE,
)
_AUTH_HEADER_RE = re.compile(
    r"\b(authorization)\s*:\s*(?:bearer\s+|apikey\s+)?[^\s,;\"'<>]+",
    re.IGNORECASE,
)
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:apikey|api_key|token|access_token|secret|key)=)[^&\s\"'<>]+",
    re.IGNORECASE,
)


class ModelSlotError(ProviderFailure):
    """Fail-closed capability or routing error for 4-slot model configuration."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        slot: ModelSlot | None = None,
        resolved: "ResolvedModelSlots | None" = None,
    ) -> None:
        super().__init__(code, message, retryable=False)
        self.slot = slot
        self.resolved = resolved


def _redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    cleaned = text
    for secret in secrets:
        if not secret:
            continue
        for variant in (secret, quote(secret, safe=""), quote_plus(secret)):
            if variant:
                cleaned = cleaned.replace(variant, "***")
    cleaned = _QUERY_SECRET_RE.sub(r"\1***", cleaned)
    cleaned = _KV_SECRET_RE.sub(lambda match: f"{match.group(1)}=***", cleaned)
    cleaned = _AUTH_HEADER_RE.sub(lambda match: f"{match.group(1)}: ***", cleaned)
    return cleaned


def _contains_forbidden_secret_key(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return any(isinstance(key, str) and key.lower() in _FORBIDDEN_SECRET_KEYS for key in value)


def _validate_base_url(raw_url: str, *, allow_private_network: bool = False) -> tuple[str, str]:
    if raw_url != raw_url.strip():
        raise ValueError("base_url must not contain surrounding whitespace")
    if not raw_url or len(raw_url) > 2048 or "\\" in raw_url or any(ord(ch) <= 32 for ch in raw_url):
        raise ValueError("base_url is invalid")
    try:
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("base_url is invalid") from None
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("base_url must not include userinfo, query parameters, or fragments")
    if not hostname or (port is not None and not 1 <= port <= 65535):
        raise ValueError("base_url host or port is invalid")
    normalized_host = hostname.rstrip(".").lower()
    local_gateway = allow_private_network and normalized_host in _LOCAL_GATEWAY_HOSTS
    allowed_schemes = {"https", "http"} if local_gateway else {"https"}
    if parsed.scheme.lower() not in allowed_schemes:
        raise ValueError("base_url must use HTTPS")
    if normalized_host in _METADATA_HOSTS:
        raise ValueError("base_url host is not permitted")
    try:
        canonical_host = _normalize_hostname(hostname)
    except ProviderFailure:
        raise ValueError("base_url host must be a valid public DNS name") from None
    normalized_url = urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path.rstrip("/"), "", ""))
    return normalized_url.rstrip("/"), canonical_host


def _validate_model_name(raw_model: str) -> str:
    if raw_model != raw_model.strip():
        raise ValueError("model must not contain surrounding whitespace")
    if not raw_model or len(raw_model) > 128 or any(ord(ch) < 0x20 for ch in raw_model):
        raise ValueError("model identifier is invalid")
    if _RAW_SECRET_PREFIX_RE.match(raw_model):
        raise ValueError("model identifier appears to contain a credential")
    return raw_model


def _validate_credential_ref(raw_ref: str) -> str:
    if raw_ref != raw_ref.strip():
        raise ValueError("credential_ref must not contain surrounding whitespace")
    if _RAW_SECRET_PREFIX_RE.match(raw_ref) or not _SAFE_REF_RE.fullmatch(raw_ref):
        raise ValueError("credential_ref must be a non-secret reference identifier")
    return raw_ref


def _endpoint_for_protocol(protocol: ModelProtocol, base_url: str) -> str:
    cleaned = base_url.rstrip("/")
    if protocol == "openai_chat_completions":
        return _chat_endpoint(cleaned)
    if protocol == "openai_responses":
        return _responses_endpoint(cleaned)
    if protocol == "anthropic_messages":
        return _messages_endpoint(cleaned)
    raise ValueError("unsupported model protocol")


def _default_version_for_protocol(protocol: ModelProtocol) -> str:
    if protocol == "openai_responses":
        return "responses-v1"
    if protocol == "anthropic_messages":
        return "2023-06-01"
    return "chat-completions-v1"


def _infer_protocol_from_endpoint(endpoint: str) -> ModelProtocol:
    path = urlsplit(endpoint).path.rstrip("/").lower()
    if path.endswith("/messages"):
        return "anthropic_messages"
    if path.endswith("/responses"):
        return "openai_responses"
    return "openai_chat_completions"


def _base_url_from_endpoint(protocol: ModelProtocol, endpoint: str) -> str:
    cleaned = endpoint.rstrip("/")
    if protocol == "openai_chat_completions" and cleaned.endswith("/chat/completions"):
        return cleaned.removesuffix("/chat/completions")
    if protocol == "openai_responses" and cleaned.endswith("/responses"):
        return cleaned.removesuffix("/responses")
    if protocol == "anthropic_messages":
        if cleaned.endswith("/v1/messages"):
            return cleaned.removesuffix("/messages")
        if cleaned.endswith("/messages"):
            return cleaned.removesuffix("/messages")
    return cleaned


class SlotCapabilities(BaseModel):
    """Explicit wire and tool capabilities aligned with ``provider_capabilities.CapabilityProfile``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(default="v1", min_length=1, max_length=64)
    mode: CapabilityMode = "json_object"
    schema_dialect: str | None = None
    context_tokens: int | None = Field(default=None, ge=1)
    work_context_tokens: int | None = Field(default=None, ge=1)
    output_tokens: int | None = Field(default=None, ge=1)
    input_counter: InputCounter | None = None
    native_tools: bool | None = None
    stream: bool | None = None
    usage: bool | None = None
    continuation: bool | None = None
    explicit_cache_control: bool = False
    generation_parameters: tuple[str, ...] | None = None
    continuation_fields: tuple[str, ...] | None = None
    dynamic_tool_definitions: bool = False

    @field_validator("schema_dialect")
    @classmethod
    def _validate_schema_dialect(cls, value: str | None) -> str | None:
        if value is not None and value != "required_nullable_v1":
            raise ValueError("schema_dialect must be 'required_nullable_v1' when set")
        return value

    @field_validator("input_counter")
    @classmethod
    def _validate_input_counter(cls, value: InputCounter | None) -> InputCounter | None:
        if value is not None and value not in VALID_INPUT_COUNTERS:
            raise ValueError("unsupported input_counter")
        return value

    @field_validator("generation_parameters", "continuation_fields")
    @classmethod
    def _validate_name_tuple(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        if any(not isinstance(item, str) or not item or item != item.strip() for item in value):
            raise ValueError("capability parameter names must be non-empty trimmed strings")
        return value

    @classmethod
    def from_capability_profile(cls, profile: CapabilityProfile) -> "SlotCapabilities":
        data = asdict(profile)
        data.pop("endpoint", None)
        data.pop("model", None)
        return cls.model_validate(data)

    def to_capability_profile(self, *, endpoint: str, model: str) -> CapabilityProfile:
        return CapabilityProfile(
            endpoint=endpoint,
            model=model,
            version=self.version,
            mode=self.mode,
            schema_dialect=self.schema_dialect,
            context_tokens=self.context_tokens,
            work_context_tokens=self.work_context_tokens,
            output_tokens=self.output_tokens,
            input_counter=self.input_counter,
            native_tools=self.native_tools,
            stream=self.stream,
            usage=self.usage,
            continuation=self.continuation,
            explicit_cache_control=self.explicit_cache_control,
            generation_parameters=self.generation_parameters,
            continuation_fields=self.continuation_fields,
            dynamic_tool_definitions=self.dynamic_tool_definitions,
        )


class ModelSlotBinding(BaseModel):
    """Configured model profile or deployment reference for a single research slot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deployment_ref: str | None = Field(default=None, min_length=1, max_length=128)
    protocol: ModelProtocol | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    credential_ref: str | None = Field(default=None, min_length=1, max_length=128)
    capabilities: SlotCapabilities | None = None
    endpoint_version: str | None = Field(default=None, min_length=1, max_length=64)
    generation_parameters: ChatGenerationParameters | None = None
    messages_auth_scheme: Literal["x-api-key", "bearer"] | None = None
    messages_max_tokens: int | None = Field(default=None, gt=0)
    messages_effort: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    tools_verified: bool | None = None
    allow_private_network: bool = False

    @model_validator(mode="before")
    @classmethod
    def _coerce_and_guard_secrets(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        if _contains_forbidden_secret_key(data):
            raise ValueError("Raw credentials are not permitted in ModelSlotBinding; use credential_ref instead.")
        updated = dict(data)
        caps = updated.get("capabilities")
        if isinstance(caps, CapabilityProfile):
            updated["capabilities"] = SlotCapabilities.from_capability_profile(caps)
        return updated

    @field_validator("deployment_ref")
    @classmethod
    def _validate_deployment_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_credential_ref(value)

    @field_validator("credential_ref")
    @classmethod
    def _validate_cred_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_credential_ref(value)

    @field_validator("model")
    @classmethod
    def _validate_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_model_name(value)

    @model_validator(mode="after")
    def _validate_binding(self) -> "ModelSlotBinding":
        if self.deployment_ref is None and not all((self.protocol, self.base_url, self.model)):
            raise ValueError("ModelSlotBinding requires protocol, base_url, and model unless deployment_ref is set")
        if self.base_url is not None:
            normalized_url, _ = _validate_base_url(self.base_url, allow_private_network=self.allow_private_network)
            object.__setattr__(self, "base_url", normalized_url)
        if self.protocol == "anthropic_messages" and self.generation_parameters is not None:
            raise ValueError("generation_parameters require openai_chat_completions or openai_responses")
        if (
            self.protocol == "openai_responses"
            and self.generation_parameters is not None
            and self.generation_parameters.seed is not None
        ):
            raise ValueError("openai_responses does not accept seed")
        if (
            self.protocol is not None
            and self.protocol != "anthropic_messages"
            and any((
                self.messages_auth_scheme is not None,
                self.messages_max_tokens is not None,
                self.messages_effort is not None,
            ))
        ):
            raise ValueError("messages options require anthropic_messages protocol")
        return self


class ModelSlotConfig(BaseModel):
    """Optional bindings for the 4 logical research model slots."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fast_extractor: ModelSlotBinding | None = None
    deep_reasoner: ModelSlotBinding | None = None
    bear_reviewer: ModelSlotBinding | None = None
    writer: ModelSlotBinding | None = None

    def get(self, slot: ModelSlot) -> ModelSlotBinding | None:
        return getattr(self, slot)

    def configured_slots(self) -> tuple[ModelSlot, ...]:
        return tuple(slot for slot in MODEL_SLOTS if getattr(self, slot) is not None)

    def is_empty(self) -> bool:
        return not any(getattr(self, slot) is not None for slot in MODEL_SLOTS)

    def as_dict(self) -> dict[ModelSlot, ModelSlotBinding]:
        return {
            slot: getattr(self, slot)
            for slot in MODEL_SLOTS
            if getattr(self, slot) is not None
        }


class ResolvedModelSlot(BaseModel):
    """Resolved, capability-validated configuration for one model slot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot: ModelSlot
    role: str
    fallback_used: bool
    source: Literal["slot_override", "primary_fallback"]
    protocol: ModelProtocol | None = None
    base_url: str | None = None
    endpoint: str | None = None
    model: str | None = None
    deployment_ref: str | None = None
    credential_ref: str | None = None
    capabilities: SlotCapabilities | None = None
    capability_profile: CapabilityProfile | None = None
    capability_source: CapabilitySource = "unavailable"
    native_tools_required: bool = False
    native_tools_supported: bool | None = None
    capability_valid: bool = False
    validation_errors: tuple[str, ...] = ()
    generation_parameters: ChatGenerationParameters | None = None
    endpoint_version: str | None = None
    messages_auth_scheme: Literal["x-api-key", "bearer"] | None = None
    messages_max_tokens: int | None = None
    messages_effort: str | None = None
    differs_from_primary: bool = False


class ResolvedModelSlots(BaseModel):
    """Resolved 4-slot routing matrix with per-slot capability validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fast_extractor: ResolvedModelSlot
    deep_reasoner: ResolvedModelSlot
    bear_reviewer: ResolvedModelSlot
    writer: ResolvedModelSlot
    primary_protocol: ModelProtocol | None = None
    primary_model: str | None = None
    primary_base_url: str | None = None
    primary_credential_ref: str | None = None
    cross_family_bear_review: bool = False
    is_valid: bool = True
    errors: tuple[str, ...] = ()

    def slot_for(self, slot: ModelSlot) -> ResolvedModelSlot:
        return getattr(self, slot)

    def __getitem__(self, slot: ModelSlot) -> ResolvedModelSlot:
        if slot not in MODEL_SLOTS:
            raise KeyError(slot)
        return getattr(self, slot)

    def slots_dict(self) -> dict[ModelSlot, ResolvedModelSlot]:
        return {slot: getattr(self, slot) for slot in MODEL_SLOTS}

    def ensure_valid(self) -> "ResolvedModelSlots":
        if not self.is_valid:
            first_error = self.errors[0] if self.errors else "Model slot capability validation failed."
            first_slot = next((slot for slot in MODEL_SLOTS if not getattr(self, slot).capability_valid), None)
            raise ModelSlotError(
                "model_slot_capability_invalid",
                first_error,
                slot=first_slot,
                resolved=self,
            )
        return self

    def summary(self) -> dict[str, Any]:
        return slot_routing_summary(self)


class SlotSummaryItem(BaseModel):
    """Secret-free per-slot summary for API responses and run diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot: ModelSlot
    role: str
    configured: bool
    fallback_used: bool
    source: Literal["slot_override", "primary_fallback"]
    protocol: ModelProtocol | None = None
    base_url: str | None = None
    endpoint_host: str | None = None
    model: str | None = None
    deployment_ref: str | None = None
    credential_ref: str | None = None
    capability_source: CapabilitySource = "unavailable"
    mode: str | None = None
    native_tools_required: bool = False
    native_tools_supported: bool | None = None
    capability_valid: bool = False
    validation_errors: tuple[str, ...] = ()
    context_tokens: int | None = None
    output_tokens: int | None = None
    input_counter: str | None = None
    differs_from_primary: bool = False
    generation_parameters: dict[str, Any] | None = None


class SlotRoutingSummary(BaseModel):
    """Secret-free summary payload for API responses and run diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["v1"] = "v1"
    is_valid: bool
    cross_family_bear_review: bool = False
    configured_slots: tuple[ModelSlot, ...] = ()
    fallback_slots: tuple[ModelSlot, ...] = ()
    heterogeneous_slots: tuple[ModelSlot, ...] = ()
    errors: tuple[str, ...] = ()
    slots: dict[ModelSlot, SlotSummaryItem]


def primary_profile_from_settings(
    settings: OpenAICompatibleSettings,
    *,
    protocol: ModelProtocol = "openai_chat_completions",
    credential_ref: str | None = None,
    endpoint_version: str | None = None,
    messages_auth_scheme: Literal["x-api-key", "bearer"] | None = None,
    messages_max_tokens: int | None = None,
    messages_effort: str | None = None,
) -> ModelSlotBinding:
    """Create a secret-free primary ``ModelSlotBinding`` from runtime settings."""
    endpoint = _endpoint_for_protocol(protocol, settings.base_url)
    version = endpoint_version or _default_version_for_protocol(protocol)
    profile = research_profile(settings, endpoint, protocol, version)
    caps = SlotCapabilities.from_capability_profile(profile) if profile is not None else None
    effective_ref = credential_ref or ("account_primary" if settings.personal else "platform_primary")
    return ModelSlotBinding(
        protocol=protocol,
        base_url=settings.base_url,
        model=settings.model,
        credential_ref=effective_ref,
        capabilities=caps,
        endpoint_version=endpoint_version,
        generation_parameters=settings.generation_parameters if protocol != "anthropic_messages" else None,
        messages_auth_scheme=messages_auth_scheme if protocol == "anthropic_messages" else None,
        messages_max_tokens=messages_max_tokens if protocol == "anthropic_messages" else None,
        messages_effort=messages_effort if protocol == "anthropic_messages" else None,
        tools_verified=settings.tools_verified if settings.personal else None,
        allow_private_network=settings.allow_private_network,
    )


def _coerce_primary_profile(
    primary_profile: ModelSlotBinding | CapabilityProfile | OpenAICompatibleSettings | Mapping[str, Any] | None,
) -> tuple[ModelSlotBinding | None, list[str]]:
    """Normalize primary profile input without ever retaining raw API keys."""
    if primary_profile is None:
        return None, []
    if isinstance(primary_profile, ModelSlotBinding):
        return primary_profile, []
    if isinstance(primary_profile, OpenAICompatibleSettings):
        secrets = [primary_profile.api_key] if primary_profile.api_key else []
        return primary_profile_from_settings(primary_profile), secrets
    if isinstance(primary_profile, CapabilityProfile):
        protocol = _infer_protocol_from_endpoint(primary_profile.endpoint)
        base_url = _base_url_from_endpoint(protocol, primary_profile.endpoint)
        return (
            ModelSlotBinding(
                protocol=protocol,
                base_url=base_url,
                model=primary_profile.model,
                credential_ref="profile_primary",
                capabilities=SlotCapabilities.from_capability_profile(primary_profile),
                endpoint_version=primary_profile.version,
                tools_verified=primary_profile.native_tools,
            ),
            [],
        )
    if isinstance(primary_profile, Mapping):
        if _contains_forbidden_secret_key(primary_profile):
            raise ModelSlotError(
                "model_slot_configuration_invalid",
                "Raw credentials are not permitted in primary_profile; pass credential_ref or OpenAICompatibleSettings.",
            )
        try:
            return ModelSlotBinding.model_validate(primary_profile), []
        except ValueError as error:
            raise ModelSlotError("model_slot_configuration_invalid", str(error)) from None
    raise ModelSlotError("model_slot_configuration_invalid", "Unsupported primary_profile type.")


def _coerce_slot_config(
    slot_overrides: ModelSlotConfig | Mapping[str, Any] | None,
) -> ModelSlotConfig:
    if slot_overrides is None:
        return ModelSlotConfig()
    if isinstance(slot_overrides, ModelSlotConfig):
        return slot_overrides
    if isinstance(slot_overrides, Mapping):
        if _contains_forbidden_secret_key(slot_overrides):
            raise ModelSlotError(
                "model_slot_configuration_invalid",
                "Raw credentials are not permitted in slot_overrides; use credential_ref instead.",
            )
        for slot_val in slot_overrides.values():
            if _contains_forbidden_secret_key(slot_val):
                raise ModelSlotError(
                    "model_slot_configuration_invalid",
                    "Raw credentials are not permitted in slot bindings; use credential_ref instead.",
                )
        try:
            return ModelSlotConfig.model_validate(slot_overrides)
        except ValueError as error:
            raise ModelSlotError("model_slot_configuration_invalid", _redact_text(str(error))) from None
    raise ModelSlotError("model_slot_configuration_invalid", "Unsupported slot_overrides type.")


def _load_env_deployments(environ: Mapping[str, str] | None = None) -> dict[str, ModelSlotBinding]:
    source = os.environ if environ is None else environ
    raw = source.get(_SLOT_DEPLOYMENTS_ENV, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("deployment registry must be a JSON object")
        registry: dict[str, ModelSlotBinding] = {}
        for ref, value in parsed.items():
            if not isinstance(ref, str) or not _SAFE_REF_RE.fullmatch(ref):
                raise ValueError("deployment_ref key is invalid")
            if _contains_forbidden_secret_key(value):
                raise ValueError("deployment registry entries must not contain raw credentials")
            registry[ref] = ModelSlotBinding.model_validate(value)
        return registry
    except (ValueError, TypeError):
        raise ModelSlotError(
            "model_slot_configuration_invalid",
            "Deployment slot registry configuration is invalid.",
        ) from None


def _coerce_deployment_registry(
    deployment_registry: Mapping[str, ModelSlotBinding | Mapping[str, Any]] | None,
) -> dict[str, ModelSlotBinding] | None:
    if deployment_registry is None:
        return None
    normalized: dict[str, ModelSlotBinding] = {}
    for ref, entry in deployment_registry.items():
        if isinstance(entry, ModelSlotBinding):
            normalized[ref] = entry
        elif isinstance(entry, Mapping):
            if _contains_forbidden_secret_key(entry):
                raise ModelSlotError(
                    "model_slot_configuration_invalid",
                    "Raw credentials are not permitted in deployment_registry entries.",
                )
            try:
                normalized[ref] = ModelSlotBinding.model_validate(entry)
            except ValueError as error:
                raise ModelSlotError("model_slot_configuration_invalid", _redact_text(str(error))) from None
        else:
            raise ModelSlotError("model_slot_configuration_invalid", "Unsupported deployment_registry entry type.")
    return normalized


def _resolve_deployment_ref(
    binding: ModelSlotBinding,
    explicit_registry: dict[str, ModelSlotBinding] | None,
    env_registry: dict[str, ModelSlotBinding],
) -> ModelSlotBinding:
    if binding.deployment_ref is None:
        return binding
    registry = explicit_registry if explicit_registry is not None else env_registry
    base_entry = registry.get(binding.deployment_ref)
    if base_entry is None:
        if explicit_registry is not None or not all((binding.protocol, binding.base_url, binding.model)):
            raise ModelSlotError(
                "model_slot_configuration_invalid",
                f"Unknown or unconfigured deployment_ref '{binding.deployment_ref}'.",
            )
        return binding
    merged = base_entry.model_dump(exclude_none=True)
    override = binding.model_dump(exclude_unset=True, exclude_none=True)
    merged.update(override)
    return ModelSlotBinding.model_validate(merged)


def _with_output_limit(profile: CapabilityProfile, max_output_override: int | None) -> CapabilityProfile:
    capped_out = resolve_output_token_limit(profile.output_tokens, max_output_override)
    if capped_out == profile.output_tokens:
        return profile
    return CapabilityProfile(**{**asdict(profile), "output_tokens": capped_out})


def _merge_capabilities(
    *,
    env_profile: CapabilityProfile | None,
    explicit_caps: SlotCapabilities | None,
    endpoint: str,
    model: str,
    protocol: ModelProtocol,
    binding: ModelSlotBinding,
    max_output_override: int | None,
) -> tuple[CapabilityProfile, SlotCapabilities, CapabilitySource]:
    version = binding.endpoint_version or _default_version_for_protocol(protocol)
    if env_profile is not None and explicit_caps is None:
        profile = _with_output_limit(env_profile, max_output_override)
        return profile, SlotCapabilities.from_capability_profile(profile), "configured_profile"

    if env_profile is not None and explicit_caps is not None:
        if env_profile.native_tools is False:
            native_tools: bool | None = False
        elif env_profile.native_tools is True:
            native_tools = False if explicit_caps.native_tools is False else True
        else:
            native_tools = False if explicit_caps.native_tools is False else None
        context_candidates = [c for c in (env_profile.context_tokens, explicit_caps.context_tokens) if c is not None]
        context_tokens = min(context_candidates) if context_candidates else None
        work_candidates = [
            c for c in (env_profile.work_context_tokens, explicit_caps.work_context_tokens) if c is not None
        ]
        work_context_tokens = min(work_candidates) if work_candidates else None
        output_tokens = resolve_output_token_limit(
            env_profile.output_tokens,
            explicit_caps.output_tokens,
            max_output_override,
        )
        merged_profile = CapabilityProfile(
            endpoint=env_profile.endpoint,
            model=env_profile.model,
            version=env_profile.version,
            mode=env_profile.mode,
            schema_dialect=env_profile.schema_dialect,
            context_tokens=context_tokens,
            work_context_tokens=work_context_tokens,
            output_tokens=output_tokens,
            input_counter=env_profile.input_counter or explicit_caps.input_counter,
            native_tools=native_tools,
            stream=env_profile.stream if env_profile.stream is not None else explicit_caps.stream,
            usage=env_profile.usage if env_profile.usage is not None else explicit_caps.usage,
            continuation=env_profile.continuation if env_profile.continuation is not None else explicit_caps.continuation,
            explicit_cache_control=env_profile.explicit_cache_control or explicit_caps.explicit_cache_control,
            generation_parameters=env_profile.generation_parameters or explicit_caps.generation_parameters,
            continuation_fields=env_profile.continuation_fields or explicit_caps.continuation_fields,
            dynamic_tool_definitions=env_profile.dynamic_tool_definitions or explicit_caps.dynamic_tool_definitions,
        )
        return merged_profile, SlotCapabilities.from_capability_profile(merged_profile), "configured_profile"

    if explicit_caps is not None:
        profile = _with_output_limit(
            explicit_caps.to_capability_profile(endpoint=endpoint, model=model),
            max_output_override,
        )
        return profile, SlotCapabilities.from_capability_profile(profile), "explicit_binding"

    if binding.tools_verified is True:
        settings_proxy = SimpleNamespace(
            personal=True,
            model=model,
            tools_verified=True,
            generation_parameters=binding.generation_parameters,
        )
        verified_profile = research_profile(settings_proxy, endpoint, protocol, version)
        if verified_profile is not None:
            profile = _with_output_limit(verified_profile, max_output_override)
            return profile, SlotCapabilities.from_capability_profile(profile), "verified_personal"

    fallback_profile = CapabilityProfile(
        endpoint=endpoint,
        model=model,
        version=version,
        mode="json_object",
        output_tokens=max_output_override,
        native_tools=False if binding.tools_verified is False else None,
    )
    return fallback_profile, SlotCapabilities.from_capability_profile(fallback_profile), "unknown_default"


def _validate_resolved_slot(
    *,
    slot: ModelSlot,
    binding: ModelSlotBinding,
    capability_profile: CapabilityProfile,
    fallback_used: bool,
) -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []
    requires_native = SLOT_REQUIRES_NATIVE_TOOLS[slot]
    if requires_native and capability_profile.native_tools is not True:
        state = "disabled" if capability_profile.native_tools is False else "unverified/unknown"
        errors.append(
            f"Slot '{slot}' requires verified native tool calling capability (native_tools=True), but resolved capability is {state}."
        )
    if capability_profile.mode not in {"json_schema", "tool", "json_object"}:
        errors.append(f"Slot '{slot}' has unsupported capability mode '{capability_profile.mode}'.")
    if capability_profile.mode != "json_object" and capability_profile.schema_dialect != "required_nullable_v1":
        errors.append(
            f"Slot '{slot}' mode '{capability_profile.mode}' requires schema_dialect='required_nullable_v1'."
        )
    if (
        binding.generation_parameters is not None
        and capability_profile.generation_parameters is not None
    ):
        supplied_keys = set(binding.generation_parameters.model_dump(exclude_none=True))
        allowed_keys = set(capability_profile.generation_parameters)
        disallowed = sorted(supplied_keys - allowed_keys)
        if disallowed:
            errors.append(
                f"Slot '{slot}' generation_parameters contain unsupported parameter(s): {', '.join(disallowed)}."
            )
    if (
        binding.protocol == "anthropic_messages"
        and capability_profile.continuation is False
        and requires_native
    ):
        errors.append(f"Slot '{slot}' requires continuation support for multi-turn native tool calls.")
    if fallback_used and binding.model is None:
        errors.append(f"Slot '{slot}' could not resolve a primary model.")
    return len(errors) == 0, tuple(errors)


def _resolve_single_slot(
    *,
    slot: ModelSlot,
    primary_binding: ModelSlotBinding | None,
    override_binding: ModelSlotBinding | None,
    explicit_registry: dict[str, ModelSlotBinding] | None,
    env_registry: dict[str, ModelSlotBinding],
    capability_lookup: Callable[[str, str, str], CapabilityProfile | None],
    max_output_override: int | None,
    secrets: Iterable[str],
) -> ResolvedModelSlot:
    role = SLOT_ROLES[slot]
    requires_native = SLOT_REQUIRES_NATIVE_TOOLS[slot]
    fallback_used = override_binding is None
    source: Literal["slot_override", "primary_fallback"] = "primary_fallback" if fallback_used else "slot_override"

    raw_binding = primary_binding if fallback_used else override_binding
    if raw_binding is None:
        msg = f"Slot '{slot}' has no binding and no primary research model profile was provided."
        return ResolvedModelSlot(
            slot=slot,
            role=role,
            fallback_used=True,
            source="primary_fallback",
            native_tools_required=requires_native,
            capability_valid=False,
            validation_errors=(msg,),
        )

    try:
        resolved_binding = _resolve_deployment_ref(raw_binding, explicit_registry, env_registry)
    except ModelSlotError as error:
        return ResolvedModelSlot(
            slot=slot,
            role=role,
            fallback_used=fallback_used,
            source=source,
            deployment_ref=raw_binding.deployment_ref,
            credential_ref=raw_binding.credential_ref,
            native_tools_required=requires_native,
            capability_valid=False,
            validation_errors=(_redact_text(str(error), secrets),),
        )

    if not resolved_binding.protocol or not resolved_binding.base_url or not resolved_binding.model:
        msg = f"Slot '{slot}' binding is incomplete: protocol, base_url, and model are required."
        return ResolvedModelSlot(
            slot=slot,
            role=role,
            fallback_used=fallback_used,
            source=source,
            protocol=resolved_binding.protocol,
            base_url=resolved_binding.base_url,
            model=resolved_binding.model,
            deployment_ref=resolved_binding.deployment_ref,
            credential_ref=resolved_binding.credential_ref,
            native_tools_required=requires_native,
            capability_valid=False,
            validation_errors=(msg,),
        )

    endpoint = _endpoint_for_protocol(resolved_binding.protocol, resolved_binding.base_url)
    try:
        env_cap = capability_lookup(endpoint, resolved_binding.model, resolved_binding.protocol)
    except ProviderFailure as error:
        msg = _redact_text(
            f"Slot '{slot}' capability configuration is invalid: {error}",
            secrets,
        )
        return ResolvedModelSlot(
            slot=slot,
            role=role,
            fallback_used=fallback_used,
            source=source,
            protocol=resolved_binding.protocol,
            base_url=resolved_binding.base_url,
            endpoint=endpoint,
            model=resolved_binding.model,
            deployment_ref=resolved_binding.deployment_ref,
            credential_ref=resolved_binding.credential_ref,
            native_tools_required=requires_native,
            capability_valid=False,
            validation_errors=(msg,),
        )

    cap_profile, slot_caps, cap_source = _merge_capabilities(
        env_profile=env_cap,
        explicit_caps=resolved_binding.capabilities,
        endpoint=endpoint,
        model=resolved_binding.model,
        protocol=resolved_binding.protocol,
        binding=resolved_binding,
        max_output_override=max_output_override,
    )
    is_valid, validation_errors = _validate_resolved_slot(
        slot=slot,
        binding=resolved_binding,
        capability_profile=cap_profile,
        fallback_used=fallback_used,
    )
    differs_from_primary = (
        primary_binding is not None
        and (resolved_binding.protocol, resolved_binding.base_url, resolved_binding.model)
        != (primary_binding.protocol, primary_binding.base_url, primary_binding.model)
    )
    return ResolvedModelSlot(
        slot=slot,
        role=role,
        fallback_used=fallback_used,
        source=source,
        protocol=resolved_binding.protocol,
        base_url=resolved_binding.base_url,
        endpoint=endpoint,
        model=resolved_binding.model,
        deployment_ref=resolved_binding.deployment_ref,
        credential_ref=resolved_binding.credential_ref,
        capabilities=slot_caps,
        capability_profile=cap_profile,
        capability_source=cap_source,
        native_tools_required=requires_native,
        native_tools_supported=cap_profile.native_tools,
        capability_valid=is_valid,
        validation_errors=validation_errors,
        generation_parameters=resolved_binding.generation_parameters,
        endpoint_version=resolved_binding.endpoint_version or cap_profile.version,
        messages_auth_scheme=resolved_binding.messages_auth_scheme,
        messages_max_tokens=resolved_binding.messages_max_tokens,
        messages_effort=resolved_binding.messages_effort,
        differs_from_primary=differs_from_primary,
    )


def _prepare_resolution_env(
    deployment_registry: Mapping[str, ModelSlotBinding | Mapping[str, Any]] | None,
    capability_resolver: Callable[[str, str, str], CapabilityProfile | None] | None,
) -> tuple[
    dict[str, ModelSlotBinding] | None,
    dict[str, ModelSlotBinding],
    Callable[[str, str, str], CapabilityProfile | None],
    int | None,
]:
    explicit_registry = _coerce_deployment_registry(deployment_registry)
    env_registry = _load_env_deployments() if explicit_registry is None else {}
    try:
        max_output_override = configured_research_max_output_tokens()
    except ProviderFailure as error:
        raise ModelSlotError("model_slot_capability_invalid", str(error)) from None
    lookup = (
        capability_resolver
        if capability_resolver is not None
        else (lambda endpoint, model, protocol: configured_profile(endpoint, model, protocol=protocol))
    )
    return explicit_registry, env_registry, lookup, max_output_override


def resolve_model_slots(
    primary_profile: ModelSlotBinding | CapabilityProfile | OpenAICompatibleSettings | Mapping[str, Any] | None,
    slot_overrides: ModelSlotConfig | Mapping[str, Any] | None = None,
    *,
    strict: bool = True,
    deployment_registry: Mapping[str, ModelSlotBinding | Mapping[str, Any]] | None = None,
    capability_resolver: Callable[[str, str, str], CapabilityProfile | None] | None = None,
) -> ResolvedModelSlots:
    """Resolve and validate the 4 heterogeneous model slots against ``provider_capabilities.py``.

    Unset slots fall back to ``primary_profile`` with ``fallback_used=True``.
    When ``strict=True`` (default), any capability failure (such as ``fast_extractor``
    lacking native tool calling) fails closed by raising ``ModelSlotError``.
    When ``strict=False``, returns ``ResolvedModelSlots`` with ``is_valid=False`` and
    per-slot ``validation_errors`` for inspection or API diagnostics.
    """
    primary_binding, secrets = _coerce_primary_profile(primary_profile)
    slot_config = _coerce_slot_config(slot_overrides)
    explicit_registry, env_registry, lookup, max_output_override = _prepare_resolution_env(
        deployment_registry,
        capability_resolver,
    )

    resolved_map: dict[ModelSlot, ResolvedModelSlot] = {}
    all_errors: list[str] = []
    for slot in MODEL_SLOTS:
        resolved_slot = _resolve_single_slot(
            slot=slot,
            primary_binding=primary_binding,
            override_binding=slot_config.get(slot),
            explicit_registry=explicit_registry,
            env_registry=env_registry,
            capability_lookup=lookup,
            max_output_override=max_output_override,
            secrets=secrets,
        )
        resolved_map[slot] = resolved_slot
        all_errors.extend(resolved_slot.validation_errors)

    deep = resolved_map["deep_reasoner"]
    bear = resolved_map["bear_reviewer"]
    cross_family_bear_review = (
        bear.capability_valid
        and deep.capability_valid
        and (bear.protocol, bear.base_url, bear.model) != (deep.protocol, deep.base_url, deep.model)
    )
    resolved = ResolvedModelSlots(
        fast_extractor=resolved_map["fast_extractor"],
        deep_reasoner=resolved_map["deep_reasoner"],
        bear_reviewer=resolved_map["bear_reviewer"],
        writer=resolved_map["writer"],
        primary_protocol=primary_binding.protocol if primary_binding else None,
        primary_model=primary_binding.model if primary_binding else None,
        primary_base_url=primary_binding.base_url if primary_binding else None,
        primary_credential_ref=primary_binding.credential_ref if primary_binding else None,
        cross_family_bear_review=cross_family_bear_review,
        is_valid=len(all_errors) == 0,
        errors=tuple(all_errors),
    )
    if strict:
        return resolved.ensure_valid()
    return resolved


def _endpoint_host(base_url: str | None) -> str | None:
    if not base_url:
        return None
    try:
        return urlsplit(base_url).hostname
    except ValueError:
        return None


def slot_routing_summary_model(
    target: ResolvedModelSlots | ModelSlotConfig | Mapping[str, Any] | None,
    *,
    primary_profile: ModelSlotBinding | CapabilityProfile | OpenAICompatibleSettings | Mapping[str, Any] | None = None,
    deployment_registry: Mapping[str, ModelSlotBinding | Mapping[str, Any]] | None = None,
) -> SlotRoutingSummary:
    """Build a typed, secret-free ``SlotRoutingSummary`` for API responses and diagnostics."""
    if isinstance(target, ResolvedModelSlots):
        resolved = target
    elif primary_profile is not None:
        resolved = resolve_model_slots(
            primary_profile,
            target,
            strict=False,
            deployment_registry=deployment_registry,
        )
    else:
        slot_config = _coerce_slot_config(target)
        explicit_registry, env_registry, lookup, max_output_override = _prepare_resolution_env(
            deployment_registry,
            None,
        )
        resolved_map: dict[ModelSlot, ResolvedModelSlot] = {}
        all_errors: list[str] = []
        for slot in MODEL_SLOTS:
            override_binding = slot_config.get(slot)
            if override_binding is None:
                resolved_map[slot] = ResolvedModelSlot(
                    slot=slot,
                    role=SLOT_ROLES[slot],
                    fallback_used=True,
                    source="primary_fallback",
                    native_tools_required=SLOT_REQUIRES_NATIVE_TOOLS[slot],
                    capability_source="unavailable",
                    capability_valid=False,
                    validation_errors=(),
                )
            else:
                slot_entry = _resolve_single_slot(
                    slot=slot,
                    primary_binding=None,
                    override_binding=override_binding,
                    explicit_registry=explicit_registry,
                    env_registry=env_registry,
                    capability_lookup=lookup,
                    max_output_override=max_output_override,
                    secrets=(),
                )
                resolved_map[slot] = slot_entry
                all_errors.extend(slot_entry.validation_errors)
        deep = resolved_map["deep_reasoner"]
        bear = resolved_map["bear_reviewer"]
        cross_family = (
            not bear.fallback_used
            and bear.capability_valid
            and (bear.protocol, bear.base_url, bear.model) != (deep.protocol, deep.base_url, deep.model)
        )
        resolved = ResolvedModelSlots(
            fast_extractor=resolved_map["fast_extractor"],
            deep_reasoner=resolved_map["deep_reasoner"],
            bear_reviewer=resolved_map["bear_reviewer"],
            writer=resolved_map["writer"],
            cross_family_bear_review=cross_family,
            is_valid=len(all_errors) == 0,
            errors=tuple(all_errors),
        )

    items: dict[ModelSlot, SlotSummaryItem] = {}
    configured_slots: list[ModelSlot] = []
    fallback_slots: list[ModelSlot] = []
    heterogeneous_slots: list[ModelSlot] = []

    for slot in MODEL_SLOTS:
        entry = resolved.slot_for(slot)
        if not entry.fallback_used:
            configured_slots.append(slot)
        else:
            fallback_slots.append(slot)
        if entry.differs_from_primary:
            heterogeneous_slots.append(slot)

        caps = entry.capabilities
        gen_params = (
            entry.generation_parameters.model_dump(exclude_none=True)
            if entry.generation_parameters is not None
            else None
        )
        items[slot] = SlotSummaryItem(
            slot=slot,
            role=entry.role,
            configured=not entry.fallback_used,
            fallback_used=entry.fallback_used,
            source=entry.source,
            protocol=entry.protocol,
            base_url=entry.base_url,
            endpoint_host=_endpoint_host(entry.base_url),
            model=entry.model,
            deployment_ref=entry.deployment_ref,
            credential_ref=entry.credential_ref,
            capability_source=entry.capability_source,
            mode=caps.mode if caps else None,
            native_tools_required=entry.native_tools_required,
            native_tools_supported=entry.native_tools_supported,
            capability_valid=entry.capability_valid,
            validation_errors=entry.validation_errors,
            context_tokens=caps.context_tokens if caps else None,
            output_tokens=caps.output_tokens if caps else None,
            input_counter=caps.input_counter if caps else None,
            differs_from_primary=entry.differs_from_primary,
            generation_parameters=gen_params or None,
        )

    return SlotRoutingSummary(
        is_valid=resolved.is_valid,
        cross_family_bear_review=resolved.cross_family_bear_review,
        configured_slots=tuple(configured_slots),
        fallback_slots=tuple(fallback_slots),
        heterogeneous_slots=tuple(heterogeneous_slots),
        errors=resolved.errors,
        slots=items,
    )


def slot_routing_summary(
    target: ResolvedModelSlots | ModelSlotConfig | Mapping[str, Any] | None,
    *,
    primary_profile: ModelSlotBinding | CapabilityProfile | OpenAICompatibleSettings | Mapping[str, Any] | None = None,
    deployment_registry: Mapping[str, ModelSlotBinding | Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Serialize a secret-free 4-slot routing summary dictionary for API responses & run diagnostics."""
    return slot_routing_summary_model(
        target,
        primary_profile=primary_profile,
        deployment_registry=deployment_registry,
    ).model_dump(mode="json", exclude_none=True)


__all__ = [
    "CapabilityMode",
    "CapabilitySource",
    "InputCounter",
    "MODEL_PROTOCOLS",
    "MODEL_SLOTS",
    "ModelProtocol",
    "ModelSlot",
    "ModelSlotBinding",
    "ModelSlotConfig",
    "ModelSlotError",
    "ResolvedModelSlot",
    "ResolvedModelSlots",
    "SLOT_REQUIRES_NATIVE_TOOLS",
    "SLOT_ROLES",
    "SlotCapabilities",
    "SlotRoutingSummary",
    "SlotSummaryItem",
    "primary_profile_from_settings",
    "resolve_model_slots",
    "slot_routing_summary",
    "slot_routing_summary_model",
]
