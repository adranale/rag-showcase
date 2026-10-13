import os

import psycopg
from dotenv import load_dotenv

load_dotenv()

database_url = os.getenv("DATABASE_URL")

if not database_url:
    raise SystemExit("ERROR: DATABASE_URL is missing from .env")

try:
    with psycopg.connect(database_url, connect_timeout=5) as conn:
        database, user, version = conn.execute(
            "SELECT current_database(), current_user, version();"
        ).fetchone()

        vector = conn.execute(
            """
            SELECT extversion
            FROM pg_extension
            WHERE extname = 'vector';
            """
        ).fetchone()

        print("Database connection: OK")
        print(f"Database: {database}")
        print(f"User: {user}")
        print(f"PostgreSQL: {version}")
        print(f"pgvector: {vector[0] if vector else 'NOT ENABLED'}")

except psycopg.Error as exc:
    print(f"Database connection: FAILED\n{exc}")
    raise SystemExit(1)