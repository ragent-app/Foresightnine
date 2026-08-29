# Architecture baseline

The v1 boundary is contract-first and provider-neutral. One Draft 2020-12 JSON Schema owns core entities, request/response/error shapes, registry and data/factor manifests, client attestations, operations and SSE events. TypeScript is generated; Pydantic is generated at import; OpenAPI and static registry artifacts are deterministic snapshots protected by drift checks.

The domain package owns pure lifecycle, probability and worldline invariants. The legal durable graph follows the approved plan. `CONFIRM_WITH_RESERVATIONS` is a separate acknowledged command from `EVIDENCE_REQUIRED`, not an undocumented state edge.

The FastAPI adapter defaults to a fixed-clock, deterministic mock LLM provider. Phase 3 also defines provider-neutral LLM/search-evidence boundaries and an opt-in, server-only OpenCode DeepSeek Flash adapter. Structured provider output is parsed as untrusted data and validated again against canonical JSON Schema before return. The API supplies manifests and shards but never computes a user's final portfolio, connects to a broker, or claims current market data.

Recoverability is part of the transport contract: POST idempotency survives state advancement, operation IDs are stable, cancel/resume tokens are explicit, and SSE event IDs support suffix replay via `Last-Event-ID`. Research entities and operation/idempotency state are repository-backed with PostgreSQL in production configuration and explicit in-memory adapters for development/tests. The PostgreSQL operation store persists typed receipts/errors, operation leases, session associations and append-only SSE events; Redis distributed abuse controls, provider receipt recovery and live runtime execution remain separate deployment work.
