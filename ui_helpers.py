"""Streamlit-free helpers of chat_ui.py (importable and unit-testable)."""

import re
import time
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

import httpx

STARTUP_RETRIES = 6
STARTUP_WAIT_SECONDS = 10

INVITE_COOKIE = "rag_invite"
_INVITE_CODE = re.compile(r"[A-Za-z0-9_-]{16,128}")

SUBMIT_MESSAGES = {
    "created": ("success", "Request sent. You can use the assistant once it is approved."),
    "pending": ("info", "Your request is already waiting for approval."),
    "already_user": ("info", "You already have an account. Reload the page."),
    "invalid_code": ("error", "This invite link is not valid (any more). Ask for a new one."),
    "refused_recently": ("warning", "Your last request was declined. Try again in a week."),
    "too_many": ("warning", "Too many open requests right now. Please try again later."),
}


def extract_invite_code(value: str | None) -> str | None:
    """The invite code from a code or a pasted invite link; None if it is not valid."""
    value = (value or "").strip()
    if "invite=" in value:
        value = (parse_qs(urlsplit(value).query).get("invite") or [""])[0]
    return value if _INVITE_CODE.fullmatch(value) else None


def invite_cookie_script(code: str | None) -> str:
    """JavaScript that keeps the invite code across the Microsoft sign-in redirect.

    Streamlit returns to "/" after login, dropping the ?invite= parameter; the app
    reads the cookie back with st.context.cookies. Only validated codes are embedded;
    None clears the cookie.
    """
    if code is not None and not _INVITE_CODE.fullmatch(code):
        raise ValueError("invalid invite code")
    value, age = (code, 3600) if code else ("", 0)
    return (
        '<div style="display:none"></div><script>'
        f'document.cookie = "{INVITE_COOKIE}={value}; path=/; max-age={age}; '
        'secure; samesite=lax";</script>'
    )


def invite_link(redirect_uri: str, code: str) -> str:
    """UI start page (from the login redirect URI .../oauth2callback) with the code."""
    parts = urlsplit(redirect_uri)
    base = f"{parts.scheme}://{parts.netloc}/" if parts.netloc else "/"
    return f"{base}?invite={code}"


def request_with_retry(
    send: Callable[[], httpx.Response],
    *,
    method: str,
    notify: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
    retries: int = STARTUP_RETRIES,
    wait: float = STARTUP_WAIT_SECONDS,
) -> httpx.Response:
    """Wait for a scaled-to-zero API / stopped database to start.

    Retries only when the request cannot have been processed (connection failure or
    503); GET requests are also retried after a timeout.
    """
    for attempt in range(1, retries + 1):
        last = attempt == retries
        try:
            response = send()
            if response.status_code != 503 or last:
                return response
        except httpx.RequestError as exc:
            retryable = method == "GET" or isinstance(
                exc, (httpx.ConnectError, httpx.ConnectTimeout)
            )
            if not retryable or last:
                raise
        notify(f"The service is starting up, retrying... (attempt {attempt}/{retries})")
        sleep(wait)
    raise AssertionError("unreachable")


def describe_location(location: dict) -> str:
    parts = []
    if location.get("page_number"):
        parts.append(f"page {location['page_number']}")
    if "char_start" in location and "char_end" in location:
        parts.append(f"characters {location['char_start']}-{location['char_end']}")
    return ", ".join(parts) or "location unknown"


def group_citations(citations: list[dict]) -> list[dict]:
    """One entry per document, with all its cited passages in answer order."""
    groups: dict[int, dict] = {}
    for citation in citations:
        group = groups.setdefault(
            citation["document_id"],
            {
                "document_id": citation["document_id"],
                "title": citation["title"],
                "source_url": citation.get("source_url"),
                "passages": [],
            },
        )
        group["passages"].append(citation)
    return list(groups.values())
