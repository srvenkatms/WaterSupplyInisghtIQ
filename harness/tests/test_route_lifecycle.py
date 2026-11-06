import json
from pathlib import Path
import runpy
import sqlite3
import sys
from types import SimpleNamespace
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE = runpy.run_path(str(ROOT / "scripts" / "route-lifecycle.py"))
GUEST = runpy.run_path(str(ROOT / "scripts" / "route-lifecycle-guest.py"))
Lifecycle = MODULE["Lifecycle"]
SUB = "11111111-2222-4333-8444-555555555555"
TENANT = "22222222-3333-4444-8555-666666666666"
TAGS = {"project": "swu-lab", "deployment": "swurab", "environment": "route-ab"}
REVISION = "ca-swurab-console--test"


class Cloud:
    def __init__(self):
        self.calls = []
        self.active = True
        self.machines = {
            route: [{"name": name, "id": f"/subscriptions/{SUB}/resourceGroups/rg-swurab-{route}"
                      f"/providers/Microsoft.Compute/virtualMachines/{name}", "tags": dict(TAGS),
                     "powerState": "VM deallocated" if name.endswith("operator") else "VM running"}
                    for name in sorted(MODULE["VM_NAMES"])]
            for route in ("routea", "routeb")}
        self.account = {"id": SUB, "tenantId": TENANT, "state": "Enabled"}
        self.tags = dict(TAGS)
        self.fail_role = None

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("account", "show"):
            return self.account
        if args[:2] == ("group", "show"):
            return {"tags": self.tags}
        if args[:2] == ("vm", "list"):
            return self.machines[args[args.index("--resource-group") + 1].rsplit("-", 1)[-1]]
        if args[:2] == ("containerapp", "show"):
            return {"tags": TAGS, "properties": {"configuration": {"activeRevisionsMode": "Single"},
                    "latestReadyRevisionName": REVISION, "latestRevisionName": REVISION}}
        if args[:3] == ("containerapp", "revision", "list"):
            return [{"name": REVISION, "properties": {"active": self.active}}]
        if args[:3] == ("containerapp", "replica", "list"):
            return []
        if args[:3] in (("containerapp", "revision", "activate"), ("containerapp", "revision", "deactivate")):
            self.active = args[2] == "activate"
            return {}
        if args[:3] == ("storage", "account", "list"):
            return [{"name": "testprivatestorage", "tags": TAGS}]
        if args[0] == "vm" and args[1] in ("show", "start", "deallocate"):
            route = args[args.index("--resource-group") + 1].rsplit("-", 1)[-1]
            name = args[args.index("--name") + 1]
            machine = next(vm for vm in self.machines[route] if vm["name"] == name)
            if args[1] != "show":
                if name.endswith(self.fail_role or "not-a-role"):
                    raise RuntimeError("Simulated VM failure")
                machine["powerState"] = "VM running" if args[1] == "start" else "VM deallocated"
            return machine
        raise AssertionError(f"Unexpected Azure call: {args}")


def controller(monkeypatch, cloud=None):
    cloud = cloud or Cloud()
    instance = Lifecycle(SUB, TENANT, "swurab")
    monkeypatch.setattr(instance, "azure", cloud)
    instance.inspect()
    return instance, cloud


@pytest.mark.parametrize("change", ["tenant", "group-tags", "vm-tags", "inventory", "vm-id"])
def test_preflight_rejects_mismatches_before_any_mutation(monkeypatch, change):
    cloud = Cloud()
    if change == "tenant":
        cloud.account["tenantId"] = SUB
    elif change == "group-tags":
        cloud.tags["deployment"] = "other"
    elif change == "vm-tags":
        cloud.machines["routeb"][0]["tags"]["deployment"] = "other"
    elif change == "inventory":
        cloud.machines["routea"].pop()
    else:
        cloud.machines["routea"][0]["id"] = "/subscriptions/unrelated/vm"
    instance = Lifecycle(SUB, TENANT, "swurab")
    monkeypatch.setattr(instance, "azure", cloud)
    with pytest.raises(RuntimeError):
        instance.inspect()
    assert all(call[1] in ("show", "list", "account", "revision") for call in cloud.calls)
    assert not any("activate" in call or "deallocate" in call or "start" in call for call in cloud.calls)


