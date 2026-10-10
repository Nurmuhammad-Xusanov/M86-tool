"""Scanmatik SM3 message framing.

Every message is a 16-byte header followed by ``len`` bytes of data::

    0..1   seq    u16 LE   USB: a counter · TCP: always 0xffff
    2..3   token  u16 LE   USB: the driver's echo · TCP: always 0
    4..7   h4     u32 LE   fingerprint over the data (see :mod:`m86tool.sm3.h4`)
    8..11  h8     u32 LE   sum of the covered data words
    12     op
    13..14 len    u16 LE
    15     chk    (op + len_lo + len_hi + 0x55) & 0xff
    16..   data

Two details matter and are easy to get wrong:

* A message occupies ``align(16 + len, 4)`` bytes on the wire, **not**
  ``16 + len``. Sending the short form makes the device wait for bytes that
  never arrive and the connection desynchronises.
* Replies always carry ``op = 0x00`` regardless of what was asked, so the only
  thing that pairs a reply with its request is arrival order. Messages with
  ``op = 0xfe`` are pushed CAN-frame deliveries, not answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import h4 as _h4

HEADER = 16
CHECKSUM_SALT = 0x55
OP_REPLY = 0x00
OP_FRAMES = 0xFE


def checksum(op: int, length: int) -> int:
    """Header checksum: ``(op + len_lo + len_hi + 0x55) & 0xff``."""
    return (op + (length & 0xFF) + ((length >> 8) & 0xFF) + CHECKSUM_SALT) & 0xFF


def stride(length: int) -> int:
    """Bytes a message really occupies on the wire, four-byte aligned."""
    return (HEADER + length + 3) & ~3


@dataclass
class Message:
    seq: int
    token: int
    op: int
    data: bytes
    h4: int = 0
    h8: int = 0

    def encode(self) -> bytes:
        """Serialise, padded to the four-byte stride. Padding is zero here.

        The device's own padding is stale buffer content, but only that the
        bytes are present matters, not their value.
        """
        length = len(self.data)
        out = bytearray(stride(length))
        out[0:2] = self.seq.to_bytes(2, "little")
        out[2:4] = self.token.to_bytes(2, "little")
        out[4:8] = (self.h4 & 0xFFFFFFFF).to_bytes(4, "little")
        out[8:12] = (self.h8 & 0xFFFFFFFF).to_bytes(4, "little")
        out[12] = self.op & 0xFF
        out[13:15] = length.to_bytes(2, "little")
        out[15] = checksum(self.op, length)
        out[HEADER:HEADER + length] = self.data
        return bytes(out)


def size_at(buf: bytes, offset: int = 0) -> Optional[int]:
    """Bytes the message starting at ``offset`` occupies, or ``None``.

    ``None`` means "there is no header there"; it does not mean "not enough
    bytes yet", which the caller must handle by reading more.
    """
    if offset + HEADER > len(buf):
        return None
    op = buf[offset + 12]
    length = buf[offset + 13] | (buf[offset + 14] << 8)
    if buf[offset + 15] != checksum(op, length):
        return None
    return stride(length)


def decode(buf: bytes, offset: int = 0) -> Optional[Tuple[Message, int]]:
    """Parse one message at ``offset``. Returns it and the bytes consumed."""
    if offset + HEADER > len(buf):
        return None
    op = buf[offset + 12]
    length = buf[offset + 13] | (buf[offset + 14] << 8)
    if buf[offset + 15] != checksum(op, length):
        return None
    if offset + HEADER + length > len(buf):
        return None
    msg = Message(
        seq=int.from_bytes(buf[offset:offset + 2], "little"),
        token=int.from_bytes(buf[offset + 2:offset + 4], "little"),
        h4=int.from_bytes(buf[offset + 4:offset + 8], "little"),
        h8=int.from_bytes(buf[offset + 8:offset + 12], "little"),
        op=op,
        data=bytes(buf[offset + HEADER:offset + HEADER + length]),
    )
    return msg, min(stride(length), len(buf) - offset)


def split(buf: bytes) -> List[Message]:
    """Walk a buffer, skipping anything that does not parse."""
    out: List[Message] = []
    off = 0
    while off + HEADER <= len(buf):
        r = decode(buf, off)
        if r is None:
            off += 1
        else:
            out.append(r[0])
            off += r[1]
    return out


def reframe(reference: bytes, data: bytes) -> bytes:
    """Rebuild ``reference`` with a new data region, fixing both derived words.

    ``data`` is the replacement payload *including padding*: the same length
    the reference carries after its header. The reference's sequence number and
    the bytes past its declared length (its padding) are reused unchanged; the
    reference's ``h4`` and ``h8`` are recomputed from ``data``.

    Both words move with the payload, not just ``h4``. A frame that carries a
    recomputed ``h4`` next to the reference's stale ``h8`` is refused exactly
    like one carrying neither.
    """
    msg, _ = decode(reference)
    if msg is None:
        raise ValueError("reference does not decode")
    h4 = _h4.derive(reference, data)
    out = bytearray(reference)
    out[4:8] = h4.to_bytes(4, "little")
    out[8:12] = _h4.h8(data).to_bytes(4, "little")
    declared = len(msg.data)
    out[HEADER:HEADER + declared] = data[:declared]
    for i in range(declared, len(data)):
        if HEADER + i < len(out):
            out[HEADER + i] = data[i]
    return bytes(out)


def build(op: int, data: bytes = b"", seq: int = 0xFFFF, token: int = 0) -> bytes:
    """Build a signable message, deriving ``h4``/``h8`` from the reference.

    A zero-data message is the one case where ``h4`` needs no table: the sum of
    the (empty) data is zero and ``h4`` reduces to ``seq``. That is what the
    voltage request relies on. For anything longer, use :mod:`m86tool.sm3.recorded`
    plus :func:`m86tool.sm3.h4.derive` so the fingerprint matches a captured
    frame.
    """
    if data:
        raise ValueError("build() only supports empty-data messages; use Recorded")
    msg = Message(seq=seq, token=token, op=op, data=b"", h4=seq & 0xFFFFFFFF, h8=0)
    return msg.encode()
