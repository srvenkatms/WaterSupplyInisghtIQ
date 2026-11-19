"""Where records go: Azure Monitor Logs Ingestion API, or JSON Lines files
for local runs and tests."""
from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import datetime


class JsonlSink:
    def __init__(self, folder: str | Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def send(self, utility_id: str, batches: dict[str, list[dict]]) -> dict[str, int]:
        counts = {}
        for table, recs in batches.items():
            with open(self.folder / f"{utility_id}.{table}.jsonl", "a") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
            counts[table] = len(recs)
        return counts


class IngestionSink:
    """Uploads with the caller's managed identity (SEC-01). utilities is the
    deployment output utilityIngestion: [{utilityId, dcrImmutableId, ...}]."""

    def __init__(self, endpoint: str, utilities: list[dict], credential=None):
        from azure.identity import DefaultAzureCredential
        from azure.monitor.ingestion import LogsIngestionClient
        self.client = LogsIngestionClient(endpoint=endpoint, credential=credential or DefaultAzureCredential())
        self.rules = {u["utilityId"]: u["dcrImmutableId"] for u in utilities}

    @classmethod
    def from_env(cls) -> "IngestionSink":
        return cls(os.environ["SWU_DCE_ENDPOINT"], json.loads(os.environ["SWU_UTILITIES"]))

    def send(self, utility_id: str, batches: dict[str, list[dict]]) -> dict[str, int]:
        rule = self.rules[utility_id]   # each utility writes only to its own rule (FR-02, SEC-03)
        for records in batches.values():
            for record in records:
                if record["UtilityId"] != utility_id:
                    raise ValueError("Cross-utility ingestion is not permitted")
                if datetime.fromisoformat(record["TimeGenerated"]).tzinfo is None:
                    raise ValueError("TimeGenerated must include its UTC offset")
                if not record.get("RecordId"):
                    raise ValueError("RecordId is required for retry-safe ingestion")
        counts = {}
        for table, recs in batches.items():
            if recs:
                self.client.upload(rule_id=rule, stream_name=f"Custom-{table}", logs=recs)
            counts[table] = len(recs)
        return counts
