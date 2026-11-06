import pytest

from swugen import load_profile
from swugen.configuration import validate_profile


@pytest.mark.parametrize("uid", ["uta", "utb", "utc"])
def test_repository_profiles_are_valid(uid):
    validate_profile(load_profile(uid))


@pytest.mark.parametrize("field,value,message", [
    ("id", "ut_a", "Invalid utility ID"),
    ("id", "UTa", "Invalid utility ID"),
    ("processSource", "none", "Baseline utilities"),
    ("tier", "baseline", "Baseline utilities"),
    ("id", "utd", "Only Utility A"),
])
def test_invalid_deployment_profile_is_rejected(field, value, message):
    profile = load_profile("uta")
    profile[field] = value
    with pytest.raises(ValueError, match=message):
        validate_profile(profile)


@pytest.mark.parametrize("field,value,message", [
    ("residualTauMin", 0, "positive response time"),
    ("fillRatePctPerMin", 0, "sufficient pump capacity"),
    ("pumpLowSetpoint", 100, "below high pump setpoint"),
    ("clDoseSetpoint", 600, "Unsafe baseline value"),
    ("initialLevelPct", -1, "Invalid plant parameter"),
    ("residualGain", float("inf"), "Invalid plant parameter"),
    ("baseDemandPctPerMin", float("nan"), "Invalid plant parameter"),
])
def test_invalid_plant_parameters_are_rejected(field, value, message):
    profile = load_profile("uta")
    profile["plant"][field] = value
    with pytest.raises(ValueError, match=message):
        validate_profile(profile)


def test_required_safe_limits_cannot_be_removed():
    profile = load_profile("uta")
    del profile["safeLimits"]["ClResidual"]
    with pytest.raises(ValueError, match="safe limits are missing"):
        validate_profile(profile)


@pytest.mark.parametrize("start,end", [
    ("2026-11-02T00:00:00", "2026-11-02T01:00:00"),
    ("2026-11-02T01:00:00+00:00", "2026-11-02T00:00:00+00:00"),
])
def test_invalid_maintenance_windows_are_rejected(start, end):
    profile = load_profile("uta")
    profile["maintenanceWindows"] = [{"start": start, "end": end, "description": "Synthetic maintenance"}]
    with pytest.raises(ValueError, match="ordered, timezone-aware"):
        validate_profile(profile)


def test_invalid_backup_date_is_rejected():
    profile = load_profile("uta")
    profile["backups"] = [{"asset": "Synthetic asset", "lastBackup": "not-a-date"}]
    with pytest.raises(ValueError):
        validate_profile(profile)
