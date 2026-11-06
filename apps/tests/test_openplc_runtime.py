"""Required CI gate: compile and run real OpenPLC, never substitute SoftPlc."""
import json
import os
import struct
import subprocess
import time
import uuid
import socket
import asyncio
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest
from pymodbus.client import ModbusTcpClient

from swuapps.registers import MAP, READ_COUNT, encode, from_block
from swugen import load_profile
from swuapps.gateway import Gateway, ReadOnlyPlc
from swuapps.plant import Plant, write_sensor_inputs
from swuapps.modbus_proxy import ModbusProxy, read_frame
from swuapps import hmi

pytestmark = pytest.mark.openplc
ROOT = Path(__file__).resolve().parents[2]


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    docker("info")
    directory = tmp_path_factory.mktemp("openplc-inputs")
    sensors = directory / "inputs.bin"
    sensors.write_bytes(struct.pack(">3H", encode(65), encode(1.08), 0))
    profile = directory / "profile.json"
    profile.write_text(json.dumps(load_profile("uta")), encoding="utf-8")
    name = "swulab-test-" + uuid.uuid4().hex[:12]
    image = name + ":test"
    subprocess.run(["docker", "build", "-t", image, str(ROOT / "apps" / "openplc")], check=True)
    client = None
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        docker("run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:1502",
               "-v", f"{directory}:/sensors:ro", "-v", f"{profile}:/config/profile.json:ro", image)
        deadline = time.monotonic() + 120
        while docker("inspect", "-f", "{{.State.Health.Status}}", name) != "healthy":
            if time.monotonic() > deadline:
                pytest.fail("Actual OpenPLC did not become healthy:\n" + docker("logs", name))
            time.sleep(1)
        assert int(docker("port", name, "1502/tcp").rsplit(":", 1)[1]) == port
        client = ModbusTcpClient("127.0.0.1", port=port)
        assert client.connect()
        yield client, sensors, port, name
    finally:
        if client:
            client.close()
        docker("rm", "-f", name)
        docker("image", "rm", image)


