import argparse
import io
import json
from types import SimpleNamespace

import pytest


def test_log_query_client_reuses_its_authenticated_pipeline(monkeypatch):
    import azure.monitor.query

    calls = []
    client = object()
    monkeypatch.setattr(azure.monitor.query, "LogsQueryClient", lambda credential: calls.append(credential) or client)
    monkeypatch.setattr(swuharness, "credential", lambda: "synthetic-credential")
    swuharness.logs_client.cache_clear()
    try:
        assert swuharness.logs_client() is swuharness.logs_client() is client
        assert calls == ["synthetic-credential"]
    finally:
        swuharness.logs_client.cache_clear()


@pytest.mark.parametrize("control_fails", [False, True])
def test_network_proof_is_read_only_and_always_stops_the_temporary_tunnel(monkeypatch, tmp_path, control_fails):
    import sys

    calls = []
    process = SimpleNamespace(terminate=lambda: calls.append("terminate"), wait=lambda timeout: 0)
    monkeypatch.setitem(sys.modules, "private_operator", SimpleNamespace(
        tunnel_command=lambda manifest, role: ["synthetic", role],
        wait_for_ssh=lambda role, process: None))
    monkeypatch.setattr(swuharness, "outputs", lambda: {})
    monkeypatch.setattr(swuharness, "Path", lambda path: tmp_path / "tunnel.log")
    monkeypatch.setattr(swuharness, "remote", lambda role, args: calls.append((role, args)) or "read proof")
    monkeypatch.setattr(swuharness.subprocess, "Popen", lambda *args, **kwargs: process)

    def run(command, **kwargs):
        assert command[:2] == ["ssh", "swu-plc"]
        assert kwargs["check"] is True and "1.1.1.1" in command[-1]
        if control_fails:
            raise RuntimeError("Forbidden connection succeeded")
        return SimpleNamespace(stdout="BLOCKED 1.1.1.1:443")

    monkeypatch.setattr(swuharness.subprocess, "run", run)
    if control_fails:
        with pytest.raises(RuntimeError, match="Forbidden connection"):
            swuharness.cmd_network(SimpleNamespace())
    else:
        assert swuharness.cmd_network(SimpleNamespace()) == 0
    assert calls[-1] == "terminate"
    assert [call[1][-1] for call in calls[:2]] == ["scan", "hmi-scan"]
    assert "1502" in calls[2][1][-1] and "8080" in calls[2][1][-1]

import swuharness


@pytest.mark.parametrize("evidence,passes", [
    ({"pass": True, "deniedFunctions": [6, 16, 6, 1, 4], "setpointsUnchanged": True}, True),
    ({"pass": True, "deniedFunctions": [], "setpointsUnchanged": True}, False),
    ({"pass": True, "deniedFunctions": [6, 16, 6, 1, 4], "setpointsUnchanged": False}, False),
])
def test_gateway_protocol_proof_requires_explicit_complete_denial_evidence(monkeypatch, tmp_path, evidence, passes):
    def remote(role, arguments):
        assert role == "gateway" and arguments[:4] == ["docker", "exec", "swu-gateway", "python"]
        compile(arguments[-1], "<gateway-probe>", "exec")
        assert "exception_code" in arguments[-1]
        assert 'client.write_register(address, before[address])' in arguments[-1]
        return json.dumps(evidence)

    monkeypatch.setattr(swuharness, "remote", remote)
    monkeypatch.setattr(swuharness, "ROOT", tmp_path)
    if passes:
        assert swuharness.cmd_protocol(SimpleNamespace()) == 0
        files = list((tmp_path / "test-results").glob("gateway-protocol-*.json"))
        assert len(files) == 1 and json.loads(files[0].read_text())["pass"] is True
    else:
        with pytest.raises(RuntimeError, match="incomplete protocol denial"):
            swuharness.cmd_protocol(SimpleNamespace())
        assert not (tmp_path / "test-results").exists()


@pytest.fixture
def manifest(monkeypatch):
    value = {"subscription": "11111111-1111-4111-8111-111111111111",
             "resourceGroups": ["lab-shared", "lab-uta"],
             "sharedWorkspaceId": "shared-workspace", "budgetAmount": 500,
             "utilities": [{"utilityId": "uta", "workspaceResourceId": "/synthetic/workspaces/uta"}]}
    monkeypatch.setattr(swuharness, "outputs", lambda: value)
    monkeypatch.setattr(swuharness, "require_ready", lambda: None)
    return value


