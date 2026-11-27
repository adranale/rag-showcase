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

# Stored per chunk. The sync re-chunks documents whose chunks do not start with the
# current generation (connectors/sync.py), except OCR'd ones (OCR is billed per page).
CHUNKING_GENERATION = "lc-recursive-v1"
PDF_CHUNKING_VERSION = f"{CHUNKING_GENERATION}-pdf"
TEXT_CHUNKING_VERSION = f"{CHUNKING_GENERATION}-text"
OFFICE_CHUNKING_VERSION = f"{CHUNKING_GENERATION}-office"
OCR_CHUNKING_VERSION = f"{CHUNKING_GENERATION}-ocr"
# Versions of documents that are only re-chunked when the file itself changes.
KEEP_CHUNKING_PREFIXES = ("ocr-", OCR_CHUNKING_VERSION)
OVERLAP = 16


def needs_rechunk(stored_version: str | None) -> bool:
    """True for documents chunked by an older generation (not for OCR'd ones)."""
    return (
        stored_version is not None
        and not stored_version.startswith(CHUNKING_GENERATION)
        and not stored_version.startswith(KEEP_CHUNKING_PREFIXES)
    )


def split_paragraphs(text: str) -> list[str]:
    return [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", text)
        if paragraph.strip()
    ]


def split_page(text, tokenizer, max_tokens, overlap, model_limit):
    """Split text into token windows without decoding token fragments.

    Exact fallback for the rare splitter chunk that exceeds the model limit.
    """
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


def _offsets(part: dict) -> dict:
    """Character range of a chunk, omitted when it could not be located."""
    if part.get("char_start") is None:
        return {}
    return {"char_start": part["char_start"], "char_end": part["char_end"]}


class PipelineChunker:
    """Chunks downloaded bytes with LangChain's RecursiveCharacterTextSplitter.

    Lengths are measured with the embedding model's tokenizer, so chunks fit the
    model's input limit; the splitter prefers paragraph, then line, then word breaks.
    The tokenizer is loaded lazily, only when a changed file needs chunking, so
    unchanged re-runs stay cheap. Scanned PDFs and images go through Azure Document
    Intelligence when it is configured (see connectors/ocr.py).
    """

    generation = CHUNKING_GENERATION

    def __init__(self, ocr: DocumentIntelligence | None = None) -> None:
        self._tokenizer = None
        self._splitter = None
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
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        self._splitter = RecursiveCharacterTextSplitter.from_huggingface_tokenizer(
            self._tokenizer, chunk_size=self._budget, chunk_overlap=OVERLAP
        )

    def _token_count(self, text: str) -> int:
        return len(
            self._tokenizer(text, add_special_tokens=True, truncation=False)["input_ids"]
        )

    def _windows(self, text: str):
        """{"text", "char_start", "char_end"} chunks of `text` within the model limit."""
        cursor = 0
        for part in self._splitter.split_text(text):
            # The splitter's own start_index is unreliable with a token length
            # function; chunks are substrings in order, so search forward.
            start = text.find(part, cursor)
            if start < 0:
                start = text.find(part)
            cursor = start + 1 if start >= 0 else cursor
            if self._token_count(part) <= self._model_limit:
                yield {
                    "text": part,
                    "char_start": start if start >= 0 else None,
                    "char_end": start + len(part) if start >= 0 else None,
                }
                continue
            offset = start if start >= 0 else 0
            for window in split_page(
                part, self._tokenizer, self._budget, OVERLAP, self._model_limit
            ):
                yield {
                    **window,
                    "char_start": window["char_start"] + offset,
                    "char_end": window["char_end"] + offset,
                }

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
                            **_offsets(part),
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
                        "location_metadata": {**location, **_offsets(part)},
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
        chunks = [
            {"text": part["text"], "location_metadata": _offsets(part)}
            for part in self._windows(text)
        ]
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
                            **_offsets(part),
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
