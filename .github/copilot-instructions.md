# Copilot Instructions

## Project
RAG Showcase: a document assistant for Google Drive and Dropbox, built with Python 3.12 and FastAPI. The project is early-stage (v0.1.0); only a `/health` endpoint exists so far.

## Layout
- `app/main.py` – FastAPI app instance (`app`) and routes.
- `app/` – application package; put new modules (connectors, ingestion, retrieval, schemas) here.
- `data/private/` – local private data; git-ignored, never commit or read contents into code/logs.

## Setup & commands (Windows / PowerShell)
- Virtualenv lives in `.venv` (git-ignored): `.\.venv\Scripts\Activate.ps1`
- Run dev server: `uvicorn app.main:app --reload`
- Tests (when added): `pytest`; lint/format: `ruff check .` / `ruff format .`

## Conventions
- Use type hints and Pydantic models for request/response schemas.
- Keep route handlers thin; put logic in separate modules under `app/`.
- Prefer `async def` for handlers that do I/O (Drive/Dropbox/LLM/vector store calls).
- Follow Ruff defaults; keep imports sorted.
- Add a pytest test (using FastAPI `TestClient`) for each new endpoint.
- Add new dependencies to a dependency manifest (e.g. `requirements.txt` or `pyproject.toml`) – none exists yet.

## Security
- Never commit secrets. API keys and OAuth credentials (Google, Dropbox, LLM providers) come from environment variables / `.env` (git-ignored).
- Do not log document contents or access tokens.
- Request only the minimum OAuth scopes (read-only where possible).