@pytest.mark.parametrize("hours", [23, float("nan"), float("inf")])
def test_availability_requires_a_finite_full_day(manifest, hours):
    with pytest.raises(ValueError, match="at least 24 hours"):
        swuharness.cmd_availability(argparse.Namespace(hours=hours))


@pytest.mark.parametrize("rows,expected", [
    ([], 1),
    ([{"Records": 0, "MaxGapSeconds": None}], 1),
    ([{"Records": 1, "MaxGapSeconds": 601}], 1),
    ([{"Records": 100, "MaxGapSeconds": 600}], 0),
])
def test_availability_rejects_empty_or_gapped_evidence(monkeypatch, manifest, rows, expected):
    def query(workspace, kql, hours):
        assert workspace == manifest["sharedWorkspaceId"]
        assert hours == 24
        assert 'swu-generate utility=uta ' in kql
        assert "prev(TimeGenerated)" in kql
        assert "now(), LastSeen" in kql
        assert "FirstSeen, ago(24h)" in kql
        return rows

    monkeypatch.setattr(swuharness, "query_workspace", query)
    assert swuharness.cmd_availability(argparse.Namespace(hours=24)) == expected


def cost_response(monkeypatch, rows):
    monkeypatch.setattr(swuharness, "credential", lambda: SimpleNamespace(
        get_token=lambda scope: SimpleNamespace(token="synthetic-token")))
    payload = {"properties": {
        "columns": [{"name": "PreTaxCost"}, {"name": "Currency"}], "rows": rows}}

    def send(request, timeout):
        assert request.get_method() == "POST"
        assert timeout == 60
        body = json.loads(request.data)
        assert body["timeframe"] == "MonthToDate"
        assert body["dataset"]["filter"]["dimensions"]["values"] == ["lab-shared", "lab-uta"]
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(swuharness, "urlopen", send)


def test_cost_is_scoped_and_observation_only(monkeypatch, manifest, capsys):
    cost_response(monkeypatch, [[12.5, "USD"], [3, "USD"]])
    assert swuharness.cmd_cost(argparse.Namespace()) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["monthToDate"] == 15.5
    assert report["monthlyBudget"] == 500
    assert report["status"].startswith("observation-only")


@pytest.mark.parametrize("rows,message", [
    ([], "no billing rows"),
    ([[1, "USD"], [2, "EUR"]], "multiple billing currencies"),
    ([["invalid", "USD"]], "invalid billing amounts"),
    ([[float("nan"), "USD"]], "invalid billing amounts"),
    ([[1, ""]], "no billing currency"),
])
def test_cost_rejects_unusable_evidence(monkeypatch, manifest, rows, message):
    cost_response(monkeypatch, rows)
    with pytest.raises(RuntimeError, match=message):
        swuharness.cmd_cost(argparse.Namespace())


def test_cost_service_failure_is_not_a_pass(monkeypatch, manifest):
    cost_response(monkeypatch, [[1, "USD"]])

    def fail(*args, **kwargs):
        raise OSError("Synthetic billing service failure")

    monkeypatch.setattr(swuharness, "urlopen", fail)
    with pytest.raises(OSError, match="billing service failure"):
        swuharness.cmd_cost(argparse.Namespace())


@pytest.mark.parametrize("event,expected", [
    ("PlcUnreachable", 1), (None, 1), ("Recovered", 0), ("Heartbeat", 0),
])
def test_health_does_not_confuse_unreachable_messages_with_a_working_plc(
        monkeypatch, manifest, event, expected):
    from datetime import datetime, timezone
    from swugen import load_profile
    from swugen.configuration import deployment_profile

    now = datetime.now(timezone.utc)
    names = {alias: table for table, alias in swuharness.NORMALIZERS.items()}
    rows = [{"Source": names[item["Source"]], "LastSeen": now, "Rows": 1}
            for item in deployment_profile(load_profile("uta"))["healthSources"]]
    monkeypatch.setattr(swuharness, "query", lambda uid, kql, hours:
                        [{"EventType": event}] if "arg_max" in kql else rows)
    assert swuharness.cmd_health(argparse.Namespace()) == expected


