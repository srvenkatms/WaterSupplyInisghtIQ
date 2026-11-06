"""Edge filter (FR-17, FR-28): turns a stream of process values into the few
events the edge gateway sends. Shared by the generator's process model and
the real gateway container, so both emit identical event shapes."""
from __future__ import annotations

from datetime import datetime

SETPOINT_TAGS = ("PumpLowSetpoint", "PumpHighSetpoint", "ClDoseSetpoint", "PumpOverride")
LIMITED_TAGS = ("TankLevelPct", "ClResidual")
HEARTBEAT_TAGS = ("TankLevelPct", "ClResidual", "PumpState", "ClDoseSetpoint")
UNITS = {"TankLevelPct": "percent", "ClResidual": "mg/L", "ClDoseSetpoint": "mg/L",
         "PumpLowSetpoint": "percent", "PumpHighSetpoint": "percent", "PumpState": "state",
         "PumpOverride": "state", "Mode": "state"}
ALARMS = {"TankHighHigh": ("TankLevelPct", ">=", 97.0), "TankLowLow": ("TankLevelPct", "<=", 8.0)}
HEARTBEAT_SECONDS = 15 * 60


class EdgeFilter:
    def __init__(self, site_id: str, safe_limits: dict, state: dict | None = None):
        self.site = site_id
        self.limits = safe_limits
        st = state or {}
        self.last = st.get("last", {})            # last seen value per tag
        self.out_of_range = st.get("oor", {})     # tag -> bool
        self.alarm_active = st.get("alarms", {})  # alarm -> bool
        self.last_heartbeat = st.get("hb")        # ISO time of last heartbeat

    def state(self) -> dict:
        return {"last": self.last, "oor": self.out_of_range, "alarms": self.alarm_active, "hb": self.last_heartbeat}

    def _event(self, now: datetime, tag: str, kind: str, old=None, new=None, by: str = "", limit=None) -> dict:
        return {"TimeGenerated": now.isoformat(), "SiteId": self.site, "Tag": tag, "EventType": kind,
                "OldValue": old, "NewValue": new, "Unit": UNITS.get(tag, ""), "ChangedBy": by, "Limit": limit}

    def observe(self, now: datetime, values: dict, changed_by: str = "") -> list[dict]:
        """values: tag -> number. changed_by is known only in the generator;
        a real PLC does not record who changed a value (FR-31)."""
        events = []
        for tag in SETPOINT_TAGS:
            if tag in values and tag in self.last and round(values[tag], 3) != round(self.last[tag], 3):
                events.append(self._event(now, tag, "SetpointChange", self.last[tag], values[tag], changed_by))
        if "Mode" in values and "Mode" in self.last and values["Mode"] != self.last["Mode"]:
            events.append(self._event(now, "Mode", "ModeChange", self.last["Mode"], values["Mode"], changed_by))
        for tag in LIMITED_TAGS:
            if tag not in values or tag not in self.limits:
                continue
            lo, hi = self.limits[tag]
            v, was = values[tag], self.out_of_range.get(tag, False)
            now_out = v < lo or v > hi
            if now_out and not was:
                events.append(self._event(now, tag, "OutOfRange", self.last.get(tag), round(v, 3), limit=hi if v > hi else lo))
            elif was and not now_out:
                events.append(self._event(now, tag, "Recovered", self.last.get(tag), round(v, 3)))
            self.out_of_range[tag] = now_out
        for name, (tag, op, threshold) in ALARMS.items():
            if tag not in values:
                continue
            active = values[tag] >= threshold if op == ">=" else values[tag] <= threshold
            if active and not self.alarm_active.get(name, False):
                events.append(self._event(now, name, "Alarm", None, round(values[tag], 3), limit=threshold))
            self.alarm_active[name] = active
        hb_due = self.last_heartbeat is None or (now - datetime.fromisoformat(self.last_heartbeat)).total_seconds() >= HEARTBEAT_SECONDS
        if hb_due:
            for tag in HEARTBEAT_TAGS:
                if tag in values:
                    events.append(self._event(now, tag, "Heartbeat", None, round(values[tag], 3)))
            self.last_heartbeat = now.isoformat()
        self.last.update({k: v for k, v in values.items()})
        return events

    def unreachable(self, now: datetime) -> dict:
        return self._event(now, "PLC", "PlcUnreachable")

    def plc_recovered(self, now: datetime) -> dict:
        return self._event(now, "PLC", "Recovered")
