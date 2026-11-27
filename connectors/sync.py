from dataclasses import dataclass, field

from connectors.base import (
    PermanentDocumentError,
    RemoteFile,
    SourceConnector,
    failure_detail,
)
from connectors.chunking import Chunker, needs_rechunk
from connectors.store import DocumentStore


@dataclass
class SyncResult:
    ingested: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    removal_skipped: bool = False


def display_path(remote: RemoteFile) -> str:
    # Dropbox files carry their full path in the URL; other providers only have a name.
    prefix = "https://www.dropbox.com/home"
    if remote.url and remote.url.startswith(prefix):
        return remote.url[len(prefix) :] or remote.name
    return remote.name


def sync(
    connector: SourceConnector,
    store: DocumentStore,
    chunker: Chunker,
    *,
    delete_removed: bool = False,
    allow_empty: bool = False,
    log_paths: bool = True,
) -> SyncResult:
    """Make the database mirror the remote folder.

    A file is re-ingested only when its remote version changed, a previous attempt
    failed, or its chunks come from an older chunking generation (when the chunker
    has a `generation`; OCR'd documents are kept to avoid paying for OCR again). Files that vanished remotely are tombstoned (chunks deleted,
    status 'removed') or hard-deleted. Removal only happens after the remote
    listing completed successfully, so an API error never wipes the index.
    `log_paths=False` keeps file names out of the logs (multi-user worker).
    """
    result = SyncResult()
    rechunk = getattr(chunker, "generation", None) is not None
    connection_id = store.ensure_connection(
        connector.provider, connector.account_identifier
    )
    remote_files = {remote.file_id: remote for remote in connector.list_files()}
    known = store.documents(connection_id)

    for file_id, remote in remote_files.items():
        stored = known.get(file_id)
        # Only usable documents are re-chunked: failed ones are retried anyway, and an
        # unsupported one keeps its old chunks, so it would be re-processed forever.
        outdated = (
            rechunk
            and stored is not None
            and stored.status in ("pending", "indexed")
            and needs_rechunk(stored.chunking_version)
        )
        if (
            stored
            and not outdated
            and stored.version == remote.version
            and stored.status in ("pending", "indexed", "unsupported")
        ):
            result.skipped.append(file_id)
            continue
        try:
            document = chunker.chunk(remote.kind, connector.download(remote))
            if (
                stored
                and not outdated
                and stored.status in ("pending", "indexed")
                and stored.content_hash == document.content_hash
            ):
                store.update_version(stored.id, remote.version)
                result.skipped.append(file_id)
                continue
            store.save(connection_id, remote, document)
            result.ingested.append(file_id)
        except Exception as exc:  # noqa: BLE001
            # Log only the error type, plus the message of our own error types:
            # other messages could contain document content.
            detail = failure_detail(exc)
            suffix = f" | {detail}" if detail else ""
            where = display_path(remote) if log_paths else "<file>"
            print(f"FAILED: {where}: {type(exc).__name__}{suffix}")
            if isinstance(exc, PermanentDocumentError):
                store.mark_failed(connection_id, remote, permanent=True)
                result.unsupported.append(file_id)
            else:
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
