"""Stops the PostgreSQL server when the API received no requests for a while.

Runs as a scheduled Container Apps Job with a managed identity.
Env: POSTGRES_RESOURCE_ID, API_APP_RESOURCE_ID, IDLE_MINUTES (default 30).
"""

import os
import sys

from app import azure_arm

METRIC_SLACK_MINUTES = 5


def run(server_id: str, app_id: str, idle_minutes: int) -> str:
    state = azure_arm.server_state(server_id)
    if state != "Ready":
        return f"server state is {state}; nothing to do"

    requests = azure_arm.request_count(app_id, idle_minutes)
    if requests > 0:
        return f"{requests} API request(s) in the last {idle_minutes} min; keeping the server running"

    # Metrics lag by a few minutes, hence the slack; a freshly started server
    # (e.g. for an ingestion run) must not be stopped before the idle window passed.
    uptime = azure_arm.uptime_minutes(server_id, idle_minutes)
    if uptime + METRIC_SLACK_MINUTES < idle_minutes:
        return f"server up for about {uptime} min, less than {idle_minutes}; keeping it running"

    azure_arm.stop_server(server_id)
    return f"no API requests for {idle_minutes} min; stop requested"


def main() -> int:
    try:
        server_id = os.environ["POSTGRES_RESOURCE_ID"]
        app_id = os.environ["API_APP_RESOURCE_ID"]
        idle_minutes = int(os.getenv("IDLE_MINUTES", "30"))
        print(run(server_id, app_id, idle_minutes))
    except Exception as exc:  # fail closed: a failed check never stops the server
        print(f"idle check failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
