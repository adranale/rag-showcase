import os

import psycopg
from dotenv import load_dotenv

from app import wake

load_dotenv()


def get_connection():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured")

    try:
        return psycopg.connect(
            database_url,
            connect_timeout=5,
        )
    except psycopg.OperationalError as exc:
        # With auto-stop enabled the server may simply be stopped: start it.
        if wake.trigger_start():
            raise wake.DatabaseStarting from exc
        raise
