"""Per-user cloud storage: every user connects one provider (Dropbox or Google
Drive) and chooses one folder. Handles the OAuth state, the encrypted refresh token,
the folder choice and the documents of earlier choices.

Switching the provider or the folder deletes the documents of the previous choice,
but only after the caller confirmed it (`ConfirmationRequired` otherwise), so only
one folder is ever searchable. Local sample data (provider 'local') is not touched.
"""

import secrets
from types import ModuleType
from typing import NamedTuple

import httpx

from app import crypto, dropbox_oauth, gdrive_oauth
from app.db import get_connection
from app.oauth import OAuthError
from app.users import User
from connectors.base import ConnectorError, SourceConnector

PROVIDERS: dict[str, ModuleType] = {
    "dropbox": dropbox_oauth,
    "google_drive": gdrive_oauth,
}
CLOUD_PROVIDERS = tuple(PROVIDERS)
STATE_MAX_AGE = "10 minutes"


class ConfirmationRequired(Exception):
    """The change would delete indexed documents and was not confirmed."""

    def __init__(self, documents: int) -> None:
        super().__init__(f"{documents} indexed documents would be deleted")
        self.documents = documents


class Credentials(NamedTuple):
    provider: str
    refresh_token: str
    folder: str | None


def _module(provider: str) -> ModuleType:
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise OAuthError("Unknown storage provider.") from None


def provider_name(provider: str) -> str:
    return _module(provider).NAME


def available() -> list[str]:
    """Providers configured on this server, in display order."""
    return [name for name, module in PROVIDERS.items() if module.configured()]


def _documents_outside(conn, user_id: int, provider: str, folder: str | None) -> int:
    """Indexed documents of cloud sources other than (provider, folder).

    With `folder` None every source of another provider counts.
    """
    if folder is None:
        condition, params = "s.provider <> %s", (provider,)
    else:
        condition = "NOT (s.provider = %s AND s.account_identifier = %s)"
        params = (provider, folder)
    return conn.execute(
        f"""
        SELECT count(*) FROM documents AS d
        JOIN source_connections AS s ON s.id = d.connection_id
        WHERE s.user_id = %s AND s.provider = ANY(%s)
          AND d.ingestion_status <> 'removed' AND {condition};
        """,
        (user_id, list(CLOUD_PROVIDERS), *params),
    ).fetchone()[0]


def _delete_outside(conn, user_id: int, provider: str, folder: str | None) -> None:
    if folder is None:
        condition, params = "provider <> %s", (provider,)
    else:
        condition = "NOT (provider = %s AND account_identifier = %s)"
        params = (provider, folder)
    conn.execute(
        f"DELETE FROM source_connections WHERE user_id = %s AND provider = ANY(%s) "
        f"AND {condition};",
        (user_id, list(CLOUD_PROVIDERS), *params),
    )


def start_authorization(user: User, provider: str, confirm_delete: bool = False) -> str:
    """Authorization URL of `provider`; switching providers needs `confirm_delete`."""
    module = _module(provider)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    url = module.authorize_url(state, verifier)
    with get_connection(user.id) as conn:
        affected = _documents_outside(conn, user.id, provider, None)
        if affected and not confirm_delete:
            raise ConfirmationRequired(affected)
        conn.execute(
            f"DELETE FROM oauth_states WHERE created_at < now() - interval '{STATE_MAX_AGE}';"
        )
        conn.execute(
            "INSERT INTO oauth_states (state, user_id, code_verifier, provider) "
            "VALUES (%s, %s, %s, %s);",
            (state, user.id, verifier, provider),
        )
    return url


def complete_authorization(user: User, code: str, state: str) -> str:
    """Store the provider's refresh token; returns the provider of the state."""
    with get_connection(user.id) as conn:
        # Single use and bound to the user that started the flow.
        row = conn.execute(
            f"""
            DELETE FROM oauth_states
            WHERE state = %s AND user_id = %s
              AND created_at > now() - interval '{STATE_MAX_AGE}'
            RETURNING code_verifier, provider;
            """,
            (state, user.id),
        ).fetchone()
    if row is None:
        raise OAuthError("The connection request expired or is invalid. Please start again.")
    verifier, provider = row
    refresh_token, account_id = _module(provider).exchange_code(code, verifier)

    previous = load_credentials(user.id)
    with get_connection(user.id) as conn:
        # Reconnecting the same provider keeps the folder; another provider starts over
        # (the switch was confirmed when the authorization was started).
        conn.execute(
            """
            INSERT INTO storage_credentials (user_id, provider, refresh_token_encrypted,
                                             account_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                provider = EXCLUDED.provider,
                refresh_token_encrypted = EXCLUDED.refresh_token_encrypted,
                account_id = EXCLUDED.account_id,
                folder = CASE WHEN storage_credentials.provider = EXCLUDED.provider
                              THEN storage_credentials.folder END,
                folder_name = CASE WHEN storage_credentials.provider = EXCLUDED.provider
                                   THEN storage_credentials.folder_name END,
                updated_at = now();
            """,
            (user.id, provider, crypto.encrypt(refresh_token), account_id),
        )
        _delete_outside(conn, user.id, provider, None)
    if previous is not None and previous.provider != provider:
        _revoke(previous)
    return provider


