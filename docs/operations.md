# Operations runbook

## Azure console

The Azure deployment uses a dedicated **Azure Container Apps Consumption** environment and a private state container in the existing lab storage account. App Service Basic and P0v3 creation were rejected by subscription quota; a generic regional quota listing did not predict the actual create result. The console is not co-hosted with the telemetry generator or the private operator.

Deploy using the approved Azure account and the preserved lab manifest:

```powershell
.\.venv\Scripts\python.exe scripts\deploy-console.py
```

The one-time publisher needs Azure CLI, Bicep, Python and a Linux Docker engine. Runtime does not depend on the developer's machine. The clean image context includes only console code, generator/profile code, pinned requirements and a minimal readiness snapshot; it excludes credentials, SSH material, the full lab manifest and local settings. Deployment metadata is saved in ignored `console-deployment.json`. Separate `--phase provision`, `--phase publish`, `--phase auth` and `--phase verify` options support recovering infrastructure, the image, authentication or live verification independently. Authentication recovery rotates the client credential; retain the previously recorded credential key IDs until the replacement is verified, then remove only those superseded deployment-owned credentials.

Generator runtime assets include `source_health.kql`, required when validating profiles. The no-network Linux image smoke check loads all three profiles and generates a bounded scenario before publishing, not just importing Flask. The publisher waits for the current revision and secret configuration to become ready; only the specific transient `AuthConfigSecretRefNotFound` response receives a bounded retry.

Preserve `console-deployment.json` alongside the lab manifest. The main deployment script detects it and sets `preserveConsoleSubnet=true`, retaining the console subnet and its private-storage allow rule during a later lab redeployment. When invoking `infra/main.bicep` directly after deploying the console, pass that parameter explicitly. Do not redeploy the original network with the console omitted.

Ingress starts internal-only. The publisher installs and reads back the Entra policy before enabling authenticated public HTTPS. The policy is single-tenant and explicitly allowlists the administrator object ID and the console/Azure CLI client IDs; the CLI's delegated scope permits real signed-in-administrator verification, not application-only access. Other enabled identity providers are rejected. The backend independently requires Container Apps' platform-injected `X-MS-CLIENT-PRINCIPAL-ID` to match the administrator on every route. Tenant validation belongs to the pinned Entra issuer; Container Apps does not provide App Service's base64 claims envelope. Opaque sessions without an identifiable administrator are rejected, not treated as an alternate authorization path. Trusted-host, same-origin and CSRF checks remain enforced, and incomplete Azure configuration fails closed. Session signing/encryption keys are platform-managed, as supported by native Container Apps authentication; no custom key references are necessary. VPN/private-only remains disabled. No PLC/HMI endpoint is published, and the console subnet explicitly denies direct access to the OT network.

Settings and generated datasets are stored in the private `wateriq-console` blob container using the console's managed identity. Its storage role is scoped only to that container, not the generator's state, scenario queue or Sentinel workspaces. Settings use blob ETags plus revision checks so retries, restarts or overlapping replicas cannot overwrite a concurrent edit. Missing or inaccessible storage and corrupt configuration are explicit failures. Generated files must upload successfully before a run reports success.

The Entra client credential is transferred directly to Container Apps' encrypted secret configuration over TLS, never through command-line arguments, logs, manifests or the container image. Its expiry is recorded in deployment metadata; rotate it before expiry and validate sign-in before removing the previous credential. Application stdout/stderr is collected in the existing shared Log Analytics workspace. Budget alerts remain notifications, not a spending cap; review Container Apps, registry and log ingestion charges.

Live verification checks anonymous and forged-identity-header denial, the exact Entra redirect, actual delegated administrator access, and private settings write/readback. In on-demand mode it also generates one bounded synthetic dataset in Azure. Results are saved in `test-results/console-azure-verification.json`; none of these checks certifies PLC/Sentinel acceptance. The account holder completes interactive browser sign-in; a client-directed access-token-only session without administrator claims is not a substitute.

**Hosting is not live scenario integration.** On-demand actions still generate synthetic datasets, now entirely in Azure; they do not operate the PLC/HMI, inject Sentinel events or send Teams messages. Acceptance readiness shown by the console is a saved deployment record, not a fresh health query. Lab lifecycle controls and the private command worker remain separate work.

## Optional local developer preview