def test_pause_drains_both_routes_before_deallocation_and_is_repeatable(monkeypatch):
    instance, cloud = controller(monkeypatch)
    guest_calls = []
    def guest(route, role, action):
        assert not cloud.active
        assert not any(call[:2] == ("vm", "deallocate") for call in cloud.calls)
        guest_calls.append((route, role, action))
        return {"runnerStoppedWithNoUnfinishedActions": True}
    monkeypatch.setattr(instance, "guest", guest)
    result = instance.pause()
    assert [route for route, _, _ in guest_calls] == ["routea", "routeb"]
    assert not result["consoleActive"]
    assert all(state == "VM deallocated" for machines in result["routes"].values() for state in machines.values())
    deallocations = [call for call in cloud.calls if call[:2] == ("vm", "deallocate")]
    assert len(deallocations) == 8  # Operators were already deallocated.
    instance.pause()
    assert len([call for call in cloud.calls if call[:2] == ("vm", "deallocate")]) == 8


def test_failed_drain_keeps_console_off_and_does_not_deallocate(monkeypatch):
    instance, cloud = controller(monkeypatch)
    def unfinished(*args):
        raise TimeoutError("restoration failed")
    monkeypatch.setattr(instance, "guest", unfinished)
    with pytest.raises(TimeoutError):
        instance.pause()
    assert not cloud.active
    assert not any(call[:2] == ("vm", "deallocate") for call in cloud.calls)


