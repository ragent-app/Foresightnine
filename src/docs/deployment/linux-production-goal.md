# Linux agent goal — production API, persistence and Cloudflare

Status: **goal specification only; do not execute until the owner explicitly
dispatches this file.**

This file is the Linux half of the next ForesightNine milestone. It is not a
deployment authorization and it does not replace the approved `plan.md` or
`agent.md`.

## Required reading

Before any future execution, read completely:

```text
D:\Foresightnine\plan.md
D:\Foresightnine\agent.md
D:\Foresightnine\src\README.md
D:\Foresightnine\src\docs\progress.md
D:\Foresightnine\src\docs\architecture\README.md
D:\Foresightnine\src\docs\security\production-preflight.md
D:\Foresightnine\src\docs\deployment\linux-transfer-manifest.md
D:\Foresightnine\src\docs\deployment\linux-agent-handoff.md
D:\Foresightnine\src\infra\deployment\README.md
D:\Foresightnine\src\infra\deployment\production-release-runbook.md
```

The original research and FM reference roots are read-only. Do not edit or
run write-producing jobs in:

```text
D:\FM acceleration
D:\FM acceleration-deepseek
D:\WL-data-final
D:\ACFM_WL_paperwriting
/home/lxx/trade-agent-crisis
/home/lxx/trade-agent-benchmark
```

## Goal

Prepare and, only after explicit dispatch, deploy the ForesightNine FastAPI
backend as a persistent Linux staging/production candidate. The service must
support the existing viewpoint → challenge → evidence → worldline → client FM
flow while keeping PostgreSQL and Redis durable/private and keeping all final
FM portfolio computation on Web/Android clients.

The target topology is:

```text
Cloudflare DNS/TLS/WAF/CDN
        │
        ├── Cloudflare Pages: Vite static Web artifact
        │
        └── api.<DOMAIN>: Cloudflare Tunnel or strict reverse proxy
                         │
                   Linux FastAPI
                    │          │
              private PostgreSQL  private TLS Redis
```

Use `<DOMAIN>` until the owner supplies the actual purchased Cloudflare
domain. Never guess a hostname, add an unapproved CORS origin, or access the
Cloudflare account without explicit authorization.

## Scope and ownership

The Linux agent owns only:

- `src/apps/api` server implementation and tests;
- `src/infra` Docker, migration and deployment configuration;
- minimal server-side additions to `src/packages/contracts` for typed auth,
  session or entitlement boundaries, if genuinely required;
- staging/production runbooks and redacted deployment evidence.

The Windows agent owns Web/Android client code, Console UI, Android QA and Play
Store evidence. Do not edit `src/apps/web`, `src/apps/mobile`,
`src/packages/design-system`, `src/packages/fm-engine`, `src/README.md` or
`src/docs/progress.md` during the Linux task. Return changes to the primary
integrator for documentation and commit coordination.

## Required implementation outcome

1. **Persistent API runtime**

   - Run FastAPI under a supervised persistent service or container restart
     policy, with bounded timeouts, health/readiness checks, resource limits,
     redacted logs and a tested rollback target.
   - Keep development mock mode available for local/staging tests. Production
     mode must fail closed when live provider, storage, abuse-control,
     CAPTCHA, CORS or approval inputs are missing.

2. **PostgreSQL**

   - Apply the checked-in migrations through an approved change process.
   - Keep the database on a private network/volume; never expose its port to
     the public Internet.
   - Prove restart persistence, backup, restore, migration idempotency and
     expiry/cascade behavior for research entities and operation state.

3. **Redis abuse control**

   - Use authenticated TLS Redis (`rediss://`) in production, with bounded
     TTLs, HA/failover/eviction policy, alerting and an explicit unavailable
     behavior.
   - Set `FORESIGHTNINE_ABUSE_MODE=redis`; never silently fall back to memory.
   - Set `FORESIGHTNINE_PROXY_MODE=direct` with empty
     `FORESIGHTNINE_TRUSTED_PROXY_CIDRS`, or `reverse_proxy` with the actual
     ingress CIDRs. Never accept a universal `/0` trust network.

4. **Cloudflare and domain**

   - After the owner confirms `<DOMAIN>`, configure DNS/TLS and preferably
     Cloudflare Pages for the Vite static Web artifact.
   - Expose `api.<DOMAIN>` through Cloudflare Tunnel or a hardened reverse
     proxy. Keep origin/admin/database ports private where possible.
   - Use Full (strict) TLS, HTTPS-only CORS, security headers, WAF/rate
     controls, health checks, origin restrictions and redacted access logs.
   - Record DNS/TLS/health evidence without committing account tokens. Do not
     change public DNS or traffic until the release owner approves it.

5. **AI/search provider**

   - Use the existing openai-compatible server adapter and official GLM Coding
     Plan endpoint only through server-side environment/secret-manager
     injection. The test key supplied in chat is sensitive input; never copy
     it into files, prompts, logs, fixtures, Git history or the client.
   - Keep a future OpenAI-compatible BYOK/subscription adapter behind an
     explicit allowlist and typed configuration; do not expose provider keys
     to the browser or APK.
   - Do not add or transmit `temperature` or `top_p`.
   - Prefer provider-native search. If unavailable, accept the typed client
     `SearchEvidenceBundle` with user-supplied provenance. Validate bounded
     JSON, HTTPS candidates, size, quota and retryability.

6. **Google/GitHub server-side auth boundary**

   - If the existing contract is ready, implement server-side OAuth callback,
     state/PKCE, exact redirect allowlists, secure session issuance, logout,
     account deletion and provider error handling.
   - Keep OAuth client secrets server-only. Do not guess redirect URLs or use
     a production client ID without owner-provided values.
   - If credentials, consent-screen approval or confirmed domain are absent,
     implement only mock/contract tests and record the blocker.

7. **Subscription entitlement boundary**

   - Keep the first public release free unless the owner explicitly chooses a
     paid MVP.
   - If paid access is selected, make the server authoritative for Google Play
     purchase-token verification, entitlement, restore, cancellation, refund
     and expiration. Never trust a client boolean.

## Required evidence when this goal is later dispatched

- immutable source commit and Linux install output;
- portable test/typecheck/contract/secret/audit results; Windows-only absolute
  path gates must remain Windows evidence unless a reviewed compatibility
  harness is added;
- PostgreSQL migration, restart, backup and restore evidence;
- Redis TLS, quota, TTL, failure and no-memory-fallback evidence;
- explicit proxy-topology and CORS/TLS preflight output;
- provider/search smoke output with only redacted metadata;
- OAuth contract/live callback evidence or an explicit credential blocker;
- Cloudflare DNS/TLS/Pages/Tunnel evidence for the owner-confirmed domain;
- staging rollback and incident/secret-rotation runbook;
- no public traffic before legal, privacy, security, data-license, operations,
  artifact-provenance and release approval.

## Stop conditions

Stop the dependent part of this goal and report evidence when any of these is
missing: Linux host access, writable deployment directory, confirmed domain,
Cloudflare permission, production database/Redis, new provider secret,
OAuth/CAPTCHA configuration, licensed current data, or written release
approval. Continue no implementation after the owner says “do not execute”.

The Linux agent must return a concise handoff containing commit, changed files,
commands/counts, artifact hashes, provider mode, data cutoff, security result,
external blockers and the Sol review status. It must never include a secret or
raw user/provider payload.
