# ADR-007: Durable operation state

- Status: accepted and implemented for PostgreSQL persistence
- Date: 2026-08-29

## Decision

Operation claims, operation documents, operation-to-session associations and
append-only SSE events use a dedicated store separate from research entity
documents. Production/public-hardening configuration selects PostgreSQL;
development and tests may select the explicit in-memory adapter. The claim
boundary stores only SHA-256 idempotency-key and request hashes, never raw keys
or request bodies. PostgreSQL uses a unique `(endpoint,
idempotency_key_hash)` constraint and row locks for terminal transitions.

The initial operation receipt and events are committed before provider work.
Completion, cancellation and lease-expiry recovery are serialized against the
operation row. An expired lease becomes a retryable terminal failure; the
provider is not automatically resubmitted because the current provider has no
receipt that proves idempotent external execution. SSE replay retains the
`operation_id:sequence` contract and rejects cursors older than the retained
event window.

## Consequences and limits

Same-body retries replay the typed response and different bodies conflict before
provider work. The store supports cross-process ownership when a live
PostgreSQL runtime is configured, but this repository has not yet executed a
live migration or container test on the current host. Redis-backed distributed
rate limits/model quotas, a worker queue and provider-specific receipt recovery
remain later deployment slices. The operation store does not permit the service
to claim public production readiness by itself.
