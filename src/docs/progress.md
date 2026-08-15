# Progress

## Phase 4 Sol gate remediation — 2026-08-15

- Closed synchronous unknown-provider failures in `OperationManager.execute`: ordinary exceptions now append the existing safe `operation.error`, transition to `failed`, retain the idempotent terminal error, release single-flight waiters, and remove pending execution state without exposing exception text.
- Bound accepted typed worldlines to the retained thesis and request: every path must retain the thesis premise, fixed API baseline date, requested horizon, bundle/challenge identity, and the existing probability partition validation before any worldline is persisted.
- Tightened worldline graph semantics in both API and domain validation: causal parents must exactly match incoming edges; schedules are non-empty, cover every node exactly once, use each node's event date, and retain the documented early-reveal `leak` exception only.
- Replaced the 9WL index with an auditable deterministic regression fixture. Each record carries source paths/SHA-256 values, copied title/trigger and stage-news reveal date, normalized event-date rationale, and complete synthetic weak/main/strong test branches. The fixture explicitly disclaims forecasts and does not present its probabilities as source estimates.

## Phase 4 provider-boundary gate reinforcement — 2026-08-15

- Revalidated every typed `WorldlineBundle` at `POST /v1/worldlines` before persistence: canonical schema, request challenge identity, bundle/worldline identity, worldline time/graph/reveal constraints, and weak/main/strong conditional partition plus joint-probability semantics.
- Revalidated typed `ProbabilityAssessment` results at `POST /v1/probabilities/assess` with the canonical schema, requested-worldline identity, and joint-probability semantics. Boundary failures now use the existing safe failed-operation terminal path without returning provider diagnostics.
- Added fake typed-output regressions for challenge/bundle identity, nested asset-impact schema, reveal-horizon, conditional partition, wrong-worldline and invalid-joint probability assessments; rejected bundles persist no worldlines.

## Phase 4 Worldlines and Probability — 2026-08-15

### Delivered

- Added provider-neutral `WorldlineGenerator` and `ProbabilityAssessor` interfaces, with deterministic no-key mock defaults. Worldline generation now derives its premise from the stored thesis belonging to a `CONFIRMED` challenge; the API rejects caller-supplied premise/path fields.
- Canonical worldline contracts now include node event/data-as-of dates, embedded asset impacts, and three attached probability assessments for weak/main/strong paths. Contracts, generated TypeScript, and OpenAPI are regenerated together.
- Enforced schema-plus-semantic gates for acyclic graph shape, edge-time monotonicity, baseline/horizon containment, no future data references, event/reveal rules, exact interval/scalar joint recomputation, shared premise probabilities, and conditional branch partition consistency.
- Added one 9WL regression fixture containing nine reference-derived scenario records, each with weak/main/strong expected structure, probability decomposition and event/reveal schedule expectations; added API/domain coverage including unknown-provider fail-closed behavior.

### Material judgment and limit

- The fixture copies only minimal scenario titles and trigger timing from the read-only 9WL source. Its probability values are deliberately deterministic test inputs, not historical estimates or forecasts.
- Live OpenCode smoke remains **blocked**: no newly authorized server-side provider secret is available. No live-provider claim is made; deterministic mock completion is the verified path.

## Phase 3 formal FAIL remediation — 2026-08-15

### Delivered

- Reworked the OpenCode 1.3.0 adapter for raw JSONL events while retaining only a compatibility path for single-envelope mocks. It now extracts final structured text/payload and usage, and establishes native-search provenance only from actual `tool_use.part → part.tool` `search`/`websearch` telemetry with `part.state.status` `completed`/`success`; generic `capabilities`, `tools`, `search`, and `results` fields never establish provenance.
- Added an opt-in asynchronous Phase 3 message endpoint. It returns the existing typed operation receipt before provider completion; `StreamingResponse` supplies replay, typed terminal events and keepalive heartbeats. Cancel signals the actual provider runner event before state commit. Existing synchronous endpoints remain synchronous and unchanged.
- Made `OperationManager.execute` endpoint-plus-idempotency-key single-flight under concurrency. Same-body callers wait and replay the one submission; mismatch remains a conflict. A Barrier regression test proves one provider call.
- Made evidence-backed follow-up messages return `CHALLENGING` only when both draft evidence gaps and unresolved issues are clear. Normal confirmation now has an audit regression proving preserved citations/provenance after a valid public-evidence turn.
- Promoted `citation_ids`, `public_fact_citation_ids`, `native_search_used` and server-derived `provenance` into required canonical `ChallengeRecord` fields, regenerated TypeScript and OpenAPI, and retained attached-evidence grade validation.
- Tightened usage so `total_tokens` is an exact non-negative Python integer and `cost_usd` is a finite, non-negative non-bool integer/float; rejected bool, NaN, infinity and negative values. Expanded prompt-control detection to forget-prior, approval and compliance forms across claims and untrusted evidence strings.

