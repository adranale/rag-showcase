# Copilot Instructions

## Project
RAG Showcase: a document assistant (vector search + cited answers) built with Python 3.12, FastAPI, PostgreSQL/pgvector, sentence-transformers and a local Ollama LLM. Pre-release (v0.1.0); `main` is kept stable, work happens on `dev`. Google Drive / Dropbox connectors are planned; only local files are ingested today.

## Setup & commands (Windows / PowerShell; the `.sh` scripts need Git Bash)
- Virtualenv lives in `.venv` (git-ignored): `.\.venv\Scripts\Activate.ps1`
- Install: `pip install -r requirements-dev.txt` (adds pytest); runtime-only deps are in `requirements.txt`.
- Azure deployment: `pwsh ./deploy/azure.ps1 -SubscriptionId <id> -Suffix <x> [-PreflightOnly]` (see README "Deploy to Azure"; not idempotent); the image is built by `.github/workflows/build-image.yml` (tags: full commit SHA, `latest` only from `main`).
- Database (`compose.yaml`): `docker compose up -d db` (pgvector/pg16, db/user `rag`, port 5432). Full stack: `docker compose up` (db + api + Streamlit ui on :8501).
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
- `db.py` – `get_connection()` opens a new psycopg connection per call from `DATABASE_URL` (no pool); callers using vectors must `register_vector(conn)`. If the connection fails and `POSTGRES_RESOURCE_ID` is set (Azure auto-stop), it asks `app/wake.py` to start the stopped PostgreSQL server (via `app/azure_arm.py`, managed identity) and raises `DatabaseStarting`, which the routes map to 503 "database is starting". `idle_stop.py` (scheduled Container Apps Job, set up by `deploy/azure-autostop.ps1`) stops the server after `IDLE_MINUTES` without API requests.

## Configuration
Env / `.env` (loaded in `app/db.py`): `DATABASE_URL`, `GENERATION_PROVIDER`, `OLLAMA_BASE_URL` (default `http://127.0.0.1:11434`), `OLLAMA_MODEL` (default `llama3.2:3b`), `AZURE_OPENAI_ENDPOINT` / `_DEPLOYMENT` / `_API_KEY` / `_API_VERSION`, `API_KEY` (optional `X-API-Key` check on `/search` and `/chat`), `APP_PASSWORD` (optional UI login), `RAG_API_BASE_URL` (for `chat_ui.py`). In Docker the API reaches host Ollama via `host.docker.internal`.

## Conventions
- Use type hints and Pydantic models for request/response schemas.
- Keep route handlers thin; put logic in separate modules under `app/`.
- Prefer `async def` for handlers that do I/O (new handlers); keep Ruff-sorted imports.
- Add a pytest test (using FastAPI `TestClient`) for each new endpoint.
- Add dependencies to `requirements.txt` (fully pinned).

## Azure test deployment task (branch `azure-deployment`)
Goal: a small, sample-data-only Azure deployment of the existing app, run from macOS (PowerShell 7, Azure CLI logged in). Keep PostgreSQL + pgvector; do not redesign the app, migrate the DB, or add LangChain/other vector DBs/private networking. Inspect actual files first, then implement and validate (not just recommend). Do not provision billable resources, change cloud config, push, publish images, or delete anything without explicit approval; stop before executing the deployment. Never print or commit credentials.

Target (defaults in `deploy/azure.ps1`): resource group `rg-rag-showcase`; PostgreSQL 16 Flexible Server (`Standard_B1ms`, 32 GiB, db `rag`, `VECTOR` in `azure.extensions`); Azure OpenAI `gpt-4.1-mini` / `2025-04-14` / `GlobalStandard`; region `francecentral` (westeurope is PostgreSQL-restricted and swedencentral cannot create PostgreSQL on this subscription; gpt-4o-mini has no quota in francecentral); Container Apps env; internal API app (8000, 1 CPU/2 GiB, 0–1 replicas); public Streamlit UI (8501, 0.5 CPU/1 GiB, 0–1 replicas, UI password); shared API key; image `ghcr.io/adranale/rag-showcase:latest`. These defaults are not verified for region availability, quota or cost.

