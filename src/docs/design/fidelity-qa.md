# Design fidelity QA

## Phase 9 release-candidate boundary — 2026-08-29

This section separates completed implementation evidence from the final production gate. The Web and Android artifacts below are QA evidence: deterministic/mock behavior, historical fixtures, and a debug APK are not production evidence and must not be described as such.

The historical comparison tables below are retained as an audit trail. Their older `Sol pending` labels and runtime descriptions do not override this Phase 9 section or the dedicated Android Metro smoke record.

### Completed Web and Android implementation evidence

- Web Browser/IAB and local-browser runs exercised the thesis → challenge/evidence → worldline → client FM → Raw/Public Default → NAV/export path. Browser console `error`/`warn` collections were empty in the recorded runs. The current captures were inspected with `view_image` and are retained under `apps/web/qa/screenshots/`, including `web-01-thesis-current.jpg`, `web-02-challenge-current.jpg`, `web-03-worldline-current.jpg`, `web-06-nav-current.jpg`, `web-09-portfolio-desktop-fixed.png`, `web-11-fm-complete-fixed.png`, `web-12-methods-fixed.png`, `web-14-console-desktop.png`, and `web-15-console-mobile.png`.
- The Web Phase 8 freshness/Public Default state was also inspected with `view_image` at `docs/qa/web/phase8-public-default.png`; unavailable provider freshness correctly disables current Public Default amounts and leaves residual cash visible. The available static QA bundle is `apps/web/dist/`, not a production deployment.
- Android API-36 emulator evidence was exercised in the documented Metro + `adb reverse tcp:8081` debug/dev-client mode, including thesis, mock challenge/reservation, local FM, fail-closed provider behavior, and disabled Public Default amounts. `view_image` inspected `docs/qa/android/phase8-home.png`, `phase8-provider-fail-closed.png`, `phase8-public-default.png`, and the current re-install capture `phase9-current-home.png`; UI-tree evidence includes `phase8-home.xml` and `phase9-current-home.xml`. The APK used for this evidence is `apps/mobile/android/app/build/outputs/apk/debug/app-debug.apk`, which is a debug QA artifact only. This does not prove an independently self-contained APK run without Metro; a signed release build remains a separate gate.
- The API evidence is a deterministic mock run, not a live provider run. The rerunnable command is `uv run --project apps/api pytest apps/api/tests`; the latest recorded result is 124 API tests passed, as documented in `docs/progress.md`.

### Final production gate still missing

- No final production fidelity/launch gate is claimed. The Web bundle is an ignored static QA output, the Android package is debug-signed, and the API path is deterministic/mock unless a separately approved server deployment supplies a provider.
- The remaining production gates are external or deployment-owned: legal/privacy/terms/risk and investment-advice review; market-data and FX licensing; server-side secret-manager injection with bundle/source-map/log/database scans; CAPTCHA, abuse quotas and Redis operations; UUID/security/dependency/container/CORS/CSP/SSRF review; observability, incident response, kill switches, rollback, retention/deletion approval; signed/fresh provider manifests; staged smoke/abuse/parity/rollback tests; and Android release signing, identity, EAS/Play, privacy and artifact provenance approvals.
- Until those gates are recorded by the release owner, neither the QA screenshots nor the mock/debug artifacts may be used to claim public production readiness.

## Latest Web remediation evidence — 2026-08-23

- The previously observed Public Default selector mismatch was corrected: the selected mode now has the selected glyph and `aria-pressed`, and the table caption/copy agree with the mode.
- Inspected implementation screenshots: `apps/web/qa/screenshots/web-09-portfolio-desktop-fixed.png` at the default desktop viewport and `apps/web/qa/screenshots/web-07-portfolio-mobile-p1.png` at 390×844. Both preserve the approved warm-paper/editorial rhythm, restrained terracotta/sage/blue semantics, single-column mobile flow and explicit Raw → Public Default explanation.
- Functional/browser evidence covers the complete Web path and console `error`/`warn` collections are empty. The Web portfolio row is **evidence-ready**; fresh Sol verdict `ship` accepted the complete Phase 6 Web gate on commit `84a02f2`.

## Historical Web implementation evidence — 2026-08-23

