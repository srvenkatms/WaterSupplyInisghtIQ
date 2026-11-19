import copy
import base64
import json
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from unittest.mock import Mock

import pytest

from swuapps import console

AZURE_HOST = "app-wateriq-test.eastus2-01.azurewebsites.net"
AZURE_URL = f"https://{AZURE_HOST}"
TENANT = "11111111-2222-4333-8444-555555555555"
ADMIN = "22222222-3333-4444-8555-666666666666"


def principal_header(tenant=TENANT, identity=ADMIN, provider="aad"):
    value = {"auth_typ": provider, "claims": [
        {"typ": "http://schemas.microsoft.com/identity/claims/tenantid", "val": tenant},
        {"typ": "http://schemas.microsoft.com/identity/claims/objectidentifier", "val": identity},
    ]}
    return base64.b64encode(json.dumps(value).encode()).decode()


@pytest.fixture
def azure_api(tmp_path, monkeypatch):
    for key, value in {
        "SWU_CONSOLE_ENVIRONMENT": "azure", "WEBSITE_SITE_NAME": "app-wateriq-test",
        "WEBSITE_HOSTNAME": AZURE_HOST, "WEBSITE_AUTH_ENABLED": "True",
        "SWU_CONSOLE_TENANT_ID": TENANT, "SWU_CONSOLE_ADMIN_OBJECT_IDS": ADMIN,
    }.items():
        monkeypatch.setenv(key, value)
    settings = tmp_path / "azure-settings.json"
    manifest = tmp_path / "azure-readiness.json"
    manifest.write_text(json.dumps({"warmupStartedAt": datetime.now(timezone.utc).isoformat()}))
    app = console.create_app(settings, manifest)
    app.testing = True
    client = app.test_client()
    headers = {"X-MS-CLIENT-PRINCIPAL": principal_header()}
    page = client.get("/", base_url=AZURE_URL, headers=headers)
    assert page.status_code == 200
    headers["X-SWU-CSRF"] = re.search(r'name="swu-csrf" content="([^"]+)"', page.text)[1]
    return client, headers, settings, manifest


@pytest.fixture
def api(tmp_path):
    settings = tmp_path / "settings.json"
    manifest = tmp_path / "deployment.json"
    started = datetime.now(timezone.utc) - timedelta(hours=1)
    manifest.write_text(json.dumps({"warmupStartedAt": started.isoformat()}))
    app = console.create_app(settings, manifest)
    app.testing = True
    client = app.test_client()
    page = client.get("/")
    token = re.search(r'name="swu-csrf" content="([^"]+)"', page.text)[1]
    return client, {"X-SWU-CSRF": token}, settings, manifest


def save(api, settings=None, revision=0):
    client, headers, _, _ = api
    return client.put("/api/settings", headers=headers,
                      json={"revision": revision, "settings": settings or copy.deepcopy(console.DEFAULTS)})


def test_console_defaults_are_local_on_demand_with_teams_off(api):
    client, _, path, _ = api
    page = client.get("/")
    assert page.status_code == 200 and "Administration" in page.text
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    assert client.get("/api/settings").json == {"revision": 0, "settings": console.DEFAULTS}
    assert not path.exists()
    assert client.get("/console_static/console.js").status_code == 200
    assert client.get("/console_static/console.css").status_code == 200
    assert 'id="session-expired"' not in page.text


def test_hosted_console_provides_explicit_same_site_sign_in_recovery(azure_api):
    client, headers, _, _ = azure_api
    page = client.get("/", base_url=AZURE_URL, headers=headers)
    assert 'id="session-expired"' in page.text
    assert 'href="/.auth/login/aad?post_login_redirect_uri=%2F"' in page.text
    assert "no control command is automatically retried" in page.text


@pytest.mark.parametrize("enabled", [False, True])
def test_motor_pump_tank_diagrams_are_unique_and_live_view_only(api, enabled):
    with api[0].application.test_request_context("/"):
        page = api[0].application.jinja_env.get_template("console.html").render(
            csrf="test-csrf", hosted=False, live_enabled=enabled)
    identities = re.findall(r'\bid="([^"]+)"', page)
    assert len(identities) == len(set(identities))
    for route in ("routea", "routeb"):
        assert (f'id="{route}-plant"' in page) is enabled
        assert (f'id="{route}-machinery-state"' in page) is enabled
        assert (f'id="{route}-water"' in page) is enabled
        assert (f'id="{route}-sensor-level"' in page) is enabled
        assert (f'id="{route}-sensor-chlorine"' in page) is enabled
        assert (f'id="{route}-plc-state"' in page) is enabled
        assert (f'id="{route}-command-state"' in page) is enabled
        assert (f'id="{route}-plc-command"' in page) is enabled
    if enabled:
        assert page.count('class="motor-rotor rotor"') == 2
        assert page.count('class="pump-rotor rotor"') == 2
        assert page.count('class="flow-water"') == 2
        assert 'class="plant-schematic running"' not in page
        assert "not separate motor or flow feedback" in page
        assert 'id="routeb-hmi"' in page and 'id="routea-hmi"' not in page
        assert "directly to PLC over Modbus; no HMI login" in page
        assert "password-only HMI to PLC" in page


