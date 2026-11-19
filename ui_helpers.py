"""Streamlit-free helpers of chat_ui.py (importable and unit-testable)."""

import time
from collections.abc import Callable

import httpx

STARTUP_RETRIES = 6
STARTUP_WAIT_SECONDS = 10


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
