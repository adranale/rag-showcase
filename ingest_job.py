"""Container Apps Job entrypoint: remote ingestion followed by embedding.

Configuration comes from environment variables only (PROVIDER, FOLDER,
DELETE_REMOVED, plus the connector credentials and DATABASE_URL), so no
arguments that start with '-' have to survive the az CLI.
"""

import base64
import binascii
import os
import subprocess
import sys

PROVIDERS = ("dropbox", "google_drive")
TRUE_VALUES = ("1", "true", "yes")


def build_ingest_command(env: dict[str, str]) -> list[str]:
    provider = env.get("PROVIDER", "").strip()
    folder = env.get("FOLDER", "").strip()
    if provider not in PROVIDERS:
        raise SystemExit(f"PROVIDER must be one of: {', '.join(PROVIDERS)}")
    if not folder:
        raise SystemExit("FOLDER is required (Dropbox path or Google Drive folder ID)")
    command = [
        sys.executable,
        "ingest_remote.py",
        "--provider",
        provider,
        "--folder",
        folder,
    ]
    if env.get("DELETE_REMOVED", "").strip().lower() in TRUE_VALUES:
        command.append("--delete-removed")
    return command


def decode_service_account(env: dict[str, str]) -> None:
    """Accept the service account key base64-encoded (safe on a command line)."""
    encoded = env.pop("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "").strip()
    if encoded and not env.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        try:
            env["GOOGLE_SERVICE_ACCOUNT_JSON"] = base64.b64decode(
                encoded, validate=True
            ).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise SystemExit(
                "GOOGLE_SERVICE_ACCOUNT_JSON_B64 is not valid base64"
            ) from exc


def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("DATABASE_URL is not set")
    env = dict(os.environ)
    decode_service_account(env)
    steps = [
        [sys.executable, "init_db.py"],
        build_ingest_command(env),
        [sys.executable, "embed_chunks.py"],
    ]
    ingest_code = 0
    for step in steps:
        print(f"== {step[1]}", flush=True)
        result = subprocess.run(step, env=env, check=False)
        if result.returncode == 0:
            continue
        if step[1] == "ingest_remote.py":
            # Files that failed are retried on the next run; the files that were
            # ingested must still be embedded, otherwise search finds nothing.
            ingest_code = result.returncode
            continue
        raise SystemExit(result.returncode)
    if ingest_code:
        raise SystemExit(ingest_code)


if __name__ == "__main__":
    main()
