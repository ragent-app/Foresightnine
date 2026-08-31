# Linux staging module evidence

This file records redacted, local/private staging evidence for the Linux
agent modules. It is not production approval and contains no credentials.

## Module 1 — workspace and boundary

- Workspace: `/home/lxx/Foresightnine`
- Application source root: `/home/lxx/Foresightnine/src`
- Synced source and Linux provider/auth/observability/preflight/ops checkpoint: `11cfb1c`
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
- A fresh staging account was registered, the API container was restarted, and
  the account then logged in successfully before being hard-deleted; the
  post-deletion login returned 401. This directly verifies account persistence
  across an API process restart without retaining the test account.
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
- `backup-staging.sh` passed `bash -n`, rejected a repository-internal output
  path, and created a repository-external PostgreSQL custom dump (37,800 bytes),
  Redis RDB snapshot (2,540 bytes), and SHA-256 sidecar. Checksum verification
  passed; the generated files were kept mode `600`. Restore remains a separate
  owner-approved operation.
- The current backup was restored into isolated empty PostgreSQL and Redis
  containers: PostgreSQL recovered five public tables and 22 operation-state
  rows; Redis loaded the RDB with `loading=0` and eight keys. The temporary
  containers were removed after verification; the staging volumes were not
  used as restore targets.

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
- The staging Web preview binds to loopback `127.0.0.1:4173` under the enabled
  user service `foresightnine-web.service` with `Restart=on-failure`; its Vite
  preview responses now include CSP, COOP, Permissions-Policy, Referrer-Policy,
  `nosniff` and `DENY` frame protection.
- A controlled Web service cgroup fault increased `NRestarts` from 0 to 1; the
  unit returned active and the public Web route returned HTTP 200.
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
real Turnstile, public TLS, production Redis/PostgreSQL, or public traffic
evidence.

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
  connector registered two HTTP/2 edge connections (`sin14` and `sin17`), and
  its metrics showed zero origin request errors across three consecutive public
  health checks.
- Public `GET /health` returned 200 with a browser User-Agent, and a public
  CORS preflight returned 200 with the staging Web origin. A bare `curl`
  User-Agent receives the zone's Cloudflare managed Challenge; this is an
  edge policy behavior, not a Tunnel/origin failure.
- A user-level systemd unit now supervises the connector with
  `Restart=on-failure`, `ProtectHome=read-only`, `NoNewPrivileges` and
  `PrivateTmp`; `loginctl` reports linger enabled. A controlled SIGKILL fault
  test incremented `NRestarts` to 1, then the service returned active/running
  and both public health endpoints returned 200.

No production hostname, Pages project, traffic cutover, credential-backed OAuth
exchange, or production Cloudflare mutation was performed.

## Module 6 — bounded provider and client market runtime

- The hidden `POST /snapshots` route is protected by the same Redis abuse
  boundary as `/v1/*` and accepts only the shared three-anchor request shape.
- Historical pre-authorization staging evidence used a bounded Stooq-first
  chain with a Yahoo fallback. That chain is retired; Yahoo is no longer
  configured or called.
- The code contains an isolated `public_evaluation` Stooq adapter with an
  optional server-only `FORESIGHTNINE_STOOQ_API_KEY` input, the fixed symbol
  allowlist (`GRID/PAVE`, `CHIP/SOXX`, `UTIL/XLU`, `GOLD/GLD`, `WORLD/VT`, and
  canonical `ETF:SPY`, `ETF:SOXX`, `ETF:GLD`, `ETF:QQQ`, `ETF:IEF`), bounded
  response size, an eight-second timeout and redirect rejection. It never
  accepts a URL or credential from a request, caches raw responses, or derives
  factor or FX values. The current manifest enables no provider, so this path
  is not admitted at runtime.
- Compose retains the explicit staging mode, but the unapproved manifest makes
  all requested provider rows `missing` and keeps Public Default disabled;
  `CASH` and any unapproved asset also return `missing` rows.
- The current provider recheck records Stooq, Kenneth French MOM/RMW, AQR QMJ
  and Frankfurter as candidates only. No public production provider is
  approved; the manifest keeps all four data-authorizations unapproved and
  leaves production mode disabled.
- Before the policy update, the live staging route accepted a canonical
  `ETF:SPY`, legacy `GRID`, and unapproved `CASH:USD` request through the
  Stooq-first chain: 9 rows were returned, with 6 price rows and 3 explicit
  missing rows; the network challenge caused Yahoo fallback to serve the six
  price rows. This is historical evidence only. After the policy update, a
  Stooq challenge produces explicit missing rows rather than silently switching
  to an unauthorized price source.
