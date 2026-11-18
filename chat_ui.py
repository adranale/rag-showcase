import os
import time

import httpx
import streamlit as st
from dotenv import load_dotenv

from app.auth import AuthError, issue_user_token

load_dotenv()

API_BASE_URL = os.getenv(
    "RAG_API_BASE_URL",
    "http://127.0.0.1:8000",
).rstrip("/")
API_KEY = os.getenv("API_KEY")

st.set_page_config(
    page_title="Document Assistant",
    page_icon="📚",
)

if not st.user.is_logged_in:
    st.title("Document Assistant")
    st.write(
        "Ask questions about your own documents. Access is by invitation: "
        "sign in with the Microsoft account that was invited."
    )
    st.button("Sign in with Microsoft", on_click=st.login, args=["microsoft"])
    st.stop()

claims = {
    "oid": st.user.get("oid") or st.user.get("sub"),
    "tid": st.user.get("tid"),
    "email": st.user.get("email") or st.user.get("preferred_username"),
    "name": st.user.get("name"),
}


def api(method: str, path: str, **kwargs) -> httpx.Response:
    """Call the API as the signed-in user (short-lived signed identity token)."""
    headers = {"Authorization": f"Bearer {issue_user_token(claims)}"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    return httpx.request(
        method,
        f"{API_BASE_URL}{path}",
        headers=headers,
        timeout=kwargs.pop("timeout", httpx.Timeout(30.0, connect=5.0)),
        **kwargs,
    )


def error_text(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    return f"API error {response.status_code}: {detail or 'Request failed.'}"


def call(method: str, path: str, **kwargs):
    """(data, error message); errors are shown to the user, never raised."""
    try:
        response = api(method, path, **kwargs)
    except AuthError:
        return None, "The server is not configured for user sign-in."
    except httpx.RequestError:
        return None, "Could not reach the API, or the request timed out."
    if response.status_code >= 400:
        return None, error_text(response)
    try:
        return response.json(), None
    except ValueError:
        return None, "The API returned an unexpected response."


# Dropbox sends the browser back here after the user approved (or denied) access.
params = st.query_params
if "state" in params and ("code" in params or "error" in params):
    if "code" in params:
        _, problem = call(
            "POST",
            "/dropbox/callback",
            json={"code": params["code"], "state": params["state"]},
        )
        st.session_state.flash = (
            ("error", problem) if problem else ("success", "Dropbox connected.")
        )
    else:
        st.session_state.flash = ("error", "Dropbox access was not granted.")
    st.query_params.clear()
    st.session_state.page = "My documents"
    st.rerun()

st.title("Document Assistant")
st.caption(
    "Answers from your indexed documents, with source references. "
    "Each question is currently answered independently."
)

if "messages" not in st.session_state:
    st.session_state.messages = []

with st.sidebar:
    st.caption(f"Signed in as {claims['email'] or claims['name']}")
    st.button("Sign out", on_click=st.logout)
    page = st.radio("Page", ["Chat", "My documents"], key="page", label_visibility="collapsed")

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


def documents_page() -> None:
    st.subheader("My documents")
    st.caption(
        "Your documents are stored and searched only for your account. Read-only "
        "access to the Dropbox folder you choose; you can disconnect and delete "
        "everything at any time. The service operator has technical access to the "
        "database and could in principle read stored text."
    )
    if flash := st.session_state.pop("flash", None):
        (st.success if flash[0] == "success" else st.error)(flash[1])

    info, problem = call("GET", "/me")
    if problem:
        st.error(problem)
        return
    dropbox = info["dropbox"]

    if not dropbox["connected"]:
        st.write("Connect your Dropbox to build your own searchable document index.")
        if st.button("Connect Dropbox"):
            data, problem = call("POST", "/dropbox/authorize")
            if problem:
                st.error(problem)
            else:
                st.link_button("Continue to Dropbox", data["url"])
        return

    st.write(f"Dropbox connected. Folder: `{dropbox['folder'] or 'not chosen yet'}`")
    folder = st.text_input(
        "Dropbox folder", value=dropbox["folder"] or "", placeholder="/Documents/notes"
    )
    if st.button("Save folder"):
        _, problem = call("PUT", "/dropbox/folder", json={"folder": folder})
        if problem:
            st.error(problem)
        else:
            st.success("Folder saved.")
            st.rerun()

    st.write(
        f"Indexed documents: {dropbox['documents_indexed']} "
        f"(skipped or failed: {dropbox['documents_skipped']}). "
        f"Limit: {info['files_per_sync']} files per sync."
    )
    sync = info["sync"]
    if sync:
        line = f"Last sync: {sync['status']}"
        if sync.get("message"):
            line += f" - {sync['message']}"
        st.caption(line)

    left, right = st.columns(2)
    if left.button("Sync now", disabled=not dropbox["folder"]):
        _, problem = call("POST", "/sync")
        (st.error(problem) if problem else st.info("Sync started; refresh in a minute."))
    right.button("Refresh status")

    with st.expander("Disconnect or delete"):
        remove = st.checkbox("Also delete all my indexed documents")
        if st.button("Disconnect Dropbox"):
            _, problem = call("DELETE", "/dropbox", params={"delete_documents": remove})
            if problem:
                st.error(problem)
            else:
                st.rerun()
        if st.button("Delete all my indexed documents"):
            _, problem = call("DELETE", "/me/documents")
            (st.error(problem) if problem else st.success("All your documents were deleted."))


if page == "My documents":
    documents_page()
    st.stop()


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
            response = api(
                "POST",
                "/chat",
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
                "Check that FastAPI and the answer provider (Ollama / Azure OpenAI) are running."
            ),
            "error": True,
        }

    except AuthError:
        assistant_message = {
            "role": "assistant",
            "content": "The server is not configured for user sign-in.",
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