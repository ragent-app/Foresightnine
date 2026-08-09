# Component and state matrix

| Capability | Web | Android | Required non-happy states |
| --- | --- | --- | --- |
| Thesis input | Sidebar, editor, currency/amount field, process rail | Editor, amount, bottom nav, sticky CTA | empty, too long, invalid amount, draft restored |
| Challenge | Continuous transcript, rubric, composer | Vertical transcript, compact rubric, composer | streaming, cancelled, timeout, 429, invalid provider output, retry |
| Evidence | Right drawer and source inspector | Full-screen evidence list | client-search fallback, invalid URL, duplicate, grade D/E warning, offline |
| Worldline | Branching horizontal canvas, probability inspector | Branch selector and vertical timeline | broken reference, unnormalised branches, unvalidated probability, regenerated version |
| FM | Worker progress trace and intermediate table | shared-engine progress sheet | invalid signature, expired snapshot, no factor, cancel, memory limit, resumed |
| Portfolio | Raw/public toggle, allocation table, stress panel | allocation list and stress drill-down | tiny capital, untradeable proxy, FX stale, lot residual cash |
| NAV | Large path chart plus drawdown | compact path chart plus drawdown | no history, partial history, simulation unavailable, reduced motion |
| Method/export | Method appendix and export actions | privacy-aware export flow | export failure, private premise hidden, public-link disabled/expired |

## Accessibility acceptance

- Web keyboard order follows the visible reading order; dialogs/drawers trap focus and restore it on close.
- Visible focus uses the terracotta focus ring with at least 3:1 contrast against adjacent colours.
- Charts expose a textual summary and downloadable data table; colour is never the only branch cue.
- Dynamic progress and streamed challenge text use polite live regions; critical failures use assertive alerts.
- Android controls expose `accessibilityRole`, label, hint and selected/disabled state. Timeline and allocation rows are coherent reading groups.
- Body text remains readable at 200% Web zoom and Android large-font settings; no required information is clipped.
