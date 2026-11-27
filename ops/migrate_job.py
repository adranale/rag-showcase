"""Container Apps Job entrypoint: apply the schema and set up the runtime role.

Environment: ADMIN_DATABASE_URL (owner connection), RUNTIME_DB_PASSWORD, and
optionally OWNER_EMAIL (assigns documents imported before multi-user mode to that user). Both steps are
idempotent, so the job can be re-run.
"""

import os
import subprocess
import sys
from pathlib import Path

REQUIRED = ("ADMIN_DATABASE_URL", "RUNTIME_DB_PASSWORD")
ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))
    for module in ("ops.init_db", "ops.setup_runtime_role"):
        print(f"== {module}", flush=True)
        result = subprocess.run([sys.executable, "-m", module], cwd=ROOT, check=False)
        if result.returncode:
            raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
