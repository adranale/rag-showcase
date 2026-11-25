"""Multi-user isolation against a real PostgreSQL (with pgvector).

Skipped unless TEST_DATABASE_URL points at an admin connection (any database of a
disposable server); the test creates and drops its own database `rag_mu_test`.
"""

import os
import uuid
from pathlib import Path

import numpy as np
import psycopg
import pytest
from pgvector.psycopg import register_vector
from psycopg.conninfo import make_conninfo

ADMIN_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="TEST_DATABASE_URL not set")

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
RUNTIME_PASSWORD = "runtime-test-password-1234567890"
PERSONAL = "9188040d-6c67-4c5b-b112-36a304b66dad"


@pytest.fixture(scope="module")
def admin_url():
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS rag_mu_test WITH (FORCE);")
        conn.execute("CREATE DATABASE rag_mu_test;")
    url = make_conninfo(ADMIN_URL, dbname="rag_mu_test")
    with psycopg.connect(url) as conn:
        for path in sorted(SQL_DIR.glob("*.sql")):
            conn.execute(path.read_text(encoding="utf-8"))
        # Applying the migrations twice must be harmless.
        for path in sorted(SQL_DIR.glob("*.sql")):
            conn.execute(path.read_text(encoding="utf-8"))
        conn.execute(
            f"ALTER ROLE rag_runtime WITH LOGIN PASSWORD '{RUNTIME_PASSWORD}'"
        )
    yield url
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS rag_mu_test WITH (FORCE);")


@pytest.fixture
def runtime(admin_url, monkeypatch):
    """Point the app at the non-owner runtime role."""
    url = make_conninfo(admin_url, user="rag_runtime", password=RUNTIME_PASSWORD)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("POSTGRES_RESOURCE_ID", raising=False)
    return url


def make_user(admin_url, **fields) -> int:
    values = {
        "email": f"{uuid.uuid4().hex[:8]}@example.org",
        "status": "active",
        "chats_per_day": 200,
        "files_per_sync": 2000,
        **fields,
    }
    with psycopg.connect(admin_url) as conn:
        return conn.execute(
            """
            INSERT INTO users (email, status, chats_per_day, files_per_sync,
                               entra_tid, entra_oid)
            VALUES (%(email)s, %(status)s, %(chats_per_day)s, %(files_per_sync)s,
                    %(entra_tid)s, %(entra_oid)s) RETURNING id;
            """,
            {"entra_tid": None, "entra_oid": None, **values},
        ).fetchone()[0]


def load_user(admin_url, user_id):
    from app.users import User

    with psycopg.connect(admin_url) as conn:
        row = conn.execute(
            "SELECT id, email, status, chats_per_day, files_per_sync "
            "FROM users WHERE id = %s;",
            (user_id,),
        ).fetchone()
    return User(*row)


def add_document(admin_url, user_id, title, embedding=None):
    with psycopg.connect(admin_url) as conn:
        register_vector(conn)
        connection_id = conn.execute(
            """
            INSERT INTO source_connections (user_id, provider, account_identifier)
            VALUES (%s, 'dropbox', %s) RETURNING id;
            """,
            (user_id, f"/{title}"),
        ).fetchone()[0]
        document_id = conn.execute(
            """
            INSERT INTO documents (connection_id, external_file_id, title,
                                   ingestion_status)
            VALUES (%s, 'f1', %s, 'indexed') RETURNING id;
            """,
            (connection_id, title),
        ).fetchone()[0]
        from app.search import MODEL_NAME

        conn.execute(
            """
            INSERT INTO chunks (document_id, chunk_number, text, embedding,
                                embedding_model)
            VALUES (%s, 0, %s, %s, %s);
            """,
            (document_id, f"secret text of {title}", embedding, MODEL_NAME),
        )


