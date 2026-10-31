import io
import sys
import types
import zipfile

import httpx
import pytest

from connectors import ocr
from connectors.base import classify
from connectors.chunking import PipelineChunker, _page_ranges


class FakeChunker(PipelineChunker):
    def _load(self) -> None:
        pass

    def _windows(self, text: str):
        return [{"text": text, "char_start": 0, "char_end": len(text)}]


def make_docx(paragraphs: list[str], images: dict[str, bytes] | None = None) -> bytes:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(
        f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>' for text in paragraphs
    )
    xml = f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
        for name, content in (images or {}).items():
            archive.writestr(f"word/media/{name}", content)
    return buffer.getvalue()


def blank_pdf(pages: int = 2) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.fixture
def ocr_env(monkeypatch):
    monkeypatch.setenv("DOCINTEL_ENDPOINT", "https://di.example.test")
    monkeypatch.setenv("DOCINTEL_KEY", "k")


@pytest.fixture
def no_ocr_env(monkeypatch):
    monkeypatch.delenv("DOCINTEL_ENDPOINT", raising=False)
    monkeypatch.delenv("DOCINTEL_KEY", raising=False)


def test_classify_new_types():
    assert classify("Report.DOCX")[0] == "docx"
    assert classify("scan.JPG") == ("image", "image/jpeg")
    assert classify("scan.tiff")[0] == "image"
    assert classify("old.doc") is None


def test_page_ranges():
    assert _page_ranges([1]) == "1"
    assert _page_ranges([1, 2, 3, 5, 7, 8]) == "1-3,5,7-8"


def test_docx_text_without_ocr(no_ocr_env):
    data = make_docx(["Hello world", "", "Second paragraph"], {"a.png": b"x" * 9000})

    document = FakeChunker().chunk("docx", data)

    assert [c["text"] for c in document.chunks] == ["Hello world", "Second paragraph"]
    assert document.chunking_version == "docx-v1"


def test_docx_ocr_reads_large_images_only(monkeypatch, ocr_env):
    calls = []

    def fake_read(data, pages=None):
        calls.append(len(data))
        return {1: "text in picture"}

    monkeypatch.setattr(ocr, "read_pages", fake_read)
    data = make_docx(
        ["Body"], {"big.png": b"x" * 9000, "tiny.png": b"x" * 100, "vector.emf": b"x" * 9000}
    )

    document = FakeChunker().chunk("docx", data)

    assert calls == [9000]
    assert document.chunks[-1]["text"] == "text in picture"
    assert document.chunks[-1]["location_metadata"] == {"image": "big.png"}
    assert document.chunking_version.endswith("+ocr-v1")


def test_docx_survives_failing_image_ocr(monkeypatch, ocr_env):
    def fail(data, pages=None):
        raise ocr.OcrError("boom")

    monkeypatch.setattr(ocr, "read_pages", fail)

    document = FakeChunker().chunk("docx", make_docx(["Body"], {"a.png": b"x" * 9000}))

    assert len(document.chunks) == 1


def test_invalid_docx(no_ocr_env):
    with pytest.raises(ValueError):
        FakeChunker().chunk("docx", b"not a zip")


def test_image_requires_ocr(no_ocr_env):
    with pytest.raises(ValueError, match="OCR"):
        FakeChunker().chunk("image", b"\x89PNG")


def test_image_ocr(monkeypatch, ocr_env):
    monkeypatch.setattr(ocr, "read_pages", lambda data, pages=None: {1: "scanned text"})

    document = FakeChunker().chunk("image", b"\x89PNG")

    assert document.chunks[0]["text"] == "scanned text"
    assert document.chunks[0]["location_metadata"]["page_number"] == 1


def test_scanned_pdf_uses_ocr_for_empty_pages(monkeypatch, ocr_env):
    seen = {}

    def fake_read(data, pages=None):
        seen["pages"] = pages
        return {1: "first", 2: "second"}

    monkeypatch.setattr(ocr, "read_pages", fake_read)

    document = FakeChunker().chunk("pdf", blank_pdf(2))

    assert seen["pages"] == "1-2"
    assert [c["text"] for c in document.chunks] == ["first", "second"]
    assert document.chunking_version.endswith("+ocr-v1")


def test_scanned_pdf_without_ocr_fails_clearly(no_ocr_env):
    with pytest.raises(ValueError, match="OCR is not configured"):
        FakeChunker().chunk("pdf", blank_pdf(1))


def operation_responses(monkeypatch, result):
    calls = {"post": [], "get": 0}

    def fake_post(url, **kwargs):
        calls["post"].append((url, kwargs))
        return httpx.Response(
            202,
            headers={"Operation-Location": "https://di.example.test/op/1"},
            request=httpx.Request("POST", url),
        )

    def fake_get(url, **kwargs):
        calls["get"] += 1
        body = {"status": "running"} if calls["get"] == 1 else result
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(ocr.httpx, "post", fake_post)
    monkeypatch.setattr(ocr.httpx, "get", fake_get)
    monkeypatch.setattr(ocr.time, "sleep", lambda seconds: None)
    return calls


def test_read_pages_polls_and_parses(monkeypatch, ocr_env):
    calls = operation_responses(
        monkeypatch,
        {
            "status": "succeeded",
            "analyzeResult": {
                "pages": [
                    {"pageNumber": 1, "lines": [{"content": "a"}, {"content": "b"}]},
                    {"pageNumber": 2, "lines": []},
                ]
            },
        },
    )

    assert ocr.read_pages(b"data", "1-2") == {1: "a\nb"}
    url, kwargs = calls["post"][0]
    assert url.endswith("/documentintelligence/documentModels/prebuilt-read:analyze")
    assert kwargs["params"]["pages"] == "1-2"
    assert kwargs["headers"]["Ocp-Apim-Subscription-Key"] == "k"


def test_read_pages_reports_failure_without_secrets(monkeypatch, ocr_env):
    operation_responses(
        monkeypatch, {"status": "failed", "error": {"code": "InvalidContent"}}
    )

    with pytest.raises(ocr.OcrError, match="InvalidContent"):
        ocr.read_pages(b"data")


def test_read_pages_requires_configuration(no_ocr_env):
    with pytest.raises(ocr.OcrError):
        ocr.read_pages(b"data")


@pytest.fixture(autouse=True)
def stub_sentence_transformers(monkeypatch):
    # chunk_pdf imports it at module level; the tests never load a model.
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=object),
    )
