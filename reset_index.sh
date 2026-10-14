#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "WARNING: This deletes ALL indexed documents, chunks, and embeddings."
read -r -p "Type RESET to continue: " confirmation

if [[ "$confirmation" != "RESET" ]]; then
    echo "Cancelled."
    exit 0
fi

docker compose exec -T db psql \
    -U rag -d rag \
    -v ON_ERROR_STOP=1 \
    -c "TRUNCATE TABLE chunks, documents RESTART IDENTITY;"

rm -f output/pdf_batch/state.json

echo "Index cleared. Source files were not deleted."