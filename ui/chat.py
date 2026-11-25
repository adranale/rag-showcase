import html
import logging
import os
import time

import httpx
import streamlit as st
from dotenv import load_dotenv

from app.auth import AuthError, issue_user_token
from ui.helpers import (
    INVITE_COOKIE,
    PROVIDERS,
    SUBMIT_MESSAGES,
    describe_location,
    extract_invite_code,
    group_citations,
    invite_cookie_script,
    invite_link,
    provider_label,
    request_with_retry,
)

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
    # Streamlit only provides login state when [auth] is configured (ui/start.py writes it).
    logging.getLogger(__name__).error(
        "Login is not configured: start the UI with python -m ui.start and set "
        "AUTH_REDIRECT_URI, AUTH_COOKIE_SECRET, MS_CLIENT_ID and MS_CLIENT_SECRET."
    )
    st.error("Sign-in is temporarily unavailable. Please contact the administrator.")
    st.stop()

invite_code = extract_invite_code(st.query_params.get("invite"))

if not st.user.is_logged_in:
    st.title("Document Assistant")
    if invite_code:
        # Kept in a short-lived cookie: the sign-in redirect drops the URL parameter.
        st.html(invite_cookie_script(invite_code), unsafe_allow_javascript=True)
        st.write(
            "You have been invited to the Document Assistant. Sign in with your personal "
            "Microsoft account (e.g. outlook.com, hotmail.com or live.com), then send "
            "your access request."
        )
    else:
        st.write(
            "Ask questions about your own documents. Access is by invitation: "
            "sign in with the Microsoft account that was invited, or open the invite "
            "link you received."
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


def api_retry(method: str, path: str, **kwargs) -> httpx.Response:
    """Like api(), but shows a notice and waits while the API/database starts."""
    notice = st.empty()
    try:
        return request_with_retry(
            lambda: api(method, path, **kwargs), method=method, notify=notice.info
        )
    finally:
        notice.empty()


def error_text(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    return f"API error {response.status_code}: {detail or 'Request failed.'}"


def call_with_status(method: str, path: str, **kwargs):
    """(HTTP status or None, data, error message); never raises."""
    try:
        response = api_retry(method, path, **kwargs)
    except AuthError:
        return None, None, "The server is not configured for user sign-in."
    except httpx.RequestError:
        return None, None, "Could not reach the API, or the request timed out."
    if response.status_code >= 400:
        return response.status_code, None, error_text(response)
    try:
        return response.status_code, response.json(), None
    except ValueError:
        return response.status_code, None, "The API returned an unexpected response."


def call(method: str, path: str, **kwargs):
    """(data, error message); errors are shown to the user, never raised."""
    _, data, problem = call_with_status(method, path, **kwargs)
    return data, problem


def format_time(value: str | None) -> str:
    return value[:16].replace("T", " ") if value else "never"


def access_request_page() -> None:
    """Signed in, but not (yet) a user: request access with the invite link."""
    st.title("Document Assistant")
    st.caption(f"Signed in as {claims['email'] or claims['name']}")
    st.button("Sign out", on_click=st.logout)

    status, problem = call("GET", "/access-request")
    if problem:
        st.error(problem)
        return
    if status["account_status"] == "disabled":
        st.error("Your account is disabled. Please contact the administrator.")
        return
    if status["request_status"] == "pending":
        st.info(
            f"Your access request from {format_time(status['requested_at'])} is waiting "
            "for approval. Come back later; you can close this page."
        )
        return
    if status["request_status"] == "refused" and status["decided_at"]:
        st.warning(
            "Your last access request was declined. You can send a new one a week "
            "after the decision."
        )

    code = invite_code or extract_invite_code(st.context.cookies.get(INVITE_COOKIE))
    st.subheader("Request access")
    with st.form("access-request"):
        if code:
            st.caption("Invite link recognized.")
            pasted = ""
        else:
            pasted = st.text_input(
                "Invite link", help="Paste the complete invite link you received."
            )
        message = st.text_area(
            "Message to the administrator (optional)",
            max_chars=500,
            placeholder="Who are you? Why would you like to use the assistant?",
        )
        submitted = st.form_submit_button("Request access", type="primary")
    if not submitted:
        return
    code = code or extract_invite_code(pasted)
    if not code:
        st.error("Please paste the complete invite link you received.")
        return
    data, problem = call(
        "POST", "/access-request", json={"invite_code": code, "message": message or None}
    )
    if problem:
        st.error(problem)
        return
    kind, text = SUBMIT_MESSAGES.get(data["result"], ("error", "Unexpected answer."))
    getattr(st, kind)(text)
    if data["result"] in ("created", "pending", "already_user"):
        st.html(invite_cookie_script(None), unsafe_allow_javascript=True)


def load_account(refresh: bool = False) -> dict:
    """The signed-in user's account (/me), cached per session; stops for non-users."""
    if refresh or "account" not in st.session_state:
        status, data, problem = call_with_status("GET", "/me")
        if status == 403:
            access_request_page()
            st.stop()
        if problem:
            st.title("Document Assistant")
            st.error(problem)
            st.button("Retry")
            st.stop()
        st.session_state.account = data
    return st.session_state.account


account = load_account()


# Dropbox / Google send the browser back here after the user approved (or denied)
# access; the API knows the provider from the state.
params = st.query_params
if "state" in params and ("code" in params or "error" in params):
    if "code" in params:
        data, problem = call(
            "POST",
            "/storage/callback",
            json={"code": params["code"], "state": params["state"]},
        )
        st.session_state.flash = (
            ("error", problem)
            if problem
            else ("success", f"{provider_label(data['provider'])} connected.")
        )
    else:
        st.session_state.flash = ("error", "Access to your storage was not granted.")
    st.session_state.pop("confirm_connect", None)
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
    pages = ["Chat", "My documents"] + (["Admin"] if account.get("is_admin") else [])
    pending = account.get("pending_requests") or 0
    page = st.radio(
        "Page",
        pages,
        key="page",
        label_visibility="collapsed",
        format_func=lambda name: f"Admin ({pending} open)" if name == "Admin" and pending
        else name,
    )

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
        "access to the one Dropbox or Google Drive folder you choose; you can "
        "disconnect and delete everything at any time. The service operator has "
        "technical access to the database and could in principle read stored text."
    )
    if flash := st.session_state.pop("flash", None):
        (st.success if flash[0] == "success" else st.error)(flash[1])

    info, problem = call("GET", "/me")
    if problem:
        st.error(problem)
        return
    st.session_state.account = info
    storage = info["storage"]

    st.markdown("#### Your index")
    if storage["documents_indexed"] or storage["documents_skipped"]:
        first, second, third = st.columns(3)
        first.metric("Indexed files", storage["documents_indexed"])
        second.metric("Searchable passages", storage["chunks"])
        third.metric("Skipped or failed", storage["documents_skipped"])
        if storage["last_indexed_at"]:
            st.caption(f"Last file indexed: {format_time(storage['last_indexed_at'])}")
        for source in storage["sources"]:
            st.write(
                f"{provider_label(source['provider'])} folder `{source['folder']}`: "
                f"{source['documents_indexed']} files indexed, last sync "
                f"{format_time(source['last_sync'])}."
            )
    else:
        st.write("Nothing is indexed yet.")

    st.markdown("#### Storage connection")
    available = storage["available"]
    if not storage["connected"]:
        if storage["sources"]:
            st.info(
                "These documents were imported before you connected your storage. "
                "They stay searchable, but nothing is synced automatically. Connect the "
                "same provider and choose the same folder to keep them up to date."
            )
        else:
            st.write(
                "Connect Dropbox or Google Drive and choose one folder to build your "
                "own searchable document index."
            )
        if not available:
            st.warning("No storage provider is configured on the server.")
        else:
            provider = st.radio(
                "Where are your documents?",
                available,
                format_func=provider_label,
                horizontal=True,
            )
            connect_controls(provider)
        remove_data_section(None)
        return

    provider = storage["provider"]
    label = provider_label(provider)
    account = f" as {storage['account']}" if storage["account"] else ""
    st.write(
        f"Connected to {label}{account}. "
        f"Folder: `{storage['folder_name'] or 'not chosen yet'}`"
    )
    folder_controls(provider, storage)

    st.caption(f"Limit: {info['files_per_sync']} files per sync.")
    sync = info["sync"]
    if sync:
        line = f"Last sync: {sync['status']}"
        if sync.get("message"):
            line += f" - {sync['message']}"
        st.caption(line)

    left, right = st.columns(2)
    if left.button("Sync now", disabled=not storage["folder"]):
        _, problem = call("POST", "/sync")
        (st.error(problem) if problem else st.info("Sync started; refresh in a minute."))
    right.button("Refresh status")

    for other in (name for name in available if name != provider):
        with st.expander(f"Use {provider_label(other)} instead"):
            st.write(
                f"Your {label} connection is replaced and the documents indexed from "
                f"{label} are deleted. You can choose one folder in "
                f"{provider_label(other)} afterwards."
            )
            connect_controls(other)
    remove_data_section(provider)


def connect_controls(provider: str) -> None:
    """Connect button; switching providers asks before documents are deleted."""
    label = provider_label(provider)
    pending = st.session_state.get("confirm_connect")
    if pending and pending["provider"] == provider:
        st.warning(pending["message"])
        confirmed = st.checkbox(
            "Yes, delete my indexed documents", key=f"confirm-connect-{provider}"
        )
        if not st.button(
            f"Delete and connect {label}", disabled=not confirmed, key=f"go-{provider}"
        ):
            return
        status, data, problem = call_with_status(
            "POST", f"/storage/{provider}/authorize", json={"confirm_delete": True}
        )
    elif st.button(f"Connect {label}", key=f"connect-{provider}"):
        status, data, problem = call_with_status(
            "POST", f"/storage/{provider}/authorize", json={}
        )
        if status == 409:
            st.session_state.confirm_connect = {"provider": provider, "message": problem}
            st.rerun()
    else:
        return
    if problem:
        st.error(problem)
        return
    st.session_state.pop("confirm_connect", None)
    st.link_button(f"Continue to {label}", data["url"])


def folder_controls(provider: str, storage: dict) -> None:
    """Folder input; changing the folder asks before documents are deleted."""
    texts = PROVIDERS[provider]
    current = storage["folder"] if provider == "dropbox" else ""
    folder = st.text_input(
        texts["folder_label"],
        value=current or "",
        placeholder=texts["placeholder"],
        help=None
        if provider == "dropbox"
        else "Open the folder in Google Drive and copy the link from the address bar.",
    )
    pending = st.session_state.get("confirm_folder")
    confirm = False
    if pending and pending["folder"] == folder:
        st.warning(pending["message"])
        confirm = st.checkbox("Yes, delete my indexed documents", key="confirm-folder")
        clicked = st.button("Delete and change folder", disabled=not confirm)
    else:
        clicked = st.button("Save folder")
    if not clicked:
        return
    status, data, problem = call_with_status(
        "PUT", "/storage/folder", json={"folder": folder, "confirm_delete": confirm}
    )
    if status == 409:
        st.session_state.confirm_folder = {"folder": folder, "message": problem}
        st.rerun()
    if problem:
        st.error(problem)
        return
    st.session_state.pop("confirm_folder", None)
    deleted = data["documents_deleted"]
    st.session_state.flash = (
        "success",
        f"Folder saved: {data['folder_name']}."
        + (f" {deleted} documents of the previous folder were deleted." if deleted else ""),
    )
    st.rerun()


def remove_data_section(provider: str | None) -> None:
    st.markdown("#### Remove data")
    title = (
        f"Disconnect {provider_label(provider)} or delete my documents"
        if provider
        else "Delete my documents"
    )
    with st.expander(title):
        if provider:
            remove = st.checkbox("Also delete all my indexed documents")
            if st.button(f"Disconnect {provider_label(provider)}"):
                _, problem = call(
                    "DELETE", "/storage", params={"delete_documents": remove}
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


def admin_done(kind: str, text: str) -> None:
    """Show the outcome after the rerun and refresh the open-request count."""
    st.session_state.flash = (kind, text)
    load_account(refresh=True)
    st.rerun()


def admin_page() -> None:
    st.subheader("Admin")
    if flash := st.session_state.pop("flash", None):
        getattr(st, flash[0])(flash[1])

    st.markdown("#### Invite link")
    invite, problem = call("GET", "/admin/invite")
    if problem:
        st.error(problem)
    else:
        st.code(invite_link(os.getenv("AUTH_REDIRECT_URI", ""), invite["code"]), language=None)
        st.caption(
            "Send this link to people you want to invite. They sign in with a personal "
            "Microsoft account and send a request, which you approve or refuse below "
            "(you get an e-mail when e-mail notifications are configured). Anyone with "
            "the link can send a request."
        )
        with st.expander("Generate a new link"):
            confirmed = st.checkbox("The current link stops working")
            if st.button("Generate new link", disabled=not confirmed):
                _, problem = call("POST", "/admin/invite")
                if problem:
                    st.error(problem)
                else:
                    admin_done("success", "New invite link generated.")

    st.markdown("#### Access requests")
    requests, problem = call("GET", "/admin/requests", params={"pending_only": True})
    if problem:
        st.error(problem)
    elif not requests:
        st.write("No open requests.")
    for request in requests or []:
        with st.container(border=True):
            st.markdown(f"**{request['display_name'] or request['email']}** · {request['email']}")
            st.caption(
                f"Requested {format_time(request['created_at'])} · "
                + (
                    "personal Microsoft account"
                    if request["personal_account"]
                    else "work or school account (e-mail address not verified)"
                )
            )
            if request["message"]:
                st.text(request["message"])
            approve, refuse = st.columns(2)
            for column, decision, label in (
                (approve, "approve", "Approve"),
                (refuse, "refuse", "Refuse"),
            ):
                if column.button(
                    label,
                    key=f"{decision}-{request['id']}",
                    type="primary" if decision == "approve" else "secondary",
                ):
                    data, problem = call(
                        "POST", f"/admin/requests/{request['id']}/{decision}"
                    )
                    if problem:
                        st.error(problem)
                    else:
                        admin_done(
                            "success",
                            f"{request['email']}: "
                            + ("approved" if data["result"] == "approved" else "refused"),
                        )
    with st.expander("Decided requests"):
        decided, problem = call("GET", "/admin/requests", params={"pending_only": False})
        rows = [r for r in decided or [] if r["status"] != "pending"]
        if problem:
            st.error(problem)
        elif not rows:
            st.write("None yet.")
        else:
            st.dataframe(
                [
                    {
                        "E-mail": r["email"],
                        "Name": r["display_name"] or "",
                        "Decision": r["status"],
                        "Decided": format_time(r["decided_at"]),
                    }
                    for r in rows
                ],
                hide_index=True,
            )

    st.markdown("#### Users")
    people, problem = call("GET", "/admin/users")
    if problem:
        st.error(problem)
        return
    st.dataframe(
        [
            {
                "E-mail": person["email"],
                "Name": person["display_name"] or "",
                "Status": person["status"] + (" (admin)" if person["is_admin"] else ""),
                "Documents": person["documents"],
                "Last login": format_time(person["last_login_at"]),
            }
            for person in people
        ],
        hide_index=True,
    )
    others = [person for person in people if person["email"] != account["email"]]
    if not others:
        return
    chosen = st.selectbox(
        "Change a user",
        others,
        format_func=lambda person: f"{person['email']} ({person['status']})",
    )
    enable, disable = st.columns(2)
    for column, status, label in (
        (enable, "active", "Enable"),
        (disable, "disabled", "Disable"),
    ):
        if column.button(label, key=f"user-{status}"):
            data, problem = call(
                "PUT", f"/admin/users/{chosen['id']}/status", json={"status": status}
            )
            if problem:
                st.error(problem)
            else:
                admin_done("success", f"{chosen['email']}: {data['status']}")


if page == "Admin":
    admin_page()
    st.stop()

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