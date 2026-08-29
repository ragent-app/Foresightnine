# Phase 9 release-candidate evidence

## Boundary and status

This is a QA release-candidate record for the current checkout, captured on 2026-08-30. The verifier prints the exact `HEAD` used for each run. It is not a production-release approval, a public-launch declaration, or evidence that live providers, licensed current market data, signing credentials, or external operational gates are ready.

The final fresh Sol read-only review returned `ship` for the previous QA evidence and authorized its Phase 9 commit/push. Subsequent standalone Android hardening and search-configuration parity are recorded in `docs/progress.md`; the verifier must be used to bind this document to the exact current `HEAD`. No QA review authorizes production release or completion of the overall goal.

The candidate is intentionally composed of offline/mock and debug artifacts. They are suitable for static, UI, contract, and emulator QA only. A mock or debug artifact must not be labelled as production in a release system, client store, or public deployment. The Web output and Android APK below were rebuilt from the current checkout. The Android artifact remains debug-signed and is not a production release artifact.

## Currently generatable QA candidates

### Web static QA RC

- Existing static output: `apps/web/dist/`.
- Entry point: `apps/web/dist/index.html`.
- Generation command: `corepack pnpm --filter @foresightnine/web build`.
- The current output is a Vite static bundle with client-side FM and deterministic fixture/mock behavior. It can be served from a local or private static host for QA; it does not establish production API, provider, data-license, HTTPS, CSP, or traffic-release readiness. It was regenerated after the search runtime-config change and is paired in this record with the current-checkout Android rebuild. The subsequent Android manifest hardening is also included in the current checkout and is reflected in the current APK hash below.
- Current files and SHA-256 values are emitted by `corepack pnpm verify:release-candidate`; the directory is an ignored build output and must be regenerated for a different source revision.

### Android debug QA artifact

- Existing artifact: `apps/mobile/android/app/build/outputs/apk/debug/app-debug.apk`.
- Generation sequence: `corepack pnpm --filter @foresightnine/mobile export:android`, followed by the approved debug Gradle build for the target emulator ABI.
- Current size: 81,070,551 bytes.
- Current SHA-256: `37DB03028D3FB207664DAB15F419B50F640C66340DD9FA27E0C210E4324CB97D`.
- Current-revision rebuild status: **verified**. With JDK 17 and x86_64, the approved command used the repository helper `scripts/android-short-cmake.init.gradle` and completed with `BUILD SUCCESSFUL in 1m 51s`. This helper only stages generated native build output under a short path; it does not alter application or release configuration.
- The current debug artifact was generated with `react.debuggableVariants = []`
  and verified to contain `assets/index.android.bundle`. It was installed on
  API 36 emulator `emulator-5554` after Metro was stopped; the complete mock
  research path reached client FM and the Raw/Public Default configuration
  screen without a listener on port 8081. This strengthens standalone QA
  evidence only; the APK remains debug-signed and is not a release artifact.
- The current single-share-only home and share-intent captures are
  `docs/qa/android/phase9-single-share-only-home.{png,xml}` and
  `docs/qa/android/phase9-single-share-only-share.{png,xml}`. `apkanalyzer`
  confirms that the packaged manifest has the single `ACTION_SEND` filter and
  no empty `SEND_MULTIPLE` filter. The share intent opened `/evidence`,
  produced a local Grade C entry, normalized a safe HTTPS URL, and displayed
  the non-independent-review warning. One invalid XML child bounds rectangle
  remains; the UI tree is used for semantic evidence, while screenshot
  inspection is the geometry/fidelity evidence.
- This is a debug, QA-only APK. It is not a signed Android release artifact and cannot satisfy Play Console, package identity, keystore, EAS, privacy declaration, or release-provenance gates.

### API mock run

- Rerunnable command: `uv run --project apps/api pytest apps/api/tests`.
- The default path is `DeterministicMockLlmProvider` and the corresponding deterministic worldline/probability providers. It is schema-validated offline research material; it does not call a live GLM/OpenCode provider, native search, or licensed market-data service.
- The original Phase 9 candidate full-suite result was 124 API tests passed, with the documented Starlette/httpx deprecation warning. The later post-Phase 9 production-preparation working-tree regression is 128 API tests passed and is recorded in `docs/progress.md`; it does not retroactively turn this mock/debug candidate into a production release. The request-level smoke evidence is retained in `docs/qa/api/phase9-mock-smoke.md`; it covers health, OpenAPI, strict validation, session creation, and challenge creation without making a live-provider claim.
- The mock-run implementation and tests are retained at `apps/api/README.md`, `apps/api/tests/test_phase3_providers.py`, and `apps/api/tests/test_phase4.py`.

## Existing implementation evidence included in this RC

The following paths are required evidence for the verifier and are implementation-QA evidence only:

- Web Browser/IAB or local browser captures inspected with `view_image`: `apps/web/qa/screenshots/web-01-thesis-current.jpg`, `web-02-challenge-current.jpg`, `web-03-worldline-current.jpg`, `web-06-nav-current.jpg`, `web-09-portfolio-desktop-fixed.png`, `web-11-fm-complete-fixed.png`, `web-12-methods-fixed.png`, `web-14-console-desktop.png`, and `web-15-console-mobile.png`.
- Web Phase 8 freshness/Public Default capture inspected with `view_image`: `docs/qa/web/phase8-public-default.png`.
- Android API-36 emulator captures inspected with `view_image`: `docs/qa/android/phase8-home.png`, `phase8-provider-fail-closed.png`, `phase8-public-default.png`, the Metro-era `docs/qa/android/phase9-current-home.png`, and the standalone captures `docs/qa/android/phase9-standalone-home.png`, `phase9-standalone-worldline.png`, and `phase9-standalone-portfolio.png`; corresponding UI evidence includes the same `.xml` paths. The Metro + `adb reverse` environment is recorded in `docs/qa/android/phase9-metro-smoke.md`, while the standalone captures were made with port 8081 unused. Neither debug mode nor these captures is a signed release/public-deployment claim.
- Android share-intent evidence inspected with `view_image`: the historical
  captures `docs/qa/android/phase9-share-intent-evidence.png` and its UI tree,
 plus the current single-share-only captures
  `docs/qa/android/phase9-single-share-only-share.png` and its UI tree. A
  real API-36 `ACTION_SEND` text intent opened the evidence workspace and
  produced a local, unverified Grade C entry without fetching the URL. This is
  functional QA evidence, not a production data-validation claim.
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

## Container verification boundary

- `docker compose -f infra/docker/compose.yaml config --quiet` passed after the Dockerfile and Compose configuration corrections.
- The attempted API image build was not verified because the local Docker Desktop Linux engine/daemon was unavailable. No image startup, database/Redis connectivity, or container health claim is made until a Docker-enabled host reruns the build and staged smoke checks.

## Verifier

From `D:\Foresightnine\src`, run:

```text
corepack pnpm verify:release-candidate
```

The verifier checks the repository `HEAD`, required documentation/evidence paths, a non-empty Web `dist` containing `index.html`, and a non-empty Android debug APK. It prints SHA-256 hashes for the Web static files and APK. It does not build, invoke providers, inspect environment variables, or print secrets. Missing QA artifacts cause a non-zero exit.
