-- ForesightNine durable operation, idempotency and SSE replay state.
-- Forward-only, credential-free, and safe to rerun on an empty or existing DB.

CREATE TABLE IF NOT EXISTS operation_state (
    operation_id text PRIMARY KEY,
    endpoint text NOT NULL,
    idempotency_key_hash char(64) NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    operation jsonb NOT NULL,
    lease_owner text,
    lease_expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT operation_state_key_hash_format CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS operation_state_status_lease_idx
    ON operation_state (status, lease_expires_at)
    WHERE status IN ('pending', 'running');

CREATE TABLE IF NOT EXISTS operation_idempotency (
    endpoint text NOT NULL,
    idempotency_key_hash char(64) NOT NULL,
    request_hash char(64) NOT NULL,
    operation_id text REFERENCES operation_state(operation_id) ON DELETE SET NULL,
    mode text NOT NULL CHECK (mode IN ('sync', 'async', 'control')),
    response jsonb,
    response_type text,
    error jsonb,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT operation_idempotency_endpoint_key_hash_unique UNIQUE (endpoint, idempotency_key_hash),
    CONSTRAINT operation_idempotency_key_hash_format CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT operation_idempotency_request_hash_format CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT operation_idempotency_result_exclusive CHECK (NOT (response IS NOT NULL AND error IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS operation_idempotency_operation_idx
    ON operation_idempotency (operation_id);
CREATE INDEX IF NOT EXISTS operation_idempotency_updated_idx
    ON operation_idempotency (updated_at);

CREATE TABLE IF NOT EXISTS operation_events (
    operation_id text NOT NULL REFERENCES operation_state(operation_id) ON DELETE CASCADE,
    sequence bigint NOT NULL CHECK (sequence >= 1),
    event jsonb NOT NULL,
    emitted_at timestamptz NOT NULL,
    PRIMARY KEY (operation_id, sequence)
);

CREATE TABLE IF NOT EXISTS operation_session_associations (
    operation_id text PRIMARY KEY REFERENCES operation_state(operation_id) ON DELETE CASCADE,
    session_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS operation_session_associations_session_idx
    ON operation_session_associations (session_id);
