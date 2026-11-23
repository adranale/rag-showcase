"""Give the `rag_runtime` role a login password (run once with the admin connection).

    ADMIN_DATABASE_URL=... RUNTIME_DB_PASSWORD=... python -m ops.setup_runtime_role

The role is created without login by sql/005_multi_user.sql; its password is never
stored in the repository. Afterwards point the API/worker DATABASE_URL at
`rag_runtime`, keeping the admin URL only for migrations and owner tasks.
"""

import os

import psycopg
from psycopg import sql

MIN_PASSWORD_LENGTH = 24


def main() -> None:
    admin_url = os.getenv("ADMIN_DATABASE_URL") or os.getenv("DATABASE_URL")
    password = os.getenv("RUNTIME_DB_PASSWORD", "")
    if not admin_url:
        raise SystemExit("Set ADMIN_DATABASE_URL (or DATABASE_URL) to the admin connection.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(
            f"RUNTIME_DB_PASSWORD must be set and at least {MIN_PASSWORD_LENGTH} characters."
        )

    with psycopg.connect(admin_url) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = 'rag_runtime';"
        ).fetchone()
        if not exists:
            raise SystemExit("Role rag_runtime is missing. Run python -m ops.init_db first.")
        conn.execute(
            sql.SQL("ALTER ROLE rag_runtime WITH LOGIN PASSWORD {}").format(
                sql.Literal(password)
            )
        )
    print("rag_runtime can now log in.")


if __name__ == "__main__":
    main()