def test_row_level_security_isolates_users(admin_url, runtime):
    from app.db import get_connection

    a, b = make_user(admin_url), make_user(admin_url)
    add_document(admin_url, a, "alpha")
    add_document(admin_url, b, "beta")

    with psycopg.connect(admin_url) as conn:
        owners = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT text, user_id FROM chunks WHERE user_id IN (%s, %s);", (a, b)
            )
        }
    assert owners == {"secret text of alpha": a, "secret text of beta": b}

    with get_connection(a) as conn:
        texts = [r[0] for r in conn.execute("SELECT text FROM chunks;")]
        assert texts == ["secret text of alpha"]
        assert conn.execute("SELECT count(*) FROM documents;").fetchone()[0] == 1

    with get_connection() as conn:  # no user context: nothing is visible
        for table in ("chunks", "documents", "source_connections"):
            assert conn.execute(f"SELECT count(*) FROM {table};").fetchone()[0] == 0

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with get_connection(a) as conn:
            conn.execute(
                "INSERT INTO source_connections (user_id, provider, account_identifier)"
                " VALUES (%s, 'dropbox', '/stolen');",
                (b,),
            )

    # The trigger assigns the parent's owner: a foreign user_id cannot be forged.
    with get_connection(a) as conn:
        connection_id = conn.execute("SELECT id FROM source_connections;").fetchone()[0]
        conn.execute(
            "INSERT INTO documents (connection_id, external_file_id, title, user_id)"
            " VALUES (%s, 'f2', 'forged', %s);",
            (connection_id, b),
        )
        assert conn.execute(
            "SELECT user_id FROM documents WHERE title = 'forged';"
        ).fetchone()[0] == a


def test_runtime_cannot_manage_users(admin_url, runtime):
    from app.db import get_connection

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with get_connection() as conn:
            conn.execute("INSERT INTO users (email) VALUES ('x@example.org');")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with get_connection() as conn:
            conn.execute("UPDATE users SET chats_per_day = 1000000;")


def test_search_returns_only_own_chunks(admin_url, runtime):
    from app.search import search_documents

    class FakeModel:
        max_seq_length = 128

        def __init__(self):
            self.tokenizer = lambda text, **_: {"input_ids": [1, 2, 3]}

        def encode(self, text, **_):
            return np.ones(384, dtype=np.float32) / np.sqrt(384)

    vector = np.ones(384, dtype=np.float32) / np.sqrt(384)
    a, b = make_user(admin_url), make_user(admin_url)
    add_document(admin_url, a, "searchalpha", vector)
    add_document(admin_url, b, "searchbeta", vector)

    results = search_documents(FakeModel(), "searchalpha searchbeta", 10, user_id=a)
    assert [r["title"] for r in results] == ["searchalpha"]


def test_store_creates_user_owned_documents(admin_url, runtime):
    from connectors.base import RemoteFile
    from connectors.chunking import ChunkedDocument
    from connectors.store import PostgresStore

    a, b = make_user(admin_url), make_user(admin_url)
    remote = RemoteFile("id:1", "note.txt", "rev1", "text", "text/plain")
    document = ChunkedDocument("hash", "v1", [{"text": "hello", "location_metadata": {}}])

    store_a = PostgresStore(a)
    connection_id = store_a.ensure_connection("dropbox", "/same-folder")
    store_a.save(connection_id, remote, document)
    assert set(store_a.documents(connection_id)) == {"id:1"}

    # The same folder name of another user is a separate, empty connection.
    store_b = PostgresStore(b)
    other_id = store_b.ensure_connection("dropbox", "/same-folder")
    assert other_id != connection_id
    assert store_b.documents(other_id) == {}
    assert store_b.documents(connection_id) == {}


def test_document_chunks_and_status_are_scoped_to_the_owner(admin_url, runtime):
    from app import documents, storage

    a, b = make_user(admin_url), make_user(admin_url)
    add_document(admin_url, a, "alpha")
    add_document(admin_url, b, "beta")
    user_a, user_b = load_user(admin_url, a), load_user(admin_url, b)

    with psycopg.connect(admin_url) as conn:
        document_id = conn.execute(
            "SELECT id FROM documents WHERE user_id = %s;", (a,)
        ).fetchone()[0]

    result = documents.document_chunks(user_a, document_id)
    assert [c["text"] for c in result["chunks"]] == ["secret text of alpha"]
    assert documents.document_chunks(user_b, document_id) is None

    # Indexed data without stored credentials still shows up (pre-v2 import).
    status = storage.status(user_a)
    assert status["connected"] is False and status["provider"] is None
    assert status["documents_indexed"] == 1 and status["chunks"] == 1
    assert [(s["provider"], s["folder"]) for s in status["sources"]] == [("dropbox", "/alpha")]
    assert status["sources"][0]["documents_indexed"] == 1


