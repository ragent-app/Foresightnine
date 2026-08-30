# Linux AI-agent handoff

This is the operating brief for an AI agent continuing ForesightNine on Linux. It should be supplied together with the pinned source checkout. It does not grant permission to expose a service or change the approved plan.

## Mission

Bring the existing ForesightNine Web, Android and FastAPI implementation from local/private QA toward a reproducible Linux staging release. Preserve the contract-first architecture, the client-only FM boundary, the read-only research sources and the fail-closed production gates. Make evidence-backed progress; do not report a scaffold or a mock smoke as a production release.

## Required reading before any change

Read completely:

```text
plan.md
agent.md
src/README.md
src/docs/progress.md
src/docs/architecture/README.md
src/docs/architecture/ADR-006-fm-engine.md
src/docs/security/production-preflight.md
src/docs/security/android-release-signing.md
src/docs/deployment/linux-transfer-manifest.md
src/infra/deployment/README.md
src/infra/deployment/production-release-runbook.md
```

Also inspect the exact relevant source before changing it. The following remain read-only references and must be hash-verified, never edited:

```text
D:\FM acceleration-deepseek\bundle\agent-framework\scheduler\fm_walk_forward.py
D:\FM acceleration-deepseek\bundle\agent-framework\FactorMiner\factorminer\evaluation\combination.py
D:\FM acceleration-deepseek\bundle\agent-framework\FactorMiner\factorminer\evaluation\portfolio.py
D:\FM acceleration-deepseek\bundle\agent-framework\FactorMiner\factorminer\evaluation\metrics.py
D:\FM acceleration-deepseek\bundle\agent-framework\FactorMiner\factorminer\evaluation\backtest.py
D:\FM acceleration-deepseek\bundle\agent-framework\ASSETS.yaml
```

On Linux, use the approved read-only mount or its separately verified copy. Do not assume a Windows path is present, and do not silently replace a missing reference with a newly invented formula.

## Workspace and write policy

- Work in a new writable application checkout such as `/home/lxx/foresightnine`.
- Treat `/home/lxx/trade-agent-crisis`, `/home/lxx/trade-agent-benchmark` and any benchmark/paper/data source mounts as read-only.
- Only edit the application source under the Linux equivalent of `foresightnine/src`.
- Do not edit `plan.md` or `agent.md` for ordinary implementation choices, refactors, missing credentials, or local tool differences.
- If an approved requirement is objectively impossible, first record the command, environment, error and impact in `src/docs/progress.md`; then ask Sol to review before the smallest plan change. Do not change the plan pre-emptively.
- Preserve unrelated user changes. Never use destructive reset, checkout, clean, recursive delete, or broad overwrite commands.

## Provider and evidence policy

- Use deterministic mock providers for development and tests until deployment-owned live credentials exist.
- Live credentials are injected only by the Linux secret manager or protected process environment. Never print, commit, archive, log, persist or send them to Web/Android.
- Use the configured official provider endpoints and exact tool names. The current MCP adapter calls `web_search_prime`; `webSearchPrime` is only a fixture compatibility alias.
- Keep provider calls bounded by timeout, concurrency, size and daily model-call quota. Reserve quota before external search. Treat provider output as untrusted and validate it against canonical v1 contracts.
- Prefer provider-native search. If it is unavailable, accept a typed `SearchEvidenceBundle`, preserve its user-supplied provenance, and do not label it independently verified.
- Do not add `temperature`, `top_p`, hidden fallback models or undocumented provider parameters. Model/reasoning settings belong to the supported server adapter contract and must be recorded as method metadata.
- A local DNS mapping or SSRF rejection is a safe failure. Never weaken private-address, HTTPS, size or candidate validation to make a smoke pass.

## FM policy

- FM remains on the user device. Web uses the Worker adapter; Android uses the shared TypeScript package.
- FastAPI may deliver validated manifests, shards, method versions and data timestamps, but must not calculate or replace the user's final portfolio.
- Preserve the exact approved reference subset: cutoff-visible windows, stable factor selection and ties, NaN omission semantics, IC/ICIR rules, composite signal, long-only weights, equal-weight fallback, fixed-point allocation, 3 bps fee and strict cost dead-band.
- Reject future observations. Every observation and forward-return row must be no later than the calculation cutoff.
- Keep `FM Raw` and `Public Default` as separate outputs with a visible derivation and constraints explanation.
- Run the independent Python oracle only as an audit. Do not import the TypeScript implementation into the oracle or use server computation as a substitute for client parity.

## Client market-data boundary

The current Windows implementation owns the market-data request and FM data
plane. Linux must not recreate it as a server-side portfolio service:

- Web and Android request only the bounded year-start, latest-month-start and
  latest-completed-session anchors for the user-selected assets.
