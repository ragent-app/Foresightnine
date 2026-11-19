# Mock research API (local UI QA)

Run: `node qa/mock-api/server.mjs` (port `MOCK_API_PORT`, default 8787), then in another shell:
`FORESIGHTNINE_DEV_API_TARGET=http://127.0.0.1:8787 VITE_FORESIGHTNINE_LOCAL_AUTH=mock corepack pnpm dev`

- All companies, sources and numbers are fictional QA data; state is in memory and resets on restart.
- Any email and a 12+ char password signs in; each new account gets 3 seeded topics.
- A run question containing `部分` ends `failed` with a saved partial turn; `失败` ends `failed` with no turn (both continuable).
- Topic C has a display-off and a simulated source (source text returns 403); topic A's Northwind report source (`src_northwind_fy26`) forbids export, so full topic export returns 403. Topic A also has a second, downloadable fictional PDF (`src_northwind_deck`, display and export allowed) for the original-download flow; `src_northwind_fy26` also keeps an original but export is off, so its download is 403.

## Source endpoints (bearer auth, strict bodies: unknown keys are 422)

Visible source = source metadata without `status`; `text` only when `permissions.display` is true. Timestamps need a timezone offset or `Z`. Creating a source requires `permissions.store` (422 `Source storage permission is required.`).

| Endpoint | Notes |
| --- | --- |
| `POST /v2/research/sources` | Text source (201). |
| `POST /v2/research/sources/pdf` | `pdf_base64` must decode to `%PDF-...` (max 5,000,000 bytes), else 422. 201 without `text`, original kept. |
| `POST /v2/research/sources/html` | UTF-8 HTML, tags/script/style stripped (422 if invalid, empty or too large). 201 without `text`, original kept. |
| `POST /v2/research/sources/fetch` | Allowlist: `https://www.bea.gov`, `www.federalreserve.gov`, `www.sec.gov` (SEC: direct `/Archives/edgar/data/<cik>/<18 digits>/<file>.htm(l)` only, no query). No network call; fictional text. SEC responses omit `text`. |
| `POST /v2/research/identities/sec-search` | Fictional index: NWEX (1000001), CTEX (1000002), FBEX (1000003). |
| `POST /v2/research/sources/sec-concept` | Fictional `companyconcept` records; honors `unit`, `filing_before`, `max_records`. |
| `GET /v2/research/sources/:source` | Visible source. |
| `GET /v2/research/sources/:source/original` | Stored bytes (PDF `application/pdf`, HTML `application/octet-stream`, attachment); 403 unless an original exists and display and export are both on. |
| `POST /v2/research/sources/:source/reextract` | `{text_order}`; optional `Idempotency-Key` (8-128 chars). Same key, original and order replays the same source (200 + `Idempotency-Replayed: true`); a different original/order is 409. |
| `POST /v2/research/topics/:topic/sources` | `{parent_version_id, source_ids}` appends a user version with the union of source ids; stale parent 409, unknown topic/source 404. |

Magic inputs:

- PDF title containing `繁忙`: 503 `internal_error`, retryable (checked before the PDF bytes).
- PDF title containing `间距`: the source gets `pdf_text_spacing_warning: true`.
- Fetch URL path containing `timeout`: 503, retryable.
- SEC search query exactly `未配置`: 503 `SEC source contact is not configured.`, not retryable.
- SEC concept CIK `0009999999` (unknown company): 422 `SEC source data could not be used.`.

## Run source selection and discovery

- Available sources = topic request source ids + latest version source ids + body `source_ids`. A body id the account does not own is 404; `selected_source_ids` (unique, nonblank) outside the available set is 422. The run reads the selection (`[]` reads nothing), else every available source; findings and the saved `sources` summary use only what was read. Body `source_ids` are merged into the saved version.
- `discover_sources` omitted: enabled unless `max_search_calls` is `0`, or `selected_source_ids` is `[]` without a positive `max_search_calls`. Explicit `true` enables, `false` and `null` disable. When enabled and `max_search_calls` is not `0`, the run emits a completed `discover_sources` tool event first (`search_calls` +1, turn `search_status: "completed"`) and appends two fictional `source_candidates` leads (existing URLs are not duplicated). Otherwise `search_status` is `not_requested`. The resolved flag is kept on the run record as `resolvedDiscover`.
- The scripted run events are unchanged: tests that count events pass `discover_sources: false`.

## Versions and calculations (bearer auth, strict bodies: unknown keys are 422)

Mirrors `create_research_version`, `create_research_calculation` and `recalculate_research` in the real API. Every write appends a version with `created_by: "user"` and `source_ids` copied from the parent, bumps `updated_at` and returns the full topic (201). Unknown topic 404; `parent_version_id` that is not the latest version id is 409 `lifecycle_conflict`.