def test_authenticate_binds_personal_account_once(admin_url, runtime):
    from app import users

    email = f"{uuid.uuid4().hex[:8]}@outlook.com"
    with psycopg.connect(admin_url) as conn:
        conn.execute("INSERT INTO users (email) VALUES (%s);", (email,))

    claims = {"oid": "oid-1", "tid": PERSONAL, "email": email.upper(), "name": "Pat"}
    first = users.authenticate(claims)
    assert first is not None and first.status == "active"
    assert users.authenticate(claims).id == first.id

    # Another identity cannot claim the same invitation, nor can a work tenant.
    assert users.authenticate({**claims, "oid": "oid-2"}) is None
    assert users.authenticate({**claims, "oid": "oid-3", "tid": "tenant-x"}) is None
    assert users.authenticate({"oid": "oid-4", "tid": PERSONAL, "email": "nobody@x.org"}) is None

    with psycopg.connect(admin_url) as conn:
        conn.execute("UPDATE users SET status = 'disabled' WHERE id = %s;", (first.id,))
    assert users.authenticate(claims) is None


def test_work_account_needs_bound_identity(admin_url, runtime):
    from app import users

    email = f"{uuid.uuid4().hex[:8]}@corp.example"
    make_user(admin_url, email=email, entra_tid="tenant-1", entra_oid="oid-w")
    assert users.authenticate({"oid": "oid-w", "tid": "tenant-1", "email": "x"}).email == email
    # Same e-mail claim from a different tenant is not accepted.
    assert users.authenticate({"oid": "oid-z", "tid": "tenant-2", "email": email}) is None


def test_chat_quota(admin_url, runtime):
    from app import quota

    user = load_user(admin_url, make_user(admin_url, chats_per_day=2))
    quota.consume_chat(user)
    quota.consume_chat(user)
    with pytest.raises(quota.QuotaExceeded):
        quota.consume_chat(user)

    disabled = load_user(admin_url, make_user(admin_url, chats_per_day=0))
    with pytest.raises(quota.QuotaExceeded):
        quota.consume_chat(disabled)


def test_job_queue(admin_url, runtime):
    from app import jobs

    user_id = make_user(admin_url)
    job, created = jobs.enqueue(user_id)
    again, created_again = jobs.enqueue(user_id)
    assert created and not created_again and again["id"] == job["id"]

    claimed = jobs.claim_next()
    assert claimed["id"] == job["id"] and claimed["status"] == "running"
    assert jobs.claim_next() is None

    jobs.finish(job["id"], True, None, {"ingested": 3})
    assert jobs.latest(user_id)["summary"] == {"ingested": 3}
    _, created_next = jobs.enqueue(user_id)
    assert created_next


