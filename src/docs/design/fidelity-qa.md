# Design fidelity QA

## Latest Web remediation evidence — 2026-08-23

- The previously observed Public Default selector mismatch was corrected: the selected mode now has the selected glyph and `aria-pressed`, and the table caption/copy agree with the mode.
- Inspected implementation screenshots: `apps/web/qa/screenshots/web-09-portfolio-desktop-fixed.png` at the default desktop viewport and `apps/web/qa/screenshots/web-07-portfolio-mobile-p1.png` at 390×844. Both preserve the approved warm-paper/editorial rhythm, restrained terracotta/sage/blue semantics, single-column mobile flow and explicit Raw → Public Default explanation.
- Functional/browser evidence covers the complete Web path and console `error`/`warn` collections are empty. The Web portfolio row is now **evidence available / formal comparison pending**; a fresh Sol verdict is still required before recording a phase pass.

## Current Web implementation evidence — 2026-08-23

- A fresh Browser/IAB run produced and inspected current Web screenshots at the active browser viewport (approximately 1265×709): `apps/web/qa/screenshots/web-01-thesis-current.jpg`, `web-02-challenge-current.jpg`, `web-03-worldline-current.jpg`, `web-05-portfolio-current.jpg`, and `web-06-nav-current.jpg`.
- The screenshots confirm the approved warm-paper/editorial shell, adversarial challenge/evidence layout, interval-first probability cards, Raw → Public Default delta view with stress output, and historical/simulated NAV with labelled today boundary and drawdown section. The medium-width inspector collision was corrected and rechecked in `web-03-worldline-current.jpg`.
- DOM checks for thesis → challenge → worldline → client FM → portfolio → NAV/export found no browser `error` or `warn` entries. This is implementation evidence; it does not by itself approve the remaining Android or final 1536×1024 concept comparison.
- Web fidelity rows are now **evidence available / formal comparison pending** because the captured viewport differs from the concept artboard and the current Web screenshots do not yet cover every concept state (FM and methods). Android rows remain **pending**.

## Sol freeze review — 2026-08-23

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

## Implementation comparison log

To be completed during Web and Android QA. Each entry must include concept path, implementation screenshot path, viewport/device, observed mismatch, correction commit and Sol verdict.

| State | Concept | Implementation evidence | Status |
| --- | --- | --- | --- |
| Web thesis | `concepts/web/01-thesis-input.png` | `apps/web/qa/screenshots/web-01-thesis-current.jpg`; 1265×709 IAB + 1536×1024/390×844 implementation captures | evidence-ready; Sol pending |
| Web challenge/evidence | `concepts/web/02-challenge-evidence.png` | `apps/web/qa/screenshots/web-02-challenge-current.jpg`; 1265×709 IAB | evidence-ready; Sol pending |
| Web worldline | `concepts/web/03-worldline-probability.png` | `apps/web/qa/screenshots/web-03-worldline-current.jpg`; 1265×709 IAB | evidence-ready; Sol pending |
| Web FM | `concepts/web/04-fm-progress.png` | `apps/web/qa/screenshots/web-11-fm-complete-fixed.png`; 1265px default desktop IAB, full-page capture | evidence-ready; Sol pending |
| Web portfolio/stress | `concepts/web/05-portfolio-stress.png` | `apps/web/qa/screenshots/web-09-portfolio-desktop-fixed.png`, `web-07-portfolio-mobile-p1.png`; default desktop + 390×844 | evidence-ready; Sol pending |
| Web NAV | `concepts/web/06-worldline-nav.png` | `apps/web/qa/screenshots/web-06-nav-current.jpg`; 1265×709 IAB | evidence-ready; Sol pending |
| Web methods/risk | `concepts/web/07-methodology-risk.png` | `apps/web/qa/screenshots/web-12-methods-fixed.png`; 1265px default desktop IAB, full-page capture | evidence-ready; Sol pending |
| Android input | `concepts/android/01-thesis-input.png` | pending | pending |
| Android challenge | `concepts/android/02-challenge-conversation.png` | pending | pending |
| Android evidence | `concepts/android/03-evidence-workspace.png` | pending | pending |
| Android worldline | `concepts/android/04-worldline-timeline.png` | pending | pending |
| Android portfolio/NAV | `concepts/android/05-portfolio-nav.png` | pending | pending |
| Android export | `concepts/android/06-share-export.png` | pending | pending |

