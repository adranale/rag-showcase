"""Per-user Dropbox connection: OAuth (authorization code + PKCE, read-only scopes),
folder selection and disconnect. Refresh tokens are stored encrypted (app/crypto.py).

Dropbox redirects the browser to `DROPBOX_REDIRECT_URI`, which must be the public UI
(the API is internal); the UI then hands `code` and `state` to the API.
"""

import base64
import hashlib
import os
import secrets
from urllib.parse import urlencode

import httpx

from app import crypto
from app.db import get_connection
from app.users import User
from connectors.base import ConnectorError
from connectors.dropbox_source import TOKEN_URL, DropboxSource

AUTHORIZE_URL = "https://www.dropbox.com/oauth2/authorize"
SCOPES = ("files.metadata.read", "files.content.read")
STATE_MAX_AGE = "10 minutes"
TIMEOUT = 30.0


class OAuthError(Exception):
    """User-presentable problem while connecting Dropbox (never contains secrets)."""


def _app_credentials() -> tuple[str, str]:
    key = os.getenv("DROPBOX_APP_KEY", "")
    secret = os.getenv("DROPBOX_APP_SECRET", "")
    if not key or not secret:
        raise OAuthError("Dropbox is not configured on the server")
    return key, secret


def _redirect_uri() -> str:
    uri = os.getenv("DROPBOX_REDIRECT_URI", "")
    if not uri:
        raise OAuthError("Dropbox is not configured on the server")
    return uri


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def start_authorization(user: User) -> str:
    key, _ = _app_credentials()
    redirect_uri = _redirect_uri()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    with get_connection(user.id) as conn:
        conn.execute(
            f"DELETE FROM oauth_states WHERE created_at < now() - interval '{STATE_MAX_AGE}';"
        )
        conn.execute(
            "INSERT INTO oauth_states (state, user_id, code_verifier) VALUES (%s, %s, %s);",
            (state, user.id, verifier),
        )
    query = urlencode(
        {
            "client_id": key,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge(verifier),
            "code_challenge_method": "S256",
            "token_access_type": "offline",
            "scope": " ".join(SCOPES),
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def complete_authorization(user: User, code: str, state: str) -> None:
    key, secret = _app_credentials()
    with get_connection(user.id) as conn:
        # Single use and bound to the user that started the flow.
        row = conn.execute(
            f"""
            DELETE FROM oauth_states
            WHERE state = %s AND user_id = %s
              AND created_at > now() - interval '{STATE_MAX_AGE}'
            RETURNING code_verifier;
            """,
            (state, user.id),
        ).fetchone()
    if row is None:
        raise OAuthError("The connection request expired or is invalid. Please start again.")

    try:
        response = httpx.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _redirect_uri(),
                "code_verifier": row[0],
            },
            auth=(key, secret),
            timeout=TIMEOUT,
        )
    except httpx.RequestError:
        raise OAuthError("Could not reach Dropbox. Please try again.") from None
    if response.status_code != 200:
        raise OAuthError("Dropbox rejected the authorization. Please start again.")

    body = response.json()
    refresh_token = body.get("refresh_token")
    granted = set((body.get("scope") or "").split())
    if not refresh_token or not set(SCOPES) <= granted:
        raise OAuthError("Dropbox did not grant the required read-only access.")

    with get_connection(user.id) as conn:
        conn.execute(
            """
            INSERT INTO dropbox_credentials (user_id, refresh_token_encrypted, account_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE
                SET refresh_token_encrypted = EXCLUDED.refresh_token_encrypted,
                    account_id = EXCLUDED.account_id,
                    updated_at = now();
            """,
            (user.id, crypto.encrypt(refresh_token), body.get("account_id")),
        )


def status(user: User) -> dict:
    with get_connection(user.id) as conn:
        row = conn.execute(
            "SELECT folder FROM dropbox_credentials WHERE user_id = %s;", (user.id,)
        ).fetchone()
        counts = conn.execute(
            """
            SELECT count(*) FILTER (WHERE ingestion_status = 'indexed'),
                   count(*) FILTER (WHERE ingestion_status IN ('failed', 'unsupported')),
                   max(indexed_at)
            FROM documents WHERE user_id = %s;
            """,
            (user.id,),
        ).fetchone()
        chunk_count = conn.execute(
            "SELECT count(*) FROM chunks WHERE user_id = %s;", (user.id,)
        ).fetchone()[0]
        # Dropbox folders that were indexed, whether or not this account is connected
        # (data imported before sign-in existed has no stored credentials).
        sources = conn.execute(
            """
            SELECT s.account_identifier, s.last_successful_sync,
                   count(d.id) FILTER (WHERE d.ingestion_status = 'indexed'),
                   count(d.id) FILTER (WHERE d.ingestion_status IN ('failed', 'unsupported'))
            FROM source_connections AS s
            LEFT JOIN documents AS d ON d.connection_id = s.id
            WHERE s.user_id = %s AND s.provider = 'dropbox'
            GROUP BY s.id ORDER BY s.id;
            """,
            (user.id,),
        ).fetchall()
    return {
        "connected": row is not None,
        "folder": row[0] if row else None,
        "documents_indexed": counts[0],
        "documents_skipped": counts[1],
        "last_indexed_at": counts[2],
        "chunks": chunk_count,
        "sources": [
            {
                "folder": source[0],
                "last_sync": source[1],
                "documents_indexed": source[2],
                "documents_skipped": source[3],
            }
            for source in sources
        ],
    }


def source_for(refresh_token: str, folder: str) -> DropboxSource:
    key, secret = _app_credentials()
    return DropboxSource(folder, key, secret, refresh_token)


def load_credentials(user_id: int) -> tuple[str, str | None] | None:
    """(refresh token, folder or None if not chosen yet), or None if not connected."""
    with get_connection(user_id) as conn:
        row = conn.execute(
            "SELECT refresh_token_encrypted, folder FROM dropbox_credentials "
            "WHERE user_id = %s;",
            (user_id,),
        ).fetchone()
    return (crypto.decrypt(row[0]), row[1]) if row else None


def set_folder(user: User, folder: str) -> str:
    folder = "/" + folder.strip().strip("/")
    credentials = load_credentials(user.id)
    if credentials is None:
        raise OAuthError("Connect Dropbox first.")
    try:
        exists = source_for(credentials[0], folder).check_folder()
    except (ConnectorError, httpx.RequestError):
        raise OAuthError("Could not check the folder at Dropbox. Please try again.") from None
    if not exists:
        raise OAuthError("This folder does not exist in your Dropbox.")
    with get_connection(user.id) as conn:
        conn.execute(
            "UPDATE dropbox_credentials SET folder = %s, updated_at = now() "
            "WHERE user_id = %s;",
            (folder, user.id),
        )
    return folder


def disconnect(user: User, delete_documents: bool) -> None:
    credentials = load_credentials(user.id)
    if credentials is not None:
        try:
            source_for(credentials[0], "").revoke()
        except (ConnectorError, crypto.CryptoError, httpx.RequestError):
            pass  # The local credentials are removed either way.
    with get_connection(user.id) as conn:
        conn.execute("DELETE FROM dropbox_credentials WHERE user_id = %s;", (user.id,))
        if delete_documents:
            conn.execute("DELETE FROM source_connections WHERE user_id = %s;", (user.id,))
