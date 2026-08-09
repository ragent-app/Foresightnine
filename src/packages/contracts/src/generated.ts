/**
 * GENERATED FILE — DO NOT EDIT.
 * Source: schemas/v1/contracts.schema.json
 */
export const CONTRACT_VERSION = "v1" as const;
export const CONTRACT_SCHEMA_SHA256 = "deb077db03cd740cf9d7e2d618ad5fc9c5610bdd4ac9e6fe0f1f8c26fa48d01d" as const;

export type SchemaVersion = "v1";

export type Identifier = string;

export type Sha256 = string;

export type Currency = string;

export type ResearchStatus = "DRAFT" | "CHALLENGING" | "EVIDENCE_REQUIRED" | "CONFIRMED" | "WORLDLINE_GENERATING" | "WORLDLINE_READY" | "DATA_PREPARING" | "CLIENT_COMPUTING" | "PORTFOLIO_READY" | "EXPORTED";

export interface SourceRef {
  readonly "ref_id": Identifier;
  readonly "source_type": "public" | "user_submitted" | "model_inference" | "market_data" | "registry" | "factor_library";
  readonly "label": string;
  readonly "url"?: string;
}

export interface ContractMetadata {
  readonly "schema_version": SchemaVersion;
  readonly "created_at": string;
  readonly "source_refs": readonly (SourceRef)[];
  readonly "content_hash": Sha256;
}

export interface ProbabilityRange {
  readonly "minimum": number;
  readonly "maximum": number;
}

export interface ResearchSession {
  readonly "schema_version": SchemaVersion;
  readonly "created_at": string;
  readonly "source_refs": readonly (SourceRef)[];
  readonly "content_hash": Sha256;
  readonly "session_id": Identifier;
  readonly "status": ResearchStatus;
  readonly "expires_at": string;
}

export interface ThesisDocument {
  readonly "schema_version": SchemaVersion;
  readonly "created_at": string;
  readonly "source_refs": readonly (SourceRef)[];
  readonly "content_hash": Sha256;
  readonly "thesis_id": Identifier;
  readonly "session_id": Identifier;
  readonly "raw_text": string;
  readonly "normalized_premise": string;
  readonly "horizon": string;
  readonly "trigger_events": readonly (string)[];
  readonly "claimed_asset_impacts": readonly (string)[];
  readonly "capital": number;
  readonly "base_currency": Currency;
  readonly "user_private_evidence_claims": boolean;
  readonly "status": ResearchStatus;
}

export interface EvidenceItem {
  readonly "schema_version": SchemaVersion;
  readonly "created_at": string;
  readonly "source_refs": readonly (SourceRef)[];
  readonly "content_hash": Sha256;
  readonly "evidence_id": Identifier;
  readonly "challenge_id": Identifier;
  readonly "source_type": "primary" | "reliable_secondary" | "commentary" | "user_private" | "model_inference";
  readonly "url"?: string;
  readonly "title": string;
  readonly "publisher"?: string;
  readonly "published_at"?: string;
  readonly "retrieved_at": string;
  readonly "excerpt": string;
  readonly "user_note"?: string;
  readonly "grade": "A" | "B" | "C" | "D" | "E";
  readonly "supports_or_challenges": "supports" | "challenges" | "context";
  readonly "independently_reviewable": boolean;
}

export interface RubricScore {
  readonly "criterion": "premise_clarity" | "time_boundary" | "trigger_observability" | "causal_chain" | "counterfactuals" | "evidence_quality" | "asset_transmission" | "already_priced" | "falsifiability" | "user_confirmation";
  readonly "score": 0 | 1 | 2 | 3 | 4;
  readonly "rationale": string;
}

