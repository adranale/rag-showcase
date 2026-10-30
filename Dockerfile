FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    HF_HOME=/home/appuser/.cache/huggingface \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app

# The bracket glob makes the corporate CA optional: the build still works
# when certs/corporate-root-ca.crt is absent (CI, Azure).
COPY requirements.txt cert[s]/corporate-root-ca.crt /tmp/build/

RUN if [ -f /tmp/build/corporate-root-ca.crt ]; then \
        cp /tmp/build/corporate-root-ca.crt \
           /usr/local/share/ca-certificates/corporate-root-ca.crt; \
    fi \
    && update-ca-certificates

# CPU-only torch wheels avoid downloading several GB of CUDA libraries.
RUN python -m pip install --no-cache-dir \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    -r /tmp/build/requirements.txt

RUN useradd --create-home appuser \
    && mkdir -p /home/appuser/.cache/huggingface \
    && chown -R appuser:appuser /home/appuser/.cache

COPY --chown=appuser:appuser app/ ./app/
COPY --chown=appuser:appuser chat_ui.py idle_stop.py ./
# Remote ingestion job (deploy/azure-ingest-job.ps1) runs from the same image.
COPY --chown=appuser:appuser connectors/ ./connectors/
COPY --chown=appuser:appuser sql/ ./sql/
COPY --chown=appuser:appuser init_db.py ingest_text.py ingest_remote.py \
    ingest_job.py embed_chunks.py chunk_pdf.py extract_pdf.py ./

USER appuser

# Bake the embedding model into the image so scale-from-zero starts do not
# download it.
RUN python -c "from sentence_transformers import SentenceTransformer; \
from app.search import MODEL_NAME; \
SentenceTransformer(MODEL_NAME, device='cpu')"

EXPOSE 8000 8501

CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000"]