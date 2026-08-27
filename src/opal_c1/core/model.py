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
    # Capture size; 0 means same as the output. Capturing 4K while publishing
    # 1080p is what makes zoom lossless up to 2x.
    in_width: int = 0
    in_height: int = 0
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
    zoom: float = 1.0
    pan_x: float = 0.0
    pan_y: float = 0.0
    clahe: float = 0.0
    # Background effect: blur strength (0 = off) and replacement image.
    blur: float = 0.0
    background: Optional[str] = None
    # Person segmentation: model file and device. The ONNX session is built
    # at engine startup, so changing either restarts the engine; None means
    # the engine's own default (the vendored MediaPipe model, on cpu).
    seg_model: Optional[str] = None
    seg_device: Optional[str] = None
    # The user's model chain: ((path, device), ...) plus one strength per
    # entry. Membership and devices are session facts (restart); strengths
    # are live protocol lines.
    models: tuple = ()
    model_strengths: tuple = ()

    # Fields whose change cannot be applied over the control socket: the
    # engine has to be restarted for them. Everything else is a live update.
    RESTART_FIELDS = (
        "input", "output", "width", "height", "in_width", "in_height",
        "lut_dir", "seg_model", "seg_device", "models",
    )

    def needs_restart_from(self, other: "EngineConfig") -> bool:
        return any(
            getattr(self, f) != getattr(other, f) for f in self.RESTART_FIELDS
        )


def engine_cli_args(config: EngineConfig) -> list:
    """The engine's command line, derived from the config and nowhere else.

    Socket paths are the adapter's business; everything the engine needs to
    *reproduce a session* comes from here, which is why a restarted engine
    cannot come back missing settings: its argv and the runtime protocol are
    two projections of the same struct.
    """
    args = [
        "--input", config.input,
        "--output", config.output,
        "--width", str(config.width),
        "--height", str(config.height),
        "--look", config.look,
        "--strength", str(config.strength),
        "--flip", str(config.flip),
        "--overlay-rect",
        f"{config.overlay_x},{config.overlay_y},"
        f"{config.overlay_w},{config.overlay_h}",
        "--overlay-opacity", str(config.overlay_opacity),
    ]
    if config.in_width and config.in_height:
        args += ["--in-width", str(config.in_width),
                 "--in-height", str(config.in_height)]
    if config.zoom != 1.0:
        args += ["--zoom", str(config.zoom)]
    if config.clahe > 0.0:
        args += ["--clahe", str(config.clahe)]
    if config.pan_x or config.pan_y:
        args += ["--pan-x", str(config.pan_x), "--pan-y", str(config.pan_y)]
    if config.blur > 0.0:
        args += ["--blur", str(config.blur)]
    if config.background:
        args += ["--background", config.background]
    if config.seg_model:
        args += ["--seg-model", config.seg_model]
    if config.seg_device:
        args += ["--seg-device", config.seg_device]
    for i, (path, device) in enumerate(config.models):
        strength = (
            config.model_strengths[i]
            if i < len(config.model_strengths) else 1.0
        )
        args += ["--model", f"{path}:{device}:{strength}"]
    if config.lut_dir:
        args += ["--lut-dir", config.lut_dir]
    if config.overlay:
        args += ["--overlay", config.overlay]
    return args


def engine_delta_lines(old: EngineConfig, new: EngineConfig) -> list:
    """Control-socket lines that turn a running engine's `old` into `new`.

    The only place protocol strings are composed. Restart-only differences
    are not expressible as lines — needs_restart_from decides that first.
    """
    lines = []
    if new.look != old.look:
        lines.append(f"look {new.look}")
    if new.strength != old.strength:
        lines.append(f"strength {new.strength}")
    if new.flip != old.flip:
        lines.append(f"flip {new.flip}")
    rect = (new.overlay_x, new.overlay_y, new.overlay_w, new.overlay_h)
    if rect != (old.overlay_x, old.overlay_y, old.overlay_w, old.overlay_h):
        lines.append("overlay-rect {} {} {} {}".format(*rect))
    if new.overlay_opacity != old.overlay_opacity:
        lines.append(f"overlay-opacity {new.overlay_opacity}")
    if new.overlay != old.overlay:
        lines.append(f"overlay {new.overlay or 'off'}")
    if new.zoom != old.zoom:
        lines.append(f"zoom {new.zoom}")
    if (new.pan_x, new.pan_y) != (old.pan_x, old.pan_y):
        lines.append(f"pan {new.pan_x} {new.pan_y}")
    if new.clahe != old.clahe:
        lines.append(f"clahe {new.clahe}")
    if new.blur != old.blur:
        lines.append(f"blur {new.blur}")
    if new.background != old.background:
        lines.append(f"background {new.background or 'off'}")
    if len(new.model_strengths) == len(old.model_strengths):
        for i, (a, b) in enumerate(zip(old.model_strengths, new.model_strengths)):
            if a != b:
                lines.append(f"model-strength {i} {b}")
    return lines


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
