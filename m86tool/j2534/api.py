"""J2534 PassThru API implementation backed by m86tool + SM2 adapter.

Implements minimal v04.04 subset: Open/Close/Connect/Disconnect
ReadMsgs/WriteMsgs/StartMsgFilter/StopMsgFilter/Ioctl/ReadVersion/GetLastError.

Linux-only userspace backend via libusb (no Windows DLL required).
"""

from __future__ import annotations

import ctypes as c
import time
from collections import deque
from typing import Optional, Tuple

from ..isotp import IsoTp, IsoTpError
from ..sm2.device import SM2, SM2Error
from ..sm2.protocol import Cmd
from .defs import (
    CAN_29BIT_ID,
    CLEAR_MSG_FILTERS,
    CLEAR_RX_QUEUE,
    CLEAR_TX_QUEUE,
    CONFIG_DATA_RATE,
    CONFIG_LIST,
    CONFIG_LOOPBACK,
    ERR_BUFFER_EMPTY,
    ERR_CHANNEL_NOT_OPEN,
    ERR_DEVICE_NOT_CONNECTED,
    ERR_DEVICE_NOT_OPEN,
    ERR_INVALID_FILTER_ID,
    ERR_MSG_PROTOCOL_ID,
    ERR_NO_FLOW_CONTROL,
    ERR_TIMEOUT,
    FLOW_CONTROL_FILTER,
    GET_CONFIG,
    PASS_FILTER,
    PASSTHRU_MSG,
    PROTOCOL_ISO15765,
    SCONFIG,
    SCONFIG_LIST,
    SET_CONFIG,
    STATUS_NOERROR,
)

TX_ID_DEFAULT = 0x7E0
RX_ID_DEFAULT = 0x7E8


class J2534Error(Exception):
    def __init__(self, code: int):
        self.code = code
        super().__init__(f"J2534 error 0x{code:02X}")


class DeviceHandle:
    def __init__(self, pid: Optional[int] = None):
        self.sm2 = SM2(pid=pid)
        self.opened = False
        self.last_err: str = ""
        self.channel: Optional["ChannelHandle"] = None


class ChannelHandle:
    def __init__(self, dev: DeviceHandle, protocol: int, baudrate: int):
        self.dev = dev
        self.protocol = protocol
        self.baudrate = baudrate
        self.opened = True
        self.tp: Optional[IsoTp] = None
        self.rxq: deque = deque(maxlen=1024)
        self.filters: dict = {}
        self.baud = baudrate
        # for ISO-TP: set FC in start filter
        self.flow_ctrl_set = False
        self.fc_id: Optional[int] = None
        self.tx_id: int = TX_ID_DEFAULT
        self.rx_id: int = RX_ID_DEFAULT

    def start_rx(self):
        # background RX already running after sm2.open_can
        pass

    def dispatch(self):
        if not self.dev.sm2:
            return
        f = self.dev.sm2.recv(timeout=0.001)
        if not f:
            return
        # enqueue as RxStatus PRESENT; caller builds PASSTHRU_MSG
        self.rxq.append(f)


