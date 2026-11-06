import os
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASH = Path(r"C:\Program Files\Git\bin\bash.exe") if os.name == "nt" else shutil.which("bash")


@pytest.fixture
def script_lab(tmp_path):
    (tmp_path / "scripts").mkdir()
    for script in (ROOT / "scripts").glob("*.sh"):
        shutil.copyfile(script, tmp_path / "scripts" / script.name)
    binary = tmp_path / "bin"
    binary.mkdir()
    shutil.copyfile(ROOT / "harness" / "tests" / "fixtures" / "az", binary / "az")
    (binary / "az").chmod(0o755)
    jq = shutil.which("jq") or str(ROOT / ".venv" / "tools" / "jq.exe")
    if not Path(jq).is_file():
        pytest.fail("jq is required to execute deployment-script tests")
    shutil.copyfile(jq, binary / ("jq.exe" if os.name == "nt" else "jq"))
    manifest = {
        "subscription": "00000000-0000-0000-0000-000000000001",
        "prefix": "swulab", "resourceGroups": ["rg-swulab-shared", "rg-swulab-uta"],
        "utilityAResourceGroup": "rg-swulab-uta", "sharedResourceGroup": "rg-swulab-shared",
        "dce": "https://synthetic.example.invalid",
        "utilities": [{"utilityId": "uta", "dcrImmutableId": "dcr-synthetic"}],
        "hmiIp": "10.10.1.11",
    }
    (tmp_path / "deployment-outputs.json").write_text(json.dumps(manifest))
    return tmp_path, manifest


def run_script(lab, script, arguments=(), extra=None):
    directory, _ = lab
    env = os.environ | {"SWU_COMMAND_LOG": "calls.log"}
    env.update(extra or {})
    command = ('export PATH="$PWD/bin:$PATH"; '
               '[[ "$(command -v az)" == "$PWD/bin/az" ]] || exit 99; '
               f'bash scripts/{script} "$@"')
    result = subprocess.run([str(BASH), "-c", command, "test", *arguments], cwd=directory,
                            env=env, text=True, capture_output=True, timeout=60)
    assert result.returncode != 99, "Azure stub guard failed; no script executed"
    log = directory / "calls.log"
    return result, log.read_text() if log.exists() else ""


def test_teardown_refuses_wrong_subscription_without_any_cloud_call(script_lab):
    result, calls = run_script(script_lab, "teardown.sh", ["different-subscription"])
    assert result.returncode != 0
    assert not calls


def test_teardown_rejects_foreign_manifest_group_before_deleting(script_lab):
    directory, manifest = script_lab
    manifest["resourceGroups"] = ["unrelated-group"]
    (directory / "deployment-outputs.json").write_text(json.dumps(manifest))
    result, calls = run_script(script_lab, "teardown.sh", [manifest["subscription"]])
    assert result.returncode != 0
    assert "group delete" not in calls and "budget delete" not in calls


def test_teardown_rerun_handles_already_deleted_groups(script_lab):
    _, manifest = script_lab
    result, calls = run_script(script_lab, "teardown.sh", [manifest["subscription"]])
    assert result.returncode == 0, result.stderr
    assert "group delete" not in calls
    assert "consumption budget list" in calls


def test_teardown_rejects_empty_group_manifest(script_lab):
    directory, manifest = script_lab
    manifest["resourceGroups"] = []
    (directory / "deployment-outputs.json").write_text(json.dumps(manifest))
    result, calls = run_script(script_lab, "teardown.sh", [manifest["subscription"]])
    assert result.returncode != 0
    assert "delete" not in calls


def test_lockdown_surfaces_query_failure(script_lab):
    result, calls = run_script(script_lab, "lockdown.sh",
                               extra={"SWU_STUB_FAIL": "network nsg rule list"})
    assert result.returncode != 0
    assert "network vnet subnet update" not in calls
    assert "Temporary VM build egress removed" not in result.stdout


def test_lockdown_verifies_each_rule_and_nat_attachment(script_lab):
    result, calls = run_script(script_lab, "lockdown.sh")
    assert result.returncode == 0, result.stderr
    assert calls.count("network nsg rule list") == 3
    assert calls.count("network vnet subnet show") == 6
    assert calls.count("--route-table rt-swulab-uta-isolated") == 2


def test_operator_bootstrap_egress_is_removed_without_changing_ot_lockdown(script_lab):
    directory, manifest = script_lab
    manifest["operatorVmName"] = "vm-uta-operator"
    (directory / "deployment-outputs.json").write_text(json.dumps(manifest))
    result, calls = run_script(script_lab, "lockdown.sh")
    assert result.returncode == 0, result.stderr
    assert calls.count("network nsg rule list") == 4
    assert "nsg-swulab-uta-operator" in calls
    assert calls.count("--route-table rt-swulab-uta-isolated") == 2
    assert calls.count("network vnet subnet show") == 6


def test_operator_verification_is_optional_for_existing_manifests(script_lab):
    result, calls = run_script(script_lab, "verify-operator.sh")
    assert result.returncode == 0, result.stderr
    assert not calls


def test_application_bootstrap_stops_if_vm_cannot_be_started(script_lab):
    directory, _ = script_lab
    for folder in ("apps", "generator", "profiles"):
        shutil.copytree(ROOT / folder, directory / folder, ignore=shutil.ignore_patterns("__pycache__"))
    result, calls = run_script(script_lab, "deploy-apps.sh", extra={"SWU_STUB_FAIL": "vm start"})
    assert result.returncode != 0
    assert "vm start -g rg-swulab-uta -n vm-uta-plc" in calls
    assert "vm run-command invoke" not in calls


