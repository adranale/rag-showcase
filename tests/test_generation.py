import json

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.runnables import RunnableLambda

from app import generation
from app.generation import GenerationError, generate_answer


@pytest.fixture
def passages():
    return [
        {
            "document_id": 1,
            "chunk_id": 4,
            "title": "volunteer-handbook",
            "source_url": None,
            "location": {"char_start": 0, "char_end": 74},
            "text": (
                "Travel expenses can be reimbursed with prior approval "
                "and a valid receipt."
            ),
        }
    ]


def mock_model(monkeypatch, output) -> list:
    """Replace the chat model by a fake returning `output`; returns the prompts seen."""
    prompts = []
    content = output if isinstance(output, str) else json.dumps(output)
    fake = FakeListChatModel(responses=[content])

    def record(messages):
        prompts.append(messages)
        return messages

    monkeypatch.setattr(
        generation, "get_chat_model", lambda: RunnableLambda(record) | fake
    )
    return prompts


def test_supported_answer_without_inline_citation(monkeypatch, passages):
    prompts = mock_model(
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

    # The system prompt is sent verbatim; the question and sources go as JSON.
    messages = prompts[0].to_messages()
    assert messages[0].type == "system" and "untrusted" in messages[0].content
    payload = json.loads(messages[1].content)
    assert payload["question"] == "Can volunteers claim travel costs?"
    assert payload["sources"][0]["id"] == "S1"


def test_supported_answer_with_inline_citation(monkeypatch, passages):
    mock_model(
        monkeypatch,
        {
            "answer": "Yes, with prior approval and a valid receipt [S1].",
            "citation_ids": ["S1"],
            "insufficient_evidence": False,
        },
    )

    result = generate_answer("Can volunteers claim travel costs?", passages)

    assert result["citations"][0]["id"] == "S1"


def test_json_in_a_markdown_fence_is_accepted(monkeypatch, passages):
    body = {"answer": "Yes.", "citation_ids": ["S1"], "insufficient_evidence": False}
    mock_model(monkeypatch, "```json\n" + json.dumps(body) + "\n```")

    assert generate_answer("Travel costs?", passages)["answer"] == "Yes."


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
    mock_model(
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
    mock_model(
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
    mock_model(
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
    mock_model(
        monkeypatch,
        {
            "answer": "",
            "citation_ids": ["S1"],
            "insufficient_evidence": False,
        },
    )

    with pytest.raises(GenerationError):
        generate_answer("Can volunteers claim travel costs?", passages)


def test_no_passages_does_not_call_the_model(monkeypatch):
    def unexpected_call():
        pytest.fail("The model must not be called when no passages exist.")

    monkeypatch.setattr(generation, "get_chat_model", unexpected_call)

    result = generate_answer("What is the Wi-Fi password?", [])

    assert result["insufficient_evidence"] is True
    assert result["citations"] == []


def test_model_timeout_becomes_generation_error(monkeypatch, passages):
    def timeout(messages):
        raise httpx.ReadTimeout("Simulated timeout")

    monkeypatch.setattr(generation, "get_chat_model", lambda: RunnableLambda(timeout))

    with pytest.raises(GenerationError, match="ReadTimeout"):
        generate_answer("Can volunteers claim travel costs?", passages)


@pytest.mark.parametrize(
    "output", [{"answer": "An answer with required fields missing."}, "not json at all"]
)
def test_invalid_model_output(monkeypatch, passages, output):
    mock_model(monkeypatch, output)

    with pytest.raises(GenerationError, match="Invalid model response"):
        generate_answer("Can volunteers claim travel costs?", passages)


def test_ollama_is_the_default_provider(monkeypatch):
    from langchain_ollama import ChatOllama

    monkeypatch.delenv("GENERATION_PROVIDER", raising=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434/")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:3b")

    model = generation.get_chat_model()

    assert isinstance(model, ChatOllama)
    assert model.model == "llama3.2:3b" and model.base_url == "http://ollama.test:11434"
    assert model.temperature == 0 and model.num_predict == 512
    assert model.format == generation.GeneratedAnswer.model_json_schema()


def test_azure_openai_provider(monkeypatch):
    from langchain_openai import AzureChatOpenAI

    monkeypatch.setenv("GENERATION_PROVIDER", "azure_openai")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("AZURE_OPENAI_API_VERSION", raising=False)

    model = generation.get_chat_model()

    assert model.kwargs == {"response_format": {"type": "json_object"}}
    chat = model.bound
    assert isinstance(chat, AzureChatOpenAI)
    assert chat.azure_endpoint == "https://example.openai.azure.com"
    assert chat.deployment_name == "gpt-4.1-mini"
    assert chat.openai_api_version == "2024-10-21"
    assert chat.openai_api_key.get_secret_value() == "test-key"
    assert chat.temperature == 0 and chat.max_tokens == 512


def test_azure_openai_requires_configuration(monkeypatch, passages):
    monkeypatch.setenv("GENERATION_PROVIDER", "azure_openai")
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)

    with pytest.raises(GenerationError, match="must be configured"):
        generate_answer("Can volunteers claim travel costs?", passages)


def test_unknown_provider(monkeypatch, passages):
    monkeypatch.setenv("GENERATION_PROVIDER", "nope")

    with pytest.raises(GenerationError, match="Unknown"):
        generate_answer("Can volunteers claim travel costs?", passages)
