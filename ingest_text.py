import hashlib
import re
from pathlib import Path

from psycopg.types.json import Jsonb

from app.db import get_connection

PROJECT_ROOT = Path(__file__).resolve().parent
SAMPLE_FOLDER = PROJECT_ROOT / "data" / "sample"


def ingest_file(path: Path):
    text = path.read_text(encoding="utf-8").strip()

    if not text:
        raise ValueError(f"Document is empty: {path.name}")

    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    external_file_id = path.relative_to(SAMPLE_FOLDER).as_posix()

    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", text)
        if paragraph.strip()
    ]

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
            WHERE connection_id = %s AND external_file_id = %s;
            """,
            (connection_id, external_file_id),
        ).fetchone()

        if existing and existing[1] == content_hash:
            print(f"Unchanged, skipped: {external_file_id}")
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
                external_file_id,
                path.stem,
                "text/plain",
                content_hash,
            ),
        ).fetchone()[0]

        conn.execute(
            "DELETE FROM chunks WHERE document_id = %s;",
            (document_id,),
        )

        for number, paragraph in enumerate(paragraphs):
            conn.execute(
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
                (
                    document_id,
                    number,
                    paragraph,
                    Jsonb({"paragraph": number + 1}),
                    "paragraph-v1",
                ),
            )

    print(
        f"Stored: {external_file_id} | "
        f"document_id={document_id} | chunks={len(paragraphs)}"
    )


if __name__ == "__main__":
    ingest_file(SAMPLE_FOLDER / "volunteer-handbook.txt")