export interface ChallengeRecord {
  readonly "schema_version": SchemaVersion;
  readonly "created_at": string;
  readonly "source_refs": readonly (SourceRef)[];
  readonly "content_hash": Sha256;
  readonly "turn_id": Identifier;
  readonly "challenge_id": Identifier;
  readonly "thesis_id": Identifier;
  readonly "claims": readonly (string)[];
  readonly "objections": readonly (string)[];
  readonly "missing_evidence": readonly (string)[];
  readonly "questions": readonly (string)[];
  readonly "rubric_scores": readonly (RubricScore)[];
  readonly "unresolved_issues": readonly (string)[];
  readonly "user_confirmation": "pending" | "confirmed" | "confirmed_with_reservations";
  readonly "reservation_note"?: string;
  readonly "private_evidence_declared": boolean;
  readonly "status": "CHALLENGING" | "EVIDENCE_REQUIRED" | "CONFIRMED";
}

export interface WorldlineNode {
  readonly "node_id": Identifier;
  readonly "time_window": string;
  readonly "event": string;
  readonly "observable_indicators": readonly (string)[];
  readonly "causal_parents": readonly (Identifier)[];
  readonly "state_changes": readonly (string)[];
  readonly "confidence": "low" | "medium" | "high";
}

export interface WorldlineEdge {
  readonly "from_node_id": Identifier;
  readonly "to_node_id": Identifier;
}

export interface RevealEvent {
  readonly "event_id": Identifier;
  readonly "event_date": string;
  readonly "reveal_date": string;
  readonly "rule": "scheduled" | "leak" | "observed";
}

export interface Worldline {
  readonly "schema_version": SchemaVersion;
  readonly "created_at": string;
  readonly "source_refs": readonly (SourceRef)[];
  readonly "content_hash": Sha256;
  readonly "worldline_id": Identifier;
  readonly "bundle_id": Identifier;
  readonly "branch_kind": "weak" | "main" | "strong";
  readonly "title": string;
  readonly "premise": string;
  readonly "baseline_date": string;
  readonly "horizon_end": string;
  readonly "nodes": readonly (WorldlineNode)[];
  readonly "edges": readonly (WorldlineEdge)[];
  readonly "asset_anchors": readonly (Identifier)[];
  readonly "event_reveal_schedule": readonly (RevealEvent)[];
  readonly "invalidation_conditions": readonly (string)[];
  readonly "provenance": string;
  readonly "status": "WORLDLINE_READY";
}

export interface WorldlineBundle {
  readonly "schema_version": SchemaVersion;
  readonly "created_at": string;
  readonly "source_refs": readonly (SourceRef)[];
  readonly "content_hash": Sha256;
  readonly "bundle_id": Identifier;
  readonly "challenge_id": Identifier;
  readonly "worldlines": readonly (Worldline)[];
}

export interface ProbabilityAssessment {
  readonly "schema_version": SchemaVersion;
  readonly "created_at": string;
  readonly "source_refs": readonly (SourceRef)[];
  readonly "content_hash": Sha256;
  readonly "assessment_id": Identifier;
  readonly "worldline_id": Identifier;
  readonly "premise_probability": number;
  readonly "premise_probability_range": ProbabilityRange;
  readonly "conditional_probability": number;
  readonly "conditional_probability_range": ProbabilityRange;
  readonly "joint_probability": number;
  readonly "joint_probability_range": ProbabilityRange;
  readonly "confidence": "low" | "medium" | "high";
  readonly "base_rate_refs": readonly (Identifier)[];
  readonly "adjustment_reasons": readonly (string)[];
  readonly "calibration_status": "model_estimate_unvalidated" | "historically_calibrated";
}

export interface AssetImpact {
  readonly "schema_version": SchemaVersion;
  readonly "created_at": string;
  readonly "source_refs": readonly (SourceRef)[];
  readonly "content_hash": Sha256;
  readonly "impact_id": Identifier;
  readonly "economic_exposure_id": Identifier;
  readonly "instrument_id"?: Identifier;
  readonly "direction": "positive" | "negative" | "mixed";
  readonly "severity": "low" | "medium" | "high";
  readonly "start_window": string;
  readonly "peak_window": string;
  readonly "decay": string;
  readonly "first_order_reason": string;
  readonly "second_order_reason"?: string;
  readonly "already_priced_score": 0 | 1 | 2 | 3 | 4;
  readonly "evidence_refs": readonly (Identifier)[];
}

