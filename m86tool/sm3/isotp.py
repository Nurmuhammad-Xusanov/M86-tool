"""ISO-TP reassembly and segmentation for SM3 CAN frames.

An ELM327 hands a response back already joined, so a bridge has to do the
joining itself. The first byte says what a frame is::

    0x0X  single frame, X data bytes
    0x1X  first of several, 12-bit length in X and the next byte
    0x2X  consecutive
    0x3X  flow control, no payload
"""

from __future__ import annotations

from typing import Dict, List, Optional

#: Clear to send, no block limit, no separation time.
FLOW_CONTROL = bytes([0x30, 0x00, 0x00, 0, 0, 0, 0, 0])


class Reassembler:
    """Join ISO-TP frames into packets, one partial packet per CAN id."""

    def __init__(self) -> None:
        self._open: Dict[int, List[bytes]] = {}
        self._expected: Dict[int, int] = {}

    def reset(self) -> None:
        self._open.clear()
        self._expected.clear()

    def push(self, can_id: int, frame: bytes) -> Optional[bytes]:
        """Feed one CAN frame; return a packet when one completes."""
        if not frame:
            return None
        kind = (frame[0] & 0xFF) >> 4
        if kind == 0:
            n = frame[0] & 0x0F
            if n == 0 or len(frame) < 1 + n:
                return None
            return frame[1:1 + n]
        if kind == 1:
            if len(frame) < 2:
                return None
            expected = ((frame[0] & 0x0F) << 8) | (frame[1] & 0xFF)
            self._expected[can_id] = expected
            self._open[can_id] = [frame[2:]]
            return None
        if kind == 2:
            parts = self._open.get(can_id)
            if parts is None:
                return None
            parts.append(frame[1:])
            joined = b"".join(parts)
            expected = self._expected.get(can_id, 0)
            if len(joined) >= expected:
                self._open.pop(can_id, None)
                self._expected.pop(can_id, None)
                return joined[:expected]
            return None
        return None


def segment(payload: bytes) -> List[bytes]:
    """Wrap ``payload`` as one or more ISO-TP frames (8 bytes each)."""
    if len(payload) <= 7:
        return [bytes([len(payload)]) + payload + bytes(7 - len(payload))]
    frames = [bytes([0x10 | ((len(payload) >> 8) & 0x0F), len(payload) & 0xFF])
              + payload[:6]]
    rest = payload[6:]
    seq = 1
    while rest:
        chunk = rest[:7]
        frames.append(bytes([0x20 | (seq & 0x0F)]) + chunk + bytes(7 - len(chunk)))
        rest = rest[7:]
        seq = (seq + 1) & 0x0F
    return frames
