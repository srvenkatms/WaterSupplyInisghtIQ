"""Validated utility profiles and deterministic infrastructure reference data."""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
from pathlib import Path
from datetime import datetime, date

REMOTE_TOOLS = {
    "teamviewer.exe": "TeamViewer", "anydesk.exe": "AnyDesk", "rustdesk.exe": "RustDesk",
    "screenconnect.clientservice.exe": "ScreenConnect", "srservice.exe": "Splashtop",
    "vncserver.exe": "VNC", "tvnserver.exe": "TightVNC", "logmein.exe": "LogMeIn",
}
INTERVALS = {"Firewall": 60, "Process": 30, "Endpoint": 60, "Signin": 60,
             "Access": 60, "Exposure": 11520, "Inventory": 11520}
HEALTH_FEEDS = {"SwuSignins" if name == "Signin" else f"Swu{name}" for name in INTERVALS}
COLLECTOR_FIELDS = {"SwuSignins": "ResultType", "SwuAccess": "Action",
                    "SwuEndpoint": "ActionType", "SwuFirewall": "Activity"}
SOURCE_HEALTH_UNION = Path(__file__).with_name("source_health.kql").read_text(encoding="utf-8").strip()


def validate_profile(profile: dict) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9]{1,15}", profile["id"]):
        raise ValueError("Invalid utility ID")
    if profile["tier"] not in {"baseline", "process", "otvisibility"}:
        raise ValueError("Unknown collection tier")
    if profile["processSource"] not in {"none", "model", "gateway"}:
        raise ValueError("Unknown process source")
    if profile["processSource"] == "gateway" and profile["id"] != "uta":
        raise ValueError("Only Utility A is wired to the real gateway deployment")
    if (profile["tier"] == "baseline") != (profile["processSource"] == "none"):
        raise ValueError("Baseline utilities must have no process collector; other tiers require one")
    if not profile["operators"] or not profile["vendors"]:
        raise ValueError("Every utility needs an operator and vendor")
    for person in profile["operators"] + profile["vendors"] + [profile["leaver"]]:
        if not person["upn"].endswith(".example"):
            raise ValueError("Only synthetic .example identities are permitted")
    required_limits = {"TankLevelPct", "ClResidual", "ClDoseSetpoint", "PumpLowSetpoint", "PumpHighSetpoint"}
    if not required_limits <= profile["safeLimits"].keys():
        raise ValueError("Required process safe limits are missing")
    for tag, bounds in profile["safeLimits"].items():
        if len(bounds) != 2 or not all(type(v) in (int, float) and math.isfinite(v) for v in bounds) or bounds[0] >= bounds[1]:
            raise ValueError(f"Invalid safe limits for {tag}")
    for person in profile["operators"]:
        if not person["onCallDays"] or any(day not in range(7) for day in person["onCallDays"]):
            raise ValueError("Invalid on-call schedule")
    for vendor in profile["vendors"]:
        if any(day not in range(7) for day in vendor["days"]) or not 0 <= vendor["hours"][0] < vendor["hours"][1] <= 24:
            raise ValueError("Invalid vendor access window")
    plant = profile["plant"]
    for field in ("pumpLowSetpoint", "pumpHighSetpoint", "clDoseSetpoint", "fillRatePctPerMin",
                  "baseDemandPctPerMin", "peakDemandPctPerMin", "residualGain", "residualTauMin", "initialLevelPct"):
        value = plant[field]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid plant parameter: {field}")
    if plant["residualTauMin"] <= 0 or plant["fillRatePctPerMin"] <= plant["peakDemandPctPerMin"]:
        raise ValueError("The baseline plant must have a positive response time and sufficient pump capacity")
    if plant["pumpLowSetpoint"] >= plant["pumpHighSetpoint"]:
        raise ValueError("Low pump setpoint must be below high pump setpoint")
    for tag, value in (("TankLevelPct", plant["initialLevelPct"]),
                       ("ClResidual", plant["clDoseSetpoint"] * plant["residualGain"]),
                       ("ClDoseSetpoint", plant["clDoseSetpoint"]),
                       ("PumpLowSetpoint", plant["pumpLowSetpoint"]), ("PumpHighSetpoint", plant["pumpHighSetpoint"])):
        lower, upper = profile["safeLimits"][tag]
        if not lower <= value <= upper or not 0 <= value <= 655.35:
            raise ValueError(f"Unsafe baseline value: {tag}")
    for window in profile.get("maintenanceWindows", []):
        start, end = datetime.fromisoformat(window["start"]), datetime.fromisoformat(window["end"])
        if start.tzinfo is None or end.tzinfo is None or end <= start or not window["description"]:
            raise ValueError("Maintenance windows require ordered, timezone-aware timestamps and a description")
    for backup in profile.get("backups", []):
        date.fromisoformat(backup["lastBackup"])
        if not backup["asset"]:
            raise ValueError("Backup record needs an asset")


