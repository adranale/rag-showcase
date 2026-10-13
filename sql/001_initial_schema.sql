CREATE TABLE IF NOT EXISTS source_connections (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider TEXT NOT NULL
        CHECK (provider IN ('local', 'google_drive', 'dropbox')),
    account_identifier TEXT NOT NULL,
    sync_cursor TEXT,
    last_successful_sync TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (provider, account_identifier)
);

CREATE TABLE IF NOT EXISTS documents (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    connection_id BIGINT NOT NULL
        REFERENCES source_connections(id) ON DELETE CASCADE,
    external_file_id TEXT NOT NULL,
    title TEXT NOT NULL,
    source_url TEXT,
    mime_type TEXT,
    content_hash TEXT,
    source_version TEXT,
    ingestion_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (ingestion_status IN ('pending', 'indexed', 'failed')),
    indexed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (connection_id, external_file_id)
);

CREATE TABLE IF NOT EXISTS chunks (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id BIGINT NOT NULL
        REFERENCES documents(id) ON DELETE CASCADE,
    chunk_number INTEGER NOT NULL CHECK (chunk_number >= 0),
    text TEXT NOT NULL CHECK (length(trim(text)) > 0),
    location_metadata JSONB NOT NULL DEFAULT '{}',
    chunking_version TEXT NOT NULL DEFAULT 'v1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (document_id, chunk_number)
);

INSERT INTO source_connections (provider, account_identifier)
VALUES ('local', 'sample-folder')
ON CONFLICT (provider, account_identifier) DO NOTHING;