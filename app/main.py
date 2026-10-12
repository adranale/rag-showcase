import logging

import psycopg
from fastapi import FastAPI, HTTPException

from app.db import get_connection

logger = logging.getLogger(__name__)

app = FastAPI(
    title="RAG Showcase",
    description="Document assistant for Google Drive and Dropbox",
    version="0.1.0",
)


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