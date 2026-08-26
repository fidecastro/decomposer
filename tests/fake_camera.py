"""A camera that misbehaves on command.

Satisfies the same ports as the real backends, so anything written against
ports.CameraBackend / ports.FrameSource can be exercised against a camera
that dies young, refuses controls, or goes silent — without costing the real
one another firmware reboot.
"""

from __future__ import annotations

from typing import Mapping, Optional, Tuple

from opal_c1.core.model import Mode


class FakeFrame:
    def __init__(self, sequence: int):
        self.sequence = sequence
        self.lens = 140
        self.iso = 400
        self.exposure_us = 20000
        self.color_temp = 4500

    def nv12(self) -> bytes:
        return b"\x80" * 64


class FakeCamera:
    """Scriptable backend: for Studio-shaped tests set mode/frames as needed."""

    def __init__(
        self,
        mode: Mode = Mode.STUDIO,
        frames_before_silence: Optional[int] = None,
        refuse: Optional[dict] = None,
        fail_attach: bool = False,
    ):
        self.mode = mode
        self.attached = False
        self.applied_log: list = []
        self._refuse = refuse or {}
        self._fail_attach = fail_attach
        self._frames_left = frames_before_silence
        self._sequence = 0
        self._controls: dict = {}

    # -- CameraBackend ----------------------------------------------------

    def attach(self) -> None:
        if self._fail_attach:
            raise RuntimeError("fake attach failure")
        self.attached = True

    def release(self) -> None:
        self.attached = False

    def apply_controls(
        self, values: Mapping[str, object]
    ) -> Tuple[dict, dict]:
        applied, refused = {}, {}
        for key, value in values.items():
            if key in self._refuse:
                refused[key] = self._refuse[key]
            else:
                applied[key] = value
                self._controls[key] = value
        self.applied_log.append(dict(applied))
        return applied, refused

    def read_controls(self) -> dict:
        return dict(self._controls)

    # -- FrameSource ------------------------------------------------------

    def try_read_frame(self) -> Optional[FakeFrame]:
        if not self.attached:
            return None
        if self._frames_left is not None:
            if self._frames_left <= 0:
                return None  # the camera has gone silent: a stall, not an exit
            self._frames_left -= 1
        self._sequence += 1
        return FakeFrame(self._sequence)
