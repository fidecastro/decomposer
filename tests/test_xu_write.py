"""SET_CUR write-sweep helpers: packing, value plans, restore discipline."""

from __future__ import annotations

import struct

import pytest

from opal_c1 import probe as probe_mod
from opal_c1 import uvcxu


def test_query_still_rejects_set_cur():
    with pytest.raises(ValueError, match="set_cur"):
        uvcxu.query(fd=0, unit=4, selector=1, code=uvcxu.UVC_SET_CUR, size=1)


def test_set_cur_packs_uvc_request(monkeypatch):
    seen = {}

    def fake_ioctl(fd, req, arg):
        seen["fd"] = fd
        seen["req"] = req
        seen["arg"] = arg
        return 0

    monkeypatch.setattr(uvcxu.fcntl, "ioctl", fake_ioctl)
    uvcxu.set_cur(7, unit=4, selector=3, data=b"\x5c")

    unit, selector, code, size, ptr = struct.unpack(uvcxu._QUERY_STRUCT, seen["arg"])
    assert seen["fd"] == 7
    assert seen["req"] == uvcxu.UVCIOC_CTRL_QUERY
    assert (unit, selector, code, size) == (4, 3, uvcxu.UVC_SET_CUR, 1)
    assert ptr != 0


def test_set_cur_rejects_empty_payload():
    with pytest.raises(ValueError, match="non-empty"):
        uvcxu.set_cur(0, 4, 1, b"")


def test_candidate_values_skips_current_and_respects_range():
    p = uvcxu.ControlProbe(
        selector=1,
        length=1,
        info=uvcxu.INFO_GET | uvcxu.INFO_SET,
        values={
            "cur": b"\x5c",
            "min": b"\x00",
            "max": b"\xff",
            "res": b"\x01",
            "def": b"\x01",
        },
    )
    planned = probe_mod.candidate_values(p)
    ints = [int.from_bytes(b, "little") for b in planned]
    assert 0x5c not in ints
    assert 0 in ints
    assert 1 in ints
    assert 255 in ints
    assert all(0 <= v <= 255 for v in ints)


def test_candidate_values_honours_extras_inside_range():
    p = uvcxu.ControlProbe(
        selector=2,
        length=1,
        info=3,
        values={
            "cur": b"\x01",
            "min": b"\x00",
            "max": b"\x0a",
            "res": b"\x01",
            "def": b"\x01",
        },
    )
    planned = probe_mod.candidate_values(p, extra=[3, 99, 3])
    ints = [int.from_bytes(b, "little") for b in planned]
    assert 3 in ints
    assert 99 not in ints
    assert ints.count(3) == 1


def test_writable_requires_set_cap():
    get_only = uvcxu.ControlProbe(selector=1, length=1, info=uvcxu.INFO_GET)
    both = uvcxu.ControlProbe(
        selector=1, length=1, info=uvcxu.INFO_GET | uvcxu.INFO_SET
    )
    assert not get_only.writable
    assert both.writable


def test_write_sweep_dry_run_plans_without_opening_device(monkeypatch):
    probes = [
        uvcxu.ControlProbe(
            selector=1,
            length=1,
            info=3,
            values={
                "cur": b"\x10",
                "min": b"\x00",
                "max": b"\x20",
                "res": b"\x10",
                "def": b"\x10",
            },
        ),
        uvcxu.ControlProbe(selector=2, length=0, info=3),
    ]

    class FakeXU:
        unit_id = 4
        guid = "{ffffffff-ffff-ffff-ffff-ffffffffffff}"
        num_controls = 80

    class FakeUSB:
        def describe(self):
            return {"product": "Opal C1", "idVendor": "03e7", "idProduct": "f63d"}

    monkeypatch.setattr(
        probe_mod, "find_extension_units", lambda path: (FakeUSB(), [FakeXU()])
    )
    monkeypatch.setattr(probe_mod, "probe_unit", lambda *a, **k: probes)

    opened = []

    def boom(*a, **k):
        opened.append(True)
        raise AssertionError("dry-run must not open the video node")

    monkeypatch.setattr(probe_mod.os, "open", boom)

    report = probe_mod.run_write_sweep(
        "/dev/null", unit=4, selectors=range(1, 3), dry_run=True
    )
    assert opened == []
    assert all(s.readback is None and s.error is None for s in report.steps)
    assert report.steps
    # length-0 selectors are not writable targets; only get-only supported
    # controls appear in skipped.
    assert all(s.selector == 1 for s in report.steps)
    assert 2 not in {s.selector for s in report.steps}
