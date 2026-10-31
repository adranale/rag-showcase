"""Read-only index diagnostics, run inside Azure (see the "diagnose" action of the
Azure operations workflow). Prints counts, file names and similarity scores only;
never document text.

Environment: DATABASE_URL (from the job) and DIAG_QUERY (question or search term).
"""

import os

from app.db import get_connection
from app.search import MODEL_NAME, search_documents

TOP_K = 8


def report(query: str) -> None:
    term = f"%{query}%"
    with get_connection() as conn:
        print("== documents by status")
        for status, count in conn.execute(
            "SELECT ingestion_status, count(*) FROM documents GROUP BY 1 ORDER BY 1"
        ):
            print(f"{status}: {count}")

        print("== failed documents")
        for (title,) in conn.execute(
            "SELECT title FROM documents WHERE ingestion_status = 'failed' ORDER BY 1"
        ):
            print(title)

        print("== chunks")
        for model, total, embedded in conn.execute(
            "SELECT embedding_model, count(*), count(embedding) FROM chunks GROUP BY 1"
        ):
            print(f"model={model} chunks={total} embedded={embedded}")

        print(f"== documents whose title or text contains {query!r}")
        rows = conn.execute(
            """
            SELECT d.title, d.ingestion_status,
                   count(c.id) FILTER (WHERE c.text ILIKE %s) AS text_hits,
                   count(c.id) AS chunks
            FROM documents AS d
            LEFT JOIN chunks AS c ON c.document_id = d.id
            WHERE d.title ILIKE %s OR c.text ILIKE %s
            GROUP BY d.id, d.title, d.ingestion_status
            ORDER BY text_hits DESC
            LIMIT 20
            """,
            (term, term, term),
        ).fetchall()
        for title, status, hits, chunks in rows:
            print(f"{title} [{status}] text_hits={hits} chunks={chunks}")
        if not rows:
            print("(none)")

    print(f"== semantic top {TOP_K} (model {MODEL_NAME})")
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(MODEL_NAME)
    for hit in search_documents(model, query, TOP_K):
        print(f"{hit['similarity']:.3f} {hit['title']}")


if __name__ == "__main__":
    report(os.environ.get("DIAG_QUERY", "").strip() or "test")
