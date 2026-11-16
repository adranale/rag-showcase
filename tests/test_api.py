import os

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.auth import issue_user_token
from app.main import app

TEST_TID = "00000000-0000-0000-0000-00000000aaaa"
TEST_OID = "00000000-0000-0000-0000-00000000bbbb"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def auth_headers():
    """Active test user who owns the sample data (needs the admin DATABASE_URL)."""
    os.environ.setdefault("USER_TOKEN_SECRET", "t" * 40)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        user_id = conn.execute(
            """
            INSERT INTO users (email, status, entra_tid, entra_oid)
            VALUES ('api-test@example.org', 'active', %s, %s)
            ON CONFLICT (lower(email)) DO UPDATE SET status = 'active',
                entra_tid = EXCLUDED.entra_tid, entra_oid = EXCLUDED.entra_oid
            RETURNING id;
            """,
            (TEST_TID, TEST_OID),
        ).fetchone()[0]
        for table in ("source_connections", "documents", "chunks"):
            conn.execute(
                f"UPDATE {table} SET user_id = %s WHERE user_id IS NULL;", (user_id,)
            )
    token = issue_user_token({"oid": TEST_OID, "tid": TEST_TID, "email": "x"})
    return {"Authorization": f"Bearer {token}"}


def test_health(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_database_ready(client):
    response = client.get("/ready")

    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "ready"
    assert data["database"] == "connected"
    assert data["vector_extension"]


def test_search_returns_relevant_passage(client, auth_headers):
    response = client.post(
        "/search",
        headers=auth_headers,
        json={
            "question": "Can volunteers claim travel costs?",
            "top_k": 3,
        },
    )

    assert response.status_code == 200

    data = response.json()
    assert data["count"] == len(data["results"])
    assert 1 <= data["count"] <= 3

    assert any(
        "travel expenses" in result["text"].lower()
        for result in data["results"]
    )


def test_search_result_structure(client, auth_headers):
    response = client.post(
        "/search",
        headers=auth_headers,
        json={
            "question": "When should volunteers register?",
            "top_k": 2,
        },
    )

    assert response.status_code == 200

    results = response.json()["results"]
    assert results

    expected_fields = {
        "document_id",
        "title",
        "source_url",
        "chunk_id",
        "chunk_number",
        "text",
        "location",
        "similarity",
    }

    for result in results:
        assert expected_fields.issubset(result)
        assert isinstance(result["text"], str)
        assert isinstance(result["similarity"], (int, float))


def test_api_key_is_enforced_when_configured(client, auth_headers, monkeypatch):
    monkeypatch.setenv("API_KEY", "secret")
    payload = {"question": "Travel expenses?", "top_k": 1}

    assert client.post("/search", json=payload, headers=auth_headers).status_code == 401
    assert client.post("/chat", json=payload, headers=auth_headers).status_code == 401
    assert (
        client.post(
            "/search", json=payload, headers={**auth_headers, "X-API-Key": "wrong"}
        ).status_code
        == 401
    )
    assert client.get("/health").status_code == 200


def test_search_requires_user_token(client):
    payload = {"question": "Travel expenses?", "top_k": 1}
    assert client.post("/search", json=payload).status_code == 401
    assert (
        client.post(
            "/search", json=payload, headers={"Authorization": "Bearer junk"}
        ).status_code
        == 401
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"question": "", "top_k": 3},
        {"question": "   ", "top_k": 3},
        {"question": "Travel expenses?", "top_k": 0},
        {"question": "Travel expenses?", "top_k": 21},
        {"top_k": 3},
    ],
)
def test_invalid_search_request(client, auth_headers, payload):
    response = client.post("/search", json=payload, headers=auth_headers)

    assert response.status_code == 422