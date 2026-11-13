import re

from pgvector import Vector
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

from app.db import get_connection

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

RRF_K = 60
CANDIDATES_MIN = 25
MAX_KEYWORDS = 8
TITLE_CHUNKS = 2
TITLE_WEIGHT = 3.0  # file/folder-name matches count more than content matches

# Folder path of a Dropbox file (source_url is the Dropbox web URL of the path).
_FOLDER_PATH = """
    CASE WHEN d.source_url LIKE 'https://www.dropbox.com/home%%'
         THEN replace(d.source_url, 'https://www.dropbox.com/home', '')
         ELSE '' END
"""

# Question filler that would match almost every chunk in the keyword search.
STOPWORDS = frozenset(
    """
    the and for are was were what which who whom whose when where why how does did
    you your about with from that this these those have has had can could would
    should tell know please there their them they into over than then also not
    der die das den dem des ein eine einen einem einer und oder ist sind war
    waren wer wie wo wann warum welche welcher welches weiß weisst kannst
    kann bitte mir mich ich du sie wir ihr nicht mit von zu zum zur auf aus bei
    nach über unter für als auch noch nur hat haben wird werden
    """.split()
)


def extract_keywords(question: str) -> list[str]:
    """Distinct lowercase terms (letters/digits, 3+ chars) without filler words."""
    terms: list[str] = []
    for term in re.findall(r"[^\W_]+", question.lower()):
        if len(term) >= 3 and term not in STOPWORDS and term not in terms:
            terms.append(term)
    return terms[:MAX_KEYWORDS]


def fuse(
    rankings: list[list[int]],
    k: int = RRF_K,
    weights: list[float] | None = None,
) -> list[int]:
    """Weighted reciprocal rank fusion: best-first chunk ids from ranked id lists."""
    scores: dict[int, float] = {}
    for index, ranking in enumerate(rankings):
        weight = weights[index] if weights else 1.0
        for position, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (k + position)
    return sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))


def _like_pattern(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%") + "%"


_COLUMNS = """
    d.id,
    d.title,
    d.source_url,
    c.id,
    c.chunk_number,
    c.text,
    c.location_metadata,
    1 - (c.embedding <=> %s) AS similarity
"""

_FILTER = """
    c.embedding IS NOT NULL
    AND c.embedding_model = %s
    AND d.ingestion_status = 'indexed'
"""


def search_documents(
    model: SentenceTransformer,
    question: str,
    top_k: int,
) -> list[dict]:
    """Hybrid retrieval: vector similarity plus keyword and title matches, fused.

    The keyword legs catch exact names, IDs and acronyms that embeddings miss.
    `similarity` stays the cosine similarity of the chunk, whichever leg found it.
    """
    token_ids = model.tokenizer(
        question,
        truncation=False,
        add_special_tokens=True,
    )["input_ids"]

    if len(token_ids) > model.max_seq_length:
        raise ValueError(
            f"Question exceeds the model's {model.max_seq_length}-token input limit."
        )

    embedding = model.encode(
        question,
        normalize_embeddings=True,
    )
    query_vector = Vector(embedding.tolist())
    candidates = max(top_k * 5, CANDIDATES_MIN)
    keywords = extract_keywords(question)

    rows_by_id: dict[int, tuple] = {}
    rankings: list[list[int]] = []
    weights: list[float] = []

    def collect(rows: list[tuple]) -> None:
        for row in rows:
            rows_by_id.setdefault(row[3], row)
        rankings.append([row[3] for row in rows])
        weights.append(1.0)

    with get_connection() as conn:
        register_vector(conn)

        collect(
            conn.execute(
                f"""
                SELECT {_COLUMNS}
                FROM chunks AS c
                JOIN documents AS d ON d.id = c.document_id
                WHERE {_FILTER}
                ORDER BY c.embedding <=> %s, c.id
                LIMIT %s;
                """,
                (query_vector, MODEL_NAME, query_vector, candidates),
            ).fetchall()
        )

        if keywords:
            collect(
                conn.execute(
                    f"""
                    SELECT {_COLUMNS}
                    FROM chunks AS c
                    JOIN documents AS d ON d.id = c.document_id,
                         to_tsquery('simple', %s) AS q
                    WHERE {_FILTER}
                      AND to_tsvector('simple', c.text) @@ q
                    ORDER BY ts_rank_cd(to_tsvector('simple', c.text), q) DESC, c.id
                    LIMIT %s;
                    """,
                    (query_vector, " | ".join(keywords), MODEL_NAME, candidates),
                ).fetchall()
            )

            # Terms in the file name or folder path point at the document: take its
            # first chunks, documents matching the most terms (name counts double) first.
            patterns = [_like_pattern(term) for term in keywords]
            score = " + ".join(
                f"2 * (d.title ILIKE %s)::int + ({_FOLDER_PATH} ILIKE %s)::int"
                for _ in keywords
            )
            score_params = [p for pattern in patterns for p in (pattern, pattern)]
            collect(
                conn.execute(
                    f"""
                    SELECT {_COLUMNS}
                    FROM chunks AS c
                    JOIN documents AS d ON d.id = c.document_id
                    WHERE {_FILTER}
                      AND c.chunk_number < %s
                      AND (d.title ILIKE ANY(%s) OR {_FOLDER_PATH} ILIKE ANY(%s))
                    ORDER BY {score} DESC, d.id, c.chunk_number
                    LIMIT %s;
                    """,
                    (
                        query_vector,
                        MODEL_NAME,
                        TITLE_CHUNKS,
                        patterns,
                        patterns,
                        *score_params,
                        candidates,
                    ),
                ).fetchall()
            )
            weights[-1] = TITLE_WEIGHT

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
        for row in (
            rows_by_id[chunk_id] for chunk_id in fuse(rankings, weights=weights)[:top_k]
        )
    ]
