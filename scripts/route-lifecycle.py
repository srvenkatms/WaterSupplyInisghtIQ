"""Read-only by default: status, pause or resume only the owned Route A/B deployment."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("plc", "hmi", "gw", "attack")
VM_NAMES = {f"vm-uta-{role}" for role in (*ROLES, "operator")}


class Lifecycle:
    def __init__(self, subscription: str, tenant: str, prefix: str):
        self.subscription = str(uuid.UUID(subscription))
        self.tenant = str(uuid.UUID(tenant))
        if not re.fullmatch(r"[a-z][a-z0-9]{2,5}", prefix):
            raise ValueError("Prefix must have 3-6 lowercase letters/digits, beginning with a letter")
        self.prefix = prefix
        self.shared = f"rg-{prefix}-shared"
        self.console = f"ca-{prefix}-console"
        self.snapshot = None

    def azure(self, *args: str, timeout: int = 900):
        executable = shutil.which("az")
        if not executable:
            raise RuntimeError("Azure CLI is required")
        result = subprocess.run(
            [executable, *args, "--subscription", self.subscription, "--only-show-errors", "--output", "json"],
            capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f"Azure {' '.join(args[:3])} failed: {result.stderr.strip()}")
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def inspect(self) -> dict:
        account = self.azure("account", "show")
        if (account.get("id", "").lower() != self.subscription
                or account.get("tenantId", "").lower() != self.tenant or account.get("state") != "Enabled"):
            raise RuntimeError("Azure subscription/tenant does not match the explicitly requested deployment")
        routes = {}
        for suffix in ("shared", "routea", "routeb"):
            group = f"rg-{self.prefix}-{suffix}"
            tags = self.azure("group", "show", "--name", group).get("tags") or {}
            expected = {"project": "swu-lab", "deployment": self.prefix, "environment": "route-ab"}
            if any(tags.get(key) != value for key, value in expected.items()):
                raise RuntimeError(f"Resource-group ownership check failed: {group}")
            if suffix == "shared":
                continue
            machines = self.azure("vm", "list", "--resource-group", group, "--show-details")
            if {vm["name"] for vm in machines} != VM_NAMES or len(machines) != len(VM_NAMES):
                raise RuntimeError(f"Unexpected VM inventory in {group}; no changes are allowed")
            for vm in machines:
                if any((vm.get("tags") or {}).get(key) != value for key, value in expected.items()):
                    raise RuntimeError(f"VM ownership check failed: {group}/{vm['name']}")
                vm_id = f"/subscriptions/{self.subscription}/resourceGroups/{group}/providers/Microsoft.Compute/virtualMachines/{vm['name']}"
                if vm["id"].lower() != vm_id.lower():
                    raise RuntimeError("VM resource ID does not match the requested deployment")
            routes[suffix] = {vm["name"]: vm["powerState"] for vm in machines}
        app = self.azure("containerapp", "show", "--resource-group", self.shared, "--name", self.console)
        if any((app.get("tags") or {}).get(key) != value for key, value in {
                "project": "swu-lab", "deployment": self.prefix}.items()):
            raise RuntimeError("Console ownership check failed")
        properties = app["properties"]
        if properties["configuration"]["activeRevisionsMode"] != "Single":
            raise RuntimeError("Lifecycle only supports the existing single-revision console")
        revision = properties.get("latestReadyRevisionName")
        if not revision or revision != properties.get("latestRevisionName"):
            raise RuntimeError("Console deployment is incomplete; refusing a lifecycle change")
        if not revision.startswith(self.console + "--"):
            raise RuntimeError("Unexpected console revision name")
        revisions = self.azure("containerapp", "revision", "list", "--resource-group", self.shared,
                               "--name", self.console, "--all")
        active = [item["name"] for item in revisions if item["properties"].get("active")]
        if any(item != revision for item in active):
            raise RuntimeError("An unexpected console revision is active; inspect before continuing")
        storage = self.azure("storage", "account", "list", "--resource-group", self.shared)
        owned = [item["name"] for item in storage if all((item.get("tags") or {}).get(key) == value
                 for key, value in {"project": "swu-lab", "deployment": self.prefix}.items())]
        if len(owned) != 1:
            raise RuntimeError("Expected one owned private storage account")
        if not re.fullmatch(r"[a-z0-9]{3,24}", owned[0]):
            raise RuntimeError("Invalid owned storage-account name")
        self.snapshot = {"subscription": self.subscription, "tenant": self.tenant, "prefix": self.prefix,
                         "routes": routes, "consoleRevision": revision, "consoleActive": bool(active),
                         "storageAccount": owned[0]}
        return self.snapshot

    def console_active(self, enabled: bool) -> None:
        self.azure("containerapp", "revision", "activate" if enabled else "deactivate",
                   "--resource-group", self.shared, "--name", self.console,
                   "--revision", self.snapshot["consoleRevision"])
        revisions = self.azure("containerapp", "revision", "list", "--resource-group", self.shared,
                               "--name", self.console, "--all")
        active = {item["name"] for item in revisions if item["properties"].get("active")}
        expected = {self.snapshot["consoleRevision"]} if enabled else set()
        if active != expected:
            raise RuntimeError("Console revision state did not match the requested operation")
        if not enabled:
            deadline = time.monotonic() + 180
            while self.azure("containerapp", "replica", "list", "--resource-group", self.shared,
                             "--name", self.console, "--revision", self.snapshot["consoleRevision"]):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Console replicas have not stopped; worker shutdown is blocked")
                time.sleep(5)

    def power(self, route: str, role: str, running: bool) -> None:
        group, name = f"rg-{self.prefix}-{route}", f"vm-uta-{role}"
        expected = "VM running" if running else "VM deallocated"
        power = self.azure("vm", "show", "--resource-group", group, "--name", name,
                           "--show-details")["powerState"]
        if power == expected:
            return
        self.azure("vm", "start" if running else "deallocate", "--resource-group", group,
                   "--name", name, "--no-wait")
        deadline = time.monotonic() + 600
        while True:
            power = self.azure("vm", "show", "--resource-group", group, "--name", name,
                               "--show-details")["powerState"]
            if power == expected:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{group}/{name} did not reach {expected}")
            time.sleep(5)

    def guest(self, route: str, role: str, action: str) -> dict:
        source = base64.b64encode((ROOT / "scripts" / "route-lifecycle-guest.py").read_bytes()).decode()
        target = f"/tmp/swu-lifecycle-{uuid.uuid4().hex}.py"
        script = (f"set -eu\numask 077\ntrap 'rm -f {target}' EXIT\n"
                  f"printf '%s' '{source}' | base64 -d > {target}\n"
                  f"python3 {target} {action} --role {role} --route {route} "
                  f"--storage {self.snapshot['storageAccount']}\n")
        with tempfile.TemporaryDirectory(prefix="swu-lifecycle-") as directory:
            path = Path(directory) / "guest.sh"
            path.write_text(script, encoding="utf-8")
            response = self.azure("vm", "run-command", "invoke", "--resource-group",
                                  f"rg-{self.prefix}-{route}", "--name", f"vm-uta-{role}",
                                  "--command-id", "RunShellScript", "--scripts", f"@{path}")
        output = "\n".join(item.get("message", "") for item in response.get("value", []))
        matches = re.findall(r"^SWU_LIFECYCLE_JSON=(\{[^\n]*\})", output, re.MULTILINE)
        if len(matches) != 1:
            raise RuntimeError(f"{route}/{role}: guest lifecycle success marker missing; inspect Azure Run Command")
        return json.loads(matches[0])

    def pause(self) -> dict:
        self.console_active(False)
        drained = {}
        # Drain both workers before deallocating anything, so a failed restoration blocks pause.
        for route in ("routea", "routeb"):
            state = self.snapshot["routes"][route]["vm-uta-attack"]
            if state == "VM running":
                drained[route] = self.guest(route, "attack", "pause-runner")
            elif state not in ("VM deallocated", "VM stopped"):
                raise RuntimeError(f"{route}: runner VM is transitional; inspect before pausing")
        for role in ("attack", "gw", "hmi", "plc", "operator"):
            for route in ("routea", "routeb"):
                self.power(route, role, False)
        result = self.inspect()
        if result["consoleActive"] or any(power != "VM deallocated"
                for machines in result["routes"].values() for power in machines.values()):
            raise RuntimeError("Pause readback did not confirm stopped console and deallocated VMs")
        return {**result, "workerDrain": drained}

    def resume(self) -> dict:
        if self.snapshot["consoleActive"] and all(
                self.snapshot["routes"][route][f"vm-uta-{role}"] == "VM running"
                for route in ("routea", "routeb") for role in ROLES):
            health = {route: self.guest(route, "attack", "check-health") for route in ("routea", "routeb")}
            return {**self.inspect(), "workerHealth": health, "alreadyRunning": True}
        # Keep ingress unavailable until recovery, queue cancellation and PLC freshness pass.
        self.console_active(False)
        quiesced_at = time.monotonic()
        for route in ("routea", "routeb"):
            if self.snapshot["routes"][route]["vm-uta-attack"] == "VM running":
                self.guest(route, "attack", "pause-runner")
        health = {}
        for role in ROLES:
            if role == "attack":
                # An externally powered-off VM can auto-start its old worker on boot.
                # Outwait the 300-second job window plus 60-second permitted clock skew.
                remaining = 362 - (time.monotonic() - quiesced_at)
                if remaining > 0:
                    print(f"Waiting {remaining:.0f}s for pre-resume commands to expire before worker VM startup.",
                          flush=True)
                    time.sleep(remaining)
            for route in ("routea", "routeb"):
                self.power(route, role, True)
                result = self.guest(route, role, "resume-role")
                if role == "attack":
                    health[route] = result
        self.console_active(True)
        result = self.inspect()
        if not result["consoleActive"] or any(result["routes"][route][f"vm-uta-{role}"] != "VM running"
                for route in ("routea", "routeb") for role in ROLES):
            raise RuntimeError("Resume readback did not confirm the requested running state")
        return {**result, "workerHealth": health}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "pause", "resume"))
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--prefix", default="swurab")
    parser.add_argument("--azure-config-dir", type=Path)
    parser.add_argument("--apply", action="store_true", help="Without this flag, pause/resume only print a plan")
    parser.add_argument("--confirm", help="For changes, exactly pause:<prefix> or resume:<prefix>")
    parser.add_argument("--output", type=Path, default=ROOT / "test-results" / "route-lifecycle.json")
    args = parser.parse_args()
    if args.action == "status" and args.apply:
        parser.error("status is read-only and does not accept --apply")
    if args.apply and args.confirm != f"{args.action}:{args.prefix}":
        parser.error("--confirm must exactly match <action>:<prefix>")
    if args.azure_config_dir:
        os.environ["AZURE_CONFIG_DIR"] = str(args.azure_config_dir.resolve())
    controller = Lifecycle(args.subscription, args.tenant, args.prefix)
    result = {"action": args.action, "applied": False, "state": "checking",
              "scope": "Route A/B VMs and console only; no deletion, network or authentication changes",
              "remainingCosts": "Managed disks, storage, private endpoints, registry and log retention remain billable."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    try:
        result["before"] = controller.inspect()
        result["state"] = "read-only" if args.action == "status" else "planned"
        result["plan"] = {
            "status": ["Read deployment ownership, power states and console revision state"],
            "pause": ["Stop console replicas", "Drain both workers and verify restored baselines",
                      "Deallocate all ten owned route VMs"],
            "resume": ["Stop console replicas and quiesce any running workers",
                       "Start PLC/HMI/gateway services; outwait old command validity",
                       "Start workers, restore unfinished baselines and cancel queued actions",
                       "Require fresh readings from both routes before activating the console"],
        }[args.action]
        if args.apply:
            result["state"] = "in-progress"
            args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            result["after"] = controller.pause() if args.action == "pause" else controller.resume()
            result.update(applied=True, state="verified")
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        result.update(state="failed", error=str(error), checkedAt=datetime.now(timezone.utc).isoformat())
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        raise
    result["checkedAt"] = datetime.now(timezone.utc).isoformat()
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
