# Progress

## Phase 9 release-candidate preparation — 2026-08-29

- Phase 9 started only after rereading the supplied goal objective, `plan.md`, and `agent.md`; neither governance file was changed. A fresh read-only Sol planning review returned `fix-first` for evidence freshness rather than a code defect: regenerate Web/Android artifacts from the current checkout, add a unified release-candidate verifier, refresh the fidelity boundary, and record an API smoke run.
- Remediated that evidence gap in `docs/release/phase9-release-candidate.md`, `docs/qa/api/phase9-mock-smoke.md`, `scripts/verify/release-candidate.mjs`, and `docs/design/fidelity-qa.md`. The verifier checks required QA evidence, Web `dist`, Android debug APK, and prints SHA-256 without reading or printing secrets. Added `corepack pnpm verify:release-candidate` to the root scripts.
- Rebuilt the current Web static bundle with `corepack pnpm --filter @foresightnine/web build` and the Android debug QA artifact with Expo export plus the approved x86_64 Gradle command. The current APK was installed on API-36 emulator `emulator-5554`, cold-launched with Metro and `adb reverse tcp:8081` active, and its home screen/UI tree were captured as `docs/qa/android/phase9-current-home.png` and `phase9-current-home.xml`; `view_image` inspection passed. The filtered log tail contained no fatal/ReactNativeJS/RedBox error. This is explicitly Metro/debug QA evidence, not a no-Metro self-contained APK claim.
- API request-level mock smoke passed: `/health` and `/openapi.json` returned 200; strict invalid session input returned typed 422; valid session and challenge creation returned 201. The run used the deterministic mock provider and did not call GLM/OpenCode or licensed market data. No live GLM credential is present in the server environment, and no plaintext credential from task history was used or copied.
- Phase 9 regression passed: `corepack pnpm test` (FM 16, domain 9, asset registry 2, Web 49, Android 41, API 124); `corepack pnpm typecheck`; generated-contract, boundary, immutable-reference, 9WL fixture, release-candidate, secret-scan (494 Git-history blobs), and `git diff --check` verifications. The API retains one documented Starlette/httpx deprecation warning; `corepack pnpm audit --prod` remains open for the documented moderate transitive `uuid` advisory.
- Release artifacts currently hash to Web `fm.worker-C7HxMD7b.js` `9C2BD44FEA8E04FB35CD42A0CB6A551881A5BBB9C3A42C699C319634E3EC2DA6`, Web `index-BX1xOnI2.js` `FA09335B45AACDF40C7BA2129DA8917D2E6AB606726C6BB5F70925C838AE9FB1`, Web CSS `8ABA60E0273BE2C84305EFBD42EC045AA31D72552D3371E8F66228D3FC5E2A1C`, Web HTML `3EED8CC8EC66DA61E4A315028D6F2F009FA31F5BC82A3573F925C05C3B31B916`, and Android APK `1460203312862C137207794F6659AF31F04E5062F3132956CFEAEA7890FDF1AB` (90,533,667 bytes). These are QA artifacts only, not production-signed/released artifacts.
- Final fresh read-only Sol release-candidate review returned `ship` with no new P0/P1/P2. It approved the Phase 9 QA commit/push and confirmed the Android evidence is accurately scoped to Metro + `adb reverse` + debug/dev-client execution. QA evidence was committed as `dd1a5fe93945f199b2dc55918c678d1311f3429c` and pushed to `origin/master`. This does not approve production release or complete the overall goal. External production gates remain separate: legal/privacy/terms/risk approval, data/FX redistribution licenses, new server-side provider secret, production Redis/CAPTCHA/quotas/observability/rollback, dependency remediation or approved exception, and Android owner-controlled signing/store release evidence.

## Phase 8 implementation and safety verification — 2026-08-29

- Implemented the Phase 8 public-safety boundary without changing `plan.md` or `agent.md`: bounded IP/session abuse controls, model-call quota reservation, trusted-proxy handling, production fail-closed CAPTCHA integration point, raw-body/content-type/size checks, CORS/security headers, idempotency-safe terminal operations, bounded retention cleanup, cascade deletion, and redacted telemetry. Multi-replica Redis/coordination remains an explicit deployment prerequisite; the local in-memory adapter is not claimed as production infrastructure.
- The first fresh Sol Phase 8 review returned `fix-first` with no P0/P2: it identified two current-code P1s. The request boundary now caps chunked bodies during receive (before body concatenation), and model-call subjects now include a shared rolling-24-hour `global:daily` bucket. A second fresh Sol review found two additional P1s; async model quota is now admitted before a 202 receipt, preserving typed 429/`Retry-After` semantics without creating an operation, and lifecycle retention uses a progressing runtime clock while deterministic provider timestamps remain fixed. The canonical ErrorResponse enum and regression coverage cover these paths.
- Added review-ready drafts for privacy, risk disclosure, public abuse controls, monitoring/incident response, and Phase 8 deployment operations. They explicitly separate local FM processing from server challenge processing, prohibit broker/order/custody behavior, and list market-data licensing, legal review, CAPTCHA operations, secret injection, Redis, signing, and the transitive `uuid` advisory as launch gates.
- Added shared freshness evaluation and integrated it into Web and Android defaults. Offline fixtures now have provider freshness `unavailable`; only an explicitly fresh provider snapshot may enable current Public Default amounts. Stale/invalid data renders `—` and retains the full capital as residual cash, while FM Raw remains visibly historical/simulated.
- Final verification passed: `corepack pnpm test` (FM 16, domain 9, Web 49, Android 41, API 124); focused Phase 8 security tests (14); `corepack pnpm typecheck`; `corepack pnpm verify:generated-contracts`; `corepack pnpm verify:boundaries`; `corepack pnpm audit:worldline-fixtures`; `corepack pnpm scan:secrets` (444 Git-history blobs); and `git diff --check`. A direct ASGI chunked-body probe returned 413 without `Content-Length`, and cross-session plus pre-receipt async quota tests returned typed 429. The API suite retains one Starlette/httpx deprecation warning. `corepack pnpm audit --prod` still reports the documented moderate `uuid` advisory (`GHSA-w5hq-g745-h8pq`), so it remains open.
- Browser/IAB verification completed the Web path from thesis/capital through challenge response, reservation confirmation, worldlines, local FM, Raw/Public Default, and allocation. The current offline snapshot shows Public Default amounts disabled (`—`, residual cash ¥100,000); no browser console error/warning was observed. Visual evidence: `docs/qa/web/phase8-public-default.png`, inspected with `view_image`.
- Android API-36 Google Play emulator `emulator-5554` installed and launched the fresh x86_64 debug APK. Expo Android export and `:app:assembleDebug -PreactNativeArchitectures=x86_64` passed under the bundled Temurin 17 JDK with the repository short-CMake helper. The installed app rendered the thesis screen, completed the mock challenge with a reservation, ran client FM, and showed the latest freshness/Public Default allocation page with disabled amounts (`—`) and residual-cash/risk disclosure; the unavailable/invalid mock response also displayed explicit recovery without fabricating a worldline or crashing. No fatal/ReactNativeJS/RedBox error appeared in the inspected log tail. Visual evidence: `docs/qa/android/phase8-home.png`, `docs/qa/android/phase8-provider-fail-closed.png`, and `docs/qa/android/phase8-public-default.png`, all inspected with `view_image`. APK: `apps/mobile/android/app/build/outputs/apk/debug/app-debug.apk` (90,533,667 bytes; SHA-256 `1460203312862C137207794F6659AF31F04E5062F3132956CFEAEA7890FDF1AB`), QA-only and not a public release artifact.
- The latest fresh read-only Sol review returned `ship` with no current code-level P0/P1/P2 findings. This is a Phase 8 code gate only. No public deployment, live model/search request, licensed market-data claim, OAuth, billing, broker, execution, custody, or legal approval is claimed. The approved Phase 8 change set was committed as `73e9ee7` and pushed to `origin/master`; the requested pause follows that handoff, and Phase 9 remains pending.

## Phase 7 complete — Sol ship and repository handoff — 2026-08-29

- Final fresh read-only Sol review returned `ship` with no P0/P1/P2 findings. The review fingerprint matched before and after inspection: 340 files, manifest `0269c17a7d7f15e24532f004ad66a0cd32fd07f0e883536ad219f23d1798a319`, status `fe8b48b23f28ab4c614bc10b5d73bdfcf2aaabdd967341937ef0dce0f22e11b6`.
- Phase 7 now covers the full local research loop: bilingual Web and Android flows, challenge/evidence/worldline/portfolio gates, Web Worker and shared client-only FM calculation, raw versus Public Default outputs, local archive handling, search fallback, exports, and Async/reset invalidation. Web exports and Android restores fail closed for forged lifecycle, evidence, reviewability, vector, or future-date state.
- Final verification passed: `corepack pnpm test` (FM 16, domain 5, registry 2, Web 45, Android 38, API 110); `corepack pnpm typecheck`; `corepack pnpm verify:boundaries`; `corepack pnpm audit:worldline-fixtures`; `corepack pnpm scan:secrets` (301 Git-history blobs); and `git diff --check`. The API retains one third-party Starlette/httpx deprecation warning.
- Browser/IAB evidence: English Challenge rendered and completed; unconfirmed Worldlines/Allocation remained disabled; no console warning/error. Android API 36 Google Play emulator `emulator-5554` cold deep-link `foresightnine://challenge` showed the protected challenge screen with guard and start action, no retry/submit bypass, and no fatal/ReactNativeJS/RedBox error. QA artifacts are under `docs/qa/android/`.
- The current debug Android package is available at `apps/mobile/android/app/build/outputs/apk/debug/app-debug.apk`; it is a local QA build, not a signed public release. No live GLM key was used or copied; provider remains mock/unconfigured unless a server environment injects a secret.
- `corepack pnpm audit --prod` still reports the documented moderate transitive `uuid` advisory (`GHSA-w5hq-g745-h8pq`) in the Expo/Xcode build toolchain. It remains a Phase 8 production gate. Phase 8 and Phase 9 have not started; no public deployment, OAuth, billing, broker, order, custody, licensed market-data release, or legal approval is claimed.

