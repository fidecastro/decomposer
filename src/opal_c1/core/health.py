"""Supervision policy: what to do when the engine dies, as pure decisions.

The supervisor thread gathers facts (did the engine die, how long did it
live, is the camera on the bus, are frames advancing) and asks this module
what to do. The answers are values; the thread executes them. That split is
what lets every failure the camera has shown us be replayed as a unit test
instead of as an evening.

The patterns encoded here were all observed, not imagined:

- dies-young: in the degraded state, Opal's UVC firmware streams for ~11s,
  drops off the bus, reboots, and is back before the next check — so bus
  presence looks fine and only the engine's lifetime gives it away. Retrying
  fast churns the hardware with a reboot per attempt.
- vanished: the camera off the bus entirely. No retry can help; only saying
  so and waiting for it to come back (or be replugged) can.
- stall: everything alive, zero frames flowing — the pump reading None
  forever while the engine blocks on stdin. Invisible to a liveness check;
  only frame progress exposes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from opal_c1.core.model import Mode

# A Call restart only reopens a V4L2 node; a Studio restart reboots the
# camera's firmware and costs roughly twenty seconds of churn.
RETRY_FLOOR = {Mode.CALL: 3.0, Mode.STUDIO: 25.0}
MAX_BACKOFF = 120.0
GIVE_UP_AFTER = 3          # failed re-entries before Studio falls back to Call
SHORT_LIFE_SECONDS = 30.0  # an engine that dies younger than this is suspect
SHORT_LIVES_LIMIT = 4      # ...and this many in a row means the camera is sick
VANISHED_LIMIT = 3         # checks with the camera off the bus before holding
SICK_HOLD_SECONDS = 120.0
VANISHED_HOLD_SECONDS = 60.0
VANISHED_POLL_SECONDS = 5.0
STALL_SECONDS = 10.0       # no frame progress for this long, while running


class Kind(Enum):
    RETRY = "retry"                  # wait `delay`, then re-enter the mode
    HOLD_SICK = "hold_sick"          # camera dies young repeatedly: long hold
    HOLD_VANISHED = "hold_vanished"  # camera off the bus: hold, poll for return
    FALLBACK_TO_CALL = "fallback"    # studio keeps failing: rescue to call
    RECORD_FAILURE = "record"        # note it, keep the grown backoff


@dataclass(frozen=True)
class Action:
    kind: Kind
    delay: float = 0.0
    message: str = ""


SICK_MESSAGE = (
    "the camera streams for a few seconds and then drops off the bus, "
    "repeatedly. This is the C1's Opal firmware in its degraded state: power "
    "the camera off for 2-3 minutes to clear it. Studio mode usually still "
    "streams in this state (decomposer switch studio). Holding retries for "
    "2 minutes."
)
VANISHED_MESSAGE = (
    "the camera keeps dropping off the USB bus. This is the C1's firmware, "
    "not decomposer: unplug it for ~30s and plug it back into a direct USB 3 "
    "port. Retrying slowly."
)


class EnginePolicy:
    """Counters plus decisions. Feed it events; execute what it returns."""

    def __init__(self) -> None:
        self.backoff = 0.0
        self.failures = 0
        self.vanished = 0
        self.short_lives = 0

    def note_alive(self) -> None:
        """The engine is running: nothing is wrong right now."""
        self.backoff = 0.0
        self.failures = 0

    def on_death(self, mode: Mode, uptime: float, camera_on_bus: bool) -> Action:
        """The engine is dead (or never started). What now?"""
        if 0 < uptime < SHORT_LIFE_SECONDS:
            self.short_lives += 1
        elif uptime >= SHORT_LIFE_SECONDS:
            self.short_lives = 0
        if self.short_lives >= SHORT_LIVES_LIMIT:
            # Stay suspicious after the hold, but allow a probe attempt.
            self.short_lives = SHORT_LIVES_LIMIT - 2
            return Action(Kind.HOLD_SICK, SICK_HOLD_SECONDS, SICK_MESSAGE)

        if not camera_on_bus:
            self.vanished += 1
            if self.vanished >= VANISHED_LIMIT:
                return Action(
                    Kind.HOLD_VANISHED, VANISHED_HOLD_SECONDS, VANISHED_MESSAGE
                )
        else:
            self.vanished = 0

        return Action(Kind.RETRY, self.backoff or RETRY_FLOOR[mode])

    def on_reentry_ok(self) -> None:
        self.backoff = 0.0
        self.failures = 0

    def on_reentry_failed(self, mode: Mode, error: str) -> Action:
        self.failures += 1
        self.backoff = min(max(self.backoff, RETRY_FLOOR[mode]) * 2, MAX_BACKOFF)
        if self.failures >= GIVE_UP_AFTER and mode is Mode.STUDIO:
            self.failures = 0
            self.backoff = 0.0
            return Action(
                Kind.FALLBACK_TO_CALL,
                message=(
                    f"studio mode failed {GIVE_UP_AFTER} times ({error}); "
                    "fell back to call"
                ),
            )
        return Action(Kind.RECORD_FAILURE, message=error)


class StallDetector:
    """Frames stopped advancing while everything claims to be alive.

    Call `update` with the running frame counter; it answers whether the
    pipeline has been frozen for longer than the window. A stall is treated
    as an engine death so the ordinary recovery machinery applies.
    """

    def __init__(self, window: float = STALL_SECONDS) -> None:
        self.window = window
        self._last_count: Optional[int] = None
        self._last_change: Optional[float] = None

    def reset(self) -> None:
        self._last_count = None
        self._last_change = None

    def update(self, frames: int, now: float) -> bool:
        if self._last_count is None or frames != self._last_count:
            self._last_count = frames
            self._last_change = now
            return False
        return (now - self._last_change) >= self.window