### Verification and limit

- `uv run --project apps/api pytest apps/api/tests`: **51 passed** (one unchanged third-party TestClient deprecation warning), including JSONL, single-flight Barrier, provider-blocked SSE/cancel and terminal async-replay tests.
- Live smoke remains **blocked**: no new authorized server-side provider key is present. No historical key was used and no DeepSeek-live claim is made.

## Phase 3 Sol FAIL remediation — 2026-08-15

### Corrections and regression evidence

- Replaced static OpenCode search capability with fail-closed runtime telemetry verification: only a real successful `tool_use.part → part.tool → part.state.status` search event establishes provenance; missing qualifying telemetry forces client fallback.
- Added canonical-hashed full client evidence bundles, C/not-yet-verified URL evidence, D/no-URL evidence, publication-time enforcement and legacy IPv4/IPv6 SSRF rejection.
- Made draft fields, rubric objects and citation subset relations strict; D/E cannot support public facts. Normal confirmation additionally requires independently reviewable evidence, while reservation confirmation is unchanged.
- Added pre-call budget reservation, finite non-negative usage validation, retry only for 429/5xx/timeout, and immediate non-transient CLI failure.
- Registered running operations and cancellation events before provider execution; cancel reaches the live callback, prevents commit/re-submit, and explicit resume uses that callback rather than synthetic completion.
- `uv run --project apps/api pytest apps/api/tests`: **35 passed** (one unchanged third-party warning). Live smoke remains **blocked by missing new server-side secret** and is not represented as DeepSeek-live verification.

## Phase 3 independent review remediation — 2026-08-15

- Removed unsafe provider re-execution from resume. Cancelled/failed operations now retain their terminal audit checkpoint, replay without a second submission/charge, and do not emit synthetic completion events; a genuinely new user command is required for another provider attempt because OpenCode CLI exposes no auditable charge receipt.
- Rebuilt `evidence_ids` and `evidence_grades` from all attached challenge evidence on every later message turn.
- Tightened native-search provenance to actual completed/successful OpenCode `tool_use.part → part.tool → part.state.status` telemetry. Generic `capabilities`, `tools`, `search`, and `results` fields are all rejected as non-provenance, including result-shaped payloads.
- Caught `ApiProblem` from `build_result`, records typed `operation.error`, transitions to `failed`, and idempotently replays that error rather than leaving a running operation.
- Regression suite: `38 passed`, including provider submission-count assertions across failure/cancel/resume, message evidence propagation, native-search false positives and `ApiProblem` lifecycle closure.

## Phase 3 Challenge and Evidence — 2026-08-15

### Delivered

- Added a provider-neutral `LlmProvider` and `SearchEvidenceProvider`, with deterministic mock as the default and an opt-in OpenCode DeepSeek Flash adapter using the required CLI model form. The adapter never reads, prints, stores or forwards credentials; it applies a concurrency-one gate, rounds/token/cost budgets, timeout/cancel termination and bounded exponential backoff for 429/5xx.
- Added fail-closed structured-output handling: malformed JSON, prompt-control text, incomplete/invalid 10-dimension rubric output, unattached citations and D/E evidence promoted as public facts all produce no challenge record. Accepted output is rebuilt as and validated against the existing canonical `ChallengeRecord`.
- Added client evidence fallback URL safety: HTTP(S), size, user-info, port and literal private/reserved-IP guards, plus a redirect-target DNS validation hook for any future fetcher. The API never server-fetches client evidence. Missing URL stays grade D/unreviewable and leaves challenge evidence requirements open.
- Preserved the Phase 1 normal-confirmation gate and extended typed SSE/Last-Event-ID/cancel/resume/idempotency behavior for provider failures: a failed original request replays its failure without re-submitting; explicit resume retains the operation ID and makes the original request replay the validated result. No canonical schema expansion or generated-contract change was necessary.
- Added reviewed JSON fixture and tests for mock end-to-end fallback, OpenCode success/no-search capability, 429, 502, timeout, cancel, invalid JSON, prompt injection, citation mismatch, D/E public-fact rejection and SSRF/redirect checks.

### Verification and limitation

