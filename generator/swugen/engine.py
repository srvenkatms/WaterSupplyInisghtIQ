"""Generator engine: advances one utility minute by minute, merging baseline
records, process model events and active scenario steps.

State is a plain dict so it can be stored anywhere (a blob in Azure, a file
locally). The engine itself does no I/O."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import copy
import hashlib
import json

from . import baseline, scenarios
from .edgefilter import EdgeFilter
from .model import PlantModel

MAX_CATCHUP_MINUTES = 60  # after downtime, do not flood: skip older minutes


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(second=0, microsecond=0)


def new_state(profile: dict, now: datetime) -> dict:
    return {"lastMinute": (now - timedelta(minutes=1)).isoformat(), "bootstrapped": False,
            "plant": PlantModel(profile["plant"]).to_dict(), "edge": None,
            "scenarios": [], "suppress": {}}


def add_scenario(state: dict, name: str, run_id: str, start: datetime) -> None:
    if name not in scenarios.EXPECTED:
        raise ValueError(f"Unknown scenario {name}")
    if not run_id or start.tzinfo is None:
        raise ValueError("Scenario needs a run ID and timezone-aware start")
    admitted = state.setdefault("admittedRuns", {})
    if run_id in admitted:
        return
    if state["scenarios"]:
        raise ValueError("Overlapping scenarios are not supported; finish and restore the active run first")
    state["scenarios"].append({"name": name, "runId": run_id, "start": start.isoformat(), "cursor": -1})
    admitted[run_id] = start.isoformat()
    state["restorePlant"] = copy.deepcopy(state["plant"])


def _stamp(rec: dict, profile: dict, run_id: str) -> dict:
    rec.setdefault("UtilityId", profile["id"])
    rec["ScenarioRunId"] = run_id
    return rec


def tick(profile: dict, state: dict, now: datetime, acceleration: int = 1) -> dict[str, list[dict]]:
    """Generate everything for the minutes after state['lastMinute'] up to now.
    acceleration > 1 runs that many simulated plant minutes per real minute (FR-07)."""
    out: dict[str, list[dict]] = {}
    if not 1 <= acceleration <= 60:
        raise ValueError("Time acceleration must be between 1 and 60")
    sequences = {}

    def emit(table, rec):
        key = (table, rec["TimeGenerated"], rec["ScenarioRunId"])
        ordinal = sequences.get(key, 0)
        sequences[key] = ordinal + 1
        identity = json.dumps([table, ordinal, rec], sort_keys=True)
        rec["RecordId"] = hashlib.sha256(identity.encode()).hexdigest()
        out.setdefault(table, []).append(rec)
    last = datetime.fromisoformat(state["lastMinute"])
    first = max(last + timedelta(minutes=1), now - timedelta(minutes=MAX_CATCHUP_MINUTES - 1))
    model = PlantModel.from_dict(profile["plant"], state["plant"])
    edge = EdgeFilter("plant", profile["safeLimits"], state["edge"])
    use_model = profile["processSource"] == "model"

    minute = first
    while minute <= now:
        window_end = minute + timedelta(minutes=1)
        # 1. Baseline records.
        for table, recs in baseline.baseline_minute(profile, minute, bootstrap=not state["bootstrapped"]).items():
            for r in recs:
                emit(table, _stamp(r, profile, ""))
        state["bootstrapped"] = True

        # 2. Scenario steps due in this minute.
        actor, process_run_id = "", ""
        for sc in state["scenarios"]:
            start = datetime.fromisoformat(sc["start"])
            process_run_id = sc["runId"]
            if minute >= start + timedelta(hours=2):
                previous = state.pop("restorePlant", None)
                if previous:
                    for tag in ("PumpLowSetpoint", "PumpHighSetpoint", "ClDoseSetpoint", "PumpOverride", "Mode"):
                        setattr(model.s, tag, previous[tag])
                continue
            for index, step in enumerate(scenarios.build(sc["name"], profile, start)):
                at = start + timedelta(seconds=step[0])
                if index <= sc.get("cursor", -1) or at >= window_end or at > now:
                    continue
                sc["cursor"] = index
                if step[1] == "record":
                    emit(step[2], _stamp(dict(step[3]), profile, sc["runId"]))
                elif step[1] == "set" and use_model:   # gateway utilities: harness acts on the real PLC
                    setattr(model.s, step[2], step[3])
                    actor = step[4]
                elif step[1] == "suppress":
                    for table in step[2]:
                        state["suppress"][table] = (at + timedelta(seconds=step[3])).isoformat()

        # 3. Process model and edge filter.
        if use_model:
            for k in range(acceleration):
                sim_minute = minute.hour * 60 + minute.minute + k / acceleration
                model.step(sim_minute % 1440, dt_min=1.0)
            values = {k: getattr(model.s, k) for k in ("TankLevelPct", "ClResidual", "PumpState", "PumpLowSetpoint",
                                                       "PumpHighSetpoint", "ClDoseSetpoint", "PumpOverride", "Mode")}
            for ev in edge.observe(minute, values, changed_by=actor):
                emit("SimProcess_CL", _stamp(ev, profile, process_run_id))
        minute = window_end

    # 4. Retire finished scenarios (longest scenario is under 2 hours).
    state["scenarios"] = [s for s in state["scenarios"]
                          if now - datetime.fromisoformat(s["start"]) < timedelta(hours=2)]
    state["admittedRuns"] = {run: at for run, at in state.get("admittedRuns", {}).items()
                             if now - datetime.fromisoformat(at) < timedelta(days=90)}
    # 5. Feed cuts (SCN-09): drop suppressed tables until their end time.
    for table, until in list(state["suppress"].items()):
        if now < datetime.fromisoformat(until):
            out.pop(table, None)
        else:
            del state["suppress"][table]

    state["lastMinute"] = now.isoformat()
    state["plant"] = model.to_dict()
    state["edge"] = edge.state()
    return out
