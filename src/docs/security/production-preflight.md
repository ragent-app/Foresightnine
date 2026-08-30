# Production preflight

`verify:production-preflight` is a deployment-before-release gate. It is not a deployment, migration, secret injection, provider health check, or traffic-exposure command. It performs local configuration and approval checks only, and exits non-zero unless every gate passes.

The verifier is deliberately fail-closed. The current development Compose configuration selects the deterministic mock LLM provider, so it is expected to fail production preflight. Mock, debug, QA, unavailable, and unknown provider declarations are not production readiness evidence.

## Required inputs

The deployment context must provide:

- `FORESIGHTNINE_ENV=production`;
- a supported live `FORESIGHTNINE_LLM_PROVIDER` and its corresponding server-side credential (`FORESIGHTNINE_LLM_API_KEY` for `opencode`/`openai_compatible`, or `FORESIGHTNINE_GLM_CODING_API_KEY` for the GLM profiles);
- a supported external `FORESIGHTNINE_SEARCH_PROVIDER` and its corresponding server-side credential;
- `FORESIGHTNINE_STORAGE_MODE=postgres` and a
  `FORESIGHTNINE_POSTGRES_URL` or `DATABASE_URL`, and a TLS-protected
  `rediss://` value in `FORESIGHTNINE_REDIS_URL` or `REDIS_URL`;
- `FORESIGHTNINE_ABUSE_MODE=redis`;
- an explicit proxy topology: `FORESIGHTNINE_PROXY_MODE=direct` with an empty
  `FORESIGHTNINE_TRUSTED_PROXY_CIDRS`, or
  `FORESIGHTNINE_PROXY_MODE=reverse_proxy` with a comma-separated list of the
  actual ingress proxy CIDRs in `FORESIGHTNINE_TRUSTED_PROXY_CIDRS`; universal
  `/0` networks are rejected;
- an external CAPTCHA declaration in `FORESIGHTNINE_CAPTCHA_PROVIDER` (or an equivalent non-disabled `FORESIGHTNINE_CAPTCHA_MODE`) plus `FORESIGHTNINE_CAPTCHA_SECRET`. `accept`, `unavailable`, `reject`, mock, debug, and QA declarations are rejected;
- comma-separated `FORESIGHTNINE_CORS_ORIGINS`, with every origin using HTTPS and containing no path, query, or fragment;
- a readable, absolute, repository-external `FORESIGHTNINE_ANDROID_RELEASE_ARTIFACT` ending in `.apk` or `.aab`. Any `app-debug.apk`, `debug`, `qa`, or QA-only path is rejected. The verifier checks the deployment-provided artifact with filesystem stat/access or a test-injected artifact reader/stat function;
- an absolute path supplied as `--approval-manifest <path>` or `FORESIGHTNINE_RELEASE_APPROVAL_MANIFEST`. The path must be outside the repository and owned by the deployment process. Its JSON object must set all of these fields to boolean `true`: `legal`, `privacy`, `terms`, `risk`, `data_license`, `security`, `operations`, `rollback`, `android_signing`, and `artifact_provenance`.

The static container gates also read `infra/docker/Dockerfile.api` and `infra/docker/compose.yaml` from the repository (with test injection available). The Dockerfile must copy `packages/asset-registry`; Compose must contain `FORESIGHTNINE_LLM_PROVIDER` and must not contain the legacy `FORESIGHTNINE_PROVIDER` variable.

The verifier checks presence and safe configuration shape; it does not print values and does not prove that a credential is valid. Test fixtures should use dummy values that do not resemble real secrets.

The proxy topology gate is deliberately explicit. In `direct` mode the API
does not trust forwarded client headers and the trusted CIDR list must be
empty. In `reverse_proxy` mode the list must be syntactically valid and must
be restricted by the deployment owner to the actual ingress networks; the
universal `0.0.0.0/0` and `::/0` networks are rejected. The
preflight cannot prove network ownership; staging must verify that the direct
peer and ingress path match the declaration before traffic is exposed.

## Current expected result

This repository does not contain the external approval manifest, real provider secrets, production Postgres or Redis runtime, an external CAPTCHA runtime, a deployment-owned Android release artifact, or signing Android credentials. Therefore the current repository state is expected to be rejected. The absence of those deployment-owned inputs is a safety result, not a test failure. The checked-in development Compose configuration selects the mock provider and is also unsuitable for production.

Run the local checks with:

```text
corepack pnpm test:production-preflight
corepack pnpm verify:production-preflight
```

The command reports only gate ID, `PASS`/`FAIL`, and a static reason. It never emits environment values, URL query strings, secrets, artifact paths, or manifest paths. It does not contact providers, read artifact contents, start containers, or make network requests.

Passing this preflight does not replace staging smoke tests, provider/database/Redis/CAPTCHA runtime checks, rollback rehearsal, legal review, data-license verification, dependency/container audit, or Android signing and artifact-provenance verification. Deployment remains a separate, explicitly approved operation.

## Objective container closure

The API image now copies `packages/asset-registry`, because the API reads `packages/asset-registry/registry/v1/assets.json` at startup. Compose now uses the actual API variable `FORESIGHTNINE_LLM_PROVIDER`; its development `mock` value remains intentionally unsuitable for production preflight.
