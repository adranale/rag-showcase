# Document ingestion (admin / command line)

In multi-user mode (v2) users index their own Dropbox from the UI (**My documents**, processed by `sync_worker.py`); see [multi-user.md](multi-user.md). The command-line tools below write documents **without an owner**, which no v2 user can see: use them for local development or a single-user (v1) deployment.

## Local files

```powershell
docker compose up -d db
python init_db.py       # applies sql/*.sql in name order (incl. CREATE EXTENSION vector)
python ingest_text.py   # TXT/Markdown from data/sample/
python ingest_pdfs.py   # PDFs from data/private/ (git-ignored): extract -> chunk -> import, progress in output/pdf_batch/state.json
python embed_chunks.py  # embeds new or changed chunks (downloads the model on first run)
```

`reindex_all.sh` (Git Bash) runs the whole pipeline; `reset_index.sh` truncates `chunks`/`documents` (destructive).

## Ingest from Dropbox or Google Drive

`ingest_remote.py` reads TXT, Markdown, PDF, Word (`.docx`), Excel (`.xlsx`) and PowerPoint (`.pptx`) files (plus native Google Docs, exported as text) from a Dropbox or Google Drive folder, recursively, and stores them in the same `documents`/`chunks` tables as local files. Legacy `.doc`/`.xls`/`.ppt` are not supported and are skipped silently. Excel is read per sheet (cell values, first 5000 rows per sheet; formulas show their last cached value), PowerPoint per slide including tables and speaker notes. With **OCR enabled** (below), image files (`.png`, `.jpg`/`.jpeg`, `.tif`/`.tiff`, `.bmp`, `.heif`) and PDFs without a text layer are read as well. Run `embed_chunks.py` afterwards. Access is **read-only**; credentials only come from environment variables / `.env` (git-ignored) and are never logged or stored in the database.

