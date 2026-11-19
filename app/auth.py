"""User identity between the Streamlit UI and the API.

The UI authenticates the user with Microsoft Entra ID (st.login) and sends a
short-lived signed token (claims: oid, tid, email, name) together with the shared
API key. The API trusts only tokens signed with `USER_TOKEN_SECRET`; the user must
also exist in the `users` allowlist (see app/users.py).
"""

import logging
import os
import secrets

import psycopg
from fastapi import Depends, Header, HTTPException
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app import users, wake
from app.users import User

logger = logging.getLogger(__name__)

TOKEN_MAX_AGE_SECONDS = 300
MIN_SECRET_LENGTH = 32
_SALT = "rag-user-token"
_CLAIMS = ("oid", "tid", "email", "name")


class AuthError(Exception):
    pass


def _serializer() -> URLSafeTimedSerializer:
    secret = os.getenv("USER_TOKEN_SECRET", "")
    if len(secret) < MIN_SECRET_LENGTH:
        raise AuthError(
            f"USER_TOKEN_SECRET must be set and at least {MIN_SECRET_LENGTH} characters long"
        )
    return URLSafeTimedSerializer(secret, salt=_SALT)


def issue_user_token(claims: dict) -> str:
    return _serializer().dumps({key: str(claims.get(key) or "") for key in _CLAIMS})


def verify_user_token(token: str) -> dict:
    try:
        claims = _serializer().loads(token, max_age=TOKEN_MAX_AGE_SECONDS)
    except BadSignature:
        raise AuthError("Invalid or expired user token") from None
    if not isinstance(claims, dict) or not claims.get("oid") or not claims.get("tid"):
        raise AuthError("Invalid user token")
    return claims


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    # The shared key is enabled only when API_KEY is configured.
    expected = os.getenv("API_KEY")

    if expected and not (x_api_key and secrets.compare_digest(x_api_key, expected)):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def current_identity(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict:
    """Verified sign-in claims, also for people who are not users (yet)."""
    require_api_key(x_api_key)

    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Missing user token")
    try:
        return verify_user_token(token.strip())
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from None


def current_user(claims: dict = Depends(current_identity)) -> User:
    try:
        user = users.authenticate(claims)
    except (psycopg.Error, RuntimeError) as exc:
        logger.exception("User lookup failed")
        raise HTTPException(
            status_code=503, detail=wake.describe(exc, "Service temporarily unavailable")
        ) from None
    if user is None:
        raise HTTPException(
            status_code=403,
            detail="This account is not enabled. Ask the owner for an invitation.",
        )
    return user


def current_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin rights required.")
    return user