@pytest.mark.parametrize("hours,bytes_used,cap,expected", [
    (24, 199_999_999, 1, 0),
    (24, 200_000_000, 1, 1),
    (23, 100, 1, 1),
    (24, None, 1, 1),
    (24, 0, 1, 1),
    (24, 100, 2, 1),
    (24, 100, True, 1),
])
def test_volume_uses_all_workspace_billing_and_actual_cap(
        monkeypatch, manifest, hours, bytes_used, cap, expected):
    def query(uid, kql, hours):
        if kql.startswith("Usage"):
            assert "startofday(ago(1d))" in kql and "IsBillable == true" in kql
            return [{"Hours": billing_hours, "Bytes": bytes_used}]
        return [{"Records": 1, "Bytes": 100}]

    billing_hours = hours
    monkeypatch.setattr(swuharness, "query", query)
    monkeypatch.setattr(swuharness.shutil, "which", lambda name: "synthetic-az")

    def run(command, **kwargs):
        assert command[command.index("--ids") + 1] == manifest["utilities"][0]["workspaceResourceId"]
        assert "workspaceCapping.dailyQuotaGb" in command
        assert kwargs["check"] is True
        return SimpleNamespace(stdout=json.dumps(cap))

    monkeypatch.setattr(swuharness.subprocess, "run", run)
    assert swuharness.cmd_volume(argparse.Namespace()) == expected


@pytest.mark.parametrize("leaked,missing,expected", [
    (False, False, 0), (True, False, 1), (False, True, 1),
])
def test_content_checks_real_normalization_schema_and_all_watchlists(
        monkeypatch, manifest, leaked, missing, expected):
    tables = json.loads((swuharness.ROOT / "infra" / "tables.json").read_text())
    schemas = {}
    for table in tables:
        columns = {"TimeGenerated": "datetime", "ReceivedAt": "datetime", "UtilityId": "string"}
        columns.update({column["name"]: column["type"] for column in table["columns"]})
        schemas[swuharness.NORMALIZERS[table["name"]]] = columns
        if swuharness.NORMALIZERS[table["name"]] in swuharness.COLLECTOR_FIELDS:
            schemas[swuharness.NORMALIZERS[table["name"]] + "Feed"] = columns.copy()
    if leaked:
        schemas["SwuProcess"]["ScenarioRunId"] = "string"
    if missing:
        del schemas["SwuProcess"]["NewValue"]
    watchlists = []

    def query(uid, kql):
        if kql.startswith("_GetWatchlist"):
            watchlists.append(kql)
            return []
        columns = schemas.get(kql.split()[0], {"TimeGenerated": "datetime"})
        return [{"ColumnName": name, "ColumnType": kind} for name, kind in columns.items()]

    monkeypatch.setattr(swuharness, "query", query)
    assert swuharness.cmd_content(argparse.Namespace()) == expected
    assert len(watchlists) == 10
    for alias in ("SourceReceipts", "AccessHistory", "IncidentHistory"):
        assert any(f'"{alias}"' in query for query in watchlists)
    assert any("DisabledAccounts" in kql for kql in watchlists)


@pytest.mark.parametrize("successes,rows,expected", [
    (0, [{"LagSeconds": 0}], 1),
    (1, [], 1),
    (1, [{"LagSeconds": None}], 1),
    (1, [{"LagSeconds": 601}], 1),
    (1, [{"LagSeconds": 600}], 0),
    (1, [{"LagSeconds": 0}], 0),
])
def test_history_requires_timer_success_and_actual_ingestion_references(
        monkeypatch, manifest, successes, rows, expected):
    def traces(workspace, kql, hours):
        assert workspace == manifest["sharedWorkspaceId"]
        assert "swu-history utility=uta " in kql
        assert "ago(10m)" in kql
        return [{"Successes": successes}]

    def query(uid, kql, hours):
        assert "max(ReceivedAt)" in kql and "_GetWatchlist('SourceReceipts')" in kql
        assert "join kind=leftouter" in kql
        assert "withsource" not in kql
        assert '(SwuProcess | extend HealthFeed = "SwuProcess")' in kql
        return rows

    monkeypatch.setattr(swuharness, "query_workspace", traces)
    monkeypatch.setattr(swuharness, "query", query)
    assert swuharness.cmd_history(argparse.Namespace()) == expected
