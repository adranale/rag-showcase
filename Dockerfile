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

COPY certs/corporate-root-ca.crt \
    /usr/local/share/ca-certificates/corporate-root-ca.crt

RUN update-ca-certificates

COPY requirements.txt .

RUN python -m pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home appuser \
    && mkdir -p /home/appuser/.cache/huggingface \
    && chown -R appuser:appuser /home/appuser/.cache

COPY --chown=appuser:appuser app/ ./app/
COPY --chown=appuser:appuser chat_ui.py .

USER appuser

EXPOSE 8000 8501

CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000"]