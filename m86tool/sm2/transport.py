"""USB transport for Scanmatik SM2 adapters via libusb (no kernel driver)."""

from __future__ import annotations

import logging
import threading
import time
from typing import List, Optional

import usb.core
import usb.util

log = logging.getLogger(__name__)

SM2_VID = 0x20A2
KNOWN_PIDS = {
    0x0001: "SM2 Pro",
    0x0002: "SM2 (original)",
}

EP_IN = 0x81
EP_OUT = 0x02

DEFAULT_WRITE_TIMEOUT = 1000
DEFAULT_READ_TIMEOUT = 500


class TransportError(Exception):
    pass


class DeviceNotFound(TransportError):
    pass


class USBTransport:
    def __init__(self, vid: int = SM2_VID, pid: Optional[int] = None,
                 bus: Optional[int] = None, address: Optional[int] = None):
        self.vid = vid
        self.pid = pid
        self.bus = bus
        self.address = address
        self._dev = None
        self._ep_out = None
        self._ep_in = None
        self._is_open = False
        self._wlock = threading.Lock()
        self._rlock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return self._is_open

    @classmethod
    def list_devices(cls, vid: int = SM2_VID) -> List[dict]:
        out = []
        try:
            for dev in usb.core.find(find_all=True, idVendor=vid):
                d = {"bus": dev.bus, "address": dev.address,
                     "vid": dev.idVendor, "pid": dev.idProduct,
                     "model": KNOWN_PIDS.get(dev.idProduct, "unknown")}
                try:
                    d["serial"] = dev.serial_number
                except Exception:
                    d["serial"] = None
                out.append(d)
        except Exception:
            pass
        return out

    def open(self) -> None:
        if self._is_open:
            return
        self._dev = self._find()
        if self._dev is None:
            pids = f"0x{self.pid:04X}" if self.pid else "0x0001/0x0002"
            raise DeviceNotFound(
                f"Scanmatik not found (VID=0x{self.vid:04X}, PID={pids}). "
                f"Check the cable and udev permissions."
            )
        self._detach_kernel_driver()
        self._set_configuration()
        self._find_endpoints()
        self._is_open = True
        log.info("opened Scanmatik %s (PID=0x%04X, EP OUT=0x%02X IN=0x%02X)",
                 KNOWN_PIDS.get(self._dev.idProduct, "?"),
                 self._dev.idProduct, self._ep_out.bEndpointAddress,
                 self._ep_in.bEndpointAddress)

    def close(self) -> None:
        if not self._is_open:
            return
        try:
            usb.util.dispose_resources(self._dev)
        except Exception:
            pass
        self._dev = self._ep_out = self._ep_in = None
        self._is_open = False

    def write(self, data: bytes, timeout_ms: int = DEFAULT_WRITE_TIMEOUT) -> int:
        self._check()
        with self._wlock:
            try:
                return self._ep_out.write(data, timeout=timeout_ms)
            except usb.core.USBError as e:
                raise TransportError(f"USB write error: {e}") from e

    def read(self, size: int = 4096,
             timeout_ms: int = DEFAULT_READ_TIMEOUT) -> Optional[bytes]:
        self._check()
        with self._rlock:
            try:
                data = self._ep_in.read(size, timeout=timeout_ms)
                return bytes(data) if data is not None and len(data) else None
            except usb.core.USBTimeoutError:
                return None
            except usb.core.USBError as e:
                if "timeout" in str(e).lower():
                    return None
                raise TransportError(f"USB read error: {e}") from e

    def read_all(self, timeout_ms: int = DEFAULT_READ_TIMEOUT,
                 max_packets: int = 16) -> bytes:
        out = bytearray()
        for _ in range(max_packets):
            chunk = self.read(timeout_ms=timeout_ms)
            if chunk is None:
                break
            out.extend(chunk)
        return bytes(out)

    def flush_input(self) -> int:
        drained = 0
        while True:
            chunk = self.read(timeout_ms=50)
            if chunk is None:
                break
            drained += len(chunk)
        return drained

    def write_read(self, data: bytes, timeout_ms: int = 500) -> Optional[bytes]:
        self.flush_input()
        self.write(data)
        time.sleep(0.02)
        return self.read_all(timeout_ms=timeout_ms)

    # internals
    def _check(self) -> None:
        if not self._is_open:
            raise TransportError("transport not open")

    def _find(self):
        kwargs = {"idVendor": self.vid}
        if self.pid:
            kwargs["idProduct"] = self.pid
        if self.bus is not None and self.address is not None:
            for dev in usb.core.find(find_all=True, **kwargs):
                if dev.bus == self.bus and dev.address == self.address:
                    return dev
            return None
        return usb.core.find(**kwargs)

    def _set_configuration(self) -> None:
        try:
            self._dev.set_configuration()
        except usb.core.USBError:
            pass

    def _detach_kernel_driver(self) -> None:
        for cfg in self._dev:
            for intf in cfg:
                try:
                    if self._dev.is_kernel_driver_active(intf.bInterfaceNumber):
                        self._dev.detach_kernel_driver(intf.bInterfaceNumber)
                except (usb.core.USBError, NotImplementedError):
                    pass

    def _find_endpoints(self) -> None:
        cfg = self._dev.get_active_configuration()
        for intf in cfg:
            for ep in intf:
                if (ep.bmAttributes & 0x03) != 0x02:
                    continue
                direction = usb.util.endpoint_direction(ep.bEndpointAddress)
                if direction == usb.util.ENDPOINT_OUT and self._ep_out is None:
                    self._ep_out = ep
                elif direction == usb.util.ENDPOINT_IN and self._ep_in is None:
                    self._ep_in = ep
        if self._ep_out is None or self._ep_in is None:
            raise TransportError("bulk IN/OUT endpoints not found")

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
