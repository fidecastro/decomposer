"""Call mode and Studio mode.

The C1 runs one of two firmwares and cannot run both. Which one is loaded
decides what the camera can do, and the difference is not a detail we can hide:

  Call mode   (USB pid f63d, Opal's firmware)
      /dev/video0 + the UAC2 microphone + the vendor bulk interface (inert).
      Exposure, gain, brightness, contrast, saturation, hue, sharpness and
      backlight compensation are all adjustable over UVC. Focus and white
      balance are locked to automatic: the device reports those auto toggles
      as read-only, so the manual values stay permanently inactive.

  Studio mode (USB pid f63b, stock DepthAI firmware)
      Manual focus and manual white balance, plus everything else, driven over
      XLink. There is no /dev/video0 and no microphone: depthai has no audio
      support at all, so the mic array simply does not exist in this mode.

Switching means the device reboots and re-enumerates, which costs roughly 5 s
into Studio mode and 15 s back. Entering Studio mode is a side effect of
holding a depthai connection; leaving it is a side effect of releasing one.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

# The domain facts live in the pure core; this module is the sysfs adapter
# that observes them on the actual bus. Names are re-exported so existing
# imports keep working.
from opal_c1.core.model import (  # noqa: F401
    CAPABILITIES,
    Capabilities,
    Mode,
    PID_CALL,
    PID_STUDIO,
    USB_VID,
)

SYSFS_USB = Path("/sys/bus/usb/devices")


def find_camera() -> Optional[Path]:
    """Locate the C1 in sysfs, in whichever mode it is currently in."""
    if not SYSFS_USB.is_dir():
        return None
    for entry in SYSFS_USB.iterdir():
        vid = entry / "idVendor"
        if not vid.is_file():
            continue
        try:
            if vid.read_text().strip() != USB_VID:
                continue
            pid = (entry / "idProduct").read_text().strip()
        except OSError:
            continue
        if pid in (PID_CALL, PID_STUDIO):
            return entry
    return None


def camera_video_node() -> Optional[str]:
    """The C1's V4L2 capture node, discovered rather than assumed.

    Node numbers are not stable: after enough re-enumerations the camera can
    come back as /dev/video2 while other numbers stay taken, and everything
    that hardcoded /dev/video0 starts failing with ENOENT.
    """
    entry = find_camera()
    if entry is None:
        return None
    from opal_c1.v4l2 import capture_ready

    numbers = []
    for vd in entry.glob("*/video4linux/video*"):
        try:
            numbers.append(int(vd.name[5:]))
        except ValueError:
            continue
    for n in sorted(numbers):
        path = f"/dev/video{n}"
        if capture_ready(path):
            return path
    return None


def current_mode() -> Optional[Mode]:
    """Which mode the camera is in, or None if it is not on the bus.

    None is normal and transient: the device leaves the bus entirely for
    several seconds during a switch.
    """
    entry = find_camera()
    if entry is None:
        return None
    try:
        pid = (entry / "idProduct").read_text().strip()
    except OSError:
        return None
    return Mode.CALL if pid == PID_CALL else Mode.STUDIO


def wait_for_mode(mode: Mode, timeout: float = 40.0) -> Optional[float]:
    """Block until the camera is in `mode`. Returns seconds waited, or None."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if current_mode() is mode:
            return round(time.time() - t0, 1)
        time.sleep(0.2)
    return None


def wait_until_capturable(timeout: float = 40.0) -> Optional[float]:
    """Call mode only: wait until /dev/video0 can actually capture again.

    The node reappears a little after the device re-enumerates, and can be
    opened before it will stream, so this checks it answers VIDIOC_QUERYCAP as
    a capture device rather than just testing for the path.
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        if camera_video_node() is not None:
            return round(time.time() - t0, 1)
        time.sleep(0.2)
    return None


def describe(mode: Mode) -> str:
    c = CAPABILITIES[mode]
    tick = lambda b: "yes" if b else "no "
    return (
        f"{mode.value:<7} (pid {mode.pid})  "
        f"mic {tick(c.microphone)}  /dev/video0 {tick(c.video_node)}  "
        f"manual focus {tick(c.manual_focus)}  manual WB {tick(c.manual_white_balance)}"
    )
