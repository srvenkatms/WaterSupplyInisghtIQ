from datetime import datetime, timedelta, timezone

from swuharness import matches_evidence, observation, alert_observation
import argparse
import json
import pytest
import swuharness

START = datetime(2026, 11, 2, tzinfo=timezone.utc)


def test_unrelated_alert_does_not_pass_a_scenario():
    evidence = [{"Source": "SimProcess_CL", "TimeGenerated": START + timedelta(minutes=3),
                 "EventType": "SetpointChange", "Tag": "ClDoseSetpoint", "NewValue": 6.0, "ScenarioRunId": "run-123"}]
    alert = {"AlertName": "DR-06 Setpoint change", "StartTime": START, "EndTime": START + timedelta(minutes=10),
             "Entities": "", "Details": "TankHighHigh"}
    assert not matches_evidence(alert, evidence)
    alert["Details"] = '{"Tag": ["ClDoseSetpoint"], "NewValue": ["6"]}'
    assert matches_evidence(alert, evidence)
    alert["EndTime"] = START + timedelta(minutes=2)
    assert not matches_evidence(alert, evidence)


def test_early_grouped_incident_does_not_hide_late_rule_alert():
    incident = {"CreatedTime": START + timedelta(minutes=2), "Severity": "High", "IncidentNumber": 42}
    alert = {"TimeGenerated": START + timedelta(minutes=8), "EndTime": START + timedelta(minutes=7),
             "AlertSeverity": "High", "SystemAlertId": "synthetic-alert"}
    assert not alert_observation("DR-07", alert, incident, START, "SCN-08")["pass"]


def test_incident_escalation_does_not_hide_wrong_rule_alert_severity():
    incident = {"CreatedTime": START + timedelta(minutes=2), "Severity": "High", "IncidentNumber": 42}
    alert = {"TimeGenerated": START + timedelta(minutes=3), "EndTime": START + timedelta(minutes=2),
             "AlertSeverity": "Medium", "SystemAlertId": "synthetic-alert"}
    assert not alert_observation("DR-07", alert, incident, START, "SCN-08")["pass"]


def test_eventual_physical_effect_does_not_get_a_relaxed_latency_pass():
    incident = {"CreatedTime": START + timedelta(minutes=40), "Severity": "High", "IncidentNumber": 42}
    result = observation("DR-07", incident, START, "SCN-08")
    assert result["limitMin"] == 5
    assert result["latencyMin"] == 40
    assert not result["pass"]


def test_feed_loss_requires_high_severity_during_scn09():
    incident = {"CreatedTime": START + timedelta(minutes=10), "Severity": "Low", "IncidentNumber": 42}
    assert not observation("DR-11", incident, START, "SCN-09")["pass"]
    incident["Severity"] = "High"
    assert observation("DR-11", incident, START, "SCN-09")["pass"]


def test_partial_entity_names_do_not_match():
    evidence = [{"Source": "SimSignin_CL", "TimeGenerated": START,
                 "UserPrincipalName": "operator@uta.example"}]
    alert = {"AlertName": "DR-02 Password guessing", "StartTime": START, "EndTime": START,
             "Entities": '[{"FullName":"other-operator@uta.example"}]', "Details": "{}"}
    assert not matches_evidence(alert, evidence)


def test_negative_incident_latency_cannot_pass():
    incident = {"CreatedTime": START - timedelta(minutes=1), "Severity": "High", "IncidentNumber": 42}
    assert not observation("DR-07", incident, START, "SCN-08")["pass"]


def test_evidence_table_origin_does_not_collide_with_exposure_source(monkeypatch):
    def query(uid, kql, hours):
        assert uid == "uta" and hours == 4
        assert "withsource=EvidenceTable" in kql
        assert "extend Source = EvidenceTable" in kql
        assert "withsource=Source" not in kql
        return []

    monkeypatch.setattr(swuharness, "query", query)
    assert swuharness.evidence_for_run("uta", "run-123") == []


def test_live_warmup_cannot_pass_missing_receipts(monkeypatch):
    monkeypatch.setattr(swuharness, "outputs", lambda: {
        "warmupStartedAt": (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),
        "utilities": [{"utilityId": "uta"}],
    })
    monkeypatch.setattr(swuharness, "query", lambda *args, **kwargs: [{"MissingHours": 1}])
    with pytest.raises(RuntimeError, match="missing hourly"):
        swuharness.require_ready()


@pytest.mark.parametrize("started", [None, datetime.now(timezone.utc).isoformat()])
def test_infrastructure_age_cannot_substitute_for_verified_warmup(monkeypatch, started):
    manifest = {"deployedAt": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()}
    if started:
        manifest["warmupStartedAt"] = started
    monkeypatch.setattr(swuharness, "outputs", lambda: manifest)
    monkeypatch.setattr(swuharness, "query", lambda *args, **kwargs: pytest.fail("Premature acceptance queried data"))
    with pytest.raises(RuntimeError, match="warm-up"):
        swuharness.require_ready()


def test_cleanup_attempts_every_setpoint_and_metadata_clear(monkeypatch, tmp_path):
    monkeypatch.setattr(swuharness, "ROOT", tmp_path)
    monkeypatch.setattr(swuharness, "require_ready", lambda: None)
    snapshot = {"PumpLowSetpoint": 40, "PumpHighSetpoint": 90, "ClDoseSetpoint": 1.2,
                "PumpOverride": 0, "Mode": 0}
    monkeypatch.setattr(swuharness, "remote", lambda *args: json.dumps(snapshot))
    monkeypatch.setattr(swuharness, "run_injected", lambda *args: 0)
    restored, metadata = [], []

    def restore(tag, value, via_hmi):
        restored.append(tag)
        if tag == "PumpLowSetpoint":
            raise ConnectionError("Synthetic restoration failure")

    monkeypatch.setattr(swuharness, "act_on_plc", restore)
    monkeypatch.setattr(swuharness, "control_gateway", lambda run_id: metadata.append(run_id))
    with pytest.raises(ExceptionGroup, match="restoration"):
        swuharness.cmd_run(argparse.Namespace(utility="uta", scenario="SCN-01"))
    assert restored == list(snapshot)
    assert metadata[-1] == ""
