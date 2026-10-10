"""A session with the Scanmatik SM3 over its own Wi-Fi.

Opening a channel means replaying, byte for byte, the sequence the
manufacturer's driver sends: the firmware checks the ``h4`` fingerprint on every
message, so anything hand-rolled is refused. Two rules govern everything here,
and both matter:

* **TCP is a stream.** Messages are taken out of the buffer by length, never by
  read boundary (see :mod:`m86tool.sm3.transport`).
* **Every reply carries op 0x00, whatever was asked.** The only thing that pairs
  a reply with its request is arrival order, so requests go one at a time and
  the reader keeps reading until an ``op 0x00`` arrives, stashing any frame
  deliveries (``op 0xfe``) met on the way.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional

from . import frame, isotp, write
from .recorded import CLOSING, FILTERED_CHANNEL, OPENING, READ
from .transport import DEFAULT_HOST, DEFAULT_PORT, TcpTransport

OP_FRAMES = 0xFE
OP_VOLTAGES = 0x20
VOLTAGES_LENGTH = 6

STEP_TIMEOUT = 2.0
CLOSE_TIMEOUT = 0.4
RECONNECT_TO_RELEASE = 1.5


class Sm3Error(IOError):
    """Raised when the adapter stops answering."""


@dataclass
class Identity:
    serial: str
    firmware: str


@dataclass
class Voltages:
    battery_millivolts: int
    second: int
    flags: int


@dataclass
class CanFrame:
    arbitration_id: int
    data: bytes

    def __repr__(self) -> str:
        return f"CanFrame(id=0x{self.arbitration_id:03X}, data={self.data.hex(' ')})"


class Sm3Client:
    """Own tooling for the SM3 adapter: open a channel, poll, read, write."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        self.host = host
        self.port = port
        self.link = TcpTransport(host, port)
        self._frames: List[CanFrame] = []
        self.narrowed = False
        self.keep_all = False
        self.unpaired = 0
        self.identity: Optional[Identity] = None

    def __enter__(self) -> "Sm3Client":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self.link.connected

    # ── session ───────────────────────────────────────────────────────────

    def connect(self, timeout: float = 4.0) -> Identity:
        """Open the socket and greet. Returns what the device says it is."""
        self.link.open(timeout)
        reply = self._exchange(OPENING[0], timeout)
        if reply is None:
            self.link.close()
            raise Sm3Error("the adapter did not answer the greeting")
        self.identity = self._identity(reply)
        return self.identity

    def open_channel(self, narrow: bool = True) -> None:
        """Replay the recorded opening: session, config, channel, filters.

        Every step is checked. Replaying the rest after one goes unanswered is
        what leaves the device refusing everything, its own application
        included, until it is unplugged.
        """
        for i in range(1, len(OPENING)):
            if self._exchange(OPENING[i], STEP_TIMEOUT) is None:
                self.link.close()
                raise Sm3Error(
                    f"the adapter stopped answering at step {i} of the opening"
                )
        if narrow:
            self.narrowed = (
                self._exchange(FILTERED_CHANNEL, STEP_TIMEOUT) is not None
                and self._exchange(OPENING[5], STEP_TIMEOUT) is not None
                and self._exchange(OPENING[6], STEP_TIMEOUT) is not None
            )

    def close(self) -> None:
        """Hand the channel back before dropping the socket.

        The device takes one client and does not notice a socket that simply
        goes away, so a session left open keeps the next one out — the
        manufacturer's own software included — until the adapter is unplugged.
        """
        said_goodbye = self._release()
        self.link.close()
        if not said_goodbye:
            try:
                self.link.open(RECONNECT_TO_RELEASE)
                self._exchange(OPENING[0], RECONNECT_TO_RELEASE)
                self.open_channel()
                self._release()
            except Exception:
                pass
            finally:
                self.link.close()

    def _release(self) -> bool:
        if not self.link.connected:
            return False
        try:
            for message in CLOSING:
                self._exchange(message, CLOSE_TIMEOUT)
            return True
        except Exception:
            return False

    # ── bus ───────────────────────────────────────────────────────────────

    def poll(self) -> int:
        """Poll once; returns how many frames arrived."""
        before = len(self._frames)
        self._exchange(READ, STEP_TIMEOUT)
        return len(self._frames) - before

    def receive(self, timeout: float) -> int:
        """Read whatever the device is pushing, without asking it for anything."""
        if not self.link.connected:
            return 0
        before = len(self._frames)
        deadline = time.monotonic() + timeout
        while True:
            msg = self.link.next_message(deadline)
            if msg is None:
                break
            if msg.op == OP_FRAMES:
                self._frames.extend(self._frames_in(msg.data))
            else:
                self.unpaired += 1
        return len(self._frames) - before

    def voltages(self) -> Optional[Voltages]:
        """The device's own voltages, from opcode 0x20.

        A message with no data is the one case where ``h4`` needs no table: it
        is simply the sequence number, so this frame is built rather than
        replayed. The answer is exactly six bytes; accepting any other length is
        how a battery reads 0.640 V (the answer to a previous bus poll).
        """
        request = frame.build(OP_VOLTAGES)
        reply = self._exchange(request, STEP_TIMEOUT)
        if reply is None or len(reply.data) != VOLTAGES_LENGTH:
            return None
        d = reply.data
        return Voltages(
            battery_millivolts=int.from_bytes(d[0:2], "little"),
            second=int.from_bytes(d[2:4], "little"),
            flags=int.from_bytes(d[4:6], "little"),
        )

    def send(self, can_id: int, payload: bytes) -> None:
        """Put a request on the bus as an ISO-TP single frame."""
        self._exchange(write.write(can_id, payload), STEP_TIMEOUT)

    def send_frame(self, can_id: int, frame_bytes: bytes) -> None:
        """Put a whole CAN frame on the bus, PCI byte included."""
        self._exchange(write.write_frame(can_id, frame_bytes), STEP_TIMEOUT)

    def uds_request(self, payload: bytes, req_id: int = 0x7E0,
                    resp_id: int = 0x7E8, timeout: float = 2.0) -> Optional[bytes]:
        """Send a single-frame request and return the reassembled response.

        Only single-frame *requests* are supported (``payload`` up to seven
        bytes), which covers identification, DTC reads and memory-read
        commands. A multi-frame *response* is handled: the tester answers the
        module's first frame with a flow-control frame, which this channel
        passes raw.

        Returns ``None`` if no complete response arrives before ``timeout``.
        """
        if len(payload) > 7:
            raise ValueError("a single-frame request carries at most seven bytes")
        reasm = isotp.Reassembler()
        self.send(req_id, payload)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.receive(0.1)
            for f in self.drain():
                if f.arbitration_id != resp_id or not f.data:
                    continue
                kind = (f.data[0] & 0xFF) >> 4
                packet = reasm.push(resp_id, f.data)
                if packet is not None:
                    return packet
                if kind == 1:
                    # First frame: ask the module for the rest.
                    self.send_frame(req_id, isotp.FLOW_CONTROL)
        return None

    def drain(self) -> List[CanFrame]:
        out = self._frames
        self._frames = []
        return out

    # ── the wire ──────────────────────────────────────────────────────────

    def _exchange(self, message: bytes, timeout: float) -> Optional[frame.Message]:
        """Write one message and wait for its answer (the next ``op 0x00``)."""
        self.link.send(message)
        deadline = time.monotonic() + timeout
        while True:
            msg = self.link.next_message(deadline)
            if msg is None:
                return None
            if msg.op == OP_FRAMES:
                self._frames.extend(self._frames_in(msg.data))
                continue
            return msg

    def _frames_in(self, data: bytes) -> List[CanFrame]:
        """A 0xfe block: 32-byte block header, count in the high nibble of
        byte 34, then 16-byte records of ``len u32 | id u32 | 8 data bytes``."""
        if len(data) < 36:
            return []
        count = (data[34] & 0xFF) >> 4
        out: List[CanFrame] = []
        for i in range(count):
            o = 36 + i * 16
            if o + 16 > len(data):
                break
            length = int.from_bytes(data[o:o + 4], "little")
            if not 0 <= length <= 8:
                continue
            can_id = int.from_bytes(data[o + 4:o + 8], "little")
            if not self.keep_all and not self._worth_keeping(can_id):
                continue
            out.append(CanFrame(can_id, data[o + 8:o + 8 + length]))
        return out

    @staticmethod
    def _worth_keeping(can_id: int) -> bool:
        """The four ranges anything can answer or emit on."""
        return (
            can_id in range(0x7E8, 0x7F0)
            or can_id in range(0x640, 0x660)
            or can_id == 0x5E8
            or can_id in range(0x540, 0x560)
        )

    @staticmethod
    def _identity(reply: frame.Message) -> Identity:
        """Greeting: serial as a little-endian word at 0, firmware at 12.

        The serial is a number whose **hexadecimal is the string**: read the
        four bytes, print them in hex, and that is the serial. The adapter's own
        access point calls itself ``DIRECT-SCANMATIK-#<serial>``.
        """
        d = reply.data
        if len(d) < 20:
            raise Sm3Error(f"the greeting answer was {len(d)} bytes, expected 66")
        def word(at: int) -> str:
            return f"{int.from_bytes(d[at:at + 4], 'little'):X}"
        return Identity(word(0), word(12))
