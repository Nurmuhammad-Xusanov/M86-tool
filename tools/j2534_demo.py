"""Demo: use m86tool J2534 pass-thru to read VIN from M86."""

from __future__ import annotations

import ctypes as c
import sys
import time

from m86tool.j2534 import wrap
from m86tool.j2534.defs import (
    CAN_29BIT_ID,
    FLOW_CONTROL_FILTER,
    ISO15765_FRAME_PAD,
    PASS_FILTER,
    PROTOCOL_ISO15765,
    PASSTHRU_MSG,
    STATUS_NOERROR,
)

TX = 0x7E0
RX = 0x7E8


def run():
    did = c.c_ulong()
    ch = c.c_ulong()
    ret = wrap.PassThruOpen(None, c.byref(did))
    assert ret == STATUS_NOERROR, f"open {ret}"
    print("open OK devid=", did.value)
    ret = wrap.PassThruConnect(did, PROTOCOL_ISO15765, 0, 500000,
                               c.byref(ch))
    assert ret == STATUS_NOERROR, f"connect {ret}"
    print("connect OK chid=", ch.value)

    # minimal: just dump versions
    fv = c.create_string_buffer(80)
    dv = c.create_string_buffer(80)
    av = c.create_string_buffer(80)
    wrap.PassThruReadVersion(did, c.byref(fv), c.byref(dv), c.byref(av))
    print(fv.value, dv.value, av.value)


if __name__ == "__main__":
    run()
