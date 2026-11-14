-- Files that can never be processed (no text, encrypted, too small for OCR, ...)
-- get status 'unsupported' and keep their source_version, so they are not
-- downloaded or OCR'd again until the remote file changes. Idempotent.

ALTER TABLE documents
    DROP CONSTRAINT IF EXISTS documents_ingestion_status_check;

ALTER TABLE documents
    ADD CONSTRAINT documents_ingestion_status_check
    CHECK (ingestion_status IN (
        'pending', 'indexed', 'failed', 'removed', 'unsupported'
    ));
