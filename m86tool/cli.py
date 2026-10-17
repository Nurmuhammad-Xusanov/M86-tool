"""Command line interface: m86tool."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from .m86 import M86, FLASH_SIZE
from .sm2.device import SM2
from .sm2.transport import SM2_VID, USBTransport
from .sm3.device import Sm3Client
from .sm3.transport import DEFAULT_HOST, DEFAULT_PORT


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


def _adapter_choice(args) -> str:
    if getattr(args, "sm3", False):
        return "sm3"
    return getattr(args, "adapter", "auto")


def cmd_probe(args) -> int:
    adapter = _adapter_choice(args)
    if adapter == "sm3" or (adapter == "auto" and not USBTransport.list_devices(SM2_VID)):
        try:
            sm3 = Sm3Client(host=getattr(args, "host", DEFAULT_HOST),
                            port=getattr(args, "port", DEFAULT_PORT))
            ident = sm3.connect()
            print("Scanmatik SM3 responded to GREETING (Wi-Fi)")
            print(f"  serial={ident.serial}  fw={ident.firmware}")
            v = sm3.voltages()
            if v:
                print(f"  battery: {v.battery_millivolts / 1000:.1f} V (flags=0x{v.flags:04X})")
            sm3.close()
            return 0
        except Exception as e:
            if adapter == "sm3":
                raise
            # In auto mode, fall through to try USB if reachable check failed
    sm2 = SM2(pid=args.pid)
    info = sm2.bring_up()
    print("adapter responded to INIT (USB)")
    if info:
        print(f"  hw_id={info.hardware_id_hex}  fw={info.firmware_str}  "
              f"hw={info.hardware_str}")
    print(f"  echo: {sm2.echo(b'M86')!r}")
    sm2.close()
    return 0


def cmd_identify(args) -> int:
    m = M86()
    m.connect(adapter=_adapter_choice(args), bitrate=args.bitrate, pid=args.pid,
              host=getattr(args, "host", DEFAULT_HOST),
              port=getattr(args, "port", DEFAULT_PORT))
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
    m.connect(adapter=_adapter_choice(args), bitrate=args.bitrate, pid=args.pid,
              host=getattr(args, "host", DEFAULT_HOST),
              port=getattr(args, "port", DEFAULT_PORT))
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
    m.connect(adapter=_adapter_choice(args), bitrate=args.bitrate, pid=args.pid,
              host=getattr(args, "host", DEFAULT_HOST),
              port=getattr(args, "port", DEFAULT_PORT))
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
    m.connect(adapter=_adapter_choice(args), bitrate=args.bitrate, pid=args.pid,
              host=getattr(args, "host", DEFAULT_HOST),
              port=getattr(args, "port", DEFAULT_PORT))
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


def _open_sm3(args) -> Sm3Client:
    """Join the adapter's Wi-Fi, greet it and open a bus channel."""
    sm3 = Sm3Client(host=args.host, port=args.port)
    ident = sm3.connect(timeout=args.timeout)
    print(f"SM3 serial={ident.serial} firmware={ident.firmware}")
    sm3.open_channel(narrow=not args.wide)
    mode = "narrowed" if sm3.narrowed else "full bus"
    print(f"channel open ({mode})")
    return sm3


def cmd_sm3_info(args) -> int:
    sm3 = _open_sm3(args)
    sm3.close()
    print("session closed cleanly")
    return 0


def cmd_sm3_voltage(args) -> int:
    sm3 = _open_sm3(args)
    try:
        v = sm3.voltages()
        if v is None:
            print("no voltage answer")
            return 1
        print(f"{v.battery_millivolts / 1000:.1f} V "
              f"(raw={v.battery_millivolts} mV, second={v.second}, "
              f"flags=0x{v.flags:04X})")
    finally:
        sm3.close()
    return 0


def _print_frames(frames) -> None:
    for f in frames:
        print(f"0x{f.arbitration_id:03X}  {f.data.hex(' ')}")


def cmd_sm3_poll(args) -> int:
    sm3 = _open_sm3(args)
    try:
        i = 0
        while args.count == 0 or i < args.count:
            sm3.poll()
            _print_frames(sm3.drain())
            i += 1
            if args.count == 0 or i < args.count:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        sm3.close()
    return 0


def cmd_sm3_monitor(args) -> int:
    sm3 = _open_sm3(args)
    try:
        end = time.monotonic() + args.seconds
        while time.monotonic() < end:
            sm3.receive(0.5)
            _print_frames(sm3.drain())
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        sm3.close()
    return 0


def cmd_sm3_send(args) -> int:
    can_id = args.can_id
    payload = bytes.fromhex("".join(args.payload))
    if len(payload) > 7:
        print("error: a single frame carries at most seven payload bytes",
              file=sys.stderr)
        return 1
    sm3 = _open_sm3(args)
    try:
        sm3.send(can_id, payload)
        deadline = time.monotonic() + args.reply
        while time.monotonic() < deadline:
            sm3.receive(0.2)
        _print_frames(sm3.drain())
    finally:
        sm3.close()
    return 0


def cmd_sm3_request(args) -> int:
    payload = bytes.fromhex("".join(args.payload))
    if len(payload) > 7:
        print("error: a single-frame request carries at most seven bytes",
              file=sys.stderr)
        return 1
    sm3 = _open_sm3(args)
    try:
        print(f"request  0x{args.tx_id:03X}  {payload.hex(' ')}")
        reply = sm3.uds_request(payload, req_id=args.tx_id,
                                resp_id=args.rx_id, timeout=args.reply)
        if reply is None:
            print("no response")
            return 1
        print(f"response 0x{args.rx_id:03X}  {reply.hex(' ')}")
    finally:
        sm3.close()
    return 0


