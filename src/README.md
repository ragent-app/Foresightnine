# ForesightNine

ForesightNine is a public-facing research-scenario and portfolio-simulation product. It turns a user's thesis into an adversarially reviewed evidence record, probabilistic weak/main/strong worldlines, client-computed FM portfolios, selected-worldline NAV paths, and exportable research snapshots.

The product is intentionally not a broker, order router, custodian, or source of guaranteed investment returns. It does not accept brokerage credentials, place orders, hold money, or redistribute market data unless the deployment has separately completed the required licensing and legal reviews.

## Current status

The current checkout is a runnable local/private QA and release-candidate build. It is not yet a public production deployment. The core user flow is implemented and tested with deterministic mock providers; production exposure is fail-closed until deployment-owned infrastructure, secrets, licensed current data, security evidence, legal/compliance approvals, Android release signing, and a hosting target are supplied.

The most recent repository evidence is recorded in [`docs/progress.md`](docs/progress.md). The short status is:

| Area | Current state | Evidence / limitation |
| --- | --- | --- |
| Contracts and domain lifecycle | Implemented | Versioned v1 JSON Schema, generated TypeScript/Pydantic/OpenAPI, strict decoding and invariants |
| Challenge and evidence | Implemented | Deterministic mock provider; server-only provider boundary; client SearchEvidenceBundle fallback |
| Worldlines and probabilities | Implemented | Weak/main/strong paths with premise, conditional, and joint probability fields; live model still deployment-gated |
| Client FM | Implemented | TypeScript engine, Web Worker adapter, Android shared entry, Python independent WL1 oracle and parity audit |
| Web | Implemented for local QA | React/Vite flow and Browser/IAB evidence; public host is not configured |
| Android | Implemented for QA | Expo/RN app and API-36 emulator evidence; current APK is debug-only, not a release artifact |
| API persistence | Implemented for development/staging | FastAPI repositories, PostgreSQL migrations, Redis abuse-control adapter; production HA/TLS/backup evidence remains external |
| Security gates | Implemented as fail-closed checks | Secret scan, boundary checks, SSRF/input validation, preflight and release-candidate verifier |
| Public launch | Blocked by external inputs | See [`docs/security/production-preflight.md`](docs/security/production-preflight.md) and [`infra/deployment/production-release-runbook.md`](infra/deployment/production-release-runbook.md) |

### Verified local evidence snapshot

The latest full regression rerun in this checkout contains 205 API tests passed and 1 skipped, 51 Web tests passed, 44 Android tests passed, 20 FM tests passed, 9 domain tests passed, 2 asset-registry tests passed, and 12 API-client tests passed, with the existing Starlette/httpx warning. Typecheck, generated-contract checks, reference-hash checks, 9WL fixture audit, production-preflight unit tests, dependency audit, and the repository secret scan also pass in the recorded environment. Older historical counts remain in `docs/progress.md`; do not mix focused counts into this full-suite snapshot without labelling them.

The cutoff-safe WL1 audit used 20 assets and 64 dates through `2026-07-15`, explicitly excluded the online segment beginning `2026-07-17`, and matched the independent Python oracle to the TypeScript engine with maximum absolute difference `3.33e-16`. This is reproducible research/QA evidence, not a current investable recommendation or a public backtest claim.

## Architecture

The system is contract-first and provider-neutral. The canonical v1 schemas are the integration boundary shared by the Web client, Android client, API, tests, and exports.