## Phase 7 gate — second fresh Sol fix-first findings — 2026-08-28

- A second fresh read-only Sol review was observably routed as `sol_advisor_sol_reviewer` / Luna high and completed with an unchanged before/after fingerprint (`333` files; manifest `BE41F461764D78252670F8E0CDDD315540028163CC6D47199F40FD158716A265`; status `9B5805FE66C368C26F5DD82FA0D62DEF2DB7C83E34FE4AC3183C9F5D5F9FCC88`). It returned `fix-first`.
- P1: Android restoration of a valid legacy privacy document without `researchState` spreads over the warm current state, allowing old challenge/evidence/FM/portfolio fields to remain. A warm-state reset regression is required.
- P1: Android `startChallenge` resets only challenge text/status and can retain evidence, branch, FM, portfolio mode and export state. New research must reset the complete downstream workflow.
- Sol re-verified Web 34/34, Android 22/22, both typechecks, 9WL audit, 301-blob secret scan and diff check; no P0 was found. No commit/push or Phase 8 work is authorized until these P1s are fixed and re-reviewed.

## Phase 7 P1 remediation — Web viewpoint reset and Android archive boundary — 2026-08-28

- Web `set_viewpoint` now preserves only locale, the new viewpoint, capital and currency; it resets stage, challenge transcript/status/reply/rubric/evidence/confirmation, branch selection, FM progress/status/result, portfolio mode, stress state and export notice. Regression coverage exercises both completed/confirmed and pending/partial prior workflows, then verifies a new challenge starts with only the new premise.
- Closed the same lifecycle boundary for an explicit `start_challenge` retry: even when the text is unchanged, the reducer now rebuilds a clean workflow before creating the two fresh challenge turns. A regression proves prior evidence, branch, FM and reply state cannot leak into a restarted challenge.
- Android archive restore now uses a dependency-free strict normalizer for the envelope, privacy document, nested research snapshot, evidence, rubric, branches/timelines, method data and JSON-safe FM result. It rejects malformed versions/shapes, arrays, enums, dates and numeric fields; maps serialized FM `null` values back to `NaN` only for fields that legitimately serialize FM `NaN`; and safely downgrades incomplete confirmation/lifecycle/downstream state to pending/empty rather than hydrating it.
- Closed the second Sol finding by centralising `mobileInitialState` and `resetMobileWorkflowState` in `apps/mobile/src/lifecycle.ts`. Android `startChallenge`, viewpoint changes and legacy input-only archive restore now reset the complete downstream workflow and rebuild the rubric in the active locale. Warm-state legacy restore and fresh-challenge tests cover the reset boundary.
- Reserved restore requires a completed challenge, explicit acknowledgement and a nonblank note; ordinary confirmed restore requires a completed challenge and evidence. Legacy v1 snapshots with missing lifecycle fields remain pending/false/empty. `restoreAppState` re-normalizes unknown input and returns the current state unchanged when rejected, so malformed archive input cannot throw during hydration.
- Focused verification passed: Web 34 Vitest tests and typecheck; Android 24 Vitest tests and typecheck; `git diff --check` passed with existing LF/CRLF normalization warnings. No FM implementation/formula, governance file, external read-only source, commit or push was changed.

## Phase 7 gate — fresh Sol fix-first findings — 2026-08-28

- A fresh, observably routed `sol_advisor_sol_reviewer` completed read-only review with an unchanged before/after fingerprint (`331` files; manifest `F88B86CFC2992DD02978303889CCA3F240DB3D21D3A19F8EB52939094D7EE187`; status `C4A266F7F93E6839FE3F94986AEA0F84EC68CFAE4A1DFA4E16E7B4608FDEFBAA`). It returned `fix-first`, not a Phase 7 pass.
- New P1: Web viewpoint reset still leaves stale challenge/evidence/FM/stage state in some paths; a changed thesis must never reuse downstream research state.
- New P1: Android archive restore accepts malformed snapshot fields/`fmResult` shapes and can throw; lifecycle consistency must be validated at the archive boundary and restore must fail closed without throwing.
- Existing gate evidence was explicitly re-verified by Sol: Android evidence/worldline route guards and empty-ledger behavior, client-only FM/no-secret boundary, bilingual challenge controls, and Web probability/date parity remain valid. No P0 was found. Terra remediation is required before another Sol gate; no commit/push or Phase 8 work is authorized.

## Phase 7 gate remediation and cross-platform confirmation QA — 2026-08-27

- Installing `jq` restored the prescribed native Sol runtime inspector. A fresh `sol_advisor_sol_reviewer` was then observably routed as Luna/high; exact before/after repository fingerprints matched, and its read-only `fix-first` verdict was accepted. The review identified six product-lifecycle and parity findings: branch-specific Web probabilities, occurrence/reveal separation, reserved-confirmation bypasses, Android mock-evidence injection, Web/Android horizon drift, and Chinese challenger copy in English mode.
- Closed all six findings. Web now renders the active branch assessment and first post-baseline reveal, uses the shared 2028-08-31 horizon, and requires a completed challenge plus explicit acknowledgement and nonempty reservation note. Android no longer injects evidence during archive/export, safely downgrades legacy archives missing lifecycle fields, applies the same reservation gate, blocks evidence/worldline deep links and `runFm` while confirmation is pending, and resets downstream state when the viewpoint changes.
- Browser/IAB QA exercised an English viewpoint through a completed challenge and reserved confirmation, confirmed no untranslated challenge controls, switched Main → Weak, and observed the active conditional range change to 20–35%, joint range to 7.00–17.15%, and first reveal to 2027-02-28. A final visual pass also removed the remaining English-date Chinese punctuation.
- Native API-36 emulator QA cold-started the current debug x86_64 build with an empty local state. Direct `foresightnine://evidence` access kept the worldline button disabled and the ledger empty; direct `foresightnine://worldline` displayed only the confirmation warning and route back to Challenge. The AndroidRuntime/ReactNativeJS error-filtered log was empty. Evidence: `docs/qa/android/phase7-evidence-gate.{png,xml}` and `docs/qa/android/phase7-worldline-gate.{png,xml}`; both screenshots were inspected with `view_image`.
- Full verification passed: FM 16, domain 5, asset registry 2, Web 31, Android 18 and API 110 tests; all workspace typechecks; governance boundaries; 9WL audit; and the current-tree/Git-history secret scan over 301 unique blobs. The API retains one third-party Starlette/httpx deprecation warning. A fresh independent Sol `ship` verdict is still required before Phase 7 commit/push or Phase 8.

## Phase 7 gate — fresh review route remains unobservable — 2026-08-27

- The Codex project registry now exposes a Git project whose authoritative path is `D:\Foresightnine`; this supersedes the earlier note that no matching project path was registered. The project label is stale (`ACFM_WL_paperwriting`), so the path—not the label—was used when checking scope.
- The Sol/Terra companion-profile check passed. A fresh read-only `sol_advisor_sol_reviewer` was assigned the complete Phase 7 gate packet, but the mandatory runtime inspector again returned `ERROR: rollout is missing, ambiguous, invalid, or inconsistent required routing metadata.` The reviewer was closed while still running, and no output or verdict was consumed.
- This remains a routing-observability failure rather than a code-quality finding. It cannot authorize a Phase 7 commit/push or Phase 8 work. A compliant independent verdict is still required.

## Phase 7 full regression after cross-platform parity coverage — 2026-08-27

- Root workspace typecheck passed. The full suite passed FM 16, domain 5, asset registry 2, Web 28, Android 14 and API 110 tests; the API retains one third-party Starlette/httpx deprecation warning.
- Governance-boundary verification and the 9WL cutoff/timeline audit passed. The current-tree plus Git-history secret scan passed after inspecting 301 unique history blobs.
- These checks verify the accumulated current worktree, including the new Web/Android FM parity regression. They do not resolve the already-documented Expo/Xcode transitive `uuid` production-audit gate, provide a live GLM call, or substitute for independent Sol acceptance.

## Phase 7 gate-support — explicit Web/Android FM parity regression — 2026-08-27

- Added an application-level parity regression in `apps/mobile/tests/mobile-domain.test.ts`. It imports the actual Web and Android FM input fixtures, asserts that the complete cutoff-safe input structures are equal, runs both through the shared client-only FM engine, and then compares selected factor IDs, combined signals, forecasts and raw target weights exactly.
- This closes the previously indirect Phase 7 acceptance evidence for “Android calculation results match Web”: future fixture or application-boundary drift now fails the Android suite instead of relying only on both apps importing the same package.
- Verification: `corepack pnpm --filter @foresightnine/mobile typecheck` passed; the mobile suite passed 14 tests; `git diff --check` passed with only existing LF/CRLF normalization warnings.
- No FM formula, fixture value, Public Default rule, plan gate or release status changed. Formal independent Sol acceptance remains required before Phase 7 commit/push or Phase 8 work.

## Repository state correction — 2026-08-27

- Read-only Git inspection confirms `origin` is configured as `https://github.com/seethelightluo/Foresightnine.git`; `master` and `origin/master` currently point to `7577176` (`feat: add Android research workflow pending native QA`). The older Phase 6 historical note saying that no remote was configured is superseded for the current checkout.
- The Phase 7 and later changes shown as modified/untracked remain local and uncommitted. No Phase 7 push is claimed because the formal Sol gate is still pending.

## Phase 7 gate-support — Android duplicate-evidence notice fix — 2026-08-27

