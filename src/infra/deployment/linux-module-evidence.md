# Linux staging module evidence

This file records redacted, local/private staging evidence for the Linux
agent modules. It is not production approval and contains no credentials.

## Module 1 — workspace and boundary

- Workspace: `/home/lxx/Foresightnine`
- Application source root: `/home/lxx/Foresightnine/src`
- Synced source and Linux provider/auth/observability/preflight checkpoint: `3a5595336f16c76bd49472ed3578ba18539212a6`
- GitHub provider-guidance sync: `d0c8204d9c3e42c83ec3fc1c034ebba8dd47a92b`
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

- Redis 7 runs on the private Compose network with a named `/data` volume and
  AOF `everysec` persistence. Production still requires deployment-owned
  authenticated TLS Redis and HA policy.
- The application `RedisAbuseControl` connected through a redacted `rediss://`
  URL and admitted the first two requests before denying the third at the
  configured rate limit.
- Model quota reservation admitted the first operation and denied a second
  distinct operation; generated abuse keys had positive TTLs.
- A deliberately unreachable Redis backend returned unavailable/denied and
  did not fall back to memory.

This is local persistence and failure-path evidence, not production HA,
authenticated TLS, failover, alerting, capacity, managed backup or
certificate-rotation evidence.

## Module 4 — supervised FastAPI mock staging evidence

- API image built successfully from the pinned checkout with the locked Python
  environment, generated contracts, asset registry, and migrations included.
- Compose now keeps PostgreSQL and Redis on the private Docker network, binds
  API port `8000` to loopback only, uses `unless-stopped` restart policy,
  `init`, a healthcheck, and limits API resources to 1 CPU and 512 MiB.
- API healthcheck became `healthy`; `/health` and `/openapi.json` returned
  success, reporting `deterministic_mock` and `client-evidence-bundle`.
- Persistent startup now probes PostgreSQL repositories and Redis before the
  FastAPI process accepts traffic; a failed probe raises a startup error rather
  than silently reverting to memory state.
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
- Ingress validation passed. The connector initially used QUIC, but this
  Linux network later showed repeated idle QUIC timeouts and temporary 530
  responses. The config was adjusted to `protocol: http2`; the restarted
  connector registered two HTTP/2 edge connections (`sin12` and `sin21`), and
  its metrics showed zero origin request errors across three consecutive public
  health checks.
- Public `GET /health` returned 200 with a browser User-Agent, and a public
  CORS preflight returned 200 with the staging Web origin. A bare `curl`
  User-Agent receives the zone's Cloudflare managed Challenge; this is an
  edge policy behavior, not a Tunnel/origin failure.

No production hostname, Pages project, traffic cutover, OAuth callback, or
production Cloudflare mutation was performed.

## Module 6 — bounded provider and client market runtime

- The hidden `POST /snapshots` route is protected by the same Redis abuse
  boundary as `/v1/*` and accepts only the shared three-anchor request shape.
- The optional `yahoo_evaluation` mode uses a fixed HTTPS host, fixed symbol
  allowlist (`GRID/PAVE`, `CHIP/SOXX`, `UTIL/XLU`, `GOLD/GLD`, `WORLD/VT`,
  and the canonical `ETF:SPY`, `ETF:SOXX`, `ETF:GLD`, `ETF:QQQ`, `ETF:IEF`),
  bounded response size and an eight-second timeout. It does not accept a URL
  or credential from a request, does not cache raw responses, and does not
  derive factor or FX values.
- Compose enables this mode only for local/development staging. Returned rows
  are price-only and warnings explicitly keep the client runtime limited;
  `CASH` and any unapproved asset return `missing` rows.
- Provider authorization, factor/FX licensing and redistribution approval are
  not asserted. Production mode disables this adapter unless an approved
  provider implementation is supplied.
- The live staging route accepted a canonical `ETF:SPY`, legacy `GRID`, and
  unapproved `CASH:USD` request: 9 rows were returned, with 6 price rows and
  3 explicit missing rows. No client-side vendor URL or credential was used.
- The 2026-08-31 Linux probe confirmed Yahoo chart returned JSON for SPY and
  SOXX, Frankfurter returned a no-key USD/EUR reference rate, and Kenneth
  French's daily momentum archive downloaded successfully. Stooq returned a
  JavaScript verification page, so it was not enabled. The auditable status
  and mappings are in `provider-manifest.v1.json`.

## Module 7 — auth/session contract boundary

- Existing typed server boundary exposes redacted provider readiness for exactly
  Google and GitHub and a strict PKCE start response.
- Development staging also exposes an explicit `local_mock` lifecycle for
  registration, password login, bearer session, logout and account deletion.
  Passwords are stored only as PBKDF2 digests; deletion removes the account
  credential/contact document and all sessions, while research data remains
  under its separate retention policy.
- Focused OAuth foundation and route tests passed `25/25`, covering HTTPS and
  deep-link allowlists, provider separation, S256 challenge derivation,
  one-use/expiry state, strict request fields, and secret redaction.
- Generated OpenAPI drift check passed with 24 paths.
- Live Google/GitHub callback and token exchange remain disabled. They are
  blocked on owner-supplied client credentials, exact callback URLs, confirmed
  web/deep-link return URIs, provider consent, and the production session/
  deletion policy.

The safe state is contract/mock-only OAuth readiness plus local staging auth; no
OAuth provider was contacted and no credential was added to the repository or
runtime.

## Module 8 — Web/Android smoke evidence

- Web staging `https://staging.foresightnine.com/` returned HTML 200. A
  Playwright fallback smoke test (the Browser plugin was unavailable) verified
  meaningful content, challenge submission entering the receiving state, a
  390x844 mobile viewport, and zero browser console errors.
- Staging API `/health` and `/readyz` both returned 200. The API CORS preflight
  allowed only `https://staging.foresightnine.com` for the tested POST route.
- A redacted Prometheus text endpoint is enabled for the local supervisor at
  `/metrics`; the Cloudflare ingress matches that exact path first and returns
  404, so operational counters are not public.
- Android debug assembly completed with Temurin JDK 17 and the user-local
  Android SDK. APK SHA-256:
  `37db03028d3fb207664dab15f419b50f640c66340dd9fa27e0c210e4324cb97d`.
- Google/GitHub buttons remain intentionally disabled as `尚未配置`; no
  provider credentials or callback exchange were fabricated.

## Portable and API validation gates

- `uv run --project apps/api pytest apps/api/tests -q`: 214 passed, 1 skipped
  (215 collected). The only skip is the intentionally unavailable Windows
  worldline source audit.
- `corepack pnpm test:production-preflight`: 12 passed. The real
  `verify:production-preflight` command correctly rejects this development
  environment because production credentials, TLS Redis, external CAPTCHA,
  OAuth configuration, licensed market admission, release artifact, monitoring
  declaration and approval manifest are absent.
- Workspace `typecheck` passed for all nine participating packages/apps.
- Workspace `test` passed, including generated-contract checks, FM-engine,
  API-client/domain/asset-registry, market-data, web and mobile tests.
- OpenAPI export drift check passed with 24 paths; generated TypeScript and
  asset-registry fingerprints matched their checked-in artifacts.
- Secret scan passed over 767 unique Git-history blobs.
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

The Cloudflare staging synchronization commit is `25d7f90`; the follow-up
Compose CORS wiring is `e797bb4`. The current Linux provider/auth/observability
implementation checkpoint is `3a55953`; this evidence follow-up records the
final test/artifact details. These commits contain only the redacted
config/evidence and staging runtime configuration. The user's
pre-existing dirty files remain unstaged and are intentionally outside all
checkpoints.
