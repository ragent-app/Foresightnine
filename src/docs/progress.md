# Progress

## Phase 2 strict-decoder and manifest-closure fix — 2026-08-15

Sol's second review found two false-negative paths in `af9b473`: `pngjs` alone accepted a valid-CRC PNG whose zlib header was invalid, and the registry did not reject duplicate paths or all required/variant/archive role drift.

### Fix

- Added `scripts/verify/design-png-guards.mjs`: verifies every chunk CRC and order, restricts the approved 8-bit non-interlaced encoding, inflates the concatenated IDAT stream with `inflateSync(..., { info: true })`, requires all compressed bytes to be consumed, and checks the exact filtered scanline length before `pngjs` decodes RGBA pixels.
- Added `scripts/verify/design-assets-regression.mjs` to construct in memory, with valid PNG CRCs, an invalid zlib header and trailing compressed bytes; both are required to fail the guard.
- The manifest gate now consumes `required_web`/`required_android`, rejects duplicate registry paths and duplicate variants, validates roles against the exact required/variant/archive path sets, and rejects role overlap, missing files and unregistered files.
- Added an explicit `archive_paths` manifest list so archive membership is independently checked in both directions against the registry role set.

### Verification

- `corepack pnpm verify:phase2`: passed all 35 pixel decodes, exact dimensions, required/variant/archive closure, and both corruption regressions.
- Manual proof: a legal-CRC invalid zlib header is rejected with `incorrect header check`; no file is written by the regression test.

## Phase 2 verifier hardening — 2026-08-13

### Blocker

Sol's independent Phase 2 re-review of commit 2d5cf57 returned FAIL: the PNG verifier checked only the signature, IHDR, dimensions and the presence of an IEND marker anywhere in the byte stream, without decompressing IDAT. A corrupted pixel stream still produced PASS. Two archive PNGs with non-standard Android dimensions (916x1717, 864x1821) were also accepted without being registered as historical exceptions.

### Fix

- Added an authoritative assets registry to phase2-manifest.json: all 35 PNGs (13 required, 4 variant, 18 archive) each with exact expected width, height and role. The verifier now refuses any on-disk PNG that is not registered and any registered PNG that is missing from disk.
- Rewrote design-assets.mjs to decode every pixel via pngjs PNG.sync.read. A corrupt or truncated IDAT stream, bad CRC or incomplete RGBA buffer now throws and fails the gate. Verified by a synthetic corruption test: PNG.sync.read throws on bad compressed data, where the old verifier returned PASS.
- Strict dimension comparison: every decoded PNG width and height must exactly match its manifest entry. The two non-standard archives are registered at their true historical dimensions and reported as archive historical exception; they are not subject to the 9:16 artboard rule but are still pixel-decoded and dimension-checked.
- Added pngjs and @types/pngjs as workspace dev dependencies.

### Verification evidence

- corepack pnpm install --frozen-lockfile and uv sync --project apps/api --frozen: passed.
- corepack pnpm typecheck: passed all five typed workspace projects.
- corepack pnpm verify:phase0: passed six immutable FM references, all governance boundaries, 119 unique Git-history blobs and the current-tree secret scan.
- corepack pnpm verify:phase1: passed generated contract/registry/OpenAPI drift checks, 4 domain tests, 2 registry tests and 20 API tests.
- corepack pnpm verify:phase2: passed 35 pixel-decoded PNG assets (exact dimension match), 13 required concepts, 4 state variants, 2 registered historical-exception archives, all specifications, contrast checks and design-system typecheck.
- corepack pnpm test: passed contracts, design-system, 4 FM parity tests, 4 domain tests, 2 registry tests and 20 API tests.
# Progress

## Phase 2 reviewer corrections complete — 2026-08-10

### Delivered

- Corrected the Web FM concept to the executable pipeline: one-based percentile rank minus 0.5, IC-sign direction, global factor quality `|IC|×|ICIR|`, per-asset valid-support combination, population-standard-deviation z-score, clipping and final long-only re-normalisation.
- Corrected evidence grade C to secondary commentary/expert interviews; it is no longer described as a company filing.
- Corrected Android Raw and Public Default NAV horizons to a coherent `2024-08 → 2026-08-10 today → 2028-08-10 (+24m)` boundary.
- Retained every replaced concept in an `archive` directory. The active suite contains 13 required full-state concepts and four state variants: Web/Android SSE recovery and Web/Android Public Default delta.
- Strengthened Phase 2 verification to inspect every PNG recursively, including variants and archives, and to enforce the required Web and Android artboard dimensions for both primary and variant states.
- The standing user instruction to accept all proposed solutions and continue through completion covers this reviewer-directed correction pass; its scope and source are recorded in `phase2-manifest.json`.

### Verification status

