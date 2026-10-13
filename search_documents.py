import argparse

from pgvector import Vector
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

from app.db import get_connection

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def search(question: str, top_k: int):
    model = SentenceTransformer(MODEL_NAME, device="cpu")

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

        results = conn.execute(
            """
            SELECT
                d.title,
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

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Search indexed document chunks."
    )
    parser.add_argument("question")
    parser.add_argument("--top-k", type=int, default=3)
    args = parser.parse_args()

    if not args.question.strip():
        parser.error("Question must not be empty.")

    if not 1 <= args.top_k <= 20:
        parser.error("--top-k must be between 1 and 20.")

    results = search(args.question.strip(), args.top_k)

    if not results:
        print("No indexed chunks found. Run ingestion and embedding first.")
        return

    print(f"\nQuestion: {args.question}\n")

    for rank, row in enumerate(results, start=1):
        title, chunk_number, text, location, similarity = row

        print(f"{rank}. {title}")
        print(f"   Chunk: {chunk_number}")
        print(f"   Location: {location}")
        print(f"   Similarity: {similarity:.4f}")
        print(f"   Text: {text}\n")


if __name__ == "__main__":
    main()