| Endpoint | Notes |
| --- | --- |
| `GET /v2/research/topics/:topic/versions/:version` | One version object; 404 for an unknown topic or version. |
| `POST /v2/research/topics/:topic/versions` | `{parent_version_id, content, review: draft\|confirmed\|reservation (default draft), note (<=10000, default "")}`. `content.turns`, `sources`, `source_candidates` and `gaps` must equal the parent's (missing = empty list), else 422 `Server-produced research records cannot be edited.`; `summary` <=100000 chars. See rules below. |
| `POST /v2/research/topics/:topic/calculations` | `{parent_version_id, formula: growth_percent\|margin_percent\|difference, first_fact_id, second_fact_id, decimal_places (integer 0..8, default 2)}`. First = current/numerator, second = baseline/denominator. Appends a calculation (`calc-<32 hex>`, `turn_id` null, `authorship: "calculation_engine"`); 2000-calculation limit is 422. Version keeps the previous note, `review: "draft"`. |
| `POST /v2/research/topics/:topic/calculations/recalculate` | `{parent_version_id, calculation_index (0..1999)}`. Recomputes that calculation from the current facts (same formula, fact ids, decimal places), keeps `calculation_id` and `turn_id`, clears `legacy_unit_warning`, replaces it in place. Index out of range: 422 `Calculation does not exist in this version.` |

Version edit rules:

- Claims: a claim equal to the parent's claim with the same id (defaults applied) is kept verbatim. A changed or new claim must cite a source in the parent's `source_ids` that exists, is displayable, not simulated and published at or before `request.as_of`, with `text.slice(start, end) === quotation` (nonblank), else 422 `Citation must match an attached, displayable, time-eligible source.`. New claims get `claim-<32 hex>` and `turn_id: null`; changed claims keep their id and `turn_id`; both get `edited_by_user: true`. `challenges_claim_ids` (new temporary ids are remapped) must name other existing claims. Duplicate ids are 422; omitted claims are deleted.
- Facts: unchanged facts (ignoring `currency_quote_missing`, `scale_quote_missing`, `currency_unspecified`) are kept. Edited or new facts need a plain decimal `value`, consistent `location`, and a valid source span; `verification` becomes `user_edited_not_independently_verified` if the value (thousands separators ignored) is in the quotation, else `user_edited_value_not_in_quotation` (such a fact cannot feed a calculation). Original `turn_id` kept (new: null). The computed flags are not re-derived for edited facts (dropped).
- Calculations: unchanged ones (ignoring `input_status`, `legacy_unit_warning`, `result_status`) are kept; edited or new ones get `authorship: "user"`, `verification: "user_edited_not_independently_verified"`, `legacy_unit_warning: null`, original `turn_id` (new: null) and recomputed `input_status`/`result_status`. After any save, `input_status` of every calculation is re-derived against the saved facts (`changed`, `missing_or_ambiguous`, ...); a stored `unverifiable` stays `unverifiable`.

Calculation engine (exact decimal math with BigInt, round half-up, fixed-decimal string): `growth_percent` = `(first / second - 1) * 100`, `margin_percent` = `first / second * 100` (both unit `percent`, currency null), `difference` = `first - second` (unit of the inputs; two percent inputs give unit `percentage points`, `formula_version` "2"). Rejections (422 `Calculation cannot be performed with the current facts.`): same fact twice, unit or currency mismatch, margin with different periods, growth with equal periods or (when both periods are recognized and of the same kind) a current period not later than the baseline, non-positive baseline or denominator, percent inputs with a currency. Each fact id must match exactly one fact (`Calculation input facts are missing or ambiguous.`) and not be `user_edited_value_not_in_quotation`. Not mirrored: the backend's currency/scale-in-quotation checks (seeded `F3`/`F4` carry `currency_quote_missing` but are still usable) and its table/XBRL evidence checks.

Topic A seed (latest version `ver_a3`): facts `F1` 总收入 FY2026 / `F2` 总收入 FY2025 (growth: first `F1`, second `F2`), `F3` 营业利润 FY2026 / `F1` (margin: first `F3`, second `F1`; also `F4`/`F2` for FY2025), `F5` / `F6` 营业利润率 FY2026 / FY2025 (percent facts, no currency). Calculation index 3 (`C-margin-diff-legacy`) is a `difference` over `F5`/`F6` with `legacy_unit_warning: "percentage_difference_requires_recalculation"` (unit `percent`, `input_status: "unverifiable"`, `result_status: "mismatch"`); recalculating it gives `-0.38 percentage points`. Versions: `ver_a1` (user, empty), `ver_a2` (turn 1: two facts, one calculation), `ver_a3` (turn 2: adds facts, four claims and calculations).

- Tests: `node --test qa/mock-api/server.test.mjs`. Step interval: `createServer({ tickMs })`, default 1500 ms.
