"""Explicit endpoint/model/version capabilities; unknown means JSON object only.

Profiles are deployment-controlled evidence, never inferred from provider names.
A virtual schema tool is a response envelope and is never executed.
"""
from __future__ import annotations
import json
import os
from copy import deepcopy
from dataclasses import dataclass
from .phase3 import ProviderFailure


@dataclass(frozen=True)
class CapabilityProfile:
    endpoint: str
    model: str
    version: str
    mode: str
    schema_dialect: str | None = None
    context_tokens: int | None = None
    output_tokens: int | None = None
    input_counter: str | None = None
    native_tools: bool | None = None
    stream: bool | None = None
    usage: bool | None = None
    continuation: bool | None = None
    generation_parameters: tuple[str, ...] | None = None
    continuation_fields: tuple[str, ...] | None = None


def _invalid() -> ProviderFailure:
    return ProviderFailure('provider_configuration_invalid','Provider capability or schema configuration is invalid.',retryable=False)


def configured_profile(endpoint: str, model: str) -> CapabilityProfile | None:
    raw = os.environ.get('FORESIGHTNINE_LLM_CAPABILITY_PROFILE', '').strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or not {'endpoint','model','version','mode'} <= set(value) or set(value) - {'endpoint','model','version','mode','schema_dialect','context_tokens','output_tokens','input_counter','native_tools','stream','usage','continuation','generation_parameters','continuation_fields'}:
            raise ValueError()
        if any(not isinstance(value[k], str) or not value[k] or len(value[k]) > 512 for k in ('endpoint','model','version','mode')):
            raise ValueError()
        if any(type(value[k]) is not int or value[k] < 1 for k in ('context_tokens','output_tokens') if k in value):
            raise ValueError()
        if any(type(value[k]) is not bool for k in ('native_tools','stream','usage','continuation') if k in value):
            raise ValueError()
        for key in ('generation_parameters', 'continuation_fields'):
            if key in value:
                if not isinstance(value[key], list) or any(not isinstance(x, str) or not x for x in value[key]):
                    raise ValueError()
                value[key] = tuple(value[key])
        if 'input_counter' in value and value['input_counter'] != 'utf8_byte_upper_bound':
            raise ValueError()
        if value['mode'] not in {'json_schema','tool','json_object'}:
            raise ValueError()
        if value['mode'] != 'json_object' and value.get('schema_dialect') != 'required_nullable_v1':
            raise ValueError()
        if value['endpoint'] != endpoint or value['model'] != model:
            return None
        active_version = os.environ.get('FORESIGHTNINE_LLM_ENDPOINT_VERSION', '').strip()
        if not active_version or active_version != value['version']:
            return None
        return CapabilityProfile(**value)
    except (ValueError, TypeError, json.JSONDecodeError):
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
