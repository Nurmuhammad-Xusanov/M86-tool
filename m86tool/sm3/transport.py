"""Reading whole SM3 messages off a byte stream.

TCP delivers a stream, not a sequence of messages: one ``recv`` may return half
a message, three messages, or a message and a half. Treating a read as a message
makes the greeting come back too short to hold a serial, and every later read is
offset by the leftovers. So the socket feeds a buffer and messages are taken out
of it by length, never by read boundary.

The adapter is a plain TCP server on its own access point: join
``DIRECT-SCANMATIK-#<serial>`` and connect to ``192.168.81.1:777``.
"""

from __future__ import annotations

import socket
import time
from typing import Optional

from . import frame

DEFAULT_HOST = "192.168.81.1"
DEFAULT_PORT = 777

#: How long one socket read blocks before the deadline is rechecked.
READ_SLICE = 0.010

_BUFFER_SIZE = 1 << 16


class TransportError(IOError):
    """Raised when the link is gone or the stream cannot be resynchronised."""


class TcpTransport:
    """A buffered message reader over a TCP socket."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        self.host = host
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._buf = bytearray(_BUFFER_SIZE)
        self._used = 0
        self.resynchronised = 0
        self.last_fault: Optional[str] = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def open(self, timeout: float = 4.0) -> None:
        self.close()
        self.last_fault = None
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((self.host, self.port))
        except OSError:
            s.close()
            raise
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = s
        self._used = 0

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._used = 0

    def send(self, data: bytes) -> None:
        if self._sock is None:
            raise TransportError("not connected")
        try:
            self._sock.sendall(data)
        except OSError as e:
            self._fail(f"write failed: {e}")
            raise TransportError(f"write failed: {e}") from e

    def next_message(self, deadline: float) -> Optional[frame.Message]:
        """Take the next whole message out of the buffer. ``None`` on timeout."""
        while True:
            if self._used >= frame.HEADER:
                size = frame.size_at(self._buf, 0)
                if size is None:
                    # Not a header. One byte at a time is the only honest way
                    # back: the stream has no marker to search for.
                    self._discard(1)
                    self.resynchronised += 1
                    continue
                if self._used >= size:
                    result = frame.decode(self._buf, 0)
                    self._discard(size)
                    if result is not None:
                        return result[0]
                    self.resynchronised += size
                    continue
                if size > len(self._buf):
                    why = f"a message of {size} bytes does not fit the buffer"
                    self._fail(why)
                    raise TransportError(why)
            if not self._fill(deadline):
                return None

    def _fill(self, deadline: float) -> bool:
        if self._sock is None:
            raise TransportError("not connected")
        while time.monotonic() < deadline:
            if self._used == len(self._buf):
                self._fail("read buffer full with nothing that parses")
                raise TransportError("read buffer full with nothing that parses")
            remaining = deadline - time.monotonic()
            self._sock.settimeout(min(READ_SLICE, max(remaining, 0.001)))
            try:
                chunk = self._sock.recv(len(self._buf) - self._used)
            except socket.timeout:
                continue
            except OSError as e:
                self._fail(f"read failed: {e}")
                raise TransportError(f"read failed: {e}") from e
            if not chunk:
                self._fail("the adapter closed the connection")
                raise TransportError("the adapter closed the connection")
            self._buf[self._used:self._used + len(chunk)] = chunk
            self._used += len(chunk)
            return True
        return False

    def _discard(self, n: int) -> None:
        self._buf[:self._used - n] = self._buf[n:self._used]
        self._used -= n

    def _fail(self, why: str) -> None:
        self.last_fault = why
        self.close()
