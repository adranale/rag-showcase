import httpx
import pytest

from ui_helpers import describe_location, group_citations, request_with_retry


def run(responses, method="GET", retries=3):
    """Feed the outcomes (response or exception) to request_with_retry."""
    outcomes = list(responses)
    notices, sleeps = [], []

    def send():
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    result = request_with_retry(
        send, method=method, notify=notices.append, sleep=sleeps.append, retries=retries, wait=7
    )
    return result, notices, sleeps, outcomes


def test_get_is_retried_after_timeouts_with_notice():
    result, notices, sleeps, left = run(
        [httpx.ReadTimeout("t"), httpx.Response(503), httpx.Response(200)]
    )
    assert result.status_code == 200
    assert sleeps == [7, 7] and left == []
    assert "starting up" in notices[0] and "1/3" in notices[0]


def test_post_is_not_retried_after_a_timeout_but_after_connect_errors():
    with pytest.raises(httpx.ReadTimeout):
        run([httpx.ReadTimeout("t"), httpx.Response(200)], method="POST")
    result, _, sleeps, _ = run([httpx.ConnectError("c"), httpx.Response(200)], method="POST")
    assert result.status_code == 200 and sleeps == [7]


def test_gives_up_after_the_last_attempt():
    result, _, sleeps, _ = run([httpx.Response(503)] * 3)
    assert result.status_code == 503 and len(sleeps) == 2
    with pytest.raises(httpx.ConnectError):
        run([httpx.ConnectError("c")] * 3)


def test_other_error_statuses_are_returned_immediately():
    result, notices, _, left = run([httpx.Response(502), httpx.Response(200)])
    assert result.status_code == 502 and notices == [] and len(left) == 1


def test_group_citations_and_location_text():
    citations = [
        {"id": "S1", "document_id": 7, "title": "A", "source_url": "u", "chunk_id": 1,
         "location": {"page_number": 1, "char_start": 0, "char_end": 9}},
        {"id": "S2", "document_id": 8, "title": "B", "chunk_id": 2, "location": {}},
        {"id": "S3", "document_id": 7, "title": "A", "source_url": "u", "chunk_id": 3,
         "location": {"page_number": 2}},
    ]
    groups = group_citations(citations)
    assert [g["document_id"] for g in groups] == [7, 8]
    assert [p["id"] for p in groups[0]["passages"]] == ["S1", "S3"]
    assert groups[1]["source_url"] is None
    assert describe_location(citations[0]["location"]) == "page 1, characters 0-9"
    assert describe_location({}) == "location unknown"
