import hashlib
import io
from dataclasses import dataclass
from typing import Protocol

from connectors.extract import EXTRACTORS
from connectors.ocr import DocumentIntelligence
from ingest_text import split_paragraphs

TEXT_CHUNKING_VERSION = "paragraph-token-window-v1"
OFFICE_CHUNKING_VERSION = "office-token-window-v1"
OCR_CHUNKING_VERSION = "ocr-page-token-window-v1"
OVERLAP = 16


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

        from chunk_pdf import MODEL_NAME

        model = SentenceTransformer(MODEL_NAME, device="cpu")
        if not model.tokenizer.is_fast:
            raise RuntimeError("A fast tokenizer is required.")
        self._tokenizer = model.tokenizer
        self._model_limit = model.max_seq_length
        self._budget = self._model_limit - self._tokenizer.num_special_tokens_to_add(
            pair=False
        )

    def _windows(self, text: str):
        from chunk_pdf import split_page

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
        sections = EXTRACTORS[kind](data)
        if not sections:
            raise ValueError("Document is empty.")
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
            raise ValueError("OCR found no text.")
        return ChunkedDocument(
            hashlib.sha256(data).hexdigest(), OCR_CHUNKING_VERSION, chunks
        )

    def _chunk_image(self, data: bytes) -> ChunkedDocument:
        if self._ocr is None:
            raise ValueError(
                "OCR is not configured (DOCINTEL_ENDPOINT / DOCINTEL_KEY)."
            )
        try:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as image:
                frames = getattr(image, "n_frames", 1)
        except Exception:  # noqa: BLE001 - e.g. HEIF, which Pillow cannot open
            frames = 1
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
            raise ValueError("Document is empty.")
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

        from chunk_pdf import CHUNKING_VERSION

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs are not supported.")
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
                raise ValueError(
                    "No extractable text found. The PDF may require OCR "
                    "(set DOCINTEL_ENDPOINT / DOCINTEL_KEY)."
                )
            return self._ocr_document(data, len(reader.pages))
        return ChunkedDocument(
            hashlib.sha256(data).hexdigest(), CHUNKING_VERSION, chunks
        )