- Corrected `apps/mobile/app/evidence.tsx` and `apps/mobile/src/app-store.tsx`: the UI no longer reads a result from inside React's asynchronous state updater. Duplicate evidence is determined from the current rendered ledger before dispatch, while the store keeps the duplicate write guard. A duplicate project-search selection now preserves the duplicate notice instead of being overwritten as a successful selection.
- Added repository-local ignore rules for generated `.build-cache`, `.playwright-cli`, Gradle/Kotlin and Android build output. Native Android source remains visible to Git; generated QA/build output no longer pollutes the Phase 7 change set.
- Verification: `corepack pnpm --filter @foresightnine/mobile typecheck` passed; the focused mobile suite passed 13 tests; `git diff --check` passed with only existing LF/CRLF normalization warnings.
- This is a Phase 7 technical correction only. It does not change the evidence hash/duplicate rule, client-only search boundary, formal Sol gate, phase ordering, or release status.

## Phase 7 gate — native Sol routing reattempt still fail-closed — 2026-08-27

- The exact companion-profile check passed again, and the host exposed both required native role names. A fresh `sol_advisor_sol_reviewer` was given a complete read-only Phase 7 packet and was closed without consuming its response.
- The prescribed runtime inspector again returned `ERROR: rollout is missing, ambiguous, invalid, or inconsistent required routing metadata.` Because selected role/model/effort and reviewer isolation are not observable, this attempt supplies no valid Sol verdict and cannot authorize a phase transition.
- The Codex app project registry still does not contain `D:\Foresightnine`, so the contract-compliant user-visible Luna task lane cannot be opened for this repository either. No fallback model or unverified review was substituted.
- Current primary verification remains green and the implementation evidence is unchanged. Phase 7 remains technically evidence-ready but formally pending; Phase 8/9, commit/push, live provider testing and public deployment remain unauthorized.

## Phase 7 primary verification pause — 2026-08-27

- Primary Sol reran the Phase 7 verification set on the current checkout: `corepack pnpm typecheck`, `corepack pnpm test` (FM 16, domain 5, registry 2, Web 28, Android 13, API 110), `corepack pnpm verify:boundaries`, `corepack pnpm audit:worldline-fixtures`, `git diff --check`, `corepack pnpm scan:secrets` (301 Git-history blobs), and Android production Hermes export/debug build evidence; all required checks passed. The API suite retains one third-party Starlette/httpx deprecation warning.
- `corepack pnpm audit --prod` still reports only the documented moderate Expo/Xcode transitive `uuid` advisory (`GHSA-w5hq-g745-h8pq`); it remains a release gate and is not silently treated as resolved. No live GLM request was made because no server-injected secret was available.
- The final Android project-search fallback screenshot and UI-tree evidence were inspected again. It shows a bounded query, explicit unconfigured-API fallback, no-auto-fetch disclosure and an unobscured local `SearchEvidenceBundle` action; the preceding Metro refactor removed the warning overlay and preserved the FM public surface.
- Formal Phase 7 acceptance is **not granted**. The native Sol reviewer routing remains invalid/unobservable, and the Codex app project registry currently has no `D:\Foresightnine` Git project, so the contract-compliant Luna review lane cannot be created for this repository. This is an environment/routing blocker, not a code-quality verdict. No Phase 8/9 work, commit/push, public deployment, production OAuth/billing, live provider call or release claim is authorized by this entry.

## Phase 7 gate-support — Android project search and Metro warning remediation — 2026-08-27

- Replaced the former Android-only `API search unavailable` placeholder with a bounded project-search client. It reads only the deployer-supplied `EXPO_PUBLIC_API_BASE_URL`, requires a public HTTPS API base, calls only `GET /v1/search/candidates`, sends no credentials, and never opens or fetches a returned source URL. Malformed responses, unconfigured API, non-2xx responses and transport failures fail closed into the existing browser-share/paste `SearchEvidenceBundle` fallback.
- A person must select a displayed candidate before the client creates a local C-grade contextual bundle. The bundle retains `search_provider` and `search_result_hash`, is labelled unverified, and cannot silently become a public fact. Android deliberately does not connect to GLM MCP or carry a model/search secret; project API search is the feasible mobile path, while Share Intent/paste remains the offline fallback.
- Removed the two Android Metro `Require cycle` warnings without changing FM formulas or the public FM package surface: the public export barrel is now separate from a dependency-free core module, and AST/orchestration import that core directly. This removes the debug warning overlay that previously obscured lower-screen controls.
- A new Metro process cold-started the Android app after that refactor and emitted only its normal bundle record (no `Require cycle` warning); the final UI tree had no `Open debugger to view warnings` overlay. This separates the passing evidence from the old Metro session's buffered pre-refactor warning messages.
- Native API-36 Google Play emulator QA: built and installed the current debug x86_64 APK, opened `foresightnine://evidence`, used UI-tree-derived field/action coordinates, entered `grid`, dismissed the IME, invoked project search with no configured public API, and confirmed the explicit fallback plus no-auto-fetch disclosure. The native process had no `FATAL EXCEPTION`, `Unable to load script`, TypeError or ReferenceError. Evidence: `docs/qa/android/phase7-project-search-query.xml`, `docs/qa/android/phase7-project-search-fallback.xml`, and `docs/qa/android/phase7-project-search-fallback.png`; the final screenshot was inspected with `view_image` after the warning-removal fix.
- Verification: root workspace typecheck; full test suite (FM 16, domain 5, registry 2, Web 28, Android 13, API 110); Android production Hermes export; debug APK build; governance-boundary/9WL audits; `git diff --check`; and the current-tree/Git-history secret scan all passed. The production dependency audit still reports the documented Expo/Xcode transitive moderate `uuid` advisory (`GHSA-w5hq-g745-h8pq`), so it remains a release gate. No GLM key was supplied through a server environment, so no live provider/search request is claimed.
- Formal Phase 7 acceptance is still blocked by the independent reviewer routing-metadata failure recorded on 2026-08-26. This entry does not authorize Phase 8/9, release/Play distribution, a phase commit/push, production OAuth/billing or public deployment.

## Phase 7 gate — independent reviewer routing rechecked, still fail-closed — 2026-08-26

- Re-read the approved `D:\Foresightnine\plan.md`, `D:\Foresightnine\agent.md`, and the Sol orchestration contracts before a new gate attempt. The non-mutating companion-profile check passed and the host exposed the exact `sol_advisor_sol_reviewer` role.
- Spawned a fresh reviewer with a read-only final-review packet covering the accumulated Phase 7 Android/shared-client diff, current test evidence, no-secret boundary, lifecycle/share behavior, and explicit non-production limitations. Before accepting any verdict, the prescribed runtime inspector returned: `ERROR: rollout is missing, ambiguous, invalid, or inconsistent required routing metadata.` The reviewer was immediately shut down and no verdict was consumed.
- This is a routing-observability failure, not a code-quality verdict. Per the fail-closed role contract, it cannot be represented as a Sol pass, cannot authorize Phase 8/9, and cannot authorize the requested phase commit/push. The Phase 7 technical evidence remains intact; the formal acceptance gate remains pending a host that exposes consistent role/model/effort metadata.

## Phase 7 gate-support — Console, optional account boundary and responsive native QA — 2026-08-24

- Replaced the visible Web and Android **Methods** navigation entry with **Console**. The original methodology, evidence-grade, probability, FM Raw/Public Default and risk material remains accessible beneath the optional account status rather than being removed.
- Console exposes only truthful states: Google/GitHub OAuth is disabled until a future server readiness/PKCE start action exists; GLM Coding is documented as server-environment-only; the OpenAI-compatible personal-provider surface explicitly reserves a future encrypted server credential-vault design and has no BYOK/API-key field; Google Play billing is only a disabled planning surface.
- Web verification: TypeScript typecheck; 28 Vitest tests; local Playwright desktop and 390×844 mobile interaction/screenshots; `view_image` inspection. The IAB browser-control runtime failed to load its own module, so the prescribed local Playwright fallback was used. The final local browser console contained **0 errors, 0 warnings** (one React DevTools informational message only).
- Android verification: TypeScript typecheck; 11 focused Vitest tests; native emulator deep-link route `foresightnine://console`; `view_image` inspection of phone (`phase7-console-phone.png`) and a temporary 1200dp-wide tablet profile (`phase7-console-tablet.png`). Phone controls stack; tablet control bounds prove Google `[37,373]–[596,421]` and GitHub `[604,373]–[1163,421]` are side-by-side. Both are disabled and contain no secret.
- Final regression: `expo export --platform android` produced the Android Hermes bundle; `:app:assembleDebug` succeeded for x86_64 under Temurin 17/API 36; workspace typecheck and tests passed (FM 16, domain 5, registry 2, Web 28, Android 11, API 110); governance-boundary and 9WL fixture audits passed; `git diff --check` passed; secret scan passed for the current tree and 301 unique Git-history blobs. The only production dependency audit result is the pre-existing moderate `uuid` advisory through Expo/Xcode tooling (`GHSA-w5hq-g745-h8pq`); it remains a release gate.
- Corrected the release-evidence record in `docs/plan-deviations.md`: current release signing is fail-closed and no release/Play artifact is claimed from this pass. The current debug native build remains emulator QA only.
- Formal Phase 7 acceptance remains **blocked**: this runtime cannot provide valid, observable routing metadata for the required independent Sol reviewer. No Phase 8/9 start, commit, push, production OAuth, live GLM request, user key storage, billing operation or public deployment is claimed.

## Phase 8 gate-support — refreshed production dependency audit — 2026-08-24

- `corepack pnpm audit --prod` now reports one moderate advisory, `GHSA-w5hq-g745-h8pq`, via `xcode@3.0.1 > uuid@7.0.3` in the Expo configuration/build toolchain. The former high-severity `image-size` findings are no longer present in the current resolved graph.
- The issue is not silently overridden: local dependency tracing confirms it is a build-time Xcode-project manipulation path, but the public-release audit is still not clear. The evidence, containment boundary and supported remediation order are in `docs/security/dependency-audit.md`.
- This is Phase 8 support evidence only. Phase 8 has **not** started or passed because the required Phase 7 Sol gate remains pending.

## Phase 7 gate-support — fresh Android back and lifecycle regression — 2026-08-24

