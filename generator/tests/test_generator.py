import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from swugen import engine, load_profile, load_profiles, scenarios
from rules_py import evaluate, ts

TABLES = {t["name"]: {c["name"] for c in t["columns"]} | {"TimeGenerated", "UtilityId", "ScenarioRunId", "RecordId"}
          for t in json.loads((Path(__file__).resolve().parents[2] / "infra" / "tables.json").read_text())}
MONDAY = datetime(2026, 11, 2, tzinfo=timezone.utc)


import copy

WARMUP_DAYS = 7   # FR-08: tests start after the same warm-up the lab uses
HIGH_RULES = {"DR-02", "DR-04", "DR-05", "DR-06", "DR-07", "DR-08", "DR-09", "DR-10"}
_warm = {}


def warmed(uid, start):
    """State and history after a WARMUP_DAYS baseline ending at start (cached)."""
    key = (uid, start)
    if key not in _warm:
        _warm[key] = run(uid, start - timedelta(days=WARMUP_DAYS), WARMUP_DAYS * 1440, keep_state=True)
    p, recs, state = _warm[key]
    return p, copy.deepcopy(recs), copy.deepcopy(state)


def run(uid, start, minutes, scenario=None, at_minute=0, run_id="test", state=None, recs=None, keep_state=False):
    p = load_profile(uid)
    state = state or engine.new_state(p, start)
    recs = recs if recs is not None else {}
    for m in range(minutes):
        now = start + timedelta(minutes=m)
        if scenario and m == at_minute:
            engine.add_scenario(state, scenario, run_id, now)
        for table, batch in engine.tick(p, state, now).items():
            recs.setdefault(table, []).extend(batch)
    return (p, recs, state) if keep_state else (p, recs)


@pytest.fixture(scope="module")
def two_days():
    return {uid: run(uid, MONDAY, 2 * 1440) for uid in load_profiles()}


def test_tc05_reproducible():
    _, a = run("utc", MONDAY, 600)
    _, b = run("utc", MONDAY, 600)
    assert a == b


def test_tc04_schema_matches_tables(two_days):
    for uid, (_, recs) in two_days.items():
        for table, batch in recs.items():
            assert table in TABLES, table
            for r in batch:
                assert set(r) <= TABLES[table], (table, set(r) - TABLES[table])


def test_tc03_records_carry_own_utility(two_days):
    for uid, (_, recs) in two_days.items():
        assert all(r["UtilityId"] == uid for batch in recs.values() for r in batch)


def test_tc02_every_tier_table_has_data_within_30_minutes():
    expected = {"SimSignin_CL", "SimFirewall_CL", "SimEndpoint_CL", "SimExposure_CL", "SimInventory_CL"}
    for uid in load_profiles():
        p, recs = run(uid, MONDAY + timedelta(hours=9), 30)
        need = expected | ({"SimProcess_CL"} if p["processSource"] == "model" else set())
        assert need <= {t for t, b in recs.items() if b}, (uid, need - set(recs))


def test_tc06_process_stays_safe_and_cycles_over_24h():
    p = load_profile("utc")
    model = engine.PlantModel(p["plant"])
    levels, residuals, pump = [], [], []
    for m in range(1440 * 2):
        s = model.step(m % 1440)
        levels.append(s.TankLevelPct); residuals.append(s.ClResidual); pump.append(s.PumpState)
    lo, hi = p["safeLimits"]["TankLevelPct"]
    assert lo <= min(levels) and max(levels) <= hi
    assert p["plant"]["pumpLowSetpoint"] - 2 <= min(levels[1440:]) and max(levels[1440:]) <= p["plant"]["pumpHighSetpoint"] + 2
    assert sum(1 for a, b in zip(pump, pump[1:]) if a == 0 and b == 1) >= 2, "pump should start at least twice"
    rlo, rhi = p["safeLimits"]["ClResidual"]
    assert rlo <= min(residuals) and max(residuals) <= rhi


def test_tc06_heartbeat_every_15_minutes(two_days):
    _, recs = two_days["utc"]
    hb = sorted(ts(r) for r in recs["SimProcess_CL"] if r["EventType"] == "Heartbeat" and r["Tag"] == "TankLevelPct")
    gaps = {(b - a) for a, b in zip(hb, hb[1:])}
    assert gaps == {timedelta(minutes=15)}


