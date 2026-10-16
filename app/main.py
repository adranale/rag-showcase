import logging
import os
import secrets
from contextlib import asynccontextmanager
from threading import Lock

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sentence_transformers import SentenceTransformer

from app.db import get_connection
from app.search import MODEL_NAME, search_documents
from app.generation import GenerationError, generate_answer

logger = logging.getLogger(__name__)


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    # Authentication is enabled only when API_KEY is configured.
    expected = os.getenv("API_KEY")

    if expected and not (
        x_api_key and secrets.compare_digest(x_api_key, expected)
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key",
        )


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


@app.post("/search", dependencies=[Depends(require_api_key)])
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

@app.post("/chat", dependencies=[Depends(require_api_key)])
def chat(payload: SearchRequest, request: Request):
    try:
        with request.app.state.search_lock:
            passages = search_documents(
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
        logger.exception("Chat retrieval failed")
        raise HTTPException(
            status_code=503,
            detail="Search is temporarily unavailable",
        ) from None

    try:
        result = generate_answer(payload.question, passages)

    except GenerationError:
        logger.exception("Answer generation failed")
        raise HTTPException(
            status_code=502,
            detail="Could not generate a valid cited answer. "
                   "Check that Ollama is running and the model is installed.",
        ) from None

    return {
        "question": payload.question,
        "retrieved_count": len(passages),
        **result,
    }