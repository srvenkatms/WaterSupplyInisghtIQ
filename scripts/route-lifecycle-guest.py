"""Fixed guest operations used by route-lifecycle.py; no user-supplied scripts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import time

CONTAINERS = {
    "plc": ("swu-plc", "swu-plant", "swu-proxy"),
    "hmi": ("swu-hmi",),
    "gw": ("swu-gateway", "swu-audit-relay"),
    "attack": ("swu-runner",),
}
RECOVER_AND_CANCEL = r"""
import json, os, uuid
from pathlib import Path
from azure.identity import ManagedIdentityCredential
from azure.storage.blob import ContainerClient
from azure.storage.queue import QueueClient
from swuapps.route_worker import Runner, DirectPlc, PasswordHmi, ACTIONS, now
route, storage = os.environ["SWU_ROUTE"], os.environ["SWU_STORAGE_ACCOUNT"]
credential = ManagedIdentityCredential()
blobs = ContainerClient(f"https://{storage}.blob.core.windows.net", f"{route}-state", credential)
queue = QueueClient(f"https://{storage}.queue.core.windows.net", f"{route}-commands", credential)
audits = QueueClient(f"https://{storage}.queue.core.windows.net", f"{route}-audit", credential)
client = (DirectPlc("10.30.1.10") if route == "routea" else
          PasswordHmi("https://10.40.1.11:1881", Path("/private/hmi.json"), Path("/private/hmi.crt")))
runner = Runner(route, client, blobs, queue, audits, Path("/state"))
runner.recover()
cancelled = 0
for message in queue.receive_messages(messages_per_page=1, visibility_timeout=300):
    if cancelled >= 100:
        raise RuntimeError("Too many queued messages; resume remains blocked")
    job = json.loads(message.content)
    identity = str(uuid.UUID(job["runId"]))
    if job["route"] != route or job["action"] not in ACTIONS or identity != job["runId"]:
        raise RuntimeError("Invalid queued command; inspect before resuming")
    existing = runner.db.execute("SELECT finished FROM jobs WHERE id=?", (identity,)).fetchone()
    if existing is None:
        result = {**job, "stage": "expired-unobserved", "cancelledAt": now(),
                  "error": "Cancelled during lifecycle resume; the queued action was not executed."}
        with runner.db:
            runner.db.execute("INSERT INTO jobs(id,document,finished) VALUES(?,?,1)",
                              (identity, json.dumps(job)))
            runner.db.execute("UPDATE jobs SET result=? WHERE id=?", (json.dumps(result), identity))
            runner.audit(job, "LabControlCancelled", "NotExecuted", {"reason": "lifecycle-resume"})
    elif not existing[0]:
        raise RuntimeError("Unfinished journal entry still blocks resume")
    saved = runner.db.execute("SELECT result FROM jobs WHERE id=?", (identity,)).fetchone()[0]
    if saved:
        runner.publish(f"jobs/{identity}.json", json.loads(saved))
    queue.delete_message(message)
    cancelled += 1
runner.flush_audit()
print(json.dumps({"cancelledOrDeduplicatedMessages": cancelled, "baselineRecoveryCompleted": True}))
"""
CHECK_HEALTH = r"""
import json, os, time
from datetime import datetime, timezone
from azure.core.exceptions import AzureError
from azure.identity import ManagedIdentityCredential
from azure.storage.blob import ContainerClient
route, storage = os.environ["SWU_ROUTE"], os.environ["SWU_STORAGE_ACCOUNT"]
blob = ContainerClient(f"https://{storage}.blob.core.windows.net", f"{route}-state", ManagedIdentityCredential())
deadline = time.monotonic() + 150
while True:
    try:
        state = json.loads(blob.download_blob("latest.json").readall())
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(state["observedAt"])).total_seconds()
        expected = "direct-modbus" if route == "routea" else "password-hmi"
        if (state["route"] == route and state["path"] == expected and not state.get("error")
                and 0 <= age <= 20 and datetime.fromisoformat(state["observedAt"]) >
                    datetime.fromisoformat(os.environ["SWU_RESUME_STARTED_AT"])):
            print(json.dumps({"route": route, "observedAt": state["observedAt"],
                              "ageSeconds": round(age, 1), "path": state["path"]}))
            break
    except (AzureError, ValueError, KeyError) as error:
        print("Waiting for worker health: " + type(error).__name__, flush=True)
    if time.monotonic() >= deadline:
        raise TimeoutError("Fresh post-start PLC observations did not become available")
    time.sleep(5)
