"""ctypes-friendly wrappers for loading via DLL-style API or for testing."""

from __future__ import annotations

import ctypes as c
import os

from .defs import (
    GET_CONFIG,
    SET_CONFIG,
    SCONFIG,
    SCONFIG_LIST,
    STATUS_NOERROR,
)
from .api import impl

# library init
_lib = None


# ---- call into impl ----
def PassThruOpen(pName, pDeviceID):
    return impl.PassThruOpen(pName, pDeviceID)


def PassThruClose(DeviceID):
    return impl.PassThruClose(DeviceID)


def PassThruConnect(DeviceID, ProtocolID, Flags, Baudrate, pChannelID):
    return impl.PassThruConnect(DeviceID, ProtocolID, Flags, Baudrate, pChannelID)


def PassThruDisconnect(ChannelID):
    return impl.PassThruDisconnect(ChannelID)


def PassThruWriteMsgs(ChannelID, pMsg, pNumMsgs, Timeout):
    return impl.PassThruWriteMsgs(ChannelID, pMsg, pNumMsgs, Timeout)


def PassThruReadMsgs(ChannelID, pMsg, pNumMsgs, Timeout):
    return impl.PassThruReadMsgs(ChannelID, pMsg, pNumMsgs, Timeout)


def PassThruStartMsgFilter(ChannelID, FilterType, pMask, pPattern, pFlowControl,
                           pFilterID):
    return impl.PassThruStartMsgFilter(ChannelID, FilterType, pMask, pPattern,
                                      pFlowControl, pFilterID)


def PassThruStopMsgFilter(ChannelID, FilterID):
    return impl.PassThruStopMsgFilter(ChannelID, FilterID)


def PassThruReadVersion(DeviceID, pFirmwareVersion, pDllVersion, pApiVersion):
    return impl.PassThruReadVersion(DeviceID, pFirmwareVersion, pDllVersion,
                                    pApiVersion)


def PassThruGetLastError(pErrorDescription):
    return impl.PassThruGetLastError(pErrorDescription)


def PassThruIoctl(ChannelID, IoctlID, pInput, pOutput):
    return impl.PassThruIoctl(ChannelID, IoctlID, pInput, pOutput)
