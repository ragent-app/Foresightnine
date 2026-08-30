# Linux transfer manifest

This manifest defines the recommended transfer from the Windows development checkout to a new Linux application workspace. It is a packaging boundary, not permission to deploy publicly. The source of truth remains the approved `plan.md` and `agent.md`; this document only makes the transfer reproducible.

## Recommended destination

Use a new writable application directory owned by the deployment account, for example:

```text
/home/lxx/foresightnine
```

Treat `/home/lxx/trade-agent-crisis` and `/home/lxx/trade-agent-benchmark` as read-only research/reference inputs. Do not overwrite, rename, or run a write-producing workflow against those directories. If a Linux experiment needs their files, mount or copy a selected subset into a separate read-only data directory and record its source path, source SHA-256, cutoff and licence status.

## Tier A — required product source

Copy or clone these paths from the pinned ForesightNine commit:

```text
src/apps/web/
src/apps/mobile/
src/apps/api/app/
src/apps/api/scripts/
src/apps/api/tests/
src/apps/api/pyproject.toml
src/apps/api/uv.lock
src/apps/api/.python-version
src/packages/
src/infra/docker/
src/infra/migrations/
src/scripts/
src/package.json
src/pnpm-lock.yaml
src/pnpm-workspace.yaml
src/tsconfig.json
src/tsconfig.base.json
```

Also include all package-local `package.json`, `tsconfig*.json`, Expo configuration, schema, generated OpenAPI, registry and source files. A fresh Git clone at a reviewed commit is preferred; if an archive is used, include a file list and SHA-256 manifest.

## Tier B — required reproducibility and acceptance evidence

Include these documents and assets so the Linux agent can reproduce decisions and understand what is, and is not, a production claim:

```text
plan.md                         # governance input; copy read-only
agent.md                        # governance input; copy read-only
GOAL_PROMPT.md                  # governance input; copy read-only
src/README.md
src/docs/progress.md
src/docs/architecture/
src/docs/security/
src/docs/compliance/
src/docs/operations/
src/docs/release/
src/docs/runbooks/
src/docs/verification/
src/docs/qa/
src/docs/design/concepts/
src/docs/deployment/
src/infra/deployment/
```

The PNG/XML QA evidence is optional for a runtime-only server image but should be kept in the evidence archive. It is not a substitute for re-running Browser/IAB or Android device tests on Linux/staging.

The current `verify:reference-hashes` and `verify:boundaries` scripts resolve the recorded Windows absolute paths and the Windows `D:\Foresightnine\src` implementation-root suffix. Therefore they are Windows checkout gates in the current repository. On Linux, preserve the governance/reference files as read-only and run the portable gates; do not report the two Windows-path gates as passed. A Linux run may use a separately reviewed compatibility harness that maps the approved read-only sources and records the mapping, but it must not rewrite the baseline manifests or weaken the boundary check.

## Tier C — optional, separately controlled research inputs

Transfer only the exact completed inputs required for a named audit or experiment:

- cutoff-labelled, licensed market/factor shards;
- a manifest containing source, field definitions, currency/lot semantics, data timestamp, decision cutoff, licence/redistribution status and SHA-256;
- the six pinned FM reference files, or a read-only mount to them, when an independent parity audit is required;
- the completed WL1 slice needed by `scripts/fm/audit_wl_warmup.mjs` and its Python oracle.

Do not put raw or large market datasets in the application Docker image or Git repository by default. Keep them outside the writable application tree, mount them read-only, and disable Public Default amounts if the manifest is missing, stale, unsigned, future-dated, unavailable or not licensed for the target region.

## Never transfer

The following must not be copied, committed, or included in an archive:

```text
.env*
files containing credentials, access tokens, API keys, passwords, cookies or private key blocks
shell history, credential stores and provider receipts containing secrets
node_modules/
nm/ (the pnpm virtual store configured by pnpm-workspace.yaml)
.venv/ and Python virtual environments
.pytest_cache/ .mypy_cache/ .ruff_cache/
*.tsbuildinfo
Vite/Metro/Gradle/CMake caches and emulator state
dist/ build/ android/app/build/ and untracked intermediates
.playwright-cli/
raw request/response logs and personal user exports
unlicensed, incomplete or future-dated data
the historical revoked provider credential or any recoverable representation
```

Do not exclude source merely because its filename contains `key`, `token` or `secret`; for example, `src/scripts/verify/secret-scan.mjs` is required source. Review file contents and known credential locations instead. Run the repository scanner against the final archive and inspect the archive file list before transfer.

## Environment injection on Linux

Secrets and deployment-only values belong to a secret manager or protected process environment. They must never be placed in the archive or passed to a client build. The production preflight currently expects these classes of inputs:

| Class | Required shape | Owner |
| --- | --- | --- |
| Runtime | `FORESIGHTNINE_ENV=production` | deployment |
| LLM | supported `FORESIGHTNINE_LLM_PROVIDER` plus its server-side credential | AI/deployment |
| Search | supported `FORESIGHTNINE_SEARCH_PROVIDER` plus its server-side credential | AI/deployment |
| Database | `FORESIGHTNINE_STORAGE_MODE=postgres` and `FORESIGHTNINE_POSTGRES_URL` or `DATABASE_URL` | operations |
| Abuse control | `FORESIGHTNINE_ABUSE_MODE=redis` and TLS `rediss://` URL | operations/security |
| Edge/IP trust | `FORESIGHTNINE_PROXY_MODE=direct` with no trusted CIDRs, or `reverse_proxy` with actual `FORESIGHTNINE_TRUSTED_PROXY_CIDRS` | operations/security |
| CAPTCHA | external provider declaration and secret | security/operations |
| Web origin | HTTPS-only `FORESIGHTNINE_CORS_ORIGINS` | Web/deployment |
| Android release | repository-external signed `.apk`/`.aab` path | mobile/release |
| Approval | external JSON approval manifest with all required booleans true | release owner |

See [`../../docs/security/production-preflight.md`](../../docs/security/production-preflight.md) for the exact variable names and rejection rules. Do not invent aliases in deployment scripts.

## Transfer acceptance checklist

- [ ] Source commit and archive/clone SHA-256 are recorded.
- [ ] The writable destination is separate from read-only research sources.
- [ ] `corepack pnpm install --frozen-lockfile` completes.
- [ ] `uv sync --project apps/api --frozen` completes.
- [ ] Reference hashes and read-only boundaries pass on the Windows checkout, or a reviewed Linux compatibility harness records an equivalent mapping.
- [ ] Generated contracts and registry pass drift checks.
- [ ] Typecheck, full tests, 9WL audit and secret scan pass.
- [ ] Docker image build includes the asset registry and migrations are present.
- [ ] No secret, `.env`, cache, debug build or unlicensed data entered the archive.
- [ ] Any production variables are injected outside source control and outside the client bundles.
- [ ] Live provider/search, PostgreSQL, Redis, CAPTCHA, Android release and approval evidence are recorded separately before public traffic.

## Suggested archive layout

If a file transfer rather than a fresh clone is necessary, use a structure like this:

```text
foresightnine-transfer/
  src/                         # Tier A + Tier B
  manifests/
    source-sha256.txt
    data-manifest.example.json # placeholder only; no secret/value
  evidence/
    local-verification.txt
  README-transfer.txt
```

The `manifests` and `evidence` files must contain metadata and redacted results only. They must not contain provider keys, raw user viewpoints, raw response payloads, or credentials.