def test_storage_oauth_flow(admin_url, runtime, monkeypatch):
    import httpx

    from app import crypto, dropbox_oauth, storage

    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", crypto.generate_key())
    monkeypatch.setenv("DROPBOX_APP_KEY", "key")
    monkeypatch.setenv("DROPBOX_APP_SECRET", "secret")
    monkeypatch.setenv("DROPBOX_REDIRECT_URI", "https://ui.example/")

    def fake_post(url, **kwargs):
        assert kwargs["data"]["code_verifier"]
        return httpx.Response(
            200,
            json={
                "refresh_token": "REFRESH-SECRET",
                "account_id": "dbid:1",
                "scope": "files.metadata.read files.content.read",
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    owner = load_user(admin_url, make_user(admin_url))
    intruder = load_user(admin_url, make_user(admin_url))

    url = storage.start_authorization(owner, "dropbox")
    assert "code_challenge_method=S256" in url and "scope=files.metadata.read" in url
    state = url.split("state=")[1].split("&")[0]

    with pytest.raises(storage.OAuthError):  # state belongs to another user
        storage.complete_authorization(intruder, "code", state)
    assert storage.complete_authorization(owner, "code", state) == "dropbox"
    with pytest.raises(storage.OAuthError):  # single use
        storage.complete_authorization(owner, "code", state)

    with psycopg.connect(admin_url) as conn:
        stored = conn.execute(
            "SELECT refresh_token_encrypted FROM storage_credentials WHERE user_id = %s;",
            (owner.id,),
        ).fetchone()[0]
    assert b"REFRESH-SECRET" not in bytes(stored)
    assert storage.load_credentials(owner.id) == ("dropbox", "REFRESH-SECRET", None)
    assert storage.load_credentials(intruder.id) is None

    revoked = []
    monkeypatch.setattr(dropbox_oauth, "revoke", lambda token: revoked.append(token))
    storage.disconnect(owner, delete_documents=False)
    assert revoked == ["REFRESH-SECRET"] and storage.load_credentials(owner.id) is None


def add_source_document(admin_url, user_id, provider, folder, title):
    with psycopg.connect(admin_url) as conn:
        connection_id = conn.execute(
            """
            INSERT INTO source_connections (user_id, provider, account_identifier)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, provider, account_identifier)
            DO UPDATE SET account_identifier = EXCLUDED.account_identifier
            RETURNING id;
            """,
            (user_id, provider, folder),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO documents (connection_id, external_file_id, title, ingestion_status) "
            "VALUES (%s, %s, %s, 'indexed');",
            (connection_id, title, title),
        )


def titles(admin_url, user_id):
    with psycopg.connect(admin_url) as conn:
        rows = conn.execute(
            "SELECT title FROM documents WHERE user_id = %s ORDER BY title;", (user_id,)
        ).fetchall()
    return [row[0] for row in rows]


def test_switching_provider_or_folder_deletes_only_after_confirmation(
    admin_url, runtime, monkeypatch
):
    from app import crypto, dropbox_oauth, gdrive_oauth, storage

    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", crypto.generate_key())
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "sec")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "https://ui.example/")
    user_id = make_user(admin_url)
    user = load_user(admin_url, user_id)
    other = make_user(admin_url)
    with psycopg.connect(admin_url) as conn:
        conn.execute(
            "INSERT INTO storage_credentials (user_id, provider, refresh_token_encrypted, "
            "folder, folder_name) VALUES (%s, 'dropbox', %s, '/alpha', '/alpha');",
            (user_id, crypto.encrypt("DROPBOX-TOKEN")),
        )
    add_source_document(admin_url, user_id, "dropbox", "/alpha", "dropbox-doc")
    add_source_document(admin_url, user_id, "local", "sample-folder", "sample-doc")
    add_source_document(admin_url, other, "dropbox", "/alpha", "other-users-doc")

    # Switching to Google Drive would delete the Dropbox documents: confirm first.
    with pytest.raises(storage.ConfirmationRequired) as pending:
        storage.start_authorization(user, "google_drive")
    assert pending.value.documents == 1
    url = storage.start_authorization(user, "google_drive", confirm_delete=True)
    state = url.split("state=")[1].split("&")[0]
    assert "accounts.google.com" in url

    revoked = []
    monkeypatch.setattr(dropbox_oauth, "revoke", lambda token: revoked.append(token))
    monkeypatch.setattr(
        gdrive_oauth, "exchange_code", lambda code, verifier: ("GOOGLE-TOKEN", "me@gmail.com")
    )
    assert storage.complete_authorization(user, "code", state) == "google_drive"
    assert revoked == ["DROPBOX-TOKEN"]
    assert storage.load_credentials(user_id) == ("google_drive", "GOOGLE-TOKEN", None)
    # Local sample data and other users are never touched.
    assert titles(admin_url, user_id) == ["sample-doc"]
    assert titles(admin_url, other) == ["other-users-doc"]

    # Changing the Drive folder deletes the documents of the previous folder.
    folders = {"https://drive.google.com/drive/folders/NEW": ("NEW", "Notes")}
    monkeypatch.setattr(
        gdrive_oauth, "resolve_folder", lambda token, value: folders[value]
    )
    add_source_document(admin_url, user_id, "google_drive", "OLD", "old-drive-doc")
    link = "https://drive.google.com/drive/folders/NEW"
    with pytest.raises(storage.ConfirmationRequired):
        storage.set_folder(user, link)
    assert titles(admin_url, user_id) == ["old-drive-doc", "sample-doc"]
    result = storage.set_folder(user, link, confirm_delete=True)
    assert result == {"folder": "NEW", "folder_name": "Notes", "documents_deleted": 1}
    assert titles(admin_url, user_id) == ["sample-doc"]

    # Saving the same folder again needs no confirmation and keeps its documents.
    add_source_document(admin_url, user_id, "google_drive", "NEW", "new-drive-doc")
    assert storage.set_folder(user, link)["documents_deleted"] == 0
    status = storage.status(user)
    assert status["provider"] == "google_drive" and status["account"] == "me@gmail.com"
    assert status["folder_name"] == "Notes"
    assert [(s["provider"], s["folder"]) for s in status["sources"]] == [
        ("google_drive", "Notes")
    ]

    # Reconnecting the same provider keeps the folder.
    url = storage.start_authorization(user, "google_drive")
    storage.complete_authorization(user, "code", url.split("state=")[1].split("&")[0])
    assert storage.load_credentials(user_id).folder == "NEW"
    assert titles(admin_url, user_id) == ["new-drive-doc", "sample-doc"]


