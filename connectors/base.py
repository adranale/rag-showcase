import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

OFFICE_TYPES = {
    ".docx": (
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    ".xlsx": (
        "xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    ".pptx": (
        "pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
}
# Formats accepted by the Azure Document Intelligence Read model.
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
    ".heif": "image/heif",
}
SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", *OFFICE_TYPES, *IMAGE_TYPES}
MAX_FILE_BYTES = 25 * 1024 * 1024

# Native Google Docs have no file extension; they are exported as plain text.
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"


class ConnectorError(RuntimeError):
    """Raised for configuration or remote API problems (never contains secrets)."""


class DocumentError(ValueError):
    """A file cannot be processed; the message is fixed text, never file content."""


def failure_detail(exc: Exception) -> str:
    """Message that is safe to log; empty for exceptions that might carry content."""
    return str(exc) if isinstance(exc, ConnectorError | DocumentError) else ""


@dataclass(frozen=True)
class RemoteFile:
    file_id: str
    name: str
    # Changes whenever the remote content changes (Dropbox rev / Drive md5 or version).
    version: str
    kind: str  # "text", "pdf", "docx", "xlsx", "pptx" or "image"
    mime_type: str
    size: int | None = None
    url: str | None = None
    export_mime: str | None = None  # set for native Google Docs


class SourceConnector(Protocol):
    provider: str

    @property
    def account_identifier(self) -> str:
        """Stable identifier of the synced location, e.g. the folder."""

    def list_files(self) -> Iterator[RemoteFile]:
        """Yield every supported file below the configured folder."""

    def download(self, remote: RemoteFile) -> bytes:
        """Return the raw (or exported) file content."""


def ocr_configured() -> bool:
    return bool(os.getenv("DOCINTEL_ENDPOINT") and os.getenv("DOCINTEL_KEY"))


def classify(name: str) -> tuple[str, str] | None:
    """Return (kind, mime_type) for a supported file name, else None.

    Images are only listed when OCR (Azure Document Intelligence) is configured;
    otherwise they would fail on every run.
    """
    lowered = name.lower()
    if lowered.endswith(".pdf"):
        return "pdf", "application/pdf"
    if lowered.endswith(".md"):
        return "text", "text/markdown"
    if lowered.endswith(".txt"):
        return "text", "text/plain"
    extension = os.path.splitext(lowered)[1]
    if extension in OFFICE_TYPES:
        return OFFICE_TYPES[extension]
    if extension in IMAGE_TYPES and ocr_configured():
        return "image", IMAGE_TYPES[extension]
    return None
