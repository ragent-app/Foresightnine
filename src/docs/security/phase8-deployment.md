# Phase 8 API deployment limits

The Phase 8 slice is provider-neutral. Development and tests use bounded
in-memory protection; multi-worker or multi-replica deployments can select the
Redis implementation with `FORESIGHTNINE_ABUSE_MODE=redis` and a server-side
`FORESIGHTNINE_REDIS_URL` (or `REDIS_URL`).

Production and public-hardening startup fails closed unless Redis mode and its
URL are explicit. Do not enable public production traffic with the
deterministic CAPTCHA accept verifier; configure a real `CaptchaVerifier`
through deployment code. The generic interface is present, but no CAPTCHA
vendor or secret is selected by this repository.

Redis admission is atomic across IP/session request subjects and across
global/IP/session quota subjects. Connection, timeout, and protocol failures
are retryable 503s; exhausted limits remain 429s. No runtime memory fallback is
available. The Redis reset hook is non-destructive and relies on bounded TTLs
and normal pruning.

This repository contains deterministic fake-Redis tests and an opt-in live
test gated by `FORESIGHTNINE_REDIS_INTEGRATION=1` plus a supplied URL. Passing
the local suite is not live Redis, HA, TLS, eviction, failover, alerting,
capacity, or rollback evidence. Those operational checks remain deployment
gates.

The API will fail closed when `FORESIGHTNINE_ENV=production` or
`FORESIGHTNINE_PUBLIC_HARDENING=true` and CAPTCHA verification is unavailable.
Forwarded client headers are only trusted when the direct peer matches
`FORESIGHTNINE_TRUSTED_PROXY_CIDRS`. Deployments must set that list to the
actual proxy network rather than copying a broad or user-controlled value.

The current retention job preserves pending/running operation state and removes
only expired session descendants and bounded terminal operation state. A
deployment that persists state outside this process needs an equivalent
transactional expiry/cascade policy before launch.
