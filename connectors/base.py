from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
}
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx", *IMAGE_MIME_TYPES}
MAX_FILE_BYTES = 25 * 1024 * 1024

# Native Google Docs have no file extension; they are exported as plain text.
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"


class ConnectorError(RuntimeError):
    """Raised for configuration or remote API problems (never contains secrets)."""


@dataclass(frozen=True)
class RemoteFile:
    file_id: str
    name: str
    # Changes whenever the remote content changes (Dropbox rev / Drive md5 or version).
    version: str
    kind: str  # "text", "pdf", "docx" or "image"
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


def classify(name: str) -> tuple[str, str] | None:
    """Return (kind, mime_type) for a supported file name, else None."""
    lowered = name.lower()
    if lowered.endswith(".pdf"):
        return "pdf", "application/pdf"
    if lowered.endswith(".md"):
        return "text", "text/markdown"
    if lowered.endswith(".txt"):
        return "text", "text/plain"
    if lowered.endswith(".docx"):
        return "docx", DOCX_MIME
    for extension, mime_type in IMAGE_MIME_TYPES.items():
        if lowered.endswith(extension):
            return "image", mime_type
    return None
