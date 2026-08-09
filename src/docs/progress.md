# Progress

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
- `corepack pnpm verify:phase2`: passed 13 approved concepts, 2 variants, specification files, contrast checks and design-system typecheck.
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
