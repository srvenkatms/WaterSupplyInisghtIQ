"""Verify actual route paths, temporary PLC changes, restoration and Sentinel ingestion."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("route_publisher", ROOT / "scripts" / "deploy-route-ab.py")
publisher_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher_module)

PROBE = """set -eu
docker exec -i swu-runner python - <<'PY'
import json, os, socket, time
from pathlib import Path
from urllib.error import HTTPError
from swuapps.route_worker import DirectPlc, PasswordHmi
route = os.environ['SWU_ROUTE']
host = os.environ['SWU_PLC_HOST']
blocked = False
unauthorized = False
wrong_password = False
if route == 'routeb':
    try:
        connection = socket.create_connection((host,502),timeout=3)
    except (TimeoutError, ConnectionRefusedError, OSError):
        blocked = True
    else:
        connection.close()
        raise AssertionError('Route B raw PLC connection unexpectedly succeeded')
    client = PasswordHmi(os.environ['SWU_HMI_URL'],Path('/private/hmi.json'),Path('/private/hmi.crt'))
    try:
        client.request('/api/values')
    except HTTPError as error:
        assert error.code == 401, error.code
        unauthorized = True
    assert unauthorized
    client.csrf = client.request('/api/session')['csrf']
    try:
        client.request('/api/login',dict(client.credentials,password='invalid-live-probe-password'))
    except HTTPError as error:
        assert error.code == 401, error.code
        wrong_password = True
    assert wrong_password
else:
    client = DirectPlc(host)
before = client.read()
baseline = before['ClDoseSetpoint']
try:
    client.write('ClDoseSetpoint',4.0)
    time.sleep(2)
    during = client.read()
    assert during['ClDoseSetpoint'] == 4.0, during
    time.sleep(6)
finally:
    client.write('ClDoseSetpoint',baseline)
time.sleep(2)
after = client.read()
assert after['ClDoseSetpoint'] == baseline, after
print('__SWU_PATH_EVIDENCE__'+json.dumps(dict(route=route,directPlcDenied=blocked,
    unauthenticatedHmiDenied=unauthorized,wrongPasswordDenied=wrong_password,
    baseline=baseline,observedDose=during['ClDoseSetpoint'],restoredDose=after['ClDoseSetpoint'],
    before=before,after=after)))
PY
echo __SWU_PATH_OK__
"""

GATEWAY = """set -eu
docker exec -i swu-gateway python - <<'PY'
from pymodbus.client import ModbusTcpClient
import os
c=ModbusTcpClient(os.environ['SWU_PLC_HOST'],timeout=3,retries=0)
assert c.connect()
before=c.read_holding_registers(12,count=1)
assert not before.isError()
denied=c.write_register(12,before.registers[0])
assert denied.isError(), 'Gateway unexpectedly has Modbus write permission'
c.close()
PY
echo __SWU_GATEWAY_READ_ONLY_OK__
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--azure-config-dir", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    args = parser.parse_args()
    state = json.loads(publisher_module.STATE.read_text())
    publisher = publisher_module.Publisher(state, args.azure_config_dir, args.owner)
    publisher.context()
    if state.get("buildOutboundEnabled") is not False or state.get("runtimePhase") not in ("validation-required", "verified"):
        raise RuntimeError("Runtime deployment and build-egress lockdown must complete first")
    started = datetime.now(timezone.utc).isoformat()
    evidence = {"startedAt": started, "routes": [], "formalAcceptanceCertified": False}
    with tempfile.TemporaryDirectory(prefix="swu-route-probe-") as directory:
        scripts = {}
        for name, script in (("path", PROBE), ("gateway", GATEWAY)):
            path = Path(directory) / f"{name}.sh"
            path.write_text(script, encoding="utf-8", newline="\n")
            scripts[name] = path

        def verify_route(route):
            records = {}
            for name, role, marker in (("path", "vm-uta-attack", "__SWU_PATH_OK__"),
                                       ("gateway", "vm-uta-gw", "__SWU_GATEWAY_READ_ONLY_OK__")):
                result = publisher.azure("vm", "run-command", "invoke", "--resource-group", route["resourceGroup"],
                                         "--name", role, "--command-id", "RunShellScript", "--scripts", f"@{scripts[name]}")
                text = "\n".join(value.get("message", "") for value in result.get("value", []))
                if marker not in text:
                    raise RuntimeError(f"{route['id']}/{name}: actual route verification failed: {text[-4000:]}")
                if name == "path":
                    line = next(line for line in text.splitlines() if line.startswith("__SWU_PATH_EVIDENCE__"))
                    records = json.loads(line.removeprefix("__SWU_PATH_EVIDENCE__"))
                else:
                    records["gatewayWriteDenied"] = True
            print(f"Actual access path and restoration verified: {route['id']}", flush=True)
            return records

        with ThreadPoolExecutor(max_workers=2) as pool:
            evidence["routes"] = list(pool.map(verify_route, state["routes"]))
    deadline = time.monotonic() + 900
    pending = {route["id"]: route for route in state["routes"]}
    while pending:
        token = publisher.azure("account", "get-access-token", "--resource", "https://api.loganalytics.io")["accessToken"]
        for identity, route in list(pending.items()):
            query = (f"SimProcess_CL | where TimeGenerated >= datetime({started}) "
                     "| where Tag == 'ClDoseSetpoint' and NewValue == 4.0 "
                     "| summarize Records=dcount(RecordId), ObservedAt=max(TimeGenerated)")
            req = Request(f"https://api.loganalytics.azure.com/v1/workspaces/{route['workspaceId']}/query",
                          data=json.dumps({"query": query, "timespan": "PT1H"}).encode(),
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            with urlopen(req, timeout=60) as response:
                data = json.load(response)
            if data.get("error") or len(data.get("tables", [])) != 1:
                raise RuntimeError(f"{identity}: Sentinel returned incomplete process evidence")
            table = data["tables"][0]
            columns = [column["name"] for column in table["columns"]]
            row = table["rows"][0]
            if row[columns.index("Records")] > 0:
                record = next(record for record in evidence["routes"] if record["route"] == identity)
                record["sentinelObservedAt"] = row[columns.index("ObservedAt")]
                record["sentinelProcessRecords"] = row[columns.index("Records")]
                del pending[identity]
                print(f"Actual changed PLC value found in Sentinel: {identity}", flush=True)
        if pending:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Actual process records did not appear in Sentinel: {', '.join(pending)}")
            time.sleep(20)
    evidence["verifiedAt"] = datetime.now(timezone.utc).isoformat()
    results = ROOT / "test-results"
    results.mkdir(exist_ok=True)
    (results / "route-ab-runtime-verification.json").write_text(json.dumps(evidence, indent=2) + "\n")
    publisher.state.update(runtimeDeployed=True, runtimePhase="verified", runtimeVerifiedAt=evidence["verifiedAt"])
    publisher.persist()
    print("Both actual application paths and Sentinel process ingestion verified; console verification is separate.", flush=True)


if __name__ == "__main__":
    main()
