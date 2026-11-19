"""Access requests through the shared invite link, and admin operations.

Everything goes through the SECURITY DEFINER functions of
sql/006_access_requests.sql: the runtime role has no direct access to the request
table, and the admin functions check that the caller (app.user_id) is an admin.
"""

import re
import secrets

from app.db import get_connection
from app.users import User

INVITE_CODE_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}")
MAX_MESSAGE_LENGTH = 500

SUBMIT_RESULTS = (
    "created",
    "pending",
    "already_user",
    "invalid_code",
    "refused_recently",
    "too_many",
)


def new_invite_code() -> str:
    return secrets.token_urlsafe(24)


def _identity(claims: dict) -> tuple[str, str]:
    return (claims.get("tid") or "").strip().lower(), (claims.get("oid") or "").strip()


def submit_request(claims: dict, code: str, message: str | None) -> str:
    """One of SUBMIT_RESULTS."""
    if not INVITE_CODE_PATTERN.fullmatch(code or ""):
        return "invalid_code"
    tid, oid = _identity(claims)
    with get_connection() as conn:
        return conn.execute(
            "SELECT submit_access_request(%s, %s, %s, %s, %s, %s);",
            (
                code,
                tid,
                oid,
                claims.get("email") or "",
                claims.get("name") or "",
                (message or "")[:MAX_MESSAGE_LENGTH],
            ),
        ).fetchone()[0]


def request_status(claims: dict) -> dict:
    tid, oid = _identity(claims)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM access_request_status(%s, %s);", (tid, oid)
        ).fetchone()
    return {
        "request_status": row[0],
        "requested_at": row[1],
        "decided_at": row[2],
        "account_status": row[3],
    }


def list_requests(admin: User, pending_only: bool) -> list[dict]:
    with get_connection(admin.id) as conn:
        rows = conn.execute(
            "SELECT * FROM admin_list_requests(%s);", (pending_only,)
        ).fetchall()
    keys = ("id", "email", "display_name", "personal_account", "message", "status",
            "created_at", "decided_at")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def pending_count(admin: User) -> int:
    with get_connection(admin.id) as conn:
        return conn.execute("SELECT admin_pending_count();").fetchone()[0]


def decide(admin: User, request_id: int, approve: bool) -> str:
    """approved | refused | not_pending | email_in_use"""
    with get_connection(admin.id) as conn:
        return conn.execute(
            "SELECT admin_decide_request(%s, %s);", (request_id, approve)
        ).fetchone()[0]


def list_users(admin: User) -> list[dict]:
    with get_connection(admin.id) as conn:
        rows = conn.execute("SELECT * FROM admin_list_users();").fetchall()
    keys = ("id", "email", "display_name", "status", "is_admin", "created_at",
            "last_login_at", "documents")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def set_user_status(admin: User, user_id: int, status: str) -> str:
    """active | invited | disabled | not_found | self"""
    with get_connection(admin.id) as conn:
        return conn.execute(
            "SELECT admin_set_user_status(%s, %s);", (user_id, status)
        ).fetchone()[0]


def invite_code(admin: User) -> str:
    """The current invite code; the first call creates one."""
    with get_connection(admin.id) as conn:
        code = conn.execute("SELECT admin_get_invite_code();").fetchone()[0]
        if code is None:
            code = new_invite_code()
            conn.execute("SELECT admin_set_invite_code(%s);", (code,))
    return code


def rotate_invite_code(admin: User) -> str:
    """Replace the invite code; links with the old code stop working."""
    code = new_invite_code()
    with get_connection(admin.id) as conn:
        conn.execute("SELECT admin_set_invite_code(%s);", (code,))
    return code