```text
┌──────────────────────────────────────────────────────────────────────┐
│ User surfaces                                                        │
│  Web (React/Vite)                       Android (Expo/RN)             │
│  - viewpoint/evidence UI                - viewpoint/evidence UI        │
│  - challenge/worldline UI                - share-intent import         │
│  - FM in Web Worker                     - shared TS FM engine          │
└───────────────┬──────────────────────────────────────┬───────────────┘
                │ typed v1 HTTP/SSE + idempotency       │
                └──────────────────────┬───────────────┘
                                       │
                         ┌─────────────▼─────────────┐
                         │ FastAPI API                │
                         │ strict input/size/CORS     │
                         │ challenge/evidence flow    │
                         │ worldline orchestration    │
                         │ abuse control + CAPTCHA    │
                         │ typed operations/SSE replay │
                         └──────┬───────────┬─────────┘
                                │           │
              ┌─────────────────▼─┐   ┌────▼──────────────────────────┐
              │ Provider boundary  │   │ Durable service state          │
              │ mock (dev/tests)    │   │ PostgreSQL: research entities, │
              │ OpenCode DeepSeek   │   │ operations, receipts, SSE      │
              │ GLM profiles        │   │                                │
              │ MCP search adapter  │   │ Redis: distributed limits,     │
              │ client evidence     │   │ quota admission and short state │
              └─────────────────────┘   └────────────────────────────────┘

Data/compute boundary:
  API -> validated manifests/shards and method metadata
  Client -> factor evaluation, IC/ICIR, signal, weights, costs, amounts
  API never calculates or replaces the user's final portfolio.
```

### End-to-end product flow

1. The user submits a viewpoint, premise, time horizon, triggering event, expected asset impact, and available capital.
2. The API stores a typed thesis and asks the configured provider to challenge it. The challenge must expose counterexamples, logical jumps, potentially priced-in claims, missing evidence, and a bounded rubric score.
3. The user adds evidence or a `SearchEvidenceBundle`. Provider-native search is preferred; when it is unavailable, the client bundle remains visibly labelled as user-supplied and independently reviewable rather than provider-verified.
4. The API accepts a normal confirmation or an explicit confirmation-with-reservations. It then produces weak, main, and strong worldlines with time anchors, revealing events, asset impacts, evidence references, probability intervals, confidence, and calibration status.
5. The client receives an approved market/factor snapshot and computes FM locally. The Web path runs in a Worker; Android uses the same pure TypeScript engine through the shared package.
6. The UI displays both `FM Raw` and a separately derived `Public Default`. Public constraints never overwrite raw output. The result includes explanation, risks, stress tests, method version, data timestamp, uncertainty, and risk disclosure.

### Package and application map

| Path | Responsibility |
| --- | --- |
| `apps/web` | React/Vite Web application, Worker wiring, responsive interaction and exports |
| `apps/mobile` | Expo/React Native Android application, share-intent boundary and local portfolio display |
| `apps/api` | FastAPI adapter, repositories, provider adapters, abuse control, migrations integration |
| `packages/contracts` | Canonical v1 JSON Schema, generated runtime types, OpenAPI snapshots and errors |
| `packages/domain` | Dependency-free lifecycle, worldline, probability and invariant logic |
| `packages/fm-engine` | Pure deterministic TypeScript FM computation and client parity tests |
| `packages/design-system` | Shared design tokens and platform-neutral component semantics |
| `packages/asset-registry` | Asset identity, proxy, currency, price and trading-rule metadata |
| `packages/api-client` | Typed HTTP/SSE client and safe transport/error classification |
| `packages/test-fixtures` | A–G workflow fixtures, 9WL regression fixtures and Python FM oracle inputs |
| `infra/migrations` | PostgreSQL schema migrations |
| `infra/docker` | Development Compose and API image definition |
| `docs` | Architecture decisions, design concepts, QA evidence, security and release runbooks |

### Security and trust boundaries

- Provider secrets are server-only deployment inputs. Never place them in Web/Android bundles, source maps, fixtures, logs, databases, exports, Git history, or the Linux transfer archive.
- Provider output is untrusted JSON. It is bounded, parsed, schema-validated, semantically validated, and converted into safe typed errors before the client sees it.
- OpenCode and GLM adapters are opt-in. The default development provider is deterministic mock. Production/public-hardening mode rejects mock, unknown, unavailable, and debug providers.
- Provider-native search uses the exact configured tool contract. The current MCP adapter uses the official `web_search_prime` tool name; `webSearchPrime` is retained only as a strict fixture alias.
- Client URLs and evidence are untrusted. The API does not fetch user URLs and applies HTTPS, length, private-address and response-shape guards to evidence.
- CORS is explicit; forwarded IP headers are trusted only from configured proxy CIDRs. Public abuse control requires external CAPTCHA and TLS/authenticated Redis rather than silently falling back to memory.
- No future data may enter FM computation. Every market observation and forward-return observation is cutoff-labelled and checked against the cutoff.

## FM computation ownership and parity

