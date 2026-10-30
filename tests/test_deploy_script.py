import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "azure.ps1"
WORKFLOW = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "build-image.yml"
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
