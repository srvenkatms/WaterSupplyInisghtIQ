"""Baseline activity per data domain (FR-01, FR-04, FR-05).

baseline_minute() returns every record for one utility for one minute,
keyed by table name. Volumes follow the profile's per-day ranges, shaped by
working hours, and include legitimate unusual events such as on-call night
sign-ins and approved vendor sessions."""
from __future__ import annotations

from datetime import datetime, timedelta

from .rng import minute_rng, poisson

OT_PORTS = (502, 102, 20256, 44818, 5900)
NOISE_PORTS = (22, 23, 80, 443, 445, 1433, 3389, 8080, 8443)
OFFICE_PROCS = ("outlook.exe", "msedge.exe", "excel.exe", "winword.exe", "teams.exe")
SCADA_PROCS = ("hmi.exe", "historian-client.exe", "trend.exe")
COLLECTOR_HEARTBEAT = "CollectorHeartbeat"


def _iso(t: datetime) -> str:
    return t.isoformat()


def _day_rate(rng, lo_hi, weight: float) -> float:
    """Events per minute from a per-day range, scaled by a 0-1 time-of-day weight."""
    lo, hi = lo_hi
    per_day = lo + (hi - lo) * rng.random()
    return per_day / 1440 * weight * 3  # weight averages ~1/3 over a day


def _work_weight(t: datetime) -> float:
    if t.weekday() >= 5:
        return 0.15
    return 1.0 if 7 <= t.hour < 17 else 0.1


def on_call(op: dict, t: datetime) -> bool:
    sunday_zero = (t.weekday() + 1) % 7
    return sunday_zero in op["onCallDays"]


def is_night(t: datetime) -> bool:
    return t.hour >= 22 or t.hour < 6


def signin(p, t, upn, ip, result="0", location="US-Rural", app="Entra Private Access", user_type="Member", mfa="Success"):
    return {"TimeGenerated": _iso(t), "UtilityId": p["id"], "UserPrincipalName": upn, "UserType": user_type,
            "AppDisplayName": app, "ResultType": result, "IPAddress": ip, "Location": location,
            "DeviceId": "", "MfaResult": mfa if result == "0" else "", "RiskLevel": "none"}


def access(p, t, upn, ip, minutes, location="US-Rural", app="HMI"):
    return {"TimeGenerated": _iso(t), "UtilityId": p["id"], "UserPrincipalName": upn, "DestinationApp": app,
            "SourceIp": ip, "Location": location, "SessionStart": _iso(t),
            "SessionEnd": _iso(t + timedelta(minutes=minutes)), "Action": "Allowed"}


def firewall(p, t, action, src, dst, port, proto, src_zone, dst_zone, rule, activity="Traffic"):
    return {"TimeGenerated": _iso(t), "UtilityId": p["id"], "DeviceAction": action, "SourceIP": src,
            "DestinationIP": dst, "DestinationPort": port, "Protocol": proto, "SourceZone": src_zone,
            "DestinationZone": dst_zone, "RuleName": rule, "Activity": activity}


def endpoint(p, t, role, action, file_name, account, cmd="", alert="", severity=""):
    dev = p["devices"]["office"] if role == "Office" else p["devices"]["scada"]
    return {"TimeGenerated": _iso(t), "UtilityId": p["id"], "DeviceName": dev, "DeviceRole": role,
            "ActionType": action, "FileName": file_name, "ProcessCommandLine": cmd, "AccountName": account,
            "AlertTitle": alert, "Severity": severity}


def exposure_batch(p, t) -> list[dict]:
    """Weekly scan results: low-severity, non-OT findings only at baseline."""
    return [{"TimeGenerated": _iso(t), "UtilityId": p["id"], "Source": "CISA VS", "AssetIp": "203.0.113.5",
             "Port": 443, "Service": "https", "Product": "Billing portal", "Finding": "TLS 1.0 enabled",
             "Severity": "Low", "FirstSeen": _iso(t - timedelta(days=30)), "Status": "Existing"}]


def inventory_batch(p, t) -> list[dict]:
    rows = [{"AssetId": f"{p['id']}-plc-plant", "AssetType": "PLC", "SiteId": "plant", "Model": "OpenPLC" if p["processSource"] == "gateway" else "Simulated PLC",
             "Firmware": "1.0", "IpType": "Private"}]
    for s in p["sites"][1:]:
        rows.append({"AssetId": f"{p['id']}-{s}-rtu", "AssetType": "RTU", "SiteId": s, "Model": "RTU-100",
                     "Firmware": "2.4.1", "IpType": "Private"})
        rows.append({"AssetId": f"{p['id']}-{s}-modem", "AssetType": "CellularModem", "SiteId": s,
                     "Model": "LTE-200", "Firmware": "5.1", "IpType": "PrivateAPN"})
    for r in rows:
        r.update({"TimeGenerated": _iso(t), "UtilityId": p["id"], "LastSeen": _iso(t)})
    return rows


