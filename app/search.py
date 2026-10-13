from pgvector import Vector
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

from app.db import get_connection

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def search_documents(
    model: SentenceTransformer,
    question: str,
    top_k: int,
) -> list[dict]:
    token_ids = model.tokenizer(
        question,
        truncation=False,
        add_special_tokens=True,
    )["input_ids"]

    if len(token_ids) > model.max_seq_length:
        raise ValueError(
            f"Question exceeds the model's "
            f"{model.max_seq_length}-token input limit."
        )

    embedding = model.encode(
        question,
        normalize_embeddings=True,
    )
    query_vector = Vector(embedding.tolist())

    with get_connection() as conn:
        register_vector(conn)

        rows = conn.execute(
            """
            SELECT
                d.id,
                d.title,
                d.source_url,
                c.id,
                c.chunk_number,
                c.text,
                c.location_metadata,
                1 - (c.embedding <=> %s) AS similarity
            FROM chunks AS c
            JOIN documents AS d ON d.id = c.document_id
            WHERE c.embedding IS NOT NULL
              AND c.embedding_model = %s
              AND d.ingestion_status = 'indexed'
            ORDER BY c.embedding <=> %s, c.id
            LIMIT %s;
            """,
            (query_vector, MODEL_NAME, query_vector, top_k),
        ).fetchall()

    return [
        {
            "document_id": row[0],
            "title": row[1],
            "source_url": row[2],
            "chunk_id": row[3],
            "chunk_number": row[4],
            "text": row[5],
            "location": row[6],
            "similarity": float(row[7]),
        }
        for row in rows
    ]