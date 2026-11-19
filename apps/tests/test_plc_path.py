"""Integration tests for Utility A's PLC data path, run against a real Modbus
TCP server on localhost: TC-15, TC-16, TC-18 and TC-19 in miniature."""
import asyncio
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from pymodbus.client import ModbusTcpClient

from swugen import load_profile
from swuapps.gateway import Gateway, ReadOnlyPlc
from swuapps.plant import Plant
from swuapps.registers import MAP, encode
from swuapps.softplc import SoftPlc

PROFILE = load_profile("uta")
T0 = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PlcThread:
    """Runs the soft PLC's Modbus server in a background event loop."""

    def __init__(self, port):
        self.port, self.loop = port, asyncio.new_event_loop()
        self.plc = None
        ready = threading.Event()

        async def start():
            self.plc = SoftPlc(PROFILE["plant"], port=port, host="127.0.0.1")
            self.task = asyncio.ensure_future(self.plc.make_server().serve_forever())

        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(start())
            ready.set()
            self.loop.run_forever()
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        ready.wait(5)
        for _ in range(50):          # wait until the port accepts connections
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)

    def stop(self):
        fut = asyncio.run_coroutine_threadsafe(self.plc.server.shutdown(), self.loop)
        fut.result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


class ListSink:
    def __init__(self):
        self.events = []

    def send(self, uid, batches):
        self.events.extend(batches["SimProcess_CL"])


@pytest.fixture
def plc():
    t = PlcThread(free_port())
    yield t
    t.stop()


def run_loop(plc, plant, gw, minutes, start, dt_min=1.0):
    """Advance plant and PLC by dt_min simulated minutes per step; poll the gateway each step."""
    events, now = [], start
    for _ in range(int(minutes / dt_min)):
        plc.plc.scan()
        plant.step(dt_min, now.hour * 60 + now.minute)
        plc.plc.scan()
        events += gw.poll(now)
        now += timedelta(minutes=dt_min)
    return events, now


def test_tc15_gateway_readings_match_plc_registers(plc):
    plant = Plant(ModbusTcpClient("127.0.0.1", port=plc.port), PROFILE["plant"])
    plant.client.connect()
    sink = ListSink()
    gw = Gateway(ReadOnlyPlc("127.0.0.1", plc.port), sink, "uta", PROFILE["safeLimits"])
    events, _ = run_loop(plc, plant, gw, 60, T0)
    hb = [e for e in events if e["EventType"] == "Heartbeat"]
    assert len(hb) >= 4 * 4                       # 4 tags, every 15 minutes
    gw.poll(T0 + timedelta(hours=2))               # one more read, then compare every tag
    for tag in ("TankLevelPct", "ClResidual", "PumpState", "PumpLowSetpoint", "PumpHighSetpoint", "ClDoseSetpoint"):
        assert gw.filter.last[tag] == plc.plc.read(tag), tag
    assert all(e["UtilityId"] == "uta" and e["ChangedBy"] == "" for e in events)


def test_tc16_gateway_sends_only_read_requests(plc):
    plc.plc.function_codes.clear()
    gw = Gateway(ReadOnlyPlc("127.0.0.1", plc.port), ListSink(), "uta", PROFILE["safeLimits"])
    for i in range(10):
        gw.poll(T0 + timedelta(seconds=5 * i))
    assert plc.plc.function_codes and set(plc.plc.function_codes) == {3}
    assert not hasattr(gw.plc, "write_register") and not hasattr(gw.plc, "write_registers")


def test_tc18_plc_outage_raises_unreachable_then_recovers():
    port = free_port()
    plc = PlcThread(port)
    sink = ListSink()
    gw = Gateway(ReadOnlyPlc("127.0.0.1", port), sink, "uta", PROFILE["safeLimits"])
    gw.poll(T0)
    plc.stop()
    for i in range(1, 5):
        gw.poll(T0 + timedelta(seconds=5 * i))
    unreachable = [e for e in sink.events if e["EventType"] == "PlcUnreachable"]
    assert len(unreachable) == 1, "raised once, after three failed polls"
    plc2 = PlcThread(port)
    try:
        assert gw.poll(T0 + timedelta(minutes=20))   # heartbeat due again after recovery
        assert gw.failures == 0
    finally:
        plc2.stop()


def test_tc19_real_modbus_write_is_seen_in_process_data(plc):
    plant = Plant(ModbusTcpClient("127.0.0.1", port=plc.port), PROFILE["plant"])
    plant.client.connect()
    sink = ListSink()
    gw = Gateway(ReadOnlyPlc("127.0.0.1", plc.port), sink, "uta", PROFILE["safeLimits"])
    _, now = run_loop(plc, plant, gw, 30, T0)
    attacker = ModbusTcpClient("127.0.0.1", port=plc.port)      # SCN-08 / SCN-04 as real writes
    attacker.connect()
    attacker.write_register(MAP["PumpHighSetpoint"], encode(110))
    attacker.write_register(MAP["PumpOverride"], encode(1))
    attacker.close()
    events, _ = run_loop(plc, plant, gw, 120, now)
    kinds = {(e["EventType"], e["Tag"]) for e in events}
    assert ("SetpointChange", "PumpHighSetpoint") in kinds
    assert ("SetpointChange", "PumpOverride") in kinds
    assert ("OutOfRange", "TankLevelPct") in kinds, "tank overflows because the pump is forced on"
    assert ("Alarm", "TankHighHigh") in kinds
    change = next(e for e in events if e["EventType"] == "SetpointChange")
    assert change["ChangedBy"] == ""                  # FR-31: a PLC does not record who