class J2534Impl:
    """Singleton-ish in-process J2534 implementation."""

    def __init__(self):
        self.devices = []
        self.last_error: str = "NOERROR"

    def _find_open_device(self, devid: int) -> Optional[DeviceHandle]:
        if 0 <= devid < len(self.devices):
            return self.devices[devid]
        return None

    # ---- API ----
    def PassThruOpen(self, pname, pDeviceID) -> int:
        d = DeviceHandle(pid=None)
        try:
            d.sm2.bring_up()
        except SM2Error as e:
            self.last_error = str(e)
            return ERR_DEVICE_NOT_CONNECTED
        d.opened = True
        if not self.devices:
            self.devices.append(d)
            idx = 0
        else:
            # append
            self.devices.append(d)
            idx = len(self.devices) - 1
        if pDeviceID:
            pDeviceID.contents.value = idx
        return STATUS_NOERROR

    def PassThruClose(self, devid: int) -> int:
        d = self._find_open_device(devid)
        if not d or not d.opened:
            return ERR_DEVICE_NOT_OPEN
        try:
            if d.channel:
                d.channel.opened = False
            d.sm2.close()
        except Exception:
            pass
        d.opened = False
        d.channel = None
        return STATUS_NOERROR

    def PassThruConnect(self, devid: int, protocol: int, flags: int,
                        baudrate: int, pChannelID) -> int:
        d = self._find_open_device(devid)
        if not d or not d.opened:
            return ERR_DEVICE_NOT_OPEN
        if protocol != PROTOCOL_ISO15765:
            self.last_error = "only ISO15765 supported in this minimal impl"
            return ERR_MSG_PROTOCOL_ID
        try:
            rc = d.sm2.open_can(baudrate)
            if rc is None:
                pass
        except SM2Error as e:
            self.last_error = str(e)
            return ERR_DEVICE_NOT_CONNECTED
        ch = ChannelHandle(d, protocol, baudrate)
        # build tp
        ch.tp = IsoTp(d.sm2, TX_ID_DEFAULT, RX_ID_DEFAULT)
        d.channel = ch
        # find slot
        idx = 0
        # return channel id as index? keep simple
        pChannelID.contents.value = id(ch)
        return STATUS_NOERROR

    def PassThruDisconnect(self, ch_id: int) -> int:
        ch = self._resolve_ch(ch_id)
        if not ch:
            return ERR_CHANNEL_NOT_OPEN
        ch.opened = False
        return STATUS_NOERROR

    def _resolve_ch(self, ch_id: int) -> Optional[ChannelHandle]:
        for d in self.devices:
            if d.channel and id(d.channel) == ch_id:
                return d.channel
        return None

    def PassThruWriteMsgs(self, ch_id: int, pMsg, pNumMsgs, Timeout) -> int:
        ch = self._resolve_ch(ch_id)
        if not ch or not ch.tp:
            return ERR_CHANNEL_NOT_OPEN
        nm = pNumMsgs.contents.value
        msgs = pMsg
        ok = 0
        for i in range(nm):
            m = msgs[i]
            if m.ProtocolID != PROTOCOL_ISO15765:
                return ERR_MSG_PROTOCOL_ID
            data = bytes(m.Data)[: m.DataSize]
            try:
                if not ch.flow_ctrl_set:
                    # try to send anyway but spec says need FC
                    pass
                ch.tp.send(data)
                ok += 1
            except IsoTpError as e:
                self.last_error = str(e)
                return ERR_NO_FLOW_CONTROL if "flow" in str(e).lower() else ERR_INVALID_MSG
            except SM2Error as e:
                self.last_error = str(e)
                return ERR_DEVICE_NOT_CONNECTED
        pNumMsgs.contents.value = ok
        return STATUS_NOERROR

    def PassThruReadMsgs(self, ch_id: int, pMsg, pNumMsgs, Timeout) -> int:
        ch = self._resolve_ch(ch_id)
        if not ch:
            return ERR_CHANNEL_NOT_OPEN
        ch.dispatch()
        nm_req = pNumMsgs.contents.value
        ok = 0
        msgs = pMsg
        t0 = time.monotonic()
        tout_ms = Timeout
        tout = tout_ms / 1000.0 if tout_ms else 0
        while ok < nm_req:
            if ch.rxq:
                f = ch.rxq.popleft()
                m = msgs[ok]
                m.ProtocolID = PROTOCOL_ISO15765
                m.TxFlags = 0
                m.RxStatus = 0
                # pack CAN id + data into J2534 Data
                # common: put id as 4 bytes big-endian? some apps vary
                canid = f.arbitration_id & 0x7FF
                buf = bytearray(4)
                buf[0] = (canid >> 24) & 0xFF
                buf[1] = (canid >> 16) & 0xFF
                buf[2] = (canid >> 8) & 0xFF
                buf[3] = canid & 0xFF
                buf.extend(f.data)
                d = bytes(buf)[:4128]
                c.memmove(m.Data, d, len(d))
                m.DataSize = len(d)
                m.ExtraDataIndex = m.DataSize
                ok += 1
                continue
            if tout > 0 and (time.monotonic() - t0) >= tout:
                break
            time.sleep(0.001)
            ch.dispatch()
        pNumMsgs.contents.value = ok
        if ok == 0 and tout > 0:
            return ERR_TIMEOUT
        return STATUS_NOERROR

    def PassThruStartMsgFilter(self, ch_id: int, ft: int, pMask, pPattern,
                               pFlowControl, pFilterID) -> int:
        ch = self._resolve_ch(ch_id)
        if not ch:
            return ERR_CHANNEL_NOT_OPEN
        if ft != FLOW_CONTROL_FILTER:
            # naive: store filter, but for now mainly FC
            fid = len(ch.filters) + 1
            ch.filters[fid] = {"type": ft, "mask": pMask, "pat": pPattern}
            if pFilterID:
                pFilterID.contents.value = fid
            return STATUS_NOERROR
        ch.flow_ctrl_set = True
        ch.fc_id = len(ch.filters) + 1
        if pFilterID:
            pFilterID.contents.value = ch.fc_id
        return STATUS_NOERROR

    def PassThruStopMsgFilter(self, ch_id: int, fid: int) -> int:
        ch = self._resolve_ch(ch_id)
        if not ch:
            return ERR_CHANNEL_NOT_OPEN
        if fid in ch.filters:
            ch.filters.pop(fid)
            if fid == ch.fc_id:
                ch.flow_ctrl_set = False
                ch.fc_id = None
            return STATUS_NOERROR
        return ERR_INVALID_FILTER_ID

    def PassThruReadVersion(self, devid: int, pFirmwareVersion,
                            pDllVersion, pApiVersion) -> int:
        d = self._find_open_device(devid)
        if not d:
            return ERR_DEVICE_NOT_OPEN
        fw = "m86tool-0.1.0"
        dll = "m86tool-j2534-0.1.0"
        api = "04.04"
        try:
            pFirmwareVersion.contents.value = fw.encode("ascii")
            pDllVersion.contents.value = dll.encode("ascii")
            pApiVersion.contents.value = api.encode("ascii")
        except Exception:
            pass
        return STATUS_NOERROR

    def PassThruGetLastError(self, pErrorDescription) -> int:
        try:
            pErrorDescription.contents.value = self.last_error.encode("ascii")
        except Exception:
            pass
        return STATUS_NOERROR

    def PassThruIoctl(self, ch_id_or_dev: int, ioctl: int, pInput, pOutput) -> int:
        # minimal
        ch = self._resolve_ch(ch_id_or_dev)
        if ch is None:
            return ERR_CHANNEL_NOT_OPEN
        if ioctl == CLEAR_MSG_FILTERS:
            ch.filters.clear()
            ch.flow_ctrl_set = False
            ch.fc_id = None
            return STATUS_NOERROR
        if ioctl == CLEAR_RX_QUEUE:
            ch.rxq.clear()
            return STATUS_NOERROR
        if ioctl == CLEAR_TX_QUEUE:
            return STATUS_NOERROR
        return STATUS_NOERROR


impl = J2534Impl()
