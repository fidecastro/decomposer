"""Optional virtual webcam output via pyvirtualcam."""

from __future__ import annotations

from typing import Iterator

import numpy as np


def frames_to_virtual_cam(
    frames: Iterator[np.ndarray],
    width: int,
    height: int,
    fps: float = 30.0,
) -> None:
    """Write BGR frames to a virtual camera (RGB for pyvirtualcam).

    Linux: typically needs `v4l2loopback` loaded.
    Windows / macOS: uses platform backends from pyvirtualcam.
    """
    try:
        import pyvirtualcam
    except ImportError as e:
        raise SystemExit(
            "pyvirtualcam is not installed. Install with:\n"
            "  pip install 'decomposer[virtual]'\n"
            "On Linux also: sudo modprobe v4l2loopback"
        ) from e

    with pyvirtualcam.Camera(width=width, height=height, fps=fps, fmt=pyvirtualcam.PixelFormat.BGR) as cam:
        print(f"Virtual camera: {cam.device}")
        for frame in frames:
            if frame.shape[1] != width or frame.shape[0] != height:
                import cv2

                frame = cv2.resize(frame, (width, height))
            cam.send(frame)
            cam.sleep_until_next_frame()