- On the already-installed QA release running on Google Play API 36 `emulator-5554`, opened the English Challenge route from a persisted local Thesis, then issued Android system Back (`KEYCODE_BACK`). The native UI tree returned to the Thesis screen with the exact persisted viewpoint and `1,000,000` CNY capital still present.
- Forced `com.foresightnine.mobile` to stop, relaunched it with `adb shell monkey -p`, and re-inspected the native tree. The application again restored the local Thesis and capital, and the inspected logcat tail contained no application `FATAL EXCEPTION` or `AndroidRuntime` crash record. This supplements, rather than replaces, the existing full-loop Phase 7 device evidence.
- Rebuilt the current Android release from this worktree with the per-process Android Studio JBR/SDK variables and `scripts/android-short-cmake.init.gradle`; `assembleRelease` completed successfully in 33 seconds. The rebuilt APK is SHA-256 `13EA89383B4F2706F3B8B735A3933BEECA9D308E8272C4A9E3AA1F020FDB7D1E`, installed successfully with `adb install -r`, launched on `emulator-5554`, and exposed the persisted Chinese Thesis screen in the native UI tree. A second 15-second build with explicit `NODE_ENV=production` also passed and removed the earlier missing-NODE_ENV notice. The remaining SDK-XML compatibility and Gradle-10 deprecation notices are release-hardening items, not hidden build failures.
- This is emulator-only technical QA. The Phase remains **not formally accepted** because the mandated native Sol reviewer routing is still invalid/unobservable; it does not authorize Phase 8, a release claim, commit, or push.

## Phase 7 gate-support — truthful optional account affordances and refreshed device QA — 2026-08-24

- Added matching Google/GitHub account-status components to the approved Web and Android thesis flows. Both intentionally render an **anonymous research** state and disabled provider controls until a future server-readiness endpoint has verified OAuth configuration and supplied a PKCE start action. No OAuth client ID, redirect, secret, token exchange, session or simulated successful login is bundled or claimed.
- The Web component is visible in the desktop research rail and, at narrow breakpoints, relocates to the end of the thesis flow instead of disappearing with the rail. Browser/IAB QA covered desktop and 390×844 views, Chinese ↔ English language switching, page identity/non-blank DOM/no framework overlay, disabled Google control and empty `error`/`warn` console collections. A first visual pass found excessive English rail wrapping; the label was shortened and rechecked before acceptance as implementation QA.
- Rebuilt the Android release after the UI change. The first direct Gradle invocation demonstrated missing per-process JBR/SDK paths; a retry with the already-installed Android Studio JBR/SDK and the repository's Windows-only short-CMake init helper completed successfully. The helper only relocates generated CMake staging output and does not modify Android product/release configuration. The QA-only APK SHA-256 is `13EA89383B4F2706F3B8B735A3933BEECA9D308E8272C4A9E3AA1F020FDB7D1E`.
- Installed the rebuilt release on `emulator-5554` (Google Play API 36), resolved and launched `com.foresightnine.mobile/.MainActivity`, inspected its native UI tree and a release screenshot, then switched the UI to English. The tree confirmed `Anonymous research`, `Sign in with Google — Not configured` and `Sign in with GitHub — Not configured`, with both provider buttons `enabled=false`; the Android crash buffer was empty. This is local emulator evidence only, not Play/distribution evidence.
- Regression evidence: full `pnpm test` passes shared FM 16, Web 25 and mobile 9 tests; API pytest passes 108 tests with one existing Starlette deprecation warning. Workspace typecheck, Git-history/current-tree secret scan (301 blobs), boundary verification, 9WL fixture audit, real cutoff-safe WL panel FM audit and `git diff --check` pass.
- Phase 7 remains **not formally accepted**. Native Sol reviewer routing is still invalid/unobservable in this session, so this evidence does not constitute a Sol pass, does not authorize Phase 8 and does not authorize a commit/push.

## Phase 7 gate-support — GLM Coding MCP project-search boundary — 2026-08-24

- Added a server-only, fixed-host GLM Coding Plan MCP search candidate adapter. It is opt-in via `FORESIGHTNINE_SEARCH_PROVIDER=glm_coding_mcp`, retains client `SearchEvidenceBundle` fallback by default, carries Streamable HTTP MCP sessions, validates the advertised `webSearchPrime` schema and fails closed on unsafe URLs, protocol errors, missing server secrets and unparseable results.
- Search output is explicitly unverified `SearchCandidate` data. It is neither fetched nor passed into an LLM prompt; the existing canonical client-evidence submission path must retain, hash and grade a user-selected source before it can affect a Challenge.
- No live call was made: the server environment has no injected GLM secret. This change does not advance Phase 8 or replace the fresh Sol Phase 7 verdict.
- Added deterministic protocol/security tests for initialization, notification, session propagation, fixed endpoint, bounded query, absent secret, unexpected tool schema and private-result URL rejection. Configuration and operating limits are documented in `docs/security/glm-coding-mcp-search.md`.

## Phase 7 gate-support evidence — bilingual Web, GLM boundary, FM/9WL audit; pause requested — 2026-08-24

- Kept the approved plan sequence intact. The formal Phase 7 Sol acceptance remains unavailable because this session's native reviewer-routing metadata is invalid; no Phase 8/9 claim or GitHub push is made.
- Added the server-only `glm_coding` OpenAI-compatible provider preset. It pins the GLM Coding endpoint and host allowlist, accepts only the server environment secret, defaults to the requested model name, prevents redirect through generic provider variables, and fails closed when no secret exists. Its configuration guide explicitly forbids client-side/BYOK secret capture and requires a separate one-request, redacted live acceptance when a valid injected secret is available.
- Added a cutoff-safe real-panel FM audit against `D:\WL-data-final\panels\WL1_full.parquet` and a separate 9WL fixture audit. The FM audit consumed 20 historical assets through the actual shared client engine with no server allocation and no future rows. The 9WL audit verifies source SHA-256s, trigger text, stage-news reveal dates, baseline/horizon ordering, explicit leak/observed semantics, branch partition and joint-probability arithmetic for WL1–WL9. Neither audit is a forecast, return claim or public-release market-data assertion.
- Completed Web Chinese/English product-copy coverage, including persisted locale choice, translated worldline labels/nodes, form validation/a11y labels and a fixture-wrapper translator that keeps user-entered thesis text verbatim. A hydration guard prevents the initial local-draft write from overwriting a restored language preference.
- Browser/IAB QA found and fixed a real 208px side-rail defect: the language button visually overflowed into the main sheet and was not clickable. It now occupies its own in-rail row. Desktop and 390×844 mobile checks prove Chinese ⇄ English switching, restored locale persistence, non-blank page identity, translated worldline view, responsive navigation and empty browser `error`/`warn` collections. The current visual result remains aligned with the approved warm-paper/editorial concept; user input remains in its authored language by design.
- Focused verification passed: Web `typecheck` and 24 Vitest tests; GLM-provider tests (11); Phase 3 provider tests (25); shared FM tests (16); WL1 warm-up FM audit; 9WL timeline/probability audit; secret scan. Full workspace verification and a formal Sol pass remain the next gate before any commit/push.

## Phase 7 Android — technical device QA complete; formal Sol gate pending — 2026-08-24

- This entry supersedes the earlier **native-build-blocked** statements below. A reproducible Expo SDK 57 native build is now available on Windows after pinning Expo's compatible `react-native-reanimated@4.5.1` / `react-native-worklets@0.10.1`, adding `react-native-safe-area-context@~5.7.0`, and moving the pnpm virtual store to repository-relative `nm` to stay below the Windows CMake path limit.
- The Windows-only Gradle init helper places application and linked-module CMake staging output under a short external path. It changes no product/release configuration. The generated Android directory remains inside `apps/mobile/android`.
- Fixed two device-discovered P1 defects: Android gesture navigation could obscure the Challenge submit button (the shared screen now uses `react-native-safe-area-context`); and a JSON base64 archive envelope could not be restored through the Android AES bridge. Restore now decodes the envelope's base64 ciphertext to bytes before `AESSealedData.fromCombined`. Dev-only failure diagnostics disclose only a rejection stage, never keys, ciphertext, documents or caught errors.
- Emulator evidence on `emulator-5554` (Google Play API 36): thesis → mock challenge → local evidence → main/weak/strong worldline → client FM → FM Raw/Public Default switch → encrypted local archive → cold-start restore → public Android share sheet. The release APK additionally accepted `ACTION_SEND text/plain` and routed the text into the local evidence workspace without a network request.
- Visual/device artifacts are under `docs/qa/android/`: `phase7-release-home.png`, `phase7-challenge.png`, `phase7-worldline.png`, `phase7-portfolio.png`, `phase7-archive-restored-fixed.png`, their UI-tree XML companions, and `phase7-release-share-import.xml`. `view_image` inspected the release thesis, challenge, portfolio and restored archive states against the approved quiet/warm-paper concept direction.
- Builds: `:app:assembleDebug` and `:app:assembleRelease` both pass with Temurin 17, Android SDK 36, NDK 27.1.12297006 and CMake 3.22.1. The self-contained release APK is `apps/mobile/android/app/build/outputs/apk/release/app-release.apk` (97,484,137 bytes; SHA-256 `C36B9699BD2FDAA861BDC2CB343C87F676DEAA6A728A668F4E91ECD8A40F6EE1` at QA time). It was installed after Metro was stopped and `adb reverse --remove tcp:8081`; it launched and restored the encrypted archive with no `Unable to load script` or fatal Android log in the inspected tail.
- Verification: mobile `typecheck` and 9 focused Vitest tests pass; Android native archive save/restore and public share dispatch pass in the emulator; release launch and inbound share intent pass; root `pnpm test` passes 99 tests (one existing Starlette deprecation warning), `verify:boundaries`, Git-history/current-tree secret scan and `git diff --check` pass. The release artifact is **QA-only** because the generated project currently uses the Android debug keystore; it is not a Play/public-distribution artifact.
- Phase 7 is technically evidence-ready but **not formally accepted**: native Sol routing metadata is invalid in this session, so the mandated independent Sol reviewer cannot issue the required gate verdict. Phase 8 does not begin.

