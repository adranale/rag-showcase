import base64
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import ingest_job

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "azure-ingest-job.ps1"


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_build_ingest_command_uses_env():
    command = ingest_job.build_ingest_command(
        {"PROVIDER": "dropbox", "FOLDER": "/Docs", "DELETE_REMOVED": "true"}
    )

    assert command[1:] == [
        "ingest_remote.py",
        "--provider",
        "dropbox",
        "--folder",
        "/Docs",
        "--delete-removed",
    ]


@pytest.mark.parametrize(
    "env",
    [
        {"PROVIDER": "box", "FOLDER": "/x"},
        {"PROVIDER": "dropbox", "FOLDER": " "},
        {},
    ],
)
def test_build_ingest_command_rejects_invalid(env):
    with pytest.raises(SystemExit):
        ingest_job.build_ingest_command(env)


def test_service_account_is_decoded_from_base64():
    key = json.dumps({"client_email": "x@y"})
    env = {"GOOGLE_SERVICE_ACCOUNT_JSON_B64": base64.b64encode(key.encode()).decode()}

    ingest_job.decode_service_account(env)

    assert env["GOOGLE_SERVICE_ACCOUNT_JSON"] == key
    assert "GOOGLE_SERVICE_ACCOUNT_JSON_B64" not in env


def test_invalid_base64_is_rejected():
    with pytest.raises(SystemExit):
        ingest_job.decode_service_account({"GOOGLE_SERVICE_ACCOUNT_JSON_B64": "!!"})


def test_main_runs_three_steps_in_order(monkeypatch):
    calls = []

    def fake_run(command, env, check):
        calls.append(command[1:])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("PROVIDER", "google_drive")
    monkeypatch.setenv("FOLDER", "abc")
    monkeypatch.setattr(ingest_job.subprocess, "run", fake_run)

    ingest_job.main()

    assert [c[0] for c in calls] == [
        "init_db.py",
        "ingest_remote.py",
        "embed_chunks.py",
    ]


def test_main_stops_after_failed_step(monkeypatch):
    calls = []

    def fake_run(command, env, check):
        calls.append(command[1])
        return subprocess.CompletedProcess(command, 3 if len(calls) == 2 else 0)

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("PROVIDER", "dropbox")
    monkeypatch.setenv("FOLDER", "/")
    monkeypatch.setattr(ingest_job.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc:
        ingest_job.main()

    assert exc.value.code == 3
    assert "embed_chunks.py" not in calls


def test_main_requires_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SystemExit):
        ingest_job.main()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
def test_job_script_parses():
    command = (
        "$e=$null;$t=$null;"
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{SCRIPT}',[ref]$t,[ref]$e)|Out-Null;"
        "if($e.Count){$e|ForEach-Object Message;exit 1}"
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout


def test_job_script_subscription_mandatory_and_applied(script_text):
    assert re.search(r"\[Parameter\(Mandatory\)\][^\n]*\$SubscriptionId", script_text)
    assert "$azArgs += '--subscription', $SubscriptionId" in script_text


def test_job_script_does_not_echo_secrets(script_text):
    assert "-join ' '" not in script_text
    assert "$($args" not in script_text
    assert not re.search(
        r"Write-(Host|Output|Verbose)[^\n]*\$(databaseUrl|credentialMap|secretArgs|envArgs)",
        script_text,
    )
    assert "SecretValues.Add($databaseUrl)" in script_text


def test_job_script_avoids_dash_argument_pitfalls(script_text):
    assert "--args=" not in script_text
    assert not re.search(r"'--args',\s*'-", script_text)
    assert "'--command', 'python', '--args', 'ingest_job.py'" in script_text
    assert "DATABASE_URL=secretref:db-url" in script_text


def test_job_script_reuses_api_image_and_secret(script_text):
    assert "--secret-name db-url" in script_text
    assert "properties.template.containers[0].image" in script_text


def test_entrypoint_is_in_image():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    for name in ("ingest_job.py", "ingest_remote.py", "embed_chunks.py", "connectors/"):
        assert name in dockerfile
    assert sys.version_info >= (3, 12)