def load_credentials(user_id: int) -> Credentials | None:
    with get_connection(user_id) as conn:
        row = conn.execute(
            "SELECT provider, refresh_token_encrypted, folder FROM storage_credentials "
            "WHERE user_id = %s;",
            (user_id,),
        ).fetchone()
    return Credentials(row[0], crypto.decrypt(row[1]), row[2]) if row else None


def source_for(credentials: Credentials) -> SourceConnector:
    if credentials.folder is None:
        raise OAuthError("Choose a folder first.")
    return _module(credentials.provider).source_for(
        credentials.refresh_token, credentials.folder
    )


def set_folder(user: User, value: str, confirm_delete: bool = False) -> dict:
    """Validate and store the folder; documents of other folders need `confirm_delete`."""
    credentials = load_credentials(user.id)
    if credentials is None:
        raise OAuthError("Connect Dropbox or Google Drive first.")
    module = _module(credentials.provider)
    folder, name = module.resolve_folder(credentials.refresh_token, value)
    with get_connection(user.id) as conn:
        affected = _documents_outside(conn, user.id, credentials.provider, folder)
        if affected and not confirm_delete:
            raise ConfirmationRequired(affected)
        _delete_outside(conn, user.id, credentials.provider, folder)
        conn.execute(
            "UPDATE storage_credentials SET folder = %s, folder_name = %s, "
            "updated_at = now() WHERE user_id = %s;",
            (folder, name, user.id),
        )
    return {"folder": folder, "folder_name": name, "documents_deleted": affected}


def _revoke(credentials: Credentials) -> None:
    try:
        _module(credentials.provider).revoke(credentials.refresh_token)
    except (ConnectorError, OAuthError, httpx.RequestError):
        pass  # The local credentials are removed either way.


def disconnect(user: User, delete_documents: bool) -> None:
    credentials = load_credentials(user.id)
    if credentials is not None:
        _revoke(credentials)
    with get_connection(user.id) as conn:
        conn.execute("DELETE FROM storage_credentials WHERE user_id = %s;", (user.id,))
        if delete_documents:
            conn.execute("DELETE FROM source_connections WHERE user_id = %s;", (user.id,))


def status(user: User) -> dict:
    with get_connection(user.id) as conn:
        row = conn.execute(
            "SELECT provider, account_id, folder, folder_name FROM storage_credentials "
            "WHERE user_id = %s;",
            (user.id,),
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
        # Indexed folders, whether or not a provider is connected (data imported
        # before sign-in existed has no stored credentials).
        sources = conn.execute(
            """
            SELECT s.provider, s.account_identifier, s.last_successful_sync,
                   count(d.id) FILTER (WHERE d.ingestion_status = 'indexed'),
                   count(d.id) FILTER (WHERE d.ingestion_status IN ('failed', 'unsupported'))
            FROM source_connections AS s
            LEFT JOIN documents AS d ON d.connection_id = s.id
            WHERE s.user_id = %s AND s.provider = ANY(%s)
            GROUP BY s.id ORDER BY s.id;
            """,
            (user.id, list(CLOUD_PROVIDERS)),
        ).fetchall()
    provider, account, folder, folder_name = row if row else (None, None, None, None)
    return {
        "available": available(),
        "provider": provider,
        "connected": row is not None,
        "account": account if provider == "google_drive" else None,
        "folder": folder,
        "folder_name": folder_name,
        "documents_indexed": counts[0],
        "documents_skipped": counts[1],
        "last_indexed_at": counts[2],
        "chunks": chunk_count,
        "sources": [
            {
                "provider": source[0],
                "folder": folder_name
                if (source[0], source[1]) == (provider, folder) and folder_name
                else source[1],
                "last_sync": source[2],
                "documents_indexed": source[3],
                "documents_skipped": source[4],
            }
            for source in sources
        ],
    }
