"""Start the Streamlit UI with the Microsoft login configured from environment variables.

Streamlit reads its login settings from a secrets file; the container has no such file
(and secrets must not be baked into the image), so it is written at start from:

    AUTH_REDIRECT_URI   public UI URL + /oauth2callback (registered in the Entra app)
    AUTH_COOKIE_SECRET  random string that signs the login cookie
    MS_CLIENT_ID, MS_CLIENT_SECRET
    MS_TENANT           'consumers' (default, personal Microsoft accounts)

'common'/'organizations' (work/school accounts) currently fail Streamlit's issuer
check ("invalid_claim: iss"), see docs/multi-user.md "Limitations".
"""

import json
import os
import sys
from pathlib import Path

REQUIRED = ("AUTH_REDIRECT_URI", "AUTH_COOKIE_SECRET", "MS_CLIENT_ID", "MS_CLIENT_SECRET")
ROOT = Path(__file__).resolve().parent.parent
STREAMLIT_ARGS = [
    "-m",
    "streamlit",
    "run",
    str(ROOT / "ui" / "chat.py"),
    "--server.address=0.0.0.0",
    "--server.port=8501",
    "--server.headless=true",
]


def _quote(value: str) -> str:
    return json.dumps(value)


def build_secrets(env: dict[str, str]) -> str:
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))
    tenant = env.get("MS_TENANT", "consumers").strip() or "consumers"
    metadata = f"https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration"
    return "\n".join(
        [
            "[auth]",
            f"redirect_uri = {_quote(env['AUTH_REDIRECT_URI'])}",
            f"cookie_secret = {_quote(env['AUTH_COOKIE_SECRET'])}",
            "",
            "[auth.microsoft]",
            f"client_id = {_quote(env['MS_CLIENT_ID'])}",
            f"client_secret = {_quote(env['MS_CLIENT_SECRET'])}",
            f"server_metadata_url = {_quote(metadata)}",
            "",
        ]
    )


def main() -> None:
    secrets_file = Path.home() / ".streamlit" / "secrets.toml"
    secrets_file.parent.mkdir(parents=True, exist_ok=True)
    secrets_file.write_text(build_secrets(dict(os.environ)), encoding="utf-8")
    secrets_file.chmod(0o600)
    # ui/chat.py imports the `app` and `ui` packages from the repository root.
    os.environ["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])
    )
    os.execv(sys.executable, [sys.executable, *STREAMLIT_ARGS])


if __name__ == "__main__":
    main()
