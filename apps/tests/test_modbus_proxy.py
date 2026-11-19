import asyncio
import socket
import struct
from pathlib import Path

import pytest

from swuapps.modbus_proxy import ModbusProxy, permitted, read_frame
from swuapps.registers import encode
from swuapps.spool import EventSpool


def frame(function, address=10, value=4000):
    return struct.pack(">HHHBBHH", 7, 0, 6, 1, function, address, value)


def test_gateway_origin_cannot_write_any_function_or_debug_force():
    assert permitted(frame(3, 0, 22), writer=False)
    for function in (5, 6, 15, 16, 0x42):
        assert not permitted(frame(function), writer=False)
    assert permitted(frame(6), writer=True)
    assert not permitted(frame(6, 20), writer=True)
    assert not permitted(frame(3, 0, 23), writer=False)
    assert not permitted(frame(3, 0, 0), writer=False)


def test_network_write_rejected_without_contacting_upstream():
    async def check():
        contacts = []

        async def upstream(reader, writer):
            contacts.append(True)
            writer.close()
            await writer.wait_closed()

        upstream_server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        upstream_port = upstream_server.sockets[0].getsockname()[1]
        proxy = ModbusProxy("127.0.0.1", upstream_port, writers=set())
        server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
            request = frame(6)
            writer.write(request[:3])
            await writer.drain()
            writer.write(request[3:])
            await writer.drain()
            response = await read_frame(reader)
            assert response[7:] == bytes([0x86, 1])
            assert not contacts
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            upstream_server.close()
            await server.wait_closed()
            await upstream_server.wait_closed()

    asyncio.run(check())


def test_spool_retains_events_through_transport_failure_and_restart(tmp_path):
    path = tmp_path / "outbox.sqlite"
    spool = EventSpool(path)
    spool.append([{"Tag": "TankLevelPct", "ScenarioRunId": "run-123"}])

    class FailedSink:
        def send(self, uid, batches):
            raise ConnectionError("ingestion unavailable")

    with pytest.raises(ConnectionError):
        spool.flush("uta", FailedSink())
    spool.connection.close()
    reopened = EventSpool(path)
    received = []

    class Sink:
        def send(self, uid, batches):
            received.extend(batches["SimProcess_CL"])

    reopened.flush("uta", Sink())
    assert received == [{"Tag": "TankLevelPct", "ScenarioRunId": "run-123"}]
    assert reopened.connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    reopened.connection.close()


def test_proxy_serializes_concurrent_clients_over_one_upstream_connection():
    async def check():
        contacts, requests = [], []

        async def upstream(reader, writer):
            contacts.append(True)
            try:
                while True:
                    request = await read_frame(reader)
                    requests.append(request)
                    await asyncio.sleep(0.005)
                    writer.write(request)
                    await writer.drain()
            except asyncio.IncompleteReadError:
                pass
            finally:
                writer.close()
                await writer.wait_closed()

        upstream_server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        proxy = ModbusProxy("127.0.0.1", upstream_server.sockets[0].getsockname()[1], {"127.0.0.1"})
        server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)

        async def client(number):
            reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
            try:
                for index in range(10):
                    request = struct.pack(">H", number * 10 + index) + frame(6)[2:]
                    writer.write(request)
                    await writer.drain()
                    assert await read_frame(reader) == request
            finally:
                writer.close()
                await writer.wait_closed()

        try:
            await asyncio.wait_for(asyncio.gather(*(client(n) for n in range(5))), 10)
            assert len(contacts) == 1
            assert len(requests) == 50
        finally:
            server.close()
            await server.wait_closed()
            await proxy.close()
            upstream_server.close()
            await upstream_server.wait_closed()

    asyncio.run(check())


def test_proxy_does_not_replay_a_write_after_upstream_response_is_lost():
    async def check():
        requests = []

        async def upstream(reader, writer):
            try:
                request = await read_frame(reader)
                requests.append(request)
                if len(requests) > 1:
                    writer.write(request)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        upstream_server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        proxy = ModbusProxy("127.0.0.1", upstream_server.sockets[0].getsockname()[1], {"127.0.0.1"})
        try:
            with pytest.raises(asyncio.IncompleteReadError):
                await proxy.forward(frame(6))
            assert requests == [frame(6)]
            assert proxy.upstream_writer is None
            assert await proxy.forward(frame(6, 10, 4500)) == frame(6, 10, 4500)
            assert len(requests) == 2
        finally:
            await proxy.close()
            upstream_server.close()
            await upstream_server.wait_closed()

    asyncio.run(check())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 655.36])
def test_invalid_register_values_are_not_silently_clamped(value):
    with pytest.raises(ValueError):
        encode(value)
