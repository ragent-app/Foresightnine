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

For the development Compose stack, PostgreSQL runs this file from its standard
first-initialization directory. Production deployments should apply the same
reviewed migration through their normal migration runner; this repository does
not contain credentials or claim a production migration service.
