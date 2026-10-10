"""High-level Scanmatik SM2 adapter: USB + protocol codec + CAN channel."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional

from .protocol import CANFrame, CANMode, Codec, Cmd, DeviceInfo, parse_frame
from .transport import SM2_VID, USBTransport, TransportError

log = logging.getLogger(__name__)


class SM2Error(Exception):
    pass


class SM2:
    def __init__(self, vid: int = SM2_VID, pid: Optional[int] = None,
                 bus: Optional[int] = None, address: Optional[int] = None):
        self._t = USBTransport(vid=vid, pid=pid, bus=bus, address=address)
        self._c = Codec()
        self._open = False
        self._info: Optional[DeviceInfo] = None
        self._rx: deque = deque(maxlen=8192)
        self._rx_thread = None
        self._rx_run = False

    @property
    def device_info(self) -> Optional[DeviceInfo]:
        return self._info

    @property
    def is_open(self) -> bool:
        return self._open

    # ---- low level ----
    def cmd(self, cmd: int, payload: bytes = b"",
            timeout_ms: int = 1000) -> Optional[tuple]:
        """Send a raw command. Returns (rx_cmd, payload) or None on timeout."""
        from .protocol import build_frame
        resp = self._t.write_read(build_frame(cmd, payload), timeout_ms=timeout_ms)
        if not resp:
            return None
        parsed = parse_frame(resp)
        if parsed is None:
            return None
        rx_cmd, _length, valid, pl = parsed
        if not valid:
            log.warning("checksum mismatch in response")
        return (rx_cmd, pl)

    def echo(self, payload: bytes = b"M86") -> Optional[bytes]:
        r = self.cmd(Cmd.ECHO, payload, timeout_ms=1000)
        return r[1] if r else None

    def bring_up(self) -> DeviceInfo:
        """Open USB and run the standard init sequence. Returns device info."""
        try:
            self._t.open()
        except TransportError as e:
            raise SM2Error(str(e)) from e

        try:
            r = self.cmd(Cmd.INIT)
            if r is None:
                raise SM2Error(
                    "no response to INIT — adapter has no power. "
                    "Connect it to the car and turn the ignition ON (do not "
                    "start the engine), or use a 12V supply."
                )
            if r[0] == Cmd.NACK:
                raise SM2Error("device rejected INIT")

            if self.echo(b"M86") is None:
                log.warning("ECHO gave no response")

            r = self.cmd(Cmd.DEVICE_INFO)
            if r and r[0] == Cmd.SUCCESS and r[1]:
                self._info = self._c.decode_device_info(r[1])

            self.cmd(Cmd.CLEAR_FIFO)
        except TransportError as e:
            raise SM2Error(
                f"adapter did not respond to commands ({e}). "
                "It likely lacks 12V power — connect it to the car with the "
                "ignition ON (do not start the engine)."
            ) from e
        self._open = True
        return self._info

    def open_can(self, bitrate: int = 500000, mode: int = CANMode.NORMAL) -> Optional[int]:
        r = self.cmd(Cmd.CAN_OPEN,
                     __import__("struct").pack("<BBI", 0, int(mode), bitrate))
        self._start_rx()
        return r[0] if r else None

    def close(self) -> None:
        self._stop_rx()
        if self._t.is_open:
            try:
                from .protocol import build_frame
                self._t.write(build_frame(Cmd.CAN_CLOSE))
                time.sleep(0.05)
            except Exception:
                pass
            self._t.close()
        self._open = False

    # ---- CAN ----
    def send(self, arb_id: int, data: bytes,
             extended: bool = False) -> None:
        if not self._open:
            raise SM2Error("not open")
        frame = CANFrame(arb_id, bytes(data[:8]), is_extended_id=extended)
        self._t.write(self._c.can_send(frame))

    def recv(self, timeout: float = 1.0) -> Optional[CANFrame]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._rx:
                return self._rx.popleft()
            time.sleep(0.001)
        return None

    def _start_rx(self) -> None:
        self._rx_run = True
        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True,
                                           name="sm2-rx")
        self._rx_thread.start()

    def _stop_rx(self) -> None:
        self._rx_run = False
        if self._rx_thread:
            self._rx_thread.join(timeout=2.0)
            self._rx_thread = None

    def _rx_loop(self) -> None:
        while self._rx_run and self._t.is_open:
            try:
                data = self._t.read(timeout_ms=50)
                if not data:
                    continue
                for cmd, payload in self._c.feed(data):
                    if cmd == Cmd.CAN_RECV:
                        f = self._c.decode_can_frame(payload)
                        if f:
                            f.timestamp = time.time()
                            self._rx.append(f)
            except TransportError:
                time.sleep(0.01)
            except Exception as e:  # noqa: BLE001
                log.debug("rx loop: %s", e)
                time.sleep(0.01)

    def __enter__(self):
        self.bring_up()
        return self

    def __exit__(self, *exc):
        self.close()
