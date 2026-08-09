"""Deterministic, offline provider implementations for the Phase 1 contract gate."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .models import (
    AssetRegistryEntry,
    ChallengeRecord,
    ClientComputationAttestation,
    DataLicense,
    FactorAdmission,
    FactorDefinition,
    FactorLibraryManifest,
    FactorLibraryShard,
    Integrity,
    MarketDataManifest,
    MarketDataRow,
    MarketDataShard,
    ProbabilityAssessment,
    ProbabilityRange,
    RevealEvent,
    RubricScore,
    ShardDescriptor,
    SourceRef,
    ThesisDocument,
    Worldline,
    WorldlineBundle,
    WorldlineEdge,
    WorldlineNode,
)
from .services import content_hash, deterministic_id, validate_payload, validate_probability_semantics


class FixedClock:
    """An injected clock makes every mock response and fixture byte-stable."""

    def now(self) -> datetime:
        return datetime(2026, 8, 10, 0, 0, tzinfo=UTC)

    def today(self) -> date:
        return self.now().date()


def _model_ref(ref_id: str, label: str) -> SourceRef:
    return SourceRef(ref_id=ref_id, source_type="model_inference", label=label)


def _metadata(payload: dict[str, Any], refs: tuple[SourceRef, ...], clock: FixedClock) -> dict[str, Any]:
    source_refs = [ref.model_dump(mode="json", exclude_none=True) for ref in refs]
    hash_input = {"schema_version": "v1", "source_refs": source_refs, **payload}
    return {
        "schema_version": "v1",
        "created_at": clock.now(),
        "source_refs": refs,
        "content_hash": content_hash(hash_input),
    }


RUBRIC_CRITERIA = (
    "premise_clarity", "time_boundary", "trigger_observability", "causal_chain",
    "counterfactuals", "evidence_quality", "asset_transmission", "already_priced",
    "falsifiability", "user_confirmation",
)


class ResearchProvider(ABC):
    @abstractmethod
    def challenge(self, **kwargs: Any) -> ChallengeRecord: ...

    @abstractmethod
    def generate_worldlines(self, **kwargs: Any) -> WorldlineBundle: ...

    @abstractmethod
    def assess_probability(self, **kwargs: Any) -> ProbabilityAssessment: ...


class MockResearchProvider(ResearchProvider):
    """Safe deterministic fixtures only; no current-market or factual forecast claim."""

    def __init__(self, clock: FixedClock | None = None) -> None:
        self.clock = clock or FixedClock()

    def thesis(
        self,
        *,
        operation_id: str,
        session_id: str,
        raw_text: str,
        capital: float,
        base_currency: str,
        private_evidence: bool,
    ) -> ThesisDocument:
        payload = {
            "thesis_id": deterministic_id("thesis", operation_id),
            "session_id": session_id,
            "raw_text": raw_text,
            "normalized_premise": " ".join(raw_text.split()),
            "horizon": "User horizon requires confirmation during challenge.",
            "trigger_events": ["A dated observable trigger is supplied."],
            "claimed_asset_impacts": [],
            "capital": capital,
            "base_currency": base_currency,
            "user_private_evidence_claims": private_evidence,
            "status": "CHALLENGING",
        }
        refs = (_model_ref("mock:thesis-normalizer", "Deterministic thesis normalizer"),)
        result = ThesisDocument(**_metadata(payload, refs, self.clock), **payload)
        validate_payload(result, "ThesisDocument")
        return result

    def challenge(
        self,
        *,
        operation_id: str,
        thesis_id: str,
        challenge_id: str,
        claim: str,
        turn: int,
        status: str,
        confirmation: str = "pending",
        reservation_note: str | None = None,
        private_evidence_declared: bool = False,
    ) -> ChallengeRecord:
        refs = (_model_ref("mock:challenge", "Deterministic adversarial review"),)
        score = 3 if status == "CONFIRMED" else 2
        scores = tuple(
            RubricScore(criterion=criterion, score=score, rationale="Mock rubric remains evidence-conservative and deterministic.")
            for criterion in RUBRIC_CRITERIA
        )
        payload: dict[str, Any] = {
            "turn_id": deterministic_id("turn", f"{challenge_id}:{turn}"),
            "challenge_id": challenge_id,
            "thesis_id": thesis_id,
            "claims": [claim],
            "objections": ["The proposed causal link may have a competing explanation or already be priced."],
            "missing_evidence": [] if status == "CONFIRMED" else ["Add a dated, independently verifiable base-rate source."],
            "questions": ["What observable result would falsify this premise?"],
            "rubric_scores": [item.model_dump(mode="json", exclude_none=True) for item in scores],
            "unresolved_issues": ["No public base-rate evidence has been independently verified in mock mode."],
            "user_confirmation": confirmation,
            "private_evidence_declared": private_evidence_declared,
            "status": status,
        }
        if reservation_note is not None:
            payload["reservation_note"] = reservation_note
        result = ChallengeRecord(**_metadata(payload, refs, self.clock), **payload)
        validate_payload(result, "ChallengeRecord")
        return result

    def generate_worldlines(
        self,
        *,
        operation_id: str,
        challenge_id: str,
        premise: str,
        horizon_end: date,
    ) -> WorldlineBundle:
        refs = (_model_ref("mock:worldline", "Deterministic weak/main/strong worldline generator"),)
        bundle_id = deterministic_id("bundle", operation_id)
        worldlines: list[Worldline] = []
        for index, branch in enumerate(("weak", "main", "strong"), start=1):
            worldline_id = deterministic_id("worldline", f"{bundle_id}:{branch}")
            first = f"{branch}:premise"
            second = f"{branch}:reveal"
            nodes = (
                WorldlineNode(
                    node_id=first,
                    time_window="baseline",
                    event="The user-confirmed premise is recorded.",
                    observable_indicators=("Confirmed challenge record",),
                    causal_parents=(),
                    state_changes=("Scenario tracking starts",),
                    confidence="low",
                ),
                WorldlineNode(
                    node_id=second,
                    time_window="horizon",
                    event=f"The {branch} transmission path is evaluated against visible evidence.",
                    observable_indicators=("Dated public evidence", "Invalidation checks"),
                    causal_parents=(first,),
                    state_changes=("Scenario is retained, weakened, strengthened, or invalidated",),
                    confidence="low",
                ),
            )
            payload = {
                "worldline_id": worldline_id,
                "bundle_id": bundle_id,
                "branch_kind": branch,
                "title": f"Deterministic {branch} research path",
                "premise": premise,
                "baseline_date": self.clock.today(),
                "horizon_end": horizon_end,
                "nodes": [node.model_dump(mode="json", exclude_none=True) for node in nodes],
                "edges": [WorldlineEdge(from_node_id=first, to_node_id=second).model_dump(mode="json", exclude_none=True)],
                "asset_anchors": ["IDX:SPX", "YIELD:US10Y"],
                "event_reveal_schedule": [RevealEvent(event_id=second, event_date=horizon_end, reveal_date=horizon_end, rule="observed").model_dump(mode="json", exclude_none=True)],
                "invalidation_conditions": ["A verified observation contradicts the stated premise."],
                "provenance": "deterministic_mock; no live market claim",
                "status": "WORLDLINE_READY",
            }
            worldline = Worldline(**_metadata(payload, refs, self.clock), **payload)
            validate_payload(worldline, "Worldline")
            worldlines.append(worldline)
        bundle_payload = {
            "bundle_id": bundle_id,
            "challenge_id": challenge_id,
            "worldlines": [item.model_dump(mode="json", exclude_none=True) for item in worldlines],
        }
        result = WorldlineBundle(**_metadata(bundle_payload, refs, self.clock), **bundle_payload)
        validate_payload(result, "WorldlineBundle")
        return result

    def assess_probability(
        self,
        *,
        operation_id: str,
        worldline_id: str,
        premise: str,
        conditional_path: str,
    ) -> ProbabilityAssessment:
        refs = (_model_ref("mock:probability", "Deterministic unvalidated probability estimate"),)
        premise_range = ProbabilityRange(minimum=0.15, maximum=0.25)
        conditional_range = ProbabilityRange(minimum=0.40, maximum=0.60)
        joint_range = ProbabilityRange(minimum=0.06, maximum=0.15)
        payload = {
            "assessment_id": deterministic_id("probability", operation_id),
            "worldline_id": worldline_id,
            "premise_probability": 0.20,
            "premise_probability_range": premise_range.model_dump(mode="json", exclude_none=True),
            "conditional_probability": 0.50,
            "conditional_probability_range": conditional_range.model_dump(mode="json", exclude_none=True),
            "joint_probability": 0.10,
            "joint_probability_range": joint_range.model_dump(mode="json", exclude_none=True),
            "confidence": "low",
            "base_rate_refs": [],
            "adjustment_reasons": [f"Mock premise only: {premise}", f"Mock conditional path only: {conditional_path}"],
            "calibration_status": "model_estimate_unvalidated",
        }
        result = ProbabilityAssessment(**_metadata(payload, refs, self.clock), **payload)
        validate_payload(result, "ProbabilityAssessment")
        validate_probability_semantics(result)
        return result


class MockDataProvider:
    def __init__(self, clock: FixedClock | None = None) -> None:
        self.clock = clock or FixedClock()
        self.refs = (SourceRef(ref_id="mock:market", source_type="market_data", label="Synthetic deterministic market panel"),)
        self.license = DataLicense(
            license_id="mock-development-v1",
            redistribution="allowed",
            usage="development_mock",
            attribution="Synthetic data; not current market data.",
        )

    def rows(self, asset_ids: tuple[str, ...], assets: dict[str, AssetRegistryEntry], as_of: datetime) -> tuple[MarketDataRow, ...]:
        rows: list[MarketDataRow] = []
        for index, asset_id in enumerate(asset_ids, start=1):
            base = float(100 + index)
            rows.append(MarketDataRow(
                datetime=as_of,
                asset_id=asset_id,
                open=base,
                high=base + 1,
                low=base - 1,
                close=base + 0.25,
                volume=float(1000 * index),
                amount=float(1000 * index) * (base + 0.25),
                currency=assets[asset_id].currency,
                adjusted=True,
                provider="deterministic_mock",
                as_of=as_of,
            ))
        return tuple(rows)

    def manifest(
        self,
        *,
        operation_id: str,
        asset_ids: tuple[str, ...],
        field_set: tuple[str, ...],
        from_at: datetime,
        to_at: datetime,
        assets: dict[str, AssetRegistryEntry],
    ) -> MarketDataManifest:
        manifest_id = deterministic_id("market-manifest", operation_id)
        shard_id = deterministic_id("market-shard", manifest_id)
        rows = self.rows(asset_ids, assets, to_at)
        row_hash = content_hash([row.model_dump(mode="json", exclude_none=True) for row in rows])
        descriptor = ShardDescriptor(shard_id=shard_id, content_hash=row_hash, row_count=len(rows), **{"from": from_at}, to=to_at)
        integrity = Integrity(algorithm="sha256", digest=row_hash, key_id="mock-not-production-trust")
        payload = {
            "manifest_id": manifest_id,
            "market_data_version": "v1.0.0",
            "registry_version": "v1.0.0",
            "provider": "deterministic_mock",
            "as_of": self.clock.now(),
            "freshness": "historical_only",
            "field_set": list(field_set),
            "asset_ids": list(asset_ids),
            "data_license": self.license.model_dump(mode="json", exclude_none=True),
            "shards": [descriptor.model_dump(mode="json", exclude_none=True)],
            "integrity": integrity.model_dump(mode="json", exclude_none=True),
        }
        result = MarketDataManifest(**_metadata(payload, self.refs, self.clock), **payload)
        validate_payload(result, "MarketDataManifest")
        return result

    def shard(self, manifest: MarketDataManifest, assets: dict[str, AssetRegistryEntry]) -> MarketDataShard:
        descriptor = manifest.shards[0]
        rows = self.rows(manifest.asset_ids, assets, descriptor.to)
        integrity = Integrity(algorithm="sha256", digest=descriptor.content_hash, key_id="mock-not-production-trust")
        payload = {
            "manifest_id": manifest.manifest_id,
            "shard_id": descriptor.shard_id,
            "rows": [row.model_dump(mode="json", exclude_none=True) for row in rows],
            "integrity": integrity.model_dump(mode="json", exclude_none=True),
        }
        result = MarketDataShard(**_metadata(payload, self.refs, self.clock), **payload)
        validate_payload(result, "MarketDataShard")
        return result


class MockFactorProvider:
    def __init__(self, clock: FixedClock | None = None) -> None:
        self.clock = clock or FixedClock()
        self.refs = (SourceRef(ref_id="mock:factors", source_type="factor_library", label="Synthetic whitelist-token factor library"),)
        self.factors = (
            FactorDefinition(
                factor_id="factor:momentum-20",
                name="Mock 20-day close difference",
                expression_tokens=(
                    {"kind": "field", "field": "close"},
                    {"kind": "constant", "value": 20.0},
                    {"kind": "operator", "operator": "difference", "arity": 2},
                ),
                ic_mean=0.012,
                icir=0.11,
                forecast_horizon=10,
                valid=True,
            ),
        )

    def manifest(self, version: str) -> FactorLibraryManifest:
        if version != "v1.0.0":
            raise KeyError(version)
        factor_payload = [factor.model_dump(mode="json", exclude_none=True) for factor in self.factors]
        factor_hash = content_hash(factor_payload)
        descriptor = ShardDescriptor(
            shard_id="factor-shard:core",
            content_hash=factor_hash,
            row_count=len(self.factors),
            **{"from": self.clock.now()},
            to=self.clock.now(),
        )
        payload = {
            "library_version": version,
            "metric_version": "paper_ic_v5",
            "dsl_version": "postfix-whitelist-v1",
            "as_of": self.clock.now(),
            "admission": FactorAdmission(ic_threshold=0.007, icir_threshold=0.084, correlation_threshold=0.5, active_top_k=10).model_dump(mode="json", exclude_none=True),
            "factor_ids": [factor.factor_id for factor in self.factors],
            "shards": [descriptor.model_dump(mode="json", exclude_none=True)],
            "integrity": Integrity(algorithm="sha256", digest=factor_hash, key_id="mock-not-production-trust").model_dump(mode="json", exclude_none=True),
        }
        result = FactorLibraryManifest(**_metadata(payload, self.refs, self.clock), **payload)
        validate_payload(result, "FactorLibraryManifest")
        return result

    def shard(self, version: str, shard_id: str) -> FactorLibraryShard:
        manifest = self.manifest(version)
        descriptor = manifest.shards[0]
        if shard_id != descriptor.shard_id:
            raise KeyError(shard_id)
        payload = {
            "library_version": version,
            "shard_id": shard_id,
            "factors": [factor.model_dump(mode="json", exclude_none=True) for factor in self.factors],
            "integrity": manifest.integrity.model_dump(mode="json", exclude_none=True),
        }
        result = FactorLibraryShard(**_metadata(payload, self.refs, self.clock), **payload)
        validate_payload(result, "FactorLibraryShard")
        return result


def build_attestation(
    *, operation_id: str, computation_id: str, request: Any, clock: FixedClock
) -> ClientComputationAttestation:
    refs = (SourceRef(ref_id="client:attestation", source_type="user_submitted", label="Client-computed result hashes"),)
    accepted = request.engine_version.startswith("v1")
    payload = {
        "computation_id": computation_id,
        "operation_id": request.operation_id,
        "engine_version": request.engine_version,
        "worldline_hash": request.worldline_hash,
        "market_manifest_hash": request.market_manifest_hash,
        "factor_manifest_hash": request.factor_manifest_hash,
        "input_hash": request.input_hash,
        "output_hash": request.output_hash,
        "completed_at": request.completed_at,
        "accepted": accepted,
        "rejection_reasons": [] if accepted else ["Unsupported client engine major version."],
    }
    result = ClientComputationAttestation(**_metadata(payload, refs, clock), **payload)
    validate_payload(result, "ClientComputationAttestation")
    return result
