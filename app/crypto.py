"""Encryption of stored third-party tokens (Dropbox refresh tokens).

`TOKEN_ENCRYPTION_KEY` holds one or more comma-separated Fernet keys; the first
encrypts, all of them decrypt (key rotation). The key lives outside the database
(Container Apps secret), so a database dump or an admin with database access alone
cannot read the tokens. A Key Vault backed implementation can replace this module
without touching callers.
"""

import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class CryptoError(RuntimeError):
    """The encryption key is missing/invalid or the ciphertext cannot be read."""


def _cipher() -> MultiFernet:
    raw = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
    if not raw:
        raise CryptoError("TOKEN_ENCRYPTION_KEY is not configured")
    try:
        return MultiFernet([Fernet(key.strip()) for key in raw.split(",") if key.strip()])
    except ValueError:
        raise CryptoError("TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from None


def generate_key() -> str:
    return Fernet.generate_key().decode()


def encrypt(plaintext: str) -> bytes:
    return _cipher().encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext: bytes) -> str:
    try:
        return _cipher().decrypt(bytes(ciphertext)).decode("utf-8")
    except InvalidToken:
        raise CryptoError("Stored token cannot be decrypted with the configured key") from None
