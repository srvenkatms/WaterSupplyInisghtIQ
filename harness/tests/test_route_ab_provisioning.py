import copy
import io
import json
from pathlib import Path
import runpy
import shutil
import subprocess
from urllib.error import HTTPError

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = runpy.run_path(str(ROOT / "scripts" / "provision-route-ab.py"))


def test_guest_bootstrap_uses_file_transport_and_requires_success_marker(tmp_path):
    script = tmp_path / "bootstrap.sh"
    script.write_text(SCRIPT["BOOTSTRAP_SCRIPT"])
    calls = []

    def azure(*args, **kwargs):
        calls.append(args)
        assert args[args.index("--scripts") + 1] == f"@{script}"
        assert "__ROUTE_AB_BOOT_OK__" in script.read_text()
        return {"value": [{"message": "__ROUTE_AB_BOOT_OK__"}]}

    SCRIPT["bootstrap_guest"](azure, "owned-group", {"name": "vm"}, script)
    assert len(calls) == 1
    with pytest.raises(RuntimeError, match="Guest bootstrap failed"):
        SCRIPT["bootstrap_guest"](lambda *args, **kwargs: {"value": [{"message": "Enable succeeded"}]},
                                  "owned-group", {"name": "vm"}, script)


def test_detection_verification_requires_nrt_but_preserves_unrelated_native_rules():
    check = SCRIPT["check_detection_rules"]
    rules = [
        {"kind": "Scheduled", "properties": {"displayName": "DR-01 Example", "enabled": True}},
        {"kind": "NRT", "properties": {"displayName": "DR-07 Example", "enabled": True}},
        {"kind": "Fusion", "properties": {"displayName": "Advanced Multistage Attack Detection", "enabled": True}},
    ]
    check(rules, {"DR-01", "DR-07"})
    with pytest.raises(RuntimeError, match="required kinds"):
        check(rules[:1] + rules[2:], {"DR-01", "DR-07"})
    changed = copy.deepcopy(rules)
    changed[1]["kind"] = "Scheduled"
    with pytest.raises(RuntimeError, match="required kinds"):
        check(changed, {"DR-01", "DR-07"})
    changed = copy.deepcopy(rules)
    changed[0]["properties"]["enabled"] = False
    with pytest.raises(RuntimeError, match="required kinds"):
        check(changed, {"DR-01", "DR-07"})


def watchlist_result(names=("Roster", "Vendors", "SafeLimits", "RemoteTools")):
    return {"tables": [{"columns": [{"name": "Source"}, {"name": "Items"}],
                        "rows": [[name, 2] for name in names]}]}


def test_watchlist_readiness_requires_real_fields_and_nonempty_reference_lists():
    check = SCRIPT["watchlists_ready"]
    assert check(watchlist_result()) is True
    assert check(watchlist_result(("Roster", "SafeLimits"))) is False
    result = watchlist_result()
    result["tables"][0]["rows"][0][1] = 0
    assert check(result) is False
    with pytest.raises(RuntimeError, match="incomplete"):
        check({"tables": [], "error": {"code": "PartialError"}})


def test_watchlist_wait_uses_the_real_query_result_without_logging_tokens(monkeypatch, capsys):
    wait = SCRIPT["wait_for_watchlists"]
    calls = []

    def query(request, timeout):
        calls.append(json.loads(request.data))
        return io.BytesIO(json.dumps(watchlist_result()).encode())

    monkeypatch.setitem(wait.__globals__, "urlopen", query)
    wait(lambda *args: {"accessToken": "synthetic-token"}, {
        "routes": [{"id": "routea", "workspaceId": "workspace"}],
    }, timeout=1)
    assert "_GetWatchlist('SafeLimits')" in calls[0]["query"]
    assert "synthetic-token" not in capsys.readouterr().out


def test_watchlist_wait_does_not_hide_permission_errors(monkeypatch):
    wait = SCRIPT["wait_for_watchlists"]

    def forbidden(request, timeout):
        raise HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b"Access denied"))

    monkeypatch.setitem(wait.__globals__, "urlopen", forbidden)
    with pytest.raises(RuntimeError, match="403"):
        wait(lambda *args: {"accessToken": "synthetic-token"}, {
            "routes": [{"id": "routea", "workspaceId": "workspace"}],
        }, timeout=1)