export interface DataLicense {
  readonly "license_id": Identifier;
  readonly "redistribution": "allowed" | "forbidden" | "internal_test_only";
  readonly "usage": "public_display" | "derived_only" | "development_mock";
  readonly "attribution"?: string;
}

export interface DerivativeSpec {
  readonly "expiry": string;
  readonly "strike"?: number;
  readonly "option_type"?: "call" | "put";
  readonly "delta"?: number;
  readonly "implied_volatility"?: number;
  readonly "margin": number;
  readonly "contract_multiplier": number;
  readonly "risk_model_version": string;
}

export interface AssetRegistryEntry {
  readonly "schema_version": SchemaVersion;
  readonly "created_at": string;
  readonly "source_refs": readonly (SourceRef)[];
  readonly "content_hash": Sha256;
  readonly "asset_id": Identifier;
  readonly "economic_exposure_id": Identifier;
  readonly "display_name": string;
  readonly "asset_class": "equity" | "etf" | "fund" | "index" | "sovereign_bond" | "credit_bond" | "yield_curve" | "fx" | "commodity_spot" | "commodity_etf" | "future" | "crypto" | "real_estate" | "reit" | "option" | "derivative" | "cash";
  readonly "subtype": string;
  readonly "exchange": string;
  readonly "currency": Currency;
  readonly "timezone": string;
  readonly "price_type": "adjusted_close" | "close" | "spot" | "yield" | "nav" | "mark";
  readonly "tradable": boolean;
  readonly "proxy_instruments": readonly (Identifier)[];
  readonly "contract_multiplier": number;
  readonly "lot_size": number;
  readonly "fractional_supported": boolean;
  readonly "liquidity_tier": "high" | "medium" | "low" | "restricted";
  readonly "data_license": DataLicense;
  readonly "provider_symbol_map": Readonly<Record<string, string>>;
  readonly "default_allocation_eligible": boolean;
  readonly "ineligibility_reason"?: string;
  readonly "derivative"?: DerivativeSpec;
}

export interface AssetRegistrySnapshot {
  readonly "schema_version": SchemaVersion;
  readonly "created_at": string;
  readonly "source_refs": readonly (SourceRef)[];
  readonly "content_hash": Sha256;
  readonly "registry_version": string;
  readonly "as_of": string;
  readonly "assets": readonly (AssetRegistryEntry)[];
}

export interface Integrity {
  readonly "algorithm": "sha256";
  readonly "digest": Sha256;
  readonly "key_id": Identifier;
}

export interface ShardDescriptor {
  readonly "shard_id": Identifier;
  readonly "content_hash": Sha256;
  readonly "row_count": number;
  readonly "from": string;
  readonly "to": string;
}

export interface MarketDataManifest {
  readonly "schema_version": SchemaVersion;
  readonly "created_at": string;
  readonly "source_refs": readonly (SourceRef)[];
  readonly "content_hash": Sha256;
  readonly "manifest_id": Identifier;
  readonly "market_data_version": string;
  readonly "registry_version": string;
  readonly "provider": string;
  readonly "as_of": string;
  readonly "freshness": "current" | "stale" | "historical_only";
  readonly "field_set": readonly ("open" | "high" | "low" | "close" | "volume" | "amount" | "currency" | "adjusted" | "provider" | "as_of")[];
  readonly "asset_ids": readonly (Identifier)[];
  readonly "data_license": DataLicense;
  readonly "shards": readonly (ShardDescriptor)[];
  readonly "integrity": Integrity;
}

