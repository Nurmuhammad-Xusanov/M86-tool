"""High-level M86 (Itelma) ECU access over Scanmatik + ISO-TP + UDS.

M86 flash layout (from ./Documents/m86.md):
    0x000000  boot + program code
    0x010000  main program (192 KB)
    0x040000  EEPROM image (8 blocks of 8 KB)
    0x080000  calibration data
    0x10FFFF  end (full image = 0x110000 = 1 114 112 bytes)
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .isotp import IsoTp, IsoTpError
from .sm2.device import SM2, SM2Error
from .sm2.transport import SM2_VID, USBTransport
from .sm3.device import Sm3Client, Sm3Error
from .sm3.transport import DEFAULT_HOST, DEFAULT_PORT
from .uds import SID_READ_DID, UdsClient, UdsError, dtc_status_str

log = logging.getLogger(__name__)

# Common 11-bit UDS diagnostic addresses for VAZ/Itelma engine ECUs.
DEFAULT_TX_ID = 0x7E0
DEFAULT_RX_ID = 0x7E8

FLASH_SIZE = 0x110000
EEPROM_BASE = 0x040000
EEPROM_SIZE = 0x10000
CALIB_BASE = 0x080000


def is_sm3_reachable(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 0.3) -> bool:
    """Quick probe to check if an SM3 Wi-Fi adapter is reachable on TCP."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        return True
    except (OSError, socket.timeout):
        return False


# Standard ISO 14229 DIDs (best effort; M86 also exposes manufacturer DIDs).
STANDARD_DIDS = {
    0xF186: "activeDiagnosticSession",
    0xF187: "sparePartNumber",
    0xF188: "ECUSoftwareNumber",
    0xF189: "ECUSoftwareVersionNumber",
    0xF18A: "systemSupplierIdentifier",
    0xF18B: "ECUManufacturingDate",
    0xF18C: "ECUSerialNumber",
    0xF190: "VIN",
    0xF191: "ECUHardwareNumber",
    0xF192: "systemSupplierECUHardwareNumber",
    0xF193: "systemSupplierECUHardwareVersionNumber",
    0xF194: "systemSupplierECUSoftwareNumber",
    0xF195: "systemSupplierECUSoftwareVersionNumber",
    0xF1A0: "diagnosticAddress",
}


@dataclass
class M86Identity:
    vin: Optional[str] = None
    part_number: Optional[str] = None
    sw_version: Optional[str] = None
    hw_number: Optional[str] = None
    serial: Optional[str] = None
    raw: Optional[dict] = None


def _ascii(b: bytes) -> str:
    return b.rstrip(b"\x00\xff ").decode("ascii", errors="replace")


