"""The retrieval-augmented answer pipeline as one LangChain (LCEL) chain.

question -> HybridRetriever (user-scoped hybrid search) -> passages
         -> generate_answer (prompt | chat model | parser, citation check)

Errors keep their types, so the API maps them as before: ValueError (question too
long) -> 422, database errors -> 503, GenerationError -> 502.
"""

from operator import itemgetter

from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import Runnable, RunnableLambda, RunnablePassthrough

from app.generation import generate_answer
from app.retrieval import to_passages


def _answer(inputs: dict) -> dict:
    passages = inputs["passages"]
    return {
        "retrieved_count": len(passages),
        **generate_answer(inputs["question"], passages),
    }


def build_rag_chain(retriever: BaseRetriever) -> Runnable:
    """question (str) -> {"retrieved_count", "answer", "insufficient_evidence", "citations"}."""
    return (
        RunnablePassthrough.assign(
            passages=itemgetter("question") | retriever | RunnableLambda(to_passages)
        )
        | RunnableLambda(_answer)
    ).with_types(input_type=dict, output_type=dict)


def answer_question(retriever: BaseRetriever, question: str) -> dict:
    return build_rag_chain(retriever).invoke({"question": question})
