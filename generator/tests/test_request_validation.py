from datetime import datetime, timezone

import pytest

from swugen import engine, load_profile, load_profiles
from swugen.requests import validate_request

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


@pytest.mark.parametrize("envelope", [
    [], {}, {"utility": "other", "scenario": "SCN-01"},
    {"utility": "uta", "scenario": "SCN-01", "runId": "../run-invalid", "start": NOW.isoformat()},
    {"utility": "uta", "scenario": "SCN-01", "runId": "run-123", "start": "2026-11-02"},
    {"utility": "uta", "scenario": "SCN-01", "runId": "run-123", "start": None},
])
def test_invalid_requests_are_explicitly_rejected(envelope):
    with pytest.raises(ValueError):
        validate_request(envelope, load_profiles())


def test_valid_envelope_and_retry_identifiers_are_deterministic():
    validate_request({"utility": "uta", "scenario": "SCN-01", "runId": "run-123", "start": NOW.isoformat()},
                     load_profiles())
    profile = load_profile("utc")
    first = engine.tick(profile, engine.new_state(profile, NOW), NOW)
    replay = engine.tick(profile, engine.new_state(profile, NOW), NOW)
    assert first == replay
    ids = [record["RecordId"] for records in first.values() for record in records]
    assert len(ids) == len(set(ids))