## Phase 7 Android — Sol fix-first remediation and blocked native gate — 2026-08-24

- Replaced the mobile-only branch fixture's incompatible probability values with one shared P(premise) range, a documented constrained weak/main/strong conditional simplex (20% / 55% / 25%) and independently recomputed joint ranges. Regression coverage now validates the three branches in both Chinese and English rather than accepting visually plausible but inconsistent numbers.
- Extended the mobile worldline fixture and UI with a data-as-of date, baseline/horizon, explicit `historical_reference_and_simulated` label, occurrence/reveal times, observable indicators, causal parents, asset anchors and source references. The timeline validator rejects future data, unordered/reveal-invalid nodes, and incomplete provenance.
- Replaced the portfolio's static NAV decoration with selected-path typed historical/simulated NAV points, start-boundary scenario return, running-peak drawdown and numeric maximum drawdown. The portfolio now carries exact raw/public market value, lot/FX cash remainder and units; the rounding remainder is transferred to the cash-equivalent row rather than discarded.
- Public Default now retains Raw independently while applying a 25% security cap, 35% aggregate high-volatility cap, stale/low-liquidity exclusion, explicit proxy mapping and per-row explanations. Cash is intentionally not treated as a security cap; a mode is flagged if it cannot be executed rather than being presented as an order.
- Closed the bilingual evidence/archive accessibility leakage found by Sol: Back/language labels, share metadata and privacy-safe export disclaimers now use the active locale. A raw Android shared URL is classified as user-supplied/unverified, is not independently reviewable, is never fetched, and cannot support a public fact.
- Verification after remediation: mobile TypeScript typecheck; 8 focused Vitest tests; Android Metro production export; root secret scan; `git diff --check`. No live LLM, broker, order, execution, custody, secret, or market-data call was made.
- Android Studio, API 36 SDK/platform tools/emulator and a Google Play API 36 AVD are installed and the AVD boots. The actual release Gradle build is still blocked before configuration because the official Gradle 9.3.1 distribution cannot be connected from this host, even after the generated wrapper timeout was raised from 10 s to 60 s. Therefore APK install, Share Intent, back/lifecycle, SecureStore/FileSystem native archive round-trip and the six required screenshot/view-image comparisons remain **unproven**; Phase 7 remains **not accepted**.

## Phase 7 Android — host implementation complete; native acceptance blocked — 2026-08-23

- Added `apps/mobile`, an Expo SDK 57 / Expo Router Android client that consumes the shared FM engine and design tokens. The implemented local-only loop covers bilingual draft input, visible counterargument/rubric/reservation states, client SearchEvidenceBundle import with an explicit API-search-unavailable state, weak/main/strong probability timelines, local `computeFm`, distinct FM Raw/Public Default allocations, risk/NAV summaries and privacy-aware export.
- Android text/URL receiving uses Expo's official `expo-sharing` config plugin and `app/+native-intent.ts`. Shared text is normalized locally and never fetched. The app remains mock-only: no GLM/OpenCode request, API key, market fetch, broker, order, execution or custody integration was added.
- Implemented a native archive adapter: AES-256-GCM from `expo-crypto`, its device-local key stored in `expo-secure-store`, and the sealed envelope in `expo-file-system`. Private evidence is excluded by default; opt-in is device-local only and public-safe export rejects it.
- Host verification passed: mobile typecheck; 6 focused mobile Vitest checks (validation, URL normalization/duplicates, i18n, worldline intervals, shared FM/Public Default, privacy envelope); Android Metro production export; root workspace typecheck/tests; secret scan; `git diff --check`. `expo config --type public` confirms SDK 57 and the text/plain/text/uri-list share configuration.
- `pnpm audit --prod` was also run. It reports two high-severity `image-size@1.2.1` parser DoS advisories via Expo SDK 57's Metro/React Native toolchain and one moderate `uuid@7.0.3` bounds-check advisory via Expo config tooling. Both are transitive build-tool dependencies, but they are release gates: no unreviewed major-version override was applied, and a compatible Expo/React-Native toolchain update or a reviewed vendor remediation is required before public release.
- Fresh Sol review returned **fix-first** with no P0: actual APK/native build, Android emulator/device flow, Share Intent dispatch, back/lifecycle behavior, AES/SecureStore/FileSystem round-trip, and six `view_image` screenshot comparisons are not proven. The host has no Java, Android SDK, ADB, emulator or Android Studio. The Android rows in `docs/design/fidelity-qa.md` therefore remain explicitly pending; Phase 7 is **not accepted** and Phase 8 must not start.

## Phase 6 Web visual gate — Sol accepted — 2026-08-23

- Closed the remaining visual-state gap from fresh Sol review: `fm_complete` now keeps the user on the completed FM audit page until “查看配置方案” is selected. This makes the factor table, FM-v1 formula note, Web Worker boundary and JSON-intermediate notice reachable in the actual flow.
- Added and inspected `apps/web/qa/screenshots/web-11-fm-complete-fixed.png` and `web-12-methods-fixed.png`; refreshed the portfolio desktop/mobile evidence as `web-09-portfolio-desktop-fixed.png` and `web-07-portfolio-mobile-p1.png`.
- Completed the single canonical Web comparison log in `docs/design/fidelity-qa.md`, including concept path, implementation evidence, viewport, mismatch/correction and verification result. Android rows remain pending for Phase 7.
- Verification after the FM visibility correction: Web 23 tests passed, typecheck passed, Vite build passed. No new provider, key, server-side FM, broker, order or custody path was introduced.

### Fresh Sol acceptance

- Verdict: **ship**; no P0/P1. Phase 6 Web is accepted. The reviewer verified the FM audit page is reachable, the refreshed 390×844 Public Default screenshot is semantically aligned, the canonical Web comparison log is complete, and the Browser/IAB console collections are empty.
- Accepted commits: `ac598a0` (Web workflow) and `84a02f2` (visual evidence gate correction). The repository has no configured remote, so no GitHub push is claimed.
- Remaining scope is explicitly deferred: Android implementation/QA, live OpenCode/GLM provider verification, production API/PostgreSQL wiring, dependency/security/licensing gates, and final Phase 9 release acceptance.

## Phase 6 Web P1 remediation and browser QA — 2026-08-23

- Closed the three functional findings from the Sol freeze review: branch conditional probabilities are exhaustive (`0.27 + 0.54 + 0.19 = 1.00`) and each reveal schedule is derived from the generated node IDs/dates; `allocationRows(null, ...)` is empty and JSON export reports `not_computed` without fabricated weights; D/E SearchEvidenceBundle entries may omit URL/publication date while A–C still require a safe HTTP(S) URL.
- Kept the full Web audit `evidenceHash` separate from the API-compatible `sourceMaterialHash`. The latter was checked against the Python API canonical hash: `cd3d44755bda8101435b8d21841f2297c1a14bc9ce1427a71b3a60269869f317`.
- Corrected the Raw/Public mode selector so its selected glyph, `aria-pressed`, panel copy and table caption stay aligned.
- Independent verification: Web 22 tests passed, typecheck passed, Vite build passed; API 71 tests passed with one unchanged Starlette TestClient deprecation warning; `git diff --check` and secret scan passed.
- Browser/IAB evidence now covers viewpoint → challenge → no-URL D evidence → reserved confirmation → three worldlines/probability ranges → client FM Worker → Raw/Public Default → stress selection → NAV/drawdown → methods/risk. Browser console `error`/`warn` collections were empty. New inspected screenshots: `apps/web/qa/screenshots/web-07-portfolio-mobile-p1.png` and `web-09-portfolio-desktop-fixed.png`.
- `view_image` inspection found the implementation visually consistent with the approved warm-paper/editorial concept for the inspected portfolio state at desktop and 390×844 mobile; the complete Web/Android concept matrix and fresh Sol verdict remain pending. Web uses deterministic mock/fixture mode; no live GLM/OpenCode provider claim is made.

## Phase 6 Web visual/interaction QA follow-up — 2026-08-23

- Repaired the reviewer-found medium-width collision: at approximately 1265px the research inspector is now hidden before the three-column probability cards can be clipped. The 1536px desktop inspector layout remains available.
- Browser/IAB smoke now covers the full client path with current viewpoint propagation, fail-closed 25/40 challenge state, 31/40 plus independently reviewable evidence normal confirmation, worldline premise/three branches, client FM completion, USD formatting, Raw/Public Default, stress selection, NAV interval and drawdown toggle. Fresh console `error`/`warn` collections were empty.
- Added and visually inspected current Web screenshots: `apps/web/qa/screenshots/web-01-thesis-current.jpg`, `web-02-challenge-current.jpg`, `web-03-worldline-current.jpg`, `web-05-portfolio-current.jpg`, and `web-06-nav-current.jpg`. The current IAB viewport is approximately 1265×709 rather than the 1536×1024 concept artboard; screenshots are therefore evidence for implementation QA, not a final pixel-perfect Sol pass.
- Web verification after this follow-up: 18 Vitest tests passed, typecheck passed, Vite production build passed, and `git diff --check` passed. No GLM/OpenCode key was read, stored, or used; the Web path remains deterministic mock/fixture mode.
- Phase remains **in progress**: Android, bilingual shared i18n, live OpenAI-compatible GLM provider boundary, API/PostgreSQL production wiring, security/dependency/licensing gates, and final Sol acceptance are not complete.

### Sol freeze review — 2026-08-23