- The container copies and loads the admission manifest at startup; a missing
  or invalid manifest disables evaluation rows rather than enabling a provider
  by code default. The release preflight also checks that the manifest is
  present in the source image inputs.
- Runtime admission now validates every requested asset symbol against a unique
  asset-level entry in that manifest. The five legacy IDs and five canonical
  ETF IDs are therefore bound to the fixed Stooq source; missing, duplicate,
  or mismatched mappings fail closed.
- The production preflight now has a separate `DATA_AUTHORIZATION` gate: the
  manifest must explicitly mark market price, factor features and FX reference
  fields approved with written terms and public redistribution rights. The
  checked-in staging manifest records all three authorizations as unapproved;
  production admission remains blocked.
- The gate accepts a deployment-owned provider manifest through the external
  `--provider-admission-manifest` flag or
  `FORESIGHTNINE_PROVIDER_ADMISSION_MANIFEST`; absent that input, it safely
  evaluates the checked-in staging manifest and fails production admission.
- The fixed-host adapter rejects vendor HTTP redirects instead of allowing a
  provider response to move the request to an unapproved host. The production
  preflight parses the manifest and rejects malformed JSON, empty provider or
  asset lists, and duplicate IDs.
- Hardened API startup now validates CORS origins itself: production and
  public-hardening runtimes reject non-HTTPS, credential-bearing, query-bearing,
  fragment-bearing, or non-root origins; an absent declaration remains an empty
  allowlist.
- Production CAPTCHA now has a concrete Cloudflare Turnstile server verifier:
  the secret never leaves the server, the fixed siteverify endpoint rejects
  redirects and oversized responses, and a successful response must match the
  configured deployment hostname. Missing, rejected, unsupported, or remote-
  unavailable verification remains fail-closed; staging intentionally has no
  real CAPTCHA secret.
- Browser OAuth start/callback routes now share the same IP abuse limiter as
  `/v1/*` and `/snapshots`, preventing unauthenticated callback flooding from
  bypassing the production rate boundary.
- The security regression suite now covers 22 cases, including real Turnstile
  verifier selection, hostname mismatch rejection, and OAuth route rate limits.
- The production preflight additionally requires that CAPTCHA hostname to match
  one of the declared HTTPS CORS origin hostnames, preventing a valid but
  misbound Turnstile site from satisfying the release gate.
- The 2026-08-31 Linux probe confirmed Stooq returned a JavaScript verification
  page, while Frankfurter returned a no-key USD/EUR reference rate and Kenneth
  French's daily momentum archive downloaded successfully. AQR QMJ remains a
  candidate quality primary. These observations grant no redistribution rights:
  all price, factor and FX authorizations remain unapproved in the manifest,
  and the three-anchor response emits neither factor features nor FX
  conversion. The auditable candidate order and mappings are in
  `provider-manifest.v1.json`.
- The adapter now marks a provider session before the requested anchor as
  `stale` instead of `ok`, while preserving the real `asOf` date. This keeps
  delayed or holiday-ambiguous rows visible but prevents the client from
  treating them as eligible current Public Default data.
- After rebuilding the API image from `0ca79ba`, the public canonical
  `ETF:SPY` three-anchor request returned HTTP 200; delayed sessions were
  explicitly marked `stale` and no factor/FX fields were emitted.
- The follow-up image from `7a9db9b` remained healthy after restart; a public
  request for `ETF:SPY` and `ETF:SOXX` returned HTTP 200 with six rows, one
  exact-anchor `ok` row per asset and four delayed/holiday-ambiguous `stale`
  rows. Future vendor sessions are rejected before they can reach the client.
- The image rebuilt from `69e01b0` returned HTTP 200 for the public canonical
  route with three explicit `missing` rows for `ETF:SPY`; no provider request,
  Yahoo fallback or unauthorized factor/FX value was made because the current
  manifest enables no provider. `/readyz` remained HTTP 200.

## Module 7 — auth/session contract boundary

- Existing typed server boundary exposes redacted provider readiness for exactly
  Google and GitHub and a strict PKCE start response.
- A feature-gated browser OAuth path now provides fixed Google/GitHub
  authorization redirects, one-use callback state consumption, bounded
  server-side code exchange, verified identity extraction, persistent OAuth
  identity records, session issuance, logout, and generic account deletion.
  Provider access tokens are not persisted or returned to clients.
