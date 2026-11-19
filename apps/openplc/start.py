"""Start the compiled OpenPLC core without exposing its administrative UI."""
import os
import signal
import socket
import subprocess
import time
import json
import struct


def main():
    if os.path.exists("/tmp/swu-ready"):
        os.unlink("/tmp/swu-ready")
    sensor_file = os.environ.get("SWU_SENSOR_FILE", "/sensors/inputs.bin")
    deadline = time.monotonic() + 60
    while not os.path.isfile(sensor_file):
        if time.monotonic() >= deadline:
            raise TimeoutError("Plant simulator did not initialize OpenPLC inputs")
        time.sleep(0.2)
    core = subprocess.Popen(["./core/openplc"])

    def stop(signum, frame):
        core.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while True:
            if core.poll() is not None:
                raise RuntimeError(f"OpenPLC exited during startup: {core.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", 43628), timeout=2) as control:
                    control.sendall(b"start_modbus(1502)\n")
                    response = control.recv(4096)
                    if not response or b"error" in response.lower():
                        raise RuntimeError(f"OpenPLC refused Modbus startup: {response!r}")
                break
            except (ConnectionRefusedError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("OpenPLC control socket did not become ready")
                time.sleep(0.2)
        with open("/config/profile.json", encoding="utf-8") as file:
            plant = json.load(file)["plant"]
        for address, value in ((10, plant["pumpLowSetpoint"]), (11, plant["pumpHighSetpoint"]),
                               (12, plant["clDoseSetpoint"]), (13, 0), (14, 0)):
            packet = struct.pack(">HHHBBHH", address, 0, 6, 1, 6, address, round(value * 100))
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", 1502), timeout=2) as client:
                        client.sendall(packet)
                        response = b""
                        while len(response) < len(packet):
                            chunk = client.recv(len(packet) - len(response))
                            if not chunk:
                                raise ConnectionError("OpenPLC initialization response ended early")
                            response += chunk
                        if response != packet:
                            raise RuntimeError("OpenPLC rejected initial setpoint")
                    break
                except ConnectionRefusedError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("OpenPLC Modbus server did not become ready")
                    time.sleep(0.2)
        with open("/tmp/swu-ready", "w", encoding="ascii") as file:
            file.write("ready")
        raise SystemExit(core.wait())
    finally:
        if os.path.exists("/tmp/swu-ready"):
            os.unlink("/tmp/swu-ready")
        if core.poll() is None:
            core.terminate()
            core.wait(timeout=10)


if __name__ == "__main__":
    main()
