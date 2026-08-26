"""The mode state machine: who may switch, when, and onto what.

Two decisions live here, both pure:

- `evaluate_switch`: whether a requested mode change may proceed right now.
  Every switch is a firmware reboot, and rapid cycling is what drives the
  camera into its degraded state, so switches are rate-limited and mutually
  exclusive by design rather than by luck.

- `choose_device`: which enumerated DepthAI device, if any, is safe to attach
  to. Attaching to the bootloader — which is what naively taking devices[0]
  does when a switch lands mid-reboot — wedges the session with
  "Couldn't read data from stream: `_bootloader` (X_LINK_ERROR)".

Time is a parameter (monotonic seconds); device facts are plain strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence, Tuple

from opal_c1.core.model import Mode

# A switch reboots the camera's firmware. Two in quick succession means the
# second lands on a device that is still settling from the first, and heavy
# churn is what degrades Opal's UVC firmware. Twenty seconds comfortably
# covers the longest observed settle (~15s back to Call).
MIN_SECONDS_BETWEEN_SWITCHES = 20.0


@dataclass
class Ledger:
    """What the transition machinery knows about itself."""

    current: Mode
    last_switch_at: Optional[float] = None  # monotonic seconds
    in_progress: bool = False


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


def evaluate_switch(ledger: Ledger, want: Mode, now: float) -> Decision:
    """May a client-requested transition to `want` proceed at `now`?

    Same-mode requests are restarts, not switches: they do not reboot into a
    different firmware, and their pacing is the health policy's job, so they
    pass. The supervisor's recovery re-entries bypass this function entirely —
    rescue must not be rate-limited by the thing it is rescuing from.
    """
    if ledger.in_progress:
        return Decision(False, "a mode transition is already in progress")
    if want is ledger.current:
        return Decision(True)
    if ledger.last_switch_at is not None:
        elapsed = now - ledger.last_switch_at
        if elapsed < MIN_SECONDS_BETWEEN_SWITCHES:
            remaining = MIN_SECONDS_BETWEEN_SWITCHES - elapsed
            return Decision(
                False,
                f"switched modes {elapsed:.0f}s ago; every switch reboots the "
                f"camera's firmware and rapid cycling degrades it. "
                f"Try again in {remaining:.0f}s.",
            )
    return Decision(True)


class AttachAction(Enum):
    ATTACH = "attach"
    WAIT = "wait"
    NONE = "none"


@dataclass(frozen=True)
class AttachDecision:
    action: AttachAction
    index: int = -1
    reason: str = ""


# States a device can safely be attached (and booted) from. NON_EXCLUSIVE is
# an enum alias of FLASH_BOOTED in depthai, so either name may surface.
_ATTACHABLE = {
    "X_LINK_FLASH_BOOTED",
    "X_LINK_BOOTED_NON_EXCLUSIVE",
    "X_LINK_UNBOOTED",
}
# Transient states: the device is between firmwares. Attaching now is the
# _bootloader wedge; the right move is to wait for it to finish.
_TRANSIENT = {"X_LINK_BOOTLOADER", "X_LINK_BOOTED"}


def choose_device(devices: Sequence[Tuple[str, str]]) -> AttachDecision:
    """Pick a device to attach to from (state_name, status_name) pairs."""
    for i, (state, status) in enumerate(devices):
        if status == "X_LINK_SUCCESS" and state in _ATTACHABLE:
            return AttachDecision(AttachAction.ATTACH, index=i)
    for state, status in devices:
        if state in _TRANSIENT:
            return AttachDecision(
                AttachAction.WAIT,
                reason=f"device is in {state}: mid-transition, not attachable yet",
            )
    for state, status in devices:
        if status != "X_LINK_SUCCESS":
            return AttachDecision(
                AttachAction.WAIT,
                reason=f"device present but errored ({state}, {status})",
            )
    return AttachDecision(AttachAction.NONE, reason="no DepthAI device on the bus")
