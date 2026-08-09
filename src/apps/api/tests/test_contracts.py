from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from app import models
from app.contract_models import CONTRACT_SCHEMA, DEFINITIONS
from app.registry import REGISTRY

from .helpers import WORKSPACE_ROOT, assert_contract


def _object_shape(schema: dict[str, object]) -> tuple[set[str], set[str]]:
    properties: set[str] = set()
    required: set[str] = set()

    def visit(node: dict[str, object]) -> None:
        reference = node.get("$ref")
        if isinstance(reference, str):
            visit(DEFINITIONS[reference.removeprefix("#/$defs/")])
            return
        for part in node.get("allOf", []):
            if isinstance(part, dict) and ("$ref" in part or part.get("type") == "object" or "properties" in part):
                visit(part)
        raw_properties = node.get("properties", {})
        if isinstance(raw_properties, dict):
            properties.update(raw_properties)
        raw_required = node.get("required", [])
        if isinstance(raw_required, list):
            required.update(str(item) for item in raw_required)

    visit(schema)
    return properties, required


def test_canonical_schema_is_valid_and_compatibility_files_are_aliases() -> None:
    Draft202012Validator.check_schema(CONTRACT_SCHEMA)
    for name in ("research.schema.json", "entities.schema.json"):
        alias = json.loads((WORKSPACE_ROOT / "packages" / "contracts" / "schemas" / "v1" / name).read_text(encoding="utf-8"))
        assert alias["$ref"] == "contracts.schema.json"


def test_every_core_entity_composes_required_metadata() -> None:
    core_entities = {
        "ResearchSession", "ThesisDocument", "EvidenceItem", "ChallengeRecord", "Worldline", "WorldlineBundle",
        "ProbabilityAssessment", "AssetImpact", "AssetRegistryEntry", "AssetRegistrySnapshot", "MarketDataManifest",
        "MarketDataShard", "FactorLibraryManifest", "FactorLibraryShard", "ClientComputationAttestation", "Operation",
    }
    for name in core_entities:
        refs = {part.get("$ref") for part in DEFINITIONS[name].get("allOf", [])}
        assert "#/$defs/ContractMetadata" in refs, name
        _, required = _object_shape(DEFINITIONS[name])
        assert {"schema_version", "created_at", "source_refs", "content_hash"} <= required, name


def test_pydantic_models_are_generated_with_exact_field_and_required_sets() -> None:
    for name in models._MODEL_NAMES:
        model = getattr(models, name)
        properties, required = _object_shape(DEFINITIONS[name])
        assert set(model.model_fields) == properties, name
        assert {field_name for field_name, field in model.model_fields.items() if field.is_required()} == required, name
        assert model.model_config.get("extra") == "forbid", name


def test_every_generated_pydantic_object_rejects_unknown_fields() -> None:
    for name in models._MODEL_NAMES:
        model = getattr(models, name)
        try:
            model.model_validate({"unexpected_phase1_field": True})
        except ValidationError as error:
            assert any(item["type"] == "extra_forbidden" for item in error.errors()), name
        else:
            raise AssertionError(f"{name} accepted an unknown field")


def test_registry_snapshot_and_all_entries_validate() -> None:
    payload = REGISTRY.model_dump(mode="json", exclude_none=True)
    assert_contract(payload, "AssetRegistrySnapshot")
    assert len(payload["assets"]) == 36
    for asset in payload["assets"]:
        assert_contract(asset, "AssetRegistryEntry")


def test_versioned_acceptance_fixtures_a_through_g_validate() -> None:
    fixture_root = WORKSPACE_ROOT / "packages" / "contracts" / "fixtures" / "v1" / "scenarios"
    observed: set[str] = set()
    for path in sorted(fixture_root.glob("*.json")):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        assert_contract(fixture, "AcceptanceFixture")
        observed.add(fixture["scenario"])
    assert observed == set("ABCDEFG")


def test_canonical_schema_rejects_unknown_nested_and_top_level_fields() -> None:
    asset = REGISTRY.assets[0].model_dump(mode="json", exclude_none=True)
    asset["unexpected"] = True
    validator = Draft202012Validator({"$ref": "#/$defs/AssetRegistryEntry", "$defs": CONTRACT_SCHEMA["$defs"]}, format_checker=FormatChecker())
    assert list(validator.iter_errors(asset))
    clean = REGISTRY.assets[0].model_dump(mode="json", exclude_none=True)
    clean["data_license"]["unexpected"] = True
    assert list(validator.iter_errors(clean))
