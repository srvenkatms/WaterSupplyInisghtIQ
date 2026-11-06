"""Bootstrap SSH material through encrypted Azure protected parameters, not logs."""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
API = "https://management.azure.com"
BOOTSTRAP = r"""bash -s -- <<'SWULAB_OPERATOR_SCRIPT'
set -euo pipefail
umask 077
install -d -m 700 /opt/swu/operator
printf '%s' "${sshKey:?Protected SSH key is required}" | base64 -d > /opt/swu/operator/id_ed25519
unset sshKey
chmod 600 /opt/swu/operator/id_ed25519
ssh-keygen -y -f /opt/swu/operator/id_ed25519 >/dev/null
if docker inspect swu-operator >/dev/null 2>&1; then docker rm -f swu-operator >/dev/null; fi
docker run -d --name swu-operator --restart unless-stopped --network host \
  -v /opt/swu/operator/id_ed25519:/run/secrets/id_ed25519:ro \
  -v /opt/swu/operator/known_hosts:/run/secrets/known_hosts:ro \
  -v /opt/swu/operator/deployment-outputs.json:/lab/deployment-outputs.json:ro \
  -v /opt/swu/operator/state:/state \
  -v /opt/swu/operator/state/test-results:/lab/test-results swu-operator >/dev/null
for i in $(seq 1 90); do
  STATUS=$(docker inspect -f '{{.State.Health.Status}}' swu-operator)
  if [ "$STATUS" = healthy ]; then echo __SWULAB_OPERATOR_READY__; exit 0; fi
  sleep 5
done
echo "Private operator did not become healthy; inspect its container and tunnel logs." >&2
exit 1
SWULAB_OPERATOR_SCRIPT
"""


def private_key() -> bytes:
    name = os.environ.get("SWULAB_SSH_PRIVATE_KEY_FILE")
    if not name:
        raise ValueError("SWULAB_SSH_PRIVATE_KEY_FILE is required for private operator bootstrap")
    path = Path(name)
    if os.name == "nt":
        acl_check = (
            "$ErrorActionPreference='Stop'; "
            "$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
            "$allowed=@($sid,'S-1-5-18','S-1-5-32-544'); "
            "$unsafe=@((Get-Acl -LiteralPath $env:SWULAB_SSH_PRIVATE_KEY_FILE).Access | "
            "Where-Object { $_.AccessControlType -eq 'Allow' -and "
            "($_.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::ReadData) -and "
            "$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -notin $allowed }); "
            "if ($unsafe.Count -gt 0) { exit 1 }"
        )
        env = os.environ.copy()
        env.pop("PSModulePath", None)
        shell = shutil.which("pwsh") or "powershell.exe"
        acl = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", acl_check],
                             capture_output=True, text=True, timeout=30, env=env)
        if acl.returncode:
            raise ValueError("Operator SSH key ACL must restrict reading to its owner, SYSTEM and administrators")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ValueError("Operator SSH key must not be accessible to group or other users")
    public = subprocess.run(["ssh-keygen", "-y", "-f", str(path)], check=True,
                            capture_output=True, text=True, timeout=10).stdout.split()
    expected = os.environ.get("SWULAB_SSH_PUBLIC_KEY", "").split()
    if len(expected) < 2 or public[:2] != expected[:2]:
        raise ValueError("Operator private key does not match the approved VM public key")
    return path.read_bytes()


def protected_payload(manifest: dict, key: bytes) -> dict:
    if manifest.get("operatorVmName") != "vm-uta-operator":
        raise ValueError("Manifest does not identify the private operator VM")
    return {
        "location": manifest["location"],
        "properties": {
            "source": {"script": BOOTSTRAP},
            "asyncExecution": True,
            "timeoutInSeconds": 900,
            "treatFailureAsDeploymentFailure": True,
            "protectedParameters": [
                {"name": "sshKey", "value": base64.b64encode(key).decode("ascii")}
            ],
        },
    }


def azure_cli(*arguments: str) -> list[str]:
    executable = shutil.which("az")
    if not executable:
        raise RuntimeError("Azure CLI is required on PATH")
    return [executable, *arguments]


def azure_request(url: str, method: str = "GET", payload: dict | None = None) -> dict:
    token = subprocess.run(
        azure_cli("account", "get-access-token", "--resource", API + "/",
                  "--query", "accessToken", "-o", "tsv"),
        check=True, capture_output=True, text=True, timeout=60).stdout.strip()
    request = Request(url, method=method,
                      data=json.dumps(payload).encode() if payload is not None else None,
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urlopen(request, timeout=60) as response:
        body = response.read()
        return json.loads(body) if body else {}


def deploy(manifest: dict, key: bytes) -> None:
    subscription = str(uuid.UUID(manifest["subscription"]))
    group = manifest["utilityAResourceGroup"]
    prefix = manifest["prefix"]
    if not re.fullmatch(r"[a-z][a-z0-9]{2,7}", prefix) or group != f"rg-{prefix}-uta":
        raise ValueError("Unexpected private operator resource group")
    # Protected parameter values are sent in memory over TLS and are never
    # placed in a CLI argument, temporary JSON file, manifest or image.
    url = (f"{API}/subscriptions/{subscription}/resourceGroups/{group}"
           "/providers/Microsoft.Compute/virtualMachines/vm-uta-operator"
           "/runCommands/swulab-operator-bootstrap?api-version=2024-07-01")
    azure_request(url, "PUT", protected_payload(manifest, key))
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        result = azure_request(url + "&$expand=instanceView")
        properties = result.get("properties", {})
        view = properties.get("instanceView", {})
        state = view.get("executionState")
        if state == "Succeeded":
            if view.get("exitCode") != 0 or "__SWULAB_OPERATOR_READY__" not in view.get("output", ""):
                raise RuntimeError("Operator bootstrap returned no verified readiness evidence")
            azure_request(url, "DELETE")
            deletion_deadline = time.monotonic() + 900
            while time.monotonic() < deletion_deadline:
                try:
                    azure_request(url)
                except HTTPError as error:
                    if error.code != 404:
                        raise
                    print("Private operator bootstrap completed; both pinned-key Bastion SSH paths are ready.")
                    return
                time.sleep(5)
            raise TimeoutError("Operator is ready but protected bootstrap resource deletion was not verified")
        if state in {"Failed", "Canceled", "TimedOut"} or properties.get("provisioningState") == "Failed":
            raise RuntimeError(f"Operator bootstrap failed ({state}, exit {view.get('exitCode')}); inspect VM run-command logs")
        time.sleep(10)
    raise TimeoutError("Private operator bootstrap did not complete within 20 minutes")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-key", action="store_true")
    args = parser.parse_args()
    key = private_key()
    if args.check_key:
        print("Operator SSH key matches the VM public key.")
        return
    manifest = json.loads((ROOT / "deployment-outputs.json").read_text())
    subprocess.run(azure_cli("account", "set", "--subscription", manifest["subscription"]), check=True)
    try:
        deploy(manifest, key)
    except HTTPError as error:
        raise RuntimeError(f"Azure operator bootstrap request failed with HTTP {error.code}") from None


if __name__ == "__main__":
    main()
