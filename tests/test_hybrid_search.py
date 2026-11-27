import pytest

from app.search import extract_keywords, fuse


def test_extract_keywords_drops_filler_and_short_words():
    assert extract_keywords("What do you know about Toters?") == ["toters"]
    assert extract_keywords("Was ist der Toters_AI_Lead Vertrag?") == [
        "toters",
        "lead",
        "vertrag",
    ]


def test_extract_keywords_is_distinct_and_bounded():
    assert extract_keywords("Toters toters TOTERS") == ["toters"]
    assert len(extract_keywords(" ".join(f"term{i}" for i in range(20)))) == 8


def test_fuse_promotes_chunks_found_by_several_rankings():
    vector_hits = [10, 11, 12]
    keyword_hits = [99, 12]
    assert fuse([vector_hits, keyword_hits]) == [12, 10, 99, 11]


def test_fuse_handles_empty_rankings():
    assert fuse([[], []]) == []
    assert fuse([[3, 1]]) == [3, 1]


def test_fuse_weight_lets_a_title_hit_outrank_a_content_hit():
    content_hits = [1, 2]
    title_hits = [3]
    assert fuse([content_hits, title_hits]) == [1, 3, 2]
    assert fuse([content_hits, title_hits], weights=[1.0, 3.0]) == [3, 1, 2]


def test_title_leg_query_has_matching_placeholders_and_params(monkeypatch):
    from app import search

    executed = []

    class FakeResult:
        def fetchall(self):
            return []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, query, params=()):
            if "SELECT" in query:
                executed.append((query, params))
            return FakeResult()

    class FakeEmbeddings:
        def check_query(self, text):
            pass

        def embed_query(self, text):
            return [0.0, 0.0, 0.0]

    model = FakeEmbeddings()
    monkeypatch.setattr(search, "get_connection", lambda user_id=None: FakeConnection())
    monkeypatch.setattr(search, "register_vector", lambda conn: None)

    search.search_documents(model, "toters finance report", 5, user_id=7)

    assert len(executed) == 3
    assert all(7 in params for _, params in executed)
    for query, params in executed:
        assert query.count("%s") == len(params)


def test_too_long_question_is_rejected_before_any_query(monkeypatch):
    from app import search

    class LongQuestion:
        def check_query(self, text):
            raise ValueError("Question exceeds the model's 128-token input limit.")

    monkeypatch.setattr(search, "get_connection", lambda user_id=None: pytest.fail("no DB"))
    with pytest.raises(ValueError, match="token input limit"):
        search.search_documents(LongQuestion(), "x " * 500, 3, user_id=1)


def test_retriever_returns_documents_of_the_users_search(monkeypatch):
    from threading import Lock

    from app import retrieval, search

    calls = []
    row = {
        "document_id": 3,
        "title": "handbook",
        "source_url": None,
        "chunk_id": 9,
        "chunk_number": 0,
        "text": "Travel costs are reimbursed.",
        "location": {"page_number": 2},
        "similarity": 0.8,
    }

    def fake_search(embeddings, question, top_k, user_id):
        calls.append((question, top_k, user_id, lock.locked()))
        return [row]

    lock = Lock()
    monkeypatch.setattr(search, "search_documents", fake_search)
    retriever = retrieval.HybridRetriever(embeddings=object(), user_id=7, top_k=4, lock=lock)

    documents = retriever.invoke("travel costs")

    assert calls == [("travel costs", 4, 7, True)]
    assert documents[0].page_content == "Travel costs are reimbursed."
    assert documents[0].metadata["chunk_id"] == 9
    assert retrieval.to_passages(documents) == [row]


def test_rag_chain_retrieves_then_answers(monkeypatch):
    from langchain_core.documents import Document
    from langchain_core.retrievers import BaseRetriever

    from app import rag

    class StaticRetriever(BaseRetriever):
        def _get_relevant_documents(self, query, *, run_manager):
            return [
                Document(
                    page_content="Receipts are required.",
                    metadata={
                        "document_id": 1,
                        "title": "t",
                        "source_url": None,
                        "chunk_id": 2,
                        "chunk_number": 0,
                        "location": {},
                        "similarity": 0.5,
                    },
                )
            ]

    seen = {}

    def fake_generate(question, passages):
        seen["args"] = (question, [p["text"] for p in passages])
        return {"answer": "Yes [S1].", "insufficient_evidence": False, "citations": []}

    monkeypatch.setattr(rag, "generate_answer", fake_generate)

    result = rag.answer_question(StaticRetriever(), "Receipts?")

    assert seen["args"] == ("Receipts?", ["Receipts are required."])
    assert result["retrieved_count"] == 1 and result["answer"] == "Yes [S1]."
