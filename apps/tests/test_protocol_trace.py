import io
import json
import socket
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest
from werkzeug.security import generate_password_hash

from swuapps import hmi, route_worker
from swuapps.protocol_trace import ControlTrace, TracedModbusClient, MAX_EVENTS, decode_frames


@pytest.fixture
def wire_server():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.1)
    stopped = threading.Event()
    requests, responses, registers = [], [], [0] * 22
    connections = []
    def serve():
        while not stopped.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            connections.append(connection)
            connection.settimeout(0.1)
            buffer = b""
            with connection:
                while not stopped.is_set():
                    try:
                        chunk = connection.recv(4096)
                    except TimeoutError:
                        continue
                    if not chunk:
                        break
                    buffer += chunk
                    while len(buffer) >= 6 and len(buffer) >= int.from_bytes(buffer[4:6], "big") + 6:
                        size = int.from_bytes(buffer[4:6], "big") + 6
                        raw, buffer = buffer[:size], buffer[size:]
                        requests.append(raw)
                        address, number = int.from_bytes(raw[8:10], "big"), int.from_bytes(raw[10:12], "big")
                        if raw[7] == 6:
                            registers[address] = number
                            response = raw
                        else:
                            payload = bytes([raw[6], 3, number * 2]) + b"".join(
                                value.to_bytes(2, "big") for value in registers[address:address + number])
                            response = raw[:4] + len(payload).to_bytes(2, "big") + payload
                        responses.append(response)
                        connection.sendall(response)
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield SimpleNamespace(port=listener.getsockname()[1], requests=requests, responses=responses,
                          registers=registers)
    stopped.set()
    thread.join(3)
    listener.close()
    assert not thread.is_alive()


def test_real_transport_trace_contains_the_exact_socket_request_and_response_bytes(wire_server):
    trace = ControlTrace("test-run")
    client = TracedModbusClient("127.0.0.1", port=wire_server.port, retries=0)
    client.control_trace = trace
    try:
        assert client.connect()
        assert not client.write_register(13, 100).isError()
        assert client.read_holding_registers(0, count=22).registers[13] == 100
    finally:
        client.close()
    tx = [bytes.fromhex(event["hex"]) for event in trace.events if event["direction"] == "TX"]
    rx = [bytes.fromhex(event["hex"]) for event in trace.events if event["direction"] == "RX"]
    assert tx == wire_server.requests
    assert b"".join(rx) == b"".join(wire_server.responses)
    assert trace.events[0]["frames"][0]["address"] == 13
    assert trace.events[0]["frames"][0]["rawValue"] == 100
    assert trace.events[0]["status"] == "socket-accepted"
    assert trace.events[1]["frames"][0]["functionCode"] == 6
    assert trace.events[-1]["frames"][0]["registers"][13] == 100


def test_partial_or_exception_frames_are_not_fabricated_and_capture_is_bounded():
    raw = bytes.fromhex("0001000000060106000d0064")
    assert decode_frames(raw[:5], True) == []
    assert decode_frames(raw[5:], True) == []
    exception = bytes.fromhex("000100000003018602")
    assert decode_frames(exception, False)[0]["exceptionCode"] == 2
    trace = ControlTrace()
    for _ in range(MAX_EVENTS + 2):
        trace.packet("worker-to-plc", False, raw)
    assert len(trace.events) == MAX_EVENTS and trace.document()["droppedEvents"] == 2


def test_no_response_is_invented_when_socket_send_fails(monkeypatch):
    from pymodbus.client import ModbusTcpClient
    def failed(*args):
        raise OSError("transport lost")
    monkeypatch.setattr(ModbusTcpClient, "send", failed)
    client = TracedModbusClient("127.0.0.1", retries=0)
    client.control_trace = ControlTrace()
    with pytest.raises(OSError):
        client.send(bytes.fromhex("0001000000060106000d0064"))
    assert client.control_trace.events[0]["status"] == "send-failed-or-unknown"
    assert not any(event["direction"] == "RX" for event in client.control_trace.events)


