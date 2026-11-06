"""Modbus holding-register map shared by the PLC, plant, gateway, HMI and attacker.
Values are scaled by 100 and stored as unsigned 16-bit integers."""
SCALE = 100
import math
MAP = {
    # written by the plant simulator (sensors, Purdue level 0)
    "TankLevelPct": 0, "ClResidual": 1, "FlowPctPerMin": 2,
    # setpoints held by the PLC, writable by the HMI (and by an attacker)
    "PumpLowSetpoint": 10, "PumpHighSetpoint": 11, "ClDoseSetpoint": 12, "PumpOverride": 13, "Mode": 14,
    # outputs written by the PLC logic (actuators)
    "PumpState": 20, "DoseRate": 21,
}
SENSORS = ("TankLevelPct", "ClResidual", "FlowPctPerMin")
SETPOINTS = ("PumpLowSetpoint", "PumpHighSetpoint", "ClDoseSetpoint", "PumpOverride", "Mode")
OUTPUTS = ("PumpState", "DoseRate")
READ_START, READ_COUNT = 0, 22


def encode(v: float) -> int:
    if not math.isfinite(v) or not 0 <= v <= 65535 / SCALE:
        raise ValueError("Register value must be finite and fit the unsigned scaled word")
    return int(round(v * SCALE))


def decode(raw: int) -> float:
    return raw / SCALE


def from_block(regs: list[int]) -> dict:
    """regs: READ_COUNT registers starting at READ_START."""
    if len(regs) != READ_COUNT:
        raise ValueError(f"Expected {READ_COUNT} registers, received {len(regs)}")
    return {tag: decode(regs[addr - READ_START]) for tag, addr in MAP.items()}
