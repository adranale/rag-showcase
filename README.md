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

