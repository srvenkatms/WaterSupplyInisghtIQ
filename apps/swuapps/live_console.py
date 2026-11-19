"""Entra-console dispatch and evidence queries; no OT network client or HMI credentials."""
from datetime import datetime, timezone
import json
import logging
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid

from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.storage.blob import ContainerClient
from azure.storage.queue import QueueClient
from flask import abort, jsonify, request

ROUTES = ("routea", "routeb")
ACTIONS = ("pump-on", "chlorine-up")


def register_live_routes(app, credential, storage_url, console_container):
    workspaces = json.loads(os.environ["SWU_ROUTE_WORKSPACES"])
    if set(workspaces) != set(ROUTES):
        raise ValueError("Both approved route workspace IDs are required")
    workspaces = {route: str(uuid.UUID(value)) for route, value in workspaces.items()}
    containers = {route: ContainerClient(storage_url, f"{route}-state", credential,
                                        connection_timeout=5, read_timeout=15, retry_total=1) for route in ROUTES}
    queues = {route: QueueClient(storage_url.replace(".blob.", ".queue."), f"{route}-commands",
                                 credential, connection_timeout=5, read_timeout=15, retry_total=1) for route in ROUTES}

    def read(container, name):
        try:
            return json.loads(container.download_blob(name).readall())
        except ResourceNotFoundError as error:
            if error.error_code != "BlobNotFound":
                raise
            return None

    def route_id(route):
        if route not in ROUTES:
            abort(404, "Unknown route")
        return route

    def result(route, run_id):
        route_id(route)
        try:
            run_id = str(uuid.UUID(run_id))
        except ValueError:
            abort(400, "Invalid run identifier")
        document = read(containers[route], f"jobs/{run_id}.json")
        if document is None:
            document = read(console_container, f"live/{route}/{run_id}.json")
        if document is None:
            abort(404, "Unknown live command")
        if document.get("stage") in ("dispatching", "queued") and (datetime.now(timezone.utc) -
                datetime.fromisoformat(document["issuedAt"])).total_seconds() > 330:
            document.update(stage="expired-unobserved", error="The command expired without a worker execution record.")
        return document

    @app.get("/api/live/routes")
    def live_status():
        states = []
        for route in ROUTES:
            state = read(containers[route], "latest.json")
            if state is None:
                states.append({"route": route, "ready": False, "stale": True,
                               "error": "No live worker observation has been published."})
                continue
            at = datetime.fromisoformat(state["observedAt"])
            age = (datetime.now(timezone.utc) - at).total_seconds()
            state.update(ready=True, stale=not 0 <= age <= 20, ageSeconds=round(age, 1))
            states.append(state)
        return jsonify(routes=states, target={"deployed": False, "authentication": "Entra + MFA (future subscription)"})

    @app.post("/api/live/commands")
    def dispatch():
        body = request.get_json()
        if (not isinstance(body, dict) or set(body) != {"route", "action", "confirmed"}
                or body["route"] not in ROUTES or body["action"] not in ACTIONS):
            abort(400, "Choose an approved route and a fixed lab action")
        if body["confirmed"] is not True:
            abort(409, "Explicit confirmation is required for every live action")
        current = read(containers[body["route"]], "latest.json")
        if current is None or not 0 <= (datetime.now(timezone.utc) -
                datetime.fromisoformat(current["observedAt"])).total_seconds() <= 20:
            abort(409, "The route has no fresh PLC observation")
        if current.get("activeRunId"):
            abort(409, "The route is executing or restoring another command")
        document = {"runId": str(uuid.uuid4()), "route": body["route"], "action": body["action"],
                    "actor": str(uuid.UUID(request.headers["X-MS-CLIENT-PRINCIPAL-ID"])),
                    "issuedAt": datetime.now(timezone.utc).isoformat(), "confirmed": True}
        name = f"live/{body['route']}/{document['runId']}.json"
        console_container.upload_blob(name, json.dumps({**document, "stage": "dispatching"}), overwrite=False)
        try:
            queues[body["route"]].send_message(json.dumps(document), time_to_live=300)
            console_container.upload_blob(name, json.dumps({**document, "stage": "queued"}), overwrite=True)
        except AzureError:
            app.logger.exception("Live queue dispatch outcome is uncertain for %s", document["runId"])
            return jsonify(error="Queue dispatch is uncertain; inspect this run before retrying.",
                           runId=document["runId"], route=body["route"]), 503
        return jsonify(**document, stage="queued", physicalActionsExecuted=False), 202

    @app.get("/api/live/commands/<route>/<run_id>")
    def command_result(route, run_id):
        return jsonify(result(route, run_id))

    @app.get("/api/live/commands/<route>/<run_id>/evidence")
    def command_evidence(route, run_id):
        document = result(route, run_id)
        identity = str(uuid.UUID(document["runId"]))
        query = f"""union
          (SimAudit_CL | where TimeGenerated > ago(1d) and ScenarioRunId == '{identity}'
            | summarize Records=dcount(RecordId) | extend Kind='lab-action-audit'),
          (SecurityAlert | where TimeGenerated > ago(1d) and ExtendedProperties has '{identity}'
            | summarize Records=dcount(SystemAlertId) | extend Kind='correlated-alert')
          | project Kind, Records"""
        token = credential.get_token("https://api.loganalytics.io/.default").token
        req = Request(f"https://api.loganalytics.azure.com/v1/workspaces/{workspaces[route]}/query",
                      data=json.dumps({"query": query, "timespan": "P1D"}).encode(),
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=30) as response:
                data = json.load(response)
        except HTTPError as error:
            app.logger.exception("Sentinel evidence query rejected for %s", identity)
            if error.code not in (408, 429, 500, 502, 503, 504):
                abort(502, f"Sentinel evidence query rejected (HTTP {error.code}); no successful query is asserted.")
            raise RuntimeError("Sentinel evidence query temporarily unavailable; no successful query is asserted.") from error
        except (URLError, TimeoutError, ConnectionError) as error:
            raise RuntimeError("Sentinel evidence query temporarily unavailable; no successful query is asserted.") from error
        if data.get("error") or len(data.get("tables", [])) != 1:
            raise RuntimeError("Sentinel returned an incomplete evidence query")
        table = data["tables"][0]
        columns = [column["name"] for column in table["columns"]]
        counts = {row[columns.index("Kind")]: row[columns.index("Records")] for row in table["rows"]}
        return jsonify(runId=identity, route=route, queriedAt=datetime.now(timezone.utc).isoformat(),
                       sentinel="ingested" if counts.get("lab-action-audit", 0) else "queried-no-records",
                       alert="observed" if counts.get("correlated-alert", 0) else "queried-no-correlated-alert",
                       counts=counts, nativeEntraDefenderFirewallFeeds=False)

    @app.errorhandler(RuntimeError)
    def live_error(error):
        logging.exception("Live console operation failed")
        return jsonify(error="Live operation failed. Check the console cloud logs; no success is asserted."), 503
