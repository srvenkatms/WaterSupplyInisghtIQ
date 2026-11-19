import copy
import hashlib
import io
import json
import runpy
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest

DEPLOY = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "deploy-console.py"))
APP_ID = "11111111-1111-4111-8111-111111111111"
TENANT = "22222222-2222-4222-8222-222222222222"
ADMIN = "33333333-3333-4333-8333-333333333333"
STATE = {"applicationClientId": APP_ID, "tenantId": TENANT, "adminObjectIds": [ADMIN]}


def authentication():
    return {
        "platform": {"enabled": True},
        "globalValidation": {"redirectToProvider": "azureactivedirectory",
                             "unauthenticatedClientAction": "RedirectToLoginPage"},
        "identityProviders": {"azureActiveDirectory": {
            "enabled": True,
            "registration": {"clientId": APP_ID, "openIdIssuer": f"https://login.microsoftonline.com/{TENANT}/v2.0",
                             "clientSecretSettingName": DEPLOY["AUTH_SETTING"]},
            "validation": {"allowedAudiences": [APP_ID, f"api://{APP_ID}"],
                           "defaultAuthorizationPolicy": {
                               "allowedApplications": [APP_ID, DEPLOY["AZURE_CLI_CLIENT_ID"]],
                               "allowedPrincipals": {"identities": [ADMIN]}}},
        }},
        "httpSettings": {"requireHttps": True},
    }


def test_console_publication_requires_exact_admin_only_authentication_policy():
    DEPLOY["verify_auth"](authentication(), STATE)


def test_console_auth_uses_platform_managed_session_keys(monkeypatch):
    configure_auth = DEPLOY["configure_auth"]
    namespace = configure_auth.__globals__
    state = {**STATE, "applicationObjectId": APP_ID, "siteId": "/test/console",
             "siteName": "console", "resourceGroup": "test", "url": "https://console.example"}
    writes = []
    policies = []

    def rest(method, url, body=None):
        if url.endswith("/addPassword"):
            return {"keyId": "test-key", "secretText": "test-client-secret"}
        if method == "GET" and "/authConfigs/" in url:
            return {"properties": authentication()}
        if method == "PUT" and "/authConfigs/" in url:
            policies.append(body["properties"])
        return {}

    monkeypatch.setitem(namespace, "rest", rest)
    monkeypatch.setitem(namespace, "azure", lambda *args: [{"id": "test-principal"}])
    monkeypatch.setitem(namespace, "persist", lambda state: None)
    monkeypatch.setitem(namespace, "protected_rest", lambda method, url, body: writes.append(body))
    monkeypatch.setitem(namespace, "wait_for_app", lambda *args: None)
    configure_auth(state)

    stored = {secret["name"]: secret["value"]
              for secret in writes[0]["properties"]["configuration"]["secrets"]}
    assert stored == {"entra-client-secret": "test-client-secret"}
    DEPLOY["verify_auth"](policies[0], state)
    assert "encryptionSettings" not in policies[0]


def test_console_waits_for_ready_revision_and_secret_before_authentication(monkeypatch):
    wait = DEPLOY["wait_for_app"]
    ready = {"provisioningState": "Succeeded", "runningStatus": "Running",
             "latestRevisionName": "new", "latestReadyRevisionName": "new",
             "configuration": {"secrets": [{"name": DEPLOY["AUTH_SETTING"]}]}}
    old_revision = {**ready, "latestReadyRevisionName": "old"}
    missing_secret = {**ready, "configuration": {"secrets": []}}
    null_configuration = {**ready, "configuration": None}
    null_secrets = {**ready, "configuration": {"secrets": None}}
    responses = iter([old_revision, null_configuration, null_secrets, missing_secret, ready])
    sleeps = []
    from types import SimpleNamespace
    monkeypatch.setitem(wait.__globals__, "azure", lambda *args: {"properties": next(responses)})
    monkeypatch.setitem(wait.__globals__, "time", SimpleNamespace(monotonic=lambda: 0, sleep=sleeps.append))
    wait({"resourceGroup": "test", "siteName": "console"}, DEPLOY["AUTH_SETTING"])
    assert sleeps == [5, 5, 5, 5]


