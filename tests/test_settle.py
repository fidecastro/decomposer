"""The Call firmware crashes if asked to stream too soon after enumerating.

Measured on fw 4.10: stream opened under ~6 s after the device appears and
the firmware drops off the bus a second or two later. These tests pin the
rule (core) and the one place that enforces it (modes.wait_until_capturable).
"""

from opal_c1 import modes
from opal_c1.core.model import CALL_STREAM_SETTLE_SECONDS, call_settle_remaining


def test_settle_time_covers_the_measured_threshold():
    # Deaths were observed at 3 s and 5 s, survival from 6 s.
    assert CALL_STREAM_SETTLE_SECONDS >= 7.0


def test_remaining_counts_down_and_never_goes_negative():
    assert call_settle_remaining(0.0) == CALL_STREAM_SETTLE_SECONDS
    assert call_settle_remaining(3.0) == CALL_STREAM_SETTLE_SECONDS - 3.0
    assert call_settle_remaining(CALL_STREAM_SETTLE_SECONDS) == 0.0
    assert call_settle_remaining(4000.0) == 0.0


def test_unknown_age_waits_the_full_settle():
    assert call_settle_remaining(None) == CALL_STREAM_SETTLE_SECONDS


class FakeBus:
    """A camera that enumerated `age` seconds before the clock started."""

    def __init__(self, monkeypatch, age, node_after=0.0, readable=True):
        self.now = 1000.0
        self.age = age
        self.node_after = node_after
        self.readable = readable
        monkeypatch.setattr(modes.time, "time", lambda: self.now)
        monkeypatch.setattr(modes.time, "sleep", self.sleep)
        monkeypatch.setattr(modes, "camera_video_node", self.node)
        monkeypatch.setattr(modes, "seconds_since_enumeration", self.since)

    def sleep(self, seconds):
        self.now += seconds

    def node(self):
        return "/dev/video0" if self.now - 1000.0 >= self.node_after else None

    def since(self):
        return self.age + (self.now - 1000.0) if self.readable else None


def test_a_long_enumerated_camera_is_not_delayed(monkeypatch):
    FakeBus(monkeypatch, age=600.0)
    assert modes.wait_until_capturable(timeout=10) == 0.0


def test_a_freshly_enumerated_camera_is_held_until_settled(monkeypatch):
    bus = FakeBus(monkeypatch, age=1.0)
    waited = modes.wait_until_capturable(timeout=10)
    assert waited is not None
    assert bus.since() >= CALL_STREAM_SETTLE_SECONDS
    assert waited <= CALL_STREAM_SETTLE_SECONDS


def test_settle_is_not_charged_to_the_timeout(monkeypatch):
    # The node takes 9 s of a 10 s budget; the settle wait must still happen.
    bus = FakeBus(monkeypatch, age=-9.0, node_after=9.0)
    assert modes.wait_until_capturable(timeout=10) is not None
    assert bus.since() >= CALL_STREAM_SETTLE_SECONDS


def test_unreadable_age_waits_once_and_returns(monkeypatch):
    bus = FakeBus(monkeypatch, age=0.0, readable=False)
    waited = modes.wait_until_capturable(timeout=10)
    assert CALL_STREAM_SETTLE_SECONDS <= waited <= CALL_STREAM_SETTLE_SECONDS + 1.5


def test_no_node_still_times_out(monkeypatch):
    FakeBus(monkeypatch, age=600.0, node_after=1e9)
    assert modes.wait_until_capturable(timeout=3) is None
