import argparse
import hashlib
import json
from pathlib import Path

from psycopg.types.json import Jsonb

from app.db import get_connection


def import_chunks(json_path: Path, source_file_id: str):
    document = json.loads(json_path.read_text(encoding="utf-8"))
    chunks = document["chunks"]

    if not chunks:
        raise ValueError("The file contains no chunks.")

    numbers = [chunk["chunk_number"] for chunk in chunks]
    if numbers != list(range(len(chunks))):
        raise ValueError("Chunk numbers must be sequential, starting at zero.")

    for chunk in chunks:
        if not isinstance(chunk["text"], str) or not chunk["text"].strip():
            raise ValueError("Every chunk must contain non-empty text.")

        page = chunk["location_metadata"].get("page_number")
        if not isinstance(page, int) or page < 1:
            raise ValueError("Every PDF chunk must have a valid page number.")

        if chunk["token_count"] > document["model_max_tokens"]:
            raise ValueError("A chunk exceeds the recorded model input limit.")

    # Include chunking configuration and metadata, not just extracted text.
    payload = {
        "title": document["title"],
        "mime_type": document["mime_type"],
        "embedding_model": document["embedding_model"],
        "chunking_version": document["chunking_version"],
        "model_max_tokens": document["model_max_tokens"],
        "overlap_tokens": document["overlap_tokens"],
        "chunks": chunks,
    }
    content_hash = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    with get_connection() as conn:
        source = conn.execute(
            """
            SELECT id
            FROM source_connections
            WHERE provider = 'local'
              AND account_identifier = 'sample-folder';
            """
        ).fetchone()

        if source is None:
            raise RuntimeError("Local source missing. Run init_db.py first.")

        connection_id = source[0]

        existing = conn.execute(
            """
            SELECT id, content_hash
            FROM documents
            WHERE connection_id = %s
              AND external_file_id = %s;
            """,
            (connection_id, source_file_id),
        ).fetchone()

        if existing and existing[1] == content_hash:
            print(f"Unchanged, skipped: {source_file_id}")
            return

        document_id = conn.execute(
            """
            INSERT INTO documents (
                connection_id,
                external_file_id,
                title,
                mime_type,
                content_hash,
                ingestion_status,
                indexed_at
            )
            VALUES (%s, %s, %s, %s, %s, 'pending', NULL)
            ON CONFLICT (connection_id, external_file_id)
            DO UPDATE SET
                title = EXCLUDED.title,
                mime_type = EXCLUDED.mime_type,
                content_hash = EXCLUDED.content_hash,
                ingestion_status = 'pending',
                indexed_at = NULL
            RETURNING id;
            """,
            (
                connection_id,
                source_file_id,
                document["title"],
                document["mime_type"],
                content_hash,
            ),
        ).fetchone()[0]

        conn.execute(
            "DELETE FROM chunks WHERE document_id = %s;",
            (document_id,),
        )

        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO chunks (
                    document_id,
                    chunk_number,
                    text,
                    location_metadata,
                    chunking_version
                )
                VALUES (%s, %s, %s, %s, %s);
                """,
                [
                    (
                        document_id,
                        chunk["chunk_number"],
                        chunk["text"],
                        Jsonb(chunk["location_metadata"]),
                        document["chunking_version"],
                    )
                    for chunk in chunks
                ],
            )

    print(
        f"Imported: {source_file_id} | "
        f"document_id={document_id} | chunks={len(chunks)}"
    )
    print("Status: pending — run embed_chunks.py next.")


def main():
    parser = argparse.ArgumentParser(
        description="Import page-aware PDF chunks into PostgreSQL."
    )
    parser.add_argument("chunk_json", type=Path)
    parser.add_argument(
        "--source-file-id",
        required=True,
        help="PDF path relative to data/sample, e.g. policies/handbook.pdf",
    )
    args = parser.parse_args()

    import_chunks(args.chunk_json, args.source_file_id)


if __name__ == "__main__":
    main()
