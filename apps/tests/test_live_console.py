from datetime import datetime, timedelta, timezone
import io
import json
import uuid

from azure.core.exceptions import ResourceNotFoundError
from flask import Flask
import pytest

from swuapps import live_console


class Blobs:
    def __init__(self):
        self.data = {}

    def upload_blob(self, name, value, overwrite):
        self.data[name] = value

    def download_blob(self, name):
        if name not in self.data:
            error = ResourceNotFoundError("missing")
            error.error_code = "BlobNotFound"
            raise error
        return type("Download", (), {"readall": lambda _: self.data[name].encode()})()


@pytest.fixture
def live(monkeypatch):
    states = {route: Blobs() for route in live_console.ROUTES}
    commands = {route: [] for route in live_console.ROUTES}
    monkeypatch.setenv("SWU_ROUTE_WORKSPACES", json.dumps({route: str(uuid.uuid4()) for route in states}))
    monkeypatch.setattr(live_console, "ContainerClient", lambda url, name, *args, **kwargs: states[name[:-6]])
    monkeypatch.setattr(live_console, "QueueClient", lambda url, name, *args, **kwargs: type(
        "Queue", (), {"send_message": lambda self, value, **kw: commands[name[:-9]].append(json.loads(value))})())
    app = Flask(__name__)
    app.testing = True
    own = Blobs()
    credential = type("Credential", (), {"get_token": lambda self, _: type("Token", (), {"token": "test-token"})()})()
    live_console.register_live_routes(app, credential, "https://example.blob.core.windows.net", own)
    return app.test_client(), states, commands, own


def test_missing_and_stale_observations_do_not_claim_health(live):
    client, states, _, _ = live
    response = client.get("/api/live/routes").get_json()
    assert all(not route["ready"] and route["stale"] for route in response["routes"])
    states["routea"].data["latest.json"] = json.dumps({
        "route": "routea", "observedAt": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()})
    assert client.get("/api/live/routes").get_json()["routes"][0]["stale"] is True
    assert client.post("/api/live/commands", json={
        "route": "routea", "action": "pump-on", "confirmed": True}).status_code == 409


def test_fixed_confirmed_dispatch_is_queued_not_executed(live):
    client, states, commands, own = live
    states["routeb"].data["latest.json"] = json.dumps({
        "route": "routeb", "observedAt": datetime.now(timezone.utc).isoformat(), "activeRunId": None})
    body = {"route": "routeb", "action": "chlorine-up", "confirmed": True}
    assert client.post("/api/live/commands", json={**body, "confirmed": False}).status_code == 409
    assert client.post("/api/live/commands", json={**body, "host": "arbitrary"}).status_code == 400
    actor = str(uuid.uuid4())
    response = client.post("/api/live/commands", json=body, headers={"X-MS-CLIENT-PRINCIPAL-ID": actor})
    assert response.status_code == 202
    result = response.get_json()
    assert result["physicalActionsExecuted"] is False
    assert commands["routeb"][0]["actor"] == actor
    assert not states["routeb"].data.get(f"jobs/{result['runId']}.json")
    assert client.get(f"/api/live/commands/routeb/{result['runId']}").get_json()["stage"] == "queued"
    assert own.data


def test_evidence_requires_actual_query_records(live, monkeypatch):
    client, states, _, _ = live
    identity = str(uuid.uuid4())
    states["routea"].data[f"jobs/{identity}.json"] = json.dumps({"runId": identity, "stage": "completed-restored"})
    counts = [0, 0]

    def response(request, timeout):
        assert identity in json.loads(request.data)["query"]
        return io.BytesIO(json.dumps({"tables": [{"columns": [{"name": "Kind"}, {"name": "Records"}],
                       "rows": [["lab-action-audit", counts[0]], ["correlated-alert", counts[1]]]}]}).encode())

    monkeypatch.setattr(live_console, "urlopen", response)
    url = f"/api/live/commands/routea/{identity}/evidence"
    assert client.get(url).get_json()["sentinel"] == "queried-no-records"
    counts[0] = 3
    result = client.get(url).get_json()
    assert result["sentinel"] == "ingested"
    assert result["alert"] == "queried-no-correlated-alert"
    assert result["nativeEntraDefenderFirewallFeeds"] is False


def test_evidence_timeout_reports_unavailable_without_success(live, monkeypatch):
    client, states, _, _ = live
    identity = str(uuid.uuid4())
    states["routea"].data[f"jobs/{identity}.json"] = json.dumps({"runId": identity, "stage": "completed-restored"})

    def unavailable(request, timeout):
        raise TimeoutError("Sentinel query timed out")

    monkeypatch.setattr(live_console, "urlopen", unavailable)
    response = client.get(f"/api/live/commands/routea/{identity}/evidence")
    assert response.status_code == 503
    assert "error" in response.get_json()
    assert "sentinel" not in response.get_json()
