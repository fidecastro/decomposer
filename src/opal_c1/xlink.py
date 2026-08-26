"""Minimal XLink client for the Opal C1's vendor bulk interface.

Why this exists: depthai will not attach to the firmware already running on the
camera. It reboots the device through its bootloader into stock DepthAI
firmware, which has no UVC and no audio, so /dev/video0 and the mic array both
disappear for as long as it is connected.

Opal's own firmware keeps all of that alive and - going by Composer's macOS
behaviour, where the raw UVC device and the Composer feed were visible at the
same time - serves XLink alongside it. Interface 0 can be claimed with libusb
without triggering the mode switch, so if that firmware answers XLink we get
video, microphone and manual camera control simultaneously.

Wire format, from luxonis/XLink (src/shared/XLinkDispatcherImpl.c):
dispatcherEventSend writes the 84-byte event header raw to the bulk OUT
endpoint, followed by the payload only for XLINK_WRITE_REQ. Responses are read
as another raw 84-byte header. There is no framing or preamble.
"""

from __future__ import annotations

import struct
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Optional

import usb.core
import usb.util

VID = 0x03E7
PID_CAMERA = 0xF63D   # Opal firmware: UVC + UAC2 mic + vendor bulk
PID_DEPTHAI = 0xF63B  # stock DepthAI firmware: vendor bulk only

# usb_host.cpp: USB_ENDPOINT_IN / USB_ENDPOINT_OUT
EP_IN = 0x81
EP_OUT = 0x01

MAX_STREAM_NAME_LENGTH = 52

# xLinkEventType_t, in declaration order (XLinkPrivateDefines.h)
XLINK_WRITE_REQ = 0
XLINK_READ_REQ = 1
XLINK_READ_REL_REQ = 2
XLINK_CREATE_STREAM_REQ = 3
XLINK_CLOSE_STREAM_REQ = 4
XLINK_PING_REQ = 5
XLINK_RESET_REQ = 6
XLINK_REQUEST_LAST = 7
XLINK_WRITE_RESP = 8
XLINK_READ_RESP = 9
XLINK_READ_REL_RESP = 10
XLINK_CREATE_STREAM_RESP = 11
XLINK_CLOSE_STREAM_RESP = 12
XLINK_PING_RESP = 13
XLINK_RESET_RESP = 14
XLINK_RESP_LAST = 15

TYPE_NAMES = {
    XLINK_WRITE_REQ: "WRITE_REQ", XLINK_READ_REQ: "READ_REQ",
    XLINK_READ_REL_REQ: "READ_REL_REQ", XLINK_CREATE_STREAM_REQ: "CREATE_STREAM_REQ",
    XLINK_CLOSE_STREAM_REQ: "CLOSE_STREAM_REQ", XLINK_PING_REQ: "PING_REQ",
    XLINK_RESET_REQ: "RESET_REQ", XLINK_WRITE_RESP: "WRITE_RESP",
    XLINK_READ_RESP: "READ_RESP", XLINK_READ_REL_RESP: "READ_REL_RESP",
    XLINK_CREATE_STREAM_RESP: "CREATE_STREAM_RESP",
    XLINK_CLOSE_STREAM_RESP: "CLOSE_STREAM_RESP", XLINK_PING_RESP: "PING_RESP",
    XLINK_RESET_RESP: "RESET_RESP",
}

# id(i) type(i) streamName[52] tnsec(I) tsecLsb(I) tsecMsb(I) streamId(I) size(I) flags(I)
HEADER_FMT = "<ii52sIIIIII"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
assert HEADER_SIZE == 84, HEADER_SIZE

# flags bitfield, LSB first
FLAG_ACK = 1 << 0
FLAG_NACK = 1 << 1
FLAG_BLOCK = 1 << 2
FLAG_LOCAL_SERVE = 1 << 3
FLAG_TERMINATE = 1 << 4
FLAG_BUFFER_FULL = 1 << 5
FLAG_SIZE_TOO_BIG = 1 << 6
FLAG_NO_SUCH_STREAM = 1 << 7
FLAG_MOVE_SEMANTIC = 1 << 8

