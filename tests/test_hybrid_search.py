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
    import numpy as np

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

    class FakeModel:
        max_seq_length = 128

        class tokenizer:
            @staticmethod
            def __call__(*args, **kwargs):
                return {"input_ids": [1, 2]}

        def encode(self, *args, **kwargs):
            return np.zeros(3)

    class FakeTokenizer:
        def __call__(self, *args, **kwargs):
            return {"input_ids": [1, 2]}

    model = FakeModel()
    model.tokenizer = FakeTokenizer()
    monkeypatch.setattr(search, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(search, "register_vector", lambda conn: None)

    search.search_documents(model, "toters finance report", 5)

    assert len(executed) == 3
    for query, params in executed:
        assert query.count("%s") == len(params)
