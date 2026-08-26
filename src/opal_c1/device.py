"""Opal C1 capture and control over XLink (DepthAI).

The C1 is a Luxonis OAK-1 MAX underneath: an LCM48 / IMX582 module on a Myriad X
(RVC2). Its UVC path is what the kernel binds as /dev/video0, but the UVC
controls have their auto modes locked on, so manual focus and white balance are
unreachable there. Those live on XLink.

IMPORTANT: attaching XLink tears down the camera's UVC interfaces. /dev/video0
disappears while a connection is held and returns roughly 14 seconds after it is
released. The two paths cannot be used at once, so this module owns the camera
for as long as it is open.

Only depthai and numpy are needed here — no OpenCV.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterator, Optional

import depthai as dai
import numpy as np

# Lens position and ISO ranges the device accepts. These match what the UVC path
# advertises (focus_absolute 0-255, gain 100-1600), which is how we know both
# paths are thin wrappers over the same ISP.
LENS_MIN, LENS_MAX = 0, 255
ISO_MIN, ISO_MAX = 100, 1600
EXPOSURE_MIN_US, EXPOSURE_MAX_US = 1, 33_000
WB_MIN_K, WB_MAX_K = 1000, 12_000


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


@dataclass
class Frame:
    """One NV12 frame plus the ISP state that produced it."""

    data: np.ndarray  # flat uint8, width*height*3//2
    width: int
    height: int
    sequence: int
    lens: Optional[int]
    iso: Optional[int]
    exposure_us: Optional[int]
    color_temp: Optional[int]
    stride: int = 0

    def nv12(self):
        """Tightly packed NV12, as something writable to a binary stream.

        The device is free to pad each row out to a stride wider than the
        frame. At 1080p it does not, but the consumer expects width-packed
        NV12, so never assume it.

        In the common unpadded case this returns a memoryview over the frame
        rather than a copy: at 3 MB and 30 fps, a `.tobytes()` here costs about
        90 MB/s of pointless memcpy.
        """
        if self.stride in (0, self.width):
            return memoryview(self.data)
        rows = self.data[: self.stride * self.height]
        y = rows.reshape(self.height, self.stride)[:, : self.width]
        uv = self.data[self.stride * self.height :]
        uv = uv.reshape(self.height // 2, self.stride)[:, : self.width]
        return np.concatenate([y.ravel(), uv.ravel()]).tobytes()

    @property
    def y_plane(self) -> np.ndarray:
        return self.data[: self.width * self.height].reshape(self.height, self.width)

    @property
    def uv_plane(self) -> np.ndarray:
        return self.data[self.width * self.height :].reshape(
            self.height // 2, self.width
        )


def find_device() -> dai.DeviceInfo:
    devices = dai.Device.getAllAvailableDevices()
    if not devices:
        raise RuntimeError(
            "No DepthAI device found.\n"
            "  - Is the C1 plugged into a USB 3 port directly (it draws ~896 mA)?\n"
            "  - Are the udev rules installed? See packaging/60-opal-c1.rules.\n"
            "    Without them depthai logs 'Insufficient permissions' and reports 0 devices."
        )
    return devices[0]


class OpalDevice:
    """Owns the camera: streams frames and applies manual controls.

    Use as a context manager. While open, /dev/video0 will not exist.
    """

    def __init__(self, width: int = 1920, height: int = 1080, fps: float = 30.0):
        self.width = width
        self.height = height
        self.fps = fps
        self._device: Optional[dai.Device] = None
        self._pipeline: Optional[dai.Pipeline] = None
        self._queue = None
        self._control = None
        # Mirrors of the last commanded state, so partial updates (e.g. changing
        # ISO alone) can resend the full exposure pair the device expects.
        self._exposure_us = 20_000
        self._iso = 800

    def __enter__(self) -> "OpalDevice":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> "OpalDevice":
        info = find_device()
        self._device = dai.Device(info)
        self._pipeline = dai.Pipeline(self._device)
        cam = self._pipeline.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_A)
        # The C1's sensor is mounted upside down. Opal's own firmware corrects
        # for that, stock DepthAI firmware does not, so Studio mode would
        # otherwise deliver an image rotated 180 degrees from Call mode.
        # Measured, not assumed: correlating the two modes against each other
        # scores +0.80 for rotate-180 and -0.66 for identical.
        cam.setImageOrientation(dai.CameraImageOrientation.ROTATE_180_DEG)
        out = cam.requestOutput(
            (self.width, self.height), dai.ImgFrame.Type.NV12, fps=self.fps
        )
        self._queue = out.createOutputQueue(maxSize=4, blocking=False)
        self._control = cam.inputControl.createInputQueue()
        self._pipeline.start()
        return self

    def close(self) -> None:
        if self._pipeline is not None:
            with suppress(Exception):
                self._pipeline.stop()
        if self._device is not None:
            with suppress(Exception):
                self._device.close()
        self._pipeline = self._device = self._queue = self._control = None

    # -- device facts ----------------------------------------------------

    def describe(self) -> dict:
        d = self._device
        if d is None:
            raise RuntimeError("device is not open")
        features = d.getConnectedCameraFeatures()
        f = features[0] if features else None
        return {
            "device_id": d.getDeviceInfo().deviceId,
            "usb_speed": d.getUsbSpeed().name,
            "platform": d.getPlatformAsString(),
            "bootloader": str(d.getBootloaderVersion()),
            "sensor": getattr(f, "sensorName", None),
            "native": (getattr(f, "width", None), getattr(f, "height", None)),
            "has_autofocus": bool(getattr(f, "hasAutofocus", False)),
        }

    # -- frames ----------------------------------------------------------

    def read(self) -> Frame:
        img = self._queue.get()
        return Frame(
            data=img.getData(),
            width=img.getWidth(),
            height=img.getHeight(),
            sequence=img.getSequenceNum(),
            lens=img.getLensPosition(),
            iso=img.getSensitivity(),
            exposure_us=(
                int(img.getExposureTime().total_seconds() * 1e6)
                if img.getExposureTime() is not None
                else None
            ),
            color_temp=img.getColorTemperature(),
            stride=img.getStride(),
        )

    def frames(self) -> Iterator[Frame]:
        while self._queue is not None:
            yield self.read()

    # -- controls --------------------------------------------------------

    def _send(self, ctrl: dai.CameraControl) -> None:
        if self._control is None:
            raise RuntimeError("device is not open")
        self._control.send(ctrl)

    def set_focus(self, position: Optional[int]) -> None:
        """Manual lens position 0-255, or None for continuous autofocus."""
        c = dai.CameraControl()
        if position is None:
            c.setAutoFocusMode(dai.CameraControl.AutoFocusMode.CONTINUOUS_VIDEO)
            c.setAutoFocusTrigger()
        else:
            c.setAutoFocusMode(dai.CameraControl.AutoFocusMode.OFF)
            c.setManualFocus(_clamp(position, LENS_MIN, LENS_MAX))
        self._send(c)

    def set_white_balance(self, kelvin: Optional[int]) -> None:
        """Manual white balance in kelvin (1000-12000), or None for auto."""
        c = dai.CameraControl()
        if kelvin is None:
            c.setAutoWhiteBalanceMode(dai.CameraControl.AutoWhiteBalanceMode.AUTO)
        else:
            c.setAutoWhiteBalanceMode(dai.CameraControl.AutoWhiteBalanceMode.OFF)
            c.setManualWhiteBalance(_clamp(kelvin, WB_MIN_K, WB_MAX_K))
        self._send(c)

    def set_exposure(
        self, exposure_us: Optional[int] = None, iso: Optional[int] = None
    ) -> None:
        """Manual exposure and ISO. Pass both None for auto exposure.

        The device takes exposure and ISO as a pair, so changing one resends the
        other from the last commanded value.
        """
        c = dai.CameraControl()
        if exposure_us is None and iso is None:
            c.setAutoExposureEnable()
        else:
            if exposure_us is not None:
                self._exposure_us = _clamp(exposure_us, EXPOSURE_MIN_US, EXPOSURE_MAX_US)
            if iso is not None:
                self._iso = _clamp(iso, ISO_MIN, ISO_MAX)
            c.setManualExposure(timedelta(microseconds=self._exposure_us), self._iso)
        self._send(c)

    def set_auto(self) -> None:
        """Return focus, white balance and exposure to automatic."""
        c = dai.CameraControl()
        c.setAutoFocusMode(dai.CameraControl.AutoFocusMode.CONTINUOUS_VIDEO)
        c.setAutoFocusTrigger()
        c.setAutoWhiteBalanceMode(dai.CameraControl.AutoWhiteBalanceMode.AUTO)
        c.setAutoExposureEnable()
        self._send(c)
