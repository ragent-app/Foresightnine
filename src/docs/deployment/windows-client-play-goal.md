# Windows agent goal — Web, Android, OAuth and Google Play

Status: **goal specification only; do not execute until the owner explicitly
dispatches this file.**

This file is the Windows/client half of the next ForesightNine milestone. It is
not a release authorization and it does not replace the approved `plan.md` or
`agent.md`.

## Required reading

Before any future execution, read completely:

```text
D:\Foresightnine\plan.md
D:\Foresightnine\agent.md
D:\Foresightnine\src\README.md
D:\Foresightnine\src\docs\progress.md
D:\Foresightnine\src\docs\design\concepts\
D:\Foresightnine\src\docs\deployment\linux-transfer-manifest.md
D:\Foresightnine\src\docs\deployment\linux-agent-handoff.md
D:\Foresightnine\src\docs\security\android-release-signing.md
D:\Foresightnine\src\docs\qa\
```

Only write under `D:\Foresightnine\src`. Never modify these read-only roots:

```text
D:\FM acceleration
D:\FM acceleration-deepseek
D:\WL-data-final
D:\ACFM_WL_paperwriting
/home/lxx/trade-agent-crisis
/home/lxx/trade-agent-benchmark
```

## Goal

Prepare and, only after explicit dispatch, make the Web and Android PoC
consume the Linux staging API safely. Verify the complete user journey:

```text
viewpoint + capital
  → structured challenge
  → evidence/rubric confirmation
  → weak/main/strong worldlines
  → probabilities, impacts and reveal dates
  → client FM factor/weight/cost/amount/share calculation
  → FM Raw + Public Default + risk/stress/disclosure/export
```

The Web and Android clients must keep the FM calculation local. Web uses the
Web Worker; Android uses the shared TypeScript engine. The API may provide
validated data manifests/shards and method metadata, but it must not replace
the final client portfolio calculation.

## Confirmed release strategy and strict serial order

The project-level control bus is
[twopaltformpipeline.md](./twopaltformpipeline.md). It is the authoritative
serial order and synchronization record; this file defines the Windows/client
scope and evidence requirements for the same modules.

The first public release is planned as **free Web + free Android**, with a
subscription entry reserved in Console for a later entitlement release. The
owner's Google Play developer account being marked paid is an account/billing
capability fact; it does not require the first app version to charge users.

Cloudflare browser preflight on 2026-08-30 confirmed the zone
`foresightnine.com` with `DNS Setup: Full`. It currently has zero DNS records,
no Workers & Pages project, and the visible Tunnel inventory is all `Down`
with no usable routes. The client plan therefore consumes staging only after
Linux creates and verifies these approved hostnames:

```text
staging.foresightnine.com       Web staging
api-staging.foresightnine.com   API staging
app.foresightnine.com           Web production
api.foresightnine.com           API production
```

Execution is strictly serial; do not run the next module before the previous
module's evidence and Sol gate are complete. `WIN` and `LINUX` are execution
labels, not permanent Git branches.

1. `[WIN]` Freeze the pinned commit, exact Cloudflare hostnames, free-first
   product decision, OAuth inputs, Android package/signing identity and Play
   permissions. Gate: no client task depends on a guessed external value.
2. `[LINUX]` Create the writable Linux workspace, PostgreSQL, Redis and the
   supervised FastAPI staging service. Gate: persistence, abuse-control,
   health/readiness and mock API evidence passes.
3. `[LINUX]` Create Cloudflare staging Pages/API routes only after owner
   approval of the zone and hostnames. Gate: HTTPS Web/API staging is healthy
   with the required CORS/TLS/WAF behavior.
4. `[LINUX]` Publish the auth/session and provider/search contracts. Gate:
   client-consumable contracts and server contract tests are stable.
5. `[WIN]` Configure Web and Android for mock/staging/production build-time
   API bases. Gate: no provider, OAuth, database, billing or signing secret is
   present in source, source maps or artifacts.
