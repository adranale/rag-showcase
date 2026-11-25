"""Google Drive as a per-user storage provider: OAuth (authorization code + PKCE,
scope drive.readonly plus openid/email to show the connected account), folder
check and token revocation.

Google redirects the browser to `GOOGLE_REDIRECT_URI`, the public UI root (the API
is internal); the UI then hands `code` and `state` to the API. drive.readonly is a
restricted scope: an unverified app shows Google's warning screen and is limited
to 100 users (see docs/multi-user.md).
"""

import base64
import json
import os
from urllib.parse import urlencode

import httpx

from app.oauth import TIMEOUT, OAuthError, code_challenge
from connectors.base import ConnectorError
from connectors.gdrive_source import (
    OAUTH_TOKEN_URL,
    SCOPES as DRIVE_SCOPES,
    GoogleDriveSource,
    parse_folder_id,
)

NAME = "Google Drive"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SCOPES = ("openid", "email", *DRIVE_SCOPES)


def configured() -> bool:
    return all(
        os.getenv(name)
        for name in (
            "GOOGLE_OAUTH_CLIENT_ID",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "GOOGLE_REDIRECT_URI",
        )
    )


def _client_credentials() -> tuple[str, str]:
    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
    secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not client_id or not secret:
        raise OAuthError("Google Drive is not configured on the server")
    return client_id, secret


def _redirect_uri() -> str:
    uri = os.getenv("GOOGLE_REDIRECT_URI", "")
    if not uri:
        raise OAuthError("Google Drive is not configured on the server")
    return uri


def authorize_url(state: str, verifier: str) -> str:
    client_id, _ = _client_credentials()
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": _redirect_uri(),
            "scope": " ".join(SCOPES),
            "state": state,
            "code_challenge": code_challenge(verifier),
            "code_challenge_method": "S256",
            # A refresh token is only returned for offline access, and on a repeated
            # consent only when the consent screen is shown again.
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _email_from_id_token(id_token: str | None) -> str | None:
    # Received directly from Google's token endpoint over TLS, so the payload can be
    # read without verifying the signature (Google's OpenID Connect guidance).
    if not id_token or id_token.count(".") != 2:
        return None
    payload = id_token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return None
    email = claims.get("email")
    return email if isinstance(email, str) else None


def exchange_code(code: str, verifier: str) -> tuple[str, str | None]:
    """(refresh token, Google account e-mail) for an authorization code."""
    client_id, secret = _client_credentials()
    try:
        response = httpx.post(
            OAUTH_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": secret,
                "redirect_uri": _redirect_uri(),
                "code_verifier": verifier,
            },
            timeout=TIMEOUT,
        )
    except httpx.RequestError:
        raise OAuthError("Could not reach Google. Please try again.") from None
    if response.status_code != 200:
        raise OAuthError("Google rejected the authorization. Please start again.")

    body = response.json()
    refresh_token = body.get("refresh_token")
    granted = set((body.get("scope") or "").split())
    # Users can untick single permissions on Google's consent screen.
    if not set(DRIVE_SCOPES) <= granted:
        raise OAuthError(
            "Google did not grant read access to your Drive. Please connect again "
            "and allow viewing your Google Drive files."
        )
    if not refresh_token:
        raise OAuthError("Google did not return offline access. Please connect again.")
    return refresh_token, _email_from_id_token(body.get("id_token"))


def source_for(refresh_token: str, folder: str) -> GoogleDriveSource:
    client_id, secret = _client_credentials()
    return GoogleDriveSource.for_user(folder, client_id, secret, refresh_token)


def resolve_folder(refresh_token: str, value: str) -> tuple[str, str]:
    """(folder ID, folder name) for a pasted folder link or ID."""
    folder_id = parse_folder_id(value)
    if folder_id is None:
        raise OAuthError("Paste the link of a Google Drive folder (or its ID).")
    try:
        name = source_for(refresh_token, folder_id).check_folder()
    except (ConnectorError, httpx.RequestError):
        raise OAuthError(
            "Could not check the folder at Google Drive. Please try again."
        ) from None
    if name is None:
        raise OAuthError("This folder was not found in your Google Drive.")
    return folder_id, name


def revoke(refresh_token: str) -> None:
    httpx.post(REVOKE_URL, data={"token": refresh_token}, timeout=TIMEOUT)