export interface MarketDataRow {
  readonly "datetime": string;
  readonly "asset_id": Identifier;
  readonly "open": number;
  readonly "high": number;
  readonly "low": number;
  readonly "close": number;
  readonly "volume": number;
  readonly "amount": number;
  readonly "currency": Currency;
  readonly "adjusted": boolean;
  readonly "provider": string;
  readonly "as_of": string;
  readonly "expiry"?: string;
  readonly "strike"?: number;
  readonly "option_type"?: "call" | "put";
  readonly "delta"?: number;
  readonly "implied_volatility"?: number;
  readonly "margin"?: number;
  readonly "contract_multiplier"?: number;
}

export interface MarketDataShard {
  readonly "schema_version": SchemaVersion;
  readonly "created_at": string;
  readonly "source_refs": readonly (SourceRef)[];
  readonly "content_hash": Sha256;
  readonly "manifest_id": Identifier;
  readonly "shard_id": Identifier;
  readonly "rows": readonly (MarketDataRow)[];
  readonly "integrity": Integrity;
}

export interface FactorAdmission {
  readonly "ic_threshold": number;
  readonly "icir_threshold": number;
  readonly "correlation_threshold": number;
  readonly "active_top_k": number;
}

export type FactorToken = { readonly "kind": "field"; readonly "field": "open" | "high" | "low" | "close" | "volume" | "amount"; } | { readonly "kind": "constant"; readonly "value": number; } | { readonly "kind": "operator"; readonly "operator": "add" | "subtract" | "multiply" | "divide" | "log" | "difference" | "rank" | "rolling_mean"; readonly "arity": 1 | 2; };

export interface FactorDefinition {
  readonly "factor_id": Identifier;
  readonly "name": string;
  readonly "expression_tokens": readonly (FactorToken)[];
  readonly "ic_mean": number;
  readonly "icir": number;
  readonly "forecast_horizon": number;
  readonly "valid": boolean;
}

export interface FactorLibraryManifest {
  readonly "schema_version": SchemaVersion;
  readonly "created_at": string;
  readonly "source_refs": readonly (SourceRef)[];
  readonly "content_hash": Sha256;
  readonly "library_version": string;
  readonly "metric_version": string;
  readonly "dsl_version": string;
  readonly "as_of": string;
  readonly "admission": FactorAdmission;
  readonly "factor_ids": readonly (Identifier)[];
  readonly "shards": readonly (ShardDescriptor)[];
  readonly "integrity": Integrity;
}

export interface FactorLibraryShard {
  readonly "schema_version": SchemaVersion;
  readonly "created_at": string;
  readonly "source_refs": readonly (SourceRef)[];
  readonly "content_hash": Sha256;
  readonly "library_version": string;
  readonly "shard_id": Identifier;
  readonly "factors": readonly (FactorDefinition)[];
  readonly "integrity": Integrity;
}

export interface ClientComputationAttestation {
  readonly "schema_version": SchemaVersion;
  readonly "created_at": string;
  readonly "source_refs": readonly (SourceRef)[];
  readonly "content_hash": Sha256;
  readonly "computation_id": Identifier;
  readonly "operation_id": Identifier;
  readonly "engine_version": string;
  readonly "worldline_hash": Sha256;
  readonly "market_manifest_hash": Sha256;
  readonly "factor_manifest_hash": Sha256;
  readonly "input_hash": Sha256;
  readonly "output_hash": Sha256;
  readonly "completed_at": string;
  readonly "accepted": boolean;
  readonly "rejection_reasons": readonly (string)[];
}

export interface Operation {
  readonly "schema_version": SchemaVersion;
  readonly "created_at": string;
  readonly "source_refs": readonly (SourceRef)[];
  readonly "content_hash": Sha256;
  readonly "operation_id": Identifier;
  readonly "endpoint": string;
  readonly "status": "pending" | "running" | "completed" | "failed" | "cancelled";
  readonly "updated_at": string;
  readonly "idempotency_key_hash": Sha256;
  readonly "cancel_token": string;
  readonly "resume_token": string;
  readonly "last_event_id": string;
}

