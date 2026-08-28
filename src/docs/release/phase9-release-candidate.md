# Phase 9 release-candidate evidence

## Boundary and status

This is a QA release-candidate record for the current checkout, captured on 2026-08-29. The verifier prints the exact `HEAD` used for each run. It is not a production-release approval, a public-launch declaration, or evidence that live providers, licensed current market data, signing credentials, or external operational gates are ready.

The final fresh Sol read-only review returned `ship` for this QA evidence and authorized the Phase 9 commit/push. The evidence was pushed as commit `dd1a5fe93945f199b2dc55918c678d1311f3429c`. It did not authorize production release or completion of the overall goal.

The candidate is intentionally composed of offline/mock and debug artifacts. They are suitable for static, UI, contract, and emulator QA only. A mock or debug artifact must not be labelled as production in a release system, client store, or public deployment.

## Currently generatable QA candidates

### Web static QA RC

- Existing static output: `apps/web/dist/`.
- Entry point: `apps/web/dist/index.html`.
- Generation command: `corepack pnpm --filter @foresightnine/web build`.
- The current output is a Vite static bundle with client-side FM and deterministic fixture/mock behavior. It can be served from a local or private static host for QA; it does not establish production API, provider, data-license, HTTPS, CSP, or traffic-release readiness.
- Current files and SHA-256 values are emitted by `corepack pnpm verify:release-candidate`; the directory is an ignored build output and must be regenerated for a different source revision.

### Android debug QA artifact

- Existing artifact: `apps/mobile/android/app/build/outputs/apk/debug/app-debug.apk`.
- Generation sequence: `corepack pnpm --filter @foresightnine/mobile export:android`, followed by the approved debug Gradle build for the target emulator ABI.
- Current size: 90,533,667 bytes.
- Current SHA-256: `1460203312862C137207794F6659AF31F04E5062F3132956CFEAEA7890FDF1AB`.
- This is a debug, QA-only APK. It is not a signed Android release artifact and cannot satisfy Play Console, package identity, keystore, EAS, privacy declaration, or release-provenance gates.

### API mock run

- Rerunnable command: `uv run --project apps/api pytest apps/api/tests`.
- The default path is `DeterministicMockLlmProvider` and the corresponding deterministic worldline/probability providers. It is schema-validated offline research material; it does not call a live GLM/OpenCode provider, native search, or licensed market-data service.
- The Phase 9 full-suite result is 124 API tests passed, with the documented Starlette/httpx deprecation warning. The request-level smoke evidence is retained in `docs/qa/api/phase9-mock-smoke.md`; it covers health, OpenAPI, strict validation, session creation, and challenge creation without making a live-provider claim.
- The mock-run implementation and tests are retained at `apps/api/README.md`, `apps/api/tests/test_phase3_providers.py`, and `apps/api/tests/test_phase4.py`.

## Existing implementation evidence included in this RC

The following paths are required evidence for the verifier and are implementation-QA evidence only:

- Web Browser/IAB or local browser captures inspected with `view_image`: `apps/web/qa/screenshots/web-01-thesis-current.jpg`, `web-02-challenge-current.jpg`, `web-03-worldline-current.jpg`, `web-06-nav-current.jpg`, `web-09-portfolio-desktop-fixed.png`, `web-11-fm-complete-fixed.png`, `web-12-methods-fixed.png`, `web-14-console-desktop.png`, and `web-15-console-mobile.png`.
- Web Phase 8 freshness/Public Default capture inspected with `view_image`: `docs/qa/web/phase8-public-default.png`.
- Android API-36 emulator captures inspected with `view_image`: `docs/qa/android/phase8-home.png`, `phase8-provider-fail-closed.png`, `phase8-public-default.png`, and the freshly installed current APK capture `docs/qa/android/phase9-current-home.png`; UI evidence includes `docs/qa/android/phase8-home.xml` and `docs/qa/android/phase9-current-home.xml`. The exact Metro + `adb reverse` environment is recorded in `docs/qa/android/phase9-metro-smoke.md`; this is not a no-Metro/self-contained APK claim.
- The evidence interpretation and the explicit mock/debug boundary are maintained in `docs/design/fidelity-qa.md`; the deployment gate inventory remains in `infra/deployment/README.md`.

## External gates still required before production

The candidate remains blocked from public production until the release owner records all applicable approvals and evidence, including:

- target-region legal review, approved privacy policy, service terms, risk disclosures, and investment-advice compliance review;
- written market-data and FX licensing/redistribution rights for production fields, symbols, caches, exports, and client-delivered shards;
- a newly issued server-side provider credential injected only through the deployment secret manager or an uncommitted deployment environment, plus proof that bundles, source maps, logs, fixtures, and databases contain no credential;
- CAPTCHA and abuse thresholds, IP/session quotas, daily model budgets, Redis TLS/authentication/TTL/backup/failure checks, and tested staged behavior;
- cryptographically secure UUID validation, secret/dependency/container scanning, CORS/CSP/SSRF review, redacted observability, incident runbooks, kill switches, rollback evidence, and data-retention/deletion approval;
- licensed/current provider freshness and signed-manifest verification, plus staged smoke, abuse, client-parity, and rollback runs;
- Android owner-controlled release signing, package/application identity, Expo/EAS credentials, Play Console enrollment, privacy declarations, and release-artifact provenance;
- written release-owner approval before changing traffic exposure.

None of these gates is satisfied merely by the Web static bundle, the Android debug APK, the emulator screenshots, or the API mock run. Broker connectivity, order routing, custody, and real-money execution remain out of scope.

## Verifier

From `D:\Foresightnine\src`, run:

```text
corepack pnpm verify:release-candidate
```

The verifier checks the repository `HEAD`, required documentation/evidence paths, a non-empty Web `dist` containing `index.html`, and a non-empty Android debug APK. It prints SHA-256 hashes for the Web static files and APK. It does not build, invoke providers, inspect environment variables, or print secrets. Missing QA artifacts cause a non-zero exit.
