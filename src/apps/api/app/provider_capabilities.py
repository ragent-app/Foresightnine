"""Explicit endpoint/model/version capabilities; unknown means JSON object only.

Profiles are deployment-controlled evidence, never inferred from provider names.
A virtual schema tool is a response envelope and is never executed.
"""
from __future__ import annotations
import json
import os
from copy import deepcopy
from dataclasses import dataclass
from typing import Mapping
from pydantic import BaseModel, ConfigDict, Field
from .phase3 import ProviderFailure


VALID_INPUT_COUNTERS = frozenset({'utf8_byte_upper_bound', 'chars_div_3'})


@dataclass(frozen=True)
class DualInputEstimate:
    """Separate conservative UTF-8 byte safety bound from ceil(chars/3) working estimate."""
    utf8_byte_upper_bound: int
    heuristic_tokens: int
    char_count: int
    heuristic_method: str = 'ceil_chars_div_3'


def estimate_utf8_byte_upper_bound(text: str, *, envelope_bytes: int = 0) -> int:
    """Conservative UTF-8 byte safety upper bound for quota admission and wire limits."""
    return len(text.encode('utf-8')) + max(0, envelope_bytes)


def estimate_heuristic_tokens(text: str, *, envelope_chars: int = 0) -> int:
    """Realistic working token estimate using ceil(chars / 3) for mixed EN/CJK text."""
    total_chars = len(text) + max(0, envelope_chars)
    return (total_chars + 2) // 3 if total_chars > 0 else 0


def estimate_dual_input(text: str, *, envelope_bytes: int = 0, envelope_chars: int = 0) -> DualInputEstimate:
    """Compute both conservative UTF-8 byte upper bound and ceil(chars / 3) token estimate."""
    return DualInputEstimate(
        utf8_byte_upper_bound=estimate_utf8_byte_upper_bound(text, envelope_bytes=envelope_bytes),
        heuristic_tokens=estimate_heuristic_tokens(text, envelope_chars=envelope_chars),
        char_count=len(text),
    )


def configured_research_max_output_tokens(environ: Mapping[str, str] | None = None) -> int | None:
    """Read FORESIGHTNINE_RESEARCH_MAX_OUTPUT_TOKENS when set; fail closed if invalid."""
    source = os.environ if environ is None else environ
    raw = source.get('FORESIGHTNINE_RESEARCH_MAX_OUTPUT_TOKENS', '').strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise _invalid() from None
    if value < 1:
        raise _invalid()
    return value


def resolve_output_token_limit(*candidates: int | None) -> int | None:
    """Return the tightest positive integer output cap across explicit policy/profile bounds."""
    limits = [value for value in candidates if type(value) is int and value > 0]
    return min(limits) if limits else None


@dataclass(frozen=True)
class CapabilityProfile:
    endpoint: str
    model: str
    version: str
    mode: str
    schema_dialect: str | None = None
    context_tokens: int | None = None
    work_context_tokens: int | None = None
    output_tokens: int | None = None
    input_counter: str | None = None
    native_tools: bool | None = None
    stream: bool | None = None
    usage: bool | None = None
    continuation: bool | None = None
    explicit_cache_control: bool = False
    generation_parameters: tuple[str, ...] | None = None
    continuation_fields: tuple[str, ...] | None = None
    dynamic_tool_definitions: bool = False


_PROFILE_KEYS = frozenset({
    'endpoint',
    'model',
    'version',
    'mode',
    'schema_dialect',
    'context_tokens',
    'work_context_tokens',
    'output_tokens',
    'input_counter',
    'native_tools',
    'stream',
    'usage',
    'continuation',
    'dynamic_tool_definitions',
    'explicit_cache_control',
    'generation_parameters',
    'continuation_fields',
})


def _invalid() -> ProviderFailure:
    return ProviderFailure('provider_configuration_invalid','Provider capability or schema configuration is invalid.',retryable=False)


class ChatGenerationParameters(BaseModel):
    """User-selected generation controls for a single personal Chat connection."""
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)

    reasoning_effort: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None


def research_profile(settings, endpoint: str, protocol: str, version: str) -> CapabilityProfile | None:
    if not getattr(settings, "personal", False):
        return configured_profile(endpoint, settings.model, protocol=protocol)
    if not settings.tools_verified:
        return None
    parameters = getattr(settings, "generation_parameters", None)
    names = tuple(parameters.model_dump(exclude_none=True)) if parameters else None
    return CapabilityProfile(endpoint, settings.model, version, mode="json_object", native_tools=True,
                             generation_parameters=names)


def _registry_entry(raw: str, protocol: str, endpoint: str, model: str) -> dict | None:
    try:
        entries = json.loads(raw)
        if not isinstance(entries, list):
            raise ValueError()
        match = None
        for entry in entries:
            if not isinstance(entry, dict) or entry['protocol'] not in ('openai_chat_completions', 'anthropic_messages'):
                raise ValueError()
            if (entry['protocol'], entry['endpoint'], entry['model']) == (protocol, endpoint, model):
                if match is not None:
                    raise ValueError()
                match = entry
        return match
    except (ValueError, KeyError, TypeError):
        raise _invalid() from None


def chat_generation_parameters(endpoint: str, model: str) -> str:
    registry = os.environ.get('FORESIGHTNINE_LLM_CAPABILITY_PROFILES', '').strip()
    if not registry:
        return os.environ.get('FORESIGHTNINE_LLM_GENERATION_PARAMETERS', '').strip()
    entry = _registry_entry(registry, 'openai_chat_completions', endpoint, model)
    if not entry or 'parameter_values' not in entry:
        return ''
    return json.dumps(entry['parameter_values'], sort_keys=True, separators=(',', ':'))