- Verdict: **fix-first / frozen pause**, with no P0. This is not a Web pass or release-candidate claim.
- P1 blockers retained for the next continuation: `makeBranches()` conditional probabilities currently sum to `0.99` and its reveal schedule contains node IDs not present in the generated branch graph; `allocationRows(null, ...)` shows deterministic fallback allocations while the no-FM JSON state says no weights exist; and the Web evidence form currently requires a URL, so it cannot represent the approved no-URL D/unreviewable path. Web canonical-hash parity with the API is also not independently demonstrated.
- Formal concept comparison remains pending: current Web screenshot evidence is implementation QA at approximately 1265×709, with FM and Methods states not yet captured; Android has not entered implementation QA.
- This is the requested pause point. Do not advance to Android/API production work until the next continuation explicitly resolves these P1 items and obtains a fresh Sol review.

## Phase 6 Web P1 remediation — 2026-08-23

- Dynamic viewpoint input is consumed by challenge start, user reply remains editable and is retained in the transcript/export, and the worldline premise follows the current viewpoint. SearchEvidenceBundle fields remain explicit (`url`, `title`, `publisher`, `published_at`, `excerpt`, `user_note`, direction and grade); content hashes are retained. Evidence classification records D/E and `user_private`/`model_inference` as `independently_reviewable: false`; A/B/C can be independently reviewable only when a URL is present.
- Public Default preserves FM Raw and applies the 25% single-security cap, 35% high-volatility-class cap, long-only/fully-invested eligibility, historical/stale and low-liquidity exclusion, proxy mapping with reasons, deterministic FX snapshot, and lot-aware Raw/Public amounts, units and residual cash. These are deterministic fixtures; no live provider or API key is configured.
- Structured JSON/CSV export retains the thesis, challenge transcript, evidence audit fields, three worldline premises/anchors/reveals/invalidation data, local FM arrays/results when available, Raw/Public allocation fields and reasons, method/data labels, risk disclosures, and complete NAV historical/weak/main/strong/interval/drawdown arrays. The NAV drawdown control has a real toggle handler.
- Verification evidence: Web has 16 passing Vitest tests; direct typecheck, Vite build and `git diff --check` passed. A fresh Browser/IAB session DOM check recorded empty `error`/`warn` evidence. The implementation remains deterministic fixture mode, with no live provider/API key.
- Fidelity limitation: the screenshot command is unsupported in the current IAB session, so no screenshot artifact was generated or inspected. Fidelity comparison rows remain **pending**; this entry is not a Sol pass.

## Phase 6 Web implementation — 2026-08-23

- Added the strict React + Vite Web MVP under `apps/web`, consuming the shared contracts, domain/design tokens, and FM engine. The local workflow covers draft restore and validation, challenge retry/cancel/recovery, SearchEvidenceBundle fixture import, worldline probability ranges and selection, client-side FM computation through a real Web Worker, FM Raw/Public Default deltas, lot-aware allocation/residual states, historical/simulation NAV and drawdown SVG charts, methods/risk disclosure, and JSON/CSV export.
- Added deterministic Web tests for thesis validation, challenge state recovery, worker/FM output retention, Raw/Public capping, small-capital residuals, and exports. Focused Web typecheck/build/test and diff checks pass; screenshot-based fidelity evidence remains pending because no screenshot artifact was committed.
- Material judgment: the default mode is deterministic offline fixture data with explicit historical/simulated/model-estimate labels; no broker, order, custody, live-network, or secret-bearing affordance was added.

## Phase 4 Sol gate remediation — 2026-08-15

- Closed synchronous unknown-provider failures in `OperationManager.execute`: ordinary exceptions now append the existing safe `operation.error`, transition to `failed`, retain the idempotent terminal error, release single-flight waiters, and remove pending execution state without exposing exception text.
- Bound accepted typed worldlines to the retained thesis and request: every path must retain the thesis premise, fixed API baseline date, requested horizon, bundle/challenge identity, and the existing probability partition validation before any worldline is persisted.
- Tightened worldline graph semantics in both API and domain validation: causal parents must exactly match incoming edges; schedules are non-empty, cover every node exactly once, use each node's event date, and retain the documented early-reveal `leak` exception only.
- Replaced the 9WL index with an auditable deterministic regression fixture. Each record carries source paths/SHA-256 values, copied title/trigger and stage-news reveal date, normalized event-date rationale, and complete synthetic weak/main/strong test branches. The fixture explicitly disclaims forecasts and does not present its probabilities as source estimates.

## Phase 4 provider-boundary gate reinforcement — 2026-08-15

- Revalidated every typed `WorldlineBundle` at `POST /v1/worldlines` before persistence: canonical schema, request challenge identity, bundle/worldline identity, worldline time/graph/reveal constraints, and weak/main/strong conditional partition plus joint-probability semantics.
- Revalidated typed `ProbabilityAssessment` results at `POST /v1/probabilities/assess` with the canonical schema, requested-worldline identity, and joint-probability semantics. Boundary failures now use the existing safe failed-operation terminal path without returning provider diagnostics.
- Added fake typed-output regressions for challenge/bundle identity, nested asset-impact schema, reveal-horizon, conditional partition, wrong-worldline and invalid-joint probability assessments; rejected bundles persist no worldlines.

## Phase 4 Worldlines and Probability — 2026-08-15

### Delivered

- Added provider-neutral `WorldlineGenerator` and `ProbabilityAssessor` interfaces, with deterministic no-key mock defaults. Worldline generation now derives its premise from the stored thesis belonging to a `CONFIRMED` challenge; the API rejects caller-supplied premise/path fields.
- Canonical worldline contracts now include node event/data-as-of dates, embedded asset impacts, and three attached probability assessments for weak/main/strong paths. Contracts, generated TypeScript, and OpenAPI are regenerated together.
- Enforced schema-plus-semantic gates for acyclic graph shape, edge-time monotonicity, baseline/horizon containment, no future data references, event/reveal rules, exact interval/scalar joint recomputation, shared premise probabilities, and conditional branch partition consistency.
- Added one 9WL regression fixture containing nine reference-derived scenario records, each with weak/main/strong expected structure, probability decomposition and event/reveal schedule expectations; added API/domain coverage including unknown-provider fail-closed behavior.

### Material judgment and limit

- The fixture copies only minimal scenario titles and trigger timing from the read-only 9WL source. Its probability values are deliberately deterministic test inputs, not historical estimates or forecasts.
- Live OpenCode smoke remains **blocked**: no newly authorized server-side provider secret is available. No live-provider claim is made; deterministic mock completion is the verified path.

## Phase 3 formal FAIL remediation — 2026-08-15

### Delivered

- Reworked the OpenCode 1.3.0 adapter for raw JSONL events while retaining only a compatibility path for single-envelope mocks. It now extracts final structured text/payload and usage, and establishes native-search provenance only from actual `tool_use.part → part.tool` `search`/`websearch` telemetry with `part.state.status` `completed`/`success`; generic `capabilities`, `tools`, `search`, and `results` fields never establish provenance.
- Added an opt-in asynchronous Phase 3 message endpoint. It returns the existing typed operation receipt before provider completion; `StreamingResponse` supplies replay, typed terminal events and keepalive heartbeats. Cancel signals the actual provider runner event before state commit. Existing synchronous endpoints remain synchronous and unchanged.
- Made `OperationManager.execute` endpoint-plus-idempotency-key single-flight under concurrency. Same-body callers wait and replay the one submission; mismatch remains a conflict. A Barrier regression test proves one provider call.
- Made evidence-backed follow-up messages return `CHALLENGING` only when both draft evidence gaps and unresolved issues are clear. Normal confirmation now has an audit regression proving preserved citations/provenance after a valid public-evidence turn.
- Promoted `citation_ids`, `public_fact_citation_ids`, `native_search_used` and server-derived `provenance` into required canonical `ChallengeRecord` fields, regenerated TypeScript and OpenAPI, and retained attached-evidence grade validation.
- Tightened usage so `total_tokens` is an exact non-negative Python integer and `cost_usd` is a finite, non-negative non-bool integer/float; rejected bool, NaN, infinity and negative values. Expanded prompt-control detection to forget-prior, approval and compliance forms across claims and untrusted evidence strings.

### Verification and limit

- `uv run --project apps/api pytest apps/api/tests`: **51 passed** (one unchanged third-party TestClient deprecation warning), including JSONL, single-flight Barrier, provider-blocked SSE/cancel and terminal async-replay tests.
- Live smoke remains **blocked**: no new authorized server-side provider key is present. No historical key was used and no DeepSeek-live claim is made.

## Phase 3 Sol FAIL remediation — 2026-08-15

### Corrections and regression evidence

- Replaced static OpenCode search capability with fail-closed runtime telemetry verification: only a real successful `tool_use.part → part.tool → part.state.status` search event establishes provenance; missing qualifying telemetry forces client fallback.
- Added canonical-hashed full client evidence bundles, C/not-yet-verified URL evidence, D/no-URL evidence, publication-time enforcement and legacy IPv4/IPv6 SSRF rejection.
- Made draft fields, rubric objects and citation subset relations strict; D/E cannot support public facts. Normal confirmation additionally requires independently reviewable evidence, while reservation confirmation is unchanged.
- Added pre-call budget reservation, finite non-negative usage validation, retry only for 429/5xx/timeout, and immediate non-transient CLI failure.
- Registered running operations and cancellation events before provider execution; cancel reaches the live callback, prevents commit/re-submit, and explicit resume uses that callback rather than synthetic completion.
- `uv run --project apps/api pytest apps/api/tests`: **35 passed** (one unchanged third-party warning). Live smoke remains **blocked by missing new server-side secret** and is not represented as DeepSeek-live verification.

## Phase 3 independent review remediation — 2026-08-15

- Removed unsafe provider re-execution from resume. Cancelled/failed operations now retain their terminal audit checkpoint, replay without a second submission/charge, and do not emit synthetic completion events; a genuinely new user command is required for another provider attempt because OpenCode CLI exposes no auditable charge receipt.
- Rebuilt `evidence_ids` and `evidence_grades` from all attached challenge evidence on every later message turn.
- Tightened native-search provenance to actual completed/successful OpenCode `tool_use.part → part.tool → part.state.status` telemetry. Generic `capabilities`, `tools`, `search`, and `results` fields are all rejected as non-provenance, including result-shaped payloads.
- Caught `ApiProblem` from `build_result`, records typed `operation.error`, transitions to `failed`, and idempotently replays that error rather than leaving a running operation.
- Regression suite: `38 passed`, including provider submission-count assertions across failure/cancel/resume, message evidence propagation, native-search false positives and `ApiProblem` lifecycle closure.

