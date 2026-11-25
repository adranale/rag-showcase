"""Pieces shared by the storage providers' OAuth flows."""

import base64
import hashlib

TIMEOUT = 30.0


class OAuthError(Exception):
    """User-presentable problem while connecting storage (never contains secrets)."""


def code_challenge(verifier: str) -> str:
    """PKCE S256 challenge of a code verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
