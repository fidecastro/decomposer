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

import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

USB_VID = "03e7"
PID_CALL = "f63d"
PID_STUDIO = "f63b"

SYSFS_USB = Path("/sys/bus/usb/devices")


class Mode(Enum):
    CALL = "call"
    STUDIO = "studio"

    @property
    def pid(self) -> str:
        return PID_CALL if self is Mode.CALL else PID_STUDIO


@dataclass(frozen=True)
class Capabilities:
    microphone: bool
    video_node: bool
    manual_focus: bool
    manual_white_balance: bool
    manual_exposure: bool
    looks: bool = True


CAPABILITIES = {
    Mode.CALL: Capabilities(
        microphone=True, video_node=True,
        manual_focus=False, manual_white_balance=False, manual_exposure=True,
    ),
    Mode.STUDIO: Capabilities(
        microphone=False, video_node=False,
        manual_focus=True, manual_white_balance=True, manual_exposure=True,
    ),
}

# Controls that exist only in Studio mode, so the CLI can route sensibly.
STUDIO_ONLY = ("focus", "white_balance")


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
    """Call mode only: wait for /dev/video0 to exist again after a switch.

    The node reappears a little after the device re-enumerates, so callers that
    want to capture immediately should wait on this rather than on the mode.
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists("/dev/video0"):
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
