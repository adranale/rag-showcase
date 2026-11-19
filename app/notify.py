"""Optional e-mail notifications through Azure Communication Services (ACS).

Configured by ACS_EMAIL_CONNECTION_STRING ("endpoint=https://...;accesskey=...") and
ACS_EMAIL_SENDER (e.g. DoNotReply@<id>.azurecomm.net); without them nothing is sent.
Requests are signed with the access key (HMAC-SHA256), see
https://learn.microsoft.com/rest/api/communication/authentication. Failures are logged
without addresses or content and never break the calling request.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import UTC, datetime
from email.utils import format_datetime
from urllib.parse import urlsplit

import httpx

from app import users

logger = logging.getLogger(__name__)

API_VERSION = "2023-03-31"
TIMEOUT = httpx.Timeout(15.0, connect=5.0)


def _config() -> tuple[str, str, str] | None:
    """(endpoint, access key, sender) or None if not configured."""
    parts = {}
    for item in os.getenv("ACS_EMAIL_CONNECTION_STRING", "").split(";"):
        key, _, value = item.partition("=")
        if key.strip():
            parts[key.strip().lower()] = value.strip()
    endpoint, key = parts.get("endpoint", ""), parts.get("accesskey", "")
    sender = os.getenv("ACS_EMAIL_SENDER", "").strip()
    if not (endpoint.startswith("https://") and key and sender):
        return None
    return endpoint.rstrip("/"), key, sender


def signed_headers(url: str, body: bytes, access_key: str, now: datetime) -> dict:
    parts = urlsplit(url)
    path_and_query = parts.path + (f"?{parts.query}" if parts.query else "")
    timestamp = format_datetime(now.astimezone(UTC), usegmt=True)
    content_hash = base64.b64encode(hashlib.sha256(body).digest()).decode()
    string_to_sign = f"POST\n{path_and_query}\n{timestamp};{parts.netloc};{content_hash}"
    signature = base64.b64encode(
        hmac.new(
            base64.b64decode(access_key), string_to_sign.encode("utf-8"), hashlib.sha256
        ).digest()
    ).decode()
    return {
        "x-ms-date": timestamp,
        "x-ms-content-sha256": content_hash,
        "Authorization": (
            "HMAC-SHA256 SignedHeaders=x-ms-date;host;x-ms-content-sha256"
            f"&Signature={signature}"
        ),
        "Content-Type": "application/json",
    }


def send_email(recipients: list[str], subject: str, text: str) -> bool:
    """Send a plain-text e-mail; True if ACS accepted it."""
    config = _config()
    if config is None or not recipients:
        return False
    endpoint, key, sender = config
    url = f"{endpoint}/emails:send?api-version={API_VERSION}"
    body = json.dumps(
        {
            "senderAddress": sender,
            "recipients": {"to": [{"address": address} for address in recipients]},
            "content": {"subject": " ".join(subject.split())[:200], "plainText": text},
        }
    ).encode("utf-8")
    try:
        response = httpx.post(
            url,
            content=body,
            headers=signed_headers(url, body, key, datetime.now(UTC)),
            timeout=TIMEOUT,
        )
    except httpx.RequestError as exc:
        logger.warning("E-mail notification failed: %s", type(exc).__name__)
        return False
    if response.status_code != 202:
        logger.warning("E-mail notification rejected: HTTP %s", response.status_code)
        return False
    return True


def notify_access_request(claims: dict, message: str | None) -> None:
    """Tell the admins about a new access request (runs after the response)."""
    try:
        recipients = users.admin_emails()
    except Exception:
        logger.exception("Could not load the admin addresses")
        return
    ui = os.getenv("UI_BASE_URL", "").rstrip("/")
    name = (claims.get("name") or "").strip() or "Someone"
    lines = [
        (
            f"{name} <{claims.get('email') or 'no e-mail'}> asked for access to the "
            "Document Assistant."
        ),
    ]
    if message:
        lines += ["", "Message:", message.strip()]
    lines += ["", f"Approve or refuse it on the admin page: {ui}/" if ui else ""]
    if send_email(recipients, f"Access request: {name}", "\n".join(lines).strip()):
        logger.info("Access request notification sent to %d admin(s)", len(recipients))
