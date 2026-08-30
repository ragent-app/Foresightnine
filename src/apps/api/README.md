# ForesightNine API

FastAPI adapter for the canonical v1 JSON Schema. Pydantic models are generated at runtime, all model and request objects reject unknown fields, and deterministic mock outputs are schema/semantic validated. No provider key, network search, live market claim, broker connection or server-side final portfolio calculation exists in Phase 1.

## Phase 3 provider boundary

`DeterministicMockLlmProvider` is the default and needs no key. It exercises the complete Challenge/evidence path offline. `OpenCodeDeepSeekFlashProvider` is opt-in only with the non-secret server setting `FORESIGHTNINE_LLM_PROVIDER=opencode`; it invokes `opencode run --format json -m opencode-go/deepseek-v4-flash` and does not read, print, persist, or inject any credential. A deployment's new OpenCode secret must be supplied only by its server-side secret manager/environment to the CLI.

The development default is never a production fallback: when `FORESIGHTNINE_ENV=production` or `FORESIGHTNINE_PUBLIC_HARDENING=true`, an explicit mock provider and any unknown provider selector fail closed without generating a simulated challenge. Run the repository production preflight before a release.

The live OpenCode path is bounded to one concurrent call, pre-reserved round/token/cost budgets, timeout/cancel handling, and exponential retry only for 429/5xx/timeout. Runtime OpenCode envelopes, not a static setting, determine whether native search was available; absent search results keep client fallback active. Its JSON is rejected unless it can be rebuilt into and validated against the canonical `ChallengeRecord`.

## Phase 8 public boundary

The API has a provider-neutral abuse boundary with a memory default for
development/tests and an atomic Redis backend for multi-worker deployments.
Both expose per-IP and per-session sliding-window limits plus an at-most-once
model-call quota reservation. Redis uses server-clock Lua scripts, hashed
subjects, one-script multi-subject admission, bounded sorted sets and key TTLs;
it never falls back to memory after a runtime failure. Strict JSON
body/content-size limits, security response headers, and explicit CORS origins
remain enabled. `X-Forwarded-For` is ignored unless the direct peer is in
`FORESIGHTNINE_TRUSTED_PROXY_CIDRS` (a comma-separated CIDR list). Configure
`FORESIGHTNINE_PUBLIC_HARDENING=true` or `FORESIGHTNINE_ENV=production` only
with a real deployment `CaptchaVerifier`; an unavailable verifier fails closed.
The deterministic accept verifier is limited to non-public development/test
defaults and is not a production integration.

Relevant settings are `FORESIGHTNINE_ABUSE_MODE` (`memory` or `redis`),
`FORESIGHTNINE_REDIS_URL` (or `REDIS_URL`), `FORESIGHTNINE_MODEL_CALL_QUOTA`,
`FORESIGHTNINE_MAX_BODY_BYTES`, and `FORESIGHTNINE_CORS_ORIGINS`. Production
preflight also requires an explicit `FORESIGHTNINE_PROXY_MODE` (`direct` or
`reverse_proxy`): direct mode must have an empty
`FORESIGHTNINE_TRUSTED_PROXY_CIDRS`, while reverse-proxy mode must list the
actual ingress CIDRs. Redis socket
and connect timeouts are bounded by `FORESIGHTNINE_REDIS_SOCKET_TIMEOUT_SECONDS`
and `FORESIGHTNINE_REDIS_CONNECT_TIMEOUT_SECONDS`. Development defaults to
memory. Production/public-hardening requires an explicit Redis mode and URL;
memory, unknown, missing-mode, and missing-URL configurations fail closed at
startup. Redis connection, timeout, or protocol failures become stable retryable
503 responses, while actual limit exhaustion remains 429. No CAPTCHA vendor,
key, URL fetcher, or production Redis HA/TLS/eviction policy is selected here.

Telemetry is a bounded allow-list of low-cardinality counters/events. It does
not retain request content, credentials, tokens, raw URLs, or IP addresses.
Expired sessions cascade through their thesis, challenge, evidence, and linked
worldline state; active work is retained. Terminal operation/idempotency/SSE
state is bounded without deleting pending/running work.

Client evidence is a canonical-hashed bundle of URL, title, publication/retrieval times, excerpt and user note. URL evidence requires `published_at`, begins at grade C and is independently reviewable but not independently verified; no-URL evidence is grade D/non-reviewable. URLs are never fetched by the API and receive protocol, length and private-address guards.

From the workspace root:

```powershell
uv sync --project apps/api --frozen
uv run --project apps/api pytest apps/api/tests
uv run --project apps/api python apps/api/scripts/export_openapi.py --check
uv run --project apps/api uvicorn app.main:app --app-dir apps/api
```

## Entity storage boundary

The API builds typed repositories for sessions, theses, challenges,
challenge turns, evidence, worldlines, market manifests, and worldline
challenge links at import time. Development and tests default to deterministic
in-memory repositories and make no database connection. A deployment must set
`FORESIGHTNINE_ENV=production` or `FORESIGHTNINE_PUBLIC_HARDENING=true`,
`FORESIGHTNINE_STORAGE_MODE=postgres`, and a nonblank server-side URL in
`FORESIGHTNINE_POSTGRES_URL` or `DATABASE_URL`; missing or memory storage then
fails closed. The existing `FORESIGHTNINE_POSTGRES_DSN` name remains supported
as a compatibility alias.

PostgreSQL access is lazy, but a selected Postgres operation failure is surfaced
as a storage runtime error and is never replaced with memory. The migration is
`infra/migrations/001_research_state.sql`.

This boundary does not make the complete service production-ready: live Redis
HA/TLS/eviction/alerting evidence, provider receipt recovery, and deployment
rollback evidence remain external gates. The development Compose file starts
Postgres and Redis with healthchecks and explicitly wires the API to the
secret-free Redis backend. It remains a development-only setup without Redis
TLS/authentication; production still requires deployment-owned hardened Redis
configuration and runtime evidence.