FM is a client computation boundary, not a server convenience function. The implementation is a finite factor-expression AST (`field`, `constant`, `add`, `sub`, `mul`, `div`, `neg`, `abs`, `rank`, `rolling_mean`, `rolling_std`) evaluated by a switch visitor. It does not use `eval`, `Function`, dynamic imports, or arbitrary code execution.

The implementation reproduces the approved reference subset: cutoff-visible expanding windows, stable top-factor selection, rank-minus-0.5 signal, support-weighted composite signal, population z-score long-only weights, forecast scaling, fixed-point asset transfer, free initial allocation, 3 bps subsequent transfer cost, and strict cost dead-band. It preserves `NaN` as missing, omits invalid IC/ICIR metadata before factor admission, keeps stable input order for ties, and falls back explicitly to equal weights when usable support is insufficient.

The pinned read-only references and their path-independent hashes are recorded in `packages/test-fixtures/fm/v1/core-parity.json`. The independent oracle and re-run instructions are documented in [`docs/architecture/ADR-006-fm-engine.md`](docs/architecture/ADR-006-fm-engine.md) and [`docs/qa/wl-warmup-fm-audit.md`](docs/qa/wl-warmup-fm-audit.md).

## Dependencies and toolchain

### Required development toolchain

- Node.js 22.x (`package.json` requires `>=22.0.0`)
- Corepack with pnpm 11.x (`packageManager` pins `pnpm@11.21.0`)
- Python 3.13 (`apps/api/.python-version`)
- uv 0.11.7 or a compatible locked resolver
- Git 2.51 or newer on the recorded Windows workstation
- Java 17, Android SDK/platform API 36 and an emulator or device for native Android QA
- Docker/Compose only for the PostgreSQL/Redis development smoke; it is not a substitute for production HA/TLS/backup evidence

### Runtime dependencies

- Web: React 18, React DOM, Vite 6, TypeScript, Vitest and the shared workspace packages.
- Android: Expo 57, React Native 0.86, React 19, Expo Router, Secure Store, File System, Sharing, Reanimated and the shared workspace packages.
- API: FastAPI, Pydantic 2, JSON Schema validation, Uvicorn, psycopg, Redis client, httpx and pytest. Exact allowed ranges are pinned in `apps/api/pyproject.toml` and resolved in `apps/api/uv.lock`.
- Persistence: PostgreSQL 16 is used by the development Compose file; Redis 7 is used there for development-only distributed abuse-control wiring. Production versions, TLS, auth, HA, eviction and backup policy are deployment-owned decisions.

### Install and verification

Run from `D:\ForesightNine\src` on Windows, or the equivalent repository `src` root on Linux:

```powershell
corepack pnpm install --frozen-lockfile
uv sync --project apps/api --frozen

corepack pnpm verify:reference-hashes
corepack pnpm verify:boundaries
corepack pnpm scan:secrets
corepack pnpm verify:generated-contracts
corepack pnpm typecheck
corepack pnpm test
corepack pnpm audit:worldline-fixtures
corepack pnpm test:production-preflight
```

The API can be started locally with:

```powershell
uv run --project apps/api uvicorn app.main:app --app-dir apps/api --reload
```

For the reproducible Web/API demo, follow [`docs/runbooks/local-api-backed-demo.md`](docs/runbooks/local-api-backed-demo.md). It binds both processes to loopback, uses deterministic mock output, and keeps FM in the Web Worker. Do not interpret this demo as public availability.

## Web and Android design/QA

The visual direction is quiet, restrained and conversation-first, inspired by the usability qualities of modern AI products without copying Claude, Anthropic marks, or proprietary assets. Design concepts and state coverage live under [`docs/design/concepts`](docs/design/concepts). Web Browser/IAB and Android emulator evidence live under [`docs/qa`](docs/qa).

The current Android artifact is a debug QA build only. A public release requires an owner-controlled keystore, `assembleRelease`, artifact provenance, Expo/EAS and Play Console setup, privacy declarations, and a release approval manifest. See [`docs/security/android-release-signing.md`](docs/security/android-release-signing.md).

## Remaining plan for a production Web/Android release

