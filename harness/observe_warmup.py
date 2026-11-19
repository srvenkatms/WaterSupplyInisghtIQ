"""Observe the existing seven-real-day warm-up without changing the lab."""
from __future__ import annotations

import contextlib
import io
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import swuharness


def deadline(manifest: dict) -> datetime:
    value = manifest.get("warmupStartedAt")
    if not value:
        raise ValueError("A verified warm-up start is required")
    started = datetime.fromisoformat(value)
    if started.tzinfo is None or started > datetime.now(timezone.utc):
        raise ValueError("Warm-up start must be timezone-aware and not in the future")
    return started + timedelta(days=7)


def observe(path: Path) -> bool:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        health = swuharness.cmd_health(None)
        history = swuharness.cmd_history(None)
    record = {
        "observedAt": datetime.now(timezone.utc).isoformat(),
        "healthPassed": health == 0,
        "historyPassed": history == 0,
        "evidence": output.getvalue(),
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
        stream.flush()
    passed = record["healthPassed"] and record["historyPassed"]
    print(f"{record['observedAt']} warm-up observation: {'PASS' if passed else 'FAIL'}", flush=True)
    if not passed:
        print(record["evidence"], flush=True)
    return passed


def main() -> int:
    target = deadline(swuharness.outputs())
    directory = swuharness.ROOT / "test-results"
    directory.mkdir(exist_ok=True)
    path = directory / "warmup-observations.jsonl"
    print(f"Observing the existing lab until {target.isoformat()}; no scenario injection or configuration changes.",
          flush=True)
    while True:
        passed = observe(path)
        remaining = (target - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            if not passed:
                raise RuntimeError("Final live health/history evidence failed; acceptance remains blocked")
            swuharness.require_ready()
            print("Seven-day readiness passed. Scenario, permission, recovery and billing acceptance remain separate.",
                  flush=True)
            return 0
        time.sleep(min(300, remaining))


if __name__ == "__main__":
    raise SystemExit(main())