def test_route_b_captures_actual_hmi_plc_bytes_but_never_login_password_csrf_or_cookies(wire_server, monkeypatch):
    secret = "private-test-only-password"
    monkeypatch.setattr(hmi, "HOST", "127.0.0.1")
    monkeypatch.setattr(hmi, "PORT", wire_server.port)
    monkeypatch.setattr(hmi, "AUTH", "password")
    monkeypatch.setenv("SWU_HMI_USERNAME", "operator")
    monkeypatch.setenv("SWU_HMI_PASSWORD_HASH", generate_password_hash(secret))
    monkeypatch.setitem(hmi.app.config, "SECRET_KEY", "session-signing-test-only")
    flask_client = hmi.app.test_client()
    class Response(io.BytesIO):
        status = 200
    def open_request(request, timeout):
        response = flask_client.open(urlsplit(request.full_url).path,
                                     method="POST" if request.data is not None else "GET",
                                     data=request.data, headers=dict(request.header_items()),
                                     base_url="https://hmi.local")
        if response.status_code >= 400:
            raise HTTPError(request.full_url, response.status_code, "HMI error", {}, io.BytesIO(response.data))
        return Response(response.data)
    client = object.__new__(route_worker.PasswordHmi)
    client.url, client.csrf = "https://hmi.local", None
    client.credentials = {"username": "operator", "password": secret}
    client.opener = SimpleNamespace(open=open_request)
    client.trace = ControlTrace("test-route-b")
    client.trace.phase = "override-write"
    client.write("PumpOverride", 1)
    assert client.read()["PumpOverride"] == 1
    captured = client.trace.document()
    text = json.dumps(captured)
    assert secret not in text and client.csrf not in text
    assert "/api/login" not in text and "/api/session" not in text
    assert "Cookie" not in text and "X-SWU-CSRF" not in text
    writes = [event for event in captured["events"] if event["protocol"] == "Modbus TCP"
              and event["direction"] == "TX" and event["frames"][0]["functionCode"] == 6]
    assert len(writes) == 1 and writes[0]["channel"] == "hmi-to-plc"
    assert bytes.fromhex(writes[0]["hex"]) in wire_server.requests
    assert writes[0]["frames"][0]["rawValue"] == 100
    assert any(event.get("status") == 401 for event in captured["events"])
    assert any(event.get("body", {}).get("written") is True for event in captured["events"]
               if isinstance(event.get("body"), dict))


def test_runner_persists_override_readback_and_restore_bytes_for_the_same_run(wire_server, tmp_path, monkeypatch):
    import uuid
    from datetime import datetime, timezone
    client = route_worker.DirectPlc("127.0.0.1")
    client.client.close()
    client.client = TracedModbusClient("127.0.0.1", port=wire_server.port, retries=0)
    documents = {}
    blobs = SimpleNamespace(upload_blob=lambda name, content, **kwargs: documents.update({name: json.loads(content)}))
    audit = SimpleNamespace(send_message=lambda *args, **kwargs: None)
    runner = route_worker.Runner("routea", client, blobs, None, audit, tmp_path)
    job = {"runId": str(uuid.uuid4()), "route": "routea", "action": "pump-on",
           "actor": str(uuid.uuid4()), "issuedAt": datetime.now(timezone.utc).isoformat(), "confirmed": True}
    monkeypatch.setattr(route_worker, "DURATION", 0)
    monkeypatch.setattr(route_worker.time, "sleep", lambda _: None)
    try:
        runner.execute(job)
    finally:
        client.client.close()
    result = documents[f"jobs/{job['runId']}.json"]
    assert result["stage"] == "completed-restored"
    events = result["protocolTrace"]["events"]
    writes = [(event["phase"], event["frames"][0]["rawValue"]) for event in events
              if event["direction"] == "TX" and event["frames"][0]["functionCode"] == 6]
    assert writes == [("override-write", 100), ("restore-write", 0)]
    assert any(event["direction"] == "RX" and event["phase"] == "restore-readback" for event in events)
    assert json.loads(runner.db.execute("SELECT result FROM jobs").fetchone()[0])["protocolTrace"] == result["protocolTrace"]
    assert client.trace is None and runner.trace is None
