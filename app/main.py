import logging
from contextlib import asynccontextmanager
from threading import Lock

import psycopg
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sentence_transformers import SentenceTransformer

from app.db import get_connection
from app.search import MODEL_NAME, search_documents

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.embedding_model = SentenceTransformer(
        MODEL_NAME,
        device="cpu",
    )
    app.state.search_lock = Lock()
    yield
    del app.state.embedding_model


app = FastAPI(
    title="RAG Showcase",
    version="0.1.0",
    lifespan=lifespan,
)


class SearchRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=3, ge=1, le=20)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT extversion
                FROM pg_extension
                WHERE extname = 'vector'
                """
            ).fetchone()

    except (psycopg.Error, RuntimeError):
        logger.exception("Database readiness check failed")
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        ) from None

    if row is None:
        raise HTTPException(
            status_code=503,
            detail="Vector extension is not enabled",
        )

    return {
        "status": "ready",
        "database": "connected",
        "vector_extension": row[0],
    }


@app.post("/search")
def search(payload: SearchRequest, request: Request):
    try:
        with request.app.state.search_lock:
            results = search_documents(
                model=request.app.state.embedding_model,
                question=payload.question,
                top_k=payload.top_k,
            )

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from None

    except (psycopg.Error, RuntimeError):
        logger.exception("Search failed")
        raise HTTPException(
            status_code=503,
            detail="Search is temporarily unavailable",
        ) from None

    return {
        "question": payload.question,
        "count": len(results),
        "results": results,
    }