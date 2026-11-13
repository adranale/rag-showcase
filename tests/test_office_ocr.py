import io

import httpx
import pytest

from connectors import base
from connectors.base import ConnectorError
from connectors.chunking import PipelineChunker
from connectors.extract import docx_sections, pptx_sections, xlsx_sections
from connectors.ocr import DocumentIntelligence, OcrBudgetError

ENDPOINT = "https://docintel.example.cognitiveservices.azure.com"


@pytest.fixture(autouse=True)
def _no_ocr_env(monkeypatch):
    monkeypatch.delenv("DOCINTEL_ENDPOINT", raising=False)
    monkeypatch.delenv("DOCINTEL_KEY", raising=False)


def make_docx() -> bytes:
    from docx import Document

    doc = Document()
    doc.add_paragraph("Intro paragraph")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Name"
    table.rows[0].cells[1].text = "Toters"
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def make_xlsx() -> bytes:
    import xlsxwriter

    buffer = io.BytesIO()
    workbook = xlsxwriter.Workbook(buffer, {"in_memory": True})
    sheet = workbook.add_worksheet("Budget")
    sheet.write_row(0, 0, ["Item", "Cost"])
    sheet.write_row(1, 0, ["Servers", 42])
    workbook.add_worksheet("Empty")
    workbook.close()
    return buffer.getvalue()


def make_pptx() -> bytes:
    from pptx import Presentation

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "Roadmap"
    slide.placeholders[1].text = "Launch in Q3"
    slide.notes_slide.notes_text_frame.text = "Speaker note"
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def test_docx_keeps_paragraphs_and_tables():
    ((_, text),) = docx_sections(make_docx())
    assert text.splitlines() == ["Intro paragraph", "Name | Toters"]


def test_xlsx_one_section_per_non_empty_sheet():
    sections = xlsx_sections(make_xlsx())
    assert sections == [({"sheet": "Budget"}, "Item | Cost\nServers | 42")]


def test_pptx_includes_slide_text_and_notes():
    ((location, text),) = pptx_sections(make_pptx())
    assert location == {"slide": 1}
    assert "Roadmap" in text and "Launch in Q3" in text and "Speaker note" in text


def test_classify_images_only_with_ocr(monkeypatch):
    monkeypatch.delenv("DOCINTEL_ENDPOINT", raising=False)
    monkeypatch.delenv("DOCINTEL_KEY", raising=False)
    assert base.classify("scan.png") is None
    assert base.classify("a.docx") is not None
    monkeypatch.setenv("DOCINTEL_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("DOCINTEL_KEY", "k")
    assert base.classify("scan.PNG") is not None


def ocr_client(handler, **kwargs) -> DocumentIntelligence:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return DocumentIntelligence(
        ENDPOINT, "secret-key", client=client, poll_seconds=0, **kwargs
    )


def analyze_handler(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Ocp-Apim-Subscription-Key"] == "secret-key"
        if request.method == "POST":
            return httpx.Response(
                202, headers={"Operation-Location": f"{ENDPOINT}/op/1"}
            )
        if len([c for c in calls if c.method == "GET"]) == 1:
            return httpx.Response(200, json={"status": "running"})
        return httpx.Response(
            200,
            json={
                "status": "succeeded",
                "analyzeResult": {
                    "pages": [
                        {
                            "pageNumber": 1,
                            "lines": [{"content": "Hello"}, {"content": "World"}],
                        },
                        {"pageNumber": 2, "lines": []},
                    ]
                },
            },
        )

    return handler


def test_ocr_polls_until_done_and_skips_empty_pages():
    calls: list = []
    ocr = ocr_client(analyze_handler(calls))
    assert ocr.read(b"%PDF", pages=2) == [(1, "Hello\nWorld")]
    assert ocr.pages_used == 2


def test_ocr_budget_is_enforced_before_any_request():
    calls: list = []
    ocr = ocr_client(analyze_handler(calls), page_budget=3)
    ocr.read(b"x", pages=2)
    calls.clear()
    with pytest.raises(OcrBudgetError):
        ocr.read(b"x", pages=2)
    assert calls == []


def test_ocr_errors_do_not_leak_key_or_body():
    ocr = ocr_client(lambda request: httpx.Response(401, text="secret-key leaked"))
    with pytest.raises(ConnectorError) as info:
        ocr.read(b"x")
    assert "401" in str(info.value)
    assert "secret-key" not in str(info.value)


def test_ocr_from_env_requires_both_values(monkeypatch):
    monkeypatch.setenv("DOCINTEL_ENDPOINT", ENDPOINT)
    monkeypatch.delenv("DOCINTEL_KEY", raising=False)
    with pytest.raises(ConnectorError):
        DocumentIntelligence.from_env()
    monkeypatch.delenv("DOCINTEL_ENDPOINT")
    assert DocumentIntelligence.from_env() is None


class FakeOcr:
    def __init__(self, pages):
        self.pages = pages
        self.requested: list[int] = []

    def read(self, data, pages=1):
        self.requested.append(pages)
        return self.pages


class StubChunker(PipelineChunker):
    """Skips loading the embedding model; each text becomes one window."""

    def _load(self):
        pass

    def _windows(self, text):
        yield {"text": text, "char_start": 0, "char_end": len(text)}


def blank_pdf() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_office_chunks_carry_location_and_stable_hash():
    chunker = StubChunker(ocr=None)
    first = chunker.chunk("xlsx", make_xlsx())
    again = chunker.chunk("xlsx", make_xlsx())
    assert first.chunks[0]["location_metadata"]["sheet"] == "Budget"
    assert first.content_hash == again.content_hash
    assert "xlsx" in first.chunking_version


def test_scanned_pdf_uses_ocr_when_configured():
    ocr = FakeOcr([(1, "scanned text")])
    result = StubChunker(ocr=ocr).chunk("pdf", blank_pdf())
    assert ocr.requested == [1]
    assert result.chunks[0]["text"] == "scanned text"
    assert result.chunks[0]["location_metadata"]["page_number"] == 1
    assert "ocr" in result.chunking_version


def test_scanned_pdf_without_ocr_fails_clearly():
    with pytest.raises(ValueError, match="OCR"):
        StubChunker(ocr=None).chunk("pdf", blank_pdf())


def test_image_without_ocr_fails_and_with_empty_ocr_fails():
    with pytest.raises(ValueError, match="not configured"):
        StubChunker(ocr=None).chunk("image", b"x")
    with pytest.raises(ValueError, match="no text"):
        StubChunker(ocr=FakeOcr([])).chunk("image", b"x")