def test_storage_migration_moves_dropbox_credentials(admin_url, runtime):
    from app.db import get_connection

    a, b = make_user(admin_url), make_user(admin_url)
    with psycopg.connect(admin_url) as conn:
        conn.execute(
            "INSERT INTO dropbox_credentials (user_id, refresh_token_encrypted, folder) "
            "VALUES (%s, %s, '/legacy');",
            (a, b"encrypted"),
        )
        migration = (SQL_DIR / "007_storage_providers.sql").read_text(encoding="utf-8")
        conn.execute(migration)
        conn.execute(migration)  # moved, not copied: a re-run changes nothing
        assert conn.execute("SELECT count(*) FROM dropbox_credentials;").fetchone()[0] == 0
        rows = conn.execute(
            "SELECT user_id, provider, folder, folder_name FROM storage_credentials "
            "WHERE user_id = %s;",
            (a,),
        ).fetchall()
    assert rows == [(a, "dropbox", "/legacy", "/legacy")]

    # Row-level security: another user cannot see the credentials.
    with get_connection(b) as conn:
        assert conn.execute("SELECT count(*) FROM storage_credentials;").fetchone()[0] == 0
    with get_connection(a) as conn:
        assert conn.execute("SELECT count(*) FROM storage_credentials;").fetchone()[0] == 1


def make_admin(admin_url):
    from dataclasses import replace

    user_id = make_user(admin_url)
    with psycopg.connect(admin_url) as conn:
        conn.execute("UPDATE users SET is_admin = true WHERE id = %s;", (user_id,))
    return replace(load_user(admin_url, user_id), is_admin=True)


