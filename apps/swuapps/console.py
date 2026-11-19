"""Lab console with loopback preview or administrator-only Azure hosting."""
from __future__ import annotations

import copy
import base64
import json
import logging
import os
import re
import secrets
import threading
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from swugen import load_profile, scenarios
from swugen.cli import simulate
from swugen.sink import JsonlSink

ROOT = Path(__file__).resolve().parents[2]
DEFAULTS = {
    "mode": "on-demand",
    "utilityId": "uta",
    "confirmRuns": True,
    "teams": {"enabled": False, "teamId": "", "channelId": ""},
}
SCENARIO_NAMES = {
    "SCN-01": "Suspicious operator and HMI dose change",
    "SCN-02": "Vendor password spray",
    "SCN-03": "Public Modbus exposure",
    "SCN-04": "Direct PLC setpoint manipulation",
    "SCN-05": "Disabled account activity",
    "SCN-06": "Unapproved remote-access software",
    "SCN-07": "Ransomware indicators and blocked movement",
    "SCN-08": "Vendor access and pump override",
    "SCN-09": "Operator compromise and telemetry loss",
    "SCN-10": "Normal operator control",
}


def require_azure_admin(tenant: str, administrators: set[str], provider: str) -> None:
    # Azure's authentication proxy strips caller identity headers and injects verified claims.
    if provider == "container-apps":
        # Container Apps supplies the principal ID, not App Service's claims envelope.
        identity = request.headers.get("X-MS-CLIENT-PRINCIPAL-ID", "").lower()
        if not identity or request.headers.get("X-MS-CLIENT-PRINCIPAL-IDP", "aad") != "aad":
            abort(401, "Microsoft Entra sign-in is required")
        if identity not in administrators:
            abort(403, "This identity is not an authorized lab administrator")
        return
    encoded = request.headers.get("X-MS-CLIENT-PRINCIPAL", "")
    if not encoded or len(encoded) > 32768:
        abort(401, "Microsoft Entra sign-in is required")
    try:
        principal = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
    except (ValueError, UnicodeError):
        abort(401, "Invalid authenticated identity")
    if not isinstance(principal, dict) or principal.get("auth_typ") != "aad":
        abort(401, "Microsoft Entra sign-in is required")
    claims = principal.get("claims")
    if not isinstance(claims, list) or any(
        not isinstance(claim, dict) or not isinstance(claim.get("typ"), str)
        or not isinstance(claim.get("val"), str) for claim in claims
    ):
        abort(401, "Invalid authenticated identity claims")
    tenants = {claim["val"].lower() for claim in claims
               if claim["typ"] in ("tid", "http://schemas.microsoft.com/identity/claims/tenantid")}
    identities = {claim["val"].lower() for claim in claims
                  if claim["typ"] in ("oid", "http://schemas.microsoft.com/identity/claims/objectidentifier")}
    if tenants != {tenant} or len(identities) != 1 or not identities.issubset(administrators):
        abort(403, "This identity is not an authorized lab administrator")


class SettingsConflict(Exception):
    pass