- `uv run --project apps/api pytest apps/api/tests`: 28 passed; one unchanged third-party Starlette TestClient deprecation warning.
- Live smoke is **blocked by missing server secret** in this environment. The OpenCode path is implemented but is not represented as DeepSeek-live verified; deterministic mock acceptance is the verified path.

## Phase 2 strict-decoder and manifest-closure fix — 2026-08-15

Sol's second review found two false-negative paths in `af9b473`: `pngjs` alone accepted a valid-CRC PNG whose zlib header was invalid, and the registry did not reject duplicate paths or all required/variant/archive role drift.

### Fix

- Added `scripts/verify/design-png-guards.mjs`: verifies every chunk CRC and order, restricts the approved 8-bit non-interlaced encoding, inflates the concatenated IDAT stream with `inflateSync(..., { info: true })`, requires all compressed bytes to be consumed, and checks the exact filtered scanline length before `pngjs` decodes RGBA pixels.
- Added `scripts/verify/design-assets-regression.mjs` to construct in memory, with valid PNG CRCs, an invalid zlib header and trailing compressed bytes; both are required to fail the guard.
- The manifest gate now consumes `required_web`/`required_android`, rejects duplicate registry paths and duplicate variants, validates roles against the exact required/variant/archive path sets, and rejects role overlap, missing files and unregistered files.
- Added an explicit `archive_paths` manifest list so archive membership is independently checked in both directions against the registry role set.
- Added PNG `PLTE` ordering, uniqueness, length and grayscale-compatibility guards; the regression suite now rejects a legal-CRC `PLTE-after-IDAT` stream.

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

# Phase 5 FM engine — implementation handoff — 2026-08-15

### Sol P1 remediation in progress

- Corrected forecast scaling to average `|IC|` only over factor IDs that actually
  contributed to the support-weighted composite; added a regression proving an
  unused selected factor cannot change forecast values or dead-band decisions.
- Changed `forwardReturns` to typed `{asOf, values}` rows. Every row must align to
  its observation timestamp and be no later than `cutoff`; future and mismatched
  rows fail closed before metrics evaluation. Plain `number[][]` is no longer part
  of the TypeScript contract and is rejected at runtime as malformed input.
- Selection now skips non-finite `icMean`/`icir` metadata before `maxFactors`
  truncation; NaN/Infinity-only inputs retain explicit equal-weight fallback while
  stable finite-quality ties remain input ordered.

### Delivered

- Added the pure client `computeFm` orchestration API with intermediate factor
  signals, selected IDs, combination/support, weights, forecast scale, forecasts,
  metrics, rebalance/dead-band result, and explicit reasons. Every observation is
  cutoff-checked before sorting/evaluation; future rows are rejected.
- Corrected factor-quality ties to Python stable input ordering and retained NaN
  support semantics, equal-weight fallback, free initial allocation, 3 bps default
  subsequent transfer, strict dead-band comparison, FX conversion, lot rounding,
  and canonical hashing.
- Added the finite AST DSL and browser Worker adapter. Core source contains no
  Node/DOM imports; the Android-sized test imports and executes the same package
  entry with 15 assets.
- Reworked the Python fixture generator to hash all six immutable references and to
  emit null for non-finite values, input orientation, cutoff data, formula cases for
  combination/portfolio/metrics/backtest, and the client-bound walk-forward/ASSETS
  subsets. Import failure is visible and `--require-reference-import` is fail-closed;
  normal `--check` is deterministic because transient exception text is not stored.
- Added [ADR-006](architecture/ADR-006-fm-engine.md) with the formula mapping,
  client-only boundary, Worker/Hermes compatibility, oracle mode, and limitations.

### Verification evidence

- `Set-Location D:\Foresightnine\src; uv run --project apps/api python scripts/fm/generate_oracle.py --check`: passed byte-identical fixture and all six hashes. The local environment reports `ModuleNotFoundError: No module named 'matplotlib'` and uses the documented copied-formula oracle mode.
- `Set-Location D:\Foresightnine\src; corepack pnpm --filter @foresightnine/fm-engine typecheck`: passed.
- `Set-Location D:\Foresightnine\src; corepack pnpm --filter @foresightnine/fm-engine test`: passed 12 tests, 0 failures, including Python parity, reference cases, AST rejection, cutoff, Worker, Android-sized smoke, fallback, 3 bps, dead-band, FX, and lot checks.

Workspace and phase gates remain to be run in the primary session before the Phase
5 commit. Full reference-package import remains unavailable in the current minimal
`uv` environment; this is the only known oracle limitation.

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

