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

- Tests: `node --test qa/mock-api/server.test.mjs`. Step interval: `createServer({ tickMs })`, default 1500 ms.
