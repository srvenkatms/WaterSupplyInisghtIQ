"""Durable gateway outbox; replay is at-least-once after a transport failure."""
import json
import sqlite3
from pathlib import Path


class EventSpool:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        self.connection.commit()

    def state(self) -> dict | None:
        row = self.connection.execute("SELECT payload FROM checkpoint WHERE id = 1").fetchone()
        return json.loads(row[0]) if row else None

    def append(self, events: list[dict], state: dict | None = None) -> None:
        with self.connection:
            self.connection.executemany("INSERT INTO outbox(payload) VALUES (?)",
                                        [(json.dumps(event),) for event in events])
            if state is not None:
                self.connection.execute("INSERT OR REPLACE INTO checkpoint(id, payload) VALUES (1, ?)",
                                        (json.dumps(state),))

    def flush(self, uid: str, sink) -> None:
        while True:
            rows = self.connection.execute("SELECT id, payload FROM outbox ORDER BY id LIMIT 500").fetchall()
            if not rows:
                return
            sink.send(uid, {"SimProcess_CL": [json.loads(payload) for _, payload in rows]})
            with self.connection:
                self.connection.executemany("DELETE FROM outbox WHERE id = ?", [(key,) for key, _ in rows])
