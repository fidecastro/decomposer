"""The two camera backends' handling of -1: hand the control back."""

from opal_c1.adapters import depthai_cam, uvc_cam


class _FakeUvc:
    """Records which of the two exposure paths a request takes."""

    calls: list = []

    def __init__(self, _node):
        pass

    def set(self, name, value):
        _FakeUvc.calls.append(("set", name, value))
        return value

    def set_manual_exposure(self, exposure_us=None, iso=None):
        _FakeUvc.calls.append(("manual", exposure_us, iso))
        out = {}
        if exposure_us is not None:
            out["exposure_time_absolute"] = exposure_us
        if iso is not None:
            out["gain"] = iso
        return out

    def set_auto_exposure(self):
        _FakeUvc.calls.append(("auto",))
        return 0


def _uvc(monkeypatch):
    _FakeUvc.calls = []
    monkeypatch.setattr(uvc_cam, "UvcControls", _FakeUvc)
    return uvc_cam.UvcBackend(node_resolver=lambda: "/dev/video0")


def test_uvc_negative_exposure_returns_the_pair_to_auto(monkeypatch):
    backend = _uvc(monkeypatch)
    applied, refused = backend.apply_controls({"exposure": -1})
    assert _FakeUvc.calls == [("auto",)]
    assert applied == {"exposure": -1, "iso": -1}
    assert refused == {}


def test_uvc_numeric_exposure_engages_manual_mode(monkeypatch):
    backend = _uvc(monkeypatch)
    applied, _ = backend.apply_controls({"exposure": 5000, "brightness": 120})
    assert ("manual", 5000, None) in _FakeUvc.calls
    assert ("auto",) not in _FakeUvc.calls
    assert applied == {"brightness": 120, "exposure": 5000}


class _FakeDev:
    def __init__(self):
        self.exposure_calls = []

    def set_exposure(self, exposure_us=None, iso=None):
        self.exposure_calls.append((exposure_us, iso))

    def set_focus(self, position):
        self.focus = position


def _xlink():
    backend = depthai_cam.XLinkBackend(width=1920, height=1080, fps=30.0)
    backend._dev = _FakeDev()
    return backend


def test_xlink_negative_exposure_hands_both_back():
    backend = _xlink()
    applied, refused = backend.apply_controls({"iso": -1})
    assert backend._dev.exposure_calls == [(None, None)]
    assert applied == {"exposure": -1, "iso": -1}
    assert refused == {}


def test_xlink_numeric_exposure_and_auto_focus_are_unchanged():
    backend = _xlink()
    applied, _ = backend.apply_controls({"exposure": 8000, "iso": 400, "focus": -1})
    assert backend._dev.exposure_calls == [(8000, 400)]
    assert backend._dev.focus is None
    assert applied == {"exposure": 8000, "iso": 400, "focus": -1}