6. `[WIN]` Implement the approved Console and separately adapt Web, Android
   phone and Android tablet states. Gate: the three form factors pass design
   state review; Console contains the future subscription entry and does not
   expose obsolete temperature/top-p controls.
7. `[WIN]` Test Web in Browser/IAB and Android on `api36_play:5554`, including
   keyboard, safe area, back navigation, share, loading, retry and error
   states. Gate: core-flow evidence is recorded.
8. `[LINUX]` Enable real server-side AI/search and OAuth credentials only after
   secret, domain and callback approvals. Gate: redacted provider and OAuth
   smoke evidence passes or a precise blocker is recorded.
9. `[WIN]` Run real Web OAuth and Android deep-link flows. Gate: cancellation,
   wrong account, replay, expiry, logout, deletion and provider failure pass.
10. `[WIN]` Run client FM parity and scenario QA with Sol review of event dates,
    probabilities, expected market paths and portfolio paths. Gate: golden
    parity, 3 bps cost, dead-band, rounding and Raw/Public Default checks pass.
11. `[LINUX]` Complete production hardening, current-data/licensing policy,
    monitoring, backup/restore, rollback and release preflight. Gate: all
    legal, privacy, security, data and operational approvals are explicit.
12. `[WIN]` Build and sign the non-debug AAB; record hash/provenance and
    prepare privacy policy, data-safety, support URL and Play listing assets.
    Gate: release candidate works on phone and tablet.
13. `[WIN]` Upload to Play internal testing and verify install, upgrade,
    account, sharing, offline and disclosure flows. Gate: internal testing is
    accepted; paid billing remains out of the first-release critical path.
14. `[LINUX]` Cut production traffic through the approved Cloudflare Pages/API
    setup after written release approval. Gate: health, monitoring and
    rollback checks pass.
15. `[WIN]` Test the production Web and Android clients, then submit the Play
    release after Sol's final pass. Gate: no unresolved client or production
    blocker remains.
16. `[LINUX]` Accept the operations handoff and complete the first production
    backup, rollback, secret-rotation and incident drills.

The client agent must not create a separate `win` branch or fork server
contracts. It consumes the exact Linux-published contract and tests the exact
commit that Linux deploys.

## Scope and ownership

The Windows agent owns only:

- `src/apps/web` and `src/apps/mobile`;
- client-facing use of `src/packages/design-system` and
  `src/packages/api-client`;
- build-time, non-secret API endpoint selection;
- Web Browser/IAB and Android emulator/device QA;
- Android release, Play Console and artifact evidence.

The Linux agent owns `src/apps/api`, `src/infra` and server-side contract or
database changes. Do not edit those files, `src/packages/contracts`,
`src/packages/fm-engine`, `src/README.md` or `src/docs/progress.md` during the
Windows task. Consume generated contracts; do not silently fork schemas.

## Required implementation outcome

1. **Staging API configuration**

   - Support local mock, Linux staging and later production through build-time
     public API-base configuration only.
   - Do not put provider keys, OAuth secrets, billing secrets, database
     credentials or signing material in Web/Android bundles, source maps,
     fixtures or logs.
   - Preserve safe offline/mock fallback while the Linux backend is absent.

2. **Console surface**

   - Rename the existing method/configuration area to `Console` where the
     approved UI concept permits it.
   - Move the current method/provider explanation into Console rather than
     presenting it as the main research result.
   - Put account login, Google/GitHub account state, subscription entry and
     optional OpenAI-compatible BYOK settings (`base URL`, model name and key)
     behind Console.
   - Do not add temperature/top-p controls. Do not collect or retain plaintext
     BYOK keys until the secure storage/transport contract exists; otherwise
     show the entry as disabled/planned.

3. **Phone/tablet/Web layout**

   - Implement separate responsive behavior for Web, Android phone and
     Android tablet. Do not simply stretch the Web layout onto a phone.
   - Test keyboard, safe areas, scroll, back navigation, loading/retry/error
     states, worldline cards, portfolio rows, disclosures, export and share
     import.
   - Use the approved quiet, restrained, conversation-first visual concept;
     do not copy Claude/Anthropic branding or proprietary assets.

