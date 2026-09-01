"""UVC camera controls over V4L2, for Call mode.

In Call mode the camera runs Opal's firmware, so the microphone and /dev/video0
are both alive but focus and white balance are locked to automatic — the device
reports those auto toggles as read-only. Everything else is adjustable here.

Stdlib only: these are plain ioctls on the video node.
"""

from __future__ import annotations

import fcntl
import os
import struct
from dataclasses import dataclass
from typing import Optional

# linux/videodev2.h
_QUERYCTRL_FMT = "<II32siiiiI2I"
_CONTROL_FMT = "<Ii"


def _iowr(nr: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord("V") << 8) | nr


VIDIOC_QUERYCTRL = _iowr(36, struct.calcsize(_QUERYCTRL_FMT))
VIDIOC_G_CTRL = _iowr(27, struct.calcsize(_CONTROL_FMT))
VIDIOC_S_CTRL = _iowr(28, struct.calcsize(_CONTROL_FMT))

# v4l2_ctrl_flags
FLAG_DISABLED = 0x0001
FLAG_GRABBED = 0x0002
FLAG_READ_ONLY = 0x0004
FLAG_UPDATE = 0x0008
FLAG_INACTIVE = 0x0010

# Control IDs actually present on the C1, from `v4l2-ctl -L`.
CONTROLS = {
    "brightness": 0x00980900,
    "contrast": 0x00980901,
    "saturation": 0x00980902,
    "hue": 0x00980903,
    "white_balance_automatic": 0x0098090C,
    "gain": 0x00980913,
    "power_line_frequency": 0x00980918,
    "sharpness": 0x0098091B,
    "backlight_compensation": 0x0098091C,
    "auto_exposure": 0x009A0901,
    "exposure_time_absolute": 0x009A0902,
    "focus_absolute": 0x009A090A,
    "focus_automatic_continuous": 0x009A090C,
}

# On the C1, gain is ISO and focus_absolute is lens position — the same units
# DepthAI uses in Studio mode. See docs/camera-notes.md.
ALIASES = {"iso": "gain", "exposure": "exposure_time_absolute", "focus": "focus_absolute"}


@dataclass
class Control:
    name: str
    id: int
    value: Optional[int]
    minimum: int
    maximum: int
    step: int
    default: int
    flags: int

    @property
    def read_only(self) -> bool:
        return bool(self.flags & FLAG_READ_ONLY)

    @property
    def inactive(self) -> bool:
        return bool(self.flags & FLAG_INACTIVE)

    @property
    def writable(self) -> bool:
        return not (self.flags & (FLAG_READ_ONLY | FLAG_DISABLED | FLAG_GRABBED))

    def describe(self) -> str:
        notes = []
        if self.read_only:
            notes.append("read-only")
        if self.inactive:
            notes.append("inactive")
        tag = f"  ({', '.join(notes)})" if notes else ""
        val = "n/a" if self.value is None else self.value
        return f"{self.name:<28} {str(val):>8}   [{self.minimum}..{self.maximum}]{tag}"


