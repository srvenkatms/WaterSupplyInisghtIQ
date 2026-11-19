"""Test harness for the Level 1 lab (ARC-07, FR-21 to FR-24).

  python harness/swuharness.py run SCN-01 --utility utc
  python harness/swuharness.py run SCN-04 --utility uta        # act mode: real Modbus write
  python harness/swuharness.py baseline-check --hours 24        # NFR-07
  python harness/swuharness.py health                           # last record per table

Reads deployment-outputs.json written by scripts/deploy.sh. Uses the
signed-in Azure CLI identity (az login), which needs Storage Queue Data
Message Sender on the lab storage account and Log Analytics Reader on the
workspaces (assigned by Bicep when testerPrincipalId is set)."""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
import uuid
import os
import shlex
import shutil
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "generator"))
from swugen import load_profile, scenarios  # noqa: E402
from swugen.configuration import COLLECTOR_FIELDS, SOURCE_HEALTH_UNION  # noqa: E402

TABLES = ["SimSignin_CL", "SimAudit_CL", "SimAccessTraffic_CL", "SimFirewall_CL", "SimEndpoint_CL",
          "SimOtAlert_CL", "SimProcess_CL", "SimExposure_CL", "SimInventory_CL"]
NORMALIZERS = {
    "SimSignin_CL": "SwuSignins", "SimAudit_CL": "SwuAudit", "SimAccessTraffic_CL": "SwuAccess",
    "SimFirewall_CL": "SwuFirewall", "SimEndpoint_CL": "SwuEndpoint", "SimOtAlert_CL": "SwuOtAlerts",
    "SimProcess_CL": "SwuProcess", "SimExposure_CL": "SwuExposure", "SimInventory_CL": "SwuInventory",
    "SimAgentAction_CL": "SwuAgentActions",
}
PRIMARY_TIMEOUT = timedelta(minutes=15)     # NFR-01
CONTRACT = json.loads((ROOT / "rules" / "contract.json").read_text(encoding="utf-8"))


def outputs() -> dict:
    return json.loads((ROOT / "deployment-outputs.json").read_text())


def workspace_id(uid: str) -> str:
    return next(u["workspaceId"] for u in outputs()["utilities"] if u["utilityId"] == uid)


def credential():
    from azure.identity import AzureCliCredential
    return AzureCliCredential(process_timeout=60)


@lru_cache(maxsize=1)
def logs_client():
    from azure.monitor.query import LogsQueryClient
    return LogsQueryClient(credential())


def query(uid: str, kql: str, hours: float = 24):
    return query_workspace(workspace_id(uid), kql, hours)


def query_workspace(workspace: str, kql: str, hours: float = 24):
    from azure.monitor.query import LogsQueryStatus
    resp = logs_client().query_workspace(workspace, kql, timespan=timedelta(hours=hours))
    if resp.status != LogsQueryStatus.SUCCESS:
        raise RuntimeError(f"Log query failed or returned partial results: {resp.partial_error}")
    if not resp.tables:
        raise RuntimeError("Log query returned no result table")
    table = resp.tables[0]
    return [dict(zip(table.columns, row)) for row in table.rows]


def send_scenario(uid: str, name: str, run_id: str, start: datetime) -> None:
    from azure.storage.queue import QueueClient, TextBase64EncodePolicy
    QueueClient.from_queue_url(outputs()["queue"], credential=credential(),
                              message_encode_policy=TextBase64EncodePolicy()).send_message(
        json.dumps({"scenario": name, "utility": uid, "runId": run_id, "start": start.isoformat()}))


def remote(role: str, arguments: list[str]) -> str:
    host = os.environ.get(f"SWU_{role.upper()}_SSH")
    if not host:
        raise RuntimeError(f"Set SWU_{role.upper()}_SSH to a private/Bastion-forwarded SSH target; "
                           "VM run-command is not available under strict post-build outbound lockdown")
    result = subprocess.run(["ssh", host, shlex.join(["sudo", "-n", *arguments])],
                            check=True, capture_output=True, text=True)
    return result.stdout


def act_on_plc(tag: str, value: float, via_hmi: bool) -> None:
    command = "hmi-set" if via_hmi else "set"
    remote("attack", ["docker", "exec", "swu-attack", "python", "-m", "swuapps.attack",
                      command, tag, str(value)])


