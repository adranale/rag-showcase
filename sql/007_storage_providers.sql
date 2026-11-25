-- Per-user cloud storage: each user connects exactly one provider (Dropbox or
-- Google Drive) and chooses one folder. Replaces dropbox_credentials, whose rows
-- are moved here (not copied: a re-run must not bring back a disconnected account).
-- Applied on every init_db run, so every statement is idempotent.

CREATE TABLE IF NOT EXISTS storage_credentials (
    user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('dropbox', 'google_drive')),
    refresh_token_encrypted BYTEA NOT NULL,
    account_id TEXT,
    -- Dropbox: folder path; Google Drive: folder ID. NULL until chosen.
    folder TEXT,
    folder_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

WITH moved AS (
    DELETE FROM dropbox_credentials
    RETURNING user_id, refresh_token_encrypted, account_id, folder, created_at, updated_at
)
INSERT INTO storage_credentials (
    user_id, provider, refresh_token_encrypted, account_id, folder, folder_name,
    created_at, updated_at
)
SELECT user_id, 'dropbox', refresh_token_encrypted, account_id, folder, folder,
       created_at, updated_at
FROM moved
-- A row written by an old app version after the first move wins if it is newer.
ON CONFLICT (user_id) DO UPDATE SET
    provider = EXCLUDED.provider,
    refresh_token_encrypted = EXCLUDED.refresh_token_encrypted,
    account_id = EXCLUDED.account_id,
    folder = EXCLUDED.folder,
    folder_name = EXCLUDED.folder_name,
    updated_at = EXCLUDED.updated_at
WHERE EXCLUDED.updated_at > storage_credentials.updated_at;

-- The provider whose authorization a state belongs to (existing rows: Dropbox).
ALTER TABLE oauth_states
    ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'dropbox'
        CHECK (provider IN ('dropbox', 'google_drive'));

ALTER TABLE storage_credentials ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS owner_only ON storage_credentials;
CREATE POLICY owner_only ON storage_credentials TO rag_runtime
    USING (user_id = (SELECT app_user_id()))
    WITH CHECK (user_id = (SELECT app_user_id()));

GRANT SELECT, INSERT, UPDATE, DELETE ON storage_credentials TO rag_runtime;
