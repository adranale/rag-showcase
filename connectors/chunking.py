import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from typing import Protocol

from app.config import MODEL_NAME
from connectors.base import DocumentError, PermanentDocumentError
from connectors.extract import EXTRACTORS
from connectors.ocr import DocumentIntelligence, min_image_pixels

# Stored per chunk; changing a version string re-chunks the affected documents.
PDF_CHUNKING_VERSION = "page-token-window-v1"
TEXT_CHUNKING_VERSION = "paragraph-token-window-v1"
OFFICE_CHUNKING_VERSION = "office-token-window-v1"
OCR_CHUNKING_VERSION = "ocr-page-token-window-v1"
OVERLAP = 16


def split_paragraphs(text: str) -> list[str]:
    return [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", text)
        if paragraph.strip()
    ]


def split_page(text, tokenizer, max_tokens, overlap, model_limit):
    """Split text into token windows without decoding token fragments."""
    encoded = tokenizer(
        text,
        add_special_tokens=False,
        truncation=False,
        return_offsets_mapping=True,
    )
    offsets = encoded["offset_mapping"]

    start = 0

    while start < len(offsets):
        end = min(start + max_tokens, len(offsets))

        # Retokenize the actual substring: its token count can differ
        # slightly from its count within the complete page.
        while end > start:
            char_start = offsets[start][0]
            char_end = offsets[end - 1][1]
            chunk_text = text[char_start:char_end]

            token_count = len(
                tokenizer(
                    chunk_text,
                    add_special_tokens=True,
                    truncation=False,
                )["input_ids"]
            )

            if token_count <= model_limit:
                break

            end -= 1
        else:
            raise ValueError("Unable to create a chunk within the token limit.")

        if chunk_text.strip():
            yield {
                "text": chunk_text,
                "token_count": token_count,
                "char_start": char_start,
                "char_end": char_end,
            }

        if end == len(offsets):
            break

        start = max(start + 1, end - overlap)


@dataclass(frozen=True)
class ChunkedDocument:
    content_hash: str
    chunking_version: str
    chunks: list[dict]  # {"text": str, "location_metadata": dict}


class Chunker(Protocol):
    def chunk(self, kind: str, data: bytes) -> ChunkedDocument: ...