Required work:
1. **Safe errors:** replace `Invoke-Az` (it echoes args incl. secrets on failure) with a helper using a safe operation label, stopping on failure, applied to all `az` calls (also endpoint/key/FQDN lookups); reject empty/invalid required outputs.
2. **Container startup args:** verify the UI `--command python --args=-m,streamlit,...` syntax under PowerShell→az; use a reliable array or YAML config for `python -m streamlit run chat_ui.py --server.address=0.0.0.0 --server.port=8501 --server.headless=true`. Check image contents, workdir, Docker `ENTRYPOINT`/`CMD` (default must serve FastAPI on 0.0.0.0:8000), UI→internal API HTTPS reachability, and that `APP_PASSWORD`/`API_KEY` are really enforced in code.
3. **Generated secrets:** persist before later failures (macOS-safe paths, `.env.azure` git-ignored, restrictive file permissions, never silently overwrite). Don't claim idempotency unless real; either support safe reruns or detect existing resources and stop with recovery steps; avoid rotating DB credentials / mismatching app secrets.
4. **Explicit subscription:** mandatory parameter; resolve, verify enabled, show name/ID/tenant/resource group/region before creating anything; pass it to every command; fail safely. Don't infer NGO funding from the name (billing must be checked separately). An older Free Trial subscription exists and must not be used by accident.
5. **Preflight (before billable steps):** tools and PowerShell version, auth/subscription, az extensions and resource providers, image accessibility and platform compatibility, OpenAI model/version/SKU availability and quota where queryable, naming rules, non-empty UI password. Report what needs manual verification; never silently switch model, region or SKU.
6. **Image publishing:** confirm the workflow builds the intended branch, GHCR permissions, target platform, and that the deployed reference matches the build. Prefer immutable commit tag/digest (configurable) over `latest`; document that anonymous pulls need public package visibility (or implement authenticated pulls). No secrets in the image.
7. **Networking/DB security (document honestly):** `--public-access 0.0.0.0` is the allow-Azure-services exception, not a subscription/app restriction; keep TLS required; validate and restrict the local ingestion firewall rule to the current public IP and document removing it; not production-hardened. Review admin-account use; prefer a least-privilege runtime role if practical, else document the limitation.
8. **Cost wording:** model quota/capacity is not a monetary cap; budget alerts only notify; scale-to-zero ≠ zero idle cost (PostgreSQL, storage, logs); check NGO credit coverage/balance/expiry in billing. No invented price estimates.
9. **macOS ingestion docs:** PowerShell 7 flow to run the script from repo root, load `DATABASE_URL` from the generated file without printing it, create a Python 3.12 venv, install deps, run `python init_db.py`, `python ingest_text.py`, `python embed_chunks.py` (verify schema creates the real `vector` extension). Sample data only; no PDF ingestion or `reindex_all.sh`.
10. **Validation/cleanup docs:** verify `/health`, `/ready`, UI login, UI→API connectivity, sample retrieval, answer generation (API is internal, so no public `curl`; document an internal test method). Document cleanup for the exact resource group with a data-loss warning; never run it automatically.

Validation without deploying: parse the PowerShell script, run tests, validate workflow/config syntax, build/inspect the container if practical, add focused tests; report tested / failed / untestable. Deliver: changed-files summary with reasons, remaining limitations and manual Azure checks, exact macOS commands separated into safe local/preflight vs billable.

## Working agreement
- If a request is ambiguous, ask the user (ask_user) before acting; do not guess and implement.
- Do not commit, push, deploy, or delete anything without explicit approval.
- Update documentation affected by a change (README, instructions) as part of that change; never ask for permission to do so.
- Commit messages must not mention Copilot or AI tools: no "Co-authored-by: Copilot" trailer, no tool names.

## Security
- Never commit secrets. API keys and OAuth credentials (Google, Dropbox, LLM providers) come from environment variables / `.env` (git-ignored).
- Do not log document contents or access tokens.
- Request only the minimum OAuth scopes (read-only where possible).
