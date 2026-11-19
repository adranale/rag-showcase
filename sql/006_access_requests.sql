-- Access requests through a shared invite link, and admin operations. Idempotent.
--
-- The runtime role (API) gets no direct access to the new tables. Everything goes
-- through the SECURITY DEFINER functions below, which run as the migration role and
-- check the caller themselves:
--   * submit/status functions take the identity verified by the API (Microsoft sign-in);
--   * admin_* functions require that app.user_id (set by the API, like for row-level
--     security) belongs to an active user with is_admin.

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS access_requests (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entra_tid TEXT NOT NULL,
    entra_oid TEXT NOT NULL,
    email TEXT NOT NULL,
    display_name TEXT,
    message TEXT CHECK (length(message) <= 500),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'refused')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at TIMESTAMPTZ,
    decided_by BIGINT REFERENCES users(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS access_requests_one_pending_key
    ON access_requests (entra_tid, entra_oid) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS access_requests_identity_idx
    ON access_requests (entra_tid, entra_oid, created_at DESC);

CREATE OR REPLACE FUNCTION require_admin() RETURNS bigint
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
DECLARE
    caller bigint := app_user_id();
BEGIN
    IF caller IS NULL OR NOT EXISTS (
        SELECT 1 FROM users WHERE id = caller AND is_admin AND status = 'active'
    ) THEN
        RAISE EXCEPTION 'admin rights required' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN caller;
END
$$;

-- Result: created | pending | already_user | invalid_code | refused_recently | too_many
CREATE OR REPLACE FUNCTION submit_access_request(
    p_code text, p_tid text, p_oid text, p_email text, p_name text, p_message text
) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
DECLARE
    expected text;
    last_status text;
    last_decided timestamptz;
BEGIN
    SELECT value INTO expected FROM app_settings WHERE key = 'invite_code';
    IF expected IS NULL OR p_code IS NULL OR p_code <> expected THEN
        RETURN 'invalid_code';
    END IF;
    IF coalesce(p_tid, '') = '' OR coalesce(p_oid, '') = '' THEN
        RAISE EXCEPTION 'identity required' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF EXISTS (SELECT 1 FROM users WHERE entra_tid = lower(p_tid) AND entra_oid = p_oid) THEN
        RETURN 'already_user';
    END IF;

    SELECT status, decided_at INTO last_status, last_decided
    FROM access_requests
    WHERE entra_tid = lower(p_tid) AND entra_oid = p_oid
    ORDER BY created_at DESC LIMIT 1;
    IF last_status = 'pending' THEN
        RETURN 'pending';
    END IF;
    IF last_status = 'refused' AND last_decided > now() - interval '7 days' THEN
        RETURN 'refused_recently';
    END IF;
    -- The link may leak: cap the number of open requests.
    IF (SELECT count(*) FROM access_requests WHERE status = 'pending') >= 50 THEN
        RETURN 'too_many';
    END IF;

    INSERT INTO access_requests (entra_tid, entra_oid, email, display_name, message)
    VALUES (lower(p_tid), p_oid, lower(trim(coalesce(p_email, ''))),
            nullif(left(trim(coalesce(p_name, '')), 200), ''),
            nullif(left(trim(coalesce(p_message, '')), 500), ''));
    INSERT INTO audit_log (action) VALUES ('access_requested');
    RETURN 'created';
EXCEPTION WHEN unique_violation THEN
    RETURN 'pending';
END
$$;

-- Latest request of an identity and the state of its account (if any).
CREATE OR REPLACE FUNCTION access_request_status(p_tid text, p_oid text)
RETURNS TABLE (request_status text, created_at timestamptz, decided_at timestamptz,
               account_status text)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
    SELECT r.status, r.created_at, r.decided_at,
           (SELECT u.status FROM users u
            WHERE u.entra_tid = lower(p_tid) AND u.entra_oid = p_oid)
    FROM (SELECT 1) AS one
    LEFT JOIN LATERAL (
        SELECT status, created_at, decided_at FROM access_requests
        WHERE entra_tid = lower(p_tid) AND entra_oid = p_oid
        ORDER BY created_at DESC LIMIT 1
    ) AS r ON true;
$$;

CREATE OR REPLACE FUNCTION admin_list_requests(p_pending_only boolean)
RETURNS TABLE (id bigint, email text, display_name text, personal_account boolean,
               message text, status text, created_at timestamptz, decided_at timestamptz)
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
BEGIN
    PERFORM require_admin();
    RETURN QUERY
        SELECT r.id, r.email, r.display_name,
               r.entra_tid = '9188040d-6c67-4c5b-b112-36a304b66dad',
               r.message, r.status, r.created_at, r.decided_at
        FROM access_requests r
        WHERE NOT p_pending_only OR r.status = 'pending'
        ORDER BY r.created_at DESC
        LIMIT 200;
END
$$;

CREATE OR REPLACE FUNCTION admin_pending_count() RETURNS bigint
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
BEGIN
    PERFORM require_admin();
    RETURN (SELECT count(*) FROM access_requests WHERE status = 'pending');
END
$$;

-- Result: approved | refused | not_pending | email_in_use
CREATE OR REPLACE FUNCTION admin_decide_request(p_id bigint, p_approve boolean)
RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
DECLARE
    caller bigint := require_admin();
    r access_requests%ROWTYPE;
    existing users%ROWTYPE;
BEGIN
    SELECT * INTO r FROM access_requests WHERE id = p_id FOR UPDATE;
    IF NOT FOUND OR r.status <> 'pending' THEN
        RETURN 'not_pending';
    END IF;

    IF NOT p_approve THEN
        UPDATE access_requests SET status = 'refused', decided_at = now(), decided_by = caller
        WHERE id = p_id;
        INSERT INTO audit_log (user_id, action, detail)
        VALUES (caller, 'access_refused', p_id::text);
        RETURN 'refused';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM users WHERE entra_tid = r.entra_tid AND entra_oid = r.entra_oid
    ) THEN
        SELECT * INTO existing FROM users
        WHERE r.email <> '' AND lower(email) = r.email;
        IF FOUND THEN
            -- Only an unbound invitation of a personal account (verified e-mail) may be
            -- bound; work/school e-mail claims are not verified.
            IF existing.entra_oid IS NOT NULL
               OR r.entra_tid <> '9188040d-6c67-4c5b-b112-36a304b66dad' THEN
                RETURN 'email_in_use';
            END IF;
            UPDATE users
            SET entra_tid = r.entra_tid, entra_oid = r.entra_oid,
                display_name = coalesce(display_name, r.display_name),
                status = CASE WHEN status = 'disabled' THEN status ELSE 'active' END
            WHERE id = existing.id;
        ELSE
            INSERT INTO users (email, entra_tid, entra_oid, display_name, status)
            VALUES (CASE WHEN r.email = '' THEN r.entra_oid || '@unknown.invalid'
                         ELSE r.email END,
                    r.entra_tid, r.entra_oid, r.display_name, 'active');
        END IF;
    END IF;

    UPDATE access_requests SET status = 'approved', decided_at = now(), decided_by = caller
    WHERE id = p_id;
    INSERT INTO audit_log (user_id, action, detail)
    VALUES (caller, 'access_approved', p_id::text);
    RETURN 'approved';