The implementation order remains the approved Phase 0–9 order in `D:\ForesightNine\plan.md`. The remaining production work is deployment-owned and must not be hidden by changing the plan or labelling local QA as launch readiness.

1. **Provision an isolated Linux staging workspace.** Clone or unpack the approved source at a pinned commit. Keep research/server source mounts read-only and use a separate writable application workspace. Run the portable generated-artifact, typecheck, full-test, audit and secret gates on Linux; run the existing Windows-absolute-path reference/boundary gates on their Windows checkout, or use a separately reviewed Linux compatibility harness. Do not claim those two gates pass on Linux without such a harness.
2. **Choose production hosting and networking.** Provide an approved HTTPS Web host, API host, DNS, TLS termination, health checks, secure headers, deployment identity, logs/metrics/alerts, and an actual rollback target. The current GitHub Pages URL is not evidence of a live deployment.
3. **Provision durable services and the edge policy.** Provide PostgreSQL with migration/backup/restore evidence and Redis with TLS/authentication, bounded TTLs, failure alerts, HA/eviction policy and the required `rediss://` URL. Declare `FORESIGHTNINE_PROXY_MODE=reverse_proxy` with the actual proxy CIDRs in `FORESIGHTNINE_TRUSTED_PROXY_CIDRS`, or declare `direct` with no trusted CIDRs. No public deployment may use development Compose credentials, memory fallbacks or an implicit forwarded-IP trust policy.
4. **Configure live AI/search services.** Inject newly issued server-side credentials through a secret manager/environment. Run the default transport GLM/OpenCode and MCP smokes from the deployment network. Do not put keys in source, mobile/Web bundles, CI logs or artifacts; do not add `temperature`/`top_p` overrides when the selected provider does not expose them.
5. **Supply licensed, current market data.** Publish a signed provider/data manifest covering symbols, prices, FX, factors, timestamps, cutoff/freshness policy, redistribution rights, retention, exports and regional availability. Invalid, stale, unavailable or future-dated data must disable current Public Default amounts.
6. **Complete public abuse and privacy controls.** Integrate external CAPTCHA, set IP/session quotas and daily model-call budgets, verify CORS/CSP/SSRF/input limits, redact telemetry, define retention/deletion and incident response, and perform dependency/container/security scans.
7. **Build and validate production clients.** Build Web from the pinned commit, run Browser/IAB smoke against staging, create a signed Android release artifact, test the core flow on an API-36 emulator/device, and preserve screenshot/UI-tree and checksum evidence. Re-run client FM parity and verify no server-side final portfolio calculation has appeared.
8. **Obtain written approvals.** Complete target-region legal review, privacy policy, terms, risk disclosure, data-license approval, security/operations/rollback approval, Android signing ownership and artifact provenance. Only then run the release-candidate verifier with an external approval manifest and change traffic exposure.
9. **Operate after launch.** Monitor provider/search failures, quota exhaustion, persistence/Redis health, data freshness, client error rates and safe-deletion requests. Keep kill switches, rollback artifacts, method/data version labels and known limitations visible to users.

For the exact gate variables and order, use [`docs/security/production-preflight.md`](docs/security/production-preflight.md) and [`infra/deployment/production-release-runbook.md`](infra/deployment/production-release-runbook.md).

## Recommended Linux transfer boundary

Transfer the product source and reproducibility instructions, not the entire Windows research environment. The detailed checklist and AI-agent handoff are in [`docs/deployment/linux-transfer-manifest.md`](docs/deployment/linux-transfer-manifest.md) and [`docs/deployment/linux-agent-handoff.md`](docs/deployment/linux-agent-handoff.md).

### Package and transfer

- The tracked `src` source tree, `pnpm-lock.yaml`, workspace manifests, all TypeScript configs, `apps/api/pyproject.toml`, `apps/api/uv.lock`, Dockerfile/Compose, migrations, generated contracts, asset registry, scripts, design concepts, QA evidence and release/security documents.
- A pinned Git commit or signed archive manifest with SHA-256 checksums. Prefer a fresh clone plus a separately verified artifact bundle over copying a dirty working directory.
- Only approved, completed, licensed and cutoff-labelled data shards required for Linux smoke or a specific experiment. Keep large/raw datasets on a separate read-only data mount with a manifest and checksum; do not put them in the application image or Git repository by default.
- Read-only FM reference files and benchmark inputs only when the Linux agent needs to reproduce parity; mount/copy them read-only and verify the recorded hashes. Never modify the original `trade-agent-crisis`, `trade-agent-benchmark`, or other research source directories.