"""


def docker(*args: str, timeout: int = 240) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Guest Docker {args[0]} failed: {result.stderr[-2000:]}")
    return result.stdout.strip()


def pause_runner(directory: Path = Path("/opt/swu/state"), timeout: int = 180) -> None:
    journal = directory / "commands.sqlite"
    if not journal.is_file():
        raise RuntimeError("Runner journal is missing; refusing an unverified stop")
    deadline = time.monotonic() + timeout
    while True:
        with sqlite3.connect(f"file:{journal}?mode=rw", uri=True, timeout=10) as db:
            # Hold the write lock until stopped: no new action can journal/write its baseline.
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute("SELECT count(*) FROM jobs WHERE finished=0").fetchone()[0]
            if pending == 0:
                docker("stop", "--time", "30", "swu-runner")
                return
        if time.monotonic() >= deadline:
            raise TimeoutError("An unfinished action still needs restoration; VM deallocation is blocked")
        print("Waiting for the active action to restore its baseline.", flush=True)
        time.sleep(3)


def resume_role(role: str, route: str, storage: str) -> dict:
    from datetime import datetime, timezone
    started = datetime.now(timezone.utc).isoformat()
    if role == "attack":
        image = docker("inspect", "-f", "{{.Config.Image}}", "swu-runner")
        if docker("inspect", "-f", "{{.State.Running}}", "swu-runner") == "true":
            pause_runner()
        recovery = docker(
            "run", "--rm", "--network", "host", "-v", "/opt/swu/state:/state",
            "-v", "/opt/swu/private:/private:ro", "-e", f"SWU_ROUTE={route}",
            "-e", f"SWU_STORAGE_ACCOUNT={storage}", "--entrypoint", "python",
            image, "-c", RECOVER_AND_CANCEL)
        recovery_result = json.loads(recovery.splitlines()[-1])
    else:
        recovery_result = {}
    for name in CONTAINERS[role]:
        docker("start", name)
        if docker("inspect", "-f", "{{.State.Running}}", name) != "true":
            raise RuntimeError(f"Container {name} is not running")
    if role == "attack":
        health = docker("exec", "-e", f"SWU_RESUME_STARTED_AT={started}",
                        "swu-runner", "python", "-c", CHECK_HEALTH)
        return {**recovery_result, "health": json.loads(health.splitlines()[-1])}
    if role == "plc":
        deadline = time.monotonic() + 150
        while docker("inspect", "-f", "{{.State.Health.Status}}", "swu-plc") != "healthy":
            if time.monotonic() >= deadline:
                raise TimeoutError("Actual OpenPLC container did not become healthy")
            time.sleep(3)
    return {"runningContainers": list(CONTAINERS[role])}


def check_health() -> dict:
    from datetime import datetime, timezone
    started = datetime.now(timezone.utc).isoformat()
    health = docker("exec", "-e", f"SWU_RESUME_STARTED_AT={started}",
                    "swu-runner", "python", "-c", CHECK_HEALTH)
    return {"health": json.loads(health.splitlines()[-1])}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pause-runner", "resume-role", "check-health"))
    parser.add_argument("--role", choices=CONTAINERS, required=True)
    parser.add_argument("--route", choices=("routea", "routeb"), required=True)
    parser.add_argument("--storage", required=True)
    args = parser.parse_args()
    if args.action == "pause-runner":
        if args.role != "attack":
            raise ValueError("Only the runner has a command journal")
        pause_runner()
        result = {"runnerStoppedWithNoUnfinishedActions": True}
    elif args.action == "check-health":
        if args.role != "attack":
            raise ValueError("Only the runner publishes route observations")
        result = check_health()
    else:
        result = resume_role(args.role, args.route, args.storage)
    print("SWU_LIFECYCLE_JSON=" + json.dumps(result))


if __name__ == "__main__":
    main()
