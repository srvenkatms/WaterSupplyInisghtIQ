import copy
import json
from types import SimpleNamespace

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError

from swuapps.console import BlobSettingsStore, DEFAULTS, SettingsConflict


class Blob:
    def __init__(self):
        self.data = None
        self.etag = 0
        self.missing_code = "BlobNotFound"
        self.race = False

    def download_blob(self):
        if self.data is None:
            error = ResourceNotFoundError("missing")
            error.error_code = self.missing_code
            raise error
        return SimpleNamespace(readall=lambda: self.data, properties=SimpleNamespace(etag=str(self.etag)))

    def upload_blob(self, data, overwrite=False, **conditions):
        if not overwrite and self.data is not None:
            raise ResourceExistsError("already created")
        if overwrite:
            assert conditions["match_condition"] == MatchConditions.IfNotModified
            if self.race or conditions["etag"] != str(self.etag):
                raise ResourceModifiedError("concurrent update")
        self.data = data
        self.etag += 1


class Container:
    def __init__(self):
        self.blob = Blob()
        self.runs = {}

    def get_blob_client(self, name):
        assert name == "settings.json"
        return self.blob

    def upload_blob(self, name, data, overwrite):
        assert overwrite is False
        self.runs[name] = data


def test_private_blob_settings_survive_replicas_and_reject_stale_revisions():
    container = Container()
    first = BlobSettingsStore(container)
    second = BlobSettingsStore(container)
    assert first.read() == {"revision": 0, "settings": DEFAULTS}
    settings = copy.deepcopy(DEFAULTS)
    settings["utilityId"] = "utc"
    saved = first.save(settings, 0)
    assert second.read() == saved
    with pytest.raises(SettingsConflict):
        second.save(DEFAULTS, 0)
    assert second.save(DEFAULTS, 1)["revision"] == 2


def test_blob_etag_guards_a_race_after_the_revision_read():
    container = Container()
    store = BlobSettingsStore(container)
    store.save(DEFAULTS, 0)
    before = container.blob.data
    container.blob.race = True
    with pytest.raises(SettingsConflict, match="another replica"):
        store.save(DEFAULTS, 1)
    assert container.blob.data == before


def test_missing_container_and_corrupt_blob_are_not_silent_defaults():
    container = Container()
    store = BlobSettingsStore(container)
    container.blob.missing_code = "ContainerNotFound"
    with pytest.raises(ResourceNotFoundError):
        store.read()
    container.blob.data = b"{broken"
    with pytest.raises(ValueError):
        store.save(DEFAULTS, 0)
    assert container.blob.data == b"{broken"


def test_generated_runs_are_uploaded_to_the_console_container(tmp_path):
    container = Container()
    (tmp_path / "uta.SimSignin_CL.jsonl").write_text('{"synthetic":true}\n')
    (tmp_path / "result.json").write_text(json.dumps({"acceptanceCertified": False}))
    BlobSettingsStore(container).persist_run(tmp_path, "console-run")
    assert set(container.runs) == {"runs/console-run/uta.SimSignin_CL.jsonl", "runs/console-run/result.json"}
