"""Sync job queue in PostgreSQL (one open job per user; a worker claims jobs)."""

import logging
import os

from psycopg.types.json import Jsonb

from app import azure_arm
from app.db import get_connection

logger = logging.getLogger(__name__)

STALE_AFTER = "2 hours"
_COLUMNS = "id, user_id, status, message, summary, requested_at, started_at, finished_at"


def _as_dict(row) -> dict:
    keys = ("id", "user_id", "status", "message", "summary", "requested_at",
            "started_at", "finished_at")
    return dict(zip(keys, row, strict=True))


def enqueue(user_id: int) -> tuple[dict, bool]:
    """Queue a sync; returns (job, created). An already open job is returned as is."""
    with get_connection() as conn:
        # A worker that died leaves its job 'running' forever: release it.
        conn.execute(
            f"""
            UPDATE sync_jobs
            SET status = 'failed', message = 'Worker did not finish', finished_at = now()
            WHERE user_id = %s AND status = 'running'
              AND started_at < now() - interval '{STALE_AFTER}';
            """,
            (user_id,),
        )
        row = conn.execute(
            f"""
            INSERT INTO sync_jobs (user_id) VALUES (%s)
            ON CONFLICT (user_id) WHERE status IN ('queued', 'running') DO NOTHING
            RETURNING {_COLUMNS};
            """,
            (user_id,),
        ).fetchone()
        if row is not None:
            return _as_dict(row), True
        row = conn.execute(
            f"""
            SELECT {_COLUMNS} FROM sync_jobs
            WHERE user_id = %s AND status IN ('queued', 'running');
            """,
            (user_id,),
        ).fetchone()
    return _as_dict(row), False


def latest(user_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM sync_jobs WHERE user_id = %s ORDER BY id DESC LIMIT 1;",
            (user_id,),
        ).fetchone()
    return _as_dict(row) if row else None


def claim_next() -> dict | None:
    """Atomically take the oldest queued job (several workers are safe)."""
    with get_connection() as conn:
        row = conn.execute(
            f"""
            UPDATE sync_jobs SET status = 'running', started_at = now()
            WHERE id = (
                SELECT id FROM sync_jobs WHERE status = 'queued'
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
            )
            RETURNING {_COLUMNS};
            """
        ).fetchone()
    return _as_dict(row) if row else None


def finish(job_id: int, ok: bool, message: str | None, summary: dict | None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE sync_jobs
            SET status = %s, message = %s, summary = %s, finished_at = now()
            WHERE id = %s;
            """,
            (
                "succeeded" if ok else "failed",
                message,
                Jsonb(summary) if summary is not None else None,
                job_id,
            ),
        )


def trigger_worker() -> bool:
    """Start the worker Container Apps Job (best effort; needs WORKER_JOB_RESOURCE_ID)."""
    job_id = os.getenv("WORKER_JOB_RESOURCE_ID")
    if not job_id:
        return False
    try:
        azure_arm.start_job(job_id)
    except Exception:
        # The job also runs on its schedule, so a failed trigger only delays the sync.
        logger.exception("Could not start the sync worker job")
        return False
    return True