def test_operator_verification_requires_real_post_lockdown_evidence(script_lab):
    directory, manifest = script_lab
    manifest["operatorVmName"] = "vm-uta-operator"
    (directory / "deployment-outputs.json").write_text(json.dumps(manifest))
    result, calls = run_script(script_lab, "verify-operator.sh")
    assert result.returncode != 0
    assert "docker restart swu-operator" in calls
    assert "vm run-command invoke" in calls
    assert "Private operator post-lockdown proof recorded" not in result.stdout


def test_sentinel_solution_provider_is_checked_before_infrastructure_deployment(script_lab):
    directory, _ = script_lab
    for folder in ("profiles", "generator", "infra"):
        shutil.copytree(ROOT / folder, directory / folder)
    result, calls = run_script(script_lab, "deploy.sh", extra={
        "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000001",
        "SWULAB_OWNER": "test-owner", "SWULAB_BUDGET_AMOUNT": "1000",
        "SWULAB_PREFIX": "swulab",
        "SWULAB_BUDGET_EMAILS": '["owner@example.invalid"]',
        "SWULAB_SSH_PUBLIC_KEY": "synthetic-public-key-for-script-test",
        "SWULAB_TESTER_ID": "synthetic-tester", "SWULAB_DEPLOY_OPERATOR": "false",
        "SWU_STUB_FAIL": "deployment sub what-if",
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
    })
    infrastructure_log = directory / "infra" / "calls.log"
    assert infrastructure_log.is_file(), result.stderr + result.stdout
    calls += infrastructure_log.read_text()
    assert result.returncode != 0
    assert "provider show --namespace Microsoft.OperationsManagement" in calls, result.stderr + result.stdout
    assert "provider register --namespace Microsoft.OperationsManagement" in calls
    assert calls.index("Microsoft.OperationsManagement") < calls.index("deployment sub what-if")
    assert "deployment sub create" not in calls


def test_lockdown_rejects_missing_null_route_before_network_edits(script_lab):
    result, calls = run_script(script_lab, "lockdown.sh", extra={"SWU_STUB_DENY_ROUTES": "0"})
    assert result.returncode != 0
    assert "lacks the null Internet route" in result.stderr
    assert "network nsg rule delete" not in calls
    assert "network vnet subnet update" not in calls


def test_vm_command_admission_without_success_marker_fails(script_lab):
    directory, _ = script_lab
    for folder in ("apps", "profiles"):
        shutil.copytree(ROOT / folder, directory / folder)
    (directory / "generator").mkdir()
    shutil.copytree(ROOT / "generator" / "swugen", directory / "generator" / "swugen")
    result, calls = run_script(script_lab, "deploy-apps.sh", extra={"SWU_STUB_NO_MARKER": "1"})
    assert result.returncode != 0
    assert "vm run-command invoke" in calls
    assert "bash -s -- <<'SWULAB_APPLICATION_SCRIPT'\nset -euo pipefail" in calls
    assert "Utility A apps running" not in result.stdout


def test_guest_scripts_explicitly_select_bash():
    import runpy

    bootstrap = runpy.run_path(str(ROOT / "scripts" / "deploy-operator.py"))["BOOTSTRAP"]
    assert bootstrap.startswith("bash -s -- <<'SWULAB_OPERATOR_SCRIPT'\n")
    assert bootstrap.endswith("SWULAB_OPERATOR_SCRIPT\n")
    verification = (ROOT / "scripts" / "verify-operator.sh").read_text()
    assert "bash -s -- <<" in verification
    assert "SWULAB_VERIFICATION_SCRIPT\n' -o json)" in verification


@pytest.mark.parametrize("failures,expected_attempts,success", [(0, 1, True), (2, 3, True), (6, 6, False)])
def test_generator_publisher_preserves_linux_path_and_bounds_storage_retries(
        script_lab, failures, expected_attempts, success):
    directory, manifest = script_lab
    manifest["functionApp"] = "func-synthetic"
    (directory / "deployment-outputs.json").write_text(json.dumps(manifest))
    for folder in ("generator", "profiles"):
        shutil.copytree(ROOT / folder, directory / folder, ignore=shutil.ignore_patterns("__pycache__"))
    sleep = directory / "bin" / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n")
    sleep.chmod(0o755)
    result, calls = run_script(script_lab, "deploy-generator.sh", extra={
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        "SWU_STUB_STORAGE_FAILURES": str(failures),
        "SWU_STUB_CHECK_PACKAGE": "1",
    })
    assert (result.returncode == 0) == success, result.stderr
    assert ("Generator published" in result.stdout) == success
    assert calls.count("functionapp deployment source config-zip") == expected_attempts
    assert "SWU_PROFILE_DIR=/home/site/wwwroot/profiles" in calls
    assert calls.index("SWU_PROFILE_DIR=") < calls.index("functionapp deployment source config-zip")
    for name in ("deploy.sh", "deploy-generator.sh"):
        assert "export MSYS_NO_PATHCONV=1" in (ROOT / "scripts" / name).read_text()


def test_generator_publisher_does_not_retry_other_deployment_failures(script_lab):
    directory, manifest = script_lab
    manifest["functionApp"] = "func-synthetic"
    (directory / "deployment-outputs.json").write_text(json.dumps(manifest))
    for folder in ("generator", "profiles"):
        shutil.copytree(ROOT / folder, directory / folder, ignore=shutil.ignore_patterns("__pycache__"))
    result, calls = run_script(script_lab, "deploy-generator.sh", extra={
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        "SWU_STUB_FAIL": "functionapp deployment source config-zip",
    })
    assert result.returncode != 0
    assert calls.count("functionapp deployment source config-zip") == 1
    assert "Synthetic Azure command failure" in result.stderr
    assert "Generator published" not in result.stdout
