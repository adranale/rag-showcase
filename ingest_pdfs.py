import hashlib
import json
from pathlib import Path

from sentence_transformers import SentenceTransformer

from chunk_pdf import CHUNKING_VERSION, MODEL_NAME, split_page
from extract_pdf import extract_pdf
from import_pdf_chunks import import_chunks

PROJECT_ROOT = Path(__file__).resolve().parent
PDF_FOLDER = PROJECT_ROOT / "data" / "private"
OUTPUT_FOLDER = PROJECT_ROOT / "output" / "pdf_batch"
OVERLAP = 16


def file_hash(path):
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


def load_previous_state(path):
    if not path.exists():
        return {}

    return json.loads(path.read_text(encoding="utf-8"))


def main():
    if not PDF_FOLDER.is_dir():
        raise SystemExit(f"Folder not found: {PDF_FOLDER}")

    pdfs = sorted(
        path
        for path in PDF_FOLDER.rglob("*")
        if path.is_file() and path.suffix.lower() == ".pdf"
    )

    if not pdfs:
        print("No PDF files found.")
        return

    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
    state_path = OUTPUT_FOLDER / "state.json"
    previous_state = load_previous_state(state_path)

    model = SentenceTransformer(MODEL_NAME, device="cpu")
    tokenizer = model.tokenizer
    model_limit = model.max_seq_length

    if not tokenizer.is_fast:
        raise SystemExit("A fast tokenizer is required.")

    content_budget = (
            model_limit - tokenizer.num_special_tokens_to_add(pair=False)
    )

    if not 0 <= OVERLAP < content_budget:
        raise SystemExit("Overlap must be smaller than the content budget.")

    config = {
        "model": MODEL_NAME,
        "model_limit": model_limit,
        "overlap": OVERLAP,
        "chunking_version": CHUNKING_VERSION,
        "pipeline_version": "pdf-batch-v1",
    }

    imported = 0
    skipped = 0
    failures = []

    for pdf_path in pdfs:
        source_file_id = pdf_path.relative_to(PDF_FOLDER).as_posix()

        try:
            fingerprint = {
                "file_hash": file_hash(pdf_path),
                "config": config,
            }

            if previous_state.get(source_file_id) == fingerprint:
                print(f"Unchanged, skipped: {source_file_id}")
                skipped += 1
                continue

            document = extract_pdf(pdf_path)
            chunks = []

            for page in document["pages"]:
                for part in split_page(
                        page["text"],
                        tokenizer,
                        content_budget,
                        OVERLAP,
                        model_limit,
                ):
                    chunks.append({
                        "chunk_number": len(chunks),
                        "text": part["text"],
                        "token_count": part["token_count"],
                        "location_metadata": {
                            "page_number": page["page_number"],
                            "char_start": part["char_start"],
                            "char_end": part["char_end"],
                        },
                    })

            if not chunks:
                raise ValueError("No chunks produced.")

            payload = {
                "title": document["title"],
                "filename": document["filename"],
                "mime_type": document["mime_type"],
                "embedding_model": MODEL_NAME,
                "chunking_version": CHUNKING_VERSION,
                "model_max_tokens": model_limit,
                "overlap_tokens": OVERLAP,
                "chunks": chunks,
            }

            artifact_id = hashlib.sha256(
                source_file_id.encode("utf-8")
            ).hexdigest()

            chunk_path = OUTPUT_FOLDER / f"{artifact_id}.chunks.json"
            chunk_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            import_chunks(chunk_path, source_file_id)

            previous_state[source_file_id] = fingerprint

            temporary_path = state_path.with_suffix(".tmp")
            temporary_path.write_text(
                json.dumps(previous_state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary_path.replace(state_path)

            imported += 1

        except Exception as exc:
            failures.append(source_file_id)
            print(f"FAILED: {source_file_id}: {exc}")

    print(
        f"\nPDFs found: {len(pdfs)} | "
        f"Imported/processed: {imported} | "
        f"Skipped: {skipped} | Failed: {len(failures)}"
    )

    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
