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

The owner has confirmed the purchased Cloudflare zone as
`foresightnine.com`. The browser-only preflight on 2026-08-30 found
`DNS Setup: Full`, zero DNS records, no Workers & Pages projects, and three
existing Tunnel objects that are all `Down` with no usable route. Treat these
as observed facts, not as authorization to change Cloudflare. The execution
agent must still confirm the exact final hostnames before adding CORS or OAuth
redirects:

```text
staging.foresightnine.com       Web staging
api-staging.foresightnine.com   API staging
app.foresightnine.com           Web production
api.foresightnine.com           API production
```

Never guess a hostname, add an unapproved CORS origin, or access/change the
Cloudflare account without explicit execution dispatch.

## Strict serial execution plan

The project-level control bus is
[twopaltformpipeline.md](./twopaltformpipeline.md). It is the authoritative
serial order and synchronization record; this file defines the Linux scope
and evidence requirements for the same modules.

This plan is intentionally sequential. Even when two tasks are technically
independent, the next numbered module must not start until the previous module
has produced its evidence and Sol reviewer has passed its gate. `WIN` and
`LINUX` identify the execution environment, not long-lived Git branches.

1. `[WIN]` Freeze the source commit, exact domain/hostname list, free-first
   release decision, OAuth application inputs, Android identity and Play
   permissions. Gate: the external-input checklist is complete enough to
   create staging without guessing.
2. `[LINUX]` Clone the pinned commit into a writable Linux deployment
   workspace, preserve read-only research mounts, and establish artifact,
     secret and rollback boundaries. Gate: install path and commit SHA are
     recorded.
3. `[LINUX]` Provision private PostgreSQL, apply migrations, and prove
   restart persistence, backup, restore, expiry and cascade behavior. Gate:
     the persistence evidence is complete.
4. `[LINUX]` Provision authenticated private Redis, quotas, TTLs, failure
   behavior and explicit proxy topology/CIDRs. Gate: no-memory-fallback and
     Redis failure tests pass.
5. `[LINUX]` Run FastAPI as a supervised staging service with health/readiness,
     bounded resources, redacted logs, mock provider and rollback target. Gate:
     the mock viewpoint-to-worldline API flow passes.
6. `[LINUX]` Configure Cloudflare staging only after the owner approves the
     observed zone and hostnames: Pages for static Web and Tunnel or a strict
   reverse proxy for API. Gate: HTTPS staging Web/API reachability, TLS,
     CORS, WAF and health checks pass.
7. `[LINUX]` Publish the typed server auth/session boundary and contract tests
     for Google/GitHub OAuth, logout and account deletion. Gate: generated
     contract is stable for the client.
8. `[WIN]` Point Web and Android build-time configuration at Linux staging;
     keep mock mode and all provider/database secrets out of client artifacts.
     Gate: both clients connect to staging on the pinned commit.
9. `[WIN]` Implement the approved Console, Web states, Android phone layout
     and Android tablet layout. Gate: design and state QA pass separately for
     Web, phone and tablet.
10. `[WIN]` Run Browser/IAB Web staging QA and API 36 Play emulator QA for the
    complete viewpoint-to-portfolio flow, including language switching and
    error/retry states. Gate: core-flow evidence is recorded.
11. `[LINUX]` Inject the real OpenAI-compatible provider/search configuration
    through server secrets, test timeout/quota/fallback behavior, and keep
    provider keys server-only. Gate: redacted provider/search smoke passes or
    the missing-secret blocker is recorded.
12. `[LINUX]` Configure real OAuth server credentials and exact callbacks only
    after the domain and redirect list are approved. Gate: server callback,
    state/PKCE, session, replay and expiry tests pass.
13. `[WIN]` Complete Web OAuth and Android deep-link integration against
    staging. Gate: cancel, wrong-account, replay, expiry, logout, deletion
    and provider-failure cases pass.
14. `[WIN]` Run real client FM parity and scenario review; Sol challenges event
    dates, probabilities, market paths and portfolio paths. Gate: FM golden,
    cost and Public Default evidence passes.
15. `[LINUX]` Apply production data freshness/licensing policy, monitoring,
    secret rotation, backups, incident response and production preflight. Gate:
    legal/privacy/security/data-license/operations approvals are explicit.
16. `[WIN]` Build the signed non-debug Android AAB, record provenance/hash,
    prepare privacy/data-safety/support/store materials and validate phone and
    tablet release behavior. Gate: release candidate is installable.
17. `[WIN]` Upload to Google Play internal testing and execute install,
    upgrade, login, sharing, offline and account tests. Gate: internal-test
    evidence passes. The first release remains free; subscription is a future
    entitlement milestone.
18. `[LINUX]` Deploy the production Pages/API configuration and cut traffic
    only after written release approval. Gate: health, rollback and monitoring
    evidence pass.
19. `[WIN]` Run final production Browser/Android QA, then submit the approved
    Android release to Play. Gate: Sol final review passes.
20. `[LINUX]` Receive the operations handoff and run the first backup,
    rollback, secret-rotation and incident drills. Gate: production ownership
    and runbooks are accepted.

### Release policy

The first public product is **free Web + free Android**. The Console may show
that subscription is planned, but no paid entitlement is required for the
first release. Google Play may retain the existing paid-developer account;
that account status does not make the app paid. When subscriptions are later
activated, Google Play purchase-token verification and entitlement state must
be authoritative on Linux before any paid feature is enabled.

### Repository and handoff discipline

Use one protected `main` line plus short-lived `feature/*` and optional
`release/*` branches. Deploy the exact commit tested on Windows. Record every
module's commit, evidence and Sol verdict before starting the next module.
Use separate GitHub `staging` and `production` environments for protected
secrets and approvals; do not use `win`/`linux` as persistent branches.

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
