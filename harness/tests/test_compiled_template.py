import json
import shutil
import subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]


def nested_templates(template):
    yield template
    for resource in template["resources"]:
        if resource["type"] == "Microsoft.Resources/deployments":
            yield from nested_templates(resource["properties"]["template"])


@pytest.fixture(scope="module")
def compiled_template():
    az = shutil.which("az")
    assert az, "Azure CLI/Bicep is required for compiled-template validation"
    return json.loads(subprocess.run(
        [az, "bicep", "build", "--file", str(ROOT / "infra" / "main.bicep"), "--stdout"],
        check=True, capture_output=True, text=True, timeout=120).stdout)


def test_non_loop_modules_do_not_leak_resource_group_copy_indices(compiled_template):
    template = compiled_template
    checked = set()
    for resource in template["resources"]:
        if resource["type"] != "Microsoft.Resources/deployments" or "copy" in resource:
            continue
        outside_template = {key: value for key, value in resource.items() if key != "properties"}
        outside_template["parameters"] = resource["properties"].get("parameters", {})
        assert "copyIndex(" not in json.dumps(outside_template), resource["name"]
        if resource["name"] == "network-uta":
            assert resource["properties"]["parameters"]["vmSize"]["value"] == "[parameters('utilityAVmSize')]"
        checked.add(resource["name"])
    assert {"network-uta", "gateway-dcr-role"} <= checked


def test_private_operator_is_optional_and_does_not_add_ot_access(compiled_template):
    modules = {r["name"]: r for r in compiled_template["resources"]
               if r["type"] == "Microsoft.Resources/deployments" and "copy" not in r}
    network = modules["network-uta"]["properties"]
    assert network["parameters"]["deployOperatorHost"]["value"] == "[parameters('deployOperatorHost')]"
    child = network["template"]
    nsg = next(r for r in child["resources"] if r["type"] == "Microsoft.Network/networkSecurityGroups"
               and "operator" in r["name"])
    assert nsg["condition"] == "[parameters('deployOperatorHost')]"
    assert "operatorAzureEgress" in nsg["properties"]["securityRules"]
    allowed = json.dumps(child["variables"])
    for service in ("AzureResourceManager", "AzureMonitor", "AzureActiveDirectory", "Storage"):
        assert service in allowed
    assert "'443'" in nsg["properties"]["securityRules"]
    assert "'502'" not in nsg["properties"]["securityRules"]
    assert "'1881'" not in nsg["properties"]["securityRules"]
    reader = next(r for r in child["resources"]
                  if r["type"] == "Microsoft.Authorization/roleAssignments")
    assert reader["condition"] == "[parameters('deployOperatorHost')]"
    assert "acdd72a7-3385-48ef-bd42-f606fba81ae7" in reader["properties"]["roleDefinitionId"]
    assert "copyIndex(" not in json.dumps(reader)
    shared = modules["operator-shared-role"]
    assert "deployOperatorHost" in shared["condition"]
    assert "sharedWorkspaceName" in shared["properties"]["parameters"]["workspaceName"]["value"]
    assert compiled_template["parameters"]["deployOperatorHost"]["defaultValue"] is False