export interface ErrorDetail {
  readonly "location": string;
  readonly "message": string;
}

export interface ErrorResponse {
  readonly "schema_version": SchemaVersion;
  readonly "error_code": "bad_request" | "validation_error" | "not_found" | "invalid_state_transition" | "confirmation_requirements_not_met" | "idempotency_conflict" | "invalid_operation_token" | "invalid_last_event_id" | "contract_violation" | "internal_error";
  readonly "message": string;
  readonly "request_id": Identifier;
  readonly "retryable": boolean;
  readonly "details": readonly (ErrorDetail)[];
}

export interface SseEventBase {
  readonly "schema_version": SchemaVersion;
  readonly "operation_id": Identifier;
  readonly "sequence": number;
  readonly "emitted_at": string;
}

export interface OperationStartedEvent {
  readonly "schema_version": SchemaVersion;
  readonly "operation_id": Identifier;
  readonly "sequence": number;
  readonly "emitted_at": string;
  readonly "event": "operation.started";
  readonly "message": string;
}

export interface OperationProgressEvent {
  readonly "schema_version": SchemaVersion;
  readonly "operation_id": Identifier;
  readonly "sequence": number;
  readonly "emitted_at": string;
  readonly "event": "operation.progress";
  readonly "progress": number;
  readonly "message": string;
}

export interface HeartbeatEvent {
  readonly "schema_version": SchemaVersion;
  readonly "operation_id": Identifier;
  readonly "sequence": number;
  readonly "emitted_at": string;
  readonly "event": "heartbeat";
}

export interface OperationErrorEvent {
  readonly "schema_version": SchemaVersion;
  readonly "operation_id": Identifier;
  readonly "sequence": number;
  readonly "emitted_at": string;
  readonly "event": "operation.error";
  readonly "error": ErrorResponse;
  readonly "retryable": boolean;
}

export interface OperationCompleteEvent {
  readonly "schema_version": SchemaVersion;
  readonly "operation_id": Identifier;
  readonly "sequence": number;
  readonly "emitted_at": string;
  readonly "event": "operation.complete";
  readonly "result_ref": Identifier;
  readonly "result_hash": Sha256;
}

export type SseEvent = OperationStartedEvent | OperationProgressEvent | HeartbeatEvent | OperationErrorEvent | OperationCompleteEvent;

export interface CreateSessionRequest {
  readonly "client_locale"?: string;
}

export interface CreateChallengeRequest {
  readonly "session_id": Identifier;
  readonly "thesis": string;
  readonly "capital": number;
  readonly "base_currency": Currency;
  readonly "user_private_evidence_claims": boolean;
}

export interface ChallengeMessageRequest {
  readonly "message": string;
}

export interface EvidenceSubmissionRequest {
  readonly "url"?: string;
  readonly "title": string;
  readonly "publisher"?: string;
  readonly "published_at"?: string;
  readonly "retrieved_at": string;
  readonly "excerpt": string;
  readonly "user_note": string;
  readonly "content_hash": Sha256;
}

export interface ConfirmChallengeRequest {
  readonly "command": "CONFIRM" | "CONFIRM_WITH_RESERVATIONS";
  readonly "unresolved_issues_acknowledged": boolean;
  readonly "reservation_note"?: string;
}

export interface WorldlineRequest {
  readonly "challenge_id": Identifier;
  readonly "premise": string;
  readonly "horizon_end": string;
}

export interface ProbabilityRequest {
  readonly "worldline_id": Identifier;
  readonly "premise": string;
  readonly "conditional_path": string;
}

export interface MarketDataManifestRequest {
  readonly "asset_ids": readonly (Identifier)[];
  readonly "from": string;
  readonly "to": string;
  readonly "field_set": readonly ("open" | "high" | "low" | "close" | "volume" | "amount" | "currency" | "adjusted" | "provider" | "as_of")[];
}

