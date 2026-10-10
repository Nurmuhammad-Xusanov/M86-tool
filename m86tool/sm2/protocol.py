"""
Scanmatik SM2 wire protocol codec.

Frame format (confirmed via APK reverse engineering + hardware probing):

    offset  size  field
    0       1     command / response byte
    1       1     payload length low  (LE16)
    2       1     payload length high (LE16)
    3       1     checksum
    4       N     payload

Checksum = (cmd + 0x55 + len_hi + len_lo) & 0xFF

Protocol facts reused from the MIT-licensed `sm2can` project
(https://github.com/aldoguzman97/sm2can, (c) 2026 Aldo Guzman).

NOTE: the command-byte values below are confirmed for SM2 Pro (PID 0x0001)
for the *system* commands. The CAN channel commands (0x8A+) are best-guess
and must be re-verified against real hardware, in particular for the
original SM2 (PID 0x0002).
"""

from __future__ import annotations

import enum
import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

CHECKSUM_SALT = 0x55
HEADER_SIZE = 4


class Cmd(enum.IntEnum):
    # system (confirmed on SM2 Pro)
    STATUS = 0x80
    ECHO = 0x82
    DEVICE_INFO = 0x83
    CLEAR_FIFO = 0x86
    POWER_DOWN = 0x87
    INIT = 0x88

    # CAN channel (best-guess, verify on hardware)
    CAN_OPEN = 0x8A
    CAN_SEND = 0x8B
    CAN_RECV = 0x8C
    CAN_CLOSE = 0x8D
    CAN_SET_FILTER = 0x8E
    CAN_CLEAR_FILTER = 0x8F
    CAN_STATUS = 0x90

    # responses (carried in the response command byte)
    SUCCESS = 0x00
    NACK = 0x01
    STATUS_IDLE = 0x5E


class CANMode(enum.IntEnum):
    NORMAL = 0
    LISTEN_ONLY = 1
    LOOPBACK = 2


@dataclass
class CANFrame:
    arbitration_id: int
    data: bytes
    is_extended_id: bool = False
    is_remote_frame: bool = False
    timestamp: float = 0.0

    def __repr__(self) -> str:
        return (f"CANFrame(id=0x{self.arbitration_id:03X}, "
                f"data={self.data.hex(' ')}, ext={self.is_extended_id})")


@dataclass
class DeviceInfo:
    raw: bytes = field(default_factory=bytes)
    hardware_id: int = 0
    firmware_version: int = 0
    hardware_rev: int = 0

    @property
    def firmware_str(self) -> str:
        if not self.firmware_version:
            return "unknown"
        return f"{(self.firmware_version >> 8) & 0xFF}.{self.firmware_version & 0xFF}"

    @property
    def hardware_str(self) -> str:
        if not self.hardware_rev:
            return "unknown"
        return f"{(self.hardware_rev >> 8) & 0xFF}.{self.hardware_rev & 0xFF}"

    @property
    def hardware_id_hex(self) -> str:
        if len(self.raw) >= 3:
            return self.raw[:3].hex(" ")
        return "unknown"


def sm2_checksum(cmd: int, length: int) -> int:
    return (cmd + CHECKSUM_SALT + ((length >> 8) & 0xFF) + (length & 0xFF)) & 0xFF


def build_frame(cmd: int, payload: bytes = b"") -> bytes:
    length = len(payload)
    ck = sm2_checksum(cmd, length)
    return bytes([cmd, length & 0xFF, (length >> 8) & 0xFF, ck]) + payload


def verify_checksum(data: bytes) -> bool:
    if len(data) < HEADER_SIZE:
        return False
    length = data[1] | (data[2] << 8)
    return sm2_checksum(data[0], length) == data[3]


def parse_frame(data: bytes) -> Optional[Tuple[int, int, bool, bytes]]:
    if len(data) < HEADER_SIZE:
        return None
    length = data[1] | (data[2] << 8)
    valid = verify_checksum(data)
    payload = data[HEADER_SIZE:HEADER_SIZE + length] if len(data) >= HEADER_SIZE + length else b""
    return (data[0], length, valid, payload)


class Codec:
    """Stateful streaming encoder/decoder."""

    def __init__(self) -> None:
        self._rx = bytearray()

    # encoders
    def echo(self, payload: bytes = b"M86") -> bytes:
        return build_frame(Cmd.ECHO, payload)

    def device_info(self) -> bytes:
        return build_frame(Cmd.DEVICE_INFO)

    def init(self) -> bytes:
        return build_frame(Cmd.INIT)

    def clear_fifo(self) -> bytes:
        return build_frame(Cmd.CLEAR_FIFO)

    def status(self) -> bytes:
        return build_frame(Cmd.STATUS)

    def can_open(self, bitrate: int = 500000, mode: int = CANMode.NORMAL) -> bytes:
        payload = struct.pack("<BBI", 0x00, int(mode), bitrate)
        return build_frame(Cmd.CAN_OPEN, payload)

    def can_close(self) -> bytes:
        return build_frame(Cmd.CAN_CLOSE)

    def can_send(self, frame: CANFrame) -> bytes:
        flags = 0
        if frame.is_extended_id:
            flags |= 0x01
        if frame.is_remote_frame:
            flags |= 0x02
        data = frame.data[:8]
        payload = struct.pack("<IBB", frame.arbitration_id, flags, len(data)) + data
        return build_frame(Cmd.CAN_SEND, payload)

    def can_filter(self, arb_id: int, mask: int = 0x7FF) -> bytes:
        return build_frame(Cmd.CAN_SET_FILTER, struct.pack("<II", arb_id, mask))

    def can_clear_filter(self) -> bytes:
        return build_frame(Cmd.CAN_CLEAR_FILTER)

    # decoders
    def decode_device_info(self, payload: bytes) -> DeviceInfo:
        info = DeviceInfo(raw=payload)
        if len(payload) >= 3:
            info.hardware_id = payload[0] | (payload[1] << 8) | (payload[2] << 16)
        if len(payload) >= 14:
            info.firmware_version = payload[12] | (payload[13] << 8)
        if len(payload) >= 18:
            info.hardware_rev = payload[16] | (payload[17] << 8)
        return info

    def decode_can_frame(self, payload: bytes) -> Optional[CANFrame]:
        if len(payload) < 6:
            return None
        arb_id, flags, dlc = struct.unpack("<IBB", payload[:6])
        dlc = min(dlc, 8)
        return CANFrame(
            arbitration_id=arb_id,
            data=payload[6:6 + dlc],
            is_extended_id=bool(flags & 0x01),
            is_remote_frame=bool(flags & 0x02),
        )

    def feed(self, data: bytes) -> List[Tuple[int, bytes]]:
        """Streaming reassembly. Returns list of (cmd, payload)."""
        self._rx.extend(data)
        out: List[Tuple[int, bytes]] = []
        while len(self._rx) >= HEADER_SIZE:
            length = self._rx[1] | (self._rx[2] << 8)
            total = HEADER_SIZE + length
            if len(self._rx) < total:
                break
            frame = bytes(self._rx[:total])
            del self._rx[:total]
            if verify_checksum(frame):
                out.append((frame[0], frame[HEADER_SIZE:]))
        return out

    def reset(self) -> None:
        self._rx.clear()