def wait_for(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    pytest.fail("OpenPLC state did not reach the required value")


def test_actual_openplc_input_output_scaling_and_writable_setpoints(runtime):
    client, sensors, _, _ = runtime

    def values():
        response = client.read_holding_registers(0, count=READ_COUNT)
        assert not response.isError()
        return from_block(response.registers)

    wait_for(lambda: values()["TankLevelPct"] == 65 and values()["ClResidual"] == 1.08)
    assert values()["PumpLowSetpoint"] == 40
    assert values()["PumpHighSetpoint"] == 90
    assert values()["DoseRate"] == 1.2
    write_sensor_inputs(sensors, (35, 1.08, 0))
    wait_for(lambda: values()["TankLevelPct"] == 35 and values()["PumpState"] == 1)
    inputs = client.read_input_registers(0, count=3)
    assert not inputs.isError() and inputs.registers[0] == encode(35)
    response = client.write_register(MAP["ClDoseSetpoint"], encode(12))
    assert not response.isError()
    wait_for(lambda: values()["DoseRate"] == 12)
    write_sensor_inputs(sensors, (96, 1.08, 0))
    wait_for(lambda: values()["TankLevelPct"] == 96 and values()["PumpState"] == 0)


def test_actual_openplc_outputs_drive_physics_and_gateway_edges(runtime, monkeypatch):
    client, sensors, port, _ = runtime
    profile = load_profile("uta")
    monkeypatch.setenv("SWU_SENSOR_FILE", str(sensors))
    plant = Plant(client, profile["plant"])
    plant.write_inputs(sensors)

    def values():
        result = client.read_holding_registers(0, count=READ_COUNT)
        assert not result.isError()
        return from_block(result.registers)

    for tag, value in (("PumpOverride", 0), ("PumpHighSetpoint", 90), ("ClDoseSetpoint", 1.2)):
        assert not client.write_register(MAP[tag], encode(value)).isError()
    wait_for(lambda: values()["TankLevelPct"] == round(plant.model.s.TankLevelPct, 2)
             and values()["DoseRate"] == 1.2)

    class Sink:
        def __init__(self):
            self.events = []

        def send(self, uid, batches):
            self.events.extend(batches["SimProcess_CL"])

    reader, sink = ReadOnlyPlc("127.0.0.1", port), Sink()
    gateway = Gateway(reader, sink, "uta", profile["safeLimits"])
    now = datetime.now(timezone.utc)
    try:
        gateway.poll(now)
        for tag, value in (("PumpOverride", 1), ("PumpHighSetpoint", 110), ("ClDoseSetpoint", 12)):
            assert not client.write_register(MAP[tag], encode(value)).isError()
        wait_for(lambda: values()["PumpState"] == 1 and values()["DoseRate"] == 12)
        gateway.poll(now + timedelta(seconds=1))
        for minute in range(1, 121):
            plant.step(1, 600 + minute)
            expected = {tag: encode(getattr(plant.model.s, tag))
                        for tag in ("TankLevelPct", "ClResidual", "FlowPctPerMin")}

            def inputs_match():
                snapshot = values()
                return all(encode(snapshot[tag]) == raw for tag, raw in expected.items())

            wait_for(inputs_match)
            gateway.poll(now + timedelta(minutes=minute))
            assert abs(gateway.filter.last["ClResidual"] - plant.model.s.ClResidual) <= 0.011
        out_of_range = {event["Tag"] for event in sink.events if event["EventType"] == "OutOfRange"}
        assert {"TankLevelPct", "ClResidual"} <= out_of_range
        assert any(event["EventType"] == "SetpointChange" and event["Tag"] == "ClDoseSetpoint"
                   for event in sink.events)
        assert any(event["EventType"] == "Alarm" and event["Tag"] == "TankHighHigh" for event in sink.events)
        assert all(event["ChangedBy"] == "" and event["RecordId"] for event in sink.events)
    finally:
        reader.close()


def test_actual_openplc_outage_and_restart_recovers_gateway(runtime, record_property):
    _, sensors, port, name = runtime
    write_sensor_inputs(sensors, (65, 1.08, 0))
    reader = ReadOnlyPlc("127.0.0.1", port)
    profile = load_profile("uta")

    class Sink:
        def __init__(self):
            self.events = []

        def send(self, uid, batches):
            self.events.extend(batches["SimProcess_CL"])

    sink = Sink()
    gateway = Gateway(reader, sink, "uta", profile["safeLimits"])
    seconds = int(os.environ.get("SWU_OPENPLC_OUTAGE_SECONDS", "15"))
    if seconds < 15:
        raise ValueError("Outage observation must allow at least three gateway polls")
    try:
        wait_for(lambda: from_block(reader.read_block())["TankLevelPct"] == 65)
        gateway.poll()
        docker("stop", name)
        outage_started = time.monotonic()
        deadline = outage_started + seconds
        while time.monotonic() < deadline:
            gateway.poll()
            time.sleep(5)
        assert any(event["EventType"] == "PlcUnreachable" for event in sink.events)
        record_property("outage_seconds", time.monotonic() - outage_started)
        docker("start", name)
        wait_for(lambda: docker("inspect", "-f", "{{.State.Health.Status}}", name) == "healthy", seconds=120)
        gateway.poll()
        assert gateway.failures == 0
        assert any(event["EventType"] == "Recovered" and event["Tag"] == "PLC" for event in sink.events)
        assert gateway.filter.last["TankLevelPct"] == 65
        assert gateway.filter.last["ClDoseSetpoint"] == profile["plant"]["clDoseSetpoint"]
    finally:
        reader.close()


def test_sustained_actual_plc_gateway_register_agreement(runtime, monkeypatch, record_property):
    client, sensors, port, _ = runtime
    profile = load_profile("uta")
    monkeypatch.setenv("SWU_SENSOR_FILE", str(sensors))
    client.close()
    assert client.connect()
    for tag, value in (("PumpLowSetpoint", 40), ("PumpHighSetpoint", 90),
                       ("ClDoseSetpoint", 1.2), ("PumpOverride", 0), ("Mode", 0)):
        assert not client.write_register(MAP[tag], encode(value)).isError()
    plant = Plant(client, profile["plant"])
    plant.write_inputs(sensors)
    reader = ReadOnlyPlc("127.0.0.1", port)

    class Sink:
        def send(self, uid, batches):
            pass

    gateway = Gateway(reader, Sink(), "uta", profile["safeLimits"])
    seconds = int(os.environ.get("SWU_OPENPLC_OBSERVATION_SECONDS", "15"))
    if seconds < 15:
        raise ValueError("Register observation must be at least fifteen real seconds")
    record_property("observation_seconds", seconds)
    previous = time.monotonic()
    deadline = previous + seconds
    samples = 0
    try:
        while time.monotonic() < deadline:
            now = datetime.now(timezone.utc)
            current = time.monotonic()
            plant.step((current - previous) / 60, now.hour * 60 + now.minute + now.second / 60)
            previous = current
            expected = {tag: encode(getattr(plant.model.s, tag))
                        for tag in ("TankLevelPct", "ClResidual", "FlowPctPerMin")}

            def inputs_match():
                result = client.read_holding_registers(0, count=READ_COUNT)
                assert not result.isError()
                snapshot = from_block(result.registers)
                return all(encode(snapshot[tag]) == raw for tag, raw in expected.items())

            wait_for(inputs_match)
            result = client.read_holding_registers(0, count=READ_COUNT)
            assert not result.isError()
            plc_values = from_block(result.registers)
            gateway.poll()
            assert all(abs(value - plc_values[tag]) <= 0.011 for tag, value in gateway.filter.last.items())
            assert profile["safeLimits"]["ClResidual"][0] <= plc_values["ClResidual"] <= profile["safeLimits"]["ClResidual"][1]
            samples += 1
            time.sleep(1)
        assert samples > 0
        record_property("samples", samples)
    finally:
        reader.close()


def test_actual_readonly_proxy_rejects_writes_and_force_requests(runtime):
    client, _, port, _ = runtime
    before = client.read_holding_registers(10, count=5)
    assert not before.isError()

    async def check():
        proxy = ModbusProxy("127.0.0.1", port, writers=set())
        server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        try:
            read = struct.pack(">HHHBBHH", 7, 0, 6, 1, 3, 0, READ_COUNT)
            writer.write(read)
            await writer.drain()
            response = await read_frame(reader)
            assert response[7:9] == bytes([3, READ_COUNT * 2])
            assert len(response[9:]) == READ_COUNT * 2
            for function in (5, 6, 15, 16, 0x42):
                request = struct.pack(">HHHBBHH", 7, 0, 6, 1, function, MAP["ClDoseSetpoint"], encode(12))
                writer.write(request)
                await writer.drain()
                response = await read_frame(reader)
                assert response[7:] == bytes([function | 0x80, 1])
        finally:
            writer.close()
            await writer.wait_closed()
            server.close()
            await server.wait_closed()
            await proxy.close()

    asyncio.run(check())
    after = client.read_holding_registers(10, count=5)
    assert not after.isError() and after.registers == before.registers


def test_actual_hmi_setpoint_reaches_openplc_and_gateway(runtime, monkeypatch):
    client, sensors, port, _ = runtime
    profile = load_profile("uta")
    client.close()
    assert client.connect()
    write_sensor_inputs(sensors, (65, 1.08, 0))
    assert not client.write_register(MAP["ClDoseSetpoint"], encode(1.2)).isError()

    def values():
        result = client.read_holding_registers(0, count=READ_COUNT)
        assert not result.isError()
        return from_block(result.registers)

    wait_for(lambda: values()["TankLevelPct"] == 65 and values()["DoseRate"] == 1.2)

    async def check():
        proxy = ModbusProxy("127.0.0.1", port, writers={"127.0.0.1"})
        server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        proxy_port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(hmi, "HOST", "127.0.0.1")
        monkeypatch.setattr(hmi, "PORT", proxy_port)
        records = []

        class Sink:
            def send(self, uid, batches):
                records.extend(batches["SimProcess_CL"])

        reader = ReadOnlyPlc("127.0.0.1", proxy_port)
        gateway = Gateway(reader, Sink(), "uta", profile["safeLimits"])
        web = hmi.app.test_client()
        try:
            await asyncio.to_thread(gateway.poll)
            response = await asyncio.to_thread(web.get, "/api/values")
            assert response.status_code == 200
            assert response.get_json()["ClDoseSetpoint"] == 1.2
            response = await asyncio.to_thread(web.post, "/set",
                                               data={"tag": "ClDoseSetpoint", "value": "12"})
            assert response.status_code == 302
            wait_for(lambda: values()["DoseRate"] == 12)
            await asyncio.to_thread(gateway.poll)
            assert any(event["EventType"] == "SetpointChange" and event["Tag"] == "ClDoseSetpoint"
                       and event["NewValue"] == 12 and event["ChangedBy"] == "" for event in records)
            response = await asyncio.to_thread(web.post, "/set",
                                               data={"tag": "DoseRate", "value": "12"})
            assert response.status_code == 400
            response = await asyncio.to_thread(web.post, "/set",
                                               data={"tag": "Mode", "value": "2"})
            assert response.status_code == 400
        finally:
            try:
                response = await asyncio.to_thread(web.post, "/set",
                                                   data={"tag": "ClDoseSetpoint", "value": "1.2"})
                assert response.status_code == 302
                wait_for(lambda: values()["DoseRate"] == 1.2)
            finally:
                reader.close()
                server.close()
                await server.wait_closed()
                await proxy.close()

    asyncio.run(check())