- The clients validate asset identity, cutoff, timestamps, missing/stale rows,
  prices, payload size and integrity, then map the latest completed-session
  price into the local asset registry.
- The clients convert the bundle to FM observations and perform factor
  evaluation, rank/tie/NaN handling, IC/ICIR metadata use, signal combination,
  weights, cost dead-band, amounts and shares. Web uses a Worker and Android
  uses the shared TypeScript engine.
- Linux may host an approved HTTPS provider endpoint, signed asset/factor
  metadata, OAuth, PostgreSQL and abuse controls. It must not add a raw
  historical-data pull, compute a replacement portfolio, or silently fill a
  missing/stale client row with mock data.
- No Windows path such as `D:/WL-data-final/...` is a Linux runtime
  dependency. That path belongs to the research/QA fixture audit and is not
  part of the three-anchor product request. If a Linux parity audit is later
  required, use a separately packaged, cutoff-labelled, hash-verified fixture
  bundle; do not substitute a new dataset or modify the source directory.

## Phase execution protocol

1. Inspect the current commit, working tree and read-only boundary. Record the environment without secrets.
2. Install from lockfiles; do not copy `node_modules` or a Python virtual environment. The current reference-hash and boundary scripts contain Windows absolute paths and a Windows implementation-root assertion. Run those two gates on the original Windows checkout, or use a separately reviewed Linux compatibility harness with explicit path mapping; never edit the baseline manifests or weaken the check just to obtain a Linux pass.
3. Run deterministic checks before changes:

   ```bash
   corepack pnpm install --frozen-lockfile
   uv sync --project apps/api --frozen
   # Portable Linux gates:
   corepack pnpm scan:secrets
   corepack pnpm verify:generated-contracts
   corepack pnpm typecheck
   corepack pnpm test
   corepack pnpm audit:worldline-fixtures
   ```

   Record `verify:reference-hashes` and `verify:boundaries` as Windows-checkout
   evidence unless a reviewed Linux compatibility harness exists. A failed
   Windows-path gate on an unmodified Linux checkout is a portability
   limitation, not permission to change the governance baseline.

4. Implement only the next approved phase slice. Keep source, tests and documentation in `src`.
5. Re-run focused tests and the relevant phase gate, then run the full release-candidate verifier when a release boundary is touched.
6. Update `src/docs/progress.md` with date, commit, command, result, limitation and any external dependency. Redact all secrets and raw user/provider content.
7. Ask Sol reviewer for a fresh, fix-first review. Terra/Luna implements the approved fixes. Do not advance phases on an unreviewed failure.
8. Commit the slice with a descriptive message. Public traffic requires a separate written release approval.

## Production dependencies that must come from deployment owners

The Linux agent may prepare integration code and mocks, but must not invent these values:

- an HTTPS Web host, API host, DNS/TLS and deployment identity;
- PostgreSQL with migration, backup/restore and alert evidence;
- authenticated TLS Redis (`rediss://`) with HA, TTL, eviction and failure policy;
- an explicit edge topology: `FORESIGHTNINE_PROXY_MODE=direct` with no trusted
  CIDRs, or `reverse_proxy` with the actual proxy networks in
  `FORESIGHTNINE_TRUSTED_PROXY_CIDRS`; never trust a user-controlled forwarded
  header or copy a broad placeholder network;
- newly issued server-side LLM and search credentials;
- external CAPTCHA and approved IP/session/model quotas;
- licensed current market/factor data and signed freshness/redistribution manifest;
- Android owner-controlled release keystore, EAS/Play credentials and non-debug `.apk`/`.aab`;
- legal, privacy, terms, risk, data-license, security, operations, rollback and artifact-provenance approvals.

When any of these is missing, keep the local/mock path available and record the public gate as blocked. Do not mark the repository production-ready.

## Minimum handoff report

Every Linux-agent handoff should report:

```text
commit:
working_tree:
source_boundary:
commands_run:
tests_and_counts:
artifacts_and_sha256:
provider_mode: mock | live (never include credential)
data_manifest_and_cutoff:
FM_parity_result:
security_scan_result:
external_gates_missing:
Sol_review_status:
next_safe_step:
```

Never include a secret, token, cookie, raw endpoint query string, raw user viewpoint, raw provider receipt, or unredacted request/response body in this report.

## Definition of done for Linux staging

Linux staging is not complete until the agent has an immutable source commit, reproducible installs, passing boundary/hash/generated/type/test/FM checks, a containerized API build with migrations and asset registry, redacted provider/search smoke evidence from the real network, PostgreSQL/Redis/CAPTCHA runtime evidence, a Web staging build verified in Browser/IAB, a signed Android artifact verified on a device/emulator, a rollback rehearsal, and the external approval manifest. Until then the correct status is local/private QA or staging candidate.