## Phase 3 Challenge and Evidence — 2026-08-15

### Delivered

- Added a provider-neutral `LlmProvider` and `SearchEvidenceProvider`, with deterministic mock as the default and an opt-in OpenCode DeepSeek Flash adapter using the required CLI model form. The adapter never reads, prints, stores or forwards credentials; it applies a concurrency-one gate, rounds/token/cost budgets, timeout/cancel termination and bounded exponential backoff for 429/5xx.
- Added fail-closed structured-output handling: malformed JSON, prompt-control text, incomplete/invalid 10-dimension rubric output, unattached citations and D/E evidence promoted as public facts all produce no challenge record. Accepted output is rebuilt as and validated against the existing canonical `ChallengeRecord`.
- Added client evidence fallback URL safety: HTTP(S), size, user-info, port and literal private/reserved-IP guards, plus a redirect-target DNS validation hook for any future fetcher. The API never server-fetches client evidence. Missing URL stays grade D/unreviewable and leaves challenge evidence requirements open.
- Preserved the Phase 1 normal-confirmation gate and extended typed SSE/Last-Event-ID/cancel/resume/idempotency behavior for provider failures: a failed original request replays its failure without re-submitting; explicit resume retains the operation ID and makes the original request replay the validated result. No canonical schema expansion or generated-contract change was necessary.
- Added reviewed JSON fixture and tests for mock end-to-end fallback, OpenCode success/no-search capability, 429, 502, timeout, cancel, invalid JSON, prompt injection, citation mismatch, D/E public-fact rejection and SSRF/redirect checks.

### Verification and limitation

- `uv run --project apps/api pytest apps/api/tests`: 28 passed; one unchanged third-party Starlette TestClient deprecation warning.
- Live smoke is **blocked by missing server secret** in this environment. The OpenCode path is implemented but is not represented as DeepSeek-live verified; deterministic mock acceptance is the verified path.

## Phase 2 strict-decoder and manifest-closure fix — 2026-08-15

Sol's second review found two false-negative paths in `af9b473`: `pngjs` alone accepted a valid-CRC PNG whose zlib header was invalid, and the registry did not reject duplicate paths or all required/variant/archive role drift.

### Fix

- Added `scripts/verify/design-png-guards.mjs`: verifies every chunk CRC and order, restricts the approved 8-bit non-interlaced encoding, inflates the concatenated IDAT stream with `inflateSync(..., { info: true })`, requires all compressed bytes to be consumed, and checks the exact filtered scanline length before `pngjs` decodes RGBA pixels.
- Added `scripts/verify/design-assets-regression.mjs` to construct in memory, with valid PNG CRCs, an invalid zlib header and trailing compressed bytes; both are required to fail the guard.
- The manifest gate now consumes `required_web`/`required_android`, rejects duplicate registry paths and duplicate variants, validates roles against the exact required/variant/archive path sets, and rejects role overlap, missing files and unregistered files.
- Added an explicit `archive_paths` manifest list so archive membership is independently checked in both directions against the registry role set.
- Added PNG `PLTE` ordering, uniqueness, length and grayscale-compatibility guards; the regression suite now rejects a legal-CRC `PLTE-after-IDAT` stream.

### Verification

- `corepack pnpm verify:phase2`: passed all 35 pixel decodes, exact dimensions, required/variant/archive closure, and both corruption regressions.
- Manual proof: a legal-CRC invalid zlib header is rejected with `incorrect header check`; no file is written by the regression test.

## Phase 2 verifier hardening — 2026-08-13

### Blocker

Sol's independent Phase 2 re-review of commit 2d5cf57 returned FAIL: the PNG verifier checked only the signature, IHDR, dimensions and the presence of an IEND marker anywhere in the byte stream, without decompressing IDAT. A corrupted pixel stream still produced PASS. Two archive PNGs with non-standard Android dimensions (916x1717, 864x1821) were also accepted without being registered as historical exceptions.

### Fix

- Added an authoritative assets registry to phase2-manifest.json: all 35 PNGs (13 required, 4 variant, 18 archive) each with exact expected width, height and role. The verifier now refuses any on-disk PNG that is not registered and any registered PNG that is missing from disk.
- Rewrote design-assets.mjs to decode every pixel via pngjs PNG.sync.read. A corrupt or truncated IDAT stream, bad CRC or incomplete RGBA buffer now throws and fails the gate. Verified by a synthetic corruption test: PNG.sync.read throws on bad compressed data, where the old verifier returned PASS.
- Strict dimension comparison: every decoded PNG width and height must exactly match its manifest entry. The two non-standard archives are registered at their true historical dimensions and reported as archive historical exception; they are not subject to the 9:16 artboard rule but are still pixel-decoded and dimension-checked.
- Added pngjs and @types/pngjs as workspace dev dependencies.

### Verification evidence

- corepack pnpm install --frozen-lockfile and uv sync --project apps/api --frozen: passed.
- corepack pnpm typecheck: passed all five typed workspace projects.
- corepack pnpm verify:phase0: passed six immutable FM references, all governance boundaries, 119 unique Git-history blobs and the current-tree secret scan.
- corepack pnpm verify:phase1: passed generated contract/registry/OpenAPI drift checks, 4 domain tests, 2 registry tests and 20 API tests.
- corepack pnpm verify:phase2: passed 35 pixel-decoded PNG assets (exact dimension match), 13 required concepts, 4 state variants, 2 registered historical-exception archives, all specifications, contrast checks and design-system typecheck.
- corepack pnpm test: passed contracts, design-system, 4 FM parity tests, 4 domain tests, 2 registry tests and 20 API tests.
# Progress

# Phase 5 FM engine — implementation handoff — 2026-08-15

### Sol P1 remediation complete — follow-up commit

- Corrected forecast scaling to average `|IC|` only over factor IDs that actually
  contributed to the support-weighted composite; added a regression proving an
  unused selected factor cannot change forecast values or dead-band decisions.
- Changed `forwardReturns` to typed `{asOf, values}` rows. Every row must align to
  its observation timestamp and be no later than `cutoff`; future and mismatched
  rows fail closed before metrics evaluation. Plain `number[][]` is no longer part
  of the TypeScript contract and is rejected at runtime as malformed input.
- Selection now skips non-finite `icMean`/`icir` metadata before `maxFactors`
  truncation; NaN/Infinity-only inputs retain explicit equal-weight fallback while
  stable finite-quality ties remain input ordered.
- `computeFm` now preserves the known asset width when no factor contributes: it
  returns width-shaped NaN composite/forecast arrays, explicit equal weights,
  fallback reasons, and a normal initial allocation/rebalance path.
- `allocateCapital` now rejects non-finite or non-positive price, FX and lot
  values, as well as non-finite/negative capital and weights, before any arithmetic.

### Delivered

- Added the pure client `computeFm` orchestration API with intermediate factor
  signals, selected IDs, combination/support, weights, forecast scale, forecasts,
  metrics, rebalance/dead-band result, and explicit reasons. Every observation is
  cutoff-checked before sorting/evaluation; future rows are rejected.
- Corrected factor-quality ties to Python stable input ordering and retained NaN
  support semantics, equal-weight fallback, free initial allocation, 3 bps default
  subsequent transfer, strict dead-band comparison, FX conversion, lot rounding,
  and canonical hashing.
- Added the finite AST DSL and browser Worker adapter. Core source contains no
  Node/DOM imports; the Android-sized test imports and executes the same package
  entry with 15 assets.
- Reworked the Python fixture generator to hash all six immutable references and to
  emit null for non-finite values, input orientation, cutoff data, formula cases for
  combination/portfolio/metrics/backtest, and the client-bound walk-forward/ASSETS
  subsets. Import failure is visible and `--require-reference-import` is fail-closed;
  normal `--check` is deterministic because transient exception text is not stored.
- Added [ADR-006](architecture/ADR-006-fm-engine.md) with the formula mapping,
  client-only boundary, Worker/Hermes compatibility, oracle mode, and limitations.

### Verification evidence

- `Set-Location D:\Foresightnine\src; uv run --project apps/api python scripts/fm/generate_oracle.py --check`: passed byte-identical fixture and all six hashes. The local environment reports `ModuleNotFoundError: No module named 'matplotlib'` and uses the documented copied-formula oracle mode.
- `Set-Location D:\Foresightnine\src; corepack pnpm --filter @foresightnine/fm-engine typecheck`: passed.
- The preceding Phase 5 implementation suite passed 14 tests, 0 failures; this
  follow-up adds two deterministic regressions, so the current suite passes 16
  tests, 0 failures, covering Python parity, reference cases, AST rejection,
  cutoff, Worker, Android-sized smoke, orchestration fallback, finite numeric
  guards, 3 bps, dead-band, FX, and lot checks.

The workspace and Phase 0/1/2 gates were run for the preceding Phase 5 commit and
remain green in the current checkout; the full reference-package import remains
unavailable in the minimal `uv` environment and is the only known oracle limitation.

## Phase 2 reviewer corrections complete — 2026-08-10

### Delivered

- Corrected the Web FM concept to the executable pipeline: one-based percentile rank minus 0.5, IC-sign direction, global factor quality `|IC|×|ICIR|`, per-asset valid-support combination, population-standard-deviation z-score, clipping and final long-only re-normalisation.
- Corrected evidence grade C to secondary commentary/expert interviews; it is no longer described as a company filing.
- Corrected Android Raw and Public Default NAV horizons to a coherent `2024-08 → 2026-08-10 today → 2028-08-10 (+24m)` boundary.
- Retained every replaced concept in an `archive` directory. The active suite contains 13 required full-state concepts and four state variants: Web/Android SSE recovery and Web/Android Public Default delta.
- Strengthened Phase 2 verification to inspect every PNG recursively, including variants and archives, and to enforce the required Web and Android artboard dimensions for both primary and variant states.
- The standing user instruction to accept all proposed solutions and continue through completion covers this reviewer-directed correction pass; its scope and source are recorded in `phase2-manifest.json`.

