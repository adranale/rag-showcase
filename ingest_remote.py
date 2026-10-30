import argparse

from connectors import PROVIDERS, create_connector
from connectors.base import ConnectorError
from connectors.chunking import PipelineChunker
from connectors.store import PostgresStore
from connectors.sync import sync


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest TXT/MD/PDF files from Dropbox or Google Drive."
    )
    parser.add_argument("--provider", required=True, choices=PROVIDERS)
    parser.add_argument(
        "--folder",
        required=True,
        help="Dropbox path (e.g. /Documents/rag, '/' for everything) "
        "or Google Drive folder ID",
    )
    parser.add_argument(
        "--delete-removed",
        action="store_true",
        help="hard-delete documents whose remote file vanished "
        "(default: keep a 'removed' tombstone without chunks)",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="allow removals even if the remote folder lists no supported files",
    )
    args = parser.parse_args()

    try:
        connector = create_connector(args.provider, args.folder)
        result = sync(
            connector,
            PostgresStore(),
            PipelineChunker(),
            delete_removed=args.delete_removed,
            allow_empty=args.allow_empty,
        )
    except ConnectorError as exc:
        raise SystemExit(f"Remote ingestion failed: {exc}") from exc

    print(
        f"Ingested: {len(result.ingested)} | Skipped: {len(result.skipped)} | "
        f"Removed: {len(result.removed)} | Failed: {len(result.failed)}"
    )
    if result.removal_skipped:
        print(
            "Remote folder lists no supported files; removals skipped "
            "(use --allow-empty to override)."
        )
    if result.ingested:
        print("Status: pending — run embed_chunks.py next.")
    if result.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