- Development staging also exposes an explicit `local_mock` lifecycle for
  registration, password login, bearer session, logout and account deletion.
  Passwords are stored only as PBKDF2 digests; deletion removes the account
  credential/contact document and all sessions, while research data remains
  under its separate retention policy.
- Focused OAuth foundation, route, and callback/provider tests passed `29/29`,
  covering HTTPS and deep-link allowlists, fixed provider redirects, bounded
  code exchange, verified identities, S256 challenge derivation, one-use/
  expiry state, strict request fields, and secret redaction.
- Generated OpenAPI drift check passed with 24 paths.
- The callback implementation is not enabled in staging because all six OAuth
  environment inputs are absent. The live Google/GitHub exchange remains
  unverified until owner-supplied client credentials, exact callback URLs,
  confirmed web/deep-link return URIs, provider consent, and the production
  session/deletion policy exist. Without those inputs, the public callback and
  browser start routes return a typed 400 without contacting a provider.

The safe state is a production-capable, feature-gated callback plus local
staging auth; no OAuth provider was contacted and no credential was added to the
repository or runtime.

## Module 8 — Web/Android smoke evidence

- Web staging `https://staging.foresightnine.com/` returned HTML 200. A
  Playwright fallback smoke test (the Browser plugin was unavailable) verified
  meaningful content, challenge submission entering the receiving state, a
  390x844 mobile viewport, and zero browser console errors.
- After the Web supervisor/security-header hardening, the same public Web route
  returned 200 with the expected CSP, COOP, Permissions-Policy, Referrer-Policy,
  `X-Content-Type-Options: nosniff` and `X-Frame-Options: DENY` headers.
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
- Public staging account smoke registered and hard-deleted an ephemeral local
  mock account: registration returned 201, deletion returned 200, subsequent
  login returned 401, and the test-created orphan auth session was removed.

## Portable and API validation gates

- `corepack pnpm test` completed the workspace tests and API suite;
  the latest run was 230 passed, 1 skipped. The only skip is
  the optional Redis integration test because
  no external integration URL was supplied.
- `corepack pnpm test:production-preflight`: 13 passed. The real
  `verify:production-preflight` command correctly rejects this development
  environment because production credentials, TLS Redis, external CAPTCHA,
  OAuth configuration, production admission, release artifact, monitoring
  declaration and approval manifest are absent. The checked-in provider
  manifest intentionally remains staging-only and unapproved.
- After the provider recheck, the real preflight reports
  `DATA_AUTHORIZATION FAIL` and `MARKET_DATA_ADMISSION FAIL`; no provider
  authorization or licensed production mode is being claimed.
- Workspace `typecheck` passed for all nine participating packages/apps.
- `corepack pnpm verify:generated-contracts` passed for generated TypeScript,
  asset registry, and the 25-path OpenAPI export.
- `corepack pnpm audit --prod --audit-level high` and `uvx --from pip-audit
  pip-audit apps/api` both reported no known vulnerabilities. The release
  candidate verifier passed its repository/QA artifact checks, while the
  production preflight correctly continues to reject the debug APK as a
  production release artifact.
- `corepack pnpm verify:phase2` passed the design-asset, image-regression,
  contrast and design-system checks. `verify:reference-hashes` remains a
  Windows-only gate because its six authoritative references use unavailable
  `D:\` paths; the baseline was not rewritten.
- `corepack pnpm verify:boundaries` was run and correctly failed on Linux-only
  portability constraints: the repository intentionally references the
  Windows `D:\Foresightnine` governance files and Windows implementation-root
  suffix. The baseline was not weakened or rewritten.
- Workspace `test` passed, including generated-contract checks, FM-engine,
  API-client/domain/asset-registry, market-data, web and mobile tests.
- OpenAPI export drift check passed with 25 paths; generated TypeScript and
  asset-registry fingerprints matched their checked-in artifacts.
- Secret scan passed over 1,097 unique Git-history blobs.
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
Compose CORS wiring is `e797bb4`. The historical Linux provider/auth/observability
implementation checkpoint is `595a71c`; the current unapproved-provider and
Web-hardening checkpoint is `ad4d8ae`; external provider authorization input
handling is in `a70413e`, OAuth callback, provider mapping, redirect hardening
and runtime CORS hardening are in `3eaf5dc`, `ea8f12c` and `a0fb7b3`. This
evidence follow-up records the final test/artifact details. These commits contain only the redacted
config/evidence and staging runtime configuration. The user's
pre-existing dirty files remain unstaged and are intentionally outside all
checkpoints.
