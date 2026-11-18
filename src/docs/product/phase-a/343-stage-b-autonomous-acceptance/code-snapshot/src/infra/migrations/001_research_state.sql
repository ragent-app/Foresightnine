-- ForesightNine research-state document storage.
-- Forward-only, credential-free, and safe to run more than once.

CREATE TABLE IF NOT EXISTS research_state_documents (
    collection text NOT NULL,
    document_key text NOT NULL,
    document jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at timestamptz,
    version bigint NOT NULL DEFAULT 1,
    CONSTRAINT research_state_documents_collection_key
        PRIMARY KEY (collection, document_key),
    CONSTRAINT research_state_documents_collection_format
        CHECK (collection ~ '^[a-z][a-z0-9_]{0,62}$'),
    CONSTRAINT research_state_documents_key_nonblank
        CHECK (length(document_key) > 0),
    CONSTRAINT research_state_documents_version_positive
        CHECK (version >= 1)
);

CREATE INDEX IF NOT EXISTS research_state_documents_expiry_idx
    ON research_state_documents (expires_at)
    WHERE expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS research_state_documents_collection_updated_idx
    ON research_state_documents (collection, updated_at);
