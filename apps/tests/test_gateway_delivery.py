from datetime import datetime, timedelta, timezone

import pytest
from pymodbus.exceptions import ConnectionException, ModbusIOException

from swugen import load_profile
from swuapps.gateway import Gateway
from swuapps.registers import MAP, READ_COUNT, encode

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


class Plc:
    def __init__(self):
        self.fail = False
        self.values = {"TankLevelPct": 65, "ClResidual": 1.2, "PumpLowSetpoint": 40,
                       "PumpHighSetpoint": 90, "ClDoseSetpoint": 1.2}

    def read_block(self):
        if self.fail:
            raise ConnectionError("Synthetic PLC outage")
        block = [0] * READ_COUNT
        for tag, value in self.values.items():
            block[MAP[tag]] = encode(value)
        return block

    def close(self):
        pass


class Sink:
    def __init__(self):
        self.records = []
        self.fail = False

    def send(self, uid, batches):
        if self.fail:
            raise ConnectionError("Ingestion unavailable")
        self.records.extend(batches["SimProcess_CL"])


def gateway(plc, sink, path):
    return Gateway(plc, sink, "uta", load_profile("uta")["safeLimits"], state_dir=path)


@pytest.mark.parametrize("error_type", [ConnectionException, ModbusIOException])
def test_modbus_failure_emits_unreachable_then_recovers(error_type, monkeypatch):
    plc, sink = Plc(), Sink()
    gw = gateway(plc, sink, None)
    gw.poll(NOW)
    closed = []

    def fail():
        raise error_type("Synthetic Modbus transport failure")

    with monkeypatch.context() as patch:
        patch.setattr(plc, "read_block", fail)
        patch.setattr(plc, "close", lambda: closed.append(True))
        for i in range(1, 5):
            gw.poll(NOW + timedelta(seconds=5 * i))

    assert len(closed) == 4
    assert gw.failures == 4
    assert len([e for e in sink.records if e["EventType"] == "PlcUnreachable"]) == 1
    recovered = gw.poll(NOW + timedelta(seconds=25))
    assert any(e["Tag"] == "PLC" and e["EventType"] == "Recovered" for e in recovered)
    assert gw.failures == 0 and gw.last_unreachable is None


def test_append_failure_rolls_back_edge_state(tmp_path, monkeypatch):
    plc, sink = Plc(), Sink()
    gw = gateway(plc, sink, tmp_path)
    gw.poll(NOW)
    plc.values["ClDoseSetpoint"] = 6
    original = gw.spool.append

    def fail(*args):
        raise OSError("Disk unavailable")

    monkeypatch.setattr(gw.spool, "append", fail)
    with pytest.raises(OSError):
        gw.poll(NOW + timedelta(seconds=5))
    assert gw.filter.last["ClDoseSetpoint"] == 1.2
    monkeypatch.setattr(gw.spool, "append", original)
    events = gw.poll(NOW + timedelta(seconds=10))
    assert any(event["EventType"] == "SetpointChange" for event in events)
    gw.spool.connection.close()


def test_transport_failure_retains_checkpoint_and_replays_after_restart(tmp_path):
    plc, sink = Plc(), Sink()
    gw = gateway(plc, sink, tmp_path)
    gw.poll(NOW)
    plc.values["ClDoseSetpoint"] = 6
    sink.fail = True
    with pytest.raises(ConnectionError):
        gw.poll(NOW + timedelta(seconds=5))
    assert gw.filter.last["ClDoseSetpoint"] == 6
    pending = gw.spool.connection.execute("SELECT payload FROM outbox").fetchall()
    assert len(pending) == 1
    gw.spool.connection.close()
    restarted = gateway(plc, sink, tmp_path)
    sink.fail = False
    assert restarted.poll(NOW + timedelta(seconds=10)) == []
    changes = [event for event in sink.records if event["EventType"] == "SetpointChange"]
    assert len(changes) == 1
    assert changes[0]["RecordId"]
    assert restarted.spool.connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    restarted.spool.connection.close()


def test_unreachable_checkpoint_failure_retries_and_recovery_survives_restart(tmp_path, monkeypatch):
    plc, sink = Plc(), Sink()
    gw = gateway(plc, sink, tmp_path)
    gw.poll(NOW)
    plc.fail = True
    gw.poll(NOW + timedelta(seconds=5))
    gw.poll(NOW + timedelta(seconds=10))
    original = gw.spool.append

    def fail(*args):
        raise OSError("Synthetic checkpoint failure")

    monkeypatch.setattr(gw.spool, "append", fail)
    with pytest.raises(OSError):
        gw.poll(NOW + timedelta(seconds=15))
    assert gw.failures == 2 and gw.last_unreachable is None
    monkeypatch.setattr(gw.spool, "append", original)
    assert gw.poll(NOW + timedelta(seconds=20))[0]["EventType"] == "PlcUnreachable"
    gw.spool.connection.close()
    restarted = gateway(plc, sink, tmp_path)
    assert restarted.failures == 3 and restarted.last_unreachable == NOW + timedelta(seconds=20)
    assert restarted.poll(NOW + timedelta(seconds=25)) == []
    plc.fail = False
    recovered = restarted.poll(NOW + timedelta(seconds=30))
    assert any(event["Tag"] == "PLC" and event["EventType"] == "Recovered" for event in recovered)
    assert restarted.failures == 0 and restarted.last_unreachable is None
    restarted.spool.connection.close()


def test_invalid_control_file_does_not_lose_observed_edge(tmp_path):
    plc, sink = Plc(), Sink()
    gw = gateway(plc, sink, tmp_path)
    gw.poll(NOW)
    plc.values["ClDoseSetpoint"] = 6
    control = tmp_path / "control.json"
    control.write_text("malformed json")
    with pytest.raises(ValueError):
        gw.poll(NOW + timedelta(seconds=5))
    assert gw.filter.last["ClDoseSetpoint"] == 1.2
    control.unlink()
    assert any(event["EventType"] == "SetpointChange" for event in gw.poll(NOW + timedelta(seconds=10)))
    gw.spool.connection.close()
