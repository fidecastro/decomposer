"""Raw UVC Extension Unit access through uvcvideo's UVCIOC_CTRL_QUERY ioctl.

GET_* helpers are the default path and never write. The single write entry
point is `set_cur`; call it only from an explicit diagnostic that restores
what it changed.

Reference: USB Device Class Definition for Video Devices 1.5, section 4.2.2.2
(Extension Unit control requests) and A.8 (request codes).
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import struct
from dataclasses import dataclass, field

# Video class-specific request codes (UVC 1.5 A.8 / linux/usb/video.h)
UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81
UVC_GET_MIN = 0x82
UVC_GET_MAX = 0x83
UVC_GET_RES = 0x84
UVC_GET_LEN = 0x85
UVC_GET_INFO = 0x86
UVC_GET_DEF = 0x87

QUERY_NAMES = {
    UVC_GET_CUR: "cur",
    UVC_GET_MIN: "min",
    UVC_GET_MAX: "max",
    UVC_GET_RES: "res",
    UVC_GET_DEF: "def",
}

# GET_INFO capability bits (UVC 1.5 4.1.2)
INFO_GET = 1 << 0
INFO_SET = 1 << 1
INFO_DISABLED = 1 << 2
INFO_AUTOUPDATE = 1 << 3
INFO_ASYNC = 1 << 4

# struct uvc_xu_control_query { u8 unit; u8 selector; u8 query; u16 size; u8 *data; }
# Native alignment puts size at offset 4 and the pointer at offset 8 (16 bytes on LP64).
_QUERY_STRUCT = "@BBBHP"
_QUERY_SIZE = struct.calcsize(_QUERY_STRUCT)

# _IOWR('u', 0x21, struct uvc_xu_control_query)
UVCIOC_CTRL_QUERY = (3 << 30) | (_QUERY_SIZE << 16) | (ord("u") << 8) | 0x21

# Errno values worth naming in a report; anything else is passed through as-is.
ERRNO_HINTS = {
    2: "ENOENT — uvcvideo has no such selector (beyond bControlSize)",
    32: "EPIPE — device stalled the request (selector not implemented)",
    22: "EINVAL — driver rejected the request (bad size or unit)",
    5: "EIO — transfer failed",
    110: "ETIMEDOUT — device did not answer",
    13: "EACCES — permission denied on the video node",
    19: "ENODEV — device went away",
}


def decode_info(info: int) -> list[str]:
    """Human-readable capability flags from a GET_INFO byte."""
    caps = []
    if info & INFO_GET:
        caps.append("get")
    if info & INFO_SET:
        caps.append("set")
    if info & INFO_DISABLED:
        caps.append("disabled-by-auto")
    if info & INFO_AUTOUPDATE:
        caps.append("autoupdate")
    if info & INFO_ASYNC:
        caps.append("async")
    return caps


def query(fd: int, unit: int, selector: int, code: int, size: int) -> bytes:
    """Issue one UVC class-specific GET request. Raises OSError on stall."""
    if code == UVC_SET_CUR:
        raise ValueError("use set_cur() for writes; query() is GET-only")
    buf = ctypes.create_string_buffer(max(size, 1))
    arg = struct.pack(
        _QUERY_STRUCT, unit, selector, code, size, ctypes.addressof(buf)
    )
    fcntl.ioctl(fd, UVCIOC_CTRL_QUERY, arg)
    return buf.raw[:size]


def set_cur(fd: int, unit: int, selector: int, data: bytes) -> None:
    """Issue one UVC SET_CUR. The only write path in this module."""
    if not data:
        raise ValueError("SET_CUR payload must be non-empty")
    size = len(data)
    buf = ctypes.create_string_buffer(data, max(size, 1))
    arg = struct.pack(
        _QUERY_STRUCT, unit, selector, UVC_SET_CUR, size, ctypes.addressof(buf)
    )
    fcntl.ioctl(fd, UVCIOC_CTRL_QUERY, arg)


def get_len(fd: int, unit: int, selector: int) -> int:
    """Payload length of a control, in bytes. GET_LEN itself always returns 2."""
    return struct.unpack("<H", query(fd, unit, selector, UVC_GET_LEN, 2))[0]


def get_info(fd: int, unit: int, selector: int) -> int:
    return query(fd, unit, selector, UVC_GET_INFO, 1)[0]


def get_cur(fd: int, unit: int, selector: int, size: int) -> bytes:
    return query(fd, unit, selector, UVC_GET_CUR, size)


@dataclass
class ControlProbe:
    """Everything we could learn about one extension-unit selector."""

    selector: int
    length: int | None = None
    info: int | None = None
    values: dict[str, bytes] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)

    @property
    def supported(self) -> bool:
        return self.length is not None and self.length > 0

    @property
    def writable(self) -> bool:
        return self.supported and self.info is not None and bool(self.info & INFO_SET)

    @property
    def caps(self) -> list[str]:
        return decode_info(self.info) if self.info is not None else []

    def as_int(self, which: str) -> int | None:
        """Interpret a stored payload as a little-endian unsigned integer."""
        raw = self.values.get(which)
        if raw is None or not raw or len(raw) > 8:
            return None
        return int.from_bytes(raw, "little")

    def to_dict(self) -> dict:
        return {
            "selector": self.selector,
            "length": self.length,
            "info": self.info,
            "caps": self.caps,
            "values": {k: v.hex() for k, v in self.values.items()},
            "errors": self.errors,
        }


def probe_selector(fd: int, unit: int, selector: int) -> ControlProbe:
    """Read-only interrogation of a single selector.

    GET_LEN first: a stall there means the selector is not implemented and we
    stop, which keeps the probe cheap and avoids hammering absent controls.
    """
    p = ControlProbe(selector=selector)

    try:
        p.length = get_len(fd, unit, selector)
    except OSError as e:
        p.errors["len"] = e.errno
        return p

    try:
        p.info = get_info(fd, unit, selector)
    except OSError as e:
        p.errors["info"] = e.errno

    if not p.length:
        return p
    if p.info is not None and not (p.info & INFO_GET):
        return p

    for code, name in QUERY_NAMES.items():
        try:
            p.values[name] = query(fd, unit, selector, code, p.length)
        except OSError as e:
            p.errors[name] = e.errno
    return p


def probe_unit(
    dev_path: str, unit: int, selectors: range, on_progress=None
) -> list[ControlProbe]:
    """Probe a range of selectors on one extension unit. Never writes."""
    out: list[ControlProbe] = []
    fd = os.open(dev_path, os.O_RDWR)
    try:
        for sel in selectors:
            probe = probe_selector(fd, unit, sel)
            out.append(probe)
            if on_progress:
                on_progress(probe)
    finally:
        os.close(fd)
    return out
