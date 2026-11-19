import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "observe_warmup", Path(__file__).resolve().parents[1] / "observe_warmup.py")
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)


@pytest.mark.parametrize("manifest", [
    {}, {"warmupStartedAt": "2026-01-01T00:00:00"},
    {"warmupStartedAt": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
])
def test_observation_requires_real_verified_aware_start(manifest):
    with pytest.raises(ValueError):
        observer.deadline(manifest)


def test_observation_does_not_accelerate_or_replace_the_start():
    started = datetime.now(timezone.utc) - timedelta(hours=1)
    manifest = {"warmupStartedAt": started.isoformat(), "deployedAt": "2020-01-01T00:00:00+00:00"}
    assert observer.deadline(manifest) == started + timedelta(days=7)
    assert manifest["warmupStartedAt"] == started.isoformat()


@pytest.mark.parametrize("health,history", [(0, 0), (1, 0), (0, 1)])
def test_observation_preserves_actual_failures(monkeypatch, tmp_path, health, history):
    monkeypatch.setattr(observer.swuharness, "cmd_health", lambda args: health)
    monkeypatch.setattr(observer.swuharness, "cmd_history", lambda args: history)
    path = tmp_path / "observations.jsonl"
    assert observer.observe(path) == (health == history == 0)
    record = json.loads(path.read_text())
    assert record["healthPassed"] is (health == 0)
    assert record["historyPassed"] is (history == 0)


def test_observer_does_not_hide_query_errors(monkeypatch, tmp_path):
    def failed(args):
        raise RuntimeError("Actual query failed")
    monkeypatch.setattr(observer.swuharness, "cmd_health", failed)
    with pytest.raises(RuntimeError, match="Actual query failed"):
        observer.observe(tmp_path / "observations.jsonl")