@pytest.mark.parametrize("configuration", [None, {}, {"secrets": None}])
def test_console_never_accepts_missing_authentication_secret(monkeypatch, configuration):
    wait = DEPLOY["wait_for_app"]
    properties = {"provisioningState": "Succeeded", "runningStatus": "Running",
                  "latestRevisionName": "new", "latestReadyRevisionName": "new",
                  "configuration": configuration}
    from types import SimpleNamespace
    clock = iter([0, 301])
    monkeypatch.setitem(wait.__globals__, "azure", lambda *args: {"properties": properties})
    monkeypatch.setitem(wait.__globals__, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    with pytest.raises(TimeoutError, match="authentication secret did not become ready"):
        wait({"resourceGroup": "test", "siteName": "console"}, DEPLOY["AUTH_SETTING"])


@pytest.mark.parametrize("path,value", [
    (["platform", "enabled"], False),
    (["globalValidation", "unauthenticatedClientAction"], "AllowAnonymous"),
    (["globalValidation", "excludedPaths"], ["/api/*"]),
    (["globalValidation", "redirectToProvider"], "github"),
    (["identityProviders", "github"], {"enabled": True}),
    (["identityProviders", "azureActiveDirectory", "registration", "clientId"], "another-app"),
    (["identityProviders", "azureActiveDirectory", "registration", "openIdIssuer"],
     "https://login.microsoftonline.com/common/v2.0"),
    (["identityProviders", "azureActiveDirectory", "validation", "allowedAudiences"], ["management"]),
    (["identityProviders", "azureActiveDirectory", "validation", "defaultAuthorizationPolicy",
      "allowedPrincipals", "identities"], []),
    (["identityProviders", "azureActiveDirectory", "validation", "defaultAuthorizationPolicy",
      "allowedApplications"], ["another-client"]),
    (["httpSettings", "requireHttps"], False),
])
def test_console_publication_refuses_unauthenticated_or_broader_access(path, value):
    properties = copy.deepcopy(authentication())
    target = properties
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(RuntimeError, match="administrator-only Entra"):
        DEPLOY["verify_auth"](properties, STATE)


def test_console_zip_contains_only_application_and_minimal_readiness_data(tmp_path):
    package = tmp_path / "console.zip"
    started = "2026-11-05T17:07:46.014409+00:00"
    digest = DEPLOY["build_package"](package, {"warmupStartedAt": started, "sshKey": "must-not-be-packaged"})
    assert digest == hashlib.sha256(package.read_bytes()).hexdigest()
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        assert {"apps/swuapps/console.py", "generator/swugen/cli.py", "profiles/uta.json",
                "generator/swugen/source_health.kql", "requirements.txt",
                "console-readiness.json"}.issubset(names)
        assert "deployment-outputs.json" not in names
        assert not any(name.startswith(("local-data/", ".venv/", ".git/", "test-results/")) for name in names)
        readiness = json.loads(archive.read("console-readiness.json"))
        assert set(readiness) == {"warmupStartedAt", "recordedAt"}
        assert readiness["warmupStartedAt"] == started
        assert b"must-not-be-packaged" not in archive.read("console-readiness.json")


def test_console_live_feature_uses_canonical_environment_booleans():
    root = Path(__file__).resolve().parents[2]
    template = (root / "infra" / "console-app.bicep").read_text()
    assert "{ name: 'SWU_CONSOLE_LIVE', value: enableLiveControl ? 'true' : 'false' }" in template


@pytest.mark.parametrize("requested,reported,has_view,error", [
    (False, False, False, None),
    (False, None, False, None),
    (True, True, True, None),
    (True, False, True, "live API"),
    (True, None, True, "live API"),
    (True, True, False, "live view"),
    (False, True, False, "live API"),
    (False, False, True, "live view"),
])
def test_verification_checks_actual_live_feature_and_preserves_legacy_console(
        monkeypatch, tmp_path, requested, reported, has_view, error):
    verify = DEPLOY["verify"]
    namespace = verify.__globals__
    state = {**STATE, "siteId": "/test/console", "url": "https://console.example",
             "liveControlEnabled": requested}
    settings = {"revision": 0, "settings": {"mode": "acceptance"}}

    class Response(io.BytesIO):
        def __init__(self, status, body=b"", headers=None):
            super().__init__(body)
            self.status = status
            self.headers = headers or {}

    class Opener:
        def open(self, request, timeout):
            path = urlsplit(request.full_url).path
            if path == "/.auth/login/aad":
                return Response(302, headers={"Location":
                    f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize?client_id={APP_ID}"})
            if not request.get_header("Authorization"):
                return Response(401)
            if path == "/":
                html = b'AZURE / ENTRA PROTECTED <meta name="swu-csrf" content="test-csrf">'
                if has_view:
                    html += b'<section id="view-live"></section>'
                return Response(200, html)
            if path == "/api/settings":
                return Response(200, json.dumps(settings).encode())
            assert path == "/api/status"
            status = {"scope": "azure-console"}
            if reported is not None:
                status["liveControlEnabled"] = reported
            return Response(200, json.dumps(status).encode())

    (tmp_path / "test-results").mkdir()
    monkeypatch.setitem(namespace, "ROOT", tmp_path)
    monkeypatch.setitem(namespace, "build_opener", lambda *args: Opener())
    monkeypatch.setitem(namespace, "wait_for_app", lambda *args: None)
    monkeypatch.setitem(namespace, "rest", lambda *args: {"properties": authentication()})
    monkeypatch.setitem(namespace, "azure", lambda *args: {"accessToken": "unit-test-token"})
    monkeypatch.setitem(namespace, "persist", lambda state: None)
    if error:
        with pytest.raises(RuntimeError, match=error):
            verify(state)
        assert state.get("state") != "verified"
    else:
        verify(state)
        assert state["state"] == "verified"
