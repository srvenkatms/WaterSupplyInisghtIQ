"""Bounded control-action diagnostics from actual application transport bytes."""
from __future__ import annotations

from datetime import datetime, timezone

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

MAX_EVENTS = 512
MAX_CHUNK = 4096


def decode_frames(data: bytes, sending: bool) -> list[dict]:
    frames = []
    while len(data) >= 8:
        length = int.from_bytes(data[4:6], "big") + 6
        if data[2:4] != b"\0\0" or not 8 <= length <= 260 or len(data) < length:
            break
        frame, data = data[:length], data[length:]
        code = frame[7]
        decoded = {"transactionId": int.from_bytes(frame[:2], "big"),
                   "unitId": frame[6], "functionCode": code}
        if code == 6 and length == 12:
            decoded.update(function="Write single register",
                           address=int.from_bytes(frame[8:10], "big"),
                           rawValue=int.from_bytes(frame[10:12], "big"))
        elif code == 3 and sending and length == 12:
            decoded.update(function="Read holding registers",
                           address=int.from_bytes(frame[8:10], "big"),
                           count=int.from_bytes(frame[10:12], "big"))
        elif code == 3 and not sending and length >= 9 and frame[8] == length - 9 and frame[8] % 2 == 0:
            decoded.update(function="Read holding registers response",
                           registers=[int.from_bytes(frame[i:i + 2], "big") for i in range(9, length, 2)])
        elif code & 0x80 and length == 9:
            decoded.update(function="Modbus exception", exceptionCode=frame[8])
        frames.append(decoded)
    return frames


class ControlTrace:
    def __init__(self, run_id: str | None = None, previous: dict | None = None):
        self.run_id = run_id
        self.phase = "baseline-read"
        previous = previous or {}
        self.events = list(previous.get("events", []))[:MAX_EVENTS]
        self.dropped = previous.get("droppedEvents", 0)

    def append(self, event: dict) -> dict | None:
        if len(self.events) >= MAX_EVENTS:
            self.dropped += 1
            return None
        event = {"sequence": len(self.events) + 1,
                 "at": datetime.now(timezone.utc).isoformat(), "phase": self.phase, **event}
        self.events.append(event)
        return event

    def packet(self, channel: str, sending: bool, data: bytes, **metadata) -> dict | None:
        return self.append({"protocol": "Modbus TCP", "channel": channel,
                            "direction": "TX" if sending else "RX",
                            "byteCount": len(data), "hex": data[:MAX_CHUNK].hex(" ").upper(),
                            "chunkTruncated": len(data) > MAX_CHUNK,
                            "frames": decode_frames(data[:MAX_CHUNK], sending), **metadata})

    def error(self, channel: str, operation: str, error: Exception) -> None:
        self.append({"protocol": "Modbus TCP", "channel": channel, "direction": "ERROR",
                     "operation": operation, "errorType": type(error).__name__})

    def import_hmi(self, document: dict) -> None:
        self.dropped += document.get("droppedEvents", 0)
        for event in document.get("events", [])[:MAX_EVENTS]:
            if event["protocol"] != "Modbus TCP" or event["channel"] != "hmi-to-plc":
                raise ValueError("Unexpected HMI trace protocol")
            if event["direction"] in ("TX", "RX"):
                raw = bytes.fromhex(event["hex"])
                self.packet("hmi-to-plc", event["direction"] == "TX", raw, at=event["at"],
                            status=event.get("status", "received"), sentBytes=event.get("sentBytes"),
                            chunkTruncated=event.get("chunkTruncated", False))
            elif event["direction"] == "ERROR":
                self.append({key: event[key] for key in
                             ("protocol", "channel", "direction", "at", "operation", "errorType")})
            else:
                raise ValueError("Unexpected HMI trace direction")

    def document(self) -> dict:
        return {"runId": self.run_id, "events": list(self.events), "droppedEvents": self.dropped,
                "capture": "Application transport bytes, not an IP/TCP packet capture. TX socket acceptance "
                           "is not PLC acknowledgement; FC06 acknowledgement is not subsequent state readback.",
                "registerAddressing": "Zero-based addresses; engineering values use raw register value / 100."}


class TracedModbusClient(ModbusTcpClient):
    """The pinned synchronous pymodbus 3.8.6 does not invoke its RX trace hook."""
    def __init__(self, *args, **kwargs):
        self.control_trace: ControlTrace | None = None
        self.trace_channel = "worker-to-plc"
        super().__init__(*args, **kwargs)

    def send(self, request: bytes, addr=None) -> int:
        trace = self.control_trace
        event = trace.packet(self.trace_channel, True, request, status="send-attempt") if trace else None
        try:
            sent = super().send(request, addr)
        except (ModbusException, OSError) as error:
            if event is not None:
                event["status"] = "send-failed-or-unknown"
            if trace:
                trace.error(self.trace_channel, "send", error)
            raise
        if event is not None:
            event.update(status="socket-accepted" if sent == len(request) else "partial-send", sentBytes=sent)
        return sent

    def recv(self, size: int | None) -> bytes:
        try:
            data = super().recv(size)
        except (ModbusException, OSError) as error:
            if self.control_trace:
                self.control_trace.error(self.trace_channel, "receive", error)
            raise
        if self.control_trace and data:
            self.control_trace.packet(self.trace_channel, False, data, status="received")
        return data
