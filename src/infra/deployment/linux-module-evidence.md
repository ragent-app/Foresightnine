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
- Both migration files were rerun with `ON_ERROR_STOP=1`; existing relations
  were skipped idempotently. After restarting PostgreSQL, five tables and 11
  persisted source documents remained available, and the API health check
  succeeded.

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

## Module 5 — Cloudflare API staging

- `cloudflared 2026.8.2` is installed and the account certificate is present
  outside the repository with mode `600`.
- Owner dispatch selected the documented staging values: API hostname
  `api-staging.foresightnine.com`, Web CORS origin
  `https://staging.foresightnine.com`, origin `http://127.0.0.1:8000`, and
  locally managed Tunnel routing.
- Dedicated Tunnel `foresightnine-api-staging` was created with UUID
  `b3a095a8-b1a7-4dcc-b452-9a9cd83cd5a4`; DNS route creation returned success.
- The local config is outside Git at
  `/home/lxx/.cloudflared/foresightnine-api-staging.yml`, with the credential
  JSON outside Git and mode `600`. The redacted reproducible template is
  `cloudflared/foresightnine-api-staging.yml.example`.
- Ingress validation passed. The connector registered two QUIC edge
  connections (`sin07` and `sin16`), and its metrics showed zero origin
  request errors.
- Public `GET /health` returned 200 with a browser User-Agent, and a public
  CORS preflight returned 200 with the staging Web origin. A bare `curl`
  User-Agent receives the zone's Cloudflare managed Challenge; this is an
  edge policy behavior, not a Tunnel/origin failure.

No production hostname, Pages project, traffic cutover, OAuth callback, or
production Cloudflare mutation was performed.

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

## Portable and API validation gates

- `uv run --project apps/api pytest apps/api/tests -q`: 206 passed, 1 skipped.
- Workspace `typecheck` passed for all eight participating packages/apps.
- Workspace `test` passed, including generated-contract checks, FM-engine
  tests, API-client/domain/asset-registry tests, web/mobile tests, and the API
  suite (206 passed, 1 skipped).
- OpenAPI export drift check passed with 24 paths; generated TypeScript and
  asset-registry fingerprints matched their checked-in artifacts.
- Secret scan passed over 720 unique Git-history blobs.
- The worldline fixture audit remains unavailable on this Linux checkout
  because its configured source is the Windows-only path
  `D:/WL-data-final/wordline-md/wordline1.md`; no substitute data was guessed.

## Checkpoint index

Each module was committed and pushed in sequence. The Module 1 source pin is
the starting checkout; later module commits are the auditable checkpoints:

- Module 1: `7fda926`
- Module 2: `70d8822`
- Module 3: `f016a04`
- Module 4: `d70bfcd`
- Module 5: `e43e7d5`
- Module 6: `febdfdc`

The final evidence-only synchronization commit is recorded by Git history
after these entries are updated. The user's pre-existing dirty files remain
unstaged and are intentionally outside all checkpoints.
