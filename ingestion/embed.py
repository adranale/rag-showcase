from pgvector import Vector
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

from app.config import EMBEDDING_DIMENSION, MODEL_NAME
from app.db import get_connection

SLICE_SIZE = 1000


def main():
    print(f"Loading model: {MODEL_NAME}")
    model = SentenceTransformer(MODEL_NAME, device="cpu")

    dimension = model.get_embedding_dimension()
    print(f"Embedding dimension: {dimension}")
    print(f"Maximum input length: {model.max_seq_length} tokens")

    with get_connection() as conn:
        # The columns are created by sql/005_multi_user.sql (vector(384)).
        if dimension != EMBEDDING_DIMENSION:
            raise SystemExit(
                f"Model dimension {dimension} does not match the schema "
                f"(vector({EMBEDDING_DIMENSION})); update sql/ first."
            )

        embed_pending(conn, model)


def embed_pending(conn, model: SentenceTransformer) -> int:
    """Embed every chunk visible on `conn` that lacks a current embedding.

    On a user-scoped connection (get_connection(user_id)) only that user's chunks
    are visible. Returns the number of embedded chunks.
    """
    register_vector(conn)

    chunks = conn.execute(
        """
        SELECT id, text
        FROM chunks
        WHERE embedding IS NULL
           OR embedding_model IS DISTINCT FROM %s
        ORDER BY id;
        """,
        (MODEL_NAME,),
    ).fetchall()

    if not chunks:
        print("No chunks need embedding.")
        mark_indexed(conn)
        conn.commit()
        return 0

    texts = [text for _, text in chunks]

    for chunk_id, text in chunks:
        token_ids = model.tokenizer(
            text,
            truncation=False,
            add_special_tokens=True,
        )["input_ids"]

        if len(token_ids) > model.max_seq_length:
            raise ValueError(
                f"Chunk {chunk_id} has {len(token_ids)} tokens. "
                f"The model limit is {model.max_seq_length}. "
                "Split this chunk before embedding."
            )

    # Commit per slice so a timed-out run keeps its progress and a rerun resumes.
    for start in range(0, len(chunks), SLICE_SIZE):
        part = chunks[start : start + SLICE_SIZE]
        embeddings = model.encode(
            texts[start : start + SLICE_SIZE],
            batch_size=16,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        for (chunk_id, _), embedding in zip(part, embeddings):
            conn.execute(
                """
                UPDATE chunks
                SET embedding = %s,
                    embedding_model = %s
                WHERE id = %s;
                """,
                (Vector(embedding.tolist()), MODEL_NAME, chunk_id),
            )
        mark_indexed(conn)
        conn.commit()
        print(f"Embedded {start + len(part)}/{len(chunks)} chunks", flush=True)

    print(f"Saved embeddings for {len(chunks)} chunks.")
    return len(chunks)


def mark_indexed(conn) -> None:
    conn.execute(
        """
        UPDATE documents AS d
        SET ingestion_status = 'indexed',
            indexed_at = now()
        WHERE d.ingestion_status IS DISTINCT FROM 'indexed'
        AND EXISTS (
            SELECT 1
            FROM chunks AS c
            WHERE c.document_id = d.id
        )
        AND NOT EXISTS (
            SELECT 1
            FROM chunks AS c
            WHERE c.document_id = d.id
              AND (
                  c.embedding IS NULL
                  OR c.embedding_model IS DISTINCT FROM %s
              )
        );
        """,
        (MODEL_NAME,),
    )


if __name__ == "__main__":
    main()