# ForesightNine two-platform release pipeline

Status: **planning and evidence synchronization only; no deployment is
authorized by this file.** The owner must dispatch the relevant goal before
any Linux, Cloudflare, OAuth, provider, Play or production mutation.

This is the single serial control bus for the two execution environments. The
Linux and Windows goal files contain the scoped instructions for each side:

- [Linux production goal](./linux-production-goal.md)
- [Windows client and Play goal](./windows-client-play-goal.md)

## Non-negotiable execution rules

1. Execute exactly one numbered module at a time.
2. The next module starts only after the current module has evidence and a Sol
   reviewer pass.
3. `WIN` and `LINUX` are execution labels, not permanent Git branches.
4. Windows tests and Linux deployments must use the same immutable commit.
5. No production DNS, traffic cutover, OAuth activation, provider secret,
   billing activation or Play release is implied by this planning document.
6. Never guess a domain, origin, callback, package identity, secret or data
   license. Record the blocker and stop only the dependent module.
7. The client data plane is Windows-owned: only the three bounded market
   anchors are requested, validated and converted into local FM inputs. Linux
   provides deployment, identity/database/abuse services and, if approved, a
   provider contract; it does not compute FM or replace client data with a
   historical warehouse.

## Confirmed decisions and Cloudflare evidence

Recorded on 2026-08-30:

- Cloudflare zone confirmed: `foresightnine.com`.
- Cloudflare dashboard showed `DNS Setup: Full`.
- DNS record inventory was empty; `www.foresightnine.com` was not reachable.
- Workers & Pages had no existing project.
- Three existing Tunnel objects were visible, all `Down`, with no usable
  routes.
- First public release: free Web + free Android.
- Console may expose a planned subscription entry, but paid entitlement is
  not part of the first release.
- Google Play developer-account billing status does not make the first app
  version paid.
- Repository policy: one protected `main`, short-lived feature/release
  branches, no permanent `win` or `linux` branches.

These are observations and product decisions, not permission to modify
Cloudflare. The proposed hostnames are:

```text
staging.foresightnine.com       Web staging
api-staging.foresightnine.com   API staging
app.foresightnine.com           Web production
api.foresightnine.com           API production
```

The exact hostnames, Linux origin port, Tunnel name, OAuth callbacks and
Cloudflare routing mode must be confirmed by the execution owner before any
record is created.

## Current phase status

### Module 0 — `[WIN]` input and release freeze

Status: **partially complete; not passed**.

Completed:

- purchased domain identified;
- free-first release decision recorded;
- future subscription direction recorded;
- sequential WIN/LINUX ownership agreed;
- Cloudflare inventory captured read-only.

Still required before this module can pass:

- exact final hostname list approved;
- Linux host access and writable deployment path;
- API origin bind address/port;
- Google/GitHub OAuth application inputs;
- Android package/signing identity and Play permissions;
- privacy policy, terms, support URL and data-safety owner;
- staging/production approval owner.

Cloudflare is **not** configured in this module. DNS records cannot safely be
created before the API origin or Cloudflare Tunnel route exists. Pages cannot
be safely bound before the Web artifact and build settings are fixed.

## Strict serial pipeline

| # | Environment | Module | Pass condition before next module |
|---:|---|---|---|
| 0 | WIN | Freeze source SHA, domain/hostnames, free-first policy, OAuth, Android and Play inputs | No required external value is guessed |
| 1 | LINUX | Create writable Linux workspace and read-only source/data boundaries | Pinned SHA, paths and rollback boundary recorded |
| 2 | LINUX | Provision private PostgreSQL and migrations | Restart, migration, backup and restore evidence passes |
| 3 | LINUX | Provision private authenticated Redis and abuse controls | TLS/auth, TTL, quota, failure and no-memory-fallback evidence passes |
| 4 | LINUX | Run supervised FastAPI staging with mock provider | Health/readiness and mock viewpoint-to-worldline flow pass |
| 5 | LINUX | Configure Cloudflare API staging with `cloudflared` | `api-staging` HTTPS route, TLS, CORS, WAF and health checks pass |
| 6 | LINUX | Publish auth/session and server contract boundary | Contract tests and callback boundary are stable |
| 7 | WIN | Configure mock/staging/production client API bases | Both clients connect to staging without secrets |
| 8 | WIN | Implement approved Console and Web/phone/tablet layouts | Three form factors pass state/design review |
| 9 | WIN | Browser/IAB and Android API 36 staging QA | Core user journey and failure states have evidence |
| 10 | LINUX | Enable real provider/search through server secrets | Redacted provider/search smoke passes or blocker is recorded |
| 11 | LINUX | Configure real Google/GitHub OAuth server integration | State/PKCE, callback, session, replay and expiry pass |
| 12 | WIN | Complete Web OAuth and Android deep-link QA | Cancel, wrong-account, logout and deletion cases pass |
| 13 | WIN | FM parity and worldline/market/portfolio scenario review | Golden, IC/ICIR, cost, dead-band and Public Default checks pass |
| 14 | LINUX | Production data, security, monitoring and rollback hardening | Legal/privacy/security/data/operations approvals pass |
| 15 | WIN | Build signed non-debug Android AAB and listing materials | Provenance/hash and phone/tablet release QA pass |
| 16 | WIN | Google Play internal testing | Install, upgrade, login, sharing and disclosure tests pass |
| 17 | LINUX | Deploy Pages Web artifact and production API route | Written release approval, health and rollback checks pass |
| 18 | WIN | Final production Web/Android verification | Sol final review passes with no release blocker |
| 19 | WIN | Submit Play release | Production submission is accepted by owner |
| 20 | LINUX | Operations handoff and first drills | Backup, rollback, rotation and incident drills pass |

## Cloudflare module decision

The browser has already confirmed the account and zone, but actual
configuration is deferred to module 5 on Linux. This is intentional because
the browser is not the right place to guess or store the Linux origin and
because `cloudflared` must run persistently beside the API. Module 5 should:

1. install or verify `cloudflared` on Linux;
2. create or select a dedicated API Tunnel;
3. bind the Tunnel to the verified Linux FastAPI localhost address;
4. create only the approved `api-staging.foresightnine.com` route;
5. verify HTTPS, origin health, CORS, trusted proxy CIDRs and logs;
6. record the Tunnel UUID and redacted route evidence, never the token;
7. defer `app.foresightnine.com`/Pages production binding until module 17.

Illustrative commands for the later Linux agent (placeholders only; do not
run from this planning turn):

```bash
cloudflared tunnel login
cloudflared tunnel create foresightnine-api-staging
cloudflared tunnel route dns foresightnine-api-staging api-staging.foresightnine.com
cloudflared tunnel --config /etc/cloudflared/foresightnine-api-staging.yml run
```

The Linux agent must replace placeholders with an approved tunnel, origin and
secret injection method, then run the existing production preflight. It must
not create a public DNS record pointing at an unknown IP or expose PostgreSQL
or Redis.

## Synchronization record

Each completed module must append or update a redacted record containing:

- module number and environment;
- source commit SHA;
- changed files and commands;
- test counts and artifact hashes;
- external dependencies and blockers;
- Sol reviewer verdict;
- exact next module.

The primary integrator should update this file only after the responsible
agent returns evidence. This file does not replace `plan.md`, `agent.md` or
the approved phase gates.
