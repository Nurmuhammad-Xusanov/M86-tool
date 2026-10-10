"""Minimal UDS (ISO 14229) client over an IsoTp link."""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Optional

from .isotp import IsoTp

log = logging.getLogger(__name__)


class UdsError(Exception):
    def __init__(self, message: str, sid: int = 0, nrc: int = 0):
        super().__init__(message)
        self.sid = sid
        self.nrc = nrc

    @property
    def nrc_name(self) -> str:
        return NRC.get(self.nrc, f"0x{self.nrc:02X}")


NRC = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x14: "responseTooLong",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x25: "noResponseFromSubnetComponent",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted",
    0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure",
    0x73: "wrongBlockSequenceCounter",
    0x78: "requestCorrectlyReceivedResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}


@dataclass
class UdsResponse:
    service: int
    data: bytes


# service ids
SID_SESSION = 0x10
SID_RESET = 0x11
SID_CLEAR_DTC = 0x14
SID_READ_DTC = 0x19
SID_READ_DID = 0x22
SID_READ_MEM = 0x23
SID_SECURITY = 0x27
SID_WRITE_DID = 0x2E
SID_ROUTINE = 0x31
SID_REQUEST_DOWNLOAD = 0x34
SID_REQUEST_UPLOAD = 0x35
SID_TRANSFER = 0x36
SID_TRANSFER_EXIT = 0x37
SID_TESTER_PRESENT = 0x3E
SID_CONTROL_DTC = 0x85


class UdsClient:
    def __init__(self, tp: IsoTp, timeout: float = 2.0, p2_star: float = 5.0):
        self.tp = tp
        self.timeout = timeout
        self.p2_star = p2_star

    def request(self, service: int, data: bytes = b"",
                timeout: Optional[float] = None) -> UdsResponse:
        req = bytes([service]) + data
        log.debug("UDS TX: %s", req.hex(" "))
        self.tp.send(req)

        deadline = timeout if timeout is not None else self.timeout
        while True:
            resp = self.tp.recv(timeout=deadline)
            if resp is None:
                raise UdsError(f"no response to service 0x{service:02X}",
                               sid=service)
            sid = resp[0]
            if sid == 0x7F:
                if len(resp) < 3:
                    raise UdsError("malformed negative response", sid=service)
                nrc = resp[2]
                if nrc == 0x78:  # response pending — keep waiting
                    log.debug("UDS: responsePending")
                    deadline = self.p2_star
                    continue
                raise UdsError(
                    f"negative response to 0x{service:02X}: "
                    f"{NRC.get(nrc, 'unknown')} (0x{nrc:02X})",
                    sid=service, nrc=nrc)
            if sid != (service | 0x40):
                # ignore unrelated frames
                continue
            log.debug("UDS RX: %s", resp.hex(" "))
            return UdsResponse(service=service, data=resp[1:])

    # ---- convenience services ----
    def tester_present(self, suppress: bool = True) -> None:
        sub = 0x80 if suppress else 0x00
        self.request(SID_TESTER_PRESENT, bytes([sub]))

    def session(self, session_type: int) -> UdsResponse:
        # 0x01 default, 0x02 programming, 0x03 extended
        return self.request(SID_SESSION, bytes([session_type]))

    def ecu_reset(self, mode: int = 0x01) -> UdsResponse:
        return self.request(SID_RESET, bytes([mode]))

    def read_did(self, did: int) -> bytes:
        r = self.request(SID_READ_DID, struct.pack(">H", did))
        return r.data

    def read_memory(self, address: int, size: int,
                    addr_len: int = 4, size_len: int = 4) -> bytes:
        alfid = ((size_len & 0x0F) << 4) | (addr_len & 0x0F)
        payload = bytes([alfid])
        payload += address.to_bytes(addr_len, "big")
        payload += size.to_bytes(size_len, "big")
        r = self.request(SID_READ_MEM, payload)
        return r.data

    def security_seed(self, level: int) -> bytes:
        r = self.request(SID_SECURITY, bytes([level]))
        return r.data

    def security_key(self, level: int, key: bytes) -> bytes:
        r = self.request(SID_SECURITY, bytes([level + 1]) + key)
        return r.data

    def request_upload(self, address: int, size: int,
                       addr_len: int = 4, size_len: int = 4) -> bytes:
        alfid = ((size_len & 0x0F) << 4) | (addr_len & 0x0F)
        payload = bytes([alfid])
        payload += address.to_bytes(addr_len, "big")
        payload += size.to_bytes(size_len, "big")
        r = self.request(SID_REQUEST_UPLOAD, payload)
        return r.data

    def transfer(self, block_seq: int, data: bytes) -> bytes:
        r = self.request(SID_TRANSFER, bytes([block_seq & 0xFF]) + data)
        return r.data

    # ---- DTC services (0x19 / 0x14) ----
    def read_dtc_number(self, status_mask: int = 0xFF) -> int:
        r = self.request(SID_READ_DTC, bytes([0x01, status_mask]))
        if len(r.data) < 4:
            raise UdsError("short readDTC count response", sid=SID_READ_DTC)
        return int.from_bytes(r.data[-2:], "big")

    def read_dtcs(self, status_mask: int = 0xFF) -> list:
        """Returns [(code_str, hi, lo, status)] from 0x19 0x02."""
        r = self.request(SID_READ_DTC, bytes([0x02, status_mask]))
        data = r.data[1:]  # skip statusAvailabilityMask
        out = []
        for i in range(0, len(data) - 2, 3):
            hi, lo, status = data[i], data[i + 1], data[i + 2]
            out.append(format_dtc(hi, lo, status))
        return out

    def clear_dtcs(self, group: int = 0xFFFFFF) -> None:
        self.request(SID_CLEAR_DTC, group.to_bytes(3, "big"))

    def read_dtc_snapshot_identification(self) -> bytes:
        r = self.request(SID_READ_DTC, bytes([0x03]))
        return r.data

    # ---- live data ----
    def read_by_periodic_identifier(self, mode: int = 0x02,
                                    identifiers: list = ()) -> bytes:
        payload = bytes([mode]) + bytes(identifiers)
        r = self.request(SID_READ_DID, payload, timeout=self.timeout)
        return r.data


def format_dtc(hi: int, lo: int, status: int) -> tuple:
    """Decode a 3-byte UDS DTC record to (code, hi, lo, status_bits).

    Uses the ISO 15031-6 (SAE J1979) presentation as used on VAZ ECUs:
    bits 7-6 of the high byte select P/C/B/U.
    """
    letters = ("P", "C", "B", "U")
    first = letters[(hi >> 6) & 0x03]
    code = f"{first}{(hi >> 4) & 0x03:X}{hi & 0x0F:X}{(lo >> 4) & 0x0F:X}{lo & 0x0F:X}"
    return (code, hi, lo, status)


DTC_STATUS_BITS = {
    0x01: "testFailed",
    0x02: "testFailedThisOperationCycle",
    0x04: "pendingDTC",
    0x08: "confirmedDTC",
    0x10: "testNotCompletedSinceLastClear",
    0x20: "testFailedSinceLastClear",
    0x40: "testNotCompletedThisOperationCycle",
    0x80: "warningIndicatorRequested",
}


def dtc_status_str(status: int) -> str:
    names = [name for bit, name in DTC_STATUS_BITS.items() if status & bit]
    return ",".join(names) if names else f"0x{status:02X}"
