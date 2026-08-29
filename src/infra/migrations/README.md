# Database migrations

Migrations in this directory are forward-only SQL files. They must be reviewed,
tested on an empty database and a previous release snapshot, and must never
persist provider credentials.

`001_research_state.sql` creates the JSONB document table used by the API's
typed entity repositories. It is safe to rerun because the table and indexes
use `IF NOT EXISTS`; rerunning it does not modify existing documents.

The migration establishes storage durability for entity documents only. It does
not persist `OperationManager` idempotency/replay state, worker leases or
restart recovery, and it does not provide multi-process Redis abuse controls.

`002_operation_state.sql` adds the durable operation boundary: hash-only
idempotency claims, typed response/error documents, operation status and worker
leases, append-only SSE events, and operation-to-session associations. Its
named unique constraint on `(endpoint, idempotency_key_hash)` is the cross-
process owner gate. Terminal transitions lock the operation row and append the
event before committing. Rerunning the migration is safe; it does not alter
existing rows.

For the development Compose stack, PostgreSQL runs both files from its standard
first-initialization directory. Existing named volumes must be migrated with a
normal migration runner (or a one-time `psql` invocation) because Docker's
first-initialization directory is only applied to an empty data directory.
Production deployments should apply the reviewed files through their normal
migration runner; this repository does not contain credentials or claim a
production migration service.
