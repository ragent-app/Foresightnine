# Entity storage architecture

The API's eight research-state entity maps are backed by the typed document
repository boundary in `apps/api/app/storage.py`:

| Map | Collection | Loader |
| --- | --- | --- |
| `SESSIONS` | `sessions` | `ResearchSession.model_validate` |
| `THESES` | `theses` | `ThesisDocument.model_validate` |
| `CHALLENGES` | `challenges` | `ChallengeRecord.model_validate` |
| `CHALLENGE_TURNS` | `challenge_turns` | `int` |
| `EVIDENCE` | `evidence` | `EvidenceItem.model_validate` |
| `WORLDLINES` | `worldlines` | `Worldline.model_validate` |
| `MARKET_MANIFESTS` | `market_manifests` | `MarketDataManifest.model_validate` |
| `WORLDLINE_CHALLENGES` | `worldline_challenges` | `str` |

`main.py` normalizes `FORESIGHTNINE_POSTGRES_URL` and `DATABASE_URL` to the
storage foundation's compatibility DSN input without printing or persisting
credentials. The existing `FORESIGHTNINE_POSTGRES_DSN` variable remains
accepted. Outside production/public-hardening mode, an absent explicit mode
defaults to the deterministic in-memory repository. In production or public
hardening, missing Postgres mode or URL, and explicit memory mode, raise a
configuration error during application construction. A selected Postgres
repository connects lazily; connection and operation failures become
`StorageRuntimeError` and cannot silently fall back to memory.

The schema in `infra/migrations/001_research_state.sql` uses the stable
`(collection, document_key)` identifier, JSONB documents, lifecycle timestamps,
optional expiry, optimistic-concurrency versions, and supporting indexes. It is
forward-only and safe to rerun. The development Compose stack initializes this
schema and waits for PostgreSQL health before starting the API. Redis is present
as a development service for the upcoming stack but is not configured or used
by the API today.

This layer persists entity documents only. It does not claim durable operation
idempotency/replay state, worker leases or restart recovery, or atomic
multi-process abuse control; those remain later stack boundaries.
