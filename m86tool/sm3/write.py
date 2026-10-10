"""Build signed CAN writes the SM3 will accept.

The fingerprint on a hand-built write cannot be guessed: the firmware checks
both derived words and drops anything that does not add up, closing the link
without a word. Every write is therefore built by taking the recorded reference
write and moving only the CAN id and payload, then recomputing ``h4`` and
``h8`` (see :mod:`m86tool.sm3.h4`).
"""

from __future__ import annotations

from . import frame
from .recorded import WRITE_TEMPLATE


def single_frame(payload: bytes) -> bytes:
    """Wrap ``payload`` as an ISO-TP single frame: the PCI byte is the length."""
    if len(payload) > 7:
        raise ValueError("a single frame carries at most seven payload bytes")
    return bytes([len(payload)]) + payload + bytes(7 - len(payload))


def write_frame(can_id: int, frame_bytes: bytes) -> bytes:
    """Build a write message carrying ``frame_bytes`` as the CAN frame.

    The eight record bytes are the CAN frame itself, PCI byte included. That
    matters for anything that is not a single frame: flow control has to reach
    the module as ``30 00 00``, and building it as a length plus seven payload
    bytes puts ``08 30 00`` on the wire, which reads as a single frame invoking
    service 0x30.
    """
    if len(frame_bytes) > 8:
        raise ValueError("a CAN frame carries at most eight bytes")
    msg, _ = frame.decode(WRITE_TEMPLATE)
    assert msg is not None
    data = bytearray(WRITE_TEMPLATE[frame.HEADER:])
    data[7:11] = (can_id & 0xFFFFFFFF).to_bytes(4, "little")
    for i in range(8):
        data[11 + i] = frame_bytes[i] if i < len(frame_bytes) else 0
    return frame.reframe(WRITE_TEMPLATE, bytes(data))


def write(can_id: int, payload: bytes) -> bytes:
    """Build a write message for ``can_id`` carrying ``payload`` as a single frame."""
    return write_frame(can_id, single_frame(payload))
