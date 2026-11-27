# ForesightNine design specification

Status: approved for implementation on 2026-08-10. The user explicitly approved all proposed solutions and asked execution to continue without a second visual pause. Approval applies to the complete Web and Android concept suites listed below, not to any third-party brand assets.

## Product character

The [personal investment decision agent](../product/fusion-finance-ai-macro-plan-2026-11-11.md) is designed to help investors compare opportunities and revisit portfolio decisions. The following visual guidance and concepts cover the August research and simulation workflow. The interface uses an editorial document rhythm, warm paper surfaces, precise tables and timelines, and restrained chart colour. It may share Claude.ai's calm interaction qualities, but the brand, wordmark, layout, icons and copy are original. No Anthropic or Claude asset may ship.

## Concept inventory

### Web (7 required states)

1. `concepts/web/01-thesis-input.png` — thesis and capital input.
2. `concepts/web/02-challenge-evidence.png` — adversarial conversation, rubric and open evidence drawer.
3. `concepts/web/03-worldline-probability.png` — weak/main/strong branches and three-layer probability.
4. `concepts/web/04-fm-progress.png` — client-side FM progress, factor table and reproducibility inspector.
5. `concepts/web/05-portfolio-stress.png` — FM Raw/Public Default, allocation and stress results.
6. `concepts/web/06-worldline-nav.png` — selected-worldline NAV, uncertainty band and drawdown.
7. `concepts/web/07-methodology-risk.png` — methods, evidence grades, provenance and risk.

Web state variants:

- `concepts/web/02b-sse-recovery.png` — Last-Event-ID recovery with preserved transcript/evidence.
- `concepts/web/05b-public-default-delta.png` — Public Default selected with Raw→Public deltas and reasons.

### Android (6 required states)

1. `concepts/android/01-thesis-input.png` — mobile thesis and capital input.
2. `concepts/android/02-challenge-conversation.png` — dialogue-first challenge.
3. `concepts/android/03-evidence-workspace.png` — graded evidence inspection.
4. `concepts/android/04-worldline-timeline.png` — vertical branch timeline and probability.
5. `concepts/android/05-portfolio-nav.png` — selected-worldline NAV plus allocation.
6. `concepts/android/06-share-export.png` — privacy-aware research export.

Android state variants:

- `concepts/android/02b-sse-recovery.png` — recoverable streaming disconnect with preserved draft.
- `concepts/android/05b-public-default-delta.png` — Public Default selected with per-asset deltas.

The original generation prompts are recorded in `imagegen-prompts.md`. Generated raster concepts are design references; production text, charts, icons and controls must be code-native.

## Non-negotiable implementation rules

- Warm ivory is the default canvas; dark umber is the primary text colour; terracotta is the main action/worldline colour. Dusty blue and sage are semantic branch colours, not decoration.
- Main surfaces are continuous sheets separated by fine rules. Do not turn the product into a card or bento dashboard.
- Serif is reserved for display and section headings; controls and long-form body copy use the sans stack.
- Historical and simulated NAV must be separated by a labelled `today` boundary. Simulated median, 10–90% interval and weak/strong references are visually distinct.
- Always show all three probability layers: `P(H)`, `P(W | H)` and joint probability. Intervals and calibration status remain visible.
- Always preserve FM Raw; Public Default is a separately derived view with an explanation of every changed constraint.
- Minimum interactive target is 44 CSS pixels on Web and 48 density-independent pixels on Android.
- Every interactive element needs keyboard/focus semantics on Web and accessibility role/label/state on Android.
- Motion is limited to 120–240 ms state transitions and progress feedback. Respect reduced-motion settings.
- Empty, loading, partial, cancelled, offline, invalid-evidence, provider-timeout and recoverable-error states must keep the user's thesis and evidence intact.
- No broker connection, order placement or custody affordance may appear.

## Fidelity QA

Implementation acceptance requires viewport screenshots at desktop, narrow Web and Android dimensions. Compare those screenshots with the concepts using `view_image`, then record deviations and fixes in `docs/design/fidelity-qa.md`. Concepts are directional specifications; generated text artefacts never override the approved copy deck or domain contracts.
