-- Multi-user support: every document belongs to a user, and a non-owner runtime
-- role only sees its own rows (row-level security). Idempotent.
--
-- The role that applies the migrations (the table owner / DB admin) is NOT subject
-- to the policies. The API and the sync worker must connect as `rag_runtime`
-- (see ops/setup_runtime_role.py) so that the isolation is enforced by the database.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_runtime') THEN
        CREATE ROLE rag_runtime NOLOGIN NOBYPASSRLS;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS users (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email TEXT NOT NULL,
    entra_tid TEXT,
    entra_oid TEXT,
    display_name TEXT,
    status TEXT NOT NULL DEFAULT 'invited'
        CHECK (status IN ('invited', 'active', 'disabled')),
    chats_per_day INTEGER NOT NULL DEFAULT 200 CHECK (chats_per_day >= 0),
    files_per_sync INTEGER NOT NULL DEFAULT 2000 CHECK (files_per_sync >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS users_email_key ON users (lower(email));
CREATE UNIQUE INDEX IF NOT EXISTS users_identity_key
    ON users (entra_tid, entra_oid) WHERE entra_oid IS NOT NULL;

-- Ownership columns. Nullable so that data created before this migration can be
-- assigned to the owner by ops/init_db.py (OWNER_EMAIL); documents and chunks inherit
-- the owner of their parent through the triggers below.
ALTER TABLE source_connections
    ADD COLUMN IF NOT EXISTS user_id BIGINT REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS user_id BIGINT REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS user_id BIGINT REFERENCES users(id) ON DELETE CASCADE;

-- The embedding columns used to be created by embed_chunks.py (needs table
-- ownership); the runtime role must find them in place.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding vector(384);
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding_model TEXT;

ALTER TABLE source_connections
    DROP CONSTRAINT IF EXISTS source_connections_provider_account_identifier_key;
CREATE UNIQUE INDEX IF NOT EXISTS source_connections_owner_key
    ON source_connections (user_id, provider, account_identifier) NULLS NOT DISTINCT;

CREATE INDEX IF NOT EXISTS documents_user_idx ON documents (user_id);
CREATE INDEX IF NOT EXISTS chunks_user_idx ON chunks (user_id);

CREATE OR REPLACE FUNCTION documents_inherit_owner() RETURNS trigger AS $$
BEGIN
    SELECT user_id INTO NEW.user_id FROM source_connections WHERE id = NEW.connection_id;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION chunks_inherit_owner() RETURNS trigger AS $$
BEGIN
    SELECT user_id INTO NEW.user_id FROM documents WHERE id = NEW.document_id;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS documents_inherit_owner ON documents;
CREATE TRIGGER documents_inherit_owner BEFORE INSERT OR UPDATE OF connection_id
    ON documents FOR EACH ROW EXECUTE FUNCTION documents_inherit_owner();

DROP TRIGGER IF EXISTS chunks_inherit_owner ON chunks;
CREATE TRIGGER chunks_inherit_owner BEFORE INSERT OR UPDATE OF document_id
    ON chunks FOR EACH ROW EXECUTE FUNCTION chunks_inherit_owner();

CREATE TABLE IF NOT EXISTS dropbox_credentials (
    user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    refresh_token_encrypted BYTEA NOT NULL,
    account_id TEXT,
    folder TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_verifier TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sync_jobs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    message TEXT,
    summary JSONB,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

-- At most one open job per user.
CREATE UNIQUE INDEX IF NOT EXISTS sync_jobs_one_open_key
    ON sync_jobs (user_id) WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS chat_usage (
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day DATE NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

-- Security-relevant events only (never document content or tokens).
CREATE TABLE IF NOT EXISTS audit_log (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    detail TEXT
);

CREATE OR REPLACE FUNCTION app_user_id() RETURNS bigint
    LANGUAGE sql STABLE
    AS $$ SELECT NULLIF(current_setting('app.user_id', true), '')::bigint $$;

ALTER TABLE source_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE dropbox_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE oauth_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_usage ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS owner_only ON source_connections;
CREATE POLICY owner_only ON source_connections TO rag_runtime
    USING (user_id = (SELECT app_user_id()))
    WITH CHECK (user_id = (SELECT app_user_id()));

DROP POLICY IF EXISTS owner_only ON documents;
CREATE POLICY owner_only ON documents TO rag_runtime
    USING (user_id = (SELECT app_user_id()))
    WITH CHECK (user_id = (SELECT app_user_id()));

DROP POLICY IF EXISTS owner_only ON chunks;
CREATE POLICY owner_only ON chunks TO rag_runtime
    USING (user_id = (SELECT app_user_id()))
    WITH CHECK (user_id = (SELECT app_user_id()));

DROP POLICY IF EXISTS owner_only ON dropbox_credentials;
CREATE POLICY owner_only ON dropbox_credentials TO rag_runtime
    USING (user_id = (SELECT app_user_id()))
    WITH CHECK (user_id = (SELECT app_user_id()));

DROP POLICY IF EXISTS owner_only ON oauth_states;
CREATE POLICY owner_only ON oauth_states TO rag_runtime
    USING (user_id = (SELECT app_user_id()))
    WITH CHECK (user_id = (SELECT app_user_id()));

DROP POLICY IF EXISTS owner_only ON chat_usage;
CREATE POLICY owner_only ON chat_usage TO rag_runtime
    USING (user_id = (SELECT app_user_id()))
    WITH CHECK (user_id = (SELECT app_user_id()));

GRANT USAGE ON SCHEMA public TO rag_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    source_connections, documents, chunks, dropbox_credentials, oauth_states,
    chat_usage, sync_jobs TO rag_runtime;
-- Users are invited by the owner (manage_users.py); the runtime may only read them
-- and record the first login.
GRANT SELECT ON users TO rag_runtime;
GRANT UPDATE (entra_tid, entra_oid, display_name, status, last_login_at)
    ON users TO rag_runtime;
GRANT INSERT ON audit_log TO rag_runtime;
