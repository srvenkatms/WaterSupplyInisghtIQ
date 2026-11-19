import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


operator = load("private_operator", "harness/private_operator.py")
bootstrap = load("operator_bootstrap", "scripts/deploy-operator.py")
MANIFEST = {
    "subscription": "00000000-0000-0000-0000-000000000001",
    "prefix": "swulab", "utilityAResourceGroup": "rg-swulab-uta",
    "operatorVmName": "vm-uta-operator", "location": "eastus2",
}


def test_direct_harness_entrypoint_does_not_shadow_standard_library():
    subprocess.run([sys.executable, str(ROOT / "harness" / "swuharness.py"), "--help"],
                   check=True, capture_output=True, text=True, timeout=30)


def test_operator_packages_the_schema_and_persists_acceptance_results():
    dockerfile = (ROOT / "harness" / "Dockerfile").read_text()
    staging = (ROOT / "scripts" / "deploy-apps.sh").read_text()
    assert "COPY infra/tables.json /lab/infra/tables.json" in dockerfile
    assert 'cp infra/tables.json "$STAGE/infra/"' in staging
    assert (ROOT / "infra" / "tables.json").is_file()
    assert "/opt/swu/operator/state/test-results:/lab/test-results" in bootstrap.BOOTSTRAP


def test_azure_cli_uses_resolved_windows_command_launcher(monkeypatch):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: r"C:\tools\az.cmd")
    assert bootstrap.azure_cli("account", "show") == [r"C:\tools\az.cmd", "account", "show"]


def test_missing_azure_cli_is_an_explicit_failure(monkeypatch):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="Azure CLI is required"):
        bootstrap.azure_cli("account", "show")


def test_tunnels_target_only_bastion_and_named_lab_vms():
    for role, (vm, port) in operator.TARGETS.items():
        command = operator.tunnel_command(MANIFEST, role)
        assert command[:4] == ["az", "network", "bastion", "tunnel"]
        assert command[command.index("--name") + 1] == "bas-swulab-uta"
        assert command[command.index("--target-resource-id") + 1].endswith("/virtualMachines/" + vm)
        assert command[command.index("--resource-port") + 1] == "22"
        assert command[command.index("--port") + 1] == str(port)


def test_control_proof_uses_a_temporary_private_bastion_tunnel():
    command = operator.tunnel_command(MANIFEST, "plc")
    assert command[command.index("--target-resource-id") + 1].endswith("/virtualMachines/vm-uta-plc")
    assert command[command.index("--port") + 1] == "2203"
    assert "plc" not in operator.TARGETS


@pytest.mark.parametrize("changes", [
    {"prefix": "../foreign"}, {"utilityAResourceGroup": "unrelated"},
    {"subscription": "invalid"},
])
def test_tunnels_reject_foreign_or_malformed_network_manifest(changes):
    with pytest.raises(ValueError):
        operator.tunnel_command(MANIFEST | changes, "attack")


def test_ssh_requires_live_tunnel_and_authenticated_connection(monkeypatch):
    process = SimpleNamespace(poll=lambda: 0)
    with pytest.raises(RuntimeError, match="tunnel exited"):
        operator.wait_for_ssh("attack", process)
    calls = []
    monkeypatch.setattr(operator.subprocess, "run", lambda args, **kw:
                        calls.append(args) or SimpleNamespace(returncode=0))
    operator.wait_for_ssh("attack", SimpleNamespace(poll=lambda: None))
    assert calls == [["ssh", "swu-attack", "true"]]


def test_protected_key_never_appears_in_script_or_regular_parameters():
    key = b"synthetic private key for unit testing"
    payload = bootstrap.protected_payload(MANIFEST, key)
    properties = payload["properties"]
    assert "parameters" not in properties
    assert properties["protectedParameters"][0]["name"] == "sshKey"
    assert key.decode() not in json.dumps(payload)
    encoded = properties["protectedParameters"][0]["value"]
    assert encoded not in properties["source"]["script"]
    assert "chmod 600 /opt/swu/operator/id_ed25519" in properties["source"]["script"]
    assert "/run/secrets/id_ed25519:ro" in properties["source"]["script"]
    assert "set -x" not in properties["source"]["script"]
    assert '${sshKey:?Protected SSH key is required}' in properties["source"]["script"]
    assert "unset sshKey" in properties["source"]["script"]
    assert '"$1"' not in properties["source"]["script"]


def test_bootstrap_rejects_wrong_operator_vm():
    with pytest.raises(ValueError, match="operator VM"):
        bootstrap.protected_payload(MANIFEST | {"operatorVmName": "foreign-vm"}, b"synthetic")


@pytest.mark.parametrize("view", [
    {"executionState": "Succeeded", "exitCode": 1, "output": "__SWULAB_OPERATOR_READY__"},
    {"executionState": "Succeeded", "exitCode": 0, "output": "accepted"},
    {"executionState": "Failed", "exitCode": 1},
])
def test_bootstrap_does_not_report_admission_or_execution_failure_as_success(monkeypatch, view):
    calls = []

    def request(url, method="GET", payload=None):
        calls.append(method)
        return {"properties": {"instanceView": view}} if method == "GET" else {}

    monkeypatch.setattr(bootstrap, "azure_request", request)
    with pytest.raises(RuntimeError):
        bootstrap.deploy(MANIFEST, b"synthetic")
    assert calls == ["PUT", "GET"]


def test_success_removes_protected_control_plane_payload(monkeypatch, capsys):
    calls = []

    def request(url, method="GET", payload=None):
        calls.append(method)
        if method == "GET" and "DELETE" in calls:
            raise HTTPError(url, 404, "Deleted", None, None)
        return {"properties": {"instanceView": {
            "executionState": "Succeeded", "exitCode": 0, "output": "__SWULAB_OPERATOR_READY__"
        }}} if method == "GET" else {}

    monkeypatch.setattr(bootstrap, "azure_request", request)
    bootstrap.deploy(MANIFEST, b"synthetic")
    assert calls == ["PUT", "GET", "DELETE", "GET"]
    assert "bootstrap completed" in capsys.readouterr().out


def test_protected_cleanup_waits_for_actual_azure_deletion_not_just_admission(monkeypatch):
    calls = []
    ticks = iter([0, 1, 2, 183, 184])
    monkeypatch.setattr(bootstrap, "time", SimpleNamespace(
        monotonic=lambda: next(ticks), sleep=lambda seconds: None))

    def request(url, method="GET", payload=None):
        calls.append(method)
        if method == "GET" and "DELETE" in calls:
            if calls.count("GET") == 2:
                return {"properties": {"provisioningState": "Deleting"}}
            raise HTTPError(url, 404, "Deleted", None, None)
        return {"properties": {"instanceView": {
            "executionState": "Succeeded", "exitCode": 0, "output": "__SWULAB_OPERATOR_READY__"
        }}} if method == "GET" else {}

    monkeypatch.setattr(bootstrap, "azure_request", request)
    bootstrap.deploy(MANIFEST, b"synthetic")
    assert calls == ["PUT", "GET", "DELETE", "GET", "GET"]
