#!/usr/bin/env python3
"""Characterize a Scanmatik SM2 adapter: enumerate, init, echo, brute commands.

Use this first on any new adapter (especially the original SM2, PID 0x0002)
to see which command bytes it answers. Needs 12V on the OBD connector for
full operation.

Run:  .venv/bin/python tools/probe_device.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from m86tool.sm2.protocol import Cmd, build_frame, parse_frame
from m86tool.sm2.transport import SM2_VID, USBTransport


def enumerate_devices() -> None:
    print("== Scanmatik devices ==")
    devs = USBTransport.list_devices(SM2_VID)
    if not devs:
        print("  none found")
    for d in devs:
        print(f"  bus {d['bus']} addr {d['address']} PID=0x{d['pid']:04X} "
              f"({d['model']}) serial={d['serial']}")


def try_cmd(t: USBTransport, cmd: int, payload: bytes = b"") -> None:
    resp = t.write_read(build_frame(cmd, payload), timeout_ms=500)
    name = Cmd(cmd).name if cmd in Cmd._value2member_map_ else "?"
    if not resp:
        print(f"  0x{cmd:02X} {name:12s} -> (no response)")
        return
    parsed = parse_frame(resp)
    if parsed:
        rx, length, valid, pl = parsed
        print(f"  0x{cmd:02X} {name:12s} -> rx=0x{rx:02X} len={length} "
              f"ck={'ok' if valid else 'BAD'} payload={pl.hex(' ')}")
    else:
        print(f"  0x{cmd:02X} {name:12s} -> raw {resp.hex(' ')}")


def main() -> int:
    enumerate_devices()

    t = USBTransport()
    try:
        t.open()
    except Exception as e:  # noqa: BLE001
        print(f"cannot open adapter: {e}")
        return 1

    print("\n== known system commands ==")
    for cmd in (Cmd.INIT, Cmd.ECHO, Cmd.DEVICE_INFO, Cmd.CLEAR_FIFO, Cmd.STATUS):
        payload = b"M86" if cmd == Cmd.ECHO else b""
        try_cmd(t, int(cmd), payload)
        time.sleep(0.05)

    print("\n== brute-force command space (0x00-0xFF, empty payload) ==")
    for cmd in range(0x100):
        if cmd in {int(Cmd.POWER_DOWN)}:
            continue
        resp = t.write_read(build_frame(cmd), timeout_ms=150)
        if resp:
            parsed = parse_frame(resp)
            if parsed:
                rx, length, valid, pl = parsed
                print(f"  0x{cmd:02X} -> rx=0x{rx:02X} len={length} "
                      f"ck={'ok' if valid else 'BAD'} payload={pl.hex(' ')}")
            else:
                print(f"  0x{cmd:02X} -> raw {resp.hex(' ')}")

    t.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
