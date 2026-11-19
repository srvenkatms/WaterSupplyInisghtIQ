"""Keep private Bastion tunnels alive; never give the future agent SSH access."""
from __future__ import annotations

import json
import re
import signal
import subprocess
import time
import uuid
from pathlib import Path

TARGETS = {"attack": ("vm-uta-attack", 2201), "gateway": ("vm-uta-gw", 2202)}


def tunnel_command(manifest: dict, role: str) -> list[str]:
    prefix = manifest["prefix"]
    subscription = str(uuid.UUID(manifest["subscription"]))
    group = manifest["utilityAResourceGroup"]
    if not re.fullmatch(r"[a-z][a-z0-9]{2,7}", prefix) or group != f"rg-{prefix}-uta":
        raise ValueError("Unexpected operator network manifest")
    vm, port = ("vm-uta-plc", 2203) if role == "plc" else TARGETS[role]
    return ["az", "network", "bastion", "tunnel", "--subscription", subscription,
            "--resource-group", group, "--name", f"bas-{prefix}-uta",
            "--target-resource-id",
            f"/subscriptions/{subscription}/resourceGroups/{group}/providers/Microsoft.Compute/virtualMachines/{vm}",
            "--resource-port", "22", "--port", str(port), "--only-show-errors"]


def wait_for_ssh(role: str, process: subprocess.Popen, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{role} Bastion tunnel exited; inspect /state/tunnel-{role}.log")
        result = subprocess.run(["ssh", f"swu-{role}", "true"], capture_output=True,
                                text=True, timeout=20)
        if result.returncode == 0:
            return
        last_error = result.stderr.strip()
        time.sleep(3)
    raise RuntimeError(f"{role} SSH readiness failed: {last_error}")


def main() -> None:
    state = Path("/state")
    state.mkdir(parents=True, exist_ok=True)
    ready = state / "ready"
    ready.unlink(missing_ok=True)
    manifest = json.loads(Path("/lab/deployment-outputs.json").read_text())
    commands = {role: tunnel_command(manifest, role) for role in TARGETS}
    subprocess.run(["az", "login", "--identity", "--allow-no-subscriptions", "--output", "none"],
                   check=True, timeout=120)
    processes = {}
    logs = []

    def stop(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for role, command in commands.items():
            log = (state / f"tunnel-{role}.log").open("a", buffering=1)
            logs.append(log)
            processes[role] = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        for role, process in processes.items():
            wait_for_ssh(role, process)
        ready.write_text("Bastion and pinned-key SSH ready\n")
        print("Private operator ready; Azure CLI uses this VM's managed identity.", flush=True)
        next_probe = time.monotonic() + 30
        while True:
            for role, process in processes.items():
                if process.poll() is not None:
                    raise RuntimeError(f"{role} Bastion tunnel exited; inspect /state/tunnel-{role}.log")
            if time.monotonic() >= next_probe:
                for role, process in processes.items():
                    wait_for_ssh(role, process, timeout=30)
                next_probe = time.monotonic() + 30
            time.sleep(3)
    finally:
        ready.unlink(missing_ok=True)
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
