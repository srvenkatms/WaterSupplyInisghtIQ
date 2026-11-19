"""Python mirror of the Sentinel rule logic, used to test generated data
offline. It follows the KQL in infra/modules/content.bicep; the KQL itself
is verified against a live workspace in TC-07."""
from datetime import datetime, timedelta

OT_PORTS = {502, 102, 20256, 44818, 5900}
REMOTE_TOOLS = {"teamviewer.exe", "anydesk.exe", "rustdesk.exe", "screenconnect.clientservice.exe",
                "srservice.exe", "vncserver.exe", "tvnserver.exe", "logmein.exe"}


def ts(r):
    return datetime.fromisoformat(r["TimeGenerated"])


def on_call_days(profile):
    return {o["upn"].lower(): set(o["onCallDays"]) for o in profile["operators"]}


def evaluate(profile, recs, window_start, window_end):
    """recs: table -> list of records (all history). Returns {rule: [records]} for
    records inside [window_start, window_end)."""
    fields = {"SimSignin_CL": "ResultType", "SimAccessTraffic_CL": "Action",
              "SimEndpoint_CL": "ActionType", "SimFirewall_CL": "Activity"}
    recs = {table: [r for r in rows if r.get(fields.get(table, "")) != "CollectorHeartbeat"]
            for table, rows in recs.items()}
    inw = lambda r: window_start <= ts(r) < window_end
    hits = {}
    hit = lambda rule, r: hits.setdefault(rule, []).append(r)
    oncall = on_call_days(profile)

    access = sorted(recs.get("SimAccessTraffic_CL", []), key=ts)
    for r in access:
        if not inw(r):
            continue
        t, upn = ts(r), r["UserPrincipalName"].lower()
        seen = any(a is not r and a["UserPrincipalName"].lower() == upn and a["SourceIp"] == r["SourceIp"]
                   and t - timedelta(days=14) <= ts(a) < t - timedelta(minutes=15) for a in access)
        night = t.hour >= 22 or t.hour < 6
        tonight = ((t.weekday() + 1) % 7) in oncall.get(upn, set())
        if (night and not tonight) or not seen:
            hit("DR-01", r)

    signins = sorted(recs.get("SimSignin_CL", []), key=ts)
    for r in signins:
        if not inw(r) or r["ResultType"] != "0":
            continue
        t = ts(r)
        fails = [s for s in signins if s["UserPrincipalName"] == r["UserPrincipalName"] and s["ResultType"] != "0"
                 and t - timedelta(minutes=20) <= ts(s) <= t]
        if len(fails) >= 20:
            hit("DR-02", r)

    for r in recs.get("SimExposure_CL", []):
        if inw(r) and r["Status"] == "New" and (r["Port"] in OT_PORTS or r["Severity"] in ("Medium", "High", "Critical")):
            hit("DR-03", r)

    for r in recs.get("SimFirewall_CL", []):
        if not inw(r):
            continue
        if r["DestinationPort"] in OT_PORTS and not r["SourceIP"].startswith("10.10."):
            hit("DR-04", r)
        if r["SourceZone"] == "Enterprise" and r["DestinationZone"] == "Control":
            hit("DR-10", r)

    for r in recs.get("SimOtAlert_CL", []):
        if inw(r) and r["AlertType"] in ("ModbusWrite", "ProgramDownload", "ModeChange"):
            hit("DR-05", r)

    for r in recs.get("SimProcess_CL", []):
        if inw(r) and r["EventType"] == "SetpointChange":
            hit("DR-06", r)    # no maintenance windows are open in the lab profile
        if inw(r) and r["EventType"] == "OutOfRange":
            hit("DR-07", r)

    audit = recs.get("SimAudit_CL", [])
    for r in signins:
        if not inw(r):
            continue
        ops = [a for a in audit if a["TargetUser"].lower() == r["UserPrincipalName"].lower() and ts(a) < ts(r)
               and ts(a) >= ts(r) - timedelta(days=90)
               and a["OperationName"] in ("Disable account", "Enable account")]
        if ops and max(ops, key=ts)["OperationName"] == "Disable account":
            hit("DR-08", r)

    for r in recs.get("SimEndpoint_CL", []):
        if not inw(r):
            continue
        if r["DeviceRole"] == "SCADA" and r["ActionType"] in ("SoftwareInstalled", "ProcessCreated") \
                and r["FileName"].lower() in REMOTE_TOOLS:
            hit("DR-09", r)
        if "ransomware" in r["AlertTitle"].lower():
            hit("DR-10", r)
    return hits
