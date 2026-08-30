# Linux staging module evidence

This file records redacted, local/private staging evidence for the Linux
agent modules. It is not production approval and contains no credentials.

## Module 1 — workspace and boundary

- Workspace: `/home/lxx/Foresightnine`
- Application source root: `/home/lxx/Foresightnine/src`
- Pinned source at execution start: `3a169d6bf91ef2edc7601027107cb50e7a4f893d`
- Branch: `master`, tracking `origin/master`
- Research/reference roots observed and left untouched:
  `/home/lxx/trade-agent-crisis`, `/home/lxx/trade-agent-benchmark`
- Cloudflare authentication material is outside the repository at
  `/home/lxx/.cloudflared/cert.pem` with mode `600`.
- Existing user worktree edits were preserved and are not part of this
  module's commit; they are listed by `git status` at handoff time.
- Rollback target: the pinned source SHA above.

Commands used:

```text
git rev-parse HEAD
git status --short --branch
git remote -v
docker compose -f src/infra/docker/compose.yaml config --quiet
```

The repository checkout is writable and the Compose configuration parses
successfully. Production traffic, public DNS, and production secrets are not
claimed by this evidence.

## Module 2 — PostgreSQL local staging evidence

- Compose PostgreSQL 16 container reached `healthy`.
- Both checked-in migrations were applied; five expected public tables were
  present: `research_state_documents`, `operation_state`,
  `operation_idempotency`, `operation_events`, and
  `operation_session_associations`.
- A synthetic mock flow completed session, challenge, message, evidence,
  confirmation, and worldline generation with three branches.
- A custom-format `pg_dump` completed at 23,014 bytes; restoring it into a
  fresh database restored all 11 source documents.
- After an API restart, the same session idempotency key replayed the same
  persisted session result with `Idempotency-Replayed: true`.
- Expiry cleanup was exercised against the PostgreSQL-backed repositories and
  removed the expired session plus its thesis, challenge, evidence, turns,
  worldline links, and worldlines.
- A storage bug found during this evidence was fixed: PostgreSQL JSONB scalar
  strings are preserved after the driver has already decoded them. The fix has
  a focused regression test.

The database container has no published host port. This is development/mock
staging evidence only; production PostgreSQL credentials, TLS/HA, backup
retention and restore ownership remain external gates.

## Module 3 — Redis abuse-control local staging evidence

- A separate Redis 7 container was started on the private Compose network with
  TLS enabled on port `6380`, password authentication, no plaintext port, a
  64 MiB memory limit, and `allkeys-lru` eviction policy.
- The application `RedisAbuseControl` connected through a redacted `rediss://`
  URL and admitted the first two requests before denying the third at the
  configured rate limit.
- Model quota reservation admitted the first operation and denied a second
  distinct operation; generated abuse keys had positive TTLs.
- A deliberately unreachable Redis backend returned unavailable/denied and
  did not fall back to memory.

This is a local TLS/authentication and failure-path probe, not production HA,
failover, alerting, capacity, managed backup or certificate-rotation evidence.

## Module 4 — supervised FastAPI mock staging evidence

- API image built successfully from the pinned checkout with the locked Python
  environment, generated contracts, asset registry, and migrations included.
- Compose now keeps PostgreSQL and Redis on the private Docker network, binds
  API port `8000` to loopback only, uses `unless-stopped` restart policy,
  `init`, a healthcheck, and limits API resources to 1 CPU and 512 MiB.
- API healthcheck became `healthy`; `/health` and `/openapi.json` returned
  success, reporting `deterministic_mock` and `client-evidence-bundle`.
- A fresh synthetic flow completed session, challenge, message, evidence,
  confirmation, and three-branch worldline generation through the running
  container.
- API logs contained only normal startup/request metadata in the inspected
  tail; no provider credential or raw evidence payload was emitted.

This is private loopback/mock staging evidence. It is not live-provider,
CAPTCHA, public TLS, production Redis/PostgreSQL, or public traffic evidence.

## Module 5 — Cloudflare staging blocker

- `cloudflared 2026.8.2` is installed and the account certificate is present
  outside the repository with mode `600`.
- Read-only account inventory shows three existing Tunnel objects, all with
  no active connections; none was selected because the documented inventory
  does not identify a usable API route.
- The API is healthy on `127.0.0.1:8000`, but no Cloudflare Tunnel was created
  and no DNS record was changed.
- Module 5 is blocked pending owner confirmation of the exact final API
  hostname, dedicated Tunnel name/UUID, origin address/port, routing mode,
  CORS origin, and staging approval. The document's hostname list is marked
  proposed and must not be treated as authorization.

No public DNS, traffic cutover, OAuth callback, or Cloudflare production
mutation was performed.

## Module 6 — auth/session contract boundary

- Existing typed server boundary exposes redacted provider readiness for exactly
  Google and GitHub and a strict PKCE start response.
- Focused OAuth foundation and route tests passed `25/25`, covering HTTPS and
  deep-link allowlists, provider separation, S256 challenge derivation,
  one-use/expiry state, strict request fields, and secret redaction.
- Generated OpenAPI drift check passed with 24 paths.
- Live callback/token exchange, server session issuance, logout and account
  deletion are not enabled by the current code. They remain blocked on
  owner-supplied Google/GitHub client credentials, exact callback URLs,
  confirmed web/deep-link return URIs, and the session/deletion policy.

The safe state is contract/mock-only OAuth readiness; no provider was contacted
and no credential was added to the repository or runtime.