class UvcControls:
    """Read and write the camera's V4L2 controls."""

    def __init__(self, dev_path: str = "/dev/video0"):
        self.dev_path = dev_path

    def _open(self) -> int:
        return os.open(self.dev_path, os.O_RDWR | os.O_NONBLOCK)

    def query(self, name: str) -> Optional[Control]:
        cid = CONTROLS.get(ALIASES.get(name, name))
        if cid is None:
            return None
        fd = self._open()
        try:
            buf = bytearray(struct.calcsize(_QUERYCTRL_FMT))
            struct.pack_into("<I", buf, 0, cid)
            try:
                fcntl.ioctl(fd, VIDIOC_QUERYCTRL, buf, True)
            except OSError:
                return None
            (_id, _type, raw_name, lo, hi, step, default, flags, _r0, _r1) = struct.unpack(
                _QUERYCTRL_FMT, bytes(buf)
            )
            if flags & FLAG_DISABLED:
                return None
            value = None
            try:
                cbuf = bytearray(struct.pack(_CONTROL_FMT, cid, 0))
                fcntl.ioctl(fd, VIDIOC_G_CTRL, cbuf, True)
                value = struct.unpack(_CONTROL_FMT, bytes(cbuf))[1]
            except OSError:
                pass
            return Control(
                name=raw_name.split(b"\x00")[0].decode("ascii", "replace"),
                id=cid, value=value, minimum=lo, maximum=hi,
                step=step, default=default, flags=flags,
            )
        finally:
            os.close(fd)

    def list(self) -> list[Control]:
        out = []
        for name in CONTROLS:
            c = self.query(name)
            if c is not None:
                out.append(c)
        return out

    def get(self, name: str) -> Optional[int]:
        c = self.query(name)
        return c.value if c else None

    def set(self, name: str, value: int) -> int:
        """Set a control, clamped to its advertised range. Returns the readback."""
        c = self.query(name)
        if c is None:
            raise ValueError(f"unknown or unavailable control {name!r}")
        if not c.writable:
            why = "read-only" if c.read_only else "not writable"
            raise PermissionError(
                f"{c.name} is {why} on this firmware. "
                "Focus and white balance need Studio mode — see `decomposer mode`."
            )
        value = max(c.minimum, min(c.maximum, value))
        fd = self._open()
        try:
            buf = bytearray(struct.pack(_CONTROL_FMT, c.id, value))
            fcntl.ioctl(fd, VIDIOC_S_CTRL, buf, True)
        except OSError as e:
            if e.errno == 32:  # EPIPE: the device stalled the request
                raise PermissionError(
                    f"{c.name} was refused by the camera. Exposure and gain are "
                    "owned by auto-exposure until it is switched to Manual Mode "
                    "(see set_manual_exposure)."
                ) from e
            raise
        finally:
            os.close(fd)
        return self.get(name)

    # Auto exposure menu on the C1: 0 = Auto Mode, 1 = Manual Mode.
    AUTO_EXPOSURE_AUTO = 0
    AUTO_EXPOSURE_MANUAL = 1

    def set_manual_exposure(
        self, exposure_us: Optional[int] = None, iso: Optional[int] = None
    ) -> dict:
        """Take exposure and gain off automatic, then apply them.

        The camera stalls writes to exposure_time_absolute and gain while
        auto-exposure owns them, so Manual Mode has to be engaged first.
        """
        self.set("auto_exposure", self.AUTO_EXPOSURE_MANUAL)
        out = {}
        if exposure_us is not None:
            out["exposure_time_absolute"] = self.set("exposure_time_absolute", exposure_us)
        if iso is not None:
            out["gain"] = self.set("gain", iso)
        return out

    def set_auto_exposure(self) -> int:
        """Hand exposure and gain back to the camera."""
        return self.set("auto_exposure", self.AUTO_EXPOSURE_AUTO)


# struct v4l2_capability: driver[16] card[32] bus_info[32] version caps device_caps reserved[3]
_QUERYCAP_FMT = "<16s32s32sIII3I"
VIDIOC_QUERYCAP = (2 << 30) | (struct.calcsize(_QUERYCAP_FMT) << 16) | (ord("V") << 8) | 0
V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_VIDEO_OUTPUT = 0x00000002


def exclusive_caps_ready(dev_path: str) -> bool:
    """True when a loopback node exposes only its currently usable direction.

    v4l2loopback's exclusive_caps mode advertises VIDEO_OUTPUT before a
    producer connects and VIDEO_CAPTURE after it connects.  A non-exclusive
    node advertises both at once.  Inspecting the node works for devices added
    dynamically as well as those created from module parameters.
    """
    try:
        fd = os.open(dev_path, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        buf = bytearray(struct.calcsize(_QUERYCAP_FMT))
        fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf, True)
        values = struct.unpack(_QUERYCAP_FMT, bytes(buf))
        caps = values[5] or values[4]
        directions = caps & (V4L2_CAP_VIDEO_CAPTURE | V4L2_CAP_VIDEO_OUTPUT)
        return directions in (V4L2_CAP_VIDEO_CAPTURE, V4L2_CAP_VIDEO_OUTPUT)
    except OSError:
        return False
    finally:
        os.close(fd)


def capture_ready(dev_path: str = "/dev/video0") -> bool:
    """True if the node exists *and* answers as a capture device.

    After a mode switch the node reappears roughly 1.5s before it can actually
    stream, so testing os.path.exists alone races: the engine opens the device,
    fails, and exits.
    """
    try:
        fd = os.open(dev_path, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        buf = bytearray(struct.calcsize(_QUERYCAP_FMT))
        fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf, True)
        caps = struct.unpack(_QUERYCAP_FMT, bytes(buf))[4]
        device_caps = struct.unpack(_QUERYCAP_FMT, bytes(buf))[5]
        return bool((device_caps or caps) & V4L2_CAP_VIDEO_CAPTURE)
    except OSError:
        return False
    finally:
        os.close(fd)
