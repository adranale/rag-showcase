"""OCR through Azure Document Intelligence (prebuilt-read model, REST API).

Enabled only when DOCINTEL_ENDPOINT and DOCINTEL_KEY are set. Each OCR page is
billed by Azure, so a per-run page budget (DOCINTEL_MAX_PAGES, default 1000,
0 = unlimited) stops a run once it is used up; the remaining files fail and are
retried on the next run. Only status codes are put into errors, never content.
"""

import os
import time

import httpx

from connectors.base import ConnectorError

API_VERSION = "2024-11-30"
DEFAULT_PAGE_BUDGET = 1000
DEFAULT_MIN_IMAGE_PIXELS = 100
POLL_SECONDS = 2.0
TIMEOUT_SECONDS = 600.0
RETRY_STATUS = {429, 503}
MAX_RETRIES = 5


class OcrBudgetError(ValueError):
    """The per-run OCR page budget is used up."""


def min_image_pixels() -> int:
    """Smallest side (px) an image needs to be OCR'd; 0 disables the check."""
    try:
        return max(
            0, int(os.getenv("DOCINTEL_MIN_IMAGE_PIXELS", DEFAULT_MIN_IMAGE_PIXELS))
        )
    except ValueError:
        return DEFAULT_MIN_IMAGE_PIXELS


class DocumentIntelligence:
    def __init__(
        self,
        endpoint: str,
        key: str,
        *,
        page_budget: int | None = DEFAULT_PAGE_BUDGET,
        client: httpx.Client | None = None,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._key = key
        self._budget = page_budget or None  # 0 / None = unlimited
        self._client = client or httpx.Client(timeout=60.0)
        self._poll_seconds = poll_seconds
        self.pages_used = 0

    @classmethod
    def from_env(cls, client: httpx.Client | None = None):
        endpoint = os.getenv("DOCINTEL_ENDPOINT", "").strip()
        key = os.getenv("DOCINTEL_KEY", "").strip()
        if not endpoint and not key:
            return None
        if not endpoint or not key:
            raise ConnectorError(
                "Set both DOCINTEL_ENDPOINT and DOCINTEL_KEY, or neither."
            )
        if not endpoint.startswith("https://"):
            raise ConnectorError("DOCINTEL_ENDPOINT must be an https:// URL.")
        try:
            budget = int(os.getenv("DOCINTEL_MAX_PAGES", DEFAULT_PAGE_BUDGET))
        except ValueError as exc:
            raise ConnectorError("DOCINTEL_MAX_PAGES must be an integer.") from exc
        return cls(endpoint, key, page_budget=budget, client=client)

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        headers = {"Ocp-Apim-Subscription-Key": self._key, **kwargs.pop("headers", {})}
        for attempt in range(MAX_RETRIES + 1):
            response = self._client.request(method, url, headers=headers, **kwargs)
            if response.status_code not in RETRY_STATUS or attempt == MAX_RETRIES:
                return response
            try:
                delay = float(response.headers.get("Retry-After", ""))
            except ValueError:
                delay = 5.0
            time.sleep(min(delay, 60.0))
        return response  # pragma: no cover

    def _reserve(self, pages: int) -> None:
        if self._budget is not None and self.pages_used + pages > self._budget:
            raise OcrBudgetError(
                f"OCR page budget of {self._budget} pages per run is used up."
            )
        self.pages_used += pages

    def read(self, data: bytes, pages: int = 1) -> list[tuple[int, str]]:
        """OCR a PDF or image; return (page_number, text) for pages with text."""
        self._reserve(pages)
        url = (
            f"{self._endpoint}/documentintelligence/documentModels/"
            f"prebuilt-read:analyze?api-version={API_VERSION}"
        )
        response = self._request(
            "POST",
            url,
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        if response.status_code != 202:
            raise ConnectorError(
                f"Document Intelligence rejected the file (HTTP {response.status_code})"
            )
        operation = response.headers.get("Operation-Location", "")
        if not operation.startswith(self._endpoint):
            raise ConnectorError("Document Intelligence returned no operation URL.")

        deadline = time.monotonic() + TIMEOUT_SECONDS
        while True:
            result = self._request("GET", operation)
            if result.status_code != 200:
                raise ConnectorError(
                    f"Document Intelligence polling failed (HTTP {result.status_code})"
                )
            body = result.json()
            status = body.get("status")
            if status == "succeeded":
                break
            if status in ("failed", "canceled"):
                raise ConnectorError(f"Document Intelligence analysis {status}.")
            if time.monotonic() > deadline:
                raise ConnectorError("Document Intelligence analysis timed out.")
            time.sleep(self._poll_seconds)

        extracted = []
        for page in body.get("analyzeResult", {}).get("pages", []):
            text = "\n".join(line.get("content", "") for line in page.get("lines", []))
            text = text.replace("\x00", "").strip()
            if text:
                extracted.append((int(page.get("pageNumber", 0)), text))
        return extracted