Start the loopback-only console using the command in the [README](../README.md#local-console-and-admin-tab), then open `http://127.0.0.1:8787/`. The Admin tab saves mode, default utility, confirmation and Teams configuration to `local-data/console/settings.json`. Writes are atomic and revision-checked; a stale browser tab cannot overwrite another saved edit. Invalid or corrupt settings produce an explicit error, not a reset to defaults. Discard affects only unsaved form edits.

On-demand mode permits bounded offline scenario datasets under `local-data/console/runs/`. Acceptance mode refuses these accelerated local datasets as evidence and displays the actual immutable warm-up eligibility from `deployment-outputs.json`; elapsed time alone is not certified readiness. These controls do not execute physical PLC/HMI actions or change Azure timers, resources, costs or the ongoing cloud warm-up.

Teams is off by default. Enabling its configuration requires a valid team GUID and channel ID. **Export saved Teams parameters** downloads the existing `deployTeamsPlaybook`, `teamsTeamId` and `teamsChannelId` Bicep parameters. This partial parameter file must be merged with the separately approved deployment inputs. Exporting or saving does not deploy, authorize a connector or send a notification. No tokens, SSH keys, passwords or webhooks are accepted by the form.

The local preview is a single-user developer tool, not the Azure deployment. It binds only to `127.0.0.1`, restricts Host/origin/client addresses, requires an anti-CSRF header for changes and does not enable cross-origin access. Do not publish it through a network proxy. Live graphical scenario execution and Azure start/stop lifecycle controls remain separate work.

Admin's **Access & Security** section distinguishes the Azure deployment's enforced Entra authentication from the local preview's planned policy. The VPN option is visibly disabled; both access-policy controls are read-only and cannot be saved or activated through the settings API. No gateway is provisioned. A future private-only rollout must add VPN connectivity, a private endpoint and private DNS, keep Entra authentication, verify access before removing public ingress and preserve management-plane recovery.

## Before provisioning

Obtain explicit approval for subscription, region, monthly budget, resource prefix and private operator access. Validate Premium private-only Bastion availability and price, NAT/VM/storage costs, and the deployment identity's permissions. Budget alerts do not stop charges. Either provide an existing private access path or enable the optional self-contained operator VM below. The latter does not peer or modify unrelated networks.

**Cost preflight, 2026-11-04:** the [Azure Retail Prices API](https://prices.azure.com/api/retail/prices) returned USD 0.45/hour for the East US 2 `Premium Gateway` Bastion meter, or approximately USD 328.50 for a 730-hour planning month, before additional scale units, NAT, VMs, disks and telemetry. That component alone exceeds the template's USD 300 example budget. The example is not spending approval or evidence that the original monthly cost target can be met. Obtain a budget/design decision before provisioning; do not silently substitute public Bastion or weaken the private-only boundary to reduce cost.

Provide Azure deployment permissions, required provider registration and role-assignment privileges through an approved identity. Sentinel onboarding requires both `Microsoft.SecurityInsights` and `Microsoft.OperationsManagement`; a successful `what-if` does not prove that the latter is registered. The deployment checks required provider states and registers missing providers before provisioning. CI uses protected `lab-plan`/`lab` environments and OIDC; configure required reviewers. For a service-principal tester, specify its **object ID**, not application/client ID.

Flex VNet integration also requires `Microsoft.App`, with subnet delegation to `Microsoft.App/environments`, not `Microsoft.Web/serverFarms`. Shared storage stays private, including when a management-group modify policy enforces that setting. Do not retry a publishing 403 by enabling shared keys, anonymous blobs or a policy exemption. Verify private blob/queue endpoint approval, both private DNS links, Function integration and operator endpoint routing. The lab creates its own shared services network and only peers it with its own Utility A network; it never peers or edits unrelated networks. Include the additional private endpoints, DNS and services NAT in the approved budget review. Operators supplied instead of the optional private VM need an explicitly approved private route, DNS and endpoint source allowlist.

Publishing uses the supported Flex `config-zip` remote-build path. Private networking and identity access can require convergence after provisioning: the publisher logs and retries only a deployment-storage `BlobUploadFailedException` with HTTP 403, at most six attempts separated by one minute. Other failures stop immediately; exhausted retries remain a deployment failure. Verify actual published functions, successful timers and ingested records afterward. A successful private deployment was observed without relaxing storage policy; initial 403 failures alone do not identify their precise networking or authorization cause.

Example Bash configuration; replace every placeholder with approved values:

```bash
export AZURE_SUBSCRIPTION_ID="approved-subscription-id"
export SWULAB_OWNER="approved-owner"
export SWULAB_PREFIX="swulab"
export SWULAB_LOCATION="eastus2"
export SWULAB_VM_SIZE="Standard_B2s" # Verify regional/subscription capacity; choose an available alternative if needed.
export SWULAB_BUDGET_AMOUNT="approved-positive-integer"
export SWULAB_BUDGET_EMAILS='["approved-recipient@example.invalid"]'
export SWULAB_SSH_PUBLIC_KEY="$(cat /approved/path/id_ed25519.pub)"
# Optional self-contained private operator; otherwise supply private connectivity:
export SWULAB_DEPLOY_OPERATOR=true
export SWULAB_SSH_PRIVATE_KEY_FILE="/approved/protected/path/id_ed25519"
# Required for non-user deployment identities:
export SWULAB_TESTER_ID="tester-object-id"
export SWULAB_TESTER_TYPE="ServicePrincipal"
export PYTHONPATH=generator:apps:harness
bash scripts/deploy.sh "$AZURE_SUBSCRIPTION_ID"
```

Use a working Linux Docker engine for the explicit local OpenPLC test before live provisioning. Do not fall back to the Python soft PLC. Pinning upstream source does not demonstrate that its compiler/bootstrap works; the actual runtime gate must pass.

VM family quota does not establish SKU capacity. Azure preflight may reject `Standard_B2s` even with unused quota; use `SWULAB_VM_SIZE` to select a verified available size without changing role/network boundaries. CI can set the same repository variable. The deployment manifest records the selected VM size.

A successful VM resource deployment is not proof of a running guest. Application publishing explicitly starts each lab VM before invoking cloud-init and container builds. Inspect actual disk resources when retrying a partially provisioned deployment: a reserved disk can report a different tier from the requested `StandardSSD_LRS`, and changing the tier through a VM update is rejected. Reconcile only the owned disk resource to the approved tier before retrying; do not delete the VM or silently substitute another tier. Confirm running state, guest-agent readiness and positive application health separately.

Azure Linux Run Command can execute the supplied script with `/bin/sh`, even when the local publisher uses Bash. Guest application, protected-operator and verification scripts explicitly invoke Bash before enabling `pipefail`. Managed Run Command exposes named Linux protected parameters as environment variables, not positional arguments: bootstrap requires `sshKey`, decodes it without logging and unsets it before starting Docker. Do not remove that wrapper or treat Run Command admission as successful execution.

The deployment entrypoint and generator publisher disable Git Bash's native-argument path conversion. The Function's `SWU_PROFILE_DIR` must remain `/home/site/wwwroot/profiles`, not a Windows installation path. Verify the stored Azure setting and actual timer execution after publishing, not only the publisher's exit status.

Always run Azure `what-if`, not just local Bicep compilation. A compiled cross-resource-group module can still contain an invalid ARM loop expression; the compiled-template regression checks that non-loop modules do not leak `copyIndex`. Dynamic managed-identity role assignments may remain unidentifiable in `what-if` until their principal IDs exist. Preserve those preview limitations and verify the actual role assignments after deployment.

The application Docker build uses a staged context containing `swugen`, `profiles`, `swuapps` and an initially empty `wheelhouse`. Normal deployment installs pinned requirements over HTTPS during bootstrap. If the local Docker network cannot complete PyPI TLS handshakes, securely download Python 3.11 Linux x64 wheels on the host into that staged `wheelhouse`, then build with `--build-arg PIP_NO_INDEX=true`. The verified offline build used `--network none`; it did not disable TLS verification or substitute installed packages. Do not include private SSH keys or Azure credentials in the Docker context.

The operator image uses the official Azure CLI 2.92.0 image, Bastion extension 1.4.3 and OpenSSH. Its Python 3.12 harness virtual environment is separate from the CLI's SDKs. Host-downloaded offline harness wheels must target Linux x64/Python 3.12, not the application's Python 3.11; `PIP_NO_INDEX=true` disables Python package downloads, but installing the CLI extension and SSH tools still needs authenticated HTTPS bootstrap access. After building, verify the image's CLI and direct harness entrypoints without network access.

The private key must match the VM public key and be restricted to its owner (on Windows, SYSTEM/administrators may also read it). Bootstrap sends it in-memory over TLS using Azure managed Run Command `protectedParameters`, never as a CLI argument, regular parameter, manifest value or image layer. The completed bootstrap resource is deleted. The operator stores the key in a root-only file and mounts it read-only. SSH host keys are collected during bootstrap and pinned; do not replace strict host checking with trust-on-first-use. CI defaults the operator off; enabling `SWULAB_DEPLOY_OPERATOR` requires the protected `lab` environment's matching `SWULAB_SSH_PRIVATE_KEY` secret, which is staged with restrictive permissions and removed after the deployment step.

For existing VMs, preserve the exact original public-key parameter, including trailing newlines. Azure rejects changes to immutable `linuxConfiguration.ssh.publicKeys` metadata even when the cryptographic key is identical. When reconciling formatting between shells, compare key fingerprints first and preserve the existing public metadata serialization; do not rotate credentials or recreate VMs to bypass the error.

## Profiles and changes

Edit `profiles/*.json`, not generated infrastructure watchlists:

```bash
PYTHONPATH=generator python -m swugen.configuration
PYTHONPATH=generator python -m swugen.configuration --check
```

Review synthetic `.example` identities, on-call/vendor schedules, safe limits, backup dates and UTC maintenance windows. A new utility requires a validated profile and regenerated infrastructure configuration; only `uta` is wired to the actual VM/OpenPLC path.

Plan maintenance windows narrowly. An overly broad window changes DR-05/DR-06 behavior and invalidates tests expecting out-of-window detection. Deploying updated reference data can overwrite the generator-maintained disabled-account watchlist; pause generation and restore/verify its state deliberately when updating that content.

Empty watchlists are created with metadata only: Azure rejects header-only CSV uploads. Do not invent placeholder maintenance, disabled-account or history records. The three history references (`SourceReceipts`, `AccessHistory`, `IncidentHistory`) are populated only from successfully queried, actually ingested data. An independent minute timer queries long history with the generator's managed identity, writes changed entries under per-utility leases, and checkpoints each successful write. Partial queries and failed writes are explicit `swu-history failed` errors covered by the generator failure alert; unchanged entries are refreshed after seven days.

Sentinel requires at least hourly scheduling when a scheduled rule's query period is two days or longer. DR-01, DR-08 and DR-11 therefore use short scheduler windows plus durable references, rather than weakening their original five-minute evaluation or long-history semantics. DR-01 retains fourteen-day prior-access history; DR-08 retains ninety-day disabled-account state; DR-11 retains actual source receipts and incident changes for eight-day quiet-source thresholds and escalation. Recent raw observations override older reference state. The history timer has workspace query rights and write rights scoped only to its three watchlists; it does not gain workspace-wide Sentinel administration. Investigate stale history before accepting multi-day detection evidence.

The process-source reference also retains the actual latest PLC failure/recovery event and heartbeat over nine days. This keeps an unresolved PLC outage visible after the rule's short raw-data window expires, without treating late outbox deliveries as a healthy PLC. Missing optional PLC observations remain empty, not synthetic timestamps; a newer real recovery or heartbeat supersedes older failure state.

Health queries label each normalized function's union branch explicitly using `swugen/source_health.kql`, shared by history, DR-11, the workbook and history verification. Azure can report saved-function union sources as `union_arg0` rather than the function alias; `withsource` is therefore reserved for raw tables. Unknown history source names are rejected instead of checkpointed as valid receipts.

Sparse synthetic sign-in, access, endpoint and firewall feeds emit an explicit `CollectorHeartbeat` every fifteen minutes in their existing schemas. These records contain no invented user/session activity and are excluded from the event-normalizing functions used by attack analytics and access history. Separate `Swu*Feed` functions include them for collector receipts, so a quiet utility does not imply a failed collection pipeline. Event volume targets exclude collector heartbeats; billed volume includes them. Feed suppression also drops their heartbeats. No generator heartbeat is added to Utility A's process feed: its PLC health still requires actual gateway heartbeat/recovery observations.

## After deployment

Preserve `deployment-outputs.json`, including the shared workspace ID and approved budget amount. Confirm all declared resource groups, utility/workspace mappings, ten schemas, role scopes, network rules, current ingestion and application health. Verify no temporary build rule or control/test NAT attachment remains, and the isolated route table has a `0.0.0.0/0` next hop of `None`. Confirm the raw PLC/admin endpoints are not reachable from other VMs.

Set private SSH aliases in the operator's SSH config; each alias must select the appropriate target through the approved private/Bastion route:

```bash
export SWU_ATTACK_SSH="swu-private-test"
export SWU_GATEWAY_SSH="swu-private-gateway"
python harness/swuharness.py schema-check
python harness/swuharness.py content-check
python harness/swuharness.py history-check
python harness/swuharness.py health
python harness/swuharness.py network-check
python harness/swuharness.py protocol-check
```

The aliases must allow noninteractive `sudo -n` execution of the deployed `swu-attack` and `swu-gateway` containers. Without the optional operator, supply existing private connectivity. With it, the `swu-operator` container establishes both tunnels and uses the VM's managed identity. Its NSG permits private Bastion HTTPS and Azure management/monitor/identity/storage HTTPS, not direct operator-origin Modbus/HMI or SSH to OT VMs. Lockdown removes its broad bootstrap rule while retaining its separate NAT attachment. Control/test retain their null Internet routes and no NAT.

`network-check` performs read-only Modbus/HMI reads from the test VM, then opens fresh TCP probes that must fail for public Internet and raw PLC/admin endpoints. A temporary third Bastion tunnel reaches the control VM with its pre-collected pinned host key and verifies its public Internet denial separately. That tunnel is always stopped; it is not an additional persistent path or an attack injection. Post-lockdown verification requires these positive and negative observations before starting warm-up.

The operator image includes the canonical `infra/tables.json` schema used by its live schema/content checks. Acceptance results under `/lab/test-results` are mounted from the host's protected operator state directory so recreating the container does not erase the evidence.

`protocol-check` uses the real private gateway route. It reads the PLC, attempts FC6/FC16 writes with the already-held values, and requires explicit Modbus IllegalFunction responses rather than accepting a timeout as a denial. It also checks actuator-write and FC1/FC4 rejection, confirms setpoints remain unchanged and persists the actual result. It does not inject an attack setpoint, restart a service or replace the warm-up/scenario gates.

For bounded continuous observation of the existing warm-up, run `python harness/observe_warmup.py`. It records actual health/history results every five minutes in ignored `test-results/warmup-observations.jsonl`, retains failed observations, and stops on query/authentication errors. At the real seven-day deadline it requires fresh passing health/history and the existing hourly-ingestion readiness gate. It never advances the start date, modifies the lab or injects scenarios. This is a local foreground test process, not a saved automation: closing the process/app, sleeping the computer or losing Azure authentication can interrupt it. Restarting it uses the original manifest deadline and appends evidence; a completed observer is not full lab acceptance.

The tester and operator identities have utility-scoped Sentinel Responder, query and queue-sender rights, and shared-workspace query access for generator availability checks. Only the operator additionally receives read-only Utility A resource-group metadata access needed by Bastion; neither its Azure identity nor the future agent receives VM Contributor. Responder permits authorized incident handling; the harness does not automatically close incidents. The future reader/action-writer agent has neither the operator's SSH key nor operator roles.

Invoke harness commands on the private operator through the signed-in deployment user's Azure management access, for example:

```bash
az vm run-command invoke -g "$(jq -r .utilityAResourceGroup deployment-outputs.json)" \
  -n vm-uta-operator --command-id RunShellScript \
  --scripts 'set -e; docker exec swu-operator /opt/harness/bin/python /lab/harness/swuharness.py health'
```

This management path belongs only to the operator subnet; it does not restore VM run-command availability on locked-down control/test. After lockdown, `deploy.sh` runs `scripts/verify-operator.sh`: it checks that broad operator build egress is absent, restarts the operator to force fresh authenticated connections, and requires successful live schema/content/health checks. Initial role/ingestion convergence is retried with explicit error logs and a finite limit; command admission alone is never success. Verify actual managed-identity roles and retain this evidence before accepting the path. Run subscription cost reporting locally under the approved billing/deployment identity: the operator is deliberately not granted subscription-wide cost access.

Successful post-lockdown verification records `warmupStartedAt` in both local and private-operator manifests. Infrastructure `deployedAt` is not a substitute: acceptance refuses a missing or younger-than-seven-days readiness timestamp and still requires complete hourly ingested receipts. Without the optional operator, complete equivalent private-path verification and record its actual UTC completion time; do not copy the infrastructure timestamp.

Leave generation and all required VMs running for seven real days from verified readiness, with acceleration set to one. Nightly shutdown is disabled by default. Then run:

```bash
python harness/swuharness.py ready
python harness/swuharness.py run SCN-01 --utility uta
python harness/swuharness.py suite --utility uta --scenario SCN-01
python harness/swuharness.py baseline-check --hours 24
python harness/swuharness.py volume-check
python harness/swuharness.py availability-check --hours 24
python harness/swuharness.py cost-report
```

`suite` runs three repetitions for the selected scope; omit filters to select all applicable utility/scenario combinations. It stops at a failure or an existing open High incident. Each run observes strict injection deadlines even when waiting longer for physical evidence. Successful runs wait through the two-hour scenario lifetime and at least two additional recovery hours. The operator must close previous test incidents and check recovery before the next run; the harness does not auto-close unrelated incidents or elevate its identity.

A full suite can take days and incur substantial cost. Approve its operating window explicitly. A single filtered suite is not evidence that every scenario passed.

`availability-check` measures generator success-trace gaps, including both observation-window boundaries, against the original ten-minute maximum. Run it outside deliberate SCN-09 disruption and retain source-specific health evidence separately. `cost-report` queries month-to-date charges for the recorded resource groups, including untagged managed disks; the budget uses the same group scope. It is observation-only, not proof that full-month cost meets the original target, and budget notifications do not cap expenditure.

`content-check` executes all normalization functions, checks their column types and hidden-field exclusions, and queries every configured watchlist. Empty maintenance/disabled-account lists are legitimate; a failed query is not. `health` checks positive PLC state separately from receipt freshness, so recurring `PlcUnreachable` messages cannot make a broken PLC appear healthy. The workbook likewise separates collector state from source receipt health.

`history-check`, also required by post-lockdown verification, requires a successful history timer trace in the last ten minutes and populated reference receipts within ten minutes of actual ingested source receipts. Empty results, missing references and failed queries cannot pass. This is initial operational proof, not a substitute for multi-day acceptance.

`volume-check` requires telemetry arrival, the actual configured 1 GB daily cap, and complete billable-usage evidence for the previous UTC day. It includes all billable workspace data, not just generated custom tables, and rejects missing hourly coverage or consumption at or above 200 MB. Preserve the separate last-24-hour custom-telemetry reading as well.

## Failures and recovery

Inspect generator traces/failure alerts, rejected-request state, queue poison messages, gateway logs, the SQLite outbox and raw run-tagged records. Fix ingestion/role/lease failures; do not delete pending state to make the next run appear healthy. Delivery is at-least-once, with normalization-level deduplication.

The harness sends Base64 queue messages and the Functions host decodes Base64. Preserve that paired configuration when changing senders. PLC unreachability/recovery state survives gateway restarts together with the outbox; malformed control files and checkpoint errors are explicit failures, not accepted data loss.

The generator restores original setpoints after a scenario's two-hour lifetime. The harness attempts every actual-PLC setpoint restoration and clears gateway metadata even if one restore fails; failed cleanup invalidates its result file. Physical tank/chlorine recovery may take longer than setpoint restoration.

SCN-09 intentionally suppresses selected feeds. Confirm suppression metadata is cleared and actual receipts resume. Do not treat health-query errors, absent telemetry or not-applicable scenarios as passes. Low health alerts for naturally quiet sources follow the original sixty-minute requirement.

Use approved, narrow maintenance windows for cleanup or record its High alerts as acceptance failures. Run the clean 24-hour baseline only after recovery and incident handling; do not hide cleanup by filtering run identifiers from baseline queries.

## Teardown

Use only the recorded manifest and confirm the subscription:

```bash
bash scripts/teardown.sh "$AZURE_SUBSCRIPTION_ID"
```

The script prechecks group ownership/deployment tags, deletes only listed groups and the named budget, and tolerates already-deleted groups on a retry. It does not purge a soft-deleted Key Vault. Missing or invalid manifests must be corrected, not replaced with a broad tag-based deletion command.
## Route A/B subscription foundation

`infra/route-ab.bicep` provisions the Route A/B foundation independently of the
original Level 1 deployment. `scripts/provision-route-ab.py` requires an isolated,
already authenticated `--azure-config-dir`, explicit subscription/tenant/owner,
and a public SSH key. It checks the signed-in identity and refuses to overwrite
resource groups without the Route A/B ownership tags. State is written only to
the ignored `route-ab-deployment.json`, never to `deployment-outputs.json` or
`console-deployment.json`.

The default `swurab` deployment creates `rg-swurab-shared`,
`rg-swurab-routea`, and `rg-swurab-routeb`. Route A uses `10.30.0.0/16`;
Route B uses `10.40.0.0/16`. Each has separate PLC, HMI, gateway, scenario,
and operator VMs, a Sentinel workspace, tables, DCRs, and detection content.
Both represent Utility A (`uta`); their independent workspaces and route IDs
separate the two access profiles. Route A allows the isolated scenario subnet
to reach Modbus. Route B permits HMI traffic but blocks that direct PLC path.
Neither route exposes VM/PLC/HMI ports publicly. The services VNet is
`10.20.0.0/16`, with private, identity-only Blob/Queue storage and no forwarded
traffic or transit between the two route VNets.

Subscription bootstrap must also register the networking feature
`Microsoft.Network/AllowBringYourOwnPublicIpAddress`, then re-register
`Microsoft.Network`. Azure required this feature even for the ordinary
outbound-only NAT public IP resources; those IPs provide no inbound PLC/HMI
endpoint. Do not work around the prerequisite by publishing VM ports.

The publisher validates and previews changes, provisions the resources, verifies
Docker/cloud-init using Azure VM Run Command, and removes temporary build egress,
including on guest-bootstrap failure. Control and scenario subnets then use a
null Internet route. A separate SSH key is retained outside the repository.
On a fresh Sentinel workspace, watchlist creation can finish before its custom
columns are queryable. The first stage provisions reference data without new
analytics rules. The publisher then queries the actual Roster, Vendors,
SafeLimits, and RemoteTools fields and requires nonempty results in both
workspaces before enabling all eleven rules per route in the lockdown stage.
The wait is bounded to fifteen minutes; schema convergence and throttling are
reported, while permission and unrelated query errors fail explicitly. Query
validation is never skipped and missing reference columns are not defaulted.
Rule readback uses the NRT-capable `2024-01-01-preview` API, matching DR-07's
creation API; the stable `2024-03-01` list omits NRT rules. Verification requires
all eleven lab rules with the correct Scheduled/NRT kinds, follows pagination,
and preserves unrelated Microsoft-native rules such as Fusion.
Existing subscription budgets are not modified; budgets are alerts, not spending
caps. A retail-price lookup during this provisioning attempt returned HTTP 429,
so no complete monthly-cost estimate is certified. Private Premium Bastion,
VPN Gateway, Azure Firewall, Defender target
configuration, and a public console are deliberately not provisioned by this
foundation. Supported Azure control-plane management remains available.

**Foundation provisioning is not application readiness.** The foundation does
not install OpenPLC/plant/gateway containers, Route B HMI password/session
enforcement, bounded scenario execution, live process graphics or the CPI
tenant's authenticated console. The separate application publishers and
verification reports below cover those surfaces. The existing MCAP console
does not gain access to this tenant or these PLCs. Do not start acceptance or
claim live process ingestion merely because the Sentinel resources exist.

Run the publisher with the same explicit identity parameters and `--verify-only`
to recheck a completed foundation. Re-running provisioning uses the original
deployment timestamp; changing subscription/tenant/prefix/region/VM size in an
existing manifest after resource creation starts is rejected. A VM-size change
after a failed preflight is allowed only if none of its resource groups exist.
The deployment uses `Standard_E2as_v6`, validated against actual East US 2
capacity rather than relying solely on nominal vCPU quota. Later runtime publication requires a controlled
build-egress window followed by lockdown and fresh verification.
`--resume-readiness` recovers a failed guest/reference-data check only when its
manifest confirms that build egress was already removed. It does not reopen
Internet access or recreate the initial foundation. Guest Run Command scripts
use `@file` transport to preserve all lines through Windows `az.cmd`; Azure's
generic "Enable succeeded" response alone is not guest success.

## Route A/B application runtime and console

The application publisher checks the same isolated account and foundation
ownership, adds scenario-VM managed identities and per-route storage roles,
and opens a controlled build-egress window. It builds the pinned actual
OpenPLC and application images on the Azure VMs, then removes build egress in
`finally`, including on deployment failure. It never substitutes the soft-PLC
test fixture. PLC port 1502 is loopback-only; the private port-502 proxy admits
setpoint writes from the Route A scenario VM or each route's HMI, not the
gateway. Route B additionally blocks scenario-to-PLC traffic at the NSGs.

Use the approved owner, isolated CLI directory and external public-key path:

```powershell
.\.venv\Scripts\python.exe scripts\deploy-route-ab.py --owner $ApprovedOwner --azure-config-dir $RouteAzureConfig --ssh-public-key $RouteSshPublicKey
.\.venv\Scripts\python.exe scripts\verify-route-ab-runtime.py --owner $ApprovedOwner --azure-config-dir $RouteAzureConfig
$env:AZURE_CONFIG_DIR = $RouteAzureConfig
.\.venv\Scripts\python.exe scripts\deploy-console.py --route-ab --phase provision
.\.venv\Scripts\python.exe scripts\deploy-console.py --route-ab --phase publish
.\.venv\Scripts\python.exe scripts\deploy-console.py --route-ab --phase verify
.\.venv\Scripts\python.exe scripts\verify-route-ab-console.py
```

The verifier changes the actual dose setpoint temporarily, reads it back,
restores it, checks Route B raw-PLC/unauthenticated/wrong-password denials and
gateway write rejection, and waits for the changed value in each real Sentinel
workspace. `runtimeDeployed` becomes true only after those checks. Its report is
`test-results/route-ab-runtime-verification.json`; seven-day/formal scenario
acceptance is a separate gate.
Console verification records are separate from MCAP:
`test-results/route-ab-console-azure-verification.json` and
`test-results/route-ab-live-console-verification.json`. The latter requires
actual queue dispatch, PLC readback, baseline restoration and all three audit
stages in each real Sentinel workspace before setting
`consoleIntegrationVerified`. An alert is reported only if the actual evidence
query finds one; it is not a prerequisite substituted for process/action proof.
The publisher verifies the actual live view and API feature flag, not just the
requested deployment settings. The Live plant view opens after saved settings
load. A timed-out Sentinel query is explicitly unavailable, never reported as
an empty successful query; verification retries evidence reads without
redispatching the physical action.

An expired Entra browser session is separate from route availability. The
console detects authentication redirects without following them as cross-origin
API requests and displays **Sign in again**. Authentication-expired polling
stops until the page is reloaded; control POSTs are never automatically replayed.
Old tank/chlorine/pump values are explicitly labelled **LAST OBSERVED / NOT LIVE**
with their observation date and time, and stale controls remain disabled.
Both route cards show a motor, pump, inlet pipe and storage tank. Motor rotation
and moving inlet dashes indicate the fresh PLC `PumpState`, not button acceptance
or independent motor/flow sensor feedback. OFF, stale or unavailable observations
pause animation; reduced-motion preferences disable it. Tank height always uses
observed `TankLevelPct`, with no accelerated fill or invented process readings.
OpenPLC runs the actual 500 ms control program: normal pump thresholds are 40%
and 90%, with a pump override and chlorine dose output. The separate plant
simulator reads those outputs, computes tank/chlorine physics and feeds sensor
inputs back into OpenPLC. The console visualizes this loop; it does not replace
the PLC or change simulation speed. Restoring an override returns to automatic
logic and does not guarantee an immediate pump stop.
Both diagrams also show level/chlorine sensors feeding the PLC. Route A's
command line goes directly from private-worker Modbus access to the PLC;
Route B shows a password-only HMI computer before the PLC. These are illustrative
application paths, not packet captures or new public device endpoints. The HMI
screen mirrors workflow/pump observations; it is not a separate interactive login.
A confirmed button click highlights a pending request, with queued/executing
states distinct from `plc-observed` readback and `completed-restored` restoration.
The readable command panel names the requested write (`PumpOverride = 1` or
`ClDoseSetpoint = 4`), its 20-second hold, actual tag readback and previous-value
restore target. Restoration is labelled verified only after the worker reports a
restored terminal stage. These are engineering tag values, not raw Modbus wire
register words, and the panel is not an arbitrary command editor.
Failed, interrupted, expired and uncertain outcomes are explicit, not successful
animations. Sensor/command traffic animation pauses on stale or unavailable
observations and respects reduced motion; pump motion remains driven only by
the fresh PLC `PumpState`.
For stopped feeds, check VM power states and Azure Activity Log start/deallocate
operations as well as worker logs. A disabled lab auto-shutdown schedule does not
prevent another identity or an organization-level process from stopping VMs.
Do not disable external shutdown controls to recover the lab. Starting only the
PLC/HMI/gateway/scenario VMs restores the application path without starting
unused operator VMs; VM charges resume.

Route B uses an actual password session, Secure/HttpOnly/SameSite cookies,
session CSRF tokens and HTTPS with a pinned private certificate. Its username
is `operator@uta.lab.example`; its generated password, signing key and TLS key
are delivered through encrypted Azure extension protected settings and retained
only in root-readable VM files. Never copy them into manifests, logs, container
images or chat. The password is not an Entra account and MFA is deliberately
absent at this HMI. Re-publishing rotates the private HMI credentials/session;
the paired worker receives the matching credentials.

The separate CPI console retains exact-tenant, administrator-only Entra
authentication. Its identity can send only to the two route command queues,
read each route's state and query the two workspaces, plus manage its own
settings container. It has neither HMI credentials nor direct OT network
access. A scenario-VM worker follows direct Modbus for A or authenticated HMI
for B. Only pump override and dose-4.0 presets are accepted, with explicit
confirmation and a 20-second hold. Baselines are journaled before writes;
readback and restoration are required. A failed restoration blocks later
commands, and restart recovery restores without replaying the action.
The Modbus proxy serializes requests on one persistent upstream PLC connection,
instead of opening an OpenPLC connection for every read/write. Gateway write
denials and the fixed register allowlist remain enforced before forwarding.
The direct worker closes a failed transport and retries a read once; writes
are never automatically retried because their outcome may be uncertain.
The durable journal retains command, baseline, readback and failure details.
`interrupted-restored` means an unfinished action was recovered and restored;
it does not itself prove a process restart. `recoveryReason` distinguishes
startup recovery from recovery in the same process, and no action is replayed.

For an existing deployment, a narrowly scoped, offline control-path repair is:

```powershell
python scripts\deploy-route-ab.py --azure-config-dir <isolated-CPI-config> `
  --owner <approved-CPI-owner> --repair-control-path
```

It updates only the existing proxy/runner containers using locally cached VM
images, verifies deployed module hashes, preserves private credentials and
network rules, and refuses runner replacement while a journal action is
unfinished. It does not rebuild OpenPLC, rotate HMI secrets or open build egress.
The repair record remains `validation-required` until actual cloud readback,
20-second hold and restoration have been verified.

The gateway remains read-only and emits actual PLC observations into
`SimProcess_CL`, leaving unknown human attribution empty. The private audit
queue/relay emits the worker's actual requested, observed and restored actions
into `SimAudit_CL` with the requesting administrator and command ID. Queue
delivery is at-least-once, with stable record IDs for query deduplication.
These are lab telemetry tables, not fabricated native Entra, Defender or
Firewall records. The console distinguishes queue acceptance, PLC readback,
restoration, actual Sentinel-query results and correlated alerts; an absent
alert is not a successful detection.

The Live plant tab displays timestamped tank/chlorine/pump observations and
the last 120 samples; stale/unavailable values disable controls. Restoring
setpoints does not instantly undo the simulated water/chemical state. The
original console, deployment manifests and MCAP resources remain separate.
Future foundation publication preserves the CPI console subnet and runtime
permissions when their separate state records exist. The secured Target,
native security feeds and certified total monthly cost remain separate work.

## Route A/B cost pause and resume

The dedicated `scripts\route-lifecycle.py` operates only the explicitly selected,
tagged Route A/B groups and their console. It does not operate the original MCAP
lab or future Target. It verifies subscription, tenant, group/VM ownership, the
fixed five-VM-per-route inventory, private storage ownership and the existing
single console revision before making any change. An Azure user or an OIDC
service principal can run it; neither credentials nor deployment manifests are
arguments. Use a separate Azure CLI configuration for the CPI account.

```powershell
# Authenticate the approved CPI identity in its own CLI configuration first.
$env:AZURE_CONFIG_DIR = '<isolated-CPI-Azure-config-directory>'
az login --tenant <CPI-tenant-ID>
python scripts\route-lifecycle.py status --subscription <CPI-subscription-ID> --tenant <CPI-tenant-ID>

# Without --apply these commands only inspect and print the scoped plan.
python scripts\route-lifecycle.py pause --subscription <CPI-subscription-ID> --tenant <CPI-tenant-ID>
python scripts\route-lifecycle.py pause --subscription <CPI-subscription-ID> --tenant <CPI-tenant-ID> --apply --confirm pause:swurab
python scripts\route-lifecycle.py resume --subscription <CPI-subscription-ID> --tenant <CPI-tenant-ID> --apply --confirm resume:swurab
```

Pause deactivates the console revision and requires zero running replicas; merely
setting minimum replicas to zero would not stop HTTP-driven activation. Each
running worker must finish its action/restoration. A SQLite write lock prevents
another action from journaling while the runner container stops. If an unfinished
action cannot restore within the deadline, pause fails with the console inactive
and does **not** deallocate any VMs. Investigate/recover the worker before retrying.
Once both workers are drained, scenario, gateway, HMI, PLC and operator VMs are
deallocated in that order. Already deallocated VMs are left alone.

Resume keeps the console unavailable while starting the eight application VMs;
the operator VMs are left unchanged and are not started. It explicitly starts
named containers because Docker's `unless-stopped` policy does not restart a
manually stopped runner. Before worker VM startup, it outwaits the five-minute
command-validity window plus the permitted one-minute clock skew, measured from
confirmed console shutdown. This also protects an externally stopped VM whose
old runner could automatically start on boot. Allow several minutes for resume.
Recovery then restores unfinished journaled baselines without replaying the
action. Remaining queued commands are cancelled/deduplicated, with a durable
`expired-unobserved` result and real `LabControlCancelled` audit records for
previously unexecuted commands. It does not fabricate control/readback evidence.
An invalid queue message or failed recovery blocks reopening the console.

Both workers must publish fresh, error-free observations through their actual
direct-Modbus/password-HMI paths after startup before the console revision is
reactivated. Resuming an already-running, active deployment only verifies fresh
observations; it does not interrupt controls. The script does not change Entra,
private credentials, endpoints, NSGs, IAM or build egress, and requires neither
public OT access nor a local application server. Azure Run Command uses file
transport and an exact guest-result marker; an Azure command exit code alone is
not accepted as guest success.

Results are saved under ignored `test-results\route-lifecycle.json`. A failed
mutation records failure rather than leaving an earlier successful report;
partial operations fail explicitly and can be investigated/retried. No resources
are deleted. The command journal and stored evidence remain on disk/in Blob;
OpenPLC and plant processes restart, so volatile process state can reinitialize
from the profile rather than resume at the previous tank level.
Managed disks, storage, private endpoints, registry and retained
logs can still incur charges. The original MCAP generator/console/resources are
not stopped by this script. Pausing also interrupts continuous observation and
is incompatible with an uninterrupted seven-day acceptance period; do not treat
downtime as acceptance evidence or silently move the original warm-up timestamp.
The Admin settings tab is not a start/stop control.

### GitHub lifecycle workflow setup

The manually dispatched `.github\workflows\route-lifecycle.yml` has no schedule.
Publish/merge it to the repository's default branch before using the Actions
page; mutations are deliberately restricted to that branch. Configure a
**protected `lab-lifecycle-cpi` environment** with required reviewers and these
environment variables: `CPI_AZURE_CLIENT_ID`, `CPI_AZURE_TENANT_ID` and
`CPI_AZURE_SUBSCRIPTION_ID`. These identifiers are configuration, not passwords.
Do not reuse the original lab's `AZURE_*` configuration.

Create an approved Azure application/service principal and GitHub OIDC federated
credential with subject
`repo:srvenkatms/WaterSupplyInisghtIQ:environment:lab-lifecycle-cpi`,
issuer `https://token.actions.githubusercontent.com` and audience
`api://AzureADTokenExchange`. Grant only the scoped management permissions needed
to read the three owned groups/storage/VMs/console, read VM power states, start
and deallocate the ten route VMs, invoke guest Run Command on application VMs,
read console revisions/replicas and activate/deactivate that console revision.
Have the Azure administrator validate a custom role against provider operations
and scope it to the owned resources/groups; do not grant subscription Owner or
data-plane storage access. Guest operations use the existing VM managed
identities and private endpoints. This repository does not automatically create
federation, grant roles, configure environment reviewers or save account details.

In **Actions > Route A-B lifecycle > Run workflow**, select `status`, `pause` or
`resume`. Changes require the exact matching `pause:swurab` or `resume:swurab`
confirmation and environment approval. The workflow serializes lifecycle runs,
does not cancel one midway, and uploads its result. Do not run competing CLI
operations or deployments at the same time; the workflow lock cannot serialize
commands run outside GitHub.

CI validates branch pushes and pull requests. The existing original-lab
deployment workflow is separate: it needs explicit
`SWULAB_ENABLE_DEPLOYMENT=true`, its own `AZURE_*` variables, protected environments
and deployment prerequisites. It is not a full CPI runtime deployment pipeline.
Publishing source does not redeploy Azure or automatically synchronize subsequent
uncommitted local changes: commit/push updates and deliberately run the relevant
publisher for cloud changes. Generated manifests, evidence, keys and Azure login
state must remain outside Git.
