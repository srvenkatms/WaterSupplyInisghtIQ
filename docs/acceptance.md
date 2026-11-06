# Acceptance and evidence

**Full Level 1 acceptance is not certified.** The limited Azure readiness proof below passed, but does not certify the scenario, duration, recovery or permission cases. Compilation and local tests exercise code, not the deployed Azure service. Complete the supplied Level 1 TC-01–12 and TC-14–19; TC-13 applies only when Teams is explicitly enabled. Use the original document for the authoritative numbering and wording.

| Evidence surface | Automated support | Live work still required |
|---|---|---|
| Actual OpenPLC | Actual pinned image built; local scaling/physics/proxy denial exercised; explicit duration-controlled Docker tests | Preserve one-hour/five-minute results and repeat on deployed gateway; real incident/recovery evidence |
| Schema/isolation | `schema-check` checks ten table schemas and observed UtilityIds/RecordIds | Initial applicable-source arrival within thirty minutes; cross-workspace negative ingestion tests |
| Normalization/detections | Compiled content, local mirror tests, `content-check` for deployed function schemas/watchlists | Verify real alerts/incidents and original semantics; schema checks alone are not rule acceptance |
| Warm-up | `ready` checks elapsed seven days and hourly real ingestion receipts | Preserve deployment/uptime evidence, acceleration=1 and any outages |
| Scenarios | `run`; three-repetition `suite`; run-specific raw evidence and alert/incident IDs | All applicable combinations, clean state between runs and three consecutive passes |
| Baseline/volume | `baseline-check --hours 24`; `volume-check` verifies actual cap and complete prior-UTC-day billed usage | Include SCN-10; preserve real results and full-month cost review |
| Health | Tier-aware `health`; original DR-11 thresholds; `availability-check --hours 24` | Suppress/restore each required source; test never-seen and High-incident escalation; maximum ten-minute generator gap |
| Agent boundary | Reader/action-DCR-only deployment roles | Prove reads succeed, action-log writes succeed, and all other writes/control operations fail |
| Network | Proxy negative tests and lockdown script | Real NSG/effective route/connectivity probes, public exposure checks and bypass attempts |
| Operations | Manifest-scoped deployment/teardown and failure logging | Verify Functions publishing/host storage, alerts, private access, recovery and complete scoped teardown |
| Optional Teams | Disabled-by-default template | Tenant authorization and comments-only behavior, if approved |

## Original timing remains binding

`rules/contract.json` defines expected severity, ICS technique references and maximum injection-to-incident latency. Scheduled targets remain fifteen minutes; DR-07 remains five. Results additionally record alert latency and the last-evidence-event-to-alert interval. A longer observation period does not turn a missed deadline into a pass.

The supplied physical scenario timing can conflict with those limits: process values can leave range much later than the injection; DR-11 process silence requires thirty minutes before a five-minute scheduler can detect it. Resolve clock/scenario requirements with the owner **before claiming compliance**. This implementation intentionally exposes those failures instead of extending the contractual limits.

The original source limits are sixty minutes for sign-in/access/firewall/endpoint, thirty minutes for process and eight days for exposure/inventory. These describe received data, not just source event timestamps. Low-volume source design can legitimately generate silence alerts; no thresholds were silently lengthened.

## Preserved local runtime evidence

On 2026-11-04, the pinned actual OpenPLC suite completed all five cases with no failures or skips in approximately 66 minutes. The sustained case recorded 3,600 observation seconds and 2,727 comparisons; the outage case ran with a 300-second stopped-PLC interval. Evidence is in ignored `test-results/openplc-live.xml`. A separate latest-code recovery regression passed and is recorded in `test-results/openplc-recovery.xml`.

An additional actual-backend HMI test and a recovery rerun both passed, with evidence in `test-results/openplc-hmi-recovery.xml`. The HMI test invokes Flask routes through their test client against a real live Modbus proxy and OpenPLC backend: valid HMI setpoints reach the PLC's dose output and generate gateway change events, while actuator writes and invalid state values receive HTTP 400. This proves application routing/backend integration, not deployed HTTP network access.

These are local software-runtime results, not Azure TC-15/TC-18 signoff. Deployed gateway transport and the limited subnet probes below subsequently passed; sustained deployed register agreement, Sentinel DR-11 incident/recovery behavior and real cloud outage recovery remain unverified. Normalization/source unions use separate origin columns before projecting `Source`, avoiding collision with the exposure record's own `Source` field.

## Azure preflight evidence

