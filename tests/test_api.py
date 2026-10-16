import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


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


def test_search_returns_relevant_passage(client):
    response = client.post(
        "/search",
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


def test_search_result_structure(client):
    response = client.post(
        "/search",
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


def test_api_key_is_enforced_when_configured(client, monkeypatch):
    monkeypatch.setenv("API_KEY", "secret")
    payload = {"question": "Travel expenses?", "top_k": 1}

    assert client.post("/search", json=payload).status_code == 401
    assert client.post("/chat", json=payload).status_code == 401
    assert (
        client.post(
            "/search", json=payload, headers={"X-API-Key": "wrong"}
        ).status_code
        == 401
    )
    assert client.get("/health").status_code == 200


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
def test_invalid_search_request(client, payload):
    response = client.post("/search", json=payload)

    assert response.status_code == 422