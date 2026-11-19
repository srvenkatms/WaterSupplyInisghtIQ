"""Local simulation without Azure: python -m swugen.cli simulate --utility utc --hours 24 --out out/
Optionally inject a scenario: --scenario SCN-08 --at-minute 120"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from . import engine, load_profile
from .sink import JsonlSink


def simulate(utility: str, start: datetime, hours: float, sink, scenario: str | None = None,
             at_minute: int = 0, run_id: str = "local") -> dict:
    p = load_profile(utility)
    state = engine.new_state(p, start)
    totals: dict[str, int] = {}
    for m in range(int(hours * 60)):
        now = start + timedelta(minutes=m)
        if scenario and m == at_minute:
            engine.add_scenario(state, scenario, run_id, now)
        for table, n in sink.send(utility, engine.tick(p, state, now)).items():
            totals[table] = totals.get(table, 0) + n
    return totals


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("simulate")
    s.add_argument("--utility", required=True)
    s.add_argument("--hours", type=float, default=24)
    s.add_argument("--start", default="2026-11-02T00:00:00+00:00")
    s.add_argument("--out", default="out")
    s.add_argument("--scenario")
    s.add_argument("--at-minute", type=int, default=0)
    a = ap.parse_args()
    start = datetime.fromisoformat(a.start).astimezone(timezone.utc)
    print(simulate(a.utility, start, a.hours, JsonlSink(a.out), a.scenario, a.at_minute))


if __name__ == "__main__":
    main()
