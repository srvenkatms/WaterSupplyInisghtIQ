from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from azure.monitor.query import LogsQueryStatus

import swugen.detection_history as history
from swugen import load_profile
from swugen.configuration import reference_data

STAMP = datetime(2026, 11, 2, tzinfo=timezone.utc)


@pytest.mark.parametrize("row,alias", [
    ({"Kind": "feed", "Source": "SwuExposure", "LastSeen": STAMP}, "SourceReceipts"),
    ({"Kind": "access", "Upn": "operator@uta.example", "SourceIp": "192.0.2.1",
      "Location": "Local", "SeenAt": STAMP}, "AccessHistory"),
    ({"Kind": "incident", "IncidentNumber": 1, "ChangedAt": STAMP,
      "Severity": "High", "Status": "Active"}, "IncidentHistory"),
])
def test_history_references_keep_real_timestamps_and_stable_scoped_keys(row, alias):
    actual_alias, item, values = history.reference("uta", row)
    assert actual_alias == alias
    assert STAMP.isoformat() in values.values()
    assert history.reference("uta", row)[1] == item
    assert history.reference("utc", row)[1] != item
    assert not {"ScenarioRunId", "RecordId"} & values.keys()


@pytest.mark.parametrize("stamp", [None, "2026-11-02T00:00:00"])
def test_missing_or_naive_receipt_timestamp_is_not_fabricated(stamp):
    with pytest.raises(ValueError):
        history.reference("uta", {"Kind": "feed", "Source": "SwuExposure", "LastSeen": stamp})


def test_health_union_labels_function_results_explicitly():
    assert "withsource" not in history.QUERY
    for source in history.HEALTH_FEEDS:
        function = source + "Feed" if source in {"SwuSignins", "SwuAccess", "SwuFirewall", "SwuEndpoint"} else source
        assert f'({function} | extend HealthFeed = "{source}")' in history.QUERY


@pytest.mark.parametrize("source", ["union_arg0", "unknown", ""])
def test_history_rejects_noncanonical_source_names(source):
    with pytest.raises(ValueError, match="Unknown detection history source"):
        history.reference("uta", {"Kind": "feed", "Source": source, "LastSeen": STAMP})


def create_sink(monkeypatch, rows, status=LogsQueryStatus.SUCCESS):
    columns = list(rows[0]) if rows else ["Kind"]
    table = SimpleNamespace(columns=columns, rows=[[row.get(c) for c in columns] for row in rows])
    client = SimpleNamespace()
    queries = []

    def query(workspace, kql, timespan):
        queries.append((workspace, kql, timespan))
        return SimpleNamespace(status=status, partial_error="synthetic query error", tables=[table])

    client.query_workspace = query
    monkeypatch.setattr(history, "LogsQueryClient", lambda credential: client)
    sink = history.DetectionHistorySink([{
        "utilityId": "uta", "workspaceId": "workspace-customer-id", "workspaceResourceId": "/synthetic/workspace",
    }], object())
    writes = []
    sink.watchlists.put_item = lambda *args: writes.append(args)
    return sink, writes, queries


def test_only_successfully_queried_changed_history_is_persisted(monkeypatch):
    sink, writes, queries = create_sink(monkeypatch, [
        {"Kind": "feed", "Source": "SwuExposure", "LastSeen": STAMP},
    ])
    state, saves = {}, []
    assert sink.refresh("uta", state, lambda state: saves.append(dict(state))) == 1
    assert len(writes) == len(saves) == 1
    assert queries[0][0] == "workspace-customer-id"
    assert queries[0][2] == timedelta(days=14)
    assert "max(ReceivedAt)" in queries[0][1]
    assert "ago(14d) .. ago(15m)" in queries[0][1]
    assert sink.refresh("uta", state, lambda state: saves.append(dict(state))) == 0
    assert len(writes) == 1


def test_failed_reference_write_cannot_advance_durable_checkpoint(monkeypatch):
    sink, _, _ = create_sink(monkeypatch, [
        {"Kind": "feed", "Source": "SwuInventory", "LastSeen": STAMP},
    ])

    def fail(*args):
        raise ConnectionError("synthetic ARM failure")

    sink.watchlists.put_item = fail
    state = {}
    with pytest.raises(ConnectionError):
        sink.refresh("uta", state, lambda state: pytest.fail("Failed delivery advanced the checkpoint"))
    assert not state


