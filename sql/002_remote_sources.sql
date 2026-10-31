-- Remote sources (Dropbox / Google Drive): track files that disappeared remotely.
-- Removed documents keep a tombstone row (no chunks) so the removal is auditable
-- and a re-appearing file is re-ingested. Idempotent.

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS removed_at TIMESTAMPTZ;

ALTER TABLE documents
    DROP CONSTRAINT IF EXISTS documents_ingestion_status_check;

ALTER TABLE documents
    ADD CONSTRAINT documents_ingestion_status_check
    CHECK (ingestion_status IN ('pending', 'indexed', 'failed', 'removed'));

CREATE INDEX IF NOT EXISTS documents_connection_status_idx
    ON documents (connection_id, ingestion_status);