### Verification status

- `corepack pnpm install --frozen-lockfile` and `uv sync --project apps/api --frozen`: passed with the checked-in lockfiles.
- `corepack pnpm typecheck`: passed all five typed workspace projects.
- `corepack pnpm verify:phase0`: passed six immutable FM references, all governance boundaries, 119 unique Git-history blobs and the current-tree secret scan.
- `corepack pnpm verify:phase1`: passed generated contract/registry/OpenAPI drift checks, 4 domain tests, 2 registry tests and 20 API tests.
- `corepack pnpm verify:phase2`: passed 35 recursively inspected PNGs, 13 required concepts, 4 state variants, all specifications, contrast checks and design-system typecheck.
- `corepack pnpm test`: passed contracts, design-system, 4 FM parity tests, 4 domain tests, 2 registry tests and 20 API tests.
- Original-resolution visual inspection passed the corrected FM equation, evidence-grade C definition and both Android NAV horizons. Formal Sol re-review remains the phase-gate authority.

## Phase 1 Sol confirmation blocker remediated — 2026-08-10

### Delivered

- Reproduced the blocker at commit `1af8069`: an initial `CHALLENGING` record with a 20/40 rubric accepted ordinary `CONFIRM` with `unresolved_issues_acknowledged=false`, then confirmation-derived provider logic rewrote the rubric to 30/40.
- Ordinary `CONFIRM` now requires a rubric total of at least 30/40; `premise_clarity`, `time_boundary`, `causal_chain`, `asset_transmission` and `falsifiability` must each be at least 3/4; `missing_evidence` and `unresolved_issues` must both be empty. Failure returns structured 409 code `confirmation_requirements_not_met` with per-gate details and leaves the challenge and turn counter unchanged.
- `CONFIRM_WITH_RESERVATIONS` is an explicit pre-confirmation command from `CHALLENGING` or `EVIDENCE_REQUIRED`. Canonical validation requires `unresolved_issues_acknowledged=true` and a non-whitespace reservation note.
- Confirmation no longer invokes the research provider. It copies the current claims, scoring, missing evidence, unresolved issues, AI objections, questions and private-evidence declaration unchanged; only the new turn, confirmed status, user confirmation, optional reservation note, provenance and content hash are written.
- The deterministic mock review becomes rubric-passing only after independently reviewable evidence is accepted. Provider scoring no longer depends on `CONFIRMED` status.
- Added direct early-confirmation rejection, false acknowledgment/missing/whitespace note, reservation preservation, no-rewrite and passing ordinary-confirmation regression coverage. Updated the shared domain reservation command for both pre-confirmation review states.
- Added the canonical error code and non-whitespace note constraint, then regenerated TypeScript contracts and both 20-path OpenAPI snapshots. Canonical schema SHA-256 is `deb077db03cd740cf9d7e2d618ad5fc9c5610bdd4ac9e6fe0f1f8c26fa48d01d`.

### Verification evidence

- `corepack pnpm install --frozen-lockfile`: passed; all 6 workspace projects already up to date.
- `uv sync --project apps/api --frozen`: passed; 32 packages checked.
- `corepack pnpm typecheck`: passed all 5 typed workspace projects.
- `corepack pnpm verify:phase1`: passed generated-contract/OpenAPI drift gates, 4 domain tests, 2 registry tests and 20 API tests.
- `corepack pnpm test`: passed contracts, design-system, 4 FM parity tests, 4 domain tests, 2 registry tests and 20 API tests.
- The existing third-party Starlette `httpx2` migration warning remains non-failing and unchanged.

### Scope preservation

- This remediation did not edit design concepts, FM implementation/fixtures, governance files, root plans or any declared read-only source.
- No Git commit was created; integration remains with the main thread.

## Phase 1 gate complete — 2026-08-10

### Delivered

- Established `packages/contracts/schemas/v1/contracts.schema.json` as the sole Draft 2020-12 authority. The previous entity/research files are compatibility references only.
- Generated strict TypeScript contracts (schema SHA-256 `8763a1c15fde062aba0978226c41c3dfd899980eae0bd90b8f51750f8eab4b49`), compiled Pydantic models directly from the schema, and exported a 20-path OpenAPI 3.1 snapshot with fully resolvable local references.
- Added strict unknown-property rejection to core entities, nested records, requests, responses and errors. Every core entity composes `schema_version`, `created_at`, `source_refs` and `content_hash`.
- Added premise, conditional and joint probability scalars plus intervals, ordering/containment/product invariants, and explicit `model_estimate_unvalidated` mock status.
- Added a generated 36-entry asset registry covering all 15 benchmark economic exposures, explicit tradable proxies, global equity/bond/commodity products, BTC/ETH, REIT and cash. Registry source, snapshot and nested hashes are drift-gated.
- Added versioned market manifest/shard, factor manifest/shard with whitelist postfix tokens and admission thresholds, and client-computation attestation contracts/endpoints. Market mock data is explicitly `historical_only`; final portfolio computation remains client-side.
- Completed all planned API endpoints plus canonical request/success/error payloads. Every POST requires `Idempotency-Key`; same-body retry returns the exact operation/result after state advancement, while body conflict returns 409.
- Added deterministic fixed clock and content-derived session/thesis/challenge/worldline/operation IDs, operation/cancel/resume tokens, typed SSE started/progress/heartbeat/error/complete events, and suffix replay via `Last-Event-ID`.
- Enforced the approved lifecycle graph. Early confirmation requires the explicit `CONFIRM_WITH_RESERVATIONS` command, acknowledged unresolved issues and a non-empty reservation note.
- Added content-hashed versioned A–G acceptance fixtures and TS compile positive/negative gates, domain state/probability/worldline tests, registry tests, Python schema/model/API/operation tests, OpenAPI resolver tests and generation drift tests.
- Added `verify:generated-contracts` and a real `verify:phase1`; root `test` executes all workspace tests plus the Python API suite.

### Verification evidence

- `corepack pnpm install --frozen-lockfile`: passed; 6 workspace projects, lockfile current.
- `uv sync --project apps/api --frozen`: passed; 32 packages checked.
- `corepack pnpm typecheck`: passed for contracts, domain, asset-registry, design-system and the existing fm-engine.
- `uv run --project apps/api pytest apps/api/tests`: **18 passed**; one third-party Starlette warning recommends migrating its TestClient from `httpx` to `httpx2`.
- `corepack pnpm verify:phase0`: passed 6 immutable reference hashes, all 3 governance hashes/boundaries and secret scan.
- `corepack pnpm verify:phase1`: passed generated TS/registry/OpenAPI drift checks, 4 domain tests, 2 registry tests and 18 Python tests.
- `corepack pnpm verify:phase2`: passed 13 approved concepts, 4 variants, specification files, contrast checks and design-system typecheck.
- `corepack pnpm test`: passed contracts generation/type tests, 4 existing fm-engine parity tests, 4 domain tests, 2 registry tests, design-system typecheck and 18 Python tests.

### Preserved concurrent work

- No source or test file under `packages/fm-engine` was edited; its existing parity suite was only executed by required root verification.
- Existing Phase 0 governance/reference/boundary/secret scripts and Phase 2 concepts/design-system work were retained and passed unchanged gates.
- `plan.md`, `agent.md`, `GOAL_PROMPT.md` and all declared external reference/source roots remained read-only; Phase 0 boundary hashes confirm this.

### Known limits / later-phase obligations

- The Phase 1 stores and SSE replay log are process-local. PostgreSQL/Redis persistence must preserve the tested wire semantics before public operation.
- Integrity records use a clearly labelled deterministic mock key ID, not production signing. Live provider/search behavior, rate limits, abuse controls, licensed current market data and production key management remain later phases.
- The Starlette TestClient deprecation warning is dependency-owned and non-failing; dependency migration should be handled with a lockfile-reviewed toolchain update.

The older baseline below is retained as historical evidence and is superseded where it described interval-only probability, incomplete endpoints, or unavailable streaming.

## Phase 0/1 baseline — 2026-08-10

### Delivered

- pnpm workspace and TypeScript strict baseline, with contracts and domain packages.
- `v1` JSON Schema/OpenAPI/TypeScript contracts for core research entities and API outputs.
- Explicit research lifecycle state machine plus probability interval and worldline DAG invariants.
- `uv`/FastAPI skeleton with schema-validated mock Challenge, Worldline, Probability, session, evidence, asset, health, and methodology endpoints.
- Contract tests that validate live FastAPI mock responses against the shared JSON Schema.
- Docker API skeleton and two architecture ADRs.

### Material judgment calls

- No key or real provider was configured; the approved mock fallback is the only active provider.
- Probability remains interval-only and declares `model_estimate_unvalidated` to avoid pseudo precision.
- Failure states are not persisted as a new lifecycle enum: an operation failure retains its durable prior state and can retry, preserving the approved state-machine vocabulary.

### Evidence and verification

- `corepack pnpm typecheck`: passed for `@foresightnine/contracts` and `@foresightnine/domain`.
- `uv run pytest` from `apps/api`: **5 passed**. A third-party FastAPI/Starlette TestClient deprecation warning remains; it does not affect contract results.
- Credential-like assignment scan of authored source/config/docs (excluding dependencies, virtual environments, generated output, and lock files): no matches.

### Known limits / next phase

- No persistence, streaming, rate limit, search adapter, real market data, FM engine, Web, or Android implementation yet.
- OpenAPI is the public endpoint description; generated client artifacts will be added with the consuming clients.