def control_gateway(run_id: str, until: datetime | None = None) -> None:
    arguments = ["docker", "exec", "swu-gateway", "python", "-m", "swuapps.gateway_control", "--run-id", run_id]
    if until:
        arguments.extend(["--suppress-until", until.isoformat()])
    remote("gateway", arguments)


def alerts_since(uid: str, since: datetime) -> list[dict]:
    kql = f"""SecurityAlert
| where TimeGenerated >= datetime({since.isoformat()})
| where AlertName startswith "DR-"
| project TimeGenerated, AlertName, AlertSeverity, SystemAlertId, StartTime, EndTime,
          Entities = tostring(Entities), Details = tostring(ExtendedProperties)"""
    return query(uid, kql, hours=4)


def evidence_for_run(uid: str, run_id: str) -> list[dict]:
    if not run_id.startswith("run-") or not run_id.replace("-", "").isalnum():
        raise ValueError("Invalid run ID")
    return query(uid, f'''union withsource=EvidenceTable {", ".join(TABLES)}
| where ScenarioRunId == "{run_id}"
| extend Source = EvidenceTable | project-away EvidenceTable''', hours=4)


RULE_SOURCES = {
    "DR-01": {"SimAccessTraffic_CL"}, "DR-02": {"SimSignin_CL"}, "DR-03": {"SimExposure_CL"},
    "DR-04": {"SimFirewall_CL"}, "DR-05": {"SimOtAlert_CL"}, "DR-06": {"SimProcess_CL"},
    "DR-07": {"SimProcess_CL"}, "DR-08": {"SimSignin_CL"}, "DR-09": {"SimEndpoint_CL"},
    "DR-10": {"SimFirewall_CL", "SimEndpoint_CL"},
}


def matches_evidence(alert: dict, evidence: list[dict]) -> bool:
    rule = alert["AlertName"].split(" ")[0]
    details = set(json_values([alert.get("Entities", ""), alert.get("Details", "")]))
    start, end = alert.get("StartTime"), alert.get("EndTime")
    if not start or not end:
        return False
    for row in evidence:
        if row["Source"].split(".")[-1] not in RULE_SOURCES.get(rule, set()):
            continue
        if not start <= row["TimeGenerated"] <= end:
            continue
        if rule == "DR-06" and row.get("EventType") != "SetpointChange":
            continue
        if rule == "DR-07" and row.get("EventType") != "OutOfRange":
            continue
        if rule == "DR-02" and row.get("ResultType") != "0":
            continue
        identities = {
            "DR-01": ("UserPrincipalName", "SourceIp"), "DR-02": ("UserPrincipalName", "IPAddress"),
            "DR-03": ("AssetIp",), "DR-04": ("SourceIP",), "DR-05": ("SourceIp",),
            "DR-06": ("Tag", "NewValue"), "DR-07": ("Tag", "NewValue"),
            "DR-08": ("UserPrincipalName", "IPAddress"), "DR-09": ("DeviceName",),
            "DR-10": ("DeviceName",) if row["Source"].split(".")[-1] == "SimEndpoint_CL" else ("SourceIP",),
        }
        fields = identities.get(rule, ())
        if fields and all(row.get(field) is not None and row.get(field) != ""
                          and set(json_values(row[field])) <= details for field in fields):
            return True
    return False


