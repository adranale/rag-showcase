import shutil
import subprocess
from pathlib import Path

import httpx
import psycopg
import pytest

import idle_stop
from app import azure_arm, db, wake

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "azure-autostop.ps1"
SERVER = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.DBforPostgreSQL/flexibleServers/pg"
APP = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.App/containerApps/rag-api"


@pytest.fixture(autouse=True)
def reset_wake(monkeypatch):
    monkeypatch.setattr(wake, "_last_attempt", 0.0)
    monkeypatch.delenv("POSTGRES_RESOURCE_ID", raising=False)


def test_wake_disabled_without_resource_id(monkeypatch):
    monkeypatch.setattr(wake, "Thread", lambda **kw: pytest.fail("must not start"))
    assert wake.trigger_start() is False


def test_wake_starts_stopped_server_once_per_minute(monkeypatch):
    monkeypatch.setenv("POSTGRES_RESOURCE_ID", SERVER)
    calls = []

    class SyncThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(wake, "Thread", SyncThread)
    monkeypatch.setattr(azure_arm, "server_state", lambda _id: "Stopped")
    monkeypatch.setattr(azure_arm, "start_server", lambda _id: calls.append(_id))

    assert wake.trigger_start() is True
    assert wake.trigger_start() is True
    assert calls == [SERVER]


def test_wake_does_not_start_running_server(monkeypatch):
    monkeypatch.setenv("POSTGRES_RESOURCE_ID", SERVER)
    monkeypatch.setattr(azure_arm, "server_state", lambda _id: "Ready")
    monkeypatch.setattr(
        azure_arm, "start_server", lambda _id: pytest.fail("must not start")
    )
    wake._start()