def test_access_request_flow(admin_url, runtime):
    from app import access, users

    admin = make_admin(admin_url)
    member = load_user(admin_url, make_user(admin_url))
    code = access.invite_code(admin)
    assert access.invite_code(admin) == code  # created once, then stable
    oid = uuid.uuid4().hex
    claims = {"oid": oid, "tid": PERSONAL.upper(), "email": f"{oid[:8]}@outlook.com",
              "name": "New Person"}

    assert access.submit_request(claims, "wrong-code-123456789", None) == "invalid_code"
    assert access.submit_request(claims, code, "hello") == "created"
    assert access.submit_request(claims, code, "again") == "pending"
    assert access.request_status(claims)["request_status"] == "pending"
    assert users.authenticate(claims) is None

    # The runtime role cannot read or change requests directly.
    from app.db import get_connection

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with get_connection(admin.id) as conn:
            conn.execute("SELECT * FROM access_requests;")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        access.list_requests(member, pending_only=True)

    pending = access.list_requests(admin, pending_only=True)
    request = next(r for r in pending if r["email"] == claims["email"])
    assert request["personal_account"] and request["message"] == "hello"
    assert access.pending_count(admin) >= 1

    assert access.decide(admin, request["id"], approve=True) == "approved"
    assert access.decide(admin, request["id"], approve=False) == "not_pending"
    user = users.authenticate(claims)
    assert user is not None and user.status == "active" and not user.is_admin
    assert access.submit_request(claims, code, None) == "already_user"

    # Refused: no new request for a week.
    other = {**claims, "oid": uuid.uuid4().hex, "email": "x@outlook.com"}
    assert access.submit_request(other, code, None) == "created"
    other_id = next(
        r["id"] for r in access.list_requests(admin, True) if r["email"] == "x@outlook.com"
    )
    assert access.decide(admin, other_id, approve=False) == "refused"
    assert access.submit_request(other, code, None) == "refused_recently"
    with psycopg.connect(admin_url) as conn:
        conn.execute(
            "UPDATE access_requests SET decided_at = now() - interval '8 days' WHERE id = %s;",
            (other_id,),
        )
    assert access.submit_request(other, code, None) == "created"

    # A new link invalidates the old one.
    new_code = access.rotate_invite_code(admin)
    assert new_code != code
    assert access.submit_request({**claims, "oid": "z" * 10}, code, None) == "invalid_code"


def test_approval_does_not_take_over_a_bound_account(admin_url, runtime):
    from app import access, users

    admin = make_admin(admin_url)
    code = access.invite_code(admin)
    taken = f"{uuid.uuid4().hex[:8]}@outlook.com"
    make_user(admin_url, email=taken, entra_tid=PERSONAL, entra_oid="owner-oid")
    claims = {"oid": "other-oid", "tid": PERSONAL, "email": taken, "name": "Copy"}
    assert access.submit_request(claims, code, None) == "created"
    request_id = next(r["id"] for r in access.list_requests(admin, True) if r["email"] == taken)
    assert access.decide(admin, request_id, approve=True) == "email_in_use"
    assert users.authenticate(claims) is None


def test_admin_user_management(admin_url, runtime):
    from app import access, users

    admin = make_admin(admin_url)
    target_id = make_user(admin_url, entra_tid=PERSONAL, entra_oid=uuid.uuid4().hex)
    listed = {u["id"]: u for u in access.list_users(admin)}
    assert listed[admin.id]["is_admin"] and not listed[target_id]["is_admin"]

    assert access.set_user_status(admin, admin.id, "disabled") == "self"
    assert access.set_user_status(admin, 10**9, "disabled") == "not_found"
    assert access.set_user_status(admin, target_id, "disabled") == "disabled"
    target = load_user(admin_url, target_id)
    with psycopg.connect(admin_url) as conn:
        oid = conn.execute(
            "SELECT entra_oid FROM users WHERE id = %s;", (target_id,)
        ).fetchone()[0]
    assert users.authenticate({"oid": oid, "tid": PERSONAL, "email": target.email}) is None
    assert access.set_user_status(admin, target_id, "active") == "active"
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        access.set_user_status(target, admin.id, "disabled")


def test_init_db_makes_the_owner_admin(admin_url):
    import subprocess
    import sys

    email = f"{uuid.uuid4().hex[:8]}@outlook.com"
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "ADMIN_DATABASE_URL": admin_url, "OWNER_EMAIL": email.upper()}
    env.pop("POSTGRES_RESOURCE_ID", None)
    for _ in range(2):  # idempotent
        subprocess.run([sys.executable, "-m", "ops.init_db"], cwd=root, env=env, check=True,
                       capture_output=True)
    with psycopg.connect(admin_url) as conn:
        assert conn.execute(
            "SELECT is_admin FROM users WHERE email = %s;", (email,)
        ).fetchall() == [(True,)]
