# Copilot Instructions

## Project
RAG Showcase: a document assistant (vector search + cited answers) built with Python 3.12, FastAPI, PostgreSQL/pgvector, sentence-transformers and a local Ollama LLM. Pre-release (v0.1.0); `main` is kept stable, work happens on `dev`. Google Drive / Dropbox connectors are planned; only local files are ingested today.

## Setup & commands (Windows / PowerShell; the `.sh` scripts need Git Bash)
- Virtualenv lives in `.venv` (git-ignored): `.\.venv\Scripts\Activate.ps1`
- Install: `pip install -r requirements-dev.txt` (adds pytest); runtime-only deps are in `requirements.txt`.
- Azure deployment: `deploy\azure.ps1` (see README "Deploy to Azure"); the image is built by `.github/workflows/build-image.yml`.
- Database: `docker compose up -d db` (pgvector/pg16, db/user `rag`, port 5432). Full stack: `docker compose up` (db + api + Streamlit ui on :8501).
- API: `uvicorn app.main:app --reload`; chat UI: `streamlit run chat_ui.py` (or `run_ui.sh`)
- Tests: `pytest`; single test: `pytest tests/test_api.py::test_health -q`
  - `tests/test_api.py` is an integration test: needs a running, indexed Postgres (`DATABASE_URL`) and downloads the embedding model on first run.
  - `tests/test_generation.py` mocks Ollama by monkeypatching `httpx.post`; no services needed.
- Evaluation: `python evaluation\run_evaluation.py` (reads `evaluation/questions.json`, calls the running API, writes `results.csv` / `responses.json`).
- Lint/format (Ruff defaults, no config file): `ruff check .` / `ruff format .`

## Architecture
Ingestion scripts and the API share one Postgres database.

**Ingestion (root-level scripts; `reindex_all.sh` runs the whole pipeline):**
- `init_db.py` applies every `sql/*.sql` file in name order (`000_extensions.sql` creates the `vector` extension).
- `ingest_text.py` (TXT/MD) and `ingest_pdfs.py` (`extract_pdf.py` → `chunk_pdf.py` → `import_pdf_chunks.py`, progress cached in `output/pdf_batch/state.json`) fill `documents`/`chunks`.
- `embed_chunks.py` adds the `embedding` / `embedding_model` columns to `chunks` itself (`ALTER TABLE … IF NOT EXISTS`; they are not in the SQL schema) and embeds rows that are missing or were made with a different model.
- `reset_index.sh` truncates `chunks`/`documents` (destructive). `test_db_connection.py` is a connectivity script, not a pytest test.

**Serving (`app/`):**
- `main.py` – routes `/health`, `/ready`, `/search`, `/chat`. The SentenceTransformer is loaded once in `lifespan` onto `app.state` and retrieval is serialized by `search_lock`. Handlers are sync `def`; error mapping: `ValueError`→422, DB errors→503, `GenerationError`→502.
- `search.py` – embeds the question (normalized; rejects input over the model's token limit) and does cosine search (`<=>`) on `chunks` joined to `documents`, restricted to `embedding_model = MODEL_NAME` and `ingestion_status = 'indexed'`. Changing `MODEL_NAME` requires re-running `embed_chunks.py`.
- `generation.py` – provider chosen by `GENERATION_PROVIDER` (`ollama` default, `azure_openai`); both return JSON validated as `GeneratedAnswer`; passages are treated as untrusted. Citations are validated: `citation_ids` must be a non-empty subset of the supplied `S<n>` IDs and include every inline `[S<n>]`, otherwise `GenerationError`. Insufficient evidence returns no citations.
- `db.py` – `get_connection()` opens a new psycopg connection per call from `DATABASE_URL` (no pool); callers using vectors must `register_vector(conn)`.

## Configuration
Env / `.env` (loaded in `app/db.py`): `DATABASE_URL`, `GENERATION_PROVIDER`, `OLLAMA_BASE_URL` (default `http://127.0.0.1:11434`), `OLLAMA_MODEL` (default `llama3.2:3b`), `AZURE_OPENAI_ENDPOINT` / `_DEPLOYMENT` / `_API_KEY` / `_API_VERSION`, `API_KEY` (optional `X-API-Key` check on `/search` and `/chat`), `APP_PASSWORD` (optional UI login), `RAG_API_BASE_URL` (for `chat_ui.py`). In Docker the API reaches host Ollama via `host.docker.internal`.

## Conventions
- Use type hints and Pydantic models for request/response schemas.
- Keep route handlers thin; put logic in separate modules under `app/`.
- Prefer `async def` for handlers that do I/O (new handlers); keep Ruff-sorted imports.
- Add a pytest test (using FastAPI `TestClient`) for each new endpoint.
- Add dependencies to `requirements.txt` (fully pinned).

## Security
- Never commit secrets. API keys and OAuth credentials (Google, Dropbox, LLM providers) come from environment variables / `.env` (git-ignored).
- Do not log document contents or access tokens.
- Request only the minimum OAuth scopes (read-only where possible).
