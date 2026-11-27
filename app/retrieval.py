"""The hybrid search (app/search.py) as a LangChain retriever.

The SQL, the per-user row-level security and the rank fusion stay in app/search.py;
the retriever only scopes a search to one user and turns rows into `Document`s.
"""

from contextlib import nullcontext
from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict

from app import search

# Row fields carried as Document metadata (the text is the page content).
METADATA_FIELDS = (
    "document_id",
    "title",
    "source_url",
    "chunk_id",
    "chunk_number",
    "location",
    "similarity",
)


class HybridRetriever(BaseRetriever):
    """Top `top_k` chunks of one user's indexed documents for a question."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    embeddings: Any
    user_id: int
    top_k: int = 3
    # Serializes use of the shared embedding model (app.state.search_lock).
    lock: Any = None

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        with self.lock if self.lock is not None else nullcontext():
            rows = search.search_documents(
                self.embeddings, query, self.top_k, user_id=self.user_id
            )
        return [
            Document(
                page_content=row["text"],
                metadata={name: row[name] for name in METADATA_FIELDS},
            )
            for row in rows
        ]


def to_passages(documents: list[Document]) -> list[dict]:
    """The search result dicts (API format) of retrieved documents."""
    return [{**document.metadata, "text": document.page_content} for document in documents]
