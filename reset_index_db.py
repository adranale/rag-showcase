"""Deletes all indexed data (documents, chunks, embeddings, remote sources).

Destructive. Used by the "Reset index (Azure)" workflow; the local equivalent
is reset_index.sh. Requires CONFIRM=RESET. The schema stays; init_db.py
re-creates the default local source row afterwards.
"""

import os
import sys

from app.db import get_connection

TRUNCATE = (
    "TRUNCATE TABLE chunks, documents, source_connections RESTART IDENTITY CASCADE"
)


def main() -> int:
    if os.environ.get("CONFIRM") != "RESET":
        print("Refusing to reset: set CONFIRM=RESET.", file=sys.stderr)
        return 1
    with get_connection() as conn:
        conn.execute(TRUNCATE)
    print("Index cleared (documents, chunks, source connections).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
