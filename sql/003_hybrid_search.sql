-- Keyword side of hybrid search: full-text index over chunk text. The 'simple'
-- configuration (no stemming, no stop words) keeps names and acronyms searchable
-- in any language. The expression must match app/search.py exactly. Idempotent.

CREATE INDEX IF NOT EXISTS chunks_text_fts_idx
    ON chunks USING gin (to_tsvector('simple', text));
