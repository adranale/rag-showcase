"""Settings shared by the API and the ingestion pipeline."""

# Changing the model requires re-embedding (python -m ingestion.embed) and, for a
# different dimension, a schema change in sql/ (vector(384)).
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_DIMENSION = 384
