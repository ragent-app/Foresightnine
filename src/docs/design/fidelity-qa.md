# Design fidelity QA

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
| Web thesis | `concepts/web/01-thesis-input.png` | pending | pending |
| Web challenge/evidence | `concepts/web/02-challenge-evidence.png` | pending | pending |
| Web worldline | `concepts/web/03-worldline-probability.png` | pending | pending |
| Web FM | `concepts/web/04-fm-progress.png` | pending | pending |
| Web portfolio/stress | `concepts/web/05-portfolio-stress.png` | pending | pending |
| Web NAV | `concepts/web/06-worldline-nav.png` | pending | pending |
| Web methods/risk | `concepts/web/07-methodology-risk.png` | pending | pending |
| Android input | `concepts/android/01-thesis-input.png` | pending | pending |
| Android challenge | `concepts/android/02-challenge-conversation.png` | pending | pending |
| Android evidence | `concepts/android/03-evidence-workspace.png` | pending | pending |
| Android worldline | `concepts/android/04-worldline-timeline.png` | pending | pending |
| Android portfolio/NAV | `concepts/android/05-portfolio-nav.png` | pending | pending |
| Android export | `concepts/android/06-share-export.png` | pending | pending |
