import logging
from contextlib import asynccontextmanager, contextmanager
from threading import Lock
from typing import Literal

import psycopg
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sentence_transformers import SentenceTransformer

from app import access, documents, jobs, notify, storage, users, wake
from app.auth import current_admin, current_identity, current_user
from app.crypto import CryptoError
from app.db import get_connection
from app.generation import GenerationError, generate_answer
from app.oauth import OAuthError
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
    version="0.1.0",
    lifespan=lifespan,
)


class SearchRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=3, ge=1, le=20)


class StorageCallback(BaseModel):
    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=1, max_length=256)


class AuthorizeRequest(BaseModel):
    # Required when connecting another provider would delete indexed documents.
    confirm_delete: bool = False


class FolderRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    folder: str = Field(max_length=1024)
    # Required when the new folder would delete documents of another folder.
    confirm_delete: bool = False


class AccessRequestBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    invite_code: str = Field(min_length=1, max_length=128)
    message: str | None = Field(default=None, max_length=access.MAX_MESSAGE_LENGTH)


class UserStatusBody(BaseModel):
    status: Literal["active", "disabled"]


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
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except storage.ConfirmationRequired as exc:
        raise HTTPException(
            status_code=409,
            detail=f"This deletes your {exc.documents} indexed documents of the current "
            "folder. Confirm to continue.",
        ) from None
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
        storage_status = storage.status(user)
        job = jobs.latest(user.id)
        pending = access.pending_count(user) if user.is_admin else None
    return {
        "email": user.email,
        "is_admin": user.is_admin,
        "pending_requests": pending,
        "chats_per_day": user.chats_per_day,
        "files_per_sync": user.files_per_sync,
        "storage": storage_status,
        "sync": _public_job(job),
    }


@app.get("/access-request")
def access_request_status(claims: dict = Depends(current_identity)):
    with _service_errors("Access requests"):
        return access.request_status(claims)


@app.post("/access-request")
def request_access(
    payload: AccessRequestBody,
    background: BackgroundTasks,
    claims: dict = Depends(current_identity),
):
    with _service_errors("Access requests"):
        result = access.submit_request(claims, payload.invite_code, payload.message)
    if result == "created":
        background.add_task(notify.notify_access_request, claims, payload.message)
    return {"result": result}


@app.get("/admin/requests")
def admin_requests(pending_only: bool = True, admin: User = Depends(current_admin)):
    with _service_errors("Admin"):
        return access.list_requests(admin, pending_only)


@app.post("/admin/requests/{request_id}/{decision}")
def admin_decide(
    request_id: int,
    decision: Literal["approve", "refuse"],
    admin: User = Depends(current_admin),
):
    with _service_errors("Admin"):
        result = access.decide(admin, request_id, decision == "approve")
    if result == "not_pending":
        raise HTTPException(status_code=409, detail="This request was already decided.")
    if result == "email_in_use":
        raise HTTPException(
            status_code=409, detail="Another account already uses this e-mail address."
        )
    return {"result": result}


@app.get("/admin/users")
def admin_users(admin: User = Depends(current_admin)):
    with _service_errors("Admin"):
        return access.list_users(admin)


@app.put("/admin/users/{user_id}/status")
def admin_user_status(
    user_id: int, payload: UserStatusBody, admin: User = Depends(current_admin)
):
    with _service_errors("Admin"):
        result = access.set_user_status(admin, user_id, payload.status)
    if result == "self":
        raise HTTPException(status_code=409, detail="You cannot change your own account.")
    if result == "not_found":
        raise HTTPException(status_code=404, detail="User not found.")
    return {"status": result}


@app.get("/admin/invite")
def admin_invite(admin: User = Depends(current_admin)):
    with _service_errors("Admin"):
        return {"code": access.invite_code(admin)}


@app.post("/admin/invite")
def admin_rotate_invite(admin: User = Depends(current_admin)):
    with _service_errors("Admin"):
        return {"code": access.rotate_invite_code(admin)}


@app.post("/storage/{provider}/authorize")
def storage_authorize(
    provider: Literal["dropbox", "google_drive"],
    payload: AuthorizeRequest | None = None,
    user: User = Depends(current_user),
):
    confirm = payload.confirm_delete if payload else False
    with _service_errors("Storage"):
        return {"url": storage.start_authorization(user, provider, confirm)}


@app.post("/storage/callback")
def storage_callback(payload: StorageCallback, user: User = Depends(current_user)):
    with _service_errors("Storage"):
        provider = storage.complete_authorization(user, payload.code, payload.state)
        users.audit(user.id, "storage_connected", f"provider={provider}")
    return {"connected": True, "provider": provider}


@app.put("/storage/folder")
def storage_folder(payload: FolderRequest, user: User = Depends(current_user)):
    with _service_errors("Storage"):
        result = storage.set_folder(user, payload.folder, payload.confirm_delete)
        if result["documents_deleted"]:
            users.audit(
                user.id, "folder_changed", f"documents_deleted={result['documents_deleted']}"
            )
    return result


@app.delete("/storage")
def storage_disconnect(
    delete_documents: bool = False, user: User = Depends(current_user)
):
    with _service_errors("Storage"):
        storage.disconnect(user, delete_documents)
        users.audit(user.id, "storage_disconnected", f"delete_documents={delete_documents}")
    return {"connected": False, "documents_deleted": delete_documents}


@app.post("/sync", status_code=202)
def start_sync(user: User = Depends(current_user)):
    with _service_errors("Sync"):
        status = storage.status(user)
        if not status["connected"]:
            raise HTTPException(
                status_code=409, detail="Connect Dropbox or Google Drive first."
            )
        if status["folder"] is None:
            raise HTTPException(status_code=409, detail="Choose a folder first.")
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