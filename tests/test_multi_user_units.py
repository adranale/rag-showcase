import tomllib

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import auth, crypto, quota, users
from app.main import app
from connectors.base import ConnectorError, RemoteFile
from connectors.limits import LimitedConnector
from ui_start import build_secrets

SECRET = "s" * 40


@pytest.fixture(autouse=True)
def secrets_env(monkeypatch):
    monkeypatch.setenv("USER_TOKEN_SECRET", SECRET)
    monkeypatch.delenv("API_KEY", raising=False)


def test_user_token_round_trip_and_tampering():
    token = auth.issue_user_token({"oid": "o", "tid": "t", "email": "a@b.c", "x": "ignored"})
    claims = auth.verify_user_token(token)
    assert claims == {"oid": "o", "tid": "t", "email": "a@b.c", "name": ""}
    with pytest.raises(auth.AuthError):
        auth.verify_user_token(token[:-2] + "xx")


def test_user_token_expires_and_needs_identity(monkeypatch):
    token = auth.issue_user_token({"oid": "o", "tid": "t"})
    monkeypatch.setattr(auth, "TOKEN_MAX_AGE_SECONDS", -1)
    with pytest.raises(auth.AuthError):
        auth.verify_user_token(token)
    monkeypatch.setattr(auth, "TOKEN_MAX_AGE_SECONDS", 300)
    with pytest.raises(auth.AuthError):
        auth.verify_user_token(auth.issue_user_token({"oid": "", "tid": "t"}))


def test_short_secret_is_rejected(monkeypatch):
    monkeypatch.setenv("USER_TOKEN_SECRET", "short")
    with pytest.raises(auth.AuthError):
        auth.issue_user_token({"oid": "o", "tid": "t"})


def test_current_user_requires_bearer_and_api_key(monkeypatch):
    with pytest.raises(HTTPException) as missing:
        auth.current_user(x_api_key=None, authorization=None)
    assert missing.value.status_code == 401

    monkeypatch.setenv("API_KEY", "k")
    token = auth.issue_user_token({"oid": "o", "tid": "t"})
    with pytest.raises(HTTPException) as wrong_key:
        auth.current_user(x_api_key="nope", authorization=f"Bearer {token}")
    assert wrong_key.value.status_code == 401

    monkeypatch.setattr(users, "authenticate", lambda claims: None)
    with pytest.raises(HTTPException) as not_enabled:
        auth.current_user(x_api_key="k", authorization=f"Bearer {token}")
    assert not_enabled.value.status_code == 403


def test_crypto_round_trip_and_wrong_key(monkeypatch):
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", crypto.generate_key())
    blob = crypto.encrypt("refresh-token")
    assert b"refresh-token" not in blob
    assert crypto.decrypt(blob) == "refresh-token"

    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", crypto.generate_key())
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(blob)


def test_crypto_key_rotation(monkeypatch):
    old, new = crypto.generate_key(), crypto.generate_key()
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", old)
    blob = crypto.encrypt("v")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", f"{new},{old}")
    assert crypto.decrypt(blob) == "v"


def test_crypto_requires_key(monkeypatch):
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    with pytest.raises(crypto.CryptoError):
        crypto.encrypt("x")


class FakeConnector:
    provider = "dropbox"
    account_identifier = "/f"

    def __init__(self, count):
        self.files = [RemoteFile(f"id:{i}", f"{i}.txt", "r", "text", None) for i in range(count)]

    def list_files(self):
        return iter(self.files)

    def download(self, remote):
        return b"x"


def test_limited_connector_never_truncates():
    assert len(list(LimitedConnector(FakeConnector(3), 3).list_files())) == 3
    with pytest.raises(ConnectorError):
        list(LimitedConnector(FakeConnector(4), 3).list_files())


def test_ui_secrets_are_valid_toml_and_default_to_consumers():
    env = {
        "AUTH_REDIRECT_URI": "https://ui/oauth2callback",
        "AUTH_COOKIE_SECRET": 'quo"te',
        "MS_CLIENT_ID": "cid",
        "MS_CLIENT_SECRET": "sec",
    }
    parsed = tomllib.loads(build_secrets(env))
    assert parsed["auth"]["cookie_secret"] == 'quo"te'
    assert "/consumers/v2.0/" in parsed["auth"]["microsoft"]["server_metadata_url"]
    with pytest.raises(SystemExit):
        build_secrets({})


def test_pkce_challenge_matches_rfc7636_example():
    from app.dropbox_oauth import code_challenge

    assert (
        code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


@pytest.fixture
def client():
    user = users.User(id=1, email="a@b.c", status="active", chats_per_day=5, files_per_sync=10)
    app.dependency_overrides[auth.current_user] = lambda: user
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_me_endpoint(client, monkeypatch):
    from app import dropbox_oauth, jobs

    monkeypatch.setattr(dropbox_oauth, "status", lambda user: {"connected": False})
    monkeypatch.setattr(jobs, "latest", lambda user_id: None)
    response = client.get("/me")
    assert response.status_code == 200
    assert response.json()["email"] == "a@b.c"


def test_document_chunks_endpoint(client, monkeypatch):
    from app import documents

    found = {"document_id": 7, "title": "t", "source_url": None, "chunks": []}
    monkeypatch.setattr(documents, "document_chunks", lambda user, doc_id: found)
    assert client.get("/documents/7/chunks").json() == found
    monkeypatch.setattr(documents, "document_chunks", lambda user, doc_id: None)
    assert client.get("/documents/8/chunks").status_code == 404


def test_chat_quota_maps_to_429(client, monkeypatch):
    def deny(user):
        raise quota.QuotaExceeded("limit")

    monkeypatch.setattr(quota, "consume_chat", deny)
    monkeypatch.setattr("app.main.consume_chat", deny, raising=False)
    response = client.post("/chat", json={"question": "hi"})
    assert response.status_code == 429


def test_endpoints_reject_anonymous_calls():
    client = TestClient(app)
    for method, path in [
        ("get", "/me"),
        ("post", "/sync"),
        ("delete", "/me/documents"),
        ("get", "/documents/1/chunks"),
        ("post", "/dropbox/authorize"),
    ]:
        assert getattr(client, method)(path).status_code == 401, path
