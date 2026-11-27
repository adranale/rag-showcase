"""The embedding model as a LangChain `Embeddings` object.

`ModelEmbeddings` is LangChain's HuggingFaceEmbeddings (sentence-transformers) with
the project's settings, plus the token checks the model's input limit needs.
LangChain replaces newlines with spaces before embedding; this model's tokenizer
treats both the same, so vectors match the ones stored before the switch.
"""

from langchain_huggingface import HuggingFaceEmbeddings

from app.config import MODEL_NAME

BATCH_SIZE = 16


class ModelEmbeddings(HuggingFaceEmbeddings):
    @property
    def max_seq_length(self) -> int:
        return self._client.max_seq_length

    @property
    def dimension(self) -> int:
        return self._client.get_embedding_dimension()

    def token_count(self, text: str) -> int:
        """Tokens of `text` including the model's special tokens."""
        return len(
            self._client.tokenizer(text, truncation=False, add_special_tokens=True)[
                "input_ids"
            ]
        )

    def check_query(self, text: str) -> None:
        if self.token_count(text) > self.max_seq_length:
            raise ValueError(
                f"Question exceeds the model's {self.max_seq_length}-token input limit."
            )


def load_embeddings() -> ModelEmbeddings:
    return ModelEmbeddings(
        model_name=MODEL_NAME,
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True, "batch_size": BATCH_SIZE},
    )
