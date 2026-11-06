from datetime import datetime, timedelta, timezone
import json
import uuid
import sqlite3
from unittest.mock import Mock

import pytest
from werkzeug.security import generate_password_hash

from swuapps import hmi, route_worker
from pymodbus.exceptions import ModbusIOException


@pytest.fixture
def password_hmi(monkeypatch):
    monkeypatch.setattr(hmi, "AUTH", "password")
    monkeypatch.setenv("SWU_HMI_USERNAME", "operator")
    monkeypatch.setenv("SWU_HMI_PASSWORD_HASH", generate_password_hash("test-only-password"))
    monkeypatch.setitem(hmi.app.config, "SECRET_KEY", "a" * 48)
    monkeypatch.setitem(hmi.app.config, "SESSION_COOKIE_SECURE", True)
    return hmi.app.test_client()


def test_password_hmi_requires_https_login_and_csrf(password_hmi):
    client = password_hmi
    assert client.get("/api/values", base_url="http://hmi.local").status_code == 403
    assert client.get("/api/values", base_url="https://hmi.local").status_code == 401
    session = client.get("/api/session", base_url="https://hmi.local").get_json()
    body = {"username": "operator", "password": "test-only-password"}
    assert client.post("/api/login", json=body, base_url="https://hmi.local").status_code == 403
    headers = {"X-SWU-CSRF": session["csrf"]}
    assert client.post("/api/login", json={**body, "password": "wrong"}, headers=headers,
                       base_url="https://hmi.local").status_code == 401
    response = client.post("/api/login", json=body, headers=headers, base_url="https://hmi.local")
    assert response.status_code == 200
    assert response.get_json()["mfa"] is False
    csrf = response.get_json()["csrf"]
    assert csrf != session["csrf"]
    assert "Secure" in response.headers["Set-Cookie"]
    assert client.post("/api/setpoint", json={"tag": "ClDoseSetpoint", "value": 20},
                       headers={"X-SWU-CSRF": csrf}, base_url="https://hmi.local").status_code == 400
    assert client.post("/api/setpoint", json={"tag": "ClDoseSetpoint", "value": 2},
                       headers={"X-SWU-CSRF": csrf, "Origin": "https://evil.invalid"},
                       base_url="https://hmi.local").status_code == 403


class Plc:
    def __init__(self):
        self.values = {"PumpOverride": 0.0, "ClDoseSetpoint": 1.2}
        self.writes = []
        self.fail_restore = False

    def read(self):
        return dict(self.values)

    def write(self, tag, value):
        if self.fail_restore and value == 0:
            raise ConnectionError("Restore is unavailable")
        self.writes.append((tag, value))
        self.values[tag] = value


class Container:
    def __init__(self):
        self.documents = {}

    def upload_blob(self, name, content, overwrite):
        self.documents[name] = json.loads(content)


class Audit:
    def __init__(self):
        self.messages = []

    def send_message(self, content, **kwargs):
        self.messages.append(json.loads(content))


def job():
    return {"runId": str(uuid.uuid4()), "route": "routea", "action": "pump-on",
            "actor": str(uuid.uuid4()), "issuedAt": datetime.now(timezone.utc).isoformat(),
            "confirmed": True}


