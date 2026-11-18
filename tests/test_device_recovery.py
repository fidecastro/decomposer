"""Explicit USB 2 recovery waits for stable enumeration before a RAM boot."""

from types import SimpleNamespace

import pytest

from opal_c1 import device


def _enumeration(monkeypatch, disappear_at=None):
    clock = [0.0]
    info = SimpleNamespace(
        state=SimpleNamespace(name="X_LINK_BOOTLOADER"),
        status=SimpleNamespace(name="X_LINK_SUCCESS"),
        getDeviceId=lambda: "test-c1",
    )

    def available():
        return [] if clock[0] == disappear_at else [info]

    monkeypatch.setattr(device.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(device.time, "sleep", lambda n: clock.__setitem__(0, clock[0] + n))
    monkeypatch.setattr(device.dai.Device, "getAllAvailableDevices", available)
    return info, clock


def test_recovery_waits_three_seconds_for_the_same_enumeration(monkeypatch):
    info, clock = _enumeration(monkeypatch)
    assert device.find_device(allow_bootloader=True) is info
    assert clock[0] == 3.0


def test_a_disconnect_restarts_the_stability_wait(monkeypatch):
    info, clock = _enumeration(monkeypatch, disappear_at=2.0)
    assert device.find_device(allow_bootloader=True) is info
    assert clock[0] == 6.0


def test_normal_mode_never_attaches_to_the_bootloader(monkeypatch):
    _enumeration(monkeypatch)
    with pytest.raises(RuntimeError, match="no attachable DepthAI device"):
        device.find_device(deadline_s=4)