_FLAG_NAMES = [
    (FLAG_ACK, "ack"), (FLAG_NACK, "nack"), (FLAG_BLOCK, "block"),
    (FLAG_LOCAL_SERVE, "localServe"), (FLAG_TERMINATE, "terminate"),
    (FLAG_BUFFER_FULL, "bufferFull"), (FLAG_SIZE_TOO_BIG, "sizeTooBig"),
    (FLAG_NO_SUCH_STREAM, "noSuchStream"), (FLAG_MOVE_SEMANTIC, "moveSemantic"),
]


def decode_flags(raw: int) -> list[str]:
    return [n for bit, n in _FLAG_NAMES if raw & bit]


@dataclass
class Event:
    id: int = 0
    type: int = XLINK_PING_REQ
    stream_name: bytes = b""
    stream_id: int = 0
    size: int = 0
    flags: int = 0
    tnsec: int = 0
    tsec_lsb: int = 0
    tsec_msb: int = 0
    payload: bytes = field(default=b"", repr=False)

    def pack(self) -> bytes:
        return struct.pack(
            HEADER_FMT, self.id, self.type,
            self.stream_name[:MAX_STREAM_NAME_LENGTH], self.tnsec,
            self.tsec_lsb, self.tsec_msb, self.stream_id, self.size, self.flags,
        )

    @classmethod
    def unpack(cls, raw: bytes) -> "Event":
        (eid, etype, name, tnsec, lsb, msb, sid, size, flags) = struct.unpack(
            HEADER_FMT, raw[:HEADER_SIZE]
        )
        return cls(
            id=eid, type=etype, stream_name=name.split(b"\x00")[0],
            stream_id=sid, size=size, flags=flags,
            tnsec=tnsec, tsec_lsb=lsb, tsec_msb=msb,
        )

    def describe(self) -> str:
        t = TYPE_NAMES.get(self.type, f"type{self.type}")
        f = ",".join(decode_flags(self.flags)) or "-"
        name = self.stream_name.decode("ascii", "replace")
        return (f"{t} id={self.id} stream={name!r} streamId={self.stream_id} "
                f"size={self.size} flags={f}")


class XLinkUSB:
    """Raw XLink transport over the vendor bulk interface.

    Claiming interface 0 does not disturb the camera: /dev/video0 keeps
    streaming and the mic stays present.
    """

    def __init__(self, pid: int = PID_CAMERA):
        self.pid = pid
        self.dev = None
        self._id = 0

    def __enter__(self) -> "XLinkUSB":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> "XLinkUSB":
        self.dev = usb.core.find(idVendor=VID, idProduct=self.pid)
        if self.dev is None:
            raise RuntimeError(
                f"No device {VID:04x}:{self.pid:04x}. Is the C1 plugged in, and are "
                "the udev rules from packaging/60-opal-c1.rules installed?"
            )
        usb.util.claim_interface(self.dev, 0)
        return self

    def close(self) -> None:
        if self.dev is not None:
            with suppress(Exception):
                usb.util.release_interface(self.dev, 0)
            with suppress(Exception):
                usb.util.dispose_resources(self.dev)
            self.dev = None

    def next_id(self) -> int:
        self._id += 1
        return self._id

    def send(self, event: Event, timeout: int = 2000) -> int:
        now = time.monotonic()
        event.tsec_lsb = int(now) & 0xFFFFFFFF
        event.tsec_msb = (int(now) >> 32) & 0xFFFFFFFF
        event.tnsec = int((now % 1) * 1e9)
        n = self.dev.write(EP_OUT, event.pack(), timeout=timeout)
        if event.type == XLINK_WRITE_REQ and event.payload:
            n += self.dev.write(EP_OUT, event.payload, timeout=timeout)
        return n

    def recv(self, timeout: int = 2000) -> Optional[Event]:
        data = self.dev.read(EP_IN, HEADER_SIZE, timeout=timeout)
        if len(data) < HEADER_SIZE:
            return None
        return Event.unpack(bytes(data))

    def ping(self, timeout: int = 2000) -> Optional[Event]:
        """Send XLINK_PING_REQ. A PING_RESP proves an XLink server is listening."""
        self.send(Event(id=self.next_id(), type=XLINK_PING_REQ), timeout=timeout)
        return self.recv(timeout=timeout)
