"""Ports: the interfaces the application depends on.

Hexagonal rule: the application (daemon/service) talks to these shapes and
never to depthai, ioctls or sysfs directly. The real adapters live in
opal_c1.adapters; the fakes live in tests. Both satisfy the same protocols,
which is what lets the camera's misbehavior be rehearsed without the camera.
"""

from __future__ import annotations

from typing import Mapping, Optional, Protocol, Tuple, runtime_checkable

from opal_c1.core.model import Mode


@runtime_checkable
class CameraBackend(Protocol):
    """One mode's control surface for the camera.

    `apply_controls` takes only values the routing table already allows for
    this backend's mode — mode-level refusals are the caller's job, from
    core.model.refusal_reason, so that policy lives in exactly one place.
    It returns (applied, refused): refused here means the *hardware* said no.
    """

    mode: Mode

    def attach(self) -> None: ...

    def release(self) -> None: ...

    def apply_controls(
        self, values: Mapping[str, object]
    ) -> Tuple[dict, dict]: ...

    def read_controls(self) -> dict: ...


@runtime_checkable
class FrameSource(Protocol):
    """A backend that also delivers frames to the daemon.

    Separate from CameraBackend (interface segregation): in Call mode the
    engine reads the V4L2 node itself and the daemon never touches pixels,
    so the Call backend simply does not have this method.
    """

    def try_read_frame(self) -> Optional[object]: ...