def test_get_connection_reports_database_starting(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/d")
    monkeypatch.setenv("POSTGRES_RESOURCE_ID", SERVER)

    def refuse(*args, **kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(db.psycopg, "connect", refuse)
    monkeypatch.setattr(
        wake, "Thread", lambda **kw: type("T", (), {"start": lambda s: None})()
    )

    with pytest.raises(wake.DatabaseStarting) as info:
        db.get_connection()

    assert wake.describe(info.value, "default") == wake.STARTING_MESSAGE


def test_get_connection_error_unchanged_without_auto_stop(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/d")

    def refuse(*args, **kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(db.psycopg, "connect", refuse)

    with pytest.raises(psycopg.OperationalError):
        db.get_connection()


@pytest.mark.parametrize(
    ("state", "requests", "uptime", "stops"),
    [
        ("Ready", 0, 30, True),
        ("Ready", 0, 27, True),
        ("Ready", 0, 5, False),
        ("Ready", 3, 30, False),
        ("Stopped", 0, 0, False),
        ("Starting", 0, 0, False),
    ],
)
def test_idle_stop_decisions(monkeypatch, state, requests, uptime, stops):
    stopped = []
    monkeypatch.setattr(azure_arm, "server_state", lambda _id: state)
    monkeypatch.setattr(azure_arm, "request_count", lambda _id, _m: requests)
    monkeypatch.setattr(azure_arm, "uptime_minutes", lambda _id, _m: uptime)
    monkeypatch.setattr(azure_arm, "stop_server", lambda _id: stopped.append(_id))

    idle_stop.run(SERVER, APP, 30)

    assert bool(stopped) is stops


def test_idle_stop_never_stops_when_check_fails(monkeypatch, capsys):
    stopped = []
    monkeypatch.setenv("POSTGRES_RESOURCE_ID", SERVER)
    monkeypatch.setenv("API_APP_RESOURCE_ID", APP)
    monkeypatch.setattr(azure_arm, "server_state", lambda _id: "Ready")

    def fail(*args):
        raise httpx.ConnectError("metrics unavailable")

    monkeypatch.setattr(azure_arm, "request_count", fail)
    monkeypatch.setattr(azure_arm, "stop_server", lambda _id: stopped.append(_id))

    assert idle_stop.main() == 1
    assert not stopped
    assert "idle check failed" in capsys.readouterr().err


def test_idle_stop_keeps_server_while_ingestion_runs(monkeypatch):
    stopped = []
    monkeypatch.setattr(azure_arm, "server_state", lambda _id: "Ready")
    monkeypatch.setattr(azure_arm, "running_job_executions", lambda _id: 1)
    monkeypatch.setattr(azure_arm, "request_count", lambda _id, _m: 0)
    monkeypatch.setattr(azure_arm, "uptime_minutes", lambda _id, _m: 60)
    monkeypatch.setattr(azure_arm, "stop_server", lambda _id: stopped.append(_id))

    message = idle_stop.run(SERVER, APP, 30, "/job")

    assert not stopped
    assert "ingestion" in message


def test_idle_stop_ignores_ingestion_check_when_not_configured(monkeypatch):
    stopped = []
    monkeypatch.setattr(azure_arm, "server_state", lambda _id: "Ready")
    monkeypatch.setattr(
        azure_arm, "running_job_executions", lambda _id: pytest.fail("not configured")
    )
    monkeypatch.setattr(azure_arm, "request_count", lambda _id, _m: 0)
    monkeypatch.setattr(azure_arm, "uptime_minutes", lambda _id, _m: 60)
    monkeypatch.setattr(azure_arm, "stop_server", lambda _id: stopped.append(_id))

    idle_stop.run(SERVER, APP, 30)

    assert stopped


def test_idle_stop_fails_closed_when_job_lookup_fails(monkeypatch):
    stopped = []
    monkeypatch.setenv("POSTGRES_RESOURCE_ID", SERVER)
    monkeypatch.setenv("API_APP_RESOURCE_ID", APP)
    monkeypatch.setenv("INGEST_JOB_RESOURCE_ID", "/job")
    monkeypatch.setattr(azure_arm, "server_state", lambda _id: "Ready")

    def forbidden(_id):
        raise httpx.ConnectError("no access")

    monkeypatch.setattr(azure_arm, "running_job_executions", forbidden)
    monkeypatch.setattr(azure_arm, "stop_server", lambda _id: stopped.append(_id))

    assert idle_stop.main() == 1
    assert not stopped


@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (200, {"value": [{"properties": {"status": "Running"}}, {"properties": {"status": "Failed"}}]}, 1),
        (200, {"value": []}, 0),
        (404, {}, 0),
    ],
)
def test_running_job_executions(monkeypatch, status_code, payload, expected):
    def fake_call(*args, **kwargs):
        request = httpx.Request("GET", "https://management.azure.com/x")
        response = httpx.Response(status_code, json=payload, request=request)
        response.raise_for_status()
        return response

    monkeypatch.setattr(azure_arm, "_call", fake_call)

    assert azure_arm.running_job_executions("/job") == expected

def test_request_count_sums_metric_points(monkeypatch):
    body = {
        "value": [
            {
                "timeseries": [
                    {"data": [{"total": 2.0}, {}, {"total": 1.0}]},
                ]
            }
        ]
    }

    class Response:
        def json(self):
            return body

    monkeypatch.setattr(azure_arm, "_call", lambda *a, **k: Response())
    assert azure_arm.request_count(APP, 30) == 3


def test_uptime_minutes_counts_metric_points(monkeypatch):
    body = {
        "value": [
            {"timeseries": [{"data": [{"average": 1.0}, {}, {"average": 0.0}]}]},
        ]
    }

    class Response:
        def json(self):
            return body

    monkeypatch.setattr(azure_arm, "_call", lambda *a, **k: Response())
    assert azure_arm.uptime_minutes(SERVER, 30) == 2


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
def test_autostop_script_parses():
    command = (
        "$e=$null;$t=$null;"
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{SCRIPT}',[ref]$t,[ref]$e)|Out-Null;"
        "if($e.Count){$e|ForEach-Object Message;exit 1}"
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
def test_autostop_script_requires_subscription():
    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(SCRIPT),
            "-Suffix",
            "abc",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "SubscriptionId" in result.stdout + result.stderr


def test_autostop_script_never_echoes_arguments():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "if ($azArgs[0] -ne 'extension')" in text
    assert "--args idle_stop.py" in text
    assert "Write-Host $azArgs" not in text
    assert 'throw "Azure CLI step failed: $label' in text


def test_image_contains_idle_stop_entrypoint():
    assert "idle_stop.py" in (ROOT / "Dockerfile").read_text(encoding="utf-8")
