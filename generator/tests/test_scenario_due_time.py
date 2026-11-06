from datetime import datetime, timedelta, timezone

from swugen import engine, load_profile, scenarios


def test_timer_does_not_apply_future_scenario_steps(monkeypatch):
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    record = {"TimeGenerated": (now + timedelta(seconds=30)).isoformat()}
    monkeypatch.setattr(scenarios, "build", lambda *args: [(30, "record", "SimAudit_CL", record)])
    profile = load_profile("uta")
    state = engine.new_state(profile, now)
    engine.add_scenario(state, "SCN-01", "run-123", now)
    first = engine.tick(profile, state, now)
    assert not any(row["ScenarioRunId"] == "run-123" for row in first.get("SimAudit_CL", []))
    assert state["scenarios"][0]["cursor"] == -1
    second = engine.tick(profile, state, now + timedelta(minutes=1))
    assert sum(row["ScenarioRunId"] == "run-123" for row in second["SimAudit_CL"]) == 1
    assert state["scenarios"][0]["cursor"] == 0
