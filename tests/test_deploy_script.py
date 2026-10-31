import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "azure.ps1"
WORKFLOW = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "build-and-deploy-image.yml"
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


def test_azure_operations_workflow_dispatches_both_actions():
    text = (WORKFLOW.parent / "azure-operations.yml").read_text(encoding="utf-8")

    assert "autostop-setup" in text and "ingest-job" in text
    assert "./.github/actions/start-postgres" in text
    assert (WORKFLOW.parents[1] / "actions/start-postgres/action.yml").exists()


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