- `corepack pnpm install --frozen-lockfile` and `uv sync --project apps/api --frozen`: passed with the checked-in lockfiles.
- `corepack pnpm typecheck`: passed all five typed workspace projects.
- `corepack pnpm verify:phase0`: passed six immutable FM references, all governance boundaries, 119 unique Git-history blobs and the current-tree secret scan.
- `corepack pnpm verify:phase1`: passed generated contract/registry/OpenAPI drift checks, 4 domain tests, 2 registry tests and 20 API tests.
- `corepack pnpm verify:phase2`: passed 35 recursively inspected PNGs, 13 required concepts, 4 state variants, all specifications, contrast checks and design-system typecheck.
- `corepack pnpm test`: passed contracts, design-system, 4 FM parity tests, 4 domain tests, 2 registry tests and 20 API tests.
- Original-resolution visual inspection passed the corrected FM equation, evidence-grade C definition and both Android NAV horizons. Formal Sol re-review remains the phase-gate authority.

## Phase 1 Sol confirmation blocker remediated — 2026-08-10

### Delivered

- Reproduced the blocker at commit `1af8069`: an initial `CHALLENGING` record with a 20/40 rubric accepted ordinary `CONFIRM` with `unresolved_issues_acknowledged=false`, then confirmation-derived provider logic rewrote the rubric to 30/40.
- Ordinary `CONFIRM` now requires a rubric total of at least 30/40; `premise_clarity`, `time_boundary`, `causal_chain`, `asset_transmission` and `falsifiability` must each be at least 3/4; `missing_evidence` and `unresolved_issues` must both be empty. Failure returns structured 409 code `confirmation_requirements_not_met` with per-gate details and leaves the challenge and turn counter unchanged.
- `CONFIRM_WITH_RESERVATIONS` is an explicit pre-confirmation command from `CHALLENGING` or `EVIDENCE_REQUIRED`. Canonical validation requires `unresolved_issues_acknowledged=true` and a non-whitespace reservation note.
- Confirmation no longer invokes the research provider. It copies the current claims, scoring, missing evidence, unresolved issues, AI objections, questions and private-evidence declaration unchanged; only the new turn, confirmed status, user confirmation, optional reservation note, provenance and content hash are written.
- The deterministic mock review becomes rubric-passing only after independently reviewable evidence is accepted. Provider scoring no longer depends on `CONFIRMED` status.
- Added direct early-confirmation rejection, false acknowledgment/missing/whitespace note, reservation preservation, no-rewrite and passing ordinary-confirmation regression coverage. Updated the shared domain reservation command for both pre-confirmation review states.
- Added the canonical error code and non-whitespace note constraint, then regenerated TypeScript contracts and both 20-path OpenAPI snapshots. Canonical schema SHA-256 is `deb077db03cd740cf9d7e2d618ad5fc9c5610bdd4ac9e6fe0f1f8c26fa48d01d`.

### Verification evidence

- `corepack pnpm install --frozen-lockfile`: passed; all 6 workspace projects already up to date.
- `uv sync --project apps/api --frozen`: passed; 32 packages checked.
- `corepack pnpm typecheck`: passed all 5 typed workspace projects.
- `corepack pnpm verify:phase1`: passed generated-contract/OpenAPI drift gates, 4 domain tests, 2 registry tests and 20 API tests.
- `corepack pnpm test`: passed contracts, design-system, 4 FM parity tests, 4 domain tests, 2 registry tests and 20 API tests.
- The existing third-party Starlette `httpx2` migration warning remains non-failing and unchanged.

### Scope preservation

- This remediation did not edit design concepts, FM implementation/fixtures, governance files, root plans or any declared read-only source.
- No Git commit was created; integration remains with the main thread.

## Phase 1 gate complete — 2026-08-10

### Delivered

