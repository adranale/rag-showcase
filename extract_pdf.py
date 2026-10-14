import argparse
import json
from pathlib import Path

from pypdf import PdfReader


def extract_pdf(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    reader = PdfReader(path)

    if reader.is_encrypted:
        raise ValueError(
            "Encrypted PDFs are not supported in this first version."
        )

    pages = []
    empty_pages = []

    for page_number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()

        if not text:
            empty_pages.append(page_number)
            continue

        pages.append({
            "page_number": page_number,
            "text": text,
        })

    if not pages:
        raise ValueError(
            "No extractable text found. The PDF may require OCR."
        )

    return {
        "title": path.stem,
        "filename": path.name,
        "mime_type": "application/pdf",
        "page_count": len(reader.pages),
        "pages_without_text": empty_pages,
        "pages": pages,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Extract PDF text while preserving page numbers."
    )
    parser.add_argument("pdf_path", type=Path)
    args = parser.parse_args()

    try:
        document = extract_pdf(args.pdf_path)
    except Exception as exc:
        raise SystemExit(f"Extraction failed: {exc}") from exc

    output_dir = Path(__file__).resolve().parent / "output" / "extraction"
    output_dir.mkdir(parents=True, exist_ok=True)

    output_path = output_dir / f"{args.pdf_path.stem}.json"
    output_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Document: {document['title']}")
    print(f"Total pages: {document['page_count']}")
    print(f"Pages with text: {len(document['pages'])}")
    print(f"Pages without text: {document['pages_without_text']}")
    print(f"Saved extraction: {output_path}")

    print("\nFirst-page preview:\n")
    print(document["pages"][0]["text"][:1000])


if __name__ == "__main__":
    main()
