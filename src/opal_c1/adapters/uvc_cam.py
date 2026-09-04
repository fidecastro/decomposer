"""The Call-mode backend: Opal's firmware, controlled over UVC.

Frames never pass through here — the engine reads the V4L2 node itself, which
is why this backend implements CameraBackend but not FrameSource. attach and
release are deliberate no-ops: the kernel owns the device, and holding an
extra open would only add another reader to a camera that dislikes company.
"""

from __future__ import annotations

from typing import Callable, Mapping, Optional, Tuple

from opal_c1.core.model import Mode
from opal_c1.modes import camera_video_node
from opal_c1.v4l2 import UvcControls

# UVC control names for the daemon's control keys. exposure/iso are absent
# here on purpose: they must go through set_manual_exposure, which flips the
# camera to Manual Mode first — a bare write stalls with EPIPE — or, for a
# request of -1, through set_auto_exposure, which hands both back.
_SIMPLE = {
    "brightness": "brightness",
    "contrast": "contrast",
    "saturation": "saturation",
    "hue": "hue",
    "sharpness": "sharpness",
}
_READBACK = {
    "brightness": "brightness",
    "contrast": "contrast",
    "saturation": "saturation",
    "sharpness": "sharpness",
    "iso": "gain",
    "exposure": "exposure_time_absolute",
}


def _wants_auto(value) -> bool:
    """-1 (any negative) asks the camera to drive the control itself."""
    return value is not None and int(value) < 0


class UvcBackend:
    mode = Mode.CALL

    def __init__(
        self, node_resolver: Callable[[], Optional[str]] = camera_video_node
    ) -> None:
        self._resolve = node_resolver

    def attach(self) -> None:
        pass

    def release(self) -> None:
        pass

    def _controls(self) -> UvcControls:
        # Resolved per use: the node number changes across re-enumerations.
        return UvcControls(self._resolve() or "/dev/video0")

    def apply_controls(
        self, values: Mapping[str, object]
    ) -> Tuple[dict, dict]:
        applied: dict = {}
        refused: dict = {}
        uvc = self._controls()
        for key, name in _SIMPLE.items():
            if values.get(key) is None:
                continue
            try:
                applied[key] = uvc.set(name, int(values[key]))
            except (PermissionError, ValueError, OSError) as e:
                refused[key] = str(e)
        exposure, iso = values.get("exposure"), values.get("iso")
        if exposure is not None or iso is not None:
            try:
                if _wants_auto(exposure) or _wants_auto(iso):
                    # The ISP owns exposure and gain as a pair: -1 on
                    # either returns both to Auto Mode.
                    uvc.set_auto_exposure()
                    applied["exposure"] = -1
                    applied["iso"] = -1
                else:
                    got = uvc.set_manual_exposure(
                        None if exposure is None else int(exposure),
                        None if iso is None else int(iso),
                    )
                    if "exposure_time_absolute" in got:
                        applied["exposure"] = got["exposure_time_absolute"]
                    if "gain" in got:
                        applied["iso"] = got["gain"]
            except (PermissionError, ValueError, TypeError, OSError) as e:
                refused["exposure"] = str(e)
        return applied, refused

    def read_controls(self) -> dict:
        out: dict = {}
        try:
            uvc = self._controls()
            for key, name in _READBACK.items():
                control = uvc.query(name)
                if control is not None and control.value is not None:
                    out[key] = control.value
        except OSError:
            pass
        return out
