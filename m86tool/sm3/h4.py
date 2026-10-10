"""The two derived words every Scanmatik SM3 message carries: ``h4`` and ``h8``.

This is an independent implementation of the SM3 wire protocol. The protocol
was recovered from the adapter by capture and measurement (see
``THIRD_PARTY_LICENSES.txt``); the facts below are functional and this module
was written from the written specification rather than copied from any source.

``h8`` is a plain sum
---------------------

Read the twenty covered data bytes as five little-endian 32-bit words and add
them, discarding the carry out of the top::

    h8 = data[0:4] + data[4:8] + data[8:12] + data[12:16] + data[16:20]

``h4`` is a CRC
---------------

``h4`` is affine over GF(2), so a new frame's fingerprint is a reference
frame's fingerprint XOR the contribution of every data bit that differs::

    h4(new) = h4(reference) XOR contributions(bits that changed)

The contribution of each of the 160 covered bits is the contribution of the
next one shifted right by one, with the reflected polynomial ``0x9960034C``
folded in whenever a one falls off the bottom. That is the recurrence of a
reflected CRC, so the whole table follows from a single anchor and contains no
fitted or guessed entries.
"""

from __future__ import annotations

#: Reflected CRC polynomial used by the ``h4`` fingerprint.
POLY = 0x9960034C

#: Number of data bytes that take part in both derived words.
COVERED = 20

BITS = 160


def _build_table() -> list[int]:
    """Generate the 160 32-bit bit contributions from the polynomial.

    ``BITS[k]`` is the contribution of data bit ``k`` (``byte = k // 8``,
    ``bit = k % 8``) to ``h4``. Entry 159 is the anchor; each earlier entry is
    the next one shifted right with the polynomial folded in when a one drops
    out, which is the defining recurrence of a reflected CRC.
    """
    table = [0] * BITS
    table[BITS - 1] = POLY
    for i in range(BITS - 2, -1, -1):
        v = table[i + 1]
        table[i] = (v >> 1) ^ (POLY if (v & 1) else 0)
    return table


#: Contribution of each covered data bit to ``h4``, index ``byte * 8 + bit``.
H4_BITS: list[int] = _build_table()


def h8(data: bytes) -> int:
    """Sum of the covered data as five little-endian 32-bit words."""
    total = 0
    for word in range(COVERED // 4):
        v = 0
        for byte in range(4):
            i = word * 4 + byte
            v |= (data[i] if i < len(data) else 0) << (byte * 8)
        total = (total + v) & 0xFFFFFFFF
    return total


def derive(reference: bytes, data: bytes) -> int:
    """``h4`` for ``data`` given a ``reference`` frame whose ``h4`` is known.

    ``reference`` is a whole wire message (16-byte header first); only its
    ``h4`` field and its data region are read. ``data`` is the replacement
    payload plus padding, the same length the reference carries after its
    header.
    """
    h4 = int.from_bytes(reference[4:8], "little")
    tail = reference[16:]
    for byte in range(COVERED):
        a = tail[byte] if byte < len(tail) else 0
        b = data[byte] if byte < len(data) else 0
        diff = a ^ b
        if not diff:
            continue
        for bit in range(8):
            if diff & (1 << bit):
                h4 ^= H4_BITS[byte * 8 + bit]
    return h4 & 0xFFFFFFFF
