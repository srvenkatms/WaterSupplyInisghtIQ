"""Provision isolated Route A/B foundations without touching the original lab."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "route-ab-deployment.json"
BOOTSTRAP_SCRIPT = """set -eu
cloud-init status --wait
systemctl is-active --quiet docker
docker info >/dev/null
echo __ROUTE_AB_BOOT_OK__
"""
WATCHLIST_QUERY = """
union
(_GetWatchlist('Roster') | project Source='Roster', Value=tostring(UserPrincipalName)),
(_GetWatchlist('Vendors') | project Source='Vendors', Value=tostring(UserPrincipalName)),
(_GetWatchlist('SafeLimits') | project Source='SafeLimits', Value=tostring(Tag)),
(_GetWatchlist('RemoteTools') | project Source='RemoteTools', Value=tostring(FileName))
| where isnotempty(Value)
| summarize Items=count() by Source
"""


def watchlists_ready(result: dict) -> bool:
    if "error" in result or len(result.get("tables", [])) != 1:
        raise RuntimeError(f"Sentinel watchlist query returned an incomplete result: {result.get('error')}")
    table = result["tables"][0]
    columns = [column["name"] for column in table["columns"]]
    source, items = columns.index("Source"), columns.index("Items")
    counts = {row[source]: row[items] for row in table["rows"]}
    return all(counts.get(name, 0) > 0 for name in ("Roster", "Vendors", "SafeLimits", "RemoteTools"))


def bootstrap_guest(azure, group: str, machine: dict, script_file: Path) -> None:
    # File transport prevents az.cmd from truncating multiline scripts on Windows.
    result = azure("vm", "run-command", "invoke", "--resource-group", group,
                   "--name", machine["name"], "--command-id", "RunShellScript",
                   "--scripts", f"@{script_file}", timeout=900)
    messages = "\n".join(value.get("message", "") for value in result.get("value", []))
    if "__ROUTE_AB_BOOT_OK__" not in messages:
        raise RuntimeError(f"Guest bootstrap failed for {group}/{machine['name']}: {messages}")
    print(f"Guest ready: {group}/{machine['name']}", flush=True)


def wait_for_watchlists(azure, state: dict, timeout: int = 900) -> None:
    deadline = time.monotonic() + timeout
    pending = {route["id"]: route for route in state["routes"]}
    while pending:
        token = azure("account", "get-access-token", "--resource", "https://api.loganalytics.io")["accessToken"]
        for route_id, route in list(pending.items()):
            request = Request(
                f"https://api.loganalytics.azure.com/v1/workspaces/{route['workspaceId']}/query",
                data=json.dumps({"query": WATCHLIST_QUERY, "timespan": "P1D"}).encode(),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST")
            try:
                with urlopen(request, timeout=60) as response:
                    ready = watchlists_ready(json.load(response))
            except HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")
                if error.code == 400 and "SEM0100" in detail and "scalar expression" in detail:
                    print(f"{route_id}: reference-data columns are still converging.", flush=True)
                    ready = False
                elif error.code == 429:
                    print(f"{route_id}: Sentinel query throttled; waiting before retry.", flush=True)
                    ready = False
                else:
                    raise RuntimeError(f"{route_id}: watchlist readiness query failed ({error.code}): {detail}") from error
            if ready:
                del pending[route_id]
                print(f"Watchlist schema ready: {route_id}", flush=True)
        if pending:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Sentinel watchlist schema did not converge: {', '.join(pending)}")
            time.sleep(min(30, max(0, deadline - time.monotonic())))


def check_context(account: dict, user: dict, subscription: str, tenant: str, owner: str) -> None:
    if (account.get("id") != subscription or account.get("tenantId") != tenant
            or account.get("state") != "Enabled"
            or account.get("user", {}).get("name", "").casefold() != owner.casefold()
            or user.get("userPrincipalName", "").casefold() != owner.casefold()):
        raise RuntimeError("Azure account does not match the approved Route A/B identity")


def check_groups(groups: list[dict], prefix: str) -> None:
    expected = {f"rg-{prefix}-{suffix}" for suffix in ("shared", "routea", "routeb")}
    for group in groups:
        if group["name"] in expected:
            tags = group.get("tags") or {}
            if any(tags.get(key) != value for key, value in {
                "project": "swu-lab", "deployment": prefix, "environment": "route-ab",
            }.items()):
                raise RuntimeError(f"Refusing to overwrite an unowned group: {group['name']}")


def check_detection_rules(rules: list[dict], expected: set[str]) -> None:
    owned = [rule for rule in rules if rule["properties"]["displayName"].split()[0] in expected]
    if (len(owned) != len(expected)
            or {rule["properties"]["displayName"].split()[0] for rule in owned} != expected
            or any(rule["properties"].get("enabled") is not True for rule in owned)
            or any(rule["kind"] != ("NRT" if rule["properties"]["displayName"].startswith("DR-07 ") else "Scheduled")
                   for rule in owned)):
        raise RuntimeError("The expected Sentinel detection rules are not all enabled with their required kinds")


def check_binding(state: dict, binding: dict, groups: list[dict]) -> None:
    differences = {key for key, value in binding.items() if state.get(key) != value}
    if not state or not differences:
        return
    expected = {f"rg-{binding['prefix']}-{suffix}" for suffix in ("shared", "routea", "routeb")}
    if (differences == {"vmSize"} and state.get("phase") == "validating"
            and not state.get("resourcesMayExist")
            and not any(group["name"] in expected for group in groups)):
        return
    raise RuntimeError("Existing Route A/B state belongs to another deployment")


def check_network(control: dict, test: dict, route: dict) -> None:
    direct = route["directPlcAccess"]
    prefix = "10.30" if route["id"] == "routea" else "10.40"
    inbound = next(r["properties"] for r in control["properties"]["securityRules"]
                   if r["name"] == "allow-modbus-from-boundary-and-test")
    expected = [f"{prefix}.2.0/24", f"{prefix}.4.0/24"] if direct else [f"{prefix}.2.0/24"]
    if (inbound.get("sourceAddressPrefixes") != expected
            or inbound.get("destinationPortRange") != "502"
            or inbound.get("access") != "Allow"):
        raise RuntimeError(f"{route['id']}: unexpected Modbus inbound policy")
    outbound = next(r["properties"] for r in test["properties"]["securityRules"]
                    if r["name"] == "allow-modbus-and-hmi-to-control")
    if (outbound.get("destinationPortRanges") != (["502", "1881"] if direct else ["1881"])
            or outbound.get("access") != "Allow"):
        raise RuntimeError(f"{route['id']}: unexpected scenario-subnet outbound policy")
    for nsg in (control, test):
        rules = nsg["properties"]["securityRules"]
        if any(r["name"] == "allow-build-egress" for r in rules):
            raise RuntimeError(f"{route['id']}: temporary build egress remains enabled")
        for name, direction in (("deny-vnet-in", "Inbound"), ("deny-other-out", "Outbound")):
            rule = next((r["properties"] for r in rules if r["name"] == name), {})
            if (rule.get("access") != "Deny" or rule.get("direction") != direction
                    or rule.get("destinationPortRange") != "*"):
                raise RuntimeError(f"{route['id']}: missing default-deny network policy")


def persist(state: dict) -> None:
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--azure-config-dir", type=Path, required=True)
    parser.add_argument("--ssh-public-key", type=Path, required=True)
    parser.add_argument("--prefix", default="swurab")
    parser.add_argument("--location", default="eastus2")
    parser.add_argument("--vm-size", default="Standard_E2as_v6")
    phases = parser.add_mutually_exclusive_group()
    phases.add_argument("--verify-only", action="store_true")
    phases.add_argument("--resume-readiness", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9]{2,5}", args.prefix):
        parser.error("Prefix must contain 3-6 lowercase alphanumeric characters")
    if not args.azure_config_dir.is_dir():
        parser.error("Use an existing, separately authenticated Azure CLI directory")
    executable = shutil.which("az")
    if not executable:
        parser.error("Azure CLI is required")
    env = os.environ | {"AZURE_CONFIG_DIR": str(args.azure_config_dir.resolve())}

    def azure(*command: str, timeout: int = 180):
        if command[0] not in {"account", "ad", "bicep"} and "--subscription" not in command:
            command = (*command, "--subscription", args.subscription)
        result = subprocess.run(
            [executable, *command, "--only-show-errors", "--output", "json"],
            env=env, text=True, capture_output=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f"Azure {' '.join(command[:3])} failed: {result.stderr.strip()}")
        return json.loads(result.stdout) if result.stdout.strip() else {}

    account = azure("account", "show")
    user = azure("ad", "signed-in-user", "show")
    check_context(account, user, args.subscription, args.tenant, args.owner)
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    binding = {"subscriptionId": args.subscription, "tenantId": args.tenant,
               "prefix": args.prefix, "location": args.location, "vmSize": args.vm_size}
    groups = azure("group", "list", "--subscription", args.subscription)
    check_binding(state, binding, groups)
    check_groups(groups, args.prefix)
    if args.resume_readiness and (
        not state.get("routes") or state.get("phase") != "readiness-failed"
        or state.get("buildOutboundEnabled") is not False
    ):
        raise RuntimeError("Readiness recovery requires a provisioned, locked-down failure manifest")
    if not args.verify_only:
        feature = azure("feature", "show", "--namespace", "Microsoft.Network",
                        "--name", "AllowBringYourOwnPublicIpAddress")
        if feature.get("properties", {}).get("state") != "Registered":
            raise RuntimeError("Register Microsoft.Network/AllowBringYourOwnPublicIpAddress and "
                               "re-register Microsoft.Network before provisioning outbound NAT IPs")
        key = args.ssh_public_key.read_text(encoding="utf-8").strip()
        if not key.startswith(("ssh-ed25519 ", "ssh-rsa ")):
            raise RuntimeError("Supply the SSH public key, never the private key")
        stamp = state.get("deployedAt") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        name = f"{args.prefix}-foundation"
        parameters = {key: {"value": value} for key, value in {
            "location": args.location, "prefix": args.prefix, "owner": args.owner,
            "adminSshPublicKey": key, "vmSize": args.vm_size,
            "testerPrincipalId": user["id"], "deployedAt": stamp,
            "allowBuildOutbound": not args.resume_readiness,
            "deployAnalyticsRules": False,
            "enableRouteRuntime": state.get("runtimeInfrastructureEnabled", False),
            "preserveConsoleSubnet": (ROOT / "route-ab-console-deployment.json").exists(),
        }.items()}
        state = state | binding | {"deployedAt": stamp, "deploymentName": name,
                                   "phase": "validating", "runtimeDeployed": False}
        persist(state)
        with tempfile.TemporaryDirectory(prefix="swu-route-ab-") as directory:
            parameter_file = Path(directory) / "parameters.json"
            parameter_file.write_text(json.dumps({
                "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
                "contentVersion": "1.0.0.0", "parameters": parameters,
            }), encoding="utf-8")
            deployment = ("--subscription", args.subscription, "--location", args.location,
                          "--template-file", str(ROOT / "infra" / "route-ab.bicep"),
                          "--parameters", f"@{parameter_file}")
            if not args.resume_readiness:
                print("Validating isolated Route A/B deployment...", flush=True)
                azure("deployment", "sub", "validate", "--name", name, *deployment, timeout=600)
                changes = azure("deployment", "sub", "what-if", "--name", name, *deployment,
                                "--no-pretty-print", timeout=600)
                if changes.get("status") != "Succeeded":
                    raise RuntimeError(f"Route A/B what-if did not succeed: {changes.get('error')}")
                if any(change.get("changeType") == "Delete" for change in changes.get("changes", [])):
                    raise RuntimeError("Refusing an unexpected resource deletion in Route A/B what-if")
                state["phase"] = "provisioning"
                state["resourcesMayExist"] = True
                persist(state)
                print("Creating Route A/B private infrastructure...", flush=True)
                result = azure("deployment", "sub", "create", "--name", name, *deployment, timeout=3600)
                if result["properties"]["provisioningState"] != "Succeeded":
                    raise RuntimeError("Route A/B deployment did not succeed")
                state.update({key: value["value"] for key, value in result["properties"]["outputs"].items()})
            else:
                print("Resuming readiness checks without reopening build egress...", flush=True)
            state["phase"] = "bootstrapping"
            persist(state)
            script_file = Path(directory) / "guest-bootstrap.sh"
            script_file.write_text(BOOTSTRAP_SCRIPT, encoding="utf-8", newline="\n")

            booted = False
            try:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    futures = [pool.submit(bootstrap_guest, azure, route["resourceGroup"], machine, script_file)
                               for route in state["routes"] for machine in route["machines"]]
                    for future in futures:
                        future.result()
                state["guestBootstrapVerified"] = True
                state["phase"] = "watchlist-readiness"
                persist(state)
                print("Waiting for queryable Sentinel watchlist schemas...", flush=True)
                wait_for_watchlists(azure, state)
                state["watchlistSchemasVerified"] = True
                booted = True
            finally:
                print("Removing temporary build egress...", flush=True)
                parameters["allowBuildOutbound"]["value"] = False
                parameters["deployAnalyticsRules"]["value"] = booted
                parameter_file.write_text(json.dumps({
                    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
                    "contentVersion": "1.0.0.0", "parameters": parameters,
                }), encoding="utf-8")
                result = azure("deployment", "sub", "create", "--name", f"{name}-lockdown",
                               *deployment, timeout=3600)
                if result["properties"]["provisioningState"] != "Succeeded":
                    raise RuntimeError("Route A/B build-egress lockdown did not succeed")
                state.update({key: value["value"] for key, value in result["properties"]["outputs"].items()})
                state["phase"] = "provisioned" if booted else "readiness-failed"
                persist(state)
    elif not state.get("routes") or state.get("phase") not in {"provisioned", "verified"}:
        raise RuntimeError("Route A/B provisioning and guest bootstrap must complete before verification")

    for route in state["routes"]:
        group = route["resourceGroup"]
        nics = azure("network", "nic", "list", "--subscription", args.subscription, "-g", group)
        expected = {machine["ip"] for machine in route["machines"]}
        actual = set()
        for nic in nics:
            for config in nic["ipConfigurations"]:
                if config.get("publicIPAddress"):
                    raise RuntimeError(f"{group}: a lab VM has a public IP")
                actual.add(config["privateIPAddress"])
        if actual != expected:
            raise RuntimeError(f"{group}: VM private IP inventory does not match the manifest")
        nsg_base = f"/subscriptions/{args.subscription}/resourceGroups/{group}/providers/Microsoft.Network/networkSecurityGroups"
        control = azure("resource", "show", "--ids", f"{nsg_base}/nsg-{route['prefix']}-uta-control",
                        "--api-version", "2024-05-01")
        test = azure("resource", "show", "--ids", f"{nsg_base}/nsg-{route['prefix']}-uta-test",
                     "--api-version", "2024-05-01")
        check_network(control, test, route)
        for subnet in ("control", "test"):
            subnet_state = azure("network", "vnet", "subnet", "show", "-g", group,
                                 "--vnet-name", f"vnet-{route['prefix']}-uta", "-n", f"snet-{subnet}")
            if subnet_state.get("natGateway") or not subnet_state.get("routeTable"):
                raise RuntimeError(f"{group}/{subnet}: isolated subnet still has Internet egress")
        routes = azure("network", "route-table", "show", "-g", group,
                       "-n", f"rt-{route['prefix']}-uta-isolated")
        if not any(item.get("addressPrefix") == "0.0.0.0/0" and item.get("nextHopType") == "None"
                   for item in routes.get("routes", [])):
            raise RuntimeError(f"{group}: the isolated subnet route table lacks its Internet null route")
        for role in ("boundary", "operator"):
            nsg = azure("network", "nsg", "show", "-g", group, "-n", f"nsg-{route['prefix']}-uta-{role}")
            if any(rule["name"] == "allow-build-egress" for rule in nsg["securityRules"]):
                raise RuntimeError(f"{group}/{role}: temporary build egress remains enabled")
        peers = azure("network", "vnet", "peering", "list", "-g", group,
                      "--vnet-name", f"vnet-{route['prefix']}-uta")
        if (len(peers) != 1 or peers[0].get("peeringState") != "Connected"
                or peers[0].get("remoteVirtualNetwork", {}).get("id", "").casefold()
                != state["servicesVnetId"].casefold()
                or peers[0].get("allowForwardedTraffic") or peers[0].get("allowGatewayTransit")):
            raise RuntimeError(f"{group}: private services peering is missing or permits transit")
        sentinel = azure("resource", "show", "--ids",
                         f"{route['workspaceResourceId']}/providers/Microsoft.SecurityInsights/onboardingStates/default",
                         "--api-version", "2024-03-01")
        if sentinel.get("name") != "default":
            raise RuntimeError(f"{group}: Sentinel onboarding not verified")
        rule_url = (f"https://management.azure.com{route['workspaceResourceId']}"
                    "/providers/Microsoft.SecurityInsights/alertRules?api-version=2024-01-01-preview")
        rules = []
        for _ in range(100):
            page = azure("rest", "--method", "get", "--url", rule_url)
            rules.extend(page["value"])
            rule_url = page.get("nextLink")
            if not rule_url:
                break
        else:
            raise RuntimeError(f"{group}: Sentinel rule pagination did not terminate")
        expected_rules = set(json.loads((ROOT / "rules" / "contract.json").read_text(encoding="utf-8")))
        check_detection_rules(rules, expected_rules)
    storage = azure("storage", "account", "show", "-g", state["sharedResourceGroup"],
                    "-n", state["storageAccountName"])
    if (storage.get("publicNetworkAccess") != "Disabled" or storage.get("allowSharedKeyAccess") is not False
            or storage.get("allowBlobPublicAccess") is not False):
        raise RuntimeError("Route A/B storage is not private and identity-only")
    endpoints = azure("network", "private-endpoint", "list", "-g", state["sharedResourceGroup"])
    if len(endpoints) != 2 or any(
        endpoint.get("provisioningState") != "Succeeded"
        or not endpoint.get("privateLinkServiceConnections")
        or any(connection.get("privateLinkServiceConnectionState", {}).get("status") != "Approved"
               for connection in endpoint["privateLinkServiceConnections"])
        for endpoint in endpoints
    ):
        raise RuntimeError("Route A/B Blob/Queue private endpoints are not approved and ready")
    expected_links = {state["servicesVnetId"].casefold(),
                      *(route["vnetId"].casefold() for route in state["routes"])}
    for service in ("blob", "queue"):
        links = azure("network", "private-dns", "link", "vnet", "list",
                      "-g", state["sharedResourceGroup"], "--zone-name", f"privatelink.{service}.core.windows.net")
        if ({link["virtualNetwork"]["id"].casefold() for link in links} != expected_links
                or any(link.get("virtualNetworkLinkState") != "Completed" for link in links)):
            raise RuntimeError(f"Route A/B private {service} DNS links are not ready")
    state["phase"] = "verified"
    state["verifiedAt"] = datetime.now(timezone.utc).isoformat()
    persist(state)
    print("Route A/B foundations verified; application runtime and console integration remain pending.", flush=True)


if __name__ == "__main__":
    main()
