# ADR-003: Idempotent operations and typed SSE replay

- Status: accepted
- Date: 2026-08-10

## Decision

Every POST is an idempotent command keyed by required `Idempotency-Key`. A canonical request digest binds that key to one exact response and deterministic operation identity. Operation entities expose status, hashed idempotency identity, cancel/resume tokens and a terminal event cursor.

Streaming uses a closed JSON event union. IDs are `operation_id:sequence`; `Last-Event-ID` requests the strict suffix after that sequence. Heartbeat, error and complete are first-class typed events. Cancel/resume commands are idempotent, and a completion/cancel race resolves safely without replacing a completed result.

## Consequences

Refresh/retry cannot create a second mock result or silently advance lifecycle twice. A changed payload with the same key is a conflict. The PostgreSQL operation store now provides durable hash-only claims, typed receipts/errors, operation leases and append-only event sequence state; the explicit in-memory adapter remains for development/tests. Redis distributed abuse controls and provider-receipt-based resumption are separate deployment work. Both stores must preserve request digests, response bodies, control-token verification and append-only event sequence semantics.
