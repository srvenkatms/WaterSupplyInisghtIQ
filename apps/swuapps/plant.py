"""Plant simulator (Purdue level 0): reads the PLC's outputs, applies tank and
chlorine physics, and writes sensor values back into the PLC (FR-26)."""
from __future__ import annotations

import os
import struct
import time
from pathlib import Path
from datetime import datetime, timezone

from pymodbus.client import ModbusTcpClient

from swugen import load_profile
from swugen.model import PlantModel
from .registers import MAP, READ_COUNT, READ_START, encode, from_block


def write_sensor_inputs(path: Path, values: tuple[float, float, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(struct.pack(">3H", *(encode(value) for value in values)))
    deadline = time.monotonic() + 2
    while True:
        try:
            temporary.replace(path)
            return
        except PermissionError:
            # Docker Desktop can briefly hold the Windows bind-mounted file open.
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


class Plant:
    def __init__(self, client: ModbusTcpClient, params: dict):
        self.client, self.model = client, PlantModel(params)

    def step(self, dt_min: float, minute_of_day: float) -> dict:
        response = self.client.read_holding_registers(READ_START, count=READ_COUNT)
        if response.isError():
            raise ConnectionError(f"Plant PLC snapshot read failed: {response}")
        v = from_block(response.registers)
        s = self.model.s
        s.PumpState, s.ClDoseSetpoint = int(v["PumpState"]), v["DoseRate"]
        self.model.physics(s, minute_of_day, dt_min)
        for tag in ("TankLevelPct", "ClResidual", "FlowPctPerMin"):
            if not os.environ.get("SWU_SENSOR_FILE"):
                response = self.client.write_register(MAP[tag], encode(getattr(s, tag)))
                if response.isError():
                    raise ConnectionError(f"Sensor register write failed: {response}")
        if os.environ.get("SWU_SENSOR_FILE"):
            self.write_inputs(Path(os.environ["SWU_SENSOR_FILE"]))
        return {"TankLevelPct": s.TankLevelPct, "ClResidual": s.ClResidual, "PumpState": s.PumpState}

    def write_inputs(self, path: Path) -> None:
        write_sensor_inputs(path, (self.model.s.TankLevelPct, self.model.s.ClResidual, self.model.s.FlowPctPerMin))


def main() -> None:
    p = load_profile(os.environ.get("SWU_UTILITY", "uta"))
    host, port = os.environ.get("SWU_PLC_HOST", "127.0.0.1"), int(os.environ.get("SWU_MODBUS_PORT", "502"))
    accel = float(os.environ.get("SWU_TIME_ACCELERATION", "1"))
    client = ModbusTcpClient(host, port=port)
    plant = Plant(client, p["plant"])
    if os.environ.get("SWU_SENSOR_FILE"):
        plant.write_inputs(Path(os.environ["SWU_SENSOR_FILE"]))
    while True:
        if not client.connected:
            client.connect()
        try:
            now = datetime.now(timezone.utc)
            plant.step(dt_min=accel / 60, minute_of_day=now.hour * 60 + now.minute + now.second / 60)
        except Exception as exc:   # PLC restarting: keep trying
            print(f"plant: {exc}", flush=True)
            client.close()
        time.sleep(1)


if __name__ == "__main__":
    main()
