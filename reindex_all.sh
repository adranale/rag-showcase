#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
source .venv/Scripts/activate

docker compose up -d db

echo "Checking database connection..."
python test_db_connection.py

# Force PDF extraction and chunking to run again.
rm -f output/pdf_batch/state.json

failed=0

echo "Importing TXT and Markdown files..."
python ingest_text.py || failed=1

echo "Extracting, chunking, and importing PDFs..."
python ingest_pdfs.py || failed=1

echo "Generating embeddings..."
python embed_chunks.py || failed=1

if [[ "$failed" -ne 0 ]]; then
    echo "Completed with errors. Check the output above."
    exit 1
fi

echo "Rescan and indexing completed."