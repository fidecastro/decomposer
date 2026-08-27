"""The Studio-mode backend: stock DepthAI firmware, controlled over XLink.

Owns the OpalDevice lifecycle. attach boots the firmware (a real camera
reboot, ~5s), release drops it (~15s back to Call), and while attached this
backend is also the frame source — Studio mode has no V4L2 node, so the
daemon pumps frames from here into the engine's stdin.
"""

from __future__ import annotations

from typing import Mapping, Optional, Tuple

from opal_c1.core.model import Mode
from opal_c1.device import OpalDevice


class XLinkBackend:
    mode = Mode.STUDIO

    def __init__(
        self, width: int, height: int, fps: float, mask_model: bool = False
    ) -> None:
        self._mask_model = mask_model
        self._width = width
        self._height = height
        self._fps = fps
        self._dev: Optional[OpalDevice] = None
        self._last_frame = None

    def attach(self) -> None:
        self._dev = OpalDevice(
            width=self._width, height=self._height, fps=self._fps,
            mask_model=self._mask_model,
        ).open()

    def release(self) -> None:
        if self._dev is not None:
            try:
                self._dev.close()
            finally:
                self._dev = None
                self._last_frame = None

    # -- frames ----------------------------------------------------------

    def try_read_frame(self):
        if self._dev is None:
            return None
        frame = self._dev.try_read()
        if frame is not None:
            if (frame.width, frame.height) != (self._width, self._height):
                # depthai downgrades an undeliverable geometry SILENTLY
                # (a 5312x6000 NV12 request comes back 4000x3000: the ISP
                # cannot go higher). Streaming that into a consumer sized
                # for the request is how "the feed is garbage" happens - a
                # loud error here is the honest failure.
                raise RuntimeError(
                    f"camera delivered {frame.width}x{frame.height} for a "
                    f"{self._width}x{self._height} request; this geometry "
                    "is not deliverable over the ISP"
                )
            self._last_frame = frame
        return frame

    def try_read_mask(self):
        """Latest on-VPU person mask, or None (also when disabled)."""
        if self._dev is None:
            return None
        return self._dev.try_read_mask()

    # -- controls ---------------------------------------------------------

    def apply_controls(
        self, values: Mapping[str, object]
    ) -> Tuple[dict, dict]:
        dev = self._dev
        if dev is None:
            return {}, {k: "studio device is not attached" for k in values}
        applied: dict = {}
        refused: dict = {}

        def attempt(key, fn, *args):
            try:
                fn(*args)
                applied[key] = values[key]
            except Exception as e:  # depthai raises plain RuntimeErrors
                refused[key] = f"{type(e).__name__}: {e}"

        for key in ("af_region", "ae_region"):
            region = values.get(key)
            if region:
                x, y, w, h = (int(v) for v in region)
                setter = dev.set_af_region if key == "af_region" else dev.set_ae_region
                attempt(key, setter, x, y, w, h)
                if key in applied:
                    applied[key] = [x, y, w, h]
        if values.get("effect") is not None:
            attempt("effect", dev.set_effect, str(values["effect"]))
        if values.get("scene") is not None:
            attempt("scene", dev.set_scene, str(values["scene"]))
        if values.get("focus") is not None:
            v = int(values["focus"])
            attempt("focus", dev.set_focus, None if v < 0 else v)
            if "focus" in applied:
                applied["focus"] = v
        if values.get("wb") is not None:
            v = int(values["wb"])
            attempt("wb", dev.set_white_balance, None if v < 0 else v)
            if "wb" in applied:
                applied["wb"] = v
        if values.get("exposure") is not None or values.get("iso") is not None:
            try:
                dev.set_exposure(values.get("exposure"), values.get("iso"))
                if values.get("exposure") is not None:
                    applied["exposure"] = values["exposure"]
                if values.get("iso") is not None:
                    applied["iso"] = values["iso"]
            except Exception as e:
                refused["exposure"] = f"{type(e).__name__}: {e}"
        return applied, refused

    def read_controls(self) -> dict:
        frame = self._last_frame
        if frame is None:
            return {}
        out: dict = {}
        for key, value in (
            ("focus", frame.lens),
            ("iso", frame.iso),
            ("exposure", frame.exposure_us),
            ("wb", frame.color_temp),
        ):
            if value is not None:
                out[key] = value
        return out
