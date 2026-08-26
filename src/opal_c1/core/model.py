"""Domain model: the facts of the hardware, as data.

The C1 runs one of two firmwares and cannot run both. Everything else in the
system is a consequence of that fact, so it lives here, once, as data that the
rest of the code consults instead of re-deriving in scattered branches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# USB identities. These are domain facts, not configuration: f63d is Opal's
# firmware (UVC + microphone), f63b is stock DepthAI firmware (XLink only),
# f63c is the Luxonis bootloader the device falls back to between the two.
USB_VID = "03e7"
PID_CALL = "f63d"
PID_STUDIO = "f63b"
PID_BOOTLOADER = "f63c"


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

# Which control is reachable in which mode. This is the single routing table:
# anything that wants to know "can this mode do that" asks here, rather than
# keeping its own if-studio branch that can drift.
CALL_ONLY_CONTROLS = frozenset({
    "brightness", "contrast", "saturation", "hue", "sharpness",
})
SHARED_CONTROLS = frozenset({"exposure", "iso"})
STUDIO_ONLY_CONTROLS = frozenset({
    "focus", "wb", "af_region", "ae_region", "effect", "scene",
})


def controls_for(mode: Mode) -> frozenset:
    if mode is Mode.CALL:
        return CALL_ONLY_CONTROLS | SHARED_CONTROLS
    return STUDIO_ONLY_CONTROLS | SHARED_CONTROLS


# Controls whose requested values should be replayed after the firmware
# reboots. Every mode switch (and every Studio engine restart) boots a fresh
# firmware with default settings, so without replay the camera silently
# reverts while status keeps claiming the old values. Regions are excluded
# deliberately: a tap-to-focus was aimed at a moment, not a policy.
STICKY_CONTROLS = frozenset({
    "brightness", "contrast", "saturation", "hue", "sharpness",
    "exposure", "iso", "focus", "wb", "effect", "scene",
})


def sticky_for_mode(sticky: dict, mode: Mode) -> dict:
    """The subset of remembered requests that this mode can replay."""
    reachable = controls_for(mode)
    return {
        key: value
        for key, value in sticky.items()
        if key in STICKY_CONTROLS and key in reachable
    }


def merge_reported(live: dict, sticky: dict) -> dict:
    """Combine hardware readback with remembered intent for status.

    An explicit request for automatic (-1) outranks whatever value the ISP
    reports while it hunts, and effect/scene have no readback at all — the
    remembered request is the only truth available.
    """
    out = dict(live)
    for key in ("focus", "wb"):
        if sticky.get(key) == -1:
            out[key] = -1
    for key in ("effect", "scene"):
        if key in sticky:
            out[key] = sticky[key]
    return out


def refusal_reason(mode: Mode, control: str) -> Optional[str]:
    """Why `control` cannot be applied in `mode`, or None if it can."""
    if control in controls_for(mode):
        return None
    if control in STUDIO_ONLY_CONTROLS:
        return (
            f"{control} needs Studio mode; it is an XLink control the "
            "Call-mode firmware does not expose"
        )
    if control in CALL_ONLY_CONTROLS:
        return (
            f"{control} needs Call mode; Studio firmware has no UVC "
            "processing block for it"
        )
    return f"unknown control {control!r}"


@dataclass
class EngineConfig:
    """Everything the engine needs to know, in one place.

    This is the chokepoint that ends the argv-versus-control-socket split:
    spawn arguments and runtime commands are both derived from an instance of
    this, so a restarted engine always comes back with the full desired state
    rather than whichever subset happened to be on its command line.
    """

    input: str = "/dev/video0"          # a V4L2 node, or "-" for stdin
    output: str = "/dev/video10"
    width: int = 1920
    height: int = 1080
    look: str = "none"
    strength: float = 0.5
    flip: int = 0                        # bit 0 mirror-h, bit 1 mirror-v
    overlay: Optional[str] = None
    overlay_x: int = 0
    overlay_y: int = 0
    overlay_w: int = 0
    overlay_h: int = 0
    overlay_opacity: float = 1.0
    lut_dir: Optional[str] = None

    # Fields whose change cannot be applied over the control socket: the
    # engine has to be restarted for them. Everything else is a live update.
    RESTART_FIELDS = ("input", "output", "width", "height", "lut_dir")

    def needs_restart_from(self, other: "EngineConfig") -> bool:
        return any(
            getattr(self, f) != getattr(other, f) for f in self.RESTART_FIELDS
        )


@dataclass
class ControlsState:
    """Last known control values, plus which of them were explicit requests.

    `sticky` records what the user asked for (e.g. focus 150, effect sepia) so
    it can be replayed after the firmware reboots — Studio-mode settings die
    with the firmware on every exit or engine restart, and status must not
    keep claiming values the camera no longer holds.
    """

    live: dict = field(default_factory=dict)
    sticky: dict = field(default_factory=dict)
