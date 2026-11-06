"""Checkpoint generated batches before delivery; retry failures without data loss."""
from __future__ import annotations

import copy

from . import engine


def prepare(profile: dict, state: dict, now, acceleration: int = 1) -> None:
    if state.get("pending"):
        raise ValueError("Pending ingestion must be delivered before generating another batch")
    advanced = copy.deepcopy(state)
    batches = engine.tick(profile, advanced, now, acceleration)
    advanced["pending"] = {table: rows for table, rows in batches.items() if rows}
    state.clear()
    state.update(advanced)


def flush(uid: str, state: dict, sink, save, account_sink=None) -> dict[str, int]:
    counts = {}
    for table in list(state.get("pending", {})):
        records = state["pending"][table]
        sink.send(uid, {table: records})
        if table == "SimAudit_CL" and account_sink is not None:
            account_sink.apply(uid, records)
        del state["pending"][table]
        save(state)
        counts[table] = len(records)
    return counts