class M86:
    def __init__(self, tx_id: int = DEFAULT_TX_ID, rx_id: int = DEFAULT_RX_ID):
        self.tx_id = tx_id
        self.rx_id = rx_id
        self.adapter = None
        self.sm2: Optional[SM2] = None
        self.sm3: Optional[Sm3Client] = None
        self.tp: Optional[IsoTp] = None
        self.uds: Optional[UdsClient] = None

    # ---- connection ----
    def connect(self, adapter: str = "auto", bitrate: int = 500000,
                pid: Optional[int] = None, host: str = DEFAULT_HOST,
                port: int = DEFAULT_PORT) -> None:
        adapter_type = adapter.lower()
        if adapter_type == "auto":
            if is_sm3_reachable(host, port):
                adapter_type = "sm3"
            else:
                adapter_type = "sm2"

        if adapter_type == "sm3":
            log.info("Connecting to Scanmatik SM3 via Wi-Fi (%s:%d)...", host, port)
            self.sm3 = Sm3Client(host=host, port=port)
            self.sm3.bring_up()
            self.sm3.open_can(bitrate)
            if not (0x7E8 <= self.rx_id < 0x7F0):
                self.sm3.keep_all = True
            self.adapter = self.sm3
            self.sm2 = self.sm3  # For backward-compatibility
        else:
            log.info("Connecting to Scanmatik SM2 via USB...")
            self.sm2 = SM2(pid=pid)
            self.sm2.bring_up()
            rc = self.sm2.open_can(bitrate)
            log.info("CAN open response: %s", rc)
            self.adapter = self.sm2

        self.tp = IsoTp(self.adapter, self.tx_id, self.rx_id)
        self.uds = UdsClient(self.tp)

    def close(self) -> None:
        if self.adapter:
            self.adapter.close()
            self.adapter = None
            self.sm2 = None
            self.sm3 = None
        elif self.sm2:
            self.sm2.close()
            self.sm2 = None

    # ---- identification ----
    def identify(self, extended_session: bool = True,
                 timeout: float = 2.0) -> M86Identity:
        assert self.uds is not None, "not connected"
        if extended_session:
            try:
                self.uds.session(0x03)
            except UdsError as e:
                log.warning("could not enter extended session: %s", e)

        found: dict = {}
        for did in STANDARD_DIDS:
            try:
                data = self.uds.read_did(did)
            except UdsError:
                continue
            found[did] = data

        idn = M86Identity(raw=found)
        if 0xF190 in found:
            idn.vin = _ascii(found[0xF190])
        if 0xF187 in found:
            idn.part_number = _ascii(found[0xF187])
        if 0xF189 in found:
            idn.sw_version = _ascii(found[0xF189])
        if 0xF191 in found:
            idn.hw_number = _ascii(found[0xF191])
        if 0xF18C in found:
            idn.serial = _ascii(found[0xF18C])
        return idn

    # ---- memory read (UDS RequestUpload 0x35 / TransferData 0x36) ----
    def read_memory(self, address: int, size: int,
                    block_size: int = 0x400,
                    progress: Optional[Callable[[int, int], None]] = None
                    ) -> bytes:
        """Read `size` bytes starting at `address` via RequestUpload + TransferData.

        NOTE: the exact TransferData request format is ECU-specific (some expect
        only the block sequence counter, others expect sequence + block length).
        This assumes seq-only; verify on hardware.
        """
        assert self.uds is not None, "not connected"
        out = bytearray()
        seq = 1
        addr = address
        remaining = size
        while remaining > 0:
            chunk = min(block_size, remaining)
            self.uds.request_upload(addr, chunk)
            block = self.uds.transfer(seq, b"")
            if not block:
                raise UdsError("empty transfer block")
            out.extend(block)
            seq = (seq + 1) & 0xFF
            addr += chunk
            remaining -= chunk
            if progress:
                progress(size - remaining, size)
        return bytes(out)

    def read_full_flash(self, block_size: int = 0x400,
                        progress: Optional[Callable[[int, int], None]] = None
                        ) -> bytes:
        return self.read_memory(0, FLASH_SIZE, block_size, progress)

    def read_eeprom(self, block_size: int = 0x100,
                    progress: Optional[Callable[[int, int], None]] = None
                    ) -> bytes:
        return self.read_memory(EEPROM_BASE, EEPROM_SIZE, block_size, progress)

    # ---- DTCs ----
    def read_dtcs(self, status_mask: int = 0xFF) -> list:
        assert self.uds is not None, "not connected"
        try:
            number = self.uds.read_dtc_number(status_mask)
        except UdsError:
            number = None
        records = self.uds.read_dtcs(status_mask)
        result = {
            "number_reported": number,
            "count": len(records),
        }
        rows = []
        for code, hi, lo, status in records:
            rows.append({"code": code, "raw": f"{hi:02X}{lo:02X}",
                         "status": hex(status),
                         "status_names": dtc_status_str(status)})
        result["dtcs"] = rows
        return result

    def clear_dtcs(self, group: int = 0xFFFFFF) -> None:
        assert self.uds is not None, "not connected"
        self.uds.clear_dtcs(group)

    # ---- live data (poll UDS 0x22 DIDs) ----
    def live_data(self, dids: list,
                  interval: float = 0.5,
                  count: int = 0,
                  callback: Optional[Callable[[float, int, bytes], None]] = None
                  ) -> None:
        """Poll the given DIDs via 0x22 until interrupted or `count` samples.

        callback(timestamp, did, payload)
        """
        assert self.uds is not None, "not connected"
        samples = 0
        while True:
            for did in dids:
                try:
                    payload = self.uds.read_did(int(did))
                except UdsError as e:
                    if callback:
                        callback(time.time(), int(did), b"")
                    log.debug("DID 0x%04X: %s", did, e)
                    continue
                if callback:
                    callback(time.time(), int(did), payload)
            samples += 1
            if count and samples >= count:
                break
            time.sleep(interval)
