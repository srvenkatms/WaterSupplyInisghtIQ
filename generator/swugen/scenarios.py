"""Attack and control scenarios SCN-01 to SCN-10 (FR-21 to FR-24).

Each scenario is a list of steps at offsets (seconds) from its start:
  ("record", table, fields)        a synthetic record to inject
  ("set", tag, value, actor)       a process setpoint change (model or act mode)
  ("suppress", [tables], seconds)  stop writing these tables (feed cut)
EXPECTED lists the rules each scenario should trigger, by utility tier."""
from __future__ import annotations

from datetime import datetime, timedelta

from . import baseline as b

OTHER = "198.51.100.77"     # never seen before for any lab user
ATTACKER = "203.0.113.50"


def _pick_not_on_call(p: dict, t: datetime) -> dict:
    for op in p["operators"]:
        if not b.on_call(op, t):
            return op
    return p["operators"][0]


def _pick_on_call(p: dict, t: datetime) -> dict:
    for op in p["operators"]:
        if b.on_call(op, t):
            return op
    return p["operators"][0]


def build(name: str, p: dict, start: datetime) -> list[tuple]:
    d = p["devices"]
    t = lambda s: start + timedelta(seconds=s)
    steps: list[tuple] = []
    rec = lambda off, table, fields: steps.append((off, "record", table, fields))

    if name in ("SCN-01", "SCN-09"):
        op = _pick_not_on_call(p, start)
        rec(0, "SimSignin_CL", b.signin(p, t(0), op["upn"], OTHER, location="Unknown"))
        rec(60, "SimAccessTraffic_CL", b.access(p, t(60), op["upn"], OTHER, 30, location="Unknown"))
        steps.append((180, "set", "ClDoseSetpoint", p["plant"]["clDoseSetpoint"] * 10, op["upn"]))
        if name == "SCN-09":
            steps.append((300, "suppress", ["SimFirewall_CL", "SimProcess_CL"], 90 * 60))
    elif name == "SCN-02":
        ven = p["vendors"][0]
        for i in range(40):
            rec(i * 15, "SimSignin_CL", b.signin(p, t(i * 15), ven["upn"], ATTACKER, result="50126",
                                                 location="Unknown", user_type="Guest"))
        rec(615, "SimSignin_CL", b.signin(p, t(615), ven["upn"], ATTACKER, location="Unknown", user_type="Guest"))
    elif name == "SCN-03":
        rec(0, "SimExposure_CL", {"TimeGenerated": t(0).isoformat(), "UtilityId": p["id"], "Source": "Exposure Assessor",
                                  "AssetIp": "203.0.113.10", "Port": 502, "Service": "modbus",
                                  "Product": "PLC with HMI", "Finding": "Industrial protocol reachable from internet",
                                  "Severity": "High", "FirstSeen": t(0).isoformat(), "Status": "New"})
        for i in range(5):
            rec(120 + i * 30, "SimFirewall_CL", b.firewall(p, t(120 + i * 30), "Allow", "192.0.2.66", d["plcIp"],
                                                           502, "TCP", "Internet", "Control", "port-forward-502"))
    elif name == "SCN-04":
        if p.get("otAlerts"):
            rec(0, "SimOtAlert_CL", {"TimeGenerated": t(0).isoformat(), "UtilityId": p["id"], "AlertType": "ModbusWrite",
                                     "SourceIp": d["scadaIp"], "DestinationIp": d["plcIp"], "Protocol": "ModbusTCP",
                                     "Register": 11, "Value": 99.0, "Severity": "High"})
        steps.append((5, "set", "PumpHighSetpoint", 99.0, ""))  # a real PLC does not record who
    elif name == "SCN-05":
        upn = p["leaver"]["upn"]
        rec(0, "SimAudit_CL", {"TimeGenerated": t(0).isoformat(), "UtilityId": p["id"], "OperationName": "Disable account",
                               "InitiatedBy": "admin@lab.example", "TargetUser": upn, "Result": "success"})
        for i in range(3):
            rec(300 + i * 60, "SimSignin_CL", b.signin(p, t(300 + i * 60), upn, "192.0.2.90", result="50057", location="US-Rural"))
    elif name == "SCN-06":
        acct = p["operators"][0]["upn"]
        rec(0, "SimEndpoint_CL", b.endpoint(p, t(0), "SCADA", "SoftwareInstalled", "AnyDesk.exe", acct, "AnyDesk.exe --install"))
        rec(60, "SimEndpoint_CL", b.endpoint(p, t(60), "SCADA", "ProcessCreated", "AnyDesk.exe", acct, "AnyDesk.exe --service"))
        rec(90, "SimFirewall_CL", b.firewall(p, t(90), "Deny", d["scadaIp"], "203.0.113.80", 443, "TCP",
                                             "Control", "Internet", "control-deny-internet"))
    elif name == "SCN-07":
        acct = p["operators"][0]["upn"]
        rec(0, "SimEndpoint_CL", b.endpoint(p, t(0), "Office", "ProcessCreated", "vssadmin.exe", acct,
                                            "vssadmin.exe delete shadows /all /quiet"))
        rec(30, "SimEndpoint_CL", b.endpoint(p, t(30), "Office", "AlertRaised", "", acct, "",
                                             "Ransomware behavior detected", "High"))
        for i in range(6):
            rec(60 + i * 10, "SimFirewall_CL", b.firewall(p, t(60 + i * 10), "Deny", d["officeIp"], d["scadaIp"], 445,
                                                          "TCP", "Enterprise", "Control", "zone-default-deny"))
    elif name == "SCN-08":
        ven = p["vendors"][0]
        rec(0, "SimAccessTraffic_CL", b.access(p, t(0), ven["upn"], OTHER, 45, location="Unknown"))
        steps.append((60, "set", "PumpHighSetpoint", 110.0, ven["upn"]))
        steps.append((61, "set", "PumpOverride", 1, ven["upn"]))
    elif name == "SCN-10":
        op = _pick_on_call(p, start)
        rec(0, "SimSignin_CL", b.signin(p, t(0), op["upn"], op["knownIps"][0], location=op["location"]))
        rec(30, "SimAccessTraffic_CL", b.access(p, t(30), op["upn"], op["knownIps"][0], 10, op["location"]))
        rec(60, "SimProcess_CL", {"TimeGenerated": t(60).isoformat(), "SiteId": "remote-1", "Tag": "LowFlowWell1",
                                  "EventType": "Alarm", "OldValue": None, "NewValue": 0.0, "Unit": "state",
                                  "ChangedBy": "", "Limit": None})
        rec(240, "SimProcess_CL", {"TimeGenerated": t(240).isoformat(), "SiteId": "remote-1", "Tag": "LowFlowWell1",
                                   "EventType": "AlarmAck", "OldValue": None, "NewValue": 0.0, "Unit": "state",
                                   "ChangedBy": op["upn"], "Limit": None})
    else:
        raise ValueError(f"Unknown scenario {name}")
    for s in steps:
        if s[1] == "record":
            s[3].setdefault("UtilityId", p["id"])
    return sorted(steps, key=lambda s: s[0])


