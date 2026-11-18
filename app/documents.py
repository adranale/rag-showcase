from app.db import get_connection
from app.users import User

MAX_CHUNKS = 500


def document_chunks(user: User, document_id: int) -> dict | None:
    """Chunks of one of the user's documents in reading order, or None if not theirs."""
    with get_connection(user.id) as conn:
        document = conn.execute(
            "SELECT title, source_url FROM documents WHERE id = %s AND user_id = %s;",
            (document_id, user.id),
        ).fetchone()
        if document is None:
            return None
        rows = conn.execute(
            """
            SELECT id, chunk_number, text, location_metadata
            FROM chunks WHERE document_id = %s AND user_id = %s
            ORDER BY chunk_number LIMIT %s;
            """,
            (document_id, user.id, MAX_CHUNKS),
        ).fetchall()
    return {
        "document_id": document_id,
        "title": document[0],
        "source_url": document[1],
        "chunks": [
            {"chunk_id": r[0], "chunk_number": r[1], "text": r[2], "location": r[3]}
            for r in rows
        ],
    }