def cmd_sm3_selftest(_args) -> int:
    from .sm3 import selftest
    return selftest.main()


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true")
    common.add_argument("--pid", type=lambda x: int(x, 0), default=None,
                        help="force USB PID (default: auto 0x0001/0x0002)")
    common.add_argument("--bitrate", type=int, default=500000)
    common.add_argument("--adapter", choices=["auto", "sm2", "sm3"], default="auto",
                        help="adapter to use: sm2 (USB), sm3 (Wi-Fi), or auto (default: auto)")
    common.add_argument("--sm3", action="store_true",
                        help="use Scanmatik SM3 over Wi-Fi (shortcut for --adapter sm3)")
    common.add_argument("--host", default=DEFAULT_HOST,
                        help=f"SM3 Wi-Fi host (default: {DEFAULT_HOST})")
    common.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"SM3 Wi-Fi port (default: {DEFAULT_PORT})")

    p = argparse.ArgumentParser(prog="m86tool",
                                description="Own M86 tooling over Scanmatik SM2/SM3.",
                                parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list Scanmatik adapters").set_defaults(func=cmd_list)
    sub.add_parser("probe", parents=[common], help="open adapter and run init/echo").set_defaults(func=cmd_probe)
    sub.add_parser("identify", parents=[common], help="read M86 identification").set_defaults(func=cmd_identify)

    rd = sub.add_parser("read", parents=[common], help="read ECU memory to a file")
    rd.add_argument("output")
    rd.add_argument("--address", type=lambda x: int(x, 0), default=0)
    rd.add_argument("--size", type=lambda x: int(x, 0), default=FLASH_SIZE)
    rd.add_argument("--eeprom", action="store_true", help="read EEPROM region")
    rd.set_defaults(func=cmd_read)

    dtc = sub.add_parser("dtc", parents=[common], help="read or clear M86 DTCs")
    dtc.add_argument("--clear", action="store_true", help="clear all DTCs")
    dtc.set_defaults(func=cmd_dtc)

    lv = sub.add_parser("live", parents=[common], help="poll live data DIDs via UDS 0x22")
    lv.add_argument("dids", nargs="+", help="DIDs as hex, e.g. F190 F187")
    lv.add_argument("--interval", type=float, default=0.5)
    lv.add_argument("--count", type=int, default=0,
                    help="number of samples (0 = forever)")
    lv.set_defaults(func=cmd_live)

    sm3_common = argparse.ArgumentParser(add_help=False)
    sm3_common.add_argument("--host", default=DEFAULT_HOST,
                            help=f"adapter address (default {DEFAULT_HOST})")
    sm3_common.add_argument("--port", type=int, default=DEFAULT_PORT,
                            help=f"adapter TCP port (default {DEFAULT_PORT})")
    sm3_common.add_argument("--timeout", type=float, default=4.0,
                            help="connect/greeting timeout in seconds")
    sm3_common.add_argument("--wide", action="store_true",
                            help="keep the full bus instead of narrowing")

    sm3 = sub.add_parser("sm3", help="Scanmatik SM3 over its own Wi-Fi")
    sm3sub = sm3.add_subparsers(dest="sm3cmd", required=True)
    sm3info = sm3sub.add_parser("info", parents=[sm3_common],
                                help="connect, greet and open a channel")
    sm3info.set_defaults(func=cmd_sm3_info)
    sm3volt = sm3sub.add_parser("voltage", parents=[sm3_common],
                                help="read the adapter's own battery voltage")
    sm3volt.set_defaults(func=cmd_sm3_voltage)
    sp = sm3sub.add_parser("poll", parents=[sm3_common],
                           help="poll the bus and print arriving frames")
    sp.add_argument("--interval", type=float, default=0.5)
    sp.add_argument("--count", type=int, default=0,
                    help="number of polls (0 = forever)")
    sp.set_defaults(func=cmd_sm3_poll)
    sm = sm3sub.add_parser("monitor", parents=[sm3_common],
                           help="read frames the adapter pushes, without polling")
    sm.add_argument("--seconds", type=float, default=10.0)
    sm.set_defaults(func=cmd_sm3_monitor)
    ss = sm3sub.add_parser("send", parents=[sm3_common],
                           help="send one CAN request and print the replies")
    ss.add_argument("can_id", type=lambda x: int(x, 0), help="e.g. 0x7DF")
    ss.add_argument("payload", nargs="+", help="payload bytes as hex, e.g. 01 00")
    ss.add_argument("--reply", type=float, default=1.0,
                    help="seconds to listen for replies")
    ss.set_defaults(func=cmd_sm3_send)
    sq = sm3sub.add_parser("request", parents=[sm3_common],
                           help="send a UDS request and print the reassembled reply")
    sq.add_argument("payload", nargs="+", help="UDS bytes as hex, e.g. 22 F1 90")
    sq.add_argument("--tx-id", type=lambda x: int(x, 0), default=0x7E0, dest="tx_id")
    sq.add_argument("--rx-id", type=lambda x: int(x, 0), default=0x7E8, dest="rx_id")
    sq.add_argument("--reply", type=float, default=2.0,
                    help="seconds to wait for the response")
    sq.set_defaults(func=cmd_sm3_request)
    sm3st = sm3sub.add_parser("selftest",
                              help="offline self-check of the protocol layer (no hardware)")
    sm3st.set_defaults(func=cmd_sm3_selftest)
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
