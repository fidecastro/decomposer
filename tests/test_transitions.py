"""Mode-switch guards and depthai attach gating.

The attach cases replay 2026-08-26 by name: a switch that landed while the
camera was mid-reboot attached to its bootloader and wedged the session with
"Couldn't read data from stream: `_bootloader` (X_LINK_ERROR)".
"""

from opal_c1.core.model import Mode
from opal_c1.core.transitions import (
    MIN_SECONDS_BETWEEN_SWITCHES,
    AttachAction,
    Ledger,
    choose_device,
    evaluate_switch,
)


def test_switch_allowed_when_idle_and_cold():
    ledger = Ledger(current=Mode.CALL)
    assert evaluate_switch(ledger, Mode.STUDIO, now=100.0).allowed


def test_switch_rejected_while_transition_in_progress():
    ledger = Ledger(current=Mode.CALL, in_progress=True)
    decision = evaluate_switch(ledger, Mode.STUDIO, now=100.0)
    assert not decision.allowed
    assert "in progress" in decision.reason


def test_switch_rate_limited():
    ledger = Ledger(current=Mode.STUDIO, last_switch_at=100.0)
    decision = evaluate_switch(ledger, Mode.CALL, now=105.0)
    assert not decision.allowed
    assert "reboots" in decision.reason


def test_switch_allowed_after_the_interval():
    ledger = Ledger(current=Mode.STUDIO, last_switch_at=100.0)
    now = 100.0 + MIN_SECONDS_BETWEEN_SWITCHES + 1
    assert evaluate_switch(ledger, Mode.CALL, now).allowed


def test_same_mode_restart_is_not_rate_limited():
    # Re-entering the current mode is a restart: pacing is the health
    # policy's job, and blocking it would block recovery.
    ledger = Ledger(current=Mode.CALL, last_switch_at=100.0)
    assert evaluate_switch(ledger, Mode.CALL, now=101.0).allowed


def test_attach_prefers_flash_booted():
    decision = choose_device([("X_LINK_FLASH_BOOTED", "X_LINK_SUCCESS")])
    assert decision.action is AttachAction.ATTACH
    assert decision.index == 0


def test_attach_accepts_the_non_exclusive_alias():
    decision = choose_device([("X_LINK_BOOTED_NON_EXCLUSIVE", "X_LINK_SUCCESS")])
    assert decision.action is AttachAction.ATTACH


def test_attach_never_targets_bootloader():
    # The 2026-08-26 wedge: mid-reboot, only the bootloader is visible.
    decision = choose_device([("X_LINK_BOOTLOADER", "X_LINK_SUCCESS")])
    assert decision.action is AttachAction.WAIT
    assert "transition" in decision.reason


def test_attach_skips_errored_device():
    # Matches the depthai log line: "skipping ... (status: X_LINK_ERROR)".
    decision = choose_device([("X_LINK_FLASH_BOOTED", "X_LINK_ERROR")])
    assert decision.action is AttachAction.WAIT
    assert "errored" in decision.reason


def test_attach_picks_the_healthy_one_among_several():
    decision = choose_device([
        ("X_LINK_BOOTLOADER", "X_LINK_SUCCESS"),
        ("X_LINK_FLASH_BOOTED", "X_LINK_SUCCESS"),
    ])
    assert decision.action is AttachAction.ATTACH
    assert decision.index == 1


def test_attach_reports_empty_bus():
    decision = choose_device([])
    assert decision.action is AttachAction.NONE
