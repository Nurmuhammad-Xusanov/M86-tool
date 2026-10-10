"""Command line interface: m86tool."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .m86 import M86, FLASH_SIZE
from .sm2.device import SM2
from .sm2.transport import SM2_VID, USBTransport


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


def cmd_list(_args) -> int:
    devs = USBTransport.list_devices(SM2_VID)
    if not devs:
        print("no Scanmatik found (VID 0x20A2)")
        return 1
    for d in devs:
        print(f"bus {d['bus']} addr {d['address']}  "
              f"PID=0x{d['pid']:04X} ({d['model']})  serial={d['serial']}")
    return 0


def cmd_probe(args) -> int:
    sm2 = SM2(pid=args.pid)
    info = sm2.bring_up()
    print("adapter responded to INIT")
    if info:
        print(f"  hw_id={info.hardware_id_hex}  fw={info.firmware_str}  "
              f"hw={info.hardware_str}")
    print(f"  echo: {sm2.echo(b'M86')!r}")
    sm2.close()
    return 0


def cmd_identify(args) -> int:
    m = M86()
    m.connect(bitrate=args.bitrate, pid=args.pid)
    try:
        idn = m.identify()
        print("M86 identification:")
        print(f"  VIN         : {idn.vin}")
        print(f"  part number : {idn.part_number}")
        print(f"  SW version  : {idn.sw_version}")
        print(f"  HW number   : {idn.hw_number}")
        print(f"  serial      : {idn.serial}")
        if idn.raw:
            print("  raw DIDs:")
            for did, data in sorted(idn.raw.items()):
                print(f"    0x{did:04X}: {data.hex(' ')}")
    finally:
        m.close()
    return 0


def cmd_read(args) -> int:
    out = Path(args.output)
    m = M86()
    m.connect(bitrate=args.bitrate, pid=args.pid)
    try:
        def progress(done: int, total: int) -> None:
            pct = 100.0 * done / total
            sys.stderr.write(f"\rreading {done}/{total} bytes ({pct:5.1f}%)")
            sys.stderr.flush()

        if args.eeprom:
            data = m.read_eeprom(progress=progress)
        else:
            data = m.read_memory(args.address, args.size, progress=progress)
        sys.stderr.write("\n")
        out.write_bytes(data)
        print(f"wrote {len(data)} bytes to {out}")
    finally:
        m.close()
    return 0


def _fmt_dtc(row: dict) -> str:
    return (f"{row['code']:<8} status=0x{int(row['status'], 16):02X} "
            f"({row['status_names']})  raw={row['raw']}")


def cmd_dtc(args) -> int:
    m = M86()
    m.connect(bitrate=args.bitrate, pid=args.pid)
    try:
        if args.clear:
            m.clear_dtcs()
            print("DTCs cleared")
            return 0
        result = m.read_dtcs()
        if result["number_reported"] is not None:
            print(f"DTC count reported: {result['number_reported']}")
        print(f"DTC records parsed: {result['count']}")
        for row in result["dtcs"]:
            print("  " + _fmt_dtc(row))
    finally:
        m.close()
    return 0


def cmd_live(args) -> int:
    dids = [int(x, 0) for x in args.dids]
    m = M86()
    m.connect(bitrate=args.bitrate, pid=args.pid)
    last_sample = {}
    try:
        def cb(ts: float, did: int, payload: bytes) -> None:
            if not payload:
                print(f"t={ts:8.3f}  0x{did:04X}: (no response)")
                return
            sig = (did, payload)
            changed = last_sample.get(did) != payload
            last_sample[did] = payload
            mark = "*" if changed else " "
            print(f"t={ts:8.3f}  0x{did:04X}: {payload.hex(' ')}{mark}")

        mode = f"interval={args.interval}s"
        mode += ", forever" if args.count == 0 else f", {args.count} samples"
        print(f"polling DIDs: {[hex(d) for d in dids]} ({mode}, Ctrl+C to stop)")

        try:
            m.live_data(dids, interval=args.interval, count=args.count,
                        callback=cb)
        except KeyboardInterrupt:
            print("\nstopped")
    finally:
        m.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="m86tool",
                                description="Own M86 tooling over Scanmatik SM2.")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--pid", type=lambda x: int(x, 0), default=None,
                   help="force USB PID (default: auto 0x0001/0x0002)")
    p.add_argument("--bitrate", type=int, default=500000)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list Scanmatik adapters").set_defaults(func=cmd_list)
    sub.add_parser("probe", help="open adapter and run init/echo").set_defaults(func=cmd_probe)
    sub.add_parser("identify", help="read M86 identification").set_defaults(func=cmd_identify)

    rd = sub.add_parser("read", help="read ECU memory to a file")
    rd.add_argument("output")
    rd.add_argument("--address", type=lambda x: int(x, 0), default=0)
    rd.add_argument("--size", type=lambda x: int(x, 0), default=FLASH_SIZE)
    rd.add_argument("--eeprom", action="store_true", help="read EEPROM region")
    rd.set_defaults(func=cmd_read)

    dtc = sub.add_parser("dtc", help="read or clear M86 DTCs")
    dtc.add_argument("--clear", action="store_true", help="clear all DTCs")
    dtc.set_defaults(func=cmd_dtc)

    lv = sub.add_parser("live", help="poll live data DIDs via UDS 0x22")
    lv.add_argument("dids", nargs="+", help="DIDs as hex, e.g. F190 F187")
    lv.add_argument("--interval", type=float, default=0.5)
    lv.add_argument("--count", type=int, default=0,
                    help="number of samples (0 = forever)")
    lv.set_defaults(func=cmd_live)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except Exception as e:
        import traceback
        if args.verbose:
            traceback.print_exc()
        print(f"error: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