def baseline_minute(p: dict, t: datetime, bootstrap: bool = False) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    add = lambda table, rec: out.setdefault(table, []).append(rec)
    seed, uid, v = p["seed"], p["id"], p["volumes"]
    dev = p["devices"]

    if t.minute % 15 == 0:
        add("SimSignin_CL", signin(p, t, "", "", result=COLLECTOR_HEARTBEAT,
                                  location="", app="Collector", user_type=""))
        heartbeat = access(p, t, "", "", 0, location="", app="Collector")
        heartbeat["Action"] = COLLECTOR_HEARTBEAT
        add("SimAccessTraffic_CL", heartbeat)
        add("SimEndpoint_CL", endpoint(p, t, "SCADA", COLLECTOR_HEARTBEAT, "", ""))
        add("SimFirewall_CL", firewall(p, t, COLLECTOR_HEARTBEAT, "", "", 0, "", "", "",
                                      "collector-health", COLLECTOR_HEARTBEAT))

    # Identity: operators by day, the on-call operator occasionally at night.
    r = minute_rng(seed, uid, t, "signin")
    for op in p["operators"]:
        w = _work_weight(t) if not is_night(t) else (0.4 if on_call(op, t) else 0.0)
        for _ in range(poisson(r, _day_rate(r, v["signinsPerDay"], w) / len(p["operators"]) / 1.6)):
            ip = op["knownIps"][0]
            if r.random() < 0.05:  # occasional typo, never a burst (FR-05)
                add("SimSignin_CL", signin(p, t, op["upn"], ip, result="50126"))
            add("SimSignin_CL", signin(p, t, op["upn"], ip, location=op["location"]))
            if r.random() < 0.1:
                add("SimAccessTraffic_CL", access(p, t, op["upn"], ip, r.randint(5, 40), op["location"]))

    # Approved vendor sessions inside their access windows.
    for ven in p["vendors"]:
        sunday_zero = (t.weekday() + 1) % 7
        if sunday_zero in ven["days"] and ven["hours"][0] <= t.hour < ven["hours"][1]:
            rv = minute_rng(seed, uid, t, "vendor-" + ven["upn"])
            if rv.random() < 1 / 600:
                ip = ven["knownIps"][0]
                add("SimSignin_CL", signin(p, t, ven["upn"], ip, location=ven["location"], user_type="Guest"))
                add("SimAccessTraffic_CL", access(p, t, ven["upn"], ip, rv.randint(15, 90), ven["location"]))

    # Firewall: internet background noise, always denied, plus hourly summaries.
    rf = minute_rng(seed, uid, t, "fw")
    for _ in range(poisson(rf, _day_rate(rf, v["firewallDeniedPerDay"], 1 / 3))):
        src = f"203.0.113.{rf.randint(1, 254)}" if rf.random() < 0.5 else f"192.0.2.{rf.randint(100, 254)}"
        add("SimFirewall_CL", firewall(p, t, "Deny", src, "203.0.113.5", rf.choice(NOISE_PORTS), "TCP",
                                       "Internet", "Boundary", "default-deny-inbound"))
    if t.minute == 0:
        add("SimFirewall_CL", firewall(p, t, "Allow", dev["gatewayIp"], dev["plcIp"], 502, "TCP",
                                       "Boundary", "Control", "gw-to-plc", "Summary"))
        add("SimFirewall_CL", firewall(p, t, "Allow", dev["gatewayIp"], "AzureMonitor", 443, "TCP",
                                       "Boundary", "Internet", "gw-to-azure-monitor", "Summary"))
        add("SimFirewall_CL", firewall(p, t, "Allow", dev["officeIp"], "Internet", 443, "TCP",
                                       "Enterprise", "Internet", "office-web", "Summary"))

    # Endpoints: ordinary processes and logons; no alerts at baseline.
    re_ = minute_rng(seed, uid, t, "ep")
    for _ in range(poisson(re_, _day_rate(re_, v["endpointPerDay"], _work_weight(t)))):
        op = re_.choice(p["operators"])["upn"]
        if re_.random() < 0.7:
            add("SimEndpoint_CL", endpoint(p, t, "Office", "ProcessCreated", re_.choice(OFFICE_PROCS), op))
        else:
            add("SimEndpoint_CL", endpoint(p, t, "SCADA", "ProcessCreated", re_.choice(SCADA_PROCS), op))

    # First start: each operator signs in once, so identity data exists at once (TC-02).
    if bootstrap:
        add("SimFirewall_CL", firewall(p, t, "Allow", dev["gatewayIp"], dev["plcIp"], 502, "TCP",
                                       "Boundary", "Control", "gw-to-plc", "Summary"))
        for role in ("Office", "SCADA"):
            add("SimEndpoint_CL", endpoint(p, t, role, "LogonSuccess", "", p["operators"][0]["upn"]))
        for op in p["operators"]:
            add("SimSignin_CL", signin(p, t, op["upn"], op["knownIps"][0], location=op["location"]))
            add("SimAudit_CL", {"TimeGenerated": _iso(t), "UtilityId": uid, "OperationName": "Add user",
                               "InitiatedBy": "admin@lab.example", "TargetUser": op["upn"], "Result": "success"})
            if not is_night(t) or on_call(op, t):
                add("SimAccessTraffic_CL", access(p, t, op["upn"], op["knownIps"][0], 5, op["location"]))
        if p.get("otAlerts"):
            add("SimOtAlert_CL", {"TimeGenerated": _iso(t), "UtilityId": uid, "AlertType": "SensorOnline",
                                 "SourceIp": dev["scadaIp"], "DestinationIp": dev["plcIp"],
                                 "Protocol": "ModbusTCP", "Register": 0, "Value": 0.0, "Severity": "Informational"})

    # Weekly batches on Tuesday 06:00, and once at first start so every table has data (TC-02).
    if bootstrap or (t.weekday() == 1 and t.hour == 6 and t.minute == 0):
        out.setdefault("SimExposure_CL", []).extend(exposure_batch(p, t))
        out.setdefault("SimInventory_CL", []).extend(inventory_batch(p, t))
    return out
