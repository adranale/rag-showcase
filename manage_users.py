"""Owner tool: manage the invitation list (uses the admin connection).

    python manage_users.py list
    python manage_users.py invite friend@outlook.com            # personal Microsoft account
    python manage_users.py invite colleague@corp.com --tid <tenant-id> --oid <object-id>
    python manage_users.py disable friend@outlook.com
    python manage_users.py enable friend@outlook.com
    python manage_users.py quota friend@outlook.com --chats-per-day 50 --files-per-sync 500
    python manage_users.py admin friend@outlook.com              # may approve requests
    python manage_users.py unadmin friend@outlook.com

Most users join through the invite link instead (admin page of the UI); this tool
is the fallback when no admin can sign in.

Work/school accounts must be invited with their tenant and object ID (visible in the
Entra portal): the e-mail claim of a tenant is set by that tenant's admin and cannot
identify a person. This tool only touches the `users` table, never document data.
"""

import argparse
import os

from app.db import get_connection

if os.getenv("ADMIN_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["ADMIN_DATABASE_URL"]


def invite(email: str, tid: str | None, oid: str | None) -> None:
    if bool(tid) != bool(oid):
        raise SystemExit("--tid and --oid must be given together.")
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (email, entra_tid, entra_oid, status)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (lower(email)) DO UPDATE
                SET status = CASE WHEN users.status = 'disabled'
                                  THEN users.status ELSE EXCLUDED.status END,
                    entra_tid = COALESCE(EXCLUDED.entra_tid, users.entra_tid),
                    entra_oid = COALESCE(EXCLUDED.entra_oid, users.entra_oid);
            """,
            (email.lower(), tid and tid.lower(), oid, "active" if oid else "invited"),
        )
    print(f"Invited {email}" + (" (identity bound)" if oid else ""))


def set_status(email: str, status: str) -> None:
    with get_connection() as conn:
        updated = conn.execute(
            """
            UPDATE users
            SET status = CASE WHEN %s = 'active' AND entra_oid IS NULL
                              THEN 'invited' ELSE %s END
            WHERE lower(email) = %s;
            """,
            (status, status, email.lower()),
        ).rowcount
    if not updated:
        raise SystemExit(f"Unknown user: {email}")
    print(f"{email}: {status}")


def set_quota(email: str, chats: int | None, files: int | None) -> None:
    with get_connection() as conn:
        updated = conn.execute(
            """
            UPDATE users
            SET chats_per_day = COALESCE(%s, chats_per_day),
                files_per_sync = COALESCE(%s, files_per_sync)
            WHERE lower(email) = %s;
            """,
            (chats, files, email.lower()),
        ).rowcount
    if not updated:
        raise SystemExit(f"Unknown user: {email}")
    print(f"Updated limits for {email}")


def set_admin(email: str, is_admin: bool) -> None:
    with get_connection() as conn:
        updated = conn.execute(
            "UPDATE users SET is_admin = %s WHERE lower(email) = %s;",
            (is_admin, email.lower()),
        ).rowcount
    if not updated:
        raise SystemExit(f"Unknown user: {email}")
    print(f"{email}: admin={is_admin}")


def list_users() -> None:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT u.id, u.email, u.status, u.chats_per_day, u.files_per_sync,
                   u.last_login_at, (SELECT count(*) FROM documents d WHERE d.user_id = u.id),
                   u.is_admin
            FROM users u ORDER BY u.id;
            """
        ).fetchall()
    for row in rows:
        print(
            f"{row[0]:>3} {row[1]:<35} {row[2]:<9}{' admin' if row[7] else ''} "
            f"chats/day={row[3]} files/sync={row[4]} last_login={row[5]} documents={row[6]}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    invite_parser = commands.add_parser("invite")
    invite_parser.add_argument("email")
    invite_parser.add_argument("--tid")
    invite_parser.add_argument("--oid")
    for name in ("disable", "enable", "admin", "unadmin"):
        commands.add_parser(name).add_argument("email")
    quota = commands.add_parser("quota")
    quota.add_argument("email")
    quota.add_argument("--chats-per-day", type=int)
    quota.add_argument("--files-per-sync", type=int)
    args = parser.parse_args()

    if args.command == "list":
        list_users()
    elif args.command == "invite":
        invite(args.email, args.tid, args.oid)
    elif args.command == "disable":
        set_status(args.email, "disabled")
    elif args.command == "enable":
        set_status(args.email, "active")
    elif args.command in ("admin", "unadmin"):
        set_admin(args.email, args.command == "admin")
    else:
        set_quota(args.email, args.chats_per_day, args.files_per_sync)


if __name__ == "__main__":
    main()
