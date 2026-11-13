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
