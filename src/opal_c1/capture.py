"""Camera device discovery and frame capture (OpenCV / V4L2 / AVFoundation)."""

from __future__ import annotations

import sys
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class CameraInfo:
    index: int
    name: str
    backend: str


def _backend_candidates() -> list[int]:
    if sys.platform == "darwin":
        return [cv2.CAP_AVFOUNDATION, cv2.CAP_ANY]
    if sys.platform.startswith("linux"):
        return [cv2.CAP_V4L2, cv2.CAP_ANY]
    return [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]


def list_cameras(max_index: int = 8) -> list[CameraInfo]:
    found: list[CameraInfo] = []
    seen: set[int] = set()
    for backend in _backend_candidates():
        misses = 0
        for i in range(max_index):
            if i in seen:
                continue
            cap = cv2.VideoCapture(i, backend)
            if not cap.isOpened():
                cap.release()
                misses += 1
                if misses >= 2 and found:
                    break
                continue
            misses = 0
            name = f"camera{i}"
            # OpenCV does not always expose a friendly name; probe width as sanity.
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            if w > 0 and h > 0:
                name = f"camera{i} ({w}x{h})"
            found.append(CameraInfo(index=i, name=name, backend=str(backend)))
            seen.add(i)
            cap.release()
        if found:
            break
    return found


def open_camera(
    index: int = 0,
    width: int = 1280,
    height: int = 720,
    fps: float = 30.0,
) -> cv2.VideoCapture:
    last_err = None
    for backend in _backend_candidates():
        cap = cv2.VideoCapture(index, backend)
        if not cap.isOpened():
            cap.release()
            last_err = f"backend {backend} failed to open index {index}"
            continue
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
        ok, frame = cap.read()
        if ok and frame is not None:
            return cap
        cap.release()
        last_err = f"backend {backend} opened but read failed"
    raise RuntimeError(last_err or f"Could not open camera index {index}")


def read_frame(cap: cv2.VideoCapture) -> np.ndarray:
    ok, frame = cap.read()
    if not ok or frame is None:
        raise RuntimeError("Failed to read frame from camera")
    return frame


def grab_still(
    index: int = 0,
    width: int = 1280,
    height: int = 720,
    settle_frames: int = 8,
) -> np.ndarray:
    """Grab a single settled frame (useful for reference stills)."""
    cap = open_camera(index=index, width=width, height=height)
    try:
        frame = None
        for _ in range(settle_frames):
            frame = read_frame(cap)
        assert frame is not None
        return frame
    finally:
        cap.release()