END
$$;

CREATE OR REPLACE FUNCTION admin_list_users()
RETURNS TABLE (id bigint, email text, display_name text, status text, is_admin boolean,
               created_at timestamptz, last_login_at timestamptz, documents bigint)
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
BEGIN
    PERFORM require_admin();
    RETURN QUERY
        SELECT u.id, u.email, u.display_name, u.status, u.is_admin, u.created_at,
               u.last_login_at,
               (SELECT count(*) FROM documents d
                WHERE d.user_id = u.id AND d.ingestion_status = 'indexed')
        FROM users u
        ORDER BY u.id;
END
$$;

-- Result: active | invited | disabled | not_found | self
CREATE OR REPLACE FUNCTION admin_set_user_status(p_user_id bigint, p_status text)
RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
DECLARE
    caller bigint := require_admin();
    result text;
BEGIN
    IF p_status NOT IN ('active', 'disabled') THEN
        RAISE EXCEPTION 'invalid status' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_user_id = caller THEN
        RETURN 'self';
    END IF;
    UPDATE users
    SET status = CASE WHEN p_status = 'active' AND entra_oid IS NULL
                      THEN 'invited' ELSE p_status END
    WHERE id = p_user_id
    RETURNING status INTO result;
    IF result IS NULL THEN
        RETURN 'not_found';
    END IF;
    INSERT INTO audit_log (user_id, action, detail)
    VALUES (caller, 'user_' || p_status, p_user_id::text);
    RETURN result;
END
$$;

CREATE OR REPLACE FUNCTION admin_get_invite_code() RETURNS text
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
BEGIN
    PERFORM require_admin();
    RETURN (SELECT value FROM app_settings WHERE key = 'invite_code');
END
$$;

CREATE OR REPLACE FUNCTION admin_set_invite_code(p_code text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
DECLARE
    caller bigint := require_admin();
BEGIN
    IF p_code IS NULL OR p_code !~ '^[A-Za-z0-9_-]{16,128}$' THEN
        RAISE EXCEPTION 'invalid invite code' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    INSERT INTO app_settings (key, value) VALUES ('invite_code', p_code)
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
    INSERT INTO audit_log (user_id, action) VALUES (caller, 'invite_link_changed');
END
$$;

REVOKE ALL ON FUNCTION
    require_admin(), submit_access_request(text, text, text, text, text, text),
    access_request_status(text, text), admin_list_requests(boolean),
    admin_pending_count(), admin_decide_request(bigint, boolean), admin_list_users(),
    admin_set_user_status(bigint, text), admin_get_invite_code(),
    admin_set_invite_code(text)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    require_admin(), submit_access_request(text, text, text, text, text, text),
    access_request_status(text, text), admin_list_requests(boolean),
    admin_pending_count(), admin_decide_request(bigint, boolean), admin_list_users(),
    admin_set_user_status(bigint, text), admin_get_invite_code(),
    admin_set_invite_code(text)
TO rag_runtime;
