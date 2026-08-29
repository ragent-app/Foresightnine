"""Generate strict Pydantic v2 models directly from the canonical JSON Schema."""
from __future__ import annotations

from datetime import date, datetime
from functools import reduce
import json
from pathlib import Path
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, create_model

CONTRACT_PATH = Path(__file__).resolve().parents[3] / "packages" / "contracts" / "schemas" / "v1" / "contracts.schema.json"
CONTRACT_SCHEMA: dict[str, Any] = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
DEFINITIONS: dict[str, dict[str, Any]] = CONTRACT_SCHEMA["$defs"]


class StrictContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


OAuthProviderName = Literal["google", "github"]


class StrictOAuthModel(StrictContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class OAuthProviderReadiness(StrictOAuthModel):
    """Public OAuth capability metadata; no provider credential is serialised."""

    provider: OAuthProviderName
    enabled: bool


class OAuthProvidersResponse(StrictOAuthModel):
    """The complete, fixed provider readiness surface for client consoles."""

    providers: tuple[OAuthProviderReadiness, OAuthProviderReadiness]


class OAuthStartRequest(StrictOAuthModel):
    """The only client-controlled value accepted by an OAuth start request."""

    return_uri: Annotated[str, Field(min_length=1, max_length=2048)]


class OAuthAuthorizationResponse(StrictOAuthModel):
    """Safe authorization parameters returned after a server-side PKCE transaction."""

    provider: OAuthProviderName
    client_id: str
    redirect_uri: str
    return_uri: str
    state: str
    code_challenge: str
    code_challenge_method: Literal["S256"]


_cache: dict[str, Any] = {}
_building: set[str] = set()


def _ref_name(reference: str) -> str:
    prefix = "#/$defs/"
    if not reference.startswith(prefix):
        raise ValueError(f"Only canonical local refs are supported: {reference}")
    return reference.removeprefix(prefix)


def _merge_object_schema(schema: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    additional_properties: Any = schema.get("additionalProperties", False if schema.get("unevaluatedProperties") is False else True)

    def visit(node: dict[str, Any]) -> None:
        nonlocal additional_properties
        if "$ref" in node:
            visit(DEFINITIONS[_ref_name(node["$ref"])])
            return
        for part in node.get("allOf", []):
            if "$ref" in part or part.get("type") == "object" or "properties" in part:
                visit(part)
        properties.update(node.get("properties", {}))
        for field_name in node.get("required", []):
            if field_name not in required:
                required.append(field_name)
        if node.get("additionalProperties") is False or node.get("unevaluatedProperties") is False:
            additional_properties = False
        elif isinstance(node.get("additionalProperties"), dict):
            additional_properties = node["additionalProperties"]

    visit(schema)
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": additional_properties}


def _literal(values: list[Any] | tuple[Any, ...]) -> Any:
    return Literal.__getitem__(tuple(values))


def _union(types: list[Any]) -> Any:
    unique: list[Any] = []
    for item in types:
        if item not in unique:
            unique.append(item)
    return unique[0] if len(unique) == 1 else Union.__getitem__(tuple(unique))


def _constraints(schema: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "minimum": "ge",
        "maximum": "le",
        "exclusiveMinimum": "gt",
        "exclusiveMaximum": "lt",
        "minLength": "min_length",
        "maxLength": "max_length",
        "minItems": "min_length",
        "maxItems": "max_length",
        "pattern": "pattern",
        "multipleOf": "multiple_of",
    }
    return {target: schema[source] for source, target in mapping.items() if source in schema}


def _annotation(schema: dict[str, Any], context: str) -> Any:
    if "$ref" in schema:
        return model_or_type(_ref_name(schema["$ref"]))
    if "const" in schema:
        return _literal([schema["const"]])
    if "enum" in schema:
        return _literal(schema["enum"])
    if "oneOf" in schema:
        return _union([_annotation(part, f"{context}Variant{index + 1}") for index, part in enumerate(schema["oneOf"])])
    if "anyOf" in schema:
        return _union([_annotation(part, f"{context}Option{index + 1}") for index, part in enumerate(schema["anyOf"])])
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        return _union([_annotation({**schema, "type": item}, context) for item in schema_type])
    if schema_type == "string":
        base: Any = {"date-time": datetime, "date": date, "uri": HttpUrl}.get(schema.get("format"), str)
    elif schema_type == "number":
        base = float
    elif schema_type == "integer":
        base = int
    elif schema_type == "boolean":
        base = bool
    elif schema_type == "null":
        return type(None)
    elif schema_type == "array":
        item_type = _annotation(schema.get("items", {}), f"{context}Item")
        base = list[item_type]
    elif schema_type == "object" or "properties" in schema or "allOf" in schema:
        merged = _merge_object_schema(schema)
        if not merged["properties"] and isinstance(merged["additionalProperties"], dict):
            base = dict[str, _annotation(merged["additionalProperties"], f"{context}Value")]
        else:
            return _build_object_model(context, merged)
    else:
        return Any
    constraints = _constraints(schema)
    return Annotated[base, Field(**constraints)] if constraints else base


def _build_object_model(name: str, schema: dict[str, Any]) -> type[StrictContractModel]:
    if name in _cache:
        cached = _cache[name]
        if isinstance(cached, type) and issubclass(cached, BaseModel):
            return cached
    if name in _building:
        raise ValueError(f"Recursive contract model is unsupported in v1: {name}")
    _building.add(name)
    merged = _merge_object_schema(schema)
    required = set(merged["required"])
    fields: dict[str, tuple[Any, Any]] = {}
    for field_name, field_schema in merged["properties"].items():
        annotation = _annotation(field_schema, f"{name}{field_name.title().replace('_', '')}")
        default = ... if field_name in required else None
        fields[field_name] = (annotation, default)
    generated = create_model(name, __base__=StrictContractModel, __module__="app.models", **fields)
    _cache[name] = generated
    _building.remove(name)
    return generated


def model_or_type(name: str) -> Any:
    if name in _cache:
        return _cache[name]
    definition = DEFINITIONS[name]
    if definition.get("type") == "object" or "properties" in definition or "allOf" in definition:
        return _build_object_model(name, definition)
    generated = _annotation(definition, name)
    _cache[name] = generated
    return generated


def model_for(name: str) -> type[StrictContractModel]:
    generated = model_or_type(name)
    if not isinstance(generated, type) or not issubclass(generated, BaseModel):
        raise TypeError(f"{name} is not an object contract")
    return generated
