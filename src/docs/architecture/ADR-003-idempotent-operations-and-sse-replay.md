# ADR-003: Idempotent operations and typed SSE replay

- Status: accepted
- Date: 2026-08-10

## Decision

Every POST is an idempotent command keyed by required `Idempotency-Key`. A canonical request digest binds that key to one exact response and deterministic operation identity. Operation entities expose status, hashed idempotency identity, cancel/resume tokens and a terminal event cursor.

Streaming uses a closed JSON event union. IDs are `operation_id:sequence`; `Last-Event-ID` requests the strict suffix after that sequence. Heartbeat, error and complete are first-class typed events. Cancel/resume commands are idempotent, and a completion/cancel race resolves safely without replacing a completed result.

## Consequences

Refresh/retry cannot create a second mock result or silently advance lifecycle twice. A changed payload with the same key is a conflict. The current in-memory store is not production durability; PostgreSQL/Redis implementation must preserve request digests, response bodies, control-token verification and append-only event sequence semantics.