def test_context_requires_the_exact_approved_identity_and_tenant():
    account = {"id": "sub", "tenantId": "tenant", "state": "Enabled",
               "user": {"name": "admin@example.invalid"}}
    user = {"userPrincipalName": "admin@example.invalid"}
    SCRIPT["check_context"](account, user, "sub", "tenant", "admin@example.invalid")
    for changed in (
        account | {"id": "old-sub"},
        account | {"tenantId": "old-tenant"},
        account | {"state": "Disabled"},
        account | {"user": {"name": "other@example.invalid"}},
    ):
        with pytest.raises(RuntimeError, match="approved"):
            SCRIPT["check_context"](changed, user, "sub", "tenant", "admin@example.invalid")
    with pytest.raises(RuntimeError, match="approved"):
        SCRIPT["check_context"](account, {"userPrincipalName": "other@example.invalid"},
                                "sub", "tenant", "admin@example.invalid")


def test_existing_resource_groups_must_be_owned_by_the_route_ab_deployment():
    check = SCRIPT["check_groups"]
    check([{"name": "unrelated-group", "tags": {}}], "swurab")
    check([{"name": "rg-swurab-routea", "tags": {
        "project": "swu-lab", "deployment": "swurab", "environment": "route-ab",
    }}], "swurab")
    for tags in ({}, {"project": "swu-lab", "deployment": "swulab", "environment": "route-ab"}):
        with pytest.raises(RuntimeError, match="unowned"):
            check([{"name": "rg-swurab-routea", "tags": tags}], "swurab")


def test_vm_size_can_change_only_after_failed_validation_without_existing_resources():
    check = SCRIPT["check_binding"]
    old = {"subscriptionId": "sub", "tenantId": "tenant", "prefix": "swurab",
           "location": "eastus2", "vmSize": "Standard_B2s", "phase": "validating"}
    binding = {key: old[key] for key in ("subscriptionId", "tenantId", "prefix", "location", "vmSize")}
    binding["vmSize"] = "Standard_E2as_v6"
    check(old, binding, [])
    for state, groups in (
        (old | {"resourcesMayExist": True}, []),
        (old | {"phase": "provisioned"}, []),
        (old, [{"name": "rg-swurab-routea"}]),
    ):
        with pytest.raises(RuntimeError, match="another deployment"):
            check(state, binding, groups)
    with pytest.raises(RuntimeError, match="another deployment"):
        check(old, binding | {"tenantId": "other-tenant"}, [])


def policies(direct):
    prefix = "10.30" if direct else "10.40"
    deny = [
        {"name": "deny-vnet-in", "properties": {
            "access": "Deny", "direction": "Inbound", "destinationPortRange": "*",
        }},
        {"name": "deny-other-out", "properties": {
            "access": "Deny", "direction": "Outbound", "destinationPortRange": "*",
        }},
    ]
    sources = [f"{prefix}.2.0/24", f"{prefix}.4.0/24"] if direct else [f"{prefix}.2.0/24"]
    control = {"properties": {"securityRules": deny + [{
        "name": "allow-modbus-from-boundary-and-test", "properties": {
            "sourceAddressPrefixes": sources, "destinationPortRange": "502", "access": "Allow",
        },
    }]}}
    test = {"properties": {"securityRules": deny + [{
        "name": "allow-modbus-and-hmi-to-control", "properties": {
            "destinationPortRanges": ["502", "1881"] if direct else ["1881"], "access": "Allow",
        },
    }]}}
    return control, test, {"id": "routea" if direct else "routeb", "directPlcAccess": direct}


@pytest.mark.parametrize("direct", [True, False])
def test_live_policy_verifier_accepts_only_the_expected_route(direct):
    SCRIPT["check_network"](*policies(direct))


