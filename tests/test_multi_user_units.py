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
        auth.current_identity(x_api_key=None, authorization=None)
    assert missing.value.status_code == 401

    monkeypatch.setenv("API_KEY", "k")
    token = auth.issue_user_token({"oid": "o", "tid": "t"})
    with pytest.raises(HTTPException) as wrong_key:
        auth.current_identity(x_api_key="nope", authorization=f"Bearer {token}")
    assert wrong_key.value.status_code == 401

    claims = auth.current_identity(x_api_key="k", authorization=f"Bearer {token}")
    monkeypatch.setattr(users, "authenticate", lambda claims: None)
    with pytest.raises(HTTPException) as not_enabled:
        auth.current_user(claims)
    assert not_enabled.value.status_code == 403

    member = users.User(id=1, email="a@b.c", status="active", chats_per_day=1,
                        files_per_sync=1)
    with pytest.raises(HTTPException) as not_admin:
        auth.current_admin(member)
    assert not_admin.value.status_code == 403


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
        ("get", "/access-request"),
        ("get", "/admin/users"),
        ("post", "/admin/invite"),
    ]:
        assert getattr(client, method)(path).status_code == 401, path


def test_access_request_notifies_admins_only_for_new_requests(monkeypatch):
    from app import access, notify

    claims = {"oid": "o", "tid": "t", "email": "new@outlook.com", "name": "New"}
    app.dependency_overrides[auth.current_identity] = lambda: claims
    sent = []
    monkeypatch.setattr(notify, "notify_access_request", lambda c, m: sent.append((c, m)))
    try:
        client = TestClient(app)
        for result in ("created", "pending"):
            monkeypatch.setattr(access, "submit_request", lambda c, code, m, r=result: r)
            response = client.post(
                "/access-request", json={"invite_code": "c" * 20, "message": "hi"}
            )
            assert response.json() == {"result": result}
    finally:
        app.dependency_overrides.clear()
    assert sent == [(claims, "hi")]


def test_admin_endpoints_require_admin_and_map_results(client, monkeypatch):
    from app import access

    assert client.get("/admin/requests").status_code == 403

    admin = users.User(id=2, email="boss@x.org", status="active", chats_per_day=1,
                       files_per_sync=1, is_admin=True)
    app.dependency_overrides[auth.current_user] = lambda: admin
    expected = {"not_pending": 409, "email_in_use": 409, "approved": 200}
    for result, code in expected.items():
        monkeypatch.setattr(access, "decide", lambda a, i, ok, r=result: r)
        assert client.post("/admin/requests/5/approve").status_code == code
    assert client.post("/admin/requests/5/delete").status_code == 422

    monkeypatch.setattr(access, "set_user_status", lambda a, i, s: "self")
    response = client.put("/admin/users/2/status", json={"status": "disabled"})
    assert response.status_code == 409
    assert client.put("/admin/users/3/status", json={"status": "admin"}).status_code == 422


def test_email_is_signed_and_skipped_without_configuration(monkeypatch):
    import base64
    import hashlib
    import hmac
    from urllib.parse import urlsplit

    import httpx

    from app import notify

    monkeypatch.delenv("ACS_EMAIL_CONNECTION_STRING", raising=False)
    assert notify.send_email(["a@b.c"], "s", "t") is False

    key = base64.b64encode(b"k" * 32).decode()
    monkeypatch.setenv(
        "ACS_EMAIL_CONNECTION_STRING", f"endpoint=https://acs.example/;accesskey={key}"
    )
    monkeypatch.setenv("ACS_EMAIL_SENDER", "DoNotReply@x.azurecomm.net")
    captured = {}

    def fake_post(url, content, headers, timeout):
        captured.update(url=url, body=content, headers=headers)
        return httpx.Response(202)

    monkeypatch.setattr(httpx, "post", fake_post)
    assert notify.send_email(["a@b.c"], "Line\nbreak", "text") is True

    headers, body = captured["headers"], captured["body"]
    assert captured["url"] == "https://acs.example/emails:send?api-version=2023-03-31"
    assert b'"subject": "Line break"' in body
    content_hash = base64.b64encode(hashlib.sha256(body).digest()).decode()
    assert headers["x-ms-content-sha256"] == content_hash
    parts = urlsplit(captured["url"])
    string_to_sign = (
        f"POST\n{parts.path}?{parts.query}\n{headers['x-ms-date']};{parts.netloc};"
        f"{content_hash}"
    )
    signature = base64.b64encode(
        hmac.new(base64.b64decode(key), string_to_sign.encode(), hashlib.sha256).digest()
    ).decode()
    assert headers["Authorization"] == (
        "HMAC-SHA256 SignedHeaders=x-ms-date;host;x-ms-content-sha256"
        f"&Signature={signature}"
    )

    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(401))
    assert notify.send_email(["a@b.c"], "s", "t") is False
