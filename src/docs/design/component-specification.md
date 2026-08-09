# Component implementation specification

Every component is shared semantically across Web and Android; rendering primitives remain platform-native. All sizes below are minimums.

| Component | Variants and slots | Interaction and accessibility |
| --- | --- | --- |
| App shell | rail, inspector, bottom-nav; header/content/footer slots | skip link on Web; current route state; Android back behaviour; rail collapses below 900 px |
| Button | primary, secondary, quiet, caution, danger; compact/default/touch | rest/hover/focus/pressed/loading/disabled; 44 px Web, 48 dp Android; loading preserves label width |
| Thesis editor | label, helper, input, character count, validation | draft autosave, IME-safe, 2,000-character cap, invalid amount summary, restore after refresh |
| Money input | currency, amount, validation, implementation warning | locale-aware display; stores integer minor units; currency selector always reachable |
| Conversation turn | user, challenger, system; author/time/body/source slots | streamed text uses polite live region; cancelled output remains visible and resumable |
| Rubric | dimension, score, rationale, critical flag | total plus all dimensions; never conveys pass by colour alone; below-threshold state exposes two distinct actions |
| Evidence item | A–E; title/source kind/verification/provenance/support/opposition | grade has text label plus colour; expandable; external URL has explicit origin and safe-open hint |
| Evidence drawer/screen | list, filter, inspector, add-client-bundle | focus trap/restore on Web; full-screen route on Android; invalid/duplicate bundle errors inline |
| Probability range | premise, conditional, joint; low/high/confidence/calibration | interval is primary; no extra point precision; textual equation and recalculable joint range |
| Worldline | weak/main/strong; nodes/edges/invalidation/reveal time | line dash, marker and label supplement colour; keyboard node traversal; vertical Android variant |
| Operation status | idle/streaming/paused/cancelled/recovering/failed/complete | operation ID retained; retry and cancel are distinct; Last-Event-ID recovery copy shown |
| FM trace | signed snapshot, selection, signal, normalisation, dead-band | progress and current step; cancellable; intermediates downloadable; no main-thread-only Web path |
| Portfolio mode | FM Raw, Public Default | Raw always recoverable; Public displays per-asset delta and reason; mode change never rewrites Raw hash |
| Allocation table/list | asset, role, weight, amount, units, residual/proxy | sortable Web table; grouped Android rows; totals announced; unavailable/lot constraint has explicit reason |
| NAV chart | history, selected median, interval, weak, strong, today, drawdown | text summary and data table; direct labels/dashes/markers; zoom/pan keyboard alternatives; simulated boundary labelled |
| Export controls | PDF, JSON, CSV; privacy and expiry | dependent settings disabled when public link is off; private premises hidden by default; generation recoverable |

## Responsive layout

- Desktop ≥1200 px: 248 px rail, fluid main sheet, optional 320 px inspector.
- Narrow Web 640–1199 px: collapsible rail; inspectors become drawers; tables retain first column and horizontal overflow semantics.
- Compact Web <640 px: follows Android information order but remains Web-native and keyboard accessible.
- Android 9:16 concepts are reference artboards; implementation must reflow at 320–600 dp width, large font and display cutouts.

## Focus, disabled and error rules

- Focus ring uses the 3:1 non-text `focus` token and is never suppressed.
- Disabled controls retain readable text and explain why when the reason is not obvious.
- Errors are adjacent to the failed field or operation and mirrored in a summary for long forms.
- Retry never discards thesis, evidence, selected worldline or completed FM intermediates.
- Caution actions such as `带保留意见确认` never reuse the success colour or primary success iconography.
