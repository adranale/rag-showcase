import os

import psycopg
from dotenv import load_dotenv

from app import wake

load_dotenv()


def get_connection(user_id: int | None = None):
    """Open a connection; with `user_id` the row-level-security scope is set.

    Under the `rag_runtime` role a connection without `user_id` sees no user data.
    """
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured")

    try:
        conn = psycopg.connect(
            database_url,
            connect_timeout=5,
        )
    except psycopg.OperationalError as exc:
        # With auto-stop enabled the server may simply be stopped: start it.
        if wake.trigger_start():
            raise wake.DatabaseStarting from exc
        raise

    if user_id is not None:
        conn.execute(
            "SELECT set_config('app.user_id', %s, false)", (str(int(user_id)),)
        )
    return conn
