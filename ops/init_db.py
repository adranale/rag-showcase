import os
from pathlib import Path

from app.db import get_connection

sql_dir = Path(__file__).resolve().parent.parent / "sql"

# Migrations need the table owner; the app itself runs as the rag_runtime role.
if os.getenv("ADMIN_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["ADMIN_DATABASE_URL"]


def assign_owner(conn, email: str) -> None:
    """Create the owner user (an admin) and give them all data that has no owner yet."""
    owner_id = conn.execute(
        """
        INSERT INTO users (email, is_admin) VALUES (%s, true)
        ON CONFLICT (lower(email)) DO UPDATE SET is_admin = true
        RETURNING id;
        """,
        (email.strip().lower(),),
    ).fetchone()[0]
    conn.execute(
        "UPDATE source_connections SET user_id = %s WHERE user_id IS NULL;", (owner_id,)
    )
    conn.execute(
        """
        UPDATE documents AS d SET user_id = s.user_id
        FROM source_connections AS s
        WHERE d.connection_id = s.id AND d.user_id IS NULL;
        """
    )
    conn.execute(
        """
        UPDATE chunks AS c SET user_id = d.user_id
        FROM documents AS d
        WHERE c.document_id = d.id AND c.user_id IS NULL;
        """
    )
    print(f"Existing data is owned by user id {owner_id}")


with get_connection() as conn:
    for sql_path in sorted(sql_dir.glob("*.sql")):
        print(f"Applying {sql_path.name}")
        conn.execute(sql_path.read_text(encoding="utf-8"))

    if os.getenv("OWNER_EMAIL"):
        assign_owner(conn, os.environ["OWNER_EMAIL"])

    tables = conn.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name IN (
              'source_connections',
              'documents',
              'chunks'
          )
        ORDER BY table_name;
        """
    ).fetchall()

    source = conn.execute(
        """
        SELECT id, provider, account_identifier
        FROM source_connections
        WHERE provider = 'local'
          AND account_identifier = 'sample-folder';
        """
    ).fetchone()

print("Schema setup completed.")
print("Tables:", ", ".join(row[0] for row in tables))
print("Local source:", source)