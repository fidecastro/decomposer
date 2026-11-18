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
    CALL_STREAM_SETTLE_SECONDS,
    CAPABILITIES,
    Capabilities,
    Mode,
    PID_CALL,
    call_settle_remaining,
    mode_for_identity,
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
        bcd = (entry / "bcdDevice").read_text().strip()
    except OSError:
        return None
    return mode_for_identity(pid, bcd)


def on_flash_firmware() -> bool:
    """True while the camera is on its own stored firmware (not RAM-booted)."""
    entry = find_camera()
    if entry is None:
        return False
    try:
        return (entry / "idProduct").read_text().strip() == PID_CALL
    except OSError:
        return False


def wait_for_mode(mode: Mode, timeout: float = 40.0) -> Optional[float]:
    """Block until the camera is in `mode`. Returns seconds waited, or None."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if current_mode() is mode:
            return round(time.time() - t0, 1)
        time.sleep(0.2)
    return None


def seconds_since_enumeration() -> Optional[float]:
    """How long the camera has been on the bus in its current personality.

    Read from the kernel's own counter for the USB device, which restarts at
    every enumeration. None if the camera is absent or the counter is not
    readable.
    """
    entry = find_camera()
    if entry is None:
        return None
    try:
        return int((entry / "power/connected_duration").read_text()) / 1000.0
    except (OSError, ValueError):
        return None


def wait_until_capturable(timeout: float = 40.0) -> Optional[float]:
    """Call mode only: wait until the camera can be asked to stream.

    Two conditions. The capture node must answer VIDIOC_QUERYCAP as a capture
    device: it reappears a little after the device re-enumerates, and a bare
    path test passes too early. And the firmware must have been up for
    CALL_STREAM_SETTLE_SECONDS: asked to stream sooner, it crashes (see
    core/model.py). The settle wait is not charged to `timeout`; it is
    bounded by the settle time itself.
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        if camera_video_node() is not None:
            break
        time.sleep(0.2)
    else:
        return None
    settle_deadline = time.time() + CALL_STREAM_SETTLE_SECONDS + 1.0
    while time.time() < settle_deadline:
        remaining = call_settle_remaining(seconds_since_enumeration())
        if remaining <= 0:
            break
        time.sleep(min(remaining, 0.2))
    return round(time.time() - t0, 1)


def describe(mode: Mode) -> str:
    c = CAPABILITIES[mode]
    tick = lambda b: "yes" if b else "no "
    return (
        f"{mode.value:<7} (pid {mode.pid})  "
        f"mic {tick(c.microphone)}  /dev/video0 {tick(c.video_node)}  "
        f"manual focus {tick(c.manual_focus)}  manual WB {tick(c.manual_white_balance)}"
    )
