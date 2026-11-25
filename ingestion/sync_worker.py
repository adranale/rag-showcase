"""Sync worker: processes queued per-user storage syncs (Dropbox or Google Drive),
then exits.

Runs as a Container Apps Job (started by the API on /sync, plus a safety schedule).
It connects as the `rag_runtime` role and scopes every database access to the job's
user (row-level security). Logs contain counts and error types only, never file
names or content.
"""

import sys

from app import crypto, jobs, storage
from app.db import get_connection
from app.oauth import OAuthError
from connectors.base import ConnectorError
from connectors.chunking import PipelineChunker
from connectors.limits import LimitedConnector
from connectors.store import PostgresStore
from connectors.sync import sync


def run_job(job: dict, load_model) -> bool:
    user_id = job["user_id"]
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT files_per_sync, status FROM users WHERE id = %s;", (user_id,)
            ).fetchone()
        if row is None or row[1] != "active":
            raise ConnectorError("This account is not enabled.")

        credentials = storage.load_credentials(user_id)
        if credentials is None or credentials.folder is None:
            raise ConnectorError("Connect Dropbox or Google Drive and choose a folder first.")

        connector = LimitedConnector(storage.source_for(credentials), row[0])
        result = sync(connector, PostgresStore(user_id), PipelineChunker(), log_paths=False)

        embedded = 0
        with get_connection(user_id) as conn:
            embedded = _embed(conn, load_model)
        summary = {
            "ingested": len(result.ingested),
            "skipped": len(result.skipped),
            "removed": len(result.removed),
            "failed": len(result.failed),
            "unsupported": len(result.unsupported),
            "embedded_chunks": embedded,
        }
        message = (
            "Some files failed and are retried on the next sync." if result.failed else None
        )
        jobs.finish(job["id"], not result.failed, message, summary)
        return not result.failed
    except (ConnectorError, OAuthError, crypto.CryptoError) as exc:
        jobs.finish(job["id"], False, str(exc) or type(exc).__name__, None)
    except Exception as exc:  # noqa: BLE001
        # The message of other exceptions could contain document content.
        print(f"Job {job['id']} failed: {type(exc).__name__}", flush=True)
        jobs.finish(job["id"], False, f"Sync failed ({type(exc).__name__})", None)
    return False


def _embed(conn, load_model) -> int:
    from ingestion.embed import embed_pending

    return embed_pending(conn, load_model())


def main() -> int:
    model = None

    def load_model():
        nonlocal model
        if model is None:
            from sentence_transformers import SentenceTransformer

            from app.config import MODEL_NAME

            model = SentenceTransformer(MODEL_NAME, device="cpu")
        return model

    processed = failures = 0
    while (job := jobs.claim_next()) is not None:
        processed += 1
        if not run_job(job, load_model):
            failures += 1
    print(f"Sync jobs processed: {processed}, failed: {failures}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
