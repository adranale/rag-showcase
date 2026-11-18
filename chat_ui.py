import html
import logging
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

if not hasattr(st.user, "is_logged_in"):
    # Streamlit only provides login state when [auth] is configured (ui_start.py writes it).
    logging.getLogger(__name__).error(
        "Login is not configured: start the UI with ui_start.py and set "
        "AUTH_REDIRECT_URI, AUTH_COOKIE_SECRET, MS_CLIENT_ID and MS_CLIENT_SECRET."
    )
    st.error("Sign-in is temporarily unavailable. Please contact the administrator.")
    st.stop()

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


STARTUP_RETRIES = 6
STARTUP_WAIT_SECONDS = 10


def api_retry(method: str, path: str, **kwargs) -> httpx.Response:
    """Like api(), but waits for a scaled-to-zero API / stopped database to start.

    Retries only when the request cannot have been processed (connection failure or
    503); GET requests are also retried after a timeout.
    """
    notice = st.empty()
    try:
        for attempt in range(1, STARTUP_RETRIES + 1):
            last = attempt == STARTUP_RETRIES
            try:
                response = api(method, path, **kwargs)
                if response.status_code != 503 or last:
                    return response
            except httpx.RequestError as exc:
                retryable = method == "GET" or isinstance(
                    exc, (httpx.ConnectError, httpx.ConnectTimeout)
                )
                if not retryable or last:
                    raise
            notice.info(
                f"The service is starting up, retrying... (attempt {attempt}/{STARTUP_RETRIES})"
            )
            time.sleep(STARTUP_WAIT_SECONDS)
    finally:
        notice.empty()