def test_save_is_persistent_and_does_not_change_live_warmup(api):
    client, _, path, manifest = api
    before = manifest.read_bytes()
    settings = copy.deepcopy(console.DEFAULTS)
    settings.update(mode="acceptance", utilityId="utc", confirmRuns=False)
    response = save(api, settings)
    assert response.status_code == 200 and response.json["revision"] == 1
    restarted = console.create_app(path, manifest).test_client()
    assert restarted.get("/api/settings").json["settings"] == settings
    assert manifest.read_bytes() == before
    assert client.get("/api/status").json["readiness"]["state"] == "warming-up"


def test_access_policy_is_clearly_planned_and_vpn_is_disabled(api):
    class Inputs(HTMLParser):
        def __init__(self):
            super().__init__()
            self.fields = {}

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == "input" and values.get("name") == "accessMode":
                self.fields[values["id"]] = values

    page = api[0].get("/").text
    inputs = Inputs()
    inputs.feed(page)
    assert set(inputs.fields) == {"access-entra", "access-vpn"}
    assert all("disabled" in values for values in inputs.fields.values())
    assert "checked" in inputs.fields["access-entra"]
    assert "checked" not in inputs.fields["access-vpn"]
    assert 'aria-describedby="vpn-unavailable"' in page
    assert "Microsoft Entra sign-in will still be required" in page
    assert "not active protection" in page
    assert "No VPN gateway will be provisioned or billed by this screen" in page


@pytest.mark.parametrize("change", [
    {"mode": "accelerated"}, {"utilityId": "../uta"}, {"confirmRuns": "false"},
    {"teams": {"enabled": True, "teamId": "", "channelId": ""}},
    {"teams": {"enabled": "true", "teamId": "", "channelId": ""}},
    {"teams": {"enabled": False, "teamId": "not-a-guid", "channelId": ""}},
    {"teams": {"enabled": False, "teamId": "", "channelId": "https://example.com/secret"}},
    {"teams": {"enabled": False, "teamId": "", "channelId": "", "token": "not-allowed"}},
    {"warmupStartedAt": "2020-01-01T00:00:00Z"},
    {"accessMode": "vpn-private"},
    {"accessMode": "authenticated-internet"},
])
def test_invalid_or_secret_settings_never_persist(api, change):
    settings = copy.deepcopy(console.DEFAULTS)
    settings.update(change)
    assert save(api, settings).status_code == 400
    assert not api[2].exists()


def test_csrf_origin_remote_address_and_host_boundaries(api):
    client, headers, _, _ = api
    body = {"revision": 0, "settings": console.DEFAULTS}
    assert client.put("/api/settings", json=body).status_code == 403
    assert client.put("/api/settings", json=body, headers=headers | {"Origin": "https://evil.example"}).status_code == 403
    assert client.put("/api/settings", json=body, headers=headers,
                      environ_overrides={"REMOTE_ADDR": "10.10.4.10"}).status_code == 403
    assert client.get("/api/settings", base_url="http://evil.example").status_code == 400
    assert not api[2].exists()


def test_stale_tab_cannot_overwrite_saved_settings(api):
    assert save(api).status_code == 200
    assert save(api).status_code == 409
    assert api[0].get("/api/settings").json["revision"] == 1


def test_corrupt_storage_is_not_replaced_with_defaults(api):
    api[2].write_text("{broken")
    response = api[0].get("/api/settings")
    assert response.status_code == 500
    assert api[2].read_text() == "{broken"


def test_teams_export_reuses_real_bicep_parameters_without_deployment(api):
    client, _, _, _ = api
    settings = copy.deepcopy(console.DEFAULTS)
    settings["teams"] = {"enabled": True, "teamId": "11111111-1111-4111-8111-111111111111",
                         "channelId": "19:abc@thread.tacv2"}
    assert save(api, settings).status_code == 200
    export = client.get("/api/teams/parameters")
    assert export.json["parameters"] == {
        "deployTeamsPlaybook": {"value": True},
        "teamsTeamId": {"value": settings["teams"]["teamId"]},
        "teamsChannelId": {"value": settings["teams"]["channelId"]},
    }
    assert "attachment" in export.headers["Content-Disposition"]
    assert client.get("/api/status").json["azureChanged"] is False


