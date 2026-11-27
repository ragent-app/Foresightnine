# ForesightNine privacy policy draft

**Status: pending legal review and approval. Not a public launch notice.**

This draft describes the intended privacy boundary for the Phase 8 public-test build. It is not the final privacy policy and does not replace advice for any target jurisdiction.

## Data inventory and purpose

This inventory covers the Phase 8 research and simulation build. The [product direction](../product/fusion-finance-ai-macro-plan-2026-11-11.md) also describes future investor profiles and return tracking. Data for those services needs its own purpose and retention assessment before collection.

| Data | Purpose | Default location |
| --- | --- | --- |
| Thesis text and capital amount | Build the user-requested research scenario and scale simulated amounts | Local client draft; server only when a user submits a live workflow |
| Challenge replies, confirmation choice, reservations | Preserve the auditable research decision and unresolved issues | Local client and short-lived server workflow state when live services are enabled |
| Evidence URL, title, publisher, dates, excerpt, grade, notes, hashes | Track provenance and distinguish public evidence from private claims | Local client; only the minimum required server fields for a submitted workflow |
| Worldline, probability intervals, method/data versions | Explain and reproduce the scenario | Local export and short-lived server workflow state |
| Market-data manifests, signed shards, FX snapshot metadata | Run client-side FM calculations and show data provenance | Client cache or local archive; raw shards are not uploaded as the final portfolio |
| Device-local archive and language preference | Restore an explicit local research archive | Device storage; encrypted archive on Android |
| Operational events | Protect availability, investigate abuse, and measure failures | Service logs/metrics with redaction and bounded retention |

The product should not collect brokerage credentials, payment credentials, private keys, API keys, or a user's full account/holdings data. A user-private evidence claim is treated as a private assertion, not as a public fact.

## Local versus server processing

The Web and Android clients perform factor selection, signal combination, weights, amount/lot conversion, and Public Default safety constraints locally. The server may accept the minimum workflow inputs needed for challenge, evidence, worldline, and manifest services, but it must not calculate or silently replace the user's final client-side FM result. The clients must never collect or display provider secrets.

Local drafts and Android archives are device-only unless the user explicitly exports or submits them. Public exports exclude user-private evidence by default. A share/export action is an explicit user action and must state what is included.

## Retention and deletion expectations

The final service configuration must define and publish retention periods before launch approval. The intended defaults are:

- anonymous server sessions, challenge transcripts, submitted evidence, and temporary job state expire after the shortest period needed for recovery and abuse investigation;
- operational logs contain redacted identifiers and expire on a separately approved schedule;
- cached market manifests and shards expire with the provider license and cache policy;
- device drafts and encrypted archives remain until the user deletes them, uninstalls the app, resets secure storage, or uses an in-product delete action;
- deletion requests must cover server workflow records, derived exports held by the service, and queued jobs where technically possible, with legally required security/audit exceptions documented;
- backups and replicas require an approved deletion propagation schedule.

No retention period is final until legal, security, provider-license, and operational owners approve it.

## Provider and credential boundaries

Provider credentials are server-side deployment secrets only. They must come from a secret manager or uncommitted environment injection, never from Web/Android bundles, source maps, local archives, logs, error reports, fixtures, or database records. Old or exposed keys are revoked and are never reused. Provider responses are schema-validated and redacted before storage or telemetry.

The release candidate must document the processor, region, data fields, subprocessors, and cross-border transfer basis for every enabled provider. No live provider is implied by an offline fixture or by an unconfigured console entry.

## User rights and review gates

The final notice must identify applicable access, correction, deletion, objection, portability, and complaint routes for each target jurisdiction. Data licensing, privacy notice, service terms, risk disclosure, and investment-advice/legal review are launch blockers. This draft does not claim public launch or regulatory clearance.
