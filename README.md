# WaterSupplyInisghtIQ

Small Water Utility Cyber Lab, **Level 1**: three isolated Azure Sentinel workspaces, synthetic IT/OT telemetry, eleven detection rules, ten scenarios, and an actual OpenPLC process path for Utility A.

**Status: Azure deployed and private readiness verified; full acceptance remains pending.** Post-lockdown schema/content/history/health, private Modbus/HMI reads, bounded network denials and gateway protocol-write rejection passed on 2026-11-05. The preserved local OpenPLC run also passed five integration cases, including a one-hour comparison with 2,727 samples and a five-minute outage/restart. These observations do not certify every deployed scenario, permission or recovery case. Formal acceptance still requires seven real days from verified readiness and three consecutive applicable scenario passes; see the [actual evidence and remaining gates](docs/acceptance.md).

| Utility | Collection tier | Process source |
|---|---|---|
| A (`uta`) | Process awareness | Actual pinned OpenPLC v3, plant simulator, HMI, read-only gateway |
| B (`utb`) | Baseline | Synthetic IT telemetry; no process collector |
| C (`utc`) | OT visibility | Synthetic OT alerts and process model |

The four-agent service, production connectors, association-wide intelligence and Level 2 are **not implemented**. Teams is optional and off by default. See [architecture and Route A/B mapping](docs/architecture.md), [operations](docs/operations.md), and [acceptance requirements](docs/acceptance.md).

## Repository

`profiles/` is the utility configuration source. `generator/` produces synthetic telemetry and manages durable scenario state. `apps/` contains OpenPLC integration, the plant, HMI, protocol-enforcing Modbus proxy and gateway. `infra/` provisions Azure resources and Sentinel content. `harness/` collects run-specific live evidence; `rules/contract.json` records original severity and latency expectations.

The supplied Python soft PLC remains a **test fixture only**. The deployment builds the actual OpenPLC source revision `b5d41356dab4aeadca0dd7ca64ba542f870b595d`; it does not substitute the soft PLC when building or running OpenPLC fails.

## Azure console and Admin tab

