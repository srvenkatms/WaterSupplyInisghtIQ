import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

import function_app as functions
from azure.core.exceptions import ResourceExistsError
from swugen import engine, load_profile

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)
CALLBACKS = {function.get_function_name(): function.get_user_function()
             for function in functions.app.get_functions()}


def callback(name):
    return CALLBACKS[name]


def test_queue_binding_encoding_matches_harness_policy():
    root = Path(__file__).resolve().parents[2]
    host = json.loads((root / "generator" / "host.json").read_text())
    assert host["extensions"]["queues"]["messageEncoding"] == "base64"
    assert "TextBase64EncodePolicy()" in (root / "harness" / "swuharness.py").read_text()


def test_queue_admission_validates_and_persists_request(monkeypatch):
    uploads = []

    class Container:
        def upload_blob(self, name, content, overwrite):
            uploads.append((name, json.loads(content), overwrite))

    class Message:
        def get_body(self):
            return json.dumps({"scenario": "SCN-01", "utility": "uta", "runId": "run-123",
                               "start": NOW.isoformat()}).encode()

    monkeypatch.setattr(functions, "_blobs", lambda: Container())
    callback("scenario")(Message())
    assert uploads[0][0] == "requests/uta/run-123.json"
    assert uploads[0][1]["start"] == NOW.isoformat()


def test_one_utility_failure_does_not_prevent_other_utilities(monkeypatch):
    visited = []
    monkeypatch.setenv("SWU_UTILITIES", json.dumps([{"utilityId": uid} for uid in ("uta", "utb", "utc")]))
    monkeypatch.setattr(functions, "_blobs", lambda: object())
    monkeypatch.setattr(functions.IngestionSink, "from_env", lambda: object())
    monkeypatch.setattr(functions, "AccountStateSink", lambda *args: object())

    def generate(uid, *args):
        visited.append(uid)
        if uid == "uta":
            raise ConnectionError("Synthetic ingestion failure")

    monkeypatch.setattr(functions, "generate_utility", generate)
    with pytest.raises(ExceptionGroup):
        callback("generate")(None)
    assert visited == ["uta", "utb", "utc"]


def test_history_refresh_failure_is_logged_without_blocking_other_utilities(monkeypatch, caplog):
    visited = []
    monkeypatch.setenv("SWU_UTILITIES", json.dumps([{"utilityId": uid} for uid in ("uta", "utb", "utc")]))

    class Container:
        def get_blob_client(self, path):
            return path

    class History:
        def refresh(self, uid, state, save):
            visited.append(uid)
            if uid == "uta":
                raise ConnectionError("synthetic history query failure")
            return 0

    @contextmanager
    def leased(blob, initial):
        assert blob.startswith("history/")
        yield {}, lambda state: None

    monkeypatch.setattr(functions, "_blobs", Container)
    monkeypatch.setattr(functions, "DetectionHistorySink", lambda *args: History())
    monkeypatch.setattr(functions, "leased_state", leased)
    with pytest.raises(ExceptionGroup):
        callback("detection_history")(None)
    assert visited == ["uta", "utb", "utc"]
    assert "swu-history failed for uta" in caplog.text


def test_container_creation_race_reuses_existing_container(monkeypatch):
    class Container:
        def exists(self):
            return False

        def create_container(self):
            raise ResourceExistsError("Synthetic concurrent creation")

    container = Container()

    class Service:
        def __init__(self, *args, **kwargs):
            pass

        def get_container_client(self, name):
            assert name == functions.CONTAINER
            return container

    monkeypatch.setenv("AzureWebJobsStorage__accountName", "syntheticstorage")
    monkeypatch.setattr(functions, "BlobServiceClient", Service)
    monkeypatch.setattr(functions, "DefaultAzureCredential", lambda: object())
    assert functions._blobs() is container


def test_initialization_failure_is_logged_and_raised(monkeypatch, caplog):
    def fail():
        raise ConnectionError("Synthetic storage unavailable")

    monkeypatch.setattr(functions, "_blobs", fail)
    with pytest.raises(ConnectionError, match="storage unavailable"):
        callback("generate")(None)
    assert "swu-generate failed during initialization" in caplog.text


@pytest.mark.parametrize("acceleration", ["not-a-number", "0", "61"])
def test_invalid_acceleration_is_logged_before_storage_access(monkeypatch, caplog, acceleration):
    monkeypatch.setenv("SWU_TIME_ACCELERATION", acceleration)
    monkeypatch.setattr(functions, "_blobs", lambda: pytest.fail("Invalid acceleration accessed storage"))
    with pytest.raises(ValueError):
        callback("generate")(None)
    assert "swu-generate failed during initialization" in caplog.text


def test_malformed_request_is_journaled_without_blocking_generation(monkeypatch):
    state = engine.new_state(load_profile("uta"), NOW)
    saves, deleted = [], []

    @contextmanager
    def leased(*args):
        yield state, lambda current: saves.append(json.loads(json.dumps(current)))

    class Body:
        def readall(self):
            return b'{"runId": "run-invalid", "start": null}'

    class Container:
        def get_blob_client(self, path):
            return object()

        def list_blobs(self, **kwargs):
            return [type("Blob", (), {"name": "requests/uta/run-invalid.json"})()]

        def download_blob(self, path):
            return Body()

        def delete_blob(self, path):
            deleted.append(path)

    monkeypatch.setattr(functions, "leased_state", leased)
    monkeypatch.setattr(functions.delivery, "flush", lambda *args: {})
    prepared = []
    monkeypatch.setattr(functions.delivery, "prepare", lambda *args: prepared.append(True))
    functions.generate_utility("uta", load_profile("uta"), {"uta": load_profile("uta")},
                               Container(), object(), object(), NOW, 1)
    assert "requests/uta/run-invalid.json" in state["rejectedRuns"]
    assert deleted == ["requests/uta/run-invalid.json"]
    assert saves and prepared == [True]
