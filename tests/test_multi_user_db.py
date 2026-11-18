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


def test_dropbox_oauth_flow(admin_url, runtime, monkeypatch):
    import httpx

    from app import crypto, dropbox_oauth

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

    url = dropbox_oauth.start_authorization(owner)
    assert "code_challenge_method=S256" in url and "scope=files.metadata.read" in url
    state = url.split("state=")[1].split("&")[0]

    with pytest.raises(dropbox_oauth.OAuthError):  # state belongs to another user
        dropbox_oauth.complete_authorization(intruder, "code", state)
    dropbox_oauth.complete_authorization(owner, "code", state)
    with pytest.raises(dropbox_oauth.OAuthError):  # single use
        dropbox_oauth.complete_authorization(owner, "code", state)

    with psycopg.connect(admin_url) as conn:
        stored = conn.execute(
            "SELECT refresh_token_encrypted FROM dropbox_credentials WHERE user_id = %s;",
            (owner.id,),
        ).fetchone()[0]
    assert b"REFRESH-SECRET" not in bytes(stored)
    assert dropbox_oauth.load_credentials(owner.id) == ("REFRESH-SECRET", None)
    assert dropbox_oauth.load_credentials(intruder.id) is None

    revoked = []

    class FakeSource:
        def revoke(self):
            revoked.append(True)

    monkeypatch.setattr(dropbox_oauth, "source_for", lambda *args: FakeSource())
    dropbox_oauth.disconnect(owner, delete_documents=False)
    assert revoked and dropbox_oauth.load_credentials(owner.id) is None