def validate_settings(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ValueError("Provide exactly mode, utilityId, confirmRuns and teams")
    if value["mode"] not in ("on-demand", "acceptance"):
        raise ValueError("Mode must be on-demand or acceptance")
    if value["utilityId"] not in ("uta", "utb", "utc"):
        raise ValueError("Select an existing utility")
    if type(value["confirmRuns"]) is not bool:
        raise ValueError("Run confirmation must be a boolean")
    teams = value["teams"]
    if not isinstance(teams, dict) or set(teams) != {"enabled", "teamId", "channelId"}:
        raise ValueError("Teams requires enabled, teamId and channelId")
    if type(teams["enabled"]) is not bool:
        raise ValueError("Teams enabled must be a boolean")
    if any(not isinstance(teams[key], str) or len(teams[key]) > 200 for key in ("teamId", "channelId")):
        raise ValueError("Teams IDs must be strings of at most 200 characters")
    result = copy.deepcopy(value)
    team = result["teams"]
    team["teamId"], team["channelId"] = team["teamId"].strip(), team["channelId"].strip()
    if team["teamId"]:
        try:
            uuid.UUID(team["teamId"])
        except ValueError as error:
            raise ValueError("Teams team ID must be a GUID") from error
    if team["channelId"] and not re.fullmatch(r"19:[A-Za-z0-9._~+%=-]+@thread\.(tacv2|skype)", team["channelId"]):
        raise ValueError("Teams channel ID must be a channel ID such as 19:...@thread.tacv2")
    if team["enabled"] and not (team["teamId"] and team["channelId"]):
        raise ValueError("Provide both Teams IDs before enabling its configuration")
    return result


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()

    def read(self) -> dict:
        with self.lock:
            if not self.path.exists():
                return {"revision": 0, "settings": copy.deepcopy(DEFAULTS)}
            return self.validate_document(json.loads(self.path.read_text(encoding="utf-8")))

    @staticmethod
    def validate_document(document: dict) -> dict:
        if (not isinstance(document, dict) or set(document) != {"revision", "settings"}
                or type(document["revision"]) is not int or document["revision"] < 0):
            raise ValueError("Invalid persisted console settings")
        return {"revision": document["revision"], "settings": validate_settings(document["settings"])}

    def save(self, value: dict, revision: int) -> dict:
        settings = validate_settings(value)
        if type(revision) is not int or revision < 0:
            raise ValueError("A valid settings revision is required")
        with self.lock:
            current = self.read()
            if revision != current["revision"]:
                raise SettingsConflict("Settings changed in another tab. Reload before saving.")
            document = {"revision": revision + 1, "settings": settings}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(document, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
            return document


class BlobSettingsStore:
    def __init__(self, container):
        self.container = container
        self.blob = container.get_blob_client("settings.json")
        self.lock = threading.RLock()

    def _read(self):
        from azure.core.exceptions import ResourceNotFoundError
        try:
            download = self.blob.download_blob()
            return SettingsStore.validate_document(json.loads(download.readall())), download.properties.etag
        except ResourceNotFoundError as error:
            if error.error_code != "BlobNotFound":
                raise
            return {"revision": 0, "settings": copy.deepcopy(DEFAULTS)}, None

    def read(self):
        with self.lock:
            return self._read()[0]

    def save(self, value: dict, revision: int) -> dict:
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceExistsError, ResourceModifiedError
        settings = validate_settings(value)
        if type(revision) is not int or revision < 0:
            raise ValueError("A valid settings revision is required")
        with self.lock:
            current, etag = self._read()
            if current["revision"] != revision:
                raise SettingsConflict("Settings changed in another tab. Reload before saving.")
            document = {"revision": revision + 1, "settings": settings}
            try:
                conditions = {"etag": etag, "match_condition": MatchConditions.IfNotModified} if etag else {}
                self.blob.upload_blob(json.dumps(document).encode(), overwrite=etag is not None, **conditions)
            except (ResourceExistsError, ResourceModifiedError) as error:
                raise SettingsConflict("Settings changed in another replica. Reload before saving.") from error
            return document

    def persist_run(self, directory: Path, run: str):
        for path in sorted(directory.iterdir()):
            self.container.upload_blob(f"runs/{run}/{path.name}", path.read_bytes(), overwrite=False)


def readiness_status(manifest_path: Path) -> dict:
    if not manifest_path.exists():
        return {"state": "not-configured", "message": "No Azure deployment manifest is available."}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    value = manifest.get("warmupStartedAt")
    if not value:
        return {"state": "not-started", "message": "Verified private readiness has not started warm-up."}
    started = datetime.fromisoformat(value)
    if started.tzinfo is None or started > datetime.now(timezone.utc):
        raise ValueError("Deployment warm-up timestamp is invalid")
    eligible = started + timedelta(days=7)
    return {
        "state": "warming-up" if datetime.now(timezone.utc) < eligible else "verification-required",
        "startedAt": started.isoformat(), "eligibleAt": eligible.isoformat(),
        "message": "Seven real days and actual hourly ingestion are required. No acceptance is certified here.",
    }


def create_app(settings_path: Path | None = None, manifest_path: Path | None = None) -> Flask:
    environment = os.environ.get("SWU_CONSOLE_ENVIRONMENT", "local")
    if environment not in ("local", "azure"):
        raise ValueError("SWU_CONSOLE_ENVIRONMENT must be local or azure")
    if (os.environ.get("WEBSITE_SITE_NAME") or os.environ.get("CONTAINER_APP_NAME")) and environment != "azure":
        raise ValueError("Azure hosting requires explicit Azure authentication configuration")
    hosted = environment == "azure"
    provider = os.environ.get("SWU_CONSOLE_HOSTING_PROVIDER", "app-service")
    if hosted:
        hostname = os.environ.get("SWU_CONSOLE_HOSTNAME" if provider == "container-apps" else "WEBSITE_HOSTNAME", "").lower()
        if provider == "container-apps":
            if (not os.environ.get("CONTAINER_APP_NAME")
                    or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+\.azurecontainerapps\.io", hostname)):
                raise ValueError("Azure hosting requires a configured Container Apps hostname")
        elif provider != "app-service" or (not os.environ.get("WEBSITE_SITE_NAME")
                or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)?\.azurewebsites\.net", hostname)
                or os.environ.get("WEBSITE_AUTH_ENABLED", "").lower() == "false"):
            raise ValueError("Azure hosting requires App Service with authentication enabled")
        tenant = str(uuid.UUID(os.environ["SWU_CONSOLE_TENANT_ID"]))
        administrators = {str(uuid.UUID(value.strip())) for value in
                          os.environ["SWU_CONSOLE_ADMIN_OBJECT_IDS"].split(",")}
        hosts = [hostname]
        settings_path = settings_path or Path(os.environ.get(
            "SWU_CONSOLE_SETTINGS_PATH", "/home/data/wateriq-console/settings.json"))
    else:
        hosts = ["localhost", "127.0.0.1", "[::1]"]
    app = Flask(__name__, template_folder="console_templates", static_folder="console_static")
    app.config.update(MAX_CONTENT_LENGTH=16 * 1024, TRUSTED_HOSTS=hosts)
    if hosted and provider == "container-apps":
        from azure.core.exceptions import AzureError
        from azure.identity import ManagedIdentityCredential
        from azure.storage.blob import ContainerClient
        endpoint = os.environ["SWU_CONSOLE_STORAGE_URL"]
        if not re.fullmatch(r"https://[a-z0-9]+\.blob\.core\.windows\.net/?", endpoint):
            raise ValueError("A private Azure Blob endpoint is required")
        credential = ManagedIdentityCredential(client_id=str(uuid.UUID(os.environ["AZURE_CLIENT_ID"])))
        container = ContainerClient(endpoint, "wateriq-console", credential=credential,
                                    connection_timeout=5, read_timeout=20, retry_total=2)
        store = BlobSettingsStore(container)

        @app.errorhandler(AzureError)
        def cloud_storage_error(error):
            app.logger.exception("Private Azure console storage operation failed")
            return jsonify(error="Private Azure storage is unavailable. Check the console's cloud logs."), 503
    else:
        store = SettingsStore(settings_path or ROOT / "local-data" / "console" / "settings.json")
    manifest = manifest_path or ROOT / ("console-readiness.json" if hosted else "deployment-outputs.json")
    csrf = secrets.token_urlsafe(32)
    app.extensions["settings_store"] = store
    live_enabled = os.environ.get("SWU_CONSOLE_LIVE", "false") == "true"
    if live_enabled:
        if not hosted or provider != "container-apps":
            raise ValueError("Live dispatch requires the authenticated Azure Container Apps console")
        from .live_console import register_live_routes
        register_live_routes(app, credential, endpoint, store.container)

    @app.before_request
    def authorize():
        if hosted:
            require_azure_admin(tenant, administrators, provider)
        elif request.remote_addr not in ("127.0.0.1", "::1"):
            abort(403, "The Admin console is local-only")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            expected_origin = f"https://{hostname}" if hosted else request.host_url.rstrip("/")
            if origin and origin.rstrip("/") != expected_origin:
                abort(403, "Cross-origin changes are not allowed")
            if not secrets.compare_digest(request.headers.get("X-SWU-CSRF", "").encode(), csrf.encode()):
                abort(403, "Reload the console before changing settings")

    @app.after_request
    def headers(response):
        if hosted:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.errorhandler(OSError)
    @app.errorhandler(ValueError)
    def configuration_error(error):
        app.logger.exception("Console configuration or storage operation failed")
        return jsonify(error="Console configuration could not be read or written. Check the server log."), 500

    @app.get("/")
    def index():
        return render_template("console.html", csrf=csrf, hosted=hosted,
                               hosting_service="Azure Container Apps" if provider == "container-apps" else "Azure App Service",
                               blob_storage=isinstance(store, BlobSettingsStore), live_enabled=live_enabled)

    @app.get("/api/settings")
    def settings():
        return jsonify(store.read())

    @app.put("/api/settings")
    def save():
        body = request.get_json()
        if not isinstance(body, dict) or set(body) != {"revision", "settings"}:
            abort(400, "Provide revision and settings")
        try:
            document = store.save(body["settings"], body["revision"])
        except SettingsConflict as error:
            app.logger.warning("Rejected concurrent console settings update")
            return jsonify(error=str(error)), 409
        except ValueError as error:
            app.logger.warning("Rejected invalid console settings: %s", error)
            return jsonify(error=str(error)), 400
        app.logger.info("Saved console settings revision %s", document["revision"])
        return jsonify(document)

    @app.get("/api/status")
    def status():
        return jsonify(readiness=readiness_status(manifest),
                       scope="azure-console" if hosted else "local-console", azureChanged=False,
                       authentication="microsoft-entra" if hosted else "loopback-only",
                       liveControlEnabled=live_enabled, readinessSource="deployment-manifest",
                       teamsStatus="Configuration export only; Azure deployment and connector authorization are separate.")

    @app.get("/api/teams/parameters")
    def teams_parameters():
        teams = store.read()["settings"]["teams"]
        response = jsonify({
            "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
            "contentVersion": "1.0.0.0",
            "parameters": {
                "deployTeamsPlaybook": {"value": teams["enabled"]},
                "teamsTeamId": {"value": teams["teamId"]},
                "teamsChannelId": {"value": teams["channelId"]},
            },
        })
        response.headers["Content-Disposition"] = 'attachment; filename="teams.parameters.json"'
        return response

    @app.get("/api/scenarios")
    def catalogue():
        profile = load_profile(store.read()["settings"]["utilityId"])
        return jsonify(scenarios=[
            {"id": name, "name": label, **scenarios.expected_rules(name, profile)}
            for name, label in SCENARIO_NAMES.items()])

    @app.post("/api/datasets")
    def dataset():
        body = request.get_json()
        if (not isinstance(body, dict) or set(body) != {"scenario", "confirmed"}
                or not isinstance(body["scenario"], str) or body["scenario"] not in SCENARIO_NAMES
                or type(body["confirmed"]) is not bool):
            abort(400, "Choose a known scenario and supply a boolean confirmation")
        with store.lock:
            config = store.read()["settings"]
            if config["mode"] != "on-demand":
                abort(409, "Offline datasets are not acceptance evidence. Use the live acceptance harness.")
            if config["confirmRuns"] and not body["confirmed"]:
                abort(409, "Confirm the dataset generation first")
            profile = load_profile(config["utilityId"])
            if not scenarios.expected_rules(body["scenario"], profile)["applicable"]:
                abort(400, "This scenario is not applicable to the selected utility")
            run = "console-" + uuid.uuid4().hex
            with tempfile.TemporaryDirectory(prefix="wateriq-dataset-") as temporary:
                cloud = isinstance(store, BlobSettingsStore)
                directory = Path(temporary) if cloud else store.path.parent / "runs" / run
                start = datetime.now(timezone.utc).replace(second=0, microsecond=0)
                totals = simulate(config["utilityId"], start, 2, JsonlSink(directory),
                                  scenario=body["scenario"], run_id=run)
                result = {
                    "runId": run, "scenario": body["scenario"], "utilityId": config["utilityId"],
                    "kind": "offline-synthetic-dataset", "acceptanceCertified": False,
                    "azureChanged": False, "physicalActionsExecuted": False, "counts": totals,
                    "executionLocation": "azure" if hosted else "local",
                    "note": ("Two simulated hours of Azure-hosted JSONL only. No PLC, HMI, Sentinel or Teams actions were executed."
                             if hosted else "Two simulated hours of local JSONL only. No PLC, HMI, Azure or Teams actions were executed."),
                }
                (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
                if cloud:
                    store.persist_run(directory, run)
                return jsonify(result)

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    create_app().run(host="127.0.0.1", port=8787, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