def test_actions_are_fixed_and_jobs_bound_to_route_and_time():
    assert route_worker.DURATION == 20
    assert route_worker.ACTIONS == {"pump-on": {"PumpOverride": 1.0}, "chlorine-up": {"ClDoseSetpoint": 4.0}}
    for changed in ({"route": "routeb"}, {"action": "shell"}, {"confirmed": False},
                    {"issuedAt": (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()},
                    {"host": "arbitrary"}):
        with pytest.raises(ValueError):
            route_worker.validate_job({**job(), **changed}, "routea")


def test_real_readback_restore_and_duplicate_suppression(tmp_path, monkeypatch):
    monkeypatch.setattr(route_worker, "DURATION", 0)
    monkeypatch.setattr(route_worker.time, "sleep", lambda _: None)
    plc, blobs, audit = Plc(), Container(), Audit()
    runner = route_worker.Runner("routea", plc, blobs, None, audit, tmp_path)
    command = job()
    runner.execute(command)
    runner.execute(command)
    assert plc.writes == [("PumpOverride", 1.0), ("PumpOverride", 0.0)]
    result = blobs.documents[f"jobs/{command['runId']}.json"]
    assert result["stage"] == "completed-restored"
    assert result["observed"]["PumpOverride"] == 1
    assert result["sentinel"] == "not-yet-queried"
    runner.flush_audit()
    assert len(audit.messages) == 3


def test_crash_recovery_blocks_until_baseline_restored(tmp_path, monkeypatch):
    monkeypatch.setattr(route_worker, "DURATION", 0)
    monkeypatch.setattr(route_worker.time, "sleep", lambda _: None)
    plc, blobs = Plc(), Container()
    runner = route_worker.Runner("routea", plc, blobs, None, Audit(), tmp_path)
    command = job()
    plc.fail_restore = True
    with pytest.raises(ConnectionError):
        runner.execute(command)
    assert runner.db.execute("SELECT finished FROM jobs").fetchone()[0] == 0
    with pytest.raises(ConnectionError):
        runner.recover()
    plc.fail_restore = False
    runner.recover()
    assert plc.values["PumpOverride"] == 0
    assert runner.db.execute("SELECT finished FROM jobs").fetchone()[0] == 1
    assert blobs.documents[f"jobs/{command['runId']}.json"]["stage"] == "interrupted-restored"
    result = blobs.documents[f"jobs/{command['runId']}.json"]
    assert result["action"] == "pump-on"
    assert result["baseline"] == {"PumpOverride": 0}
    assert result["observed"]["PumpOverride"] == 1
    assert result["recoveryReason"] == "startup"
    assert "restarted" not in result["error"]
    assert plc.writes.count(("PumpOverride", 1.0)) == 1


def test_in_process_recovery_does_not_claim_a_runner_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(route_worker, "DURATION", 0)
    monkeypatch.setattr(route_worker.time, "sleep", lambda _: None)
    plc, blobs = Plc(), Container()
    runner = route_worker.Runner("routea", plc, blobs, None, Audit(), tmp_path)
    runner.recover()
    plc.fail_restore = True
    command = job()
    with pytest.raises(ConnectionError):
        runner.execute(command)
    plc.fail_restore = False
    runner.recover()
    result = blobs.documents[f"jobs/{command['runId']}.json"]
    assert result["recoveryReason"] == "unfinished-action"
    assert result["observed"]["PumpOverride"] == 1
    assert result["baseline"]["PumpOverride"] == 0
    assert "restarted" not in result["error"]
    assert runner.active is None


def test_migrates_existing_journal_and_recovers_without_replaying(tmp_path, monkeypatch):
    monkeypatch.setattr(route_worker.time, "sleep", lambda _: None)
    command = job()
    with sqlite3.connect(tmp_path / "commands.sqlite") as db:
        db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY,document TEXT NOT NULL,baseline TEXT,finished INTEGER NOT NULL DEFAULT 0)")
        db.execute("INSERT INTO jobs(id,document,baseline) VALUES(?,?,?)",
                   (command["runId"], json.dumps(command), '{"PumpOverride":0}'))
    plc, blobs = Plc(), Container()
    plc.values["PumpOverride"] = 1
    runner = route_worker.Runner("routea", plc, blobs, None, Audit(), tmp_path)
    runner.recover()
    result = blobs.documents[f"jobs/{command['runId']}.json"]
    assert result["action"] == "pump-on" and result["durationSeconds"] == 20
    assert result["baseline"] == {"PumpOverride": 0}
    assert result["stage"] == "interrupted-restored"
    assert plc.writes == [("PumpOverride", 0)]


def test_failed_action_and_failed_restore_keep_original_failure_for_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(route_worker, "DURATION", 0)
    monkeypatch.setattr(route_worker.time, "sleep", lambda _: None)
    plc, blobs = Plc(), Container()
    read = plc.read
    reads = 0

    def fail_readback():
        nonlocal reads
        reads += 1
        if reads == 2:
            raise ConnectionError("PLC readback transport failed")
        return read()

    plc.read = fail_readback
    plc.fail_restore = True
    runner = route_worker.Runner("routea", plc, blobs, None, Audit(), tmp_path)
    runner.recover()
    command = job()
    with pytest.raises(ConnectionError):
        runner.execute(command)
    plc.fail_restore = False
    runner.recover()
    result = blobs.documents[f"jobs/{command['runId']}.json"]
    assert "PLC readback transport failed" in result["error"]
    assert result["stage"] == "interrupted-restored"
    assert "observed" not in result
    assert plc.writes.count(("PumpOverride", 1.0)) == 1


def test_direct_plc_reconnects_once_for_reads_only(monkeypatch):
    client = Mock(connected=True)
    success = Mock(registers=[0] * route_worker.READ_COUNT)
    success.isError.return_value = False
    client.read_holding_registers.side_effect = [ModbusIOException("lost connection"), success]
    monkeypatch.setattr(route_worker, "ModbusTcpClient", Mock(return_value=client))
    plc = route_worker.DirectPlc("127.0.0.1")
    assert plc.read()["PumpOverride"] == 0
    assert client.read_holding_registers.call_count == 2
    client.close.assert_called_once()


def test_direct_plc_read_failure_is_bounded_and_write_failure_is_not_replayed(monkeypatch):
    client = Mock(connected=True)
    client.read_holding_registers.side_effect = ModbusIOException("PLC unavailable")
    monkeypatch.setattr(route_worker, "ModbusTcpClient", Mock(return_value=client))
    plc = route_worker.DirectPlc("127.0.0.1")
    with pytest.raises(ModbusIOException):
        plc.read()
    assert client.read_holding_registers.call_count == 2
    success = Mock(registers=[0] * route_worker.READ_COUNT)
    success.isError.return_value = False
    client.read_holding_registers.side_effect = None
    client.read_holding_registers.return_value = success
    client.write_register.side_effect = ModbusIOException("write response lost")
    with pytest.raises(ModbusIOException):
        plc.write("PumpOverride", 1)
    client.write_register.assert_called_once()
