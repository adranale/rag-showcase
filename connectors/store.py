from dataclasses import dataclass
from typing import Protocol

from psycopg.types.json import Jsonb

from app.db import get_connection
from connectors.base import RemoteFile
from connectors.chunking import ChunkedDocument


@dataclass(frozen=True)
class StoredDocument:
    id: int
    version: str | None
    content_hash: str | None
    status: str


class DocumentStore(Protocol):
    def ensure_connection(self, provider: str, account_identifier: str) -> int: ...

    def documents(self, connection_id: int) -> dict[str, StoredDocument]: ...

    def save(
        self, connection_id: int, remote: RemoteFile, document: ChunkedDocument
    ) -> None: ...

    def update_version(self, document_id: int, version: str) -> None: ...

    def mark_failed(self, connection_id: int, remote: RemoteFile) -> None: ...

    def remove(
        self, connection_id: int, external_ids: list[str], delete: bool
    ) -> None: ...

    def mark_synced(self, connection_id: int) -> None: ...


class PostgresStore:
    """Persists remote documents; every method runs in its own transaction."""

    def ensure_connection(self, provider: str, account_identifier: str) -> int:
        with get_connection() as conn:
            return conn.execute(
                """
                INSERT INTO source_connections (provider, account_identifier)
                VALUES (%s, %s)
                ON CONFLICT (provider, account_identifier)
                DO UPDATE SET account_identifier = EXCLUDED.account_identifier
                RETURNING id;
                """,
                (provider, account_identifier),
            ).fetchone()[0]

    def documents(self, connection_id: int) -> dict[str, StoredDocument]:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT external_file_id, id, source_version, content_hash,
                       ingestion_status
                FROM documents
                WHERE connection_id = %s;
                """,
                (connection_id,),
            ).fetchall()
        return {row[0]: StoredDocument(*row[1:]) for row in rows}

    def save(
        self, connection_id: int, remote: RemoteFile, document: ChunkedDocument
    ) -> None:
        with get_connection() as conn:
            document_id = conn.execute(
                """
                INSERT INTO documents (
                    connection_id, external_file_id, title, source_url,
                    mime_type, content_hash, source_version,
                    ingestion_status, indexed_at, removed_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', NULL, NULL)
                ON CONFLICT (connection_id, external_file_id)
                DO UPDATE SET
                    title = EXCLUDED.title,
                    source_url = EXCLUDED.source_url,
                    mime_type = EXCLUDED.mime_type,
                    content_hash = EXCLUDED.content_hash,
                    source_version = EXCLUDED.source_version,
                    ingestion_status = 'pending',
                    indexed_at = NULL,
                    removed_at = NULL
                RETURNING id;
                """,
                (
                    connection_id,
                    remote.file_id,
                    remote.name.rsplit(".", 1)[0]
                    if "." in remote.name
                    else remote.name,
                    remote.url,
                    remote.mime_type,
                    document.content_hash,
                    remote.version,
                ),
            ).fetchone()[0]
            conn.execute("DELETE FROM chunks WHERE document_id = %s;", (document_id,))
            with conn.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO chunks (
                        document_id, chunk_number, text,
                        location_metadata, chunking_version
                    )
                    VALUES (%s, %s, %s, %s, %s);
                    """,
                    [
                        (
                            document_id,
                            number,
                            chunk["text"],
                            Jsonb(chunk["location_metadata"]),
                            document.chunking_version,
                        )
                        for number, chunk in enumerate(document.chunks)
                    ],
                )

    def update_version(self, document_id: int, version: str) -> None:
        with get_connection() as conn:
            conn.execute(
                "UPDATE documents SET source_version = %s WHERE id = %s;",
                (version, document_id),
            )

    def mark_failed(self, connection_id: int, remote: RemoteFile) -> None:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO documents (
                    connection_id, external_file_id, title, source_url,
                    mime_type, ingestion_status
                )
                VALUES (%s, %s, %s, %s, %s, 'failed')
                ON CONFLICT (connection_id, external_file_id)
                DO UPDATE SET ingestion_status = 'failed';
                """,
                (
                    connection_id,
                    remote.file_id,
                    remote.name,
                    remote.url,
                    remote.mime_type,
                ),
            )

    def remove(self, connection_id: int, external_ids: list[str], delete: bool) -> None:
        if not external_ids:
            return
        with get_connection() as conn:
            if delete:
                conn.execute(
                    """
                    DELETE FROM documents
                    WHERE connection_id = %s AND external_file_id = ANY(%s);
                    """,
                    (connection_id, external_ids),
                )
                return
            conn.execute(
                """
                DELETE FROM chunks
                WHERE document_id IN (
                    SELECT id FROM documents
                    WHERE connection_id = %s AND external_file_id = ANY(%s)
                );
                """,
                (connection_id, external_ids),
            )
            conn.execute(
                """
                UPDATE documents
                SET ingestion_status = 'removed', removed_at = now(),
                    indexed_at = NULL
                WHERE connection_id = %s AND external_file_id = ANY(%s);
                """,
                (connection_id, external_ids),
            )

    def mark_synced(self, connection_id: int) -> None:
        with get_connection() as conn:
            conn.execute(
                "UPDATE source_connections SET last_successful_sync = now() "
                "WHERE id = %s;",
                (connection_id,),
            )
