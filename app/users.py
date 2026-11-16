"""Allowlist of users and first-login binding to a Microsoft identity."""

from dataclasses import dataclass

from app.db import get_connection

# Tenant ID of personal Microsoft accounts (outlook.com, live.com, ...). For them
# Microsoft verifies the e-mail address. In work/school tenants the e-mail claim is
# set by the tenant admin and must not be trusted, so those users are invited with
# their immutable tenant/object ID instead (manage_users.py invite --tid --oid).
PERSONAL_TENANT_ID = "9188040d-6c67-4c5b-b112-36a304b66dad"

_COLUMNS = "id, email, status, chats_per_day, files_per_sync"


@dataclass(frozen=True)
class User:
    id: int
    email: str
    status: str
    chats_per_day: int
    files_per_sync: int


def authenticate(claims: dict) -> User | None:
    """Return the enabled user for verified identity claims, or None."""
    oid = (claims.get("oid") or "").strip()
    tid = (claims.get("tid") or "").strip().lower()
    email = (claims.get("email") or "").strip().lower()
    if not oid or not tid:
        return None

    with get_connection() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM users WHERE entra_tid = %s AND entra_oid = %s;",
            (tid, oid),
        ).fetchone()
        if row is not None:
            return User(*row) if row[2] == "active" else None

        if tid != PERSONAL_TENANT_ID or not email:
            return None

        # First login of an invited personal account: bind the identity.
        row = conn.execute(
            f"""
            UPDATE users
            SET entra_tid = %s, entra_oid = %s, display_name = %s,
                status = 'active', last_login_at = now()
            WHERE lower(email) = %s AND status = 'invited' AND entra_oid IS NULL
            RETURNING {_COLUMNS};
            """,
            (tid, oid, (claims.get("name") or "")[:200] or None, email),
        ).fetchone()
        if row is not None:
            conn.execute(
                "INSERT INTO audit_log (user_id, action) VALUES (%s, 'first_login');",
                (row[0],),
            )
        return User(*row) if row else None


def audit(user_id: int, action: str, detail: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO audit_log (user_id, action, detail) VALUES (%s, %s, %s);",
            (user_id, action, detail),
        )
