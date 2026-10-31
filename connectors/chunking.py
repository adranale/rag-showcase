import hashlib
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from typing import Protocol
from xml.etree import ElementTree

from connectors import ocr
from ingest_text import split_paragraphs

logger = logging.getLogger(__name__)

TEXT_CHUNKING_VERSION = "paragraph-token-window-v1"
OVERLAP = 16

OCR_SUFFIX = "+ocr-v1"
MAX_OCR_PDF_PAGES = 300
MAX_DOCX_IMAGES = 30
MIN_DOCX_IMAGE_BYTES = 5 * 1024
MAX_DOCX_PART_BYTES = 50 * 1024 * 1024
DOCX_OCR_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff")
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass(frozen=True)
class ChunkedDocument:
    content_hash: str
    chunking_version: str
    chunks: list[dict]  # {"text": str, "location_metadata": dict}


class Chunker(Protocol):
    def chunk(self, kind: str, data: bytes) -> ChunkedDocument: ...


def _clean(text: str) -> str:
    # PostgreSQL text columns reject NUL characters.
    return text.replace("\x00", "").strip()


def _page_ranges(pages: list[int]) -> str:
    ranges = []
    start = previous = pages[0]
    for page in pages[1:] + [None]:
        if page is not None and page == previous + 1:
            previous = page
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        if page is not None:
            start = previous = page
    return ",".join(ranges)


def _page_has_images(page) -> bool:
    try:
        xobjects = page["/Resources"]["/XObject"].get_object()
        return any(
            xobjects[name].get_object().get("/Subtype") == "/Image" for name in xobjects
        )
    except (KeyError, TypeError, AttributeError):
        return False


def _ocr_enabled_version(version: str) -> str:
    return version + OCR_SUFFIX if ocr.is_configured() else version


class PipelineChunker:
    """Chunks downloaded bytes with the existing project chunking code.

    The embedding model's tokenizer is loaded lazily, only when a changed file
    needs chunking, so unchanged re-runs stay cheap. When Document Intelligence
    is configured, scanned PDF pages, PDF pages with images, images inside Word
    files and standalone image files are read with OCR.
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
        if kind == "docx":
            return self._chunk_docx(data)
        if kind == "image":
            return self._chunk_image(data)
        return self._chunk_text(data)

    def _decode(self, data: bytes) -> str:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
        return text.replace("\x00", "")

    def _paragraph_chunks(self, text: str) -> list[dict]:
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
        return chunks

    def _page_chunks(self, text: str, page_number: int) -> list[dict]:
        return [
            {
                "text": part["text"],
                "location_metadata": {
                    "page_number": page_number,
                    "char_start": part["char_start"],
                    "char_end": part["char_end"],
                },
            }
            for part in self._windows(text)
        ]

    def _chunk_text(self, data: bytes) -> ChunkedDocument:
        text = self._decode(data).strip()
        if not text:
            raise ValueError("Document is empty.")
        self._load()
        return ChunkedDocument(
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
            TEXT_CHUNKING_VERSION,
            self._paragraph_chunks(text),
        )

    def _chunk_pdf(self, data: bytes) -> ChunkedDocument:
        from pypdf import PdfReader

        from chunk_pdf import CHUNKING_VERSION

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("Password-protected PDFs are not supported.")
        self._load()

        use_ocr = ocr.is_configured()
        local: dict[int, str] = {}
        ocr_pages: list[int] = []
        for page_number, page in enumerate(reader.pages, start=1):
            text = _clean(page.extract_text() or "")
            local[page_number] = text
            if use_ocr and (not text or _page_has_images(page)):
                ocr_pages.append(page_number)

        if len(ocr_pages) > MAX_OCR_PDF_PAGES:
            raise ValueError(
                f"PDF needs OCR on {len(ocr_pages)} pages (limit {MAX_OCR_PDF_PAGES})."
            )
        ocr_text: dict[int, str] = {}
        if ocr_pages:
            ocr_text = ocr.read_pages(data, _page_ranges(ocr_pages))

        chunks = []
        for page_number in sorted(local):
            text = ocr_text.get(page_number) or local[page_number]
            if text:
                chunks.extend(self._page_chunks(text, page_number))
        if not chunks:
            raise ValueError(
                "No text found."
                if use_ocr
                else "No extractable text found and OCR is not configured."
            )
        return ChunkedDocument(
            hashlib.sha256(data).hexdigest(),
            _ocr_enabled_version(CHUNKING_VERSION),
            chunks,
        )

    def _chunk_image(self, data: bytes) -> ChunkedDocument:
        if not ocr.is_configured():
            raise ValueError("Images need OCR, which is not configured.")
        pages = ocr.read_pages(data)
        if not pages:
            raise ValueError("No text found in the image.")
        self._load()
        chunks = []
        for page_number, text in sorted(pages.items()):
            chunks.extend(self._page_chunks(text, page_number))
        return ChunkedDocument(
            hashlib.sha256(data).hexdigest(), "image-ocr-v1", chunks
        )

    def _docx_paragraphs(self, archive: zipfile.ZipFile) -> list[str]:
        info = archive.getinfo("word/document.xml")
        if info.file_size > MAX_DOCX_PART_BYTES:
            raise ValueError("Word document is too large.")
        root = ElementTree.fromstring(archive.read(info))
        paragraphs = []
        for paragraph in root.iter(f"{WORD_NS}p"):
            parts = []
            for node in paragraph.iter():
                if node.tag == f"{WORD_NS}t":
                    parts.append(node.text or "")
                elif node.tag in (f"{WORD_NS}tab", f"{WORD_NS}br"):
                    parts.append(" ")
            text = re.sub(r"[ \t]+", " ", _clean("".join(parts)))
            if text:
                paragraphs.append(text)
        return paragraphs

    def _docx_image_text(self, archive: zipfile.ZipFile) -> list[tuple[str, str]]:
        if not ocr.is_configured():
            return []
        names = [
            info.filename
            for info in archive.infolist()
            if info.filename.startswith("word/media/")
            and info.filename.lower().endswith(DOCX_OCR_EXTENSIONS)
            and MIN_DOCX_IMAGE_BYTES <= info.file_size <= MAX_DOCX_PART_BYTES
        ][:MAX_DOCX_IMAGES]
        results = []
        for name in names:
            try:
                pages = ocr.read_pages(archive.read(name))
            except ocr.OcrError as exc:
                # One unreadable picture must not fail the whole document.
                logger.warning("OCR skipped for an image in a Word file: %s", exc)
                continue
            text = "\n".join(pages[number] for number in sorted(pages))
            if text:
                results.append((name.rsplit("/", 1)[-1], text))
        return results

    def _chunk_docx(self, data: bytes) -> ChunkedDocument:
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
            paragraphs = self._docx_paragraphs(archive)
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
            raise ValueError("Not a readable .docx file.") from exc
        image_texts = self._docx_image_text(archive)
        if not paragraphs and not image_texts:
            raise ValueError("Word document has no text.")
        self._load()
        chunks = self._paragraph_chunks("\n\n".join(paragraphs)) if paragraphs else []
        for name, text in image_texts:
            for part in self._windows(text):
                chunks.append(
                    {
                        "text": part["text"],
                        "location_metadata": {"image": name},
                    }
                )
        return ChunkedDocument(
            hashlib.sha256(data).hexdigest(),
            _ocr_enabled_version("docx-v1"),
            chunks,
        )