def test_fast_rules_use_durable_history_not_unsupported_multiday_scheduler_windows(compiled_template):
    assert compiled_template["parameters"]["deployedAt"]["defaultValue"] == "[utcNow('yyyy-MM-ddTHH:mm:ssZ')]"
    content = next(t for t in nested_templates(compiled_template)
                   if any(r["type"] == "Microsoft.SecurityInsights/watchlists" for r in t["resources"]))
    rules = {r["properties"]["displayName"].split()[0]: r["properties"]
             for r in content["resources"] if r.get("kind") == "Scheduled" and "copy" not in r}
    remaining = {rule["id"]: rule for rule in content["variables"]["scheduledRules"]}
    for rule in rules.values():
        assert rule["queryFrequency"] == "PT5M"
        assert rule["queryPeriod"] in {"PT5M", "PT10M", "PT15M", "PT20M", "PT30M", "PT1H", "P1D"}
    assert remaining["DR-01"]["period"] == "PT1H"
    assert remaining["DR-08"]["period"] == "PT10M"
    assert "AccessHistory" in remaining["DR-01"]["query"]
    assert "SourceReceipts" in rules["DR-11"]["query"]
    assert "IncidentHistory" in rules["DR-11"]["query"]
    assert "IncidentNumber = tolong(IncidentNumber)" in rules["DR-11"]["query"]
    assert "cachedPlc" in rules["DR-11"]["query"]
    assert "PlcChangedAt" in rules["DR-11"]["query"]
    assert "cachedHeartbeat" in rules["DR-11"]["query"]
    assert "withsource" not in rules["DR-11"]["query"]
    assert "source_health.kql" in (ROOT / "infra" / "modules" / "content.bicep").read_text()
    workbook = next(t for t in nested_templates(compiled_template)
                    if any(r["type"] == "Microsoft.Insights/workbooks" for r in t["resources"]))
    assert "withsource" not in workbook["variables"]["healthQuery"]
    assert "withsource" not in workbook["variables"]["volumeQuery"]
    assert "SwuProcess | extend HealthFeed" in json.dumps(content)
    assert content["variables"]["collectorFields"] == {
        "SwuSignins": "ResultType", "SwuAccess": "Action",
        "SwuEndpoint": "ActionType", "SwuFirewall": "Activity",
    }
    serialized_functions = json.dumps(content["variables"])
    assert "CollectorHeartbeat" in serialized_functions
    assert "analyticsFunctions" in serialized_functions and "collectionFunctions" in serialized_functions
    assert "Feed" in serialized_functions
    watchlists = next(r for r in content["resources"] if r["type"] == "Microsoft.SecurityInsights/watchlists")
    assert "hasItems" in watchlists["properties"]
    assert "rawContent" in watchlists["properties"]


def test_shared_storage_keeps_tenant_required_private_identity_only_access(compiled_template):
    shared = next(t for t in nested_templates(compiled_template)
                  if any(r["type"] == "Microsoft.Storage/storageAccounts" for r in t["resources"]))
    storage = next(r["properties"] for r in shared["resources"] if r["type"] == "Microsoft.Storage/storageAccounts")
    assert storage["publicNetworkAccess"] == "Disabled"
    assert storage["allowBlobPublicAccess"] is False
    assert storage["allowSharedKeyAccess"] is False
    assert storage["supportsHttpsTrafficOnly"] is True


def test_private_storage_network_is_separate_and_only_peers_with_the_owned_lab(compiled_template):
    private = next(t for t in nested_templates(compiled_template)
                   if any(r["type"] == "Microsoft.Network/privateEndpoints" for r in t["resources"]))
    assert private["variables"]["services"] == ["blob", "queue"]
    vnet = next(r for r in private["resources"] if r["type"] == "Microsoft.Network/virtualNetworks")
    subnets = vnet["properties"]["subnets"]
    assert "snet-functions" in subnets
    assert "'serviceName', 'Microsoft.App/environments'" in subnets
    assert "'defaultOutboundAccess', false" in subnets
    assert "'privateEndpointNetworkPolicies', 'Enabled'" in subnets
    assert private["parameters"]["preserveConsoleSubnet"]["defaultValue"] is False
    nsg = next(r for r in private["resources"] if r["type"] == "Microsoft.Network/networkSecurityGroups")
    rules = nsg["properties"]["securityRules"]
    assert "'destinationPortRange', '443'" in rules
    assert "'sourceAddressPrefixes', createArray('10.20.1.0/26', '10.10.5.0/24')" in rules
    assert private["parameters"]["additionalStorageClientPrefixes"]["defaultValue"] == []
    assert private["parameters"]["additionalLabVnetIds"]["defaultValue"] == []
    peer = next(r for r in private["resources"]
                if r["type"] == "Microsoft.Network/virtualNetworks/virtualNetworkPeerings")
    assert "labVnetId" in peer["condition"]
    assert peer["properties"]["allowForwardedTraffic"] is False
    assert peer["properties"]["allowGatewayTransit"] is False
    shared = next(t for t in nested_templates(compiled_template)
                  if any(r["type"] == "Microsoft.Web/sites" for r in t["resources"]))
    site = next(r for r in shared["resources"] if r["type"] == "Microsoft.Web/sites")
    assert "functionsSubnetId" in site["properties"]["virtualNetworkSubnetId"]
