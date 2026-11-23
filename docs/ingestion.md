# Document ingestion

Users index their own Dropbox folder from the UI (**My documents** → *Sync now*). The API queues a job, and the sync worker (`ingestion/sync_worker.py`, the Container Apps Job `rag-sync`) syncs and embeds the files for that user; see [multi-user.md](multi-user.md) for login, isolation and quotas.

## Pipeline

1. **List** the folder recursively through the Dropbox connector (`connectors/dropbox_source.py`, read-only scopes `files.metadata.read` and `files.content.read`). Folders with more files than the user's `files_per_sync` quota are rejected, never truncated (`connectors/limits.py`).
2. **Download and extract** only new or changed files (`connectors/sync.py`, `connectors/extract.py`, `connectors/ocr.py`).
3. **Chunk** the text into token windows that fit the embedding model (`connectors/chunking.py`).
4. **Store** documents and chunks for that user (`connectors/store.py`), then **embed** the new chunks (`ingestion/embed.py`).

Logs contain counts and error types only, never file names or text.

## Supported files

TXT, Markdown, PDF, Word (`.docx`), Excel (`.xlsx`) and PowerPoint (`.pptx`). Legacy `.doc`/`.xls`/`.ppt`, other types and files over 25 MB are skipped. Excel is read per sheet (cell values, first 5000 rows per sheet; formulas show their last cached value), PowerPoint per slide including tables and speaker notes. Text files that are not UTF-8 are read as Windows-1252; NUL characters are removed. With **OCR enabled** (below), image files (`.png`, `.jpg`/`.jpeg`, `.tif`/`.tiff`, `.bmp`, `.heif`) and PDFs without a text layer are read as well.

**OCR with Azure Document Intelligence (optional, billable):** set `DOCINTEL_ENDPOINT` (https URL) and `DOCINTEL_KEY` of an Azure AI Document Intelligence resource for the worker (`deploy/azure-multiuser.ps1` passes them to `rag-sync`; create the resource yourself, e.g. `az cognitiveservices account create --kind FormRecognizer --sku <sku> ...`, and check the SKU's page/size limits and current pricing). Without both variables OCR is off: images are ignored and scanned PDFs fail. Only PDFs without any extractable text and images are sent to the service (prebuilt-read model), never text-layer PDFs or Office files. Every OCR page is billed, so `DOCINTEL_MAX_PAGES` (default 1000 pages per run, `0` = unlimited) caps a run: files beyond the budget fail and are retried on the next run. Images whose smaller side is under `DOCINTEL_MIN_IMAGE_PIXELS` (default 100, `0` = off) are rejected as "too small" without calling the service; this filters icons and logos.

## Change tracking

Each folder is one `source_connections` row. Per file, `documents` stores the remote file ID (`external_file_id`), the remote revision (`source_version`, Dropbox `rev`), a content hash and `ingestion_status`.

- A sync downloads and re-chunks only files whose revision changed or whose last attempt `failed`; if the revision changed but the extracted content did not, only the revision is updated.
- Files that vanished remotely are **tombstoned**: their chunks are deleted (search never returns them) and the row stays with `ingestion_status = 'removed'` and `removed_at`; a file that reappears is re-ingested. Removals are applied only after the listing succeeded and are skipped when the listing is empty (likely a wrong folder or permissions).
- Files that fail with a transient error are marked `failed` (hidden from search) and retried on the next sync.
- Files that can never be processed (no text, encrypted, empty, too small for OCR) get status `unsupported` and are skipped, without download or OCR, until their revision changes.
- Chunks store a `chunking_version` and an `embedding_model`; search only uses chunks of the current model (`app/config.py`). After changing the model run `python -m ingestion.embed`.

## Sample data (local development)

```powershell
docker compose up -d db
python -m ops.init_db             # applies sql/*.sql in name order (incl. CREATE EXTENSION vector)
python -m ingestion.sample_data   # TXT/Markdown from data/sample/
python -m ingestion.embed         # embeds new or changed chunks (downloads the model on first run)
```

The sample documents have no owner, so no signed-in user sees them; set `OWNER_EMAIL` when running `ops.init_db` to assign them (and any other unowned data) to that user. The workflows **Deploy to Azure** and **Ingest sample data (Azure)** run the same three steps against Azure.

## Google Drive connector

`connectors/gdrive_source.py` (service account or user OAuth, scope `drive.readonly`; native Google Docs are exported as text) implements the same connector interface but is currently not wired to the UI or the worker.