def test_resume_orders_services_cancels_before_reactivation_and_leaves_operators_off(monkeypatch):
    cloud = Cloud()
    cloud.active = False
    for machines in cloud.machines.values():
        for vm in machines:
            vm["powerState"] = "VM deallocated"
    instance, cloud = controller(monkeypatch, cloud)
    clock = [0.0]
    monkeypatch.setattr(MODULE["time"], "monotonic", lambda: clock[0])
    monkeypatch.setattr(MODULE["time"], "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    calls = []
    def guest(route, role, action):
        assert not cloud.active
        if role == "attack":
            assert clock[0] >= 362
        calls.append((route, role, action))
        return {"health": {"route": route, "ageSeconds": 1}}
    monkeypatch.setattr(instance, "guest", guest)
    result = instance.resume()
    assert [role for _, role, _ in calls] == ["plc", "plc", "hmi", "hmi", "gw", "gw", "attack", "attack"]
    assert result["consoleActive"]
    assert len(result["workerHealth"]) == 2
    assert all(result["routes"][route]["vm-uta-operator"] == "VM deallocated" for route in ("routea", "routeb"))


def test_already_running_resume_only_reads_fresh_health(monkeypatch):
    instance, cloud = controller(monkeypatch)
    guests = []
    monkeypatch.setattr(instance, "guest", lambda *args: guests.append(args) or {"health": "fresh"})
    assert instance.resume()["alreadyRunning"]
    assert [action for _, _, action in guests] == ["check-health", "check-health"]
    assert not any("activate" in call or "deactivate" in call or "start" in call for call in cloud.calls)


def test_failed_health_or_partial_start_never_reactivates_console(monkeypatch):
    instance, cloud = controller(monkeypatch)
    cloud.active = False
    for vm in cloud.machines["routeb"]:
        vm["powerState"] = "VM deallocated"
    instance.inspect()
    cloud.fail_role = "hmi"
    monkeypatch.setattr(instance, "guest", lambda *args: {})
    with pytest.raises(RuntimeError, match="VM failure"):
        instance.resume()
    assert not cloud.active
    assert not any(call[:3] == ("containerapp", "revision", "activate") for call in cloud.calls)


def test_console_must_have_zero_replicas_before_stopping_workers(monkeypatch):
    instance, cloud = controller(monkeypatch)
    def azure(*args):
        if args[:3] == ("containerapp", "replica", "list"):
            return [{"name": "still-draining"}]
        return cloud(*args)
    monkeypatch.setattr(instance, "azure", azure)
    times = iter((0, 181))
    monkeypatch.setattr(MODULE["time"], "monotonic", lambda: next(times))
    monkeypatch.setattr(instance, "guest", lambda *args: pytest.fail("Worker must not stop before replicas"))
    with pytest.raises(TimeoutError, match="replicas"):
        instance.pause()
    assert not any(call[:2] == ("vm", "deallocate") for call in cloud.calls)


@pytest.mark.parametrize("age,route,path,error,valid", [
    (20, "routea", "direct-modbus", None, True),
    (20.001, "routea", "direct-modbus", None, False),
    (-1, "routea", "direct-modbus", None, False),
    (0, "routeb", "direct-modbus", None, False),
    (0, "routea", "password-hmi", None, False),
    (0, "routea", "direct-modbus", "PLC read failed", False),
    (30, "routea", "direct-modbus", None, False),
])
def test_guest_health_requires_exact_route_path_freshness_and_no_error(
        monkeypatch, age, route, path, error, valid):
    import datetime
    import azure.identity
    import azure.storage.blob
    stamp = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    class Clock(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return stamp
    document = {"route": route, "path": path, "error": error,
                "observedAt": (stamp - datetime.timedelta(seconds=age)).isoformat()}
    blob = SimpleNamespace(download_blob=lambda _: SimpleNamespace(
        readall=lambda: json.dumps(document).encode()))
    monkeypatch.setattr(datetime, "datetime", Clock)
    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", lambda: object())
    monkeypatch.setattr(azure.storage.blob, "ContainerClient", lambda *args: blob)
    monkeypatch.setenv("SWU_ROUTE", "routea")
    monkeypatch.setenv("SWU_STORAGE_ACCOUNT", "teststorage")
    monkeypatch.setenv("SWU_RESUME_STARTED_AT", (stamp - datetime.timedelta(seconds=25)).isoformat())
    ticks = iter((0, 151))
    monkeypatch.setattr(MODULE["time"], "monotonic", lambda: next(ticks))
    if valid:
        exec(compile(GUEST["CHECK_HEALTH"], "<lifecycle-health>", "exec"), {})
    else:
        with pytest.raises(TimeoutError, match="Fresh post-start"):
            exec(compile(GUEST["CHECK_HEALTH"], "<lifecycle-health>", "exec"), {})


def test_preflight_failure_replaces_previous_success_report(tmp_path, monkeypatch):
    output = tmp_path / "report.json"
    output.write_text('{"state":"verified","applied":true}')
    def fail(_):
        raise RuntimeError("Ownership mismatch")
    monkeypatch.setattr(Lifecycle, "inspect", fail)
    monkeypatch.setattr(sys, "argv",
                        ["route-lifecycle.py", "pause", "--subscription", SUB, "--tenant", TENANT,
                         "--apply", "--confirm", "pause:swurab", "--output", str(output)])
    with pytest.raises(RuntimeError, match="Ownership mismatch"):
        MODULE["main"]()
    report = json.loads(output.read_text())
    assert report["state"] == "failed" and report["applied"] is False


def test_run_command_requires_file_transport_and_exact_guest_result(monkeypatch):
    instance, _ = controller(monkeypatch)
    def azure(*args):
        path = args[args.index("--scripts") + 1]
        assert path.startswith("@")
        script = Path(path[1:]).read_text()
        assert "base64 -d" in script and "trap 'rm -f /tmp/swu-lifecycle-" in script
        assert "pause-runner --role attack --route routea --storage testprivatestorage" in script
        return {"value": [{"message": "Enable succeeded"}]}
    monkeypatch.setattr(instance, "azure", azure)
    with pytest.raises(RuntimeError, match="success marker missing"):
        instance.guest("routea", "attack", "pause-runner")
    monkeypatch.setattr(instance, "azure", lambda *args: {
        "value": [{"message": '[stdout]\nSWU_LIFECYCLE_JSON={"drained":true}\n[stderr]'}]})
    assert instance.guest("routea", "attack", "pause-runner") == {"drained": True}


def test_pause_holds_sqlite_write_lock_until_container_stops(tmp_path, monkeypatch):
    journal = tmp_path / "commands.sqlite"
    with sqlite3.connect(journal) as db:
        db.execute("CREATE TABLE jobs(finished INTEGER)")
    calls = []
    def docker(*args):
        with sqlite3.connect(journal, timeout=0) as second:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                second.execute("INSERT INTO jobs VALUES(0)")
        calls.append(args)
    monkeypatch.setitem(GUEST["pause_runner"].__globals__, "docker", docker)
    GUEST["pause_runner"](tmp_path, timeout=0)
    assert calls == [("stop", "--time", "30", "swu-runner")]
    with sqlite3.connect(journal) as db:
        db.execute("INSERT INTO jobs VALUES(0)")
    calls.clear()
    with pytest.raises(TimeoutError, match="restoration"):
        GUEST["pause_runner"](tmp_path, timeout=0)
    assert calls == []


def test_recovery_helper_restores_then_cancels_without_executing_queued_actions(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "apps"))
    import azure.identity
    import azure.storage.blob
    import azure.storage.queue
    from swuapps import route_worker
    from datetime import datetime, timezone
    class Plc:
        values = {"PumpOverride": 1.0, "ClDoseSetpoint": 1.2}
        writes = []
        def read(self):
            return dict(self.values)
        def write(self, tag, value):
            self.values[tag] = value
            self.writes.append((tag, value))
    class Blobs:
        documents = {}
        def upload_blob(self, name, content, **kwargs):
            self.documents[name] = json.loads(content)
    class Audit:
        records = []
        def send_message(self, content, **kwargs):
            self.records.append(json.loads(content))
    def command():
        return {"runId": str(uuid.uuid4()), "route": "routea", "action": "pump-on",
                "actor": str(uuid.uuid4()), "issuedAt": datetime.now(timezone.utc).isoformat(), "confirmed": True}
    pending, queued = command(), command()
    messages = [SimpleNamespace(content=json.dumps(job)) for job in (pending, queued)]
    deleted = []
    queue = SimpleNamespace(receive_messages=lambda **kwargs: iter(messages),
                            delete_message=lambda message: deleted.append(message))
    plc, blobs, audits = Plc(), Blobs(), Audit()
    runner = route_worker.Runner("routea", plc, blobs, queue, audits, tmp_path)
    with runner.db:
        runner.db.execute("INSERT INTO jobs(id,document,baseline) VALUES(?,?,?)",
                          (pending["runId"], json.dumps(pending), json.dumps({"PumpOverride": 0.0})))
    monkeypatch.setattr(route_worker.time, "sleep", lambda _: None)
    monkeypatch.setattr(route_worker, "Runner", lambda *args: runner)
    monkeypatch.setattr(route_worker, "DirectPlc", lambda *args: plc)
    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", lambda: object())
    monkeypatch.setattr(azure.storage.blob, "ContainerClient", lambda *args: blobs)
    monkeypatch.setattr(azure.storage.queue, "QueueClient",
                        lambda url, name, credential: queue if name.endswith("-commands") else audits)
    monkeypatch.setenv("SWU_ROUTE", "routea")
    monkeypatch.setenv("SWU_STORAGE_ACCOUNT", "teststorage")
    exec(compile(GUEST["RECOVER_AND_CANCEL"], "<lifecycle-recovery>", "exec"), {})
    assert plc.writes == [("PumpOverride", 0.0)]
    assert len(deleted) == 2
    assert blobs.documents[f"jobs/{pending['runId']}.json"]["stage"] == "interrupted-restored"
    result = blobs.documents[f"jobs/{queued['runId']}.json"]
    assert result["stage"] == "expired-unobserved" and "not executed" in result["error"]
    assert any("LabControlCancelled" == record["SimAudit_CL"][0]["OperationName"] for record in audits.records)
    assert runner.db.execute("SELECT count(*) FROM jobs WHERE finished=0").fetchone()[0] == 0


def test_workflow_is_manual_scoped_serialized_and_not_a_deployment_pipeline():
    workflow = (ROOT / ".github" / "workflows" / "route-lifecycle.yml").read_text()
    assert "workflow_dispatch:" in workflow and "schedule:" not in workflow
    assert "environment: lab-lifecycle-cpi" in workflow
    assert "id-token: write" in workflow and "cancel-in-progress: false" in workflow
    assert "github.event.repository.default_branch" in workflow
    assert '[[ "$CONFIRMATION" == "$ACTION:swurab" ]]' in workflow
    assert "CPI_AZURE_SUBSCRIPTION_ID" in workflow
    assert "deploy-route-ab.py" not in workflow
