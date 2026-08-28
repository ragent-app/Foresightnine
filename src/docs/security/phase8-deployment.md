# Phase 8 API deployment limits

The Phase 8 slice is intentionally provider-neutral and in-memory. It provides
bounded single-process protection, not a distributed security service.

Before a multi-worker or multi-replica public deployment, replace or wrap the
in-memory IP/session limiter and model quota with an atomic shared store (for
example Redis) and run retention as a coordinated job. Do not enable public
production traffic with the deterministic CAPTCHA accept verifier; configure a
real `CaptchaVerifier` through deployment code. The generic interface is
present, but no CAPTCHA vendor or secret is selected by this repository.

The API will fail closed when `FORESIGHTNINE_ENV=production` or
`FORESIGHTNINE_PUBLIC_HARDENING=true` and CAPTCHA verification is unavailable.
Forwarded client headers are only trusted when the direct peer matches
`FORESIGHTNINE_TRUSTED_PROXY_CIDRS`. Deployments must set that list to the
actual proxy network rather than copying a broad or user-controlled value.

The current retention job preserves pending/running operation state and removes
only expired session descendants and bounded terminal operation state. A
deployment that persists state outside this process needs an equivalent
transactional expiry/cascade policy before launch.

