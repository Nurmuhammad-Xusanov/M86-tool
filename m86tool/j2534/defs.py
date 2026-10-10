"""SAE J2534-1 v04.04 constants and data structures (minimal, Linux-first).

Reference: J2534 v04.04 (PassThru API). Supports ISO15765 (CAN) and basic IOCTL.
"""

from __future__ import annotations

import ctypes as c

STATUS_NOERROR = 0x00
ERR_DEVICE_NOT_CONNECTED = 0x01
ERR_DEVICE_NOT_OPEN = 0x02
ERR_CHANNEL_NOT_OPEN = 0x03
ERR_MSG_PROTOCOL_ID = 0x07
ERR_INVALID_MSG = 0x09
ERR_MSG_NOT_ALLOWED = 0x0C
ERR_INVALID_FILTER_ID = 0x0D
ERR_NO_FLOW_CONTROL = 0x0E
ERR_TIMEOUT = 0x09  # sometimes reused in text; use spec mapping
ERR_BUFFER_EMPTY = 0x0A
ERR_BUFFER_FULL = 0x0B

# Protocols
PROTOCOL_ISO15765 = 0x06  # CAN (ISO-TP)
PROTOCOL_CAN = 0x05

# Connect/Tx/Rx flags
ISO15765_FRAME_PAD = 0x00000040
CAN_29BIT_ID = 0x00000100
TX_MSG_TYPE = 0x00000001

# Filter types
PASS_FILTER = 0x01
BLOCK_FILTER = 0x02
FLOW_CONTROL_FILTER = 0x03

# PASSTHRU_MSG (v04.04)
PASSTHRU_MSG_SIZE = 4 + 4 + 4 + 4 + 4 + 4 + 4128


class PASSTHRU_MSG(c.Structure):
    _fields_ = [
        ("ProtocolID", c.c_ulong),
        ("RxStatus", c.c_ulong),
        ("TxFlags", c.c_ulong),
        ("Timestamp", c.c_ulong),
        ("DataSize", c.c_ulong),
        ("ExtraDataIndex", c.c_ulong),
        ("Data", c.c_ubyte * 4128),
    ]

    def to_dict(self):
        return {
            "ProtocolID": self.ProtocolID,
            "RxStatus": self.RxStatus,
            "TxFlags": self.TxFlags,
            "Timestamp": self.Timestamp,
            "DataSize": self.DataSize,
            "ExtraDataIndex": self.ExtraDataIndex,
            "Data": bytes(self.Data)[: self.DataSize],
        }


# Ioctl GET_CONFIG/SET_CONFIG
GET_CONFIG = 0x01
SET_CONFIG = 0x02
READ_PIN_VOLTAGE = 0x03
READ_PROG_VOLTAGE = 0x04
FIVE_BAUD_INIT = 0x05
FAST_INIT = 0x06
CLEAR_TX_QUEUE = 0x07
CLEAR_RX_QUEUE = 0x08
CLEAR_PERIODIC_MSGS = 0x09
CLEAR_MSG_FILTERS = 0x0A

CONFIG_LIST = 0x14
CONFIG_DATA_RATE = 0x01
CONFIG_LOOPBACK = 0x03


class SCONFIG(c.Structure):
    _fields_ = [("Parameter", c.c_ulong), ("Value", c.c_ulong)]


class SCONFIG_LIST(c.Structure):
    _fields_ = [("NumOfParams", c.c_ulong), ("ConfigPtr", c.POINTER(SCONFIG))]


# Helper
def mk_msg(protocol: int, txflags: int, data: bytes) -> PASSTHRU_MSG:
    m = PASSTHRU_MSG()
    m.ProtocolID = protocol
    m.TxFlags = txflags
    d = bytes(data)[:4128]
    m.DataSize = len(d)
    m.ExtraDataIndex = m.DataSize
    c.memmove(m.Data, d, len(d))
    return m