def csv_text(headers: list[str], rows: list[list]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue()


def reference_data(profile: dict) -> list[dict]:
    def watchlist(alias, key, headers, rows):
        return {"alias": alias, "key": key, "csv": csv_text(headers, rows), "hasItems": bool(rows)}
    return [
        watchlist("Roster", "UserPrincipalName", ["UserPrincipalName", "Role", "OnCallDays"],
                  [[p["upn"], "Operator", " ".join(map(str, p["onCallDays"]))] for p in profile["operators"]]),
        watchlist("Vendors", "UserPrincipalName", ["UserPrincipalName", "Vendor", "Days", "StartHour", "EndHour"],
                  [[p["upn"], p["vendor"], " ".join(map(str, p["days"])), *p["hours"]] for p in profile["vendors"]]),
        watchlist("SafeLimits", "Tag", ["Tag", "Min", "Max"],
                  [[tag, *bounds] for tag, bounds in sorted(profile["safeLimits"].items())]),
        watchlist("MaintenanceWindows", "Start", ["Start", "End", "Description"],
                  [[w["start"], w["end"], w["description"]] for w in profile.get("maintenanceWindows", [])]),
        watchlist("Backups", "Asset", ["Asset", "LastBackup"],
                  [[b["asset"], b["lastBackup"]] for b in profile.get("backups", [])]),
        watchlist("RemoteTools", "FileName", ["FileName", "Product"], list(map(list, REMOTE_TOOLS.items()))),
        watchlist("DisabledAccounts", "UserPrincipalName", ["UserPrincipalName", "DisabledAt", "UtilityId"], []),
        watchlist("SourceReceipts", "Source", ["Source", "LastSeen"], []),
        watchlist("AccessHistory", "Upn", ["Upn", "SourceIp", "Location", "SeenAt"], []),
        watchlist("IncidentHistory", "IncidentNumber", ["IncidentNumber", "ChangedAt", "Severity", "Status"], []),
    ]


def deployment_profile(profile: dict) -> dict:
    validate_profile(profile)
    health = [{"Source": f"Swu{name}", "MaxSilenceMin": interval} for name, interval in INTERVALS.items()
              if name != "Process" or profile["processSource"] != "none"]
    for row in health:
        row["Source"] = {"SwuSignin": "SwuSignins", "SwuAccess": "SwuAccess"}.get(row["Source"], row["Source"])
    return {"id": profile["id"], "name": profile["name"], "tier": profile["tier"],
            "processSource": profile["processSource"], "watchlists": reference_data(profile),
            "healthSources": health}


def main() -> None:
    from . import load_profiles
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[2] / "infra" / "utility-config.json")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = json.dumps([deployment_profile(p) for p in load_profiles().values()], indent=2) + "\n"
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != content:
            raise SystemExit("Infrastructure profiles are stale: run python -m swugen.configuration")
    else:
        args.output.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