def test_acceptance_mode_cannot_generate_accelerated_demo_evidence(api):
    settings = copy.deepcopy(console.DEFAULTS)
    settings["mode"] = "acceptance"
    assert save(api, settings).status_code == 200
    client, headers, _, _ = api
    response = client.post("/api/datasets", headers=headers, json={"scenario": "SCN-01", "confirmed": True})
    assert response.status_code == 409 and "not acceptance evidence" in response.json["error"]
    assert not (api[2].parent / "runs").exists()


def test_on_demand_generates_real_local_files_not_plc_or_azure_actions(api):
    client, headers, path, _ = api
    body = {"scenario": "SCN-02", "confirmed": True}
    response = client.post("/api/datasets", headers=headers, json=body)
    assert response.status_code == 200
    result = response.json
    assert result["counts"]["SimSignin_CL"] > 0
    assert result["acceptanceCertified"] is False and result["physicalActionsExecuted"] is False
    assert result["azureChanged"] is False
    directory = path.parent / "runs" / result["runId"]
    assert (directory / "result.json").exists()
    assert list(directory.glob("uta.*.jsonl"))


@pytest.mark.parametrize("body", [
    {"scenario": "SCN-01", "confirmed": False},
    {"scenario": ["SCN-01"], "confirmed": True},
    {"scenario": "SCN-99", "confirmed": True},
])
def test_invalid_or_unconfirmed_datasets_do_not_run(api, body):
    client, headers, path, _ = api
    assert client.post("/api/datasets", headers=headers, json=body).status_code in (400, 409)
    assert not (path.parent / "runs").exists()


def test_catalogue_honors_saved_utility_tier(api):
    settings = copy.deepcopy(console.DEFAULTS)
    settings["utilityId"] = "utb"
    assert save(api, settings).status_code == 200
    rows = api[0].get("/api/scenarios").json["scenarios"]
    assert len(rows) == 10
    assert not next(row for row in rows if row["id"] == "SCN-04")["applicable"]


def test_elapsed_time_alone_never_claims_acceptance(api):
    manifest = api[3]
    manifest.write_text(json.dumps({"warmupStartedAt": (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()}))
    assert api[0].get("/api/status").json["readiness"]["state"] == "verification-required"


@pytest.mark.parametrize("principal", ["", "not-base64", "W10=", "e30=",
                                     principal_header(provider="github")])
def test_azure_requests_require_valid_entra_identity(azure_api, principal):
    client = azure_api[0]
    for path in ("/", "/api/settings", "/api/status", "/console_static/console.js"):
        response = client.get(path, base_url=AZURE_URL, headers={"X-MS-CLIENT-PRINCIPAL": principal})
        assert response.status_code == 401


@pytest.mark.parametrize("principal", [
    principal_header(tenant="00000000-0000-0000-0000-000000000001"),
    principal_header(identity="00000000-0000-0000-0000-000000000001"),
])
def test_azure_authentication_does_not_authorize_another_tenant_or_user(azure_api, principal):
    response = azure_api[0].get("/api/settings", base_url=AZURE_URL,
                                headers={"X-MS-CLIENT-PRINCIPAL": principal})
    assert response.status_code == 403
    assert not azure_api[2].exists()


def test_azure_admin_sees_actual_hosting_and_disabled_vpn(azure_api):
    client, headers, _, _ = azure_api
    page = client.get("/", base_url=AZURE_URL, headers=headers)
    assert 'data-hosting="azure"' in page.text
    assert "ACTIVE - MICROSOFT ENTRA" in page.text
    assert "This preview is local-only" not in page.text
    assert "FUTURE OPTION - DISABLED" in page.text
    assert page.headers["Strict-Transport-Security"] == "max-age=31536000"
    status = client.get("/api/status", base_url=AZURE_URL, headers=headers).json
    assert status["scope"] == "azure-console"
    assert status["authentication"] == "microsoft-entra"
    assert status["readinessSource"] == "deployment-manifest"
    assert status["liveControlEnabled"] is False


def test_azure_settings_keep_csrf_origin_host_and_revision_guards(azure_api):
    client, headers, settings, manifest = azure_api
    body = {"revision": 0, "settings": console.DEFAULTS}
    assert client.put("/api/settings", base_url=AZURE_URL, headers={
        "X-MS-CLIENT-PRINCIPAL": headers["X-MS-CLIENT-PRINCIPAL"]}, json=body).status_code == 403
    assert client.put("/api/settings", base_url=AZURE_URL, headers=headers | {
        "Origin": "https://evil.example"}, json=body).status_code == 403
    assert client.get("/api/settings", base_url="https://evil.example", headers=headers).status_code == 400
    response = client.put("/api/settings", base_url=AZURE_URL,
                          headers=headers | {"Origin": AZURE_URL}, json=body)
    assert response.status_code == 200 and response.json["revision"] == 1
    restarted = console.create_app(settings, manifest).test_client()
    assert restarted.get("/api/settings", base_url=AZURE_URL, headers=headers).json == response.json
    assert client.put("/api/settings", base_url=AZURE_URL, headers=headers, json=body).status_code == 409


def test_azure_dataset_runs_in_cloud_storage_without_claiming_live_actions(azure_api):
    client, headers, settings, _ = azure_api
    result = client.post("/api/datasets", base_url=AZURE_URL, headers=headers,
                         json={"scenario": "SCN-02", "confirmed": True})
    assert result.status_code == 200
    assert result.json["executionLocation"] == "azure"
    assert result.json["physicalActionsExecuted"] is False
    assert result.json["acceptanceCertified"] is False
    assert "Azure-hosted JSONL" in result.json["note"]
    assert (settings.parent / "runs" / result.json["runId"] / "result.json").exists()


def test_azure_cannot_fall_back_to_local_authentication(azure_api, monkeypatch):
    monkeypatch.delenv("SWU_CONSOLE_ENVIRONMENT")
    with pytest.raises(ValueError, match="explicit Azure authentication"):
        console.create_app()


def test_azure_refuses_disabled_authentication_or_missing_administrators(azure_api, monkeypatch):
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "False")
    with pytest.raises(ValueError, match="authentication enabled"):
        console.create_app()
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "True")
    monkeypatch.setenv("SWU_CONSOLE_ADMIN_OBJECT_IDS", "")
    with pytest.raises(ValueError):
        console.create_app()


