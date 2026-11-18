"""Per-user daily usage limits."""

from app.db import get_connection
from app.users import User


class QuotaExceeded(Exception):
    pass


def consume_chat(user: User) -> None:
    """Count one chat request or raise QuotaExceeded when the daily limit is used up."""
    if user.chats_per_day <= 0:
        raise QuotaExceeded("Chat is disabled for this account")

    with get_connection(user.id) as conn:
        row = conn.execute(
            """
            INSERT INTO chat_usage (user_id, day, count)
            VALUES (%s, (now() AT TIME ZONE 'UTC')::date, 1)
            ON CONFLICT (user_id, day) DO UPDATE
                SET count = chat_usage.count + 1
                WHERE chat_usage.count < %s
            RETURNING count;
            """,
            (user.id, user.chats_per_day),
        ).fetchone()

    if row is None:
        raise QuotaExceeded(
            f"Daily limit of {user.chats_per_day} questions reached. Try again tomorrow."
        )
