"""Soft PLC (Purdue level 1): Modbus TCP server running the tank and dosing
control logic every 0.5 s. Uses the same control() as the generator's model.
Replaceable by OpenPLC with openplc/tank_control.st (see README)."""
from __future__ import annotations

import asyncio
import json
import os

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext, ModbusSlaveContext
from pymodbus.server import ModbusTcpServer

from swugen import load_profile
from swugen.model import PlantModel, PlantState
from .registers import MAP, decode, encode

FC_READ_HOLDING = 3


class SoftPlc:
    def __init__(self, plant_params: dict, port: int = 502, host: str = "0.0.0.0"):
        self.slave = ModbusSlaveContext(hr=ModbusSequentialDataBlock(0, [0] * 101))
        self.context = ModbusServerContext(slaves=self.slave, single=True)
        for tag in ("PumpLowSetpoint", "PumpHighSetpoint", "ClDoseSetpoint"):
            self.write(tag, plant_params[tag[0].lower() + tag[1:]])
        self.write("TankLevelPct", plant_params["initialLevelPct"])
        self.function_codes: list[int] = []
        self.address = (host, port)
        self.server = None        # created inside the running event loop

    def make_server(self) -> ModbusTcpServer:
        self.server = ModbusTcpServer(self.context, address=self.address, trace_pdu=self._trace)
        return self.server

    def _trace(self, sending: bool, pdu):
        if not sending:
            self.function_codes.append(pdu.function_code)
        return pdu

    def read(self, tag: str) -> float:
        return decode(self.slave.getValues(FC_READ_HOLDING, MAP[tag], 1)[0])

    def write(self, tag: str, value: float) -> None:
        self.slave.setValues(FC_READ_HOLDING, MAP[tag], [encode(value)])

    def scan(self) -> None:
        """One PLC scan: inputs -> control logic -> outputs."""
        s = PlantState(TankLevelPct=self.read("TankLevelPct"), ClResidual=self.read("ClResidual"),
                       PumpState=int(self.read("PumpState")), PumpLowSetpoint=self.read("PumpLowSetpoint"),
                       PumpHighSetpoint=self.read("PumpHighSetpoint"), ClDoseSetpoint=self.read("ClDoseSetpoint"),
                       PumpOverride=int(self.read("PumpOverride")), Mode=int(self.read("Mode")))
        if s.Mode == 0:                       # program/maintenance mode holds outputs
            PlantModel.control(s)
            self.write("PumpState", s.PumpState)
            self.write("DoseRate", s.ClDoseSetpoint)

    async def run(self) -> None:
        async def logic():
            while True:
                self.scan()
                await asyncio.sleep(0.5)
        await asyncio.gather(self.make_server().serve_forever(), logic())


def main() -> None:
    p = load_profile(os.environ.get("SWU_UTILITY", "uta"))
    asyncio.run(SoftPlc(p["plant"], port=int(os.environ.get("SWU_MODBUS_PORT", "502"))).run())


if __name__ == "__main__":
    main()
