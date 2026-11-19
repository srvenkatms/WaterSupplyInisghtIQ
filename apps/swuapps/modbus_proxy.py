"""Enforce read-only gateway access at the PLC, independent of client code."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import struct

from .registers import MAP, READ_COUNT, SETPOINTS

LOG = logging.getLogger(__name__)


def permitted(frame: bytes, writer: bool) -> bool:
    if len(frame) < 12 or frame[7] not in {3, 6, 16}:
        return False
    address, value = struct.unpack(">HH", frame[8:12])
    if frame[7] == 3:
        return len(frame) == 12 and 0 < value <= READ_COUNT and address + value <= READ_COUNT
    if not writer:
        return False
    writable = {MAP[tag] for tag in SETPOINTS}
    if frame[7] == 6:
        return len(frame) == 12 and address in writable
    count = value
    return (len(frame) == 13 + count * 2 and frame[12] == count * 2
            and 0 < count <= len(writable) and all(i in writable for i in range(address, address + count)))


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    header = await asyncio.wait_for(reader.readexactly(7), timeout=10)
    _, protocol, length, unit = struct.unpack(">HHHB", header)
    if protocol != 0 or not 2 <= length <= 254 or unit != 1:
        raise ValueError("Invalid Modbus TCP header")
    return header + await asyncio.wait_for(reader.readexactly(length - 1), timeout=10)


class ModbusProxy:
    def __init__(self, upstream_host: str, upstream_port: int, writers: set[str]):
        self.host, self.port = upstream_host, upstream_port
        self.writers = {str(ipaddress.ip_address(ip)) for ip in writers}
        self.lock = asyncio.Lock()
        self.upstream_reader = None
        self.upstream_writer = None

    async def close(self):
        if self.upstream_writer is not None:
            writer = self.upstream_writer
            self.upstream_reader = self.upstream_writer = None
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                LOG.warning("Upstream Modbus connection failed while closing", exc_info=True)

    async def forward(self, frame):
        # One serialized connection avoids per-request accept/thread churn in OpenPLC.
        async with self.lock:
            try:
                if self.upstream_writer is None:
                    self.upstream_reader, self.upstream_writer = await asyncio.wait_for(
                        asyncio.open_connection(self.host, self.port), timeout=3)
                self.upstream_writer.write(frame)
                await self.upstream_writer.drain()
                response = await read_frame(self.upstream_reader)
                if response[:4] != frame[:4]:
                    raise ValueError("Modbus response transaction does not match")
                return response
            except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError):
                await self.close()
                # A write may already have reached the PLC; never replay it here.
                raise

    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername")[0]
        try:
            while True:
                frame = await read_frame(reader)
                if not permitted(frame, peer in self.writers):
                    LOG.warning("Rejected Modbus function %s from %s", frame[7], peer)
                    response = frame[:4] + struct.pack(">HB", 3, frame[6]) + bytes([frame[7] | 0x80, 1])
                else:
                    response = await self.forward(frame)
                writer.write(response)
                await writer.drain()
        except asyncio.IncompleteReadError:
            LOG.debug("Modbus client %s disconnected", peer)
        except (OSError, ValueError, TimeoutError):
            LOG.exception("Modbus proxy request failed for %s", peer)
        finally:
            writer.close()
            await writer.wait_closed()


async def serve():
    writers = set(filter(None, os.environ["SWU_MODBUS_WRITERS"].split(",")))
    proxy = ModbusProxy("127.0.0.1", int(os.environ.get("SWU_UPSTREAM_PORT", "1502")), writers)
    server = await asyncio.start_server(proxy.handle, "0.0.0.0", int(os.environ.get("SWU_PROXY_PORT", "502")))
    try:
        async with server:
            await server.serve_forever()
    finally:
        await proxy.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())