def configured_profile(endpoint: str, model: str, *, protocol: str = "openai_chat_completions") -> CapabilityProfile | None:
    raw = os.environ.get('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', '').strip()
    registry = os.environ.get('FORESIGHTNINE_LLM_CAPABILITY_PROFILES', '').strip()
    if not raw and not registry:
        return None
    try:
        if registry:
            entry = _registry_entry(registry, protocol, endpoint, model)
            if entry is None:
                return None
            value = dict(entry)
            del value['protocol']
            if 'parameter_values' in value:
                parameters = value.pop('parameter_values')
                if (protocol != 'openai_chat_completions' or not isinstance(parameters, dict)
                        or not set(parameters) <= set(value.get('generation_parameters', []))):
                    raise ValueError()
        else:
            value = json.loads(raw)
        if not isinstance(value, dict) or not {'endpoint','model','version','mode'} <= set(value) or set(value) - _PROFILE_KEYS:
            raise ValueError()
        if any(not isinstance(value[k], str) or not value[k] or len(value[k]) > 512 for k in ('endpoint','model','version','mode')):
            raise ValueError()
        if any(type(value[k]) is not int or value[k] < 1 for k in ('context_tokens','work_context_tokens','output_tokens') if k in value):
            raise ValueError()
        if any(type(value[k]) is not bool for k in ('native_tools','stream','usage','continuation','explicit_cache_control','dynamic_tool_definitions') if k in value):
            raise ValueError()
        for key in ('generation_parameters', 'continuation_fields'):
            if key in value:
                if not isinstance(value[key], list) or any(not isinstance(x, str) or not x for x in value[key]):
                    raise ValueError()
                value[key] = tuple(value[key])
        if 'input_counter' in value and value['input_counter'] not in VALID_INPUT_COUNTERS:
            raise ValueError()
        if value['mode'] not in {'json_schema','tool','json_object'}:
            raise ValueError()
        if value['mode'] != 'json_object' and value.get('schema_dialect') != 'required_nullable_v1':
            raise ValueError()
        if value['endpoint'] != endpoint or value['model'] != model:
            return None
        active_version = os.environ.get('FORESIGHTNINE_LLM_ENDPOINT_VERSION', '').strip()
        if not registry and (not active_version or active_version != value['version']):
            return None
        return CapabilityProfile(**value)
    except (ValueError, TypeError, KeyError):
        raise _invalid() from None


_SCHEMA_KEYS = frozenset({'$defs','$ref','title','description','type','properties','required',
    'additionalProperties','anyOf','items','minLength','maxLength','minItems','maxItems',
    'minimum','maximum','exclusiveMinimum','exclusiveMaximum','enum','const','default'})

def _strict_schema(schema: dict) -> dict:
    """Preserve constraints; require nullable fields only when omission means null.

    Profile owner must verify this dialect's keywords with the exact endpoint.
    Unsupported keywords/defaults fail before HTTP, never silently weaken schema.
    """
    def walk(node):
        if not isinstance(node, dict) or set(node) - _SCHEMA_KEYS:
            raise _invalid()
        node = deepcopy(node)
        original_properties = node.get('properties', {})
        for key in ('$defs','properties'):
            if key in node:
                if not isinstance(node[key], dict): raise _invalid()
                node[key] = {name: walk(child) for name, child in node[key].items()}
        if 'items' in node: node['items'] = walk(node['items'])
        if 'anyOf' in node:
            if not isinstance(node['anyOf'],list): raise _invalid()
            node['anyOf'] = [walk(child) for child in node['anyOf']]
        if node.get('type') == 'object' or 'properties' in node:
            properties = node.get('properties')
            if not isinstance(properties,dict) or node.get('additionalProperties') is not False:
                raise _invalid()
            required = node.get('required',[])
            if not isinstance(required,list) or not set(required) <= set(properties): raise _invalid()
            for name, child in properties.items():
                if name not in required:
                    nullable = child.get('type') == 'null' or any(isinstance(x,dict) and x.get('type') == 'null' for x in child.get('anyOf',[]))
                    if original_properties[name].get('default', object()) is not None or not nullable: raise _invalid()
            node['required'] = list(properties)
        # JSON Schema default is annotation; no accepted constraint is removed.
        node.pop('default', None)
        return node
    return walk(schema)


def wire_contract(contract, mode: str) -> dict:
    """Build one immutable selection; strict modes need a matching profile."""
    if contract is None or mode == 'json_object':
        return {'response_format': {'type': 'json_object'}}
    name = contract.name
    schema = _strict_schema(contract.schema)
    if mode == 'json_schema':
        return {'response_format': {'type':'json_schema','json_schema': {'name':name,'strict':True,'schema':schema}}}
    if mode == 'tool':
        return {'tools': [{'type':'function','function': {'name':name, 'description':'Return the structured result.', 'strict':True, 'parameters':schema}}],
                'tool_choice': {'type':'function','function':{'name':name}}, 'parallel_tool_calls': False}
    raise _invalid()


def contract_wire_overhead(contract, mode: str) -> int:
    """Conservative UTF-8 cost of the final wire relative to JSON object mode."""
    encode = lambda value: len(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8'))
    extra = max(0, encode(wire_contract(contract,mode))-encode(wire_contract(None,'json_object')))
    return extra + 32 if extra else 0