def error_text(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    return f"API error {response.status_code}: {detail or 'Request failed.'}"


def call(method: str, path: str, **kwargs):
    """(data, error message); errors are shown to the user, never raised."""
    try:
        response = api_retry(method, path, **kwargs)
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


def format_time(value: str | None) -> str:
    return value[:16].replace("T", " ") if value else "never"


def describe_location(location: dict) -> str:
    parts = []
    if location.get("page_number"):
        parts.append(f"page {location['page_number']}")
    if "char_start" in location and "char_end" in location:
        parts.append(f"characters {location['char_start']}-{location['char_end']}")
    return ", ".join(parts) or "location unknown"


def group_citations(citations: list[dict]) -> list[dict]:
    """One entry per document, with all its cited passages in answer order."""
    groups: dict[int, dict] = {}
    for citation in citations:
        group = groups.setdefault(
            citation["document_id"],
            {
                "document_id": citation["document_id"],
                "title": citation["title"],
                "source_url": citation.get("source_url"),
                "passages": [],
            },
        )
        group["passages"].append(citation)
    return list(groups.values())


@st.dialog("Document passages", width="large")
def show_document(document_id: int, title: str, cited_chunk_ids: list[int]) -> None:
    data, problem = call("GET", f"/documents/{document_id}/chunks")
    if problem:
        st.error(problem)
        return
    st.subheader(title)
    st.caption(
        "Extracted text, split into passages (neighbouring passages can overlap "
        "slightly). Highlighted passages were used for the answer."
    )
    whole = st.toggle("Show the whole document", value=False)
    chunks = data["chunks"]
    cited = set(cited_chunk_ids)
    near = set()
    for position, chunk in enumerate(chunks):
        if chunk["chunk_id"] in cited:
            near.update({position - 1, position, position + 1})
    for position, chunk in enumerate(chunks):
        if not whole and position not in near:
            continue
        highlighted = chunk["chunk_id"] in cited
        st.caption(f"{describe_location(chunk['location'])}")
        background = "rgba(255, 193, 7, 0.25)" if highlighted else "transparent"
        st.html(
            f'<div style="white-space: pre-wrap; padding: 0.5rem; border-radius: 0.4rem; '
            f'background: {background};">{html.escape(chunk["text"])}</div>'
        )
    if len(chunks) >= 500:
        st.caption("Only the first 500 passages are shown.")


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

    st.markdown("#### Your index")
    if dropbox["documents_indexed"] or dropbox["documents_skipped"]:
        first, second, third = st.columns(3)
        first.metric("Indexed files", dropbox["documents_indexed"])
        second.metric("Searchable passages", dropbox["chunks"])
        third.metric("Skipped or failed", dropbox["documents_skipped"])
        if dropbox["last_indexed_at"]:
            st.caption(f"Last file indexed: {format_time(dropbox['last_indexed_at'])}")
        for source in dropbox["sources"]:
            st.write(
                f"Dropbox folder `{source['folder']}`: {source['documents_indexed']} "
                f"files indexed, last sync {format_time(source['last_sync'])}."
            )
    else:
        st.write("Nothing is indexed yet.")

    st.markdown("#### Dropbox connection")
    if not dropbox["connected"]:
        if dropbox["sources"]:
            st.info(
                "These documents were imported before your own Dropbox connection "
                "existed. They stay searchable, but nothing is synced automatically. "
                "Connect your Dropbox and choose the same folder to keep them up to date."
            )
        else:
            st.write("Connect your Dropbox to build your own searchable document index.")
        if st.button("Connect Dropbox"):
            data, problem = call("POST", "/dropbox/authorize")
            if problem:
                st.error(problem)
            else:
                st.link_button("Continue to Dropbox", data["url"])
        remove_data_section(connected=False)
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

    st.caption(f"Limit: {info['files_per_sync']} files per sync.")
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
    remove_data_section(connected=True)


def remove_data_section(connected: bool) -> None:
    st.markdown("#### Remove data")
    with st.expander("Disconnect Dropbox or delete my documents"):
        if connected:
            remove = st.checkbox("Also delete all my indexed documents")
            if st.button("Disconnect Dropbox"):
                _, problem = call(
                    "DELETE", "/dropbox", params={"delete_documents": remove}
                )
                if problem:
                    st.error(problem)
                else:
                    st.rerun()
        confirmed = st.checkbox("I understand that deleting my documents cannot be undone")
        if st.button("Delete all my indexed documents", disabled=not confirmed):
            _, problem = call("DELETE", "/me/documents")
            if problem:
                st.error(problem)
            else:
                st.session_state.flash = ("success", "All your documents were deleted.")
                st.rerun()


if page == "My documents":
    documents_page()
    st.stop()


def display_message(message, index: int = 0):
    with st.chat_message(message["role"]):
        if message.get("error"):
            st.error(message["content"])
            return

        st.markdown(message["content"])

        if message.get("insufficient_evidence"):
            st.info("The retrieved documents did not provide enough evidence.")

        groups = group_citations(message.get("citations", []))

        if groups:
            with st.expander(f"Sources ({len(groups)} document{'s' if len(groups) != 1 else ''})"):
                for group in groups:
                    st.markdown(f"**{group['title']}**")
                    for passage in group["passages"]:
                        st.caption(
                            f"[{passage['id']}] {describe_location(passage.get('location') or {})}"
                        )
                        if passage.get("text"):
                            st.text(passage["text"][:300] + ("…" if len(passage["text"]) > 300 else ""))
                    open_view, open_link = st.columns(2)
                    if open_view.button(
                        "View passages in context",
                        key=f"view-{index}-{group['document_id']}",
                    ):
                        show_document(
                            group["document_id"],
                            group["title"],
                            [passage["chunk_id"] for passage in group["passages"]],
                        )
                    source_url = group["source_url"]
                    if source_url and source_url.startswith(("https://", "http://")):
                        open_link.link_button("Open original", source_url)

        if message.get("latency_seconds") is not None:
            st.caption(
                f"Response time: {message['latency_seconds']:.1f}s"
            )


for index, message in enumerate(st.session_state.messages):
    display_message(message, index)

if question := st.chat_input("Ask a question about your documents"):
    user_message = {
        "role": "user",
        "content": question,
    }
    st.session_state.messages.append(user_message)
    display_message(user_message, len(st.session_state.messages) - 1)

    started = time.perf_counter()

    try:
        with st.spinner("Searching documents and generating an answer..."):
            response = api_retry(
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
    display_message(assistant_message, len(st.session_state.messages) - 1)