# Rules each scenario must trigger (primary, within NFR-01) and may trigger later
# (eventual, as the process responds), per tier. Empty means "no High incident".
EXPECTED = {
    "SCN-01": {"primary": ["DR-01", "DR-06"], "eventual": ["DR-07"], "tiers": ["process", "otvisibility", "baseline"]},
    "SCN-02": {"primary": ["DR-02"], "eventual": [], "tiers": ["process", "otvisibility", "baseline"]},
    "SCN-03": {"primary": ["DR-03", "DR-04"], "eventual": [], "tiers": ["process", "otvisibility", "baseline"]},
    "SCN-04": {"primary": ["DR-06"], "eventual": [], "tiers": ["process", "otvisibility"]},
    "SCN-05": {"primary": ["DR-08"], "eventual": [], "tiers": ["process", "otvisibility", "baseline"]},
    "SCN-06": {"primary": ["DR-09"], "eventual": [], "tiers": ["process", "otvisibility", "baseline"]},
    "SCN-07": {"primary": ["DR-10"], "eventual": [], "tiers": ["process", "otvisibility", "baseline"]},
    "SCN-08": {"primary": ["DR-01", "DR-06"], "eventual": ["DR-07"], "tiers": ["process", "otvisibility"]},
    "SCN-09": {"primary": ["DR-01", "DR-06"], "eventual": ["DR-11"], "tiers": ["process", "otvisibility"]},
    "SCN-10": {"primary": [], "eventual": [], "tiers": ["process", "otvisibility", "baseline"]},
}


def expected_rules(name: str, profile: dict) -> dict:
    e = dict(EXPECTED[name])
    primary, eventual = list(e["primary"]), list(e["eventual"])
    if profile["processSource"] == "none":   # no process data: setpoint rules cannot fire
        primary = [r for r in primary if r not in ("DR-06",)]
        eventual = [r for r in eventual if r not in ("DR-07", "DR-11")]
    if name == "SCN-04" and profile.get("otAlerts"):
        primary.append("DR-05")
    return {"primary": primary, "eventual": eventual, "applicable": profile["tier"] in e["tiers"]}
