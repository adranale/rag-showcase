"""Dropbox as a per-user storage provider: OAuth (authorization code + PKCE,
read-only scopes), folder check and token revocation.

Dropbox redirects the browser to `DROPBOX_REDIRECT_URI`, which must be the public UI
(the API is internal); the UI then hands `code` and `state` to the API.
"""

import os
from urllib.parse import urlencode

import httpx

from app.oauth import TIMEOUT, OAuthError, code_challenge
from connectors.base import ConnectorError
from connectors.dropbox_source import TOKEN_URL, DropboxSource

NAME = "Dropbox"
AUTHORIZE_URL = "https://www.dropbox.com/oauth2/authorize"
SCOPES = ("files.metadata.read", "files.content.read")


def configured() -> bool:
    return all(
        os.getenv(name)
        for name in ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REDIRECT_URI")
    )


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


def authorize_url(state: str, verifier: str) -> str:
    key, _ = _app_credentials()
    query = urlencode(
        {
            "client_id": key,
            "response_type": "code",
            "redirect_uri": _redirect_uri(),
            "state": state,
            "code_challenge": code_challenge(verifier),
            "code_challenge_method": "S256",
            "token_access_type": "offline",
            "scope": " ".join(SCOPES),
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def exchange_code(code: str, verifier: str) -> tuple[str, str | None]:
    """(refresh token, account ID) for an authorization code."""
    key, secret = _app_credentials()
    try:
        response = httpx.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _redirect_uri(),
                "code_verifier": verifier,
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
    return refresh_token, body.get("account_id")


def source_for(refresh_token: str, folder: str) -> DropboxSource:
    key, secret = _app_credentials()
    return DropboxSource(folder, key, secret, refresh_token)


def resolve_folder(refresh_token: str, value: str) -> tuple[str, str]:
    """(stored folder, display name) for the user's input; the folder must exist."""
    folder = "/" + value.strip().strip("/")
    try:
        exists = source_for(refresh_token, folder).check_folder()
    except (ConnectorError, httpx.RequestError):
        raise OAuthError("Could not check the folder at Dropbox. Please try again.") from None
    if not exists:
        raise OAuthError("This folder does not exist in your Dropbox.")
    return folder, folder


def revoke(refresh_token: str) -> None:
    source_for(refresh_token, "").revoke()
