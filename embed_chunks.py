from pgvector import Vector
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

from app.db import get_connection

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def main():
    print(f"Loading model: {MODEL_NAME}")
    model = SentenceTransformer(MODEL_NAME, device="cpu")

    dimension = model.get_sentence_embedding_dimension()
    print(f"Embedding dimension: {dimension}")
    print(f"Maximum input length: {model.max_seq_length} tokens")

    with get_connection() as conn:
        conn.execute(
            f"""
            ALTER TABLE chunks
            ADD COLUMN IF NOT EXISTS embedding vector({int(dimension)});
            """
        )
        conn.execute(
            """
            ALTER TABLE chunks
            ADD COLUMN IF NOT EXISTS embedding_model TEXT;
            """
        )

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
            return

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

        embeddings = model.encode(
            texts,
            batch_size=16,
            normalize_embeddings=True,
            show_progress_bar=True,
        )

        for (chunk_id, _), embedding in zip(chunks, embeddings):
            conn.execute(
                """
                UPDATE chunks
                SET embedding = %s,
                    embedding_model = %s
                WHERE id = %s;
                """,
                (Vector(embedding.tolist()), MODEL_NAME, chunk_id),
            )

        conn.execute(
            """
            UPDATE documents AS d
            SET ingestion_status = 'indexed',
                indexed_at = now()
            WHERE EXISTS (
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

    print(f"Saved embeddings for {len(chunks)} chunks.")


if __name__ == "__main__":
    main()