def json_values(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from json_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from json_values(item)
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            yield value.lower()
        else:
            if parsed == value:
                yield value.lower()
            else:
                yield from json_values(parsed)
    elif type(value) in (int, float):
        yield format(value, ".15g")
    elif value is not None:
        yield str(value).lower()


def incidents_since(uid: str, start: datetime) -> list[dict]:
    return query(uid, f"""SecurityIncident
| where CreatedTime >= datetime({start.isoformat()})
| summarize arg_max(TimeGenerated, *) by IncidentNumber
| project IncidentNumber, CreatedTime, Severity, Status, AlertIds""", hours=4)


def observation(rule: str, incident: dict, start: datetime, scenario: str) -> dict:
    limit = CONTRACT[rule]["maxLatencyMinutes"]
    latency = (incident["CreatedTime"] - start).total_seconds() / 60
    required = "High" if rule == "DR-11" and scenario == "SCN-09" else CONTRACT[rule]["severity"]
    ranks = {"Low": 0, "Medium": 1, "High": 2}
    severity_ok = ranks.get(incident["Severity"], -1) >= ranks[required]
    return {"rule": rule, "incident": incident["IncidentNumber"], "latencyMin": latency,
            "limitMin": limit, "severity": incident["Severity"], "requiredSeverity": required,
            "pass": 0 <= latency <= limit and severity_ok}


def alert_observation(rule: str, alert: dict, incident: dict, start: datetime, scenario: str) -> dict:
    result = observation(rule, incident, start, scenario)
    result["alertLatencyMin"] = (alert["TimeGenerated"] - start).total_seconds() / 60
    result["eventToAlertMin"] = (alert["TimeGenerated"] - alert["EndTime"]).total_seconds() / 60
    result["alertId"] = alert["SystemAlertId"]
    ranks = {"Low": 0, "Medium": 1, "High": 2}
    alert_severity_ok = ranks.get(alert["AlertSeverity"], -1) >= ranks[result["requiredSeverity"]]
    result["pass"] &= 0 <= result["alertLatencyMin"] <= result["limitMin"] and alert_severity_ok
    return result


def cmd_run(a) -> int:
    require_ready()
    p = load_profile(a.utility)
    exp = scenarios.expected_rules(a.scenario, p)
    if not exp["applicable"]:
        print(f"{a.scenario} does not apply to {a.utility} (tier {p['tier']})")
        return 2
    run_id, start = f"run-{uuid.uuid4().hex[:8]}", datetime.now(timezone.utc)
    snapshot = None
    if p["processSource"] == "gateway":
        snapshot = json.loads(remote("attack", ["docker", "exec", "swu-attack", "python", "-m", "swuapps.attack", "scan"]))
    try:
        if snapshot is not None:
            control_gateway(run_id)
        return run_injected(a, p, exp, run_id, start, snapshot)
    finally:
        if snapshot is not None:
            failures = []
            for tag in ("PumpLowSetpoint", "PumpHighSetpoint", "ClDoseSetpoint", "PumpOverride", "Mode"):
                try:
                    act_on_plc(tag, snapshot[tag], via_hmi=True)
                except Exception as exc:
                    failures.append(exc)
            try:
                control_gateway("")
            except Exception as exc:
                failures.append(exc)
            path = ROOT / "test-results" / f"{a.scenario}-{a.utility}-{run_id}.json"
            if path.exists():
                record = json.loads(path.read_text())
                record["cleanup"] = {"pass": not failures, "errors": [type(exc).__name__ for exc in failures]}
                record["pass"] = record["pass"] and not failures
                path.write_text(json.dumps(record, indent=2))
            if failures:
                raise ExceptionGroup("PLC restoration or gateway cleanup failed", failures)


def run_injected(a, p, exp, run_id, start, snapshot) -> int:
    send_scenario(a.utility, a.scenario, run_id, start)
    print(f"Injected {a.scenario} into {a.utility} as {run_id} at {start:%H:%M:%S}Z")
    if p["processSource"] == "gateway":
        for step in scenarios.build(a.scenario, p, start):
            if step[1] == "set":
                wait = (start + timedelta(seconds=step[0]) - datetime.now(timezone.utc)).total_seconds()
                time.sleep(max(0, wait))
                act_on_plc(step[2], step[3], via_hmi=a.scenario in {"SCN-01", "SCN-08", "SCN-09"})
                print(f"  act: {step[2]} -> {step[3]} on the real PLC")
            elif step[1] == "suppress":
                wait = (start + timedelta(seconds=step[0]) - datetime.now(timezone.utc)).total_seconds()
                time.sleep(max(0, wait))
                control_gateway(run_id, datetime.now(timezone.utc) + timedelta(seconds=step[3]))
    want = set(exp["primary"] + exp["eventual"])
    seen: dict[str, dict] = {}
    deadline = start + timedelta(minutes=120 if want else 60)
    while datetime.now(timezone.utc) < deadline and (not want or want - set(seen)):
        evidence = evidence_for_run(a.utility, run_id)
        incidents = incidents_since(a.utility, start)
        for al in alerts_since(a.utility, start):
            rule = al["AlertName"].split(" ")[0]
            health = (rule == "DR-11" and a.scenario == "SCN-09" and al["AlertSeverity"] == "High"
                      and any(source in al["Details"] for source in ("SwuFirewall", "SwuProcess")))
            if rule in want and (health or matches_evidence(al, evidence)):
                for incident in incidents:
                    if al["SystemAlertId"] in incident["AlertIds"]:
                        result = alert_observation(rule, al, incident, start, a.scenario)
                        if rule not in seen or (result["pass"] and not seen[rule]["pass"]):
                            seen[rule] = result
                            print(f"  {rule}: incident {result['incident']} after {result['latencyMin']:.1f} min ({result['severity']})")
        time.sleep(60)
    results = []
    for rule in sorted(want):
        results.append(seen.get(rule, {"rule": rule, "pass": False, "reason": "No run-correlated incident",
                                      "limitMin": CONTRACT[rule]["maxLatencyMinutes"]}))
    if a.scenario == "SCN-10":   # control: no High alert may appear
        highs = [x for x in alerts_since(a.utility, start) if x["AlertSeverity"] == "High"]
        results.append({"rule": "no-high-alerts", "pass": not highs, "found": [x["AlertName"] for x in highs]})
    passed = all(r["pass"] for r in results)
    record = {"scenario": a.scenario, "utility": a.utility, "runId": run_id, "start": start.isoformat(),
              "clock": "injection-to-incident", "runtime": "OpenPLC" if snapshot is not None else "synthetic",
              "results": results, "pass": passed}
    out = ROOT / "test-results"
    out.mkdir(exist_ok=True)
    (out / f"{a.scenario}-{a.utility}-{run_id}.json").write_text(json.dumps(record, indent=2, default=str))
    print("PASS" if passed else "FAIL", json.dumps(results, default=str))
    return 0 if passed else 1


def cmd_baseline(a) -> int:
    require_ready()
    if not math.isfinite(a.hours) or a.hours < 24:
        raise ValueError("Baseline acceptance requires at least 24 hours")
    bad = {}
    for u in outputs()["utilities"]:
        if not query(u["utilityId"], "SwuFirewall | summarize Records=count() | where Records > 0", hours=a.hours):
            raise RuntimeError(f"No baseline telemetry for {u['utilityId']}; refusing an empty-data pass")
        rows = query(u["utilityId"], 'SecurityAlert | where AlertSeverity == "High" | summarize n = count() by AlertName',
                     hours=a.hours)
        if rows:
            bad[u["utilityId"]] = rows
        incidents = query(u["utilityId"], """SecurityIncident
| summarize arg_max(TimeGenerated, *) by IncidentNumber
| where Severity == "High" | project IncidentNumber, Status""", hours=a.hours)
        if incidents:
            bad.setdefault(u["utilityId"], []).extend(incidents)
    print("PASS" if not bad else f"FAIL {bad}")
    return 0 if not bad else 1


def cmd_health(a) -> int:
    union = ", ".join(TABLES)
    kql = (f"union withsource = HealthFeed {union} "
           "| summarize LastSeen = max(ingestion_time()), Rows = count() by HealthFeed "
           "| project Source = HealthFeed, LastSeen, Rows")
    failed = False
    from swugen.configuration import deployment_profile
    for u in outputs()["utilities"]:
        print(f"== {u['utilityId']}")
        rows = query(u["utilityId"], kql, hours=24 * 9)
        received = {r["Source"].split(".")[-1]: r["LastSeen"] for r in rows}
        names = {alias: table for table, alias in NORMALIZERS.items()}
        profile = load_profile(u["utilityId"])
        for expected in deployment_profile(profile)["healthSources"]:
            table = names[expected["Source"]]
            last = received.get(table)
            if last is None or (datetime.now(timezone.utc) - last).total_seconds() > expected["MaxSilenceMin"] * 60:
                failed = True
                print(f"  FAIL {table}: never seen or silent")
        if profile["processSource"] == "gateway":
            state = query(u["utilityId"], """SwuProcess
| where TimeGenerated > ago(9d)
| where EventType == "Heartbeat" or (Tag == "PLC" and EventType in ("PlcUnreachable", "Recovered"))
| summarize arg_max(TimeGenerated, EventType)
| project EventType""", hours=24 * 9)
            if len(state) != 1 or state[0]["EventType"] not in {"Heartbeat", "Recovered"}:
                failed = True
                print("  FAIL PLC: unreachable or no positive gateway state")
        for r in sorted(rows, key=lambda r: r["Source"]):
            print(f"  {r['Source']:<22} last {r['LastSeen']:%Y-%m-%d %H:%M}Z  rows {r['Rows']}")
    return 1 if failed else 0


def require_ready() -> None:
    started = outputs().get("warmupStartedAt")
    if not started:
        raise RuntimeError("Verified private-path readiness must start the live warm-up before acceptance")
    deployed = datetime.fromisoformat(started)
    if deployed.tzinfo is None or datetime.now(timezone.utc) - deployed < timedelta(days=7):
        raise RuntimeError("Seven real days of live warm-up are required; backfill is not a substitute")
    for utility in outputs()["utilities"]:
        uid = utility["utilityId"]
        table = "SimFirewall_CL" if load_profile(uid)["processSource"] == "none" else "SimProcess_CL"
        rows = query(uid, f"""let actual = {table}
| extend Slot = bin(ingestion_time(), 1h)
| summarize Records = count() by Slot;
range Offset from 0 to 167 step 1
| extend Slot = bin(now(), 1h) - Offset * 1h
| join kind=leftouter actual on Slot
| summarize MissingHours = countif(isnull(Records))""", hours=24 * 8)
        if len(rows) != 1 or rows[0]["MissingHours"] != 0:
            raise RuntimeError(f"{uid}: live warm-up has missing hourly receipts")


def cmd_ready(a) -> int:
    require_ready()
    print("PASS: seven-day live ingestion readiness")
    return 0


def cmd_network(a) -> int:
    from private_operator import tunnel_command, wait_for_ssh

    negative = """import socket
for host, port in TARGETS:
    try:
        connection = socket.create_connection((host, port), timeout=5)
    except OSError:
        print(f"BLOCKED {host}:{port}", flush=True)
    else:
        connection.close()
        raise RuntimeError(f"Forbidden connection succeeded: {host}:{port}")
"""
    attack = negative.replace("TARGETS", repr([("1.1.1.1", 443), ("10.10.1.10", 1502),
                                              ("10.10.1.10", 8080)]))
    for action in ("scan", "hmi-scan"):
        print(remote("attack", ["docker", "exec", "swu-attack", "python", "-m", "swuapps.attack", action]))
    print(remote("attack", ["docker", "exec", "swu-attack", "python", "-c", attack]))
    control = negative.replace("TARGETS", repr([("1.1.1.1", 443)]))
    with Path("/state/tunnel-plc.log").open("a") as log:
        process = subprocess.Popen(tunnel_command(outputs(), "plc"), stdout=log, stderr=subprocess.STDOUT)
        try:
            wait_for_ssh("plc", process)
            result = subprocess.run(["ssh", "swu-plc", shlex.join(["sudo", "-n", "python3", "-c", control])],
                                    check=True, capture_output=True, text=True, timeout=30)
            print(result.stdout)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    print("PASS: private Modbus/HMI reads; fresh control/test Internet and raw PLC/admin connections blocked")
    return 0


def cmd_protocol(a) -> int:
    probe = """import json, os
from pymodbus.client import ModbusTcpClient
from swuapps.registers import MAP, READ_COUNT, READ_START, SETPOINTS
client = ModbusTcpClient(os.environ.get("SWU_PLC_HOST", "10.10.1.10"),
                         port=int(os.environ.get("SWU_MODBUS_PORT", "502")), timeout=5, retries=0)
if not client.connect():
    raise ConnectionError("Gateway cannot read the private PLC proxy")
def snapshot():
    response = client.read_holding_registers(READ_START, count=READ_COUNT)
    if response.isError() or len(response.registers) != READ_COUNT:
        raise RuntimeError("Gateway register read failed")
    return response.registers
try:
    before = snapshot()
    address = MAP["ClDoseSetpoint"]
    requests = [
        (6, lambda: client.write_register(address, before[address])),
        (16, lambda: client.write_registers(address, [before[address]])),
        (6, lambda: client.write_register(MAP["PumpState"], before[MAP["PumpState"]])),
        (1, lambda: client.read_coils(0, count=1)),
        (4, lambda: client.read_input_registers(0, count=1)),
    ]
    denied = []
    for function, request in requests:
        response = request()
        if not response.isError() or getattr(response, "exception_code", None) != 1:
            raise RuntimeError(f"Expected explicit IllegalFunction rejection for FC{function}, got {response}")
        denied.append(function)
    after = snapshot()
    if any(before[MAP[tag]] != after[MAP[tag]] for tag in SETPOINTS):
        raise RuntimeError("PLC setpoints changed during gateway denial proof")
    print(json.dumps({"pass": True, "deniedFunctions": denied, "setpointsUnchanged": True}))
finally:
    client.close()
"""
    result = json.loads(remote("gateway", ["docker", "exec", "swu-gateway", "python", "-c", probe]))
    if (result.get("pass") is not True or result.get("deniedFunctions") != [6, 16, 6, 1, 4]
            or result.get("setpointsUnchanged") is not True):
        raise RuntimeError(f"Gateway returned incomplete protocol denial evidence: {result}")
    result["observedAt"] = datetime.now(timezone.utc).isoformat()
    result["route"] = "private-gateway-to-plc-proxy"
    directory = ROOT / "test-results"
    directory.mkdir(exist_ok=True)
    path = directory / f"gateway-protocol-{uuid.uuid4()}.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    print("PASS: real gateway reads succeed; FC6/FC16 writes and FC1/FC4 reads explicitly rejected; setpoints unchanged")
    return 0


def cmd_schema(a) -> int:
    tables = json.loads((ROOT / "infra" / "tables.json").read_text())
    common = {"TimeGenerated": "datetime", "UtilityId": "string", "ScenarioRunId": "string", "RecordId": "string"}
    failed = False
    for utility in outputs()["utilities"]:
        uid = utility["utilityId"]
        for table in tables:
            actual = {row["ColumnName"]: row["ColumnType"] for row in query(uid, f'{table["name"]} | getschema')}
            expected = common | {column["name"]: column["type"] for column in table["columns"]}
            mismatch = {name: {"expected": kind, "actual": actual.get(name)}
                        for name, kind in expected.items() if actual.get(name) != kind}
            if mismatch:
                failed = True
                print(f"FAIL {uid}/{table['name']}: {mismatch}")
        invalid = query(uid, f"""union {", ".join(t["name"] for t in tables)}
| where UtilityId != "{uid}" or isempty(RecordId)
| summarize Invalid = count()""", hours=24 * 9)
        if len(invalid) != 1 or invalid[0]["Invalid"] != 0:
            failed = True
            print(f"FAIL {uid}: cross-utility or uncorrelated records")
    print("FAIL" if failed else "PASS: table schemas and observed isolation")
    return int(failed)


def cmd_content(a) -> int:
    schemas = json.loads((ROOT / "infra" / "tables.json").read_text())
    failed = False
    for utility in outputs()["utilities"]:
        uid = utility["utilityId"]
        for table in schemas:
            event_alias = NORMALIZERS[table["name"]]
            aliases = [event_alias, event_alias + "Feed"] if event_alias in COLLECTOR_FIELDS else [event_alias]
            expected = {"TimeGenerated": "datetime", "ReceivedAt": "datetime", "UtilityId": "string"}
            expected.update({column["name"]: column["type"] for column in table["columns"]})
            for alias in aliases:
                actual = {row["ColumnName"]: row["ColumnType"]
                          for row in query(uid, f"{alias} | getschema")}
                mismatch = {name: {"expected": kind, "actual": actual.get(name)}
                            for name, kind in expected.items() if actual.get(name) != kind}
                hidden = {"ScenarioRunId", "RecordId"} & actual.keys()
                if mismatch or hidden:
                    failed = True
                    print(f"FAIL {uid}/{alias}: schema={mismatch}, leaked fields={sorted(hidden)}")
        for alias in ("SwuAlerts", "SwuIncidents"):
            if not query(uid, f"{alias} | getschema"):
                failed = True
                print(f"FAIL {uid}/{alias}: empty function schema")
        from swugen.configuration import reference_data
        for watchlist in reference_data(load_profile(uid)):
            query(uid, f'_GetWatchlist("{watchlist["alias"]}") | take 1')
    print("FAIL" if failed else "PASS: deployed functions and watchlists")
    return int(failed)


def cmd_history(a) -> int:
    failed = False
    for utility in outputs()["utilities"]:
        uid = utility["utilityId"]
        traces = query_workspace(outputs()["sharedWorkspaceId"], f'''AppTraces
| where TimeGenerated > ago(10m) and Message startswith "swu-history utility={uid} "
| summarize Successes = count()''', hours=1)
        rows = query(uid, f'''let actual = {SOURCE_HEALTH_UNION}
| where TimeGenerated > ago(9d)
| summarize ReceivedAt = max(ReceivedAt) by HealthFeed
| project Source = HealthFeed, ReceivedAt;
let cached = _GetWatchlist('SourceReceipts')
| project Source = tostring(column_ifexists("Source", "")),
          CachedAt = todatetime(column_ifexists("LastSeen", ""))
| summarize CachedAt = max(CachedAt) by Source;
actual | join kind=leftouter cached on Source
| project Source, LagSeconds = datetime_diff('second', ReceivedAt, CachedAt)''', hours=24 * 9)
        ok = (len(traces) == 1 and traces[0]["Successes"] > 0 and bool(rows)
              and all(row["LagSeconds"] is not None and abs(row["LagSeconds"]) <= 600 for row in rows))
        failed |= not ok
        print(f"{uid}: {'PASS' if ok else 'FAIL'} history timer={traces}, actual receipt references={rows}")
    return int(failed)


def cmd_volume(a) -> int:
    require_ready()
    az = shutil.which("az")
    if not az:
        raise RuntimeError("Azure CLI is required to verify the deployed workspace cap")
    failed = False
    for utility in outputs()["utilities"]:
        rows = query(utility["utilityId"], f"""union {", ".join(TABLES)}
| where ingestion_time() > ago(24h)
| summarize Records = count(), Bytes = sum(_BilledSize)""", hours=24 * 2)
        billing = query(utility["utilityId"], """Usage
| where StartTime >= startofday(ago(1d)) and EndTime <= startofday(now()) and IsBillable == true
| summarize Hours = dcount(bin(StartTime, 1h)), Bytes = sum(Quantity) * 1000000""", hours=48)
        cap = json.loads(subprocess.run(
            [az, "monitor", "log-analytics", "workspace", "show", "--ids", utility["workspaceResourceId"],
             "--query", "workspaceCapping.dailyQuotaGb", "-o", "json"],
            check=True, capture_output=True, text=True).stdout)
        ok = (len(rows) == 1 and rows[0]["Records"] > 0 and rows[0]["Bytes"] < 200_000_000
              and len(billing) == 1 and billing[0]["Hours"] == 24
              and billing[0]["Bytes"] is not None and 0 < billing[0]["Bytes"] < 200_000_000
              and type(cap) in (int, float) and cap == 1)
        failed |= not ok
        print(f"{utility['utilityId']}: {'PASS' if ok else 'FAIL'} "
              f"telemetry={rows}, previousUtcDayBilling={billing}, dailyQuotaGb={cap}")
    return int(failed)


def cmd_availability(a) -> int:
    require_ready()
    if not math.isfinite(a.hours) or a.hours < 24:
        raise ValueError("Availability acceptance requires at least 24 hours")
    failed = False
    for utility in outputs()["utilities"]:
        uid = utility["utilityId"]
        load_profile(uid)
        rows = query_workspace(outputs()["sharedWorkspaceId"], f"""AppTraces
| where Message startswith "swu-generate utility={uid} "
| sort by TimeGenerated asc
| serialize
| extend GapSeconds = datetime_diff('second', TimeGenerated, prev(TimeGenerated))
| summarize Records = count(), FirstSeen = min(TimeGenerated), LastSeen = max(TimeGenerated),
            MaxGapSeconds = max(GapSeconds)
| extend MaxGapSeconds = max_of(MaxGapSeconds, datetime_diff('second', now(), LastSeen),
                                datetime_diff('second', FirstSeen, ago({a.hours}h)))""", hours=a.hours)
        ok = (len(rows) == 1 and rows[0]["Records"] > 0 and rows[0]["MaxGapSeconds"] is not None
              and rows[0]["MaxGapSeconds"] <= 600)
        failed |= not ok
        print(f"{uid}: {'PASS' if ok else 'FAIL'} {rows}")
    return int(failed)


def cmd_cost(a) -> int:
    manifest = outputs()
    subscription = str(uuid.UUID(manifest["subscription"]))
    body = {"type": "Usage", "timeframe": "MonthToDate", "dataset": {
        "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
        "filter": {"dimensions": {"name": "ResourceGroupName", "operator": "In",
                                  "values": manifest["resourceGroups"]}},
    }}
    token = credential().get_token("https://management.azure.com/.default").token
    request = Request(f"https://management.azure.com/subscriptions/{subscription}/providers/"
                      "Microsoft.CostManagement/query?api-version=2023-03-01",
                      data=json.dumps(body).encode(), method="POST",
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urlopen(request, timeout=60) as response:
        data = json.load(response)["properties"]
    rows = [dict(zip((column["name"] for column in data["columns"]), row)) for row in data["rows"]]
    if not rows:
        raise RuntimeError("Cost Management returned no billing rows; budget compliance is unverified")
    currencies = {row["Currency"] for row in rows}
    if len(currencies) != 1:
        raise RuntimeError("Cost report contains multiple billing currencies")
    costs = [row["PreTaxCost"] for row in rows]
    if any(isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost)
           for cost in costs):
        raise RuntimeError("Cost report contains invalid billing amounts")
    if not next(iter(currencies)):
        raise RuntimeError("Cost report has no billing currency")
    print(json.dumps({"monthToDate": sum(costs),
                      "currency": next(iter(currencies)), "monthlyBudget": manifest["budgetAmount"],
                      "status": "observation-only; full-month cost acceptance remains required"}))
    return 0


def cmd_suite(a) -> int:
    require_ready()
    runs = []
    for repetition in range(1, 4):
        for utility in outputs()["utilities"]:
            uid = utility["utilityId"]
            if a.utility and a.utility != uid:
                continue
            profile = load_profile(uid)
            for scenario in scenarios.EXPECTED:
                if a.scenario and scenario != a.scenario:
                    continue
                if not scenarios.expected_rules(scenario, profile)["applicable"]:
                    continue
                active = query(uid, """SwuIncidents
| where Status != "Closed" and Severity == "High" | project IncidentNumber""")
                if active:
                    raise RuntimeError(f"{uid}: restore the plant and close previous test High incidents before continuing")
                started = datetime.now(timezone.utc)
                result = cmd_run(argparse.Namespace(utility=uid, scenario=scenario))
                runs.append({"repetition": repetition, "utility": uid, "scenario": scenario, "pass": result == 0})
                directory = ROOT / "test-results"
                directory.mkdir(exist_ok=True)
                (directory / "suite.json").write_text(json.dumps(runs, indent=2))
                if result:
                    return 1
                wait = (started + timedelta(hours=2, minutes=a.settle_minutes) - datetime.now(timezone.utc)).total_seconds()
                time.sleep(max(0, wait))
    if not runs:
        raise ValueError("No applicable scenarios selected")
    print("PASS: three consecutive scenario suites")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("scenario"); r.add_argument("--utility", required=True)
    b = sub.add_parser("baseline-check"); b.add_argument("--hours", type=float, default=24)
    sub.add_parser("health")
    sub.add_parser("ready")
    sub.add_parser("schema-check")
    sub.add_parser("content-check")
    sub.add_parser("history-check")
    sub.add_parser("network-check")
    sub.add_parser("protocol-check")
    sub.add_parser("volume-check")
    availability = sub.add_parser("availability-check")
    availability.add_argument("--hours", type=float, default=24)
    sub.add_parser("cost-report")
    suite = sub.add_parser("suite")
    suite.add_argument("--utility", choices=("uta", "utb", "utc"))
    suite.add_argument("--scenario", choices=tuple(scenarios.EXPECTED))
    suite.add_argument("--settle-minutes", type=int, default=120)
    a = ap.parse_args()
    if a.cmd == "suite" and a.settle_minutes < 120:
        raise ValueError("Suite recovery observation must be at least 120 minutes")
    return {"run": cmd_run, "baseline-check": cmd_baseline, "health": cmd_health,
            "ready": cmd_ready, "schema-check": cmd_schema, "content-check": cmd_content,
            "history-check": cmd_history, "network-check": cmd_network, "protocol-check": cmd_protocol,
            "volume-check": cmd_volume, "suite": cmd_suite,
            "availability-check": cmd_availability, "cost-report": cmd_cost}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