### Web state-by-state comparison log — 2026-08-23

| State | Observed mismatch against concept | Correction / verification | Result |
| --- | --- | --- | --- |
| Thesis | No blocking mismatch; implementation keeps the concept's warm-paper shell and viewpoint-first entry. | Existing 1536×1024 and 390×844 captures; current IAB smoke reloaded the same entry. | evidence-ready |
| Challenge/evidence | No blocking mismatch; the live DOM exposes the complete evidence form and rubric rather than a decorative card. | D/no-URL submission and A–C blank-URL rejection were exercised; 22 Web tests and console checks passed. | evidence-ready |
| Worldline | At the previous medium viewport, the inspector could crowd the three probability cards. | Hidden inspector breakpoint moved to ≤1340px; `web-03-worldline-current.jpg` rechecked at the affected viewport. | corrected; evidence-ready |
| FM | The worker completed too quickly to leave the FM state visible; the approved concept's audit surface was unreachable. | `fm_complete` now retains `stage: "fm"`; the user explicitly clicks “查看配置方案”. `web-11-fm-complete-fixed.png` was inspected with factor table, formula note, Worker label and completion state. | corrected; evidence-ready |
| Portfolio/stress | Mode glyph stayed visually on FM Raw while the table had already switched to Public Default. | Dynamic glyphs plus `aria-pressed`; `web-09-portfolio-desktop-fixed.png` and `web-07-portfolio-mobile-p1.png` inspected after the fix. | corrected; evidence-ready |
| NAV | No blocking mismatch; historical/simulated boundary, interval, direct labels and drawdown remain visible. | Existing `web-06-nav-current.jpg` plus fresh IAB DOM path check; console warnings/errors empty. | evidence-ready |
| Methods/risk | No blocking mismatch; methods page is a real scrollable state with formula, evidence grades, Raw/Public boundary and disclosures. | `web-12-methods-fixed.png` inspected; methods export remains wired. | evidence-ready |

The Web rows are now evidence-ready but remain **Sol pending** until a fresh reviewer accepts the complete comparison log. Android rows are intentionally still pending for Phase 7.

### Phase 6 Web implementation note — 2026-08-23

- The Web MVP is implemented under `apps/web` with the approved shell, editorial spacing, warm-paper tokens, typed local workflow, client-side FM Worker path, Raw/Public Default delta view, SVG NAV/drawdown charts, and JSON/CSV export controls.
- A local browser smoke run loaded `http://127.0.0.1:5173/`, rendered the thesis screen, and exercised thesis submission through completed challenge state. No implementation screenshot file path was committed, so all Web comparison rows above remain **pending**; this note is not a Sol fidelity acceptance.

### Phase 6 Web P1 DOM evidence — 2026-08-23

- Fresh Browser/IAB-session DOM evidence covered the Web shell and primary thesis/challenge/worldline/portfolio/NAV/export path; the captured `error` and `warn` collections were empty. This is DOM acceptance evidence, not a screenshot-based visual comparison.
- The implementation evidence includes current-viewpoint premise propagation, editable/re-exported reply, SearchEvidenceBundle content hash and grade/source classification, Raw/Public constraint and lot-aware display, structured JSON/CSV audit fields, complete NAV arrays, and the working drawdown toggle.
- Screenshot evidence remains **pending** because the screenshot command is unsupported in the current IAB session and no screenshot artifact could be generated or inspected. All fidelity comparison rows remain **pending**; no Sol pass is claimed.