**OCR with Azure Document Intelligence (optional, billable):** set `DOCINTEL_ENDPOINT` (https URL) and `DOCINTEL_KEY` of an Azure AI Document Intelligence resource (create it yourself, e.g. `az cognitiveservices account create --kind FormRecognizer --sku <sku> ...`; check the SKU's page/size limits and current pricing in the Azure portal). Without both variables OCR is off: images are ignored and scanned PDFs fail as before. Files are sent to the Azure service for text extraction (prebuilt-read model); only PDFs without any extractable text and images are sent, never text-layer PDFs or Office files. Every OCR page is billed, so `DOCINTEL_MAX_PAGES` (default 1000 pages per run, `0` = unlimited) caps a run: files beyond the budget fail and are retried on the next run, which makes large backlogs resumable. Images whose smaller side is under `DOCINTEL_MIN_IMAGE_PIXELS` (default 100, `0` = off) are skipped as failed ("too small") without calling the service; this filters icons and logos. For the Azure job set the GitHub secrets `DOCINTEL_ENDPOINT` / `DOCINTEL_KEY` (and optionally the variable `DOCINTEL_MAX_PAGES`) before running the ingest workflow; `deploy/azure-ingest-job.ps1` stores them as job secrets. Note that the script replaces the job's env vars, so always re-supply them on later runs.

```powershell
python init_db.py                      # applies sql/002_remote_sources.sql (idempotent)
python ingest_remote.py --provider dropbox --folder /Documents/rag
python ingest_remote.py --provider google_drive --folder <drive-folder-id>
python embed_chunks.py
```
Options: `--delete-removed` (hard-delete instead of tombstone), `--allow-empty` (allow removals when the remote folder lists no supported files).

**Change tracking.** Each folder is one `source_connections` row (`dropbox` / `google_drive`, identified by the folder). Per file, `documents` stores the remote file ID (`external_file_id`), the remote revision (`source_version`: Dropbox `rev`, Drive `md5Checksum` or `version`), a content hash and `ingestion_status`. A re-run downloads and re-chunks only files whose revision changed or whose last attempt `failed`; if the revision changed but the extracted content did not, only the revision is updated. Files that vanished remotely are **tombstoned** by default: their chunks are deleted (so search never returns them) and the row stays with `ingestion_status = 'removed'` and `removed_at`; a file that reappears is re-ingested. Hard deletion is opt-in. Removals are applied only after the remote listing succeeded, and are skipped when the listing is empty (likely a wrong folder or permissions) unless `--allow-empty` is set. Files that fail to download or are rejected by a transient error are marked `failed` (hidden from search) and retried on the next run; the CLI then exits non-zero. Files that can never be processed (no text, encrypted, empty, too small for OCR) get status `unsupported` (`sql/004_unsupported_status.sql`) and are skipped, without download or OCR, until their remote revision changes; they do not make the CLI fail. Unsupported file types and files over 25 MB are skipped.

### Dropbox setup
1. Go to the [Dropbox App Console](https://www.dropbox.com/developers/apps) → *Create app* → *Scoped access* → *Full Dropbox* (or *App folder* to restrict to one folder).
2. Under *Permissions* enable only `files.metadata.read` and `files.content.read`, then *Submit*.
3. Note the *App key* and *App secret*.
4. Obtain a long-lived refresh token once: open `https://www.dropbox.com/oauth2/authorize?client_id=<APP_KEY>&response_type=code&token_access_type=offline`, approve, and copy the code. Exchange it (keep the output private):
   ```powershell
   curl.exe -u "<APP_KEY>:<APP_SECRET>" -d "code=<CODE>&grant_type=authorization_code" https://api.dropboxapi.com/oauth2/token
   ```
   Use the `refresh_token` from the response. Set `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN`. `--folder` is a path such as `/Documents/rag` (`/` = whole app scope). In the Azure workflow the folder is read from the `azure-test` environment **secret** `INGEST_FOLDER` (Settings → Environments → *Secrets*); there is no input for it, so GitHub masks the path in the public logs.

### Google Drive setup (service account, recommended)
1. In Google Cloud Console create a project, enable the **Google Drive API**, create a **service account** and add a JSON key (store it securely; never commit it).
2. In Drive, **share the folder with the service account's e-mail** (Viewer is enough). The scope used is `drive.readonly`; `drive.file` would not see files you shared with it.
3. Set `GOOGLE_SERVICE_ACCOUNT_JSON` to the *content* of the key file. `--folder` is the folder ID (the last part of the folder URL).
4. Alternative (user OAuth): set `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN` (refresh token obtained for the `drive.readonly` scope).

### Run inside Azure (Container Apps Job)
`deploy/azure-ingest-job.ps1` creates/updates the job `rag-ingest` in the existing Container Apps environment (`cae-rag-showcase`). The job runs the **same image as the deployed `rag-api`** (read from the app; the image must be built from a commit that contains `ingest_job.py`, so redeploy `rag-api` with a new image first) and executes `python ingest_job.py`, i.e. `init_db.py` → `ingest_remote.py` → `embed_chunks.py`, configured by the env vars `PROVIDER` / `FOLDER` (1 CPU, 2 GiB, no retries by default, 1 h timeout). `DATABASE_URL` is reused from the `db-url` secret of `rag-api` (never printed, never stored in the repo); the PostgreSQL "allow Azure services" rule gives the job access, so no firewall rule is needed.

The job bills only while an execution runs (scale-to-zero); Log Analytics ingestion and PostgreSQL are billed regardless. An optional cron schedule is available (`-Schedule`, UTC).

Security: provider credentials and the DB URL are stored as Container Apps secrets, which anyone with Contributor (or higher) on the resource group can read. They are passed to `az` as command-line arguments (visible to local process listings on the machine running the script); use a dedicated read-only provider app/token.

Run (PowerShell 7, `az login` done; export the provider credentials as env vars first):

```powershell
$env:DROPBOX_APP_KEY = '...'; $env:DROPBOX_APP_SECRET = '...'; $env:DROPBOX_REFRESH_TOKEN = '...'
pwsh ./deploy/azure-ingest-job.ps1 -SubscriptionId <guid> -Suffix <suffix> -Provider dropbox -Folder /Documents/rag -StartNow
# recurring (UTC cron): add -Schedule '0 3 * * *'; Google Drive: -Provider google_drive -Folder <folderId>
```

Rerunning updates the job in place; switching between manual and scheduled requires deleting the job first. Or use the manual workflow **Azure ingestion job** (`azure-ingest-job.yml`, environment `azure-test`: same Azure OIDC secrets as the deploy workflow, plus the provider secrets (`DROPBOX_*` / `GOOGLE_*`) and `INGEST_FOLDER` as `azure-test` environment secrets). The database must be running (auto-stop) when the job starts.

> **Legacy (single-user, v1):** in multi-user mode (v2) this job writes documents without an owner that no user can see; users sync their own Dropbox from the UI instead. The `azure-test` secrets `DROPBOX_REFRESH_TOKEN` and `INGEST_FOLDER` were removed from the deployment environment; add them again only if you run this workflow against a v1 deployment.

**Partial failures:** a file that cannot be processed (scanned PDF without text, encrypted PDF, unreadable file) is logged as `FAILED: <path>: <error type> | <reason>` (full Dropbox path, file name for other providers) (the reason, e.g. HTTP status, size, pages, is shown only for our own error types, never for messages that could contain document text) and retried on the next run, except permanent failures (shown as `Unsupported: <n>` in the summary), which are not retried until the file changes and do not fail the job. The job still embeds everything that was ingested and then exits non-zero, so the execution shows `Failed` while the other documents are searchable. Text files that are not UTF-8 are read as Windows-1252; NUL characters are removed. PDFs without a text layer are OCR'd when Document Intelligence is configured, otherwise they fail.

View executions and logs:

```powershell
az containerapp job execution list --resource-group rg-rag-showcase --name rag-ingest --subscription <guid> --output table
# logs: Log Analytics workspace of the environment (may lag a few minutes)
az monitor log-analytics query --workspace <workspace-customer-id> --analytics-query "ContainerAppConsoleLogs_CL | where ContainerGroupName_s startswith '<execution-name>' | project TimeGenerated, Log_s | order by TimeGenerated asc"
```

Delete the job (credentials stored in it go away with it): `az containerapp job delete --resource-group rg-rag-showcase --name rag-ingest --subscription <guid> --yes`.

### Change the Dropbox folder or reset the index (Azure)
- **Change the folder:** update the `azure-test` secret `INGEST_FOLDER` and run **Azure ingestion job** again. Each folder is its own source; documents of the old folder stay indexed.
- **Reset:** the manual workflow **Reset index (Azure)** (`reset-index-azure.yml`, environment `azure-test`) deletes **all** documents, chunks, embeddings and source connections (`TRUNCATE`; the schema stays, `init_db.py` re-creates the default local source). It only runs when `confirm` is exactly `RESET`, starts the database if auto-stop had stopped it, opens a temporary firewall rule for the runner and always removes it. The sample documents are deleted too: afterwards run **Ingest sample data (Azure)** and/or **Azure ingestion job**. There is no undo. Locally (Docker) use `reset_index.sh`.
- **Diagnose:** the manual, read-only workflow **Azure diagnose** (`azure-diagnose.yml`, environment `azure-test`) runs `diagnose.py` inside Azure as a one-off run of the `rag-ingest` job (the job must exist). Given a `query` it prints document counts by status, failed file names, chunk/embedding counts, files whose title or text contain the query, and the top semantic hits with scores. It never prints document text. The result contains file names and titles, so the public GitHub log only shows a line count; read the output in Log Analytics (`ContainerAppConsoleLogs_CL`) or set `print_output=true` (anyone with a GitHub account can read workflow logs of a public repo). Use it to tell "not indexed" from "not retrieved".
