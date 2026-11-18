"""Container Apps Job entrypoint: apply the schema and set up the runtime role.

Environment: ADMIN_DATABASE_URL (owner connection), RUNTIME_DB_PASSWORD, and
optionally OWNER_EMAIL (assigns pre-v2 documents to that user). Both steps are
idempotent, so the job can be re-run.
"""

import os
import subprocess
import sys

REQUIRED = ("ADMIN_DATABASE_URL", "RUNTIME_DB_PASSWORD")


def main() -> None:
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))
    for script in ("init_db.py", "setup_runtime_role.py"):
        print(f"== {script}", flush=True)
        result = subprocess.run([sys.executable, script], check=False)
        if result.returncode:
            raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
