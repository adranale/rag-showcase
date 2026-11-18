import logging
from contextlib import asynccontextmanager, contextmanager
from threading import Lock

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sentence_transformers import SentenceTransformer

from app import documents, dropbox_oauth, jobs, users, wake
from app.auth import current_user
from app.crypto import CryptoError
from app.db import get_connection
from app.generation import GenerationError, generate_answer
from app.quota import QuotaExceeded, consume_chat
from app.search import MODEL_NAME, search_documents
from app.users import User

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
    version="2.0.0",
    lifespan=lifespan,
)


class SearchRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=3, ge=1, le=20)


class DropboxCallback(BaseModel):
    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=1, max_length=256)


class FolderRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    folder: str = Field(max_length=1024)


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

    except (psycopg.Error, RuntimeError) as exc:
        logger.exception("Database readiness check failed")
        raise HTTPException(
            status_code=503,
            detail=wake.describe(exc, "Database unavailable"),
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
def search(
    payload: SearchRequest, request: Request, user: User = Depends(current_user)
):
    try:
        with request.app.state.search_lock:
            results = search_documents(
                model=request.app.state.embedding_model,
                question=payload.question,
                top_k=payload.top_k,
                user_id=user.id,
            )

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from None

    except (psycopg.Error, RuntimeError) as exc:
        logger.exception("Search failed")
        raise HTTPException(
            status_code=503,
            detail=wake.describe(exc, "Search is temporarily unavailable"),
        ) from None

    return {
        "question": payload.question,
        "count": len(results),
        "results": results,
    }


@app.post("/chat")
def chat(
    payload: SearchRequest, request: Request, user: User = Depends(current_user)
):
    try:
        consume_chat(user)
    except QuotaExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from None
    except (psycopg.Error, RuntimeError) as exc:
        logger.exception("Quota check failed")
        raise HTTPException(
            status_code=503,
            detail=wake.describe(exc, "Service temporarily unavailable"),
        ) from None

    try:
        with request.app.state.search_lock:
            passages = search_documents(
                model=request.app.state.embedding_model,
                question=payload.question,
                top_k=payload.top_k,
                user_id=user.id,
            )

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from None

    except (psycopg.Error, RuntimeError) as exc:
        logger.exception("Chat retrieval failed")
        raise HTTPException(
            status_code=503,
            detail=wake.describe(exc, "Search is temporarily unavailable"),
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


@contextmanager
def _service_errors(what: str):
    """Map storage/crypto failures of the account routes to HTTP errors."""
    try:
        yield
    except dropbox_oauth.OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except CryptoError:
        logger.exception("Token encryption is not usable")
        raise HTTPException(status_code=503, detail=f"{what} is not available") from None
    except (psycopg.Error, RuntimeError) as exc:
        logger.exception("%s failed", what)
        raise HTTPException(
            status_code=503,
            detail=wake.describe(exc, f"{what} is temporarily unavailable"),
        ) from None


@app.get("/me")
def me(user: User = Depends(current_user)):
    with _service_errors("Account"):
        dropbox = dropbox_oauth.status(user)
        job = jobs.latest(user.id)
    return {
        "email": user.email,
        "chats_per_day": user.chats_per_day,
        "files_per_sync": user.files_per_sync,
        "dropbox": dropbox,
        "sync": _public_job(job),
    }


@app.post("/dropbox/authorize")
def dropbox_authorize(user: User = Depends(current_user)):
    with _service_errors("Dropbox"):
        return {"url": dropbox_oauth.start_authorization(user)}


@app.post("/dropbox/callback")
def dropbox_callback(payload: DropboxCallback, user: User = Depends(current_user)):
    with _service_errors("Dropbox"):
        dropbox_oauth.complete_authorization(user, payload.code, payload.state)
        users.audit(user.id, "dropbox_connected")
    return {"connected": True}


@app.put("/dropbox/folder")
def dropbox_folder(payload: FolderRequest, user: User = Depends(current_user)):
    with _service_errors("Dropbox"):
        return {"folder": dropbox_oauth.set_folder(user, payload.folder)}


@app.delete("/dropbox")
def dropbox_disconnect(
    delete_documents: bool = False, user: User = Depends(current_user)
):
    with _service_errors("Dropbox"):
        dropbox_oauth.disconnect(user, delete_documents)
        users.audit(user.id, "dropbox_disconnected", f"delete_documents={delete_documents}")
    return {"connected": False, "documents_deleted": delete_documents}


@app.post("/sync", status_code=202)
def start_sync(user: User = Depends(current_user)):
    with _service_errors("Sync"):
        status = dropbox_oauth.status(user)
        if not status["connected"]:
            raise HTTPException(status_code=409, detail="Connect Dropbox first.")
        if status["folder"] is None:
            raise HTTPException(status_code=409, detail="Choose a Dropbox folder first.")
        job, created = jobs.enqueue(user.id)
    if created:
        jobs.trigger_worker()
    return _public_job(job)


@app.get("/documents/{document_id}/chunks")
def document_text(document_id: int, user: User = Depends(current_user)):
    with _service_errors("Documents"):
        result = documents.document_chunks(user, document_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    return result


@app.delete("/me/documents")
def delete_documents(user: User = Depends(current_user)):
    with _service_errors("Account"):
        with get_connection(user.id) as conn:
            conn.execute("DELETE FROM source_connections WHERE user_id = %s;", (user.id,))
        users.audit(user.id, "documents_deleted")
    return {"documents_deleted": True}


def _public_job(job: dict | None) -> dict | None:
    if job is None:
        return None
    return {
        "status": job["status"],
        "message": job["message"],
        "summary": job["summary"],
        "requested_at": job["requested_at"],
        "finished_at": job["finished_at"],
    }