class PipelineChunker:
    """Chunks downloaded bytes with the existing project chunking code.

    The embedding model's tokenizer is loaded lazily, only when a changed file
    needs chunking, so unchanged re-runs stay cheap. Scanned PDFs and images go
    through Azure Document Intelligence when it is configured (see connectors/ocr.py).
    """

    def __init__(self, ocr: DocumentIntelligence | None = None) -> None:
        self._tokenizer = None
        self._model_limit = 0
        self._budget = 0
        self._ocr = ocr if ocr is not None else DocumentIntelligence.from_env()

    def _load(self) -> None:
        if self._tokenizer is not None:
            return
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(MODEL_NAME, device="cpu")
        if not model.tokenizer.is_fast:
            raise RuntimeError("A fast tokenizer is required.")
        self._tokenizer = model.tokenizer
        self._model_limit = model.max_seq_length
        self._budget = self._model_limit - self._tokenizer.num_special_tokens_to_add(
            pair=False
        )

    def _windows(self, text: str):
        return split_page(
            text, self._tokenizer, self._budget, OVERLAP, self._model_limit
        )

    def chunk(self, kind: str, data: bytes) -> ChunkedDocument:
        if kind == "pdf":
            return self._chunk_pdf(data)
        if kind in EXTRACTORS:
            return self._chunk_office(kind, data)
        if kind == "image":
            return self._chunk_image(data)
        return self._chunk_text(data)

    def _page_chunks(self, pages: list[tuple[int, str]]) -> list[dict]:
        self._load()
        chunks = []
        for page_number, text in pages:
            for part in self._windows(text):
                chunks.append(
                    {
                        "text": part["text"],
                        "location_metadata": {
                            "page_number": page_number,
                            "char_start": part["char_start"],
                            "char_end": part["char_end"],
                        },
                    }
                )
        return chunks

    def _chunk_office(self, kind: str, data: bytes) -> ChunkedDocument:
        try:
            sections = EXTRACTORS[kind](data)
        except zipfile.BadZipFile as exc:
            raise PermanentDocumentError(
                "File is not a valid Office document."
            ) from exc
        if not sections:
            raise PermanentDocumentError("Document is empty.")
        self._load()
        chunks = []
        for location, text in sections:
            for part in self._windows(text):
                chunks.append(
                    {
                        "text": part["text"],
                        "location_metadata": {
                            **location,
                            "char_start": part["char_start"],
                            "char_end": part["char_end"],
                        },
                    }
                )
        digest = hashlib.sha256(
            "\n\n".join(text for _, text in sections).encode("utf-8")
        ).hexdigest()
        return ChunkedDocument(digest, f"{OFFICE_CHUNKING_VERSION}-{kind}", chunks)

    def _ocr_document(self, data: bytes, pages: int) -> ChunkedDocument:
        chunks = self._page_chunks(self._ocr.read(data, pages=pages))
        if not chunks:
            raise PermanentDocumentError("OCR found no text.")
        return ChunkedDocument(
            hashlib.sha256(data).hexdigest(), OCR_CHUNKING_VERSION, chunks
        )

    def _chunk_image(self, data: bytes) -> ChunkedDocument:
        if self._ocr is None:
            raise DocumentError(
                "OCR is not configured (DOCINTEL_ENDPOINT / DOCINTEL_KEY)."
            )
        try:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as image:
                frames = getattr(image, "n_frames", 1)
                image_size = image.size
        except Exception:  # noqa: BLE001 - e.g. HEIF, which Pillow cannot open
            frames, image_size = 1, None
        # Icons and logos have no useful text and small images are rejected by the service.
        if image_size is not None and min(image_size) < min_image_pixels():
            raise PermanentDocumentError(
                f"Image is too small for OCR ({image_size[0]}x{image_size[1]} px)."
            )
        return self._ocr_document(data, frames)

    def _decode(self, data: bytes) -> str:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
        # PostgreSQL text columns reject NUL characters.
        return text.replace("\x00", "")

    def _chunk_text(self, data: bytes) -> ChunkedDocument:
        text = self._decode(data).strip()
        if not text:
            raise PermanentDocumentError("Document is empty.")
        self._load()
        chunks = []
        for number, paragraph in enumerate(split_paragraphs(text), start=1):
            # Long paragraphs are split so no chunk exceeds the model limit.
            for part in self._windows(paragraph):
                chunks.append(
                    {
                        "text": part["text"],
                        "location_metadata": {"paragraph": number},
                    }
                )
        return ChunkedDocument(
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
            TEXT_CHUNKING_VERSION,
            chunks,
        )

    def _chunk_pdf(self, data: bytes) -> ChunkedDocument:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise PermanentDocumentError("Encrypted PDFs are not supported.")
        self._load()
        chunks = []
        for page_number, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").replace("\x00", "").strip()
            if not text:
                continue
            for part in self._windows(text):
                chunks.append(
                    {
                        "text": part["text"],
                        "location_metadata": {
                            "page_number": page_number,
                            "char_start": part["char_start"],
                            "char_end": part["char_end"],
                        },
                    }
                )
        if not chunks:
            if self._ocr is None:
                raise DocumentError(
                    "No extractable text found. The PDF may require OCR "
                    "(set DOCINTEL_ENDPOINT / DOCINTEL_KEY)."
                )
            return self._ocr_document(data, len(reader.pages))
        return ChunkedDocument(
            hashlib.sha256(data).hexdigest(), PDF_CHUNKING_VERSION, chunks
        )
