import importlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "azure.ps1"
WORKFLOW = (
    Path(__file__).resolve().parent.parent
    / ".github"
    / "workflows"
    / "build-and-deploy-image.yml"
)


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
def test_script_parses():
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
    )

    assert result.returncode == 0, result.stdout


def test_invoke_az_does_not_print_arguments(script_text):
    assert "-join ' '" not in script_text
    assert "$($args" not in script_text


def test_ui_startup_avoids_unreliable_dash_arguments(script_text):
    assert "--args=" not in script_text
    assert not re.search(r'--args\s+"?-', script_text)
    assert "STREAMLIT_SERVER_ADDRESS=0.0.0.0" in script_text


def test_subscription_is_mandatory_and_applied(script_text):
    assert re.search(r"\[Parameter\(Mandatory\)\][^\n]*\$SubscriptionId", script_text)
    assert "$azArgs += '--subscription', $SubscriptionId" in script_text


def test_secrets_file_written_before_provisioning(script_text):
    assert script_text.index("Set-Content -Path $envFile") < script_text.index(
        "create resource group"
    )


def test_workflow_builds_amd64_and_tags_latest_only_on_default_branch():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "platforms: linux/amd64" in text
    assert "is_default_branch" in text


def test_ingest_workflow_masks_secret_and_always_removes_firewall_rule():
    text = (WORKFLOW.parent / "ingest-sample-data.yml").read_text(encoding="utf-8")

    assert "::add-mask::$url" in text
    assert "secret show" in text and "db-url" in text
    assert re.search(r"if: \$\{\{ always\(\) \}\}", text)
    assert "firewall-rule delete" in text


def test_script_is_resumable_without_rotating_credentials(script_text):
    assert "Test-AzExists" in script_text
    assert "not idempotent" not in script_text.lower().replace("not be idempotent", "")
    # Existing credentials are reused (file, then rag-api secrets); never regenerated
    # when the server exists, and the file is only created when absent.
    assert "secret list" in script_text
    assert "The script will not reset the admin password" in script_text
    assert "if (-not $credentialsSaved)" in script_text
    assert "New-Item -ItemType File -Path $envFile -ErrorAction Stop" in script_text
    assert (
        "-Force"
        not in script_text.split("# Persist credentials first")[1].split(
            "# --- Provisioning"
        )[0]
    )
    # Creation steps are guarded by existence checks.
    for guard in (
        "if (-not $aoaiExists)",
        "if ($pgExists)",
        "if ($found['API app'])",
        "if ($found['UI app'])",
    ):
        assert guard in script_text
    assert "Specified server name is already used" in script_text


MULTIUSER = SCRIPT.parent / "azure-multiuser.ps1"


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
def test_multiuser_script_parses():
    command = (
        "$e=$null;$t=$null;"
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{MULTIUSER}',[ref]$t,[ref]$e)|Out-Null;"
        "if($e.Count){$e|ForEach-Object Message;exit 1}"
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stdout


def test_multiuser_script_is_safe_and_ordered():
    text = MULTIUSER.read_text(encoding="utf-8")

    assert re.search(r"\[Parameter\(Mandatory\)\][^\n]*\$SubscriptionId", text)
    assert "$azArgs += '--subscription', $SubscriptionId" in text
    assert "-join ' '" not in text and "$($args" not in text
    # Secrets are persisted before anything is changed, and never force-overwritten.
    assert text.index("Add-Content -Path $envFile") < text.index("# --- Jobs")
    assert "Set-Content" not in text and "-Force" not in text
    # The migration must succeed before the apps are switched to the runtime role.
    assert text.index("# --- Migration") < text.index("# --- rag-api")
    assert "The apps were NOT switched" in text
    # The UI start command must not contain arguments that start with a dash.
    assert "--command python --args ui/start.py" in text
    assert "APP_PASSWORD" in text
    # The e-mail key is masked and in place before the API is reconfigured.
    assert "$script:SecretValues.Add($acsConnection)" in text
    assert text.index("# --- E-mail notifications") < text.index("# --- rag-api")


def test_multiuser_workflow_is_pinned_and_environment_protected():
    text = (WORKFLOW.parent / "azure-multiuser.yml").read_text(encoding="utf-8")

    assert "environment: azure-test" in text
    assert "preflight_only" in text and "default: true" in text
    for line in text.splitlines():
        if "uses:" in line:
            assert re.search(r"@[0-9a-f]{40}\b", line), line


def test_migrate_job_requires_admin_url_and_runtime_password(monkeypatch):
    from ops import migrate_job

    monkeypatch.delenv("ADMIN_DATABASE_URL", raising=False)
    monkeypatch.delenv("RUNTIME_DB_PASSWORD", raising=False)
    with pytest.raises(SystemExit):
        migrate_job.main()

# Commands of Azure apps/jobs created before the package layout (see the root shims).
LEGACY_ENTRY_POINTS = (
    "ui_start.py",
    "sync_worker.py",
    "idle_stop.py",
    "manage_users.py",
    "migrate_job.py",
)


def test_container_commands_point_to_files_in_the_image():
    root = SCRIPT.parent.parent
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    texts = [path.read_text(encoding="utf-8") for path in (root / "deploy").glob("*.ps1")]
    texts.append((root / "compose.yaml").read_text(encoding="utf-8"))
    paths = set()
    for text in texts:
        paths.update(re.findall(r"--args (?:run )?([\w/]+\.py)", text))
        paths.update(re.findall(r"'python', '([\w/]+\.py)'", text))
        paths.update(re.findall(r'"python", "([\w/]+\.py)"', text))
    assert {
        "ui/start.py",
        "ui/chat.py",
        "ops/idle_stop.py",
        "ops/manage_users.py",
        "ops/migrate_job.py",
        "ingestion/sync_worker.py",
    } <= paths
    for path in paths | set(LEGACY_ENTRY_POINTS):
        assert (root / path).is_file(), path
        package = path.split("/")[0]
        assert (f" {package}/ ./{package}/" if "/" in path else path) in dockerfile, path


@pytest.mark.parametrize("name", LEGACY_ENTRY_POINTS)
def test_legacy_entry_points_still_resolve(name):
    module = importlib.import_module(name.removesuffix(".py"))
    assert callable(module.main)