def test_daily_volumes_within_profile_ranges(two_days):
    for uid, (p, recs) in two_days.items():
        day = lambda t: [r for r in recs.get(t, []) if MONDAY <= ts(r) < MONDAY + timedelta(days=1)]
        lo, hi = p["volumes"]["firewallDeniedPerDay"]
        denied = [r for r in day("SimFirewall_CL") if r["DeviceAction"] == "Deny"]
        assert lo <= len(denied) <= hi, (uid, len(denied))
        lo, hi = p["volumes"]["endpointPerDay"]
        events = [r for r in day("SimEndpoint_CL") if r["ActionType"] != "CollectorHeartbeat"]
        assert lo <= len(events) <= hi, (uid, len(events))


def test_sparse_sources_report_explicit_collector_health_without_fabricated_activity(two_days):
    fields = {"SimSignin_CL": "ResultType", "SimAccessTraffic_CL": "Action",
              "SimEndpoint_CL": "ActionType", "SimFirewall_CL": "Activity"}
    for _, recs in two_days.values():
        for table, field in fields.items():
            records = [r for r in recs[table] if r[field] == "CollectorHeartbeat"]
            assert len(records) == 2 * 24 * 4
            assert all(ts(b) - ts(a) == timedelta(minutes=15) for a, b in zip(records, records[1:]))
            assert all(not r.get("UserPrincipalName") and not r.get("AccountName") for r in records)
        assert not [r for r in recs.get("SimProcess_CL", []) if r.get("EventType") == "CollectorHeartbeat"]


def test_nfr07_no_high_rule_matches_at_baseline():
    """A 24-hour baseline after warm-up triggers no High rule, and few Medium ones."""
    for uid in load_profiles():
        p, recs, state = warmed(uid, MONDAY)
        p, recs = run(uid, MONDAY, 1440, state=state, recs=recs)
        hits = evaluate(p, recs, MONDAY, MONDAY + timedelta(days=1))
        assert not (set(hits) & HIGH_RULES), (uid, {k: len(v) for k, v in hits.items()})
        assert len(hits.get("DR-01", [])) <= 2, (uid, "too many unusual-session alerts at baseline")


CASES = [(s, u) for s in scenarios.EXPECTED for u in load_profiles()]


@pytest.mark.parametrize("name,uid", CASES)
def test_tc07_scenarios_trigger_expected_rules(name, uid):
    p = load_profile(uid)
    exp = scenarios.expected_rules(name, p)
    if not exp["applicable"]:
        pytest.skip(f"{name} does not apply to tier {p['tier']}")
    start = MONDAY + timedelta(hours=2)               # 02:00, after the warm-up
    p, recs, state = warmed(uid, start)
    p, recs = run(uid, start, 180, scenario=name, at_minute=0, state=state, recs=recs)
    primary = evaluate(p, recs, start, start + timedelta(minutes=15))
    later = evaluate(p, recs, start, start + timedelta(minutes=180))
    act_mode = p["processSource"] == "gateway"         # Utility A changes the real PLC instead
    for rule in exp["primary"]:
        if act_mode and rule in ("DR-06",):
            continue
        assert rule in primary, (name, uid, rule, sorted(primary))
    for rule in exp["eventual"]:
        if act_mode or rule == "DR-11":
            continue
        assert rule in later, (name, uid, rule, sorted(later))
    if name == "SCN-10":
        assert later == {}, sorted(later)   # legitimate night work raises nothing


def test_scn09_cuts_firewall_and_process_feeds():
    start = MONDAY + timedelta(hours=10)
    _, recs = run("utc", start, 150, scenario="SCN-09", at_minute=0)
    cut_from, cut_to = start + timedelta(minutes=6), start + timedelta(minutes=94)
    for table in ("SimFirewall_CL", "SimProcess_CL"):
        assert not [r for r in recs[table] if cut_from <= ts(r) < cut_to], table
        assert [r for r in recs[table] if ts(r) >= cut_to + timedelta(minutes=2)], f"{table} should resume"


def test_scenario_records_carry_run_id():
    _, recs = run("utb", MONDAY + timedelta(hours=10), 20, scenario="SCN-07", at_minute=0, run_id="run-123")
    tagged = [r for b in recs.values() for r in b if r["ScenarioRunId"] == "run-123"]
    assert len(tagged) >= 8
