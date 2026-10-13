import os
import time

import httpx
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

API_BASE_URL = os.getenv(
    "RAG_API_BASE_URL",
    "http://127.0.0.1:8000",
).rstrip("/")

st.set_page_config(
    page_title="Document Assistant",
    page_icon="📚",
)

st.title("Document Assistant")
st.caption(
    "Answers from your indexed documents, with source references. "
    "Each question is currently answered independently."
)

if "messages" not in st.session_state:
    st.session_state.messages = []

with st.sidebar:
    st.header("Settings")

    top_k = st.slider(
        "Retrieved passages",
        min_value=1,
        max_value=10,
        value=3,
    )

    if st.button("Clear conversation"):
        st.session_state.messages = []
        st.rerun()

    st.caption(f"API: {API_BASE_URL}")


def display_message(message):
    with st.chat_message(message["role"]):
        if message.get("error"):
            st.error(message["content"])
            return

        st.markdown(message["content"])

        if message.get("insufficient_evidence"):
            st.info("The retrieved documents did not provide enough evidence.")

        citations = message.get("citations", [])

        if citations:
            with st.expander(f"Sources ({len(citations)})"):
                for citation in citations:
                    st.write(
                        f"[{citation['id']}] {citation['title']}"
                    )
                    st.caption(
                        f"Document ID: {citation['document_id']} · "
                        f"Chunk ID: {citation['chunk_id']}"
                    )
                    st.json(citation.get("location", {}))

                    source_url = citation.get("source_url")
                    if source_url and source_url.startswith(
                        ("https://", "http://")
                    ):
                        st.link_button("Open document", source_url)

        if message.get("latency_seconds") is not None:
            st.caption(
                f"Response time: {message['latency_seconds']:.1f}s"
            )


for message in st.session_state.messages:
    display_message(message)

if question := st.chat_input("Ask a question about your documents"):
    user_message = {
        "role": "user",
        "content": question,
    }
    st.session_state.messages.append(user_message)
    display_message(user_message)

    started = time.perf_counter()

    try:
        with st.spinner("Searching documents and generating an answer..."):
            response = httpx.post(
                f"{API_BASE_URL}/chat",
                json={
                    "question": question,
                    "top_k": top_k,
                },
                timeout=httpx.Timeout(240.0, connect=5.0),
            )
            response.raise_for_status()
            data = response.json()

        assistant_message = {
            "role": "assistant",
            "content": data["answer"],
            "citations": data["citations"],
            "insufficient_evidence": data["insufficient_evidence"],
            "latency_seconds": time.perf_counter() - started,
        }

    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail")
        except ValueError:
            detail = None

        assistant_message = {
            "role": "assistant",
            "content": (
                f"API error {exc.response.status_code}: "
                f"{detail or 'Request failed.'}"
            ),
            "error": True,
        }

    except httpx.RequestError:
        assistant_message = {
            "role": "assistant",
            "content": (
                "Could not reach the API, or the request timed out. "
                "Check that FastAPI and Ollama are running."
            ),
            "error": True,
        }

    except (ValueError, KeyError, TypeError):
        assistant_message = {
            "role": "assistant",
            "content": "The API returned an unexpected response.",
            "error": True,
        }

    st.session_state.messages.append(assistant_message)
    display_message(assistant_message)