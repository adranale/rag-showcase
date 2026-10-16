# RAG Showcase

RAG Showcase is a lightweight Retrieval-Augmented Generation (RAG) application for searching and asking questions about internal documents. It is built with Python 3.12, FastAPI, PostgreSQL with the pgvector extension, and sentence-transformers for embeddings.

The project is currently in early pre-release status and focuses on a document assistant pattern suitable for enterprise knowledge bases backed by local or cloud document stores.

## What this project does

- Stores document chunks in a vector database
- Embeds queries and matches relevant passages
- Returns grounded search results via a FastAPI API
- Answers questions using retrieved passages and an LLM provider
- Includes sample local documents for testing and evaluation

## Current API

The application exposes the following endpoints:

- `GET /health` — basic service health check
- `GET /ready` — checks database availability and pgvector extension
- `POST /search` — retrieve the most relevant document chunks for a question
- `POST /chat` — retrieve relevant passages and generate a cited answer

## Architecture

- `app/main.py` — FastAPI app and route definitions
- `app/db.py` — database connection helpers
- `app/search.py` — embedding and vector search logic
- `app/generation.py` — answer generation from retrieved context
- `data/sample/` — sample documents used for local development
- `tests/` — API and generation tests
- `evaluation/` — evaluation scripts and result output

## Local development

### Prerequisites

- Python 3.12
- PostgreSQL with the `vector` extension enabled
- Ollama installed and running if you want answer generation

### Setup

On Windows PowerShell:

```powershell
cd C:\Gothaer\code\github\adranale\rag-showcase
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Run the API

```powershell
uvicorn app.main:app --reload
```

Then open:

- http://127.0.0.1:8000/docs for the FastAPI Swagger UI
- http://127.0.0.1:8000/health for the health endpoint

## Environment and secrets

This project expects configuration through environment variables or a local `.env` file (git-ignored). Keep secrets out of source control.

Typical configuration includes:

- database connection settings
- LLM provider credentials or local model configuration
- optional cloud connector credentials for Google Drive or Dropbox

## Data and privacy

- `data/private/` is reserved for local private content and is git-ignored
- Do not commit API keys, OAuth tokens, or document contents to the repository
- Follow the least-privilege OAuth model for external integrations

## Example usage

Search for relevant passages:

```bash
curl -X POST "http://127.0.0.1:8000/search" \
  -H "Content-Type: application/json" \
  -d '{"question":"What is the volunteer handbook policy?","top_k":3}'
```

Generate an answer from retrieved context:

```bash
curl -X POST "http://127.0.0.1:8000/chat" \
  -H "Content-Type: application/json" \
  -d '{"question":"What is the volunteer handbook policy?","top_k":3}'
```

## Deploy to Azure (minimum cost)

Architecture: PostgreSQL Flexible Server (Burstable B1ms, pgvector) + Azure OpenAI (`gpt-4o-mini`, capped quota) + two Azure Container Apps (internal API, public password-protected UI) that scale to zero. Embeddings stay local (CPU) inside the image.

1. **Image:** the `Build image` workflow pushes `ghcr.io/adranale/rag-showcase:latest`. After the first run, set the package visibility to **Public** (GitHub → Packages → Package settings) so Container Apps can pull it without credentials. The image contains no secrets.
2. **Provision:** `az login`, then `.\deploy\azure.ps1 -Suffix <unique-lowercase-id>`. It generates all secrets, stores them as Container Apps secrets and writes the database URL to the git-ignored `.env.azure`.
3. **Initialise and ingest** from your machine (the script adds your IP to the DB firewall):
   ```powershell
   $env:DATABASE_URL = (Get-Content .env.azure).Split('=', 2)[1]
   python init_db.py
   python ingest_text.py      # sample documents only
   python embed_chunks.py
   ```
   Do not run `ingest_pdfs.py` / `reindex_all.sh` against Azure unless the PDFs are meant to be public (`data/private/` is personal).
4. Open the UI URL printed by the script and log in with the chosen password. The first request after idle takes about 30–60 s (cold start).
5. Create a budget alert in Azure Cost Management. Delete everything with `az group delete --name rg-rag-showcase`.

Settings: `GENERATION_PROVIDER` (`ollama` default, or `azure_openai`), `API_KEY` (optional; enforces the `X-API-Key` header on `/search` and `/chat`), `APP_PASSWORD` (optional UI password). See `.env.azure.example`.

## Repository conventions

- Use async route handlers for I/O-bound work
- Prefer type hints and Pydantic models
- Keep route handlers thin and move logic into modules under `app/`
- Validate with pytest when adding functionality

## Release status

This repository is currently tagged as a pre-release:

- `v0.1.0-pre-release`

The `main` branch is kept stable while ongoing work continues on the `dev` branch.

## Project status

This is an early-stage showcase and is intended to demonstrate a practical RAG implementation for document retrieval and answer generation. It is not yet production-hardened and should be treated as a development-focused reference project.

