"""Provider-neutral Phase 4 generation boundary.

Only typed, canonical-contract objects cross this module.  A live adapter may
be added later, but unrecognised or semantically invalid output is rejected
before it can be persisted or streamed to a client.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

from .models import AssetImpact, ChallengeRecord, ProbabilityAssessment, ProbabilityRange, ThesisDocument, Worldline, WorldlineBundle, WorldlineEdge, WorldlineNode, RevealEvent
from .providers import FixedClock, _metadata, _model_ref
from .services import deterministic_id, validate_branch_probability_consistency, validate_payload, validate_probability_semantics, validate_worldline_bundle_semantics


@dataclass(frozen=True)
class WorldlineGenerationInput:
    operation_id: str
    challenge: ChallengeRecord
    thesis: ThesisDocument
    horizon_end: date


@dataclass(frozen=True)
class ProbabilityAssessmentInput:
    operation_id: str
    worldline: Worldline


class WorldlineGenerator(ABC):
    """Provider-neutral structured worldline interface."""

    name: str

    @abstractmethod
    def generate(self, request: WorldlineGenerationInput) -> WorldlineBundle: ...


class ProbabilityAssessor(ABC):
    """Provider-neutral structured probability interface."""

    name: str

    @abstractmethod
    def assess(self, request: ProbabilityAssessmentInput) -> ProbabilityAssessment: ...


class DeterministicMockProbabilityAssessor(ProbabilityAssessor):
    name = "deterministic_mock"
    _conditional = {"weak": (0.20, (0.15, 0.25)), "main": (0.50, (0.40, 0.60)), "strong": (0.30, (0.25, 0.35))}

    def __init__(self, clock: FixedClock | None = None) -> None:
        self.clock = clock or FixedClock()

    def assess(self, request: ProbabilityAssessmentInput) -> ProbabilityAssessment:
        worldline = request.worldline
        conditional, conditional_interval = self._conditional[worldline.branch_kind]
        premise = ProbabilityRange(minimum=0.15, maximum=0.25)
        path = ProbabilityRange(minimum=conditional_interval[0], maximum=conditional_interval[1])
        joint = ProbabilityRange(minimum=premise.minimum * path.minimum, maximum=premise.maximum * path.maximum)
        refs = (_model_ref("mock:probability", "Deterministic interval-first uncalibrated probability estimate"),)
        payload = {
            "assessment_id": deterministic_id("probability", f"{request.operation_id}:{worldline.worldline_id}"),
            "worldline_id": worldline.worldline_id,
            "premise_probability": 0.20,
            "premise_probability_range": premise.model_dump(mode="json", exclude_none=True),
            "conditional_probability": conditional,
            "conditional_probability_range": path.model_dump(mode="json", exclude_none=True),
            "joint_probability": 0.20 * conditional,
            "joint_probability_range": joint.model_dump(mode="json", exclude_none=True),
            "confidence": "low",
            "base_rate_refs": [],
            "adjustment_reasons": ["Deterministic mock uses coarse intervals only.", f"Conditional mass is assigned to the {worldline.branch_kind} branch."],
            "calibration_status": "model_estimate_unvalidated",
        }
        result = ProbabilityAssessment(**_metadata(payload, refs, self.clock), **payload)
        validate_payload(result, "ProbabilityAssessment")
        validate_probability_semantics(result)
        return result


class DeterministicMockWorldlineGenerator(WorldlineGenerator):
    name = "deterministic_mock"

    def __init__(self, assessor: ProbabilityAssessor, clock: FixedClock | None = None) -> None:
        self.clock = clock or FixedClock()
        self.assessor = assessor

    def generate(self, request: WorldlineGenerationInput) -> WorldlineBundle:
        if request.challenge.status != "CONFIRMED":
            raise ValueError("Worldline generation requires a confirmed challenge")
        if request.horizon_end < self.clock.today():
            raise ValueError("Worldline horizon must not precede the baseline")
        refs = (_model_ref("mock:worldline", "Deterministic weak/main/strong worldline generator"),)
        bundle_id = deterministic_id("bundle", request.operation_id)
        premise = request.thesis.normalized_premise
        branches = (("weak", "Transmission weakens despite premise confirmation", "negative"), ("main", "Primary transmission follows the confirmed premise", "mixed"), ("strong", "Second-order effects strengthen transmission", "positive"))
        worldlines: list[Worldline] = []
        for branch, outcome, direction in branches:
            worldline_id = deterministic_id("worldline", f"{bundle_id}:{branch}")
            start_id, outcome_id = f"{branch}:premise", f"{branch}:outcome"
            nodes = (
                WorldlineNode(node_id=start_id, time_window="baseline", event_date=self.clock.today(), data_as_of=self.clock.today(), event="Confirmed challenge premise enters scenario tracking.", observable_indicators=("Confirmed challenge record",), causal_parents=(), state_changes=("Tracking begins",), confidence="low"),
                WorldlineNode(node_id=outcome_id, time_window="horizon", event_date=request.horizon_end, data_as_of=self.clock.today(), event=outcome + ".", observable_indicators=("Dated observable indicator", "Recorded invalidation check"), causal_parents=(start_id,), state_changes=("Scenario outcome is recorded",), confidence="low"),
            )
            impact_payload = {"impact_id": deterministic_id("impact", f"{worldline_id}:broad-market"), "economic_exposure_id": "IDX:SPX", "instrument_id": "ETF:SPY", "direction": direction, "severity": "medium", "start_window": "baseline", "peak_window": "horizon", "decay": "review at horizon", "first_order_reason": "Deterministic placeholder for visible asset-transmission review.", "second_order_reason": "Second-order effects are explicitly retained for the strong branch.", "already_priced_score": 2, "evidence_refs": []}
            impact = AssetImpact(**_metadata(impact_payload, refs, self.clock), **impact_payload)
            payload = {"worldline_id": worldline_id, "bundle_id": bundle_id, "branch_kind": branch, "title": f"Deterministic {branch} path", "premise": premise, "baseline_date": self.clock.today(), "horizon_end": request.horizon_end, "nodes": [node.model_dump(mode="json", exclude_none=True) for node in nodes], "edges": [WorldlineEdge(from_node_id=start_id, to_node_id=outcome_id).model_dump(mode="json", exclude_none=True)], "asset_anchors": ["IDX:SPX", "YIELD:US10Y"], "asset_impacts": [impact.model_dump(mode="json", exclude_none=True)], "event_reveal_schedule": [RevealEvent(event_id=start_id, event_date=self.clock.today(), reveal_date=self.clock.today(), rule="observed").model_dump(mode="json", exclude_none=True), RevealEvent(event_id=outcome_id, event_date=request.horizon_end, reveal_date=request.horizon_end, rule="observed").model_dump(mode="json", exclude_none=True)], "invalidation_conditions": ["A dated observable result contradicts the confirmed premise."], "provenance": "deterministic_mock; no live market claim", "status": "WORLDLINE_READY"}
            worldline = Worldline(**_metadata(payload, refs, self.clock), **payload)
            validate_payload(worldline, "Worldline")
            worldlines.append(worldline)
        assessments = [self.assessor.assess(ProbabilityAssessmentInput(request.operation_id, worldline)) for worldline in worldlines]
        bundle_payload = {"bundle_id": bundle_id, "challenge_id": request.challenge.challenge_id, "worldlines": [item.model_dump(mode="json", exclude_none=True) for item in worldlines], "probability_assessments": [item.model_dump(mode="json", exclude_none=True) for item in assessments]}
        result = WorldlineBundle(**_metadata(bundle_payload, refs, self.clock), **bundle_payload)
        validate_payload(result, "WorldlineBundle")
        validate_worldline_bundle_semantics(result)
        validate_branch_probability_consistency(result.probability_assessments, result.worldlines)
        return result
