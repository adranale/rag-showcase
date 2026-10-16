from pathlib import Path

from app.db import get_connection

sql_dir = Path(__file__).resolve().parent / "sql"

with get_connection() as conn:
    for sql_path in sorted(sql_dir.glob("*.sql")):
        print(f"Applying {sql_path.name}")
        conn.execute(sql_path.read_text(encoding="utf-8"))

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