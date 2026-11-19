"""Exercise the authenticated CPI console through both real private access paths."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("console_publisher", ROOT / "scripts" / "deploy-console.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def main():
    state = json.loads((ROOT / "route-ab-console-deployment.json").read_text())
    runtime_path = ROOT / "route-ab-deployment.json"
    runtime = json.loads(runtime_path.read_text())
    account = publisher.azure("account", "show")
    if (account["id"] != runtime["subscriptionId"] or account["tenantId"] != runtime["tenantId"]
            or state["subscription"] != account["id"] or state["tenantId"] != account["tenantId"]
            or runtime.get("runtimeDeployed") is not True or state.get("state") != "verified"):
        raise RuntimeError("Verified runtime/console and the exact CPI context are required")
    token = publisher.azure("account", "get-access-token",
                            "--scope", f"api://{state['applicationClientId']}/Console.Access")["accessToken"]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Origin": state["url"]}

    def request(path, body=None, csrf=True):
        supplied = dict(headers)
        if not csrf:
            supplied.pop("X-SWU-CSRF", None)
        req = Request(state["url"] + path, headers=supplied,
                      data=json.dumps(body).encode() if body is not None else None)
        try:
            with urlopen(req, timeout=60) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()

    status, html = request("/")
    if status != 200 or b'id="view-live"' not in html:
        raise RuntimeError(f"Authenticated live console page did not load: HTTP {status}")
    csrf = re.search(rb'name="swu-csrf" content="([^"]+)"', html)
    if csrf is None:
        raise RuntimeError("Live console did not provide its CSRF token")
    headers["X-SWU-CSRF"] = csrf[1].decode()
    status, body = request("/api/status")
    if status != 200 or json.loads(body).get("liveControlEnabled") is not True:
        raise RuntimeError("Live console control dispatch is not enabled")
    status, body = request("/api/live/routes")
    routes = json.loads(body)["routes"]
    if status != 200 or {route["route"] for route in routes} != {"routea", "routeb"} or any(
            not route.get("ready") or route.get("stale") for route in routes):
        raise RuntimeError("Both actual route observations must be fresh")
    command = {"route": "routea", "action": "chlorine-up", "confirmed": True}
    if request("/api/live/commands", command, csrf=False)[0] != 403:
        raise RuntimeError("Live command without CSRF was not denied")
    if request("/api/live/commands", {**command, "confirmed": False})[0] != 409:
        raise RuntimeError("Live command without confirmation was not denied")
    if request("/api/live/commands", {**command, "action": "arbitrary-script"})[0] != 400:
        raise RuntimeError("Arbitrary live action was not denied")
    evidence = {"startedAt": datetime.now(timezone.utc).isoformat(), "url": state["url"],
                "routes": [], "formalAcceptanceCertified": False, "nativeSecurityFeedsVerified": False}
    for route in ("routea", "routeb"):
        status, body = request("/api/live/commands", {**command, "route": route})
        if status != 202:
            raise RuntimeError(f"{route}: actual queue dispatch failed: HTTP {status}")
        queued = json.loads(body)
        if queued.get("physicalActionsExecuted") is not False:
            raise RuntimeError("Queue acceptance incorrectly claimed physical execution")
        identity = queued["runId"]
        deadline = time.monotonic() + 180
        while True:
            status, body = request(f"/api/live/commands/{route}/{identity}")
            if status != 200:
                raise RuntimeError(f"{route}: live command result read failed: HTTP {status}")
            result = json.loads(body)
            if result["stage"] == "completed-restored":
                break
            if result["stage"] in ("failed-restored", "interrupted-restored", "expired-unobserved"):
                raise RuntimeError(f"{route}: actual command did not complete: {result['stage']}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{route}: command execution/readback/restoration did not finish")
            time.sleep(5)
        if result["observed"]["ClDoseSetpoint"] != 4.0 or not result.get("restoredAt"):
            raise RuntimeError(f"{route}: actual PLC readback/restoration evidence is incomplete")
        deadline = time.monotonic() + 900
        while True:
            status, body = request(f"/api/live/commands/{route}/{identity}/evidence")
            if status != 200:
                if status == 503 and time.monotonic() < deadline:
                    print(f"{route}: Sentinel query temporarily unavailable; retrying evidence only.", flush=True)
                    time.sleep(20)
                    continue
                raise RuntimeError(f"{route}: actual Sentinel evidence query failed: HTTP {status}")
            ingestion = json.loads(body)
            if ingestion["sentinel"] == "ingested" and ingestion["counts"].get("lab-action-audit", 0) >= 3:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{route}: the requested/observed/restored audit records did not reach Sentinel")
            time.sleep(20)
        evidence["routes"].append({"route": route, "result": result, "evidence": ingestion})
        print(f"Console-to-{route} actual execution, restoration and Sentinel audit verified.", flush=True)
    evidence["verifiedAt"] = datetime.now(timezone.utc).isoformat()
    (ROOT / "test-results" / "route-ab-live-console-verification.json").write_text(json.dumps(evidence, indent=2) + "\n")
    runtime.update(consoleIntegrationVerified=True, liveConsoleUrl=state["url"], liveVerifiedAt=evidence["verifiedAt"])
    temporary = runtime_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(runtime, indent=2) + "\n")
    temporary.replace(runtime_path)
    print(f"Both actual route workflows verified through the CPI console: {state['url']}", flush=True)


if __name__ == "__main__":
    main()
