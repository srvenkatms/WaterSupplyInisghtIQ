"""Actual-ingestion reference history for fast rules with long time horizons."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from azure.monitor.query import LogsQueryClient, LogsQueryStatus

from .account_state import WatchlistSink
from .configuration import HEALTH_FEEDS, SOURCE_HEALTH_UNION

QUERY = """
let plc = SwuProcess
| where TimeGenerated > ago(9d) and Tag == "PLC" and EventType in ("PlcUnreachable", "Recovered")
| summarize arg_max(TimeGenerated, EventType)
| project Source = "SwuProcess", PlcChangedAt = TimeGenerated, PlcEvent = EventType;
let heartbeat = SwuProcess
| where TimeGenerated > ago(9d) and EventType == "Heartbeat"
| summarize HeartbeatAt = max(TimeGenerated)
| extend Source = "SwuProcess";
let feeds = __HEALTH_UNION__
| where TimeGenerated > ago(9d)
| summarize LastSeen = max(ReceivedAt) by HealthFeed
| project Source = HealthFeed, LastSeen
| join kind=leftouter plc on Source
| join kind=leftouter heartbeat on Source
| project Kind = "feed", Source, LastSeen, PlcChangedAt = tostring(PlcChangedAt),
          PlcEvent = tostring(PlcEvent), HeartbeatAt = tostring(HeartbeatAt);
let access = SwuAccess
| where TimeGenerated between (ago(14d) .. ago(15m))
| summarize SeenAt = max(TimeGenerated) by Upn = tolower(UserPrincipalName), SourceIp, Location
| extend Kind = "access";
let incidents = SwuIncidents
| where TimeGenerated > ago(9d)
| project Kind = "incident", IncidentNumber, ChangedAt = TimeGenerated, Severity, Status;
union kind=outer feeds, access, incidents
""".replace("__HEALTH_UNION__", SOURCE_HEALTH_UNION)

FIELDS = {
    "feed": ("SourceReceipts", ("Source",),
             ("Source", "LastSeen", "PlcChangedAt", "PlcEvent", "HeartbeatAt")),
    "access": ("AccessHistory", ("Upn", "SourceIp", "Location"), ("Upn", "SourceIp", "Location", "SeenAt")),
    "incident": ("IncidentHistory", ("IncidentNumber",), ("IncidentNumber", "ChangedAt", "Severity", "Status")),
}
OPTIONAL_PLC_FIELDS = {"PlcChangedAt", "PlcEvent", "HeartbeatAt"}


def reference(uid: str, row: dict) -> tuple[str, uuid.UUID, dict]:
    if row["Kind"] == "feed" and row["Source"] not in HEALTH_FEEDS:
        raise ValueError(f"Unknown detection history source: {row['Source']}")
    alias, keys, columns = FIELDS[row["Kind"]]
    values = {}
    for column in columns:
        value = row.get(column, "") if column in OPTIONAL_PLC_FIELDS else row[column]
        if value is None:
            raise ValueError(f"Missing detection history value: {column}")
        values[column] = value.isoformat() if isinstance(value, datetime) else str(value)
    for column in ("LastSeen", "SeenAt", "ChangedAt", "PlcChangedAt", "HeartbeatAt"):
        if column not in values:
            continue
        if column in OPTIONAL_PLC_FIELDS and values[column] == "":
            continue
        stamp = datetime.fromisoformat(values[column].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError(f"Detection history requires a timezone-aware timestamp: {column}")
    key = json.dumps([uid, alias, *(values[column] for column in keys)], separators=(",", ":"))
    return alias, uuid.uuid5(uuid.NAMESPACE_URL, key), values


class DetectionHistorySink:
    def __init__(self, utilities: list[dict], credential):
        self.workspaces = {u["utilityId"]: u["workspaceId"] for u in utilities}
        self.query = LogsQueryClient(credential)
        self.watchlists = WatchlistSink(utilities, credential)

    def refresh(self, uid: str, state: dict, save) -> int:
        result = self.query.query_workspace(self.workspaces[uid], QUERY, timespan=timedelta(days=14))
        if result.status != LogsQueryStatus.SUCCESS:
            raise RuntimeError(f"Detection history query failed or was partial: {result.partial_error}")
        if not result.tables:
            raise RuntimeError("Detection history query returned no result table")
        now = datetime.now(timezone.utc)
        table = result.tables[0]
        updated = 0
        for values in table.rows:
            alias, item_id, record = reference(uid, dict(zip(table.columns, values)))
            key = str(item_id)
            previous = state.get(key)
            if previous and previous["values"] == record:
                refreshed = datetime.fromisoformat(previous["refreshedAt"])
                if now - refreshed < timedelta(days=7):
                    continue
            self.watchlists.put_item(uid, alias, item_id, record)
            state[key] = {"values": record, "refreshedAt": now.isoformat()}
            save(state)
            updated += 1
        return updated
