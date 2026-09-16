# ForesightNine production release runbook

This runbook is an execution checklist for an approved deployment owner. It
does not contain credentials and does not authorize public traffic. The
current repository is a QA candidate; the commands below must run in a
deployment workspace with an external secret manager, licensed market-data
configuration, and written release approvals.

## 1. Pin the release

From `D:\Foresightnine\src`:

```powershell
git fetch origin
git checkout --detach <approved-commit>
corepack pnpm install --frozen-lockfile
uv sync --project apps/api --frozen
corepack pnpm verify:boundaries
corepack pnpm verify:generated-contracts
corepack pnpm verify:reference-hashes
corepack pnpm test
corepack pnpm scan:secrets
corepack pnpm audit --prod
```

The deployment record must retain the exact commit SHA and the output of each
gate. Never put the provider credential in a command line, repository file,
Web bundle, Android bundle, source map, log, fixture, or database row.

## 2. Inject production configuration

Inject these values only into the server/build process through the approved
secret/configuration manager:

```text
FORESIGHTNINE_ENV=production
FORESIGHTNINE_LLM_PROVIDER=glm_coding
FORESIGHTNINE_GLM_CODING_API_KEY=<secret-manager-reference>
FORESIGHTNINE_SEARCH_PROVIDER=glm_coding_mcp
FORESIGHTNINE_STORAGE_MODE=postgres
FORESIGHTNINE_POSTGRES_URL=<tls-or-provider-managed-postgres-url>
FORESIGHTNINE_REDIS_URL=rediss://<managed-redis-host>:<port>/<db>
FORESIGHTNINE_ABUSE_MODE=redis
FORESIGHTNINE_PROXY_MODE=reverse_proxy
FORESIGHTNINE_TRUSTED_PROXY_CIDRS=<actual-ingress-proxy-cidrs>
FORESIGHTNINE_CAPTCHA_PROVIDER=<approved-external-provider>
FORESIGHTNINE_CAPTCHA_SECRET=<secret-manager-reference>
FORESIGHTNINE_CAPTCHA_HOSTNAME=<approved-web-hostname>
FORESIGHTNINE_CORS_ORIGINS=https://<approved-web-origin>
```

If the API directly sees clients instead of sitting behind a reverse proxy,
set `FORESIGHTNINE_PROXY_MODE=direct` and leave
`FORESIGHTNINE_TRUSTED_PROXY_CIDRS` empty. For a reverse proxy, use
`reverse_proxy` and provide only the actual ingress proxy networks. The
release owner must verify the direct peer and forwarded-header path in
staging; never use a broad placeholder network or a user-controlled value.

The GLM profile is fixed to the official Coding Plan endpoint and `glm-5.3-flash`.
It uses the server-owned thinking configuration and does not expose or send
user-selectable `temperature` or `top_p`. The MCP search profile uses the
fixed Streamable HTTP endpoint and exact tool name returned by `tools/list`.
The generic OpenAI-compatible provider remains a separate, explicitly
allowlisted future/BYOK path and must not be substituted silently.

## 3. Run configuration and provider smoke

Provide an approval manifest outside the repository with all required legal,
privacy, terms, risk, data-license, security, operations, rollback, Android
signing, and artifact-provenance approvals set to true. Also provide an
owner-controlled, non-debug Android `.apk` or `.aab` outside the repository.
Then run:

```powershell
corepack pnpm verify:production-preflight -- --approval-manifest <external-manifest-path> --provider-admission-manifest <external-provider-manifest-path>
uv run --project apps/api python apps/api/scripts/glm_coding_live_smoke.py
uv run --project apps/api python apps/api/scripts/glm_mcp_live_smoke.py
```

Both live smokes must pass using the default transport. A custom transport or
the current Windows reserved-DNS mapping is diagnostic evidence only and does
not satisfy this gate. Preserve only redacted status, protocol, request-id
(if approved), and validation metadata.

## 4. Build and stage

```powershell
corepack pnpm --filter @foresightnine/web build
corepack pnpm verify:release-candidate
corepack pnpm --filter @foresightnine/mobile export:android
Set-Location apps/mobile/android
.\gradlew.bat assembleRelease --init-script ..\..\..\scripts\android-short-cmake.init.gradle '-Dforesightnine.cxx.root=C:/fn-cxx'
```

Install the signed artifact in the API-36 Play emulator and an owner-approved
physical device. Verify the complete flow, language switching, share import,
client-side FM Worker/engine, Raw/Public Default separation, stale-data
disablement, cancellation/recovery, and absence of broker/order/custody
behavior. Use Browser/IAB and `view_image` for the Web visual QA; use the
Android QA workflow for UI-tree, screenshot, and logcat evidence.

## 5. Staging, rollback, and traffic

Before public traffic:

1. Apply migrations through the approved change process and verify PostgreSQL
   restore readiness.
2. Verify Redis TLS/authentication, TTLs, quota keys, CAPTCHA enforcement,
   rate-limit denial, provider failure, and fail-closed behavior.
3. Run a staging end-to-end research with non-private synthetic evidence and
   inspect redacted logs for keys, raw market shards, and personal data.
4. Exercise cancellation, replay, rollback to the pinned prior commit, and
   re-run client FM parity after rollback.
5. Record data-provider freshness, license scope, method version, artifact
   hashes, monitoring owner, kill switch, incident contact, and rollback SHA.
6. Obtain written release-owner approval before changing traffic exposure.

Broker connectivity, order routing, custody, and real-money execution remain
out of scope. If any gate is missing or uncertain, keep the service private or
disabled rather than presenting historical/simulated data as a current
executable allocation.