4. **Google and GitHub login**

   - Web uses HTTPS redirect with server-issued state/PKCE and exact callback
     allowlists.
   - Android uses the approved package/deep-link/app redirect only after the
     Linux server allowlist and signing identity exist.
   - Test cancelled login, wrong account, callback replay, expired session,
     logout, account deletion, provider failure and network retry.
   - OAuth client IDs may be public where the provider requires it, but client
     secrets remain server-only. Never guess the domain, callback URL or
     client ID. If the owner has not supplied them, run mock/contract tests and
     record the external blocker without enabling a fake production login.

5. **Google Play Store**

   - Confirm application identity, package name, versioning, privacy policy,
     data-safety answers, support URL, screenshots, content declarations and
     internal-test track setup.
   - Use the owner-controlled release keystore/EAS credentials. Build a signed
     non-debug AAB, record SHA-256/provenance and test it on API 36 Play
     emulator `api36_play:5554` plus an owner-approved device when available.
   - The current debug APK is QA-only and never a production release artifact.

6. **Billing decision**

   - Default recommendation: ship the first public MVP as free and keep
     subscription entry visible in Console as a planned/disabled state until
     pricing, legal text and entitlement service are approved.
   - If the owner explicitly chooses paid access, implement Google Play Billing
     with server-side purchase-token verification, entitlement state, restore,
     cancellation, expiration and refund handling. Never trust a client-only
     purchase flag. Web billing is a separate decision and must not be added
     implicitly.

7. **Real FM and scenario QA**

   - Run a real example through the client FM engine with the approved data
     cutoff and factor manifest. Confirm factor selection, stable ties, NaN,
     IC/ICIR, composite signal, weights, equal-weight fallback, 3 bps fee,
     cost dead-band, amount/lot rounding and Raw/Public Default separation.
   - Ask Sol to challenge whether event dates, scenario probabilities and
     expected market/portfolio paths are internally consistent. Fix only with
     evidence and preserve the distinction between historical/simulated NAV
     and current investable data.

## Required verification evidence

- Web local/mock and Linux-staging API flows in Browser/IAB;
- Android API 36 Play emulator evidence on phone layout and tablet layout where
  available, including screenshot/UI tree/logcat for core states;
- successful OAuth tests or a precise missing-credential/redirect blocker;
- signed non-debug AAB, checksum and Play internal-test evidence, or a precise
  signing/Play permission blocker;
- full tests/typecheck, FM golden/parity audit, dependency audit and secret
  scan;
- final screenshots inspected with `view_image` against the approved concept;
- no provider/OAuth/billing/signing secret in source or client artifact;
- a redacted handoff to the primary integrator and a fresh Sol review before
  the client phase is accepted.

## Required Windows inputs from the owner

- confirmed Linux staging API URL and Web origin;
- confirmed Cloudflare domain, if it is used for callback/origin testing;
- Google Web/Android OAuth client IDs, redirect URIs, package/SHA-1 and consent
  screen configuration;
- GitHub OAuth client ID, server secret and callback URL;
- Android package identity, release keystore/EAS credentials and Play Console
  app permissions;
- privacy policy, terms, support URL and data-safety declarations;
- explicit free-MVP versus paid-MVP decision.

## Stop conditions

Stop the dependent part and report evidence when the staging API, confirmed
domain, OAuth settings, Android SDK/emulator, signing credentials, Play Console
permission, billing decision or legal text is missing. Continue no
implementation after the owner says “do not execute”. Do not replace a missing
external dependency with guessed production configuration.

The Windows agent must return commit, changed files, commands/counts,
device/emulator, artifact hashes, API mode, FM cutoff/parity result, security
result, external blockers and Sol review status. Never include secrets, raw
user viewpoints, raw provider payloads or unredacted login tokens.
