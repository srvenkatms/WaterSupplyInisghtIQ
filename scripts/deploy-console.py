"""Publish the console behind Entra authentication without changing the OT lab."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen, HTTPRedirectHandler, build_opener
from urllib.error import HTTPError
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "console-deployment.json"
GRAPH = "https://graph.microsoft.com/v1.0"
WEB_API = "2025-01-01"
AUTH_SETTING = "entra-client-secret"
AZURE_CLI_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"


def compile_installed_bicep(source: Path, destination: Path) -> Path:
    config = Path(os.environ.get("AZURE_CONFIG_DIR", str(Path.home() / ".azure")))
    compiler = config / "bin" / ("bicep.exe" if os.name == "nt" else "bicep")
    result = subprocess.run([str(compiler), "build", str(source), "--outfile", str(destination)],
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f"Installed Bicep compilation failed: {result.stderr}")
    return destination


def azure(*args: str, timeout: int = 180) -> dict | list:
    executable = shutil.which("az")
    if not executable:
        raise RuntimeError("Azure CLI is required")
    result = subprocess.run([executable, *args, "--only-show-errors", "--output", "json"],
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Azure operation failed ({args[0]}): {result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def rest(method: str, url: str, body: dict | None = None) -> dict:
    args = ["rest", "--method", method, "--uri", url]
    if body is not None:
        args += ["--body", json.dumps(body)]
    return azure(*args)


def protected_rest(method: str, url: str, body: dict) -> dict:
    token = azure("account", "get-access-token", "--resource", "https://management.azure.com/")["accessToken"]
    request = Request(url, method=method, data=json.dumps(body).encode(),
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urlopen(request, timeout=120) as response:
        result = response.read()
        return json.loads(result) if result else {}


def persist(state: dict) -> None:
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE)


def wait_for_app(state: dict, secret_name: str | None = None) -> None:
    deadline = time.monotonic() + 300
    while True:
        properties = azure("containerapp", "show", "--resource-group", state["resourceGroup"],
                           "--name", state["siteName"])["properties"]
        if properties.get("provisioningState") == "Failed":
            raise RuntimeError("Azure console provisioning failed")
        configuration = properties.get("configuration") or {}
        secrets_ready = secret_name is None or any(
            secret["name"] == secret_name
            for secret in (configuration.get("secrets") or []))
        if (properties.get("provisioningState") == "Succeeded"
                and properties.get("runningStatus") == "Running"
                and properties.get("latestRevisionName")
                and properties.get("latestRevisionName") == properties.get("latestReadyRevisionName")
                and secrets_ready):
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("Azure console revision or authentication secret did not become ready in five minutes")
        time.sleep(5)


def verify_auth(properties: dict, state: dict) -> None:
    aad = properties.get("identityProviders", {}).get("azureActiveDirectory", {})
    registration = aad.get("registration", {})
    validation = properties.get("globalValidation", {})
    identities = aad.get("validation", {}).get("defaultAuthorizationPolicy", {}).get("allowedPrincipals", {}).get("identities")
    if (properties.get("platform", {}).get("enabled") is not True
            or validation.get("excludedPaths")
            or validation.get("unauthenticatedClientAction") != "RedirectToLoginPage"
            or validation.get("redirectToProvider", "").lower() != "azureactivedirectory"
            or aad.get("enabled") is not True
            or any(provider.get("enabled") for name, provider in properties.get("identityProviders", {}).items()
                   if name != "azureActiveDirectory")
            or registration.get("clientId") != state["applicationClientId"]
            or registration.get("openIdIssuer") != f'https://login.microsoftonline.com/{state["tenantId"]}/v2.0'
            or registration.get("clientSecretSettingName") != AUTH_SETTING
            or set(aad.get("validation", {}).get("allowedAudiences", [])) != {
                state["applicationClientId"], f'api://{state["applicationClientId"]}'}
            or identities != state["adminObjectIds"]
            or set(aad.get("validation", {}).get("defaultAuthorizationPolicy", {}).get("allowedApplications", []))
            != {state["applicationClientId"], AZURE_CLI_CLIENT_ID}
            or aad.get("validation", {}).get("defaultAuthorizationPolicy", {}).get("allowedPrincipals", {}).get("groups")
            or properties.get("httpSettings", {}).get("requireHttps") is not True):
        raise RuntimeError("Authentication policy differs from the approved, administrator-only Entra policy")


def provision(manifest: dict) -> dict:
    account = azure("account", "show")
    if (account["id"] != manifest["subscription"]
            or manifest.get("tenantId", account["tenantId"]) != account["tenantId"]):
        raise ValueError("Selected subscription differs from the approved deployment manifest")
    prefix = manifest["prefix"]
    group = f"rg-{prefix}-shared"
    resource_group = azure("group", "show", "--name", group)
    if resource_group.get("tags", {}).get("project") != "swu-lab":
        raise ValueError("The target resource group is not owned by this lab")
    user = azure("ad", "signed-in-user", "show")
    if manifest.get("routes") and user["userPrincipalName"].casefold() != resource_group["tags"]["owner"].casefold():
        raise ValueError("The signed-in user is not the approved Route A/B owner")
    marker = f"swu-lab:{account['id']}:{prefix}:console"
    applications = azure("ad", "app", "list", "--display-name", "WaterIQ Lab Console")
    applications = [app for app in applications if app.get("displayName") == "WaterIQ Lab Console"]
    if len(applications) > 1 or (applications and applications[0].get("notes") != marker):
        raise ValueError("An ambiguous or differently owned console app registration already exists")
    if applications:
        application = applications[0]
    else:
        application = rest("POST", f"{GRAPH}/applications", {
            "displayName": "WaterIQ Lab Console", "signInAudience": "AzureADMyOrg",
            "notes": marker, "requiredResourceAccess": [],
        })
    previous = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    if previous and (previous.get("subscription") != account["id"]
                     or previous.get("applicationClientId") != application["appId"]):
        raise ValueError("Existing console deployment metadata belongs to another subscription or registration")
    state = {**previous,
        "state": "provisioning", "subscription": account["id"], "tenantId": account["tenantId"],
        "resourceGroup": group, "applicationObjectId": application["id"],
        "applicationClientId": application["appId"], "adminObjectIds": previous.get("adminObjectIds", [user["id"]]),
    }
    persist(state)
    state["provider"] = "container-apps"
    state["prefix"] = prefix
    state["location"] = manifest["location"]
    storage_name = manifest.get("storageAccountName") or urlparse(manifest["queue"]).hostname.split(".")[0]
    state["liveControlEnabled"] = bool(manifest.get("routes"))
    parameters = [f"prefix={prefix}", f"location={manifest['location']}", f"storageAccountName={storage_name}"]
    if state["liveControlEnabled"]:
        parameters.append("enableLiveControl=true")
    results = ROOT / "test-results"
    results.mkdir(exist_ok=True)
    template = ROOT / "infra" / "console-container.bicep"
    if state["liveControlEnabled"]:
        template = compile_installed_bicep(template, results / "route-ab-console-container.json")
    print("Previewing the isolated console resources; OT VMs and existing collectors are not redeployed.", flush=True)
    preview = azure("deployment", "group", "what-if", "--resource-group", group,
                    "--template-file", str(template),
                    "--parameters", *parameters, "--no-pretty-print", timeout=600)
    summary = [{"resourceId": change.get("resourceId"), "changeType": change.get("changeType")}
               for change in preview.get("changes", [])]
    (results / ("route-ab-console-what-if.json" if state["liveControlEnabled"] else "console-what-if.json")).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("Provisioning a Consumption Container Apps environment, managed identity, registry and private state container.", flush=True)
    deployment = azure("deployment", "group", "create", "--resource-group", group,
                       "--name", f"{prefix}-console", "--template-file", str(template),
                       "--parameters", *parameters, timeout=1200)
    state.update({key: value["value"] for key, value in deployment["properties"]["outputs"].items()})
    state["state"] = "environment-provisioned"
    persist(state)
    print("Azure environment and private state are provisioned; no public application ingress exists yet.", flush=True)
    return state


def build_package(destination: Path, manifest: dict) -> str:
    readiness = {"warmupStartedAt": manifest.get("warmupStartedAt"),
                 "recordedAt": datetime.now(timezone.utc).isoformat()}
    files = [ROOT / "apps" / "swuapps" / name for name in ("__init__.py", "console.py", "live_console.py")]
    for folder, pattern in [
        ("apps/swuapps/console_templates", "*.html"), ("apps/swuapps/console_static", "*"),
        ("generator/swugen", "*.py"), ("generator/swugen", "*.kql"), ("profiles", "*.json"),
    ]:
        files.extend(path for path in ROOT.joinpath(*folder.split("/")).glob(pattern) if path.is_file())
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(ROOT).as_posix())
        archive.write(ROOT / "apps" / "console-requirements.txt", "requirements.txt")
        archive.writestr("console-readiness.json", json.dumps(readiness))
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def publish(state: dict, manifest: dict) -> None:
    if azure("account", "show")["id"] != state["subscription"]:
        raise ValueError("Selected subscription differs from the console deployment")
    results = ROOT / "test-results"
    results.mkdir(exist_ok=True)
    package = results / "wateriq-console.zip"
    state["packageSha256"] = build_package(package, manifest)
    image = f'{state["registryServer"]}/wateriq-console:{state["packageSha256"][:16]}'
    print("Building the cloud image from a clean application-only context.", flush=True)
    def run(command, **kwargs):
        result = subprocess.run(command, capture_output=True, text=True, timeout=1200, **kwargs)
        if result.returncode:
            raise RuntimeError(f"Image build/publish operation failed: {result.stderr[-4000:]} {result.stdout[-4000:]}")
        return result.stdout.strip()
    with tempfile.TemporaryDirectory(prefix="console-build-", dir=results) as directory:
        context = Path(directory)
        with zipfile.ZipFile(package) as archive:
            archive.extractall(context)
        shutil.copyfile(ROOT / "apps" / "Console.Dockerfile", context / "Dockerfile")
        run([sys.executable, "-m", "pip", "download", "--quiet", "--only-binary=:all:",
             "--platform", "manylinux2014_x86_64", "--python-version", "312", "--implementation", "cp",
             "--abi", "cp312", "-r", str(context / "requirements.txt"), "--dest", str(context / "wheelhouse")])
        endpoint = run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"])
        docker = ["docker", "--host", endpoint, "--config", str(context / "docker-auth")]
        run([*docker, "build", "--quiet", "--tag", image, str(context)])
        smoke_env = {
            "PYTHONPATH": "/lab/apps:/lab/generator", "SWU_CONSOLE_ENVIRONMENT": "azure",
            "SWU_CONSOLE_HOSTING_PROVIDER": "container-apps", "CONTAINER_APP_NAME": "console-smoke",
            "SWU_CONSOLE_HOSTNAME": "console-smoke.test.eastus2.azurecontainerapps.io",
            "SWU_CONSOLE_TENANT_ID": state["tenantId"],
            "SWU_CONSOLE_ADMIN_OBJECT_IDS": ",".join(state["adminObjectIds"]),
            "SWU_CONSOLE_STORAGE_URL": state["storageUrl"], "AZURE_CLIENT_ID": state["identityClientId"],
        }
        environment = [argument for key, value in smoke_env.items() for argument in ("--env", f"{key}={value}")]
        run([*docker, "run", "--rm", "--network", "none", *environment, "--entrypoint", "python", image, "-c",
             "from swuapps.console import create_app; app=create_app(); "
             "response=app.test_client().get('/',base_url='https://console-smoke.test.eastus2.azurecontainerapps.io'); "
             "assert response.status_code==401, response.status_code; "
             "from swugen import load_profile; [load_profile(utility) for utility in ('uta','utb','utc')]; "
             "from swugen.cli import simulate; from swugen.sink import JsonlSink; "
             "from datetime import datetime, timezone; import tempfile; "
             "temporary=tempfile.TemporaryDirectory(); "
             "counts=simulate('uta',datetime.now(timezone.utc),2,JsonlSink(temporary.name),"
             "scenario='SCN-02',run_id='console-image-smoke'); "
             "assert counts.get('SimSignin_CL',0)>0; temporary.cleanup(); "
             "print('Cloud startup, unauthenticated denial and packaged scenario generation verified')"])
        print("Linux cloud startup passed; publishing the image to the Azure registry.", flush=True)
        login = azure("acr", "login", "--name", state["registryName"], "--expose-token")
        run([*docker, "login", state["registryServer"], "--username", "00000000-0000-0000-0000-000000000000",
             "--password-stdin"], input=login["accessToken"])
        run([*docker, "push", "--quiet", image])
        state["imageDigest"] = json.loads(run([*docker, "image", "inspect", image]))[0]["RepoDigests"][0]
    state["image"] = state["imageDigest"]
    state["state"] = "image-published"
    persist(state)
    values = {"location": state["location"], "prefix": state["prefix"],
              "environmentName": state["environmentId"].split("/")[-1], "image": state["image"],
              "registryServer": state["registryServer"], "identityName": state["identityId"].split("/")[-1],
              "storageUrl": state["storageUrl"], "tenantId": state["tenantId"],
              "adminObjectIds": state["adminObjectIds"], "enableLiveControl": state.get("liveControlEnabled", False),
              "routeWorkspaceIds": {route["id"]: route["workspaceId"] for route in manifest.get("routes", [])}}
    print("Deploying the container with internal-only ingress before configuring authentication.", flush=True)
    with tempfile.TemporaryDirectory(prefix="console-parameters-") as directory:
        parameter_file = Path(directory) / "parameters.json"
        parameter_file.write_text(json.dumps({"parameters": {key: {"value": value} for key, value in values.items()}}))
        template = ROOT / "infra" / "console-app.bicep"
        if state.get("liveControlEnabled"):
            template = compile_installed_bicep(template, Path(directory) / "template.json")
        deployment = azure("deployment", "group", "create", "--resource-group", state["resourceGroup"],
                           "--name", f'{state["prefix"]}-console-app', "--template-file", str(template),
                           "--parameters", f"@{parameter_file}", timeout=1200)
    state.update({key: value["value"] for key, value in deployment["properties"]["outputs"].items()})
    state["url"] = f'https://{state["hostname"]}'
    state["state"] = "container-internal-only"
    persist(state)
    configure_auth(state)


def configure_auth(state: dict) -> None:
    app_url = f'{GRAPH}/applications/{state["applicationObjectId"]}'
    existing_scopes = rest("GET", app_url + "?$select=api").get("api", {}).get("oauth2PermissionScopes", [])
    existing_scope = next((scope for scope in existing_scopes if scope.get("value") == "Console.Access"), None)
    scope_id = state.setdefault("scopeId", existing_scope["id"] if existing_scope else str(uuid.uuid4()))
    persist(state)
    rest("PATCH", app_url, {
        "web": {"redirectUris": [state["url"] + "/.auth/login/aad/callback"], "homePageUrl": state["url"],
                "implicitGrantSettings": {"enableIdTokenIssuance": True, "enableAccessTokenIssuance": False}},
        "identifierUris": [f'api://{state["applicationClientId"]}'],
        "api": {"requestedAccessTokenVersion": 2, "oauth2PermissionScopes": [{
            "id": scope_id, "value": "Console.Access", "type": "Admin", "isEnabled": True,
            "adminConsentDisplayName": "Access the WaterIQ console",
            "adminConsentDescription": "Access the console as the signed-in, explicitly authorized lab administrator.",
        }]},
    })
    rest("PATCH", app_url, {"api": {"preAuthorizedApplications": [{
        "appId": AZURE_CLI_CLIENT_ID, "delegatedPermissionIds": [scope_id],
    }]}})
    principals = azure("ad", "sp", "list", "--filter", f"appId eq '{state['applicationClientId']}'")
    if not principals:
        azure("ad", "sp", "create", "--id", state["applicationClientId"])
    expiry = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
    credential = rest("POST", app_url + "/addPassword", {
        "passwordCredential": {"displayName": "WaterIQ Container Apps sign-in", "endDateTime": expiry}})
    state["credentialKeyId"] = credential["keyId"]
    state["credentialExpiresAt"] = expiry
    persist(state)
    site_url = "https://management.azure.com" + state["siteId"]
    protected_rest("PATCH", site_url + f"?api-version={WEB_API}", {"properties": {"configuration": {"secrets": [
        {"name": AUTH_SETTING, "value": credential["secretText"]},
    ]}}})
    del credential
    wait_for_app(state, AUTH_SETTING)
    policy = {
        "platform": {"enabled": True},
        "globalValidation": {"unauthenticatedClientAction": "RedirectToLoginPage",
                             "redirectToProvider": "azureactivedirectory"},
        "identityProviders": {"azureActiveDirectory": {
            "enabled": True,
            "registration": {"clientId": state["applicationClientId"], "clientSecretSettingName": AUTH_SETTING,
                             "openIdIssuer": f'https://login.microsoftonline.com/{state["tenantId"]}/v2.0'},
            "login": {"loginParameters": ["scope=openid profile email"]},
            "validation": {"allowedAudiences": [state["applicationClientId"], f'api://{state["applicationClientId"]}'],
                           "defaultAuthorizationPolicy": {
                               "allowedApplications": [state["applicationClientId"], AZURE_CLI_CLIENT_ID],
                               "allowedPrincipals": {"identities": state["adminObjectIds"]}}},
        }},
        "httpSettings": {"requireHttps": True},
        "login": {"cookieExpiration": {"convention": "FixedTime", "timeToExpiration": "08:00:00"},
                  "nonce": {"validateNonce": True}},
    }
    for attempt in range(4):
        try:
            rest("PUT", site_url + f"/authConfigs/current?api-version={WEB_API}", {"properties": policy})
            break
        except RuntimeError as error:
            if "AuthConfigSecretRefNotFound" not in str(error) or attempt == 3:
                raise
            print("Waiting for the newly configured Entra secret to converge.", flush=True)
            time.sleep(5)
    auth = rest("GET", site_url + f"/authConfigs/current?api-version={WEB_API}")
    verify_auth(auth["properties"], state)
    print("Administrator-only Entra policy verified. Enabling authenticated HTTPS public ingress.", flush=True)
    azure("containerapp", "ingress", "enable", "--resource-group", state["resourceGroup"],
          "--name", state["siteName"], "--type", "external", "--target-port", "8000", "--transport", "auto")
    wait_for_app(state, AUTH_SETTING)
    state["state"] = "published-verification-required"
    state["publishedAt"] = datetime.now(timezone.utc).isoformat()
    persist(state)
    print(f'Published console: {state["url"]}. Authentication and application verification are still required.', flush=True)


def verify(state: dict) -> None:
    wait_for_app(state, AUTH_SETTING)
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = build_opener(NoRedirect())

    def request(path, headers=None, value=None, method=None):
        request = Request(state["url"] + path, headers=headers or {}, method=method,
                          data=json.dumps(value).encode() if value is not None else None)
        try:
            with opener.open(request, timeout=60) as response:
                return response.status, response.headers, response.read()
        except HTTPError as error:
            return error.code, error.headers, error.read()

    site_url = "https://management.azure.com" + state["siteId"]
    auth = rest("GET", site_url + f"/authConfigs/current?api-version={WEB_API}")
    verify_auth(auth["properties"], state)
    evidence = {"checkedAt": datetime.now(timezone.utc).isoformat(), "url": state["url"],
                "unauthenticated": {}, "liveControlEnabled": state.get("liveControlEnabled", False)}
    for path in ("/", "/api/settings", "/api/status", "/api/scenarios", "/console_static/console.js"):
        status, _, _ = request(path)
        if status not in (302, 401, 403):
            raise RuntimeError(f"Unauthenticated {path} was not rejected: HTTP {status}")
        evidence["unauthenticated"][path] = status
    forged = base64.b64encode(json.dumps({"auth_typ": "aad", "claims": [
        {"typ": "tid", "val": state["tenantId"]}, {"typ": "oid", "val": state["adminObjectIds"][0]},
    ]}).encode()).decode()
    status, _, _ = request("/api/settings", headers={
        "X-MS-CLIENT-PRINCIPAL": forged,
        "X-MS-CLIENT-PRINCIPAL-ID": state["adminObjectIds"][0],
        "X-MS-CLIENT-PRINCIPAL-IDP": "aad",
    })
    if status not in (302, 401, 403):
        raise RuntimeError(f"Forged external principal headers were not rejected: HTTP {status}")
    evidence["forgedHeaderStatus"] = status
    status, _, _ = request("/api/settings", headers={
        "X-MS-CLIENT-PRINCIPAL-ID": "sid:forged-session",
        "X-MS-CLIENT-PRINCIPAL-NAME": "admin@example.invalid",
        "X-MS-CLIENT-PRINCIPAL-IDP": "aad",
    })
    if status not in (302, 401, 403):
        raise RuntimeError(f"Forged external session identity was not rejected: HTTP {status}")
    evidence["forgedSessionHeaderStatus"] = status
    status, headers, _ = request("/.auth/login/aad")
    location = urlparse(headers.get("Location", ""))
    if (status != 302 or location.hostname != "login.microsoftonline.com"
            or state["tenantId"] not in location.path
            or parse_qs(location.query).get("client_id") != [state["applicationClientId"]]):
        raise RuntimeError("The live sign-in route did not redirect to the intended tenant and app registration")
    evidence["entraRedirectVerified"] = True
    token = azure("account", "get-access-token", "--scope", f'api://{state["applicationClientId"]}/Console.Access')
    authorized = {"Authorization": f'Bearer {token["accessToken"]}'}
    status, _, html = request("/", headers=authorized)
    if status != 200 or b"AZURE / ENTRA PROTECTED" not in html:
        raise RuntimeError(f"Authenticated console page did not load correctly: HTTP {status}")
    if (b'id="view-live"' in html) != state.get("liveControlEnabled", False):
        raise RuntimeError("The deployed console live view differs from its requested feature configuration")
    import re
    csrf = re.search(rb'name="swu-csrf" content="([^"]+)"', html)[1].decode()
    authorized.update({"X-SWU-CSRF": csrf, "Content-Type": "application/json", "Origin": state["url"]})
    status, _, body = request("/api/settings", headers=authorized)
    if status != 200:
        raise RuntimeError(f"Authenticated private settings read failed: HTTP {status}")
    settings = json.loads(body)
    status, _, body = request("/api/settings", headers=authorized, value=settings, method="PUT")
    if status != 200:
        raise RuntimeError(f"Authenticated cloud settings persistence failed: HTTP {status}")
    saved = json.loads(body)
    status, _, body = request("/api/settings", headers=authorized)
    if status != 200 or json.loads(body) != saved:
        raise RuntimeError("The saved cloud settings could not be read back")
    status, _, body = request("/api/status", headers=authorized)
    if status != 200 or json.loads(body).get("scope") != "azure-console":
        raise RuntimeError("The authenticated status endpoint is not the Azure console")
    if json.loads(body).get("liveControlEnabled", False) is not state.get("liveControlEnabled", False):
        raise RuntimeError("The deployed console live API differs from its requested feature configuration")
    if saved["settings"]["mode"] == "on-demand":
        status, _, body = request("/api/datasets", headers=authorized,
                                  value={"scenario": "SCN-02", "confirmed": True}, method="POST")
        if status != 200:
            raise RuntimeError(f"Azure-hosted synthetic dataset generation failed: HTTP {status}")
        dataset = json.loads(body)
        if (dataset.get("executionLocation") != "azure"
                or dataset.get("physicalActionsExecuted") is not False
                or dataset.get("acceptanceCertified") is not False
                or dataset.get("kind") != "offline-synthetic-dataset"
                or dataset.get("counts", {}).get("SimSignin_CL", 0) <= 0):
            raise RuntimeError("Cloud dataset output differs from the declared synthetic-only behavior")
        evidence["cloudDatasetRunId"] = dataset["runId"]
        evidence["cloudDatasetCounts"] = dataset["counts"]
    evidence["authenticatedStatus"] = status
    evidence["settingsRevision"] = saved["revision"]
    evidence["privateSettingsReadbackVerified"] = True
    state["state"] = "verified"
    state["verifiedAt"] = evidence["checkedAt"]
    persist(state)
    (ROOT / "test-results" / ("route-ab-console-azure-verification.json" if state.get("liveControlEnabled") else
                            "console-azure-verification.json")).write_text(json.dumps(evidence, indent=2) + "\n")
    print(f'Azure console verified: {state["url"]}', flush=True)


def main() -> None:
    global STATE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("provision", "publish", "auth", "verify", "all"), default="all")
    parser.add_argument("--route-ab", action="store_true", help="Use separate Route A/B deployment and console state")
    args = parser.parse_args()
    if args.route_ab:
        STATE = ROOT / "route-ab-console-deployment.json"
    manifest = json.loads((ROOT / ("route-ab-deployment.json" if args.route_ab else "deployment-outputs.json")).read_text(encoding="utf-8"))
    if args.route_ab:
        manifest["subscription"] = manifest["subscriptionId"]
    state = provision(manifest) if args.phase in ("provision", "all") else json.loads(STATE.read_text(encoding="utf-8"))
    if args.phase in ("publish", "all"):
        publish(state, manifest)
    if args.phase == "auth":
        configure_auth(state)
    if args.phase in ("verify", "all"):
        verify(state)


if __name__ == "__main__":
    main()
