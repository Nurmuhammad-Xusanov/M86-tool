"""Offline self-check for the SM3 protocol layer.

Runs the framing, derived-word and reassembly code against frames the device
actually sent or accepted, with no adapter attached. ``m86tool sm3 selftest``
uses this so a user can confirm the build is sound before blaming the car.

Nothing here talks to hardware; a green run means "the encoding is right", not
"your adapter answers".
"""

from __future__ import annotations

from typing import List, Tuple

from . import frame, h4, isotp, write
from .recorded import CLOSING, OPENING, READ, WRITE_TEMPLATE


def _hx(s: str) -> bytes:
    return bytes.fromhex(s)


Check = Tuple[str, bool, str]


def _check(name: str, ok: bool, detail: str = "") -> Check:
    return (name, ok, detail)


def run() -> List[Check]:
    """Return one ``(name, ok, detail)`` per check. Nothing raises for a failure."""
    out: List[Check] = []

    def eq(name: str, got: object, want: object) -> None:
        out.append(_check(name, got == want, "" if got == want else f"{got!r} != {want!r}"))

    # ── framing ──────────────────────────────────────────────────────────
    for n, w in [(0, 16), (172, 188), (175, 192), (2, 20), (261, 280), (305, 324)]:
        eq(f"stride({n}) == {w}", frame.stride(n), w)
    for op, ln, w in [(0x83, 0, 0xD8), (0x88, 0, 0xDD), (0x30, 261, 0x8B),
                      (0x1C, 19, 0x84), (0x1C, 3, 0x74)]:
        eq(f"checksum({op:#x}, {ln}) == {w:#x}", frame.checksum(op, ln), w)

    greeting = _hx("ffff0000ffff000000000000830000d8")
    result = frame.decode(greeting)
    ok = result is not None and result[0].encode() == greeting and result[1] == 16
    out.append(_check("greeting round-trips", ok))

    # ── derived words ────────────────────────────────────────────────────
    poly = h4.POLY
    chain = all(
        h4.H4_BITS[n] == ((h4.H4_BITS[n + 1] >> 1) ^ (poly if h4.H4_BITS[n + 1] & 1 else 0))
        for n in range(h4.BITS - 1)
    )
    out.append(_check("h4 table is a CRC chain from the polynomial", chain))

    recorded_vin = _hx("ffff0000d2f1e4e2811003ea1c130084"
                       "60800208000000e0070000021a90000000000000")
    data = bytearray(WRITE_TEMPLATE[frame.HEADER:])
    data[7], data[8], data[11], data[12], data[13] = 0xE0, 0x07, 2, 0x1A, 0x90
    eq("reframe reproduces the recorded VIN request",
       frame.reframe(WRITE_TEMPLATE, bytes(data)), recorded_vin)
    eq("write() reproduces the recorded VIN request",
       write.write(0x7E0, bytes([0x1A, 0x90])), recorded_vin)

    # ── recorded data ────────────────────────────────────────────────────
    good = all(
        frame.decode(m) is not None and frame.decode(m)[1] == len(m)
        for m in OPENING + CLOSING + [READ, WRITE_TEMPLATE]
    )
    out.append(_check("every recorded message decodes at its full length", good))

    # ── reassembly ───────────────────────────────────────────────────────
    r = isotp.Reassembler()
    eq("single frame reassembles",
       r.push(0x7E8, _hx("064100983b201300")), _hx("4100983b2013"))
    r = isotp.Reassembler()
    r.push(0x7E8, _hx("1011010203040506"))
    r.push(0x7E8, _hx("2107080910111213"))
    eq("first + consecutive frames join",
       r.push(0x7E8, _hx("2214151617000000")), _hx("0102030405060708091011121314151617"))

    return out


def main() -> int:
    checks = run()
    for name, ok, detail in checks:
        mark = "ok  " if ok else "FAIL"
        print(f"  [{mark}] {name}{'  -- ' + detail if detail else ''}")
    failed = sum(1 for _, ok, _ in checks if not ok)
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
