"""Edge gateway (Purdue level 3.5, FR-27 to FR-29).

Polls the PLC with read-only Modbus requests, runs the shared edge filter and
pushes only changes, exceptions and heartbeats to Utility A's workspace with
the VM's managed identity. The client class exposes reads only, so the
gateway has no code path that can write to the PLC (TC-16)."""
from __future__ import annotations

import json
import os
import time
import logging
import hashlib
import copy
import math
from pathlib import Path
from datetime import datetime, timedelta, timezone

from pymodbus.client import ModbusTcpClient

from swugen import load_profile
from swugen.edgefilter import EdgeFilter
from .registers import READ_COUNT, READ_START, from_block

UNREACHABLE_AFTER = 3                    # failed polls before PlcUnreachable (FR-29)
UNREACHABLE_REPEAT = timedelta(minutes=5)
TAGS = ("TankLevelPct", "ClResidual", "PumpState", "PumpLowSetpoint", "PumpHighSetpoint",
        "ClDoseSetpoint", "PumpOverride", "Mode")


class ReadOnlyPlc:
    """Wraps the Modbus client and exposes function code 3 only."""

    def __init__(self, host: str, port: int):
        self._c = ModbusTcpClient(host, port=port, timeout=2, retries=0)

    def read_block(self) -> list[int]:
        if not self._c.connected and not self._c.connect():
            raise ConnectionError("PLC not reachable")
        rr = self._c.read_holding_registers(READ_START, count=READ_COUNT)
        if rr.isError():
            raise ConnectionError(str(rr))
        return rr.registers

    def close(self) -> None:
        self._c.close()


class StdoutSink:
    def send(self, utility_id, batches):
        for recs in batches.values():
            for r in recs:
                print(json.dumps(r), flush=True)


class Gateway:
    def __init__(self, plc, sink, utility_id: str, safe_limits: dict, site: str = "plant", state_dir: Path | None = None):
        self.plc, self.sink, self.uid = plc, sink, utility_id
        self.failures, self.last_unreachable = 0, None
        self.state_dir = state_dir
        self.spool = None
        if state_dir:
            from .spool import EventSpool
            self.spool = EventSpool(state_dir / "outbox.sqlite")
        self.filter = EdgeFilter(site, safe_limits)
        if self.spool and (state := self.spool.state()) is not None:
            self._restore(state)

    def _checkpoint(self) -> dict:
        return self.filter.state() | {"plcFailures": self.failures,
                                      "plcLastUnreachable": self.last_unreachable.isoformat() if self.last_unreachable else None}

    def _restore(self, state: dict) -> None:
        self.filter = EdgeFilter(self.filter.site, self.filter.limits, state)
        self.failures = state.get("plcFailures", 0)
        at = state.get("plcLastUnreachable")
        self.last_unreachable = datetime.fromisoformat(at) if at else None

    def _send(self, events: list[dict], previous: dict | None = None) -> None:
        try:
            control = {}
            if self.state_dir and (self.state_dir / "control.json").exists():
                control = json.loads((self.state_dir / "control.json").read_text(encoding="utf-8"))
            until = control.get("suppressUntil")
            suppressed = until and datetime.now(timezone.utc) < datetime.fromisoformat(until)
            if suppressed:
                logging.warning("SCN-09 intentionally suppressing Utility A process feed")
                events = []
            if events:
                for e in events:
                    e.update({"UtilityId": self.uid, "ScenarioRunId": control.get("runId", "")})
                    e["RecordId"] = hashlib.sha256(json.dumps(e, sort_keys=True).encode()).hexdigest()
                if not self.spool:
                    self.sink.send(self.uid, {"SimProcess_CL": events})
            if self.spool:
                self.spool.append(events, self._checkpoint())
        except Exception:
            if previous is not None:
                self._restore(previous)
            raise
        if suppressed:
            return
        if self.spool:
            self.spool.flush(self.uid, self.sink)

    def poll(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("Gateway poll timestamp must include its UTC offset")
        previous = copy.deepcopy(self._checkpoint())
        try:
            values = from_block(self.plc.read_block())
        except (ConnectionError, OSError, ValueError):
            logging.exception("Gateway PLC poll failed")
            self.failures += 1
            self.plc.close()
            due = self.last_unreachable is None or now - self.last_unreachable >= UNREACHABLE_REPEAT
            if self.failures >= UNREACHABLE_AFTER and due:
                self.last_unreachable = now
                ev = [self.filter.unreachable(now)]
                self._send(ev, previous)
                return ev
            self._send([], previous)
            return []
        recovered = self.last_unreachable is not None
        self.failures, self.last_unreachable = 0, None
        events = self.filter.observe(now, {t: values[t] for t in TAGS})   # ChangedBy stays empty (FR-31)
        if recovered:
            events.insert(0, self.filter.plc_recovered(now))
        self._send(events, previous)
        return events


def main() -> None:
    uid = os.environ.get("SWU_UTILITY", "uta")
    p = load_profile(uid)
    plc = ReadOnlyPlc(os.environ.get("SWU_PLC_HOST", "10.10.1.10"), int(os.environ.get("SWU_MODBUS_PORT", "502")))
    if os.environ.get("SWU_DRY_RUN") == "1":
        sink = StdoutSink()
    else:
        from azure.identity import ManagedIdentityCredential
        from swugen.sink import IngestionSink
        sink = IngestionSink(os.environ["SWU_DCE_ENDPOINT"],
                             [{"utilityId": uid, "dcrImmutableId": os.environ["SWU_DCR_IMMUTABLE_ID"]}],
                             credential=ManagedIdentityCredential())
    if os.environ.get("SWU_DRY_RUN") == "1":
        directory = Path(os.environ["SWU_STATE_DIR"]) if os.environ.get("SWU_STATE_DIR") else None
    else:
        directory = Path(os.environ["SWU_STATE_DIR"])
    gw = Gateway(plc, sink, uid, p["safeLimits"], state_dir=directory)
    interval = float(os.environ.get("SWU_POLL_SECONDS", "5"))
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("Polling interval must be finite and positive")
    while True:
        try:
            gw.poll()
        except Exception:   # ingestion errors must not stop polling
            logging.exception("Gateway ingestion or checkpoint failed")
        time.sleep(interval)


if __name__ == "__main__":
    main()