- A fresh Browser/IAB run produced and inspected current Web screenshots at the active browser viewport (approximately 1265×709): `apps/web/qa/screenshots/web-01-thesis-current.jpg`, `web-02-challenge-current.jpg`, `web-03-worldline-current.jpg`, `web-05-portfolio-current.jpg`, and `web-06-nav-current.jpg`.
- The screenshots confirm the approved warm-paper/editorial shell, adversarial challenge/evidence layout, interval-first probability cards, Raw → Public Default delta view with stress output, and historical/simulated NAV with labelled today boundary and drawdown section. The medium-width inspector collision was corrected and rechecked in `web-03-worldline-current.jpg`.
- DOM checks for thesis → challenge → worldline → client FM → portfolio → NAV/export found no browser `error` or `warn` entries. This is implementation evidence; it does not by itself approve the remaining Android or final 1536×1024 concept comparison.
- At the time of this entry, Web fidelity rows were **evidence available / formal comparison pending** because FM and Methods captures had not yet been added. The canonical comparison log and later Sol `ship` verdict supersede that interim status; Android rows remain **pending**.

## Historical Sol freeze review — 2026-08-23

- Verdict: **fix-first**, no P0; the current screenshots are retained as implementation evidence only.
- The formal fidelity gate is paused with the Web functional P1 blockers recorded in `docs/progress.md`: strict worldline probability/reveal-schema closure, no-FM allocation/export semantics, and no-URL D evidence flow. FM and Methods screenshots, exact concept-artboard comparison, and all Android comparisons remain pending.

## Concept review — 2026-08-10

Reviewer: implementation lead, pending formal Sol phase review.

User approval: explicit. The user stated that all proposed solutions were accepted and execution should continue until completion. This approval was given after the first concepts were visible and while the complete suite was being produced; no additional visual pause is required.

### Passed

- Seven complete Web states and six complete Android states exist; the suite covers the full thesis-to-export product surface rather than a landing-page hero.
- The visual identity is original: ForesightNine wordmark, warm paper palette, terracotta/sage/dusty-blue semantics and editorial document rhythm. No Anthropic or Claude name, logo or illustration appears.
- Thesis input remains the primary entry point and requires only a viewpoint and capital.
- Challenge screens expose adversarial questions, rubric progress, evidence access and private-premise warning.
- Evidence grades A/C/E are visually distinct and the copy explains that grade does not equal truth.
- Worldline views show weak/main/strong alternatives, occurrence versus market-visible time, invalidation and all three probability layers.
- FM progress is explicitly local, identifies Web Worker execution, exposes intermediate steps and avoids a server-calculation affordance.
- Portfolio screens preserve an explicit FM Raw/Public Default switch.
- Web and Android portfolio concepts include selected-worldline NAV and drawdown. A labelled today boundary separates historical data from simulated paths; the median and uncertainty interval are visually distinct.
- Risk, no-broker/no-order/no-custody and no-return-promise disclosures are visible at the decision points where a user could otherwise over-interpret results.
- Mobile concepts use single-column task flows and large controls rather than shrinking the desktop canvas.

### Concept correction completed

- The first Android portfolio concept used iOS-like device chrome and mixed an NAV title with a currency y-axis. It was retained under `concepts/android/archive/05-portfolio-nav-v1.png`, edited from the local original, and replaced by a corrected Android concept with three-button navigation and normalized 0.7–1.5 NAV levels.
- The FM concept now uses cross-sectional mean/population standard deviation, eligible-asset `N`, consistent quality weights and explicit clip/re-normalise equations.
- Probability concepts now show intervals and unvalidated calibration status; additive percentage-point attribution and the 23.1% point estimate were removed.
- Web NAV weak/strong paths were corrected and now have direct labels and distinct dashes.
- Evidence examples and grade C semantics were corrected; below-threshold confirmation is explicitly cautionary and persists objections.
- Baseline/reveal dates now use 2026-08-10 and coherent +6/+12/+18/+24-month anchors.
- Public Default selected-state variants show Raw→Public deltas and reasons without hiding or overwriting Raw.
- Android input/export chrome and dependent settings were corrected; recovery-state variants were added for Web and Android.
- Final reviewer corrections align the FM concept with the executable rank/IC/quality/valid-support/z-score/clip/normalise sequence, restore grade C to secondary commentary/expert interviews, and align both Android portfolio states to the 2026-08-10 boundary plus a 24-month 2028-08-10 endpoint.

### Implementation corrections required

ImageGen concepts are visual specifications, not data fixtures. Production must correct or derive the following from versioned contracts:

- Do not hard-code sample dates, factor statistics, probabilities, allocations or NAV paths shown in a concept.
- Probability percentages must display configured rounding and interval/calibration state; unvalidated estimates must not gain extra precision.
- Generated concepts occasionally use serif text in controls. Production controls use the sans stack for legibility; serif is limited to display and section headings.
- Evidence URLs must never be trusted from visual copy. Runtime sources are schema-validated, sanitized and opened through the approved external-link policy.
- Chart colours require contrast checks on the implemented background, and every path must also differ by line style/label.
- The portfolio concept allocations are illustrative. Actual weight, amount, lot, residual cash and proxy logic come only from client FM output and the signed data snapshot.

## Canonical Web comparison log — 2026-08-23

Each Web row consolidates the approved concept path, implementation screenshot, viewport, observed mismatch, correction/verification and current Sol status. This is the single source of truth for the Phase 6 visual gate.

| State | Concept path | Implementation screenshot + viewport | Observed mismatch | Correction / verification | Result |
| --- | --- | --- | --- | --- | --- |
| Thesis | `concepts/web/01-thesis-input.png` | `apps/web/qa/screenshots/web-01-thesis-current.jpg`; 1265×709 IAB; existing 1536×1024 and 390×844 captures | No blocking mismatch; warm-paper shell and viewpoint-first entry preserved. | IAB smoke reloaded the current entry; `view_image` checked the current thesis and concept. | evidence-ready; Sol pending |
| Challenge/evidence | `concepts/web/02-challenge-evidence.png` | `apps/web/qa/screenshots/web-02-challenge-current.jpg`; 1265×709 IAB | No blocking mismatch; live DOM exposes the complete evidence form and rubric. | Exercised D/no-URL submission and A–C blank-URL rejection; Web tests and console checks passed. | evidence-ready; Sol pending |
| Worldline | `concepts/web/03-worldline-probability.png` | `apps/web/qa/screenshots/web-03-worldline-current.jpg`; 1265×709 IAB | Previous medium viewport could crowd the probability cards with the inspector. | Inspector hidden at ≤1340px; current worldline screenshot rechecked and `view_image` inspected. | corrected; evidence-ready; Sol pending |
| FM | `concepts/web/04-fm-progress.png` | `apps/web/qa/screenshots/web-11-fm-complete-fixed.png`; 1265px default desktop IAB, full-page | Worker completed too quickly to leave the FM audit surface reachable. | `fm_complete` retains `stage: "fm"`; user selects “查看配置方案”. Screenshot shows factor table, formula note, Worker/local boundary and completion state. | corrected; evidence-ready; Sol pending |
| Portfolio/stress | `concepts/web/05-portfolio-stress.png` | `apps/web/qa/screenshots/web-09-portfolio-desktop-fixed.png`; default desktop; `web-07-portfolio-mobile-p1.png`; 390×844 | Old screenshot had FM Raw selected glyph while Public Default content was displayed. | Dynamic glyphs plus `aria-pressed`; mobile screenshot was refreshed after the fix and checked with `view_image`. | corrected; evidence-ready; Sol pending |
| NAV | `concepts/web/06-worldline-nav.png` | `apps/web/qa/screenshots/web-06-nav-current.jpg`; 1265×709 IAB | No blocking mismatch; historical/simulated boundary, interval, direct labels and drawdown remain visible. | Existing screenshot plus fresh IAB DOM path check; console warnings/errors empty. | evidence-ready; Sol pending |
| Methods/risk | `concepts/web/07-methodology-risk.png` | `apps/web/qa/screenshots/web-12-methods-fixed.png`; 1265px default desktop IAB, full-page | No blocking mismatch; methods is a real scrollable state with formula, evidence grades, Raw/Public boundary and disclosures. | Screenshot inspected with `view_image`; methods export remains wired. | evidence-ready; Sol pending |
| Console / account boundary | `concepts/web/07-methodology-risk.png` (same approved quiet editorial direction; no new visual claim) | `apps/web/qa/screenshots/web-14-console-desktop.png`; 1440px full-page, and `web-15-console-mobile.png`; 390×844 | Account, provider and billing status did not have an explicit visible home; the legacy Methods entry obscured their configuration state. | Replaced the rail label with Console; kept all methodology/risk content below the optional account panel. Google/GitHub controls are disabled until a server PKCE readiness response exists. GLM, personal OpenAI-compatible routing and Google Play are represented only as truthful, unconfigured states. Local Playwright was used because the IAB control runtime failed to load; the local browser console has 0 errors and 0 warnings. | implementation QA pass; Sol pending |

The Web rows are evidence-ready but remain **Sol pending** until a fresh reviewer accepts this canonical log. Android comparison is intentionally deferred to Phase 7.

