# ForesightNine

ForesightNine is a public research-scenario and portfolio-simulation product that turns a user thesis into an adversarially reviewed evidence record, probabilistic weak/main/strong worldlines, client-computed FM portfolios, selected-worldline NAV paths and exportable research snapshots. It deliberately contains no broker execution, credentials, custody or licensed market-data redistribution.

## Layout

- `apps/web`: React + Vite + strict TypeScript Web client.
- `apps/mobile`: React Native + Expo Android client.
- `apps/api`: `uv`-managed FastAPI service using deterministic mock providers when no server-side provider is configured.
- `packages/contracts`: versioned canonical schemas, OpenAPI and generated/verified runtime types.
- `packages/domain`: dependency-free TypeScript state machine and invariants.
- `packages/fm-engine`: deterministic client FM implementation and golden parity tests.
- `packages/design-system`: shared Web/Android tokens and component semantics.
- `packages/asset-registry`: instrument identity, proxy, currency, price and trading rules.
- `packages/test-fixtures`: A–G workflow, 9WL regression and Python FM golden fixtures.
- `infra`: Docker, PostgreSQL migrations and deployment manifests.
- `docs`: architecture, API, design, compliance, QA and phase evidence.

All authored implementation is under this `src` directory. The experiment, benchmark, paper and server source roots listed in `docs/verification/read-only-boundaries.md` are read-only inputs. The historical provider credential is registered only as `revoked`; its value or recoverable hash is never stored.

## Pinned local toolchain

- Node.js 22.18.0 or compatible 22.x
- pnpm 11.21.0 through Corepack
- Python selected by `apps/api/.python-version`
- uv 0.11.7 or compatible locked resolver
- Git 2.51 or newer on Windows

## Local verification

```powershell
corepack pnpm install --frozen-lockfile
uv sync --project apps/api --frozen

corepack pnpm verify:phase0
corepack pnpm verify:phase1
corepack pnpm verify:phase2
corepack pnpm typecheck
corepack pnpm test

uv run --project apps/api uvicorn app.main:app --app-dir apps/api --reload
```

Phase-specific commands may be unavailable until that phase lands; a missing command is a failed gate rather than an implicit pass. `verify:reference-hashes`, `verify:boundaries` and `scan:secrets` are deterministic and do not rewrite their expected results.

The API advertises contract version `v1`. Without a newly issued server-side provider secret, deterministic mock providers are mandatory. Their output is schema-validated research material, never current investment advice.

## Safety and release gates

- Never place a provider secret in Web/Android bundles, source maps, logs, fixtures, databases or exported research.
- Client URLs and evidence are untrusted input; Phase 3 applies strict validation, sanitation and SSRF protection.
- Market-data licence and target-region legal review are hard gates for public production deployment.
- FM Raw is never overwritten by Public Default. Public constraints are a separate derivation with a recorded explanation.
- Historical and simulated NAV are separated at the decision date; simulated paths always carry uncertainty and no-return-promise disclosures.
