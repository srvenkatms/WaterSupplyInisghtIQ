"""Water treatment process model (FR-12 to FR-18).

One storage tank, one supply pump with level setpoints, and chlorine dosing
whose residual follows the dose with a first-order lag. Values are in the
same units the PLC and the edge gateway use."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass


def demand_pct_per_min(minute_of_day: float, base: float, peak: float) -> float:
    """Daily demand curve with morning (07:00) and evening (19:00) peaks."""
    morning = math.exp(-((minute_of_day - 420) / 90) ** 2)
    evening = math.exp(-((minute_of_day - 1140) / 120) ** 2)
    return base + (peak - base) * max(morning, evening)


@dataclass
class PlantState:
    TankLevelPct: float
    ClResidual: float
    PumpState: int
    PumpLowSetpoint: float
    PumpHighSetpoint: float
    ClDoseSetpoint: float
    PumpOverride: int = 0   # 0 = automatic, 1 = forced on
    Mode: int = 0           # 0 = run, 1 = program or maintenance
    FlowPctPerMin: float = 0.0


class PlantModel:
    """Advances the plant one step at a time. control() is the PLC logic;
    physics() is the plant. The soft PLC and plant simulator containers use
    the same two halves separately."""

    def __init__(self, params: dict, state: PlantState | None = None):
        self.p = params
        self.s = state or PlantState(
            TankLevelPct=params["initialLevelPct"],
            ClResidual=params["clDoseSetpoint"] * params["residualGain"],
            PumpState=0,
            PumpLowSetpoint=params["pumpLowSetpoint"],
            PumpHighSetpoint=params["pumpHighSetpoint"],
            ClDoseSetpoint=params["clDoseSetpoint"],
        )

    # PLC logic: hysteresis on tank level, or forced on by override.
    @staticmethod
    def control(s: PlantState) -> None:
        if s.PumpOverride:
            s.PumpState = 1
        elif s.TankLevelPct <= s.PumpLowSetpoint:
            s.PumpState = 1
        elif s.TankLevelPct >= s.PumpHighSetpoint:
            s.PumpState = 0

    # Plant physics over dt_min simulated minutes.
    def physics(self, s: PlantState, minute_of_day: float, dt_min: float) -> None:
        demand = demand_pct_per_min(minute_of_day, self.p["baseDemandPctPerMin"], self.p["peakDemandPctPerMin"])
        fill = self.p["fillRatePctPerMin"] if s.PumpState else 0.0
        s.TankLevelPct = min(100.0, max(0.0, s.TankLevelPct + (fill - demand) * dt_min))
        s.FlowPctPerMin = demand
        target = s.ClDoseSetpoint * self.p["residualGain"]
        alpha = 1 - math.exp(-dt_min / self.p["residualTauMin"])
        s.ClResidual += (target - s.ClResidual) * alpha

    def step(self, minute_of_day: float, dt_min: float = 1.0) -> PlantState:
        self.control(self.s)
        self.physics(self.s, minute_of_day, dt_min)
        return self.s

    def to_dict(self) -> dict:
        return asdict(self.s)

    @classmethod
    def from_dict(cls, params: dict, d: dict) -> "PlantModel":
        return cls(params, PlantState(**d))
