# API v1 contract

`packages/contracts/schemas/v1/contracts.schema.json` is the sole v1 payload authority. TypeScript is generated from it, Pydantic models are compiled from it at runtime with `extra="forbid"`, and FastAPI OpenAPI is exported deterministically to both `openapi.json` and JSON-compatible `openapi.yaml`. `corepack pnpm verify:generated-contracts` fails on drift in any generated artifact, registry snapshot, acceptance-fixture hash, or OpenAPI snapshot.

## Endpoint inventory

| Method | Path | Request | Success response |
| --- | --- | --- | --- |
| GET | `/health` | — | `HealthResponse` |
| POST | `/v1/sessions` | `CreateSessionRequest` | `SessionOperationResponse` |
| POST | `/v1/challenges` | `CreateChallengeRequest` | `ChallengeOperationResponse` |
| POST | `/v1/challenges/{challenge_id}/messages` | `ChallengeMessageRequest` | `ChallengeOperationResponse` |
| POST | `/v1/challenges/{challenge_id}/evidence` | `EvidenceSubmissionRequest` | `ChallengeOperationResponse` |
| POST | `/v1/challenges/{challenge_id}/confirm` | `ConfirmChallengeRequest` | `ChallengeOperationResponse` |
| POST | `/v1/worldlines` | `WorldlineRequest` | `WorldlineBundleOperationResponse` |
| GET | `/v1/worldlines/{worldline_id}` | — | `Worldline` |
| POST | `/v1/probabilities/assess` | `ProbabilityRequest` | `ProbabilityOperationResponse` |
| GET | `/v1/assets/search` | `query` | `AssetSearchResponse` |
| GET | `/v1/assets/{asset_id}` | — | `AssetRegistryEntry` |
| POST | `/v1/market-data/manifests` | `MarketDataManifestRequest` | `MarketDataManifestOperationResponse` |
| POST | `/v1/market-data/shards` | `MarketDataShardRequest` | `MarketDataShardOperationResponse` |
| GET | `/v1/factor-libraries/{version}/manifest` | — | `FactorLibraryManifest` |
| GET | `/v1/factor-libraries/{version}/shards` | `shard_id` | `FactorLibraryShard` |
| POST | `/v1/client-computations/{computation_id}/attest` | `ClientComputationAttestRequest` | `ClientAttestationOperationResponse` |
| POST | `/v1/operations/{operation_id}/cancel` | `CancelOperationRequest` | `OperationResponse` |
| POST | `/v1/operations/{operation_id}/resume` | `ResumeOperationRequest` | `OperationResponse` |
| GET | `/v1/operations/{operation_id}/events` | `Last-Event-ID` | `text/event-stream` / `SseEvent` |
| GET | `/v1/methodology` | — | `MethodologyResponse` |

Every operation has an explicit `operationId`. Every route documents the canonical `ErrorResponse` for 400, 403, 404, 409, 422 and 500. Request and response objects reject unknown properties; nested entities do too.

## Idempotency and operation control

Every POST requires `Idempotency-Key` (8–128 characters). Its scope is endpoint plus key.

- First use hashes the canonical request body, creates a deterministic `operation_id`, `cancel_token`, and `resume_token`, and returns `Idempotency-Replayed: false`.
- Reusing the key with the same body returns the exact stored response and IDs with `Idempotency-Replayed: true`, even if durable research state has since advanced.
- Reusing the key with a different body returns `409 idempotency_conflict`.
- Raw idempotency keys are not returned or persisted in entities; only SHA-256 digests are exposed.
- Cancel and resume are themselves idempotent commands. A cancel racing a completed operation is a successful no-op. A cancelled running operation retains its event history and can resume from its checkpoint token.

Research entity documents and operation/idempotency state are persisted through configured repositories: production/public-hardening requires PostgreSQL, while development and tests may explicitly use deterministic in-memory adapters. Operation records, typed receipts/errors, worker lease metadata, session associations and append-only SSE events are covered by the PostgreSQL operation store; Redis distributed abuse controls, provider receipt recovery and live runtime execution remain deployment work. The wire semantics and tests are already fixed here.

## SSE event and replay semantics

The event union is `operation.started | operation.progress | heartbeat | operation.error | operation.complete`. Heartbeats are typed JSON events, not unstructured comments. Every event contains `schema_version`, `operation_id`, monotonic `sequence`, and `emitted_at`.

Event IDs use `operation_id:sequence`. On reconnect, a client sends the last fully processed ID in `Last-Event-ID`; the server returns only events whose sequence is greater. A cursor for another operation, a malformed sequence, or a sequence outside the retained replay window returns `400 invalid_last_event_id`. A terminal `operation.complete` carries a result reference and content hash; `operation.error` carries the canonical error envelope and retryability. The deterministic mock retains the complete process-local log.

## Probability, manifests and attestation

`POST /v1/worldlines` accepts only a confirmed `challenge_id` and horizon; its premise is derived from the retained challenge thesis. It returns exactly weak, main and strong branches, their event/reveal schedule, asset impacts, and three attached assessments. `POST /v1/probabilities/assess` accepts only a generated `worldline_id`.

`ProbabilityAssessment` always carries premise, conditional and joint scalar values plus all three intervals. The API verifies scalar multiplication, interval multiplication, interval ordering and scalar containment. Branches share a premise estimate, conditional scalar mass sums to one, and conditional intervals must jointly admit one. Mock output is `model_estimate_unvalidated` and uses coarse values.

Market and factor payloads are manifest/shard pairs with version, content hash, mock integrity key ID, field/admission metadata and explicit data-license status. Market mock data is always `historical_only`. Final portfolio computation remains client-side; the server only records hash-based `ClientComputationAttestation` data.
