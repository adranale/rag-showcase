"""OCR through Azure AI Document Intelligence (prebuilt-read, REST API).

Configured by DOCINTEL_ENDPOINT and DOCINTEL_KEY. Without them OCR is
unavailable and callers fall back to the text they can extract locally.
The read model detects the language itself (German, English, Arabic, ...).
"""

import os
import time

import httpx

API_VERSION = "2024-11-30"
POLL_INTERVAL_SECONDS = 2.0
POLL_TIMEOUT_SECONDS = 600.0
MAX_RETRIES = 5


class OcrError(RuntimeError):
    """OCR request failed. Messages never contain the key or document text."""


def is_configured() -> bool:
    return bool(os.getenv("DOCINTEL_ENDPOINT", "").strip()) and bool(
        os.getenv("DOCINTEL_KEY", "").strip()
    )


def _headers() -> dict[str, str]:
    return {
        "Ocp-Apim-Subscription-Key": os.environ["DOCINTEL_KEY"].strip(),
        "Content-Type": "application/octet-stream",
    }


def _request(method: str, url: str, **kwargs) -> httpx.Response:
    for attempt in range(MAX_RETRIES):
        try:
            response = getattr(httpx, method)(
                url, headers=_headers(), timeout=httpx.Timeout(120.0, connect=10.0), **kwargs
            )
        except httpx.RequestError as exc:
            raise OcrError(f"Could not contact Document Intelligence: {type(exc).__name__}") from exc
        if response.status_code in (429, 503) and attempt < MAX_RETRIES - 1:
            time.sleep(min(float(response.headers.get("Retry-After", 5) or 5), 30.0))
            continue
        return response
    raise OcrError("Document Intelligence kept rate limiting the request.")


def read_pages(data: bytes, pages: str | None = None) -> dict[int, str]:
    """OCR a PDF or image; returns {page number: text}.

    `pages` limits a PDF/TIFF to pages such as "1,3-5" (1-based).
    """
    if not is_configured():
        raise OcrError("OCR is not configured (DOCINTEL_ENDPOINT / DOCINTEL_KEY).")

    endpoint = os.environ["DOCINTEL_ENDPOINT"].strip().rstrip("/")
    params = {"api-version": API_VERSION}
    if pages:
        params["pages"] = pages
    start = _request(
        "post",
        f"{endpoint}/documentintelligence/documentModels/prebuilt-read:analyze",
        params=params,
        content=data,
    )
    if start.status_code != 202:
        raise OcrError(f"Document Intelligence rejected the file (HTTP {start.status_code}).")
    operation_url = start.headers.get("Operation-Location")
    if not operation_url or not operation_url.startswith(endpoint):
        raise OcrError("Document Intelligence returned no valid operation URL.")

    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while True:
        result = _request("get", operation_url)
        if result.status_code != 200:
            raise OcrError(f"Document Intelligence status check failed (HTTP {result.status_code}).")
        body = result.json()
        status = body.get("status")
        if status == "succeeded":
            break
        if status in ("failed", "canceled"):
            code = (body.get("error") or {}).get("code", "unknown")
            raise OcrError(f"Document Intelligence analysis {status} ({code}).")
        if time.monotonic() > deadline:
            raise OcrError("Document Intelligence did not finish in time.")
        time.sleep(POLL_INTERVAL_SECONDS)

    texts: dict[int, str] = {}
    for page in body.get("analyzeResult", {}).get("pages", []):
        lines = [line.get("content", "") for line in page.get("lines", [])]
        text = "\n".join(line for line in lines if line.strip()).replace("\x00", "").strip()
        if text:
            texts[int(page.get("pageNumber", len(texts) + 1))] = text
    return texts
