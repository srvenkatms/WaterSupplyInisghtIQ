import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

import swuapps.plant as plant
from swuapps.registers import encode


def test_atomic_publish_retries_transient_windows_reader_lock(tmp_path, monkeypatch):
    path = tmp_path / "inputs.bin"
    original = Path.replace
    attempts = []

    def replace(self, target):
        attempts.append(True)
        if len(attempts) < 3:
            raise PermissionError("Synthetic Windows reader lock")
        return original(self, target)

    monkeypatch.setattr(plant, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(Path, "replace", replace)
    plant.write_sensor_inputs(path, (65, 1.08, 0))
    assert len(attempts) == 3
    assert path.read_bytes() == struct.pack(">3H", encode(65), encode(1.08), 0)


def test_publish_does_not_hide_permanent_unix_permission_error(tmp_path, monkeypatch):
    def replace(*args):
        raise PermissionError("Synthetic permission failure")

    monkeypatch.setattr(plant, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(Path, "replace", replace)
    with pytest.raises(PermissionError):
        plant.write_sensor_inputs(tmp_path / "inputs.bin", (65, 1.08, 0))
