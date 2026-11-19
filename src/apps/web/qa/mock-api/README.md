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

## Account, model and market-data settings (bearer auth, `Cache-Control: no-store`)

Mirrors `GET /v1/account` (`billing.snapshot`), `model_credentials.py` and `market_credentials.py`. Strict bodies: unknown keys, wrong types and bound violations are 422 `validation_error`. Stored API keys are never returned; only the masked `key_hint`. No network call is made; the test endpoints are simulated. Per-account, in memory.

| Endpoint | Notes |
| --- | --- |
| `GET /v1/account` | `{account: {account_id, email}, plan: "free", entitlement_status: "active", product_id: null, base_plan_id: null, quota_limit: 200000, quota_consumed, quota_reserved: 0, quota_remaining, quota_resets_at (first day of next month 00:00Z), provider_model: "platform-model-example"}`. `quota_consumed` starts at 18500 and grows by each finished run's `usage.total_tokens` (counted once the run is terminal, continuations only add new tokens) only when the account's model mode was `platform` at run start; personal-model runs never consume quota. |
| `GET /v1/account/model-config` | Platform: `{schema_version, mode: "platform", configured: true, updated_at?}` (no null fields; `updated_at` only after a save), plus the saved personal `protocol, base_url, model, key_hint` when a personal configuration was kept. Personal: adds `protocol, base_url, model, key_hint` (`••••` + last 4, or last 4 hex of the sha256 when the suffix is not ASCII alphanumeric). |
| `PUT /v1/account/model-config` | `{mode, protocol, base_url (1-2048), model (1-128), api_key (8-512), clear_api_key}`. 422: `api_key` with `clear_api_key`; platform mode with any personal field or `clear_api_key`; surrounding whitespace; non-boolean `clear_api_key`. Platform keeps the saved personal fields and key (inactive) so `{mode: "personal"}` alone switches back; `DELETE` removes them. Personal merges with the stored values (`protocol` defaults to `openai_chat_completions`); changing origin (scheme/host/port) or protocol without `api_key` is 422 `Changing model origin or protocol requires supplying the API key again.`; missing base URL, model or key is 422 `Personal model configuration is incomplete.`; `base_url` must be https, no credentials/query/fragment, port absent or 443, not a metadata host, `localhost`, a private/loopback/link-local/reserved IP literal or a `.local`/`.internal` name, else 422 `Personal model configuration is invalid.`. |
| `DELETE /v1/account/model-config` | Removes everything, returns the platform snapshot. |
| `POST /v1/account/model-config/test` | No body. `{status: "ready"\|"unavailable"}`: unavailable unless personal with `openai_chat_completions`; also unavailable when the host or model contains `down`. |
| `GET /v1/account/market-data-config` | `{schema_version, fallback_order, providers, updated_at}`. `providers` are always in the fixed order `market_data, alpha_vantage, twelve_data, finnhub, yahoo` (as the backend), each `{provider_id, display_name, enabled, requires_key, personal_key_configured, platform_available, key_hint (null when none)}`. Default: all enabled, no personal keys, `updated_at: null`. Simulated deployment keys: `platform_available` is true for `alpha_vantage` and `yahoo` (the backend always reports yahoo), false for the rest. |
| `PUT /v1/account/market-data-config` | `{fallback_order, providers: [{provider_id, enabled, api_key?, clear_api_key?}]}`; both lists must hold each of the 5 ids exactly once. 422: `api_key` with `clear_api_key`, a key action for `yahoo`, whitespace-padded or out-of-bounds `api_key`, non-boolean `enabled`/`clear_api_key`. Existing keys are kept when neither is given. |
| `POST /v1/account/market-data-config/test` | `{provider_id}` (strict) gives `{provider_id, status}`: `ready` when the provider is enabled, usable (no key needed, personal key set, or `platform_available`) and its personal key (if any) does not contain `bad`; otherwise `unavailable`. |

Magic values: an `api_key` of `storage-unavailable-key` (model config, or any provider in the market-data update) is 503 `model_credential_storage_unavailable` / `market_credential_storage_unavailable`, `retryable: true`, `Retry-After: 30`, and nothing is stored. Test probes: host or model containing `down` is unavailable (model); a personal key containing `bad` is unavailable (market).

- Tests: `node --test qa/mock-api/server.test.mjs`. Step interval: `createServer({ tickMs })`, default 1500 ms.

- `GET /v2/research/capabilities` mirrors the backend capability list. SEC is reported available because the mock implements the SEC endpoints; start with `MOCK_SEC_AVAILABLE=0` to see the UI's SEC gate.
- `GET /v1/account` also returns `research_model` (the model platform research runs on), as the backend does.

- Like the backend boundary, POST/PUT/PATCH without `Content-Type: application/json` get 415 (except the body-less `/v1/account/model-config/test` probe); the UI sends `{}` for body-less writes.
