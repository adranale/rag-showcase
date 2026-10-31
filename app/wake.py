"""Starts a stopped PostgreSQL Flexible Server when the API cannot reach it.

Enabled only when POSTGRES_RESOURCE_ID is set (Azure deployment with auto-stop).
"""

import logging
import os
import time
from threading import Lock, Thread

from app import azure_arm

logger = logging.getLogger(__name__)

STARTING_MESSAGE = (
    "The database was stopped after an idle period and is starting. "
    "Please retry in 1-3 minutes."
)
THROTTLE_SECONDS = 60

_lock = Lock()
_last_attempt = 0.0


class DatabaseStarting(RuntimeError):
    """The database is unreachable and a start was requested."""


def enabled() -> bool:
    return bool(os.getenv("POSTGRES_RESOURCE_ID"))


def _start() -> None:
    server_id = os.environ["POSTGRES_RESOURCE_ID"]
    try:
        if azure_arm.server_state(server_id) == "Stopped":
            azure_arm.start_server(server_id)
            logger.info("Requested start of the stopped PostgreSQL server")
    except Exception:
        logger.exception("Could not request a PostgreSQL start")


def trigger_start() -> bool:
    """Requests a server start in the background (at most once a minute)."""
    global _last_attempt

    if not enabled():
        return False

    with _lock:
        now = time.monotonic()
        if now - _last_attempt >= THROTTLE_SECONDS:
            _last_attempt = now
            Thread(target=_start, daemon=True).start()
    return True


def describe(exc: Exception, default: str) -> str:
    return STARTING_MESSAGE if isinstance(exc, DatabaseStarting) else default
