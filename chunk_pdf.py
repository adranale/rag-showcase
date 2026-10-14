import argparse
import json
from pathlib import Path

from sentence_transformers import SentenceTransformer

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
CHUNKING_VERSION = "page-token-window-v1"


def split_page(text, tokenizer, max_tokens, overlap, model_limit):
    """Split extracted page text without decoding token fragments."""
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


def main():
    parser = argparse.ArgumentParser(
        description="Chunk extracted PDF JSON for the embedding model."
    )
    parser.add_argument("extracted_json", type=Path)
    parser.add_argument("--overlap", type=int, default=16)
    args = parser.parse_args()

    document = json.loads(
        args.extracted_json.read_text(encoding="utf-8")
    )

    model = SentenceTransformer(MODEL_NAME, device="cpu")
    tokenizer = model.tokenizer
    model_limit = model.max_seq_length

    if not tokenizer.is_fast:
        raise SystemExit("This script requires a fast tokenizer.")

    special_tokens = tokenizer.num_special_tokens_to_add(pair=False)
    content_budget = model_limit - special_tokens

    if not 0 <= args.overlap < content_budget:
        parser.error(
            f"--overlap must be between 0 and {content_budget - 1}."
        )

    chunks = []

    for page in document["pages"]:
        for part in split_page(
                page["text"],
                tokenizer,
                content_budget,
                args.overlap,
                model_limit
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
        raise SystemExit("No chunks produced.")

    output = {
        "title": document["title"],
        "filename": document["filename"],
        "mime_type": document["mime_type"],
        "embedding_model": MODEL_NAME,
        "chunking_version": CHUNKING_VERSION,
        "model_max_tokens": model_limit,
        "overlap_tokens": args.overlap,
        "chunks": chunks,
    }

    output_dir = Path(__file__).resolve().parent / "output" / "chunks"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{args.extracted_json.stem}.chunks.json"

    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Model input limit: {model_limit} tokens")
    print(f"Content budget: {content_budget} tokens")
    print(f"Chunks created: {len(chunks)}")
    print(f"Saved: {output_path}")

    first = chunks[0]
    print(f"\nFirst chunk location: {first['location_metadata']}")
    print(f"Token count including special tokens: {first['token_count']}")
    print(first["text"])


if __name__ == "__main__":
    main()