def test_container_apps_uses_private_blob_storage_and_surfaces_cloud_errors(azure_api, monkeypatch):
    from azure.core.exceptions import AzureError, ResourceNotFoundError
    hostname = "ca-wateriq.test.eastus2.azurecontainerapps.io"
    for key, value in {
        "SWU_CONSOLE_HOSTING_PROVIDER": "container-apps", "CONTAINER_APP_NAME": "ca-wateriq",
        "SWU_CONSOLE_HOSTNAME": hostname, "SWU_CONSOLE_STORAGE_URL": "https://wateriq.blob.core.windows.net/",
        "AZURE_CLIENT_ID": ADMIN,
    }.items():
        monkeypatch.setenv(key, value)
    container = Mock()
    missing = ResourceNotFoundError("missing")
    missing.error_code = "BlobNotFound"
    container.get_blob_client.return_value.download_blob.side_effect = missing
    monkeypatch.setattr("azure.storage.blob.ContainerClient", Mock(return_value=container))
    monkeypatch.setattr("azure.identity.ManagedIdentityCredential", Mock())
    app = console.create_app(manifest_path=azure_api[3])
    assert isinstance(app.extensions["settings_store"], console.BlobSettingsStore)
    client = app.test_client()
    headers = {"X-MS-CLIENT-PRINCIPAL-ID": ADMIN, "X-MS-CLIENT-PRINCIPAL-IDP": "aad"}
    origin = f"https://{hostname}"
    for rejected in ({}, {"X-MS-CLIENT-PRINCIPAL": principal_header()},
                     {**headers, "X-MS-CLIENT-PRINCIPAL-IDP": "github"}):
        assert client.get("/", base_url=origin, headers=rejected).status_code == 401
    assert client.get("/", base_url=origin, headers={
        **headers, "X-MS-CLIENT-PRINCIPAL-ID": "00000000-0000-0000-0000-000000000001"
    }).status_code == 403
    assert client.get("/", base_url=origin, headers={
        "X-MS-CLIENT-PRINCIPAL-ID": ADMIN
    }).status_code == 200
    assert client.get("/", base_url=origin, headers={
        "X-MS-CLIENT-PRINCIPAL-ID": "sid:unidentified-session",
        "X-MS-CLIENT-PRINCIPAL-NAME": "admin@example.onmicrosoft.com",
    }).status_code == 403
    page = client.get("/", base_url=origin, headers=headers)
    assert page.status_code == 200 and "Azure Container Apps enforces" in page.text
    assert "private Azure Blob Storage" in page.text
    headers["X-SWU-CSRF"] = re.search(r'name="swu-csrf" content="([^"]+)"', page.text)[1]
    assert client.get("/api/settings", base_url=origin, headers=headers).json["revision"] == 0
    result = client.post("/api/datasets", base_url=origin, headers=headers,
                         json={"scenario": "SCN-02", "confirmed": True})
    assert result.status_code == 200 and result.json["executionLocation"] == "azure"
    assert container.upload_blob.call_count > 1
    container.get_blob_client.return_value.download_blob.side_effect = AzureError("unavailable")
    assert client.get("/api/settings", base_url=origin, headers=headers).status_code == 503
