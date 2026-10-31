import json

import httpx
import pytest

from app.generation import GenerationError, generate_answer


@pytest.fixture
def passages():
    return [
        {
            "document_id": 1,
            "chunk_id": 4,
            "title": "volunteer-handbook",
            "source_url": None,
            "location": {"paragraph": 4},
            "text": (
                "Travel expenses can be reimbursed with prior approval "
                "and a valid receipt."
            ),
        }
    ]


def mock_ollama(monkeypatch, output):
    def fake_post(url, **kwargs):
        request = httpx.Request("POST", url)

        return httpx.Response(
            status_code=200,
            request=request,
            json={
                "message": {
                    "role": "assistant",
                    "content": json.dumps(output),
                }
            },
        )

    monkeypatch.setattr("app.generation.httpx.post", fake_post)


def test_supported_answer_without_inline_citation(monkeypatch, passages):
    mock_ollama(
        monkeypatch,
        {
            "answer": "Yes, with prior approval and a valid receipt.",
            "citation_ids": ["S1"],
            "insufficient_evidence": False,
        },
    )

    result = generate_answer("Can volunteers claim travel costs?", passages)

    assert result["insufficient_evidence"] is False
    assert len(result["citations"]) == 1
    assert result["citations"][0]["id"] == "S1"
    assert result["citations"][0]["chunk_id"] == 4


def test_supported_answer_with_inline_citation(monkeypatch, passages):
    mock_ollama(
        monkeypatch,
        {
            "answer": "Yes, with prior approval and a valid receipt [S1].",
            "citation_ids": ["S1"],
            "insufficient_evidence": False,
        },
    )

    result = generate_answer("Can volunteers claim travel costs?", passages)

    assert result["citations"][0]["id"] == "S1"


@pytest.mark.parametrize(
    "answer,citation_ids",
    [
        ("Yes, with prior approval.", []),
        ("Yes, with prior approval.", ["S99"]),
        ("Yes, with prior approval [S2].", ["S1"]),
    ],
)
def test_invalid_citations_are_rejected(
    monkeypatch, passages, answer, citation_ids
):
    mock_ollama(
        monkeypatch,
        {
            "answer": answer,
            "citation_ids": citation_ids,
            "insufficient_evidence": False,
        },
    )

    with pytest.raises(GenerationError, match="Citation validation failed"):
        generate_answer("Can volunteers claim travel costs?", passages)


def test_insufficient_evidence(monkeypatch, passages):
    mock_ollama(
        monkeypatch,
        {
            "answer": "The password is not provided in the sources.",
            "citation_ids": [],
            "insufficient_evidence": True,
        },
    )

    result = generate_answer("What is the Wi-Fi password?", passages)

    assert result["insufficient_evidence"] is True
    assert result["citations"] == []


def test_insufficient_evidence_with_empty_answer_and_extra_key(
    monkeypatch, passages
):
    mock_ollama(
        monkeypatch,
        {
            "answer": "",
            "explanation": "The sources do not mention this.",
            "citation_ids": [],
            "insufficient_evidence": True,
        },
    )

    result = generate_answer("Unrelated question?", passages)

    assert result["insufficient_evidence"] is True
    assert result["citations"] == []


def test_supported_answer_must_not_be_empty(monkeypatch, passages):
    mock_ollama(
        monkeypatch,
        {
            "answer": "",
            "citation_ids": ["S1"],
            "insufficient_evidence": False,
        },
    )

    with pytest.raises(GenerationError):
        generate_answer("Can volunteers claim travel costs?", passages)


def test_no_passages_does_not_call_ollama(monkeypatch):
    def unexpected_call(*args, **kwargs):
        pytest.fail("Ollama must not be called when no passages exist.")

    monkeypatch.setattr("app.generation.httpx.post", unexpected_call)

    result = generate_answer("What is the Wi-Fi password?", [])

    assert result["insufficient_evidence"] is True
    assert result["citations"] == []


def test_ollama_timeout(monkeypatch, passages):
    def fake_post(url, **kwargs):
        raise httpx.ReadTimeout(
            "Simulated timeout",
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("app.generation.httpx.post", fake_post)

    with pytest.raises(GenerationError):
        generate_answer("Can volunteers claim travel costs?", passages)


def test_invalid_model_output(monkeypatch, passages):
    mock_ollama(
        monkeypatch,
        {
            "answer": "An answer with required fields missing."
        },
    )

    with pytest.raises(GenerationError):
        generate_answer("Can volunteers claim travel costs?", passages)


def test_azure_openai_provider(monkeypatch, passages):
    monkeypatch.setenv("GENERATION_PROVIDER", "azure_openai")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/"
    )
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        return httpx.Response(
            status_code=200,
            request=httpx.Request("POST", url),
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "answer": "Yes, with a receipt.",
                                    "citation_ids": ["S1"],
                                    "insufficient_evidence": False,
                                }
                            )
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr("app.generation.httpx.post", fake_post)

    result = generate_answer("Can volunteers claim travel costs?", passages)

    assert captured["url"].startswith(
        "https://example.openai.azure.com/openai/deployments/gpt-4o-mini/"
    )
    assert captured["headers"] == {"api-key": "test-key"}
    assert result["citations"][0]["id"] == "S1"


def test_azure_openai_requires_configuration(monkeypatch, passages):
    monkeypatch.setenv("GENERATION_PROVIDER", "azure_openai")
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)

    with pytest.raises(GenerationError, match="must be configured"):
        generate_answer("Can volunteers claim travel costs?", passages)


def test_unknown_provider(monkeypatch, passages):
    monkeypatch.setenv("GENERATION_PROVIDER", "nope")

    with pytest.raises(GenerationError, match="Unknown"):
        generate_answer("Can volunteers claim travel costs?", passages)