"""Own tooling for the Scanmatik SM3 adapter over Wi-Fi or USB.

The SM3 speaks the same message protocol on every transport. On Wi-Fi it is a
plain TCP server the adapter runs on its own access point; on USB the very same
bytes travel inside ``DeviceIoControl`` on the manufacturer's NDIS driver. This
package implements the protocol from its written specification against captured
traffic; see ``THIRD_PARTY_LICENSES.txt`` for the provenance of the protocol
recovery.

Typical use::

    from m86tool.sm3 import Sm3Client

    with Sm3Client() as sm3:
        print(sm3.identity)
        sm3.send(0x7DF, bytes([0x01, 0x00]))
        sm3.poll()
        for f in sm3.drain():
            print(f)
"""

from __future__ import annotations

from .device import CanFrame, Identity, Sm3Client, Sm3Error, Voltages
from .frame import HEADER, Message, checksum, reframe, stride
from .h4 import COVERED, H4_BITS, POLY, derive, h8
from .transport import DEFAULT_HOST, DEFAULT_PORT

__all__ = [
    "Sm3Client",
    "Sm3Error",
    "Identity",
    "Voltages",
    "CanFrame",
    "Message",
    "HEADER",
    "stride",
    "checksum",
    "reframe",
    "derive",
    "h8",
    "COVERED",
    "POLY",
    "H4_BITS",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
]
