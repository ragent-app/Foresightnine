# ADR-008: Redis distributed abuse control

## Status

Code-complete for the deterministic local contract and fake-Redis tests. Live
Redis, HA, TLS, eviction, failover, alerting, capacity, and rollback evidence
remain deployment gates.

## Decision

Select the backend with `FORESIGHTNINE_ABUSE_MODE=memory|redis`. Memory remains
the non-production default. Hardened environments must explicitly select Redis
and provide `FORESIGHTNINE_REDIS_URL` or `REDIS_URL`; startup fails closed for
missing, unknown, or memory configuration.

Redis request and model-quota admission each use one Lua script per logical
decision. Scripts obtain Redis `TIME`, prune expired sorted-set members, and
mutate all subjects only after the whole decision passes. Keys use SHA-256
subject/marker material under the fixed `{abuse}` hash tag so Redis Cluster
co-locates the transaction without exposing IPs, sessions, or operation IDs.
TTLs and cardinality are bounded by configuration. Reservation markers make a
repeated operation reservation at-most-once.

Backend errors become the stable `abuse_backend_unavailable` decision. The
transport maps request admission errors to retryable 503, and model reservation
errors to retryable `ApiProblem` 503 before provider work. Limit exhaustion keeps
the existing 429 and `Retry-After` behavior. Redis reset is a no-op and cannot
flush or broadly delete keys.

## Consequences

The API can coordinate abuse controls across workers without changing the
existing `AbuseDecision` or `.model.reserve(reservation_id, subjects)` shape.
Redis operations are bounded by redis-py connect/read timeouts. A live Redis
deployment must still supply secure transport/credentials, configure eviction
and retention policy, exercise failover, and provide operational evidence; the
repository intentionally does not claim those runtime properties.
