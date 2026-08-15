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


def validate_worldline_bundle_semantics(payload: BaseModel) -> None:
    """Enforce the temporal and graph rules JSON Schema cannot express."""
    value = payload.model_dump(mode="json", exclude_none=True)
    for worldline in value["worldlines"]:
        baseline, horizon = worldline["baseline_date"], worldline["horizon_end"]
        if baseline > horizon:
            raise ContractViolation("Worldline", ["baseline_date must not be after horizon_end"])
        nodes = {node["node_id"]: node for node in worldline["nodes"]}
        if len(nodes) != len(worldline["nodes"]):
            raise ContractViolation("Worldline", ["node_id values must be unique"])
        children: dict[str, list[str]] = {node_id: [] for node_id in nodes}
        for edge in worldline["edges"]:
            source, target = edge["from_node_id"], edge["to_node_id"]
            if source not in nodes or target not in nodes:
                raise ContractViolation("Worldline", ["edges must refer only to declared nodes"])
            if nodes[source]["event_date"] > nodes[target]["event_date"]:
                raise ContractViolation("Worldline", ["edge event dates must be monotonic"])
            children[source].append(target)
        visiting: set[str] = set()
        visited: set[str] = set()
        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ContractViolation("Worldline", ["worldline graph must be acyclic"])
            if node_id in visited:
                return
            visiting.add(node_id)
            for child in children[node_id]:
                visit(child)
            visiting.remove(node_id)
            visited.add(node_id)
        for node_id, node in nodes.items():
            if not baseline <= node["event_date"] <= horizon:
                raise ContractViolation("Worldline", ["node event_date must be within baseline/horizon"])
            if node["data_as_of"] > baseline or node["data_as_of"] > node["event_date"]:
                raise ContractViolation("Worldline", ["node data_as_of must not reference future data"])
            visit(node_id)
        scheduled = {event["event_id"] for event in worldline["event_reveal_schedule"]}
        if not scheduled.issubset(nodes):
            raise ContractViolation("Worldline", ["reveal schedule must refer to declared nodes"])
        for event in worldline["event_reveal_schedule"]:
            if not baseline <= event["event_date"] <= horizon or not baseline <= event["reveal_date"] <= horizon:
                raise ContractViolation("Worldline", ["event and reveal dates must remain within baseline/horizon"])
            if event["rule"] in {"scheduled", "observed"} and event["reveal_date"] < event["event_date"]:
                raise ContractViolation("Worldline", ["scheduled/observed reveal cannot precede event"])


def validate_branch_probability_consistency(assessments: Any, worldlines: Any, tolerance: float = 1e-12) -> None:
    """Branches form an exhaustive conditional partition, not three unrelated guesses."""
    branches = {worldline.branch_kind: worldline.worldline_id for worldline in worldlines}
    if set(branches) != {"weak", "main", "strong"} or len(assessments) != 3:
        raise ContractViolation("WorldlineBundle", ["bundle must contain weak, main, and strong branches exactly once"])
    by_worldline = {assessment.worldline_id: assessment for assessment in assessments}
    if set(by_worldline) != set(branches.values()):
        raise ContractViolation("WorldlineBundle", ["each branch requires exactly one probability assessment"])
    first = next(iter(by_worldline.values()))
    for assessment in by_worldline.values():
        validate_probability_semantics(assessment)
        if assessment.premise_probability != first.premise_probability or assessment.premise_probability_range != first.premise_probability_range:
            raise ContractViolation("WorldlineBundle", ["all adjacent branches must share the same premise probability"])
    conditional_total = sum(item.conditional_probability for item in by_worldline.values())
    if abs(conditional_total - 1.0) > tolerance:
        raise ContractViolation("WorldlineBundle", ["conditional branch probabilities must sum to one"])
    minimum_total = sum(item.conditional_probability_range.minimum for item in by_worldline.values())
    maximum_total = sum(item.conditional_probability_range.maximum for item in by_worldline.values())
    if minimum_total > 1.0 + tolerance or maximum_total < 1.0 - tolerance:
        raise ContractViolation("WorldlineBundle", ["conditional branch intervals must be jointly feasible"])
