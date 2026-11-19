from datetime import datetime, timedelta, timezone

from swugen import engine, load_profile


def test_queue_delay_does_not_drop_scenario_steps_before_last_minute():
    profile = load_profile("utb")
    start = datetime(2026, 11, 2, tzinfo=timezone.utc)
    state = engine.new_state(profile, start)
    engine.tick(profile, state, start + timedelta(minutes=2))
    engine.add_scenario(state, "SCN-07", "run-delayed", start)
    records = engine.tick(profile, state, start + timedelta(minutes=3))
    injected = [row for batch in records.values() for row in batch if row["ScenarioRunId"] == "run-delayed"]
    assert len(injected) == 8
    next_batch = engine.tick(profile, state, start + timedelta(minutes=4))
    assert not [row for batch in next_batch.values() for row in batch if row["ScenarioRunId"] == "run-delayed"]
