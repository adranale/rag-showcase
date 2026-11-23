# Multi-user mode (v2)

Invited users sign in with a Microsoft account, connect **their own Dropbox** and query **only their own documents**. Released as v2.0.0 (invite link and access requests since v2.1.0) and running on the Azure test deployment. Deployment: see [deployment.md](deployment.md#upgrade-to-multi-user-mode-v2).

## How it works

- **Isolation:** every row of `source_connections`, `documents`, `chunks`, `dropbox_credentials`, `sync_jobs`, `chat_usage` carries `user_id`; PostgreSQL row-level security (policy `owner_only`) filters by the session setting `app.user_id`, and queries additionally filter on `user_id`. The API and worker must connect as the non-owner role `rag_runtime` (RLS does not apply to the table owner / admin). Migration: `sql/005_multi_user.sql`.
- **Login:** Streamlit `st.login("microsoft")`; the UI signs the claims (`oid`, `tid`, `email`) into a 5-minute token (`USER_TOKEN_SECRET`, 32+ chars, shared by UI and API). The API accepts only invited, `active` users. Personal accounts are bound by e-mail on first login; work accounts must be invited with `--tid/--oid` (the e-mail claim is not trusted).
- **Invite link and access requests:** admins (`users.is_admin`; `OWNER_EMAIL` becomes admin in `ops/init_db.py`, others via `python -m ops.manage_users admin <email>`) see an **Admin** page with one shared invite link (`<ui>/?invite=<code>`, regenerate to invalidate it), the open requests (approve/refuse) and the user list (enable/disable). Whoever opens the link signs in with Microsoft and sends a request with an optional message; the code survives the sign-in redirect in a 1-hour cookie (or can be pasted). Approval creates an `active` user bound to that exact Microsoft identity (tenant + object ID). Refused people can ask again after 7 days; at most 50 requests can be open. With Azure Communication Services configured (`ACS_EMAIL_CONNECTION_STRING`, `ACS_EMAIL_SENDER`, `UI_BASE_URL`), every admin gets an e-mail per new request (name, address, message, link). The runtime role has no access to the request table: everything goes through `SECURITY DEFINER` functions (`sql/006_access_requests.sql`) that check the caller's admin flag via `app.user_id`, the same trust boundary as row-level security.
- **Dropbox:** per-user OAuth (PKCE, scopes `files.metadata.read` and `files.content.read`, offline refresh token). The refresh token is stored encrypted (Fernet, `TOKEN_ENCRYPTION_KEY`; comma-separated keys allow rotation). The redirect URI is the public UI root (`DROPBOX_REDIRECT_URI`).
- **Sync:** `POST /sync` queues a job (one open job per user); `ingestion/sync_worker.py` (a Container Apps Job triggered by the API through `WORKER_JOB_RESOURCE_ID`, or run manually) syncs and embeds per user. Quotas per user: `chats_per_day` (default 200), `files_per_sync` (default 2000; larger folders are rejected, never truncated). Logs contain counts and error types, never paths or text.
- **Audit:** `audit_log` records Dropbox connect/disconnect, document deletion, access requests and admin decisions, user enable/disable and invite-link changes (logins and syncs are not audited yet).

## API

- `GET /health` — basic service health check
- `GET /ready` — checks database availability and the pgvector extension
- `POST /search`, `POST /chat` — user-scoped retrieval / cited answers (need a signed user token)
- `GET /me`, `POST /dropbox/authorize`, `POST /dropbox/callback`, `PUT /dropbox/folder`, `DELETE /dropbox`, `POST /sync`, `DELETE /me/documents` — per-user Dropbox connection and sync; `GET /me` also reports index statistics and the indexed Dropbox folders (including data imported before sign-in existed, which has no stored credentials)
- `GET /documents/{id}/chunks` — passages of one of your documents in reading order (the UI's "View passages in context" dialog highlights the ones cited in an answer)
- `GET /access-request`, `POST /access-request` — status of / send an access request with the invite code (signed-in Microsoft identity, no account needed)
- `GET /admin/requests`, `POST /admin/requests/{id}/approve|refuse`, `GET /admin/users`, `PUT /admin/users/{id}/status`, `GET /admin/invite`, `POST /admin/invite` (new code) — admins only

When `API_KEY` is set, the user endpoints also require the `X-API-Key` header (the UI sends it).

## Local setup

```powershell
# .env: DATABASE_URL (runtime role), ADMIN_DATABASE_URL (owner), USER_TOKEN_SECRET, TOKEN_ENCRYPTION_KEY,
#       DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REDIRECT_URI, AUTH_REDIRECT_URI, AUTH_COOKIE_SECRET, MS_CLIENT_ID, MS_CLIENT_SECRET
python -c "from app.crypto import generate_key; print(generate_key())"     # TOKEN_ENCRYPTION_KEY
$env:OWNER_EMAIL = 'you@example.org'; python -m ops.init_db                 # migrate; existing data goes to the owner
$env:RUNTIME_DB_PASSWORD = '<24+ chars>'; python -m ops.setup_runtime_role  # login password for rag_runtime
python -m ops.manage_users invite friend@outlook.com                        # personal account
python -m ops.manage_users invite colleague@corp.com --tid <tenant-id> --oid <object-id>
python -m ops.manage_users list                                             # also: enable|disable <email>, quota <email> --chats-per-day N --files-per-sync N
python -m ops.manage_users admin you@outlook.com                            # admin page (invite link, requests); unadmin <email> removes it
python -m ingestion.sync_worker                                             # process queued sync jobs
python -m ui.start                                                          # UI with Microsoft login (needs the auth variables)
```

Register an **Entra app** (redirect URI `<ui-url>/oauth2callback`, web platform, accounts: personal Microsoft accounts) and a **Dropbox app** (scopes above, redirect URI = UI root). `MS_TENANT` defaults to `consumers`.

## Limitations (read before inviting people)

- **Work/school accounts:** Streamlit's login fails for the `common`/`organizations` endpoints (templated issuer, streamlit/streamlit#11391). Only personal Microsoft accounts work until that is solved; the allowlist logic for work accounts is in place.
- **The operator can still read data.** Database administrators (Azure admin credentials) bypass RLS, the app owner can grant themselves access, and embeddings can be inverted. The baseline reduces exposure (runtime role without admin rights, encrypted tokens, audit log, no content in logs) but is not a technical guarantee. Tell users this.
- Key Vault is not used yet (env key; `app/crypto.py` is the swap point). Dropbox apps in development mode may cap the number of linked users; check before inviting. OCR budget is per job, not per day. No connection pool; retrieval is serialised.