Azure `what-if` succeeded in East US 2 with the available `Standard_E2as_v6` alternative after fixing a leaked ARM loop expression and rejecting unavailable B2/D2 SKUs. It previewed 185 creations and 16 unidentifiable managed-identity role assignments; the latter depend on runtime principal IDs and are **not verified permissions**. That preflight itself created no resources; the later approved deployment is recorded below. The full application Dockerfile also built and passed Python 3.11 import/profile checks using verified offline wheels.

This historical preflight is not deployment or acceptance evidence.

## Azure readiness activation

The approved deployment passed fresh post-lockdown readiness on **2026-11-05 at 17:07:46.014409 UTC**. The identical timezone-aware `warmupStartedAt` is persisted in the operator and local deployment manifests.

- Actual Function publishing and real ingestion into all three workspaces succeeded. Raw schemas, normalized functions/watchlists, canonical receipt-history freshness and positive Utility A PLC state passed from the private operator after its restart.
- Explicit collector heartbeats arrived in all three workspaces and were absent from the attack-event normalizers. They do not fabricate successful users, sessions or PLC health.
- Temporary build rules were absent in boundary/control/test/operator NSGs. Control/test had no NAT, disabled implicit outbound and a `0.0.0.0/0` route with next hop `None`.
- Actual private Modbus and HMI reads succeeded. Fresh Internet TCP 443 attempts failed from both control and test; test-origin raw PLC 1502 and admin 8080 attempts also failed. These are bounded observations, not proof of every possible endpoint or protocol.
- A subsequent live configuration check confirmed all five VM interfaces have no public IP attached and Bastion is Premium/private-only with no public IP.
- At 17:20 UTC, the deployed gateway-route protocol proof passed: real register reads succeeded; same-value FC6/FC16 setpoint writes, an actuator write and FC1/FC4 reads received explicit IllegalFunction rejections. Setpoints remained unchanged and the result was persisted in the operator's host-backed acceptance volume. This does not certify every vendor/debug function or replace duration/recovery cases.
- Protected operator bootstrap resources were confirmed deleted. Acceptance results have a host-backed volume so container recreation does not erase them.

The first eligible seven-day readiness check is **2026-11-12 at 17:07:46.014409 UTC**, subject to continuous real ingestion. `ready` rejects earlier execution; neither the infrastructure creation date nor backfilled timestamps can substitute. No scenario suite has been certified, and no seven-day or full-month observation is claimed. A read-only post-activation incident preflight found no open High incidents in any utility; repeat that check before acceptance and preserve any later blockers.

The USD 1,000 monthly budget is an alert, not a spending cap or proof of cost compliance. Resources remain running and billable. Continue with the permission, protocol/write-denial, sustained deployed register comparison, controlled recovery, repeated scenario and billing/availability evidence below when their prerequisites are satisfied. Do not inject outages or attack scenarios into the live warm-up merely to bypass the time gate.

## Permission proof

Use a dedicated approved test session for the reader/action-log identity, not the privileged deployment identity. Capture the identity object/client IDs and each response:

1. Query each permitted workspace; verify the expected utility-only normalized data.
2. Submit an action-log record with all common/action fields to that utility's separate action DCR; verify it arrives.
3. Attempt telemetry-DCR ingestion, analytics/watchlist/incident/resource edits and arbitrary storage writes; require explicit authorization denial, not merely absent data.
4. Verify no assignment grants PLC/HMI control, VM access or broader inherited Azure write rights.

The action writer is not attached to a VM by default. Do not attach it to the gateway, which has different ingestion rights, to manufacture a combined identity test.

## Network and process proof

From the actual gateway route, read the complete snapshot and attempt FC6/FC16 setpoint writes, actuator writes, forbidden function codes and force/debug access. Require protocol rejection and unchanged PLC outputs. From approved HMI/test routes, verify valid setpoint writes, invalid-value errors and correct physical response.

Verify loopback-only raw Modbus, unexposed admin/control endpoints, no public VM IPs, Premium private-only Bastion, removed build rules, no control/test NAT, the effective null Internet route, and boundary egress limited to documented services. Exercise real PLC outage/restart and ingestion failure/restart; prove queued edge events survive and do not inflate rule counts on replay.

Compare actual PLC and gateway values over the required one-hour interval with documented sampling/quantization tolerance. Accelerated model tests are not that live interval and are never seven-day warm-up evidence.

## Signoff

Keep raw result JSON, Azure query responses, identity/network probes, timestamps, cost observations and a case-by-case outcome against the supplied requirements. Record failures and not-applicable cases separately. A compiled template, Python mirror result, skipped runtime gate, empty query, closed incident or restored setpoint is not a substitute for the required evidence.
