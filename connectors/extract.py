"""Text extraction for Office files. Pure functions: bytes in, sections out.

A section is (location_metadata, text); the chunker splits each section into
token windows. Legacy binary formats (.doc, .xls, .ppt) are not supported.
"""

import io

Section = tuple[dict, str]

MAX_SHEET_ROWS = 5000  # per sheet; keeps huge exports from flooding the index


def _join_unique(values: list[str]) -> str:
    # Merged table cells repeat their text in every spanned cell.
    unique = list(dict.fromkeys(value for value in values if value))
    return " | ".join(unique)


def docx_sections(data: bytes) -> list[Section]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(io.BytesIO(data))
    lines: list[str] = []
    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            text = Paragraph(child, document).text.strip()
            if text:
                lines.append(text)
        elif child.tag.endswith("}tbl"):
            for row in Table(child, document).rows:
                line = _join_unique([cell.text.strip() for cell in row.cells])
                if line:
                    lines.append(line)
    return [({}, "\n".join(lines))] if lines else []


def xlsx_sections(data: bytes) -> list[Section]:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        sections = []
        for sheet in workbook.worksheets:
            lines = []
            for row in sheet.iter_rows(values_only=True, max_row=MAX_SHEET_ROWS):
                line = _join_unique(
                    [str(value).strip() for value in row if value is not None]
                )
                if line:
                    lines.append(line)
            if lines:
                sections.append(({"sheet": sheet.title}, "\n".join(lines)))
        return sections
    finally:
        workbook.close()


def _shape_texts(shapes) -> list[str]:
    texts = []
    for shape in shapes:
        if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
            texts.append(shape.text_frame.text.strip())
        if getattr(shape, "has_table", False) and shape.has_table:
            for row in shape.table.rows:
                texts.append(_join_unique([cell.text.strip() for cell in row.cells]))
        if hasattr(shape, "shapes"):  # grouped shapes
            texts.extend(_shape_texts(shape.shapes))
    return [text for text in texts if text]


def pptx_sections(data: bytes) -> list[Section]:
    from pptx import Presentation

    presentation = Presentation(io.BytesIO(data))
    sections = []
    for number, slide in enumerate(presentation.slides, start=1):
        texts = _shape_texts(slide.shapes)
        if slide.has_notes_slide:
            texts.extend(_shape_texts(slide.notes_slide.shapes))
        if texts:
            sections.append(({"slide": number}, "\n".join(texts)))
    return sections


EXTRACTORS = {
    "docx": docx_sections,
    "xlsx": xlsx_sections,
    "pptx": pptx_sections,
}