### Historical Android implementation + device evidence — 2026-08-24

| State | Concept | Implementation evidence | Status |
| --- | --- | --- | --- |
| Input | `concepts/android/01-thesis-input.png` | Historical capture `docs/qa/android/phase7-release-home.png`; its original runtime details are superseded by the explicit Phase 9 Metro + `adb reverse` record above | visually inspected; historical evidence |
| Challenge | `concepts/android/02-challenge-conversation.png` | `docs/qa/android/phase7-challenge.png` plus UI tree; mock rubric/reply flow and gesture-safe CTA exercised | visually inspected; device pass; Sol pending |
| Evidence | `concepts/android/03-evidence-workspace.png` | Local B-grade fixture and release `ACTION_SEND text/plain` evidence in `phase7-release-share-import.xml`; no fetch performed | device pass; Sol pending |
| Worldline | `concepts/android/04-worldline-timeline.png` | `docs/qa/android/phase7-worldline.png`; probability interval, calibration and reveal copy visible | visually inspected; device pass; Sol pending |
| Portfolio/NAV | `concepts/android/05-portfolio-nav.png` | `docs/qa/android/phase7-portfolio.png` and Raw-mode UI tree; local FM and Raw/Public selector exercised | visually inspected; device pass; Sol pending |
| Export | `concepts/android/06-share-export.png` | `docs/qa/android/phase7-archive-restored-fixed.png`, share-sheet UI tree, save → cold-start → AES-GCM restore | visually inspected; device pass; Sol pending |
| Console / account boundary | `concepts/android/01-thesis-input.png` (same warm-paper single-column direction; no new visual claim) | `docs/qa/android/phase7-console-phone.png` (1080×2400) and `phase7-console-tablet.png` (temporary 1200dp-wide emulator profile) | Account/provider/billing settings were not presented as a truthful mobile surface. | Phone layout stacks disabled Google/GitHub controls; tablet profile places them side-by-side. `view_image` inspected both; no OAuth client ID, secret, token exchange or API-key input is present. | implementation QA pass; Sol pending |
| Project search fallback | `concepts/android/03-evidence-workspace.png` (same evidence-workspace direction; new executable fallback state) | `docs/qa/android/phase7-project-search-fallback.png` (1080×2400), API-36 Google Play emulator; query/fallback UI trees beside it | The prior Android API-search affordance was a permanent unavailable placeholder and debug Metro warnings obscured lower controls. | The mobile client now queries only a configured HTTPS ForesightNine API and otherwise labels the client-evidence fallback. `view_image` verified the warm-paper hierarchy, visible no-auto-fetch boundary, legible fallback and unobscured lower CTA after the FM import-cycle fix. | implementation QA pass; Sol pending |

The Android screenshots preserve the approved quiet warm-paper/editorial hierarchy, single-column task rhythm, strong terracotta action states, bilingual labels and no-broker disclosure. Device QA found and corrected the original Android bottom-inset collision before this comparison. It is evidence for the required Phase 7 review, not a substitute for the mandated independent Sol acceptance.

### Phase 6 Web implementation note — 2026-08-23

- The Web MVP is implemented under `apps/web` with the approved shell, editorial spacing, warm-paper tokens, typed local workflow, client-side FM Worker path, Raw/Public Default delta view, SVG NAV/drawdown charts, and JSON/CSV export controls.
- A local browser smoke run loaded `http://127.0.0.1:5173/`, rendered the thesis screen, and exercised thesis submission through completed challenge state. The earlier “no screenshot artifact” observation is superseded by the canonical Web comparison log above, which records the later FM, Methods, portfolio and mobile captures. This note is historical and is not itself a Sol fidelity acceptance.

### Phase 6 Web P1 DOM evidence — 2026-08-23

- Fresh Browser/IAB-session DOM evidence covered the Web shell and primary thesis/challenge/worldline/portfolio/NAV/export path; the captured `error` and `warn` collections were empty. This is DOM acceptance evidence, not a screenshot-based visual comparison.
- The implementation evidence includes current-viewpoint premise propagation, editable/re-exported reply, SearchEvidenceBundle content hash and grade/source classification, Raw/Public constraint and lot-aware display, structured JSON/CSV audit fields, complete NAV arrays, and the working drawdown toggle.
- At the time of this historical entry, screenshot capture was unavailable in that IAB session. It is superseded by the later `view_image`-inspected artifacts and canonical comparison log above; no Sol pass is claimed by this historical entry.
