"""Private, fixed-action route runner. Never accepts scripts, addresses or credentials from jobs."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import http.cookiejar
import json
import logging
import os
from pathlib import Path
import sqlite3
import ssl
import time
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPCookieProcessor, HTTPSHandler, Request
import uuid

from azure.identity import ManagedIdentityCredential
from azure.storage.blob import ContainerClient
from azure.storage.queue import QueueClient
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from .registers import MAP, READ_COUNT, READ_START, encode, from_block

ACTIONS = {"pump-on": {"PumpOverride": 1.0}, "chlorine-up": {"ClDoseSetpoint": 4.0}}
DURATION = 20


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_job(job: dict, route: str) -> None:
    if not isinstance(job, dict) or set(job) != {"runId", "route", "action", "actor", "issuedAt", "confirmed"}:
        raise ValueError("Invalid live command fields")
    if not all(isinstance(job[key], str) for key in ("runId", "route", "action", "actor", "issuedAt")):
        raise ValueError("Invalid live command types")
    uuid.UUID(job["runId"])
    uuid.UUID(job["actor"])
    if job["route"] != route or job["action"] not in ACTIONS or job["confirmed"] is not True:
        raise ValueError("Unapproved route or action")
    issued = datetime.fromisoformat(job["issuedAt"])
    if issued.tzinfo is None or not -60 <= (datetime.now(timezone.utc) - issued).total_seconds() <= 300:
        raise ValueError("Live command expired or has an invalid timestamp")


class DirectPlc:
    def __init__(self, host: str):
        self.client = ModbusTcpClient(host, timeout=3, retries=0)

    def read(self) -> dict:
        for attempt in range(2):
            try:
                if not self.client.connected and not self.client.connect():
                    raise ConnectionError("Direct PLC connection failed")
                response = self.client.read_holding_registers(READ_START, count=READ_COUNT)
                if response.isError():
                    raise ConnectionError(f"Direct PLC read failed: {response}")
                return from_block(response.registers)
            except (ModbusException, OSError):
                self.client.close()
                if attempt:
                    raise
                logging.warning("Direct PLC read failed; reconnecting once for a read-only retry", exc_info=True)

    def write(self, tag: str, value: float) -> None:
        self.read()
        try:
            response = self.client.write_register(MAP[tag], encode(value))
            if response.isError():
                raise ConnectionError(f"Direct PLC write failed: {response}")
        except (ModbusException, OSError):
            self.client.close()
            raise


class PasswordHmi:
    def __init__(self, url: str, credentials: Path, ca: Path):
        self.url = url
        self.credentials = json.loads(credentials.read_text())
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()),
                                  HTTPSHandler(context=ssl.create_default_context(cafile=str(ca))))
        self.csrf = None

    def request(self, path: str, body: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.csrf:
            headers["X-SWU-CSRF"] = self.csrf
        with self.opener.open(Request(self.url + path, headers=headers,
                                    data=json.dumps(body).encode() if body is not None else None), timeout=10) as response:
            return json.load(response)

    def login(self) -> None:
        self.csrf = self.request("/api/session")["csrf"]
        response = self.request("/api/login", self.credentials)
        if response.get("authenticated") is not True or response.get("mfa") is not False:
            raise ConnectionError("Password-only HMI authentication was not verified")
        self.csrf = response["csrf"]
        logging.info("Route B HMI password session established; MFA is not enabled")

    def read(self) -> dict:
        try:
            return self.request("/api/values")
        except HTTPError as error:
            if error.code != 401:
                raise
            self.login()
            return self.request("/api/values")

    def write(self, tag: str, value: float) -> None:
        self.read()
        self.request("/api/setpoint", {"tag": tag, "value": value})


class Runner:
    def __init__(self, route: str, client, container, queue, audit_queue, directory: Path):
        self.route, self.client, self.container, self.queue, self.audit_queue = route, client, container, queue, audit_queue
        directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "commands.sqlite")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, document TEXT NOT NULL, baseline TEXT, finished INTEGER NOT NULL DEFAULT 0)")
        self.db.execute("CREATE TABLE IF NOT EXISTS audit (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        if "result" not in {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN result TEXT")
        self.db.commit()
        self.samples = deque(maxlen=120)
        self.last_snapshot = 0.0
        self.active = None
        self.startup_recovery = True

    def publish(self, name: str, document: dict) -> None:
        self.container.upload_blob(name, json.dumps(document), overwrite=True)

    def save_result(self, result: dict) -> None:
        with self.db:
            self.db.execute("UPDATE jobs SET result=? WHERE id=?",
                            (json.dumps(result), result["runId"]))
        self.publish(f"jobs/{result['runId']}.json", result)

    def audit(self, job: dict, operation: str, result: str, detail: dict) -> None:
        identity = str(uuid.uuid4())
        record = {"TimeGenerated": now(), "UtilityId": "uta", "RecordId": identity,
                  "ScenarioRunId": job["runId"], "OperationName": operation,
                  "InitiatedBy": job["actor"], "TargetUser": json.dumps({"route": self.route, **detail}),
                  "Result": result}
        self.db.execute("INSERT INTO audit VALUES (?,?)", (identity, json.dumps({"SimAudit_CL": [record]})))
        self.db.commit()

    def flush_audit(self) -> None:
        for identity, payload in self.db.execute("SELECT id,payload FROM audit").fetchall():
            self.audit_queue.send_message(payload, time_to_live=604800)
            self.db.execute("DELETE FROM audit WHERE id=?", (identity,))
            self.db.commit()

    def snapshot(self, force: bool = False) -> dict:
        values = self.client.read()
        stamp = now()
        self.samples.append({"at": stamp, **values})
        if force or time.monotonic() - self.last_snapshot >= 5:
            self.publish("latest.json", {"route": self.route, "observedAt": stamp, "values": values,
                                        "path": "direct-modbus" if self.route == "routea" else "password-hmi",
                                        "samples": list(self.samples), "activeRunId": self.active, "error": None})
            self.last_snapshot = time.monotonic()
        return values

    def restore(self, job: dict, baseline: dict) -> None:
        for tag, value in baseline.items():
            self.client.write(tag, value)
        time.sleep(1)
        observed = self.snapshot(force=True)
        if any(abs(observed[tag] - value) > 0.005 for tag, value in baseline.items()):
            raise RuntimeError("Baseline restoration did not read back from the PLC")
        self.audit(job, "LabBaselineRestored", "Success", baseline)

    def recover(self) -> None:
        for identity, document, baseline, saved in self.db.execute(
                "SELECT id,document,baseline,result FROM jobs WHERE finished=0").fetchall():
            job = json.loads(document)
            result = json.loads(saved) if saved else {
                **job, "baseline": json.loads(baseline) if baseline else {},
                "changes": ACTIONS[job["action"]], "durationSeconds": DURATION}
            self.active = identity
            if baseline:
                self.restore(job, json.loads(baseline))
            previous_error = result.get("error")
            result.update(stage="interrupted-restored", restoredAt=now(),
                          recoveryReason="startup" if self.startup_recovery else "unfinished-action",
                          error=(f"{previous_error} " if previous_error else "") +
                          "An unfinished action was recovered; its baseline was restored without replaying the action.")
            self.save_result(result)
            self.db.execute("UPDATE jobs SET finished=1 WHERE id=?", (identity,))
            self.db.commit()
            if self.active == identity:
                self.active = None
        self.startup_recovery = False

    def execute(self, job: dict) -> None:
        validate_job(job, self.route)
        if self.db.execute("SELECT 1 FROM jobs WHERE id=?", (job["runId"],)).fetchone():
            return
        changes = ACTIONS[job["action"]]
        before = self.snapshot()
        baseline = {tag: before[tag] for tag in changes}
        with self.db:
            self.db.execute("INSERT INTO jobs (id,document,baseline) VALUES (?,?,?)",
                            (job["runId"], json.dumps(job), json.dumps(baseline)))
        self.active = job["runId"]
        result = {**job, "stage": "executing", "startedAt": now(), "baseline": baseline,
                  "changes": changes, "durationSeconds": DURATION, "sentinel": "not-yet-queried",
                  "alert": "not-yet-queried"}
        failure = None
        try:
            self.save_result(result)
            self.audit(job, "LabControlRequested", "Accepted", {"action": job["action"]})
            for tag, value in changes.items():
                self.client.write(tag, value)
            time.sleep(1)
            observed = self.snapshot(force=True)
            if any(abs(observed[tag] - value) > 0.005 for tag, value in changes.items()):
                raise RuntimeError("Setpoint change did not read back from the PLC")
            result.update(stage="plc-observed", observedAt=now(), observed=observed)
            self.audit(job, "LabSetpointObserved", "Success", {"old": baseline, "new": changes})
            self.save_result(result)
            deadline = time.monotonic() + DURATION
            while time.monotonic() < deadline:
                time.sleep(1)
                self.snapshot()
        except Exception as error:
            logging.exception("Bounded route action failed")
            failure = str(error)
            result["error"] = failure
        finally:
            # Retain readback and failure details even if restoration must be recovered later.
            with self.db:
                self.db.execute("UPDATE jobs SET result=? WHERE id=?",
                                (json.dumps(result), job["runId"]))
            self.restore(job, baseline)
            result.update(stage="failed-restored" if failure else "completed-restored",
                          restoredAt=now(), error=failure)
            self.save_result(result)
            with self.db:
                self.db.execute("UPDATE jobs SET finished=1 WHERE id=?", (job["runId"],))
            self.active = None

    def step(self) -> None:
        # A failed restore leaves the journal unfinished and blocks every later action.
        self.recover()
        self.flush_audit()
        self.snapshot()
        for message in self.queue.receive_messages(messages_per_page=1, visibility_timeout=300):
            try:
                job = json.loads(message.content)
                self.execute(job)
            except (ValueError, KeyError, TypeError):
                logging.exception("Rejected invalid or expired live command")
                self.queue.delete_message(message)
                break
            self.queue.delete_message(message)
            self.flush_audit()
            break


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    route = os.environ["SWU_ROUTE"]
    if route not in ("routea", "routeb"):
        raise ValueError("Unknown route")
    storage = os.environ["SWU_STORAGE_ACCOUNT"]
    credential = ManagedIdentityCredential()
    container = ContainerClient(f"https://{storage}.blob.core.windows.net", f"{route}-state", credential)
    queue = QueueClient(f"https://{storage}.queue.core.windows.net", f"{route}-commands", credential)
    audits = QueueClient(f"https://{storage}.queue.core.windows.net", f"{route}-audit", credential)
    client = (DirectPlc(os.environ["SWU_PLC_HOST"]) if route == "routea" else
              PasswordHmi(os.environ["SWU_HMI_URL"], Path("/private/hmi.json"), Path("/private/hmi.crt")))
    runner = Runner(route, client, container, queue, audits, Path("/state"))
    while True:
        try:
            runner.step()
        except Exception as error:
            logging.exception("Route worker unavailable; no success or fresh PLC values asserted")
            try:
                runner.publish("error.json", {"at": now(), "error": str(error)})
            except Exception:
                logging.exception("Could not publish the route worker error")
        time.sleep(2)


if __name__ == "__main__":
    main()
