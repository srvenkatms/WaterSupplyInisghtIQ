(* All public registers use SCALE=100, including state values.
   The hardware adapter supplies actual input words; output mirrors let the
   gateway obtain an atomic read-only snapshot using function code 3. *)
PROGRAM tank_control
  VAR
    TankInput    AT %IW0  : INT;
    ResidualInput AT %IW1 : INT;
    FlowInput    AT %IW2  : INT;
    TankLevel    AT %QW0  : INT;
    ClResidual   AT %QW1  : INT;
    Flow         AT %QW2  : INT;
    PumpLowSp    AT %QW10 : INT := 4000;
    PumpHighSp   AT %QW11 : INT := 9000;
    ClDoseSp     AT %QW12 : INT := 120;
    PumpOverride AT %QW13 : INT := 0;
    Mode         AT %QW14 : INT := 0;
    PumpState    AT %QW20 : INT;   (* outputs, written by this program *)
    DoseRate     AT %QW21 : INT;
  END_VAR

  TankLevel := TankInput;
  ClResidual := ResidualInput;
  Flow := FlowInput;
  IF Mode = 0 THEN
    IF PumpOverride <> 0 THEN
      PumpState := 100;
    ELSIF TankLevel <= PumpLowSp THEN
      PumpState := 100;
    ELSIF TankLevel >= PumpHighSp THEN
      PumpState := 0;
    END_IF;
    DoseRate := ClDoseSp;
  END_IF;
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK MainTask(INTERVAL := T#500ms, PRIORITY := 0);
    PROGRAM Inst0 WITH MainTask : tank_control;
  END_RESOURCE
END_CONFIGURATION