- Established `packages/contracts/schemas/v1/contracts.schema.json` as the sole Draft 2020-12 authority. The previous entity/research files are compatibility references only.
- Generated strict TypeScript contracts (schema SHA-256 `8763a1c15fde062aba0978226c41c3dfd899980eae0bd90b8f51750f8eab4b49`), compiled Pydantic models directly from the schema, and exported a 20-path OpenAPI 3.1 snapshot with fully resolvable local references.
- Added strict unknown-property rejection to core entities, nested records, requests, responses and errors. Every core entity composes `schema_version`, `created_at`, `source_refs` and `content_hash`.
- Added premise, conditional and joint probability scalars plus intervals, ordering/containment/product invariants, and explicit `model_estimate_unvalidated` mock status.
- Added a generated 36-entry asset registry covering all 15 benchmark economic exposures, explicit tradable proxies, global equity/bond/commodity products, BTC/ETH, REIT and cash. Registry source, snapshot and nested hashes are drift-gated.
- Added versioned market manifest/shard, factor manifest/shard with whitelist postfix tokens and admission thresholds, and client-computation attestation contracts/endpoints. Market mock data is explicitly `historical_only`; final portfolio computation remains client-side.
- Completed all planned API endpoints plus canonical request/success/error payloads. Every POST requires `Idempotency-Key`; same-body retry returns the exact operation/result after state advancement, while body conflict returns 409.
- Added deterministic fixed clock and content-derived session/thesis/challenge/worldline/operation IDs, operation/cancel/resume tokens, typed SSE started/progress/heartbeat/error/complete events, and suffix replay via `Last-Event-ID`.
- Enforced the approved lifecycle graph. Early confirmation requires the explicit `CONFIRM_WITH_RESERVATIONS` command, acknowledged unresolved issues and a non-empty reservation note.
- Added content-hashed versioned A–G acceptance fixtures and TS compile positive/negative gates, domain state/probability/worldline tests, registry tests, Python schema/model/API/operation tests, OpenAPI resolver tests and generation drift tests.
- Added `verify:generated-contracts` and a real `verify:phase1`; root `test` executes all workspace tests plus the Python API suite.

### Verification evidence

- `corepack pnpm install --frozen-lockfile`: passed; 6 workspace projects, lockfile current.
- `uv sync --project apps/api --frozen`: passed; 32 packages checked.
- `corepack pnpm typecheck`: passed for contracts, domain, asset-registry, design-system and the existing fm-engine.
- `uv run --project apps/api pytest apps/api/tests`: **18 passed**; one third-party Starlette warning recommends migrating its TestClient from `httpx` to `httpx2`.
- `corepack pnpm verify:phase0`: passed 6 immutable reference hashes, all 3 governance hashes/boundaries and secret scan.
- `corepack pnpm verify:phase1`: passed generated TS/registry/OpenAPI drift checks, 4 domain tests, 2 registry tests and 18 Python tests.
- `corepack pnpm verify:phase2`: passed 13 approved concepts, 4 variants, specification files, contrast checks and design-system typecheck.
- `corepack pnpm test`: passed contracts generation/type tests, 4 existing fm-engine parity tests, 4 domain tests, 2 registry tests, design-system typecheck and 18 Python tests.

### Preserved concurrent work

- No source or test file under `packages/fm-engine` was edited; its existing parity suite was only executed by required root verification.
- Existing Phase 0 governance/reference/boundary/secret scripts and Phase 2 concepts/design-system work were retained and passed unchanged gates.
- `plan.md`, `agent.md`, `GOAL_PROMPT.md` and all declared external reference/source roots remained read-only; Phase 0 boundary hashes confirm this.

### Known limits / later-phase obligations

- The Phase 1 stores and SSE replay log are process-local. PostgreSQL/Redis persistence must preserve the tested wire semantics before public operation.
- Integrity records use a clearly labelled deterministic mock key ID, not production signing. Live provider/search behavior, rate limits, abuse controls, licensed current market data and production key management remain later phases.
- The Starlette TestClient deprecation warning is dependency-owned and non-failing; dependency migration should be handled with a lockfile-reviewed toolchain update.

The older baseline below is retained as historical evidence and is superseded where it described interval-only probability, incomplete endpoints, or unavailable streaming.

## Phase 0/1 baseline — 2026-08-10

### Delivered

- pnpm workspace and TypeScript strict baseline, with contracts and domain packages.
- `v1` JSON Schema/OpenAPI/TypeScript contracts for core research entities and API outputs.
- Explicit research lifecycle state machine plus probability interval and worldline DAG invariants.
- `uv`/FastAPI skeleton with schema-validated mock Challenge, Worldline, Probability, session, evidence, asset, health, and methodology endpoints.
- Contract tests that validate live FastAPI mock responses against the shared JSON Schema.
- Docker API skeleton and two architecture ADRs.

### Material judgment calls

- No key or real provider was configured; the approved mock fallback is the only active provider.
- Probability remains interval-only and declares `model_estimate_unvalidated` to avoid pseudo precision.
- Failure states are not persisted as a new lifecycle enum: an operation failure retains its durable prior state and can retry, preserving the approved state-machine vocabulary.

### Evidence and verification

- `corepack pnpm typecheck`: passed for `@foresightnine/contracts` and `@foresightnine/domain`.
- `uv run pytest` from `apps/api`: **5 passed**. A third-party FastAPI/Starlette TestClient deprecation warning remains; it does not affect contract results.
- Credential-like assignment scan of authored source/config/docs (excluding dependencies, virtual environments, generated output, and lock files): no matches.

### Known limits / next phase

- No persistence, streaming, rate limit, search adapter, real market data, FM engine, Web, or Android implementation yet.
- OpenAPI is the public endpoint description; generated client artifacts will be added with the consuming clients.