def test_successful_rows_survive_a_later_failure_and_retry_skips_them(monkeypatch):
    sink, writes, _ = create_sink(monkeypatch, [
        {"Kind": "feed", "Source": "SwuInventory", "LastSeen": STAMP},
        {"Kind": "feed", "Source": "SwuExposure", "LastSeen": STAMP},
    ])
    successful = sink.watchlists.put_item

    def fail_second(*args):
        if len(writes) == 1:
            raise ConnectionError("synthetic second-write failure")
        successful(*args)

    sink.watchlists.put_item = fail_second
    state, saves = {}, []
    with pytest.raises(ConnectionError):
        sink.refresh("uta", state, lambda state: saves.append(dict(state)))
    assert len(state) == len(saves) == len(writes) == 1
    sink.watchlists.put_item = successful
    assert sink.refresh("uta", state, lambda state: saves.append(dict(state))) == 1
    assert len(state) == len(saves) == len(writes) == 2


def test_unchanged_reference_is_refreshed_after_seven_days(monkeypatch):
    sink, writes, _ = create_sink(monkeypatch, [
        {"Kind": "feed", "Source": "SwuExposure", "LastSeen": STAMP},
    ])
    state = {}
    sink.refresh("uta", state, lambda state: None)
    next(iter(state.values()))["refreshedAt"] = (
        datetime.now(timezone.utc) - timedelta(days=7, seconds=1)).isoformat()
    assert sink.refresh("uta", state, lambda state: None) == 1
    assert len(writes) == 2


@pytest.mark.parametrize("kind,column,fields", [
    ("access", "SeenAt", {"Upn": "a@uta.example", "SourceIp": "192.0.2.1", "Location": "Local"}),
    ("incident", "ChangedAt", {"IncidentNumber": 1, "Severity": "High", "Status": "Active"}),
])
def test_access_and_incident_history_reject_naive_timestamps(kind, column, fields):
    with pytest.raises(ValueError, match=column):
        history.reference("uta", {"Kind": kind, column: "2026-11-02T00:00:00", **fields})


def test_partial_history_query_does_not_update_reference_state(monkeypatch):
    sink, writes, _ = create_sink(monkeypatch, [], LogsQueryStatus.PARTIAL)
    with pytest.raises(RuntimeError, match="partial"):
        sink.refresh("uta", {}, lambda state: pytest.fail("Partial query advanced checkpoint"))
    assert not writes


def test_no_ingested_rows_is_legitimate_empty_history_not_synthetic_data(monkeypatch):
    sink, writes, _ = create_sink(monkeypatch, [])
    assert sink.refresh("uta", {}, lambda state: pytest.fail("Empty history wrote a checkpoint")) == 0
    assert not writes


def test_empty_watchlists_are_metadata_only_without_fake_items():
    lists = {item["alias"]: item for item in reference_data(load_profile("uta"))}
    for alias in ("MaintenanceWindows", "DisabledAccounts", "SourceReceipts", "AccessHistory", "IncidentHistory"):
        assert lists[alias]["hasItems"] is False
        assert len(lists[alias]["csv"].splitlines()) == 1
    assert lists["Roster"]["hasItems"] is True


def test_process_receipt_keeps_actual_plc_state_and_heartbeat_for_long_outages():
    alias, _, values = history.reference("uta", {
        "Kind": "feed", "Source": "SwuProcess", "LastSeen": STAMP,
        "PlcChangedAt": STAMP, "PlcEvent": "PlcUnreachable",
        "HeartbeatAt": STAMP - timedelta(hours=1),
    })
    assert alias == "SourceReceipts"
    assert values["PlcChangedAt"] == STAMP.isoformat()
    assert values["HeartbeatAt"] == (STAMP - timedelta(hours=1)).isoformat()
    assert values["PlcEvent"] == "PlcUnreachable"
    assert 'ago(9d) and Tag == "PLC"' in history.QUERY


def test_no_plc_status_is_empty_optional_metadata_not_a_fabricated_heartbeat():
    _, _, values = history.reference("uta", {"Kind": "feed", "Source": "SwuExposure", "LastSeen": STAMP})
    assert values["PlcChangedAt"] == values["PlcEvent"] == values["HeartbeatAt"] == ""