### Exclude from the transfer archive

- All API keys, the historical revoked key, `.env*`, shell history, tokens, cookies, credentials, provider receipts containing secrets, raw request logs, and personal data.
- `node_modules`, Python virtual environments, `.venv`, caches, `.pytest_cache`, Vite/Metro caches, `.playwright-cli`, temporary files, and local emulator state. Recreate dependencies from lockfiles.
- `dist`, `build`, generated Android intermediates and debug APKs unless a specific QA evidence package requires them. Never treat a debug APK as a release artifact.
- Unlicensed or incomplete market data, future-dated data, unreviewed user exports, and full copies of unrelated research/server directories.

The recommended Linux application workspace is a new directory owned by the deployment account, for example `/home/lxx/foresightnine`; any existing research directories should be source-only/read-only inputs. Do not write into `/home/lxx/trade-agent-crisis` or `/home/lxx/trade-agent-benchmark` as part of this transfer without separate authorization.

## Instructions for the Linux AI agent

1. Read `plan.md`, `agent.md`, this README, `docs/progress.md`, `docs/architecture/README.md`, `docs/security/production-preflight.md`, `docs/deployment/linux-transfer-manifest.md`, and `docs/deployment/linux-agent-handoff.md` before acting.
2. Treat `plan.md` as the approved baseline. Do not edit it for implementation preference, refactoring, missing credentials, or a failing local tool. Any objectively impossible requirement must first be recorded with command/output evidence and reviewed by Sol before the smallest plan change.
3. Write application changes only inside the writable ForesightNine `src` workspace. Keep the research and reference roots read-only. Preserve unrelated user changes and do not use destructive reset/checkout commands.
4. Use deterministic mock providers until deployment-owned live credentials exist. Secrets must arrive via the Linux secret manager/environment and must never be printed, committed, copied into fixtures, or sent to the client.
5. Keep the GLM/OpenCode/MCP adapters bounded and fail-closed. Use exact configured endpoints/tool names, server-only credentials, HTTPS candidate validation, quota reservation before external search, and no provider parameter overrides that are not part of the official contract.
6. Keep FM on the client. Run the Python oracle only as an independent audit; do not move final portfolio calculation to FastAPI, use future observations, or replace `FM Raw` with `Public Default`.
7. Before each phase handoff, run the relevant deterministic gates, update `docs/progress.md` with evidence and commit hashes, ask Sol to review, and only then proceed. A green mock test is not evidence of live provider or public production readiness.
8. Stop and request the missing external decision when the next action requires hosting authority, a new secret, licensed data, legal approval, release signing ownership, or public traffic permission. Continue all independent work that does not require that dependency.

## Known limitations

- Live provider networking and search must be tested from the actual deployment network; a local DNS/SSRF environment failure is not permission to weaken the guard.
- The current FM audit demonstrates formula and cutoff parity for the recorded WL1 slice, not investability, out-of-sample returns, or a guarantee that future data will be available or licensed.
- The development Compose file is secret-free and intentionally unsuitable for public production: it has development credentials, non-TLS Redis and mock provider wiring.
- Public launch, current-data delivery, legal/regulatory interpretation and target-region availability remain deployment gates, not claims made by this repository.

## Further reading

- [`docs/architecture/README.md`](docs/architecture/README.md) — architecture baseline and decisions.
- [`docs/progress.md`](docs/progress.md) — chronological implementation and QA evidence.
- [`docs/deployment/linux-transfer-manifest.md`](docs/deployment/linux-transfer-manifest.md) — what to package and what to exclude.
- [`docs/deployment/linux-agent-handoff.md`](docs/deployment/linux-agent-handoff.md) — Linux operator/AI-agent instructions.
- [`infra/deployment/README.md`](infra/deployment/README.md) — deployment gates.
- [`infra/deployment/production-release-runbook.md`](infra/deployment/production-release-runbook.md) — release sequence.
- [`docs/qa/README.md`](docs/qa/README.md) — QA evidence index.
