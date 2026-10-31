from dataclasses import dataclass, field

from connectors.base import SourceConnector
from connectors.chunking import Chunker
from connectors.store import DocumentStore


@dataclass
class SyncResult:
    ingested: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    removal_skipped: bool = False


def sync(
    connector: SourceConnector,
    store: DocumentStore,
    chunker: Chunker,
    *,
    delete_removed: bool = False,
    allow_empty: bool = False,
) -> SyncResult:
    """Make the database mirror the remote folder.

    A file is re-ingested only when its remote version changed (or a previous
    attempt failed). Files that vanished remotely are tombstoned (chunks deleted,
    status 'removed') or hard-deleted. Removal only happens after the remote
    listing completed successfully, so an API error never wipes the index.
    """
    result = SyncResult()
    connection_id = store.ensure_connection(
        connector.provider, connector.account_identifier
    )
    remote_files = {remote.file_id: remote for remote in connector.list_files()}
    known = store.documents(connection_id)

    for file_id, remote in remote_files.items():
        stored = known.get(file_id)
        if (
            stored
            and stored.version == remote.version
            and stored.status in ("pending", "indexed")
        ):
            result.skipped.append(file_id)
            continue
        try:
            document = chunker.chunk(remote.kind, connector.download(remote))
            if (
                stored
                and stored.status in ("pending", "indexed")
                and stored.content_hash == document.content_hash
            ):
                store.update_version(stored.id, remote.version)
                result.skipped.append(file_id)
                continue
            store.save(connection_id, remote, document)
            result.ingested.append(file_id)
        except Exception as exc:  # noqa: BLE001
            # Log only the error type: messages could contain document content.
            print(f"FAILED: {remote.name}: {type(exc).__name__}")
            store.mark_failed(connection_id, remote)
            result.failed.append(file_id)

    gone = [
        file_id
        for file_id, stored in known.items()
        if file_id not in remote_files
        and (stored.status != "removed" or delete_removed)
    ]
    if gone and not remote_files and not allow_empty:
        # An empty listing is more likely a wrong folder or permissions problem.
        result.removal_skipped = True
    else:
        store.remove(connection_id, gone, delete_removed)
        result.removed = gone

    if not result.failed:
        store.mark_synced(connection_id)
    return result
