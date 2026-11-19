import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from swugen import engine, load_profile, scenarios

SCHEMAS = {table["name"]: {column["name"]: column["type"] for column in table["columns"]}
           for table in json.loads((Path(__file__).resolve().parents[2] / "infra" / "tables.json").read_text())}
COMMON = {"TimeGenerated": "datetime", "UtilityId": "string", "ScenarioRunId": "string", "RecordId": "string"}
NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


@pytest.mark.parametrize("uid", ["uta", "utb", "utc"])
def test_all_generated_scenario_records_satisfy_required_schema_and_types(uid):
    profile = load_profile(uid)
    for scenario in scenarios.EXPECTED:
        if not scenarios.expected_rules(scenario, profile)["applicable"]:
            continue
        state = engine.new_state(profile, NOW)
        engine.add_scenario(state, scenario, f"run-{scenario}", NOW)
        batches = engine.tick(profile, state, NOW + timedelta(minutes=59))
        for table, records in batches.items():
            expected = SCHEMAS[table] | COMMON
            for record in records:
                assert expected.keys() <= record.keys(), (uid, scenario, table, expected.keys() - record.keys())
                assert record["UtilityId"] == uid and record["RecordId"]
                for field, kind in expected.items():
                    value = record[field]
                    if value is None:
                        assert field not in COMMON
                    elif kind == "string":
                        assert isinstance(value, str), (table, field, value)
                    elif kind == "datetime":
                        assert datetime.fromisoformat(value).tzinfo is not None
                    elif kind == "int":
                        assert type(value) is int
                    elif kind == "real":
                        assert type(value) in (int, float) and math.isfinite(value)
