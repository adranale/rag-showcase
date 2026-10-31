import hashlib
import io
from dataclasses import dataclass
from typing import Protocol

from ingest_text import split_paragraphs

TEXT_CHUNKING_VERSION = "paragraph-token-window-v1"
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
    needs chunking, so unchanged re-runs stay cheap.
    """

    def __init__(self) -> None:
        self._tokenizer = None
        self._model_limit = 0
        self._budget = 0

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
        return self._chunk_text(data)

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
            raise ValueError("No extractable text found. The PDF may require OCR.")
        return ChunkedDocument(
            hashlib.sha256(data).hexdigest(), CHUNKING_VERSION, chunks
        )