@pytest.mark.parametrize("mutation", ["source", "port", "build", "deny"])
def test_live_policy_verifier_rejects_route_b_bypasses_and_unfinished_lockdown(mutation):
    control, test, route = copy.deepcopy(policies(False))
    if mutation == "source":
        control["properties"]["securityRules"][-1]["properties"]["sourceAddressPrefixes"].append("10.40.4.0/24")
    elif mutation == "port":
        test["properties"]["securityRules"][-1]["properties"]["destinationPortRanges"].append("502")
    elif mutation == "build":
        test["properties"]["securityRules"].append({"name": "allow-build-egress", "properties": {}})
    else:
        test["properties"]["securityRules"][0]["properties"]["access"] = "Allow"
    with pytest.raises(RuntimeError):
        SCRIPT["check_network"](control, test, route)


@pytest.fixture(scope="module")
def template():
    az = shutil.which("az")
    assert az, "Azure CLI/Bicep is required for template validation"
    result = subprocess.run(
        [az, "bicep", "build", "--file", str(ROOT / "infra" / "route-ab.bicep"), "--stdout"],
        text=True, capture_output=True, check=True, timeout=120)
    return json.loads(result.stdout)


def test_foundation_has_distinct_routes_and_preserves_original_network_defaults(template):
    routes = template["variables"]["routes"]
    assert [(r["id"], r["network"], r["directPlcAccess"]) for r in routes] == [
        ("routea", "10.30", True), ("routeb", "10.40", False),
    ]
    network = next(r for r in template["resources"] if r.get("copy", {}).get("name") == "networks")
    parameters = network["properties"]["parameters"]
    assert parameters["deployBastion"]["value"] is False
    assert parameters["deployOperatorHost"]["value"] is True
    assert "directPlcAccess" in parameters["directPlcAccess"]["value"]
    child = network["properties"]["template"]
    assert child["parameters"]["addressPrefix"]["defaultValue"] == "10.10"
    assert child["parameters"]["directPlcAccess"]["defaultValue"] is True
    assert child["parameters"]["deployBastion"]["defaultValue"] is True
    assert not any(r["type"] == "Microsoft.Network/publicIPAddresses" for r in child["resources"]
                   if "egress" not in r["name"])
    nic = next(r for r in child["resources"] if r["type"] == "Microsoft.Network/networkInterfaces")
    assert "publicIPAddress" not in json.dumps(nic)
    assert not any(r["type"] == "Microsoft.Consumption/budgets" for r in template["resources"])


def test_route_ab_deployment_does_not_overwrite_original_manifests_or_create_a_console(template):
    assert SCRIPT["STATE"].name == "route-ab-deployment.json"
    services = next(r for r in template["resources"] if r["name"] == "route-ab-services")
    resources = services["properties"]["template"]["resources"]
    assert not any(r["type"] in {"Microsoft.Web/sites", "Microsoft.App/containerApps"} for r in resources)
    storage = next(r for r in resources if r["type"] == "Microsoft.Storage/storageAccounts")
    assert storage["properties"]["publicNetworkAccess"] == "Disabled"
    assert storage["properties"]["allowSharedKeyAccess"] is False
    assert storage["properties"]["allowBlobPublicAccess"] is False


def test_non_loop_modules_never_inherit_copy_indices_from_resource_group_collections(template):
    for resource in template["resources"]:
        if resource["type"] != "Microsoft.Resources/deployments" or "copy" in resource:
            continue
        outside = {key: value for key, value in resource.items() if key != "properties"}
        outside["parameters"] = resource["properties"].get("parameters", {})
        assert "copyIndex(" not in json.dumps(outside), resource["name"]


def test_analytics_can_wait_for_watchlist_ingestion_without_changing_original_defaults(template):
    telemetry = next(r for r in template["resources"] if r.get("copy", {}).get("name") == "telemetry")
    utility = telemetry["properties"]["template"]
    assert utility["parameters"]["deployAnalyticsRules"]["defaultValue"] is True
    content = next(r for r in utility["resources"] if r["name"] == "[format('content-{0}', parameters('utilityId'))]")
    child = content["properties"]["template"]
    assert child["parameters"]["deployAnalyticsRules"]["defaultValue"] is True
    for resource in child["resources"]:
        if resource["type"] == "Microsoft.SecurityInsights/alertRules":
            assert resource["condition"] == "[parameters('deployAnalyticsRules')]"
    assert "column_ifexists" not in SCRIPT["WATCHLIST_QUERY"]