export interface MarketDataShardRequest {
  readonly "manifest_id": Identifier;
  readonly "shard_id": Identifier;
}

export interface ClientComputationAttestRequest {
  readonly "operation_id": Identifier;
  readonly "engine_version": string;
  readonly "worldline_hash": Sha256;
  readonly "market_manifest_hash": Sha256;
  readonly "factor_manifest_hash": Sha256;
  readonly "input_hash": Sha256;
  readonly "output_hash": Sha256;
  readonly "completed_at": string;
}

export interface CancelOperationRequest {
  readonly "cancel_token": string;
}

export interface ResumeOperationRequest {
  readonly "resume_token": string;
}

export interface HealthResponse {
  readonly "status": "ok";
  readonly "provider": "deterministic_mock";
  readonly "schema_version": SchemaVersion;
  readonly "clock": string;
}

export interface SessionOperationResponse {
  readonly "operation": Operation;
  readonly "session": ResearchSession;
}

export interface ChallengeOperationResponse {
  readonly "operation": Operation;
  readonly "challenge": ChallengeRecord;
}

export interface WorldlineBundleOperationResponse {
  readonly "operation": Operation;
  readonly "bundle": WorldlineBundle;
}

export interface ProbabilityOperationResponse {
  readonly "operation": Operation;
  readonly "probability": ProbabilityAssessment;
}

export interface MarketDataManifestOperationResponse {
  readonly "operation": Operation;
  readonly "manifest": MarketDataManifest;
}

export interface MarketDataShardOperationResponse {
  readonly "operation": Operation;
  readonly "shard": MarketDataShard;
}

export interface ClientAttestationOperationResponse {
  readonly "operation": Operation;
  readonly "attestation": ClientComputationAttestation;
}

export interface OperationResponse {
  readonly "operation": Operation;
}

export interface AssetSearchResponse {
  readonly "schema_version": SchemaVersion;
  readonly "items": readonly (AssetRegistryEntry)[];
  readonly "total": number;
}

export interface MethodologyResponse {
  readonly "schema_version": SchemaVersion;
  readonly "methodology_version": "v1";
  readonly "disclosure": string;
  readonly "probability_display": "interval_first";
  readonly "client_computation": true;
}

export type AcceptanceFixtureInput = { readonly "kind": "complete_thesis"; readonly "thesis": string; readonly "capital": number; readonly "base_currency": Currency; } | { readonly "kind": "private_evidence"; readonly "claim": string; readonly "public_evidence_available": false; } | { readonly "kind": "client_evidence_fallback"; readonly "url": string; readonly "excerpt": string; readonly "search_available": false; } | { readonly "kind": "no_valid_factors"; readonly "valid_factor_count": 0; } | { readonly "kind": "small_capital"; readonly "capital": number; readonly "minimum_lot_cost": number; } | { readonly "kind": "stale_data"; readonly "as_of": string; readonly "freshness_threshold_hours": number; } | { readonly "kind": "provider_failure"; readonly "failure": "429" | "502" | "timeout"; readonly "idempotency_key": string; };

export interface AcceptanceFixtureExpected {
  readonly "outcome": "portfolio_ready" | "confirmed_with_reservations" | "evidence_accepted" | "equal_weight_fallback" | "cash_or_proxy_rounding" | "historical_only" | "retry_cancel_resume";
  readonly "markers": readonly (string)[];
}

export interface AcceptanceFixture {
  readonly "schema_version": SchemaVersion;
  readonly "created_at": string;
  readonly "source_refs": readonly (SourceRef)[];
  readonly "content_hash": Sha256;
  readonly "fixture_version": "v1";
  readonly "scenario": "A" | "B" | "C" | "D" | "E" | "F" | "G";
  readonly "title": string;
  readonly "input": AcceptanceFixtureInput;
  readonly "expected": AcceptanceFixtureExpected;
}
