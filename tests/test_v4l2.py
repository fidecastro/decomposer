"""Small V4L2 capability helpers used by setup diagnostics."""

import struct

import pytest

from opal_c1 import v4l2


@pytest.mark.parametrize(
    ("directions", "expected"),
    [
        (v4l2.V4L2_CAP_VIDEO_CAPTURE, True),
        (v4l2.V4L2_CAP_VIDEO_OUTPUT, True),
        (v4l2.V4L2_CAP_VIDEO_CAPTURE | v4l2.V4L2_CAP_VIDEO_OUTPUT, False),
    ],
)
def test_exclusive_caps_checks_the_device_not_module_parameters(
    monkeypatch, directions, expected
):
    monkeypatch.setattr(v4l2.os, "open", lambda *_args: 7)
    monkeypatch.setattr(v4l2.os, "close", lambda _fd: None)

    def fake_ioctl(_fd, request, buf, mutate):
        assert request == v4l2.VIDIOC_QUERYCAP
        assert mutate is True
        buf[:] = struct.pack(
            v4l2._QUERYCAP_FMT,
            b"v4l2 loopback", b"decomposer", b"platform", 1,
            0x80000000, directions, 0, 0, 0,
        )

    monkeypatch.setattr(v4l2.fcntl, "ioctl", fake_ioctl)
    assert v4l2.exclusive_caps_ready("/dev/video11") is expected
