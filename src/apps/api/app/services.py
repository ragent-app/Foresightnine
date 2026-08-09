from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel

from .contract_models import CONTRACT_SCHEMA


class ContractViolation(ValueError):
    def __init__(self, contract: str, errors: list[str]) -> None:
        super().__init__(f"{contract} violates canonical v1 contract: {'; '.join(errors)}")
        self.contract = contract
        self.errors = errors


def canonical_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: item.isoformat() if hasattr(item, "isoformat") else str(item),
    )


def content_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def deterministic_id(namespace: str, material: str) -> str:
    digest = sha256(f"{namespace}\0{material}".encode("utf-8")).hexdigest()[:24]
    return f"{namespace}:{digest}"


def validator_for(contract: str) -> Draft202012Validator:
    return Draft202012Validator(
        {"$ref": f"#/$defs/{contract}", "$defs": CONTRACT_SCHEMA["$defs"]},
        format_checker=FormatChecker(),
    )


def validate_payload(value: BaseModel | dict[str, Any], contract: str) -> None:
    payload = value.model_dump(mode="json", exclude_none=True) if isinstance(value, BaseModel) else value
    errors = sorted(validator_for(contract).iter_errors(payload), key=lambda error: list(error.absolute_path))
    if errors:
        details = [f"{'/'.join(map(str, error.absolute_path)) or 'root'}: {error.message}" for error in errors]
        raise ContractViolation(contract, details)


def validate_probability_semantics(payload: BaseModel) -> None:
    value = payload.model_dump(mode="json", exclude_none=True)
    premise = value["premise_probability_range"]
    conditional = value["conditional_probability_range"]
    joint = value["joint_probability_range"]
    if premise["minimum"] > premise["maximum"] or conditional["minimum"] > conditional["maximum"]:
        raise ContractViolation("ProbabilityAssessment", ["probability ranges must be ordered"])
    expected_min = premise["minimum"] * conditional["minimum"]
    expected_max = premise["maximum"] * conditional["maximum"]
    if abs(joint["minimum"] - expected_min) > 1e-12 or abs(joint["maximum"] - expected_max) > 1e-12:
        raise ContractViolation("ProbabilityAssessment", ["joint interval must equal interval product"])
    expected_scalar = value["premise_probability"] * value["conditional_probability"]
    if abs(value["joint_probability"] - expected_scalar) > 1e-12:
        raise ContractViolation("ProbabilityAssessment", ["joint scalar must equal scalar product"])
    for scalar_name, range_name in (
        ("premise_probability", "premise_probability_range"),
        ("conditional_probability", "conditional_probability_range"),
        ("joint_probability", "joint_probability_range"),
    ):
        scalar = value[scalar_name]
        interval = value[range_name]
        if not interval["minimum"] <= scalar <= interval["maximum"]:
            raise ContractViolation("ProbabilityAssessment", [f"{scalar_name} must be contained by {range_name}"])