The console is deployed in **Azure Container Apps**, with Microsoft Entra sign-in restricted to the configured lab administrator. [Open the Azure console](https://ca-swulab-console.agreeablecliff-486dc8f9.eastus2.azurecontainerapps.io/). Admin settings and bounded synthetic scenario datasets persist in private Azure Blob Storage using a container-scoped managed identity. VPN/private-only access remains a visible, disabled future option. No local server is required for the Azure deployment.

Publish using `.\.venv\Scripts\python.exe scripts\deploy-console.py` with the approved Azure account and existing `deployment-outputs.json`. The publisher provisions an isolated environment, builds and deploys an application-only image, and verifies Entra policy before enabling public HTTPS. The returned URL and deployment state are saved in ignored `console-deployment.json`; see [Azure console operations](docs/operations.md#azure-console).

**The original MCAP console remains synthetic-only.** Its on-demand runs generate cloud datasets, not live PLC/HMI actions or Sentinel scenario injection; the live acceptance harness remains authoritative. Saving Admin settings does not stop VMs, alter timers or deploy Teams.

The separate Route A/B foundation uses `infra/route-ab.bicep` and
`scripts/provision-route-ab.py`, with its own subscription-scoped authentication
and ignored `route-ab-deployment.json`. It does not repoint this console or
overwrite the original lab manifest. See [Route A/B provisioning and its
application-readiness boundary](docs/operations.md#route-ab-subscription-foundation).
The separate runtime publisher, `scripts/deploy-route-ab.py`, installs actual
OpenPLC, private route workers and Route B password-only HTTPS HMI access.
The CPI console uses `scripts/deploy-console.py --route-ab` and its own
`route-ab-console-deployment.json`. [Open the CPI Route A/B live console](https://ca-swurab-console.wittyocean-9e634a1c.eastus2.azurecontainerapps.io).
It adds bounded live controls and observed
tank/chlorine trends without changing the original MCAP console. Deployment
alone is not readiness: require the runtime and authenticated console
verification records described in operations. Both access paths, console
dose-change/readback/restoration workflows and Sentinel lab-action audit
ingestion were verified on 2026-11-06. The actual rendered UI's pump controls
also completed and restored on both routes. This is not seven-day/formal
scenario acceptance or proof of correlated alerts or native Target feeds.
New control runs also expose a **Raw PLC commands / responses** panel:
actual Modbus TX/RX hex and decoded register fields, including acknowledgement,
readback and baseline restoration. Route B separates the HMI application
exchange from its downstream PLC traffic; credentials are excluded.

## Stop and restart Route A/B

`scripts\route-lifecycle.py` provides guarded `status`, `pause` and `resume`
operations for the separate Route A/B deployment. It defaults to read-only;
changes require `--apply` and an exact confirmation. Pause stops the console,
waits for journaled actions to restore, then deallocates all ten route VMs.
Resume starts the eight application VMs, restores any unfinished baseline,
cancels queued old actions and requires fresh PLC observations before reopening
the console. The two unused operator VMs are not started.

The manual **Route A-B lifecycle** GitHub Actions workflow uses a protected
`lab-lifecycle-cpi` environment and Azure OIDC, with no scheduled shutdown.
It requires default-branch publication, environment variables, federation and
scoped Azure permissions before it can operate. See
[commands, GitHub setup and remaining costs](docs/operations.md#route-ab-cost-pause-and-resume).
Managed disks, storage, private endpoints, registry and log retention remain
billable; this is not a zero-cost shutdown or an original-MCAP shutdown.
Source changes are synchronized through ordinary Git commits/pushes and CI;
an existing running Azure deployment is not automatically updated by a push.

## Local console and Admin tab

The loopback-only browser console includes **Admin**, **Overview** and **Scenarios** views. Start it with the existing Python dependencies:

```powershell
$env:PYTHONPATH = "$PWD\apps;$PWD\generator"
.\.venv\Scripts\python.exe -m swuapps.console
```

Open **http://127.0.0.1:8787/**. Admin persists the operating mode, default utility, run confirmation and optional Teams team/channel configuration under ignored `local-data/console/`. Teams parameters can be exported using the existing Bicep names; deployment and tenant connector authorization remain separate.

**On-demand** allows bounded offline scenario datasets using the existing generator; files are saved under `local-data/console/runs/`. They are synthetic JSONL, not live PLC/HMI actions or Sentinel results. **Acceptance** disables those accelerated datasets as evidence and displays the immutable real warm-up eligibility from the deployment manifest. The existing live harness remains authoritative.

**Saving console settings does not pause Azure timers, deallocate VMs, deploy Teams or change the running Azure lab.** Azure lifecycle controls and live graphical scenario execution are not implemented by this initial Admin console. Do not expose this single-user, local-only server through a public proxy.

## Local checks

Use Python 3.11 or later, Azure CLI with Bicep, and Bash/`jq` for the infrastructure/script tests. On Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r generator\requirements.txt -r apps\requirements.txt -r harness\requirements.txt pytest
$env:PYTHONPATH = "$PWD\generator;$PWD\apps;$PWD\harness"
.\.venv\Scripts\python.exe -m swugen.configuration --check
.\.venv\Scripts\python.exe -m pytest -q generator\tests apps\tests harness\tests
az bicep build --file infra\main.bicep
```

The default suite deliberately excludes the actual OpenPLC test. Run it explicitly against a working Linux Docker engine:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m openplc apps\tests\test_openplc_runtime.py
```

CI requires this real-runtime test separately. A missing Docker engine is a failure, not a skip or a successful substitute.

For the original one-hour comparison and five-minute actual PLC outage, use unaccelerated durations and preserve the evidence:

```powershell
$env:SWU_OPENPLC_OUTAGE_SECONDS = "300"
$env:SWU_OPENPLC_OBSERVATION_SECONDS = "3600"
.\.venv\Scripts\python.exe -m pytest -q -m openplc apps\tests\test_openplc_runtime.py --junitxml=test-results\openplc-live.xml
```

## Deployment

Run the Bash scripts from Linux or Git Bash with Azure CLI, Python, `jq`, `zip`, and `tar` available. Configure the approved subscription, owner, SSH public key, budget and notification recipients before running `scripts/deploy.sh`. The script provisions infrastructure, publishes the generator, builds/deploys OpenPLC and apps, checks readiness, and removes temporary build egress. It writes `deployment-outputs.json`; preserve that manifest for the harness and scoped teardown.

If no private operator network exists, set `SWULAB_DEPLOY_OPERATOR=true` and provide a protected `SWULAB_SSH_PRIVATE_KEY_FILE` matching the VM public key. The optional private operator uses managed identity, pinned-key Bastion SSH tunnels and restricted Azure HTTPS egress; it has no public IP and does not change the control/test null-route boundary. Its CLI and harness runtimes are isolated. See the [operator bootstrap and invocation instructions](docs/operations.md).

**Do not deploy on the strength of an existing Azure login.** Private-only Premium Bastion and NAT add ongoing costs. The budget is an alert, not a spending limit; the original monthly cost target is not demonstrated. Review region availability, pricing and private access before authorizing resources.

## Important requirement conflicts

Original limits are preserved rather than silently relaxed: DR-07 must produce an incident within five minutes of injection, and process-feed silence becomes detectable after thirty minutes. Physical chlorine/tank responses and a subsequent silence alert can exceed the requested injection-to-incident deadlines. Naturally quiet identity/access sources may also trigger the original sixty-minute health threshold.

Setpoint restoration is a real change and can trigger DR-06 outside an approved maintenance window. The acceptance runbook requires explicit cleanup planning, incident handling and recovery observation; it does not exclude cleanup alerts to manufacture a clean baseline.