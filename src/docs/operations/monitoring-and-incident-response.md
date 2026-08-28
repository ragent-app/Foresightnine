# Monitoring and incident response

**Status: pending operations, security, privacy, and legal review/approval. Not a public launch notice.**

## Signals to monitor

The release candidate should emit privacy-safe metrics and structured events for:

- request rate, latency, error and timeout rate by endpoint and provider;
- CAPTCHA challenges and failures, rate-limit decisions, quota exhaustion, queue depth, Redis health, and idempotency conflicts;
- model response schema failures, invalid citations, prompt-safety blocks, cancellation/retry behavior, and fallback activation;
- market-data age distribution, invalid/future timestamps, provider stale/unavailable states, manifest/signature failures, and the number of Public Default allocations disabled by freshness;
- client FM failures, worker/Hermes crashes, Web/Android parity checks, archive restore failures, and export failures;
- authentication anomalies, UUID validation failures, SSRF blocks, suspicious enumeration, secret-scan findings, and unexpected outbound destinations.

Metrics must not contain thesis text, evidence excerpts, API keys, credentials, raw URLs with secrets, or full provider payloads. Identifiers must be minimized, hashed or short-lived where possible, and subject to the approved retention schedule.

## Alerting and degradation

Alerts need severity, threshold, owner, escalation path, and a runbook link. The service must fail closed for provider outages, Redis loss when limits cannot be enforced, invalid schemas, signature failures, uncertain freshness, secret exposure, and data-license suspension. Safe degradation may preserve an already-created, clearly labelled historical/simulated archive; it must not manufacture a current executable Public Default amount.

An operational kill switch should be able to disable live provider calls, expensive workflows, search, exports, or public traffic independently. The mock/offline path must remain visibly labelled and must not be mistaken for live data.

## Incident response expectations

1. Detect, classify, and assign an incident commander.
2. Contain first: disable affected provider/workflow, rotate or revoke credentials, block abusive sources, and preserve only the minimum approved evidence.
3. Assess impact across users, data, secrets, licensed content, generated outputs, and client releases.
4. Notify security, privacy, legal, provider, and product owners according to the approved jurisdictional and contractual timelines.
5. Recover from a known-good configuration, verify freshness and schema gates, and replay only idempotent safe work.
6. Document root cause, timeline, affected versions, decisions, user/provider notices, and corrective actions; complete a post-incident review.

The incident process, on-call ownership, notification contacts, backup/restore tests, and evidence-retention rules must be approved before public production traffic. This document does not claim that those approvals or launch gates are complete.
