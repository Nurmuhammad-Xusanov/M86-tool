"""ISO-TP (ISO 15765-2) transport over classic CAN for the SM2 adapter."""

from __future__ import annotations

import logging
import time
from typing import Optional

from .sm2.protocol import CANFrame
from .sm2.device import SM2, SM2Error

log = logging.getLogger(__name__)

# PCI types
_PCI_SF = 0x00
_PCI_FF = 0x10
_PCI_CF = 0x20
_PCI_FC = 0x30

_TX_DL = 8
_STMIN_UNIT_MS = 0.001


class IsoTpError(Exception):
    pass


def _pad(data: bytes, n: int = 8, fill: int = 0x00) -> bytes:
    return data + bytes([fill]) * (n - len(data))


def _stmin_seconds(stmin: int) -> float:
    if stmin <= 0x7F:
        return stmin * _STMIN_UNIT_MS
    if 0xF1 <= stmin <= 0xF9:
        return (stmin - 0xF0) * 0.0001
    return 0.0


class IsoTp:
    """Synchronous ISO-TP link on top of an SM2 adapter (blocking calls)."""

    def __init__(self, sm2: SM2, tx_id: int, rx_id: int,
                 tx_pad: int = 0x00, rx_pad: int = 0x00):
        self.sm2 = sm2
        self.tx_id = tx_id
        self.rx_id = rx_id
        self.tx_pad = tx_pad
        self.rx_pad = rx_pad

    # ---- transmit ----
    def send(self, data: bytes) -> None:
        n = len(data)
        if n == 0:
            raise IsoTpError("empty payload")
        if n <= _TX_DL - 1:
            self.sm2.send(self.tx_id, _pad(bytes([_PCI_SF | n]) + data, fill=self.tx_pad))
            return
        if n > 0xFFF:
            raise IsoTpError("payload too large for ISO-TP single transfer")
        first = bytes([_PCI_FF | ((n >> 8) & 0x0F), n & 0xFF]) + data[:6]
        self.sm2.send(self.tx_id, _pad(first, fill=self.tx_pad))
        self._wait_flow_control()
        self._send_consecutive(data[6:])

    def _wait_flow_control(self, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            f = self.sm2.recv(timeout=deadline - time.monotonic())
            if f is None or f.arbitration_id != self.rx_id or not f.data:
                continue
            pci = f.data[0] & 0xF0
            if pci != _PCI_FC:
                continue
            fs = f.data[0] & 0x0F
            if fs == 0x0:  # CTS
                return
            if fs == 0x2:  # Overflow — abort
                raise IsoTpError("flow control: overflow")
            # fs == 1 (WAIT): keep waiting
        raise IsoTpError("timeout waiting for flow control")

    def _send_consecutive(self, rest: bytes) -> None:
        sn = 1
        for i in range(0, len(rest), _TX_DL - 1):
            chunk = rest[i:i + _TX_DL - 1]
            self.sm2.send(self.tx_id, _pad(bytes([_PCI_CF | (sn & 0x0F)]) + chunk,
                                           fill=self.tx_pad))
            sn = (sn + 1) & 0x0F
            time.sleep(_STMIN_UNIT_MS)  # conservative inter-frame gap

    # ---- receive ----
    def recv(self, timeout: float = 1.0) -> Optional[bytes]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            f = self.sm2.recv(timeout=deadline - time.monotonic())
            if f is None or f.arbitration_id != self.rx_id or not f.data:
                continue
            pci = f.data[0] & 0xF0
            if pci == _PCI_SF:
                n = f.data[0] & 0x0F
                return f.data[1:1 + n]
            if pci == _PCI_FF:
                total = ((f.data[0] & 0x0F) << 8) | f.data[1]
                buf = bytearray(f.data[2:8])
                # flow control is sent on the tester transmit ID (tx_id)
                self.sm2.send(self.tx_id,
                              _pad(bytes([_PCI_FC, 0x00, 0x00]), fill=self.tx_pad))
                return self._recv_consecutive(buf, total, deadline)
            # ignore FC / stray frames
        return None

    def _recv_consecutive(self, buf: bytearray, total: int,
                          deadline: float) -> Optional[bytes]:
        sn = 1
        while len(buf) < total and time.monotonic() < deadline:
            f = self.sm2.recv(timeout=deadline - time.monotonic())
            if f is None or f.arbitration_id != self.rx_id or not f.data:
                continue
            if (f.data[0] & 0xF0) != _PCI_CF:
                continue
            if (f.data[0] & 0x0F) != sn:
                raise IsoTpError(f"bad sequence number {f.data[0] & 0x0F} != {sn}")
            buf.extend(f.data[1:8])
            sn = (sn + 1) & 0x0F
        if len(buf) < total:
            raise IsoTpError("timeout reassembling message")
        return bytes(buf[:total])
