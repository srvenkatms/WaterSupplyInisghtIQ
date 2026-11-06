import copy
from datetime import datetime, timedelta, timezone

import pytest

from swugen import engine, load_profile
from swugen.delivery import prepare, flush
from swugen.configuration import reference_data, deployment_profile

START = datetime(2026, 11, 2, 2, tzinfo=timezone.utc)


def test_reference_data_matches_each_utility():
    for uid in ("uta", "utb", "utc"):
        profile = load_profile(uid)
        lists = {row["alias"]: row["csv"] for row in reference_data(profile)}
        assert all(person["upn"] in lists["Roster"] for person in profile["operators"])
        assert all(person["upn"] in lists["Vendors"] for person in profile["vendors"])
        assert str(profile["safeLimits"]["TankLevelPct"][1]) in lists["SafeLimits"]
        sources = deployment_profile(profile)["healthSources"]
        assert any(source["Source"] == "SwuProcess" for source in sources) == (profile["processSource"] != "none")
        assert all(source["MaxSilenceMin"] == 60 for source in sources
                   if source["Source"] in {"SwuSignins", "SwuAccess", "SwuEndpoint", "SwuFirewall"})


def test_failed_delivery_preserves_checkpoint_and_unsent_batches():
    state = {"pending": {"SimSignin_CL": [{"UtilityId": "utb"}], "SimFirewall_CL": [{"UtilityId": "utb"}]}}
    saved = []

    class Sink:
        def send(self, uid, batches):
            if "SimFirewall_CL" in batches:
                raise ConnectionError("temporary ingestion outage")

    with pytest.raises(ConnectionError):
        flush("utb", state, Sink(), lambda value: saved.append(copy.deepcopy(value)))
    assert set(state["pending"]) == {"SimFirewall_CL"}
    assert set(saved[-1]["pending"]) == {"SimFirewall_CL"}
    received = []

    class HealthySink:
        def send(self, uid, batches):
            received.extend(batches)

    flush("utb", state, HealthySink(), lambda value: None)
    assert received == ["SimFirewall_CL"]
    assert state["pending"] == {}


def test_generation_must_not_advance_past_failed_pending_batch():
    profile = load_profile("utc")
    state = engine.new_state(profile, START)
    prepare(profile, state, START)
    checkpoint = state["lastMinute"]
    with pytest.raises(ValueError, match="Pending ingestion"):
        prepare(profile, state, START + timedelta(minutes=1))
    assert state["lastMinute"] == checkpoint


def test_scenario_provenance_and_restoration():
    profile = load_profile("utc")
    state = engine.new_state(profile, START)
    initial = copy.deepcopy(state["plant"])
    engine.tick(profile, state, START)
    engine.add_scenario(state, "SCN-08", "run-provenance", START)
    engine.add_scenario(state, "SCN-08", "run-provenance", START)
    assert len(state["scenarios"]) == 1
    changes = []
    for minute in range(1, 122):
        records = engine.tick(profile, state, START + timedelta(minutes=minute))
        changes.extend(row for row in records.get("SimProcess_CL", []) if row["EventType"] == "SetpointChange")
    assert changes and all(row["ScenarioRunId"] == "run-provenance" for row in changes)
    for tag in ("PumpLowSetpoint", "PumpHighSetpoint", "ClDoseSetpoint", "PumpOverride", "Mode"):
        assert state["plant"][tag] == initial[tag]
    assert not state["scenarios"]


def test_original_ot_probe_detection_accepts_denied_traffic():
    from rules_py import evaluate
    profile = load_profile("utc")
    row = {"TimeGenerated": START.isoformat(), "DeviceAction": "Deny", "DestinationPort": 502,
           "SourceIP": "192.0.2.3", "SourceZone": "Internet", "DestinationZone": "Boundary"}
    assert "DR-04" in evaluate(profile, {"SimFirewall_CL": [row]}, START, START + timedelta(minutes=1))
